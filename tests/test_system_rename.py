"""Renaming a system in the Studio (#17, #86).

The system name is a key in several places: the GUI state map, the
inter-system links, ``systems_to_simulate``, and the loaded base config
(``systems`` and ``meta_network``). A rename must reach all of them, so the
renamed system keeps its links and stays in Validate/Run, and renaming to a
name that already exists must be rejected cleanly.
"""

from __future__ import annotations

import copy
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import yaml

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from esfex.config.schema import ESFEXConfig, SystemLinkConfig
from esfex.visualization.data.gui_model import GuiInterSystemLink, GuiModel
from esfex.visualization.data.serializer import (
    config_to_global_settings,
    config_to_gui_states,
    config_to_inter_system_links,
    gui_state_to_yaml,
    rename_system_in_config,
)
from tests.test_serializer import _make_esfex_config


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _two_system_config() -> ESFEXConfig:
    config = _make_esfex_config(sys_name="Main")
    config.systems["Island"] = copy.deepcopy(config.systems["Main"])
    config.systems["Island"].name = "Island"
    config.systems["Main"].min_cycling_period_days = 11.0
    config.meta_network.systems = ["Main", "Island"]
    config.meta_network.systems_links = [
        SystemLinkConfig(
            systems=["Main", "Island"], connections=[[0, 1]],
            existing_capacity_MW=[50.0], max_investment_MW=[0.0],
            investment_cost_per_MW=[0.0], loss_factor=[0.01],
            distance_km=[40.0], cost_per_mw_km=[0.0],
            reactance_pu=[0.07], resistance_pu=[0.004],
        )
    ]
    return config


def test_model_rename_updates_links_and_selection(qapp):
    model = GuiModel()
    model.global_settings.systems_to_simulate = ["Main", "Island"]
    model.add_inter_system_link("transmission", "Main", "Island", 0, 1)
    updated = []
    model.interSystemLinkUpdated.connect(updated.append)

    model.rename_system_references("Main", "Mainland")

    lk = model.inter_system_links[0]
    assert (lk.from_system, lk.to_system) == ("Mainland", "Island")
    assert model.global_settings.systems_to_simulate == ["Mainland", "Island"]
    assert updated == [lk.link_id]


def test_rename_in_config_keeps_order_fields_and_links():
    config = _two_system_config()
    rename_system_in_config(config, "Main", "Mainland")
    assert list(config.systems) == ["Mainland", "Island"]
    assert config.systems["Mainland"].name == "Mainland"
    # Non-GUI fields stay on the renamed system.
    assert config.systems["Mainland"].min_cycling_period_days == 11.0
    assert config.meta_network.systems == ["Mainland", "Island"]
    assert config.meta_network.systems_links[0].systems == ["Mainland", "Island"]


def _rename_like_studio(config, states, model, old, new):
    """What MainWindow._on_system_renamed does to the data."""
    states = {(new if k == old else k): v for k, v in states.items()}
    states[new].name = new
    model.rename_system_references(old, new)
    rename_system_in_config(config, old, new)
    return states


def test_renamed_system_saves_and_reloads(qapp, tmp_path):
    config = _two_system_config()
    states = config_to_gui_states(config)
    model = GuiModel()
    model.global_settings = config_to_global_settings(config)
    for lk in config_to_inter_system_links(config):
        model.restore_inter_system_link(lk)

    states = _rename_like_studio(config, states, model, "Main", "Mainland")

    out = tmp_path / "renamed.yaml"
    gui_state_to_yaml(
        states, config, out,
        inter_system_links=model.inter_system_links,
        global_settings=model.global_settings,
    )
    data = yaml.safe_load(out.read_text())
    assert data["meta_network"]["systems"] == ["Mainland", "Island"]
    assert set(data["systems"]) == {"Mainland", "Island"}
    assert data["meta_network"]["systems_links"][0]["systems"] == ["Mainland", "Island"]
    assert data["systems"]["Mainland"]["min_cycling_period_days"] == 11.0
    # The saved file is a valid config again (validator checks meta_network).
    reloaded = ESFEXConfig(**data)
    assert reloaded.primary_system.name == "Mainland"


