"""Reserves are held per system, and the RE target is a planning target.

Two problems found on the Cuba case (economic_dispatch, one year):

- The static and dynamic reserve requirement was held per node, covered only
  by units at that node: a node with demand and no unit was always short
  (2.6 TWh of reserve shortfall in Cuba while the system had GWs free). The
  requirement is now per system, covered by any reservable unit of the
  system; a merged run keeps one requirement per original system.
- Without a master the adapter fell back to ``target_re_penetration``, so
  the first year's dispatch was held to the end-of-horizon RE target.
"""

import sys

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import (  # noqa: E402
    _make_esfex_config,
    _make_generator,
    _make_node_config,
    _make_system_config,
)

from esfex.config.schema import (  # noqa: E402
    MetaNetworkConfig,
    PenaltiesConfig,
    TemporalConfig,
)
from esfex.runner import Orchestrator  # noqa: E402

LOAD = 50.0     # MW per node
RATIO = 0.2     # static reserve, share of demand


def _gas(rated):
    return _make_generator("Gas", "Non-renewable", "Gas", rated, degradation_rate=[0.0] * len(rated),
                           min_power=[0.0] * len(rated), fuel_cost=[50.0] * len(rated),
                           fixed_cost=[0.0] * len(rated), maintenance_cost=[0.0] * len(rated),
                           reservable=True)


def _system(name, rated, tmp_path, target_re=0.0):
    """One system, ``len(rated)`` nodes with ``LOAD`` each, one gas unit."""
    n = len(rated)
    nodes = _make_node_config(n).model_copy(update={
        "reserve_static": [0.0] * n, "reserve_dynamic": [0.0] * n})
    path = tmp_path / f"{name}.csv"
    np.savetxt(path, np.full((8760, n), LOAD), delimiter=",")
    s = _make_system_config(num_nodes=n, name=name, generators={"gas": _gas(rated)})
    return s.model_copy(update={
        "nodes": nodes, "demand_path": str(path),
        "reserve_static_default_ratio": RATIO, "target_re_penetration": target_re,
        "penalties": PenaltiesConfig(loss_of_load=10000.0, loss_of_reserve_static=100.0,
                                     co2_cost=0.0),
    })


def _run(cfg, tmp_path):
    cfg = cfg.model_copy(update={
        "simulation_mode": "economic_dispatch",
        "temporal": TemporalConfig(resolution_hours=24, rolling_horizon_hours=720,
                                   overlap_hours=0)})
    return Orchestrator(cfg, output_dir=tmp_path / "out").run(years=1)[0]


@pytest.mark.julia
def test_node_without_units_uses_the_system_headroom(tmp_path):
    # Node 0 has load and no unit; node 1's 300 MW cover both loads (100 MW)
    # and both nodes' 10 MW of reserve.
    s = _system("S", [0.0, 300.0], tmp_path)
    res = _run(_make_esfex_config(s), tmp_path)
    assert float(np.nansum(res.load_shed_array)) == pytest.approx(0.0, abs=1e-6)
    assert float(np.nansum(res.loss_of_reserve_static)) == pytest.approx(0.0, abs=1e-6)
    held = np.asarray(res.reserve_static)
    assert held.shape[0] == 2
    assert np.allclose(held, RATIO * LOAD, atol=1e-6)


@pytest.mark.julia
def test_merged_run_keeps_one_reserve_per_system(tmp_path):
    # A's unit has no headroom (50 MW for 50 MW of load); B's has 150 MW
    # free. Unlinked systems: B's headroom does not hold A's reserve.
    a = _system("A", [LOAD], tmp_path)
    b = _system("B", [200.0], tmp_path)
    cfg = _make_esfex_config(a, systems={"A": a, "B": b},
                             meta_network=MetaNetworkConfig(systems=["A", "B"]))
    res = _run(cfg, tmp_path)
    short = np.asarray(res.loss_of_reserve_static)
    steps = short.shape[1]
    assert float(np.nansum(short[0])) == pytest.approx(RATIO * LOAD * steps, rel=1e-6)
    assert float(np.nansum(short[1])) == pytest.approx(0.0, abs=1e-6)


@pytest.mark.julia
def test_operations_mode_applies_no_re_target(tmp_path):
    # An all-gas system with a 100 % RE target: economic dispatch must not
    # pay an RE shortfall (the target is for development runs only).
    s = _system("S", [100.0, 100.0], tmp_path, target_re=1.0)
    res = _run(_make_esfex_config(s), tmp_path)
    assert res.cost_breakdown.get("fre_penetration_cost", 0.0) == pytest.approx(0.0, abs=1e-6)
    assert res.re_penetration == pytest.approx(0.0, abs=1e-9)
