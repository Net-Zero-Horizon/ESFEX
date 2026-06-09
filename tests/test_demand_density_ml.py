# -*- coding: utf-8 -*-
"""Tests for the spatial demand-density model integration.

The density model predicts hourly MW/km² per node so the per-node forecast is
genuinely spatial instead of collapsing to total/num_nodes. These tests cover
the area partition, the feature builder, and (when the model file is present)
the end-to-end build producing a DIFFERENTIATED, calibrated per-node demand.
"""

from __future__ import annotations

import numpy as np
import pytest

from esfex.models.demand_density_ml import (
    DemandDensityModel,
    build_density_features,
    compute_node_areas_km2,
    sample_node_densities,
)


def _pop_raster_available() -> bool:
    """True if the GPW population-density raster can be sampled."""
    try:
        from esfex.models.pixel_features import sample_pop_density
        return sample_pop_density(23.1, -82.4, 2020) is not None
    except Exception:
        return False

_FEATURE_ORDER = [
    "log_pop_density", "log_gdp_density", "log_gdp_per_cap",
    "temperature", "hdd", "cdd",
    "hour_sin", "hour_cos", "dow_sin", "dow_cos", "month_sin", "month_cos",
    "is_weekend", "is_holiday", "days_to_next_holiday", "days_from_prev_holiday",
    "latitude", "longitude",
]


class TestNodeAreas:
    def test_single_node_gets_whole_region(self):
        areas = compute_node_areas_km2([21.0], [-80.0],
                                       bounds=(20.0, -84.0, 23.0, -74.0))
        assert len(areas) == 1 and areas[0] > 0

    def test_areas_sum_to_region(self):
        bounds = (20.0, -84.0, 23.0, -74.0)
        one = compute_node_areas_km2([21.5], [-79.0], bounds=bounds)[0]
        areas = compute_node_areas_km2(
            [21.0, 22.0, 21.5], [-82.0, -76.0, -79.0], bounds=bounds)
        assert len(areas) == 3
        assert sum(areas) == pytest.approx(one, rel=0.02)  # same total region

    def test_interior_node_gets_smaller_cell(self):
        # Three colinear, evenly-spaced nodes in a wide box: the middle node's
        # Voronoi cell is bounded by both bisectors (narrow), while the outer
        # nodes' cells extend to the box edges (wide).
        bounds = (15.0, -10.0, 25.0, 20.0)
        areas = compute_node_areas_km2(
            [20.0, 20.0, 20.0], [0.0, 5.0, 10.0], bounds=bounds)
        assert areas[1] < areas[0] and areas[1] < areas[2]

    def test_polygon_overrides_bounds(self):
        # A small triangle polygon → smaller total area than the wide bbox.
        poly = [(21.0, -80.0), (21.0, -79.0), (22.0, -79.5)]
        a_poly = compute_node_areas_km2([21.3], [-79.5], polygon_latlon=poly)[0]
        a_bbox = compute_node_areas_km2([21.3], [-79.5],
                                        bounds=(20.0, -84.0, 23.0, -74.0))[0]
        assert 0 < a_poly < a_bbox


class TestDensityFeatures:
    def test_shape_and_order(self):
        temp = np.full(8760, 28.0)
        X = build_density_features(
            latitude=21.0, longitude=-80.0,
            log_pop_density=1.8, log_gdp_density=6.0, log_gdp_per_cap=4.0,
            temperature_hourly=temp, base_year=2025,
            feature_order=_FEATURE_ORDER, country_iso="CU",
        )
        assert X.shape == (8760, 18)
        assert not np.isnan(X).any()
        # socio columns are constant; temperature column matches input
        assert np.allclose(X[:, 0], 1.8)
        assert np.allclose(X[:, 3], 28.0)
        # cyclical hour features stay in [-1, 1]
        assert X[:, 6].min() >= -1.0001 and X[:, 6].max() <= 1.0001

    def test_clips_to_training_ranges(self):
        temp = np.full(8760, 20.0)
        X = build_density_features(
            latitude=21.0, longitude=-80.0,
            log_pop_density=99.0,   # absurd → clipped
            log_gdp_density=6.0, log_gdp_per_cap=4.0,
            temperature_hourly=temp, base_year=2025,
            feature_order=_FEATURE_ORDER,
            train_ranges={"log_pop_density": [0.3, 3.85]},
        )
        assert X[:, 0].max() <= 3.85 + 1e-9


