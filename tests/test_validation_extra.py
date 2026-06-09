"""Additive coverage tests for esfex.visualization.data.validation.

Targets the network-simplification / topology / repair machinery that the
existing ``test_validation.py`` / ``test_validation_cov.py`` suites leave
uncovered: dead-end detection, infrastructure & topology suggestions, the
apply functions, repair_network, auto_fix_errors, drop_* cleanups,
validate_network_integrity, apply_simplification_level, and
validate_inter_system_links.

Assertions reflect real observed behaviour.  No Qt is required: the target
imports cleanly from plain dataclasses.  A defensive PySide6 stub is
installed only if a real one is unavailable.
"""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

# ── Defensive PySide6 stub (only if real one missing) ────────────
try:  # pragma: no cover - environment dependent
    import PySide6.QtWidgets  # noqa: F401
except Exception:  # pragma: no cover
    _qtcore = ModuleType("PySide6.QtCore")
    _qtcore.QObject = type("QObject", (), {"__init__": lambda self, *a, **kw: None})
    _qtcore.Signal = lambda *a, **kw: property(lambda self: None)
    _pyside6 = ModuleType("PySide6")
    sys.modules.setdefault("PySide6", _pyside6)
    sys.modules.setdefault("PySide6.QtCore", _qtcore)

from esfex.visualization.data import validation as V
from esfex.visualization.data.gui_model import (
    EndpointRef,
    GuiACDCConverter,
    GuiBatteryInstance,
    GuiBus,
    GuiFrequencyConverter,
    GuiFuelEntryPoint,
    GuiFuelStorage,
    GuiFuelTransportRoute,
    GuiGeneratorInstance,
    GuiInterSystemLink,
    GuiNode,
    GuiNodeDemand,
    GuiTechnology,
    GuiTransformer,
    GuiTransmissionLine,
    GuiSystemState,
)


# ── builders ─────────────────────────────────────────────────────


def _node(index=0, name="N", peak=0.0, total=0.0):
    n = GuiNode(index=index, name=name)
    n.demand = GuiNodeDemand(peak_mw=peak, total_mwh=total)
    return n


def _bus(bus_id, node=0, role="connection", df=0.0, voltage=220.0,
         bus_type="PQ", lat=0.0, lng=0.0):
    return GuiBus(bus_id=bus_id, name=bus_id, parent_node=node,
                  role=role, demand_fraction=df, voltage_kv=voltage,
                  bus_type=bus_type, latitude=lat, longitude=lng)


def _gen(iid, bus="bus_0", node=0, fuel="Diesel", gen_type="Thermal",
         rated=100.0, avail="", **kw):
    return GuiGeneratorInstance(
        instance_id=iid, unit_key="uk", name=iid, gen_type=gen_type,
        fuel=fuel, bus=bus, node=node, rated_power=rated,
        availability_file=avail, **kw,
    )


def _bat(iid, bus="bus_0", node=0, fuel="None", rated=10.0, capacity=40.0, **kw):
    return GuiBatteryInstance(
        instance_id=iid, unit_key="uk", name=iid, fuel=fuel, bus=bus,
        node=node, rated_power=rated, capacity=capacity, **kw,
    )


def _line(lid, fb, tb, cap=100.0, **kw):
    return GuiTransmissionLine(line_id=lid, from_bus=fb, to_bus=tb,
                               capacity_mw=cap, **kw)


def _tr(name, fb, tb, mva=100.0, imp=0.05):
    return GuiTransformer(name=name, from_bus=fb, to_bus=tb,
                          rated_power_mva=mva, impedance_pu=imp)


def _state(**kw):
    s = GuiSystemState()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


class FakeModel:
    """Minimal GuiModel surrogate that mutates ``state`` directly.

    Mirrors the methods that validation.simplify_network /
    apply_infrastructure_simplification / apply_topology_suggestion call.
    """

    def __init__(self, state):
        self.state = state
        self._counter = 0

    # bulk update no-ops
    def begin_bulk_update(self):
        self._in_bulk = True

    def end_bulk_update(self):
        self._in_bulk = False

    def remove_line(self, line_id):
        self.state.transmission_lines = [
            ln for ln in self.state.transmission_lines if ln.line_id != line_id
        ]

    def remove_bus(self, bus_id):
        self.state.buses.pop(bus_id, None)

    def remove_fuel_route(self, route_id):
        self.state.fuel_transport_routes = [
            r for r in self.state.fuel_transport_routes if r.route_id != route_id
        ]

    def remove_fuel_entry(self, index):
        if 0 <= index < len(self.state.fuel_entry_points):
            del self.state.fuel_entry_points[index]

    def remove_fuel_storage(self, storage_id):
        self.state.fuel_storages.pop(storage_id, None)

    def add_generator_instance(self, **kw):
        self._counter += 1
        iid = f"agg_gen_{self._counter}"
        bus = kw.pop("bus")
        gen = GuiGeneratorInstance(
            instance_id=iid, unit_key=kw.pop("unit_key", "uk"),
            name=kw.pop("name", iid), gen_type=kw.pop("gen_type", "Thermal"),
            fuel=kw.pop("fuel", "Diesel"), bus=bus, node=0,
        )
        for k, v in kw.items():
            if hasattr(gen, k):
                setattr(gen, k, v)
        self.state.generators[iid] = gen
        return iid

    def add_battery_instance(self, **kw):
        self._counter += 1
        iid = f"agg_bat_{self._counter}"
        bus = kw.pop("bus")
        bat = GuiBatteryInstance(
            instance_id=iid, unit_key=kw.pop("unit_key", "uk"),
            name=kw.pop("name", iid), fuel="None", bus=bus, node=0,
        )
        for k, v in kw.items():
            if hasattr(bat, k):
                setattr(bat, k, v)
        self.state.batteries[iid] = bat
        return iid


# ══════════════════════════════════════════════════════════════════
# _load_demand_for_nodes  (csv read paths)
# ══════════════════════════════════════════════════════════════════


def test_load_demand_no_paths_returns_early():
    n = _node(0)
    n.demand = GuiNodeDemand(csv_path="", data=None)
    # No csv_path → nothing to do, no error
    V._load_demand_for_nodes([n])
    assert n.demand.data is None


def test_load_demand_missing_file_skipped(tmp_path):
    n = _node(0)
    n.demand = GuiNodeDemand(csv_path=str(tmp_path / "nope.csv"), data=None)
    V._load_demand_for_nodes([n])
    assert n.demand.data is None


def test_load_demand_reads_single_column(tmp_path):
    pytest.importorskip("pandas")
    p = tmp_path / "d.csv"
    p.write_text("10\n20\n30\n")
    n = _node(0)
    n.demand = GuiNodeDemand(csv_path=str(p), data=None)
    V._load_demand_for_nodes([n])
    assert n.demand.data == [10.0, 20.0, 30.0]
    assert n.demand.peak_mw == 30.0
    assert n.demand.num_hours == 3


def test_load_demand_multicol_picks_node_index(tmp_path):
    pytest.importorskip("pandas")
    p = tmp_path / "multi.csv"
    p.write_text("1,5,9\n2,6,10\n")
    n = _node(2)
    n.demand = GuiNodeDemand(csv_path=str(p), data=None)
    V._load_demand_for_nodes([n])
    assert n.demand.data == [9.0, 10.0]


