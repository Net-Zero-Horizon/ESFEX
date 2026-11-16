"""Clock of each training series of the density demand model, and the
conversion of its demand and temperature to local civil time.

The v1 hourly frame (reflexpy ``build_unified_dataset``) used row ``h`` of
every archive parquet as hour ``h`` of the year: demand on whatever clock
its source used, ERA5 temperature in UTC (``era5_{year}.npy``, the first 8760
UTC hours) and calendar features read from the row index. ESFEX's convention
(rounds 31-32) is different: demand and temperature of the same instant, on
the local civil time of the series' location (daylight saving time included),
calendar features of each row's real date with 29 February skipped.

``CORE_CLOCKS`` records the clock most years of a series were recorded on;
``clock_audit.py`` checks every series-year against it and writes
``series_clocks.csv`` (the clock actually used per year, or why a year is
left out). A clock is ``"UTC"``, a fixed zone (``Etc/GMT-1`` is UTC+1; the
IANA sign is inverted) or an IANA zone whose civil time (with DST) the rows
follow.
"""

from __future__ import annotations

import datetime
from typing import Mapping, Optional

import numpy as np
import pandas as pd

# Recorded clock of the v1 archive rows, by series ("ISO3", "ISO3_zone" or
# "ISO3_*" for every zone of a country). Unlisted series are UTC: the
# European national series (equal to OPSD's utc_timestamp load in 2016-2019)
# and the Electricity Maps / DemandCast series of the Americas and Asia.
# Evidence (clock_audit.py): trough of the mean day in local solar time,
# daylight-saving test (a fixed clock moves the profile by an hour at each
# change, civil time does not), OPSD anchor, and the R1 clock audit.
CORE_CLOCKS: dict[str, str] = {
    # operator data on local standard time (R1: measured against the same
    # load in UTC where Electricity Maps has it)
    "AUS_*": "Etc/GMT-10",           # NEM market time, AEST all year
    "CAN": "Etc/GMT+6",              # IESO hours; R1 measured +6 h to UTC
    "CAN_*": "Etc/GMT+6",
    "CHN_*": "Asia/Shanghai",
    "JPN": "Asia/Tokyo",
    "IND": "Asia/Kolkata",           # trough says UTC+5.5 (R1 used +5)
    "PAK": "Asia/Karachi",
    "BGD": "Asia/Dhaka",
    "KHM": "Asia/Phnom_Penh",
    "THA_*": "Asia/Bangkok",
    "SGP": "Asia/Singapore",
    "PHL": "Asia/Manila",
    "TWN": "Asia/Taipei",
    "TWN_*": "Asia/Taipei",
    "PRI": "America/Puerto_Rico",
    "COL": "America/Bogota",
    "GLP": "America/Guadeloupe",
    "GUF": "America/Cayenne",
    "URY": "America/Montevideo",     # 2018-2023; other years UTC (audit)
    "ZAF": "Africa/Johannesburg",    # 2019-2022 trough fits SAST, not UTC
    "TUR": "Etc/GMT-3",              # trough fits UTC+3 (R1: UTC, outlier)
    # local civil time: the profile does not move at the DST changes
    "USA": "America/New_York",       # R1: fixed UTC-5
    "CUB": "America/Havana",         # R1: fixed UTC-5
    "NZL": "Pacific/Auckland",       # R1: fixed UTC+12
    "BRA": "America/Sao_Paulo",      # ONS civil time; R1: fixed UTC-3
    "BRA_*": "America/Sao_Paulo",
    "GBR": "Europe/London",          # ESO settlement periods; 2025 is UTC
    "RUS": "Europe/Moscow",          # trough fits Moscow time (R1: UTC)
    # DemandCast's ENTSO-E series: UKR equals OPSD's load one hour later
    # (CET, UTC+1 all year); the neighbours' troughs agree
    "ALB": "Etc/GMT-1", "BIH": "Etc/GMT-1", "GEO": "Etc/GMT-1",
    "MDA": "Etc/GMT-1", "MKD": "Etc/GMT-1", "UKR": "Etc/GMT-1",
    "XKX": "Etc/GMT-1",
}

# Years whose clock differs from the core one for a reason the profile test
# cannot see (a civil series whose last year comes from another source).
MANUAL_YEAR_CLOCKS: dict[tuple[str, str, int], str] = {
    ("GBR", "", 2025): "UTC",        # Electricity Maps year: DST test 0.9
    # SENI years whose parquet temperature peaks ~5 h early if read as UTC:
    # local time (UTC-4); the mean day moves 3-4 h earlier against 2018-2020
    **{("DOM", "", y): "America/Santo_Domingo" for y in range(2021, 2026)},
}

