"""
master_fuel.jl - Fuel balance, fuel infrastructure and electrolyzers in the
master problem (#89).

When primary energy is on, the master builds the dispatch's own fuel model
(`primary_energy.jl`) on each representative day (or TSAM period): the same
tanks, supply limits, route flows, non-electric demand, penalties and fuel
prices, so master and dispatch charge the same fuel costs. On a day:
- the tank level is cyclic (the day is not chronological), so the tank
  drawdown is worth nothing;
- route shipments arrive the same day (no transit delay);
- the supply-limit period is the day, the dispatch's default 24 h period.
Fuel-mapped generators and candidate technologies burn tank fuel and, as in
the dispatch, do not also pay their flat `fuel_cost` (a unit with a cost
curve keeps its curve). Electrolyzers draw power at their bus (capped by
existing + built capacity) and fill the `Hydrogen` tank.

The master is the only model that builds fuel storage (per fuel and node),
route transport capacity (per fuel and route) and electrolyzer power (per
bus); the dispatch receives them as existing capacity, year by year.

Units: fuel quantities are amounts per step, so a day's fuel cost is scaled
to the year by days_in_year / representative days (TSAM: the period weight),
without the ×Δt applied to MW terms. Electrolyzer power terms carry Δt here.
"""

using JuMP: @variable, @constraint, AffExpr, VariableRef, value, add_to_expression!

const MasterFuelKey = Tuple{Symbol, String, Int}

has_master_fuel(input::MasterProblemInput) = input.primary_energy !== nothing

# Room left to build storage of `fuel_name` at node `n`: the configured tank
# × storage_expansion_limit, less what earlier years built. Same limit the
# dispatch used to apply.
function master_fuel_storage_limit(pe::PrimaryEnergyInput, fuel_name::String, n::Int)
    haskey(pe.infrastructure, fuel_name) || return 0.0
    idx = findfirst(f -> f.name == fuel_name, pe.fuels)
    idx === nothing && return 0.0
    cap = pe.fuels[idx].storage_capacity
    n <= length(cap) || return 0.0
    lim = cap[n] * pe.infrastructure[fuel_name].storage_expansion_limit
    return max(0.0, lim - _cumulative_capacity(pe, "storage", fuel_name, n))
end

# Room left on route r for `fuel_name`: route capacity × transport_expansion_limit
function master_fuel_transport_limit(pe::PrimaryEnergyInput, fuel_name::String, r::Int)
    haskey(pe.infrastructure, fuel_name) || return 0.0
    route = pe.transport_routes[r]
    haskey(route.fuel_params, fuel_name) || return 0.0
    lim = route.fuel_params[fuel_name].capacity *
          pe.infrastructure[fuel_name].transport_expansion_limit
    return max(0.0, lim - _cumulative_capacity(pe, "transport", fuel_name, r))
end

_elz_value(v::AbstractVector, b::Int) = b <= length(v) ? v[b] : 0.0

"""
    master_fuel_keys(input) -> Vector{MasterFuelKey}

The capacities the master can build: `(:storage, fuel, node)`,
`(:transport, fuel, route)` and `(:electrolyzer, string(e), bus)`.
"""
function master_fuel_keys(input::MasterProblemInput)
    keys = MasterFuelKey[]
    has_master_fuel(input) || return keys
    mf = input.primary_energy
    pe = mf.pe
    for f in pe.fuels
        for n in 1:pe.num_nodes
            master_fuel_storage_limit(pe, f.name, n) > 0 && push!(keys, (:storage, f.name, n))
        end
        for r in eachindex(pe.transport_routes)
            master_fuel_transport_limit(pe, f.name, r) > 0 && push!(keys, (:transport, f.name, r))
        end
    end
    for (e, ec) in enumerate(mf.electrolyzers), b in eachindex(ec.invest_max_power)
        ec.invest_max_power[b] > 0 && push!(keys, (:electrolyzer, string(e), b))
    end
    return keys
end

