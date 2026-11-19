"""No raw translation key on screen.

Keys built at run time (``tr(f"wizard_financial.{k}")``, tab and step
lists) are invisible to the static audit in test_i18n_coverage: a missing
one shows as ``section.key`` in the window. This opens the Studio's
windows, wizards and element forms in two languages and checks every
text a widget shows.
"""
from __future__ import annotations

import re

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import (  # noqa: E402
    QAbstractButton, QApplication, QComboBox, QGroupBox, QLabel, QLineEdit,
    QTableWidget, QTabWidget, QTreeWidget, QWidget,
)

import json  # noqa: E402
from pathlib import Path  # noqa: E402

from esfex.visualization.i18n import init_i18n  # noqa: E402

# A raw key is "<section>.<key>" with a real section of the translation files,
# anywhere in the text ("3. grid_builder.step_demand" included).
_SECTIONS = sorted(json.loads((Path(__file__).resolve().parent.parent / "src/esfex/"
                               "visualization/translations/en.json").read_text(
                                   encoding="utf-8")), key=len, reverse=True)
_RAW_KEY = re.compile(r"(?<![\w.])(?:" + "|".join(map(re.escape, _SECTIONS))
                      + r")\.[a-z0-9_]+(?![\w(])")


def _texts(root: QWidget):
    """(widget class, text) of everything a window shows."""
    widgets = [root] + root.findChildren(QWidget)
    for w in widgets:
        name = type(w).__name__
        yield name, w.windowTitle() if w is root else ""
        yield name, w.toolTip()
        if isinstance(w, QLabel):
            yield name, w.text()
        elif isinstance(w, QAbstractButton):
            yield name, w.text()
        elif isinstance(w, QGroupBox):
            yield name, w.title()
        elif isinstance(w, QLineEdit):
            yield name, w.placeholderText()
        elif isinstance(w, QComboBox):
            for i in range(w.count()):
                yield name, w.itemText(i)
        elif isinstance(w, QTabWidget):
            for i in range(w.count()):
                yield name, w.tabText(i)
        elif isinstance(w, QTableWidget):
            for i in range(w.columnCount()):
                item = w.horizontalHeaderItem(i)
                if item is not None:
                    yield name, item.text()
        elif isinstance(w, QTreeWidget):
            hdr = w.headerItem()
            for i in range(hdr.columnCount() if hdr else 0):
                yield name, hdr.text(i)


def _model():
    from esfex.visualization.data.gui_model import GuiModel
    return GuiModel()


def _windows():
    from esfex.visualization.panels import (
        battery_form, bus_form, generator_form, global_settings_form,
        line_form, system_form, technology_form,
    )
    from esfex.visualization.workflows.financial_wizard import FinancialWizard
    from esfex.visualization.workflows.grid_mapping_wizard import GridMappingWizard
    from esfex.visualization.workflows.otec_studio.window import OTECStudioWindow
    from esfex.visualization.workflows.risk_wizard import RiskWorkbench
    return {
        "grid_builder": lambda: GridMappingWizard(None, _model()),
        "financial": FinancialWizard,
        "risk": lambda: RiskWorkbench(None, _model()),
        "otec": OTECStudioWindow,
        "bus_form": lambda: bus_form.BusForm(_model()),
        "generator_form": lambda: generator_form.GeneratorForm(_model()),
        "battery_form": lambda: battery_form.BatteryForm(_model()),
        "line_form": lambda: line_form.LineForm(_model()),
        "technology_form": lambda: technology_form.TechnologyForm(_model()),
        "system_form": lambda: system_form.SystemForm(_model()),
        "global_settings_form": lambda: global_settings_form.GlobalSettingsForm(_model()),
    }


@pytest.fixture(scope="module")
def _app():
    app = QApplication.instance() or QApplication([])
    yield app
    init_i18n("en")


@pytest.mark.parametrize("lang", ["es", "ja"])
@pytest.mark.parametrize("window", sorted(_windows()))
def test_no_raw_key_on_screen(_app, lang, window):
    init_i18n(lang)
    w = _windows()[window]()
    try:
        raw = sorted({(cls, t) for cls, t in _texts(w) if t and _RAW_KEY.search(t)})
        assert not raw, f"raw keys shown in {window} ({lang}): {raw}"
    finally:
        w.deleteLater()
