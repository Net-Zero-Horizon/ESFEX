"""Small-screen fixes: font scale may shrink; the properties panel has a
scaled, non-collapsible-only minimum width."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from esfex.visualization import ui_scale as U


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_font_scale_can_shrink_below_one(monkeypatch):
    # Was floored at 1.0 (never shrank) → text overflowed small laptops.
    monkeypatch.setattr(U, "ui_scale", lambda: 0.8)
    assert U.font_scale() == pytest.approx(0.85)   # floored at 0.85
    monkeypatch.setattr(U, "ui_scale", lambda: 0.9)
    assert U.font_scale() == pytest.approx(0.9)
    monkeypatch.setattr(U, "ui_scale", lambda: 1.5)
    assert U.font_scale() == pytest.approx(1.2)     # capped


def test_ui_scale_floor_lowered(qapp):
    # Chrome can now shrink to 0.8 on small screens (was 0.9).
    U.reset_cache()
    assert 0.8 <= U.ui_scale() <= 1.4


def test_scale_qss_fonts_scales_px_and_pt(monkeypatch):
    monkeypatch.setattr(U, "font_scale", lambda: 0.85)
    assert U.scale_qss_fonts("QLabel { font-size: 12px; }") == \
        "QLabel { font-size: 10px; }"
    assert U.scale_qss_fonts("font-size: 14pt;") == "font-size: 12pt;"
    # Braces preserved; templated sizes (no literal digit) left untouched.
    assert U.scale_qss_fonts("font-size: {n}px;") == "font-size: {n}px;"


def test_scale_qss_fonts_noop_at_unity(monkeypatch):
    monkeypatch.setattr(U, "font_scale", lambda: 1.0)
    s = "QPushButton { font-size: 11px; padding: 2px; }"
    assert U.scale_qss_fonts(s) is s  # identity at 1080p


def test_scaled_px_floors_at_one(monkeypatch):
    monkeypatch.setattr(U, "font_scale", lambda: 0.01)
    assert U.scaled_px(12) == 1


def test_properties_panel_min_is_scaled_and_narrower(qapp):
    from esfex.visualization.panels.properties import PropertiesPanel
    from esfex.visualization.ui_scale import scaled
    p = PropertiesPanel()
    assert p.minimumWidth() == scaled(130)
    assert p.minimumWidth() < 400  # narrower than the old hard 400 floor
