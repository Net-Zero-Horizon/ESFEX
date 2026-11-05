"""Convert a GuiSystemState into an SLD layout descriptor.

Every ``GuiBus`` is a horizontal bar and every transmission line /
transformer / converter is an edge between two bars. Equipment hangs below
its bar and is arranged by the JavaScript renderer. Positions and edge
routes are computed here; the renderer draws them as given.

Two layouts:

* **Site layout** (buses have map coordinates): buses are grouped into
  sites — substations and line junctions (:mod:`.substations`). Each site is
  laid out as a compact block with the transformer-tree grid below (HV bars
  above LV bars, transformers as verticals). The blocks are placed on a cell
  grid following the geography (:mod:`.placement`) and the lines between
  them are routed along the clear channels of that grid
  (:mod:`.channel_router`).
* **Node grid** (no usable coordinates): one column per node, rows by
  transformer depth (:func:`_apply_grid_layout`).

The output is a single JSON dict:

- ``"elkGraph"`` — buses (``children``, with x/y) and edges (with sections)
- ``"busEquipment"`` — ``{ bus_id: [ {equipment metadata}, ... ] }``
- ``"nodeGroups"`` — frames drawn around buses: ``[ { nodeId, name,
  busIds, siteId? }, ... ]`` (one per substation in the site layout, one
  per node in the node grid)
- ``"constants"`` — sizes shared with the renderer
"""

from __future__ import annotations

import logging
import math
from typing import Any

from esfex.visualization.data.gui_model import GuiSystemState
from esfex.visualization.sld.router import GridRouter, Obstacles
from esfex.visualization.sld.voltage_colors import get_voltage_color

log = logging.getLogger(__name__)

# ── Sizing constants ──
# Bus bars are drawn HORIZONTAL (with merge-by-substation collapsing
# many GuiBus into one wide bar per voltage level).
_BUS_H = 6               # Bus bar thickness
_BUS_MIN_LEN = 200       # Minimum bus bar length (horizontal extent)
_BUS_PER_EQUIP = 70      # Extra bar length per attached equipment slot
_BUS_PER_EDGE = 28       # Extra bar length per line terminal slot
_BUS_PER_XFMR = 100      # Extra bar length per transformer terminal (its
                         # vertical symbol + capacity label need more room, so
                         # parallel transformers don't overlap their labels)
_BUS_LABEL_PAD = 24      # Bottom/top label margin
_EQUIP_SIZE = 36         # Symbol diameter
_EQUIP_SPACING = 70      # Spacing between equipment slots along the bar
_STUB_LEN = 36           # Vertical stub length (bar → equipment center)

# ── Deterministic layout constants (PowerFactory-style grid) ──
_ROW_SPACING_Y = 280     # Minimum vertical distance between voltage rows
_COL_GAP_X = 100         # Horizontal gap between adjacent node columns
_INTRA_NODE_GAP_X = 24   # Gap between adjacent bars at the same
                         # (substation, voltage) — only used when a
                         # node has multiple groups at one voltage
                         # (rare with merge enabled).
_LANE_STEP_Y = 28        # Vertical spacing between adjacent edge lanes
                         # within a row gap (each edge gets its own lane Y
                         # for the horizontal segment, eliminating overlap)
_LANE_MARGIN_Y = 30      # Margin from row edges to first/last lane


