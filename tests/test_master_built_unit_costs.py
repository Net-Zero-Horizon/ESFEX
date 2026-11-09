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
                            tech_by_name=tech_by_name, built_mw=np.array([7.0]))
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
        # The configured units keep their config's capacity
        for name in sc["generation"]:
            if name != "Investment Gas CC":
                assert "rated_power" not in sc["generation"][name].attrs
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
