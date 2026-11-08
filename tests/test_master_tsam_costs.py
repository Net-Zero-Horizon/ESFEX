"""TSAM periods charge the same operating cost as representative days.

The TSAM copy of the daily cost left out the reserve and inertia shortfall
penalties, so with TSAM the master met reserves for free and built nothing
for them.
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
    TemporalConfig,
)


@pytest.mark.julia
@pytest.mark.parametrize("use_tsam", [False, True], ids=["representative_days", "tsam"])
def test_reserve_shortfall_drives_investment(tmp_path, use_tsam):
    from esfex.runner import Orchestrator

    demand = tmp_path / "demand.csv"
    np.savetxt(demand, np.full(8760, 50.0))
    diesel = _make_generator("Diesel", "Non-renewable", "Diesel", [60.0],
                             degradation_rate=[0.0], fuel_cost=[80.0])
    gas = _make_technology("gas_new", "Gas", 1, tech_type="Non-renewable",
                           invest_cost=200000.0, invest_max_power=500.0)
    gas = gas.model_copy(update={"fuel_cost": [150.0]})   # dearer than diesel to run
    sysc = _make_system_config(num_nodes=1, coords=[(21.0, -80.0)],
                               generators={"diesel": diesel}, technologies={"gas_new": gas})
    nodes = sysc.nodes.model_copy(update={"reserve_static": [30.0], "reserve_dynamic": [0.0]})
    sysc = sysc.model_copy(update={
        "demand_path": str(demand), "nodes": nodes, "reserve_static_default_ratio": 0.0,
        "penalties": PenaltiesConfig(loss_of_load=10000.0, loss_of_reserve_static=1000.0)})
    master = (MasterProblemConfig(use_tsam=True, tsam_num_periods=4) if use_tsam
              else MasterProblemConfig())
    cfg = _make_esfex_config(
        sysc, simulation_mode="development",
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720, overlap_hours=0),
        master_problem=master)
    result = Orchestrator(cfg, output_dir=tmp_path / "out").run(years=1)[0]

    # 60 MW of diesel for a 50 MW load leaves 10 MW of headroom; 30 MW of
    # static reserve needs 20 MW more.
    assert result.investments.get("tech_investment_power_0_0", 0.0) == pytest.approx(20.0, abs=0.5)
