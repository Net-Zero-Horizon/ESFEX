"""Clock alignment and features of the density model's training pipeline
(scripts/demand_model): demand and temperature of the same instant on the
series' local civil time, calendar of the real date."""

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "demand_model"


def _load(name):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


clocks = _load("clocks")
audit = _load("clock_audit")


def _h(stamp):
    return pd.Timestamp(stamp).tz_convert("UTC").value / 3.6e12


# ── row instants ──────────────────────────────────────────────────────────


def test_row_instants_fixed_and_civil_clocks():
    assert clocks.row_instants(2019, 8760, "UTC")[0] == _h("2019-01-01 00:00Z")
    # UTC-5: row 0 is 05:00 UTC
    assert clocks.row_instants(2019, 8760, "Etc/GMT+5")[0] == _h("2019-01-01 05:00Z")
    civil = clocks.row_instants(2019, 8760, "America/New_York")
    july = 181 * 24 + 12                       # 1 July 12:00 wall time
    assert civil[july] == _h("2019-07-01 16:00Z")      # EDT, UTC-4
    assert civil[12] == _h("2019-01-01 17:00Z")         # EST, UTC-5


def test_row_instants_leap_year_rows():
    kept = clocks.row_instants(2020, 8760, "UTC", feb29_kept=True)
    dropped = clocks.row_instants(2020, 8760, "UTC", feb29_kept=False)
    assert kept[1416] == _h("2020-02-29 00:00Z")
    assert dropped[1416] == _h("2020-03-01 00:00Z")
    assert kept[-1] == _h("2020-12-30 23:00Z")
    assert dropped[-1] == _h("2020-12-31 23:00Z")


# ── alignment on local civil time ─────────────────────────────────────────


def _utc_hour_series(years):
    """A 'series' whose value is its own UTC instant (hours since 1970)."""
    return {y: clocks.row_instants(y, 8760, "UTC") for y in years}


def test_on_local_time_reads_the_instant_of_each_local_hour():
    years = [2018, 2019, 2020]
    vals = _utc_hour_series(years)
    out = clocks.on_local_time(vals, {y: "UTC" for y in years}, {y: True for y in years},
                               "America/Havana", targets=[2019])[2019]
    # 1 Jan 00:00 Havana (CST, UTC-5) is 05:00 UTC
    assert out[0] == _h("2019-01-01 05:00Z")
    # 1 July 00:00 Havana (CDT, UTC-4) is 04:00 UTC
    assert out[181 * 24] == _h("2019-07-01 04:00Z")
    # last local hour reads the next recorded year: nothing wraps
    assert out[-1] == _h("2020-01-01 04:00Z")


def test_on_local_time_missing_neighbour_year_is_nan_not_wrapped():
    vals = _utc_hour_series([2019])
    out = clocks.on_local_time(vals, {2019: "UTC"}, {2019: True}, "Asia/Shanghai")[2019]
    # 1 Jan 00:00-07:00 Beijing time are 31 Dec 2018 in UTC: absent
    assert np.isnan(out[:8]).all()
    assert out[8] == _h("2019-01-01 00:00Z")


def test_demand_and_temperature_paired_at_the_same_instant():
    """Demand recorded on UTC+8 peaks at 19 h local; ERA5 (UTC) peaks at
    14 h local = 06 UTC. After alignment both sit at those local hours."""
    year = 2019
    h = np.arange(8760)
    dem = np.where(h % 24 == 19, 2.0, 1.0)                     # rows on UTC+8
    temp = np.where(h % 24 == 6, 30.0, 20.0)                   # rows on UTC
    zone = "Asia/Shanghai"
    d = clocks.on_local_time({year: dem, year + 1: dem}, {year: "Etc/GMT-8", year + 1: "Etc/GMT-8"},
                             {year: True, year + 1: True}, zone, targets=[year])[year]
    t = clocks.on_local_time({year: temp, year + 1: temp}, {year: "UTC", year + 1: "UTC"},
                             {year: True, year + 1: True}, zone, targets=[year])[year]
    day = slice(24 * 100, 24 * 101)
    assert np.argmax(d[day]) == 19
    assert np.argmax(t[day]) == 14
    # v1 paired row h with UTC hour h: the temperature peak sat at row 6
    assert np.argmax(temp[day]) == 6


def test_half_hour_zone_interpolates_between_hours():
    vals = {2019: np.arange(8760, dtype=float), 2020: np.arange(8760, 17520, dtype=float)}
    out = clocks.on_local_time(vals, {2019: "UTC", 2020: "UTC"}, {2019: True, 2020: True},
                               "Asia/Kolkata", targets=[2020])[2020]
    # 1 Jan 2020 00:00 IST = 31 Dec 2019 18:30 UTC: halfway between rows 8754 and 8755
    assert out[0] == pytest.approx(8754.5)


def test_half_hour_recorded_clock_onto_its_own_zone_is_identity():
    vals = {2019: np.arange(8760, dtype=float)}
    out = clocks.on_local_time(vals, {2019: "Asia/Kolkata"}, {2019: True}, "Asia/Kolkata")[2019]
    np.testing.assert_array_equal(out, vals[2019])


