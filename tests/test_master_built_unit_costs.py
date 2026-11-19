"""Costs of the units the master built ("Investment <technology>"), which
have results datasets but no config of their own, in the financial
analysis, the results charts and the runner's HDF5 export (bug sweep S11)."""

import json
from pathlib import Path

import h5py
import numpy as np
import pytest

from esfex.models import financial_analysis as fa


def _file(path, *, two_systems=False):
    """One existing coal unit (listed first) and a Gas CC unit the master
    built: the Gas CC dataset sorts before ``coal`` and has no config."""
    with h5py.File(path, "w") as f:
        f.attrs["temporal_resolution_hours"] = 1
        if two_systems:
            f.attrs["subsystem_names"] = ["A", "B"]
            f.attrs["subsystem_offsets"] = [0, 1]
            f.attrs["subsystem_node_counts"] = [1, 1]
        f.create_dataset("summary_results/year", data=[2025])
        sc = f.create_group("system_configuration")
        gens = sc.create_group("generators")
        gens.attrs["num_generators"] = 2
        gens.create_group("generator_0").attrs.update(
            name="coal", fuel="Coal", fuel_cost=50.0, fixed_cost=0.0,
            maintenance_cost=0.0, rated_power=100.0)
        techs = sc.create_group("technologies")
        techs.attrs["num_technologies"] = 1
        techs.create_group("technology_0").attrs.update(
            name="Gas CC", fuel="Gas", type="Non-renewable", invest_cost="[0.0]",
            fuel_cost="[3.0]", fixed_cost="[876.0]", maintenance_cost="[0.5]",
            start_up_cost="[10.0]")
        bts = sc.create_group("battery_technologies")
        bts.attrs["num_battery_technologies"] = 1
        bts.create_group("battery_technology_0").attrs.update(
            name="Li", invest_cost_power="[0.0]", invest_cost_energy="[0.0]",
            maintenance_cost="[2.0]", fixed_cost="[4380.0]")
        y = f.create_group("detailed_results/year_2025_threshold_0")
        y.attrs["year"] = 2025
        y.create_dataset("generation/coal", data=np.full((1, 24), 1.0))
        ds = y.create_dataset("generation/Investment Gas CC", data=np.full((1, 24), 4.0))
        ds.attrs["rated_power"] = np.array([10.0])
        ds = y.create_dataset("battery_discharge/Investment Li", data=np.full((1, 24), 1.0))
        ds.attrs["rated_power"] = np.array([5.0])
        y.create_dataset("battery_charge/Investment Li", data=np.full((1, 24), 0.5))
    return path


# ── shared lookup ─────────────────────────────────────────────────────

def test_lookup_resolves_master_built_unit_to_its_technology():
    gens = [{"name": "coal", "fuel_cost": 50.0}]
    techs = [{"name": "Cuba/Gas CC", "fuel_cost": "[3.0]"}]
    by_name = fa._index_configs_by_name(gens)
    tech_by_name = fa._index_configs_by_name(techs)
    cfg = fa._lookup_config("Investment Cuba - Gas CC (1)", by_name, gens, 0,
                            tech_by_name=tech_by_name, in_service_mw=np.array([7.0]))
    assert cfg["fuel_cost"] == "[3.0]"
    np.testing.assert_array_equal(cfg["rated_power"], [7.0])
    # The technology's own config is not modified
    assert "rated_power" not in techs[0]
    # A named file never pairs an unmatched dataset with another unit by
    # position (it got the coal unit's costs)
    assert fa._lookup_config("Investment Wind", by_name, gens, 0,
                             tech_by_name=tech_by_name) == {}


# ── financial analysis ────────────────────────────────────────────────

def test_recalculated_costs_include_master_built_units(tmp_path):
    path = _file(tmp_path / "res.h5")
    info = fa._load_system_from_h5(path)
    year = fa._load_year_data(path, "year_2025_threshold_0")
    costs = fa._recalculate_costs(
        year, info["gen_configs"], info["bat_configs"], 1,
        info["tech_configs"], info["bat_tech_configs"])
    # coal 24 MWh × 50 + Gas CC 96 MWh × 3 (the Gas CC dataset sorts first
    # and took the coal unit's config by position)
    assert costs["fuel_cost"] == pytest.approx(24 * 50 + 96 * 3.0)
    # Gas CC: 96 MWh × 0.5 + 876 $/MW·yr × 10 MW × 24/8760
    assert costs["om_cost"] == pytest.approx(96 * 0.5 + 876 * 10 * 24 / 8760)
    # Li: 36 MWh of throughput × 2 + 4380 × 5 MW × 24/8760
    assert costs["battery_cost"] == pytest.approx(36 * 2.0 + 4380 * 5 * 24 / 8760)


