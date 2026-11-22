# -*- coding: utf-8 -*-
"""OTEC Studio — Cycle & Design panel (M2).

Pick a thermodynamic cycle, working fluid, and (for Kalina/Uehara) the ammonia
concentration, set the operating point (T_evap / T_cond), and see the live
T-s / P-h diagram plus the full state table. The composition knob updates the
diagram and states in real time. "Apply to scenario" pushes the cycle/fluid/
composition into the active scenario config used by the Optimization panel.
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
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from esfex.visualization.i18n import tr
from esfex.visualization.workflows.otec_studio import cycles as cyc
from esfex.visualization.workflows.otec_studio.project import StudioConfig
from esfex.visualization.ui_scale import scale_qss_fonts

logger = logging.getLogger(__name__)

# Cycle / fluid ids; their labels are ``otec_studio.cycle_<id>`` and
# ``otec_studio.fluid_<id>`` (also used by the scenario comparison table).
_CYCLES = ["rankine_closed", "rankine_open", "rankine_hybrid", "kalina", "uehara"]
_FLUIDS = ["ammonia", "R134a", "R245fa", "propane", "isobutane"]


class CyclePanel(QWidget):
    """Interactive thermodynamic cycle explorer."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._project = None
        self._build_ui()
        self._recompute()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QHBoxLayout(self)

        # ── Left: controls ──
        ctrl = QGroupBox(tr("otec_studio.cycle_group"))
        form = QFormLayout(ctrl)
        ctrl.setMaximumWidth(320)

        self._combo_cycle = QComboBox()
        for key in _CYCLES:
            self._combo_cycle.addItem(tr(f"otec_studio.cycle_{key}"), key)
        self._combo_cycle.currentIndexChanged.connect(self._on_cycle_changed)
        form.addRow(tr("otec_studio.cycle_label"), self._combo_cycle)

        self._combo_fluid = QComboBox()
        for key in _FLUIDS:
            self._combo_fluid.addItem(tr(f"otec_studio.fluid_{key}"), key)
        self._combo_fluid.currentIndexChanged.connect(self._recompute)
        form.addRow(tr("otec_studio.cycle_working_fluid"), self._combo_fluid)

        self._spin_x = QDoubleSpinBox()
        self._spin_x.setRange(0.50, 0.95)
        self._spin_x.setSingleStep(0.01)
        self._spin_x.setDecimals(2)
        self._spin_x.setValue(0.70)
        self._spin_x.valueChanged.connect(self._recompute)
        form.addRow(tr("otec_studio.cycle_nh3_concentration"), self._spin_x)

        self._spin_tevap = self._temp_spin(25.0)
        self._spin_tcond = self._temp_spin(8.0)
        form.addRow(tr("otec_studio.cycle_t_evaporator"), self._spin_tevap)
        form.addRow(tr("otec_studio.cycle_t_condenser"), self._spin_tcond)

        self._btn_apply = QPushButton(tr("otec_studio.cycle_apply"))
        self._btn_apply.setToolTip(tr("otec_studio.cycle_apply_tip"))
        self._btn_apply.clicked.connect(self._apply_to_scenario)
        form.addRow(self._btn_apply)

        self._lbl_info = QLabel("")
        self._lbl_info.setWordWrap(True)
        self._lbl_info.setStyleSheet(scale_qss_fonts("color: #888; font-size: 10px;"))
        form.addRow(self._lbl_info)

        root.addWidget(ctrl)

        # ── Right: diagrams + state table ──
        self._tabs = QTabWidget()

        self._fig_ts = Figure(figsize=(5, 4), dpi=100, layout="constrained")
        self._canvas_ts = FigureCanvasQTAgg(self._fig_ts)
        self._ax_ts = self._fig_ts.add_subplot(111)
        self._tabs.addTab(self._canvas_ts, tr("otec_studio.cycle_ts_diagram"))

        self._fig_ph = Figure(figsize=(5, 4), dpi=100, layout="constrained")
        self._canvas_ph = FigureCanvasQTAgg(self._fig_ph)
        self._ax_ph = self._fig_ph.add_subplot(111)
        self._tabs.addTab(self._canvas_ph, tr("otec_studio.cycle_ph_diagram"))

        self._table = QTableWidget()
        self._table.setColumnCount(2)
        self._table.setHorizontalHeaderLabels(
            [tr("otec_studio.cycle_col_state"), tr("otec_studio.cycle_col_value")])
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._tabs.addTab(self._table, tr("otec_studio.cycle_state_table"))

        root.addWidget(self._tabs, 1)

    @staticmethod
    def _temp_spin(val):
        s = QDoubleSpinBox()
        s.setRange(0, 40)
        s.setDecimals(1)
        s.setSingleStep(0.5)
        s.setSuffix(" °C")
        s.setValue(val)
        return s

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def _current_config(self) -> StudioConfig:
        return StudioConfig(
            cycle_type=self._combo_cycle.currentData(),
            fluid_type=self._combo_fluid.currentData(),
            ammonia_concentration=self._spin_x.value(),
        )

    def on_scenario_changed(self, scenario, project):
        self._project = project
        cfg = scenario.config
        # reflect the scenario's cycle config into the controls (no recompute storm)
        for w in (self._combo_cycle, self._combo_fluid, self._spin_x):
            w.blockSignals(True)
        idx = self._combo_cycle.findData(cfg.cycle_type)
        if idx >= 0:
            self._combo_cycle.setCurrentIndex(idx)
        fidx = self._combo_fluid.findData(cfg.fluid_type)
        if fidx >= 0:
            self._combo_fluid.setCurrentIndex(fidx)
        self._spin_x.setValue(cfg.ammonia_concentration)
        for w in (self._combo_cycle, self._combo_fluid, self._spin_x):
            w.blockSignals(False)
        self._on_cycle_changed()

    def _on_cycle_changed(self, *_):
        is_mix = self._combo_cycle.currentData() in cyc.MIXTURE_CYCLES
        self._spin_x.setEnabled(is_mix)
        # mixture cycles build their own fluid; the fluid selector is for
        # closed/hybrid only
        self._combo_fluid.setEnabled(
            self._combo_cycle.currentData() not in cyc.MIXTURE_CYCLES
        )
        self._recompute()

    def _apply_to_scenario(self):
        if self._project is None:
            return
        self._project.update_active_config(
            cycle_type=self._combo_cycle.currentData(),
            fluid_type=self._combo_fluid.currentData(),
            ammonia_concentration=self._spin_x.value(),
        )
        self._lbl_info.setText(tr("otec_studio.cycle_applied"))

    # ------------------------------------------------------------------
    # Compute + draw
    # ------------------------------------------------------------------

    def _recompute(self, *_):
        cfg = self._current_config()
        t_evap = self._spin_tevap.value()
        t_cond = self._spin_tcond.value()
        if t_evap <= t_cond:
            self._lbl_info.setText(tr("otec_studio.cycle_t_order_error"))
            return
        try:
            out = cyc.compute_states(cfg, t_evap, t_cond)
        except Exception as exc:  # noqa: BLE001
            self._lbl_info.setText(tr("otec_studio.cycle_error", error=exc))
            return

        mf = out["mass_flow"]
        if isinstance(mf, dict):
            mf_txt = ", ".join(f"{k}={float(v):,.0f}" for k, v in mf.items())
        elif isinstance(mf, (int, float)):
            mf_txt = f"{mf:,.1f} kg/s"
        else:
            mf_txt = tr("otec_studio.not_available")
        # Carnot ceiling for this operating point — the theoretical max any
        # cycle could reach between these reservoirs (OTEC is a few %).
        from esfex.visualization.workflows.otec_studio import engineering as eng
        carnot = eng.carnot_efficiency(t_evap, t_cond)
        self._lbl_info.setText(tr(
            "otec_studio.cycle_info", mass_flow=mf_txt,
            carnot=f"{carnot * 100:.2f}"))

        self._draw_diagrams(cfg, out, t_evap, t_cond)
        self._fill_table(out["states"])

    def _draw_diagrams(self, cfg, out, t_evap, t_cond):
        fluid = out["fluid"]
        states = out["states"]
        dome = cyc.saturation_dome(fluid, max(0.5, t_cond - 3), t_evap + 5, n=60)
        is_loop = cfg.cycle_type in cyc.LOOP_CYCLES

        # ── T-s ──
        ax = self._ax_ts
        ax.clear()
        ax.plot(dome["s_liq"], dome["T"], color="#2980b9", lw=1)
        ax.plot(dome["s_vap"], dome["T"], color="#2980b9", lw=1,
                label=tr("otec_studio.cycle_saturation_dome"))
        if is_loop:
            s, T = cyc.closed_loop_ts(states, t_evap, t_cond, fluid)
            ax.plot(s, T, "o-", color="#e74c3c", ms=3, lw=1.5,
                    label=tr("otec_studio.cycle_legend_cycle"))
            for i, key in enumerate(("1", "2", "3", "4"), start=0):
                sp = states[f"s_{key}"]
                tp = t_evap if key == "3" else t_cond
                ax.annotate(key, (sp, tp), fontsize=9, fontweight="bold",
                            color="#c0392b")
        else:
            ax.text(0.5, 0.06, tr("otec_studio.cycle_states_in_table",
                                  cycle=self._combo_cycle.currentText()),
                    transform=ax.transAxes, ha="center", fontsize=8, color="#888")
        ax.set_xlabel(tr("otec_studio.cycle_axis_entropy"))
        ax.set_ylabel(tr("otec_studio.cycle_axis_temperature"))
        ax.set_title(tr("otec_studio.cycle_ts_diagram"), fontsize=11,
                     fontweight="bold")
        ax.legend(fontsize=8)
        ax.grid(alpha=0.3)
        self._canvas_ts.draw()

        # ── P-h ──
        ax2 = self._ax_ph
        ax2.clear()
        ax2.plot(dome["h_liq"], dome["p"], color="#2980b9", lw=1)
        ax2.plot(dome["h_vap"], dome["p"], color="#2980b9", lw=1,
                 label=tr("otec_studio.cycle_saturation_dome"))
        if is_loop:
            h, p = cyc.closed_loop_ph(states, out["p_evap"], out["p_cond"])
            ax2.plot(h, p, "o-", color="#e74c3c", ms=3, lw=1.5,
                     label=tr("otec_studio.cycle_legend_cycle"))
        else:
            ax2.text(0.5, 0.06, tr("otec_studio.cycle_states_in_table",
                                   cycle=self._combo_cycle.currentText()),
                     transform=ax2.transAxes, ha="center", fontsize=8, color="#888")
        ax2.set_yscale("log")
        ax2.set_xlabel(tr("otec_studio.cycle_axis_enthalpy"))
        ax2.set_ylabel(tr("otec_studio.cycle_axis_pressure"))
        ax2.set_title(tr("otec_studio.cycle_ph_diagram"), fontsize=11,
                      fontweight="bold")
        ax2.legend(fontsize=8)
        ax2.grid(alpha=0.3, which="both")
        self._canvas_ph.draw()

    def _fill_table(self, states):
        rows = cyc.format_states(states)
        self._table.setRowCount(len(rows))
        for r, (k, v) in enumerate(rows):
            self._table.setItem(r, 0, QTableWidgetItem(k))
            self._table.setItem(r, 1, QTableWidgetItem(v))
