"""Master investments keep their buses when ``network_reduction`` is on.

The reduction renumbers the buses it keeps (a pruned leaf shifts every later
bus down by one). The master's per-bus results came back on the reduced
numbering and were read as the system's own buses, and the dispatch read the
units built from them (per bus of the system) as reduced-bus arrays, so new
capacity landed on the wrong bus, even at another node. In a myopic run the
next year's master also got the earlier builds at the node's first bus, so it
built the same capacity again where it was needed.
"""

import sys
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_battery_soc_carryover import _reduced_system  # noqa: E402
from test_myopic_master_history import _system  # noqa: E402
from test_runner import _make_esfex_config, _make_generator  # noqa: E402

from esfex.bridge.adapters import _per_bus_unit_on_model_buses  # noqa: E402
from esfex.config.schema import (  # noqa: E402
    BusConfig,
    MasterProblemConfig,
    NetworkReductionConfig,
    TemporalConfig,
    TransmissionLineGeo,
)
from esfex.runner import Orchestrator  # noqa: E402


def _bus(bus_id, node, demand=0.0, role="load"):
    return BusConfig(bus_id=bus_id, parent_node=node, demand_fraction=demand, role=role)


def test_per_bus_unit_is_laid_out_on_the_reduced_buses():
    # Original buses 0..3; 0 and 2 are eliminated, 1 and 3 kept.
    buses = [_bus("x", 0, role="connection"), _bus("a", 0, 1.0),
             _bus("y", 1, role="connection"), _bus("b", 1, 1.0)]
    rm = SimpleNamespace(n_original_buses=4, n_reduced_buses=2,
                         retained_original_indices=[1, 3])
    unit = {"name": "Investment Gas", "_is_per_bus": True,
            "rated_power": [5.0, 30.0, 0.0, 20.0], "fuel_cost": [1.0, 2.0, 3.0, 4.0],
            "fuel": "Gas"}
    out = _per_bus_unit_on_model_buses(unit, rm, buses)
    # Capacity on the eliminated bus x moves to node 0's retained bus
    assert out["rated_power"] == [35.0, 20.0]
    assert out["fuel_cost"] == [2.0, 4.0]
    assert out["fuel"] == "Gas"
    # Node-level units and unreduced networks pass through
    node_unit = dict(unit, _is_per_bus=False)
    assert _per_bus_unit_on_model_buses(node_unit, rm, buses) is node_unit
    assert _per_bus_unit_on_model_buses(unit, None, buses) is unit


@pytest.mark.julia
def test_dispatch_places_a_built_unit_on_its_bus():
    """30 MW built on bus_0 (node 0, original index 1, reduced index 0)."""
    from esfex.bridge.adapters import PowerSystemAdapter

    cfg, sysc = _reduced_system()
    units = {"inv_gas": {"name": "Investment Gas", "type": "Non-renewable",
                         "fuel": "Gas", "rated_power": [0.0, 30.0, 0.0],
                         "fuel_cost": [0.0] * 3, "_is_per_bus": True}}
    ps = PowerSystemAdapter(
        config=cfg, system_config=sysc, demand=np.full((3, 2), 50.0), hours=3,
        num_nodes=2, year=2025, base_year=2025, mode="economic_dispatch",
        units_config=units)
    ps.build_model()
    assert ps.solve() == 1
    assert ps._reduction_map.retained_original_indices == [1, 2]
    out = np.asarray(ps.get_solution_values()["gen_output"])[-1, :, 0]
    assert out[1] == pytest.approx(30.0, abs=1e-6)   # bus_0, node 0
    assert out[2] == pytest.approx(0.0, abs=1e-6)    # was injected at node 1


