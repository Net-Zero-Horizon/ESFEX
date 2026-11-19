"""Additive unit tests maximising coverage of ``grid_mapping_builder``.

Targets the internal branches of
:mod:`esfex.visualization.workflows.grid_mapping_builder`:
fuel/technology normalization and auto-creation, every ``_create_*`` phase
helper, the public ``build_grid_from_features`` orchestrator including its
guard clauses, error paths, snap-collapse self-loops, voltage defaults and
the per-circuit line composition.

PySide6 is stubbed only when a working Qt is genuinely unavailable, mirroring
``tests/test_auto_complete.py``.  A lightweight ``MockGuiModel`` mirrors the
GuiModel mutation methods the builder calls so no live QApplication is needed.
"""

from __future__ import annotations

import sys
from types import ModuleType

import pytest


class _ReqExc(Exception):
    """Stand-in for requests.RequestException (narrow, unlike bare Exception)."""

# ---------------------------------------------------------------------------
# Stub PySide6 + i18n only if a working Qt is absent.
# ---------------------------------------------------------------------------
try:
    import PySide6.QtWidgets  # noqa: F401
    _PYSIDE6_AVAILABLE = True
except Exception:
    _PYSIDE6_AVAILABLE = False

if not _PYSIDE6_AVAILABLE:
    _qtcore = ModuleType("PySide6.QtCore")
    _qtcore.QObject = type(  # type: ignore[attr-defined]
        "QObject", (), {"__init__": lambda self, *a, **kw: None},
    )
    _qtcore.Signal = lambda *a, **kw: property(lambda self: None)  # type: ignore[attr-defined]
    _qtcore.Qt = type(  # type: ignore[attr-defined]
        "Qt", (), {"AlignmentFlag": type("AF", (), {"AlignCenter": 0})},
    )()
    _qtcore.QThread = type(  # type: ignore[attr-defined]
        "QThread", (), {"__init__": lambda self, *a, **kw: None},
    )
    _pyside6 = ModuleType("PySide6")
    sys.modules.setdefault("PySide6", _pyside6)
    sys.modules.setdefault("PySide6.QtCore", _qtcore)
    _qtwidgets = ModuleType("PySide6.QtWidgets")
    for _w in [
        "QWidget", "QVBoxLayout", "QHBoxLayout", "QFormLayout",
        "QGroupBox", "QLabel", "QPushButton", "QSpinBox", "QDoubleSpinBox",
        "QCheckBox", "QComboBox", "QTextEdit", "QProgressBar", "QScrollArea",
        "QMessageBox", "QInputDialog", "QHeaderView", "QRadioButton",
        "QTableWidget", "QTableWidgetItem",
    ]:
        setattr(
            _qtwidgets, _w,
            type(_w, (), {"__init__": lambda self, *a, **kw: None}),
        )
    sys.modules.setdefault("PySide6.QtWidgets", _qtwidgets)
    _i18n = ModuleType("esfex.visualization.i18n")
    _i18n.tr = lambda key, **kw: key  # type: ignore[attr-defined]
    sys.modules.setdefault("esfex.visualization.i18n", _i18n)


from esfex.visualization.data.geo_asset_parser import ParseResult  # noqa: E402
from esfex.visualization.data.gui_model import (  # noqa: E402
    GeoPoint,
    GuiBus,
    GuiFuel,
    GuiFuelEntryPoint,
    GuiFuelStorage,
    GuiGeneratorInstance,
    GuiNode,
    GuiNodeDemand,
    GuiSystemState,
    GuiTechnology,
    GuiTransformer,
)
from esfex.visualization.workflows.grid_mapping_fetchers import GridFeature  # noqa: E402
from esfex.visualization.workflows import grid_mapping_builder as gmb  # noqa: E402


# ======================================================================
# Helpers
# ======================================================================


def _make_state(**overrides) -> GuiSystemState:
    nodes = overrides.pop("nodes", [
        GuiNode(index=0, name="North",
                demand=GuiNodeDemand(peak_mw=100.0, total_mwh=800.0)),
    ])
    buses = overrides.pop("buses", {
        "bus_hv": GuiBus(
            bus_id="bus_hv", name="HV Bus", parent_node=0,
            voltage_kv=110.0, latitude=21.0, longitude=-82.0,
        ),
    })
    return GuiSystemState(
        name="test_system",
        nodes=nodes,
        buses=buses,
        generators=overrides.pop("generators", {}),
        batteries=overrides.pop("batteries", {}),
        transmission_lines=overrides.pop("transmission_lines", []),
        transformers=overrides.pop("transformers", []),
        acdc_converters=overrides.pop("acdc_converters", []),
        **overrides,
    )


class MockGuiModel:
    """Mirror the GuiModel mutation methods the builder invokes."""

    def __init__(self, state: GuiSystemState):
        self.state = state

    def add_fuel(self, fuel_id: str, name: str, **kw) -> str:
        f = GuiFuel(fuel_id=fuel_id, name=name)
        for k, v in kw.items():
            if hasattr(f, k):
                setattr(f, k, v)
        self.state.fuels[fuel_id] = f
        return fuel_id

    def add_technology(self, name="T", category="Renewable",
                       tech_id=None, **kw) -> str:
        if tech_id is None:
            tech_id = f"tech_{self.state._next_tech_id}"
            self.state._next_tech_id += 1
        t = GuiTechnology(tech_id=tech_id, name=name, category=category)
        for k, v in kw.items():
            if hasattr(t, k):
                setattr(t, k, v)
        self.state.technologies[tech_id] = t
        return tech_id

    def update_technology(self, tech_id: str, **kw) -> None:
        t = self.state.technologies.get(tech_id)
        if t is None:
            return
        for k, v in kw.items():
            if hasattr(t, k):
                setattr(t, k, v)

    def add_fuel_entry(self, name, fuels=None, node=0,
                       lat=0.0, lng=0.0, **kw) -> int:
        self.state.fuel_entry_points.append(GuiFuelEntryPoint(
            name=name, fuels=fuels or [], node=node,
            coordinate=GeoPoint(lat, lng, name),
        ))
        return len(self.state.fuel_entry_points) - 1

    def add_fuel_storage(self, name, fuel="", node=0, **kw) -> str:
        sid = f"fuel_storage_{len(self.state.fuel_storages)}"
        self.state.fuel_storages[sid] = GuiFuelStorage(
            storage_id=sid, name=name,
            fuels=[fuel] if fuel else [], node=node,
        )
        return sid


def _feat(ftype, name="F", lat=21.0, lng=-82.0, **kw) -> GridFeature:
    return GridFeature(source="osm", feature_type=ftype, name=name,
                       latitude=lat, longitude=lng, **kw)


# ======================================================================
# _normalize_fuel_key
# ======================================================================


class TestNormalizeFuelKey:
    def test_alias_lookup(self):
        assert gmb._normalize_fuel_key("Solar") == "sun"
        assert gmb._normalize_fuel_key("PV") == "sun"
        assert gmb._normalize_fuel_key("photovoltaic") == "sun"

    def test_punctuation_and_spaces_stripped(self):
        assert gmb._normalize_fuel_key("natural-gas") == "naturalgas"
        assert gmb._normalize_fuel_key("natural_gas") == "naturalgas"
        assert gmb._normalize_fuel_key("Natural Gas") == "naturalgas"

    def test_oil_variants_split(self):
        assert gmb._normalize_fuel_key("diesel") == "diesel"
        assert gmb._normalize_fuel_key("HFO") == "fuel_oil"
        assert gmb._normalize_fuel_key("mazut") == "fuel_oil"

    def test_unknown_returns_normalized_input(self):
        # Not in alias table → returns the cleaned string itself.
        assert gmb._normalize_fuel_key("Unobtanium") == "unobtanium"


# ======================================================================
# _find_existing_fuel / _find_existing_technology
# ======================================================================


class TestFindExisting:
    def test_find_fuel_by_id(self):
        state = _make_state()
        state.fuels["Sun"] = GuiFuel(fuel_id="Sun", name="Sun")
        assert gmb._find_existing_fuel(state, "sun") == "Sun"

    def test_find_fuel_by_name(self):
        state = _make_state()
        state.fuels["X1"] = GuiFuel(fuel_id="X1", name="Natural Gas")
        assert gmb._find_existing_fuel(state, "naturalgas") == "X1"

    def test_find_fuel_none(self):
        state = _make_state()
        assert gmb._find_existing_fuel(state, "coal") is None

    def test_find_technology_exact_fuel(self):
        state = _make_state()
        state.technologies["t0"] = GuiTechnology(
            tech_id="t0", name="Solar", category="Renewable", fuel="Sun")
        assert gmb._find_existing_technology(state, "Sun", "sun") == "t0"

    def test_find_technology_by_normalized_fuel(self):
        state = _make_state()
        state.technologies["t0"] = GuiTechnology(
            tech_id="t0", name="GT", category="Non-renewable",
            fuel="Natural Gas")
        # fuel_id arg doesn't match exactly, but normalized fuel does.
        assert gmb._find_existing_technology(state, "Natural_gas",
                                             "naturalgas") == "t0"

    def test_find_technology_none(self):
        state = _make_state()
        state.technologies["t0"] = GuiTechnology(
            tech_id="t0", name="GT", category="Non-renewable", fuel="")
        assert gmb._find_existing_technology(state, "Coal", "coal") is None


# ======================================================================
# _create_fuels_and_technologies
# ======================================================================


class TestMinCapacityBuildFilter:
    def test_drops_below_min_and_unknown_capacity(self):
        # min_capacity is a firm contract on the built network: a known unit
        # below the floor AND an unknown-capacity unit (capacity_mw == 0, which
        # the fetch filter lets through) are both dropped; above-floor kept.
        model = MockGuiModel(_make_state(buses={}))
        feats = [
            _feat("generator", name="big", fuel="coal", capacity_mw=100.0),
            _feat("generator", name="small", fuel="coal", capacity_mw=0.4),
            _feat("generator", name="unknown", fuel="coal", capacity_mw=0.0),
        ]
        gmb.build_grid_from_features(model, feats, min_capacity_mw=1.0)
        names = {g.name for g in model.state.generators.values()}
        assert names == {"big"}

    def test_zero_min_includes_all(self):
        # Documented behaviour: min_capacity == 0 keeps every generator,
        # including unknown-capacity ones.
        model = MockGuiModel(_make_state(buses={}))
        feats = [
            _feat("generator", name="big", fuel="coal", capacity_mw=100.0),
            _feat("generator", name="unknown", fuel="coal", capacity_mw=0.0),
        ]
        gmb.build_grid_from_features(model, feats, min_capacity_mw=0.0)
        assert len(model.state.generators) == 2


class TestCreateFuelsAndTechnologies:
    def test_creates_default_fuel_and_tech(self):
        model = MockGuiModel(_make_state())
        result = ParseResult()
        gens = [_feat("generator", fuel="coal", capacity_mw=100)]
        fuel_remap, tech_remap = gmb._create_fuels_and_technologies(
            model, gens, result)
        assert fuel_remap["coal"] == "Coal"
        assert tech_remap[("Coal ST", "Coal")] is not None
        assert result.fuels_created == 1
        assert result.technologies_created == 1
        assert "Coal" in model.state.fuels

    def test_unfueled_generators_get_other_fuel_and_tech(self):
        # Generators with no detected fuel must still get a real fuel AND a
        # technology (routed through the catalog "Other" entry) — never a
        # dangling reference with technology_id=None.
        model = MockGuiModel(_make_state())
        result = ParseResult()
        gens = [
            _feat("generator", fuel="None"),
            _feat("generator", fuel=""),
        ]
        fuel_remap, tech_remap = gmb._create_fuels_and_technologies(
            model, gens, result)
        assert fuel_remap["Other"] == "Other"
        assert tech_remap[("Other", "Other")] is not None
        assert "Other" in model.state.fuels
        assert result.fuels_created == 1 and result.technologies_created == 1

    def test_none_fuel_routed_to_other(self):
        # A literal "none" fuel is treated as undetected and routed to "Other"
        # so the generator still gets a fuel + technology.
        model = MockGuiModel(_make_state())
        result = ParseResult()
        gens = [_feat("generator", fuel="none")]
        fuel_remap, tech_remap = gmb._create_fuels_and_technologies(
            model, gens, result)
        assert fuel_remap["Other"] == "Other"
        assert tech_remap[("Other", "Other")] is not None

    def test_reuses_existing_fuel(self):
        state = _make_state()
        state.fuels["Sun"] = GuiFuel(fuel_id="Sun", name="Sun")
        model = MockGuiModel(state)
        result = ParseResult()
        gens = [_feat("generator", fuel="solar")]
        fuel_remap, _ = gmb._create_fuels_and_technologies(
            model, gens, result)
        assert fuel_remap["solar"] == "Sun"
        assert result.fuels_created == 0  # reused, not created

    def test_generic_fuel_for_unknown_key(self):
        model = MockGuiModel(_make_state())
        result = ParseResult()
        gens = [_feat("generator", fuel="Magic Dust")]
        fuel_remap, tech_remap = gmb._create_fuels_and_technologies(
            model, gens, result)
        # Unknown key → generic fuel id with spaces replaced.
        assert fuel_remap["Magic Dust"] == "Magic_Dust"
        assert result.fuels_created == 1
        # Unknown canonical → a generic technology is created (never None).
        assert tech_remap[("Other", "Magic_Dust")] is not None
        assert result.technologies_created == 1

    def test_existing_tech_by_name_reused(self):
        state = _make_state()
        state.fuels["Coal"] = GuiFuel(fuel_id="Coal", name="Coal")
        # Existing tech with the catalog name but an inconsistent fuel id is
        # reused (matched by name) and its fuel is made consistent.
        state.technologies["t0"] = GuiTechnology(
            tech_id="t0", name="Coal Steam Turbine", category="Non-renewable",
            fuel="coal")
        model = MockGuiModel(state)
        result = ParseResult()
        gens = [_feat("generator", fuel="coal")]
        _, tech_remap = gmb._create_fuels_and_technologies(
            model, gens, result)
        assert tech_remap[("Coal ST", "Coal")] == "t0"
        assert state.technologies["t0"].fuel == "Coal"
        assert result.technologies_created == 0


