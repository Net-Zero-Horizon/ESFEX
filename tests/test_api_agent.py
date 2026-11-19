"""esfex.api and the machine-readable CLI (--json on validate / info / run,
``esfex summary``), and the end-of-run warnings summary (/diagnostics/warnings).
"""

import copy
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import MethodType
from unittest.mock import MagicMock, patch

import h5py
import numpy as np
import pytest
import yaml
from typer.testing import CliRunner

from esfex.cli import app

runner = CliRunner()
FIXTURE = Path(__file__).parent / "fixtures" / "studio_export_island.yaml"
REPO = Path(__file__).resolve().parents[1]


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


from contextlib import contextmanager  # noqa: E402


@contextmanager
def caplog_at_info():
    """The records logged inside the block (root at DEBUG meanwhile)."""
    root = logging.getLogger()
    h, level = _ListHandler(), root.level
    root.addHandler(h)
    root.setLevel(logging.DEBUG)
    try:
        yield h.records
    finally:
        root.removeHandler(h)
        root.setLevel(level)


def _json(result) -> dict:
    """The one JSON document on stdout (CliRunner keeps stderr apart)."""
    return json.loads(result.stdout)


def _assert_envelope(doc: dict, command: str, ok: bool) -> None:
    assert doc["schema"] == f"esfex.{command}/1"
    assert doc["ok"] is ok
    assert isinstance(doc["errors"], list)
    for e in doc["errors"]:
        assert set(e) == {"code", "key", "message", "fix"}


@pytest.fixture
def case(tmp_path):
    """The island fixture copied into tmp_path, as a dict to edit and a writer."""
    data = yaml.safe_load(FIXTURE.read_text())

    def write(d=None, name="case.yaml"):
        p = tmp_path / name
        p.write_text(yaml.safe_dump(d if d is not None else data, sort_keys=False))
        return p

    return data, write


def _gen(data, key="solar"):
    sysname = next(iter(data["systems"]))
    return sysname, data["systems"][sysname]["generators"][key]


# ── Loader key paths ──────────────────────────────────────────────────────

class TestLoaderKeyPaths:
    def test_bad_value_names_its_yaml_path(self, case):
        from esfex.config.loader import ConfigLoadError, load_config

        data, write = case
        sysname, gen = _gen(data)
        gen["rated_power"] = "lots"
        with pytest.raises(ConfigLoadError) as ei:
            load_config(write())
        keys = [i["key"] for i in ei.value.issues]
        assert f"systems.{sysname}.generators.solar.rated_power" in keys
        assert f"at systems.{sysname}.generators.solar" in str(ei.value)

    def test_missing_required_key(self, case):
        from esfex.config.loader import ConfigLoadError, load_config

        data, write = case
        sysname, gen = _gen(data)
        del gen["life_time"]
        with pytest.raises(ConfigLoadError) as ei:
            load_config(write())
        (issue,) = ei.value.issues
        assert issue["key"] == f"systems.{sysname}.generators.solar.life_time"
        assert issue["type"] == "missing"

    def test_top_level_section(self, case):
        from esfex.config.loader import ConfigLoadError, load_config

        data, write = case
        data["temporal"]["resolution_hours"] = "hourly"
        with pytest.raises(ConfigLoadError) as ei:
            load_config(write())
        assert ei.value.issues[0]["key"] == "temporal.resolution_hours"

    def test_yaml_syntax_error(self, tmp_path):
        from esfex.config.loader import ConfigLoadError, load_config

        p = tmp_path / "bad.yaml"
        p.write_text("a: [1, 2\nb: c\n")
        with pytest.raises(ConfigLoadError) as ei:
            load_config(p)
        assert ei.value.issues[0]["type"] == "yaml"

    def test_validation_error_still_reaches_other_callers(self):
        """_convert_system re-raises pydantic's own error (load_system_config
        and the Studio catch ValidationError)."""
        from pydantic import ValidationError

        from esfex.config.loader import _convert_system

        with pytest.raises(ValidationError) as ei:
            _convert_system({"name": "s", "nodes": {"num_nodes": "two"}})
        assert getattr(ei.value, "_esfex_loc") == ("nodes",)


