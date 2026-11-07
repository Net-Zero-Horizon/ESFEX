"""Primary energy across rolling-horizon windows and with the electrolyzers.

- Fuel shipped in a window's committed steps that arrives after the commit
  point was paid and never delivered: the next window started from the tank
  levels only, so a route-fed node was short at the start of every window.
- The dispatch electrolyzers' hydrogen never reached the primary-energy
  ``Hydrogen`` tank (they were a plain load); a hydrogen-fuelled unit ran on
  fuel supply or shortfall instead.
"""

import sys
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

sys.path.insert(0, "tests")


def _route_adapter(offset, init_levels=None, arrivals=None):
    """48 steps, node 1 supplies, node 2 (no tank) has 10/day of demand and
    is fed over a 100 km route with 1 day of transit."""
    from esfex.bridge.adapters import PrimaryEnergyAdapter

    return PrimaryEnergyAdapter(
        year=2025, base_year=2025, hours=48, num_nodes=2,
        fuels_config={"Gas": {
            "max_availability": [87600.0, 0.0], "storage_capacity": [100.0, 0.0],
            "initial_storage_level": [0.0, 0.0], "import_cost": [0.0, 0.0],
            "transport_transit_days_per_100km": 1.0}},
        non_electric_demand={"Industrial_Gas": {"fuel": "Gas", "demand": [0.0, 3650.0]}},
        infrastructure_config={}, transport_distances=[[0.0, 100.0], [100.0, 0.0]],
        generators=[],
        fuels_definition={"Gas": {"price_base": 10.0, "energy_content": 10.0}},
        penalties_config={"loss_of_fuel_supply": 1000.0, "non_electric_demand_loss": 300.0},
        primary_energy_resolution=24, mode="economic_dispatch",
        transport_routes=[{"route_id": "r", "from_node": 0, "to_node": 1,
                           "distance_km": 100.0, "fuel_params": {"Gas": {"capacity": 50.0}}}],
        year_hour_offset=offset, initial_storage_levels=init_levels,
        scheduled_arrivals=arrivals,
    )


def _solve_block(adapter):
    """Build the adapter's block in its own model and minimise its cost."""
    from esfex.bridge.julia_setup import get_julia

    jl = get_julia()
    jl.seval("using JuMP")
    model = jl.seval("""let m = JuMP.Model(ESFEX.create_optimizer(solver_name = "highs"))
        JuMP.set_silent(m); m end""")
    adapter.create_variables(model)
    jl._pe_m = model
    jl._pe_terms = adapter.get_objective_terms()
    jl.seval("JuMP.@objective(_pe_m, Min, sum(values(_pe_terms))); JuMP.optimize!(_pe_m)")
    assert str(jl.seval("JuMP.termination_status(_pe_m)")) == "OPTIMAL"


@pytest.mark.julia
def test_fuel_in_transit_reaches_the_next_window():
    """Default rolling horizon (48 h window, 24 h kept) and 1 day of transit:
    day 1's shipment is on the road at the commit point and reaches the next
    window; node 2 was short on the first day of every window."""
    from esfex.bridge.julia_setup import get_julia

    jl = get_julia()
    levels, arrivals, short_day1 = None, None, []
    for w in range(4):
        pe = _route_adapter(24 * w, levels, arrivals)
        _solve_block(pe)
        jl._pe_vars = pe._jl_vars
        short_day1.append(float(jl.seval(
            'JuMP.value(_pe_vars.ne_demand_shortfall_periodic[("Gas", "Industrial")][2, 1])')))
        levels = pe.get_final_storage_levels(step=24)
        arrivals = pe.get_in_transit_arrivals(step=24)
        # 10 units reach node 2's tank (0.8 inflow efficiency) during the next day
        assert arrivals["Gas"].shape == (2, 24)
        assert arrivals["Gas"].sum() == pytest.approx(10.0 / 0.8, abs=1e-6)
    assert short_day1[0] == pytest.approx(10.0, abs=1e-6)   # nothing on the way yet
    assert short_day1[1:] == pytest.approx([0.0] * 3, abs=1e-6)


