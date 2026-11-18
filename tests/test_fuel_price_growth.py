"""``fuels.<f>.price_growth_rate`` grows the fuel price in every mode.

It used to act only on the primary-energy fuel balance: the flat
``fuel_cost`` the loader derives from the fuels block (or a unit burning a
fuel of that block) never grew, in the dispatch or in the master.
"""

from __future__ import annotations

import sys
from pathlib import Path
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_master_rep_days_once import _demand, _orch, _run_foresight, _run_myopic  # noqa: E402
from test_runner import _make_generator, _make_node_config  # noqa: E402

from esfex.config.schema import FuelConfig, SystemConfig, fuel_price_factor  # noqa: E402

GAS = FuelConfig(name="Gas", emission_factor=0.2, price_base=10.0, price_growth_rate=0.1)


def test_fuel_price_factor():
    fuels = {"Gas": GAS, "Oil": FuelConfig(name="Oil", emission_factor=0.3, price_base=5.0)}
    assert fuel_price_factor(fuels, "Gas", 2027, 2025) == pytest.approx(1.21)
    assert fuel_price_factor(fuels, "Gas", 2025, 2025) == 1.0
    assert fuel_price_factor(fuels, "Oil", 2027, 2025) == 1.0      # no growth
    assert fuel_price_factor(fuels, "Diesel", 2027, 2025) == 1.0   # not in fuels
    assert fuel_price_factor(None, "Gas", 2027, 2025) == 1.0


def _system() -> SystemConfig:
    return SystemConfig(
        name="f", nodes=_make_node_config(1), fuels={"Gas": GAS},
        generators={"gas": _make_generator("Gas", "Non-renewable", "Gas", [40.0],
                                           fuel_cost=[50.0]),
                    "oil": _make_generator("Oil", "Non-renewable", "Oil", [40.0],
                                           fuel_cost=[60.0])})


@pytest.mark.julia
def test_dispatch_grows_the_fuel_cost_of_units_burning_a_growing_fuel():
    from esfex.bridge.adapters import PowerSystemAdapter

    # A unit the master built from a gas technology (virtual generator)
    ocgt = {"name": "OCGT", "type": "Non-renewable", "fuel": "Gas",
            "rated_power": [20.0], "fuel_cost": [80.0]}

    def fuel_costs(year):
        ps = PowerSystemAdapter(config=_system(), demand=np.full((2, 1), 30.0), hours=2,
                                num_nodes=1, year=year, base_year=2025,
                                mode="economic_dispatch", units_config={"ocgt": ocgt})
        ps.build_model()
        return {str(g.name): float(g.fuel_cost[0]) for g in ps._jl_input.generators}

    base, later = fuel_costs(2025), fuel_costs(2027)
    assert later["Gas"] == pytest.approx(1.21 * base["Gas"])
    assert later["OCGT"] == pytest.approx(1.21 * base["OCGT"])
    assert later["Oil"] == pytest.approx(base["Oil"])   # its fuel is not in fuels


@pytest.mark.julia
def test_master_input_carries_the_fuel_growth():
    from esfex.bridge.adapters import MasterProblemAdapter

    master = MasterProblemAdapter(config=_system(), years=[2027], base_year=2027,
                                  demand=np.full((24, 1), 30.0), fuel_price_base_year=2025)
    jl_input = master._create_input()
    assert dict(jl_input.fuel_price_growth) == {"Gas": pytest.approx(0.1)}
    assert jl_input.fuel_price_base_year == 2025
    # Default: the master's own base year
    master = MasterProblemAdapter(config=_system(), years=[2025], base_year=2025,
                                  demand=np.full((24, 1), 30.0))
    assert master._create_input().fuel_price_base_year == 2025


@pytest.mark.julia
def test_masters_grow_fuel_prices_from_the_runs_first_year(tmp_path):
    """Each myopic year's master (base_year = its own year) and the
    foresight master get the run's first year as the fuel price base."""
    demand = _demand([(9, 80.0)])
    captured = _run_myopic(_orch(tmp_path, planning_mode="myopic"), demand,
                           [2025, 2026, 2027])
    assert [c["fuel_price_base_year"] for c in captured] == [2025] * 3
    assert [c["base_year"] for c in captured] == [2025, 2026, 2027]
    captured = _run_foresight(_orch(tmp_path), np.vstack([demand, demand]), [2025, 2026])
    assert captured["fuel_price_base_year"] == 2025


def test_lcoe_charges_the_years_fuel_price(tmp_path):
    from types import SimpleNamespace

    from test_runner import _create_orchestrator

    from esfex.runner import YearResults

    orch = _create_orchestrator(tmp_path=tmp_path)
    orch._num_nodes = 1
    orch._gen_names = ["Gas"]
    gen = SimpleNamespace(
        name="Gas", type="Non-renewable", fuel="Gas", rated_power=[10.0], fuel_cost=[1.0],
        fixed_cost=[0.0], maintenance_cost=[0.0], start_up_cost=[0.0], ramp_up=[1.0])
    orch.primary_system = SimpleNamespace(generators={"g": gen}, batteries={},
                                          fuels={"Gas": GAS})
    orch._current_units_config = {}
    orch.state = SimpleNamespace(base_year=2025)
    res = YearResults(year=2027, objective=0.0, solve_time=0.0, feasible=True,
                      gen_output=np.array([[[5.0, 5.0]]]), prices=np.array([[10.0, 20.0]]))
    orch._compute_derived_metrics(res)
    np.testing.assert_allclose(res.lcoe[0, 0], [1.21, 1.21])
