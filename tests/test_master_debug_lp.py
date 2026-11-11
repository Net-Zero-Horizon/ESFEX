"""The perfect-foresight master LP is written only when asked for.

Every perfect-foresight build wrote ``<output>/logs/master_problem_debug.lp``
before solving (Cuba: 1.9 GB and ~13 minutes). It is now written only with
``ESFEX_MASTER_DEBUG_LP=1``.
"""

import sys

import pytest

sys.path.insert(0, "tests")
from test_myopic_master_history import _system  # noqa: E402
from test_runner import _create_orchestrator, _make_esfex_config  # noqa: E402

from esfex.config.schema import MasterProblemConfig, TemporalConfig  # noqa: E402
from esfex.runner import Orchestrator  # noqa: E402


def test_master_lp_path_only_with_the_env_var(tmp_path, monkeypatch):
    orch = _create_orchestrator(_make_esfex_config(), tmp_path=tmp_path)
    monkeypatch.delenv("ESFEX_MASTER_DEBUG_LP", raising=False)
    assert orch._master_debug_lp_path() is None
    monkeypatch.setenv("ESFEX_MASTER_DEBUG_LP", "1")
    assert orch._master_debug_lp_path() == tmp_path / "logs" / "master_problem_debug.lp"
    assert (tmp_path / "logs").is_dir()


@pytest.mark.julia
@pytest.mark.parametrize("flag", [None, "1"])
def test_foresight_master_lp_written_only_on_request(tmp_path, monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv("ESFEX_MASTER_DEBUG_LP", raising=False)
    else:
        monkeypatch.setenv("ESFEX_MASTER_DEBUG_LP", flag)
    cfg = _make_esfex_config(
        _system("Island", tmp_path), simulation_mode="development",
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720,
                                overlap_hours=0),
        master_problem=MasterProblemConfig(planning_mode="perfect_foresight"))
    out = tmp_path / "out"
    orch = Orchestrator(cfg, output_dir=out)
    orch.run(years=1, master_only=True)
    assert orch.master_solution is not None
    assert (out / "logs" / "master_problem_debug.lp").exists() == (flag == "1")
