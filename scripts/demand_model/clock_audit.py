#!/usr/bin/env python
"""Clock audit of the density model's training series.

For every series-year of the v1 dataset this reads the archive parquet
(demand, row ``h`` = hour ``h`` of the year on the source's clock) and
establishes:

* the clock the rows are on: the core clock of the series (``clocks.
  CORE_CLOCKS``), the OPSD anchor for European years (the archive equals
  OPSD's ``utc_timestamp`` load at some lag), and the delay of the year's
  mean daily profile against the rest of the series (a source change moves
  it by whole hours);
* whether a leap year kept 29 February (and lost 31 December) or dropped it
  (the weekly cycle after 1 March tells);
* diagnostics: trough of the mean day in local solar time if the rows were
  UTC, and a daylight-saving test (a fixed clock moves the profile by an
  hour at each change, civil time does not);
* how far v1's pairing was off: the hour of temperature v1 paired with each
  demand hour, and the share of rows whose hour, weekday, month or holiday
  flag differ from the local civil date of the demand's instant.

Writes ``series_clocks.csv`` (one row per series-year: the clock used by
``build_density_demand_dataset.py``, or why the year is left out) and
``clock_misalignment.csv`` into ``--out``. Reads the archive, the v1 dataset
and the R1 audit read-only.

Usage::

    PYTHONPATH=src python scripts/demand_model/clock_audit.py \\
        --archive "/media/.../Demand_forecasting/data/demand_dataset" \\
        --v1-dataset ".../demand_estimation/ecvi/density_demand_dataset.parquet" \\
        --r1-audit "/media/.../Demand_forecasting/r1_revision/cv" --out OUTDIR
"""
from __future__ import annotations

import argparse
import datetime
import sys
import warnings
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import clocks  # noqa: E402

# OPSD columns of the European national series (ISO3 -> OPSD prefix)
OPSD_CODES = {
    "AUT": "AT", "BEL": "BE", "BGR": "BG", "CHE": "CH", "CZE": "CZ", "DEU": "DE",
    "DNK": "DK", "EST": "EE", "ESP": "ES", "FIN": "FI", "FRA": "FR", "GRC": "GR",
    "HRV": "HR", "HUN": "HU", "IRL": "IE", "ITA": "IT", "LTU": "LT", "LUX": "LU",
    "LVA": "LV", "MNE": "ME", "NLD": "NL", "NOR": "NO", "POL": "PL", "PRT": "PT",
    "ROU": "RO", "SRB": "RS", "SWE": "SE", "SVN": "SI", "SVK": "SK", "UKR": "UA",
}
OPSD_LAST_FULL_YEAR = 2019        # the file ends in September 2020
# R1 (s03a_series.CLOCK_TO_UTC): hours to ADD to a row's hour to get UTC
R1_CLOCK_TO_UTC = {"JPN": -9, "CAN": 6, "AUS": -10, "BRA": 3, "USA": 5, "CHN": -8,
                   "IND": -5, "PAK": -5, "BGD": -6, "KHM": -7, "THA": -7, "SGP": -8,
                   "PHL": -8, "TWN": -8, "NZL": -12, "CUB": 5, "PRI": 4, "COL": 5,
                   "GLP": 4, "GUF": 3}
# zonal fetchers dropped 29 February; national parquets kept it (weak tests)
LEAP_MARGIN = 0.15
DELAY_MIN = 0.7          # smaller year-to-year delays are not a clock change
CLEAN_CORR = 0.94        # a shifted profile this close is a clock change
ANCHOR_EQUAL = 0.9       # share of hours equal to OPSD that fixes a clock
MAX_SHIFT = 10           # larger "shifts" are not a plausible clock


# ── profile diagnostics ───────────────────────────────────────────────────


def _shift(p: np.ndarray, s: float) -> np.ndarray:
    """``p(h + s)``, circular and fractional."""
    n = len(p)
    f = np.fft.rfftfreq(n)
    return np.fft.irfft(np.fft.rfft(p) * np.exp(2j * np.pi * f * s), n)


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    a = a - a.mean()
    b = b - b.mean()
    return float((a * b).sum() / np.sqrt((a * a).sum() * (b * b).sum()))


