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
- Fuel had two storage layers (periodic and hourly): shipped fuel never reached
  the hourly tanks, non-electric use was counted twice and the periodic
  shortfall acted as a sink. One tank per node now, balanced per time step.
- Source disruption hours were compared with the window's step numbers
  instead of absolute horizon hours.
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
    # Nothing is bought; the 30 units of demand come out of the tank (it
    # used to take 50: non-electric use was counted twice) and are charged at
    # the fuel price
    assert float(drawn) == pytest.approx(30.0, rel=1e-6)
    assert float(supply_cost) == pytest.approx(float(drawn) * scale_cost(10.0), rel=1e-6)


def _solve(adapter, result_expr):
    """Build the adapter's block alone, minimise its cost, evaluate ``result_expr``."""
    from esfex.bridge.julia_setup import get_julia

    jl = get_julia()
    jl._pe_in = adapter._create_julia_input()
    jl.seval("using JuMP, HiGHS")
    return jl.seval(f"""
        let m = Model(HiGHS.Optimizer)
            set_silent(m)
            vars, temporal, prices = ESFEX.create_primary_energy_model(m, _pe_in)
            terms = ESFEX.get_primary_energy_objective_terms(vars, _pe_in, temporal, prices)
            @objective(m, Min, sum(values(terms)))
            optimize!(m)
            @assert termination_status(m) == MOI.OPTIMAL
            short = sum(sum(value.(v)) for v in values(vars.ne_demand_shortfall_periodic))
            loss = sum(sum(value.(v)) for v in values(vars.fuel_loss_of_supply_hourly))
            {result_expr}
        end""")


def test_tank_contents_serve_the_same_amount_of_demand():
    """50 units in the tank serve 50 units of non-electric demand (it served 25)."""
    fuels = {"Gas": {"max_availability": [0.0, 0.0], "storage_capacity": [100.0, 0.0],
                     "initial_storage_level": [0.5, 0.0], "import_cost": [0.0, 0.0]}}
    # 20/day for 3 days = 60 units wanted, 50 in the tank
    ne = {"Industrial_Gas": {"fuel": "Gas", "demand": [7300.0, 0.0]}}
    served, short, loss = _solve(
        _adapter(fuels_config=fuels, non_electric_demand=ne, mode="economic_dispatch"),
        "(sum(sum(value.(v)) for v in values(vars.non_electric_consumption_periodic)), "
        "short, loss)")
    assert float(served) == pytest.approx(50.0, rel=1e-6)
    assert float(short) == pytest.approx(10.0, rel=1e-6)
    assert float(loss) == pytest.approx(0.0, abs=1e-6)


def test_no_shortfall_while_fuel_is_available():
    """Supply available at the node: demand is met from the first hour, with
    no shortfall even though the tank starts empty."""
    fuels = {"Gas": {"max_availability": [87600.0, 0.0], "storage_capacity": [100.0, 0.0],
                     "initial_storage_level": [0.0, 0.0], "import_cost": [0.0, 0.0]}}
    ne = {"Industrial_Gas": {"fuel": "Gas", "demand": [3650.0, 0.0]}}
    short, loss = _solve(_adapter(fuels_config=fuels, non_electric_demand=ne,
                                  mode="economic_dispatch"), "(short, loss)")
    assert float(short) == pytest.approx(0.0, abs=1e-6)
    assert float(loss) == pytest.approx(0.0, abs=1e-6)


def _route(transit_days_per_100km=0.0):
    fuels = {"Gas": {"max_availability": [87600.0, 0.0], "storage_capacity": [100.0, 0.0],
                     "initial_storage_level": [0.0, 0.0], "import_cost": [0.0, 0.0],
                     "transport_transit_days_per_100km": transit_days_per_100km}}
    # Node 2 has no supply and no tank: only shipments from node 1 feed it
    ne = {"Industrial_Gas": {"fuel": "Gas", "demand": [0.0, 3650.0]}}
    routes = [{"route_id": "r", "from_node": 0, "to_node": 1, "distance_km": 100.0,
               "fuel_params": {"Gas": {"capacity": 50.0}}}]
    return _adapter(fuels_config=fuels, non_electric_demand=ne, transport_routes=routes,
                    mode="economic_dispatch")


