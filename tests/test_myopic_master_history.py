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
