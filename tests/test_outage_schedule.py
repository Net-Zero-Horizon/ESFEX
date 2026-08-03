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

from esfex.bridge.converters import compile_outage_factor
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