"""
    build_master_fuel_variables!(model, input, ypp) -> Union{Nothing, MasterFuelVariables}

Investment variables for fuel storage, route transport and electrolyzers at
each investment-period start year, bounded so the builds over the horizon
stay within the expansion limits (`invest_max_power` for electrolyzers).
"""
function build_master_fuel_variables!(model::Model, input::MasterProblemInput, ypp::Int)
    has_master_fuel(input) || return nothing
    mf = input.primary_energy
    pe = mf.pe
    inv_years = collect(1:ypp:length(input.years))
    n_routes = length(pe.transport_routes)
    storage = Dict{Int, Dict{String, Vector{VariableRef}}}()
    transport = Dict{Int, Dict{String, Vector{VariableRef}}}()
    electrolyzer = Dict{Int, Dict{Int, Vector{VariableRef}}}()
    for y in inv_years
        storage[y] = Dict{String, Vector{VariableRef}}()
        transport[y] = Dict{String, Vector{VariableRef}}()
        electrolyzer[y] = Dict{Int, Vector{VariableRef}}()
        for f in pe.fuels
            storage[y][f.name] = @variable(model, [n = 1:pe.num_nodes], lower_bound = 0,
                upper_bound = master_fuel_storage_limit(pe, f.name, n),
                base_name = "fuel_storage_inv_y$(y)_$(f.name)")
            transport[y][f.name] = @variable(model, [r = 1:n_routes], lower_bound = 0,
                upper_bound = master_fuel_transport_limit(pe, f.name, r),
                base_name = "fuel_transport_inv_y$(y)_$(f.name)")
        end
        for (e, ec) in enumerate(mf.electrolyzers)
            electrolyzer[y][e] = @variable(model, [b = 1:length(ec.invest_max_power)],
                lower_bound = 0, upper_bound = max(0.0, ec.invest_max_power[b]),
                base_name = "elz_inv_y$(y)_e$(e)")
        end
    end
    # The limits hold for the builds of the whole horizon
    if length(inv_years) > 1
        for (kind, by_year) in (("storage", storage), ("transport", transport))
            for f in pe.fuels, i in eachindex(by_year[inv_years[1]][f.name])
                lim = kind == "storage" ? master_fuel_storage_limit(pe, f.name, i) :
                                          master_fuel_transport_limit(pe, f.name, i)
                @constraint(model, sum(by_year[y][f.name][i] for y in inv_years) <= lim,
                    base_name = "fuel_$(kind)_inv_total_$(f.name)_$(i)")
            end
        end
        for (e, ec) in enumerate(mf.electrolyzers), b in eachindex(ec.invest_max_power)
            @constraint(model, sum(electrolyzer[y][e][b] for y in inv_years) <=
                               max(0.0, ec.invest_max_power[b]),
                base_name = "elz_inv_total_e$(e)_b$(b)")
        end
    end
    return MasterFuelVariables(storage, transport, electrolyzer)
end

"""
    master_fuel_extra_expr(fv, input, year_idx, key) -> AffExpr

Capacity the master has built for `key` and that is in service in year
`year_idx`: fuel storage and transport never retire (as in the dispatch), an
electrolyzer build retires after its `life_time` (0 = never).
"""
function master_fuel_extra_expr(fv::Union{Nothing, MasterFuelVariables},
                                input::MasterProblemInput, year_idx::Int,
                                key::MasterFuelKey)
    expr = AffExpr(0.0)
    fv === nothing && return expr
    kind, name, i = key
    if kind === :storage || kind === :transport
        by_year = kind === :storage ? fv.storage : fv.transport
        for (y, by_fuel) in by_year
            y <= year_idx || continue
            v = get(by_fuel, name, nothing)
            v !== nothing && i <= length(v) && add_to_expression!(expr, v[i])
        end
    elseif kind === :electrolyzer
        e = parse(Int, name)
        life = _elz_value(input.primary_energy.electrolyzers[e].life_time, i)
        for (y, by_elz) in fv.electrolyzer
            y <= year_idx || continue
            (life <= 0 || year_idx - y < life) || continue
            v = by_elz[e]
            i <= length(v) && add_to_expression!(expr, v[i])
        end
    end
    return expr
