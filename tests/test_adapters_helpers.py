"""
Tests for helper functions in esfex.bridge.adapters that do NOT require Julia.

Covers:
- _haversine_km: haversine distance calculation
- _pwl_segments_from_config: loss model mapping for operational problems
- _pwl_segments_from_config_master: loss model mapping for master problem
- _RENEWABLE_FUELS: renewable fuel type set
"""

import math
import types

import pytest

from esfex.bridge.adapters import (
    _RENEWABLE_FUELS,
    _haversine_km,
    _pwl_segments_from_config,
    _pwl_segments_from_config_master,
)


# ──────────────────────────────────────────────────────────────────────
# _haversine_km
# ──────────────────────────────────────────────────────────────────────


class TestHaversineKm:
    """Tests for _haversine_km(lat1, lng1, lat2, lng2)."""

    def test_same_point_returns_zero(self):
        """Distance from a point to itself is zero."""
        assert _haversine_km(40.0, -74.0, 40.0, -74.0) == 0.0

    def test_same_point_origin(self):
        """Distance from origin (0,0) to itself is zero."""
        assert _haversine_km(0.0, 0.0, 0.0, 0.0) == 0.0

    def test_new_york_to_london(self):
        """Known distance: New York (40.7128, -74.0060) to London (51.5074, -0.1278).

        Accepted reference: ~5570 km.
        """
        dist = _haversine_km(40.7128, -74.0060, 51.5074, -0.1278)
        assert dist == pytest.approx(5570, rel=0.02)

    def test_paris_to_berlin(self):
        """Known distance: Paris (48.8566, 2.3522) to Berlin (52.5200, 13.4050).

        Accepted reference: ~878 km.
        """
        dist = _haversine_km(48.8566, 2.3522, 52.5200, 13.4050)
        assert dist == pytest.approx(878, rel=0.02)

    def test_equator_one_degree_longitude(self):
        """One degree of longitude at the equator is ~111.2 km."""
        dist = _haversine_km(0.0, 0.0, 0.0, 1.0)
        assert dist == pytest.approx(111.2, rel=0.01)

    def test_one_degree_latitude(self):
        """One degree of latitude is ~111.2 km regardless of longitude."""
        dist = _haversine_km(0.0, 0.0, 1.0, 0.0)
        assert dist == pytest.approx(111.2, rel=0.01)

    def test_symmetry(self):
        """Distance(A, B) == Distance(B, A)."""
        d1 = _haversine_km(35.0, 139.0, -33.0, 151.0)
        d2 = _haversine_km(-33.0, 151.0, 35.0, 139.0)
        assert d1 == pytest.approx(d2)

    def test_antipodal_points(self):
        """Antipodal points (opposite sides of the Earth) are ~20015 km apart."""
        dist = _haversine_km(0.0, 0.0, 0.0, 180.0)
        assert dist == pytest.approx(20015, rel=0.01)

    def test_north_pole_to_south_pole(self):
        """North Pole to South Pole is ~20015 km (half circumference)."""
        dist = _haversine_km(90.0, 0.0, -90.0, 0.0)
        assert dist == pytest.approx(20015, rel=0.01)

    def test_short_distance(self):
        """Two nearby points should give a small distance."""
        # ~1 meter apart at equator (0.00001 degrees of longitude)
        dist = _haversine_km(0.0, 0.0, 0.0, 0.00001)
        assert dist < 0.01  # less than 10 meters

    def test_return_type_is_float(self):
        """Return type is always float."""
        result = _haversine_km(10.0, 20.0, 30.0, 40.0)
        assert isinstance(result, float)

    def test_uses_earth_radius_6371(self):
        """Verify the function uses Earth radius of 6371 km.

        Quarter-circle (0,0) to (90,0) should be pi/2 * 6371 ~ 10007.5 km.
        """
        dist = _haversine_km(0.0, 0.0, 90.0, 0.0)
        expected = math.pi / 2 * 6371.0
        assert dist == pytest.approx(expected, rel=1e-6)

    def test_negative_latitudes(self):
        """Southern hemisphere coordinates work correctly."""
        dist = _haversine_km(-33.8688, 151.2093, -37.8136, 144.9631)
        # Sydney to Melbourne: ~714 km
        assert dist == pytest.approx(714, rel=0.03)


