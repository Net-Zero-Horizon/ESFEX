"""Studio projects and their results.

* A Studio run writes to ``<project folder>/results`` (an unsaved project: the
  ESFEX cache dir), not to the ``results`` folder of the Studio's CWD.
* The project remembers its last results file in the GUI-only top-level key
  ``_last_results`` (relative to the config folder); reopening the project
  re-enables Results and offers, without loading, to open the file. A missing
  file is forgotten without an error.
* Export Project bundles the results only on request; Import restores them and
  the remembered file.

HOME / XDG dirs and the preferences module point at a tmp folder in every test,
so the user's preferences, recent files and caches are never touched.
"""

from __future__ import annotations

import os
import time
import zipfile
from pathlib import Path

import pytest
import yaml

from esfex.visualization.data import project_results as PR

FIXTURE = Path(__file__).parent / "fixtures" / "studio_export_island.yaml"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated HOME, XDG dirs and preferences file."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    for var in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        monkeypatch.setenv(var, str(h / var.lower()))
    pytest.importorskip("PySide6")
    from esfex.visualization import preferences as prefs

    monkeypatch.setattr(prefs, "_PREFS_DIR", h / ".config" / "esfex")
    monkeypatch.setattr(prefs, "_PREFS_FILE", h / ".config" / "esfex" / "preferences.json")
    return h


def _h5(path: Path, payload: bytes = b"fake-results") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


# ── Pure helpers ──────────────────────────────────────────────────────────

def test_project_output_dir_is_under_the_config_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "proj" / "cfg.yaml"
    out = PR.project_output_dir(str(cfg))
    assert out == (tmp_path / "proj" / "results")
    assert out.is_absolute()
    # A relative config path still gives an absolute folder (the run's CWD
    # must not matter).
    assert PR.project_output_dir("proj/cfg.yaml") == tmp_path / "proj" / "results"


def test_unsaved_project_runs_go_to_the_cache_dir(home):
    assert PR.project_output_dir(None) == home / ".cache" / "esfex" / "studio_results"
    assert PR.project_output_dir("") == home / ".cache" / "esfex" / "studio_results"


def test_results_ref_relative_under_config_folder_else_absolute(tmp_path):
    proj = tmp_path / "proj"
    assert PR.results_ref_for(proj / "results" / "r.h5", proj) == "results/r.h5"
    other = tmp_path / "elsewhere" / "r.h5"
    assert PR.results_ref_for(other, proj) == str(other)


def test_remembered_results_resolves_and_ignores_missing(tmp_path):
    proj = tmp_path / "proj"
    f = _h5(proj / "results" / "r.h5")
    assert PR.remembered_results({"_last_results": "results/r.h5"}, proj) == f
    assert PR.remembered_results({"_last_results": str(f)}, None) == f
    # Missing file, no key, junk value → nothing remembered, no error.
    assert PR.remembered_results({"_last_results": "results/gone.h5"}, proj) is None
    assert PR.remembered_results({}, proj) is None
    assert PR.remembered_results(None, proj) is None
    assert PR.remembered_results({"_last_results": 3}, proj) is None
    assert PR.remembered_results({"_last_results": "r.h5"}, None) is None


def test_newest_results_file_skips_mga_and_older_files(tmp_path):
    d = tmp_path / "results"
    old = _h5(d / "results_old.h5")
    os.utime(old, (1_000, 1_000))
    new = _h5(d / "results_new.h5")
    os.utime(new, (5_000, 5_000))
    mga = _h5(d / "mga_x.h5")
    os.utime(mga, (9_000, 9_000))
    assert PR.newest_results_file(d) == new
    assert PR.newest_results_file(d, since=6_000) is None
    assert PR.newest_results_file(tmp_path / "none") is None


def test_format_size():
    assert PR.format_size(512) == "512 B"
    assert PR.format_size(2048) == "2.0 KB"
    assert PR.format_size(int(4.7 * 1024 ** 3)) == "4.7 GB"


def _island_states(folder: Path):
    from esfex.config.loader import load_config
    from esfex.visualization.data.serializer import config_to_gui_states

    cfg_path = _project(folder)
    cfg = load_config(str(cfg_path))
    return cfg_path, cfg, config_to_gui_states(cfg, base_dir=str(folder))


