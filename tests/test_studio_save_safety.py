"""Studio save safety with ``esfex studio -c file.yaml [-o out.yaml]``.

* Autosave writes only the autosave cache, never the user's file.
* Nothing is saved before the ``-c`` project has finished loading (it is
  populated only once the map is ready), and an empty placeholder project
  never overwrites a file that defines systems.
* ``-c a/cfg.yaml -o b/out.yaml``: relative data paths are rebased so the
  written file still finds its data; Export Project resolves them against
  the folder the project was loaded from.

Every test points HOME / XDG dirs and the preferences module at a tmp folder,
so the user's real preferences, recent files and autosave cache are never
touched.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path

import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("PySide6.QtWebEngineWidgets")

FIXTURE = Path(__file__).parent / "fixtures" / "studio_export_island.yaml"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated HOME, XDG dirs and preferences file."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    for var in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        monkeypatch.setenv(var, str(h / var.lower()))
    from esfex.visualization import preferences as prefs

    monkeypatch.setattr(prefs, "_PREFS_DIR", h / ".config" / "esfex")
    monkeypatch.setattr(prefs, "_PREFS_FILE", h / ".config" / "esfex" / "preferences.json")
    return h


@pytest.fixture
def no_dialogs(monkeypatch):
    """Answer every message box with Yes/Ok and record the questions asked."""
    from PySide6.QtWidgets import QMessageBox

    asked: list[str] = []

    def _record(*a, **_k):
        asked.append(str(a[2]) if len(a) > 2 else "")
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", staticmethod(_record))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(_record))
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    return asked


def _md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def _project(folder: Path) -> Path:
    """A copy of the island fixture with relative demand / availability paths
    and the data files next to it."""
    folder.mkdir(parents=True, exist_ok=True)
    data = folder / "data"
    data.mkdir()
    for name in ("d0.csv", "d1.csv", "solar.csv"):
        (data / name).write_text("\n".join(["1.0"] * 24) + "\n")
    d = yaml.safe_load(FIXTURE.read_text())
    sys_d = d["systems"]["island"]
    sys_d["demand_paths"] = ["data/d0.csv", "data/d1.csv"]
    sys_d["generators"]["solar"]["Availability"] = "data/solar.csv"
    cfg = folder / "cfg.yaml"
    cfg.write_text(yaml.safe_dump(d, sort_keys=False) + "# my comment\n")
    return cfg


def _start(config, output=None):
    from esfex.visualization.app import _get_or_create_app, run_studio

    app = _get_or_create_app()
    w = run_studio(config=str(config), output=str(output) if output else None)
    return app, w


def _wait_loaded(app, w, timeout=60.0):
    t0 = time.time()
    while w._project_load_pending and time.time() - t0 < timeout:
        app.processEvents()
        time.sleep(0.02)
    assert not w._project_load_pending, "project never finished loading"
    assert w._all_states


def _close(w):
    from PySide6.QtCore import QCoreApplication, QEvent

    w._modified = False
    w._project_load_pending = False
    w.close()
    # Delete the window (and its WebEngine page) now, not at interpreter exit,
    # where a page outliving its profile aborts the process.
    w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QCoreApplication.processEvents()


def test_autosave_writes_cache_not_user_file(home, tmp_path, no_dialogs):
    cfg = _project(tmp_path / "proj")
    h0 = _md5(cfg)
    app, w = _start(cfg)
    try:
        _wait_loaded(app, w)
        w._mark_modified()
        w._on_autosave()
        assert _md5(cfg) == h0, "autosave rewrote the user's file"
        cache = home / ".cache" / "esfex" / "autosave.yaml"
        assert cache.is_file()
        assert w._modified, "autosave to the cache must not clear unsaved changes"
        assert w._last_saved_path is None
        # The cache copy still finds the project's data (paths rebased).
        cached = yaml.safe_load(cache.read_text())
        for p in cached["systems"]["island"]["demand_paths"]:
            assert (cache.parent / p).is_file() or Path(p).is_file()
    finally:
        _close(w)


def test_nothing_saved_before_project_loaded(home, tmp_path, no_dialogs):
    cfg = _project(tmp_path / "proj")
    h0 = _md5(cfg)
    app, w = _start(cfg)
    try:
        assert w._project_load_pending
        assert not w._all_states
        assert not w._act_save.isEnabled()
        assert not w._act_export.isEnabled()
        assert not w._act_export_project.isEnabled()
        w._mark_modified()
        w._on_save()
        w._on_autosave()
        w._save_config_file(str(cfg))
        assert _md5(cfg) == h0
        assert not w._all_states, "a save created the placeholder system"
        # Closing does not offer to save the unloaded project.
        from PySide6.QtGui import QCloseEvent
        ev = QCloseEvent()
        w.closeEvent(ev)
        assert ev.isAccepted()
        assert not no_dialogs
        assert _md5(cfg) == h0
        assert w._last_saved_path is None

        _wait_loaded(app, w)
        assert w._act_save.isEnabled()
    finally:
        _close(w)


def test_empty_placeholder_never_overwrites_systems(home, tmp_path, no_dialogs):
    from esfex.visualization.app import _get_or_create_app
    from esfex.visualization.main_window import MainWindow

    _get_or_create_app()
    cfg = _project(tmp_path / "proj")
    h0 = _md5(cfg)
    w = MainWindow()
    try:
        # E.g. the deferred load failed: the window points at the file but
        # holds no project. Ctrl+S must not replace it with System_1.
        w._config_path = str(cfg)
        w._modified = True
        w._on_save()
        assert _md5(cfg) == h0
        assert any("empty" in q for q in no_dialogs)
        assert w._last_saved_path is None
        # A new file is fine.
        new = tmp_path / "new.yaml"
        w._save_config_file(str(new))
        assert new.is_file()
        assert w._last_saved_path == str(new)
    finally:
        _close(w)


def test_output_elsewhere_rebases_data_paths(home, tmp_path, no_dialogs):
    from esfex.config.loader import load_config

    cfg = _project(tmp_path / "a")
    out_dir = tmp_path / "b"
    out_dir.mkdir()
    out = out_dir / "out.yaml"
    h0 = _md5(cfg)
    app, w = _start(cfg, output=out)
    try:
        _wait_loaded(app, w)
        assert w._config_path == str(out)
        w._mark_modified()
        w._on_save()
        assert w._last_saved_path == str(out)
        assert _md5(cfg) == h0
        saved = load_config(out)
        isl = saved.systems["island"]
        for p in isl.demand_paths:
            assert Path(p).is_absolute() and Path(p).is_file(), p
        av = isl.generators["solar"].availability_file
        assert Path(av).is_absolute() and Path(av).is_file()
        # The export-project bundle resolves the data against the load folder.
        assert w._data_base_dir() == str(cfg.parent)
    finally:
        _close(w)


def test_rebase_keeps_paths_under_output_folder_relative(tmp_path):
    from esfex.visualization.data.serializer import _rebase_data_paths

    d = {"systems": {"s": {
        "demand_paths": ["data/d0.csv", "", "/abs/d.csv"],
        "generators": {"g": {"Availability": "data/sol.csv",
                             "reservoir_inflow_file": "in.csv"}},
    }}}
    _rebase_data_paths(d, tmp_path / "proj", tmp_path)
    s = d["systems"]["s"]
    assert s["demand_paths"] == ["proj/data/d0.csv", "", "/abs/d.csv"]
    assert s["generators"]["g"]["Availability"] == "proj/data/sol.csv"
    assert s["generators"]["g"]["reservoir_inflow_file"] == "proj/in.csv"
    _rebase_data_paths(d, tmp_path, tmp_path / "other")
    assert s["demand_paths"][0] == str(tmp_path / "proj" / "data" / "d0.csv")


def test_export_project_uses_load_folder(home, tmp_path, no_dialogs, monkeypatch):
    import zipfile

    from PySide6.QtWidgets import QFileDialog

    cfg = _project(tmp_path / "a")
    out_dir = tmp_path / "b"
    out_dir.mkdir()
    bundle = tmp_path / "proj.esfexp"
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(bundle), "")))
    app, w = _start(cfg, output=out_dir / "out.yaml")
    try:
        _wait_loaded(app, w)
        w._on_export_project()
        names = zipfile.ZipFile(bundle).namelist()
        assert "demand/d0.csv" in names and "availability/solar.csv" in names
    finally:
        _close(w)
