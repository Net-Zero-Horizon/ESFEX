# -*- coding: utf-8 -*-
"""Real-topology construction for the Grid Builder (issue #16).

Implements the GridKit / PyPSA-Earth step that ESFEX was missing: **splitting a
transmission line where a substation sits ON it** (not at an endpoint). In OSM a
line is frequently one long ``way`` that passes through or alongside substations
without an explicit shared node there. Using only the line's first/last vertex
(as the old import did) leaves those substations isolated, which forced the
auto-connect to *fabricate* long straight bridges to glue the network back
together. Splitting the line at the overpassing substation captures the real
topology, so no fabrication is needed.

Mirrors ``build_osm_network.fix_overpassing_lines``: buffer/snap each substation
onto the lines within a small tolerance, split the line at the projected point,
and let each resulting segment connect to that substation. Line geometry is
preserved — a line is only subdivided, never re-routed.
"""

from __future__ import annotations

import dataclasses
import logging
import math
from collections import Counter, defaultdict
from typing import Iterable

from esfex.visualization.workflows.grid_mapping_fetchers import GridFeature

logger = logging.getLogger(__name__)

# Local equirectangular metres-per-degree (good to <0.1 % over a city-sized
# region — far better than the snap tolerances we use here).
_M_PER_DEG_LAT = 110_540.0
_M_PER_DEG_LNG_EQ = 111_320.0


def _voltage_compatible(v_line: float, v_bus: float, tol: float = 0.1) -> bool:
    """A line only connects to a bus at (≈) its own voltage; unknown → allow."""
    if v_line <= 0 or v_bus <= 0:
        return True
    return abs(v_bus - v_line) <= tol * v_line


def merge_contiguous_line_segments(
    lines: list[GridFeature],
) -> list[GridFeature]:
    """Merge contiguous same-voltage line segments into whole lines.

    OSM splits a transmission line into many short ``way`` segments that share
    exact end nodes. Evaluated individually, a segment shorter than the bus-snap
    distance has BOTH endpoints snap to the same nearby bus and is dropped as a
    self-loop — silently deleting the line and fragmenting the grid. Merging the
    segments first (shapely ``linemerge``, the step ESFEX was missing vs
    PyPSA/GridKit) yields whole lines with two distinct, well-separated
    endpoints that no longer collapse. Real geometry is preserved (segments are
    only concatenated); junctions where 3+ lines meet stay as breaks.
    """
    try:
        from shapely.geometry import LineString
        from shapely.ops import linemerge
    except Exception:  # pragma: no cover - shapely is a hard dep
        logger.warning("shapely unavailable; skipping line-merge (#16)")
        return list(lines)

    mergeable = [ln for ln in lines
                 if ln.line_coords and len(ln.line_coords) >= 2]
    rest = [ln for ln in lines
            if not (ln.line_coords and len(ln.line_coords) >= 2)]

    groups: dict[float, list[GridFeature]] = defaultdict(list)
    for ln in mergeable:
        groups[round(ln.voltage_kv, 1)].append(ln)  # only merge same-voltage

    out: list[GridFeature] = list(rest)
    n_in, n_out = 0, 0
    for _v, group in groups.items():
        n_in += len(group)
        if len(group) == 1:
            out.append(group[0])
            n_out += 1
            continue
        geoms = [LineString([(lo, la) for (la, lo) in ln.line_coords])
                 for ln in group]
        merged = linemerge(geoms)
        parts = (list(merged.geoms)
                 if merged.geom_type == "MultiLineString" else [merged])
        if len(parts) >= len(group):
            # nothing shared an endpoint → no merge; keep originals
            out.extend(group)
            n_out += len(group)
            continue
        # Property carry-over: each merged part inherits the originating segment
        # that contributes the most vertices (preserves name/voltage/circuits).
        coord_to_seg: dict[tuple[float, float], int] = {}
        for i, ln in enumerate(group):
            for (la, lo) in ln.line_coords:
                coord_to_seg.setdefault((round(la, 6), round(lo, 6)), i)
        for part in parts:
            coords_ll = [(la, lo) for (lo, la) in part.coords]
            if len(coords_ll) < 2:
                continue
            votes: Counter = Counter()
            for (la, lo) in coords_ll:
                seg = coord_to_seg.get((round(la, 6), round(lo, 6)))
                if seg is not None:
                    votes[seg] += 1
            rep = group[votes.most_common(1)[0][0]] if votes else group[0]
            out.append(dataclasses.replace(
                rep, line_coords=coords_ll,
                latitude=coords_ll[0][0], longitude=coords_ll[0][1]))
            n_out += 1

    if n_out < n_in:
        logger.info(
            "Line-merge (#16): %d OSM segments → %d contiguous lines",
            n_in, n_out,
        )
    return out


