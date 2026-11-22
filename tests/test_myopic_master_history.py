"""Each myopic year's master sees what earlier years built.

The one-year masters were handed the merged master system unchanged: the
aged units and the capacity built in earlier years were applied to a copy of
the primary system that the adapter never read (``system_config`` wins), and
were keyed by technology while being applied to generators, so they never
matched. Every year planned from the base-year fleet and rebuilt what the
year before had built, and the dispatch got no myopic investments at all.
"""

import sys
from unittest.mock import patch

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator  # noqa: E402
from test_zones import (  # noqa: E402
    _make_battery_technology, _make_system_config, _make_technology)

from esfex.config.schema import (  # noqa: E402
    MasterProblemConfig,
    MetaNetworkConfig,
    PenaltiesConfig,
    TemporalConfig,
)
from esfex.runner import Orchestrator  # noqa: E402
from esfex.utils.temporal import HOURS_STD_YEAR  # noqa: E402


def _gas_tech(invest_max=500.0):
    tech = _make_technology("Gas CC", "Gas", 1, tech_type="Non-renewable",
                            invest_cost=200000.0, invest_max_power=invest_max)
    return tech.model_copy(update={"fuel_cost": [40.0], "min_output": [0.3]})


def _system(name, tmp_path=None, load=60.0):
    """One node: 50 MW of dear diesel, a gas candidate and a battery candidate."""
    diesel = _make_generator("Diesel", "Non-renewable", "Diesel", [50.0],
                             initial_age=[3], degradation_rate=[0.0], fuel_cost=[300.0])
    s = _make_system_config(num_nodes=1, coords=[(21.0, -80.0)],
                            generators={"diesel": diesel},
                            technologies={"gas_new": _gas_tech()},
                            battery_technologies={"li": _make_battery_technology("Li-ion", 1)})
    update = {"name": name, "penalties": PenaltiesConfig(loss_of_load=10000.0),
              "target_re_penetration": 0.0, "min_annual_increment": 0.0}
    if tmp_path is not None:
        path = tmp_path / f"{name}.csv"
        np.savetxt(path, np.full(8760, load))
        update["demand_path"] = str(path)
    return s.model_copy(update=update)


def _two_systems(tmp_path):
    a, b = _system("A"), _system("B")
    cfg = _make_esfex_config(a, systems={"A": a, "B": b},
                             meta_network=MetaNetworkConfig(systems=["A", "B"]),
                             master_problem=MasterProblemConfig(planning_mode="myopic"))
    orch = Orchestrator(cfg, output_dir=tmp_path)
    orch._availability_cache = {}
    return orch


def test_year_system_holds_aged_units_builds_and_reduced_limits(tmp_path):
    orch = _two_systems(tmp_path)
    base = orch.master_system
    assert list(base.technologies) == ["A__gas_new", "B__gas_new"]
    built = {"tech": {("B__gas_new", 0): np.array([0.0, 30.0]),
                      ("A__gas_new", 1): np.array([12.0, 0.0])},
             "bat_tech_power": {("B__li", 0): np.array([0.0, 10.0])},
             "bat_tech_capacity": {("B__li", 0): np.array([0.0, 40.0])}}

    year_sys, built_gens, built_bats = orch._prepare_system_for_myopic_year(2, built)

    # Existing units are two years older; the shared master system is untouched
    assert year_sys.generators["A__diesel"].initial_age[0] == 3 + 2
    assert base.generators["A__diesel"].initial_age[0] == 3
    assert "B__gas_new__built_y1" not in base.generators

    # Built capacity is an existing unit of its technology, at its node and age
    assert built_gens == [("B__gas_new__built_y1", "B__gas_new"),
                          ("A__gas_new__built_y2", "A__gas_new")]
    unit = year_sys.generators["B__gas_new__built_y1"]
    assert unit.rated_power == [0.0, 30.0]
    assert unit.initial_age == [2, 2]
    assert unit.life_time == [25, 25]
    assert unit.technology == "B__gas_new"
    assert unit.type == "Non-renewable" and unit.fuel == "Gas"
    assert unit.fuel_cost[1] == 40.0
    assert unit.min_power[1] == 0.3               # a fraction, not MW
    assert unit.degradation_rate == base.technologies["B__gas_new"].degradation_rate
    assert unit.invest_cost == base.technologies["B__gas_new"].invest_cost
    assert year_sys.generators["A__gas_new__built_y2"].initial_age == [1, 1]

    # What is left to build, per node (each system's candidate is 0 elsewhere)
    assert year_sys.technologies["B__gas_new"].invest_max_power == [0.0, 470.0]
    assert year_sys.technologies["A__gas_new"].invest_max_power == [488.0, 0.0]
    assert base.technologies["B__gas_new"].invest_max_power == [0.0, 500.0]

    assert built_bats == [("B__li__built_y1", "B__li")]
    bat = year_sys.batteries["B__li__built_y1"]
    assert bat.MaxDischargePower == [0.0, 10.0] and bat.capacity == [0.0, 40.0]
    assert bat.initial_age == [2, 2] and bat.life_time == [15, 15]
    # Degraded at the technology's 1 %/yr from its build year (round 44: it
    # was held at 0, since perfect foresight did not degrade builds either);
    # the master and the dispatch of year 3 hold it at 0.99^2
    assert base.battery_technologies["B__li"].degradation_rate == [0.0, 0.01]
    assert bat.degradation_rate == [0.0, 0.01]
    bt = year_sys.battery_technologies["B__li"]
    assert bt.invest_max_power == [0.0, 190.0]
    assert bt.invest_max_capacity == [0.0, 760.0]


