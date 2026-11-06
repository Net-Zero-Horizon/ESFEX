"""Rolling-horizon battery SOC carry-over on multi-bus systems.

The boundary SOC comes from the solved window per *bus*; each battery's
per-node SOC must be read at the bus its capacity sits on, not at the bus
whose index equals the node index.
"""

import sys

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_generator, _make_node_config  # noqa: E402
from test_zones import _make_battery  # noqa: E402

from esfex.config.schema import BusConfig, SystemConfig, TransmissionLineGeo  # noqa: E402


def _system(bat_bus):
    """2 nodes, 3 buses: bus_2 is a second bus of node 0 (index 2 != node 0)."""
    bat = _make_battery("BESS", 2).model_copy(update=dict(
        capacity=[40.0, 0.0], MaxChargePower=[10.0, 0.0], MaxDischargePower=[10.0, 0.0],
        rated_power=[10.0, 0.0], max_DoD=[1.0, 1.0], soc_initial=[0.5, 0.0],
        degradation_rate=[0.0, 0.0], invest_max_power=[0.0, 0.0],
        invest_max_capacity=[0.0, 0.0], bus_id_per_node={0: bat_bus},
    ))
    gas = _make_generator("Gas", "Non-renewable", "Gas", [100.0, 100.0],
                          bus_id_per_node={0: "bus_0", 1: "bus_1"})
    return SystemConfig(
        name="soc", nodes=_make_node_config(2),
        generators={"gas": gas}, batteries={"bess": bat},
        buses=[BusConfig(bus_id="bus_0", parent_node=0, demand_fraction=1.0),
               BusConfig(bus_id="bus_1", parent_node=1, demand_fraction=1.0),
               BusConfig(bus_id="bus_2", parent_node=0, demand_fraction=0.0)],
        transmission_lines_geo=[
            TransmissionLineGeo(line_id="l0", from_node=0, to_node=1,
                                from_bus=0, to_bus=1, capacity_mw=200.0),
            TransmissionLineGeo(line_id="l1", from_node=0, to_node=0,
                                from_bus=2, to_bus=0, capacity_mw=200.0),
        ],
    )


@pytest.mark.julia
@pytest.mark.parametrize("bat_bus, bus_idx", [("bus_2", 2), ("bus_0", 0)])
def test_window_starts_from_inherited_soc_on_the_battery_bus(bat_bus, bus_idx):
    from esfex.bridge.adapters import PowerSystemAdapter

    hours = 4
    inherited = np.zeros((1, 3))
    inherited[0, bus_idx] = 12.0  # MWh left by the previous window
    ps = PowerSystemAdapter(
        config=_system(bat_bus), demand=np.full((hours, 2), 30.0), hours=hours,
        num_nodes=2, year=2025, base_year=2025, mode="economic_dispatch",
        boundary_conditions={"battery_soc": inherited},
    )
    ps.build_model()
    assert ps.solve() == 1
    soc = np.asarray(ps.get_solution_values()["bat_soc"])
    assert soc[0, bus_idx, 0] == pytest.approx(12.0, abs=1e-6)


@pytest.mark.julia
def test_inherited_energy_is_kept_when_capacity_is_degraded():
    """The carried SOC is energy (MWh): a degraded battery must not lose
    the capacity-degradation share of it at every window boundary."""
    from esfex.bridge.adapters import PowerSystemAdapter

    sysc = _system("bus_2")
    bat = sysc.batteries["bess"]
    sysc.batteries["bess"] = bat.model_copy(
        update=dict(degradation_rate=[0.02, 0.0], initial_age=[10, 0]))
    inherited = np.zeros((1, 3))
    inherited[0, 2] = 12.0
    ps = PowerSystemAdapter(
        config=sysc, demand=np.full((4, 2), 30.0), hours=4, num_nodes=2,
        year=2025, base_year=2025, mode="economic_dispatch",
        boundary_conditions={"battery_soc": inherited},
    )
    ps.build_model()
    assert ps.solve() == 1
    soc = np.asarray(ps.get_solution_values()["bat_soc"])
    assert soc[0, 2, 0] == pytest.approx(12.0, abs=1e-6)  # not 12 × 0.98**10


def _reduced_system():
    """A passive leaf bus at index 0 is pruned by network reduction, which
    shifts every other bus index down by one in the model."""
    from test_runner import _make_esfex_config

    from esfex.config.schema import NetworkReductionConfig

    gas = _make_generator("Gas", "Non-renewable", "Gas", [100.0, 100.0],
                          ramp_up=[0.1, 0.1], ramp_down=[0.1, 0.1],
                          degradation_rate=[0.0, 0.0],
                          bus_id_per_node={0: "bus_0", 1: "bus_1"})
    bat = _make_battery("BESS", 2).model_copy(update=dict(
        capacity=[40.0, 0.0], MaxChargePower=[10.0, 0.0], MaxDischargePower=[10.0, 0.0],
        rated_power=[10.0, 0.0], max_DoD=[1.0, 1.0], soc_initial=[0.5, 0.0],
        degradation_rate=[0.0, 0.0], invest_max_power=[0.0, 0.0],
        invest_max_capacity=[0.0, 0.0], bus_id_per_node={0: "bus_0"},
    ))
    sysc = SystemConfig(
        name="red", nodes=_make_node_config(2),
        generators={"gas": gas}, batteries={"bess": bat},
        buses=[BusConfig(bus_id="bus_x", parent_node=0, demand_fraction=0.0, role="connection"),
               BusConfig(bus_id="bus_0", parent_node=0, demand_fraction=1.0),
               BusConfig(bus_id="bus_1", parent_node=1, demand_fraction=1.0)],
        transmission_lines_geo=[
            TransmissionLineGeo(line_id="lx", from_node=0, to_node=0,
                                from_bus=0, to_bus=1, capacity_mw=200.0),
            TransmissionLineGeo(line_id="l0", from_node=0, to_node=1,
                                from_bus=1, to_bus=2, capacity_mw=200.0),
        ],
    )
    cfg = _make_esfex_config(sysc).model_copy(
        update=dict(network_reduction=NetworkReductionConfig(enabled=True)))
    return cfg, sysc


@pytest.mark.julia
def test_boundary_state_lands_on_reduced_buses():
    """Boundary state arrives on the original buses; with network reduction
    it must be mapped to the reduced model's bus numbering."""
    from esfex.bridge.adapters import PowerSystemAdapter

    cfg, sysc = _reduced_system()
    prev_out = np.zeros((1, 3))
    prev_out[0, 1] = 80.0   # gas at node 0 on bus_0 (original index 1)
    soc = np.zeros((1, 3))
    soc[0, 1] = 12.0        # battery on bus_0
    ps = PowerSystemAdapter(
        config=cfg, system_config=sysc, demand=np.full((3, 2), 50.0), hours=3,
        num_nodes=2, year=2025, base_year=2025, mode="economic_dispatch",
        boundary_conditions={"gen_output_prev": prev_out, "battery_soc": soc},
    )
    ps.build_model()
    assert ps.solve() == 1
    assert ps._reduction_map.n_reduced_buses == 2
    sol = ps.get_solution_values()
    # Ramp 10 %/h of 100 MW from 80 MW: the first hour is within [70, 90].
    assert 70.0 - 1e-6 <= np.asarray(sol["gen_output"])[0, 1, 0] <= 90.0 + 1e-6
    assert np.asarray(sol["bat_soc"])[0, 1, 0] == pytest.approx(12.0, abs=1e-6)
