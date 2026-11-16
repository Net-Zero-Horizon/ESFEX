"""The default representative days match the year's demand and renewable energy.

The days were the peak-demand day plus the highest-peak day of each segment
of the year, chosen from demand only. Where sunny and windy days are also
high-demand days (Cuba: PV ×1.14, wind ×1.45, demand ×1.06 on the 5 days),
the master, which weighs every day as 365 / n days, saw a sunnier, windier
and more loaded year than the real one: it reported 95 % RE where the hourly
dispatch of the same plan gave 65 %. The days now keep the peak day and
match the annual mean of the demand and of every renewable profile
(existing renewables and candidate technologies), and the bias is logged.
"""

import logging
import sys

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import (  # noqa: E402
    _create_orchestrator, _make_esfex_config, _make_generator, _make_system_config)

from esfex.config.schema import MasterProblemConfig, TechnologyConfig  # noqa: E402
from esfex.utils.temporal import HOURS_STD_YEAR  # noqa: E402

DAY = 24
N_DAYS = 365
# Unit keys of the merged (one-system) run
SOLAR, PV_NEW, WIND_NEW, GAS = (f"test_sys__{k}" for k in ("solar", "pv_new", "wind_new", "gas"))


def _biased_year():
    """Demand (8760 × 2 nodes), PV and wind (8760) of one year whose high
    days are both the high-demand and the sunny/windy days: day d's level
    a_d spreads over [0, 1] in every part of the year."""
    a = ((np.arange(N_DAYS) * 97) % N_DAYS) / (N_DAYS - 1)
    hour = np.arange(DAY)
    shape = 1.0 + 0.2 * np.sin(2 * np.pi * (hour - 8) / DAY)
    demand_total = np.concatenate([100.0 * (1.0 + a_d) * shape for a_d in a])
    demand = np.column_stack([0.6 * demand_total, 0.4 * demand_total])
    sun = np.clip(np.sin(np.pi * (hour - 6) / 12), 0.0, None)
    pv = np.concatenate([(0.5 + a_d) * 0.4 * sun for a_d in a])
    wind = np.repeat(0.2 + 0.6 * a, DAY)
    return demand, pv, wind


def _ratio(series, offsets):
    steps = np.concatenate([np.arange(o, o + DAY) for o in offsets])
    return series[steps].mean() / series.mean()


