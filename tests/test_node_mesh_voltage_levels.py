"""The node-mesh reduction keeps one bus per node and voltage level.

It collapsed every node to one bus and dropped its transformers, so a
reduced case had no transformer at all: 220 and 110 kV lines met on the same
bus and the single-line diagram showed a network with no voltage levels.
Each node now keeps the levels its lines run at, joined by its own
transformers. A level the data does not connect to the rest (OSM often lacks
the transformer) joins the node's highest one: no transformer is invented.
"""

import math

import pytest

pytest.importorskip("PySide6")

from esfex.visualization.data import validation  # noqa: E402
from esfex.visualization.data.gui_model import (  # noqa: E402
    GuiBus,
    GuiGeneratorInstance,
    GuiNode,
    GuiSystemState,
    GuiTransformer,
    GuiTransmissionLine,
)


def _bus(bid, node, kv, df=0.0, lat=21.0):
    return GuiBus(bus_id=bid, name=bid, parent_node=node, voltage_kv=kv,
                  role="load" if df else "connection", demand_fraction=df,
                  frequency_hz=60.0, latitude=lat, longitude=-80.0)


def _line(lid, fb, tb, kv, cap=500.0):
    return GuiTransmissionLine(line_id=lid, from_bus=fb, to_bus=tb, capacity_mw=cap,
                               voltage_kv=kv, reactance_pu=0.05, resistance_pu=0.005)


def _tr(name, fb, tb, mva, z=0.1, losses=0.005):
    return GuiTransformer(name=name, from_bus=fb, to_bus=tb, rated_power_mva=mva,
                          impedance_pu=z, losses_fraction=losses)


def _state(buses, lines, transformers, generators=()):
    nodes = sorted({b.parent_node for b in buses})
    st = GuiSystemState()
    st.nodes = [GuiNode(index=i, name="AB"[i] if i < 2 else f"N{i}") for i in nodes]
    st.buses = {b.bus_id: b for b in buses}
    st.transmission_lines = list(lines)
    st.transformers = list(transformers)
    st.generators = {g.instance_id: g for g in generators}
    st.batteries = {}
    return st


def _lines(st):
    return {frozenset((ln.from_bus, ln.to_bus)): ln for ln in st.transmission_lines
            if not getattr(ln, "decorative", False)}


def _two_level_case():
    """Nodes A and B, each with a 220 and a 110 kV level, lines at both.
    A has two 220/110 kV transformers in parallel and a generator behind a
    13.8 kV step-up transformer; B has one 220/110 kV transformer."""
    buses = [
        _bus("a220", 0, 220.0, df=0.4), _bus("a110", 0, 110.0, df=0.6),
        _bus("a13", 0, 13.8),
        _bus("b220", 1, 220.0, lat=21.5), _bus("b110", 1, 110.0, df=1.0, lat=21.5),
    ]
    lines = [_line("hv", "a220", "b220", 220.0), _line("lv", "a110", "b110", 110.0, cap=150.0)]
    trs = [
        _tr("T1", "a220", "a110", 200.0, z=0.1, losses=0.01),
        _tr("T2", "a110", "a220", 100.0, z=0.2),        # drawn LV → HV
        _tr("GSU", "a110", "a13", 50.0),
        _tr("TB", "b220", "b110", 300.0),
    ]
    gen = GuiGeneratorInstance(instance_id="g", unit_key="uk", name="g", gen_type="Thermal",
                               fuel="Diesel", bus="a13", node=0, rated_power=40.0)
    return _state(buses, lines, trs, [gen])


def test_each_node_keeps_one_bus_per_voltage_level():
    st = _two_level_case()
    n = validation.reduce_to_node_mesh(st)
    assert n == 4
    assert {(b.bus_id, b.voltage_kv) for b in st.buses.values()} == {
        ("node_0", 220.0), ("node_0_b1", 110.0), ("node_1", 220.0), ("node_1_b1", 110.0)}
    # The highest level keeps the node's name; the others say their level
    assert st.buses["node_0"].name == "A"
    assert st.buses["node_0_b1"].name == "A (110 kV)"
    # One equivalent line per level and pair of nodes, never across levels
    assert set(_lines(st)) == {frozenset(("node_0", "node_1")),
                               frozenset(("node_0_b1", "node_1_b1"))}
    assert _lines(st)[frozenset(("node_0_b1", "node_1_b1"))].capacity_mw == 150.0


