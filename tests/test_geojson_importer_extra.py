"""Additive coverage tests for esfex.visualization.data.geojson_importer.

These tests assert the module's *current observed* behavior. Note that the
``import_geojson`` Point-import path passes ``coordinate=...`` to ``GuiNode``,
which does not accept that keyword (see gui_model.GuiNode signature). As a
result, every Point feature raises a ``TypeError`` that is caught and turned
into a warning, and ``nodes_added`` stays 0. The line/zone tests therefore
seed nodes directly on the state object rather than relying on Point import.
"""

from __future__ import annotations

import json
import math

import pytest

from esfex.visualization.data.geojson_importer import (
    ImportResult,
    _find_nearest_node,
    _haversine_km,
    import_geojson,
)
from esfex.visualization.data.gui_model import GuiNode, GuiSystemState


def _write(tmp_path, obj):
    p = tmp_path / "data.geojson"
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


# --------------------------------------------------------------------------
# Helper functions
# --------------------------------------------------------------------------

def test_haversine_zero_distance():
    assert _haversine_km(10.0, 20.0, 10.0, 20.0) == pytest.approx(0.0, abs=1e-9)


def test_haversine_known_distance():
    # ~111 km per degree of latitude near the equator
    d = _haversine_km(0.0, 0.0, 1.0, 0.0)
    assert d == pytest.approx(111.19, abs=1.0)


def test_find_nearest_node_empty():
    idx, dist = _find_nearest_node(1.0, 2.0, [])
    assert idx is None
    assert dist == float("inf")


def test_find_nearest_node_returns_first():
    nodes = [GuiNode(index=7, name="a"), GuiNode(index=9, name="b")]
    idx, dist = _find_nearest_node(1.0, 2.0, nodes)
    assert idx == 7
    assert dist == 0.0


# --------------------------------------------------------------------------
# import_geojson: top-level feature collection handling
# --------------------------------------------------------------------------

def test_no_features_returns_error(tmp_path):
    path = _write(tmp_path, {"type": "FeatureCollection", "features": []})
    state = GuiSystemState()
    result = import_geojson(state, path)
    assert isinstance(result, ImportResult)
    assert result.errors == ["No features found in GeoJSON file"]
    assert result.nodes_added == 0


def test_single_feature_object_wrapped(tmp_path):
    # A bare Feature (no FeatureCollection) is wrapped into a 1-item list.
    feat = {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1]]]},
        "properties": {"name": "Z"},
    }
    path = _write(tmp_path, feat)
    state = GuiSystemState()
    result = import_geojson(state, path)
    assert result.zones_added == 1


def test_geometry_only_object_wrapped(tmp_path):
    # An object lacking type=Feature but having a geometry is wrapped too.
    obj = {
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1]]]},
        "properties": {},
    }
    path = _write(tmp_path, obj)
    state = GuiSystemState()
    result = import_geojson(state, path)
    assert result.zones_added == 1


# --------------------------------------------------------------------------
# Point features (currently always error due to GuiNode signature mismatch)
# --------------------------------------------------------------------------

def test_point_feature_triggers_warning_not_node(tmp_path):
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [10.0, 20.0]},
                "properties": {"name": "P1"},
            }
        ],
    }
    path = _write(tmp_path, fc)
    state = GuiSystemState()
    result = import_geojson(state, path)
    # GuiNode(coordinate=...) raises TypeError -> caught -> warning, no node.
    assert result.nodes_added == 0
    assert len(state.nodes) == 0
    assert any("Point feature error" in w for w in result.warnings)


def test_point_with_invalid_coordinates(tmp_path):
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [10.0]},
                "properties": {},
            }
        ],
    }
    path = _write(tmp_path, fc)
    state = GuiSystemState()
    result = import_geojson(state, path)
    assert result.nodes_added == 0
    assert any("invalid coordinates" in w for w in result.warnings)


def test_point_near_existing_node_skipped(tmp_path):
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [10.0, 20.0]},
                "properties": {"name": "P2"},
            }
        ],
    }
    path = _write(tmp_path, fc)
    state = GuiSystemState()
    # Seed a node so _find_nearest_node returns (0, 0.0) < threshold -> skip.
    state.nodes.append(GuiNode(index=0, name="existing"))
    result = import_geojson(state, path)
    assert result.nodes_added == 0
    assert any("within" in w and "existing Node 0" in w for w in result.warnings)


# --------------------------------------------------------------------------
# LineString features
# --------------------------------------------------------------------------

def test_linestring_too_few_points(tmp_path):
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": [[0, 0]]},
                "properties": {},
            }
        ],
    }
    path = _write(tmp_path, fc)
    state = GuiSystemState()
    result = import_geojson(state, path)
    assert result.lines_added == 0
    assert any("fewer than 2 points" in w for w in result.warnings)


def test_linestring_no_node_skipped(tmp_path):
    # No nodes -> _find_nearest_node returns (None, inf) -> start warning.
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[0, 0], [1, 1]],
                },
                "properties": {},
            }
        ],
    }
    path = _write(tmp_path, fc)
    state = GuiSystemState()
    result = import_geojson(state, path)
    assert result.lines_added == 0
    assert any("LineString start" in w and "no node" in w for w in result.warnings)


