# -*- coding: utf-8 -*-
"""
Step widgets for the Financial Analysis wizard.

Eight steps organized in two phases:
Phase A — Economic Overview (steps 1-4)
Phase B — Deep Financial Analysis (steps 5-8)
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import replace
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from esfex.models.financial_analysis import (
    FinancialAssumptions,
    MonteCarloResult,
    SensitivityResult,
    SystemFinancials,
    TechnologyFinancials,
    compute_system_financials,
    compute_technology_financials,
    run_monte_carlo,
    run_sensitivity_analysis,
)
from esfex.visualization.workflows.financial_charts import (
    BubbleChart,
    CostPieChart,
    CumulativeNPVChart,
    DSCRTimeline,
    IRRHistogram,
    NPVHistogram,
    SpiderPlot,
    StackedBarChart,
    TechRevenueVsCostChart,
    TornadoDiagram,
    WaterfallChart,
    PriceDurationCurve,
)
from esfex.visualization.i18n import tr
from esfex.visualization.ui_scale import scale_qss_fonts

logger = logging.getLogger(__name__)


# =====================================================================
# Background Workers
# =====================================================================


class _FinancialWorker(QThread):
    """Run compute_system_financials in background."""
    finished = Signal(object, object)  # (SystemFinancials, dict[str, TechnologyFinancials])
    error = Signal(str)

    def __init__(self, h5_path: str, assumptions: FinancialAssumptions):
        super().__init__()
        self._h5_path = h5_path
        self._assumptions = assumptions

    def run(self):
        try:
            sf = compute_system_financials(self._h5_path, self._assumptions)
            tf = compute_technology_financials(self._h5_path, self._assumptions)
            self.finished.emit(sf, tf)
        except Exception as e:
            self.error.emit(str(e))


class _SensitivityWorker(QThread):
    """Run sensitivity analysis in background."""
    finished = Signal(object)
    error = Signal(str)

    def __init__(self, h5_path, assumptions, variables, n_points):
        super().__init__()
        self._h5_path = h5_path
        self._assumptions = assumptions
        self._variables = variables
        self._n_points = n_points

    def run(self):
        try:
            result = run_sensitivity_analysis(
                self._h5_path, self._assumptions,
                variables=self._variables, n_points=self._n_points,
            )
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))


class _MonteCarloWorker(QThread):
    """Run Monte Carlo simulation in background."""
    finished = Signal(object)
    error = Signal(str)

    def __init__(self, h5_path, assumptions, n_samples):
        super().__init__()
        self._h5_path = h5_path
        self._assumptions = assumptions
        self._n_samples = n_samples

    def run(self):
        try:
            result = run_monte_carlo(
                self._h5_path, self._assumptions, n_samples=self._n_samples,
            )
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))


# =====================================================================
# Helper: Metric Card Widget
# =====================================================================


def _pct_or_na(value, decimals: int = 1) -> str:
    """Format a fraction as a percentage, or 'N/A' when it is NaN/None.

    IRR is NaN for operational runs with no investment outflow (undefined
    return) — show N/A rather than a misleading number.
    """
    import math
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return tr("wizard_financial.na")
    return f"{value:.{decimals}%}"


def _years_or_na(value: float, long: bool = False) -> str:
    """Format a payback period in years, or 'N/A' when it never pays back."""
    if value >= 100:
        return tr("wizard_financial.na")
    key = "wizard_financial.years_long" if long else "wizard_financial.years_short"
    return tr(key, value=f"{value:.1f}")


def _display_width(text: str) -> int:
    """Terminal-style width: East Asian wide/full-width characters count 2."""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in text)


def _pad(text: str, width: int) -> str:
    """Left-justify *text* to *width* display columns (CJK-aware)."""
    return text + " " * max(1, width - _display_width(text))


def _report_line(key: str, value: str) -> str:
    """One ``label: value`` row of the plain-text report, label translated."""
    return "  " + _pad(tr(f"wizard_financial.{key}"), 20) + value


# Cash-flow DataFrame column -> translation key (table headers only; the CSV
# export keeps the raw column names).
_CF_COLUMN_KEYS = {
    "year": "cf_year",
    "revenue": "cf_revenue",
    "fuel_cost": "cf_fuel_cost",
    "om_cost": "cf_om_cost",
    "insurance": "cf_insurance",
    "carbon_cost": "cf_carbon_cost",
    "penalties": "cf_penalties",
    "capex": "cf_capex",
    "depreciation": "cf_depreciation",
    "tax": "cf_tax",
    "ptc_benefit": "cf_ptc_benefit",
    "itc_benefit": "cf_itc_benefit",
    "debt_service": "cf_debt_service",
    "net_cash_flow": "cf_net_cash_flow",
    "equity_cash_flow": "cf_equity_cash_flow",
    "cumulative_npv": "cf_cumulative_npv",
    "dscr": "cf_dscr",
}

# Sensitivity variable (FinancialAssumptions field) -> translation key
_SENSITIVITY_VAR_KEYS = {
    "discount_rate": "var_discount_rate",
    "ppa_price": "var_ppa_price",
    "carbon_price": "var_carbon_price",
    "debt_fraction": "var_debt_fraction",
    "cost_of_debt": "var_cost_of_debt",
    "tax_rate": "var_tax_rate",
}


def _metric_card(label: str, value: str, color: str = "#2980b9") -> QGroupBox:
    """Create a compact metric display card."""
    box = QGroupBox()
    box.setStyleSheet(
        f"QGroupBox {{ border: 2px solid {color}; border-radius: 6px; padding: 8px; }}"
    )
    lay = QVBoxLayout(box)
    lay.setContentsMargins(8, 8, 8, 8)
    val_lbl = QLabel(value)
    val_lbl.setStyleSheet(scale_qss_fonts(f"font-size: 18px; font-weight: bold; color: {color};"))
    val_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lay.addWidget(val_lbl)
    name_lbl = QLabel(label)
    name_lbl.setStyleSheet(scale_qss_fonts("font-size: 10px; color: #888;"))
    name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lay.addWidget(name_lbl)
    return box


# =====================================================================
# Step 1: Load Results & Configure Assumptions
# =====================================================================


class LoadResultsStep(QWidget):
    """HDF5 file picker + financial assumptions form + analysis trigger."""

    analysisFinished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._h5_path: Optional[str] = None
        self._assumptions = FinancialAssumptions()
        self._system_financials: Optional[SystemFinancials] = None
        self._tech_financials: Optional[dict] = None
        self._worker: Optional[_FinancialWorker] = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # File picker
        file_row = QHBoxLayout()
        file_row.addWidget(QLabel(tr("wizard_financial.results_hdf5")))
        self._lbl_path = QLabel(tr("wizard_financial.no_file_selected"))
        self._lbl_path.setStyleSheet("color: #888;")
        file_row.addWidget(self._lbl_path, 1)
        btn_browse = QPushButton(tr("common.browse"))
        btn_browse.clicked.connect(self._browse)
        file_row.addWidget(btn_browse)
        layout.addLayout(file_row)

        # Scrollable 2-column assumptions form
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        form_widget = QWidget()
        columns = QHBoxLayout(form_widget)

        # ── Left column ──
        left = QVBoxLayout()

        cap_group = QGroupBox(tr("wizard_financial.grp_capital"))
        cap_form = QFormLayout(cap_group)
        self._spin_debt_frac = self._pct_spin(0.60)
        cap_form.addRow(tr("wizard_financial.debt_fraction"), self._spin_debt_frac)
        self._spin_cost_debt = self._pct_spin(0.05)
        cap_form.addRow(tr("wizard_financial.cost_of_debt"), self._spin_cost_debt)
        self._spin_cost_equity = self._pct_spin(0.12)
        cap_form.addRow(tr("wizard_financial.cost_of_equity"), self._spin_cost_equity)
        self._spin_debt_tenor = QSpinBox()
        self._spin_debt_tenor.setRange(1, 40)
        self._spin_debt_tenor.setValue(15)
        cap_form.addRow(tr("wizard_financial.debt_tenor"), self._spin_debt_tenor)
        left.addWidget(cap_group)

        tax_group = QGroupBox(tr("wizard_financial.grp_tax"))
        tax_form = QFormLayout(tax_group)
        self._spin_tax_rate = self._pct_spin(0.25)
        tax_form.addRow(tr("wizard_financial.tax_rate"), self._spin_tax_rate)
        self._combo_depreciation = QComboBox()
        self._combo_depreciation.addItem(
            tr("wizard_financial.dep_straight_line"), "straight_line")
        self._combo_depreciation.addItem(tr("wizard_financial.dep_macrs"), "macrs")
        tax_form.addRow(tr("wizard_financial.depreciation"), self._combo_depreciation)
        self._spin_dep_years = QSpinBox()
        self._spin_dep_years.setRange(1, 40)
        self._spin_dep_years.setValue(20)
        tax_form.addRow(tr("wizard_financial.depreciation_years"), self._spin_dep_years)
        self._spin_itc = self._pct_spin(0.0)
        tax_form.addRow(tr("wizard_financial.itc_rate"), self._spin_itc)
        self._spin_ptc = QDoubleSpinBox()
        self._spin_ptc.setRange(0, 100)
        self._spin_ptc.setDecimals(2)
        self._spin_ptc.setSuffix(" $/MWh")
        tax_form.addRow(tr("wizard_financial.ptc_rate"), self._spin_ptc)
        left.addWidget(tax_group)

        env_group = QGroupBox(tr("wizard_financial.grp_environmental"))
        env_form = QFormLayout(env_group)
        self._spin_carbon = QDoubleSpinBox()
        self._spin_carbon.setRange(0, 500)
        self._spin_carbon.setDecimals(1)
        self._spin_carbon.setSuffix(" $/tCO2")
        env_form.addRow(tr("wizard_financial.carbon_price"), self._spin_carbon)
        self._spin_carbon_esc = self._pct_spin(0.02)
        env_form.addRow(tr("wizard_financial.carbon_escalation"), self._spin_carbon_esc)
        left.addWidget(env_group)

        left.addStretch()
        columns.addLayout(left)

        # ── Right column ──
        right = QVBoxLayout()

        rev_group = QGroupBox(tr("wizard_financial.grp_revenue"))
        rev_form = QFormLayout(rev_group)
        self._spin_ppa = QDoubleSpinBox()
        self._spin_ppa.setRange(0, 500)
        self._spin_ppa.setDecimals(1)
        self._spin_ppa.setSuffix(" $/MWh")
        self._spin_ppa.setToolTip(tr("wizard_financial.ppa_price_tip"))
        rev_form.addRow(tr("wizard_financial.ppa_price"), self._spin_ppa)
        self._spin_ppa_esc = self._pct_spin(0.02)
        rev_form.addRow(tr("wizard_financial.ppa_escalation"), self._spin_ppa_esc)
        self._spin_cap_pay = QDoubleSpinBox()
        self._spin_cap_pay.setRange(0, 500000)
        self._spin_cap_pay.setDecimals(0)
        self._spin_cap_pay.setSuffix(" $/MW-yr")
        rev_form.addRow(tr("wizard_financial.capacity_payment"), self._spin_cap_pay)
        self._spin_rec = QDoubleSpinBox()
        self._spin_rec.setRange(0, 200)
        self._spin_rec.setDecimals(1)
        self._spin_rec.setSuffix(" $/MWh")
        rev_form.addRow(tr("wizard_financial.rec_price"), self._spin_rec)
        right.addWidget(rev_group)

        other_group = QGroupBox(tr("wizard_financial.grp_other"))
        other_form = QFormLayout(other_group)
        self._spin_insurance = self._pct_spin(0.005)
        other_form.addRow(tr("wizard_financial.insurance_rate"), self._spin_insurance)
        self._spin_salvage = self._pct_spin(0.05)
        other_form.addRow(tr("wizard_financial.salvage_fraction"), self._spin_salvage)
        self._spin_discount = self._pct_spin(0.08)
        other_form.addRow(tr("wizard_financial.discount_rate"), self._spin_discount)
        right.addWidget(other_group)

        right.addStretch()
        columns.addLayout(right)

        scroll.setWidget(form_widget)
        layout.addWidget(scroll, 1)

        # Analysis button + progress
        btn_row = QHBoxLayout()
        self._btn_analyze = QPushButton(tr("wizard_financial.load_analyze"))
        self._btn_analyze.setStyleSheet(
            "background-color: #2980b9; color: white; font-weight: bold; "
            "padding: 8px 16px; border-radius: 4px;"
        )
        self._btn_analyze.clicked.connect(self._run_analysis)
        btn_row.addWidget(self._btn_analyze)
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setVisible(False)
        btn_row.addWidget(self._progress)
        self._lbl_status = QLabel("")
        btn_row.addWidget(self._lbl_status)
        layout.addLayout(btn_row)

    def _pct_spin(self, default: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0, 1.0)
        spin.setDecimals(4)
        spin.setSingleStep(0.005)
        spin.setValue(default)
        return spin

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("wizard_financial.open_results"), "",
            tr("wizard_financial.filter_hdf5"),
        )
        if path:
            self._h5_path = path
            self._lbl_path.setText(Path(path).name)
            self._lbl_path.setStyleSheet("color: #2ecc71; font-weight: bold;")

    def _collect_assumptions(self) -> FinancialAssumptions:
        return FinancialAssumptions(
            debt_fraction=self._spin_debt_frac.value(),
            cost_of_debt=self._spin_cost_debt.value(),
            cost_of_equity=self._spin_cost_equity.value(),
            debt_tenor=self._spin_debt_tenor.value(),
            tax_rate=self._spin_tax_rate.value(),
            depreciation_method=self._combo_depreciation.currentData(),
            depreciation_years=self._spin_dep_years.value(),
            itc_rate=self._spin_itc.value(),
            ptc_rate=self._spin_ptc.value(),
            ppa_price=self._spin_ppa.value(),
            ppa_escalation=self._spin_ppa_esc.value(),
            capacity_payment=self._spin_cap_pay.value(),
            rec_price=self._spin_rec.value(),
            carbon_price=self._spin_carbon.value(),
            carbon_price_escalation=self._spin_carbon_esc.value(),
            insurance_rate=self._spin_insurance.value(),
            salvage_fraction=self._spin_salvage.value(),
            discount_rate=self._spin_discount.value(),
        )

    def _run_analysis(self):
        if not self._h5_path:
            QMessageBox.warning(
                self, tr("wizard_financial.no_file_title"),
                tr("wizard_financial.no_file_msg"),
            )
            return
        self._assumptions = self._collect_assumptions()
        self._btn_analyze.setEnabled(False)
        self._progress.setVisible(True)
        self._lbl_status.setText(tr("wizard_financial.analyzing"))

        self._worker = _FinancialWorker(self._h5_path, self._assumptions)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_finished(self, sf, tf):
        self._system_financials = sf
        self._tech_financials = tf
        self._progress.setVisible(False)
        self._btn_analyze.setEnabled(True)
        self._lbl_status.setText(tr("wizard_financial.analysis_done"))
        self._lbl_status.setStyleSheet("color: #2ecc71; font-weight: bold;")
        self.analysisFinished.emit()

    def _on_error(self, msg):
        self._progress.setVisible(False)
        self._btn_analyze.setEnabled(True)
        self._lbl_status.setText(tr("wizard_financial.error_status", e=msg))
        self._lbl_status.setStyleSheet("color: #e74c3c;")
        QMessageBox.critical(self, tr("wizard_financial.analysis_error_title"), msg)

    def is_valid(self) -> bool:
        return self._system_financials is not None

    def get_h5_path(self) -> Optional[str]:
        return self._h5_path

    def get_assumptions(self) -> FinancialAssumptions:
        return self._assumptions

    def get_system_financials(self) -> Optional[SystemFinancials]:
        return self._system_financials

    def get_tech_financials(self) -> Optional[dict]:
        return self._tech_financials


# =====================================================================
# Step 2: Cost Decomposition
# =====================================================================


class CostDecompositionStep(QWidget):
    """NPV waterfall, annual stacked bar, and pie chart."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        self._tabs = QTabWidget()

        self._waterfall = WaterfallChart()
        self._tabs.addTab(self._waterfall, tr("wizard_financial.tab_waterfall"))

        self._stacked = StackedBarChart()
        self._tabs.addTab(self._stacked, tr("wizard_financial.tab_annual_cf"))

        self._pie = CostPieChart()
        self._tabs.addTab(self._pie, tr("wizard_financial.tab_cost_breakdown"))

        layout.addWidget(self._tabs)

    def set_inputs(self, sf: SystemFinancials):
        # Waterfall — components must include Tax so the running sum lands on
        # the Net NPV total bar (see WaterfallChart.update_chart).
        labels = [tr(f"wizard_financial.{k}") for k in (
            "wf_revenue", "wf_fuel", "wf_om", "wf_capex", "wf_penalties",
            "wf_tax", "wf_tax_benefits", "wf_salvage", "wf_net_npv",
        )]
        values = [
            sf.npv_revenue, -sf.npv_fuel, -sf.npv_om, -sf.npv_capex,
            -sf.npv_penalties, -sf.npv_tax, sf.npv_tax_benefits,
            sf.npv_salvage, sf.npv_total,
        ]
        self._waterfall.update_chart(
            labels, values, title=tr("wizard_financial.chart_waterfall"))

        # Stacked bar
        self._stacked.update_chart(
            sf.cash_flows, title=tr("wizard_financial.tab_annual_cf"))

        # Pie
        pie_labels = [tr(f"wizard_financial.{k}") for k in (
            "wf_fuel", "wf_om", "wf_capex", "wf_penalties")]
        pie_values = [sf.npv_fuel, sf.npv_om, sf.npv_capex, sf.npv_penalties]
        self._pie.update_chart(pie_labels, pie_values, title=tr("wizard_financial.chart_cost_pie"))

    def is_valid(self) -> bool:
        return True


