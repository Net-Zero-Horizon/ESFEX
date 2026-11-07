"""The master has the terms the dispatch charges behind six stochastic keys (#89).

The master had no CO2 budget, no converters, no start-ups and no flow cost on
inter-system links, and battery maintenance had no multiplier of its own, so
the Studio keys ``penalty_co2_budget_violation``, ``converter_invest_cost``,
``converter_variable_cost``, ``gen_start_up_cost``,
``transmission_cost_per_mw_km`` and ``bat_maintenance_cost`` were dropped.
The model terms themselves are checked against the dispatch in the Julia
suite (runtests.jl, "master: ..."); these tests cover the plumbing and the
plan end to end.
"""

import sys
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator  # noqa: E402
from test_zones import _make_system_config, _make_technology  # noqa: E402

from esfex.config.schema import (  # noqa: E402
    ACDCConverterConfig,
    CO2BudgetConfig,
    FuelConfig,
    MasterProblemConfig,
    MetaNetworkConfig,
    PenaltiesConfig,
    StochasticScenarioConfig,
    SystemLinkConfig,
    TemporalConfig,
)
from esfex.runner import Orchestrator, _converter_investment_key, _link_flow_costs  # noqa: E402

_TEMPORAL = TemporalConfig(resolution_hours=24, rolling_horizon_hours=720, overlap_hours=0)


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

def test_converter_investment_key():
    assert _converter_investment_key("converter_investment_acdc_3") == ("acdc", 3)
    assert _converter_investment_key("converter_investment_freq_0") == ("freq", 0)
    assert _converter_investment_key("transfer_investment_0_1") == (None, -1)
    assert _converter_investment_key("converter_investment_xyz_1") == (None, -1)


def _conv(**kw):
    return ACDCConverterConfig(name="c1", from_node=0, to_node=1, from_bus=4, to_bus=7,
                               rated_power_mva=10.0, **kw)


def test_built_converter_capacity_reaches_the_dispatch():
    conv = _conv()
    orch = SimpleNamespace(primary_system=SimpleNamespace(acdc_converters=[conv],
                                                         freq_converters=[]))
    Orchestrator._apply_converter_investments(orch, {
        "converter_investment_acdc_0": 25.0, "tech_investment_power_0_0": 5.0})
    assert conv.rated_power_mva == pytest.approx(35.0)


def test_myopic_years_see_earlier_converter_builds():
    sysc = _make_system_config(num_nodes=2)
    sysc = sysc.model_copy(update={"acdc_converters": [_conv(invest_max_power=50.0)]})
    orch = SimpleNamespace(master_system=sysc)
    built = {"tech": {}, "bat_tech_power": {}, "bat_tech_capacity": {}}
    Orchestrator._accumulate_myopic_investments(
        orch, {"converter_investment_acdc_0": 20.0}, 0, built)
    year_sys, _, _ = Orchestrator._prepare_system_for_myopic_year(orch, 1, built)
    conv = year_sys.acdc_converters[0]
    assert (conv.rated_power_mva, conv.invest_max_power) == (30.0, 30.0)
    assert sysc.acdc_converters[0].rated_power_mva == 10.0   # the base is untouched


def test_master_converters_join_nodes():
    # The merged master has one bus per node: a converter's dispatch-network
    # bus indices would point at the wrong master buses.
    sysc = _make_system_config(num_nodes=2)
    sysc = sysc.model_copy(update={"acdc_converters": [_conv()]})
    master, op, _ = Orchestrator._merge_systems(_make_esfex_config(sysc))
    m, o = master.acdc_converters[0], op.acdc_converters[0]
    assert (m.from_bus, m.to_bus, m.from_node, m.to_node) == (None, None, 0, 1)
    assert (o.from_bus, o.to_bus) == (4, 7)


def _link(cost, dist=200.0, cap=100.0, conns=([0, 0],)):
    n = len(conns)
    return SystemLinkConfig(
        systems=["A", "B"], connections=[list(c) for c in conns],
        existing_capacity_MW=[cap] * n, max_investment_MW=[0.0] * n,
        investment_cost_per_MW=[0.0] * n, loss_factor=[0.0] * n,
        distance_km=[dist] * n, cost_per_mw_km=[cost] * n)


