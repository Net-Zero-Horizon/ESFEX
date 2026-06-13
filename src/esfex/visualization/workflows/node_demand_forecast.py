"""Single-node demand-forecast dialog.

Launched from the node attributes panel ("Forecast" button). It hosts the Grid
Builder's demand step (:class:`GridMappingDemandStep`) scoped to one node via
``set_single_node``, so the user can forecast — and, when the node already has
≥2 demand-carrying buses, distribute — demand for that node without running the
whole Grid Builder wizard.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QPushButton,
    QVBoxLayout,
)


class NodeDemandForecastDialog(QDialog):
    """Forecast (and optionally distribute) demand for a single node."""

    def __init__(self, model, node_index: int, parent=None):
        super().__init__(parent)
        node = model.get_node(node_index)
        name = node.name if node is not None else f"node {node_index}"
        self.setWindowTitle(f"Demand forecast — {name}")
        self.resize(900, 820)

        # Import here to avoid a heavy import at module load / circular imports.
        from esfex.visualization.workflows.grid_mapping_steps import (
            GridMappingDemandStep,
        )

        self._step = GridMappingDemandStep(model=model, map_widget=None)
        self._step.set_single_node(model, node_index)

        layout = QVBoxLayout(self)
        layout.addWidget(self._step, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        # Stop any in-flight worker (fetch / forecast / distribute) on close.
        self.finished.connect(self._cleanup)

    def _cleanup(self, *_):
        for fn in ("cancel_all", "cleanup_map"):
            try:
                getattr(self._step, fn)()
            except Exception:
                pass
