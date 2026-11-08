"""The dispatch knows the run's simulation mode, not only its formulation.

A development run solves its windows as economic dispatch (or unit
commitment with ``use_uc_in_dispatch``), and that formulation was all the
dispatch received. ``n1_security.apply_to_modes`` (default
``[development, unit_commitment]``) lists simulation modes, so N-1 was never
applied in development runs; the primary-energy rule that a development
window ends with its tanks at least 10 % full never ran either.
"""

import sys

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator  # noqa: E402
from test_zones import _make_system_config  # noqa: E402

from esfex.config.schema import N1SecurityConfig, TemporalConfig  # noqa: E402


def _n1_config(tmp_path, mode, apply_to_modes=None, compute=False):
    demand = tmp_path / "demand.csv"
    np.savetxt(demand, np.full((8760, 1), 10.0), delimiter=",")
    # One 60 MW unit serving 10 MW: losing it leaves 10 MW uncovered in
    # every step, so N-1 shows up as its shortfall penalty.
    gas = _make_generator("Gas", "Non-renewable", "Gas", [60.0],
                          degradation_rate=[0.0], fuel_cost=[50.0],
                          start_up_cost=[0.0], initial_age=[0],
                          invest_max_power=[0.0])
    sysc = _make_system_config(num_nodes=1, coords=[(23.0, -82.0)],
                               generators={"gas": gas})
    sysc = sysc.model_copy(update={"demand_path": str(demand)})
    n1 = dict(enabled=True, transmission_enabled=False, generation_enabled=True,
              compute_security_cost=compute)
    if apply_to_modes is not None:
        n1["apply_to_modes"] = apply_to_modes
    return _make_esfex_config(
        sysc, simulation_mode=mode,
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720,
                                overlap_hours=0),
        n1_security=N1SecurityConfig(**n1))


def test_adapter_run_mode_defaults_to_the_simulation_mode(tmp_path):
    """The adapter checks apply_to_modes against the run's mode."""
    from esfex.bridge.adapters import PowerSystemAdapter

    cfg = _n1_config(tmp_path, "development", compute=True)
    ps = PowerSystemAdapter(config=cfg, demand=np.zeros((2, 1)), hours=2,
                            num_nodes=1, year=2025, base_year=2025,
                            mode="economic_dispatch")
    assert ps.mode == "economic_dispatch"
    assert ps.run_mode == "development"
    assert ps._n1_security_cost_requested()

    # An explicit run mode wins; N-1 not listed for it is off
    ps = PowerSystemAdapter(config=cfg, demand=np.zeros((2, 1)), hours=2,
                            num_nodes=1, year=2025, base_year=2025,
                            mode="economic_dispatch", run_mode="economic_dispatch")
    assert not ps._n1_security_cost_requested()


@pytest.mark.julia
def test_n1_applies_in_development_runs(tmp_path):
    from esfex.runner import Orchestrator

    dev = Orchestrator(_n1_config(tmp_path, "development"),
                       output_dir=tmp_path / "dev").run(years=1)[0]
    assert dev.cost_breakdown["n1_security_shortfall_cost"] > 0

    # "development" listed alone: an economic-dispatch run gets no N-1
    ed = Orchestrator(_n1_config(tmp_path, "economic_dispatch",
                                 apply_to_modes=["development"]),
                      output_dir=tmp_path / "ed").run(years=1)[0]
    assert ed.cost_breakdown["n1_security_shortfall_cost"] == pytest.approx(0.0, abs=1e-9)


@pytest.mark.julia
def test_development_windows_give_the_fuel_model_the_run_mode(tmp_path, monkeypatch):
    """The fuel model gets "development" in a development run (it got the
    dispatch formulation, so the 10 % end-of-window tank fill never ran)."""
    import esfex.runner as runner_mod
    from test_master_fuel import _hydrogen_plan

    modes = []
    real = runner_mod.PrimaryEnergyAdapter

    class _Recording(real):
        def __init__(self, *args, **kwargs):
            modes.append(kwargs.get("mode"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(runner_mod, "PrimaryEnergyAdapter", _Recording)
    _hydrogen_plan(tmp_path, "dev")
    assert modes and set(modes) == {"development"}
