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


def _bus(bid, lat, lng, role="connection", dem=0.0, node=0):
    return GuiBus(bus_id=bid, name=bid, parent_node=node, voltage_kv=220.0,
                  latitude=lat, longitude=lng, role=role, demand_fraction=dem)


def _gen(gid, name, fuel, bus, mw, key=None, node=0):
    return GuiGeneratorInstance(instance_id=gid, unit_key=key or gid, name=name,
                               fuel=fuel, gen_type="Non-renewable", node=node,
                               rated_power=mw, bus=bus)


def _line(lid, a, b, cap):
    return GuiTransmissionLine(line_id=lid, from_bus=a, to_bus=b, capacity_mw=cap,
                              voltage_kv=220.0,
                              from_endpoint=EndpointRef("bus", a),
                              to_endpoint=EndpointRef("bus", b))


def test_detect_duplicate_plants_by_name():
    state = GuiSystemState(
        name="D",
        buses={"bus_0": _bus("bus_0", 0, 0), "bus_1": _bus("bus_1", 0, 0.045),
               "bus_2": _bus("bus_2", 1, 0)},
        generators={
            "g0": _gen("g0", "Tomato-Atsuma Power Station", "Coal", "bus_0", 1650),
            "g1": _gen("g1", "Tomato-Atsuma power station Unit 4", "Coal", "bus_1", 700),
            "g2": _gen("g2", "Kyogoku", "Water", "bus_2", 600),
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


# ── Wired into the Studio's Validate (#20) ───────────────────────────


def test_realism_category_runs_in_validate_state():
    """The realism checks were only reachable from Python; Validate runs them."""
    from esfex.visualization.data.gui_model import GuiNode, GuiNodeDemand
    from esfex.visualization.data.validation import CATEGORY_ORDER, validate_state

    assert "realism" in CATEGORY_ORDER
    state = GuiSystemState(
        name="B",
        nodes=[GuiNode(index=0, name="N0",
                       demand=GuiNodeDemand(peak_mw=300.0))],
        buses={
            "bus_0": _bus("bus_0", 0, 0),
            "bus_1": _bus("bus_1", 0, 1, "load", 1.0),
        },
        generators={
            "g0": _gen("g0", "Plant A", "Gas", "bus_0", 300),
            "g1": _gen("g1", "Plant A unit 2", "Gas", "bus_0", 300),
        },
        transmission_lines=[_line("l0", "bus_0", "bus_1", 100)],
    )
    issues = validate_state(state, categories={"realism"})
    msgs = [(i.severity, i.message) for i in issues]
    assert any(s == "warning" and "Transmission bottleneck" in m for s, m in msgs)
    assert any(s == "info" and "duplicate plant" in m for s, m in msgs)
    assert all(i.category == "Realism" for i in issues)


def test_duplicate_detection_scales():
    """Bucketed matching: thousands of units stay fast and match like before."""
    import time

    buses = {f"b{i}": _bus(f"b{i}", (i % 100) * 0.5, (i // 100) * 0.5)
             for i in range(2000)}
    gens = {f"g{i}": _gen(f"g{i}", f"Farm {i}x", "Sun", f"b{i}", 10)
            for i in range(2000)}
    # Two same-fuel units 1 km apart, differently written names sharing a
    # distinctive word → one group.
    buses["x0"] = _bus("x0", 40.0, 40.0)
    buses["x1"] = _bus("x1", 40.009, 40.0)
    gens["x0"] = _gen("x0", "Alpha Power Station", "Coal", "x0", 100)
    gens["x1"] = _gen("x1", "Central Termoeléctrica Alpha", "Coal", "x1", 100)
    state = GuiSystemState(name="S", buses=buses, generators=gens)
    t0 = time.perf_counter()
    dups = detect_duplicate_plants(state)
    assert time.perf_counter() - t0 < 5.0
    groups = [sorted(d["members"]) for d in dups]
    assert ["x0", "x1"] in groups
    assert len(groups) == 1


# ── Review round 2: per-node instances, per-node demand, generation ──


def test_name_match_needs_proximity():
    state = GuiSystemState(
        name="P",
        buses={"a": _bus("a", 0, 0), "b": _bus("b", 0, 5)},   # ~555 km apart
        generators={
            "g0": _gen("g0", "Plant X", "Gas", "a", 100),
            "g1": _gen("g1", "Plant X", "Gas", "b", 100),
        },
    )
    assert detect_duplicate_plants(state) == []


def test_per_node_instances_of_a_key_are_not_duplicates():
    """Cuba-like: a generator key has one instance per node (``gas_n1`` …),
    all carrying the key's name; other keys of the same fuel share their bus."""
    buses = {f"b{i}": _bus(f"b{i}", 20.0 + 0.05 * i, -80.0, node=i)
             for i in range(4)}
    gens = {}
    for i in range(4):
        gens[f"gas_n{i}"] = _gen(f"gas_n{i}", "Antonio Maceo Thermoelectric Power Plant",
                                 "Gas", f"b{i}", 100, key="gas", node=i)
        gens[f"fuel_oil_n{i}"] = _gen(f"fuel_oil_n{i}", "Generator node/9211309678",
                                      "Fuel_oil", f"b{i}", 100, key="fuel_oil", node=i)
        gens[f"fuel_oil_1_n{i}"] = _gen(f"fuel_oil_1_n{i}", "Generator node/9211309686",
                                        "Fuel_oil", f"b{i}", 300, key="fuel_oil_1", node=i)
    # A single-node key next to the aggregate one, and unnamed turbines.
    gens["gas_4_n1"] = _gen("gas_4_n1", "Boca de Jaruco", "Gas", "b1", 238,
                            key="gas_4", node=1)
    for k in range(3):
        gens[f"wind_{k}_n2"] = _gen(f"wind_{k}_n2", f"Generator node/62252719{k}",
                                    "Wind", "b2", 1.5, key=f"wind_{k}", node=2)
    state = GuiSystemState(name="C", buses=buses, generators=gens)
    assert detect_duplicate_plants(state) == []


def test_shared_name_word_matches_same_fuel_nearby():
    state = GuiSystemState(
        name="R",
        buses={"a": _bus("a", 22.0, -78.7), "b": _bus("b", 22.01, -78.7)},
        generators={
            "b0": _gen("b0", "Bioeléctrica Ciro Redondo", "Biomass", "a", 22),
            "b1": _gen("b1", "Ciro Redondo power station", "Biomass", "b", 60),
            "s0": _gen("s0", "Parque Fotovoltaico Frigorífico", "Sun", "a", 5),
            "s1": _gen("s1", "Parque Fotovoltaico Yaguaramas", "Sun", "b", 5),
        },
    )
    dups = detect_duplicate_plants(state)
    assert [sorted(d["members"]) for d in dups] == [["b0", "b1"]]


def _two_node_state(gen_mw, corridor_mw):
    """Node 0: 100 MW peak on two buses (0.5/0.5) next to the generator.
    Node 1: 300 MW peak on one bus behind a corridor."""
    from esfex.visualization.data.gui_model import GuiNode, GuiNodeDemand

    return GuiSystemState(
        name="T",
        nodes=[GuiNode(index=0, name="N0", demand=GuiNodeDemand(peak_mw=100.0)),
               GuiNode(index=1, name="N1", demand=GuiNodeDemand(peak_mw=300.0))],
        buses={
            "g": _bus("g", 0, 0),
            "l0": _bus("l0", 0, 0.1, "load", 0.5),
            "l1": _bus("l1", 0, 0.2, "load", 0.5),
            "far": _bus("far", 1, 0, "load", 1.0, node=1),
        },
        generators={"g0": _gen("g0", "P", "Gas", "g", gen_mw)},
        transmission_lines=[
            _line("a", "g", "l0", 1000), _line("b", "g", "l1", 1000),
            _line("c", "g", "far", corridor_mw),
        ],
    )


def test_maxflow_splits_each_node_peak_over_its_own_buses():
    # Node 1 needs 300 MW through a 250 MW corridor. The old split (system
    # peak × fraction / sum of all fractions) put 400 × 1/2 = 200 MW there
    # and missed the bottleneck.
    fz = maxflow_feasibility(_two_node_state(1000, 250), peak_demand_mw=400)
    assert fz["peak_demand_mw"] == pytest.approx(400)
    assert not fz["feasible"]
    assert fz["unservable_mw"] == pytest.approx(50)


def test_generation_shortfall_is_not_a_transmission_bottleneck():
    # 200 MW installed for 400 MW of peak demand; the corridors are ample.
    from esfex.visualization.data.validation import validate_state

    state = _two_node_state(200, 1000)
    fz = maxflow_feasibility(state, peak_demand_mw=400)
    assert fz["feasible"]
    assert fz["generation_shortfall_mw"] == pytest.approx(200)
    assert fz["unservable_mw"] == pytest.approx(0)
    rep = validate_network(state, peak_demand_mw=400)
    assert not any("bottleneck" in f for f in rep.flags)
    msgs = [i.message for i in validate_state(state, categories={"realism"})]
    assert not any("bottleneck" in m for m in msgs)
    # With a thin corridor, only the part generation could serve is reported.
    fz = maxflow_feasibility(_two_node_state(200, 50), peak_demand_mw=400)
    assert not fz["feasible"]
    assert fz["deliverable_mw"] == pytest.approx(150)
    assert fz["unservable_mw"] == pytest.approx(50)
