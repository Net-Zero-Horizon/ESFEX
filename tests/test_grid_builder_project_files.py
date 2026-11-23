"""The files the Grid Builder writes belong to one system and one project.

A project's systems share ``demand/`` and ``availability/``. Two systems with
a node of the same name ("Node 18", as the naming leaves the nodes it finds no
place name for) wrote the same ``demand_Node_18.csv`` and both kept the demand
of the one forecast last. The same holds for generator ids such as
``gen_wind_4``. And none of the files went to the project: the wizard looked
for the project's path on its own window, found none and wrote to the
directory the Studio was started from.
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest


def _nodes(*names):
    return [SimpleNamespace(name=n, centroid_lat=18.0, centroid_lng=-77.0) for n in names]


def _result(n_nodes, level):
    series = np.full((8760, n_nodes), float(level))
    return SimpleNamespace(demand_multi_year=series, peak_mw=[float(level)] * n_nodes,
                           config=SimpleNamespace(base_year=2025))


def test_two_systems_with_the_same_node_name_keep_their_own_demand(tmp_path):
    pytest.importorskip("PySide6.QtWidgets")
    from esfex.visualization.workflows.grid_mapping_steps import forecast_node_demands

    out = tmp_path / "demand"
    first = forecast_node_demands(_nodes("Node 18", "Harbour"), _result(2, 10.0), out,
                               system="North")
    second = forecast_node_demands(_nodes("Node 18"), _result(1, 240.0), out,
                               system="South Island")
    first_path = first[0][1].csv_path
    second_path = second[0][1].csv_path
    assert first_path.endswith("demand_North_Node_18.csv")
    assert second_path.endswith("demand_South_Island_Node_18.csv")
    assert np.loadtxt(first_path).max() == 10.0      # not replaced by the later system
    assert np.loadtxt(second_path).max() == 240.0
    assert first[1][1].csv_path.endswith("demand_North_Harbour.csv")


def test_a_system_without_a_name_keeps_the_plain_file_name(tmp_path):
    pytest.importorskip("PySide6.QtWidgets")
    from esfex.visualization.workflows.grid_mapping_steps import forecast_node_demands

    res = forecast_node_demands(_nodes("Node 18"), _result(1, 5.0), tmp_path)
    assert res[0][1].csv_path.endswith("demand_Node_18.csv")


def test_two_systems_with_the_same_generator_id_keep_their_own_availability(tmp_path):
    from esfex.plugins.availability_generator import grid_builder_hook as gbh

    def state(name, lat):
        gen = SimpleNamespace(fuel="Fuel Oil", latitude=lat, longitude=-77.0,
                              rated_power=50.0, availability_file=None)
        return SimpleNamespace(name=name, generators={"gen_fuel_oil_3": gen}, nodes=[])

    one, two = state("North", 18.0), state("East", 18.5)
    gbh.generate_for_grid_build(one, tmp_path)
    gbh.generate_for_grid_build(two, tmp_path)
    a = one.generators["gen_fuel_oil_3"].availability_file
    b = two.generators["gen_fuel_oil_3"].availability_file
    assert a.endswith("North_gen_fuel_oil_3_availability.csv")
    assert b.endswith("East_gen_fuel_oil_3_availability.csv")
    assert a != b and len(list(tmp_path.glob("*.csv"))) == 2


def test_file_name_part():
    from esfex.utils.paths import file_name_part

    assert file_name_part("South Island") == "South_Island"
    assert file_name_part("Jagüey Grande / 2") == "Jag_ey_Grande_2"
    assert file_name_part("") == "" and file_name_part(None) == ""


def test_a_wizard_step_finds_the_project_folder_of_the_main_window(tmp_path):
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication, QDialog, QVBoxLayout, QWidget

    from esfex.visualization.workflows._wizard_utils import project_data_dir

    QApplication.instance() or QApplication([])
    main = QWidget()
    main._config_path = str(tmp_path / "proj" / "config.yaml")
    wizard = QDialog(main)                      # as the Studio opens its wizards
    step = QWidget()
    QVBoxLayout(wizard).addWidget(step)
    assert step.window() is wizard              # the step's window is the dialog
    assert project_data_dir(step) == str(tmp_path / "proj")

    # The folder the project was loaded from wins over the config path's folder.
    main._data_base_dir = lambda: str(tmp_path / "loaded")
    assert project_data_dir(step) == str(tmp_path / "loaded")

    # No project file: nothing to return (the caller uses the working directory).
    bare = QWidget()
    lone = QDialog(bare)
    inner = QWidget()
    QVBoxLayout(lone).addWidget(inner)
    assert project_data_dir(inner) is None
    for w in (wizard, main, lone, bare):
        w.deleteLater()


def test_the_demand_step_writes_into_the_project_with_the_system_name(tmp_path, monkeypatch):
    pytest.importorskip("PySide6.QtWidgets")
    import time

    # Without a project folder the step writes to the working directory:
    # keep a regression from writing into the repository.
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")

    from PySide6.QtWidgets import QApplication, QWidget

    from esfex.visualization.data.gui_model import GuiBus, GuiModel
    from esfex.visualization.workflows.grid_mapping_wizard import GridMappingWizard

    app = QApplication.instance() or QApplication([])
    main = QWidget()
    main._config_path = str(tmp_path / "config.yaml")     # the main window's, not the wizard's
    model = GuiModel()
    model.state.name = "North"
    while model.state.nodes:
        model.remove_node(len(model.state.nodes) - 1)
    idx = model.add_node("Node 18")
    model.update_node(idx, centroid_lat=18.0, centroid_lng=-77.0)
    model.state.buses = {"b0": GuiBus(bus_id="b0", parent_node=0, role="load",
                                      demand_fraction=1.0)}
    w = GridMappingWizard(None, model, parent=main)
    step = w._step_demand
    step._model = model
    step._forecast_result = SimpleNamespace(
        demand_multi_year=np.full((8760, 1), 7.0), peak_mw=[7.0], annual_gwh=[61.3],
        config=SimpleNamespace(base_year=2025))
    step._assignments = [{"bus_id": "b0", "new_fraction": 1.0}]
    busy = []
    step.busyChanged.connect(busy.append)
    step._apply_all()
    t0 = time.monotonic()
    while busy != [True, False]:
        app.processEvents()
        assert time.monotonic() - t0 < 60
        time.sleep(0.01)
    assert (tmp_path / "demand" / "demand_North_Node_18.csv").exists()
    assert model.state.nodes[0].demand.csv_path == str(tmp_path / "demand" / "demand_North_Node_18.csv")
    assert not (tmp_path / "cwd" / "demand").exists()
    w.deleteLater()
    main.deleteLater()
