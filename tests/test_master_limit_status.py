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
        return "[OBJECTIVE FUNCTION BREAKDOWN - test]"


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


def test_solution_summary_reaches_the_log(caplog):
    # The Julia function returns the summary as a string; it was dropped
    with caplog.at_level(logging.DEBUG, logger="esfex.bridge.adapters"):
        assert _solve_master("OPTIMAL", "FEASIBLE_POINT")[0] == 1
    assert any("[OBJECTIVE FUNCTION BREAKDOWN - test]" in r.getMessage()
               for r in caplog.records if r.levelno == logging.DEBUG)


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


# --- Dispatch windows (round 39) -------------------------------------------
# The dispatch ran a plain optimize! and accepted Gurobi's SUBOPTIMAL
# (LOCALLY_SOLVED) silently. It now solves as the master
# (ESFEX.optimize_dispatch!: one more solve with BarHomogeneous = 1, a
# stalled loss-repair round undone); a window still LOCALLY_SOLVED keeps its
# feasible point with a warning that names it.

def _window(window=4):
    from esfex.bridge.adapters import PowerSystemAdapter

    ps = PowerSystemAdapter.__new__(PowerSystemAdapter)
    ps.year, ps.start_hour, ps.hours = 2030, 96, 48
    ps.kwargs = {} if window is None else {"window": window}
    ps.esfex_config = None
    return ps


class FakeDispatchJulia(FakeJulia):
    def __init__(self, status, primal, solver="Gurobi"):
        super().__init__(status, primal)
        self.answers["solver_name(_ps_model)"] = solver
        self.calls = []

    def seval(self, code):
        if "optimize_dispatch!" in code:
            return lambda model, label: self.calls.append((code, label))
        self.calls.append((code, None))
        return super().seval(code)


def test_dispatch_locally_solved_kept_with_window_warning(caplog):
    ps = _window()
    with caplog.at_level(logging.WARNING, logger="esfex.bridge.adapters"):
        assert ps._map_termination_status(
            FakeDispatchJulia("LOCALLY_SOLVED", "FEASIBLE_POINT")) == 1
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "Dispatch window 4 (2030, from hour 96)" in msg
    assert "short of optimal" in msg and "LOCALLY_SOLVED" in msg


def test_dispatch_locally_solved_rules(caplog):
    # Ipopt's normal answer: kept without a warning
    with caplog.at_level(logging.WARNING, logger="esfex.bridge.adapters"):
        assert _window()._map_termination_status(
            FakeDispatchJulia("LOCALLY_SOLVED", "FEASIBLE_POINT", solver="Ipopt")) == 1
    assert not caplog.records
    # Without a feasible point it is no dispatch (INFEASIBLE_POINT contains
    # the text FEASIBLE_POINT)
    assert _window()._map_termination_status(
        FakeDispatchJulia("LOCALLY_SOLVED", "INFEASIBLE_POINT")) == 0
    assert _window()._map_termination_status(
        FakeDispatchJulia("OPTIMAL", "FEASIBLE_POINT")) == 1
    assert _window(None)._window_label() == "Dispatch (2030, from hour 96)"


def test_dispatch_solves_through_optimize_dispatch():
    ps = _window(7)
    ps._jl_model = object()
    ps._jl_vars = None
    jl = FakeDispatchJulia("OPTIMAL", "FEASIBLE_POINT")
    with patch("esfex.bridge.adapters.get_julia", return_value=jl):
        assert ps.solve() == 1
    solves = [(c, label) for c, label in jl.calls if "optimize" in c]
    assert len(solves) == 1
    assert "ESFEX.optimize_dispatch!" in solves[0][0]
    assert solves[0][1] == "Dispatch window 7 (2030, from hour 96)"