def profile_delay(ref: np.ndarray, p: np.ndarray,
                  lim: float = 12.0) -> tuple[float, float, float]:
    """Delay ``d`` (hours) with ``p(h) ~ ref(h - d)``; correlation at ``d``
    and at 0."""
    best = (-2.0, 0.0)
    for s in np.arange(-lim, lim, 0.05):
        c = _corr(ref, _shift(p, s))
        if c > best[0]:
            best = (c, float(s))
    return round(best[1], 2), round(best[0], 4), round(_corr(ref, p), 4)


def day_matrix(x: np.ndarray) -> np.ndarray:
    d = np.asarray(x[:8760], dtype=float).reshape(365, 24)
    with np.errstate(invalid="ignore", divide="ignore"):
        return d / np.nanmean(d, axis=1, keepdims=True)


def mean_day(x: np.ndarray) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        return np.nanmean(day_matrix(x), axis=0)


def trough_hour(p: np.ndarray) -> float:
    i = int(np.argmin(p))
    n = len(p)
    y0, y1, y2 = p[(i - 1) % n], p[i], p[(i + 1) % n]
    den = y0 - 2 * y1 + y2
    return (i + (0.5 * (y0 - y2) / den if den > 0 else 0.0)) % n


def leap_test(dem: np.ndarray, year: int) -> tuple[float, float]:
    """Correlation of the weekly cycle after 1 March with that of Jan-Feb,
    if the rows kept 29 February and if they dropped it."""
    daily = np.nanmean(np.asarray(dem[:8760], float).reshape(365, 24), axis=1)
    roll = pd.Series(daily).rolling(7, center=True, min_periods=4).mean().to_numpy()
    r = daily / roll
    wd0 = datetime.date(year, 1, 1).weekday()
    days = np.arange(365)
    ref = np.array([np.nanmean(r[:59][(wd0 + days[:59]) % 7 == k]) for k in range(7)])
    post = days >= 60
    out = []
    for extra in (0, 1):
        x, y = r[post], ref[(wd0 + days[post] + extra) % 7]
        ok = np.isfinite(x) & np.isfinite(y)
        out.append(float(np.corrcoef(x[ok], y[ok])[0, 1]) if ok.sum() > 30 else np.nan)
    return out[0], out[1]


def dst_transitions(zone: str, year: int) -> list[tuple[int, float]]:
    """(day of year, offset change in hours) of each DST change of *zone*."""
    z = ZoneInfo(zone)
    out, prev = [], None
    n = 366 if pd.Timestamp(year=year, month=1, day=1).is_leap_year else 365
    for d in range(n):
        t = datetime.datetime(year, 1, 1, 12) + datetime.timedelta(days=d)
        off = t.replace(tzinfo=z).utcoffset().total_seconds() / 3600
        if prev is not None and off != prev:
            out.append((d, off - prev))
        prev = off
    return out


def dst_test(dem: np.ndarray, year: int, zone: str, feb29_kept: bool) -> float:
    """~1 when the rows are on a fixed clock, ~0 on the zone's civil time;
    NaN without DST changes. Weekday profiles two weeks either side."""
    m = day_matrix(dem)
    wd0 = datetime.date(year, 1, 1).weekday()
    leap = pd.Timestamp(year=year, month=1, day=1).is_leap_year

    def row_of(day):
        return day - 1 if (leap and not feb29_kept and day >= 60) else day

    def weekday(row):
        return (wd0 + row + (1 if (leap and not feb29_kept and row >= 59) else 0)) % 7

    vals = []
    for d, dh in dst_transitions(zone, year):
        row = row_of(d)
        before = [k for k in range(row - 14, row - 1) if 0 <= k < 365 and weekday(k) < 5]
        after = [k for k in range(row + 2, row + 15) if 0 <= k < 365 and weekday(k) < 5]
        if len(before) < 5 or len(after) < 5:
            continue
        pb, pa = np.nanmean(m[before], axis=0), np.nanmean(m[after], axis=0)
        if not (np.isfinite(pb).all() and np.isfinite(pa).all()):
            continue
        delay = profile_delay(pb, pa, lim=4.0)[0]
        vals.append(-delay / dh)
    return float(np.mean(vals)) if vals else np.nan


# ── inputs ────────────────────────────────────────────────────────────────


def archive_path(archive: Path, country: str, zone: str, year: int) -> Path:
    name = f"{country}_{zone}_{year}.parquet" if zone else f"{country}_{year}.parquet"
    return archive / country / name