# =====================================================================
# Step 3: Technology Economics
# =====================================================================


class TechnologyEconomicsStep(QWidget):
    """Per-technology metrics table, bubble chart, revenue vs cost bars."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        self._tabs = QTabWidget()

        # Table tab
        self._table = QTableWidget()
        self._table.setColumnCount(8)
        self._table.setHorizontalHeaderLabels([tr(f"wizard_financial.{k}") for k in (
            "col_technology", "col_type", "col_installed", "col_generation",
            "col_cf", "col_lcoe", "col_revenue", "col_roi",
        )])
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._tabs.addTab(self._table, tr("wizard_financial.tab_metrics_table"))

        # Bubble chart tab
        self._bubble = BubbleChart()
        self._tabs.addTab(self._bubble, tr("wizard_financial.tab_cf_lcoe"))

        # Revenue vs Cost tab
        self._rev_cost = TechRevenueVsCostChart()
        self._tabs.addTab(self._rev_cost, tr("wizard_financial.tab_rev_cost"))

        layout.addWidget(self._tabs)

    def set_inputs(self, tech_financials: dict[str, TechnologyFinancials]):
        # Table
        items = list(tech_financials.values())
        self._table.setRowCount(len(items))
        for i, tf in enumerate(items):
            self._table.setItem(i, 0, QTableWidgetItem(tf.name))
            self._table.setItem(i, 1, QTableWidgetItem(tf.tech_type))
            self._table.setItem(i, 2, QTableWidgetItem(f"{tf.installed_mw:.1f}"))
            self._table.setItem(i, 3, QTableWidgetItem(f"{tf.generation_mwh/1e3:.1f}"))
            self._table.setItem(i, 4, QTableWidgetItem(f"{tf.capacity_factor*100:.1f}"))
            lcoe_str = f"{tf.lcoe:.1f}" if tf.lcoe < 1000 else tr("wizard_financial.na")
            self._table.setItem(i, 5, QTableWidgetItem(lcoe_str))
            self._table.setItem(i, 6, QTableWidgetItem(f"{tf.revenue_total/1e6:.2f}"))
            self._table.setItem(i, 7, QTableWidgetItem(f"{tf.roi*100:.1f}"))

        # Bubble chart
        self._bubble.update_chart(tech_financials)

        # Revenue vs Cost
        self._rev_cost.update_chart(tech_financials)

    def is_valid(self) -> bool:
        return True


# =====================================================================
# Step 4: Market Analysis
# =====================================================================


class MarketAnalysisStep(QWidget):
    """Price duration curve and revenue breakdown."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        self._tabs = QTabWidget()

        self._price_curve = PriceDurationCurve()
        self._tabs.addTab(self._price_curve, tr("wizard_financial.tab_price_duration"))

        self._cum_npv = CumulativeNPVChart()
        self._tabs.addTab(self._cum_npv, tr("wizard_financial.tab_cum_npv"))

        layout.addWidget(self._tabs)

    def set_inputs(self, sf: SystemFinancials, prices: Optional[np.ndarray] = None):
        if prices is not None:
            self._price_curve.update_chart(prices)
        self._cum_npv.update_chart(sf.cash_flows)

    def is_valid(self) -> bool:
        return True


