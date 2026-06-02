"""
Additive tests for esfex.cli, targeting branches not covered by
tests/test_cli.py.

Focus areas (Missing line numbers from coverage):
  - _force_blocking_stdio / _safe_console_print BlockingIOError fallback
  - studio command (ImportError + saved/not-saved paths)
  - plugin install / uninstall / enable / disable commands
  - precompile command (up-to-date, stale, build success/failure)
  - info command sysimage + solver branches
  - train_demand_model (success, ImportError, generic failure)
  - build_demand_dataset (success, failure)
  - _show_config_summary verbose path
  - _check_julia_solver branches

Commands import their dependencies locally, so we patch at origin modules.
"""

import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

import esfex.cli as cli
from esfex.cli import app

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _plain(result) -> str:
    return _ANSI_RE.sub("", result.output)


# ---------------------------------------------------------------------------
# Helper: a lightweight config object for _show_config_summary
# ---------------------------------------------------------------------------

def _make_cfg():
    sys_cfg = SimpleNamespace(
        num_nodes=2,
        generators={"g0": object()},
        batteries={},
    )
    return SimpleNamespace(
        simulation_mode="development",
        solver=SimpleNamespace(name="highs", verbose=False),
        meta_network=SimpleNamespace(systems=["sys_a"]),
        systems={"sys_a": sys_cfg},
        temporal=SimpleNamespace(use_rolling_horizon=True),
        enable_primary_energy=False,
        n1_security=SimpleNamespace(enabled=False),
        logging=SimpleNamespace(console_level="basic"),
    )


# ---------------------------------------------------------------------------
# _force_blocking_stdio
# ---------------------------------------------------------------------------

class TestForceBlockingStdio:
    def test_runs_without_error(self):
        # Best-effort no-op; just confirm it doesn't raise on real stdio.
        cli._force_blocking_stdio()

    def test_handles_fileno_error(self):
        bad = SimpleNamespace()  # no fileno attribute

        def boom():
            raise ValueError("no fd")

        bad.fileno = boom
        with patch.object(cli.sys, "stdout", bad), \
             patch.object(cli.sys, "stderr", bad):
            # Should swallow the ValueError from fileno() and return cleanly.
            cli._force_blocking_stdio()

    def test_clears_nonblock_flag(self):
        import os
        captured = {}

        class FakeFcntl:
            F_GETFL = 3
            F_SETFL = 4

            def fcntl(self, fd, op, *args):
                if op == self.F_GETFL:
                    return os.O_NONBLOCK  # pretend non-blocking is set
                captured["set"] = args[0] if args else None
                return 0

        fake = FakeFcntl()
        stream = SimpleNamespace(fileno=lambda: 1)
        with patch.dict(sys.modules, {"fcntl": fake}), \
             patch.object(cli.sys, "stdout", stream), \
             patch.object(cli.sys, "stderr", SimpleNamespace(fileno=lambda: 2)):
            cli._force_blocking_stdio()
        # The O_NONBLOCK bit should have been cleared in the F_SETFL call.
        assert "set" in captured
        assert not (captured["set"] & os.O_NONBLOCK)


# ---------------------------------------------------------------------------
# _show_config_summary (exercised via validate, but call directly too)
# ---------------------------------------------------------------------------

class TestShowConfigSummary:
    def test_direct_call(self):
        # Just ensure it renders without raising for a plausible cfg.
        cli._show_config_summary(_make_cfg())


# ---------------------------------------------------------------------------
# run command: verbose + summary + safe print
# ---------------------------------------------------------------------------

