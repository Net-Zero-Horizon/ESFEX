"""Deleting a system must remove it from state and the element tree.

Regression for the Grid Builder flow: the wizard renames the system after
creating it, and the tree item's stored id used to be left out of sync by the
rename, so deleting it targeted a missing key, raised KeyError, and left the
system in the tree.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def main_window(qapp, monkeypatch):
    # Auto-confirm the delete confirmation dialog.
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *a, **k: QMessageBox.StandardButton.Yes)
    from esfex.visualization.main_window import MainWindow
    w = MainWindow()
    try:
        yield w
    finally:
        w.close()


def _two_systems(w, a, b):
    w._create_system_for_wizard(a)
    w._create_system_for_wizard(b)


def test_delete_normal_system(main_window):
    w = main_window
    _two_systems(w, "S0", "S1")
    w._on_delete_system("S0")
    assert "S0" not in w._all_states
    assert "S0" not in w.element_tree._system_items


def test_delete_renamed_system(main_window):
    """A renamed system (Grid Builder) must delete via its tree id."""
    w = main_window
    _two_systems(w, "A", "Keep")
    w._on_system_renamed("A", "Region1")

    # The tree item's stored id must follow the rename...
    item = w.element_tree._system_items["Region1"]
    assert item.data(0, 100) == ("system", "Region1")

    # ...so deleting via that id (what the context menu emits) works.
    eid = item.data(0, 100)[1]
    w._on_delete_system(eid)
    assert "Region1" not in w._all_states
    assert "Region1" not in w.element_tree._system_items
    assert "Keep" in w._all_states


def test_delete_unknown_id_is_noop(main_window):
    w = main_window
    _two_systems(w, "X", "Y")
    before = set(w._all_states)
    w._on_delete_system("GHOST")  # a stale id must not raise
    assert set(w._all_states) == before
    assert set(w.element_tree._system_items) == before


def test_rename_keeps_tree_item_id_in_sync(main_window):
    w = main_window
    _two_systems(w, "Old", "Keep")
    w._on_system_renamed("Old", "New")
    item = w.element_tree._system_items["New"]
    assert item.data(0, 100) == ("system", "New")
    assert "Old" not in w.element_tree._system_items
