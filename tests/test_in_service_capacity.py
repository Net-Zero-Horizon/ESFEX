"""Capacity in service and unit configs in the results readers (bug sweep S12).

- The results file recorded the MW in service only for the units the master
  built: the dashboard and the financial-analysis fallback charged the fixed
  O&M of the existing units on their configured ``rated_power``, ignoring
  their retirement (end of life, life extension) and degradation. The export
  now records it for every unit.
- The System Metrics chart counted only the configured generators' rated
  power (its per-node mean) as installed capacity, not the MW the master
  built nor the retirements.
- The UC dispatch, net-load, ramp and storage charts, the dashboard's UC mix
  and the per-unit capex lists paired configs with datasets by position:
  h5py lists the datasets alphabetically and a master-built unit has no
  config, so a unit got another one's fuel.
- A candidate technology's per-node properties were read by position on the
  buses of a unit built from it when the node list was at least as long as
  the bus list.
"""

import sys
from types import SimpleNamespace

import h5py
import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator  # noqa: E402
from test_zones import (  # noqa: E402
    _make_battery, _make_battery_technology, _make_system_config, _make_technology)

from esfex.models import financial_analysis as fa  # noqa: E402
from esfex.runner import Orchestrator, YearResults  # noqa: E402

SC = "detailed_results/year_2025_threshold_0"


# ── runner export ─────────────────────────────────────────────────────

def test_existing_power_in_service_follows_the_dispatch():
    from esfex.runner import existing_power_in_service
    unit = {"rated_power": [100.0, 50.0, 80.0], "initial_age": [10, 29, 5],
            "life_time": [30, 30, 30], "degradation_rate": [0.01, 0.0, 0.0]}
    # Node 0 degraded at age 12, node 1 past its life at age 31, node 2 as is
    assert existing_power_in_service(unit, "rated_power", 3, 2) == pytest.approx(
        [100.0 * 0.99 ** 12, 0.0, 80.0])
    # Batteries: rated (not degraded) power while within the lifetime
    assert existing_power_in_service(unit, "rated_power", 3, 2, degrade=False) == [
        100.0, 0.0, 80.0]
    # After a master run: the master's MW, no degradation and no end of life
    master = {"rated_power": [60.0], "initial_age": [0], "life_time": [999],
              "degradation_rate": [0.0]}
    assert existing_power_in_service(master, "rated_power", 1, 40) == [60.0]


def _orch(tmp_path):
    old = _make_generator("Old", "Non-renewable", "Coal", [100.0], life_time=[20],
                          initial_age=[19], degradation_rate=[0.0])
    aged = _make_generator("Aged", "Non-renewable", "Gas", [50.0], life_time=[40],
                           initial_age=[10], degradation_rate=[0.02])
    bess = _make_battery("BESS", 1).model_copy(update=dict(
        MaxDischargePower=[10.0], life_time=[15], initial_age=[14]))
    sysc = _make_system_config(num_nodes=1, coords=[(21.0, -80.0)],
                               generators={"old": old, "aged": aged},
                               batteries={"bess": bess})
    orch = Orchestrator(_make_esfex_config(sysc), output_dir=tmp_path)
    orch._num_nodes = 1
    return orch


def test_export_records_the_mw_in_service_of_existing_units(tmp_path):
    orch = _orch(tmp_path)
    units = orch._gather_units()
    orch._rebuild_unit_names(units, year_offset=1)
    path = tmp_path / "out.h5"
    orch._bus_to_node = None
    orch._initialize_hdf5(path, num_nodes=1, num_years=1, start_year=2025, end_year=2026)
    H = 4
    orch._append_year_to_hdf5(path, YearResults(
        year=2026, objective=0.0, solve_time=0.0, feasible=True,
        gen_output=np.ones((2, 1, H)), bat_charge=np.ones((1, 1, H)),
        bat_discharge=np.ones((1, 1, H)), bat_soc=np.ones((1, 1, H))))
    with h5py.File(path, "r") as f:
        gen = f["detailed_results/year_2026_threshold_0/generation"]
        names = list(orch._gen_names)
        old = gen[orch._h5safe(names[[n.endswith("Old") for n in names].index(True)])]
        aged = gen[orch._h5safe(names[[n.endswith("Aged") for n in names].index(True)])]
        # Old reaches its life_time of 20 years in the second year
        np.testing.assert_allclose(old.attrs["rated_power"], [0.0])
        np.testing.assert_allclose(aged.attrs["rated_power"], [50.0 * 0.98 ** 11])
        dis = f["detailed_results/year_2026_threshold_0/battery_discharge"]
        bess = dis[list(dis)[0]]
        np.testing.assert_allclose(bess.attrs["rated_power"], [0.0])