# ── validate_case / esfex validate --json ────────────────────────────────

class TestValidate:
    def test_valid_case(self, case):
        from esfex.api import validate_case

        _, write = case
        rep = validate_case(write())
        assert rep.ok and rep.errors == []
        s = rep.summary
        assert s["simulation_mode"] == "development"
        assert s["systems"][0]["generators"] == 2
        assert s["systems"][0]["generators_by_type"] == {"Renewable": 1, "Non-renewable": 1}
        # The loader's warnings come back as warnings, not on stdout
        assert any(w.code == "loader_warning" and "ramp" in w.message for w in rep.warnings)
        json.dumps(rep.to_dict())

    def test_invalid_case_keyed_errors(self, case):
        from esfex.api import validate_case

        data, write = case
        sysname, gen = _gen(data)
        gen["rated_power"] = "lots"
        rep = validate_case(write())
        assert not rep.ok and rep.summary is None
        err = next(e for e in rep.errors if e.key.endswith("solar.rated_power"))
        assert err.code == "list_type"
        assert err.fix  # a hint for a known error type
        assert "lots" in err.message

    def test_missing_availability_file(self, case):
        from esfex.api import validate_case

        data, write = case
        sysname, gen = _gen(data)
        gen["availability_file"] = "profiles/solar.csv"
        rep = validate_case(write())
        assert not rep.ok
        (err,) = rep.errors
        assert err.code == "missing_file"
        assert err.key == f"systems.{sysname}.generators.solar.availability_file"

        # Present under the config's directory: fine
        p = write()
        (p.parent / "profiles").mkdir()
        (p.parent / "profiles" / "solar.csv").write_text("1\n")
        assert validate_case(p).ok

    def test_unknown_keys_are_warnings(self, case):
        from esfex.api import validate_case

        data, write = case
        sysname, gen = _gen(data)
        gen["rated_powr"] = [1, 2]
        rep = validate_case(write())
        assert rep.ok
        assert [w.key for w in rep.warnings if w.code == "unknown_key"] == [
            f"systems.{sysname}.generators.solar.rated_powr"]
        assert not any("nknown configuration key" in w.message for w in rep.warnings
                       if w.code == "loader_warning")

    def test_cli_json_valid(self, case):
        _, write = case
        r = runner.invoke(app, ["validate", "-c", str(write()), "--json"])
        assert r.exit_code == 0
        doc = _json(r)
        _assert_envelope(doc, "validate", True)
        assert doc["case"]["systems"][0]["nodes"] == 2

    def test_cli_json_invalid_exit_1(self, case):
        data, write = case
        sysname, gen = _gen(data)
        del gen["life_time"]
        r = runner.invoke(app, ["validate", "-c", str(write()), "--json"])
        assert r.exit_code == 1
        doc = _json(r)
        _assert_envelope(doc, "validate", False)
        assert doc["errors"][0]["key"] == f"systems.{sysname}.generators.solar.life_time"
        assert doc["errors"][0]["code"] == "missing"

    def test_cli_usage_error_exit_2(self, tmp_path):
        r = runner.invoke(app, ["validate", "-c", str(tmp_path / "nope.yaml"), "--json"])
        assert r.exit_code == 2

    def test_cli_text_reports_missing_file(self, case):
        data, write = case
        _, gen = _gen(data)
        gen["availability_file"] = "missing.csv"
        r = runner.invoke(app, ["validate", "-c", str(write())])
        assert r.exit_code == 1
        assert "missing.csv" in r.output


# ── info --json ───────────────────────────────────────────────────────────

