"""NPV-based retirement of existing units in ``esfex run``.

``base_lcoe`` and ``npv_annual_return_rate`` fed only Julia functions no run
called: the NPV loop retired nothing (``force_unit_retirements!`` was empty)
and its NPV ignored the solved plan. With the system setting
``npv_retirement`` on, the runner now solves the master, retires the existing
units whose NPV in the plan is below ``npv_retirement_threshold`` for the
whole horizon and re-solves, in every master mode; the retired units are out
of the dispatch and listed in the results file (``npv_retirements``). Off, the
run is as before.
"""

import sys

import h5py
import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator  # noqa: E402
from test_zones import _make_battery, _make_system_config  # noqa: E402

from esfex.config.schema import (  # noqa: E402
    MasterProblemConfig, PenaltiesConfig, ScenarioMultipliers, StochasticScenarioConfig,
    TemporalConfig,
)


def _config(tmp_path, *, npv: bool, method="monolithic", planning="perfect_foresight",
            stochastic=False):
    demand = tmp_path / "demand.csv"
    np.savetxt(demand, np.full(8760, 60.0))
    # Diesel serves the load at $50/MWh (base_lcoe $93/MWh: it pays);
    # the $500/MWh peaker never runs and pays $20,000/MW·yr fixed O&M;
    # the battery's fixed O&M ($1,000/MW·yr) is above its NPV revenue
    # (0.15 × $800/MW = $120/MW·yr)
    diesel = _make_generator("Diesel", "Non-renewable", "Diesel", [100.0],
                             degradation_rate=[0.0], fuel_cost=[50.0], min_power=[0.0])
    peaker = _make_generator("Peaker", "Non-renewable", "Diesel", [40.0],
                             degradation_rate=[0.0], fuel_cost=[500.0], min_power=[0.0],
                             fixed_cost=[20000.0], decommissioning_cost=[1000.0])
    bat = _make_battery("Bat", 1).model_copy(update={"fixed_cost": [1000.0],
                                                     "degradation_rate": [0.0]})
    sysc = _make_system_config(num_nodes=1, coords=[(21.0, -80.0)],
                               generators={"diesel": diesel, "peaker": peaker},
                               batteries={"bat": bat})
    update = {"demand_path": str(demand), "penalties": PenaltiesConfig(loss_of_load=10000.0),
              "npv_retirement": npv, "target_re_penetration": 0.0}
    if stochastic:
        update["stochastic_scenarios"] = [
            StochasticScenarioConfig(name="low", probability=0.5,
                                     multipliers=ScenarioMultipliers(fuel_cost=0.8)),
            StochasticScenarioConfig(name="high", probability=0.5,
                                     multipliers=ScenarioMultipliers(fuel_cost=1.2)),
        ]
    sysc = sysc.model_copy(update=update)
    return _make_esfex_config(
        sysc, simulation_mode="development",
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720, overlap_hours=0),
        master_problem=MasterProblemConfig(solver_method=method, planning_mode=planning,
                                           stochastic=stochastic))


def _run(tmp_path, tag, years=1, **kw):
    from esfex.runner import Orchestrator

    out = tmp_path / tag
    orch = Orchestrator(_config(tmp_path, **kw), output_dir=out)
    results = orch.run(years=years)
    h5 = next(out.glob("*.h5"))
    return orch, results, h5


def _retired(orch):
    return {(r["kind"], r["key"]) for r in orch._npv_retirements}


@pytest.mark.julia
@pytest.mark.parametrize("method", ["monolithic", "benders"])
def test_uneconomic_units_are_retired(tmp_path, method):
    orch, results, h5 = _run(tmp_path, method, npv=True, method=method)
    assert _retired(orch) == {("generator", "peaker"), ("battery", "bat")}
    peaker = next(r for r in orch._npv_retirements if r["key"] == "peaker")
    assert peaker["npv"] < 0 and peaker["iteration"] == 1
    assert peaker["year"] == results[0].year and peaker["share"] == pytest.approx(1.0)
    # Out of the dispatch
    units = orch._current_units_config
    assert units["peaker"]["rated_power"] == pytest.approx([0.0])
    assert units["bat"]["MaxDischargePower"] == pytest.approx([0.0])
    assert units["diesel"]["rated_power"][0] > 0
    # Listed in the results file, where the dashboard's retirement chart
    # finds the peaker (generator index 1) retired in full
    from esfex.visualization.panels.results_charts import (
        _load_decommissioning_data, _load_gen_configs,
    )
    with h5py.File(h5, "r") as f:
        grp = f["npv_retirements"]
        assert sorted(k.decode() if isinstance(k, bytes) else k for k in grp["key"][:]) == \
            ["bat", "peaker"]
        assert all(v < 0 for v in grp["npv"][:])
        sc = f[f"detailed_results/year_{results[0].year}_threshold_0"]
        _, fracs = _load_decommissioning_data(sc, gen_configs=_load_gen_configs(f))
        assert fracs.get(1) == pytest.approx(1.0)
        assert fracs.get(0, 0.0) == pytest.approx(0.0)


@pytest.mark.julia
def test_off_retires_nothing(tmp_path):
    off, off_results, h5 = _run(tmp_path, "off", npv=False)
    assert off._npv_retirements == []
    assert off._current_units_config["peaker"]["rated_power"][0] > 0
    assert off._current_units_config["bat"]["MaxDischargePower"] == pytest.approx([50.0])
    with h5py.File(h5, "r") as f:
        assert "npv_retirements" not in f
    # The retired units no longer pay fixed O&M in the dispatch
    _, on_results, _ = _run(tmp_path, "on", npv=True)
    assert on_results[0].objective < off_results[0].objective


@pytest.mark.julia
def test_myopic_keeps_retired_units_out(tmp_path):
    orch, results, _ = _run(tmp_path, "myopic", years=2, npv=True, planning="myopic")
    # Retired by the first year's master, once; the second keeps them out
    assert _retired(orch) == {("generator", "peaker"), ("battery", "bat")}
    assert {r["year"] for r in orch._npv_retirements} == {results[0].year}
    units = orch._current_units_config  # the second year's dispatch
    assert units["peaker"]["rated_power"] == pytest.approx([0.0])
    assert units["bat"]["MaxDischargePower"] == pytest.approx([0.0])


@pytest.mark.julia
def test_stochastic_master_retires(tmp_path):
    orch, _, _ = _run(tmp_path, "stochastic", npv=True, stochastic=True)
    assert _retired(orch) == {("generator", "peaker"), ("battery", "bat")}
