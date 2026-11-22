"""Every stochastic multiplier the Studio offers changes the stochastic master.

The Studio's scenario form offered 32 multiplier keys, but the model knew only
10 names: pydantic dropped the rest, so only ``demand_growth`` and
``discount_rate`` reached the model. The model now has a multiplier for each
cost or penalty term of the master, the Studio offers exactly those, older
keys are translated (``gen_fuel_cost`` → ``fuel_cost``, ...) and keys for
costs the master does not have are dropped with a warning.
"""

import logging
import sys

import numpy as np
import pytest

sys.path.insert(0, "tests")

from esfex.config.schema import (  # noqa: E402
    SCENARIO_MULTIPLIER_ALIASES,
    ScenarioMultipliers,
    StochasticScenarioConfig,
)

# The keys the Studio catalog offered before the fix.
OLD_GUI_KEYS = [
    "gen_invest_cost", "gen_decommissioning_cost", "gen_fuel_cost", "gen_fixed_cost",
    "gen_maintenance_cost", "gen_start_up_cost", "bat_invest_cost_power",
    "bat_invest_cost_capacity", "bat_maintenance_cost", "bat_decommissioning_cost",
    "transmission_invest_cost", "transmission_cost_per_mw_km", "electrolyzer_invest_cost",
    "electrolyzer_fixed_cost", "electrolyzer_variable_cost", "electrolyzer_water_cost",
    "converter_invest_cost", "converter_variable_cost", "fuel_import_cost", "fuel_price",
    "fuel_transport_cost", "fuel_storage_invest_cost", "fuel_price_growth_rate",
    "penalty_loss_of_load", "penalty_co2_cost", "penalty_co2_budget_violation",
    "penalty_loss_of_reserve", "penalty_curtailment", "penalty_loss_of_fuel_supply",
    "penalty_fre_penetration", "demand_growth", "discount_rate",
]


# ---------------------------------------------------------------------------
# Schema: legacy keys, dropped keys
# ---------------------------------------------------------------------------

def test_every_old_gui_key_is_a_field_or_an_alias():
    fields = set(ScenarioMultipliers.model_fields)
    for key in OLD_GUI_KEYS:
        assert (key in fields) + (key in SCENARIO_MULTIPLIER_ALIASES) == 1, key
    for targets in SCENARIO_MULTIPLIER_ALIASES.values():
        assert set(targets) <= fields


@pytest.mark.parametrize("key, fields", [
    ("gen_fuel_cost", ["fuel_cost"]),
    ("fuel_price", ["fuel_cost"]),
    ("gen_maintenance_cost", ["maintenance_cost"]),
    ("transmission_invest_cost", ["invest_cost_transmission"]),
    ("fuel_price_growth_rate", ["fuel_price_growth"]),
    ("penalty_co2_cost", ["carbon_price"]),
    ("gen_invest_cost", ["invest_cost_renewables", "invest_cost_conventional"]),
])
def test_legacy_key_is_translated(key, fields):
    m = StochasticScenarioConfig(name="s", probability=1.0, multipliers={key: 1.7}).multipliers
    for name in ScenarioMultipliers.model_fields:
        assert getattr(m, name) == pytest.approx(1.7 if name in fields else 1.0), name


def test_keys_on_the_same_field_multiply(caplog):
    with caplog.at_level(logging.WARNING, logger="esfex.config.schema"):
        m = ScenarioMultipliers(gen_invest_cost=1.1, invest_cost_renewables=0.8)
    assert m.invest_cost_renewables == pytest.approx(0.88)
    assert m.invest_cost_conventional == pytest.approx(1.1)
    assert "invest_cost_renewables" in caplog.text


def test_unknown_keys_are_dropped_with_a_warning(caplog):
    dropped = ["not_a_key"]
    with caplog.at_level(logging.WARNING, logger="esfex.config.schema"):
        m = ScenarioMultipliers(**{k: 2.0 for k in dropped}, electrolyzer_invest_cost=2.0,
                                penalty_loss_of_load=3.0)
    # electrolyzer_invest_cost scales the master's electrolyzer builds (#89)
    assert m.model_fields_set == {"penalty_loss_of_load", "electrolyzer_invest_cost"}
    assert m.electrolyzer_invest_cost == 2.0
    for key in dropped:
        assert key in caplog.text
    assert "electrolyzer_invest_cost" not in caplog.text


