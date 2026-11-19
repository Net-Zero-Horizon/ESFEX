# -*- coding: utf-8 -*-
"""OTEC Studio — Uncertainty & Sensitivity panel (M5).

Monte Carlo, Tornado and Sobol studies over a *selectable* output metric (not
fixed to LCOE) with *editable* parameter distributions — the wizard hardcodes
both. A shared, editable parameter table feeds all three sub-analyses; results
are stored on the active scenario.
"""

from __future__ import annotations

import logging

import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from esfex.visualization.i18n import tr
from esfex.visualization.workflows.otec_studio import uq
from esfex.visualization.workflows.otec_studio.project import StudioConfig
from esfex.visualization.workflows.otec_studio.workers import UQWorker
from esfex.visualization.ui_scale import scale_qss_fonts

logger = logging.getLogger(__name__)


def _metric_label(metric: str) -> str:
    """Translated label of an output metric (lcoe, net_power, capex, opex)."""
    key = f"otec_studio.uq_metric_{metric}"
    text = tr(key)
    return metric if text == key else text


def _dist_label(dist: str) -> str:
    """Translated name of a distribution (normal, uniform, ...)."""
    key = f"otec_studio.uq_dist_{dist}"
    text = tr(key)
    return dist if text == key else text


def _param_label(name: str) -> str:
    """Translated label of an OTEX uncertain parameter (raw name if unknown)."""
    key = f"otec_studio.uq_param_{name}"
    text = tr(key)
    return name if text == key else text


def _fill_metric_combo(combo: QComboBox, metrics) -> None:
    for m in metrics:
        combo.addItem(_metric_label(m), m)