def test_retired_builds_leave_the_fleet_but_still_count_against_the_limit(tmp_path):
    orch = _two_systems(tmp_path)
    built = {"tech": {("A__gas_new", 0): np.array([20.0, 0.0])},
             "bat_tech_power": {}, "bat_tech_capacity": {}}
    year_sys, built_gens, _ = orch._prepare_system_for_myopic_year(25, built)   # lifetime 25
    assert built_gens == []
    assert year_sys.technologies["A__gas_new"].invest_max_power == [480.0, 0.0]


def test_investments_accumulate_per_technology_node_and_build_year(tmp_path):
    orch = _two_systems(tmp_path)
    built = {"tech": {}, "bat_tech_power": {}, "bat_tech_capacity": {}}
    orch._accumulate_myopic_investments(
        {"tech_investment_power_1_1": 30.0, "bat_tech_investment_power_1_1": 10.0,
         "bat_tech_investment_capacity_1_1": 40.0, "transfer_investment_0_1": 5.0}, 0, built)
    orch._accumulate_myopic_investments({"tech_investment_power_1_1": 5.0}, 1, built)
    assert {k: list(v) for k, v in built["tech"].items()} == {
        ("B__gas_new", 0): [0.0, 30.0], ("B__gas_new", 1): [0.0, 5.0]}
    assert list(built["bat_tech_power"][("B__li", 0)]) == [0.0, 10.0]
    assert list(built["bat_tech_capacity"][("B__li", 0)]) == [0.0, 40.0]


def test_next_year_master_gets_the_built_fleet_and_the_dispatch_its_capacity(tmp_path):
    """Two myopic years with a stand-in master: year 1 builds 30 MW of gas."""
    orch = _two_systems(tmp_path)
    captured = []

    class FakeMaster:
        def __init__(self, **kwargs):
            captured.append(kwargs)

        def build_model(self, **_):
            pass

        def solve(self):
            return 1

        def get_solution_values(self):
            year = captured[-1]["years"][0]
            first = len(captured) == 1
            gens = {g + 1: np.zeros(2) for g in range(len(captured[-1]["system_config"].generators))}
            if not first:
                gens[3] = np.array([0.0, 29.85])          # the built unit, degraded
            return {
                "solution": {year: {"tech_investment": (
                    {"tech_investment_power_1_1": 30.0} if first else {})}},
                "cumulative_gen_capacity": {1: gens},
                "cumulative_tech_capacity": {1: {1: np.zeros(2), 2: np.array(
                    [0.0, 30.0 if first else 0.0])}},
                "re_penetration_by_year": [0.0],
            }

    demand = np.full((2 * HOURS_STD_YEAR, 2), 10.0)
    with patch("esfex.bridge.adapters.MasterProblemAdapter", FakeMaster):
        inv, _, caps, _ = orch._solve_master_problem_myopic(
            years_range=[2025, 2026], demand=demand[:HOURS_STD_YEAR],
            hours=HOURS_STD_YEAR, num_nodes=2, total_demand=demand)

    year2 = captured[1]["system_config"]
    assert year2.generators["B__gas_new__built_y1"].rated_power == [0.0, 30.0]
    assert year2.generators["B__gas_new__built_y1"].initial_age == [1, 1]
    assert year2.technologies["B__gas_new"].invest_max_power == [0.0, 470.0]
    assert year2.generators["A__diesel"].initial_age[0] == 4
    assert inv[2025] == {"tech_investment_power_1_1": 30.0}
    # The dispatch gets each year's capacity per technology, original units apart
    assert list(caps[2025]["tech"][2]) == [0.0, 30.0]
    assert list(caps[2026]["tech"][2]) == pytest.approx([0.0, 29.85])
    assert set(caps[2026]["gen"]) == {1, 2}


