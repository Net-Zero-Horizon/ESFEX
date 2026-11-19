"""Realism / operability validation for a built network.

The topology audit (``esfex.bridge.topology_audit``) checks the network is
*well-formed* for the solver. This module checks two further, higher questions:

* **Statistical plausibility** — do the aggregate quantities (installed
  capacity by fuel, reserve margin, line length, duplicate plants) look like a
  real power system, or does something obviously not add up?
* **Electrical operability** — can generation actually be *delivered* to the
  demand within the line capacities? A connected network can still be unable to
  serve its load if a corridor is too thin. A max-flow / min-cut answers this
  as a necessary condition (it checks capacity, not voltage-angle limits).

Everything here is self-contained (no external grid database, no Julia solver).
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass, field


# ── Statistics ───────────────────────────────────────────────────────

def capacity_by_fuel(state) -> dict[str, float]:
    cap: dict[str, float] = defaultdict(float)
    for g in state.generators.values():
        cap[g.fuel or "?"] += g.rated_power or 0.0
    return dict(sorted(cap.items(), key=lambda kv: -kv[1]))


def _norm_name(name: str) -> str:
    """Normalise a plant name for duplicate matching: strip accents, unit
    suffixes, punctuation and case; collapse whitespace."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", name).lower()
    s = re.sub(r"(unit|no\.?|#|号機|号|ユニット)\s*\d+", " ", s)
    s = re.sub(r"[\s\-_/.,()（）]+", " ", s)
    return s.strip()


def _close_pairs(points, radius_km: float):
    """Yield pairs ``(a, b)`` of ``points`` (``(id, lat, lon)``) closer than
    ``radius_km``, comparing only neighbouring latitude bands one radius wide
    so large inputs stay fast."""
    band = radius_km / 111.0
    bands: dict[int, list] = defaultdict(list)
    for pid, la, lo in points:
        bands[math.floor(la / band)].append((pid, la, lo))
    for pid_a, la, lo in points:
        k = math.floor(la / band)
        cos_la = math.cos(math.radians(la))
        for kk in (k - 1, k, k + 1):
            for pid_b, lb, ob in bands.get(kk, ()):
                if pid_b == pid_a:
                    continue
                dkm = math.hypot((la - lb) * 111.0, (lo - ob) * 111.0 * cos_la)
                if dkm <= radius_km:
                    yield pid_a, pid_b


# Words that say what kind of plant a name is, not which one: two plants that
# share only these ("Parque Fotovoltaico X" / "Parque Fotovoltaico Y") are
# not the same plant. OSM placeholder names ("Generator node/123") reduce to
# nothing: an unnamed unit matches nothing by proximity alone (nine turbines
# next to a wind farm are not one plant counted nine times).
_GENERIC_NAME_WORDS = frozenset("""
    power station plant powerplant powerstation generating generation generator
    node way relation unit units central centrale centre center thermal
    thermoelectric termoelectrica termoeléctrica electrica eléctrica electric
    hidroelectrica hidroeléctrica hydroelectric hydro dam bioelectrica
    bioeléctrica biomass parque park farm solar fotovoltaico fotovoltaica
    photovoltaic pv wind eolico eólico eolica eólica diesel gas oil fuel coal
    nuclear energy energia energía grupo electrogeno electrógeno bateria batería
    the and del los las des eg cte mw kw
""".split())


def _name_tokens(name: str) -> frozenset:
    """Distinctive words of a plant name (generic plant words, numbers and
    words shorter than three letters removed)."""
    return frozenset(
        t for t in _norm_name(name).split()
        if len(t) >= 3 and not t.isdigit() and t not in _GENERIC_NAME_WORDS)


def _close_pairs(points, radius_km: float):
    """Yield pairs ``(a, b)`` of ``points`` (``(id, lat, lon)``) closer than
    ``radius_km``, comparing only neighbouring latitude bands one radius wide
    so large inputs stay fast."""
    band = radius_km / 111.0
    bands: dict[int, list] = defaultdict(list)
    for pid, la, lo in points:
        bands[math.floor(la / band)].append((pid, la, lo))
    for pid_a, la, lo in points:
        k = math.floor(la / band)
        cos_la = math.cos(math.radians(la))
        for kk in (k - 1, k, k + 1):
            for pid_b, lb, ob in bands.get(kk, ()):
                if pid_b == pid_a:
                    continue
                dkm = math.hypot((la - lb) * 111.0, (lo - ob) * 111.0 * cos_la)
                if dkm <= radius_km:
                    yield pid_a, pid_b


