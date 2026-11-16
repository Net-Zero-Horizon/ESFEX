"""Masters model the critical days, and storage is firm only up to its energy.

Cuba myopic (round 31): the dispatch shed 0.2-1.1 % of the demand at night
and in the early morning, mostly October to December, on genuinely
energy-short low-renewable days, while every master saw no shortage. Two
causes (round 33):

- the representative days, chosen to match the year's mean energy, never
  include a dark, windless day: each master now also models the days with
  the largest net load (``critical_days``), one day of the year each, the
  representative days reweighted so the year keeps its energy;
- the capacity adequacy counted a battery at its full power whatever its
  duration: it now counts min(power, usable energy / H), H the longest
  stretch without solar on the critical days (``storage_firm_hours``).
"""

import logging
import sys

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import (  # noqa: E402
    _create_orchestrator,
    _make_esfex_config,
    _make_generator,
    _make_system_config,
)

from esfex.config.schema import MasterProblemConfig, TechnologyConfig  # noqa: E402

DAY = 24
N_DAYS = 365
SOLAR, WIND_NEW = (f"test_sys__{k}" for k in ("solar", "wind_new"))
DARK = 290            # the dark, windless day (0-based)


def _year():
    """Demand (8760 × 2 nodes), PV and wind (8760) of a year whose levels
    vary from day to day, with one dark, windless day (DARK) and a dim one
    (day 100)."""
    a = ((np.arange(N_DAYS) * 97) % N_DAYS) / (N_DAYS - 1)
    hour = np.arange(DAY)
    shape = 1.0 + 0.2 * np.sin(2 * np.pi * (hour - 8) / DAY)
    total = np.concatenate([100.0 * (1.0 + 0.3 * a_d) * shape for a_d in a])
    demand = np.column_stack([0.6 * total, 0.4 * total])
    sun = np.clip(np.sin(np.pi * (hour - 6) / 12), 0.0, None)   # dark 18-6 h: 13 h
    level = 0.6 + 0.4 * a
    level[DARK], level[100] = 0.1, 0.4
    pv = np.concatenate([lv * 0.8 * sun for lv in level])
    wind_level = 0.3 + 0.4 * a
    wind_level[DARK] = 0.02
    wind = np.repeat(wind_level, DAY)
    return demand, pv, wind


def _mean_ratio(series, offsets, weights):
    """Weighted mean of *series* on the days at *offsets* over its annual mean."""
    days = np.array([series[o:o + DAY].mean() for o in offsets])
    return float(np.dot(weights, days) / np.sum(weights) / series.mean())


@pytest.mark.julia
def test_the_dark_day_is_critical_and_the_night_sets_the_firm_hours():
    from esfex.bridge.adapters import master_critical_days

    demand, pv, wind = _year()
    days, hours = master_critical_days(demand, 1, 2, 5, profiles=[pv, wind],
                                       capacities=[100.0, 50.0])
    assert days[0] == DARK * DAY          # the most critical first
    assert len(days) == 2 and abs(days[1] - days[0]) >= 5 * DAY
    assert hours == 13.0                  # the night: sun at most 1 % from 18 h to 6 h
    # A modelled day is not chosen again; 0 critical days still give the hours
    days, _ = master_critical_days(demand, 1, 1, 5, profiles=[pv, wind],
                                   capacities=[100.0, 50.0], exclude=[DARK * DAY])
    assert days != [DARK * DAY]
    assert master_critical_days(demand, 1, 0, 5, profiles=[pv, wind],
                                capacities=[100.0, 50.0]) == ([], 13.0)
    # At 6-hour steps: two dark steps of 6 h
    agg = lambda x: x.reshape(-1, 6, *x.shape[1:]).mean(axis=1)  # noqa: E731
    days6, hours6 = master_critical_days(agg(demand), 1, 1, 5, resolution_hours=6,
                                         profiles=[agg(pv), agg(wind)],
                                         capacities=[100.0, 50.0])
    assert days6 == [DARK * 4] and hours6 == 12.0
    # Wind only: the windless day is renewable-short all day
    assert master_critical_days(demand, 1, 1, 5, profiles=[wind])[1] == 24.0
    # No renewable profile: 4 h
    assert master_critical_days(demand, 1, 1, 5)[1] == 4.0