@pytest.mark.julia
def test_myopic_year_two_does_not_rebuild_year_one(tmp_path):
    sysc = _system("Island", tmp_path)
    cfg = _make_esfex_config(
        sysc, simulation_mode="development",
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720, overlap_hours=0),
        master_problem=MasterProblemConfig(planning_mode="myopic"))
    orch = Orchestrator(cfg, output_dir=tmp_path / "out")
    orch.run(years=2, master_only=True)
    sol = orch.master_solution
    y1, y2 = sorted(sol["investments"])
    gas_y1 = sol["investments"][y1].get("tech_investment_power_0_0", 0.0)
    gas_y2 = sol["investments"][y2].get("tech_investment_power_0_0", 0.0)
    assert gas_y1 > 5.0                     # 60 MW of load, 50 MW of diesel
    assert gas_y2 < 0.05 * gas_y1           # already built: not bought again
    # The dispatch sees the year-1 build in both years (degraded 0.5 %/year)
    assert sol["cumulative_capacities"][y1]["tech"][1][0] == pytest.approx(gas_y1, rel=1e-6)
    assert sol["cumulative_capacities"][y2]["tech"][1][0] == pytest.approx(
        gas_y1 * 0.995 + gas_y2, rel=1e-4)


def test_virtual_generator_min_power_is_a_fraction(tmp_path):
    """The dispatch's virtual generator (same field mapping as the built
    units above) carried min_output × rated MW in min_power, a fraction of
    rated everywhere else: with unit commitment its minimum output became
    min_output × rated²."""
    orch = Orchestrator(_make_esfex_config(_system("Island")), output_dir=tmp_path)
    units = orch._build_config_from_cumulative({}, {"tech": {1: np.array([60.0])}})
    vgen = units["inv_gas_new"]
    assert vgen["rated_power"] == [60.0]
    assert vgen["min_power"] == [0.3]
    assert vgen["fuel_cost"] == [40.0]


# Each myopic year's objective charges only the annuities of what that year
# builds; the reported yearly cost adds those of earlier years' builds still
# in service, so it is comparable with a perfect-foresight year.

def test_earlier_build_annuities_follow_the_build_lifetimes():
    from esfex.runner import earlier_build_annuities
    streams = [(0, 25.0, 100.0), (0, 2.0, 10.0), (1, 0.0, 1.0)]
    assert earlier_build_annuities(streams, 0) == 0.0     # nothing built before
    assert earlier_build_annuities(streams, 1) == 110.0   # year-1 builds
    assert earlier_build_annuities(streams, 2) == 101.0   # the 2-year build retired
    assert earlier_build_annuities(streams, 40) == 1.0    # no life: 40 years
    assert earlier_build_annuities(streams, 41) == 0.0