# Clock of the ``temperature_c`` column of archive parquets that v1 read
# when no ERA5 cache existed; UTC (Open-Meteo, timezone=UTC) otherwise.
PARQUET_TEMPERATURE_CLOCKS: dict[tuple[str, str, int], str] = {
    **{("DOM", "", y): "America/Santo_Domingo" for y in range(2021, 2025)},
}

# The time zone of a series whose population-weighted centroid falls at sea.
LOCAL_ZONE_OVERRIDES: dict[str, str] = {"MYS": "Asia/Kuala_Lumpur"}


def series_key(country: str, zone: str = "") -> str:
    return f"{country}_{zone}" if zone else country


def core_clock(country: str, zone: str = "") -> str:
    """Recorded clock of most years of a series (see ``CORE_CLOCKS``)."""
    key = series_key(country, zone)
    if key in CORE_CLOCKS:
        return CORE_CLOCKS[key]
    if zone and f"{country}_*" in CORE_CLOCKS:
        return CORE_CLOCKS[f"{country}_*"]
    return "UTC"


def fixed_clock(offset_hours: float) -> str:
    """The fixed zone ``offset_hours`` east of UTC (``Etc/GMT-1`` = UTC+1)."""
    if offset_hours == 0:
        return "UTC"
    if offset_hours != int(offset_hours):
        raise ValueError(f"no Etc zone for a fractional offset {offset_hours}")
    return f"Etc/GMT{-int(offset_hours):+d}"


def standard_offset(clock: str, year: int) -> float:
    """Offset of *clock* from UTC (hours) on 15 January of *year*."""
    if clock == "UTC":
        return 0.0
    t = pd.Timestamp(year=year, month=1, day=15, hour=12).tz_localize(clock)
    return t.utcoffset().total_seconds() / 3600.0


def series_local_zone(country: str, lat: float, lon: float) -> str:
    """Time zone whose civil time the series is put on (its location's)."""
    if country in LOCAL_ZONE_OVERRIDES:
        return LOCAL_ZONE_OVERRIDES[country]
    from esfex.models.demand_real_data import series_time_zone
    return series_time_zone(lat, lon)


_EPOCH = pd.Timestamp("1970-01-01", tz="UTC")


def _hours_since_epoch(stamps: pd.DatetimeIndex) -> np.ndarray:
    return np.asarray((stamps.tz_convert("UTC") - _EPOCH) / pd.Timedelta(hours=1),
                      dtype=float)


def _localize(wall: pd.DatetimeIndex, zone: str) -> pd.DatetimeIndex:
    """Wall times in *zone*: a skipped wall hour is the instant after it, a
    repeated one its first occurrence (as ESFEX's model_clock helpers)."""
    return wall.tz_localize(zone, ambiguous=np.ones(len(wall), dtype=bool),
                            nonexistent="shift_forward")


def row_instants(year: int, n_rows: int, clock: str,
                 feb29_kept: bool = True) -> np.ndarray:
    """UTC instant (hours since 1970) of each archive row of *year*.

    Row ``h`` is wall time ``h`` hours after 1 Jan 00:00 on *clock*. With
    *feb29_kept* the rows run through 29 February of a leap year (and stop
    before 31 December: the archive keeps the first 8760 hours); otherwise
    29 February is skipped.
    """
    if feb29_kept or not pd.Timestamp(year=year, month=1, day=1).is_leap_year:
        wall = pd.date_range(f"{year}-01-01", periods=n_rows, freq="h")
    else:
        from esfex.utils.temporal import model_time_index
        wall = pd.DatetimeIndex(model_time_index(f"{year}-01-01", n_rows))
    return _hours_since_epoch(_localize(wall, clock))


def local_row_instants(year: int, local_zone: str) -> np.ndarray:
    """UTC instant of each of the 8760 local civil hours of *year* (29 Feb
    dropped), the rows of the rebuilt dataset (ESFEX demand_dataset)."""
    from esfex.models.demand_dataset import local_hour_stamps
    return _hours_since_epoch(local_hour_stamps(year, local_zone))


