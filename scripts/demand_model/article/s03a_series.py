#!/usr/bin/env python
"""Step 1 of the training table: which series there are, what load they report, and the
area each one covers.

Sources of hourly load
  * the hourly frame assembled for the submission (84 countries; national series plus
    Chinese provinces, Australian states, Brazilian subsystems, Ontario, Thai and Taiwanese
    regions);
  * the Electricity Maps zones in data/validation_subzones (US balancing authorities,
    Indian regions, Japanese utility areas, Italian, Norwegian, Swedish and Danish bidding
    zones, Canadian provinces, Russian price zones). The submission described them as part
    of the archive but they had never been joined to it.

Footprints. Each series is tied to the land its load actually covers, as the FRACTION of
every 0.25 deg cell that belongs to it:
  * Ontario's ten IESO zones are summed into one Ontario series (the submission gave each
    zone the whole province, and filed the Ontario total as Canada);
  * Thai regions are summed to the national load; Taiwanese regional years fill gaps in the
    national series;
  * Brazilian subsystems, and national series that cover part of a country (Philippines =
    Luzon, Malaysia = Peninsular, Great Britain, mainland France, Norway without Svalbard,
    lower-48 USA), use the matching polygons; the series filed as Japan is the TEPCO area.

Time axis. The hourly frame keeps each series-year as 8,760 values in order, without time
stamps, and was built as if every source reported in UTC. Its sources do not share a clock
(UTC, local standard time, wall clocks with daylight saving, hours labelled at their end; some
series change clock between years), hours were closed up over gaps, and three sources left out
29 February. Every hour is filed under its UTC time here, in this order:
  1. series-years whose original files carry time stamps are read from those files (Open Power
     System Data for the European national series 2015-2019, the operators' files for
     Australia, Ontario, Thailand and Taiwan), never adding a series-year the frame did not hold;
  2. the rest are moved from the clock of their source (CLOCKS) to UTC;
  3. years in which a dated grid failure falls in its hour (PINNED), and years that an
     independent UTC record confirms (records.py), are left as they are;
  4. the remaining years are compared with the weekly cycle of their own series and filed
     again where they are displaced (timeaxis.py).
The calendar of every series is then rebuilt: hour in UTC, weekday and holidays on the local
date. s03d_clock_audit.py and s03e_clock_table.py hold the evidence, series-year by series-year.

Temperature is NOT taken from these sources: it is rebuilt for every series in s03b.

Writes dataset/series_hourly.parquet, dataset/series_static.csv, dataset/footprints.npz.
"""
from __future__ import annotations
import datetime, json
import pytz
import numpy as np, pandas as pd, geopandas as gpd
from shapely.geometry import Point
from shapely.ops import unary_union
import timeaxis, records
from paths import (RAW, GRIDS, DATASET, MANIFEST, HOURLY_FRAME as HOURLY, grid, cell_area_km2,
                   load_population, POP_KEY, NLON)
from gridutil import polygon_fraction, block_mean

EM_GEOJSON = RAW / "data/gridded/shapefiles/em_world.geojson"
ADMIN1 = RAW / "data/gridded/shapefiles/ne_10m_admin_1/ne_10m_admin_1_states_provinces.shp"
ZDIR = RAW / "data/validation_subzones"
CAL = ["hour_sin", "hour_cos", "dow_sin", "dow_cos", "month_sin", "month_cos", "is_weekend",
       "is_holiday", "days_to_next_holiday", "days_from_prev_holiday"]

NE_CODE = {"XKX": "KOS", "ALA": "ALD"}        # dataset ISO3 -> Natural Earth ADM0_A3
# national series whose load covers a named part of the country: Electricity Maps polygons
EM_FOOTPRINT = {"JPN": ["JP-TK"], "PHL": ["PH-LU"], "MYS": ["MY-WM"], "GBR": ["GB"], "FRA": ["FR"],
                "NOR": ["NO-NO1", "NO-NO2", "NO-NO3", "NO-NO4", "NO-NO5"], "GLP": ["GP"], "GUF": ["GF"]}
# EIA demand is the lower 48. The Moroccan record is the distribution network of the city of
# Tetouan (2017, ends on 30 December), which the archive had multiplied to the national total.
BBOX_FOOTPRINT = {"USA": ((-125.0, 24.0, -66.0, 50.0), "admin-0 clipped to the lower 48"),
                  "MAR": ((-5.5, 35.5, -5.25, 35.75), "city of Tetouan")}
