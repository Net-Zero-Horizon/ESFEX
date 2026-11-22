"""Which rows a year reads: calendar, relabelling and horizon hours.

* ``--start-year`` only relabels; the Feb 29 rows dropped are those of the
  data's own calendar (``date_start``).
* ``solve_dispatch(year=Y)`` dispatches data year Y with the run's outage
  base year, instead of the first data year labelled Y.
* The master reads the dispatch's profiles (runner cache) or, from disk, the
  same calendar; reservoir inflows are not clipped to [0, 1].
* Myopic and TSAM masters read each year's block of a multi-year profile.
* The initial RE share counts a renewable without a profile at full
  capacity, as the dispatch does.
"""

from datetime import datetime
from unittest.mock import patch

import numpy as np
import pytest

from esfex.bridge.adapters import MasterProblemAdapter
from esfex.io.demand import load_availability_profile
from esfex.runner import Orchestrator, YearResults
from esfex.utils.temporal import HOURS_STD_YEAR as H

from tests.test_demand_time_handling import _orch
from tests.test_runner import _make_esfex_config, _make_generator, _make_system_config


# ── --start-year relabels only ─────────────────────────────────────────────

def test_relabelled_leap_data_still_drops_its_feb29(tmp_path):
    f = tmp_path / "demand.csv"
    n = 8784 + H                                                # 2028 (leap), 2029
    np.savetxt(f, np.column_stack([np.arange(float(n))] * 2), delimiter=",")
    orch = _orch(tmp_path, f, date_start="01/01/2028 00:00")
    orch._start_date = orch._resolve_start_date(2025)           # label: 2025
    demand, total, _, _, time_index = orch._load_demand(years=2, start_year=2025)
    assert total == 2 * H
    assert demand[1416, 0] == 1440.0          # 29 Feb 2028 dropped
    assert demand[H, 0] == 8784.0             # second year starts on 1 Jan 2029
    assert time_index[0] == datetime(2025, 1, 1)


def test_relabelling_into_a_leap_year_keeps_the_data_calendar(tmp_path):
    f = tmp_path / "demand.csv"
    n = H + 8784                                                # 2027, 2028 (leap)
    np.savetxt(f, np.column_stack([np.arange(float(n))] * 2), delimiter=",")
    orch = _orch(tmp_path, f, date_start="01/01/2027 00:00")
    orch._start_date = orch._resolve_start_date(2024)           # label: 2024
    demand, total, *_ = orch._load_demand(years=2, start_year=2024)
    # The 2024 calendar used to drop 24 rows of 2027 instead of 29 Feb 2028
    assert total == 2 * H
    np.testing.assert_array_equal(demand[:H, 0], np.arange(float(H)))
    assert demand[H + 1416, 0] == H + 1440.0


# ── solve_dispatch(year=Y) dispatches data year Y ──────────────────────────

def test_solve_dispatch_runs_data_year(tmp_path):
    from esfex.visualization.scripting_api import ESFEXAPI

    cfg = _make_esfex_config(simulation_mode="economic_dispatch")   # date_start 2025
    calls = []

    def fake_run(self, **kw):
        calls.append(kw)
        return [YearResults(year=2027, objective=0.0, solve_time=0.0, feasible=True)]

    with patch.object(Orchestrator, "run", fake_run):
        ESFEXAPI.solve_dispatch(cfg, year=2027, output_dir=tmp_path)
        ESFEXAPI.solve_dispatch(cfg, output_dir=tmp_path)
    assert calls[0] == {"years": 3, "dispatch_years": [2027]}
    assert calls[1] == {"years": 1, "dispatch_years": [2025]}
    with pytest.raises(ValueError, match="before the data's first year"):
        ESFEXAPI.solve_dispatch(cfg, year=2024, output_dir=tmp_path)