def test_serializer_writes_gui_only_key_the_loader_accepts(tmp_path):
    from esfex.config.loader import load_config
    from esfex.visualization.data.serializer import gui_state_to_yaml

    cfg_path, cfg, states = _island_states(tmp_path / "proj")
    res = _h5(tmp_path / "proj" / "results" / "r.h5")
    out = tmp_path / "proj" / "saved.yaml"
    gui_state_to_yaml(states, cfg, out, last_results_file=str(res),
                      data_base_dir=str(cfg_path.parent))
    raw = yaml.safe_load(out.read_text())
    assert raw["_last_results"] == "results/r.h5"
    # The CLI's unknown-key check does not report it.
    assert "_last_results" not in load_config(str(out)).unknown_keys
    # Without a results file the key is not written.
    gui_state_to_yaml(states, cfg, out, data_base_dir=str(cfg_path.parent))
    assert "_last_results" not in yaml.safe_load(out.read_text())


def test_export_without_results_bundles_none(tmp_path):
    from esfex.visualization.data import project_bundle as PB

    cfg_path, cfg, states = _island_states(tmp_path / "proj")
    _h5(tmp_path / "proj" / "results" / "r.h5")
    dest = tmp_path / "p.esfexp"
    rep = PB.export_project(states, cfg, dest, src_base=str(cfg_path.parent))
    assert rep.results is None
    names = zipfile.ZipFile(dest).namelist()
    assert not any(n.startswith("results/") for n in names)
    out = PB.import_project(dest, tmp_path / "imp")
    assert "_last_results" not in yaml.safe_load(out.read_text())


def test_export_with_results_and_import_restores_them(tmp_path):
    import json

    from esfex.visualization.data import project_bundle as PB

    cfg_path, cfg, states = _island_states(tmp_path / "proj")
    res = _h5(tmp_path / "proj" / "results" / "results_1.h5", b"x" * 4096)
    dest = tmp_path / "p.esfexp"
    rep = PB.export_project(states, cfg, dest, src_base=str(cfg_path.parent),
                            results_file=str(res))
    assert rep.results == "results/results_1.h5"
    with zipfile.ZipFile(dest) as zf:
        info = zf.getinfo("results/results_1.h5")
        assert info.compress_type == zipfile.ZIP_STORED
        manifest = json.loads(zf.read(PB.MANIFEST_NAME))
    assert manifest["results"] == "results/results_1.h5"

    imp = tmp_path / "imp"
    out = PB.import_project(dest, imp)
    raw = yaml.safe_load(out.read_text())
    assert raw["_last_results"] == "results/results_1.h5"
    restored = PR.remembered_results(raw, out.parent)
    assert restored == imp / "results" / "results_1.h5"
    assert restored.read_bytes() == b"x" * 4096


def test_export_with_vanished_results_reports_missing(tmp_path):
    from esfex.visualization.data import project_bundle as PB

    cfg_path, cfg, states = _island_states(tmp_path / "proj")
    dest = tmp_path / "p.esfexp"
    gone = tmp_path / "proj" / "results" / "gone.h5"
    rep = PB.export_project(states, cfg, dest, src_base=str(cfg_path.parent),
                            results_file=str(gone))
    assert rep.results is None
    assert str(gone) in rep.missing


# ── Studio (Qt offscreen) ─────────────────────────────────────────────────

@pytest.fixture
def qt(home):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWebEngineWidgets")


@pytest.fixture
def no_dialogs(qt, monkeypatch):
    """Answer every static message box with Yes/Ok and record the texts."""
    from PySide6.QtWidgets import QMessageBox

    asked: list[str] = []

    def _record(*a, **_k):
        asked.append(str(a[2]) if len(a) > 2 else "")
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", staticmethod(_record))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(_record))
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(_record))
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    return asked