@pytest.mark.parametrize("key", [
    "gen_start_up_cost", "bat_maintenance_cost", "transmission_cost_per_mw_km",
    "converter_invest_cost", "converter_variable_cost", "penalty_co2_budget_violation"])
def test_master_terms_keys_are_fields(key):
    # Dropped before the master had start-ups, a battery-only maintenance
    # multiplier, link flow costs, converters and a CO2 budget
    m = ScenarioMultipliers(**{key: 2.5})
    assert m.model_fields_set == {key} and getattr(m, key) == 2.5


def test_existing_names_still_load():
    old = dict(invest_cost_renewables=0.5, invest_cost_storage=0.6,
               invest_cost_conventional=0.7, invest_cost_transmission=0.8,
               fuel_cost=0.9, maintenance_cost=1.1, discount_rate=1.2,
               demand_growth=1.3, fuel_price_growth=1.4, carbon_price=1.5)
    m = ScenarioMultipliers(**old)
    assert {k: getattr(m, k) for k in old} == old


def test_loader_translates_legacy_keys():
    from esfex.config.loader import _convert_system
    from test_loader_extra import _minimal_system

    data = _minimal_system()
    data["stochastic_scenarios"] = [{"name": "s", "probability": 1.0, "multipliers": {
        "gen_fuel_cost": 1.2, "penalty_co2_cost": 3.0, "penalty_curtailment": 0.5}}]
    m = _convert_system(data).stochastic_scenarios[0].multipliers
    assert (m.fuel_cost, m.carbon_price, m.penalty_curtailment) == (1.2, 3.0, 0.5)


def _stochastic_config(multipliers, **master):
    from esfex.config.schema import MasterProblemConfig
    cfg = _config_with_scenarios([
        StochasticScenarioConfig(name="hi", probability=1.0, multipliers=multipliers)])
    return cfg.model_copy(update={
        "enable_primary_energy": False,
        "master_problem": MasterProblemConfig(stochastic=True, **master)})


def test_multipliers_without_a_term_in_this_configuration_are_reported():
    """Start-ups need use_uc_in_dispatch, the fuel keys primary energy and
    the MW·km key a line with a per-MW·km cost."""
    from esfex.config.schema import ineffective_scenario_multipliers

    keys = {"gen_start_up_cost": 2.0, "fuel_import_cost": 2.0,
            "electrolyzer_invest_cost": 2.0, "transmission_cost_per_mw_km": 2.0,
            "fuel_cost": 2.0}
    got = {k for _, k, _ in ineffective_scenario_multipliers(_stochastic_config(keys))}
    assert got == {"gen_start_up_cost", "fuel_import_cost",
                   "electrolyzer_invest_cost", "transmission_cost_per_mw_km"}

    from esfex.config.schema import TransmissionLineGeo
    cfg = _stochastic_config(keys, use_uc_in_dispatch=True)
    sysc = cfg.primary_system
    link = TransmissionLineGeo(from_node=0, to_node=0, cost_per_mw_km=1.0, length_km=10.0)
    cfg.systems[sysc.name] = sysc.model_copy(update={"transmission_lines_geo": [link]})
    got = {k for _, k, _ in ineffective_scenario_multipliers(cfg)}
    assert got == {"fuel_import_cost", "electrolyzer_invest_cost"}
    # Keys left at 1 and deterministic masters stay silent.
    assert ineffective_scenario_multipliers(_stochastic_config({})) == []
    off = _stochastic_config(keys).model_copy(update={
        "master_problem": _stochastic_config(keys).master_problem.model_copy(
            update={"stochastic": False})})
    assert ineffective_scenario_multipliers(off) == []


