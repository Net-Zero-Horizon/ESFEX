"""Tests for the Geofabrik (local .osm.pbf) grid-extraction backend."""
import os

import pytest

pytest.importorskip("osmium")
pytest.importorskip("shapely")

from shapely.geometry import box  # noqa: E402

from esfex.visualization.workflows.grid_mapping_fetchers import (  # noqa: E402
    OSMGridFetcher,
)
from esfex.visualization.workflows.grid_mapping_geofabrik import (  # noqa: E402
    GeofabrikGridFetcher,
    make_osm_fetcher,
    should_use_geofabrik,
)

_ALL = {
    "substation", "generator", "line", "transformer", "storage",
    "converter", "fuel_entry", "fuel_storage",
}


def _silence(fetcher):
    fetcher.progress = type("S", (), {"emit": staticmethod(lambda *a, **k: None)})()
    return fetcher


def _write_synthetic_pbf(path):
    """A tiny extract: point substation, wind generator, refinery, a power
    line, a closed-way (polygon) substation, a road near fuel, a road far from
    fuel."""
    import osmium
    from osmium.osm import mutable

    w = osmium.SimpleWriter(str(path))
    w.add_node(mutable.Node(id=1, location=(139.70, 35.70),
               tags={"power": "substation", "voltage": "275000", "name": "Sub A"}))
    w.add_node(mutable.Node(id=2, location=(139.71, 35.71),
               tags={"power": "generator", "generator:source": "wind",
                     "generator:output:electricity": "50 MW"}))
    w.add_node(mutable.Node(id=3, location=(139.72, 35.72),
               tags={"industrial": "refinery", "name": "Refinery X"}))
    w.add_node(mutable.Node(id=10, location=(139.70, 35.70)))
    w.add_node(mutable.Node(id=11, location=(139.80, 35.75)))
    w.add_way(mutable.Way(id=100, nodes=[10, 11],
              tags={"power": "line", "voltage": "275000"}))
    for nid, lon, lat in [(20, 139.60, 35.60), (21, 139.62, 35.60),
                          (22, 139.62, 35.62), (23, 139.60, 35.62)]:
        w.add_node(mutable.Node(id=nid, location=(lon, lat)))
    w.add_way(mutable.Way(id=200, nodes=[20, 21, 22, 23, 20],
              tags={"power": "substation", "voltage": "154000", "name": "Poly"}))
    w.add_node(mutable.Node(id=30, location=(139.71, 35.71)))
    w.add_node(mutable.Node(id=31, location=(139.73, 35.73)))
    w.add_way(mutable.Way(id=300, nodes=[30, 31],
              tags={"highway": "primary", "name": "Near Road"}))
    w.add_node(mutable.Node(id=40, location=(141.0, 38.0)))
    w.add_node(mutable.Node(id=41, location=(141.1, 38.1)))
    w.add_way(mutable.Way(id=400, nodes=[40, 41],
              tags={"highway": "primary", "name": "Far Road"}))
    w.close()


class TestGeofabrikStreaming:
    def _fetcher(self):
        f = GeofabrikGridFetcher((34.0, 138.0, 37.0, 141.5), element_types=_ALL)
        return _silence(f)

    def test_stream_pbf_converts_nodes_ways_and_polygons(self, tmp_path):
        pbf = tmp_path / "mini.osm.pbf"
        _write_synthetic_pbf(pbf)
        f = self._fetcher()
        feats = f._stream_pbf(pbf, f._domain_polygon())
        by_id = {ft.osm_id: ft for ft in feats}

        assert by_id["node/1"].feature_type == "substation"
        assert by_id["node/1"].voltage_kv == 275.0
        # Wind generator kept with its capacity (not a rooftop drop)
        assert by_id["node/2"].feature_type == "generator"
        assert by_id["node/2"].capacity_mw == 50.0
        assert by_id["node/3"].feature_type == "fuel_entry"
        # Power line keeps full geometry
        assert by_id["way/100"].feature_type == "line"
        assert len(by_id["way/100"].line_coords) == 2
        # Closed-way substation collapses to a point at its centroid
        assert by_id["way/200"].feature_type == "substation"
        assert 35.60 <= by_id["way/200"].latitude <= 35.62

    def test_roads_far_from_fuel_are_pruned(self, tmp_path):
        pbf = tmp_path / "mini.osm.pbf"
        _write_synthetic_pbf(pbf)
        f = self._fetcher()
        feats = f._prune_roads(f._stream_pbf(pbf, f._domain_polygon()))
        roads = [ft for ft in feats if ft.feature_type == "road"]
        assert [r.osm_id for r in roads] == ["way/300"]  # near-fuel kept, far dropped

    def test_domain_polygon_excludes_out_of_area_features(self, tmp_path):
        pbf = tmp_path / "mini.osm.pbf"
        _write_synthetic_pbf(pbf)
        # Domain that excludes the far road (at 38N,141E) but keeps Tokyo cluster
        f = GeofabrikGridFetcher((35.0, 139.0, 36.0, 140.0), element_types=_ALL)
        _silence(f)
        dom = box(139.0, 35.0, 140.0, 36.0)
        feats = f._stream_pbf(pbf, dom)
        assert all(ft.osm_id != "way/400" for ft in feats)
        assert any(ft.osm_id == "node/1" for ft in feats)

    def test_relevance_filter_respects_element_types(self):
        # A fetcher that only wants substations must ignore generators/roads.
        f = GeofabrikGridFetcher((34.0, 138.0, 37.0, 141.5),
                                 element_types={"substation"})
        _silence(f)

        class _T:
            def __init__(self, d):
                self._d = d

            def get(self, k, default=None):
                return self._d.get(k, default)

        assert f._pbf_relevant(_T({"power": "substation"})) is True
        assert f._pbf_relevant(_T({"power": "generator"})) is False
        assert f._pbf_relevant(_T({"highway": "primary"})) is False

    def test_relevance_filter_matches_overpass_transformer_and_rooftop_rules(self):
        # Geofabrik must build the SAME network as Overpass: voltage-tagged
        # transformers only, and no roof-mounted PV.
        f = GeofabrikGridFetcher((34.0, 138.0, 37.0, 141.5),
                                 element_types={"transformer", "generator"})
        _silence(f)

        class _T(dict):
            def get(self, k, default=None):
                return dict.get(self, k, default)

        # transformer without voltage → dropped (avoids fake 220/110 injection)
        assert f._pbf_relevant(_T({"power": "transformer"})) is False
        assert f._pbf_relevant(_T({"power": "transformer",
                                   "voltage": "220000"})) is True
        # roof-mounted PV → dropped; ground/utility generator → kept
        assert f._pbf_relevant(_T({"power": "generator",
                                   "location": "roof"})) is False
        assert f._pbf_relevant(_T({"power": "generator",
                                   "generator:place": "roof"})) is False
        assert f._pbf_relevant(_T({"power": "generator",
                                   "generator:source": "wind"})) is True


