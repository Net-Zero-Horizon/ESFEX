"""Bus properties form."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QWidget,
)

from esfex.visualization.data.gui_model import GuiModel
from esfex.visualization.i18n import tr
from esfex.visualization.panels.multi_edit import add_choices, combo_value, select_value
from esfex.visualization.panels.visual_style_widget import VisualStyleWidget


class BusForm(QWidget):
    """Property editor for an electrical bus."""

    busChanged = Signal(str)  # bus_id
    busDeleteRequested = Signal(str)  # bus_id

    def __init__(self, model: GuiModel, parent=None):
        super().__init__(parent)
        self._model = model
        self._current_id: str | None = None
        self._multi_ids: list[str] | None = None
        self._updating = False
        self._role_edited = False

        layout = QFormLayout(self)

        self._system_label = QLabel("")
        layout.addRow(tr("bus_form.system"), self._system_label)

        self._bus_id_label = QLabel("")
        layout.addRow(tr("bus_form.bus_id"), self._bus_id_label)

        self._name = QLineEdit()
        self._name.editingFinished.connect(self._on_changed)
        layout.addRow(tr("bus_form.name"), self._name)

        self._parent_node = QComboBox()
        self._parent_node.currentIndexChanged.connect(self._on_changed)
        layout.addRow(tr("bus_form.parent_node"), self._parent_node)

        self._voltage_kv = QDoubleSpinBox()
        self._voltage_kv.setRange(0.1, 2000)
        self._voltage_kv.editingFinished.connect(self._on_changed)
        layout.addRow(tr("bus_form.voltage"), self._voltage_kv)

        self._frequency_hz = QDoubleSpinBox()
        self._frequency_hz.setRange(0.1, 400)
        self._frequency_hz.editingFinished.connect(self._on_changed)
        layout.addRow(tr("bus_form.frequency"), self._frequency_hz)

        self._current_type = QComboBox()
        add_choices(self._current_type, [("AC", "AC"), ("DC", "DC")])
        self._current_type.currentIndexChanged.connect(self._on_changed)
        layout.addRow(tr("bus_form.current_type"), self._current_type)

        self._bus_type = QComboBox()
        add_choices(self._bus_type, [
            ("PQ", "PQ"), ("PV", "PV"), ("slack", tr("bus_form.bus_type_slack")),
        ])
        self._bus_type.currentIndexChanged.connect(self._on_bus_type_changed)
        layout.addRow(tr("bus_form.bus_type"), self._bus_type)

        # Semantic role: drives whether the bus participates in demand-side
        # KCL terms (load_shed, reserves, demand). Connection buses force
        # demand_fraction = 0.
        self._role = QComboBox()
        add_choices(self._role, [
            ("connection", tr("bus_form.role_connection")),
            ("load", tr("bus_form.role_load")),
            ("mixed", tr("bus_form.role_mixed")),
        ])
        self._role.currentIndexChanged.connect(self._on_role_changed)
        layout.addRow(tr("bus_form.role"), self._role)

        self._demand_fraction = QDoubleSpinBox()
        self._demand_fraction.setRange(0.0, 1.0)
        self._demand_fraction.setDecimals(6)
        self._demand_fraction.setSingleStep(0.01)
        self._demand_fraction.editingFinished.connect(self._on_changed)
        layout.addRow(tr("bus_form.demand_fraction"), self._demand_fraction)

        # -- Appearance --
        self._style_widget = VisualStyleWidget(
            show_color=True, show_size=True,
        )
        self._style_widget.styleChanged.connect(self._on_changed)
        layout.addRow(self._style_widget)

    def _field_map(self):
        return [
            ("name", self._name),
            ("voltage_kv", self._voltage_kv),
            ("frequency_hz", self._frequency_hz),
            ("current_type", self._current_type),
            ("bus_type", self._bus_type),
            ("role", self._role),
            ("demand_fraction", self._demand_fraction),
        ]

    def load_element(self, element_id: str):
        self._multi_ids = None
        bus = self._model.state.buses.get(element_id)
        if bus is None:
            return
        self._current_id = element_id
        self._updating = True

        self._system_label.setText(tr("bus_form.system_label", name=self._model.state.name))
        self._bus_id_label.setText(bus.bus_id)
        self._name.setText(bus.name)

        self._parent_node.clear()
        for n in self._model.state.nodes:
            self._parent_node.addItem(n.name, n.index)
        idx = self._parent_node.findData(bus.parent_node)
        if idx >= 0:
            self._parent_node.setCurrentIndex(idx)

        self._voltage_kv.setValue(bus.voltage_kv)
        self._frequency_hz.setValue(bus.frequency_hz)

        select_value(self._current_type, bus.current_type)
        select_value(self._bus_type, bus.bus_type)
        select_value(self._role, bus.role)

        self._demand_fraction.setValue(bus.demand_fraction)
        # Connection buses cannot carry demand — disable the field
        self._demand_fraction.setEnabled(bus.role != "connection")
        from esfex.visualization.data.default_colors import BUS
        self._style_widget.set_default_color(BUS)
        self._style_widget.load_style(bus.style)
        self._updating = False

    def load_elements(self, element_ids: list[str]):
        from esfex.visualization.panels.multi_edit import collect_attr, set_widget_value
        buses = self._model.state.buses
        instances = [buses[eid] for eid in element_ids if eid in buses]
        if not instances:
            return
        self._multi_ids = element_ids
        self._current_id = element_ids[0]
        self._system_label.setText(tr("bus_form.n_selected", count=len(element_ids)))
        self._bus_id_label.setText(tr("bus_form.multi_selection"))
        self._updating = True
        # Populate parent_node combo (needed for combo matching)
        self._parent_node.clear()
        for n in self._model.state.nodes:
            self._parent_node.addItem(n.name, n.index)
        for attr, w in self._field_map():
            set_widget_value(w, collect_attr(instances, attr))
        self._updating = False

    def _on_role_changed(self):
        """Enforce role/demand_fraction invariant: connection buses must have df=0."""
        if self._updating:
            return
        # A role picked here is the user's: the save-time role repair
        # (repair_bus_roles_and_demand) must keep it.
        self._role_edited = True
        new_role = combo_value(self._role)
        if new_role == "connection":
            self._updating = True
            self._demand_fraction.setValue(0.0)
            self._demand_fraction.setEnabled(False)
            self._updating = False
        else:
            self._demand_fraction.setEnabled(True)
        self._on_changed()

    def _on_bus_type_changed(self):
        """Persist bus_type; show an informational warning on a 2nd slack bus."""
        if not self._updating:
            self._warn_if_duplicate_slack()
        self._on_changed()

    def _warn_if_duplicate_slack(self):
        """Inform the user when another bus is already designated as slack."""
        if combo_value(self._bus_type) != "slack":
            return
        edited = set(self._multi_ids) if self._multi_ids else (
            {self._current_id} if self._current_id is not None else set()
        )
        others = [
            b for bid, b in self._model.state.buses.items()
            if bid not in edited and b.bus_type == "slack"
        ]
        if not others:
            return
        shown = ", ".join(b.name or b.bus_id for b in others[:5])
        if len(others) > 5:
            shown += ", ..."
        QMessageBox.information(
            self,
            tr("bus_form.slack_warn_title"),
            tr("bus_form.slack_warn_body", names=shown),
        )

    def _on_changed(self):
        if self._updating:
            return
        self._model.checkpoint()
        from esfex.visualization.panels.multi_edit import widget_is_mixed

        if self._multi_ids:
            buses = self._model.state.buses
            instances = [buses[eid] for eid in self._multi_ids if eid in buses]
        elif self._current_id is not None:
            bus = self._model.state.buses.get(self._current_id)
            instances = [bus] if bus else []
        else:
            return

        if not instances:
            return

        for attr, w in self._field_map():
            if widget_is_mixed(w):
                continue
            if isinstance(w, QLineEdit):
                val = w.text()
            elif isinstance(w, QComboBox):
                val = combo_value(w)
            else:
                val = w.value()
            for inst in instances:
                setattr(inst, attr, val)

        if self._role_edited and not widget_is_mixed(self._role):
            for inst in instances:
                inst.role_user_set = True
        self._role_edited = False

        # Parent node is special (uses data, not text)
        if not widget_is_mixed(self._parent_node):
            node_val = self._parent_node.currentData() or 0
            for inst in instances:
                inst.parent_node = node_val

        if not self._multi_ids:
            instances[0].style = self._style_widget.get_style()

        bus_id = self._current_id if self._current_id else ""
        self.busChanged.emit(bus_id)