def test_stale_selection_never_reaches_meta_network(tmp_path):
    """A selection naming a missing system is dropped, not written."""
    config = _two_system_config()
    states = config_to_gui_states(config)
    gs = config_to_global_settings(config)
    gs.systems_to_simulate = ["Gone", "Island"]
    out = tmp_path / "stale.yaml"
    gui_state_to_yaml(states, config, out, global_settings=gs)
    data = yaml.safe_load(out.read_text())
    assert data["meta_network"]["systems"] == ["Island"]


def test_restore_link_keeps_impedance(qapp):
    """Reloading inter-system links keeps reactance/resistance (extra item)."""
    model = GuiModel()
    for lk in config_to_inter_system_links(_two_system_config()):
        model.restore_inter_system_link(lk)
    lk = model.inter_system_links[0]
    assert lk.reactance_pu == pytest.approx(0.07)
    assert lk.resistance_pu == pytest.approx(0.004)
    assert lk.distance_km == pytest.approx(40.0)


def test_restore_link_fresh_id(qapp):
    model = GuiModel()
    model.add_inter_system_link("transmission", "A", "B", 0, 0)  # islink_0
    link = GuiInterSystemLink("islink_0", "transmission", "A", "B", 1, 1)
    new_id = model.restore_inter_system_link(link, keep_id=False)
    assert new_id != "islink_0"
    assert len({lk.link_id for lk in model.inter_system_links}) == 2


def _fake_window(model, states, current):
    from esfex.visualization.main_window import MainWindow

    calls = SimpleNamespace(tree=[], form_reload=0, warnings=[])
    fake = SimpleNamespace(
        model=model,
        _all_states=states,
        _current_system_name=current,
        _loaded_config=None,
        _system_form=SimpleNamespace(
            load_element=lambda: setattr(calls, "form_reload", calls.form_reload + 1)),
        element_tree=SimpleNamespace(
            rename_system=lambda o, n: calls.tree.append((o, n))),
        _sync_systems_to_form=lambda: None,
        _invalidate_validation=lambda: None,
    )
    fake._on_system_renamed = MainWindow._on_system_renamed.__get__(fake)
    return fake, calls


def test_rename_to_existing_name_is_rejected(qapp, monkeypatch):
    """Renaming onto an existing system warns and reverts (no AttributeError)."""
    from esfex.visualization import main_window as mw

    warnings = []
    monkeypatch.setattr(
        mw.QMessageBox, "warning", lambda *a, **k: warnings.append(a))
    config = _two_system_config()
    states = config_to_gui_states(config)
    model = GuiModel()
    model.load_state(states["Main"])
    # The system form has already written the new name into the state.
    model.state.name = "Island"
    fake, calls = _fake_window(model, states, "Main")

    fake._on_system_renamed("Main", "Island")

    assert warnings, "duplicate name must be reported"
    assert model.state.name == "Main"
    assert states["Main"].name == "Main"
    assert list(fake._all_states) == ["Main", "Island"]
    assert calls.form_reload == 1
    assert calls.tree == []


def test_rename_keeps_state_order(qapp):
    config = _two_system_config()
    states = config_to_gui_states(config)
    model = GuiModel()
    model.load_state(states["Main"])
    model.global_settings.systems_to_simulate = ["Main", "Island"]
    model.state.name = "Mainland"
    fake, calls = _fake_window(model, states, "Main")
    fake._loaded_config = config

    fake._on_system_renamed("Main", "Mainland")

    assert list(fake._all_states) == ["Mainland", "Island"]
    assert fake._current_system_name == "Mainland"
    assert model.global_settings.systems_to_simulate == ["Mainland", "Island"]
    assert list(config.systems) == ["Mainland", "Island"]
    assert calls.tree == [("Main", "Mainland")]
