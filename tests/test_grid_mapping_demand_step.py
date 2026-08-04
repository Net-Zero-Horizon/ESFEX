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