def build_elk_graph(
    state: GuiSystemState,
    theme_colors: dict | None = None,
    filter_substation: int | None = None,
) -> dict:
    """Build the SLD layout descriptor from the current system state.

    The SLD is a single, electrically faithful schematic: **every** ``GuiBus``
    renders as its own bar and **every** transformer / line / converter is its
    own connection between its two real buses. There is no level aggregation —
    buses are never pooled and parallel-looking elements are never collapsed
    into an ``N×`` symbol, so the diagram always matches the actual topology
    (each transformer visibly bridges its specific HV and LV bus).

    Parameters
    ----------
    state : GuiSystemState
        The system to render.
    theme_colors : dict, optional
        Per-element-type color overrides.
    filter_substation : int, optional
        If provided, only buses with ``parent_node == filter_substation``
        are included.

    Returns
    -------
    dict
        ``{"elkGraph": ..., "busEquipment": ..., "nodeGroups": ...,
           "constants": ...}``
    """
    colors = theme_colors or {}

    # ── Map bus_id → parent_node for topology validation ──
    if filter_substation is not None:
        included_buses = {
            bus.bus_id for bus in state.buses.values()
            if bus.parent_node == filter_substation
        }
    else:
        included_buses = set(state.buses.keys())
    bus_to_node: dict[str, int] = {
        bus.bus_id: bus.parent_node for bus in state.buses.values()
        if bus.bus_id in included_buses
    }

    # ── One bar per GuiBus (no merging) ──
    bus_to_group: dict[str, str] = {}
    group_to_buses: dict[str, list[str]] = {}
    group_meta: dict[str, dict] = {}
    _node_by_idx = {n.index: n for n in state.nodes}

    for bus in state.buses.values():
        if bus.bus_id not in included_buses:
            continue
        gid = f"bus_{bus.bus_id}"
        label = bus.name or bus.bus_id
        bus_to_group[bus.bus_id] = gid
        group_to_buses.setdefault(gid, []).append(bus.bus_id)
        group_meta[gid] = {
            "parent_node": bus.parent_node,
            "voltage_kv": bus.voltage_kv,
            "name": label,
            "color": get_voltage_color(bus.voltage_kv),
            "n_buses": 1,
            "lat": float(bus.latitude or 0.0),
            "lon": float(bus.longitude or 0.0),
        }

    # ── Collect equipment per merged bar (group_id) ──
    bus_equipment: dict[str, list[dict]] = {gid: [] for gid in group_meta}

    # Generators
    for gen in state.generators.values():
        gid = bus_to_group.get(gen.bus)
        if gid is None:
            continue
        sym = "gen-renewable" if gen.gen_type == "Renewable" else "gen-nonrenewable"
        color = colors.get(sym, "#27AE60" if sym == "gen-renewable" else "#7F8C8D")
        sublabel = f"{gen.rated_power:.0f} MW" if gen.rated_power else ""
        bus_equipment[gid].append({
            "elementType": "generator",
            "elementId": gen.instance_id,
            "label": gen.name,
            "sublabel": sublabel,
            "symbolType": sym,
            "color": color,
            "fuel": gen.fuel,
        })

    # Batteries
    for bat in state.batteries.values():
        gid = bus_to_group.get(bat.bus)
        if gid is None:
            continue
        color = colors.get("battery", "#F39C12")
        sublabel = f"{bat.capacity:.0f} MWh" if bat.capacity else ""
        bus_equipment[gid].append({
            "elementType": "battery",
            "elementId": bat.instance_id,
            "label": bat.name,
            "sublabel": sublabel,
            "symbolType": "battery",
            "color": color,
        })

    # Electrolyzers
    for el in state.electrolyzers.values():
        gid = bus_to_group.get(el.bus)
        if gid is None:
            continue
        color = colors.get("electrolyzer", "#16A085")
        sublabel = f"{el.rated_power:.0f} MW" if el.rated_power else ""
        bus_equipment[gid].append({
            "elementType": "electrolyzer",
            "elementId": el.instance_id,
            "label": el.name,
            "sublabel": sublabel,
            "symbolType": "electrolyzer",
            "color": color,
        })

    # Demand loads — attach the node's load to the LOWEST-voltage merged
    # bar of that node (where distribution feeders connect in real grids).
    node_buses: dict[int, list[str]] = {}
    for bus in state.buses.values():
        node_buses.setdefault(bus.parent_node, []).append(bus.bus_id)
    for node in state.nodes:
        node_buses.setdefault(node.index, [])
    for node in state.nodes:
        if not (node.demand and (node.demand.peak_mw or 0) > 0):
            continue
        # Find lowest-voltage group for this node
        node_groups = [
            (gid, m) for gid, m in group_meta.items()
            if m["parent_node"] == node.index
        ]
        if not node_groups:
            continue
        node_groups.sort(key=lambda x: x[1]["voltage_kv"])
        target_gid = node_groups[0][0]
        bus_equipment[target_gid].append({
            "elementType": "load",
            "elementId": f"load_node_{node.index}",
            "label": f"Load {node.name}",
            "sublabel": f"{node.demand.peak_mw:.0f} MW",
            "symbolType": "load",
            "color": colors.get("load", "#E67E22"),
        })

    # ── One edge per physical element (no aggregation) ──
    # Every transformer / line / converter is its own connection between its
    # two real buses, so the schematic stays electrically faithful (parallel
    # elements are NOT collapsed into an N× symbol).
    valid_bus_ids = included_buses

    edge_records: list[dict] = []
    edge_count: dict[str, int] = {gid: 0 for gid in group_meta}
    xfmr_count: dict[str, int] = {gid: 0 for gid in group_meta}  # transformer terminals

    def _add_edge(src_gid: str, tgt_gid: str, etype: str,
                   voltage: float, capacity: float, element_id: str) -> None:
        if src_gid == tgt_gid:
            return  # both ends on the same bus → nothing to draw
        edge_records.append({
            "src": src_gid, "tgt": tgt_gid, "etype": etype,
            "voltage": float(voltage or 0), "capacity": float(capacity or 0),
            "element_id": str(element_id),
        })
        edge_count[src_gid] += 1
        edge_count[tgt_gid] += 1
        if etype == "transformer":
            xfmr_count[src_gid] += 1
            xfmr_count[tgt_gid] += 1

    # ── Geographic connectivity: bus --line--> transformer --line--> bus ──
    # A transformer's two windings are wired to buses by the lines that
    # TERMINATE at it (from_endpoint/to_endpoint == that transformer). Those
    # lines are the transformer's stubs — consumed here, not drawn as separate
    # transmission — and the transformer bridges the buses on their far ends.
    # (A transformer's own from_bus/to_bus fields can disagree with the drawn
    # topology, so the lines — the geographic network — are the source of truth.)
    tr_terminals: dict[str, list[str]] = {}
    consumed_lines: set[str] = set()
    for line in state.transmission_lines:
        for ep, far_bus in ((line.from_endpoint, line.to_bus),
                            (line.to_endpoint, line.from_bus)):
            if (ep and ep.element_type == "transformer"
                    and far_bus in valid_bus_ids):
                tr_terminals.setdefault(str(ep.element_id), []).append(far_bus)
                consumed_lines.add(line.line_id)

    # Transmission lines: every line that is NOT a transformer stub, bus→bus.
    for line in state.transmission_lines:
        if line.line_id in consumed_lines:
            continue
        if (line.from_bus not in valid_bus_ids
                or line.to_bus not in valid_bus_ids
                or line.from_bus == line.to_bus):
            continue
        sg = bus_to_group.get(line.from_bus)
        tg = bus_to_group.get(line.to_bus)
        if not sg or not tg:
            continue
        _add_edge(
            sg, tg, "transmission",
            line.voltage_kv or 220.0, line.capacity_mw or 0,
            line.line_id,
        )

    # Transformers: bridge the two buses wired to it via its stub lines; fall
    # back to from_bus/to_bus when the geographic stubs aren't both available.
    for i, tr in enumerate(state.transformers):
        seen: list[str] = []
        for b in tr_terminals.get(str(i), []):
            if b not in seen:
                seen.append(b)
        fb, tb = (seen[0], seen[1]) if len(seen) >= 2 else (tr.from_bus, tr.to_bus)
        if (fb not in valid_bus_ids or tb not in valid_bus_ids or fb == tb):
            continue
        sg = bus_to_group.get(fb)
        tg = bus_to_group.get(tb)
        if not sg or not tg:
            continue
        if group_meta[sg]["parent_node"] != group_meta[tg]["parent_node"]:
            continue
        _add_edge(sg, tg, "transformer", 0.0, tr.rated_power_mva or 0, i)
    for i, conv in enumerate(state.acdc_converters):
        if (conv.from_bus not in valid_bus_ids
                or conv.to_bus not in valid_bus_ids
                or conv.from_bus == conv.to_bus):
            continue
        sg = bus_to_group.get(conv.from_bus)
        tg = bus_to_group.get(conv.to_bus)
        if not sg or not tg:
            continue
        if group_meta[sg]["parent_node"] != group_meta[tg]["parent_node"]:
            continue
        _add_edge(
            sg, tg, "converter",
            0.0, conv.rated_power_mva or 0,
            f"acdc_{i}",
        )
    for i, conv in enumerate(state.freq_converters):
        if (conv.from_bus not in valid_bus_ids
                or conv.to_bus not in valid_bus_ids
                or conv.from_bus == conv.to_bus):
            continue
        sg = bus_to_group.get(conv.from_bus)
        tg = bus_to_group.get(conv.to_bus)
        if not sg or not tg:
            continue
        if group_meta[sg]["parent_node"] != group_meta[tg]["parent_node"]:
            continue
        _add_edge(
            sg, tg, "converter",
            0.0, conv.rated_power_mva or 0,
            f"freq_{i}",
        )

    # ── Build flat ELK graph: one bar per bus ──
    # Horizontal bar: width = bar_len, height = bar + stub + equipment.
    elk_children: list[dict] = []
    for gid, meta in group_meta.items():
        n_eq = len(bus_equipment.get(gid, []))
        n_edges = edge_count.get(gid, 0)
        n_xf = xfmr_count.get(gid, 0)
        bar_len = max(
            _BUS_MIN_LEN,
            n_eq * _BUS_PER_EQUIP,
            # transformer terminals need more room than line terminals
            n_xf * _BUS_PER_XFMR + (n_edges - n_xf) * _BUS_PER_EDGE,
        )
        equip_extent = _STUB_LEN + _EQUIP_SIZE + 24 if n_eq > 0 else 12
        bus_w = bar_len + 40   # margin for end labels
        bus_h = _BUS_H + equip_extent + _BUS_LABEL_PAD
        elk_children.append({
            "id": gid,
            "width": bus_w,
            "height": bus_h,
            "properties": {
                "elementType": "bus",
                "elementId": gid,
                "voltageKv": meta["voltage_kv"],
                "color": meta["color"],
                "label": meta["name"],
                "parentNode": meta["parent_node"],
                "edgeCount": n_edges,
                "nMergedBuses": meta["n_buses"],
                "orientation": 0,  # horizontal bar
            },
        })

    # ── Emit one ELK edge per physical element ──
    elk_edges: list[dict] = []
    for rec in edge_records:
        etype = rec["etype"]
        cap = rec["capacity"]
        # A transmission line must join two buses of the SAME voltage; if the
        # data connects different voltages (e.g. 110↔138), flag it so the SLD
        # marks the line as a data inconsistency instead of drawing it as normal.
        sv = group_meta.get(rec["src"], {}).get("voltage_kv", 0.0)
        tv = group_meta.get(rec["tgt"], {}).get("voltage_kv", 0.0)
        mismatch = (etype == "transmission" and sv and tv and sv != tv)
        if etype == "transmission":
            color = get_voltage_color(rec["voltage"])
            label = (f"{cap:.0f} MW  ⚠ {sv:g}/{tv:g} kV" if mismatch
                     else f"{cap:.0f} MW")
        elif etype == "transformer":
            color = colors.get("transformer", "#9B59B6")
            label = f"{cap:.0f} MVA"
        else:
            color = colors.get("acdc_converter", "#2980B9")
            label = f"{cap:.0f} MVA"
        elk_edges.append({
            "id": f"{etype}_{rec['element_id']}_{rec['src']}_{rec['tgt']}",
            "sources": [rec["src"]],
            "targets": [rec["tgt"]],
            "properties": {
                "elementType": etype,
                "elementId": rec["element_id"],
                "edgeType": etype,
                "voltageKv": rec["voltage"],
                "capacityMw": cap,
                "nCircuits": 1,
                "color": "#E74C3C" if mismatch else color,
                "voltageMismatch": bool(mismatch),
                "label": label,
            },
        })

    # ── Node groups (for visual background grouping) ──
    # Each visual rectangle wraps all merged bars belonging to one
    # geographic node (substation), regardless of voltage.
    node_to_groups: dict[int, list[str]] = {}
    for gid, meta in group_meta.items():
        node_to_groups.setdefault(meta["parent_node"], []).append(gid)
    groups: list[dict] = []
    for node in state.nodes:
        bus_ids = node_to_groups.get(node.index, [])
        if bus_ids:
            groups.append({
                "nodeId": node.index,
                "name": node.name or f"Node {node.index}",
                "busIds": bus_ids,
            })

    # ── Layout ──
    # With bus coordinates, the diagram is laid out by SITE: buses are
    # grouped into substations / junctions, each drawn as a compact block
    # (the transformer-tree layout below, per block), and the blocks are
    # placed on a grid following the system's geography. Without usable
    # coordinates, fall back to the single voltage-row × node-column grid.
    positions = _bus_positions_for_layout(state, group_meta, edge_records)
    if positions is not None and elk_children:
        groups = _apply_site_layout(
            elk_children, elk_edges, bus_equipment, group_meta, positions,
            {n.index: (n.name or f"Node {n.index}") for n in state.nodes},
        )
    else:
        _apply_grid_layout(elk_children, elk_edges, state, bus_equipment)
    elk_graph: dict[str, Any] = {
        "id": "root",
        "children": elk_children,
        "edges": elk_edges,
        # Tell the JS side to skip elk.layout() — positions are final.
        "precomputedLayout": True,
    }

    log.info(
        "SLD graph: %d buses, %d edges (%d lines input, %d transformers, "
        "%d acdc, %d freq converters)",
        len(elk_children), len(elk_edges),
        len(state.transmission_lines), len(state.transformers),
        len(state.acdc_converters), len(state.freq_converters),
    )
    for edge in elk_edges:
        p = edge.get("properties", {})
        log.debug(
            "  edge %s: %s → %s  [%s, %s]",
            edge["id"], edge["sources"][0], edge["targets"][0],
            p.get("edgeType", "?"), p.get("label", ""),
        )

    return {
        "elkGraph": elk_graph,
        "busEquipment": bus_equipment,
        "nodeGroups": groups,
        "constants": {
            "busH": _BUS_H,
            "stubLen": _STUB_LEN,
            "equipSize": _EQUIP_SIZE,
            "equipSpacing": _EQUIP_SPACING,
        },
    }


