"""Additive coverage tests for esfex.models.hazard_assessment.

This file complements tests/test_hazard_assessment_cov.py by exercising the
network-backed fetcher code paths *with mocked transports* (urllib /
_api_get_json), so the real parsing / estimation / caching branches execute
without touching live endpoints.  It also fills in a handful of remaining
pure-logic branches (copula fallback, screening classification, scenario
reduction edge cases, SLR query interpolation, etc.).

Nothing here contacts the network: every fetcher is given a temp cache_dir
and its transport monkeypatched.  Assertions reflect the module's actual
observed behavior.
"""
from __future__ import annotations

import io
import json
import urllib.request

import numpy as np
import pytest

from esfex.models import hazard_assessment as ha
from esfex.models.hazard_assessment import (
    CompositeRiskAssessment,
    CycloneFetcher,
    FloodFetcher,
    FragilityLibrary,
    HazardIntensityMap,
    NodeRiskProfile,
    ScenarioGenerator,
    ScreeningFetcher,
    SeaLevelFetcher,
    SeismicFetcher,
    TsunamiFetcher,
    VolcanicFetcher,
    WildfireFetcher,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeResp:
    """Context-manager stand-in for urllib.request.urlopen's return value."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def _patch_urlopen(monkeypatch, payload: bytes):
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: _FakeResp(payload)
    )


def _progress_collector():
    events: list[tuple[int, str]] = []
    return events, lambda pct, msg: events.append((pct, msg))


# ===========================================================================
# _api_get_json — success path (existing tests only cover the failure path)
# ===========================================================================

def test_api_get_json_success(monkeypatch):
    _patch_urlopen(monkeypatch, json.dumps({"hello": "world"}).encode())
    data = ha._api_get_json("https://example.com/x")
    assert data == {"hello": "world"}


# ===========================================================================
# _fit_gumbel_return_periods — fit path and method-of-moments fallback
# ===========================================================================

def test_fit_gumbel_fit_path_runs():
    maxima = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    out = ha._fit_gumbel_return_periods(maxima, [100, 500])
    assert set(out) == {100, 500}
    # Larger return period -> at-least-as-large value
    assert out[500] >= out[100]


def test_fit_gumbel_moment_fallback(monkeypatch):
    # Force gumbel_r.fit to raise so the method-of-moments branch runs.
    import scipy.stats as ss

    monkeypatch.setattr(
        ss.gumbel_r, "fit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    out = ha._fit_gumbel_return_periods([1.0, 2.0, 3.0, 4.0], [50, 200])
    assert set(out) == {50, 200}
    assert all(isinstance(v, float) for v in out.values())


# ===========================================================================
# HazardFetcher caching (_save_cache / _load_cache round trip + bad json)
# ===========================================================================

def test_cache_roundtrip_and_corrupt(tmp_path):
    f = SeismicFetcher(cache_dir=str(tmp_path))
    f._save_cache("k1", {"a": 1})
    assert f._load_cache("k1") == {"a": 1}
    # Missing key returns None
    assert f._load_cache("nope") is None
    # Corrupt the cache file -> _load_cache swallows the error, returns None
    p = f._cache_path("k1")
    p.write_text("{ not json")
    assert f._load_cache("k1") is None


def test_base_fetcher_fetch_not_implemented():
    with pytest.raises(NotImplementedError):
        ha.HazardFetcher().fetch([(0.0, 0.0)])


# ===========================================================================
# SeismicFetcher — USGS path with mocked GeoJSON + ISC fallback
# ===========================================================================

def test_seismic_usgs_with_events(monkeypatch, tmp_path):
    geo = {
        "features": [
            {"properties": {"mag": 6.5},
             "geometry": {"coordinates": [-76.0, 20.0, 10.0]}},
            {"properties": {"mag": 5.0},
             "geometry": {"coordinates": [-76.1, 20.1, 12.0]}},
            {"properties": {"mag": 5.5},
             "geometry": {"coordinates": [-75.9, 19.9, 8.0]}},
        ]
    }
    _patch_urlopen(monkeypatch, json.dumps(geo).encode())
    f = SeismicFetcher(cache_dir=str(tmp_path), source="usgs")
    events, cb = _progress_collector()
    hmap = f.fetch([(20.0, -76.0)], [475], on_progress=cb)
    assert hmap.hazard_type == "earthquake"
    assert hmap.intensity_measure == "PGA"
    # 3 events -> no ISC fallback; PGA computed > 0
    assert hmap.node_intensities[0][475] > 0
    assert events  # progress fired


def test_seismic_usgs_triggers_isc_fallback(monkeypatch, tmp_path):
    # USGS returns < 3 events -> ISC fallback is queried.
    _patch_urlopen(monkeypatch, json.dumps({"features": []}).encode())
    isc_rows = [(19.5, -75.5, 6.0), (21.0, -77.0, 6.2)]
    monkeypatch.setattr(
        SeismicFetcher, "_query_isc_catalog",
        staticmethod(lambda lat, lon, **k: isc_rows),
    )
    f = SeismicFetcher(cache_dir=str(tmp_path), source="usgs")
    hmap = f.fetch([(20.0, -76.0)], [475])
    assert hmap.node_intensities[0][475] >= 0


def test_seismic_usgs_uses_cache(monkeypatch, tmp_path):
    f = SeismicFetcher(cache_dir=str(tmp_path), source="usgs")
    # Pre-seed cache so the network path is skipped.
    f._save_cache("usgs_v2_pga_20.000_-76.000", {"475": 0.42})

    def _boom(*a, **k):
        raise AssertionError("network should not be called")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    hmap = f.fetch([(20.0, -76.0)], [475])
    assert hmap.node_intensities[0][475] == 0.42


def test_seismic_query_isc_catalog_parses_text(monkeypatch):
    text = (
        "#comment\n"
        "EventID|x|lat|lon|d|d|d|d|d|d|mag|extra\n"
        "1|t|19.5|-75.5|x|x|x|x|x|x|6.1|z\n"
        "2|t|bad|-75.5|x|x|x|x|x|x|6.1|z\n"  # bad lat -> skipped
    )
    _patch_urlopen(monkeypatch, text.encode())
    out = SeismicFetcher._query_isc_catalog(20.0, -76.0)
    assert out == [(19.5, -75.5, 6.1)]


def test_seismic_isc_source_fetch(monkeypatch, tmp_path):
    text = (
        "EventID|x|lat|lon|d|d|d|d|d|d|mag|e\n"
        "1|t|19.0|-75.0|x|x|x|x|x|x|6.4|z\n"
    )
    _patch_urlopen(monkeypatch, text.encode())
    f = SeismicFetcher(cache_dir=str(tmp_path), source="isc")
    hmap = f.fetch([(20.0, -76.0)], [475])
    assert hmap.source == "isc"
    assert hmap.node_intensities[0][475] > 0


def test_seismic_isc_source_network_error(monkeypatch, tmp_path):
    def _boom(*a, **k):
        raise OSError("down")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    f = SeismicFetcher(cache_dir=str(tmp_path), source="isc")
    hmap = f.fetch([(20.0, -76.0)], [475])
    # On failure intensities default to 0.0
    assert hmap.node_intensities[0][475] == 0.0


def test_seismic_estimate_pga_return_period_scaling():
    events = [(20.0, -76.0, 6.0)]
    out = SeismicFetcher._estimate_pga(events, 20.0, -76.0, [100, 475, 2475])
    assert out[2475] > out[475] > out[100]


# ===========================================================================
# CycloneFetcher — CSV and ERDDAP paths
# ===========================================================================

def test_cyclone_csv_download_and_assess(monkeypatch, tmp_path):
    # Minimal IBTrACS CSV: header, units row, then data rows.
    csv_text = (
        "SID,LAT,LON,WMO_WIND\n"
        "units,deg,deg,kts\n"
        "S1,20.0,-76.0,90\n"
        "S1,20.1,-76.1,85\n"
        "S2,20.0,-76.0,100\n"
    )
    _patch_urlopen(monkeypatch, csv_text.encode())
    f = CycloneFetcher(cache_dir=str(tmp_path), source="ibtracs")
    events, cb = _progress_collector()
    hmap = f.fetch([(20.0, -76.0)], [100, 500], on_progress=cb)
    assert hmap.hazard_type == "cyclone"
    assert hmap.metadata["basin"] in {"NA", "EP", "ALL", "WP", "NI", "SI", "SP", "SA"}
    assert 0 in hmap.node_intensities


def test_cyclone_csv_download_failure_empty(monkeypatch, tmp_path):
    def _boom(*a, **k):
        raise OSError("no net")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    f = CycloneFetcher(cache_dir=str(tmp_path), source="ibtracs")
    hmap = f.fetch([(20.0, -76.0)], [100])
    # No tracks -> zero winds
    assert hmap.node_intensities[0][100] == 0.0


def test_cyclone_csv_short_response(monkeypatch, tmp_path):
    # Fewer than 3 lines -> _download_ibtracs_basin returns []
    _patch_urlopen(monkeypatch, b"SID,LAT,LON,WMO_WIND\n")
    f = CycloneFetcher(cache_dir=str(tmp_path), source="ibtracs")
    hmap = f.fetch([(20.0, -76.0)], [100])
    assert hmap.node_intensities[0][100] == 0.0


def test_cyclone_erddap_path(monkeypatch, tmp_path):
    table = {
        "table": {
            "rows": [
                ["S1", 20.0, -76.0, 90.0],
                ["S1", 20.05, -76.05, 95.0],
                ["S2", 20.0, -76.0, 60.0],
                ["bad"],  # too short -> skipped
            ]
        }
    }
    monkeypatch.setattr(ha, "_api_get_json", lambda url, timeout=30: table)
    f = CycloneFetcher(cache_dir=str(tmp_path), source="ibtracs_erddap")
    hmap = f.fetch([(20.0, -76.0)], [100, 500])
    assert hmap.source == "ibtracs_erddap"
    assert 0 in hmap.node_intensities


def test_cyclone_winds_to_rp_extrapolation_branch():
    # Few winds + a large RP forces the Gumbel-extrapolation else-branch.
    out = CycloneFetcher._winds_to_rp([50.0, 40.0], [1000], n_years=2)
    assert 1000 in out and isinstance(out[1000], float)


# ===========================================================================
# FloodFetcher — recent and historical via mocked Open-Meteo
# ===========================================================================

def _flood_payload():
    return {
        "daily": {
            "time": ["2020-01-01", "2020-06-01", "2021-01-01", "2021-06-01"],
            "river_discharge": [100.0, 150.0, 120.0, 200.0],
        }
    }


def test_flood_recent_with_data(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ha, "_api_get_json", lambda url, timeout=30: _flood_payload()
    )
    f = FloodFetcher(cache_dir=str(tmp_path), source="open_meteo")
    events, cb = _progress_collector()
    hmap = f.fetch([(20.0, -76.0)], [100, 500], on_progress=cb)
    assert hmap.intensity_measure == "depth"
    assert 0 in hmap.node_intensities
    assert events


def test_flood_historical_path(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ha, "_api_get_json", lambda url, timeout=30: _flood_payload()
    )
    f = FloodFetcher(cache_dir=str(tmp_path), source="open_meteo_historical")
    hmap = f.fetch([(20.0, -76.0)], [100])
    assert hmap.source == "open_meteo_historical"


def test_flood_query_no_daily_key(monkeypatch, tmp_path):
    monkeypatch.setattr(ha, "_api_get_json", lambda url, timeout=30: {"x": 1})
    f = FloodFetcher(cache_dir=str(tmp_path))
    out = f._query_open_meteo(20.0, -76.0, [100], past_days=730)
    assert out == {100: 0.0}


def test_flood_query_empty_annual_maxima(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ha, "_api_get_json",
        lambda url, timeout=30: {"daily": {"time": [], "river_discharge": []}},
    )
    f = FloodFetcher(cache_dir=str(tmp_path))
    out = f._query_open_meteo(20.0, -76.0, [100], start_date="1984-01-01", end_date="2025-12-31")
    assert out == {100: 0.0}


def test_flood_discharge_to_depth_zero_guard():
    assert FloodFetcher._discharge_to_depth(0.0, 10.0) == 0.0
    assert FloodFetcher._discharge_to_depth(10.0, 0.0) == 0.0


# ===========================================================================
# TsunamiFetcher — runups and events
# ===========================================================================

def test_tsunami_runups(monkeypatch, tmp_path):
    payload = {"items": [{"maxWaterHeight": 3.2}, {"runup": 1.0}]}
    monkeypatch.setattr(ha, "_api_get_json", lambda url, timeout=20: payload)
    f = TsunamiFetcher(cache_dir=str(tmp_path), source="noaa_runups")
    hmap = f.fetch([(20.0, -76.0)], [500])
    assert hmap.intensity_measure == "runup_height"
    assert hmap.node_intensities[0][500] == 3.2


def test_tsunami_events(monkeypatch, tmp_path):
    payload = [{"waterHeight": 5.0}, {"maxRunup": 2.0}]
    monkeypatch.setattr(ha, "_api_get_json", lambda url, timeout=20: payload)
    f = TsunamiFetcher(cache_dir=str(tmp_path), source="noaa_events")
    hmap = f.fetch([(20.0, -76.0)], [500])
    assert hmap.intensity_measure == "wave_height"
    assert hmap.node_intensities[0][500] == 5.0


def test_tsunami_extract_max_height_handles_bad_values():
    items = [{"h": "not-a-number"}, {"h": 4.0}, {"h": None}]
    assert TsunamiFetcher._extract_max_height(items, ["h"]) == 4.0


# ===========================================================================
# WildfireFetcher — FIRMS CSV count
# ===========================================================================

def test_wildfire_fetch_counts_fires(monkeypatch, tmp_path):
    csv = "header\n" + "\n".join("row" for _ in range(30))
    _patch_urlopen(monkeypatch, csv.encode())
    f = WildfireFetcher(cache_dir=str(tmp_path))
    events, cb = _progress_collector()
    hmap = f.fetch([(20.0, -76.0)], [100], on_progress=cb)
    assert hmap.intensity_measure == "FWI"
    # 30 fire rows -> FWI = 30/10 = 3.0
    assert hmap.node_intensities[0][100] == 3.0


def test_wildfire_fetch_network_error(monkeypatch, tmp_path):
    def _boom(*a, **k):
        raise OSError("down")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    f = WildfireFetcher(cache_dir=str(tmp_path))
    hmap = f.fetch([(20.0, -76.0)], [100])
    assert hmap.node_intensities[0][100] == 0.0


# ===========================================================================
# VolcanicFetcher — GVP and NCEI
# ===========================================================================

def test_volcanic_gvp(monkeypatch, tmp_path):
    payload = {
        "features": [
            {"geometry": {"coordinates": [-76.0, 20.2]},
             "properties": {"Maximum_VEI": 4}},
            {"geometry": {"coordinates": [-76.0, 20.2]},
             "properties": {}},  # no VEI -> defaults to 0
            {"geometry": {}},    # malformed -> skipped
        ]
    }
    monkeypatch.setattr(ha, "_api_get_json", lambda url, timeout=60: payload)
    f = VolcanicFetcher(cache_dir=str(tmp_path), source="gvp")
    hmap = f.fetch([(20.0, -76.0)], [500])
    assert hmap.intensity_measure == "ashfall_thickness"
    assert "volcanoes_total" in hmap.metadata
    assert hmap.node_intensities[0][500] >= 0


def test_volcanic_ncei(monkeypatch, tmp_path):
    payload = {"items": [
        {"latitude": 20.2, "longitude": -76.0, "vei": 5},
        {"latitude": "bad", "longitude": -76.0, "vei": 2},  # skipped
    ]}
    monkeypatch.setattr(ha, "_api_get_json", lambda url, timeout=20: payload)
    f = VolcanicFetcher(cache_dir=str(tmp_path), source="noaa_ncei")
    hmap = f.fetch([(20.0, -76.0)], [500])
    assert hmap.source == "noaa_ncei"
    assert 0 in hmap.node_intensities


def test_volcanic_ashfall_clamps_and_radius():
    f = VolcanicFetcher(cache_dir="")
    # A far-away volcano (> search radius) contributes nothing.
    out = f._ashfall_for_node(0.0, 0.0, [(60.0, 60.0, 6)], [500], search_radius_km=50.0)
    assert out[500] == 0.0
    # Close, high-VEI volcano contributes ashfall.
    out2 = f._ashfall_for_node(20.0, -76.0, [(20.0, -76.0, 5)], [500])
    assert out2[500] > 0


# ===========================================================================
# SeaLevelFetcher — NOAA SLR query path (success + None fallback)
# ===========================================================================

def test_sealevel_noaa_query_match_year(monkeypatch, tmp_path):
    payload = {"projections": [
        {"scenario": "Intermediate", "projectionYear": 2050, "projectionRsl": 24.0},
        {"scenario": "Intermediate", "projectionYear": 2100, "projectionRsl": 56.0},
    ]}
    monkeypatch.setattr(ha, "_api_get_json", lambda url, timeout=20: payload)
    f = SeaLevelFetcher(cache_dir=str(tmp_path), source="noaa_slr")
    hmap = f.fetch([(20.0, -76.0)], ssp="ssp245", year=2050)
    # 24 cm -> 0.24 m exact-year match
    assert hmap.node_intensities[0][0] == pytest.approx(0.24, abs=1e-6)


def test_sealevel_noaa_query_interpolates(monkeypatch):
    payload = {"projections": [
        {"scenario": "High", "year": 2050, "slr": 30.0},
        {"scenario": "High", "year": 2100, "slr": 80.0},
    ]}
    monkeypatch.setattr(ha, "_api_get_json", lambda url, timeout=20: payload)
    f = SeaLevelFetcher(cache_dir="")
    val = f._query_noaa_slr(20.0, -76.0, "High", 2075)
    # Halfway between 0.30 and 0.80 -> 0.55
    assert val == pytest.approx(0.55, abs=1e-6)


def test_sealevel_noaa_query_no_projections_returns_none(monkeypatch):
    monkeypatch.setattr(ha, "_api_get_json", lambda url, timeout=20: {})
    f = SeaLevelFetcher(cache_dir="")
    assert f._query_noaa_slr(20.0, -76.0, "High", 2050) is None


def test_sealevel_noaa_fetch_falls_back_to_ar6(monkeypatch, tmp_path):
    # _query_noaa_slr returns None -> AR6 interpolation used.
    monkeypatch.setattr(SeaLevelFetcher, "_query_noaa_slr", lambda self, *a, **k: None)
    f = SeaLevelFetcher(cache_dir=str(tmp_path), source="noaa_slr")
    hmap = f.fetch([(20.0, -76.0)], ssp="ssp245", year=2050)
    assert hmap.node_intensities[0][0] > 0


def test_sealevel_noaa_query_extrapolate_below_first(monkeypatch):
    payload = {"projections": [
        {"scenario": "Low", "year": 2050, "slr": 20.0},
        {"scenario": "Low", "year": 2100, "slr": 40.0},
    ]}
    monkeypatch.setattr(ha, "_api_get_json", lambda url, timeout=20: payload)
    f = SeaLevelFetcher(cache_dir="")
    val = f._query_noaa_slr(20.0, -76.0, "Low", 2030)
    assert val is not None and val >= 0


# ===========================================================================
# ScreeningFetcher — full classification path with patched sub-fetchers
# ===========================================================================

def test_screening_classifies_levels(monkeypatch, tmp_path):
    # Patch create_fetcher to return a stub whose .fetch yields high IMs so
    # the classification branches (high/medium/low/very_low) all run.
    class _Stub:
        def __init__(self, haz, im):
            self._haz = haz
            self._im = im

        def fetch(self, coords, rps):
            return HazardIntensityMap(
                hazard_type=self._haz, source="stub",
                intensity_measure="x", units="x",
                return_periods=rps,
                node_intensities={0: {rps[0]: self._im}},
            )

    # Map each hazard to an IM that lands in a different category.
    im_by_haz = {
        "earthquake": 0.5,   # >= high (0.30) -> level 1
        "cyclone": 20.0,     # >= medium (18) -> level 2
        "flood": 0.1,        # >= low (0.05) -> level 3
        "tsunami": 0.0,      # < low -> level 4
        "wildfire": 40.0,    # high
        "volcanic": 0.5,     # >= low (0.1) -> level 3
        "sea_level_rise": 0.0,
    }

    def _fake_create(haz, source=""):
        return _Stub(haz, im_by_haz[haz])

    monkeypatch.setattr(ha, "create_fetcher", _fake_create)
    f = ScreeningFetcher(cache_dir=str(tmp_path))
    events, cb = _progress_collector()
    hmap = f.fetch([(20.0, -76.0)], on_progress=cb)
    levels = hmap.node_intensities[0]
    keys = ScreeningFetcher._HAZARD_KEYS
    assert levels[keys["earthquake"]] == 1.0
    assert levels[keys["cyclone"]] == 2.0
    assert levels[keys["flood"]] == 3.0
    assert levels[keys["tsunami"]] == 4.0
    assert "raw_im" in hmap.metadata
    assert events


def test_screening_handles_fetcher_exception(monkeypatch, tmp_path):
    def _fake_create(haz, source=""):
        raise RuntimeError("boom")

    monkeypatch.setattr(ha, "create_fetcher", _fake_create)
    f = ScreeningFetcher(cache_dir=str(tmp_path))
    hmap = f.fetch([(20.0, -76.0)])
    # All hazards failed -> every level is Very Low (4.0)
    assert all(v == 4.0 for v in hmap.node_intensities[0].values())


# ===========================================================================
# Composite copula fallback + remaining branches
# ===========================================================================

def test_copula_fallback_on_cdf_failure(monkeypatch):
    cra = CompositeRiskAssessment(combination_method="copula")
    import scipy.stats as ss

    # Make multivariate_normal raise so the except-branch runs.
    monkeypatch.setattr(
        ss, "multivariate_normal",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bad cov")),
    )
    out = cra.combine_hazards({"a": 0.2, "b": 0.3})
    # Fallback = independent combination = 1 - (0.8*0.7) = 0.44
    assert out == pytest.approx(0.44, abs=1e-9)


def test_combine_gaussian_copula_empty_and_single():
    cra = CompositeRiskAssessment(combination_method="copula")
    assert cra._combine_gaussian_copula({}) == 0.0
    assert cra._combine_gaussian_copula({"a": 0.4}) == 0.4


def test_compute_risk_coefficients_missing_profile_and_no_probs():
    cra = CompositeRiskAssessment()
    # Profile exists for node 0 but has no failure probs for the component.
    prof = NodeRiskProfile(
        node_index=0, coordinates=(0.0, 0.0),
        hazard_intensities={}, component_failure_probs={"solar_pv": {}},
        composite_risk=0.0, expected_annual_loss=0.0, dominant_hazard="none",
    )
    gen_map = {"g_known": (0, "solar_pv"), "g_missing": (99, "solar_pv")}
    bat_map = {"b_known": (0, "battery"), "b_missing": (99, "battery")}
    gen_c, bat_c = cra.compute_risk_coefficients([prof], gen_map, bat_map)
    # Empty probs and missing profile both yield full coefficient 1.0
    assert gen_c["g_known"] == 1.0
    assert gen_c["g_missing"] == 1.0
    assert bat_c["b_known"] == 1.0
    assert bat_c["b_missing"] == 1.0


def test_compute_risk_coefficients_with_real_probs():
    cra = CompositeRiskAssessment()
    prof = NodeRiskProfile(
        node_index=0, coordinates=(0.0, 0.0),
        hazard_intensities={},
        component_failure_probs={"solar_pv": {"earthquake": 0.2}},
        composite_risk=0.2, expected_annual_loss=0.0, dominant_hazard="earthquake",
    )
    gen_c, bat_c = cra.compute_risk_coefficients([prof], {"g": (0, "solar_pv")})
    assert gen_c["g"] == pytest.approx(0.8, abs=1e-4)
    assert bat_c == {}


def test_compute_technology_risk_coefficients_branches():
    cra = CompositeRiskAssessment()
    prof = NodeRiskProfile(
        node_index=1, coordinates=(0.0, 0.0),
        hazard_intensities={},
        component_failure_probs={"battery": {"flood": 0.3}},
        composite_risk=0.3, expected_annual_loss=0.0, dominant_hazard="flood",
    )
    coeffs = cra.compute_technology_risk_coefficients([prof], "battery", n_nodes=3)
    assert len(coeffs) == 3
    assert coeffs[0] == 1.0      # node 0 has no profile
    assert coeffs[1] == pytest.approx(0.7, abs=1e-4)  # node 1 has probs
    assert coeffs[2] == 1.0      # node 2 has no profile


# ===========================================================================
# ScenarioGenerator — importance no-hazard skip + build_scenario_tree edge
# ===========================================================================

def test_scenario_importance_skips_node_without_hazards():
    gen = ScenarioGenerator(seed=1)
    # composite_risk > 0 so it can be sampled, but no hazard_intensities.
    prof = NodeRiskProfile(
        node_index=0, coordinates=(0.0, 0.0),
        hazard_intensities={}, component_failure_probs={},
        composite_risk=0.5, expected_annual_loss=0.0, dominant_hazard="none",
    )
    scenarios = gen.generate_hazard_scenarios(
        [prof], {"g": (0, "solar_pv")}, n_scenarios=3, method="importance",
    )
    # Only the baseline (no disaster) remains.
    assert all(s["hazard_type"] == "" for s in scenarios)


def test_build_scenario_tree_remaining_clamped_to_one():
    gen = ScenarioGenerator(seed=1)
    climate = [{"name": f"c{i}", "probability": 0.1} for i in range(5)]
    hazard = [
        {"name": "baseline_no_disaster", "probability": 0.5},
        {"name": "h1", "probability": 0.3},
        {"name": "h2", "probability": 0.2},
    ]
    # max_scenarios < len(climate) forces remaining < 1 -> clamped to 1.
    red_climate, red_hazard = gen.build_scenario_tree(climate, hazard, max_scenarios=3)
    assert red_climate is climate
    assert len(red_hazard) == 1
    assert red_hazard[0]["probability"] == pytest.approx(1.0, abs=1e-6)


def test_build_scenario_tree_redistributes_to_baseline():
    gen = ScenarioGenerator(seed=1)
    climate = [{"name": "c0", "probability": 1.0}]
    hazard = [
        {"name": "baseline_no_disaster", "probability": 0.6},
        {"name": "h1", "probability": 0.25},
        {"name": "h2", "probability": 0.1},
        {"name": "h3", "probability": 0.05},
    ]
    red_climate, red_hazard = gen.build_scenario_tree(climate, hazard, max_scenarios=3)
    names = {s["name"] for s in red_hazard}
    assert "baseline_no_disaster" in names
    assert abs(sum(s["probability"] for s in red_hazard) - 1.0) < 1e-6


def test_scenario_lhs_no_active_profiles_returns_empty():
    gen = ScenarioGenerator(seed=1)
    prof = NodeRiskProfile(
        node_index=0, coordinates=(0.0, 0.0),
        hazard_intensities={"flood": {100: 1.0}}, component_failure_probs={},
        composite_risk=0.0, expected_annual_loss=0.0, dominant_hazard="none",
    )
    out = gen._generate_lhs_scenarios([prof], {"g": (0, "solar_pv")}, {}, 4)
    assert out == []


# ===========================================================================
# ResilienceAnalyzer — redundancy / rto branches with scenarios
# ===========================================================================

def test_resilience_metrics_with_scenarios_redundancy_rto():
    analyzer = ha.ResilienceAnalyzer()
    profiles = [
        NodeRiskProfile(
            node_index=0, coordinates=(0.0, 0.0),
            hazard_intensities={}, component_failure_probs={},
            composite_risk=0.1, expected_annual_loss=0.0, dominant_hazard="flood",
        )
    ]
    scenarios = [
        {"name": "baseline_no_disaster", "probability": 0.7,
         "damage_fraction": {}, "recovery_hours": 0},
        {"name": "big", "probability": 0.3,
         "damage_fraction": {"g": 0.8}, "recovery_hours": 100},
    ]
    metrics = analyzer.compute_metrics(
        profiles, scenarios, total_demand_mwh=8760.0,
        total_capacity_mw=100.0, n_generators=4,
    )
    # RTO is the worst-case recovery_hours.
    assert metrics.rto_hours == 100.0
    # One of two scenarios has < 0.5 max damage -> redundancy 0.5.
    assert metrics.redundancy_index == pytest.approx(0.5, abs=1e-6)
    assert metrics.scenario_eens is not None
