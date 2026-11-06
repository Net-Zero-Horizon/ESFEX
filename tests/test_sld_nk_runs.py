"""SLD Analysis "Run All N-1" and "Run Screening" (#90).

The buttons called ``analyze_all_contingencies`` (which does not exist),
``analyze_n1_1`` with the wrong signature and ``r.pi`` on dicts, so every
run ended in "Error: ...". They now go through ``run_all_contingencies`` /
``screen_all_contingencies``, which work for the DC analyzer and for the AC
analyzer (through its DC fallback where PTDF/LODF are needed).
"""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from esfex.analysis.contingency import (
    ContingencyAnalyzer,
    ContingencyResult,
    run_all_contingencies,
    screen_all_contingencies,
)
from tests.test_contingency import _make_extended_snapshot, _make_extended_system


def _analyzer():
    lines, gens, bats, trafos = _make_extended_system()
    return ContingencyAnalyzer(
        lines=lines, generators=gens, num_nodes=3,
        batteries=bats, transformers=trafos,
    )


def test_run_all_n1_trips_every_contingency():
    an, snap = _analyzer(), _make_extended_snapshot()
    expected = an.get_contingency_list(snap)
    results = run_all_contingencies(an, snap)
    assert len(results) == len(expected)
    assert all(isinstance(r, ContingencyResult) for r in results)
    assert {r.contingency_type for r in results} >= {"generator", "line"}


def test_run_all_n1_filters_types():
    an, snap = _analyzer(), _make_extended_snapshot()
    results = run_all_contingencies(an, snap, types={"generator", "line"})
    assert results
    assert {r.contingency_type for r in results} <= {"generator", "line"}
    ids = {r.element_id for r in results}
    assert "trafo_0" not in ids and "bat_0" not in ids


def test_min_line_loading_skips_lightly_loaded_lines():
    """critical_line_threshold: only lines loaded >= 35 % are tripped."""
    an, snap = _analyzer(), _make_extended_snapshot()
    results = run_all_contingencies(
        an, snap, types={"line"}, min_line_loading=0.35)
    # line_0 carries 40 MW of 100; line_1/line_2 carry 10 and 20 MW.
    assert [r.element_id for r in results] == ["line_0"]
    ranked, n_total = screen_all_contingencies(
        an, snap, types={"line"}, min_line_loading=0.35)
    assert n_total == 1


def test_run_all_restores_redistribution_mode():
    an, snap = _analyzer(), _make_extended_snapshot()
    run_all_contingencies(an, snap, redistribution="droop")
    assert an.redistribution_mode == "pro_rata"


def test_run_all_n1_1_returns_results():
    an, snap = _analyzer(), _make_extended_snapshot()
    results = run_all_contingencies(
        an, snap, depth="n1_1", stress_threshold_pct=0.0, max_pairs=5)
    assert 0 < len(results) <= 5
    assert all("N-1-1" in r.element_description for r in results)


def test_screening_counts_and_threshold():
    an, snap = _analyzer(), _make_extended_snapshot()
    ranked, n_total = screen_all_contingencies(an, snap, pi_threshold=0.0)
    assert n_total == len(an.get_contingency_list(snap))
    assert len(ranked) == n_total
    pis = [r["pi"] for r in ranked]
    assert pis == sorted(pis, reverse=True)
    high, total2 = screen_all_contingencies(an, snap, pi_threshold=pis[0] + 1)
    assert high == [] and total2 == n_total


def test_screening_with_ac_analyzer_uses_dc_fallback():
    dc, snap = _analyzer(), _make_extended_snapshot()
    ac_like = SimpleNamespace(_dc_fallback=dc)
    ranked, n_total = screen_all_contingencies(ac_like, snap)
    assert n_total > 0 and len(ranked) == n_total


def _fake_window(analyzer, monkeypatch, **gs):
    from esfex.analysis import snapshot_builder
    from esfex.visualization.main_window import MainWindow

    monkeypatch.setattr(
        snapshot_builder, "build_snapshot_from_scenario",
        lambda state, scenario: _make_extended_snapshot())
    summaries = []
    g = SimpleNamespace(
        n1_transformer_contingencies=gs.get("transformers", True),
        n1_battery_contingencies=gs.get("batteries", True),
        n1_critical_line_threshold=gs.get("crit", 0.0),
    )
    fake = SimpleNamespace(
        _sld_contingency_analyzer=analyzer,
        _analysis_panel=SimpleNamespace(
            update_nk_summary=summaries.append, get_scenario=lambda: None),
        model=SimpleNamespace(state=None, global_settings=g),
    )
    fake._nk_contingency_types = MainWindow._nk_contingency_types.__get__(fake)
    fake.run_all = MainWindow._on_run_all_n1.__get__(fake)
    fake.screen = MainWindow._on_run_screening.__get__(fake)
    return fake, summaries


@pytest.mark.parametrize("depth", ["n1", "n1_1"])
def test_run_all_button_reports_counts(monkeypatch, depth):
    fake, summaries = _fake_window(_analyzer(), monkeypatch)
    fake.run_all(depth, "pro_rata", 0.0)
    assert summaries and not summaries[-1].startswith("Error"), summaries
    assert summaries[-1].startswith("N-1-1" if depth == "n1_1" else "N-1:")


def test_run_all_button_honours_global_types(monkeypatch):
    fake, summaries = _fake_window(
        _analyzer(), monkeypatch, transformers=False, batteries=False)
    fake.run_all("n1", "pro_rata", 0.0)
    an, snap = _analyzer(), _make_extended_snapshot()
    n_expected = sum(
        1 for c in an.get_contingency_list(snap)
        if c["type"] in ("generator", "line"))
    assert summaries[-1].startswith(f"N-1: {n_expected} contingencies")


def test_screening_button_reports_counts(monkeypatch):
    fake, summaries = _fake_window(_analyzer(), monkeypatch)
    fake.screen("pro_rata", 0.001)
    assert summaries[-1].startswith("Screening: "), summaries
    assert "Error" not in summaries[-1]


def test_failed_run_is_reported_not_raised(monkeypatch):
    """A failing analyzer ends in an "Error:" summary. ``log`` was undefined in
    main_window, so the except branch itself raised NameError."""
    class _Broken:
        def get_contingency_list(self, snapshot):
            raise RuntimeError("boom")

    fake, summaries = _fake_window(_Broken(), monkeypatch)
    fake.run_all("n1", "pro_rata", 0.0)
    assert summaries == ["Error: boom"]


def test_analysis_panel_nk_defaults():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from esfex.visualization.data.gui_model import GuiModel
    from esfex.visualization.panels.analysis_panel import AnalysisPanel

    QApplication.instance() or QApplication([])
    panel = AnalysisPanel(GuiModel())
    panel.set_nk_defaults("n1_1", "droop", 2.5)
    assert panel._nk_depth_combo.currentData() == "n1_1"
    assert panel._nk_redispatch_combo.currentData() == "droop"
    assert panel._nk_pi_threshold.value() == pytest.approx(2.5)