def load_demand(archive: Path, country: str, zone: str, year: int) -> np.ndarray:
    d = pd.read_parquet(archive_path(archive, country, zone, year),
                        columns=["demand_mw"])["demand_mw"].to_numpy(float)[:8760]
    d = d.copy()
    d[~(d > 0)] = np.nan
    return d


def v1_series(v1_dataset: Path) -> pd.DataFrame:
    cols = ["country", "zone", "year", "latitude", "longitude"]
    df = pd.read_parquet(v1_dataset, columns=cols)
    df["zone"] = df["zone"].fillna("").astype(str)
    return (df.groupby(["country", "zone", "year"], as_index=False)
              .agg(lat=("latitude", "first"), lon=("longitude", "first")))


def opsd_anchor(archive: Path, series: pd.DataFrame) -> pd.DataFrame:
    """Lag at which each European archive year equals OPSD's UTC load:
    ``row h = UTC hour h - lag`` (the rows' clock is UTC + lag)."""
    path = archive / "_raw" / "opsd" / "opsd_60min.csv"
    if not path.exists():
        return pd.DataFrame(columns=["country", "year", "opsd_lag", "opsd_equal", "opsd_corr"])
    cols = ["utc_timestamp"] + [f"{v}_load_actual_entsoe_transparency" for v in OPSD_CODES.values()]
    op = pd.read_csv(path, usecols=cols)
    op.index = pd.to_datetime(op.pop("utc_timestamp"), utc=True)
    rows = []
    sel = series[series.country.isin(OPSD_CODES) & (series.zone == "")
                 & (series.year <= OPSD_LAST_FULL_YEAR)]
    for r in sel.itertuples():
        b = load_demand(archive, r.country, "", r.year)
        ref = op[f"{OPSD_CODES[r.country]}_load_actual_entsoe_transparency"].reindex(
            pd.date_range(f"{r.year}-01-01", periods=8760 + 48, freq="h", tz="UTC")
            - pd.Timedelta(hours=24)).to_numpy()
        best = None
        for lag in range(-6, 7):
            o = ref[24 - lag: 24 - lag + 8760]
            ok = np.isfinite(o) & np.isfinite(b)
            if ok.sum() < 1000:
                continue
            eq = float(np.mean(np.abs(o[ok] - b[ok]) <= 1e-6 * np.maximum(1.0, np.abs(b[ok]))))
            cc = float(np.corrcoef(o[ok], b[ok])[0, 1])
            if best is None or (eq, cc) > best[:2]:
                best = (eq, cc, lag)
        if best:
            rows.append({"country": r.country, "year": r.year, "opsd_lag": best[2],
                         "opsd_equal": round(best[0], 3), "opsd_corr": round(best[1], 4)})
    return pd.DataFrame(rows)


# ── decisions ─────────────────────────────────────────────────────────────


def decide_year_clock(core: str, year: int, delay: float, corr_best: float,
                      opsd: dict | None, manual: str | None = None,
                      local_zone: str | None = None) -> tuple[str, str, str]:
    """(clock, status, reason) of one series-year.

    ``status`` is "core", "anchored", "shifted" or "excluded". OPSD equality
    over 90 % of the hours fixes the clock; a European year that matches
    OPSD at no lag (correlation < 0.9) is not the published load and is left
    out, and so is one whose hours equal OPSD's in part (50-90 %) at a lag
    other than the core clock's (mixed clocks). Otherwise the delay of the year's mean day against the series' core
    years decides: under 0.7 h it is the core clock; a delay that lands, with
    the shifted profile matching closely, on a whole-hour offset (within
    0.3 h) or on UTC or the local standard offset (within 0.6 h, interval
    labelling) is a clock change; anything else leaves the clock unknown.
    """
    if manual:
        return manual, "shifted", "manual (source change)"
    if opsd is not None:
        if opsd["opsd_equal"] >= ANCHOR_EQUAL:
            return (clocks.fixed_clock(opsd["opsd_lag"]), "anchored",
                    f"equals OPSD UTC load at lag {opsd['opsd_lag']:+d} h "
                    f"({opsd['opsd_equal']:.0%} of hours)")
        if opsd["opsd_corr"] < 0.9:
            return "", "excluded", (f"matches OPSD at no lag (best r={opsd['opsd_corr']:.2f}): "
                                    "not the published load, clock unknown")
        if opsd["opsd_equal"] >= 0.5:
            if clocks.fixed_clock(opsd["opsd_lag"]) == core:
                return core, "core", (f"{opsd['opsd_equal']:.0%} of hours equal OPSD's at "
                                      "the core clock")
            return "", "excluded", (f"{opsd['opsd_equal']:.0%} of hours equal OPSD's at lag "
                                    f"{opsd['opsd_lag']:+d} h, the rest not: mixed clocks")
    if abs(delay) < DELAY_MIN:
        return core, "core", ""
    off = clocks.standard_offset(core, year) + delay
    if corr_best >= CLEAN_CORR and abs(delay) <= MAX_SHIFT:
        cands = [(abs(off - round(off)), float(round(off)), 0.3), (abs(off), 0.0, 0.6)]
        if local_zone:
            loc = clocks.standard_offset(local_zone, year)
            if loc == int(loc):
                cands.append((abs(off - loc), loc, 0.6))
        fits = [(d, t) for d, t, tol in cands if d <= tol]
        if fits:
            target = min(fits)[1]
            return (clocks.fixed_clock(target), "shifted",
                    f"mean day delayed {delay:+.2f} h vs core years (r={corr_best:.3f})")
    return "", "excluded", (f"mean day delayed {delay:+.2f} h vs core years, "
                            f"not a clean clock change (r={corr_best:.3f})")


