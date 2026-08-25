"""CMIP6 climate-fetch retry for the SSP demand forecast.

The Open-Meteo climate API rate-limits heavily; a bare request lost a node's
future climate on the first HTTP 429 (seen building the Japan systems:
``CMIP6 climate fetch failed @… 429``), degrading that node's SSP demand
forecast. The fetch now retries transient failures with backoff.
"""
from __future__ import annotations

import pytest

import esfex.models.demand_density_ml as ddm


class _Resp:
    def __init__(self, code):
        self.status_code = code

    def raise_for_status(self):
        import requests
        if self.status_code >= 400:
            exc = requests.HTTPError(str(self.status_code))
            exc.response = self
            raise exc


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    monkeypatch.setattr(ddm, "_CMIP6_BACKOFF_S", 0.001)


def _patch_get(monkeypatch, codes):
    import requests
    calls = {"n": 0}

    def fake_get(url, timeout=None):
        r = _Resp(codes[min(calls["n"], len(codes) - 1)])
        calls["n"] += 1
        return r

    monkeypatch.setattr(requests, "get", fake_get)
    return calls


def test_transient_429_recovers(monkeypatch):
    calls = _patch_get(monkeypatch, [429, 429, 200])
    resp = ddm._cmip6_get_with_retry("http://x")
    assert resp.status_code == 200
    assert calls["n"] == 3


def test_persistent_429_raises_after_retries(monkeypatch):
    import requests
    calls = _patch_get(monkeypatch, [429])
    with pytest.raises(requests.HTTPError):
        ddm._cmip6_get_with_retry("http://x")
    assert calls["n"] == ddm._CMIP6_RETRIES


def test_server_error_5xx_retried(monkeypatch):
    calls = _patch_get(monkeypatch, [503, 200])
    resp = ddm._cmip6_get_with_retry("http://x")
    assert resp.status_code == 200
    assert calls["n"] == 2


def test_client_error_400_not_retried(monkeypatch):
    import requests
    calls = _patch_get(monkeypatch, [400])
    with pytest.raises(requests.HTTPError):
        ddm._cmip6_get_with_retry("http://x")
    assert calls["n"] == 1  # non-transient → no retry


def test_retry_after_header_is_honoured(monkeypatch):
    import requests

    class _RespH(_Resp):
        def __init__(self, code, retry_after=None):
            super().__init__(code)
            self.headers = {"Retry-After": retry_after} if retry_after else {}

    seq = [_RespH(429, "7"), _RespH(200)]
    calls = {"n": 0}

    def fake_get(url, timeout=None):
        r = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        return r

    monkeypatch.setattr(requests, "get", fake_get)
    slept = []
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: slept.append(s))
    resp = ddm._cmip6_get_with_retry("http://x")
    assert resp.status_code == 200
    # The 7-second Retry-After hint was used verbatim (not the backoff formula).
    assert 7 in slept


class TestCmip6DailyLimit:
    """The hard *daily* quota (``"try again tomorrow"``) must abort immediately
    and degrade, not burn retries × backoff on every cell for hours."""

    class _RespJson(_Resp):
        def __init__(self, code, reason=None):
            super().__init__(code)
            self.headers = {}
            self._reason = reason

        def json(self):
            return {"error": True, "reason": self._reason} if self._reason else {}

    def test_daily_limit_aborts_without_retrying(self, monkeypatch):
        import requests
        calls = {"n": 0}

        def fake_get(url, timeout=None):
            calls["n"] += 1
            return self._RespJson(
                429, "Daily API request limit exceeded. Please try again tomorrow.")

        monkeypatch.setattr(requests, "get", fake_get)
        with pytest.raises(ddm._Cmip6DailyLimit):
            ddm._cmip6_get_with_retry("http://x")
        assert calls["n"] == 1  # NO retries against a dead daily quota

    def test_burst_429_still_retries(self, monkeypatch):
        # A per-minute burst 429 (no "daily" reason) is transient → retried.
        import requests
        seq = [self._RespJson(429, "Minutely API request limit exceeded"),
               self._RespJson(200)]
        calls = {"n": 0}

        def fake_get(url, timeout=None):
            r = seq[min(calls["n"], len(seq) - 1)]
            calls["n"] += 1
            return r

        monkeypatch.setattr(requests, "get", fake_get)
        import time as _t
        monkeypatch.setattr(_t, "sleep", lambda *a, **k: None)
        resp = ddm._cmip6_get_with_retry("http://x")
        assert resp.status_code == 200
        assert calls["n"] == 2

    def test_fetch_stops_hammering_after_daily_limit(self, monkeypatch):
        # Once one cell reports the daily limit, remaining cells are not fetched.
        fetched = []

        def fake_point(lat, lon, s, e, model=ddm._CMIP6_MODEL):
            fetched.append((lat, lon))
            raise ddm._Cmip6DailyLimit("quota")

        monkeypatch.setattr(ddm, "_cmip6_point_hourly", fake_point)
        import time as _t
        monkeypatch.setattr(_t, "sleep", lambda *a, **k: None)

        # 5 nodes in 5 distinct cells; only the FIRST should be attempted.
        lats = [33.2, 34.5, 35.7, 36.2, 42.5]
        lons = [130.5, 133.0, 140.5, 139.5, 141.0]
        out = ddm.fetch_cmip6_node_temps(lats, lons, 2025, 26)

        assert len(fetched) == 1          # stopped after the first daily-limit
        assert out == [None] * 5          # all degrade to shared climate


class TestCmip6SpatialDedup:
    def test_nodes_in_same_cell_fetched_once_and_shared(self, monkeypatch):
        calls = []

        def fake_point(lat, lon, s, e, model=ddm._CMIP6_MODEL):
            calls.append((round(lat, 4), round(lon, 4)))
            import numpy as np
            return {s: np.zeros(8760)}

        monkeypatch.setattr(ddm, "_cmip6_point_hourly", fake_point)
        import time as _t
        monkeypatch.setattr(_t, "sleep", lambda *a, **k: None)

        # 6 nodes clustered into 3 coarse (~0.25°) cells.
        lats = [35.19, 35.21, 35.24, 39.06, 39.07, 34.71]
        lons = [139.96, 139.97, 139.99, 139.99, 140.00, 138.16]
        out = ddm.fetch_cmip6_node_temps(lats, lons, 2025, 26)

        assert len(calls) == 3           # one fetch per unique cell, not per node
        assert len(out) == 6             # one result per node
        assert all(o is not None for o in out)
        assert out[0] is out[1] is out[2]  # same cell → shared result object

    def test_cached_cells_are_not_throttled(self, monkeypatch, tmp_path):
        # If every cell is already cached, no network throttle sleep happens.
        monkeypatch.setattr(
            ddm, "_cmip6_point_hourly",
            lambda lat, lon, s, e, model=ddm._CMIP6_MODEL: {s: __import__("numpy").zeros(8760)})
        monkeypatch.setattr(
            ddm, "_cmip6_cache_path",
            lambda *a, **k: tmp_path / "exists.npz")
        (tmp_path / "exists.npz").write_bytes(b"x")
        slept = []
        import time as _t
        monkeypatch.setattr(_t, "sleep", lambda s: slept.append(s))
        ddm.fetch_cmip6_node_temps([35.1, 35.2], [139.1, 139.2], 2025, 26)
        assert slept == []  # all cached → no throttling
