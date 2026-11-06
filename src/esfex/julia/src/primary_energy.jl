"""
primary_energy.jl - Primary Energy Supply Chain Model

Models primary energy flows (fuels) with multi-scale temporal resolution
and integration with the electrical system, considering non-electric demands.

Temporal Scales:
- Investment: Capacity decisions (typically annual)
- Primary Period: supply limits and fuel shipments (configurable, 24 h default)
- Hourly (time step): the fuel tanks and every use of fuel

Each node has one tank, balanced per time step:
    level[t+1] = level[t] + (local supply_t + arrivals_t) * efficiency
                 - drawn_t - departures_t
    drawn_t + shortfall_t >= fuel for power_t + non-electric consumption_t
A period's route shipment leaves its origin evenly over the period's steps
and arrives at the destination after the route's transit delay, net of
transport losses; fuel still in transit when a rolling-horizon window's
committed steps end arrives in the next window (`in_transit_arrivals`).
Local supply per step is free within the period's limit. The `Hydrogen` tank
also receives the hydrogen of the dispatch electrolyzers at its node.

Constants:
- KG_PER_TONNE: electrolyzer output (kg) → hydrogen tank units (t)
"""

using JuMP: @variable, @constraint, @expression, @objective, Model, AffExpr
using JuMP: value, VariableRef, add_to_expression!, lower_bound, upper_bound

# Electrolyzer hydrogen (kg, `electrolyzer_h2_per_mwh`) enters the Hydrogen
# tank in tonnes
const KG_PER_TONNE = 1000.0

# The fuel whose tank receives the electrolyzers' hydrogen
const HYDROGEN_FUEL = "Hydrogen"

# =============================================================================
# Temporal Period Mapping
# =============================================================================

"""
    TemporalMapping

Internal struct to hold temporal period mappings.
"""
struct TemporalMapping
    # Number of periods at each scale
    num_primary_periods::Int
    num_investment_periods::Int

    # Period indices
    primary_period_indices::Vector{Int}
    investment_period_indices::Vector{Int}

    # Mappings
    hour_to_primary_period::Dict{Int, Int}
    primary_to_investment_period::Dict{Int, Int}
    hours_in_primary_period::Dict{Int, Vector{Int}}
    primary_periods_in_investment_period::Dict{Int, Vector{Int}}
end

"""
    create_temporal_mapping(hours, primary_resolution, investment_resolution)

Create temporal period mappings for multi-scale optimization.
"""
function create_temporal_mapping(
    hours::Int,
    primary_resolution::Int,
    investment_resolution::Int
)::TemporalMapping
    # Primary periods
    period_length = min(primary_resolution, hours)
    if period_length <= 0
        period_length = hours
    end

    num_primary_periods = cld(hours, period_length)  # Ceiling division

    primary_period_indices = collect(1:num_primary_periods)
    hour_to_primary_period = Dict{Int, Int}()
    hours_in_primary_period = Dict{Int, Vector{Int}}()

    current_hour = 1
    for p in 1:num_primary_periods
        start_h = current_hour
        end_h = min(hours, start_h + period_length - 1)
        hours_in_primary_period[p] = collect(start_h:end_h)
        for h in start_h:end_h
            hour_to_primary_period[h] = p
        end
        current_hour = end_h + 1
    end

    # Investment periods (typically spans entire simulation)
    inv_period_length = min(investment_resolution, hours)
    if inv_period_length <= 0
        inv_period_length = hours
    end

    num_investment_periods = cld(hours, inv_period_length)
    investment_period_indices = collect(1:num_investment_periods)

    primary_to_investment_period = Dict{Int, Int}()
    primary_periods_in_investment_period = Dict{Int, Vector{Int}}()

    for inv_p in 1:num_investment_periods
        primary_periods_in_investment_period[inv_p] = Int[]
        start_hour_inv = (inv_p - 1) * inv_period_length + 1
        end_hour_inv = min(hours, inv_p * inv_period_length)

        for p in primary_period_indices
            if !isempty(hours_in_primary_period[p])
                first_hour_p = hours_in_primary_period[p][1]
                if first_hour_p >= start_hour_inv && first_hour_p <= end_hour_inv
                    primary_to_investment_period[p] = inv_p
                    push!(primary_periods_in_investment_period[inv_p], p)
                end
            end
        end
    end

    return TemporalMapping(
        num_primary_periods,
        num_investment_periods,
        primary_period_indices,
        investment_period_indices,
        hour_to_primary_period,
        primary_to_investment_period,
        hours_in_primary_period,
        primary_periods_in_investment_period
    )
end

# =============================================================================
# Input Data Preparation
# =============================================================================

"""
    _cumulative_capacity(input, kind, fuel, idx) -> Float64

Capacity built in earlier windows: `input.cumulative_capacities[kind][fuel][idx]`
with `kind` "storage" (idx = node) or "transport" (idx = route), 0 if absent.
"""
function _cumulative_capacity(input::PrimaryEnergyInput, kind::String, fuel::String, idx::Int)
    by_fuel = get(input.cumulative_capacities, kind, nothing)
    by_fuel isa AbstractDict || return 0.0
    by_idx = get(by_fuel, fuel, nothing)
    by_idx isa AbstractDict || return 0.0
    return Float64(get(by_idx, idx, 0.0))
end

"""
    _window_start_hour(input) -> Int

Absolute horizon hour at which the window starts: hour 0 is Jan 1 of
`base_year` and every modelled year is 8760 h long.
"""
_window_start_hour(input::PrimaryEnergyInput) =
    (input.year - input.base_year) * HOURS_STD_YEAR + input.year_hour_offset

"""
    _transit_steps(fuel, route, step_hours) -> Int

Transit delay of `route` for `fuel`, in time steps of `step_hours` hours
(`transport_transit_days_per_100km` × distance / 100, rounded).
"""
function _transit_steps(fuel::FuelConfig, route::TransportRoute, step_hours::Int)
    days = fuel.transport_transit_days_per_100km * route.distance_km / 100.0
    return max(0, round(Int, days * 24.0 / max(step_hours, 1)))
end

"""
    _delivered_fraction(fparams, route) -> Float64

Share of a shipment that reaches the destination (transport losses per 100 km).
"""
_delivered_fraction(fparams::FuelRouteParams, route::TransportRoute) =
    max(0.0, 1.0 - fparams.transport_losses * route.distance_km / 100.0)

"""
    _storage_efficiency(input, fuel_name) -> Float64

Efficiency applied to a tank's inflows (`storage_efficiency` of the fuel's
infrastructure, 0.8 when the fuel has none).
"""
function _storage_efficiency(input::PrimaryEnergyInput, fuel_name::String)
    haskey(input.infrastructure, fuel_name) &&
        return input.infrastructure[fuel_name].storage_efficiency
    # @debug not @warn: called per step; the default is fine.
    @debug "Storage efficiency not configured for $fuel_name, using default 0.8"
    return 0.8
end

