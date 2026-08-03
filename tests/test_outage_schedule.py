"""Tests for deterministic scheduled interruptions (Capa 1).

Covers the outage-schedule schema, the availability-mask compiler
(:func:`compile_outage_factor`) and — behind the ``julia`` marker — the
integration check that a scheduled outage is carried into the real Julia
``GeneratorConfig`` availability matrix (the value Julia now caps every
generator by).
"""
from __future__ import annotations

import numpy as np
import pytest

from esfex.bridge.converters import (
    build_outage_mask_by_id,
    build_outage_mask_matrix,
    compile_outage_factor,
    fuel_source_outage_windows,
)
from esfex.config.schema import NodeConfig, OutageWindow, SystemConfig


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
class TestOutageWindowSchema:
    def test_valid_window(self):
        w = OutageWindow(
            element_type="generator", element_id="CCGT-1",
            start_hour=100, end_hour=340, availability=0.0, label="overhaul",
        )
        assert w.element_type == "generator"
        assert w.availability == 0.0

    def test_default_availability_is_full_cut(self):
        w = OutageWindow(element_type="line", element_id="3", start_hour=0, end_hour=24)
        assert w.availability == 0.0

    def test_partial_derate_allowed(self):
        w = OutageWindow(
            element_type="fuel_source", element_id="Gas", start_hour=0,
            end_hour=48, availability=0.5,
        )
        assert w.availability == 0.5

    def test_end_must_exceed_start(self):
        with pytest.raises(ValueError):
            OutageWindow(element_type="battery", element_id="B1", start_hour=50, end_hour=50)

    def test_availability_bounds(self):
        with pytest.raises(ValueError):
            OutageWindow(element_type="generator", element_id="g", start_hour=0, end_hour=1, availability=1.5)

    def test_unknown_element_type_rejected(self):
        with pytest.raises(ValueError):
            OutageWindow(element_type="turbine", element_id="g", start_hour=0, end_hour=1)

    def test_system_config_round_trip(self):
        w = OutageWindow(element_type="generator", element_id="G1", start_hour=10, end_hour=20)
        sys = SystemConfig(
            name="S", nodes=NodeConfig(num_nodes=1, nodes_connections=[0.0]),
            outage_schedule=[w],
        )
        sys2 = SystemConfig(**sys.model_dump())
        assert len(sys2.outage_schedule) == 1
        assert sys2.outage_schedule[0].element_id == "G1"

    def test_schedule_defaults_empty(self):
        sys = SystemConfig(name="S", nodes=NodeConfig(num_nodes=1, nodes_connections=[0.0]))
        assert sys.outage_schedule == []


# ---------------------------------------------------------------------------
# Mask compiler
# ---------------------------------------------------------------------------
class TestCompileOutageFactor:
    def test_no_windows_is_all_ones(self):
        f = compile_outage_factor([], start_hour=0, hours=24)
        np.testing.assert_array_equal(f, np.ones(24))

    def test_full_cut_window(self):
        # Outage over hours [6, 12) -> those local indices become 0, rest 1.
        f = compile_outage_factor([(6, 12, 0.0)], start_hour=0, hours=24)
        assert f[5] == 1.0
        np.testing.assert_array_equal(f[6:12], np.zeros(6))
        assert f[12] == 1.0

    def test_partial_derate(self):
        f = compile_outage_factor([(6, 12, 0.5)], start_hour=0, hours=24)
        np.testing.assert_allclose(f[6:12], 0.5)
        assert f[5] == 1.0

    def test_window_offset_by_rolling_horizon(self):
        # Same absolute window [30, 34) but the local slice starts at hour 24.
        f = compile_outage_factor([(30, 34, 0.0)], start_hour=24, hours=24)
        # Local indices 6..9 correspond to absolute hours 30..33.
        assert f[5] == 1.0
        np.testing.assert_array_equal(f[6:10], np.zeros(4))
        assert f[10] == 1.0

    def test_window_entirely_outside_local_slice(self):
        f = compile_outage_factor([(1000, 1100, 0.0)], start_hour=0, hours=24)
        np.testing.assert_array_equal(f, np.ones(24))

    def test_resolution_partial_overlap_weighted(self):
        # 6-hour periods; outage covers hours [3, 6) -> half of period 0.
        # factor = (1 - 0.5) + 0.5 * 0.0 = 0.5
        f = compile_outage_factor([(3, 6, 0.0)], start_hour=0, hours=4, resolution_hours=6)
        np.testing.assert_allclose(f[0], 0.5)
        np.testing.assert_allclose(f[1:], 1.0)

    def test_overlapping_windows_compound(self):
        # Two 0.5 derates over the same hour compound to 0.25.
        f = compile_outage_factor([(6, 12, 0.5), (6, 12, 0.5)], start_hour=0, hours=24)
        np.testing.assert_allclose(f[6:12], 0.25)