def test_load_demand_col_out_of_range_skipped(tmp_path):
    pytest.importorskip("pandas")
    p = tmp_path / "two.csv"
    p.write_text("1,2\n3,4\n")
    n = _node(9)  # index beyond columns
    n.demand = GuiNodeDemand(csv_path=str(p), data=None)
    V._load_demand_for_nodes([n])
    assert n.demand.data is None


def test_load_demand_single_col_shared_refuses_broadcast(tmp_path):
    pytest.importorskip("pandas")
    p = tmp_path / "one.csv"
    p.write_text("5\n6\n")
    a = _node(0)
    a.demand = GuiNodeDemand(csv_path=str(p), data=None)
    b = _node(1)
    b.demand = GuiNodeDemand(csv_path=str(p), data=None)
    V._load_demand_for_nodes([a, b])
    assert a.demand.data is None and b.demand.data is None


def test_load_demand_corrupt_file_logs_and_skips(tmp_path):
    pytest.importorskip("pandas")
    # a directory masquerading via a path that exists but isn't a real csv
    p = tmp_path / "bad.csv"
    p.write_bytes(b"\x00\x01\x02 not,really\x00 valid")
    n = _node(0)
    n.demand = GuiNodeDemand(csv_path=str(p), data=None)
    # Should not raise even if pandas chokes
    V._load_demand_for_nodes([n])


# ══════════════════════════════════════════════════════════════════
# dead-end bus detection
# ══════════════════════════════════════════════════════════════════


def test_find_dead_end_buses_empty():
    assert V._find_dead_end_buses(_state()) == []


def test_find_dead_end_buses_prunes_stub():
    # bus_0 (active, demand) — line — bus_1 (dead-end leaf)
    nodes = [_node(0, peak=10.0)]
    buses = {
        "bus_0": _bus("bus_0", node=0, role="load", df=1.0),
        "bus_1": _bus("bus_1", node=0, role="connection", df=0.0),
    }
    lines = [_line("L1", "bus_0", "bus_1")]
    st = _state(nodes=nodes, buses=buses, transmission_lines=lines)
    actions = V._find_dead_end_buses(st)
    kinds = {a.action_type for a in actions}
    assert "remove_bus" in kinds
    assert any(a.element_id == "bus_1" for a in actions
               if a.action_type == "remove_bus")
    assert any(a.element_id == "L1" for a in actions
               if a.action_type == "remove_line")


def test_find_dead_end_buses_keeps_equipment_bus():
    nodes = [_node(0, peak=10.0)]
    buses = {
        "bus_0": _bus("bus_0", node=0, role="load", df=1.0),
        "bus_1": _bus("bus_1", node=0),
    }
    gens = {"g1": _gen("g1", bus="bus_1", rated=50.0)}
    lines = [_line("L1", "bus_0", "bus_1")]
    st = _state(nodes=nodes, buses=buses, generators=gens,
                transmission_lines=lines)
    actions = V._find_dead_end_buses(st)
    assert not any(a.element_id == "bus_1" and a.action_type == "remove_bus"
                   for a in actions)


def test_find_dead_end_buses_progress_callback():
    buses = {"bus_0": _bus("bus_0")}
    st = _state(nodes=[_node(0)], buses=buses)
    seen = []
    V._find_dead_end_buses(st, progress_callback=lambda s, t, d: seen.append(d))
    assert seen  # callback fired


def test_bus_has_demand_paths():
    nodes = [_node(0, peak=5.0)]
    buses = {"b": _bus("b", node=0, df=0.5)}
    st = _state(nodes=nodes, buses=buses)
    assert V._bus_has_demand(st, "b") is True
    # df = 0 → False
    buses["b"].demand_fraction = 0.0
    assert V._bus_has_demand(st, "b") is False
    # missing bus → False
    assert V._bus_has_demand(st, "missing") is False


def test_bus_has_demand_no_node():
    buses = {"b": _bus("b", node=99, df=0.5)}
    st = _state(nodes=[_node(0)], buses=buses)
    assert V._bus_has_demand(st, "b") is False


# ══════════════════════════════════════════════════════════════════
# fuel dead-end detection
# ══════════════════════════════════════════════════════════════════


def test_find_dead_end_fuel_empty():
    assert V._find_dead_end_fuel_elements(_state()) == []


def test_find_dead_end_fuel_prunes_leaf_node():
    nodes = [_node(0), _node(1)]
    routes = [GuiFuelTransportRoute(route_id="r1", fuels=["Diesel"],
                                    from_node=0, to_node=1, capacity=100.0)]
    entries = [GuiFuelEntryPoint(name="entry1", fuels=["Diesel"], node=1)]
    storages = {"s1": GuiFuelStorage(storage_id="s1", name="st", fuels=["Diesel"],
                                     node=1)}
    st = _state(nodes=nodes, fuel_transport_routes=routes,
                fuel_entry_points=entries, fuel_storages=storages)
    actions = V._find_dead_end_fuel_elements(st)
    kinds = {a.action_type for a in actions}
    # node 1 has no fuel consumers → its route/entry/storage are removed
    assert "remove_fuel_route" in kinds
    assert "remove_fuel_entry" in kinds
    assert "remove_fuel_storage" in kinds


def test_fuel_node_has_consumers():
    nodes = [_node(0)]
    gens = {"g": _gen("g", node=0, fuel="Diesel", rated=10.0)}
    st = _state(nodes=nodes, generators=gens)
    assert V._fuel_node_has_consumers(st, 0) is True
    # renewable fuel → not a consumer
    gens["g"].fuel = "Sun"
    assert V._fuel_node_has_consumers(st, 0) is False


def test_find_dead_end_buses_public_combines():
    st = _state()
    assert V.find_dead_end_buses(st) == []


# ══════════════════════════════════════════════════════════════════
# simplify_network
# ══════════════════════════════════════════════════════════════════


def test_simplify_network_applies_all_phases():
    buses = {"bus_0": _bus("bus_0"), "bus_1": _bus("bus_1")}
    lines = [_line("L1", "bus_0", "bus_1")]
    routes = [GuiFuelTransportRoute(route_id="r1", fuels=["Diesel"],
                                    from_node=0, to_node=1, capacity=1.0)]
    entries = [GuiFuelEntryPoint(name="e", fuels=["Diesel"], node=0)]
    storages = {"s1": GuiFuelStorage(storage_id="s1", name="s", fuels=["x"],
                                     node=0)}
    st = _state(buses=buses, transmission_lines=lines,
                fuel_transport_routes=routes, fuel_entry_points=entries,
                fuel_storages=storages)
    model = FakeModel(st)
    actions = [
        V.SimplificationAction("remove_line", "L1", "r"),
        V.SimplificationAction("remove_bus", "bus_1", "r"),
        V.SimplificationAction("remove_fuel_route", "r1", "r"),
        V.SimplificationAction("remove_fuel_entry", "0", "r"),
        V.SimplificationAction("remove_fuel_storage", "s1", "r"),
    ]
    applied = V.simplify_network(model, actions)
    assert applied == 5
    assert "bus_1" not in st.buses
    assert st.transmission_lines == []
    assert st.fuel_transport_routes == []
    assert st.fuel_entry_points == []
    assert "s1" not in st.fuel_storages


