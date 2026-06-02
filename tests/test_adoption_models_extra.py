"""Additive coverage tests for esfex.models.adoption_models."""

from __future__ import annotations

import math

import numpy as np
import pytest

from esfex.models.adoption_models import (
    AdoptionCurve,
    MacroeconomicData,
    ValidationData,
    fit_adoption_to_rooftop_config,
    run_abm_adoption,
    run_bass_diffusion,
    run_logistic_adoption,
    run_techno_economic,
)


# ── Data structures ────────────────────────────────────────────────


def test_macroeconomic_defaults():
    m = MacroeconomicData()
    assert m.gdp_per_capita == 5000.0
    assert m.pv_cost_trajectory == {}
    assert m.country_iso == ""


def test_adoption_curve_defaults():
    c = AdoptionCurve(method="x")
    assert c.years == []
    assert c.penetration == []
    assert c.parameters == {}


def test_validation_data_defaults():
    v = ValidationData(label="IRENA")
    assert v.source == "manual"
    assert v.years == []
    assert v.capacity_mw == []


# ── Logistic ───────────────────────────────────────────────────────


def test_logistic_basic_shape():
    m = MacroeconomicData()
    curve = run_logistic_adoption(m, max_potential_mw=100.0, base_year=2025, target_year=2030)
    assert curve.method == "logistic"
    assert curve.years == [2025, 2026, 2027, 2028, 2029, 2030]
    assert len(curve.penetration) == 6
    assert len(curve.capacity_mw) == 6
    # penetration in [0,1]
    assert all(0.0 <= p <= 1.0 for p in curve.penetration)
    # capacity = prob * max
    assert curve.capacity_mw[0] == pytest.approx(curve.penetration[0] * 100.0)


def test_logistic_custom_coefficients_merge():
    m = MacroeconomicData()
    curve = run_logistic_adoption(
        m, 100.0, 2025, 2026, coefficients={"beta_0": 5.0, "beta_policy": 1.0}
    )
    # custom coeff stored in parameters, defaults preserved for others
    assert curve.parameters["beta_0"] == 5.0
    assert curve.parameters["beta_policy"] == 1.0
    assert curve.parameters["beta_gdp"] == 0.00005


def test_logistic_uses_cost_trajectory_override():
    # Trajectory for a specific year should change penetration vs no trajectory
    m_no = MacroeconomicData()
    m_traj = MacroeconomicData(pv_cost_trajectory={2026: 50.0})
    c_no = run_logistic_adoption(m_no, 100.0, 2025, 2026)
    c_traj = run_logistic_adoption(m_traj, 100.0, 2025, 2026)
    # year 2026 differs because cost override changes z
    assert c_no.penetration[1] != c_traj.penetration[1]
    # 2025 (base) same
    assert c_no.penetration[0] == pytest.approx(c_traj.penetration[0])


def test_logistic_beta_policy_absent_uses_default():
    m = MacroeconomicData()
    # coefficients dict missing beta_policy -> default kept (0.5 from base dict)
    curve = run_logistic_adoption(m, 100.0, 2025, 2025, coefficients={"beta_0": 0.0})
    assert curve.parameters["beta_policy"] == 0.5


# ── Bass diffusion ─────────────────────────────────────────────────


def test_bass_default_low_initial_no_offset():
    # initial_penetration default 0.01 <= 0.001? no, 0.01 > 0.001 triggers offset loop
    curve = run_bass_diffusion(100.0, 2025, 2030)
    assert curve.method == "bass"
    assert len(curve.penetration) == 6
    assert all(0.0 <= p <= 1.0 for p in curve.penetration)
    assert curve.parameters["p"] == 0.03


def test_bass_initial_below_threshold_no_offset():
    # initial_penetration <= 0.001 skips the bisection branch (t_offset stays 0)
    curve = run_bass_diffusion(100.0, 2025, 2026, initial_penetration=0.0005)
    # At t=0, F(0)=0
    assert curve.penetration[0] == pytest.approx(0.0)


def test_bass_offset_branch_increases_start():
    # Larger initial penetration forces t_offset > 0 so first value reflects it
    curve = run_bass_diffusion(100.0, 2025, 2026, initial_penetration=0.05)
    assert curve.penetration[0] >= 0.05 - 1e-6


def test_bass_monotonic_increasing():
    curve = run_bass_diffusion(100.0, 2025, 2050)
    pen = curve.penetration
    assert all(pen[i + 1] >= pen[i] - 1e-9 for i in range(len(pen) - 1))


