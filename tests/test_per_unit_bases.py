"""Per-unit bases of the DC network (#109) and the master's node links (#110).

``dc_power_flow.base_impedance`` served both as Z_base (Ω), to turn
length × reactance_per_km into p.u., and as S_base (MVA), to turn p.u. into
MW in the flow and loss terms. Line data from the Grid Builder is per unit on
100 MVA and the line's own voltage, so a derived reactance used the wrong
base at every voltage but 100 kV. S_base is now ``ac_power_flow.base_mva`` and
Z_base = V² / S_base per line.

The master's node links took their reactance from fuel_transport_distances
(or 0.01), not from the lines they stand for, so its flow split in meshed
node networks did not follow the grid.
"""

import sys

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator, _make_system_config  # noqa: E402

from esfex.config.schema import (  # noqa: E402
    ACPowerFlowConfig,
    BusConfig,
    DCPowerFlowConfig,
    PenaltiesConfig,
    TemporalConfig,
    TransmissionLineGeo,
)
from esfex.runner import Orchestrator  # noqa: E402
from esfex.utils.per_unit import derived_reactance_pu  # noqa: E402


def test_derived_reactance_uses_line_voltage_and_power_base():
    dc = DCPowerFlowConfig()   # 0.4 Ω/km, 220 kV
    # 100 km, 40 Ω: 220 kV on 100 MVA → Z_base 484 Ω
    assert derived_reactance_pu(100.0, None, dc, 100.0) == pytest.approx(40.0 / 484.0)
    # 34.5 kV line → Z_base 11.9 Ω
    assert derived_reactance_pu(100.0, 34.5, dc, 100.0) == pytest.approx(40.0 / (34.5**2 / 100))
    # Doubling S_base doubles the p.u. value
    assert derived_reactance_pu(100.0, 34.5, dc, 200.0) == pytest.approx(2 * 40.0 / (34.5**2 / 100))
    assert derived_reactance_pu(0.0, 220.0, dc, 100.0) == 0.01


def test_base_impedance_is_ignored_with_a_warning(caplog):
    with caplog.at_level("WARNING"):
        DCPowerFlowConfig(base_impedance=529.0)
    assert "base_impedance" in caplog.text
    caplog.clear()
    with caplog.at_level("WARNING"):
        DCPowerFlowConfig()
    assert caplog.text == ""


@pytest.mark.julia
def test_julia_network_gets_power_base_and_line_reactance():
    from esfex.bridge.converters import convert_network_config
    from esfex.bridge.julia_setup import get_julia

    sysc = _make_system_config(num_nodes=2, generators={})
    line = TransmissionLineGeo(line_id="L", from_node=0, to_node=1, from_bus=0, to_bus=1,
                               capacity_mw=100.0, length_km=50.0, voltage_kv=110.0)
    net = convert_network_config(sysc.nodes, DCPowerFlowConfig(), transmission_lines_geo=[line],
                                 s_base_mva=250.0)
    jl = get_julia()
    jl._net = net
    assert float(jl.seval("_net.s_base")) == 250.0
    assert float(jl.seval("_net.transmission_lines[1].reactance_pu")) == pytest.approx(
        20.0 / (110.0**2 / 250.0))


def _line_losses(tmp_path, base_mva):
    """Mean MW lost on a 0.1 p.u. line carrying 80 MW (economic dispatch)."""
    demand = tmp_path / f"d{base_mva}.csv"
    load = np.zeros((8760, 2))
    load[:, 1] = 80.0
    np.savetxt(demand, load, delimiter=",")
    gen = _make_generator("Gas", "Non-renewable", "Gas", [300.0, 0.0], min_power=[0.0, 0.0],
                          degradation_rate=[0.0, 0.0])
    sysc = _make_system_config(num_nodes=2, generators={"gas": gen})
    sysc = sysc.model_copy(update={
        "demand_path": str(demand),
        "buses": [BusConfig(bus_id="b0", parent_node=0), BusConfig(bus_id="b1", parent_node=1)],
        "transmission_lines_geo": [TransmissionLineGeo(
            line_id="L", from_node=0, to_node=1, from_bus=0, to_bus=1, capacity_mw=200.0,
            reactance_pu=0.2, resistance_pu=0.1)],
        "dc_power_flow": DCPowerFlowConfig(loss_model="pwl", pwl_loss_segments=8),
        "ac_power_flow": ACPowerFlowConfig(base_mva=base_mva),
        "penalties": PenaltiesConfig(loss_of_load=10000.0)})
    cfg = _make_esfex_config(
        sysc, simulation_mode="economic_dispatch",
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720, overlap_hours=0))
    res = Orchestrator(cfg, output_dir=tmp_path / f"o{base_mva}").run(years=1)[0]
    gen = np.asarray(res.gen_output, dtype=float)    # [unit, node, step] MW
    return float(np.nansum(gen, axis=(0, 1)).mean()) - 80.0


