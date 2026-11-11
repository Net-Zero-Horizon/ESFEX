"""Julia log records reach the Python logger (``esfex.julia``).

Under juliacall Julia's default logger wrote @info/@warn to the bare
process stderr: Benders iterations and Julia warnings never reached the
ESFEX console handler or the run's log file.
"""

import logging

import pytest

from esfex.bridge.julia_setup import get_julia
from esfex.logging_config import _BasicFilter

pytestmark = pytest.mark.julia


def _julia_records(caplog):
    return [r for r in caplog.records if r.name == "esfex.julia"]


def test_julia_info_warn_and_esfex_debug_reach_the_python_log(caplog):
    jl = get_julia()
    with caplog.at_level(logging.DEBUG, logger="esfex.julia"):
        jl.seval('@info "z22 info line" answer = 42')
        jl.seval('@warn "z22 warning"')
        jl.seval('@error "z22 error"')
        jl.seval('ESFEX.eval(:(@debug "z22 esfex debug"))')
        jl.seval('@debug "z22 debug outside ESFEX"')
    got = [(r.levelno, r.getMessage()) for r in _julia_records(caplog)]
    assert (logging.INFO, "z22 info line\n  answer = 42") in got
    assert (logging.WARNING, "z22 warning") in got
    assert (logging.ERROR, "z22 error") in got
    assert (logging.DEBUG, "z22 esfex debug") in got
    # Debug records of other modules (Base, packages) stay out
    assert not any("outside ESFEX" in m for _, m in got)


def test_benders_progress_is_a_milestone_of_the_basic_console(caplog):
    jl = get_julia()
    with caplog.at_level(logging.DEBUG, logger="esfex.julia"):
        jl.seval('ESFEX.eval(:(@info "Benders iteration 1: LB = 1.0" _group = :benders))')
        jl.seval('@info "z22 other info"')
    recs = {r.getMessage(): r for r in _julia_records(caplog)}
    basic = _BasicFilter()
    assert basic.filter(recs["Benders iteration 1: LB = 1.0"])
    assert not basic.filter(recs["z22 other info"])


def test_maxlog_is_honoured(caplog):
    jl = get_julia()
    with caplog.at_level(logging.INFO, logger="esfex.julia"):
        jl.seval('for i in 1:3; @info "z22 once" maxlog = 1; end')
    assert [r.getMessage() for r in _julia_records(caplog)].count("z22 once") == 1
