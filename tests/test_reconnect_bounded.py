"""Bounded reconnection: connect near isolated components, leave remote ones."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from esfex.bridge.topology_audit import audit_gui_state
from esfex.visualization.data.gui_model import (
    EndpointRef, GuiBus, GuiSystemState, GuiTransmissionLine,
)
from esfex.visualization.workflows.grid_mapping_builder import (
    ParseResult, _reconnect_isolated_bounded,
)


def _bus(bid, lat, lng, v=220.0):
    return GuiBus(bus_id=bid, name=bid, parent_node=0, voltage_kv=v,
                  latitude=lat, longitude=lng)


def _line(lid, a, b):
    return GuiTransmissionLine(line_id=lid, from_bus=a, to_bus=b,
                              capacity_mw=100.0, voltage_kv=220.0,
                              from_endpoint=EndpointRef("bus", a),
                              to_endpoint=EndpointRef("bus", b))


def _km_to_deg(km):
    return km / 111.0


def test_near_island_reconnects_remote_stays():
    # Main backbone: bus_0-bus_1-bus_2 along a line.
    # Near island (bus_3-bus_4) ~2 km from the backbone → should reconnect.
    # Remote island (bus_5-bus_6) ~40 km away → must stay isolated.
    d2 = _km_to_deg(2.0)
    d40 = _km_to_deg(40.0)
    state = GuiSystemState(
        name="R",
        buses={
            "bus_0": _bus("bus_0", 0.0, 0.0),
            "bus_1": _bus("bus_1", 0.0, 0.2),
            "bus_2": _bus("bus_2", 0.0, 0.4),
            "bus_3": _bus("bus_3", d2, 0.2),
            "bus_4": _bus("bus_4", d2, 0.25),
            "bus_5": _bus("bus_5", d40, 0.2),
            "bus_6": _bus("bus_6", d40, 0.25),
        },
        transmission_lines=[
            _line("l0", "bus_0", "bus_1"), _line("l1", "bus_1", "bus_2"),
            _line("l2", "bus_3", "bus_4"),
            _line("l3", "bus_5", "bus_6"),
        ],
    )
    before = audit_gui_state(state)
    assert len(before.components) == 3

    counts = _reconnect_isolated_bounded(
        state, ParseResult(), max_km=8.0, snap_km=0.5)
    assert counts["tapped"] + counts["linked"] >= 1

    after = audit_gui_state(state)
    # near island merged into main; remote island still separate → 2 components
    assert len(after.components) == 2
    main = max(after.components.values(), key=len)
    assert "bus_3" in main and "bus_4" in main   # near island reconnected
    assert "bus_5" not in main and "bus_6" not in main  # remote left isolated


def test_absorb_dysfunctional_keeps_functional():
    """Dysfunctional islands (gen XOR demand) are absorbed into the nearest kept
    bus with their gen/demand preserved; functional islands (gen+demand) stay."""
    from esfex.visualization.data.gui_model import GuiGeneratorInstance
    from esfex.visualization.workflows.grid_mapping_builder import (
        _absorb_dysfunctional_islands,
    )

    def load_bus(bid, lat, lng, dem):
        b = _bus(bid, lat, lng)
        b.role = "load"
        b.demand_fraction = dem
        return b

    d5 = _km_to_deg(5.0)
    state = GuiSystemState(
        name="A",
        buses={
            # main (has gen + demand)
            "bus_0": load_bus("bus_0", 0.0, 0.0, 0.5),
            "bus_1": _bus("bus_1", 0.0, 0.2),
            # deficit island (demand, no gen) — must be absorbed, demand moved
            "bus_2": load_bus("bus_2", d5, 0.1, 0.3),
            "bus_3": load_bus("bus_3", d5, 0.15, 0.1),
            # functional island (gen + demand) — must be kept
            "bus_4": load_bus("bus_4", _km_to_deg(80), 0.1, 0.1),
            "bus_5": _bus("bus_5", _km_to_deg(80), 0.15),
        },
        generators={
            "g_main": GuiGeneratorInstance(
                instance_id="g_main", unit_key="m", name="M", fuel="Gas",
                gen_type="Non-renewable", node=0, rated_power=100.0, bus="bus_1",
                latitude=0.0, longitude=0.2),
            "g_isl": GuiGeneratorInstance(
                instance_id="g_isl", unit_key="i", name="I", fuel="Wind",
                gen_type="Renewable", node=0, rated_power=50.0, bus="bus_5",
                latitude=_km_to_deg(80), longitude=0.15),
        },
        transmission_lines=[
            _line("l0", "bus_0", "bus_1"),
            _line("l2", "bus_2", "bus_3"),   # deficit island
            _line("l4", "bus_4", "bus_5"),   # functional island
        ],
    )
    demand_before = sum(b.demand_fraction for b in state.buses.values())

    counts = _absorb_dysfunctional_islands(state, ParseResult())

    assert counts["islands_absorbed"] == 1          # only the deficit island
    assert counts["demand_moved"] >= 1
    # deficit island buses removed
    assert "bus_2" not in state.buses and "bus_3" not in state.buses
    # functional island kept intact
    assert "bus_4" in state.buses and "bus_5" in state.buses
    assert state.generators["g_isl"].bus == "bus_5"
    # total demand preserved (moved, not dropped)
    demand_after = sum(b.demand_fraction for b in state.buses.values())
    assert abs(demand_after - demand_before) < 1e-9
