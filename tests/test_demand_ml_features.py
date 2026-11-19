"""The national demand model's inference features (``demand_ml``).

``build_hourly_features`` must fill every column of the shipped booster's
schema, by name, with the value the training dataset computes for it.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pytest

pd = pytest.importorskip("pandas")
pytest.importorskip("pyarrow")
xgb = pytest.importorskip("xgboost")

from esfex.models import demand_ml, demand_training  # noqa: E402
from esfex.models.demand_ml import FEATURE_COLS, build_hourly_features  # noqa: E402

LAT, LON = 23.13, -82.38
STATICS = {"log_gdp_per_cap": 8.7, "log_pop_density": 6.5, "urbanization": 23.0}


def _temperature(year: int = 2024) -> np.ndarray:
    h = np.arange(8760)
    return 25.0 + 4.0 * np.sin(2 * np.pi * h / 24) + 3.0 * np.sin(
        2 * np.pi * h / 8760 + year)


def _training_rows(tmp_path, iso3="CUB", year=2024):
    """The unified training dataset of one series (pixel features stubbed)."""
    (tmp_path / "manifest.json").write_text(json.dumps(
        {iso3: {"lat": LAT, "lon": LON, "years": [year]}}))
    (tmp_path / iso3).mkdir()
    (tmp_path / iso3 / "metadata.json").write_text(
        json.dumps({"source": "UNE", "lat": LAT, "lon": LON}))
    pd.DataFrame({"demand_mw": 1000 + 100 * np.random.default_rng(0)
                  .random(8760)}).to_parquet(
        tmp_path / iso3 / f"{iso3}_{year}.parquet")
    temp = _temperature(year)
    feats = SimpleNamespace(
        temperature=temp, hdd=np.maximum(18.0 - temp, 0.0),
        cdd=np.maximum(temp - 24.0, 0.0), **STATICS)
    with mock.patch("esfex.models.pixel_features.build_features_for_point",
                    return_value=feats):
        return demand_training.build_unified_dataset(tmp_path)


def test_features_match_training_dataset_column_by_column(tmp_path):
    df = _training_rows(tmp_path)
    cid = df.attrs["country_to_id"]["CUB"]
    X = build_hourly_features("CUB", _temperature(2024), LAT, LON, 2024,
                              static_features=STATICS, country_id=cid)
    assert X.shape == (8760, len(FEATURE_COLS))
    for j, name in enumerate(FEATURE_COLS):
        np.testing.assert_allclose(
            X[:, j], df[name].to_numpy(dtype=np.float64), rtol=1e-6,
            atol=1e-5, err_msg=name)
    # The columns the old positional builder left at 0 carry information.
    for name in ("is_holiday", "days_to_next_holiday", "temp_daily_max",
                 "temp_diurnal_range"):
        assert X[:, FEATURE_COLS.index(name)].any(), name


def test_shipped_booster_schema_and_country_map():
    model = demand_ml.DemandMLModel.load_bundled(engine="xgboost")
    booster = model._model
    assert booster.num_features() == len(FEATURE_COLS)
    assert booster.feature_names == FEATURE_COLS
    cmap = demand_ml.load_country_map()
    assert sorted(cmap.values()) == list(range(len(cmap)))
    assert list(cmap) == sorted(cmap)             # alphabetical, as training
    splits = booster.trees_to_dataframe()
    cid_splits = splits.loc[splits.Feature == "country_id", "Split"]
    assert cid_splits.max() <= len(cmap) - 1 + 1e-9
    assert demand_ml.country_id_for("CUB") == cmap["CUB"]


def test_prediction_on_shipped_booster():
    model = demand_ml.DemandMLModel.load_bundled(engine="xgboost")
    X = build_hourly_features("CUB", _temperature(2025), LAT, LON, 2025,
                              simulation_years=2, static_features=STATICS)
    shape = model.predict(X)
    assert shape.shape == (2 * 8760,)
    assert np.all(np.isfinite(shape)) and shape.mean() == pytest.approx(1.0)
    daily = shape[:8760].reshape(365, 24).mean(axis=0)
    assert daily.max() / daily.min() > 1.05       # a daily cycle, not flat


def test_unknown_country_gets_an_unseen_id(caplog):
    cmap = demand_ml.load_country_map()
    with caplog.at_level("WARNING", logger="esfex.models.demand_ml"):
        assert demand_ml.country_id_for("XYZ") == len(cmap)
    assert "XYZ" in caplog.text


def test_missing_country_map_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(demand_ml, "model_path",
                        lambda *a, **k: tmp_path / "demand_model.xgb")
    with pytest.raises(FileNotFoundError, match="country encoding"):
        demand_ml.load_country_map()


def test_growth_moves_the_static_features():
    X = build_hourly_features(
        "CUB", _temperature(), LAT, LON, 2025, simulation_years=3,
        static_features=STATICS, country_id=17.0,
        gdp_growth_by_year={2026: 0.10, 2027: 0.05},
        pop_growth_by_year={2026: 0.02})
    gdp = X[::8760, FEATURE_COLS.index("log_gdp_per_cap")]
    pop = X[::8760, FEATURE_COLS.index("log_pop_density")]
    np.testing.assert_allclose(
        gdp, 8.7 + np.log([1.0, 1.10, 1.10 * 1.05]))
    np.testing.assert_allclose(pop, 6.5 + np.log([1.0, 1.02, 1.02]))
    assert set(X[:, FEATURE_COLS.index("urbanization")]) == {23.0}


def test_temperature_must_cover_the_year():
    with pytest.raises(ValueError, match="8760"):
        build_hourly_features("CUB", np.full(100, 20.0), LAT, LON, 2025,
                              static_features=STATICS, country_id=17.0)
    X = build_hourly_features("CUB", None, LAT, LON, 2025,
                              static_features=STATICS, country_id=17.0)
    assert set(X[:, FEATURE_COLS.index("temperature")]) == {20.0}


def test_static_features_sampled_as_in_training(monkeypatch):
    calls = []

    def fake(lat, lon, year):
        calls.append(year)
        return 9.1, 5.2, 21.0, []

    monkeypatch.setattr("esfex.models.pixel_features.static_features_for_point",
                        fake)
    assert demand_ml.static_features_at(LAT, LON, 2035) == {
        "log_gdp_per_cap": 9.1, "log_pop_density": 5.2, "urbanization": 21.0}
    assert calls == [demand_ml.STATIC_RASTER_LAST_YEAR]
    demand_ml.static_features_at(LAT, LON, 2015)
    assert calls[-1] == 2015

    monkeypatch.setattr("esfex.models.pixel_features.static_features_for_point",
                        lambda lat, lon, year: (np.nan, 5.0, 10.0, ["gdp_total"]))
    with pytest.raises(ValueError, match="log_gdp_per_cap"):
        demand_ml.static_features_at(0.0, -30.0, 2025)


def test_scripting_estimate_demand():
    from esfex.visualization.scripting_api import _WorkflowsNamespace

    mw = _WorkflowsNamespace.estimate_demand(
        "CUB", LAT, LON, 2025, temperatures_hourly=_temperature(2025),
        annual_demand_mwh=8760 * 2000.0, static_features=STATICS)
    assert mw.shape == (8760,)
    assert mw.sum() == pytest.approx(8760 * 2000.0)
    shape = _WorkflowsNamespace.estimate_demand(
        "CUB", LAT, LON, 2025, temperatures_hourly=_temperature(2025),
        static_features=STATICS)
    np.testing.assert_allclose(shape * 2000.0, mw)


def test_studio_ml_engine_builds_the_training_features(monkeypatch):
    """The Demand Estimation fallback engine feeds the model the training
    features of each node (static ones sampled at the node, the country's
    holidays), not World Bank values in the wrong columns."""
    from esfex.visualization.workflows.demand_estimation_analysis import (
        DemandEstimationConfig, DemandProfileBuilder, MacroData, MeteoData,
        ProxyData,
    )

    sampled = []

    def fake_statics(lat, lon, year):
        sampled.append((lat, lon, year))
        return dict(STATICS)

    seen = []
    real_predict = demand_ml.DemandMLModel.predict

    def spy(self, features):
        seen.append(features)
        return real_predict(self, features)

    monkeypatch.setattr(demand_ml, "static_features_at", fake_statics)
    monkeypatch.setattr(demand_ml.DemandMLModel, "predict", spy)
    cfg = DemandEstimationConfig(base_year=2025, simulation_years=1,
                                 num_nodes=1, use_density_model=False,
                                 ml_engine="xgboost")
    proxy = ProxyData(node_lats=[LAT], node_lons=[LON], node_names=["H"],
                      building_weights=[1.0])
    macro = MacroData(country_iso="CUB", gdp_per_capita=9500.0,
                      population=11_000_000.0)
    meteo = MeteoData(node_temperature_hourly={0: list(_temperature(2025))},
                      year=2025)
    res = DemandProfileBuilder(cfg).build(proxy, macro, meteo)
    assert res.demand_source == "ml_xgboost"
    assert sampled == [(LAT, LON, 2025)]
    X = seen[0]
    col = {n: X[:, j] for j, n in enumerate(FEATURE_COLS)}
    assert set(col["country_id"]) == {demand_ml.load_country_map()["CUB"]}
    assert set(col["log_gdp_per_cap"]) == {STATICS["log_gdp_per_cap"]}
    assert set(col["latitude"]) == {LAT}
    assert col["is_holiday"][:24].tolist() == [1.0] * 24    # 1 Jan in Cuba
