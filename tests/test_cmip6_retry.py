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
