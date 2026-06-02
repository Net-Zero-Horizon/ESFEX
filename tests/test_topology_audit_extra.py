"""Additive coverage tests for esfex.bridge.topology_audit.

Targets every branch of TopologyAuditReport, audit_gui_state,
audit_system_config and diff_audits using plain dataclasses (GUI side)
and lightweight stub objects (SystemConfig side).  No Qt or optional
heavy dependency is required.

Assertions reflect real observed behaviour.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

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

from esfex.bridge.topology_audit import (
    TopologyAuditReport,
    audit_gui_state,
    audit_system_config,
    diff_audits,
)
from esfex.visualization.data.gui_model import (
    EndpointRef,
    GuiACDCConverter,
    GuiBatteryInstance,
    GuiBus,
    GuiFrequencyConverter,
    GuiGeneratorInstance,
    GuiSystemState,
    GuiTransformer,
    GuiTransmissionLine,
)


# ── builders ─────────────────────────────────────────────────────


def _bus(bus_id, role="connection", df=0.0):
    return GuiBus(bus_id=bus_id, name=bus_id, role=role, demand_fraction=df)


def _gen(iid, bus="bus_0"):
    return GuiGeneratorInstance(
        instance_id=iid, unit_key="uk", name=iid, gen_type="Thermal",
        fuel="Diesel", bus=bus, node=0, rated_power=100.0,
        availability_file="",
    )


def _bat(iid, bus="bus_0"):
    return GuiBatteryInstance(
        instance_id=iid, unit_key="uk", name=iid, fuel="None", bus=bus,
        node=0, rated_power=10.0, capacity=40.0,
    )


def _line(lid, fb, tb, cap=100.0, **kw):
    return GuiTransmissionLine(line_id=lid, from_bus=fb, to_bus=tb,
                               capacity_mw=cap, **kw)


def _state(**kw):
    s = GuiSystemState()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _elz(bus):
    """Lightweight electrolyzer stub (only ``bus`` is consulted)."""
    return SimpleNamespace(bus=bus)


# ─────────────────────────────────────────────────────────────────
# TopologyAuditReport dataclass methods
# ─────────────────────────────────────────────────────────────────


def test_report_default_is_clean():
    rep = TopologyAuditReport()
    assert rep.is_clean() is True


@pytest.mark.parametrize("field_name,value", [
    ("orphan_buses", ["b"]),
    ("orphan_generators", ["g"]),
    ("orphan_batteries", ["bt"]),
    ("orphan_electrolyzers", ["e"]),
    ("lines_dropped_unresolved", ["l"]),
    ("lines_dropped_out_of_range", ["l2"]),
    ("inert_components", [0]),
    ("surplus_components", [1]),
])
def test_report_not_clean_per_field(field_name, value):
    rep = TopologyAuditReport()
    setattr(rep, field_name, value)
    assert rep.is_clean() is False


def test_report_summary_empty():
    rep = TopologyAuditReport()
    txt = rep.summary()
    assert "Components: 0 (largest = 0 bus(es))" in txt
    assert "Orphan buses: 0" in txt
    assert "0 gen, 0 bat, 0 elz" in txt
    assert "0 unresolved, 0 out of range" in txt
    assert "Inert components (no gen, no demand): 0" in txt
    assert "Surplus components (gen but no demand): 0" in txt


def test_report_summary_populated():
    rep = TopologyAuditReport()
    rep.components = {0: {"a", "b"}, 1: {"c"}}
    rep.orphan_buses = ["x"]
    rep.orphan_generators = ["g1"]
    rep.orphan_batteries = ["bt1"]
    rep.orphan_electrolyzers = ["e1"]
    rep.lines_dropped_unresolved = ["l1"]
    rep.lines_dropped_out_of_range = ["l2"]
    rep.inert_components = [1]
    rep.surplus_components = [0]
    txt = rep.summary()
    # largest component = 2
    assert "Components: 2 (largest = 2 bus(es))" in txt
    assert "1 gen, 1 bat, 1 elz" in txt
    assert "1 unresolved, 1 out of range" in txt


# ─────────────────────────────────────────────────────────────────
# audit_gui_state
# ─────────────────────────────────────────────────────────────────


def test_gui_orphan_equipment_detected():
    """Generators / batteries / electrolyzers referencing missing buses."""
    st = _state(
        buses={"bus_0": _bus("bus_0")},
        generators={"g_ok": _gen("g_ok", bus="bus_0"),
                    "g_bad": _gen("g_bad", bus="nope")},
        batteries={"bt_bad": _bat("bt_bad", bus="ghost")},
        electrolyzers={"e_bad": _elz("ghost"), "e_ok": _elz("bus_0")},
    )
    rep = audit_gui_state(st)
    assert rep.orphan_generators == ["g_bad"]
    assert rep.orphan_batteries == ["bt_bad"]
    assert rep.orphan_electrolyzers == ["e_bad"]


def test_gui_line_dropped_unresolved():
    """A real (non-wire) line with an unresolved endpoint is flagged."""
    st = _state(
        buses={"bus_0": _bus("bus_0")},
        transmission_lines=[_line("L1", "bus_0", "missing", cap=100.0)],
    )
    rep = audit_gui_state(st)
    assert rep.lines_dropped_unresolved == ["L1"]


def test_gui_wire_line_decorative_skipped():
    """A line explicitly marked decorative is treated as a wire (skipped)."""
    st = _state(
        buses={"bus_0": _bus("bus_0")},
        transmission_lines=[_line("Ldec", "bus_0", "missing",
                                  cap=100.0, decorative=True)],
    )
    rep = audit_gui_state(st)
    assert rep.lines_dropped_unresolved == []


def test_gui_wire_line_equipment_endpoint_zero_capacity_skipped():
    """Equipment endpoint + zero capacity ⇒ wire line, not an edge."""
    st = _state(
        buses={"bus_0": _bus("bus_0")},
        transmission_lines=[
            _line("Lwire", "bus_0", "missing", cap=0.0,
                  from_endpoint=EndpointRef("generator", "g1"),
                  to_endpoint=EndpointRef("bus", "bus_0")),
        ],
    )
    rep = audit_gui_state(st)
    assert rep.lines_dropped_unresolved == []


def test_gui_equipment_endpoint_with_capacity_is_real_edge():
    """bus↔transformer line with capacity>0 is a real edge (not a wire),
    so unresolved endpoint gets flagged."""
    st = _state(
        buses={"bus_0": _bus("bus_0")},
        transmission_lines=[
            _line("Ltrafo", "bus_0", "missing", cap=50.0,
                  to_endpoint=EndpointRef("transformer", "tr1")),
        ],
    )
    rep = audit_gui_state(st)
    assert rep.lines_dropped_unresolved == ["Ltrafo"]


def test_gui_components_two_islands_and_edge():
    """Two buses joined by a real line form one component; a third is alone."""
    st = _state(
        buses={"bus_0": _bus("bus_0"), "bus_1": _bus("bus_1"),
               "bus_2": _bus("bus_2")},
        transmission_lines=[_line("L1", "bus_0", "bus_1", cap=100.0)],
    )
    rep = audit_gui_state(st)
    sizes = sorted(len(c) for c in rep.components.values())
    assert sizes == [1, 2]


def test_gui_triangle_revisits_stacked_bus():
    """A fully-connected triangle pushes a bus onto the DFS stack twice,
    exercising the already-visited re-pop guard."""
    st = _state(
        buses={"bus_0": _bus("bus_0"), "bus_1": _bus("bus_1"),
               "bus_2": _bus("bus_2")},
        transmission_lines=[
            _line("L01", "bus_0", "bus_1", cap=100.0),
            _line("L12", "bus_1", "bus_2", cap=100.0),
            _line("L20", "bus_2", "bus_0", cap=100.0),
        ],
    )
    rep = audit_gui_state(st)
    assert len(rep.components) == 1
    assert len(next(iter(rep.components.values()))) == 3


def test_gui_self_loop_line_not_an_edge():
    """from_bus == to_bus must not create adjacency."""
    st = _state(
        buses={"bus_0": _bus("bus_0"), "bus_1": _bus("bus_1")},
        transmission_lines=[_line("Lself", "bus_0", "bus_0", cap=100.0)],
    )
    rep = audit_gui_state(st)
    # bus_0 and bus_1 each isolated
    assert sorted(len(c) for c in rep.components.values()) == [1, 1]


def test_gui_transformer_acdc_freq_edges():
    """Transformers, AC/DC and frequency converters all bridge buses."""
    st = _state(
        buses={f"bus_{i}": _bus(f"bus_{i}") for i in range(4)},
        transformers=[GuiTransformer(name="t", from_bus="bus_0",
                                     to_bus="bus_1")],
        acdc_converters=[GuiACDCConverter(name="c", from_bus="bus_1",
                                          to_bus="bus_2")],
        freq_converters=[GuiFrequencyConverter(name="f", from_bus="bus_2",
                                               to_bus="bus_3")],
    )
    rep = audit_gui_state(st)
    # All four buses chained together
    assert len(rep.components) == 1
    assert len(next(iter(rep.components.values()))) == 4


def test_gui_self_loop_transformer_acdc_freq_skipped():
    """from_bus == to_bus on each edge type does not link anything."""
    st = _state(
        buses={"bus_0": _bus("bus_0")},
        transformers=[GuiTransformer(name="t", from_bus="bus_0",
                                     to_bus="bus_0")],
        acdc_converters=[GuiACDCConverter(name="c", from_bus="bus_0",
                                          to_bus="bus_0")],
        freq_converters=[GuiFrequencyConverter(name="f", from_bus="bus_0",
                                               to_bus="bus_0")],
    )
    rep = audit_gui_state(st)
    assert len(rep.components) == 1


def test_gui_inert_component():
    """A lone bus with no gen and no demand ⇒ inert component + orphan bus."""
    st = _state(buses={"bus_0": _bus("bus_0", role="connection", df=0.0)})
    rep = audit_gui_state(st)
    assert rep.inert_components == [0]
    assert rep.surplus_components == []
    assert rep.orphan_buses == ["bus_0"]


def test_gui_surplus_component():
    """Generation but no demand ⇒ surplus, and bus is NOT an orphan."""
    st = _state(
        buses={"bus_0": _bus("bus_0")},
        generators={"g1": _gen("g1", bus="bus_0")},
    )
    rep = audit_gui_state(st)
    assert rep.surplus_components == [0]
    assert rep.inert_components == []
    assert rep.orphan_buses == []


def test_gui_demand_component_not_flagged():
    """A load bus with demand is neither inert nor surplus nor orphan."""
    st = _state(
        buses={"bus_0": _bus("bus_0", role="load", df=1.0)},
    )
    rep = audit_gui_state(st)
    assert rep.inert_components == []
    assert rep.surplus_components == []
    assert rep.orphan_buses == []


def test_gui_orphan_bus_excluded_when_holding_battery():
    """A singleton bus that holds a battery is not an orphan bus."""
    st = _state(
        buses={"bus_0": _bus("bus_0")},
        batteries={"bt": _bat("bt", bus="bus_0")},
    )
    rep = audit_gui_state(st)
    assert rep.orphan_buses == []


def test_gui_orphan_bus_excluded_when_holding_electrolyzer():
    st = _state(
        buses={"bus_0": _bus("bus_0")},
        electrolyzers={"e": _elz("bus_0")},
    )
    rep = audit_gui_state(st)
    assert rep.orphan_buses == []


def test_gui_no_electrolyzers_attr():
    """When the state has no ``electrolyzers`` attribute the elz branches
    are skipped without error.  GuiSystemState always defines the attr, so
    emulate an older state object via a lightweight surrogate."""
    surrogate = SimpleNamespace(
        buses={"bus_0": _bus("bus_0")},
        generators={},
        batteries={},
        transmission_lines=[],
        transformers=[],
        acdc_converters=[],
    )
    rep = audit_gui_state(surrogate)
    assert rep.orphan_electrolyzers == []
    assert rep.orphan_buses == ["bus_0"]


# ─────────────────────────────────────────────────────────────────
# audit_system_config
# ─────────────────────────────────────────────────────────────────


def _cfg(buses, lines=None, transformers=None, acdc=None, **extra):
    ns = SimpleNamespace(
        buses=buses,
        transmission_lines_geo=lines or [],
        transformers=transformers or [],
        acdc_converters=acdc or [],
    )
    for k, v in extra.items():
        setattr(ns, k, v)
    return ns


def _cfgbus(bus_id):
    return SimpleNamespace(bus_id=bus_id)


def _cfgline(line_id, fb, tb, ft=None, tt=None):
    return SimpleNamespace(line_id=line_id, from_bus=fb, to_bus=tb,
                           from_endpoint_type=ft, to_endpoint_type=tt)


def _cfgedge(fb, tb):
    return SimpleNamespace(from_bus=fb, to_bus=tb)


def test_cfg_empty_buses_returns_empty():
    rep = audit_system_config(_cfg(buses=[]))
    assert rep.components == {}
    assert rep.lines_dropped_unresolved == []


def test_cfg_line_wire_endpoint_skipped():
    cfg = _cfg(
        buses=[_cfgbus("b0"), _cfgbus("b1")],
        lines=[_cfgline("Lw", 0, None, ft="transformer")],
    )
    rep = audit_system_config(cfg)
    assert rep.lines_dropped_unresolved == []


def test_cfg_line_unresolved_endpoint():
    cfg = _cfg(
        buses=[_cfgbus("b0"), _cfgbus("b1")],
        lines=[_cfgline("Lu", None, 1)],
    )
    rep = audit_system_config(cfg)
    assert rep.lines_dropped_unresolved == ["Lu"]


def test_cfg_line_unresolved_missing_line_id():
    """from_bus None and no line_id ⇒ defaults to '?'."""
    line = SimpleNamespace(from_bus=None, to_bus=1,
                           from_endpoint_type=None, to_endpoint_type=None)
    cfg = _cfg(buses=[_cfgbus("b0"), _cfgbus("b1")], lines=[line])
    rep = audit_system_config(cfg)
    assert rep.lines_dropped_unresolved == ["?"]


def test_cfg_line_out_of_range():
    cfg = _cfg(
        buses=[_cfgbus("b0"), _cfgbus("b1")],
        lines=[_cfgline("Lo", 0, 5)],
    )
    rep = audit_system_config(cfg)
    assert rep.lines_dropped_out_of_range == ["Lo"]


def test_cfg_components_via_line():
    cfg = _cfg(
        buses=[_cfgbus("b0"), _cfgbus("b1"), _cfgbus("b2")],
        lines=[_cfgline("L", 0, 1)],
    )
    rep = audit_system_config(cfg)
    sizes = sorted(len(c) for c in rep.components.values())
    assert sizes == [1, 2]
    # component contents are bus_ids (not indices)
    all_ids = set().union(*rep.components.values())
    assert all_ids == {"b0", "b1", "b2"}


def test_cfg_triangle_revisits_stacked_bus():
    """Triangle of lines exercises the already-visited re-pop guard."""
    cfg = _cfg(
        buses=[_cfgbus("b0"), _cfgbus("b1"), _cfgbus("b2")],
        lines=[_cfgline("L01", 0, 1), _cfgline("L12", 1, 2),
               _cfgline("L20", 2, 0)],
    )
    rep = audit_system_config(cfg)
    assert len(rep.components) == 1
    assert len(next(iter(rep.components.values()))) == 3


def test_cfg_self_loop_line_no_edge():
    cfg = _cfg(
        buses=[_cfgbus("b0"), _cfgbus("b1")],
        lines=[_cfgline("L", 0, 0)],
    )
    rep = audit_system_config(cfg)
    assert sorted(len(c) for c in rep.components.values()) == [1, 1]


def test_cfg_line_none_endpoints_skipped_in_adjacency():
    """A line with None endpoints is skipped in the component build."""
    cfg = _cfg(
        buses=[_cfgbus("b0"), _cfgbus("b1")],
        lines=[_cfgline("L", None, None)],
    )
    rep = audit_system_config(cfg)
    assert sorted(len(c) for c in rep.components.values()) == [1, 1]


def test_cfg_line_wire_endpoint_tt_skipped_in_adjacency():
    """to_endpoint_type wire ⇒ excluded from adjacency too."""
    cfg = _cfg(
        buses=[_cfgbus("b0"), _cfgbus("b1")],
        lines=[_cfgline("L", 0, 1, tt="battery")],
    )
    rep = audit_system_config(cfg)
    assert sorted(len(c) for c in rep.components.values()) == [1, 1]


def test_cfg_transformer_and_acdc_edges():
    cfg = _cfg(
        buses=[_cfgbus("b0"), _cfgbus("b1"), _cfgbus("b2")],
        transformers=[_cfgedge(0, 1)],
        acdc=[_cfgedge(1, 2)],
    )
    rep = audit_system_config(cfg)
    assert len(rep.components) == 1


def test_cfg_transformer_none_and_oor_skipped():
    cfg = _cfg(
        buses=[_cfgbus("b0"), _cfgbus("b1")],
        transformers=[_cfgedge(None, 1), _cfgedge(0, 9)],
    )
    rep = audit_system_config(cfg)
    assert sorted(len(c) for c in rep.components.values()) == [1, 1]


def test_cfg_acdc_none_and_oor_skipped():
    cfg = _cfg(
        buses=[_cfgbus("b0"), _cfgbus("b1")],
        acdc=[_cfgedge(None, 0), _cfgedge(0, 9)],
    )
    rep = audit_system_config(cfg)
    assert sorted(len(c) for c in rep.components.values()) == [1, 1]


def test_cfg_no_transmission_lines_geo_attr():
    """Absent transmission_lines_geo attribute defaults to empty list."""
    ns = SimpleNamespace(buses=[_cfgbus("b0")], transformers=[],
                         acdc_converters=[])
    rep = audit_system_config(ns)
    assert len(rep.components) == 1


def test_cfg_none_transformers_and_acdc():
    ns = SimpleNamespace(buses=[_cfgbus("b0")], transmission_lines_geo=[],
                         transformers=None, acdc_converters=None)
    rep = audit_system_config(ns)
    assert len(rep.components) == 1


# ─────────────────────────────────────────────────────────────────
# diff_audits
# ─────────────────────────────────────────────────────────────────


def test_diff_identical_no_messages():
    a = TopologyAuditReport()
    a.components = {0: {"x", "y"}}
    b = TopologyAuditReport()
    b.components = {0: {"x", "y"}}
    assert diff_audits(a, b) == []


def test_diff_component_count_differs():
    g = TopologyAuditReport()
    g.components = {0: {"a"}, 1: {"b"}}
    c = TopologyAuditReport()
    c.components = {0: {"a", "b"}}
    msgs = diff_audits(g, c)
    assert any("Component count differs" in m for m in msgs)
    assert any("Largest component size differs" in m for m in msgs)


def test_diff_dropped_lines_only_in_solver():
    g = TopologyAuditReport()
    g.components = {0: {"a"}}
    g.lines_dropped_unresolved = []
    c = TopologyAuditReport()
    c.components = {0: {"a"}}
    c.lines_dropped_unresolved = ["L1", "L2"]
    msgs = diff_audits(g, c)
    assert any("Lines dropped only in solver path" in m for m in msgs)
    assert "['L1', 'L2']" in "\n".join(msgs)


def test_diff_dropped_lines_overflow_more():
    g = TopologyAuditReport()
    g.components = {0: {"a"}}
    c = TopologyAuditReport()
    c.components = {0: {"a"}}
    c.lines_dropped_unresolved = [f"L{i}" for i in range(13)]
    msgs = diff_audits(g, c)
    joined = "\n".join(msgs)
    assert "(+3 more)" in joined  # 13 - 10


def test_diff_empty_reports():
    assert diff_audits(TopologyAuditReport(), TopologyAuditReport()) == []
