"""Unit tests for GeoAsset → workflow domain dissolving."""

import pytest

from esfex.visualization.workflows.geo_domain import (
    domain_bounds,
    domain_shapely,
    geoasset_to_domain_polygon,
)


def _poly_feature(ring_lnglat):
    return {"type": "Feature", "geometry": {
        "type": "Polygon", "coordinates": [ring_lnglat]}}


def _fc(*features):
    return {"type": "FeatureCollection", "features": list(features)}


# A 0..10 x 0..10 square in (lng, lat).
SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]


def _area(ring):
    from esfex.visualization.workflows.geo_domain import domain_shapely
    return domain_shapely(ring).area


def test_single_polygon_returns_latlng_ring():
    poly = geoasset_to_domain_polygon(_fc(_poly_feature(SQUARE)))
    assert len(poly) == 4
    # (lat, lng) order — GeoJSON was (lng, lat).
    assert (0.0, 0.0) in poly and (10.0, 10.0) in poly
    assert _area(poly) == pytest.approx(100.0)


def test_overlapping_polygons_union_to_one():
    a = _poly_feature(SQUARE)
    b = _poly_feature([[5, 5], [15, 5], [15, 15], [5, 15], [5, 5]])
    poly = geoasset_to_domain_polygon(_fc(a, b))
    # Union of two overlapping squares → one L-shaped ring (area 175, not 200).
    assert _area(poly) == pytest.approx(175.0)


def test_disjoint_polygons_keep_every_part():
    small = _poly_feature([[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]])      # area 4
    big = _poly_feature([[20, 20], [30, 20], [30, 30], [20, 30], [20, 20]])  # area 100
    poly = geoasset_to_domain_polygon(_fc(small, big))
    # Every disjoint part is preserved (archipelago domains like Japan) — the
    # earlier "largest part only" collapse dropped whole islands.
    assert len(poly) == 2  # multi-ring: one ring per island
    assert _area(poly) == pytest.approx(104.0)  # both parts, not just the big one


def test_polygon_with_hole_keeps_exterior_only():
    geom = {"type": "Feature", "geometry": {
        "type": "Polygon",
        "coordinates": [
            SQUARE,
            [[3, 3], [3, 7], [7, 7], [7, 3], [3, 3]],  # hole
        ],
    }}
    poly = geoasset_to_domain_polygon(_fc(geom))
    assert _area(poly) == pytest.approx(100.0)  # hole ignored


def test_multipolygon_geometry_supported():
    geom = {"type": "Feature", "geometry": {
        "type": "MultiPolygon",
        "coordinates": [
            [[[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]],     # area 16
            [[[20, 20], [22, 20], [22, 22], [20, 22], [20, 20]]],  # area 4
        ],
    }}
    poly = geoasset_to_domain_polygon(_fc(geom))
    # Both parts of the MultiPolygon are kept (16 + 4).
    assert len(poly) == 2  # multi-ring
    assert _area(poly) == pytest.approx(20.0)


def test_no_polygons_returns_empty():
    point = {"type": "Feature", "geometry": {
        "type": "Point", "coordinates": [1, 2]}}
    line = {"type": "Feature", "geometry": {
        "type": "LineString", "coordinates": [[0, 0], [1, 1]]}}
    assert geoasset_to_domain_polygon(_fc(point, line)) == []
    assert geoasset_to_domain_polygon(_fc()) == []


def test_bare_geometry_and_feature_accepted():
    bare = {"type": "Polygon", "coordinates": [SQUARE]}
    assert len(geoasset_to_domain_polygon(bare)) == 4
    feat = _poly_feature(SQUARE)
    assert len(geoasset_to_domain_polygon(feat)) == 4


def test_domain_bounds():
    poly = [(1.0, 2.0), (3.0, 8.0), (5.0, 4.0)]
    assert domain_bounds(poly) == (1.0, 2.0, 5.0, 8.0)


def test_domain_shapely_roundtrip():
    poly = geoasset_to_domain_polygon(_fc(_poly_feature(SQUARE)))
    shp = domain_shapely(poly)
    assert shp.area == pytest.approx(100.0)


# ── Multi-ring (archipelago) domains ─────────────────────────────────

def test_as_rings_single_and_multi():
    from esfex.visualization.workflows.geo_domain import as_rings
    single = [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0)]
    assert as_rings(single) == [single]                 # bare ring wrapped
    multi = [single, [(5.0, 5.0), (5.0, 6.0), (6.0, 6.0)]]
    assert as_rings(multi) == multi                     # already multi-ring
    assert as_rings([]) == []
    assert as_rings([(0.0, 0.0), (0.0, 1.0)]) == []     # < 3 vertices dropped


def test_domain_bounds_spans_all_rings():
    # Two disjoint islands: bounds must be the union bbox, not one island's.
    multi = [
        [(0.0, 0.0), (0.0, 2.0), (2.0, 2.0), (2.0, 0.0)],
        [(20.0, 30.0), (20.0, 32.0), (22.0, 32.0), (22.0, 30.0)],
    ]
    assert domain_bounds(multi) == (0.0, 0.0, 22.0, 32.0)


def test_multiring_domain_keeps_disjoint_island_features():
    # The Japan bug: a feature on a second island must survive the polygon
    # clip when the domain is multi-ring (was dropped when only the largest
    # ring was kept).
    from esfex.visualization.workflows.grid_mapping_fetchers import (
        GridFeature,
        filter_features_by_polygon,
    )
    honshu = [(0.0, 0.0), (0.0, 10.0), (10.0, 10.0), (10.0, 0.0)]
    hokkaido = [(40.0, 140.0), (40.0, 142.0), (42.0, 142.0), (42.0, 140.0)]
    domain = [honshu, hokkaido]

    def _sub(lat, lng):
        return GridFeature(source="osm", feature_type="substation",
                           name="s", latitude=lat, longitude=lng)
    on_honshu = _sub(5.0, 5.0)
    on_hokkaido = _sub(41.0, 141.0)
    offshore = _sub(60.0, 60.0)

    kept = filter_features_by_polygon([on_honshu, on_hokkaido, offshore], domain)
    assert on_honshu in kept
    assert on_hokkaido in kept      # would be dropped before the fix
    assert offshore not in kept