class TestInfo:
    def test_json_does_not_start_julia(self):
        with patch("esfex.bridge.julia_setup.get_julia",
                   side_effect=AssertionError("Julia started")):
            r = runner.invoke(app, ["info", "--json"])
        assert r.exit_code == 0, r.output
        doc = _json(r)
        _assert_envelope(doc, "info", True)
        assert doc["esfex"]["version"]
        assert doc["julia"]["probed"] is False
        names = {s["name"] for s in doc["solvers"]}
        assert {"highs", "gurobi"} <= names
        for s in doc["solvers"]:
            assert isinstance(s["licensed"], bool)
            assert isinstance(s["installed"], bool)
        highs = next(s for s in doc["solvers"] if s["name"] == "highs")
        assert highs["installed"] and highs["licensed"]
        assert "data_root" in doc["data"]

    def test_probe_loads_installed_solvers(self):
        from esfex.api import system_info

        jl = MagicMock()
        jl.seval.side_effect = lambda code: (
            "1.11.8" if code == "string(VERSION)"
            else "1.9.0" if code.startswith("string(pkgversion") else None)
        with patch("esfex.bridge.julia_setup.get_julia", return_value=jl):
            info = system_info(probe=True)
        assert info["julia"]["starts"] is True and info["julia"]["version"] == "1.11.8"
        highs = next(s for s in info["solvers"] if s["name"] == "highs")
        assert highs["loads"] is True and highs["version"] == "1.9.0"
        assert all(s["loads"] is None for s in info["solvers"] if not s["installed"])

    def test_gurobi_licence_by_location_only(self, tmp_path, monkeypatch):
        from esfex.api import environment

        lic = tmp_path / "gurobi.lic"
        monkeypatch.setenv("GRB_LICENSE_FILE", str(lic))
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "nohome")
        assert environment._licensed("gurobi", True) is False
        lic.write_text("SECRET")
        assert environment._licensed("gurobi", True) is True
        assert environment._licensed("gurobi", False) is False
        doc = json.dumps(environment.system_info())
        assert "SECRET" not in doc

    def test_api_import_is_qt_and_julia_free(self):
        code = ("import sys, esfex.api; "
                "bad = [m for m in sys.modules if m.startswith(('PySide6', 'juliacall'))]; "
                "print(bad)")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             env={**os.environ, "PYTHONPATH": str(REPO / "src")}, check=True)
        assert out.stdout.strip() == "[]"


# ── Warnings collector ───────────────────────────────────────────────────