# ── Site (substation) layout ────────────────────────────────────────

# Inside a substation block the voltage rows sit closer than in the legacy
# whole-system grid: only the transformer symbol and its label live there.
_SITE_ROW_SPACING_Y = 150
# Frame drawn around a substation by the JS renderer (_drawNodeGroup):
# 18 px bar label + 30 px padding + 28 px title above, 30 px on the sides
# and below. The block footprint must include it.
_FRAME_TOP = 76
_FRAME_SIDE = 30
_FRAME_BOTTOM = 30
_JUNCTION_PAD = 12
_JUNCTION_LABEL = 22
_TERM_EDGE_PAD = 16      # keep line terminals this far from the bar ends
_TERM_CLEAR = 12         # ... and this far from other stubs on the same face
_EQ_PITCH_JS = 72.0      # equipment pitch used by the JS renderer


def _bus_positions_for_layout(state, group_meta, edge_records):
    """``{gid: (lat, lon)}`` for every bar, or None (no usable geography)."""
    from esfex.visualization.sld.substations import bus_positions

    if not group_meta:
        return None
    coords = {g: (m.get("lat", 0.0), m.get("lon", 0.0)) for g, m in group_meta.items()}
    node_of = {g: m["parent_node"] for g, m in group_meta.items()}
    centroids = {n.index: (float(n.centroid_lat or 0.0), float(n.centroid_lng or 0.0))
                 for n in state.nodes}
    nbrs: dict[str, set[str]] = {}
    for r in edge_records:
        nbrs.setdefault(r["src"], set()).add(r["tgt"])
        nbrs.setdefault(r["tgt"], set()).add(r["src"])
    return bus_positions(coords, node_of, centroids, nbrs)


def _equip_stub_xs(child: dict, n_eq: int) -> list[float]:
    """X of each equipment stub below a horizontal bar (as the JS draws)."""
    if n_eq <= 0:
        return []
    start = child["x"] + (child["width"] - n_eq * _EQ_PITCH_JS) / 2.0 + _EQ_PITCH_JS / 2.0
    return [start + i * _EQ_PITCH_JS for i in range(n_eq)]


def _block_bbox(children, edges, bus_equipment, framed: bool):
    """Footprint (x0, y0, x1, y1) of a laid-out block, frame included."""
    x0 = y0 = math.inf
    x1 = y1 = -math.inf
    for c in children:
        n_eq = len(bus_equipment.get(c["id"], []))
        below = (_BUS_H + _STUB_LEN + _EQUIP_SIZE + 20) if n_eq else (_BUS_H + 10)
        x0 = min(x0, c["x"])
        x1 = max(x1, c["x"] + c["width"])
        y0 = min(y0, c["y"] - 18)
        y1 = max(y1, c["y"] + below)
    for e in edges:
        for sec in e.get("sections", []):
            for p in [sec["startPoint"], sec["endPoint"]] + sec.get("bendPoints", []):
                x0 = min(x0, p["x"] - 10)
                x1 = max(x1, p["x"] + 10)
                y0 = min(y0, p["y"] - 10)
                y1 = max(y1, p["y"] + 10)
    if framed:
        return (x0 - _FRAME_SIDE, y0 - (_FRAME_TOP - 18),
                x1 + _FRAME_SIDE, y1 + _FRAME_BOTTOM)
    return (x0 - _JUNCTION_PAD, y0 - (_JUNCTION_LABEL - 18),
            x1 + _JUNCTION_PAD, y1 + _JUNCTION_PAD)


def _translate(children, edges, dx, dy):
    for c in children:
        c["x"] = c["x"] + dx
        c["y"] = c["y"] + dy
    for e in edges:
        for sec in e.get("sections", []):
            for p in [sec["startPoint"], sec["endPoint"]] + sec.get("bendPoints", []):
                p["x"] += dx
                p["y"] += dy


def _spread_terminals(lo, hi, occupied, k):
    """``k`` increasing X positions on [lo, hi] spread over the stretches
    that stay clear of the ``occupied`` X positions."""
    if k <= 0:
        return []
    if hi <= lo:
        return [(lo + hi) / 2.0] * k
    free = [(lo, hi)]
    for x in sorted(occupied):
        a, b = x - _TERM_CLEAR, x + _TERM_CLEAR
        nxt = []
        for f0, f1 in free:
            if b <= f0 or a >= f1:
                nxt.append((f0, f1))
                continue
            if a > f0:
                nxt.append((f0, a))
            if b < f1:
                nxt.append((b, f1))
        free = nxt
    total = sum(f1 - f0 for f0, f1 in free)
    if total < 1.0:
        step = (hi - lo) / (k + 1)
        return [lo + step * (i + 1) for i in range(k)]
    out = []
    for i in range(k):
        d = total * (i + 1) / (k + 1)
        for f0, f1 in free:
            if d <= f1 - f0:
                out.append(f0 + d)
                break
            d -= f1 - f0
        else:
            out.append(free[-1][1])
    return out