"""
    prepare_fuel_data(input, temporal)

Prepare fuel supply limits and demand requirements per period.
"""
function prepare_fuel_data(input::PrimaryEnergyInput, temporal::TemporalMapping)
    years_diff = input.year - input.base_year

    # Adjusted fuel prices
    adjusted_prices = Dict{String, Float64}()
    for fuel in input.fuels
        price = fuel.price_base * (1 + fuel.price_growth_rate)^years_diff
        adjusted_prices[fuel.name] = price
    end

    # Max supply per period (scaled from annual availability)
    # CRITICAL FIX: Always use full-year hours as scaling base for annual availability
    # The max_availability in config is specified as "tons/year" (annual availability)
    # In rolling horizon, each window covers a portion of the year, so availability must be
    # prorated based on the actual fraction of the year covered, not the window duration.
    # Using window duration (total_operational_hours) would allow each window to access
    # 100% of annual availability, leading to over-consumption across multiple windows.
    scaling_base_hours = Float64(hours_in_year(input.year))  # Full year hours (always 8760 for leap)
    # Absolute horizon hour at which the window's first step starts (hour 0 =
    # Jan 1 of base_year); disruption windows are given in these hours.
    window_hour0 = _window_start_hour(input)
    step_h = max(input.step_hours, 1)
    max_supply_periodic = Dict{String, Dict{Int, Dict{Int, Float64}}}()
    for fuel in input.fuels
        max_supply_periodic[fuel.name] = Dict{Int, Dict{Int, Float64}}()
        for n in 1:input.num_nodes
            max_supply_periodic[fuel.name][n] = Dict{Int, Float64}()
            annual_avail = fuel.max_availability[n]

            for p in temporal.primary_period_indices
                hrs = temporal.hours_in_primary_period[p]
                hours_in_p = length(hrs)
                # Scale availability proportionally to this period's duration relative to full year
                fraction = hours_in_p / scaling_base_hours
                # Source disruption: scale availability by the fraction of this
                # period's hours that fall inside the disruption window (reduced
                # to disruption_availability there). Step s covers the absolute
                # hours [window_hour0 + (s-1)*step_h, window_hour0 + s*step_h).
                disruption_mult = 1.0
                if fuel.disruption_end_hour > fuel.disruption_start_hour && hours_in_p > 0
                    n_cut = sum(hrs) do s
                        h0 = window_hour0 + (s - 1) * step_h
                        max(0, min(h0 + step_h, fuel.disruption_end_hour) -
                               max(h0, fuel.disruption_start_hour))
                    end
                    frac_cut = n_cut / (hours_in_p * step_h)
                    disruption_mult = (1.0 - frac_cut) +
                        frac_cut * fuel.disruption_availability
                end
                max_supply_periodic[fuel.name][n][p] =
                    annual_avail * fraction * disruption_mult
            end
        end
    end

    # Non-electric demand per period
    ne_demand_periodic = Dict{Tuple{String, String}, Dict{Int, Dict{Int, Float64}}}()
    for ne_config in input.non_electric_demand
        key = (ne_config.fuel, ne_config.sector)
        ne_demand_periodic[key] = Dict{Int, Dict{Int, Float64}}()

        growth_factor = (1 + ne_config.growth_rate)^years_diff

        for n in 1:input.num_nodes
            ne_demand_periodic[key][n] = Dict{Int, Float64}()
            annual_demand = ne_config.annual_demand[n] * growth_factor

            if annual_demand > 1e-6
                for p in temporal.primary_period_indices
                    steps_in_p = temporal.hours_in_primary_period[p]
                    if isempty(steps_in_p)
                        continue
                    end

                    # Month of the period's mid-point, in hours of the year
                    year_h = Float64(hours_in_year(input.year))
                    mid_hour = input.year_hour_offset + (mean(steps_in_p) - 1) * input.step_hours
                    month_idx = clamp(floor(Int, mid_hour / (year_h / 12)) + 1, 1, 12)
                    # Monthly factors relative to their mean (shares summing to 1
                    # and multipliers averaging 1 both keep the annual total)
                    sf = ne_config.seasonal_factors
                    sf_mean = isempty(sf) ? 0.0 : mean(sf)
                    seasonal_factor = sf_mean > 0 ? sf[month_idx] / sf_mean : 1.0

                    # Scale demand to this period's share of the year
                    fraction_of_year = length(steps_in_p) * input.step_hours / year_h
                    ne_demand_periodic[key][n][p] = annual_demand * seasonal_factor * fraction_of_year
                end
            end
        end
    end

    return adjusted_prices, max_supply_periodic, ne_demand_periodic
end

# =============================================================================
# Variable Creation
# =============================================================================

"""
    build_primary_energy_variables!(model, input, temporal)

Create all primary energy decision variables.
"""
function build_primary_energy_variables!(
    model::Model,
    input::PrimaryEnergyInput,
    temporal::TemporalMapping
)::PrimaryEnergyVariables
    n_nodes = input.num_nodes
    n_hours = input.hours
    n_primary = temporal.num_primary_periods
    n_invest = temporal.num_investment_periods

    fuel_names = [f.name for f in input.fuels]
    n_routes = length(input.transport_routes)

    # Collect sectors
    sectors = Set{String}()
    for ne in input.non_electric_demand
        push!(sectors, ne.sector)
    end

    # =========================================================================
    # Investment Variables
    # Skip creation if investment_from_master flag is true (handled by MasterProblem)
    # Matches Python legacy lines 361-379
    # =========================================================================

    transport_inv = Dict{String, Matrix{VariableRef}}()
    storage_inv = Dict{String, Matrix{VariableRef}}()

    # Only create investment variables if NOT handled by MasterProblem
    if input.mode in ["development", "economic_dispatch"] && !input.investment_from_master
        for fuel_name in fuel_names
            if haskey(input.infrastructure, fuel_name)
                infra = input.infrastructure[fuel_name]

                # Transport investment [inv_period, route]
                if n_routes > 0
                    transport_inv[fuel_name] = @variable(model,
                        [inv_p=1:n_invest, r=1:n_routes],
                        lower_bound = 0,
                        base_name = "inv_transport_$(fuel_name)"
                    )
                end

                # Storage investment [inv_period, node]
                storage_inv[fuel_name] = @variable(model,
                    [inv_p=1:n_invest, n=1:n_nodes],
                    lower_bound = 0,
                    base_name = "inv_storage_$(fuel_name)"
                )
            end
        end
    end

    # =========================================================================
    # Primary Period Variables
    # =========================================================================

    fuel_supply_pp = Dict{String, Matrix{VariableRef}}()
    fuel_transport_pp = Dict{String, Matrix{VariableRef}}()
    ne_consumption_pp = Dict{Tuple{String, String}, Matrix{VariableRef}}()
    ne_short_pp = Dict{Tuple{String, String}, Matrix{VariableRef}}()
    excess_supply_pp = Dict{String, Matrix{VariableRef}}()

    for fuel_name in fuel_names
        # Fuel supply [node, period]
        fuel_supply_pp[fuel_name] = @variable(model,
            [n=1:n_nodes, p=1:n_primary],
            lower_bound = 0,
            base_name = "pp_supply_$(fuel_name)"
        )

        # Fuel transport [route, period]
        if n_routes > 0
            fuel_transport_pp[fuel_name] = @variable(model,
                [r=1:n_routes, p=1:n_primary],
                lower_bound = 0,
                base_name = "pp_transport_$(fuel_name)"
            )
        end

        # Excess supply [node, period] - Enabled to match Python legacy
        # Slack variable to allow exceeding max_supply_periodic if needed for feasibility
        # This allows importing more fuel than the periodic limit when necessary (with high cost penalty)
        excess_supply_pp[fuel_name] = @variable(model,
            [n=1:n_nodes, p=1:n_primary],
            lower_bound = 0,
            base_name = "pp_excess_supply_$(fuel_name)"
        )

        # Non-electric consumption per sector
        for sector in sectors
            key = (fuel_name, sector)
            ne_consumption_pp[key] = @variable(model,
                [n=1:n_nodes, p=1:n_primary],
                lower_bound = 0,
                base_name = "pp_ne_$(fuel_name)_$(sector)"
            )
            ne_short_pp[key] = @variable(model,
                [n=1:n_nodes, p=1:n_primary],
                lower_bound = 0,
                base_name = "pp_ne_short_$(fuel_name)_$(sector)"
            )
        end
    end

    # =========================================================================
    # Hourly Operational Variables
    # =========================================================================

    storage_level_hr = Dict{String, Matrix{VariableRef}}()
    storage_in_hr = Dict{String, Matrix{VariableRef}}()
    storage_out_hr = Dict{String, Matrix{VariableRef}}()
    fuel_for_power_hr = Dict{Int, Matrix{VariableRef}}()
    ne_consumption_hr = Dict{Tuple{String, String}, Matrix{VariableRef}}()
    loss_supply_hr = Dict{String, Matrix{VariableRef}}()
    emissions_hr = Dict{String, Matrix{VariableRef}}()

    for fuel_name in fuel_names
        # Storage level (includes hour T+1 for final state)
        storage_level_hr[fuel_name] = @variable(model,
            [n=1:n_nodes, t=1:(n_hours+1)],
            lower_bound = 0,
            base_name = "hr_storage_$(fuel_name)"
        )

        # Local supply into the tank / fuel drawn for consumption [node, hour]
        storage_in_hr[fuel_name] = @variable(model,
            [n=1:n_nodes, t=1:n_hours],
            lower_bound = 0,
            base_name = "hr_storage_in_$(fuel_name)"
        )

        storage_out_hr[fuel_name] = @variable(model,
            [n=1:n_nodes, t=1:n_hours],
            lower_bound = 0,
            base_name = "hr_storage_out_$(fuel_name)"
        )

        # Consumption the tank cannot cover (fuel shortfall) [node, hour]
        loss_supply_hr[fuel_name] = @variable(model,
            [n=1:n_nodes, t=1:n_hours],
            lower_bound = 0,
            base_name = "hr_loss_supply_$(fuel_name)"
        )

        # Emissions from non-electric consumption
        emissions_hr[fuel_name] = @variable(model,
            [n=1:n_nodes, t=1:n_hours],
            lower_bound = 0,
            base_name = "hr_emissions_$(fuel_name)"
        )

        # Hourly NE consumption per sector
        for sector in sectors
            key = (fuel_name, sector)
            ne_consumption_hr[key] = @variable(model,
                [n=1:n_nodes, t=1:n_hours],
                lower_bound = 0,
                base_name = "hr_ne_$(fuel_name)_$(sector)"
            )
        end
    end

    # Fuel for power generation [gen_idx, node, hour]
    for (gen_idx, fuel_info) in input.generator_fuel_map
        fuel_for_power_hr[gen_idx] = @variable(model,
            [n=1:n_nodes, t=1:n_hours],
            lower_bound = 0,
            base_name = "hr_fuel_power_gen$(gen_idx)"
        )
    end

    # Total primary emissions [node, hour]
    total_emissions_hr = @variable(model,
        [n=1:n_nodes, t=1:n_hours],
        lower_bound = 0,
        base_name = "hr_total_primary_emissions"
    )

    return PrimaryEnergyVariables(
        transport_inv,
        storage_inv,
        fuel_supply_pp,
        fuel_transport_pp,
        ne_consumption_pp,
        excess_supply_pp,
        storage_level_hr,
        storage_in_hr,
        storage_out_hr,
        fuel_for_power_hr,
        ne_consumption_hr,
        loss_supply_hr,
        emissions_hr,
        total_emissions_hr,
        ne_short_pp
    )
