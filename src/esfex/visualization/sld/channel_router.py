"""Orthogonal routing of lines between diagram blocks along grid channels.

:mod:`.placement` puts every block on a regular cell grid and keeps the cell
boundaries clear: a line can always travel along them without touching a
block. This router searches the boundary network — corners of the cell grid
joined by the channel segments that no block straddles — with A* (length +
a turn penalty + a congestion cost that spreads parallel lines over parallel
channels).

Each line leaves its bar vertically to the channel on the block's top or
bottom side, follows the channels, and drops vertically onto the other bar.
Lines sharing a channel are then given separate tracks inside it (interval
colouring per channel line), so they run side by side instead of on top of
each other.

Pure Python (no Qt), so it is unit-testable.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

TURN_PENALTY = 60.0      # px-equivalent cost of one bend
CONGESTION = 40.0        # px-equivalent cost per line already in a segment
TRACK_SPACING = 10.0     # preferred distance between parallel lines, px
MIN_TRACK_SPACING = 4.0
TRACK_MARGIN = 8.0       # keep tracks this far from the channel edges


@dataclass
class LineSpec:
    eid: str
    s_block: str
    t_block: str
    sx: float            # terminal x on the source bar
    sy: float            # terminal y (bar face) on the source bar
    s_up: bool           # leave the source bar upwards (else downwards)
    tx: float
    ty: float
    t_up: bool


def route_lines(placement, lines: list[LineSpec]) -> dict[str, list[tuple[float, float]]]:
    """Polyline (terminal → terminal) for every line in ``lines``."""
    cw, ch = placement.cell_w, placement.cell_h
    owner = placement.owner_grid()
    cells = placement.cells
    n_cols, n_rows = placement.n_cols, placement.n_rows

    def h_free(i, j):
        """Channel segment along row-boundary j, over column i."""
        a = owner.get((i, j - 1))
        return a is None or a != owner.get((i, j))

    def v_free(i, j):
        """Channel segment along column-boundary i, over row j."""
        a = owner.get((i - 1, j))
        return a is None or a != owner.get((i, j))

    use: dict[tuple, int] = {}

    def seg_key(p, q):
        return (p, q) if p <= q else (q, p)

    def access(block, x, up):
        """Channel row and column of the point where a terminal at ``x``
        meets its block's top (``up``) or bottom channel."""
        c0, r0, sc, sr = cells[block]
        j = r0 if up else r0 + sr
        i = int(math.floor(x / cw))
        i = min(max(i, c0), c0 + sc - 1)
        return i, j

    routes: dict[str, list[tuple[float, float]]] = {}
    runs_by_line: dict[str, list[list]] = {}

    # Short lines first: they have the fewest alternatives.
    order = sorted(lines, key=lambda L: (abs(L.sx - L.tx) + abs(L.sy - L.ty), L.eid))
    for L in order:
        si, sj = access(L.s_block, L.sx, L.s_up)
        ti, tj = access(L.t_block, L.tx, L.t_up)
        S = ("S",)
        T = ("T",)
        ps = (L.sx, sj * ch)
        pt = (L.tx, tj * ch)

        def coord(n):
            if n == S:
                return ps
            if n == T:
                return pt
            return (n[0] * cw, n[1] * ch)

        def neighbours(n):
            if n == S:
                return [((si, sj), abs(si * cw - L.sx), "h"),
                        ((si + 1, sj), abs((si + 1) * cw - L.sx), "h")]
            i, j = n
            out = []
            if j == tj and i in (ti, ti + 1):
                out.append((T, abs(i * cw - L.tx), "h"))
            if i > 0 and h_free(i - 1, j):
                out.append(((i - 1, j), cw, "h"))
            if i < n_cols and h_free(i, j):
                out.append(((i + 1, j), cw, "h"))
            if j > 0 and v_free(i, j - 1):
                out.append(((i, j - 1), ch, "v"))
            if j < n_rows and v_free(i, j):
                out.append(((i, j + 1), ch, "v"))
            return out

        def h(n):
            x, y = coord(n)
            return abs(x - pt[0]) + abs(y - pt[1])

        path = None
        if (si, sj) == (ti, tj):
            path = [S, T]
        else:
            start = (S, "h")
            best = {start: 0.0}
            came = {start: None}
            pq = [(h(S), 0.0, 0, S, "h")]
            tie = 0
            goal_state = None
            while pq:
                _f, g, _t, n, d = heapq.heappop(pq)
                st = (n, d)
                if g > best.get(st, math.inf):
                    continue
                if n == T:
                    goal_state = st
                    break
                for m, length, md in neighbours(n):
                    cost = length
                    if n != S and m != T:
                        cost += CONGESTION * use.get(seg_key(n, m), 0)
                    if md != d:
                        cost += TURN_PENALTY
                    ng = g + cost
                    ms = (m, md)
                    if ng < best.get(ms, math.inf):
                        best[ms] = ng
                        came[ms] = st
                        tie += 1
                        heapq.heappush(pq, (ng + h(m), ng, tie, m, md))
            if goal_state is not None:
                path = []
                st = goal_state
                while st is not None:
                    path.append(st[0])
                    st = came[st]
                path.reverse()
        if path is None:
            # No channel path (should not happen on a valid placement):
            # fall back to a plain Z between the two terminals.
            ym = (L.sy + L.ty) / 2.0
            routes[L.eid] = [(L.sx, L.sy), (L.sx, ym), (L.tx, ym), (L.tx, L.ty)]
            continue
        for a, b in zip(path, path[1:]):
            if a != S and b != T:
                k = seg_key(a, b)
                use[k] = use.get(k, 0) + 1

        # Collapse to straight runs on channel lines.
        pts = [coord(n) for n in path]
        runs = []          # [orient, channel coordinate, lo, hi]
        for p, q in zip(pts, pts[1:]):
            if abs(p[1] - q[1]) < 1e-6:
                o, c, lo, hi = "h", p[1], min(p[0], q[0]), max(p[0], q[0])
            else:
                o, c, lo, hi = "v", p[0], min(p[1], q[1]), max(p[1], q[1])
            if runs and runs[-1][0] == o and abs(runs[-1][1] - c) < 1e-6:
                runs[-1][2] = min(runs[-1][2], lo)
                runs[-1][3] = max(runs[-1][3], hi)
            else:
                runs.append([o, c, lo, hi])
        runs_by_line[L.eid] = runs
        routes[L.eid] = None            # filled after track assignment

    # Track assignment: per channel line, greedy interval colouring.
    by_channel: dict[tuple, list[tuple[float, float, str, int]]] = {}
    for eid, runs in runs_by_line.items():
        for k, (o, c, lo, hi) in enumerate(runs):
            by_channel.setdefault((o, round(c, 3)), []).append((lo, hi, eid, k))
    offset: dict[tuple[str, int], float] = {}
    for (o, _c), items in by_channel.items():
        items.sort()
        ends: list[float] = []
        track_of = []
        for lo, hi, eid, k in items:
            t = next((i for i, e in enumerate(ends) if e + 1.0 < lo), None)
            if t is None:
                t = len(ends)
                ends.append(hi)
            else:
                ends[t] = hi
            track_of.append(t)
        n = len(ends)
        gap = placement.gap_y if o == "h" else placement.gap_x
        room = max(gap - 2 * TRACK_MARGIN, 0.0)
        sp = TRACK_SPACING if n <= 1 else max(MIN_TRACK_SPACING,
                                              min(TRACK_SPACING, room / (n - 1)))
        for (lo, hi, eid, k), t in zip(items, track_of):
            offset[(eid, k)] = (t - (n - 1) / 2.0) * sp

    by_eid = {L.eid: L for L in lines}
    for eid, runs in runs_by_line.items():
        L = by_eid[eid]
        coords = []
        for k, (o, c, _lo, _hi) in enumerate(runs):
            coords.append((o, c + offset.get((eid, k), 0.0)))
        pts = [(L.sx, L.sy)]
        x, y = L.sx, None
        for o, c in coords:
            if o == "h":
                y = c
            else:
                x = c
            if y is not None:
                pts.append((x, y))
        pts.append((L.tx, y if y is not None else L.ty))
        pts.append((L.tx, L.ty))
        # Drop duplicate / collinear points.
        clean = [pts[0]]
        for p in pts[1:]:
            if abs(p[0] - clean[-1][0]) < 1e-6 and abs(p[1] - clean[-1][1]) < 1e-6:
                continue
            if len(clean) >= 2:
                a, b = clean[-2], clean[-1]
                if ((abs(a[0] - b[0]) < 1e-6 and abs(b[0] - p[0]) < 1e-6)
                        or (abs(a[1] - b[1]) < 1e-6 and abs(b[1] - p[1]) < 1e-6)):
                    clean[-1] = p
                    continue
            clean.append(p)
        routes[eid] = clean
    return routes