def test_myopic_year_cost_adds_earlier_annuities(tmp_path, caplog):
    import logging
    orch = Orchestrator(_make_esfex_config(_system("Island")), output_dir=tmp_path)
    streams: list = []
    with caplog.at_level(logging.INFO, logger="esfex.runner"):
        orch._report_myopic_year_cost(2025, 0, {"objective": 500.0,
                                                "annuity_streams": [(1, 25.0, 80.0)]},
                                      streams)
        orch._report_myopic_year_cost(2026, 1, {"objective": 300.0,
                                                "annuity_streams": []}, streams)
    assert orch.myopic_year_costs[2025] == {"objective": 500.0,
                                            "earlier_annuities": 0.0, "total": 500.0}
    assert orch.myopic_year_costs[2026] == {"objective": 300.0,
                                            "earlier_annuities": 80.0, "total": 380.0}
    assert streams == [(0, 25.0, 80.0)]
    assert any("Myopic year 2026" in r.getMessage() and "$380" in r.getMessage()
               for r in caplog.records)


@pytest.mark.julia
def test_myopic_reported_cost_carries_year_one_annuity(tmp_path):
    sysc = _system("Island", tmp_path)
    cfg = _make_esfex_config(
        sysc, simulation_mode="development",
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720, overlap_hours=0),
        master_problem=MasterProblemConfig(planning_mode="myopic"))
    orch = Orchestrator(cfg, output_dir=tmp_path / "out")
    orch.run(years=2, master_only=True)
    y1, y2 = sorted(orch.master_solution["investments"])
    gas_y1 = orch.master_solution["investments"][y1]["tech_investment_power_0_0"]
    rate, life = sysc.discount_rate, 25
    crf = rate * (1 + rate) ** life / ((1 + rate) ** life - 1)
    costs = orch.myopic_year_costs
    assert costs[y1]["earlier_annuities"] == 0.0
    assert costs[y2]["earlier_annuities"] == pytest.approx(gas_y1 * 200000.0 * crf, rel=1e-6)
    assert costs[y2]["total"] == pytest.approx(
        costs[y2]["objective"] + costs[y2]["earlier_annuities"])


def test_myopic_small_battery_build_keeps_power_and_energy_together(tmp_path):
    """A battery the master builds below 0.1 MW but above 0.1 MWh reaches the
    next year whole. A 0.1 cut-off dropped the power and kept the energy,
    leaving a battery with energy and no power whose minimum-cycling
    constraint made every dispatch window of the next year infeasible."""
    orch = _two_systems(tmp_path)
    captured = []

    class FakeMaster:
        def __init__(self, **kwargs):
            captured.append(kwargs)

        def build_model(self, **_):
            pass

        def solve(self):
            return 1

        def get_solution_values(self):
            year = captured[-1]["years"][0]
            first = len(captured) == 1
            inv = {"bat_tech_power_investment": {"bat_tech_investment_power_1_1": 0.0917},
                   "bat_tech_capacity_investment": {"bat_tech_investment_capacity_1_1": 0.1833}}
            return {"solution": {year: inv if first else {}},
                    "re_penetration_by_year": [0.0]}

    demand = np.full((2 * HOURS_STD_YEAR, 2), 10.0)
    with patch("esfex.bridge.adapters.MasterProblemAdapter", FakeMaster):
        inv, _, _, _ = orch._solve_master_problem_myopic(
            years_range=[2025, 2026], demand=demand[:HOURS_STD_YEAR],
            hours=HOURS_STD_YEAR, num_nodes=2, total_demand=demand)

    assert inv[2025] == {"bat_tech_investment_power_1_1": 0.0917,
                         "bat_tech_investment_capacity_1_1": 0.1833}
    bat = captured[1]["system_config"].batteries["B__li__built_y1"]
    assert bat.MaxChargePower == [0.0, 0.0917] and bat.MaxDischargePower == [0.0, 0.0917]
    assert bat.capacity == [0.0, 0.1833]


