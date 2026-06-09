# -*- coding: utf-8 -*-
"""Tests for real-topology construction — line splitting at overpassing
substations (issue #16)."""

from __future__ import annotations

import pytest

from esfex.visualization.workflows.grid_mapping_fetchers import GridFeature
from esfex.visualization.workflows.grid_mapping_topology import (
    split_lines_at_substations,
    _voltage_compatible,
)


def _line(name, coords, voltage_kv=220.0):
    return GridFeature(
        source="osm", feature_type="line", name=name,
        latitude=coords[0][0], longitude=coords[0][1],
        voltage_kv=voltage_kv, line_coords=list(coords),
    )


class TestVoltageCompatible:
    def test_same_voltage(self):
        assert _voltage_compatible(220.0, 220.0)

    def test_within_tolerance(self):
        assert _voltage_compatible(220.0, 230.0)  # <10%

    def test_incompatible(self):
        assert not _voltage_compatible(220.0, 110.0)

    def test_unknown_allows(self):
        assert _voltage_compatible(0.0, 220.0)
        assert _voltage_compatible(220.0, 0.0)


class TestSplitLinesAtSubstations:
    def test_overpassing_substation_splits_line(self):
        # A horizontal line with a same-voltage bus sitting on its midpoint.
        ln = _line("L", [(21.0, -82.0), (21.0, -81.0)], voltage_kv=220.0)
        buses = [("sub_mid", 21.0, -81.5, 220.0)]
        out = split_lines_at_substations([ln], buses)
        assert len(out) == 2  # split into two segments
        # segments meet at ~the substation longitude
        e0 = out[0].line_coords[-1]
        s1 = out[1].line_coords[0]
        assert e0[1] == pytest.approx(-81.5, abs=0.01)
        assert s1[1] == pytest.approx(-81.5, abs=0.01)
        # outer ends preserved
        assert out[0].line_coords[0][1] == pytest.approx(-82.0, abs=0.01)
        assert out[1].line_coords[-1][1] == pytest.approx(-81.0, abs=0.01)

    def test_no_overpass_keeps_line(self):
        ln = _line("L", [(21.0, -82.0), (21.0, -81.0)])
        buses = [("far", 21.5, -81.5, 220.0)]  # ~55 km off the line
        out = split_lines_at_substations([ln], buses)
        assert len(out) == 1
        assert out[0].name == "L"

    def test_voltage_mismatch_no_split(self):
        ln = _line("L", [(21.0, -82.0), (21.0, -81.0)], voltage_kv=220.0)
        buses = [("lv_mid", 21.0, -81.5, 110.0)]  # on the line but 110 kV
        out = split_lines_at_substations([ln], buses)
        assert len(out) == 1

    def test_substation_at_endpoint_no_split(self):
        ln = _line("L", [(21.0, -82.0), (21.0, -81.0)], voltage_kv=220.0)
        buses = [("end", 21.0, -82.0, 220.0)]  # exactly at the start endpoint
        out = split_lines_at_substations([ln], buses)
        assert len(out) == 1

    def test_two_overpassing_substations_three_segments(self):
        ln = _line("L", [(21.0, -82.0), (21.0, -81.0)], voltage_kv=220.0)
        buses = [
            ("s1", 21.0, -81.7, 220.0),
            ("s2", 21.0, -81.3, 220.0),
        ]
        out = split_lines_at_substations([ln], buses)
        assert len(out) == 3

    def test_geometry_preserved_polyline(self):
        # An L-shaped line with a real bend; a bus on the second leg.
        coords = [(21.0, -82.0), (21.0, -81.0), (21.5, -81.0)]
        ln = _line("L", coords, voltage_kv=220.0)
        buses = [("mid", 21.25, -81.0, 220.0)]  # on the vertical leg
        out = split_lines_at_substations([ln], buses)
        assert len(out) == 2
        # the bend vertex (21.0,-81.0) must survive in the geometry
        all_pts = out[0].line_coords + out[1].line_coords
        assert any(abs(la - 21.0) < 1e-6 and abs(lo + 81.0) < 1e-6
                   for (la, lo) in all_pts)


