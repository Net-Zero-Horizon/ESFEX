"""`_resize_dialog` scales a dialog's natural size by per-axis factors."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QInputDialog

from esfex.visualization.main_window import _resize_dialog


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _input_dialog():
    d = QInputDialog()
    d.setInputMode(QInputDialog.TextInput)
    d.setWindowTitle("New System")
    d.setLabelText("Enter a name for the new system:")
    d.adjustSize()
    return d


def test_widens_by_factor(qapp):
    d = _input_dialog()
    base_w = d.sizeHint().width()
    _resize_dialog(d, width_factor=1.44)
    assert d.size().width() == pytest.approx(round(base_w * 1.44), abs=2)
    # Minimum width is set so the wider size sticks once shown.
    assert d.minimumSize().width() == pytest.approx(round(base_w * 1.44), abs=2)


def test_scales_height_independently(qapp):
    d = _input_dialog()
    base = d.sizeHint()
    _resize_dialog(d, width_factor=1.44, height_factor=1.2)
    assert d.size().width() == pytest.approx(round(base.width() * 1.44), abs=2)
    assert d.minimumSize().height() == pytest.approx(
        round(base.height() * 1.2), abs=2)


def test_factor_below_one_never_shrinks(qapp):
    d = _input_dialog()
    base = d.sizeHint()
    _resize_dialog(d, width_factor=0.5, height_factor=0.5)  # clamped to 1.0
    assert d.size().width() >= base.width()
    assert d.size().height() >= base.height()