def year_delays(profiles: dict[int, np.ndarray]) -> dict[int, tuple[float, float, float]]:
    """Delay of each year's mean day against the series' core years (the
    year agreeing within 0.5 h with most others, and those)."""
    ys = sorted(profiles)
    pair = {(a, b): profile_delay(profiles[a], profiles[b])[0] for a in ys for b in ys if a != b}
    agree = {y: [o for o in ys if o != y and abs(pair[(o, y)]) < 0.5] for y in ys}
    core_y = max(ys, key=lambda y: len(agree[y]))
    core = [core_y] + agree[core_y]
    with np.errstate(invalid="ignore"):
        ref = np.nanmean(np.stack([profiles[y] for y in core]), axis=0)
    return {y: profile_delay(ref, profiles[y]) for y in ys}


# ── v1 misalignment ───────────────────────────────────────────────────────


def misalignment(archive: Path, row: pd.Series, era5: tuple[np.ndarray, np.ndarray] | None,
                 local_zone: str) -> dict:
    """How v1 paired row ``h``: temperature of UTC hour ``h`` and the
    calendar of the row index, against the demand's instant."""
    y = int(row.year)
    inst = clocks.row_instants(y, 8760, row.clock, bool(row.feb29_kept))
    utc_rows = pd.Timestamp(f"{y}-01-01", tz="UTC").value / 3.6e12 + np.arange(8760)
    t_inst = pd.to_datetime(inst * 3600.0, unit="s", utc=True).tz_convert(local_zone)
    v1 = clocks.v1_calendar_features(y, row.country)
    hour_err = (v1["hour"] - np.asarray(t_inst.hour)) % 24
    hour_err = np.where(hour_err > 12, hour_err - 24, hour_err)
    true_dow = np.asarray(t_inst.dayofweek)
    true_month = np.asarray(t_inst.month) - 1
    out = {
        "v1_hour_error_mean_h": round(float(np.mean(hour_err)), 2),
        "v1_hour_error_min_h": float(hour_err.min()), "v1_hour_error_max_h": float(hour_err.max()),
        "v1_wrong_weekday_share": round(float(np.mean(v1["dow"] != true_dow)), 4),
        "v1_wrong_month_share": round(float(np.mean(v1["month"] != true_month)), 4),
        # v1's temperature of row h is UTC hour h; the demand is at inst[h]:
        # positive = the temperature was that many hours later than the demand
        "v1_temp_offset_h": round(float(np.median(utc_rows - inst)), 2),
    }
    if era5 is not None:
        e_inst, e_val = era5
        v1_temp = clocks.sample_at(e_inst, e_val, utc_rows)
        true_temp = clocks.sample_at(e_inst, e_val, inst)
        ok = np.isfinite(v1_temp) & np.isfinite(true_temp)
        out["v1_temp_abs_error_c"] = round(float(np.mean(np.abs(v1_temp[ok] - true_temp[ok]))), 3) if ok.any() else np.nan
    return out


