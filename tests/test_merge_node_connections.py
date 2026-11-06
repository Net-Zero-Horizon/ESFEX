"""A system wired only by ``nodes_connections`` keeps its links when merged.

Once the merged network has any per-line data (an inter-system link or
another system's lines), the dispatch builds its network from lines only, so
the internal links of a system without lines vanished from the dispatch while
the master still saw them: its nodes were islanded in operation.
"""

import sys

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator, _make_system_config  # noqa: E402

from esfex.config.schema import (  # noqa: E402
    MetaNetworkConfig,
    PenaltiesConfig,
    SystemLinkConfig,
    TemporalConfig,
)
from esfex.runner import Orchestrator  # noqa: E402


def _link():
    return SystemLinkConfig(
        systems=["A", "B"], connections=[[0, 0]], existing_capacity_MW=[150.0],
        max_investment_MW=[0.0], investment_cost_per_MW=[0.0], loss_factor=[0.0],
        distance_km=[50.0], cost_per_mw_km=[0.0])


def _two_systems(tmp_path=None, **cfg_kw):
    """A: two nodes joined by nodes_connections, load at node 1, no generation.
    B: one node with a generator, linked to A's node 0."""
    a = _make_system_config(num_nodes=2, generators={}, name="A")
    a = a.model_copy(update={
        "nodes": a.nodes.model_copy(update={"nodes_connections": [0.0, 200.0, 200.0, 0.0]}),
        "fuel_transport_distances": [[0.0, 40.0], [40.0, 0.0]],
        "penalties": PenaltiesConfig(loss_of_load=10000.0)})
    gen = _make_generator("Gas", "Non-renewable", "Gas", [300.0], degradation_rate=[0.0],
                          min_power=[0.0], invest_max_power=[0.0])
    b = _make_system_config(num_nodes=1, generators={"gas": gen}, name="B")
    b = b.model_copy(update={"penalties": PenaltiesConfig(loss_of_load=10000.0)})
    if tmp_path is not None:
        load = np.zeros((8760, 2))
        load[:, 1] = 50.0
        np.savetxt(tmp_path / "a.csv", load, delimiter=",")
        np.savetxt(tmp_path / "b.csv", np.zeros(8760))
        a = a.model_copy(update={"demand_path": str(tmp_path / "a.csv")})
        b = b.model_copy(update={"demand_path": str(tmp_path / "b.csv")})
    return _make_esfex_config(
        a, systems={"A": a, "B": b},
        meta_network=MetaNetworkConfig(systems=["A", "B"], systems_links=[_link()]), **cfg_kw)


def test_merged_dispatch_network_keeps_node_connections():
    master, op, _ = Orchestrator._merge_systems(_two_systems())
    lines = {(ln.from_bus, ln.to_bus): ln for ln in op.transmission_lines_geo}
    assert set(lines) == {(0, 2), (0, 1)}       # the link and A's own 0–1 connection
    a01 = lines[(0, 1)]
    assert a01.capacity_mw == 200.0
    dc = op.dc_power_flow
    # 40 km at voltage_level_kv on the 100 MVA base: Z_base = 220² / 100
    assert a01.reactance_pu == pytest.approx(40.0 * dc.reactance_per_km / (220.0**2 / 100.0))
    # The master sees the same two links
    assert {(ln.from_node, ln.to_node) for ln in master.transmission_lines_geo} == {(0, 1), (0, 2)}


@pytest.mark.julia
def test_load_behind_node_connections_is_served(tmp_path):
    cfg = _two_systems(
        tmp_path, simulation_mode="economic_dispatch",
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720, overlap_hours=0))
    res = Orchestrator(cfg, output_dir=tmp_path / "out").run(years=1)[0]
    # 50 MW at A's node 1 reach it from B through A's node 0
    assert float(np.nansum(res.load_shed_array)) == pytest.approx(0.0, abs=1e-3)