def test_transported_fuel_serves_hourly_demand_at_destination():
    """Shipped fuel reaches the destination tank (it only entered the periodic
    balance, so the hourly use there ran on the shortfall slack)."""
    short, loss, shipped = _solve(
        _route(), "(short, loss, sum(value.(vars.fuel_transport_periodic[\"Gas\"])))")
    assert float(short) == pytest.approx(0.0, abs=1e-6)
    assert float(loss) == pytest.approx(0.0, abs=1e-6)
    assert float(shipped) >= 30.0 - 1e-6


def test_shipments_arrive_after_the_transit_delay():
    """1 day per 100 km on a 100 km route: the first day's demand at the
    destination is unmet, later days are served by what was shipped."""
    short = _solve(_route(1.0),
                   "[sum(value.(v[1:end, p])) for p in 1:3 "
                   "for v in values(vars.ne_demand_shortfall_periodic)]")
    assert [float(x) for x in short] == pytest.approx([10.0, 0.0, 0.0], abs=1e-6)


def _max_supply(adapter):
    """Per-period supply limit of Gas at node 1 (prepare_fuel_data)."""
    from esfex.bridge.julia_setup import get_julia

    jl = get_julia()
    jl._pe_in = adapter._create_julia_input()
    return [float(x) for x in jl.seval("""
        let t = ESFEX.create_temporal_mapping(_pe_in.hours,
                    _pe_in.primary_energy_resolution, _pe_in.investment_resolution)
            ms = ESFEX.prepare_fuel_data(_pe_in, t)[2]["Gas"][1]
            [ms[p] for p in sort(collect(keys(ms)))]
        end""")]


def test_source_disruption_uses_absolute_horizon_hours():
    """Disruption hours are horizon hours (0 = Jan 1 of base_year); they were
    compared with the window's step numbers, so they hit every window's start
    and never a later year or a later part of the year."""
    def fuels(start, end):
        return {"Gas": {"max_availability": [8760.0, 0.0], "storage_capacity": [100.0, 0.0],
                        "initial_storage_level": [0.5, 0.0], "import_cost": [0.0, 0.0],
                        "disruption_start_hour": start, "disruption_end_hour": end,
                        "disruption_availability": 0.0}}
    # Second day of the second year
    got = _max_supply(_adapter(year=2026, fuels_config=fuels(8760 + 24, 8760 + 48)))
    assert got == pytest.approx([24.0, 0.0, 24.0])
    # A window in July of the base year
    got = _max_supply(_adapter(year_hour_offset=4400, fuels_config=fuels(4424, 4448)))
    assert got == pytest.approx([24.0, 0.0, 24.0])
    # A disruption in the first days of the base year does not repeat later
    assert _max_supply(_adapter(year=2026, fuels_config=fuels(0, 24))) == \
        pytest.approx([24.0, 24.0, 24.0])
    # 2 h steps: half of the first 12-step period is cut
    got = _max_supply(_adapter(hours=36, step_hours=2, primary_energy_resolution=12,
                               fuels_config=fuels(12, 24)))
    assert got[:2] == pytest.approx([6.0, 12.0])


def test_scheduled_fuel_source_outage_in_a_later_year():
    """A scheduled fuel-source outage applies in the year it is scheduled
    (once), or every year when recurring."""
    from esfex.config.schema import OutageWindow

    fuels = {"Gas": {"max_availability": [8760.0, 0.0], "storage_capacity": [100.0, 0.0],
                     "initial_storage_level": [0.5, 0.0], "import_cost": [0.0, 0.0]}}

    def ow(start, end, recurring=False):
        return [OutageWindow(element_type="fuel_source", element_id="Gas",
                             start_hour=start, end_hour=end, recurring=recurring)]
    # Year 2 window, scheduled for year 2: cut on day 2
    assert _max_supply(_adapter(year=2026, fuels_config=fuels,
                                outage_schedule=ow(8784, 8808))) == pytest.approx([24.0, 0.0, 24.0])
    # Same hours of the year, scheduled once for the base year: no cut in year 2
    assert _max_supply(_adapter(year=2026, fuels_config=fuels,
                                outage_schedule=ow(24, 48))) == pytest.approx([24.0] * 3)
    # ... unless it recurs
    assert _max_supply(_adapter(year=2026, fuels_config=fuels,
                                outage_schedule=ow(24, 48, True))) == pytest.approx([24.0, 0.0, 24.0])