def test_myopic_carry_drops_solver_noise(tmp_path):
    """Round 36: a barrier solve without crossover reports capacity it did not
    build (2e-6 MW at a node) and leaves a limit it reached at e.g. 3e-7 MW.
    Carried into the next myopic year they became existing units and limits
    of that size (rows like ``gen <= 2e-9``, bounds of 3e-7) that made the
    next master numerically fragile. At most MIN_BUILD (1 kW) is noise."""
    from esfex.runner import MIN_BUILD, master_year_builds, remaining_limit

    assert MIN_BUILD == 1e-3
    assert master_year_builds({"tech_investment": {
        "tech_investment_power_1_1": 2e-6, "tech_investment_power_0_0": 5e-4,
        "tech_investment_power_1_0": 0.02}}) == {"tech_investment_power_1_0": 0.02}
    assert remaining_limit(500.0, 500.0 - 3e-7) == 0.0
    assert remaining_limit(500.0, 499.0) == 1.0
    assert remaining_limit(10.0, 12.0) == 0.0

    orch = _two_systems(tmp_path)
    built = {"tech": {("B__gas_new", 0): np.array([0.0, 500.0 - 3e-7])},
             "bat_tech_power": {("B__li", 0): np.array([0.0, 200.0 - 2e-7])},
             "bat_tech_capacity": {("B__li", 0): np.array([0.0, 800.0 - 4e-7])}}
    year_sys, _, _ = orch._prepare_system_for_myopic_year(1, built)
    assert year_sys.technologies["B__gas_new"].invest_max_power == [0.0, 0.0]
    bt = year_sys.battery_technologies["B__li"]
    assert bt.invest_max_power == [0.0, 0.0] and bt.invest_max_capacity == [0.0, 0.0]


# ── Round 44: the batteries the master builds degrade ─────────────────

def test_rated_battery_technology_power_counts_live_builds():
    from esfex.runner import rated_bat_tech_power

    builds = {1: {1: [10.0]}, 3: {1: np.array([5.0]), 2: [7.0]}}
    assert list(rated_bat_tech_power(builds, 1, [2, 15])[1]) == [10.0]
    assert list(rated_bat_tech_power(builds, 2, [2, 15])[1]) == [10.0]
    # Year 1's build is 2 years old in year 3: past its 2-year lifetime
    at3 = rated_bat_tech_power(builds, 3, [2, 15])
    assert list(at3[1]) == [5.0] and list(at3[2]) == [7.0]
    assert list(rated_bat_tech_power(builds, 3, [2])) == [1]


def _fake_battery_master(captured, built_mw=10.0):
    """A stand-in master whose first year builds *built_mw* MW / 4 h of the
    battery technology at node B, which the next year reports 1 % smaller."""

    class FakeMaster:
        def __init__(self, **kwargs):
            captured.append(kwargs)

        def build_model(self, **_):
            pass

        def solve(self):
            return 1

        def get_solution_values(self):
            year = captured[-1]["years"][0]
            first = len(captured) == 1
            n_bat = len(captured[-1]["system_config"].batteries)
            built = np.array([0.0, built_mw if first else 0.0])
            out = {
                "solution": {year: {
                    "bat_tech_power_investment": (
                        {"bat_tech_investment_power_1_1": built_mw} if first else {}),
                    "bat_tech_capacity_investment": (
                        {"bat_tech_investment_capacity_1_1": 4 * built_mw} if first else {})}},
                "bat_tech_power_investment": {1: {1: np.zeros(2), 2: built}},
                "cumulative_bat_tech_power": {1: {1: np.zeros(2), 2: built}},
                "cumulative_bat_tech_capacity": {1: {1: np.zeros(2), 2: 4 * built}},
                "re_penetration_by_year": [0.0],
            }
            if not first:
                # The year-1 battery, an existing unit now, a year older
                out["cumulative_bat_power"] = {1: {n_bat: np.array([0.0, 0.99 * built_mw])}}
                out["cumulative_bat_capacity"] = {
                    1: {n_bat: np.array([0.0, 0.99 * 4 * built_mw])}}
            return out

    return FakeMaster


