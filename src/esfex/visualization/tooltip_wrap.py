"""Application-wide tooltip word-wrapping.

Qt only word-wraps a tooltip when its text is *rich text* (``Qt::mightBeRichText``
returns true); a plain-text tooltip is drawn on a single line, so a long
sentence stretches all the way across the screen.

An app-global ``QApplication.installEventFilter`` is NOT usable here: routing
every event through Python is incompatible with the embedded QtWebEngine map
(it hangs / segfaults). Instead we wrap at *assignment* time by patching
``QWidget.setToolTip`` once, so every plain-text tooltip is stored as rich text
pre-wrapped to a logical width. This never touches the event loop.

Call :func:`install_tooltip_wrapping` once, after the ``QApplication`` exists.
"""

from __future__ import annotations

import html

from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QToolTip, QWidget

# Target tooltip line length, in "average characters". Converted to pixels via
# the tooltip font's metrics so the wrap width tracks the font size / UI scale
# instead of being a fixed pixel count.
_TARGET_CHARS = 52


def _wrap_to_width(text: str, fm: QFontMetrics, max_px: int) -> str:
    """Word-wrap plain ``text`` to ``max_px`` and return rich text with <br>s.

    Existing newlines are kept as hard breaks (paragraphs); each paragraph is
    then greedily wrapped by measured pixel width, so proportional fonts wrap
    correctly. A single word wider than ``max_px`` is left on its own line
    rather than split mid-word.
    """
    lines: list[str] = []
    for para in text.split("\n"):
        if not para.strip():
            lines.append("")
            continue
        cur = ""
        for word in para.split(" "):
            if not word:
                continue
            trial = word if not cur else f"{cur} {word}"
            if not cur or fm.horizontalAdvance(trial) <= max_px:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
    return "<br>".join(html.escape(line) for line in lines)


def wrap_tooltip(text: str) -> str | None:
    """Return a rich-text, width-wrapped version of ``text``, or None to leave
    it untouched (already rich text, empty, or short enough to fit one line)."""
    if not text:
        return None
    if text.lstrip().startswith("<"):
        # Author-supplied rich text already controls its own wrapping. (Plain
        # text with a stray '<' does not start with one, and is HTML-escaped
        # below, so it is never mistaken for markup.)
        return None
    fm = QFontMetrics(QToolTip.font())
    max_px = max(220, fm.averageCharWidth() * _TARGET_CHARS)
    if fm.horizontalAdvance(text) <= max_px and "\n" not in text:
        return None
    return f"<qt>{_wrap_to_width(text, fm, max_px)}</qt>"


_installed = False


def install_tooltip_wrapping() -> None:
    """Patch ``QWidget.setToolTip`` so every long plain-text tooltip is stored
    pre-wrapped to a logical width. Idempotent."""
    global _installed
    if _installed:
        return
    _installed = True

    _orig_set_tooltip = QWidget.setToolTip

    def _set_tooltip(self, text):
        try:
            if text:
                wrapped = wrap_tooltip(text)
                if wrapped is not None:
                    text = wrapped
        except Exception:
            # Never let tooltip formatting break setting a tooltip.
            pass
        _orig_set_tooltip(self, text)

    QWidget.setToolTip = _set_tooltip