def test_simplify_network_skips_out_of_range_fuel_entry():
    st = _state(fuel_entry_points=[])
    model = FakeModel(st)
    actions = [V.SimplificationAction("remove_fuel_entry", "5", "r")]
    assert V.simplify_network(model, actions) == 0


# ══════════════════════════════════════════════════════════════════
# infrastructure suggestions
# ══════════════════════════════════════════════════════════════════


def _two_gen_state(level_buses=True):
    nodes = [_node(0)]
    buses = {"bus_0": _bus("bus_0", node=0)}
    gens = {
        "g1": _gen("g1", bus="bus_0", fuel="Diesel", rated=50.0),
        "g2": _gen("g2", bus="bus_0", fuel="Diesel", rated=30.0),
    }
    return _state(nodes=nodes, buses=buses, generators=gens)


def test_find_infrastructure_bus_level_merges_pair():
    st = _two_gen_state()
    sugg = V.find_infrastructure_simplifications(st, level="bus")
    assert len(sugg) == 1
    s = sugg[0]
    assert s.equipment_type == "generator"
    assert set(s.instance_ids) == {"g1", "g2"}
    assert s.total_rated_power == 80.0
    assert s.target_bus == "bus_0"
    assert s.reduction == 1


def test_find_infrastructure_no_merge_singletons():
    nodes = [_node(0)]
    buses = {"bus_0": _bus("bus_0")}
    gens = {"g1": _gen("g1", bus="bus_0")}
    st = _state(nodes=nodes, buses=buses, generators=gens)
    assert V.find_infrastructure_simplifications(st, level="bus") == []


def test_find_infrastructure_node_level_redundant_infra():
    # Two buses in node, gens on both, level="node" finds redundant infra
    nodes = [_node(0)]
    buses = {
        "bus_0": _bus("bus_0", node=0),
        "bus_1": _bus("bus_1", node=0),
    }
    gens = {
        "g1": _gen("g1", bus="bus_0", fuel="Diesel", rated=80.0),
        "g2": _gen("g2", bus="bus_1", fuel="Diesel", rated=20.0),
    }
    lines = [_line("L1", "bus_0", "bus_1")]
    st = _state(nodes=nodes, buses=buses, generators=gens,
                transmission_lines=lines)
    sugg = V.find_infrastructure_simplifications(st, level="node")
    assert len(sugg) == 1
    s = sugg[0]
    assert s.target_bus == "bus_0"  # highest capacity
    # bus_1 becomes empty after merge → removable
    assert "bus_1" in s.buses_to_remove
    assert "L1" in s.lines_to_remove


def test_find_infrastructure_battery_grouping():
    nodes = [_node(0)]
    buses = {"bus_0": _bus("bus_0", node=0)}
    bats = {
        "b1": _bat("b1", bus="bus_0", rated=5.0, capacity=20.0),
        "b2": _bat("b2", bus="bus_0", rated=15.0, capacity=60.0),
    }
    st = _state(nodes=nodes, buses=buses, batteries=bats)
    sugg = V.find_infrastructure_simplifications(st, level="bus")
    assert len(sugg) == 1
    s = sugg[0]
    assert s.equipment_type == "battery"
    assert s.total_rated_power == 20.0
    assert s.total_capacity == 80.0


def test_find_infrastructure_circuit_level():
    nodes = [_node(0)]
    buses = {
        "bus_0": _bus("bus_0", node=0),
        "bus_1": _bus("bus_1", node=0),
    }
    gens = {
        "g1": _gen("g1", bus="bus_0", fuel="Gas", rated=40.0),
        "g2": _gen("g2", bus="bus_1", fuel="Gas", rated=40.0),
    }
    lines = [_line("L1", "bus_0", "bus_1")]
    st = _state(nodes=nodes, buses=buses, generators=gens,
                transmission_lines=lines)
    sugg = V.find_infrastructure_simplifications(st, level="circuit")
    assert len(sugg) == 1
    assert sugg[0].level == "circuit"


# ══════════════════════════════════════════════════════════════════
# apply_infrastructure_simplification
# ══════════════════════════════════════════════════════════════════


def test_apply_infra_generator_merge():
    st = _two_gen_state()
    model = FakeModel(st)
    sugg = V.find_infrastructure_simplifications(st, level="bus")[0]
    new_id = V.apply_infrastructure_simplification(model, sugg)
    assert new_id
    # original gens consumed
    assert "g1" not in st.generators and "g2" not in st.generators
    agg = st.generators[new_id]
    assert agg.rated_power == 80.0


def test_apply_infra_generator_fewer_than_two_returns_empty():
    nodes = [_node(0)]
    buses = {"bus_0": _bus("bus_0")}
    gens = {"g1": _gen("g1", bus="bus_0")}
    st = _state(nodes=nodes, buses=buses, generators=gens)
    model = FakeModel(st)
    sugg = V.InfrastructureSuggestion(
        level="bus", equipment_type="generator",
        instance_ids=["g1"], target_bus="bus_0", target_unit_key="k",
        target_name="n", fuel="Diesel", gen_type="Thermal",
        total_rated_power=10.0, total_capacity=0.0, reduction=0,
    )
    assert V.apply_infrastructure_simplification(model, sugg) == ""


def test_apply_infra_battery_merge():
    nodes = [_node(0)]
    buses = {"bus_0": _bus("bus_0", node=0)}
    bats = {
        "b1": _bat("b1", bus="bus_0", rated=5.0, capacity=20.0),
        "b2": _bat("b2", bus="bus_0", rated=15.0, capacity=60.0),
    }
    st = _state(nodes=nodes, buses=buses, batteries=bats)
    model = FakeModel(st)
    sugg = V.find_infrastructure_simplifications(st, level="bus")[0]
    new_id = V.apply_infrastructure_simplification(model, sugg)
    assert new_id
    assert st.batteries[new_id].rated_power == 20.0
    assert st.batteries[new_id].capacity == 80.0


def test_apply_infra_generator_uses_weighted_centroid_when_no_bus_coords():
    nodes = [_node(0)]
    buses = {"bus_0": _bus("bus_0", node=0, lat=0.0, lng=0.0)}
    gens = {
        "g1": _gen("g1", bus="bus_0", fuel="Diesel", rated=100.0,
                   latitude=10.0, longitude=20.0),
        "g2": _gen("g2", bus="bus_0", fuel="Diesel", rated=100.0,
                   latitude=30.0, longitude=40.0),
    }
    st = _state(nodes=nodes, buses=buses, generators=gens)
    model = FakeModel(st)
    sugg = V.find_infrastructure_simplifications(st, level="bus")[0]
    new_id = V.apply_infrastructure_simplification(model, sugg)
    agg = st.generators[new_id]
    # bus has 0,0 coords → fall back to weighted centroid (avg of gens)
    assert agg.latitude == pytest.approx(20.0)
    assert agg.longitude == pytest.approx(30.0)


