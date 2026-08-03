"""Weather-fetch reliability for Grid Builder availability profiles.

Two compounding bugs made availability profiles come back as all-zeros for
grids with many generators (e.g. Japan): the fetch pool defaulted to
``cpu_count - 2`` workers (≈38 on a many-core box), which draws HTTP 429s from
Open-Meteo, and the backends *swallow* a failed fetch into an all-zero series
instead of raising — so the retry wrapper's ``return query()`` treated a
rate-limited miss as success and never retried.
"""
from __future__ import annotations

import types

import numpy as np
import pytest

import esfex.plugins.availability_generator.grid_builder_hook as gbh


def _gen():
    return types.SimpleNamespace(latitude=43.8, longitude=142.5, rated_power=10.0)


def test_default_workers_is_rate_limit_safe():
    # Must not scale with the core count — Open-Meteo 429s above ~4 concurrent.
    assert gbh._default_max_workers() <= 4
    assert gbh._resolve_max_workers(100) <= 4


def test_env_override_still_respected(monkeypatch):
    monkeypatch.setenv("ESFEX_AVAILABILITY_WORKERS", "2")
    assert gbh._resolve_max_workers(100) == 2


class TestRetryOnZeros:
    def _patch(self, monkeypatch, fn):
        solarex = pytest.importorskip("solarex")
        monkeypatch.setattr(solarex, "compute_solar_hourly_cf", fn, raising=False)
        monkeypatch.setattr(gbh, "_WEATHER_BACKOFF_S", 0.001)

    def test_persistent_zeros_raises_after_retries(self, monkeypatch):
        calls = {"n": 0}

        def zeros(*a, **k):
            calls["n"] += 1
            return np.zeros(8760)

        self._patch(monkeypatch, zeros)
        # An all-zero series is a failed fetch, not a valid profile: it must be
        # retried and finally raise (caller skips the generator), never returned.
        with pytest.raises(Exception):
            gbh._fetch_one_weather_cf("solar", _gen(), 2023, "open_meteo")
        assert calls["n"] == gbh._WEATHER_RETRIES

    def test_transient_zeros_recovered_by_retry(self, monkeypatch):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] < 2:
                return np.zeros(8760)          # first attempt: rate-limited
            out = np.zeros(8760)
            out[12::24] = 0.5                  # then a real daytime series
            return out

        self._patch(monkeypatch, flaky)
        res = gbh._fetch_one_weather_cf("solar", _gen(), 2023, "open_meteo")
        assert res is not None and np.count_nonzero(res) == 365
        assert calls["n"] == 2

    def test_valid_series_returned_without_retry(self, monkeypatch):
        calls = {"n": 0}

        def good(*a, **k):
            calls["n"] += 1
            out = np.zeros(8760)
            out[12::24] = 0.4
            return out

        self._patch(monkeypatch, good)
        res = gbh._fetch_one_weather_cf("solar", _gen(), 2023, "open_meteo")
        assert np.count_nonzero(res) == 365
        assert calls["n"] == 1
