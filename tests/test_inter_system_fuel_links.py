"""Inter-system fuel-route links stay fuel routes.

The Studio can draw a ``fuel_route`` link between two systems, but the
serializer saved every link as an electrical one (no type, no fuel) and
reloaded it as ``transmission``, and the merge turned it into a power line.
A fuel link now round-trips its type and fuel and becomes a pair of
primary-energy transport routes between the two systems' nodes.
"""

import sys

import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_system_config  # noqa: E402

from esfex.config.schema import MetaNetworkConfig, SystemLinkConfig  # noqa: E402
from esfex.runner import Orchestrator  # noqa: E402
from esfex.visualization.data import serializer as S  # noqa: E402
from esfex.visualization.data.gui_model import GuiInterSystemLink  # noqa: E402


def _fuel_link(**kw):
    d = dict(systems=["A", "B"], connections=[[1, 0]], link_type="fuel_route", fuel="Diesel",
             existing_capacity_MW=[300.0], max_investment_MW=[0.0],
             investment_cost_per_MW=[0.0], loss_factor=[0.002], distance_km=[90.0],
             cost_per_mw_km=[0.05])
    d.update(kw)
    return SystemLinkConfig(**d)


def _config(*links):
    a = _make_system_config(num_nodes=2, name="A")
    b = _make_system_config(num_nodes=1, name="B",
                            generators={"gas": _make_system_config().generators["gas"]})
    return _make_esfex_config(a, systems={"A": a, "B": b},
                              meta_network=MetaNetworkConfig(systems=["A", "B"],
                                                             systems_links=list(links)))


def test_fuel_link_round_trips_through_the_serializer():
    links = [
        GuiInterSystemLink(link_id="islink_0", link_type="fuel_route", fuel="Diesel",
                           from_system="A", to_system="B", from_node=1, to_node=0,
                           capacity_mw=300.0, distance_km=90.0),
        GuiInterSystemLink(link_id="islink_1", link_type="transmission",
                           from_system="A", to_system="B", from_node=0, to_node=0,
                           capacity_mw=150.0, distance_km=40.0),
    ]
    cd = {"meta_network": {}}
    S.inter_system_links_to_config_dict(links, cd)
    sls = cd["meta_network"]["systems_links"]
    # Type and fuel are per entry: the two links of the same pair split
    assert [(sl["link_type"], sl.get("fuel")) for sl in sls] == [
        ("fuel_route", "Diesel"), ("transmission", None)]

    meta = MetaNetworkConfig(systems=["A", "B"],
                             systems_links=[SystemLinkConfig(**sl) for sl in sls])

    class _Cfg:
        meta_network = meta

    back = sorted(S.config_to_inter_system_links(_Cfg()), key=lambda lk: lk.from_node)
    assert [(lk.link_type, lk.fuel, lk.from_node, lk.capacity_mw) for lk in back] == [
        ("transmission", "", 0, 150.0), ("fuel_route", "Diesel", 1, 300.0)]


def test_link_type_defaults_to_transmission():
    sl = SystemLinkConfig(systems=["A", "B"], connections=[[0, 0]], existing_capacity_MW=[1.0],
                          max_investment_MW=[0.0], investment_cost_per_MW=[0.0],
                          loss_factor=[0.0], distance_km=[1.0], cost_per_mw_km=[0.0])
    assert sl.link_type == "transmission" and sl.fuel is None


def test_fuel_link_becomes_fuel_routes_not_a_line():
    master, op, offsets = Orchestrator._merge_systems(_config(_fuel_link()))
    assert offsets == {"A": 0, "B": 2}
    # Not in the electrical network: no line, no adjacency, no transfer investment
    assert not any(ln.line_id.startswith("link_") for ln in op.transmission_lines_geo)
    assert {(ln.from_node, ln.to_node) for ln in master.transmission_lines_geo} == {(0, 1)}
    n = op.nodes.num_nodes
    assert op.nodes.nodes_connections[1 * n + 2] == 0.0
    assert op.nodes.nodes_connections[2 * n + 1] == 0.0
    # Two one-way routes between A's node 1 and B's node 0 (global node 2)
    routes = {r["route_id"]: r for r in op.fuel_transport_routes}
    assert set(routes) == {"link_A_B_0_fwd", "link_A_B_0_rev"}
    fwd, rev = routes["link_A_B_0_fwd"], routes["link_A_B_0_rev"]
    assert (fwd["from_node"], fwd["to_node"]) == (1, 2)
    assert (rev["from_node"], rev["to_node"]) == (2, 1)
    assert fwd["distance_km"] == 90.0
    assert fwd["fuel_params"] == {"Diesel": {"capacity": 300.0, "transport_cost": 0.05,
                                             "losses_fraction": 0.002}}
    assert master.fuel_transport_routes == op.fuel_transport_routes


def test_transmission_link_is_still_a_line():
    link = _fuel_link(link_type="transmission", fuel=None)
    _, op, _ = Orchestrator._merge_systems(_config(link))
    assert [ln.line_id for ln in op.transmission_lines_geo if ln.line_id.startswith("link_")] == [
        "link_A_B_0"]
    assert op.fuel_transport_routes == []


def test_fuel_link_without_fuel_is_ignored(caplog):
    _, op, _ = Orchestrator._merge_systems(_config(_fuel_link(fuel=None)))
    assert op.fuel_transport_routes == []
    assert not any(ln.line_id.startswith("link_") for ln in op.transmission_lines_geo)
    assert "has no fuel" in caplog.text


@pytest.mark.parametrize("field", ["max_investment_MW", "investment_cost_per_MW"])
def test_unused_investment_fields_warn(caplog, field):
    Orchestrator._merge_systems(_config(_fuel_link(**{field: [10.0]})))
    assert "not used" in caplog.text


@pytest.mark.julia
def test_fuel_link_routes_reach_the_primary_energy_block():
    from esfex.bridge.adapters import PrimaryEnergyAdapter
    from esfex.bridge.julia_setup import get_julia

    _, op, _ = Orchestrator._merge_systems(_config(_fuel_link()))
    pe = PrimaryEnergyAdapter(
        year=2025, base_year=2025, hours=48, num_nodes=3,
        fuels_config={"Diesel": {
            "max_availability": [100.0, 0.0, 0.0], "storage_capacity": [10.0, 10.0, 10.0],
            "initial_storage_level": [0.5, 0.5, 0.5], "import_cost": [0.0, 0.0, 0.0]}},
        non_electric_demand={}, infrastructure_config={}, transport_distances=[],
        transport_routes=op.fuel_transport_routes, generators=[],
        fuels_definition={"Diesel": {"price_base": 1.0, "energy_content": 10.0}},
        penalties_config={}, primary_energy_resolution=24)
    jl = get_julia()
    jl._pe_in = pe._create_julia_input()
    got = jl.seval('[(r.route_id, r.from_node, r.to_node, r.fuel_params["Diesel"].capacity)'
                   " for r in _pe_in.transport_routes]")
    assert [(str(i), int(f), int(t), float(c)) for i, f, t, c in got] == [
        ("link_A_B_0_fwd", 2, 3, 300.0), ("link_A_B_0_rev", 3, 2, 300.0)]