def test_link_flow_cost_per_master_corridor():
    offsets = {"A": 0, "B": 2}
    costs = _link_flow_costs([_link(0.5)], offsets)
    assert costs == {(0, 2): {"length_km": 200.0, "cost_per_mw_km": 0.5}}
    # Links without a cost add nothing
    assert _link_flow_costs([_link(0.0)], offsets) == {}


def test_merged_master_line_carries_the_link_flow_cost():
    # The master charges each line its own flow_cost, as the dispatch does
    # (it used to charge a per-bus-pair cost on every branch of the pair)
    from esfex.bridge.converters import line_flow_cost_per_mwh

    a = _make_system_config(num_nodes=1)
    a = a.model_copy(update={"name": "A"})
    b = a.model_copy(update={"name": "B"})
    cfg = _make_esfex_config(a, systems={"A": a, "B": b}, meta_network=MetaNetworkConfig(
        systems=["A", "B"], systems_links=[_link(0.5)]))
    master, _, _ = Orchestrator._merge_systems(cfg)
    (line,) = master.transmission_lines_geo
    assert line.length_km * line.cost_per_mw_km == pytest.approx(100.0)   # $/MWh
    assert line_flow_cost_per_mwh(line) == pytest.approx(100.0)


def test_master_co2_budget_groups():
    pytest.importorskip("juliacall")
    from esfex.bridge.adapters import _master_co2_budget_groups
    from esfex.bridge.converters import scale_cost
    from esfex.bridge.julia_setup import get_esfex_module

    ESFEX = get_esfex_module()
    sysc = _make_system_config(num_nodes=2)
    sysc = sysc.model_copy(update={
        "co2_budget": CO2BudgetConfig(enabled=True, annual_budget=1234.0),
        "penalties": PenaltiesConfig(co2_budget_violation=300.0)})
    (grp,) = list(_master_co2_budget_groups(sysc, ESFEX))
    assert list(grp.nodes) == [1, 2] and grp.budget == 1234.0
    assert grp.violation_penalty == pytest.approx(scale_cost(300.0))
    off = sysc.model_copy(update={"co2_budget": CO2BudgetConfig(enabled=False)})
    assert len(_master_co2_budget_groups(off, ESFEX)) == 0


def test_master_models_start_ups_only_when_the_dispatch_commits_units():
    from esfex.bridge.adapters import MasterProblemAdapter

    adapter = SimpleNamespace(esfex_config=_make_esfex_config(
        master_problem=MasterProblemConfig(use_uc_in_dispatch=True)))
    assert MasterProblemAdapter._dispatch_commits_units(adapter) is True
    adapter.esfex_config = _make_esfex_config()
    assert MasterProblemAdapter._dispatch_commits_units(adapter) is False
    adapter.esfex_config = None
    assert MasterProblemAdapter._dispatch_commits_units(adapter) is False


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------

def _gas_built_under_co2_budget(tmp_path, tag, scenarios=None):
    """MW of a cleaner, dearer gas plant built next to a diesel fleet whose
    emissions far exceed a 1000 t/year budget priced at 500 $/t."""
    demand = tmp_path / "demand.csv"
    np.savetxt(demand, np.full(8760, 60.0))
    diesel = _make_generator("Diesel", "Non-renewable", "Diesel", [150.0],
                             degradation_rate=[0.0], fuel_cost=[80.0], invest_max_power=[0.0])
    gas = _make_technology("gas_new", "Gas", 1, tech_type="Non-renewable",
                           invest_cost=200000.0, invest_max_power=500.0)
    gas = gas.model_copy(update={"fuel_cost": [150.0]})
    sysc = _make_system_config(num_nodes=1, coords=[(21.0, -80.0)],
                               generators={"diesel": diesel}, technologies={"gas_new": gas})
    sysc = sysc.model_copy(update={
        "demand_path": str(demand),
        "fuels": {
            "Diesel": FuelConfig(name="Diesel", emission_factor=0.8, price_base=80.0),
            "Gas": FuelConfig(name="Gas", emission_factor=0.0, price_base=150.0),
        },
        "co2_budget": CO2BudgetConfig(enabled=True, annual_budget=1000.0),
        "penalties": PenaltiesConfig(loss_of_load=10000.0, co2_cost=0.0,
                                     co2_budget_violation=500.0),
        "stochastic_scenarios": scenarios or []})
    cfg = _make_esfex_config(
        sysc, simulation_mode="development", temporal=_TEMPORAL,
        master_problem=MasterProblemConfig(stochastic=bool(scenarios)))
    results = Orchestrator(cfg, output_dir=tmp_path / tag).run(years=1)
    return results[0].investments.get("tech_investment_power_0_0", 0.0)


