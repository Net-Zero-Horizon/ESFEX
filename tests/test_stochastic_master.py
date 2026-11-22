"""Two-stage stochastic master: each scenario operates on its own demand.

Scenarios used to share the deterministic operation and only rescale its
cost, so a scenario with growing demand left the plan unchanged.
"""

import sys

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator  # noqa: E402
from test_zones import _make_system_config, _make_technology  # noqa: E402

from esfex.config.schema import (  # noqa: E402
    MasterProblemConfig,
    PenaltiesConfig,
    ScenarioMultipliers,
    StochasticScenarioConfig,
    TemporalConfig,
)


def _plan(tmp_path, tag, scenarios=None):
    from esfex.runner import Orchestrator

    demand = tmp_path / "demand.csv"
    np.savetxt(demand, np.full(8760, 80.0))
    diesel = _make_generator("Diesel", "Non-renewable", "Diesel", [60.0],
                             degradation_rate=[0.0], fuel_cost=[80.0], invest_max_power=[0.0])
    gas = _make_technology("gas_new", "Diesel", 1, tech_type="Non-renewable",
                           invest_cost=800000.0, invest_max_power=500.0)
    sysc = _make_system_config(num_nodes=1, coords=[(21.0, -80.0)],
                               generators={"diesel": diesel}, technologies={"gas_new": gas})
    sysc = sysc.model_copy(update={
        "demand_path": str(demand), "penalties": PenaltiesConfig(loss_of_load=10000.0),
        "stochastic_scenarios": scenarios or []})
    cfg = _make_esfex_config(
        sysc, simulation_mode="development",
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720, overlap_hours=0),
        master_problem=MasterProblemConfig(stochastic=bool(scenarios)))
    results = Orchestrator(cfg, output_dir=tmp_path / tag).run(years=2)
    return [r.investments.get("tech_investment_power_0_0", 0.0) for r in results]


@pytest.mark.julia
def test_scenarios_operate_on_their_own_demand(tmp_path):
    deterministic = _plan(tmp_path, "det")
    neutral = _plan(tmp_path, "neutral", [
        StochasticScenarioConfig(name="base", probability=1.0)])
    boom = _plan(tmp_path, "boom", [
        StochasticScenarioConfig(name="base", probability=0.5),
        StochasticScenarioConfig(name="boom", probability=0.5,
                                 multipliers=ScenarioMultipliers(demand_growth=2.0))])

    assert neutral == pytest.approx(deterministic, abs=1e-3)
    # Year 2 of the boom scenario needs twice the demand: more is built.
    assert sum(boom) > sum(deterministic) + 1.0
    assert boom[1] > 0
