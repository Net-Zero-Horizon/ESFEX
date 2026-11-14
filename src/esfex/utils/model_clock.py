"""The model clock: one time base for every hourly series of a case.

ESFEX indexes all hourly series (demand, availability, ...) by an hour index
``t`` that must mean the same instant at every node, also across systems joined
by inter-system links. That time base is the *model clock*: a fixed offset from
UTC with no daylight saving time, so every model day has 24 hours. Hour 0 of a
year is 1 January 00:00 on the clock.

``temporal.model_clock`` sets it:

- ``auto`` (default): the standard offset (no DST) of the time zone that holds
  the largest share of demand. Nodes whose zones share a standard offset count
  together. Without demand, every node counts once.
- an IANA zone (``America/Havana``): its standard offset. A zone with daylight
  saving time is used at its standard offset all year.
- ``UTC`` or a fixed zone such as ``Etc/GMT+5`` (UTC-5; the ``Etc`` sign is
  inverted by the IANA convention).

Series reach the clock from one of two source time bases:

- UTC (weather data): shifted by the clock's fixed offset.
- local civil time of a node (demand), DST-aware: each node's zone comes from
  its coordinates. When clocks spring forward, the skipped local hour is never
  read; when they fall back, the repeated local hour is read twice.

The year wraps: clock hours that fall before 1 January or after 31 December of
the source series read the other end of the same year. A shift by a fraction
of an hour (zones such as UTC+05:30) interpolates linearly between the two
neighbouring hours. Series of 8760 hours follow ESFEX's calendar without
29 February; series of 8784 hours keep it.
"""

from __future__ import annotations

import datetime as _dt
import logging
import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, Optional, Sequence, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np

logger = logging.getLogger(__name__)

CLOCK_AUTO = "auto"

_HOURS_STD = 8760
_HOURS_LEAP = 8784
_FEB29_START = 59 * 24


@dataclass(frozen=True)
class ModelClock:
    """A resolved model clock: a zone name and its fixed offset from UTC."""

    zone: str
    offset_hours: float

    @property
    def label(self) -> str:
        """``America/Havana standard time (UTC-05:00)``."""
        return f"{self.zone} standard time ({format_offset(self.offset_hours)})"

    @classmethod
    def from_zone(cls, zone: str) -> "ModelClock":
        zone = normalize_clock_setting(zone)
        if zone == CLOCK_AUTO:
            raise ValueError("'auto' must be resolved against the case's nodes first")
        return cls(zone, standard_offset_hours(zone))


def format_offset(hours: float) -> str:
    """``-5`` → ``UTC-05:00``; ``5.5`` → ``UTC+05:30``."""
    sign = "-" if hours < 0 else "+"
    minutes = int(round(abs(hours) * 60))
    return f"UTC{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def normalize_clock_setting(value: Optional[str]) -> str:
    """Validate a ``temporal.model_clock`` value; return it normalised.

    ``None`` / empty / ``auto`` → ``auto``; ``utc`` → ``UTC``; any other value
    must be a time zone known to the IANA database.
    """
    text = str(value or "").strip()
    if not text or text.lower() == CLOCK_AUTO:
        return CLOCK_AUTO
    if text.upper() == "UTC":
        return "UTC"
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(
            f"model_clock {text!r} is not 'auto', 'UTC' or a known IANA time "
            "zone (e.g. 'America/Havana' or 'Etc/GMT+5')") from None
    return text


@lru_cache(maxsize=None)
def standard_offset_hours(zone: str) -> float:
    """Fixed standard offset of *zone* from UTC (hours, east positive).

    The smaller of the zone's offsets on 1 January and 1 July of the current
    year: the offset outside daylight saving time.
    """
    tz = ZoneInfo(zone)
    year = _dt.date.today().year
    offsets = [
        _dt.datetime(year, m, 1, 12, tzinfo=_dt.timezone.utc)
        .astimezone(tz).utcoffset().total_seconds() / 3600.0
        for m in (1, 7)
    ]
    return min(offsets)


