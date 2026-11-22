"""A transformer whose ends sit in two nodes joins them in the master.

The master's node links take their capacity from ``nodes_connections`` and
their impedance from the bus-level branches between each node pair. Both
counted lines only: a node pair joined by a 220/110 kV transformer and a
110 kV line got the line's capacity alone, so the master planned with a
weaker network than the dispatch runs.
"""

import math

import pytest

from esfex.config.schema import DCPowerFlowConfig, TransformerConfig, TransmissionLineGeo
from esfex.runner import _node_link_impedances
from esfex.visualization.data import serializer
from esfex.visualization.data.gui_model import (
    GuiBus,
    GuiNode,
    GuiSystemState,
    GuiTransformer,
    GuiTransmissionLine,
)


def _trafo(a, b, z=0.1, losses=0.06, resistance=None):
    return TransformerConfig(name=f"T{a}{b}", from_node=a, to_node=b, from_bus=a, to_bus=b,
                             from_voltage_kv=220.0, to_voltage_kv=110.0,
                             rated_power_mva=300.0, impedance_pu=z,
                             losses_fraction=losses, resistance_pu=resistance)


def test_node_link_impedance_counts_transformers_between_nodes():
    dc = DCPowerFlowConfig()
    line = TransmissionLineGeo(from_node=0, to_node=1, from_bus=0, to_bus=1,
                               capacity_mw=100.0, resistance_pu=0.02, reactance_pu=0.1)
    # z = 0.1, r = 0.06·0.1 = 0.006, x = √(0.1² − 0.006²): the bridge's R and X
    x_t = math.sqrt(0.1 ** 2 - 0.006 ** 2)
    b = 1 / 0.1 + 1 / x_t
    r_eq = 0.02 * ((1 / 0.1) / b) ** 2 + 0.006 * ((1 / x_t) / b) ** 2
    assert _node_link_impedances([line], [], dc, 100.0, [_trafo(0, 1)]) == {
        (0, 1): pytest.approx((r_eq, 1 / b))}
    # A transformer alone joins its two nodes; its own resistance wins
    assert _node_link_impedances([], [], dc, 100.0, [_trafo(1, 0, resistance=0.01)]) == {
        (0, 1): pytest.approx((0.01, math.sqrt(0.1 ** 2 - 0.01 ** 2)))}
    # A transformer inside a node does not join nodes
    assert _node_link_impedances([], [], dc, 100.0, [_trafo(1, 1)]) == {}


def test_nodes_connections_sum_transformers_between_nodes():
    state = GuiSystemState(
        nodes=[GuiNode(index=0, name="A"), GuiNode(index=1, name="B")],
        buses={
            "a220": GuiBus(bus_id="a220", parent_node=0, voltage_kv=220.0),
            "a110": GuiBus(bus_id="a110", parent_node=0, voltage_kv=110.0),
            "b110": GuiBus(bus_id="b110", parent_node=1, voltage_kv=110.0),
        },
        transmission_lines=[GuiTransmissionLine(line_id="l0", from_bus="a110", to_bus="b110",
                                                capacity_mw=1200.0, voltage_kv=110.0)],
        transformers=[
            # Crosses from node A's 220 kV bus to node B's 110 kV bus
            GuiTransformer(name="T1", from_bus="a220", to_bus="b110", rated_power_mva=3000.0),
            # Inside node A: no inter-node capacity
            GuiTransformer(name="T2", from_bus="a220", to_bus="a110", rated_power_mva=500.0),
        ],
    )
    sys_dict: dict = {}
    serializer._apply_gui_state_to_dict(state, sys_dict)
    assert sys_dict["nodes"]["nodes_connections"] == [0.0, 4200.0, 4200.0, 0.0]
