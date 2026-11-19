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
    "latitude", "longitude", "t_mean", "t_range", "hh_elec_share", "bld_share_elec",
    "lhour_sin", "lhour_cos", "smonth_sin", "smonth_cos",
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

    def test_nan_cell_demand_scrubbed(self):
        # A degraded forecast (e.g. CMIP6 rate-limited) can produce NaN per-cell
        # demand. It must not poison the solve / fractions — bad cells count as 0.
        served = allocate_demand_capacitated(
            cell_lats=[0.0, 0.1], cell_lons=[0.0, 0.1],
            cell_demand=[np.nan, 5.0],
            bus_lats=[0.0, 0.2], bus_lons=[0.0, 0.2], bus_cap=[100.0, 100.0])
        assert np.isfinite(served).all()
        assert served.sum() == pytest.approx(5.0)

    def test_inf_and_negative_cell_demand_scrubbed(self):
        served = allocate_demand_capacitated(
            cell_lats=[0.0, 0.1, 0.2], cell_lons=[0.0, 0.1, 0.2],
            cell_demand=[np.inf, -3.0, 7.0],
            bus_lats=[0.0, 0.2], bus_lons=[0.0, 0.2], bus_cap=[100.0, 100.0])
        assert np.isfinite(served).all()
        assert served.sum() == pytest.approx(7.0)

    def test_all_nan_cell_demand_yields_zeros(self):
        served = allocate_demand_capacitated(
            [0.0, 0.1], [0.0, 0.1], [np.nan, np.nan],
            [0.0, 0.2], [0.0, 0.2], [100.0, 100.0])
        assert np.isfinite(served).all()
        assert served.sum() == pytest.approx(0.0)


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
        lats, lons, areas, inside = region_cells_025(bounds=(19.8, -85.0, 23.3, -74.0))
        assert len(lats) == len(lons) == len(areas) > 0
        # Centroids sit on the global 0.25° grid (…, x.125, x.375, …).
        frac = np.modf(np.abs(lats) / 0.25)[0]
        assert np.allclose(frac, 0.5, atol=1e-6)
        assert areas.min() > 0
        assert np.all(inside == 1.0)       # no polygon: every cell whole
        # Roughly covers the Cuba bounding box span.
        assert lats.min() >= 19.5 and lats.max() <= 23.6

    def test_region_cells_clipped_to_polygon(self):
        from esfex.models.demand_density_ml import region_cells_025
        # A small triangle → fewer cells than its bounding box.
        poly = [(21.0, -80.0), (21.0, -78.0), (23.0, -79.0)]
        latp, lonp, _, _ = region_cells_025(polygon_latlon=poly)
        latb, lonb, _, _ = region_cells_025(bounds=(21.0, -80.0, 23.0, -78.0))
        assert 0 < len(latp) < len(latb)


class TestMultiRingDomainClip:
    """Cells and node areas clip to a multi-ring (archipelago) domain, so an
    island nation's border nodes don't absorb a neighbouring country's cells.
    Regression for the Japan demand: western nodes swallowed ~45 M of South
    Korea + ~24 M of China, inflating their load ~10x (Kitakyushu 42 GW)."""

    # Two disjoint 1°-boxes ~10° apart (e.g. two islands), plus the wide gap
    # of open water/foreign land between them.
    ISLAND_A = [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0)]
    ISLAND_B = [(0.0, 10.0), (0.0, 11.0), (1.0, 11.0), (1.0, 10.0)]

    def test_region_cells_clip_to_all_islands_excluding_gap(self):
        from esfex.models.demand_density_ml import region_cells_025
        multi = [self.ISLAND_A, self.ISLAND_B]
        lats, lons, _, _ = region_cells_025(polygon_latlon=multi)
        assert len(lats) > 0
        # Every kept cell lies inside one island box — none in the 1°<lon<10°
        # gap (the "foreign" water/land that the bbox would otherwise include).
        in_a = (lats >= 0) & (lats <= 1) & (lons >= 0) & (lons <= 1)
        in_b = (lats >= 0) & (lats <= 1) & (lons >= 10) & (lons <= 11)
        assert np.all(in_a | in_b)
        assert in_a.any() and in_b.any()   # both islands kept
        # Strictly fewer than the full bounding box (which spans the gap).
        latb, _, _, _ = region_cells_025(bounds=(0.0, 0.0, 1.0, 11.0))
        assert len(lats) < len(latb)

    def test_node_area_partition_respects_multiring(self):
        from esfex.models.demand_density_ml import compute_node_areas_km2
        multi = [self.ISLAND_A, self.ISLAND_B]
        # One node on each island: each gets ~half the (two-island) land area,
        # not a bbox-inflated share spanning the gap.
        areas = compute_node_areas_km2(
            [0.5, 0.5], [0.5, 10.5], polygon_latlon=multi, grid=120)
        assert len(areas) == 2
        assert areas[0] > 0 and areas[1] > 0
        # Total land ≈ two 1°×1° boxes, far less than the 1°×11° bbox.
        bbox_area = compute_node_areas_km2(
            [0.5], [5.5], bounds=(0.0, 0.0, 1.0, 11.0))[0]
        assert sum(areas) < 0.5 * bbox_area


