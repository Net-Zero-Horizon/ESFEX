"""Orthogonal obstacle-avoidance router for the single-line diagram.

The PowerFactory-style SLD lays buses out as horizontal busbars in voltage
rows, with transformers drawn as clean verticals between rows and equipment
hanging on short stubs below each bar.  Transmission lines connect busbar to
busbar.  A naive lane route for a line crosses straight over whatever sits in
the gap it traverses — most visibly the transformer legs that span a whole
inter-row gap, so there is *no* clear horizontal Y to slip through.

This module routes a line orthogonally **around** every element.  Obstacles
are the busbars (horizontal rectangles) and the vertical legs (transformer and
equipment stubs).  Routing happens on a coarse grid whose horizontal tracks
are the clear lanes inside each inter-row channel plus the always-clear bands
above the top row and below the bottom row, and whose vertical tracks are the
clear gaps between bars (and between node columns).  An A* search finds the
shortest orthogonal path that never crosses an obstacle; a congestion penalty
spreads parallel circuits onto distinct lanes so they keep their separation.

The caller (``graph_builder``) only routes lines whose cheap "simple" route is
already blocked, so clean diagrams are untouched and the search runs on the
hard cases alone.
"""

from __future__ import annotations

import bisect
import heapq

_EPS = 0.5

# Lightweight diagnostics (success/failure of the A* fallback). Reset per build.
route_stats = {"calls": 0, "ok": 0, "fail": 0}


class Obstacles:
    """Bars + vertical legs, with fast segment-intersection queries."""

    def __init__(self, bars, vlegs):
        # bars: list of (x0, x1, y0, y1, gid)
        # vlegs: list of (x, y0, y1)   transformer / equipment legs
        self.bars = list(bars)
        # bucket bars by the row band (y0) so a vertical query only scans bars
        # whose band can intersect it.
        self._bar_y0 = sorted({b[2] for b in self.bars})
        self._bars_by_y0: dict[float, list] = {}
        for b in self.bars:
            self._bars_by_y0.setdefault(b[2], []).append(b)
        self.vlegs = sorted(vlegs, key=lambda v: v[0])
        self._leg_x = [v[0] for v in self.vlegs]

    def h_blocked(self, x0, x1, y) -> bool:
        """A horizontal segment at ``y`` from x0..x1 crossing any leg?"""
        if x1 < x0:
            x0, x1 = x1, x0
        lo = bisect.bisect_left(self._leg_x, x0 - _EPS)
        hi = bisect.bisect_right(self._leg_x, x1 + _EPS)
        for i in range(lo, hi):
            lx, ly0, ly1 = self.vlegs[i]
            if x0 - _EPS < lx < x1 + _EPS and ly0 - _EPS < y < ly1 + _EPS:
                return True
        return False

    def v_blocked(self, x, y0, y1, ignore_bars=()) -> bool:
        """A vertical segment at ``x`` from y0..y1 piercing any bar or leg?"""
        if y1 < y0:
            y0, y1 = y1, y0
        for by0 in self._bar_y0:
            if by0 > y1 + _EPS:
                break
            for (bx0, bx1, b0, b1, gid) in self._bars_by_y0[by0]:
                if gid in ignore_bars:
                    continue
                # Strict overlap: a segment that only *touches* a bar's edge at
                # its own endpoint (e.g. a riser starting on the bar plane and
                # going away from it) does not pierce the bar.
                if bx0 - _EPS < x < bx1 + _EPS and not (b1 <= y0 + _EPS or b0 >= y1 - _EPS):
                    return True
        # Don't run on top of a leg (would visually merge with it).
        i = bisect.bisect_left(self._leg_x, x - _EPS)
        while i < len(self.vlegs) and self.vlegs[i][0] < x + _EPS:
            _lx, ly0, ly1 = self.vlegs[i]
            if not (ly1 < y0 - _EPS or ly0 > y1 + _EPS):
                return True
            i += 1
        return False