def test_loader_warns_on_multipliers_without_effect(tmp_path, caplog):
    import yaml

    from esfex.config.loader import load_config

    cfg = _stochastic_config({"gen_start_up_cost": 3.0})
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(cfg.model_dump(mode="json", by_alias=True)))
    with caplog.at_level(logging.WARNING, logger="esfex.config.loader"):
        load_config(path)
    assert "multiplier gen_start_up_cost has no effect" in caplog.text


# ---------------------------------------------------------------------------
# Bridge: every field reaches Julia by name
# ---------------------------------------------------------------------------

def test_convert_scenario_passes_every_field_by_name():
    from unittest.mock import MagicMock, patch

    from esfex.bridge.converters import convert_scenario

    esfex = MagicMock()
    with patch("esfex.bridge.julia_setup.get_esfex_module", return_value=esfex):
        convert_scenario({"name": "s", "probability": 1.0,
                          "multipliers": {"gen_fuel_cost": 2.0, "penalty_curtailment": 3.0,
                                          "gen_start_up_cost": 9.0, "not_a_key": 7.0}})
    kwargs = esfex.ScenarioMultipliers.call_args.kwargs
    assert set(kwargs) == set(ScenarioMultipliers.model_fields)
    assert kwargs["fuel_cost"] == 2.0 and kwargs["penalty_curtailment"] == 3.0
    assert kwargs["gen_start_up_cost"] == 9.0
    assert all(v == 1.0 for k, v in kwargs.items()
               if k not in ("fuel_cost", "penalty_curtailment", "gen_start_up_cost"))


# ---------------------------------------------------------------------------
# Studio: catalog and serializer round trip
# ---------------------------------------------------------------------------

def test_studio_catalog_is_the_model():
    pytest.importorskip("PySide6")
    from esfex.visualization.panels.stochastic_form import _COST_MULTIPLIER_CATALOG

    keys = [k for k, _, _ in _COST_MULTIPLIER_CATALOG]
    assert len(keys) == len(set(keys))
    assert set(keys) == set(ScenarioMultipliers.model_fields)


def test_studio_labels_exist_in_every_language():
    pytest.importorskip("PySide6")
    import json
    from pathlib import Path

    from esfex.visualization.panels.stochastic_form import _COST_MULTIPLIER_CATALOG

    tdir = Path(__file__).parents[1] / "src/esfex/visualization/translations"
    for path in tdir.glob("*.json"):
        section = json.loads(path.read_text(encoding="utf-8"))["stochastic_form"]
        for _, label, group in _COST_MULTIPLIER_CATALOG:
            for tr_key in (label, group):
                assert tr_key.split(".", 1)[1] in section, (path.name, tr_key)


def _config_with_scenarios(scenarios):
    from test_runner import _make_esfex_config
    from test_zones import _make_system_config

    sysc = _make_system_config(num_nodes=1, coords=[(21.0, -80.0)])
    sysc = sysc.model_copy(update={"stochastic_scenarios": scenarios})
    return _make_esfex_config(sysc)


def test_gui_round_trip_translates_and_keeps_only_set_keys():
    from esfex.visualization.data.serializer import (
        config_to_stochastic_scenarios,
        stochastic_scenarios_to_config_dict,
    )

    legacy = {"gen_fuel_cost": 1.5, "penalty_loss_of_load": 2.0, "demand_growth": 1.03,
              "electrolyzer_invest_cost": 0.5}
    cfg = _config_with_scenarios([
        StochasticScenarioConfig(name="high", probability=1.0, multipliers=legacy)])

    gui = config_to_stochastic_scenarios(cfg)
    assert gui[0].multipliers == {"fuel_cost": 1.5, "demand_growth": 1.03,
                                  "penalty_loss_of_load": 2.0,
                                  "electrolyzer_invest_cost": 0.5}

    out = {}
    stochastic_scenarios_to_config_dict(gui, out)
    again = StochasticScenarioConfig(**out["stochastic_scenarios"][0])
    assert again.multipliers == cfg.primary_system.stochastic_scenarios[0].multipliers


