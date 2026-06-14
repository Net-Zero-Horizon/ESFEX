"""Solar Rooftop Analysis wizard dialog.

Two-phase multi-step wizard:

Phase A — Building Potential Analysis:
  1. Define domain (rectangle on map or manual coordinates)
  2. Fetch building footprints and solar resource data
  3. Configure panel/roof/shading parameters
  4. Run analysis
  5. View and export results

Phase B — Adoption Modeling & Integration:
  6. Macroeconomic data (auto-fetch + manual edit)
  7. Adoption modeling (4 methods)
  8. Scenario comparison (chart + selection)
  9. Model integration (apply to ESFEX or export)
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

from esfex.visualization.workflows.solar_rooftop_steps import (
    AnalysisStep,
    ConfigStep,
    DataSourcesStep,
    DomainStep,
    ResultsStep,
)

from esfex.visualization.workflows.solar_adoption_steps import (
    AdoptionModelStep,
    IntegrationStep,
    MacroDataStep,
    ScenarioComparisonStep,
)
from esfex.visualization.workflows._workflow_step import WorkflowStep

# Consolidated two-column steps
_STEP_NAMES = [
    lambda: tr("wizard_solar.step_domain_data"),
    lambda: tr("wizard_solar.step_config_analysis"),
    lambda: tr("wizard_solar.step_results_macro"),
    lambda: tr("wizard_solar.step_adoption_integ"),
]

# Index of the container that holds the (long-running) Analysis step.
_ANALYSIS_STEP = 1


class SolarRooftopWizard(QDialog):
    """Multi-step wizard for solar rooftop potential analysis and adoption modeling."""

    def __init__(self, map_widget, model=None, parent=None,
                 geo_assets_provider=None):
        super().__init__(parent)
        self._geo_assets_provider = geo_assets_provider
        self.setWindowTitle(tr("wizard_solar.title"))
        self.setMinimumSize(750, 580)
        self.resize(950, 700)
        # Non-modal so the user can interact with the map while the wizard is open
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
        self._step_domain = DomainStep(
            self._map_widget, geo_assets_provider=self._geo_assets_provider)
        self._step_data = DataSourcesStep()
        self._step_config = ConfigStep()
        self._step_analysis = AnalysisStep()
        self._step_results = ResultsStep(self._map_widget)
        self._step_macro = MacroDataStep()
        self._step_adoption = AdoptionModelStep()
        self._step_compare = ScenarioComparisonStep()
        self._step_integration = IntegrationStep(model=self._model)

        # Intra-container wiring (consolidated layout):
        #  - Domain feeds DataSources bounds/polygon live (same container).
        #  - Analysis pulls buildings/solar + sibling Config at Run time.
        #  - Adoption finishing populates the sibling Scenario Comparison.
        #  - Integration pulls the live scenario selection at Apply time.
        self._step_domain.domainChanged.connect(self._sync_data_bounds)
        self._step_analysis.set_input_provider(self._analysis_inputs)
        self._step_adoption.modelsFinished.connect(self._populate_compare)
        self._step_integration.set_input_provider(self._integration_inputs)

        # Four consolidated steps. Two columns only for the light, chart-free
        # Config | Analysis pair; wide panels (data fetch, results, macro,
        # adoption charts) get full-width rows and scroll vertically when tall.
        self._steps = [
            WorkflowStep([self._step_domain, self._step_data]),
            WorkflowStep([(self._step_config, self._step_analysis)]),
            WorkflowStep([self._step_results, self._step_macro]),
            WorkflowStep([
                self._step_adoption,
                self._step_compare,
                self._step_integration,
            ]),
        ]
        for step in self._steps:
            self._stack.addWidget(step)

        layout.addWidget(self._stack, 1)

        # Button row
        btn_layout = QHBoxLayout()

        self._btn_cancel = QPushButton(tr("wizard_solar.cancel"))
        self._btn_cancel.clicked.connect(self._on_cancel)
        btn_layout.addWidget(self._btn_cancel)

        btn_layout.addStretch()

        self._btn_back = QPushButton(tr("wizard_solar.back"))
        self._btn_back.clicked.connect(self._go_back)
        btn_layout.addWidget(self._btn_back)

        self._btn_next = QPushButton(tr("wizard_solar.next"))
        self._btn_next.clicked.connect(self._go_next)
        btn_layout.addWidget(self._btn_next)

        layout.addLayout(btn_layout)

        # Connect step signals
        self._step_analysis.analysisFinished.connect(self._on_analysis_finished)
        self._step_adoption.modelsFinished.connect(self._on_models_finished)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _sync_data_bounds(self):
        """Keep DataSources' bounds/polygon in step with the live domain."""
        self._step_data.set_bounds(self._step_domain.get_bounds())
        self._step_data.set_polygon(self._step_domain.get_polygon())

    def _analysis_inputs(self):
        """Live inputs for the Analysis step (set_inputs args tuple)."""
        return (
            self._step_data.get_buildings(),
            self._step_data.get_solar_data(),
            self._step_config.get_config(),
        )

    def _populate_compare(self):
        """Adoption finished → feed the sibling Scenario Comparison."""
        self._step_compare.set_curves(
            self._step_adoption.get_curves(),
            validation_data=self._step_adoption.get_validation_data(),
            max_potential_mw=self._step_adoption.get_max_potential_mw(),
        )

    def _integration_inputs(self):
        """Live inputs for Integration (set_inputs kwargs)."""
        return dict(
            selected_curve=self._step_compare.get_selected_curve(),
            all_curves=self._step_compare.get_all_curves(),
            macro=self._step_macro.get_macro_data(),
            analysis_summary=self._step_analysis.get_summary(),
        )

    def _go_next(self):
        step = self._steps[self._current_step]
        if not step.is_valid():
            return

        # Push data across container boundaries (same calls as before, grouped).
        if self._current_step == 1:
            # Config & Analysis → Results & Macro: feed Results + Macro bounds.
            self._step_results.set_results(
                self._step_analysis.get_summary(),
                self._step_data.get_buildings(),
            )
            self._step_macro.set_bounds(self._step_domain.get_bounds())
        elif self._current_step == 2:
            # Results & Macro → Adoption & Integration: feed Adoption inputs.
            summary = self._step_analysis.get_summary()
            max_mw = summary.total_capacity_kwp / 1000.0 if summary else 10.0
            positions = self._get_building_positions()
            self._step_adoption.set_inputs(
                self._step_macro.get_macro_data(),
                max_mw,
                building_positions=positions,
            )

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
            # Config & Analysis: only enable Next once the analysis has run.
            self._btn_next.setText(tr("wizard_solar.next"))
            try:
                self._btn_next.clicked.disconnect()
            except RuntimeError:
                pass
            self._btn_next.clicked.connect(self._go_next)
            self._btn_next.setEnabled(self._step_analysis.is_valid())
        else:
            self._btn_next.setText(tr("wizard_solar.next"))
            try:
                self._btn_next.clicked.disconnect()
            except RuntimeError:
                pass
            self._btn_next.clicked.connect(self._go_next)
            self._btn_next.setEnabled(True)

    def _on_analysis_finished(self):
        """Enable Next button when analysis completes."""
        self._btn_next.setEnabled(True)

    def _on_models_finished(self):
        """Enable Next button when adoption models complete."""
        self._btn_next.setEnabled(True)

    def _get_building_positions(self):
        """Extract building centroid positions as numpy array for ABM."""
        try:
            import numpy as np

            buildings = self._step_data.get_buildings()
            if buildings is None or buildings.empty:
                return None
            centroids = buildings.geometry.centroid
            coords = np.column_stack([centroids.y, centroids.x])
            return coords
        except Exception:
            return None

    def closeEvent(self, event):
        from esfex.visualization.workflows._wizard_utils import cleanup_wizard
        cleanup_wizard(self)
        super().closeEvent(event)

    def _cleanup_map(self):
        """Remove all temporary rooftop overlays from the map."""
        self._map_widget.clear_rooftop_domain()
        self._map_widget.clear_rooftop_results()
        self._map_widget.disable_rectangle_draw()

    def _on_cancel(self):
        self._step_macro.cancel_all()
        self._cleanup_map()
        self.reject()

    def accept(self):
        self._cleanup_map()
        super().accept()

    def reject(self):
        self._step_macro.cancel_all()
        self._cleanup_map()
        super().reject()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _step_style(is_current: bool, is_done: bool) -> str:
        if is_current:
            return (
                "background-color: #2980b9; color: white; "
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
