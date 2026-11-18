"""n1_security_cost is the objective with N-1 minus the objective without it.

It used to be always 0 (the dispatch never computed it). With
``n1_security.compute_security_cost`` each window is re-solved without the N-1
constraints; with the option off the field stays 0.
"""

import sys

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator  # noqa: E402
from test_zones import _make_system_config  # noqa: E402

from esfex.config.schema import N1SecurityConfig, TemporalConfig  # noqa: E402


def _run(tmp_path, compute: bool, load=None, overlap_hours: int = 0):
    from esfex.runner import Orchestrator

    demand = tmp_path / "demand.csv"
    np.savetxt(demand, np.full((8760, 1), 10.0) if load is None else load, delimiter=",")
    # One 60 MW unit serving 10 MW: losing it leaves 10 MW uncovered in every
    # step, so N-1 only adds its shortfall penalty (the dispatch is the same).
    gas = _make_generator("Gas", "Non-renewable", "Gas", [60.0],
                          degradation_rate=[0.0], fuel_cost=[50.0],
                          start_up_cost=[0.0], initial_age=[0])
    sysc = _make_system_config(num_nodes=1, coords=[(23.0, -82.0)],
                               generators={"gas": gas})
    sysc = sysc.model_copy(update={"demand_path": str(demand)})
    cfg = _make_esfex_config(
        sysc, simulation_mode="economic_dispatch",
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720,
                                overlap_hours=overlap_hours),
        n1_security=N1SecurityConfig(
            enabled=True, apply_to_modes=["economic_dispatch"],
            transmission_enabled=False, generation_enabled=True,
            compute_security_cost=compute))
    out = tmp_path / ("on" if compute else "off") / str(overlap_hours)
    return Orchestrator(cfg, output_dir=out).run(years=1)[0]


@pytest.mark.julia
def test_n1_security_cost_is_objective_difference(tmp_path):
    on = _run(tmp_path, compute=True)
    shortfall = on.cost_breakdown["n1_security_shortfall_cost"]
    assert shortfall > 0
    # Without N-1 the dispatch is unchanged: the difference is the penalty.
    assert on.n1_security_cost == pytest.approx(shortfall, rel=1e-6)

    off = _run(tmp_path, compute=False)
    assert off.n1_security_cost == 0.0
    assert off.cost_breakdown["n1_security_shortfall_cost"] == pytest.approx(shortfall, rel=1e-6)


@pytest.mark.julia
def test_n1_security_cost_covers_the_committed_steps(tmp_path):
    """With an overlap, each window is valued over its kept steps.

    The whole-window difference used to be scaled by kept / window steps.
    With a load rising through the year, every overlap tail is dearer than
    the kept steps, so that overstated the cost; it now equals the N-1
    shortfall penalty of the kept steps, as the cost breakdown counts it.
    """
    load = np.linspace(5.0, 50.0, 8760).reshape(-1, 1)
    on = _run(tmp_path, compute=True, load=load, overlap_hours=240)
    shortfall = on.cost_breakdown["n1_security_shortfall_cost"]
    assert shortfall > 0
    assert on.n1_security_cost == pytest.approx(shortfall, rel=1e-6)
