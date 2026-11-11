"""A master solve stopped at a limit with a feasible plan is accepted.

The master treated every non-OPTIMAL status as a failure, so a 6-hour Cuba
master that stopped at TIME_LIMIT aborted the run even when the solver held a
feasible plan; the dispatch already accepted it. A limit without a feasible
point stays a failure, with advice. ``INFEASIBLE_POINT`` contains the text
``FEASIBLE_POINT``: the dispatch accepted it too.
"""

import logging
import sys
from unittest.mock import patch

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_zones import _make_system_config  # noqa: E402

from esfex.bridge.adapters import (  # noqa: E402
    MasterProblemAdapter,
    _accept_limit_incumbent,
)


class FakeJulia:
    """Answers the ``seval`` calls of a solve with a given status."""

    def __init__(self, status, primal, objective=100.0, bound=90.0):
        self.answers = {
            "termination_status(_mp_model)": status,
            "primal_status(_mp_model)": primal,
            "termination_status(_ps_model)": status,
            "primal_status(_ps_model)": primal,
            "objective_value(_mp_model)": objective,
            "objective_bound(_mp_model)": bound,
            "objective_value(_ps_model)": objective,
            "objective_bound(_ps_model)": bound,
        }

    def seval(self, code):
        return self.answers.get(code)


class FakeESFEX:
    def extract_master_solution(self, *args):
        return None

    def log_solution_summary(self, *args):
        pass


def _solve_master(status, primal, **kw):
    sysc = _make_system_config(num_nodes=1, coords=[(21.0, -80.0)])
    master = MasterProblemAdapter(config=sysc, years=[2025], base_year=2025,
                                  demand=np.ones((24, 1)))
    master._jl_model = object()
    jl = FakeJulia(status, primal, **kw)
    with patch("esfex.bridge.adapters.get_julia", return_value=jl), \
            patch("esfex.bridge.adapters.get_esfex_module", return_value=FakeESFEX()):
        return master._solve_once(), master


@pytest.mark.parametrize("status", ["TIME_LIMIT", "ITERATION_LIMIT", "NODE_LIMIT"])
def test_limit_with_feasible_plan_is_accepted(status, caplog):
    with caplog.at_level(logging.WARNING, logger="esfex.bridge.adapters"):
        code, master = _solve_master(status, "FEASIBLE_POINT")
    assert code == 1
    assert master._last_status == status
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "master problem solver stopped at " + status in msg
    assert "gap=10.00%" in msg


def test_limit_without_plan_fails_with_advice(caplog):
    with caplog.at_level(logging.ERROR, logger="esfex.bridge.adapters"):
        code, _ = _solve_master("TIME_LIMIT", "NO_SOLUTION")
    assert code == 0
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "without a feasible plan" in msg
    assert "solver.time_limit" in msg and "crossover: off" in msg


def test_limit_with_infeasible_point_fails():
    assert _solve_master("ITERATION_LIMIT", "INFEASIBLE_POINT")[0] == 0


def test_optimal_and_infeasible_unchanged():
    assert _solve_master("OPTIMAL", "FEASIBLE_POINT")[0] == 1
    assert _solve_master("INFEASIBLE", "NO_SOLUTION")[0] == -1


def test_accept_limit_incumbent_rules(caplog):
    jl = FakeJulia("TIME_LIMIT", "FEASIBLE_POINT", bound=float("-inf"))
    with caplog.at_level(logging.WARNING, logger="esfex.bridge.adapters"):
        assert _accept_limit_incumbent(jl, "_mp_model", "TIME_LIMIT", "FEASIBLE_POINT",
                                       "X", "advice")
    assert "gap unknown" in caplog.records[-1].getMessage()
    assert not _accept_limit_incumbent(jl, "_mp_model", "TIME_LIMIT",
                                       "INFEASIBLE_POINT", "X", "advice")
    assert not _accept_limit_incumbent(jl, "_mp_model", "NUMERICAL_ERROR",
                                       "FEASIBLE_POINT", "X", "advice")