def test_sample_at_keeps_first_of_repeated_instants():
    out = clocks.sample_at(np.array([0.0, 1.0, 1.0, 2.0]), np.array([10.0, 11.0, 99.0, 12.0]),
                           np.array([1.0, 2.0, 3.0]))
    assert out[0] == 11.0 and out[1] == 12.0 and np.isnan(out[2])


# ── calendar features ─────────────────────────────────────────────────────


def test_calendar_features_follow_the_real_date_in_a_leap_year():
    cal = clocks.calendar_features(2024, "ESP")
    row = 1416                                   # 1 March 2024 (29 Feb skipped)
    assert cal["month_sin"][row] == pytest.approx(np.sin(2 * np.pi * 2 / 12))
    assert cal["dow_sin"][row] == pytest.approx(np.sin(2 * np.pi * 4 / 7))   # Friday
    assert cal["hour_sin"][row + 13] == pytest.approx(np.sin(2 * np.pi * 13 / 24))
    v1 = clocks.v1_calendar_features(2024, "ESP")
    # v1 counted weekdays from 1 Jan through the skipped day: Thursday
    assert v1["dow"][row] == 3
    # Christmas is a holiday on its real row
    xmas = (pd.Timestamp("2024-12-25") - pd.Timestamp("2024-01-01")).days - 1
    assert cal["is_holiday"][xmas * 24] == 1.0


# ── audit decisions ───────────────────────────────────────────────────────


def test_decide_year_clock_rules():
    core = "Asia/Taipei"
    # OPSD equality fixes the clock
    assert audit.decide_year_clock("UTC", 2015, -1.0, 1.0,
                                   {"opsd_lag": -1, "opsd_equal": 1.0, "opsd_corr": 1.0})[:2] == ("Etc/GMT+1", "anchored")
    # matches OPSD at no lag: left out
    assert audit.decide_year_clock("UTC", 2015, 0.0, 1.0,
                                   {"opsd_lag": 0, "opsd_equal": 0.0, "opsd_corr": 0.3})[1] == "excluded"
    # a clean whole-hour delay is a clock change (Taipei +8 -> UTC)
    assert audit.decide_year_clock(core, 2024, -8.05, 0.99, None)[:2] == ("UTC", "shifted")
    # small delays keep the core clock; messy ones are left out
    assert audit.decide_year_clock(core, 2024, 0.3, 0.8, None)[:2] == (core, "core")
    assert audit.decide_year_clock(core, 2024, -2.5, 0.99, None)[1] == "excluded"
    assert audit.decide_year_clock(core, 2024, -2.0, 0.85, None)[1] == "excluded"
    # a half-hour off delay onto the local standard offset (interval labels)
    assert audit.decide_year_clock("UTC", 2017, -5.55, 0.99, None,
                                   local_zone="America/Panama")[:2] == ("Etc/GMT+5", "shifted")
    # from a half-hour core zone back to UTC
    assert audit.decide_year_clock("Asia/Kolkata", 2025, -5.6, 0.99, None)[:2] == ("UTC", "shifted")
    # partial OPSD equality does not anchor; a poor match excludes
    assert audit.decide_year_clock("UTC", 2016, -2.25, 0.99,
                                   {"opsd_lag": -3, "opsd_equal": 0.74, "opsd_corr": 0.93})[1] == "excluded"


def _weekly_series(year, feb29_kept):
    """Demand with a weekend dip, rows from 1 Jan (29 Feb kept or dropped)."""
    stamps = pd.date_range(f"{year}-01-01", periods=8784, freq="h")
    if not feb29_kept:
        stamps = stamps[~((stamps.month == 2) & (stamps.day == 29))]
    stamps = stamps[:8760]
    return np.where(stamps.dayofweek >= 5, 0.7, 1.0) * (1 + 0.3 * np.sin(2 * np.pi * stamps.hour / 24))


def test_leap_test_tells_kept_from_dropped():
    kept, dropped = audit.leap_test(_weekly_series(2020, True), 2020)
    assert kept > dropped + 0.5
    kept, dropped = audit.leap_test(_weekly_series(2020, False), 2020)
    assert dropped > kept + 0.5


def test_dst_test_tells_fixed_from_civil_clock():
    zone, year = "Europe/Madrid", 2019
    civil_wall = pd.date_range(f"{year}-01-01", periods=8760, freq="h")
    # a load driven by the wall clock (morning ramp at 07 h civil)
    def load(local_hour):
        return 1 + 0.5 * np.clip(local_hour - 7, 0, 1) - 0.5 * np.clip(local_hour - 22, 0, 1)
    civil = load(np.asarray(civil_wall.hour, float))
    utc_inst = civil_wall.tz_localize("UTC")
    fixed = load(np.asarray(utc_inst.tz_convert(zone).hour, float))   # rows on UTC
    assert abs(audit.dst_test(civil, year, zone, True)) < 0.3
    assert audit.dst_test(fixed, year, zone, True) == pytest.approx(1.0, abs=0.3)
