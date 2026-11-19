"""Static audit of the text in the Studio's web views (used by
test_js_i18n_coverage).

The map, SLD, dashboard and chart pages are JavaScript in
``src/esfex/visualization/resources``. Their text should come from
``_t("web.<page>.<key>")`` (see esfex.visualization.web_i18n). This finds the
``_t`` keys used and the string literals that look like visible text written
in the code instead (titles, trace names, labels, HTML snippets).
"""
from __future__ import annotations

import re
from pathlib import Path

RESOURCES = (Path(__file__).resolve().parent.parent
             / "src" / "esfex" / "visualization" / "resources")
# Third-party libraries, with their own text (leaflet.draw is localised
# through L.drawLocal from the page code).
VENDORED = {"d3.v7.min.js", "elk.bundled.js", "plotly.min.js", "xterm.js",
            "xterm-addon-fit.js", "leaflet.js", "leaflet.draw.js"}
BASELINE = Path(__file__).resolve().parent / "js_untranslated_baseline.json"

_KEY = re.compile(r"""\b_t\(\s*(["'])(web\.[\w.]+)\1""")
_STR = re.compile(r"""(?P<q>["'`])(?P<s>(?:\\.|(?!(?P=q)).)*?)(?P=q)""")
_TECH_BEFORE = re.compile(
    r"""(getElementById|querySelector(All)?|addEventListener|removeEventListener|"""
    r"""\.attr|\.style|\.on|\.classed|\.select(All)?|\.append|\.insert|"""
    r"""classList\.\w+|setAttribute|getAttribute|createElement|\b_t|\bcase|"""
    r"""===|!==|==|!=|\btypeof|\[)\s*\(?\s*$""")


def _visible(s: str) -> bool:
    text = re.sub(r"<[^>]*>|&\w+;|\$\{[^}]*\}|%\{[^}]*\}", " ", s).strip()
    if not re.search(r"[A-Za-z]{2,}", text) or text == "use strict":
        return False
    one_word = " " not in text
    if one_word and not re.match(r"[A-Z][a-z]", text):
        return False            # identifiers, CSS classes, keys, units
    if re.match(r"^(#|\.|rgba?\(|hsla?\(|\d|var\(|url\(|translate|rotate|"
                r"scale|M\s?-?\d)", text):
        return False
    return True


def js_files():
    for f in sorted(RESOURCES.glob("*.js")):
        if f.name not in VENDORED:
            yield f


_ATTR_KEY = re.compile(r"""data-i18n(?:-title|-placeholder)?=["'](web\.[\w.]+)["']""")


def used_keys():
    """(file, line, key) of every ``_t("web....")`` call and ``data-i18n``
    attribute."""
    out = []
    for f in list(js_files()) + sorted(RESOURCES.glob("*.html")):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            out += [(f.name, n, m.group(2)) for m in _KEY.finditer(line)]
            out += [(f.name, n, m.group(1)) for m in _ATTR_KEY.finditer(line)]
    return out


def hardcoded_texts():
    """(file, line, text) of literals that look like visible text."""
    out = []
    for f in js_files():
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            st = line.strip()
            if st.startswith(("//", "*", "/*")) or "console." in line:
                continue
            for m in _STR.finditer(line):
                if _TECH_BEFORE.search(line[:m.start()]):
                    continue
                if _visible(m.group("s")):
                    out.append((f.name, n, m.group("s")))
    return out


def html_texts():
    """(file, 0, text) of visible text in the pages' HTML not marked
    ``data-i18n`` (the ``<title>`` is not shown in the Studio)."""
    out = []
    for f in sorted(RESOURCES.glob("*.html")):
        s = f.read_text(encoding="utf-8")
        s = re.sub(r"<script.*?</script>|<style.*?</style>|<!--.*?-->|"
                   r"<title>.*?</title>", "", s, flags=re.S)
        # Elements marked data-i18n: drop their (placeholder) content.
        s = re.sub(r"(<[^>]*\bdata-i18n=[^>]*>)[^<]*", r"\1", s)
        for t in re.findall(r">([^<>]+)<", s):
            t = t.strip()
            if re.search(r"[A-Za-z]{2,}", re.sub(r"&\w+;", "", t)):
                out.append((f.name, 0, t))
        for t in re.findall(r'\s(?:title|placeholder|aria-label)="([^"]*)"', s):
            if re.search(r"[A-Za-z]{2,}", t):
                out.append((f.name, 0, t))
    return out


def current_untranslated() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name, _n, text in hardcoded_texts() + html_texts():
        out.setdefault(name, []).append(text)
    return {k: sorted(v) for k, v in sorted(out.items())}


if __name__ == "__main__":       # python tests/js_i18n_audit.py: rewrite the baseline
    import json
    BASELINE.write_text(json.dumps(current_untranslated(), ensure_ascii=False,
                                   indent=1) + "\n", encoding="utf-8")
    print(f"wrote {BASELINE}")
