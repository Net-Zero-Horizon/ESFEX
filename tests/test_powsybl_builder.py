"""Tests for the ESFEX → PowSyBl network mapping (SLD/NAD rendering)."""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

# graph/gui imports pull PySide6; stub it if missing (mirrors other tests).
try:
    import PySide6.QtCore  # noqa: F401
except Exception:
    _qtcore = ModuleType("PySide6.QtCore")
    _qtcore.QObject = type("QObject", (), {"__init__": lambda self, *a, **k: None})
    _qtcore.Signal = lambda *a, **k: property(lambda self: None)
    sys.modules.setdefault("PySide6", ModuleType("PySide6"))
    sys.modules.setdefault("PySide6.QtCore", _qtcore)

pp = pytest.importorskip("pypowsybl")

from esfex.visualization.data.gui_model import (  # noqa: E402
    GuiBus,
    GuiGeneratorInstance,
    GuiNode,
    GuiSystemState,
    GuiTransformer,
    GuiTransmissionLine,
)
from esfex.visualization.sld import powsybl_builder as pb  # noqa: E402


def _gen(iid, bus, node, mw):
    return GuiGeneratorInstance(
        instance_id=iid, unit_key="gt", name=iid, gen_type="thermal",
        fuel="gas", bus=bus, node=node, rated_power=mw)


def _state():
    st = GuiSystemState()
    st.nodes = [GuiNode(index=0, name="Alpha"), GuiNode(index=1, name="Beta")]
    st.buses = {
        "a220": GuiBus(bus_id="a220", parent_node=0, voltage_kv=220.0,
                       name="A220", role="mixed", demand_fraction=0.6),
        "a110": GuiBus(bus_id="a110", parent_node=0, voltage_kv=110.0,
                       name="A110", role="load", demand_fraction=0.4),
        "b220": GuiBus(bus_id="b220", parent_node=1, voltage_kv=220.0,
                       name="B220", role="mixed"),
    }
    st.generators = {"G1": _gen("G1", "a220", 0, 100.0)}
    st.transmission_lines = [GuiTransmissionLine(
        line_id="L12", from_bus="a220", to_bus="b220",
        capacity_mw=300.0, voltage_kv=220.0)]
    st.transformers = [GuiTransformer(
        name="T_A", from_bus="a220", to_bus="a110", from_node=0, to_node=0,
        from_voltage_kv=220.0, to_voltage_kv=110.0, rated_power_mva=150.0)]
    return st


def test_mapping_produces_expected_topology():
    n = pb.build_powsybl_network(_state())
    assert len(n.get_substations()) == 2
    assert set(n.get_voltage_levels().index) == {"VL_0_220", "VL_0_110", "VL_1_220"}
    assert len(n.get_buses()) == 3
    assert len(n.get_generators()) == 1
    assert len(n.get_lines()) == 1
    assert len(n.get_2_windings_transformers()) == 1
    # one load per load-role bus (a220 mixed, a110 load, b220 mixed)
    assert len(n.get_loads()) == 3


def test_substation_ids_one_per_node_with_buses():
    assert pb.substation_ids(_state()) == ["S0", "S1"]


def test_generator_falls_back_to_node_bus_when_bus_missing():
    st = _state()
    # generator references a non-existent bus → falls back to node 0's HV bus
    st.generators = {"G1": _gen("G1", "ghost", 0, 50.0)}
    n = pb.build_powsybl_network(st)
    gens = n.get_generators()
    assert len(gens) == 1
    assert gens.iloc[0]["voltage_level_id"] == "VL_0_220"   # highest-V bus


def test_cross_node_transformer_is_skipped():
    st = _state()
    # transformer whose ends sit in different substations cannot be a 2WT
    st.transformers = [GuiTransformer(
        name="bad", from_bus="a220", to_bus="b220", from_node=0, to_node=1,
        from_voltage_kv=220.0, to_voltage_kv=220.0, rated_power_mva=100.0)]
    n = pb.build_powsybl_network(st)
    assert len(n.get_2_windings_transformers()) == 0


def test_svg_generation_has_busbar_and_feeder_structure(tmp_path):
    n = pb.build_powsybl_network(_state())
    svg = tmp_path / "s0.svg"
    n.write_single_line_diagram_svg("S0", str(svg))
    text = svg.read_text()
    assert "sld-busbar-section" in text          # busbars with connection points
    assert "sld-two-wt" in text or "winding" in text  # transformer symbol
    assert "<svg" in text


def test_empty_state_returns_network_without_error():
    n = pb.build_powsybl_network(GuiSystemState())
    assert len(n.get_buses()) == 0