end

# =============================================================================
# Constraints
# =============================================================================

"""
    add_primary_energy_constraints!(model, vars, input, temporal, adjusted_prices, max_supply, ne_demand)

Add all primary energy constraints to the model.
"""
function add_primary_energy_constraints!(
    model::Model,
    vars::PrimaryEnergyVariables,
    input::PrimaryEnergyInput,
    temporal::TemporalMapping,
    adjusted_prices::Dict{String, Float64},
    max_supply::Dict{String, Dict{Int, Dict{Int, Float64}}},
    ne_demand::Dict{Tuple{String, String}, Dict{Int, Dict{Int, Float64}}}
)
    n_nodes = input.num_nodes
    n_hours = input.hours

    fuel_names = [f.name for f in input.fuels]
    fuel_by_name = Dict(f.name => f for f in input.fuels)

    sectors = Set{String}()
    for ne in input.non_electric_demand
        push!(sectors, ne.sector)
    end

    # =========================================================================
    # I. Investment Constraints
    # =========================================================================

    if input.mode in ["development", "economic_dispatch"]
        for inv_p in temporal.investment_period_indices
            for fuel_name in fuel_names
                if haskey(input.infrastructure, fuel_name)
                    infra = input.infrastructure[fuel_name]
                    fuel = fuel_by_name[fuel_name]

                    # Transport investment limits (route-based)
                    if haskey(vars.transport_capacity_investment, fuel_name)
                        for (r, route) in enumerate(input.transport_routes)
                            if haskey(route.fuel_params, fuel_name)
                                fparams = route.fuel_params[fuel_name]
                                # Expansion limit net of what earlier windows built
                                built = _cumulative_capacity(input, "transport", fuel_name, r)
                                @constraint(model,
                                    vars.transport_capacity_investment[fuel_name][inv_p, r] <=
                                    max(0.0, fparams.capacity * infra.transport_expansion_limit - built),
                                    base_name = "inv_transport_limit_$(fuel_name)_$(inv_p)_r$(r)"
                                )
                            end
                        end
                    end

                    # Storage investment limits
                    if haskey(vars.storage_capacity_investment, fuel_name)
                        for n in 1:n_nodes
                            base_cap = fuel.storage_capacity[n]
                            built = _cumulative_capacity(input, "storage", fuel_name, n)
                            @constraint(model,
                                vars.storage_capacity_investment[fuel_name][inv_p, n] <=
                                max(0.0, base_cap * infra.storage_expansion_limit - built),
                                base_name = "inv_storage_limit_$(fuel_name)_$(inv_p)_$(n)"
                            )
                        end
                    end
                end
            end
        end
    end

    # =========================================================================
    # II. Primary Period Constraints (supply limits, NE demand, shipments)
    # =========================================================================

    for p in temporal.primary_period_indices
        inv_p = get(temporal.primary_to_investment_period, p, 1)
        days_in_p = length(temporal.hours_in_primary_period[p]) * input.step_hours / 24.0

        for fuel_name in fuel_names
            for n in 1:n_nodes
                # Supply limit (with excess slack)
                max_sup = get(get(max_supply[fuel_name], n, Dict()), p, Inf)
                @constraint(model,
                    vars.fuel_supply_periodic[fuel_name][n, p] <=
                    max_sup + vars.fuel_excess_supply_periodic[fuel_name][n, p],
                    base_name = "pp_max_supply_$(fuel_name)_$(n)_$(p)"
                )

                # NE demand satisfaction
                for sector in sectors
                    key = (fuel_name, sector)
                    if haskey(ne_demand, key)
                        required = get(get(ne_demand[key], n, Dict()), p, 0.0)
                        if required > 1e-6
                            @constraint(model,
                                vars.non_electric_consumption_periodic[key][n, p] <= required,
                                base_name = "pp_ne_max_$(fuel_name)_$(sector)_$(n)_$(p)"
                            )
                            # Each sector's unmet demand has its own slack, priced at
                            # non_electric_demand_loss (one shared fuel slack used to
                            # cover every sector at once).
                            @constraint(model,
                                vars.non_electric_consumption_periodic[key][n, p] +
                                vars.ne_demand_shortfall_periodic[key][n, p] >= required,
                                base_name = "pp_ne_satisfy_$(fuel_name)_$(sector)_$(n)_$(p)"
                            )
                        end
                    end
                end
            end

            # Transport capacity constraints (route-based, units/day)
            for (r, route) in enumerate(input.transport_routes)
                if !haskey(route.fuel_params, fuel_name)
                    continue
                end
                fparams = route.fuel_params[fuel_name]
                base_daily_cap = fparams.capacity

                # Look up cumulative capacity using route index
                cumul_cap = _cumulative_capacity(input, "transport", fuel_name, r)
                inv_cap = if haskey(vars.transport_capacity_investment, fuel_name)
                    vars.transport_capacity_investment[fuel_name][inv_p, r]
                else
                    0.0
                end

                total_daily_cap = base_daily_cap + cumul_cap + inv_cap

                @constraint(model,
                    vars.fuel_transport_periodic[fuel_name][r, p] <=
                    total_daily_cap * days_in_p,
                    base_name = "pp_transport_cap_$(fuel_name)_r$(r)_$(p)"
                )
            end
        end
    end

    # =========================================================================
    # III. Shipments per time step
    # =========================================================================
    # A period's route flow leaves its origin evenly over the period's steps
    # and reaches the destination tank transit-delay steps later, net of the
    # transport losses. Fuel shipped in an earlier window and still in transit
    # arrives as scheduled (input.scheduled_arrivals); fuel this window ships
    # in its committed steps but delivers later is carried to the next window
    # by in_transit_arrivals.

    steps_in_period = Dict(p => length(temporal.hours_in_primary_period[p])
                           for p in temporal.primary_period_indices)
    arrivals = Dict{String, Matrix{AffExpr}}()
    departures = Dict{String, Matrix{AffExpr}}()
    for fuel_name in fuel_names
        fuel = fuel_by_name[fuel_name]
        arr = [AffExpr(0.0) for _ in 1:n_nodes, _ in 1:n_hours]
        dep = [AffExpr(0.0) for _ in 1:n_nodes, _ in 1:n_hours]
        if haskey(vars.fuel_transport_periodic, fuel_name)
            flow = vars.fuel_transport_periodic[fuel_name]
            for (r, route) in enumerate(input.transport_routes)
                haskey(route.fuel_params, fuel_name) || continue
                fparams = route.fuel_params[fuel_name]
                delivered = _delivered_fraction(fparams, route)
                delay = _transit_steps(fuel, route, input.step_hours)
                from_ok = 1 <= route.from_node <= n_nodes
                to_ok = 1 <= route.to_node <= n_nodes
                for t in 1:n_hours
                    p = temporal.hour_to_primary_period[t]
                    from_ok && add_to_expression!(dep[route.from_node, t],
                        flow[r, p], 1.0 / steps_in_period[p])
                    s = t - delay
                    if to_ok && s >= 1
                        ps = temporal.hour_to_primary_period[s]
                        add_to_expression!(arr[route.to_node, t],
                            flow[r, ps], delivered / steps_in_period[ps])
                    end
                end
            end
        end
        sched = input.scheduled_arrivals === nothing ? nothing :
                get(input.scheduled_arrivals, fuel_name, nothing)
        if sched !== nothing
            for n in 1:min(n_nodes, size(sched, 1)), t in 1:min(n_hours, size(sched, 2))
                add_to_expression!(arr[n, t], sched[n, t])
            end
        end
        arrivals[fuel_name] = arr
        departures[fuel_name] = dep
    end

    # =========================================================================
    # IV. Hourly Operational Constraints (the tanks)
    # =========================================================================

    # (configured, built earlier, total) tank capacity at node n in investment period inv_p
    function tank_capacity(fuel, fuel_name, n, inv_p)
        base_cap = fuel.storage_capacity[n]
        cumul_cap = _cumulative_capacity(input, "storage", fuel_name, n)
        inv_cap = if haskey(vars.storage_capacity_investment, fuel_name)
            vars.storage_capacity_investment[fuel_name][inv_p, n]
        else
            0.0
        end
        return base_cap, cumul_cap, base_cap + cumul_cap + inv_cap
    end

    for fuel_name in fuel_names
        fuel = fuel_by_name[fuel_name]
        for n in 1:n_nodes
            initial_level = if input.initial_storage_levels !== nothing &&
                               haskey(input.initial_storage_levels, fuel_name) &&
                               n <= length(input.initial_storage_levels[fuel_name])
                input.initial_storage_levels[fuel_name][n]
            else
                fuel.initial_storage_level[n] * fuel.storage_capacity[n]
            end
            @constraint(model,
                vars.fuel_storage_level_hourly[fuel_name][n, 1] == initial_level,
                base_name = "hr_storage_initial_$(fuel_name)_$(n)"
            )
        end
    end

    # Tank balances of the Hydrogen fuel, for the electrolyzer coupling
    h2_balance = Matrix{Any}(nothing, n_nodes, n_hours)

    for t in 1:n_hours
        p = temporal.hour_to_primary_period[t]
        inv_p = get(temporal.primary_to_investment_period, p, 1)

        for fuel_name in fuel_names
            fuel = fuel_by_name[fuel_name]
            storage_eff = _storage_efficiency(input, fuel_name)

            for n in 1:n_nodes
                # Tank balance:
                #   level[t+1] = level[t] + (local supply + arrivals [+ H2]) * eff
                #                - drawn - departures
                # The Hydrogen tank also receives the dispatch electrolyzers'
                # output, added by couple_primary_energy_to_power_system!.
                inflow = AffExpr(0.0)
                add_to_expression!(inflow, vars.fuel_storage_in_hourly[fuel_name][n, t])
                add_to_expression!(inflow, arrivals[fuel_name][n, t])
                balance = @constraint(model,
                    vars.fuel_storage_level_hourly[fuel_name][n, t+1] ==
                    vars.fuel_storage_level_hourly[fuel_name][n, t] +
                    storage_eff * inflow -
                    vars.fuel_storage_out_hourly[fuel_name][n, t] -
                    departures[fuel_name][n, t],
                    base_name = "hr_storage_balance_$(fuel_name)_$(n)_$(t)"
                )
                fuel_name == HYDROGEN_FUEL && (h2_balance[n, t] = balance)

                base_cap, cumul_cap, total_cap = tank_capacity(fuel, fuel_name, n, inv_p)

                # Max dispatch rate: a fraction of the tank capacity per hour,
                # so per step × step_hours. Only where there is a tank: a node
                # without one draws whatever it holds.
                if haskey(input.infrastructure, fuel_name) && (base_cap > 0 || cumul_cap > 0)
                    infra = input.infrastructure[fuel_name]
                    if infra.max_hourly_dispatch_rate >= 0
                        @constraint(model,
                            vars.fuel_storage_out_hourly[fuel_name][n, t] <=
                            infra.max_hourly_dispatch_rate * input.step_hours * total_cap,
                            base_name = "hr_storage_dispatch_rate_$(fuel_name)_$(n)_$(t)"
                        )
                    end
                end

                # Minimum storage level (after each step; the initial level is given)
                if fuel.min_storage_level > 0 && base_cap > 0
                    @constraint(model,
                        vars.fuel_storage_level_hourly[fuel_name][n, t+1] >=
                        fuel.min_storage_level * base_cap,
                        base_name = "hr_storage_min_$(fuel_name)_$(n)_$(t)"
                    )
                end

                # Maximum storage level. A node without any tank capacity has
                # no bound: its tank only buffers fuel within the window.
                if base_cap > 0 || cumul_cap > 0
                    @constraint(model,
                        vars.fuel_storage_level_hourly[fuel_name][n, t+1] <= total_cap,
                        base_name = "hr_storage_max_$(fuel_name)_$(n)_$(t)"
                    )
                end

                # Fuel needed for power + NE consumption <= drawn + shortfall
                fuel_for_power = AffExpr(0.0)
                for (gen_idx, fuel_info) in input.generator_fuel_map
                    gen_fuel_name, _, _, _ = fuel_info
                    if gen_fuel_name == fuel_name
                        add_to_expression!(fuel_for_power,
                            vars.fuel_for_power_hourly[gen_idx][n, t])
                    end
                end

                ne_hourly = AffExpr(0.0)
                for sector in sectors
                    key = (fuel_name, sector)
                    if haskey(vars.non_electric_consumption_hourly, key)
                        add_to_expression!(ne_hourly,
                            vars.non_electric_consumption_hourly[key][n, t])
                    end
                end

                # Hourly NE demand limit per sector: each hour's NE consumption
                # cannot exceed periodic_demand / hours_in_period
                n_hours_in_p = steps_in_period[p]
                for sector in sectors
                    key = (fuel_name, sector)
                    if haskey(vars.non_electric_consumption_hourly, key) && haskey(ne_demand, key)
                        required_periodic = get(get(ne_demand[key], n, Dict()), p, 0.0)
                        if required_periodic > 1e-6 && n_hours_in_p > 0
                            hourly_limit = required_periodic / n_hours_in_p
                            @constraint(model,
                                vars.non_electric_consumption_hourly[key][n, t] <= hourly_limit,
                                base_name = "hr_ne_limit_$(fuel_name)_$(sector)_$(n)_$(t)"
                            )
                        end
                    end
                end

                @constraint(model,
                    vars.fuel_storage_out_hourly[fuel_name][n, t] +
                    vars.fuel_loss_of_supply_hourly[fuel_name][n, t] >=
                    fuel_for_power + ne_hourly,
                    base_name = "hr_demand_coverage_$(fuel_name)_$(n)_$(t)"
                )

                # Emissions from NE consumption
                # Matches Python legacy (primary_energy.py lines 837-844):
                # Conditional formula based on energy_content
                if fuel.energy_content > 1e-6
                    # If energy_content is defined and non-zero: emissions = consumption × energy_content × emission_factor
                    @constraint(model,
                        vars.primary_sector_emissions_hourly[fuel_name][n, t] ==
                        ne_hourly * fuel.energy_content * fuel.emission_factor,
                        base_name = "hr_emissions_$(fuel_name)_$(n)_$(t)"
                    )
                else
                    # If EF is per physical unit or no energy content: emissions = consumption × emission_factor
                    @constraint(model,
                        vars.primary_sector_emissions_hourly[fuel_name][n, t] ==
                        ne_hourly * fuel.emission_factor,
                        base_name = "hr_emissions_direct_$(fuel_name)_$(n)_$(t)"
                    )
                end
            end
        end

        # Total primary emissions
        for n in 1:n_nodes
            total_em = AffExpr(0.0)
            for fuel_name in fuel_names
                add_to_expression!(total_em,
                    vars.primary_sector_emissions_hourly[fuel_name][n, t])
            end
            @constraint(model,
                vars.total_primary_emissions_hourly[n, t] == total_em,
                base_name = "hr_total_emissions_$(n)_$(t)"
            )
        end
    end

    # =========================================================================
    # V. Period ↔ step links
    # =========================================================================

    for p in temporal.primary_period_indices
        hours_in_p = temporal.hours_in_primary_period[p]
        if isempty(hours_in_p)
            continue
        end
        last_hour = hours_in_p[end]
        inv_p = get(temporal.primary_to_investment_period, p, 1)

        for fuel_name in fuel_names
            fuel = fuel_by_name[fuel_name]
            for n in 1:n_nodes
                # The period's local supply is delivered over its steps
                storage_in_sum = AffExpr(0.0)
                for t in hours_in_p
                    add_to_expression!(storage_in_sum,
                        vars.fuel_storage_in_hourly[fuel_name][n, t])
                end
                @constraint(model,
                    storage_in_sum == vars.fuel_supply_periodic[fuel_name][n, p],
                    base_name = "link_supply_storage_in_$(fuel_name)_$(n)_$(p)"
                )

                # Minimum storage level at the period's end (on the total capacity)
                if fuel.min_storage_level > 0
                    base_cap, cumul_cap, total_cap = tank_capacity(fuel, fuel_name, n, inv_p)
                    if base_cap > 0 || cumul_cap > 0
                        @constraint(model,
                            vars.fuel_storage_level_hourly[fuel_name][n, last_hour + 1] >=
                            fuel.min_storage_level * total_cap,
                            base_name = "hr_storage_min_final_$(fuel_name)_$(n)_$(p)"
                        )
                    end
                end
            end

            # Hourly NE consumption adds up to the period's
            for sector in sectors
                key = (fuel_name, sector)
                if haskey(vars.non_electric_consumption_hourly, key) &&
                   haskey(vars.non_electric_consumption_periodic, key)
                    for n in 1:n_nodes
                        hourly_sum = AffExpr(0.0)
                        for t in hours_in_p
                            add_to_expression!(hourly_sum,
                                vars.non_electric_consumption_hourly[key][n, t])
                        end
                        @constraint(model,
                            hourly_sum == vars.non_electric_consumption_periodic[key][n, p],
                            base_name = "link_ne_$(fuel_name)_$(sector)_$(n)_$(p)"
                        )
                    end
                end
            end
        end

        # Development mode: the window ends with every tank at least 10 % full
        # (no cyclic storage condition).
        if p == temporal.primary_period_indices[end] && input.mode == "development"
            for fuel_name in fuel_names
                haskey(input.infrastructure, fuel_name) || continue
                fuel = fuel_by_name[fuel_name]
                for n in 1:n_nodes
                    base_cap, _, total_cap = tank_capacity(fuel, fuel_name, n, inv_p)
                    if base_cap > 0
                        min_fill = 0.1
                        @constraint(model,
                            vars.fuel_storage_level_hourly[fuel_name][n, last_hour + 1] >=
                            min_fill * total_cap,
                            base_name = "hr_storage_min_last_$(fuel_name)_$(n)"
                        )
                    end
                end
            end
        end
    end

    if HYDROGEN_FUEL in fuel_names
        model.ext[:pe_hydrogen_balance] = (h2_balance, _storage_efficiency(input, HYDROGEN_FUEL))
    end
