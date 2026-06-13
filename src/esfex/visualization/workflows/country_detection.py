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


# ISO-3166 alpha-2 → alpha-3 country codes. This lives here (the country
# utilities module) so it survives the removal of the obsolete standalone
# demand-estimation wizard, which used to host it. Callers: grid_mapping_steps
# (forecast country detection) and ``_iso3_to_iso2`` below.
_ISO2_TO_ISO3: dict[str, str] = {
    "AD": "AND", "AE": "ARE", "AF": "AFG", "AG": "ATG", "AL": "ALB",
    "AM": "ARM", "AO": "AGO", "AR": "ARG", "AT": "AUT", "AU": "AUS",
    "AZ": "AZE", "BA": "BIH", "BB": "BRB", "BD": "BGD", "BE": "BEL",
    "BF": "BFA", "BG": "BGR", "BH": "BHR", "BI": "BDI", "BJ": "BEN",
    "BN": "BRN", "BO": "BOL", "BR": "BRA", "BS": "BHS", "BT": "BTN",
    "BW": "BWA", "BY": "BLR", "BZ": "BLZ", "CA": "CAN", "CD": "COD",
    "CF": "CAF", "CG": "COG", "CH": "CHE", "CI": "CIV", "CL": "CHL",
    "CM": "CMR", "CN": "CHN", "CO": "COL", "CR": "CRI", "CU": "CUB",
    "CV": "CPV", "CY": "CYP", "CZ": "CZE", "DE": "DEU", "DJ": "DJI",
    "DK": "DNK", "DM": "DMA", "DO": "DOM", "DZ": "DZA", "EC": "ECU",
    "EE": "EST", "EG": "EGY", "ER": "ERI", "ES": "ESP", "ET": "ETH",
    "FI": "FIN", "FJ": "FJI", "FM": "FSM", "FR": "FRA", "GA": "GAB",
    "GB": "GBR", "GD": "GRD", "GE": "GEO", "GH": "GHA", "GM": "GMB",
    "GN": "GIN", "GQ": "GNQ", "GR": "GRC", "GT": "GTM", "GW": "GNB",
    "GY": "GUY", "HN": "HND", "HR": "HRV", "HT": "HTI", "HU": "HUN",
    "ID": "IDN", "IE": "IRL", "IL": "ISR", "IN": "IND", "IQ": "IRQ",
    "IR": "IRN", "IS": "ISL", "IT": "ITA", "JM": "JAM", "JO": "JOR",
    "JP": "JPN", "KE": "KEN", "KG": "KGZ", "KH": "KHM", "KI": "KIR",
    "KM": "COM", "KN": "KNA", "KP": "PRK", "KR": "KOR", "KW": "KWT",
    "KZ": "KAZ", "LA": "LAO", "LB": "LBN", "LC": "LCA", "LI": "LIE",
    "LK": "LKA", "LR": "LBR", "LS": "LSO", "LT": "LTU", "LU": "LUX",
    "LV": "LVA", "LY": "LBY", "MA": "MAR", "MD": "MDA", "ME": "MNE",
    "MG": "MDG", "MH": "MHL", "MK": "MKD", "ML": "MLI", "MM": "MMR",
    "MN": "MNG", "MR": "MRT", "MT": "MLT", "MU": "MUS", "MV": "MDV",
    "MW": "MWI", "MX": "MEX", "MY": "MYS", "MZ": "MOZ", "NA": "NAM",
    "NE": "NER", "NG": "NGA", "NI": "NIC", "NL": "NLD", "NO": "NOR",
    "NP": "NPL", "NR": "NRU", "NZ": "NZL", "OM": "OMN", "PA": "PAN",
    "PE": "PER", "PG": "PNG", "PH": "PHL", "PK": "PAK", "PL": "POL",
    "PR": "PRI", "PT": "PRT", "PW": "PLW", "PY": "PRY", "QA": "QAT",
    "RO": "ROU",
    "RS": "SRB", "RU": "RUS", "RW": "RWA", "SA": "SAU", "SB": "SLB",
    "SC": "SYC", "SD": "SDN", "SE": "SWE", "SG": "SGP", "SI": "SVN",
    "SK": "SVK", "SL": "SLE", "SM": "SMR", "SN": "SEN", "SO": "SOM",
    "SR": "SUR", "SS": "SSD", "ST": "STP", "SV": "SLV", "SY": "SYR",
    "SZ": "SWZ", "TC": "TCA", "TD": "TCD", "TG": "TGO", "TH": "THA",
    "TJ": "TJK", "TL": "TLS", "TM": "TKM", "TN": "TUN", "TO": "TON",
    "TR": "TUR", "TT": "TTO", "TV": "TUV", "TZ": "TZA", "UA": "UKR",
    "UG": "UGA", "US": "USA", "UY": "URY", "UZ": "UZB", "VA": "VAT",
    "VC": "VCT", "VE": "VEN", "VN": "VNM", "VU": "VUT", "WS": "WSM",
    "YE": "YEM", "ZA": "ZAF", "ZM": "ZMB", "ZW": "ZWE",
}


def _iso2_to_iso3(iso2: str) -> str:
    return _ISO2_TO_ISO3.get(iso2.upper(), iso2)


@lru_cache(maxsize=1)
def _iso3_to_iso2() -> dict[str, str]:
    """Reverse of the ISO2→ISO3 table, for the few callers that want ISO2."""
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
