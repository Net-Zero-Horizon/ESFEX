"""Matplotlib fonts that can draw every language the Studio offers.

Matplotlib's default font, DejaVu Sans, has no Japanese (or Chinese/Korean)
glyphs, so translated chart titles and node names in Japanese came out as
empty boxes. Matplotlib 3.6+ falls back font by font for each glyph, so a CJK
font installed on the system is appended after the default one: Latin text
keeps its look, CJK text gets drawn.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Common CJK fonts on Linux, macOS and Windows, preferred first.
CJK_FONTS = (
    "Noto Sans CJK JP", "Noto Sans JP", "Source Han Sans JP", "IPAexGothic",
    "IPAGothic", "TakaoGothic", "Hiragino Sans", "Hiragino Kaku Gothic ProN",
    "Yu Gothic", "Meiryo", "MS Gothic", "Noto Sans CJK SC", "Noto Sans CJK TC",
    "Noto Sans CJK KR",
)


def configure_cjk_fallback() -> str | None:
    """Append the first installed CJK font to matplotlib's sans-serif list.

    Returns the font used, or None when matplotlib or a CJK font is missing
    (charts then still work, without CJK glyphs)."""
    try:
        import matplotlib
        from matplotlib import font_manager
    except Exception:   # matplotlib not installed
        return None
    installed = {f.name for f in font_manager.fontManager.ttflist}
    font = next((f for f in CJK_FONTS if f in installed), None)
    if font is None:
        logger.info("No CJK font found; Japanese chart text will not render.")
        return None
    # The per-glyph fallback runs over the ``font.family`` list (a generic
    # family there resolves to one font), so the CJK font goes after it.
    family = list(matplotlib.rcParams["font.family"])
    if font not in family:
        matplotlib.rcParams["font.family"] = family + [font]
    return font
