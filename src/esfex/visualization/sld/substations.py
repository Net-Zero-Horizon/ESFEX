"""Substation detection for the single-line diagram.

A grid model's *node* is a planning region; one node can hold hundreds of
buses spread over hundreds of kilometres (a Grid Builder import of a whole
island, for instance). Laying the diagram out by node therefore lumps
unrelated buses into one block. This module groups buses by **site**
instead — the buses an engineer would draw inside one substation frame:

* buses joined by a transformer or converter whose two ends are close
  (``xfmr_km``): the HV and LV sides of the same substation;
* buses within ``site_km`` of each other, whatever joins them (double
  busbars, a solar farm's collector bus next to the substation, ...).

A site holding a single bare bus (no equipment, no transformer) is a line
junction — a tee or a tap point of a transmission corridor — and is drawn as
a compact junction instead of a substation frame.

Pure Python (no Qt), so it is unit-testable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# Two ends of a transformer further apart than this are separate sites: the
# data then models a long connecting line inside the transformer (step-up
# transformers of remote plants), and the diagram shows a transformer link
# between the two blocks. Closer pairs share one block, where the
# transformer is drawn as a clean vertical between its two bars.
XFMR_SITE_KM = 5.0
# Buses this close are one site even without a transformer between them.
SITE_RADIUS_KM = 0.3

_EARTH_R_KM = 6371.0


@dataclass
class Site:
    """One block of the diagram: a substation or a line junction."""

    sid: str
    bus_gids: list[str]
    lat: float
    lon: float
    name: str
    node: int
    kind: str = "substation"          # "substation" | "junction"
    voltages: list[float] = field(default_factory=list)


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1 = math.radians(a[0]), math.radians(a[1])
    la2, lo2 = math.radians(b[0]), math.radians(b[1])
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * _EARTH_R_KM * math.asin(math.sqrt(min(1.0, h)))


def _has_coord(lat: float, lon: float) -> bool:
    return not (lat == 0.0 and lon == 0.0) and math.isfinite(lat) and math.isfinite(lon)


def bus_positions(
    bus_coords: dict[str, tuple[float, float]],
    node_of: dict[str, int],
    node_centroids: dict[int, tuple[float, float]],
    neighbours: dict[str, set[str]],
    min_share: float = 0.5,
) -> dict[str, tuple[float, float]] | None:
    """Latitude / longitude for every bus, or None when the system has too
    little geography to lay out geographically.

    A bus without coordinates takes its node's centroid, then the mean of its
    already-placed neighbours (repeated, so a chain of unplaced buses is
    filled from both ends), then the mean of all placed buses.
    """
    pos = {g: c for g, c in bus_coords.items() if _has_coord(*c)}
    if len(pos) < max(2, min_share * len(bus_coords)):
        return None
    if len({(round(a, 5), round(b, 5)) for a, b in pos.values()}) < 2:
        return None
    for g in bus_coords:
        if g not in pos:
            c = node_centroids.get(node_of.get(g, -1))
            if c is not None and _has_coord(*c):
                pos[g] = c
    for _ in range(len(bus_coords)):
        missing = [g for g in bus_coords if g not in pos]
        if not missing:
            break
        progress = False
        for g in missing:
            placed = [pos[n] for n in neighbours.get(g, ()) if n in pos]
            if placed:
                pos[g] = (sum(p[0] for p in placed) / len(placed),
                          sum(p[1] for p in placed) / len(placed))
                progress = True
        if not progress:
            break
    if len(pos) < len(bus_coords):
        mlat = sum(p[0] for p in pos.values()) / len(pos)
        mlon = sum(p[1] for p in pos.values()) / len(pos)
        for g in bus_coords:
            pos.setdefault(g, (mlat, mlon))
    return pos


class _DSU:
    def __init__(self, items):
        self.p = {i: i for i in items}

    def find(self, a):
        p = self.p
        while p[a] != a:
            p[a] = p[p[a]]
            a = p[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Deterministic root: the smaller key wins.
            if rb < ra:
                ra, rb = rb, ra
            self.p[rb] = ra


def _close_pairs(pos: dict[str, tuple[float, float]], radius_km: float):
    """Pairs of buses within ``radius_km`` (grid bucketing, ~O(n))."""
    if not pos:
        return
    lat0 = math.radians(sum(p[0] for p in pos.values()) / len(pos))
    kx = 111.32 * math.cos(lat0)
    ky = 110.57
    cell = max(radius_km, 1e-6)
    buckets: dict[tuple[int, int], list[str]] = {}
    xy = {}
    for g, (la, lo) in pos.items():
        x, y = lo * kx, la * ky
        xy[g] = (x, y)
        buckets.setdefault((int(math.floor(x / cell)), int(math.floor(y / cell))), []).append(g)
    for (cx, cy), members in buckets.items():
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                other = buckets.get((cx + dx, cy + dy))
                if not other:
                    continue
                for a in members:
                    for b in other:
                        if a < b and haversine_km(pos[a], pos[b]) <= radius_km:
                            yield a, b


def detect_sites(
    bus_meta: dict[str, dict],
    edges: list[tuple[str, str, str]],
    pos: dict[str, tuple[float, float]],
    equipped: set[str],
    node_names: dict[int, str] | None = None,
    xfmr_km: float = XFMR_SITE_KM,
    site_km: float = SITE_RADIUS_KM,
) -> list[Site]:
    """Group buses into sites.

    Parameters
    ----------
    bus_meta : {gid: {"voltage_kv", "name", "parent_node"}}
    edges : [(src_gid, tgt_gid, edge_type)] with edge_type in
        ``transmission`` / ``transformer`` / ``converter``.
    pos : {gid: (lat, lon)} for every gid in ``bus_meta``.
    equipped : gids that carry at least one piece of equipment.
    node_names : {node index: name}, used to name a site that is a whole node.
    """
    node_names = node_names or {}
    dsu = _DSU(sorted(bus_meta))
    has_xfmr: set[str] = set()
    for s, t, kind in edges:
        if kind == "transmission" or s not in bus_meta or t not in bus_meta:
            continue
        has_xfmr.add(s)
        has_xfmr.add(t)
        if haversine_km(pos[s], pos[t]) <= xfmr_km:
            dsu.union(s, t)
    for a, b in _close_pairs(pos, site_km):
        dsu.union(a, b)

    members: dict[str, list[str]] = {}
    for g in sorted(bus_meta):
        members.setdefault(dsu.find(g), []).append(g)

    buses_per_node: dict[int, int] = {}
    for m in bus_meta.values():
        n = int(m.get("parent_node", 0) or 0)
        buses_per_node[n] = buses_per_node.get(n, 0) + 1

    sites: list[Site] = []
    for root, gids in members.items():
        gids.sort(key=lambda g: (-float(bus_meta[g].get("voltage_kv") or 0.0), g))
        hv = gids[0]
        lat = sum(pos[g][0] for g in gids) / len(gids)
        lon = sum(pos[g][1] for g in gids) / len(gids)
        nodes = {int(bus_meta[g].get("parent_node", 0) or 0) for g in gids}
        node = int(bus_meta[hv].get("parent_node", 0) or 0)
        # A site that is exactly one whole node takes the node's name.
        if (len(nodes) == 1 and buses_per_node.get(node) == len(gids)
                and node_names.get(node)):
            name = node_names[node]
        else:
            name = bus_meta[hv].get("name") or hv
        bare = (len(gids) == 1 and hv not in equipped and hv not in has_xfmr)
        sites.append(Site(
            sid=f"site_{root}",
            bus_gids=gids,
            lat=lat, lon=lon,
            name=str(name),
            node=node,
            kind="junction" if bare else "substation",
            voltages=sorted({float(bus_meta[g].get("voltage_kv") or 0.0)
                             for g in gids}, reverse=True),
        ))
    sites.sort(key=lambda s: s.sid)
    return sites
