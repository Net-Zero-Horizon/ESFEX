"""Offline country detection by point-in-polygon.

The Grid Builder used to detect a region's country by reverse-geocoding the
bounding-box centroid via Nominatim at ``zoom=3``. That folds territories into
their sovereign state (a point in Puerto Rico comes back as the United States),
only ever finds one country (a region spanning two countries reports just the
centroid's one), and returns localized names. This module instead tests points
against the bundled ``world_countries.geojson`` — which carries Puerto Rico,
Haiti and the other territories as distinct, English-named features — so a
multi-country region resolves to *all* the countries it actually intersects.

ISO3 codes come from the bundled ``country_name_iso3.json`` (one entry per
geojson feature name), so no online lookup or extra dependency is needed.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from esfex.visualization.workflows.grid_mapping_fetchers import _point_in_polygon

_RESOURCES = Path(__file__).resolve().parents[1] / "resources"


@lru_cache(maxsize=1)
def _iso3_to_iso2() -> dict[str, str]:
    """Reverse of the ISO2→ISO3 table, for the few callers that want ISO2."""
    from esfex.visualization.workflows.demand_estimation_fetchers import (
        _ISO2_TO_ISO3,
    )
    return {iso3: iso2 for iso2, iso3 in _ISO2_TO_ISO3.items()}


def _rings(geometry: dict) -> list[tuple[list, list]]:
    """Return ``[(outer, [holes]), ...]`` with vertices as ``(lat, lng)``."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates", [])
    if gtype == "Polygon":
        polys = [coords]
    elif gtype == "MultiPolygon":
        polys = coords
    else:
        return []
    out = []
    for poly in polys:
        if not poly:
            continue
        outer = [(lat, lng) for lng, lat in poly[0]]
        holes = [[(lat, lng) for lng, lat in ring] for ring in poly[1:]]
        out.append((outer, holes))
    return out


@lru_cache(maxsize=1)
def _countries() -> list[tuple]:
    """Bundled country polygons: ``(name, iso3, iso2, polygons, bbox)``."""
    geo = json.loads((_RESOURCES / "world_countries.geojson").read_text("utf-8"))
    name_iso3 = json.loads(
        (_RESOURCES / "country_name_iso3.json").read_text("utf-8"))
    rev = _iso3_to_iso2()
    out = []
    for feat in geo.get("features", []):
        name = feat.get("properties", {}).get("name", "")
        if not name:
            continue
        polys = _rings(feat.get("geometry", {}))
        if not polys:
            continue
        iso3 = name_iso3.get(name, "")
        lats = [lat for outer, _ in polys for lat, _ in outer]
        lngs = [lng for outer, _ in polys for _, lng in outer]
        bbox = (min(lats), min(lngs), max(lats), max(lngs))
        out.append((name, iso3, rev.get(iso3, ""), polys, bbox))
    return out


def _contains(lat: float, lng: float, polys: list[tuple[list, list]]) -> bool:
    for outer, holes in polys:
        if _point_in_polygon(lat, lng, outer) and not any(
            _point_in_polygon(lat, lng, h) for h in holes
        ):
            return True
    return False


def detect_countries(
    points: list[tuple[float, float]],
) -> list[dict[str, str]]:
    """All countries whose territory contains any of ``points``.

    *points* are ``(lat, lng)`` pairs. Returns a list of
    ``{"name", "iso3", "iso2"}`` dicts, de-duplicated and sorted by name.
    Territories (Puerto Rico, U.S. Virgin Is., ...) resolve to themselves, not
    to their sovereign state.
    """
    countries = _countries()
    found: dict[str, dict[str, str]] = {}
    for lat, lng in points:
        if lat is None or lng is None:
            continue
        for name, iso3, iso2, polys, bbox in countries:
            min_lat, min_lng, max_lat, max_lng = bbox
            if not (min_lat <= lat <= max_lat and min_lng <= lng <= max_lng):
                continue
            if _contains(lat, lng, polys):
                key = iso3 or name
                if key not in found:
                    found[key] = {
                        "name": name,
                        "iso3": iso3 or name,
                        "iso2": iso2,
                    }
                break  # a point lies in at most one country
    return sorted(found.values(), key=lambda d: d["name"])


def sample_bbox(
    bounds: tuple[float, float, float, float], n: int = 6,
) -> list[tuple[float, float]]:
    """An ``n×n`` grid of points across ``bounds`` = (south, west, north, east).

    Used as a fallback when no node coordinates are available, so a region that
    spans several countries still surfaces all of them.
    """
    south, west, north, east = bounds
    n = max(2, n)
    return [
        (south + (north - south) * i / (n - 1),
         west + (east - west) * j / (n - 1))
        for i in range(n) for j in range(n)
    ]
