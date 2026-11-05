"""Quota-aware Open-Meteo weather download for Grid Builder availability.

Large grids came back with many wind/solar units lacking a profile: one
location-year costs ≈ 26 Open-Meteo calls, so the free tier's 600 calls/min
ran out after ~23 locations and the short retry backoff never outlasted the
one-minute window. The download now paces to the quota, waits out a 429,
stops on an hourly/daily limit, and caches cells on disk.
"""
from __future__ import annotations

import threading
import types

import numpy as np
import pytest

import esfex.plugins.availability_generator.grid_builder_hook as gbh
import esfex.plugins.availability_generator.open_meteo_weather as om

pytest.importorskip("solarex")
pytest.importorskip("windrex")


class _Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, d):
        self.slept.append(d)
        self.t += d


class _Resp:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _hourly_payload(n=8760, seed=0):
    rng = np.random.default_rng(seed)
    hours = np.arange(n)
    ghi = np.clip(900 * np.sin(np.pi * ((hours % 24) - 6) / 12), 0, None)
    return {"hourly": {
        "time": [f"t{i}" for i in range(n)],
        "shortwave_radiation": ghi.tolist(),
        "temperature_2m": (25 + 5 * rng.standard_normal(n)).tolist(),
        "wind_speed_10m": (5 + 2 * rng.random(n)).tolist(),
        "wind_speed_100m": (8 + 3 * rng.random(n)).tolist(),
    }}


def test_request_cost_follows_open_meteo_rule():
    assert om.request_cost(14, 4) == 1.0
    assert om.request_cost(14, 15) == 1.5
    assert om.request_cost(28, 4) == 2.0
    assert om.year_request_cost(2023) == pytest.approx(365 / 14)


def test_limiter_blocks_until_minute_window_frees():
    clk = _Clock()
    lim = om.MinuteRateLimiter(budget=100, clock=clk, sleep=clk.sleep)
    for _ in range(3):
        lim.acquire(30)
    assert clk.t == 0.0           # 90 of 100 used: no wait yet
    lim.acquire(30)               # would be 120 → must wait for the window
    assert clk.t >= 60.0


def test_minute_429_waits_and_retries(tmp_path, monkeypatch):
    import requests
    clk = _Clock()
    lim = om.MinuteRateLimiter(clock=clk, sleep=clk.sleep)
    replies = [_Resp(429, {"error": True, "reason":
                           "Minutely API request limit exceeded. Please try "
                           "again in one minute."}),
               _Resp(200, _hourly_payload())]
    calls = []
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: calls.append(k) or replies.pop(0))

    data = om.fetch_cell_weather(22.1, -80.3, 2023, lim, tmp_path, sleep=clk.sleep)
    assert data is not None and data["shortwave_radiation"].size == 8760
    assert len(calls) == 2
    assert clk.t >= 60.0          # waited out the minute window
    assert calls[0]["params"]["wind_speed_unit"] == "ms"

    # Cached: a second call makes no request.
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: pytest.fail("should hit the cache"))
    again = om.fetch_cell_weather(22.1, -80.3, 2023, lim, tmp_path)
    np.testing.assert_array_equal(again["wind_speed_100m"], data["wind_speed_100m"])


def test_daily_quota_raises_without_retry(tmp_path, monkeypatch):
    import requests
    calls = []
    monkeypatch.setattr(requests, "get", lambda *a, **k: calls.append(1) or _Resp(
        429, {"error": True, "reason": "Daily API request limit exceeded."}))
    lim = om.MinuteRateLimiter()
    with pytest.raises(om.OpenMeteoQuotaExhausted):
        om.fetch_cell_weather(22.1, -80.3, 2023, lim, tmp_path)
    assert len(calls) == 1


def test_capacity_factors_match_solarex_and_windrex(monkeypatch):
    """Same data in → same CF out as the per-location backends."""
    import requests
    from solarex import compute_solar_hourly_cf
    from windrex import compute_wind_hourly_cf

    payload = _hourly_payload(seed=3)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(200, payload))
    ref_solar = compute_solar_hourly_cf(22.0, -80.0, 2023, "open_meteo")
    ref_wind = compute_wind_hourly_cf(22.0, -80.0, 2023, "open_meteo",
                                      rated_power_mw=50.0)

    weather = {k: np.asarray(payload["hourly"][k], dtype=np.float32)
               for k in om.VARIABLES}
    np.testing.assert_allclose(om.solar_cf(weather), ref_solar, atol=1e-4)
    np.testing.assert_allclose(om.wind_cf(weather), ref_wind, atol=1e-4)


def _gen(lat, lon, rated=10.0):
    return types.SimpleNamespace(latitude=lat, longitude=lon, rated_power=rated)


def test_prefetch_one_request_per_cell(tmp_path, monkeypatch):
    monkeypatch.setenv("ESFEX_WEATHER_CACHE", str(tmp_path))
    payload = _hourly_payload()
    weather = {k: np.asarray(payload["hourly"][k], dtype=np.float32)
               for k in om.VARIABLES}
    cells = []

    def fake_fetch(lat, lon, year, limiter, cache_dir, stop):
        cells.append((lat, lon))
        return weather

    monkeypatch.setattr(om, "fetch_cell_weather", fake_fetch)
    classified = [
        ("g1", _gen(22.01, -80.02, 5), "sun", "solar"),
        ("g2", _gen(22.02, -80.01, 50), "wind", "wind"),   # same cell, other rating
        ("g3", _gen(22.03, -79.98, 3), "wind", "wind"),    # same cell again
        ("g4", _gen(21.50, -78.00, 5), "wind", "wind"),    # another cell
    ]
    cache = gbh._prefetch_weather_cfs(classified, 2023, "open_meteo", None)
    assert sorted(cells) == sorted({(22.0, -80.0), (21.5, -78.0)})
    for _gid, gen, _f, kind in classified:
        assert gbh._weather_cache_key(kind, gen, 2023, "open_meteo") in cache


def test_prefetch_stops_after_quota_exhausted(tmp_path, monkeypatch):
    monkeypatch.setenv("ESFEX_WEATHER_CACHE", str(tmp_path))
    monkeypatch.setenv("ESFEX_AVAILABILITY_WORKERS", "1")   # deterministic order
    calls = []

    def fake_fetch(lat, lon, year, limiter, cache_dir, stop):
        if stop.is_set():
            return None
        calls.append((lat, lon))
        raise om.OpenMeteoQuotaExhausted("Hourly API request limit exceeded.")

    monkeypatch.setattr(om, "fetch_cell_weather", fake_fetch)
    classified = [(f"g{i}", _gen(20.0 + i, -80.0), "sun", "solar")
                  for i in range(5)]
    cache = gbh._prefetch_weather_cfs(classified, 2023, "open_meteo", None)
    assert cache == {}
    assert len(calls) == 1        # no more requests once the quota is gone


def test_unreadable_200_body_is_retried(tmp_path, monkeypatch):
    """Under load a 200 can carry a truncated body: retry, don't fail."""
    import requests

    class _Broken(_Resp):
        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    replies = [_Broken(200), _Resp(200, _hourly_payload())]
    monkeypatch.setattr(requests, "get", lambda *a, **k: replies.pop(0))
    clk = _Clock()
    lim = om.MinuteRateLimiter(clock=clk, sleep=clk.sleep)
    data = om.fetch_cell_weather(22.1, -80.3, 2023, lim, tmp_path, sleep=clk.sleep)
    assert data is not None and not replies
