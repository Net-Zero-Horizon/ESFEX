"""A chart page's empty-state message stays visible.

Several MGA pages showed "no data" from render() and then refresh() hid the
status right after render() returned, so the message never appeared. Pages
whose render() can show a status must hide the old status before render().
"""
from __future__ import annotations

import re
from pathlib import Path

RESOURCES = Path(__file__).resolve().parent.parent / "src/esfex/visualization/resources"


def _render_body(js: str) -> str:
    m = re.search(r"^function render\(.*?^}", js, re.S | re.M)
    return m.group(0) if m else ""


def test_status_is_not_hidden_after_render_shows_it():
    offenders = []
    for f in sorted(RESOURCES.glob("*.js")):
        js = f.read_text(encoding="utf-8")
        if "showStatus(" not in _render_body(js):
            continue
        # render(...) followed directly by hideStatus() hides render's message.
        if re.search(r"render\([^)]*\);\s*(?://[^\n]*)?\s*hideStatus\(\)", js):
            offenders.append(f.name)
    assert not offenders, f"hideStatus() runs after render(): {offenders}"