# ======================================================================
# Standard-line-type capacity / impedance (PyPSA catalog methodology)
# ======================================================================


class TestStandardLineTypeCapacity:
    @pytest.mark.parametrize("kv,expected_mw", [
        # sqrt(3) * V * I_nom(nearest type) * 0.9  (per circuit, bundled EHV)
        (110, 171.4),
        (220, 686.0),
        (380, 2132.5),
        (500, 3351.5),
    ])
    def test_thermal_capacity(self, kv, expected_mw):
        from esfex.visualization.workflows.grid_mapping_quality import (
            estimate_line_capacity_mw,
        )
        assert estimate_line_capacity_mw(kv) == pytest.approx(expected_mw, rel=0.02)

    def test_capacity_increases_with_voltage(self):
        from esfex.visualization.workflows.grid_mapping_quality import (
            estimate_line_capacity_mw,
        )
        caps = [estimate_line_capacity_mw(v) for v in (66, 110, 220, 380, 500, 750)]
        assert caps == sorted(caps)  # strictly increasing with voltage

    def test_circuits_scale_capacity(self):
        from esfex.visualization.workflows.grid_mapping_quality import (
            estimate_line_capacity_mw,
        )
        one = estimate_line_capacity_mw(220, num_circuits=1)
        two = estimate_line_capacity_mw(220, num_circuits=2)
        assert two == pytest.approx(2 * one)

    def test_voltage_snaps_to_nearest_type(self):
        from esfex.visualization.workflows.grid_mapping_quality import (
            _nearest_std_line_type,
        )
        assert _nearest_std_line_type(400)[0] == 380.0   # 400 -> 380
        assert _nearest_std_line_type(230)[0] == 220.0   # 230 -> 220
        assert _nearest_std_line_type(345)[0] == 380.0   # 345 -> 380 (closer)

    def test_impedance_from_same_type(self):
        from esfex.visualization.workflows.grid_mapping_quality import (
            estimate_line_rxb_per_km,
        )
        r, x, b = estimate_line_rxb_per_km(380)
        assert r == pytest.approx(0.022) and x == pytest.approx(0.245)
        assert b > 0  # susceptance derived from the type capacitance


# ======================================================================
# _create_buses_from_substation
# ======================================================================


class TestCreateBusesFromSubstation:
    def test_per_voltage_creates_two_buses_and_transformer(self):
        state = _make_state(buses={})
        result = ParseResult()
        sub = _feat("substation", name="S", voltage_kv=220.0,
                    voltage_kv_secondary=110.0)
        bus_ids = gmb._create_buses_from_substation(
            state, sub, "per_voltage", 5.0, result, {}, None)
        assert len(bus_ids) == 2
        assert result.transformers_added == 1
        assert len(state.transformers) == 1

    def test_auto_transformer_impedance_on_system_base(self):
        """The substation transformer's impedance is referred to
        ac_power_flow.base_mva (800 MVA bank at 10 % own base: 0.05 p.u. on
        400 MVA, 0.0125 p.u. on the 100 MVA it used regardless)."""
        state = _make_state(buses={})
        state.ac_power_flow.base_mva = 400.0
        sub = _feat("substation", name="S", voltage_kv=220.0,
                    voltage_kv_secondary=110.0)
        gmb._create_buses_from_substation(
            state, sub, "per_voltage", 5.0, ParseResult(), {}, None)
        tr = state.transformers[0]
        assert tr.rated_power_mva == 800.0
        assert tr.impedance_pu == pytest.approx(0.05)

    def test_per_voltage_snap_collapse_no_transformer(self):
        # Both voltage levels snap to a single existing bus (huge snap_km
        # and an existing close bus) → no self-transformer.
        state = _make_state(buses={
            "bus0": GuiBus(bus_id="bus0", parent_node=0, voltage_kv=220.0,
                           latitude=21.0, longitude=-82.0),
        })
        result = ParseResult()
        sub = _feat("substation", name="S", voltage_kv=220.0,
                    voltage_kv_secondary=220.0)
        bus_ids = gmb._create_buses_from_substation(
            state, sub, "per_voltage", 1000.0, result, {}, force_node=0)
        assert bus_ids[0] == bus_ids[1]
        assert result.transformers_added == 0

    def test_per_substation_single_bus(self):
        state = _make_state(buses={})
        result = ParseResult()
        sub = _feat("substation", name="S", voltage_kv=220.0)
        bus_ids = gmb._create_buses_from_substation(
            state, sub, "per_substation", 5.0, result, {}, None)
        assert len(bus_ids) == 1
        assert result.transformers_added == 0

    def test_single_bus_unknown_voltage_when_missing(self):
        # An untagged substation must be left at 0 (unknown) so that
        # _propagate_voltage can later infer its real level from neighbours.
        # Defaulting to 220 kV floods the model with a voltage the grid does
        # not use and corrupts the derived capacity/impedance.
        state = _make_state(buses={})
        result = ParseResult()
        sub = _feat("substation", name="", voltage_kv=0.0)
        bus_ids = gmb._create_buses_from_substation(
            state, sub, "per_substation", 5.0, result, {}, None)
        bus = state.buses[bus_ids[0]]
        assert bus.voltage_kv == 0.0


# ======================================================================
# _ensure_generator_stepup
# ======================================================================


class TestEnsureGeneratorStepup:
    def test_missing_gen_bus_returns(self):
        state = _make_state(buses={})
        result = ParseResult()
        gen = _feat("generator", name="G")
        gmb._ensure_generator_stepup(state, gen, 0, "nonexistent", result)
        assert result.transformers_added == 0

    def test_no_higher_voltage_bus_no_transformer(self):
        state = _make_state(buses={
            "bus_lo": GuiBus(bus_id="bus_lo", parent_node=0, voltage_kv=34.5,
                             latitude=21.0, longitude=-82.0),
        })
        result = ParseResult()
        gen = _feat("generator", name="G")
        gmb._ensure_generator_stepup(state, gen, 0, "bus_lo", result)
        assert result.transformers_added == 0

    def test_lower_voltage_sibling_bus_skipped(self):
        # A sibling bus at or below the generator voltage is not a step-up
        # target → exercises the ``bv <= gen_v`` continue.
        state = _make_state(buses={
            "bus_gen": GuiBus(bus_id="bus_gen", parent_node=0,
                              voltage_kv=110.0,
                              latitude=21.0, longitude=-82.0),
            "bus_low": GuiBus(bus_id="bus_low", parent_node=0,
                              voltage_kv=34.5,
                              latitude=21.01, longitude=-82.0),
        })
        result = ParseResult()
        gen = _feat("generator", name="G")
        gmb._ensure_generator_stepup(state, gen, 0, "bus_gen", result)
        # Only a lower-voltage sibling exists → no GSU created.
        assert result.transformers_added == 0

    def test_creates_gsu_to_hv_bus(self):
        state = _make_state(buses={
            "bus_lo": GuiBus(bus_id="bus_lo", parent_node=0, voltage_kv=34.5,
                             latitude=21.0, longitude=-82.0),
            "bus_hi": GuiBus(bus_id="bus_hi", parent_node=0, voltage_kv=220.0,
                             latitude=21.01, longitude=-82.0),
        })
        result = ParseResult()
        gen = _feat("generator", name="G", capacity_mw=50.0)
        gmb._ensure_generator_stepup(state, gen, 0, "bus_lo", result)
        assert result.transformers_added == 1
        tr = state.transformers[-1]
        assert tr.from_bus == "bus_hi"
        assert tr.to_bus == "bus_lo"

    def test_skips_when_transformer_already_bridges(self):
        state = _make_state(buses={
            "bus_lo": GuiBus(bus_id="bus_lo", parent_node=0, voltage_kv=34.5,
                             latitude=21.0, longitude=-82.0),
            "bus_hi": GuiBus(bus_id="bus_hi", parent_node=0, voltage_kv=220.0,
                             latitude=21.01, longitude=-82.0),
        }, transformers=[
            GuiTransformer(name="existing", from_bus="bus_lo",
                           to_bus="bus_hi"),
        ])
        result = ParseResult()
        gen = _feat("generator", name="G")
        gmb._ensure_generator_stepup(state, gen, 0, "bus_lo", result)
        assert result.transformers_added == 0

    def test_tiebreak_prefers_nearer_equal_voltage_bus(self):
        # Two HV buses at the same (higher) voltage: the farther one is
        # inserted first, the nearer one second → exercises the
        # ``bv == hv_v and d < hv_d`` proximity tie-break.
        state = _make_state(buses={
            "bus_lo": GuiBus(bus_id="bus_lo", parent_node=0, voltage_kv=34.5,
                             latitude=21.0, longitude=-82.0),
            "bus_far": GuiBus(bus_id="bus_far", parent_node=0,
                              voltage_kv=220.0,
                              latitude=22.0, longitude=-83.0),
            "bus_near": GuiBus(bus_id="bus_near", parent_node=0,
                               voltage_kv=220.0,
                               latitude=21.001, longitude=-82.001),
        })
        result = ParseResult()
        gen = _feat("generator", name="G", capacity_mw=50.0)
        gmb._ensure_generator_stepup(state, gen, 0, "bus_lo", result)
        assert result.transformers_added == 1
        tr = state.transformers[-1]
        assert tr.from_bus == "bus_near"

    def test_zero_gen_voltage_uses_default_lo(self):
        # gen bus has voltage 0 → v_lo defaults to 34.5 in the GSU.
        state = _make_state(buses={
            "bus_lo": GuiBus(bus_id="bus_lo", parent_node=0, voltage_kv=0.0,
                             latitude=21.0, longitude=-82.0),
            "bus_hi": GuiBus(bus_id="bus_hi", parent_node=0, voltage_kv=220.0,
                             latitude=21.01, longitude=-82.0),
        })
        result = ParseResult()
        gen = _feat("generator", name="G")
        gmb._ensure_generator_stepup(state, gen, 0, "bus_lo", result)
        tr = state.transformers[-1]
        assert tr.to_voltage_kv == 34.5


# ======================================================================
# _create_generator
# ======================================================================


class TestCreateGenerator:
    def test_basic_generator(self):
        state = _make_state()
        result = ParseResult()
        gen = _feat("generator", name="G1", capacity_mw=50.0, fuel="solar")
        gmb._create_generator(state, gen, 5.0, result, {}, None)
        assert result.generators_added == 1
        inst = next(iter(state.generators.values()))
        assert inst.rated_power == 50.0

    def test_fuel_remap_applied(self):
        state = _make_state()
        result = ParseResult()
        gen = _feat("generator", name="G1", capacity_mw=10.0, fuel="solar")
        gmb._create_generator(
            state, gen, 5.0, result, {}, None,
            fuel_remap={"solar": "Sun"}, tech_remap={("PV", "Sun"): "tech_0"})
        inst = next(iter(state.generators.values()))
        assert inst.fuel == "Sun"
        assert inst.technology_id == "tech_0"

    def test_empty_fuel_falls_back_to_other(self):
        state = _make_state()
        result = ParseResult()
        gen = _feat("generator", name="G1", capacity_mw=10.0, fuel="")
        gmb._create_generator(state, gen, 5.0, result, {}, None)
        inst = next(iter(state.generators.values()))
        assert inst.fuel == "Other"

    def test_initial_age_from_commissioning_year(self):
        state = _make_state()
        result = ParseResult()
        gen = _feat("generator", name="G1", capacity_mw=10.0, fuel="coal",
                    commissioning_year=2000)
        gmb._create_generator(state, gen, 5.0, result, {}, None)
        inst = next(iter(state.generators.values()))
        assert inst.initial_age > 0

    def test_no_commissioning_year_zero_age(self):
        state = _make_state()
        result = ParseResult()
        gen = _feat("generator", name="G1", capacity_mw=10.0, fuel="coal")
        gmb._create_generator(state, gen, 5.0, result, {}, None)
        inst = next(iter(state.generators.values()))
        assert inst.initial_age == 0

    def test_gen_type_default_non_renewable(self):
        state = _make_state()
        result = ParseResult()
        gen = _feat("generator", name="G1", capacity_mw=10.0, fuel="coal",
                    gen_type="")
        gmb._create_generator(state, gen, 5.0, result, {}, None)
        inst = next(iter(state.generators.values()))
        assert inst.gen_type == "Non-renewable"


# ======================================================================
# _create_battery
# ======================================================================


class TestCreateBattery:
    def test_explicit_energy(self):
        state = _make_state()
        result = ParseResult()
        bat = _feat("battery", name="B", capacity_mw=10.0, energy_mwh=80.0)
        gmb._create_battery(state, bat, 5.0, result, {}, None)
        inst = next(iter(state.batteries.values()))
        assert inst.capacity == 80.0
        assert result.batteries_added == 1

    def test_default_4h_duration(self):
        state = _make_state()
        result = ParseResult()
        bat = _feat("battery", name="B", capacity_mw=10.0, energy_mwh=0.0)
        gmb._create_battery(state, bat, 5.0, result, {}, None)
        inst = next(iter(state.batteries.values()))
        assert inst.capacity == 40.0


# ======================================================================
# _create_line
# ======================================================================


