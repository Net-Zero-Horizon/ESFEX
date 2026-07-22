"""Generator type (Renewable/Non-renewable) round-trips through the form in a
non-English UI. Regression for the "all generators get the last type" bug where
the combo used translated labels but the model stores English literals."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from esfex.visualization.i18n import init_i18n


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def es_ui():
    init_i18n("es")           # Spanish UI: labels differ from model literals
    yield
    init_i18n("en")


def _model_with_two_gens():
    from esfex.visualization.data.gui_model import GuiModel
    m = GuiModel()
    a = m.add_generator_instance(unit_key="u1", name="A", gen_type="Renewable",
                                 fuel="Wind", node=0, rated_power=1.0)
    b = m.add_generator_instance(unit_key="u2", name="B",
                                 gen_type="Non-renewable", fuel="Gas", node=0,
                                 rated_power=1.0)
    return m, a, b


def test_combo_not_sticky_across_generators(qapp, es_ui):
    from esfex.visualization.panels.generator_form import GeneratorForm
    m, a, b = _model_with_two_gens()
    f = GeneratorForm(m)
    f.load_element(a)
    assert f._gen_type.currentData() == "Renewable"
    f.load_element(b)                       # was "sticky" at A's value before
    assert f._gen_type.currentData() == "Non-renewable"
    f.load_element(a)
    assert f._gen_type.currentData() == "Renewable"


def test_writeback_stores_english_literal(qapp, es_ui):
    from esfex.visualization.panels.generator_form import GeneratorForm
    m, a, b = _model_with_two_gens()
    f = GeneratorForm(m)
    f.load_element(b)
    f._on_changed()
    assert m.state.generators[b].gen_type == "Non-renewable"  # not "No renovable"


def test_legacy_translated_value_normalized(qapp, es_ui):
    from esfex.visualization.panels.generator_form import GeneratorForm
    m, a, b = _model_with_two_gens()
    m.state.generators[a].gen_type = "No renovable"   # corrupted legacy value
    f = GeneratorForm(m)
    f.load_element(a)
    assert f._gen_type.currentData() == "Non-renewable"
    f._on_changed()
    assert m.state.generators[a].gen_type == "Non-renewable"