def _apply_site_layout(
    elk_children: list[dict],
    elk_edges: list[dict],
    bus_equipment: dict[str, list[dict]],
    group_meta: dict[str, dict],
    positions: dict[str, tuple[float, float]],
    node_names: dict[int, str],
) -> list[dict]:
    """In-place site layout; returns the substation frames (``nodeGroups``)."""
    from types import SimpleNamespace

    from esfex.visualization.sld.channel_router import LineSpec, route_lines
    from esfex.visualization.sld.placement import Block, place_blocks
    from esfex.visualization.sld.substations import detect_sites

    edge_ends = [(e["sources"][0], e["targets"][0],
                  e["properties"].get("edgeType", "transmission")) for e in elk_edges]
    equipped = {g for g, eq in bus_equipment.items() if eq}
    sites = detect_sites(group_meta, edge_ends, positions, equipped, node_names)
    site_of = {g: s.sid for s in sites for g in s.bus_gids}
    by_id = {c["id"]: c for c in elk_children}

    intra: dict[str, list[dict]] = {s.sid: [] for s in sites}
    inter: list[dict] = []
    for e in elk_edges:
        a, b = site_of.get(e["sources"][0]), site_of.get(e["targets"][0])
        if a is not None and a == b:
            intra[a].append(e)
        else:
            inter.append(e)

    inter_count: dict[str, int] = {}
    for e in inter:
        for g in (e["sources"][0], e["targets"][0]):
            inter_count[g] = inter_count.get(g, 0) + 1

    # 1. Lay out each block on its own, at the origin.
    blocks: list[Block] = []
    bbox: dict[str, tuple[float, float, float, float]] = {}
    for s in sites:
        kids = [by_id[g] for g in s.bus_gids if g in by_id]
        if s.kind == "junction":
            c = kids[0]
            n = inter_count.get(c["id"], 0)
            c["width"] = float(max(80, _BUS_PER_EDGE * n + 2 * _TERM_EDGE_PAD))
            c["properties"]["junction"] = True
        sub_eq = {g: bus_equipment.get(g, []) for g in s.bus_gids}
        _apply_grid_layout(kids, intra[s.sid], SimpleNamespace(nodes=[]), sub_eq,
                           row_spacing=_SITE_ROW_SPACING_Y)
        bb = _block_bbox(kids, intra[s.sid], bus_equipment, s.kind != "junction")
        bbox[s.sid] = bb
        blocks.append(Block(s.sid, bb[2] - bb[0], bb[3] - bb[1], s.lat, s.lon))

    links = sorted({tuple(sorted((site_of[e["sources"][0]], site_of[e["targets"][0]])))
                    for e in inter
                    if e["sources"][0] in site_of and e["targets"][0] in site_of})
    placement = place_blocks(blocks, links)

    # 2. Move every block to its place.
    for s in sites:
        bx, by = placement.xy[s.sid]
        x0, y0, _x1, _y1 = bbox[s.sid]
        kids = [by_id[g] for g in s.bus_gids if g in by_id]
        _translate(kids, intra[s.sid], bx - x0, by - y0)

    # 3. Terminals for the lines between blocks. A bar in the block's top
    #    row connects upwards and any other bar downwards; a bar alone in
    #    its block with nothing hanging below it (a junction, a bare bus)
    #    connects on whichever side faces the far end. Terminals are spread
    #    over the face, clear of the stubs already on it, ordered by the
    #    direction of the far end so lines leaving left start on the left.
    site_top: dict[str, float] = {}
    site_bottom: dict[str, float] = {}
    for s in sites:
        ys = [by_id[g]["y"] for g in s.bus_gids if g in by_id]
        site_top[s.sid] = min(ys)
        site_bottom[s.sid] = max(ys)

    block_cy = {s.sid: placement.xy[s.sid][1] + (bbox[s.sid][3] - bbox[s.sid][1]) / 2.0
                for s in sites}

    def face_up(gid, other_gid):
        c = by_id[gid]
        sid = site_of[gid]
        top = abs(c["y"] - site_top[sid]) < 0.5
        bottom = abs(c["y"] - site_bottom[sid]) < 0.5
        if top and bottom and not bus_equipment.get(gid):
            return block_cy[site_of[other_gid]] < c["y"]
        return top

    occupied: dict[tuple[str, bool], list[float]] = {}
    for s in sites:
        for e in intra[s.sid]:
            for sec in e.get("sections", []):
                for p in (sec["startPoint"], sec["endPoint"]):
                    for g in s.bus_gids:
                        c = by_id[g]
                        if not (c["x"] - 1 <= p["x"] <= c["x"] + c["width"] + 1):
                            continue
                        if abs(p["y"] - c["y"]) < 0.5:
                            occupied.setdefault((g, True), []).append(p["x"])
                        elif abs(p["y"] - (c["y"] + _BUS_H)) < 0.5:
                            occupied.setdefault((g, False), []).append(p["x"])
    for g, c in by_id.items():
        occupied.setdefault((g, False), []).extend(
            _equip_stub_xs(c, len(bus_equipment.get(g, []))))

    ends_on: dict[tuple[str, bool], list[tuple[float, str, str]]] = {}
    for e in inter:
        s0, t0 = e["sources"][0], e["targets"][0]
        if s0 not in by_id or t0 not in by_id:
            continue
        cs, ct = by_id[s0], by_id[t0]
        ends_on.setdefault((s0, face_up(s0, t0)), []).append(
            (ct["x"] + ct["width"] / 2.0, e["id"], "s"))
        ends_on.setdefault((t0, face_up(t0, s0)), []).append(
            (cs["x"] + cs["width"] / 2.0, e["id"], "t"))
    term: dict[tuple[str, str], tuple[float, float, bool]] = {}
    for (g, up), lst in ends_on.items():
        c = by_id[g]
        lst.sort()
        xs = _spread_terminals(c["x"] + _TERM_EDGE_PAD, c["x"] + c["width"] - _TERM_EDGE_PAD,
                               occupied.get((g, up), []), len(lst))
        y = c["y"] if up else c["y"] + _BUS_H
        for (_ox, eid, end), x in zip(lst, xs):
            term[(eid, end)] = (x, y, up)

    # 4. Route the lines along the channels between blocks.
    specs = []
    for e in inter:
        if (e["id"], "s") not in term or (e["id"], "t") not in term:
            continue
        sx, sy, sup = term[(e["id"], "s")]
        tx, ty, tup = term[(e["id"], "t")]
        specs.append(LineSpec(e["id"], site_of[e["sources"][0]], site_of[e["targets"][0]],
                              sx, sy, sup, tx, ty, tup))
    routes = route_lines(placement, specs)
    for e in inter:
        pts = routes.get(e["id"])
        if not pts:
            continue
        e["properties"]["precomputedRoute"] = True
        e["sections"] = [{
            "startPoint": {"x": pts[0][0], "y": pts[0][1]},
            "endPoint": {"x": pts[-1][0], "y": pts[-1][1]},
            "bendPoints": [{"x": p[0], "y": p[1]} for p in pts[1:-1]],
        }]

    for c in elk_children:
        c["x"] = float(c["x"])
        c["y"] = float(c["y"])

    groups = []
    for s in sites:
        if s.kind != "substation":
            continue
        groups.append({
            "nodeId": s.node,
            "siteId": s.sid,
            "name": s.name,
            "busIds": list(s.bus_gids),
        })
    log.info("SLD site layout: %d sites (%d junctions) on a %d×%d grid",
             len(sites), sum(1 for s in sites if s.kind == "junction"),
             placement.n_cols, placement.n_rows)
    return groups


