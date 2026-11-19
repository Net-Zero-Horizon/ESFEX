# -*- coding: utf-8 -*-
"""OTEC Studio window shell (M0).

A non-modal workbench over a shared :class:`OtexProject`. M0 ships the shell:
a working scenario bar (new / branch / remove / compare) and a tabbed workbench
of placeholder panels. Panels are filled in across milestones M1–M7 (see
``OTEX_STUDIO_DESIGN.md``); each will read/write the active scenario.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from esfex.visualization.i18n import tr
from esfex.visualization.workflows.otec_studio.project import OtexProject
from esfex.visualization.ui_scale import scale_qss_fonts

logger = logging.getLogger(__name__)


# Panel stubs: (panel id, milestone). The tab title and one-line purpose are
# the translation keys ``otec_studio.panel_<id>`` / ``otec_studio.panel_<id>_purpose``.
# Replaced by real panels as each milestone lands; kept here so the shell is
# navigable and self-documenting from day one.
_PANELS = [
    ("site", "M6"),
    ("cycle", "M2"),
    ("optimization", "M1"),
    ("operation", "M4"),
    ("economics", "M3"),
    ("uncertainty", "M5"),
    ("regional", "M7"),
]


def _stub_panel(title: str, purpose: str, milestone: str) -> QWidget:
    """A placeholder panel that documents what will live here."""
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setAlignment(Qt.AlignmentFlag.AlignTop)
    head = QLabel(f"<h2>{title}</h2>")
    lay.addWidget(head)
    desc = QLabel(purpose)
    desc.setWordWrap(True)
    desc.setStyleSheet("color: #aaa;")
    lay.addWidget(desc)
    badge = QLabel(tr("otec_studio.planned_milestone", milestone=milestone))
    badge.setStyleSheet(
        scale_qss_fonts("background-color: #34495e; color: #ecf0f1; border-radius: 4px; "
        "padding: 4px 8px; font-size: 11px;")
    )
    badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
    badge.setMaximumWidth(220)
    lay.addSpacing(8)
    lay.addWidget(badge)
    lay.addStretch()
    return w


class OTECStudioWindow(QMainWindow):
    """Non-linear OTEC design/analysis workbench over the OTEX library.

    Additive sibling of ``OTECWizard`` — does not replace it. ``map_widget`` and
    ``model`` are accepted for future site-picking / data export but are
    optional; the Studio is self-contained in M0.
    """

    def __init__(self, parent=None, model=None, map_widget=None):
        super().__init__(parent)
        self.model = model
        self.map_widget = map_widget
        self.project = OtexProject()

        self.setWindowTitle("OTEC Studio")
        self.resize(1150, 780)
        # Independent top-level window (non-modal), not a child dialog.
        self.setWindowFlags(Qt.WindowType.Window)

        self._build_ui()
        self._refresh_scenarios()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        root.addLayout(self._build_scenario_bar())

        self._tabs = QTabWidget()
        self._panels: dict[str, QWidget] = {}
        for panel_id, milestone in _PANELS:
            title = tr(f"otec_studio.panel_{panel_id}")
            purpose = tr(f"otec_studio.panel_{panel_id}_purpose")
            panel = self._make_panel(panel_id, title, purpose, milestone)
            self._panels[panel_id] = panel
            self._tabs.addTab(panel, title)
        root.addWidget(self._tabs, 1)

    def _make_panel(self, panel_id: str, title: str, purpose: str,
                    milestone: str) -> QWidget:
        """Real panel where implemented (M1 Optimization, M2 Cycle), else stub."""
        if panel_id == "optimization":
            from esfex.visualization.workflows.otec_studio.optimization_panel import (
                OptimizationPanel,
            )
            return OptimizationPanel()
        if panel_id == "cycle":
            from esfex.visualization.workflows.otec_studio.cycle_panel import (
                CyclePanel,
            )
            return CyclePanel()
        if panel_id == "economics":
            from esfex.visualization.workflows.otec_studio.economics_panel import (
                EconomicsPanel,
            )
            return EconomicsPanel()
        if panel_id == "operation":
            from esfex.visualization.workflows.otec_studio.operation_panel import (
                OperationPanel,
            )
            return OperationPanel()
        if panel_id == "uncertainty":
            from esfex.visualization.workflows.otec_studio.uq_panel import (
                UncertaintyPanel,
            )
            return UncertaintyPanel()
        if panel_id == "site":
            from esfex.visualization.workflows.otec_studio.resource_panel import (
                ResourcePanel,
            )
            return ResourcePanel()
        if panel_id == "regional":
            from esfex.visualization.workflows.otec_studio.regional_panel import (
                RegionalPanel,
            )
            return RegionalPanel()
        return _stub_panel(title, purpose, milestone)

    def _build_scenario_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.addWidget(QLabel(tr("otec_studio.scenario_label")))

        self._scenario_combo = QComboBox()
        self._scenario_combo.setMinimumWidth(220)
        self._scenario_combo.currentIndexChanged.connect(self._on_scenario_changed)
        bar.addWidget(self._scenario_combo)

        btn_new = QPushButton(tr("otec_studio.scenario_new"))
        btn_new.setToolTip(tr("otec_studio.scenario_new_tip"))
        btn_new.clicked.connect(self._on_new)
        bar.addWidget(btn_new)

        btn_branch = QPushButton(tr("otec_studio.scenario_branch"))
        btn_branch.setToolTip(tr("otec_studio.scenario_branch_tip"))
        btn_branch.clicked.connect(self._on_branch)
        bar.addWidget(btn_branch)

        self._btn_remove = QPushButton(tr("common.remove"))
        self._btn_remove.clicked.connect(self._on_remove)
        bar.addWidget(self._btn_remove)

        bar.addStretch()

        btn_compare = QPushButton(tr("otec_studio.scenario_compare"))
        btn_compare.setToolTip(tr("otec_studio.scenario_compare_tip"))
        btn_compare.clicked.connect(self._on_compare)
        bar.addWidget(btn_compare)

        return bar

    # ------------------------------------------------------------------
    # Scenario bar behaviour
    # ------------------------------------------------------------------

    def _refresh_scenarios(self) -> None:
        """Repopulate the combo from the project without re-triggering signals."""
        self._scenario_combo.blockSignals(True)
        self._scenario_combo.clear()
        self._scenario_combo.addItems([s.name for s in self.project.scenarios])
        self._scenario_combo.setCurrentIndex(self.project.active_index)
        self._scenario_combo.blockSignals(False)
        self._btn_remove.setEnabled(len(self.project.scenarios) > 1)

    def _on_scenario_changed(self, index: int) -> None:
        if 0 <= index < len(self.project.scenarios):
            self.project.set_active(index)
            self._sync_panels()

    def _on_new(self) -> None:
        self.project.add_scenario()
        self._refresh_scenarios()
        self._sync_panels()

    def _on_branch(self) -> None:
        self.project.branch()
        self._refresh_scenarios()
        self._sync_panels()

    def _on_remove(self) -> None:
        if len(self.project.scenarios) <= 1:
            return
        self.project.remove_scenario(self.project.active_index)
        self._refresh_scenarios()
        self._sync_panels()

    def _sync_panels(self) -> None:
        """Notify panels that the active scenario changed (no-op for stubs)."""
        active = self.project.active
        for panel in self._panels.values():
            updater = getattr(panel, "on_scenario_changed", None)
            if callable(updater):
                updater(active, self.project)

    def _on_compare(self) -> None:
        ScenarioCompareDialog(self.project, parent=self).exec()


class ScenarioCompareDialog(QDialog):
    """Side-by-side metric table across all scenarios — the wizard's hard wall."""

    # (metric key, header translation key)
    _COLUMNS = [
        ("name", "otec_studio.compare_col_scenario"),
        ("cycle", "otec_studio.compare_col_cycle"),
        ("fluid", "otec_studio.compare_col_fluid"),
        ("lcoe", "otec_studio.compare_col_lcoe"),
        ("p_net_mw", "otec_studio.compare_col_net_power"),
        ("capex", "otec_studio.compare_col_capex"),
        ("has_results", "otec_studio.compare_col_has_results"),
    ]

    def __init__(self, project: OtexProject, parent=None):
        super().__init__(parent)
        self._project = project
        self._rows = project.compare()
        self.setWindowTitle(tr("otec_studio.compare_title"))
        self.resize(820, 520)
        lay = QVBoxLayout(self)

        table = QTableWidget(len(self._rows), len(self._COLUMNS))
        table.setHorizontalHeaderLabels([tr(c[1]) for c in self._COLUMNS])
        table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        for r, row in enumerate(self._rows):
            for c, (key, _label) in enumerate(self._COLUMNS):
                val = row.get(key)
                if val is None:
                    text = "—"
                elif key == "lcoe" and isinstance(val, float):
                    text = f"{val:.4f}"
                elif key in ("p_net_mw", "capex") and isinstance(val, float):
                    text = f"{val:,.1f}"
                elif key == "has_results":
                    text = tr("common.yes") if val else tr("common.no")
                elif key in ("cycle", "fluid"):
                    label_key = f"otec_studio.{key}_{val}"
                    text = tr(label_key)
                    if text == label_key:   # value without a translated label
                        text = str(val)
                else:
                    text = str(val)
                table.setItem(r, c, QTableWidgetItem(text))
        lay.addWidget(table)

        # LCOE bar chart across scenarios (only those with a value)
        self._add_lcoe_chart(lay)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_export = QPushButton(tr("otec_studio.export_csv"))
        btn_export.clicked.connect(self._export)
        btn_row.addWidget(btn_export)
        lay.addLayout(btn_row)

    def _add_lcoe_chart(self, lay):
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure

        named = [(r["name"], r["lcoe"]) for r in self._rows if r.get("lcoe")]
        if not named:
            return
        fig = Figure(figsize=(6, 2.6), dpi=100, layout="constrained")
        canvas = FigureCanvasQTAgg(fig)
        ax = fig.add_subplot(111)
        names = [n for n, _ in named]
        vals = [v for _, v in named]
        ax.bar(range(len(names)), vals, color="#2980b9", edgecolor="white")
        ax.set_xticks(range(len(names)))
        ax.set_xticklabels(names, rotation=20, ha="right", fontsize=8)
        ax.set_ylabel("LCOE")
        ax.set_title(tr("otec_studio.compare_chart_title"), fontsize=10,
                     fontweight="bold")
        ax.grid(axis="y", alpha=0.3)
        lay.addWidget(canvas)

    def _export(self):
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        path, _ = QFileDialog.getSaveFileName(
            self, tr("otec_studio.compare_export_title"), "otec_scenarios.csv",
            tr("otec_studio.csv_filter"))
        if not path:
            return
        try:
            import csv
            keys = [k for k, _ in self._COLUMNS]
            with open(path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=keys)
                w.writeheader()
                for row in self._rows:
                    w.writerow({k: row.get(k) for k in keys})
            QMessageBox.information(self, tr("otec_studio.export_title"),
                                    tr("otec_studio.saved_to", path=path))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, tr("otec_studio.export_failed"), str(exc))
