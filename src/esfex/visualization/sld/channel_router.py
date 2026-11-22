"""Orthogonal routing of lines between diagram blocks along grid channels.

:mod:`.placement` puts every block on a regular cell grid and keeps the cell
boundaries clear: a line can always travel along them without touching a
block. Routing runs in four steps:

1. **Paths.** A* over the boundary network — corners of the cell grid joined
   by the channel segments that no block straddles — with length, a turn
   penalty and a congestion cost that spreads lines over parallel channels.
   Each line leaves its bar vertically to the channel on the block's top or
   bottom side and drops vertically onto the other bar. Parallel circuits
   (lines between the same two bar faces) follow the path of the first one,
   so they are drawn as one ribbon.
2. **Terminal order.** The terminals sharing one bar face are re-ordered so
   that lines leave the bar in the order of their routes, which avoids
   crossings right at the bar.
3. **Tracks.** Lines sharing a channel get separate, parallel tracks. Their
   order follows where the lines go next (as on a metro map): of two lines
   running side by side, the one that will turn left first, or turns left
   where the other goes straight, takes the left track. Applied the same way
   on every channel, two lines then cross at most once, where their paths
   actually force it.
4. **Channel widths.** A channel carrying more tracks than fit in the gap
   between its blocks is widened, shifting the blocks beyond it, so tracks
   always keep ``TRACK_SPACING`` between them.

Pure Python (no Qt), so it is unit-testable.
"""

from __future__ import annotations

import functools
import heapq
import math
from dataclasses import dataclass, field

TURN_PENALTY = 120.0     # px-equivalent cost of one bend
CONGESTION = 40.0        # px-equivalent cost per line already in a segment
BUNDLE_DISCOUNT = 0.3    # cost factor on the segments of a parallel circuit
TRACK_SPACING = 10.0     # distance between parallel lines, px
TRACK_MARGIN = 10.0      # keep the outer tracks this far from the gap edges

_EPS = 1e-6


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
    # Identity of the bar face each end sits on. Terminals on one face swap
    # positions so lines leave in the order of their routes, and lines
    # between the same two faces are routed as one ribbon. Empty: neither.
    s_face: str = ""
    t_face: str = ""


@dataclass
class Routing:
    """Polylines (terminal → terminal) and the block moves that widened
    the channels. ``shift[bid]`` must be added to everything drawn inside
    block ``bid``; the routes are already in the shifted coordinates."""

    routes: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    shift: dict[str, tuple[float, float]] = field(default_factory=dict)


# ── geometry helpers ───────────────────────────────────────────────


def _dir(p, q):
    dx, dy = q[0] - p[0], q[1] - p[1]
    return ((dx > _EPS) - (dx < -_EPS), (dy > _EPS) - (dy < -_EPS))


def _cross(d, e) -> int:
    """> 0: e turns right of d (screen coordinates, y down); < 0: left."""
    return d[0] * e[1] - d[1] * e[0]


def _proj(p, d) -> float:
    return p[0] * d[0] + p[1] * d[1]


def _side(pa, ia, pb, ib, d) -> int:
    """-1 if line ``a`` must run on the left of line ``b``, +1 on the right,
    0 if undecided. Both lines travel direction ``d`` on the same channel
    line, ``a`` on its segment ``pa[ia] → pa[ia + 1]``, ``b`` likewise.

    The lines are followed forward until they part: the one that turns
    first goes on the side it turns to; if both turn at the same point the
    one turning left goes left; if they turn together the same way the walk
    continues on the new channel.
    """
    na, nb = len(pa), len(pb)
    for _ in range(na + nb):
        # Skip straight-through vertices and zero-length jogs.
        while ia + 2 < na and _dir(pa[ia + 1], pa[ia + 2]) in (d, (0, 0)):
            ia += 1
        while ib + 2 < nb and _dir(pb[ib + 1], pb[ib + 2]) in (d, (0, 0)):
            ib += 1
        if ia + 2 >= na or ib + 2 >= nb:
            return 0
        ta, tb = _proj(pa[ia + 1], d), _proj(pb[ib + 1], d)
        da = _dir(pa[ia + 1], pa[ia + 2])
        db = _dir(pb[ib + 1], pb[ib + 2])
        if ta < tb - _EPS:
            c = _cross(d, da)
            return -1 if c < 0 else (1 if c > 0 else 0)
        if tb < ta - _EPS:
            c = _cross(d, db)
            return 1 if c < 0 else (-1 if c > 0 else 0)
        if da != db:
            ca, cb = _cross(d, da), _cross(d, db)
            return -1 if ca < cb else (1 if ca > cb else 0)
        ia += 1
        ib += 1
        d = da
    return 0