def nominal_zone(lon: float) -> str:
    """Fixed zone of the 15°-wide longitude band holding *lon*.

    ``Etc/GMT+5`` is UTC-5 (the IANA ``Etc`` sign is inverted).
    """
    hours = int(math.floor((float(lon) + 7.5) / 15.0))
    hours = max(-12, min(12, hours))
    if hours == 0:
        return "UTC"
    return f"Etc/GMT{-hours:+d}"


@lru_cache(maxsize=1)
def _finder():
    from timezonefinder import TimezoneFinder
    return TimezoneFinder()


@lru_cache(maxsize=65536)
def node_time_zone(lat: float, lon: float) -> str:
    """IANA time zone of the land at (*lat*, *lon*).

    A point at sea or outside every zone gets the nominal zone of its
    longitude band (:func:`nominal_zone`, no daylight saving time), with a
    warning.
    """
    zone = None
    try:
        zone = _finder().timezone_at_land(lng=float(lon), lat=float(lat))
    except Exception as exc:  # out-of-range coordinates
        logger.debug("timezonefinder failed at (%s, %s): %s", lat, lon, exc)
    if zone:
        return zone
    zone = nominal_zone(lon)
    logger.warning(
        "No land time zone at (%.4f, %.4f) (at sea?): using %s (%s, no "
        "daylight saving time).", lat, lon, zone,
        format_offset(standard_offset_hours(zone)))
    return zone


def node_time_zones(coords: Iterable[tuple[float, float]]) -> list[str]:
    """Time zone of every ``(lat, lon)`` in *coords*."""
    return [node_time_zone(round(float(la), 4), round(float(lo), 4))
            for la, lo in coords]


def resolve_model_clock(
    setting: Optional[str],
    coords: Sequence[tuple[float, float]] = (),
    weights: Optional[Sequence[float]] = None,
) -> ModelClock:
    """Turn a ``temporal.model_clock`` value into a :class:`ModelClock`.

    A concrete zone is used as given. ``auto`` picks, among the zones of the
    nodes at *coords* grouped by standard offset, the offset with the largest
    total weight (demand per node; every node counts once when *weights* is
    None or sums to zero), named after its largest zone. Ties go to the first
    node. Without coordinates ``auto`` falls back to UTC with a warning.
    """
    setting = normalize_clock_setting(setting)
    if setting != CLOCK_AUTO:
        return ModelClock.from_zone(setting)
    coords = list(coords)
    if not coords:
        logger.warning("model_clock 'auto': no node coordinates; using UTC.")
        return ModelClock("UTC", 0.0)
    zones = node_time_zones(coords)
    w = [1.0] * len(zones)
    if weights is not None:
        given = [max(float(x), 0.0) if x is not None and math.isfinite(float(x))
                 else 0.0 for x in list(weights)[:len(zones)]]
        given += [0.0] * (len(zones) - len(given))
        if sum(given) > 0:
            w = given
    by_offset: dict[float, float] = {}
    by_zone: dict[str, float] = {}
    for z, wi in zip(zones, w):   # dicts keep first-seen order: ties → first node
        off = standard_offset_hours(z)
        by_offset[off] = by_offset.get(off, 0.0) + wi
        by_zone[z] = by_zone.get(z, 0.0) + wi
    best_off = max(by_offset, key=lambda o: by_offset[o])
    best_zone = max((z for z in by_zone if standard_offset_hours(z) == best_off),
                    key=lambda z: by_zone[z])
    return ModelClock(best_zone, best_off)


def resolve_for_nodes(setting: Optional[str], nodes: Iterable,
                      weights: Optional[Sequence[float]] = None) -> ModelClock:
    """:func:`resolve_model_clock` for Studio nodes (``centroid_lat`` /
    ``centroid_lng``). Without *weights*, each node's demand
    (``node.demand.total_mwh``) weighs it when any node has demand."""
    nodes = list(nodes)
    coords = [(float(n.centroid_lat), float(n.centroid_lng)) for n in nodes]
    if weights is None:
        weights = [float(getattr(getattr(n, "demand", None), "total_mwh", 0.0)
                         or 0.0) for n in nodes]
    return resolve_model_clock(setting, coords, weights)