def test_power_in_service_depends_on_the_dispatch_year(tmp_path):
    """Aged one more year: Old retires and Aged degrades."""
    orch = _orch(tmp_path)
    orch._rebuild_unit_names(orch._gather_units(), year_offset=0)
    first = [list(v) for v in orch._gen_in_service_mw]
    orch._rebuild_unit_names(orch._gather_units(), year_offset=1)
    assert first != [list(v) for v in orch._gen_in_service_mw]


# ── readers ───────────────────────────────────────────────────────────

def _file(path, *, in_service=True):
    """Configs in an order that does not match the alphabetical datasets:
    ``z_coal`` (100 MW, retired to 40 MW in service) and a master-built
    ``Investment Solar PV`` (10 MW, no config of its own)."""
    with h5py.File(path, "w") as f:
        f.attrs["temporal_resolution_hours"] = 1
        f.create_dataset("summary_results/year", data=[2025])
        sc = f.create_group("system_configuration")
        gens = sc.create_group("generators")
        gens.attrs["num_generators"] = 1
        gens.create_group("generator_0").attrs.update(
            name="z_coal", fuel="Coal", type="Non-renewable", fuel_cost=50.0,
            fixed_cost=8760.0, maintenance_cost=0.0, rated_power=100.0)
        techs = sc.create_group("technologies")
        techs.attrs["num_technologies"] = 1
        techs.create_group("technology_0").attrs.update(
            name="Solar PV", fuel="Sun", type="Renewable", invest_cost="[0.0]",
            fuel_cost="[0.0]", fixed_cost="[0.0]", maintenance_cost="[0.0]")
        bats = sc.create_group("batteries")
        bats.attrs["num_batteries"] = 2
        bats.create_group("battery_0").attrs.update(
            name="z_bess", capacity=10.0, MaxDischargePower=5.0, fixed_cost=0.0,
            maintenance_cost=0.0)
        bats.create_group("battery_1").attrs.update(
            name="a_bess", capacity=40.0, MaxDischargePower=20.0, fixed_cost=0.0,
            maintenance_cost=0.0)
        y = f.create_group(SC)
        y.attrs["year"] = 2025
        coal = y.create_dataset("generation/z_coal", data=np.full((1, 24), 30.0))
        pv = y.create_dataset("generation/Investment Solar PV",
                              data=np.tile(np.r_[np.zeros(12), np.full(12, 8.0)], (1, 1)))
        pv.attrs["rated_power"] = np.array([10.0])
        if in_service:
            coal.attrs["rated_power"] = np.array([40.0])
        y.create_dataset("demand", data=np.full((1, 24), 25.0))
        for key in ("battery_soc", "battery_charge", "battery_discharge"):
            for b in ("z_bess", "a_bess"):
                y.create_dataset(f"{key}/{b}", data=np.zeros((1, 24)))
        # Legacy per-unit investment group (written by name)
        inv = y.create_group("gen_investment_power")
        inv.create_dataset("z_coal", data=np.array([3.0]))
    with h5py.File(path, "a") as f:
        f["system_configuration/generators/generator_0"].attrs["invest_cost"] = 1000.0
    return path