def test_dispatch_years_keep_their_data_block_and_base_year(tmp_path):
    f = tmp_path / "demand.csv"
    np.savetxt(f, np.repeat(np.arange(3.0), H)[:, None] * np.ones((1, 2)), delimiter=",")
    orch = _orch(tmp_path, f, date_start="01/01/2025 00:00")
    orch.config = orch.config.model_copy(update={"simulation_mode": "economic_dispatch"})
    seen = []

    def fake_dispatch(**kw):
        seen.append((kw["year"], kw["year_idx"], float(kw["demand"][0, 0]),
                     orch.state.base_year))
        return YearResults(year=kw["year"], objective=0.0, solve_time=0.0,
                           feasible=True, demand=kw["demand"])

    with patch.object(orch, "_run_operational_dispatch", side_effect=fake_dispatch):
        res = orch.run(years=3, dispatch_years=[2027])
    # Only 2027 runs, on the third data year, with hour 0 = 1 Jan 2025
    assert seen == [(2027, 2, 2.0, 2025)]
    assert [r.year for r in res] == [2027]
    with pytest.raises(ValueError, match="outside the horizon"):
        orch.run(years=3, dispatch_years=[2030])


def test_skipped_years_still_apply_their_master_builds(tmp_path):
    """Transmission/converter builds are per-year increments: with
    dispatch_years, the builds of the years before the dispatched one must
    reach it."""
    f = tmp_path / "demand.csv"
    np.savetxt(f, np.ones((3 * H, 2)), delimiter=",")
    orch = _orch(tmp_path, f, date_start="01/01/2025 00:00")
    orch.config = orch.config.model_copy(update={"simulation_mode": "development"})
    inv = {2025: {"transfer_investment_0_1": 10.0},
           2026: {"converter_investment_acdc_0": 5.0},
           2027: {"transfer_investment_0_1": 1.0}}
    applied, seen = [], []

    def fake_dispatch(**kw):
        seen.append((kw["year"], list(applied)))
        return YearResults(year=kw["year"], objective=0.0, solve_time=0.0,
                           feasible=True, demand=kw["demand"])

    with patch.object(orch, "_solve_master_problem", return_value=(inv, {}, {}, {})), \
         patch.object(orch, "_apply_transfer_investments",
                      side_effect=lambda d: applied.append(("t", dict(d)))), \
         patch.object(orch, "_apply_converter_investments",
                      side_effect=lambda d: applied.append(("c", dict(d)))), \
         patch.object(orch, "_run_operational_dispatch", side_effect=fake_dispatch):
        orch.run(years=3, dispatch_years=[2027])
    assert len(seen) == 1 and seen[0][0] == 2027
    got = seen[0][1]
    assert ("t", inv[2025]) in got and ("c", inv[2026]) in got
    assert ("t", inv[2027]) in got


# ── master profiles: dispatch cache, data calendar, no inflow clipping ─────

def _master(tmp_path, **kw):
    sysc = _make_system_config(num_nodes=2)
    return MasterProblemAdapter(
        config=_make_esfex_config(sysc), years=[2025], base_year=2025,
        demand=np.ones((H, 2)), temporal_resolution_hours=1,
        config_path=str(tmp_path / "c.yaml"), **kw)


def test_master_profile_prefers_the_dispatch_cache(tmp_path):
    cached = np.full((H, 2), 0.25)
    m = _master(tmp_path, availability_cache={"g": cached})
    np.testing.assert_array_equal(
        m._master_profile(m._availability_cache, "g", "missing.csv", 2,
                          load_availability_profile), cached)


def test_master_profile_from_disk_drops_feb29_of_multi_year_leap_data(tmp_path):
    # 2027 (8760 h) then 2028 (8784 h): 29 Feb 2028 is 8760 + 1416 .. + 1440
    rows = np.arange(8760 + 8784, dtype=float) / 1e5
    np.savetxt(tmp_path / "cf.csv", rows)
    m = _master(tmp_path, date_start="01/01/2027 00:00")
    prof = m._master_profile({}, "g", "cf.csv", 2, load_availability_profile)
    assert prof.shape == (2 * H, 2)
    assert prof[H + 1416, 0] == pytest.approx(rows[H + 1440])   # 1 Mar 2028 00:00


