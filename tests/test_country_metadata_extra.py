"""Additive unit tests for esfex.models.country_metadata.

Targets the pure-Python logic of the fetcher/loader/assembler functions by
mocking out network (requests) and only relying on pandas for CSV parsing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from esfex.models import country_metadata as cm
from esfex.models.country_metadata import (
    COUNTRY_COORDS,
    CountryRecord,
    build_country_registry,
    fetch_all_worldbank,
    load_owid_electricity,
    load_un_wpp,
)


# ── CountryRecord dataclass ─────────────────────────────────────────────

def test_country_record_defaults():
    rec = CountryRecord(iso3="ABC")
    assert rec.iso3 == "ABC"
    assert rec.name == ""
    assert rec.latitude == 0.0
    assert rec.longitude == 0.0
    assert rec.gdp_per_capita == 0.0
    assert rec.population == 0.0
    assert rec.urbanization_pct == 50.0
    assert rec.electricity_access_pct == 100.0
    assert rec.kwh_per_capita == 0.0
    assert rec.annual_gwh == 0.0
    assert rec.data_quality == "unknown"
    assert rec.pop_projections == {}


def test_country_record_projection_dict_independent():
    a = CountryRecord(iso3="A")
    b = CountryRecord(iso3="B")
    a.pop_projections[2030] = 1.0
    assert b.pop_projections == {}


def test_country_coords_structure():
    assert "USA" in COUNTRY_COORDS
    lat, lon = COUNTRY_COORDS["USA"]
    assert isinstance(lat, float) and isinstance(lon, float)


# ── fetch_all_worldbank ─────────────────────────────────────────────────

def test_fetch_worldbank_uses_cache(tmp_path):
    cache_dir = tmp_path / "wb"
    cache_dir.mkdir()
    cached = {"USA": {"population": 331000000.0}}
    (cache_dir / "wb_all.json").write_text(json.dumps(cached))

    out = fetch_all_worldbank(cache_dir)
    assert out == cached


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_fetch_worldbank_paginated_download(tmp_path, monkeypatch):
    cache_dir = tmp_path / "wb"

    # One indicator -> two pages; the rest -> single empty page.
    # The function iterates over 5 indicators, so build per-call responses.
    calls = {"n": 0}

    def fake_get(url, timeout=30):
        calls["n"] += 1
        # First indicator (gdp_per_capita): page 1 has records & 2 pages.
        if "NY.GDP.PCAP.CD" in url and "page=1" in url:
            return _FakeResp([
                {"pages": 2},
                [
                    {"countryiso3code": "USA", "value": 60000.0},
                    {"countryiso3code": "", "value": 1.0},        # skip: no iso
                    {"countryiso3code": "FRA", "value": None},     # skip: None
                ],
            ])
        if "NY.GDP.PCAP.CD" in url and "page=2" in url:
            return _FakeResp([
                {"pages": 2},
                [
                    # USA already present -> branch "name in entry" skipped
                    {"countryiso3code": "USA", "value": 99999.0},
                    {"countryiso3code": "FRA", "value": 45000.0},
                ],
            ])
        # population indicator: empty records list -> break on `if not records`
        if "SP.POP.TOTL" in url:
            return _FakeResp([{"pages": 1}, []])
        # urbanization: payload not a list -> break on isinstance check
        if "SP.URB.TOTL.IN.ZS" in url:
            return _FakeResp({"message": "bad"})
        # electricity_access: raises -> exception branch
        if "EG.ELC.ACCS.ZS" in url:
            raise RuntimeError("network down")
        # kwh_per_capita: single page with one record
        if "EG.USE.ELEC.KH.PC" in url:
            return _FakeResp([{"pages": 1}, [{"countryiso3code": "USA", "value": 12000.0}]])
        return _FakeResp([{"pages": 1}, []])

    import requests
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(cm.time, "sleep", lambda *a, **k: None)

    out = fetch_all_worldbank(cache_dir)

    assert out["USA"]["gdp_per_capita"] == 60000.0  # kept first, not overwritten
    assert out["FRA"]["gdp_per_capita"] == 45000.0
    assert out["USA"]["kwh_per_capita"] == 12000.0
    # Cache written
    assert (cache_dir / "wb_all.json").exists()
    saved = json.loads((cache_dir / "wb_all.json").read_text())
    assert saved["USA"]["gdp_per_capita"] == 60000.0


# ── load_owid_electricity ───────────────────────────────────────────────

def _write_owid_csv(path: Path):
    df = pd.DataFrame(
        {
            "iso_code": ["USA", "USA", "FRA", "OWID_WRL", None],
            "year": [2020, 2021, 2021, 2021, 2021],
            "electricity_generation": [100.0, 200.0, 50.0, 9999.0, 1.0],
        }
    )
    df.to_csv(path, index=False)


def test_load_owid_cached(tmp_path):
    cache_dir = tmp_path / "owid"
    cache_dir.mkdir()
    _write_owid_csv(cache_dir / "owid-energy-data.csv")

    out = load_owid_electricity(cache_dir)
    # Most recent year for USA is 2021 -> 200 TWh -> 200000 GWh
    assert out["USA"] == pytest.approx(200000.0)
    assert out["FRA"] == pytest.approx(50000.0)
    # OWID_WRL aggregate filtered (len != 3); None dropped
    assert "OWID_WRL" not in out


def test_load_owid_zero_generation_filtered(tmp_path):
    cache_dir = tmp_path / "owid"
    cache_dir.mkdir()
    df = pd.DataFrame(
        {
            "iso_code": ["ZZZ"],
            "year": [2021],
            "electricity_generation": [0.0],
        }
    )
    df.to_csv(cache_dir / "owid-energy-data.csv", index=False)
    out = load_owid_electricity(cache_dir)
    assert out == {}


def test_load_owid_downloads_when_missing(tmp_path, monkeypatch):
    cache_dir = tmp_path / "owid"

    csv_bytes = (
        b"iso_code,year,electricity_generation\n"
        b"USA,2021,10.0\n"
    )

    class _Resp:
        content = csv_bytes

        def raise_for_status(self):
            pass

    import requests
    monkeypatch.setattr(requests, "get", lambda url, timeout=60: _Resp())

    out = load_owid_electricity(cache_dir)
    assert out["USA"] == pytest.approx(10000.0)
    assert (cache_dir / "owid-energy-data.csv").exists()


# ── load_un_wpp ─────────────────────────────────────────────────────────

def test_load_un_wpp_standard(tmp_path):
    csv = tmp_path / "wpp.csv"
    df = pd.DataFrame(
        {
            "ISO3_code": ["USA", "USA", "USA", "FRA", "XX", "USA"],
            "Time": [2030, 2050, 2010, 2040, 2030, 2200],
            "PopTotal": [350000.0, 380000.0, 300000.0, 70000.0, 1.0, 9.0],
            "Variant": ["Medium", "Medium", "Medium", "Medium", "Medium", "Medium"],
        }
    )
    df.to_csv(csv, index=False)

    out = load_un_wpp(csv)
    # 2010 excluded (<2020), 2200 excluded (>2100), XX excluded (len!=3)
    assert out["USA"] == {2030: 350000000.0, 2050: 380000000.0}
    assert out["FRA"] == {2040: 70000000.0}
    assert "XX" not in out


def test_load_un_wpp_alt_columns_and_partial_match(tmp_path):
    csv = tmp_path / "wpp2.csv"
    # No standard ISO/pop column names -> exercise partial-match fallbacks.
    df = pd.DataFrame(
        {
            "country_iso3_x": ["DEU"],
            "Year": [2030],
            "my_pop_total_col": [80000.0],
        }
    )
    df.to_csv(csv, index=False)

    out = load_un_wpp(csv)
    assert out["DEU"] == {2030: 80000000.0}


def test_load_un_wpp_bad_year_skipped(tmp_path):
    csv = tmp_path / "wpp3.csv"
    df = pd.DataFrame(
        {
            "ISO3_code": ["USA", "USA"],
            "Time": ["notayear", 2030],
            "PopTotal": [1.0, 350000.0],
        }
    )
    df.to_csv(csv, index=False)
    out = load_un_wpp(csv)
    assert out["USA"] == {2030: 350000000.0}


def test_load_un_wpp_no_iso_column_raises(tmp_path):
    csv = tmp_path / "bad.csv"
    pd.DataFrame({"foo": [1], "bar": [2]}).to_csv(csv, index=False)
    with pytest.raises(ValueError, match="ISO3 column"):
        load_un_wpp(csv)


def test_load_un_wpp_no_pop_column_raises(tmp_path):
    csv = tmp_path / "bad2.csv"
    pd.DataFrame({"ISO3_code": ["USA"], "Time": [2030]}).to_csv(csv, index=False)
    with pytest.raises(ValueError, match="population column"):
        load_un_wpp(csv)


def test_load_un_wpp_empty_result_logs_zero(tmp_path):
    # All rows filtered (years out of range) -> result empty, the min/max
    # log branch uses the `if result else 0` fallback.
    csv = tmp_path / "empty.csv"
    df = pd.DataFrame(
        {"ISO3_code": ["USA"], "Time": [1900], "PopTotal": [1.0]}
    )
    df.to_csv(csv, index=False)
    out = load_un_wpp(csv)
    assert out == {}


# ── build_country_registry ──────────────────────────────────────────────

def test_build_registry_quality_branches(tmp_path, monkeypatch):
    # Mock the three data sources.
    wb = {
        "USA": {  # observed (owid present) -> owid wins
            "population": 331e6, "gdp_per_capita": 60000.0,
            "kwh_per_capita": 12000.0, "urbanization": 80.0,
            "electricity_access": 100.0,
        },
        "FRA": {  # estimated_wb (no owid, has kwh+pop)
            "population": 67e6, "gdp_per_capita": 40000.0,
            "kwh_per_capita": 7000.0, "urbanization": 80.0,
            "electricity_access": 100.0,
        },
        "TCD": {  # estimated_gdp (no owid, no kwh, has gdp+pop)
            "population": 16e6, "gdp_per_capita": 700.0,
            "kwh_per_capita": 0.0,
        },
        "AND": {},  # missing (no owid, no kwh, no gdp) -> defaults
        "ZZZ": {"population": 1.0},  # no coords -> skipped
    }
    owid = {"USA": 4000000.0}
    wpp = {"USA": {2030: 350e6}}

    monkeypatch.setattr(cm, "fetch_all_worldbank", lambda d: wb)
    monkeypatch.setattr(cm, "load_owid_electricity", lambda d: owid)
    monkeypatch.setattr(cm, "load_un_wpp", lambda p: wpp)

    csv = tmp_path / "wpp.csv"
    csv.write_text("dummy")  # exists() True so load_un_wpp branch runs

    reg = build_country_registry(un_wpp_csv=csv, data_dir=tmp_path)

    assert reg["USA"].data_quality == "observed"
    assert reg["USA"].annual_gwh == 4000000.0
    assert reg["USA"].pop_projections == {2030: 350e6}

    assert reg["FRA"].data_quality == "estimated_wb"
    expected_fra = 7000.0 * 67e6 * (100.0 / 100) / 1e6
    assert reg["FRA"].annual_gwh == pytest.approx(expected_fra)
    # defaults applied where indicator absent
    assert reg["FRA"].electricity_access_pct == 100.0

    assert reg["TCD"].data_quality == "estimated_gdp"
    expected_tcd = 700.0 * 16e6 * 0.4 / 1e6
    assert reg["TCD"].annual_gwh == pytest.approx(expected_tcd)
    # missing-indicator defaults
    assert reg["TCD"].urbanization_pct == 50.0
    assert reg["TCD"].electricity_access_pct == 100.0

    assert reg["AND"].data_quality == "missing"
    assert reg["AND"].annual_gwh == 0.0

    # No coords -> excluded
    assert "ZZZ" not in reg


def test_build_registry_no_wpp_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "fetch_all_worldbank", lambda d: {})
    monkeypatch.setattr(cm, "load_owid_electricity", lambda d: {})

    reg = build_country_registry(un_wpp_csv=None, data_dir=tmp_path)
    # Every record comes from COUNTRY_COORDS, all missing quality
    assert len(reg) == len(COUNTRY_COORDS)
    sample = reg["USA"]
    assert sample.pop_projections == {}
    assert sample.data_quality == "missing"
    assert sample.latitude == COUNTRY_COORDS["USA"][0]


def test_build_registry_nonexistent_wpp_path(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "fetch_all_worldbank", lambda d: {})
    monkeypatch.setattr(cm, "load_owid_electricity", lambda d: {})
    # load_un_wpp must NOT be called when path doesn't exist
    monkeypatch.setattr(
        cm, "load_un_wpp",
        lambda p: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    missing = tmp_path / "nope.csv"
    reg = build_country_registry(un_wpp_csv=missing, data_dir=tmp_path)
    assert reg["USA"].pop_projections == {}