def _project(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    data = folder / "data"
    data.mkdir(exist_ok=True)
    for name in ("d0.csv", "d1.csv", "solar.csv"):
        (data / name).write_text("\n".join(["1.0"] * 24) + "\n")
    d = yaml.safe_load(FIXTURE.read_text())
    sys_d = d["systems"]["island"]
    sys_d["demand_paths"] = ["data/d0.csv", "data/d1.csv"]
    sys_d["generators"]["solar"]["Availability"] = "data/solar.csv"
    cfg = folder / "cfg.yaml"
    cfg.write_text(yaml.safe_dump(d, sort_keys=False))
    return cfg


def _start(config=None):
    from esfex.visualization.app import _get_or_create_app, run_studio

    app = _get_or_create_app()
    w = run_studio(config=str(config) if config else None)
    t0 = time.time()
    while w._project_load_pending and time.time() - t0 < 60:
        app.processEvents()
        time.sleep(0.02)
    assert not w._project_load_pending, "project never finished loading"
    return app, w


def _close(w):
    from PySide6.QtCore import QCoreApplication, QEvent

    offer = getattr(w, "_results_offer", None)
    if offer is not None:
        try:
            offer.close()
        except RuntimeError:
            pass
    w._modified = False
    w._project_load_pending = False
    w.close()
    w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QCoreApplication.processEvents()


def _capture_run(w, monkeypatch) -> list:
    cmds: list = []
    monkeypatch.setattr(w.run_output, "run", lambda cmd, env=None: cmds.append(cmd))
    return cmds


def _offer_visible(w) -> bool:
    offer = getattr(w, "_results_offer", None)
    try:
        return offer is not None and offer.isVisible()
    except RuntimeError:  # deleted on close
        return False


def test_run_writes_under_project_folder_not_cwd(qt, no_dialogs, tmp_path, monkeypatch):
    cfg = _project(tmp_path / "proj")
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    app, w = _start(cfg)
    try:
        cmds = _capture_run(w, monkeypatch)
        w._on_run_requested()
        out = cmds[0][cmds[0].index("--output") + 1]
        assert Path(out) == tmp_path / "proj" / "results"
        assert str(tmp_path / "proj" / "results") in w.statusBar().currentMessage()
    finally:
        _close(w)


def test_unsaved_project_run_goes_to_cache_and_says_where(qt, no_dialogs, home, monkeypatch):
    from esfex.visualization.app import _get_or_create_app
    from esfex.visualization.main_window import MainWindow

    _get_or_create_app()
    w = MainWindow()
    try:
        cmds = _capture_run(w, monkeypatch)
        w._on_run_requested()
        out = Path(cmds[0][cmds[0].index("--output") + 1])
        assert out == home / ".cache" / "esfex" / "studio_results"
        msg = w.statusBar().currentMessage()
        assert str(out) in msg and "not saved" in msg
    finally:
        _close(w)


def test_results_remembered_through_save_and_reopen(qt, no_dialogs, tmp_path, monkeypatch):
    cfg = _project(tmp_path / "proj")
    app, w = _start(cfg)
    try:
        assert not w.toolbar._act_results.isEnabled()
        cmds = _capture_run(w, monkeypatch)
        w._on_run_requested()
        assert cmds
        # The "run" writes its results file into the project's results folder.
        res = _h5(tmp_path / "proj" / "results" / "results_20261119_120000.h5")
        w._modified = False
        w._on_subprocess_finished(0)
        assert w._project_results_file == str(res)
        assert w._modified, "a new results file is a change to save"
        w._on_save()
        raw = yaml.safe_load(cfg.read_text())
        assert raw["_last_results"] == "results/results_20261119_120000.h5"
    finally:
        _close(w)

    # Reopen in a fresh window, from another working directory.
    other = tmp_path / "cwd"
    other.mkdir()
    monkeypatch.chdir(other)
    shown: list = []
    app, w = _start(cfg)
    try:
        monkeypatch.setattr(w, "_show_results", lambda: shown.append(w._last_results_file))
        assert w._project_results_file == str(res)
        assert w.toolbar._act_results.isEnabled()
        assert w._last_output_dir == str(res.parent)
        # Offered, not loaded.
        assert _offer_visible(w)
        assert not w._results_offer.isModal()
        assert getattr(w, "_results_dialog", None) is None
        assert not shown
        w._open_project_results()
        assert shown == [str(res)]

        # The same through File > Open on an already open window.
        w._results_offer.close()
        w._project_results_file = ""
        w._load_config_file(str(cfg))
        assert w._project_results_file == str(res)
        assert _offer_visible(w)
    finally:
        _close(w)


def test_missing_results_file_is_forgotten_quietly(qt, no_dialogs, tmp_path):
    cfg = _project(tmp_path / "proj")
    d = yaml.safe_load(cfg.read_text())
    d["_last_results"] = "results/gone.h5"
    cfg.write_text(yaml.safe_dump(d, sort_keys=False))
    app, w = _start(cfg)
    try:
        assert w._project_results_file == ""
        assert not w.toolbar._act_results.isEnabled()
        assert not _offer_visible(w)
        assert not any("gone.h5" in q for q in no_dialogs)
        assert w._last_output_dir == str(tmp_path / "proj" / "results")
        w._mark_modified()
        w._on_save()
        assert "_last_results" not in yaml.safe_load(cfg.read_text())
    finally:
        _close(w)


def test_load_results_is_remembered(qt, no_dialogs, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    cfg = _project(tmp_path / "proj")
    ext = _h5(tmp_path / "shared" / "r.h5")
    monkeypatch.setattr(QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(ext), "")))
    app, w = _start(cfg)
    try:
        monkeypatch.setattr(w, "_show_results", lambda: None)
        w._on_load_results()
        assert w._project_results_file == str(ext)
        w._on_save()
        # Outside the project folder → absolute.
        assert yaml.safe_load(cfg.read_text())["_last_results"] == str(ext)
        # A new scenario forgets the previous project's results.
        w._on_new_scenario()
        assert w._project_results_file == ""
        assert not w.toolbar._act_results.isEnabled()
    finally:
        _close(w)