def test_bass_capacity_scales():
    curve = run_bass_diffusion(200.0, 2025, 2026, initial_penetration=0.0)
    assert curve.capacity_mw[1] == pytest.approx(curve.penetration[1] * 200.0)


# ── Techno-economic ────────────────────────────────────────────────


def test_techno_economic_basic():
    m = MacroeconomicData()
    curve = run_techno_economic(m, 100.0, base_year=2025, target_year=2030)
    assert curve.method == "techno_economic"
    assert len(curve.penetration) == 6
    assert all(0.0 <= p <= 1.0 for p in curve.penetration)
    assert curve.parameters["system_lifetime"] == 25


def test_techno_economic_zero_discount_rate_crf_branch():
    # r <= 0 path: crf = 1/n
    m = MacroeconomicData(discount_rate=0.0)
    curve = run_techno_economic(
        m, 100.0, base_year=2025, target_year=2026, system_lifetime=10
    )
    assert len(curve.penetration) == 2
    assert all(0.0 <= p <= 1.0 for p in curve.penetration)


def test_techno_economic_cost_trajectory_branch():
    m_traj = MacroeconomicData(pv_cost_trajectory={2026: 10.0})
    m_no = MacroeconomicData()
    c_traj = run_techno_economic(m_traj, 100.0, base_year=2025, target_year=2026)
    c_no = run_techno_economic(m_no, 100.0, base_year=2025, target_year=2026)
    # Lower cost in 2026 -> lower LCOE -> higher adoption
    assert c_traj.penetration[1] >= c_no.penetration[1]


def test_techno_economic_high_tariff_high_adoption():
    m = MacroeconomicData(electricity_tariff=2.0, pv_system_cost=500.0)
    curve = run_techno_economic(m, 100.0, base_year=2025, target_year=2025)
    # Very high tariff vs low cost -> near saturation
    assert curve.penetration[0] > 0.9


# ── ABM ────────────────────────────────────────────────────────────


def test_abm_basic_deterministic_with_seed():
    m = MacroeconomicData()
    curve = run_abm_adoption(
        m, 100.0, base_year=2025, target_year=2027,
        n_agents=30, n_iterations=3, seed=42,
    )
    assert curve.method == "abm"
    assert len(curve.penetration) == 3
    assert len(curve.confidence_low) == 3
    assert len(curve.confidence_high) == 3
    assert all(0.0 <= p <= 1.0 for p in curve.penetration)
    assert curve.parameters["n_agents"] == 30


def test_abm_reproducible_same_seed():
    m = MacroeconomicData()
    kw = dict(base_year=2025, target_year=2026, n_agents=25, n_iterations=2, seed=7)
    c1 = run_abm_adoption(m, 100.0, **kw)
    c2 = run_abm_adoption(m, 100.0, **kw)
    assert c1.penetration == c2.penetration


def test_abm_seed_none_branch():
    # seed=None exercises the `seed + iteration if seed else None` False branch
    m = MacroeconomicData()
    curve = run_abm_adoption(
        m, 100.0, 2025, 2026, n_agents=20, n_iterations=2, seed=None
    )
    assert len(curve.penetration) == 2


def test_abm_building_positions_more_than_agents():
    # len(positions) >= n_agents -> slice branch
    m = MacroeconomicData()
    positions = np.random.default_rng(0).uniform(0, 5, (50, 2))
    curve = run_abm_adoption(
        m, 100.0, 2025, 2026, n_agents=10, n_iterations=2,
        building_positions=positions, seed=1,
    )
    assert len(curve.penetration) == 2


def test_abm_building_positions_fewer_than_agents():
    # 0 < len(positions) < n_agents -> resample-with-replacement branch
    m = MacroeconomicData()
    positions = np.random.default_rng(0).uniform(0, 5, (5, 2))
    curve = run_abm_adoption(
        m, 100.0, 2025, 2026, n_agents=20, n_iterations=2,
        building_positions=positions, seed=1,
    )
    assert len(curve.penetration) == 2


def test_abm_empty_building_positions_uses_random():
    # building_positions present but len==0 -> falls to random else branch
    m = MacroeconomicData()
    positions = np.empty((0, 2))
    curve = run_abm_adoption(
        m, 100.0, 2025, 2026, n_agents=15, n_iterations=2,
        building_positions=positions, seed=1,
    )
    assert len(curve.penetration) == 2


