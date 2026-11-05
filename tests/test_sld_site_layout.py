"""Geographic site layout of the single-line diagram.

Large systems used to be drawn as one row of node columns: 51:1 (Cuba,
398 buses), 170:1 (Hokkaido, 1 435 buses) or ~1000:1 (Japan, 296 buses)
aspect ratios, every line crossing half the diagram, and minutes of
routing. With bus coordinates the SLD now groups buses into sites
(substations / line junctions), lays each site out as a compact block and
places the blocks on a grid that follows the geography, routing lines
along the clear channels between blocks.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("PySide6.QtCore")

from esfex.visualization.data.gui_model import (  # noqa: E402
    GuiBus,
    GuiGeneratorInstance,
    GuiNode,
    GuiSystemState,
    GuiTransformer,
    GuiTransmissionLine,
)
from esfex.visualization.sld import graph_builder as gb  # noqa: E402
from esfex.visualization.sld.channel_router import (  # noqa: E402
    TRACK_SPACING,
    LineSpec,
    route_lines,
)
from esfex.visualization.sld.placement import Block, place_blocks  # noqa: E402
from esfex.visualization.sld.substations import (  # noqa: E402
    bus_positions,
    detect_sites,
)

# ── Site detection ────────────────────────────────────────────────


def _meta(kv, node=0, name=""):
    return {"voltage_kv": kv, "parent_node": node, "name": name}


def test_transformer_pair_close_together_is_one_site():
    meta = {"hv": _meta(220), "lv": _meta(110)}
    pos = {"hv": (22.0, -80.0), "lv": (22.01, -80.0)}          # ~1.1 km
    sites = detect_sites(meta, [("hv", "lv", "transformer")], pos, set())
    assert len(sites) == 1
    assert sites[0].bus_gids == ["hv", "lv"]                   # HV first
    assert sites[0].kind == "substation"


def test_transformer_ends_far_apart_are_two_sites():
    meta = {"hv": _meta(220), "lv": _meta(34.5)}
    pos = {"hv": (22.0, -80.0), "lv": (22.1, -80.0)}           # ~11 km
    sites = detect_sites(meta, [("hv", "lv", "transformer")], pos, set())
    assert len(sites) == 2
    assert all(s.kind == "substation" for s in sites)          # not junctions


def test_buses_on_the_same_site_join_without_transformer():
    meta = {"a": _meta(220), "b": _meta(220), "far": _meta(220)}
    pos = {"a": (22.0, -80.0), "b": (22.001, -80.0), "far": (23.0, -80.0)}
    sites = detect_sites(meta, [("a", "far", "transmission")], pos, {"a", "b", "far"})
    groups = sorted(s.bus_gids for s in sites)
    assert groups == [["a", "b"], ["far"]]


def test_bare_single_bus_is_a_junction():
    meta = {"j": _meta(220), "s": _meta(220)}
    pos = {"j": (22.0, -80.0), "s": (23.0, -80.0)}
    sites = {s.bus_gids[0]: s for s in detect_sites(
        meta, [("j", "s", "transmission")], pos, equipped={"s"})}
    assert sites["j"].kind == "junction"
    assert sites["s"].kind == "substation"


def test_site_that_is_a_whole_node_takes_the_node_name():
    meta = {"a": _meta(220, node=3, name="Bus A"), "b": _meta(110, node=3, name="Bus B")}
    pos = {"a": (22.0, -80.0), "b": (22.0, -80.0)}
    sites = detect_sites(meta, [], pos, {"a"}, node_names={3: "Matanzas"})
    assert sites[0].name == "Matanzas"


def test_positions_none_without_geography():
    coords = {"a": (0.0, 0.0), "b": (0.0, 0.0)}
    assert bus_positions(coords, {}, {}, {}) is None


def test_positions_fill_missing_from_node_then_neighbours():
    coords = {"a": (22.0, -80.0), "b": (23.0, -81.0), "c": (0.0, 0.0), "d": (0.0, 0.0)}
    node_of = {"a": 0, "b": 0, "c": 5, "d": 6}
    pos = bus_positions(coords, node_of, {5: (24.0, -82.0)},
                        {"d": {"a", "b"}, "a": {"d"}, "b": {"d"}})
    assert pos["c"] == (24.0, -82.0)                   # node centroid
    assert pos["d"] == pytest.approx((22.5, -80.5))    # mean of neighbours


# ── Placement ─────────────────────────────────────────────────────


def _overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def test_blocks_never_overlap_and_keep_a_channel():
    # A dense cluster: 30 blocks within a few km.
    blocks = [Block(f"b{i}", 200 + 37 * (i % 5), 150 + 29 * (i % 3),
                    22.0 + 0.002 * (i % 6), -80.0 + 0.002 * (i // 6)) for i in range(30)]
    pl = place_blocks(blocks)
    rects = {b.bid: (pl.xy[b.bid][0], pl.xy[b.bid][1],
                     pl.xy[b.bid][0] + b.w, pl.xy[b.bid][1] + b.h) for b in blocks}
    ids = sorted(rects)
    for i, a in enumerate(ids):
        ra = rects[a]
        grown = (ra[0] - pl.gap_x / 2 + 1e-6, ra[1] - pl.gap_y / 2 + 1e-6,
                 ra[2] + pl.gap_x / 2 - 1e-6, ra[3] + pl.gap_y / 2 - 1e-6)
        for b in ids[i + 1:]:
            assert not _overlap(grown, rects[b]), (a, b)


def test_placement_keeps_geographic_order():
    blocks = [Block("west", 300, 200, 22.0, -84.0),
              Block("east", 300, 200, 22.0, -76.0),
              Block("north", 300, 200, 23.0, -80.0),
              Block("south", 300, 200, 20.0, -80.0)]
    pl = place_blocks(blocks)
    x = {b: pl.xy[b][0] for b in pl.xy}
    y = {b: pl.xy[b][1] for b in pl.xy}
    assert x["west"] < x["north"] < x["east"]
    assert y["north"] < y["west"] < y["south"]


def test_empty_stretches_are_collapsed():
    # Two clusters 2 000 km apart must not leave thousands of empty cells.
    blocks = [Block(f"a{i}", 200, 150, 22.0 + 0.1 * i, -80.0) for i in range(5)]
    blocks += [Block(f"b{i}", 200, 150, 22.0 + 0.1 * i, -60.0) for i in range(5)]
    pl = place_blocks(blocks)
    assert pl.n_cols < 30


# ── Channel routing ───────────────────────────────────────────────


def test_line_routes_around_a_block_in_between():
    blocks = [Block("L", 200, 150, 22.0, -81.0),
              Block("M", 200, 150, 22.0, -80.0),
              Block("R", 200, 150, 22.0, -79.0)]
    pl = place_blocks(blocks, [("L", "R")], refine_passes=0)
    rect = {b.bid: (pl.xy[b.bid][0], pl.xy[b.bid][1],
                    pl.xy[b.bid][0] + b.w, pl.xy[b.bid][1] + b.h) for b in blocks}
    lx, ly = pl.xy["L"][0] + 100, pl.xy["L"][1] + 80
    rx, ry = pl.xy["R"][0] + 100, pl.xy["R"][1] + 80
    r = route_lines(pl, [LineSpec("e", "L", "R", lx, ly, True, rx, ry, True)])
    pts = r.routes["e"]
    (dlx, dly), (drx, dry), (dmx, dmy) = r.shift["L"], r.shift["R"], r.shift["M"]
    assert pts[0] == (lx + dlx, ly + dly) and pts[-1] == (rx + drx, ry + dry)
    m = rect["M"]
    m = (m[0] + dmx, m[1] + dmy, m[2] + dmx, m[3] + dmy)
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        assert x0 == x1 or y0 == y1                     # orthogonal
        seg = (min(x0, x1), min(y0, y1), max(x0, x1) + 1e-9, max(y0, y1) + 1e-9)
        assert not _overlap(seg, m)


def test_parallel_lines_get_separate_tracks():
    blocks = [Block("L", 200, 150, 22.0, -81.0), Block("R", 200, 150, 22.0, -79.0)]
    pl = place_blocks(blocks, [("L", "R")])
    x0, y0 = pl.xy["L"]
    x1, y1 = pl.xy["R"]
    lines = [LineSpec(f"e{k}", "L", "R", x0 + 60 + 20 * k, y0 + 80, True,
                      x1 + 60 + 20 * k, y1 + 80, True) for k in range(3)]
    routes = route_lines(pl, lines).routes
    horizontals = {round(p[1], 3) for r in routes.values()
                   for p, q in zip(r, r[1:]) if p[1] == q[1] and p[0] != q[0]}
    assert len(horizontals) >= 3


def _segments(routes):
    """Horizontal and vertical segments: (coord, lo, hi, line id)."""
    h, v = [], []
    for eid, pts in routes.items():
        for p, q in zip(pts, pts[1:]):
            if abs(p[1] - q[1]) < 1e-6 and abs(p[0] - q[0]) > 1e-6:
                h.append((p[1], min(p[0], q[0]), max(p[0], q[0]), eid))
            elif abs(p[0] - q[0]) < 1e-6 and abs(p[1] - q[1]) > 1e-6:
                v.append((p[0], min(p[1], q[1]), max(p[1], q[1]), eid))
    return h, v


def _crossings(routes):
    h, v = _segments(routes)
    return sum(1 for y, x0, x1, a in h for x, y0, y1, b in v
               if a != b and x0 + 1e-6 < x < x1 - 1e-6 and y0 + 1e-6 < y < y1 - 1e-6)


def _collinear_overlaps(routes):
    n = 0
    for segs in _segments(routes):
        for i, (c, lo, hi, a) in enumerate(segs):
            for c2, lo2, hi2, b in segs[i + 1:]:
                if a != b and abs(c - c2) < 0.5 and min(hi, hi2) - max(lo, lo2) > 1e-6:
                    n += 1
    return n


def _face(pl, bid, up, dx):
    x, y = pl.xy[bid]
    return x + dx, (y + 20.0 if up else y + 100.0)


def test_lines_sharing_a_channel_do_not_cross_where_they_part():
    # Two lines leave the west block together, then one heads north-east
    # and the other south-east: the northbound one must take the upper
    # track, so they part without crossing.
    blocks = [Block("W", 300, 120, 22.0, -81.0),
              Block("N", 200, 120, 23.5, -78.0),
              Block("S", 200, 120, 20.5, -78.0)]
    pl = place_blocks(blocks, [("W", "N"), ("W", "S")], refine_passes=0)
    sx1, sy1 = _face(pl, "W", True, 100)
    sx2, sy2 = _face(pl, "W", True, 140)
    nx, ny = _face(pl, "N", True, 100)
    tx, ty = _face(pl, "S", True, 100)
    lines = [LineSpec("to_s", "W", "S", sx1, sy1, True, tx, ty, True, "W|u", "S|u"),
             LineSpec("to_n", "W", "N", sx2, sy2, True, nx, ny, True, "W|u", "N|u")]
    routes = route_lines(pl, lines).routes
    assert _crossings(routes) == 0
    assert _collinear_overlaps(routes) == 0


def test_parallel_circuits_run_as_one_ribbon():
    blocks = [Block("L", 300, 120, 22.0, -81.0), Block("M", 300, 300, 22.0, -80.0),
              Block("R", 300, 120, 22.0, -79.0)]
    pl = place_blocks(blocks, [("L", "R")], refine_passes=0)
    lines = []
    for k in range(3):
        sx, sy = _face(pl, "L", True, 60 + 30 * k)
        tx, ty = _face(pl, "R", True, 60 + 30 * k)
        lines.append(LineSpec(f"c{k}", "L", "R", sx, sy, True, tx, ty, True, "L|u", "R|u"))
    routes = route_lines(pl, lines).routes
    # Same path shape (bend sequence), side by side, never crossing.
    shapes = {tuple("h" if abs(p[1] - q[1]) < 1e-6 else "v" for p, q in zip(r, r[1:]))
              for r in routes.values()}
    assert len(shapes) == 1
    assert _crossings(routes) == 0
    assert _collinear_overlaps(routes) == 0


def test_crowded_channel_is_widened_not_squeezed():
    blocks = [Block("L", 600, 120, 22.0, -81.0), Block("R", 600, 120, 22.0, -79.0)]
    pl = place_blocks(blocks, [("L", "R")])
    lines = []
    for k in range(14):
        sx, sy = _face(pl, "L", True, 40 + 36 * k)
        tx, ty = _face(pl, "R", True, 40 + 36 * k)
        # distinct faces: separate lines, not a ribbon
        lines.append(LineSpec(f"e{k}", "L", "R", sx, sy, True, tx, ty, True, f"L{k}", f"R{k}"))
    r = route_lines(pl, lines)
    h, _v = _segments(r.routes)
    ys = sorted({round(y, 6) for y, *_ in h})
    assert len(ys) >= 14
    assert min(b - a for a, b in zip(ys, ys[1:])) >= TRACK_SPACING - 1e-6
    # The channel grew: everything below it moved down, and the tracks stay
    # clear of both blocks.
    shifted = [dy for _dx, dy in r.shift.values()]
    assert max(shifted) > 0


@pytest.mark.parametrize("seed", range(10))
def test_router_invariants_on_random_systems(seed):
    import random

    rnd = random.Random(seed)
    nb = 6 + 3 * seed
    blocks = [Block(f"b{i}", rnd.choice([120, 240, 420]), rnd.choice([120, 260]),
                    22 + rnd.random() * rnd.choice([0.05, 1.0]),
                    -80 + rnd.random() * rnd.choice([0.05, 2.0])) for i in range(nb)]
    size = {b.bid: (b.w, b.h) for b in blocks}
    pairs = [tuple(rnd.sample([b.bid for b in blocks], 2)) for _ in range(2 * nb)]
    pl = place_blocks(blocks, pairs)
    lines = []
    for k, (a, b) in enumerate(pairs):
        ua, ub = rnd.random() < 0.6, rnd.random() < 0.6
        (ax, ay), (bx, by) = pl.xy[a], pl.xy[b]
        sx = ax + 20 + rnd.random() * (size[a][0] - 40)
        tx = bx + 20 + rnd.random() * (size[b][0] - 40)
        sy = ay + (20 if ua else size[a][1] - 20)
        ty = by + (20 if ub else size[b][1] - 20)
        lines.append(LineSpec(f"e{k}", a, b, sx, sy, ua, tx, ty, ub,
                              f"{a}|{ua}", f"{b}|{ub}"))
    r = route_lines(pl, lines)
    assert r.routes == route_lines(pl, lines).routes          # deterministic
    rects = {bid: (x + r.shift[bid][0], y + r.shift[bid][1],
                   x + r.shift[bid][0] + size[bid][0], y + r.shift[bid][1] + size[bid][1])
             for bid, (x, y) in pl.xy.items()}
    for ln in lines:
        pts = r.routes[ln.eid]
        for p, q in zip(pts, pts[1:]):
            assert abs(p[0] - q[0]) < 1e-6 or abs(p[1] - q[1]) < 1e-6
            seg = (min(p[0], q[0]) + 1, min(p[1], q[1]) + 1,
                   max(p[0], q[0]) - 1 + 0.01, max(p[1], q[1]) - 1 + 0.01)
            for bid, rect in rects.items():
                if bid not in (ln.s_block, ln.t_block):
                    assert not _overlap(seg, rect), (ln.eid, bid)
    assert _collinear_overlaps(r.routes) == 0


# ── build_elk_graph in site mode ──────────────────────────────────


def _bus(bid, node, kv, lat, lon, name=""):
    return GuiBus(bus_id=bid, parent_node=node, voltage_kv=kv, name=name,
                  latitude=lat, longitude=lon)


def _geo_state():
    """Two substations 150 km apart (each 220/110 kV with a transformer and a
    generator) joined by a 220 kV line through a bare tee junction."""
    st = GuiSystemState()
    st.nodes = [GuiNode(index=0, name="West"), GuiNode(index=1, name="East")]
    st.buses = {
        "w220": _bus("w220", 0, 220, 22.0, -81.0, "W 220"),
        "w110": _bus("w110", 0, 110, 22.001, -81.0, "W 110"),
        "e220": _bus("e220", 1, 220, 22.0, -79.6, "E 220"),
        "e110": _bus("e110", 1, 110, 22.001, -79.6, "E 110"),
        "tee": _bus("tee", 0, 220, 22.3, -80.3, "Tee"),
    }
    st.transformers = [
        GuiTransformer(name="TW", from_bus="w220", to_bus="w110", rated_power_mva=200.0),
        GuiTransformer(name="TE", from_bus="e220", to_bus="e110", rated_power_mva=200.0),
    ]
    st.transmission_lines = [
        GuiTransmissionLine(line_id="L1", from_bus="w220", to_bus="tee",
                            capacity_mw=300.0, voltage_kv=220.0),
        GuiTransmissionLine(line_id="L2", from_bus="tee", to_bus="e220",
                            capacity_mw=300.0, voltage_kv=220.0),
    ]
    st.generators = {
        "g1": GuiGeneratorInstance(instance_id="g1", unit_key="u", name="G1",
                                   gen_type="Non-renewable", fuel="Gas",
                                   bus="w110", rated_power=100.0),
    }
    return st


def test_site_mode_groups_are_substations_not_nodes():
    out = gb.build_elk_graph(_geo_state())
    groups = {tuple(sorted(g["busIds"])) for g in out["nodeGroups"]}
    assert groups == {("bus_w110", "bus_w220"), ("bus_e110", "bus_e220")}
    assert all("siteId" in g for g in out["nodeGroups"])
    by_id = {c["id"]: c for c in out["elkGraph"]["children"]}
    assert by_id["bus_tee"]["properties"].get("junction") is True


def test_site_mode_draws_transformers_inside_their_block():
    out = gb.build_elk_graph(_geo_state())
    by_id = {c["id"]: c for c in out["elkGraph"]["children"]}
    for e in out["elkGraph"]["edges"]:
        if e["properties"]["edgeType"] != "transformer":
            continue
        assert e["properties"].get("transformerVertical") is True
        hv, lv = e["sources"][0], e["targets"][0]
        assert by_id[hv]["y"] < by_id[lv]["y"]          # HV above LV


def test_site_mode_follows_geography_and_lines_are_orthogonal():
    out = gb.build_elk_graph(_geo_state())
    by_id = {c["id"]: c for c in out["elkGraph"]["children"]}
    assert by_id["bus_w220"]["x"] < by_id["bus_tee"]["x"] < by_id["bus_e220"]["x"]
    assert by_id["bus_tee"]["y"] < by_id["bus_e220"]["y"]  # the tee lies north
    for e in out["elkGraph"]["edges"]:
        sec = e["sections"][0]
        pts = [sec["startPoint"]] + sec["bendPoints"] + [sec["endPoint"]]
        for p, q in zip(pts, pts[1:]):
            assert abs(p["x"] - q["x"]) < 1e-6 or abs(p["y"] - q["y"]) < 1e-6


def test_site_mode_is_deterministic():
    a = json.dumps(gb.build_elk_graph(_geo_state()), sort_keys=True)
    b = json.dumps(gb.build_elk_graph(_geo_state()), sort_keys=True)
    assert a == b


def _edge_points(out, eid):
    e = next(e for e in out["elkGraph"]["edges"] if e["id"] == eid)
    sec = e["sections"][0]
    return [sec["startPoint"]] + sec["bendPoints"] + [sec["endPoint"]], e


def test_line_leaves_on_the_face_toward_the_far_block():
    # The far end lies well south: the line leaves the LV bar downwards
    # (beside its generator) instead of looping over its own substation.
    st = _geo_state()
    st.buses["far"] = _bus("far", 1, 110, 20.0, -81.0, "Far")
    st.transmission_lines.append(GuiTransmissionLine(
        line_id="L3", from_bus="w110", to_bus="far", capacity_mw=100.0, voltage_kv=110.0))
    out = gb.build_elk_graph(st)
    by_id = {c["id"]: c for c in out["elkGraph"]["children"]}
    eid = next(e["id"] for e in out["elkGraph"]["edges"]
               if {e["sources"][0], e["targets"][0]} == {"bus_w110", "bus_far"})
    pts, e = _edge_points(out, eid)
    w110 = by_id["bus_w110"]
    end = pts[0] if e["sources"][0] == "bus_w110" else pts[-1]
    assert end["y"] == pytest.approx(w110["y"] + gb._BUS_H)      # bottom face


def test_covered_middle_bar_is_lengthened_instead_of_crossed():
    # 400 / 220 / 110 kV stacked; the 110 kV bar carries many generators so
    # it spans the whole site. The 220 kV line to a far site would have to
    # cut through the 400 or the 110 kV bar: the 220 kV bar is lengthened
    # and the line leaves from the part that sticks out.
    st = GuiSystemState()
    st.nodes = [GuiNode(index=0, name="A"), GuiNode(index=1, name="B")]
    st.buses = {
        "a400": _bus("a400", 0, 400, 22.0, -81.0),
        "a220": _bus("a220", 0, 220, 22.0005, -81.0),
        "a110": _bus("a110", 0, 110, 22.001, -81.0),
        "b220": _bus("b220", 1, 220, 22.0, -79.0),
    }
    st.transformers = [
        GuiTransformer(name="T1", from_bus="a400", to_bus="a220", rated_power_mva=500.0),
        GuiTransformer(name="T2", from_bus="a220", to_bus="a110", rated_power_mva=300.0),
    ]
    st.transmission_lines = [GuiTransmissionLine(
        line_id="L", from_bus="a220", to_bus="b220", capacity_mw=300.0, voltage_kv=220.0)]
    st.generators = {
        f"g{i}": GuiGeneratorInstance(instance_id=f"g{i}", unit_key="u", name=f"G{i}",
                                      gen_type="Non-renewable", fuel="Gas",
                                      bus="a110", rated_power=50.0)
        for i in range(8)
    }
    out = gb.build_elk_graph(st)
    by_id = {c["id"]: c for c in out["elkGraph"]["children"]}
    eid = next(e["id"] for e in out["elkGraph"]["edges"]
               if e["properties"].get("edgeType") == "transmission")
    pts, _e = _edge_points(out, eid)
    for bid in ("bus_a400", "bus_a110"):
        c = by_id[bid]
        bar = (c["x"], c["y"], c["x"] + c["width"], c["y"] + gb._BUS_H)
        for p, q in zip(pts, pts[1:]):
            seg = (min(p["x"], q["x"]), min(p["y"], q["y"]),
                   max(p["x"], q["x"]) + 1e-9, max(p["y"], q["y"]) + 1e-9)
            assert not _overlap(seg, bar), bid


def test_without_coordinates_the_node_grid_is_kept():
    st = _geo_state()
    for b in st.buses.values():
        b.latitude = b.longitude = 0.0
    out = gb.build_elk_graph(st)
    assert {g["nodeId"] for g in out["nodeGroups"]} == {0, 1}
    assert all("siteId" not in g for g in out["nodeGroups"])
