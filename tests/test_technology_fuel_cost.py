"""Candidate technologies pay the fuel priced in the ``fuels`` block.

The loader filled ``fuel_cost`` from ``fuels`` only for generators, so a
technology with ``fuel: Fuel_oil`` and no ``fuel_cost`` was built by the
master as free generation and its units ran in the dispatch at no fuel cost
(Cuba: 2,538 MW of fuel-oil engines). The loader rule itself is tested in
test_loader_extra.py (TestTechnologyFuelLinking); this runs a plan end to end.
"""

import logging
import sys

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config  # noqa: E402

from esfex.config.loader import _convert_system  # noqa: E402
from esfex.config.schema import MasterProblemConfig, TemporalConfig  # noqa: E402
from esfex.runner import Orchestrator  # noqa: E402

_TEMPORAL = TemporalConfig(resolution_hours=24, rolling_horizon_hours=720, overlap_hours=0)

# Fuel oil at 405 $/t, 11.2 MWh/t, burnt at 42 %: 86.1 $/MWh
_ENGINE_FUEL_COST = 405.0 / 11.2 / 0.42
_DIESEL_FUEL_COST = 80.0


def _system(demand_path: str):
    """One node, 60 MW of load, 30 MW of diesel at 80 $/MWh and cheap
    fuel-oil engines to build (no fuel_cost given)."""
    return _convert_system({
        "name": "Island",
        "demand_path": demand_path,
        "target_re_penetration": 0.0,
        "reserve_static_default_ratio": 0.0,
        "nodes": {
            "nodes_connections": [0.0],
            "reserve_static": [0.0],
            "reserve_dynamic": [0.0],
            "reserve_duration": [1.0],
            "losses": [0.0],
            "transference_invest_cost": [0.0],
            "transference_invest_max": [0.0],
        },
        "penalties": {"loss_of_load": 10000.0},
        "fuels": {"Fuel_oil": {"name": "Fuel oil", "unit": "t", "emission_factor": 0.279,
                               "energy_content": 11.2, "price_base": 405.0}},
        "generators": {"diesel": {
            "name": "Diesel", "type": "Non-renewable", "fuel": "Diesel",
            "life_time": [40], "initial_age": [0], "degradation_rate": [0.0],
            "decommissioning_cost": [0.0], "rated_power": [30.0], "min_power": [0.0],
            "min_up": [0], "min_down": [0], "ramp_up": [1.0], "ramp_down": [1.0],
            "eff_at_rated": [0.35], "eff_at_min": [0.35], "inertia": [0.0],
            "start_up_cost": [0.0], "fuel_cost": [_DIESEL_FUEL_COST], "fixed_cost": [0.0],
            "maintenance_cost": [0.0], "invest_cost": [0.0]}},
        "technologies": {"engines_new": {
            "type": "Non-renewable", "fuel": "Fuel_oil", "invest_cost": [1000.0],
            "invest_max_power": [200.0], "eff_at_rated": [0.42], "degradation_rate": [0.0],
            "lifetime": 25}},
    })


@pytest.mark.julia
def test_built_engines_pay_their_fuel(tmp_path, caplog):
    demand = tmp_path / "demand.csv"
    np.savetxt(demand, np.full(8760, 60.0))
    sysc = _system(str(demand))
    assert sysc.technologies["engines_new"].fuel_cost == pytest.approx([_ENGINE_FUEL_COST])
    cfg = _make_esfex_config(sysc, simulation_mode="development", temporal=_TEMPORAL,
                             master_problem=MasterProblemConfig())
    with caplog.at_level(logging.WARNING, logger="esfex.bridge.adapters"):
        res = Orchestrator(cfg, output_dir=tmp_path / "out").run(years=1)[0]
    # A thermal technology without a profile is simply always available:
    # no warning (one was logged for each unit in every dispatch window)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING
                and "Virtual gen" in r.getMessage()]

    built = res.investments.get("tech_investment_power_0_0", 0.0)
    # Built: the 30 MW the diesel cannot cover plus the adequacy margin
    assert 29.0 < built < 60.0
    # The diesel (80 $/MWh) is cheaper to run than the engines (86 $/MWh):
    # it runs flat out and the engines make the rest. Free engines made
    # everything and the run paid no fuel at all.
    hours = 8760.0
    fuel = res.cost_breakdown["fuel_cost"]
    expected = 30.0 * hours * (_DIESEL_FUEL_COST + _ENGINE_FUEL_COST)
    assert fuel == pytest.approx(expected, rel=0.02)
    # Their CO2 (0.279 t per MWh of fuel / 0.42) reaches the dispatch too;
    # the diesel has no fuels entry, so it emits nothing here.
    assert res.emissions == pytest.approx(
        30.0 * hours * 0.279 / 0.42, rel=0.02)
