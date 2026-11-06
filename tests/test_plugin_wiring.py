"""Plugin hooks that ESFEX defined but never called.

- ``get_cli_commands``: the CLI only discovered plugins, so nothing was loaded
  and ``esfex <plugin> ...`` failed with "No such command".
- ``esfex plugin install --git`` had no way to pass ``trust_host``.
- ``get_config_schema`` / ``on_config_loaded`` were never called.
- ``get_tree_categories``, ``get_forms``, ``get_toolbar_actions`` and
  ``get_map_layers`` were looked up under window attributes the Studio's main
  window does not have.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

import esfex.plugins.manager as mod
from esfex import cli
from esfex.plugins.manager import PluginManager, _hash_directory, reset_plugin_manager
from esfex.plugins.protocol import ESFEXPlugin, PluginContext, PluginMeta

_CLI_PLUGIN = '''
import typer
from esfex.plugins.protocol import ESFEXPlugin

sub = typer.Typer(help="Carbon tools.")

@sub.command()
def report(path: str):
    print(f"carbon report for {path}")

@sub.command()
def other():
    print("other")

class P(ESFEXPlugin):
    def get_cli_commands(self):
        return [sub]

def create_plugin(ctx):
    return P(ctx)
'''


def _plugin(root: Path, name: str, body: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "plugin.json").write_text(json.dumps({"name": name, "version": "1.0"}))
    (d / "__init__.py").write_text(body)
    return d


def _trust(state_file: Path, *dirs: Path) -> None:
    state_file.write_text(json.dumps(
        {"trusted_hashes": {d.name: _hash_directory(d) for d in dirs}}))


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "_USER_PLUGINS_DIR", tmp_path / "plugins")
    monkeypatch.setattr(mod, "_USER_DATA_DIR", tmp_path / "plugin_data")
    monkeypatch.setattr(mod, "_STATE_FILE", tmp_path / "plugins.json")
    monkeypatch.delenv("ESFEX_PLUGIN_PATH", raising=False)
    reset_plugin_manager()
    groups = list(cli.app.registered_groups)
    yield tmp_path
    cli.app.registered_groups[:] = groups
    reset_plugin_manager()


def _entrypoint(argv, capsys):
    """Run the ``esfex`` entry point; return (exit code, stdout)."""
    with patch.object(sys, "argv", ["esfex", *argv]):
        with pytest.raises(SystemExit) as exc:
            cli._entrypoint()
    return exc.value.code, capsys.readouterr().out


# ── CLI sub-commands (#15) ─────────────────────────────────────────────


class TestPluginCliCommands:
    def test_trusted_plugin_command_runs(self, sandbox, capsys):
        d = _plugin(sandbox / "plugins", "carbon", _CLI_PLUGIN)
        _trust(sandbox / "plugins.json", d)
        code, out = _entrypoint(["carbon", "report", "x.h5"], capsys)
        assert code == 0
        assert "carbon report for x.h5" in out

    def test_help_lists_plugin_group(self, sandbox, capsys):
        d = _plugin(sandbox / "plugins", "carbon", _CLI_PLUGIN)
        _trust(sandbox / "plugins.json", d)
        code, out = _entrypoint(["--help"], capsys)
        assert code == 0
        assert "carbon" in out and "run" in out

    def test_untrusted_plugin_is_not_loaded(self, sandbox, capsys):
        trusted = _plugin(sandbox / "plugins", "other_plugin", "def create_plugin(c): pass\n")
        _trust(sandbox / "plugins.json", trusted)
        _plugin(sandbox / "plugins", "carbon", _CLI_PLUGIN)
        code, _ = _entrypoint(["carbon", "other"], capsys)
        assert code != 0

    def test_disabled_plugin_is_not_loaded(self, sandbox, capsys):
        d = _plugin(sandbox / "plugins", "carbon", _CLI_PLUGIN)
        _trust(sandbox / "plugins.json", d)
        PluginManager().disable("carbon")
        code, _ = _entrypoint(["carbon", "other"], capsys)
        assert code != 0

    def test_builtin_command_does_not_load_plugins(self, sandbox):
        # `esfex run` / `esfex studio` load plugins themselves, with a config
        d = _plugin(sandbox / "plugins", "carbon", _CLI_PLUGIN)
        _trust(sandbox / "plugins.json", d)
        cli._register_plugin_cli(["run", "config.yaml"])
        assert mod.get_plugin_manager().plugins == []
        cli._register_plugin_cli(["--help"])
        assert [p.meta.name for p in mod.get_plugin_manager().plugins] == ["carbon"]

    def test_broken_plugins_do_not_break_help(self, sandbox, capsys):
        good = _plugin(sandbox / "plugins", "carbon", _CLI_PLUGIN)
        broken = _plugin(sandbox / "plugins", "broken", "raise RuntimeError('boom')\n")
        bad_cli = _plugin(sandbox / "plugins", "bad_cli", (
            "from esfex.plugins.protocol import ESFEXPlugin\n"
            "class P(ESFEXPlugin):\n"
            "    def get_cli_commands(self):\n"
            "        return ['not a typer app']\n"
            "def create_plugin(ctx):\n"
            "    return P(ctx)\n"))
        clash = _plugin(sandbox / "plugins", "run", _CLI_PLUGIN)
        _trust(sandbox / "plugins.json", good, broken, bad_cli, clash)
        code, out = _entrypoint(["--help"], capsys)
        assert code == 0
        assert "carbon" in out and "bad_cli" not in out
        # The built-in `run` is untouched by the plugin named "run"
        code, out = _entrypoint(["run", "--help"], capsys)
        assert code == 0 and "Run the ESFEX optimization model" in out

    def test_several_apps_merge_under_plugin_name(self, sandbox):
        import typer

        a, b = typer.Typer(), typer.Typer()
        a.command("one")(lambda: print("one"))
        b.command("two")(lambda: print("two"))

        class P(ESFEXPlugin):
            meta = PluginMeta(name="multi", version="1.0")

            def get_cli_commands(self):
                return [a, b]

        pm = PluginManager()
        pm._plugins = [P(PluginContext(config=None, plugin_dir=sandbox, data_dir=sandbox))]
        app = typer.Typer()
        app.command("run")(lambda: None)
        pm.register_cli_commands(app)
        runner = CliRunner()
        assert runner.invoke(app, ["multi", "one"]).output.strip() == "one"
        assert runner.invoke(app, ["multi", "two"]).output.strip() == "two"


# ── plugin install --trust-host (#28) ──────────────────────────────────


class TestInstallTrustHost:
    @pytest.mark.parametrize("flag, expected", [([], False), (["--trust-host"], True)])
    def test_flag_reaches_install_from_git(self, sandbox, flag, expected):
        with patch.object(PluginManager, "install_from_git", return_value="x") as inst:
            result = CliRunner().invoke(
                cli.app, ["plugin", "install", "--git", "https://git.example.org/x.git", *flag])
        assert result.exit_code == 0, result.output
        assert inst.call_args.kwargs["trust_host"] is expected

    def test_untrusted_host_still_refused_without_flag(self, sandbox):
        result = CliRunner().invoke(
            cli.app, ["plugin", "install", "--git", "https://git.example.org/x.git"])
        assert result.exit_code == 1
        assert "--trust-host" in result.output


# ── get_config_schema / on_config_loaded (#16) ─────────────────────────

_CONFIG_PLUGIN = '''
from pydantic import BaseModel
from esfex.plugins.protocol import ESFEXPlugin

class Settings(BaseModel):
    cache_hours: int = 24

class P(ESFEXPlugin):
    seen = None

    def get_config_schema(self):
        return Settings

    def on_config_loaded(self, config):
        self.seen = config

def create_plugin(ctx):
    return P(ctx)
'''


class TestConfigHooks:
    def _config(self, section):
        sys.path.insert(0, "tests")
        from test_runner import _make_esfex_config

        return _make_esfex_config(plugins={"cfgplug": section})

    def test_apply_config_validates_and_calls_hook(self, sandbox):
        d = _plugin(sandbox / "plugins", "cfgplug", _CONFIG_PLUGIN)
        _trust(sandbox / "plugins.json", d)
        pm = PluginManager()
        pm.load_all(config=None)
        cfg = self._config({"cache_hours": 6})
        assert pm.apply_config(cfg) == []
        plugin = pm.plugins[0]
        assert plugin.seen is cfg and plugin.context.config is cfg

        bad = self._config({"cache_hours": "soon"})
        errors = pm.apply_config(bad)
        assert len(errors) == 1 and errors[0].startswith("plugins.cfgplug:")
        assert plugin.seen is cfg  # not called with the invalid config

    def test_run_stops_on_invalid_plugin_section(self, sandbox, tmp_path):
        from esfex.runner import Orchestrator

        d = _plugin(sandbox / "plugins", "cfgplug", _CONFIG_PLUGIN)
        _trust(sandbox / "plugins.json", d)
        with pytest.raises(ValueError, match="plugins.cfgplug"):
            Orchestrator(self._config({"cache_hours": "soon"}), output_dir=tmp_path / "out")

    def test_run_calls_on_config_loaded(self, sandbox, tmp_path):
        from esfex.runner import Orchestrator

        d = _plugin(sandbox / "plugins", "cfgplug", _CONFIG_PLUGIN)
        _trust(sandbox / "plugins.json", d)
        cfg = self._config({"cache_hours": 6})
        Orchestrator(cfg, output_dir=tmp_path / "out")
        assert mod.get_plugin_manager().plugins[0].seen is cfg


# ── GUI hooks on the real main window (#92) ────────────────────────────


@pytest.fixture(scope="module")
def main_window():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from esfex.visualization.main_window import MainWindow

    from esfex.visualization.panels import element_tree as et

    registries = (list(et._CATEGORY_KEYS), dict(et._ELEMENT_TYPE_TO_CATEGORY),
                  dict(et._PLUGIN_CATEGORY_LABELS))
    window = MainWindow()
    yield window
    window.close()
    # Plugin categories are module-level registries: don't leak them
    et._CATEGORY_KEYS[:] = registries[0]
    for reg, saved in ((et._ELEMENT_TYPE_TO_CATEGORY, registries[1]),
                       (et._PLUGIN_CATEGORY_LABELS, registries[2])):
        reg.clear()
        reg.update(saved)


def test_gui_hooks_reach_main_window(main_window, sandbox):
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QLabel

    seen = {}

    class Form(QLabel):
        def load_element(self, element_id):
            seen["loaded"] = element_id

    class P(ESFEXPlugin):
        meta = PluginMeta(name="guiplug", version="1.0")

        def get_tree_categories(self):
            return [{"key": "co2_sources", "label": "CO2 Sources", "element_type": "co2_source"}]

        def get_forms(self, model):
            seen["model"] = model
            return [("co2_source", Form("co2"))]

        def get_toolbar_actions(self, toolbar, window):
            self.action = QAction("Carbon report", window)
            return [self.action]

        def get_map_layers(self, map_widget):
            seen["map"] = map_widget

    w = main_window
    tree = w.element_tree
    tree.add_system("Main")
    tree.set_current_system("Main")
    pm = PluginManager()
    plugin = P(PluginContext(config=None, plugin_dir=sandbox, data_dir=sandbox, gui_mode=True))
    pm._plugins = [plugin]
    pm.register_gui_extensions(w)

    assert seen["model"] is w.model
    assert seen["map"] is w.map_widget
    assert plugin.action in w.toolbar.actions()
    assert w.properties_panel.get_form("co2_source") is not None

    root = tree._system_roots["Main"]["co2_sources"]
    assert root.text(0).startswith("CO2 Sources")
    tree.add_plugin_element("co2_sources", "s1", "Source 1")
    assert root.child(0).data(0, 100) == ("co2_source", "s1")
    w.properties_panel.show_element("co2_source", "s1")
    assert seen["loaded"] == "s1"
    # Systems added later get the category with its label
    tree.add_system("Later")
    assert tree._system_roots["Later"]["co2_sources"].text(0).startswith("CO2 Sources")
