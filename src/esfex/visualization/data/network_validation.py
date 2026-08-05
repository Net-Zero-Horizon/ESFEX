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


def detect_duplicate_plants(state, radius_km: float = 15.0) -> list[dict]:
    """Group generators that are probably the same physical plant counted twice
    (e.g. once by OSM, once by GEM). Match by normalised name OR same fuel +
    close proximity. Returns one entry per group of size > 1."""
    gens = list(state.generators.items())

    def coord(g):
        b = state.buses.get(g.bus)
        return (getattr(b, "latitude", None), getattr(b, "longitude", None)) if b else (None, None)

    parent = {gid: gid for gid, _ in gens}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        parent[find(a)] = find(b)

    for i, (gid_a, ga) in enumerate(gens):
        na = _norm_name(ga.name)
        la, lo = coord(ga)
        for gid_b, gb in gens[i + 1:]:
            same = False
            nb = _norm_name(gb.name)
            if na and nb and na == nb:
                same = True
            elif ga.fuel == gb.fuel and None not in (la, lo):
                lb, ob = coord(gb)
                if None not in (lb, ob):
                    dkm = math.hypot((la - lb) * 111.0,
                                     (lo - ob) * 111.0 * math.cos(math.radians(la)))
                    if dkm <= radius_km:
                        same = True
            if same:
                union(gid_a, gid_b)

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


def maxflow_feasibility(state, peak_demand_mw: float) -> dict:
    """Can generation be delivered to demand within line capacities?

    Models a flow network: super-source → each generator bus (cap = installed
    capacity there) → line/transformer edges (cap = rated capacity, bidirectional)
    → demand buses (cap = that bus's share of ``peak_demand_mw``) → super-sink.
    Returns the maximum deliverable MW and, if short, the unservable amount.
    """
    bids = [b for b in state.buses]
    idx = {b: i for i, b in enumerate(bids)}
    n = len(bids)
    if n == 0 or peak_demand_mw <= 0:
        return {"deliverable_mw": 0.0, "peak_demand_mw": peak_demand_mw,
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

    # Demand per bus (share of peak by demand_fraction) → to sink.
    df_total = sum(
        b.demand_fraction or 0.0 for b in state.buses.values()
        if b.role in ("load", "mixed"))
    served_demand = 0.0
    if df_total > 0:
        for b, bus in state.buses.items():
            if bus.role in ("load", "mixed") and (bus.demand_fraction or 0) > 0:
                dmw = peak_demand_mw * (bus.demand_fraction / df_total)
                din.add(idx[b], T, dmw)
                served_demand += dmw

    flow = din.max_flow(S, T)
    unserv = max(0.0, served_demand - flow)
    return {
        "deliverable_mw": flow,
        "peak_demand_mw": served_demand,
        "unservable_mw": unserv,
        "feasible": unserv < 0.01 * max(served_demand, 1.0),
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
        rep.flags.append(
            f"{len(rep.duplicates)} probable duplicate plant(s) inflating "
            f"capacity by ~{dup_mw:.0f} MW (same plant counted more than once).")
    if rep.reserve_margin is not None:
        if rep.reserve_margin < 0:
            rep.flags.append(
                f"Installed capacity ({rep.total_capacity_mw:.0f} MW) is BELOW "
                f"peak demand ({peak_demand_mw:.0f} MW) — the system cannot "
                "meet load (reserve margin {:.0%}).".format(rep.reserve_margin))
        elif rep.reserve_margin > 1.0:
            rep.flags.append(
                f"Reserve margin is {rep.reserve_margin:.0%} — implausibly high "
                "for a real system (typically 15–40%); capacity is likely "
                "over-counted (duplicates / non-operational units).")
    if ref_capacity_mw and ref_capacity_mw > 0:
        ratio = rep.total_capacity_mw / ref_capacity_mw
        if ratio > 1.3 or ratio < 0.7:
            rep.flags.append(
                f"Installed capacity {rep.total_capacity_mw:.0f} MW is "
                f"{ratio:.1f}× the reference {ref_capacity_mw:.0f} MW.")
    if rep.feasibility and not rep.feasibility.get("feasible", True):
        rep.flags.append(
            f"Transmission bottleneck: only {rep.feasibility['deliverable_mw']:.0f} "
            f"of {rep.feasibility['peak_demand_mw']:.0f} MW peak demand is "
            f"deliverable — {rep.feasibility['unservable_mw']:.0f} MW is stranded "
            "behind under-capacity corridors (max-flow < demand).")
    return rep