# =====================================================================
# Step 5: Cash Flow Pro Forma
# =====================================================================


class CashFlowStep(QWidget):
    """Pro forma table + cumulative NPV chart + DSCR timeline."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Vertical)

        # Table
        self._table = QTableWidget()
        splitter.addWidget(self._table)

        # Charts
        chart_tabs = QTabWidget()
        self._cum_chart = CumulativeNPVChart()
        chart_tabs.addTab(self._cum_chart, tr("wizard_financial.tab_cum_npv"))
        self._dscr_chart = DSCRTimeline()
        chart_tabs.addTab(self._dscr_chart, tr("wizard_financial.tab_dscr"))
        splitter.addWidget(chart_tabs)

        splitter.setSizes([300, 300])
        layout.addWidget(splitter)

    def set_inputs(self, sf: SystemFinancials):
        cf = sf.cash_flows
        if cf.empty:
            return

        # Table
        cols = list(cf.columns)
        self._table.setColumnCount(len(cols))
        self._table.setHorizontalHeaderLabels([
            tr(f"wizard_financial.{_CF_COLUMN_KEYS[c]}") if c in _CF_COLUMN_KEYS else str(c)
            for c in cols
        ])
        self._table.setRowCount(len(cf))
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)

        for r in range(len(cf)):
            for c, col in enumerate(cols):
                val = cf.iloc[r][col]
                if isinstance(val, float):
                    text = f"{val:,.0f}" if abs(val) > 100 else f"{val:.3f}"
                else:
                    text = str(val)
                self._table.setItem(r, c, QTableWidgetItem(text))

        # Charts
        self._cum_chart.update_chart(cf)
        years = cf["year"].tolist()
        self._dscr_chart.update_chart(years, sf.dscr_annual)

    def is_valid(self) -> bool:
        return True


# =====================================================================
# Step 6: Investment Metrics Dashboard
# =====================================================================


class InvestmentMetricsStep(QWidget):
    """Large-font metric cards for key financial indicators."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(tr("wizard_financial.key_metrics_header")))

        self._cards_layout = QHBoxLayout()
        layout.addLayout(self._cards_layout)

        self._cards_layout_2 = QHBoxLayout()
        layout.addLayout(self._cards_layout_2)

        layout.addWidget(QLabel(
            "<b>{}</b> <span style='color:#888;'>{}</span>".format(
                tr("wizard_financial.new_investment_header"),
                tr("wizard_financial.new_investment_note"),
            )
        ))
        self._cards_layout_3 = QHBoxLayout()
        layout.addLayout(self._cards_layout_3)

        layout.addStretch()

    def set_inputs(self, sf: SystemFinancials):
        # Clear existing cards
        for lay in (self._cards_layout, self._cards_layout_2, self._cards_layout_3):
            while lay.count():
                item = lay.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()

        # Row 1
        self._cards_layout.addWidget(
            _metric_card(tr("wizard_financial.card_npv"), f"${sf.npv_total/1e6:,.1f}M",
                         "#27ae60" if sf.npv_total > 0 else "#e74c3c")
        )
        self._cards_layout.addWidget(
            _metric_card(tr("wizard_financial.card_project_irr"), _pct_or_na(sf.project_irr), "#2980b9")
        )
        self._cards_layout.addWidget(
            _metric_card(tr("wizard_financial.card_equity_irr"), _pct_or_na(sf.equity_irr), "#16a085")
        )
        self._cards_layout.addWidget(
            _metric_card(tr("wizard_financial.card_mirr"), _pct_or_na(sf.mirr), "#8e44ad")
        )

        # Row 2
        pb_str = _years_or_na(sf.payback_simple)
        self._cards_layout_2.addWidget(_metric_card(tr("wizard_financial.card_simple_payback"), pb_str, "#e67e22"))

        dpb_str = _years_or_na(sf.payback_discounted)
        self._cards_layout_2.addWidget(_metric_card(tr("wizard_financial.card_discounted_payback"), dpb_str, "#f39c12"))

        self._cards_layout_2.addWidget(
            _metric_card(tr("wizard_financial.card_wacc"), f"{sf.wacc:.2%}", "#2c3e50")
        )

        dscr_str = f"{sf.dscr_min:.2f}×" if sf.dscr_min < 100 else "∞"
        dscr_color = "#27ae60" if sf.dscr_min >= 1.2 else "#e74c3c"
        self._cards_layout_2.addWidget(_metric_card(tr("wizard_financial.card_min_dscr"), dscr_str, dscr_color))

        self._cards_layout_2.addWidget(
            _metric_card(tr("wizard_financial.card_system_lcoe"), f"${sf.lcoe_system:.1f}/MWh", "#2980b9")
        )

        # Row 3 — new-investment economics
        self._cards_layout_3.addWidget(
            _metric_card(tr("wizard_financial.card_inv_npv"), f"${sf.investment_npv/1e6:,.1f}M",
                         "#27ae60" if sf.investment_npv > 0 else "#e74c3c")
        )
        self._cards_layout_3.addWidget(
            _metric_card(tr("wizard_financial.card_inv_irr"), _pct_or_na(sf.investment_irr), "#2980b9")
        )
        inv_pb = _years_or_na(sf.investment_payback)
        self._cards_layout_3.addWidget(
            _metric_card(tr("wizard_financial.card_inv_payback"), inv_pb, "#e67e22")
        )
        self._cards_layout_3.addWidget(
            _metric_card(tr("wizard_financial.card_inv_capex"), f"${sf.investment_capex/1e6:,.1f}M",
                         "#2c3e50")
        )
        inv_lcoe = (f"${sf.investment_lcoe:.1f}/MWh"
                    if sf.investment_lcoe < 1e4 else tr("wizard_financial.na"))
        self._cards_layout_3.addWidget(
            _metric_card(tr("wizard_financial.card_inv_lcoe"), inv_lcoe, "#16a085")
        )

    def is_valid(self) -> bool:
        return True