def test_gui_save_translates_legacy_keys_and_warns_on_dropped(caplog):
    from esfex.visualization.data.gui_model import GuiStochasticScenario
    from esfex.visualization.data.serializer import stochastic_scenarios_to_config_dict

    sc = GuiStochasticScenario(name="s", probability=1.0, multipliers={
        "fuel_price": 1.2, "gen_start_up_cost": 2.0, "bat_invest_cost_power": 0.7,
        "not_a_key": 3.0})
    out = {}
    with caplog.at_level(logging.WARNING, logger="esfex.config.schema"):
        stochastic_scenarios_to_config_dict([sc], out)
    assert out["stochastic_scenarios"][0]["multipliers"] == {
        "fuel_cost": 1.2, "gen_start_up_cost": 2.0, "bat_invest_cost_power": 0.7}
    assert "not_a_key" in caplog.text


# ---------------------------------------------------------------------------
# Julia: each multiplier scales its term of the stochastic master
# ---------------------------------------------------------------------------

_PROBES = """
_probe_ops(si) = Dict(
    "gen_fuel" => si.generators[2].fuel_cost[1],
    "gen_curve" => si.gen_cost_curves[2][1][2].marginal_cost,
    "tech_fuel" => si.technologies[1].fuel_cost[1],
    "gen_fixed" => si.generators[2].fixed_cost[1],
    "tech_fixed" => si.technologies[1].fixed_cost[1],
    "gen_maint" => si.generators[2].maintenance_cost[1],
    "co2" => si.co2_cost,
    "lol" => si.loss_of_load_penalty,
    "res_static" => si.loss_of_reserve_static,
    "res_dynamic" => si.loss_of_reserve_dynamic,
    "curt" => si.curtailment_cost,
    "fre" => si.fre_penetration_loss_penalty,
    "demand" => si.base_demand[10, 1],
)
"""

# multiplier -> probes it must scale (all others must stay put)
_OPERATING = {
    "fuel_cost": {"gen_fuel", "gen_curve", "tech_fuel"},
    "maintenance_cost": {"gen_maint"},
    "carbon_price": {"co2"},
    "penalty_loss_of_load": {"lol"},
    "penalty_loss_of_reserve": {"res_static", "res_dynamic"},
    "penalty_curtailment": {"curt"},
    "penalty_fre_penetration": {"fre"},
}


@pytest.fixture
def master_input(julia_setup, simple_network, simple_generators, simple_batteries,
                 simple_demand):
    """Small master input with a technology, a battery technology, a fuel-cost
    curve, priced penalties and units that retire in year 2."""
    jl, ESFEX = julia_setup
    from esfex.bridge.converters import (
        convert_battery_technology_config,
        convert_technology_config,
        py_to_julia_matrix,
    )
    from test_zones import _make_battery_technology, _make_technology

    tech = _make_technology("pv_new", "Sun", 2).model_copy(update={"fuel_cost": [4.0, 4.0]})
    jl_techs = jl.seval("TechnologyConfig[]")
    jl.seval("push!")(jl_techs, convert_technology_config(tech, np.ones((48, 2))))
    jl_btechs = jl.seval("BatteryTechnologyConfig[]")
    jl.seval("push!")(jl_btechs, convert_battery_technology_config(
        _make_battery_technology("li_new", 2)))

    # Gas (generator 2) retires in year 2; so does the battery.
    jl._mi_gens = simple_generators
    jl._mi_bats = simple_batteries
    jl.seval("""
        _mi_gens[2] = ESFEX.with_fields(_mi_gens[2]; life_time=[21.0, 21.0],
                                        decommissioning_cost=[5000.0, 5000.0])
        _mi_bats[1] = ESFEX.with_fields(_mi_bats[1]; life_time=[1.0, 1.0],
                                        decommissioning_cost=[2000.0, 2000.0])
    """)
    curves = jl.seval("""Dict{Int, Dict{Int, Vector{ESFEX.CostSegment}}}(
        2 => Dict{Int, Vector{ESFEX.CostSegment}}(
            1 => [ESFEX.CostSegment(0.5, 40.0), ESFEX.CostSegment(0.5, 60.0)]))""")
    return ESFEX.MasterProblemInput(
        years=jl.seval("Int64[2025, 2026]"), base_year=2025, system_name="test",
        network=simple_network, generators=jl._mi_gens, batteries=jl._mi_bats,
        technologies=jl_techs, battery_technologies=jl_btechs,
        base_demand=py_to_julia_matrix(simple_demand),
        discount_rate=0.05, max_annual_investment=1e8,
        target_re_penetration=0.5, initial_re_penetration=0.3,
        loss_of_load_penalty=1000.0, fre_penetration_loss_penalty=50.0,
        curtailment_cost=20.0, loss_of_reserve_static=100.0,
        loss_of_reserve_dynamic=200.0, co2_cost=30.0,
        gen_cost_curves=curves,
    )