end

"""
    master_fuel_extra_values(fv, input, year_idx; val=value) -> Dict{MasterFuelKey, Float64}

Solved value of `master_fuel_extra_expr` for every buildable capacity
(Benders), with each variable's value `val(v)`.
"""
master_fuel_extra_values(fv, input::MasterProblemInput, year_idx::Int;
                         val::Function = value) =
    Dict{MasterFuelKey, Float64}(k => value(val, master_fuel_extra_expr(fv, input, year_idx, k))
                                 for k in master_fuel_keys(input))

"""
    master_electrolyzer_power_vars(model, input, prefix, hours)

Electrolyzer power [electrolyzer, bus, step] of a representative day, or
`nothing` without the fuel block or electrolyzers. The day's power-balance
builders subtract it as load (`electrolyzer_load`).
"""
function master_electrolyzer_power_vars(model::Model, input::MasterProblemInput,
                                        prefix::String, hours::Int)
    has_master_fuel(input) || return nothing
    elz = input.primary_energy.electrolyzers
    isempty(elz) && return nothing
    return @variable(model, [1:length(elz), 1:input.network.num_buses, 1:hours],
        lower_bound = 0, base_name = "$(prefix)_elz")
end

"""
    master_fuel_day_input(input, year_idx, start_hour, hours) -> PrimaryEnergyInput

The fuel model of one representative day: the system's primary-energy input
for year `year_idx`, `hours` steps of `temporal_resolution_hours` starting at
master step `start_hour`, one supply period.

Routes have no transit delay here: the day is cyclic, so a shipment that
left in its last steps would arrive after the day ended and be lost (paid
for, never delivered). With one supply period the flow leaves evenly over
the day, so a delay wrapped around the cyclic day would deliver the same
even arrivals as no delay at all.
"""
function master_fuel_day_input(input::MasterProblemInput, year_idx::Int,
                               start_hour::Int, hours::Int)
    pe = input.primary_energy.pe
    tres = max(1, input.temporal_resolution_hours)
    steps_per_year = max(1, input.hours_per_year[year_idx] ÷ tres)
    return with_fields(pe;
        fuels = [with_fields(f; transport_transit_days_per_100km = 0.0) for f in pe.fuels],
        year = input.years[year_idx],
        hours = hours,
        primary_energy_resolution = hours,
        investment_resolution = hours,
        mode = "master",
        initial_storage_levels = nothing,
        scheduled_arrivals = nothing,
        step_hours = tres,
        year_hour_offset = mod(start_hour - 1, steps_per_year) * tres)
end