class UncertaintyPanel(QWidget):
    """Monte Carlo / Tornado / Sobol with selectable output + editable params."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._project = None
        self._params = uq.default_parameters()
        self._workers = {}
        self._build_ui()
        self._fill_param_table()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)

        site = QHBoxLayout()
        site.addWidget(QLabel(tr("otec_studio.uq_warm_t")))
        self._t_ww = self._spin(0, 40, 26.0, " °C")
        site.addWidget(self._t_ww)
        site.addWidget(QLabel(tr("otec_studio.uq_cold_t")))
        self._t_cw = self._spin(0, 40, 5.0, " °C")
        site.addWidget(self._t_cw)
        site.addStretch()
        root.addLayout(site)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter, 1)

        # ── Left: editable parameter distributions ──
        pgrp = QGroupBox(tr("otec_studio.uq_params_group"))
        pl = QVBoxLayout(pgrp)
        self._table = QTableWidget()
        self._table.setColumnCount(4)
        self._table.setHorizontalHeaderLabels(
            [tr("otec_studio.uq_col_parameter"), tr("otec_studio.uq_col_dist"),
             "P1", "P2"])
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        pl.addWidget(self._table)
        hint = QLabel(tr("otec_studio.uq_params_hint"))
        hint.setStyleSheet(scale_qss_fonts("color: #888; font-size: 10px;"))
        pl.addWidget(hint)
        splitter.addWidget(pgrp)

        # ── Right: analysis sub-tabs ──
        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_mc_tab(), tr("otec_studio.uq_monte_carlo"))
        self._tabs.addTab(self._build_tornado_tab(), tr("otec_studio.uq_tornado"))
        self._tabs.addTab(self._build_sobol_tab(), tr("otec_studio.uq_sobol"))
        splitter.addWidget(self._tabs)
        splitter.setSizes([380, 640])

    def _build_mc_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel(tr("otec_studio.uq_samples")))
        self._mc_n = QSpinBox(); self._mc_n.setRange(50, 5000); self._mc_n.setValue(500)
        self._mc_n.setSingleStep(50)
        ctrl.addWidget(self._mc_n)
        ctrl.addWidget(QLabel(tr("otec_studio.uq_seed")))
        self._mc_seed = QSpinBox(); self._mc_seed.setRange(0, 99999); self._mc_seed.setValue(42)
        ctrl.addWidget(self._mc_seed)
        ctrl.addWidget(QLabel(tr("otec_studio.uq_metric")))
        self._mc_metric = QComboBox()
        _fill_metric_combo(self._mc_metric, uq.MC_METRICS)
        ctrl.addWidget(self._mc_metric)
        self._mc_btn = QPushButton(tr("otec_studio.uq_run_mc"))
        self._mc_btn.clicked.connect(lambda: self._run("mc"))
        ctrl.addWidget(self._mc_btn)
        ctrl.addStretch()
        lay.addLayout(ctrl)
        self._mc_stats = QLabel("")
        lay.addWidget(self._mc_stats)
        self._mc_fig = Figure(figsize=(5, 3.5), dpi=100, layout="constrained")
        self._mc_canvas = FigureCanvasQTAgg(self._mc_fig)
        self._mc_ax = self._mc_fig.add_subplot(111)
        lay.addWidget(self._mc_canvas, 1)
        self._mc_prog = self._progress(lay)
        return w

    def _build_tornado_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel(tr("otec_studio.uq_variation")))
        self._tor_pct = QSpinBox(); self._tor_pct.setRange(1, 50); self._tor_pct.setValue(10)
        ctrl.addWidget(self._tor_pct)
        ctrl.addWidget(QLabel(tr("otec_studio.uq_output")))
        self._tor_out = QComboBox()
        _fill_metric_combo(self._tor_out, uq.SENS_OUTPUTS)
        ctrl.addWidget(self._tor_out)
        self._tor_btn = QPushButton(tr("otec_studio.uq_run_tornado"))
        self._tor_btn.clicked.connect(lambda: self._run("tornado"))
        ctrl.addWidget(self._tor_btn)
        ctrl.addStretch()
        lay.addLayout(ctrl)
        self._tor_fig = Figure(figsize=(5, 4), dpi=100, layout="constrained")
        self._tor_canvas = FigureCanvasQTAgg(self._tor_fig)
        self._tor_ax = self._tor_fig.add_subplot(111)
        lay.addWidget(self._tor_canvas, 1)
        self._tor_prog = self._progress(lay)
        return w

    def _build_sobol_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel(tr("otec_studio.uq_samples")))
        self._sob_n = QSpinBox(); self._sob_n.setRange(16, 2048); self._sob_n.setValue(256)
        self._sob_n.setSingleStep(16)
        ctrl.addWidget(self._sob_n)
        ctrl.addWidget(QLabel(tr("otec_studio.uq_output")))
        self._sob_out = QComboBox()
        _fill_metric_combo(self._sob_out, uq.SENS_OUTPUTS)
        ctrl.addWidget(self._sob_out)
        self._sob_btn = QPushButton(tr("otec_studio.uq_run_sobol"))
        self._sob_btn.clicked.connect(lambda: self._run("sobol"))
        ctrl.addWidget(self._sob_btn)
        ctrl.addStretch()
        lay.addLayout(ctrl)
        self._sob_fig = Figure(figsize=(5, 4), dpi=100, layout="constrained")
        self._sob_canvas = FigureCanvasQTAgg(self._sob_fig)
        self._sob_ax = self._sob_fig.add_subplot(111)
        lay.addWidget(self._sob_canvas, 1)
        self._sob_prog = self._progress(lay)
        return w

    @staticmethod
    def _spin(lo, hi, val, suffix=""):
        from PySide6.QtWidgets import QDoubleSpinBox
        s = QDoubleSpinBox()
        s.setRange(lo, hi); s.setDecimals(1); s.setSingleStep(0.5); s.setValue(val)
        if suffix:
            s.setSuffix(suffix)
        return s

    @staticmethod
    def _progress(lay):
        p = QProgressBar(); p.setRange(0, 0); p.setVisible(False)
        lay.addWidget(p)
        return p

    def _fill_param_table(self):
        self._table.setRowCount(len(self._params))
        for r, p in enumerate(self._params):
            name = QTableWidgetItem(_param_label(p["name"]))
            name.setToolTip(p["name"])
            name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
            dist = QTableWidgetItem(_dist_label(p["distribution"]))
            dist.setFlags(dist.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(r, 0, name)
            self._table.setItem(r, 1, dist)
            self._table.setItem(r, 2, QTableWidgetItem(f"{p['p1']:.4g}"))
            self._table.setItem(r, 3, QTableWidgetItem(f"{p['p2']:.4g}"))

    # ------------------------------------------------------------------
    # Scenario sync
    # ------------------------------------------------------------------

    def on_scenario_changed(self, scenario, project):
        self._project = project
        site = getattr(scenario, "site", None)
        res = getattr(project, "resource", None)
        if site is not None:
            self._t_ww.setValue(getattr(site, "T_WW_in", self._t_ww.value()))
            self._t_cw.setValue(getattr(site, "T_CW_in", self._t_cw.value()))
        elif res is not None and res.has_design_point:
            self._t_ww.setValue(res.t_ww)
            self._t_cw.setValue(res.t_cw)

    def _config(self) -> StudioConfig:
        return self._project.active.config if self._project else StudioConfig()

    def _read_params(self) -> list[dict]:
        """Pull edited P1/P2 back from the table."""
        params = []
        for r, p in enumerate(self._params):
            q = dict(p)
            try:
                q["p1"] = float(self._table.item(r, 2).text())
                q["p2"] = float(self._table.item(r, 3).text())
            except (ValueError, AttributeError):
                pass
            params.append(q)
        return params

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def _run(self, kind):
        btn, prog = {
            "mc": (self._mc_btn, self._mc_prog),
            "tornado": (self._tor_btn, self._tor_prog),
            "sobol": (self._sob_btn, self._sob_prog),
        }[kind]
        if kind == "mc":
            opts = {"n_samples": self._mc_n.value(), "seed": self._mc_seed.value()}
        elif kind == "tornado":
            opts = {"variation_pct": float(self._tor_pct.value()),
                    "output": self._tor_out.currentData()}
        else:
            opts = {"n_samples": self._sob_n.value(),
                    "output": self._sob_out.currentData()}
        btn.setEnabled(False)
        prog.setVisible(True)
        worker = UQWorker(
            kind, self._config(), self._t_ww.value(), self._t_cw.value(),
            self._read_params(), opts,
        )
        worker.finished.connect(self._on_done)
        worker.error.connect(lambda m, b=btn, p=prog: self._on_error(m, b, p))
        self._workers[kind] = worker
        worker.start()

    def _on_error(self, msg, btn, prog):
        prog.setVisible(False)
        btn.setEnabled(True)
        logger.error("UQ error: %s", msg)

    def _on_done(self, out):
        kind = out["kind"]
        if kind == "mc":
            self._mc_prog.setVisible(False); self._mc_btn.setEnabled(True)
            self._draw_mc(out)
            if self._project:
                self._project.active.uncertainty = out["stats"]
        elif kind == "tornado":
            self._tor_prog.setVisible(False); self._tor_btn.setEnabled(True)
            self._draw_tornado(out)
            if self._project:
                self._project.active.sensitivity["tornado"] = out
        else:
            self._sob_prog.setVisible(False); self._sob_btn.setEnabled(True)
            self._draw_sobol(out)
            if self._project:
                self._project.active.sensitivity["sobol"] = out

    # ------------------------------------------------------------------
    # Draw
    # ------------------------------------------------------------------

    def _draw_mc(self, out):
        metric = self._mc_metric.currentData()
        df = out["df"]
        if metric not in df.columns:
            return
        samples = np.asarray(df[metric], dtype=float)
        samples = samples[np.isfinite(samples)]
        st = out["stats"].get(metric, {})
        mean = st.get(f"{metric}_mean")
        p5 = st.get(f"{metric}_p5")
        p95 = st.get(f"{metric}_p95")
        ax = self._mc_ax
        ax.clear()
        ax.hist(samples, bins=min(40, len(samples) // 2 + 1),
                color="#2980b9", alpha=0.75, edgecolor="white")
        for val, c, lbl in [(mean, "#27ae60", tr("otec_studio.uq_mean")),
                            (p5, "#e74c3c", "p5"), (p95, "#e67e22", "p95")]:
            if val is not None:
                ax.axvline(val, color=c, lw=1.5, ls="--", label=f"{lbl}={val:.3g}")
        ax.set_xlabel(_metric_label(metric))
        ax.set_ylabel(tr("otec_studio.uq_frequency"))
        ax.set_title(tr("otec_studio.uq_mc_title", metric=_metric_label(metric)),
                     fontsize=11, fontweight="bold")
        ax.legend(fontsize=8); ax.grid(alpha=0.3)
        self._mc_canvas.draw()
        cv = st.get(f"{metric}_cv")
        self._mc_stats.setText(
            tr("otec_studio.uq_mc_stats", mean=f"{mean:.4g}",
               std=f"{st.get(f'{metric}_std', float('nan')):.4g}",
               cv=f"{cv:.3f}", p5=f"{p5:.3g}", p95=f"{p95:.3g}")
            if mean is not None else ""
        )

    def _draw_tornado(self, out):
        ranking = out["ranking"]
        ax = self._tor_ax
        ax.clear()
        names = [r[0] for r in ranking][:10][::-1]
        swings = [float(r[1]) for r in ranking][:10][::-1]
        ax.barh(range(len(names)), swings, color="#e74c3c", edgecolor="white")
        ax.set_yticks(range(len(names)))
        ax.set_yticklabels([_param_label(n) for n in names], fontsize=8)
        ax.set_xlabel(tr("otec_studio.uq_swing",
                         metric=_metric_label(out["output"])))
        ax.set_title(tr("otec_studio.uq_tornado_title",
                        metric=_metric_label(out["output"])),
                     fontsize=11, fontweight="bold")
        ax.grid(axis="x", alpha=0.3)
        self._tor_canvas.draw()

    def _draw_sobol(self, out):
        # S1 / ST are dicts keyed by parameter name; rank by total effect.
        s1 = out["S1"]; st = out["ST"]
        order = [r[0] for r in out["ranking"]][:10]
        names = [n for n in order if n in st]
        s1v = [float(s1.get(n, 0.0)) for n in names]
        stv = [float(st.get(n, 0.0)) for n in names]
        ax = self._sob_ax
        ax.clear()
        y = np.arange(len(names))
        ax.barh(y - 0.2, s1v, height=0.4, color="#2ecc71", label=tr("otec_studio.uq_s1"))
        ax.barh(y + 0.2, stv, height=0.4, color="#9b59b6", label=tr("otec_studio.uq_st"))
        ax.set_yticks(y)
        ax.set_yticklabels([_param_label(n) for n in names], fontsize=8)
        ax.invert_yaxis()
        ax.set_xlabel(tr("otec_studio.uq_sobol_index"))
        ax.set_title(tr("otec_studio.uq_sobol_title",
                        metric=_metric_label(out["output"])),
                     fontsize=11, fontweight="bold")
        ax.legend(fontsize=8); ax.grid(axis="x", alpha=0.3)
        self._sob_canvas.draw()
