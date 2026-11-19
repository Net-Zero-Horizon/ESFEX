"""Every text the web views (map, SLD, dashboard, charts) show is translated.

Their text comes from ``_t("web.<page>.<key>")``; a key missing from the
translation files shows as the key itself. Texts still written in English in
the JavaScript are listed in ``js_untranslated_baseline.json``, which may only
shrink: after translating some, run ``python tests/js_i18n_audit.py``.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import js_i18n_audit  # noqa: E402  (tests/ is on sys.path)

from esfex.visualization.i18n import _flatten

_TR = Path(__file__).resolve().parent.parent / "src/esfex/visualization/translations"
EN = _flatten(json.loads((_TR / "en.json").read_text(encoding="utf-8")))


def test_every_web_key_exists():
    missing = [f"{f}:{n} {k}" for f, n, k in js_i18n_audit.used_keys() if k not in EN]
    assert not missing, "_t() keys missing from en.json:\n" + "\n".join(missing)


def test_no_new_untranslated_web_text():
    baseline = json.loads(js_i18n_audit.BASELINE.read_text(encoding="utf-8"))
    now = js_i18n_audit.current_untranslated()
    new = []
    for f, texts in now.items():
        new += [f"{f}: {t!r}" for t in (Counter(texts) - Counter(baseline.get(f, []))).elements()]
    assert not new, "web text not wrapped in _t() (add a web.* key):\n" + "\n".join(new)


def test_baseline_only_lists_text_still_untranslated():
    baseline = json.loads(js_i18n_audit.BASELINE.read_text(encoding="utf-8"))
    now = js_i18n_audit.current_untranslated()
    stale = []
    for f, texts in baseline.items():
        stale += [f"{f}: {t!r}" for t in (Counter(texts) - Counter(now.get(f, []))).elements()]
    assert not stale, ("translated, so remove from the baseline "
                       "(python tests/js_i18n_audit.py):\n" + "\n".join(stale))
