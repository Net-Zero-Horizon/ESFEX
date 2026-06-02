"""Additive coverage tests for esfex.visualization.theme.

Targets the QSS/CSS/JS generators, accessor helpers, theme registry
lookup (incl. aliases and unknown names), the singleton get/set, and
``apply_theme`` (with a stub QApplication so no display is required).
"""

from __future__ import annotations

import pytest

from esfex.visualization import theme as T


# ──────────────────────────────────────────────────────────────────
# Singleton get/set + restore fixture
# ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _restore_theme():
    saved = T.current_theme()
    yield
    T.set_theme(saved)


def test_current_and_set_theme_roundtrip():
    T.set_theme(T.THEME_DRACULA)
    assert T.current_theme() is T.THEME_DRACULA
    T.set_theme(T.THEME_LIGHT_CLASSIC)
    assert T.current_theme() is T.THEME_LIGHT_CLASSIC


# ──────────────────────────────────────────────────────────────────
# get_theme_by_name: direct, alias, unknown
# ──────────────────────────────────────────────────────────────────


def test_get_theme_by_name_direct():
    assert T.get_theme_by_name("Dracula") is T.THEME_DRACULA
    assert T.get_theme_by_name("Light") is T.THEME_LIGHT_CLASSIC


@pytest.mark.parametrize(
    "alias,target",
    [
        ("Dark", T.THEME_VSCODE_DARK),
        ("Twilight", T.THEME_DRACULA),
        ("Vivid", T.THEME_ONE_DARK),
    ],
)
def test_get_theme_by_name_alias(alias, target):
    assert T.get_theme_by_name(alias) is target


def test_get_theme_by_name_unknown_defaults_light():
    assert T.get_theme_by_name("NoSuchTheme") is T.THEME_LIGHT_CLASSIC


def test_get_theme_by_name_alias_points_to_missing(monkeypatch):
    # alias resolves but the target name is not in THEMES -> default light
    monkeypatch.setitem(T._THEME_ALIASES, "Ghost", "AlsoMissing")
    assert T.get_theme_by_name("Ghost") is T.THEME_LIGHT_CLASSIC


# ──────────────────────────────────────────────────────────────────
# generate_qss: default-arg branch + each theme's _theme_extras branch
# ──────────────────────────────────────────────────────────────────


def test_generate_qss_uses_current_theme_when_none():
    T.set_theme(T.THEME_LIGHT_CLASSIC)
    qss = T.generate_qss()
    assert "QWidget" in qss
    assert "QPushButton" in qss
    # Light classic has no extras appended
    assert "/* VSCode" not in qss


@pytest.mark.parametrize(
    "theme,marker",
    [
        (T.THEME_VSCODE_DARK, "VSCode: flat buttons"),
        (T.THEME_DRACULA, "Dracula: purple accent buttons"),
        (T.THEME_ONE_DARK, "OneDark: muted warm buttons"),
        (T.THEME_GITHUB_LIGHT, "GitHub: rounded buttons"),
    ],
)
def test_generate_qss_theme_extras(theme, marker):
    qss = T.generate_qss(theme)
    assert marker in qss
    # color tokens must be interpolated into the sheet
    assert theme.colors.accent_primary in qss


def test_generate_qss_light_classic_no_extras():
    assert T._theme_extras(T.THEME_LIGHT_CLASSIC) == ""


def test_theme_extras_uses_color_overrides():
    # VS Code Dark+ defines status_bar_bg / focus_border overrides; ensure
    # those override branches (a or b) take the non-empty value.
    extras = T._theme_extras(T.THEME_VSCODE_DARK)
    assert T.THEME_VSCODE_DARK.colors.status_bar_bg in extras
    assert T.THEME_VSCODE_DARK.colors.focus_border in extras


def test_theme_extras_falls_back_when_override_empty():
    # Dracula has empty status_bar_bg/fg -> falls back to surface/text.
    d = T.THEME_DRACULA
    assert d.colors.status_bar_bg == ""
    extras = T._theme_extras(d)
    # Dracula status bar uses surface_dark explicitly, but focus uses
    # focus_border (non-empty) -> just confirm it builds.
    assert "Dracula" in extras