def test_weighted_avg_zero_weight():
    assert V._weighted_avg([1.0, 2.0], [0.0, 0.0]) == 0.0
    assert V._weighted_avg([2.0, 4.0], [1.0, 1.0]) == 3.0


# ══════════════════════════════════════════════════════════════════
# topology helpers
# ══════════════════════════════════════════════════════════════════


def test_logical_bus_adjacency_skips_wire_and_selfloop():
    buses = {"a": _bus("a"), "b": _bus("b")}
    lines = [
        _line("real", "a", "b"),
        _line("selfloop", "a", "a"),
        GuiTransmissionLine(line_id="wire", from_bus="a", to_bus="b",
                            capacity_mw=0.0,
                            from_endpoint=EndpointRef("generator", "g1"),
                            to_endpoint=EndpointRef("bus", "b")),
    ]
    st = _state(buses=buses, transmission_lines=lines)
    adj = V._logical_bus_adjacency(st)
    assert adj["a"] == {"b"} and adj["b"] == {"a"}


def test_bus_helpers_equipment_slack_demand():
    nodes = [_node(0, peak=5.0)]
    buses = {
        "a": _bus("a", node=0, bus_type="slack"),
        "b": _bus("b", node=0, df=1.0, role="load"),
    }
    gens = {"g": _gen("g", bus="b")}
    st = _state(nodes=nodes, buses=buses, generators=gens)
    assert V._bus_has_equipment(st, "b") is True
    assert V._bus_has_equipment(st, "a") is False
    assert V._bus_is_slack(st, "a") is True
    assert V._bus_is_slack(st, "b") is False
    assert V._bus_is_active(st, "a") is True   # slack
    assert V._bus_is_active(st, "b") is True   # equipment + demand


def test_node_has_demand_and_fraction_and_equipment():
    nodes = [_node(0, peak=5.0), _node(1)]
    buses = {"a": _bus("a", node=0, df=0.5), "b": _bus("b", node=1)}
    gens = {"g": _gen("g", bus="a", node=0)}
    st = _state(nodes=nodes, buses=buses, generators=gens)
    assert V._node_has_demand(st, 0) is True
    assert V._node_has_demand(st, 1) is False
    assert V._node_has_demand(st, 99) is False
    assert V._node_has_demand_fraction(st, 0) is True
    assert V._node_has_demand_fraction(st, 1) is False
    assert V._node_has_equipment(st, 0) is True
    assert V._node_has_equipment(st, 1) is False


def test_find_bridges_simple_chain():
    # a-b-c : both edges are bridges
    adj = {"a": {"b"}, "b": {"a", "c"}, "c": {"b"}}
    bridges = V._find_bridges(adj)
    assert frozenset({"a", "b"}) in bridges
    assert frozenset({"b", "c"}) in bridges


def test_find_bridges_cycle_has_no_bridge():
    adj = {"a": {"b", "c"}, "b": {"a", "c"}, "c": {"a", "b"}}
    assert V._find_bridges(adj) == set()


def test_compute_parallel_impedance():
    l1 = _line("a", "x", "y", cap=100.0, reactance_pu=0.1, resistance_pu=0.2,
               susceptance_pu=0.01, num_circuits=1)
    l2 = _line("b", "x", "y", cap=50.0, reactance_pu=0.1, resistance_pu=0.2,
               susceptance_pu=0.02, num_circuits=1)
    eq = V._compute_parallel_impedance([l1, l2])
    assert eq["capacity_mw"] == 150.0
    assert eq["reactance_pu"] == pytest.approx(0.05)
    assert eq["resistance_pu"] == pytest.approx(0.1)
    assert eq["susceptance_pu"] == pytest.approx(0.03)
    assert eq["num_circuits"] == 2


def test_compute_parallel_impedance_missing_values():
    l1 = _line("a", "x", "y", reactance_pu=0.0, resistance_pu=0.0)
    l2 = _line("b", "x", "y", reactance_pu=0.0, resistance_pu=0.0)
    eq = V._compute_parallel_impedance([l1, l2])
    assert eq["reactance_pu"] is None
    assert eq["resistance_pu"] is None
    assert eq["susceptance_pu"] is None


def test_find_parallel_lines():
    buses = {"x": _bus("x"), "y": _bus("y")}
    lines = [
        _line("L1", "x", "y", cap=100.0, reactance_pu=0.1),
        _line("L2", "x", "y", cap=50.0, reactance_pu=0.1),
    ]
    st = _state(buses=buses, transmission_lines=lines)
    sugg = V._find_parallel_lines(st, V.SimplificationConfig())
    assert len(sugg) == 1
    s = sugg[0]
    assert s.action_type == "parallel_line_merge"
    assert set(s.lines_to_remove) == {"L1", "L2"}
    assert s.elements_removed == 1


# ══════════════════════════════════════════════════════════════════
# radial / series finders
# ══════════════════════════════════════════════════════════════════


def _chain_state():
    """slack a — b — c(dead leaf), all in same node with demand on a."""
    nodes = [_node(0, peak=10.0)]
    buses = {
        "a": _bus("a", node=0, bus_type="slack"),
        "b": _bus("b", node=0),
        "c": _bus("c", node=0),
    }
    lines = [_line("L1", "a", "b"), _line("L2", "b", "c")]
    return _state(nodes=nodes, buses=buses, transmission_lines=lines)


def test_find_radial_buses_prunes_leaf():
    st = _chain_state()
    sugg = V._find_radial_buses(st)
    # c is a degree-1 unprotected leaf → pruned
    assert any("c" in s.buses_to_remove for s in sugg)


def test_find_series_buses_eliminates_passthrough():
    st = _chain_state()
    sugg = V._find_series_buses(st)
    # b is degree-2 pass-through → eliminated, merging L1+L2
    assert any("b" in s.buses_to_remove for s in sugg)
    s = next(s for s in sugg if "b" in s.buses_to_remove)
    assert s.action_type == "series_eliminate"
    assert set(s.lines_to_remove) == {"L1", "L2"}


def test_bus_degree_and_edges_for_bus():
    buses = {"a": _bus("a"), "b": _bus("b")}
    lines = [_line("L1", "a", "b")]
    trafos = [_tr("T0", "a", "b")]
    st = _state(buses=buses, transmission_lines=lines, transformers=trafos)
    adj = V._logical_bus_adjacency(st)
    assert V._bus_degree(adj, "a") == 1
    edges = V._edges_for_bus(st, "a")
    types = {e[0] for e in edges}
    assert types == {"line", "transformer"}


def test_other_bus():
    ln = _line("L", "a", "b")
    assert V._other_bus(ln, "a") == "b"
    assert V._other_bus(ln, "b") == "a"


# ══════════════════════════════════════════════════════════════════
# voltage / node collapse
# ══════════════════════════════════════════════════════════════════