class TestCreateLine:
    def test_no_geometry_skipped(self):
        state = _make_state()
        result = ParseResult()
        line = _feat("line", name="L", line_coords=[])
        gmb._create_line(state, line, 5.0, result, {}, None)
        assert result.lines_added == 0
        assert any("no geometry" in w for w in result.warnings)

    def test_single_point_skipped(self):
        state = _make_state()
        result = ParseResult()
        line = _feat("line", name="L", line_coords=[(21.0, -82.0)])
        gmb._create_line(state, line, 5.0, result, {}, None)
        assert result.lines_added == 0

    def test_endpoints_same_bus_skipped(self):
        # Both endpoints snap to the same existing bus.
        state = _make_state(buses={
            "bus0": GuiBus(bus_id="bus0", parent_node=0, voltage_kv=110.0,
                           latitude=21.0, longitude=-82.0),
        })
        result = ParseResult()
        line = _feat("line", name="L",
                     line_coords=[(21.0, -82.0), (21.0001, -82.0001)])
        gmb._create_line(state, line, 1000.0, result, {}, force_node=0)
        assert result.lines_added == 0
        assert any("same bus" in w for w in result.warnings)

    def test_creates_line_with_waypoints_and_impedance(self):
        state = _make_state(buses={})
        result = ParseResult()
        line = _feat("line", name="L", voltage_kv=220.0, num_circuits=2,
                     line_coords=[(21.0, -82.0), (21.3, -82.3), (21.6, -82.6)])
        gmb._create_line(state, line, 5.0, result, {}, None)
        assert result.lines_added == 1
        gl = state.transmission_lines[-1]
        assert len(gl.waypoints) == 1
        assert gl.length_km is not None and gl.length_km > 0
        assert gl.resistance_pu is not None
        assert gl.num_circuits == 2

    def test_no_voltage_leaves_impedance_none(self):
        state = _make_state(buses={})
        result = ParseResult()
        line = _feat("line", name="L", voltage_kv=0.0,
                     line_coords=[(21.0, -82.0), (22.0, -83.0)])
        gmb._create_line(state, line, 5.0, result, {}, None)
        gl = state.transmission_lines[-1]
        assert gl.voltage_kv is None
        # The length is measured anyway, so the impedance can be derived
        # once _propagate_voltage infers the voltage.
        assert gl.length_km == pytest.approx(151.9, abs=0.5)
        assert gl.resistance_pu is None and gl.reactance_pu is None

    def test_untagged_line_gets_impedance_from_inferred_voltage(self):
        """Regression: an OSM line without a voltage tag kept no length, so
        _propagate_voltage gave it a voltage but no impedance, and the
        node-level reduction then wrote 0.01 p.u. for it whatever its
        length."""
        from esfex.visualization.workflows.grid_mapping_quality import (
            estimate_line_pu_params,
        )
        state = _make_state(buses={})
        result = ParseResult()
        line = _feat("line", name="L", voltage_kv=0.0, num_circuits=2,
                     line_coords=[(21.0, -82.0), (21.5, -82.5)])
        gmb._create_line(state, line, 5.0, result, {}, None)
        gl = state.transmission_lines[-1]
        assert gl.reactance_pu is None
        # One end turns out to be a 110 kV substation (tagged neighbour)
        state.buses[gl.from_bus].voltage_kv = 110.0

        gmb._propagate_voltage(state)
        assert gl.voltage_kv == 110.0
        r, x, b = estimate_line_pu_params(110.0, gl.length_km)
        # Two circuits in parallel, as for a tagged line
        assert gl.reactance_pu == pytest.approx(x / 2)
        assert gl.resistance_pu == pytest.approx(r / 2)
        assert gl.susceptance_pu == pytest.approx(b * 2)

    def test_impedance_on_system_base(self):
        """Line impedances are on ac_power_flow.base_mva, not a fixed
        100 MVA (tagged lines and voltages inferred later alike)."""
        from esfex.visualization.workflows.grid_mapping_quality import (
            estimate_line_pu_params,
        )
        state = _make_state(buses={})
        state.ac_power_flow.base_mva = 250.0
        result = ParseResult()
        tagged = _feat("line", name="T", voltage_kv=220.0,
                       line_coords=[(21.0, -82.0), (21.5, -82.5)])
        gmb._create_line(state, tagged, 5.0, result, {}, None)
        gl = state.transmission_lines[-1]
        r, x, b = estimate_line_pu_params(220.0, gl.length_km, 250.0)
        assert gl.reactance_pu == pytest.approx(x)
        assert gl.resistance_pu == pytest.approx(r)
        assert gl.susceptance_pu == pytest.approx(b)

        untagged = _feat("line", name="U", voltage_kv=0.0,
                         line_coords=[(23.0, -80.0), (23.5, -80.5)])
        gmb._create_line(state, untagged, 5.0, result, {}, None)
        ul = state.transmission_lines[-1]
        state.buses[ul.from_bus].voltage_kv = 110.0
        gmb._propagate_voltage(state)
        r, x, b = estimate_line_pu_params(110.0, ul.length_km, 250.0)
        assert ul.reactance_pu == pytest.approx(x)

    def test_explicit_capacity_scaled_by_circuits(self):
        state = _make_state(buses={})
        result = ParseResult()
        line = _feat("line", name="L", voltage_kv=220.0, capacity_mw=300.0,
                     num_circuits=3,
                     line_coords=[(21.0, -82.0), (22.0, -83.0)])
        gmb._create_line(state, line, 5.0, result, {}, None)
        gl = state.transmission_lines[-1]
        assert gl.capacity_mw == 900.0

    def test_estimated_capacity_when_unspecified(self):
        state = _make_state(buses={})
        result = ParseResult()
        line = _feat("line", name="L", voltage_kv=500.0, capacity_mw=0.0,
                     num_circuits=1,
                     line_coords=[(21.0, -82.0), (22.0, -83.0)])
        gmb._create_line(state, line, 5.0, result, {}, None)
        gl = state.transmission_lines[-1]
        # Thermal rating of the nearest (500 kV) standard type:
        # sqrt(3) * 500 * 4.3 kA * 0.9 ~= 3352 MW.
        assert gl.capacity_mw == pytest.approx(3351.5, rel=0.01)


# ======================================================================
# _create_transformer
# ======================================================================


class TestCreateTransformer:
    def test_creates_transformer(self):
        state = _make_state(buses={})
        result = ParseResult()
        tr = _feat("transformer", name="TR", voltage_kv=220.0,
                   voltage_kv_secondary=110.0, capacity_mw=200.0)
        gmb._create_transformer(state, tr, 5.0, result, {}, None)
        assert result.transformers_added == 1
        gt = state.transformers[-1]
        assert gt.from_voltage_kv == 220.0
        assert gt.to_voltage_kv == 110.0
        assert gt.rated_power_mva == 200.0

    def test_untagged_side_is_not_invented(self):
        # An untagged secondary is not given a fixed 110 kV (above a 34.5 kV
        # primary, once): its side snaps voltage-agnostically to the primary's
        # bus and the transformer is skipped, with the reason.
        state = _make_state(buses={})
        result = ParseResult()
        tr = _feat("transformer", name="TR", voltage_kv=34.5,
                   voltage_kv_secondary=0.0, capacity_mw=0.0)
        gmb._create_transformer(state, tr, 5.0, result, {}, None)
        assert state.transformers == []
        assert {b.voltage_kv for b in state.buses.values()} == {34.5}
        assert any("untagged" in w for w in result.warnings)

    def test_default_capacity(self):
        state = _make_state(buses={})
        result = ParseResult()
        tr = _feat("transformer", name="TR", voltage_kv=110.0,
                   voltage_kv_secondary=34.5, capacity_mw=0.0)
        gmb._create_transformer(state, tr, 5.0, result, {}, None)
        assert state.transformers[-1].rated_power_mva == 100.0

    def test_self_loop_skipped(self):
        # HV and LV sides snap to the same existing bus.
        state = _make_state(buses={
            "bus0": GuiBus(bus_id="bus0", parent_node=0, voltage_kv=220.0,
                           latitude=21.0, longitude=-82.0),
        })
        result = ParseResult()
        tr = _feat("transformer", name="TR", voltage_kv=220.0,
                   voltage_kv_secondary=220.0)
        gmb._create_transformer(state, tr, 1000.0, result, {}, force_node=0)
        assert result.transformers_added == 0
        assert any("self-loop" in w for w in result.warnings)


# ======================================================================
# _create_converter
# ======================================================================


class TestCreateConverter:
    def test_creates_converter(self):
        state = _make_state(buses={})
        result = ParseResult()
        conv = _feat("converter", name="C", voltage_kv=400.0,
                     capacity_mw=500.0)
        # snap_km small enough that the ~33 m DC offset does not collapse
        # onto the AC bus.
        gmb._create_converter(state, conv, 0.01, result, {}, None)
        assert result.acdc_converters_added == 1
        gc = state.acdc_converters[-1]
        assert gc.rated_power_mva == 500.0

    def test_default_voltage_and_capacity(self):
        state = _make_state(buses={})
        result = ParseResult()
        conv = _feat("converter", name="C", voltage_kv=0.0, capacity_mw=0.0)
        gmb._create_converter(state, conv, 0.01, result, {}, None)
        gc = state.acdc_converters[-1]
        assert gc.from_voltage_kv == 0.0      # unknown until propagation
        assert gc.rated_power_mva == 100.0

    def test_self_loop_skipped(self):
        state = _make_state(buses={
            "bus0": GuiBus(bus_id="bus0", parent_node=0, voltage_kv=220.0,
                           current_type="AC",
                           latitude=21.0, longitude=-82.0),
        })
        result = ParseResult()
        conv = _feat("converter", name="C", voltage_kv=220.0)
        gmb._create_converter(state, conv, 1000.0, result, {}, force_node=0)
        assert result.acdc_converters_added == 0
        assert any("self-loop" in w for w in result.warnings)


# ======================================================================
# _create_fuel_entry / _create_fuel_storage
# ======================================================================


class TestCreateFuelEntry:
    def test_maps_known_fuel_to_default_id(self):
        model = MockGuiModel(_make_state())
        result = ParseResult()
        fe = _feat("fuel_entry", name="FE", fuel="gas")
        gmb._create_fuel_entry(model, fe, 5.0, result, {}, None)
        assert result.fuel_entries_added == 1
        entry = model.state.fuel_entry_points[-1]
        assert entry.fuels == ["Natural_gas"]

    def test_existing_fuel_preferred(self):
        state = _make_state()
        state.fuels["MyGas"] = GuiFuel(fuel_id="MyGas", name="Natural Gas")
        model = MockGuiModel(state)
        result = ParseResult()
        fe = _feat("fuel_entry", name="FE", fuel="gas")
        gmb._create_fuel_entry(model, fe, 5.0, result, {}, None)
        entry = model.state.fuel_entry_points[-1]
        assert entry.fuels == ["MyGas"]

    def test_empty_fuel_gives_no_fuels(self):
        model = MockGuiModel(_make_state())
        result = ParseResult()
        fe = _feat("fuel_entry", name="FE", fuel="")
        gmb._create_fuel_entry(model, fe, 5.0, result, {}, None)
        entry = model.state.fuel_entry_points[-1]
        assert entry.fuels == []

    def test_force_node_used(self):
        model = MockGuiModel(_make_state())
        result = ParseResult()
        fe = _feat("fuel_entry", name="FE", fuel="coal")
        gmb._create_fuel_entry(model, fe, 5.0, result, {}, force_node=0)
        entry = model.state.fuel_entry_points[-1]
        assert entry.node == 0

    def test_unknown_fuel_kept_raw(self):
        model = MockGuiModel(_make_state())
        result = ParseResult()
        fe = _feat("fuel_entry", name="FE", fuel="Unobtanium")
        gmb._create_fuel_entry(model, fe, 5.0, result, {}, None)
        entry = model.state.fuel_entry_points[-1]
        # Unknown canonical, no existing, not in defaults → raw kept.
        assert entry.fuels == ["Unobtanium"]


class TestCreateFuelStorage:
    def test_maps_known_fuel(self):
        model = MockGuiModel(_make_state())
        result = ParseResult()
        fs = _feat("fuel_storage", name="FS", fuel="diesel")
        gmb._create_fuel_storage(model, fs, 5.0, result, {}, None)
        assert result.fuel_storages_added == 1
        store = list(model.state.fuel_storages.values())[-1]
        assert store.fuels == ["Diesel"]

    def test_existing_fuel_preferred(self):
        state = _make_state()
        state.fuels["D1"] = GuiFuel(fuel_id="D1", name="Diesel")
        model = MockGuiModel(state)
        result = ParseResult()
        fs = _feat("fuel_storage", name="FS", fuel="diesel")
        gmb._create_fuel_storage(model, fs, 5.0, result, {}, None)
        store = list(model.state.fuel_storages.values())[-1]
        assert store.fuels == ["D1"]

    def test_empty_fuel(self):
        model = MockGuiModel(_make_state())
        result = ParseResult()
        fs = _feat("fuel_storage", name="FS", fuel="")
        gmb._create_fuel_storage(model, fs, 5.0, result, {}, force_node=0)
        store = list(model.state.fuel_storages.values())[-1]
        assert store.fuels == []
        assert store.node == 0


# ======================================================================
# build_grid_from_features (orchestrator)
# ======================================================================


