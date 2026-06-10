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
    allocate_demand_capacitated,
    build_density_features,
    compute_node_areas_km2,
    gdp_density_point,
    sample_node_densities,
    sample_node_gdp_density,
    sample_node_pop_and_area,
    _normalize_ssp,
)


def _gdp_ssp_available() -> bool:
    try:
        return gdp_density_point(23.1, -82.4, 2030, "ssp2") is not None
    except Exception:
        return False


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


class TestCapacitatedAllocation:
    """Capacity-constrained demand→bus transport (substation territories)."""

    def test_conserves_total_demand(self):
        served = allocate_demand_capacitated(
            cell_lats=[0.0, 0.1, 0.2], cell_lons=[0.0, 0.1, 0.2],
            cell_demand=[10.0, 20.0, 30.0],
            bus_lats=[0.0, 0.2], bus_lons=[0.0, 0.2],
            bus_cap=[100.0, 100.0])
        assert served.sum() == pytest.approx(60.0)

    def test_single_bus_gets_all(self):
        served = allocate_demand_capacitated(
            [0.0, 1.0], [0.0, 1.0], [5.0, 7.0],
            [0.5], [0.5], [1000.0])
        assert served.tolist() == pytest.approx([12.0])

    def test_proximity_when_uncapacitated(self):
        # Two far-apart clusters, ample capacity → each bus serves its own.
        served = allocate_demand_capacitated(
            cell_lats=[0.0, 0.0, 10.0, 10.0],
            cell_lons=[0.0, 0.1, 0.0, 0.1],
            cell_demand=[10.0, 10.0, 10.0, 10.0],
            bus_lats=[0.0, 10.0], bus_lons=[0.05, 0.05],
            bus_cap=[1000.0, 1000.0])
        assert served[0] == pytest.approx(20.0, abs=1e-6)
        assert served[1] == pytest.approx(20.0, abs=1e-6)

    def test_spillover_when_nearest_saturates(self):
        # All demand sits next to bus 0, but bus 0 cannot hold it all → the
        # excess must spill to the farther bus 1 (Voronoi could never do this).
        served = allocate_demand_capacitated(
            cell_lats=[0.0, 0.0, 0.0], cell_lons=[0.0, 0.01, 0.02],
            cell_demand=[40.0, 40.0, 40.0],
            bus_lats=[0.0, 5.0], bus_lons=[0.0, 0.0],
            bus_cap=[50.0, 1000.0], headroom=1.0)
        assert served[0] == pytest.approx(50.0, abs=1e-6)
        assert served[1] == pytest.approx(70.0, abs=1e-6)
        assert served.sum() == pytest.approx(120.0)

    def test_empty_buses(self):
        served = allocate_demand_capacitated(
            [0.0], [0.0], [10.0], [], [], [])
        assert served.size == 0

    def test_no_cells(self):
        served = allocate_demand_capacitated(
            [], [], [], [0.0, 1.0], [0.0, 1.0], [10.0, 10.0])
        assert served.tolist() == [0.0, 0.0]


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


class TestCellGrid:
    def test_cell_area_decreases_toward_poles(self):
        from esfex.models.demand_density_ml import cell_area_km2
        eq = float(cell_area_km2(0.0))
        mid = float(cell_area_km2(45.0))
        hi = float(cell_area_km2(70.0))
        # A 0.25° cell at the equator is ~770 km²; it shrinks with |lat|.
        assert 600 < eq < 900
        assert eq > mid > hi > 0

    def test_region_cells_aligned_and_inside_bounds(self):
        from esfex.models.demand_density_ml import region_cells_025
        lats, lons, areas = region_cells_025(bounds=(19.8, -85.0, 23.3, -74.0))
        assert len(lats) == len(lons) == len(areas) > 0
        # Centroids sit on the global 0.25° grid (…, x.125, x.375, …).
        frac = np.modf(np.abs(lats) / 0.25)[0]
        assert np.allclose(frac, 0.5, atol=1e-6)
        assert areas.min() > 0
        # Roughly covers the Cuba bounding box span.
        assert lats.min() >= 19.5 and lats.max() <= 23.6

    def test_region_cells_clipped_to_polygon(self):
        from esfex.models.demand_density_ml import region_cells_025
        # A small triangle → fewer cells than its bounding box.
        poly = [(21.0, -80.0), (21.0, -78.0), (23.0, -79.0)]
        latp, lonp, _ = region_cells_025(polygon_latlon=poly)
        latb, lonb, _ = region_cells_025(bounds=(21.0, -80.0, 23.0, -78.0))
        assert 0 < len(latp) < len(latb)


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
    def test_populated_area_excludes_ocean(self):
        # An ocean-padded bbox: the populated area summed over nodes must be a
        # small fraction of the full region (most of the Cuba bbox is sea), so
        # sparse island/coastal nodes are not over-forecast.
        from esfex.models.demand_density_ml import (
            _region_ring, _polygon_area_km2,
        )
        lats = [23.1, 21.7]; lons = [-82.4, -82.8]  # Havana, Isla de la Juventud
        pa = sample_node_pop_and_area(lats, lons, 2025, bounds=self._CUBA, grid=120)
        assert len(pa) == 2
        pop_area_total = sum(a for _d, a in pa)
        ring, _ml, region_area = _region_ring(lats, lons, None, self._CUBA)
        # Inhabited land is a small slice of the ocean-heavy bbox.
        assert 0 < pop_area_total < 0.5 * region_area
        # Each node has a positive population density.
        assert all(d and d > 0 for d, _a in pa)

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


class TestSSPNormalization:
    def test_normalize_variants(self):
        assert _normalize_ssp("ssp2") == "2"
        assert _normalize_ssp("SSP245") == "2"
        assert _normalize_ssp(3) == "3"
        assert _normalize_ssp("nonsense") == "2"   # default