def test_cost_fallback_charges_fixed_om_on_the_mw_in_service(tmp_path):
    path = _file(tmp_path / "res.h5")
    info = fa._load_system_from_h5(path)
    year = fa._load_year_data(path, "year_2025_threshold_0")
    costs = fa._recalculate_costs(
        year, info["gen_configs"], info["bat_configs"], 1,
        info["tech_configs"], info["bat_tech_configs"])
    # 8760 $/MW·yr × 40 MW in service (not the 100 MW configured) × 24 h
    assert costs["om_cost"] == pytest.approx(40.0 * 24)
    # A file without the attr keeps the configured rating
    old = _file(tmp_path / "old.h5", in_service=False)
    costs = fa._recalculate_costs(
        fa._load_year_data(old, "year_2025_threshold_0"), info["gen_configs"],
        info["bat_configs"], 1, info["tech_configs"], info["bat_tech_configs"])
    assert costs["om_cost"] == pytest.approx(100.0 * 24)


def test_dashboard_fixed_om_on_the_mw_in_service(tmp_path):
    from esfex.visualization.panels.dashboard_loader import DashboardLoader
    path = _file(tmp_path / "res.h5")
    with h5py.File(path, "r") as f:
        comp = DashboardLoader({}, {})._per_system_cost(f, "", 2025)
    assert comp["fixed_om_cost"] == pytest.approx(40.0 * 24)


def test_system_metrics_count_the_capacity_in_service(tmp_path):
    from esfex.visualization.panels.results_charts import (
        SystemMetricsEvolutionChart, _load_bat_configs, _load_bat_tech_configs,
        _load_gen_configs, _load_tech_configs,
    )
    path = _file(tmp_path / "res.h5")
    with h5py.File(path, "r") as f:
        m = SystemMetricsEvolutionChart._extract_metrics(
            SystemMetricsEvolutionChart, f[SC], "year_2025_threshold_0", f, 1,
            _load_gen_configs(f), _load_bat_configs(f), _load_tech_configs(f),
            _load_bat_tech_configs(f))
    # 40 MW of coal in service + 10 MW built, against a 25 MW peak
    assert m["cap_total"] == pytest.approx(50.0)
    assert m["reserve_margin"] == pytest.approx((50.0 / 25.0 - 1.0) * 100.0)
    # The per-unit investment group is matched by name: 3 MW × 1000 $/MW
    assert m["inv_cost"] == pytest.approx(3000.0 / 1e6)


def test_uc_charts_find_each_units_config_by_name(tmp_path):
    from esfex.visualization.panels import results_charts as rc
    path = _file(tmp_path / "res.h5")
    stack = rc.UCDispatchStackChart._build_payload(None, path, [2025])
    # The PV dataset sorts first and took the coal config by position
    assert set(stack["gen_by_tech"]) == {"Solar", "Coal"}
    assert sum(stack["gen_by_tech"]["Solar"]) == pytest.approx(12 * 8.0)

    nl = rc.UCNetLoadDurationChart._build_payload(None, path, [2025])
    # Net load = 25 − PV: its minimum is 17 MW
    assert min(nl["net_load_sorted"]) == pytest.approx(17.0)

    ramps = rc.UCRampDistributionChart._build_payload(None, path, [2025])
    assert "Solar" in str(ramps) and "Coal" not in str(ramps)

    soc = rc.UCStorageSOCChart._build_payload(None, path, [2025])
    # No SOC: the capacity comes from each battery's own config
    assert soc["totals"]["a_bess"]["capacity_mwh"] == pytest.approx(40.0)
    assert soc["totals"]["z_bess"]["capacity_mwh"] == pytest.approx(10.0)


def test_lookup_applies_the_mw_in_service_to_an_existing_unit():
    gens = [{"name": "coal", "rated_power": 100.0}]
    by_name = fa._index_configs_by_name(gens)
    cfg = fa._lookup_config("coal", by_name, gens, in_service_mw=np.array([40.0]))
    np.testing.assert_array_equal(cfg["rated_power"], [40.0])
    assert gens[0]["rated_power"] == 100.0
    assert fa._lookup_config("coal", by_name, gens) is gens[0]


# ── technology properties on the buses of a built unit ───────────────

