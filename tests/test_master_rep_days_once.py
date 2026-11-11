"""Every master of a development run models the same representative days.

Each year picked its own peak-demand days, so the weather sample changed
from year to year and the share the existing renewables give swung without
any capacity change (Cuba myopic: 49 → 59 → 54 → 67 %), pushing some years
over the RE growth cap. The days (or TSAM periods) are now chosen once, from
the horizon-mean demand, and each year uses them with its own demand.
"""

import logging
import sys
from unittest.mock import patch

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import (  # noqa: E402
    _create_orchestrator, _make_esfex_config, _make_generator, _make_system_config)

from esfex.config.schema import MasterProblemConfig  # noqa: E402
from esfex.utils.temporal import HOURS_STD_YEAR  # noqa: E402

DAY = 24


def _demand(peaks):
    """Flat 50 MW on two nodes for len(peaks) years; year k's peak day is
    peaks[k] (0-based) at peak MW, (day, MW) tuples."""
    d = np.full((len(peaks) * HOURS_STD_YEAR, 2), 25.0)
    for k, (day, mw) in enumerate(peaks):
        h = k * HOURS_STD_YEAR + day * DAY
        d[h:h + DAY, :] = mw / 2
    return d


def _orch(tmp_path, **mp):
    gas = _make_generator("Gas", "Non-renewable", "Gas", [100.0, 50.0])
    sysc = _make_system_config(generators={"gas": gas}).model_copy(update={
        "target_re_penetration": 0.0, "min_annual_increment": 0.0,
        "max_annual_increment": 1.0})
    cfg = _make_esfex_config(sysc, master_problem=MasterProblemConfig(
        representative_days=1, min_day_separation=1, **mp))
    orch = _create_orchestrator(cfg, tmp_path=tmp_path)
    orch._availability_cache = {}
    return orch


class _FakeMaster:
    captured: list = []

    def __init__(self, **kwargs):
        _FakeMaster.captured.append(kwargs)

    def build_model(self, **_):
        pass

    def solve(self):
        return 1

    def get_solution_values(self):
        return {"solution": {}, "re_penetration_by_year": [0.0]}


def _run_myopic(orch, demand, years):
    _FakeMaster.captured = []
    with patch("esfex.bridge.adapters.MasterProblemAdapter", _FakeMaster):
        orch._solve_master_problem_myopic(
            years_range=years, demand=demand[:HOURS_STD_YEAR],
            hours=HOURS_STD_YEAR, num_nodes=2, total_demand=demand)
    return _FakeMaster.captured


def _run_foresight(orch, demand, years):
    captured = {}

    def fake_adapter(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop after construction")

    with patch("esfex.runner.MasterProblemAdapter", side_effect=fake_adapter):
        orch._solve_master_problem_foresight(
            years, demand[:HOURS_STD_YEAR], HOURS_STD_YEAR, 2, total_demand=demand)
    return captured


@pytest.mark.julia
def test_myopic_years_share_the_horizon_days(tmp_path, caplog):
    # Each year peaks on a different day; the horizon mean peaks on day 200
    # (year 2's 90 MW day: (50 + 90 + 50) / 3 beats (80 + 50 + 50) / 3)
    demand = _demand([(9, 80.0), (199, 90.0), (299, 60.0)])
    with caplog.at_level(logging.INFO, logger="esfex.runner"):
        captured = _run_myopic(_orch(tmp_path, planning_mode="myopic"), demand,
                               [2025, 2026, 2027])
    assert len(captured) == 3
    assert [c["rep_day_offsets"] for c in captured] == [[199 * DAY]] * 3
    msgs = [r.getMessage() for r in caplog.records
            if "Representative days, the same for every planning year" in r.getMessage()]
    assert len(msgs) == 1 and "19 Jul" in msgs[0]


@pytest.mark.julia
def test_foresight_master_gets_the_horizon_days(tmp_path):
    demand = _demand([(9, 80.0), (199, 90.0)])
    captured = _run_foresight(_orch(tmp_path), demand, [2025, 2026])
    assert captured["rep_day_offsets"] == [199 * DAY]


@pytest.mark.julia
def test_offsets_match_the_julia_selection():
    from esfex.bridge.adapters import master_rep_day_offsets

    demand = _demand([(9, 80.0), (199, 90.0)])
    # One year only: that year's own peak day
    assert master_rep_day_offsets(demand, 1, 1, 1) == [9 * DAY]
    assert master_rep_day_offsets(demand, 2, 1, 1) == [199 * DAY]
    # At 6 h steps the offsets are in steps
    agg = demand.reshape(-1, 6, 2).max(axis=1)
    assert master_rep_day_offsets(agg, 2, 1, 1, resolution_hours=6) == [199 * 4]


def _tsam_demand(years):
    """Each year its own pattern: a different season gets the high load."""
    d = np.full((years * HOURS_STD_YEAR, 2), 20.0)
    for k in range(years):
        h = k * HOURS_STD_YEAR + (40 + 100 * k) * DAY
        d[h:h + 30 * DAY, :] = 40.0 + 5 * k
    return d


def test_myopic_tsam_periods_chosen_once(tmp_path):
    demand = _tsam_demand(3)
    captured = _run_myopic(
        _orch(tmp_path, planning_mode="myopic", use_tsam=True, tsam_num_periods=4),
        demand, [2025, 2026, 2027])
    starts = [c["tsam_period_start_hours"] for c in captured]
    weights = [c["tsam_period_weights"] for c in captured]
    assert len(starts) == 3 and starts[0] == starts[1] == starts[2]
    assert weights[0] == weights[1] == weights[2]
    assert sum(weights[0][0]) == pytest.approx(365)
    assert all(c["rep_day_offsets"] is None for c in captured)


def test_foresight_tsam_periods_chosen_once(tmp_path):
    demand = _tsam_demand(2)
    captured = _run_foresight(_orch(tmp_path, use_tsam=True, tsam_num_periods=4),
                              demand, [2025, 2026])
    s1, s2 = captured["tsam_period_start_hours"]
    # The same days of the year, each in its own year's block
    assert [h + HOURS_STD_YEAR for h in s1] == s2
    w1, w2 = captured["tsam_period_weights"]
    assert w1 == w2
