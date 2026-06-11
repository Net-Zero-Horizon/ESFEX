"""QWebEngineView that displays PowSyBl-generated single-line diagram SVGs.

The diagram itself (busbars, feeder cells with switchgear, transformer symbols,
orthogonal routing and native operational P annotations) is produced in Python
by :mod:`esfex.visualization.sld.powsybl_builder`; this widget only displays the
resulting SVG with pan/zoom and relays click selection back through the bridge.
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView

from esfex.visualization.bridge.channel import setup_sld_channel
from esfex.visualization.bridge.sld_bridge import SldBridge

_RESOURCES_DIR = Path(__file__).parent / "resources"


class SldWidget(QWebEngineView):
    """Displays a PowSyBl SLD SVG (pan/zoom + click selection)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bridge: SldBridge = setup_sld_channel(self)

        settings = self.page().settings()
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True
        )
        profile = self.page().profile()
        profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.NoCache)

        html_path = _RESOURCES_DIR / "sld_powsybl.html"
        self.load(QUrl.fromLocalFile(str(html_path)))

    # ------------------------------------------------------------------
    # Python -> JavaScript helpers
    # ------------------------------------------------------------------

    def _run_js(self, script: str):
        self.page().runJavaScript(script)

    def render_powsybl_svg(self, svg: str):
        """Display a PowSyBl SVG string (operational values, if any, are
        already baked into the SVG at generation time)."""
        self._run_js(f"renderPowsyblSvg({json.dumps(svg)})")

    def select_element(self, element_type: str, element_id: str):
        """Highlight an element by its PowSyBl id (from tree selection)."""
        self._run_js(
            f"highlightElement({json.dumps(element_type)}, {json.dumps(element_id)})")

    def clear_selection(self):
        self._run_js("clearSelection()")

    def fit_view(self):
        self._run_js("fitView()")

    def export_svg(self):
        """Ask the page to serialize the current SVG back via the bridge."""
        self._run_js("exportSvg()")

    # ------------------------------------------------------------------
    # Legacy no-ops (operational / contingency / theme / labels are now
    # baked into the SVG at generation time; kept so older call sites
    # degrade gracefully during the migration).
    # ------------------------------------------------------------------

    def render_graph(self, _elk_graph_json: str):
        pass

    def update_operational_data(self, _snapshot_json: str):
        pass

    def clear_operational_data(self):
        pass

    def update_contingency_data(self, _contingency_json: str):
        pass

    def clear_contingency_data(self):
        pass

    def update_theme(self, _colors_json: str):
        pass

    def toggle_labels(self, _show: bool):
        pass
