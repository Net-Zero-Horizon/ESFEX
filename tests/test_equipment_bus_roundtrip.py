"""Regression: equipment→bus assignment must survive a save/load round-trip.

Grid-Builder networks are bus-level: every bus shares ``parent_node`` 0 and
every generator has ``node`` 0, with the real link carried by ``gen.bus``.
Generators serialize node-indexed with no bus reference, so before the
``_equipment_buses`` persistence a reload re-guessed each unit's bus by nearest
coordinate — scrambling assignments and stranding a few more generators on
every export/import cycle. This test pins the exact assignment across the
round-trip using a coordinate that would resolve to the WRONG bus.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from esfex.config.schema import (
    ESFEXConfig,
    MetaNetworkConfig,
    NodeConfig,
    SystemConfig,
)
from esfex.visualization.data.gui_model import (
    EndpointRef,
    GuiBus,
    GuiGeneratorInstance,
    GuiSystemState,
    GuiTransmissionLine,
)
from esfex.visualization.data.serializer import (
    config_to_gui_states,
    gui_state_to_yaml,
)


def _bus(bid, lat, lng):
    return GuiBus(bus_id=bid, name=bid, parent_node=0, voltage_kv=220.0,
                  latitude=lat, longitude=lng)


def _default_config(name):
    return ESFEXConfig(
        meta_network=MetaNetworkConfig(systems=[name]),
        systems={name: SystemConfig(
            name=name,
            nodes=NodeConfig(num_nodes=1, nodes_connections=[0.0]),
            fuel_transport_distances=[[0.0]],
            fuels={},
        )},
    )


def test_generator_bus_survives_roundtrip(tmp_path):
    name = "BusLevel"
    state = GuiSystemState(
        name=name,
        buses={
            "bus_0": _bus("bus_0", 0.0, 0.0),
            "bus_1": _bus("bus_1", 0.0, 1.0),
            "bus_2": _bus("bus_2", 0.0, 2.0),
        },
        generators={
            "wind_n0": GuiGeneratorInstance(
                instance_id="wind_n0", unit_key="wind", name="Far Wind",
                gen_type="Renewable", fuel="Wind", node=0, rated_power=40.0,
                # On bus_2, but sitting right next to bus_0 — nearest-coordinate
                # resolution would wrongly snap it to bus_0.
                bus="bus_2", latitude=0.0, longitude=0.05,
            ),
        },
        transmission_lines=[
            GuiTransmissionLine(line_id="l0", from_bus="bus_0", to_bus="bus_1",
                                capacity_mw=100.0,
                                from_endpoint=EndpointRef("bus", "bus_0"),
                                to_endpoint=EndpointRef("bus", "bus_1")),
            GuiTransmissionLine(line_id="l1", from_bus="bus_1", to_bus="bus_2",
                                capacity_mw=100.0,
                                from_endpoint=EndpointRef("bus", "bus_1"),
                                to_endpoint=EndpointRef("bus", "bus_2")),
        ],
    )

    out = tmp_path / "p.yaml"
    gui_state_to_yaml({name: state}, base_config=_default_config(name),
                      output_path=out)
    from esfex.config.loader import load_config
    reloaded = config_to_gui_states(load_config(out), base_dir=str(tmp_path))[name]

    gens = reloaded.generators
    assert len(gens) == 1
    gen = next(iter(gens.values()))
    assert gen.bus == "bus_2", (
        f"generator bus scrambled on reload: got {gen.bus!r}, expected 'bus_2'"
    )


def test_roundtrip_is_idempotent(tmp_path):
    """Repeated export/import must not keep changing the assignment."""
    name = "BusLevel"
    state = GuiSystemState(
        name=name,
        buses={f"bus_{i}": _bus(f"bus_{i}", 0.0, float(i)) for i in range(4)},
        generators={
            "wind_n0": GuiGeneratorInstance(
                instance_id="wind_n0", unit_key="wind", name="W", fuel="Wind",
                gen_type="Renewable", node=0, rated_power=10.0,
                bus="bus_3", latitude=0.0, longitude=0.05),
        },
        transmission_lines=[
            GuiTransmissionLine(line_id=f"l{i}", from_bus=f"bus_{i}",
                                to_bus=f"bus_{i+1}", capacity_mw=50.0,
                                from_endpoint=EndpointRef("bus", f"bus_{i}"),
                                to_endpoint=EndpointRef("bus", f"bus_{i+1}"))
            for i in range(3)
        ],
    )
    from esfex.config.loader import load_config
    cur = state
    cfg = _default_config(name)
    seen = []
    for i in range(3):
        out = tmp_path / f"p{i}.yaml"
        gui_state_to_yaml({name: cur}, base_config=cfg, output_path=out)
        cfg = load_config(out)
        cur = config_to_gui_states(cfg, base_dir=str(tmp_path))[name]
        seen.append(next(iter(cur.generators.values())).bus)
    assert seen == ["bus_3", "bus_3", "bus_3"], seen
