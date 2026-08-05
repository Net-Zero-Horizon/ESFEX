"""Realism / operability validation: duplicates, reserve margin, max-flow."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from esfex.visualization.data.gui_model import (
    EndpointRef, GuiBus, GuiGeneratorInstance, GuiSystemState,
    GuiTransmissionLine,
)
from esfex.visualization.data.network_validation import (
    detect_duplicate_plants, maxflow_feasibility, validate_network,
)


def _bus(bid, lat, lng, role="connection", dem=0.0):
    return GuiBus(bus_id=bid, name=bid, parent_node=0, voltage_kv=220.0,
                  latitude=lat, longitude=lng, role=role, demand_fraction=dem)


def _gen(gid, name, fuel, bus, mw):
    return GuiGeneratorInstance(instance_id=gid, unit_key=gid, name=name,
                               fuel=fuel, gen_type="Non-renewable", node=0,
                               rated_power=mw, bus=bus)


def _line(lid, a, b, cap):
    return GuiTransmissionLine(line_id=lid, from_bus=a, to_bus=b, capacity_mw=cap,
                              voltage_kv=220.0,
                              from_endpoint=EndpointRef("bus", a),
                              to_endpoint=EndpointRef("bus", b))


def test_detect_duplicate_plants_by_name():
    state = GuiSystemState(
        name="D",
        buses={"bus_0": _bus("bus_0", 0, 0), "bus_1": _bus("bus_1", 0, 5)},
        generators={
            "g0": _gen("g0", "Tomato-Atsuma Power Station", "Coal", "bus_0", 1650),
            "g1": _gen("g1", "Tomato-Atsuma power station Unit 4", "Coal", "bus_1", 700),
            "g2": _gen("g2", "Kyogoku", "Water", "bus_0", 600),
        },
    )
    dups = detect_duplicate_plants(state)
    assert len(dups) == 1                    # the two Tomato-Atsuma entries
    assert dups[0]["count"] == 2
    assert abs(dups[0]["total_mw"] - 2350) < 1e-6


def test_maxflow_detects_bottleneck():
    # 300 MW of generation, 300 MW of demand, but the only corridor is 100 MW.
    state = GuiSystemState(
        name="B",
        buses={
            "bus_0": _bus("bus_0", 0, 0),                 # gen side
            "bus_1": _bus("bus_1", 0, 1, "load", 1.0),    # demand side
        },
        generators={"g0": _gen("g0", "P", "Gas", "bus_0", 300)},
        transmission_lines=[_line("l0", "bus_0", "bus_1", 100)],  # thin corridor
    )
    fz = maxflow_feasibility(state, peak_demand_mw=300)
    assert not fz["feasible"]
    assert abs(fz["deliverable_mw"] - 100) < 1e-6
    assert abs(fz["unservable_mw"] - 200) < 1e-6


def test_feasible_when_corridor_sufficient():
    state = GuiSystemState(
        name="F",
        buses={
            "bus_0": _bus("bus_0", 0, 0),
            "bus_1": _bus("bus_1", 0, 1, "load", 1.0),
        },
        generators={"g0": _gen("g0", "P", "Gas", "bus_0", 300)},
        transmission_lines=[_line("l0", "bus_0", "bus_1", 500)],
    )
    fz = maxflow_feasibility(state, peak_demand_mw=300)
    assert fz["feasible"]
    assert abs(fz["deliverable_mw"] - 300) < 1e-6


def test_validate_network_flags_over_capacity():
    state = GuiSystemState(
        name="O",
        buses={"bus_0": _bus("bus_0", 0, 0, "load", 1.0)},
        generators={"g0": _gen("g0", "P", "Gas", "bus_0", 5000)},
    )
    rep = validate_network(state, peak_demand_mw=1000, ref_capacity_mw=1200)
    assert rep.reserve_margin == pytest.approx(4.0)
    assert any("Reserve margin" in f for f in rep.flags)
    assert any("2.4×" in f or "4.2×" in f for f in rep.flags)
