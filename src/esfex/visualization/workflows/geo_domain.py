"""Turn an imported GeoAsset (vector GeoJSON) into a workflow study domain.

Workflows (Grid Builder, Solar PV, Wind, …) operate over a spatial domain. Today
the user draws it by hand; this module lets an imported GeoAsset polygon define
it instead — dissolving multi-feature assets into a single boundary.

Pure (no Qt) so it is unit-testable. Only depends on ``shapely`` (already a
project dependency).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

LatLng = tuple[float, float]


def _polygon_geoms(geojson: dict) -> list:
    """Collect every Polygon/MultiPolygon as a shapely geometry.

    Accepts a FeatureCollection, a bare Feature, or a bare geometry (mirroring
    ``geojson_importer``). Non-polygon features (points, lines) are skipped.
    """
    from shapely.geometry import shape

    gtype = geojson.get("type")
    if gtype == "FeatureCollection":
        geoms_src = [f.get("geometry") for f in geojson.get("features", [])]
    elif gtype == "Feature":
        geoms_src = [geojson.get("geometry")]
    else:
        geoms_src = [geojson]  # bare geometry

    out = []
    for g in geoms_src:
        if not g or g.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        try:
            out.append(shape(g))
        except Exception as exc:  # malformed geometry — skip, don't crash
            logger.warning("Skipping invalid polygon geometry: %s", exc)
    return out


def has_polygon(geojson: dict) -> bool:
    """Cheap check (no shapely) — does this GeoAsset hold any polygon feature?"""
    if not isinstance(geojson, dict):
        return False
    gtype = geojson.get("type")
    if gtype == "FeatureCollection":
        geoms = [(f or {}).get("geometry") or {} for f in geojson.get("features", [])]
    elif gtype == "Feature":
        geoms = [geojson.get("geometry") or {}]
    else:
        geoms = [geojson]
    return any(g.get("type") in ("Polygon", "MultiPolygon") for g in geoms)


def as_rings(polygon) -> list[list[LatLng]]:
    """Normalize a domain to a list of rings.

    A domain is either a single ring ``[(lat, lng), ...]`` or a multi-ring
    ``[[(lat, lng), ...], ...]`` (an archipelago / disjoint region). Returns
    the list of rings with < 3 vertices dropped. Backward-compatible: a bare
    ring is wrapped into a one-element list.

    Detection is by nesting depth of the first element: for a single ring the
    first element is a point ``(lat, lng)`` whose ``[0]`` is a number; for a
    multi-ring it is itself a ring whose ``[0]`` is a point (a sequence).
    """
    if not polygon:
        return []
    first = polygon[0]
    is_multi = (
        isinstance(first, (list, tuple))
        and len(first) > 0
        and isinstance(first[0], (list, tuple))
    )
    rings = polygon if is_multi else [polygon]
    return [list(r) for r in rings if r and len(r) >= 3]


def geoasset_to_domain_polygon(geojson: dict):
    """Dissolve a GeoAsset's polygons into a domain ring — or multi-ring.

    All Polygon/MultiPolygon geometries are unioned (``shapely.unary_union``).
    Coordinates are returned in ``(lat, lng)`` order (GeoJSON stores
    ``(lng, lat)``; every ESFEX consumer expects ``(lat, lng)``).

    Returns a single ring ``[(lat, lng), ...]`` when the union is one polygon
    (the common case), or a multi-ring ``[[(lat, lng), ...], ...]`` when it is
    disjoint (an archipelago such as Japan — Honshu, Hokkaido, Kyushu, …). This
    preserves every island: the earlier "keep only the largest ring" collapse
    silently dropped whole islands from both the fetch bbox and the clip.

    Returns ``[]`` when the asset has no polygon features.
    """
    from shapely import unary_union
    from shapely.geometry import MultiPolygon, Polygon

    geoms = _polygon_geoms(geojson)
    if not geoms:
        return []

    dissolved = unary_union(geoms)
    if isinstance(dissolved, MultiPolygon):
        parts = list(dissolved.geoms)
    elif isinstance(dissolved, Polygon):
        parts = [dissolved]
    else:
        parts = []

    rings: list[list[LatLng]] = []
    for part in parts:
        if not isinstance(part, Polygon) or part.is_empty:
            continue
        coords = list(part.exterior.coords)
        if len(coords) > 1 and coords[0] == coords[-1]:
            coords = coords[:-1]  # drop the closing duplicate
        rings.append([(lat, lng) for lng, lat in coords])

    if not rings:
        return []
    return rings[0] if len(rings) == 1 else rings


def domain_bounds(polygon) -> tuple[float, float, float, float]:
    """Bounding box ``(south, west, north, east)`` over every ring of a domain
    (single ring or multi-ring)."""
    pts = [p for ring in as_rings(polygon) for p in ring]
    lats = [p[0] for p in pts]
    lngs = [p[1] for p in pts]
    return (min(lats), min(lngs), max(lats), max(lngs))


def domain_shapely(polygon):
    """A shapely geometry in ``(lng, lat)`` for GeoDataFrame clipping — a
    ``Polygon`` for a single-ring domain, a ``MultiPolygon`` for a multi-ring
    (archipelago) domain."""
    from shapely.geometry import MultiPolygon, Polygon

    polys = [
        Polygon([(lng, lat) for lat, lng in ring])
        for ring in as_rings(polygon)
    ]
    if not polys:
        return Polygon()
    return polys[0] if len(polys) == 1 else MultiPolygon(polys)