def _island_with_buses(tmp_path, mode):
    """One node, three buses: bus_x is a passive leaf (pruned), bus_a has
    no load, bus_b has the load and the diesel; a-b is a 1 MW line, so new
    gas must be built on bus_b."""
    sysc = _system("Island", tmp_path)
    diesel = _make_generator("Diesel", "Non-renewable", "Diesel", [50.0],
                             initial_age=[3], degradation_rate=[0.0], fuel_cost=[300.0],
                             invest_max_power=[0.0], bus_id_per_node={0: "bus_b"})
    sysc = sysc.model_copy(update=dict(
        generators={"diesel": diesel},
        buses=[_bus("bus_x", 0, role="connection"), _bus("bus_a", 0),
               _bus("bus_b", 0, 1.0, role="mixed")],
        transmission_lines_geo=[
            TransmissionLineGeo(line_id="lx", from_node=0, to_node=0,
                                from_bus=0, to_bus=1, capacity_mw=200.0),
            TransmissionLineGeo(line_id="lab", from_node=0, to_node=0,
                                from_bus=1, to_bus=2, capacity_mw=1.0),
        ]))
    cfg = _make_esfex_config(
        sysc, simulation_mode="development",
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720,
                                overlap_hours=0),
        master_problem=MasterProblemConfig(planning_mode=mode),
    ).model_copy(update=dict(network_reduction=NetworkReductionConfig(enabled=True)))
    return Orchestrator(cfg, output_dir=tmp_path / "out")


@pytest.mark.julia
@pytest.mark.parametrize("mode", ["myopic", "perfect_foresight"])
def test_master_investments_on_a_reduced_network_keep_their_bus(tmp_path, mode):
    orch = _island_with_buses(tmp_path, mode)
    assert orch.master_system is orch.primary_system   # a bus-level master
    orch.run(years=2, master_only=True)
    sol = orch.master_solution
    y1, y2 = sorted(sol["investments"])
    # 60 MW of load, 50 MW of diesel that may retire: gas on bus_b (index 2)
    gas_y1 = sol["investments"][y1].get("tech_investment_power_0_2", 0.0)
    assert gas_y1 > 10.0
    assert not any(k.startswith("tech_investment_power_0_") and not k.endswith("_2")
                   for y in (y1, y2) for k in sol["investments"][y])
    caps = sol["cumulative_capacities"][y1]["tech"][1]
    assert list(caps[:2]) == [0.0, 0.0] and caps[2] == pytest.approx(gas_y1)
    # Year 2 tops up the 0.5 %/year degradation; it does not build again
    gas_y2 = sol["investments"][y2].get("tech_investment_power_0_2", 0.0)
    assert gas_y2 < 0.05 * gas_y1
    # The dispatch's unit sits on bus_b
    units = orch._build_config_from_cumulative({}, sol["cumulative_capacities"][y1])
    assert units["inv_gas_new"]["rated_power"] == pytest.approx([0.0, 0.0, gas_y1])


def test_myopic_builds_on_a_bus_level_master_stay_on_their_bus(tmp_path):
    orch = _island_with_buses(tmp_path, "myopic")
    built = {"tech": {}, "bat_tech_power": {}, "bat_tech_capacity": {}}
    orch._accumulate_myopic_investments(
        {"tech_investment_power_0_2": 40.0, "tech_investment_power_0_1": 5.0,
         "bat_tech_investment_power_0_2": 10.0,
         "bat_tech_investment_capacity_0_2": 40.0}, 0, built)
    assert list(built["tech"][("gas_new", 0)]) == [0.0, 5.0, 40.0]

    year_sys, built_gens, built_bats = orch._prepare_system_for_myopic_year(1, built)
    assert built_gens == [("gas_new__built_y1__bus_a", "gas_new"),
                          ("gas_new__built_y1__bus_b", "gas_new")]
    unit = year_sys.generators["gas_new__built_y1__bus_b"]
    assert unit.rated_power == [40.0] and unit.bus_id_per_node == {0: "bus_b"}
    assert year_sys.generators["gas_new__built_y1__bus_a"].rated_power == [5.0]
    # The horizon limit counts both, per node
    assert year_sys.technologies["gas_new"].invest_max_power == [455.0]
    assert built_bats == [("li__built_y1__bus_b", "li")]
    bat = year_sys.batteries["li__built_y1__bus_b"]
    assert bat.MaxDischargePower == [10.0] and bat.capacity == [40.0]
    assert bat.bus_id_per_node == {0: "bus_b"}
