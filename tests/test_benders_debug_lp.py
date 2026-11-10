"""Benders writes the LPs that fail only when asked, into the run's logs.

Every verbose Benders run wrote the master and subproblem LPs that failed to
hard-coded ``/tmp/benders_*_failed_*.lp`` files, which piled up there.
"""

import sys
from unittest.mock import patch

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _create_orchestrator, _make_esfex_config  # noqa: E402
from test_zones import _make_system_config  # noqa: E402

from esfex.bridge.adapters import MasterProblemAdapter  # noqa: E402


def test_runner_asks_for_debug_lps_only_with_the_env_var(tmp_path, monkeypatch):
    orch = _create_orchestrator(_make_esfex_config(), tmp_path=tmp_path)
    monkeypatch.delenv("ESFEX_BENDERS_DEBUG_LP", raising=False)
    assert orch._benders_debug_lp_dir() is None
    monkeypatch.setenv("ESFEX_BENDERS_DEBUG_LP", "1")
    assert orch._benders_debug_lp_dir() == str(tmp_path / "logs")
    assert (tmp_path / "logs").is_dir()


@pytest.mark.parametrize("debug_dir, expected", [(None, ""), ("/run/logs", "/run/logs")])
def test_adapter_hands_the_folder_to_benders(debug_dir, expected):
    sysc = _make_system_config(num_nodes=1, coords=[(21.0, -80.0)])
    master = MasterProblemAdapter(config=sysc, years=[2025], base_year=2025,
                                  demand=np.ones((24, 1)), solver_method="benders",
                                  debug_lp_dir=debug_dir)
    master._jl_input = object()
    seen = {}

    class Stop(Exception):
        pass

    class FakeESFEX:
        @staticmethod
        def run_benders_decomposition(_input, **kwargs):
            seen.update(kwargs)
            raise Stop

    with patch("esfex.bridge.adapters.get_esfex_module", return_value=FakeESFEX):
        with pytest.raises(Stop):
            master._solve_benders()
    assert seen["debug_lp_dir"] == expected