from test_master_problem import (  # noqa: E402,F401
    julia_setup,
    simple_batteries,
    simple_demand,
    simple_generators,
    simple_network,
)


def _probe(jl, ESFEX, master_input, year_idx, **mult):
    jl.seval(_PROBES)
    si = ESFEX.scenario_master_input(master_input, ESFEX.ScenarioMultipliers(**mult), year_idx)
    return {str(k): float(v) for k, v in jl.seval("_probe_ops")(si).items()}


@pytest.mark.julia
@pytest.mark.parametrize("key", sorted(_OPERATING))
def test_operating_multiplier_scales_its_term(julia_setup, master_input, key):
    jl, ESFEX = julia_setup
    base = _probe(jl, ESFEX, master_input, 1)
    scaled = _probe(jl, ESFEX, master_input, 1, **{key: 2.0})
    for probe, value in base.items():
        assert value != 0.0, probe
        expected = 2.0 if probe in _OPERATING[key] else 1.0
        assert scaled[probe] == pytest.approx(expected * value), (key, probe)


@pytest.mark.julia
def test_growth_multipliers_compound_by_year(julia_setup, master_input):
    jl, ESFEX = julia_setup
    base = _probe(jl, ESFEX, master_input, 2)
    grown = _probe(jl, ESFEX, master_input, 2, demand_growth=1.1, fuel_price_growth=1.2)
    assert grown["demand"] == pytest.approx(1.1 * base["demand"])
    assert grown["gen_curve"] == pytest.approx(1.2 * base["gen_curve"])


_OBJECTIVE = """
function _probe_objective(input, mult)
    model, v, _ = ESFEX.create_master_problem(input; use_representative_days=false)
    obj = ESFEX.build_stochastic_objective!(model, v, input,
        [ESFEX.Scenario("s", 1.0, mult)], [Dict{Int, Vector{JuMP.AffExpr}}()])
    Dict("tech" => JuMP.coefficient(obj, v.tech_investment[1][1][1]),
         "bat_power" => JuMP.coefficient(obj, v.bat_tech_power_investment[1][1][1]),
         "bat_energy" => JuMP.coefficient(obj, v.bat_tech_capacity_investment[1][1][1]),
         "decomm" => JuMP.constant(obj))
end
"""


def _objective(jl, ESFEX, master_input, **mult):
    """Stochastic objective of one scenario (no operation): coefficients of the
    year-1 investments at bus 1 and the constant (decommissioning and fixed
    O&M) term."""
    jl.seval("using JuMP")
    jl.seval(_OBJECTIVE)
    out = jl.seval("_probe_objective")(master_input, ESFEX.ScenarioMultipliers(**mult))
    return {str(k): float(v) for k, v in out.items()}