def detect_duplicate_plants(state, radius_km: float = 15.0) -> list[dict]:
    """Group generators that are probably the same physical plant counted twice
    (e.g. once by OSM, once by GEM). Returns one entry per group of size > 1.

    Two units match when they are within ``radius_km`` of each other and
    either have the same normalised name, or have the same fuel and names
    sharing a distinctive word ("Bioeléctrica Ciro Redondo" / "Ciro Redondo
    power station"). Units without coordinates match by name only.

    A generator key whose instances sit in several nodes (``gas_n1`` …
    ``gas_n8``, all named after the key) is an aggregate, not a plant: its
    instances are left out, and units of one key never match each other.
    """
    gens = list(state.generators.items())

    def coord(g):
        b = state.buses.get(g.bus)
        return (getattr(b, "latitude", None), getattr(b, "longitude", None)) if b else (None, None)

    unit_of = {gid: (getattr(g, "unit_key", None) or gid) for gid, g in gens}
    nodes_of_key: dict[str, set] = defaultdict(set)
    for gid, g in gens:
        nodes_of_key[unit_of[gid]].add(getattr(g, "node", None))
    gens = [(gid, g) for gid, g in gens if len(nodes_of_key[unit_of[gid]]) <= 1]

    parent = {gid: gid for gid, _ in gens}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        parent[find(a)] = find(b)

    tokens = {gid: _name_tokens(g.name) for gid, g in gens}
    norm = {gid: _norm_name(g.name) for gid, g in gens}
    fuel = {gid: g.fuel for gid, g in gens}

    def same_plant(a, b) -> bool:
        if unit_of[a] == unit_of[b]:
            return False
        if norm[a] and norm[a] == norm[b]:
            return True
        if fuel[a] != fuel[b]:
            return False
        return not tokens[a].isdisjoint(tokens[b])

    # Candidates are pairs within the radius among units of one fuel or one
    # name (latitude-banded, so the check also runs on the Studio's Validate
    # for large Grid Builder networks).
    by_name: dict[str, list] = defaultdict(list)
    by_fuel: dict[str, list] = defaultdict(list)
    unlocated_by_name: dict[str, list] = defaultdict(list)
    for gid, g in gens:
        la, lo = coord(g)
        if la is None or lo is None:
            if norm[gid]:
                unlocated_by_name[norm[gid]].append(gid)
            continue
        if norm[gid]:
            by_name[norm[gid]].append((gid, la, lo))
        by_fuel[g.fuel].append((gid, la, lo))
    for groups in (by_name, by_fuel):
        for pts in groups.values():
            if len(pts) > 1:
                for a, b in _close_pairs(pts, radius_km):
                    if same_plant(a, b):
                        union(a, b)
    for n, ids in unlocated_by_name.items():
        others = ids + [pid for pid, _, _ in by_name.get(n, ())]
        for a in ids:
            for b in others:
                if a != b and unit_of[a] != unit_of[b]:
                    union(a, b)

    groups: dict[str, list] = defaultdict(list)
    for gid, _ in gens:
        groups[find(gid)].append(gid)
    out = []
    for members in groups.values():
        if len(members) > 1:
            gs = [state.generators[m] for m in members]
            out.append({
                "name": gs[0].name,
                "fuel": gs[0].fuel,
                "count": len(members),
                "total_mw": sum(g.rated_power or 0.0 for g in gs),
                "members": members,
            })
    return sorted(out, key=lambda d: -d["total_mw"])


def network_metrics(state) -> dict:
    line_km = sum(ln.length_km or 0.0 for ln in state.transmission_lines)
    tr_mva = sum(getattr(t, "rated_power_mva", 0.0) or 0.0 for t in state.transformers)
    return {
        "buses": len(state.buses),
        "lines": len(state.transmission_lines),
        "line_km": line_km,
        "transformers": len(state.transformers),
        "transformer_mva": tr_mva,
        "generators": len(state.generators),
    }


# ── Max-flow feasibility ─────────────────────────────────────────────