# ──────────────────────────────────────────────────────────────────────
# _pwl_segments_from_config
# ──────────────────────────────────────────────────────────────────────


class TestPwlSegmentsFromConfig:
    """Tests for _pwl_segments_from_config(dc_config)."""

    def test_pwl_mode_default_segments(self):
        """PWL mode with no pwl_loss_segments returns default 3."""
        cfg = types.SimpleNamespace(loss_model="pwl")
        assert _pwl_segments_from_config(cfg) == 3

    def test_pwl_mode_custom_segments(self):
        """PWL mode with explicit pwl_loss_segments returns that value."""
        cfg = types.SimpleNamespace(loss_model="pwl", pwl_loss_segments=5)
        assert _pwl_segments_from_config(cfg) == 5

    def test_pwl_mode_segments_as_float(self):
        """PWL segments specified as float are converted to int."""
        cfg = types.SimpleNamespace(loss_model="pwl", pwl_loss_segments=4.0)
        result = _pwl_segments_from_config(cfg)
        assert result == 4
        assert isinstance(result, int)

    def test_linear_mode(self):
        """Linear loss model returns -1."""
        cfg = types.SimpleNamespace(loss_model="linear")
        assert _pwl_segments_from_config(cfg) == -1

    def test_none_mode(self):
        """None (lossless) mode returns 0."""
        cfg = types.SimpleNamespace(loss_model="none")
        assert _pwl_segments_from_config(cfg) == 0

    def test_missing_loss_model_defaults_to_pwl(self):
        """Missing loss_model attribute defaults to 'pwl' (returns 3)."""
        cfg = types.SimpleNamespace()
        assert _pwl_segments_from_config(cfg) == 3

    def test_missing_both_attrs_defaults(self):
        """Missing both loss_model and pwl_loss_segments returns default 3."""
        cfg = object()  # no attributes at all
        assert _pwl_segments_from_config(cfg) == 3

    def test_unknown_mode_returns_zero(self):
        """Any unrecognized loss_model string falls to else branch (0)."""
        cfg = types.SimpleNamespace(loss_model="quadratic")
        assert _pwl_segments_from_config(cfg) == 0

    def test_pwl_mode_segments_one(self):
        """Single segment PWL."""
        cfg = types.SimpleNamespace(loss_model="pwl", pwl_loss_segments=1)
        assert _pwl_segments_from_config(cfg) == 1

    def test_pwl_mode_segments_ten(self):
        """Ten segment PWL."""
        cfg = types.SimpleNamespace(loss_model="pwl", pwl_loss_segments=10)
        assert _pwl_segments_from_config(cfg) == 10


# ──────────────────────────────────────────────────────────────────────
# _pwl_segments_from_config_master
# ──────────────────────────────────────────────────────────────────────


class TestPwlSegmentsFromConfigMaster:
    """Tests for _pwl_segments_from_config_master(dc_config)."""

    def test_pwl_mode_default_segments_master(self):
        """PWL mode with no pwl_loss_segments_master returns default 2."""
        cfg = types.SimpleNamespace(loss_model="pwl")
        assert _pwl_segments_from_config_master(cfg) == 2

    def test_pwl_mode_custom_segments_master(self):
        """PWL mode with explicit pwl_loss_segments_master returns that value."""
        cfg = types.SimpleNamespace(loss_model="pwl", pwl_loss_segments_master=4)
        assert _pwl_segments_from_config_master(cfg) == 4

    def test_linear_mode_master(self):
        """Linear loss model returns -1 for master."""
        cfg = types.SimpleNamespace(loss_model="linear")
        assert _pwl_segments_from_config_master(cfg) == -1

    def test_none_mode_master(self):
        """None (lossless) mode returns 0 for master."""
        cfg = types.SimpleNamespace(loss_model="none")
        assert _pwl_segments_from_config_master(cfg) == 0

    def test_missing_loss_model_master(self):
        """Missing loss_model defaults to 'pwl' with master default (2)."""
        cfg = types.SimpleNamespace()
        assert _pwl_segments_from_config_master(cfg) == 2

    def test_master_default_differs_from_operational(self):
        """Master default (2) differs from operational default (3)."""
        cfg = types.SimpleNamespace(loss_model="pwl")
        assert _pwl_segments_from_config_master(cfg) != _pwl_segments_from_config(cfg)
        assert _pwl_segments_from_config_master(cfg) == 2
        assert _pwl_segments_from_config(cfg) == 3

    def test_unknown_mode_returns_zero_master(self):
        """Unrecognized loss_model returns 0 for master too."""
        cfg = types.SimpleNamespace(loss_model="fancy")
        assert _pwl_segments_from_config_master(cfg) == 0

    def test_pwl_mode_segments_master_as_float(self):
        """Master segments specified as float are converted to int."""
        cfg = types.SimpleNamespace(loss_model="pwl", pwl_loss_segments_master=6.0)
        result = _pwl_segments_from_config_master(cfg)
        assert result == 6
        assert isinstance(result, int)