end

# =============================================================================
# Objective Terms
# =============================================================================

"""
    get_primary_energy_objective_terms(vars, input, temporal, adjusted_prices)

Build a granular decomposition of the primary-energy objective contribution
keyed by category symbol. The caller (`integrate_with_power_system`) sums
the dict's values into the PowerSystem objective AND merges the dict into
`model.ext[:cost_expressions]` so each sub-cost lands in CostBreakdown.

Buckets:
  :pe_investment_cost     — transport + storage infrastructure annualised CAPEX
  :pe_supply_cost         — base_price + import_cost × periodic supply, plus the tank drawdown (start − end level) at base_price
  :pe_loss_cost           — fuel shortfall (hourly) and unmet non-electric demand penalties
  :pe_excess_cost         — excess-supply penalty
  :pe_transport_cost      — route transport cost × periodic flow
"""
function get_primary_energy_objective_terms(
    vars::PrimaryEnergyVariables,
    input::PrimaryEnergyInput,
    temporal::TemporalMapping,
    adjusted_prices::Dict{String, Float64}
)::Dict{Symbol, AffExpr}
    pe_investment_cost     = AffExpr(0.0)
    pe_supply_cost         = AffExpr(0.0)
    pe_loss_cost           = AffExpr(0.0)
    pe_excess_cost         = AffExpr(0.0)
    pe_transport_cost      = AffExpr(0.0)

    fuel_names = [f.name for f in input.fuels]
    fuel_by_name = Dict(f.name => f for f in input.fuels)
    n_nodes = input.num_nodes

    loss_penalty = input.loss_of_fuel_supply_penalty

    # =========================================================================
    # Investment Costs
    # =========================================================================

    # Annuities are annual: charge the window's share of the year, like the
    # dispatch's own investment costs (the capacity carries over to later windows).
    window_fraction = input.hours * input.step_hours / Float64(hours_in_year(input.year))
    if input.mode in ["development", "economic_dispatch"]
        for inv_p in temporal.investment_period_indices
            for fuel_name in fuel_names
                if haskey(input.infrastructure, fuel_name)
                    infra = input.infrastructure[fuel_name]

                    # Capital recovery factor for transport
                    crf_transport = if infra.lifetime_transport > 0 && input.discount_rate > 0
                        r = input.discount_rate
                        n = infra.lifetime_transport
                        (r * (1 + r)^n) / ((1 + r)^n - 1)
                    else
                        1.0 / max(infra.lifetime_transport, 1.0)
                    end

                    # Capital recovery factor for storage
                    crf_storage = if infra.lifetime_storage > 0 && input.discount_rate > 0
                        r = input.discount_rate
                        n = infra.lifetime_storage
                        (r * (1 + r)^n) / ((1 + r)^n - 1)
                    else
                        1.0 / max(infra.lifetime_storage, 1.0)
                    end

                    # Transport investment cost (route-based)
                    if haskey(vars.transport_capacity_investment, fuel_name)
                        for (r, route) in enumerate(input.transport_routes)
                            add_to_expression!(pe_investment_cost,
                                vars.transport_capacity_investment[fuel_name][inv_p, r],
                                infra.transport_investment_cost * route.distance_km * crf_transport * window_fraction)
                        end
                    end

                    # Storage investment cost
                    if haskey(vars.storage_capacity_investment, fuel_name)
                        for n in 1:n_nodes
                            add_to_expression!(pe_investment_cost,
                                vars.storage_capacity_investment[fuel_name][inv_p, n],
                                infra.storage_investment_cost * crf_storage * window_fraction)
                        end
                    end
                end
            end
        end
    end

    # =========================================================================
    # Operational Costs (Primary Period)
    # =========================================================================

    for p in temporal.primary_period_indices
        for fuel_name in fuel_names
            fuel = fuel_by_name[fuel_name]
            base_price = adjusted_prices[fuel_name]

            for n in 1:n_nodes
                # Supply cost
                import_cost = fuel.import_cost[n]
                add_to_expression!(pe_supply_cost,
                    vars.fuel_supply_periodic[fuel_name][n, p],
                    base_price + import_cost)

                # Excess supply penalty
                add_to_expression!(pe_excess_cost,
                    vars.fuel_excess_supply_periodic[fuel_name][n, p],
                    loss_penalty)
            end

            # Transport cost (route-based, outside node loop to avoid counting N times)
            for (r, route) in enumerate(input.transport_routes)
                if !haskey(route.fuel_params, fuel_name)
                    continue
                end
                fparams = route.fuel_params[fuel_name]
                add_to_expression!(pe_transport_cost,
                    vars.fuel_transport_periodic[fuel_name][r, p],
                    fparams.transport_cost * route.distance_km)
            end
        end
    end

    # Fuel drawn from the tanks is valued at the fuel price too (stock at the
    # window's start minus at its end), so burning stored fuel is not free.
    # With that, the fuel cost is the price of what is used whether it was
    # bought now or before; the dispatch drops the coupled units' fuel_cost.
    for fuel_name in fuel_names
        haskey(vars.fuel_storage_level_hourly, fuel_name) || continue
        level = vars.fuel_storage_level_hourly[fuel_name]
        price = adjusted_prices[fuel_name]
        for n in 1:n_nodes
            add_to_expression!(pe_supply_cost, level[n, 1], price)
            add_to_expression!(pe_supply_cost, level[n, size(level, 2)], -price)
        end
    end

    # =========================================================================
    # Hourly Loss Penalties
    # =========================================================================

    for fuel_name in fuel_names
        for n in 1:n_nodes
            for t in 1:input.hours
                add_to_expression!(pe_loss_cost,
                    vars.fuel_loss_of_supply_hourly[fuel_name][n, t],
                    loss_penalty)
            end
        end
    end
    for short in values(vars.ne_demand_shortfall_periodic)
        for idx in eachindex(short)
            add_to_expression!(pe_loss_cost, short[idx], input.non_electric_demand_loss_penalty)
        end
    end

    return Dict{Symbol, AffExpr}(
        :pe_investment_cost     => pe_investment_cost,
        :pe_supply_cost         => pe_supply_cost,
        :pe_loss_cost           => pe_loss_cost,
        :pe_excess_cost         => pe_excess_cost,
        :pe_transport_cost      => pe_transport_cost,
    )