def test_technology_financials_of_master_built_units(tmp_path):
    path = _file(tmp_path / "res.h5")
    tf = fa.compute_technology_financials(path)
    gas = tf["Investment Gas CC"]
    assert gas.fuel_cost_total == pytest.approx(96 * 3.0)
    assert gas.om_cost_total == pytest.approx(96 * 0.5 + 876 * 10 * 24 / 8760)
    assert tf["coal"].fuel_cost_total == pytest.approx(24 * 50.0)
    li = tf["Investment Li"]
    assert li.om_cost_total == pytest.approx(36 * 2.0 + 4380 * 5 * 24 / 8760)


# ── results charts ────────────────────────────────────────────────────

def test_chart_resolver_and_second_system_configs(tmp_path):
    from esfex.visualization.panels.results_charts import (
        _gen_config_resolver, _get_gen_types, _load_gen_configs,
    )
    path = tmp_path / "res.h5"
    with h5py.File(path, "w") as f:
        f.attrs["subsystem_names"] = ["A", "B"]
        f.attrs["subsystem_offsets"] = [0, 1]
        f.attrs["subsystem_node_counts"] = [1, 1]
        g = f.create_group("system_configuration/generators")
        g.attrs["num_generators"] = 2
        g.create_group("generator_0").attrs.update(name="A/coal", type="Non-renewable")
        g.create_group("generator_1").attrs.update(name="B/gas", type="Non-renewable",
                                                   fuel_cost=2.0)
        t = f.create_group("system_configuration/technologies")
        t.attrs["num_technologies"] = 1
        t.create_group("technology_0").attrs.update(name="B/Gas CC", fuel_cost="[3.0]")
    with h5py.File(path, "r") as f:
        # The second system's configs came back empty
        assert [c["name"] for c in _load_gen_configs(f, "systems/B")] == ["B/gas"]
        assert _get_gen_types(f, "systems/B") == {"B/gas": "Non-renewable"}
        resolve = _gen_config_resolver(f, "systems/B")
        assert resolve("B - gas")["fuel_cost"] == 2.0
        assert resolve("Investment B - Gas CC")["fuel_cost"] == "[3.0]"


# ── runner export ─────────────────────────────────────────────────────

def test_export_records_technology_costs_and_built_power(tmp_path):
    from esfex.config.schema import BatteryTechnologyConfig, TechnologyConfig
    from esfex.runner import YearResults
    from tests.test_runner import _create_orchestrator

    orch = _create_orchestrator(tmp_path=tmp_path)
    tech = TechnologyConfig(
        name="Gas CC", type="Non-renewable", fuel="Gas", invest_cost=[1.0],
        invest_max_power=[100.0], eff_at_rated=[0.5], degradation_rate=[0.0],
        lifetime=30, fuel_cost=[3.0], fixed_cost=[876.0], maintenance_cost=[0.5],
        start_up_cost=[10.0])
    bat_tech = BatteryTechnologyConfig(
        name="Li", invest_cost_power=[1.0], invest_cost_energy=[1.0],
        invest_max_power=[10.0], invest_max_capacity=[40.0],
        efficiency_charge=[0.9], efficiency_discharge=[0.9],
        degradation_rate=[0.0], lifetime=15, maintenance_cost=[2.0],
        fixed_cost=[4380.0])
    orch.primary_system = orch.primary_system.model_copy(update={
        "technologies": {"gas_cc": tech}, "battery_technologies": {"li": bat_tech}})
    orch._num_nodes = 1
    n_gen = len(orch.primary_system.generators)
    n_bat = len(orch.primary_system.batteries)
    orch._rebuild_unit_names({
        "inv_gas_cc": {"name": "Investment Gas CC", "fuel": "Gas",
                       "rated_power": [10.0], "_is_per_bus": True},
        "inv_li": {"_type": "battery", "name": "Investment Li", "capacity": [20.0],
                   "MaxChargePower": [5.0], "MaxDischargePower": [5.0],
                   "_is_per_bus": True},
    })
    path = tmp_path / "out.h5"
    orch._bus_to_node = None
    orch._initialize_hdf5(path, num_nodes=1, num_years=1, start_year=2025, end_year=2026)
    H = 4
    orch._append_year_to_hdf5(path, YearResults(
        year=2025, objective=0.0, solve_time=0.0, feasible=True,
        gen_output=np.ones((n_gen + 1, 1, H)),
        bat_charge=np.ones((n_bat + 1, 1, H)), bat_discharge=np.ones((n_bat + 1, 1, H)),
        bat_soc=np.ones((n_bat + 1, 1, H))))
    with h5py.File(path, "r") as f:
        t = f["system_configuration/technologies/technology_0"].attrs
        assert (t["fuel_cost"], t["fixed_cost"], t["maintenance_cost"],
                t["start_up_cost"]) == ("[3.0]", "[876.0]", "[0.5]", "[10.0]")
        bt = f["system_configuration/battery_technologies/battery_technology_0"].attrs
        assert (bt["maintenance_cost"], bt["fixed_cost"]) == ("[2.0]", "[4380.0]")
        sc = f["detailed_results/year_2025_threshold_0"]
        np.testing.assert_array_equal(
            sc["generation/Investment Gas CC"].attrs["rated_power"], [10.0])
        np.testing.assert_array_equal(
            sc["battery_discharge/Investment Li"].attrs["rated_power"], [5.0])
        # The configured units record theirs too (bug sweep S12, see
        # test_in_service_capacity.py); only discharge datasets carry it
        for name in sc["generation"]:
            assert "rated_power" in sc["generation"][name].attrs
        assert "rated_power" not in sc["battery_charge/Investment Li"].attrs


