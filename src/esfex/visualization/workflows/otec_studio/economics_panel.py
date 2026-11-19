# -*- coding: utf-8 -*-
"""OTEC Studio — Economics panel (M3).

Runs the nominal on-design cost model, then layers OTEX's lifetime power
degradation to compute an NPV-based LCOE — surfacing how degradation compounds
over a 20-30 year life (the wizard reports only point-in-time LCOE). Shows the
CAPEX component breakdown, the per-year power curve, and the nameplate-vs-NPV
LCOE gap. The cost level (low/high) and degradation model are user-selectable.
"""

from __future__ import annotations

import logging

import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from esfex.visualization.i18n import tr
from esfex.visualization.workflows.otec_studio import economics as eco
from esfex.visualization.workflows.otec_studio.project import StudioConfig
from esfex.visualization.workflows.otec_studio.workers import EconomicsWorker

logger = logging.getLogger(__name__)


class EconomicsPanel(QWidget):
    """On-design cost model + lifetime degradation + NPV-LCOE."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._project = None
        self._worker = None
        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QHBoxLayout(self)

        ctrl = QGroupBox(tr("otec_studio.eco_group"))
        ctrl.setMaximumWidth(320)
        form = QFormLayout(ctrl)

        self._t_ww = self._spin(0, 40, 26.0, " °C")
        self._t_cw = self._spin(0, 40, 5.0, " °C")
        self._dist = self._spin(0, 1000, 20.0, " km")
        form.addRow(tr("otec_studio.warm_water_t"), self._t_ww)
        form.addRow(tr("otec_studio.cold_water_t"), self._t_cw)
        form.addRow(tr("otec_studio.distance_to_shore"), self._dist)

        self._combo_cost = QComboBox()
        for key in ("low_cost", "high_cost"):
            self._combo_cost.addItem(tr(f"otec_studio.eco_cost_{key}"), key)
        form.addRow(tr("otec_studio.eco_cost_level"), self._combo_cost)

        self._combo_deg = QComboBox()
        for key in ("constant", "logistic"):
            self._combo_deg.addItem(tr(f"otec_studio.eco_deg_{key}"), key)
        self._combo_deg.currentIndexChanged.connect(self._on_deg_changed)
        form.addRow(tr("otec_studio.eco_degradation"), self._combo_deg)

        self._rate = self._spin(0, 0.1, 0.005, "", decimals=4, step=0.001)
        form.addRow(tr("otec_studio.eco_annual_rate"), self._rate)
        self._log_L = self._spin(0, 1, 0.30, "", decimals=2, step=0.05)
        self._log_k = self._spin(0, 2, 0.30, "", decimals=2, step=0.05)
        self._log_t0 = self._spin(0, 40, 15.0, " yr", decimals=0, step=1.0)
        self._row_L = form.addRow(
            tr("otec_studio.eco_logistic_param", param="L"), self._log_L) or self._log_L
        form.addRow(tr("otec_studio.eco_logistic_param", param="k"), self._log_k)
        form.addRow(tr("otec_studio.eco_logistic_param", param="t₀"), self._log_t0)

        self._btn = QPushButton(tr("otec_studio.eco_analyze"))
        self._btn.setStyleSheet(
            "background-color: #2980b9; color: white; font-weight: bold; padding: 8px;"
        )
        self._btn.clicked.connect(self._run)
        form.addRow(self._btn)
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setVisible(False)
        form.addRow(self._progress)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        form.addRow(self._status)

        root.addWidget(ctrl)
        self._on_deg_changed()

        # ── Right: results ──
        self._tabs = QTabWidget()

        summ = QWidget()
        sl = QVBoxLayout(summ)
        sl.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._lbl_summary = QLabel(tr("otec_studio.eco_run_hint"))
        self._lbl_summary.setTextFormat(Qt.TextFormat.RichText)
        sl.addWidget(self._lbl_summary)
        self._tabs.addTab(summ, tr("otec_studio.eco_summary"))

        self._fig_capex = Figure(figsize=(5, 4), dpi=100, layout="constrained")
        self._canvas_capex = FigureCanvasQTAgg(self._fig_capex)
        self._ax_capex = self._fig_capex.add_subplot(111)
        self._tabs.addTab(self._canvas_capex, tr("otec_studio.eco_capex_breakdown"))

        self._fig_deg = Figure(figsize=(5, 4), dpi=100, layout="constrained")
        self._canvas_deg = FigureCanvasQTAgg(self._fig_deg)
        self._ax_deg = self._fig_deg.add_subplot(111)
        self._tabs.addTab(self._canvas_deg, tr("otec_studio.eco_power_over_life"))

        root.addWidget(self._tabs, 1)

    @staticmethod
    def _spin(lo, hi, val, suffix="", decimals=1, step=0.5):
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(decimals)
        s.setSingleStep(step)
        s.setValue(val)
        if suffix:
            s.setSuffix(suffix)
        return s

    def _on_deg_changed(self, *_):
        is_log = self._combo_deg.currentData() == "logistic"
        for w in (self._log_L, self._log_k, self._log_t0):
            w.setEnabled(is_log)

    # ------------------------------------------------------------------
    # Scenario sync
    # ------------------------------------------------------------------

    def on_scenario_changed(self, scenario, project):
        self._project = project
        cfg = scenario.config
        idx = self._combo_cost.findData(cfg.cost_level)
        if idx >= 0:
            self._combo_cost.setCurrentIndex(idx)
        # Prefer the optimized SiteContext, else the project's shared resource
        # (set by the Site & Resource panel).
        site = getattr(scenario, "site", None)
        res = getattr(project, "resource", None)
        if site is not None:
            self._t_ww.setValue(getattr(site, "T_WW_in", self._t_ww.value()))
            self._t_cw.setValue(getattr(site, "T_CW_in", self._t_cw.value()))
            self._dist.setValue(getattr(site, "dist_shore", self._dist.value()))
        elif res is not None and res.has_design_point:
            self._t_ww.setValue(res.t_ww)
            self._t_cw.setValue(res.t_cw)

    def _config(self) -> StudioConfig:
        if self._project is not None:
            cfg = self._project.active.config
            from dataclasses import replace
            return replace(cfg, cost_level=self._combo_cost.currentData())
        return StudioConfig(cost_level=self._combo_cost.currentData())

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def _run(self):
        model = self._combo_deg.currentData()
        deg_kw = {}
        if model == "logistic":
            deg_kw = {
                "logistic_L": self._log_L.value(),
                "logistic_k": self._log_k.value(),
                "logistic_t0": self._log_t0.value(),
            }
        self._btn.setEnabled(False)
        self._progress.setVisible(True)
        self._status.setText(tr("otec_studio.eco_running"))
        self._worker = EconomicsWorker(
            self._config(), self._t_ww.value(), self._t_cw.value(),
            self._dist.value(), model, self._rate.value(), deg_kw,
        )
        self._worker.finished.connect(self._on_done)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_done(self, out):
        self._progress.setVisible(False)
        self._btn.setEnabled(True)
        self._status.setText(tr("otec_studio.done"))
        self._status.setStyleSheet("color: #2ecc71;")

        premium = (out["lcoe_npv"] - out["lcoe_nominal"])
        pct = 100 * premium / out["lcoe_nominal"] if out["lcoe_nominal"] else 0
        rows = [
            ("eco_nominal_lcoe", f"{out['lcoe_nominal']:.3f}"),
            ("eco_npv_lcoe", f"{out['lcoe_npv']:.3f}"),
            ("eco_degradation_premium", f"+{premium:.3f} ({pct:.1f}%)"),
            ("eco_capex_total", f"${out['capex_total'] / 1e6:,.1f} M"),
            ("eco_opex_per_year", f"${(out['opex'] or 0) / 1e6:,.2f} M"),
            ("eco_net_power_nameplate",
             f"{abs(out['p_net_nom']) / 1000:,.1f} MW"),
            ("eco_lifetime",
             tr("otec_studio.eco_years", years=out["lifetime"])),
        ]
        self._lbl_summary.setText(
            "<table cellpadding=4>"
            + "".join(f"<tr><td><b>{tr('otec_studio.' + k)}</b></td>"
                      f"<td>{v}</td></tr>" for k, v in rows)
            + "</table>"
        )

        self._draw_capex(out["capex_components"])
        self._draw_degradation(out["p_net_by_year"])

        if self._project is not None:
            self._project.active.cost_breakdown = {
                "CAPEX_total": out["capex_total"],
                "LCOE": out["lcoe_npv"],
            }

    def _on_error(self, msg):
        self._progress.setVisible(False)
        self._btn.setEnabled(True)
        self._status.setText(tr("otec_studio.error_msg", error=msg))
        self._status.setStyleSheet("color: #e74c3c;")

    def _draw_capex(self, comps):
        ax = self._ax_capex
        ax.clear()
        items = sorted(comps.items(), key=lambda kv: kv[1], reverse=True)
        names = [k for k, _ in items]
        vals = [v / 1e6 for _, v in items]
        ax.barh(range(len(names)), vals, color="#2980b9", edgecolor="white")
        ax.set_yticks(range(len(names)))
        ax.set_yticklabels(names, fontsize=8)
        ax.invert_yaxis()
        ax.set_xlabel(tr("otec_studio.eco_axis_capex"))
        ax.set_title(tr("otec_studio.eco_capex_breakdown"), fontsize=11,
                     fontweight="bold")
        ax.grid(axis="x", alpha=0.3)
        self._canvas_capex.draw()

    def _draw_degradation(self, p_by_year):
        ax = self._ax_deg
        ax.clear()
        years = np.arange(len(p_by_year))
        ax.fill_between(years, p_by_year, alpha=0.2, color="#e67e22")
        ax.plot(years, p_by_year, "o-", color="#e67e22", ms=3, lw=1.5)
        ax.axhline(p_by_year[0], color="#555", lw=0.6, ls="--",
                   label=tr("otec_studio.eco_nameplate"))
        ax.set_xlabel(tr("otec_studio.axis_year"))
        ax.set_ylabel(tr("otec_studio.axis_net_power_mw"))
        ax.set_title(tr("otec_studio.eco_power_over_life_title"), fontsize=11,
                     fontweight="bold")
        ax.legend(fontsize=8)
        ax.grid(alpha=0.3)
        self._canvas_deg.draw()
