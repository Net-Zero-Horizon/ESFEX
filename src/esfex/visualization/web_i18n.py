"""Translations for the Studio's web views (map, SLD, dashboard, charts).

The map, the single-line diagram, the dashboard and most result charts are
HTML/JavaScript pages in QtWebEngine views. Their texts live in the ``web``
section of the translation files. :func:`install` adds a script to the
default web profile that runs before any page script and defines:

* ``window.ESFEX_I18N``: the ``web.*`` strings of the current language;
* ``window.ESFEX_LANG``: the language code;
* ``window._t(key, params)``: the string for ``key`` (e.g. ``"web.map.bus"``),
  with ``{name}`` placeholders filled from ``params``. Plotly's ``%{x}``
  hover fields are left alone. A missing key returns the key itself, as
  ``tr()`` does, so tests can spot it. (Named ``_t``: page scripts use
  ``t`` for their own variables.)

Static HTML text is marked ``data-i18n="web.key"`` (``data-i18n-title``,
``data-i18n-placeholder`` for attributes) and filled when the page loads.

On a language change the script is replaced for pages loaded afterwards,
and pages already open get the new strings and their marked elements
refilled; a page that defines ``window.onEsfexLanguageChanged`` is called to
redraw the texts its scripts build.
"""
from __future__ import annotations

import json
import logging

from esfex.visualization import i18n

logger = logging.getLogger(__name__)

SCRIPT_NAME = "esfex-i18n"
SECTION = "web."

_JS_HELPERS = r"""
window._t = function (key, params) {
    var s = (window.ESFEX_I18N && window.ESFEX_I18N[key]);
    if (s === undefined || s === null) return key;
    if (!params) return s;
    return s.replace(/(?<!%)\{(\w+)\}/g, function (m, name) {
        return params[name] !== undefined ? String(params[name]) : m;
    });
};
// Static HTML: <span data-i18n="web.x">, data-i18n-title, data-i18n-placeholder.
window._applyI18n = function (root) {
    (root || document).querySelectorAll(
        "[data-i18n],[data-i18n-title],[data-i18n-placeholder]").forEach(function (el) {
        var k = el.getAttribute("data-i18n");
        if (k) el.textContent = window._t(k);
        k = el.getAttribute("data-i18n-title");
        if (k) el.title = window._t(k);
        k = el.getAttribute("data-i18n-placeholder");
        if (k) el.placeholder = window._t(k);
    });
};
if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { window._applyI18n(); });
} else {
    window._applyI18n();
}
"""


def web_strings() -> dict[str, str]:
    """The ``web.*`` strings of the current language."""
    return {k: v for k, v in i18n._strings.items() if k.startswith(SECTION)}


def bootstrap_js() -> str:
    """The script that defines the translations and ``t()`` in a page."""
    data = json.dumps(web_strings(), ensure_ascii=False)
    lang = json.dumps(i18n.get_language())
    return (f"window.ESFEX_I18N = {data};\nwindow.ESFEX_LANG = {lang};\n"
            + _JS_HELPERS)


def _profile():
    from PySide6.QtWebEngineCore import QWebEngineProfile
    return QWebEngineProfile.defaultProfile()


def _install_script() -> None:
    from PySide6.QtWebEngineCore import QWebEngineScript
    scripts = _profile().scripts()
    for old in scripts.find(SCRIPT_NAME):
        scripts.remove(old)
    script = QWebEngineScript()
    script.setName(SCRIPT_NAME)
    script.setSourceCode(bootstrap_js())
    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
    script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
    script.setRunsOnSubFrames(True)
    scripts.insert(script)


def refresh_open_pages() -> None:
    """Send the current language's strings to every open web view."""
    try:
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtWidgets import QApplication
    except Exception:
        return
    code = (bootstrap_js()
            + "\nwindow._applyI18n();"
            + "\nif (window.onEsfexLanguageChanged) window.onEsfexLanguageChanged();")
    for w in QApplication.allWidgets():
        if isinstance(w, QWebEngineView):
            try:
                w.page().runJavaScript(code)
            except RuntimeError:     # view being destroyed
                pass


def _on_language_changed() -> None:
    _install_script()
    refresh_open_pages()


_connected = False


def install() -> None:
    """Give every web page the translations (call after ``init_i18n``)."""
    global _connected
    try:
        _install_script()
    except Exception as exc:   # QtWebEngine missing: no web views to serve
        logger.debug("web i18n not installed: %s", exc)
        return
    if not _connected and i18n.language_changed is not None:
        i18n.language_changed.connect(_on_language_changed)
        _connected = True