# ── router ─────────────────────────────────────────────────────────


def route_lines(placement, lines: list[LineSpec]) -> Routing:
    """Route ``lines`` between the blocks of ``placement``."""
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

    use: dict[tuple, int] = {}
    spec = {ln.eid: ln for ln in lines}
    xs = {ln.eid: [ln.sx, ln.tx] for ln in lines}

    def row_of(ln, end):
        blk, up = (ln.s_block, ln.s_up) if end == 0 else (ln.t_block, ln.t_up)
        c0, r0, sc, sr = cells[blk]
        return r0 if up else r0 + sr

    # Terminals reaching the same channel closer than a track apart (two
    # facing bars, typically) would draw their stubs on top of each other:
    # push them apart.
    by_row: dict[int, list[tuple[float, str, int]]] = {}
    for ln in lines:
        for end in (0, 1):
            by_row.setdefault(row_of(ln, end), []).append((xs[ln.eid][end], ln.eid, end))
    for _j, ends in by_row.items():
        ends.sort()
        for (xa, ea, na), (xb, eb, nb) in zip(ends, ends[1:]):
            xa, xb = xs[ea][na], xs[eb][nb]
            if xb - xa < TRACK_SPACING:
                push = (TRACK_SPACING - (xb - xa)) / 2.0
                xs[ea][na] = xa - push
                xs[eb][nb] = xb + push

    def astar(ln: LineSpec, prefer: set):
        """Corner path (without the terminals) or None."""
        sx, tx = xs[ln.eid]
        si, sj = access(ln.s_block, sx, ln.s_up)
        ti, tj = access(ln.t_block, tx, ln.t_up)
        if (si, sj) == (ti, tj):
            return []
        src, dst = ("S",), ("T",)
        ps, pt = (sx, sj * ch), (tx, tj * ch)

        def coord(n):
            if n == src:
                return ps
            if n == dst:
                return pt
            return (n[0] * cw, n[1] * ch)

        def neighbours(n):
            if n == src:
                return [((si, sj), abs(si * cw - sx), "h"),
                        ((si + 1, sj), abs((si + 1) * cw - sx), "h")]
            i, j = n
            out = []
            if j == tj and i in (ti, ti + 1):
                out.append((dst, abs(i * cw - tx), "h"))
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
            return BUNDLE_DISCOUNT * (abs(x - pt[0]) + abs(y - pt[1])) if prefer \
                else abs(x - pt[0]) + abs(y - pt[1])

        start = (src, "h")
        best = {start: 0.0}
        came = {start: None}
        pq = [(h(src), 0.0, 0, src, "h")]
        tie = 0
        goal = None
        while pq:
            _f, g, _t, n, d = heapq.heappop(pq)
            st = (n, d)
            if g > best.get(st, math.inf):
                continue
            if n == dst:
                goal = st
                break
            for m, length, md in neighbours(n):
                if n != src and m != dst:
                    k = seg_key(n, m)
                    if k in prefer:
                        cost = length * BUNDLE_DISCOUNT
                    else:
                        cost = length + CONGESTION * use.get(k, 0)
                else:
                    cost = length
                if md != d:
                    cost += TURN_PENALTY
                ng = g + cost
                ms = (m, md)
                if ng < best.get(ms, math.inf):
                    best[ms] = ng
                    came[ms] = st
                    tie += 1
                    heapq.heappush(pq, (ng + h(m), ng, tie, m, md))
        if goal is None:
            return None
        path = []
        st = goal
        while st is not None:
            path.append(st[0])
            st = came[st]
        path.reverse()
        return [n for n in path if n not in (src, dst)]

    # 1. Paths. Short lines first (fewest alternatives); the members of a
    #    parallel circuit right after each other, following the first.
    def bundle_of(ln):
        if ln.s_face and ln.t_face:
            return tuple(sorted((ln.s_face, ln.t_face)))
        return (ln.eid,)

    def manh(ln):
        return abs(ln.sx - ln.tx) + abs(ln.sy - ln.ty)

    bundles: dict[tuple, list[LineSpec]] = {}
    for ln in lines:
        bundles.setdefault(bundle_of(ln), []).append(ln)
    order = []
    for key, members in sorted(bundles.items(),
                               key=lambda kv: (min(manh(ln) for ln in kv[1]), kv[0])):
        order.extend(sorted(members, key=lambda ln: ln.eid))

    corners: dict[str, list[tuple[int, int]]] = {}
    fixed: dict[str, list[tuple[float, float]]] = {}
    leader_segs: dict[tuple, set] = {}
    for ln in order:
        b = bundle_of(ln)
        path = astar(ln, leader_segs.get(b, set()))
        if path is None:
            # No channel path (should not happen on a valid placement).
            sx, tx = xs[ln.eid]
            ym = (ln.sy + ln.ty) / 2.0
            fixed[ln.eid] = [(sx, ln.sy), (sx, ym), (tx, ym), (tx, ln.ty)]
            continue
        corners[ln.eid] = path
        segs = {seg_key(a, c) for a, c in zip(path, path[1:])}
        for k in segs:
            use[k] = use.get(k, 0) + 1
        leader_segs.setdefault(b, segs)

    def nominal(eid):
        """Vertices (descriptor, point) of the line in nominal coordinates:
        bar face, channel entry, corners, channel entry, bar face."""
        ln = spec[eid]
        sx, tx = xs[eid]
        sj, tj = row_of(ln, 0), row_of(ln, 1)
        inner = [(("e", 0), (sx, sj * ch))]
        inner += [(("c", i, j), (i * cw, j * ch)) for i, j in corners[eid]]
        inner += [(("e", 1), (tx, tj * ch))]
        # Drop repeated and collinear corners. A terminal right at a column
        # boundary keeps its zero-length run along the row channel: it is
        # the jog between the stub and the column track, and needs a track
        # of its own like any other run.
        out = []
        for v in inner:
            if out:
                b = out[-1]
                if _same(b[1], v[1]):
                    if v[0][0] == "c" and b[0][0] == "c":
                        continue
                    if (b[0][0] == "c" and len(out) >= 2
                            and abs(out[-2][1][1] - b[1][1]) < _EPS):
                        out[-1] = v          # row run continues into the entry
                        continue
                    out.append(v)
                    continue
                if len(out) >= 2 and b[0][0] == "c":
                    a = out[-2][1]
                    if _same(a, b[1]):
                        if abs(b[1][1] - v[1][1]) < _EPS:
                            out[-1] = v      # zero-length row run, then the row
                            continue
                    elif ((abs(a[0] - b[1][0]) < _EPS and abs(b[1][0] - v[1][0]) < _EPS)
                          or (abs(a[1] - b[1][1]) < _EPS and abs(b[1][1] - v[1][1]) < _EPS)):
                        out[-1] = v
                        continue
            out.append(v)
        return ([(("f", 0), (sx, ln.sy))] + out + [(("f", 1), (tx, ln.ty))])

    # 2. Terminal order on each bar face.
    faces: dict[str, list[tuple[str, int]]] = {}
    for eid in corners:
        ln = spec[eid]
        if ln.s_face:
            faces.setdefault(ln.s_face, []).append((eid, 0))
        if ln.t_face:
            faces.setdefault(ln.t_face, []).append((eid, 1))
    pts_cache = {eid: [p for _v, p in nominal(eid)] for eid in corners}
    for _face, ends in sorted(faces.items()):
        if len(ends) < 2:
            continue
        ends.sort(key=lambda e: (xs[e[0]][e[1]], e[0]))
        slots = [xs[eid][end] for eid, end in ends]

        def oriented(e):
            p = pts_cache[e[0]]
            return p if e[1] == 0 else p[::-1]

        d = _dir(*oriented(ends[0])[:2])
        if d == (0, 0):
            continue

        def cmp(a, b):
            s = _side(oriented(a), 0, oriented(b), 0, d)
            return s if s else (-1 if (xs[a[0]][a[1]], a[0]) < (xs[b[0]][b[1]], b[0]) else 1)

        ranked = sorted(ends, key=functools.cmp_to_key(cmp))
        # Travelling up, "left" is west (low x); travelling down, east.
        if d[1] > 0:
            ranked.reverse()
        for (eid, end), x in zip(ranked, slots):
            xs[eid][end] = x
        # The next faces order themselves against these new positions.
        for eid, _end in ends:
            pts_cache[eid] = [p for _v, p in nominal(eid)]
    nominal_of = {eid: nominal(eid) for eid in corners}
    pts_of = {eid: [p for _v, p in nv] for eid, nv in nominal_of.items()}
    rev_of = {eid: p[::-1] for eid, p in pts_of.items()}

    # 3. Tracks. A run is one channel segment of a line (the two stubs are
    #    not in a channel). Channel ids: ("h", row boundary) / ("v", column
    #    boundary); canonical travel: east on "h", south on "v".
    runs: dict[tuple, list[tuple[float, float, str, int]]] = {}
    # Per run: the channel crossing each of its ends at a corner (its final
    # end point moves by that channel's track offset), or None.
    end_chan: dict[tuple[str, int], tuple] = {}
    # Per run: the side (-1 / +1 along the crossing axis) its line leaves
    # to at the low and the high end.
    end_side: dict[tuple[str, int], tuple[int, int]] = {}

    def leave(pts, m, step, axis):
        """Sign along ``axis`` of the first non-zero move from pts[m]."""
        i = m
        while 0 <= i + step < len(pts):
            dd = pts[i + step][axis] - pts[m][axis]
            if abs(dd) > _EPS:
                return 1 if dd > 0 else -1
            if abs(pts[i + step][1 - axis] - pts[m][1 - axis]) > _EPS:
                return 0
            i += step
        return 0

    for eid, nv in nominal_of.items():
        pts = pts_of[eid]
        for k in range(1, len(nv) - 2):
            (va, p), (vb, q) = nv[k], nv[k + 1]
            if abs(p[1] - q[1]) < _EPS:
                key = ("h", int(round(p[1] / ch)))
                lo, hi = sorted((p[0], q[0]))
                perp = [("v", int(round(v[1][0] / cw))) if v[0][0] == "c" else None
                        for v in ((va, p), (vb, q))]
            else:
                key = ("v", int(round(p[0] / cw)))
                lo, hi = sorted((p[1], q[1]))
                perp = [("h", int(round(v[1][1] / ch))) if v[0][0] == "c" else None
                        for v in ((va, p), (vb, q))]
            axis = 1 if abs(p[1] - q[1]) < _EPS else 0
            sides = [leave(pts, k, -1, axis), leave(pts, k + 1, 1, axis)]
            if (p[0] + p[1]) > (q[0] + q[1]):
                perp.reverse()
                sides.reverse()
            end_chan[(eid, k)] = tuple(perp)
            end_side[(eid, k)] = (sides[0], sides[1])
            runs.setdefault(key, []).append((lo, hi, eid, k))

    def pack(n, before, span):
        """Constrained left-edge: fill tracks one by one, each with the runs
        whose predecessors are all placed, left to right."""
        by_lo = sorted(range(n), key=lambda r: (span[r][0], span[r][1], r))
        placed = [-1] * n
        left = set(range(n))
        t = 0
        while left:
            ready = [r for r in by_lo if r in left
                     and all(placed[p] >= 0 for p in before[r])]
            if not ready:   # cycle: free the run with fewest pending predecessors
                r = min(left, key=lambda r: (sum(placed[p] < 0 for p in before[r]), r))
                ready = [r]
            end = -math.inf
            for r in ready:
                lo, hi = span[r]
                if lo > end + 1.0:
                    placed[r] = t
                    left.discard(r)
                    end = hi
            t += 1
        return placed, t

    constraints: dict[tuple, list[set[int]]] = {}
    for key, items in runs.items():
        d = (1, 0) if key[0] == "h" else (0, 1)
        items.sort(key=lambda r: (r[0], r[1], r[2]))
        n = len(items)

        def state(r):
            lo, hi, eid, k = r
            p = pts_of[eid]
            if _proj(p[k + 1], d) >= _proj(p[k], d):
                return p, k
            return rev_of[eid], len(p) - 2 - k

        before: list[set[int]] = [set() for _ in range(n)]   # must sit lower-index
        for a in range(n):
            la, ha = items[a][0], items[a][1]
            for b in range(a + 1, n):
                lb, hb = items[b][0], items[b][1]
                if lb > ha + _EPS:
                    break
                if min(ha, hb) - max(la, lb) < 1.0:
                    # Touching ends: the line leaving to the low side (north
                    # on a row, west on a column) takes the lower track.
                    sa = sb = 0
                    if abs(ha - lb) <= _EPS:
                        sa = end_side[(items[a][2], items[a][3])][1]
                        sb = end_side[(items[b][2], items[b][3])][0]
                    elif abs(hb - la) <= _EPS:
                        sa = end_side[(items[a][2], items[a][3])][0]
                        sb = end_side[(items[b][2], items[b][3])][1]
                    if sa and sb and sa != sb:
                        (before[b] if sa < sb else before[a]).add(a if sa < sb else b)
                    continue
                pa, ia = state(items[a])
                pb, ib = state(items[b])
                s = _side(pa, ia, pb, ib, d)
                if s == 0:
                    s = -1 if items[a][2] < items[b][2] else 1
                # Left of east = north (low track); left of south = east (high).
                a_first = (s < 0) if key[0] == "h" else (s > 0)
                if a_first:
                    before[b].add(a)
                else:
                    before[a].add(b)
        constraints[key] = before

    # 4. Channel widths and block shifts. Widening a channel moves what lies
    #    beyond it, and a corner on a boundary inside a wide block moves
    #    against that block's terminals; so the tracks are packed on the
    #    final geometry, repeated until the track counts settle.
    def geometry(nt):
        def extra(key):
            n = nt.get(key, 0)
            gap = placement.gap_y if key[0] == "h" else placement.gap_x
            return max(0.0, (n - 1) * TRACK_SPACING + 2 * TRACK_MARGIN - gap) if n else 0.0

        ex_c = [extra(("v", i)) for i in range(n_cols + 1)]
        ex_r = [extra(("h", j)) for j in range(n_rows + 1)]
        cum_c = [0.0]
        for e in ex_c:
            cum_c.append(cum_c[-1] + e)
        cum_r = [0.0]
        for e in ex_r:
            cum_r.append(cum_r[-1] + e)
        cx = [i * cw + cum_c[i] + ex_c[i] / 2.0 for i in range(n_cols + 1)]
        cy = [j * ch + cum_r[j] + ex_r[j] / 2.0 for j in range(n_rows + 1)]
        sh = {bid: (cum_c[c + 1], cum_r[r + 1]) for bid, (c, r, _sc, _sr) in cells.items()}
        return cx, cy, sh

    def spans(items, nt, geo):
        cx, cy, sh = geo

        def half(chan):
            return (nt.get(chan, 1) - 1) / 2.0 * TRACK_SPACING + TRACK_SPACING / 2.0

        out = []
        for _lo, _hi, eid, k in items:
            ln = spec[eid]
            lo = hi = None
            for desc, p in (nominal_of[eid][k], nominal_of[eid][k + 1]):
                horizontal = abs(nominal_of[eid][k][1][1] - nominal_of[eid][k + 1][1][1]) < _EPS
                if desc[0] == "c":
                    if horizontal:
                        pos, h = cx[desc[1]], half(("v", desc[1]))
                    else:
                        pos, h = cy[desc[2]], half(("h", desc[2]))
                else:   # channel entry of a terminal: moves with its block
                    blk = ln.s_block if desc[1] == 0 else ln.t_block
                    pos, h = xs[eid][desc[1]] + sh[blk][0], 0.0
                lo = pos - h if lo is None else min(lo, pos - h)
                hi = pos + h if hi is None else max(hi, pos + h)
            out.append((lo, hi))
        return out

    n_tracks: dict[tuple, int] = {}
    nominal_geo = ([i * cw for i in range(n_cols + 1)],
                   [j * ch for j in range(n_rows + 1)],
                   {bid: (0.0, 0.0) for bid in cells})
    track: dict[tuple[str, int], int] = {}
    nt_prev: dict[tuple, int] = {}
    geo = nominal_geo
    for _pass in range(6):
        nt: dict[tuple, int] = {}
        for key, items in runs.items():
            placed, nt[key] = pack(len(items), constraints[key],
                                   spans(items, nt_prev, geo))
            for r, (_lo, _hi, eid, k) in enumerate(items):
                track[(eid, k)] = placed[r]
        if nt == nt_prev:
            break
        nt_prev = nt
        geo = geometry(nt)
    n_tracks = nt
    cx_list, cy_list, shift = geometry(n_tracks)

    def chan_x(i):
        return cx_list[i]

    def chan_y(j):
        return cy_list[j]

    routes: dict[str, list[tuple[float, float]]] = {}
    for eid, nv in nominal_of.items():
        ln = spec[eid]
        dxs, dys = shift[ln.s_block]
        dxt, dyt = shift[ln.t_block]
        sx, tx = xs[eid][0] + dxs, xs[eid][1] + dxt
        # Each segment's line: ("v", x) or ("h", y), in final coordinates.
        seg_lines = [("v", sx)]
        for k in range(1, len(nv) - 2):
            p, q = nv[k][1], nv[k + 1][1]
            key_t = track.get((eid, k), 0)
            if abs(p[1] - q[1]) < _EPS:
                j = int(round(p[1] / ch))
                n = n_tracks.get(("h", j), 1)
                seg_lines.append(("h", chan_y(j) + (key_t - (n - 1) / 2.0) * TRACK_SPACING))
            else:
                i = int(round(p[0] / cw))
                n = n_tracks.get(("v", i), 1)
                seg_lines.append(("v", chan_x(i) + (key_t - (n - 1) / 2.0) * TRACK_SPACING))
        seg_lines.append(("v", tx))

        def at(v):
            """Final position of a nominal vertex (for jogs)."""
            desc, p = v
            if desc[0] == "c":
                return (chan_x(desc[1]), chan_y(desc[2]))
            if desc[0] == "e":
                return ((sx, chan_y(row_of(ln, 0))) if desc[1] == 0
                        else (tx, chan_y(row_of(ln, 1))))
            return (sx, ln.sy + dys) if desc[1] == 0 else (tx, ln.ty + dyt)

        pts = [(sx, ln.sy + dys)]
        for k in range(1, len(seg_lines)):
            (oa, ca), (ob, cb) = seg_lines[k - 1], seg_lines[k]
            if oa != ob:
                pts.append((ca, cb) if oa == "v" else (cb, ca))
            elif abs(ca - cb) > _EPS:
                vx, vy = at(nv[k])
                if oa == "v":
                    pts += [(ca, vy), (cb, vy)]
                else:
                    pts += [(vx, ca), (vx, cb)]
        pts.append((tx, ln.ty + dyt))
        routes[eid] = _clean(pts)

    for eid, pts in fixed.items():
        ln = spec[eid]
        dxs, dys = shift[ln.s_block]
        dxt, dyt = shift[ln.t_block]
        (sx, sy), (_a, ym), (_b, _c), (tx, ty) = pts
        sx, sy, tx, ty = sx + dxs, sy + dys, tx + dxt, ty + dyt
        ym = (sy + ty) / 2.0
        routes[eid] = [(sx, sy), (sx, ym), (tx, ym), (tx, ty)]

    _separate_stubs(routes)
    return Routing(routes=routes, shift=shift)