def test_abm_cost_trajectory_branch():
    # pv_cost_trajectory hit inside ABM year loop (line ~390)
    m = MacroeconomicData(pv_cost_trajectory={2025: 100.0, 2026: 80.0})
    curve = run_abm_adoption(
        m, 100.0, 2025, 2026, n_agents=15, n_iterations=2, seed=4,
    )
    assert len(curve.penetration) == 2


def test_abm_zero_discount_rate_agent_crf_branch():
    # personal_dr clipped min is 0.02 so r_a>0 always; force tiny via gdp tricks not needed.
    # Instead high tariff forces econ high so adoptions happen (exercises adopted continue path)
    m = MacroeconomicData(electricity_tariff=5.0, pv_system_cost=100.0)
    curve = run_abm_adoption(
        m, 100.0, 2025, 2030, n_agents=30, n_iterations=2, seed=3,
    )
    # With strong economics, some adoption should occur by the end
    assert curve.penetration[-1] > 0.0


# ── fit_adoption_to_rooftop_config ─────────────────────────────────


def test_fit_empty_curve_returns_empty_dict():
    m = MacroeconomicData()
    empty = AdoptionCurve(method="logistic")
    assert fit_adoption_to_rooftop_config(empty, m, 2, [10, 10], [5.0, 5.0]) == {}


def test_fit_no_penetration_returns_empty():
    m = MacroeconomicData()
    c = AdoptionCurve(method="logistic", years=[2025, 2026], penetration=[])
    assert fit_adoption_to_rooftop_config(c, m, 1, [5], [4.0]) == {}


def test_fit_basic_midpoint_slope_branch():
    m = MacroeconomicData()
    years = list(range(2025, 2031))
    pen = [0.0, 0.1, 0.25, 0.45, 0.6, 0.7]
    c = AdoptionCurve(method="logistic", years=years, penetration=pen)
    out = fit_adoption_to_rooftop_config(c, m, 3, [10, 20, 30], [5.0, 5.0, 5.0])
    assert out["base_year"] == 2025
    assert out["target_year"] == 2030
    assert out["initial_adoption"] == [0.0, 0.0, 0.0]
    assert out["adoption_scenario"] == "medium"
    assert out["max_adoption"]["medium"] == pytest.approx(0.7)
    assert out["max_adoption"]["low"] == pytest.approx(0.7 * 0.6)
    assert out["max_adoption"]["high"] == pytest.approx(min(0.95, 0.7 * 1.3))
    assert out["cost_per_kw"] == m.pv_system_cost
    # rate from midpoint slope branch
    assert 0.01 <= out["adoption_rates"]["medium"] <= 0.25


def test_fit_max_adoption_high_capped_at_095():
    m = MacroeconomicData()
    years = [2025, 2026, 2027]
    pen = [0.0, 0.4, 0.9]  # 0.9*1.3=1.17 -> capped 0.95
    c = AdoptionCurve(method="logistic", years=years, penetration=pen)
    out = fit_adoption_to_rooftop_config(c, m, 1, [5], [4.0])
    assert out["max_adoption"]["high"] == 0.95


def test_fit_single_point_curve_default_rate():
    # len==1 -> mid_idx=0 -> else branch rate=0.08
    m = MacroeconomicData()
    c = AdoptionCurve(method="logistic", years=[2025], penetration=[0.3])
    out = fit_adoption_to_rooftop_config(c, m, 1, [5], [4.0])
    assert out["adoption_rates"]["medium"] == pytest.approx(0.08)
    assert out["adoption_rates"]["low"] == pytest.approx(0.08 * 0.6)
    assert out["adoption_rates"]["high"] == pytest.approx(0.08 * 1.5)


def test_fit_rate_clamped_minimum():
    # flat curve -> slope ~0 -> rate clamped to 0.01
    m = MacroeconomicData()
    pen = [0.5, 0.5, 0.5, 0.5, 0.5]
    c = AdoptionCurve(method="logistic", years=list(range(2025, 2030)), penetration=pen)
    out = fit_adoption_to_rooftop_config(c, m, 1, [5], [4.0])
    assert out["adoption_rates"]["medium"] == pytest.approx(0.01)


def test_fit_rate_clamped_maximum():
    # steep curve -> slope*2 large -> rate clamped to 0.25
    m = MacroeconomicData()
    pen = [0.0, 0.0, 0.0, 0.99, 1.0]
    c = AdoptionCurve(method="logistic", years=list(range(2025, 2030)), penetration=pen)
    out = fit_adoption_to_rooftop_config(c, m, 1, [5], [4.0])
    assert out["adoption_rates"]["medium"] == pytest.approx(0.25)
