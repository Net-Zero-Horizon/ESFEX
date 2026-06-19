"""Windows Qt DLL resolution for the Studio launcher.

These guard the fix for the ERROR_PROC_NOT_FOUND ("DLL load failed ... the
specified procedure could not be found") crash, where a conda Qt on PATH
shadowed PySide6's own Qt. Logic is exercised on any OS via monkeypatching.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types

import pytest

from esfex.visualization import (
    _ensure_qt_runtime_on_path,
    _raise_qt_import_error,
)


def _fake_spec(pyside_dir):
    return types.SimpleNamespace(submodule_search_locations=[pyside_dir])


# ── _ensure_qt_runtime_on_path ──────────────────────────────────────────────


def test_noop_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("PATH", "/orig")
    _ensure_qt_runtime_on_path()
    assert os.environ["PATH"] == "/orig"


def test_self_contained_pyside_wins_and_skips_conda(monkeypatch, tmp_path):
    # PySide6 ships its own Qt6Core.dll → it must be first, and the conda
    # Library/bin must NOT be added (so it can never shadow the bundled Qt).
    pyside = tmp_path / "PySide6"
    pyside.mkdir()
    (pyside / "Qt6Core.dll").write_bytes(b"")  # marks self-contained
    prefix = tmp_path / "prefix"
    (prefix / "Library" / "bin").mkdir(parents=True)

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name: _fake_spec(str(pyside)))
    monkeypatch.setenv("PATH", "ORIG")

    _ensure_qt_runtime_on_path()

    parts = os.environ["PATH"].split(os.pathsep)
    assert parts[0] == str(pyside)               # bundled Qt first
    assert str(prefix / "Library" / "bin") not in parts  # conda skipped
    assert parts[-1] == "ORIG"


def test_conda_provided_qt_is_lower_priority(monkeypatch, tmp_path):
    # PySide6 without a bundled Qt → conda dirs are added, but AFTER the
    # PySide6 dir so they cannot shadow it.
    pyside = tmp_path / "PySide6"
    pyside.mkdir()  # no Qt6Core.dll → not self-contained
    prefix = tmp_path / "prefix"
    libbin = prefix / "Library" / "bin"
    qt6 = prefix / "Library" / "lib" / "qt6"
    libbin.mkdir(parents=True)
    qt6.mkdir(parents=True)

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name: _fake_spec(str(pyside)))
    monkeypatch.setenv("PATH", "ORIG")

    _ensure_qt_runtime_on_path()

    parts = os.environ["PATH"].split(os.pathsep)
    assert parts[0] == str(pyside)
    assert parts.index(str(libbin)) > 0
    assert parts.index(str(qt6)) > parts.index(str(libbin))


# ── _raise_qt_import_error ──────────────────────────────────────────────────


def test_dll_load_failure_gives_conflict_guidance(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    exc = ImportError(
        "DLL load failed while importing QtWidgets: "
        "The specified procedure could not be found.")
    with pytest.raises(ImportError) as ei:
        _raise_qt_import_error(exc)
    msg = str(ei.value)
    assert "DLL conflict" in msg
    assert "where esfex" in msg
    assert "force-reinstall" not in msg  # not the wrong, misleading advice


def test_missing_package_gives_reinstall_hint(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    exc = ModuleNotFoundError("No module named 'PySide6'")
    with pytest.raises(ImportError) as ei:
        _raise_qt_import_error(exc)
    assert "force-reinstall esfex" in str(ei.value)