class _Dinic:
    """Minimal Dinic max-flow on a small integer/float graph."""

    def __init__(self, n):
        self.n = n
        self.g: list[list[list]] = [[] for _ in range(n)]  # [to, cap, rev_idx]

    def add(self, u, v, cap):
        self.g[u].append([v, cap, len(self.g[v])])
        self.g[v].append([u, 0.0, len(self.g[u]) - 1])

    def _bfs(self, s, t):
        self.level = [-1] * self.n
        self.level[s] = 0
        q = deque([s])
        while q:
            u = q.popleft()
            for e in self.g[u]:
                if e[1] > 1e-9 and self.level[e[0]] < 0:
                    self.level[e[0]] = self.level[u] + 1
                    q.append(e[0])
        return self.level[t] >= 0

    def _dfs(self, u, t, f):
        if u == t:
            return f
        while self.it[u] < len(self.g[u]):
            e = self.g[u][self.it[u]]
            if e[1] > 1e-9 and self.level[e[0]] == self.level[u] + 1:
                d = self._dfs(e[0], t, min(f, e[1]))
                if d > 1e-9:
                    e[1] -= d
                    self.g[e[0]][e[2]][1] += d
                    return d
            self.it[u] += 1
        return 0.0

    def max_flow(self, s, t):
        flow = 0.0
        while self._bfs(s, t):
            self.it = [0] * self.n
            while True:
                f = self._dfs(s, t, float("inf"))
                if f <= 1e-9:
                    break
                flow += f
        return flow

    def min_cut_reachable(self, s):
        seen = [False] * self.n
        q = deque([s])
        seen[s] = True
        while q:
            u = q.popleft()
            for e in self.g[u]:
                if e[1] > 1e-9 and not seen[e[0]]:
                    seen[e[0]] = True
                    q.append(e[0])
        return seen


def _bus_peak_demand(state, peak_demand_mw: float) -> dict[str, float]:
    """Peak demand (MW) per load/mixed bus.

    Each node's own peak (``node.demand.peak_mw``) is split over that node's
    load/mixed buses by their ``demand_fraction`` (which sum to 1 per node),
    and the result is scaled so the buses add up to ``peak_demand_mw``. When
    no node carries a peak, ``peak_demand_mw`` is shared equally by the nodes
    that have demand buses.
    """
    by_node: dict = defaultdict(list)
    for b, bus in state.buses.items():
        if bus.role in ("load", "mixed") and (bus.demand_fraction or 0) > 0:
            by_node[bus.parent_node].append((b, bus.demand_fraction))
    if not by_node:
        return {}
    node_peak = {
        n.index: float(n.demand.peak_mw or 0.0)
        for n in getattr(state, "nodes", ())
        if getattr(n, "demand", None) is not None
    }
    node_peak = {k: v for k, v in node_peak.items() if k in by_node and v > 0}
    if node_peak:
        total = sum(node_peak.values())
        scale = peak_demand_mw / total if peak_demand_mw > 0 else 1.0
        node_peak = {k: v * scale for k, v in node_peak.items()}
    else:
        node_peak = {k: peak_demand_mw / len(by_node) for k in by_node}
    out: dict[str, float] = {}
    for node, buses in by_node.items():
        peak = node_peak.get(node, 0.0)
        fsum = sum(f for _, f in buses)
        if peak <= 0 or fsum <= 0:
            continue
        for b, f in buses:
            out[b] = peak * f / fsum
    return out


