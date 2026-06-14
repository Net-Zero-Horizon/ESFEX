"""Two-column container that pairs existing wizard step widgets side by side.

Workflow wizards used to show one step widget per screen. To compact a flow,
wrap two (or, rarely, three) self-contained step widgets in a
``TwoColumnStep``: a horizontal ``QSplitter`` gives a left and a right column,
and a column built from a list stacks its widgets in a vertical ``QSplitter``.

The wrapped step widgets are reused **unchanged** — the container only forwards
the wizard lifecycle methods it cares about:

- ``is_valid()`` → the AND of every child that defines ``is_valid`` (output-only
  panels without it never block navigation).
- ``on_enter()`` → forwarded to any child that defines it (used to push
  cross-container inputs when the container becomes the current page).
- ``children`` → the flattened list of wrapped step widgets.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QSplitter, QVBoxLayout, QWidget


def _build_column(content) -> QWidget:
    """Return a single widget, or a vertical splitter stacking a list of them."""
    if isinstance(content, (list, tuple)):
        if len(content) == 1:
            return content[0]
        col = QSplitter(Qt.Orientation.Vertical)
        col.setChildrenCollapsible(False)
        for w in content:
            col.addWidget(w)
        return col
    return content


class TwoColumnStep(QWidget):
    """Pair two columns of existing step widgets in a horizontal splitter."""

    def __init__(self, left, right, *, sizes=None, parent=None):
        super().__init__(parent)
        # Preserve order: left column children first, then right column.
        self._children: list[QWidget] = []
        for side in (left, right):
            if isinstance(side, (list, tuple)):
                self._children.extend(side)
            else:
                self._children.append(side)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.addWidget(_build_column(left))
        self._splitter.addWidget(_build_column(right))
        if sizes:
            self._splitter.setSizes(list(sizes))
        else:
            self._splitter.setSizes([1, 1])
        layout.addWidget(self._splitter)

    @property
    def children_steps(self) -> list[QWidget]:
        """Flattened list of the wrapped step widgets (left column first)."""
        return list(self._children)

    def is_valid(self) -> bool:
        return all(
            c.is_valid() for c in self._children if hasattr(c, "is_valid")
        )

    def on_enter(self):
        for c in self._children:
            if hasattr(c, "on_enter"):
                c.on_enter()
