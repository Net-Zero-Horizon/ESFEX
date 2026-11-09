"""Units at the end of their life_time: dispatch, master hand-off and reports.

The dispatch retired a unit at the end of its life_time only when it had a
degradation rate, so an undegraded unit ran on at full capacity after the
master retired it. Retiring it there must not undo the master's decisions:
in a development run the dispatch receives the master's capacity (life
extension included) with no degradation and initial_age 0, and the units the
master keeps must never be retired again. The retirement reports read a 0/1
"within original lifetime" flag, so a unit the master life-extends was
reported retired.
"""

import sys

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator  # noqa: E402
from test_zones import _make_battery, _make_system_config  # noqa: E402

from esfex.config.schema import MasterProblemConfig, PenaltiesConfig, TemporalConfig  # noqa: E402
from esfex.runner import Orchestrator  # noqa: E402


def _bess(**kw):
    return _make_battery("BESS", 1).model_copy(update=dict(
        capacity=[40.0], MaxChargePower=[10.0], MaxDischargePower=[10.0],
        rated_power=[10.0], degradation_rate=[0.0], **kw))


def _orch(tmp_path, gens=None, bats=None):
    sysc = _make_system_config(num_nodes=1, coords=[(21.0, -80.0)],
                               generators=gens or {}, batteries=bats or {})
    return Orchestrator(_make_esfex_config(sysc), output_dir=tmp_path)


def test_master_generators_get_no_end_of_life_in_the_dispatch(tmp_path):
    """The master's capacity (life-extended here) reaches the dispatch with no
    degradation and no end of life: the dispatch's own end-of-life check would
    retire it life_time years after the base year (initial_age is reset)."""
    old = _make_generator("Old", "Non-renewable", "Gas", [100.0], life_time=[2],
                          initial_age=[5], degradation_rate=[0.02])
    orch = _orch(tmp_path, gens={"old": old})
    units = orch._build_config_from_cumulative(
        orch._gather_units(), {"gen": {1: np.array([80.0])}}, year_offset=3)
    assert units["old"]["rated_power"] == [80.0]
    assert units["old"]["degradation_rate"] == [0.0]
    assert units["old"]["initial_age"] == [0]
    assert units["old"]["life_time"] == [999]


def test_battery_past_its_lifetime_gets_what_the_master_keeps(tmp_path):
    """Batteries keep the dispatch's own ageing within their lifetime; past it
    the dispatch would retire them, so they get the master's life extension."""
    orch = _orch(tmp_path, bats={"bess": _bess(life_time=[10], initial_age=[8])})
    caps = {"bat_power": {1: np.array([6.0])}, "bat": {1: np.array([24.0])}}
    # Year offset 1: age 9 < 10, the dispatch ages it itself
    units = orch._build_config_from_cumulative(orch._gather_units(), caps, year_offset=1)
    assert units["bess"]["MaxDischargePower"] == [10.0]
    assert units["bess"]["life_time"] == [10]
    # Year offset 2: age 10, past its lifetime; the master keeps 6 of 10 MW
    units = orch._build_config_from_cumulative(orch._gather_units(), caps, year_offset=2)
    bess = units["bess"]
    assert bess["MaxDischargePower"] == [6.0]
    assert bess["MaxChargePower"] == [pytest.approx(6.0)]
    assert bess["capacity"] == [24.0]
    assert bess["life_time"] == [999] and bess["initial_age"] == [0]
    assert bess["degradation_rate"] == [0.0]
    # Retired by the master: nothing left
    caps0 = {"bat_power": {1: np.array([0.0])}, "bat": {1: np.array([0.0])}}
    units = orch._build_config_from_cumulative(orch._gather_units(), caps0, year_offset=2)
    assert units["bess"]["MaxDischargePower"] == [0.0]
    assert units["bess"]["capacity"] == [0.0]


@pytest.mark.julia
def test_life_extended_unit_runs_and_is_not_reported_retired(tmp_path):
    """Development run, 3 years. The only unit is past its lifetime from the
    first year (initial_age 5, life_time 2, no degradation) and nothing can be
    built: the master extends its life. Every year's dispatch must run it (the
    dispatch used to see initial_age 0 and would retire it from year 3), and
    the reports must not list it as retired (they read a 0/1 lifetime flag)."""
    demand = tmp_path / "demand.csv"
    np.savetxt(demand, np.full(3 * 8760, 50.0))
    old = _make_generator("Old", "Non-renewable", "Gas", [100.0], life_time=[2],
                          initial_age=[5], degradation_rate=[0.0], invest_cost=[1000.0],
                          fuel_cost=[50.0])
    sysc = _make_system_config(num_nodes=1, coords=[(21.0, -80.0)],
                               generators={"old": old})
    sysc = sysc.model_copy(update={"demand_path": str(demand),
                                   "penalties": PenaltiesConfig(loss_of_load=10000.0)})
    cfg = _make_esfex_config(
        sysc, simulation_mode="development",
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720,
                                overlap_hours=0),
        master_problem=MasterProblemConfig())
    results = Orchestrator(cfg, output_dir=tmp_path / "out").run(years=3)
    assert len(results) == 3
    for yr in results:
        assert yr.retirements.get("gen_0", 0.0) == pytest.approx(0.0, abs=0.01)
        # The old unit serves the 50 MW load every step (MW per step)
        assert float(np.max(yr.load_shed_array)) == pytest.approx(0.0, abs=1e-3)
        assert float(np.mean(yr.gen_output[0].sum(axis=0))) == pytest.approx(50.0, rel=1e-3)
    # The results file records the MW the master keeps in service (bug
    # sweep S12): the readers charged fixed O&M on the configured rating
    import h5py
    (h5_path,) = (tmp_path / "out").rglob("*.h5")
    with h5py.File(h5_path, "r") as f:
        for yr in results:
            gen = f[f"detailed_results/year_{yr.year}_threshold_0/generation"]
            (name,) = list(gen)
            assert 50.0 - 1e-3 <= float(np.sum(gen[name].attrs["rated_power"])) <= 100.0