# ──────────────────────────────────────────────────────────────────────
# _RENEWABLE_FUELS
# ──────────────────────────────────────────────────────────────────────


class TestRenewableFuels:
    """Tests for the _RENEWABLE_FUELS set."""

    def test_contains_sun(self):
        assert "Sun" in _RENEWABLE_FUELS

    def test_contains_wind(self):
        assert "Wind" in _RENEWABLE_FUELS

    def test_contains_water(self):
        assert "Water" in _RENEWABLE_FUELS

    def test_contains_otec(self):
        assert "OTEC" in _RENEWABLE_FUELS

    def test_contains_none_string(self):
        assert "None" in _RENEWABLE_FUELS

    def test_does_not_contain_coal(self):
        assert "Coal" not in _RENEWABLE_FUELS

    def test_does_not_contain_natural_gas(self):
        assert "Natural Gas" not in _RENEWABLE_FUELS

    def test_does_not_contain_hydrogen(self):
        assert "Hydrogen" not in _RENEWABLE_FUELS

    def test_does_not_contain_lowercase_sun(self):
        """Membership is case-sensitive."""
        assert "sun" not in _RENEWABLE_FUELS

    def test_is_a_set(self):
        assert isinstance(_RENEWABLE_FUELS, set)

    def test_has_five_members(self):
        assert len(_RENEWABLE_FUELS) == 5


# ──────────────────────────────────────────────────────────────────────
# _method_gated_drops — method-specific solver options must not be sent
# while the controlling LP/QP method is on auto/"choose"
# ──────────────────────────────────────────────────────────────────────


class TestMethodGatedDrops:
    """A HiGHS config that carries PDLP options while the LP method is left on
    'choose' must not forward them — some HiGHS builds reject `pdlp_scaling`
    with UnsupportedAttribute and abort the entire model build (the real
    Hokkaido failure)."""

    def test_choose_drops_all_method_specific(self):
        from esfex.bridge.adapters import _method_gated_drops
        opts = {"solver_method": "choose", "pdlp_scaling": True,
                "simplex_strategy": 1, "presolve": "on"}
        drops = _method_gated_drops(opts, "highs")
        assert "pdlp_scaling" in drops
        assert "simplex_strategy" in drops
        assert "presolve" not in drops  # ungated option is kept

    def test_pdlp_method_keeps_pdlp_options(self):
        from esfex.bridge.adapters import _method_gated_drops
        opts = {"solver_method": "pdlp", "pdlp_scaling": True,
                "simplex_strategy": 1}
        drops = _method_gated_drops(opts, "highs")
        assert "pdlp_scaling" not in drops
        assert "simplex_strategy" in drops  # simplex-only, wrong method

    def test_simplex_method_keeps_simplex_options(self):
        from esfex.bridge.adapters import _method_gated_drops
        opts = {"solver_method": "simplex", "pdlp_scaling": True,
                "simplex_strategy": 1}
        drops = _method_gated_drops(opts, "highs")
        assert "pdlp_scaling" in drops
        assert "simplex_strategy" not in drops

    def test_missing_method_defaults_to_choose(self):
        from esfex.bridge.adapters import _method_gated_drops
        drops = _method_gated_drops({"pdlp_scaling": True}, "highs")
        assert "pdlp_scaling" in drops

    def test_controller_under_solver_attr(self):
        # Controller may be stored under its solver attr ("solver") rather than
        # the GUI key ("solver_method").
        from esfex.bridge.adapters import _method_gated_drops
        drops = _method_gated_drops({"solver": "pdlp", "pdlp_scaling": True},
                                    "highs")
        assert "pdlp_scaling" not in drops

    def test_empty_options_no_drops(self):
        from esfex.bridge.adapters import _method_gated_drops
        assert _method_gated_drops({}, "highs") == set()