def test_node_values_on_buses():
    from esfex.runner import _node_values_on_buses
    assert _node_values_on_buses([10.0, 20.0], 1, [1]) == [20.0]
    assert _node_values_on_buses([10.0, 20.0, 30.0], 2, [2, 0]) == [30.0, 10.0]
    assert _node_values_on_buses([10.0, 20.0], 3, [0, 1, 5], default=-1.0) == [
        10.0, 20.0, -1.0]
    assert _node_values_on_buses([7.0], 2, [1, 0]) == [7.0, 7.0]
    assert _node_values_on_buses([1.0, 2.0], 2, None) == [1.0, 2.0]
    assert _node_values_on_buses([], 2, [0, 1], default=3.0) == [3.0, 3.0]


def test_technology_properties_follow_the_bus_nodes(tmp_path):
    """Two nodes, one bus (at node 1): the per-node lists are longer than
    the bus list, and were read by position (node 0's value)."""
    tech = _make_technology("Gas CC", "Gas", 2, tech_type="Non-renewable")
    tech = tech.model_copy(update={"fuel_cost": [10.0, 20.0],
                                   "maintenance_cost": [1.0, 2.0]})
    li = _make_battery_technology("Li", 2).model_copy(update={
        "maintenance_cost": [3.0, 4.0], "efficiency_charge": [0.8, 0.9]})
    sysc = _make_system_config(num_nodes=2, technologies={"gas": tech},
                               battery_technologies={"li": li})
    orch = Orchestrator(_make_esfex_config(sysc), output_dir=tmp_path)
    orch.primary_system = orch.primary_system.model_copy(update={
        "buses": [SimpleNamespace(parent_node=1, demand_fraction=1.0)]})
    gas_key = next(iter(orch.primary_system.technologies))
    li_key = next(iter(orch.primary_system.battery_technologies))
    units = orch._build_config_from_cumulative({}, {
        "tech": {1: np.array([0.0, 30.0])},
        "bat_tech_power": {1: np.array([0.0, 5.0])},
        "bat_tech_capacity": {1: np.array([0.0, 20.0])}})
    gas = units[f"inv_{gas_key}"]
    assert gas["rated_power"] == [30.0]
    assert gas["fuel_cost"] == [20.0]
    assert gas["maintenance_cost"] == [2.0]
    bat = units[f"inv_{li_key}"]
    assert bat["MaxDischargePower"] == [5.0]
    assert bat["maintenance_cost"] == [4.0]
    assert bat["efficiency_charge"] == [0.9]


# ── second system of a multi-system file: no second node slicing ──────

def _two_system_file(path):
    """Systems A (node 0) and B (node 1). In B: coal (fuel 50, 40 MW in
    service of 100 configured) at full output, gas (fuel 80, 30 MW) at 25 MW
    and oil (fuel 120, 10 MW) at full output: gas is the only partial unit,
    the marginal one while committed."""
    H = 24
    with h5py.File(path, "w") as f:
        f.attrs["temporal_resolution_hours"] = 1
        f.attrs["subsystem_names"] = ["A", "B"]
        f.attrs["subsystem_offsets"] = [0, 1]
        f.attrs["subsystem_node_counts"] = [1, 1]
        f.create_dataset("summary_results/year", data=[2025])
        sc = f.create_group("system_configuration")
        sc.create_dataset("nodes/name", data=np.array([b"nA", b"nB"]))
        gens = sc.create_group("generators")
        gens.attrs["num_generators"] = 3
        for i, (name, fuel, cost, rated) in enumerate((
                ("coal", "Coal", 50.0, 100.0), ("gas", "Gas", 80.0, 30.0),
                ("oil", "Oil", 120.0, 10.0))):
            gens.create_group(f"generator_{i}").attrs.update(
                name=f"B/{name}", key=f"B__{name}", fuel=fuel, fuel_cost=cost,
                maintenance_cost=0.0, rated_power=f"[0.0, {rated}]")
        bats = sc.create_group("batteries")
        bats.attrs["num_batteries"] = 1
        bats.create_group("battery_0").attrs.update(
            name="B/bess", key="B__bess", capacity="[0.0, 40.0]")
        y = f.create_group(SC)
        y.attrs["year"] = 2025

        def per_node(a, b):
            return np.array([np.full(H, a), np.full(H, b)], dtype=float)

        coal = y.create_dataset("generation/B - coal", data=per_node(0.0, 40.0))
        coal.attrs["rated_power"] = np.array([0.0, 40.0])
        y.create_dataset("generation/B - gas", data=per_node(0.0, 25.0))
        y.create_dataset("generation/B - oil", data=per_node(0.0, 10.0))
        status = np.array([np.zeros(H), np.r_[np.zeros(4), np.ones(H - 4)]])
        for name in ("B - coal", "B - gas", "B - oil"):
            y.create_dataset(f"gen_status/{name}", data=status)
        y.create_dataset("demand", data=per_node(1.0, 75.0))
        y.create_dataset("loss_load", data=per_node(0.0, 0.5))
        y.create_dataset("curtailment", data=per_node(0.0, 3.0))
        y.create_dataset("nodal_electricity_prices", data=per_node(5.0, 80.0))
        y.create_dataset("electricity_prices", data=np.full(H, 40.0))
        y.create_dataset("battery_soc/B - bess", data=per_node(0.0, 20.0))
        y.create_dataset("battery_charge/B - bess", data=per_node(0.0, 1.0))
        y.create_dataset("battery_discharge/B - bess", data=per_node(0.0, 2.0))
    return path


