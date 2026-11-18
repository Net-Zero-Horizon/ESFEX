"""Predictors of the demand density model, built as its training table built them.

The shipped model (``demand_density_v2``, the final model of the article
"Global map of electricity demand sensitivity to warming learned from observed
hourly load") was trained on demand series audited onto UTC. Its 26 predictors
are, per location and hour:

- ``log_pop_density``, ``log_gdp_density``, ``log_gdp_per_cap``, ``latitude``,
  ``longitude``: the place;
- ``temperature``, ``hdd`` (below 18 °C), ``cdd`` (above 24 °C): the 2 m
  temperature of that instant;
- ``hour_sin/cos`` and ``month_sin/cos``: the UTC hour and month;
- ``dow_sin/cos``, ``is_weekend``, ``is_holiday``, ``days_to_next_holiday``,
  ``days_from_prev_holiday``: the local date, at the standard offset of the
  country's time zone nearest to the longitude;
- ``lhour_sin/cos``: local solar time (the UTC hour turned by the longitude,
  15° per hour); ``smonth_sin/cos``: the month shifted by six in the southern
  hemisphere;
- ``t_mean``, ``t_range``: the long-run climate of the place, mean and 5-95 %
  spread of its daily mean temperature;
- ``hh_elec_share``, ``bld_share_elec``: how electricity is used there
  (share of household energy that is electric; share of electricity used in
  buildings).

The functions here follow the article's code line by line (copied in
``scripts/demand_model/article``): :func:`utc_calendar` is
``s03a_series.calendar`` and :func:`holiday_day_features`
``s03a_series.holiday_features``; :func:`local_solar_calendar` and
:func:`climate_normals` are ``model.prepare``; :func:`std_offset` is
``s03a_series.std_offset``. Rows are ESFEX's 8760-h year in UTC, 29 February
skipped (:func:`utc_row_hours`); the article's year is the first 8760 hours
of the UTC calendar year, so 31 December of a leap year, which it never saw,
takes the same rules (December, clipped holiday index).
"""

from __future__ import annotations

import datetime as _dt
import logging
from functools import lru_cache
from pathlib import Path
from typing import Optional

import numpy as np

from esfex.utils.temporal import year_row_dates

logger = logging.getLogger(__name__)

HDD_BASE, CDD_BASE = 18.0, 24.0
# s03a_series.MONTH_DAYS: month of an hour of the year counted on a 365-day calendar
MONTH_DAYS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
_MONTH_OF_HOUR = np.repeat(np.arange(12), np.array(MONTH_DAYS) * 24)
# s03a_series.HOLIDAY_ISO2: countries the holidays package knows by ISO2 only
HOLIDAY_ISO2 = {"BGR": "BG", "GTM": "GT", "MAR": "MA", "XKX": "XK"}
HOLIDAY_CLIP = 7

CAL_UTC = ["hour_sin", "hour_cos", "dow_sin", "dow_cos", "month_sin", "month_cos",
           "is_weekend", "is_holiday", "days_to_next_holiday", "days_from_prev_holiday"]
CAL_LOCAL = ["lhour_sin", "lhour_cos", "smonth_sin", "smonth_cos"]


# ── Calendar ───────────────────────────────────────────────────────────


