"""Flexible demand sectors shift energy in the dispatch.

The shift variables sat outside the power balance, so ``is_flexible`` sectors
changed nothing but the objective (through a curtailment "benefit" that did not
reduce any demand).
"""

import sys

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator  # noqa: E402
from test_zones import _make_system_config  # noqa: E402

from esfex.config.schema import DemandSectorConfig, PenaltiesConfig, TemporalConfig  # noqa: E402


def _peaker_mwh(tmp_path, flexible):
    from esfex.runner import Orchestrator

    demand = tmp_path / f"demand_{flexible}.csv"
    # 100 MW for 12 h, 60 MW for 12 h; 40 % of it is the "flex" sector
    np.savetxt(demand, np.tile(np.r_[np.full(12, 100.0), np.full(12, 60.0)], 365))
    gens = {
        "base": _make_generator("Base", "Non-renewable", "Gas", [80.0], degradation_rate=[0.0],
                                fuel_cost=[50.0], min_power=[0.0], start_up_cost=[0.0]),
        "peak": _make_generator("Peak", "Non-renewable", "Diesel", [100.0], degradation_rate=[0.0],
                                fuel_cost=[500.0], min_power=[0.0], start_up_cost=[0.0]),
    }
    sysc = _make_system_config(num_nodes=1, coords=[(21.0, -80.0)], generators=gens)
    sysc = sysc.model_copy(update={
        "demand_path": str(demand),
        "electric_demand": {
            "flex": DemandSectorConfig(is_flexible=flexible, flexibility_ratio=0.5, delay_tolerance=12),
            "rest": DemandSectorConfig(),
        },
        "sector_distribution": {0: {"flex": 0.4, "rest": 0.6}},
        "penalties": PenaltiesConfig(loss_of_load=10000.0)})
    cfg = _make_esfex_config(
        sysc, simulation_mode="economic_dispatch",
        temporal=TemporalConfig(resolution_hours=6, rolling_horizon_hours=720, overlap_hours=0))
    res = Orchestrator(cfg, output_dir=tmp_path / f"out_{flexible}").run(years=1)[0]
    return float(np.sum(res.gen_output[1])) * 6, float(np.sum(res.gen_output)) * 6


@pytest.mark.julia
def test_flexible_sector_flattens_the_peak(tmp_path):
    rigid_peak, rigid_total = _peaker_mwh(tmp_path, False)
    flex_peak, flex_total = _peaker_mwh(tmp_path, True)
    # Without shifting the peaker covers 20 MW for 12 h a day; shifting
    # 20 MW of the flex sector (half of its 40 MW) to the night flattens
    # the load at 80 MW, all of it from the base unit.
    assert rigid_peak == pytest.approx(20.0 * 12 * 365, rel=1e-3)
    assert flex_peak == pytest.approx(0.0, abs=1.0)
    # Energy is moved, not removed
    assert flex_total == pytest.approx(rigid_total, rel=1e-6)