def test_find_voltage_collapse_merges_low_into_high():
    nodes = [_node(0, peak=10.0)]
    buses = {
        "hv": _bus("hv", node=0, voltage=220.0, lat=0.0, lng=0.0),
        "lv": _bus("lv", node=0, voltage=66.0, lat=0.0, lng=0.0),
    }
    trafos = [_tr("T0", "hv", "lv")]
    st = _state(nodes=nodes, buses=buses, transformers=trafos)
    sugg = V._find_voltage_collapse(st, V.SimplificationConfig())
    assert len(sugg) == 1
    s = sugg[0]
    assert s.action_type == "voltage_collapse"
    assert "lv" in s.buses_to_remove
    assert s.buses_to_merge == {"lv": "hv"}


def test_find_voltage_collapse_skips_far_apart():
    nodes = [_node(0)]
    buses = {
        "hv": _bus("hv", node=0, voltage=220.0, lat=0.0, lng=0.0),
        "lv": _bus("lv", node=0, voltage=66.0, lat=10.0, lng=10.0),  # ~1500 km
    }
    st = _state(nodes=nodes, buses=buses)
    sugg = V._find_voltage_collapse(st, V.SimplificationConfig())
    assert sugg == []


def test_find_full_node_collapse():
    nodes = [_node(0)]
    buses = {
        "a": _bus("a", node=0, voltage=220.0, lat=0.0, lng=0.0),
        "b": _bus("b", node=0, voltage=220.0, lat=0.0, lng=0.0),
    }
    st = _state(nodes=nodes, buses=buses)
    sugg = V._find_full_node_collapse(st, V.SimplificationConfig())
    assert len(sugg) == 1
    assert sugg[0].action_type == "full_node_collapse"
    assert sugg[0].level == 4


def test_merge_bus_into_suggestion_slack_and_equipment():
    nodes = [_node(0, peak=10.0)]
    buses = {
        "surv": _bus("surv", node=0, voltage=220.0, role="connection"),
        "rem": _bus("rem", node=0, voltage=66.0, bus_type="slack",
                    role="load", df=1.0),
        "ext": _bus("ext", node=0, voltage=220.0),
    }
    gens = {"g": _gen("g", bus="rem", node=0)}
    lines = [_line("L1", "rem", "ext", reactance_pu=0.1)]
    st = _state(nodes=nodes, buses=buses, generators=gens,
                transmission_lines=lines)
    s = V._merge_bus_into_suggestion(st, "rem", "surv", None, 0.02)
    assert s.slack_transfer == ("rem", "surv")
    assert s.equipment_reassignment == {"g": "surv"}
    # connection bus inheriting load but having no equipment of its own
    # (the gen is still on 'rem' at suggestion-build time) becomes "load"
    assert buses["surv"].role == "load"
    assert s.demand_redistribution["surv"] == pytest.approx(1.0)
    # external line reterminated
    assert "L1" in s.lines_to_remove
    assert len(s.lines_to_create) == 1


# ══════════════════════════════════════════════════════════════════
# small generator absorb
# ══════════════════════════════════════════════════════════════════


def test_find_small_generators_and_apply():
    nodes = [_node(0)]
    buses = {"bus_0": _bus("bus_0", node=0)}
    gens = {
        "big": _gen("big", bus="bus_0", fuel="Diesel", rated=1000.0),
        "tiny": _gen("tiny", bus="bus_0", fuel="Diesel", rated=1.0),
    }
    st = _state(nodes=nodes, buses=buses, generators=gens)
    cfg = V.SimplificationConfig(small_generator_fraction=0.01)
    sugg = V._find_small_generators(st, cfg)
    assert len(sugg) == 1
    assert sugg[0].action_type == "small_gen_absorb"

    model = FakeModel(st)
    changed = V._apply_small_gen_absorb(model, sugg[0])
    assert changed == 1
    assert "tiny" not in st.generators
    assert st.generators["big"].rated_power == 1001.0


def test_find_small_generators_threshold_zero_skipped():
    nodes = [_node(0)]
    buses = {"bus_0": _bus("bus_0", node=0)}
    gens = {
        "a": _gen("a", bus="bus_0", fuel="Diesel", rated=0.0),
        "b": _gen("b", bus="bus_0", fuel="Diesel", rated=0.0),
    }
    st = _state(nodes=nodes, buses=buses, generators=gens)
    assert V._find_small_generators(st, V.SimplificationConfig()) == []


# ══════════════════════════════════════════════════════════════════
# apply_topology_suggestion
# ══════════════════════════════════════════════════════════════════


def test_apply_topology_suggestion_full():
    nodes = [_node(0)]
    buses = {
        "surv": _bus("surv", node=0, bus_type="PQ"),
        "rem": _bus("rem", node=0, bus_type="slack"),
        "ext": _bus("ext", node=0),
    }
    gens = {"g": _gen("g", bus="rem", node=0)}
    lines = [_line("L1", "rem", "ext", reactance_pu=0.1)]
    st = _state(nodes=nodes, buses=buses, generators=gens,
                transmission_lines=lines)
    model = FakeModel(st)
    sugg = V.TopologySuggestion(
        action_type="voltage_collapse", level=3, description="d",
        buses_to_remove=["rem"], buses_to_merge={"rem": "surv"},
        lines_to_remove=["L1"],
        lines_to_create=[{
            "from_bus": "surv", "to_bus": "ext", "capacity_mw": 100.0,
            "reactance_pu": 0.12, "current_type": "AC", "frequency_hz": 50.0,
        }],
        slack_transfer=("rem", "surv"),
        equipment_reassignment={"g": "surv"},
        demand_redistribution={"surv": 1.0},
    )
    changed = V.apply_topology_suggestion(model, sugg)
    assert changed > 0
    assert "rem" not in st.buses
    assert st.buses["surv"].bus_type == "slack"
    assert st.generators["g"].bus == "surv"
    assert st.buses["surv"].demand_fraction == 1.0
    # a new line was created
    assert any(ln.to_bus == "ext" for ln in st.transmission_lines)


# ══════════════════════════════════════════════════════════════════
# drop_isolated_components / drop_dangling_refs / orphans
# ══════════════════════════════════════════════════════════════════


def test_drop_isolated_components_empty():
    assert V.drop_isolated_components(_state())["buses"] == 0


def test_drop_isolated_components_drops_island():
    # big component a-b-c, island x (degree 0)
    buses = {
        "a": _bus("a"), "b": _bus("b"), "c": _bus("c"),
        "x": _bus("x"),
    }
    lines = [_line("L1", "a", "b"), _line("L2", "b", "c")]
    gens = {"g": _gen("g", bus="x")}
    st = _state(buses=buses, transmission_lines=lines, generators=gens)
    counts = V.drop_isolated_components(st, min_buses=2, keep_largest=True)
    assert "x" not in st.buses
    assert counts["buses"] == 1
    assert counts["generators"] == 1


def test_drop_isolated_components_keep_largest_no_drop():
    buses = {"a": _bus("a"), "b": _bus("b")}
    lines = [_line("L1", "a", "b")]
    st = _state(buses=buses, transmission_lines=lines)
    counts = V.drop_isolated_components(st, min_buses=5, keep_largest=True)
    # only one component which is the largest → preserved
    assert counts["buses"] == 0