# ---------------------------------------------------------------------------
# Centralised mask matrix (branch/injection element types)
# ---------------------------------------------------------------------------
class TestBuildOutageMaskMatrix:
    def test_none_when_no_matching_outage(self):
        # Windows exist but for an id not in the order -> nothing to mask.
        m = build_outage_mask_matrix(
            {"OTHER": [(6, 12, 0.0)]}, ["B1", "B2"], start_hour=0, hours=24,
        )
        assert m is None

    def test_none_when_empty(self):
        assert build_outage_mask_matrix({}, ["B1"], start_hour=0, hours=24) is None

    def test_row_alignment(self):
        # Only B2 has an outage -> its row (index 1) is masked, others stay 1.
        m = build_outage_mask_matrix(
            {"B2": [(6, 12, 0.0)]}, ["B1", "B2", "B3"], start_hour=0, hours=24,
        )
        assert m.shape == (3, 24)
        np.testing.assert_array_equal(m[0], np.ones(24))       # B1 untouched
        np.testing.assert_array_equal(m[1, 6:12], np.zeros(6))  # B2 out
        assert m[1, 5] == 1.0
        np.testing.assert_array_equal(m[2], np.ones(24))       # B3 untouched

    def test_partial_derate_row(self):
        m = build_outage_mask_matrix(
            {"E1": [(0, 6, 0.25)]}, ["E1"], start_hour=0, hours=12,
        )
        np.testing.assert_allclose(m[0, 0:6], 0.25)
        np.testing.assert_allclose(m[0, 6:], 1.0)

    def test_respects_rolling_horizon_offset(self):
        m = build_outage_mask_matrix(
            {"B1": [(30, 34, 0.0)]}, ["B1"], start_hour=24, hours=24,
        )
        np.testing.assert_array_equal(m[0, 6:10], np.zeros(4))
        assert m[0, 5] == 1.0


# ---------------------------------------------------------------------------
# Interruptions dialog (element collection + apply-to-model logic)
# ---------------------------------------------------------------------------
class TestInterruptionsDialog:
    def _dialog(self):
        import json
        pytest.importorskip("PySide6.QtWebEngineWidgets")
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        from esfex.visualization.i18n import init_i18n
        init_i18n()
        from esfex.visualization.data.gui_model import (
            GuiGeneratorInstance, GuiModel, GuiOutageWindow, GuiTransmissionLine,
        )
        m = GuiModel()
        m.state.generators["g1"] = GuiGeneratorInstance(
            instance_id="g1", unit_key="g1", name="CCGT-1",
            gen_type="Non-renewable", fuel="Gas")
        m.state.transmission_lines.append(GuiTransmissionLine(line_id="line_3"))
        m.state.outage_schedule = [GuiOutageWindow(
            element_type="generator", element_id="g1",
            start_hour=100, end_hour=340, availability=0.0)]
        from esfex.visualization.panels.interruptions_dialog import InterruptionsDialog
        return InterruptionsDialog(m), m, json

    def test_payload_lists_elements_by_group(self):
        dlg, _m, json = self._dialog()
        payload = json.loads(dlg._build_payload())
        cats = {g["category"] for g in payload["groups"]}
        assert {"generator", "line"} <= cats
        assert payload["horizon_hours"] == 8760
        assert len(payload["schedule"]) == 1

    def test_apply_writes_and_drops_invalid(self):
        dlg, m, _json = self._dialog()
        dlg._schedule = [
            {"element_type": "line", "element_id": "line_3",
             "start_hour": 0, "end_hour": 48, "availability": 0.5, "label": "maint"},
            {"element_type": "generator", "element_id": "g1",
             "start_hour": 10, "end_hour": 10},  # invalid window → dropped
        ]
        dlg._apply_to_model()
        got = m.state.outage_schedule
        assert len(got) == 1
        assert got[0].element_id == "line_3" and got[0].availability == 0.5


