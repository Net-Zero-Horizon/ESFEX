"""Demand dataset fixes of round 32.

- ``build_dataset(sources=...)`` forwarded only six ``include_*`` flags, so
  ENTSO-E, RTE, UK, Eskom and the newer fetchers were always downloaded,
  whatever ``--sources`` said.
- RTE eco2mix: rows were paired as half-hours by count; with 15-minute data
  the first half-year filled all 8760 hours. Hourly means now come from the
  rows' timestamps, whatever the step.
- Zonal wall-time series (ONS Brazil before 2019 had daylight saving time):
  a label repeated when clocks fall back made the reindex raise. A repeated
  wall hour now keeps its first occurrence and a skipped one holds the hour
  after (``model_clock.to_local_time``'s convention).
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from esfex.models import demand_dataset as ds
from esfex.models import demand_real_data as rd


def _record_flags(monkeypatch):
    seen = {}

    def _fetch_all(**kw):
        seen.update(kw)
        return {}, {}

    monkeypatch.setattr(rd, "fetch_all_real_load", _fetch_all)
    return seen


def test_demand_sources_are_the_fetchers_flags():
    params = inspect.signature(rd.fetch_all_real_load).parameters
    flags = {p[len("include_"):] for p in params if p.startswith("include_")}
    assert set(rd.DEMAND_SOURCES) == flags


@pytest.mark.parametrize("name", rd.DEMAND_SOURCES)
def test_build_dataset_fetches_only_the_named_source(monkeypatch, tmp_path, name):
    seen = _record_flags(monkeypatch)
    ds.build_dataset(cache_dir=tmp_path, sources=[name.upper()])
    flags = {k: v for k, v in seen.items() if k.startswith("include_")}
    assert flags == {f"include_{n}": n == name for n in rd.DEMAND_SOURCES}


def test_build_dataset_all_and_unknown_sources(monkeypatch, tmp_path):
    seen = _record_flags(monkeypatch)
    ds.build_dataset(cache_dir=tmp_path, sources=["all"])
    assert all(seen[f"include_{n}"] for n in rd.DEMAND_SOURCES)
    seen.clear()
    ds.build_dataset(cache_dir=tmp_path)
    assert all(seen[f"include_{n}"] for n in rd.DEMAND_SOURCES)
    with pytest.raises(ValueError, match="unknown demand source"):
        ds.build_dataset(cache_dir=tmp_path, sources=["rte", "nordpool"])


def test_cli_sources_list_every_source_and_rejects_unknown(monkeypatch, tmp_path):
    from esfex.cli import app, build_demand_dataset

    help_text = inspect.signature(build_demand_dataset).parameters[
        "sources"].default.help
    for name in rd.DEMAND_SOURCES:
        assert name in help_text
    seen = _record_flags(monkeypatch)
    runner = CliRunner()
    res = runner.invoke(app, ["build-demand-dataset", "-s", "rte,uk",
                              "--cache-dir", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert {n for n in rd.DEMAND_SOURCES if seen[f"include_{n}"]} == {"rte", "uk"}
    res = runner.invoke(app, ["build-demand-dataset", "-s", "rte,nordpool"])
    assert res.exit_code == 2


# ── RTE eco2mix: hourly means by timestamp ────────────────────────────────


def _eco2mix_table(year: int, step_min: int, fill_quarters: bool = True):
    """An eco2mix-like table: French civil Date/Heures every *step_min*.

    Consumption = the CET hour index of the row's instant (so each hourly
    mean is that index); with ``fill_quarters=False`` only the half-hours
    carry values (older files).
    """
    start = pd.Timestamp(f"{year}-01-01", tz="Etc/GMT-1")
    inst = pd.date_range(start, start + pd.DateOffset(years=1),
                         freq=f"{step_min}min", inclusive="left")
    wall = inst.tz_convert("Europe/Paris")
    conso = np.floor((inst - start) / pd.Timedelta(hours=1)).astype(float)
    conso = np.asarray(conso)
    if not fill_quarters:
        conso[np.asarray(wall.minute % 30 != 0)] = np.nan
    return pd.DataFrame({
        "Périmètre": "France", "Nature": "Données définitives",
        "Date": wall.strftime("%Y-%m-%d"), "Heures": wall.strftime("%H:%M"),
        "Consommation": conso,
    })


@pytest.mark.parametrize("step_min, fill", [(15, True), (15, False), (30, True),
                                            (60, True)])
def test_rte_hourly_by_timestamps_any_step(step_min, fill):
    df = _eco2mix_table(2019, step_min, fill)
    arr = rd._rte_hourly(df, "Consommation", 2019)
    assert arr.shape == (8760,)
    assert np.array_equal(arr, np.arange(8760.0))


def test_rte_hourly_leap_year_drops_feb29_and_rejects_short_years():
    df = _eco2mix_table(2020, 15)
    arr = rd._rte_hourly(df, "Consommation", 2020)
    feb29 = 59 * 24
    assert arr.shape == (8760,)
    assert arr[feb29] == feb29 + 24 and arr[-1] == 8783
    assert rd._rte_hourly(df.iloc[: len(df) // 2], "Consommation", 2020) is None


def test_rte_hourly_without_date_columns_infers_the_step():
    df = _eco2mix_table(2019, 15)[["Consommation"]]
    arr = rd._rte_hourly(df, "Consommation", 2019)
    assert np.array_equal(arr, np.arange(8760.0))


@pytest.mark.parametrize("ext", [".csv", ".xls"])
def test_fetch_rte_france_aggregates_quarter_hours(monkeypatch, tmp_path, ext):
    import io
    import zipfile

    import requests

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        text = _eco2mix_table(2019, 15).to_csv(sep="\t", index=False)
        text += "RTE ne pourra être tenu responsable\n"
        zf.writestr(f"eCO2mix_RTE_Annuel-Definitif_2019{ext}",
                    text.encode("latin-1"))
    resp = SimpleNamespace(status_code=200, content=buf.getvalue())
    monkeypatch.setattr(requests, "get", lambda url, timeout=0: resp)
    monkeypatch.setattr(rd.time, "sleep", lambda s: None)
    out = rd.fetch_rte_france(tmp_path, years=range(2019, 2020))
    # Before: quarter-hours were paired as half-hours, so the first half of
    # the year was stretched over all 8760 hours.
    assert np.array_equal(out["FRA"][2019], np.arange(8760.0))


# ── Zonal series on a DST wall clock ──────────────────────────────────────


def _sao_paulo_wall_series(year: int, order: str = "time"):
    """Wall-time labels of every UTC hour around *year* in São Paulo.

    Values = UTC hours since 1 Jan of *year* 00:00 UTC. 2018 has daylight
    saving time: 23:00 on 17 February is repeated and 00:00 on 4 November
    skipped.
    """
    t0 = pd.Timestamp(f"{year}-01-01", tz="UTC")
    inst = pd.date_range(t0 - pd.Timedelta(hours=6),
                         t0 + pd.Timedelta(days=366), freq="h")
    wall = inst.tz_convert("America/Sao_Paulo").tz_localize(None)
    vals = np.asarray((inst - t0) / pd.Timedelta(hours=1), dtype=float)
    return pd.Series(vals, index=wall).sort_index(kind="stable"), t0


def test_zonal_align_handles_dst_fall_back_and_spring_forward():
    from esfex.models.demand_zonal_fetchers import (
        ZONAL_TIME_BASES, _align_year_8760)

    series, t0 = _sao_paulo_wall_series(2018)
    assert series.index.duplicated().any()
    arr = _align_year_8760(series, 2018, ZONAL_TIME_BASES["BRA"])
    assert arr.shape == (8760,)
    # Expected: each wall hour at its instant, the repeated one at its first
    # occurrence (summer time), the skipped one at the instant just after.
    wall = pd.date_range("2018-01-01", periods=8760, freq="h")
    inst = wall.tz_localize("America/Sao_Paulo",
                            ambiguous=np.ones(8760, dtype=bool),
                            nonexistent="shift_forward")
    expected = np.asarray((inst - t0) / pd.Timedelta(hours=1), dtype=float)
    assert np.array_equal(arr, expected)
    feb17_23 = (31 + 16) * 24 + 23
    assert arr[feb17_23] == expected[feb17_23] and arr[feb17_23 + 1] == arr[feb17_23] + 2
    nov4 = (pd.Timestamp("2018-11-04") - pd.Timestamp("2018-01-01")).days * 24
    assert arr[nov4] == arr[nov4 - 1] + 1 == arr[nov4 + 1]


def test_zonal_align_without_dst_unchanged():
    from esfex.models.demand_zonal_fetchers import _align_year_8760

    idx = pd.date_range("2021-01-01", periods=8760, freq="h")
    series = pd.Series(np.arange(8760.0), index=idx)
    assert np.array_equal(_align_year_8760(series, 2021, "America/Sao_Paulo"),
                          np.arange(8760.0))
    # A short gap is filled forward, as before.
    arr = _align_year_8760(series.drop(idx[100:103]), 2021)
    assert arr[100] == arr[102] == 99.0
