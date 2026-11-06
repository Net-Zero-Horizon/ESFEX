"""Benders through the runner plans like the monolithic master.

The runner's debug LP export raised for Benders (no single model), a one-node
one-step day broke the subproblem's curtailment expression, and the
subproblems capped renewable output at the master's RE ratio, so with a 0 %
target no renewable could be used and none was built.
"""

import sys

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator  # noqa: E402
from test_zones import _make_system_config, _make_technology  # noqa: E402

from esfex.config.schema import MasterProblemConfig, PenaltiesConfig, TemporalConfig  # noqa: E402


def _solar_built(tmp_path, method):
    from esfex.runner import Orchestrator

    demand = tmp_path / f"demand_{method}.csv"
    np.savetxt(demand, np.full(8760, 60.0))
    diesel = _make_generator("Diesel", "Non-renewable", "Diesel", [100.0], degradation_rate=[0.0],
                             fuel_cost=[200.0], invest_max_power=[0.0], min_power=[0.0])
    solar = _make_technology("solar_new", "Sun", 1, tech_type="Renewable",
                             invest_cost=20000.0, invest_max_power=50.0)
    sysc = _make_system_config(num_nodes=1, coords=[(21.0, -80.0)],
                               generators={"diesel": diesel}, technologies={"solar_new": solar})
    sysc = sysc.model_copy(update={"demand_path": str(demand),
                                   "penalties": PenaltiesConfig(loss_of_load=10000.0)})
    cfg = _make_esfex_config(
        sysc, simulation_mode="development",
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720, overlap_hours=0),
        master_problem=MasterProblemConfig(solver_method=method))
    results = Orchestrator(cfg, output_dir=tmp_path / method).run(years=1)
    return results[0].investments.get("tech_investment_power_0_0", 0.0)


@pytest.mark.julia
def test_benders_builds_what_the_monolithic_master_builds(tmp_path):
    mono = _solar_built(tmp_path, "monolithic")
    assert mono == pytest.approx(50.0, abs=1e-3)
    assert _solar_built(tmp_path, "benders") == pytest.approx(mono, abs=0.5)
