"""Primary-energy inputs reach the model as configured.

- Non-electric demand was a twelfth of the annual demand (monthly shares used
  as multipliers), its month came from the position inside the window, and
  with steps longer than 1 h it shrank by the step length.
- Non-electric emissions were always 0: the emission factor was read from the
  source config instead of the fuel definition.
- A route without ``route_id`` made the block fail (and the run drop it).
- Built fuel capacity never reached later windows (cumulative capacities were
  passed empty), and unmet non-electric demand ignored
  ``non_electric_demand_loss`` (one shared fuel slack covered every sector).
- The dispatch charged ``fuel_cost`` for fuel the primary-energy block buys.
"""

import pytest

pytestmark = pytest.mark.julia


def _adapter(**kw):
    from esfex.bridge.adapters import PrimaryEnergyAdapter

    args = dict(
        year=2025, base_year=2025, hours=72, num_nodes=2,
        fuels_config={"Gas": {
            "max_availability": [1000.0, 500.0], "storage_capacity": [100.0, 50.0],
            "initial_storage_level": [0.5, 0.5], "import_cost": [0.0, 0.0]}},
        non_electric_demand={"Industrial_Gas": {"fuel": "Gas", "demand": [8760.0, 0.0]}},
        infrastructure_config={},
        transport_distances=[[0.0, 100.0], [100.0, 0.0]],
        generators=[],
        fuels_definition={"Gas": {"price_base": 10.0, "energy_content": 10.0,
                                  "emission_factor": 0.2}},
        penalties_config={"loss_of_fuel_supply": 1000.0, "non_electric_demand_loss": 300.0},
        primary_energy_resolution=24,
    )
    args.update(kw)
    return PrimaryEnergyAdapter(**args)


def _ne_demand(adapter):
    """Periodic non-electric demand of (Gas, Industrial) at node 1, per period."""
    from esfex.bridge.julia_setup import get_julia

    jl = get_julia()
    jl._pe_in = adapter._create_julia_input()
    return list(jl.seval("""
        let t = ESFEX.create_temporal_mapping(_pe_in.hours,
                    _pe_in.primary_energy_resolution, _pe_in.investment_resolution)
            ne = ESFEX.prepare_fuel_data(_pe_in, t)[3][("Gas", "Industrial")][1]
            [ne[p] for p in sort(collect(keys(ne)))]
        end"""))


def test_non_electric_demand_is_prorated_by_hours_and_month():
    # 8760 units/yr, even profile: 24 per day (it used to be 2)
    assert _ne_demand(_adapter()) == pytest.approx([24.0] * 3)
    # 2 h steps: 36 steps of a 72 h window, still 24 per day
    assert _ne_demand(_adapter(hours=36, step_hours=2, primary_energy_resolution=12)) == \
        pytest.approx([24.0] * 3)
    # January twice as heavy as other months (factors relative to their mean)
    jan = [2.0] + [1.0] * 11
    ne = {"Industrial_Gas": {"fuel": "Gas", "demand": [8760.0, 0.0], "seasonal_factors": jan}}
    assert _ne_demand(_adapter(non_electric_demand=ne))[0] == pytest.approx(24.0 * 2 / (13 / 12))
    # A window in July falls in a normal month
    assert _ne_demand(_adapter(non_electric_demand=ne, year_hour_offset=4400))[0] == \
        pytest.approx(24.0 / (13 / 12))