# Hour by hour the archive's values are 59.306 times the published record (sum of the three zones
# of "Power consumption of Tetouan city", UCI repository, hourly means; mean 71.2 MW): undone here.
RESCALED = {("MAR", ""): 1.0 / 59.306}
FLAT_RUN = 12                                                   # hours; longer runs of one value are filled gaps
# The load of a whole system does not change by a fifth from one hour to the next more than
# occasionally. A year in which it does so in over a tenth of the hours records outages, load
# shedding, generation netted against load or reporting faults, not demand.
JUMP, JUMP_SHARE = 0.20, 0.10
COPY_DAY = 24            # hours; a day or more that repeats the previous day value for value is a filled gap
COPY_YEAR = 0.90         # two years of one series whose hourly departures correlate above this are one record
COPY_SHIFT = 192         # hours of misalignment allowed when comparing two years
BRA_ZONES = {"N": "BR-N", "NE": "BR-NE", "SE": "BR-CS", "S": "BR-S"}
AUS_EXTRA = {"NSW1": ["Australian Capital Territory", "Jervis Bay Territory"]}
# Electricity Maps zones whose load is already in the archive straight from the operator
ZONE_DUPLICATES = {"AU-NSW", "AU-QLD", "AU-SA", "AU-TAS", "AU-VIC", "BR-CS", "BR-N", "BR-NE", "BR-S",
                   "CA-ON", "JP-TK"}
ZONE_POLYGON = {"RUS-EUR": "RU-1", "RUS-SIB": "RU-2"}
ZONE_YEARS = range(2020, 2026)
HOLIDAY_ISO2 = {"BGR": "BG", "GTM": "GT", "MAR": "MA", "XKX": "XK"}
MONTH_DAYS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
# Clock of each operator series, by year. The hourly frame keeps no time stamps and its sources
# do not share a clock; some series change clock from one year to the next because their years
# came from different providers. Each entry: (first year, last year, time zone, hours).
# The recorded hour plus `hours` is wall-clock time in `time zone` (daylight saving included),
# or UTC when the time zone is None. Countries not listed are recorded in UTC.
# Ontario: the first value of a year is IESO's "Hour 1", the hour that ENDS at 01:00 EST.
# What establishes each clock is in dataset/clocks.csv (s03e_clock_table.py).
CLOCKS = {
    "JPN": [(0, 9999, None, -9)], "CAN": [(0, 9999, None, 5)], "AUS": [(0, 9999, None, -10)],
    "BRA": [(0, 9999, "America/Sao_Paulo", 0)], "USA": [(0, 9999, "America/Chicago", 0)], "CHN": [(0, 9999, None, -8)],
    "IND": [(0, 2023, None, -5), (2024, 9999, None, 0)], "PAK": [(0, 9999, None, -5)],
    "BGD": [(0, 2024, None, -1), (2025, 9999, None, 0)], "KHM": [(0, 9999, None, -7)],
    "THA": [(0, 9999, None, -7)], "SGP": [(0, 2015, None, 0), (2016, 9999, None, -8)],
    "PHL": [(0, 9999, None, -8)], "TWN": [(0, 2021, None, -8), (2022, 9999, None, 0)],
    "NZL": [(0, 9999, "Pacific/Auckland", 0)], "CUB": [(0, 9999, "America/Havana", 0)],
    "PRI": [(0, 9999, None, 4)], "COL": [(0, 2017, None, 0), (2018, 2022, None, 5), (2023, 9999, None, 0)],
    "GLP": [(0, 9999, None, 4)], "GUF": [(0, 9999, None, 3)],
    "PAN": [(0, 2019, None, 5), (2020, 9999, None, 0)],
    "URY": [(0, 2017, None, 0), (2018, 2023, None, 3), (2024, 9999, None, 0)],
    "KOR": [(0, 2024, None, 0), (2025, 9999, None, -9)],
    "PER": [(0, 1997, None, 5), (1998, 9999, None, -1)],
    "CHL": [(0, 1999, "America/Santiago", 0), (2000, 2024, None, -1), (2025, 9999, None, 0)],
    "PRY": [(0, 2008, "America/Asuncion", 0), (2009, 9999, "America/Asuncion", -3)],
    "RUS": [(0, 9999, "Europe/Moscow", 0)], "MEX": [(0, 9999, None, -1)],
    "ZAF": [(0, 2022, None, -2), (2023, 9999, None, 0)],
    "DOM": [(0, 2020, None, 0), (2021, 2021, None, 4), (2022, 9999, None, 0)],
    "ALB": [(0, 9999, None, -1)], "BIH": [(0, 9999, None, -1)], "GEO": [(0, 9999, None, -1)], "MDA": [(0, 9999, None, -1)],
    "MKD": [(0, 9999, None, -1)], "XKX": [(0, 9999, None, -1)], "UKR": [(0, 9999, None, -1)],
}
# Records kept by settlement period on a wall clock, stored without the hour that is repeated when
# clocks go back: from that night to the end of the year every hour is filed one hour early
REPEATED_HOUR_REMOVED = {"GBR": ("Europe/London", 2024)}          # time zone, last year affected
# European national series that are the load published by ENTSO-E: 2015-2019 are read from the Open
# Power System Data release, which carries UTC time stamps (OPSD country code where it is not ISO-2)
OPSD_YEARS = range(2015, 2020)
OPSD_NOT = {"GBR", "CYP"}                                         # in the archive from another publisher
# Sources that left 29 February out: from 1 March of a leap year every hour is filed a day early
LEAP_DAY_REMOVED = {"AUS", "CAN", "BRA"}
# Years in which a dated grid failure is found at its hour (s03d_clock_audit.py), or that come
# from files with time stamps: their time axis is not touched
PINNED = {"PAK": {2015}, "PAN": {2017, 2019}, "CRI": {2017}, "URY": {2019}, "IND": {2020}, "MEX": {2017, 2020},
          "BGD": {2022}, "LKA": {2025}, "CHL": {2010, 2011, 2025}, "PER": {2007}, "ESP": {2025}, "PRT": {2025},
          "DOM": {2025}, "TWN": {2017, 2018, 2019, 2020, 2021, 2022}, "THA": {2023, 2024},
          "ZAF": set(range(2019, 2026))}       # South Africa: daily cycle too irregular to be compared between years