class TestWarnings:
    def test_category_strips_numbers_and_names(self):
        from esfex.api.diagnostics import warning_category

        a = warning_category("esfex.runner", "Generator 'pv' has 3 hours at 1.5e3 MW")
        b = warning_category("esfex.runner", 'Generator "wind" has 17 hours at 20 MW')
        assert a == b == "esfex.runner: Generator '*' has # hours at # MW"
        # An apostrophe is not a quote; %-placeholders count as names / numbers
        assert (warning_category("x", "ESFEX's Julia %s (v%d) can't load")
                == "x: ESFEX's Julia '*' (v#) can't load")

    def test_groups_and_counts(self):
        from esfex.api.diagnostics import collect_warnings

        log = logging.getLogger("esfex.test_api")
        with collect_warnings() as c:
            for i in range(3):
                log.warning("Unit %r: %d steps shed", f"u{i}", i)
            log.warning(f"free text {42}")
            log.error("broken %s", "x")
            log.info("not collected")
        s = c.summary()
        assert s.total == 5 and s.kinds == 3 and s.errors == 1
        top = s.groups[0]
        assert top.count == 3 and top.message == "Unit 'u0': 0 steps shed"
        assert s.text().startswith("5 warnings in 3 kinds:")
        # Detached after the block
        log.warning("after")
        assert c.summary().total == 5

    def test_nested_collectors_share(self):
        from esfex.api.diagnostics import collect_warnings

        log = logging.getLogger("esfex.test_api")
        with collect_warnings() as outer:
            log.warning("one")
            with collect_warnings() as inner:
                assert inner is outer
                log.warning("two")
        assert outer.summary().total == 2

    def test_julia_logger_records_are_collected(self):
        from esfex.api.diagnostics import collect_warnings
        from esfex.bridge.julia_setup import _forward_julia_log

        with collect_warnings() as c:
            _forward_julia_log(30, "Window 3 infeasible\n  status = INFEASIBLE", False)
        (g,) = c.summary().groups
        assert g.logger == "esfex.julia"
        assert g.category == "esfex.julia: Window # infeasible"

    def test_write_and_read_back(self, tmp_path):
        from esfex.api.diagnostics import collect_warnings, read_warnings, write_warnings

        p = tmp_path / "r.h5"
        h5py.File(p, "w").close()
        log = logging.getLogger("esfex.test_api")
        with collect_warnings() as c:
            log.warning("a %d", 1)
            log.warning("a %d", 2)
            log.warning("b")
        write_warnings(p, c.summary())
        write_warnings(p, c.summary())  # replaces, no error
        w = read_warnings(p)
        assert w["total"] == 3 and w["kinds"] == 2
        assert w["count_by_category"] == {"esfex.test_api: a #": 2, "esfex.test_api: b": 1}
        assert w["groups"][0]["message"] == "a 1" and w["groups"][0]["count"] == 2
        with h5py.File(p, "r") as f:
            rec = f["diagnostics/warnings/records"]
            assert set(rec.dtype.names) == {"time", "level", "logger", "category",
                                            "message", "count"}
            json.loads(f["diagnostics/warnings"].attrs["count_by_category"])

    def test_json_string_rows_are_read(self, tmp_path):
        from esfex.api.diagnostics import read_warnings

        p = tmp_path / "r.h5"
        with h5py.File(p, "w") as f:
            g = f.create_group("diagnostics/warnings")
            rows = [json.dumps({"time": "t", "level": "WARNING", "logger": "x",
                                "category": "x: y", "message": "y", "count": 4})]
            g.create_dataset("records", data=rows, dtype=h5py.string_dtype())
        w = read_warnings(p)
        assert w["total"] == 4 and w["groups"][0]["category"] == "x: y"

    def test_orchestrator_writes_warnings_even_when_the_run_fails(self, tmp_path):
        from esfex.api.diagnostics import read_warnings
        from esfex.runner import Orchestrator

        path = tmp_path / "run.h5"

        def _run(self, **kw):
            h5py.File(path, "w").close()
            self._hdf5_path = path
            logging.getLogger("esfex.runner").warning("Year %d: %d windows failed", 2030, 2)
            raise RuntimeError("solver died")

        orch = MagicMock()
        orch._run = MethodType(_run, orch)
        orch._finish_warnings = MethodType(Orchestrator._finish_warnings, orch)
        with caplog_at_info() as records, pytest.raises(RuntimeError):
            Orchestrator.run(orch, years=1)
        assert orch.warnings_summary.total == 1
        w = read_warnings(path)
        assert w["groups"][0]["category"] == "esfex.runner: Year #: # windows failed"
        # The summary is a milestone: the basic console shows it
        (rec,) = [r for r in records if r.getMessage().startswith("The run logged")]
        assert rec.levelno == logging.INFO and rec.milestone
        assert "1 warning in 1 kind:" in rec.getMessage()

    def test_master_diagnostics_layouts(self, tmp_path):
        """Year groups and the single-solve group ``all``; a table as a
        group of column datasets or as JSON rows."""
        from esfex.api.diagnostics import read_master_diagnostics

        p = tmp_path / "r.h5"
        with h5py.File(p, "w") as f:
            g = f.create_group("diagnostics/master/all/binding")
            g.create_dataset("family", data=["budget", "re_target"], dtype=h5py.string_dtype())
            g.create_dataset("count_binding", data=[1, 0])
            y = f.create_group("diagnostics/master/year_2035")
            y.create_dataset("slacks", data=[json.dumps({"name": "s", "cost": 5.0})],
                             dtype=h5py.string_dtype())
        m = read_master_diagnostics(p)
        assert m["all"]["binding"] == [{"family": "budget", "count_binding": 1},
                                       {"family": "re_target", "count_binding": 0}]
        assert m["2035"]["slacks"] == [{"name": "s", "cost": 5.0}]
        assert m["2035"]["binding"] == []
        assert set(read_master_diagnostics(p, years={2030})) == {"all"}
        h5py.File(tmp_path / "old.h5", "w").close()
        assert read_master_diagnostics(tmp_path / "old.h5") is None


# ── Synthetic results files ──────────────────────────────────────────────

