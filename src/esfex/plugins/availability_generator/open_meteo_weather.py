"""Quota-aware Open-Meteo weather download for Grid Builder availability.

Open-Meteo's free tier allows 600 API calls per minute, 5 000 per hour and
10 000 per day, and a request longer than two weeks counts as several calls
(https://open-meteo.com/en/pricing): one location-year of hourly data costs
≈ 26 calls. Firing requests as fast as a thread pool allows therefore hits the
per-minute limit after ~23 locations, and a short retry backoff never outlasts
the one-minute window — every location after that came back empty.

This module paces requests to a per-minute call budget, waits out the window
when Open-Meteo still answers HTTP 429, stops cleanly (instead of hammering)
when the hourly / daily quota is exhausted, and caches every downloaded cell
on disk so a re-run only fetches what is missing.

One request per grid cell fetches the variables for both solar and wind (four
variables cost the same as one: the multiplier only starts above ten), and the
capacity factors are then computed locally with the same models as
``solarex`` / ``windrex``.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
VARIABLES = ("shortwave_radiation", "temperature_2m",
             "wind_speed_10m", "wind_speed_100m")

# Free tier: 600 calls/min. Keep headroom for other Open-Meteo users in the
# same session (e.g. the demand step's ERA5 temperature fetch).
CALLS_PER_MINUTE_BUDGET = 500.0

_MAX_ATTEMPTS = 6
# A full year of hourly data can take well over a minute to be served.
_TIMEOUT_S = 120
_MINUTE_S = 60.0


class OpenMeteoQuotaExhausted(RuntimeError):
    """The hourly or daily Open-Meteo quota is used up; retrying now is futile."""


def request_cost(n_days: int, n_variables: int) -> float:
    """API calls one single-location request is billed as (fractional):
    two weeks and ≤ 10 variables count as one call, scaling linearly."""
    return max(1.0, n_days / 14.0) * max(1.0, n_variables / 10.0)


def year_request_cost(year: int) -> float:
    days = 366 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 365
    return request_cost(days, len(VARIABLES))


def default_cache_dir() -> Path:
    override = os.environ.get("ESFEX_WEATHER_CACHE")
    if override:
        return Path(override)
    return Path.home() / ".cache" / "esfex" / "weather" / "open_meteo"


class MinuteRateLimiter:
    """Thread-safe sliding-window limiter on billed API calls per minute.

    ``acquire(cost)`` blocks until the call fits in the last-60-s budget;
    ``pause(seconds)`` holds every caller back (used after a 429)."""

    def __init__(self, budget: float = CALLS_PER_MINUTE_BUDGET,
                 clock: Optional[Callable[[], float]] = None,
                 sleep: Optional[Callable[[float], None]] = None):
        self.budget = budget
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._lock = threading.Lock()
        self._events: deque[tuple[float, float]] = deque()
        self._paused_until = 0.0

    def acquire(self, cost: float) -> None:
        cost = min(cost, self.budget)
        while True:
            with self._lock:
                now = self._clock()
                while self._events and now - self._events[0][0] >= _MINUTE_S:
                    self._events.popleft()
                used = sum(c for _, c in self._events)
                if now >= self._paused_until and used + cost <= self.budget:
                    self._events.append((now, cost))
                    return
                wait = max(self._paused_until - now, 0.0)
                if used + cost > self.budget and self._events:
                    wait = max(wait, _MINUTE_S - (now - self._events[0][0]))
            self._sleep(max(wait, 0.05))

    def pause(self, seconds: float) -> None:
        with self._lock:
            self._paused_until = max(self._paused_until,
                                     self._clock() + seconds)


def _cache_path(cache_dir: Path, lat: float, lon: float, year: int) -> Path:
    return cache_dir / f"{year}_{lat:+08.3f}_{lon:+09.3f}.npz"


def fetch_cell_weather(
    lat: float,
    lon: float,
    year: int,
    limiter: MinuteRateLimiter,
    cache_dir: Optional[Path] = None,
    stop: Optional[threading.Event] = None,
    sleep: Optional[Callable[[float], None]] = None,
) -> Optional[dict[str, np.ndarray]]:
    """Hourly ``VARIABLES`` for one location-year, from the disk cache or
    Open-Meteo. Returns None if ``stop`` is set before a download succeeds.

    Raises :class:`OpenMeteoQuotaExhausted` on an hourly / daily limit, and
    the last error when every attempt fails.
    """
    import requests

    sleep = sleep or time.sleep
    cache_dir = cache_dir or default_cache_dir()
    path = _cache_path(cache_dir, lat, lon, year)
    if path.exists():
        try:
            with np.load(path) as z:
                return {k: z[k] for k in VARIABLES}
        except Exception:
            logger.debug("Unreadable weather cache %s — re-downloading", path)

    params = {
        "latitude": round(lat, 4),
        "longitude": round(lon, 4),
        "start_date": f"{year}-01-01",
        "end_date": f"{year}-12-31",
        "hourly": ",".join(VARIABLES),
        "wind_speed_unit": "ms",
        "timezone": "UTC",
    }
    cost = year_request_cost(year)
    last_exc: Optional[Exception] = None
    for attempt in range(_MAX_ATTEMPTS):
        if stop is not None and stop.is_set():
            return None
        limiter.acquire(cost)
        if stop is not None and stop.is_set():
            return None
        try:
            resp = requests.get(ARCHIVE_URL, params=params, timeout=_TIMEOUT_S)
        except Exception as exc:  # timeout / connection reset
            last_exc = exc
            sleep(2.0 * (2 ** attempt) * (0.5 + random.random()))
            continue

        if resp.status_code == 429:
            reason = _error_reason(resp)
            low = reason.lower()
            if "hour" in low or "daily" in low or "day" in low:
                raise OpenMeteoQuotaExhausted(reason or "Open-Meteo quota exhausted")
            # Per-minute limit: everyone waits for the window to roll over.
            wait = _retry_after(resp) or _MINUTE_S
            logger.info("Open-Meteo minute limit hit — pausing %.0f s", wait)
            limiter.pause(wait + random.uniform(0.0, 3.0))
            last_exc = RuntimeError(reason or "HTTP 429")
            continue
        if resp.status_code >= 500:
            last_exc = RuntimeError(f"HTTP {resp.status_code}")
            sleep(2.0 * (2 ** attempt) * (0.5 + random.random()))
            continue
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {_error_reason(resp)}")

        # A 200 can still carry a truncated / non-JSON body under load:
        # treat it as transient, like a 5xx.
        try:
            hourly = (resp.json() or {}).get("hourly", {})
        except ValueError as exc:
            last_exc = RuntimeError(f"unreadable Open-Meteo response: {exc}")
            sleep(2.0 * (2 ** attempt) * (0.5 + random.random()))
            continue
        data = {k: np.asarray([np.nan if v is None else v
                               for v in hourly.get(k, [])], dtype=np.float32)
                for k in VARIABLES}
        if data["shortwave_radiation"].size == 0 or data["wind_speed_100m"].size == 0:
            raise RuntimeError("Open-Meteo returned no hourly data")
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp.npz")
            np.savez_compressed(tmp, **data)
            os.replace(tmp, path)
        except Exception as exc:
            logger.debug("Could not cache weather at %s: %s", path, exc)
        return data

    assert last_exc is not None
    raise last_exc


def _error_reason(resp) -> str:
    try:
        return str((resp.json() or {}).get("reason", ""))
    except Exception:
        return ""


def _retry_after(resp) -> Optional[float]:
    try:
        v = float(resp.headers.get("Retry-After", ""))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


# ── Capacity factors (same models as solarex / windrex) ─────────────


def solar_cf(weather: dict[str, np.ndarray]) -> np.ndarray:
    """Hourly PV capacity factor, as ``solarex.compute_solar_hourly_cf``
    computes it for Open-Meteo data with its default module parameters."""
    from solarex.core.capacity_factor import (
        _irradiance_to_hourly_cf,
        normalize_to_8760,
    )
    ghi = np.nan_to_num(weather["shortwave_radiation"].astype(float))
    temp = weather["temperature_2m"].astype(float)
    if temp.size < ghi.size:
        temp = np.full_like(ghi, 25.0)
    temp = np.nan_to_num(temp[: ghi.size], nan=25.0)
    cf = _irradiance_to_hourly_cf(ghi, temp, 0.20, -0.40, 45.0)
    return normalize_to_8760(cf)


def wind_cf(weather: dict[str, np.ndarray], hub_height: int = 80) -> np.ndarray:
    """Hourly wind capacity factor, as ``windrex.compute_wind_hourly_cf``
    computes it for Open-Meteo data with its default turbine and hub height
    (power-law shear between 10 m and 100 m)."""
    from windrex.core.capacity_factor import (
        _resolve_power_curve,
        _wind_speed_to_hourly_cf,
        normalize_to_8760,
    )
    ws100 = np.nan_to_num(weather["wind_speed_100m"].astype(float))
    ws10 = np.nan_to_num(weather["wind_speed_10m"].astype(float))
    if hub_height == 100 or ws10.size == 0:
        ws_hub = ws100
    else:
        ws10_safe = np.maximum(ws10, 0.01)
        alpha = np.log(np.maximum(ws100, 0.01) / ws10_safe) / np.log(100.0 / 10.0)
        alpha = np.clip(alpha, 0.05, 0.50)
        ws_hub = ws100 * (hub_height / 100.0) ** alpha
    pc_ws, pc_mw, rated = _resolve_power_curve(None, None, 3.0, None)
    return normalize_to_8760(_wind_speed_to_hourly_cf(ws_hub, pc_ws, pc_mw, rated))