class TestBuildGridFromFeatures:
    def test_empty_features(self):
        model = MockGuiModel(_make_state())
        result = gmb.build_grid_from_features(model, [])
        assert isinstance(result, ParseResult)
        assert result.generators_added == 0

    def test_include_filter(self):
        model = MockGuiModel(_make_state(buses={}))
        feats = [
            _feat("substation", name="S1", voltage_kv=220.0, include=False),
            _feat("generator", name="G1", capacity_mw=10.0, fuel="solar",
                  include=True),
        ]
        result = gmb.build_grid_from_features(model, feats)
        # Excluded substation not processed; generator processed.
        assert result.generators_added == 1

    def test_full_pipeline_all_types(self):
        model = MockGuiModel(_make_state(buses={}))
        feats = [
            _feat("substation", name="S1", voltage_kv=220.0,
                  voltage_kv_secondary=110.0),
            _feat("generator", name="G1", capacity_mw=50.0, fuel="solar"),
            _feat("battery", name="B1", capacity_mw=5.0),
            _feat("line", name="L1", voltage_kv=220.0,
                  line_coords=[(21.0, -82.0), (21.5, -82.5)]),
            _feat("transformer", name="TR1", voltage_kv=220.0,
                  voltage_kv_secondary=110.0),
            _feat("converter", name="C1", voltage_kv=220.0),
            _feat("fuel_entry", name="FE1", fuel="gas"),
            _feat("fuel_storage", name="FS1", fuel="diesel"),
        ]
        result = gmb.build_grid_from_features(model, feats)
        assert result.generators_added == 1
        assert result.batteries_added == 1
        assert result.lines_added == 1
        assert result.fuel_entries_added == 1
        assert result.fuel_storages_added == 1
        assert result.fuels_created >= 1

    def test_substation_exception_captured_as_warning(self, monkeypatch):
        model = MockGuiModel(_make_state(buses={}))

        def _boom(*a, **kw):
            raise RuntimeError("boom-sub")

        monkeypatch.setattr(gmb, "_create_buses_from_substation", _boom)
        feats = [_feat("substation", name="BadSub", voltage_kv=220.0)]
        result = gmb.build_grid_from_features(model, feats)
        assert any("BadSub" in w and "boom-sub" in w for w in result.warnings)

    def test_generator_exception_captured_as_warning(self, monkeypatch):
        model = MockGuiModel(_make_state(buses={}))

        def _boom(*a, **kw):
            raise RuntimeError("boom-gen")

        monkeypatch.setattr(gmb, "_create_generator", _boom)
        feats = [_feat("generator", name="BadGen", fuel="coal")]
        result = gmb.build_grid_from_features(model, feats)
        assert any("BadGen" in w for w in result.warnings)

    def test_battery_exception_captured(self, monkeypatch):
        model = MockGuiModel(_make_state(buses={}))
        monkeypatch.setattr(
            gmb, "_create_battery",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom-bat")))
        feats = [_feat("battery", name="BadBat", capacity_mw=5.0)]
        result = gmb.build_grid_from_features(model, feats)
        assert any("BadBat" in w for w in result.warnings)

    def test_line_exception_captured(self, monkeypatch):
        model = MockGuiModel(_make_state(buses={}))
        monkeypatch.setattr(
            gmb, "_create_line",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom-line")))
        feats = [_feat("line", name="BadLine",
                       line_coords=[(21.0, -82.0), (22.0, -83.0)])]
        result = gmb.build_grid_from_features(model, feats)
        assert any("BadLine" in w for w in result.warnings)

    def test_transformer_exception_captured(self, monkeypatch):
        model = MockGuiModel(_make_state(buses={}))
        monkeypatch.setattr(
            gmb, "_create_transformer",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom-tr")))
        feats = [_feat("transformer", name="BadTR", voltage_kv=220.0)]
        result = gmb.build_grid_from_features(model, feats)
        assert any("BadTR" in w for w in result.warnings)

    def test_converter_exception_captured(self, monkeypatch):
        model = MockGuiModel(_make_state(buses={}))
        monkeypatch.setattr(
            gmb, "_create_converter",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom-cv")))
        feats = [_feat("converter", name="BadCV", voltage_kv=220.0)]
        result = gmb.build_grid_from_features(model, feats)
        assert any("BadCV" in w for w in result.warnings)

    def test_fuel_entry_exception_captured(self, monkeypatch):
        model = MockGuiModel(_make_state(buses={}))
        monkeypatch.setattr(
            gmb, "_create_fuel_entry",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom-fe")))
        feats = [_feat("fuel_entry", name="BadFE", fuel="gas")]
        result = gmb.build_grid_from_features(model, feats)
        assert any("BadFE" in w for w in result.warnings)

    def test_fuel_storage_exception_captured(self, monkeypatch):
        model = MockGuiModel(_make_state(buses={}))
        monkeypatch.setattr(
            gmb, "_create_fuel_storage",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom-fs")))
        feats = [_feat("fuel_storage", name="BadFS", fuel="coal")]
        result = gmb.build_grid_from_features(model, feats)
        assert any("BadFS" in w for w in result.warnings)

    def test_phase9_repair_exception_captured(self, monkeypatch):
        # Force the consistency-repair import block to raise so the outer
        # try/except warning path runs.
        import esfex.visualization.workflows.grid_mapping_quality as q
        monkeypatch.setattr(
            q, "repair_fuel_consistency",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom-rep")))
        model = MockGuiModel(_make_state(buses={}))
        feats = [_feat("generator", name="G1", fuel="coal", capacity_mw=10)]
        result = gmb.build_grid_from_features(model, feats)
        assert any("consistency repair" in w for w in result.warnings)

    def test_substation_with_osm_id_tracked(self):
        # Non-empty osm_id exercises the substation_buses mapping branch.
        model = MockGuiModel(_make_state(buses={}))
        feats = [_feat("substation", name="S1", voltage_kv=220.0,
                       osm_id="way/123")]
        result = gmb.build_grid_from_features(model, feats)
        assert result.buses_added >= 1

    def test_substation_without_osm_id_not_tracked(self):
        # osm_id empty → skip the substation_buses mapping branch.
        model = MockGuiModel(_make_state(buses={}))
        feats = [_feat("substation", name="S1", voltage_kv=220.0, osm_id="")]
        result = gmb.build_grid_from_features(model, feats)
        assert result.buses_added >= 1

    def test_target_node_forces_placement(self):
        nodes = [
            GuiNode(index=0, name="N0",
                    demand=GuiNodeDemand(peak_mw=100.0, total_mwh=800.0)),
            GuiNode(index=1, name="N1",
                    demand=GuiNodeDemand(peak_mw=100.0, total_mwh=800.0)),
        ]
        state = _make_state(nodes=nodes, buses={})
        model = MockGuiModel(state)
        feats = [_feat("generator", name="G1", capacity_mw=10.0, fuel="solar")]
        gmb.build_grid_from_features(model, feats, target_node=1)
        inst = next(iter(state.generators.values()))
        assert inst.node == 1


class TestOSMFetcherTiling:
    """OSM fetcher splits large regions into tiles and treats a server-side
    timeout (HTTP 200 + 'runtime error' remark) as a retryable failure rather
    than a silently empty success. Regression for the all-of-Japan Grid Builder
    timeout."""

    def _fetcher(self, bounds):
        from esfex.visualization.workflows.grid_mapping_fetchers import (
            OSMGridFetcher,
        )
        return OSMGridFetcher(bounds)

    def test_large_region_tiled_and_covers_bbox(self):
        f = self._fetcher((24.0, 122.9, 45.6, 145.8))  # Japan
        tiles = f._tile_bboxes()
        assert len(tiles) > 1
        # every tile within the max span
        assert all(
            (t[2] - t[0]) <= 5.0 + 1e-9 and (t[3] - t[1]) <= 5.0 + 1e-9
            for t in tiles
        )
        # union of tiles equals the original bbox
        assert min(t[0] for t in tiles) == pytest.approx(24.0)
        assert min(t[1] for t in tiles) == pytest.approx(122.9)
        assert max(t[2] for t in tiles) == pytest.approx(45.6)
        assert max(t[3] for t in tiles) == pytest.approx(145.8)

    def test_build_query_contains_bbox_and_types(self):
        f = self._fetcher((0.0, 0.0, 1.0, 1.0))
        f.element_types = {"line", "substation"}
        q = f._build_query("0,0,1,1")
        assert "[out:json][timeout:" in q
        assert '"power"="line"' in q
        assert '"power"="substation"' in q
        assert "(0,0,1,1)" in q
        # Point features use out center (centroid only); lines use out geom
        # (full trace). The blanket >; recursion is gone.
        assert "out center" in q
        assert "out geom" in q
        assert ">;" not in q

    def test_build_query_generator_excludes_rooftop_pv(self):
        # Rooftop PV is discarded downstream (_is_rooftop_solar); don't fetch
        # the flood in the first place. power=plant is an area, not a node.
        f = self._fetcher((0.0, 0.0, 1.0, 1.0))
        f.element_types = {"generator"}
        q = f._build_query("0,0,1,1")
        assert '"generator:place"!="roof"' in q
        assert '"location"!="roof"' in q
        assert 'node["power"="plant"]' not in q
        assert 'way["power"="plant"]' in q      # areas still fetched
        assert 'relation["power"="plant"]' in q

    def test_build_query_transformer_requires_voltage(self):
        # Voltage-less pole/distribution transformers are a flood and inject
        # spurious 220/110 units — require a voltage tag to keep only the
        # transmission-relevant ones.
        f = self._fetcher((0.0, 0.0, 1.0, 1.0))
        f.element_types = {"transformer"}
        q = f._build_query("0,0,1,1")
        assert '"power"="transformer"]["voltage"]' in q

    def test_build_query_excludes_roads_from_power_query(self):
        # Major roads must NEVER ride along in the electrical query: a heavy
        # road tile 500s and would otherwise drop substations/lines with it.
        f = self._fetcher((0.0, 0.0, 1.0, 1.0))
        f.element_types = {"substation", "line", "fuel_entry", "fuel_storage"}
        q = f._build_query("0,0,1,1")
        assert "highway" not in q
        assert '"power"="substation"' in q

    def test_build_query_roads_only_is_highway_only(self):
        f = self._fetcher((0.0, 0.0, 1.0, 1.0))
        q = f._build_query("0,0,1,1", roads_only=True)
        assert "highway" in q
        assert "motorway|trunk|primary" in q
        assert "out geom" in q
        # No electrical elements leak into the road query.
        assert '"power"=' not in q

    def test_wants_roads_flag_tracks_fuel_element_types(self):
        assert self._fetcher((0.0, 0.0, 1.0, 1.0))._wants_roads is False
        f = self._fetcher((0.0, 0.0, 1.0, 1.0))
        f2 = type(f)(
            (0.0, 0.0, 1.0, 1.0),
            element_types={"substation", "fuel_entry"},
        )
        assert f2._wants_roads is True

    def test_road_fetch_failure_never_drops_the_grid(self, monkeypatch):
        # A road tile that 500s must be swallowed: single shot, no retry, no
        # subdivision — return nothing, count the failure, never raise.
        f = self._fetcher((0.0, 0.0, 1.0, 1.0))

        class _Resp500:
            status_code = 500
            content = b"Internal Server Error"

        class _FakeRequests:
            RequestException = _ReqExc

            def post(self, *a, **k):
                return _Resp500()

        monkeypatch.setattr(f, "_tile_cache_path", lambda tile, roads=False: None)
        out = f._road_fetch_tile(
            (0.4, 0.4, 0.5, 0.5), object(), {}, _FakeRequests())
        assert out == []
        assert f.road_query_failures >= 1

    def test_fuel_roads_pass_only_queries_fuel_tiles(self, monkeypatch):
        # The road pass must query ONLY tiles holding fuel infrastructure, not
        # the whole country, and must never touch a fuel-free build.
        f = self._fetcher((0.0, 0.0, 3.0, 3.0))
        tiles = f._tile_bboxes()
        queried = []
        monkeypatch.setattr(
            f, "_road_fetch_tile",
            lambda tile, *a, **k: queried.append(tile) or [])

        class _Feat:
            def __init__(self, ft, lat, lng):
                self.feature_type, self.latitude, self.longitude = ft, lat, lng

        import time as _t
        # No fuel features → no road tiles queried at all.
        assert f._fetch_fuel_roads(tiles, [], object(), {}, _t, object()) == []
        assert queried == []
        # One fuel entry at (0.5, 0.5) → only its containing tile is queried.
        feats = [_Feat("fuel_entry", 0.5, 0.5)]
        f._fetch_fuel_roads(tiles, feats, object(), {}, _t, object())
        assert len(queried) == 1
        (ts, tw, tn, te) = queried[0]
        assert ts <= 0.5 <= tn and tw <= 0.5 <= te

    def test_post_query_runtime_error_is_not_silent_success(self):
        f = self._fetcher((0.0, 0.0, 1.0, 1.0))

        class _Resp:
            status_code = 200
            content = b'{"elements": [], "remark": "runtime error: Query timed out"}'
            text = ""

        class _FakeRequests:
            RequestException = _ReqExc

            def post(self, *a, **kw):
                return _Resp()

        class _FakeApi:
            def parse_json(self, content):
                raise AssertionError("parse_json called on a timeout remark")

        class _FakeTime:
            def sleep(self, *_):
                pass

        with pytest.raises(RuntimeError, match="(?i)overpass"):
            f._post_query(_FakeApi(), {}, "q", _FakeTime(), _FakeRequests())

    def test_post_query_clean_body_parses(self):
        f = self._fetcher((0.0, 0.0, 1.0, 1.0))

        # A non-empty clean 200 body must parse and return immediately (not be
        # mistaken for a timeout, nor re-queried by the zero-element guard).
        class _Result:
            nodes = []
            ways = [object()]
            relations = []

        sentinel = _Result()

        class _Resp:
            status_code = 200
            content = b'{"elements": [{"type": "way", "id": 1}]}'
            text = ""

        class _FakeRequests:
            RequestException = _ReqExc

            def post(self, *a, **kw):
                return _Resp()

        class _FakeApi:
            def parse_json(self, content):
                return sentinel

        class _FakeTime:
            def sleep(self, *_):
                pass

        # _post_query returns (parsed_result, raw_bytes) — the raw body is
        # carried out so the caller can cache the tile.
        out, raw = f._post_query(_FakeApi(), {}, "q", _FakeTime(), _FakeRequests())
        assert out is sentinel
        assert raw == _Resp.content


class TestOSMFetcherOutputParsing:
    """The query trades the blanket ``>;`` recursion for ``out center`` on
    point features and ``out geom`` on lines, so _process_way must read
    coordinates from center/geometry rather than resolved way.nodes."""

    def _fetcher(self):
        from esfex.visualization.workflows.grid_mapping_fetchers import (
            OSMGridFetcher,
        )
        f = OSMGridFetcher((0.0, 0.0, 1.0, 1.0))
        f.min_voltage_kv = 0.0
        f.min_capacity_mw = 0.0
        return f

    def test_point_way_uses_centroid_from_out_center(self):
        overpy = pytest.importorskip("overpy")
        f = self._fetcher()
        r = overpy.Overpass().parse_json(
            '{"version":0.6,"elements":[{"type":"way","id":1,'
            '"center":{"lat":43.1,"lon":141.2},'
            '"tags":{"power":"substation","voltage":"220000"}}]}')
        feat = f._process_way(r.ways[0])
        assert feat is not None
        assert feat.feature_type == "substation"
        assert (feat.latitude, feat.longitude) == (43.1, 141.2)
        assert not feat.line_coords  # a point, no trace

    def test_line_way_uses_full_geometry_from_out_geom(self):
        overpy = pytest.importorskip("overpy")
        f = self._fetcher()
        r = overpy.Overpass().parse_json(
            '{"version":0.6,"elements":[{"type":"way","id":2,'
            '"geometry":[{"lat":43.0,"lon":141.0},{"lat":43.5,"lon":141.5},'
            '{"lat":44.0,"lon":142.0}],'
            '"tags":{"power":"line","voltage":"275000"}}]}')
        feat = f._process_way(r.ways[0])
        assert feat is not None
        assert feat.feature_type == "line"
        assert feat.line_coords == [(43.0, 141.0), (43.5, 141.5), (44.0, 142.0)]


class TestOSMFetcher400Handling:
    """HTTP 400 from Overpass covers both genuine query-syntax errors (fatal)
    and transient dispatcher/load timeouts (retryable). Regression for the
    Grid Builder raising on a transient 400 mid-fetch."""

    def _fetcher(self):
        from esfex.visualization.workflows.grid_mapping_fetchers import (
            OSMGridFetcher,
        )
        return OSMGridFetcher((0.0, 0.0, 1.0, 1.0))

    def _make_requests(self, body: bytes, status: int = 400):
        class _Resp:
            status_code = status
            content = body
            text = body.decode("utf-8", "replace")

        class _FakeRequests:
            RequestException = _ReqExc
            calls = 0

            def post(self, *a, **kw):
                _FakeRequests.calls += 1
                return _Resp()

        return _FakeRequests()

    class _FakeTime:
        def sleep(self, *_):
            pass

    class _NoParseApi:
        def parse_json(self, content):
            raise AssertionError("parse_json must not run on a 400")

    def test_400_is_retried_not_fatal(self):
        # Our Overpass query is machine-generated and static, so an
        # intermittent 400 means the server is busy, not a syntax error.
        # It must be retried (across mirrors), not raised on the first hit.
        f = self._fetcher()
        req = self._make_requests(
            b'<html><body><p><strong style="color:#FF0000">Error</strong>: '
            b'runtime error: Dispatcher_Client::request_read_and_idx::timeout. '
            b'The server is probably too busy.</p></body></html>'
        )
        with pytest.raises(RuntimeError, match="(?i)overpass"):
            f._post_query(self._NoParseApi(), {}, "q", self._FakeTime(), req)
        assert req.calls > 1  # retried, not fatal

    def test_overload_400_is_retried_then_raised(self):
        f = self._fetcher()
        req = self._make_requests(
            b'<html>Error: Dispatcher_Client::request_read_and_idx::timeout. '
            b'Probably the server is overloaded.</html>'
        )
        with pytest.raises(RuntimeError, match="(?i)overpass"):
            f._post_query(self._NoParseApi(), {}, "q", self._FakeTime(), req)
        # Retryable => multiple POST attempts before giving up.
        assert req.calls > 1


class TestOSMFeatureReduction:
    """Dense regions (e.g. Japan) map hundreds of thousands of rooftop PV
    panels that swamped the GUI and triggered an O(n^2) dedup freeze. They are
    dropped at filter time; dedup is spatially bucketed; the total is capped."""

    def _fetcher(self, **kw):
        from esfex.visualization.workflows.grid_mapping_fetchers import (
            OSMGridFetcher,
        )
        return OSMGridFetcher((34.0, 138.0, 37.0, 141.0), **kw)

    def _gf(self, **kw):
        from esfex.visualization.workflows.grid_mapping_fetchers import (
            GridFeature,
        )
        base = dict(source="osm", name="x", latitude=35.0, longitude=139.0)
        base.update(kw)
        return GridFeature(**base)

    def test_rooftop_pv_dropped_real_kept(self):
        f = self._fetcher(min_voltage_kv=110.0)
        feats = [
            # rooftop PV: power=generator, solar, no capacity -> dropped
            self._gf(feature_type="generator", fuel="Solar", capacity_mw=0.0,
                     raw_tags={"power": "generator", "generator:source": "solar"}),
            # utility solar plant -> kept
            self._gf(feature_type="generator", name="farm", fuel="Solar",
                     capacity_mw=40.0,
                     raw_tags={"power": "plant", "plant:source": "solar"}),
            # capacity-tagged solar generator -> kept
            self._gf(feature_type="generator", name="sized", fuel="Solar",
                     capacity_mw=10.0,
                     raw_tags={"power": "generator", "generator:source": "solar"}),
            # thermal generator with no capacity (e.g. Cuba) -> kept (not solar)
            self._gf(feature_type="generator", name="thermal", fuel="Gas",
                     capacity_mw=0.0,
                     raw_tags={"power": "generator", "generator:source": "gas"}),
            self._gf(feature_type="substation", name="ss", voltage_kv=275.0),
        ]
        out = self._fetcher(min_voltage_kv=110.0)._apply_filters(feats)
        names = {x.name for x in out}
        assert "x" not in names                      # rooftop PV dropped
        assert {"farm", "sized", "thermal", "ss"} <= names

    def test_feature_cap_keeps_infra_and_largest_gens(self):
        f = self._fetcher()
        f._MAX_GUI_FEATURES = 100
        feats = [self._gf(feature_type="substation", name=f"ss{i}",
                          voltage_kv=275.0) for i in range(60)]
        feats += [self._gf(feature_type="generator", name=f"g{i}",
                           capacity_mw=float(i)) for i in range(200)]
        out = f._cap_features(feats)
        assert len(out) == 100
        # all 60 substations survive; 40 largest generators kept
        assert sum(1 for x in out if x.feature_type == "substation") == 60
        gens = [x for x in out if x.feature_type == "generator"]
        assert len(gens) == 40
        assert min(g.capacity_mw for g in gens) == 160.0  # 200-40

    def test_dedup_osm_authoritative_gapfill(self):
        from esfex.visualization.workflows.grid_mapping_fetchers import (
            deduplicate_features,
        )
        # 3 co-located OSM units of a real multi-unit plant → ALL kept (a naive
        # proximity merge would wrongly collapse them, losing real capacity).
        units = [
            self._gf(feature_type="generator", name=f"u{i}", fuel="Coal",
                     capacity_mw=100.0, source="osm",
                     latitude=35.0 + i * 0.001, longitude=139.0)
            for i in range(3)
        ]
        # A GEM copy of the same plant (same fuel, next to OSM) → dropped.
        gem_dup = self._gf(feature_type="generator", name="Plant", fuel="Coal",
                           capacity_mw=300.0, source="gem",
                           latitude=35.0, longitude=139.0005)
        # A GEM plant OSM lacks (far, different fuel) → kept.
        gem_new = self._gf(feature_type="generator", name="New Wind", fuel="Wind",
                           capacity_mw=50.0, source="gem",
                           latitude=35.2, longitude=139.0)
        out = deduplicate_features(units + [gem_dup, gem_new], gen_gap_km=2.0)
        names = {x.name for x in out if x.feature_type == "generator"}
        assert names == {"u0", "u1", "u2", "New Wind"}
        assert "Plant" not in names  # cross-source duplicate dropped

    def test_dedup_drops_plant_aggregate(self):
        from esfex.visualization.workflows.grid_mapping_fetchers import (
            deduplicate_features,
        )
        # OSM plant polygon (1650 MW) sitting on its 350+700+600 MW unit nodes:
        # the aggregate is dropped, the three genuine units survive.
        units = [
            self._gf(feature_type="generator", name=n, fuel="Coal",
                     capacity_mw=c, source="osm",
                     latitude=35.0 + i * 0.0005, longitude=139.0)
            for i, (n, c) in enumerate(
                [("uA", 350.0), ("uB", 700.0), ("uC", 600.0)])
        ]
        agg = self._gf(feature_type="generator", name="Plant total", fuel="Coal",
                       capacity_mw=1650.0, source="osm",
                       latitude=35.0005, longitude=139.0)
        out = deduplicate_features(units + [agg])
        names = {x.name for x in out if x.feature_type == "generator"}
        assert names == {"uA", "uB", "uC"}  # aggregate dropped, units kept

    def test_dedup_bare_osm_marker_keeps_gem_capacity(self):
        from esfex.visualization.workflows.grid_mapping_fetchers import (
            deduplicate_features,
        )
        # OSM maps Genkai as a bare 1 MW location marker (no capacity tag);
        # GEM has its true 2x1180 MW. The marker must NOT drop the GEM units.
        osm_marker = [
            self._gf(feature_type="generator", name="Genkai", fuel="Nuclear",
                     capacity_mw=1.0, source="osm",
                     latitude=33.515 + i * 0.0005, longitude=129.836)
            for i in range(2)
        ]
        gem_units = [
            self._gf(feature_type="generator", name="Genkai nuclear", fuel="Nuclear",
                     capacity_mw=1180.0, source="gem",
                     latitude=33.515, longitude=129.836),
            self._gf(feature_type="generator", name="Genkai nuclear", fuel="Nuclear",
                     capacity_mw=1180.0, source="gem",
                     latitude=33.5155, longitude=129.836),
        ]
        out = deduplicate_features(osm_marker + gem_units, gen_gap_km=2.0)
        gens = [x for x in out if x.feature_type == "generator"]
        total = sum(g.capacity_mw or 0 for g in gens if (g.capacity_mw or 0) >= 10)
        assert total >= 2360   # both GEM 1180 units survive (real capacity kept)

    def test_dedup_real_osm_still_covers_gem(self):
        from esfex.visualization.workflows.grid_mapping_fetchers import (
            deduplicate_features,
        )
        # OSM has Sendai with real capacity → the near GEM copy is dropped.
        osm = self._gf(feature_type="generator", name="Sendai", fuel="Nuclear",
                       capacity_mw=846.0, source="osm",
                       latitude=31.834, longitude=130.19)
        gem = self._gf(feature_type="generator", name="Sendai nuclear", fuel="Nuclear",
                       capacity_mw=890.0, source="gem",
                       latitude=31.834, longitude=130.189)
        out = deduplicate_features([osm, gem], gen_gap_km=2.0)
        gens = [x for x in out if x.feature_type == "generator"]
        assert len(gens) == 1 and gens[0].source == "osm"

    def test_dedup_plant_polygon_vs_units(self):
        from esfex.visualization.workflows.grid_mapping_fetchers import (
            deduplicate_features,
        )
        # OSM plant polygon (2825 MW) on its 1215+690+920 MW unit nodes:
        # keep only one representation (they are the same plant), not both.
        units = [
            self._gf(feature_type="generator", name="Shin-Oita", fuel="Natural Gas",
                     capacity_mw=c, source="osm",
                     latitude=33.24 + i * 0.0003, longitude=131.5,
                     raw_tags={"power": "generator"})
            for i, c in enumerate([1215.0, 690.0, 920.0])
        ]
        plant = self._gf(feature_type="generator", name="Shin-Oita", fuel="Natural Gas",
                         capacity_mw=2825.0, source="osm",
                         latitude=33.2405, longitude=131.5,
                         raw_tags={"power": "plant"})
        out = deduplicate_features(units + [plant])
        gens = [x for x in out if x.feature_type == "generator"]
        assert sum(g.rated_power if hasattr(g, "rated_power") else g.capacity_mw
                   for g in gens) == pytest.approx(2825.0)  # not 5650

    def test_dedup_plant_polygon_kept_when_units_incomplete(self):
        from esfex.visualization.workflows.grid_mapping_fetchers import (
            deduplicate_features,
        )
        # Plant polygon 1700 MW with only one 700 MW unit node mapped inside:
        # the polygon total is authoritative → keep 1700, drop the 700 unit.
        unit = self._gf(feature_type="generator", name="Matsuura", fuel="Coal",
                        capacity_mw=700.0, source="osm",
                        latitude=33.35, longitude=129.684,
                        raw_tags={"power": "generator"})
        plant = self._gf(feature_type="generator", name="Matsuura", fuel="Coal",
                         capacity_mw=1700.0, source="osm",
                         latitude=33.3502, longitude=129.684,
                         raw_tags={"power": "plant"})
        out = deduplicate_features([unit, plant])
        caps = [x.capacity_mw for x in out if x.feature_type == "generator"]
        assert caps == [1700.0]

    def test_dedup_large_dense_is_fast(self):
        import time
        from esfex.visualization.workflows.grid_mapping_fetchers import (
            deduplicate_features,
        )
        dense = [
            self._gf(feature_type="generator", name=f"g{i}", capacity_mw=0.0,
                     latitude=35.6 + (i % 140) * 0.001,
                     longitude=139.7 + (i // 140) * 0.001)
            for i in range(15000)
        ]
        t0 = time.time()
        deduplicate_features(dense, proximity_km=1.0)
        assert time.time() - t0 < 10.0  # O(n^2) would be minutes


class TestOverpassErrorSnippet:
    def test_extracts_message_from_html(self):
        from esfex.visualization.workflows.grid_mapping_fetchers import (
            _overpass_error_snippet,
        )
        html = ('<html><body><p><strong style="color:#FF0000">Error</strong>: '
                'runtime error: Dispatcher_Client::request_read_and_idx::timeout.'
                ' The server is probably too busy.</p></body></html>')
        out = _overpass_error_snippet(html)
        assert "Dispatcher_Client" in out
        assert "<strong" not in out

    def test_empty_and_plain(self):
        from esfex.visualization.workflows.grid_mapping_fetchers import (
            _overpass_error_snippet,
        )
        assert _overpass_error_snippet("") == ""
        assert "boom" in _overpass_error_snippet("boom plain text")


class TestOSMFetcherResilience:
    """A single failing tile (e.g. road-dense, times out on every mirror) must
    not abort the whole region; only an all-tiles failure raises."""

    def _fetcher(self):
        from esfex.visualization.workflows.grid_mapping_fetchers import (
            OSMGridFetcher,
        )
        return OSMGridFetcher((24.0, 122.9, 45.6, 145.8))  # Japan -> many tiles

    class _EmptyResult:
        nodes: list = []
        ways: list = []
        relations: list = []

    def test_one_failed_tile_is_retried_and_recovers(self, monkeypatch):
        # A tile that fails only on the first attempt (transient load spike) is
        # re-queried in the retry pass — subdivided — and recovers, so the
        # fetch reports zero permanently-failed tiles.
        import threading
        import time as _t
        monkeypatch.setattr(_t, "sleep", lambda *a, **k: None)
        f = self._fetcher()
        n = len(f._tile_bboxes())
        calls = {"i": 0}
        lock = threading.Lock()  # tiles are now fetched concurrently

        def fake_post(api, headers, query, time, requests, mirror_offset=0):
            with lock:
                i = calls["i"]
                calls["i"] += 1
            if i == 0:
                raise RuntimeError("Overpass busy (504)")
            return self._EmptyResult(), b""   # (result, raw) — empty → uncached

        monkeypatch.setattr(f, "_post_query", fake_post)
        # Never read a stale on-disk tile cache during the test.
        monkeypatch.setattr(f, "_tile_cache_path", lambda tile: None)
        out = f._fetch()
        assert calls["i"] > n           # the failed tile was retried, not skipped
        assert f.failed_tile_count == 0  # and recovered on the retry pass
        assert out == []                # empty fakes, but no exception raised

    def test_all_tiles_failed_raises(self, monkeypatch):
        import time as _t
        monkeypatch.setattr(_t, "sleep", lambda *a, **k: None)
        f = self._fetcher()

        def fake_post(*a, **k):
            raise RuntimeError("Overpass busy (504)")

        monkeypatch.setattr(f, "_post_query", fake_post)
        with pytest.raises(RuntimeError, match=r"All .* tile"):
            f._fetch()


class TestOSMFetcher400Fatal:
    """A genuine query error (Overpass 'static error', e.g. an out-of-range
    bbox) must fail fast, not burn retries."""

    def _fetcher(self):
        from esfex.visualization.workflows.grid_mapping_fetchers import (
            OSMGridFetcher,
        )
        return OSMGridFetcher((0.0, 0.0, 1.0, 1.0))

    def test_static_error_400_is_fatal_no_retry(self):
        f = self._fetcher()

        class _Resp:
            status_code = 400
            content = b""
            text = ('<p><strong style="color:#FF0000">Error</strong>: line 3: '
                    'static error: For the attribute "w" of the element '
                    '"bbox-query" the only allowed values are floats between '
                    '-180.0 and 180.0.</p>')

        class _FakeRequests:
            RequestException = _ReqExc
            calls = 0

            def post(self, *a, **k):
                _FakeRequests.calls += 1
                return _Resp()

        class _Api:
            def parse_json(self, c):
                raise AssertionError("must not parse a 400")

        class _T:
            def sleep(self, *_):
                pass

        req = _FakeRequests()
        with pytest.raises(RuntimeError, match="rejected query"):
            f._post_query(_Api(), {}, "q", _T(), req)
        assert req.calls == 1  # fatal: no retries


class TestPolygonLongitudeNormalization:
    """Leaflet can hand back longitudes outside [-180, 180] when the world map
    is panned across a copy of the globe; the domain bounds must be normalised
    so Overpass and the plant-DB filters see a valid bbox (regression: 0 plants
    + Overpass static error for a Japan polygon drawn on a wrapped map)."""

    def test_wrapped_japan_polygon_normalises_to_valid_bbox(self):
        # Same normalisation the step applies; Japan drawn at +360 longitude.
        def _norm_lng(x):
            return ((float(x) + 180.0) % 360.0) - 180.0

        ring = [[487.0, 24.0], [505.8, 24.0], [505.8, 45.6], [487.0, 45.6]]
        poly = [(max(-90.0, min(90.0, c[1])), _norm_lng(c[0])) for c in ring]
        lngs = [p[1] for p in poly]
        assert all(-180.0 <= x <= 180.0 for x in lngs)
        assert min(lngs) == pytest.approx(127.0)
        assert max(lngs) == pytest.approx(145.8)


def test_no_orphan_generators_after_build():
    """Every generator the Grid Builder creates must reference an existing
    fuel and an existing technology — including generators whose fuel was not
    detected (empty/None) or is unknown. Regression for 'generators left
    without technologies nor fuels'."""
    model = MockGuiModel(_make_state(buses={}))
    feats = [
        _feat("substation", name="S1", voltage_kv=220.0),
        _feat("generator", name="G_solar", capacity_mw=50.0, fuel="solar"),
        _feat("generator", name="G_empty", capacity_mw=20.0, fuel=""),
        _feat("generator", name="G_none", capacity_mw=8.0, fuel="None"),
        _feat("generator", name="G_unknown", capacity_mw=10.0, fuel="Magic Dust"),
    ]
    gmb.build_grid_from_features(model, feats)
    gens = list(model.state.generators.values())
    assert len(gens) == 4
    for g in gens:
        assert g.fuel and g.fuel in model.state.fuels, \
            f"{g.name}: fuel '{g.fuel}' not in fuels"
        assert g.technology_id and g.technology_id in model.state.technologies, \
            f"{g.name}: technology_id '{g.technology_id}' not in technologies"


class TestOSMFuelDetection:
    """OSM generator fuel detection: expanded source aliases + a method
    fallback so fewer generators fall back to 'Other'."""

    def _fetcher(self):
        from esfex.visualization.workflows.grid_mapping_fetchers import (
            OSMGridFetcher,
        )
        return OSMGridFetcher((0.0, 0.0, 1.0, 1.0))

    @pytest.mark.parametrize("tags,expected", [
        ({"power": "generator", "generator:source": "natural_gas"}, "Natural Gas"),
        ({"power": "generator", "generator:source": "lng"}, "Natural Gas"),
        ({"power": "generator", "generator:source": "lignite"}, "Coal"),
        ({"power": "generator", "generator:source": "wood"}, "Biomass"),
        ({"power": "generator", "generator:source": "landfill_gas"}, "Biogas"),
        ({"power": "generator", "generator:source": "heavy_oil"}, "Fuel_oil"),
        # method fallback when source missing/unknown
        ({"power": "generator", "generator:method": "photovoltaic"}, "Solar"),
        ({"power": "generator", "generator:method": "wind_turbine"}, "Wind"),
        ({"power": "plant", "generator:method": "fission"}, "Nuclear"),
        ({"power": "generator", "generator:method": "run-of-the-river"}, "Water"),
        # genuinely unknown still maps to Other
        ({"power": "generator", "generator:source": "unobtanium"}, "Other"),
    ])
    def test_fuel_detected(self, tags, expected):
        feat = self._fetcher()._process_element(
            tags=tags, lat=0.5, lng=0.5, osm_id="node/1")
        assert feat is not None and feat.fuel == expected


class TestTechnologyClassification:
    """powerplantmatching-style technology labels: generators sharing a fuel
    map to distinct technologies (CCGT vs OCGT, steam turbine vs engine, hydro
    sub-types) with realistic efficiencies."""

    @pytest.mark.parametrize("fuel,hint,cap,expected", [
        ("naturalgas", "", 300.0, "CCGT"),         # large gas -> CCGT
        ("naturalgas", "", 30.0, "OCGT"),          # small gas -> OCGT
        ("naturalgas", "OCGT", 500.0, "OCGT"),     # hint overrides capacity
        ("naturalgas", "combined cycle", 5.0, "CCGT"),
        ("coal", "", 0.0, "Coal ST"),
        ("fuel_oil", "", 0.0, "Oil ST"),
        ("diesel", "", 0.0, "Combustion Engine"),
        ("diesel", "steam turbine", 0.0, "Oil ST"),
        ("water", "pumped storage", 0.0, "Pumped Storage"),
        ("water", "run-of-river", 0.0, "Run-Of-River"),
        ("water", "", 0.0, "Reservoir"),
        ("sun", "", 0.0, "PV"),
        ("wind", "", 0.0, "Onshore Wind"),
        ("wind", "offshore", 0.0, "Offshore Wind"),
        ("nuclear", "", 0.0, "Nuclear"),
        ("magicdust", "", 0.0, "Other"),
    ])
    def test_classify(self, fuel, hint, cap, expected):
        assert gmb.classify_technology(fuel, "", hint, cap) == expected

    def test_catalog_entry_per_label(self):
        # Every label classify_technology can return has a catalog entry.
        for spec in gmb._TECH_CATALOG.values():
            assert spec["fuel"] and spec["name"] and spec["category"]
        assert gmb._TECH_CATALOG["CCGT"]["eff_at_rated"] > \
            gmb._TECH_CATALOG["OCGT"]["eff_at_rated"]


def test_build_creates_distinct_gas_technologies():
    """A large and a small gas plant must end up on CCGT and OCGT
    respectively — not collapsed into one gas technology."""
    model = MockGuiModel(_make_state(buses={}))
    feats = [
        _feat("substation", name="S1", voltage_kv=220.0),
        _feat("generator", name="BigGas", capacity_mw=400.0, fuel="gas"),
        _feat("generator", name="Peaker", capacity_mw=40.0, fuel="gas"),
        _feat("generator", name="CoalU", capacity_mw=600.0, fuel="coal"),
    ]
    gmb.build_grid_from_features(model, feats)
    techs = {t.name for t in model.state.technologies.values()}
    assert "CCGT" in techs and "OCGT" in techs and "Coal Steam Turbine" in techs
    by_name = {g.name: g for g in model.state.generators.values()}
    ccgt = next(t.tech_id for t in model.state.technologies.values()
                if t.name == "CCGT")
    ocgt = next(t.tech_id for t in model.state.technologies.values()
                if t.name == "OCGT")
    assert by_name["BigGas"].technology_id == ccgt
    assert by_name["Peaker"].technology_id == ocgt


def test_build_scales_subquadratically():
    """The build must not be O(features x buses): a country-scale feature set
    (which used to take 30+ minutes / appeared hung at 'Building network…')
    completes in seconds thanks to the spatial bus grid + node KD-tree.
    Generous bound to stay robust on slow CI; the real time is ~1 s."""
    import time
    import random
    from esfex.visualization.data.gui_model import GuiNode

    model = MockGuiModel(_make_state(buses={}))
    model.state.nodes = []
    for i in range(200):
        model.state.nodes.append(GuiNode(
            index=i, name=f"N{i}",
            centroid_lat=24 + i * 0.05, centroid_lng=122 + (i % 30) * 0.6))
    rng = random.Random(1)
    feats = [_feat("substation", name="S1", voltage_kv=220.0)]  # placeholder
    feats = []
    for i in range(4000):
        lat = 24 + rng.random() * 21
        lng = 122 + rng.random() * 23
        feats.append(_feat("substation", name=f"S{i}", voltage_kv=220.0,
                           lat=lat, lng=lng))
        feats.append(_feat("line", name=f"L{i}", voltage_kv=220.0,
                           line_coords=[(lat, lng), (lat + 0.1, lng + 0.1)]))
    t0 = time.time()
    res = gmb.build_grid_from_features(model, feats)
    elapsed = time.time() - t0
    assert res.buses_added > 0
    assert elapsed < 20.0, f"build too slow ({elapsed:.1f}s) — O(n^2) regression?"


def test_spatial_bus_index_matches_linear_scan():
    """The grid-indexed nearest-bus must return the same bus a full scan would."""
    from esfex.visualization.data import geo_asset_parser as gap
    from esfex.visualization.data.gui_model import GuiBus

    state = _make_state(buses={}).__class__(name="S")
    # scatter buses, all at 220 kV
    import random
    rng = random.Random(0)
    for i in range(300):
        bid = f"bus_{i}"
        state.buses[bid] = GuiBus(
            bus_id=bid, name=bid, parent_node=0, voltage_kv=220.0,
            latitude=20 + rng.random() * 20, longitude=-85 + rng.random() * 20)
    # query several points; compare grid result to a brute-force scan
    for _ in range(20):
        lat = 20 + rng.random() * 20
        lng = -85 + rng.random() * 20
        gid, gdist = gap._find_nearest_bus(lat, lng, state, _snap_km=5.0,
                                           voltage_kv=220.0)
        # brute force
        best = min(state.buses.items(),
                   key=lambda kv: gap._haversine_km(lat, lng,
                                                    kv[1].latitude, kv[1].longitude))
        bdist = gap._haversine_km(lat, lng, best[1].latitude, best[1].longitude)
        # within the search window the grid must find the true nearest
        if bdist <= 5.0 / 111.0 + 0.5:
            assert gid == best[0]


def test_overpassing_substation_yields_connected_network():
    """End-to-end (#16): a line passing over a mid-route substation is split so
    the substation connects to both segments — the network is connected from
    real geometry, with no fabricated bridge."""
    from collections import Counter
    model = MockGuiModel(_make_state(buses={}))
    feats = [
        _feat("substation", name="Smid", lat=21.0, lng=-81.5, voltage_kv=220.0),
        _feat("line", name="L", lat=21.0, lng=-82.0, voltage_kv=220.0,
              line_coords=[(21.0, -82.0), (21.0, -81.0)]),
    ]
    gmb.build_grid_from_features(model, feats, snap_threshold_km=5.0)
    s = model.state
    # the line was split at the overpassing substation → two segments
    assert len(s.transmission_lines) == 2
    # both segments terminate on the same (substation) bus → A—S—B connected
    uses = Counter()
    for ln in s.transmission_lines:
        uses[ln.from_bus] += 1
        uses[ln.to_bus] += 1
    assert max(uses.values()) == 2  # the shared substation bus


def test_line_near_one_substation_is_not_collapsed():
    """A line spanning ~3 km with one end at a substation must NOT collapse
    onto that single bus (both endpoints snapping to it → dropped self-loop).
    The tight line-endpoint snap gives the far end its own bus so the real
    line survives. (#16)"""
    model = MockGuiModel(_make_state(buses={}))
    feats = [
        _feat("substation", name="S", lat=21.0, lng=-82.0, voltage_kv=220.0),
        # ~3.1 km east of S — beyond the 0.5 km line-snap, within the old 5 km.
        _feat("line", name="L", lat=21.0, lng=-82.0, voltage_kv=220.0,
              line_coords=[(21.0, -82.0), (21.0, -81.97)]),
    ]
    gmb.build_grid_from_features(model, feats, snap_threshold_km=5.0)
    s = model.state
    assert len(s.transmission_lines) == 1  # survived, not dropped
    ln = s.transmission_lines[0]
    assert ln.from_bus != ln.to_bus  # two distinct buses, no collapse


# ======================================================================
# Voltage consistency at buses (#18)
# ======================================================================


def _mismatch_lines(state) -> list[str]:
    """Lines joining buses of different voltage with no transformer between."""
    tr_pairs = {frozenset((t.from_bus, t.to_bus)) for t in state.transformers}
    bad = []
    for ln in state.transmission_lines:
        fb = state.buses.get(ln.from_bus)
        tb = state.buses.get(ln.to_bus)
        if fb and tb and (fb.voltage_kv or 0) > 0 and (tb.voltage_kv or 0) > 0:
            if abs(gmb._normalize_voltage_kv(fb.voltage_kv)
                   - gmb._normalize_voltage_kv(tb.voltage_kv)) > 1e-6:
                if frozenset((ln.from_bus, ln.to_bus)) not in tr_pairs:
                    bad.append(ln.line_id)
    return bad


class TestVoltageConsistency:
    def test_line_endpoint_buses_inherit_line_voltage(self):
        # Part 1: a line's new endpoint buses take the LINE's voltage, not the
        # old hard-coded 220 kV default — proving the voltage is passed through.
        state = _make_state(buses={})
        result = ParseResult()
        line = _feat("line", name="L", voltage_kv=66.0,
                     line_coords=[(21.0, -82.10), (21.10, -82.0)])
        gmb._create_line(state, line, 0.1, result, {}, 0)
        assert len(state.transmission_lines) == 1
        ln = state.transmission_lines[0]
        assert state.buses[ln.from_bus].voltage_kv == 66.0
        assert state.buses[ln.to_bus].voltage_kv == 66.0
        assert ln.voltage_kv == 66.0

    def test_connect_colocated_voltage_levels_adds_transformer(self):
        # Part 2: two co-located buses at different voltages get an auto-TR.
        state = _make_state(buses={
            "b132": GuiBus(bus_id="b132", parent_node=0, voltage_kv=132.0,
                           latitude=21.0, longitude=-82.0),
            "b110": GuiBus(bus_id="b110", parent_node=0, voltage_kv=110.0,
                           latitude=21.0001, longitude=-82.0001),
        })
        result = ParseResult()
        n = gmb._connect_colocated_voltage_levels(state, result, 1.0)
        assert n == 1
        assert len(state.transformers) == 1
        tr = state.transformers[0]
        assert {tr.from_voltage_kv, tr.to_voltage_kv} == {132.0, 110.0}
        assert tr.from_voltage_kv == 132.0  # HV is the from side
        # Idempotent: the level pair is now bridged, so a second run adds none.
        assert gmb._connect_colocated_voltage_levels(state, result, 1.0) == 0

    def test_no_transformer_for_far_apart_buses(self):
        # Different voltage but NOT co-located → no fabricated transformer.
        state = _make_state(buses={
            "b132": GuiBus(bus_id="b132", parent_node=0, voltage_kv=132.0,
                           latitude=21.0, longitude=-82.0),
            "b110": GuiBus(bus_id="b110", parent_node=0, voltage_kv=110.0,
                           latitude=22.0, longitude=-83.0),
        })
        result = ParseResult()
        assert gmb._connect_colocated_voltage_levels(state, result, 1.0) == 0
        assert len(state.transformers) == 0

    def test_three_levels_chain_not_mesh(self):
        # 220/132/110 co-located → adjacent-level chain (2 TR), not a mesh (3).
        state = _make_state(buses={
            "b220": GuiBus(bus_id="b220", parent_node=0, voltage_kv=220.0,
                           latitude=21.0, longitude=-82.0),
            "b132": GuiBus(bus_id="b132", parent_node=0, voltage_kv=132.0,
                           latitude=21.0001, longitude=-82.0),
            "b110": GuiBus(bus_id="b110", parent_node=0, voltage_kv=110.0,
                           latitude=21.0002, longitude=-82.0),
        })
        result = ParseResult()
        n = gmb._connect_colocated_voltage_levels(state, result, 1.0)
        assert n == 2
        pairs = {frozenset((t.from_voltage_kv, t.to_voltage_kv))
                 for t in state.transformers}
        assert pairs == {frozenset((220.0, 132.0)), frozenset((132.0, 110.0))}

    def test_voltage_less_line_does_not_span_levels(self):
        # An OSM line with NO voltage between a 220 kV and a 110 kV substation
        # must not become a 220↔110 mismatch line: infer one voltage (the
        # higher) for both ends and step down with an auto-transformer. (#18)
        state = _make_state(buses={})
        model = MockGuiModel(state)
        feats = [
            _feat("substation", name="S220", lat=21.0, lng=-82.0,
                  voltage_kv=220.0),
            _feat("substation", name="S110", lat=21.5, lng=-82.5,
                  voltage_kv=110.0),
            _feat("line", name="L", voltage_kv=0.0,
                  line_coords=[(21.0, -82.0), (21.5, -82.5)]),
        ]
        gmb.build_grid_from_features(model, feats, bus_strategy="per_voltage",
                                     snap_threshold_km=5.0)
        assert _mismatch_lines(state) == []
        # the voltage-less line took a single inferred voltage (the higher end)
        assert state.transmission_lines[0].voltage_kv == 220.0
        # a 220/110 step-down transformer ties the 110 kV substation in
        assert any({t.from_voltage_kv, t.to_voltage_kv} == {220.0, 110.0}
                   for t in state.transformers)


# ---------------------------------------------------------------------------
# _enforce_min_voltage — collapse the sub-floor distribution layer
# ---------------------------------------------------------------------------

class TestEnforceMinVoltage:
    """The voltage floor must be honoured on the *built* network, not only at
    fetch time: unknown-voltage features that were assigned a low voltage
    during the build (6.6 / 22 / 33 kV under a 66 kV floor) must be collapsed,
    with their generation relocated to the nearest transmission bus."""

    def _state(self):
        from esfex.visualization.data.gui_model import (
            GuiBatteryInstance, GuiTransmissionLine,
        )
        buses = {
            "hv": GuiBus(bus_id="hv", parent_node=0, voltage_kv=220.0,
                         latitude=21.0, longitude=-82.0),
            "mv": GuiBus(bus_id="mv", parent_node=0, voltage_kv=66.0,
                         latitude=21.1, longitude=-82.0),
            "lv": GuiBus(bus_id="lv", parent_node=0, voltage_kv=6.6,
                         latitude=21.11, longitude=-82.0, role="load",
                         demand_fraction=0.5),
        }
        gen = GuiGeneratorInstance(
            instance_id="g1", unit_key="u1", name="G1", gen_type="Thermal",
            fuel="Coal", bus="lv", node=0, latitude=21.11, longitude=-82.0,
            rated_power=[500.0],
        )
        bat = GuiBatteryInstance(
            instance_id="b1", unit_key="bu1", name="B1", fuel="Battery",
            bus="lv", node=0, latitude=21.11, longitude=-82.0,
        )
        lines = [
            GuiTransmissionLine(line_id="l_hv_mv", from_bus="hv", to_bus="mv"),
            GuiTransmissionLine(line_id="l_mv_lv", from_bus="mv", to_bus="lv"),
        ]
        trafos = [GuiTransformer(name="t1", from_bus="mv", to_bus="lv")]
        return _make_state(buses=buses, generators={"g1": gen},
                           batteries={"b1": bat},
                           transmission_lines=lines, transformers=trafos)

    def test_collapses_sub_floor_and_relocates_generation(self):
        state = self._state()
        result = ParseResult()
        stats = gmb._enforce_min_voltage(state, 66.0, result)
        # The 6.6 kV bus is gone; 220 and 66 kV survive.
        assert "lv" not in state.buses
        assert set(state.buses) == {"hv", "mv"}
        # Generation relocated to the nearest surviving bus (66 kV mv).
        assert state.generators["g1"].bus == "mv"
        assert state.batteries["b1"].bus == "mv"
        # Distribution lines/transformers incident to lv are removed.
        assert [ln.line_id for ln in state.transmission_lines] == ["l_hv_mv"]
        assert state.transformers == []
        assert stats["buses_removed"] == 1
        assert stats["generators_moved"] == 1

    def test_noop_when_floor_zero_or_all_above(self):
        state = self._state()
        result = ParseResult()
        assert gmb._enforce_min_voltage(state, 0.0, result) == {}
        assert "lv" in state.buses  # untouched
        # Floor below everything present: nothing collapses.
        assert gmb._enforce_min_voltage(state, 1.0, result) == {}
        assert "lv" in state.buses

    def test_skips_when_no_bus_at_floor(self):
        # Only sub-floor buses: don't empty the grid, warn instead.
        buses = {"lv": GuiBus(bus_id="lv", parent_node=0, voltage_kv=6.6,
                              latitude=21.0, longitude=-82.0)}
        state = _make_state(buses=buses)
        result = ParseResult()
        stats = gmb._enforce_min_voltage(state, 66.0, result)
        assert stats == {}
        assert "lv" in state.buses
        assert any("no bus at or above" in w for w in result.warnings)


class TestGeneratorCapacityGuard:
    """A gross OSM capacity tag error (e.g. a polygon tagged 24.9 GW for a
    ~5 GW gas station) is capped at a per-fuel world-record ceiling, while a
    real megaproject (Three Gorges, 22.5 GW) and any already-plausible value
    are left untouched. (A cross-source "correction" against GEM/WRI was tried
    and removed — they are unit-level and often incomplete, so a single
    covering row is not a plant total.)"""

    def _gf(self, **kw):
        from esfex.visualization.workflows.grid_mapping_fetchers import (
            GridFeature,
        )
        base = dict(feature_type="generator", name="Plant", fuel="Natural_gas",
                    latitude=35.3, longitude=139.8, source="osm",
                    osm_id="osm/Plant")
        base.update(kw)
        return GridFeature(**base)

    def test_fallback_cap_when_no_gem(self):
        from esfex.visualization.workflows.grid_mapping_fetchers import (
            _cap_implausible_capacity,
        )
        osm = self._gf(capacity_mw=24905.0)   # no GEM to cross-check
        out = _cap_implausible_capacity([osm])
        assert abs(out[0].capacity_mw - 8200.0) < 1e-6   # gas ceiling

    def test_fallback_does_not_clip_real_large_hydro(self):
        from esfex.visualization.workflows.grid_mapping_fetchers import (
            _cap_implausible_capacity,
        )
        # Three-Gorges-scale hydro (22.5 GW) is real and must survive.
        h = self._gf(capacity_mw=22500.0, fuel="Water", name="BigHydro")
        out = _cap_implausible_capacity([h])
        assert abs(out[0].capacity_mw - 22500.0) < 1e-6

    def test_max_plant_mw_overrides(self):
        from esfex.visualization.workflows.grid_mapping_fetchers import (
            _max_plant_mw,
        )
        assert _max_plant_mw("Coal") == 7700.0
        assert _max_plant_mw("Coal", {"coal": 5000.0}) == 5000.0  # user override


class TestFrequencyConsistency:
    """Every bus gets a valid mains frequency, uniform per AC-connected area,
    and equipment is aligned to its bus. Regression for the Japan 50/60 Hz
    fragmentation that stranded generation from demand (~76% spurious shed)."""

    def test_unifies_backfills_and_isolates_by_component(self):
        from esfex.visualization.data.gui_model import GuiTransmissionLine
        st = GuiSystemState(name="S")
        st.nodes = [GuiNode(index=0, name="N0")]
        st.buses = {
            # A-B-C are AC-linked (Kanto): 50, 60 (boundary noise), None.
            "A": GuiBus(bus_id="A", parent_node=0, frequency_hz=50.0,
                        latitude=35.7, longitude=139.7),
            "B": GuiBus(bus_id="B", parent_node=0, frequency_hz=60.0,
                        latitude=35.7, longitude=139.8),
            "C": GuiBus(bus_id="C", parent_node=0, frequency_hz=None,
                        latitude=35.7, longitude=139.9),
            # D is a separate component in west Japan → must stay 60 Hz.
            "D": GuiBus(bus_id="D", parent_node=0, frequency_hz=60.0,
                        latitude=34.7, longitude=135.5),
        }
        st.transmission_lines = [
            GuiTransmissionLine(line_id="L1", from_bus="A", to_bus="B"),
            GuiTransmissionLine(line_id="L2", from_bus="B", to_bus="C"),
        ]
        st.transformers = []
        st.generators = {
            "g": GuiGeneratorInstance(
                instance_id="g", unit_key="u", name="g", gen_type="Thermal",
                fuel="Gas", bus="B", node=0, frequency_hz=60.0),
        }
        st.batteries = {}

        gmb._repair_frequency_consistency(st)

        # A-B-C component: majority 50 (A=50, C backfilled→50, B=60) → all 50.
        assert st.buses["A"].frequency_hz == 50.0
        assert st.buses["B"].frequency_hz == 50.0
        assert st.buses["C"].frequency_hz == 50.0
        # D stands alone in west Japan → keeps 60 Hz (converters bridge, not us).
        assert st.buses["D"].frequency_hz == 60.0
        # Generator runs at the frequency of the bus it connects to.
        assert st.generators["g"].frequency_hz == 50.0
        # So do the AC lines of the area.
        assert {ln.frequency_hz for ln in st.transmission_lines} == {50.0}

    def test_gridfinder_lines_do_not_turn_a_60hz_grid_to_50hz(self):
        """Cuba is a 60 Hz grid. GridFinder lines and WRI/GEM plants carry no
        frequency; they used to carry the GridFeature default of 50 Hz, their
        endpoint buses outvoted the 60 Hz substations, and the whole build
        (buses, lines, generators) ended up at 50 Hz."""
        from esfex.visualization.workflows.grid_mapping_fetchers import (
            GridFeature,
        )
        model = MockGuiModel(_make_state(buses={}))
        feats = [
            GridFeature(source="osm", feature_type="substation", name="A",
                        latitude=22.0, longitude=-80.0, voltage_kv=220.0,
                        frequency_hz=60.0),
            GridFeature(source="wri", feature_type="generator", name="G",
                        latitude=22.0, longitude=-80.0, capacity_mw=100.0,
                        fuel="Gas"),
            GridFeature(source="gridfinder", feature_type="line", name="GF1",
                        latitude=22.0, longitude=-79.7,
                        line_coords=[(22.0, -80.0), (22.0, -79.5)]),
            GridFeature(source="gridfinder", feature_type="line", name="GF2",
                        latitude=22.0, longitude=-79.2,
                        line_coords=[(22.0, -79.5), (22.0, -79.0)]),
            GridFeature(source="gridfinder", feature_type="line", name="GF3",
                        latitude=22.2, longitude=-79.5,
                        line_coords=[(22.0, -79.5), (22.5, -79.5)]),
        ]
        gmb.build_grid_from_features(model, feats)
        s = model.state
        assert len(s.buses) >= 3 and len(s.transmission_lines) >= 2
        assert {b.frequency_hz for b in s.buses.values()} == {60.0}
        assert {ln.frequency_hz for ln in s.transmission_lines} == {60.0}
        assert {g.frequency_hz for g in s.generators.values()} == {60.0}


class TestVoltagePropagation:
    """Untagged buses/lines inherit their real voltage from tagged neighbours
    before the voltage floor runs. Regression for the OSM 220 kV default that
    flooded Kanto with a voltage TEPCO does not use and corrupted the derived
    line capacity/impedance (~76% spurious shed)."""

    def test_propagates_and_recomputes_capacity(self):
        from esfex.visualization.data.gui_model import GuiTransmissionLine
        from esfex.visualization.workflows.grid_mapping_quality import (
            estimate_line_capacity_mw,
        )
        st = GuiSystemState(name="S")
        st.nodes = [GuiNode(index=0, name="N0")]
        st.buses = {
            "A": GuiBus(bus_id="A", parent_node=0, voltage_kv=275.0),  # tagged
            "B": GuiBus(bus_id="B", parent_node=0, voltage_kv=0.0),    # untagged
            "C": GuiBus(bus_id="C", parent_node=0, voltage_kv=0.0),    # untagged
            "X": GuiBus(bus_id="X", parent_node=0, voltage_kv=0.0),    # isolated
        }
        st.transmission_lines = [
            GuiTransmissionLine(line_id="AB", from_bus="A", to_bus="B",
                                voltage_kv=0.0, num_circuits=2, length_km=10),
            GuiTransmissionLine(line_id="BC", from_bus="B", to_bus="C",
                                voltage_kv=0.0, num_circuits=2, length_km=10),
        ]
        st.transformers = []
        st.generators = {}
        st.batteries = {}

        changed = gmb._propagate_voltage(st)
        assert changed >= 4

        # Voltage flows A → B → C along the untagged chain.
        assert st.buses["B"].voltage_kv == 275.0
        assert st.buses["C"].voltage_kv == 275.0
        # An isolated untagged bus is medium voltage (33 kV at 50 Hz), not a
        # fixed 220 kV.
        assert st.buses["X"].voltage_kv == 33.0

        # Untagged lines take the endpoint voltage and their capacity/impedance
        # are recomputed from it (not left at the spurious default).
        exp = estimate_line_capacity_mw(275.0, 2)
        for ln in st.transmission_lines:
            assert ln.voltage_kv == 275.0
            assert abs(ln.capacity_mw - exp) < 1.0
            assert ln.reactance_pu > 0.0


class TestVoltagesFromTheData:
    """Voltages come from the data, never from fixed numbers (Cuba case: an
    untagged ``substation=transmission`` on Isla de la Juventud became 220 kV,
    and two stations tagged 132 kV, a level no Cuban line carries, put 132 kV
    buses and lines into a 220/110 kV grid)."""

    def _fetcher(self, **kw):
        from esfex.visualization.workflows.grid_mapping_fetchers import (
            OSMGridFetcher,
        )
        return OSMGridFetcher((0.0, 0.0, 1.0, 1.0), **kw)

    @pytest.mark.parametrize("role", ["transmission", "sub_transmission",
                                      "distribution"])
    def test_untagged_substation_voltage_stays_unknown(self, role):
        feat = self._fetcher()._process_element(
            tags={"power": "substation", "substation": role},
            lat=0.5, lng=0.5, osm_id="way/1")
        assert feat.voltage_kv == 0.0

    def test_untagged_transformer_secondary_and_converter_stay_unknown(self):
        f = self._fetcher()
        tr = f._process_element(tags={"power": "transformer",
                                      "voltage": "34500"},
                                lat=0.5, lng=0.5, osm_id="node/2")
        assert (tr.voltage_kv, tr.voltage_kv_secondary) == (34.5, 0.0)
        cv = f._process_element(tags={"power": "converter"},
                                lat=0.5, lng=0.5, osm_id="node/3")
        assert cv.voltage_kv == 0.0

    def test_untagged_distribution_substation_dropped_under_hv_floor(self):
        f = self._fetcher(min_voltage_kv=66.0)
        dist = f._process_element(
            tags={"power": "substation", "substation": "distribution"},
            lat=0.5, lng=0.5, osm_id="way/4")
        trans = f._process_element(
            tags={"power": "substation", "substation": "transmission"},
            lat=0.5, lng=0.6, osm_id="way/5")
        assert f._apply_filters([dist, trans]) == [trans]

    def test_country_mv_level_is_the_most_tagged(self):
        feats = [_feat("substation", voltage_kv=34.5,
                       raw_tags={"voltage": "34500"}),
                 _feat("line", voltage_kv=13.8, raw_tags={"voltage": "13800"}),
                 _feat("line", voltage_kv=34.5, raw_tags={"voltage": "34500"}),
                 _feat("line", voltage_kv=110.0, raw_tags={"voltage": "110000"})]
        assert gmb._country_mv_level(feats) == 34.5
        assert gmb._country_mv_level([feats[-1]]) == 0.0

    @staticmethod
    def _line(name, a, b, kv):
        tags = {"voltage": str(int(kv * 1000))} if kv else {}
        return _feat("line", name=name, lat=(a[0] + b[0]) / 2,
                     lng=(a[1] + b[1]) / 2, voltage_kv=kv, raw_tags=tags,
                     line_coords=[a, b], num_circuits=1)

    def test_station_level_no_line_carries_is_snapped(self):
        lines = [self._line("L1", (21.0, -82.0), (21.5, -82.0), 110.0),
                 self._line("L2", (21.5, -82.0), (22.0, -82.0), 220.0)]
        odd = _feat("substation", name="PCC", lat=23.0, lng=-82.0,
                    voltage_kv=132.0, osm_id="way/9")
        hv = _feat("substation", name="HV", lat=24.0, lng=-82.0,
                   voltage_kv=500.0, osm_id="way/10")   # 500/220 > 1.3: kept
        mv = _feat("substation", name="MV", lat=25.0, lng=-82.0,
                   voltage_kv=13.8, osm_id="way/11")    # MV: kept
        notes = []
        out = gmb._reconcile_station_voltages([odd, hv, mv], lines, 1.0, notes)
        assert [s.voltage_kv for s in out] == [110.0, 500.0, 13.8]
        assert odd.voltage_kv == 132.0                  # input untouched
        assert [(n["name"], n["was"], n["kv"], n["rule"]) for n in notes] == [
            ("PCC", 132.0, 110.0, "off_standard_level")]

    def test_station_tag_overruled_when_its_lines_agree(self):
        st = (20.3, -76.9)
        lines = [self._line("A", st, (20.5, -76.9), 110.0),
                 self._line("B", (20.1, -76.9), st, 110.0),
                 self._line("C", (21.0, -77.0), (21.5, -77.0), 220.0)]
        sub = _feat("substation", name="Mambisa", lat=st[0], lng=st[1],
                    voltage_kv=220.0, osm_id="way/12")
        lone = _feat("substation", name="Lone", lat=20.5, lng=-76.9,
                     voltage_kv=220.0, osm_id="way/13")  # one line: kept
        notes = []
        out = gmb._reconcile_station_voltages([sub, lone], lines, 1.0, notes)
        assert [s.voltage_kv for s in out] == [110.0, 220.0]
        assert notes[0]["rule"] == "line_consensus"

    def test_neighbour_stations_lines_are_not_borrowed(self):
        # A 110 kV station 300 m from a 220 kV one keeps its tag: the 220 kV
        # lines end at the 220 kV station (Cuba: "TE Refinería 110 kV").
        s220 = (20.005, -75.871)
        lines = [self._line("A", s220, (20.2, -75.871), 220.0),
                 self._line("B", (19.8, -75.871), s220, 220.0)]
        st220 = _feat("substation", name="Ref 220", lat=s220[0], lng=s220[1],
                      voltage_kv=220.0, osm_id="way/20")
        st110 = _feat("substation", name="Ref 110", lat=20.0077, lng=-75.871,
                      voltage_kv=110.0, osm_id="way/21")
        notes = []
        out = gmb._reconcile_station_voltages([st220, st110], lines, 1.0, notes)
        assert [s.voltage_kv for s in out] == [220.0, 110.0] and notes == []

    def test_isolated_island_gets_mv_and_survives_the_floor(self):
        # Mainland: two 110 kV stations on a tagged line. Island: an untagged
        # "transmission" station, an untagged line and a plant; the data tags
        # 34.5 kV somewhere. Floor 66 kV.
        from esfex.visualization.data.gui_model import GuiModel
        m = GuiModel()
        m._undo_suspended = True
        while m.state.nodes:
            m.remove_node(len(m.state.nodes) - 1)
        for name, la, lo in [("Main", 22.5, -82.0), ("Isle", 21.7, -82.8)]:
            m.update_node(m.add_node(name), centroid_lat=la, centroid_lng=lo)
        feats = [
            _feat("substation", name="S1", lat=22.5, lng=-82.0,
                  voltage_kv=110.0, raw_tags={"voltage": "110000"}, osm_id="s1"),
            _feat("substation", name="S2", lat=22.6, lng=-82.0,
                  voltage_kv=110.0, raw_tags={"voltage": "110000"}, osm_id="s2"),
            self._line("L12", (22.5, -82.0), (22.6, -82.0), 110.0),
            _feat("substation", name="Mv", lat=22.9, lng=-82.0,
                  voltage_kv=34.5, raw_tags={"voltage": "34500"}, osm_id="s3"),
            _feat("substation", name="Isle", lat=21.70, lng=-82.80,
                  voltage_kv=0.0, raw_tags={"substation": "transmission"},
                  osm_id="s4"),
            _feat("substation", name="Isle2", lat=21.80, lng=-82.80,
                  voltage_kv=0.0, osm_id="s5"),
            self._line("Lisle", (21.70, -82.80), (21.80, -82.80), 0.0),
            _feat("generator", name="Plant", lat=21.701, lng=-82.80,
                  capacity_mw=40.0, fuel="Oil", gen_type="Thermal",
                  osm_id="g1"),
        ]
        res = gmb.build_grid_from_features(m, feats, station_radius_km=1.0,
                                           min_voltage_kv=66.0)
        st = m.state
        isle = [b for b in st.buses.values() if b.latitude < 22.0]
        assert isle and {b.voltage_kv for b in isle} == {34.5}
        (ln,) = [l for l in st.transmission_lines if l.voltage_kv < 60]
        assert ln.capacity_mw < 60.0                 # MV rating, not 220 kV
        (g,) = st.generators.values()
        assert st.buses[g.bus].latitude < 22.0       # not moved to the mainland
        rules = {d["rule"] for d in res.voltage_decisions}
        assert "isolated_mv" in rules
        text = res.summary()
        assert "Voltage decisions" in text and "isolated grid" in text
        assert "Isle: 2 bus(es) at 34.5 kV" in text