class TestLandOverlapWeight:
    """A cell the region boundary crosses counts with the share of its land
    inside the region, not by whether its centre is inside: coastal cells
    centred at sea were dropped whole (Cuba lost 7 % of its population)."""

    # The 0.25° cell centred at (21.125, -79.875): lat 21.0–21.25, lon -80.0 – -79.75.
    LAT, LON = 21.125, -79.875

    @staticmethod
    def _box(s, w, n, e):
        return [(s, w), (s, e), (n, e), (n, w)]

    def test_coastal_cell_centred_at_sea_gets_its_land_share(self):
        from esfex.models.demand_density_ml import (
            cell_area_km2, land_overlap_weight, region_cells_025)
        # The island covers the western 30 % of the cell; its centre is at sea.
        lats, lons, km2, inside = region_cells_025(
            polygon_latlon=self._box(21.0, -80.0, 21.25, -79.925))
        assert len(lats) == 1
        assert (lats[0], lons[0]) == (self.LAT, self.LON)
        assert inside[0] == pytest.approx(0.3, rel=1e-9)
        cell = float(cell_area_km2(self.LAT))
        # The cell's land is that 30 %: it counts whole.
        assert land_overlap_weight(inside, [0.3 * cell], km2)[0] == pytest.approx(1.0)
        # Half the cell is land, the region covers 30 % of the cell: 0.6 of its land.
        assert land_overlap_weight(inside, [0.5 * cell], km2)[0] == pytest.approx(0.6)
        # No land in the grid: the fraction of the cell inside.
        assert land_overlap_weight(inside, [0.0], km2)[0] == pytest.approx(0.3)

    def test_interior_cell_weight_is_one(self):
        from esfex.models.demand_density_ml import land_overlap_weight, region_cells_025
        lats, lons, km2, inside = region_cells_025(
            polygon_latlon=self._box(20.0, -81.0, 22.0, -79.0))
        whole = (lats > 20.0) & (lats < 22.0) & (lons > -81.0) & (lons < -79.0)
        assert whole.sum() == 64 and len(lats) == 64
        assert np.all(inside == 1.0)
        # The grid's land area of a whole land cell can exceed the computed
        # cell area by rounding, and a lake makes it smaller: the weight is 1.
        land = km2 * np.where(np.arange(len(km2)) % 2, 1.0000028, 0.7)
        assert np.all(land_overlap_weight(inside, land, km2) == 1.0)

    def test_border_cell_split_between_neighbours(self):
        from esfex.models.demand_density_ml import (
            cell_area_km2, land_overlap_weight, region_cells_025)
        cell = float(cell_area_km2(self.LAT))
        land = 0.8 * cell          # 30 % country A, 50 % country B, 20 % sea
        a = region_cells_025(polygon_latlon=self._box(21.0, -80.5, 21.25, -79.925))
        b = region_cells_025(polygon_latlon=self._box(21.0, -79.925, 21.25, -79.8))

        def weight(res):
            lats, lons, km2, inside = res
            k = np.where((lats == self.LAT) & (lons == self.LON))[0]
            assert k.size == 1      # the shared cell is in both countries
            return land_overlap_weight(inside[k], [land], km2[k])[0]

        wa, wb = weight(a), weight(b)
        assert wa == pytest.approx(0.375)
        assert wb == pytest.approx(0.625)
        assert wa + wb == pytest.approx(1.0)

    def test_node_areas_count_coastal_samples(self):
        # A large island and a thin one (lat 1.46–1.5) that no sample point
        # falls strictly inside with grid=16 (rows every 0.1°): the thin
        # island's node must still get its share of the area (0.04 / 0.84),
        # not the one-sample floor.
        multi = [self._box(0.0, 0.0, 0.8, 1.0), self._box(1.46, 0.0, 1.5, 1.0)]
        areas = compute_node_areas_km2([0.4, 1.48], [0.5, 0.5],
                                       polygon_latlon=multi, grid=16)
        assert areas[1] / sum(areas) == pytest.approx(0.04 / 0.84, rel=0.02)