from esfex.visualization.workflows.grid_mapping_topology import (
    merge_contiguous_line_segments,
)


class TestMergeContiguousLineSegments:
    def test_three_collinear_segments_merge_into_one(self):
        # Three same-voltage segments sharing exact end nodes → one line.
        segs = [
            _line("L", [(21.0, -82.0), (21.0, -81.7)], voltage_kv=220.0),
            _line("L", [(21.0, -81.7), (21.0, -81.4)], voltage_kv=220.0),
            _line("L", [(21.0, -81.4), (21.0, -81.0)], voltage_kv=220.0),
        ]
        out = merge_contiguous_line_segments(segs)
        assert len(out) == 1
        m = out[0]
        # endpoints span the whole original extent
        lngs = [lo for (_la, lo) in m.line_coords]
        assert min(lngs) == pytest.approx(-82.0, abs=1e-6)
        assert max(lngs) == pytest.approx(-81.0, abs=1e-6)

    def test_different_voltage_not_merged(self):
        segs = [
            _line("A", [(21.0, -82.0), (21.0, -81.7)], voltage_kv=220.0),
            _line("B", [(21.0, -81.7), (21.0, -81.4)], voltage_kv=110.0),
        ]
        out = merge_contiguous_line_segments(segs)
        assert len(out) == 2  # not merged across voltage levels

    def test_disconnected_segments_kept_separate(self):
        segs = [
            _line("A", [(21.0, -82.0), (21.0, -81.7)], voltage_kv=220.0),
            _line("B", [(20.0, -80.0), (20.0, -79.7)], voltage_kv=220.0),
        ]
        out = merge_contiguous_line_segments(segs)
        assert len(out) == 2  # no shared endpoint → unchanged

    def test_junction_stays_a_break(self):
        # A T-junction: three segments meet at one node. linemerge must not
        # merge through the degree-3 node → stays 3 (or 2+1) lines, not 1.
        j = (21.0, -81.5)
        segs = [
            _line("A", [(21.0, -82.0), j], voltage_kv=220.0),
            _line("B", [j, (21.0, -81.0)], voltage_kv=220.0),
            _line("C", [j, (21.5, -81.5)], voltage_kv=220.0),
        ]
        out = merge_contiguous_line_segments(segs)
        assert len(out) >= 2  # the junction is preserved as a break

    def test_merge_prevents_collapse_end_to_end(self):
        # Three short segments (each <5 km) of one 110 kV line near a single
        # substation: without merge each collapses to the substation bus and is
        # dropped; with merge the whole line keeps two distinct endpoints.
        import sys as _sys
        _sys.path.insert(0, "tests")
        from test_grid_mapping_builder_extra import (
            MockGuiModel, _make_state, _feat,
        )
        import esfex.visualization.workflows.grid_mapping_builder as _gmb
        model = MockGuiModel(_make_state(buses={}))
        feats = [
            _feat("substation", name="S", lat=21.0, lng=-82.0, voltage_kv=110.0),
            _feat("line", name="L", lat=21.0, lng=-82.00, voltage_kv=110.0,
                  line_coords=[(21.0, -82.00), (21.0, -81.98)]),
            _feat("line", name="L", lat=21.0, lng=-81.98, voltage_kv=110.0,
                  line_coords=[(21.0, -81.98), (21.0, -81.96)]),
            _feat("line", name="L", lat=21.0, lng=-81.96, voltage_kv=110.0,
                  line_coords=[(21.0, -81.96), (21.0, -81.50)]),
        ]
        _gmb.build_grid_from_features(model, feats, snap_threshold_km=5.0)
        # the merged line survives (not dropped as a self-loop)
        assert len(model.state.transmission_lines) >= 1


from esfex.visualization.workflows.grid_mapping_topology import (
    cluster_nearby_buses,
)
from esfex.visualization.data.gui_model import (
    GuiSystemState, GuiBus, GuiTransmissionLine, EndpointRef,
)