def _separate_stubs(routes):
    """A terminal stub that lands exactly on another line's vertical track
    (a terminal right on a column channel) would hide in it: move the stub,
    and with it the terminal, half a track sideways."""
    verticals: dict[int, list[list]] = {}     # bucket -> [x, y0, y1, eid]
    for eid, pts in routes.items():
        for p, q in zip(pts, pts[1:]):
            if abs(p[0] - q[0]) < _EPS and abs(p[1] - q[1]) > _EPS:
                verticals.setdefault(int(round(p[0])), []).append(
                    [p[0], min(p[1], q[1]), max(p[1], q[1]), eid])

    def hit(x, y0, y1, eid):
        for k in (int(round(x)) - 1, int(round(x)), int(round(x)) + 1):
            for vx, a, b, other in verticals.get(k, ()):
                if other != eid and abs(vx - x) < 1.0 and min(b, y1) - max(a, y0) > _EPS:
                    return True
        return False

    def move(x, y0, y1, eid, nx):
        bucket = verticals.get(int(round(x)), [])
        for i, (vx, a, b, other) in enumerate(bucket):
            if other == eid and abs(vx - x) < _EPS and abs(a - y0) < _EPS and abs(b - y1) < _EPS:
                del bucket[i]
                break
        verticals.setdefault(int(round(nx)), []).append([nx, y0, y1, eid])

    for eid, pts in routes.items():
        if len(pts) < 3:
            continue
        for i0, i1 in ((0, 1), (len(pts) - 1, len(pts) - 2)):
            (x, y0), (_x1, y1) = pts[i0], pts[i1]
            lo, hi = min(y0, y1), max(y0, y1)
            if not hit(x, lo, hi, eid):
                continue
            for dx in (TRACK_SPACING / 2.0, -TRACK_SPACING / 2.0):
                if not hit(x + dx, lo, hi, eid):
                    move(x, lo, hi, eid, x + dx)
                    pts[i0] = (x + dx, pts[i0][1])
                    pts[i1] = (x + dx, pts[i1][1])
                    break


def _same(p, q) -> bool:
    return abs(p[0] - q[0]) < _EPS and abs(p[1] - q[1]) < _EPS


def _clean(pts):
    """Drop repeated and collinear points."""
    out = [pts[0]]
    for p in pts[1:]:
        if abs(p[0] - out[-1][0]) < _EPS and abs(p[1] - out[-1][1]) < _EPS:
            continue
        if len(out) >= 2:
            a, b = out[-2], out[-1]
            if ((abs(a[0] - b[0]) < _EPS and abs(b[0] - p[0]) < _EPS)
                    or (abs(a[1] - b[1]) < _EPS and abs(b[1] - p[1]) < _EPS)):
                out[-1] = p
                continue
        out.append(p)
    return out
