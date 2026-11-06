"""Every electrolyzer of a system is dispatched, and its results are kept.

Only the first electrolyzer reached the dispatch model, and electrolyzer power
was neither kept in YearResults nor written to the HDF5 file.
"""

import sys

import h5py
import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator  # noqa: E402
from test_zones import _make_system_config  # noqa: E402

from esfex.config.schema import ElectrolyzerConfig, TemporalConfig  # noqa: E402


def _elz(name, rated):
    # Negative variable cost: a paying offtake, so each runs at rated power.
    return ElectrolyzerConfig(
        name=name, life_time=[20], initial_age=[0], degradation_rate=[0.0],
        rated_power=[rated], min_power=[0.0], ramp_up=[0.0], ramp_down=[0.0],
        eff_at_rated=[0.7], eff_at_min=[0.7], energy_per_kg_h2=50.0,
        fixed_cost=[0.0], variable_cost=[-500.0], invest_cost=[0.0],
        invest_max_power=[0.0])


@pytest.mark.julia
def test_all_electrolyzers_dispatched_and_exported(tmp_path):
    from esfex.runner import Orchestrator

    demand = tmp_path / "demand.csv"
    np.savetxt(demand, np.full((8760, 1), 10.0), delimiter=",")
    gas = _make_generator("Gas", "Non-renewable", "Gas", [100.0],
                          degradation_rate=[0.0], fuel_cost=[50.0],
                          start_up_cost=[0.0], initial_age=[0])
    sysc = _make_system_config(num_nodes=1, coords=[(23.0, -82.0)],
                               generators={"gas": gas})
    sysc = sysc.model_copy(update={
        "demand_path": str(demand),
        "electrolyzers": {"elz_a": _elz("ElzA", 5.0), "elz_b": _elz("ElzB", 3.0)}})
    cfg = _make_esfex_config(
        sysc, simulation_mode="economic_dispatch",
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720,
                                overlap_hours=0))
    orch = Orchestrator(cfg, output_dir=tmp_path / "out")
    res = orch.run(years=1)[0]

    assert res.electrolyzer_power is not None
    assert res.electrolyzer_power.shape[:2] == (2, 1)
    assert np.allclose(res.electrolyzer_power[0], 5.0, atol=1e-5)
    assert np.allclose(res.electrolyzer_power[1], 3.0, atol=1e-5)
    assert np.allclose(res.electrolyzer_h2[1], 3.0 * 1000 * 0.7 / 50, atol=1e-4)

    h5 = next((tmp_path / "out").glob("*.h5"))
    with h5py.File(h5, "r") as f:
        year = f["detailed_results"][next(iter(f["detailed_results"]))]
        assert sorted(year["electrolyzer_power"]) == ["ElzA", "ElzB"]
        assert year["electrolyzer_power"]["ElzB"].attrs["units"] == "MW"
        assert np.allclose(year["electrolyzer_power"]["ElzB"][()], 3.0, atol=1e-5)
        assert year["electrolyzer_h2"]["ElzA"].attrs["units"] == "kg/h"
