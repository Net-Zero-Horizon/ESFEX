"""Console scripting API on a tiny config.

``esfex.solve_master()`` / ``esfex.solve_dispatch()`` built the adapters
without the demand and horizon arguments they require (TypeError), and
``esfex.load_results()`` did not match the HDF5 layout the runner writes
(generation held dataset names, prices / storage / investments / summary
and the unit names came back empty).
"""

import sys

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator  # noqa: E402
from test_zones import _make_battery_technology, _make_system_config, _make_technology  # noqa: E402

from esfex.config.schema import MasterProblemConfig, PenaltiesConfig, TemporalConfig  # noqa: E402
from esfex.visualization.scripting_api import ESFEXAPI  # noqa: E402

pytestmark = pytest.mark.julia


@pytest.fixture(scope="module")
def cfg(tmp_path_factory):
    """One node, 40/80 MW days, a cheap gas technology to build (6-h steps)."""
    tmp = tmp_path_factory.mktemp("scripting")
    demand = tmp / "demand.csv"
    np.savetxt(demand, np.tile(np.r_[np.full(12, 40.0), np.full(12, 80.0)], 365))
    gens = {
        "base": _make_generator("Base", "Non-renewable", "Gas", [60.0], degradation_rate=[0.0],
                                fuel_cost=[50.0], min_power=[0.0]),
        "peak": _make_generator("Peak", "Non-renewable", "Diesel", [100.0],
                                degradation_rate=[0.0], fuel_cost=[500.0],
                                min_power=[0.0]),
    }
    tech = _make_technology("New Gas", "Gas", 1, tech_type="Non-renewable",
                            invest_cost=1000.0, invest_max_power=100.0)
    tech = tech.model_copy(update={"fuel_cost": [40.0]})
    bt = _make_battery_technology("Li-ion", 1)
    sysc = _make_system_config(num_nodes=1, coords=[(21.0, -80.0)], generators=gens,
                               technologies={"newgas": tech}, battery_technologies={"li": bt})
    sysc = sysc.model_copy(update={"demand_path": str(demand),
                                   "penalties": PenaltiesConfig(loss_of_load=10000.0)})
    return _make_esfex_config(
        sysc, simulation_mode="development",
        temporal=TemporalConfig(resolution_hours=6, rolling_horizon_hours=720, overlap_hours=0),
        master_problem=MasterProblemConfig())


def test_solve_master_returns_plan(cfg, tmp_path):
    sol = ESFEXAPI.solve_master(cfg, years=2, output_dir=tmp_path)
    assert set(sol) == {"investments", "retirements", "cumulative_capacities", "re_targets"}
    assert sorted(sol["investments"]) == [2025, 2026]
    # The 60 MW base plant cannot meet the 80 MW evening peak: new gas is built
    assert sol["investments"][2025]["tech_investment_power_0_0"] > 20.0


def test_solve_dispatch_uses_existing_units(cfg, tmp_path):
    res = ESFEXAPI.solve_dispatch(cfg, year=2025, output_dir=tmp_path)
    assert res.year == 2025 and res.feasible
    assert res.gen_output.shape == (2, 1, 1460)  # Base, Peak; no investments
    # Evening peak above the base plant is met by the peak unit
    assert res.gen_output[1].max() == pytest.approx(20.0, abs=1e-3)


def test_load_results_matches_runner_output(cfg, tmp_path):
    results = ESFEXAPI.run(cfg, tmp_path, years=2)
    h5 = next(tmp_path.glob("*.h5"))
    r = ESFEXAPI.load_results(h5)

    assert r["years"] == [2025, 2026]
    assert set(r["generator_names"]) == {"Base", "Peak", "Investment New Gas"}
    for res in results:
        yr = res.year
        gen = r["generation"][yr]
        assert gen.shape == (3, 1460)
        np.testing.assert_allclose(gen.sum(), res.gen_output.sum(), rtol=1e-9)
        assert r["demand"][yr].shape == (1, 1460)
        assert r["prices"][yr].shape == (1, 1460)
        assert r["summary"][yr]["total_cost"] == pytest.approx(res.objective)
        assert r["summary"][yr]["renewable_penetration"] == pytest.approx(res.re_penetration)
    assert r["investments"][2025]["New Gas"] > 20.0
    assert r["curtailment"][2025].shape == (1, 1460)
    if r["battery_names"]:
        assert r["storage"][2025].shape == (len(r["battery_names"]), 1460)

    only = ESFEXAPI.load_results(h5, years=[2026])
    assert only["years"] == [2026] and list(only["generation"]) == [2026]
