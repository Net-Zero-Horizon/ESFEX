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
            stochastic=False, peaker_decommissioning=1000.0):
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
                             fixed_cost=[20000.0],
                             decommissioning_cost=[peaker_decommissioning])
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


# Fixed O&M ($/yr) of the units: diesel 100 MW × $5, peaker 40 MW × $20,000,
# battery 50 MW × $1,000
DIESEL_FIXED, PEAKER_FIXED, BAT_FIXED = 500.0, 800_000.0, 50_000.0


def _check_results_file(h5, years, *, retired: bool):
    """Each year of the results file: the peaker (generator index 1) is
    reported retired, its MW in service and the battery's are 0, the
    dashboard's retirement chart sees it and its per-system fixed O&M leaves
    both out (or, with *retired* False, the opposite)."""
    from esfex.visualization.panels.dashboard_loader import DashboardLoader
    from esfex.visualization.panels.results_charts import (
        _load_decommissioning_data, _load_gen_configs,
    )
    with h5py.File(h5, "r") as f:
        for year in years:
            sc = f[f"detailed_results/year_{year}_threshold_0"]
            assert "retirement_gen_0" not in sc.attrs
            peaker_mw = sc["generation/Peaker"].attrs["rated_power"]
            bat_mw = sc["battery_discharge/Bat"].attrs["rated_power"]
            assert sc["generation/Diesel"].attrs["rated_power"] == pytest.approx([100.0])
            _, fracs = _load_decommissioning_data(sc, gen_configs=_load_gen_configs(f))
            fixed = DashboardLoader({}, {})._per_system_cost(f, "", year)["fixed_om_cost"]
            if retired:
                assert sc.attrs["retirement_gen_1"] == pytest.approx(1.0)
                assert peaker_mw == pytest.approx([0.0])
                assert bat_mw == pytest.approx([0.0])
                assert fracs.get(1) == pytest.approx(1.0)
                assert fixed == pytest.approx(DIESEL_FIXED)
            else:
                assert "retirement_gen_1" not in sc.attrs
                assert peaker_mw == pytest.approx([40.0])
                assert bat_mw == pytest.approx([50.0])
                assert fracs.get(1, 0.0) == pytest.approx(0.0)
                assert fixed == pytest.approx(DIESEL_FIXED + PEAKER_FIXED + BAT_FIXED)


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
    # Listed in the results file, and reported as retirements
    with h5py.File(h5, "r") as f:
        grp = f["npv_retirements"]
        assert sorted(k.decode() if isinstance(k, bytes) else k for k in grp["key"][:]) == \
            ["bat", "peaker"]
        assert all(v < 0 for v in grp["npv"][:])
    assert results[0].retirements == {"gen_1": pytest.approx(1.0)}
    _check_results_file(h5, [results[0].year], retired=True)


@pytest.mark.julia
def test_off_retires_nothing(tmp_path):
    off, off_results, h5 = _run(tmp_path, "off", npv=False)
    assert off._npv_retirements == []
    assert off._current_units_config["peaker"]["rated_power"][0] > 0
    assert off._current_units_config["bat"]["MaxDischargePower"] == pytest.approx([50.0])
    with h5py.File(h5, "r") as f:
        assert "npv_retirements" not in f
    _check_results_file(h5, [off_results[0].year], retired=False)
    # The retired units no longer pay fixed O&M in the dispatch
    _, on_results, _ = _run(tmp_path, "on", npv=True)
    assert on_results[0].objective < off_results[0].objective


@pytest.mark.julia
def test_myopic_keeps_retired_units_out(tmp_path):
    orch, results, h5 = _run(tmp_path, "myopic", years=2, npv=True, planning="myopic")
    # Retired by the first year's master, once; the second keeps them out
    assert _retired(orch) == {("generator", "peaker"), ("battery", "bat")}
    assert {r["year"] for r in orch._npv_retirements} == {results[0].year}
    units = orch._current_units_config  # the second year's dispatch
    assert units["peaker"]["rated_power"] == pytest.approx([0.0])
    assert units["bat"]["MaxDischargePower"] == pytest.approx([0.0])
    # Reported retired, and out of service, in both years
    assert [r.retirements for r in results] == [{"gen_1": pytest.approx(1.0)}] * 2
    _check_results_file(h5, [r.year for r in results], retired=True)
    # The first year's NPV covers both years of the horizon (its operation
    # held), as perfect foresight values them; it covered that year only
    npv = {r["key"]: r["npv"] for r in orch._npv_retirements}
    two_years = 1 + 1 / 1.05
    assert npv["peaker"] == pytest.approx(-PEAKER_FIXED * two_years)
    assert npv["bat"] == pytest.approx(-(BAT_FIXED - 0.15 * 800.0 * 50.0) * two_years)
    foresight, _, _ = _run(tmp_path, "foresight2", years=2, npv=True)
    assert {r["key"]: r["npv"] for r in foresight._npv_retirements} == pytest.approx(npv)