# =====================================================================
# Step 7: Sensitivity & Monte Carlo
# =====================================================================


class SensitivityStep(QWidget):
    """Sensitivity variable selection + tornado/spider + Monte Carlo."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._h5_path: Optional[str] = None
        self._assumptions: Optional[FinancialAssumptions] = None
        self._sf: Optional[SystemFinancials] = None
        self._sensitivity_result: Optional[SensitivityResult] = None
        self._mc_result: Optional[MonteCarloResult] = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Controls row
        controls = QHBoxLayout()

        # Variable checkboxes
        var_group = QGroupBox(tr("wizard_financial.grp_sensitivity_vars"))
        var_lay = QVBoxLayout(var_group)
        self._var_checks: dict[str, QCheckBox] = {}
        default_vars = ["discount_rate", "ppa_price", "carbon_price",
                        "debt_fraction", "cost_of_debt", "tax_rate"]
        for var in default_vars:
            cb = QCheckBox(tr(f"wizard_financial.{_SENSITIVITY_VAR_KEYS[var]}"))
            cb.setChecked(True)
            cb.setProperty("var_name", var)
            self._var_checks[var] = cb
            var_lay.addWidget(cb)
        controls.addWidget(var_group)

        # Run buttons
        btn_col = QVBoxLayout()
        self._spin_points = QSpinBox()
        self._spin_points.setRange(3, 21)
        self._spin_points.setValue(7)
        btn_col.addWidget(QLabel(tr("wizard_financial.sweep_points")))
        btn_col.addWidget(self._spin_points)

        self._btn_sensitivity = QPushButton(tr("wizard_financial.run_sensitivity"))
        self._btn_sensitivity.setStyleSheet(
            "background-color: #2980b9; color: white; font-weight: bold; padding: 6px;"
        )
        self._btn_sensitivity.clicked.connect(self._run_sensitivity)
        btn_col.addWidget(self._btn_sensitivity)

        btn_col.addSpacing(20)
        self._spin_mc_samples = QSpinBox()
        self._spin_mc_samples.setRange(50, 10000)
        self._spin_mc_samples.setValue(500)
        self._spin_mc_samples.setSingleStep(100)
        btn_col.addWidget(QLabel(tr("wizard_financial.mc_samples")))
        btn_col.addWidget(self._spin_mc_samples)

        self._btn_mc = QPushButton(tr("wizard_financial.run_mc"))
        self._btn_mc.setStyleSheet(
            "background-color: #e67e22; color: white; font-weight: bold; padding: 6px;"
        )
        self._btn_mc.clicked.connect(self._run_monte_carlo)
        btn_col.addWidget(self._btn_mc)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setVisible(False)
        btn_col.addWidget(self._progress)
        btn_col.addStretch()
        controls.addLayout(btn_col)
        layout.addLayout(controls)

        # Charts
        self._chart_tabs = QTabWidget()
        self._tornado = TornadoDiagram()
        self._chart_tabs.addTab(self._tornado, tr("wizard_financial.tab_tornado"))
        self._spider = SpiderPlot()
        self._chart_tabs.addTab(self._spider, tr("wizard_financial.tab_spider"))
        self._npv_hist = NPVHistogram()
        self._chart_tabs.addTab(self._npv_hist, tr("wizard_financial.tab_npv_dist"))
        self._irr_hist = IRRHistogram()
        self._chart_tabs.addTab(self._irr_hist, tr("wizard_financial.tab_irr_dist"))
        layout.addWidget(self._chart_tabs, 1)

    def set_inputs(self, h5_path: str, assumptions: FinancialAssumptions, sf: SystemFinancials):
        self._h5_path = h5_path
        self._assumptions = assumptions
        self._sf = sf

    def _get_selected_vars(self) -> list[str]:
        return [
            cb.property("var_name")
            for cb in self._var_checks.values()
            if cb.isChecked()
        ]

    def _run_sensitivity(self):
        if not self._h5_path or not self._assumptions:
            return
        variables = self._get_selected_vars()
        if not variables:
            QMessageBox.warning(
                self, tr("wizard_financial.no_vars_title"),
                tr("wizard_financial.no_vars_msg"),
            )
            return

        self._progress.setVisible(True)
        self._btn_sensitivity.setEnabled(False)
        self._worker = _SensitivityWorker(
            self._h5_path, self._assumptions, variables, self._spin_points.value(),
        )
        self._worker.finished.connect(self._on_sensitivity_done)
        self._worker.error.connect(self._on_worker_error)
        self._worker.start()

    def _on_sensitivity_done(self, result: SensitivityResult):
        self._sensitivity_result = result
        self._progress.setVisible(False)
        self._btn_sensitivity.setEnabled(True)
        self._tornado.update_chart(result.tornado, result.base_npv)
        self._spider.update_chart(result.sweeps, result.base_npv)
        self._chart_tabs.setCurrentIndex(0)

    def _run_monte_carlo(self):
        if not self._h5_path or not self._assumptions:
            return
        self._progress.setVisible(True)
        self._btn_mc.setEnabled(False)
        self._mc_worker = _MonteCarloWorker(
            self._h5_path, self._assumptions, self._spin_mc_samples.value(),
        )
        self._mc_worker.finished.connect(self._on_mc_done)
        self._mc_worker.error.connect(self._on_worker_error)
        self._mc_worker.start()

    def _on_mc_done(self, result: MonteCarloResult):
        self._mc_result = result
        self._progress.setVisible(False)
        self._btn_mc.setEnabled(True)
        self._npv_hist.update_chart(result.npv_samples, result.npv_var_5, result.npv_cvar_5)
        wacc = self._sf.wacc if self._sf else 0.0
        self._irr_hist.update_chart(result.irr_samples, wacc)
        self._chart_tabs.setCurrentIndex(2)

    def _on_worker_error(self, msg):
        self._progress.setVisible(False)
        self._btn_sensitivity.setEnabled(True)
        self._btn_mc.setEnabled(True)
        QMessageBox.critical(self, tr("common.error"), msg)

    def is_valid(self) -> bool:
        return True

    def get_sensitivity_result(self) -> Optional[SensitivityResult]:
        return self._sensitivity_result

    def get_monte_carlo_result(self) -> Optional[MonteCarloResult]:
        return self._mc_result


# =====================================================================
# Step 8: Report & Export
# =====================================================================


class ReportStep(QWidget):
    """Auto-generated executive summary with export options."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sf: Optional[SystemFinancials] = None
        self._tf: Optional[dict] = None
        self._sensitivity: Optional[SensitivityResult] = None
        self._mc: Optional[MonteCarloResult] = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(tr("wizard_financial.report_header")))

        self._report = QTextEdit()
        self._report.setReadOnly(True)
        self._report.setStyleSheet(scale_qss_fonts("font-family: monospace; font-size: 11px;"))
        layout.addWidget(self._report, 1)

        # Export buttons
        btn_row = QHBoxLayout()
        btn_csv = QPushButton(tr("wizard_financial.export_csv"))
        btn_csv.clicked.connect(self._export_csv)
        btn_row.addWidget(btn_csv)
        btn_txt = QPushButton(tr("wizard_financial.export_txt"))
        btn_txt.clicked.connect(self._export_txt)
        btn_row.addWidget(btn_txt)
        btn_row.addStretch()
        layout.addLayout(btn_row)

    def set_inputs(
        self,
        sf: SystemFinancials,
        tf: Optional[dict] = None,
        sensitivity: Optional[SensitivityResult] = None,
        mc: Optional[MonteCarloResult] = None,
    ):
        self._sf = sf
        self._tf = tf
        self._sensitivity = sensitivity
        self._mc = mc
        self._generate_report()

    def _generate_report(self):
        sf = self._sf
        if sf is None:
            return

        L = _report_line
        lines = []
        lines.append("=" * 60)
        lines.append("  " + tr("wizard_financial.rpt_title"))
        lines.append("=" * 60)
        lines.append("")

        lines.append(f"--- {tr('wizard_financial.rpt_key_metrics')} ---")
        lines.append(L("rpt_npv", f"${sf.npv_total:>15,.0f}"))
        lines.append(L("rpt_project_irr", f"{_pct_or_na(sf.project_irr, 2):>15}"))
        lines.append(L("rpt_equity_irr", f"{_pct_or_na(sf.equity_irr, 2):>15}"))
        lines.append(L("rpt_mirr", f"{_pct_or_na(sf.mirr, 2):>15}"))
        lines.append(L("rpt_wacc", f"{sf.wacc:>15.2%}"))
        pb = _years_or_na(sf.payback_simple, long=True)
        lines.append(L("rpt_simple_payback", f"{pb:>15}"))
        dpb = _years_or_na(sf.payback_discounted, long=True)
        lines.append(L("rpt_discounted_payback", f"{dpb:>15}"))
        lines.append(L("rpt_system_lcoe", f"${sf.lcoe_system:>14.2f}/MWh"))
        dscr = f"{sf.dscr_min:.2f}×" if sf.dscr_min < 100 else "∞"
        lines.append(L("rpt_min_dscr", f"{dscr:>15}"))
        lines.append(L("rpt_llcr", f"{sf.llcr:>15.2f}"))
        lines.append(L("rpt_profitability_idx", f"{sf.profitability_index:>15.2f}"))
        lines.append("")

        lines.append(f"--- {tr('wizard_financial.rpt_new_investment')} ---")
        lines.append(L("rpt_inv_npv", f"${sf.investment_npv:>15,.0f}"))
        lines.append(L("rpt_inv_irr", f"{_pct_or_na(sf.investment_irr, 2):>15}"))
        inv_pb = _years_or_na(sf.investment_payback, long=True)
        lines.append(L("rpt_inv_payback", f"{inv_pb:>15}"))
        lines.append(L("rpt_inv_capex", f"${sf.investment_capex:>15,.0f}"))
        inv_lcoe = (f"${sf.investment_lcoe:.2f}/MWh"
                    if sf.investment_lcoe < 1e4 else tr("wizard_financial.na"))
        lines.append(L("rpt_inv_lcoe", f"{inv_lcoe:>15}"))
        lines.append("")

        lines.append(f"--- {tr('wizard_financial.rpt_npv_decomposition')} ---")
        lines.append(L("rpt_revenue", f"${sf.npv_revenue:>15,.0f}"))
        lines.append(L("rpt_fuel_cost", f"${sf.npv_fuel:>15,.0f}"))
        lines.append(L("rpt_om_cost", f"${sf.npv_om:>15,.0f}"))
        lines.append(L("rpt_capex", f"${sf.npv_capex:>15,.0f}"))
        lines.append(L("rpt_penalties", f"${sf.npv_penalties:>15,.0f}"))
        lines.append(L("rpt_tax_benefits", f"${sf.npv_tax_benefits:>15,.0f}"))
        lines.append(L("rpt_salvage", f"${sf.npv_salvage:>15,.0f}"))
        lines.append(L("rpt_net_npv", f"${sf.npv_total:>15,.0f}"))
        lines.append("")

        if self._tf:
            lines.append(f"--- {tr('wizard_financial.rpt_tech_economics')} ---")
            tech_hdr = _pad(tr("wizard_financial.col_technology"), 21)
            lines.append(f"  {tech_hdr}{'MW':>8} {'GWh':>8} {'CF%':>6} {'LCOE':>8} {'ROI%':>6}")
            lines.append("  " + "-" * 58)
            for name, tf in self._tf.items():
                lcoe_s = f"{tf.lcoe:.1f}" if tf.lcoe < 1000 else tr("wizard_financial.na")
                lines.append(
                    f"  {name:<20} {tf.installed_mw:>8.1f} "
                    f"{tf.generation_mwh/1e3:>8.1f} {tf.capacity_factor*100:>5.1f} "
                    f"{lcoe_s:>8} {tf.roi*100:>5.1f}"
                )
            lines.append("")

        if self._mc:
            lines.append(f"--- {tr('wizard_financial.rpt_mc_results')} ---")
            lines.append(L("rpt_mc_samples", f"{self._mc.n_samples:>15,}"))
            lines.append(L("rpt_mc_npv_mean", f"${self._mc.npv_mean:>15,.0f}"))
            lines.append(L("rpt_mc_npv_std", f"${self._mc.npv_std:>15,.0f}"))
            lines.append(L("rpt_mc_npv_p5", f"${self._mc.npv_p5:>15,.0f}"))
            lines.append(L("rpt_mc_npv_p95", f"${self._mc.npv_p95:>15,.0f}"))
            lines.append(L("rpt_mc_var5", f"${self._mc.npv_var_5:>15,.0f}"))
            lines.append(L("rpt_mc_cvar5", f"${self._mc.npv_cvar_5:>15,.0f}"))
            lines.append(L("rpt_mc_irr_mean", f"{_pct_or_na(self._mc.irr_mean, 2):>15}"))
            lines.append("")

        lines.append("=" * 60)
        self._report.setPlainText("\n".join(lines))

    def _export_csv(self):
        if self._sf is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, tr("wizard_financial.export_csv_title"), "financial_analysis.csv",
            tr("wizard_financial.filter_csv"),
        )
        if path:
            self._sf.cash_flows.to_csv(path, index=False, float_format="%.2f")
            QMessageBox.information(
                self, tr("wizard_financial.export_title"),
                tr("wizard_financial.saved_to", path=path),
            )

    def _export_txt(self):
        path, _ = QFileDialog.getSaveFileName(
            self, tr("wizard_financial.export_report_title"), "financial_report.txt",
            tr("wizard_financial.filter_txt"),
        )
        if path:
            with open(path, "w") as f:
                f.write(self._report.toPlainText())
            QMessageBox.information(
                self, tr("wizard_financial.export_title"),
                tr("wizard_financial.saved_to", path=path),
            )

    def is_valid(self) -> bool:
        return True
