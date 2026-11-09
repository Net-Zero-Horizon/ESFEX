"""Life extension of a unit with invest_cost 0 is priced from its candidate.

The master charges a life-extended MW ``invest_cost x life_extension_cost_factor``
per year; existing units default to ``invest_cost`` 0 in the Studio, which made
extension free.
"""

import logging
import sys

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator  # noqa: E402
from test_zones import (  # noqa: E402
    _make_battery,
    _make_battery_technology,
    _make_system_config,
    _make_technology,
)

from esfex.bridge import adapters  # noqa: E402
from esfex.bridge.adapters import life_extension_invest_costs  # noqa: E402
from esfex.config.schema import MasterProblemConfig, PenaltiesConfig, TemporalConfig  # noqa: E402


def test_generator_takes_the_cost_of_its_same_fuel_and_type_technology():
    old = _make_generator("Old", "Non-renewable", "Diesel", [100.0, 50.0],
                          invest_cost=[0.0, 7.0])
    techs = {
        "gas_new": _make_technology("gas_new", "Gas", 2, tech_type="Non-renewable",
                                    invest_cost=1.0),
        "diesel_re": _make_technology("diesel_re", "Diesel", 2, invest_cost=2.0),
        "diesel_new": _make_technology("diesel_new", "Diesel", 2,
                                       tech_type="Non-renewable", invest_cost=3.0),
    }
    sysc = _make_system_config(num_nodes=2, generators={"old": old}, technologies=techs)
    gen_costs, _ = life_extension_invest_costs(sysc)
    # Same fuel AND type: diesel_new; a node with its own cost keeps it.
    assert gen_costs == {"old": [3.0, 7.0]}


def test_battery_takes_its_named_battery_technology():
    bat = _make_battery("Li", 2).model_copy(
        update={"invest_cost": [0.0, 0.0], "technology": "flow"})
    sysc = _make_system_config(
        num_nodes=2, batteries={"li": bat},
        battery_technologies={"liion": _make_battery_technology("Li-ion", 2),
                              "flow": _make_battery_technology("Flow", 2)})
    sysc.battery_technologies["flow"].invest_cost_power[:] = [9.0, 9.0]
    _, bat_costs = life_extension_invest_costs(sysc)
    assert bat_costs == {"li": [9.0, 9.0]}


def test_no_candidate_stays_free_and_warns_once(caplog):
    adapters._FREE_EXTENSION_WARNED.clear()
    old = _make_generator("Old", "Non-renewable", "Coal", [100.0, 0.0],
                          invest_cost=[0.0, 0.0])
    sysc = _make_system_config(num_nodes=2, generators={"coal": old})
    with caplog.at_level(logging.WARNING, logger="esfex.bridge.adapters"):
        assert life_extension_invest_costs(sysc) == ({}, {})
        life_extension_invest_costs(sysc)   # e.g. the next myopic year
    warned = [r for r in caplog.records if "extending its life is free" in r.message]
    assert len(warned) == 1 and "'coal'" in warned[0].message


def test_cost_set_where_the_unit_is_does_not_warn(caplog):
    """Only the nodes with capacity count: a unit priced at every node it
    occupies (0 elsewhere) warned that extending its life was free when no
    candidate shared its fuel (Cuba: every plant)."""
    adapters._FREE_EXTENSION_WARNED.clear()
    gen = _make_generator("Old", "Non-renewable", "Coal", [100.0, 0.0],
                          invest_cost=[8.0, 0.0])
    bat = _make_battery("Li", 2).model_copy(
        update={"rated_power": [0.0, 20.0], "invest_cost": [0.0, 5.0]})
    sysc = _make_system_config(num_nodes=2, generators={"coal": gen},
                               batteries={"li": bat})
    with caplog.at_level(logging.WARNING, logger="esfex.bridge.adapters"):
        assert life_extension_invest_costs(sysc) == ({}, {})
    assert not [r for r in caplog.records if "is free" in r.message]


def _merged(systems):
    """Merge systems ``{name: SystemConfig}`` as a multi-system run does."""
    from esfex.config.schema import ESFEXConfig, MetaNetworkConfig
    from esfex.runner import Orchestrator

    cfg = ESFEXConfig(meta_network=MetaNetworkConfig(systems=list(systems)),
                      systems={n: s.model_copy(update={"name": n})
                               for n, s in systems.items()})
    master, _, _ = Orchestrator._merge_systems(cfg)
    return master


