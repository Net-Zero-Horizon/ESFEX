"""Binding constraints, duals and paid slacks of the master problem (A2).

After each master solve the run keeps which constraint families bind, what
they cost at the margin and which slacks the plan pays for, logs a short
digest and writes them under ``/diagnostics/master`` of the results file.
"""

import logging
import sys
from types import SimpleNamespace

import h5py
import numpy as np
import pytest

from esfex.io.master_diagnostics import (
    digest_lines,
    from_julia,
    read_master_diagnostics,
    write_master_diagnostics,
)


def _bind(year, family, label, kind, units, binding, priced, total, mx, code=""):
    return SimpleNamespace(year=year, family=family, code=code, label=label, kind=kind, units=units,
              count_binding=binding, count_priced=priced, count_total=total,
              max_abs_dual=mx, sum_abs_dual=mx * max(priced, 1))


def _diag(duals=True):
    nan = float("nan")
    d = (lambda x: x) if duals else (lambda x: nan)
    return SimpleNamespace(
        duals_available=duals, status="OPTIMAL",
        binding=[
            _bind(1, "re_target", "RE target", "policy", "$/MWh", 1, int(duals), 1, d(100.0),
                  code="RE-2"),
            _bind(1, "budget", "Annual budget (investment)", "policy", "$/$", 0, 0, 1, d(0.0)),
            _bind(1, "adequacy", "Capacity adequacy (reserve margin)", "policy", "$/MW", 0, 0,
                  1, d(0.0)),
            _bind(1, "tech_build_limit", "Technology build limit", "build_limit", "$/MW", 1,
                  int(duals), 1, d(1.6e6)),
            _bind(2, "re_target", "RE target", "policy", "$/MWh", 1, int(duals), 1, d(41.0)),
            _bind(0, "tech_capacity", "Technology capacity (hourly)", "operation", "$/MW", 24,
                  int(duals) * 20, 48, d(43800.0)),
            _bind(0, "unnamed", "Unnamed rows", "structural", "$/unit", 5, 5, 99, d(1.0)),
        ],
        top=[SimpleNamespace(year=1, name="re_target_s1_y1", family="re_target", label="RE target",
                dual=100.0, units="$/MWh")] if duals else [],
        slacks=[SimpleNamespace(year=1, name="slack_re_s1_y1", family="re_target",
                   label="RE target shortfall", value=21900.0, value_units="MWh/yr",
                   unit_cost=100.0, cost=2.19e6, count=1)],
    )


def test_groups_per_year_and_untagged_rows():
    groups = from_julia(_diag(), [2030, 2031])
    assert set(groups) == {"year_2030", "year_2031", "all"}
    g = groups["year_2030"]
    assert g["year"] == 2030
    assert g["attrs"] == {"duals_available": True, "status": "OPTIMAL"}
    assert [r["family"] for r in g["binding"]] == [
        "re_target", "budget", "adequacy", "tech_build_limit"]
    assert g["binding"][0] == {
        "family": "re_target", "label": "RE target", "code": "RE-2", "kind": "policy",
        "units": "$/MWh", "count_binding": 1, "count_priced": 1, "count_total": 1,
        "max_abs_dual": 100.0, "sum_abs_dual": 100.0}
    assert g["slacks"][0]["name"] == "slack_re_s1_y1"
    assert g["top"][0]["dual"] == 100.0
    assert groups["all"]["year"] is None
    assert {r["family"] for r in groups["all"]["binding"]} == {"tech_capacity", "unnamed"}


def test_digest_names_binding_families_slacks_and_idle_policies():
    lines = digest_lines(from_julia(_diag(), [2030, 2031]))
    assert 3 <= len(lines) <= 5
    # Policy first (largest dual and its year), then build limits
    assert lines[0] == ("Master binding: RE target 2030-2031 (100 $/MWh, 2030, at its "
                        "penalty); Technology build limit 2030 (1,600,000 $/MW)")
    assert lines[1].startswith("Master operation binding: Technology capacity (hourly) "
                               "20/48 rows (max 43,800 $/MW)")
    assert lines[2] == ("Master paid slacks: RE target shortfall 2030 21,900 MWh/yr at "
                        "100 $/MWh ($2,190,000)")
    assert lines[3] == ("Master not binding: Annual budget (investment), "
                        "Capacity adequacy (reserve margin)")
    # A single year: no year in the items
    d = _diag()
    d.binding = [r for r in d.binding if r.year == 1]
    one = from_julia(d, [2030])
    assert set(one) == {"year_2030"}
    assert digest_lines(one, prefix="Master 2030")[0].startswith(
        "Master 2030 binding: RE target (100 $/MWh, at its penalty)")


