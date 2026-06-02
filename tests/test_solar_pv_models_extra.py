"""Additive unit tests for esfex.models.solar_pv_models.

Targets every public function plus internal branches: guard clauses,
error/edge paths, defaults, and each if/elif/else.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from esfex.models import solar_pv_models as spv


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


def test_hourly_irradiance_dataclass():
    d = spv.HourlyIrradianceData(
        timestamps=["2020-01-01T00:00"],
        ghi=np.array([100.0]),
        temperature=np.array([20.0]),
    )
    assert d.timestamps == ["2020-01-01T00:00"]
    assert d.ghi[0] == 100.0
    assert d.temperature[0] == 20.0


def test_financial_inputs_defaults():
    inp = spv.SolarFinancialInputs()
    assert inp.capacity_mw == 10.0
    assert inp.lifetime_years == 25


def test_financial_results_defaults():
    res = spv.SolarFinancialResults()
    assert res.lcoe == 0.0
    assert res.capex_total == 0.0


# ---------------------------------------------------------------------------
# compute_peak_sun_hours
# ---------------------------------------------------------------------------


def test_peak_sun_hours_basic():
    # 24 hours, constant 1000 W/m² → 24 Wh-equiv per "/1000" / 1 day = 24 PSH
    ghi = np.full(24, 1000.0)
    psh = spv.compute_peak_sun_hours(ghi)
    assert psh == pytest.approx(24.0)


def test_peak_sun_hours_short_array_uses_min_one_day():
    # len < 24 → n_days clamped to 1.0
    ghi = np.array([1000.0, 1000.0])
    psh = spv.compute_peak_sun_hours(ghi)
    assert psh == pytest.approx(2.0)


def test_peak_sun_hours_nan_ignored():
    ghi = np.array([np.nan] * 12 + [1000.0] * 12)
    psh = spv.compute_peak_sun_hours(ghi)
    assert psh == pytest.approx(12000.0 / 1000.0 / 1.0)


# ---------------------------------------------------------------------------
# compute_performance_ratio
# ---------------------------------------------------------------------------


def test_performance_ratio_no_sun_returns_zero():
    ghi = np.zeros(10)
    temp = np.zeros(10)
    pr = spv.compute_performance_ratio(ghi, temp, 0.2, -0.4, 45.0)
    assert pr == 0.0


def test_performance_ratio_at_stc_is_one():
    # GHI such that t_cell == 25 at temp=25 requires GHI=0 but masked out.
    # Use small GHI and temp giving t_cell ~25 → factor ~1.
    ghi = np.array([1.0])  # tiny positive
    temp = np.array([25.0])
    pr = spv.compute_performance_ratio(ghi, temp, -0.4, -0.4, 45.0)
    # t_cell ~= 25 + (25/800)*1 ~= 25.03, factor ~ 1 - 0.004*0.03 ~ 1.0
    assert pr == pytest.approx(1.0, abs=1e-3)


def test_performance_ratio_mismatched_temp_uses_default_25():
    ghi = np.array([0.0, 800.0, 800.0])
    temp = np.array([25.0])  # length mismatch → uses full(25.0)
    pr = spv.compute_performance_ratio(ghi, temp, -0.4, -0.4, 45.0)
    assert 0.0 < pr <= 1.5


def test_performance_ratio_clip_upper():
    # Strongly positive gamma + cold → factor would exceed 1.5, gets clipped
    ghi = np.array([1000.0])
    temp = np.array([-50.0])
    pr = spv.compute_performance_ratio(ghi, temp, 10.0, -0.4, 45.0)
    assert pr <= 1.5


# ---------------------------------------------------------------------------
# compute_clearness_index
# ---------------------------------------------------------------------------


def test_clearness_index_empty_inputs():
    assert spv.compute_clearness_index(np.array([]), 40.0, []) == 0.0
    assert spv.compute_clearness_index(np.array([1.0]), 40.0, []) == 0.0


def test_clearness_index_bad_timestamp_skipped_zero_et():
    # Non-parseable timestamp → all skipped → total_et <= 0 → 0.0
    ghi = np.array([500.0])
    out = spv.compute_clearness_index(ghi, 40.0, ["not-a-date"])
    assert out == 0.0


def test_clearness_index_nighttime_only_zero():
    # Midnight → cos_z negative → et clamped to 0 → total_et<=0 → 0.0
    ghi = np.array([0.0])
    out = spv.compute_clearness_index(ghi, 40.0, ["2020-06-21T00:00:00"])
    assert out == 0.0


def test_clearness_index_daytime_bounded():
    # Noon summer at mid latitude → positive et, ratio in [0,1]
    ts = ["2020-06-21T12:00:00Z"]
    ghi = np.array([800.0])
    out = spv.compute_clearness_index(ghi, 40.0, ts)
    assert 0.0 < out <= 1.0


def test_clearness_index_capped_at_one():
    # Huge GHI relative to et → min(1.0, ...) caps it
    ts = ["2020-06-21T12:00:00"]
    ghi = np.array([1e9])
    out = spv.compute_clearness_index(ghi, 40.0, ts)
    assert out == 1.0


def test_clearness_index_more_timestamps_than_ghi():
    # i >= len(ghi) branch: et accumulates but ghi not added for extra index
    ts = ["2020-06-21T12:00:00", "2020-06-21T13:00:00"]
    ghi = np.array([800.0])  # only one GHI value
    out = spv.compute_clearness_index(ghi, 40.0, ts)
    assert 0.0 <= out <= 1.0


def test_clearness_index_southern_hemisphere():
    ts = ["2020-12-21T12:00:00"]
    ghi = np.array([700.0])
    out = spv.compute_clearness_index(ghi, -33.0, ts)
    assert 0.0 < out <= 1.0


# ---------------------------------------------------------------------------
# compute_diurnal_irradiance
# ---------------------------------------------------------------------------


def test_diurnal_basic_hours_and_means():
    ts = [f"2020-01-01T{h:02d}:00:00" for h in range(24)]
    ghi = np.arange(24, dtype=float) * 10.0
    hours, means = spv.compute_diurnal_irradiance(ghi, ts)
    assert list(hours) == list(range(24))
    assert means[5] == pytest.approx(50.0)


def test_diurnal_breaks_when_ghi_shorter_than_ts():
    ts = [f"2020-01-01T{h:02d}:00:00" for h in range(24)]
    ghi = np.array([100.0, 200.0])  # only 2 values → break at i==2
    hours, means = spv.compute_diurnal_irradiance(ghi, ts)
    assert means[0] == 100.0
    assert means[1] == 200.0
    assert means[3] == 0.0


def test_diurnal_bad_timestamp_continues():
    ts = ["bad", "2020-01-01T05:00:00"]
    ghi = np.array([999.0, 50.0])
    hours, means = spv.compute_diurnal_irradiance(ghi, ts)
    assert means[5] == pytest.approx(50.0)
    # The bad one (index 0) was skipped, so hour 0 stays 0
    assert means[0] == 0.0


# ---------------------------------------------------------------------------
# compute_monthly_irradiance
# ---------------------------------------------------------------------------


def test_monthly_basic():
    ts = ["2020-01-15T12:00:00", "2020-02-15T12:00:00"]
    ghi = np.array([1000.0, 2000.0])
    months, totals = spv.compute_monthly_irradiance(ghi, ts)
    assert list(months) == list(range(1, 13))
    assert totals[0] == pytest.approx(1.0)   # 1000 Wh → 1 kWh
    assert totals[1] == pytest.approx(2.0)


def test_monthly_breaks_when_ghi_short():
    ts = ["2020-01-15T12:00:00", "2020-02-15T12:00:00"]
    ghi = np.array([1000.0])  # break at i==1
    months, totals = spv.compute_monthly_irradiance(ghi, ts)
    assert totals[0] == pytest.approx(1.0)
    assert totals[1] == 0.0


def test_monthly_bad_timestamp_continues_and_negatives_clamped():
    ts = ["bad", "2020-03-10T12:00:00"]
    ghi = np.array([5000.0, -500.0])  # negative clamped to 0
    months, totals = spv.compute_monthly_irradiance(ghi, ts)
    assert totals[2] == 0.0  # March: max(0,-500)=0
    assert all(t == 0.0 for t in totals)


# ---------------------------------------------------------------------------
# compute_temp_analysis
# ---------------------------------------------------------------------------


def test_temp_analysis_shapes_and_derating_clip():
    ghi = np.array([0.0, 800.0, 1000.0])
    temp = np.array([25.0, 25.0, -100.0])
    t_cell, derating = spv.compute_temp_analysis(ghi, temp, 45.0)
    assert t_cell.shape == (3,)
    assert derating.shape == (3,)
    # All derating values clipped to [0, 1.5]
    assert np.all(derating >= 0.0) and np.all(derating <= 1.5)
    # At GHI=0, temp=25 → t_cell=25 → derating=1.0
    assert derating[0] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# _crf
# ---------------------------------------------------------------------------


def test_crf_positive_rate():
    crf = spv._crf(0.08, 25)
    assert crf == pytest.approx(0.08 * 1.08 ** 25 / (1.08 ** 25 - 1))


def test_crf_zero_rate():
    assert spv._crf(0.0, 25) == pytest.approx(1.0 / 25)


def test_crf_negative_rate_and_zero_years():
    # rate <= 0 branch, years clamped via max(years,1)
    assert spv._crf(-0.1, 0) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# _compute_irr
# ---------------------------------------------------------------------------


def test_irr_converges_on_known_cashflow():
    # -100 then +110 → IRR ~ 0.10
    irr = spv._compute_irr([-100.0, 110.0])
    assert irr == pytest.approx(0.10, abs=1e-3)


def test_irr_no_root_returns_midpoint():
    # All-positive cash flows: npv always > 0 → lo keeps rising → returns ~hi
    irr = spv._compute_irr([100.0, 100.0, 100.0])
    assert irr > 4.0  # pushed toward hi=5.0


# ---------------------------------------------------------------------------
# compute_pv_financials
# ---------------------------------------------------------------------------


def test_pv_financials_basic_profitable():
    inp = spv.SolarFinancialInputs(
        capacity_mw=10.0,
        capacity_factor=0.25,
        capex_per_kw=800.0,
        opex_per_kw_yr=15.0,
        discount_rate=0.06,
        lifetime_years=25,
        electricity_price=60.0,
        degradation_rate=0.005,
    )
    res = spv.compute_pv_financials(inp)
    assert res.capex_total == pytest.approx(10.0 * 1000.0 * 800.0)
    assert res.annual_opex == pytest.approx(10.0 * 1000.0 * 15.0)
    assert res.lcoe > 0
    assert res.total_generation_mwh > 0
    # Year-1 revenue
    assert res.annual_revenue == pytest.approx(10.0 * 0.25 * 8760.0 * 60.0)
    # Profitable project → payback found before lifetime
    assert res.payback_years < inp.lifetime_years


def test_pv_financials_zero_generation_infinite_lcoe():
    inp = spv.SolarFinancialInputs(capacity_factor=0.0)
    res = spv.compute_pv_financials(inp)
    assert math.isinf(res.lcoe)


def test_pv_financials_payback_not_found_defaults_to_lifetime():
    # Very low price → never recovers capex → payback stays = lifetime
    inp = spv.SolarFinancialInputs(
        capacity_factor=0.20,
        electricity_price=0.01,
        lifetime_years=5,
    )
    res = spv.compute_pv_financials(inp)
    assert res.payback_years == pytest.approx(float(5))


def test_pv_financials_zero_capex_immediate_payback():
    # capex_total = 0 and positive cash flow → cumulative >= 0 at t=1.
    # prev = cumulative - cf_t = 0 → frac = -0/cf_t = 0 → payback = (1-1)+0 = 0
    inp = spv.SolarFinancialInputs(
        capacity_mw=1.0,
        capacity_factor=0.2,
        capex_per_kw=0.0,        # capex_total = 0
        opex_per_kw_yr=10.0,
        electricity_price=100.0,  # revenue > opex → cf_t > 0
        lifetime_years=3,
    )
    res = spv.compute_pv_financials(inp)
    assert res.payback_years == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# compute_pv_lcoe_sensitivity
# ---------------------------------------------------------------------------


def test_lcoe_sensitivity_serial_small_list():
    inp = spv.SolarFinancialInputs()
    vals = [0.1, 0.2, 0.3]  # len<=3 → serial branch
    out = spv.compute_pv_lcoe_sensitivity(inp, "capacity_factor", vals)
    assert len(out) == 3
    # higher CF → lower LCOE
    assert out[0] > out[2]


def test_lcoe_sensitivity_parallel_branch():
    inp = spv.SolarFinancialInputs()
    vals = [0.1, 0.15, 0.2, 0.25, 0.3]  # len>3 → threadpool if workers>1
    out = spv.compute_pv_lcoe_sensitivity(
        inp, "capacity_factor", vals, max_workers=4
    )
    assert len(out) == 5
    assert all(v > 0 for v in out)


def test_lcoe_sensitivity_single_worker_forces_serial():
    inp = spv.SolarFinancialInputs()
    vals = [0.1, 0.15, 0.2, 0.25, 0.3]
    out = spv.compute_pv_lcoe_sensitivity(
        inp, "capacity_factor", vals, max_workers=1
    )
    assert len(out) == 5


# ---------------------------------------------------------------------------
# compute_gcr_shading_loss
# ---------------------------------------------------------------------------


def test_gcr_shading_invalid_gcr_returns_zero():
    assert spv.compute_gcr_shading_loss(40.0, 30.0, 0.0) == 0.0
    assert spv.compute_gcr_shading_loss(40.0, 30.0, 1.5) == 0.0


def test_gcr_shading_polar_sun_never_rises():
    # Very high latitude → winter solstice solar_alt <= 0 → full shading
    loss = spv.compute_gcr_shading_loss(80.0, 30.0, 0.5)
    assert loss == 1.0


def test_gcr_shading_low_density_no_shading():
    # Low GCR (wide spacing) → shadow shorter than gap → 0 loss
    loss = spv.compute_gcr_shading_loss(20.0, 10.0, 0.15)
    assert loss == 0.0


def test_gcr_shading_high_density_positive_loss():
    # High GCR + steep tilt + mid-high lat → some shading
    loss = spv.compute_gcr_shading_loss(50.0, 45.0, 0.8)
    assert 0.0 < loss <= 1.0


def test_gcr_shading_gap_nonpositive_branch():
    # GCR near 1 with large tilt → row_pitch small, footprint large → gap<=0
    loss = spv.compute_gcr_shading_loss(45.0, 5.0, 1.0)
    assert 0.0 <= loss <= 1.0


# ---------------------------------------------------------------------------
# compute_gcr_curve
# ---------------------------------------------------------------------------


def test_gcr_curve_default_gcrs_serial():
    gcrs, losses = spv.compute_gcr_curve(40.0, 30.0)
    assert len(gcrs) == 15
    assert len(losses) == 15
    assert all(0.0 <= l <= 1.0 for l in losses)


def test_gcr_curve_custom_short_list_serial():
    gcrs, losses = spv.compute_gcr_curve(40.0, 30.0, gcrs=[0.2, 0.4, 0.6])
    assert gcrs == [0.2, 0.4, 0.6]
    assert len(losses) == 3


def test_gcr_curve_parallel_branch():
    custom = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7]
    gcrs, losses = spv.compute_gcr_curve(
        40.0, 30.0, gcrs=custom, max_workers=4
    )
    assert gcrs == custom
    assert len(losses) == 6


def test_gcr_curve_single_worker_serial():
    custom = [0.2, 0.3, 0.4, 0.5, 0.6]
    gcrs, losses = spv.compute_gcr_curve(
        40.0, 30.0, gcrs=custom, max_workers=1
    )
    assert len(losses) == 5


# ---------------------------------------------------------------------------
# compute_bifacial_gain
# ---------------------------------------------------------------------------


def test_bifacial_invalid_inputs_zero():
    assert spv.compute_bifacial_gain(0.0, 0.4, 2.0, 30.0) == 0.0
    assert spv.compute_bifacial_gain(0.3, 0.0, 2.0, 30.0) == 0.0
    assert spv.compute_bifacial_gain(0.3, 0.4, 2.0, 30.0, bifaciality=0.0) == 0.0


def test_bifacial_typical_positive_gain():
    gain = spv.compute_bifacial_gain(0.5, 0.3, 2.0, 30.0, bifaciality=0.7)
    assert 0.0 < gain <= 0.50


def test_bifacial_gain_capped_at_50pct():
    # Extreme albedo + open layout pushes toward cap
    gain = spv.compute_bifacial_gain(1.0, 0.05, 5.0, 80.0, bifaciality=0.8)
    assert gain <= 0.50


def test_bifacial_clearance_floor():
    # Very flat tilt → sin(tilt)~0 → clearance floors at 0.3
    gain = spv.compute_bifacial_gain(0.5, 0.3, 2.0, 0.5, bifaciality=0.7)
    assert gain >= 0.0