def test_master_profile_starts_at_the_planned_year(tmp_path):
    prof = np.repeat(np.arange(3.0), H)[:, None] * np.ones((1, 2))
    m = _master(tmp_path, availability_cache={"g": prof}, profile_start_hour=2 * H)
    out = m._master_profile(m._availability_cache, "g", None, 2, load_availability_profile)
    assert out[0, 0] == 2.0 and out[H - 1, 0] == 2.0 and out[H, 0] == 0.0


def test_inflow_is_not_clipped(tmp_path):
    from esfex.io.demand import load_inflow_profile

    np.savetxt(tmp_path / "inflow.csv", np.full(H, 35.0))
    inflow = load_inflow_profile(tmp_path / "inflow.csv", num_nodes=2)
    assert inflow.shape == (H, 2) and float(inflow.max()) == 35.0
    with pytest.raises(FileNotFoundError):
        load_inflow_profile(tmp_path / "none.csv")
    # The master's inflow from disk, too
    m = _master(tmp_path)
    prof = m._master_profile({}, "h", "inflow.csv", 2, load_inflow_profile, required=False)
    assert float(prof.max()) == 35.0
    assert m._master_profile({}, "h", "none.csv", 2, load_inflow_profile,
                             required=False) is None


def test_runner_preloads_unclipped_inflow(tmp_path):
    np.savetxt(tmp_path / "inflow.csv", np.full(H, 35.0))
    hydro = _make_generator("Hydro", "Renewable", "Water", [10.0, 0.0],
                            reservoir_capacity=[100.0, 0.0],
                            reservoir_inflow_file="inflow.csv")
    orch = Orchestrator(_make_esfex_config(_make_system_config(generators={"hydro": hydro})),
                        output_dir=tmp_path / "out", config_path=tmp_path / "c.yaml")
    cache = orch._preload_inflow_profiles(2)
    (inflow,) = cache.values()
    assert float(inflow.max()) == 35.0


# ── year blocks of multi-year profiles (myopic / TSAM masters) ─────────────

def test_profile_year_block_follows_horizon_hours():
    from esfex.runner import _profile_year_block

    prof = np.repeat(np.arange(3.0), 4)[:, None]       # 3 "years" of 4 steps
    assert _profile_year_block(prof, 1, 4)[:, 0].tolist() == [1.0] * 4
    # Beyond the data: wraps around like the dispatch (year 4 -> block 1)
    assert _profile_year_block(prof, 4, 4)[:, 0].tolist() == [1.0] * 4
    # A one-year profile repeats
    assert _profile_year_block(prof[:4], 2, 4)[:, 0].tolist() == [0.0] * 4


# ── initial RE share ───────────────────────────────────────────────────────

def test_renewable_without_profile_counts_at_full_capacity(tmp_path):
    gens = {"bio": _make_generator("Bio", "Renewable", "Biomass", [30.0, 0.0]),
            "pv": _make_generator("PV", "Renewable", "Sun", [100.0, 0.0],
                                  availability_file="pv.csv")}
    orch = Orchestrator(_make_esfex_config(_make_system_config(generators=gens)),
                        output_dir=tmp_path)
    key = next(k for k in orch.primary_system.generators if k.endswith("pv"))
    orch._availability_cache = {key: np.full((H, 2), 0.2)}
    demand = np.zeros((H, 2))
    demand[:, 0] = 100.0
    # 30 MW biomass (no profile: 1.0, as dispatched) + 100 MW × 0.2 PV
    assert orch._calculate_initial_re_penetration(demand) == pytest.approx(0.5)
    units = {k: {"type": "Renewable", "rated_power": list(g.rated_power)}
             for k, g in orch.primary_system.generators.items()}
    assert orch._estimate_achievable_re(units, demand) == pytest.approx(0.5)