def test_built_power_summed_from_buses_to_nodes(tmp_path):
    from types import SimpleNamespace

    from tests.test_runner import _create_orchestrator
    orch = _create_orchestrator(tmp_path=tmp_path)
    orch._num_nodes = 2
    buses = [SimpleNamespace(parent_node=p) for p in (0, 0, 1)]
    orch.primary_system = SimpleNamespace(buses=buses)
    assert orch._unit_power_by_node({"_is_per_bus": True}, [1.0, 2.0, 4.0]) == [3.0, 4.0]
    # Per-node arrays are kept
    assert orch._unit_power_by_node({}, [1.0, 2.0]) == [1.0, 2.0]


# ── fuel price growth and build records (round 44, M44) ───────────────

def _two_year_file(path, *, growth=True, builds=True):
    """2025 and 2035, no cost breakdown. A Gas CC unit and a Li battery the
    master built in 2025 (10 MW; 5 MW / 20 MWh) keep the same MW in service
    in 2035, where the master built 2 MW and 0.5 MW / 2 MWh more to replace
    what retired or degraded. Gas grows 3 %/yr from 2025; coal is flat."""
    with h5py.File(path, "w") as f:
        f.attrs["temporal_resolution_hours"] = 1
        f.create_dataset("summary_results/year", data=[2025, 2035])
        sc = f.create_group("system_configuration")
        gens = sc.create_group("generators")
        gens.attrs["num_generators"] = 1
        gens.create_group("generator_0").attrs.update(
            name="coal", fuel="Coal", fuel_cost=50.0, fixed_cost=0.0,
            maintenance_cost=0.0, rated_power=100.0)
        techs = sc.create_group("technologies")
        techs.attrs["num_technologies"] = 1
        techs.create_group("technology_0").attrs.update(
            name="Gas CC", fuel="Gas", type="Non-renewable", invest_cost="[100.0]",
            fuel_cost="[3.0]", fixed_cost="[0.0]", maintenance_cost="[0.0]",
            start_up_cost="[0.0]")
        bts = sc.create_group("battery_technologies")
        bts.attrs["num_battery_technologies"] = 1
        bts.create_group("battery_technology_0").attrs.update(
            name="Li", invest_cost_power="[20.0]", invest_cost_energy="[5.0]",
            maintenance_cost="[0.0]", fixed_cost="[0.0]")
        if growth:
            fg = sc.create_group("fuel_price_growth")
            fg.attrs.update(base_year=2025, Gas=0.03, Coal=0.0)
        for year, (tech, bat_p, bat_e) in ((2025, (10.0, 5.0, 20.0)),
                                           (2035, (2.0, 0.5, 2.0))):
            y = f.create_group(f"detailed_results/year_{year}_threshold_0")
            y.attrs["year"] = year
            y.create_dataset("generation/coal", data=np.full((1, 24), 1.0))
            ds = y.create_dataset("generation/Investment Gas CC",
                                  data=np.full((1, 24), 4.0))
            ds.attrs["rated_power"] = np.array([10.0])
            ds = y.create_dataset("battery_discharge/Investment Li",
                                  data=np.full((1, 24), 1.0))
            ds.attrs["rated_power"] = np.array([5.0])
            y.create_dataset("battery_charge/Investment Li", data=np.full((1, 24), 0.5))
            y.create_dataset("battery_soc/Investment Li", data=np.full((1, 24), 20.0))
            if builds:
                y.attrs["investment_tech_investment_power_0_0"] = tech
                y.attrs["investment_bat_tech_investment_power_0_0"] = bat_p
                y.attrs["investment_bat_tech_investment_capacity_0_0"] = bat_e
    return path