class TestBackendSelection:
    def test_country_scale_with_polygon_uses_geofabrik(self, monkeypatch):
        monkeypatch.delenv("ESFEX_OSM_BACKEND", raising=False)
        poly = box(139.0, 35.0, 141.0, 37.0)
        assert should_use_geofabrik((24.0, 122.9, 45.6, 145.8), poly) is True

    def test_country_scale_without_polygon_stays_on_overpass(self, monkeypatch):
        monkeypatch.delenv("ESFEX_OSM_BACKEND", raising=False)
        assert should_use_geofabrik((24.0, 122.9, 45.6, 145.8), None) is False

    def test_small_bbox_stays_on_overpass(self, monkeypatch):
        monkeypatch.delenv("ESFEX_OSM_BACKEND", raising=False)
        poly = box(139.6, 35.6, 139.8, 35.75)
        assert should_use_geofabrik((35.6, 139.6, 35.75, 139.8), poly) is False

    def test_env_override_forces_backend(self, monkeypatch):
        poly = box(139.0, 35.0, 141.0, 37.0)
        monkeypatch.setenv("ESFEX_OSM_BACKEND", "overpass")
        assert should_use_geofabrik((24.0, 122.9, 45.6, 145.8), poly) is False
        monkeypatch.setenv("ESFEX_OSM_BACKEND", "geofabrik")
        assert should_use_geofabrik((35.6, 139.6, 35.75, 139.8), poly) is True

    def test_make_osm_fetcher_returns_right_class(self, monkeypatch):
        monkeypatch.delenv("ESFEX_OSM_BACKEND", raising=False)
        poly = box(139.0, 35.0, 141.0, 37.0)
        kw = dict(min_voltage_kv=110, min_capacity_mw=1,
                  element_types={"substation"}, polygon=poly)
        big = make_osm_fetcher((24.0, 122.9, 45.6, 145.8), **kw)
        small = make_osm_fetcher((35.6, 139.6, 35.75, 139.8), **kw)
        assert isinstance(big, GeofabrikGridFetcher)
        assert isinstance(small, OSMGridFetcher)
        assert not isinstance(small, GeofabrikGridFetcher)


class TestRegionResolution:
    def test_resolve_region_picks_smallest_covering_extract(self, monkeypatch):
        # Stub the Geofabrik index with two nested regions; the smaller one that
        # still covers the domain must win.
        f = _silence(GeofabrikGridFetcher((0, 0, 1, 1)))
        big = box(-10, -10, 10, 10)      # "continent"
        small = box(-2, -2, 2, 2)        # "country" (covers the domain, smaller)
        index = [
            {"properties": {"id": "continent",
                            "urls": {"pbf": "http://x/continent.pbf"}},
             "geometry": big.__geo_interface__},
            {"properties": {"id": "country",
                            "urls": {"pbf": "http://x/country.pbf"}},
             "geometry": small.__geo_interface__},
        ]
        monkeypatch.setattr(f, "_load_index", lambda: index)
        rid, url = f._resolve_region(box(-1, -1, 1, 1))
        assert rid == "country"
        assert url.endswith("country.pbf")
