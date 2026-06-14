"""Wind Resource Assessment wizard dialog.

Two-phase multi-step wizard:

Phase A — Wind Resource Assessment & MCDA:
  1. Define domain (rectangle on map or manual coordinates)
  2. Configure turbine and assessment parameters
  3. Configure MCDA criteria and weighting method
  4. Run analysis (ERA5 + DEM + LULC + MCDA)
  5. View results and generate development zones

Phase B — Advanced Analysis:
  6. Wind characterization (Weibull, wind rose, diurnal, seasonal)
  7. Financial analysis (LCOE, NPV, IRR, sensitivity)
  8. Wake effect modeling (Jensen/Park, array efficiency)
  9. Availability profile generation (hourly CF for model generators)
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from esfex.visualization.i18n import tr

from esfex.visualization.workflows.wind_steps import (
    CriteriaConfigStep,
    WindAnalysisStep,
    WindConfigStep,
    WindDomainStep,
    WindResultsStep,
)

from esfex.visualization.workflows.wind_advanced_steps import (
    WindAvailabilityStep,
    WindCharacterizationStep,
    WindFinancialStep,
    WindWakeLayoutStep,
)
from esfex.visualization.workflows._workflow_step import WorkflowStep

# Consolidated two-column steps
_STEP_NAMES = [
    lambda: tr("wizard_wind.step_setup"),
    lambda: tr("wizard_wind.step_suitability"),
    lambda: tr("wizard_wind.step_results_econ"),
    lambda: tr("wizard_wind.step_refinement"),
]

# Index of the container that holds the (long-running) Analysis step.
_ANALYSIS_STEP = 1


class WindWizard(QDialog):
    """Multi-step wizard for wind resource assessment with MCDA and advanced analysis."""

    def __init__(self, map_widget, model=None, parent=None,
                 geo_assets_provider=None):
        super().__init__(parent)
        self._geo_assets_provider = geo_assets_provider
        self.setWindowTitle(tr("wizard_wind.title"))
        self.setMinimumSize(750, 580)
        self.resize(950, 700)
        self.setModal(False)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
        )

        self._map_widget = map_widget
        self._model = model
        self._current_step = 0

        self._build_ui()
        self._update_navigation()

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Single 4-dot step indicator bar
        self._indicator_bar = QHBoxLayout()
        self._step_labels: list[QLabel] = []
        for i, name_fn in enumerate(_STEP_NAMES):
            lbl = QLabel(f"  {i+1}. {name_fn()}  ")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet(
                self._step_style(is_current=(i == 0), is_done=False)
            )
            self._step_labels.append(lbl)
            self._indicator_bar.addWidget(lbl)
        layout.addLayout(self._indicator_bar)

        # Separator below indicators
        sep = QWidget()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background-color: #555;")
        layout.addWidget(sep)

        # Stacked widget for step pages
        self._stack = QStackedWidget()

        # Underlying step widgets (reused unchanged inside two-column containers)
        self._step_domain = WindDomainStep(
            self._map_widget, geo_assets_provider=self._geo_assets_provider)
        self._step_config = WindConfigStep()
        self._step_criteria = CriteriaConfigStep()
        self._step_analysis = WindAnalysisStep()
        self._step_results = WindResultsStep(self._map_widget, self._model)
        self._step_characterization = WindCharacterizationStep()
        self._step_financial = WindFinancialStep()
        self._step_wake = WindWakeLayoutStep()
        self._step_availability = WindAvailabilityStep(model=self._model)

        # Analysis pulls inputs live from sibling Criteria + prior Domain/Config;
        # Wake pulls the live wind rose from sibling Characterization (C4).
        self._step_analysis.set_input_provider(self._analysis_inputs)
        self._step_wake.set_input_provider(self._wake_inputs)

        # Four consolidated steps. Two columns only for the light, chart-free
        # Criteria | Analysis pair; wide panels (turbine table, charts) get their
        # own full-width row and the step scrolls vertically when tall.
        self._steps = [
            WorkflowStep([self._step_domain, self._step_config]),
            WorkflowStep([(self._step_criteria, self._step_analysis)]),
            WorkflowStep([self._step_results, self._step_financial]),
            WorkflowStep([
                self._step_characterization,
                self._step_wake,
                self._step_availability,
            ]),
        ]
        for step in self._steps:
            self._stack.addWidget(step)

        layout.addWidget(self._stack, 1)

        # Button row
        btn_layout = QHBoxLayout()

        self._btn_cancel = QPushButton(tr("wizard_wind.cancel"))
        self._btn_cancel.clicked.connect(self._on_cancel)
        btn_layout.addWidget(self._btn_cancel)

        btn_layout.addStretch()

        self._btn_back = QPushButton(tr("wizard_wind.back"))
        self._btn_back.clicked.connect(self._go_back)
        btn_layout.addWidget(self._btn_back)

        self._btn_next = QPushButton(tr("wizard_wind.next"))
        self._btn_next.clicked.connect(self._go_next)
        btn_layout.addWidget(self._btn_next)

        layout.addLayout(btn_layout)

        # Connect step signals
        self._step_analysis.analysisFinished.connect(self._on_analysis_finished)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _analysis_inputs(self):
        """Live inputs for the Analysis step (set_inputs args tuple)."""
        return (
            self._step_domain.get_bounds(),
            self._step_config.get_config(),
            self._step_criteria.get_config(),
            self._get_transmission_lines(),
            self._step_domain.get_polygon(),
        )

    def _wake_inputs(self):
        """Live inputs for the Wake step (set_inputs args tuple)."""
        config = self._step_config.get_config()
        summary = self._step_analysis.get_summary()
        turbine = self._step_config.get_turbine_spec()
        rotor_d = turbine.rotor_diameter_m if turbine else 126.0
        workers = config.effective_workers if config else 0
        return (
            self._step_characterization.get_wind_rose(),
            rotor_d,
            summary.cf_avg if summary else 0.30,
            config.turbine_capacity_mw if config else 3.0,
            workers,
        )

    def _go_next(self):
        step = self._steps[self._current_step]
        if not step.is_valid():
            return

        # Push data across container boundaries (same calls as before, grouped).
        if self._current_step == 1:
            # Suitability → Results & Economics: feed Results + Financial.
            summary = self._step_analysis.get_summary()
            config = self._step_config.get_config()
            self._step_results.set_results(summary, config)
            capacity_mw = config.turbine_capacity_mw if config else 3.0
            cf_avg = summary.cf_avg if summary else 0.30
            workers = config.effective_workers if config else 0
            self._step_financial.set_inputs(capacity_mw, cf_avg, workers)
        elif self._current_step == 2:
            # Results & Economics → Refinement: feed Characterization, Wake, Availability.
            summary = self._step_analysis.get_summary()
            config = self._step_config.get_config()
            hourly_data = summary.hourly_data if summary else None
            self._step_characterization.set_inputs(hourly_data, summary)
            # Wake gets seeded here; its provider refreshes the wind rose at Run.
            self._step_wake.set_inputs(*self._wake_inputs())
            self._step_availability.set_inputs(hourly_data, config, summary)

        if self._current_step < len(self._steps) - 1:
            self._current_step += 1
            self._stack.setCurrentIndex(self._current_step)
            self._update_navigation()

    def _go_back(self):
        if self._current_step > 0:
            self._current_step -= 1
            self._stack.setCurrentIndex(self._current_step)
            self._update_navigation()

    def _update_navigation(self):
        idx = self._current_step
        n = len(self._steps)

        # Update step indicator labels
        for i, lbl in enumerate(self._step_labels):
            lbl.setStyleSheet(self._step_style(
                is_current=(i == idx),
                is_done=(i < idx),
            ))

        # Update buttons
        self._btn_back.setEnabled(idx > 0)
        self._btn_back.setVisible(idx > 0)

        if idx == n - 1:
            self._btn_next.setText(tr("common.close"))
            try:
                self._btn_next.clicked.disconnect()
            except RuntimeError:
                pass
            self._btn_next.clicked.connect(self.accept)
        elif idx == _ANALYSIS_STEP:
            # Suitability holds the Analysis: only enable Next once it has run.
            self._btn_next.setText(tr("wizard_wind.next"))
            try:
                self._btn_next.clicked.disconnect()
            except RuntimeError:
                pass
            self._btn_next.clicked.connect(self._go_next)
            self._btn_next.setEnabled(self._step_analysis.is_valid())
        else:
            self._btn_next.setText(tr("wizard_wind.next"))
            try:
                self._btn_next.clicked.disconnect()
            except RuntimeError:
                pass
            self._btn_next.clicked.connect(self._go_next)
            self._btn_next.setEnabled(True)

    def _on_analysis_finished(self):
        """Enable Next button when analysis completes."""
        self._btn_next.setEnabled(True)

    def _get_transmission_lines(self) -> list:
        """Extract transmission line coordinates from the GUI model."""
        if self._model is None:
            return []

        lines = []
        try:
            state = self._model.state
            for line in state.transmission_lines:
                coords = []
                for pt in line.trace:
                    coords.append([pt.lat, pt.lng])
                if coords:
                    lines.append({"coords": coords})
        except Exception:
            pass
        return lines

    def closeEvent(self, event):
        from esfex.visualization.workflows._wizard_utils import cleanup_wizard
        cleanup_wizard(self)
        super().closeEvent(event)

    def _cleanup_map(self):
        """Remove all temporary wind overlays from the map."""
        self._map_widget.clear_wind_domain()
        self._map_widget.clear_wind_results()
        self._map_widget.clear_wind_dev_zones()
        self._map_widget.disable_rectangle_draw()

    def _on_cancel(self):
        self._cleanup_map()
        self.reject()

    def accept(self):
        self._cleanup_map()
        super().accept()

    def reject(self):
        self._cleanup_map()
        super().reject()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _step_style(is_current: bool, is_done: bool) -> str:
        if is_current:
            return (
                "background-color: #8e44ad; color: white; "
                "border-radius: 4px; padding: 4px 8px; font-weight: bold;"
            )
        if is_done:
            return (
                "background-color: #27ae60; color: white; "
                "border-radius: 4px; padding: 4px 8px;"
            )
        return (
            "background-color: #555; color: #aaa; "
            "border-radius: 4px; padding: 4px 8px;"
        )
