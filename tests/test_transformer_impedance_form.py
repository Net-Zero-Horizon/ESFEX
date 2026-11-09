"""The transformer form keeps impedances above 1 p.u.

Transformer impedance is on the system base (z_own × S_base / S_rated), so a
small unit is often above 1 p.u.; the form's 0–1 range clamped it to 1.0.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from esfex.visualization.data.gui_model import GuiModel, GuiTransformer
from esfex.visualization.panels.transformer_form import TransformerForm


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_form_shows_small_transformer_impedance(qapp):
    # 6 % on a 5 MVA unit, on a 100 MVA system base
    model = GuiModel()
    model.state.transformers.append(GuiTransformer(
        name="T1", from_bus="", to_bus="", from_voltage_kv=110.0,
        to_voltage_kv=33.0, rated_power_mva=5.0, impedance_pu=1.2))
    form = TransformerForm(model)
    form.load_element("0")
    assert form._impedance.value() == pytest.approx(1.2)
