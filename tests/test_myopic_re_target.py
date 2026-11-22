"""Myopic masters follow the perfect-foresight RE trajectory.

Each one-year master used its year's initial share (last year's outcome) as
the target, so TARGET_RE_PENETRATION never entered a myopic run.
"""

import sys
from unittest.mock import patch

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import (  # noqa: E402
    _create_orchestrator, _make_esfex_config, _make_generator, _make_system_config)

from esfex.config.schema import MasterProblemConfig  # noqa: E402
from esfex.runner import re_target_trajectory  # noqa: E402
from esfex.utils.temporal import HOURS_STD_YEAR  # noqa: E402


def test_trajectory_is_linear_with_clamped_steps():
    assert re_target_trajectory(0.1, 0.5, 5, 0.0, 1.0) == pytest.approx(
        [0.1, 0.2, 0.3, 0.4, 0.5])
    # Steps of 0.2 clamped to 0.05: the path ends below the target
    assert re_target_trajectory(0.0, 0.6, 4, 0.01, 0.05) == pytest.approx(
        [0.0, 0.05, 0.10, 0.15])
    # A step below the minimum is raised to it
    assert re_target_trajectory(0.4, 0.42, 3, 0.05, 1.0) == pytest.approx(
        [0.4, 0.45, 0.50])
    assert re_target_trajectory(0.3, 0.8, 1, 0.0, 1.0) == [0.3]


@pytest.mark.julia
def test_trajectory_matches_the_julia_master():
    from esfex.bridge.julia_setup import get_esfex_module

    ESFEX = get_esfex_module()
    for init, target, years, lo, hi in [(0.1, 0.9, 6, 0.01, 0.10),
                                        (0.0, 0.3, 4, 0.12, 1.0),
                                        (0.2, 0.5, 3, 0.0, 1.0)]:
        jl, prev = [], 0.0
        for y in range(1, years + 1):
            prev = float(ESFEX._compute_year_target(y, years, init, target, lo, hi, prev))
            jl.append(prev)
        assert re_target_trajectory(init, target, years, lo, hi) == pytest.approx(jl)


def test_each_myopic_year_gets_its_trajectory_target(tmp_path):
    # Gas only: a renewable without a profile would count at full capacity
    gas = _make_generator("Gas", "Non-renewable", "Gas", [100.0, 50.0])
    sysc = _make_system_config(generators={"gas": gas}).model_copy(update={
        "target_re_penetration": 0.6, "min_annual_increment": 0.0,
        "max_annual_increment": 1.0})
    cfg = _make_esfex_config(sysc, master_problem=MasterProblemConfig(
        planning_mode="myopic"))
    orch = _create_orchestrator(cfg, tmp_path=tmp_path)
    orch._availability_cache = {}
    captured = []

    class FakeMaster:
        def __init__(self, **kwargs):
            captured.append(kwargs)

        def build_model(self, **_):
            pass

        def solve(self):
            return 1

        def get_solution_values(self):
            return {"solution": {}, "re_penetration_by_year": [0.0]}

    demand = np.full((4 * HOURS_STD_YEAR, 2), 10.0)
    with patch("esfex.bridge.adapters.MasterProblemAdapter", FakeMaster):
        orch._solve_master_problem_myopic(
            years_range=[2025, 2026, 2027, 2028], demand=demand[:HOURS_STD_YEAR],
            hours=HOURS_STD_YEAR, num_nodes=2, total_demand=demand)
    # No renewables: initial share 0, linear to 0.6 over four years
    assert [c["re_target_override"] for c in captured] == [
        {(1, 1): pytest.approx(v)} for v in (0.0, 0.2, 0.4, 0.6)]
    # Each one-year master reads its own year's block of multi-year profiles
    assert [c["profile_start_hour"] for c in captured] == [
        k * HOURS_STD_YEAR for k in range(4)]


def test_trajectory_never_asks_for_more_than_100_percent():
    # The minimum step pushes the path past the target and past 100 %
    assert re_target_trajectory(0.6, 1.0, 3, 0.3, 1.0) == pytest.approx([0.6, 0.9, 1.0])


def _gas_only_myopic(tmp_path, solve_status, re_achieved=1.0748):
    """Four myopic years on a stand-in master; returns the run's result and
    the keyword arguments each year's master got."""
    gas = _make_generator("Gas", "Non-renewable", "Gas", [100.0, 50.0])
    sysc = _make_system_config(generators={"gas": gas}).model_copy(update={
        "target_re_penetration": 0.6, "min_annual_increment": 0.01,
        "max_annual_increment": 0.10})
    cfg = _make_esfex_config(sysc, master_problem=MasterProblemConfig(
        planning_mode="myopic"))
    orch = _create_orchestrator(cfg, tmp_path=tmp_path)
    orch._availability_cache = {}
    captured = []

    class FakeMaster:
        _last_status = "INFEASIBLE_OR_UNBOUNDED"

        def __init__(self, **kwargs):
            captured.append(kwargs)

        def build_model(self, **_):
            pass

        def solve(self):
            return solve_status(len(captured))

        def get_solution_values(self):
            # Renewables charging storage: the achieved share passes 100 %
            return {"solution": {}, "re_penetration_by_year": np.array([re_achieved])}

        def conflict(self):
            return ["re_min_increment_s1_y1 : re_ratio_s1_y1 >= 1.08", "re_ratio_s1_y1 <= 1"]

    demand = np.full((4 * HOURS_STD_YEAR, 2), 10.0)
    with patch("esfex.bridge.adapters.MasterProblemAdapter", FakeMaster):
        out = orch._solve_master_problem_myopic(
            years_range=[2025, 2026, 2027, 2028], demand=demand[:HOURS_STD_YEAR],
            hours=HOURS_STD_YEAR, num_nodes=2, total_demand=demand)
    return out, captured


