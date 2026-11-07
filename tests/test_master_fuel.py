"""The master builds fuel infrastructure and electrolyzers; the dispatch uses them (#89).

The master had no fuel model, so fuel storage and routes were built by each
dispatch window on its own, electrolyzers could not be built at all, and the
stochastic fuel and electrolyzer multipliers had nothing to scale.
"""

import sys

import h5py
import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _create_orchestrator, _make_esfex_config, _make_generator  # noqa: E402
from test_zones import _make_system_config  # noqa: E402

from esfex.config.schema import (  # noqa: E402
    ElectrolyzerConfig,
    FuelConfig,
    MasterProblemConfig,
    NonElectricDemandConfig,
    PenaltiesConfig,
    PrimaryEnergySourceConfig,
    ScenarioMultipliers,
    StochasticScenarioConfig,
    TemporalConfig,
)


class _FakeMaster:
    def __init__(self, by_year):
        self.by_year = by_year

    def fuel_capacities_by_year(self, result):
        return self.by_year


def _electrolyzer(rated, invest_max, invest_cost=1000.0):
    return ElectrolyzerConfig(
        name="elz", life_time=[20], initial_age=[0], degradation_rate=[0.0],
        rated_power=[rated], min_power=[0.0], ramp_up=[0.0], ramp_down=[0.0],
        eff_at_rated=[0.65], eff_at_min=[0.65], fixed_cost=[0.0],
        variable_cost=[1.0], invest_cost=[invest_cost], invest_max_power=[invest_max])


def test_master_builds_reach_the_dispatch(tmp_path):
    """Each year's dispatch gets the fuel storage, routes and electrolyzer
    power the master built and that is in service that year."""
    orch = _create_orchestrator(tmp_path=tmp_path)
    from esfex.runner import SimulationState
    orch.state = SimulationState(year=2025, base_year=2025, units_config={})
    orch.primary_system = orch.primary_system.model_copy(
        update={"electrolyzers": {"elz": _electrolyzer(5.0, 50.0)}})
    master = _FakeMaster({
        1: {"storage": {"Gas": {0: 30.0}}, "electrolyzer": {"elz": [10.0]}},
        2: {"storage": {"Gas": {0: 30.0}}, "transport": {"Gas": {1: 7.0}},
            "electrolyzer": {"elz": [10.0]}},
    })
    orch._store_master_fuel(master, {}, [2025, 2026])
    assert orch._dispatch_pe_capacities(2025) == {"storage": {"Gas": {0: 30.0}}, "transport": {}}
    assert orch._dispatch_pe_capacities(2026)["transport"] == {"Gas": {1: 7.0}}
    assert orch.state.primary_energy_capacities["transport"] == {"Gas": {1: 7.0}}
    assert orch._dispatch_system(2026).electrolyzers["elz"].rated_power == [15.0]
    # Without builds the dispatch builds nothing either
    assert orch._dispatch_pe_capacities(2030) == {"storage": {}, "transport": {}}
    assert orch._dispatch_system(2030) is orch.primary_system

    # Myopic: a year adds its builds to what earlier years built
    orch._store_master_fuel(_FakeMaster({1: {"storage": {"Gas": {0: 5.0}},
                                             "electrolyzer": {"elz": [1.0]}}}),
                            {}, [2027], carried=orch._master_fuel_by_year[2026])
    assert orch._master_fuel_by_year[2027]["storage"] == {"Gas": {0: 35.0}}
    assert orch._master_fuel_by_year[2027]["electrolyzer"] == {"elz": [11.0]}
    # ... and the next year's master sees them as existing, with less room
    sys_y = orch._system_with_electrolyzers(orch.primary_system, {"elz": [11.0]},
                                            reduce_invest=True)
    assert sys_y.electrolyzers["elz"].rated_power == [16.0]
    assert sys_y.electrolyzers["elz"].invest_max_power == [39.0]


