"""Every text the Studio shows is translated.

``tr()`` shows the key itself when it is missing from the translation files
(the Studio displayed ``wizard_solar_pv.lcoe``), and text passed straight to
Qt stays English in every language. This checks that literal and catalogue
keys exist, and that no new untranslated text appears: the texts still
hard-coded are listed in ``i18n_untranslated_baseline.json``, which may only
shrink. After translating some, run ``python tests/i18n_audit.py``.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

from esfex.visualization.i18n import _flatten

import i18n_audit  # noqa: E402  (tests/ is on sys.path)

_TR = Path(__file__).resolve().parent.parent / "src/esfex/visualization/translations"
EN = _flatten(json.loads((_TR / "en.json").read_text(encoding="utf-8")))


def test_every_literal_key_exists():
    missing = [f"{f}:{n} {k}" for f, n, k in i18n_audit.literal_tr_keys()
               if k not in EN]
    assert not missing, "tr() keys missing from en.json:\n" + "\n".join(missing)


def _catalogue_keys():
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from esfex.config.schema import SporesObjective
    from esfex.visualization.panels import (
        demand_visualizer, doc_viewer, element_tree, investment_form,
        results_charts, stochastic_form,
    )
    from esfex.visualization.preferences import DEFAULT_SHORTCUTS
    from esfex.visualization.workflows import ev_wizard
    src = (i18n_audit.SRC / "visualization/data/auto_complete.py").read_text()
    keys = [f"auto_complete.reason_{r}" for r in re.findall(r'reason="(\w+)"', src)]
    keys += [f"demand_visualizer.chart_{k}" for k in demand_visualizer._CHART_ORDER]
    keys += [k for _, k in doc_viewer._SECTIONS]
    keys += [f"global_form.mp_mga_objective_{o.value}" for o in SporesObjective]
    keys += [k for _, k in investment_form._TECHNOLOGY_TYPE_KEYS]
    keys += [k for _, k, _ in stochastic_form._COST_MULTIPLIER_CATALOG]
    keys += [k for k, _ in ev_wizard._STEP_NAMES]
    keys += [f"tree.{k}" for k in element_tree._CATEGORY_KEYS]
    keys += [f"preferences_actions.{k}" for k in DEFAULT_SHORTCUTS]
    keys += [c.TR_KEY for c in vars(results_charts).values()
             if isinstance(c, type) and getattr(c, "TR_KEY", "")]
    rc = results_charts
    keys += [f"results_charts.{k}" for k in rc._SERIES_LABEL_KEYS.values()]
    keys += [f"results_charts.{k}" for k in rc._MGA_CATEGORY_KEYS.values()]
    keys += [f"results_charts.mga_obj_{o}" for o in rc._MGA_OBJECTIVE_COLORS]
    keys += [f"results_charts.month_short_{m}" for m in range(1, 13)]
    keys += [f"results_charts.metric_{k}" for k, _ in rc.SystemMetricsEvolutionChart._METRICS]
    keys += [f"results_charts.metric_cat_{c}"
             for c in rc.SystemMetricsEvolutionChart._CATEGORY_COLORS]
    keys += [f"results_charts.{k}" for _, k, _, _ in rc._CUSTOM_STD_VARS]
    cfg = rc._CustomConfigWidget
    keys += [k for items in (cfg._TYPES, cfg._STATS, cfg._TRANSFORMS, cfg._AGGS, cfg._SCALES)
             for _, k in items]
    return keys


def test_catalogue_keys_exist():
    missing = sorted({k for k in _catalogue_keys() if k not in EN})
    assert not missing, f"keys built at run time missing from en.json: {missing}"


def test_no_new_untranslated_text():
    baseline = json.loads(i18n_audit.BASELINE.read_text(encoding="utf-8"))
    now = i18n_audit.current_untranslated()
    new = []
    for f, texts in now.items():
        extra = Counter(texts) - Counter(baseline.get(f, []))
        new += [f"{f}: {t!r}" for t in extra.elements()]
    assert not new, ("UI text not wrapped in tr() (add a translation key):\n"
                     + "\n".join(new))


def test_baseline_only_lists_text_still_untranslated():
    baseline = json.loads(i18n_audit.BASELINE.read_text(encoding="utf-8"))
    now = i18n_audit.current_untranslated()
    stale = []
    for f, texts in baseline.items():
        gone = Counter(texts) - Counter(now.get(f, []))
        stale += [f"{f}: {t!r}" for t in gone.elements()]
    assert not stale, ("translated, so remove from the baseline "
                       "(python tests/i18n_audit.py):\n" + "\n".join(stale))
