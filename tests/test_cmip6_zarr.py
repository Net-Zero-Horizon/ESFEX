"""CMIP6 climate via the local Google Cloud Zarr backend.

The SSP the user picks in Grid Builder step 3 must drive the climate (not just
GDP/population). The Zarr backend honours it; the fetcher prefers it and falls
back to Open-Meteo. These tests avoid the network — the live store is exercised
by a marked, opt-in test.
"""
from __future__ import annotations

import pytest

import esfex.models.demand_density_ml as ddm
from esfex.models.cmip6_zarr import scenario_for_ssp


class TestSspMapping:
    def test_ssp_to_scenario(self):
        assert scenario_for_ssp("SSP1") == "ssp126"
        assert scenario_for_ssp("SSP2") == "ssp245"
        assert scenario_for_ssp("ssp2") == "ssp245"
        assert scenario_for_ssp("SSP245") == "ssp245"
        assert scenario_for_ssp(3) == "ssp370"
        assert scenario_for_ssp("SSP4") == "ssp460"   # Tier-2 pairing
        assert scenario_for_ssp("SSP5") == "ssp585"


class TestCellToHourly:
    def test_kelvin_to_celsius_per_year_hourly(self):
        pytest.importorskip("xarray")
        pytest.importorskip("pandas")
        import numpy as np
        import pandas as pd
        import xarray as xr
        from esfex.models.cmip6_zarr import _cell_to_hourly

        time = pd.date_range("2025-01-01", "2026-12-31", freq="D")
        ds = xr.Dataset({
            "tasmax": ("time", np.full(len(time), 300.0)),   # 26.85 °C
            "tasmin": ("time", np.full(len(time), 290.0)),   # 16.85 °C
        }, coords={"time": time})

        out = _cell_to_hourly(ds, 2025, 2026)
        assert set(out) == {2025, 2026}
        for yr in (2025, 2026):
            h = out[yr]
            assert h.shape == (8760,)
            # Hourly swings between the daily min/max (Kelvin correctly → °C).
            assert 16.0 < float(h.mean()) < 27.0
            assert float(h.min()) >= 16.0 and float(h.max()) <= 27.0


class TestFetchDispatch:
    """fetch_cmip6_node_temps prefers the scenario-consistent Zarr backend when
    an SSP is supplied, and falls back to Open-Meteo otherwise."""

    def test_prefers_zarr_when_ssp_given(self, monkeypatch):
        import esfex.models.cmip6_zarr as zmod

        marker = [{2025: "z0"}, {2025: "z1"}]
        called = {"zarr": 0, "openmeteo": 0}

        def fake_zarr(lats, lons, by, yrs, ssp, model=None):
            called["zarr"] += 1
            return marker

        def fake_point(*a, **k):
            called["openmeteo"] += 1
            return {2025: "om"}

        monkeypatch.setattr(zmod, "fetch_cmip6_zarr_node_temps", fake_zarr)
        monkeypatch.setattr(ddm, "_cmip6_point_hourly", fake_point)

        out = ddm.fetch_cmip6_node_temps([35.0, 34.0], [139.0, 135.0],
                                         2025, 1, ssp="SSP2")
        assert out is marker
        assert called["zarr"] == 1
        assert called["openmeteo"] == 0     # Open-Meteo never touched

    def test_falls_back_to_openmeteo_when_zarr_returns_none(self, monkeypatch):
        import esfex.models.cmip6_zarr as zmod
        import time as _t

        monkeypatch.setattr(zmod, "fetch_cmip6_zarr_node_temps",
                            lambda *a, **k: None)   # zarr unavailable
        monkeypatch.setattr(_t, "sleep", lambda *a, **k: None)
        seen = []

        def fake_point(lat, lon, s, e, model=ddm._CMIP6_MODEL):
            seen.append((lat, lon))
            return {2025: "om"}

        monkeypatch.setattr(ddm, "_cmip6_point_hourly", fake_point)
        out = ddm.fetch_cmip6_node_temps([35.0], [139.0], 2025, 1, ssp="SSP2")
        assert out == [{2025: "om"}]
        assert len(seen) == 1               # Open-Meteo path used

    def test_no_ssp_skips_zarr_entirely(self, monkeypatch):
        import esfex.models.cmip6_zarr as zmod
        import time as _t

        def boom(*a, **k):
            raise AssertionError("Zarr must not be called without an SSP")

        monkeypatch.setattr(zmod, "fetch_cmip6_zarr_node_temps", boom)
        monkeypatch.setattr(_t, "sleep", lambda *a, **k: None)
        monkeypatch.setattr(ddm, "_cmip6_point_hourly",
                            lambda *a, **k: {2025: "om"})
        out = ddm.fetch_cmip6_node_temps([35.0], [139.0], 2025, 1)  # ssp=None
        assert out == [{2025: "om"}]
