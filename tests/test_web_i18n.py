"""The web views (map, SLD, dashboard, charts) get their translations.

web_i18n injects ``window.ESFEX_I18N`` and ``window._t`` into every page of
the default web profile before the page's own scripts run, and pushes a
language change to pages already open. Runs a real QtWebEngine page in a
subprocess, as test_sld_view_lod does.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("PySide6.QtWebEngineWidgets")

_SCRIPT = textwrap.dedent(r'''
    import json
    from pathlib import Path
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtWidgets import QApplication
    from PySide6.QtWebEngineWidgets import QWebEngineView

    import esfex.visualization as vis
    from esfex.visualization import i18n, web_i18n

    app = QApplication([])
    i18n.init_i18n("es")
    i18n._strings["web.test.greet"] = "Hola {name}: %{x} de {total}"
    web_i18n.install()
    view = QWebEngineView()
    page = view.page()
    out = {}

    PROBE = """JSON.stringify({
        lang: window.ESFEX_LANG,
        greet: _t('web.test.greet', {name: 'Ana', total: 3}),
        raw: _t('web.test.greet'),
        missing: _t('web.no.such_key')})"""

    def first(r):
        out["es"] = json.loads(r)
        i18n._strings["web.test.greet"] = "x"      # replaced by the reload below
        i18n.set_language("fr")                    # pushes to the open page
        QTimer.singleShot(300, lambda: page.runJavaScript(PROBE, second))

    def second(r):
        out["fr"] = json.loads(r)
        print("RESULT " + json.dumps(out))
        app.quit()

    view.loadFinished.connect(lambda _ok: QTimer.singleShot(
        200, lambda: page.runJavaScript(PROBE, first)))
    html = Path(vis.__file__).parent / "resources" / "sld.html"
    view.load(QUrl.fromLocalFile(str(html)))
    QTimer.singleShot(30000, app.quit)
    app.exec()
''')


@pytest.fixture(scope="module")
def result():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", _SCRIPT], capture_output=True,
                          text=True, timeout=120, env=env)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    if not lines:
        pytest.skip("offscreen QtWebEngine unavailable: " + (proc.stderr or "")[-400:])
    return json.loads(lines[-1][len("RESULT "):])


def test_page_gets_the_language_and_formats_placeholders(result):
    es = result["es"]
    assert es["lang"] == "es"
    # {name} and {total} are filled; Plotly's %{x} stays for Plotly.
    assert es["greet"] == "Hola Ana: %{x} de 3"
    assert es["raw"] == "Hola {name}: %{x} de {total}"


def test_missing_key_shows_the_key(result):
    assert result["es"]["missing"] == "web.no.such_key"


def test_language_change_reaches_open_pages(result):
    assert result["fr"]["lang"] == "fr"
