"""TSAM periods carry the year's renewable energy.

The TSAM periods (k-medoids, weighted by cluster size) misrepresented the
year: on Cuba with 12 periods, PV +8 % and wind −13 % (logged by the runner's
ratio check), so the master planned on a sunnier, calmer year than the
dispatch then ran. After the periods (and the critical days) are fixed, each
renewable availability profile the master reads is now rescaled on the
periods so their weighted energy equals each year's (shape kept, capped at
full availability, the shortfall spread over the uncapped steps, as the
``tsam`` library does); the critical days stay as they are. The log no
longer advises TSAM when TSAM is in use, and reports what remains biased.
"""

import logging
import sys
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_master_critical_days import DARK, DAY, N_DAYS, SOLAR, WIND_NEW, _orch, _year  # noqa: E402

from esfex.models.tsam import (  # noqa: E402
    TSAMResult,
    rescale_periods_to_year,
    rescale_profile_periods,
)
from esfex.utils.temporal import HOURS_STD_YEAR  # noqa: E402

STARTS = [10 * DAY, 100 * DAY, 200 * DAY, 300 * DAY]


def _weighted_ratio(profile, starts, weights, year_rows=slice(0, HOURS_STD_YEAR),
                    reference=None):
    """Weighted mean of *profile* on the periods over the mean of
    *reference* (the original profile; *profile* itself when None) in the
    year."""
    year = profile[year_rows]
    ref = (profile if reference is None else reference)[year_rows]
    days = np.array([year[s:s + DAY].mean(axis=0) for s in starts])
    return np.asarray(weights, dtype=float) @ days / np.sum(weights) / ref.mean(axis=0)


