"""Node↔bus array mapping and result-array orientation.

Regression guards for two bugs that together made a correct Japan network shed
34 % of its load and rendered the Result Viewer nonsensical:

1. The bus/node mapping was applied only when ``num_buses > num_nodes``. A
   reduced network (``reduce_to_node_mesh`` + ``drop_isolated_components``) has
   FEWER buses than nodes, so the mapping was skipped and node-indexed arrays
   were consumed positionally as bus-indexed ones — 70 % of 330 GW landed on
   the wrong bus, and bus-level results were exported as if node-level.

2. ``demand`` is persisted ``(hours, nodes)`` while every other per-node array
   is ``(nodes, hours)``. Summing the wrong axis turned the demand series into
   one value per NODE — the Net Load Heatmap then invented days (296 nodes → 13
   "days" for a 24 h run) and the dispatch chart drew a meaningless demand line.
"""

import os
from dataclasses import dataclass

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from esfex.bridge.converters import (
    expand_node_to_bus_array,
    node_bus_mapping_required,
)


@dataclass
class _Bus:
    parent_node: int


def _buses(nodes):
    return [_Bus(parent_node=n) for n in nodes]


# ── 1. mapping predicate ──────────────────────────────────────────────


def test_identity_bus_list_needs_no_mapping():
    """One bus per node, in node order → arrays already align."""
    assert node_bus_mapping_required(_buses(range(5)), 5) is False


def test_fewer_buses_than_nodes_needs_mapping():
    """The regression: a reduced network drops buses, so nodes outnumber
    buses and every array past the first gap would otherwise shift."""
    # nodes 0..5, but node 2 lost its bus
    buses = _buses([0, 1, 3, 4, 5])
    assert node_bus_mapping_required(buses, 6) is True


def test_more_buses_than_nodes_needs_mapping():
    assert node_bus_mapping_required(_buses([0, 0, 1, 1]), 2) is True


def test_reordered_bus_list_needs_mapping():
    """Same count, but not in node order — positional use would scramble."""
    assert node_bus_mapping_required(_buses([1, 0, 2]), 3) is True


def test_no_buses_needs_no_mapping():
    assert node_bus_mapping_required([], 4) is False


# ── 2. capacity placement with fewer buses than nodes ─────────────────


def test_capacity_lands_on_the_mapped_bus_not_the_node_index():
    """Node 4's capacity must reach the bus that belongs to node 4, not the
    bus that happens to sit at position 4."""
    # 6 nodes, node 2 has no bus → bus positions 0..4 ↔ nodes 0,1,3,4,5
    bus_to_node = [0, 1, 3, 4, 5]
    node_array = [0.0] * 6
    node_array[4] = 500.0                     # 500 MW at node 4
    bus_per_node = {4: 3}                     # node 4 → bus position 3

    out = expand_node_to_bus_array(
        node_array, bus_to_node, "capacity", bus_per_node=bus_per_node)

    assert len(out) == len(bus_to_node)
    assert out[3] == 500.0                    # correct bus
    assert out[4] == 0.0                      # NOT the positional index
    assert sum(out) == 500.0                  # nothing duplicated or lost


def test_capacity_without_explicit_bus_falls_back_to_the_nodes_own_bus():
    bus_to_node = [0, 1, 3, 4, 5]
    node_array = [0.0] * 6
    node_array[5] = 250.0

    out = expand_node_to_bus_array(node_array, bus_to_node, "capacity")

    assert out[4] == 250.0                    # bus position of node 5
    assert sum(out) == 250.0


def test_capacity_on_a_node_with_no_bus_is_dropped_not_misplaced():
    """A node the reduction left bus-less has nowhere to put its capacity;
    it must be dropped, never silently relocated to another node's bus."""
    bus_to_node = [0, 1, 3]
    node_array = [0.0, 0.0, 900.0, 0.0]       # node 2 has no bus
    out = expand_node_to_bus_array(node_array, bus_to_node, "capacity")
    assert sum(out) == 0.0


# ── 3. demand orientation in the Result Viewer ────────────────────────


def test_demand_is_oriented_to_node_hours():
    pytest.importorskip("PySide6")
    from esfex.visualization.panels.results_charts import (
        _demand_node_hours,
        orient_to_node_hours,
    )

    n_hours, n_nodes = 24, 296
    # demand is persisted (hours, nodes) — the opposite of every other array
    demand = np.arange(n_hours * n_nodes, dtype=float).reshape(n_hours, n_nodes)
    sc = {
        "demand": demand,
        "electricity_prices": np.zeros(n_hours),
        "loss_load": np.zeros((n_nodes, n_hours)),
    }

    oriented = _demand_node_hours(sc)
    assert oriented.shape == (n_nodes, n_hours)
    # Summing the node axis must give one value per HOUR, not per node.
    assert oriented.sum(axis=0).shape == (n_hours,)
    np.testing.assert_allclose(oriented.sum(axis=0), demand.sum(axis=1))

    # An array already in (nodes, hours) must be left alone.
    already = np.zeros((n_nodes, n_hours))
    assert orient_to_node_hours(already, n_hours).shape == (n_nodes, n_hours)


