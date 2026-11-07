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
    """A file saved by the Studio (it writes ``Curtailment``, upper-case
    penalties, alias settings, ``_gui_layout``/``_technologies``, ...)."""
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
    sysd.update({"dc_base_impedance": 100.0, "DC_SLACK_BUS": 0,
                 "TARGET_RE_PENETRATION": 0.5, "EV_initial_soc": []})
    unit = copy.deepcopy(next(iter(sysd["generators"].values())))
    unit["Availability"] = "x.csv"
    sysd["unit_9"] = unit
    bat = copy.deepcopy(next(iter(sysd["batteries"].values())))
    bat["type"] = "storage"
    sysd["bat_9"] = bat
    sysd["electrolyzer"] = {}
    sysd["penalties"].update({"TransferMargin": 1.0, "CO2_COST": 5.0,
                              "coupling_slack_penalty": 1.0, "Curtailment": 1.0})
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
    sysd.update({"DC_base_impedance": 55.0, "Dc_Reactance_Per_Km": 0.7,
                 "DC_VOLTAGE_LEVEL_KV": 132.0, "dc_slack_bus": 2, "dc_reactance": 9.9})
    assert find_unknown_keys(raw) == ["systems.island.dc_reactance"]
    dc = load_config(_write(tmp_path, raw)).systems["island"].dc_power_flow
    assert (dc.base_impedance, dc.reactance_per_km, dc.voltage_level_kv, dc.slack_bus) == (
        55.0, 0.7, 132.0, 2)


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
