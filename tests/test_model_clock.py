"""The model clock: one time base for every hourly series (round 30).

Weather availability came in UTC while demand was in local time, so a Cuban
case had its PV peak at hour 17 (solar noon in UTC) against a local-time
demand. Every series is now saved on one fixed-offset clock.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest
import yaml

from esfex.utils.model_clock import (
    ModelClock,
    format_offset,
    local_to_model_clock,
    log_case_clock,
    nearest_land_zone,
    nominal_zone,
    node_time_zone,
    normalize_clock_setting,
    resolve_for_nodes,
    resolve_model_clock,
    to_model_clock,
    utc_to_model_clock,
    weather_source_zone,
    weather_to_model_clock,
)

HAVANA = (23.13, -82.38)
MEXICO_CITY = (19.43, -99.13)
CANCUN = (21.16, -86.85)
HOURS = np.arange(8760.0)
# Clock hours (2023, no leap day): 15 Jan 12:00 and 15 Jul 12:00.
JAN15_NOON = 14 * 24 + 12
JUL15_NOON = (31 + 28 + 31 + 30 + 31 + 30 + 14) * 24 + 12


# ── Helpers ─────────────────────────────────────────────────────────────


def test_havana_clock_is_its_standard_offset():
    clock = ModelClock.from_zone("America/Havana")
    assert clock.offset_hours == -5.0
    assert clock.label == "America/Havana standard time (UTC-05:00)"
    # A zone with DST configured explicitly is used at its standard offset.
    assert ModelClock.from_zone("Europe/Madrid").offset_hours == 1.0
    assert ModelClock.from_zone("Australia/Sydney").offset_hours == 10.0
    assert ModelClock.from_zone("Etc/GMT+5").offset_hours == -5.0
    assert format_offset(5.5) == "UTC+05:30"


def test_utc_series_on_havana_clock_reads_utc_hour_5_at_local_hour_0():
    out = utc_to_model_clock(HOURS, "America/Havana", 2023)
    assert out[0] == 5.0          # 00:00 UTC-5 = 05:00 UTC
    assert out[JUL15_NOON] == JUL15_NOON + 5
    # The year wraps: the last five clock hours read the first UTC hours.
    assert out[-5:].tolist() == [0.0, 1.0, 2.0, 3.0, 4.0]


def test_local_dst_series_on_standard_clock():
    """Havana demand in local civil time (DST in summer) on the standard clock."""
    local = HOURS % 24             # value = local hour of day
    out = to_model_clock(local, "America/Havana", "America/Havana", 2023)
    assert out[JAN15_NOON] == 12.0  # winter: local time = clock
    assert out[JUL15_NOON] == 13.0  # summer: 12:00 standard = 13:00 DST
    # Spring forward (12 Mar 2023, 00:00 → 01:00): local 00:00 is skipped.
    mar12 = (31 + 28 + 11) * 24
    assert out[mar12 - 1] == 23.0 and out[mar12] == 1.0
    # Fall back (5 Nov 2023, 01:00 → 00:00): local 00:00 is read twice.
    nov5 = (31 + 28 + 31 + 30 + 31 + 30 + 31 + 31 + 30 + 31 + 4) * 24
    assert out[nov5 - 1] == 0.0 and out[nov5] == 0.0 and out[nov5 + 1] == 1.0


def test_fractional_offset_interpolates_and_wraps():
    out = utc_to_model_clock(HOURS, "Asia/Kolkata", 2023)  # UTC+05:30
    assert out[0] == pytest.approx(8760 - 5.5)  # wraps to 31 Dec 18:30 UTC
    assert out[10] == pytest.approx(4.5)


def test_leap_year_series_keeps_its_length():
    out = utc_to_model_clock(np.arange(8784.0), "America/Havana", 2024)
    assert out.shape == (8784,) and out[0] == 5.0 and out[-1] == 4.0
    # Two 8760-h years: each block wraps within its own year.
    two = utc_to_model_clock(np.concatenate([HOURS, HOURS + 10_000]),
                             "America/Havana", 2023)
    assert two[8760] == 10_005.0 and two[-1] == 10_004.0


def test_multi_zone_nodes_pick_the_zone_with_most_demand():
    coords = [HAVANA, MEXICO_CITY, CANCUN]
    # By node count: Havana (-5) and Cancún (-5) outnumber Mexico City (-6).
    clock = resolve_model_clock("auto", coords)
    assert clock == ModelClock("America/Havana", -5.0)
    # By demand: Mexico City holds most of it.
    clock = resolve_model_clock("auto", coords, weights=[1.0, 5.0, 1.0])
    assert clock == ModelClock("America/Mexico_City", -6.0)
    # A configured clock wins over the nodes.
    assert resolve_model_clock("UTC", coords) == ModelClock("UTC", 0.0)


def test_studio_nodes_weigh_by_their_demand():
    nodes = [
        SimpleNamespace(centroid_lat=HAVANA[0], centroid_lng=HAVANA[1],
                        demand=SimpleNamespace(total_mwh=10.0)),
        SimpleNamespace(centroid_lat=MEXICO_CITY[0], centroid_lng=MEXICO_CITY[1],
                        demand=SimpleNamespace(total_mwh=90.0)),
    ]
    assert resolve_for_nodes("auto", nodes).zone == "America/Mexico_City"


def test_sea_point_far_from_land_falls_back_to_nominal_offset(caplog):
    with caplog.at_level(logging.WARNING, logger="esfex.utils.model_clock"):
        zone = node_time_zone(30.0, -40.0)   # mid-Atlantic, > 200 km from land
    assert zone == "Etc/GMT+3"               # UTC-3, no DST
    assert "No land time zone" in caplog.text
    assert node_time_zone(*HAVANA) == "America/Havana"
    assert nominal_zone(0.0) == "UTC" and nominal_zone(-82.4) == "Etc/GMT+5"
    assert nominal_zone(8.0) == "Etc/GMT-1"


def test_sea_point_near_land_takes_nearest_land_zone(caplog):
    # An offshore node south of Cuba (~35 km from the coast) keeps Cuba's zone,
    # with its daylight saving time, not the nominal Etc/GMT+5 of its band.
    node_time_zone.cache_clear()
    with caplog.at_level(logging.INFO, logger="esfex.utils.model_clock"):
        assert node_time_zone(21.0, -82.0) == "America/Havana"
    assert "nearest land" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    zone, km = nearest_land_zone(21.0, -82.0)
    assert zone == "America/Havana" and 20.0 <= km <= 50.0
    # Within the search radius only.
    assert nearest_land_zone(21.0, -82.0, max_km=10.0) is None
    assert nearest_land_zone(30.0, -40.0) is None


def test_clock_setting_validation():
    from esfex.config.schema import TemporalConfig

    assert TemporalConfig().model_clock == "auto"
    assert TemporalConfig(model_clock="utc").model_clock == "UTC"
    assert TemporalConfig(model_clock="America/Havana").model_clock == "America/Havana"
    with pytest.raises(ValueError, match="model_clock"):
        TemporalConfig(model_clock="Mars/Olympus_Mons")
    assert normalize_clock_setting(None) == "auto"


def test_weather_backends_time_base():
    assert weather_source_zone("open_meteo", -82.4) == "UTC"
    assert weather_source_zone("era5_atlite", -82.4) == "UTC"
    # NASA POWER answers in local solar time: the 15° longitude band.
    assert weather_source_zone("nasa_power", -97.0) == "Etc/GMT+6"
    lst = weather_to_model_clock(HOURS, "nasa_power", -97.0, "America/Havana", 2023)
    assert lst[10] == 9.0   # 10:00 at UTC-5 = 09:00 at UTC-6


# ── Availability generator ──────────────────────────────────────────────


def _avail_config(model_clock=None):
    gen = SimpleNamespace(fuel="Solar", type="Renewable", rated_power=[50.0])
    nodes = SimpleNamespace(node_coordinates=[
        SimpleNamespace(latitude=HAVANA[0], longitude=HAVANA[1])])
    cfg = SimpleNamespace(systems={"cuba": SimpleNamespace(
        generators={"pv": gen}, nodes=nodes)})
    if model_clock is not None:
        cfg.temporal = SimpleNamespace(model_clock=model_clock)
    return cfg


@patch("esfex.plugins.availability_generator.generator.compute_solar_hourly_cf")
def test_generator_saves_series_on_the_model_clock(mock_solar, tmp_path):
    from esfex.plugins.availability_generator.generator import (
        config_model_clock,
        generate_availability_profiles,
    )

    mock_solar.return_value = HOURS.copy()   # value = UTC hour index
    cfg = _avail_config()                     # model_clock auto → Havana
    assert config_model_clock(cfg).zone == "America/Havana"
    out = generate_availability_profiles(cfg, [2023], tmp_path, "open_meteo")
    saved = np.loadtxt(out["cuba/pv"], delimiter=",")
    assert saved[0] == 5.0 and saved[-1] == 4.0

    # A case that records UTC keeps the series as they come.
    out = generate_availability_profiles(_avail_config("UTC"), [2023],
                                         tmp_path / "utc", "open_meteo")
    assert np.loadtxt(out["cuba/pv"], delimiter=",")[0] == 0.0


def test_update_config_yaml_records_the_clock(tmp_path):
    from esfex.plugins.availability_generator.generator import update_config_yaml

    path = tmp_path / "case.yaml"
    path.write_text(yaml.safe_dump({
        "temporal": {"resolution_hours": 1},
        "systems": {"cuba": {"generators": {"pv": {"name": "PV"}}}},
    }))
    update_config_yaml(path, {"cuba/pv": tmp_path / "pv.csv"},
                       model_clock=ModelClock.from_zone("America/Havana"))
    data = yaml.safe_load(path.read_text())
    assert data["temporal"]["model_clock"] == "America/Havana"
    assert data["temporal"]["resolution_hours"] == 1
    assert data["systems"]["cuba"]["generators"]["pv"]["Availability"] == "pv.csv"


def test_grid_builder_hook_saves_weather_on_the_model_clock(tmp_path):
    from esfex.plugins.availability_generator import grid_builder_hook as gbh

    gen = SimpleNamespace(latitude=HAVANA[0], longitude=HAVANA[1], fuel="Wind",
                          rated_power=10.0, availability_file=None)
    node = SimpleNamespace(centroid_lat=HAVANA[0], centroid_lng=HAVANA[1],
                           demand=None)
    state = SimpleNamespace(generators={"w0": gen}, nodes=[node])
    key = gbh._weather_cache_key("wind", gen, 2023, "open_meteo")
    with patch.object(gbh, "_prefetch_weather_cfs",
                      lambda *a, **k: {key: HOURS.copy()}):
        gbh.generate_for_grid_build(state, tmp_path / "a", use_weather_data=True,
                                    weather_year=2023)
        assert np.loadtxt(gen.availability_file)[0] == 5.0  # auto → Havana
        gbh.generate_for_grid_build(state, tmp_path / "b", use_weather_data=True,
                                    weather_year=2023,
                                    model_clock=ModelClock("UTC", 0.0))
        assert np.loadtxt(gen.availability_file)[0] == 0.0


# ── Grid Builder demand ─────────────────────────────────────────────────


def test_grid_builder_demand_saved_on_the_model_clock(tmp_path):
    """The forecast is in each node's local civil time (its hour of day,
    weekday and holidays are local); the saved demand is on the clock."""
    from esfex.visualization.workflows.grid_mapping_steps import (
        write_forecast_demand_csvs,
    )

    nodes = [
        SimpleNamespace(name="Havana", demand=None,
                        centroid_lat=HAVANA[0], centroid_lng=HAVANA[1]),
        SimpleNamespace(name="CDMX", demand=None,
                        centroid_lat=MEXICO_CITY[0], centroid_lng=MEXICO_CITY[1]),
    ]
    local_hour = np.tile((HOURS % 24).reshape(-1, 1), (2, 2))  # 2 years x 2 nodes
    result = SimpleNamespace(demand_multi_year=local_hour, demand=None,
                             peak_mw=[], config=SimpleNamespace(base_year=2023))
    clock = ModelClock.from_zone("America/Havana")
    assert write_forecast_demand_csvs(nodes, result, tmp_path, model_clock=clock) == 2

    havana = np.loadtxt(nodes[0].demand.csv_path)
    cdmx = np.loadtxt(nodes[1].demand.csv_path)
    assert havana.shape == (2 * 8760,)
    assert havana[JAN15_NOON] == 12.0       # standard time in winter
    assert havana[JUL15_NOON] == 13.0       # 12:00 standard = 13:00 Cuban DST
    assert cdmx[JAN15_NOON] == 11.0         # UTC-6, no DST: one hour behind
    assert cdmx[JUL15_NOON] == 11.0
    assert havana[8760 + JUL15_NOON] == 13.0  # second year (2024) too
    assert nodes[0].demand.data == havana.tolist()

    # Without a clock the forecast is written as it is (unchanged behaviour).
    write_forecast_demand_csvs(nodes, result, tmp_path / "raw")
    assert np.loadtxt(nodes[0].demand.csv_path)[JUL15_NOON] == 12.0


# ── Config round-trip and runner ────────────────────────────────────────


def test_model_clock_round_trips_through_the_studio(tmp_path):
    pytest.importorskip("PySide6")
    from esfex.config.loader import load_config
    from esfex.visualization.data.serializer import (
        config_to_global_settings,
        config_to_gui_states,
        gui_state_to_yaml,
    )

    fixture = Path(__file__).parent / "fixtures" / "studio_export_island.yaml"
    raw = yaml.safe_load(fixture.read_text(encoding="utf-8"))
    raw["temporal"]["model_clock"] = "America/Havana"
    src = tmp_path / "case.yaml"
    src.write_text(yaml.safe_dump(raw), encoding="utf-8")

    cfg = load_config(src)
    g = config_to_global_settings(cfg)
    assert g.model_clock == "America/Havana"
    out = tmp_path / "resaved.yaml"
    gui_state_to_yaml(config_to_gui_states(cfg, base_dir=fixture.parent), cfg,
                      out, global_settings=g)
    assert load_config(out).temporal.model_clock == "America/Havana"


def _orchestrator(model_clock, coords):
    from esfex.config.schema import GeoCoordinate
    from esfex.runner import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)   # only what the log reads
    orch._system_names = ["a"]
    orch.config = SimpleNamespace(
        temporal=SimpleNamespace(model_clock=model_clock),
        systems={"a": SimpleNamespace(nodes=SimpleNamespace(node_coordinates=[
            GeoCoordinate(latitude=la, longitude=lo) for la, lo in coords]))})
    return orch


def test_runner_logs_the_model_clock_and_node_zones(caplog):
    orch = _orchestrator("America/Havana", [HAVANA, MEXICO_CITY])
    with caplog.at_level(logging.INFO, logger="esfex.utils.model_clock"):
        orch._log_model_clock()
    assert "Model clock: America/Havana standard time (UTC-05:00)" in caplog.text
    assert "America/Mexico_City (UTC-06:00, 1 node)" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_runner_warns_when_the_case_has_no_clock(caplog):
    orch = _orchestrator("auto", [HAVANA])
    with caplog.at_level(logging.INFO, logger="esfex.utils.model_clock"):
        orch._log_model_clock()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "time base of its hourly series is unknown" in warnings[0].getMessage()
    assert log_case_clock("auto") is None


# ── Local civil time (round 31: demand-model temperature) ──────────────


def test_utc_to_local_time_pairs_each_local_hour_with_its_instant():
    from esfex.utils.model_clock import utc_to_local_time

    utc = np.arange(8760.0)
    loc = utc_to_local_time(utc, "America/Havana", 2023)
    assert loc[0] == 5.0            # 1 Jan 00:00 CST = 05:00 UTC
    assert loc[-1] == 4.0           # 31 Dec 23:00 CST = 1 Jan 04:00 UTC (wraps)
    # Summer (CDT, UTC-4): 1 Jul 12:00 local is 16:00 UTC.
    noon = (31 + 28 + 31 + 30 + 31 + 30) * 24 + 12
    assert loc[noon] == noon + 4
    # Tokyo (UTC+9): 1 Jan 09:00 local = 00:00 UTC.
    assert utc_to_local_time(utc, "Asia/Tokyo", 2023)[9] == 0.0


def test_to_local_time_dst_transitions():
    import pandas as pd

    from esfex.utils.model_clock import to_local_time, utc_to_local_time

    wall = pd.date_range("2023-01-01", periods=8760, freq="h")
    loc = utc_to_local_time(np.arange(8760.0), "America/Havana", 2023)
    # 12 Mar 2023 00:00 does not exist in Havana: it holds the hour after it.
    i = int(np.where(wall == pd.Timestamp("2023-03-12 00:00"))[0][0])
    assert loc[i] == loc[i + 1] == i + 5
    # 5 Nov 2023 00:00 occurs twice: its first (CDT) occurrence.
    j = int(np.where(wall == pd.Timestamp("2023-11-05 00:00"))[0][0])
    assert loc[j] == j + 4 and loc[j + 1] == j + 1 + 5
    # A series already on that local time stays as it is (but the skipped hour).
    same = to_local_time(np.arange(8760.0), "America/Havana", "America/Havana", 2023)
    changed = np.flatnonzero(same != np.arange(8760.0))
    assert changed.tolist() == [i]


def test_to_local_time_columns_leap_year_and_bad_zone():
    from esfex.utils.model_clock import to_local_time, utc_to_local_time

    two = np.column_stack([np.arange(8784.0)] * 2)
    out = utc_to_local_time(two, ["Asia/Tokyo", "Europe/Madrid"], 2024)
    assert out.shape == (8784, 2)
    assert out[9, 0] == 0.0 and out[1, 1] == 0.0
    # A fixed-offset source (EST all year, e.g. IESO) onto Toronto civil time.
    est = to_local_time(np.arange(8760.0), "Etc/GMT+5", "America/Toronto", 2023)
    assert est[0] == 0.0
    noon = (31 + 28 + 31 + 30 + 31 + 30) * 24 + 12
    assert est[noon] == noon - 1    # 12:00 EDT is 11:00 EST
    with pytest.raises(Exception):
        utc_to_local_time(np.arange(8760.0), "Mars/Olympus", 2023)
