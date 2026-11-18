"""The shipped demand density model (v2, the article's final model) and its predictors.

The model was trained on series on UTC: hour and month in UTC, temperature of
that instant, weekday and holidays on the local date at a standard offset,
local solar hour and season-aligned month, the long-run climate of the place
and its electricity-use context; two rounds (level, then hourly from that
level). ``fixtures/demand_density_v2_rows.csv`` holds rows of the article's
training table with the article's predictors (``model.prepare``) and its
prediction (``model.predict`` on the original JSON boosters): ESFEX must build
the same predictors from the raw inputs and give the same prediction.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("xgboost")

from esfex.models import demand_density_features as F  # noqa: E402
from esfex.models import demand_density_ml as ddm  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "demand_density_v2_rows.csv"
MAR1 = 59 * 24                       # first row of 1 March in an 8760-h year


@pytest.fixture(scope="module")
def model():
    return ddm.DemandDensityModel.load_bundled()


@pytest.fixture(scope="module")
def rows():
    return pd.read_csv(FIXTURE, float_precision="round_trip")


def _series_features(model, g):
    """ESFEX's predictors for one series-year of the fixture: its rows of the
    8760 UTC rows of the year, from the raw inputs only."""
    y, iso3 = int(g.year.iloc[0]), g.country.iloc[0]
    idx = F.utc_row_hours(y)
    where = {t: i for i, t in enumerate(idx)}
    temp = np.zeros(len(idx), np.float32)
    on = g.time_idx.map(where)
    ok = on.notna().to_numpy()
    rws = on[ok].astype(int).to_numpy()
    temp[rws] = g.temperature.to_numpy()[ok]
    r0 = g.iloc[0]
    one = lambda v: np.array([v], np.float64)  # noqa: E731
    X = ddm.density_feature_block(
        latitude=one(r0.latitude), longitude=one(r0.longitude),
        log_pop_density=one(r0.log_pop_density), log_gdp_density=one(r0.log_gdp_density),
        log_gdp_per_cap=one(r0.log_gdp_per_cap), t_mean=one(r0.t_mean),
        t_range=one(r0.t_range), hh_elec_share=one(r0.hh_elec_share),
        bld_share_elec=one(r0.bld_share_elec), temperature_utc=temp, year=y,
        feature_order=model.feature_names, country_iso=iso3,
        offset_hours=one(model.context.std_offset(iso3, r0.longitude)))
    return X, rws, ok


# ── The shipped files ──────────────────────────────────────────────────


def test_shipped_model_is_the_articles_final_model(model):
    meta = model.meta
    assert meta["provenance"]["spec"] == "level_geo_two_shallow"
    assert meta["n_series"] == 166 and meta["n_countries"] == 83
    assert len(model.feature_names) == 26 and len(model.level_features) == 9
    assert model.level_features == ["log_pop_density", "log_gdp_density", "log_gdp_per_cap",
                                    "latitude", "longitude", "t_mean", "t_range",
                                    "hh_elec_share", "bld_share_elec"]
    assert model._model.feature_names == model.feature_names
    assert model._level.feature_names == model.level_features
    assert set(meta["cross_validation"]["shipped"]) == {"series_year", "country", "region"}


# ── Equivalence with the article ───────────────────────────────────────


def test_features_equal_the_training_table(model, rows):
    for _, g in rows.groupby(["series", "year"]):
        X, rws, ok = _series_features(model, g)
        mine = X[rws]
        theirs = g.loc[ok, model.feature_names].to_numpy(np.float32)
        same = (mine == theirs) | (np.isnan(mine) & np.isnan(theirs))
        bad = [f for j, f in enumerate(model.feature_names) if not same[:, j].all()]
        assert not bad, (g.series.iloc[0], bad)


def test_prediction_equals_the_articles(model, rows):
    for _, g in rows.groupby(["series", "year"]):
        X, rws, ok = _series_features(model, g)
        pred = model.predict_log_density(X)[rws]
        ref = g.prediction.to_numpy()[ok]
        assert np.max(np.abs(pred - ref)) < 1e-6, g.series.iloc[0]


def test_two_round_prediction_as_the_article_code(model, rows):
    """The article's own ``model.predict`` (copied in scripts/demand_model) on the
    shipped boosters: the level round is the base margin of the hourly round."""
    spec = importlib.util.spec_from_file_location(
        "article_model", ROOT / "scripts" / "demand_model" / "article" / "model.py")
    M = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(M)
    two = M.TwoRound(model._level, model._model, model.level_features)
    X = rows[model.feature_names]
    ref = M.predict(two, X, model.feature_names)
    np.testing.assert_allclose(model.predict_log_density(X.to_numpy(np.float32)), ref,
                               atol=1e-6)
    # without the level the hourly round alone is far off
    alone = model._model.predict(__import__("xgboost").DMatrix(
        X, feature_names=model.feature_names))
    assert np.max(np.abs(alone - ref)) > 0.1


# ── Calendar: UTC, local date, local solar time ────────────────────────


def test_rows_are_utc_hours_without_feb29():
    idx = F.utc_row_hours(2024)
    assert len(idx) == 8760 and idx[0] == 0
    assert idx[MAR1 - 1] == MAR1 - 1 and idx[MAR1] == MAR1 + 24   # 29 Feb skipped
    assert idx[-1] == 8783                                       # 31 Dec 23:00
    assert np.array_equal(F.utc_row_hours(2023), np.arange(8760))


def test_hour_is_utc_and_lhour_is_local_solar_time():
    havana_lon = -82.38
    cal = F.utc_calendar("CUB", 2023, np.arange(48), -5.0)
    hour = np.rint(np.arctan2(cal["hour_sin"], cal["hour_cos"]) * 24 / (2 * np.pi)) % 24
    assert np.array_equal(hour, np.arange(48) % 24)
    loc = F.local_solar_calendar(cal["hour_sin"], cal["hour_cos"], cal["month_sin"],
                                 cal["month_cos"], 23.13, havana_lon)
    solar = np.arctan2(loc["lhour_sin"], loc["lhour_cos"]) * 24 / (2 * np.pi) % 24
    np.testing.assert_allclose(solar, (np.arange(48) + havana_lon / 15) % 24, atol=1e-4)
    # solar noon in Havana is about 17:30 UTC
    assert abs(solar[17] - 11.5) < 0.1


def test_weekday_and_holidays_on_the_local_date():
    pytest.importorskip("holidays")
    # 1 Jan 2025 (Wednesday) is a Cuban holiday. At UTC-5 the first five UTC
    # hours are still 31 December (Tuesday); the article clips that day's
    # holiday index to 1 January, so they carry its holiday flag.
    cal = F.utc_calendar("CUB", 2025, np.arange(30), -5.0)
    dow = np.rint(np.arctan2(cal["dow_sin"], cal["dow_cos"]) * 7 / (2 * np.pi)) % 7
    assert np.all(dow[:5] == 1) and np.all(dow[5:29] == 2) and dow[29] == 3
    assert np.all(cal["is_holiday"][:29] == 1.0)        # 1 Jan, and 2 Jan from 05 UTC
    # the local date at UTC+9.5 (Adelaide offset) turns a day earlier
    east = F.utc_calendar("AUS", 2025, np.arange(30), 9.5)
    dow_e = np.rint(np.arctan2(east["dow_sin"], east["dow_cos"]) * 7 / (2 * np.pi)) % 7
    assert dow_e[14] == 2 and dow_e[15] == 3            # 14:30 UTC = 00:00 local, 2 Jan


def test_month_and_southern_hemisphere_shift():
    cal = F.utc_calendar("", 2023, np.array([0, 181 * 24]), 0.0)     # 1 Jan, 1 Jul
    north = F.local_solar_calendar(cal["hour_sin"], cal["hour_cos"], cal["month_sin"],
                                   cal["month_cos"], 40.0, 0.0)
    south = F.local_solar_calendar(cal["hour_sin"], cal["hour_cos"], cal["month_sin"],
                                   cal["month_cos"], -35.0, 0.0)
    np.testing.assert_array_equal(north["smonth_sin"], cal["month_sin"])
    np.testing.assert_array_equal(south["smonth_cos"], -cal["month_cos"])
    # January in the south reads as July in the north (rotation by half a year)
    assert south["smonth_cos"][0] == pytest.approx(north["smonth_cos"][1], abs=1e-6)


def test_leap_year_calendar_follows_the_training_table():
    idx = F.utc_row_hours(2024)
    cal = F.utc_calendar("", 2024, idx, 0.0)
    month = np.rint(np.arctan2(cal["month_sin"], cal["month_cos"]) * 12 / (2 * np.pi)) % 12
    dow = np.rint(np.arctan2(cal["dow_sin"], cal["dow_cos"]) * 7 / (2 * np.pi)) % 7
    assert dow[MAR1] == 4 and dow[MAR1 + 24] == 5            # Fri 1 Mar, Sat 2 Mar 2024
    assert dow[-1] == 1                                      # Tue 31 Dec 2024
    # The article counts months on a 365-day calendar from the UTC hour of the
    # year: in a leap year 31 March reads April, and 31 December (never in the
    # training table) stays December.
    mar31 = MAR1 + 30 * 24
    assert month[mar31 - 1] == 2 and month[mar31] == 3
    assert month[-1] == 11


def test_standard_offset_rule_of_the_article(model):
    ctx = model.context
    assert ctx.std_offset("CUB", -82.4) == -5
    assert ctx.std_offset("CU", -82.4) == -5                  # ISO2 accepted
    assert ctx.std_offset("ESP", -3.1) == 0                   # Canaries' zone is nearer
    assert ctx.std_offset("AUS", 145.0) == 9.5
    assert ctx.std_offset("", -82.4) == -5                    # longitude band


# ── Long-run climate and electricity-use context ───────────────────────


def test_climate_normals_from_daily_means():
    # two UTC days of hourly data: 10 C and 20 C
    t = np.r_[np.full(24, 10.0), np.full(24, 20.0)]
    daily = F.daily_means(t, np.arange(48))
    np.testing.assert_allclose(daily, [10.0, 20.0])
    days = np.linspace(0.0, 30.0, 301)
    m, r = F.climate_normals(days)
    assert m == pytest.approx(15.0, abs=1e-5)
    assert r == pytest.approx(np.quantile(days, 0.95) - np.quantile(days, 0.05), abs=1e-5)
    # the day of a UTC hour is idx // 24: a leap year's rows skip 29 February
    idx = F.utc_row_hours(2024)
    assert len(np.unique(idx // 24)) == 365


def test_gridded_normals_and_fallback(model):
    ctx = model.context
    tm, tr = ctx.normals([23.125], [-82.375])                 # Havana's cell
    assert 24.0 < tm[0] < 28.0 and 4.0 < tr[0] < 12.0
    tm, tr = ctx.normals([0.125], [-150.125])                 # open Pacific
    assert np.isnan(tm[0]) and np.isnan(tr[0])
    temp = np.where(np.arange(8760) // 24 % 2 == 0, 20.0, 30.0)
    p = ddm.place_predictors(model, [0.125], [-150.125], "", temp, 2025)
    assert p["t_mean"][0] == pytest.approx(25.0, abs=0.1)
    assert p["t_range"][0] == pytest.approx(10.0, abs=1e-6)


def test_energy_context_lookup_and_fallback(model):
    ctx = model.context
    cub = ctx.country("CUB")
    hh, bld = ctx.energy_context([21.875], [-79.375], "CUB")
    assert hh[0] == pytest.approx(cub["hh_elec_share"], rel=1e-6)
    assert bld[0] == pytest.approx(cub["bld_share_elec"], rel=1e-6)
    hh, _ = ctx.energy_context([30.875], [-97.125], "USA")      # Texas: its state value
    assert hh[0] == pytest.approx(0.688, abs=1e-3)
    assert hh[0] != pytest.approx(ctx.country("USA")["hh_elec_share"], abs=1e-3)
    hh, _ = ctx.energy_context([46.125], [-72.875], "CAN")      # Quebec
    assert hh[0] == pytest.approx(0.852, abs=1e-3)
    hh, bld = ctx.energy_context([24.125], [120.875], "TWN")    # no UN statistics
    assert np.isnan(hh[0]) and np.isnan(bld[0])
    # a cell off the grid (south of 60 S) takes the country's national value
    hh, _ = ctx.energy_context([-75.125], [-79.375], "CUB")
    assert hh[0] == pytest.approx(cub["hh_elec_share"], rel=1e-6)


# ── Output: UTC model rows → local time → the model clock ──────────────


def _havana_forecast(monkeypatch, utc_series):
    """The density engine for one node in Havana with a stub predictor whose
    every cell returns *utc_series* (shares of it), no CMIP6, constant GDP."""
    from esfex.visualization.workflows.demand_estimation_analysis import (
        DemandEstimationConfig,
        DemandProfileBuilder,
        MacroData,
        MeteoData,
        ProxyData,
    )
    monkeypatch.setattr(ddm, "fetch_cmip6_node_temps", lambda la, *a, **k: [None] * len(la))
    monkeypatch.setattr(ddm, "_ssp_gdp_total_nearest", lambda *a, **k: 1e10)

    def stub(model, lats, lons, areas, *a, **k):
        return np.tile(utc_series / len(lats), (len(lats), 1))

    monkeypatch.setattr(ddm, "predict_region_cell_demand", stub)
    cfg = DemandEstimationConfig(base_year=2025, simulation_years=1, num_nodes=1,
                                 national_demand_gwh=0.0)
    proxy = ProxyData(node_lats=[23.1], node_lons=[-82.4], node_names=["Havana"],
                      bounds=(22.9, -82.6, 23.2, -82.2))
    macro = MacroData(country_iso="CUB", gdp_per_capita=9500.0, population=2e6)
    res = DemandProfileBuilder(cfg).build(proxy, macro, MeteoData(
        temperature_hourly=list(np.full(8760, 26.0))))
    assert res.demand_source == "ml_density"
    return res


def test_utc_output_is_put_on_local_time_then_on_the_model_clock(monkeypatch, tmp_path):
    pytest.importorskip("PySide6.QtWidgets")
    from esfex.utils.model_clock import ModelClock
    from esfex.visualization.workflows.grid_mapping_steps import write_forecast_demand_csvs

    utc = np.arange(8760.0) + 1.0
    res = _havana_forecast(monkeypatch, utc)
    local = np.asarray(res.demand)[:, 0]
    # Havana is UTC-5 in winter, UTC-4 in summer (daylight saving time)
    assert local[0] == pytest.approx(utc[5])
    jul1 = 181 * 24
    assert local[jul1 + 12] == pytest.approx(utc[jul1 + 16])
    node = SimpleNamespace(name="Havana", centroid_lat=23.1, centroid_lng=-82.4, demand=None)
    write_forecast_demand_csvs([node], res, tmp_path / "a",
                               model_clock=ModelClock("Etc/GMT+5", -5.0))
    on_clock = np.asarray(node.demand.data)
    assert on_clock[0] == pytest.approx(utc[5]) and on_clock[jul1] == pytest.approx(utc[jul1 + 5])
    # on a UTC clock the series is the model's own UTC output again, except the
    # one autumn hour that local civil time repeats
    write_forecast_demand_csvs([node], res, tmp_path / "b", model_clock=ModelClock("UTC", 0.0))
    back = np.asarray(node.demand.data)
    assert np.sum(~np.isclose(back, utc)) <= 1


def test_leap_year_forecast_has_8760_rows_without_feb29(monkeypatch):
    from esfex.visualization.workflows import demand_estimation_analysis as dea

    seen = {}
    real = dea.DemandProfileBuilder._build_density

    def spy(self, *a, **k):
        seen["year"] = self._cfg.base_year
        return real(self, *a, **k)

    monkeypatch.setattr(dea.DemandProfileBuilder, "_build_density", spy)
    monkeypatch.setattr(ddm, "fetch_cmip6_node_temps", lambda la, *a, **k: [None] * len(la))
    monkeypatch.setattr(ddm, "_ssp_gdp_total_nearest", lambda *a, **k: 1e10)
    captured = {}

    def stub(model, lats, lons, areas, lpd, lgd, lgpc, temp, year, *a, **k):
        captured["year"], captured["n"] = year, len(temp)
        return np.ones((len(lats), 8760))

    monkeypatch.setattr(ddm, "predict_region_cell_demand", stub)
    cfg = dea.DemandEstimationConfig(base_year=2024, simulation_years=1, num_nodes=1,
                                     national_demand_gwh=0.0)
    proxy = dea.ProxyData(node_lats=[23.1], node_lons=[-82.4], node_names=["Havana"],
                          bounds=(22.9, -82.6, 23.2, -82.2))
    res = dea.DemandProfileBuilder(cfg).build(
        proxy, dea.MacroData(country_iso="CUB"), dea.MeteoData())
    assert seen["year"] == 2024 and captured == {"year": 2024, "n": 8760}
    assert np.asarray(res.demand).shape == (8760, 1)


# ── Temperature on UTC hours ───────────────────────────────────────────


def test_meteo_keeps_the_utc_temperature():
    from esfex.visualization.workflows.demand_estimation_analysis import (
        MeteoData,
        meteo_from_utc_temperature,
    )
    meteo = meteo_from_utc_temperature(np.arange(8784.0), 2024, 23.13, -82.38,
                                       [23.13], [-82.38])
    utc = np.asarray(meteo.temperature_utc_hourly)
    assert len(utc) == 8760 and utc[MAR1] == MAR1 + 24           # 29 Feb dropped
    assert np.array_equal(meteo.node_temperature_utc(0, 8760, 23.13, -82.38), utc)
    # a meteo object with only local series: moved from the node's zone to UTC
    local = MeteoData(temperature_hourly=list(np.arange(8760.0)), year=2023)
    t = local.node_temperature_utc(0, 8760, 23.13, -82.38)
    assert t[5] == 0.0 and t[181 * 24 + 4] == 181 * 24            # UTC-5 / UTC-4


def test_cmip6_curve_moved_to_utc_and_leap_day_dropped():
    pytest.importorskip("xarray")
    import xarray as xr

    from esfex.models.cmip6_zarr import _cell_to_hourly

    time = pd.date_range("2024-01-01", "2024-12-31", freq="D")
    tmax = np.where((time.month == 2) & (time.day == 29), 400.0, 300.0)
    ds = xr.Dataset({"tasmax": ("time", tmax), "tasmin": ("time", np.full(len(time), 290.0))},
                    coords={"time": time})
    h = _cell_to_hourly(ds, 2024, 2024)[2024]
    assert h.shape == (8760,) and h.max() < 30.0                  # 29 Feb (400 K) left out
    assert h[-24:].mean() == pytest.approx(h[:24].mean())          # 31 Dec kept


def test_xgb_predict_with_base_margin(model):
    import xgboost as xgb

    from esfex.models.xgb_device import predict

    X = np.random.default_rng(0).normal(size=(50, len(model.level_features))).astype(np.float32)
    bm = np.linspace(-2, 0, 50).astype(np.float32)
    ref = model._level.predict(xgb.DMatrix(X, feature_names=model.level_features, base_margin=bm))
    np.testing.assert_array_equal(
        predict(model._level, X, feature_names=model.level_features, base_margin=bm), ref)
