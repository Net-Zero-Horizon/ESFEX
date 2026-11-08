"""Per-node unit profiles of a multi-system run sit at their system's nodes.

A unit's availability (or reservoir inflow) file has one column per node of
its own system. It was read as if it covered the merged node set, so system
B's columns landed on system A's nodes, and B's own nodes got B's last column
repeated (the reader pads missing columns).
"""

import sys

import numpy as np

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator, _make_system_config  # noqa: E402
from test_zones import _make_battery  # noqa: E402

from esfex.bridge.converters import battery_availability_key  # noqa: E402
from esfex.config.schema import MetaNetworkConfig  # noqa: E402
from esfex.runner import Orchestrator  # noqa: E402


def _profile(tmp_path, name, cols):
    path = tmp_path / name
    np.savetxt(path, np.tile(np.asarray(cols, dtype=float), (8760, 1)), delimiter=",")
    return path.name


def _system(name, tmp_path, solar_cols, bat_cols, inflow_cols):
    solar = _make_generator("Solar", "Renewable", "Sun", [10.0, 20.0]).model_copy(
        update={"availability_file": _profile(tmp_path, f"{name}_sun.csv", solar_cols)})
    hydro = _make_generator("Hydro", "Renewable", "Water", [10.0, 10.0]).model_copy(
        update={"reservoir_capacity": [100.0, 100.0],
                "reservoir_inflow_file": _profile(tmp_path, f"{name}_inflow.csv",
                                                  inflow_cols)})
    bat = _make_battery("BESS", 2).model_copy(
        update={"availability_file": _profile(tmp_path, f"{name}_bat.csv", bat_cols)})
    return _make_system_config(num_nodes=2, name=name,
                               generators={"solar": solar, "hydro": hydro},
                               batteries={"bess": bat})


def _orchestrator(tmp_path):
    a = _system("A", tmp_path, [0.1, 0.2], [0.9, 0.8], [5.0, 6.0])
    b = _system("B", tmp_path, [0.3, 0.4], [0.7, 0.6], [7.0, 8.0])
    cfg = _make_esfex_config(a, systems={"A": a, "B": b},
                             meta_network=MetaNetworkConfig(systems=["A", "B"]))
    orch = Orchestrator(cfg, output_dir=tmp_path / "out")
    orch.config_path = tmp_path / "cfg.yaml"
    return orch


def test_each_system_profile_is_placed_at_its_own_nodes(tmp_path):
    orch = _orchestrator(tmp_path)
    avail = orch._preload_availability_profiles(4)
    # Generators: B's columns on B's nodes 2-3, A's on 0-1
    np.testing.assert_allclose(avail["A__solar"][0, :2], [0.1, 0.2])
    np.testing.assert_allclose(avail["B__solar"][0, 2:], [0.3, 0.4])
    # Batteries alike
    np.testing.assert_allclose(avail[battery_availability_key("A__bess")][0, :2], [0.9, 0.8])
    np.testing.assert_allclose(avail[battery_availability_key("B__bess")][0, 2:], [0.7, 0.6])
    # Reservoir inflows alike
    inflow = orch._preload_inflow_profiles(4)
    np.testing.assert_allclose(inflow["A__hydro"][0, :2], [5.0, 6.0])
    np.testing.assert_allclose(inflow["B__hydro"][0, 2:], [7.0, 8.0])


def test_a_file_shared_by_two_systems_fits_both(tmp_path):
    """The same two-column file used by A and B (e.g. by path from a virtual
    unit) holds A's read at A's nodes and B's at B's."""
    shared = _profile(tmp_path, "shared.csv", [0.25, 0.75])
    a = _system("A", tmp_path, [0.1, 0.2], [0.9, 0.8], [5.0, 6.0])
    b = _system("B", tmp_path, [0.3, 0.4], [0.7, 0.6], [7.0, 8.0])
    for s in (a, b):
        s.generators["solar"] = s.generators["solar"].model_copy(
            update={"availability_file": shared})
    cfg = _make_esfex_config(a, systems={"A": a, "B": b},
                             meta_network=MetaNetworkConfig(systems=["A", "B"]))
    orch = Orchestrator(cfg, output_dir=tmp_path / "out")
    orch.config_path = tmp_path / "cfg.yaml"
    avail = orch._preload_availability_profiles(4)
    np.testing.assert_allclose(avail[shared][0], [0.25, 0.75, 0.25, 0.75])
    np.testing.assert_allclose(avail["B__solar"][0, 2:], [0.25, 0.75])
