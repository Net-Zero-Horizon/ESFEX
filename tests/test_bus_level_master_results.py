"""Results of a bus-level master are summed onto nodes.

A one-node system with several buses (or development zones) has a
bus-level master: ``tech_investment_power_{t}_{b}``,
``transfer_investment_{i}_{j}`` and the life-extension arrays are indexed by
bus. The results summary, the MGA export and the HDF5 investment attributes
(read by the results map and dashboard) took ``b`` as a node and dropped
every bus index >= the node count. The summary also labelled technology
investments with existing generators' names (dropping technologies past the
generator count), and a unit's retired share was the mean over all buses.
"""

import sys

import h5py
import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_results_hdf5_fixes import _init_h5  # noqa: E402
from test_runner import _make_esfex_config, _make_generator  # noqa: E402
from test_zones import _make_system_config, _make_technology  # noqa: E402

from esfex.config.schema import BusConfig  # noqa: E402
from esfex.runner import Orchestrator  # noqa: E402

# One node, three buses
BUSES = [BusConfig(bus_id=f"b{i}", parent_node=0, demand_fraction=f)
         for i, f in enumerate([1.0, 0.0, 0.0])]


def _orch(tmp_path, buses=BUSES):
    old = _make_generator("Old", "Non-renewable", "Diesel", [30.0])
    techs = {f"t{k}": _make_technology(f"Tech {k}", "Gas", 1, tech_type="Non-renewable")
             for k in range(3)}
    s = _make_system_config(num_nodes=1, generators={"old": old}, technologies=techs)
    orch = Orchestrator(_make_esfex_config(s.model_copy(update={"buses": buses})),
                        output_dir=tmp_path)
    orch._num_nodes = 1
    return orch


def test_master_index_is_a_bus_only_on_a_bus_level_master(tmp_path):
    assert [_orch(tmp_path)._master_index_node(b) for b in range(3)] == [0, 0, 0]
    node_level = _orch(tmp_path, buses=[BusConfig(bus_id="n0", parent_node=0)])
    assert node_level._master_index_node(2) == 2      # node-level: index = node


def test_investment_attributes_are_per_node(tmp_path):
    from esfex.runner import YearResults

    orch = _orch(tmp_path)
    orch._gen_names, orch._gen_fuels, orch._bat_names = ["Old"], ["Diesel"], []
    orch._bus_to_node = None
    path = _init_h5(orch, tmp_path, num_nodes=1)
    result = YearResults(year=2025, objective=1.0, solve_time=0.0, feasible=True, investments={
        "tech_investment_power_2_0": 10.0,
        "tech_investment_power_2_2": 7.0,      # bus 2: "node 2" did not exist
        "bat_tech_investment_power_0_1": 4.0,
        "transfer_investment_0_2": 9.0,        # between two buses of node 0
    })
    orch._append_year_to_hdf5(path, result)
    with h5py.File(path, "r") as h5:
        attrs = dict(h5["detailed_results/year_2025_threshold_0"].attrs)
    inv = {k: float(v) for k, v in attrs.items() if k.startswith("investment_")}
    assert inv == pytest.approx({
        "investment_tech_investment_power_2_0": 17.0,
        "investment_bat_tech_investment_power_0_0": 4.0,
    })


def test_summary_counts_every_bus_and_technology(tmp_path, capsys):
    from rich.console import Console

    import esfex.runner as runner_mod

    orch = _orch(tmp_path)
    orch._gen_names, orch._bat_names = ["Old"], []
    orch._base_demand = np.full((8760, 1), 10.0)
    orch._ev_demand = None
    result = {
        "solution": {2025: {"tech_investment": {
            "tech_investment_power_2_2": 7.0,      # technology 2 > generator count, bus 2
            "tech_investment_power_0_1": 3.0,
        }}},
        # "Old" past its lifetime at bus 1 only (the master's flag per bus)
        "gen_life_extension": {1: {1: np.array([1.0, 0.0, 1.0])}},
    }
    orig = runner_mod.console
    runner_mod.console = Console(width=250, force_terminal=False)
    try:
        orch._log_master_problem_details(result, [2025])
    finally:
        runner_mod.console = orig
    out = capsys.readouterr().out
    inv_table = out[out.index("Investments -"):out.index("Retirements -")]
    assert "Tech 2" in inv_table and "Tech 0" in inv_table and "Old" not in inv_table
    total = next(line for line in inv_table.splitlines() if "TOTAL" in line)
    assert "7" in total and "3" in total
    ret_table = out[out.index("Retirements -"):]
    node0 = next(line for line in ret_table.splitlines() if "Node 0" in line)
    assert "30" in node0


def test_retired_share_is_weighted_by_capacity(tmp_path):
    orch = _orch(tmp_path)
    # Node 0 (all of the unit's 30 MW) is past its lifetime at one of its
    # buses: the whole unit retires (it was the mean over the buses, 1/3)
    assert orch._retired_share(0, [1.0, 0.0, 1.0]) == pytest.approx(1.0)
    assert orch._retired_share(0, [1.0, 1.0, 1.0]) == pytest.approx(0.0)
