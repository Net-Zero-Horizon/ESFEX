"""Benders through the runner plans like the monolithic master.

The runner's debug LP export raised for Benders (no single model), a one-node
one-step day broke the subproblem's curtailment expression, and the
subproblems capped renewable output at the master's RE ratio, so with a 0 %
target no renewable could be used and none was built.

The Benders master has neither TSAM periods nor user investment constraints;
with either, the runner solves the monolithic master instead (Benders used to
drop the user constraints silently).
"""

import logging
import sys

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator  # noqa: E402
from test_zones import _make_system_config, _make_technology  # noqa: E402

from esfex.config.schema import (  # noqa: E402
    ConstraintTerm, CustomConstraintConfig, MasterProblemConfig, PenaltiesConfig, TemporalConfig,
)

FALLBACK = "Benders decomposition does not support"


def _solar_built(tmp_path, method, *, cap=None, use_tsam=False):
    from esfex.runner import Orchestrator

    demand = tmp_path / f"demand_{method}.csv"
    np.savetxt(demand, np.full(8760, 60.0))
    diesel = _make_generator("Diesel", "Non-renewable", "Diesel", [100.0], degradation_rate=[0.0],
                             fuel_cost=[200.0], min_power=[0.0])
    solar = _make_technology("solar_new", "Sun", 1, tech_type="Renewable",
                             invest_cost=20000.0, invest_max_power=50.0)
    sysc = _make_system_config(num_nodes=1, coords=[(21.0, -80.0)],
                               generators={"diesel": diesel}, technologies={"solar_new": solar})
    update = {"demand_path": str(demand), "penalties": PenaltiesConfig(loss_of_load=10000.0)}
    if cap is not None:
        # At most `cap` MW of solar may be built
        update["custom_constraints"] = [CustomConstraintConfig(
            name="solar_cap", target="investment", sense="<=", rhs=cap,
            terms=[ConstraintTerm(variable="tech_investment",
                                  index=["all", "solar_new", "all"])])]
    sysc = sysc.model_copy(update=update)
    cfg = _make_esfex_config(
        sysc, simulation_mode="development",
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720, overlap_hours=0),
        master_problem=MasterProblemConfig(solver_method=method, use_tsam=use_tsam,
                                           tsam_num_periods=2))
    tag = f"{method}_{cap}_{use_tsam}"
    results = Orchestrator(cfg, output_dir=tmp_path / tag).run(years=1)
    return results[0].investments.get("tech_investment_power_0_0", 0.0)


@pytest.mark.julia
def test_benders_builds_what_the_monolithic_master_builds(tmp_path, caplog):
    mono = _solar_built(tmp_path, "monolithic")
    assert mono == pytest.approx(50.0, abs=1e-3)
    with caplog.at_level(logging.WARNING):
        assert _solar_built(tmp_path, "benders") == pytest.approx(mono, abs=0.5)
    assert not any(FALLBACK in r.getMessage() for r in caplog.records)


@pytest.mark.julia
def test_benders_keeps_user_investment_constraints(tmp_path, caplog):
    """With a user investment constraint, Benders hands over to the
    monolithic master, which applies it (Benders ignored it and built the
    full 50 MW)."""
    with caplog.at_level(logging.WARNING):
        built = _solar_built(tmp_path, "benders", cap=20.0)
    assert built == pytest.approx(20.0, abs=1e-3)
    assert any(FALLBACK in r.getMessage() and "custom_constraints" in r.getMessage()
               for r in caplog.records)


@pytest.mark.julia
def test_benders_with_tsam_solves_the_monolithic_master(tmp_path, caplog):
    mono = _solar_built(tmp_path, "monolithic", use_tsam=True)
    with caplog.at_level(logging.WARNING):
        assert _solar_built(tmp_path, "benders", use_tsam=True) == pytest.approx(mono, abs=1e-6)
    assert any(FALLBACK in r.getMessage() and "use_tsam" in r.getMessage()
               for r in caplog.records)
