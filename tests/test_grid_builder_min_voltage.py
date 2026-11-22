"""The Grid Builder's minimum voltage takes decimals.

It was a whole-number field, so a floor at a standard level such as 13.8 or
34.5 kV could not be entered (34.5 kV keeps Isla de la Juventud's grid; 34
also lets in 33 kV distribution).
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def step():
    QApplication.instance() or QApplication([])
    from esfex.visualization.data.gui_model import GuiModel
    from esfex.visualization.workflows.grid_mapping_wizard import GridMappingWizard
    w = GridMappingWizard(None, GuiModel())
    yield w._step_source_fetch
    w.deleteLater()


def test_min_voltage_takes_decimals(step):
    step._spin_min_voltage.setValue(34.5)
    assert step._spin_min_voltage.value() == 34.5
    assert step.get_config()["min_voltage_kv"] == 34.5


def test_capacity_floor_still_follows_the_voltage(step):
    step._min_cap_user_edited = False
    step._spin_min_voltage.setValue(110.0)
    assert step._spin_min_capacity.value() == 35.0