class TestRunVerbose:
    def test_verbose_success_path(self, tmp_path):
        cfg_path = tmp_path / "c.yaml"
        cfg_path.write_text("simulation_mode: development\n")
        cfg = _make_cfg()
        orch = MagicMock()
        orch.run.return_value = {}
        with patch("esfex.config.loader.load_config", return_value=cfg), \
             patch("esfex.runner.Orchestrator", return_value=orch), \
             patch("esfex.logging_config.setup_console_logging"):
            result = runner.invoke(
                app,
                ["run", "--config", str(cfg_path), "--verbose",
                 "--output", str(tmp_path / "out")],
            )
        assert result.exit_code == 0
        assert orch.run.called

    def test_debug_console_level_forces_solver_verbose(self, tmp_path):
        cfg_path = tmp_path / "c.yaml"
        cfg_path.write_text("simulation_mode: development\n")
        cfg = _make_cfg()
        cfg.logging.console_level = "debug"
        orch = MagicMock()
        orch.run.return_value = {}
        with patch("esfex.config.loader.load_config", return_value=cfg), \
             patch("esfex.runner.Orchestrator", return_value=orch), \
             patch("esfex.logging_config.setup_console_logging"):
            result = runner.invoke(app, ["run", "--config", str(cfg_path)])
        assert result.exit_code == 0
        # debug branch flips solver.verbose True
        assert cfg.solver.verbose is True

    def test_blocking_io_error_on_run(self, tmp_path):
        cfg_path = tmp_path / "c.yaml"
        cfg_path.write_text("simulation_mode: development\n")
        cfg = _make_cfg()
        orch = MagicMock()
        orch.run.side_effect = BlockingIOError("pipe full")
        with patch("esfex.config.loader.load_config", return_value=cfg), \
             patch("esfex.runner.Orchestrator", return_value=orch), \
             patch("esfex.logging_config.setup_console_logging"):
            result = runner.invoke(
                app, ["run", "--config", str(cfg_path),
                      "--output", str(tmp_path / "out")]
            )
        assert result.exit_code == 1

    def test_verbose_failure_prints_traceback(self, tmp_path):
        cfg_path = tmp_path / "c.yaml"
        cfg_path.write_text("simulation_mode: development\n")
        cfg = _make_cfg()
        orch = MagicMock()
        orch.run.side_effect = RuntimeError("kaboom")
        with patch("esfex.config.loader.load_config", return_value=cfg), \
             patch("esfex.runner.Orchestrator", return_value=orch), \
             patch("esfex.logging_config.setup_console_logging"):
            result = runner.invoke(
                app, ["run", "--config", str(cfg_path), "--verbose",
                      "--output", str(tmp_path / "out")]
            )
        assert result.exit_code == 1
        assert "kaboom" in result.output


# ---------------------------------------------------------------------------
# studio command
# ---------------------------------------------------------------------------

class TestStudio:
    def test_import_error(self):
        # Force the `from esfex.visualization import launch_studio` to fail.
        with patch.dict(sys.modules, {"esfex.visualization": None}):
            result = runner.invoke(app, ["studio"])
        assert result.exit_code == 1
        assert "PySide6" in result.output

    def test_saved(self, tmp_path):
        fake_vis = SimpleNamespace(launch_studio=MagicMock(return_value=True))
        with patch.dict(sys.modules, {"esfex.visualization": fake_vis}):
            result = runner.invoke(
                app, ["studio", "--config", str(tmp_path / "in.yaml"),
                      "--output", str(tmp_path / "out.yaml")]
            )
        assert result.exit_code == 0
        assert "saved" in result.output.lower()

    def test_not_saved(self):
        fake_vis = SimpleNamespace(launch_studio=MagicMock(return_value=False))
        with patch.dict(sys.modules, {"esfex.visualization": fake_vis}):
            result = runner.invoke(app, ["studio"])
        assert result.exit_code == 0
        assert "without saving" in result.output.lower()


# ---------------------------------------------------------------------------
# plugin install / uninstall / enable / disable
# ---------------------------------------------------------------------------