@pytest.mark.skipif(
    not _gdp_ssp_available(),
    reason="SSP GDP rasters not available",
)
class TestSSPGdpTrajectory:
    _CUBA = (19.8, -85.0, 23.3, -74.0)

    def test_gdp_density_increases_and_interpolates(self):
        v25 = gdp_density_point(23.1, -82.4, 2025, "ssp2")
        v27 = gdp_density_point(23.1, -82.4, 2027, "ssp2")
        v30 = gdp_density_point(23.1, -82.4, 2030, "ssp2")
        # SSP2 GDP grows; an interpolated year sits between its bracketing steps.
        assert v25 < v30
        assert v25 < v27 < v30

    def test_node_gdp_density_differentiated(self):
        out = sample_node_gdp_density(
            [23.1, 20.3], [-82.4, -77.0], 2035, "ssp2", bounds=self._CUBA, grid=50)
        assert all(o and o > 0 for o in out)
        assert out[0] != out[1]

    @pytest.mark.skipif(
        not DemandDensityModel.is_available(), reason="density model not present")
    def test_multi_year_trajectory_evolves_with_socio_and_climate(self):
        # The multi-year demand is the model re-run each year with that year's
        # evolving SSP GDP + population (the trend drivers) and CMIP6 climate
        # (the inter-annual variability). It must NOT be a frozen constant line
        # (the old fixed-rate problem). CMIP6 is fetched live; if the network is
        # unavailable it falls back to the base weather year — the build still
        # produces a valid per-year trajectory either way.
        from esfex.visualization.workflows.demand_estimation_analysis import (
            DemandProfileBuilder, DemandEstimationConfig,
            ProxyData, MacroData, MeteoData,
        )
        years = 4
        cfg = DemandEstimationConfig(
            base_year=2025, simulation_years=years, num_nodes=2,
            national_demand_gwh=0.0, ssp_scenario="ssp2",
        )
        proxy = ProxyData(
            node_lats=[23.1, 20.3], node_lons=[-82.4, -77.0],
            node_names=["Havana", "East"], bounds=self._CUBA,
        )
        macro = MacroData(
            country_iso="CU", gdp_per_capita=9500.0, population=11_000_000.0,
            electric_consumption_kwh_capita=1300.0, electricity_access_pct=100.0,
            urbanization_pct=77.0,
            pop_growth_by_year={2025 + i: -0.003 for i in range(years)},
        )
        meteo = MeteoData(temperature_hourly=list(np.full(8760, 27.0)))
        res = DemandProfileBuilder(cfg).build(proxy, macro, meteo)

        assert res.demand_source == "ml_density"
        lvl = np.asarray(res.level0_annual_mwh_by_year)
        assert len(lvl) == years
        assert np.all(lvl > 0)
        # Country-scale magnitude from the per-cell integral (Cuba ~20 TWh band).
        assert 8_000 < lvl[0] / 1000.0 < 40_000
        # The trajectory is NOT a frozen constant — socio + climate move it.
        assert lvl.std() / lvl.mean() > 1e-4

        # Spatial demand is exposed per cell for the bus-level distribution:
        # cells exist and their base-year demand sums to the node total.
        assert len(res.cell_lats) == len(res.cell_lons) == len(res.cell_annual_mwh) > 0
        cell_gwh = sum(res.cell_annual_mwh) / 1000.0
        node_gwh = float(np.asarray(res.annual_gwh).sum())
        assert cell_gwh == pytest.approx(node_gwh, rel=1e-3)


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

    def test_absolute_demand_from_model_integral_without_override(self):
        # With NO national-demand override, the national total is the SUM of the
        # per-node density×area integrals — the density model is absolute, so no
        # external kWh/capita estimate or calibration is used.
        from esfex.visualization.workflows.demand_estimation_analysis import (
            DemandProfileBuilder, DemandEstimationConfig,
            ProxyData, MacroData, MeteoData,
        )
        nodes = {"Havana": (23.1, -82.4), "Santiago": (20.02, -75.82),
                 "Camaguey": (21.38, -77.92), "SantaClara": (22.4, -79.97)}
        names = list(nodes)
        lats = [nodes[k][0] for k in names]; lons = [nodes[k][1] for k in names]
        n = len(names)
        cfg = DemandEstimationConfig(
            base_year=2025, simulation_years=1, num_nodes=n,
            national_demand_gwh=0.0, ssp_scenario="SSP2",   # no override
        )
        proxy = ProxyData(
            building_weights=[1 / n] * n, population_weights=[1 / n] * n,
            node_lats=lats, node_lons=lons, node_names=names,
            bounds=(19.8, -85.0, 23.3, -74.0),
        )
        macro = MacroData(
            country_iso="CU", gdp_per_capita=9500.0, population=11_000_000.0,
            electric_consumption_kwh_capita=1400.0,
            electricity_access_pct=100.0, urbanization_pct=77.0,
        )
        h = np.arange(8760); temp = 26 + 4 * np.sin(2 * np.pi * (h % 24 - 15) / 24)
        meteo = MeteoData(temperature_hourly=list(temp))
        res = DemandProfileBuilder(cfg).build(proxy, macro, meteo)
        assert res.demand_source == "ml_density"
        # A positive, country-scale total falls out of the integral (Cuba is
        # ~20 TWh / ~3 GW; allow a wide band for model + grid tolerance).
        assert 8_000 < res.total_annual_gwh < 35_000
        assert 1_500 < res.total_peak_mw < 5_000
        # Realistic load factor (not a flat profile).
        lf = res.total_annual_gwh * 1000 / 8760 / res.total_peak_mw
        assert 0.5 < lf < 0.9

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
