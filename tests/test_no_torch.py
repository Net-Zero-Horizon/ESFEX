"""ESFEX runs without PyTorch (round 44).

The TFT demand engine, the gridded ECVI and their torch / pytorch-lightning /
pytorch-forecasting dependencies were removed: XGBoost is the only demand
engine. No module imports torch any more, the package and the demand CLI work
with torch blocked, and the dependencies no longer list it.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import esfex

_PKG_DIR = Path(esfex.__file__).resolve().parent
_REPO_ROOT = _PKG_DIR.parents[1]
_TORCH_MODULES = {"torch", "lightning", "pytorch_lightning",
                  "pytorch_forecasting", "torchmetrics"}


def _imported_top_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def test_no_module_imports_torch():
    offenders = {
        str(p.relative_to(_PKG_DIR)): sorted(_imported_top_modules(p) & _TORCH_MODULES)
        for p in _PKG_DIR.rglob("*.py")
    }
    assert {k: v for k, v in offenders.items() if v} == {}


def test_removed_modules_are_gone():
    for name in ("demand_tft", "tft_inference", "ecvi_gridded",
                 "download_cmip6_subdaily"):
        assert not (_PKG_DIR / "models" / f"{name}.py").exists()


def test_package_and_train_demand_model_help_work_with_torch_blocked():
    code = textwrap.dedent(f"""
        import importlib.abc, sys

        BLOCKED = {sorted(_TORCH_MODULES)!r}

        class _Block(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in BLOCKED:
                    raise ImportError(f"blocked: {{name}}")
                return None

        sys.meta_path.insert(0, _Block())

        import esfex
        import esfex.models.demand_ml
        import esfex.models.demand_training
        import esfex.models.demand_projection
        from typer.testing import CliRunner
        from esfex.cli import app

        result = CliRunner().invoke(app, ["train-demand-model", "--help"])
        assert result.exit_code == 0, result.output
        assert "--folds" in result.output, result.output
        assert "--engine" not in result.output, result.output
        assert not [m for m in sys.modules if m.split(".")[0] in BLOCKED]
        print("OK")
    """)
    env = {**os.environ, "PYTHONPATH": str(_PKG_DIR.parent),
           "QT_QPA_PLATFORM": "offscreen", "COLUMNS": "200", "NO_COLOR": "1"}
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, env=env, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip().endswith("OK")


def test_dependencies_do_not_list_torch():
    tomllib = pytest.importorskip("tomllib")
    pyproject = _REPO_ROOT / "pyproject.toml"
    if not pyproject.exists():
        pytest.skip("not a source checkout")
    deps = tomllib.loads(pyproject.read_text())["project"]["dependencies"]
    names = {d.split(">")[0].split("=")[0].split("<")[0].strip().lower() for d in deps}
    assert names.isdisjoint({"torch", "pytorch-lightning", "pytorch-forecasting",
                             "lightning"})
