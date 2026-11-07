"""Studio save round-trips: deletions persist, Risk Workbench scenarios are
kept, risk defaults match the schema, Grid Builder demand step early returns.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path

import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from esfex.config.loader import load_config  # noqa: E402
from esfex.config.schema import RiskConfig  # noqa: E402
from esfex.visualization.data import serializer as S  # noqa: E402
from esfex.visualization.data.gui_model import GuiGlobalSettings  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "studio_export_island.yaml"


@pytest.fixture(autouse=True)
def _no_user_prefs(tmp_path, monkeypatch):
    # GuiGlobalSettings() reads preferences: keep the real file out of it.
    from esfex.visualization import preferences as prefs

    monkeypatch.setattr(prefs, "_PREFS_DIR", tmp_path / "prefs")
    monkeypatch.setattr(prefs, "_PREFS_FILE", tmp_path / "prefs" / "preferences.json")


def _two_systems(tmp_path) -> Path:
    d = yaml.safe_load(FIXTURE.read_text())
    s2 = copy.deepcopy(d["systems"]["island"])
    s2["name"] = "island2"
    d["systems"]["island2"] = s2
    d["meta_network"]["systems"] = ["island", "island2"]
    d["meta_network"]["systems_links"] = [{
        "systems": ["island", "island2"], "connections": [[0, 1]],
        "existing_capacity_MW": [50.0], "max_investment_MW": [0.0],
        "investment_cost_per_MW": [0.0], "loss_factor": [0.01],
        "distance_km": [30.0], "cost_per_mw_km": [0.0],
        "reactance_pu": [0.05], "resistance_pu": [0.004]}]
    d["systems"]["island"]["stochastic_scenarios"] = [
        {"name": "hi", "probability": 0.5, "multipliers": {"fuel_price": 1.3}},
        {"name": "lo", "probability": 0.5, "multipliers": {"fuel_price": 0.8}}]
    p = tmp_path / "multi.yaml"
    p.write_text(yaml.safe_dump(d, sort_keys=False))
    return p


def _load(path):
    cfg = load_config(path)
    raw = yaml.safe_load(Path(path).read_text())
    return (cfg, S.config_to_gui_states(cfg, base_dir=Path(path).parent),
            S.config_to_global_settings(cfg, raw_dict=raw))


def test_deleting_all_links_and_scenarios_persists(tmp_path):
    cfg, states, gs = _load(_two_systems(tmp_path))
    assert S.config_to_inter_system_links(cfg)
    assert S.config_to_stochastic_scenarios(cfg)
    out = tmp_path / "out.yaml"
    S.gui_state_to_yaml(states, cfg, out, inter_system_links=[],
                        global_settings=gs, stochastic_scenarios=[])
    again = load_config(out)
    assert again.meta_network.systems_links == []
    assert S.config_to_stochastic_scenarios(again) == []
    # Not passing them (None) keeps the loaded ones.
    out2 = tmp_path / "out2.yaml"
    S.gui_state_to_yaml(states, cfg, out2, global_settings=gs)
    kept = load_config(out2)
    assert len(kept.meta_network.systems_links) == 1
    assert len(S.config_to_stochastic_scenarios(kept)) == 2


def test_risk_workbench_scenarios_round_trip(tmp_path):
    cfg, states, gs = _load(FIXTURE)
    gs.risk_climate_scenarios = [{
        "name": "SSP2-4.5", "probability": 0.6, "ssp_pathway": "SSP2-4.5",
        "gcm_model": "", "availability_suffix": "_ssp245", "demand_scale": {},
        "temperature_delta": {2030: 0.6, 2050: 1.2},
        "ghi_delta_fraction": {2050: -0.01},
        "wind_speed_delta_fraction": {2050: -0.03},
        "delta_source": "ipcc-ar6-global",      # workbench-only key: dropped
    }, {"name": "SSP5-8.5", "probability": 0.4, "ssp_pathway": "SSP5-8.5"}]
    gs.risk_hazard_scenarios = [{
        "name": "quake_n0", "probability": 0.01, "hazard_type": "earthquake",
        "affected_nodes": [0], "damage_fraction": {"diesel": 0.4},
        "recovery_hours": 720, "intensity_measure": 0.3, "description": "M7",
    }]
    out = tmp_path / "risk.yaml"
    S.gui_state_to_yaml(states, cfg, out, global_settings=gs)
    again = load_config(out)
    assert again.unknown_keys == []
    assert [c.name for c in again.risk.climate_scenarios] == ["SSP2-4.5", "SSP5-8.5"]
    assert again.risk.climate_scenarios[0].temperature_delta == {2030: 0.6, 2050: 1.2}
    hz = again.risk.hazard_scenarios[0]
    assert (hz.name, hz.damage_fraction, hz.recovery_hours) == (
        "quake_n0", {"diesel": 0.4}, 720)
    gs2 = S.config_to_global_settings(again)
    assert gs2.risk_hazard_scenarios[0]["name"] == "quake_n0"
    assert gs2.risk_climate_scenarios[0]["availability_suffix"] == "_ssp245"


def test_invalid_workbench_scenario_is_skipped_not_fatal(tmp_path):
    cfg, states, gs = _load(FIXTURE)
    gs.risk_hazard_scenarios = [{"name": "bad", "probability": 7.0}]
    out = tmp_path / "risk.yaml"
    S.gui_state_to_yaml(states, cfg, out, global_settings=gs)
    assert load_config(out).risk.hazard_scenarios == []


def test_climate_probabilities_rescaled_so_file_loads(tmp_path):
    # One scenario left after deleting the other: 0.6 alone would make the
    # saved file fail validation (probabilities must sum to 1).
    cfg, states, gs = _load(FIXTURE)
    gs.risk_climate_scenarios = [{"name": "SSP2-4.5", "probability": 0.6}]
    out = tmp_path / "risk.yaml"
    S.gui_state_to_yaml(states, cfg, out, global_settings=gs)
    again = load_config(out)
    assert again.risk.climate_scenarios[0].probability == pytest.approx(1.0)


def test_risk_defaults_match_schema():
    g, r = GuiGlobalSettings(), RiskConfig()
    assert g.risk_base_temperature == r.demand_base_temperature
    assert g.risk_heating_coefficient == r.demand_heating_coefficient
    assert g.risk_cooling_coefficient == r.demand_cooling_coefficient


def test_demand_step_early_return_does_not_shadow_tr():
    """The transformer loop variable ``tr`` shadowed the translation function,
    so the early-return messages raised UnboundLocalError."""
    from types import SimpleNamespace

    from esfex.visualization.workflows.grid_mapping_steps import (
        GridMappingDemandStep,
    )

    shown = []
    fake = SimpleNamespace(
        _forecast_result=None,
        _lbl_bld_status=SimpleNamespace(setText=shown.append))
    GridMappingDemandStep._distribute_by_spatial_demand(fake)
    assert shown and isinstance(shown[0], str)


def test_risk_workbench_opens_with_saved_settings(tmp_path, qapp, monkeypatch):
    """The workbench starts from the project's saved risk.* settings: they
    show in its controls, and Apply then saves them unchanged (it used to
    start from the defaults and overwrite them)."""
    from esfex.visualization.data.gui_model import GuiModel
    from esfex.visualization.workflows import risk_panels
    from esfex.visualization.workflows.risk_wizard import RiskWorkbench

    # Apply ends with a modal confirmation
    monkeypatch.setattr(risk_panels.QMessageBox, "information",
                        staticmethod(lambda *a, **k: None))

    d = yaml.safe_load(FIXTURE.read_text())
    d["risk"].update({
        "risk_measure": "cvar", "cvar_alpha": 0.9, "cvar_lambda": 0.3,
        "combination_method": "copula",
        "climate_scenarios": [{"name": "SSP5-8.5", "probability": 1.0,
                               "ssp_pathway": "SSP5-8.5"}],
        "hazard_scenarios": [{"name": "quake", "probability": 0.01,
                              "hazard_type": "earthquake", "recovery_hours": 720}],
    })
    src = tmp_path / "saved.yaml"
    src.write_text(yaml.safe_dump(d, sort_keys=False))
    cfg, states, gs = _load(src)

    model = GuiModel()
    model.global_settings = gs
    wb = RiskWorkbench(model=model, all_states=states)
    try:
        st = wb._state
        assert (st.risk_measure, st.cvar_alpha, st.cvar_lambda, st.combination_method) == (
            "cvar", 0.9, 0.3, "copula")
        assert [s["name"] for s in st.hazard_scenarios] == ["quake"]
        assert [s["name"] for s in st.climate_scenarios] == ["SSP5-8.5"]
        dash = wb._panels[0]._dashboard
        assert dash._combo_measure.currentText() == "CVaR"
        assert dash._combo_method.currentText() == "Copula"
        assert dash._spin_alpha.value() == pytest.approx(0.9)
        assert dash._spin_lambda.value() == pytest.approx(0.3)

        wb._panels[2]._apply()
    finally:
        wb.close()
        wb.deleteLater()

    out = tmp_path / "resaved.yaml"
    S.gui_state_to_yaml(states, cfg, out, global_settings=model.global_settings)
    rk = load_config(out).risk
    assert (rk.risk_measure, rk.cvar_alpha, rk.cvar_lambda, rk.combination_method) == (
        "cvar", 0.9, 0.3, "copula")
    assert [s.name for s in rk.hazard_scenarios] == ["quake"]
    assert rk.hazard_scenarios[0].recovery_hours == 720
    assert [s.name for s in rk.climate_scenarios] == ["SSP5-8.5"]
