"""Dataset builds keep their country map; training reuses the built dataset.

- ``build_unified_dataset`` writes the ISO3 → country_id map next to the
  dataset, never over the map of a model already in MODELS_DIR.
- ``train_xgboost`` saves the map next to the model it saves.
- ``train_demand_model`` builds the dataset once and hands it (with
  ``exclude_country`` applied) to ``train_xgboost``: no second build, no
  leak of the excluded country.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pandas as pd
import pytest

from esfex.models import demand_training
from esfex.models.demand_training import COUNTRY_MAP_FILENAME


def _write_dataset(root, countries=("CUB", "FRA")):
    (root / "manifest.json").write_text(json.dumps(
        {c: {"lat": 20.0 + i, "lon": -80.0 + i, "years": [2023]}
         for i, c in enumerate(countries)}))
    for c in countries:
        (root / c).mkdir()
        (root / c / "metadata.json").write_text(json.dumps({"source": "test"}))
        pd.DataFrame({"demand_mw": np.full(8760, 100.0)}).to_parquet(
            root / c / f"{c}_2023.parquet")


def _feats():
    temp = np.full(8760, 25.0)
    return SimpleNamespace(
        temperature=temp, hdd=np.maximum(18 - temp, 0),
        cdd=np.maximum(temp - 24, 0), log_gdp_per_cap=9.0,
        log_pop_density=4.0, urbanization=0.7)


@pytest.fixture
def models_dir(tmp_path, monkeypatch):
    """A MODELS_DIR holding a deployed model's country map."""
    d = tmp_path / "models"
    d.mkdir()
    (d / COUNTRY_MAP_FILENAME).write_text(json.dumps({"DEPLOYED": 0}))
    import esfex.paths
    monkeypatch.setattr(esfex.paths, "MODELS_DIR", d)
    monkeypatch.setattr(demand_training, "MODELS_DIR", d)
    return d


def _build(dataset):
    with mock.patch("esfex.models.pixel_features.build_features_for_point",
                    return_value=_feats()):
        return demand_training.build_unified_dataset(dataset)


def test_dataset_build_leaves_deployed_country_map_alone(tmp_path, models_dir):
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    _write_dataset(dataset)
    df = _build(dataset)

    assert json.loads((models_dir / COUNTRY_MAP_FILENAME).read_text()) == {
        "DEPLOYED": 0}
    expected = {"CUB": 0, "FRA": 1}
    assert df.attrs["country_to_id"] == expected
    assert json.loads((dataset / COUNTRY_MAP_FILENAME).read_text()) == expected


def test_train_xgboost_saves_the_map_next_to_its_model(tmp_path, models_dir):
    pytest.importorskip("xgboost")
    n = 48
    df = pd.DataFrame({c: np.linspace(0.0, 1.0, n)
                       for c in demand_training.FEATURE_COLS})
    df["country_id"] = 2.0
    df["country"] = "MEX"
    df["group_id"] = "MEX_2023"
    df[demand_training.TARGET_COL] = np.linspace(0.5, 1.5, n)
    out = tmp_path / "loco" / "model.xgb"
    out.parent.mkdir()
    full_map = {"CUB": 0, "FRA": 1, "MEX": 2}
    with mock.patch.object(demand_training, "temporal_cv_splits",
                           return_value=[(df, df, 2023)]):
        demand_training.train_xgboost(df, out, country_to_id=full_map)

    assert out.exists()
    assert json.loads((out.parent / COUNTRY_MAP_FILENAME).read_text()) == full_map
    assert json.loads((models_dir / COUNTRY_MAP_FILENAME).read_text()) == {
        "DEPLOYED": 0}


def test_xgboost_training_keeps_the_excluded_country_in_the_map(
        tmp_path, models_dir):
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    _write_dataset(dataset)
    real_build = demand_training.build_unified_dataset
    builds = []

    def counting_build(*args, **kwargs):
        builds.append(args)
        return real_build(*args, **kwargs)

    fake_xgb = mock.MagicMock(return_value={"engine": "xgboost"})
    with mock.patch("esfex.models.pixel_features.build_features_for_point",
                    return_value=_feats()), \
            mock.patch.object(demand_training, "build_unified_dataset",
                              side_effect=counting_build), \
            mock.patch.object(demand_training, "train_xgboost", fake_xgb):
        demand_training.train_demand_model(
            dataset_dir=dataset, exclude_country="CUB",
            output_path=tmp_path / "m.xgb")

    assert len(builds) == 1
    df = fake_xgb.call_args.args[0]
    assert set(df["country"]) == {"FRA"}
    assert fake_xgb.call_args.kwargs["country_to_id"] == {"CUB": 0, "FRA": 1}