end

# =============================================================================
# Integration with PowerSystem
# =============================================================================

"""
    couple_primary_energy_to_power_system!(model, pe_vars, ps_vars, input;
        bus_to_node, resolution_hours, generators, electrolyzers)

Add coupling constraints between primary energy and power system.

- A fuel-mapped unit's output at a node, summed over the node's buses, is
  limited by the fuel it burns there:
  `Σ_b gen_output[g,b,t] × resolution_hours ≤ fuel_for_power[g][node,t] × MWhe/unit`.
- The dispatch electrolyzers' hydrogen (`electrolyzers`, the
  PowerSystemInput's) flows into the `Hydrogen` tank of their node, in tonnes:
  `electrolyzer_power × resolution_hours × electrolyzer_h2_per_mwh / 1000`.
"""
function couple_primary_energy_to_power_system!(
    model::Model,
    pe_vars::PrimaryEnergyVariables,
    ps_vars::PowerSystemVariables,
    input::PrimaryEnergyInput;
    bus_to_node::Vector{Int} = Int[],
    resolution_hours::Float64 = 1.0,
    generators = nothing,
    electrolyzers = nothing
)
    NUMERICAL_TOLERANCE = 1e-6
    n_nodes = input.num_nodes
    n_hours = input.hours

    # The fuel a coupled unit burns is bought here (fuel supply × price), so
    # the dispatch must not also charge the unit's fuel_cost per MWh: drop it
    # from the objective and the breakdown. Units with a cost curve keep their
    # curve (it is a bid, not only fuel). `generators` = the PowerSystemInput's.
    objective = generators === nothing ? nothing : objective_function(model)
    ce = get(model.ext, :cost_expressions, nothing)

    # If bus_to_node is empty, assume identity mapping (bus == node)
    use_b2n = !isempty(bus_to_node)
    node_of(b) = use_b2n ? bus_to_node[b] : b

    n_coupling_constraints = 0

    # @debug not @info: this function runs once per window — under
    # rolling horizon that's 60+ windows × 25 years of identical setup
    # noise. Visible in verbose/debug log levels.
    @debug "PE Coupling: $(length(input.generator_fuel_map)) fuel-mapped generators, " *
          "$(length(ps_vars.buses_of_gen)) PS generators, " *
          "use_b2n=$use_b2n, n_nodes=$n_nodes, n_hours=$n_hours, resolution=$resolution_hours h"

    for (gen_idx, fuel_info) in input.generator_fuel_map
        fuel_name, mwhe_per_unit, _, _ = fuel_info

        # Skip generators with near-zero conversion
        if mwhe_per_unit <= NUMERICAL_TOLERANCE
            continue
        end

        # Skip generators not present in power system
        if gen_idx > length(ps_vars.buses_of_gen)
            # @debug not @info: this fires once per generator inside
            # a coupling loop — N gens × W windows × Y years of noise
            # otherwise. Aggregate stats are reported once below.
            @debug "PE Coupling: gen $gen_idx ($fuel_name) skipped: gen_idx > n_ps_gen ($(length(ps_vars.buses_of_gen)))"
            continue
        end
        haskey(pe_vars.fuel_for_power_hourly, gen_idx) || continue

        # The unit's buses grouped by node: the node's fuel covers the output
        # at all of them together (one constraint per bus let each bus burn
        # the same fuel again).
        buses_by_node = Dict{Int, Vector{Int}}()
        for b in ps_vars.buses_of_gen[gen_idx]
            node = node_of(b)
            1 <= node <= n_nodes || continue
            push!(get!(buses_by_node, node, Int[]), b)
        end

        for (node, buses) in buses_by_node
            for t in 1:n_hours
                # gen_output is in MW; a step spans resolution_hours, so the
                # energy is gen_output × resolution_hours (MWh) and the fuel it
                # needs is energy / mwhe_per_unit (fuel_for_power, units/step).
                output = AffExpr(0.0)
                for b in buses
                    add_to_expression!(output, resolution_hours, ps_vars.gen_output[gen_idx, b, t])
                end
                @constraint(model,
                    output <= pe_vars.fuel_for_power_hourly[gen_idx][node, t] * mwhe_per_unit,
                    base_name = "pe_coupling_gen$(gen_idx)_n$(node)_$(t)"
                )
                n_coupling_constraints += 1
            end

            objective === nothing && continue
            for b in buses
                haskey(ps_vars.gen_seg_output, (gen_idx, b)) && continue
                fc = generators[gen_idx].fuel_cost[b]
                fc != 0 || continue
                for t in 1:n_hours
                    v = ps_vars.gen_output[gen_idx, b, t]
                    set_objective_coefficient(model, v,
                        coefficient(objective, v) - resolution_hours * fc)
                    ce === nothing || add_to_expression!(ce[:fuel_cost], -fc, v)
                end
            end
        end
    end

    couple_electrolyzers_to_hydrogen_tank!(model, ps_vars, input, electrolyzers;
        node_of = node_of, resolution_hours = resolution_hours)

    # @debug not @info: per-window setup noise (see comment above).
    @debug "PE Coupling: Created $n_coupling_constraints constraints (resolution=$(resolution_hours)h)"