# ──────────────────────────────────────────────────────────────────
# generate_map_css
# ──────────────────────────────────────────────────────────────────


def test_generate_map_css_default_theme_and_rgba_parse():
    T.set_theme(T.THEME_LIGHT_CLASSIC)
    css = T.generate_map_css()
    assert ".node-label" in css
    assert ".marker-selected" in css
    # surface_primary #FFFFFF -> rgba(255,255,255,0.88)
    assert "rgba(255,255,255,0.88)" in css


def test_generate_map_css_explicit_theme():
    css = T.generate_map_css(T.THEME_VSCODE_DARK)
    # surface_primary #1E1E1E -> 30,30,30
    assert "rgba(30,30,30,0.88)" in css
    assert T.THEME_VSCODE_DARK.map_elements.battery in css


def test_generate_map_css_label_font_size_from_prefs(monkeypatch):
    import esfex.visualization.preferences as prefs

    monkeypatch.setattr(prefs, "load_preferences", lambda: {"map": {}})
    monkeypatch.setattr(
        prefs,
        "get_preference",
        lambda p, sec, key, default: 17,
    )
    css = T.generate_map_css(T.THEME_LIGHT_CLASSIC)
    assert "17px" in css


def test_generate_map_css_prefs_exception_falls_back(monkeypatch):
    import esfex.visualization.preferences as prefs

    def _boom():
        raise RuntimeError("no prefs")

    monkeypatch.setattr(prefs, "load_preferences", _boom)
    css = T.generate_map_css(T.THEME_LIGHT_CLASSIC)
    # falls back to default 10px
    assert "10px" in css


# ──────────────────────────────────────────────────────────────────
# generate_map_js_colors
# ──────────────────────────────────────────────────────────────────


def test_generate_map_js_colors_default():
    T.set_theme(T.THEME_LIGHT_CLASSIC)
    js = T.generate_map_js_colors()
    assert "_defaultColors" in js
    assert T.THEME_LIGHT_CLASSIC.map_elements.node in js


def test_generate_map_js_colors_explicit():
    js = T.generate_map_js_colors(T.THEME_DRACULA)
    assert T.THEME_DRACULA.map_elements.bus in js
    assert "'node-marker'" in js


# ──────────────────────────────────────────────────────────────────
# apply_theme (stub QApplication, no display needed)
# ──────────────────────────────────────────────────────────────────


class _StubApp:
    def __init__(self):
        self.qss = None

    def setStyleSheet(self, s):
        self.qss = s


def test_apply_theme_sets_theme_and_stylesheet():
    app = _StubApp()
    T.apply_theme(app, T.THEME_DRACULA)
    assert T.current_theme() is T.THEME_DRACULA
    assert app.qss and "QWidget" in app.qss


def test_apply_theme_none_uses_current():
    T.set_theme(T.THEME_GITHUB_LIGHT)
    app = _StubApp()
    T.apply_theme(app)
    assert app.qss is not None
    assert T.current_theme() is T.THEME_GITHUB_LIGHT


def test_apply_theme_font_size_override():
    T.set_theme(T.THEME_LIGHT_CLASSIC)
    app = _StubApp()
    T.apply_theme(app, font_size=20)
    active = T.current_theme()
    assert active.typography.size_body == 20
    assert "font-size: 20px" in app.qss


def test_apply_theme_font_size_equal_no_replace():
    T.set_theme(T.THEME_LIGHT_CLASSIC)
    same = T.THEME_LIGHT_CLASSIC.typography.size_body
    app = _StubApp()
    T.apply_theme(app, font_size=same)
    # No replacement; singleton remains the original object.
    assert T.current_theme() is T.THEME_LIGHT_CLASSIC


# ──────────────────────────────────────────────────────────────────
# Accessor helpers
# ──────────────────────────────────────────────────────────────────


def test_get_zone_colors():
    T.set_theme(T.THEME_LIGHT_CLASSIC)
    z = T.get_zone_colors()
    assert z["Solar"] == T.THEME_LIGHT_CLASSIC.zones.solar
    assert set(z) == {"Solar", "Wind", "Battery", "Hydro", "Biomass", "Hydrogen"}


