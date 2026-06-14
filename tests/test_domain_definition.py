"""Tests for the shared DomainDefinitionWidget (polygon + GeoAsset domain)."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from esfex.visualization.workflows._domain_definition import (  # noqa: E402
    DomainDefinitionWidget,
)
from esfex.visualization.workflows.geo_domain import (  # noqa: E402
    domain_bounds,
    geoasset_to_domain_polygon,
)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class _FakeBridge(QObject):
    domainPolygonDrawn = Signal(str)
    modeReset = Signal()


class _FakeMap(QObject):
    def __init__(self):
        super().__init__()
        self.bridge = _FakeBridge()
        self.shown = None

    def enable_domain_polygon_draw(self):
        pass

    def disable_domain_polygon_draw(self):
        pass

    def show_domain_polygon(self, coords):
        self.shown = list(coords)


# A 1°×1° square GeoAsset around (40-41 N, -10..-9 E)
_SQUARE = {
    "type": "FeatureCollection",
    "features": [{
        "type": "Feature",
        "properties": {},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[-10, 40], [-9, 40], [-9, 41], [-10, 41], [-10, 40]]],
        },
    }],
}


class _Asset:
    def __init__(self, name, gj):
        self.name = name
        self.geojson_data = gj


def _provider_one():
    return {"a": _Asset("Province", _SQUARE)}


def _emit_drawn(widget, ring_lnglat):
    """Emit a drawn polygon as the JS bridge would (GeoJSON, [lng,lat])."""
    payload = json.dumps({"geometry": {"coordinates": [ring_lnglat]}})
    widget._map_widget.bridge.domainPolygonDrawn.emit(payload)


# A triangle ring in [lng, lat] order (what Leaflet sends)
_DRAWN_RING = [[5.0, 50.0], [6.0, 50.0], [5.5, 51.0], [5.0, 50.0]]


def test_draw_sets_polygon_and_bounds(app):
    w = DomainDefinitionWidget(_FakeMap(), geo_assets_provider=lambda: {})
    assert not w.is_defined()
    _emit_drawn(w, _DRAWN_RING)
    assert w.is_defined()
    poly = w.get_polygon()
    assert len(poly) >= 3
    # widget stores (lat, lng)
    assert poly[0] == (50.0, 5.0)
    assert w.get_bounds() == domain_bounds(poly)
    assert w._map_widget.shown == poly


def test_geoasset_pick_sets_domain(app):
    w = DomainDefinitionWidget(_FakeMap(), geo_assets_provider=_provider_one)
    w.refresh_assets()
    poly = geoasset_to_domain_polygon(_SQUARE)
    w._geo_ctl.domainPicked.emit(poly)
    assert w.get_polygon() == poly
    assert w.get_bounds() == domain_bounds(poly)


def test_draw_then_geoasset_overrides(app):
    w = DomainDefinitionWidget(_FakeMap(), geo_assets_provider=_provider_one)
    w.refresh_assets()
    _emit_drawn(w, _DRAWN_RING)
    geo_poly = geoasset_to_domain_polygon(_SQUARE)
    w._geo_ctl.domainPicked.emit(geo_poly)
    # GeoAsset wins
    assert w.get_polygon() == geo_poly
    assert w._map_widget.shown == geo_poly


def test_geoasset_then_draw_overrides(app):
    w = DomainDefinitionWidget(_FakeMap(), geo_assets_provider=_provider_one)
    w.refresh_assets()
    geo_poly = geoasset_to_domain_polygon(_SQUARE)
    w._geo_ctl.domainPicked.emit(geo_poly)
    _emit_drawn(w, _DRAWN_RING)
    # Drawn polygon wins
    assert w.get_polygon()[0] == (50.0, 5.0)
    assert w.get_polygon() != geo_poly


def test_empty_provider_shows_hint_inside_box(app):
    w = DomainDefinitionWidget(_FakeMap(), geo_assets_provider=lambda: {})
    w.show()
    w.refresh_assets()
    # The GeoAsset box stays visible (equal rectangle); picker hidden, hint shown.
    assert w._geo_ctl.isVisibleTo(w)
    assert not w._geo_ctl._row.isVisibleTo(w._geo_ctl)
    assert w._geo_ctl._hint.isVisibleTo(w._geo_ctl)
    # drawing still works with no assets
    _emit_drawn(w, _DRAWN_RING)
    assert w.is_defined()


def test_provider_with_assets_shows_picker(app):
    w = DomainDefinitionWidget(_FakeMap(), geo_assets_provider=_provider_one)
    w.show()
    w.refresh_assets()
    assert w._geo_ctl._row.isVisibleTo(w._geo_ctl)
    assert not w._geo_ctl._hint.isVisibleTo(w._geo_ctl)


def test_invalid_polygon_ignored(app):
    w = DomainDefinitionWidget(_FakeMap(), geo_assets_provider=lambda: {})
    _emit_drawn(w, [])  # empty ring
    assert not w.is_defined()