def test_drop_dangling_refs():
    buses = {"a": _bus("a")}
    lines = [_line("L1", "a", "ghost"),
             GuiTransmissionLine(line_id="L2", from_bus="a", to_bus="a",
                                 capacity_mw=1.0,
                                 from_endpoint=EndpointRef("bus", "ghost"),
                                 to_endpoint=EndpointRef("bus", "a"))]
    trafos = [_tr("T", "a", "ghost")]
    gens = {"g": _gen("g", bus="ghost")}
    bats = {"b": _bat("b", bus="ghost")}
    st = _state(buses=buses, transmission_lines=lines, transformers=trafos,
                generators=gens, batteries=bats)
    counts = V.drop_dangling_refs(st)
    assert counts["lines"] == 2
    assert counts["transformers"] == 1
    assert counts["generators"] == 1
    assert counts["batteries"] == 1


def test_drop_dangling_refs_empty_buses():
    assert V.drop_dangling_refs(_state())["lines"] == 0


def test_drop_fully_orphan_buses():
    buses = {
        "used": _bus("used"),
        "orphan": _bus("orphan"),
        "slackbus": _bus("slackbus", bus_type="slack"),
        "demandbus": _bus("demandbus", df=0.5),
        "other": _bus("other"),
    }
    lines = [_line("L1", "used", "other")]
    st = _state(buses=buses, transmission_lines=lines)
    n = V._drop_fully_orphan_buses(st)
    assert n == 1
    assert "orphan" not in st.buses
    assert "slackbus" in st.buses  # preserved
    assert "demandbus" in st.buses  # preserved


def test_drop_fully_orphan_buses_empty():
    assert V._drop_fully_orphan_buses(_state()) == 0


# ══════════════════════════════════════════════════════════════════
# _sync_endpoints_to_buses
# ══════════════════════════════════════════════════════════════════


def test_sync_endpoints_fixes_stale_ref():
    buses = {"a": _bus("a"), "b": _bus("b")}
    ln = GuiTransmissionLine(
        line_id="L1", from_bus="a", to_bus="b", capacity_mw=10.0,
        from_endpoint=EndpointRef("bus", "ghost"),  # stale
        to_endpoint=EndpointRef("bus", "b"),
    )
    st = _state(buses=buses, transmission_lines=[ln])
    fixed = V._sync_endpoints_to_buses(st)
    assert fixed == 1
    assert ln.from_endpoint.element_id == "a"


# ══════════════════════════════════════════════════════════════════
# repair_network / auto_fix_errors
# ══════════════════════════════════════════════════════════════════


def test_repair_network_removes_broken_and_selfloops():
    buses = {"a": _bus("a", node=0, role="load", df=2.0),
             "b": _bus("b", node=0, role="load", df=0.0)}
    nodes = [_node(0)]
    lines = [
        _line("good", "a", "b"),
        _line("badref", "a", "ghost"),
        _line("selfloop", "a", "a"),
    ]
    trafos = [_tr("badtr", "a", "ghost"), _tr("selfloop_tr", "a", "a")]
    gens = {"g": _gen("g", bus="ghost", node=0)}
    st = _state(nodes=nodes, buses=buses, transmission_lines=lines,
                transformers=trafos, generators=gens)
    log = V.repair_network(st)
    assert any("invalid bus references" in entry for entry in log)
    assert any("self-loop" in entry for entry in log)
    # orphan gen reassigned to a bus on its node
    assert st.generators["g"].bus in buses
    # df normalized (a was 2.0 → renormalized)
    assert any("Renormalized" in entry or "demand_fraction" in entry
               for entry in log)


def test_repair_network_drops_orphan_gen_when_no_bus():
    buses = {}
    gens = {"g": _gen("g", bus="ghost", node=0)}
    st = _state(nodes=[_node(0)], buses=buses, generators=gens)
    log = V.repair_network(st)
    assert "g" not in st.generators
    assert any("Removed orphaned generator" in e for e in log)


def test_auto_fix_errors_counts():
    buses = {"a": _bus("a"), "b": _bus("b")}
    lines = [
        _line("selfloop", "a", "a"),
        _line("badref", "a", "ghost"),
    ]
    st = _state(nodes=[_node(0)], buses=buses, transmission_lines=lines)
    counts = V.auto_fix_errors(st)
    assert counts["self_loop_lines"] == 1
    assert counts["dangling_lines"] == 1
    assert "wire_lines_rebuilt" in counts


def test_rebuild_visual_wire_lines_public():
    buses = {"a": _bus("a", node=0)}
    gens = {"g": _gen("g", bus="a", node=0)}
    st = _state(nodes=[_node(0)], buses=buses, generators=gens)
    n = V.rebuild_visual_wire_lines(st)
    assert n >= 1
    # the generator now has a decorative wire line
    assert any(getattr(ln, "decorative", False)
               for ln in st.transmission_lines)


def test_rebuild_wire_lines_for_transformer():
    buses = {"a": _bus("a", node=0, voltage=220.0),
             "b": _bus("b", node=0, voltage=66.0)}
    trafos = [_tr("T0", "a", "b")]
    st = _state(nodes=[_node(0)], buses=buses, transformers=trafos)
    n = V._rebuild_visual_wire_lines(st)
    assert n == 2  # two wire lines, one per transformer side


# ══════════════════════════════════════════════════════════════════
# parallel-line consolidation / aggregate / orphan removal
# ══════════════════════════════════════════════════════════════════


def test_consolidate_parallel_lines():
    buses = {"x": _bus("x"), "y": _bus("y")}
    lines = [
        _line("L1", "x", "y", cap=100.0, reactance_pu=0.1),
        _line("L2", "x", "y", cap=50.0, reactance_pu=0.1),
    ]
    st = _state(buses=buses, transmission_lines=lines)
    merged = V._consolidate_parallel_lines(st)
    assert merged == 1
    # original two consumed, one equivalent remains
    assert len(st.transmission_lines) == 1


def test_bus_to_component_id():
    buses = {"a": _bus("a"), "b": _bus("b"), "island": _bus("island")}
    lines = [_line("L1", "a", "b")]
    st = _state(buses=buses, transmission_lines=lines)
    comp = V._bus_to_component_id(st)
    assert comp["a"] == comp["b"]
    assert comp["island"] != comp["a"]


def test_aggregate_equipment_same_component():
    buses = {"a": _bus("a", node=0), "b": _bus("b", node=0)}
    lines = [_line("L1", "a", "b")]
    gens = {
        "g1": _gen("g1", bus="a", node=0, fuel="Diesel", rated=100.0),
        "g2": _gen("g2", bus="b", node=0, fuel="Diesel", rated=50.0),
    }
    st = _state(nodes=[_node(0)], buses=buses, transmission_lines=lines,
                generators=gens)
    model = FakeModel(st)
    applied = V._aggregate_equipment(st, model)
    assert applied == 1
    # one survivor with summed power
    survivors = [g for g in st.generators.values()]
    assert len(survivors) == 1
    assert survivors[0].rated_power == 150.0