@pytest.mark.skipif(
    not _pop_raster_available(),
    reason="GPW population-density raster not available",
)
class TestRasterSampling:
    _CUBA = (19.8, -85.0, 23.3, -74.0)

    def test_pop_density_sampled_per_cell(self):
        out = sample_node_densities(
            [23.1, 20.3], [-82.4, -77.0], year=2025, bounds=self._CUBA, grid=60)
        assert len(out) == 2 and all(o is not None for o in out)
        # Dense (Havana) cell vs sparser eastern cell → different densities.
        assert out[0]["pop_density"] != out[1]["pop_density"]
        assert all(o["pop_density"] > 0 for o in out)

    @pytest.mark.skipif(
        not DemandDensityModel.is_available(),
        reason="density model not present",
    )
    def test_raster_breaks_even_split_with_equal_weights(self):
        # With equal proxy weights the population-derived path collapses to an
        # exact even split (total/num_nodes); raster-sampled density must not.
        from esfex.visualization.workflows.demand_estimation_analysis import (
            DemandProfileBuilder, DemandEstimationConfig,
            ProxyData, MacroData, MeteoData,
        )
        cfg = DemandEstimationConfig(
            base_year=2025, simulation_years=1, num_nodes=2,
            national_demand_gwh=20000.0,
        )
        proxy = ProxyData(
            node_lats=[23.1, 20.3], node_lons=[-82.4, -77.0],
            node_names=["Havana", "East"],
            population_weights=[0.5, 0.5], building_weights=[0.5, 0.5],
            bounds=self._CUBA,
        )
        macro = MacroData(
            country_iso="CU", gdp_per_capita=9500.0, population=11_000_000.0,
            electric_consumption_kwh_capita=1300.0,
            electricity_access_pct=100.0, urbanization_pct=77.0,
        )
        meteo = MeteoData(temperature_hourly=list(np.full(8760, 27.0)))
        res = DemandProfileBuilder(cfg).build(proxy, macro, meteo)
        assert res.demand_source == "ml_density"
        # NOT an exact 10000/10000 even split.
        assert abs(res.annual_gwh[0] - res.annual_gwh[1]) > 100.0


@pytest.mark.skipif(
    not DemandDensityModel.is_available(),
    reason="demand_density_hourly_xgb.json not present in MODELS_DIR",
)
class TestDensityModelEndToEnd:
    def test_model_predicts_positive_density(self):
        model = DemandDensityModel.load_bundled()
        assert len(model.feature_names) == 18
        X = build_density_features(
            latitude=21.0, longitude=-80.0,
            log_pop_density=1.8, log_gdp_density=6.0, log_gdp_per_cap=4.0,
            temperature_hourly=np.full(8760, 28.0), base_year=2025,
            feature_order=model.feature_names, country_iso="CU",
            train_ranges=model.train_ranges,
        )
        dens = model.predict_density_mw_per_km2(X)
        assert dens.shape == (8760,)
        assert np.all(dens > 0)

    def test_build_density_is_differentiated_and_calibrated(self):
        from esfex.visualization.workflows.demand_estimation_analysis import (
            DemandProfileBuilder, DemandEstimationConfig,
            ProxyData, MacroData, MeteoData,
        )
        cfg = DemandEstimationConfig(
            base_year=2025, simulation_years=1, num_nodes=2,
            national_demand_gwh=20000.0,
        )
        proxy = ProxyData(
            node_lats=[21.0, 22.5], node_lons=[-82.0, -80.0],
            node_names=["A", "B"],
            population_weights=[0.7, 0.3], building_weights=[0.7, 0.3],
            bounds=(19.8, -85.0, 23.3, -74.0),
        )
        macro = MacroData(
            country_iso="CU", gdp_per_capita=9500.0, population=11_000_000.0,
            electric_consumption_kwh_capita=1300.0,
            electricity_access_pct=100.0, urbanization_pct=77.0,
        )
        meteo = MeteoData(temperature_hourly=list(np.full(8760, 27.0)))
        res = DemandProfileBuilder(cfg).build(proxy, macro, meteo)

        assert res.demand_source == "ml_density"
        # National total calibrated to the macro estimate.
        assert res.total_annual_gwh == pytest.approx(20000.0, rel=1e-3)
        # Per-node demand is NOT an even split (that would be 10000/10000 GWh).
        assert abs(res.annual_gwh[0] - res.annual_gwh[1]) > 1000.0
        # Hourly profiles differ between nodes.
        d = res.demand
        assert np.any(np.abs(d[:, 0] - d[:, 1]) > 1e-6)
