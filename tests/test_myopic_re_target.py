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
            return 0   # not optimal: the loop moves on to the next year

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
