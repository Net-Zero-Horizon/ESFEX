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