def test_fuel_valued_at_the_year_price_of_its_growing_fuel(tmp_path, caplog):
    """The financial analysis valued fuel at the flat fuel_cost: a fuel of
    the fuels block that grows 3 %/yr cost in 2035 what it cost in 2025.
    It grows as the dispatch charges it, (1 + rate)^(year - base year)."""
    path = _two_year_file(tmp_path / "res.h5")
    tf = fa.compute_technology_financials(path)
    gas_2035 = 96 * 3.0 * 1.03 ** 10
    np.testing.assert_allclose(tf["Investment Gas CC"].annual_generation, [96.0, 96.0])
    assert tf["Investment Gas CC"].fuel_cost_total == pytest.approx(96 * 3.0 + gas_2035)
    assert tf["coal"].fuel_cost_total == pytest.approx(2 * 24 * 50.0)
    sf = fa.compute_system_financials(path)
    np.testing.assert_allclose(sf.cash_flows["fuel_cost"],
                               [24 * 50.0 + 96 * 3.0, 24 * 50.0 + gas_2035])
    # A file written before the growth was recorded: flat, with a note
    old = _two_year_file(tmp_path / "old.h5", growth=False)
    with caplog.at_level("INFO", logger=fa.__name__):
        tf = fa.compute_technology_financials(old)
    assert tf["Investment Gas CC"].fuel_cost_total == pytest.approx(2 * 96 * 3.0)
    assert "does not record the fuel price growth" in caplog.text


def test_capex_of_builds_from_the_build_records(tmp_path):
    """Capex was booked on the growth of the built units' MW in service: a
    build that only replaced retired or degraded capacity (the 2035 builds,
    same MW in service) cost nothing. It is booked on what the master
    built each year now."""
    path = _two_year_file(tmp_path / "res.h5")
    sf = fa.compute_system_financials(path)
    gas = [10.0 * 100.0, 2.0 * 100.0]
    li = [5.0 * 20.0 + 20.0 * 5.0, 0.5 * 20.0 + 2.0 * 5.0]
    np.testing.assert_allclose(sf.cash_flows["capex"], np.add(gas, li))
    assert sf.investment_capex == pytest.approx(sum(gas) + sum(li))
    tf = fa.compute_technology_financials(path)
    assert tf["Investment Gas CC"].capex_total == pytest.approx(sum(gas))
    assert tf["Investment Li"].capex_total == pytest.approx(sum(li))
    # Files without build records: the growth of the MW in service
    old = fa.compute_system_financials(_two_year_file(tmp_path / "old.h5", builds=False))
    np.testing.assert_allclose(old.cash_flows["capex"], [gas[0] + li[0], 0.0])


def test_export_records_fuel_price_growth(tmp_path):
    from esfex.config.schema import FuelConfig
    from tests.test_runner import _create_orchestrator

    orch = _create_orchestrator(tmp_path=tmp_path)
    orch.primary_system = orch.primary_system.model_copy(update={"fuels": {
        "Gas": FuelConfig(name="Gas", emission_factor=0.2, price_base=10.0,
                          price_growth_rate=0.03)}})
    orch._num_nodes = 1
    orch._rebuild_unit_names({})
    orch._bus_to_node = None
    path = tmp_path / "out.h5"
    orch._initialize_hdf5(path, num_nodes=1, num_years=1, start_year=2025, end_year=2026)
    info = fa._load_system_from_h5(path)
    assert info["fuel_price_base_year"] == 2025
    assert info["fuel_price_growth"] == {"Gas": pytest.approx(0.03)}
    assert fa._fuel_price_factor(info, "Gas", 2035) == pytest.approx(1.03 ** 10)
    assert fa._fuel_price_factor(info, "Coal", 2035) == 1.0


# ── electrolyzer water cost label ─────────────────────────────────────

@pytest.mark.parametrize("lang", ["en", "es", "fr", "pt", "ja"])
def test_electrolyzer_water_cost_label_is_per_kg_h2(lang):
    """The schema, the model and the docs charge water_cost per kg of
    hydrogen; the form said $/m³."""
    import esfex.visualization as vis
    d = json.loads((Path(vis.__file__).parent / "translations" / f"{lang}.json")
                   .read_text(encoding="utf-8"))
    label = d["electrolyzer_form"]["water_cost"]
    assert "$/kg H₂" in label and "m³" not in label