end

"""
    couple_electrolyzers_to_hydrogen_tank!(model, ps_vars, input, electrolyzers;
        node_of, resolution_hours)

Add each dispatch electrolyzer's hydrogen to the inflow of the `Hydrogen`
tank at its node (the balance built by `add_primary_energy_constraints!`,
scaled by the tank's storage efficiency like every inflow). Without a
`Hydrogen` fuel in the primary-energy model the electrolyzers stay a plain
load, as in a run without primary energy.
"""
function couple_electrolyzers_to_hydrogen_tank!(model::Model, ps_vars::PowerSystemVariables,
                                                input::PrimaryEnergyInput, electrolyzers;
                                                node_of = identity,
                                                resolution_hours::Float64 = 1.0)
    (electrolyzers === nothing || isempty(electrolyzers)) && return
    ps_vars.electrolyzer_power === nothing && return
    h2 = get(model.ext, :pe_hydrogen_balance, nothing)
    if h2 === nothing
        @debug "PE Coupling: no $(HYDROGEN_FUEL) fuel; electrolyzer hydrogen is not stored"
        return
    end
    balance, eff = h2
    n_hours = min(input.hours, size(ps_vars.electrolyzer_power, 3))
    for (e, ec) in enumerate(electrolyzers), b in 1:size(ps_vars.electrolyzer_power, 2)
        node = node_of(b)
        1 <= node <= size(balance, 1) || continue
        # t of hydrogen per MW of electrolyzer power in one step
        t_per_mw = resolution_hours * electrolyzer_h2_per_mwh(ec, b) / KG_PER_TONNE
        t_per_mw > 0 || continue
        for t in 1:n_hours
            con = balance[node, t]
            con === nothing && continue
            # balance: level[t+1] − level[t] − eff × inflow + … == 0
            set_normalized_coefficient(con, ps_vars.electrolyzer_power[e, b, t], -eff * t_per_mw)
        end
    end