def sample_at(instants: np.ndarray, values: np.ndarray,
              targets: np.ndarray) -> np.ndarray:
    """*values* (at *instants*, hours) at *targets*.

    A target equal to a recorded instant reads its value; one between two
    recorded instants at most an hour apart (a half-hour zone against an
    hourly clock) is interpolated linearly between them. NaN where a needed
    value is missing. Repeated instants keep their first value.
    """
    inst = np.asarray(instants, dtype=float)
    vals = np.asarray(values, dtype=float)
    t = np.asarray(targets, dtype=float)
    ok = np.isfinite(inst)
    inst, vals = inst[ok], vals[ok]
    out = np.full(len(t), np.nan)
    if inst.size == 0:
        return out
    key = np.round(inst * 60).astype(np.int64)          # minutes
    order = np.argsort(key, kind="stable")
    key_u, first = np.unique(key[order], return_index=True)
    vals_u = vals[order][first]
    tk = np.round(t * 60).astype(np.int64)
    pos = np.searchsorted(key_u, tk)
    in_range = pos < len(key_u)
    exact = in_range & (key_u[np.minimum(pos, len(key_u) - 1)] == tk)
    out[exact] = vals_u[pos[exact]]
    mid = ~exact & (pos > 0) & in_range
    lo, hi = pos[mid] - 1, pos[mid]
    span = key_u[hi] - key_u[lo]
    w = (tk[mid] - key_u[lo]) / np.maximum(span, 1)
    v = vals_u[lo] * (1 - w) + vals_u[hi] * w
    out[np.flatnonzero(mid)] = np.where(span <= 60, v, np.nan)
    return out


def on_local_time(
    years: Mapping[int, np.ndarray],
    clocks: Mapping[int, str],
    feb29_kept: Mapping[int, bool],
    local_zone: str,
    targets: Optional[list[int]] = None,
) -> dict[int, np.ndarray]:
    """Archive years of one series (row ``h`` on ``clocks[year]``) as 8760
    local civil hours of *local_zone* per target year.

    Every available year is placed at its instants first, so a local year
    whose first or last hours lie in the neighbouring recorded year reads
    them there (NaN when that year is not available): nothing wraps.
    """
    inst, vals = [], []
    for y, arr in years.items():
        a = np.asarray(arr, dtype=float)
        inst.append(row_instants(y, len(a), clocks[y], feb29_kept.get(y, True)))
        vals.append(a)
    inst_all = np.concatenate(inst)
    vals_all = np.concatenate(vals)
    out = {}
    for y in (targets if targets is not None else sorted(years)):
        out[y] = sample_at(inst_all, vals_all, local_row_instants(y, local_zone))
    return out


def calendar_features(year: int, iso3: str) -> dict[str, np.ndarray]:
    """The v1 calendar features of the 8760 local rows of *year*, read from
    each row's real date (``temporal.year_row_dates``: 29 Feb skipped)."""
    from esfex.models.demand_training import _holiday_features
    from esfex.utils.temporal import year_row_dates

    stamps = year_row_dates(year, 8760)
    hour = np.asarray(stamps.hour, dtype=float)
    dow = np.asarray(stamps.dayofweek, dtype=float)
    month = np.asarray(stamps.month, dtype=float) - 1.0
    is_h, d_next, d_prev = _holiday_features(iso3, year, hours=8760)
    two_pi = 2.0 * np.pi
    return {
        "hour_sin": np.sin(two_pi * hour / 24), "hour_cos": np.cos(two_pi * hour / 24),
        "dow_sin": np.sin(two_pi * dow / 7), "dow_cos": np.cos(two_pi * dow / 7),
        "month_sin": np.sin(two_pi * month / 12), "month_cos": np.cos(two_pi * month / 12),
        "is_weekend": (dow >= 5).astype(float),
        "is_holiday": np.asarray(is_h, dtype=np.float32),
        "days_to_next_holiday": np.asarray(d_next, dtype=np.float32),
        "days_from_prev_holiday": np.asarray(d_prev, dtype=np.float32),
    }


def v1_calendar_features(year: int, iso3: str) -> dict[str, np.ndarray]:
    """The calendar v1 gave row ``h`` of an archive year (reflexpy
    ``build_unified_dataset``): hour ``h % 24``, weekday counted from 1 Jan,
    month from a 365-day table, holidays by day offset from 1 Jan."""
    h = np.arange(8760)
    day = h // 24
    jan1 = datetime.date(year, 1, 1)
    dow = (jan1.weekday() + day) % 7
    month = np.repeat(np.arange(12), np.array(
        [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]) * 24)
    two_pi = 2.0 * np.pi
    return {"hour": (h % 24).astype(float), "dow": dow.astype(float),
            "month": month.astype(float),
            "hour_sin": np.sin(two_pi * (h % 24) / 24),
            "dow_sin": np.sin(two_pi * dow / 7)}