def test_inputs_reach_the_julia_input():
    from esfex.bridge.adapters import scale_cost
    from esfex.bridge.julia_setup import get_julia

    jl = get_julia()
    routes = [{"from_node": 0, "to_node": 1, "distance_km": 100.0,
               "fuel_params": {"Gas": {"capacity": 50.0}}}]   # no route_id
    a = _adapter(transport_routes=routes,
                 cumulative_capacities={"storage": {"Gas": {1: 30.0}},
                                        "transport": {"Gas": {0: 5.0}}})
    jl._pe_in = a._create_julia_input()
    assert float(jl.seval("_pe_in.fuels[1].emission_factor")) == pytest.approx(0.2)
    assert str(jl.seval("_pe_in.transport_routes[1].route_id")) == "route_0"
    assert float(jl.seval('ESFEX._cumulative_capacity(_pe_in, "storage", "Gas", 2)')) == 30.0
    assert float(jl.seval('ESFEX._cumulative_capacity(_pe_in, "transport", "Gas", 1)')) == 5.0
    assert float(jl.seval("_pe_in.non_electric_demand_loss_penalty")) == pytest.approx(scale_cost(300.0))


def test_route_without_endpoints_names_the_field():
    with pytest.raises(ValueError, match="distance_km"):
        _adapter(transport_routes=[{"route_id": "r", "from_node": 0, "to_node": 1}]
                 )._create_julia_input()


def test_unmet_non_electric_demand_priced_per_sector():
    """Two sectors short of fuel: each unserved unit costs non_electric_demand_loss."""
    from esfex.bridge.julia_setup import get_julia

    jl = get_julia()
    ne = {"Industrial_Gas": {"fuel": "Gas", "demand": [3650.0, 0.0]},
          "Transport_Gas": {"fuel": "Gas", "demand": [3650.0, 0.0]}}
    # No supply and an empty tank: all 2 × 30 units (3 days × 10/day) unserved
    fuels = {"Gas": {"max_availability": [0.0, 0.0], "storage_capacity": [0.0, 0.0],
                     "initial_storage_level": [0.0, 0.0], "import_cost": [0.0, 0.0]}}
    a = _adapter(non_electric_demand=ne, fuels_config=fuels)
    jl._pe_in = a._create_julia_input()
    jl.seval("using JuMP, HiGHS")
    short = float(jl.seval("""
        let m = Model(HiGHS.Optimizer)
            set_silent(m)
            vars, temporal, prices = ESFEX.create_primary_energy_model(m, _pe_in)
            @objective(m, Min, sum(values(ESFEX.get_primary_energy_objective_terms(
                vars, _pe_in, temporal, prices))))
            optimize!(m)
            sum(sum(value.(v)) for v in values(vars.ne_demand_shortfall_periodic))
        end"""))
    assert short == pytest.approx(60.0, rel=1e-6)


def test_fuel_drawn_from_the_tank_is_charged():
    """Without the dispatch's fuel_cost, fuel already in the tank must still
    cost its price when used (stock change valued at the fuel price)."""
    from esfex.bridge.adapters import scale_cost
    from esfex.bridge.julia_setup import get_julia

    jl = get_julia()
    # No supply; 50 units in node 1's tank; 10/day non-electric demand for 3 days
    fuels = {"Gas": {"max_availability": [0.0, 0.0], "storage_capacity": [100.0, 0.0],
                     "initial_storage_level": [0.5, 0.0], "import_cost": [0.0, 0.0]}}
    ne = {"Industrial_Gas": {"fuel": "Gas", "demand": [3650.0, 0.0]}}
    jl._pe_in = _adapter(fuels_config=fuels, non_electric_demand=ne)._create_julia_input()
    jl.seval("using JuMP, HiGHS")
    supply_cost, drawn = jl.seval("""
        let m = Model(HiGHS.Optimizer)
            set_silent(m)
            vars, temporal, prices = ESFEX.create_primary_energy_model(m, _pe_in)
            terms = ESFEX.get_primary_energy_objective_terms(vars, _pe_in, temporal, prices)
            @objective(m, Min, sum(values(terms)))
            optimize!(m)
            lv = value.(vars.fuel_storage_level_hourly["Gas"])
            (value(terms[:pe_supply_cost]), lv[1, 1] - lv[1, end])
        end""")
    # Nothing is bought; what leaves the tank is charged at the fuel price
    assert float(drawn) > 1.0
    assert float(supply_cost) == pytest.approx(float(drawn) * scale_cost(10.0), rel=1e-6)