@pytest.mark.julia
def test_losses_follow_the_power_base(tmp_path):
    # loss = r · F² / S_base, F the sending-end flow (~84 MW): ~7 MW on
    # 100 MVA; on 200 MVA about half
    lost_100 = _line_losses(tmp_path, 100.0)
    lost_200 = _line_losses(tmp_path, 200.0)
    assert lost_100 == pytest.approx(7.0, rel=0.1)
    assert lost_200 / lost_100 == pytest.approx(0.5, rel=0.1)


def test_master_links_carry_the_equivalent_reactance():
    """Two parallel 0.1 p.u. lines join nodes 0 and 1; the master's link has
    X = 0.05 (it used 0.01 from the absent fuel distances)."""
    sysc = _make_system_config(num_nodes=2, generators={})
    sysc = sysc.model_copy(update={
        "buses": [BusConfig(bus_id="b0", parent_node=0), BusConfig(bus_id="b1", parent_node=1)],
        "transmission_lines_geo": [
            TransmissionLineGeo(line_id=f"L{k}", from_node=0, to_node=1, from_bus=0, to_bus=1,
                                capacity_mw=100.0, reactance_pu=0.1, resistance_pu=0.01)
            for k in range(2)]})
    master, _, _ = Orchestrator._merge_systems(_make_esfex_config(sysc))
    (link,) = master.transmission_lines_geo
    assert link.reactance_pu == pytest.approx(0.05)
    assert link.resistance_pu == pytest.approx(0.005)


# ── Per-line reactance_per_km (Studio line form field, was dropped) ──


def test_derived_reactance_uses_line_reactance_per_km():
    dc = DCPowerFlowConfig()   # 0.4 Ω/km
    assert derived_reactance_pu(100.0, 220.0, dc, 100.0, 0.3) == pytest.approx(30.0 / 484.0)
    # None / 0 fall back to dc_power_flow.reactance_per_km
    assert derived_reactance_pu(100.0, 220.0, dc, 100.0, None) == pytest.approx(40.0 / 484.0)
    assert derived_reactance_pu(100.0, 220.0, dc, 100.0, 0.0) == pytest.approx(40.0 / 484.0)


def test_line_reactance_per_km_reaches_julia_and_master():
    from esfex.bridge.converters import convert_network_config
    from esfex.bridge.julia_setup import get_julia

    sysc = _make_system_config(num_nodes=2, generators={})
    line = TransmissionLineGeo(line_id="L", from_node=0, to_node=1, from_bus=0, to_bus=1,
                               capacity_mw=100.0, length_km=50.0, voltage_kv=110.0,
                               reactance_per_km=0.25)
    net = convert_network_config(sysc.nodes, DCPowerFlowConfig(), transmission_lines_geo=[line],
                                 s_base_mva=100.0)
    jl = get_julia()
    jl._net = net
    assert float(jl.seval("_net.transmission_lines[1].reactance_pu")) == pytest.approx(
        12.5 / (110.0**2 / 100.0))

    sysc = sysc.model_copy(update={
        "buses": [BusConfig(bus_id="b0", parent_node=0), BusConfig(bus_id="b1", parent_node=1)],
        "transmission_lines_geo": [line]})
    master, _, _ = Orchestrator._merge_systems(_make_esfex_config(sysc))
    (link,) = master.transmission_lines_geo
    assert link.reactance_pu == pytest.approx(12.5 / (110.0**2 / 100.0))


def test_studio_line_reactance_per_km_round_trips(tmp_path):
    """The line form's Reactance per km is saved and read back (was dropped)."""
    pytest.importorskip("PySide6")
    import yaml

    from esfex.config.schema import ESFEXConfig
    from esfex.visualization.data.serializer import config_to_gui_states, gui_state_to_yaml

    sysc = _make_system_config(num_nodes=2, generators={})
    sysc = sysc.model_copy(update={
        "buses": [BusConfig(bus_id="b0", parent_node=0), BusConfig(bus_id="b1", parent_node=1)],
        "transmission_lines_geo": [TransmissionLineGeo(
            line_id="L", from_node=0, to_node=1, from_bus=0, to_bus=1,
            capacity_mw=100.0, length_km=50.0, voltage_kv=110.0, reactance_per_km=0.25)]})
    cfg = _make_esfex_config(sysc)
    states = config_to_gui_states(cfg)
    (ln,) = [x for x in next(iter(states.values())).transmission_lines if x.line_id == "L"]
    assert ln.reactance_per_km == pytest.approx(0.25)
    assert not hasattr(ln, "base_impedance")
    out = tmp_path / "rpk.yaml"
    gui_state_to_yaml(states, cfg, out)
    data = yaml.safe_load(out.read_text())
    (saved,) = [x for x in next(iter(data["systems"].values()))["transmission_lines_geo"]
                if x["line_id"] == "L"]
    assert saved["reactance_per_km"] == pytest.approx(0.25)
    assert "base_impedance" not in saved
    ESFEXConfig(**data)