# Frame years that are not the load of the country: against the published load of the same hours
# they stand at another level (Luxembourg, 1.25 to 1.5 times) and do not follow it
NOT_THE_SERIES = {"LUX": range(2018, 2100)}
# The years that set the reference cycle of a series, where its years split between clocks
ANCHOR_YEARS = {"DOM": {2018, 2019, 2020, 2025}}
# Whole days by which a series is filed late after its years have been brought together; the
# day of lowest load of the week then falls on Sunday, as it does in every other series
DAY_SHIFT = {"CUB": 1, "KHM": 2}
# Original files that carry their own time stamps replace the frame for these series
TIMESTAMPED = RAW / "data" / "new_sources"
ORIGINALS = RAW / "data" / "demand_dataset" / "_raw"
ONTARIO_ZONES = ["Northwest", "Northeast", "Ottawa", "East", "Toronto", "Essa", "Bruce", "Southwest", "Niagara", "West"]
# Zones whose summed load must track the operator's national series hour by hour; India's do not
# (r = 0.52 at the best lag, against 0.91 to 1.00 for Italy, Sweden and the USA)
ZONE_SETS_REJECTED = {"IND"}
TZ_ISO2 = {"XKX": "RS", "ALA": "AX", "GLP": "GP", "GUF": "GF", "PRI": "PR", "FRO": "FO", "TWN": "TW"}


def holiday_features(iso3, year, clip=7):
    """is_holiday, days to the next and from the previous holiday (per day), as in the frame."""
    import holidays
    jan1 = datetime.date(year, 1, 1)
    off = np.array([], int)
    for code in (iso3, HOLIDAY_ISO2.get(iso3)):          # as in the hourly frame: ISO3 first
        if code is None:
            continue
        try:
            hol = holidays.country_holidays(code, years=[year - 1, year, year + 1])
            off = np.array(sorted({(d - jan1).days for d in hol.keys()}))
            break
        except (NotImplementedError, KeyError, AttributeError, TypeError):
            continue
    is_h = np.zeros(365, np.float32)
    if off.size == 0:
        return is_h, np.full(365, clip, np.float32), np.full(365, clip, np.float32)
    is_h[off[(off >= 0) & (off < 365)]] = 1.0
    diff = off[None, :] - np.arange(365)[:, None]
    nxt = np.where(diff >= 0, diff, 10_000).min(axis=1); prv = np.where(diff <= 0, -diff, 10_000).min(axis=1)
    return is_h, np.minimum(nxt, clip).astype(np.float32), np.minimum(prv, clip).astype(np.float32)


def zone_year_load(zone, iso3, year, load):
    """Valid hours of one zone-year, indexed by hour of the UTC year (first 8,760)."""
    hrs = pd.date_range(f"{year}-01-01", periods=8760, freq="h", tz="UTC")
    fr = pd.DataFrame({"country": iso3, "zone": zone, "year": year, "time_idx": np.arange(8760),
                       "demand_mw": load.reindex(hrs).to_numpy()})
    return fr[np.isfinite(fr.demand_mw) & (fr.demand_mw > 0)]


