"""Additive unit tests for esfex.models.wind_models.

Targets every public function plus internal branches: guard clauses,
defaults, MLE iteration paths, financial edge cases, and the Jensen
wake model.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from esfex.models import wind_models as wm


# ---------------------------------------------------------------------------
# fit_weibull
# ---------------------------------------------------------------------------


def test_fit_weibull_too_few_returns_default_with_mean():
    speeds = np.array([5.0, 7.0, 3.0])
    k, A = wm.fit_weibull(speeds)
    assert k == 2.0
    assert A == pytest.approx(np.mean(speeds))


def test_fit_weibull_all_nonpositive_returns_default_A6():
    speeds = np.array([0.0, -1.0, 0.0])
    k, A = wm.fit_weibull(speeds)
    assert k == 2.0
    assert A == 6.0


def test_fit_weibull_normal_data_clamped_and_finite():
    rng = np.random.default_rng(42)
    speeds = rng.weibull(2.0, size=5000) * 7.0
    k, A = wm.fit_weibull(speeds)
    assert 0.5 <= k <= 10.0
    assert A > 0
    # Should recover roughly k~2, A~7
    assert 1.5 < k < 2.5
    assert 6.0 < A < 8.0


def test_fit_weibull_constant_speeds_clamps_k():
    # All equal positive values -> degenerate; k must still be clamped/finite
    speeds = np.full(100, 5.0)
    k, A = wm.fit_weibull(speeds)
    assert 0.5 <= k <= 10.0
    assert np.isfinite(A)


# ---------------------------------------------------------------------------
# weibull_pdf
# ---------------------------------------------------------------------------


def test_weibull_pdf_zero_and_negative_are_zero():
    x = np.array([-1.0, 0.0, 5.0])
    pdf = wm.weibull_pdf(x, k=2.0, A=7.0)
    assert pdf[0] == 0.0
    assert pdf[1] == 0.0
    assert pdf[2] > 0.0


def test_weibull_pdf_integrates_to_about_one():
    x = np.linspace(0.001, 40, 40000)
    pdf = wm.weibull_pdf(x, k=2.0, A=8.0)
    trapz = getattr(np, "trapezoid", None) or np.trapz
    integral = trapz(pdf, x)
    assert integral == pytest.approx(1.0, abs=0.01)


# ---------------------------------------------------------------------------
# weibull_mean_power_density
# ---------------------------------------------------------------------------


def test_weibull_mean_power_density_default_rho():
    p = wm.weibull_mean_power_density(k=2.0, A=8.0)
    expected = 0.5 * 1.225 * 8.0 ** 3 * math.gamma(1.0 + 3.0 / 2.0)
    assert p == pytest.approx(expected)


def test_weibull_mean_power_density_custom_rho():
    p = wm.weibull_mean_power_density(k=2.0, A=8.0, rho=1.0)
    expected = 0.5 * 1.0 * 8.0 ** 3 * math.gamma(1.0 + 3.0 / 2.0)
    assert p == pytest.approx(expected)


# ---------------------------------------------------------------------------
# compute_wind_rose
# ---------------------------------------------------------------------------


def test_compute_wind_rose_basic_shapes_and_normalization():
    speeds = np.array([5.0, 6.0, 7.0, 8.0])
    directions = np.array([0.0, 90.0, 180.0, 270.0])
    rose = wm.compute_wind_rose(speeds, directions, n_sectors=4)
    assert len(rose.sectors) == 4
    assert len(rose.frequencies) == 4
    assert len(rose.mean_speeds) == 4
    assert rose.frequencies.sum() == pytest.approx(1.0)


def test_compute_wind_rose_wraparound_sector_zero():
    # Sector 0 wraps around 360 (lo > hi branch). Directions near 0 and 359.
    speeds = np.array([5.0, 9.0])
    directions = np.array([359.0, 1.0])
    rose = wm.compute_wind_rose(speeds, directions, n_sectors=16)
    # Both should fall into sector 0
    assert rose.frequencies[0] == pytest.approx(1.0)
    assert rose.mean_speeds[0] == pytest.approx(7.0)


def test_compute_wind_rose_direction_modulo():
    # Direction >= 360 should be wrapped via % 360
    speeds = np.array([4.0])
    directions = np.array([450.0])  # -> 90
    rose = wm.compute_wind_rose(speeds, directions, n_sectors=4)
    # 90 deg -> sector centered at 90 (index 1)
    assert rose.frequencies[1] == pytest.approx(1.0)


def test_compute_wind_rose_empty_total_zero():
    rose = wm.compute_wind_rose(np.array([]), np.array([]), n_sectors=8)
    assert rose.frequencies.sum() == 0.0
    assert np.all(rose.mean_speeds == 0.0)


# ---------------------------------------------------------------------------
# compute_wind_shear
# ---------------------------------------------------------------------------


def test_compute_wind_shear_too_few_valid_returns_default():
    low = np.array([0.1, 0.2])  # all below 0.5 threshold
    high = np.array([0.1, 0.2])
    alpha = wm.compute_wind_shear(low, high, 10.0, 80.0)
    assert alpha == 0.143


def test_compute_wind_shear_normal():
    low = np.full(20, 5.0)
    high = np.full(20, 7.0)
    alpha = wm.compute_wind_shear(low, high, 10.0, 80.0)
    expected = np.log(7.0 / 5.0) / np.log(80.0 / 10.0)
    assert alpha == pytest.approx(expected)


def test_compute_wind_shear_ratio_clipped_max():
    # Very large high/low ratio -> alpha clipped at 0.6
    low = np.full(10, 1.0)
    high = np.full(10, 100.0)
    alpha = wm.compute_wind_shear(low, high, 10.0, 11.0)
    assert alpha == 0.6


def test_compute_wind_shear_negative_clipped_min():
    # high < low -> negative alpha clipped at 0.0
    low = np.full(10, 9.0)
    high = np.full(10, 5.0)
    alpha = wm.compute_wind_shear(low, high, 10.0, 80.0)
    assert alpha == 0.0


# ---------------------------------------------------------------------------
# extrapolate_speed
# ---------------------------------------------------------------------------


def test_extrapolate_speed():
    v = wm.extrapolate_speed(6.0, 10.0, 80.0, 0.143)
    expected = 6.0 * (80.0 / 10.0) ** 0.143
    assert v == pytest.approx(expected)


# ---------------------------------------------------------------------------
# compute_diurnal_pattern
# ---------------------------------------------------------------------------


def test_compute_diurnal_pattern_valid_iso():
    timestamps = [f"2020-01-01T{h:02d}:00:00Z" for h in range(24)]
    speeds = np.arange(24, dtype=float)
    hours, means = wm.compute_diurnal_pattern(speeds, timestamps)
    assert list(hours) == list(range(24))
    assert means[5] == pytest.approx(5.0)
    assert means[23] == pytest.approx(23.0)


def test_compute_diurnal_pattern_invalid_timestamps_fallback():
    # Non-iso strings trigger the except branch -> i % 24 fallback
    timestamps = ["not-a-date"] * 48
    speeds = np.arange(48, dtype=float)
    hours, means = wm.compute_diurnal_pattern(speeds, timestamps)
    # hour 0 covers indices 0 and 24 -> mean(0,24)=12
    assert means[0] == pytest.approx(12.0)


def test_compute_diurnal_pattern_non_string_attribute_error():
    # Passing non-string (no .replace) triggers AttributeError branch
    timestamps = [None, None, None]
    speeds = np.array([1.0, 2.0, 3.0])
    hours, means = wm.compute_diurnal_pattern(speeds, timestamps)
    assert means[0] == pytest.approx(1.0)
    assert means[1] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# compute_seasonal_pattern
# ---------------------------------------------------------------------------


def test_compute_seasonal_pattern_valid():
    timestamps = [f"2020-{m:02d}-15T12:00:00Z" for m in range(1, 13)]
    speeds = np.arange(1, 13, dtype=float)
    months, means = wm.compute_seasonal_pattern(speeds, timestamps)
    assert list(months) == list(range(1, 13))
    assert means[0] == pytest.approx(1.0)
    assert means[11] == pytest.approx(12.0)


def test_compute_seasonal_pattern_invalid_fallback():
    timestamps = ["bad"] * 730
    speeds = np.full(730, 4.0)
    months, means = wm.compute_seasonal_pattern(speeds, timestamps)
    # All indices < 730 -> month (i//730)%12+1 = 1
    assert means[0] == pytest.approx(4.0)
    assert np.all(means[1:] == 0.0)


# ---------------------------------------------------------------------------
# _crf
# ---------------------------------------------------------------------------


def test_crf_positive_rate():
    r, n = 0.08, 25
    factor = (1 + r) ** n
    assert wm._crf(r, n) == pytest.approx(r * factor / (factor - 1))


def test_crf_zero_rate_returns_one_over_n():
    assert wm._crf(0.0, 20) == pytest.approx(1.0 / 20)


def test_crf_zero_rate_zero_n_returns_one():
    assert wm._crf(0.0, 0) == 1.0


def test_crf_negative_rate_branch():
    assert wm._crf(-0.1, 10) == pytest.approx(1.0 / 10)


# ---------------------------------------------------------------------------
# compute_wind_financials
# ---------------------------------------------------------------------------


def test_compute_wind_financials_basic():
    res = wm.compute_wind_financials(wm.WindFinancialInputs())
    assert res.capex_total == pytest.approx(10.0 * 1000.0 * 1300.0)
    assert res.annual_opex == pytest.approx(10.0 * 1000.0 * 25.0)
    assert res.lcoe > 0
    assert res.total_generation_mwh > 0
    assert 0.0 <= res.payback_years <= 25.0


def test_compute_wind_financials_zero_generation_lcoe_zero():
    inp = wm.WindFinancialInputs(capacity_factor=0.0)
    res = wm.compute_wind_financials(inp)
    assert res.lcoe == 0.0


def test_compute_wind_financials_payback_not_found_defaults_to_lifetime():
    # Huge capex / tiny revenue -> cumulative never reaches capex
    inp = wm.WindFinancialInputs(
        capex_per_kw=1_000_000.0,
        electricity_price=1.0,
        capacity_factor=0.01,
    )
    res = wm.compute_wind_financials(inp)
    assert res.payback_years == float(inp.lifetime_years)
    assert res.npv < 0


def test_compute_wind_financials_high_price_payback_found_early():
    inp = wm.WindFinancialInputs(electricity_price=500.0, capex_per_kw=100.0)
    res = wm.compute_wind_financials(inp)
    assert res.payback_years < inp.lifetime_years
    assert res.npv > 0


# ---------------------------------------------------------------------------
# _compute_irr
# ---------------------------------------------------------------------------


def test_compute_irr_normal_in_range():
    irr = wm._compute_irr(
        capex=1_000_000.0,
        annual_gen_yr1=30000.0,
        annual_opex=10000.0,
        price=50.0,
        degradation=0.005,
        lifetime=25,
    )
    assert -0.5 < irr < 2.0


def test_compute_irr_unprofitable_returns_lower_bound():
    # NPV even at lo=-0.5 is still negative -> returns lo
    irr = wm._compute_irr(
        capex=1e12,
        annual_gen_yr1=1.0,
        annual_opex=0.0,
        price=1.0,
        degradation=0.0,
        lifetime=25,
    )
    assert irr == -0.5


def test_compute_irr_extremely_profitable_returns_upper_bound():
    # NPV at hi=2.0 still positive -> returns hi
    irr = wm._compute_irr(
        capex=1.0,
        annual_gen_yr1=1e9,
        annual_opex=0.0,
        price=1.0,
        degradation=0.0,
        lifetime=25,
    )
    assert irr == 2.0


# ---------------------------------------------------------------------------
# compute_lcoe_sensitivity
# ---------------------------------------------------------------------------


def test_compute_lcoe_sensitivity_valid_param():
    inp = wm.WindFinancialInputs()
    out = wm.compute_lcoe_sensitivity(inp, "capex_per_kw", [1000.0, 1500.0, 2000.0])
    assert len(out) == 3
    # Higher capex -> higher LCOE
    assert out[0] < out[1] < out[2]


def test_compute_lcoe_sensitivity_invalid_param_unchanged():
    # param_name not a field -> setattr skipped, all results equal baseline
    inp = wm.WindFinancialInputs()
    baseline = wm.compute_wind_financials(inp).lcoe
    out = wm.compute_lcoe_sensitivity(inp, "nonexistent_field", [1.0, 2.0, 3.0])
    assert all(o == pytest.approx(baseline) for o in out)


def test_compute_lcoe_sensitivity_explicit_max_workers():
    inp = wm.WindFinancialInputs()
    out = wm.compute_lcoe_sensitivity(
        inp, "capacity_factor", [0.2, 0.3, 0.4], max_workers=2
    )
    assert len(out) == 3
    # Higher capacity factor -> lower LCOE
    assert out[0] > out[1] > out[2]


# ---------------------------------------------------------------------------
# jensen_wake_deficit
# ---------------------------------------------------------------------------


def test_jensen_wake_deficit_zero_downstream():
    assert wm.jensen_wake_deficit(0.0, 100.0, 0.8) == 0.0


def test_jensen_wake_deficit_zero_diameter():
    assert wm.jensen_wake_deficit(500.0, 0.0, 0.8) == 0.0


def test_jensen_wake_deficit_decreases_with_distance():
    near = wm.jensen_wake_deficit(100.0, 100.0, 0.8)
    far = wm.jensen_wake_deficit(1000.0, 100.0, 0.8)
    assert 0.0 < far < near <= 1.0


def test_jensen_wake_deficit_clamped_to_one():
    # Ct=1 -> a=1, very close downstream -> deficit near a/denom, clamp to 1
    d = wm.jensen_wake_deficit(0.001, 100.0, 1.0, wake_decay=0.075)
    assert d <= 1.0


# ---------------------------------------------------------------------------
# compute_array_efficiency
# ---------------------------------------------------------------------------


def _simple_rose(n_sectors=4):
    speeds = np.full(n_sectors, 8.0)
    directions = np.arange(n_sectors) * (360.0 / n_sectors)
    return wm.compute_wind_rose(speeds, directions, n_sectors=n_sectors)


def test_array_efficiency_single_turbine_is_one():
    rose = _simple_rose()
    assert wm.compute_array_efficiency(1, 5.0, 100.0, 0.8, rose) == 1.0


def test_array_efficiency_multi_turbine_in_range():
    rose = _simple_rose(4)
    eff = wm.compute_array_efficiency(9, 5.0, 100.0, 0.8, rose)
    assert 0.0 < eff <= 1.0


def test_array_efficiency_all_zero_freq_returns_one():
    # Rose with all-zero frequencies -> total_weight stays 0 -> returns 1.0
    rose = wm.WindRoseData(
        sectors=np.array([0.0, 90.0, 180.0, 270.0]),
        frequencies=np.zeros(4),
        mean_speeds=np.zeros(4),
    )
    eff = wm.compute_array_efficiency(9, 5.0, 100.0, 0.8, rose)
    assert eff == 1.0


def test_array_efficiency_tight_spacing_lower_than_wide():
    rose = _simple_rose(8)
    tight = wm.compute_array_efficiency(16, 2.0, 100.0, 0.85, rose)
    wide = wm.compute_array_efficiency(16, 12.0, 100.0, 0.85, rose)
    assert tight <= wide


# ---------------------------------------------------------------------------
# compute_spacing_curve
# ---------------------------------------------------------------------------


def test_compute_spacing_curve_default_spacings():
    rose = _simple_rose(4)
    spacings, effs = wm.compute_spacing_curve(100.0, 0.8, rose)
    assert spacings == [3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0]
    assert len(effs) == len(spacings)
    assert all(0.0 < e <= 1.0 for e in effs)


def test_compute_spacing_curve_custom_spacings_and_workers():
    rose = _simple_rose(4)
    spacings, effs = wm.compute_spacing_curve(
        100.0, 0.8, rose, spacings=[4.0, 8.0], n_turbines=9, max_workers=1
    )
    assert spacings == [4.0, 8.0]
    assert len(effs) == 2