def _hydrogen_plan(tmp_path, tag, scenarios=None, myopic=False):
    """One node: 60 MW of load on diesel, 876 t/yr of hydrogen demand that
    only an electrolyzer (none installed, up to 50 MW) can make."""
    from esfex.runner import Orchestrator

    demand = tmp_path / "demand.csv"
    np.savetxt(demand, np.full(8760, 60.0))
    diesel = _make_generator("Diesel", "Non-renewable", "Diesel", [150.0],
                             degradation_rate=[0.0], fuel_cost=[80.0],
                             invest_max_power=[0.0])
    src = dict(unit="t", import_cost=[0.0], storage_capacity=[0.0],
               initial_storage_level=[0.0], storage_investment_cost=0.0,
               transport_cost=0.0, transport_losses=0.0,
               max_storage_investment_per_node=0.0, max_transport_investment_per_arc=0.0)
    sysc = _make_system_config(num_nodes=1, coords=[(21.0, -80.0)],
                               generators={"diesel": diesel})
    sysc = sysc.model_copy(update={
        "demand_path": str(demand),
        "fuels": {
            "Diesel": FuelConfig(name="Diesel", unit="t", emission_factor=0.0,
                                 energy_content=10.0, price_base=200.0),
            "Hydrogen": FuelConfig(name="Hydrogen", unit="t", emission_factor=0.0,
                                   energy_content=33.3, price_base=0.0),
        },
        "primary_energy_sources": {
            "Diesel": PrimaryEnergySourceConfig(name="Diesel", max_availability=[1e9], **src),
            "Hydrogen": PrimaryEnergySourceConfig(name="Hydrogen", max_availability=[0.0], **src),
        },
        "non_electric_demand": {
            "Industrial_Hydrogen": NonElectricDemandConfig(fuel="Hydrogen", unit="t",
                                                           demand=[876]),
        },
        "electrolyzers": {"elz": _electrolyzer(0.0, 50.0)},
        "penalties": PenaltiesConfig(loss_of_load=10000.0, loss_of_fuel_supply=1e5,
                                     non_electric_demand_loss=1e5),
        "stochastic_scenarios": scenarios or []})
    cfg = _make_esfex_config(
        sysc, simulation_mode="development",
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720, overlap_hours=0),
        master_problem=MasterProblemConfig(
            stochastic=bool(scenarios),
            planning_mode="myopic" if myopic else "perfect_foresight"))
    cfg = cfg.model_copy(update={"enable_primary_energy": True})
    orch = Orchestrator(cfg, output_dir=tmp_path / tag)
    results = orch.run(years=1)
    return orch, results


@pytest.mark.julia
def test_master_builds_the_electrolyzer_the_dispatch_runs(tmp_path):
    orch, results = _hydrogen_plan(tmp_path, "base")
    year = results[0].year
    built = orch._master_fuel_by_year[year]["electrolyzer"]["elz"][0]
    # 0.1 t/h of hydrogen at 13 kg/MWh, 80 % of it reaching the tank
    assert built == pytest.approx(0.1 / (13.0 / 1000 * 0.8), rel=1e-3)
    # The dispatch runs it: hydrogen made, none short
    assert results[0].electrolyzer_h2 is not None
    assert float(np.sum(results[0].electrolyzer_power)) > 0
    pe = results[0].primary_energy or {}
    assert sum(float(np.sum(v)) for v in (pe.get("total_loss_of_supply") or {}).values()) \
        == pytest.approx(0.0, abs=1e-3)
    # The build is reported with the year
    keys = []
    with h5py.File(orch._hdf5_path, "r") as f:
        f.visit(keys.append)
    assert any(k.endswith("master_fuel_capacity/electrolyzer/elz") for k in keys)


@pytest.mark.julia
def test_myopic_master_builds_the_electrolyzer(tmp_path):
    orch, results = _hydrogen_plan(tmp_path, "myopic", myopic=True)
    built = orch._master_fuel_by_year[results[0].year]["electrolyzer"]["elz"][0]
    assert built == pytest.approx(0.1 / (13.0 / 1000 * 0.8), rel=1e-3)


@pytest.mark.julia
def test_electrolyzer_invest_multiplier_reaches_the_plan(tmp_path):
    scenario = [StochasticScenarioConfig(
        name="dear", probability=1.0,
        multipliers=ScenarioMultipliers(electrolyzer_invest_cost=1e9))]
    orch, _ = _hydrogen_plan(tmp_path, "dear", scenario)
    caps = next(iter(orch._master_fuel_by_year.values()))
    assert sum(caps.get("electrolyzer", {}).get("elz", [0.0])) == pytest.approx(0.0, abs=1e-6)
