"""A multi-circuit line's values are those of the whole line.

The Grid Builder (and the Studio validation merges) write a line of n
circuits with its total capacity and its total series impedance (one
circuit's divided by n), while the model multiplied the capacity by
``num_circuits`` and divided the reactance and resistance by it once more:
an n-circuit line carried n² times its rating at 1/n² of its reactance.
``capacity_mw`` and the impedances now describe the whole line everywhere
and ``num_circuits`` only divides a reactance derived from a per-km value.
"""

import sys

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator, _make_node_config  # noqa: E402

from esfex.config.schema import (  # noqa: E402
    BusConfig,
    DCPowerFlowConfig,
    NetworkReductionConfig,
    SystemConfig,
    TransmissionLineGeo,
)
from esfex.utils.per_unit import derived_reactance_pu, line_reactance_pu  # noqa: E402


def test_line_reactance_is_the_whole_line():
    dc = DCPowerFlowConfig()
    own = TransmissionLineGeo(from_node=0, to_node=1, reactance_pu=0.05, num_circuits=2)
    assert line_reactance_pu(own, 100.0, dc, 100.0) == 0.05
    # Derived from the per-km value of one circuit: divided over the circuits
    two = TransmissionLineGeo(from_node=0, to_node=1, voltage_kv=220.0, num_circuits=2)
    assert line_reactance_pu(two, 100.0, dc, 100.0) == pytest.approx(
        derived_reactance_pu(100.0, 220.0, dc, 100.0) / 2)


def _dispatch(lines, demand_mw, reduce=False):
    """Economic dispatch of a cheap unit at bus g feeding bus d (dear unit)."""
    from esfex.bridge.adapters import PowerSystemAdapter

    common = dict(ramp_up=[1.0], ramp_down=[1.0], degradation_rate=[0.0])
    cheap = _make_generator("Cheap", "Non-renewable", "Gas", [500.0], fuel_cost=[1.0],
                            bus_id_per_node={0: "g"}, **common)
    dear = _make_generator("Dear", "Non-renewable", "Diesel", [500.0], fuel_cost=[200.0],
                           bus_id_per_node={0: "d"}, **common)
    sysc = SystemConfig(
        name="c", nodes=_make_node_config(1),
        generators={"cheap": cheap, "dear": dear},
        buses=[BusConfig(bus_id="g", parent_node=0, demand_fraction=0.0, role="connection"),
               BusConfig(bus_id="d", parent_node=0, demand_fraction=1.0)],
        transmission_lines_geo=lines,
    )
    cfg = _make_esfex_config(sysc).model_copy(
        update={"network_reduction": NetworkReductionConfig(enabled=reduce)})
    ps = PowerSystemAdapter(
        config=cfg, system_config=sysc, demand=np.full((3, 1), demand_mw), hours=3,
        num_nodes=1, year=2025, base_year=2025, mode="economic_dispatch",
    )
    ps.build_model()
    assert ps.solve() == 1
    sol = ps.get_solution_values()
    gen = np.asarray(sol["gen_output"])          # [unit, node, hour]
    flows = np.asarray(sol["power_flow_by_line"], dtype=float)
    return gen, flows


def _line(lid, cap, x, n):
    return TransmissionLineGeo(line_id=lid, from_node=0, to_node=0, from_bus=0, to_bus=1,
                               capacity_mw=cap, reactance_pu=x, num_circuits=n)


@pytest.mark.julia
def test_two_circuit_line_carries_its_rating_not_twice_it():
    # 30 MW for both circuits together: the cheap unit is capped at 30 MW
    gen, flows = _dispatch([_line("L", 30.0, 0.05, 2)], demand_mw=100.0)
    assert gen[0, 0, 0] == pytest.approx(30.0, abs=1e-6)
    assert abs(flows[0][0]) == pytest.approx(30.0, abs=1e-6)


@pytest.mark.julia
@pytest.mark.parametrize("reduce", [False, True])
def test_parallel_lines_split_by_their_whole_line_reactance(reduce):
    # Two circuits of 0.1 p.u. (0.05 for the line) beside one circuit of
    # 0.1 p.u.: the double line takes 2/3 of the 90 MW
    gen, flows = _dispatch([_line("L2", 1000.0, 0.05, 2), _line("L1", 1000.0, 0.1, 1)],
                           demand_mw=90.0, reduce=reduce)
    assert gen[0, 0, 0] == pytest.approx(90.0, abs=1e-6)
    assert abs(flows[0][0]) == pytest.approx(60.0, abs=1e-4)
    assert abs(flows[1][0]) == pytest.approx(30.0, abs=1e-4)