def test_export_include_results_defaults_off_and_shows_size(qt, no_dialogs, tmp_path,
                                                            monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    cfg = _project(tmp_path / "proj")
    app, w = _start(cfg)
    try:
        # No results → nothing to ask.
        assert w._ask_include_results() is False
        res = _h5(tmp_path / "proj" / "results" / "r.h5", b"y" * 3000)
        w._project_results_file = str(res)
        seen: dict = {}

        def _exec(box):
            seen["checked"] = box.checkBox().isChecked()
            seen["label"] = box.checkBox().text()
            return QMessageBox.StandardButton.Ok

        monkeypatch.setattr(QMessageBox, "exec", _exec)
        assert w._ask_include_results() is False
        assert seen["checked"] is False
        assert "r.h5" in seen["label"] and "2.9 KB" in seen["label"]
        monkeypatch.setattr(QMessageBox, "exec",
                            lambda box: QMessageBox.StandardButton.Cancel)
        assert w._ask_include_results() is None
    finally:
        _close(w)


@pytest.mark.parametrize("include", [False, True])
def test_export_and_import_project_with_and_without_results(qt, no_dialogs, tmp_path,
                                                            monkeypatch, include):
    from PySide6.QtWidgets import QFileDialog

    cfg = _project(tmp_path / "proj")
    res = _h5(tmp_path / "proj" / "results" / "r.h5")
    d = yaml.safe_load(cfg.read_text())
    d["_last_results"] = "results/r.h5"
    cfg.write_text(yaml.safe_dump(d, sort_keys=False))
    bundle = tmp_path / "p.esfexp"
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(bundle), "")))
    app, w = _start(cfg)
    try:
        assert w._project_results_file == str(res)
        monkeypatch.setattr(w, "_ask_include_results", lambda: include)
        w._on_export_project()
        names = zipfile.ZipFile(bundle).namelist()
        assert ("results/r.h5" in names) is include

        dest = tmp_path / "dest"
        dest.mkdir()
        monkeypatch.setattr(QFileDialog, "getOpenFileName",
                            staticmethod(lambda *a, **k: (str(bundle), "")))
        monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                            staticmethod(lambda *a, **k: str(dest)))
        w._on_import_project()
        imported = dest / "p" / "results" / "r.h5"
        if include:
            assert w._project_results_file == str(imported)
            assert imported.is_file()
        else:
            assert w._project_results_file == ""
            assert not imported.exists()
    finally:
        _close(w)