def test_digest_without_duals_counts_tight_rows():
    d = _diag(duals=False)
    d.binding = [r for r in d.binding if r.year == 1]
    lines = digest_lines(from_julia(d, [2030]))
    assert lines[0].startswith("Master binding (no duals: tight rows only): RE target "
                               "(1/1 rows tight)")
    assert lines[1].startswith("Master paid slacks: RE target shortfall")
    assert digest_lines({}) == []


def test_hdf5_layout_roundtrip_and_absence(tmp_path):
    path = tmp_path / "r.h5"
    with h5py.File(path, "w") as f:
        f.create_group("diagnostics/warnings")   # another writer's group stays
    assert read_master_diagnostics(path) == {}
    groups = from_julia(_diag(), [2030, 2031])
    write_master_diagnostics(path, groups, planning_mode="perfect_foresight")
    with h5py.File(path, "r") as f:
        assert "diagnostics/warnings" in f
        root = f["diagnostics/master"]
        assert root.attrs["schema"] == "esfex.master_diagnostics/1"
        assert root.attrs["planning_mode"] == "perfect_foresight"
        assert set(root) == {"year_2030", "year_2031", "all"}
        b = root["year_2030/binding"]
        assert b.dtype.names[:5] == ("family", "label", "code", "kind", "units")
        assert {"count_binding", "count_total", "max_abs_dual", "sum_abs_dual"} <= set(
            b.dtype.names)
        assert b.shape == (4,)
        assert root["year_2030"].attrs["year"] == 2030
        assert bool(root["year_2030"].attrs["duals_available"])
        assert set(root["year_2030/slacks"].dtype.names) >= {
            "name", "value", "unit_cost", "cost"}
        assert root["year_2031/slacks"].shape == (0,)
    back = read_master_diagnostics(path)
    assert back["year_2030"]["binding"] == groups["year_2030"]["binding"]
    assert back["year_2030"]["slacks"] == groups["year_2030"]["slacks"]
    assert back["year_2030"]["top"] == groups["year_2030"]["top"]
    assert back["all"]["year"] is None
    assert back["year_2030"]["attrs"]["duals_available"] is True
    # A later write replaces the group of the same name only
    write_master_diagnostics(path, {"year_2030": dict(groups["year_2030"], slacks=[])})
    back = read_master_diagnostics(path)
    assert back["year_2030"]["slacks"] == []
    assert set(back) == {"year_2030", "year_2031", "all"}


def test_adapter_reports_nothing_without_a_monolithic_model():
    from esfex.bridge.adapters import MasterProblemAdapter

    a = object.__new__(MasterProblemAdapter)
    a._jl_model, a.solver_method = None, "monolithic"
    assert a.diagnostics() is None
    a._jl_model, a.solver_method = object(), "benders"
    assert a.diagnostics() is None


def test_runner_writes_nothing_without_diagnostics(tmp_path):
    from esfex.runner import Orchestrator

    orch = object.__new__(Orchestrator)
    orch.master_diagnostics = {}
    path = tmp_path / "r.h5"
    h5py.File(path, "w").close()
    orch._write_master_diagnostics(path)
    with h5py.File(path, "r") as f:
        assert "diagnostics" not in f


@pytest.mark.julia
def test_myopic_run_logs_digest_and_writes_each_year(tmp_path, caplog):
    sys.path.insert(0, "tests")
    from test_myopic_master_history import _system
    from test_runner import _make_esfex_config

    from esfex.config.schema import MasterProblemConfig, TemporalConfig
    from esfex.runner import Orchestrator

    sysc = _system("Island", tmp_path).model_copy(update={"max_annual_investment": 1e6})
    cfg = _make_esfex_config(
        sysc, simulation_mode="development",
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720,
                                overlap_hours=0),
        master_problem=MasterProblemConfig(planning_mode="myopic"))
    orch = Orchestrator(cfg, output_dir=tmp_path / "out")
    with caplog.at_level(logging.INFO, logger="esfex.runner"):
        orch.run(years=2, master_only=True)
    y1, y2 = sorted(orch.master_solution["investments"])
    path = orch._hdf5_path
    groups = read_master_diagnostics(path)
    assert set(groups) == {f"year_{y1}", f"year_{y2}"}
    g = groups[f"year_{y1}"]
    assert g["attrs"]["duals_available"] is True
    fam = {r["family"]: r for r in g["binding"]}
    # 1 M$ a year buys 5 MW of gas at 200 000 $/MW: the budget binds
    assert fam["budget"]["count_priced"] == 1
    assert fam["budget"]["max_abs_dual"] > 0
    assert fam["adequacy"]["count_total"] == 1
    assert all(np.isfinite(r["max_abs_dual"]) for r in g["binding"])
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith(f"Master {y1} binding: Annual budget (investment)") for m in msgs)
    assert any(m.startswith(f"Master {y2} paid slacks:") for m in msgs)
