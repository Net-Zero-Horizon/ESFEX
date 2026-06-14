"""Tests for the WorkflowStep wizard container (rows + scroll)."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QLabel,
    QScrollArea,
    QWidget,
)

from esfex.visualization.workflows._workflow_step import WorkflowStep  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class _Valid(QWidget):
    def __init__(self, ok, marker):
        super().__init__()
        self._ok = ok
        self.entered = False
        self.marker = marker

    def is_valid(self):
        return self._ok

    def on_enter(self):
        self.entered = True


def test_single_and_tuple_rows_flatten(app):
    a = _Valid(True, "a")
    b = _Valid(True, "b")
    c = _Valid(True, "c")
    step = WorkflowStep([a, (b, c)])  # one full-width row + one two-column row
    assert step.children_steps == [a, b, c]


def test_has_vertical_only_scroll_area(app):
    step = WorkflowStep([_Valid(True, "a")])
    scroll = step.findChild(QScrollArea)
    assert scroll is not None
    assert scroll.widgetResizable()
    assert (
        scroll.horizontalScrollBarPolicy()
        == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )


def test_is_valid_is_and_of_children(app):
    assert WorkflowStep([_Valid(True, "a"), _Valid(True, "b")]).is_valid() is True
    assert WorkflowStep([_Valid(True, "a"), _Valid(False, "b")]).is_valid() is False
    # a tuple row participates too
    assert WorkflowStep([(_Valid(True, "a"), _Valid(False, "b"))]).is_valid() is False


def test_widget_without_is_valid_does_not_block(app):
    step = WorkflowStep([_Valid(True, "a"), QLabel("output-only")])
    assert step.is_valid() is True


def test_on_enter_forwards_to_all(app):
    a = _Valid(True, "a")
    b = _Valid(True, "b")
    plain = QLabel("no on_enter")
    step = WorkflowStep([a, (plain, b)])
    step.on_enter()
    assert a.entered and b.entered  # plain widget ignored, no crash