end

"""
    in_transit_arrivals(vars, input, temporal, k) -> Dict{String, Matrix{Float64}}

Fuel still on its way after the window's first `k` steps (a rolling horizon's
committed part): shipments that left in steps 1..k and arrive after step k,
and scheduled arrivals of this window due after step k. Returns
fuel => [node, s], the quantity reaching the node's tank in step s of a window
that starts after step k (net of transport losses), for the next window's
`scheduled_arrivals`. Fuels with nothing in transit are left out.
"""
function in_transit_arrivals(vars::PrimaryEnergyVariables, input::PrimaryEnergyInput,
                             temporal::TemporalMapping, k::Int)
    n_nodes = input.num_nodes
    k = clamp(k, 0, input.hours)
    out = Dict{String, Matrix{Float64}}()
    for fuel in input.fuels
        name = fuel.name
        pending = Dict{Tuple{Int, Int}, Float64}()   # (node, step after k) => quantity
        if haskey(vars.fuel_transport_periodic, name)
            flow = vars.fuel_transport_periodic[name]
            for (r, route) in enumerate(input.transport_routes)
                haskey(route.fuel_params, name) || continue
                1 <= route.to_node <= n_nodes || continue
                delivered = _delivered_fraction(route.fuel_params[name], route)
                delay = _transit_steps(fuel, route, input.step_hours)
                for t in max(1, k - delay + 1):k     # left by step k, arrives after it
                    p = temporal.hour_to_primary_period[t]
                    q = value(flow[r, p]) * delivered /
                        length(temporal.hours_in_primary_period[p])
                    q > 1e-9 || continue
                    key = (route.to_node, t + delay - k)
                    pending[key] = get(pending, key, 0.0) + q
                end
            end
        end
        sched = input.scheduled_arrivals === nothing ? nothing :
                get(input.scheduled_arrivals, name, nothing)
        if sched !== nothing
            for n in 1:min(n_nodes, size(sched, 1)), t in (k + 1):size(sched, 2)
                sched[n, t] > 0 || continue
                key = (n, t - k)
                pending[key] = get(pending, key, 0.0) + sched[n, t]
            end
        end
        isempty(pending) && continue
        mat = zeros(n_nodes, maximum(last, keys(pending)))
        for ((n, s), q) in pending
            mat[n, s] += q
        end
        out[name] = mat
    end
    return out
end

# =============================================================================
# Solution Extraction
# =============================================================================