def _apply_grid_layout(
    elk_children: list[dict],
    elk_edges: list[dict],
    state: GuiSystemState,
    bus_equipment: dict[str, list[dict]] | None = None,
    row_spacing: float = _ROW_SPACING_Y,
) -> None:
    """In-place: assign x/y/sections using a voltage-row × node-column grid.

    Buses are placed at the intersection of their voltage row (HV at the
    top, LV at the bottom) and their parent-node column (left-to-right
    by ``node.index``). Equipment columns hanging below each bus need
    vertical clearance — row spacing scales with the tallest bus per row.
    Edges get simple orthogonal routing with two bend points; the JS side
    then spreads incoming connections along each bar in Phase 2.
    """
    import math

    # ── 1. Transformer-tree depth → row. A bus that is the LV side of a
    #      transformer sits one row BELOW its HV parent; roots (buses that are
    #      no transformer's LV side) sit at the top. So every transformer
    #      connects adjacent rows — a clean short vertical between its two bars
    #      — instead of spanning the global voltage stack and routing around
    #      intermediate bars. Line-connected buses share their neighbour's row.
    volt = {c["id"]: float(c["properties"].get("voltageKv", 0.0) or 0.0)
            for c in elk_children}
    # Each bus may be the LV side of several transformers; collect ALL its
    # higher-voltage parents and use LONGEST-PATH layering (depth = 1 + the max
    # parent depth). This keeps the most transformers adjacent — e.g. a 110 kV
    # bus fed by both a 220 and a 400 sits below the 220 (deepest), so the
    # 220→110 transformer is a 1-row drop; only the 400→110 'shortcut' spans.
    parents: dict[str, list[str]] = {}
    for edge in elk_edges:
        if edge["properties"].get("edgeType") != "transformer":
            continue
        a, b = edge["sources"][0], edge["targets"][0]
        if volt.get(a, 0.0) == volt.get(b, 0.0):
            continue                       # same voltage → no parent relation
        hi, lo = (a, b) if volt.get(a, 0.0) > volt.get(b, 0.0) else (b, a)
        parents.setdefault(lo, []).append(hi)
    depth: dict[str, int] = {}
    parent_of: dict[str, str] = {}         # layout parent = the deepest one

    def _depth_of(g: str, seen: frozenset) -> int:
        if g in depth:
            return depth[g]
        ps = [p for p in parents.get(g, []) if p not in seen]
        if not ps:
            depth[g] = 0
            return 0
        best_p, best_d = None, -1
        for p in ps:
            d = _depth_of(p, seen | {g})
            if d > best_d:
                best_d, best_p = d, p
        depth[g] = best_d + 1
        parent_of[g] = best_p
        return depth[g]

    for c in elk_children:
        _depth_of(c["id"], frozenset())
    row_of: dict[str, int] = {c["id"]: depth[c["id"]] for c in elk_children}

    # ── Group buses by (parent_node, row) ──
    buses_by_node_volt: dict[tuple[int, int], list[dict]] = {}
    rows_seen: set[int] = set()
    nodes_seen: set[int] = set()
    for child in elk_children:
        n = int(child["properties"].get("parentNode", 0) or 0)
        r = row_of[child["id"]]
        buses_by_node_volt.setdefault((n, r), []).append(child)
        rows_seen.add(r)
        nodes_seen.add(n)

    sorted_voltages = sorted(rows_seen)          # row indices, ascending (top→down)
    v_to_row = {r: r for r in rows_seen}         # row key == row index now

    # Node columns — preserve state.nodes order; append any orphaned IDs
    state_node_order = [n.index for n in state.nodes if n.index in nodes_seen]
    for n in nodes_seen:
        if n not in state_node_order:
            state_node_order.append(n)

    # ── 2. Row heights (the tallest bar in each row). ──
    row_h: dict[int, float] = {}
    for c in elk_children:
        r = row_of[c["id"]]
        row_h[r] = max(row_h.get(r, 0.0), c["height"])

    # ── 3. X via a per-node TRANSFORMER-TREE layout. Each HV parent bar is
    #      centred over — and widened to span — the LV children it feeds, so a
    #      transformer drops straight down to its child (X-aligned). Leaves take
    #      sequential slots; substations are placed left to right. ──
    from collections import defaultdict as _dd
    children_of: dict[str, list[str]] = _dd(list)
    for _child, _par in parent_of.items():
        children_of[_par].append(_child)
    bus_w0 = {c["id"]: c["width"] for c in elk_children}
    node_of = {c["id"]: int(c["properties"].get("parentNode", 0) or 0)
               for c in elk_children}
    bus_left: dict[str, float] = {}
    bus_wide: dict[str, float] = {}

    node_x_left: dict[int, float] = {}
    node_col_w: dict[int, float] = {}
    cursor_x = 0.0
    for node_idx in state_node_order:
        # Ordered list (children order) next to the set, so roots are placed
        # in the same order on every run (set order follows str hashing).
        nlist = [g for g, nd in node_of.items() if nd == node_idx]
        nset = set(nlist)
        leaf_cursor = [cursor_x]
        placed: set[str] = set()

        def _place(gid: str, nset=nset, placed=placed, leaf_cursor=leaf_cursor):
            placed.add(gid)
            kids = [k for k in children_of.get(gid, [])
                    if k in nset and k not in placed]
            if not kids:
                xl = leaf_cursor[0]
                bus_left[gid] = xl
                bus_wide[gid] = bus_w0[gid]
                leaf_cursor[0] += bus_w0[gid] + _INTRA_NODE_GAP_X
                return xl, xl + bus_w0[gid]
            spans = [_place(k) for k in kids]
            left = min(s[0] for s in spans)
            right = max(s[1] for s in spans)
            w = max(bus_w0[gid], right - left)
            cx = (left + right) / 2.0
            xl = cx - w / 2.0
            bus_left[gid] = xl
            bus_wide[gid] = w
            return xl, xl + w

        roots = [g for g in nlist
                 if g not in parent_of or parent_of[g] not in nset]
        for r in roots:
            if r not in placed:
                _place(r)
        for g in nlist:                      # leftover (cycles / orphans)
            if g not in placed:
                _place(g)

        if nset:
            nl = min(bus_left[g] for g in nset)
            nr = max(bus_left[g] + bus_wide[g] for g in nset)
        else:
            nl, nr = cursor_x, cursor_x + _BUS_MIN_LEN
        node_x_left[node_idx] = nl
        node_col_w[node_idx] = max(nr - nl, _BUS_MIN_LEN)
        cursor_x = nr + _COL_GAP_X

    # ── 4. Y per row (cumulative; 5b later grows gaps by lane demand). ──
    row_y: dict[int, float] = {}
    cursor_y = 0.0
    sorted_v_list = list(sorted_voltages)
    for i, r in enumerate(sorted_v_list):
        row_y[r] = cursor_y
        cursor_y += row_h.get(r, 0.0) + (
            row_spacing if i < len(sorted_v_list) - 1 else 0.0)

    # ── 5. Apply x / width / y to every bus. ──
    for c in elk_children:
        g = c["id"]
        c["x"] = bus_left.get(g, 0.0)
        c["width"] = bus_wide.get(g, c["width"])
        c["y"] = row_y[row_of[g]]

    # ── 7. Edge routing with per-edge lane assignment.
    #      Each inter-row edge gets its own horizontal lane Y in the
    #      gap between rows, assigned via greedy interval-coloring so
    #      edges with non-overlapping X-ranges share lanes (keeping the
    #      number of lanes low). Each edge exits its src bus's bottom
    #      face, drops to the lane, traverses horizontally, and climbs
    #      back up to the tgt bus's top face. ──
    bus_index = {c["id"]: c for c in elk_children}
    LANE_X_MARGIN = 16

    gap_edges: dict[tuple[int, int], list[tuple[float, float, dict]]] = {}
    same_row_edges: list[dict] = []
    for edge in elk_edges:
        src = bus_index.get(edge["sources"][0])
        tgt = bus_index.get(edge["targets"][0])
        if not src or not tgt:
            continue
        sr = row_of.get(edge["sources"][0], 0)
        tr_ = row_of.get(edge["targets"][0], 0)
        sx = src["x"] + src["width"] / 2
        tx = tgt["x"] + tgt["width"] / 2
        x_left = min(sx, tx)
        x_right = max(sx, tx)
        if sr == tr_:
            same_row_edges.append(edge)
            continue
        r_lo = min(sr, tr_)
        r_hi = max(sr, tr_)
        gap_edges.setdefault((r_lo, r_hi), []).append((x_left, x_right, edge))

    # Greedy interval coloring per gap
    edge_lane_idx: dict[str, int] = {}
    gap_lane_count: dict[tuple[int, int], int] = {}
    for (r_lo, r_hi), intervals in gap_edges.items():
        intervals.sort(key=lambda t: t[0])
        lane_ends: list[float] = []
        for x_left, x_right, e in intervals:
            assigned = -1
            for i, end in enumerate(lane_ends):
                if end + LANE_X_MARGIN < x_left:
                    assigned = i
                    break
            if assigned == -1:
                assigned = len(lane_ends)
                lane_ends.append(x_right)
            else:
                lane_ends[assigned] = x_right
            edge_lane_idx[e["id"]] = assigned
        gap_lane_count[(r_lo, r_hi)] = len(lane_ends)

    # Same-row (one-voltage) edges connect different node columns; colour
    # them into lanes too so parallel circuits at the same voltage don't all
    # stack on a single Y line (the previous behaviour).
    same_row_by_row: dict[int, list[tuple[float, float, dict]]] = {}
    for edge in same_row_edges:
        src = bus_index[edge["sources"][0]]
        tgt = bus_index[edge["targets"][0]]
        sx = src["x"] + src["width"] / 2
        tx = tgt["x"] + tgt["width"] / 2
        same_row_by_row.setdefault(row_of.get(edge["sources"][0], 0), []).append(
            (min(sx, tx), max(sx, tx), edge))
    same_row_lane_idx: dict[str, int] = {}
    same_row_lane_count: dict[int, int] = {}
    for i, intervals in same_row_by_row.items():
        intervals.sort(key=lambda t: t[0])
        sr_ends: list[float] = []
        for x_left, x_right, e in intervals:
            assigned = -1
            for k, end in enumerate(sr_ends):
                if end + LANE_X_MARGIN < x_left:
                    assigned = k
                    break
            if assigned == -1:
                assigned = len(sr_ends)
                sr_ends.append(x_right)
            else:
                sr_ends[assigned] = x_right
            same_row_lane_idx[e["id"]] = assigned
        same_row_lane_count[i] = len(sr_ends)

    # ── 5b. Adaptive row spacing (the refinement promised in step 5a).
    #       Grow each inter-row gap to fit BOTH the cross-row lanes passing
    #       through it and the same-row lanes that dip below the upper row,
    #       so edges stop cramming on one another (and on intermediate
    #       bars). Sparse gaps keep the compact default. Row Y is then
    #       recomputed and every bus re-placed at its new row. ──
    n_rows = len(sorted_v_list)
    top_r = sorted_v_list[0] if sorted_v_list else 0   # HV row routes lines ABOVE
    cross_need = [0] * n_rows                 # cross-row lanes through gap below row i
    for (r_lo, r_hi), n_lanes in gap_lane_count.items():
        for i in range(r_lo, r_hi):           # edge spans every consecutive gap
            cross_need[i] = max(cross_need[i], n_lanes)
    # The top row's same-row lines route ABOVE it (open space), so they don't
    # reserve room in the gap below.
    gap_need = [cross_need[i] + (0 if i == top_r else same_row_lane_count.get(i, 0))
                for i in range(n_rows)]

    cursor_y = 0.0
    for i, v in enumerate(sorted_v_list):
        ridx = v_to_row[v]
        row_y[ridx] = cursor_y
        if i < n_rows - 1:
            gap_h = max(row_spacing,
                        gap_need[ridx] * _LANE_STEP_Y + 2 * _LANE_MARGIN_Y)
            cursor_y += row_h[ridx] + gap_h
        else:
            cursor_y += row_h[ridx]

    for c in elk_children:
        c["y"] = row_y[row_of[c["id"]]]

    # Resolve lane Y per gap. Cross-row lanes are spread across the gap but
    # start BELOW the upper row's same-row band so the two lane families
    # don't overlap.
    edge_lane_y: dict[str, float] = {}
    for (r_lo, r_hi), n_lanes in gap_lane_count.items():
        gap_top = row_y[r_lo] + row_h[r_lo]
        gap_bottom = row_y[r_hi]
        # The top row routes its same-row lines above, so its gap below has no
        # same-row band to clear.
        sr_band = (0 if r_lo == top_r
                   else same_row_lane_count.get(r_lo, 0)) * _LANE_STEP_Y
        usable_top = gap_top + _LANE_MARGIN_Y + sr_band
        usable_h = max(_LANE_STEP_Y,
                       gap_bottom - usable_top - _LANE_MARGIN_Y)
        for x_left, x_right, e in gap_edges[(r_lo, r_hi)]:
            idx = edge_lane_idx[e["id"]]
            t = (idx + 0.5) / n_lanes if n_lanes else 0.5
            edge_lane_y[e["id"]] = usable_top + t * usable_h

    # Same-row edges: own lane band. The TOP row routes ABOVE its bars (clear
    # space at the top, away from the transformers that drop below); every
    # other row dips just below.
    same_row_above: set[str] = set()
    for i, intervals in same_row_by_row.items():
        if i == top_r:
            base = row_y[i] - _LANE_MARGIN_Y
            for x_left, x_right, e in intervals:
                edge_lane_y[e["id"]] = base - same_row_lane_idx[e["id"]] * _LANE_STEP_Y
                same_row_above.add(e["id"])
        else:
            base = row_y[i] + row_h[i] + _LANE_MARGIN_Y
            for x_left, x_right, e in intervals:
                edge_lane_y[e["id"]] = base + same_row_lane_idx[e["id"]] * _LANE_STEP_Y

    # ── 8. Emit edge sections with explicit Z-shape bend points using
    #      the assigned lane Y. JS will render these directly without
    #      re-routing (precomputedRoute flag).
    #
    # Horizontal bars: edges exit/enter from TOP or BOTTOM of the bar.
    # Bar runs at y = bus.y + busH/2 across [bus.x, bus.x + bar_len]. ──

    # ── Terminal slots: spread each bus's connections ALONG its bar instead
    #    of stacking them all at the centre. Terminals are ordered by the
    #    other endpoint's centre X, so left-going connections attach on the
    #    left of the bar and right-going on the right (fewer crossings). ──
    _TERM_PAD = 24
    edge_kind = {_e["id"]: _e["properties"].get("edgeType", "transmission")
                 for _e in elk_edges}
    bus_center_x = {gid: c["x"] + c["width"] / 2 for gid, c in bus_index.items()}
    bus_edge_list: dict[str, list[tuple[str, str]]] = {gid: [] for gid in bus_index}
    for _e in elk_edges:
        s0, t0 = _e["sources"][0], _e["targets"][0]
        if s0 in bus_edge_list:
            bus_edge_list[s0].append((_e["id"], t0))
        if t0 in bus_edge_list:
            bus_edge_list[t0].append((_e["id"], s0))
    term_x: dict[tuple[str, str], float] = {}
    for gid, lst in bus_edge_list.items():
        c = bus_index[gid]
        lst.sort(key=lambda e: bus_center_x.get(e[1], c["x"]))
        # Each terminal occupies a weighted slot — transformers take more room
        # than lines so their symbol + capacity label don't collide.
        weights = [_BUS_PER_XFMR if edge_kind.get(eid) == "transformer"
                   else _BUS_PER_EDGE for eid, _ in lst]
        total = sum(weights)
        x0 = c["x"] + _TERM_PAD
        x1 = c["x"] + c["width"] - _TERM_PAD
        if x1 <= x0:
            x0, x1 = c["x"], c["x"] + c["width"]
        span = x1 - x0
        cum = 0.0
        for (eid, _other), w in zip(lst, weights):
            centre = cum + w / 2.0
            term_x[(gid, eid)] = x0 + (centre / total) * span if total else (x0 + x1) / 2
            cum += w

    # ── 7c. Spanning circuits.  A line whose two buses sit in different node
    #       columns with a THIRD substation column between them cannot dip into
    #       any inter-row gap (or hop a neighbouring row) without crossing that
    #       middle substation's transformer legs / equipment — there is no clear
    #       horizontal Y inside a populated gap.  Route every such line through
    #       the TOP clear band (above every bar) using vertical risers dropped
    #       into the empty column GAPS beside each endpoint, exactly how real
    #       SLDs carry long inter-substation circuits around the substations
    #       they pass.  The column gaps are clear of bars, legs and equipment
    #       across the full height, and the terminal ordering already parks a
    #       spanning line's slot at the bar end facing its gap, so the short
    #       header from the slot to the riser stays clear too. ──
    node_span_x = {ni: (node_x_left.get(ni, 0.0),
                        node_x_left.get(ni, 0.0) + node_col_w.get(ni, 0.0))
                   for ni in state_node_order}

    def _spans_a_substation(s_gid: str, t_gid: str) -> bool:
        sn = node_of.get(s_gid)
        tn = node_of.get(t_gid)
        if sn == tn:
            return False
        sxx = bus_center_x.get(s_gid, 0.0)
        txx = bus_center_x.get(t_gid, 0.0)
        lo, hi = (sxx, txx) if sxx <= txx else (txx, sxx)
        for ni in state_node_order:
            if ni == sn or ni == tn:
                continue
            x0, x1 = node_span_x[ni]
            if lo < (x0 + x1) / 2.0 < hi:    # a substation column sits between
                return True
        return False

    band_intervals: list[tuple[float, float, dict]] = []
    for _e in elk_edges:
        if _e["properties"].get("edgeType") == "transformer":
            continue
        s0, t0 = _e["sources"][0], _e["targets"][0]
        if s0 not in bus_index or t0 not in bus_index:
            continue
        if _spans_a_substation(s0, t0):
            sxx = bus_center_x[s0]
            txx = bus_center_x[t0]
            band_intervals.append((min(sxx, txx), max(sxx, txx), _e))
    band_lane_idx: dict[str, int] = {}
    band_intervals.sort(key=lambda t: t[0])
    band_ends: list[float] = []
    for x_left, x_right, _e in band_intervals:
        assigned = -1
        for i, end in enumerate(band_ends):
            if end + LANE_X_MARGIN < x_left:
                assigned = i
                break
        if assigned == -1:
            assigned = len(band_ends)
            band_ends.append(x_right)
        else:
            band_ends[assigned] = x_right
        band_lane_idx[_e["id"]] = assigned
    band_set = set(band_lane_idx)
    # First band lane sits clear above the top row's own same-row lanes.
    top_band_base = (row_y.get(top_r, 0.0) - _LANE_MARGIN_Y
                     - (same_row_lane_count.get(top_r, 0) + 1) * _LANE_STEP_Y)

    # ── 7d. Obstacle map + orthogonal router.  Build the full set of physical
    #       obstacles (busbars, transformer legs, equipment stubs) so any line
    #       whose cheap lane route would cross an element can instead be routed
    #       AROUND every element with an A* search on the clear channels. The
    #       transformer leg X is computed exactly as the emit step below does. ──
    obstacle_bars = [(c["x"], c["x"] + c["width"], c["y"], c["y"] + _BUS_H, c["id"])
                     for c in elk_children]
    obstacle_vlegs: list[tuple[float, float, float]] = []
    xfmr_vert: dict[str, tuple[float, float, float]] = {}
    for _e in elk_edges:
        if _e["properties"].get("edgeType") != "transformer":
            continue
        _s = bus_index.get(_e["sources"][0])
        _t = bus_index.get(_e["targets"][0])
        if not _s or not _t:
            continue
        _up, _lo = (_s, _t) if _s["y"] < _t["y"] else (_t, _s)
        _up_gid = _e["sources"][0] if _up is _s else _e["targets"][0]
        _ox0 = max(_up["x"], _lo["x"])
        _ox1 = min(_up["x"] + _up["width"], _lo["x"] + _lo["width"])
        if _ox1 > _ox0:
            _cx = term_x.get((_up_gid, _e["id"]), (_ox0 + _ox1) / 2.0)
            _cx = min(max(_cx, _ox0), _ox1)
        else:
            _uc = _up["x"] + _up["width"] / 2.0
            _cx = min(max(_uc, _lo["x"]), _lo["x"] + _lo["width"])
        _seg = (_cx, _up["y"] + _BUS_H, _lo["y"])
        xfmr_vert[_e["id"]] = _seg
        obstacle_vlegs.append(_seg)
    # Equipment stubs (vertical) below each bar — positions replicate the JS
    # ``_getEquipStubPositions`` layout (evenly spaced, 72 px pitch, centred).
    _EQ_PITCH = 72.0
    for _gid, _eqs in (bus_equipment or {}).items():
        _c = bus_index.get(_gid)
        if not _c or not _eqs:
            continue
        _n = len(_eqs)
        _start = _c["x"] + (_c["width"] - _n * _EQ_PITCH) / 2.0 + _EQ_PITCH / 2.0
        _yt = _c["y"] + _BUS_H
        _yb = _yt + _STUB_LEN + _EQUIP_SIZE
        for _i in range(_n):
            obstacle_vlegs.append((_start + _i * _EQ_PITCH, _yt, _yb))

    # Horizontal lane tracks: clear lanes inside every inter-row channel, plus
    # generous bands above the top row and below the bottom-most element.
    router_lane_ys: list[float] = []
    for _i in range(len(sorted_v_list) - 1):
        _ra, _rb = sorted_v_list[_i], sorted_v_list[_i + 1]
        _ylo = row_y[_ra] + row_h.get(_ra, 0.0)
        _yhi = row_y[_rb]
        _yy = _ylo + _LANE_MARGIN_Y
        _any = False
        while _yy < _yhi - _LANE_MARGIN_Y + 0.5:
            router_lane_ys.append(_yy)
            _yy += _LANE_STEP_Y
            _any = True
        if not _any:
            router_lane_ys.append((_ylo + _yhi) / 2.0)
    _top_y = row_y[sorted_v_list[0]] if sorted_v_list else 0.0
    _bot_y = max([row_y[r] + row_h.get(r, 0.0) for r in sorted_v_list] +
                 [s[2] for s in obstacle_vlegs] + [_top_y])
    for _k in range(1, 41):
        router_lane_ys.append(_top_y - _LANE_MARGIN_Y - _k * _LANE_STEP_Y)
    for _k in range(0, 41):
        router_lane_ys.append(_bot_y + _LANE_MARGIN_Y + _k * _LANE_STEP_Y)

    # Vertical tracks: clear gaps between bars in every row, bar-edge corridors,
    # and the wide inter-node column gaps.
    router_x_tracks: list[float] = []
    _rows_bars: dict[int, list[tuple[float, float]]] = {}
    for _c in elk_children:
        _rows_bars.setdefault(row_of[_c["id"]], []).append(
            (_c["x"], _c["x"] + _c["width"]))
    for _r, _ivs in _rows_bars.items():
        _ivs.sort()
        router_x_tracks.append(_ivs[0][0] - _COL_GAP_X / 2.0)
        for _a, _b in zip(_ivs, _ivs[1:]):
            router_x_tracks.append((_a[1] + _b[0]) / 2.0)
        router_x_tracks.append(_ivs[-1][1] + _COL_GAP_X / 2.0)
        for _x0, _x1 in _ivs:
            router_x_tracks.append(_x0 - _INTRA_NODE_GAP_X / 2.0)
            router_x_tracks.append(_x1 + _INTRA_NODE_GAP_X / 2.0)
    for _ni in state_node_order:
        router_x_tracks.append(node_x_left.get(_ni, 0.0) - _COL_GAP_X / 2.0)
        router_x_tracks.append(node_x_left.get(_ni, 0.0)
                               + node_col_w.get(_ni, 0.0) + _COL_GAP_X / 2.0)

    _obstacles = Obstacles(obstacle_bars, obstacle_vlegs)
    _router = GridRouter(_obstacles, router_lane_ys, router_x_tracks, _LANE_STEP_Y)

    def _segments_clear(pts, s_gid, t_gid):
        """True if every segment of an orthogonal polyline avoids all bars/legs."""
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if abs(y0 - y1) < 0.5:
                if _obstacles.h_blocked(x0, x1, y0):
                    return False
            elif abs(x0 - x1) < 0.5:
                if _obstacles.v_blocked(x0, y0, y1, ignore_bars={s_gid, t_gid}):
                    return False
            else:
                return False
        return True

    for edge in elk_edges:
        src = bus_index.get(edge["sources"][0])
        tgt = bus_index.get(edge["targets"][0])
        if not src or not tgt:
            continue
        sx = term_x.get((edge["sources"][0], edge["id"]),
                        src["x"] + src["width"] / 2)
        tx = term_x.get((edge["targets"][0], edge["id"]),
                        tgt["x"] + tgt["width"] / 2)
        # Default: exit src bottom, enter tgt top (DOWN-direction layout).
        sy = src["y"] + _BUS_H
        ty = tgt["y"]
        if src["y"] > tgt["y"]:
            # src is below tgt → swap exit/entry direction.
            sy = src["y"]
            ty = tgt["y"] + _BUS_H
        elif src["y"] == tgt["y"]:
            if edge["id"] in same_row_above:
                # Top row → both exit the TOP face and hop up to the lane above.
                sy = src["y"]
                ty = tgt["y"]
            else:
                # Same row → both exit the bottom face and dip to the lane
                # below as a clean U (no segment crossing a bar).
                ty = tgt["y"] + _BUS_H

        rows_apart = abs(row_of.get(edge["sources"][0], 0)
                         - row_of.get(edge["targets"][0], 0))

        edge["properties"]["precomputedRoute"] = True

        is_xfmr = edge["properties"].get("edgeType") == "transformer"
        if is_xfmr:
            # A transformer is ALWAYS drawn as a clean vertical between its two
            # bars whenever they share an X span: one shared X inside the bars'
            # overlap, exit the upper bar's bottom, enter the lower bar's top.
            # The JS draws stubs + the two windings, so the line terminates at
            # the symbol (never crosses it) and every transformer looks the
            # same. Only the rare transformer whose bars don't overlap in X at
            # all falls through to the channel route.
            upper, lower = (src, tgt) if src["y"] < tgt["y"] else (tgt, src)
            upper_gid = (edge["sources"][0] if upper is src
                         else edge["targets"][0])
            ox0 = max(upper["x"], lower["x"])
            ox1 = min(upper["x"] + upper["width"], lower["x"] + lower["width"])
            if ox1 > ox0:
                # Bars overlap: use this transformer's own terminal slot
                # (distinct per parallel transformer), clamped to the overlap.
                cx = term_x.get((upper_gid, edge["id"]), (ox0 + ox1) / 2.0)
                cx = min(max(cx, ox0), ox1)
            else:
                # No X overlap: drop from the upper bar centre, landing on the
                # lower bar's nearest point — still a consistent vertical symbol.
                uc = upper["x"] + upper["width"] / 2.0
                cx = min(max(uc, lower["x"]), lower["x"] + lower["width"])
            edge["properties"]["transformerVertical"] = True
            edge["sections"] = [{
                "startPoint": {"x": cx, "y": upper["y"] + _BUS_H},
                "endPoint": {"x": cx, "y": lower["y"]},
                "bendPoints": [],
            }]
            continue

        # Build the cheap "simple" route first (start, bends…, end). Then test
        # it against every obstacle; only if it would cross something do we fall
        # back to the A* router that goes AROUND the obstacles.
        sgid = edge["sources"][0]
        tgid = edge["targets"][0]
        if edge["id"] in band_set:
            # Spanning circuit → carry it over the TOP band, around the
            # substations it passes. Risers sit in the clear gaps facing the
            # interior; the headers exit each bar's top face by its slot.
            sn = node_of.get(sgid, 0)
            tn = node_of.get(tgid, 0)
            if tx >= sx:
                s_riser = (node_x_left.get(sn, sx) + node_col_w.get(sn, 0.0)
                           + _COL_GAP_X / 2)
                t_riser = node_x_left.get(tn, tx) - _COL_GAP_X / 2
            else:
                s_riser = node_x_left.get(sn, sx) - _COL_GAP_X / 2
                t_riser = (node_x_left.get(tn, tx) + node_col_w.get(tn, 0.0)
                           + _COL_GAP_X / 2)
            band_y = top_band_base - band_lane_idx[edge["id"]] * _LANE_STEP_Y
            s_head = src["y"] - _LANE_MARGIN_Y
            t_head = tgt["y"] - _LANE_MARGIN_Y
            s_face, t_face = src["y"], tgt["y"]
            pts = [(sx, s_face), (sx, s_head), (s_riser, s_head),
                   (s_riser, band_y), (t_riser, band_y),
                   (t_riser, t_head), (tx, t_head), (tx, t_face)]
        elif rows_apart >= 2:
            # Multi-row edge: a straight vertical drop at sx/tx would pierce
            # the bars of the rows in between. Route the long vertical run in
            # a column GAP (between node columns), which is guaranteed clear
            # of any bar, then step horizontally in to each endpoint.
            src_node = int(src["properties"].get("parentNode", 0) or 0)
            if tx >= sx:
                channel_x = (node_x_left.get(src_node, sx)
                             + node_col_w.get(src_node, 0.0) + _COL_GAP_X / 2)
            else:
                channel_x = node_x_left.get(src_node, sx) - _COL_GAP_X / 2
            down = sy < ty
            y_src = sy + _LANE_MARGIN_Y if down else sy - _LANE_MARGIN_Y
            y_tgt = ty - _LANE_MARGIN_Y if down else ty + _LANE_MARGIN_Y
            s_face, t_face = sy, ty
            pts = [(sx, sy), (sx, y_src), (channel_x, y_src),
                   (channel_x, y_tgt), (tx, y_tgt), (tx, ty)]
        else:
            lane = edge_lane_y.get(edge["id"], (sy + ty) / 2)
            s_face, t_face = sy, ty
            pts = [(sx, sy), (sx, lane), (tx, lane), (tx, ty)]

        if not _segments_clear(pts, sgid, tgid):
            sn = node_of.get(sgid, 0)
            tn = node_of.get(tgid, 0)
            wx0 = min(sx, tx, node_x_left.get(sn, sx),
                      node_x_left.get(tn, tx)) - _COL_GAP_X
            wx1 = max(sx, tx,
                      node_x_left.get(sn, sx) + node_col_w.get(sn, 0.0),
                      node_x_left.get(tn, tx) + node_col_w.get(tn, 0.0)) + _COL_GAP_X
            routed = _router.route(sx, s_face, sgid, tx, t_face, tgid,
                                   x_window=(wx0, wx1))
            if routed and len(routed) >= 2:
                _router.commit(routed)
                pts = routed

        edge["sections"] = [{
            "startPoint": {"x": pts[0][0], "y": pts[0][1]},
            "endPoint": {"x": pts[-1][0], "y": pts[-1][1]},
            "bendPoints": [{"x": p[0], "y": p[1]} for p in pts[1:-1]],
        }]
