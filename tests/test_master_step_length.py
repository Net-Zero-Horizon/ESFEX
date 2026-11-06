"""New batteries in the master store MW × step length, not MW.

The state-of-charge balance of battery technologies left out the step length,
so with ``resolution_hours > 1`` a MWh of storage shifted several MWh and the
master under-sized the energy it builds.
"""

import sys

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator  # noqa: E402
from test_zones import _make_battery_technology, _make_system_config  # noqa: E402

from esfex.config.schema import MasterProblemConfig, PenaltiesConfig, TemporalConfig  # noqa: E402


def _battery_built(tmp_path, resolution_hours):
    """(MW, MWh) of storage built to shift 20 MW over 12 h of a 40/80 MW day."""
    from esfex.runner import Orchestrator

    demand = tmp_path / f"demand_{resolution_hours}.csv"
    np.savetxt(demand, np.tile(np.r_[np.full(12, 40.0), np.full(12, 80.0)], 365))
    gens = {
        "base": _make_generator("Base", "Non-renewable", "Gas", [60.0], degradation_rate=[0.0],
                                fuel_cost=[50.0], invest_max_power=[0.0], min_power=[0.0]),
        "peak": _make_generator("Peak", "Non-renewable", "Diesel", [100.0], degradation_rate=[0.0],
                                fuel_cost=[500.0], invest_max_power=[0.0], min_power=[0.0]),
    }
    bt = _make_battery_technology("li", 1, invest_max_power=100.0, invest_max_capacity=2000.0)
    bt = bt.model_copy(update={
        "invest_cost_power": [1000.0], "invest_cost_energy": [1000.0],
        "efficiency_charge": [1.0], "efficiency_discharge": [1.0],
        "soc_initial": [0.0], "max_DoD": [1.0]})
    sysc = _make_system_config(num_nodes=1, coords=[(21.0, -80.0)], generators=gens,
                               battery_technologies={"li": bt})
    sysc = sysc.model_copy(update={"demand_path": str(demand),
                                   "penalties": PenaltiesConfig(loss_of_load=10000.0)})
    cfg = _make_esfex_config(
        sysc, simulation_mode="development",
        temporal=TemporalConfig(resolution_hours=resolution_hours,
                                rolling_horizon_hours=720, overlap_hours=0),
        master_problem=MasterProblemConfig())
    inv = Orchestrator(cfg, output_dir=tmp_path / f"out_{resolution_hours}").run(years=1)[0].investments
    return (inv.get("bat_tech_investment_power_0_0", 0.0),
            inv.get("bat_tech_investment_capacity_0_0", 0.0))


@pytest.mark.julia
def test_battery_energy_does_not_depend_on_step_length(tmp_path):
    # 20 MW × 12 h = 240 MWh whether the day has 24 steps or 4
    for resolution_hours in (1, 6):
        power, energy = _battery_built(tmp_path, resolution_hours)
        assert power == pytest.approx(20.0, abs=1e-3)
        assert energy == pytest.approx(240.0, abs=1e-2)