"""
    extract_primary_energy_solution(model, vars, input, temporal)

Extract solution values from the primary energy model.
"""
function extract_primary_energy_solution(
    model::Model,
    vars::PrimaryEnergyVariables,
    input::PrimaryEnergyInput,
    temporal::TemporalMapping;
    adjusted_prices::Dict{String, Float64} = Dict{String, Float64}()
)::PrimaryEnergyResult
    fuel_names = [f.name for f in input.fuels]
    n_nodes = input.num_nodes
    n_primary = temporal.num_primary_periods
    n_invest = temporal.num_investment_periods

    # Investment results
    n_routes = length(input.transport_routes)
    transport_inv = Dict{String, Matrix{Float64}}()
    storage_inv = Dict{String, Matrix{Float64}}()

    for fuel_name in fuel_names
        if haskey(vars.transport_capacity_investment, fuel_name)
            transport_inv[fuel_name] = Matrix{Float64}(undef, n_invest, n_routes)
            for inv_p in 1:n_invest
                for r in 1:n_routes
                    transport_inv[fuel_name][inv_p, r] =
                        value(vars.transport_capacity_investment[fuel_name][inv_p, r])
                end
            end
        end

        if haskey(vars.storage_capacity_investment, fuel_name)
            storage_inv[fuel_name] = Matrix{Float64}(undef, n_invest, n_nodes)
            for inv_p in 1:n_invest
                for n in 1:n_nodes
                    storage_inv[fuel_name][inv_p, n] =
                        value(vars.storage_capacity_investment[fuel_name][inv_p, n])
                end
            end
        end
    end

    # Periodic aggregates
    total_supply = Dict{String, Vector{Float64}}()
    total_ne_satisfied = Dict{String, Vector{Float64}}()
    total_loss = Dict{String, Vector{Float64}}()

    for fuel_name in fuel_names
        total_supply[fuel_name] = zeros(n_primary)
        total_ne_satisfied[fuel_name] = zeros(n_primary)
        total_loss[fuel_name] = zeros(n_primary)

        for p in 1:n_primary
            for n in 1:n_nodes
                total_supply[fuel_name][p] +=
                    value(vars.fuel_supply_periodic[fuel_name][n, p])
            end
            # Hourly fuel shortfall aggregated into primary periods
            if haskey(vars.fuel_loss_of_supply_hourly, fuel_name)
                hours_in_p = temporal.hours_in_primary_period[p]
                for t in hours_in_p, n in 1:n_nodes
                    total_loss[fuel_name][p] +=
                        value(vars.fuel_loss_of_supply_hourly[fuel_name][n, t])
                end
            end
        end

        # Aggregate non-electric consumption across sectors for this fuel
        for ((f, sector), ne_var) in vars.non_electric_consumption_periodic
            if f == fuel_name
                for p in 1:n_primary
                    for n in 1:n_nodes
                        total_ne_satisfied[fuel_name][p] +=
                            value(ne_var[n, p])
                    end
                end
            end
        end
    end

    # Final tank levels for carry-over
    final_storage = Dict{String, Vector{Float64}}()
    for fuel_name in fuel_names
        level = vars.fuel_storage_level_hourly[fuel_name]
        final_storage[fuel_name] = [value(level[n, end]) for n in 1:n_nodes]
    end

    # Per-route transport flows
    transport_flows = Dict{String, Matrix{Float64}}()
    for fuel_name in fuel_names
        if haskey(vars.fuel_transport_periodic, fuel_name) && n_routes > 0
            transport_flows[fuel_name] = Matrix{Float64}(undef, n_routes, n_primary)
            for r in 1:n_routes, p in 1:n_primary
                transport_flows[fuel_name][r, p] = value(vars.fuel_transport_periodic[fuel_name][r, p])
            end
        end
    end

    # Calculate actual costs from solved variables
    total_fuel_cost = 0.0
    total_transport_cost = 0.0
    total_loss_penalty = 0.0

    for fuel in input.fuels
        fuel_name = fuel.name
        price = get(adjusted_prices, fuel_name, fuel.price_base)

        # Fuel supply cost
        if haskey(vars.fuel_supply_periodic, fuel_name)
            for p in 1:n_primary, n in 1:n_nodes
                total_fuel_cost += value(vars.fuel_supply_periodic[fuel_name][n, p]) * price
            end
        end

        # Transport cost (route-based)
        if haskey(vars.fuel_transport_periodic, fuel_name)
            for p in 1:n_primary
                for (r, route) in enumerate(input.transport_routes)
                    if !haskey(route.fuel_params, fuel_name)
                        continue
                    end
                    flow_val = value(vars.fuel_transport_periodic[fuel_name][r, p])
                    if flow_val > 1e-6
                        fparams = route.fuel_params[fuel_name]
                        total_transport_cost += flow_val * fparams.transport_cost * route.distance_km
                    end
                end
            end
        end

        # Fuel shortfall penalty (hourly)
        if haskey(vars.fuel_loss_of_supply_hourly, fuel_name)
            for t in 1:input.hours, n in 1:n_nodes
                total_loss_penalty += value(vars.fuel_loss_of_supply_hourly[fuel_name][n, t]) *
                    input.loss_of_fuel_supply_penalty
            end
        end
    end

    return PrimaryEnergyResult(
        transport_inv,
        storage_inv,
        total_supply,
        total_ne_satisfied,
        total_loss,
        final_storage,
        transport_flows,
        total_fuel_cost,
        total_transport_cost,
        total_loss_penalty
    )
end

"""
    extract_hourly_results(vars, input, temporal)

Extract hourly operational results from the solved primary energy model.
Mirrors Python legacy `get_hourly_results()` (primary_energy.py line 1263).

Returns Dict with:
- "fuel_for_power_hourly" → Dict{Int, Matrix{Float64}} (gen_idx → [node, hour])
- "non_electric_consumption_hourly" → Dict{String, Dict{String, Matrix{Float64}}} (fuel → sector → [node, hour])
- "fuel_supply_periodic" → Dict{String, Matrix{Float64}} (fuel → [node, period])
- "fuel_storage_level_hourly" → Dict{String, Matrix{Float64}} (fuel → [node, hour+1])
"""
function extract_hourly_results(
    vars::PrimaryEnergyVariables,
    input::PrimaryEnergyInput,
    temporal::TemporalMapping
)::Dict{String, Any}
    n_nodes = input.num_nodes
    hours = input.hours
    n_primary = temporal.num_primary_periods
    results = Dict{String, Any}()

    # fuel_for_power_hourly: gen_idx → [node, hour] values
    fuel_power = Dict{Int, Matrix{Float64}}()
    for (gen_idx, mat) in vars.fuel_for_power_hourly
        fuel_power[gen_idx] = Matrix{Float64}(undef, n_nodes, hours)
        for n in 1:n_nodes, t in 1:hours
            fuel_power[gen_idx][n, t] = value(mat[n, t])
        end
    end
    results["fuel_for_power_hourly"] = fuel_power

    # non_electric_consumption_hourly: fuel → sector → [node, hour]
    ne_hourly = Dict{String, Dict{String, Matrix{Float64}}}()
    for ((fuel_id, sector), mat) in vars.non_electric_consumption_hourly
        if !haskey(ne_hourly, fuel_id)
            ne_hourly[fuel_id] = Dict{String, Matrix{Float64}}()
        end
        ne_hourly[fuel_id][sector] = Matrix{Float64}(undef, n_nodes, hours)
        for n in 1:n_nodes, t in 1:hours
            ne_hourly[fuel_id][sector][n, t] = value(mat[n, t])
        end
    end
    results["non_electric_consumption_hourly"] = ne_hourly

    # fuel_supply_periodic: fuel → [node, period]
    supply_periodic = Dict{String, Matrix{Float64}}()
    for (fuel_name, mat) in vars.fuel_supply_periodic
        supply_periodic[fuel_name] = Matrix{Float64}(undef, n_nodes, n_primary)
        for n in 1:n_nodes, p in 1:n_primary
            supply_periodic[fuel_name][n, p] = value(mat[n, p])
        end
    end
    results["fuel_supply_periodic"] = supply_periodic

    # fuel_storage_level_hourly: fuel → [node, hour+1]
    storage_hourly = Dict{String, Matrix{Float64}}()
    for (fuel_name, mat) in vars.fuel_storage_level_hourly
        n_steps = hours + 1  # hour 0 (initial) through hour H
        storage_hourly[fuel_name] = Matrix{Float64}(undef, n_nodes, n_steps)
        for n in 1:n_nodes, t in 1:n_steps
            storage_hourly[fuel_name][n, t] = value(mat[n, t])
        end
    end
    results["fuel_storage_level_hourly"] = storage_hourly

    return results
end

# =============================================================================
# Main API
# =============================================================================

"""
    create_primary_energy_model(model, input)

Create the primary energy optimization model components.

# Arguments
- `model::Model`: JuMP model to add variables and constraints to
- `input::PrimaryEnergyInput`: Primary energy configuration

# Returns
- `vars::PrimaryEnergyVariables`: Variable container
- `temporal::TemporalMapping`: Temporal period mapping
"""
function create_primary_energy_model(
    model::Model,
    input::PrimaryEnergyInput
)
    # The dispatch electrolyzers feed the Hydrogen tank
    # (couple_primary_energy_to_power_system!); the old inputs are gone.
    if input.h2_production_hourly !== nothing || input.electrolyzer_config !== nothing
        throw(ArgumentError("PrimaryEnergyInput: h2_production_hourly and " *
            "electrolyzer_config are no longer used and must be nothing; the " *
            "dispatch electrolyzers feed the $(HYDROGEN_FUEL) tank through " *
            "couple_primary_energy_to_power_system!(...; electrolyzers)"))
    end

    # Create temporal mapping
    temporal = create_temporal_mapping(
        input.hours,
        input.primary_energy_resolution,
        input.investment_resolution
    )

    # Prepare input data
    adjusted_prices, max_supply, ne_demand = prepare_fuel_data(input, temporal)

    # Create variables
    vars = build_primary_energy_variables!(model, input, temporal)

    # Add constraints
    add_primary_energy_constraints!(model, vars, input, temporal,
        adjusted_prices, max_supply, ne_demand)

    return vars, temporal, adjusted_prices
end