def test_increment_bounds_follow_the_perfect_foresight_chain(tmp_path):
    """Each year's RE-3 anchor was the share reached the year before. Above
    100 % (Cuba: 107 % in 2027) the next master had to reach 108 % with a
    share capped at 1, and every later year was infeasible in presolve."""
    out, captured = _gas_only_myopic(tmp_path, lambda _: 1)
    assert out[0] is not None and len(captured) == 4
    # No renewables: initial share 0, then one minimum step per year
    assert [c["initial_re_penetration"] for c in captured] == pytest.approx(
        [0.0, 0.01, 0.02, 0.03])


def test_max_increment_is_measured_from_the_share_achieved(tmp_path):
    """The maximum increment capped only a credited share, never the
    renewable energy: each myopic year now caps the growth over the share
    the year before achieved (year 1: the initial share), clamped to 1."""
    out, captured = _gas_only_myopic(tmp_path, lambda _: 1, re_achieved=0.34)
    assert out[0] is not None
    assert [c["re_share_anchor"] for c in captured] == [
        pytest.approx([0.0]), pytest.approx([0.34]), pytest.approx([0.34]),
        pytest.approx([0.34])]
    (tmp_path / "over").mkdir()
    _, captured = _gas_only_myopic(tmp_path / "over", lambda _: 1)
    assert [c["re_share_anchor"] for c in captured][1:] == [[1.0]] * 3


def test_myopic_re_anchor_stops_at_100_percent():
    from esfex.runner import myopic_re_anchor

    assert myopic_re_anchor(0.95, 0.02, 2) == pytest.approx(0.99)
    assert myopic_re_anchor(0.95, 0.02, 3) == 1.0
    assert myopic_re_anchor(1.07, 0.01, 0) == 1.0


def _wind_island_myopic(tmp_path, max_inc):
    """Two myopic years of an island with cheap wind; returns each year's
    solve status and achieved RE share."""
    from test_myopic_master_history import _system
    from test_zones import _make_technology

    from esfex.bridge.adapters import MasterProblemAdapter
    from esfex.config.schema import TemporalConfig

    wind = _make_technology("Wind", "Wind", 1, invest_cost=300000.0)
    sysc = _system("Island", tmp_path)
    sysc = sysc.model_copy(update={
        "technologies": {**sysc.technologies, "wind": wind},
        "target_re_penetration": 0.2, "min_annual_increment": 0.01,
        "max_annual_increment": max_inc})
    cfg = _make_esfex_config(
        sysc, simulation_mode="development",
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720, overlap_hours=0),
        master_problem=MasterProblemConfig(planning_mode="myopic"))
    statuses, shares = [], []
    solve = MasterProblemAdapter.solve

    def recording_solve(self):
        statuses.append(solve(self))
        if statuses[-1] == 1:
            shares.append(float(np.atleast_1d(
                self.get_solution_values()["re_penetration_by_year"])[0]))
        return statuses[-1]

    orch = _create_orchestrator(cfg, tmp_path=tmp_path / "out")
    with patch.object(MasterProblemAdapter, "solve", recording_solve):
        orch.run(years=2, master_only=True)
    assert orch.master_solution is not None
    return statuses, shares


@pytest.mark.julia
def test_myopic_year_after_a_fully_renewable_year_is_optimal(tmp_path):
    """Year 1 covers the whole load with cheap wind (achieved share 100 %).
    Year 2's minimum increment was anchored there and asked for 101 % of a
    share capped at 100 %: the master was infeasible."""
    statuses, shares = _wind_island_myopic(tmp_path, 1.0)
    assert statuses == [1, 1]
    assert shares[0] >= 0.99                       # wind serves all the load


@pytest.mark.julia
def test_myopic_years_grow_at_most_the_max_increment(tmp_path):
    """With cheap wind the first one-year master went from 0 % to 100 %
    renewable: max_annual_increment bounded only a credited share. Each
    year now grows at most 10 points over the share achieved the year
    before."""
    statuses, shares = _wind_island_myopic(tmp_path, 0.10)
    assert statuses == [1, 1]
    assert shares[0] <= 0.10 + 1e-6
    assert shares[1] <= shares[0] + 0.10 + 1e-6
    assert shares[1] > 0.15                        # cheap wind still grows


def test_a_year_that_is_not_optimal_stops_the_run(tmp_path, caplog):
    """The run used to log the failure and go on: later years planned from
    a fleet without that year's builds and the dispatch ran on the stale
    plan. It now stops, saying why."""
    caplog.set_level("ERROR", logger="esfex.runner")
    out, captured = _gas_only_myopic(tmp_path, lambda n: 1 if n < 2 else -1)
    assert out == (None, None, None, None)
    assert len(captured) == 2                      # no year after the failure
    text = caplog.text
    assert "Myopic year 2026" in text and "INFEASIBLE_OR_UNBOUNDED" in text
    assert "years 2025-2025 were planned" in text
    assert "re_ratio_s1_y1 <= 1" in text
