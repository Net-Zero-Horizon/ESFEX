"""Screen-proportional UI scaling for the Studio chrome.

Qt 6 already makes *logical* pixels DPI-independent (a 32 px icon is the same
physical size on a 1080p@100% display and a 4K@200% display), so we don't need
to re-implement high-DPI handling. What this adds is a **screen-size** factor on
top: the toolbar, panel floors and base font grow a little on large, high-
resolution monitors and shrink on small ones — proportional to the screen,
clamped so it never gets silly. The factor is computed once from the primary
screen's *logical* geometry, relative to a 1080p baseline.
"""

from __future__ import annotations

from PySide6.QtWidgets import QApplication

_BASELINE_H = 1080.0      # logical-pixel height the base sizes are tuned for
_MIN, _MAX = 0.9, 1.4     # clamp so chrome never gets tiny or huge

_cached: float | None = None


def ui_scale() -> float:
    """Screen-proportional UI factor in ``[_MIN, _MAX]`` (1.0 at 1080p)."""
    global _cached
    if _cached is not None:
        return _cached
    s = 1.0
    app = QApplication.instance()
    if app is not None:
        screen = app.primaryScreen()
        if screen is not None:
            h = float(screen.availableGeometry().height())
            if h > 0:
                s = h / _BASELINE_H
    _cached = max(_MIN, min(_MAX, s))
    return _cached


def scaled(px: float) -> int:
    """Scale a base logical-pixel size by :func:`ui_scale`, rounded to int."""
    return int(round(px * ui_scale()))


def font_scale() -> float:
    """Gentler factor for the base font (text scales more conservatively than
    chrome: it may grow up to 20 % but never shrinks below the tuned size)."""
    return max(1.0, min(1.2, ui_scale()))


def reset_cache() -> None:
    """Forget the cached factor (e.g. after a screen change). Mostly for tests."""
    global _cached
    _cached = None