def as_model_clock(clock: Union[ModelClock, str]) -> ModelClock:
    """A :class:`ModelClock` from itself or a concrete zone name."""
    return clock if isinstance(clock, ModelClock) else ModelClock.from_zone(clock)


# ── Conversion ─────────────────────────────────────────────────────────


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


@lru_cache(maxsize=4096)
def _source_positions(source_zone: str, clock_offset: float, year: int,
                      n_hours: int) -> np.ndarray:
    """Where each clock hour of *year* lies in a source series (fractional index).

    Clock hour ``h`` is the instant ``h - clock_offset`` hours after 1 Jan
    00:00 UTC of the clock's calendar; in the source zone its wall time is
    ``h + (zone_offset(instant) - clock_offset)``. A 29 February absent from a
    8760-h series is skipped in the calendar, so the index runs continuously
    across it.
    """
    import pandas as pd

    full = _HOURS_LEAP if _is_leap(year) else _HOURS_STD
    if n_hours not in (full, _HOURS_STD):
        raise ValueError(f"{n_hours} hours is not one year ({year})")
    clock_wall = pd.date_range(f"{year}-01-01", periods=full, freq="h")
    if n_hours < full:            # ESFEX calendar: 29 Feb dropped
        clock_wall = clock_wall.delete(slice(_FEB29_START, _FEB29_START + 24))
    if source_zone == "UTC":
        zone_off = np.zeros(n_hours)
    else:
        utc = clock_wall - pd.Timedelta(hours=clock_offset)
        local = utc.tz_localize("UTC").tz_convert(source_zone).tz_localize(None)
        zone_off = np.asarray((local - utc) / pd.Timedelta(hours=1), dtype=float)
    pos = np.arange(n_hours, dtype=float) + zone_off - clock_offset
    pos = np.mod(pos, n_hours)
    pos.setflags(write=False)
    return pos


def _sample(col: np.ndarray, pos: np.ndarray) -> np.ndarray:
    """``col`` at fractional, wrapped positions (linear between neighbours)."""
    n = len(col)
    lo = np.floor(pos).astype(np.int64) % n
    frac = pos - np.floor(pos)
    if not np.any(frac > 1e-9):
        return col[lo]
    hi = (lo + 1) % n
    return col[lo] * (1.0 - frac) + col[hi] * frac