def test_missing_demand_returns_none():
    pytest.importorskip("PySide6")
    from esfex.visualization.panels.results_charts import _demand_node_hours

    assert _demand_node_hours({"electricity_prices": np.zeros(24)}) is None


# ── 4. the invariant itself: every node must have a bus ───────────────


def test_dropping_isolated_components_leaves_no_bus_less_node():
    """``drop_isolated_components`` removes buses but never nodes, so the
    Grid Builder must prune the nodes it emptied — otherwise buses < nodes
    and every per-node array is mapped onto the wrong bus downstream."""
    pytest.importorskip("PySide6")
    from esfex.visualization.data.gui_model import (
        EndpointRef, GuiBus, GuiModel, GuiNode, GuiTransmissionLine,
    )
    from esfex.visualization.data.validation import drop_isolated_components

    model = GuiModel()
    st = model.state
    # 3 nodes, one bus each; nodes 0-1 are connected, node 2 is an orphan
    # island (its single bus forms a 1-bus component).
    st.nodes = [GuiNode(index=i, name=f"n{i}", centroid_lat=float(i),
                        centroid_lng=0.0) for i in range(3)]
    for i in range(3):
        st.buses[f"node_{i}"] = GuiBus(
            bus_id=f"node_{i}", name=f"node_{i}", parent_node=i,
            voltage_kv=220.0, role="load", demand_fraction=1.0,
            latitude=float(i), longitude=0.0)
    st.transmission_lines = [GuiTransmissionLine(
        line_id="l0", from_bus="node_0", to_bus="node_1",
        from_node=0, to_node=1, capacity_mw=100.0, voltage_kv=220.0,
        from_endpoint=EndpointRef("bus", "node_0"),
        to_endpoint=EndpointRef("bus", "node_1"))]

    drop_isolated_components(st, min_buses=2, keep_largest=True)
    # The orphan's bus is gone but its node is still there — the violation.
    assert len(st.buses) == 2 and len(st.nodes) == 3

    # What the build worker now does: prune nodes left without a bus.
    orphans = [n.index for n in st.nodes
               if not any(b.parent_node == n.index for b in st.buses.values())]
    assert orphans == [2]
    for idx in sorted(orphans, reverse=True):
        model.remove_node(idx)

    # Invariant restored, and the surviving buses still point at real nodes.
    assert len(st.buses) >= len(st.nodes)
    node_indices = {n.index for n in st.nodes}
    assert node_indices == set(range(len(st.nodes)))
    assert all(b.parent_node in node_indices for b in st.buses.values())


def test_pruning_spares_nodes_that_never_had_a_bus():
    """Only nodes the component drop EMPTIED may be pruned.

    Nodes are created before any equipment is placed on them (the Grid Builder
    lays down the node scaffold first, and a build can legitimately run with no
    features at all). Deleting a node that never carried a bus would silently
    undo the user's work, so the prune keys on 'had a bus and lost it', not on
    'has no bus'."""
    pytest.importorskip("PySide6")
    from esfex.visualization.data.gui_model import (
        EndpointRef, GuiBus, GuiModel, GuiNode, GuiTransmissionLine,
    )
    from esfex.visualization.data.validation import drop_isolated_components

    model = GuiModel()
    st = model.state
    # n0,n1: connected pair. n2: its own 1-bus island (will be emptied).
    # n3: scaffolding — never had a bus at all.
    st.nodes = [GuiNode(index=i, name=f"n{i}", centroid_lat=float(i),
                        centroid_lng=0.0) for i in range(4)]
    for i in range(3):
        st.buses[f"node_{i}"] = GuiBus(
            bus_id=f"node_{i}", name=f"node_{i}", parent_node=i,
            voltage_kv=220.0, role="load", demand_fraction=1.0,
            latitude=float(i), longitude=0.0)
    st.transmission_lines = [GuiTransmissionLine(
        line_id="l0", from_bus="node_0", to_bus="node_1",
        from_node=0, to_node=1, capacity_mw=100.0, voltage_kv=220.0,
        from_endpoint=EndpointRef("bus", "node_0"),
        to_endpoint=EndpointRef("bus", "node_1"))]

    before = {b.parent_node for b in st.buses.values()}
    drop_isolated_components(st, min_buses=2, keep_largest=True)
    after = {b.parent_node for b in st.buses.values()}

    orphans = [n.index for n in st.nodes if n.index in before and n.index not in after]
    assert orphans == [2], "only the node emptied by the drop may be pruned"

    for idx in sorted(orphans, reverse=True):
        model.remove_node(idx)

    # n3 (never had a bus) survives; n2 (emptied) is gone.
    assert [n.name for n in st.nodes] == ["n0", "n1", "n3"]
