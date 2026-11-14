"""Demand calendar features in a leap year (round 32).

Demand and temperature series are 8760 h with 29 February dropped, but the
day of week and the holidays were counted in real days from 1 January: in a
leap year every row from 1 March on was labelled with the previous day's
weekday and holidays. Every calendar feature now reads the real date of each
row (``temporal.year_row_dates``), at training and inference alike.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from esfex.utils.temporal import year_row_dates  # noqa: E402

MAR1 = 59 * 24                   # first row of 1 March in an 8760-h year
# 2024 is a leap year; 1 March 2024 is a Friday, 2 March a Saturday.
FRI, SAT = 4, 5


def test_year_row_dates_skips_feb29():
    stamps = year_row_dates(2024)
    assert len(stamps) == 8760
    assert stamps[MAR1 - 1] == pd.Timestamp("2024-02-28 23:00")
    assert stamps[MAR1] == pd.Timestamp("2024-03-01 00:00")
    assert stamps[-1] == pd.Timestamp("2024-12-31 23:00")
    full = year_row_dates(2024, 8784)
    assert full[MAR1] == pd.Timestamp("2024-02-29 00:00")
    assert year_row_dates(2023)[-1] == pd.Timestamp("2023-12-31 23:00")


def test_holiday_features_on_real_dates_in_leap_year():
    pytest.importorskip("holidays")
    from esfex.models.demand_training import _holiday_features

    is_hol, to_next, from_prev = _holiday_features("FRA", 2024)
    assert is_hol.shape == (8760,)
    # 14 July is row-day 194 (day 195 of the real leap year).
    jul14 = 194 * 24
    assert is_hol[jul14] == 1.0 and is_hol[jul14 + 24] == 0.0
    assert is_hol[jul14 - 24] == 0.0
    assert to_next[jul14 - 24] == 1.0 and from_prev[jul14 + 24] == 1.0
    # 25 December is the 24 rows before the last 7 days.
    assert is_hol[-7 * 24] == 1.0
    # Before 29 February nothing moves: 1 January.
    assert is_hol[0] == 1.0


def test_density_features_weekday_and_month_in_leap_year():
    from esfex.models.demand_density_ml import build_density_features

    order = ["dow_sin", "dow_cos", "month_sin", "month_cos", "is_weekend"]
    X = build_density_features(
        latitude=46.0, longitude=2.0, log_pop_density=4.0,
        log_gdp_density=10.0, log_gdp_per_cap=10.0,
        temperature_hourly=np.full(8760, 15.0), base_year=2024,
        feature_order=order)
    angle = np.arctan2(X[:, 0], X[:, 1]) % (2 * np.pi)
    dow = np.rint(angle * 7 / (2 * np.pi)).astype(int) % 7
    assert dow[MAR1] == FRI and dow[MAR1 + 24] == SAT
    assert X[MAR1 + 24, 4] == 1.0 and X[MAR1, 4] == 0.0
    assert dow[-1] == 1                          # 31 Dec 2024: Tuesday
    m_angle = np.arctan2(X[:, 2], X[:, 3]) % (2 * np.pi)
    month = np.rint(m_angle * 12 / (2 * np.pi)).astype(int) % 12
    assert month[MAR1 - 1] == 1 and month[MAR1] == 2


def test_hourly_features_weekday_per_simulation_year():
    from esfex.models.demand_ml import FEATURE_COLS, build_hourly_features

    X = build_hourly_features(
        gdp_per_capita=10_000.0, population=1e6, urbanization_pct=60.0,
        electricity_access_pct=100.0, temperature_hourly=np.full(8760, 20.0),
        latitude=0.0, longitude=0.0, base_year=2023, simulation_years=2)
    dow = X[:, FEATURE_COLS.index("day_of_week")]
    assert dow[MAR1] == 2                        # 1 Mar 2023: Wednesday
    assert dow[8760 + MAR1] == FRI               # 1 Mar 2024: Friday
    assert dow[-1] == 1                          # 31 Dec 2024: Tuesday


def test_tft_dataframe_calendar_in_leap_year():
    from esfex.models.demand_tft import build_tft_dataframe

    df = build_tft_dataframe(
        np.ones(8760), None, "FRA", 2024, 46.0, 2.0, 1e4, 1e6, 80.0, 100.0)
    assert df["is_weekend"].iloc[MAR1] == 0.0
    assert df["is_weekend"].iloc[MAR1 + 24] == 1.0
    month = np.rint(np.arctan2(df["month_sin"], df["month_cos"])
                    % (2 * np.pi) * 12 / (2 * np.pi)).astype(int) % 12
    assert month.iloc[MAR1 - 1] == 1 and month.iloc[MAR1] == 2
    assert month.iloc[-1] == 11


def test_training_rows_carry_real_dates_in_leap_year(tmp_path):
    from esfex.models import demand_training

    (tmp_path / "manifest.json").write_text(json.dumps(
        {"FRA": {"lat": 46.0, "lon": 2.0, "years": [2024]}}))
    (tmp_path / "FRA").mkdir()
    (tmp_path / "FRA" / "metadata.json").write_text(
        json.dumps({"source": "RTE"}))
    pd.DataFrame({"demand_mw": np.full(8760, 50_000.0)}).to_parquet(
        tmp_path / "FRA" / "FRA_2024.parquet")
    temp = np.full(8760, 15.0)
    feats = SimpleNamespace(
        temperature=temp, hdd=np.maximum(18 - temp, 0),
        cdd=np.maximum(temp - 24, 0), log_gdp_per_cap=10.0,
        log_pop_density=4.0, urbanization=0.8)
    with mock.patch("esfex.models.pixel_features.build_features_for_point",
                    return_value=feats):
        df = demand_training.build_unified_dataset(tmp_path)
    assert len(df) == 8760
    assert df["day_of_week"].iloc[MAR1] == FRI
    assert df["day_of_week"].iloc[MAR1 + 24] == SAT
    assert df["month"].iloc[MAR1] == 2
    assert df["day_of_week"].iloc[-1] == 1
    if df["is_holiday"].sum() > 0:               # holidays package present
        assert df["is_holiday"].iloc[194 * 24] == 1.0      # 14 July


def test_grid_builder_daily_split_uses_real_weekday():
    from esfex.visualization.workflows.demand_estimation_analysis import (
        DemandEstimationConfig, DemandProfileBuilder)

    shapes = {"shoulder_weekday": [1.0] * 24, "shoulder_saturday": [0.5] * 24,
              "shoulder_sunday": [0.25] * 24}
    builder = DemandProfileBuilder(DemandEstimationConfig(base_year=2024))
    daily = builder._distribute_daily(np.ones((1, 12, 1)),
                                      SimpleNamespace(shapes=shapes))
    d = daily[0, :, 0]
    # 1 Mar 2024 (row-day 59) is a Friday, 2 March a Saturday.
    assert d[60] < d[59]
    assert d[61] < d[60]                         # Sunday


def test_tft_annual_inference_calendar_in_leap_year():
    pytest.importorskip("torch")
    from esfex.models.tft_inference import TFTSensitivityBackend

    class _Stop(Exception):
        pass

    captured = {}

    def _from_parameters(_params, df, **_kw):
        captured["df"] = df
        raise _Stop

    backend = TFTSensitivityBackend.__new__(TFTSensitivityBackend)
    backend.encoder_length, backend.prediction_length = 72, 24
    backend._gdp_col, backend._pop_col = "log_gdp_per_cap", "log_pop_density"
    backend._need_elec_access = False
    backend.ds_params = {}
    backend._TSDClass = SimpleNamespace(from_parameters=_from_parameters)
    with pytest.raises(_Stop):
        backend.predict_annual_shape("FRA", 46.0, 2.0, 10.0, 4.0, 0.8,
                                     np.full(8760, 15.0), 2024)
    df = captured["df"]
    enc = 72
    assert len(df) == 8760 + enc
    # The encoder prefix is the end of 2023; row enc is 1 Jan 2024 00:00.
    assert df["day_of_week"].iloc[0] == 4        # 29 Dec 2023: Friday
    assert df["day_of_week"].iloc[enc] == 0      # 1 Jan 2024: Monday
    assert df["day_of_week"].iloc[enc + MAR1] == FRI
    assert df["month"].iloc[enc + MAR1] == 2
    assert df["month"].iloc[-1] == 11 and df["hour_of_day"].iloc[-1] == 23
    assert df["day_of_week"].iloc[-1] == 1       # 31 Dec 2024: Tuesday