class TestPluginCommands:
    def _pm(self):
        return MagicMock()

    def test_install_git_success(self):
        pm = self._pm()
        pm.install_from_git.return_value = "myplugin"
        with patch("esfex.plugins.get_plugin_manager", return_value=pm):
            result = runner.invoke(
                app, ["plugin", "install", "--git", "https://x/y.git"]
            )
        assert result.exit_code == 0
        assert "myplugin" in result.output
        pm.install_from_git.assert_called_once()

    def test_install_git_failure(self):
        pm = self._pm()
        pm.install_from_git.side_effect = RuntimeError("clone failed")
        with patch("esfex.plugins.get_plugin_manager", return_value=pm):
            result = runner.invoke(
                app, ["plugin", "install", "--git", "https://x/y.git"]
            )
        assert result.exit_code == 1
        assert "clone failed" in result.output

    def test_install_zip_success(self, tmp_path):
        zip_path = tmp_path / "p.zip"
        zip_path.write_text("dummy")
        pm = self._pm()
        pm.install_from_zip.return_value = "zipplugin"
        with patch("esfex.plugins.get_plugin_manager", return_value=pm):
            result = runner.invoke(
                app, ["plugin", "install", "--zip", str(zip_path)]
            )
        assert result.exit_code == 0
        assert "zipplugin" in result.output

    def test_install_zip_failure(self, tmp_path):
        zip_path = tmp_path / "p.zip"
        zip_path.write_text("dummy")
        pm = self._pm()
        pm.install_from_zip.side_effect = RuntimeError("bad zip")
        with patch("esfex.plugins.get_plugin_manager", return_value=pm):
            result = runner.invoke(
                app, ["plugin", "install", "--zip", str(zip_path)]
            )
        assert result.exit_code == 1
        assert "bad zip" in result.output

    def test_install_no_source(self):
        pm = self._pm()
        with patch("esfex.plugins.get_plugin_manager", return_value=pm):
            result = runner.invoke(app, ["plugin", "install"])
        assert result.exit_code == 1
        assert "--git" in result.output or "--zip" in result.output

    def test_uninstall(self):
        pm = self._pm()
        with patch("esfex.plugins.get_plugin_manager", return_value=pm):
            result = runner.invoke(app, ["plugin", "uninstall", "foo"])
        assert result.exit_code == 0
        pm.uninstall.assert_called_once_with("foo")
        assert "foo" in result.output

    def test_enable(self):
        pm = self._pm()
        with patch("esfex.plugins.get_plugin_manager", return_value=pm):
            result = runner.invoke(app, ["plugin", "enable", "bar"])
        assert result.exit_code == 0
        pm.enable.assert_called_once_with("bar")
        assert "bar" in result.output

    def test_disable(self):
        pm = self._pm()
        with patch("esfex.plugins.get_plugin_manager", return_value=pm):
            result = runner.invoke(app, ["plugin", "disable", "baz"])
        assert result.exit_code == 0
        pm.disable.assert_called_once_with("baz")
        assert "baz" in result.output

    def test_list_with_missing_meta(self, tmp_path, monkeypatch):
        # discover returns a name whose meta is None -> continue branch (445)
        pm = MagicMock()
        pm.discover.return_value = ["ghost", "real"]
        real_meta = SimpleNamespace(
            version="1.0", category="tools", description="A real plugin"
        )
        pm.metas = {"real": real_meta}  # "ghost" missing
        pm.is_enabled.return_value = True
        with patch("esfex.plugins.get_plugin_manager", return_value=pm):
            result = runner.invoke(app, ["plugin", "list"])
        assert result.exit_code == 0
        assert "real" in result.output


# ---------------------------------------------------------------------------
# precompile command
# ---------------------------------------------------------------------------