"""
    add_master_fuel_day!(model, ps_vars, input, year_idx, start_hour, hours,
                         tech_output; extra) -> AffExpr

Build the fuel block of one representative day on `model` and return its
cost in \$ for the day (not yet scaled to the year): fuel bought, route
transport, fuel shortfall/excess and unmet non-electric demand, the
electrolyzers' variable and water cost, less the flat `fuel_cost` of the
fuel-coupled units that `day_operational_cost` charged. `extra(key)` gives
the capacity built for a `MasterFuelKey` (an expression of the investment
variables in the master, a fixed copy in a Benders subproblem). Returns 0
without primary energy.
"""
function add_master_fuel_day!(model::Model, ps_vars::PowerSystemVariables,
                              input::MasterProblemInput, year_idx::Int,
                              start_hour::Int, hours::Int, tech_output;
                              extra::Function)
    cost = AffExpr(0.0)
    has_master_fuel(input) || return cost
    mf = input.primary_energy
    tres = Float64(max(1, input.temporal_resolution_hours))
    b2n = input.network.bus_to_node
    n_buses = input.network.num_buses
    n_gen = length(input.generators)
    cache = Dict{MasterFuelKey, Any}()
    built(k) = get!(() -> extra(k), cache, k)

    # Electrolyzers: power ≤ existing + built, ramps as in the dispatch
    # (add_electrolyzer_dispatch_constraints!), variable and water cost per MWh
    P = ps_vars.electrolyzer_power
    if P !== nothing
        for (e, ec) in enumerate(mf.electrolyzers), b in 1:size(P, 2)
            rated = _elz_value(ec.rated_power, b)
            can_build = _elz_value(ec.invest_max_power, b) > 0
            cap = can_build ? rated + built((:electrolyzer, string(e), b)) : rated
            for t in 1:hours
                @constraint(model, P[e, b, t] <= cap)
            end
            (rated > 0 || can_build) || continue
            up = _elz_value(ec.ramp_up, b)
            down = _elz_value(ec.ramp_down, b)
            for t in 2:hours
                up > 0 && @constraint(model,
                    P[e, b, t] - P[e, b, t-1] <= min(1.0, up * tres) * cap)
                down > 0 && @constraint(model,
                    P[e, b, t-1] - P[e, b, t] <= min(1.0, down * tres) * cap)
            end
            vc = _elz_value(ec.variable_cost, b)
            b <= length(ec.eff_at_rated) && (vc += ec.water_cost * electrolyzer_h2_per_mwh(ec, b))
            vc != 0 || continue
            for t in 1:hours
                add_to_expression!(cost, tres * vc, P[e, b, t])
            end
        end
    end

    # The dispatch's fuel model on this day, with the master's builds
    pe_day = master_fuel_day_input(input, year_idx, start_hour, hours)
    pe_vars, temporal, prices = create_primary_energy_model(model, pe_day;
        storage_extra = (f, n) -> built((:storage, f, n)),
        transport_extra = (f, r) -> built((:transport, f, r)),
        cyclic = true)
    couple_primary_energy_to_power_system!(model, pe_vars, ps_vars, pe_day;
        bus_to_node = collect(Int, b2n), resolution_hours = tres,
        electrolyzers = P === nothing ? nothing : mf.electrolyzers)
    # The Hydrogen balance belongs to this day only
    delete!(model.ext, :pe_hydrogen_balance)
    terms = get_primary_energy_objective_terms(pe_vars, pe_day, temporal, prices)
    for term in values(terms)
        add_to_expression!(cost, term)
    end
    # The last day's fuel model, for inspection (tests, diagnostics)
    model.ext[:master_fuel_last_day] = (pe_vars, terms)

    # Fuel-coupled units pay the fuel through the balance, not their fuel_cost
    n_nodes = pe_day.num_nodes
    node_ok(b) = 1 <= b2n[b] <= n_nodes
    for (k, (_, mwhe, _, _)) in pe_day.generator_fuel_map
        mwhe > 1e-6 || continue
        if k <= n_gen
            gen = input.generators[k]
            for b in ps_vars.buses_of_gen[k]
                node_ok(b) || continue
                haskey(ps_vars.gen_seg_output, (k, b)) && continue
                fc = gen.fuel_cost[b]
                fc != 0 || continue
                for t in 1:hours
                    add_to_expression!(cost, -tres * fc, ps_vars.gen_output[k, b, t])
                end
            end
        elseif tech_output !== nothing && k - n_gen <= size(tech_output, 1)
            # Candidate technology tc: its output at a node burns that node's
            # fuel, like the dispatch's virtual generator for it
            tc = k - n_gen
            tech = input.technologies[tc]
            ffp = pe_vars.fuel_for_power_hourly[k]
            for node in 1:n_nodes
                buses = [b for b in 1:n_buses if b2n[b] == node]
                isempty(buses) && continue
                for t in 1:hours
                    @constraint(model,
                        sum(tres * tech_output[tc, b, t] for b in buses) <= ffp[node, t] * mwhe,
                        base_name = "pe_coupling_tech$(tc)_n$(node)_$(t)")
                end
            end
            for b in 1:n_buses
                node_ok(b) || continue
                # A technology priced by its offer curve keeps it
                tech_curve_segments(input, tc, b) === nothing || continue
                fc = tech.fuel_cost[b]
                fc != 0 || continue
                for t in 1:hours
                    add_to_expression!(cost, -tres * fc, tech_output[tc, b, t])
                end
            end
        end
    end
    return cost