def _sunny_periods_year():
    """PV (8760 × 2) whose four period days are the sunniest of the year,
    and wind (8760) at full output on them but calm otherwise."""
    hour = np.arange(DAY)
    sun = np.clip(np.sin(np.pi * (hour - 6) / 12), 0.0, None)
    level = np.full(N_DAYS, 0.5)
    for s in STARTS:
        level[s // DAY] = 0.9
    pv = np.concatenate([lv * sun for lv in level])
    wind = np.full(HOURS_STD_YEAR, 0.3)
    for s in STARTS:
        wind[s:s + DAY] = np.linspace(0.2, 1.0, DAY)
    return np.column_stack([pv, 0.8 * pv]), wind


def test_biased_periods_match_the_year_and_keep_their_shape():
    pv, _ = _sunny_periods_year()
    weights = [100.0, 90.0, 95.0, 80.0]
    before = _weighted_ratio(pv, STARTS, weights)
    assert before == pytest.approx([1.78, 1.78], abs=0.01)    # biased

    new, ratios = rescale_periods_to_year(pv, STARTS, weights, DAY)
    assert ratios == pytest.approx([1.0, 1.0], abs=1e-9)
    assert _weighted_ratio(new, STARTS, weights, reference=pv) == pytest.approx([1.0, 1.0], abs=1e-6)
    # Only the period rows change, by one factor per column (the shape is
    # kept: night stays 0)
    rows = np.concatenate([np.arange(s, s + DAY) for s in STARTS])
    others = np.setdiff1d(np.arange(HOURS_STD_YEAR), rows)
    assert np.array_equal(new[others], pv[others])
    lit = pv[rows, 0] > 0
    factors = new[rows][lit] / pv[rows][lit]
    assert factors[:, 0] == pytest.approx(np.full(lit.sum(), 1 / before[0]))
    assert np.all(new[rows][~lit] == 0.0)
    assert new.max() <= 1.0


def test_the_cap_spreads_the_energy_over_the_uncapped_steps():
    _, wind = _sunny_periods_year()
    weights = [1.0, 1.0, 1.0, 1.0]
    # Down: the periods are windier than the year
    assert _weighted_ratio(wind, STARTS, weights) > 1.5
    new, ratio = rescale_profile_periods(wind, [(0, STARTS, weights)], DAY, HOURS_STD_YEAR)
    assert ratio[0] == pytest.approx([1.0]) and new.max() <= 1.0

    # Up: the periods are calmer than the year and their high hours reach
    # the cap; the energy they cannot take goes to the others
    calm = 1.0 - wind
    calm[np.concatenate([np.arange(s, s + 4) for s in STARTS])] = 0.95
    before = _weighted_ratio(calm, STARTS, weights)
    assert before < 0.7
    new, ratio = rescale_profile_periods(calm, [(0, STARTS, weights)], DAY, HOURS_STD_YEAR)
    assert ratio[0, 0] == pytest.approx(1.0, abs=1e-9)
    assert _weighted_ratio(new, STARTS, weights, reference=calm) == pytest.approx(1.0, abs=1e-6)
    assert new.max() == pytest.approx(1.0)
    capped = new >= 1.0 - 1e-12
    assert capped.any() and not (calm >= 1.0 - 1e-12).any()
    # The uncapped period steps share one factor
    period = np.zeros(HOURS_STD_YEAR, dtype=bool)
    for s in STARTS:
        period[s:s + DAY] = True
    free = period & ~capped & (calm > 0)
    factors = new[free] / calm[free]
    assert factors == pytest.approx(np.full(free.sum(), factors[0]))

    # Out of reach: one lit hour per period day, at the cap, still falls short
    dark = np.full(HOURS_STD_YEAR, 0.9)
    dark[period] = 0.0
    lit = [s + 12 for s in STARTS]
    dark[lit] = 0.01
    new, ratio = rescale_profile_periods(dark, [(0, STARTS, weights)], DAY, HOURS_STD_YEAR)
    assert np.all(new[lit] == 1.0) and np.count_nonzero(new[period]) == len(lit)
    assert ratio[0, 0] == pytest.approx(
        _weighted_ratio(new, STARTS, weights, reference=dark)[()])
    assert ratio[0, 0] == pytest.approx(1 / 24 / dark.mean())


def test_the_critical_days_are_kept_and_the_others_carry_the_correction():
    pv, _ = _sunny_periods_year()
    starts = STARTS + [DARK * DAY]
    weights = [90.0, 90.0, 90.0, 94.0, 1.0]
    dark = pv.copy()
    dark[DARK * DAY:(DARK + 1) * DAY] *= 0.1
    new, ratios = rescale_periods_to_year(dark, starts, weights, DAY, critical=[4])
    assert np.array_equal(new[DARK * DAY:(DARK + 1) * DAY], dark[DARK * DAY:(DARK + 1) * DAY])
    assert ratios == pytest.approx([1.0, 1.0], abs=1e-9)
    # The year's energy over all the periods, the critical day included
    assert _weighted_ratio(new, starts, weights, reference=dark) == pytest.approx([1.0, 1.0], abs=1e-6)


def test_each_year_of_a_multi_year_profile_is_matched_on_its_own():
    pv, _ = _sunny_periods_year()
    two = np.vstack([pv, 0.5 * pv + 0.1 * (pv > 0)])
    weights = [91.0, 91.0, 91.0, 92.0]
    years = [(0, STARTS, weights), (HOURS_STD_YEAR, STARTS, weights)]
    new, ratios = rescale_profile_periods(two, years, DAY, HOURS_STD_YEAR)
    assert ratios == pytest.approx(np.ones((2, 2)), abs=1e-9)
    for y in range(2):
        rows = slice(y * HOURS_STD_YEAR, (y + 1) * HOURS_STD_YEAR)
        assert _weighted_ratio(new, STARTS, weights, rows, two) == pytest.approx([1.0, 1.0], abs=1e-6)

    # A one-year profile read by three years is rescaled once, from the original
    once, _ = rescale_profile_periods(pv, years[:1], DAY, HOURS_STD_YEAR)
    thrice, ratios = rescale_profile_periods(
        pv, [(y * HOURS_STD_YEAR, STARTS, weights) for y in range(3)], DAY, HOURS_STD_YEAR)
    assert np.array_equal(once, thrice) and ratios == pytest.approx(np.ones((3, 2)))
    # Not a whole number of years: left as it is
    short = pv[:1000]
    same, ratios = rescale_profile_periods(short, years[:1], DAY, HOURS_STD_YEAR)
    assert np.array_equal(same, short) and np.isnan(ratios).all()


def test_the_adapter_rescales_each_year_as_the_master_reads_it():
    """1-based horizon steps: perfect foresight places year k at k × 8760
    (a one-year profile wraps), myopic at the start of its rotated profile."""
    from esfex.bridge.adapters import MasterProblemAdapter

    pv, _ = _sunny_periods_year()
    weights = [91.0, 91.0, 91.0, 92.0]
    two = np.vstack([pv, 0.5 * pv])
    foresight = SimpleNamespace(
        use_tsam=True, temporal_resolution_hours=1, tsam_critical_periods=[],
        tsam_period_start_hours=[[y * HOURS_STD_YEAR + s + 1 for s in STARTS] for y in range(2)],
        tsam_period_weights=[weights, weights])
    new = MasterProblemAdapter._tsam_energy_matched(foresight, "pv", two)
    for y in range(2):
        rows = slice(y * HOURS_STD_YEAR, (y + 1) * HOURS_STD_YEAR)
        assert _weighted_ratio(new, STARTS, weights, rows, two) == pytest.approx([1.0, 1.0], abs=1e-6)
    assert np.array_equal(MasterProblemAdapter._tsam_energy_matched(foresight, "pv", pv),
                          new[:HOURS_STD_YEAR])
    myopic = SimpleNamespace(
        use_tsam=True, temporal_resolution_hours=1, tsam_critical_periods=[],
        tsam_period_start_hours=[[s + 1 for s in STARTS]], tsam_period_weights=[weights])
    assert np.array_equal(MasterProblemAdapter._tsam_energy_matched(myopic, "pv", two)[
        :HOURS_STD_YEAR], new[:HOURS_STD_YEAR])
    # At 6-hour steps
    agg = pv.reshape(-1, 6, 2).mean(axis=1)
    six = SimpleNamespace(
        use_tsam=True, temporal_resolution_hours=6, tsam_critical_periods=[],
        tsam_period_start_hours=[[s // 6 + 1 for s in STARTS]], tsam_period_weights=[weights])
    new6 = MasterProblemAdapter._tsam_energy_matched(six, "pv", agg)
    days = np.array([new6[s // 6:s // 6 + 4].mean(axis=0) for s in STARTS])
    assert np.dot(weights, days) / sum(weights) / agg.mean(axis=0) == pytest.approx([1.0, 1.0])
    # Without TSAM: as it is
    off = SimpleNamespace(use_tsam=False, tsam_period_start_hours=[])
    assert MasterProblemAdapter._tsam_energy_matched(off, "pv", pv) is pv


@pytest.mark.julia
def test_runner_plan_marks_the_critical_periods_and_logs_the_residual(tmp_path, caplog):
    demand, _, _ = _year()
    orch = _orch(tmp_path, use_tsam=True, tsam_num_periods=6, critical_days=1)
    with caplog.at_level(logging.INFO, logger="esfex.runner"):
        plan = orch._master_day_plan(demand, 1, 1)
    assert plan.tsam.critical_periods == [6]
    assert plan.tsam.period_start_hours[6] == DARK * DAY
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "TSAM periods (before rescaling the renewable profiles) mean / annual mean" in text
    after = [r.getMessage() for r in caplog.records
             if "rescaled to the year's energy, mean / annual mean" in r.getMessage()]
    assert len(after) == 1
    assert f"{SOLAR} 1.000" in after[0] and f"{WIND_NEW} 1.000" in after[0]
    assert "use_tsam" not in text


@pytest.mark.julia
def test_warning_text_per_mode(tmp_path, caplog):
    demand, pv, wind = _year()
    # Representative days: one day misses the year; more days or TSAM
    orch = _orch(tmp_path, representative_days=1, min_day_separation=1, critical_days=0)
    with caplog.at_level(logging.INFO, logger="esfex.runner"):
        orch._master_day_plan(demand, 1, 1)
    warn = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("misrepresent" in m and "TSAM (use_tsam)" in m for m in warn)

    # TSAM: what the rescaling cannot fix (the demand, a profile at the cap)
    caplog.clear()
    orch = _orch(tmp_path, use_tsam=True, tsam_num_periods=6, critical_days=0)
    # Sunny every day but the period's, lit two hours only: at the cap it
    # still has far less than a day's mean
    year = np.where(pv > 0, 0.9, 0.0)
    peak_day = int(np.argmax(demand.sum(axis=1))) // DAY
    starts = [peak_day * DAY]
    year[starts[0]:starts[0] + DAY] = 0.0
    year[starts[0] + 12:starts[0] + 14] = 0.05
    orch._availability_cache = {SOLAR: np.column_stack([year, year]),
                                WIND_NEW: np.column_stack([wind, wind])}
    tsam = TSAMResult(period_start_hours=starts, period_weights=[float(N_DAYS)],
                      day_period=[0] * N_DAYS, num_periods=1)
    with caplog.at_level(logging.INFO, logger="esfex.runner"):
        orch._log_tsam_rescaling(demand, 1, tsam, 1)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert f"kept the TSAM periods short of the year's energy for: {SOLAR}" in text
    warn = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warn) == 1
    assert "demand" in warn[0] and SOLAR in warn[0] and WIND_NEW not in warn[0]
    assert "after rescaling the renewable profiles" in warn[0]
    assert "tsam_num_periods" in warn[0] and "use_tsam" not in warn[0]


@pytest.mark.julia
def test_the_master_reads_the_rescaled_profiles(tmp_path):
    """The perfect-foresight master built from the runner's plan: its
    renewable profiles match each year's energy on the TSAM periods, the
    critical day and the non-renewable profiles as they are."""
    from test_master_rep_days_once import _run_foresight

    from esfex.bridge.adapters import MasterProblemAdapter

    demand, pv, wind = _year()
    two_years = np.vstack([demand, demand])
    orch = _orch(tmp_path, use_tsam=True, tsam_num_periods=6, critical_days=1)
    wind_two = np.concatenate([wind, 0.6 * wind])             # a two-year profile
    orch._availability_cache = {SOLAR: np.column_stack([pv, pv]),
                                WIND_NEW: np.column_stack([wind_two, wind_two]),
                                "test_sys__gas": np.full((HOURS_STD_YEAR, 2), 0.9)}
    orch._availability_cache["test_sys__gas"][100 * DAY:200 * DAY] = 0.5
    captured = _run_foresight(orch, two_years, [2025, 2026])
    assert captured["tsam_critical_periods"] == [6]
    master = MasterProblemAdapter(**captured)
    jl_input = master._create_input()
    # The day map reaches Julia, the critical day in its own period
    jl_map = [[int(k) for k in m] for m in jl_input.tsam_day_period]
    assert jl_map == captured["tsam_day_period"] and len(jl_map) == 2
    assert jl_map[0].count(7) == 1 and jl_map[0][captured["tsam_period_start_hours"][0][6] // DAY] == 7
    names = [str(g.name) for g in jl_input.generators]
    solar = np.asarray(jl_input.generators[names.index("test_sys/Solar")].availability)
    gas = np.asarray(jl_input.generators[names.index("test_sys/Gas")].availability)
    tech = np.asarray(jl_input.technologies[0].availability)

    starts0 = [s - 1 for s in captured["tsam_period_start_hours"][0]]
    weights = captured["tsam_period_weights"][0]
    crit = starts0[6]                    # the critical day, one day of the year
    assert weights[6] == 1.0
    # Before: the periods misrepresent the year
    assert abs(_weighted_ratio(np.column_stack([pv, pv]), starts0, weights)[0] - 1.0) > 1e-3
    assert _weighted_ratio(solar, starts0, weights,
                           reference=np.column_stack([pv, pv])) == pytest.approx([1.0, 1.0], abs=1e-6)
    for y in range(2):
        rows = slice(y * HOURS_STD_YEAR, (y + 1) * HOURS_STD_YEAR)
        assert _weighted_ratio(tech, starts0, weights, rows,
                               np.column_stack([wind_two, wind_two])) == pytest.approx(
            [1.0, 1.0], abs=1e-6)
    crit_rows = slice(crit, crit + DAY)
    assert np.array_equal(solar[crit_rows, 0], pv[crit_rows])
    assert np.array_equal(tech[crit_rows, 0], wind[crit_rows])
    assert np.array_equal(tech[HOURS_STD_YEAR + crit:HOURS_STD_YEAR + crit + DAY, 0],
                          0.6 * wind[crit_rows])
    assert np.array_equal(gas, orch._availability_cache["test_sys__gas"])