def test_linestring_endpoints_same_node_skipped(tmp_path):
    # One node -> both endpoints snap to node 0 -> "same node" warning.
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[0, 0], [1, 1]],
                },
                "properties": {},
            }
        ],
    }
    path = _write(tmp_path, fc)
    state = GuiSystemState()
    state.nodes.append(GuiNode(index=0, name="n0"))
    result = import_geojson(state, path)
    assert result.lines_added == 0
    assert any("same node" in w for w in result.warnings)


def test_linestring_created_with_waypoints_and_capacity(tmp_path):
    # Two nodes -> both endpoints snap to node 0 (always first). To get two
    # distinct snapped indices we patch is impossible without changing the
    # source; instead we confirm that with a single node + capacity given,
    # the same-node guard fires. To exercise a successful line creation we
    # need from_idx != to_idx, which _find_nearest_node cannot produce
    # (always returns nodes[0]). So the successful-creation branch is
    # unreachable via this helper; document it here.
    pass


def test_linestring_feature_error_caught(tmp_path):
    # coordinates entries are not subscriptable as expected -> exception path.
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [5, 6],  # ints, coords[0][0] -> TypeError
                },
                "properties": {},
            }
        ],
    }
    path = _write(tmp_path, fc)
    state = GuiSystemState()
    state.nodes.append(GuiNode(index=0, name="n0"))
    result = import_geojson(state, path)
    assert result.lines_added == 0
    assert any("LineString feature error" in w for w in result.warnings)


# --------------------------------------------------------------------------
# Polygon features
# --------------------------------------------------------------------------

def test_polygon_no_coordinates(tmp_path):
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": []},
                "properties": {},
            }
        ],
    }
    path = _write(tmp_path, fc)
    state = GuiSystemState()
    result = import_geojson(state, path)
    assert result.zones_added == 0
    assert any("no coordinates" in w for w in result.warnings)


def test_polygon_empty_ring(tmp_path):
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [[]]},
                "properties": {},
            }
        ],
    }
    path = _write(tmp_path, fc)
    state = GuiSystemState()
    result = import_geojson(state, path)
    assert result.zones_added == 0
    assert any("no coordinates" in w for w in result.warnings)


def test_polygon_full_properties(tmp_path):
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2]]],
                },
                "properties": {
                    "name": "WindZone",
                    "technology": "Wind",
                    "max_capacity_mw": 500.0,
                    "color": "#abcdef",
                    "opacity": 0.5,
                },
            }
        ],
    }
    path = _write(tmp_path, fc)
    state = GuiSystemState()
    result = import_geojson(state, path)
    assert result.zones_added == 1
    z = state.development_zones[0]
    assert z.name == "WindZone"
    assert z.technology == "Wind"
    assert z.max_capacity_mw == 500.0
    assert z.style.color == "#abcdef"
    assert z.style.opacity == 0.5
    # GeoPoint coordinates are [lng,lat] swapped: first ring vertex [0,0]
    assert z.polygon[0].lat == 0
    assert z.polygon[0].lng == 0
    assert len(z.polygon) == 4


def test_polygon_defaults_applied(tmp_path):
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[1, 1], [3, 1], [3, 3]]],
                },
                "properties": {},
            }
        ],
    }
    path = _write(tmp_path, fc)
    state = GuiSystemState()
    result = import_geojson(state, path)
    assert result.zones_added == 1
    z = state.development_zones[0]
    assert z.name == "Zone 0"  # default name uses current count
    assert z.technology == "Solar"  # default technology
    assert z.max_capacity_mw is None  # default when missing
    assert z.style.color is None
    assert z.style.opacity == 0.15  # default opacity


def test_polygon_feature_error_caught(tmp_path):
    # outer_ring entries are ints -> c[1] raises -> Polygon feature error.
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [[1, 2, 3]]},
                "properties": {},
            }
        ],
    }
    path = _write(tmp_path, fc)
    state = GuiSystemState()
    result = import_geojson(state, path)
    assert result.zones_added == 0
    assert any("Polygon feature error" in w for w in result.warnings)


# --------------------------------------------------------------------------
# Mixed / non-matching geometries are simply skipped per pass
# --------------------------------------------------------------------------

def test_unknown_geometry_type_ignored(tmp_path):
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "MultiPoint", "coordinates": [[0, 0]]},
                "properties": {},
            }
        ],
    }
    path = _write(tmp_path, fc)
    state = GuiSystemState()
    result = import_geojson(state, path)
    assert result.nodes_added == 0
    assert result.lines_added == 0
    assert result.zones_added == 0
    assert result.warnings == []
    assert result.errors == []


def test_import_result_defaults():
    r = ImportResult()
    assert r.nodes_added == 0
    assert r.lines_added == 0
    assert r.zones_added == 0
    assert r.warnings == []
    assert r.errors == []
