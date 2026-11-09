"""Configuration keys that ESFEX ignores are reported, not dropped silently.

The pydantic models ignore keys they do not declare; ``load_config`` logs each
such key with its full path and keeps the list in ``config.unknown_keys``, and
the Studio's validation lists them. Keys the loader translates (legacy flat
keys, aliases, case-insensitive penalties, GUI metadata) are not reported.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path

import pytest
import yaml

from esfex.config.loader import load_config
from esfex.config.unknown_keys import find_unknown_keys, find_unknown_system_keys

FIXTURE = Path(__file__).parent / "fixtures" / "studio_export_island.yaml"


def _raw() -> dict:
    return yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))


def _write(tmp_path: Path, raw: dict) -> Path:
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return p


# ── The walker ──────────────────────────────────────────────────────────


def test_studio_export_has_no_unknown_keys():
    """A file saved by the Studio (it writes upper-case penalties, alias
    settings, ``_gui_layout``/``_technologies``, ...)."""
    assert find_unknown_keys(_raw()) == []


def test_typos_are_reported_with_their_path():
    raw = _raw()
    sysd = raw["systems"]["island"]
    gen_key = next(iter(sysd["generators"]))
    bat_key = next(iter(sysd["batteries"]))
    sysd["generators"][gen_key]["dorop"] = [0.05]
    sysd["batteries"][bat_key]["capacitty"] = [1.0]
    sysd["nodes"]["node_nmes"] = ["a"]
    sysd["penalties"]["LOSS_OF_LAOD"] = 1.0
    sysd["dc_reactance"] = 0.3                     # not a legacy dc_* key
    sysd["max_curtailment_ratio"] = 0.1            # belongs in penalties
    raw["master_problem"]["mga"]["num_alternative"] = 3
    raw["risk"]["voll"]["hospital"] = 1.0
    raw["solver"]["thread"] = 2
    raw["simulaton_mode"] = "development"
    raw["meta_network"]["systems_links"] = [{"systems": ["a", "b"], "existing_capacity_MW": [0],
                                             "colour": "red"}]
    assert sorted(find_unknown_keys(raw)) == sorted([
        f"systems.island.generators.{gen_key}.dorop",
        f"systems.island.batteries.{bat_key}.capacitty",
        "systems.island.nodes.node_nmes",
        "systems.island.penalties.LOSS_OF_LAOD",
        "systems.island.dc_reactance",
        "systems.island.max_curtailment_ratio",
        "master_problem.mga.num_alternative",
        "risk.voll.hospital",
        "solver.thread",
        "simulaton_mode",
        "meta_network.systems_links[0].colour",
    ])


def test_translated_and_free_form_keys_are_not_reported():
    raw = _raw()
    sysd = raw["systems"]["island"]
    # Legacy flat DC keys (any case), legacy unit_/bat_ units, aliases.
    sysd.update({"dc_voltage_level_kv": 220.0, "DC_SLACK_BUS": 0,
                 "TARGET_RE_PENETRATION": 0.5, "EV_initial_soc": []})
    unit = copy.deepcopy(next(iter(sysd["generators"].values())))
    unit["Availability"] = "x.csv"
    sysd["unit_9"] = unit
    bat = copy.deepcopy(next(iter(sysd["batteries"].values())))
    bat["type"] = "storage"
    sysd["bat_9"] = bat
    sysd["electrolyzer"] = {}
    sysd["penalties"].update({"TransferMargin": 1.0, "CO2_COST": 5.0, "Loss_Of_Load": 1.0})
    # Multipliers warn about their own unknown / no-effect keys.
    sysd["stochastic_scenarios"] = [{"name": "a", "probability": 1.0, "multipliers": {
        "gen_invest_cost": 1.1, "gen_start_up_cost": 1.2, "made_up": 1.0}}]
    # Free-form dicts and GUI-only top-level keys.
    sysd["_gui_layout"] = {"anything": {"goes": 1}}
    sysd["_gui_styles"] = {"x": {"color": "red"}}
    raw["plugins"] = {"my_plugin": {"whatever": 1}}
    raw["solver"]["options"] = {"presolve": "on", "unknown_option": 1}
    raw["visual_scaling"] = {"marker_min_px": 4}
    raw["_geo_assets"] = {}
    assert find_unknown_keys(raw) == []


def test_unit_type_routes_legacy_units():
    """A ``bat_*`` key with type Storage is checked against BatteryConfig, so
    battery fields are not reported; a generator field on it is."""
    bat = {"type": "Storage", "capacity": [1.0], "MaxChargePower": [1.0], "dorop": 1}
    assert find_unknown_system_keys({"bat_1": bat}, "s") == ["s.bat_1.dorop"]


# ── Walker and loader share the legacy translations ─────────────────────


def test_flat_dc_keys_the_walker_accepts_are_read(tmp_path):
    """Every flat ``dc_*`` spelling the walker treats as consumed must reach
    ``dc_power_flow``; one it reports must not."""
    raw = _raw()
    sysd = raw["systems"]["island"]
    sysd.pop("dc_power_flow", None)
    sysd.update({"DC_max_angle_diff_deg": 25.0, "Dc_Reactance_Per_Km": 0.7,
                 "DC_VOLTAGE_LEVEL_KV": 132.0, "dc_slack_bus": 2, "dc_reactance": 9.9})
    assert find_unknown_keys(raw) == ["systems.island.dc_reactance"]
    dc = load_config(_write(tmp_path, raw)).systems["island"].dc_power_flow
    assert (dc.max_angle_diff_deg, dc.reactance_per_km, dc.voltage_level_kv, dc.slack_bus) == (
        25.0, 0.7, 132.0, 2)


def test_nested_dc_power_flow_wins_over_flat_keys(tmp_path):
    raw = _raw()
    sysd = raw["systems"]["island"]
    sysd["dc_power_flow"] = {"slack_bus": 1}
    sysd["DC_SLACK_BUS"] = 3
    assert load_config(_write(tmp_path, raw)).systems["island"].dc_power_flow.slack_bus == 1


def test_every_penalty_field_is_read_in_any_case(tmp_path):
    """The walker accepts every ``PenaltiesConfig`` field (any case); the
    loader must read each of them, not only a hand-kept list."""
    from esfex.config.schema import PenaltiesConfig

    raw = _raw()
    pens = {name.upper(): 0.01 * (i + 1) for i, name in enumerate(PenaltiesConfig.model_fields)}
    pens["TransferMargin"] = pens.pop("TRANSFER_MARGIN")
    raw["systems"]["island"]["penalties"] = pens
    assert find_unknown_keys(raw) == []
    loaded = load_config(_write(tmp_path, raw)).systems["island"].penalties
    for i, name in enumerate(PenaltiesConfig.model_fields):
        assert getattr(loaded, name) == pytest.approx(0.01 * (i + 1)), name


# ── load_config ─────────────────────────────────────────────────────────


def test_load_config_warns_and_exposes_unknown_keys(tmp_path, caplog):
    raw = _raw()
    gen_key = next(iter(raw["systems"]["island"]["generators"]))
    raw["systems"]["island"]["generators"][gen_key]["dorop"] = [0.05]
    raw["temporal"]["rolling_horizon"] = 24
    with caplog.at_level(logging.WARNING, logger="esfex.config.loader"):
        cfg = load_config(_write(tmp_path, raw))
    expected = [f"systems.island.generators.{gen_key}.dorop", "temporal.rolling_horizon"]
    assert sorted(cfg.unknown_keys) == sorted(expected)
    logged = [r.getMessage() for r in caplog.records if "Unknown configuration key" in r.getMessage()]
    assert len(logged) == 2
    for key in expected:
        assert sum(key in m for m in logged) == 1


def test_load_config_clean_file_has_no_unknown_keys(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="esfex.config.loader"):
        cfg = load_config(FIXTURE)
    assert cfg.unknown_keys == []
    assert not [r for r in caplog.records if "Unknown configuration key" in r.getMessage()]


def test_removed_benders_lol_penalty_cap_is_reported(tmp_path, caplog):
    """``benders_lol_penalty_cap`` was removed (Benders charges the full
    VOLL); an old file that still sets it gets the generic warning."""
    raw = _raw()
    raw["master_problem"]["benders_lol_penalty_cap"] = 1000.0
    with caplog.at_level(logging.WARNING, logger="esfex.config.loader"):
        cfg = load_config(_write(tmp_path, raw))
    assert cfg.unknown_keys == ["master_problem.benders_lol_penalty_cap"]
    assert any("master_problem.benders_lol_penalty_cap" in r.getMessage()
               for r in caplog.records)


# Keys removed because they had no effect, as an older Studio wrote them.
_REMOVED_PENALTIES = {"Curtailment": 100.0, "coupling_slack_penalty": 1.0,
                      "transport_congestion": 100.0, "storage_violation": 100.0,
                      "curtailment_excess_penalty": 500.0, "re_excess_penalty": 100.0,
                      "delay_retirement_per_mw": 50000.0}
_REMOVED_N1 = {"scopf_max_iterations": 5, "scopf_violation_tolerance": 0.01}


def _old_studio_project() -> dict:
    raw = _raw()
    raw["systems"]["island"]["penalties"].update(_REMOVED_PENALTIES)
    raw["n1_security"].update(_REMOVED_N1)
    return raw


def _removed_key_paths() -> list[str]:
    return sorted([f"systems.island.penalties.{k}" for k in _REMOVED_PENALTIES]
                  + [f"n1_security.{k}" for k in _REMOVED_N1])


def test_removed_no_effect_keys_are_reported(tmp_path, caplog):
    """Penalties and SCOPF settings that had no effect were removed; a file
    that still sets them (nested under penalties / n1_security) loads with
    the generic unknown-key warning for each."""
    with caplog.at_level(logging.WARNING, logger="esfex.config.loader"):
        cfg = load_config(_write(tmp_path, _old_studio_project()))
    assert sorted(cfg.unknown_keys) == _removed_key_paths()
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "Unknown configuration key systems.island.penalties.coupling_slack_penalty" in logged
    assert "Unknown configuration key n1_security.scopf_max_iterations" in logged
    assert "deprecated" not in logged


def test_system_files_are_checked(tmp_path):
    raw = _raw()
    sysd = raw["systems"]["island"]
    sysd["foo"] = 1
    (tmp_path / "island.yaml").write_text(yaml.safe_dump(sysd), encoding="utf-8")
    raw["systems"]["island"] = "island.yaml"
    cfg = load_config(_write(tmp_path, raw))
    assert cfg.unknown_keys == ["systems.island.foo"]


# ── The Studio ──────────────────────────────────────────────────────────


def test_studio_export_roundtrip_writes_no_unknown_keys(tmp_path):
    """Everything the current serializer writes is read back."""
    pytest.importorskip("PySide6")
    from esfex.visualization.data.gui_model import GuiStochasticScenario
    from esfex.visualization.data.serializer import (
        config_to_global_settings, config_to_gui_states,
        config_to_inter_system_links, gui_state_to_yaml,
    )

    cfg = load_config(FIXTURE)
    states = config_to_gui_states(cfg, base_dir=FIXTURE.parent)
    scenarios = [GuiStochasticScenario(name="hi", probability=0.5,
                                       multipliers={"demand_growth": 1.2}),
                 GuiStochasticScenario(name="lo", probability=0.5)]
    out = tmp_path / "saved.yaml"
    gui_state_to_yaml(states, cfg, out,
                      inter_system_links=config_to_inter_system_links(cfg),
                      global_settings=config_to_global_settings(cfg),
                      stochastic_scenarios=scenarios,
                      geo_assets={"a1": type("A", (), {"name": "a", "geojson_data": {}})()})
    raw = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert find_unknown_keys(raw) == []


def test_old_studio_project_loads_and_resaves_without_removed_keys(tmp_path):
    """A project saved before the no-effect keys were removed opens in the
    Studio (validation lists them as unknown) and is saved without them."""
    pytest.importorskip("PySide6")
    from esfex.visualization.data.serializer import (
        config_to_global_settings, config_to_gui_states, gui_state_to_yaml,
    )
    from esfex.visualization.data.validation import validate_state

    cfg = load_config(_write(tmp_path, _old_studio_project()))
    states = config_to_gui_states(cfg, base_dir=FIXTURE.parent)
    issues = validate_state(states["island"], categories={"configuration"})
    assert sorted(i.message.split()[2] for i in issues) == _removed_key_paths()

    out = tmp_path / "resaved.yaml"
    gui_state_to_yaml(states, cfg, out, global_settings=config_to_global_settings(cfg))
    raw = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert find_unknown_keys(raw) == []
    assert not set(_REMOVED_PENALTIES) & set(raw["systems"]["island"]["penalties"])
    assert not set(_REMOVED_N1) & set(raw["n1_security"])
    # The values that do have an effect survive the round trip
    again = load_config(out)
    assert again.unknown_keys == []
    assert again.systems["island"].penalties == cfg.systems["island"].penalties
    assert again.n1_security == cfg.n1_security


# Round 8: settings that loaded but had no effect, as an older project or
# Studio wrote them (system-level and global).
_R8_SYSTEM = {
    "LOSS_DEMAND_TRHESHOLD": 0.05, "flexible_demand_benefit_ratio": 0.5,
    "DC_BASE_IMPEDANCE": 100.0,
}
_R8_PATHS = sorted([
    "solver.scale_constraints",
    "meta_network.dynamic_transfer_pricing", "meta_network.inter_system_loss_segments",
    "risk.hazard_data_sources", "risk.fragility_curves", "risk.risk_criteria",
    "systems.island.dc_power_flow.base_impedance",
    "systems.island.rooftop_solar_config.cost_per_kw",
    "systems.island.rooftop_solar_config.cost_reduction_rate",
    "systems.island.rooftop_solar_config.o_and_m_cost",
    "systems.island.electric_demand.residential.price_sensitivity",
    "systems.island.non_electric_demand.diesel_ne.price_sensitivity",
    "systems.island.electrolyzers.pem.technology",
    "systems.island.electrolyzers.pem.min_power",
    "systems.island.electrolyzers.pem.initial_age",
    "systems.island.electrolyzers.pem.degradation_rate",
    "systems.island.primary_energy_sources.Diesel.storage_investment_cost",
    "systems.island.primary_energy_sources.Diesel.max_storage_investment_per_node",
    "systems.island.primary_energy_sources.Diesel.max_transport_investment_per_arc",
] + [f"systems.island.{k}" for k in _R8_SYSTEM])


def _r8_old_project() -> dict:
    raw = _raw()
    raw["solver"]["scale_constraints"] = True
    raw["meta_network"].update({"dynamic_transfer_pricing": True,
                                "inter_system_loss_segments": 2})
    raw["risk"].update({"hazard_data_sources": [], "fragility_curves": [],
                        "risk_criteria": {"eal_negligible": 1000.0}})
    sysd = raw["systems"]["island"]
    sysd.update(_R8_SYSTEM)
    sysd["dc_power_flow"]["base_impedance"] = 100.0
    sysd["rooftop_solar_config"] = {"adoption_scenario": "high", "cost_per_kw": 1200,
                                    "cost_reduction_rate": 0.08, "o_and_m_cost": 20}
    sysd["electric_demand"] = {"residential": {"criticality": "high",
                                               "price_sensitivity": 0.1}}
    sysd["non_electric_demand"] = {"diesel_ne": {"fuel": "Diesel", "unit": "t",
                                                 "demand": [5, 0], "price_sensitivity": 0.2}}
    two = lambda v: [v, v]  # noqa: E731
    sysd["electrolyzers"] = {"pem": {
        "name": "PEM", "technology": "PEM", "life_time": two(20), "initial_age": two(0),
        "degradation_rate": two(0.0), "rated_power": [5.0, 0.0], "min_power": two(0.1),
        "ramp_up": two(1.0), "ramp_down": two(1.0), "eff_at_rated": two(0.7),
        "eff_at_min": two(0.6), "fixed_cost": two(0.0), "variable_cost": two(1.0),
        "invest_cost": two(0.0), "invest_max_power": two(0.0)}}
    sysd["primary_energy_sources"] = {"Diesel": {
        "unit": "t", "max_availability": two(100.0), "import_cost": two(5.0),
        "storage_capacity": two(10.0), "initial_storage_level": two(0.5),
        "storage_investment_cost": 50.0,
        "max_storage_investment_per_node": 0.0, "max_transport_investment_per_arc": 0.0}}
    return raw


def test_round8_removed_keys_are_reported(tmp_path, caplog):
    """Settings removed in round 8 (no effect) load with the generic
    unknown-key warning, and no dedicated deprecation message."""
    with caplog.at_level(logging.WARNING, logger="esfex.config.loader"):
        cfg = load_config(_write(tmp_path, _r8_old_project()))
    assert sorted(cfg.unknown_keys) == _R8_PATHS
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "Unknown configuration key solver.scale_constraints" in logged
    assert "deprecated" not in logged.lower()
    sysc = cfg.systems["island"]
    # What does have an effect is still read
    assert sysc.rooftop_solar_config.adoption_scenario == "high"
    assert sysc.electric_demand["residential"].criticality == "high"
    assert sysc.electrolyzers["pem"].rated_power == [5.0, 0.0]
    assert sysc.primary_energy_sources["Diesel"].import_cost == [5.0, 5.0]


def test_round8_old_project_resaves_without_removed_keys(tmp_path):
    """An older Studio project with those keys opens in the Studio (validation
    lists them) and is saved without them, keeping the settings that work."""
    pytest.importorskip("PySide6")
    from esfex.visualization.data.serializer import (
        config_to_global_settings, config_to_gui_states, gui_state_to_yaml,
    )
    from esfex.visualization.data.validation import validate_state

    cfg = load_config(_write(tmp_path, _r8_old_project()))
    states = config_to_gui_states(cfg, base_dir=FIXTURE.parent)
    issues = validate_state(states["island"], categories={"configuration"})
    assert sorted(i.message.split()[2] for i in issues) == _R8_PATHS

    out = tmp_path / "resaved.yaml"
    gui_state_to_yaml(states, cfg, out, global_settings=config_to_global_settings(cfg))
    raw = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert find_unknown_keys(raw) == []
    again = load_config(out)
    assert again.unknown_keys == []
    s0, s1 = cfg.systems["island"], again.systems["island"]
    assert s1.rooftop_solar_config.adoption_scenario == "high"
    assert s1.electric_demand == s0.electric_demand
    assert s1.non_electric_demand == s0.non_electric_demand
    assert s1.electrolyzers["pem"].rated_power == s0.electrolyzers["pem"].rated_power
    assert again.risk == cfg.risk
    assert again.solver == cfg.solver
    assert again.meta_network == cfg.meta_network


def test_studio_validation_lists_unknown_keys(tmp_path):
    pytest.importorskip("PySide6")
    from esfex.visualization.data.serializer import config_to_gui_states
    from esfex.visualization.data.validation import validate_state

    raw = _raw()
    gen_key = next(iter(raw["systems"]["island"]["generators"]))
    raw["systems"]["island"]["generators"][gen_key]["dorop"] = [0.05]
    raw["temporal"]["rolling_horizon"] = 24
    cfg = load_config(_write(tmp_path, raw))
    state = config_to_gui_states(cfg)["island"]
    assert sorted(state.unknown_config_keys) == sorted(cfg.unknown_keys)
    issues = validate_state(state, categories={"configuration"})
    assert sorted(i.message.split()[2] for i in issues) == sorted(cfg.unknown_keys)
    assert all(i.severity == "warning" for i in issues)


# ── Round 9: keys only the dispatch's own (unreachable) investment read ──

_R9_PATHS = sorted([
    "systems.island.generators.solar.invest_max_power",
    "systems.island.generators.diesel.invest_max_power",
    "systems.island.batteries.bess.invest_cost_energy",
    "systems.island.batteries.bess.invest_max_capacity",
    "systems.island.primary_energy_sources.Diesel.transport_cost",
    "systems.island.primary_energy_sources.Diesel.transport_losses",
    "systems.island.non_electric_demand.diesel_ne.is_flexible",
    "systems.island.non_electric_demand.diesel_ne.flexibility_ratio",
    "systems.island.non_electric_demand.diesel_ne.criticality",
    "systems.island.non_electric_demand.diesel_ne.delay_tolerance",
])


def _r9_old_project() -> dict:
    raw = _raw()
    sysd = raw["systems"]["island"]
    for g in ("solar", "diesel"):
        sysd["generators"][g]["invest_max_power"] = [10.0, 0.0]
    sysd["batteries"]["bess"]["invest_cost_energy"] = [100.0, 0.0]
    sysd["batteries"]["bess"]["invest_max_capacity"] = [40.0, 0.0]
    two = lambda v: [v, v]  # noqa: E731
    # Route costs and losses are per route; the source fields had no effect
    sysd["primary_energy_sources"] = {"Diesel": {
        "unit": "t", "max_availability": two(100.0), "import_cost": two(5.0),
        "storage_capacity": two(10.0), "initial_storage_level": two(0.5),
        "transport_cost": 0.1, "transport_losses": 0.01}}
    # The primary-energy model never read flexibility or criticality
    sysd["non_electric_demand"] = {"diesel_ne": {
        "fuel": "Diesel", "unit": "t", "demand": [5, 0], "is_flexible": True,
        "flexibility_ratio": 0.2, "criticality": "high", "delay_tolerance": 3}}
    # A transmission link without loss_factor (it is for fuel routes)
    raw["meta_network"]["systems_links"] = [{
        "systems": ["island", "island"], "connections": [[0, 1]],
        "existing_capacity_MW": [10.0], "max_investment_MW": [0.0],
        "investment_cost_per_MW": [0.0], "distance_km": [10.0],
        "cost_per_mw_km": [0.0]}]
    return raw


def test_round9_removed_investment_keys_are_reported(tmp_path, caplog):
    """The dispatch never builds (development runs dispatch the master's
    builds), so the generator build limit and the battery energy investment
    fields only it read are gone: old files load with the generic warning."""
    with caplog.at_level(logging.WARNING, logger="esfex.config.loader"):
        cfg = load_config(_write(tmp_path, _r9_old_project()))
    assert sorted(cfg.unknown_keys) == _R9_PATHS
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "deprecated" not in logged.lower()
    bess = cfg.systems["island"].batteries["bess"]
    # The battery's power placement in the master is still read
    assert bess.invest_max_power == [0.0, 0.0]
    sysc = cfg.systems["island"]
    assert sysc.primary_energy_sources["Diesel"].import_cost == [5.0, 5.0]
    assert sysc.non_electric_demand["diesel_ne"].demand == [5, 0]
    assert cfg.meta_network.systems_links[0].loss_factor == []
    assert not hasattr(cfg.systems["island"].generators["solar"], "invest_max_power")


def test_round9_old_project_resaves_without_removed_keys(tmp_path):
    pytest.importorskip("PySide6")
    from esfex.visualization.data.serializer import (
        config_to_global_settings, config_to_gui_states, gui_state_to_yaml,
    )

    cfg = load_config(_write(tmp_path, _r9_old_project()))
    states = config_to_gui_states(cfg, base_dir=FIXTURE.parent)
    out = tmp_path / "resaved.yaml"
    gui_state_to_yaml(states, cfg, out, global_settings=config_to_global_settings(cfg))
    raw = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert find_unknown_keys(raw) == []


# ── Round 11: the dispatch's forced replacement, which nothing drove ──

# As an older Studio wrote them: the alias and the field name, both.
_R11_SYSTEM = {
    "MAX_NPV_PENALTY_PER_MW": 1e6, "MAX_DECOMMISSION_COST_PER_MW": 5e5,
    "FORCE_REPLACEMENT": -5e5,
    "max_npv_penalty_per_mw": 1e6, "max_decommission_cost_per_mw": 5e5,
    "force_replacement": -5e5,
}
_R11_PATHS = sorted(f"systems.island.{k}" for k in _R11_SYSTEM)


def _r11_old_project() -> dict:
    raw = _raw()
    raw["systems"]["island"].update(_R11_SYSTEM)
    return raw


def test_round11_removed_replacement_keys_are_reported(tmp_path, caplog):
    """The NPV-penalty and decommissioning caps and the replacement threshold
    only fed the dispatch's forced-replacement term, which no constraint
    used (always 0): they are gone, and old files get the generic warning."""
    with caplog.at_level(logging.WARNING, logger="esfex.config.loader"):
        cfg = load_config(_write(tmp_path, _r11_old_project()))
    assert sorted(cfg.unknown_keys) == _R11_PATHS
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "Unknown configuration key systems.island.MAX_NPV_PENALTY_PER_MW" in logged
    assert "deprecated" not in logged.lower()
    sysc = cfg.systems["island"]
    assert not hasattr(sysc, "force_replacement")
    # The master's NPV helpers still read these
    assert sysc.base_lcoe == 93.0 and sysc.npv_annual_return_rate == 0.15


def test_round11_old_project_resaves_without_removed_keys(tmp_path):
    pytest.importorskip("PySide6")
    from esfex.visualization.data.serializer import (
        config_to_global_settings, config_to_gui_states, gui_state_to_yaml,
    )

    cfg = load_config(_write(tmp_path, _r11_old_project()))
    states = config_to_gui_states(cfg, base_dir=FIXTURE.parent)
    out = tmp_path / "resaved.yaml"
    gui_state_to_yaml(states, cfg, out, global_settings=config_to_global_settings(cfg))
    raw = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert find_unknown_keys(raw) == []
    assert not set(_R11_SYSTEM) & set(raw["systems"]["island"])
    assert not hasattr(states["island"].settings, "force_replacement")
