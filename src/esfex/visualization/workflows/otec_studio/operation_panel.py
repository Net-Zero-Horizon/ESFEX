# -*- coding: utf-8 -*-
"""OTEC Studio — Operation panel (M4).

Simulates time-series operation of a nominal plant against a seasonal seawater
temperature profile (per-site, not averaged like the wizard), and diagnoses why
net power drops below nameplate — attributing the deficit to a smaller ΔT lift
(gross power) versus higher seawater-pump parasitics.
"""

from __future__ import annotations

import logging

import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from esfex.visualization.i18n import tr
from esfex.visualization.workflows.otec_studio import operation as oper
from esfex.visualization.workflows.otec_studio.project import StudioConfig
from esfex.visualization.workflows.otec_studio.workers import OperationWorker

logger = logging.getLogger(__name__)


class OperationPanel(QWidget):
    """Per-site time-series operation + power-loss diagnostics."""

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

        ctrl = QGroupBox(tr("otec_studio.op_group"))
        ctrl.setMaximumWidth(330)
        form = QFormLayout(ctrl)

        self._t_ww_d = self._spin(0, 40, 26.0, " °C")
        self._t_cw_d = self._spin(0, 40, 5.0, " °C")
        self._dist = self._spin(0, 1000, 20.0, " km")
        form.addRow(tr("otec_studio.op_design_warm_t"), self._t_ww_d)
        form.addRow(tr("otec_studio.op_design_cold_t"), self._t_cw_d)
        form.addRow(tr("otec_studio.distance_to_shore"), self._dist)

        self._ww_mean = self._spin(0, 40, 26.0, " °C")
        self._ww_amp = self._spin(0, 10, 2.0, " K")
        self._cw_mean = self._spin(0, 40, 5.0, " °C")
        self._cw_amp = self._spin(0, 10, 0.5, " K")
        form.addRow(tr("otec_studio.op_warm_mean"), self._ww_mean)
        form.addRow(tr("otec_studio.op_warm_seasonal"), self._ww_amp)
        form.addRow(tr("otec_studio.op_cold_mean"), self._cw_mean)
        form.addRow(tr("otec_studio.op_cold_seasonal"), self._cw_amp)
        self._n_steps = QSpinBox()
        self._n_steps.setRange(4, 365)
        self._n_steps.setValue(12)
        form.addRow(tr("otec_studio.op_time_steps"), self._n_steps)

        self._btn = QPushButton(tr("otec_studio.op_run"))
        self._btn.setStyleSheet(
            "background-color: #2980b9; color: white; font-weight: bold; padding: 8px;"
        )
        self._btn.clicked.connect(self._run)
        form.addRow(self._btn)
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setVisible(False)
        form.addRow(self._progress)

        self._diag = QLabel("")
        self._diag.setWordWrap(True)
        self._diag.setTextFormat(Qt.TextFormat.RichText)
        form.addRow(self._diag)
        root.addWidget(ctrl)

        # ── Right: plots ──
        self._tabs = QTabWidget()
        self._axes = {}
        for key in ("power", "parasitics", "temps", "perf"):
            title = tr(f"otec_studio.op_tab_{key}")
            fig = Figure(figsize=(5, 4), dpi=100, layout="constrained")
            canvas = FigureCanvasQTAgg(fig)
            ax = fig.add_subplot(111)
            self._axes[key] = (fig, canvas, ax)
            self._tabs.addTab(canvas, title)
        # Ported from OTEC Analysis: CWP sizing/transmission and annual CF.
        self._tabs.addTab(self._build_pipe_tab(), tr("otec_studio.op_tab_pipe"))
        self._tabs.addTab(self._build_annual_cf_tab(),
                          tr("otec_studio.op_tab_annual_cf"))
        root.addWidget(self._tabs, 1)

    def _build_pipe_tab(self):
        """Cold-water-pipe sizing, pumping parasitics, and diameter sweep."""
        w = QWidget()
        lay = QVBoxLayout(w)
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel(tr("otec_studio.cw_depth")))
        self._pipe_depth = self._spin(100, 5000, 1000.0, " m")
        ctrl.addWidget(self._pipe_depth)
        ctrl.addWidget(QLabel(tr("otec_studio.op_diameter")))
        self._pipe_dia = self._spin(2, 25, 10.0, " m")
        ctrl.addWidget(self._pipe_dia)
        ctrl.addWidget(QLabel(tr("otec_studio.op_slope")))
        self._pipe_slope = self._spin(1, 45, 7.0, "°")
        ctrl.addWidget(self._pipe_slope)
        b = QPushButton(tr("otec_studio.op_analyze_pipe"))
        b.clicked.connect(self._run_pipe)
        ctrl.addWidget(b)
        ctrl.addStretch()
        lay.addLayout(ctrl)
        self._pipe_info = QLabel("")
        self._pipe_info.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self._pipe_info)
        fig = Figure(figsize=(5, 3.4), dpi=100, layout="constrained")
        self._pipe_canvas = FigureCanvasQTAgg(fig)
        self._pipe_ax = fig.add_subplot(111)
        lay.addWidget(self._pipe_canvas, 1)
        return w

    def _build_annual_cf_tab(self):
        """8760-hour capacity-factor profile from the design ΔT, with export."""
        w = QWidget()
        lay = QVBoxLayout(w)
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel(tr("otec_studio.op_nominal_cf")))
        self._cf_nom = self._spin(0.1, 1.0, 0.914, "")
        self._cf_nom.setDecimals(3)
        ctrl.addWidget(self._cf_nom)
        ctrl.addWidget(QLabel(tr("otec_studio.op_design_dt")))
        self._cf_dt = self._spin(1, 30, 21.0, " K")
        ctrl.addWidget(self._cf_dt)
        b = QPushButton(tr("otec_studio.op_generate"))
        b.clicked.connect(self._run_annual_cf)
        ctrl.addWidget(b)
        self._cf_export = QPushButton(tr("otec_studio.export_csv"))
        self._cf_export.setEnabled(False)
        self._cf_export.clicked.connect(self._export_cf)
        ctrl.addWidget(self._cf_export)
        ctrl.addStretch()
        lay.addLayout(ctrl)
        self._cf_info = QLabel("")
        lay.addWidget(self._cf_info)
        fig = Figure(figsize=(5, 3.4), dpi=100, layout="constrained")
        self._cf_canvas = FigureCanvasQTAgg(fig)
        self._cf_ax = fig.add_subplot(111)
        lay.addWidget(self._cf_canvas, 1)
        self._annual_cf = None
        return w

    @staticmethod
    def _spin(lo, hi, val, suffix=""):
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(1)
        s.setSingleStep(0.5)
        s.setValue(val)
        if suffix:
            s.setSuffix(suffix)
        return s

    # ------------------------------------------------------------------
    # Scenario sync
    # ------------------------------------------------------------------

    def on_scenario_changed(self, scenario, project):
        self._project = project
        site = getattr(scenario, "site", None)
        res = getattr(project, "resource", None)
        if site is not None:
            self._t_ww_d.setValue(getattr(site, "T_WW_in", self._t_ww_d.value()))
            self._t_cw_d.setValue(getattr(site, "T_CW_in", self._t_cw_d.value()))
            self._dist.setValue(getattr(site, "dist_shore", self._dist.value()))
            self._ww_mean.setValue(getattr(site, "T_WW_in", self._ww_mean.value()))
            self._cw_mean.setValue(getattr(site, "T_CW_in", self._cw_mean.value()))
        elif res is not None and res.has_design_point:
            self._t_ww_d.setValue(res.t_ww)
            self._t_cw_d.setValue(res.t_cw)
            self._ww_mean.setValue(res.t_ww)
            self._cw_mean.setValue(res.t_cw)

    def _config(self) -> StudioConfig:
        if self._project is not None:
            return self._project.active.config
        return StudioConfig()

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def _run(self):
        n = self._n_steps.value()
        ww = oper.seasonal_profile(self._ww_mean.value(), self._ww_amp.value(), n)
        cw = oper.seasonal_profile(self._cw_mean.value(), self._cw_amp.value(), n)
        self._btn.setEnabled(False)
        self._progress.setVisible(True)
        self._diag.setText(tr("otec_studio.op_simulating"))
        self._worker = OperationWorker(
            self._config(), self._t_ww_d.value(), self._t_cw_d.value(),
            self._dist.value(), ww, cw,
        )
        self._worker.finished.connect(self._on_done)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_done(self, out):
        self._progress.setVisible(False)
        self._btn.setEnabled(True)
        d = out["diagnosis"]
        ww = np.asarray(out["ww"])
        cw = np.asarray(out["cw"])
        t = np.arange(len(d["p_net_mw"]))

        dominant = {
            "ΔT / gross power": tr("otec_studio.op_driver_gross"),
            "parasitic (pumping)": tr("otec_studio.op_driver_parasitic"),
        }.get(d["dominant"], d["dominant"])
        self._diag.setText(tr(
            "otec_studio.op_diagnosis",
            cf=f"{d['cf'] * 100:.1f}",
            p_min=f"{d['p_net_min_mw']:.1f}", p_max=f"{d['p_net_max_mw']:.1f}",
            driver=dominant,
            gross=f"{d['loss_gross_frac'] * 100:.0f}",
            parasitic=f"{d['loss_parasitic_frac'] * 100:.0f}",
        ))

        # Power
        _f, c, ax = self._axes["power"]
        ax.clear()
        ax.plot(t, d["p_gross_mw"], "o-", color="#7f8c8d", ms=3,
                label=tr("otec_studio.op_legend_gross"))
        ax.plot(t, d["p_net_mw"], "o-", color="#2980b9", ms=3,
                label=tr("otec_studio.op_legend_net"))
        ax.set_xlabel(tr("otec_studio.op_axis_time_step"))
        ax.set_ylabel(tr("otec_studio.op_axis_power"))
        ax.set_title(tr("otec_studio.op_title_power"), fontsize=11,
                     fontweight="bold")
        ax.legend(fontsize=8); ax.grid(alpha=0.3); c.draw()

        # Parasitics (stacked)
        _f, c, ax = self._axes["parasitics"]
        ax.clear()
        ax.stackplot(
            t, d["pump_ww_mw"], d["pump_cw_mw"], d["pump_nh3_mw"],
            labels=[tr("otec_studio.op_legend_ww_pump"),
                    tr("otec_studio.op_legend_cw_pump"),
                    tr("otec_studio.op_legend_nh3_pump")],
            colors=["#e74c3c", "#3498db", "#9b59b6"],
        )
        ax.set_xlabel(tr("otec_studio.op_axis_time_step"))
        ax.set_ylabel(tr("otec_studio.op_axis_parasitic"))
        ax.set_title(tr("otec_studio.op_title_parasitics"), fontsize=11,
                     fontweight="bold")
        ax.legend(fontsize=8, loc="upper right"); ax.grid(alpha=0.3); c.draw()

        # Temperatures
        _f, c, ax = self._axes["temps"]
        ax.clear()
        ax.plot(t, ww, "o-", color="#e67e22", ms=3,
                label=tr("otec_studio.op_legend_warm_sw"))
        ax.plot(t, cw, "o-", color="#3498db", ms=3,
                label=tr("otec_studio.op_legend_cold_sw"))
        ax.plot(t, d["t_evap"], "--", color="#e74c3c",
                label=tr("otec_studio.op_legend_t_evap"))
        ax.plot(t, d["t_cond"], "--", color="#2980b9",
                label=tr("otec_studio.op_legend_t_cond"))
        ax.set_xlabel(tr("otec_studio.op_axis_time_step"))
        ax.set_ylabel(tr("otec_studio.axis_temperature_c"))
        ax.set_title(tr("otec_studio.op_title_temps"), fontsize=11,
                     fontweight="bold")
        ax.legend(fontsize=8); ax.grid(alpha=0.3); c.draw()

        # Performance
        _f, c, ax = self._axes["perf"]
        ax.clear()
        ax.plot(t, d["eff_net"] * 100, "o-", color="#16a085", ms=3,
                label=tr("otec_studio.op_legend_eff_net"))
        ax2 = ax.twinx()
        ax2.plot(t, d["lcoe"], "s-", color="#c0392b", ms=3, label="LCOE")
        ax.set_xlabel(tr("otec_studio.op_axis_time_step"))
        ax.set_ylabel(tr("otec_studio.op_axis_eff_net"))
        ax2.set_ylabel("LCOE")
        ax.set_title(tr("otec_studio.op_title_perf"), fontsize=11,
                     fontweight="bold")
        ax.grid(alpha=0.3); c.draw()

    def _on_error(self, msg):
        self._progress.setVisible(False)
        self._btn.setEnabled(True)
        self._diag.setText("<span style='color:#e74c3c;'>"
                           + tr("otec_studio.error_msg", error=msg) + "</span>")

    # ------------------------------------------------------------------
    # Pipe & CWP (ported from OTEC Analysis)
    # ------------------------------------------------------------------

    def _gross_power_kw(self):
        """Gross power magnitude from the active design, else the config."""
        if self._project is not None:
            d = self._project.active.design
            if d is not None and hasattr(d, "x"):
                return abs(d.x.p_gross)
            return abs(self._project.active.config.gross_power)
        return 136000.0

    def _run_pipe(self):
        from esfex.visualization.workflows.otec_studio import engineering as eng
        depth = self._pipe_depth.value()
        dist = self._dist.value()
        gp = self._gross_power_kw()
        pa = eng.pipe_analysis(depth, dist, gp, self._pipe_dia.value(),
                               self._pipe_slope.value())
        sw = eng.pipe_diameter_sweep(depth, dist, gp)
        color = "#e74c3c" if pa.pumping_fraction > 0.3 else "#2ecc71"
        self._pipe_info.setText(tr(
            "otec_studio.op_pipe_info",
            length=f"{pa.pipe_length_m:,.0f}",
            pumping=f"{pa.pumping_power_kw:,.0f}",
            color=color,
            parasitic=f"{pa.pumping_fraction * 100:.0f}",
            net=f"{pa.net_power_after_pumping_kw / 1000:,.1f}",
            eff_trans=f"{pa.eff_trans:.3f}",
            best_diameter=f"{sw['best_diameter']:.1f}",
            best_net=f"{sw['best_net_kw'] / 1000:,.1f}",
        ))
        ax = self._pipe_ax
        ax.clear()
        ax.plot(sw["diameters"], np.array(sw["net_kw"]) / 1000, "o-",
                color="#2980b9", ms=3)
        ax.axvline(sw["best_diameter"], color="#e74c3c", ls="--", lw=1.2,
                   label=tr("otec_studio.op_legend_optimum_d",
                            diameter=f"{sw['best_diameter']:.1f}"))
        ax.axvline(self._pipe_dia.value(), color="#888", ls=":", lw=1,
                   label=tr("otec_studio.op_legend_current_d",
                            diameter=f"{self._pipe_dia.value():.1f}"))
        ax.set_xlabel(tr("otec_studio.op_axis_cwp_diameter"))
        ax.set_ylabel(tr("otec_studio.op_axis_net_delivered"))
        ax.set_title(tr("otec_studio.op_title_pipe"), fontsize=11,
                     fontweight="bold")
        ax.legend(fontsize=8); ax.grid(alpha=0.3)
        self._pipe_canvas.draw()

    # ------------------------------------------------------------------
    # Annual 8760-h CF profile (ported from OTEC Analysis)
    # ------------------------------------------------------------------

    def _run_annual_cf(self):
        from esfex.visualization.workflows.otec_studio import engineering as eng
        daily = eng.synthetic_daily(self._t_ww_d.value(), self._t_cw_d.value(),
                                    self._ww_amp.value(), self._cw_amp.value())
        out = eng.annual_cf_profile(daily, self._cf_nom.value(), self._cf_dt.value())
        self._annual_cf = out["hourly_cf"]
        self._cf_export.setEnabled(True)
        self._cf_info.setText(tr("otec_studio.op_cf_info",
                                 cf=f"{out['annual_mean_cf']:.3f}"))
        ax = self._cf_ax
        ax.clear()
        ax.plot(np.arange(8760), out["hourly_cf"], color="#16a085", lw=0.4)
        ax.fill_between(np.arange(8760), out["hourly_cf"], alpha=0.2,
                        color="#16a085")
        ax.set_xlabel(tr("otec_studio.op_axis_hour_of_year"))
        ax.set_ylabel(tr("otec_studio.op_axis_capacity_factor"))
        ax.set_title(tr("otec_studio.op_title_annual_cf"), fontsize=11,
                     fontweight="bold")
        ax.grid(alpha=0.3)
        self._cf_canvas.draw()

    def _export_cf(self):
        if self._annual_cf is None:
            return
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        path, _ = QFileDialog.getSaveFileName(
            self, tr("otec_studio.op_export_cf_title"), "otec_cf_8760.csv",
            tr("otec_studio.csv_filter"))
        if not path:
            return
        try:
            np.savetxt(path, self._annual_cf, fmt="%.5f", delimiter=",",
                       header="capacity_factor", comments="")
            QMessageBox.information(self, tr("otec_studio.export_title"),
                                    tr("otec_studio.saved_to", path=path))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, tr("otec_studio.export_failed"), str(exc))