class TestMethodGatedDropsCommercialSolvers:
    """Gurobi accepts every parameter whatever the algorithm. A saved Cuba
    config (``Method: -1, Crossover: -1, BarConvTol: 1e-8``) had Crossover and
    BarConvTol dropped ("not currently selected"), and so had a config with
    barrier selected (``Method: 2``): the Studio saves combos as their integer
    value, which never matched the gate's labels."""

    def test_auto_method_keeps_barrier_options(self):
        from esfex.bridge.adapters import _method_gated_drops
        opts = {"Method": -1, "Crossover": 0, "BarConvTol": 1e-8,
                "IterationLimit": 1000}
        assert _method_gated_drops(opts, "gurobi") == set()

    def test_barrier_saved_as_integer_keeps_crossover(self):
        from esfex.bridge.adapters import _method_gated_drops
        drops = _method_gated_drops({"Method": 2, "Crossover": 0, "BarConvTol": 1e-9,
                                     "IterationLimit": 1000}, "gurobi")
        assert "Crossover" not in drops and "BarConvTol" not in drops
        assert "IterationLimit" in drops  # simplex iterations, barrier selected

    def test_simplex_drops_barrier_options(self):
        from esfex.bridge.adapters import _method_gated_drops
        for method in (0, 1, "dual_simplex"):
            drops = _method_gated_drops({"Method": method, "Crossover": 0,
                                         "BarConvTol": 1e-9}, "gurobi")
            assert {"Crossover", "BarConvTol"} <= drops

    def test_cplex_auto_keeps_tolerances(self):
        from esfex.bridge.adapters import _method_gated_drops
        opts = {"CPXPARAM_LPMethod": 0, "CPXPARAM_Barrier_ConvergeTol": 1e-9,
                "CPXPARAM_Simplex_Tolerances_Feasibility": 1e-7}
        assert _method_gated_drops(opts, "cplex") == set()
        opts["CPXPARAM_LPMethod"] = 1  # primal simplex
        assert _method_gated_drops(opts, "cplex") == {
            "CPXPARAM_Barrier_ConvergeTol", "barrier_conv_tol"}

    def test_highs_stays_strict_under_choose(self):
        from esfex.bridge.adapters import _method_gated_drops
        assert "pdlp_scaling" in _method_gated_drops(
            {"solver": "choose", "pdlp_scaling": True}, "highs")


class TestBarrierOptionsUnderAutoMethod:
    def test_crossover_off_with_auto_method(self):
        from esfex.bridge.adapters import _barrier_options_under_auto_method as f
        assert f({"Method": -1, "Crossover": 0}, "gurobi")
        assert f({"method": "auto", "crossover": "off"}, "gurobi")
        assert f({"Crossover": 0}, "gurobi")  # Method left at its default

    def test_no_hint_otherwise(self):
        from esfex.bridge.adapters import _barrier_options_under_auto_method as f
        assert not f({"Method": 2, "Crossover": 0}, "gurobi")     # barrier selected
        assert not f({"Method": -1, "Crossover": -1}, "gurobi")   # crossover on
        assert not f({"Method": -1}, "gurobi")
        assert not f({"Method": -1, "Crossover": 0}, "highs")

    @pytest.mark.julia
    def test_logged_once_and_options_forwarded(self, caplog):
        import logging
        from esfex.bridge.adapters import _solver_options_to_julia, reset_run_warnings
        reset_run_warnings()
        opts = {"Method": -1, "Crossover": 0, "BarConvTol": 1e-9}
        with caplog.at_level(logging.WARNING, logger="esfex.bridge.adapters"):
            d1 = _solver_options_to_julia(opts, "gurobi")
            _solver_options_to_julia(opts, "gurobi")
        assert {str(k) for k in d1.keys()} == {"Method", "Crossover", "BarConvTol"}
        hints = [r for r in caplog.records if "concurrent optimizer" in r.getMessage()]
        assert len(hints) == 1
        assert not any("Dropping solver option" in r.getMessage() for r in caplog.records)
