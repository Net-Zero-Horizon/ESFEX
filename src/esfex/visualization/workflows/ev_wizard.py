"""EV & V2G Assessment Workflow wizard dialog.

Two-phase wizard: Phase A (Fleet Assessment, steps 1-5) and
Phase B (V2G Analysis & Grid Integration, steps 6-9).
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from esfex.visualization.i18n import tr

logger = logging.getLogger(__name__)

# -- Phase / step definitions ----------------------------------------

# Consolidated two-column steps
_STEP_NAMES = [
    ("wizard_ev.step_domain_macro", "Domain & Macro"),
    ("wizard_ev.step_adoption_results", "Adoption & Results"),
    ("wizard_ev.step_charging_v2g", "Charging & V2G"),
    ("wizard_ev.step_grid_integ", "Grid & Integration"),
]

# Index of the container that holds the (long-running) Adoption step.
_ADOPTION_STEP = 1

_COLOR_A = "#8e44ad"   # purple
_COLOR_DONE = "#27ae60"
_COLOR_PENDING = "#555"


class EVWizardDialog(QDialog):
    """Main wizard dialog for EV & V2G Assessment."""

    def __init__(self, map_widget, model=None, parent=None, geo_assets_provider=None):
        super().__init__(parent)
        self._map_widget = map_widget
        self._model = model
        self._geo_assets_provider = geo_assets_provider
        self._current_step = 0
        self._total_steps = len(_STEP_NAMES)

        self.setWindowTitle(tr("wizard_ev.title"))
        self.resize(900, 700)
        self.setMinimumSize(780, 550)

        main_layout = QVBoxLayout(self)

        # -- Single 4-dot step indicator bar --
        self._step_labels: list[QLabel] = []
        bar = QHBoxLayout()
        for i, (key, fallback) in enumerate(_STEP_NAMES):
            text = tr(key)
            lbl = QLabel(f"  {i+1}. {text if text != key else fallback}  ")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            lbl.setFixedHeight(28)
            self._step_labels.append(lbl)
            bar.addWidget(lbl)
        main_layout.addLayout(bar)

        # -- Stacked widget for step pages --
        self._stack = QStackedWidget()
        main_layout.addWidget(self._stack, 1)

        # -- Create step widgets --
        self._create_steps()

        # -- Navigation buttons --
        nav = QHBoxLayout()
        self._btn_cancel = QPushButton(tr("wizard_common.cancel"))
        self._btn_cancel.clicked.connect(self._on_cancel)
        nav.addWidget(self._btn_cancel)

        nav.addStretch()

        self._btn_back = QPushButton(tr("wizard_common.back"))
        self._btn_back.clicked.connect(self._go_back)
        nav.addWidget(self._btn_back)

        self._btn_next = QPushButton(tr("wizard_common.next"))
        self._btn_next.clicked.connect(self._go_next)
        nav.addWidget(self._btn_next)

        main_layout.addLayout(nav)

        # Initial state
        self._update_indicators()
        self._update_buttons()

    def _create_steps(self):
        from esfex.visualization.workflows.ev_steps import (
            EVAdoptionModelStep,
            EVDomainStep,
            EVFleetResultsStep,
            EVMacroDataStep,
            EVScenarioSelectionStep,
        )
        from esfex.visualization.workflows.ev_advanced_steps import (
            EVChargingDemandStep,
            EVGridImpactStep,
            EVIntegrationStep,
            EVV2GPotentialStep,
        )

        # Phase A
        self._step_domain = EVDomainStep(
            self._map_widget, parent=self,
            geo_assets_provider=self._geo_assets_provider,
        )
        self._step_macro = EVMacroDataStep(parent=self)
        self._step_adoption = EVAdoptionModelStep(parent=self)
        self._step_results = EVFleetResultsStep(parent=self)
        self._step_scenario = EVScenarioSelectionStep(parent=self)

        # Phase B
        self._step_charging = EVChargingDemandStep(parent=self)
        self._step_v2g = EVV2GPotentialStep(parent=self)
        self._step_grid = EVGridImpactStep(parent=self)
        self._step_integration = EVIntegrationStep(model=self._model, parent=self)

        from esfex.visualization.workflows._two_column_step import TwoColumnStep

        # Intra-container wiring (consolidated layout):
        #  - Domain feeds Macro bounds live (same container).
        #  - Adoption finishing populates the sibling Results + Scenario Select.
        #  - V2G pulls the live fleet from sibling Charging at Run time.
        #  - Integration pulls the live Grid Impact result at Apply time.
        self._step_domain.domainChanged.connect(self._sync_macro_bounds)
        self._step_adoption.modelsFinished.connect(self._on_models_finished)
        self._step_adoption.modelsFinished.connect(self._populate_results)
        self._step_v2g.set_input_provider(self._v2g_fleet)
        self._step_integration.set_input_provider(self._integration_inputs)

        # Four two-column containers
        self._steps = [
            TwoColumnStep(self._step_domain, self._step_macro),
            TwoColumnStep(
                self._step_adoption,
                [self._step_results, self._step_scenario],
            ),
            TwoColumnStep(self._step_charging, self._step_v2g),
            TwoColumnStep(self._step_grid, self._step_integration),
        ]
        for step in self._steps:
            self._stack.addWidget(step)

    def _on_models_finished(self):
        """Re-enable navigation after adoption models complete."""
        self._btn_next.setEnabled(True)

    # -- Navigation --------------------------------------------------

    def _go_next(self):
        step = self._steps[self._current_step]
        if not step.is_valid():
            return

        # Propagate data to next step
        self._propagate_forward()

        if self._current_step < self._total_steps - 1:
            self._current_step += 1
            self._stack.setCurrentIndex(self._current_step)
            self._update_indicators()
            self._update_buttons()
        else:
            # Last step — close
            self.accept()

    def _go_back(self):
        if self._current_step > 0:
            self._current_step -= 1
            self._stack.setCurrentIndex(self._current_step)
            self._update_indicators()
            self._update_buttons()

    def _on_cancel(self):
        self._cleanup()
        self.reject()

    def _sync_macro_bounds(self):
        """Keep Macro's bounds in step with the live domain (same container)."""
        self._step_macro.set_bounds(self._step_domain.get_bounds())

    def _populate_results(self):
        """Adoption finished → feed the sibling Fleet Results + Scenario Select."""
        curves = self._step_adoption.get_curves()
        validation = self._step_adoption.get_validation_data()
        self._step_results.set_results(curves, validation)
        self._step_scenario.set_curves(curves, validation)

    def _v2g_fleet(self):
        """Live fleet for V2G (from the sibling Charging step)."""
        return self._step_charging.get_fleet_at_year()

    def _integration_inputs(self):
        """Live inputs for Integration (set_inputs args tuple)."""
        return (
            self._step_scenario.get_selected_curve(),
            self._step_domain.get_transport_context(),
            self._step_macro.get_ev_macro_data(),
            self._step_v2g.get_v2g_potential(),
            self._step_v2g.get_degradation(),
            self._step_grid.get_result(),
            self._step_charging.get_scenarios(),
        )

    def _propagate_forward(self):
        """Pass data across container boundaries (regrouped from 8 to 3)."""
        idx = self._current_step

        if idx == 0:
            # Domain & Macro → Adoption & Results: feed Adoption inputs.
            macro = self._step_macro.get_ev_macro_data()
            transport = self._step_domain.get_transport_context()
            self._step_adoption.set_inputs(macro, transport)

        elif idx == 1:
            # Adoption & Results → Charging & V2G: feed the selected curve.
            curve = self._step_scenario.get_selected_curve()
            self._step_charging.set_curve(curve)

        elif idx == 2:
            # Charging & V2G → Grid & Integration: feed Grid Impact inputs.
            scenarios = self._step_charging.get_scenarios()
            v2g = self._step_v2g.get_v2g_potential()
            base_demand = self._step_charging.get_base_demand()
            self._step_grid.set_inputs(scenarios, v2g, base_demand)

    # -- Visual indicators -------------------------------------------

    def _update_indicators(self):
        for i, lbl in enumerate(self._step_labels):
            if i < self._current_step:
                self._style_label(lbl, _COLOR_DONE, "white", bold=False)
            elif i == self._current_step:
                self._style_label(lbl, _COLOR_A, "white", bold=True)
            else:
                self._style_label(lbl, _COLOR_PENDING, "#aaa", bold=False)

    @staticmethod
    def _style_label(lbl: QLabel, bg: str, fg: str, bold: bool = False):
        weight = "bold" if bold else "normal"
        lbl.setStyleSheet(
            f"background-color: {bg}; color: {fg};"
            f"font-weight: {weight}; padding: 4px 8px;"
            f"border-radius: 4px; font-size: 11px;"
        )

    def _update_buttons(self):
        self._btn_back.setEnabled(self._current_step > 0)

        if self._current_step == self._total_steps - 1:
            self._btn_next.setText(tr("wizard_common.close"))
        else:
            self._btn_next.setText(tr("wizard_common.next"))

        # Disable Next while the adoption models run (Adoption & Results step)
        if self._current_step == _ADOPTION_STEP:
            worker = getattr(self._step_adoption, "_worker", None)
            if worker and worker.isRunning():
                self._btn_next.setEnabled(False)
            else:
                self._btn_next.setEnabled(True)
        else:
            self._btn_next.setEnabled(True)

    # -- Cleanup -----------------------------------------------------

    def _cleanup(self):
        """Cancel running fetchers/workers."""
        for step in self._steps:
            cancel = getattr(step, "cancel_all", None)
            if callable(cancel):
                cancel()

    def closeEvent(self, event):
        self._cleanup()
        super().closeEvent(event)