def test_transformers_between_levels_survive_combined_in_parallel():
    st = _two_level_case()
    validation.reduce_to_node_mesh(st)
    by_ends = {(t.from_bus, t.to_bus): t for t in st.transformers}
    # The generator's step-up transformer is inside the 110 kV level: absorbed
    assert set(by_ends) == {("node_0", "node_0_b1"), ("node_1", "node_1_b1")}
    a = by_ends[("node_0", "node_0_b1")]
    # Oriented HV → LV, whatever way T2 was drawn
    assert (a.from_voltage_kv, a.to_voltage_kv) == (220.0, 110.0)
    assert a.rated_power_mva == 300.0
    assert a.name == "A 220/110 kV"
    # R and X of each, as the bridge derives them, in parallel (X), with the
    # R that loses what the two lose at their flow split
    r1, r2 = 0.01 * 0.1, 0.005 * 0.2
    x1, x2 = math.sqrt(0.1 ** 2 - r1 ** 2), math.sqrt(0.2 ** 2 - r2 ** 2)
    x = 1 / (1 / x1 + 1 / x2)
    r = r1 * (x / x1) ** 2 + r2 * (x / x2) ** 2
    z = math.hypot(r, x)
    assert a.impedance_pu == pytest.approx(z)
    assert a.losses_fraction == pytest.approx(r / z)
    # A single transformer keeps its own name and values
    b = by_ends[("node_1", "node_1_b1")]
    assert (b.name, b.rated_power_mva, b.impedance_pu) == ("TB", 300.0, pytest.approx(0.1))


def test_units_and_demand_stay_on_their_level():
    st = _two_level_case()
    validation.reduce_to_node_mesh(st)
    # A bus at a level without lines (the 13.8 kV terminal) joins the
    # nearest kept level through its transformer: 110 kV, not 220 kV
    assert st.generators["g"].bus == "node_0_b1"
    # The node's demand is split as its buses carried it
    assert st.buses["node_0"].demand_fraction == pytest.approx(0.4)
    assert st.buses["node_0_b1"].demand_fraction == pytest.approx(0.6)
    assert st.buses["node_1"].demand_fraction == pytest.approx(0.0)
    assert st.buses["node_1_b1"].demand_fraction == pytest.approx(1.0)


def test_a_transformer_between_two_nodes_joins_their_levels():
    buses = [_bus("a220", 0, 220.0, df=1.0), _bus("a110", 0, 110.0),
             _bus("b220", 1, 220.0, df=1.0, lat=21.5), _bus("b110", 1, 110.0, lat=21.5)]
    st = _state(buses,
                [_line("hv", "a220", "b220", 220.0), _line("lv", "a110", "b110", 110.0)],
                [_tr("X", "a220", "b110", 400.0)])
    validation.reduce_to_node_mesh(st)
    assert len(st.buses) == 4
    [t] = st.transformers
    assert (t.from_bus, t.to_bus, t.from_node, t.to_node) == ("node_0", "node_1_b1", 0, 1)
    assert t.rated_power_mva == 400.0


def test_a_level_the_data_leaves_unconnected_joins_the_highest():
    """Node A's 110 kV bus has no transformer and its only line goes to a
    110 kV-only node C: kept apart, A's 110 kV level and C would be an island.
    A's levels merge into one bus and no transformer is invented."""
    buses = [_bus("a220", 0, 220.0, df=1.0), _bus("a110", 0, 110.0),
             _bus("b220", 1, 220.0, df=1.0, lat=21.5),
             _bus("c110", 2, 110.0, df=1.0, lat=22.0)]
    st = _state(buses,
                [_line("hv", "a220", "b220", 220.0), _line("lv", "a110", "c110", 110.0)],
                [])
    validation.reduce_to_node_mesh(st)
    assert set(st.buses) == {"node_0", "node_1", "node_2"}
    assert st.buses["node_0"].voltage_kv == 220.0
    assert st.transformers == []
    assert set(_lines(st)) == {frozenset(("node_0", "node_1")),
                               frozenset(("node_0", "node_2"))}


def test_a_level_connected_through_another_node_is_kept():
    """Node A has no 220/110 kV transformer of its own, but its 110 kV level
    reaches B's, which B's transformer joins to the 220 kV network: A keeps
    both levels (the data connects them) and still gets no transformer."""
    buses = [_bus("a220", 0, 220.0, df=1.0), _bus("a110", 0, 110.0),
             _bus("b220", 1, 220.0, df=1.0, lat=21.5), _bus("b110", 1, 110.0, lat=21.5)]
    st = _state(buses,
                [_line("hv", "a220", "b220", 220.0), _line("lv", "a110", "b110", 110.0)],
                [_tr("TB", "b220", "b110", 300.0)])
    validation.reduce_to_node_mesh(st)
    assert len(st.buses) == 4
    assert [(t.from_bus, t.to_bus) for t in st.transformers] == [("node_1", "node_1_b1")]


