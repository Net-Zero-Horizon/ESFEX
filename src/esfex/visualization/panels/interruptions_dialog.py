"""Interruptions calendar — a Gantt/timeline editor for deterministic outages.

A dedicated dialog that hosts a D3 timeline (embedded QtWebEngine, offline —
no CDN) where each infrastructure element is a row and each scheduled
interruption is a draggable bar over ``[start_hour, end_hour)`` of the horizon
year. Reads/writes ``model.state.outage_schedule`` (list of GuiOutageWindow).

The Python↔JS contract is a tiny QWebChannel bridge:

* ``get_data()``  → JSON ``{base_year, horizon_hours, groups, schedule}``
* ``commit(json)`` → the page hands back the full edited schedule; the dialog
  applies it to the model on accept.
"""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal, Slot
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QVBoxLayout

from esfex.visualization.data.gui_model import GuiModel, GuiOutageWindow
from esfex.visualization.i18n import tr
from esfex.visualization.ui_scale import scaled

_RESOURCES_DIR = Path(__file__).parent.parent / "resources"

# Element categories exposed in the calendar (order = display order). Each maps
# a schedule element_type to a translated group label and a state collector.
_ELECTRICAL_CATEGORIES = (
    "generator", "battery", "line", "transformer",
    "acdc_converter", "freq_converter",
)


class _Bridge(QObject):
    """QWebChannel-exposed object the timeline page talks to."""

    committed = Signal(str)  # full schedule JSON, on every edit

    def __init__(self, payload: str, parent=None):
        super().__init__(parent)
        self._payload = payload

    @Slot(result=str)
    def get_data(self) -> str:
        return self._payload

    @Slot(str)
    def commit(self, schedule_json: str) -> None:
        self.committed.emit(schedule_json)


class InterruptionsDialog(QDialog):
    """Graphical calendar for scheduling deterministic service interruptions."""

    def __init__(self, model: GuiModel, parent=None, focus: "tuple | None" = None):
        super().__init__(parent)
        self.model = model
        self._schedule: list[dict] = []
        self._focus = focus  # (element_type, element_id) to preselect, or None

        self.setWindowTitle(tr("interruptions.title"))
        self.resize(scaled(1040), scaled(620))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._view = QWebEngineView(self)
        layout.addWidget(self._view, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # WebChannel bridge, seeded with the current data payload.
        self._bridge = _Bridge(self._build_payload(), self)
        self._bridge.committed.connect(self._on_committed)
        self._channel = QWebChannel(self._view.page())
        self._channel.registerObject("bridge", self._bridge)
        self._view.page().setWebChannel(self._channel)

        self._view.load(QUrl.fromLocalFile(str(_RESOURCES_DIR / "interruptions.html")))

    # ------------------------------------------------------------------
    # Data collection / apply
    # ------------------------------------------------------------------
    def _collect_groups(self) -> list[dict]:
        """Elements grouped by category, in display order. Empty groups drop."""
        s = self.model.state

        def _grp(category: str, label: str, items: list[dict]) -> "dict | None":
            return {"category": category, "label": label, "items": items} if items else None

        raw = [
            _grp("generator", tr("interruptions.cat_generators"),
                 [{"id": k, "label": getattr(g, "name", None) or k}
                  for k, g in s.generators.items()]),
            _grp("battery", tr("interruptions.cat_batteries"),
                 [{"id": k, "label": getattr(b, "name", None) or k}
                  for k, b in s.batteries.items()]),
            _grp("line", tr("interruptions.cat_lines"),
                 [{"id": ln.line_id, "label": ln.line_id}
                  for ln in s.transmission_lines if getattr(ln, "line_id", None)]),
            _grp("transformer", tr("interruptions.cat_transformers"),
                 [{"id": t.name, "label": t.name}
                  for t in s.transformers if getattr(t, "name", None)]),
            _grp("acdc_converter", tr("interruptions.cat_acdc"),
                 [{"id": c.name, "label": c.name}
                  for c in s.acdc_converters if getattr(c, "name", None)]),
            _grp("freq_converter", tr("interruptions.cat_freq"),
                 [{"id": c.name, "label": c.name}
                  for c in s.freq_converters if getattr(c, "name", None)]),
        ]
        return [g for g in raw if g]

    def _build_payload(self) -> str:
        s = self.model.state
        schedule = [
            {
                "element_type": ow.element_type,
                "element_id": ow.element_id,
                "start_hour": int(ow.start_hour),
                "end_hour": int(ow.end_hour),
                "availability": float(ow.availability),
                "label": ow.label,
            }
            for ow in getattr(s, "outage_schedule", [])
        ]
        focus = None
        if self._focus:
            focus = {"element_type": self._focus[0], "element_id": self._focus[1]}
        return json.dumps({
            "base_year": int(getattr(s, "base_year", 2025)),
            "horizon_hours": 8760,
            "groups": self._collect_groups(),
            "schedule": schedule,
            "focus": focus,
            "theme": self._theme_colors(),
            "i18n": {
                "add": tr("interruptions.add"),
                "delete": tr("interruptions.delete"),
                "availability": tr("interruptions.availability"),
                "label": tr("interruptions.label_field"),
                "start": tr("interruptions.start"),
                "end": tr("interruptions.end"),
                "empty": tr("interruptions.empty_hint"),
            },
        })

    def _theme_colors(self) -> dict:
        """Active GUI palette, so the embedded timeline matches the Studio theme
        (light/dark) instead of a hard-coded light look."""
        from esfex.visualization.theme import current_theme
        c = current_theme().colors
        return {
            "bg": c.surface_primary,
            "bg2": c.surface_secondary,
            "elevated": c.surface_elevated,
            "text": c.text_primary,
            "text2": c.text_secondary,
            "border": c.border_light,
            "accent": c.accent_primary,
            "danger": c.danger,
            "warn": c.status_warning,
            "selBg": c.selection_bg,
        }

    def _on_committed(self, schedule_json: str) -> None:
        try:
            self._schedule = json.loads(schedule_json) or []
        except (ValueError, TypeError):
            self._schedule = []

    def accept(self) -> None:  # noqa: D102
        self._apply_to_model()
        super().accept()

    def _apply_to_model(self) -> None:
        windows = []
        for d in self._schedule:
            try:
                start = int(d["start_hour"])
                end = int(d["end_hour"])
                if end <= start:
                    continue
                windows.append(GuiOutageWindow(
                    element_type=str(d["element_type"]),
                    element_id=str(d["element_id"]),
                    start_hour=start,
                    end_hour=end,
                    availability=max(0.0, min(1.0, float(d.get("availability", 0.0)))),
                    label=str(d.get("label", "")),
                ))
            except (KeyError, ValueError, TypeError):
                continue
        self.model.state.outage_schedule = windows