def test_merged_systems_take_the_cost_of_their_own_system(caplog):
    """In a merged run every key is ``{system}__{key}`` and per-node lists are
    0 at the other systems' nodes: the named match never fired and the first
    system's candidate (0 at B's nodes) left B's units free, with no warning."""
    adapters._FREE_EXTENSION_WARNED.clear()

    def system(gen_cost, tech_cost, bat_cost):
        old = _make_generator("Old", "Non-renewable", "Diesel", [100.0, 50.0],
                              invest_cost=[0.0, 0.0], technology="diesel_new")
        bat = _make_battery("Li", 2).model_copy(
            update={"invest_cost": [0.0, 0.0], "technology": "liion"})
        btech = _make_battery_technology("Li-ion", 2)
        btech.invest_cost_power[:] = [bat_cost, bat_cost]
        return _make_system_config(
            num_nodes=2, generators={"old": old}, batteries={"li": bat},
            technologies={"diesel_new": _make_technology(
                "diesel_new", "Diesel", 2, tech_type="Non-renewable",
                invest_cost=tech_cost)},
            battery_technologies={"liion": btech})

    master = _merged({"A": system(0.0, 3.0, 5.0), "B": system(0.0, 4.0, 6.0)})
    gen_costs, bat_costs = life_extension_invest_costs(master)
    assert gen_costs == {"A__old": [3.0, 3.0, 0.0, 0.0], "B__old": [0.0, 0.0, 4.0, 4.0]}
    assert bat_costs == {"A__li": [5.0, 5.0, 0.0, 0.0], "B__li": [0.0, 0.0, 6.0, 6.0]}

    # B's candidate has no cost at B's nodes: still free, and said so
    master = _merged({"A": system(0.0, 3.0, 5.0), "B": system(0.0, 0.0, 6.0)})
    with caplog.at_level(logging.WARNING, logger="esfex.bridge.adapters"):
        gen_costs, _ = life_extension_invest_costs(master)
    assert gen_costs == {"A__old": [3.0, 3.0, 0.0, 0.0]}
    warned = [r.message for r in caplog.records if "is free" in r.message]
    assert len(warned) == 1 and "'B__old'" in warned[0] and "[2, 3]" in warned[0]


@pytest.mark.julia
def test_master_no_longer_extends_a_zero_cost_unit_for_free(tmp_path):
    """A 100 MW diesel past its lifetime with invest_cost 0. Extending it is
    priced like a new diesel unit (dear), so the master builds cheap gas."""
    from esfex.runner import Orchestrator

    demand = tmp_path / "demand.csv"
    np.savetxt(demand, np.full(8760, 80.0))
    old = _make_generator("Old", "Non-renewable", "Diesel", [100.0], life_time=[25],
                          initial_age=[30], degradation_rate=[0.0], invest_cost=[0.0],
                          fuel_cost=[50.0])
    gas = _make_technology("gas_new", "Gas", 1, tech_type="Non-renewable",
                           invest_cost=50000.0, invest_max_power=500.0)
    gas = gas.model_copy(update={"fuel_cost": [50.0]})
    diesel = _make_technology("diesel_new", "Diesel", 1, tech_type="Non-renewable",
                              invest_cost=5.0e6, invest_max_power=0.0)
    sysc = _make_system_config(num_nodes=1, coords=[(21.0, -80.0)],
                               generators={"old": old},
                               technologies={"gas_new": gas, "diesel_new": diesel})
    sysc = sysc.model_copy(update={"demand_path": str(demand),
                                   "penalties": PenaltiesConfig(loss_of_load=10000.0)})
    cfg = _make_esfex_config(
        sysc, simulation_mode="development",
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720,
                                overlap_hours=0),
        master_problem=MasterProblemConfig())
    inv = Orchestrator(cfg, output_dir=tmp_path / "out").run(years=1)[0].investments
    assert inv.get("tech_investment_power_0_0", 0.0) > 50.0


def test_master_uses_the_configured_life_extension_cost_factor():
    """The runner never passed life_extension_cost_factor: the adapter's
    hard-coded 0.3 applied whatever the configuration said."""
    from esfex.bridge.adapters import MasterProblemAdapter

    sysc = _make_system_config(num_nodes=1, coords=[(21.0, -80.0)]).model_copy(
        update={"life_extension_cost_factor": 0.5})
    master = MasterProblemAdapter(config=sysc, years=[2025], base_year=2025,
                                  demand=np.ones((24, 1)))
    assert master.life_extension_cost_factor == 0.5
    master = MasterProblemAdapter(config=sysc, years=[2025], base_year=2025,
                                  demand=np.ones((24, 1)),
                                  life_extension_cost_factor=0.1)
    assert master.life_extension_cost_factor == 0.1