@pytest.mark.julia
def test_days_match_demand_and_renewable_energy():
    from esfex.bridge.adapters import master_rep_day_offsets, master_rep_day_ratios
    from esfex.bridge.converters import py_to_julia_matrix
    from esfex.bridge.julia_setup import get_esfex_module

    demand, pv, wind = _biased_year()
    total = demand.sum(axis=1)

    # The old choice (peak day + highest-peak day per segment) overrates
    # all three
    ESFEX = get_esfex_module()
    old = [int(h) - 1 for h in ESFEX.select_representative_days(
        py_to_julia_matrix(demand), 1, 5, 5, DAY, HOURS_STD_YEAR)]
    assert _ratio(pv, old) > 1.3 and _ratio(wind, old) > 1.4
    assert _ratio(total, old) > 1.2

    offsets = master_rep_day_offsets(demand, 1, 5, 5, profiles=[pv, wind])
    assert len(offsets) == 5
    # The peak-demand day stays (adequacy), first
    peak_day = int(np.argmax(total.reshape(N_DAYS, DAY).max(axis=1)))
    assert offsets[0] == peak_day * DAY
    days = sorted(o // DAY for o in offsets)
    assert min(np.diff(days)) >= 5
    for series in (total, pv, wind):
        assert _ratio(series, offsets) == pytest.approx(1.0, abs=0.03)

    # The logged ratios are those, demand first
    ratios = master_rep_day_ratios(demand, 1, offsets, profiles=[pv, wind])
    assert ratios == pytest.approx([_ratio(s, offsets) for s in (total, pv, wind)])
    # Weighted periods (TSAM): the weights apply
    w = master_rep_day_ratios(demand, 1, [0, DAY], profiles=[wind], weights=[3.0, 1.0])
    assert w[1] == pytest.approx((3 * wind[0] + wind[DAY]) / 4 / wind.mean())


@pytest.mark.julia
def test_days_match_multi_year_demand_and_profiles():
    """Multi-year data: the horizon mean of each step is matched."""
    from esfex.bridge.adapters import master_rep_day_offsets

    demand, pv, wind = _biased_year()
    demand2 = np.vstack([demand, 1.1 * demand])
    pv2 = np.concatenate([pv, pv])
    offsets = master_rep_day_offsets(demand2, 2, 5, 5, profiles=[pv2, wind])
    total = demand.sum(axis=1)
    for series in (total, pv, wind):
        assert _ratio(series, offsets) == pytest.approx(1.0, abs=0.03)


def _orch(tmp_path, **mp):
    gas = _make_generator("Gas", "Non-renewable", "Gas", [100.0, 50.0])
    solar = _make_generator("Solar", "Renewable", "Sun", [50.0, 0.0])
    wind_new = TechnologyConfig(
        name="Wind", type="Renewable", fuel="Wind", invest_cost=[1.0, 1.0],
        invest_max_power=[100.0, 100.0], eff_at_rated=[1.0, 1.0],
        degradation_rate=[0.0, 0.0], lifetime=25)
    pv_new = TechnologyConfig(
        name="PV", type="Renewable", fuel="Sun", invest_cost=[1.0, 1.0],
        invest_max_power=[0.0, 100.0], eff_at_rated=[1.0, 1.0],
        degradation_rate=[0.0, 0.0], lifetime=25)
    sysc = _make_system_config(generators={"gas": gas, "solar": solar}).model_copy(
        update={"technologies": {"wind_new": wind_new, "pv_new": pv_new}})
    cfg = _make_esfex_config(sysc, master_problem=MasterProblemConfig(**mp))
    return _create_orchestrator(cfg, tmp_path=tmp_path)


def test_re_profiles_of_generators_and_technologies(tmp_path):
    """One entry per distinct profile, technologies included; each series is
    the mean over the nodes where the units sharing it have capacity."""
    _, pv, wind = _biased_year()
    orch = _orch(tmp_path)
    pv2 = np.column_stack([pv, 0.5 * pv])
    orch._availability_cache = {
        SOLAR: pv2, PV_NEW: pv2.copy(), WIND_NEW: np.column_stack([wind, wind]),
        GAS: np.ones((HOURS_STD_YEAR, 2))}
    profiles = {label: series for label, _, series in orch._master_re_profiles(1)}
    assert set(profiles) == {f"{SOLAR}, {PV_NEW}", WIND_NEW}
    # solar at node 0, pv_new may be built at node 1: both nodes
    assert profiles[f"{SOLAR}, {PV_NEW}"] == pytest.approx(0.75 * pv)
    assert profiles[WIND_NEW] == pytest.approx(wind)
    # At the master's resolution
    (_, avail6, series6), _ = orch._master_re_profiles(6)
    assert avail6.shape == (HOURS_STD_YEAR // 6, 2) and len(series6) == HOURS_STD_YEAR // 6


@pytest.mark.julia
def test_runner_days_match_the_renewable_profiles(tmp_path, caplog):
    demand, pv, wind = _biased_year()
    orch = _orch(tmp_path, representative_days=5, min_day_separation=5)
    orch._availability_cache = {SOLAR: np.column_stack([pv, pv]),
                                WIND_NEW: np.column_stack([wind, wind])}
    with caplog.at_level(logging.INFO, logger="esfex.runner"):
        offsets = orch._master_rep_day_offsets(demand, 1, 1)
    for series in (demand.sum(axis=1), pv, wind):
        assert _ratio(series, offsets) == pytest.approx(1.0, abs=0.03)
    msgs = [r.getMessage() for r in caplog.records
            if "Representative days mean / annual mean" in r.getMessage()]
    assert len(msgs) == 1
    assert "demand 1.0" in msgs[0] or "demand 0.9" in msgs[0]
    assert f"{SOLAR} " in msgs[0] and f"{WIND_NEW} " in msgs[0]
    assert not [r for r in caplog.records if r.levelno == logging.WARNING
                and "misrepresent" in r.getMessage()]


@pytest.mark.julia
def test_one_day_warns_about_the_bias(tmp_path, caplog):
    """One day (the peak day) cannot match the year: the log says so."""
    demand, pv, wind = _biased_year()
    orch = _orch(tmp_path, representative_days=1, min_day_separation=1)
    orch._availability_cache = {WIND_NEW: np.column_stack([wind, wind])}
    with caplog.at_level(logging.INFO, logger="esfex.runner"):
        orch._master_rep_day_offsets(demand, 1, 1)
    warn = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("misrepresent" in m and WIND_NEW in m for m in warn)


@pytest.mark.julia
def test_tsam_clusters_candidate_technology_profiles(tmp_path, caplog, monkeypatch):
    import esfex.models.tsam as tsam

    demand, pv, wind = _biased_year()
    orch = _orch(tmp_path, use_tsam=True, tsam_num_periods=6)
    orch._availability_cache = {SOLAR: np.column_stack([pv, pv]),
                                WIND_NEW: np.column_stack([wind, wind])}
    seen = {}
    real = tsam.compute_tsam_periods

    def spy(**kwargs):
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(tsam, "compute_tsam_periods", spy)
    with caplog.at_level(logging.INFO, logger="esfex.runner"):
        orch._master_tsam_periods(demand, 1, 1)
    assert set(seen["availability"]) == {SOLAR, WIND_NEW}
    assert any("TSAM periods (before rescaling the renewable profiles) mean / annual mean"
               in r.getMessage() for r in caplog.records)