COMPONENTS = {"fuel_cost": 600.0, "fixed_om_cost": 300.0, "load_shedding_cost": 100.0,
              "v2g_compensation": 55.0, "pe_electrolyzer_cost": 7.0,
              "operating_budget_excess": 9.0}


def _write_results(path: Path, *, total=1000.0, diagnostics=True, years=(2030, 2031),
                   complete=True) -> Path:
    """A small results file shaped like the runner's (two nodes, 4 steps
    of 6 h)."""
    tres = 6
    with h5py.File(path, "w") as f:
        f.attrs.update({"num_nodes": 2, "num_years": len(years), "simulation_mode": "development",
                        "years_range": f"{years[0]}-{years[-1]}",
                        "temporal_resolution_hours": tres, "export_complete": complete,
                        "esfex_version": "0.4.0", "solver": "highs", "config_file": "/x/case.yaml",
                        "subsystem_names": ["island"]})
        sc_cfg = f.create_group("system_configuration")
        g0 = sc_cfg.create_group("generators/generator_0")
        g0.attrs.update({"name": "Diesel", "rated_power": "[30.0, 10.0]"})
        sc_cfg["generators"].attrs["num_generators"] = 1
        t0 = sc_cfg.create_group("technologies/technology_0")
        t0.attrs.update({"key": "pv", "name": "Solar PV"})
        sc_cfg["technologies"].attrs["num_technologies"] = 1
        b0 = sc_cfg.create_group("battery_technologies/battery_technology_0")
        b0.attrs.update({"key": "li", "name": "Li-ion"})
        sc_cfg["battery_technologies"].attrs["num_battery_technologies"] = 1
        for i, y in enumerate(years):
            sc = f.create_group(f"detailed_results/year_{y}_threshold_0")
            sc.attrs.update({"year": y, "feasible": True, "total_cost": total,
                             "renewable_penetration": 0.5, "co2_emissions": 200.0,
                             "total_generation": 1000.0, "total_demand": 1010.0,
                             "load_shed": 12.0, "transmission_losses": 5.0,
                             "master_re_target": 0.6, "solve_time": 1.5})
            if i == 0:
                sc.attrs["investment_tech_investment_power_0_0"] = 4.0
                sc.attrs["investment_tech_investment_power_0_1"] = 6.0
                sc.attrs["investment_bat_tech_investment_power_0_1"] = 2.0
                sc.attrs["investment_bat_tech_investment_capacity_0_1"] = 8.0
            else:
                sc.attrs["retirement_gen_0"] = 0.5
            # Shed 1 MW in two steps (one per node), 2 MW in another
            sc.create_dataset("loss_load", data=np.array([[1.0, 0, 2.0, 0], [0, 0, 0, 0]]))
            sc.create_dataset("curtailment", data=np.array([[5.0, 0, 0, 0], [0, 5.0, 0, 0]]))
            gen = sc.create_group("generation")
            d = gen.create_dataset("Diesel", data=np.zeros((2, 4)))
            d.attrs.update({"rated_power": np.array([30.0, 10.0]), "fuel": "Diesel"})
            pv = gen.create_dataset("Investment Solar PV", data=np.zeros((2, 4)))
            pv.attrs.update({"rated_power": np.array([4.0, 6.0]), "fuel": "Sun"})
            bd = sc.create_group("battery_discharge").create_dataset("Investment Li-ion",
                                                                     data=np.zeros((2, 4)))
            bd.attrs["rated_power"] = np.array([0.0, 2.0])
            cb = f.create_group(f"cost_breakdown/year_{y}")
            cb.attrs.update(COMPONENTS)
            cb.attrs["total"] = total
        if diagnostics:
            w = f.create_group("diagnostics/warnings")
            s = h5py.string_dtype()
            dt = np.dtype([("time", s), ("level", s), ("logger", s), ("category", s),
                           ("message", s), ("count", np.int64)])
            w.create_dataset("records", data=np.array(
                [("2026-01-01T00:00:00", "WARNING", "esfex.runner", "esfex.runner: x #", "x 1", 3)],
                dtype=dt))
            w.attrs["count_by_category"] = json.dumps({"esfex.runner: x #": 3})
            m = f.create_group(f"diagnostics/master/year_{years[0]}")
            bdt = np.dtype([("family", s), ("count_binding", np.int64), ("count_total", np.int64),
                            ("max_abs_dual", float), ("sum_abs_dual", float), ("units", s)])
            m.create_dataset("binding", data=np.array(
                [("re_target", 1, 1, 41.0, 41.0, "$/MWh"), ("budget", 0, 1, 0.0, 0.0, "$/$")],
                dtype=bdt))
            sdt = np.dtype([("name", s), ("value", float), ("unit_cost", float), ("cost", float)])
            m.create_dataset("slacks", data=np.array([("fre_shortfall", 2.0, 100.0, 200.0)],
                                                     dtype=sdt))
    return path


