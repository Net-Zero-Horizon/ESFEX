"""The demand training dataset on one clock (round 31).

The parquets were labelled UTC, but each source delivered demand on its own
clock (OPSD in UTC, TEPCO and ONS in local time, ...) and the ERA5
temperature came in UTC; the training features read every row as a local
hour. Every parquet now holds demand and temperature on the local civil time
of the series' location, with tz-aware local timestamps.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from esfex.models import demand_dataset as ds
from esfex.models import demand_real_data as rd

_JUL1_NOON = 181 * 24 + 12
_FEB29 = 59 * 24


def test_pad_or_trim_drops_feb29_of_a_leap_year():
    arr = rd._pad_or_trim(np.arange(8784.0), year=2020)
    assert len(arr) == 8760
    assert arr[_FEB29] == _FEB29 + 24          # 1 Mar follows 28 Feb
    assert arr[-1] == 8783                     # 31 Dec kept
    # Without a year (or a short series), the old truncation.
    assert rd._pad_or_trim(np.arange(8784.0))[-1] == 8759
    assert len(rd._pad_or_trim(np.arange(8740.0), year=2020)) == 8760


def test_every_fetcher_declares_its_time_base():
    from zoneinfo import ZoneInfo

    for source, base in rd.SOURCE_TIME_BASES.items():
        if base != "UTC":
            ZoneInfo(base)
    for country in ("DEU", "JPN", "BRA", "AUS", "TUR", "TWN", "CHL"):
        assert country in rd.COUNTRY_COORDS


def test_on_local_time_shifts_utc_sources_only():
    utc = {"DEU": {2019: np.arange(8760.0)}}
    loc = rd.on_local_time(utc, "UTC", rd.COUNTRY_COORDS)["DEU"][2019]
    assert loc[1] == 0.0                       # 01:00 CET = 00:00 UTC
    assert loc[_JUL1_NOON] == _JUL1_NOON - 2   # CEST, UTC+2
    tokyo = {"JPN": {2019: np.arange(8760.0)}}
    same = rd.on_local_time(tokyo, "Asia/Tokyo", rd.COUNTRY_COORDS)
    assert np.array_equal(same["JPN"][2019], np.arange(8760.0))
    assert rd.on_local_time(utc, None, rd.COUNTRY_COORDS) is utc


def _only(**fetchers):
    flags = {k: False for k in (
        "include_opsd", "include_entsoe", "include_brazil", "include_colombia",
        "include_japan", "include_rte", "include_uk", "include_eskom",
        "include_australia", "include_usa", "include_turkey", "include_chile",
        "include_argentina", "include_mexico", "include_korea",
        "include_taiwan", "include_peru")}
    flags.update(fetchers)
    return flags


def test_fetch_all_real_load_returns_local_time(monkeypatch, tmp_path):
    monkeypatch.setattr(rd, "fetch_opsd",
                        lambda d: {"DEU": {2019: np.arange(8760.0)}})
    monkeypatch.setattr(rd, "fetch_tepco_japan",
                        lambda d: {"JPN": {2019: np.arange(8760.0)}})
    data, meta = rd.fetch_all_real_load(
        cache_dir=tmp_path, **_only(include_opsd=True, include_japan=True))
    assert data["DEU"][2019][_JUL1_NOON] == _JUL1_NOON - 2
    assert np.array_equal(data["JPN"][2019], np.arange(8760.0))
    assert meta["DEU"]["time_zone"] == "Europe/Berlin"
    assert meta["JPN"]["time_zone"] == "Asia/Tokyo"


def test_build_dataset_writes_local_time_parquets(monkeypatch, tmp_path):
    demand = np.arange(8760.0) + 1000.0        # already local (fetch_all)

    def _fetch_all(**kw):
        return ({"CUB": {2023: demand}},
                {"CUB": {"lat": 23.1, "lon": -82.4,
                         "time_zone": "America/Havana"}})

    monkeypatch.setattr(rd, "fetch_all_real_load", _fetch_all)
    monkeypatch.setattr(ds, "_fetch_era5_year",
                        lambda lat, lon, yr, d, zone=None: np.arange(8760.0))
    monkeypatch.setattr(ds.time, "sleep", lambda s: None)
    manifest = ds.build_dataset(cache_dir=tmp_path, sources=["all"])
    assert manifest["entries"][0]["time_zone"] == "America/Havana"

    df = pd.read_parquet(tmp_path / "CUB" / "CUB_2023.parquet")
    assert str(df["timestamp"].dt.tz) == "America/Havana"
    assert df["timestamp"].iloc[0] == pd.Timestamp("2023-01-01 00:00",
                                                   tz="America/Havana")
    assert np.array_equal(df["demand_mw"].values, demand)
    # The temperature of local hour h is that of the same instant (UTC h+5,
    # h+4 in summer time).
    t = df["temperature_c"].values
    assert t[0] == 5.0 and t[_JUL1_NOON] == _JUL1_NOON + 4
    assert ds.load_country_year("CUB", 2023, tmp_path)[1][0] == 5.0


def test_zonal_parquet_converts_the_source_clock(tmp_path):
    # IESO: EST all year → Toronto civil time; temperature UTC → same hours.
    est = np.arange(8760.0)
    ds.write_zonal_parquet("CAN", "Toronto", 2023, est, np.arange(8760.0),
                           tmp_path, lat=43.65, lon=-79.38,
                           time_base="Etc/GMT+5")
    df = pd.read_parquet(tmp_path / "CAN" / "CAN_Toronto_2023.parquet")
    assert str(df["timestamp"].dt.tz) == "America/Toronto"
    d, t = df["demand_mw"].values, df["temperature_c"].values
    assert d[0] == 0.0 and t[0] == 5.0
    assert d[_JUL1_NOON] == _JUL1_NOON - 1       # 12:00 EDT = 11:00 EST
    assert t[_JUL1_NOON] == _JUL1_NOON + 4       # 12:00 EDT = 16:00 UTC


def test_leap_year_era5_keeps_dec31_and_drops_feb29(tmp_path):
    utc = np.arange(8784.0)
    ds.write_zonal_parquet("JPN", "Tokyo", 2020, np.ones(8760), utc, tmp_path,
                           lat=35.7, lon=139.7, time_base="Asia/Tokyo")
    t = pd.read_parquet(tmp_path / "JPN" / "JPN_Tokyo_2020.parquet")[
        "temperature_c"].values
    assert len(t) == 8760
    assert t[_FEB29] == _FEB29 + 24 - 9          # 1 Mar 00:00 JST
    assert t[-1] == 8783 - 9                     # 31 Dec 23:00 JST


def test_era5_fetch_keeps_the_whole_utc_year(monkeypatch, tmp_path):
    import sys
    from unittest import mock

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"hourly": {"temperature_2m": list(range(8784))}}

    fake = mock.MagicMock()
    fake.get.return_value = _Resp()
    with mock.patch.dict(sys.modules, {"requests": fake}):
        arr = ds._fetch_era5_year(35.7, 139.7, 2020, tmp_path, zone="Kanto")
    assert len(arr) == 8784
    assert (tmp_path / "era5_utc_Kanto_2020.npy").exists()


def test_training_temperature_is_the_parquets_local_series(monkeypatch, tmp_path):
    from esfex.models import pixel_features as pf

    ds.write_zonal_parquet("CAN", "Toronto", 2023, np.ones(8760),
                           np.arange(8760.0), tmp_path, lat=43.65, lon=-79.38,
                           time_base="Etc/GMT+5")
    monkeypatch.setattr(pf, "DEMAND_DATASET_DIR", tmp_path)
    monkeypatch.setattr(pf, "ERA5_CACHE", tmp_path / "_era5")
    t = pf.load_temperature("CAN", 43.65, -79.38, 2023, zone="Toronto")
    assert t[0] == 5.0 and t[_JUL1_NOON] == _JUL1_NOON + 4
    # No parquet: the UTC cache is put on the local time of the point.
    (tmp_path / "_era5" / "CAN").mkdir(parents=True)
    np.save(tmp_path / "_era5" / "CAN" / "era5_utc_Ottawa_2023.npy",
            np.arange(8760.0))
    t = pf.load_temperature("CAN", 45.42, -75.70, 2023, zone="Ottawa")
    assert t[0] == 5.0
    assert pf.load_temperature("CAN", 45.42, -75.70, 2023, zone="X") is None


@pytest.mark.parametrize("year", [2023, 2024])
def test_local_hour_stamps(year):
    stamps = ds.local_hour_stamps(year, "America/Havana")
    assert len(stamps) == 8760
    assert not ((stamps.month == 2) & (stamps.day == 29)).any()
    assert stamps[0] == pd.Timestamp(f"{year}-01-01", tz="America/Havana")