class TestPrecompile:
    def test_up_to_date(self):
        with patch("esfex.bridge.julia_setup._find_sysimage",
                   return_value=Path("/tmp/sys.so")), \
             patch("esfex.bridge.julia_setup._sysimage_is_stale",
                   return_value=False), \
             patch("esfex.bridge.julia_setup.precompile_esfex"):
            result = runner.invoke(app, ["precompile"])
        assert result.exit_code == 0
        assert "up to date" in result.output.lower()

    def test_stale_rebuild_success(self, tmp_path):
        built = tmp_path / "sys.so"
        built.write_bytes(b"x" * 2048)
        with patch("esfex.bridge.julia_setup._find_sysimage",
                   return_value=Path("/tmp/old.so")), \
             patch("esfex.bridge.julia_setup._sysimage_is_stale",
                   return_value=True), \
             patch("esfex.bridge.julia_setup.precompile_esfex",
                   return_value=built):
            result = runner.invoke(app, ["precompile"])
        assert result.exit_code == 0
        assert "stale" in result.output.lower()
        assert "successfully" in result.output.lower()

    def test_no_existing_build_success(self, tmp_path):
        built = tmp_path / "sys.so"
        built.write_bytes(b"x" * 1024)
        with patch("esfex.bridge.julia_setup._find_sysimage",
                   return_value=None), \
             patch("esfex.bridge.julia_setup._sysimage_is_stale",
                   return_value=False), \
             patch("esfex.bridge.julia_setup.precompile_esfex",
                   return_value=built):
            result = runner.invoke(app, ["precompile"])
        assert result.exit_code == 0
        assert "Building" in result.output

    def test_build_failure(self):
        with patch("esfex.bridge.julia_setup._find_sysimage",
                   return_value=None), \
             patch("esfex.bridge.julia_setup._sysimage_is_stale",
                   return_value=False), \
             patch("esfex.bridge.julia_setup.precompile_esfex",
                   return_value=None):
            result = runner.invoke(app, ["precompile"])
        assert result.exit_code == 1
        assert "failed" in result.output.lower()

    def test_force_rebuild_when_up_to_date(self, tmp_path):
        built = tmp_path / "sys.so"
        built.write_bytes(b"x" * 512)
        # existing + up-to-date but --force -> skip early return, rebuild
        with patch("esfex.bridge.julia_setup._find_sysimage",
                   return_value=Path("/tmp/old.so")), \
             patch("esfex.bridge.julia_setup._sysimage_is_stale",
                   return_value=False), \
             patch("esfex.bridge.julia_setup.precompile_esfex",
                   return_value=built):
            result = runner.invoke(app, ["precompile", "--force"])
        assert result.exit_code == 0
        assert "successfully" in result.output.lower()


# ---------------------------------------------------------------------------
# info command
# ---------------------------------------------------------------------------

class TestInfo:
    def test_info_with_sysimage_up_to_date_and_julia(self, tmp_path):
        img = tmp_path / "sys.so"
        img.write_bytes(b"x" * 4096)
        fake_jl = SimpleNamespace(Main=object())
        with patch.dict(sys.modules, {"juliacall": fake_jl}), \
             patch("esfex.bridge.julia_setup._find_sysimage", return_value=img), \
             patch("esfex.bridge.julia_setup._sysimage_is_stale",
                   return_value=False), \
             patch("esfex.config.solver.get_solver_info",
                   return_value={"available": True, "version": "1.2"}):
            result = runner.invoke(app, ["info"])
        assert result.exit_code == 0
        assert "UP TO DATE" in result.output

    def test_info_with_stale_sysimage(self, tmp_path):
        img = tmp_path / "sys.so"
        img.write_bytes(b"x" * 4096)
        # No juliacall -> ImportError branch
        with patch.dict(sys.modules, {"juliacall": None}), \
             patch("esfex.bridge.julia_setup._find_sysimage", return_value=img), \
             patch("esfex.bridge.julia_setup._sysimage_is_stale",
                   return_value=True):
            result = runner.invoke(app, ["info"])
        assert result.exit_code == 0
        assert "STALE" in result.output
        assert "Not available" in result.output

    def test_info_no_sysimage(self):
        with patch.dict(sys.modules, {"juliacall": None}), \
             patch("esfex.bridge.julia_setup._find_sysimage", return_value=None):
            result = runner.invoke(app, ["info"])
        assert result.exit_code == 0
        assert "Not built" in result.output


# ---------------------------------------------------------------------------
# _check_julia_solver
# ---------------------------------------------------------------------------

