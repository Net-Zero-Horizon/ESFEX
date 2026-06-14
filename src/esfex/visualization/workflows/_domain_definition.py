"""Standard domain-definition control shared by every area-based Workflow.

A single two-column widget: the user defines the spatial domain either by
**drawing a polygon** on the map (left column) or by **applying an imported
GeoAsset** (right column). Both inputs are mutually exclusive — the most recent
action replaces the previous domain, both in state and on the map (the JS
``showDomainPolygon`` clears the prior outline first).

Drop it into a workflow's domain step and read ``get_polygon()`` /
``get_bounds()`` from it; both the precise polygon and its bounding box are
maintained so downstream fetches can coarse-filter by bbox and then clip to the
exact boundary.
"""

from __future__ import annotations

import json
import math

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from esfex.visualization.i18n import tr
from esfex.visualization.workflows._domain_geoasset_control import (
    GeoAssetDomainControl,
)
from esfex.visualization.workflows.geo_domain import domain_bounds


class DomainDefinitionWidget(QWidget):
    """Two-column polygon-or-GeoAsset domain selector.

    Emits ``domainChanged`` whenever the active domain changes. The domain is
    always a single ``list[(lat, lng)]`` ring plus its ``(S, W, N, E)`` bbox.
    """

    domainChanged = Signal()

    def __init__(self, map_widget, geo_assets_provider=None, parent=None):
        super().__init__(parent)
        self._map_widget = map_widget
        self._polygon: list[tuple[float, float]] = []
        self._bounds: tuple[float, float, float, float] | None = None
        self._awaiting = False

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        _equal = QSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

        # ── Left box: draw a polygon ──
        draw_group = QGroupBox(tr("domain.draw_group"))
        draw_group.setSizePolicy(_equal)
        draw_lay = QVBoxLayout(draw_group)
        self._btn_draw = QPushButton(tr("wizard_common.draw_domain"))
        self._btn_draw.clicked.connect(self._start_drawing)
        draw_lay.addWidget(self._btn_draw)
        self._status = QLabel(tr("domain.status_none"))
        self._status.setWordWrap(True)
        draw_lay.addWidget(self._status)
        draw_lay.addStretch()
        row.addWidget(draw_group, 1)

        # ── Right box: apply an imported GeoAsset (equal-sized rectangle) ──
        self._geo_ctl = GeoAssetDomainControl(geo_assets_provider)
        self._geo_ctl.setSizePolicy(_equal)
        self._geo_ctl.domainPicked.connect(self._on_geoasset_picked)
        row.addWidget(self._geo_ctl, 1)

        # Bridge wiring for polygon drawing.
        if self._map_widget is not None and hasattr(self._map_widget, "bridge"):
            bridge = self._map_widget.bridge
            if hasattr(bridge, "domainPolygonDrawn"):
                bridge.domainPolygonDrawn.connect(self._on_polygon_drawn)
            if hasattr(bridge, "modeReset"):
                bridge.modeReset.connect(self._on_cancelled)

    def showEvent(self, event):
        super().showEvent(event)
        self._geo_ctl.refresh()

    def refresh_assets(self):
        self._geo_ctl.refresh()

    # ------------------------------------------------------------------
    # Polygon drawing
    # ------------------------------------------------------------------
    def _start_drawing(self):
        if self._map_widget is None:
            self._status.setText(tr("domain.no_map"))
            return
        self._status.setText(tr("domain.draw_hint"))
        self._btn_draw.setEnabled(False)
        self._awaiting = True
        wizard = self.window()
        if wizard:
            wizard.showMinimized()
        self._map_widget.enable_domain_polygon_draw()

    def _on_cancelled(self):
        """ESC mid-draw — re-enable the button and restore the wizard."""
        if not self._awaiting:
            return
        self._awaiting = False
        self._btn_draw.setEnabled(True)
        if not self._bounds:
            self._status.setText(tr("domain.status_none"))
        wizard = self.window()
        if wizard and wizard.isMinimized():
            wizard.showNormal()
            wizard.raise_()
            wizard.activateWindow()

    def _on_polygon_drawn(self, geojson_str: str):
        self._awaiting = False
        self._btn_draw.setEnabled(True)
        try:
            data = json.loads(geojson_str)
            ring = data.get("geometry", {}).get("coordinates", [[]])[0]
        except (ValueError, IndexError, AttributeError):
            ring = None
        if not ring:
            self._status.setText(tr("domain.invalid"))
            self._restore_wizard()
            return

        # Leaflet returns longitudes outside [-180, 180] when the world has been
        # panned across a copy of the globe; normalize so the bbox stays valid.
        def _norm_lng(x: float) -> float:
            return ((float(x) + 180.0) % 360.0) - 180.0

        poly = [
            (max(-90.0, min(90.0, float(c[1]))), _norm_lng(c[0]))
            for c in ring
        ]
        self._set_domain(poly, source=None)
        self._restore_wizard()

    def _restore_wizard(self):
        wizard = self.window()
        if wizard:
            wizard.showNormal()
            wizard.raise_()
            wizard.activateWindow()

    # ------------------------------------------------------------------
    # GeoAsset
    # ------------------------------------------------------------------
    def _on_geoasset_picked(self, poly):
        # GeoAsset polygons are already valid WGS84 — do NOT re-normalize.
        name = self._geo_ctl._combo.currentText() if self._geo_ctl._combo.count() else ""
        self._set_domain(list(poly), source=name)

    # ------------------------------------------------------------------
    # Shared domain application (the single funnel → mutual exclusivity)
    # ------------------------------------------------------------------
    def _set_domain(self, poly, source):
        if not poly or len(poly) < 3:
            return
        self._polygon = list(poly)
        self._bounds = domain_bounds(self._polygon)
        if self._map_widget is not None and hasattr(self._map_widget, "show_domain_polygon"):
            # showDomainPolygon clears the previous outline first → last wins.
            self._map_widget.show_domain_polygon(self._polygon)
            if hasattr(self._map_widget, "fit_bounds"):
                s, w, n, e = self._bounds
                self._map_widget.fit_bounds(s, w, n, e)
        n = len(self._polygon)
        area = self._area_km2()
        if source:
            self._status.setText(
                tr("domain.status_geoasset").format(name=source, n=n, area=area)
            )
        else:
            self._status.setText(
                tr("domain.status_polygon").format(n=n, area=area)
            )
        self.domainChanged.emit()

    def _area_km2(self) -> float:
        if not self._bounds:
            return 0.0
        s, w, n, e = self._bounds
        lat_mid = (s + n) / 2.0
        lat_km = (n - s) * 111.32
        lon_km = (e - w) * 111.32 * math.cos(math.radians(lat_mid))
        return abs(lat_km * lon_km)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_polygon(self) -> list[tuple[float, float]]:
        return self._polygon

    def get_bounds(self) -> tuple[float, float, float, float] | None:
        return self._bounds

    def is_defined(self) -> bool:
        return self._bounds is not None
