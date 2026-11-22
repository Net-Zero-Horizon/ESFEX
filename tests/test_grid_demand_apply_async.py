"""Apply Demand Fractions runs off the GUI thread and shows its progress.

Writing every node's multi-year demand to CSV takes a while (~45 s for 136
nodes x 25 years). It ran on the GUI thread: the wizard froze, with no sign of
progress, while the progress bar only followed the quick Distribute action.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QApplication  # noqa: E402


def _wizard(tmp_path, n_nodes=6, years=3):
    from esfex.visualization.data.gui_model import GuiBus, GuiModel
    from esfex.visualization.workflows.grid_mapping_wizard import GridMappingWizard
    QApplication.instance() or QApplication([])
    model = GuiModel()
    while model.state.nodes:
        model.remove_node(len(model.state.nodes) - 1)
    for i in range(n_nodes):
        idx = model.add_node(f"N{i}")
        model.update_node(idx, centroid_lat=21.0 + 0.1 * i, centroid_lng=-80.0)
    model.state.buses = {"b0": GuiBus(bus_id="b0", parent_node=0, role="load",
                                      demand_fraction=1.0)}
    w = GridMappingWizard(None, model)
    w._config_path = str(tmp_path / "config.yaml")
    step = w._step_demand
    step._model = model
    rng = np.random.default_rng(0)
    series = rng.random((8760 * years, n_nodes)) * 50
    step._forecast_result = SimpleNamespace(
        demand_multi_year=series, peak_mw=[50.0] * n_nodes,
        annual_gwh=[1.0] * n_nodes, config=SimpleNamespace(base_year=2025))
    step._assignments = [{"bus_id": "b0", "new_fraction": 0.5}]
    # The wizard's navigation as on its last step
    w._current_step = len(w._steps) - 1
    w._update_navigation()
    return w, step, model


def _wait(app, cond, timeout=60.0):
    t0 = time.monotonic()
    while not cond():
        app.processEvents()
        if time.monotonic() - t0 > timeout:
            raise TimeoutError
        time.sleep(0.01)


def test_apply_runs_in_the_background(tmp_path):
    app = QApplication.instance() or QApplication([])
    w, step, model = _wizard(tmp_path)
    busy = []
    step.busyChanged.connect(busy.append)
    # Every value the bar takes: polling it between event loops can miss
    # them all when one loop delivers the queued updates at once.
    seen = set()
    step._apply_progress.valueChanged.connect(seen.add)
    t0 = time.monotonic()
    step._apply_all()
    assert time.monotonic() - t0 < 1.0          # returned at once
    assert busy == [True]
    assert not w._btn_next.isEnabled()          # Close held while it runs
    assert not step._btn_apply.isEnabled()
    _wait(app, lambda: busy == [True, False])
    assert seen - {0, 100}                      # it showed intermediate progress
    assert step._apply_progress.value() == 100
    assert w._btn_next.isEnabled()
    nodes = model.state.nodes
    assert all(n.demand is not None and n.demand.num_hours == 8760 * 3 for n in nodes)
    assert all((tmp_path / "demand" / f"demand_N{i}.csv").exists() for i in range(6))
    assert model.state.buses["b0"].demand_fraction == 0.5
    assert step._lbl_apply_status.text() == "Applied demand to 6 node(s) and fractions to 1 bus(es)."
    w.deleteLater()


def test_cancel_stores_nothing(tmp_path):
    app = QApplication.instance() or QApplication([])
    w, step, model = _wizard(tmp_path, n_nodes=12, years=5)
    step._apply_all()
    step.cancel_all()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 3.0:
        app.processEvents()
        time.sleep(0.01)
    assert all(n.demand is None or n.demand.num_hours != 8760 * 5
               for n in model.state.nodes)
    assert model.state.buses["b0"].demand_fraction == 1.0
    w.deleteLater()
