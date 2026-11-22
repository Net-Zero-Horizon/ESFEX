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
def engine(qapp):
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


# ── Review round 2: horizon and base year ──────────────────────────────


def test_clamp_uses_the_whole_horizon(engine):
    # Edits were clamped to one year: no window after the base year.
    _call(engine, "horizon = 25 * 8760;")
    assert _call(engine, "clampH(20 * 8760 + 5)") == 20 * 8760 + 5
    assert _call(engine, "clampH(26 * 8760)") == 25 * 8760
    _call(engine, "horizon = 8760;")


def _payload(date_start, windows):
    import json
    from types import SimpleNamespace

    from esfex.visualization.data.gui_model import (
        GuiGlobalSettings, GuiOutageWindow, GuiSystemState,
    )
    from esfex.visualization.panels.interruptions_dialog import (
        InterruptionsDialog,
    )

    state = GuiSystemState(name="S", outage_schedule=[
        GuiOutageWindow(element_type="generator", element_id="g",
                        start_hour=s, end_hour=e, availability=0.0)
        for s, e in windows])
    model = SimpleNamespace(state=state,
                            global_settings=GuiGlobalSettings(date_start=date_start))
    fake = SimpleNamespace(model=model, _focus=None,
                           _collect_groups=lambda: [], _theme_colors=lambda: {})
    return json.loads(InterruptionsDialog._build_payload(fake))


def test_payload_spans_the_run_horizon_from_date_start():
    from esfex.visualization.panels.interruptions_dialog import STUDIO_RUN_YEARS

    p = _payload("01/06/2030 00:00", [(100, 200)])
    assert p["base_year"] == 2030            # not a hard-coded 2025
    assert p["horizon_hours"] == STUDIO_RUN_YEARS * 8760
    # A window scheduled past the default horizon stays reachable.
    p = _payload("01/01/2025 00:00", [(30 * 8760 + 10, 30 * 8760 + 20)])
    assert p["horizon_hours"] == 31 * 8760
    assert p["schedule"][0]["start_hour"] == 30 * 8760 + 10


def test_calendar_and_runner_share_the_default_horizon(tmp_path, monkeypatch):
    """The calendar spans the years a run simulates without ``--years``:
    one constant for both, so they cannot drift apart."""
    import sys

    import esfex.runner as runner_mod
    from esfex.utils.temporal import DEFAULT_RUN_YEARS
    from esfex.visualization.panels.interruptions_dialog import STUDIO_RUN_YEARS

    assert STUDIO_RUN_YEARS == DEFAULT_RUN_YEARS
    sys.path.insert(0, "tests")
    from test_runner import _make_esfex_config

    monkeypatch.setattr(runner_mod, "DEFAULT_RUN_YEARS", 3)
    orch = runner_mod.Orchestrator(_make_esfex_config(), output_dir=tmp_path)
    # Stops right after the horizon is set: it reports the default's span
    with pytest.raises(ValueError, match="horizon 2025-2027"):
        orch.run(dispatch_years=[1900])