def test_aggregate_equipment_batteries():
    buses = {"a": _bus("a", node=0)}
    bats = {
        "b1": _bat("b1", bus="a", node=0, rated=10.0, capacity=40.0),
        "b2": _bat("b2", bus="a", node=0, rated=20.0, capacity=80.0),
    }
    st = _state(nodes=[_node(0)], buses=buses, batteries=bats)
    model = FakeModel(st)
    applied = V._aggregate_equipment(st, model)
    assert applied == 1
    survivors = list(st.batteries.values())
    assert len(survivors) == 1
    assert survivors[0].rated_power == 30.0
    assert survivors[0].capacity == 120.0


def test_remove_orphaned_infrastructure():
    # active a (slack) - b(empty) - c(empty leaf): b,c should go
    nodes = [_node(0, peak=10.0)]
    buses = {
        "a": _bus("a", node=0, bus_type="slack"),
        "b": _bus("b", node=0),
        "c": _bus("c", node=0),
    }
    lines = [_line("L1", "a", "b"), _line("L2", "b", "c")]
    st = _state(nodes=nodes, buses=buses, transmission_lines=lines)
    removed = V._remove_orphaned_infrastructure(st)
    assert removed > 0
    assert "a" in st.buses  # slack preserved


def test_is_safe_to_remove_bus():
    nodes = [_node(0, peak=10.0)]
    buses = {
        "a": _bus("a", node=0, bus_type="slack"),
        "b": _bus("b", node=0),
        "c": _bus("c", node=0),
    }
    lines = [_line("L1", "a", "b"), _line("L2", "b", "c")]
    st = _state(nodes=nodes, buses=buses, transmission_lines=lines)
    # 'a' is slack (active) → not safe
    assert V._is_safe_to_remove_bus(st, "a") is False
    # missing bus → not safe
    assert V._is_safe_to_remove_bus(st, "ghost") is False
    # 'c' leaf empty → safe
    assert V._is_safe_to_remove_bus(st, "c") is True


def test_cleanup_source_wire_lines():
    buses = {"a": _bus("a")}
    lines = [
        GuiTransmissionLine(line_id="w1", from_bus="a", to_bus="a",
                            capacity_mw=0.0,
                            from_endpoint=EndpointRef("generator", "g1"),
                            to_endpoint=EndpointRef("bus", "a")),
        _line("keep", "a", "a"),
    ]
    st = _state(buses=buses, transmission_lines=lines)
    removed = V._cleanup_source_wire_lines(st, "generator", "g1")
    assert removed == 1
    assert all(ln.line_id != "w1" for ln in st.transmission_lines)


def test_prune_radial_buses_and_series():
    st = _chain_state()
    pruned = V._prune_radial_buses(st)
    assert pruned >= 1

    st2 = _chain_state()
    n = V._eliminate_series_buses(st2)
    assert n >= 1


def test_eliminate_series_buses_no_candidates():
    buses = {"a": _bus("a", bus_type="slack"), "b": _bus("b", bus_type="slack")}
    lines = [_line("L1", "a", "b")]
    st = _state(nodes=[_node(0)], buses=buses, transmission_lines=lines)
    assert V._eliminate_series_buses(st) == 0


def test_remove_buses_and_their_infrastructure_migrates_equipment():
    nodes = [_node(0)]
    buses = {
        "keep": _bus("keep", node=0, voltage=220.0),
        "gone": _bus("gone", node=0, voltage=220.0),
    }
    gens = {"g": _gen("g", bus="gone", node=0)}
    lines = [_line("L1", "keep", "gone")]
    trafos = [_tr("T0", "keep", "gone")]
    st = _state(nodes=nodes, buses=buses, generators=gens,
                transmission_lines=lines, transformers=trafos)
    V._remove_buses_and_their_infrastructure(st, {"gone"})
    assert "gone" not in st.buses
    # equipment migrated to surviving sibling
    assert st.generators["g"].bus == "keep"
    # line touching removed bus dropped
    assert st.transmission_lines == []
    assert st.transformers == []


# ══════════════════════════════════════════════════════════════════
# heal disconnected demand
# ══════════════════════════════════════════════════════════════════


def test_heal_disconnected_demand_moves_to_gen_component():
    # node 0 with two components: gen comp (a) and demand-only comp (x)
    nodes = [_node(0, peak=100.0)]
    buses = {
        "a": _bus("a", node=0, role="mixed", df=0.0),
        "x": _bus("x", node=0, role="load", df=1.0),
    }
    gens = {"g": _gen("g", bus="a", node=0, rated=100.0)}
    # no line between a and x → two components within the node
    st = _state(nodes=nodes, buses=buses, generators=gens)
    healed = V._heal_disconnected_demand(st)
    assert healed >= 1
    # demand moved off x onto a
    assert buses["x"].demand_fraction == 0.0
    assert buses["a"].demand_fraction > 0.0


# ══════════════════════════════════════════════════════════════════
# topology audit (optional bridge import)
# ══════════════════════════════════════════════════════════════════


def test_validate_topology_audit_runs_or_skips():
    # Whether or not the bridge module exists, this must not raise and
    # must return a list.
    st = _state(nodes=[_node(0)], buses={"a": _bus("a")})
    issues = V._validate_topology_audit(st)
    assert isinstance(issues, list)


# ══════════════════════════════════════════════════════════════════
# apply_simplification_level (end-to-end fixpoint)
# ══════════════════════════════════════════════════════════════════


def test_apply_simplification_level_0_cleanup_only():
    buses = {"a": _bus("a", bus_type="slack"), "b": _bus("b")}
    lines = [_line("L1", "a", "b")]
    st = _state(nodes=[_node(0, peak=10.0)], buses=buses,
                transmission_lines=lines)
    model = FakeModel(st)
    log, remaining = V.apply_simplification_level(model, level=0)
    assert isinstance(log, list)
    assert isinstance(remaining, list)


def test_apply_simplification_level_1_aggregates():
    buses = {"a": _bus("a", node=0, bus_type="slack")}
    nodes = [_node(0, peak=10.0)]
    gens = {
        "g1": _gen("g1", bus="a", node=0, fuel="Diesel", rated=100.0),
        "g2": _gen("g2", bus="a", node=0, fuel="Diesel", rated=50.0),
    }
    st = _state(nodes=nodes, buses=buses, generators=gens)
    model = FakeModel(st)
    log, remaining = V.apply_simplification_level(model, level=1)
    # two diesel gens aggregated into one
    assert len(st.generators) == 1


# ══════════════════════════════════════════════════════════════════
# validate_network_integrity
# ══════════════════════════════════════════════════════════════════


def test_validate_network_integrity_orphans_and_selfloop():
    buses = {"a": _bus("a", node=0, df=1.0, role="load")}
    nodes = [_node(0, peak=10.0)]
    lines = [
        _line("ghost", "a", "missing"),
        _line("selfloop", "a", "a"),
    ]
    gens = {"g": _gen("g", bus="missing", node=0)}
    st = _state(nodes=nodes, buses=buses, transmission_lines=lines,
                generators=gens)
    issues = V.validate_network_integrity(st)
    msgs = " | ".join(i.message for i in issues)
    assert "non-existent" in msgs
    assert "self-loop" in msgs