class TestSummary:
    def test_year_kpis(self, tmp_path):
        from esfex.api import summary

        s = summary(_write_results(tmp_path / "r.h5"))
        assert s.metadata["esfex_version"] == "0.4.0"
        assert s.metadata["solver"] == "highs"
        assert s.metadata["status"] == "complete"
        assert s.metadata["years"] == [2030, 2031]
        y0, y1 = s.years
        c = y0["costs"]
        assert c["total"] == 1000.0
        assert c["check"]["ok"] and c["check"]["sum_of_components"] == pytest.approx(1000.0)
        assert set(c["reporting"]) == {"v2g_compensation", "pe_electrolyzer_cost",
                                       "operating_budget_excess"}
        assert y0["re"] == {"share": 0.5, "target": 0.6, "met": False, "gap": pytest.approx(0.1)}
        sh = y0["shedding"]
        assert sh["energy_mwh"] == 12.0 and sh["hours"] == 12.0 and sh["peak_mw"] == 2.0
        cu = y0["curtailment"]
        assert cu["energy_mwh"] == 60.0
        assert cu["share"] == pytest.approx(60.0 / (60.0 + 500.0))
        assert y0["emissions"]["co2_t"] == 200.0
        assert y0["builds"]["generation_mw"] == {"Solar PV": 10.0}
        assert y0["builds"]["storage_mw"] == {"Li-ion": 2.0}
        assert y0["builds"]["storage_mwh"] == {"Li-ion": 8.0}
        assert y1["builds"]["retired"] == {"Diesel": {"share": 0.5, "mw": 20.0}}
        assert y0["capacity"]["generation_mw_by_fuel"] == {"Diesel": 40.0, "Sun": 10.0}
        assert y0["storage"]["in_service_total_mw"] == 2.0
        assert y0["losses"]["energy_mwh"] == 5.0 and y0["losses"]["loss_repair"] is None
        assert s.totals["total_cost"] == 2000.0 and s.totals["load_shed_mwh"] == 24.0
        assert s.checks == []
        assert s.warnings["total"] == 3
        b = s.master["2030"]["binding"]
        assert b[0]["family"] == "re_target" and b[0]["max_abs_dual"] == 41.0
        assert s.master["2030"]["slacks"][0]["cost"] == 200.0
        json.dumps(s.to_dict())

    def test_cost_mismatch_is_a_check(self, tmp_path):
        from esfex.api import summary

        s = summary(_write_results(tmp_path / "r.h5", total=1500.0), years=[2031])
        assert [y["year"] for y in s.years] == [2031]
        assert not s.years[0]["costs"]["check"]["ok"]
        assert s.checks[0]["code"] == "cost_components_mismatch"
        assert s.checks[0]["year"] == 2031

    def test_old_file_without_diagnostics(self, tmp_path):
        from esfex.api import summary

        p = _write_results(tmp_path / "r.h5", diagnostics=False, complete=False)
        with h5py.File(p, "a") as f:
            for k in ("esfex_version", "solver", "config_file"):
                del f.attrs[k]
            del f["cost_breakdown"]
        s = summary(p)
        assert s.metadata["esfex_version"] is None
        assert s.metadata["status"] == "incomplete"
        assert s.warnings is None and s.master is None
        assert s.years[0]["costs"]["source"] == "total_cost"
        assert s.years[0]["costs"]["total"] == 1000.0
        assert any("diagnostics/warnings" in n for n in s.notes)

    def test_sections_and_loss_repair(self, tmp_path):
        from esfex.api import summary

        p = _write_results(tmp_path / "r.h5")
        with h5py.File(p, "a") as f:
            f["detailed_results/year_2030_threshold_0"].attrs["loss_repair_gap"] = 0.02
        s = summary(p, sections=["losses", "re"])
        assert set(s.years[0]) >= {"losses", "re"}
        assert "costs" not in s.years[0] and "builds" not in s.years[0]
        assert s.years[0]["losses"]["loss_repair"] == {"loss_repair_gap": 0.02}
        assert "warnings" not in s.to_dict()
        with pytest.raises(ValueError):
            summary(p, sections=["nope"])

    def test_not_a_results_file(self, tmp_path):
        from esfex.api import ResultsFileError, summary

        txt = tmp_path / "x.h5"
        txt.write_text("not hdf5")
        with pytest.raises(ResultsFileError) as ei:
            summary(txt)
        assert ei.value.error.code == "not_hdf5"
        empty = tmp_path / "e.h5"
        h5py.File(empty, "w").close()
        with pytest.raises(ResultsFileError) as ei:
            summary(empty)
        assert ei.value.error.code == "not_a_results_file"

    def test_cli_json(self, tmp_path):
        p = _write_results(tmp_path / "r.h5")
        r = runner.invoke(app, ["summary", str(p), "--json", "--year", "2031",
                                "--section", "costs,re", "--section", "warnings"])
        assert r.exit_code == 0, r.output
        doc = _json(r)
        _assert_envelope(doc, "summary", True)
        assert doc["sections"] == ["costs", "re", "warnings"]
        assert [y["year"] for y in doc["years"]] == [2031]
        assert doc["warnings"]["total"] == 3
        assert "master" not in doc

    def test_cli_text(self, tmp_path):
        p = _write_results(tmp_path / "r.h5")
        r = runner.invoke(app, ["summary", str(p)])
        assert r.exit_code == 0, r.output
        assert "2030" in r.output and "Solar PV" in r.output and "re_target" in r.output

    def test_cli_errors(self, tmp_path):
        bad = tmp_path / "x.h5"
        bad.write_text("nope")
        r = runner.invoke(app, ["summary", str(bad), "--json"])
        assert r.exit_code == 1
        doc = _json(r)
        _assert_envelope(doc, "summary", False)
        assert doc["errors"][0]["code"] == "not_hdf5"
        p = _write_results(tmp_path / "r.h5")
        assert runner.invoke(app, ["summary", str(p), "--section", "bogus"]).exit_code == 2
        assert runner.invoke(app, ["summary", str(tmp_path / "missing.h5")]).exit_code == 2