def test_runner_hands_fuel_in_transit_to_the_next_window(tmp_path):
    """_solve_window passes the previous window's fuel in transit to the
    primary-energy block and stores what is in transit at the commit point."""
    from esfex.config.schema import PrimaryEnergySourceConfig
    from esfex.runner import Orchestrator, SimulationState
    from test_runner import _make_esfex_config, _make_system_config  # noqa: E402

    two = [0.0, 0.0]
    sys_cfg = _make_system_config().model_copy(update={"primary_energy_sources": {
        "Gas": PrimaryEnergySourceConfig(
            name="Gas", unit="t", max_availability=[100.0, 0.0], import_cost=two,
            storage_capacity=two, initial_storage_level=two,
            transport_cost=0.0, transport_losses=0.0)}})
    orch = Orchestrator(_make_esfex_config(sys_cfg), output_dir=tmp_path)
    orch._availability_cache, orch._inflow_cache = {}, {}
    orch.state = SimulationState(year=2025, base_year=2025, units_config={})
    orch._gen_names, orch._bat_names = [], []
    ps = MagicMock()
    ps.solve.return_value = 1
    ps.get_solution_values.return_value = {"objective": 0.0, "status": "OPTIMAL"}
    pe = MagicMock()
    pe.get_results.return_value = None
    pe.get_final_storage_levels.return_value = {"Gas": {0: 1.0, 1: 0.0}}
    in_transit = {"Gas": np.ones((2, 3))}
    pe.get_in_transit_arrivals.return_value = in_transit
    earlier = {"Gas": np.full((2, 5), 2.0)}
    with patch("esfex.runner.PowerSystemAdapter", return_value=ps), \
            patch("esfex.runner.PrimaryEnergyAdapter", return_value=pe) as pe_cls:
        res = orch._solve_window(
            year=2025, window=1, demand=np.ones((6, 2)), hours=6, num_nodes=2,
            boundary_conditions={"pe_in_transit": earlier}, keep_hours=4)
    assert pe_cls.call_args.kwargs["scheduled_arrivals"] is earlier
    pe.get_in_transit_arrivals.assert_called_once_with(step=4)
    pe.get_final_storage_levels.assert_called_once_with(step=4)
    assert res["boundary_conditions"]["pe_in_transit"] is in_transit


@pytest.mark.julia
def test_electrolyzer_hydrogen_feeds_the_hydrogen_tank():
    """Step 1: sun for the load and a 50 MW electrolyzer. Step 2: no sun, a
    hydrogen turbine serves the 5 MW load on the electrolyzer's hydrogen,
    with no hydrogen supply and no shortfall (coupled by the adapter)."""
    from types import SimpleNamespace

    from esfex.bridge.julia_setup import get_julia

    jl = get_julia()
    jl.seval("using JuMP")
    ps_model, ps_vars, ps_input = jl.seval("""
        let H = 2, z = [0.0]
            gen(name, type, fuel, rated, avail) = GeneratorConfig(
                name, type, fuel, [rated], z, [0.5], [0.4], [1.0], [1.0], z, z, z,
                z, z, z, z, z, z, avail, false, [30.0], z, z, z, 50.0, "AC",
                z, z, z, z, zeros(H, 1), z, z, z, z, false, z, z, z)
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            elz = ElectrolyzerConfig([50.0], [0.65], [0.65], 50.0, [0.0], [0.0],
                                     z, z, z, [1.0e-9], 0.0, [20.0])
            input = PowerSystemInput(
                name = "h2", year = 2025, network = net,
                generators = [gen("PV", "Renewable", "Sun", 60.0, reshape([1.0, 0.0], H, 1)),
                              gen("H2GT", "Non-renewable", "Hydrogen", 10.0, ones(H, 1))],
                batteries = BatteryConfig[], demand = fill(5.0, H, 1),
                temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                electrolyzers = [elz],
                mode = "economic_dispatch", solver_name = "highs", verbose = false)
            model, vars = create_power_system(input)
            (model, vars, input)
        end""")
    from esfex.bridge.adapters import PrimaryEnergyAdapter

    pe = PrimaryEnergyAdapter(
        year=2025, base_year=2025, hours=2, num_nodes=1,
        fuels_config={"Hydrogen": {
            "max_availability": [0.0], "storage_capacity": [10.0],
            "initial_storage_level": [0.0], "import_cost": [0.0]}},
        non_electric_demand={}, infrastructure_config={}, transport_distances=[[0.0]],
        generators=[{"name": "PV", "fuel": "Sun"},
                    {"name": "H2GT", "fuel": "Hydrogen", "eff_at_rated": [0.5]}],
        fuels_definition={"Hydrogen": {"price_base": 0.0, "energy_content": 33.3}},
        penalties_config={"loss_of_fuel_supply": 1000.0, "non_electric_demand_loss": 300.0},
        mode="economic_dispatch",
    )
    pe.create_variables(ps_model)
    pe.integrate_with_power_system(SimpleNamespace(
        _jl_vars=ps_vars, _jl_input=ps_input, esfex_config=None))
    jl._m, jl._v, jl._pe = ps_model, ps_vars, pe._jl_vars
    jl.seval("JuMP.optimize!(_m)")
    assert str(jl.seval("JuMP.termination_status(_m)")) == "OPTIMAL"
    assert float(jl.seval("JuMP.value(_v.gen_output[2, 1, 2])")) == pytest.approx(5.0, abs=1e-6)
    assert float(jl.seval("sum(JuMP.value.(_v.load_shed))")) == pytest.approx(0.0, abs=1e-6)
    assert float(jl.seval(
        'sum(JuMP.value.(_pe.fuel_loss_of_supply_hourly["Hydrogen"]))')) == pytest.approx(0.0, abs=1e-6)
    assert float(jl.seval(
        'sum(JuMP.value.(_pe.fuel_excess_supply_periodic["Hydrogen"]))')) == pytest.approx(0.0, abs=1e-6)
