"""The master charges investment as annuities and reports them.

``MasterProblemResult.annuity_cost_by_year`` (the annuities each year pays,
what the objective charges) reaches the adapter's result next to
``total_investment_by_year`` (the overnight cost of each year's builds, what
the budget limits). The generic ``slack_penalty`` the master no longer uses
is gone from the adapter.
"""

import inspect
from types import SimpleNamespace

import numpy as np

from esfex.bridge.adapters import MasterProblemAdapter


def _jl_result(**extra):
    empty = {1: {}, 2: {}}
    return SimpleNamespace(
        tech_investment=empty, bat_tech_power_investment=empty,
        bat_tech_capacity_investment=empty, transfer_investment=empty,
        converter_investment=empty, re_penetration_by_year=[0.1, 0.2],
        gen_life_extension=empty, bat_life_extension=empty,
        cumulative_gen_capacity=empty, cumulative_bat_capacity=empty,
        cumulative_bat_power=empty, cumulative_tech_capacity=empty,
        cumulative_bat_tech_power=empty, cumulative_bat_tech_capacity=empty,
        status="OPTIMAL", objective=0.0, solve_time=0.0,
        total_investment_by_year=[3.0, 0.0],
        total_operational_cost_by_year=[0.0, 0.0],
        re_penetration_by_system={}, **extra)


def _adapter():
    adapter = object.__new__(MasterProblemAdapter)
    adapter.years = [2025, 2026]
    adapter.system_config = SimpleNamespace(num_nodes=1, buses=[])
    adapter._reduction_map = None
    return adapter


def test_annuities_reach_the_result_in_dollars():
    # M$ in Julia, $ in Python: 3 M$ built in 2025, 0.2 M$ a year of annuity
    out = _adapter()._convert_single_result(_jl_result(annuity_cost_by_year=[0.2, 0.2]))
    assert np.allclose(out["total_investment_by_year"], [3.0e6, 0.0])
    assert np.allclose(out["annuity_cost_by_year"], [2.0e5, 2.0e5])


def test_result_without_annuities_still_converts():
    out = _adapter()._convert_single_result(_jl_result())
    assert len(out["annuity_cost_by_year"]) == 0


def test_slack_penalty_removed():
    assert "slack_penalty" not in inspect.signature(MasterProblemAdapter.__init__).parameters