class GridRouter:
    """A* orthogonal router over clear horizontal lanes / vertical tracks."""

    def __init__(self, obstacles: Obstacles, lane_ys, x_tracks,
                 lane_step, turn_penalty=40.0):
        self.ob = obstacles
        self.lane_ys = sorted(set(lane_ys))           # candidate horizontal Ys
        self.x_tracks = sorted(set(x_tracks))         # candidate vertical Xs
        self.lane_step = lane_step
        self.turn_penalty = turn_penalty
        # congestion: how many routes already use each grid edge
        self._use_h: dict[tuple, int] = {}
        self._use_v: dict[tuple, int] = {}

    # -- congestion helpers ------------------------------------------------
    def _hkey(self, xa, xb, y):
        return (round(min(xa, xb)), round(max(xa, xb)), round(y))

    def _vkey(self, x, ya, yb):
        return (round(x), round(min(ya, yb)), round(max(ya, yb)))

    def commit(self, pts):
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if abs(y0 - y1) < _EPS:
                self._use_h[self._hkey(x0, x1, y0)] = \
                    self._use_h.get(self._hkey(x0, x1, y0), 0) + 1
            else:
                self._use_v[self._vkey(x0, y0, y1)] = \
                    self._use_v.get(self._vkey(x0, y0, y1), 0) + 1

    # -- the search --------------------------------------------------------
    def route(self, sx, sy_face, src_gid, tx, ty_face, tgt_gid,
              x_window=None):
        """Return a list of (x, y) waypoints from the src face to the tgt face,
        or ``None`` if no clear orthogonal route is found."""
        ob = self.ob
        route_stats["calls"] += 1
        # Local X tracks: those inside the window plus the two terminal Xs.
        if x_window is not None:
            lo, hi = x_window
            xs = [x for x in self.x_tracks if lo <= x <= hi]
        else:
            xs = list(self.x_tracks)
        xs = sorted(set(xs) | {sx, tx})
        ys = list(self.lane_ys)

        # Start / goal are the bar faces; the first/last move is a stub at the
        # terminal X from the face to a lane, ignoring the terminal's own bar.
        start = (sx, sy_face)
        goal = (tx, ty_face)

        x_index = {x: i for i, x in enumerate(xs)}
        y_index = {y: i for i, y in enumerate(ys)}

        def neighbors(node):
            x, y = node
            out = []
            if node == start or node == goal:
                # stub: connect the face to every lane reachable straight up/down
                own = src_gid if node == start else tgt_gid
                for ny in ys:
                    if abs(ny - y) < _EPS:
                        continue
                    if not ob.v_blocked(x, y, ny, ignore_bars={own}):
                        out.append(((x, ny), abs(ny - y), True))
                return out
            xi = x_index.get(x)
            yi = y_index.get(y)
            if xi is None or yi is None:
                return out
            # horizontal moves to adjacent x-tracks
            for nxi in (xi - 1, xi + 1):
                if 0 <= nxi < len(xs):
                    nx = xs[nxi]
                    if not ob.h_blocked(x, nx, y):
                        cong = self._use_h.get(self._hkey(x, nx, y), 0)
                        out.append(((nx, y), abs(nx - x) + cong * self.lane_step, False))
            # vertical moves to adjacent lanes
            for nyi in (yi - 1, yi + 1):
                if 0 <= nyi < len(ys):
                    ny = ys[nyi]
                    if not ob.v_blocked(x, y, ny):
                        cong = self._use_v.get(self._vkey(x, y, ny), 0)
                        out.append(((x, ny), abs(ny - y) + cong * self.lane_step, True))
            # allow stepping onto the goal face when aligned in x
            if abs(x - tx) < _EPS:
                if not ob.v_blocked(tx, y, ty_face, ignore_bars={tgt_gid}):
                    out.append((goal, abs(ty_face - y), True))
            return out

        def h(node):
            return abs(node[0] - tx) + abs(node[1] - ty_face)

        # Dijkstra/A* with turn penalty (state carries last move orientation)
        # state = (x, y, was_vertical)
        start_state = (start, None)
        pq = [(h(start), 0.0, start, None, None)]
        best: dict[tuple, float] = {}
        came: dict[tuple, tuple] = {}
        found = None
        while pq:
            _f, g, node, was_v, parent_state = heapq.heappop(pq)
            state = (node, was_v)
            if state in best and best[state] <= g:
                continue
            best[state] = g
            came[state] = parent_state
            if node == goal:
                found = state
                break
            for nxt, cost, is_v in neighbors(node):
                tp = self.turn_penalty if (was_v is not None and is_v != was_v) else 0.0
                ng = g + cost + tp
                nstate = (nxt, is_v)
                if nstate in best and best[nstate] <= ng:
                    continue
                heapq.heappush(pq, (ng + h(nxt), ng, nxt, is_v, state))
        if found is None:
            route_stats["fail"] += 1
            return None
        route_stats["ok"] += 1
        # reconstruct
        pts = []
        st = found
        while st is not None:
            pts.append(st[0])
            st = came.get(st)
        pts.reverse()
        # collapse collinear points
        out = [pts[0]]
        for p in pts[1:]:
            if len(out) >= 2:
                a, b = out[-2], out[-1]
                if (abs(a[0] - b[0]) < _EPS and abs(b[0] - p[0]) < _EPS) or \
                   (abs(a[1] - b[1]) < _EPS and abs(b[1] - p[1]) < _EPS):
                    out[-1] = p
                    continue
            out.append(p)
        return out
