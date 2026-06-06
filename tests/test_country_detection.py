"""Offline country detection for the Grid Builder (#6)."""

from esfex.visualization.workflows.country_detection import (
    detect_countries,
    sample_bbox,
)


def _names(points):
    return {c["name"] for c in detect_countries(points)}


def _iso3(points):
    return {c["iso3"] for c in detect_countries(points)}


def test_haiti_is_detected():
    # Haiti was missed by the old centroid+Nominatim path.
    res = detect_countries([(18.9, -72.3)])
    assert res and res[0]["iso3"] == "HTI"


def test_puerto_rico_resolves_to_itself_not_us():
    # The old path folded Puerto Rico into the United States.
    res = detect_countries([(18.2, -66.5)])
    assert res, "Puerto Rico interior point should resolve"
    assert res[0]["iso3"] == "PRI"
    assert res[0]["name"] == "Puerto Rico"


def test_multi_country_region_returns_all():
    # A region spanning Hispaniola must surface both countries, not just one.
    res = _iso3([(18.9, -72.3), (18.9, -70.5)])
    assert {"HTI", "DOM"} <= res


def test_caribbean_bbox_sample_finds_islands():
    # south, west, north, east around Haiti / DR / Puerto Rico.
    res = _iso3(sample_bbox((17.6, -74.4, 18.9, -65.3), n=12))
    assert {"HTI", "DOM", "PRI"} <= res


def test_united_states_mainland():
    res = detect_countries([(39.0, -98.0)])
    assert res and res[0]["iso3"] == "USA"
    assert res[0]["iso2"] == "US"


def test_offshore_point_returns_empty():
    # Mid-Atlantic: no country, triggers the Nominatim fallback upstream.
    assert detect_countries([(30.0, -45.0)]) == []


def test_results_are_deduplicated_and_sorted():
    res = detect_countries([(18.9, -72.3), (18.9, -72.3), (18.9, -70.5)])
    names = [c["name"] for c in res]
    assert names == sorted(names)
    assert len(names) == len(set(names))


def test_sample_bbox_grid_size():
    pts = sample_bbox((0.0, 0.0, 10.0, 10.0), n=5)
    assert len(pts) == 25
    assert (0.0, 0.0) in pts and (10.0, 10.0) in pts