@pytest.mark.julia
def test_co2_budget_drives_the_plan(tmp_path):
    # Diesel emits ~1.8 t/MWh: at 500 $/t over budget it costs far more than
    # gas, which emits nothing. The master had no budget and built no gas.
    assert _gas_built_under_co2_budget(tmp_path, "budget") > 50.0
    cheap = [StochasticScenarioConfig(name="lax", probability=1.0,
                                      multipliers={"penalty_co2_budget_violation": 1e-4})]
    assert _gas_built_under_co2_budget(tmp_path, "lax", cheap) == pytest.approx(0.0, abs=1e-3)


def _converter_run(tmp_path, tag, scenarios=None):
    """Two nodes joined by a 10 MW link and an AC/DC converter (10 MW, up to
    100 MW more at 1000 $/MW); gas at node 0, 50 MW of load at node 1."""
    demand = tmp_path / "demand.csv"
    load = np.zeros((8760, 2))
    load[:, 1] = 50.0
    np.savetxt(demand, load, delimiter=",")
    gas = _make_generator("Gas", "Non-renewable", "Gas", [200.0, 0.0], degradation_rate=[0.0] * 2,
                          min_power=[0.0] * 2, invest_max_power=[0.0] * 2)
    sysc = _make_system_config(num_nodes=2, generators={"gas": gas})
    nodes = sysc.nodes.model_copy(update={
        "nodes_connections": [0.0, 10.0, 10.0, 0.0], "transference_invest_max": [0.0, 0.0],
        "reserve_static": [0.0, 0.0], "reserve_dynamic": [0.0, 0.0]})
    conv = ACDCConverterConfig(name="c1", from_node=0, to_node=1, rated_power_mva=10.0,
                               efficiency_rectify=1.0, efficiency_invert=1.0,
                               standby_losses_mw=0.0, invest_cost=1000.0,
                               invest_max_power=100.0)
    sysc = sysc.model_copy(update={
        "demand_path": str(demand), "nodes": nodes, "acdc_converters": [conv],
        "reserve_static_default_ratio": 0.0,
        "penalties": PenaltiesConfig(loss_of_load=10000.0),
        "stochastic_scenarios": scenarios or []})
    cfg = _make_esfex_config(
        sysc, simulation_mode="development", temporal=_TEMPORAL,
        master_problem=MasterProblemConfig(stochastic=bool(scenarios)))
    orch = Orchestrator(cfg, output_dir=tmp_path / tag)
    res = orch.run(years=1)[0]
    return (res.investments.get("converter_investment_acdc_0", 0.0),
            float(np.nansum(res.load_shed_array)))


@pytest.mark.julia
def test_master_builds_converter_capacity_the_dispatch_uses(tmp_path):
    built, shed = _converter_run(tmp_path, "base")
    # 50 MW of load: 10 MW over the link, 10 MW through the converter, 30 MW
    # built. Without it the dispatch would shed 30 MW all year (262 800 MWh);
    # what remains is the link's losses.
    assert built == pytest.approx(30.0, abs=0.5)
    assert shed < 1.0
    dear = [StochasticScenarioConfig(name="dear", probability=1.0,
                                     multipliers={"converter_invest_cost": 1e6})]
    built, shed = _converter_run(tmp_path, "dear", dear)
    assert built == pytest.approx(0.0, abs=1e-3)
    assert shed > 0.0