# ---------------------------------------------------------------------------
# GUI round-trip (SystemConfig ↔ GuiSystemState ↔ config dict)
# ---------------------------------------------------------------------------
class TestGuiRoundTrip:
    def test_outage_schedule_survives_round_trip(self):
        from tests.test_serializer import _make_esfex_config, _make_system_config
        from esfex.visualization.data.serializer import (
            _apply_gui_state_to_dict,
            config_to_gui_states,
        )

        sc = _make_system_config()
        sc.outage_schedule = [
            OutageWindow(element_type="generator", element_id="gas_turbine",
                         start_hour=100, end_hour=340, availability=0.0, label="overhaul"),
            OutageWindow(element_type="line", element_id="L1",
                         start_hour=0, end_hour=48, availability=0.5),
        ]
        states = config_to_gui_states(_make_esfex_config(sc))
        state = states["TestSystem"]
        assert [o.element_id for o in state.outage_schedule] == ["gas_turbine", "L1"]
        assert state.outage_schedule[1].availability == 0.5

        sys_dict: dict = {}
        _apply_gui_state_to_dict(state, sys_dict)
        got = sys_dict["outage_schedule"]
        assert len(got) == 2
        assert got[0]["element_type"] == "generator" and got[0]["end_hour"] == 340
        assert got[1]["element_id"] == "L1" and got[1]["availability"] == 0.5
        # Re-validates against the schema.
        assert [OutageWindow(**d).start_hour for d in got] == [100, 0]

    def test_empty_schedule_omitted(self):
        from esfex.visualization.data.gui_model import GuiSystemState
        from esfex.visualization.data.serializer import _apply_gui_state_to_dict
        sys_dict: dict = {}
        _apply_gui_state_to_dict(GuiSystemState(name="S"), sys_dict)
        assert "outage_schedule" not in sys_dict


# ---------------------------------------------------------------------------
# Id-addressed masks (branch elements: lines, converters, routes)
# ---------------------------------------------------------------------------
class TestBuildOutageMaskById:
    def test_none_when_empty(self):
        assert build_outage_mask_by_id({}, start_hour=0, hours=24) is None

    def test_keyed_by_id_not_position(self):
        m = build_outage_mask_by_id(
            {"L3": [(6, 12, 0.0)], "L7": [(0, 4, 0.5)]}, start_hour=0, hours=24,
        )
        assert set(m) == {"L3", "L7"}
        np.testing.assert_array_equal(m["L3"][6:12], np.zeros(6))
        assert m["L3"][5] == 1.0
        np.testing.assert_allclose(m["L7"][0:4], 0.5)

    def test_ids_without_windows_excluded(self):
        # Only ids that actually have windows appear (filtering-safe lookup).
        m = build_outage_mask_by_id({"L1": [], "L2": [(0, 3, 0.0)]}, start_hour=0, hours=12)
        assert set(m) == {"L2"}

    def test_rolling_horizon_offset(self):
        m = build_outage_mask_by_id({"L1": [(30, 34, 0.0)]}, start_hour=24, hours=24)
        np.testing.assert_array_equal(m["L1"][6:10], np.zeros(4))
        assert m["L1"][5] == 1.0