def _year_blocks(n_rows: int, start_year: int) -> list[tuple[int, int, int]]:
    """``(year, start_row, n_hours)`` blocks of a series starting in *start_year*."""
    if n_rows % _HOURS_STD == 0 and n_rows > 0:
        return [(start_year + k, k * _HOURS_STD, _HOURS_STD)
                for k in range(n_rows // _HOURS_STD)]
    if n_rows == _HOURS_LEAP and _is_leap(start_year):
        return [(start_year, 0, _HOURS_LEAP)]
    raise ValueError(
        f"a series of {n_rows} hours is not a whole number of 8760-h years "
        f"(or one 8784-h leap year {start_year})")


def to_model_clock(
    series: np.ndarray,
    source_zones: Union[str, Sequence[str]],
    clock: Union[ModelClock, str],
    start_year: int,
) -> np.ndarray:
    """Put an hourly series on the model clock.

    Parameters
    ----------
    series
        ``(hours,)`` or ``(hours, nodes)``: one or more consecutive years of
        8760 h starting on 1 Jan 00:00 of *start_year* in the source time base
        (or a single 8784-h leap year).
    source_zones
        Time base of the series: ``"UTC"``, a fixed zone, or an IANA zone whose
        local civil time (DST-aware) the series is in. One per column, or one
        for all.
    clock
        Target clock (:class:`ModelClock` or a concrete zone name).
    start_year
        Calendar year of the first block (it sets the DST dates and leap days).
    """
    clock = as_model_clock(clock)
    arr = np.asarray(series, dtype=float)
    one_d = arr.ndim == 1
    mat = arr.reshape(-1, 1) if one_d else arr
    n_cols = mat.shape[1]
    zones = ([source_zones] * n_cols if isinstance(source_zones, str)
             else list(source_zones))
    if len(zones) != n_cols:
        raise ValueError(f"{len(zones)} source zones for {n_cols} columns")
    out = np.empty_like(mat)
    for year, r0, n in _year_blocks(mat.shape[0], start_year):
        for c in range(n_cols):
            pos = _source_positions(zones[c], clock.offset_hours, year, n)
            out[r0:r0 + n, c] = _sample(mat[r0:r0 + n, c], pos)
    return out[:, 0] if one_d else out


def utc_to_model_clock(series: np.ndarray, clock: Union[ModelClock, str],
                       start_year: int) -> np.ndarray:
    """A UTC series (weather data) on the model clock."""
    return to_model_clock(series, "UTC", clock, start_year)


def weather_source_zone(data_source: str, lon: float) -> str:
    """Time base of a weather backend's hourly series at longitude *lon*.

    Open-Meteo is queried in UTC and ERA5 (atlite) is UTC. NASA POWER's hourly
    API answers in its default local solar time: the fixed offset of the
    15°-wide longitude band (``solarex`` / ``windrex`` do not ask it for UTC).
    """
    return nominal_zone(lon) if data_source == "nasa_power" else "UTC"


def weather_to_model_clock(series: np.ndarray, data_source: str, lon: float,
                           clock: Union[ModelClock, str],
                           start_year: int) -> np.ndarray:
    """A weather-derived series (backend *data_source*, site longitude *lon*)
    on the model clock."""
    return to_model_clock(series, weather_source_zone(data_source, lon), clock,
                          start_year)


def local_to_model_clock(
    series: np.ndarray,
    coords: Sequence[tuple[float, float]],
    clock: Union[ModelClock, str],
    start_year: int,
) -> np.ndarray:
    """A series in each node's local civil time on the model clock.

    *coords* gives the ``(lat, lon)`` of each column (one entry for a 1-D
    series); each node's zone comes from :func:`node_time_zone`.
    """
    return to_model_clock(series, node_time_zones(coords), clock, start_year)


def log_case_clock(setting: Optional[str],
                   coords: Sequence[tuple[float, float]] = ()) -> Optional[ModelClock]:
    """Log the clock a case's series are on (once per run).

    Returns the clock, or None when the case does not record one (``auto``):
    then its time base is unknown and a warning says so. With node
    coordinates, the node time zones are listed when there are several.
    """
    try:
        setting = normalize_clock_setting(setting)
    except ValueError as exc:
        logger.warning("%s", exc)
        return None
    if setting == CLOCK_AUTO:
        logger.warning(
            "The case does not record its model clock (temporal.model_clock: "
            "auto): the time base of its hourly series is unknown, and they are "
            "used as they are. Generate the series with the Grid Builder or the "
            "availability generator, or set temporal.model_clock to the clock "
            "your demand and availability files are on.")
        return None
    clock = ModelClock.from_zone(setting)
    logger.info("Model clock: %s, no daylight saving time; hour 0 = 1 Jan "
                "00:00 on this clock.", clock.label)
    if coords:
        zones = node_time_zones(coords)
        counts: dict[str, int] = {}
        for z in zones:
            counts[z] = counts.get(z, 0) + 1
        if len(counts) > 1:
            logger.info("Node time zones: %s", ", ".join(
                f"{z} ({format_offset(standard_offset_hours(z))}, {k} node"
                f"{'s' if k != 1 else ''})" for z, k in counts.items()))
    return clock
