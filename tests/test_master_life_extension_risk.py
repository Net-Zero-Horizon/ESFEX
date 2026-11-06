"""Capacity kept past its lifetime counts at its hazard-derated value.

The master's capacity-adequacy constraint (CAP-1) derated existing units by
``risk_coefficient`` while they were within their lifetime, but counted
life-extended capacity at full rating, so extending an exposed unit looked
firmer than keeping it.
"""

import sys

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator  # noqa: E402
from test_zones import _make_system_config, _make_technology  # noqa: E402

from esfex.config.schema import MasterProblemConfig, PenaltiesConfig, TemporalConfig  # noqa: E402


def _new_capacity_built(tmp_path, risk):
    """MW of new gas built next to a 100 MW unit past its lifetime (80 MW load)."""
    from esfex.runner import Orchestrator

    demand = tmp_path / f"demand_{risk}.csv"
    np.savetxt(demand, np.full(8760, 80.0))
    # Past its lifetime and cheap to extend; derated by its hazard exposure
    old = _make_generator("Old", "Non-renewable", "Diesel", [100.0], life_time=[25],
                          initial_age=[30], degradation_rate=[0.0], invest_cost=[10000.0],
                          invest_max_power=[0.0], risk_coefficient=[risk])
    gas = _make_technology("gas_new", "Gas", 1, tech_type="Non-renewable",
                           invest_cost=50000.0, invest_max_power=500.0)
    # Dear to run: built only to meet the adequacy requirement
    gas = gas.model_copy(update={"fuel_cost": [1000.0]})
    sysc = _make_system_config(num_nodes=1, coords=[(21.0, -80.0)],
                               generators={"old": old}, technologies={"gas_new": gas})
    sysc = sysc.model_copy(update={"demand_path": str(demand),
                                   "penalties": PenaltiesConfig(loss_of_load=10000.0)})
    cfg = _make_esfex_config(
        sysc, simulation_mode="development",
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720, overlap_hours=0),
        master_problem=MasterProblemConfig())
    inv = Orchestrator(cfg, output_dir=tmp_path / f"out_{risk}").run(years=1)[0].investments
    return inv.get("tech_investment_power_0_0", 0.0)


@pytest.mark.julia
def test_life_extended_capacity_is_derated_by_risk(tmp_path):
    # Fully firm, the extended 100 MW cover the 92 MW requirement
    assert _new_capacity_built(tmp_path, 1.0) == pytest.approx(0.0, abs=1e-3)
    # Half-exposed, it counts 50 MW: 42 MW of new capacity are needed
    assert _new_capacity_built(tmp_path, 0.5) == pytest.approx(42.0, abs=1e-2)
