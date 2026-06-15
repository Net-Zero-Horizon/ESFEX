"""Dialog to add/edit user-defined optimization constraints for a system.

Each constraint is a linear expression ``Σ coeff·variable  sense  rhs`` over the
model's decision variables, or a plugin-provided type. Variable indices are
written by name (generator/battery/technology) with ``all`` to sum over an axis,
e.g. ``gen_output`` index ``GasCC, all`` caps a generator over all hours.
"""

from __future__ import annotations

import copy

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from esfex.visualization.i18n import tr
from esfex.visualization.data.gui_model import (
    GuiCustomConstraint,
    GuiCustomConstraintTerm,
)

# Decision variables that can be referenced declaratively, grouped by target.
_OPERATIONAL_VARS = [
    "gen_output", "load_shed", "curtailment",
    "bat_charge", "bat_discharge", "bat_soc", "power_flow",
]
_INVESTMENT_VARS = [
    "tech_investment", "bat_tech_power_investment", "transfer_investment",
]


def _parse_index(text: str) -> list:
    """Parse a comma-separated index into names/ints/"all" entries."""
    out: list = []
    for raw in text.split(","):
        tok = raw.strip()
        if not tok:
            continue
        if tok.lower() == "all":
            out.append("all")
        else:
            try:
                out.append(int(tok))
            except ValueError:
                out.append(tok)
    return out


def _index_to_text(index: list) -> str:
    return ", ".join(str(x) for x in index)


class _ConstraintEditor(QDialog):
    """Edit a single constraint: header fields + a table of terms."""

    def __init__(self, cc: GuiCustomConstraint | None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("custom_constraints.edit_title"))
        self.resize(560, 420)
        self._cc = copy.deepcopy(cc) if cc else GuiCustomConstraint()

        layout = QVBoxLayout(self)

        form = QFormLayout()
        self._name = QLineEdit(self._cc.name)
        form.addRow(tr("custom_constraints.name"), self._name)

        self._target = QComboBox()
        self._target.addItems(["operational", "investment"])
        self._target.setCurrentText(self._cc.target)
        self._target.currentTextChanged.connect(self._refresh_var_choices)
        form.addRow(tr("custom_constraints.target"), self._target)

        self._sense = QComboBox()
        self._sense.addItems(["<=", ">=", "=="])
        self._sense.setCurrentText(self._cc.sense)
        form.addRow(tr("custom_constraints.sense"), self._sense)

        self._rhs = QDoubleSpinBox()
        self._rhs.setRange(-1e12, 1e12)
        self._rhs.setDecimals(4)
        self._rhs.setValue(self._cc.rhs)
        form.addRow(tr("custom_constraints.rhs"), self._rhs)
        layout.addLayout(form)

        layout.addWidget(QLabel(tr("custom_constraints.terms")))
        self._terms = QTableWidget(0, 3)
        self._terms.setHorizontalHeaderLabels([
            tr("custom_constraints.col_variable"),
            tr("custom_constraints.col_index"),
            tr("custom_constraints.col_coeff"),
        ])
        self._terms.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._terms, 1)

        hint = QLabel(tr("custom_constraints.index_hint"))
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray; font-style: italic;")
        layout.addWidget(hint)

        term_btns = QHBoxLayout()
        b_add = QPushButton(tr("custom_constraints.add_term"))
        b_add.clicked.connect(lambda: self._add_term_row())
        b_del = QPushButton(tr("custom_constraints.remove_term"))
        b_del.clicked.connect(self._remove_term_row)
        term_btns.addWidget(b_add)
        term_btns.addWidget(b_del)
        term_btns.addStretch()
        layout.addLayout(term_btns)

        for t in self._cc.terms:
            self._add_term_row(t)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

    def _var_choices(self) -> list[str]:
        return (_INVESTMENT_VARS if self._target.currentText() == "investment"
                else _OPERATIONAL_VARS)

    def _add_term_row(self, term: GuiCustomConstraintTerm | None = None):
        r = self._terms.rowCount()
        self._terms.insertRow(r)
        combo = QComboBox()
        combo.addItems(self._var_choices())
        if term and term.variable:
            if combo.findText(term.variable) < 0:
                combo.addItem(term.variable)
            combo.setCurrentText(term.variable)
        self._terms.setCellWidget(r, 0, combo)
        self._terms.setItem(
            r, 1, QTableWidgetItem(_index_to_text(term.index) if term else ""))
        coeff = QDoubleSpinBox()
        coeff.setRange(-1e9, 1e9)
        coeff.setDecimals(4)
        coeff.setValue(term.coefficient if term else 1.0)
        self._terms.setCellWidget(r, 2, coeff)

    def _remove_term_row(self):
        r = self._terms.currentRow()
        if r >= 0:
            self._terms.removeRow(r)

    def _refresh_var_choices(self):
        # Repopulate each term's variable combo for the new target.
        for r in range(self._terms.rowCount()):
            combo = self._terms.cellWidget(r, 0)
            cur = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(self._var_choices())
            if combo.findText(cur) < 0:
                combo.addItem(cur)
            combo.setCurrentText(cur)
            combo.blockSignals(False)

    def get(self) -> GuiCustomConstraint:
        terms = []
        for r in range(self._terms.rowCount()):
            variable = self._terms.cellWidget(r, 0).currentText()
            idx_item = self._terms.item(r, 1)
            index = _parse_index(idx_item.text() if idx_item else "")
            coeff = self._terms.cellWidget(r, 2).value()
            terms.append(GuiCustomConstraintTerm(
                variable=variable, index=index, coefficient=coeff))
        return GuiCustomConstraint(
            name=self._name.text().strip() or "constraint",
            type=self._cc.type,
            target=self._target.currentText(),
            sense=self._sense.currentText(),
            rhs=self._rhs.value(),
            terms=terms,
            params=dict(self._cc.params or {}),
        )


