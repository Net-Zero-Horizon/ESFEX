"""Grid Builder step 3 (Demand) — bus-distribution button enablement.

Regression guard for the unified 'Distribute demand' button: after the two
method-specific buttons (spatial / footprint) were merged into one, the button's
enabled state must follow the *selected* method's precondition — spatial demand
needs a completed forecast, a footprint proxy only needs domain bounds. A prior
regression gated the single button on footprint-target detection, which left it
permanently disabled for the (default) 'Spatial demand' method.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QApplication  # noqa: E402

from esfex.visualization.i18n import init_i18n  # noqa: E402
from esfex.visualization.workflows.grid_mapping_steps import (  # noqa: E402
    GridMappingDemandStep,
)


@pytest.fixture(scope="module")
def _app():
    app = QApplication.instance() or QApplication([])
    init_i18n("en")
    return app


@pytest.fixture
def step(_app):
    return GridMappingDemandStep()


class _Forecast:
    cell_annual_mwh = [1.0]
    cell_lats = [0.0]
    cell_lons = [0.0]


def _select(step, method):
    idx = step._combo_bld_source.findData(method)
    assert idx >= 0
    step._combo_bld_source.setCurrentIndex(idx)


def test_spatial_disabled_without_forecast(step):
    _select(step, "spatial")
    step._bounds = (0.0, 0.0, 1.0, 1.0)
    step._forecast_result = None
    step._update_distribute_enabled()
    assert not step._btn_fetch_bld.isEnabled()


def test_spatial_enabled_after_forecast(step):
    # The bug: with a completed forecast, 'Spatial demand' must be runnable.
    _select(step, "spatial")
    step._forecast_result = _Forecast()
    step._update_distribute_enabled()
    assert step._btn_fetch_bld.isEnabled()


def test_footprint_enabled_with_bounds_no_forecast(step):
    _select(step, "microsoft")
    step._bounds = (0.0, 0.0, 1.0, 1.0)
    step._forecast_result = None
    step._update_distribute_enabled()
    assert step._btn_fetch_bld.isEnabled()


def test_footprint_disabled_without_bounds(step):
    _select(step, "google")
    step._bounds = None
    step._update_distribute_enabled()
    assert not step._btn_fetch_bld.isEnabled()


def test_switching_method_reevaluates_button(step):
    # Footprint runnable (bounds), spatial not (no forecast): switching between
    # them must flip the single button, not leave a stale state.
    step._bounds = (0.0, 0.0, 1.0, 1.0)
    step._forecast_result = None
    _select(step, "microsoft")
    assert step._btn_fetch_bld.isEnabled()
    _select(step, "spatial")           # currentIndexChanged → _on_dist_method_changed
    assert not step._btn_fetch_bld.isEnabled()


# ── Unified 'Fetch and Forecast' button ─────────────────────────────────
# The former 'Fetch WB + ERA5' and 'Forecast Demand' buttons were merged:
# one click fetches the inputs and, once they arrive, runs the forecast.

class _SyncThread:
    """Runs the worker inline so the fetch → forecast chain is testable
    without an event loop (``_ui_call`` then delivers directly)."""

    def __init__(self, target, daemon=None):
        self._target = target

    def start(self):
        self._target()


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _fake_get(url, **kw):
    if "worldbank" in url:
        return _Resp([{}, [{"value": 1234.0}]])
    return _Resp({"hourly": {"temperature_2m": [25.0] * 8760}})


@pytest.fixture
def ready_step(step):
    step._combo_country.addItem("Cuba", {"iso2": "CU", "iso3": "CUB", "name": "Cuba"})
    step._combo_country.setCurrentIndex(step._combo_country.count() - 1)
    step._bounds = (20.0, -85.0, 23.0, -74.0)
    return step


def test_single_fetch_and_forecast_button(step):
    assert step._btn_fetch_forecast.text() == "Fetch and Forecast"
    assert step._btn_fetch_forecast.isEnabled()
    assert not hasattr(step, "_btn_fetch_data")
    assert not hasattr(step, "_btn_forecast")


def test_fetch_then_forecast(ready_step, monkeypatch):
    import threading
    import requests
    monkeypatch.setattr(threading, "Thread", _SyncThread)
    monkeypatch.setattr(requests, "get", _fake_get)
    calls = []
    monkeypatch.setattr(ready_step, "_run_forecast", lambda: calls.append("forecast"))

    ready_step._fetch_and_forecast()

    assert calls == ["forecast"]
    assert ready_step._wb_data["population"] == 1234.0
    assert len(ready_step._era5_data["temperature_hourly"]) == 8760


def test_rerun_skips_fetch_when_inputs_unchanged(ready_step, monkeypatch):
    import threading
    import requests
    monkeypatch.setattr(threading, "Thread", _SyncThread)
    monkeypatch.setattr(requests, "get", _fake_get)
    monkeypatch.setattr(ready_step, "_run_forecast", lambda: None)
    ready_step._fetch_and_forecast()

    fetches, forecasts = [], []
    monkeypatch.setattr(ready_step, "_fetch_wb_era5",
                        lambda on_done=None: fetches.append(1))
    monkeypatch.setattr(ready_step, "_run_forecast", lambda: forecasts.append(1))
    ready_step._fetch_and_forecast()
    assert fetches == [] and forecasts == [1]

    # A new base year changes the ERA5 weather year → fetch again.
    ready_step._spin_base_year.setValue(ready_step._spin_base_year.value() - 1)
    ready_step._fetch_and_forecast()
    assert fetches == [1]


def test_fetch_error_does_not_forecast(ready_step, monkeypatch):
    import threading
    import requests
    monkeypatch.setattr(threading, "Thread", _SyncThread)
    monkeypatch.setattr(requests, "get", _fake_get)
    # Bounds missing → the ERA5 part fails inside the worker.
    ready_step._bounds = None
    calls = []
    monkeypatch.setattr(ready_step, "_run_forecast", lambda: calls.append(1))
    ready_step._fetch_and_forecast()
    assert calls == []
    assert ready_step._btn_fetch_forecast.isEnabled()
    assert "Fetch error" in ready_step._lbl_forecast_status.text()
