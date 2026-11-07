"""Reservoir capacity built by the master reaches the dispatch.

``MasterProblemResult.reservoir_investment`` was extracted in Julia but never
read by Python: a reservoir the master paid for never reached the dispatch,
nor the next myopic year's master.
"""

import sys
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_myopic_master_history import _system  # noqa: E402
from test_runner import _make_esfex_config  # noqa: E402

from esfex.config.schema import MasterProblemConfig  # noqa: E402
from esfex.runner import Orchestrator, _reservoir_investment_key  # noqa: E402
from esfex.utils.temporal import HOURS_STD_YEAR  # noqa: E402


def test_reservoir_investment_key():
    assert _reservoir_investment_key("reservoir_investment_2_5") == (2, 5)
    assert _reservoir_investment_key("converter_investment_acdc_0") is None
    assert _reservoir_investment_key("reservoir_investment_x_1") is None


def test_adapter_reports_reservoir_builds_per_year():
    from esfex.bridge.adapters import MasterProblemAdapter

    def per_year(value):
        return {1: value, 2: {}}

    empty = {1: {}, 2: {}}
    jl = SimpleNamespace(
        tech_investment=empty, bat_tech_power_investment=empty,
        bat_tech_capacity_investment=empty, transfer_investment=empty,
        converter_investment=empty,
        reservoir_investment=per_year({1: [0.0, 40.0]}),
        re_penetration_by_year=[0.0, 0.0], gen_life_extension=empty,
        bat_life_extension=empty, cumulative_gen_capacity=empty,
        cumulative_bat_capacity=empty, cumulative_bat_power=empty,
        cumulative_tech_capacity=empty, cumulative_bat_tech_power=empty,
        cumulative_bat_tech_capacity=empty, status="OPTIMAL", objective=0.0,
        solve_time=0.0, total_investment_by_year=[0.0, 0.0],
        total_operational_cost_by_year=[0.0, 0.0], re_penetration_by_system={})
    adapter = object.__new__(MasterProblemAdapter)
    adapter.years = [2025, 2026]
    adapter.system_config = SimpleNamespace(num_nodes=2, buses=[])
    adapter._reduction_map = None
    sol = adapter._convert_single_result(jl)["solution"]
    assert sol[2025]["reservoir_investment"] == {"reservoir_investment_0_1": 40.0}
    assert sol[2026]["reservoir_investment"] == {}


def _hydro_system():
    s = _system("Island")
    diesel = s.generators["diesel"].model_copy(update={
        "reservoir_capacity": [100.0], "reservoir_invest_max": [200.0]})
    return s.model_copy(update={"generators": {"diesel": diesel}})


def test_built_reservoir_is_existing_capacity_for_the_dispatch(tmp_path):
    orch = Orchestrator(_make_esfex_config(_hydro_system()), output_dir=tmp_path)
    units = orch._build_config_from_cumulative(
        orch._gather_units(), {"reservoir": {(0, 0): 30.0}})
    assert units["diesel"]["reservoir_capacity"] == [130.0]
    # The base system is untouched
    assert orch.primary_system.generators["diesel"].reservoir_capacity == [100.0]


def test_myopic_years_carry_reservoir_builds(tmp_path):
    cfg = _make_esfex_config(_hydro_system(),
                             master_problem=MasterProblemConfig(planning_mode="myopic"))
    orch = Orchestrator(cfg, output_dir=tmp_path)
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
            year = captured[-1]["years"][0]
            first = len(captured) == 1
            return {
                "solution": {year: {"reservoir_investment": (
                    {"reservoir_investment_0_0": 30.0} if first else {})}},
                "cumulative_gen_capacity": {1: {1: np.array([50.0])}},
                "re_penetration_by_year": [0.0],
            }

    demand = np.full((2 * HOURS_STD_YEAR, 1), 10.0)
    with patch("esfex.bridge.adapters.MasterProblemAdapter", FakeMaster):
        inv, _, caps, _ = orch._solve_master_problem(
            years_range=[2025, 2026], demand=demand[:HOURS_STD_YEAR],
            hours=HOURS_STD_YEAR, num_nodes=1, total_demand=demand)

    assert inv[2025] == {"reservoir_investment_0_0": 30.0}
    # Year 2's master holds the reservoir as existing, with less room left
    hydro2 = captured[1]["system_config"].generators["diesel"]
    assert hydro2.reservoir_capacity == [130.0]
    assert hydro2.reservoir_invest_max == [170.0]
    # The dispatch sees it in both years
    assert caps[2025]["reservoir"] == {(0, 0): pytest.approx(30.0)}
    assert caps[2026]["reservoir"] == {(0, 0): pytest.approx(30.0)}