# ── run --json (mocked Orchestrator) ─────────────────────────────────────

class _FakeOrch:
    feasible = True
    fail = None

    def __init__(self, cfg, output_dir, config_path):
        self.output_dir = Path(output_dir)
        self._hdf5_path = None
        self.results = []
        self.warnings_summary = None

    def run(self, years=None, start_year=None):
        print("solver chatter on stdout")
        logging.getLogger("esfex.runner").warning("Something odd in year %d", 2030)
        self._hdf5_path = _write_results(self.output_dir / "case_x.h5")
        if self.fail:
            raise self.fail
        self.results = [MagicMock(year=2030, feasible=True),
                        MagicMock(year=2031, feasible=type(self).feasible)]
        return self.results


class TestRunJson:
    def _invoke(self, case, tmp_path, orch=_FakeOrch, extra=()):
        _, write = case
        with patch("esfex.runner.Orchestrator", orch):
            return runner.invoke(app, ["run", "-c", str(write()), "-o", str(tmp_path / "out"),
                                       "--json", *extra])

    def test_success(self, case, tmp_path):
        r = self._invoke(case, tmp_path)
        assert r.exit_code == 0, r.output
        doc = _json(r)  # the chatter went to stderr
        _assert_envelope(doc, "run", True)
        assert doc["status"] == "complete"
        assert doc["output_file"].endswith("case_x.h5")
        assert doc["years"] == [2030, 2031]
        assert doc["last_year"]["year"] == 2031
        assert doc["wall_time_s"] >= 0
        # Collected by the CLI (the fake run has no collector of its own),
        # with the configuration load's warnings
        cats = [g["category"] for g in doc["warnings"]["groups"]]
        assert "esfex.runner: Something odd in year #" in cats
        assert any("ramp" in c for c in cats)
        assert doc["case"]["systems"][0]["name"]

    def test_failed_windows(self, case, tmp_path):
        class Orch(_FakeOrch):
            feasible = False

        r = self._invoke(case, tmp_path, Orch)
        assert r.exit_code == 1
        doc = _json(r)
        _assert_envelope(doc, "run", False)
        assert doc["status"] == "failed_windows"
        assert doc["errors"][0]["code"] == "failed_windows"

    def test_aborted(self, case, tmp_path):
        from esfex.runner import RunAbortedError

        class Orch(_FakeOrch):
            fail = RunAbortedError("the master problem found no plan")

        r = self._invoke(case, tmp_path, Orch)
        assert r.exit_code == 1
        doc = _json(r)
        assert doc["status"] == "aborted" and doc["errors"][0]["code"] == "aborted"
        assert doc["output_file"].endswith("case_x.h5")

    def test_invalid_config(self, case, tmp_path):
        data, write = case
        _, gen = _gen(data)
        del gen["life_time"]
        r = self._invoke(case, tmp_path)
        assert r.exit_code == 1
        doc = _json(r)
        assert doc["status"] == "invalid_config"
        assert doc["errors"][0]["key"].endswith("generators.solar.life_time")
        assert doc["output_file"] is None

    def test_dry_run(self, case, tmp_path):
        r = self._invoke(case, tmp_path, extra=("--dry-run",))
        assert r.exit_code == 0
        doc = _json(r)
        assert doc["ok"] and doc["status"] == "dry_run"


