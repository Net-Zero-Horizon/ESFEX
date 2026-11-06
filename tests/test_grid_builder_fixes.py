"""Grid Builder fixes (#94, #95, #21).

* Building-footprint distribution: a node's bus split counts only the
  buildings of that node (nearest bus overall), not every building of the
  domain snapped to the node's own buses (#94).
* GEM status filter keeps blank-status rows, as documented (#95).
* The build step offers only the simplification levels that differ (#21).
"""

from __future__ import annotations

import math
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

gpd = pytest.importorskip("geopandas")
pytest.importorskip("PySide6")
from shapely.geometry import Polygon


def _square(cx, cy, area=100.0):
    side = math.sqrt(area) / 111320
    return Polygon([(cx - side / 2, cy - side / 2), (cx + side / 2, cy - side / 2),
                    (cx + side / 2, cy + side / 2), (cx - side / 2, cy + side / 2)])


def _bus(bid, lat, lng, node):
    return SimpleNamespace(bus_id=bid, name=bid, latitude=lat, longitude=lng,
                           parent_node=node, demand_fraction=0.5)


def _run_worker(buildings, targets, reference=None):
    from esfex.visualization.workflows.demand_analysis import (
        BuildingTypeRule,
        ClassifyDistributeWorker,
    )

    gdf = gpd.GeoDataFrame(
        {"footprint_area_m2": [100.0] * len(buildings),
         "geometry": [_square(x, y) for x, y in buildings]},
        crs="EPSG:4326")
    rules = [BuildingTypeRule(name="All", area_min_m2=0.0, area_max_m2=1e9,
                              weight_per_m2=1.0)]
    out = {}
    w = ClassifyDistributeWorker(gdf, rules, 1.0, targets, reference_buses=reference)
    w.finished.connect(lambda a, s: out.setdefault("a", a))
    w.error.connect(lambda m: out.setdefault("err", m))
    w.run()
    assert "err" not in out, out.get("err")
    return {r["bus_id"]: r for r in out["a"]}


def test_node_split_uses_only_its_own_buildings():
    # Node 0 has buses A (x=0) and B (x=1); node 1 has buses C (x=10), D (x=11).
    a, b = _bus("A", 0.0, 0.0, 0), _bus("B", 0.0, 1.0, 0)
    c, d = _bus("C", 0.0, 10.0, 1), _bus("D", 0.0, 11.0, 1)
    targets = [
        {"node_index": 0, "node_name": "N0", "buses": [a, b]},
        {"node_index": 1, "node_name": "N1", "buses": [c, d]},
    ]
    # Node 0: 1 building next to A, 1 next to B. Node 1: 8 buildings near C.
    blds = [(0.0, 0.0), (1.0, 0.0)] + [(10.0 + 0.01 * i, 0.0) for i in range(8)]
    res = _run_worker(blds, targets)
    # Before: all 8 buildings of node 1 were snapped to B (nearest of A/B),
    # giving node 0 a 1/9 : 8/9 split. They belong to node 1.
    assert res["A"]["new_fraction"] == pytest.approx(0.5)
    assert res["B"]["new_fraction"] == pytest.approx(0.5)
    assert res["A"]["building_count"] + res["B"]["building_count"] == 2
    assert res["C"]["new_fraction"] == pytest.approx(1.0)
    assert res["D"]["building_count"] == 0


def test_reference_buses_of_non_target_nodes_claim_their_buildings():
    """A single-bus node is not a target, but its buildings are not ours."""
    a, b = _bus("A", 0.0, 0.0, 0), _bus("B", 0.0, 1.0, 0)
    targets = [{"node_index": 0, "node_name": "N0", "buses": [a, b]}]
    reference = [(0.0, 0.0, (None, 0)), (0.0, 1.0, (None, 0)),
                 (0.0, 5.0, (None, 2))]          # node 2's only bus at x=5
    blds = [(0.0, 0.0)] + [(5.0, 0.0)] * 5
    res = _run_worker(blds, targets, reference)
    assert res["A"]["new_fraction"] == pytest.approx(1.0)
    assert res["B"]["new_fraction"] == pytest.approx(0.0)


def test_node_without_buildings_splits_equally():
    a, b = _bus("A", 0.0, 0.0, 0), _bus("B", 0.0, 1.0, 0)
    c = _bus("C", 0.0, 10.0, 1)
    targets = [{"node_index": 0, "node_name": "N0", "buses": [a, b]}]
    reference = [(0.0, 0.0, (None, 0)), (0.0, 1.0, (None, 0)), (0.0, 10.0, (None, 1))]
    res = _run_worker([(10.0, 0.0)], targets, reference)
    assert res["A"]["new_fraction"] == pytest.approx(0.5)
    assert res["B"]["new_fraction"] == pytest.approx(0.5)


# ── GEM status (#95) ─────────────────────────────────────────────────


@pytest.mark.parametrize("value,kept", [
    ("Operating", True), (" operating ", True), ("", True), (None, True),
    (float("nan"), True), ("construction", False), ("mothballed", False),
    ("retired", False),
])
def test_gem_status_filter(value, kept):
    from esfex.visualization.workflows.grid_mapping_fetchers import _gem_status_kept

    assert _gem_status_kept(value) is kept


def test_gem_fetch_keeps_blank_status_rows(monkeypatch):
    import pandas as pd

    from esfex.visualization.workflows import grid_mapping_fetchers as F

    df = pd.DataFrame({
        "Status": ["operating", None, "construction", ""],
        "Latitude": [1.0, 1.1, 1.2, 1.3],
        "Longitude": [1.0, 1.1, 1.2, 1.3],
        "Capacity (MW)": [50.0, 60.0, 70.0, 80.0],
        "Type": ["coal", "solar", "wind", "nuclear"],
        "Plant / Project name": ["A", "B", "C", "D"],
    })
    monkeypatch.setattr(F, "_http_get_with_retry",
                        lambda *a, **k: SimpleNamespace(content=b""))
    monkeypatch.setattr(pd, "read_excel", lambda *a, **k: df.copy())
    fetcher = F.GEMGridFetcher((0.0, 0.0, 2.0, 2.0), min_capacity_mw=1.0)
    feats = fetcher._fetch()
    names = sorted(f.name for f in feats)
    assert names == ["A", "B", "D"]


# ── Simplification levels (#21) ──────────────────────────────────────


def test_build_step_offers_distinct_levels_only():
    from PySide6.QtWidgets import QApplication

    from esfex.visualization.data.gui_model import GuiModel
    from esfex.visualization.workflows.grid_mapping_steps import GridMappingBuildStep

    QApplication.instance() or QApplication([])
    step = GridMappingBuildStep(GuiModel(), {})
    combo = step._combo_simplify
    assert [combo.itemData(i) for i in range(combo.count())] == [0, 1, 2]
    assert "Radial" not in combo.itemText(2)
