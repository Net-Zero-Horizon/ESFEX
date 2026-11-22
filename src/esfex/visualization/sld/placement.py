"""Geographic block placement for the single-line diagram.

Each site (substation or junction, see :mod:`.substations`) is a rectangular
block of known pixel size. The blocks are placed on a regular grid of cells
so that

* the diagram keeps the shape of the system: a block lands as close as
  possible to its projected latitude / longitude;
* blocks never overlap and always keep a clear channel between them — the
  cell boundaries — that lines are routed along;
* connected blocks are pulled together where geography leaves room, which
  shortens lines (a few local refinement passes);
* long empty stretches (sea, unconnected land) are collapsed.

Large blocks span several cells. The scale is chosen from the data, so a
dense urban cluster is spread out while sparse regions keep their layout.

Pure Python (no Qt), so it is unit-testable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

CELL_W = 100.0
CELL_H = 100.0
GAP_X = 60.0          # minimum clear channel between two blocks, px
GAP_Y = 60.0
FILL = 0.22           # share of the occupied region covered by blocks
REFINE_PASSES = 4
MAX_EMPTY = 2         # empty grid rows / columns kept in a row (rest collapsed)


@dataclass
class Block:
    bid: str
    w: float
    h: float
    lat: float
    lon: float


@dataclass
class Placement:
    cell_w: float
    cell_h: float
    gap_x: float
    gap_y: float
    n_cols: int
    n_rows: int
    # bid -> (col, row, span_cols, span_rows)
    cells: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)
    # bid -> (x, y) top-left of the block footprint, px
    xy: dict[str, tuple[float, float]] = field(default_factory=dict)

    def owner_grid(self) -> dict[tuple[int, int], str]:
        occ: dict[tuple[int, int], str] = {}
        for bid, (c, r, sc, sr) in self.cells.items():
            for i in range(sc):
                for j in range(sr):
                    occ[(c + i, r + j)] = bid
        return occ


def _project(blocks: list[Block]) -> dict[str, tuple[float, float]]:
    """Equirectangular projection to km, north up (y grows southwards)."""
    lat0 = math.radians(sum(b.lat for b in blocks) / len(blocks))
    kx = 111.32 * math.cos(lat0)
    ky = 110.57
    return {b.bid: (b.lon * kx, -b.lat * ky) for b in blocks}


def _nearest_neighbour_km(km: dict[str, tuple[float, float]]) -> list[float]:
    """Distance from each point to its nearest distinct neighbour (bucketed)."""
    pts = list(km.values())
    if len(pts) < 2:
        return []
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1e-9)
    cell = span / max(1.0, math.sqrt(len(pts)))
    buckets: dict[tuple[int, int], list[int]] = {}
    for i, (x, y) in enumerate(pts):
        buckets.setdefault((int(x // cell), int(y // cell)), []).append(i)
    out = []
    for i, (x, y) in enumerate(pts):
        cx, cy = int(x // cell), int(y // cell)
        best = math.inf
        ring = 0
        while ring < 4 or (best == math.inf and ring < 64):
            for dx in range(-ring, ring + 1):
                for dy in range(-ring, ring + 1):
                    if max(abs(dx), abs(dy)) != ring:
                        continue
                    for j in buckets.get((cx + dx, cy + dy), ()):
                        if j == i:
                            continue
                        d = math.hypot(pts[j][0] - x, pts[j][1] - y)
                        if 1e-6 < d < best:
                            best = d
            if best < (ring) * cell:
                break
            ring += 1
        if best < math.inf:
            out.append(best)
    return out


def _occupied_area_km2(pts: list[tuple[float, float]]) -> float:
    """Area of the raster cells (plus their direct neighbours) that hold at
    least one point; the raster pitch follows the mean point spacing."""
    if len(pts) < 2:
        return 0.0
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    ex, ey = max(xs) - min(xs), max(ys) - min(ys)
    if ex <= 0 and ey <= 0:
        return 0.0
    pitch = math.sqrt(max(ex, 1e-6) * max(ey, 1e-6) / len(pts)) * 2.0
    pitch = max(pitch, max(ex, ey) / 200.0)
    cells = {(int((x - min(xs)) // pitch), int((y - min(ys)) // pitch)) for x, y in pts}
    grown = {(cx + dx, cy + dy) for cx, cy in cells for dx in (-1, 0, 1) for dy in (-1, 0, 1)
             if (cx + dx, cy + dy) in cells or (dx == 0 or dy == 0)}
    return len(grown) * pitch * pitch


def _median(v: list[float]) -> float:
    s = sorted(v)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


class _Grid:
    def __init__(self):
        self.occ: dict[tuple[int, int], str] = {}

    def free(self, c, r, sc, sr, own=None) -> bool:
        occ = self.occ
        for i in range(sc):
            for j in range(sr):
                o = occ.get((c + i, r + j))
                if o is not None and o != own:
                    return False
        return True

    def put(self, bid, c, r, sc, sr):
        for i in range(sc):
            for j in range(sr):
                self.occ[(c + i, r + j)] = bid

    def take(self, c, r, sc, sr):
        for i in range(sc):
            for j in range(sr):
                self.occ.pop((c + i, r + j), None)

    def nearest_free(self, sc, sr, tx, ty, own=None) -> tuple[int, int]:
        """Top-left cell of the free ``sc``×``sr`` rectangle whose centre is
        closest to (tx, ty), searched in growing square rings."""
        c0 = int(round(tx - sc / 2.0))
        r0 = int(round(ty - sr / 2.0))
        best = None
        best_d = math.inf
        ring = 0
        while True:
            if ring - 1.0 > best_d:
                break
            for dc in range(-ring, ring + 1):
                for dr in range(-ring, ring + 1):
                    if max(abs(dc), abs(dr)) != ring:
                        continue
                    c, r = c0 + dc, r0 + dr
                    d = math.hypot(c + sc / 2.0 - tx, r + sr / 2.0 - ty)
                    if d < best_d and self.free(c, r, sc, sr, own):
                        best_d, best = d, (c, r)
            ring += 1
        return best


def place_blocks(
    blocks: list[Block],
    links: list[tuple[str, str]] = (),
    cell_w: float = CELL_W,
    cell_h: float = CELL_H,
    gap_x: float = GAP_X,
    gap_y: float = GAP_Y,
    fill: float = FILL,
    refine_passes: int = REFINE_PASSES,
    max_empty: int = MAX_EMPTY,
) -> Placement:
    """Place ``blocks`` on the cell grid; ``links`` are block-id pairs joined
    by at least one line (used to pull neighbours together)."""
    pl = Placement(cell_w, cell_h, gap_x, gap_y, 0, 0)
    if not blocks:
        return pl

    span = {b.bid: (max(1, math.ceil((b.w + gap_x) / cell_w)),
                    max(1, math.ceil((b.h + gap_y) / cell_h))) for b in blocks}
    km = _project(blocks)

    # Scale (cells per km): the blocks should cover about `fill` of the
    # region the system occupies. The region is measured on a coarse raster
    # of the projected sites, so sea and empty land between parts of the
    # system don't count (a straight bounding box or the nearest-neighbour
    # spacing would leave a long island or a dense city far too sparse).
    total_cells = sum(sc * sr for sc, sr in span.values())
    area = _occupied_area_km2(list(km.values()))
    if area > 0:
        scale = math.sqrt(total_cells / (fill * area))
    else:
        nn = _nearest_neighbour_km(km)
        d_med = _median(nn)
        size = _median([(sc + sr) / 2.0 for sc, sr in span.values()])
        scale = 1.6 * size / d_med if d_med > 0 else 1.0
    minx = min(p[0] for p in km.values())
    miny = min(p[1] for p in km.values())
    target = {bid: ((x - minx) * scale, (y - miny) * scale)
              for bid, (x, y) in km.items()}

    nbr_sets: dict[str, set[str]] = {b.bid: set() for b in blocks}
    for a, b in links:
        if a in nbr_sets and b in nbr_sets and a != b:
            nbr_sets[a].add(b)
            nbr_sets[b].add(a)
    # Sorted, so float sums (and thus the layout) don't depend on set order.
    nbrs = {k: sorted(v) for k, v in nbr_sets.items()}

    # Greedy: big blocks first (they are hardest to fit), then by degree.
    order = sorted(blocks, key=lambda b: (-span[b.bid][0] * span[b.bid][1],
                                          -len(nbrs[b.bid]), b.bid))
    grid = _Grid()
    pos: dict[str, tuple[int, int]] = {}
    for b in order:
        sc, sr = span[b.bid]
        tx, ty = target[b.bid]
        c, r = grid.nearest_free(sc, sr, tx, ty)
        grid.put(b.bid, c, r, sc, sr)
        pos[b.bid] = (c, r)

    def centre(bid, c=None, r=None):
        sc, sr = span[bid]
        if c is None:
            c, r = pos[bid]
        return c + sc / 2.0, r + sr / 2.0

    def cost(bid, c, r):
        cx, cy = centre(bid, c, r)
        tx, ty = target[bid]
        alpha = max(1.0, float(len(nbrs[bid])))
        v = alpha * ((cx - tx) ** 2 + (cy - ty) ** 2)
        for n in nbrs[bid]:
            nx, ny = centre(n)
            v += (cx - nx) ** 2 + (cy - ny) ** 2
        return v

    # Refinement: move each block towards the balance point of its
    # geographic target and its neighbours, when that lowers its cost.
    for _ in range(max(0, refine_passes)):
        moved = 0
        for b in order:
            bid = b.bid
            ns = nbrs[bid]
            if not ns:
                continue
            sc, sr = span[bid]
            tx, ty = target[bid]
            alpha = max(1.0, float(len(ns)))
            ix = alpha * tx
            iy = alpha * ty
            for n in ns:
                nx, ny = centre(n)
                ix += nx
                iy += ny
            ix /= alpha + len(ns)
            iy /= alpha + len(ns)
            c, r = grid.nearest_free(sc, sr, ix, iy, own=bid)
            if (c, r) == pos[bid]:
                continue
            if cost(bid, c, r) < cost(bid, *pos[bid]) - 1e-9:
                grid.take(*pos[bid], sc, sr)
                grid.put(bid, c, r, sc, sr)
                pos[bid] = (c, r)
                moved += 1
        if not moved:
            break

    # Collapse long empty stretches of columns / rows.
    def _remap(used: set[int]) -> dict[int, int]:
        lo, hi = min(used), max(used)
        out = {}
        new = 0
        empty = 0
        for k in range(lo, hi + 1):
            if k in used:
                empty = 0
                out[k] = new
                new += 1
            else:
                empty += 1
                if empty <= max_empty:
                    out[k] = new
                    new += 1
        out[hi + 1] = new
        return out

    used_c: set[int] = set()
    used_r: set[int] = set()
    for bid, (c, r) in pos.items():
        sc, sr = span[bid]
        used_c.update(range(c, c + sc))
        used_r.update(range(r, r + sr))
    cmap = _remap(used_c)
    rmap = _remap(used_r)

    for b in blocks:
        c, r = pos[b.bid]
        sc, sr = span[b.bid]
        nc, nr = cmap[c], rmap[r]
        pl.cells[b.bid] = (nc, nr, sc, sr)
        x = nc * cell_w + gap_x / 2.0 + (sc * cell_w - gap_x - b.w) / 2.0
        y = nr * cell_h + gap_y / 2.0
        pl.xy[b.bid] = (x, y)
    pl.n_cols = max(c + sc for c, _r, sc, _sr in pl.cells.values())
    pl.n_rows = max(r + sr for _c, r, _sc, sr in pl.cells.values())
    return pl
