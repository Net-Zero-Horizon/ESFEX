"""Container that lays out a consolidated wizard step as a vertical stack of rows.

A *row* is either a single widget (full width) or a 2-tuple rendered as two equal
side-by-side columns. The whole stack lives in a vertical-only ``QScrollArea`` so
nothing is squashed and the window never overflows horizontally — heavy steps
simply gain a vertical scrollbar.

Use two-column rows only for related, chart-free panels small enough to sit side
by side; put wide panels (tables, matplotlib charts) on their own full-width row.
The wrapped step widgets are reused unchanged; the container only forwards the
wizard lifecycle methods it needs (``is_valid``, ``on_enter``) and exposes the
flattened child list.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


class WorkflowStep(QWidget):
    """Vertical, scroll-friendly arrangement of wizard panels.

    ``rows`` is a list whose entries are either a single ``QWidget`` (one
    full-width row) or a ``(left, right)`` tuple/list (one two-column row).
    """

    def __init__(self, rows, parent=None):
        super().__init__(parent)
        self._children: list[QWidget] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll)

        inner = QWidget()
        vbox = QVBoxLayout(inner)
        vbox.setContentsMargins(0, 0, 0, 0)
        for row in rows:
            if isinstance(row, (list, tuple)):
                hbox = QHBoxLayout()
                for w in row:
                    hbox.addWidget(w, 1)  # equal-width columns
                    self._children.append(w)
                vbox.addLayout(hbox)
            else:
                vbox.addWidget(row)
                self._children.append(row)
        vbox.addStretch()
        scroll.setWidget(inner)

    @property
    def children_steps(self) -> list[QWidget]:
        """Flattened list of the wrapped step widgets, in row order."""
        return list(self._children)

    def is_valid(self) -> bool:
        return all(
            c.is_valid() for c in self._children if hasattr(c, "is_valid")
        )

    def on_enter(self):
        for c in self._children:
            if hasattr(c, "on_enter"):
                c.on_enter()