def era5_series(archive: Path, country: str, zone: str, years) -> tuple[np.ndarray, np.ndarray] | None:
    """The archive's UTC ERA5 years of a series (first 8760 UTC hours each)."""
    inst, vals = [], []
    for y in years:
        for name in ([f"era5_{zone}_{y}.npy"] if zone else []) + [f"era5_{y}.npy"]:
            p = archive / "_era5" / country / name
            if p.exists():
                a = np.load(p).astype(float)[:8760]
                inst.append(clocks.row_instants(y, len(a), "UTC", True))
                vals.append(a)
                break
    if not inst:
        return None
    return np.concatenate(inst), np.concatenate(vals)


# ── main ──────────────────────────────────────────────────────────────────


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--archive", type=Path, required=True)
    ap.add_argument("--v1-dataset", type=Path, required=True)
    ap.add_argument("--r1-audit", type=Path, default=None)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    warnings.simplefilter("ignore", RuntimeWarning)     # all-NaN days of gappy years

    series = v1_series(a.v1_dataset)
    print(f"{len(series)} series-years, {series.groupby(['country', 'zone']).ngroups} series")
    opsd = opsd_anchor(a.archive, series)
    opsd_map = {(r.country, r.year): r._asdict() for r in opsd.itertuples(index=False)}

    rows = []
    for (c, z), g in series.groupby(["country", "zone"]):
        lat, lon = float(g.lat.iat[0]), float(g.lon.iat[0])
        local = clocks.series_local_zone(c, lat, lon)
        core = clocks.core_clock(c, z)
        dem = {int(y): load_demand(a.archive, c, z, int(y)) for y in g.year}
        prof = {y: mean_day(d) for y, d in dem.items()}
        delays = year_delays(prof)
        for y, d in dem.items():
            leap = pd.Timestamp(year=y, month=1, day=1).is_leap_year
            kept_r = dropped_r = np.nan
            feb29_kept = True
            if leap:
                kept_r, dropped_r = leap_test(d, y)
                if np.isfinite(kept_r) and np.isfinite(dropped_r) and abs(kept_r - dropped_r) >= LEAP_MARGIN:
                    feb29_kept = kept_r > dropped_r
                else:
                    feb29_kept = not z          # zonal fetchers dropped it
            delay, cb, c0 = delays[y]
            clock, status, reason = decide_year_clock(
                core, y, delay, cb, opsd_map.get((c, y)), clocks.MANUAL_YEAR_CLOCKS.get((c, z, y)), local)
            tr = trough_hour(prof[y])
            rows.append({
                "country": c, "zone": z, "year": y, "lat": lat, "lon": lon,
                "local_zone": local, "core_clock": core, "clock": clock,
                "status": status, "reason": reason, "feb29_kept": feb29_kept,
                "leap_r_kept": kept_r, "leap_r_dropped": dropped_r,
                "trough_rows_h": round(tr, 2),
                "trough_solar_if_utc_h": round((tr + lon / 15.0) % 24, 2),
                "dst_fixed_score": round(dst_test(d, y, local, feb29_kept), 2),
                "delay_vs_core_h": delay, "corr_shifted": cb, "corr_unshifted": c0,
                **{k: v for k, v in opsd_map.get((c, y), {}).items() if k.startswith("opsd_")},
            })
    out = pd.DataFrame(rows)

    # R1 audit: the clock R1 assumed for the same rows, and its trough check
    out["r1_clock"] = [clocks.fixed_clock(-R1_CLOCK_TO_UTC[c]) if c in R1_CLOCK_TO_UTC else "UTC"
                       for c in out.country]
    if a.r1_audit is not None and (a.r1_audit / "diag_clock_check.csv").exists():
        chk = pd.read_csv(a.r1_audit / "diag_clock_check.csv")
        trough = dict(zip(chk.series, chk.trough_solar))
        out["r1_trough_solar_h"] = [trough.get(clocks.series_key(c, z)) for c, z in zip(out.country, out.zone)]
    out.to_csv(a.out / "series_clocks.csv", index=False)

    # v1 misalignment of every kept series-year
    mis = []
    for (c, z), g in out[out.status != "excluded"].groupby(["country", "zone"]):
        era5 = era5_series(a.archive, c, z, sorted(set(g.year) | set(g.year + 1)))
        for r in g.itertuples(index=False):
            mis.append({"country": c, "zone": z, "year": r.year, "clock": r.clock,
                        "local_zone": r.local_zone,
                        **misalignment(a.archive, pd.Series(r._asdict()), era5, r.local_zone)})
    pd.DataFrame(mis).to_csv(a.out / "clock_misalignment.csv", index=False)
    print(out.status.value_counts().to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
