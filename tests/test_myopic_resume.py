"""Resuming an interrupted myopic run (``esfex run --resume``).

A myopic development run writes a checkpoint next to its results file after
every master year and every dispatched year (``esfex.io.checkpoint``).
Resumed, the run continues from the first year not completed and appends to
the same file; the result must be the one an uninterrupted run gives. A
resume is refused when the case changed, the run completed, the run is not
myopic or the files are missing.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import h5py
import numpy as np
import pytest
import yaml
from typer.testing import CliRunner

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator  # noqa: E402
from test_zones import (  # noqa: E402
    _make_battery, _make_battery_technology, _make_system_config, _make_technology)

from esfex.api.diagnostics import WarningsCollector  # noqa: E402
from esfex.config.schema import (  # noqa: E402
    LoggingConfig, MasterProblemConfig, PenaltiesConfig, TemporalConfig)
from esfex.io import checkpoint as ck  # noqa: E402

YEARS = 3
START = 2025
FIXTURE = Path(__file__).parent / "fixtures" / "studio_export_island.yaml"


def _case(tmp_path, planning="myopic"):
    """One node, three years of growing load (60, 70, 80 MW, with a weekly
    wave): diesel, an old oil unit at the end of its life, an uneconomic
    peaker (NPV retirement), a degrading battery, and gas and battery
    candidates."""
    demand = tmp_path / "demand.csv"
    if not demand.exists():
        days = np.arange(YEARS * 8760) / 24.0
        load = np.repeat([60.0, 70.0, 80.0], 8760) + 8.0 * np.sin(2 * np.pi * days / 7.0)
        np.savetxt(demand, load)
    diesel = _make_generator("Diesel", "Non-renewable", "Diesel", [55.0],
                             degradation_rate=[0.0], fuel_cost=[90.0], min_power=[0.0])
    old = _make_generator("Old oil", "Non-renewable", "Oil", [20.0], life_time=[25],
                          initial_age=[24], degradation_rate=[0.0], fuel_cost=[150.0],
                          min_power=[0.0])
    peaker = _make_generator("Peaker", "Non-renewable", "Diesel", [40.0],
                             degradation_rate=[0.0], fuel_cost=[500.0], min_power=[0.0],
                             fixed_cost=[20000.0])
    bat = _make_battery("Bat", 1)
    gas = _make_technology("Gas CC", "Gas", 1, tech_type="Non-renewable",
                           invest_cost=200000.0, invest_max_power=200.0)
    gas = gas.model_copy(update={"fuel_cost": [40.0]})
    sysc = _make_system_config(num_nodes=1, coords=[(21.0, -80.0)],
                               generators={"diesel": diesel, "old": old, "peaker": peaker},
                               batteries={"bat": bat},
                               technologies={"gas": gas},
                               battery_technologies={"li": _make_battery_technology("Li-ion", 1)})
    sysc = sysc.model_copy(update={
        "name": "Island", "demand_path": str(demand),
        "penalties": PenaltiesConfig(loss_of_load=10000.0),
        "npv_retirement": True, "target_re_penetration": 0.0, "min_annual_increment": 0.0})
    return _make_esfex_config(
        sysc, simulation_mode="development",
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720, overlap_hours=0),
        master_problem=MasterProblemConfig(planning_mode=planning))


def _orch(cfg, out):
    from esfex.runner import Orchestrator

    return Orchestrator(cfg, output_dir=out)


# ── Equivalence: straight vs interrupted + resumed ───────────────────────

_VOLATILE_ATTRS = {"creation_date", "last_update", "export_timestamp", "solve_time",
                   "checkpoint"}


def _h5_contents(path: Path) -> dict:
    """Every dataset and attribute of a results file (bar the timestamps
    and solve times, and the warnings, which a resume counts twice for the
    configuration load), by path."""
    out: dict = {}

    def visit(name, obj):
        if name.startswith("diagnostics/warnings"):
            return
        for k, v in obj.attrs.items():
            if k not in _VOLATILE_ATTRS:
                out[f"{name}@{k}"] = v
        if isinstance(obj, h5py.Dataset):
            out[name] = obj[()]

    with h5py.File(path, "r") as f:
        for k, v in f.attrs.items():
            if k not in _VOLATILE_ATTRS:
                out[f"@{k}"] = v
        f.visititems(visit)
    return out


def _assert_same(a, b, where):
    if isinstance(a, np.ndarray) and a.dtype.kind in "fiub":
        assert a.shape == b.shape, where
        np.testing.assert_allclose(a, b, rtol=1e-6, atol=1e-6, err_msg=where)
    elif isinstance(a, (float, np.floating)):
        assert float(a) == pytest.approx(float(b), rel=1e-6, abs=1e-6), where
    elif isinstance(a, np.ndarray):
        assert a.tolist() == b.tolist(), where
    else:
        assert a == b, where


@pytest.mark.julia
def test_interrupted_and_resumed_run_equals_the_straight_run(tmp_path):
    from esfex.runner import Orchestrator

    # The straight run
    straight = _orch(_case(tmp_path), tmp_path / "a")
    res_a = straight.run(years=YEARS)
    h5_a = straight._hdf5_path
    assert ck.checkpoint_info(h5_a)["status"] == "complete"

    # Interrupted in the master of year 2 (Ctrl-C) ...
    first = _orch(_case(tmp_path), tmp_path / "b")
    calls = {"n": 0}
    real_cost = Orchestrator._report_myopic_year_cost

    def cost_then_interrupt(self, year, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return real_cost(self, year, *a, **kw)

    with patch.object(Orchestrator, "_report_myopic_year_cost", cost_then_interrupt):
        with pytest.raises(KeyboardInterrupt):
            first.run(years=YEARS)
    h5_b = first._hdf5_path
    info = ck.checkpoint_info(h5_b)
    assert info["phase"] == "master" and info["master"]["completed_years"] == [START]
    assert info["resume_at"] == {"phase": "master", "year": START + 1}

    # ... resumed, then killed in the dispatch of year 3, after the year's
    # results were written but before its checkpoint
    second = _orch(_case(tmp_path), tmp_path / "b")
    real_append = Orchestrator._append_year_to_hdf5

    def append_then_crash(self, path, result):
        real_append(self, path, result)
        if result.year == START + 2:
            raise RuntimeError("simulated crash")

    with patch.object(Orchestrator, "_append_year_to_hdf5", append_then_crash):
        with pytest.raises(RuntimeError, match="simulated crash"):
            second.run(resume_from=h5_b)
    assert second.resume_plan.phase == "master"
    info = ck.checkpoint_info(h5_b)
    assert info["phase"] == "dispatch"
    assert [r["year"] for r in info["dispatch"]["completed"]] == [START, START + 1]
    assert info["resume_at"] == {"phase": "dispatch", "year": START + 2}
    with h5py.File(h5_b, "r") as f:   # the unfinished year is in the file
        assert f"detailed_results/year_{START + 2}_threshold_0" in f

    # ... and resumed again: only year 3 is dispatched
    third = _orch(_case(tmp_path), tmp_path / "b")
    res_c = third.run(resume_from=h5_b)
    assert [r.year for r in res_c] == [START + 2]
    assert third._hdf5_path == h5_b
    assert ck.checkpoint_info(h5_b)["status"] == "complete"
    assert len(ck.checkpoint_info(h5_b)["resumes"]) == 2

    # The plan had something to carry: builds, a retirement, NPV retirements
    inv = {y: r.investments for y, r in zip(range(START, START + YEARS), res_a)}
    assert any(v > 1.0 for i in inv.values() for v in i.values())
    assert any(r.retirements for r in res_a)
    assert {r["key"] for r in straight._npv_retirements} & {"peaker", "bat"}

    # Same plan and costs ...
    for mine, theirs in ((third._master_plan[0], straight._master_plan[0]),
                         (third._master_plan[1], straight._master_plan[1]),
                         (third.myopic_year_costs, straight.myopic_year_costs)):
        assert sorted(mine) == sorted(theirs)
        for year in theirs:
            assert mine[year] == pytest.approx(theirs[year], rel=1e-6), year
    assert third._npv_retirements == straight._npv_retirements
    assert res_c[0].objective == pytest.approx(res_a[2].objective, rel=1e-6)
    # ... and the same results file, dataset by dataset
    a, b = _h5_contents(h5_a), _h5_contents(h5_b)
    assert sorted(a) == sorted(b)
    for key in a:
        _assert_same(a[key], b[key], key)
    with h5py.File(h5_b, "r") as f:
        assert list(f["summary_results/year"][()]) == [START, START + 1, START + 2]
        assert bool(f.attrs["export_complete"])
        # Warnings of all three invocations
        assert f["diagnostics/warnings"].attrs["total"] >= 1


# ── Refusals ─────────────────────────────────────────────────────────────

def _fake_run(tmp_path, cfg, *, phase="dispatch", status="running", done=(START,)):
    """A results file with a checkpoint as a run of *cfg* writes it."""
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    h5 = out / "case_19112026_1030.h5"
    with h5py.File(h5, "w") as f:
        f.attrs["simulation_mode"] = "development"
        f.attrs["planning_mode"] = "myopic"
    ck.write_checkpoint(ck.checkpoint_path(h5), {
        "status": status, "phase": phase, "case": ck.fingerprint(cfg, None),
        "run": {"years": YEARS, "start_year": START, "dispatch_years": None},
        "master": {"completed_years": list(range(START, START + YEARS))},
        "dispatch": {"completed": [{"year": y, "feasible": True, "objective": 1.0}
                                   for y in done]},
        "resume_at": {"phase": phase, "year": START + len(done)},
        "state": None})
    return h5


def _codes(excinfo):
    return [e.code for e in excinfo.value.errors]


def test_a_checkpoint_of_the_same_case_is_accepted(tmp_path):
    cfg = _case(tmp_path)
    h5 = _fake_run(tmp_path, cfg)
    plan = ck.check_resume(h5, cfg, None)
    assert plan.phase == "dispatch" and plan.resume_year == START + 1
    assert plan.dispatch_years == [START]
    # Logging and the solver's verbosity, threads and time limit do not count
    cfg2 = cfg.model_copy(deep=True)
    cfg2.logging = LoggingConfig(console_level="debug")
    cfg2.solver.verbose, cfg2.solver.threads, cfg2.solver.time_limit = True, 32, 99
    assert ck.check_resume(h5, cfg2, None).forced == []


def test_changed_configuration_is_refused_unless_forced(tmp_path):
    cfg = _case(tmp_path)
    h5 = _fake_run(tmp_path, cfg)
    changed = cfg.model_copy(deep=True)
    changed.systems["Island"].generators["diesel"].fuel_cost = [91.0]
    with pytest.raises(ck.ResumeError) as e:
        ck.check_resume(h5, changed, None)
    assert _codes(e) == ["config_changed"]
    assert "--force-resume" in e.value.errors[0].fix
    plan = ck.check_resume(h5, changed, None, force=True)
    assert plan.forced and "configuration changed" in plan.forced[0]


def test_changed_input_file_is_refused(tmp_path):
    cfg = _case(tmp_path)
    h5 = _fake_run(tmp_path, cfg)
    demand = Path(cfg.systems["Island"].demand_path)
    demand.write_text(demand.read_text().replace("6", "7", 1))
    with pytest.raises(ck.ResumeError) as e:
        ck.check_resume(h5, cfg, None)
    assert _codes(e) == ["data_changed"]
    assert e.value.errors[0].key == str(demand)


def test_other_esfex_version_is_refused(tmp_path):
    cfg = _case(tmp_path)
    h5 = _fake_run(tmp_path, cfg)
    doc = json.loads(ck.checkpoint_path(h5).read_text())
    doc["case"]["esfex_version"] = "0.0.1"
    ck.checkpoint_path(h5).write_text(json.dumps(doc))
    with pytest.raises(ck.ResumeError) as e:
        ck.check_resume(h5, cfg, None)
    assert _codes(e) == ["version_changed"]


def test_completed_run_is_refused(tmp_path):
    cfg = _case(tmp_path)
    h5 = _fake_run(tmp_path, cfg, phase="complete", status="complete",
                   done=range(START, START + YEARS))
    with pytest.raises(ck.ResumeError) as e:
        ck.check_resume(h5, cfg, None)
    assert _codes(e) == ["run_complete"]


def test_perfect_foresight_is_not_resumable(tmp_path):
    cfg = _case(tmp_path, planning="perfect_foresight")
    h5 = tmp_path / "pf.h5"
    with h5py.File(h5, "w") as f:
        f.attrs["simulation_mode"] = "development"
        f.attrs["planning_mode"] = "perfect_foresight"
    with pytest.raises(ck.ResumeError) as e:
        ck.check_resume(h5, cfg, None)
    assert _codes(e) == ["not_resumable"]
    # A myopic run's file, but the case is now perfect foresight
    myopic = _fake_run(tmp_path, _case(tmp_path))
    with pytest.raises(ck.ResumeError) as e:
        ck.check_resume(myopic, cfg, None)
    assert _codes(e) == ["not_resumable"]


def test_missing_files_are_refused(tmp_path):
    cfg = _case(tmp_path)
    with pytest.raises(ck.ResumeError) as e:
        ck.check_resume(tmp_path / "nope.h5", cfg, None)
    assert _codes(e) == ["results_not_found"]
    h5 = _fake_run(tmp_path, cfg)
    ck.checkpoint_path(h5).unlink()
    with pytest.raises(ck.ResumeError) as e:
        ck.check_resume(h5, cfg, None)
    assert _codes(e) == ["checkpoint_missing"]


def test_other_years_are_refused(tmp_path):
    cfg = _case(tmp_path)
    h5 = _fake_run(tmp_path, cfg)
    assert ck.check_resume(h5, cfg, None, years=YEARS, start_year=START)
    with pytest.raises(ck.ResumeError) as e:
        ck.check_resume(h5, cfg, None, years=YEARS + 1, start_year=START - 1)
    assert _codes(e) == ["years_mismatch", "years_mismatch"]


def test_refused_resume_runs_nothing(tmp_path):
    """Orchestrator.run refuses before loading anything and leaves the
    results file as it was."""
    cfg = _case(tmp_path)
    h5 = _fake_run(tmp_path, cfg, phase="complete", status="complete")
    before = h5.read_bytes()
    orch = _orch(cfg, tmp_path / "out")
    with patch.object(type(orch), "_load_demand", side_effect=AssertionError("ran")):
        with pytest.raises(ck.ResumeError):
            orch.run(resume_from=h5)
    assert h5.read_bytes() == before


# ── The checkpoint file and the results file ─────────────────────────────

def test_state_reads_back_exactly(tmp_path):
    state = {"built": {("gas", 0): np.array([1.5, 2.0]), ("li", 1): np.zeros(2)},
             "caps": {2025: {"gen": {1: [55.0]}, "reservoir": {(0, 1): 3.0}}},
             "npv_retired": [("generator", "peaker", 1)], "keys": {"a", "b"},
             "levels": {"Diesel": {0: 1.25}}, "inf": float("inf"),
             "nan": np.array([np.nan, 1.0]), "n": np.int64(3), "__dict__": 1}
    path = tmp_path / "x.checkpoint.json"
    ck.write_checkpoint(path, {"state": state})
    json.loads(path.read_text())         # strict JSON (no NaN, no Infinity)
    back = ck.read_checkpoint(path)["state"]
    assert set(back["built"]) == {("gas", 0), ("li", 1)}
    assert back["built"][("gas", 0)].tolist() == [1.5, 2.0]
    assert back["caps"][2025]["gen"][1] == [55.0]
    assert back["caps"][2025]["reservoir"] == {(0, 1): 3.0}
    assert back["npv_retired"] == [("generator", "peaker", 1)]
    assert back["keys"] == {"a", "b"} and back["levels"] == {"Diesel": {0: 1.25}}
    assert back["inf"] == float("inf") and np.isnan(back["nan"][0])
    assert back["n"] == 3 and back["__dict__"] == 1
    assert list(tmp_path.iterdir()) == [path]      # no temporary file left


def test_dispatch_carry_over_reads_back(tmp_path):
    """What a dispatch-phase checkpoint holds comes back as it was: the
    master plan and the year-end carry-over (fuel tank levels and fuel in
    transit, which link the years)."""
    from esfex.runner import SimulationState

    orch = _orch(_case(tmp_path), tmp_path / "o")
    orch.state = SimulationState(year=2026, base_year=2025, units_config={})
    orch.state.boundary_conditions = {
        "pe_storage_levels": {"Diesel": {0: 12.5}},
        "pe_in_transit": {"Diesel": np.array([[1.0, 2.0]])},
        "battery_soc": np.array([[3.0]])}
    orch.state.primary_energy_capacities = {"storage": {"Diesel": {0: 10.0}}, "transport": {}}
    orch._master_plan = (
        {2025: {"tech_investment_power_0_0": 5.0}}, {2025: {"gen_1": 1.0}},
        {2025: {"gen": {1: [55.0]}, "tech": {1: np.array([5.0])}, "reservoir": {(0, 0): 1.0}}},
        {2025: 0.1})
    orch._npv_retirements = [{"kind": "generator", "key": "peaker", "node": 0, "year": 2025}]
    orch._master_fuel_by_year = {2025: {"storage": {"Diesel": {0: 10.0}}, "transport": {}}}
    orch.myopic_year_costs = {2025: {"objective": 1.0, "earlier_annuities": 0.0, "total": 1.0}}
    path = tmp_path / "c.checkpoint.json"
    ck.write_checkpoint(path, {"state": orch._dispatch_checkpoint_state()})

    other = _orch(_case(tmp_path), tmp_path / "o")
    other.state = SimulationState(year=2025, base_year=2025, units_config={})
    state = ck.read_checkpoint(path)["state"]
    inv, ret, caps, re_t = other._restore_master_plan(state)
    other._restore_dispatch_state(state)
    assert inv == {2025: {"tech_investment_power_0_0": 5.0}} and ret == {2025: {"gen_1": 1.0}}
    assert caps[2025]["tech"][1].tolist() == [5.0] and caps[2025]["reservoir"] == {(0, 0): 1.0}
    assert re_t == {2025: 0.1}
    bc = other.state.boundary_conditions
    assert bc["pe_storage_levels"] == {"Diesel": {0: 12.5}}
    assert bc["pe_in_transit"]["Diesel"].tolist() == [[1.0, 2.0]]
    assert other.state.primary_energy_capacities == orch.state.primary_energy_capacities
    assert other._npv_retirements == orch._npv_retirements
    assert other._master_fuel_by_year == orch._master_fuel_by_year
    assert other.myopic_year_costs == orch.myopic_year_costs


def test_unknown_objects_are_not_written(tmp_path):
    path = tmp_path / "x.checkpoint.json"
    ck.write_checkpoint(path, {"state": {"a": 1}})
    with pytest.raises(TypeError):
        ck.write_checkpoint(path, {"state": {"a": object()}})
    assert ck.read_checkpoint(path)["state"] == {"a": 1}    # the old one is intact


def test_truncate_drops_the_unfinished_year(tmp_path):
    h5 = tmp_path / "r.h5"
    with h5py.File(h5, "w") as f:
        for grp in ("summary_results", "global/summary_results"):
            g = f.create_group(grp)
            g.create_dataset("year", data=[2025, 2026, 2027], maxshape=(None,))
            g.create_dataset("total_cost", data=[1.0, 2.0, 3.0], maxshape=(None,))
        for y in (2025, 2026, 2027):
            f.create_group(f"detailed_results/year_{y}_threshold_0")
            f.create_group(f"cost_breakdown/year_{y}")
            f.create_dataset(f"demand/year_{y}_base_demand", data=[1.0])
            f.create_dataset(f"global/demand/year_{y}_base_demand", data=[1.0])
        f.create_group("npv_retirements")
        f.attrs["export_complete"] = True
    assert ck.truncate_results(h5, {2025, 2026}) == [2027]
    with h5py.File(h5, "r") as f:
        assert list(f["summary_results/year"][()]) == [2025, 2026]
        assert list(f["global/summary_results/total_cost"][()]) == [1.0, 2.0]
        assert sorted(f["detailed_results"]) == ["year_2025_threshold_0",
                                                 "year_2026_threshold_0"]
        assert sorted(f["demand"]) == ["year_2025_base_demand", "year_2026_base_demand"]
        assert "year_2027" not in f["cost_breakdown"]
        assert "npv_retirements" in f and not f.attrs["export_complete"]
    assert ck.truncate_results(h5, {2025, 2026}) == []


def test_newest_results_file_of_the_case(tmp_path):
    import os

    for i, name in enumerate(["case_01012026_1000.h5", "case_02012026_1000.h5",
                              "other_03012026_1000.h5", "case_x.h5"]):
        p = tmp_path / name
        p.write_bytes(b"")
        os.utime(p, (1000 + i, 1000 + i))
    assert ck.newest_results_file(tmp_path, tmp_path / "case.yaml").name == \
        "case_02012026_1000.h5"
    assert ck.newest_results_file(tmp_path, tmp_path / "none.yaml") is None


def test_collector_continues_the_earlier_counts():
    import logging

    c = WarningsCollector()
    rec = logging.LogRecord("esfex.runner", logging.WARNING, "", 0, "Year %d short", (1,), None)
    c.emit(rec)
    earlier = {"total": 5, "groups": [
        {"category": "esfex.runner: Year # short", "level": "ERROR", "logger": "esfex.runner",
         "message": "Year 1 short", "time": "2000-01-01T00:00:00", "count": 4},
        {"category": "esfex.x: other", "level": "WARNING", "logger": "esfex.x",
         "message": "other", "time": "2000-01-01T00:00:00", "count": 1}]}
    c.merge(earlier)
    s = c.summary()
    assert s.total == 6
    assert s.count_by_category == {"esfex.runner: Year # short": 5, "esfex.x: other": 1}
    assert s.groups[0].level == "ERROR"


# ── CLI ──────────────────────────────────────────────────────────────────

def _yaml_case(tmp_path, planning="myopic"):
    data = yaml.safe_load(FIXTURE.read_text())
    data["master_problem"]["planning_mode"] = planning
    sysname = next(iter(data["systems"]))
    np.savetxt(tmp_path / "demand.csv", np.full((8760, 2), 5.0), delimiter=",")
    data["systems"][sysname]["demand_path"] = "demand.csv"
    path = tmp_path / "case.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def _cli(*args):
    from esfex.cli import app

    return CliRunner().invoke(app, ["run", *map(str, args)])


def test_cli_resume_refusal_is_structured(tmp_path):
    case = _yaml_case(tmp_path)
    out = tmp_path / "results"
    # No results file of the case in the output directory
    r = _cli("-c", case, "-o", out, "--resume", "--json")
    assert r.exit_code == 1, r.output
    doc = json.loads(r.stdout)
    assert doc["ok"] is False and doc["status"] == "resume_refused"
    assert doc["errors"][0]["code"] == "results_not_found"
    # A results file without a checkpoint (the newest of the case)
    out.mkdir()
    h5 = out / "case_19112026_1030.h5"
    with h5py.File(h5, "w") as f:
        f.attrs["simulation_mode"] = "development"
    r = _cli("-c", case, "-o", out, "--resume", "--json")
    assert r.exit_code == 1
    doc = json.loads(r.stdout)
    assert doc["errors"][0]["code"] == "checkpoint_missing"
    assert doc["resumed_from"] is None


def test_cli_dry_run_reports_where_it_would_resume(tmp_path):
    from esfex.config.loader import load_config

    case = _yaml_case(tmp_path)
    h5 = _fake_run(tmp_path, load_config(case))
    doc = json.loads(ck.checkpoint_path(h5).read_text())
    doc["case"] = ck.fingerprint(load_config(case), case)
    ck.checkpoint_path(h5).write_text(json.dumps(doc))
    r = _cli("-c", case, "--resume", h5, "--dry-run", "--json")
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["resumed_from"] == START + 1
    assert out["resume"]["phase"] == "dispatch"
    assert out["resume"]["dispatch_years_completed"] == [START]
    # Once the run completed, the dry run says so
    doc.update(status="complete", phase="complete")
    ck.checkpoint_path(h5).write_text(json.dumps(doc))
    r = _cli("-c", case, "--resume", h5, "--dry-run", "--json")
    assert r.exit_code == 1
    out = json.loads(r.stdout)
    assert out["status"] == "resume_refused" and out["errors"][0]["code"] == "run_complete"


def test_cli_usage_errors(tmp_path):
    case = _yaml_case(tmp_path)
    assert _cli("-c", case, tmp_path / "x.h5").exit_code == 2       # file without --resume
    assert _cli("-c", case, "--force-resume").exit_code == 2