class TestCheckJuliaSolver:
    def test_available_with_version(self, capsys):
        with patch("esfex.config.solver.get_solver_info",
                   return_value={"available": True, "version": "9.5"}):
            cli._check_julia_solver("Gurobi")

    def test_available_without_version(self):
        with patch("esfex.config.solver.get_solver_info",
                   return_value={"available": True}):
            cli._check_julia_solver("HiGHS")

    def test_not_available(self):
        with patch("esfex.config.solver.get_solver_info",
                   return_value={"available": False}):
            cli._check_julia_solver("CPLEX")

    def test_error_branch(self):
        with patch("esfex.config.solver.get_solver_info",
                   side_effect=RuntimeError("boom")):
            cli._check_julia_solver("CPLEX")


# ---------------------------------------------------------------------------
# train_demand_model
# ---------------------------------------------------------------------------

class TestTrainDemandModel:
    def test_success(self):
        fake_mod = SimpleNamespace(train_demand_model=MagicMock(return_value="model"))
        with patch.dict(sys.modules, {"esfex.models.demand_training": fake_mod}):
            result = runner.invoke(app, ["train-demand-model"])
        assert result.exit_code == 0
        assert "trained successfully" in result.output.lower()
        # progress_cb is passed and callable; invoke it to cover on_progress
        kwargs = fake_mod.train_demand_model.call_args.kwargs
        kwargs["progress_cb"](50, "halfway")

    def test_import_error(self):
        fake_mod = SimpleNamespace(
            train_demand_model=MagicMock(side_effect=ImportError("no xgboost"))
        )
        with patch.dict(sys.modules, {"esfex.models.demand_training": fake_mod}):
            result = runner.invoke(app, ["train-demand-model"])
        assert result.exit_code == 1
        assert "Missing dependency" in result.output

    def test_generic_failure(self):
        fake_mod = SimpleNamespace(
            train_demand_model=MagicMock(side_effect=RuntimeError("train boom"))
        )
        with patch.dict(sys.modules, {"esfex.models.demand_training": fake_mod}):
            result = runner.invoke(app, ["train-demand-model"])
        assert result.exit_code == 1
        assert "Training failed" in result.output


# ---------------------------------------------------------------------------
# build_demand_dataset
# ---------------------------------------------------------------------------

class TestBuildDemandDataset:
    def test_success(self):
        fake_mod = SimpleNamespace(
            build_dataset=MagicMock(
                return_value={"n_countries": 12, "n_country_years": 240}
            )
        )
        with patch.dict(sys.modules, {"esfex.models.demand_dataset": fake_mod}):
            result = runner.invoke(
                app, ["build-demand-dataset", "--sources", "opsd,entsoe"]
            )
        assert result.exit_code == 0
        assert "12 countries" in _plain(result)
        kwargs = fake_mod.build_dataset.call_args.kwargs
        assert kwargs["sources"] == ["opsd", "entsoe"]
        kwargs["progress_cb"](10, "downloading")

    def test_failure(self):
        fake_mod = SimpleNamespace(
            build_dataset=MagicMock(side_effect=RuntimeError("dl boom"))
        )
        with patch.dict(sys.modules, {"esfex.models.demand_dataset": fake_mod}):
            result = runner.invoke(app, ["build-demand-dataset"])
        assert result.exit_code == 1
        assert "Dataset build failed" in result.output


# ---------------------------------------------------------------------------
# _register_plugin_cli (failure path swallowed)
# ---------------------------------------------------------------------------

class TestRegisterPluginCli:
    def test_swallows_exceptions(self):
        with patch("esfex.plugins.get_plugin_manager",
                   side_effect=RuntimeError("no plugins")):
            # Should not raise.
            cli._register_plugin_cli()


# ---------------------------------------------------------------------------
# _entrypoint
# ---------------------------------------------------------------------------

class TestEntrypoint:
    def test_entrypoint_invokes_app(self):
        with patch.object(cli, "app") as fake_app:
            cli._entrypoint()
        fake_app.assert_called_once()
