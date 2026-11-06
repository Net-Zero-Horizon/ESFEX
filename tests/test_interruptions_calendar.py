"""Interruptions calendar dates follow the model calendar.

The model has 8760 h per year with Feb 29 skipped (the data are 8760-h
blocks). The calendar converted hours with the real calendar, so in a leap
base year every date after Feb 28 was shown one day early, and the second
year started on Dec 31. The JS helpers are evaluated with Qt's JS engine.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
QtQml = pytest.importorskip("PySide6.QtQml")

JS = (Path(__file__).resolve().parents[1] / "src" / "esfex" / "visualization"
      / "resources" / "interruptions.js")


@pytest.fixture(scope="module")
def engine():
    from PySide6.QtCore import QCoreApplication

    QCoreApplication.instance() or QCoreApplication([])
    src = JS.read_text(encoding="utf-8")
    # Header vars + date helpers only (the rest needs a DOM and d3).
    head = src[:src.index("// ── Bootstrap")]
    head = head.replace('"use strict";', "")
    eng = QtQml.QJSEngine()
    res = eng.evaluate(head)
    assert not res.isError(), res.toString()
    return eng


def _call(eng, expr):
    r = eng.evaluate(expr)
    assert not r.isError(), r.toString()
    return r.toVariant()


@pytest.mark.parametrize("base", [2024, 2025])
def test_hour_to_date_skips_feb_29(engine, base):
    _call(engine, f"baseYear = {base};")
    # Hour 59*24 is Mar 1 in the model, whatever the base year.
    assert _call(engine, f"hourToInput({59 * 24})") == f"{base}-03-01T00:00"
    assert _call(engine, "hourToInput(8759)") == f"{base}-12-31T23:00"
    assert _call(engine, "hourToInput(8760)") == f"{base + 1}-01-01T00:00"


def test_input_round_trip_and_feb_29(engine):
    _call(engine, "baseYear = 2024;")
    for h in (0, 1416, 1440, 5000, 8759, 8760, 9000, 17519):
        assert _call(engine, f"inputToHour(hourToInput({h}))") == h
    # Feb 29 does not exist in the model: it maps onto Feb 28.
    assert _call(engine, 'inputToHour("2024-02-29T05:00")') == \
        _call(engine, 'inputToHour("2024-02-28T05:00")')
    assert _call(engine, 'inputToHour("2025-01-01T00:00")') == 8760


def test_no_real_calendar_left():
    src = JS.read_text(encoding="utf-8")
    assert not re.search(r"baseMs", src)
