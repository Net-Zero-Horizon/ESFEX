"""Tests for the Grid Builder building-footprint fetchers.

Focus on the QuadKey→bbox decoding used to pick Microsoft ML tiles, which had
two bugs that made footprints never download for the Americas: latitude was
bisected linearly instead of via Web Mercator, and the index QuadKey was read
as an int (stripping the leading zero that every western/northern-hemisphere
tile carries).
"""
from __future__ import annotations

from shapely.geometry import Point, box

from esfex.visualization.workflows.data_fetchers import BuildingFetcher

_HAVANA = Point(-82.37, 23.13)  # lon, lat


def _bbox(qk):
    return box(*BuildingFetcher._quadkey_to_bbox(qk))


class TestQuadKeyToBbox:
    def test_havana_tile_covers_havana(self):
        # Real Microsoft Cuba tile (9-digit, leading zero preserved).
        assert _bbox("032023230").contains(_HAVANA)

    def test_leading_zero_is_significant(self):
        # Dropping the leading zero (the int-parse bug) points at the wrong
        # hemisphere, so the tile no longer covers Havana.
        assert not _bbox("32023230").contains(_HAVANA)

    def test_web_mercator_latitude_not_linear(self):
        # Level-1 quadkey "0" is the NW tile: lon [-180,0], and its south edge
        # is the equator while the north edge is the Mercator limit (~85.05°),
        # NOT +90 as a linear bisection would give.
        w, s, e, n = BuildingFetcher._quadkey_to_bbox("0")
        assert (w, e) == (-180.0, 0.0)
        assert abs(s) < 1e-6
        assert 85.0 < n < 85.1

    def test_longitude_bounds_are_linear(self):
        # Longitude is linear in tile x; the four level-1 tiles tile the globe.
        assert BuildingFetcher._quadkey_to_bbox("1")[0] == 0.0     # NE west edge
        assert BuildingFetcher._quadkey_to_bbox("3")[2] == 180.0   # SE east edge

    def test_deeper_quadkey_is_smaller(self):
        parent = BuildingFetcher._quadkey_to_bbox("032023230")
        child = BuildingFetcher._quadkey_to_bbox("0320232300")
        pw, ps, pe, pn = parent
        cw, cs, ce, cn = child
        assert (ce - cw) < (pe - pw) and (cn - cs) < (pn - ps)
