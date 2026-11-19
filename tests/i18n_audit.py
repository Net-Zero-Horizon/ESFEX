"""Static audit of the Studio's user-visible text (used by test_i18n_coverage).

Finds ``tr("key")`` calls whose key is missing from the translation files
(the Studio would show the raw key) and string literals passed straight to
Qt text setters, constructors and dialogs without ``tr()`` (shown in English
whatever the language).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "esfex"

_UI_CALLS = {
    "QLabel", "QPushButton", "QCheckBox", "QRadioButton", "QGroupBox", "QAction",
    "QToolButton", "QMenu", "QTreeWidgetItem", "QDockWidget", "QLineEdit",
    "setText", "setToolTip", "setWindowTitle", "setPlaceholderText", "setStatusTip",
    "setTitle", "addTab", "insertTab", "setTabText", "setTabToolTip", "addItem",
    "addItems", "addRow", "addMenu", "addAction", "setHorizontalHeaderLabels",
    "setVerticalHeaderLabels", "setHeaderLabels", "setHeaderLabel", "showMessage",
    "setSpecialValueText", "setFormat", "setWhatsThis", "setLabelText",
    "setInformativeText", "setDetailedText",
}
_DIALOG_CALLS = {
    "warning", "information", "critical", "question", "about", "getText", "getItem",
    "getInt", "getDouble", "getOpenFileName", "getSaveFileName",
    "getExistingDirectory", "getOpenFileNames",
}
_DIALOG_OWNERS = {"QMessageBox", "QInputDialog", "QFileDialog"}
_WORD = re.compile(r"[A-Za-z]{2,}")
# Proper names and symbols shown as they are in every language.
UNTRANSLATED_OK = {
    "XGBoost", "Microsoft ML", "Overture Maps", "Google Open Buildings",
    "ESFEX", "ESFEX Studio", "OTEC Studio", "OpenStreetMap", "GridFinder",
    "ERA5", "NASA POWER", "PyPSA", "HDF5", "CSV", "SVG", "PNG", "JSON", "YAML",
    "LCOE", "NPV", "IRR", "MW", "MWh", "GWh", "kV", "Hz", "AC", "DC",
    "IM", "LHS",
}
# Calls whose later positional arguments are data, not text.
_TEXT_ARG_ONLY = {"addItem": 0, "insertItem": 1, "addRow": 0}


def _literal_text(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = [v.value for v in node.values
                 if isinstance(v, ast.Constant) and isinstance(v.value, str)]
        return "{}".join(parts) if parts else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        left, right = _literal_text(node.left), _literal_text(node.right)
        return (left or "") + (right or "") if (left or right) else None
    if isinstance(node, (ast.List, ast.Tuple)):
        texts = [t for t in (_literal_text(e) for e in node.elts) if t]
        return " | ".join(texts) if texts else None
    return None


def _call_name(node):
    fn = node.func
    return fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")


def _visible(text):
    if not text or not _WORD.search(re.sub(r"<[^>]*>|&\w+;", "", text)):
        return False
    s = text.strip()
    if s in UNTRANSLATED_OK:
        return False
    if re.fullmatch(r"[\w./-]+\.(png|svg|ico|json|csv|ya?ml|h5|html|js|css)", s):
        return False
    if s.startswith(("font-", "color:", "QWidget", "background", "padding",
                     "border", "<style")):
        return False
    return not re.fullmatch(r"[a-z_]+(\.[a-z0-9_]+)+", s)


def _gui_modules():
    for f in sorted(SRC.rglob("*.py")):
        text = f.read_text(encoding="utf-8")
        if "PySide6" in text:
            yield f.relative_to(SRC.parent).as_posix(), ast.parse(text)


def literal_tr_keys():
    """(file, line, key) of every ``tr("literal")`` call."""
    out = []
    for rel, tree in _gui_modules():
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "tr" and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                out.append((rel, node.lineno, node.args[0].value))
    return out


def hardcoded_texts():
    """(file, line, call, text) of UI text passed to Qt without ``tr()``."""
    out = []
    for rel, tree in _gui_modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = (fn.id if isinstance(fn, ast.Name)
                    else fn.attr if isinstance(fn, ast.Attribute) else "")
            owner = (fn.value.id if isinstance(fn, ast.Attribute)
                     and isinstance(fn.value, ast.Name) else "")
            if name in _DIALOG_CALLS and owner not in _DIALOG_OWNERS:
                continue
            if name not in _UI_CALLS and name not in _DIALOG_CALLS:
                continue
            if name in _TEXT_ARG_ONLY:
                pos = _TEXT_ARG_ONLY[name]
                args = [a for a in node.args[pos:pos + 2]
                        if not (isinstance(a, ast.Call) and _call_name(a) == "QIcon")][:1]
            else:
                args = list(node.args)
            args += [k.value for k in node.keywords
                     if k.arg in ("text", "title", "toolTip", "label")]
            for a in args:
                if isinstance(a, ast.Call):
                    continue
                t = _literal_text(a)
                if _visible(t):
                    out.append((rel, node.lineno, name, t))
    return out


BASELINE = Path(__file__).resolve().parent / "i18n_untranslated_baseline.json"


def current_untranslated() -> dict[str, list[str]]:
    """Hard-coded UI texts per file (sorted, repeats kept)."""
    out: dict[str, list[str]] = {}
    for rel, _line, _call, text in hardcoded_texts():
        out.setdefault(rel, []).append(text)
    return {k: sorted(v) for k, v in sorted(out.items())}


if __name__ == "__main__":       # python tests/i18n_audit.py: rewrite the baseline
    import json
    BASELINE.write_text(json.dumps(current_untranslated(), ensure_ascii=False,
                                   indent=1) + "\n", encoding="utf-8")
    print(f"wrote {BASELINE}")
