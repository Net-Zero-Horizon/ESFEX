"""The Grid Builder's steps fit the wizard in every language.

An unwrapped intro label and rows of four check boxes set a step's minimum
width wider than the wizard (step 2: 1245 px in English; step 1: 834 px in
Spanish), so the page scrolled sideways and the build progress bar ran past
the window's edge.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QApplication, QScrollArea  # noqa: E402

from esfex.visualization.i18n import init_i18n  # noqa: E402

# The wizard's usual width leaves about 760 px for a step's page.
_PAGE_WIDTH = 760


@pytest.fixture(scope="module")
def _app():
    app = QApplication.instance() or QApplication([])
    yield app
    init_i18n("en")


@pytest.mark.parametrize("lang", ["en", "es", "fr", "pt", "ja"])
def test_every_step_fits_the_wizard(_app, lang):
    init_i18n(lang)
    from esfex.visualization.data.gui_model import GuiModel
    from esfex.visualization.workflows.grid_mapping_wizard import GridMappingWizard
    w = GridMappingWizard(None, GuiModel())
    try:
        for i, step in enumerate(w._steps):
            for sa in step.findChildren(QScrollArea):
                width = sa.widget().minimumSizeHint().width()
                assert width <= _PAGE_WIDTH, (lang, i + 1, width)
    finally:
        w.deleteLater()