@pytest.mark.julia
def test_investment_and_decommissioning_multipliers(julia_setup, master_input):
    jl, ESFEX = julia_setup
    base = _objective(jl, ESFEX, master_input)
    assert all(v > 0 for v in base.values()), base

    p = _objective(jl, ESFEX, master_input, invest_cost_storage=2.0, bat_invest_cost_power=1.5)
    assert p["bat_power"] == pytest.approx(3.0 * base["bat_power"])
    assert p["bat_energy"] == pytest.approx(2.0 * base["bat_energy"])
    assert p["tech"] == pytest.approx(base["tech"])

    e = _objective(jl, ESFEX, master_input, bat_invest_cost_capacity=0.5)
    assert e["bat_energy"] == pytest.approx(0.5 * base["bat_energy"])
    assert e["bat_power"] == pytest.approx(base["bat_power"])

    # Constant = generator decommissioning G + battery decommissioning B +
    # fixed O&M F of the existing units ($/MW·yr), each scaled by its own key
    g = _objective(jl, ESFEX, master_input, gen_decommissioning_cost=2.0)["decomm"]
    b = _objective(jl, ESFEX, master_input, bat_decommissioning_cost=2.0)["decomm"]
    f = _objective(jl, ESFEX, master_input, gen_fixed_cost=2.0)["decomm"]
    G, B, F = g - base["decomm"], b - base["decomm"], f - base["decomm"]
    assert G > 0 and B > 0 and F > 0
    assert G + B + F == pytest.approx(base["decomm"])

    # gen_fixed_cost also scales the fixed O&M of the technology MW built
    t = _objective(jl, ESFEX, master_input, gen_fixed_cost=2.0)["tech"]
    assert t > base["tech"]


# ---------------------------------------------------------------------------
# End to end: keys written by the Studio change the plan
# ---------------------------------------------------------------------------

def _reserve_build(tmp_path, tag, multipliers):
    """MW of gas built to cover a 30 MW static reserve (setup of
    test_master_tsam_costs), under a single stochastic scenario."""
    from esfex.config.schema import MasterProblemConfig, PenaltiesConfig, TemporalConfig
    from esfex.runner import Orchestrator
    from test_runner import _make_esfex_config, _make_generator
    from test_zones import _make_system_config, _make_technology

    demand = tmp_path / "demand.csv"
    np.savetxt(demand, np.full(8760, 50.0))
    diesel = _make_generator("Diesel", "Non-renewable", "Diesel", [60.0],
                             degradation_rate=[0.0], fuel_cost=[80.0], invest_max_power=[0.0])
    gas = _make_technology("gas_new", "Gas", 1, tech_type="Non-renewable",
                           invest_cost=200000.0, invest_max_power=500.0)
    gas = gas.model_copy(update={"fuel_cost": [150.0]})
    sysc = _make_system_config(num_nodes=1, coords=[(21.0, -80.0)],
                               generators={"diesel": diesel}, technologies={"gas_new": gas})
    nodes = sysc.nodes.model_copy(update={"reserve_static": [30.0], "reserve_dynamic": [0.0]})
    sysc = sysc.model_copy(update={
        "demand_path": str(demand), "nodes": nodes, "reserve_static_default_ratio": 0.0,
        "penalties": PenaltiesConfig(loss_of_load=10000.0, loss_of_reserve_static=1000.0),
        "stochastic_scenarios": [StochasticScenarioConfig(
            name="s", probability=1.0, multipliers=multipliers)]})
    cfg = _make_esfex_config(
        sysc, simulation_mode="development",
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720, overlap_hours=0),
        master_problem=MasterProblemConfig(stochastic=True))
    result = Orchestrator(cfg, output_dir=tmp_path / tag).run(years=1)[0]
    return result.investments.get("tech_investment_power_0_0", 0.0)


@pytest.mark.julia
def test_reserve_penalty_multiplier_changes_the_plan(tmp_path):
    # 60 MW of diesel, 50 MW of load: 20 MW more are needed for the 30 MW
    # reserve while a shortfall costs 1000 $/MW·h, none at 0.1 $/MW·h.
    assert _reserve_build(tmp_path, "base", {}) == pytest.approx(20.0, abs=0.5)
    assert _reserve_build(tmp_path, "cheap", {"penalty_loss_of_reserve": 1e-4}) == \
        pytest.approx(0.0, abs=1e-3)


@pytest.mark.julia
def test_legacy_studio_key_reaches_the_plan(tmp_path):
    from test_master_co2 import _gas_built

    scenario = [StochasticScenarioConfig(
        name="dear", probability=1.0, multipliers={"penalty_co2_cost": 4.0})]
    assert _gas_built(tmp_path, "dear", 50.0, scenario) > 1.0