def cluster_nearby_buses(state, tol_m: float = 500.0) -> dict:
    """Merge buses at the same physical node/station into one (PyPSA
    ``set_substations_ids``).

    The faithful import creates a bus at every line endpoint; coincident
    junctions and a line endpoint sitting on a substation should be the SAME
    bus. This pass merges all same-voltage buses within ``tol_m`` of each other
    into a single bus, re-pointing every reference. ``tol_m`` is a "these points
    are the same station" tolerance — NOT a reach distance the user must tune —
    so connectivity comes from real coincidence, not from snapping to a far bus.

    Returns ``{"merged": n_removed, "selfloops_dropped": n}``.
    """
    try:
        from scipy.spatial import cKDTree
        import numpy as np
    except Exception:  # pragma: no cover
        return {"merged": 0, "selfloops_dropped": 0}
    from esfex.visualization.data.gui_model import EndpointRef

    by_voltage: dict[float, list[str]] = defaultdict(list)
    for bid, b in state.buses.items():
        if b.latitude == 0.0 and b.longitude == 0.0:
            continue
        by_voltage[round(b.voltage_kv, 1)].append(bid)

    remap: dict[str, str] = {}
    for _v, bids in by_voltage.items():
        if len(bids) < 2:
            continue
        lats = [state.buses[bid].latitude for bid in bids]
        lat0 = sum(lats) / len(lats)
        mlng = _M_PER_DEG_LNG_EQ * math.cos(math.radians(lat0))
        pts = np.array([
            (state.buses[bid].longitude * mlng,
             state.buses[bid].latitude * _M_PER_DEG_LAT)
            for bid in bids
        ])
        tree = cKDTree(pts)
        pairs = tree.query_pairs(tol_m)
        if not pairs:
            continue
        # union-find over this voltage group
        parent = list(range(len(bids)))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i, j in pairs:
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[max(ri, rj)] = min(ri, rj)  # keep lowest index survivor
        clusters: dict[int, list[int]] = defaultdict(list)
        for i in range(len(bids)):
            clusters[find(i)].append(i)
        for _root, members in clusters.items():
            if len(members) < 2:
                continue
            survivor = bids[members[0]]
            for m in members[1:]:
                remap[bids[m]] = survivor

    if not remap:
        return {"merged": 0, "selfloops_dropped": 0}

    def resolve(bid: str) -> str:
        seen = set()
        while bid in remap and bid not in seen:
            seen.add(bid)
            bid = remap[bid]
        return bid

    def fix_ep(ep):
        if ep is not None and ep.element_type == "bus":
            return EndpointRef("bus", resolve(ep.element_id))
        return ep

    for ln in state.transmission_lines:
        ln.from_bus = resolve(ln.from_bus)
        ln.to_bus = resolve(ln.to_bus)
        ln.from_endpoint = fix_ep(ln.from_endpoint)
        ln.to_endpoint = fix_ep(ln.to_endpoint)
    for tr in state.transformers:
        tr.from_bus = resolve(tr.from_bus)
        tr.to_bus = resolve(tr.to_bus)
        tr.from_endpoint = fix_ep(getattr(tr, "from_endpoint", None))
        tr.to_endpoint = fix_ep(getattr(tr, "to_endpoint", None))
    for c in state.acdc_converters:
        c.from_bus = resolve(c.from_bus)
        c.to_bus = resolve(c.to_bus)
    for coll in ("generators", "batteries", "electrolyzers"):
        items = getattr(state, coll, None)
        if not items:
            continue
        for obj in items.values():
            if getattr(obj, "bus", None):
                obj.bus = resolve(obj.bus)

    for old in remap:
        state.buses.pop(old, None)

    kept = [ln for ln in state.transmission_lines if ln.from_bus != ln.to_bus]
    n_selfloop = len(state.transmission_lines) - len(kept)
    state.transmission_lines = kept

    logger.info(
        "Bus clustering (#16): merged %d bus(es) into their station, "
        "dropped %d self-loop line(s)", len(remap), n_selfloop)
    return {"merged": len(remap), "selfloops_dropped": n_selfloop}


