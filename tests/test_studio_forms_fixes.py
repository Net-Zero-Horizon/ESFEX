"""Studio form fixes (#88).

* A bus role picked in the bus form survives save (the role repair keeps it)
  and reload (``_gui_layout.user_set_bus_roles``).
* Geo files in a projected CRS are reprojected to WGS84 on import.
* Every value the N-1 Reserve Type combo offers is accepted by the schema.
* Degradation labels say "fraction" (the value is a fraction per year).
* The Min. System Inertia field accepts absolute MW·s values.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import yaml

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from esfex.visualization.data.gui_model import GuiBus, GuiModel, GuiSystemState
from esfex.visualization.workflows import grid_mapping_quality as Q
from tests.test_equipment_bus_roundtrip import _default_config


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _state(**kw):
    base = dict(
        generators={}, fuels={}, technologies={}, fuel_entry_points=[],
        nodes=[], buses={}, batteries={}, electrolyzers={}, transformers=[],
        transmission_lines=[],
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ── Bus roles ────────────────────────────────────────────────────────


def test_user_role_survives_repair():
    """An HV bus the user marked "load" is not demoted to "connection"."""
    hv = GuiBus("bus_0", parent_node=0, voltage_kv=220.0, role="load",
                demand_fraction=0.0, role_user_set=True)
    lv = GuiBus("bus_1", parent_node=0, voltage_kv=33.0, role="load",
                demand_fraction=1.0)
    st = _state(buses={"bus_0": hv, "bus_1": lv})
    Q.repair_bus_roles_and_demand(st)
    assert hv.role == "load"


def test_user_connection_is_not_promoted_and_gets_no_demand():
    """A user "connection" bus keeps its role even in a node with no load bus."""
    a = GuiBus("bus_0", parent_node=0, voltage_kv=220.0, role="connection",
               role_user_set=True)
    b = GuiBus("bus_1", parent_node=0, voltage_kv=220.0, role="connection")
    st = _state(buses={"bus_0": a, "bus_1": b})
    Q.repair_bus_roles_and_demand(st)
    assert a.role == "connection" and a.demand_fraction == 0.0
    # The other (free) bus carries the node's demand.
    assert b.role == "load" and b.demand_fraction == pytest.approx(1.0)


def test_user_mixed_bus_shares_demand():
    """A user "mixed" bus is a demand carrier in the redistribution."""
    m = GuiBus("bus_0", parent_node=0, voltage_kv=33.0, role="mixed",
               role_user_set=True)
    lv = GuiBus("bus_1", parent_node=0, voltage_kv=33.0, role="load")
    st = _state(buses={"bus_0": m, "bus_1": lv})
    Q.repair_bus_roles_and_demand(st)
    assert m.role == "mixed"
    assert m.demand_fraction == pytest.approx(0.5)
    assert lv.demand_fraction == pytest.approx(0.5)


def test_all_user_connection_node_is_reported(caplog):
    a = GuiBus("bus_0", parent_node=0, voltage_kv=220.0, role="connection",
               role_user_set=True)
    st = _state(buses={"bus_0": a})
    counts = Q.repair_bus_roles_and_demand(st)
    assert a.role == "connection" and a.demand_fraction == 0.0
    assert counts["nodes_without_demand_bus"] == 1
    assert "no bus to go to" in caplog.text


def test_unset_roles_are_still_repaired():
    hv = GuiBus("bus_0", parent_node=0, voltage_kv=220.0, role="load",
                demand_fraction=0.0)
    lv = GuiBus("bus_1", parent_node=0, voltage_kv=33.0, role="load",
                demand_fraction=1.0)
    st = _state(buses={"bus_0": hv, "bus_1": lv})
    Q.repair_bus_roles_and_demand(st)
    assert hv.role == "connection"


def test_user_role_survives_save_and_reload(tmp_path):
    from esfex.config.loader import load_config
    from esfex.visualization.data.serializer import (
        config_to_gui_states,
        gui_state_to_yaml,
    )

    name = "Roles"
    state = GuiSystemState(
        name=name,
        buses={
            # HV bus the user wants as a load bus (the repair would make it
            # "connection").
            "bus_0": GuiBus("bus_0", name="HV", parent_node=0, voltage_kv=220.0,
                            role="load", demand_fraction=0.4,
                            latitude=1.0, longitude=1.0, role_user_set=True),
            "bus_1": GuiBus("bus_1", name="LV", parent_node=0, voltage_kv=33.0,
                            role="load", demand_fraction=0.6,
                            latitude=1.0, longitude=1.1),
        },
    )
    out = tmp_path / "roles.yaml"
    gui_state_to_yaml({name: state}, base_config=_default_config(name),
                      output_path=out)
    data = yaml.safe_load(out.read_text())
    assert data["systems"][name]["_gui_layout"]["user_set_bus_roles"] == ["bus_0"]
    saved = {b["bus_id"]: b for b in data["systems"][name]["buses"]}
    assert saved["bus_0"]["role"] == "load"
    assert saved["bus_0"]["demand_fraction"] == pytest.approx(0.4)

    reloaded = config_to_gui_states(load_config(out), base_dir=str(tmp_path))[name]
    assert reloaded.buses["bus_0"].role_user_set is True
    assert reloaded.buses["bus_1"].role_user_set is False
    assert reloaded.buses["bus_0"].role == "load"


def test_bus_form_marks_role_as_user_set(qapp):
    from esfex.visualization.panels.bus_form import BusForm

    model = GuiModel()
    model.state.buses["bus_0"] = GuiBus("bus_0", voltage_kv=220.0, role="connection")
    form = BusForm(model)
    form.load_element("bus_0")
    assert model.state.buses["bus_0"].role_user_set is False
    form._name.setText("renamed")
    form._on_changed()  # editing another field does not fix the role
    assert model.state.buses["bus_0"].role_user_set is False
    form._role.setCurrentText("load")
    bus = model.state.buses["bus_0"]
    assert bus.role == "load" and bus.role_user_set is True


# ── Geo import reprojection ──────────────────────────────────────────


def test_geo_import_reprojects_projected_crs(tmp_path):
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import Point

    from esfex.visualization.main_window import _read_geo_file

    # Havana in UTM zone 17N.
    gdf = gpd.GeoDataFrame(
        {"name": ["p"]}, geometry=[Point(-82.38, 23.13)], crs="EPSG:4326",
    ).to_crs(epsg=32617)
    shp = tmp_path / "pts.gpkg"
    gdf.to_file(shp, driver="GPKG")
    _, data = _read_geo_file(str(shp))
    lon, lat = data["features"][0]["geometry"]["coordinates"][:2]
    assert lon == pytest.approx(-82.38, abs=1e-6)
    assert lat == pytest.approx(23.13, abs=1e-6)


def test_geo_import_reprojects_legacy_geojson_crs(tmp_path):
    pytest.importorskip("geopandas")
    from pyproj import Transformer

    from esfex.visualization.main_window import _read_geo_file

    x, y = Transformer.from_crs(4326, 3857, always_xy=True).transform(-82.38, 23.13)
    gj = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3857"}},
        "features": [{"type": "Feature", "properties": {},
                      "geometry": {"type": "Point", "coordinates": [x, y]}}],
    }
    p = tmp_path / "web.geojson"
    p.write_text(json.dumps(gj))
    _, data = _read_geo_file(str(p))
    lon, lat = data["features"][0]["geometry"]["coordinates"][:2]
    assert lon == pytest.approx(-82.38, abs=1e-6)
    assert lat == pytest.approx(23.13, abs=1e-6)


def test_wgs84_geojson_is_read_verbatim(tmp_path):
    from esfex.visualization.main_window import _read_geo_file

    gj = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"a": 1},
         "geometry": {"type": "Point", "coordinates": [1.5, 2.5]}}]}
    p = tmp_path / "plain.geojson"
    p.write_text(json.dumps(gj))
    _, data = _read_geo_file(str(p))
    assert data == gj


# ── Form values accepted by the model ────────────────────────────────


def test_reserve_type_choices_are_valid(qapp):
    from typing import get_args

    from esfex.config.schema import N1SecurityConfig
    from esfex.visualization.panels.global_settings_form import GlobalSettingsForm

    form = GlobalSettingsForm(GuiModel())
    combo = form._n1_gen_type
    offered = {combo.itemData(i) for i in range(combo.count())}
    allowed = set(get_args(N1SecurityConfig.model_fields["generation_reserve_type"].annotation))
    assert offered <= allowed


def test_n1_apply_modes_round_trip_form(qapp):
    from esfex.visualization.panels.global_settings_form import GlobalSettingsForm

    model = GuiModel()
    model.global_settings.n1_apply_to_modes = ["unit_commitment", "economic_dispatch"]
    form = GlobalSettingsForm(model)
    form.load_element()
    assert not form._n1_apply_dev.isChecked() and form._n1_apply_uc.isChecked()
    form._n1_apply_dev.setChecked(True)
    assert set(model.global_settings.n1_apply_to_modes) == {
        "unit_commitment", "economic_dispatch", "development"}


@pytest.mark.parametrize("lang", ["en", "es", "fr", "pt", "ja"])
def test_degradation_labels_are_fractions(lang):
    from pathlib import Path

    import esfex.visualization as vis

    d = json.loads((Path(vis.__file__).parent / "translations" / f"{lang}.json")
                   .read_text(encoding="utf-8"))
    for sec, key in [("generator_form", "degradation"),
                     ("battery_form", "degradation"),
                     ("acdc_form", "degradation_rate")]:
        assert "%" not in d[sec][key], (lang, sec, d[sec][key])


def test_reservoir_section_shows_for_new_hydro_unit(qapp):
    """#18: a new Water generator (capacity 0) can reach the Reservoir fields."""
    from esfex.visualization.data.gui_model import GuiGeneratorInstance
    from esfex.visualization.panels.generator_form import GeneratorForm

    model = GuiModel()
    model.state.generators["hydro"] = GuiGeneratorInstance(
        instance_id="hydro", unit_key="hydro", name="Hydro", gen_type="Renewable",
        fuel="Water", node=0, rated_power=50.0)
    model.state.generators["gas"] = GuiGeneratorInstance(
        instance_id="gas", unit_key="gas", name="Gas", gen_type="Non-renewable",
        fuel="Gas", node=0, rated_power=50.0)
    form = GeneratorForm(model)
    form.load_element("hydro")
    assert not form._reservoir_group.isHidden()
    form._reservoir_capacity.setValue(1000.0)
    assert model.state.generators["hydro"].reservoir_capacity == pytest.approx(1000.0)
    form.load_element("gas")
    assert form._reservoir_group.isHidden()


def test_inertia_field_takes_mws(qapp):
    from esfex.visualization.panels.system_form import SystemForm

    form = SystemForm(GuiModel())
    form._inertia_limit_thr.setValue(5000.0)
    assert form._inertia_limit_thr.value() == pytest.approx(5000.0)
