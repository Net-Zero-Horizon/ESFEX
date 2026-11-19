"""Demand-model temperature on local civil time (round 31).

The demand models' calendar features (hour of day, day of week, month,
holidays) are the local civil time of the series' location. ERA5 temperature
comes from Open-Meteo in UTC, so it was paired index by index with the wrong
hour: for Cuba (UTC-5) the temperature fed with local hour h was that of local
h-5. The temperature paired with local hour h is now that instant's. The
density model (round 38) is on UTC hours instead: it reads the UTC series.
"""

from __future__ import annotations

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from esfex.utils.model_clock import utc_year_on_local_time  # noqa: E402

HAVANA = (23.13, -82.38)
_JUL1_NOON = 181 * 24 + 12


def test_utc_year_on_local_time_shift_gaps_and_leap_day():
    loc = utc_year_on_local_time(np.arange(8760.0), "America/Havana", 2023)
    assert loc.shape == (8760,)
    assert loc[0] == 5.0 and loc[_JUL1_NOON] == _JUL1_NOON + 4
    # Leap year: 8784 UTC hours → 8760 local hours without 29 February.
    leap = utc_year_on_local_time(np.arange(8784.0), "Asia/Tokyo", 2024)
    assert leap.shape == (8760,)
    feb28_23 = 59 * 24 - 1
    assert leap[feb28_23] == feb28_23 - 9
    assert leap[feb28_23 + 1] == feb28_23 + 1 + 24 - 9     # 1 Mar 00:00 JST
    # Gaps take the mean; a short year is padded.
    vals = [10.0] * 100 + [None] + [10.0] * 8000
    assert np.all(utc_year_on_local_time(vals, "UTC", 2023) == 10.0)
    with pytest.raises(ValueError):
        utc_year_on_local_time([None] * 8760, "UTC", 2023)


def test_meteo_node_temperature_falls_back_to_the_system_series():
    from esfex.visualization.workflows.demand_estimation_analysis import (
        MeteoData, meteo_from_utc_temperature,
    )

    meteo = MeteoData(temperature_hourly=list(np.full(8760, 7.0)))
    assert np.all(meteo.node_temperature(3, 8760) == 7.0)
    assert MeteoData().node_temperature(0, 8760) is None
    # Nothing usable fetched: an empty MeteoData (the engines fall back).
    empty = meteo_from_utc_temperature([None] * 10, 2023, *HAVANA, [23.0], [-82.0])
    assert empty.temperature_hourly == [] and empty.node_temperature_hourly == {}


def test_meteo_shares_one_series_per_zone():
    from esfex.visualization.workflows.demand_estimation_analysis import (
        meteo_from_utc_temperature,
    )

    meteo = meteo_from_utc_temperature(
        np.arange(8760.0), 2023, *HAVANA,
        [23.13, 20.02, 21.38], [-82.38, -75.82, -77.92])
    t = meteo.node_temperature_hourly
    assert t[0] is t[1] is t[2]          # all Cuba: America/Havana
    assert t[0][0] == 5.0


def _rasters_available() -> bool:
    try:
        from esfex.models.demand_density_ml import DemandDensityModel, gdp_density_point
        return (DemandDensityModel.is_available()
                and gdp_density_point(23.1, -82.4, 2030, "ssp2") is not None)
    except Exception:
        return False


@pytest.mark.skipif(not _rasters_available(),
                    reason="needs the SSP GDP rasters on disk")
def test_density_engine_reads_each_nodes_temperature_on_utc(monkeypatch):
    """Without CMIP6, the density engine runs on the base-year temperature of
    each node, not one shared series. The model's hours are UTC (round 38):
    a node's local series is moved to UTC, and a UTC series is used as is."""
    from esfex.models import demand_density_ml as ddm
    from esfex.visualization.workflows.demand_estimation_analysis import (
        DemandEstimationConfig, DemandProfileBuilder, MacroData, MeteoData,
        ProxyData,
    )

    monkeypatch.setattr(ddm, "fetch_cmip6_node_temps",
                        lambda lats, *a, **k: [None] * len(lats))
    seen = []
    real = ddm.predict_region_cell_demand

    def _spy(model, lats, lons, areas, lpd, lgd, lgpc, temp, *a, **k):
        seen.append(np.asarray(temp)[:6].tolist())
        return real(model, lats, lons, areas, lpd, lgd, lgpc, temp, *a, **k)

    monkeypatch.setattr(ddm, "predict_region_cell_demand", _spy)
    hours = np.arange(8760.0)
    meteo = MeteoData(
        temperature_hourly=list(np.full(8760, 20.0)),
        node_temperature_hourly={0: 26.0 + hours / 1e4, 1: np.full(8760, 28.0)},
        year=2024)
    cfg = DemandEstimationConfig(base_year=2025, simulation_years=1, num_nodes=2)
    proxy = ProxyData(node_lats=[23.1, 20.3], node_lons=[-82.4, -77.0],
                      node_names=["W", "E"], bounds=(19.8, -85.0, 23.3, -74.0))
    macro = MacroData(country_iso="CU", gdp_per_capita=9500.0,
                      population=11_000_000.0)
    res = DemandProfileBuilder(cfg).build(proxy, macro, meteo)
    assert res.demand_source == "ml_density"
    # node 0 (UTC-5): UTC 05:00 holds its local midnight; the year wraps
    west = next(t for t in seen if t[0] != 28.0)
    assert west[5] == pytest.approx(26.0) and west[0] == pytest.approx(26.0 + 8755 / 1e4)
    assert [28.0] * 6 in seen