@pytest.mark.julia
def test_weights_keep_the_year_of_the_representative_days():
    from esfex.bridge.adapters import (
        master_critical_days,
        master_day_weights,
        master_rep_day_offsets,
    )

    demand, pv, wind = _year()
    total = demand.sum(axis=1)
    rep = master_rep_day_offsets(demand, 1, 5, 5, profiles=[pv, wind])
    crit, _ = master_critical_days(demand, 1, 2, 5, profiles=[pv, wind],
                                   capacities=[100.0, 50.0], exclude=rep)
    weights = master_day_weights(demand, 1, rep, crit, profiles=[pv, wind])
    assert len(weights) == 7
    assert sum(weights) == pytest.approx(N_DAYS)
    assert weights[5:] == [1.0, 1.0]       # one day of the year each
    offsets = rep + crit
    for series in (total, pv, wind):
        alone = _mean_ratio(series, rep, [1.0] * 5)
        assert _mean_ratio(series, offsets, weights) == pytest.approx(alone, abs=2e-3)
    # Equal shares would make the year darker
    naive = [(N_DAYS - 2) / 5] * 5 + [1.0, 1.0]
    assert _mean_ratio(pv, offsets, naive) < _mean_ratio(pv, rep, [1.0] * 5) - 1e-3
    # TSAM: from the cluster sizes
    prior = [100.0, 80.0, 70.0, 60.0, 55.0]
    tw = master_day_weights(demand, 1, rep, crit, profiles=[pv, wind], prior=prior)
    assert sum(tw) == pytest.approx(N_DAYS) and tw[5:] == [1.0, 1.0]
    assert _mean_ratio(pv, offsets, tw) == pytest.approx(
        _mean_ratio(pv, rep, prior), abs=2e-3)
    # No critical days: the representative days' own weights
    assert master_day_weights(demand, 1, rep, [], profiles=[pv]) == [N_DAYS / 5] * 5


def _orch(tmp_path, **mp):
    gas = _make_generator("Gas", "Non-renewable", "Gas", [100.0, 50.0])
    solar = _make_generator("Solar", "Renewable", "Sun", [50.0, 0.0])
    wind_new = TechnologyConfig(
        name="Wind", type="Renewable", fuel="Wind", invest_cost=[1.0, 1.0],
        invest_max_power=[100.0, 100.0], eff_at_rated=[1.0, 1.0],
        degradation_rate=[0.0, 0.0], lifetime=25)
    sysc = _make_system_config(generators={"gas": gas, "solar": solar}).model_copy(
        update={"technologies": {"wind_new": wind_new}})
    cfg = _make_esfex_config(sysc, master_problem=MasterProblemConfig(**mp))
    orch = _create_orchestrator(cfg, tmp_path=tmp_path)
    _, pv, wind = _year()
    orch._availability_cache = {SOLAR: np.column_stack([pv, pv]),
                                WIND_NEW: np.column_stack([wind, wind])}
    return orch


