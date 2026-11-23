"""A demand cell is shared among the nodes whose people live in it.

The density model predicts demand per 0.25° cell (about 27 km). Each cell went
whole to the node nearest to its centre, so nodes closer together than a cell
got no demand and the node that took the cell carried its neighbours'.
"""
from __future__ import annotations

import numpy as np
import pytest

from esfex.models.demand_density_ml import (
    DemandDensityModel,
    _ssp_gdp_total_nearest,
    split_cells_among_nodes,
)


def _gdp_ssp_available() -> bool:
    try:
        return _ssp_gdp_total_nearest(23.1, -82.4, 2030, "ssp2") is not None
    except Exception:
        return False


def _pixels(pop_at):
    """A cell cut into a 10 x 10 grid whose weights come from *pop_at(lat, lon)*."""
    def fn(lat, lon):
        off = ((np.arange(10) + 0.5) / 10 - 0.5) * 0.25
        la, lo = np.meshgrid(lat + off, lon + off, indexing="ij")
        la, lo = la.ravel(), lo.ravel()
        return la, lo, np.array([pop_at(a, b) for a, b in zip(la, lo)], dtype=float)
    return fn


def test_two_nodes_in_one_cell_share_it_by_population():
    # One cell centred at (18.125, -77.125); a node west of the centre, one east.
    # Three quarters of the people live in the eastern half.
    share, plat, plon, by_area = split_cells_among_nodes(
        [18.125], [-77.125], [18.125, 18.125], [-77.20, -77.05],
        pixels_fn=_pixels(lambda la, lo: 3.0 if lo > -77.125 else 1.0))
    assert by_area == 0
    assert share.shape == (1, 2)
    assert share[0].sum() == pytest.approx(1.0)
    assert share[0, 0] == pytest.approx(0.25)       # the node nearest to the centre no longer takes it all
    assert share[0, 1] == pytest.approx(0.75)
    assert plon[0, 0] < -77.125 < plon[0, 1]        # each part sits on its own side


def test_a_node_far_from_every_person_gets_nothing():
    share, plat, plon, _ = split_cells_among_nodes(
        [18.125], [-77.125], [18.125, 30.0], [-77.125, -60.0],
        pixels_fn=_pixels(lambda la, lo: 1.0))
    assert share[0, 0] == pytest.approx(1.0) and share[0, 1] == 0.0
    assert np.isnan(plat[0, 1])


def test_a_cell_without_population_pixels_is_split_by_area():
    share, _, _, by_area = split_cells_among_nodes(
        [18.125], [-77.125], [18.125, 18.125], [-77.20, -77.05],
        pixels_fn=lambda lat, lon: None)
    assert by_area == 1
    assert share[0] == pytest.approx([0.5, 0.5])    # the boundary between the nodes is the cell's middle


def test_people_outside_the_region_do_not_count():
    # The region is the cell's western half: the eastern node takes only the
    # western pixels that are nearer to it than to the western node.
    region = [(18.0, -77.25), (18.25, -77.25), (18.25, -77.125), (18.0, -77.125)]
    inside, _, _, _ = split_cells_among_nodes(
        [18.125], [-77.125], [18.125, 18.125], [-77.20, -77.05],
        polygon_latlon=region, pixels_fn=_pixels(lambda la, lo: 100.0 if lo > -77.125 else 1.0))
    whole, _, _, _ = split_cells_among_nodes(
        [18.125], [-77.125], [18.125, 18.125], [-77.20, -77.05],
        pixels_fn=_pixels(lambda la, lo: 100.0 if lo > -77.125 else 1.0))
    assert whole[0, 1] > 0.98                       # the crowded east dominates without the region
    assert inside[0, 0] == pytest.approx(1.0)       # with it, only the west counts


def test_small_shares_are_dropped_and_the_row_still_sums_to_one():
    share, _, _, _ = split_cells_among_nodes(
        [18.125], [-77.125], [18.125, 18.125], [-77.20, -77.05],
        pixels_fn=_pixels(lambda la, lo: 1000.0 if lo < -77.125 else 0.001))
    assert share[0, 1] == 0.0 and share[0, 0] == pytest.approx(1.0)


@pytest.mark.skipif(not _gdp_ssp_available() or not DemandDensityModel.is_available(),
                    reason="density model or SSP GDP rasters not present")
def test_forecast_gives_demand_to_two_nodes_of_the_same_cell():
    from esfex.visualization.workflows.demand_estimation_analysis import (
        DemandEstimationConfig,
        DemandProfileBuilder,
        MacroData,
        MeteoData,
        ProxyData,
    )
    # Two nodes 14 km apart in one city and one far to the east: the first two
    # fall in the same cell.
    cfg = DemandEstimationConfig(base_year=2025, simulation_years=1, num_nodes=3,
                                 national_demand_gwh=0.0, ssp_scenario="ssp2")
    proxy = ProxyData(node_lats=[23.023, 23.098, 20.3], node_lons=[-82.258, -82.366, -77.0],
                      node_names=["City west", "City east", "East"], bounds=(19.8, -85.0, 23.3, -74.0))
    macro = MacroData(country_iso="CU", gdp_per_capita=9500.0, population=11_000_000.0,
                      electric_consumption_kwh_capita=1300.0, electricity_access_pct=100.0,
                      urbanization_pct=77.0, pop_growth_by_year={2025: 0.0})
    res = DemandProfileBuilder(cfg).build(proxy, macro, MeteoData(temperature_hourly=list(np.full(8760, 27.0))))
    gwh = np.asarray(res.annual_gwh)
    assert np.all(gwh > 0)                          # the first node used to get 0
    # The parts handed to the bus distribution name their node and add up to it.
    assert len(res.cell_nodes) == len(res.cell_annual_mwh) == len(res.cell_lats)
    for nd in range(3):
        part = sum(m for m, n in zip(res.cell_annual_mwh, res.cell_nodes) if n == nd) / 1000.0
        assert part == pytest.approx(gwh[nd], rel=1e-3)
