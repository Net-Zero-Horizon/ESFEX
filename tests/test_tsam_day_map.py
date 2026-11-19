"""TSAM periods carry the period of every day of the year (K44).

The master's seasonal storage runs an inter-day state through the 365 days
of the year on this map (Kotzur et al. 2018), while each period's costs and
energy balance count × its weight. Both must agree: each period's count in
the map is its weight, the critical days included. The old result only had
a chronological order of the periods, an empty k-means cluster got a weight
of 1 that no day backed, and critical days made the weights fractions.
"""

import numpy as np
import pytest

from esfex.models.tsam import (
    TSAMResult,
    _own_medoids,
    compute_tsam_periods,
    with_critical_days,
)

DAY = 24
N_DAYS = 365


def _year(seed: int = 3) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    rng = np.random.default_rng(seed)
    t = np.arange(N_DAYS * DAY)
    season = 1.0 + 0.3 * np.sin(2 * np.pi * t / (N_DAYS * DAY))
    daily = 1.0 + 0.2 * np.sin(2 * np.pi * (t % DAY) / DAY)
    demand = (100.0 * season * daily + rng.normal(0.0, 3.0, t.size))[:, None]
    pv = np.clip(np.sin(np.pi * ((t % DAY) - 6) / 12), 0.0, None) * (1.2 - 0.4 * season)
    return np.hstack([demand, 0.8 * demand]), {"pv": np.column_stack([pv, pv])}


@pytest.mark.parametrize("method", ["kmedoids", "kmeans"])
def test_day_map_counts_are_the_weights(method):
    demand, avail = _year()
    res = compute_tsam_periods(demand, num_periods=8, method=method, availability=avail)
    assert len(res.day_period) == N_DAYS
    assert set(res.day_period) <= set(range(res.num_periods))
    counts = np.bincount(res.day_period, minlength=res.num_periods)
    assert counts.tolist() == res.period_weights
    assert sum(res.period_weights) == N_DAYS
    # Each representative day stands for itself
    for k, start in enumerate(res.period_start_hours):
        assert res.day_period[start // DAY] == k
    # Every period stands for at least one day
    assert min(res.period_weights) >= 1


def test_single_period_maps_every_day():
    demand, _ = _year()
    res = compute_tsam_periods(demand, num_periods=1)
    assert res.day_period == [0] * N_DAYS and res.period_weights == [float(N_DAYS)]


def test_empty_or_shared_cluster_takes_its_representative():
    feats = np.array([[0.0], [0.1], [0.2], [5.0], [5.1], [9.0]])
    # k-means left cluster 2 empty, its representative (day 5) labelled 1
    labels, medoids = _own_medoids(np.array([0, 0, 0, 1, 1, 1]), [1, 4, 5], feats)
    assert labels.tolist() == [0, 0, 0, 1, 1, 2]
    assert medoids == [1, 4, 5]
    # Two clusters with the same representative: the second takes the
    # nearest day no other cluster represents
    labels, medoids = _own_medoids(np.array([0, 0, 0, 1, 1, 1]), [1, 1, 4], feats)
    assert medoids == [1, 0, 4]
    assert labels.tolist() == [1, 0, 0, 1, 2, 1]


def test_critical_days_leave_their_cluster():
    base = TSAMResult(period_start_hours=[0, 100 * DAY], period_weights=[200.0, 165.0],
                      day_period=[0] * 200 + [1] * 165, num_periods=2)
    res = with_critical_days(base, [150 * DAY, 300 * DAY], DAY)
    assert res.period_start_hours == [0, 100 * DAY, 150 * DAY, 300 * DAY]
    assert res.period_weights == [199.0, 164.0, 1.0, 1.0]
    assert res.day_period[150] == 2 and res.day_period[300] == 3
    assert res.critical_periods == [2, 3] and res.num_periods == 4
    assert base.day_period[150] == 0                     # the input is left as it is
    # 6-h resolution: 4 steps per day
    six = TSAMResult(period_start_hours=[0], period_weights=[float(N_DAYS)],
                     day_period=[0] * N_DAYS, num_periods=1)
    assert with_critical_days(six, [40 * 4], 4).day_period[40] == 1
    with pytest.raises(ValueError, match="outside the year"):
        with_critical_days(six, [N_DAYS * 4], 4)