def test_myopic_built_battery_degrades_and_keeps_rated_fixed_om(tmp_path):
    """A battery built in year 1 is a year old in year 2: an existing unit
    at the technology's 1 %/yr there (it was held at 0 %/yr), and the
    dispatch gets the master's degraded power with the rated power as the
    base of its fixed O&M."""
    orch = _two_systems(tmp_path)
    captured = []
    demand = np.full((2 * HOURS_STD_YEAR, 2), 10.0)
    with patch("esfex.bridge.adapters.MasterProblemAdapter", _fake_battery_master(captured)):
        _, _, caps, _ = orch._solve_master_problem_myopic(
            years_range=[2025, 2026], demand=demand[:HOURS_STD_YEAR],
            hours=HOURS_STD_YEAR, num_nodes=2, total_demand=demand)

    bat = captured[1]["system_config"].batteries["B__li__built_y1"]
    assert bat.degradation_rate == [0.0, 0.01] and bat.initial_age == [1, 1]
    assert list(caps[2025]["bat_tech_power"][2]) == [0.0, 10.0]
    assert list(caps[2025]["bat_tech_power_rated"][2]) == [0.0, 10.0]
    assert list(caps[2026]["bat_tech_power"][2]) == pytest.approx([0.0, 9.9])
    assert list(caps[2026]["bat_tech_capacity"][2]) == pytest.approx([0.0, 39.6])
    assert list(caps[2026]["bat_tech_power_rated"][2]) == [0.0, 10.0]


def test_perfect_foresight_dispatch_gets_degraded_battery_and_rated_om_base(tmp_path):
    """The dispatch's battery of a perfect-foresight build: the master's
    degraded MW and MWh, no further degradation, and its fixed O&M on the
    rated power, as the master charges it."""
    sysc = _system("Island", tmp_path)
    orch = Orchestrator(_make_esfex_config(sysc), output_dir=tmp_path / "out")
    orch._availability_cache = {}

    class FakeMaster:
        def __init__(self, **kwargs):
            pass

        def build_model(self, **_):
            pass

        def solve(self):
            return 1

        def get_solution_values(self):
            return {
                "solution": {2025: {"bat_tech_power_investment": {
                    "bat_tech_investment_power_0_0": 10.0}}, 2026: {}},
                "bat_tech_power_investment": {1: {1: np.array([10.0])},
                                              2: {1: np.array([0.0])}},
                "cumulative_bat_tech_power": {1: {1: np.array([10.0])},
                                              2: {1: np.array([9.9])}},
                "cumulative_bat_tech_capacity": {1: {1: np.array([40.0])},
                                                 2: {1: np.array([39.6])}},
                "re_penetration_by_year": [0.0, 0.0],
            }

    demand = np.full((2 * HOURS_STD_YEAR, 1), 10.0)
    with patch("esfex.runner.MasterProblemAdapter", FakeMaster):
        _, _, caps, _ = orch._solve_master_problem(
            years_range=[2025, 2026], demand=demand[:HOURS_STD_YEAR],
            hours=HOURS_STD_YEAR, num_nodes=1, total_demand=demand)
    assert list(caps[2026]["bat_tech_power"][1]) == pytest.approx([9.9])
    assert list(caps[2026]["bat_tech_power_rated"][1]) == [10.0]

    li = orch.primary_system.battery_technologies["li"]
    orch.primary_system.battery_technologies["li"] = li.model_copy(
        update={"fixed_cost": [1000.0]})
    bat = orch._build_config_from_cumulative({}, caps[2026])["inv_li"]
    assert bat["MaxDischargePower"] == pytest.approx([9.9])
    assert bat["MaxChargePower"] == pytest.approx([9.9])
    assert bat["capacity"] == pytest.approx([39.6])
    assert bat["degradation_rate"] == [0.0]
    assert bat["fixed_cost"][0] * bat["MaxDischargePower"][0] == pytest.approx(1000.0 * 10.0)
    # The results record the rated power as the battery's, the base of the
    # financial analysis' fixed O&M (round 44, M44: it got the degraded MW)
    assert bat["_rated_discharge_power"] == pytest.approx([10.0])
    orch._num_nodes = 1
    orch._rebuild_unit_names({"inv_li": bat})
    assert orch._bat_in_service_mw[-1] == pytest.approx([10.0])
    # Without the rated power the fixed cost is the technology's
    caps0 = {k: v for k, v in caps[2026].items() if k != "bat_tech_power_rated"}
    bat0 = orch._build_config_from_cumulative({}, caps0)["inv_li"]
    assert bat0["fixed_cost"] == [1000.0]
    orch._rebuild_unit_names({"inv_li": bat0})
    assert orch._bat_in_service_mw[-1] == pytest.approx([9.9])