def test_a_node_is_drawn_as_one_substation():
    """Every level of a node sits at one point, so the single-line diagram
    draws the node as one substation with its transformers inside. Each
    level kept the mean position of its own buses, kilometres apart, and
    the diagram drew every level as a separate block. The branches keep the
    length between the buses they stand for."""
    from esfex.visualization.sld.graph_builder import build_elk_graph
    from esfex.visualization.workflows.grid_mapping_steps import _haversine_km

    buses = [_bus("a220", 0, 220.0, df=1.0, lat=21.0), _bus("a110", 0, 110.0, lat=21.1),
             _bus("b220", 1, 220.0, df=1.0, lat=21.5), _bus("b110", 1, 110.0, lat=21.8)]
    st = _state(buses,
                [_line("hv", "a220", "b220", 220.0), _line("lv", "a110", "b110", 110.0)],
                [_tr("TA", "a220", "a110", 300.0), _tr("TB", "b220", "b110", 300.0)])
    validation.reduce_to_node_mesh(st)
    for node, top in ((0, "node_0"), (1, "node_1")):
        here = {(b.latitude, b.longitude) for b in st.buses.values() if b.parent_node == node}
        assert here == {(st.buses[top].latitude, st.buses[top].longitude)}
    lv = _lines(st)[frozenset(("node_0_b1", "node_1_b1"))]
    assert lv.length_km == pytest.approx(_haversine_km(21.1, -80.0, 21.8, -80.0))
    groups = build_elk_graph(st)["nodeGroups"]
    assert sorted(len(g["busIds"]) for g in groups) == [2, 2]


def test_a_voltage_only_mismatched_lines_reach_is_not_a_level():
    """A generator terminal wired to its 34.5 kV bus by a "line", and a bus
    tagged 138 kV whose only line is a 110 kV line (an OSM tag error): no
    line runs between two buses of that voltage, so it is not a level. The
    bus joins the level it is wired to, and the reduced network has no line
    between buses of different voltages."""
    buses = [_bus("a34", 0, 34.5, df=1.0), _bus("gen", 0, 3.3),
             _bus("a110", 0, 110.0), _bus("x138", 0, 138.0),
             _bus("b110", 1, 110.0, df=1.0, lat=21.5), _bus("b34", 1, 34.5, lat=21.5)]
    st = _state(buses,
                [_line("gsu", "gen", "a34", 3.3), _line("tag", "a110", "x138", 110.0),
                 _line("lv", "a110", "b110", 110.0), _line("mv", "a34", "b34", 34.5)],
                [_tr("T", "a110", "a34", 100.0)])
    validation.reduce_to_node_mesh(st)
    assert {b.voltage_kv for b in st.buses.values() if b.parent_node == 0} == {110.0, 34.5}
    assert all(st.buses[ln.from_bus].voltage_kv == st.buses[ln.to_bus].voltage_kv
               for ln in _lines(st).values())


def test_a_stray_fragment_does_not_anchor_the_node():
    """Node T holds the main network's 275 and 77 kV levels and a 500 kV
    fragment of two buses that no transformer reaches. The fragment joins the
    nearest anchored level (275 kV); the node was merged into its highest
    level, the fragment, and every 275 kV line left became a 500/275 one."""
    buses = [_bus("t500", 0, 500.0), _bus("u500", 0, 500.0),
             _bus("t275", 0, 275.0), _bus("t77", 0, 77.0, df=1.0),
             _bus("x275", 1, 275.0, df=1.0, lat=21.5), _bus("y275", 1, 275.0, lat=21.5),
             _bus("x77", 1, 77.0, lat=21.5)]
    st = _state(buses,
                [_line("frag", "t500", "u500", 500.0), _line("hv", "t275", "x275", 275.0),
                 _line("hv2", "t275", "y275", 275.0), _line("x", "x275", "y275", 275.0),
                 _line("mv", "t77", "x77", 77.0)],
                [_tr("T", "t275", "t77", 200.0), _tr("X", "x275", "x77", 200.0)])
    validation.reduce_to_node_mesh(st)
    assert sorted(b.voltage_kv for b in st.buses.values() if b.parent_node == 0) == [77.0, 275.0]
    assert all(st.buses[ln.from_bus].voltage_kv == st.buses[ln.to_bus].voltage_kv
               for ln in _lines(st).values())