def _failing_resolve(monkeypatch):
    """Make every master re-solve after an NPV retirement round report
    INFEASIBLE (the model is rebuilt and solved, then reported failed)."""
    from esfex.bridge.adapters import MasterProblemAdapter

    real = MasterProblemAdapter._solve_once

    def solve_once(self):
        status = real(self)
        if self.npv_retirements:
            self._last_status = "INFEASIBLE"
            return -1
        return status
    monkeypatch.setattr(MasterProblemAdapter, "_solve_once", solve_once)


@pytest.mark.julia
@pytest.mark.parametrize("planning", ["perfect_foresight", "myopic"])
def test_failed_resolve_keeps_the_last_optimal_plan(tmp_path, monkeypatch, caplog, planning):
    """A re-solve that is not optimal undoes its round: the run goes on with
    the last optimal plan, the units kept and none listed as retired (it
    stopped with "Master problem failed")."""
    import logging

    _failing_resolve(monkeypatch)
    years = 2 if planning == "myopic" else 1
    with caplog.at_level(logging.WARNING):
        orch, results, h5 = _run(tmp_path, planning, years=years, npv=True, planning=planning)
    assert len(results) == years
    assert orch._npv_retirements == []
    assert orch._current_units_config["peaker"]["rated_power"][0] > 0
    assert orch._current_units_config["bat"]["MaxDischargePower"] == pytest.approx([50.0])
    warnings = [r.getMessage() for r in caplog.records
                if "NPV retirement" in r.getMessage() and r.levelno == logging.WARNING]
    assert len(warnings) == years
    assert all("not optimal (INFEASIBLE)" in w and "Peaker" in w and "Bat" in w
               for w in warnings)
    with h5py.File(h5, "r") as f:
        assert len(f["npv_retirements/key"]) == 0
    _check_results_file(h5, [r.year for r in results], retired=False)


@pytest.mark.julia
def test_resolve_above_the_curtailment_limit_is_undone(tmp_path, monkeypatch, caplog):
    """Round 39: the annual curtailment limit is priced, not hard, so
    retiring the store that held it no longer makes the re-solve infeasible.
    A re-solve that curtails more above the limit than the plan before is
    undone like an infeasible one (here the excess is mocked: 500 MWh/yr
    once units are retired)."""
    import logging

    from esfex.bridge.adapters import MasterProblemAdapter

    monkeypatch.setattr(MasterProblemAdapter, "_curtailment_excess_mwh",
                        lambda self: 500.0 if self.npv_retirements else 0.0)
    with caplog.at_level(logging.WARNING):
        orch, results, h5 = _run(tmp_path, "excess", npv=True)
    assert orch._npv_retirements == []
    warnings = [r.getMessage() for r in caplog.records
                if "NPV retirement" in r.getMessage() and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "curtails 0.5 GWh/yr above the annual curtailment limit" in warnings[0]
    assert "Peaker" in warnings[0] and "Bat" in warnings[0]
    _check_results_file(h5, [r.year for r in results], retired=False)


@pytest.mark.julia
def test_resolve_that_raises_the_total_cost_is_undone(tmp_path, caplog):
    """A re-solve whose total cost is above the plan before's undoes its
    round (any optimal re-solve used to be kept). Decommissioning the peaker
    costs $30,000/MW, more than the $20,000/MW of fixed O&M a one-year plan
    saves: retiring it and the battery raises the cost by about $375,000."""
    import logging

    with caplog.at_level(logging.WARNING):
        orch, results, h5 = _run(tmp_path, "dear", npv=True, peaker_decommissioning=30000.0)
    assert orch._npv_retirements == []
    warnings = [r.getMessage() for r in caplog.records
                if "NPV retirement" in r.getMessage() and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "raises the plan's total cost to $" in warnings[0]
    assert "Peaker" in warnings[0] and "Bat" in warnings[0]
    _check_results_file(h5, [r.year for r in results], retired=False)


@pytest.mark.julia
def test_myopic_total_cost_counts_the_later_years(tmp_path):
    """The same units in the first year of a two-year myopic run: its
    one-year master pays the decommissioning, but the NPV also counts the
    second year's fixed O&M retiring them saves ($800,000 + $50,000,
    discounted), which the comparison counts too: the retirement is kept."""
    orch, results, _ = _run(tmp_path, "myopic_dear", years=2, npv=True, planning="myopic",
                            peaker_decommissioning=30000.0)
    assert _retired(orch) == {("generator", "peaker"), ("battery", "bat")}
    assert {r["year"] for r in orch._npv_retirements} == {results[0].year}


@pytest.mark.julia
def test_stochastic_master_retires(tmp_path):
    orch, _, _ = _run(tmp_path, "stochastic", npv=True, stochastic=True)
    assert _retired(orch) == {("generator", "peaker"), ("battery", "bat")}
