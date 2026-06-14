"""Solar PV Potential Assessment wizard dialog.

Two-phase multi-step wizard:

Phase A — Solar PV Resource Assessment & MCDA:
  1. Define domain (rectangle on map or manual coordinates)
  2. Configure module (CEC database) and assessment parameters
  3. Configure MCDA criteria and weighting method
  4. Run analysis (ERA5 + DEM + LULC + MCDA)
  5. View results and generate development zones

Phase B — Advanced Analysis:
  6. Solar characterization (GHI patterns, diurnal, seasonal, temperature)
  7. Financial analysis (LCOE, NPV, IRR, sensitivity)
  8. Array / shading analysis (GCR, inter-row shading, bifacial gain)
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

from esfex.visualization.workflows.solar_pv_steps import (
    SolarPVAnalysisStep,
    SolarPVConfigStep,
    SolarPVCriteriaStep,
    SolarPVDomainStep,
    SolarPVResultsStep,
)

from esfex.visualization.workflows.solar_pv_advanced_steps import (
    SolarArrayStep,
    SolarAvailabilityStep,
    SolarCharacterizationStep,
    SolarFinancialStep,
)
from esfex.visualization.workflows._two_column_step import TwoColumnStep

# Consolidated two-column steps
_STEP_NAMES = [
    lambda: tr("wizard_solar_pv.step_setup"),
    lambda: tr("wizard_solar_pv.step_suitability"),
    lambda: tr("wizard_solar_pv.step_results_econ"),
    lambda: tr("wizard_solar_pv.step_refinement"),
]

# Index of the container that holds the (long-running) Analysis step.
_ANALYSIS_STEP = 1


class SolarPVWizard(QDialog):
    """Multi-step wizard for solar PV assessment with MCDA and advanced analysis."""

    def __init__(self, map_widget, model=None, parent=None,
                 geo_assets_provider=None):
        super().__init__(parent)
        self._geo_assets_provider = geo_assets_provider
        self.setWindowTitle(tr("wizard_solar_pv.title"))
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
        self._step_domain = SolarPVDomainStep(
            self._map_widget, geo_assets_provider=self._geo_assets_provider)
        self._step_config = SolarPVConfigStep()
        self._step_criteria = SolarPVCriteriaStep()
        self._step_analysis = SolarPVAnalysisStep()
        self._step_results = SolarPVResultsStep(self._map_widget, self._model)
        self._step_characterization = SolarCharacterizationStep()
        self._step_financial = SolarFinancialStep()
        self._step_array = SolarArrayStep()
        self._step_availability = SolarAvailabilityStep(model=self._model)

        # The analysis pulls its inputs live from the sibling Criteria + the
        # prior-container Domain/Config at Run time (consolidated layout).
        self._step_analysis.set_input_provider(self._analysis_inputs)

        # Four two-column containers
        self._steps = [
            TwoColumnStep(self._step_domain, self._step_config),
            TwoColumnStep(self._step_criteria, self._step_analysis),
            TwoColumnStep(self._step_results, self._step_financial),
            TwoColumnStep(
                [self._step_characterization, self._step_array],
                self._step_availability,
            ),
        ]
        for step in self._steps:
            self._stack.addWidget(step)

        layout.addWidget(self._stack, 1)

        # Button row
        btn_layout = QHBoxLayout()

        self._btn_cancel = QPushButton(tr("wizard_solar_pv.cancel"))
        self._btn_cancel.clicked.connect(self._on_cancel)
        btn_layout.addWidget(self._btn_cancel)

        btn_layout.addStretch()

        self._btn_back = QPushButton(tr("wizard_solar_pv.back"))
        self._btn_back.clicked.connect(self._go_back)
        btn_layout.addWidget(self._btn_back)

        self._btn_next = QPushButton(tr("wizard_solar_pv.next"))
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
            capacity_mw = summary.total_capacity_mw if summary else 10.0
            cf_avg = summary.cf_avg if summary else 0.20
            workers = config.effective_workers if config else 0
            self._step_financial.set_inputs(capacity_mw, cf_avg, workers)
        elif self._current_step == 2:
            # Results & Economics → Refinement: feed Characterization, Array, Availability.
            summary = self._step_analysis.get_summary()
            config = self._step_config.get_config()
            hourly_data = summary.hourly_data if summary else None
            self._step_characterization.set_inputs(hourly_data, summary, config)

            bounds = self._step_domain.get_bounds()
            latitude = (bounds[0] + bounds[2]) / 2.0 if bounds else 0.0
            tilt = config.tilt if config.orientation == "custom" else abs(latitude)
            module = self._step_config.get_module_spec()
            is_bifacial = module.bifacial if module else False
            workers = config.effective_workers if config else 0
            self._step_array.set_inputs(
                latitude=latitude,
                tilt=tilt,
                capacity_factor=summary.cf_avg if summary else 0.20,
                capacity_mw=summary.total_capacity_mw if summary else 10.0,
                is_bifacial=is_bifacial,
                max_workers=workers,
            )
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
            self._btn_next.setText(tr("wizard_solar_pv.next"))
            try:
                self._btn_next.clicked.disconnect()
            except RuntimeError:
                pass
            self._btn_next.clicked.connect(self._go_next)
            self._btn_next.setEnabled(self._step_analysis.is_valid())
        else:
            self._btn_next.setText(tr("wizard_solar_pv.next"))
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
        """Remove all temporary solar PV overlays from the map."""
        self._map_widget.clear_solar_pv_domain()
        self._map_widget.clear_solar_pv_results()
        self._map_widget.clear_solar_pv_dev_zones()
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
                "background-color: #e67e22; color: white; "
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