def utc_row_hours(year: int, n_hours: int = 8760) -> np.ndarray:
    """Hours since 1 January 00:00 UTC of *year* of each row of an ESFEX year.

    Rows are UTC hours with 29 February skipped (:func:`year_row_dates`), so in
    a leap year the index jumps from 1415 to 1440 and ends at 8783.
    """
    import pandas as pd

    stamps = year_row_dates(year, n_hours)
    jan1 = pd.Timestamp(year=year, month=1, day=1)
    return np.asarray((stamps - jan1) // pd.Timedelta(hours=1), dtype=np.int64)


@lru_cache(maxsize=512)
def holiday_day_features(iso3: str, year: int, clip: int = HOLIDAY_CLIP):
    """``is_holiday``, days to the next and from the previous holiday, per day
    of a 365-day year (``s03a_series.holiday_features``).

    Holidays come from the ``holidays`` package, by ISO3 first and then by the
    ISO2 of :data:`HOLIDAY_ISO2`; a country it does not know has none.
    """
    jan1 = _dt.date(year, 1, 1)
    off = np.array([], int)
    try:
        import holidays
    except ImportError:              # pragma: no cover - environment dependent
        holidays = None
        logger.warning("The holidays package is not installed: no holidays in "
                       "the demand density features.")
    for code in (iso3, HOLIDAY_ISO2.get(iso3)):
        if code is None or holidays is None:
            continue
        try:
            hol = holidays.country_holidays(code, years=[year - 1, year, year + 1])
            off = np.array(sorted({(d - jan1).days for d in hol.keys()}))
            break
        except (NotImplementedError, KeyError, AttributeError, TypeError):
            continue
    is_h = np.zeros(365, np.float32)
    if off.size == 0:
        full = np.full(365, clip, np.float32)
        return is_h, full, full.copy()
    is_h[off[(off >= 0) & (off < 365)]] = 1.0
    diff = off[None, :] - np.arange(365)[:, None]
    nxt = np.where(diff >= 0, diff, 10_000).min(axis=1)
    prv = np.where(diff <= 0, -diff, 10_000).min(axis=1)
    return (is_h, np.minimum(nxt, clip).astype(np.float32),
            np.minimum(prv, clip).astype(np.float32))


def utc_calendar(iso3: str, year: int, idx: np.ndarray, offset: float) -> dict:
    """Calendar predictors of the UTC hours *idx* of *year*
    (``s03a_series.calendar``).

    Hour and month are UTC; the weekday and the holidays are those of the
    local date, ``idx + offset`` hours (*offset*: :func:`std_offset`). The
    values are float32, as stored in the training table.
    """
    idx = np.asarray(idx, dtype=np.int64)
    hod = idx % 24
    month = _MONTH_OF_HOUR[np.minimum(idx, len(_MONTH_OF_HOUR) - 1)]
    lday = np.floor((idx + offset) / 24.0).astype(int)        # may be -1 or 365
    dow = (_dt.date(year, 1, 1).weekday() + lday) % 7
    ih, dn, dp = holiday_day_features(iso3 or "", int(year))
    k = np.clip(lday, 0, 364)
    cols = {"hour_sin": np.sin(2 * np.pi * hod / 24), "hour_cos": np.cos(2 * np.pi * hod / 24),
            "dow_sin": np.sin(2 * np.pi * dow / 7), "dow_cos": np.cos(2 * np.pi * dow / 7),
            "month_sin": np.sin(2 * np.pi * month / 12),
            "month_cos": np.cos(2 * np.pi * month / 12),
            "is_weekend": (dow >= 5).astype(float), "is_holiday": ih[k],
            "days_to_next_holiday": dn[k], "days_from_prev_holiday": dp[k]}
    return {c: np.asarray(v, dtype=np.float32) for c, v in cols.items()}


def local_solar_calendar(hour_sin, hour_cos, month_sin, month_cos,
                         latitude, longitude) -> dict:
    """Local solar hour and season-aligned month (``model.prepare``).

    The UTC hour turned by the longitude (15° = one hour) and the month turned
    by half a year where the latitude is negative. Inputs broadcast (float32
    calendar columns against one longitude per row or per place).
    """
    a = np.deg2rad(np.asarray(longitude, dtype=np.float64))
    hs = np.asarray(hour_sin, dtype=np.float32)
    hc = np.asarray(hour_cos, dtype=np.float32)
    sgn = np.where(np.asarray(latitude, dtype=np.float64) < 0, -1.0, 1.0)
    return {"lhour_sin": (hs * np.cos(a) + hc * np.sin(a)).astype(np.float32),
            "lhour_cos": (hc * np.cos(a) - hs * np.sin(a)).astype(np.float32),
            "smonth_sin": (np.asarray(month_sin, dtype=np.float32) * sgn).astype(np.float32),
            "smonth_cos": (np.asarray(month_cos, dtype=np.float32) * sgn).astype(np.float32)}


# ── Climate normals ────────────────────────────────────────────────────


def daily_means(temperature: np.ndarray, idx: np.ndarray) -> np.ndarray:
    """Mean temperature of every UTC day present in hourly *temperature*.

    *idx* is each value's hour since 1 January 00:00 UTC (:func:`utc_row_hours`);
    the day of an hour is ``idx // 24``, as ``model.prepare`` groups it.
    Non-finite values are left out; a day without any is dropped.
    """
    t = np.asarray(temperature, dtype=np.float64)
    day = np.asarray(idx, dtype=np.int64) // 24
    ok = np.isfinite(t)
    if not ok.any():
        return np.array([], dtype=np.float64)
    days, inv = np.unique(day[ok], return_inverse=True)
    s = np.bincount(inv, weights=t[ok], minlength=len(days))
    n = np.bincount(inv, minlength=len(days))
    return s / n


def climate_normals(daily: np.ndarray) -> tuple[float, float]:
    """``t_mean`` and ``t_range`` from daily mean temperatures: their mean and
    the spread between their 95th and 5th percentiles (``model.prepare``)."""
    d = np.asarray(daily, dtype=np.float64)
    d = d[np.isfinite(d)]
    if d.size == 0:
        return float("nan"), float("nan")
    return (float(np.float32(d.mean())),
            float(np.float32(np.quantile(d, 0.95) - np.quantile(d, 0.05))))


# ── Shipped cell inputs and country table ──────────────────────────


class DensityContext:
    """The cell inputs of the model, as the article's gridded maps use them.

    ``<stem>.grids.npz`` holds them on the article's 0.25° grid (cell centres at
    -59.875 + 0.25 i, -179.875 + 0.25 j): the present-day climate normals of
    every land cell (``t_mean``, ``t_range``, from NEX-GDDP-CMIP6 daily
    temperature 2025-2029), the electricity-use context (``hh_elec_share``,
    ``bld_share_elec``: national values, by state in the USA and by province
    in Canada), the land area of each cell (``land_km2``) and its population
    in 2020 (``pop_2020``, WorldPop). ``<stem>.countries.csv`` gives, per ISO3
    country, the national context and the standard UTC offsets of its time
    zones.
    """

    NEIGHBOUR_CELLS = 4          # a cell without a value takes one within 1°

    def __init__(self, grids: dict, countries: dict) -> None:
        self._g = grids
        self._countries = countries

    @classmethod
    def load(cls, directory: Path, stem: str) -> "DensityContext":
        return _load_context(str(directory), stem)

    # grid lookup ------------------------------------------------------
    def _rc(self, lats, lons):
        g = self._g
        r = np.rint((np.asarray(lats, float) - g["lat0"]) / g["res"]).astype(np.int64)
        c = np.rint((np.asarray(lons, float) - g["lon0"]) / g["res"]).astype(np.int64)
        return r, c

    def _at(self, name: str, lats, lons, neighbours: bool = True) -> np.ndarray:
        arr = self._g[name]
        nr, nc = arr.shape
        r, c = self._rc(lats, lons)
        out = np.full(r.shape, np.nan, dtype=np.float32)
        inside = (r >= 0) & (r < nr)
        cw = np.mod(c, nc)
        out[inside] = arr[r[inside], cw[inside]]
        if not neighbours:
            return out
        k = self.NEIGHBOUR_CELLS
        for i in np.where(~np.isfinite(out) & inside)[0]:
            r0, r1 = max(r[i] - k, 0), min(r[i] + k + 1, nr)
            cols = np.mod(np.arange(c[i] - k, c[i] + k + 1), nc)
            win = arr[r0:r1][:, cols]
            ok = np.isfinite(win)
            if ok.any():
                rr, cc = np.nonzero(ok)
                d = (rr + r0 - r[i]) ** 2 + (cc - k) ** 2
                j = int(np.argmin(d))
                out[i] = win[rr[j], cc[j]]
        return out

    def population_and_land(self, lats, lons) -> tuple[np.ndarray, np.ndarray]:
        """Population in 2020 and land area (km²) of the cells at (*lats*,
        *lons*): the denominators of the density predictors and the area the
        predicted density covers (``s08_sensitivity``). 0 outside the grid."""
        pop = np.nan_to_num(self._at("pop_2020", lats, lons, neighbours=False))
        land = np.nan_to_num(self._at("land_km2", lats, lons, neighbours=False))
        return pop.astype(np.float64), land.astype(np.float64)

    def normals(self, lats, lons) -> tuple[np.ndarray, np.ndarray]:
        """``t_mean``, ``t_range`` of the cells at (*lats*, *lons*); NaN where
        no land cell lies within 1°."""
        return self._at("t_mean", lats, lons), self._at("t_range", lats, lons)

    def iso3(self, code: str) -> str:
        """ISO3 from an ISO3 or ISO2 country code."""
        code = (code or "").strip().upper()
        if len(code) == 2:
            return next((k for k, v in self._countries.items() if v["iso2"] == code), code)
        return code

    def country(self, code: str) -> Optional[dict]:
        return self._countries.get(self.iso3(code))

    def energy_context(self, lats, lons, iso3: str = "") -> tuple[np.ndarray, np.ndarray]:
        """``hh_elec_share``, ``bld_share_elec`` of the cells.

        The cell's value (state or province in the USA and Canada), else a
        cell within 1°, else the national value of *iso3*; NaN where none is
        known (as for the series of the training table without energy
        statistics: the model then takes its missing-value branches).
        """
        hh = self._at("hh_elec_share", lats, lons)
        bld = self._at("bld_share_elec", lats, lons)
        row = self.country(iso3)
        if row is not None:
            hh = np.where(np.isfinite(hh), hh, row["hh_elec_share"])
            bld = np.where(np.isfinite(bld), bld, row["bld_share_elec"])
        return hh.astype(np.float64), bld.astype(np.float64)

    def std_offset(self, iso3: str, lon: float) -> float:
        """``s03a_series.std_offset``: the standard UTC offset (hours) of the
        country's time zone nearest to *lon* (15° per hour), or the longitude
        band's when the country is unknown."""
        row = self.country(iso3)
        best = None
        for std in (row or {}).get("std_offsets", ()):
            if best is None or abs(std * 15 - lon) < best[0]:
                best = (abs(std * 15 - lon), std)
        return best[1] if best else float(round(lon / 15))


def _countries_table(path: Path) -> dict:
    import csv

    out = {}
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            def num(v):
                return float(v) if v not in ("", None) else float("nan")
            out[r["iso3"]] = {
                "iso2": r["iso2"],
                "hh_elec_share": num(r["hh_elec_share"]),
                "bld_share_elec": num(r["bld_share_elec"]),
                "std_offsets": tuple(float(x) for x in r["std_offsets"].split(";") if x),
            }
    return out


@lru_cache(maxsize=4)
def _load_context(directory: str, stem: str) -> DensityContext:
    d = Path(directory)
    z = np.load(d / f"{stem}.grids.npz")
    grids = {k: z[k] for k in ("t_mean", "t_range", "hh_elec_share", "bld_share_elec",
                               "land_km2", "pop_2020")}
    grids.update(lat0=float(z["lat0"]), lon0=float(z["lon0"]), res=float(z["res"]))
    return DensityContext(grids, _countries_table(d / f"{stem}.countries.csv"))
