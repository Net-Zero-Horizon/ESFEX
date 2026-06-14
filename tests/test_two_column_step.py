"""Tests for the TwoColumnStep wizard container."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLabel, QWidget  # noqa: E402

from esfex.visualization.workflows._two_column_step import (  # noqa: E402
    TwoColumnStep,
)


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


def test_widget_columns(app):
    left = _Valid(True, "L")
    right = QLabel("output-only")  # no is_valid
    step = TwoColumnStep(left, right)
    assert step.children_steps == [left, right]
    # right has no is_valid → does not block
    assert step.is_valid() is True


def test_is_valid_is_and_of_children(app):
    step_bad = TwoColumnStep(_Valid(True, "a"), _Valid(False, "b"))
    assert step_bad.is_valid() is False
    step_ok = TwoColumnStep(_Valid(True, "a"), _Valid(True, "c"))
    assert step_ok.is_valid() is True


def test_list_column_stacks_and_flattens(app):
    a = _Valid(True, "a")
    b = _Valid(True, "b")
    c = _Valid(True, "c")
    step = TwoColumnStep([a, b], c)  # left column stacks two
    assert step.children_steps == [a, b, c]
    assert step.is_valid() is True
    b._ok = False
    assert step.is_valid() is False


def test_on_enter_forwards_to_all(app):
    a = _Valid(True, "a")
    b = _Valid(True, "b")
    plain = QLabel("no on_enter")
    step = TwoColumnStep([a, plain], b)
    step.on_enter()
    assert a.entered and b.entered  # plain widget ignored, no crash


def test_single_item_list_column(app):
    a = _Valid(True, "a")
    b = _Valid(True, "b")
    step = TwoColumnStep([a], b)  # one-item list collapses to the widget
    assert step.children_steps == [a, b]
    assert step.is_valid() is True