def maxflow_feasibility(state, peak_demand_mw: float) -> dict:
    """Can generation be delivered to demand within line capacities?

    Models a flow network: super-source → each generator bus (cap = installed
    capacity there) → line/transformer edges (cap = rated capacity, bidirectional)
    → demand buses (cap = that bus's share of its node's peak, see
    :func:`_bus_peak_demand`) → super-sink.

    Returns the maximum deliverable MW, the demand placed on buses
    (``peak_demand_mw``), the installed generation on buses, and the part of
    the demand the network cannot deliver although generation could serve it
    (``unservable_mw``): ``min(generation, demand) − max-flow``. Demand above
    the installed capacity (``generation_shortfall_mw``) is a generation
    problem, not a transmission one, so it does not make the check fail.
    """
    bids = [b for b in state.buses]
    idx = {b: i for i, b in enumerate(bids)}
    n = len(bids)
    if n == 0 or peak_demand_mw <= 0:
        return {"deliverable_mw": 0.0, "peak_demand_mw": peak_demand_mw,
                "generation_mw": 0.0, "generation_shortfall_mw": 0.0,
                "unservable_mw": 0.0, "feasible": True}
    S, T = n, n + 1
    din = _Dinic(n + 2)

    # Generation capacity per bus → from source.
    gen_at: dict[str, float] = defaultdict(float)
    for g in state.generators.values():
        if g.bus in idx:
            gen_at[g.bus] += g.rated_power or 0.0
    for b, cap in gen_at.items():
        if cap > 0:
            din.add(S, idx[b], cap)

    # Transmission edges (bidirectional at rated capacity).
    def _edge(fb, tb, cap):
        if fb in idx and tb in idx and fb != tb and cap > 0:
            din.add(idx[fb], idx[tb], cap)
            din.add(idx[tb], idx[fb], cap)
    for ln in state.transmission_lines:
        _edge(ln.from_bus, ln.to_bus, getattr(ln, "capacity_mw", 0.0) or 0.0)
    for t in state.transformers:
        _edge(t.from_bus, t.to_bus, getattr(t, "rated_power_mva", 0.0) or 0.0)
    for c in state.acdc_converters:
        _edge(c.from_bus, c.to_bus, getattr(c, "rated_power_mva", 0.0) or 0.0)

    # Demand per bus (its node's peak split by demand_fraction) → to sink.
    served_demand = 0.0
    for b, dmw in _bus_peak_demand(state, peak_demand_mw).items():
        din.add(idx[b], T, dmw)
        served_demand += dmw

    flow = din.max_flow(S, T)
    generation = sum(gen_at.values())
    # What a copper plate would deliver: the smaller of generation and demand.
    reachable = min(generation, served_demand)
    unserv = max(0.0, reachable - flow)
    return {
        "deliverable_mw": flow,
        "peak_demand_mw": served_demand,
        "generation_mw": generation,
        "generation_shortfall_mw": max(0.0, served_demand - generation),
        "unservable_mw": unserv,
        "feasible": unserv < 0.01 * max(reachable, 1.0),
    }


# ── Report ───────────────────────────────────────────────────────────

@dataclass
class NetworkValidationReport:
    capacity_by_fuel: dict = field(default_factory=dict)
    total_capacity_mw: float = 0.0
    peak_demand_mw: float = 0.0
    reserve_margin: float | None = None
    duplicates: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    feasibility: dict = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)


def validate_network(state, peak_demand_mw: float = 0.0,
                     ref_capacity_mw: float | None = None) -> NetworkValidationReport:
    """Run the statistical (3) + operability (2) checks and collect red flags."""
    from esfex.visualization.i18n import tr

    rep = NetworkValidationReport()
    rep.capacity_by_fuel = capacity_by_fuel(state)
    rep.total_capacity_mw = sum(rep.capacity_by_fuel.values())
    rep.peak_demand_mw = peak_demand_mw
    rep.duplicates = detect_duplicate_plants(state)
    rep.metrics = network_metrics(state)

    if peak_demand_mw > 0:
        rep.reserve_margin = (rep.total_capacity_mw - peak_demand_mw) / peak_demand_mw
        rep.feasibility = maxflow_feasibility(state, peak_demand_mw)

    # Red flags.
    dup_mw = sum(d["total_mw"] - d["total_mw"] / d["count"] for d in rep.duplicates)
    if rep.duplicates:
        rep.flags.append(tr("validation.flag_duplicates", n=len(rep.duplicates),
                            mw=f"{dup_mw:.0f}"))
    if rep.reserve_margin is not None:
        if rep.reserve_margin < 0:
            rep.flags.append(tr("validation.flag_capacity_below_peak",
                                capacity=f"{rep.total_capacity_mw:.0f}",
                                peak=f"{peak_demand_mw:.0f}",
                                margin=f"{rep.reserve_margin:.0%}"))
        elif rep.reserve_margin > 1.0:
            rep.flags.append(tr("validation.flag_high_reserve_margin",
                                margin=f"{rep.reserve_margin:.0%}"))
    if ref_capacity_mw and ref_capacity_mw > 0:
        ratio = rep.total_capacity_mw / ref_capacity_mw
        if ratio > 1.3 or ratio < 0.7:
            rep.flags.append(tr("validation.flag_reference_ratio",
                                capacity=f"{rep.total_capacity_mw:.0f}",
                                ratio=f"{ratio:.1f}",
                                reference=f"{ref_capacity_mw:.0f}"))
    if rep.feasibility and not rep.feasibility.get("feasible", True):
        fz = rep.feasibility
        rep.flags.append(tr("validation.flag_bottleneck",
                            deliverable=f"{fz['deliverable_mw']:.0f}",
                            peak=f"{fz['peak_demand_mw']:.0f}",
                            unservable=f"{fz['unservable_mw']:.0f}"))
    return rep