end

"""
    master_fuel_unit_price(input, g, b, year_idx) -> Union{Nothing, Float64}

What a MWh of existing generator `g` at bus `b` costs in fuel in year
`year_idx` when it burns tank fuel (primary energy on, the unit mapped to a
fuel, as `add_master_fuel_day!` couples it): the fuel's price that year
(`price_base` grown at `price_growth_rate`, as the fuel balance prices it)
over the MWh the unit makes per fuel unit. The master charges the fuel
through the balance instead of the unit's `fuel_cost` (a unit with an offer
curve keeps its curve too). `nothing` when the unit pays its own
`fuel_cost`. For the unit's NPV (`add_unit_day_operation!`): the fuel
system's import, route transport and shortfall costs are not shared out to
the units.
"""
function master_fuel_unit_price(input::MasterProblemInput, g::Int, b::Int, year_idx::Int)
    fm = _master_unit_fuel(input, g, b)
    fm === nothing && return nothing
    fuel, mwhe = fm
    pe = input.primary_energy.pe
    price = fuel.price_base * fuel_price_factor(fuel.price_growth_rate,
                                                input.years[year_idx], pe.base_year)
    return price / mwhe
end

"""
    master_fuel_unit_growth(input, g, b) -> Union{Nothing, Float64}

Yearly growth factor (1 + `price_growth_rate`) of the tank fuel generator
`g` burns at bus `b` (`master_fuel_unit_price`); `nothing` when it is not
fuel-coupled there.
"""
function master_fuel_unit_growth(input::MasterProblemInput, g::Int, b::Int)
    fm = _master_unit_fuel(input, g, b)
    return fm === nothing ? nothing : 1.0 + fm[1].price_growth_rate
end

# The FuelConfig and MWh per fuel unit of generator g's tank fuel at bus b
function _master_unit_fuel(input::MasterProblemInput, g::Int, b::Int)
    has_master_fuel(input) || return nothing
    pe = input.primary_energy.pe
    haskey(pe.generator_fuel_map, g) || return nothing
    fuel_name, mwhe, _, _ = pe.generator_fuel_map[g]
    mwhe > 1e-6 || return nothing
    node = input.network.bus_to_node[b]
    1 <= node <= pe.num_nodes || return nothing
    idx = findfirst(f -> f.name == fuel_name, pe.fuels)
    idx === nothing && return nothing
    return pe.fuels[idx], mwhe
end

"""
    master_fuel_day_scale(input, ndays=input.representative_days_per_year) -> Float64

Days of the year one representative day stands for, with `ndays` days
selected in the year (the fuel block's day cost is already in \$, so no ×Δt).
"""
function master_fuel_day_scale(input::MasterProblemInput,
                               ndays::Integer = input.representative_days_per_year)
    days_in_year = length(input.hours_per_year) > 0 ? input.hours_per_year[1] / 24.0 : 365.0
    return days_in_year / max(ndays, 1)
end

