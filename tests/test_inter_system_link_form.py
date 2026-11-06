"""The inter-system link form edits the fuel of a fuel-route link.

It had no fuel field, so a fuel route drawn between two systems could never
say what it carries.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from esfex.visualization.data.gui_model import GuiFuel, GuiModel  # noqa: E402
from esfex.visualization.panels.inter_system_link_form import InterSystemLinkForm  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_fuel_row_only_for_fuel_routes(qapp):
    model = GuiModel()
    model.state.name = "A"
    model.state.fuels["Diesel"] = GuiFuel(fuel_id="Diesel", name="Diesel")
    fuel_id = model.add_inter_system_link("fuel_route", "A", "B", 0, 0)
    line_id = model.add_inter_system_link("transmission", "A", "B", 1, 0, capacity_mw=50.0)
    form = InterSystemLinkForm(model)
    layout = form._props_layout

    form.load_element(fuel_id)
    assert layout.isRowVisible(form._fuel)
    assert [form._fuel.itemText(i) for i in range(form._fuel.count())] == ["Diesel"]
    form._fuel.setCurrentText("Diesel")
    assert model.inter_system_links[0].fuel == "Diesel"

    form.load_element(line_id)
    assert not layout.isRowVisible(form._fuel)
    assert model.inter_system_links[1].fuel == ""