def test_uc_charts_of_a_second_system_read_its_nodes_once(tmp_path):
    """The subsystem view already holds B's node; the charts sliced it
    again with B's global range and read nothing."""
    from esfex.visualization.panels import results_charts as rc
    path = _two_system_file(tmp_path / "res.h5")
    kw = dict(base_prefix="systems/B")

    stack = rc.UCDispatchStackChart._build_payload(None, path, [2025], **kw)
    assert {k: sum(v) for k, v in stack["gen_by_tech"].items()} == pytest.approx(
        {"Coal": 960.0, "Gas": 600.0, "Fuel oil": 240.0})
    assert sum(stack["demand"]) == pytest.approx(75.0 * 24)
    assert sum(stack["bat_discharge"]) == pytest.approx(48.0)

    shed = rc.UCLoadShedCurtailmentChart._build_payload(None, path, [2025], **kw)
    assert shed["load_shed_total_mwh"] == pytest.approx(12.0)
    assert shed["demand_total_mwh"] == pytest.approx(75.0 * 24)

    nl = rc.UCNetLoadDurationChart._build_payload(None, path, [2025], **kw)
    assert max(nl["demand_sorted"]) == pytest.approx(75.0)

    soc = rc.UCStorageSOCChart._build_payload(None, path, [2025], **kw)
    assert soc["totals"]["B - bess"]["discharge_mwh"] == pytest.approx(48.0)

    heat = rc.UCCommitmentHeatmapChart._build_payload(None, path, [2025], **kw)
    assert heat["units"] == ["B - coal @ n1", "B - gas @ n1", "B - oil @ n1"]

    lmp = rc.UCLMPByNodeChart._build_payload(None, path, [2025], **kw)
    assert lmp["nodes"] == ["nB"]
    assert lmp["mean_by_node"] == pytest.approx([80.0])

    marg = rc.UCMarginalTechChart._build_payload(None, path, [2025], **kw)
    # Coal runs at its 40 MW in service (not the per-node mean of its
    # rating): gas, the only partial unit, is marginal while committed
    assert marg["hours_marginal"]["Gas"] == 20


def test_uc_ramps_of_a_second_system(tmp_path):
    from esfex.visualization.panels import results_charts as rc
    path = _two_system_file(tmp_path / "res.h5")
    with h5py.File(path, "a") as f:
        f[f"{SC}/generation/B - gas"][1, 12:] = 5.0
    ramps = rc.UCRampDistributionChart._build_payload(
        None, path, [2025], base_prefix="systems/B")
    assert "error" not in ramps and "Gas" in str(ramps)


