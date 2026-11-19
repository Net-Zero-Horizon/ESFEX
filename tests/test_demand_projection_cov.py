"""Coverage tests for esfex.models.demand_projection: the SSP growth-rate
helpers and the daily → hourly temperature interpolation.
"""

from __future__ import annotations

import numpy as np
import pytest

import esfex.models.demand_projection as dp


# ── SSP / interpolation helpers ─────────────────────────────────────────


def test_interpolate_5yr_to_annual_linear():
    r = dp._interpolate_5yr_to_annual({2020: 1.0, 2025: 2.0})
    assert r[2020] == 1.0
    assert r[2025] == 2.0
    # midpoint year 2022 is two fifths of the way: 1.0 + 0.4*(1.0)
    assert r[2022] == pytest.approx(1.4)
    # endpoints inclusive, contiguous range
    assert set(r.keys()) == set(range(2020, 2026))


def test_levels_to_growth_rates():
    rates = dp._levels_to_growth_rates({1: 1.0, 2: 1.1, 3: 1.21})
    assert rates[2] == pytest.approx(0.1)
    assert rates[3] == pytest.approx(0.1)
    # first year has no predecessor → not present
    assert 1 not in rates


def test_ssp_growth_rates_gdp_and_pop():
    gdp = dp._ssp_growth_rates("SSP2", "gdp")
    pop = dp._ssp_growth_rates("SSP2", "pop")
    # base year (2025) has no predecessor in the multiplier table → starts 2026
    assert min(gdp) == 2026
    assert max(gdp) == 2050
    assert all(isinstance(v, float) for v in gdp.values())
    # pop rates use the population multiplier table (different from gdp)
    assert pop[2026] != gdp[2026]


def test_ssp_growth_rates_pop_kind_uses_pop_table():
    # Any kind that is not "gdp" selects the population multipliers.
    pop = dp._ssp_growth_rates("SSP1", "population")
    pop2 = dp._ssp_growth_rates("SSP1", "pop")
    assert pop == pop2


def test_ssp_growth_rates_unknown_raises():
    with pytest.raises(ValueError) as exc:
        dp._ssp_growth_rates("NOPE")
    assert "Unknown SSP" in str(exc.value)


# ── daily→hourly temperature ────────────────────────────────────────────


def test_daily_to_hourly_temperature_shape_and_peaks():
    tmin = np.array([10.0, 12.0])
    tmax = np.array([20.0, 22.0])
    h = dp._daily_to_hourly_temperature(tmin, tmax, 2)
    assert h.shape == (48,)
    # cos(0) at h=14 → equals tmax for day 0
    assert h[14] == pytest.approx(20.0)
    # day 1 peak at hour 24+14=38
    assert h[38] == pytest.approx(22.0)
    # all values within [tmin, tmax] for each day
    assert h[:24].min() >= 10.0 - 1e-9
    assert h[:24].max() <= 20.0 + 1e-9