def split_lines_at_substations(
    lines: list[GridFeature],
    buses: Iterable[tuple[str, float, float, float]],
    tol_m: float = 150.0,
) -> list[GridFeature]:
    """Split each line where a same-voltage bus overpasses it.

    Parameters
    ----------
    lines : list[GridFeature]
        Line features (``feature_type == 'line'``) with ``line_coords`` set.
    buses : iterable of (bus_id, lat, lng, voltage_kv)
        Existing buses (substations, etc.) that lines may physically reach.
    tol_m : float
        A bus within this many metres of a line's interior (and not at an
        endpoint) is treated as a connection point: the line is split there.

    Returns
    -------
    list[GridFeature]
        Lines with overpassed segments split out; geometry preserved. Lines
        with no overpassing bus are returned unchanged.
    """
    try:
        from shapely import STRtree
        from shapely.geometry import LineString, Point
        from shapely.ops import substring
    except Exception:  # pragma: no cover - shapely is a hard dep, but be safe
        logger.warning("shapely unavailable; skipping line-splitting (#16)")
        return list(lines)

    bus_list = [
        (bid, float(lat), float(lng), float(v))
        for (bid, lat, lng, v) in buses
        if not (lat == 0.0 and lng == 0.0)
    ]
    if not bus_list or not lines:
        return list(lines)

    # Coarse candidate gather: an STRtree of bus points in (lng, lat) degrees.
    # A generous degree radius (using the most-compressed longitude scale) over-
    # selects; the exact metric distance is checked per candidate afterwards.
    bus_pts = [Point(lng, lat) for (_, lat, lng, _) in bus_list]
    tree = STRtree(bus_pts)
    tol_deg = tol_m / (_M_PER_DEG_LNG_EQ * math.cos(math.radians(60.0)))

    out: list[GridFeature] = []
    n_split = 0
    for ln in lines:
        coords = ln.line_coords
        if not coords or len(coords) < 2:
            out.append(ln)
            continue

        line_deg = LineString([(lo, la) for (la, lo) in coords])
        cand = tree.query(line_deg, predicate="dwithin", distance=tol_deg)
        if len(cand) == 0:
            out.append(ln)
            continue

        # Refine in local metres for an accurate distance / along-line position.
        lat0 = sum(c[0] for c in coords) / len(coords)
        mlng = _M_PER_DEG_LNG_EQ * math.cos(math.radians(lat0))

        def to_xy(la: float, lo: float) -> tuple[float, float]:
            return (lo * mlng, la * _M_PER_DEG_LAT)

        def to_ll(x: float, y: float) -> tuple[float, float]:
            return (y / _M_PER_DEG_LAT, x / mlng)

        line_xy = LineString([to_xy(la, lo) for (la, lo) in coords])
        if line_xy.length <= 0:
            out.append(ln)
            continue

        cuts: list[float] = []
        for i in cand:
            _bid, blat, blng, bv = bus_list[int(i)]
            if not _voltage_compatible(ln.voltage_kv, bv):
                continue
            p = Point(to_xy(blat, blng))
            if line_xy.distance(p) > tol_m:
                continue
            s = line_xy.project(p)
            # Ignore buses at (or past) an endpoint — those are normal
            # terminations, not an overpass.
            if s <= tol_m or s >= line_xy.length - tol_m:
                continue
            cuts.append(s)

        if not cuts:
            out.append(ln)
            continue

        # Split at the unique, ordered cut positions.
        cut_pts = sorted({round(s, 2) for s in cuts})
        bounds = [0.0] + cut_pts + [line_xy.length]
        seg_count = 0
        for k in range(len(bounds) - 1):
            a, b = bounds[k], bounds[k + 1]
            if b - a < 1.0:  # skip degenerate <1 m slivers
                continue
            seg = substring(line_xy, a, b)
            seg_ll = [to_ll(x, y) for (x, y) in seg.coords]
            if len(seg_ll) < 2:
                continue
            out.append(dataclasses.replace(
                ln,
                line_coords=seg_ll,
                name=f"{ln.name}_s{seg_count}",
                latitude=seg_ll[0][0],
                longitude=seg_ll[0][1],
            ))
            seg_count += 1
        if seg_count > 1:
            n_split += 1
        elif seg_count == 0:
            out.append(ln)  # nothing usable produced; keep original

    if n_split:
        logger.info(
            "Line-splitting (#16): split %d line(s) at overpassing substations",
            n_split,
        )
    return out