@pytest.mark.julia
def test_runner_plan_adds_the_critical_days(tmp_path, caplog):
    demand, pv, wind = _year()
    orch = _orch(tmp_path, representative_days=5, min_day_separation=5, critical_days=2)
    # The capacity behind each profile: solar 50 MW, wind 200 MW of potential
    caps = {label: c for label, _, _, c in orch._master_re_profiles_with_capacity(1)}
    assert caps == {SOLAR: 50.0, WIND_NEW: 200.0}
    with caplog.at_level(logging.INFO, logger="esfex.runner"):
        plan = orch._master_day_plan(demand, 1, 1)
    assert len(plan.rep_day_offsets) == 5 and plan.tsam is None
    assert DARK * DAY in plan.critical_day_offsets and len(plan.critical_day_offsets) == 2
    assert len(plan.rep_day_weights) == 7 and sum(plan.rep_day_weights) == pytest.approx(N_DAYS)
    assert plan.storage_firm_hours == 13.0
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "Critical days (lowest renewable supply against demand)" in text and "18 Oct" in text
    assert "Representative and critical days mean / annual mean" in text
    assert "Storage firm hours in the capacity adequacy: 13.0 h" in text

    # Configured hours; no critical days
    orch = _orch(tmp_path, representative_days=5, min_day_separation=5, critical_days=0,
                 storage_firm_hours=6.0)
    plan = orch._master_day_plan(demand, 1, 1)
    assert plan.critical_day_offsets == [] and plan.rep_day_weights == []
    assert plan.storage_firm_hours == 6.0


@pytest.mark.julia
def test_tsam_periods_include_the_critical_days(tmp_path):
    demand, _, _ = _year()
    orch = _orch(tmp_path, use_tsam=True, tsam_num_periods=6, critical_days=1)
    plan = orch._master_day_plan(demand, 1, 1)
    tsam = plan.tsam
    assert plan.rep_day_offsets is None and plan.critical_day_offsets == []
    assert tsam.num_periods == 7 and tsam.period_start_hours[-1] == DARK * DAY
    assert tsam.period_weights[-1] == 1.0
    assert sum(tsam.period_weights) == pytest.approx(N_DAYS)
    starts = tsam.period_start_hours
    assert [starts[i] for i in tsam.chronological_order] == sorted(starts)


@pytest.mark.julia
def test_every_master_gets_the_plan(tmp_path):
    """The perfect-foresight and the myopic masters receive the critical
    days, their weights and the firm hours."""
    from test_master_rep_days_once import _run_foresight, _run_myopic

    demand, _, _ = _year()
    two_years = np.vstack([demand, demand])
    orch = _orch(tmp_path, representative_days=5, min_day_separation=5, critical_days=2)
    captured = _run_foresight(orch, two_years, [2025, 2026])
    assert DARK * DAY in captured["critical_day_offsets"]
    assert len(captured["rep_day_weights"]) == 7
    assert captured["storage_firm_hours"] == 13.0
    orch = _orch(tmp_path, representative_days=5, min_day_separation=5, critical_days=2,
                 planning_mode="myopic")
    for kw in _run_myopic(orch, two_years, [2025, 2026]):
        assert DARK * DAY in kw["critical_day_offsets"]
        assert kw["storage_firm_hours"] == 13.0


def test_config_defaults_and_gui_round_trip():
    from esfex.visualization.data.serializer import (
        config_to_global_settings,
        global_settings_to_config_dict,
    )

    mp = MasterProblemConfig()
    assert mp.critical_days == 3 and mp.storage_firm_hours is None
    with pytest.raises(ValueError):
        MasterProblemConfig(critical_days=-1)
    with pytest.raises(ValueError):
        MasterProblemConfig(storage_firm_hours=0.0)

    base = _make_esfex_config(_make_system_config())
    for crit, hours in ((0, None), (4, 10.5)):
        cfg = base.model_copy(update={"master_problem": MasterProblemConfig(
            critical_days=crit, storage_firm_hours=hours)})
        g = config_to_global_settings(cfg)
        assert (g.mp_critical_days, g.mp_storage_firm_hours) == (crit, hours or 0.0)
        out: dict = {}
        global_settings_to_config_dict(g, out)
        mp_out = out["master_problem"]
        assert (mp_out["critical_days"], mp_out["storage_firm_hours"]) == (crit, hours)
        assert MasterProblemConfig.model_validate(mp_out).storage_firm_hours == hours