def test_get_generation_colors_is_copy():
    T.set_theme(T.THEME_LIGHT_CLASSIC)
    g = T.get_generation_colors()
    assert g["Solar"] == "#FFC300"
    g["Solar"] = "#000000"
    # mutation does not leak into theme
    assert T.get_generation_colors()["Solar"] == "#FFC300"


def test_get_generation_default_color():
    T.set_theme(T.THEME_LIGHT_CLASSIC)
    assert T.get_generation_default_color() == "#95A5A6"


def test_get_heatmap_gradient_match():
    T.set_theme(T.THEME_LIGHT_CLASSIC)
    # "Average LMP" contains "LMP"
    assert T.get_heatmap_gradient("Average LMP") == ("#3498DB", "#E74C3C")
    assert T.get_heatmap_gradient("RE share") == ("#E74C3C", "#27AE60")


def test_get_heatmap_gradient_default_when_no_match():
    T.set_theme(T.THEME_LIGHT_CLASSIC)
    assert T.get_heatmap_gradient("totally unknown var") == ("#3498DB", "#E74C3C")


def test_get_heatmap_gradient_default_fallback_missing(monkeypatch):
    # When even "_default" key is absent, the hardcoded tuple is returned.
    from dataclasses import replace

    th = replace(
        T.THEME_LIGHT_CLASSIC,
        charts=replace(
            T.THEME_LIGHT_CLASSIC.charts,
            heatmap_gradients={"RE": ("#111111", "#222222")},
        ),
    )
    T.set_theme(th)
    assert T.get_heatmap_gradient("nomatch") == ("#3498DB", "#E74C3C")


@pytest.mark.parametrize(
    "severity,expected_attr",
    [
        ("error", "error"),
        ("warning", "warning"),
        ("info", "info"),
        ("simplification", "simplification"),
    ],
)
def test_get_validation_color_known(severity, expected_attr):
    T.set_theme(T.THEME_LIGHT_CLASSIC)
    v = T.THEME_LIGHT_CLASSIC.validation
    assert T.get_validation_color(severity) == getattr(v, expected_attr)


def test_get_validation_color_unknown_defaults_info():
    T.set_theme(T.THEME_LIGHT_CLASSIC)
    assert T.get_validation_color("bogus") == T.THEME_LIGHT_CLASSIC.validation.info


def test_get_tab10_is_copy():
    T.set_theme(T.THEME_LIGHT_CLASSIC)
    pal = T.get_tab10()
    assert pal == T._DEFAULT_TAB10
    pal.append("#000000")
    assert T.get_tab10() == T._DEFAULT_TAB10


def test_get_tree_category_color_empty_returns_none():
    # Light classic has all-empty tree categories.
    T.set_theme(T.THEME_LIGHT_CLASSIC)
    assert T.get_tree_category_color("nodes") is None


def test_get_tree_category_color_nonempty():
    T.set_theme(T.THEME_VSCODE_DARK)
    assert T.get_tree_category_color("nodes") == T.THEME_VSCODE_DARK.tree_categories.nodes


def test_get_tree_category_color_unknown_attr_returns_none():
    T.set_theme(T.THEME_VSCODE_DARK)
    assert T.get_tree_category_color("does_not_exist") is None


# ──────────────────────────────────────────────────────────────────
# Registry / aliases sanity
# ──────────────────────────────────────────────────────────────────


def test_registry_and_backward_aliases():
    assert set(T.THEMES) == {
        "Light",
        "GitHub Light",
        "VS Code Dark+",
        "Dracula",
        "One Dark Pro",
    }
    assert T.THEME_LIGHT is T.THEME_LIGHT_CLASSIC
    assert T.THEME_DARK is T.THEME_VSCODE_DARK
    assert T.THEME_TWILIGHT is T.THEME_DRACULA
    assert T.THEME_VIVID is T.THEME_ONE_DARK


def test_default_factory_dicts_independent():
    a = T._default_generation_colors()
    b = T._default_generation_colors()
    assert a == b and a is not b
    g = T._default_heatmap_gradients()
    assert g["_default"] == ("#3498DB", "#E74C3C")
