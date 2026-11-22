"""Demand-projection helpers shared by the demand workflows.

- ``_ssp_growth_rates``: annual GDP / population growth rates of an SSP
  scenario (the Studio's demand step).
- ``_daily_to_hourly_temperature``: hourly temperature from daily Tmin/Tmax
  (the CMIP6 readers and the demand-density model).
"""

from __future__ import annotations

import numpy as np


# ── SSP growth data (bundled) ───────────────────────────────────────────

_SSP_GDP_MULTIPLIERS = {
    "SSP1": {2025: 1.12, 2030: 1.29, 2035: 1.49, 2040: 1.72, 2045: 1.99, 2050: 2.30},
    "SSP2": {2025: 1.10, 2030: 1.24, 2035: 1.40, 2040: 1.58, 2045: 1.79, 2050: 2.03},
    "SSP3": {2025: 1.06, 2030: 1.14, 2035: 1.22, 2040: 1.31, 2045: 1.41, 2050: 1.52},
    "SSP4": {2025: 1.09, 2030: 1.21, 2035: 1.35, 2040: 1.50, 2045: 1.68, 2050: 1.87},
    "SSP5": {2025: 1.14, 2030: 1.36, 2035: 1.62, 2040: 1.93, 2045: 2.30, 2050: 2.74},
}

_SSP_POP_MULTIPLIERS = {
    "SSP1": {2025: 1.04, 2030: 1.07, 2035: 1.09, 2040: 1.11, 2045: 1.12, 2050: 1.12},
    "SSP2": {2025: 1.05, 2030: 1.10, 2035: 1.15, 2040: 1.20, 2045: 1.25, 2050: 1.29},
    "SSP3": {2025: 1.07, 2030: 1.14, 2035: 1.21, 2040: 1.28, 2045: 1.36, 2050: 1.49},
    "SSP4": {2025: 1.05, 2030: 1.10, 2035: 1.16, 2040: 1.21, 2045: 1.26, 2050: 1.32},
    "SSP5": {2025: 1.04, 2030: 1.07, 2035: 1.09, 2040: 1.11, 2045: 1.12, 2050: 1.14},
}


def _interpolate_5yr_to_annual(data_5yr: dict[int, float]) -> dict[int, float]:
    """Linearly interpolate 5-year interval data to annual resolution."""
    years = sorted(data_5yr.keys())
    result = {}
    for i in range(len(years) - 1):
        y0, y1 = years[i], years[i + 1]
        v0, v1 = data_5yr[y0], data_5yr[y1]
        for y in range(y0, y1 + 1):
            t = (y - y0) / (y1 - y0)
            result[y] = v0 + t * (v1 - v0)
    return result


def _levels_to_growth_rates(levels: dict[int, float]) -> dict[int, float]:
    """Convert absolute multiplier levels to year-on-year growth rates."""
    years = sorted(levels.keys())
    rates = {}
    for i in range(1, len(years)):
        rates[years[i]] = (levels[years[i]] / levels[years[i - 1]]) - 1.0
    return rates


def _ssp_growth_rates(
    ssp: str, kind: str = "gdp",
) -> dict[int, float]:
    """Get annual growth rates for an SSP scenario."""
    mults = _SSP_GDP_MULTIPLIERS if kind == "gdp" else _SSP_POP_MULTIPLIERS
    if ssp not in mults:
        raise ValueError(f"Unknown SSP: {ssp}. Choose from {list(mults)}")
    annual = _interpolate_5yr_to_annual(mults[ssp])
    return _levels_to_growth_rates(annual)


# ── Daily → hourly temperature ─────────────────────────────────────────

def _daily_to_hourly_temperature(
    tmin: np.ndarray, tmax: np.ndarray, n_days: int,
) -> np.ndarray:
    """Convert daily Tmin/Tmax to hourly using sinusoidal interpolation.

    Assumes Tmin at 06:00, Tmax at 14:00 (standard diurnal cycle).
    Returns (n_days * 24,) array.
    """
    hourly = np.empty(n_days * 24, dtype=np.float64)
    for d in range(n_days):
        tmn = tmin[d]
        tmx = tmax[d]
        tavg = (tmn + tmx) / 2.0
        amp = (tmx - tmn) / 2.0
        for h in range(24):
            # Phase: min at 6h, max at 14h → period 24h, offset 14h
            angle = 2.0 * np.pi * (h - 14.0) / 24.0
            hourly[d * 24 + h] = tavg + amp * np.cos(angle)
    return hourly