# ---------------------------------------------------------------------------
# Fuel-source outages (reuse FuelConfig's disruption window)
# ---------------------------------------------------------------------------
class TestFuelSourceOutageWindows:
    def test_selects_matching_fuel(self):
        sched = [
            OutageWindow(element_type="fuel_source", element_id="Gas", start_hour=0, end_hour=48, availability=0.0),
            OutageWindow(element_type="fuel_source", element_id="Coal", start_hour=10, end_hour=20),
            OutageWindow(element_type="generator", element_id="Gas", start_hour=0, end_hour=5),
        ]
        w = fuel_source_outage_windows(sched, "Gas")
        assert w == [(0, 48, 0.0)]

    def test_multiple_windows_returned_in_order(self):
        sched = [
            OutageWindow(element_type="fuel_source", element_id="Gas", start_hour=0, end_hour=48),
            OutageWindow(element_type="fuel_source", element_id="Gas", start_hour=100, end_hour=148, availability=0.5),
        ]
        w = fuel_source_outage_windows(sched, "Gas")
        assert w == [(0, 48, 0.0), (100, 148, 0.5)]

    def test_no_match_empty(self):
        assert fuel_source_outage_windows([], "Gas") == []
        sched = [OutageWindow(element_type="fuel_source", element_id="Coal", start_hour=0, end_hour=1)]
        assert fuel_source_outage_windows(sched, "Gas") == []


# ---------------------------------------------------------------------------
# Julia integration: the outage is compiled into the Julia generator struct
# ---------------------------------------------------------------------------
def _minimal_gen(name="G1", gtype="Non-renewable"):
    from esfex.config.schema import GeneratorConfig
    one = [1.0]
    zero = [0.0]
    return GeneratorConfig(
        name=name, type=gtype, fuel="Gas",
        life_time=[25.0], initial_age=zero, degradation_rate=zero,
        decommissioning_cost=zero, rated_power=[100.0], min_power=zero,
        min_up=zero, min_down=zero, ramp_up=one, ramp_down=one,
        eff_at_rated=[0.45], eff_at_min=[0.35], inertia=zero,
        start_up_cost=zero, fuel_cost=[10.0], fixed_cost=zero,
        maintenance_cost=zero,
    )


@pytest.mark.julia
def test_outage_is_carried_into_julia_generator_availability():
    """The masked availability reaches the real Julia GeneratorConfig struct.

    This exercises the true Python→Julia boundary (``convert_generator_config``)
    that every run uses: a full-cut window on a *dispatchable* unit (which has
    no capacity-factor profile) must arrive as zeros in the struct's
    ``availability`` matrix for exactly those hours — the value Julia now caps
    every generator by.
    """
    from esfex.bridge.converters import (
        compile_outage_factor,
        convert_generator_config,
    )

    hours, n_nodes = 24, 1
    gen = _minimal_gen()

    # Reproduce the adapter glue: dispatchable unit -> ones, then mask.
    factor = compile_outage_factor([(6, 12, 0.0)], start_hour=0, hours=hours)
    availability = np.ones((hours, n_nodes)) * factor[:, None]

    jl_gen = convert_generator_config(gen, availability)

    got = np.array([float(jl_gen.availability[t, 0]) for t in range(hours)])
    np.testing.assert_array_equal(got[6:12], np.zeros(6))
    assert got[5] == 1.0 and got[12] == 1.0


@pytest.mark.julia
def test_partial_derate_carried_into_julia_generator_availability():
    """A 50 % derate reaches the Julia struct as 0.5 over the window."""
    from esfex.bridge.converters import (
        compile_outage_factor,
        convert_generator_config,
    )

    hours, n_nodes = 24, 1
    gen = _minimal_gen(name="Solar", gtype="Renewable")
    factor = compile_outage_factor([(6, 12, 0.5)], start_hour=0, hours=hours)
    availability = np.ones((hours, n_nodes)) * factor[:, None]

    jl_gen = convert_generator_config(gen, availability)
    got = np.array([float(jl_gen.availability[t, 0]) for t in range(hours)])
    np.testing.assert_allclose(got[6:12], 0.5)
    assert got[5] == 1.0