def to_utc(df):
    """Move every operator series-year from the clock of its source to UTC (hours that leave
    the year are re-filed under the year they belong to)."""
    out = []
    for (c, y), g in df.groupby(["country", "year"], sort=False):
        tz, hours = next(((z, h) for a, b, z, h in CLOCKS.get(c, []) if a <= y <= b), (None, 0))
        idx = g.time_idx.to_numpy()
        leap = c in LEAP_DAY_REMOVED and y % 4 == 0
        if leap:
            idx = np.where(idx >= 59 * 24, idx + 24, idx)
        if c in REPEATED_HOUR_REMOVED and y <= REPEATED_HOUR_REMOVED[c][1]:
            zone = pytz.timezone(REPEATED_HOUR_REMOVED[c][0])
            back = [t for t, i in zip(zone._utc_transition_times, zone._transition_info) if t.year == y and i[1] == datetime.timedelta(0)]
            if back:
                idx = np.where(idx >= (back[-1] - datetime.datetime(y, 1, 1)).total_seconds() // 3600, idx + 1, idx)
                leap = True                                              # falls through to the re-filing below
        if tz is None and hours == 0 and not leap:
            out.append(g); continue
        t = pd.Timestamp(f"{y}-01-01") + pd.to_timedelta(idx + hours, unit="h")
        if tz is not None:
            t = t.tz_localize(tz, ambiguous=np.ones(len(t), bool), nonexistent="shift_forward").tz_convert("UTC").tz_localize(None)
        yy = t.year.to_numpy()
        ti = ((t - pd.to_datetime(pd.Series(yy).astype(str) + "-01-01").to_numpy()) / pd.Timedelta(hours=1)).astype(int)
        out.append(g.assign(year=yy, time_idx=np.asarray(ti)))
    df = pd.concat(out, ignore_index=True)
    return df[df.time_idx < 8760].drop_duplicates(["country", "zone", "year", "time_idx"])


def std_offset(iso3, lon):
    """Standard UTC offset (hours) of the country's time zone nearest to a longitude."""
    import pytz, pycountry
    a2 = TZ_ISO2.get(iso3) or pycountry.countries.get(alpha_3=iso3).alpha_2
    best = None
    for z in pytz.country_timezones.get(a2, []):
        std = min(pytz.timezone(z).utcoffset(datetime.datetime(2021, m, 15)).total_seconds() / 3600 for m in (1, 7))
        if best is None or abs(std * 15 - lon) < best[0]:
            best = (abs(std * 15 - lon), std)
    return best[1] if best else round(lon / 15)


def calendar(iso3, year, idx, offset):
    """Calendar columns for UTC hours `idx` of `year`: hour and month in UTC, weekday and
    holidays on the local date (UTC + offset)."""
    hod = idx % 24
    month = np.repeat(np.arange(12), np.array(MONTH_DAYS) * 24)[idx]
    lday = np.floor((idx + offset) / 24.0).astype(int)                  # local day of year, may be -1 or 365
    dow = (datetime.date(year, 1, 1).weekday() + lday) % 7
    ih, dn, dp = holiday_features(iso3, year)
    k = np.clip(lday, 0, 364)
    return {"hour_sin": np.sin(2 * np.pi * hod / 24), "hour_cos": np.cos(2 * np.pi * hod / 24),
            "dow_sin": np.sin(2 * np.pi * dow / 7), "dow_cos": np.cos(2 * np.pi * dow / 7),
            "month_sin": np.sin(2 * np.pi * month / 12), "month_cos": np.cos(2 * np.pi * month / 12),
            "is_weekend": (dow >= 5).astype(float), "is_holiday": ih[k],
            "days_to_next_holiday": dn[k], "days_from_prev_holiday": dp[k]}


EM_ZONES: set = set()


def replicated_years(df, n=8784):
    """Years of a series that are another year of the same series written again.

    The hourly load of each year is reduced to its departures from the mean week and from the
    15-day running level; two genuine years share little of that, a replica shares all of it.
    """
    out = set()
    for s, g in df.groupby("series"):
        res = {}
        for y, h in g.groupby("year"):
            v = np.full(n, np.nan); v[h.time_idx.to_numpy()] = np.log(h.demand_mw.to_numpy())
            v = v - pd.Series(v).rolling(24 * 15, center=True, min_periods=24 * 5).mean().to_numpy()
            v = v - pd.Series(v).groupby(np.arange(n) % 168).transform("mean").to_numpy()
            v = np.nan_to_num(v)
            res[y] = (np.fft.rfft(v, 2 * n), np.sqrt((v ** 2).sum()))
        ys = sorted(res)
        for i, a in enumerate(ys):
            for b in ys[i + 1:]:
                if res[a][1] == 0 or res[b][1] == 0:
                    continue
                c = np.fft.irfft(np.conj(res[a][0]) * res[b][0], 2 * n) / (res[a][1] * res[b][1])
                if max(c[:COPY_SHIFT + 1].max(), c[-COPY_SHIFT:].max()) > COPY_YEAR:
                    out |= {f"{s}_{a}", f"{s}_{b}"}
    return out


def operator_series():
    """Operator series of the hourly frame on the clock of their sources, zones summed where the
    footprint is their union. Returns (frame, number of series-years in the submitted frame,
    the (country, zone, year) the submitted frame holds once its zones are summed)."""
    print("loading hourly frame ...", flush=True)
    df = pd.read_parquet(HOURLY, columns=["country", "zone", "year", "time_idx", "demand_mw"])
    df["zone"] = df["zone"].fillna("").astype(str); df["country"] = df["country"].astype(str)
    df = df[df.demand_mw > 0]
    n0 = df.groupby(["country", "zone", "year"]).ngroups
    k = df[["country", "zone", "year"]].drop_duplicates()
    k.loc[k.country == "CAN", "zone"] = "ON"; k.loc[k.country.isin(["THA", "TWN"]), "zone"] = ""
    held = set(zip(k.country, k.zone, k.year))

    def aggregate(sub):
        """Sum simultaneous zonal loads, keeping only hours every zone reported."""
        nz = sub.zone.nunique()
        out = sub.groupby(["year", "time_idx"]).agg(demand_mw=("demand_mw", "sum"), _n=("zone", "nunique")).reset_index()
        return out[out._n == nz].drop(columns="_n")

    parts = [df[~df.country.isin(["CAN", "THA", "TWN"])], df[(df.country == "TWN") & (df.zone == "") & (df.year >= 2022)]]
    can = df[df.country == "CAN"]
    on = aggregate(can[can.zone != ""])
    on25 = can[(can.zone == "") & ~can.year.isin(on.year.unique())].drop(columns=["country", "zone"])
    parts.append(pd.concat([on, on25], ignore_index=True).assign(country="CAN", zone="ON"))
    df = pd.concat(parts, ignore_index=True)
    for (iso3, zone), f in RESCALED.items():
        df.loc[(df.country == iso3) & (df.zone == zone), "demand_mw"] *= f
    df.loc[(df.country == "JPN") & (df.zone == ""), "zone"] = "JP-TK"      # the TEPCO area
    return df, n0, held


def timestamped_series(have):
    """Series read from files that carry their own time stamps, in UTC, hours labelled at their
    start: Thailand and Taiwan (2017-2021) from the operators' files (local time), the Australian
    regions from AEMO's files, Ontario from IESO's, and the European national series in `have`
    from Open Power System Data (2015-2019). Columns of the operator frame, plus `record`."""
    out = []
    th = pd.concat([pd.read_csv(f, encoding="utf-8-sig") for f in sorted((TIMESTAMPED / "thailand").glob("system_*.csv"))])
    t = pd.to_datetime(th.datetime, format="%d/%m/%Y %H:%M").dt.floor("h") - pd.Timedelta(hours=7)
    load = th[[c for c in th.columns if c.endswith("_demand")]].sum(axis=1, min_count=5)
    out.append(("THA", "", "operator's file", load.groupby(t.to_numpy()).mean()))
    tw = pd.read_csv(TIMESTAMPED / "taiwan" / "taiwan_loadarea_10min.csv", parse_dates=["datetime"])
    load = tw[["south", "north", "east", "central"]].sum(axis=1, min_count=4)
    load = load.groupby((tw.datetime.dt.floor("h") - pd.Timedelta(hours=8)).to_numpy()).mean()
    out.append(("TWN", "", "operator's file", load[(load.index >= "2017-01-01") & (load.index < "2022-01-01")]))
    # AEMO: market time (UTC+10 all year); each value is stamped at the END of its period
    # (30 minutes to September 2021, 5 minutes since)
    au = pd.concat([pd.read_csv(f, usecols=["REGION", "SETTLEMENTDATE", "TOTALDEMAND"])
                    for f in sorted((ORIGINALS / "aemo_nem").glob("pd_*.csv"))], ignore_index=True).drop_duplicates(["REGION", "SETTLEMENTDATE"])
    t = (pd.to_datetime(au.SETTLEMENTDATE, format="%Y/%m/%d %H:%M:%S") - pd.Timedelta(minutes=1)).dt.floor("h") - pd.Timedelta(hours=10)
    for z, g in au.assign(t=t.to_numpy()).groupby("REGION"):
        out.append(("AUS", z, "operator's file", g.groupby("t").TOTALDEMAND.mean()))
    # IESO: Eastern Standard Time all year; "Hour 1" is the hour that ends at 01:00
    on = pd.concat([pd.read_csv(f, skiprows=3) for f in sorted((ORIGINALS / "ieso").glob("ieso_zonal_*.csv"))], ignore_index=True)
    t = pd.to_datetime(on.Date.astype(str).str.replace("/", "-")) + pd.to_timedelta(on.Hour - 1 + 5, unit="h")
    out.append(("CAN", "ON", "operator's file", pd.Series(on[ONTARIO_ZONES].sum(axis=1, min_count=len(ONTARIO_ZONES)).to_numpy(), index=t.to_numpy())))
    op = records.opsd()
    for iso3 in op.columns:
        if iso3 not in OPSD_NOT and iso3 in have:
            out.append((iso3, "", "Open Power System Data", op[iso3][op.index.year.isin(OPSD_YEARS)]))
    frames = []
    for iso3, zone, record, v in out:
        v = v[np.isfinite(v) & (v > 0)]
        jan1 = pd.to_datetime(pd.Series(v.index.year).astype(str) + "-01-01").to_numpy()
        frames.append(pd.DataFrame({"country": iso3, "zone": zone, "year": v.index.year, "record": record,
                                    "time_idx": ((v.index - jan1) / pd.Timedelta(hours=1)).astype(int), "demand_mw": v.to_numpy()}))
    df = pd.concat(frames, ignore_index=True)
    return df[df.time_idx < 8760]


def main() -> int:
    lat, lon = grid()
    lon2d, lat2d = np.meshgrid(lon, lat)
    area2d = cell_area_km2(lat)[:, None] * np.ones((1, NLON))
    pop = load_population()
    gdp = np.load(GRIDS / "gdp_2020_025.npz")["gdp"]
    cg = np.load(GRIDS / "countries_025.npz")
    adm, land_frac = cg["adm0_a3"], cg["land_frac"].astype(np.float64)
    region_wb, subregion, income = cg["region_wb"], cg["subregion"], cg["income_grp"]
    code_idx = {c: i for i, c in enumerate(adm)}
    fine = np.load(GRIDS / "countries_fine.npz")["fine"]          # 0.025 deg country raster
    em = gpd.read_file(EM_GEOJSON).set_index("zoneName")
    a1 = gpd.read_file(ADMIN1); a1 = a1[a1.geometry.notna()]
    man = json.load(open(MANIFEST))

    def em_frac(zones):
        return polygon_fraction(unary_union([em.loc[z, "geometry"] for z in zones]))

    def admin1_frac(iso3, zlat, zlon, extra_names=()):
        sub = a1[a1.adm0_a3 == iso3]
        hit = sub[sub.contains(Point(zlon, zlat))]
        if hit.empty:
            return None
        return polygon_fraction(unary_union(list(hit.geometry) + list(sub[sub.name.isin(extra_names)].geometry)))

    def national_frac(iso3):
        if iso3 in EM_FOOTPRINT:
            return em_frac(EM_FOOTPRINT[iso3]), "EM zone " + "+".join(EM_FOOTPRINT[iso3])
        ne = NE_CODE.get(iso3, iso3)
        if ne not in code_idx:
            return None, "missing"
        f = block_mean(fine == code_idx[ne])
        if iso3 in BBOX_FOOTPRINT:
            (x0, y0, x1, y1), label = BBOX_FOOTPRINT[iso3]
            return f * ((lon2d >= x0) & (lon2d <= x1) & (lat2d >= y0) & (lat2d <= y1)), label
        return f, "admin-0"

    df, n0, held = operator_series()
    df = df[~pd.Series([y in NOT_THE_SERIES.get(c, ()) for c, y in zip(df.country, df.year)], index=df.index)]
    K = ["country", "zone", "year"]
    ts = timestamped_series(set(df.country[df.zone == ""]))
    ts = ts[~pd.Series([y in NOT_THE_SERIES.get(c, ()) for c, y in zip(ts.country, ts.year)], index=ts.index)]
    ts = ts[[k in held for k in zip(ts.country, ts.zone, ts.year)]]      # no series-year the submitted frame did not hold
    full = ts.groupby(K).size()
    full = set(full.index[full >= 8000])
    ts = ts[[k in full for k in zip(ts.country, ts.zone, ts.year)]]
    # the level of a replaced year has to be that of the frame's own year
    lv = (ts.groupby(K).demand_mw.mean() / df.groupby(K).demand_mw.mean()).dropna()
    off = lv[(lv < 0.9) | (lv > 1.1)]
    if len(off):
        raise SystemExit(f"time-stamped record at another level than the frame's: {off.round(2).to_dict()}")
    record = ts.drop_duplicates(K).set_index(K).record
    df = to_utc(df)
    df = pd.concat([df[~pd.Series(list(zip(df.country, df.zone, df.year)), index=df.index).isin(full)], ts.drop(columns="record")], ignore_index=True)
    name = lambda c, z: c + ("_" + z if z else "")
    df["series"] = df.country + np.where(df.zone != "", "_" + df.zone, "")
    key = df.drop_duplicates("series").set_index("series")[["country", "zone"]]
    pinned = {c: set(v) for c, v in PINNED.items()}
    stamped = {}
    for c, z, y in full:
        stamped.setdefault(name(c, z), set()).add(y)
        pinned.setdefault(name(c, z), set()).add(y)
    anchors = dict(ANCHOR_YEARS)
    anchors.update({s: ys for s, ys in stamped.items() if len(ys) >= 2 and s not in ("THA", "TWN")})
    print(f"series-years read from time-stamped files: {len(full)} in {len({c for c, _, _ in full})} countries "
          f"({record.value_counts().to_dict()})")
    print(f"operator series-years: {n0} in the submitted frame -> {df.groupby(['country', 'zone', 'year']).ngroups}")
    # years of the frame that an independent UTC record confirms are not touched either
    todo = set(zip(df.series, df.year)) - {(s, y) for s, ys in pinned.items() for y in ys}
    seen = records.compare(df, keys=todo)
    seen = seen[seen.ref_confirms]
    for s, y in zip(seen.series, seen.year):
        pinned.setdefault(s, set()).add(y)
    print(f"frame series-years confirmed by an independent UTC record: {len(seen)} in {seen.series.nunique()} series")
    # every hour under its own time: years on another clock, hours closed up over gaps, leap days removed
    fixed, rep = timeaxis.harmonise(df, anchors=anchors, pinned=pinned, passes=3)
    kz = [tuple(key.loc[s]) + (y,) for s, y in zip(rep.series, rep.year)]
    rep["record"] = [record.get(k, "submitted frame") for k in kz]
    by = dict(zip(zip(seen.series, seen.year), seen.ref))
    rep["confirmed_by"] = [by.get(k, "") for k in zip(rep.series, rep.year)]
    rep.to_csv(DATASET / "time_axis.csv", index=False)
    print("time axis of operator series-years:", rep.verdict.value_counts().to_dict(),
          "| months removed:", int(rep.get("months_removed", pd.Series(dtype=float)).sum()))
    print("  displaced and not recoverable, removed:", sorted(rep.series[rep.verdict == "dropped"] + "_" + rep.year[rep.verdict == "dropped"].astype(str)))
    for iso3, days in DAY_SHIFT.items():
        m = fixed.series == iso3
        t = pd.to_datetime(fixed.year[m].astype(str) + "-01-01") + pd.to_timedelta(fixed.time_idx[m] - 24 * days, unit="h")
        fixed.loc[m, "time_idx"] = ((t - pd.to_datetime(t.dt.year.astype(str) + "-01-01")) / pd.Timedelta(hours=1)).astype(int).to_numpy()
        fixed.loc[m, "year"] = t.dt.year.to_numpy()
    fixed = fixed[(fixed.time_idx >= 0) & (fixed.time_idx < 8760)].drop_duplicates(["series", "year", "time_idx"])
    df = fixed.assign(country=key.country.reindex(fixed.series).to_numpy(), zone=key.zone.reindex(fixed.series).to_numpy()).drop(columns="series")

    # ── Electricity Maps zones ──
    zrows, zframes = [], []
    for zdir in sorted(p for p in ZDIR.iterdir() if p.is_dir()):
        zone = zdir.name; iso3 = json.load(open(zdir / "metadata.json"))["iso3"]
        load = pd.concat([pd.read_parquet(p, columns=["timestamp", "demand_mw"]) for p in sorted(zdir.glob("*.parquet"))])
        load = load.drop_duplicates("timestamp").set_index("timestamp").demand_mw
        load.index = pd.to_datetime(load.index, utc=True)
        if iso3 in ZONE_SETS_REJECTED:
            print(f"  zone {zone}: set does not reproduce the operator's national series, skipped"); continue
        fr = [f for f in (zone_year_load(zone, iso3, y, load) for y in ZONE_YEARS) if len(f) >= 8000]
        if not fr:
            print(f"  zone {zone}: no year with at least 8,000 valid hours, skipped"); continue
        fr = pd.concat(fr, ignore_index=True)
        zrows.append({"zone": zone, "country": iso3, "mean_mw": fr.demand_mw.mean(),
                      "repeats": float((fr.demand_mw.diff() == 0).mean())})
        zframes.append(fr)
    zq = pd.DataFrame(zrows)
    EM_ZONES.update(zq.zone)

    # ── footprints and statics ──
    print(f"Electricity Maps zones: {len(zq)} | already in the archive from the operator: "
          f"{sorted(zq.zone[zq.zone.isin(ZONE_DUPLICATES)])}")
    zq = zq[~zq.zone.isin(ZONE_DUPLICATES)]
    zframes = [f for f in zframes if f.zone.iloc[0] not in ZONE_DUPLICATES]
    units = ([(c, z, False) for (c, z), _ in df.groupby(["country", "zone"])]
             + [(c, z, True) for c, z in zip(zq.country, zq.zone)])
    recs, fp = [], {}
    for iso3, zone, is_em in units:
        if is_em:
            f, how = em_frac([ZONE_POLYGON.get(zone, zone)]), "EM zone " + zone
        elif zone == "":
            f, how = national_frac(iso3)
        elif (iso3, zone) == ("JPN", "JP-TK"):
            f, how = em_frac(["JP-TK"]), "EM zone JP-TK"
        elif (iso3, zone) == ("CAN", "ON"):
            f, how = em_frac(["CA-ON"]), "EM zone CA-ON"
        elif iso3 == "BRA":
            f, how = em_frac([BRA_ZONES[zone]]), "EM zone " + BRA_ZONES[zone]
        else:                                        # Chinese provinces, Australian states
            z = man[iso3]["zones"][zone]
            f, how = admin1_frac(iso3, z["lat"], z["lon"], AUS_EXTRA.get(zone, ())), "admin-1"
        if f is None or not f.any():
            print(f"  !! no footprint for {iso3} {zone}"); continue
        # people and GDP of a shared cell are split in proportion to the land each side holds
        with np.errstate(divide="ignore", invalid="ignore"):
            share = np.where(f > 0, np.clip(f / np.maximum(land_frac, f), 0.0, 1.0), 0.0)
        A, P, G = float((f * area2d).sum()), float((pop * share).sum()), float((gdp * share).sum())
        if min(A, P, G) <= 0:
            print(f"  !! empty statics for {iso3} {zone}"); continue
        w = pop * share
        ne = code_idx.get(NE_CODE.get(iso3, iso3))
        series = iso3 + ("_" + zone if zone else "")
        r_, c_ = np.nonzero(w > 0)
        fp[series] = np.stack([r_, c_, w[r_, c_]]).astype(np.float32)      # cells and their people
        recs.append({"series": series, "country": iso3, "zone": zone, "footprint": how, "source": "Electricity Maps" if is_em else "operator",
                     "area_km2": A, "tot_pop": P, "tot_gdp": G,
                     "latitude": float((lat2d * w).sum() / w.sum()), "longitude": float((lon2d * w).sum() / w.sum()),
                     "region_wb": region_wb[ne], "subregion": subregion[ne], "income_grp": income[ne]})
    st = pd.DataFrame(recs)
    st.loc[st.country.isin(["GLP", "GUF"]), "region_wb"] = "Latin America & Caribbean"
    st.loc[st.country == "GLP", "subregion"] = "Caribbean"; st.loc[st.country == "GUF", "subregion"] = "South America"

    # zones failing quality checks: flat-lined records, or series that are not consumption
    zq = zq.merge(st.loc[st.source == "Electricity Maps", ["zone", "tot_pop"]], on="zone", how="left")
    zq["kwh_per_cap"] = zq.mean_mw * 8760e3 / zq.tot_pop
    bad = set(zq.zone[(zq.repeats > 0.05) | (zq.kwh_per_cap > 50_000)])
    print(f"rejected by quality checks: {sorted(bad)}")
    keepz = [f for f in zframes if f.zone.iloc[0] not in bad]
    st = st[~((st.source == "Electricity Maps") & st.zone.isin(bad))]
    df = pd.concat([df] + keepz, ignore_index=True)
    df["series"] = df.country + np.where(df.zone != "", "_" + df.zone, "")
    df = df[df.series.isin(st.series)]
    df["group_id"] = df.series + "_" + df.year.astype(str)
    # a value repeated for FLAT_RUN consecutive hours or more is a filled gap or a placeholder, not a reading
    df = df.sort_values(["series", "year", "time_idx"]).reset_index(drop=True)
    new_run = ((df.group_id != df.group_id.shift()) | (df.demand_mw != df.demand_mw.shift())
               | (df.time_idx != df.time_idx.shift() + 1)).cumsum()
    flat = new_run.map(new_run.value_counts()) >= FLAT_RUN
    print(f"flat-lined hours removed: {int(flat.sum()):,} in {df.series[flat].nunique()} series")
    df = df[~flat]
    # so is a day copied from the day before
    same = df.merge(df[["group_id", "time_idx", "demand_mw"]].assign(time_idx=lambda x: x.time_idx + 24),
                    on=["group_id", "time_idx"], how="left", suffixes=("", "_prev"))
    same = (same.demand_mw == same.demand_mw_prev).to_numpy()
    run = ((df.group_id != df.group_id.shift()) | (df.time_idx != df.time_idx.shift() + 1)
           | (same != np.r_[False, same[:-1]])).cumsum()
    copied = same & (run.map(run.value_counts()) >= COPY_DAY).to_numpy()
    print(f"hours copied from the previous day removed: {int(copied.sum()):,} in {sorted(df.series[copied].unique())}")
    df = df[~copied]
    replica = replicated_years(df)
    print(f"series-years removed as replicas of another year: {len(replica)} -> {sorted(replica)}")
    df = df[~df.group_id.isin(replica)]
    prev = df.demand_mw.shift()
    step = (df.group_id == df.group_id.shift()) & (df.time_idx == df.time_idx.shift() + 1)
    jump = ((df.demand_mw - prev).abs() / prev > JUMP).where(step)
    erratic = jump.groupby(df.group_id).mean()
    erratic = set(erratic.index[erratic > JUMP_SHARE])
    print(f"series-years removed as erratic: {len(erratic)} -> {sorted(erratic)}")
    df = df[~df.group_id.isin(erratic)]
    n = df.groupby("group_id").time_idx.transform("size")
    df = df[n >= 8000].sort_values(["series", "year", "time_idx"]).reset_index(drop=True)   # complete years only
    st["utc_offset"] = [std_offset(c, lo) for c, lo in zip(st.country, st.longitude)]
    off = st.set_index("series").utc_offset
    cal = {c: np.empty(len(df), np.float32) for c in CAL}
    for (series, year), ix in df.groupby(["series", "year"]).indices.items():
        cols = calendar(series[:3], int(year), df.time_idx.to_numpy()[ix], off[series])
        for c in CAL:
            cal[c][ix] = cols[c]
    for c in CAL:
        df[c] = cal[c]

    st["log_pop_density"] = np.log10(st.tot_pop / st.area_km2)
    st["log_gdp_density"] = np.log10(st.tot_gdp / st.area_km2)
    st["log_gdp_per_cap"] = np.log10(st.tot_gdp / st.tot_pop)
    q = df.groupby("series").agg(years=("year", "nunique"), y0=("year", "min"), y1=("year", "max"),
                                 hours=("year", "size"), mean_mw=("demand_mw", "mean")).reset_index()
    st = st.merge(q, on="series")
    st["kwh_per_cap"] = st.mean_mw * 8760e3 / st.tot_pop
    st.to_csv(DATASET / "series_static.csv", index=False)
    np.savez_compressed(DATASET / "footprints.npz", **{s: fp[s] for s in st.series})
    df[["group_id", "series", "country", "zone", "year", "time_idx", "demand_mw", *CAL]].to_parquet(
        DATASET / "series_hourly.parquet", index=False)
    print(f"saved series_hourly.parquet: {len(df):,} hours | {df.group_id.nunique()} series-years | "
          f"{df.series.nunique()} series | {df.country.nunique()} countries | population = {POP_KEY}")
    print("series by World Bank region:", st.groupby("region_wb").size().to_dict())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