"""
    master_fuel_investment_terms(vars, input; mult=nothing) -> Vector{(year_idx, var, cost, life)}

Each fuel storage, route transport and electrolyzer build variable with its
build year, overnight cost per unit built (not discounted) and economic
lifetime (years): the fuel's `lifetime_storage` / `lifetime_transport`, the
electrolyzer's `life_time` (0 takes `DEFAULT_ASSET_LIFETIME` in the
annuity). `fuel_storage_invest_cost`, `fuel_transport_cost` (D-5: it also
scales the route investment) and `electrolyzer_invest_cost` scale them per
scenario. `master_investment_terms` adds them to the master's investments.
"""
function master_fuel_investment_terms(vars::MasterProblemVariables, input::MasterProblemInput;
                                      mult::Union{Nothing, ScenarioMultipliers} = nothing)
    terms = Tuple{Int, VariableRef, Float64, Float64}[]
    fv = vars.fuel
    (fv === nothing || !has_master_fuel(input)) && return terms
    pe = input.primary_energy.pe
    m_sto = mult === nothing ? 1.0 : mult.fuel_storage_invest_cost
    m_tr = mult === nothing ? 1.0 : mult.fuel_transport_cost
    m_elz = mult === nothing ? 1.0 : mult.electrolyzer_invest_cost
    for (y, by_fuel) in fv.storage, (fname, v) in by_fuel
        infra = get(pe.infrastructure, fname, nothing)
        infra === nothing && continue
        for n in eachindex(v)
            push!(terms, (y, v[n], m_sto * infra.storage_investment_cost,
                          infra.lifetime_storage))
        end
    end
    for (y, by_fuel) in fv.transport, (fname, v) in by_fuel
        infra = get(pe.infrastructure, fname, nothing)
        infra === nothing && continue
        for r in eachindex(v)
            km = pe.transport_routes[r].distance_km
            push!(terms, (y, v[r], m_tr * infra.transport_investment_cost * km,
                          infra.lifetime_transport))
        end
    end
    for (y, by_elz) in fv.electrolyzer, (e, v) in by_elz
        ec = input.primary_energy.electrolyzers[e]
        for b in eachindex(v)
            push!(terms, (y, v[b], m_elz * _elz_value(ec.invest_cost, b),
                          _elz_value(ec.life_time, b)))
        end
    end
    return terms
end

"""
    master_fuel_annual_cost(vars, input, y_idx; mult=nothing) -> AffExpr

Undiscounted fixed cost of the electrolyzers in service in year `y_idx`:
`fixed_cost` (fixed O&M, \$/MW·year, as in the dispatch) × (rated + built and
not yet retired) power, × `electrolyzer_fixed_cost` per scenario.
"""
function master_fuel_annual_cost(vars::MasterProblemVariables, input::MasterProblemInput,
                                 y_idx::Int; mult::Union{Nothing, ScenarioMultipliers} = nothing)
    cost = AffExpr(0.0)
    has_master_fuel(input) || return cost
    m = mult === nothing ? 1.0 : mult.electrolyzer_fixed_cost
    for (e, ec) in enumerate(input.primary_energy.electrolyzers), b in eachindex(ec.fixed_cost)
        fc = ec.fixed_cost[b] * m
        fc != 0 || continue
        add_to_expression!(cost, fc * _elz_value(ec.rated_power, b))
        _elz_value(ec.invest_max_power, b) > 0 || continue
        add_to_expression!(cost,
            master_fuel_extra_expr(vars.fuel, input, y_idx, (:electrolyzer, string(e), b)), fc)
    end
    return cost
end

"""
    scale_master_fuel(mf, mult, fuel_factor) -> Union{Nothing, MasterFuelInput}

A stochastic scenario's fuel block: fuel prices × `fuel_factor` (fuel_cost ×
fuel_price_growth^(y−1), as for the units' fuel_cost), import costs ×
`fuel_import_cost`, route transport cost × `fuel_transport_cost`, fuel
shortfall/excess penalty × `penalty_loss_of_fuel_supply`, electrolyzer
variable and water cost × their multipliers. Investment and fixed costs are
scaled where they are charged.
"""
function scale_master_fuel(mf::Union{Nothing, MasterFuelInput}, mult::ScenarioMultipliers,
                           fuel_factor::Float64)
    mf === nothing && return nothing
    pe = mf.pe
    fuels = FuelConfig[with_fields(f; price_base = f.price_base * fuel_factor,
                                   import_cost = f.import_cost .* mult.fuel_import_cost)
                       for f in pe.fuels]
    routes = TransportRoute[
        TransportRoute(r.route_id, r.from_node, r.to_node, r.distance_km,
            Dict{String, FuelRouteParams}(
                k => FuelRouteParams(p.capacity, p.transport_cost * mult.fuel_transport_cost,
                                     p.transport_losses)
                for (k, p) in r.fuel_params))
        for r in pe.transport_routes]
    pe_s = with_fields(pe; fuels = fuels, transport_routes = routes,
        loss_of_fuel_supply_penalty = pe.loss_of_fuel_supply_penalty *
                                      mult.penalty_loss_of_fuel_supply)
    elz = ElectrolyzerConfig[with_fields(ec;
            variable_cost = ec.variable_cost .* mult.electrolyzer_variable_cost,
            water_cost = ec.water_cost * mult.electrolyzer_water_cost)
          for ec in mf.electrolyzers]
    return MasterFuelInput(pe_s, elz)