class TestDensityFeatures:
    def test_shape_and_order(self):
        temp = np.full(8760, 28.0)
        X = build_density_features(
            latitude=21.0, longitude=-80.0,
            log_pop_density=1.8, log_gdp_density=6.0, log_gdp_per_cap=4.0,
            temperature_hourly=temp, base_year=2025,
            feature_order=_FEATURE_ORDER, country_iso="CU",
            t_mean=25.0, t_range=6.0, hh_elec_share=0.74, bld_share_elec=0.6,
        )
        assert X.shape == (8760, 26)
        assert not np.isnan(X).any()
        # socio columns are constant; temperature column matches input
        assert np.allclose(X[:, 0], 1.8)
        assert np.allclose(X[:, 3], 28.0)
        # cyclical hour features stay in [-1, 1]
        assert X[:, 6].min() >= -1.0001 and X[:, 6].max() <= 1.0001

    def test_values_are_not_clipped(self):
        # As in the article, predictors outside the training range are passed
        # as they are (a tree model is flat beyond its outermost split).
        X = build_density_features(
            latitude=21.0, longitude=-80.0, log_pop_density=5.0,
            log_gdp_density=6.0, log_gdp_per_cap=4.0,
            temperature_hourly=np.full(8760, 49.0), base_year=2025,
            feature_order=_FEATURE_ORDER)
        assert np.all(X[:, 0] == 5.0) and np.all(X[:, 3] == 49.0)

    def test_partial_nan_temperature_scrubbed(self):
        # A full-length temperature array carrying NaN (e.g. the ERA5 fallback
        # after a CMIP6 fetch failure) passes the length guard but must not leak
        # NaN into HDD/CDD and the feature matrix.
        temp = np.full(8760, 28.0)
        temp[100:5000] = np.nan
        X = build_density_features(
            latitude=21.0, longitude=-80.0, log_pop_density=1.8,
            log_gdp_density=6.0, log_gdp_per_cap=4.0,
            temperature_hourly=temp, base_year=2025,
            feature_order=_FEATURE_ORDER, country_iso="CU")
        assert not np.isnan(X[:, 3:6]).any()

    def test_all_nan_temperature_falls_back(self):
        temp = np.full(8760, np.nan)
        X = build_density_features(
            latitude=21.0, longitude=-80.0, log_pop_density=1.8,
            log_gdp_density=6.0, log_gdp_per_cap=4.0,
            temperature_hourly=temp, base_year=2025,
            feature_order=_FEATURE_ORDER, country_iso="CU")
        assert not np.isnan(X[:, 3:6]).any()
        assert np.allclose(X[:, 3], 20.0)   # temperature column → 20 °C fallback


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
        _clip, _bbox, _ml, region_area = _region_ring(lats, lons, None, self._CUBA)
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
    not (DemandDensityModel.is_available() and _pop_raster_available()
         and _gdp_ssp_available()),
    reason="needs the population and SSP GDP rasters on disk (the model ships)",
)
class TestDensityModelEndToEnd:
    def test_model_predicts_positive_density(self):
        model = DemandDensityModel.load_bundled()
        assert len(model.feature_names) == 26
        X = build_density_features(
            latitude=21.0, longitude=-80.0,
            log_pop_density=1.8, log_gdp_density=6.0, log_gdp_per_cap=4.0,
            temperature_hourly=np.full(8760, 28.0), base_year=2025,
            feature_order=model.feature_names, country_iso="CU",
            t_mean=25.0, t_range=6.0, hh_elec_share=0.74, bld_share_elec=0.6,
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


def test_missing_density_model_is_reported(monkeypatch):
    # Without its density model the forecast used to degrade silently to a
    # trend with no spatial cells; it now says why, and the Grid Builder
    # shows it in the forecast and in step 3.
    from esfex.visualization.workflows.demand_estimation_analysis import (
        DemandEstimationConfig, DemandProfileBuilder, MacroData, MeteoData, ProxyData,
    )
    monkeypatch.setattr(DemandDensityModel, "is_available", staticmethod(lambda: False))
    cfg = DemandEstimationConfig(base_year=2025, simulation_years=1, num_nodes=2,
                                 national_demand_gwh=20000.0, ml_engine="auto")
    proxy = ProxyData(node_lats=[21.0, 22.5], node_lons=[-82.0, -80.0],
                      node_names=["A", "B"], bounds=(19.8, -85.0, 23.3, -74.0))
    macro = MacroData(country_iso="CU", gdp_per_capita=9500.0, population=11_000_000.0)
    meteo = MeteoData(temperature_hourly=list(np.full(8760, 27.0)))
    res = DemandProfileBuilder(cfg).build(proxy, macro, meteo)
    assert res.demand_source != "ml_density"
    assert not res.cell_annual_mwh
    assert any(w.startswith("Density model not found") for w in res.warnings)

    pytest.importorskip("PySide6.QtWidgets")
    from esfex.paths import BUNDLED_MODELS_DIR
    from esfex.visualization.i18n import get_language, init_i18n
    from esfex.visualization.workflows.grid_mapping_steps import GridMappingDemandStep
    lang = get_language()
    init_i18n("en")
    try:
        text = GridMappingDemandStep._density_model_missing_text(res)
    finally:
        init_i18n(lang)
    assert "reinstall" in text and str(BUNDLED_MODELS_DIR) in text