class CustomConstraintsDialog(QDialog):
    """List + add/edit/remove user constraints for the current system."""

    def __init__(self, constraints: list[GuiCustomConstraint], parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("custom_constraints.title"))
        self.resize(640, 380)
        self._items: list[GuiCustomConstraint] = copy.deepcopy(constraints or [])

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(tr("custom_constraints.intro")))

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels([
            tr("custom_constraints.name"),
            tr("custom_constraints.target"),
            tr("custom_constraints.summary"),
            tr("custom_constraints.rhs"),
        ])
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self._table.doubleClicked.connect(lambda *_: self._edit())
        layout.addWidget(self._table, 1)

        btns = QHBoxLayout()
        b_add = QPushButton(tr("custom_constraints.add"))
        b_add.clicked.connect(self._add)
        b_edit = QPushButton(tr("custom_constraints.edit"))
        b_edit.clicked.connect(self._edit)
        b_del = QPushButton(tr("custom_constraints.remove"))
        b_del.clicked.connect(self._remove)
        btns.addWidget(b_add)
        btns.addWidget(b_edit)
        btns.addWidget(b_del)
        btns.addStretch()
        layout.addLayout(btns)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

        self._refresh()

    @staticmethod
    def _summary(cc: GuiCustomConstraint) -> str:
        if cc.type != "linear":
            return f"[{cc.type}]"
        parts = [f"{t.coefficient:g}·{t.variable}[{_index_to_text(t.index)}]"
                 for t in cc.terms]
        return f"{' + '.join(parts)} {cc.sense}"

    def _refresh(self):
        self._table.setRowCount(0)
        for cc in self._items:
            r = self._table.rowCount()
            self._table.insertRow(r)
            self._table.setItem(r, 0, QTableWidgetItem(cc.name))
            self._table.setItem(r, 1, QTableWidgetItem(cc.target))
            self._table.setItem(r, 2, QTableWidgetItem(self._summary(cc)))
            self._table.setItem(r, 3, QTableWidgetItem(f"{cc.rhs:g}"))

    def _add(self):
        ed = _ConstraintEditor(None, self)
        if ed.exec() == QDialog.DialogCode.Accepted:
            self._items.append(ed.get())
            self._refresh()

    def _edit(self):
        r = self._table.currentRow()
        if r < 0:
            return
        ed = _ConstraintEditor(self._items[r], self)
        if ed.exec() == QDialog.DialogCode.Accepted:
            self._items[r] = ed.get()
            self._refresh()

    def _remove(self):
        r = self._table.currentRow()
        if r >= 0:
            del self._items[r]
            self._refresh()

    def result_constraints(self) -> list[GuiCustomConstraint]:
        return self._items
