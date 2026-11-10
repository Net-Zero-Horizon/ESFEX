"""The master plans with the carbon cost the dispatch will charge.

Its daily operating cost had no CO2 term, and the adapter read the price and
the emission factors from fields that do not exist, so ``co2_cost`` (and the
stochastic ``carbon_price``) never changed the plan.
"""

import sys

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator  # noqa: E402
from test_zones import _make_system_config, _make_technology  # noqa: E402

from esfex.config.schema import (  # noqa: E402
    FuelConfig,
    MasterProblemConfig,
    PenaltiesConfig,
    ScenarioMultipliers,
    StochasticScenarioConfig,
    TemporalConfig,
)


def _gas_built(tmp_path, tag, co2_cost, scenarios=None):
    """MW of a cleaner but dearer gas plant built next to an ample diesel fleet."""
    from esfex.runner import Orchestrator

    demand = tmp_path / "demand.csv"
    np.savetxt(demand, np.full(8760, 60.0))
    diesel = _make_generator("Diesel", "Non-renewable", "Diesel", [150.0],
                             degradation_rate=[0.0], fuel_cost=[80.0])
    # The master pays an annuity (5 %, 25 years): this overnight cost makes
    # it 200 000 $/MW in the one modelled year
    crf = 0.05 * 1.05 ** 25 / (1.05 ** 25 - 1)
    gas = _make_technology("gas_new", "Gas", 1, tech_type="Non-renewable",
                           invest_cost=200000.0 / crf, invest_max_power=500.0)
    gas = gas.model_copy(update={"fuel_cost": [150.0]})
    sysc = _make_system_config(num_nodes=1, coords=[(21.0, -80.0)],
                               generators={"diesel": diesel}, technologies={"gas_new": gas})
    sysc = sysc.model_copy(update={
        "demand_path": str(demand),
        "fuels": {
            "Diesel": FuelConfig(name="Diesel", emission_factor=0.8, price_base=80.0),
            "Gas": FuelConfig(name="Gas", emission_factor=0.2, price_base=150.0),
        },
        "penalties": PenaltiesConfig(loss_of_load=10000.0, co2_cost=co2_cost),
        "stochastic_scenarios": scenarios or []})
    cfg = _make_esfex_config(
        sysc, simulation_mode="development",
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720, overlap_hours=0),
        master_problem=MasterProblemConfig(stochastic=bool(scenarios)))
    results = Orchestrator(cfg, output_dir=tmp_path / tag).run(years=1)
    return results[0].investments.get("tech_investment_power_0_0", 0.0)


@pytest.mark.julia
def test_carbon_cost_drives_the_plan(tmp_path):
    # Diesel 95 $/MWh vs gas 165 $/MWh: gas only pays off once the 0.6 t/MWh
    # of CO2 it saves is priced at 200 $/t.
    assert _gas_built(tmp_path, "free", 0.0) == pytest.approx(0.0, abs=1e-3)
    assert _gas_built(tmp_path, "priced", 200.0) > 1.0


@pytest.mark.julia
def test_carbon_price_multiplier_reaches_the_plan(tmp_path):
    scenario = [StochasticScenarioConfig(
        name="dear", probability=1.0, multipliers=ScenarioMultipliers(carbon_price=4.0))]
    assert _gas_built(tmp_path, "base", 50.0) == pytest.approx(0.0, abs=1e-3)
    assert _gas_built(tmp_path, "dear", 50.0, scenario) > 1.0