def _state_with_buses(specs, lines=None):
    st = GuiSystemState(name="s")
    for bid, lat, lng, v in specs:
        st.buses[bid] = GuiBus(bus_id=bid, name=bid, parent_node=0,
                               voltage_kv=v, latitude=lat, longitude=lng)
    st.transmission_lines = lines or []
    return st


class TestClusterNearbyBuses:
    def test_two_coincident_buses_merge(self):
        # ~100 m apart, same voltage → one bus
        st = _state_with_buses([
            ("b1", 21.0000, -82.0, 220.0),
            ("b2", 21.0009, -82.0, 220.0),
            ("b3", 21.5000, -82.0, 220.0),  # far → untouched
        ], lines=[
            GuiTransmissionLine(line_id="L", from_bus="b2", to_bus="b3",
                                voltage_kv=220.0,
                                from_endpoint=EndpointRef("bus", "b2"),
                                to_endpoint=EndpointRef("bus", "b3")),
        ])
        out = cluster_nearby_buses(st, tol_m=500.0)
        assert out["merged"] == 1
        assert "b2" not in st.buses and "b1" in st.buses and "b3" in st.buses
        # the line that pointed at b2 now points at the survivor b1
        ln = st.transmission_lines[0]
        assert ln.from_bus == "b1"
        assert ln.from_endpoint.element_id == "b1"

    def test_different_voltage_not_merged(self):
        st = _state_with_buses([
            ("b1", 21.0, -82.0, 220.0),
            ("b2", 21.0001, -82.0, 110.0),  # coincident but different voltage
        ])
        out = cluster_nearby_buses(st, tol_m=500.0)
        assert out["merged"] == 0
        assert len(st.buses) == 2

    def test_far_buses_not_merged(self):
        st = _state_with_buses([
            ("b1", 21.0, -82.0, 220.0),
            ("b2", 22.0, -82.0, 220.0),  # ~110 km
        ])
        assert cluster_nearby_buses(st, tol_m=500.0)["merged"] == 0

    def test_internal_self_loop_dropped(self):
        # a line whose both endpoints fall in the same cluster → self-loop → drop
        st = _state_with_buses([
            ("b1", 21.0000, -82.0, 220.0),
            ("b2", 21.0009, -82.0, 220.0),
        ], lines=[
            GuiTransmissionLine(line_id="L", from_bus="b1", to_bus="b2",
                                voltage_kv=220.0,
                                from_endpoint=EndpointRef("bus", "b1"),
                                to_endpoint=EndpointRef("bus", "b2")),
        ])
        out = cluster_nearby_buses(st, tol_m=500.0)
        assert out["merged"] == 1
        assert out["selfloops_dropped"] == 1
        assert len(st.transmission_lines) == 0


def test_faithful_build_clusters_line_endpoints_to_substations():
    """End-to-end (#16): in faithful mode, a line whose endpoints sit near two
    substations connects them after clustering — no magic snap, no collapse."""
    import sys as _sys
    _sys.path.insert(0, "tests")
    from test_grid_mapping_builder_extra import MockGuiModel, _make_state, _feat
    import esfex.visualization.workflows.grid_mapping_builder as _gmb
    model = MockGuiModel(_make_state(buses={}))
    feats = [
        _feat("substation", name="A", lat=21.0, lng=-82.0, voltage_kv=220.0),
        _feat("substation", name="B", lat=21.0, lng=-81.0, voltage_kv=220.0),
        # line endpoints ~80 m off each substation centroid
        _feat("line", name="L", lat=21.0008, lng=-82.0, voltage_kv=220.0,
              line_coords=[(21.0008, -82.0), (21.0008, -81.0)]),
    ]
    _gmb.build_grid_from_features(model, feats, faithful=True)
    s = model.state
    # the line's two endpoints clustered onto the two substation buses → both
    # substations are joined by the line (a connected 2-bus network)
    assert len(s.transmission_lines) == 1
    ln = s.transmission_lines[0]
    assert ln.from_bus != ln.to_bus
    fb, tb = s.buses[ln.from_bus], s.buses[ln.to_bus]
    # endpoints landed on the substation centroids (within clustering tol)
    assert {round(fb.longitude), round(tb.longitude)} == {-82, -81}
