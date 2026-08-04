"""Application-wide tooltip word-wrapping.

Qt only word-wraps a tooltip when its text is *rich text* (``Qt::mightBeRichText``
returns true); a plain-text tooltip is drawn on a single line, so a long
sentence stretches all the way across the screen. Rather than hand-wrap the
hundreds of ``setToolTip`` calls in the GUI, a single application event filter
intercepts every tooltip request and re-shows the text wrapped to a logical
width.

Install once, on the ``QApplication``::

    app.installEventFilter(ToolTipWrapFilter(app))
"""

from __future__ import annotations

import html

from PySide6.QtCore import QEvent, QObject
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


class ToolTipWrapFilter(QObject):
    """Event filter that wraps every plain-text tooltip to a logical width."""

    def eventFilter(self, obj, event):  # noqa: N802 (Qt override)
        if event.type() != QEvent.Type.ToolTip:
            return False
        if not isinstance(obj, QWidget):
            return False
        text = obj.toolTip()
        if not text:
            # No widget tooltip (e.g. an item-view cell tooltip resolved from
            # the model) — let Qt's default handling run.
            return False
        if text.lstrip().startswith("<"):
            # Author-supplied rich text already controls its own wrapping.
            # (Plain text containing a stray '<' is handled below and HTML-
            # escaped, so it is never mistaken for markup here.)
            return False

        fm = QFontMetrics(QToolTip.font())
        max_px = max(220, fm.averageCharWidth() * _TARGET_CHARS)
        # Nothing to gain if it already fits on one line.
        if fm.horizontalAdvance(text) <= max_px and "\n" not in text:
            return False

        wrapped = _wrap_to_width(text, fm, max_px)
        QToolTip.showText(event.globalPos(), f"<qt>{wrapped}</qt>", obj)
        return True