class TestStdoutGuard:
    def test_fd_level_writes_go_to_stderr(self, capfd):
        from esfex.cli import _StdoutToStderr, _write_json

        with _StdoutToStderr():
            os.write(1, b"raw fd write\n")
            print("python print")
        _write_json({"ok": True})
        out, err = capfd.readouterr()
        assert json.loads(out) == {"ok": True}
        assert "raw fd write" in err and "python print" in err


# ── A real run (Julia, HiGHS) ────────────────────────────────────────────

@pytest.mark.julia
def test_run_json_end_to_end(tmp_path):
    """`esfex run --json` in a subprocess (stdout a pipe, not a TTY): one
    JSON object on stdout, the results file has /diagnostics/warnings and
    the summary reads it."""
    data = yaml.safe_load(FIXTURE.read_text())
    sysname = next(iter(data["systems"]))
    np.savetxt(tmp_path / "demand.csv", np.full((8760, 2), 5.0), delimiter=",")
    data["systems"][sysname]["demand_path"] = "demand.csv"
    data["simulation_mode"] = "economic_dispatch"
    data["temporal"].update(resolution_hours=24, rolling_horizon_hours=8760, overlap_hours=0)
    data["enable_primary_energy"] = False
    cfg = tmp_path / "case.yaml"
    cfg.write_text(yaml.safe_dump(data, sort_keys=False))
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    out = subprocess.run(
        [sys.executable, "-m", "esfex.cli", "run", "-c", str(cfg), "-o", str(tmp_path / "out"),
         "-y", "1", "--json"], capture_output=True, text=True, env=env, timeout=900)
    doc = json.loads(out.stdout)
    _assert_envelope(doc, "run", out.returncode == 0)
    assert doc["exit_code"] == out.returncode
    assert doc["status"] in ("complete", "failed_windows")
    h5 = Path(doc["output_file"])
    assert h5.exists()
    with h5py.File(h5, "r") as f:
        assert f.attrs["solver"] == "highs"
        assert "esfex_version" in f.attrs
        assert "diagnostics/warnings/records" in f
    assert doc["last_year"]["year"] == doc["years"][-1]
    # The loader's ramp warning is in the run's warnings
    assert any("ramp" in g["category"] for g in doc["warnings"]["groups"])