end

"""
    extract_master_fuel_solution(vars, input) -> Dict{String, Any}

The fuel builds of a solved master, 1-based throughout:
- `"storage"`, `"transport"`: year => fuel => new capacity per node / route
- `"electrolyzer"`: year => electrolyzer => new MW per bus
- `"storage_capacity"`, `"transport_capacity"`, `"electrolyzer_capacity"`:
  year => ... => capacity built and in service that year (what the
  dispatch adds to the configured capacity), for every model year.
Empty without primary energy.
"""
function extract_master_fuel_solution(vars::MasterProblemVariables, input::MasterProblemInput)
    out = Dict{String, Any}()
    fv = vars.fuel
    (fv === nothing || !has_master_fuel(input)) && return out
    vals(d) = Dict(y => Dict(k => Float64[capacity_value(x) for x in v] for (k, v) in by)
                   for (y, by) in d)
    out["storage"] = vals(fv.storage)
    out["transport"] = vals(fv.transport)
    out["electrolyzer"] = vals(fv.electrolyzer)
    pe = input.primary_energy.pe
    n_routes = length(pe.transport_routes)
    sto_cap = Dict{Int, Dict{String, Vector{Float64}}}()
    tr_cap = Dict{Int, Dict{String, Vector{Float64}}}()
    elz_cap = Dict{Int, Dict{Int, Vector{Float64}}}()
    for y in 1:length(input.years)
        sto_cap[y] = Dict(f.name => zeros(pe.num_nodes) for f in pe.fuels)
        tr_cap[y] = Dict(f.name => zeros(n_routes) for f in pe.fuels)
        elz_cap[y] = Dict(e => zeros(length(ec.invest_max_power))
                          for (e, ec) in enumerate(input.primary_energy.electrolyzers))
        for k in master_fuel_keys(input)
            kind, name, i = k
            v = capacity_value(master_fuel_extra_expr(fv, input, y, k))
            if kind === :storage
                sto_cap[y][name][i] = v
            elseif kind === :transport
                tr_cap[y][name][i] = v
            else
                elz_cap[y][parse(Int, name)][i] = v
            end
        end
    end
    out["storage_capacity"] = sto_cap
    out["transport_capacity"] = tr_cap
    out["electrolyzer_capacity"] = elz_cap
    return out
end

# =============================================================================
# Benders
# =============================================================================

"""
    benders_fuel_day!(sp, ps_vars, input, year_idx, start_hour, hours,
                      tech_output, fixed) -> (cost, fix_constraints)

The fuel block of a Benders subproblem day. Each buildable capacity is a
copy variable fixed to the master's value (`fixed`, from
`master_fuel_extra_values`); the dual of its fixing constraint is the
day's marginal cost of that capacity, which the optimality cut uses
(`extract_copy_duals`, benders.jl).
"""
function benders_fuel_day!(sp::Model, ps_vars::PowerSystemVariables,
                           input::MasterProblemInput, year_idx::Int,
                           start_hour::Int, hours::Int, tech_output,
                           fixed::AbstractDict)
    fix = Dict{MasterFuelKey, Any}()
    has_master_fuel(input) || return AffExpr(0.0), fix
    copies = Dict{MasterFuelKey, VariableRef}()
    for k in master_fuel_keys(input)
        c = @variable(sp, base_name = "fuel_cap_$(k[1])_$(k[2])_$(k[3])")
        fix[k] = @constraint(sp, c == get(fixed, k, 0.0))
        copies[k] = c
    end
    cost = add_master_fuel_day!(sp, ps_vars, input, year_idx, start_hour, hours,
        tech_output; extra = k -> get(copies, k, 0.0))
    return cost, fix
end