def test_validate_network_integrity_empty_node_and_demand_balance():
    nodes = [_node(0), _node(1)]
    # node 0 has a bus with df sum != 1; node 1 has no bus
    buses = {"a": _bus("a", node=0, df=0.5, role="load")}
    st = _state(nodes=nodes, buses=buses)
    issues = V.validate_network_integrity(st)
    msgs = " | ".join(i.message for i in issues)
    assert "no buses" in msgs
    assert "demand_fraction" in msgs


def test_validate_network_integrity_disconnected_demand():
    # comp with demand but no generation → error
    nodes = [_node(0, peak=10.0)]
    buses = {"a": _bus("a", node=0, df=1.0, role="load")}
    st = _state(nodes=nodes, buses=buses)
    issues = V.validate_network_integrity(st)
    assert any("Disconnected demand" in i.message for i in issues)


def test_validate_network_integrity_isolated_generation():
    # Demand exists elsewhere in the network (so it has been estimated): a
    # separate component holding only generation is then genuinely isolated.
    nodes = [_node(0, peak=10.0)]
    buses = {
        "a": _bus("a", node=0, df=0.0),               # gen-only component
        "d": _bus("d", node=0, df=1.0, role="load"),  # demand elsewhere
    }
    gens = {"g": _gen("g", bus="a", node=0, rated=50.0)}
    st = _state(nodes=nodes, buses=buses, generators=gens)
    issues = V.validate_network_integrity(st)
    assert any("Isolated generation" in i.message for i in issues)


def test_isolated_generation_suppressed_when_no_demand_yet():
    # Demand is estimated in a later workflow step. Before it runs, no bus
    # carries demand, so the "generation but no demand" check would fire for
    # every generation island — pure noise. It must stay silent in that state.
    nodes = [_node(0)]
    buses = {"a": _bus("a", node=0, df=0.0)}
    gens = {"g": _gen("g", bus="a", node=0, rated=50.0)}
    st = _state(nodes=nodes, buses=buses, generators=gens)
    issues = V.validate_network_integrity(st)
    assert not any("Isolated generation" in i.message for i in issues)


# ══════════════════════════════════════════════════════════════════
# find_simplifications_for_level / preview aggregation
# ══════════════════════════════════════════════════════════════════


def test_find_simplifications_for_level_0():
    st = _state(nodes=[_node(0)], buses={"a": _bus("a")})
    plan = V.find_simplifications_for_level(st, level=0)
    assert plan.level == 0
    assert plan.buses_after == plan.buses_before
    assert plan.infrastructure_suggestions == []


def test_find_simplifications_for_level_1():
    buses = {"a": _bus("a", node=0)}
    gens = {
        "g1": _gen("g1", bus="a", node=0, fuel="Diesel", rated=100.0),
        "g2": _gen("g2", bus="a", node=0, fuel="Diesel", rated=50.0),
    }
    st = _state(nodes=[_node(0)], buses=buses, generators=gens)
    plan = V.find_simplifications_for_level(st, level=1)
    assert plan.infrastructure_suggestions
    assert plan.generators_after < plan.generators_before


def test_find_simplifications_for_level_4_includes_all():
    nodes = [_node(0, peak=10.0)]
    buses = {
        "a": _bus("a", node=0, bus_type="slack", voltage=220.0),
        "b": _bus("b", node=0, voltage=66.0, lat=0.0, lng=0.0),
    }
    trafos = [_tr("T0", "a", "b")]
    st = _state(nodes=nodes, buses=buses, transformers=trafos)
    plan = V.find_simplifications_for_level(st, level=4)
    # level 4 should produce some topology suggestions
    assert isinstance(plan.topology_suggestions, list)
    assert plan.buses_after <= plan.buses_before


def test_find_aggregatable_equipment_preview():
    buses = {"a": _bus("a", node=0)}
    gens = {
        "g1": _gen("g1", bus="a", node=0, fuel="Gas", rated=100.0),
        "g2": _gen("g2", bus="a", node=0, fuel="Gas", rated=20.0),
    }
    bats = {
        "b1": _bat("b1", bus="a", node=0, rated=10.0, capacity=40.0),
        "b2": _bat("b2", bus="a", node=0, rated=5.0, capacity=20.0),
    }
    st = _state(nodes=[_node(0)], buses=buses, generators=gens, batteries=bats)
    sugg = V._find_aggregatable_equipment(st)
    types = {s.equipment_type for s in sugg}
    assert "generator" in types and "battery" in types


# ══════════════════════════════════════════════════════════════════
# inter-system links
# ══════════════════════════════════════════════════════════════════


def _isl(**kw):
    base = dict(link_id="lk", link_type="transmission",
                from_system="A", to_system="B", from_node=0, to_node=0,
                capacity_mw=100.0, loss_factor=0.05)
    base.update(kw)
    return GuiInterSystemLink(**base)


def _isl_states():
    a = _state(nodes=[_node(0)], buses={"ba": _bus("ba")})
    b = _state(nodes=[_node(0)], buses={"bb": _bus("bb")})
    return {"A": a, "B": b}


def test_inter_system_links_valid_no_issues():
    states = _isl_states()
    issues = V.validate_inter_system_links([_isl()], states)
    assert issues == []


def test_inter_system_links_missing_systems():
    states = _isl_states()
    i1 = V.validate_inter_system_links([_isl(from_system="Z")], states)
    assert any("from_system" in i.message for i in i1)
    i2 = V.validate_inter_system_links([_isl(to_system="Z")], states)
    assert any("to_system" in i.message for i in i2)


def test_inter_system_links_self_loop():
    states = _isl_states()
    issues = V.validate_inter_system_links([_isl(to_system="A")], states)
    assert any("from_system == to_system" in i.message for i in issues)


def test_inter_system_links_node_out_of_range():
    states = _isl_states()
    issues = V.validate_inter_system_links(
        [_isl(from_node=5, to_node=9)], states)
    msgs = " | ".join(i.message for i in issues)
    assert "from_node 5 out of range" in msgs
    assert "to_node 9 out of range" in msgs


def test_inter_system_links_negative_scalar_and_loss():
    states = _isl_states()
    issues = V.validate_inter_system_links(
        [_isl(capacity_mw=-1.0, loss_factor=1.5)], states)
    msgs = " | ".join(i.message for i in issues)
    assert "must be ≥ 0" in msgs
    assert "loss_factor" in msgs


def test_inter_system_links_endpoint_and_zero_capacity_and_dup():
    states = _isl_states()
    link1 = _isl(link_id="lk1", capacity_mw=0.0,
                 from_endpoint=EndpointRef("bus", "ghost"),
                 to_endpoint=EndpointRef("bus", "bb"))
    link2 = _isl(link_id="lk2")  # duplicate of default endpoints? differs id only
    # Build a true duplicate of link2's ordered endpoints
    link3 = _isl(link_id="lk3")
    issues = V.validate_inter_system_links([link1, link2, link3], states)
    msgs = " | ".join(i.message for i in issues)
    assert "not in" in msgs  # endpoint warning
    assert "capacity_mw is 0" in msgs
    assert "duplicate" in msgs


def test_inter_system_links_none_input():
    assert V.validate_inter_system_links(None, {}) == []