def test_dashboard_uc_hourly_of_a_second_system(tmp_path):
    from esfex.visualization.panels.dashboard_loader import DashboardLoader
    from esfex.visualization.panels.results_charts import _open_scenario
    path = _two_system_file(tmp_path / "res.h5")
    with h5py.File(path, "r") as f:
        sc = _open_scenario(f, "systems/B", "year_2025_threshold_0")
        out = DashboardLoader({}, {})._read_uc_hourly(f, "systems/B", sc)
    assert sum(out["trajectory"]["demand_mw"]) == pytest.approx(75.0 * 24)
    mix = {s["label"]: sum(s["values"]) for s in out["mix"]["series_pos"]}
    assert mix["Coal"] == pytest.approx(960.0)


def test_dashboard_start_ups_of_a_second_system(tmp_path):
    """gen_status is now sliced by the view, like generation: the cost
    reader must not slice it again."""
    from esfex.visualization.panels.dashboard_loader import DashboardLoader
    path = _two_system_file(tmp_path / "res.h5")
    with h5py.File(path, "a") as f:
        gens = f["system_configuration/generators"]
        for i in range(3):
            gens[f"generator_{i}"].attrs["start_up_cost"] = "[0.0, 100.0]"
    with h5py.File(path, "r") as f:
        cost = DashboardLoader({}, {})._per_system_cost(f, "systems/B", 2025)
    # One start-up per unit at B's node (status 0 → 1 at hour 4)
    assert cost["startup_cost"] == pytest.approx(3 * 100.0)


# ── capacity payment ──────────────────────────────────────────────────

def test_capacity_payment_on_the_mw_in_service(tmp_path):
    """It took node 0 of each configured generator's rated_power (0 for a
    unit at another node), never the master's builds or retirements."""
    path = _two_system_file(tmp_path / "res.h5")
    base = fa.compute_system_financials(path, fa.FinancialAssumptions())
    paid = fa.compute_system_financials(
        path, fa.FinancialAssumptions(capacity_payment=1000.0))
    extra = (paid.cash_flows["revenue"].to_numpy()
             - base.cash_flows["revenue"].to_numpy())
    # coal 40 MW in service + gas 30 + oil 10 configured
    assert extra == pytest.approx([1000.0 * 80.0])


# ── master results indexed by bus or by node ─────────────────────────

def test_bus_level_master_with_as_many_buses_as_nodes(tmp_path):
    """Two nodes, two buses both at node 0: the master solves on the buses,
    but its arrays (as long as the node list) were read per node."""
    old = _make_generator("Old", "Non-renewable", "Coal", [100.0, 0.0],
                          life_time=[40, 40], initial_age=[0, 0])
    tech = _make_technology("Gas CC", "Gas", 2, tech_type="Non-renewable")
    sysc = _make_system_config(num_nodes=2, generators={"old": old},
                               technologies={"gas": tech})
    orch = Orchestrator(_make_esfex_config(sysc), output_dir=tmp_path)
    buses = [SimpleNamespace(parent_node=0, demand_fraction=1.0),
             SimpleNamespace(parent_node=0, demand_fraction=0.0)]
    orch.primary_system = orch.primary_system.model_copy(update={"buses": buses})
    orch.master_system = orch.primary_system          # a bus-level master
    gen_key = next(iter(orch.primary_system.generators))
    tech_key = next(iter(orch.primary_system.technologies))
    units = orch._build_config_from_cumulative(orch._gather_units(), {
        "gen": {1: np.array([60.0, 20.0])},
        "tech": {1: np.array([30.0, 10.0])}})
    # Both buses are node 0's
    assert units[gen_key]["rated_power"] == [80.0, 0.0]
    assert units[f"inv_{tech_key}"]["rated_power"] == [30.0, 10.0]
    # A node-level master keeps its per-node arrays
    orch.master_system = orch.master_system.model_copy(update={"buses": []})
    units = orch._build_config_from_cumulative(orch._gather_units(), {
        "gen": {1: np.array([60.0, 20.0])}})
    assert units[gen_key]["rated_power"] == [60.0, 20.0]
