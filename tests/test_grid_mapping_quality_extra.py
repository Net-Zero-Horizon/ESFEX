"""Additive unit tests for grid_mapping_quality.

Targets the completeness predicates, physical-default heuristics, frequency
inference, and the four state-repair passes. State objects are lightweight
duck-typed stubs because the production functions only ever access attributes
via plain attribute access or ``getattr(..., default)``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from esfex.visualization.workflows import grid_mapping_quality as Q


# ── GridFeature stub ─────────────────────────────────────────────────


def feat(feature_type, **kw):
    """A minimal duck-typed GridFeature carrying the fields the predicate
    functions read. Defaults mirror the real dataclass defaults."""
    base = dict(
        feature_type=feature_type,
        name="",
        capacity_mw=0.0,
        energy_mwh=0.0,
        fuel="",
        gen_type="",
        voltage_kv=0.0,
        voltage_kv_secondary=0.0,
        line_coords=[],
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ── is_feature_complete ──────────────────────────────────────────────


def test_generator_complete_and_incomplete():
    assert Q.is_feature_complete(feat("generator", capacity_mw=10, fuel="coal"))
    assert Q.is_feature_complete(
        feat("generator", capacity_mw=10, gen_type="Renewable"))
    # zero capacity → incomplete
    assert not Q.is_feature_complete(feat("generator", capacity_mw=0, fuel="coal"))
    # capacity but no fuel/type → incomplete
    assert not Q.is_feature_complete(feat("generator", capacity_mw=10))


def test_battery_complete_requires_power_or_energy():
    assert Q.is_feature_complete(feat("battery", capacity_mw=5))
    assert Q.is_feature_complete(feat("battery", energy_mwh=20))
    assert not Q.is_feature_complete(feat("battery"))


def test_line_needs_geometry_and_voltage():
    coords = [(0.0, 0.0), (1.0, 1.0)]
    assert Q.is_feature_complete(feat("line", line_coords=coords, voltage_kv=110))
    # one point only → incomplete
    assert not Q.is_feature_complete(
        feat("line", line_coords=[(0.0, 0.0)], voltage_kv=110))
    # no voltage → incomplete
    assert not Q.is_feature_complete(feat("line", line_coords=coords, voltage_kv=0))


def test_transformer_either_side():
    assert Q.is_feature_complete(feat("transformer", voltage_kv=220))
    assert Q.is_feature_complete(feat("transformer", voltage_kv_secondary=110))
    assert not Q.is_feature_complete(feat("transformer"))


def test_substation_and_converter():
    assert Q.is_feature_complete(feat("substation", voltage_kv=220))
    assert not Q.is_feature_complete(feat("substation"))
    assert Q.is_feature_complete(feat("converter", voltage_kv=500))
    assert not Q.is_feature_complete(feat("converter"))


def test_fuel_entry_storage_need_name():
    assert Q.is_feature_complete(feat("fuel_entry", name="Port"))
    assert not Q.is_feature_complete(feat("fuel_entry"))
    assert Q.is_feature_complete(feat("fuel_storage", name="Tank"))
    assert not Q.is_feature_complete(feat("fuel_storage"))


def test_unknown_type_not_filtered():
    assert Q.is_feature_complete(feat("mystery"))


# ── reason_incomplete ────────────────────────────────────────────────


def test_reason_generator():
    assert Q.reason_incomplete(feat("generator")) == "no capacity"
    assert Q.reason_incomplete(feat("generator", capacity_mw=5)) == "no fuel/type"
    assert Q.reason_incomplete(
        feat("generator", capacity_mw=5, fuel="coal")) == ""


def test_reason_battery():
    assert Q.reason_incomplete(feat("battery")) == "no power or energy capacity"
    assert Q.reason_incomplete(feat("battery", capacity_mw=1)) == ""


def test_reason_line():
    assert Q.reason_incomplete(feat("line")) == "no geometry"
    coords = [(0.0, 0.0), (1.0, 1.0)]
    assert Q.reason_incomplete(feat("line", line_coords=coords)) == "no voltage"
    assert Q.reason_incomplete(
        feat("line", line_coords=coords, voltage_kv=110)) == ""


def test_reason_transformer_substation_converter():
    assert Q.reason_incomplete(
        feat("transformer")) == "no voltage on either side"
    assert Q.reason_incomplete(feat("transformer", voltage_kv=220)) == ""
    assert Q.reason_incomplete(feat("substation")) == "no voltage"
    assert Q.reason_incomplete(feat("converter")) == "no voltage"


def test_reason_fuel_entry_and_unknown():
    assert Q.reason_incomplete(feat("fuel_entry")) == "no name"
    assert Q.reason_incomplete(feat("fuel_storage", name="x")) == ""
    # unknown type returns "" (falls through)
    assert Q.reason_incomplete(feat("whatever")) == ""


# ── estimate_line_rxb_per_km ─────────────────────────────────────────


@pytest.mark.parametrize("v,expected", [
    # Nearest standard type; b = omega(50Hz) * C(nF) -> microsiemens/km.
    (500.0, (0.020, 0.270, 4.084)),
    (600.0, (0.020, 0.270, 4.084)),   # nearest 500
    (345.0, (0.030, 0.246, 4.335)),   # nearest 380
    (220.0, (0.060, 0.301, 3.927)),
    (110.0, (0.095, 0.380, 2.890)),
    (66.0,  (0.150, 0.400, 2.765)),
    (33.0,  (0.250, 0.400, 2.670)),
    (10.0,  (0.250, 0.400, 2.670)),   # nearest 33
    (0.0,   (0.095, 0.380, 2.890)),   # <=0 -> 110 default type
])
def test_estimate_line_rxb_per_km(v, expected):
    assert Q.estimate_line_rxb_per_km(v) == pytest.approx(expected, rel=1e-3)


def test_estimate_line_rxb_negative_fallback():
    # Non-positive voltage falls back to the 110 kV default standard type.
    assert Q.estimate_line_rxb_per_km(-5.0) == pytest.approx(
        (0.095, 0.380, 2.890), rel=1e-3)


# ── estimate_line_pu_params ──────────────────────────────────────────


def test_estimate_line_pu_zero_guards():
    assert Q.estimate_line_pu_params(0, 10) == (0.0, 0.0, 0.0)
    assert Q.estimate_line_pu_params(220, 0) == (0.0, 0.0, 0.0)
    assert Q.estimate_line_pu_params(-1, -1) == (0.0, 0.0, 0.0)


def test_estimate_line_pu_values():
    r, x, b = Q.estimate_line_pu_params(220.0, 100.0, base_mva=100.0)
    z_base = (220.0 ** 2) / 100.0
    rk, xk, bk = Q.estimate_line_rxb_per_km(220.0)
    assert r == pytest.approx((rk * 100.0) / z_base)
    assert x == pytest.approx((xk * 100.0) / z_base)
    assert b == pytest.approx((bk * 1e-6 * 100.0) * z_base)


# ── estimate_transformer_impedance_pu ────────────────────────────────


def test_transformer_impedance_branches():
    assert Q.estimate_transformer_impedance_pu(0) == 0.10
    assert Q.estimate_transformer_impedance_pu(-5) == 0.10
    assert Q.estimate_transformer_impedance_pu(5) == 0.06
    assert Q.estimate_transformer_impedance_pu(50) == 0.10
    # large unit: 0.10 * 100 / 1000 = 0.01, within clamp
    assert Q.estimate_transformer_impedance_pu(1000) == pytest.approx(0.01)
    # very large → clamped to lower bound 0.005
    assert Q.estimate_transformer_impedance_pu(100000) == 0.005
    # just at 100 boundary → scaled branch: 0.10*100/100 = 0.10
    assert Q.estimate_transformer_impedance_pu(100) == pytest.approx(0.10)
    # ratio arg is ignored
    assert Q.estimate_transformer_impedance_pu(5, ratio=99) == 0.06


# ── estimate_transformer_losses_fraction ─────────────────────────────


def test_transformer_losses_branches():
    assert Q.estimate_transformer_losses_fraction(0) == 0.005
    assert Q.estimate_transformer_losses_fraction(-1) == 0.005
    assert Q.estimate_transformer_losses_fraction(5) == 0.008
    assert Q.estimate_transformer_losses_fraction(50) == 0.005
    assert Q.estimate_transformer_losses_fraction(500) == 0.004


# ── estimate_battery_efficiencies ────────────────────────────────────


@pytest.mark.parametrize("hint,expected", [
    ("pumped-hydro", (0.85, 0.90)),
    ("PHS", (0.85, 0.90)),
    ("lead-acid", (0.85, 0.85)),
    ("flow", (0.80, 0.85)),
    ("vanadium", (0.80, 0.85)),
    ("VRFB", (0.80, 0.85)),
    ("sodium-sulfur", (0.85, 0.90)),
    ("NaS", (0.85, 0.90)),
    ("lithium-ion", (0.95, 0.95)),
    ("", (0.95, 0.95)),
])
def test_battery_efficiencies(hint, expected):
    assert Q.estimate_battery_efficiencies(hint) == expected


def test_battery_efficiencies_none_input():
    assert Q.estimate_battery_efficiencies(None) == (0.95, 0.95)


# ── estimate_generator_defaults ──────────────────────────────────────


def test_generator_defaults_known_and_unknown():
    d = Q.estimate_generator_defaults("coal")
    assert d["eff_at_rated"] == 0.38
    assert Q.estimate_generator_defaults("unobtainium") == {}


# ── infer_frequency_hz ───────────────────────────────────────────────


@pytest.mark.parametrize("lat,lng,expected", [
    (40.0, -100.0, 60.0),    # USA
    (22.0, -82.0, 60.0),     # Cuba / Caribbean
    (-15.0, -50.0, 60.0),    # Brazil
    (25.0, 45.0, 60.0),      # Saudi Arabia
    (37.0, 127.0, 60.0),     # South Korea
    (14.0, 121.0, 60.0),     # Philippines
    (23.5, 121.0, 60.0),     # Taiwan
    (35.0, 140.0, 60.0),     # East Japan
    (6.0, -9.0, 60.0),       # Liberia
    (48.0, 2.0, 50.0),       # Paris → 50 Hz
    (35.0, 134.0, 50.0),     # West Japan → 50 Hz
    (-33.0, 151.0, 50.0),    # Sydney → 50 Hz
])
def test_infer_frequency(lat, lng, expected):
    assert Q.infer_frequency_hz(lat, lng) == expected


# ── apply_realistic_generator_defaults ───────────────────────────────


class Gen:
    def __init__(self, **kw):
        self.fuel = kw.get("fuel", "")
        self.rated_power = kw.get("rated_power", 0.0)
        self.min_power = kw.get("min_power", 0.0)
        self.ramp_up = kw.get("ramp_up", 0.0)
        self.ramp_down = kw.get("ramp_down", 0.0)
        self.min_up = kw.get("min_up", 0)
        self.min_down = kw.get("min_down", 0)
        self.inertia = kw.get("inertia", 0.0)
        self.start_up_cost = kw.get("start_up_cost", 0.0)
        self.eff_at_rated = kw.get("eff_at_rated", 0.35)
        self.eff_at_min = kw.get("eff_at_min", 0.25)
        self.bus = kw.get("bus", "")


def test_apply_defaults_empty_state():
    state = SimpleNamespace(generators={})
    assert Q.apply_realistic_generator_defaults(state) == {
        "min_power": 0, "ramp_up": 0, "ramp_down": 0,
        "min_up": 0, "min_down": 0, "inertia": 0,
        "start_up_cost": 0, "eff_at_rated": 0, "eff_at_min": 0,
    }


def test_apply_defaults_skips_unknown_fuel_and_zero_power():
    # gen with no fuel → canonical "" → no defaults → skip
    g1 = Gen(fuel="", rated_power=100)
    # gen with unknown fuel → skip
    g2 = Gen(fuel="dilithium", rated_power=100)
    # gen with rated_power <= 0 → skip
    g3 = Gen(fuel="coal", rated_power=0)
    state = SimpleNamespace(generators={"g1": g1, "g2": g2, "g3": g3})
    counts = Q.apply_realistic_generator_defaults(state)
    assert all(v == 0 for v in counts.values())


def test_apply_defaults_repairs_degenerate_coal():
    # coal: min_power_frac 0.40, ramps 0.6, min_up/down 8, inertia 6,
    # start_up_cost_per_mw 90.
    g = Gen(fuel="coal", rated_power=500,
            min_power=0.0, ramp_up=0.0, ramp_down=0.0,
            min_up=0, min_down=0, inertia=0.0, start_up_cost=0.0,
            eff_at_rated=0.35, eff_at_min=0.25)
    state = SimpleNamespace(generators={"g": g})
    counts = Q.apply_realistic_generator_defaults(state)
    assert g.min_power == pytest.approx(0.40)
    assert g.ramp_up == pytest.approx(0.60)
    assert g.ramp_down == pytest.approx(0.60)
    assert g.min_up == 8 and g.min_down == 8
    assert g.inertia == pytest.approx(6.0)
    assert g.start_up_cost == pytest.approx(500 * 90.0)
    assert g.eff_at_rated == pytest.approx(0.38)
    assert g.eff_at_min == pytest.approx(0.30)
    assert counts["min_power"] == 1
    assert counts["start_up_cost"] == 1
    assert counts["eff_at_rated"] == 1


def test_apply_defaults_min_power_forced_full_load():
    # cur_min == 1.0 while tech can turn down (coal frac 0.40 < 1.0) → fixed
    g = Gen(fuel="coal", rated_power=500, min_power=1.0,
            ramp_up=0.6, ramp_down=0.6, min_up=8, min_down=8,
            inertia=6.0, start_up_cost=1.0,
            eff_at_rated=0.5, eff_at_min=0.4)
    state = SimpleNamespace(generators={"g": g})
    counts = Q.apply_realistic_generator_defaults(state)
    assert g.min_power == pytest.approx(0.40)
    assert counts["min_power"] == 1


def test_apply_defaults_no_change_when_plausible():
    # All values already plausible/non-degenerate → nothing touched.
    g = Gen(fuel="coal", rated_power=500, min_power=0.4,
            ramp_up=0.6, ramp_down=0.6, min_up=8, min_down=8,
            inertia=6.0, start_up_cost=999.0,
            eff_at_rated=0.50, eff_at_min=0.40)
    state = SimpleNamespace(generators={"g": g})
    counts = Q.apply_realistic_generator_defaults(state)
    assert all(v == 0 for v in counts.values())


def test_apply_defaults_force_resets_everything():
    g = Gen(fuel="coal", rated_power=500, min_power=0.4,
            ramp_up=0.6, ramp_down=0.6, min_up=8, min_down=8,
            inertia=6.0, start_up_cost=999.0,
            eff_at_rated=0.50, eff_at_min=0.40)
    state = SimpleNamespace(generators={"g": g})
    counts = Q.apply_realistic_generator_defaults(state, force=True)
    assert counts["min_power"] == 1
    assert counts["ramp_up"] == 1
    assert counts["inertia"] == 1
    assert g.start_up_cost == pytest.approx(500 * 90.0)
    assert g.eff_at_rated == pytest.approx(0.38)


def test_apply_defaults_vre_no_commitment_fix():
    # Sun: min_power_frac 0, ramps 1.0, min_up/down 0, inertia 0, su cost 0.
    # A solar gen with zero everything should NOT get commitment/inertia
    # fixes (targets are 0) but ramps will be set (target 1.0 > 0).
    g = Gen(fuel="Sun", rated_power=100, min_power=0.0,
            ramp_up=0.0, ramp_down=0.0, min_up=0, min_down=0,
            inertia=0.0, start_up_cost=0.0,
            eff_at_rated=0.35, eff_at_min=0.25)
    state = SimpleNamespace(generators={"g": g})
    counts = Q.apply_realistic_generator_defaults(state)
    assert counts["ramp_up"] == 1 and counts["ramp_down"] == 1
    assert counts["min_up"] == 0  # both targets 0 → not touched
    assert counts["inertia"] == 0
    assert counts["start_up_cost"] == 0
    # min_power: target 0 and cur 0 → not flagged
    assert counts["min_power"] == 0


# ── repair_fuel_consistency ──────────────────────────────────────────


def make_state(**kw):
    base = dict(
        generators={}, fuels={}, technologies={},
        fuel_entry_points=[], nodes=[], buses={},
        batteries={}, electrolyzers={}, transformers=[],
        transmission_lines=[],
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_repair_fuel_consistency_empty():
    state = make_state()
    assert Q.repair_fuel_consistency(state) == {
        "fuels_added": 0, "techs_added": 0, "fuel_entries_updated": 0}


def test_repair_fuel_consistency_creates_fuel_tech_supply():
    # A coal generator on a node with demand; nothing else present.
    from esfex.visualization.data.gui_model import GuiNode
    node = GuiNode(index=0, name="N0", centroid_lat=22.0, centroid_lng=-82.0)
    node.demand.peak_mw = 500.0
    g = Gen(fuel="coal", rated_power=300)
    state = make_state(generators={"g": g}, nodes=[node])
    counts = Q.repair_fuel_consistency(state)
    assert counts["fuels_added"] >= 1          # coal fuel created
    assert counts["techs_added"] >= 1          # coal tech created
    assert counts["fuel_entries_updated"] >= 1  # supply chain created
    # A fuel entry point was created on the demand node.
    assert len(state.fuel_entry_points) == 1
    assert state.fuel_entry_points[0].node == 0
    assert state.fuels  # catalog populated
    assert state.technologies


def test_repair_fuel_consistency_renewable_no_supply():
    # Wind generator → renewable → no fuel entry created.
    g = Gen(fuel="Wind", rated_power=100)
    state = make_state(generators={"g": g}, nodes=[])
    counts = Q.repair_fuel_consistency(state)
    # Wind fuel/tech may be added but no supply chain.
    assert counts["fuel_entries_updated"] == 0
    assert state.fuel_entry_points == []


def test_repair_fuel_consistency_skips_none_fuel():
    g = Gen(fuel="None", rated_power=100)
    state = make_state(generators={"g": g})
    counts = Q.repair_fuel_consistency(state)
    assert counts == {
        "fuels_added": 0, "techs_added": 0, "fuel_entries_updated": 0}


def test_repair_fuel_consistency_attaches_to_existing_entry():
    from esfex.visualization.data.gui_model import GuiFuelEntryPoint
    g = Gen(fuel="coal", rated_power=300)
    existing = GuiFuelEntryPoint(name="Port", fuels=[], node=0)
    state = make_state(generators={"g": g},
                       fuel_entry_points=[existing])
    counts = Q.repair_fuel_consistency(state)
    assert counts["fuel_entries_updated"] >= 1
    # attached to the existing entry, not a new one
    assert len(state.fuel_entry_points) == 1
    assert existing.fuels  # coal fuel id appended


def test_repair_fuel_consistency_generic_fuel_no_defaults():
    # A fuel that is not in _FUEL_DEFAULTS but is referenced: falls into
    # the generic-entry branch (fuel_id = raw.replace(" ", "_")).
    from esfex.visualization.workflows.grid_mapping_builder import _FUEL_DEFAULTS
    # Find a fuel string whose canonical key is NOT in _FUEL_DEFAULTS.
    g = Gen(fuel="Mystery Brew", rated_power=100)
    state = make_state(generators={"g": g})
    counts = Q.repair_fuel_consistency(state)
    assert counts["fuels_added"] == 1
    # generic id replaces spaces with underscores
    assert "Mystery_Brew" in state.fuels


def test_repair_fuel_consistency_existing_catalog_and_tech():
    # A fuel already in the catalog (with a name) AND a technology already
    # referencing it → no fuel/tech added, exercising the catalog-name and
    # tech-covered branches.
    from esfex.visualization.data.gui_model import GuiFuel, GuiTechnology
    fuel = GuiFuel(fuel_id="Coal", name="Coal")
    tech = GuiTechnology(tech_id="t0", name="Coal Plant", fuel="Coal")
    g = Gen(fuel="coal", rated_power=300)
    state = make_state(generators={"g": g},
                       fuels={"Coal": fuel},
                       technologies={"t0": tech},
                       nodes=[])
    counts = Q.repair_fuel_consistency(state)
    assert counts["fuels_added"] == 0   # already in catalog (by name)
    assert counts["techs_added"] == 0   # tech already covers coal


def test_repair_fuel_consistency_tech_id_collision():
    # Catalog has coal fuel but no tech; a technology already occupies the
    # default tech_id so the uniqueness loop must append a suffix.
    from esfex.visualization.data.gui_model import GuiFuel, GuiTechnology
    from esfex.visualization.workflows.grid_mapping_builder import (
        _TECH_DEFAULTS, _normalize_fuel_key,
    )
    tdef = _TECH_DEFAULTS["coal"]
    occupied_id = tdef["name"].replace(" ", "_")
    fuel = GuiFuel(fuel_id="Coal", name="Coal")
    # A pre-existing tech for a DIFFERENT fuel but occupying the id.
    clash = GuiTechnology(tech_id=occupied_id, name="X", fuel="something_else")
    g = Gen(fuel="coal", rated_power=300)
    state = make_state(generators={"g": g},
                       fuels={"Coal": fuel},
                       technologies={occupied_id: clash})
    counts = Q.repair_fuel_consistency(state)
    assert counts["techs_added"] == 1
    # A suffixed id must have been created.
    assert any(tid != occupied_id and tid.startswith(occupied_id)
               for tid in state.technologies)


def test_repair_fuel_consistency_already_supplied():
    # Coal fuel already supplied by an entry point → no supply update.
    from esfex.visualization.data.gui_model import GuiFuel, GuiFuelEntryPoint
    fuel = GuiFuel(fuel_id="Coal", name="Coal")
    entry = GuiFuelEntryPoint(name="Port", fuels=["Coal"], node=0)
    g = Gen(fuel="coal", rated_power=300)
    state = make_state(generators={"g": g},
                       fuels={"Coal": fuel},
                       fuel_entry_points=[entry])
    counts = Q.repair_fuel_consistency(state)
    assert counts["fuel_entries_updated"] == 0


# ── repair_bus_roles_and_demand ──────────────────────────────────────


def bus(bus_id, **kw):
    from esfex.visualization.data.gui_model import GuiBus
    return GuiBus(bus_id=bus_id, **kw)


def test_repair_bus_roles_empty():
    state = make_state()
    assert Q.repair_bus_roles_and_demand(state) == {
        "buses_role_changed": 0, "buses_demand_changed": 0,
        "nodes_redistributed": 0}


def test_repair_bus_roles_supply_makes_mixed():
    b = bus("bus_0", parent_node=0, voltage_kv=220.0, role="connection")
    g = Gen(fuel="coal", rated_power=100, bus="bus_0")
    # second load bus so redistribution path runs
    bl = bus("bus_1", parent_node=0, voltage_kv=33.0, role="connection")
    state = make_state(buses={"bus_0": b, "bus_1": bl},
                       generators={"g": g})
    counts = Q.repair_bus_roles_and_demand(state)
    assert b.role == "mixed"
    assert bl.role == "load"
    assert counts["buses_role_changed"] >= 2


def test_repair_bus_roles_lv_becomes_load_and_redistributes():
    # Two LV buses in one node, no demand fractions → equal split to 0.5.
    b0 = bus("bus_0", parent_node=0, voltage_kv=33.0, role="connection",
             demand_fraction=0.0)
    b1 = bus("bus_1", parent_node=0, voltage_kv=33.0, role="connection",
             demand_fraction=0.0)
    state = make_state(buses={"bus_0": b0, "bus_1": b1})
    counts = Q.repair_bus_roles_and_demand(state)
    assert b0.role == "load" and b1.role == "load"
    assert b0.demand_fraction == pytest.approx(0.5)
    assert b1.demand_fraction == pytest.approx(0.5)
    assert counts["nodes_redistributed"] == 1


def test_repair_bus_roles_preserves_valid_distribution():
    b0 = bus("bus_0", parent_node=0, voltage_kv=33.0, role="load",
             demand_fraction=0.7)
    b1 = bus("bus_1", parent_node=0, voltage_kv=33.0, role="load",
             demand_fraction=0.3)
    state = make_state(buses={"bus_0": b0, "bus_1": b1})
    counts = Q.repair_bus_roles_and_demand(state)
    # sum is exactly 1.0 → preserved, no redistribution
    assert b0.demand_fraction == pytest.approx(0.7)
    assert b1.demand_fraction == pytest.approx(0.3)
    assert counts["nodes_redistributed"] == 0


def test_repair_bus_roles_hv_connection_clears_stale_demand():
    # HV bus with stale demand and no load buses in node → fallback path.
    hv = bus("bus_0", parent_node=0, voltage_kv=220.0, role="connection",
             demand_fraction=0.0)
    # Single HV bus, no load → fallback promotes it to load with df 1.0
    state = make_state(buses={"bus_0": hv})
    counts = Q.repair_bus_roles_and_demand(state)
    assert hv.role == "load"
    assert hv.demand_fraction == pytest.approx(1.0)
    assert counts["nodes_redistributed"] == 1


def test_repair_bus_roles_transformer_to_bus_lv_load():
    from esfex.visualization.data.gui_model import GuiTransformer
    # to_bus is LV → load; from_bus HV with no supply → connection
    hv = bus("bus_hv", parent_node=0, voltage_kv=220.0, role="connection")
    lv = bus("bus_lv", parent_node=0, voltage_kv=33.0, role="connection")
    tr = GuiTransformer(name="T", from_bus="bus_hv", to_bus="bus_lv",
                        rated_power_mva=50.0)
    state = make_state(buses={"bus_hv": hv, "bus_lv": lv},
                       transformers=[tr])
    Q.repair_bus_roles_and_demand(state)
    assert lv.role == "load"
    assert hv.role == "connection"


def test_repair_bus_roles_mixed_fallback_when_no_load():
    # Node with only a mixed bus (has supply) and one connection HV bus.
    # No "load" bus → demand split falls back to mixed bus.
    mb = bus("bus_m", parent_node=0, voltage_kv=220.0, role="connection")
    hv = bus("bus_h", parent_node=0, voltage_kv=220.0, role="connection")
    g = Gen(fuel="coal", rated_power=100, bus="bus_m")
    state = make_state(buses={"bus_m": mb, "bus_h": hv},
                       generators={"g": g})
    Q.repair_bus_roles_and_demand(state)
    assert mb.role == "mixed"
    # demand falls back onto the mixed bus → df becomes 1.0
    assert mb.demand_fraction == pytest.approx(1.0)


def test_repair_bus_roles_clears_non_load_demand():
    # A load bus + a connection bus that wrongly carries demand.
    load = bus("bus_l", parent_node=0, voltage_kv=33.0, role="load",
               demand_fraction=1.0)
    stale = bus("bus_s", parent_node=0, voltage_kv=220.0, role="connection",
                demand_fraction=0.5)
    state = make_state(buses={"bus_l": load, "bus_s": stale})
    counts = Q.repair_bus_roles_and_demand(state)
    assert stale.demand_fraction == 0.0
    assert counts["buses_demand_changed"] >= 1


def test_repair_bus_roles_battery_electrolyzer_supply():
    # Battery and electrolyzer buses become mixed; plus a transmission line
    # whose endpoints are indexed.
    from esfex.visualization.data.gui_model import GuiTransmissionLine
    bb = bus("bus_b", parent_node=0, voltage_kv=33.0, role="connection")
    eb = bus("bus_e", parent_node=0, voltage_kv=33.0, role="connection")
    bat = SimpleNamespace(bus="bus_b")
    el = SimpleNamespace(bus="bus_e")
    ln = GuiTransmissionLine(line_id="L", from_bus="bus_b", to_bus="bus_e")
    state = make_state(buses={"bus_b": bb, "bus_e": eb},
                       batteries={"bt": bat}, electrolyzers={"el": el},
                       transmission_lines=[ln])
    Q.repair_bus_roles_and_demand(state)
    assert bb.role == "mixed" and eb.role == "mixed"


def test_repair_bus_roles_zero_voltage_become_load():
    # Voltage 0 < HV_THRESHOLD and not a transformer-from side → both
    # buses classified as "load" and demand is equal-split (0.5 each).
    b0 = bus("bus_0", parent_node=0, voltage_kv=0.0, role="connection",
             demand_fraction=0.3)
    b1 = bus("bus_1", parent_node=0, voltage_kv=0.0, role="connection",
             demand_fraction=0.0)
    state = make_state(buses={"bus_0": b0, "bus_1": b1})
    counts = Q.repair_bus_roles_and_demand(state)
    assert b0.role == "load" and b1.role == "load"
    assert b0.demand_fraction == pytest.approx(0.5)
    assert b1.demand_fraction == pytest.approx(0.5)
    assert counts["nodes_redistributed"] == 1


def test_repair_bus_roles_fallback_zero_voltage_xfm_from():
    # A single HV-from bus (transformer primary side) with voltage 0:
    # is_xfm_from is True so it stays "connection". With no load/mixed
    # bus in the node, the fallback path promotes a bus to load. Its
    # voltage is 0 so fallback uses bs[0].
    from esfex.visualization.data.gui_model import GuiTransformer
    b0 = bus("bus_0", parent_node=0, voltage_kv=0.0, role="connection",
             demand_fraction=0.0)
    tr = GuiTransformer(name="T", from_bus="bus_0", to_bus="bus_x")
    state = make_state(buses={"bus_0": b0}, transformers=[tr])
    counts = Q.repair_bus_roles_and_demand(state)
    assert b0.role == "load"
    assert b0.demand_fraction == pytest.approx(1.0)
    assert counts["nodes_redistributed"] == 1


# ── repair_node_internal_coupling ────────────────────────────────────


def test_repair_node_coupling_empty():
    state = make_state()
    assert Q.repair_node_internal_coupling(state) == {
        "transformers_added": 0, "lines_added": 0,
        "buses_coupled": 0, "nodes_restructured": 0}


def test_repair_node_coupling_fewer_than_two_significant():
    # Single significant bus → skipped.
    b = bus("bus_0", parent_node=0, voltage_kv=220.0, role="mixed",
            demand_fraction=1.0)
    g = Gen(fuel="coal", rated_power=100, bus="bus_0")
    state = make_state(buses={"bus_0": b}, generators={"g": g},
                       _next_line_id=0)
    counts = Q.repair_node_internal_coupling(state)
    assert counts["buses_coupled"] == 0


def test_repair_node_coupling_adds_line_same_voltage():
    # Two far-apart significant buses at the SAME voltage, not linked →
    # a short line is added.
    gb = bus("bus_g", parent_node=0, voltage_kv=220.0, role="mixed",
             demand_fraction=0.0, latitude=22.0, longitude=-82.0)
    lb = bus("bus_l", parent_node=0, voltage_kv=220.0, role="load",
             demand_fraction=1.0, latitude=23.0, longitude=-81.0)
    g = Gen(fuel="coal", rated_power=200, bus="bus_g")
    state = make_state(buses={"bus_g": gb, "bus_l": lb},
                       generators={"g": g}, _next_line_id=5)
    counts = Q.repair_node_internal_coupling(state)
    assert counts["lines_added"] == 1
    assert counts["buses_coupled"] == 1
    assert counts["nodes_restructured"] == 1
    assert state.transmission_lines
    assert state._next_line_id == 6


def test_repair_node_coupling_adds_transformer_diff_voltage():
    # Two significant buses at very different voltages → transformer added.
    gb = bus("bus_g", parent_node=0, voltage_kv=220.0, role="mixed",
             demand_fraction=0.0, latitude=22.0, longitude=-82.0)
    lb = bus("bus_l", parent_node=0, voltage_kv=33.0, role="load",
             demand_fraction=1.0, latitude=23.5, longitude=-81.0)
    g = Gen(fuel="coal", rated_power=400, bus="bus_g")
    state = make_state(buses={"bus_g": gb, "bus_l": lb},
                       generators={"g": g}, _next_line_id=0)
    counts = Q.repair_node_internal_coupling(state)
    assert counts["transformers_added"] == 1
    assert counts["buses_coupled"] == 1
    assert state.transformers


def test_repair_node_coupling_battery_bus_and_inter_endpoint():
    # Inter-node line establishes a backbone endpoint (hub preference) and a
    # battery-bearing bus counts as significant. The two significant buses
    # are unconnected → a line is added. Also exercises haversine via the
    # connector sizing path (same voltage).
    from esfex.visualization.data.gui_model import GuiTransmissionLine
    hub = bus("bus_hub", parent_node=0, voltage_kv=220.0, role="mixed",
              demand_fraction=0.5, latitude=22.0, longitude=-82.0)
    far = bus("bus_far", parent_node=0, voltage_kv=220.0, role="load",
              demand_fraction=0.5, latitude=24.0, longitude=-80.0)
    other = bus("bus_o", parent_node=1, voltage_kv=220.0, role="connection")
    bat = SimpleNamespace(bus="bus_hub")
    # inter-node line: hub <-> other (different nodes) → hub is endpoint
    inter = GuiTransmissionLine(line_id="I", from_bus="bus_hub",
                                to_bus="bus_o", from_node=0, to_node=1)
    state = make_state(
        buses={"bus_hub": hub, "bus_far": far, "bus_o": other},
        batteries={"bt": bat},
        transmission_lines=[inter], _next_line_id=0)
    counts = Q.repair_node_internal_coupling(state)
    assert counts["buses_coupled"] == 1
    assert counts["lines_added"] == 1


def test_repair_node_coupling_multihop_chain_close_skip():
    # Build a chain gen -- m1 -- m2 -- load via transformers/lines so that
    # the load is exactly 3 hops from gen but only 2 from the hub it picks.
    # This exercises _hops frontier expansion and the "already close" skip.
    from esfex.visualization.data.gui_model import (
        GuiTransmissionLine, GuiTransformer,
    )
    g_bus = bus("bus_g", parent_node=0, voltage_kv=220.0, role="mixed",
                demand_fraction=0.0)
    m1 = bus("bus_m1", parent_node=0, voltage_kv=220.0, role="connection")
    m2 = bus("bus_m2", parent_node=0, voltage_kv=220.0, role="connection")
    l_bus = bus("bus_l", parent_node=0, voltage_kv=220.0, role="load",
                demand_fraction=1.0)
    g = Gen(fuel="coal", rated_power=100, bus="bus_g")
    # gen -> m1 -> m2 -> load (3 hops gen..load); add a transformer link too
    lines = [
        GuiTransmissionLine(line_id="a", from_bus="bus_g", to_bus="bus_m1"),
        GuiTransmissionLine(line_id="b", from_bus="bus_m1", to_bus="bus_m2"),
    ]
    trs = [GuiTransformer(name="t", from_bus="bus_m2", to_bus="bus_l")]
    state = make_state(buses={"bus_g": g_bus, "bus_m1": m1,
                              "bus_m2": m2, "bus_l": l_bus},
                       generators={"g": g},
                       transmission_lines=lines, transformers=trs,
                       _next_line_id=0)
    counts = Q.repair_node_internal_coupling(state)
    # Both significant buses (g_bus, l_bus) resolve relative to the chosen
    # hub within MAX_HOPS through the chain, so at most a couple of new
    # connectors; the function must complete without error.
    assert isinstance(counts["buses_coupled"], int)


def test_repair_node_coupling_already_linked_skipped():
    from esfex.visualization.data.gui_model import GuiTransmissionLine
    # Two significant buses already directly connected by a line → no add.
    gb = bus("bus_g", parent_node=0, voltage_kv=220.0, role="mixed",
             demand_fraction=0.0)
    lb = bus("bus_l", parent_node=0, voltage_kv=220.0, role="load",
             demand_fraction=1.0)
    g = Gen(fuel="coal", rated_power=200, bus="bus_g")
    ln = GuiTransmissionLine(line_id="L0", from_bus="bus_g", to_bus="bus_l",
                             from_node=0, to_node=0)
    state = make_state(buses={"bus_g": gb, "bus_l": lb},
                       generators={"g": g},
                       transmission_lines=[ln], _next_line_id=0)
    counts = Q.repair_node_internal_coupling(state)
    # within MAX_HOPS (direct neighbour) → not coupled again
    assert counts["buses_coupled"] == 0
    assert counts["lines_added"] == 0
