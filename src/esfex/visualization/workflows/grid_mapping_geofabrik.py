"""Country-scale OSM grid extraction from Geofabrik ``.osm.pbf`` extracts.

The live Overpass API is rate-limited and frequently degraded (repeated HTTP
500/502, mirrors that serve an empty database), which makes it unreliable for
country-scale builds. This module downloads the relevant Geofabrik *regional*
extract once (cached on disk) and filters it locally with ``pyosmium`` — the
same approach PyPSA-Earth uses via ``earth-osm``. No rate limits, fully
reproducible, and offline after the first download.

The extracted tags are converted to :class:`GridFeature` through the *exact*
same helpers as the Overpass path (:meth:`OSMGridFetcher._process_element`,
``_build_feature``, ``_process_fuel_element``, ``_apply_filters``), so a grid
built from a Geofabrik extract is identical to one built from Overpass — only
the transport differs.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from esfex.visualization.i18n import tr

from .geo_domain import as_rings
from .grid_mapping_fetchers import (
    GridFeature,
    OSMGridFetcher,
    _is_fuel_cargo,
)

logger = logging.getLogger(__name__)

# Geofabrik's machine-readable index of every downloadable extract, each with a
# boundary polygon and a ``.pbf`` URL. Used to resolve a study domain to the
# smallest regional extract that covers it.
GEOFABRIK_INDEX_URL = "https://download.geofabrik.de/index-v1.json"

# Major-road classes kept for fuel-truck routing (mirrors the Overpass path).
_ROAD_CLASSES = ("motorway", "trunk", "primary", "secondary")
# Roads are auxiliary; keep only those within this many degrees (~55 km) of a
# fuel feature so the routing graph stays small instead of holding every major
# road in the country.
_ROAD_NEAR_FUEL_DEG = 0.5


def should_use_geofabrik(bounds, polygon, min_span_deg: float = 2.0) -> bool:
    """Whether a domain warrants the Geofabrik local-extract backend.

    Geofabrik is used for country-scale domains (large span) that carry a study
    polygon, when ``osmium`` is importable. Small hand-drawn bounding boxes stay
    on Overpass: a 2.5 GB country download isn't worth it for a city, and the
    ocean-heavy bbox of an archipelago can't be resolved to a single region
    without a land polygon. Override with ``ESFEX_OSM_BACKEND`` =
    ``geofabrik`` | ``overpass`` | ``auto`` (default)."""
    backend = os.environ.get("ESFEX_OSM_BACKEND", "auto").lower()
    if backend == "overpass":
        return False
    if backend != "geofabrik":  # auto
        south, west, north, east = bounds
        if polygon is None or max(north - south, east - west) < min_span_deg:
            return False
    try:
        import osmium  # noqa: F401
    except Exception:
        logger.warning(
            "osmium not installed — falling back to Overpass. "
            "Install it (pip install osmium) for reliable country-scale builds."
        )
        return False
    return True


def make_osm_fetcher(bounds, *, min_voltage_kv, min_capacity_mw,
                     element_types, polygon, parent=None):
    """Return the appropriate OSM fetcher for the domain: Geofabrik for
    country-scale study areas, Overpass for small bounding boxes."""
    kw = dict(
        min_voltage_kv=min_voltage_kv, min_capacity_mw=min_capacity_mw,
        element_types=element_types, parent=parent, polygon=polygon,
    )
    if should_use_geofabrik(bounds, polygon):
        logger.info("OSM backend: Geofabrik local extract (country-scale).")
        return GeofabrikGridFetcher(bounds, **kw)
    logger.info("OSM backend: live Overpass API.")
    return OSMGridFetcher(bounds, **kw)


class GeofabrikGridFetcher(OSMGridFetcher):
    """Fetch power/fuel/road infrastructure from a Geofabrik regional extract.

    Drop-in replacement for :class:`OSMGridFetcher`: same constructor, same
    ``progress``/``finished``/``error`` signals, same ``GridFeature`` output.
    Overrides only the transport (``_fetch``): resolve region → download ``.pbf``
    (cached) → stream-filter with pyosmium → shared conversion + filtering.
    """

    def _fetch(self) -> list[GridFeature]:
        try:
            import osmium  # noqa: F401
        except Exception as exc:  # pragma: no cover - env dependent
            raise RuntimeError(
                "Geofabrik extraction needs the 'osmium' package "
                "(pip install osmium). Original error: " + str(exc)
            )

        domain = self._domain_polygon()
        self.progress.emit(2, tr("grid_builder.progress_geofabrik_resolving"))
        region_id, pbf_url = self._resolve_region(domain)
        logger.info("Geofabrik region resolved: %s (%s)", region_id, pbf_url)

        pbf_path = self._download_pbf(region_id, pbf_url)
        if self._cancelled:
            return []

        self.progress.emit(60, tr("grid_builder.progress_geofabrik_reading",
                                  region=region_id))
        feats = self._stream_pbf(pbf_path, domain)
        if self._cancelled:
            return []

        feats = self._prune_roads(feats)
        self.progress.emit(92, tr("grid_builder.progress_filtering_voltage",
                                  kv=f"{self.min_voltage_kv:g}"))
        feats = self._apply_filters(feats)
        self.progress.emit(100, tr("grid_builder.progress_osm_found",
                                   count=len(feats)))
        return feats

    # ── Domain polygon ───────────────────────────────────────────────

    def _domain_polygon(self):
        """Shapely polygon of the study domain in (lng, lat) order, or the
        bounding box if no study polygon was supplied."""
        from shapely.geometry import Polygon, box

        rings = as_rings(self._polygon) if self._polygon is not None else None
        if rings:
            polys = []
            for ring in rings:
                try:
                    # rings are (lat, lng); shapely wants (x=lng, y=lat)
                    polys.append(Polygon([(lng, lat) for lat, lng in ring]))
                except Exception:
                    continue
            polys = [p for p in polys if p.is_valid and p.area > 0]
            if polys:
                from shapely.ops import unary_union
                return unary_union(polys)
        return box(self.west, self.south, self.east, self.north)

    # ── Region resolution ────────────────────────────────────────────

    def _resolve_region(self, domain):
        """Pick the smallest Geofabrik extract that covers the study domain.

        Downloads (and caches) the Geofabrik index, then selects the region
        whose boundary covers essentially all of the domain, preferring the
        most specific (smallest-area) such region so we don't download a whole
        continent for one country."""
        from shapely.geometry import shape

        index = self._load_index()
        dom_area = max(domain.area, 1e-9)
        best = None  # (region_area, id, pbf_url)
        best_any = None  # fallback: max coverage regardless of threshold
        best_any_cov = -1.0
        for feat in index:
            pbf_url = (
                feat.get("properties", {}).get("urls", {}).get("pbf")
            )
            geom = feat.get("geometry")
            if not pbf_url or not geom:
                continue
            try:
                poly = shape(geom)
                if not poly.is_valid:
                    poly = poly.buffer(0)
                inter = poly.intersection(domain).area
            except Exception:
                continue
            cov = inter / dom_area
            rid = feat.get("properties", {}).get("id", "?")
            if cov > best_any_cov:
                best_any_cov, best_any = cov, (poly.area, rid, pbf_url)
            if cov >= 0.95:
                if best is None or poly.area < best[0]:
                    best = (poly.area, rid, pbf_url)
        chosen = best or best_any
        if chosen is None:
            raise RuntimeError(
                "Could not resolve a Geofabrik region for the study area."
            )
        return chosen[1], chosen[2]

    def _load_index(self):
        """Return the Geofabrik index features (download + cache the JSON)."""
        from esfex.paths import GEOFABRIK_DIR

        import time as _time
        cache = GEOFABRIK_DIR / "index-v1.json"
        fresh = (
            cache.exists()
            and (_time.time() - cache.stat().st_mtime) < 30 * 86400
        )
        if not fresh:
            import requests

            self.progress.emit(4, tr("grid_builder.progress_geofabrik_index"))
            r = requests.get(GEOFABRIK_INDEX_URL, timeout=120)
            r.raise_for_status()
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(r.content)
        data = json.loads(cache.read_bytes())
        return data.get("features", data if isinstance(data, list) else [])

    # ── Download ─────────────────────────────────────────────────────

    def _download_pbf(self, region_id: str, pbf_url: str) -> Path:
        """Download the regional ``.pbf`` (streamed, resumable-skip, cached).

        A fresh (< 30-day) cached file is reused as-is — the dominant win of the
        Geofabrik approach is that a country extract is fetched once and then
        every rebuild is fully offline."""
        from esfex.paths import GEOFABRIK_DIR
        import time as _time

        GEOFABRIK_DIR.mkdir(parents=True, exist_ok=True)
        dest = GEOFABRIK_DIR / f"{region_id.replace('/', '_')}-latest.osm.pbf"
        if dest.exists() and (_time.time() - dest.stat().st_mtime) < 30 * 86400:
            logger.info("Using cached Geofabrik extract: %s", dest)
            self.progress.emit(58, tr("grid_builder.progress_geofabrik_cached",
                                      region=region_id))
            return dest

        import requests

        tmp = dest.with_suffix(".part")
        self.progress.emit(6, tr("grid_builder.progress_geofabrik_downloading",
                                 region=region_id))
        with requests.get(pbf_url, stream=True, timeout=120) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length", 0))
            done = 0
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if self._cancelled:
                        return dest  # abandon; .part left for a resume later
                    if not chunk:
                        continue
                    fh.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = 6 + int(50 * done / total)
                        self.progress.emit(
                            min(pct, 56),
                            tr("grid_builder.progress_geofabrik_download_bytes",
                               region=region_id, done=f"{done / 1e9:.2f}",
                               total=f"{total / 1e9:.2f}"),
                        )
        os.replace(tmp, dest)
        return dest

    # ── PBF streaming ────────────────────────────────────────────────

    @staticmethod
    def _tagdict(taglist) -> dict:
        return {t.k: t.v for t in taglist}

    def _pbf_relevant(self, t) -> bool:
        """Fast pre-filter on a pyosmium TagList — avoids building a full dict
        for the vast majority of irrelevant elements."""
        et = self.element_types
        power = t.get("power")
        if power:
            if power == "substation":
                return "substation" in et
            if power in ("generator", "plant"):
                if "generator" not in et:
                    return False
                # Exclude roof-mounted PV — matches the Overpass query filter,
                # and the vast rooftop flood is dropped downstream anyway
                # (_is_rooftop_solar in _apply_filters).
                return not (t.get("generator:place") == "roof"
                            or t.get("location") == "roof")
            if power in ("line", "cable"):
                return "line" in et
            if power == "transformer":
                # Only voltage-tagged (transmission) transformers. A voltage-less
                # pole/distribution transformer defaults to a spurious 220/110
                # unit and injects fake buses — mirror the Overpass ["voltage"]
                # requirement so both backends build the same network.
                return "transformer" in et and bool(t.get("voltage"))
            if power == "converter":
                return "converter" in et
            if power == "storage":
                return "storage" in et
            return False
        if {"fuel_entry", "fuel_storage"} & et:
            if t.get("industrial") in (
                "refinery", "fuel_depot", "petroleum_terminal", "oil",
                "tank_farm",
            ):
                return True
            if t.get("man_made") in ("storage_tank", "oil_terminal"):
                return True
            if t.get("landuse") == "port" and _is_fuel_cargo(t.get("cargo") or ""):
                return True
            if t.get("harbour") == "yes" and _is_fuel_cargo(t.get("cargo") or ""):
                return True
        if self._wants_roads and t.get("highway") in _ROAD_CLASSES:
            return True
        return False

    def _stream_pbf(self, pbf_path, domain) -> list[GridFeature]:
        """Single streaming pass over the extract, converting relevant nodes and
        ways to GridFeatures. Closed ways (substation/plant/fuel polygons) are
        reduced to their node centroid in the same pass, so no expensive area
        assembly is needed; multipolygon *relations* (a small minority) are
        skipped."""
        import osmium
        from shapely.geometry import Point
        from shapely.prepared import prep

        prepared = prep(domain) if domain is not None else None

        def in_domain(lat, lng):
            return prepared is None or prepared.contains(Point(lng, lat))

        feats: list[GridFeature] = []
        seen: set = set()
        n = 0
        fp = osmium.FileProcessor(str(pbf_path)).with_locations()
        for obj in fp:
            if self._cancelled:
                break
            n += 1
            if n % 5_000_000 == 0:
                self.progress.emit(
                    min(60 + int(n / 1_000_000) % 30, 90),
                    tr("grid_builder.progress_geofabrik_scanning",
                       millions=n // 1_000_000, kept=len(feats)),
                )
            t = obj.tags
            if not self._pbf_relevant(t):
                continue
            try:
                if obj.is_node():
                    try:
                        lat, lng = obj.location.lat, obj.location.lon
                    except Exception:
                        continue
                    if not in_domain(lat, lng):
                        continue
                    tags = self._tagdict(t)
                    oid = f"node/{obj.id}"
                    f = (self._process_element(tags, lat, lng, oid)
                         or self._process_fuel_element(tags, lat, lng, oid))
                elif obj.is_way():
                    power = t.get("power") or ""
                    highway = t.get("highway") or ""
                    coords: list = []
                    for nd in obj.nodes:
                        try:
                            coords.append((nd.lat, nd.lon))
                        except Exception:
                            continue
                    if not coords:
                        continue
                    linear = power in ("line", "cable") or highway in _ROAD_CLASSES
                    if linear:
                        geom_coords = coords
                    else:
                        clat = sum(c[0] for c in coords) / len(coords)
                        clng = sum(c[1] for c in coords) / len(coords)
                        geom_coords = [(clat, clng)]
                    clat = sum(c[0] for c in geom_coords) / len(geom_coords)
                    clng = sum(c[1] for c in geom_coords) / len(geom_coords)
                    if not in_domain(clat, clng):
                        continue
                    tags = self._tagdict(t)
                    f = self._build_feature(tags, geom_coords, f"way/{obj.id}")
                else:
                    continue  # relations skipped
            except Exception:
                continue
            if f is not None and f.osm_id not in seen:
                seen.add(f.osm_id)
                feats.append(f)
        return feats

    def _prune_roads(self, feats: list[GridFeature]) -> list[GridFeature]:
        """Drop major roads that are far from any fuel feature — the road graph
        only needs to connect fuel entries to storages, not span the country."""
        fuel = [(f.latitude, f.longitude) for f in feats
                if f.feature_type in ("fuel_entry", "fuel_storage")]
        if not fuel:
            return [f for f in feats if f.feature_type != "road"]
        out: list[GridFeature] = []
        for f in feats:
            if f.feature_type != "road":
                out.append(f)
                continue
            if any(abs(f.latitude - la) <= _ROAD_NEAR_FUEL_DEG
                   and abs(f.longitude - lo) <= _ROAD_NEAR_FUEL_DEG
                   for la, lo in fuel):
                out.append(f)
        return out
