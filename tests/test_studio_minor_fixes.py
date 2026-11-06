"""Minor Studio fixes (#93).

* The script editor runs the current tab on Ctrl+Enter (the Run tooltip
  announced it, nothing was bound).
* The ``auto_indent`` editor preference has an effect.
* A fuel route trace takes waypoints from clicks on empty map space.
* Switching the SLD Analysis off gives the results time controls back.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def editor(qapp):
    from esfex.visualization.panels.script_editor import ScriptEditor

    w = ScriptEditor()
    yield w
    w.deleteLater()


def test_ctrl_enter_runs_script(editor):
    runs = []
    editor.runScript.connect(lambda src, label: runs.append(src))
    ed = editor._current_editor()
    ed.setPlainText("x = 1")
    QTest.keyClick(ed, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert runs == ["x = 1"]
    assert ed.toPlainText() == "x = 1"  # no newline inserted


def _type_enter(ed, text):
    ed.setPlainText(text)
    ed.moveCursor(QTextCursor.MoveOperation.End)
    QTest.keyClick(ed, Qt.Key.Key_Return)
    return ed.toPlainText()


def test_auto_indent_keeps_and_adds_indent(editor):
    ed = editor._current_editor()
    assert _type_enter(ed, "    x = 1") == "    x = 1\n    "
    assert _type_enter(ed, "for i in r:") == "for i in r:\n    "


def test_auto_indent_preference_off(editor):
    editor.apply_preferences({"auto_indent": False})
    ed = editor._current_editor()
    assert _type_enter(ed, "    x = 1") == "    x = 1\n"
    # New tabs get the stored preference too.
    editor._add_tab("t2")
    assert _type_enter(editor._current_editor(), "    y = 2") == "    y = 2\n"


def test_fuel_route_map_click_adds_waypoint():
    js = (Path(__file__).resolve().parents[1] / "src" / "esfex" / "visualization"
          / "resources" / "map_controller.js").read_text(encoding="utf-8")
    m = re.search(r"\} else if \((currentMode === 'add_line'[^)]*)\) \{\s*\n\s*(//[^\n]*\n\s*)?_onMapClickForTrace",
                  js)
    assert m and "'add_fuel_route'" in m.group(1), "map click must add fuel route waypoints"


def test_analysis_off_restores_results_controls(qapp):
    from esfex.visualization.main_window import MainWindow

    calls = []

    class _W:
        def __init__(self, name):
            self.name = name
            self.enabled = None

        def setEnabled(self, v):
            self.enabled = v

        def setVisible(self, v):
            pass

        def blockSignals(self, v):
            pass

        def clear(self):
            pass

        def addItem(self, *a):
            pass

        def disconnect(self, *a):
            pass

    widgets = {n: _W(n) for n in (
        "_sld_btn_load", "_sld_year_combo", "_sld_hour_slider", "_sld_hour_spin",
        "_sld_btn_play", "_sld_speed_combo", "_sld_btn_export", "_sld_btn_clear",
        "_sld_contingency_combo", "_analysis_scroll")}
    panel = SimpleNamespace(
        hide_pf_status=lambda: None,
        analysisModeChanged=SimpleNamespace(disconnect=lambda *a: None),
    )
    fake = SimpleNamespace(
        **widgets,
        _analysis_panel=panel,
        _sld_results_loader=SimpleNamespace(num_nodes=1),
        model=SimpleNamespace(state=SimpleNamespace(
            nodes=[], buses={}, generators={}, batteries={}, transmission_lines=[],
            transformers=[])),
        sld_widget=SimpleNamespace(
            clear_operational_data=lambda: None,
            clear_contingency_data=lambda: None),
        _restore_properties_panel=lambda: None,
        _on_analysis_mode_changed=lambda *a: None,
        _update_sld_ops_overlay=lambda: calls.append("overlay"),
        _populate_contingency_combo=lambda: calls.append("combo"),
    )
    fake._enable_sld_results_controls = MainWindow._enable_sld_results_controls.__get__(fake)
    MainWindow._on_sld_analysis_toggle(fake, False)

    for n in ("_sld_year_combo", "_sld_hour_slider", "_sld_hour_spin",
              "_sld_btn_play", "_sld_speed_combo"):
        assert widgets[n].enabled is True, n
    assert calls == ["overlay", "combo"]
