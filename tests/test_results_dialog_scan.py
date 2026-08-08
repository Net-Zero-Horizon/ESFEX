"""The Map Results system selector must reflect the CURRENT run only.

Regression guard: ``_scan_results`` aggregated ``subsystem_names`` from every
HDF5 file in the results directory, so old runs (often other regions, e.g.
Japan / Isla_Juventud) leaked into the system selector alongside the actual
run. It must use only the latest run (files are sorted newest-first).
"""

import os
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
h5py = pytest.importorskip("h5py")

from esfex.visualization.panels.results_dialog import ResultsDialog


def _write_run(path, subsystems, mtime):
    with h5py.File(path, "w") as f:
        f.attrs["subsystem_names"] = list(subsystems)
    os.utime(path, (mtime, mtime))


def _scan(results_dir=None, results_file=None):
    dummy = types.SimpleNamespace(
        _results_file=results_file,
        _results_dir=results_dir,
        _h5_files={},
        _base_prefix={},
    )
    ResultsDialog._scan_results(dummy)
    return dummy._h5_files


def test_only_latest_run_populates_selector(tmp_path):
    _write_run(tmp_path / "esfex_run_old.h5", ["Japan"], mtime=1000)
    _write_run(tmp_path / "esfex_run_mid.h5", ["Isla_Juventud"], mtime=2000)
    _write_run(tmp_path / "esfex_run_new.h5", ["Hokkaido"], mtime=3000)

    names = _scan(results_dir=tmp_path)

    assert "Hokkaido" in names
    assert "Japan" not in names          # stale run must not leak
    assert "Isla_Juventud" not in names
    assert set(names) <= {"Hokkaido", "Global"}


def test_multi_system_latest_run_kept_together(tmp_path):
    # A genuine multi-system run keeps all its members.
    _write_run(tmp_path / "old.h5", ["Cuba"], mtime=1000)
    _write_run(tmp_path / "new.h5", ["North", "South"], mtime=3000)

    names = _scan(results_dir=tmp_path)

    assert {"North", "South"} <= set(names)
    assert "Cuba" not in names


def test_specific_file_used_directly(tmp_path):
    f = tmp_path / "run.h5"
    _write_run(f, ["Hokkaido"], mtime=1000)
    # A different, newer file in the same dir must be ignored when a specific
    # file is requested.
    _write_run(tmp_path / "other.h5", ["Japan"], mtime=5000)

    names = _scan(results_dir=tmp_path, results_file=f)

    assert "Hokkaido" in names
    assert "Japan" not in names
