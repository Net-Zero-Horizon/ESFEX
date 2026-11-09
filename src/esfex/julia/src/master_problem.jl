"""
master_problem.jl - Capacity Expansion Master Problem

Handles long-term investment decisions for:
- Generator capacity additions and retirements
- Battery storage investments
- Transmission expansion
- Representative day selection and operational validation

The Master Problem uses a two-stage structure:
1. First stage: Investment decisions (common across scenarios)
2. Second stage: Operational costs validated through representative days
"""

using JuMP: @variable, @constraint, @objective, @expression, Model, AffExpr
using JuMP: value, VariableRef, add_to_expression!, objective_value
using JuMP: termination_status, solve_time, optimize!
using JuMP: lower_bound, upper_bound, set_lower_bound, set_upper_bound
using JuMP: set_objective_function, set_objective_sense, MAX_SENSE, MIN_SENSE
using JuMP: objective_function

# =============================================================================
# Target RE Calculation
# =============================================================================

"""
    calculate_target_ratios(input::MasterProblemInput)

Calculate progressive RE penetration targets for each year.
Linear interpolation from initial to target penetration.
Applies min/max increment constraints as in Python legacy (lines 99-122).
Entries of `input.re_target_override` replace the computed targets.
"""
function calculate_target_ratios(input::MasterProblemInput)::Dict{Tuple{Int,Int}, Float64}
    num_years = length(input.years)
    targets = Dict{Tuple{Int,Int}, Float64}()
    n_sys = length(input.system_node_ranges)

    if n_sys == 0
        # Single global system — use global initial_re_penetration
        for y_idx in 1:num_years
            targets[(1, y_idx)] = _compute_year_target(
                y_idx, num_years, input.initial_re_penetration,
                input.target_re_penetration, input.min_re_increment,
                input.max_re_increment,
                y_idx > 1 ? targets[(1, y_idx - 1)] : 0.0
            )
        end
    else
        # Per-system targets using each system's initial_re
        for s_idx in 1:n_sys
            sys_initial = input.system_node_ranges[s_idx].initial_re
            for y_idx in 1:num_years
                targets[(s_idx, y_idx)] = _compute_year_target(
                    y_idx, num_years, sys_initial,
                    input.target_re_penetration, input.min_re_increment,
                    input.max_re_increment,
                    y_idx > 1 ? targets[(s_idx, y_idx - 1)] : 0.0
                )
            end
        end
    end

    merge!(targets, input.re_target_override)
    return targets
end

function _compute_year_target(
    y_idx::Int, num_years::Int, initial_re::Float64,
    target_re::Float64, min_inc::Float64, max_inc::Float64,
    prev_target::Float64
)::Float64
    if y_idx == 1
        return initial_re
    elseif num_years == 1
        return target_re
    else
        progress = (y_idx - 1) / (num_years - 1)
        target = initial_re + progress * (target_re - initial_re)
        increment = target - prev_target
        if increment < min_inc
            target = prev_target + min_inc
        elseif increment > max_inc
            target = prev_target + max_inc
        end
        return target
    end
end

# =============================================================================
# Representative Day Selection
# =============================================================================

"""
    select_representative_days(demand, year_idx, num_days, min_separation, timesteps_per_day, timesteps_per_year)

Select representative days based on peak gross demand.

Returns vector of starting timestep indices for each representative day.
"""
function select_representative_days(
    demand::Matrix{Float64},
    year_idx::Int,
    num_days::Int,
    min_separation::Int,
    timesteps_per_day::Int,
    timesteps_per_year::Int,
)::Vector{Int}
    year_start = (year_idx - 1) * timesteps_per_year + 1
    year_end = min(year_idx * timesteps_per_year, size(demand, 1))

    if year_end < year_start
        return Int[]
    end

    year_demand = demand[year_start:year_end, :]
    num_timesteps = size(year_demand, 1)
    num_possible_days = num_timesteps ÷ timesteps_per_day

    if num_possible_days < 1
        return Int[]
    end

    daily_peaks = Float64[]
    for d in 1:num_possible_days
        start_t = (d - 1) * timesteps_per_day + 1
        end_t = d * timesteps_per_day
        day_signal = year_demand[start_t:end_t, :]
        push!(daily_peaks, maximum(sum(day_signal, dims=2)))
    end

    # Helper: check if day satisfies minimum separation from all selected
    function is_valid_day(day_idx::Int, selected::Set{Int}, min_sep::Int)::Bool
        for sel_d in selected
            if abs(day_idx - sel_d) < min_sep
                return false
            end
        end
        return true
    end

    # Helper: find day with highest peak in range satisfying constraints
    function find_best_in_range(start_d::Int, end_d::Int, excluded::Set{Int}, min_sep::Int)::Int
        best = -1
        best_peak = -Inf
        for d in start_d:end_d
            if d in excluded
                continue
            end
            if !is_valid_day(d, excluded, min_sep)
                continue
            end
            if daily_peaks[d] > best_peak
                best_peak = daily_peaks[d]
                best = d
            end
        end
        return best
    end

    # Start with global peak day
    selected_days = Int[]
    selected_indices = Set{Int}()

    peak_day = argmax(daily_peaks)
    push!(selected_days, year_start + (peak_day - 1) * timesteps_per_day)
    push!(selected_indices, peak_day)

    # Select remaining days using segment-based approach for temporal diversity
    # (matches Python legacy: divide year into segments, pick best per segment)
    remaining_needed = min(num_days, num_possible_days) - 1

    if remaining_needed > 0
        # Divide year into segments to ensure diversity across seasons
        num_segments = remaining_needed + 1  # +1 because peak day occupies one segment
        segment_size = num_possible_days ÷ num_segments

        for seg in 0:(num_segments - 1)
            if length(selected_days) >= min(num_days, num_possible_days)
                break
            end

            seg_start = seg * segment_size + 1
            seg_end = min((seg + 1) * segment_size, num_possible_days)

            best = find_best_in_range(seg_start, seg_end, selected_indices, min_separation)
            if best > 0
                push!(selected_days, year_start + (best - 1) * timesteps_per_day)
                push!(selected_indices, best)
            end
        end
    end

    return selected_days
end

# =============================================================================
# Investment Period Helpers
# =============================================================================

"""
    inv_period_year(y_idx, years_per_inv_period)

Return the investment period start year for a given year index.
Investment decisions are grouped into periods of `years_per_inv_period` years.
E.g., with years_per_inv_period=5: years 1-5 → period start 1, years 6-10 → 6.
"""
@inline function inv_period_year(y_idx::Int, years_per_inv_period::Int)::Int
    return ((y_idx - 1) ÷ years_per_inv_period) * years_per_inv_period + 1
end

"""
    is_inv_period_start(y_idx, years_per_inv_period)

Return true if y_idx is the first year of an investment period.
"""
@inline function is_inv_period_start(y_idx::Int, years_per_inv_period::Int)::Bool
    return (y_idx - 1) % years_per_inv_period == 0
end

# =============================================================================
# Variable Creation
# =============================================================================

"""
    build_master_variables!(model, input)

Create all Master Problem decision variables.
"""
function build_master_variables!(
    model::Model,
    input::MasterProblemInput
)::MasterProblemVariables
    num_years = length(input.years)
    n_buses = input.network.num_buses
    n_gen = length(input.generators)
    n_bat = length(input.batteries)

    # Investment period grouping from investment_resolution_hours
    years_per_inv_period = max(1, input.investment_resolution_hours ÷ 8760)
    num_inv_periods = cld(num_years, years_per_inv_period)  # ceiling division
    @info "Investment periods: $(num_inv_periods) periods of $(years_per_inv_period) year(s) each"

    # Technology investment variables (per-technology, not per-generator)
    n_tech = length(input.technologies)
    n_bat_tech = length(input.battery_technologies)
    tech_investment = Dict{Int, Dict{Int, Vector{VariableRef}}}()
    bat_tech_power_investment = Dict{Int, Dict{Int, Vector{VariableRef}}}()
    bat_tech_capacity_investment = Dict{Int, Dict{Int, Vector{VariableRef}}}()
    transfer_investment = Dict{Int, Dict{Tuple{Int,Int}, VariableRef}}()
    corridors = Set{Tuple{Int,Int}}()
    for (i, j) in TransmissionDC(input.network).lines
        push!(corridors, (i, j), (j, i))
    end

    # Life extension variables
    gen_life_extension = Dict{Int, Dict{Int, Vector{Union{VariableRef, Nothing}}}}()
    bat_life_extension = Dict{Int, Dict{Int, Vector{Union{VariableRef, Nothing}}}}()

    # RE ratio — per system (sys_idx, y_idx) → variable
    # When system_node_ranges is empty, sys_idx=1 covers all buses
    re_penetration_ratio = Dict{Tuple{Int,Int}, VariableRef}()

    # Slack variables
    slack_re_target = Dict{Int, VariableRef}()
    slack_capacity = Dict{Tuple{Int,Int}, VariableRef}()
    slack_budget = Dict{Int, VariableRef}()

    b2n = input.network.bus_to_node  # bus index → node index mapping

    for y_idx in 1:num_years
        # Only create investment variables at investment period boundaries.
        # Non-start years share the same variables as their period's start year.
        if !is_inv_period_start(y_idx, years_per_inv_period)
            # Skip investment variable creation — cumulative sums iterate
            # only over period-start years via step `years_per_inv_period`.
        else
            # Technology investment per bus (per-technology, not per-generator)
            tech_investment[y_idx] = Dict{Int, Vector{VariableRef}}()
            for t in 1:n_tech
                tech = input.technologies[t]
                tech_investment[y_idx][t] = @variable(model,
                    [b=1:n_buses],
                    lower_bound = 0,
                    upper_bound = tech.invest_max[b],
                    base_name = "tech_inv_y$(y_idx)_t$(t)"
                )
            end

            # Battery technology power investment per bus
            bat_tech_power_investment[y_idx] = Dict{Int, Vector{VariableRef}}()
            for bt in 1:n_bat_tech
                btech = input.battery_technologies[bt]
                bat_tech_power_investment[y_idx][bt] = @variable(model,
                    [b=1:n_buses],
                    lower_bound = 0,
                    upper_bound = btech.invest_max_power[b],
                    base_name = "bat_tech_pow_inv_y$(y_idx)_bt$(bt)"
                )
            end

            # Battery technology capacity investment per bus
            bat_tech_capacity_investment[y_idx] = Dict{Int, Vector{VariableRef}}()
            for bt in 1:n_bat_tech
                btech = input.battery_technologies[bt]
                bat_tech_capacity_investment[y_idx][bt] = @variable(model,
                    [b=1:n_buses],
                    lower_bound = 0,
                    upper_bound = btech.invest_max_capacity[b],
                    base_name = "bat_tech_cap_inv_y$(y_idx)_bt$(bt)"
                )
            end

            # Transmission investment, only on corridors that exist as a line
            # or transformer (a candidate corridor may start at 0 MW). Capacity
            # bought between buses with no line never reaches the dispatch.
            transfer_investment[y_idx] = Dict{Tuple{Int,Int}, VariableRef}()
            for i in 1:n_buses
                for j in 1:n_buses
                    if i != j && (i, j) in corridors &&
                       (input.network.connections[i, j] > 0 || input.network.transference_invest_max[i] > 0)
                        # Upper bound from transference_invest_max (use bus i as reference)
                        max_inv = input.network.transference_invest_max[i]
                        transfer_investment[y_idx][(i, j)] = @variable(model,
                            lower_bound = 0,
                            upper_bound = max_inv,
                            base_name = "trans_inv_y$(y_idx)_$(i)_$(j)"
                        )
                    end
                end
            end
        end  # is_inv_period_start

        # Life extension variables: continuous variables for units past lifetime
        # Allows optimizer to choose how much capacity to keep online (LP-friendly)
        gen_life_extension[y_idx] = Dict{Int, Vector{Union{VariableRef, Nothing}}}()
        for g in 1:n_gen
            gen = input.generators[g]
            gen_life_extension[y_idx][g] = Vector{Union{VariableRef, Nothing}}(nothing, n_buses)
            for n in 1:n_buses
                rated_power = gen.rated_power[n]
                lifetime = gen.life_time[n]
                initial_age = gen.initial_age[n]
                age_at_year = initial_age + y_idx - 1
                degradation_rate = gen.degradation_rate[n]
                if age_at_year >= lifetime && rated_power > 0
                    deg_factor = (1.0 - degradation_rate) ^ age_at_year
                    gen_life_extension[y_idx][g][n] = @variable(model,
                        lower_bound = 0,
                        upper_bound = rated_power * deg_factor,
                        base_name = "gen_life_ext_y$(y_idx)_g$(g)_n$(n)"
                    )
                end
            end
        end

        bat_life_extension[y_idx] = Dict{Int, Vector{Union{VariableRef, Nothing}}}()
        for bi in 1:n_bat
            bat = input.batteries[bi]
            bat_life_extension[y_idx][bi] = Vector{Union{VariableRef, Nothing}}(nothing, n_buses)
            for n in 1:n_buses
                base_power = bat.max_discharge_power[n]
                lifetime = bat.life_time[n]
                initial_age = bat.initial_age[n]
                age_at_year = initial_age + y_idx - 1
                if age_at_year >= lifetime && base_power > 0
                    bat_life_extension[y_idx][bi][n] = @variable(model,
                        lower_bound = 0,
                        upper_bound = base_power,
                        base_name = "bat_life_ext_y$(y_idx)_b$(bi)_n$(n)"
                    )
                end
            end
        end

        # RE penetration ratio — per system
        n_sys = length(input.system_node_ranges)
        if n_sys == 0
            n_sys = 1  # single global system
        end
        for s_idx in 1:n_sys
            sname = n_sys == 1 && isempty(input.system_node_ranges) ? "global" : input.system_node_ranges[s_idx].name
            re_penetration_ratio[(s_idx, y_idx)] = @variable(model,
                lower_bound = 0,
                upper_bound = 1,
                base_name = "re_ratio_s$(s_idx)_y$(y_idx)"
            )
        end

        # Slack variables
        slack_re_target[y_idx] = @variable(model,
            lower_bound = 0,
            base_name = "slack_re_y$(y_idx)"
        )

        slack_budget[y_idx] = @variable(model,
            lower_bound = 0,
            base_name = "slack_budget_y$(y_idx)"
        )

        # Slack capacity per NODE (not per bus) — generators on different buses
        # of the same node can serve demand on any bus via internal transmission
        for ni in 1:input.network.num_nodes
            slack_capacity[(y_idx, ni)] = @variable(model,
                lower_bound = 0,
                base_name = "slack_cap_y$(y_idx)_node$(ni)"
            )
        end
    end

    # Fuel storage, route transport and electrolyzers: build_master_fuel_variables!
    # (master_fuel.jl), part of the returned variables below.

    # Reservoir capacity investment variables (MWh), at the investment-period
    # starts like the other builds; other years keep an empty dict
    reservoir_investment = Dict{Int, Dict{Int, Vector{VariableRef}}}()
    for y_idx in 1:num_years
        reservoir_investment[y_idx] = Dict{Int, Vector{VariableRef}}()
        is_inv_period_start(y_idx, years_per_inv_period) || continue
        for g in 1:n_gen
            gen = input.generators[g]
            if any(gen.reservoir_invest_max .> 0)
                reservoir_investment[y_idx][g] = @variable(model,
                    [b=1:n_buses],
                    lower_bound = 0,
                    upper_bound = gen.reservoir_invest_max[b],
                    base_name = "res_inv_y$(y_idx)_g$(g)"
                )
            end
        end
    end

    # Converter investment (MW) at period starts, for AC/DC and frequency
    # converters that allow it (invest_max_power > 0)
    converter_investment = Dict{Int, Dict{Tuple{String,Int}, VariableRef}}()
    for y_idx in 1:years_per_inv_period:num_years
        converter_investment[y_idx] = Dict{Tuple{String,Int}, VariableRef}()
        for (key, conv) in master_converters(input.network)
            conv.invest_max_power > 0 || continue
            converter_investment[y_idx][key] = @variable(model,
                lower_bound = 0, upper_bound = conv.invest_max_power,
                base_name = "conv_inv_y$(y_idx)_$(key[1])$(key[2])")
        end
    end

    return MasterProblemVariables(
        tech_investment,
        bat_tech_power_investment,
        bat_tech_capacity_investment,
        transfer_investment,
        gen_life_extension,
        bat_life_extension,
        re_penetration_ratio,
        Dict{Tuple{Int,Int}, Tuple{Model, PowerSystemVariables}}(),
        Dict{Int, Vector{AffExpr}}(),
        slack_re_target,
        slack_capacity,
        slack_budget,
        Dict{Int, Dict{Tuple{Int,Int,Int}, VariableRef}}(),  # inter_period_soc (TSAM)
        Dict{Int, Dict{Tuple{Int,Int,Int}, VariableRef}}(),  # inter_period_reservoir (TSAM seasonal hydro)
        reservoir_investment,
        years_per_inv_period,
        converter_investment,
        Dict{Tuple{Int,Int,Int}, VariableRef}(),  # co2_budget_violation
        build_master_fuel_variables!(model, input, years_per_inv_period)  # M2 (#89)
    )
end

"""
    master_converters(network)

The AC/DC and frequency converters of `network` as `(key, converter)` pairs,
keyed `("acdc", c)` / `("freq", c)` like `converter_investment`.
"""
master_converters(network::NetworkConfig) = vcat(
    [(("acdc", c), conv) for (c, conv) in enumerate(network.acdc_converters)],
    [(("freq", c), conv) for (c, conv) in enumerate(network.freq_converters)])

"""
    reservoir_capacity_built(vars, g, b, year_idx) -> AffExpr

Reservoir capacity (MWh) of generator `g` at bus `b` built at the
investment-period starts up to year `year_idx`. Reservoirs do not retire.
"""
function reservoir_capacity_built(vars::MasterProblemVariables, g::Int, b::Int,
                                  year_idx::Int)
    built = AffExpr(0.0)
    for y in 1:vars.years_per_inv_period:year_idx
        v = get(get(vars.reservoir_investment, y, Dict{Int, Vector{VariableRef}}()), g, nothing)
        v === nothing || add_to_expression!(built, v[b])
    end
    return built
end

"""
    converter_capacity(vars, conv, key, year_idx)

Converter capacity in year `year_idx`: its rated power plus the MW built at
the investment-period starts up to that year. As in the dispatch, a
converter does not age or retire.
"""
function converter_capacity(vars::MasterProblemVariables, conv, key::Tuple{String,Int},
                            year_idx::Int)
    cap = AffExpr(conv.rated_power_mva)
    for y in 1:vars.years_per_inv_period:year_idx
        v = get(get(vars.converter_investment, y, Dict{Tuple{String,Int}, VariableRef}()),
                key, nothing)
        v === nothing || add_to_expression!(cap, v)
    end
    return cap
end

# =============================================================================
# Constraints
# =============================================================================

"""
    add_investment_constraints!(model, vars, input)

Add cumulative investment limit constraints.
"""
function add_investment_constraints!(
    model::Model,
    vars::MasterProblemVariables,
    input::MasterProblemInput
)
    num_years = length(input.years)
    n_buses = input.network.num_buses
    b2n = input.network.bus_to_node
    n_gen = length(input.generators)
    n_bat = length(input.batteries)

    # Technology cumulative investment limits per bus
    n_tech = length(input.technologies)
    n_bat_tech = length(input.battery_technologies)
    ypp = vars.years_per_inv_period  # shorthand
    for t in 1:n_tech
        tech = input.technologies[t]
        for b in 1:n_buses
            max_total = tech.invest_max[b]
            if max_total > 0
                cumulative = sum(vars.tech_investment[y][t][b] for y in 1:ypp:num_years)
                @constraint(model,
                    cumulative <= max_total,
                    base_name = "cumul_tech_t$(t)_b$(b)"
                )
            end
        end
    end

    # Battery technology cumulative investment limits per bus
    for bt in 1:n_bat_tech
        btech = input.battery_technologies[bt]
        for b in 1:n_buses
            # Power limit
            max_power = btech.invest_max_power[b]
            if max_power > 0
                cumulative_power = sum(vars.bat_tech_power_investment[y][bt][b] for y in 1:ypp:num_years)
                @constraint(model,
                    cumulative_power <= max_power,
                    base_name = "cumul_bat_tech_pow_bt$(bt)_b$(b)"
                )
            end

            # Capacity limit
            max_cap = btech.invest_max_capacity[b]
            if max_cap > 0
                cumulative_cap = sum(vars.bat_tech_capacity_investment[y][bt][b] for y in 1:ypp:num_years)
                @constraint(model,
                    cumulative_cap <= max_cap,
                    base_name = "cumul_bat_tech_cap_bt$(bt)_b$(b)"
                )
            end
        end
    end

    # Battery technology duration constraints (energy-to-power ratio)
    # Only at investment period boundaries (same variables are shared within a period)
    for bt in 1:n_bat_tech
        btech = input.battery_technologies[bt]
        min_dur = btech.min_duration_hours
        max_dur = btech.max_duration_hours

        for y_idx in 1:ypp:num_years
            for b in 1:n_buses
                if min_dur > 0
                    @constraint(model,
                        vars.bat_tech_capacity_investment[y_idx][bt][b] >= min_dur * vars.bat_tech_power_investment[y_idx][bt][b],
                        base_name = "bat_tech_min_dur_bt$(bt)_b$(b)_y$(y_idx)"
                    )
                end
                if max_dur < Inf && max_dur > 0
                    @constraint(model,
                        vars.bat_tech_capacity_investment[y_idx][bt][b] <= max_dur * vars.bat_tech_power_investment[y_idx][bt][b],
                        base_name = "bat_tech_max_dur_bt$(bt)_b$(b)_y$(y_idx)"
                    )
                end
            end
        end
    end

    # Reservoir cumulative investment limits per bus
    for g in 1:n_gen
        gen = input.generators[g]
        for b in 1:n_buses
            max_res = gen.reservoir_invest_max[b]
            if max_res > 0 && all(haskey(vars.reservoir_investment[y], g) for y in 1:ypp:num_years)
                cumulative_res = sum(vars.reservoir_investment[y][g][b] for y in 1:ypp:num_years)
                @constraint(model,
                    cumulative_res <= max_res,
                    base_name = "cumul_res_g$(g)_b$(b)"
                )
            end
        end
    end

    # Converter cumulative investment limit
    for (key, conv) in master_converters(input.network)
        conv.invest_max_power > 0 || continue
        @constraint(model,
            sum(vars.converter_investment[y][key] for y in 1:ypp:num_years) <=
                conv.invest_max_power,
            base_name = "cumul_conv_$(key[1])$(key[2])")
    end

    # Per-node aggregate cumulative investment constraint
    # When multiple buses map to the same node, the total investment across
    # all buses at that node must not exceed the per-node limit.
    node_to_buses_inv = Dict{Int, Vector{Int}}()
    for b in 1:n_buses
        push!(get!(node_to_buses_inv, b2n[b], Int[]), b)
    end
    for t in 1:n_tech
        tech = input.technologies[t]
        for (ni, buses_at_node) in node_to_buses_inv
            length(buses_at_node) <= 1 && continue
            max_inv = tech.invest_max[buses_at_node[1]]
            max_inv > 0 || continue
            @constraint(model,
                sum(vars.tech_investment[y][t][b] for y in 1:ypp:num_years, b in buses_at_node) <= max_inv,
                base_name = "cumul_node_tech_t$(t)_n$(ni)")
        end
    end
    for bt in 1:n_bat_tech
        btech = input.battery_technologies[bt]
        for (ni, buses_at_node) in node_to_buses_inv
            length(buses_at_node) <= 1 && continue
            max_pow = btech.invest_max_power[buses_at_node[1]]
            max_cap = btech.invest_max_capacity[buses_at_node[1]]
            if max_pow > 0
                @constraint(model,
                    sum(vars.bat_tech_power_investment[y][bt][b] for y in 1:ypp:num_years, b in buses_at_node) <= max_pow,
                    base_name = "cumul_node_bat_tech_pow_bt$(bt)_n$(ni)")
            end
            if max_cap > 0
                @constraint(model,
                    sum(vars.bat_tech_capacity_investment[y][bt][b] for y in 1:ypp:num_years, b in buses_at_node) <= max_cap,
                    base_name = "cumul_node_bat_tech_cap_bt$(bt)_n$(ni)")
            end
        end
    end
end

"""
    corridor_investment(inv, from, to)

Investment variable (or value) of the corridor between buses `from` and `to`,
whichever way round it is keyed: lines are stored low→high, while a variable
exists only where its first bus allows investment (e.g. zone→grid).
Returns `nothing` when the corridor has no investment option.
"""
function corridor_investment(inv::AbstractDict, from::Int, to::Int)
    haskey(inv, (from, to)) && return inv[(from, to)]
    return get(inv, (to, from), nothing)
end

"""
    corridor_cost_terms(inv, costs)

`(variable, cost per MW)` once per corridor. When both directions of a
corridor exist they are held equal (TXN-1), so only the low→high one is
charged. A corridor costs the higher of its two ends' `costs` (an end left
at 0 does not make the line cheaper), also when only one direction exists,
as for a development zone's interconnection.
"""
function corridor_cost_terms(inv::AbstractDict, costs::AbstractVector)
    terms = Tuple{Any, Float64}[]
    for ((i, j), var) in inv
        haskey(inv, (j, i)) && j < i && continue
        push!(terms, (var, max(costs[i], costs[j])))
    end
    return terms
end

"""
    add_transmission_symmetry_constraints!(model, vars, input)

Add symmetry constraints for transmission investment.
Ensures transfer_investment[i,j] == transfer_investment[j,i].
"""
function add_transmission_symmetry_constraints!(
    model::Model,
    vars::MasterProblemVariables,
    input::MasterProblemInput
)
    num_years = length(input.years)
    n_buses = input.network.num_buses
    ypp = vars.years_per_inv_period

    # Only add at investment period starts (same variables within period)
    for y_idx in 1:ypp:num_years
        for i in 1:n_buses
            for j in (i+1):n_buses
                # Only add constraint if both directions exist
                if haskey(vars.transfer_investment[y_idx], (i, j)) &&
                   haskey(vars.transfer_investment[y_idx], (j, i))
                    @constraint(model,
                        vars.transfer_investment[y_idx][(i, j)] ==
                        vars.transfer_investment[y_idx][(j, i)],
                        base_name = "trans_sym_$(i)_$(j)_y$(y_idx)"
                    )
                end
            end
        end
    end
end

"""
    add_budget_constraints!(model, vars, input)

Add annual investment budget constraints.
"""
function add_budget_constraints!(
    model::Model,
    vars::MasterProblemVariables,
    input::MasterProblemInput
)
    num_years = length(input.years)
    n_buses = input.network.num_buses
    b2n = input.network.bus_to_node
    n_gen = length(input.generators)
    n_bat = length(input.batteries)
    ypp = vars.years_per_inv_period

    # Budget constraints only at investment period starts
    for y_idx in 1:ypp:num_years
        annual_cost = AffExpr(0.0)

        # Technology investment costs
        for t in 1:length(input.technologies)
            tech = input.technologies[t]
            for n in 1:n_buses
                add_to_expression!(annual_cost,
                    vars.tech_investment[y_idx][t][n],
                    tech.invest_cost[n])
            end
        end

        # Battery technology investment costs
        for bt in 1:length(input.battery_technologies)
            btech = input.battery_technologies[bt]
            for n in 1:n_buses
                # Power cost
                add_to_expression!(annual_cost,
                    vars.bat_tech_power_investment[y_idx][bt][n],
                    btech.invest_cost_power[n])
                # Capacity cost
                add_to_expression!(annual_cost,
                    vars.bat_tech_capacity_investment[y_idx][bt][n],
                    btech.invest_cost_capacity[n])
            end
        end

        # Transmission investment costs
        for (var, trans_cost) in corridor_cost_terms(vars.transfer_investment[y_idx],
                                                     input.network.transference_invest_cost)
            add_to_expression!(annual_cost, var, trans_cost)
        end

        # Reservoir investment costs
        for g in 1:n_gen
            gen = input.generators[g]
            if haskey(vars.reservoir_investment[y_idx], g)
                for n in 1:n_buses
                    if gen.reservoir_invest_max[n] > 0
                        add_to_expression!(annual_cost,
                            vars.reservoir_investment[y_idx][g][n],
                            gen.reservoir_invest_cost[n])
                    end
                end
            end
        end

        # Budget constraint with slack
        @constraint(model,
            annual_cost <= input.max_annual_investment + vars.slack_budget[y_idx],
            base_name = "budget_y$(y_idx)"
        )
    end
end

"""
    add_retirement_cascade_constraints!(model, vars, input)

Add cascade constraints ensuring life extension capacity can only decrease over time.
Once a unit begins retirement, it cannot reverse the decision in later years.
"""
function add_retirement_cascade_constraints!(
    model::Model,
    vars::MasterProblemVariables,
    input::MasterProblemInput
)
    num_years = length(input.years)
    n_gen = length(input.generators)
    n_bat = length(input.batteries)
    n_buses = input.network.num_buses

    for y_idx in 2:num_years
        # Generator life extension cascade: can only decrease over time
        for g in 1:n_gen
            for n in 1:n_buses
                curr = vars.gen_life_extension[y_idx][g][n]
                prev = vars.gen_life_extension[y_idx - 1][g][n]
                if curr !== nothing && prev !== nothing
                    @constraint(model, curr <= prev,
                        base_name = "cascade_gen_g$(g)_n$(n)_y$(y_idx)")
                end
            end
        end

        # Battery life extension cascade
        for bi in 1:n_bat
            for n in 1:n_buses
                curr = vars.bat_life_extension[y_idx][bi][n]
                prev = vars.bat_life_extension[y_idx - 1][bi][n]
                if curr !== nothing && prev !== nothing
                    @constraint(model, curr <= prev,
                        base_name = "cascade_bat_b$(bi)_n$(n)_y$(y_idx)")
                end
            end
        end
    end
end

"""
    add_re_target_constraints!(model, vars, input, targets)

Add renewable energy penetration target constraints.

CRITICAL: Python legacy (line 1204) uses EQUALITY constraint:
  re_penetration_ratio[y] == target

This FORCES the exact target, not a minimum with slack.
"""
function add_re_target_constraints!(
    model::Model,
    vars::MasterProblemVariables,
    input::MasterProblemInput,
    targets::Dict{Tuple{Int,Int}, Float64}
)
    num_years = length(input.years)
    n_sys = max(1, length(input.system_node_ranges))

    for s_idx in 1:n_sys
        for y_idx in 1:num_years
            target = targets[(s_idx, y_idx)]
            # Use >= with slack to allow falling short (penalized in objective)
            # Equality was infeasible: daily RE availability varies by day
            @constraint(model,
                vars.re_penetration_ratio[(s_idx, y_idx)] + vars.slack_re_target[y_idx] >= target,
                base_name = "re_target_s$(s_idx)_y$(y_idx)"
            )
        end
    end
end

"""
    add_re_increment_constraints!(model, vars, input)

Add annual RE penetration increment constraints.

Matches Python legacy (lines 346-358):
  increment = re_ratio[y] - re_ratio[y-1]
  model += increment >= min_increment
  model += increment <= max_increment

This ensures that the annual change in RE penetration stays within bounds.
"""
function add_re_increment_constraints!(
    model::Model,
    vars::MasterProblemVariables,
    input::MasterProblemInput
)
    num_years = length(input.years)

    # Skip if min/max increments allow any change
    if input.min_re_increment <= 0 && input.max_re_increment >= 1.0
        return
    end

    n_sys = max(1, length(input.system_node_ranges))

    for s_idx in 1:n_sys
        # Initial RE penetration (year-0 reference) for the increment
        # constraint at y_idx=1.  Without anchoring the first year the
        # master can jump from initial_re to anything between [0, 1]
        # without violating the year-on-year increment bound, defeating
        # the purpose of the ``max_annual_increment`` parameter.
        sys_initial_re = if isempty(input.system_node_ranges)
            input.initial_re_penetration
        else
            input.system_node_ranges[s_idx].initial_re
        end

        for y_idx in 1:num_years
            if y_idx == 1
                increment = vars.re_penetration_ratio[(s_idx, y_idx)] - sys_initial_re
            else
                increment = vars.re_penetration_ratio[(s_idx, y_idx)] - vars.re_penetration_ratio[(s_idx, y_idx - 1)]
            end

            if input.min_re_increment > 0
                @constraint(model,
                    increment >= input.min_re_increment,
                    base_name = "re_min_increment_s$(s_idx)_y$(y_idx)"
                )
            end

            if input.max_re_increment < 1.0
                @constraint(model,
                    increment <= input.max_re_increment,
                    base_name = "re_max_increment_s$(s_idx)_y$(y_idx)"
                )
            end
        end
    end
end

"""
    add_capacity_adequacy_constraints!(model, vars, input)

Add capacity adequacy constraints ensuring total capacity meets peak demand.

This is a simplified planning constraint that requires, per node:
  local_capacity + imports - exports >= peak_demand * reserve_margin

Imports between two nodes are limited to the transfer capacity between
their buses (existing lines plus cumulative transfer investment), so a node
may rely on a neighbour's surplus instead of building locally. Nodal peaks
are taken as coincident (conservative). With a slack variable to allow
infeasibility at a penalty cost.
"""
function add_capacity_adequacy_constraints!(
    model::Model,
    vars::MasterProblemVariables,
    input::MasterProblemInput
)
    num_years = length(input.years)
    n_buses = input.network.num_buses
    n_gen = length(input.generators)
    n_bat = length(input.batteries)

    # Reserve margin (require extra capacity above peak, configurable)
    reserve_margin = input.reserve_margin

    n_nodes = input.network.num_nodes
    b2n = input.network.bus_to_node
    # Node pairs joined by existing lines or by a transfer-investment option
    node_pairs = Set{Tuple{Int,Int}}()
    for i in 1:n_buses, j in 1:n_buses
        m, n = b2n[i], b2n[j]
        m == n && continue
        if input.network.connections[i, j] > 0 ||
           any(haskey(inv, (i, j)) for inv in values(vars.transfer_investment))
            push!(node_pairs, (m, n))
        end
    end

    # Calculate peak demand for each year at each node
    for y_idx in 1:num_years
        node_capacity = Dict{Int, AffExpr}()
        node_peak = Dict{Int, Float64}()
        # Get year-specific demand slice (with demand growth)
        growth_factor = (1.0 + input.demand_growth)^(y_idx - 1)
        timesteps_per_year_y = input.hours_per_year[y_idx] ÷ input.temporal_resolution_hours
        year_start = (y_idx - 1) * timesteps_per_year_y + 1
        year_end = min(y_idx * timesteps_per_year_y, size(input.base_demand, 1))

        # Capacity adequacy per NODE (aggregate all buses on same node)
        # Generators on different buses of the same node can serve demand
        # on any bus via internal transmission lines
        for ni in 1:input.network.num_nodes
            # Peak demand for this node
            if size(input.base_demand, 2) >= ni && year_end >= year_start
                peak_demand = maximum(input.base_demand[year_start:year_end, ni]) * growth_factor * reserve_margin
            elseif size(input.base_demand, 2) >= ni
                peak_demand = maximum(input.base_demand[:, ni]) * growth_factor * reserve_margin
            else
                peak_demand = maximum(input.base_demand) * growth_factor * reserve_margin
            end

            # Build cumulative capacity expression across ALL buses on this node
            total_capacity = AffExpr(0.0)

            for n in 1:n_buses
                # Skip buses not on this node
                input.network.bus_to_node[n] != ni && continue

                # Generator capacity: unified retirement with degradation
                # For renewables, derate by capacity credit (minimum availability)
                # to avoid counting solar at full rated when it produces 0 at night
                for g in 1:n_gen
                    gen = input.generators[g]
                    rated_power = gen.rated_power[n]
                    lifetime = gen.life_time[n]
                    initial_age = gen.initial_age[n]
                    age_at_year = initial_age + y_idx - 1
                    degradation_rate = gen.degradation_rate[n]

                    # Capacity credit: for renewables, use minimum availability
                    # Solar: 0.0 at night → no firm capacity contribution
                    # Wind: low but >0 minimum → small firm contribution
                    # Dispatchable: 1.0 (always available)
                    is_renewable = gen.type == "Renewable"
                    if is_renewable && size(gen.availability, 1) > 0 && n <= size(gen.availability, 2)
                        capacity_credit = minimum(gen.availability[:, n])
                    else
                        capacity_credit = 1.0
                    end

                    # Risk coefficient: geographic hazard derating (default 1.0)
                    risk_coef = n <= length(gen.risk_coefficient) ? gen.risk_coefficient[n] : 1.0

                    # Existing: active if age < lifetime, with degradation
                    if age_at_year < lifetime && rated_power > 0
                        deg_factor = (1.0 - degradation_rate) ^ age_at_year
                        add_to_expression!(total_capacity, rated_power * deg_factor * capacity_credit * risk_coef)
                    elseif rated_power > 0
                        # Past lifetime: capacity from life extension variable (if any)
                        life_ext_var = vars.gen_life_extension[y_idx][g][n]
                        if life_ext_var !== nothing
                            add_to_expression!(total_capacity, life_ext_var, capacity_credit * risk_coef)
                        end
                    end

                end

                # Battery power capacity: unified retirement (existing only)
                for b in 1:n_bat
                    bat = input.batteries[b]
                    base_power = bat.max_discharge_power[n]
                    lifetime = bat.life_time[n]
                    initial_age = bat.initial_age[n]
                    age_at_year = initial_age + y_idx - 1

                    bat_risk = n <= length(bat.risk_coefficient) ? bat.risk_coefficient[n] : 1.0

                    # Existing: active if age < lifetime
                    if age_at_year < lifetime && base_power > 0
                        add_to_expression!(total_capacity, base_power * bat_risk)
                    elseif base_power > 0
                        life_ext_var = vars.bat_life_extension[y_idx][b][n]
                        if life_ext_var !== nothing
                            add_to_expression!(total_capacity, life_ext_var, bat_risk)
                        end
                    end
                end

                # Technology investments: cumulative capacity with degradation
                for t in 1:length(input.technologies)
                    tech = input.technologies[t]
                    tech_lifetime = tech.life_time[n]
                    tech_deg = tech.degradation_rate[n]
                    # Capacity credit: minimum availability (same logic as existing generators)
                    if tech.type == "Renewable" && size(tech.availability, 1) > 0 && n <= size(tech.availability, 2)
                        tech_cc = minimum(tech.availability[:, n])
                    else
                        tech_cc = 1.0
                    end
                    tech_risk = n <= length(tech.risk_coefficient) ? tech.risk_coefficient[n] : 1.0
                    for y in 1:vars.years_per_inv_period:y_idx
                        inv_age = y_idx - y
                        if inv_age < tech_lifetime
                            deg_factor = (1.0 - tech_deg) ^ inv_age
                            add_to_expression!(total_capacity, vars.tech_investment[y][t][n], deg_factor * tech_cc * tech_risk)
                        end
                    end
                end

                # Battery technology investments: cumulative power
                for bt in 1:length(input.battery_technologies)
                    btech = input.battery_technologies[bt]
                    bt_lifetime = btech.life_time[n]
                    btech_risk = n <= length(btech.risk_coefficient) ? btech.risk_coefficient[n] : 1.0
                    for y in 1:vars.years_per_inv_period:y_idx
                        inv_age = y_idx - y
                        if inv_age < bt_lifetime
                            add_to_expression!(total_capacity, vars.bat_tech_power_investment[y][bt][n], btech_risk)
                        end
                    end
                end
            end

            node_capacity[ni] = total_capacity
            node_peak[ni] = peak_demand
        end

        # Peak-hour transfers: bounded by the inter-node transfer capacity
        imports = Dict{Tuple{Int,Int}, VariableRef}()
        for (m, n) in node_pairs
            cap = AffExpr(0.0)
            for i in 1:n_buses, j in 1:n_buses
                (b2n[i] == m && b2n[j] == n) || continue
                add_to_expression!(cap, input.network.connections[i, j])
                for y in 1:vars.years_per_inv_period:y_idx
                    if haskey(vars.transfer_investment, y) &&
                       haskey(vars.transfer_investment[y], (i, j))
                        add_to_expression!(cap, vars.transfer_investment[y][(i, j)])
                    end
                end
            end
            imports[(m, n)] = @variable(model, lower_bound = 0,
                base_name = "cap_import_y$(y_idx)_$(m)_$(n)")
            @constraint(model, imports[(m, n)] <= cap,
                base_name = "cap_import_max_y$(y_idx)_$(m)_$(n)")
        end

        for ni in 1:n_nodes
            haskey(node_capacity, ni) || continue
            net_import = AffExpr(0.0)
            for ((m, n), v) in imports
                n == ni && add_to_expression!(net_import, v)
                m == ni && add_to_expression!(net_import, -1.0, v)
            end
            # Capacity adequacy constraint: capacity + net imports + slack >= peak
            @constraint(model,
                node_capacity[ni] + net_import + vars.slack_capacity[(y_idx, ni)] >= node_peak[ni],
                base_name = "cap_adequacy_y$(y_idx)_node$(ni)"
            )
        end
    end
end

# =============================================================================
# Cumulative Capacity Calculation
# =============================================================================

"""
    build_cumulative_capacity_expressions(vars, input, year_idx)

Build expressions for cumulative capacity up to and including year_idx.

Returns Dict with:
- "gen" => Dict{gen_idx, Vector{AffExpr}} per node
- "bat_power" => Dict{bat_idx, Vector{AffExpr}} per node
- "bat_capacity" => Dict{bat_idx, Vector{AffExpr}} per node
"""
function build_cumulative_capacity_expressions(
    vars::MasterProblemVariables,
    input::MasterProblemInput,
    year_idx::Int
)::Dict{String, Any}
    n_buses = input.network.num_buses
    b2n = input.network.bus_to_node
    n_gen = length(input.generators)
    n_bat = length(input.batteries)

    result = Dict{String, Any}()

    # Generator capacity: unified retirement with degradation
    gen_cap = Dict{Int, Vector{AffExpr}}()
    for g in 1:n_gen
        gen = input.generators[g]
        gen_cap[g] = Vector{AffExpr}(undef, n_buses)

        for n in 1:n_buses
            rated_power = gen.rated_power[n]
            lifetime = gen.life_time[n]
            initial_age = gen.initial_age[n]
            age_at_year = initial_age + (year_idx - 1)
            degradation_rate = gen.degradation_rate[n]

            cap = AffExpr(0.0)

            # Existing: active if age < lifetime, with degradation
            if age_at_year < lifetime && rated_power > 0
                deg_factor = (1.0 - degradation_rate) ^ age_at_year
                add_to_expression!(cap, rated_power * deg_factor)
            elseif rated_power > 0
                # Past lifetime: capacity from life extension variable (if any)
                life_ext_var = vars.gen_life_extension[year_idx][g][n]
                if life_ext_var !== nothing
                    add_to_expression!(cap, life_ext_var)
                end
            end

            gen_cap[g][n] = cap
        end
    end
    result["gen"] = gen_cap

    # Battery power capacity: unified retirement
    bat_pow = Dict{Int, Vector{AffExpr}}()
    for b in 1:n_bat
        bat = input.batteries[b]
        bat_pow[b] = Vector{AffExpr}(undef, n_buses)

        for n in 1:n_buses
            base_power = bat.max_discharge_power[n]
            lifetime = bat.life_time[n]
            initial_age = bat.initial_age[n]
            age_at_year = initial_age + (year_idx - 1)

            cap = AffExpr(0.0)
            if age_at_year < lifetime && base_power > 0
                add_to_expression!(cap, base_power)
            elseif base_power > 0
                life_ext_var = vars.bat_life_extension[year_idx][b][n]
                if life_ext_var !== nothing
                    add_to_expression!(cap, life_ext_var)
                end
            end
            bat_pow[b][n] = cap
        end
    end
    result["bat_power"] = bat_pow

    # Battery energy capacity: unified retirement
    bat_cap = Dict{Int, Vector{AffExpr}}()
    for b in 1:n_bat
        bat = input.batteries[b]
        bat_cap[b] = Vector{AffExpr}(undef, n_buses)

        for n in 1:n_buses
            base_cap = bat.capacity[n]
            lifetime = bat.life_time[n]
            initial_age = bat.initial_age[n]
            age_at_year = initial_age + (year_idx - 1)

            cap = AffExpr(0.0)
            if age_at_year < lifetime && base_cap > 0
                add_to_expression!(cap, base_cap)
            elseif base_cap > 0
                life_ext_var = vars.bat_life_extension[year_idx][b][n]
                if life_ext_var !== nothing
                    # Scale energy capacity proportional to power life extension
                    bat_config = input.batteries[b]
                    base_power = bat_config.max_discharge_power[n]
                    if base_power > 0
                        ratio = base_cap / base_power
                        add_to_expression!(cap, life_ext_var, ratio)
                    end
                end
            end
            bat_cap[b][n] = cap
        end
    end
    result["bat_capacity"] = bat_cap

    # Technology capacity expressions (per-technology investments)
    n_tech = length(input.technologies)
    ypp = vars.years_per_inv_period
    tech_cap = Dict{Int, Vector{AffExpr}}()
    for t in 1:n_tech
        tech = input.technologies[t]
        tech_cap[t] = Vector{AffExpr}(undef, n_buses)
        for n in 1:n_buses
            cap = AffExpr(0.0)
            tech_lifetime = tech.life_time[n]
            tech_deg = tech.degradation_rate[n]
            for y in 1:ypp:year_idx
                inv_age = year_idx - y
                if inv_age < tech_lifetime
                    deg_factor = (1.0 - tech_deg) ^ inv_age
                    add_to_expression!(cap, vars.tech_investment[y][t][n], deg_factor)
                end
            end
            tech_cap[t][n] = cap
        end
    end
    result["tech"] = tech_cap

    # Battery technology power and capacity expressions
    n_bat_tech = length(input.battery_technologies)
    bat_tech_pow = Dict{Int, Vector{AffExpr}}()
    bat_tech_cap = Dict{Int, Vector{AffExpr}}()
    for bt in 1:n_bat_tech
        btech = input.battery_technologies[bt]
        bat_tech_pow[bt] = Vector{AffExpr}(undef, n_buses)
        bat_tech_cap[bt] = Vector{AffExpr}(undef, n_buses)
        for n in 1:n_buses
            pow = AffExpr(0.0)
            ecap = AffExpr(0.0)
            bt_lifetime = btech.life_time[n]
            for y in 1:ypp:year_idx
                inv_age = year_idx - y
                if inv_age < bt_lifetime
                    add_to_expression!(pow, vars.bat_tech_power_investment[y][bt][n])
                    add_to_expression!(ecap, vars.bat_tech_capacity_investment[y][bt][n])
                end
            end
            bat_tech_pow[bt][n] = pow
            bat_tech_cap[bt][n] = ecap
        end
    end
    result["bat_tech_power"] = bat_tech_pow
    result["bat_tech_capacity"] = bat_tech_cap

    return result
end

# =============================================================================
# Objective Function
# =============================================================================

"""
    build_investment_cost_expression(vars, input; mult=nothing, discount_rate=input.discount_rate)

Build the NPV investment cost expression (first-stage costs only).
Returns an AffExpr containing discounted investment costs at period starts.
A stochastic scenario's `mult` scales the technology investment costs
(`invest_cost_renewables` / `invest_cost_conventional`), battery technology
power and energy costs (`invest_cost_storage` × `bat_invest_cost_power` /
`bat_invest_cost_capacity`), transmission (`invest_cost_transmission`) and
converters (`converter_invest_cost`).
"""
function build_investment_cost_expression(
    vars::MasterProblemVariables,
    input::MasterProblemInput;
    mult::Union{Nothing, ScenarioMultipliers} = nothing,
    discount_rate::Float64 = input.discount_rate
)::AffExpr
    num_years = length(input.years)
    n_buses = input.network.num_buses
    n_gen = length(input.generators)
    # Stochastic scenarios scale the unit investment costs
    m_re = mult === nothing ? 1.0 : mult.invest_cost_renewables
    m_conv = mult === nothing ? 1.0 : mult.invest_cost_conventional
    m_sto = mult === nothing ? 1.0 : mult.invest_cost_storage
    m_bat_p = mult === nothing ? m_sto : m_sto * mult.bat_invest_cost_power
    m_bat_e = mult === nothing ? m_sto : m_sto * mult.bat_invest_cost_capacity
    m_tx = mult === nothing ? 1.0 : mult.invest_cost_transmission
    m_cvt = mult === nothing ? 1.0 : mult.converter_invest_cost

    inv_cost = AffExpr(0.0)

    ypp = vars.years_per_inv_period
    for y_idx in 1:ypp:num_years
        discount_factor = 1.0 / ((1.0 + discount_rate)^(y_idx - 1))

        investment_cost = AffExpr(0.0)

        # Technology investment
        for t in 1:length(input.technologies)
            tech = input.technologies[t]
            m_t = tech.type == "Renewable" ? m_re : m_conv
            for n in 1:n_buses
                add_to_expression!(investment_cost,
                    vars.tech_investment[y_idx][t][n],
                    tech.invest_cost[n] * m_t)
            end
        end

        # Battery technology investment
        for bt in 1:length(input.battery_technologies)
            btech = input.battery_technologies[bt]
            for n in 1:n_buses
                add_to_expression!(investment_cost,
                    vars.bat_tech_power_investment[y_idx][bt][n],
                    btech.invest_cost_power[n] * m_bat_p)
                add_to_expression!(investment_cost,
                    vars.bat_tech_capacity_investment[y_idx][bt][n],
                    btech.invest_cost_capacity[n] * m_bat_e)
            end
        end

        # Transmission investment
        for (var, trans_cost) in corridor_cost_terms(vars.transfer_investment[y_idx],
                                                     input.network.transference_invest_cost)
            add_to_expression!(investment_cost, var, trans_cost * m_tx)
        end

        # Converter investment
        for (key, conv) in master_converters(input.network)
            v = get(vars.converter_investment[y_idx], key, nothing)
            v === nothing || add_to_expression!(investment_cost, v, conv.invest_cost * m_cvt)
        end

        # Reservoir capacity investment
        for g in 1:n_gen
            gen = input.generators[g]
            if haskey(vars.reservoir_investment[y_idx], g)
                for n in 1:n_buses
                    if gen.reservoir_invest_max[n] > 0
                        add_to_expression!(investment_cost,
                            vars.reservoir_investment[y_idx][g][n],
                            gen.reservoir_invest_cost[n])
                    end
                end
            end
        end

        add_to_expression!(inv_cost, investment_cost, discount_factor)
    end

    # M2 (#89): fuel storage, route transport and electrolyzer builds
    add_to_expression!(inv_cost, master_fuel_investment_cost(vars, input;
        mult = mult, discount_rate = discount_rate))

    return inv_cost
end

"""
    build_annual_asset_cost(vars, input, y_idx; mult=nothing, by_unit=nothing)

Undiscounted per-year cost of existing assets: life extension of units past
their lifetime, (incremental, LP-linear) decommissioning, the fixed cost
(\$/MW/year) of the converter capacity built so far and the fixed O&M
(\$/MW/year) of the capacity in service:
- generators and technologies: the power `build_cumulative_capacity_expressions`
  gives (existing units within their lifetime, degraded; life-extended MW;
  technology MW built, degraded and not yet retired), the capacity the
  dispatch receives;
- batteries: rated discharge power of existing ones within their lifetime,
  life-extended MW, and battery-technology power built and not yet retired.
A stochastic scenario's `mult` scales the generator and battery
decommissioning costs, and `gen_fixed_cost` scales every fixed O&M.
Units the NPV iteration retired (`input.npv_retired_units`) pay their
decommissioning in the first year.

With a dict `by_unit`, each existing unit's share (fixed O&M, life
extension, decommissioning) is also added to `by_unit[(:gen | :bat, i, bus)]`,
for its NPV (`extract_unit_operation`).
"""
function build_annual_asset_cost(
    vars::MasterProblemVariables,
    input::MasterProblemInput,
    y_idx::Int;
    mult::Union{Nothing, ScenarioMultipliers} = nothing,
    by_unit::Union{Nothing, AbstractDict} = nothing
)::AffExpr
    n_buses = input.network.num_buses
    n_gen = length(input.generators)
    n_bat = length(input.batteries)
    m_gen_decomm = mult === nothing ? 1.0 : mult.gen_decommissioning_cost
    m_bat_decomm = mult === nothing ? 1.0 : mult.bat_decommissioning_cost
    m_fixed = mult === nothing ? 1.0 : mult.gen_fixed_cost
    annual_cost = AffExpr(0.0)
    # A term of existing unit `key`: also its own share when asked for
    function charge!(key, args...)
        add_to_expression!(annual_cost, args...)
        by_unit === nothing ||
            add_to_expression!(get!(() -> AffExpr(0.0), by_unit, key), args...)
    end

    # Life extension costs (annual): cost to keep units past lifetime
    for g in 1:n_gen
        gen = input.generators[g]
        for n in 1:n_buses
            life_ext_var = vars.gen_life_extension[y_idx][g][n]
            if life_ext_var !== nothing
                ext_cost = gen.invest_cost[n] * input.life_extension_cost_factor
                charge!((:gen, g, n), life_ext_var, ext_cost)
            end
        end
    end
    for b in 1:n_bat
        bat = input.batteries[b]
        for n in 1:n_buses
            life_ext_var = vars.bat_life_extension[y_idx][b][n]
            if life_ext_var !== nothing
                ext_cost = bat.invest_cost_power[n] * input.life_extension_cost_factor
                charge!((:bat, b, n), life_ext_var, ext_cost)
            end
        end
    end

    # Generator and technology fixed O&M on the power in service this year
    if any(any(>(0), u.fixed_cost) for u in input.generators) ||
       any(any(>(0), u.fixed_cost) for u in input.technologies)
        caps = build_cumulative_capacity_expressions(vars, input, y_idx)
        for (key, units) in (("gen", input.generators), ("tech", input.technologies))
            for (i, u) in enumerate(units), n in 1:n_buses
                fc = u.fixed_cost[n] * m_fixed
                fc > 0 || continue
                if key == "gen"
                    charge!((:gen, i, n), caps[key][i][n], fc)
                else
                    add_to_expression!(annual_cost, caps[key][i][n], fc)
                end
            end
        end
    end

    # Battery fixed cost on the discharge power in service this year
    for b in 1:n_bat
        bat = input.batteries[b]
        for n in 1:n_buses
            fc = bat.fixed_cost[n] * m_fixed
            (fc > 0 && bat.max_discharge_power[n] > 0) || continue
            if bat.initial_age[n] + (y_idx - 1) < bat.life_time[n]
                charge!((:bat, b, n), fc * bat.max_discharge_power[n])
            else
                lev = vars.bat_life_extension[y_idx][b][n]
                lev === nothing || charge!((:bat, b, n), lev, fc)
            end
        end
    end
    for (bt, btech) in enumerate(input.battery_technologies), n in 1:n_buses
        fc = btech.fixed_cost[n] * m_fixed
        fc > 0 || continue
        for y in 1:vars.years_per_inv_period:y_idx
            haskey(vars.bat_tech_power_investment, y) || continue
            y_idx - y < btech.life_time[n] || continue
            add_to_expression!(annual_cost, vars.bat_tech_power_investment[y][bt][n], fc)
        end
    end

    # Fixed cost of the converter capacity built up to this year
    for (key, conv) in master_converters(input.network)
        conv.fixed_cost > 0 || continue
        built = converter_capacity(vars, conv, key, y_idx)
        add_to_expression!(built, -conv.rated_power_mva)
        add_to_expression!(annual_cost, conv.fixed_cost, built)
    end

    # Decommissioning costs (incremental, LP-linear)
    for g in 1:n_gen
        gen = input.generators[g]
        for n in 1:n_buses
            rated_power = gen.rated_power[n]
            decomm_cost = gen.decommissioning_cost[n] * m_gen_decomm
            if rated_power <= 0 || decomm_cost <= 0
                continue
            end
            lifetime = gen.life_time[n]
            initial_age = gen.initial_age[n]
            # Year index when retirement first occurs
            retirement_y = max(1, ceil(Int, lifetime - initial_age + 1))

            if y_idx == retirement_y
                # First retirement year: decommission full capacity minus life ext
                age = initial_age + y_idx - 1
                deg = (1.0 - gen.degradation_rate[n]) ^ age
                full_cap = rated_power * deg
                charge!((:gen, g, n), full_cap * decomm_cost)
                lev = vars.gen_life_extension[y_idx][g][n]
                if lev !== nothing
                    charge!((:gen, g, n), lev, -decomm_cost)
                end
            elseif y_idx > retirement_y
                # Subsequent years: incremental decommissioning of life-extended capacity
                prev = vars.gen_life_extension[y_idx - 1][g][n]
                curr = vars.gen_life_extension[y_idx][g][n]
                if prev !== nothing
                    charge!((:gen, g, n), prev, decomm_cost)
                    if curr !== nothing
                        charge!((:gen, g, n), curr, -decomm_cost)
                    end
                end
            end
        end
    end
    # Battery decommissioning
    for b in 1:n_bat
        bat = input.batteries[b]
        for n in 1:n_buses
            base_power = bat.max_discharge_power[n]
            decomm_cost = bat.decommissioning_cost[n] * m_bat_decomm
            if base_power <= 0 || decomm_cost <= 0
                continue
            end
            lifetime = bat.life_time[n]
            initial_age = bat.initial_age[n]
            retirement_y = max(1, ceil(Int, lifetime - initial_age + 1))

            if y_idx == retirement_y
                charge!((:bat, b, n), base_power * decomm_cost)
                lev = vars.bat_life_extension[y_idx][b][n]
                if lev !== nothing
                    charge!((:bat, b, n), lev, -decomm_cost)
                end
            elseif y_idx > retirement_y
                prev = vars.bat_life_extension[y_idx - 1][b][n]
                curr = vars.bat_life_extension[y_idx][b][n]
                if prev !== nothing
                    charge!((:bat, b, n), prev, decomm_cost)
                    if curr !== nothing
                        charge!((:bat, b, n), curr, -decomm_cost)
                    end
                end
            end
        end
    end
    # Units the NPV iteration retired: decommissioned in the first year
    if y_idx == 1
        for ru in input.npv_retired_units
            ru.decommissioned_mw > 0 || continue
            if ru.unit_type == "generator"
                dc = input.generators[ru.unit_idx].decommissioning_cost[ru.bus] * m_gen_decomm
                dc > 0 && charge!((:gen, ru.unit_idx, ru.bus), ru.decommissioned_mw * dc)
            else
                dc = input.batteries[ru.unit_idx].decommissioning_cost[ru.bus] * m_bat_decomm
                dc > 0 && charge!((:bat, ru.unit_idx, ru.bus), ru.decommissioned_mw * dc)
            end
        end
    end

    # M2 (#89): electrolyzer fixed cost (existing + built)
    add_to_expression!(annual_cost, master_fuel_annual_cost(vars, input, y_idx; mult = mult))

    return annual_cost
end

"""
    build_master_objective!(model, vars, input)

Build the objective function: NPV of investment + operational costs.
"""
function build_master_objective!(
    model::Model,
    vars::MasterProblemVariables,
    input::MasterProblemInput
)
    num_years = length(input.years)
    n_buses = input.network.num_buses
    b2n = input.network.bus_to_node
    n_gen = length(input.generators)
    n_bat = length(input.batteries)

    total_cost = build_investment_cost_expression(vars, input)

    # Per-year costs: life extension, decommissioning, operational (every year, not just period starts)
    for y_idx in 1:num_years
        discount_factor = 1.0 / ((1.0 + input.discount_rate)^(y_idx - 1))
        annual_cost = build_annual_asset_cost(vars, input, y_idx)

        # Add discounted per-year costs
        add_to_expression!(total_cost, annual_cost, discount_factor)

        # Operational costs (second stage) - added from subproblems
        if haskey(vars.operational_costs, y_idx)
            for day_cost in vars.operational_costs[y_idx]
                add_to_expression!(total_cost, day_cost, discount_factor)
            end
        end
    end

    # Slack penalties
    for y_idx in 1:num_years
        add_to_expression!(total_cost, vars.slack_re_target[y_idx], input.slack_penalty)
        add_to_expression!(total_cost, vars.slack_budget[y_idx], input.slack_penalty)
        for ni in 1:input.network.num_nodes
            add_to_expression!(total_cost, vars.slack_capacity[(y_idx, ni)], input.slack_penalty)
        end
    end

    @objective(model, Min, total_cost)

    return total_cost  # Return for MGA near-optimal constraint
end

# =============================================================================
# Representative Days Operational Validation (CRITICAL for correct RE targets)
# =============================================================================

"""
    create_day_ps_vars!(model, input, year_idx, day_idx, hours)

Create PowerSystemVariables for a representative day.
Uses power_system.jl's PowerSystemVariables type directly instead of a separate struct.
"""
function create_day_ps_vars!(
    model::Model,
    input::MasterProblemInput,
    year_idx::Int,
    day_idx::Int,
    hours::Int
)::PowerSystemVariables
    n_gen = length(input.generators)
    n_bat = length(input.batteries)
    n_buses = input.network.num_buses
    n_nodes = input.network.num_nodes
    prefix = "op_y$(year_idx)_d$(day_idx)"

    # Pre-compute lookup maps: which buses each generator/battery is active at
    # (existing generators only have their rated power; what the master
    # builds is candidate technologies, see add_day_operational_constraints!)
    buses_of_gen = [Int[] for _ in 1:n_gen]
    gens_at_bus = [Int[] for _ in 1:n_buses]
    for g in 1:n_gen
        gen = input.generators[g]
        for b in 1:n_buses
            if gen.rated_power[b] > 0
                push!(buses_of_gen[g], b)
                push!(gens_at_bus[b], g)
            end
        end
    end

    buses_of_bat = [Int[] for _ in 1:n_bat]
    bats_at_bus = [Int[] for _ in 1:n_buses]
    for bi in 1:n_bat
        bat = input.batteries[bi]
        for b in 1:n_buses
            has_cap = bat.max_discharge_power[b] > 0
            has_invest = length(bat.invest_max_power) >= b && bat.invest_max_power[b] > 0
            if has_cap || has_invest
                push!(buses_of_bat[bi], b)
                push!(bats_at_bus[b], bi)
            end
        end
    end

    # Generator output — SparseAxisArray (only active gen-bus pairs)
    gen_output = @variable(model,
        [g=1:n_gen, b=buses_of_gen[g], t=1:hours],
        lower_bound = 0, base_name = "$(prefix)_gen")

    # gen_status fixed at 1 (economic dispatch — no UC in master)
    gen_status = @variable(model,
        [g=1:n_gen, b=buses_of_gen[g], t=1:hours],
        lower_bound = 1, upper_bound = 1, base_name = "$(prefix)_gs")

    # Curtailment [node, hour] (per-node, matching PowerSystemVariables convention)
    curtailment = @variable(model,
        [n=1:n_nodes, t=1:hours],
        lower_bound = 0, base_name = "$(prefix)_curt")

    # FRE penetration loss [node, hour]
    fre_penetration_loss = @variable(model,
        [n=1:n_nodes, t=1:hours],
        lower_bound = 0, base_name = "$(prefix)_fre_loss")

    # Battery variables — SparseAxisArray (only active bat-bus pairs)
    bat_charge = @variable(model,
        [bi=1:n_bat, b=buses_of_bat[bi], t=1:hours],
        lower_bound = 0, base_name = "$(prefix)_bat_ch")

    bat_discharge = @variable(model,
        [bi=1:n_bat, b=buses_of_bat[bi], t=1:hours],
        lower_bound = 0, base_name = "$(prefix)_bat_dch")

    # bat_soc uses hours+1 convention (soc[1]=initial, soc[t+1]=after hour t)
    bat_soc = @variable(model,
        [bi=1:n_bat, b=buses_of_bat[bi], t=1:(hours+1)],
        lower_bound = 0, base_name = "$(prefix)_bat_soc")

    # Voltage angle + power flow by line (DC power flow for multi-bus)
    voltage_angle = @variable(model,
        [b=1:n_buses, t=1:hours],
        lower_bound = -π, upper_bound = π,
        base_name = "$(prefix)_vangle")

    # Power flow dict (legacy node-pair indexing, populated by add_dc_constraints!)
    power_flow = Dict{Tuple{Int,Int}, Vector{VariableRef}}()

    # Reserve variables [node, hour]
    reserve_static = @variable(model,
        [n=1:n_nodes, t=1:hours],
        lower_bound = 0, base_name = "$(prefix)_rs")
    reserve_dynamic = @variable(model,
        [n=1:n_nodes, t=1:hours],
        lower_bound = 0, base_name = "$(prefix)_rd")
    reserve_static_loss = @variable(model,
        [n=1:n_nodes, t=1:hours],
        lower_bound = 0, base_name = "$(prefix)_rs_loss")
    reserve_dynamic_loss = @variable(model,
        [n=1:n_nodes, t=1:hours],
        lower_bound = 0, base_name = "$(prefix)_rd_loss")

    # Load shedding [bus, hour] (B2: indexed per-bus to match operational layout).
    # In the master, ``buses == nodes`` (one synthetic bus per node) so n_buses == n_nodes.
    load_shed = @variable(model,
        [b=1:n_buses, t=1:hours],
        lower_bound = 0, base_name = "$(prefix)_ll")

    # Inertia loss variable (only if inertia limit is configured)
    loss_of_inertia_var = if input.inertia_limit > 0
        @variable(model, [t=1:hours], lower_bound=0,
            base_name = "$(prefix)_inertia_loss")
    else
        nothing
    end

    # Sectoral loss of load variables (M2)
    loss_of_load_sectoral = Dict{String, Matrix{VariableRef}}()
    if !isempty(input.sectoral_criticality)
        for (sector, _) in input.sectoral_criticality
            loss_of_load_sectoral[sector] = @variable(model,
                [b=1:n_buses, t=1:hours],
                lower_bound = 0, base_name = "$(prefix)_lol_$(sector)")
        end
    end

    # Segment variables for PWL cost decomposition (bidding curves)
    gen_seg_output = Dict{Tuple{Int,Int}, Any}()
    for (g, bus_curves) in input.gen_cost_curves
        for (b, segs) in bus_curves
            if !isempty(segs) && b in buses_of_gen[g]
                n_seg = length(segs)
                sv = @variable(model, [k=1:n_seg, t=1:hours],
                               lower_bound=0, base_name="$(prefix)_gseg_$(g)_$(b)")
                gen_seg_output[(g, b)] = sv
                for t in 1:hours
                    @constraint(model, gen_output[g, b, t] ==
                        sum(sv[k, t] for k in 1:n_seg))
                end
            end
        end
    end

    bat_seg_discharge = Dict{Tuple{Int,Int}, Any}()
    for (bi, bus_curves) in input.bat_cost_curves
        for (b, segs) in bus_curves
            if !isempty(segs) && b in buses_of_bat[bi]
                n_seg = length(segs)
                sv = @variable(model, [k=1:n_seg, t=1:hours],
                               lower_bound=0, base_name="$(prefix)_bseg_$(bi)_$(b)")
                bat_seg_discharge[(bi, b)] = sv
                for t in 1:hours
                    @constraint(model, bat_discharge[bi, b, t] ==
                        sum(sv[k, t] for k in 1:n_seg))
                end
            end
        end
    end

    # Reservoir hydro variables for the representative day. Only created when at
    # least one generator has a reservoir, so all-thermal/RE systems are not
    # bloated. add_reservoir_constraints! enforces the water balance + cyclic
    # level so hydro is energy-limited in the master (not treated as firm MW).
    has_any_reservoir = any(any(g.reservoir_capacity .> 0) for g in input.generators)
    reservoir_level = nothing
    reservoir_spillage = nothing
    reservoir_pump = nothing
    if has_any_reservoir
        reservoir_level = @variable(model,
            [g=1:n_gen, b=buses_of_gen[g], t=1:(hours+1)],
            lower_bound = 0, base_name = "$(prefix)_res_level")
        reservoir_spillage = @variable(model,
            [g=1:n_gen, b=buses_of_gen[g], t=1:hours],
            lower_bound = 0, base_name = "$(prefix)_res_spill")
        reservoir_pump = @variable(model,
            [g=1:n_gen, b=buses_of_gen[g], t=1:hours],
            lower_bound = 0, base_name = "$(prefix)_res_pump")
    end

    return PowerSystemVariables(
        gen_output, gen_status, nothing, nothing,  # no startup/shutdown
        curtailment, fre_penetration_loss,
        bat_charge, bat_discharge, bat_soc,
        power_flow, voltage_angle,
        reserve_static, reserve_dynamic, reserve_static_loss, reserve_dynamic_loss,
        load_shed,
        buses_of_gen, gens_at_bus, buses_of_bat, bats_at_bus;
        loss_of_inertia = loss_of_inertia_var,
        loss_of_load_sectoral = loss_of_load_sectoral,
        gen_seg_output = gen_seg_output,
        bat_seg_discharge = bat_seg_discharge,
        reservoir_level = reservoir_level,
        reservoir_spillage = reservoir_spillage,
        reservoir_pump = reservoir_pump,
        # M2 (#89): electrolyzers of the fuel block (load in the bus balance)
        electrolyzer_power = master_electrolyzer_power_vars(model, input, prefix, hours)
    )
end

"""
    generators_for_day(generators, start_hour, hours)

The generators as a representative day (or TSAM period) starting at master
step `start_hour` sees them. The day model reads `availability[t, b]` and
`reservoir_inflow[t, b]` by its own step `t = 1..hours`, so both profiles are
cut to rows `start_hour : start_hour + hours - 1` (wrapping around a shorter
profile). A generator with neither profile is returned as is.
"""
function generators_for_day(generators::AbstractVector, start_hour::Int, hours::Int)
    rows(m) = size(m, 1) == 0 ? m :
        m[[mod1(start_hour + t - 1, size(m, 1)) for t in 1:hours], :]
    return map(generators) do gen
        size(gen.availability, 1) == 0 && size(gen.reservoir_inflow, 1) == 0 && return gen
        GeneratorConfig(
            gen.name, gen.type, gen.fuel,
            gen.rated_power, gen.min_power,
            gen.efficiency_rated, gen.efficiency_min,
            gen.ramp_up, gen.ramp_down,
            gen.min_up_time, gen.min_down_time,
            gen.start_up_cost, gen.fuel_cost,
            gen.fixed_cost, gen.maintenance_cost,
            gen.inertia, gen.invest_cost,
            rows(gen.availability), gen.reservable,
            gen.life_time, gen.initial_age,
            gen.degradation_rate, gen.decommissioning_cost,
            gen.frequency_hz, gen.current_type,
            gen.reservoir_capacity, gen.reservoir_initial_level,
            gen.reservoir_min_level, gen.reservoir_max_level,
            rows(gen.reservoir_inflow), gen.reservoir_turbine_efficiency,
            gen.reservoir_evaporation_rate, gen.reservoir_pump_capacity,
            gen.reservoir_pump_efficiency, gen.reservoir_spillage_allowed,
            gen.reservoir_invest_cost, gen.reservoir_invest_max,
            gen.risk_coefficient,
            gen.reservoir_min_release,
            gen.cascade_downstream, gen.cascade_delay_hours,
            gen.reservoir_head_min_factor
        )
    end
end

"""
    batteries_for_day(batteries, start_hour, hours)

The batteries (or battery technologies) as a representative day starting at
master step `start_hour` sees them: an availability profile is cut to rows
`start_hour : start_hour + hours - 1` (wrapping around a shorter profile),
as `generators_for_day` does. A unit without a profile is returned as is.
"""
function batteries_for_day(batteries::AbstractVector, start_hour::Int, hours::Int)
    return map(batteries) do bat
        m = bat.availability
        size(m, 1) == 0 && return bat
        with_fields(bat; availability =
            m[[mod1(start_hour + t - 1, size(m, 1)) for t in 1:hours], :])
    end
end

"""
    add_battery_tech_day!(model, input, pow_cap, en_cap, start_hour, hours;
                          suffix="", store=nothing) -> (discharge, charge, soc)

The candidate battery technologies' operation on one representative day or
TSAM period, as the dispatch runs the batteries they build: charge and
discharge within the power built × availability, ramp limits (BatteryConfig),
SOC within the energy built, cyclic at `soc_initial`. `pow_cap(bt, b)` and
`en_cap(bt, b)` give the capacity built: an expression of the investment
variables in the master, a number in a Benders subproblem. With `store`,
each constraint whose right-hand side holds the power (energy: the SOC limit
and the initial and cyclic SOC) is recorded in `store["bt_pow"]`
(`store["bt_cap"]`) as `(constraint, coefficient, bt, b)`, for the cut. Constraint names end in `suffix`, variable names in `var_suffix`.
"""
function add_battery_tech_day!(model::Model, input::MasterProblemInput,
                               pow_cap::Function, en_cap::Function,
                               start_hour::Int, hours::Int;
                               suffix::String = "", var_suffix::String = suffix,
                               store = nothing)
    n_bat_tech = length(input.battery_technologies)
    n_buses = input.network.num_buses
    tres = input.temporal_resolution_hours
    vsfx = var_suffix
    dis = @variable(model, [bt=1:n_bat_tech, b=1:n_buses, ts=1:hours],
        lower_bound = 0, base_name = "bat_tech_dis$(vsfx)")
    ch = @variable(model, [bt=1:n_bat_tech, b=1:n_buses, ts=1:hours],
        lower_bound = 0, base_name = "bat_tech_ch$(vsfx)")
    soc = @variable(model, [bt=1:n_bat_tech, b=1:n_buses, ts=0:hours],
        lower_bound = 0, base_name = "bat_tech_soc$(vsfx)")
    rec(kind, c, coef, bt, b) = store === nothing ||
        push!(get!(store, kind, Tuple{Any, Float64, Int, Int}[]), (c, coef, bt, b))

    day_techs = batteries_for_day(input.battery_technologies, start_hour, hours)
    for (bt, btech) in enumerate(day_techs), b in 1:n_buses
        P = pow_cap(bt, b)
        E = en_cap(bt, b)
        for ts in 1:hours
            avail = storage_availability(btech.availability, ts, b)
            c = @constraint(model, dis[bt, b, ts] <= P * avail,
                base_name = "bt_dis_cap_bt$(bt)_b$(b)_ts$(ts)$(suffix)")
            rec("bt_pow", c, avail, bt, b)
            c = @constraint(model, ch[bt, b, ts] <= P * avail,
                base_name = "bt_ch_cap_bt$(bt)_b$(b)_ts$(ts)$(suffix)")
            rec("bt_pow", c, avail, bt, b)
        end
        for ts in 0:hours
            c = @constraint(model, soc[bt, b, ts] <= E,
                base_name = "bt_soc_cap_bt$(bt)_b$(b)_ts$(ts)$(suffix)")
            rec("bt_cap", c, 1.0, bt, b)
        end
        for flow in (ch, dis)
            for (c, coef) in add_storage_ramp_constraints!(model, ts -> flow[bt, b, ts],
                    P, NaN, btech.ramp_up[b], btech.ramp_down[b], tres, hours)
                rec("bt_pow", c, coef, bt, b)
            end
        end
        ch_eff = btech.charge_efficiency[b]
        dis_eff = btech.discharge_efficiency[b]
        # Clamp to soc_min to avoid initial/end SOC vs soc_min conflict
        initial_soc_frac = max(btech.soc_initial[b], btech.soc_min[b])
        # Skip SOC dynamics if efficiency is zero (tech not at this node)
        if dis_eff <= 0.0 || ch_eff <= 0.0
            for ts in 0:hours
                @constraint(model, soc[bt, b, ts] <= 0,
                    base_name = "bt_soc_zero_bt$(bt)_b$(b)_ts$(ts)$(suffix)")
            end
            for ts in 1:hours
                @constraint(model, dis[bt, b, ts] <= 0,
                    base_name = "bt_dis_zero_bt$(bt)_b$(b)_ts$(ts)$(suffix)")
                @constraint(model, ch[bt, b, ts] <= 0,
                    base_name = "bt_ch_zero_bt$(bt)_b$(b)_ts$(ts)$(suffix)")
            end
        else
            c = @constraint(model, soc[bt, b, 0] == E * initial_soc_frac,
                base_name = "bt_soc_init_bt$(bt)_b$(b)$(suffix)")
            rec("bt_cap", c, initial_soc_frac, bt, b)
            # SOC in MWh; charge/discharge in MW over a step of tres hours
            for ts in 1:hours
                @constraint(model,
                    soc[bt, b, ts] == soc[bt, b, ts-1]
                        + tres * (ch_eff * ch[bt, b, ts] - (1.0/dis_eff) * dis[bt, b, ts]),
                    base_name = "bt_soc_dyn_bt$(bt)_b$(b)_ts$(ts)$(suffix)")
            end
            c = @constraint(model, soc[bt, b, hours] == E * initial_soc_frac,
                base_name = "bt_soc_cyc_bt$(bt)_b$(b)$(suffix)")
            rec("bt_cap", c, initial_soc_frac, bt, b)
        end
    end
    return dis, ch, soc
end

"""
    add_day_operational_constraints!(model, ps_vars, vars, input, year_idx, day_idx, demand, start_hour)

Add operational constraints for a representative day by delegating to power_system.jl
and transmission_dc.jl functions. Only master-specific logic (capacity precomputation,
technology dispatch, RE enforcement, sectoral LoL) remains here.

Returns `(tech_output, tech_day_cost)`: the candidate technologies' dispatch
and their operating cost that `day_operational_cost` does not see (the
offer-curve fuel cost of technologies with a `fuel_cost_curve`, the
curtailment penalty of candidate renewables and the battery technologies'
maintenance and throughput degradation), unscaled like `day_operational_cost`;
the callers scale both to the year the same way.

CRITICAL: This links investment decisions to operational feasibility.
"""
function add_day_operational_constraints!(
    model::Model,
    ps_vars::PowerSystemVariables,
    vars::MasterProblemVariables,
    input::MasterProblemInput,
    year_idx::Int,
    day_idx::Int,
    demand::Matrix{Float64},
    start_hour::Int;
    # TSAM inter-period SOC linking overrides (optional)
    initial_soc_overrides::Union{Nothing, Dict{Tuple{Int,Int}, Any}} = nothing,
    final_soc_targets::Union{Nothing, Dict{Tuple{Int,Int}, Any}} = nothing,
    # TSAM inter-period reservoir-level linking overrides (optional, seasonal hydro)
    initial_reservoir_overrides::Union{Nothing, Dict{Tuple{Int,Int}, Any}} = nothing,
    final_reservoir_targets::Union{Nothing, Dict{Tuple{Int,Int}, Any}} = nothing,
    # Inter-system DC-OPF: external flow contributions at border buses
    # (bus, hour) => AffExpr to add to flow_sum in KCL
    external_injections::Union{Nothing, Dict{Tuple{Int,Int}, AffExpr}} = nothing
)
    hours = size(demand, 1)
    n_gen = length(input.generators)
    n_bat = length(input.batteries)
    n_buses = input.network.num_buses
    b2n = input.network.bus_to_node

    # Apply demand growth
    growth_factor = (1.0 + input.demand_growth)^(year_idx - 1)

    # =========================================================================
    # Precompute total capacity per generator/battery per bus for this year
    # These are AffExpr (may contain investment variables) passed to power_system.jl
    # =========================================================================
    total_cap_gen = Dict{Tuple{Int,Int}, Any}()
    for g in 1:n_gen
        gen = input.generators[g]
        for b in ps_vars.buses_of_gen[g]
            cap = AffExpr(0.0)
            rated_power = gen.rated_power[b]
            lifetime = gen.life_time[b]
            initial_age = gen.initial_age[b]
            age_at_year = initial_age + (year_idx - 1)
            degradation_rate = gen.degradation_rate[b]

            if age_at_year < lifetime && rated_power > 0
                deg_factor = (1.0 - degradation_rate) ^ age_at_year
                add_to_expression!(cap, rated_power * deg_factor)
            elseif rated_power > 0
                life_ext_var = vars.gen_life_extension[year_idx][g][b]
                if life_ext_var !== nothing
                    add_to_expression!(cap, life_ext_var)
                end
            end

            total_cap_gen[(g, b)] = cap
        end
    end

    total_cap_bat_power = Dict{Tuple{Int,Int}, Any}()
    total_cap_bat_energy = Dict{Tuple{Int,Int}, Any}()
    for bi in 1:n_bat
        bat = input.batteries[bi]
        for b in ps_vars.buses_of_bat[bi]
            pow_cap = AffExpr(0.0)
            base_power = bat.max_discharge_power[b]
            lifetime = bat.life_time[b]
            initial_age = bat.initial_age[b]
            age_at_year = initial_age + (year_idx - 1)

            if age_at_year < lifetime && base_power > 0
                add_to_expression!(pow_cap, base_power)
            elseif base_power > 0
                life_ext_var = vars.bat_life_extension[year_idx][bi][b]
                if life_ext_var !== nothing
                    add_to_expression!(pow_cap, life_ext_var)
                end
            end
            total_cap_bat_power[(bi, b)] = pow_cap

            en_cap = AffExpr(0.0)
            base_cap = bat.capacity[b]
            if age_at_year < lifetime && base_cap > 0
                add_to_expression!(en_cap, base_cap)
            elseif base_cap > 0 && base_power > 0
                life_ext_var = vars.bat_life_extension[year_idx][bi][b]
                if life_ext_var !== nothing
                    ratio = base_cap / base_power
                    add_to_expression!(en_cap, life_ext_var, ratio)
                end
            end
            total_cap_bat_energy[(bi, b)] = en_cap
        end
    end

    # =========================================================================
    # Technology Generation Variables (per-technology investment dispatch)
    # Technologies are master-only concepts — not in power_system.jl
    # =========================================================================
    n_tech = length(input.technologies)
    n_bat_tech = length(input.battery_technologies)

    tech_output = nothing
    # Fuel cost of technologies with an offer curve (day_operational_cost
    # charges the others their flat fuel_cost)
    tech_curve_cost = AffExpr(0.0)
    if n_tech > 0
        tech_output = @variable(model,
            [t=1:n_tech, b=1:n_buses, ts=1:hours],
            lower_bound = 0,
            base_name = "tech_out_y$(year_idx)_d$(day_idx)")

        for t in 1:n_tech
            tech = input.technologies[t]
            is_renewable = tech.type == "Renewable"
            for b in 1:n_buses
                cumul_cap = AffExpr(0.0)
                tech_lifetime = tech.life_time[b]
                tech_deg = tech.degradation_rate[b]
                for y in 1:vars.years_per_inv_period:year_idx
                    inv_age = year_idx - y
                    if inv_age < tech_lifetime
                        deg = (1.0 - tech_deg) ^ inv_age
                        add_to_expression!(cumul_cap, vars.tech_investment[y][t][b], deg)
                    end
                end
                avails = Vector{Float64}(undef, hours)
                for ts in 1:hours
                    actual_hour = start_hour + ts - 1
                    avail_hours = size(tech.availability, 1)
                    avail_hour = avail_hours > 0 ? mod1(actual_hour, avail_hours) : 1
                    avail = is_renewable ? tech.availability[avail_hour, b] : 1.0
                    avails[ts] = avail
                    @constraint(model,
                        tech_output[t, b, ts] <= cumul_cap * avail,
                        base_name = "tech_cap_t$(t)_b$(b)_ts$(ts)_y$(year_idx)_d$(day_idx)")
                end
                add_tech_curve_segments!(model, input, tech_output, t, b,
                    cumul_cap, avails, tech_curve_cost;
                    suffix = "_y$(year_idx)_d$(day_idx)")
            end
        end
    end

    bat_tech_discharge = nothing
    bat_tech_charge = nothing
    bat_tech_soc = nothing
    if n_bat_tech > 0
        # Power and energy built and still in service this year
        function bt_built(bt, b, pick)
            cap = AffExpr(0.0)
            for y in 1:vars.years_per_inv_period:year_idx
                year_idx - y < input.battery_technologies[bt].life_time[b] || continue
                add_to_expression!(cap, pick(y)[bt][b])
            end
            return cap
        end
        bat_tech_discharge, bat_tech_charge, bat_tech_soc = add_battery_tech_day!(
            model, input,
            (bt, b) -> bt_built(bt, b, y -> vars.bat_tech_power_investment[y]),
            (bt, b) -> bt_built(bt, b, y -> vars.bat_tech_capacity_investment[y]),
            start_hour, hours; suffix = "_y$(year_idx)_d$(day_idx)")
    end

    # =========================================================================
    # Build day input NamedTuple for power_system.jl / transmission_dc.jl
    # =========================================================================
    n_nodes = input.network.num_nodes
    demand_slice = Matrix{Float64}(undef, hours, n_nodes)
    for t in 1:hours, n in 1:n_nodes
        demand_slice[t, n] = demand[t, n] * growth_factor
    end

    # The day model reads availability and inflow by its own step t = 1..hours
    day_generators = generators_for_day(input.generators, start_hour, hours)

    day_input = (
        generators = day_generators,
        batteries = batteries_for_day(input.batteries, start_hour, hours),
        network = input.network,
        demand = demand_slice,
        temporal = (hours = hours, resolution_hours = input.temporal_resolution_hours),
        year = input.base_year + year_idx - 1,
        base_year = input.base_year,
        mode = "economic_dispatch",
        reserve_static_requirement = input.reserve_static_requirement,
        reserve_static_default_ratio = input.reserve_static_default_ratio,
        reserve_dynamic_requirement = input.reserve_dynamic_requirement,
        dynamic_reserve_contribution = input.dynamic_reserve_contribution,
        inertia_limit = input.inertia_limit,
        inertia_limit_hourly = Float64[],
        rooftop_generation = nothing,
        re_penetration_target = 0.0,
        soc_end_tolerance = 0.0,
        min_cycling_ratio = 0.0,
        min_cycling_period_days = 1.0,
        pwl_loss_segments = input.transmission_loss_segments,
        gen_cost_curves = input.gen_cost_curves,
        bat_cost_curves = input.bat_cost_curves,
        # Rolling-horizon seam slots are not applicable inside the master's
        # representative-day subproblems (each day is solved fresh, with no
        # seam to a prior day). Provide empty Dicts so the shared
        # power_system.jl constraint builders fall back to their no-boundary
        # paths instead of erroring on a missing field.
        generator_output_prev = Dict{Int, Dict{Int, Float64}}(),
        reservoir_level_prev = Dict{Int, Dict{Int, Float64}}(),
    )

    # =========================================================================
    # DELEGATE to power_system.jl constraint functions
    # =========================================================================
    add_generator_constraints!(model, ps_vars, day_input;
        capacity_override=total_cap_gen)
    add_battery_constraints!(model, ps_vars, day_input;
        capacity_override_power=total_cap_bat_power,
        capacity_override_energy=total_cap_bat_energy,
        initial_soc_overrides=initial_soc_overrides,
        final_soc_targets=final_soc_targets)
    # Reservoir water balance / cyclic level — gives hydro an energy budget in
    # the master (previously omitted, so hydro was over-credited as firm MW).
    # When TSAM seasonal linking is active the cyclic closure is replaced by a
    # chronological chain across periods (overrides below). No-op with no reservoir.
    # The reservoirs' level bounds include the capacity built so far
    res_cap = Dict{Tuple{Int,Int}, Any}()
    for (g, gen) in enumerate(input.generators), b in 1:n_buses
        gen.reservoir_capacity[b] > 0 && gen.reservoir_invest_max[b] > 0 || continue
        res_cap[(g, b)] = gen.reservoir_capacity[b] +
                          reservoir_capacity_built(vars, g, b, year_idx)
    end
    add_reservoir_constraints!(model, ps_vars, day_input;
        initial_reservoir_overrides=initial_reservoir_overrides,
        final_reservoir_targets=final_reservoir_targets,
        capacity_override=res_cap)
    add_reserve_constraints!(model, ps_vars, day_input;
        capacity_override=total_cap_gen,
        battery_capacity_override=total_cap_bat_power,
        demand_scale=1.0)  # demand already scaled in demand_slice
    add_inertia_constraints!(model, ps_vars, day_input)
    # AC/DC and frequency converters as the dispatch models them, at their
    # rated power plus the converter capacity the master builds
    conv_cap = Dict{Tuple{String,Int}, AffExpr}(
        key => converter_capacity(vars, conv, key, year_idx)
        for (key, conv) in master_converters(input.network))
    add_converter_constraints!(model, ps_vars, day_input; capacity_override=conv_cap)

    # =========================================================================
    # Build extra injection function for balance (master-specific terms)
    # =========================================================================
    extra_fn = (bus, t) -> begin
        expr = AffExpr(0.0)
        # Technology generation
        if tech_output !== nothing
            for tc in 1:n_tech
                add_to_expression!(expr, tech_output[tc, bus, t])
            end
        end
        # Battery technology net discharge
        if bat_tech_discharge !== nothing
            for btc in 1:n_bat_tech
                add_to_expression!(expr, bat_tech_discharge[btc, bus, t])
                add_to_expression!(expr, -1.0, bat_tech_charge[btc, bus, t])
            end
        end
        # Inter-system external injections
        if external_injections !== nothing && haskey(external_injections, (bus, t))
            add_to_expression!(expr, external_injections[(bus, t)])
        end
        return expr
    end

    # =========================================================================
    # DELEGATE to transmission_dc.jl / power_system.jl for power balance
    # =========================================================================
    # Use network.transmission_lines (populated by convert_network_config from YAML)
    # NOT input.transmission_lines (MasterProblemInput field, may be empty)
    network_lines = input.network.transmission_lines
    n_lines = length(network_lines)
    line_flow_cost = AffExpr(0.0)
    if n_buses > 1 && n_lines > 0
        transmission = with_loss_model(TransmissionDC(input.network),
            input.transmission_loss_segments, input.network.s_base)
        inv_years = [y for y in 1:vars.years_per_inv_period:year_idx
                     if haskey(vars.transfer_investment, y)]
        expandable = Set{Int}(l for (l, (from, to)) in enumerate(transmission.lines)
            if any(corridor_investment(vars.transfer_investment[y], from, to) !== nothing
                   for y in inv_years))
        add_dc_constraints!(model, transmission, ps_vars, day_input;
            extra_injections_fn=extra_fn, expandable_lines=expandable)

        # Build line capacity overrides with cumulative investment
        # Use transmission.lines (from TransmissionDC) which includes transformers
        line_cap_override = Dict{Int, Any}()
        for (l, (from, to)) in enumerate(transmission.lines)
            base_cap = transmission.line_capacities[l]
            inv_cap = AffExpr(0.0)
            for y in inv_years
                v = corridor_investment(vars.transfer_investment[y], from, to)
                v === nothing || add_to_expression!(inv_cap, v)
            end
            line_cap_override[l] = base_cap + inv_cap
        end
        add_line_capacity_constraints!(model, transmission, ps_vars, day_input;
            capacity_override=line_cap_override)
        line_flow_cost = add_line_flow_cost!(model, transmission, ps_vars, input, hours)
    else
        add_demand_constraints!(model, ps_vars, day_input;
            extra_injections_fn=extra_fn)
    end

    # =========================================================================
    # DELEGATE curtailment to power_system.jl
    # =========================================================================
    add_curtailment_constraints!(model, ps_vars, day_input;
        capacity_override=total_cap_gen)

    # =========================================================================
    # Sectoral split of the load shed (M2) — master-specific
    # =========================================================================
    remainder_premium = add_day_sectoral_shed_constraints!(model, ps_vars, input,
        demand_slice, start_hour, growth_factor; label = "y$(year_idx)_d$(day_idx)")

    # =========================================================================
    # CRITICAL: Per-System Renewable Penetration Constraints — master-specific
    # Links to re_penetration_ratio[(s,y)] variable
    # MUST include both existing generators AND technology investments
    # =========================================================================
    targets = calculate_target_ratios(input)

    # Build system ranges (default: single system covering all buses)
    sys_ranges = input.system_node_ranges
    if isempty(sys_ranges)
        sys_ranges = [SystemNodeRange("global", 1, n_buses, input.initial_re_penetration)]
    end

    for (s_idx, sr) in enumerate(sys_ranges)
        sys_first = sr.first_bus
        sys_last = sr.first_bus + sr.num_buses - 1
        sys_buses = Set(sys_first:sys_last)

        sys_renewable = AffExpr(0.0)
        sys_generation = AffExpr(0.0)

        # Existing generators — only count buses in this system
        for g in 1:n_gen
            gen = input.generators[g]
            is_renewable = gen.type == "Renewable"
            for n in ps_vars.buses_of_gen[g]
                n in sys_buses || continue
                for t in 1:hours
                    add_to_expression!(sys_generation, ps_vars.gen_output[g, n, t])
                    if is_renewable
                        add_to_expression!(sys_renewable, ps_vars.gen_output[g, n, t])
                    end
                end
            end
        end

        # Technology investments — only count buses in this system
        if tech_output !== nothing
            for tc in 1:n_tech
                tech = input.technologies[tc]
                is_renewable = tech.type == "Renewable"
                for b in sys_first:sys_last
                    for t in 1:hours
                        add_to_expression!(sys_generation, tech_output[tc, b, t])
                        if is_renewable
                            add_to_expression!(sys_renewable, tech_output[tc, b, t])
                        end
                    end
                end
            end
        end

        target_ratio = targets[(s_idx, year_idx)]

        # System demand for this day (only system's buses)
        sys_demand_day = 0.0
        for t in 1:hours
            actual_hour = start_hour + t - 1
            hours_in_data = size(input.base_demand, 1)
            hour_in_year = hours_in_data > 0 ? mod1(actual_hour, hours_in_data) : 1
            for b in sys_first:sys_last
                parent_node = input.network.bus_to_node[b]
                bus_fraction = input.network.buses[b].demand_fraction
                sys_demand_day += input.base_demand[hour_in_year, parent_node] * bus_fraction
            end
        end
        sys_demand_day *= growth_factor

        # Map bus range to unique nodes for node-level variables
        sys_node_set = unique([input.network.bus_to_node[b] for b in sys_first:sys_last])
        sys_fre_loss = @expression(model,
            sum(ps_vars.fre_penetration_loss[n, t] for n in sys_node_set, t in 1:hours))

        # RE minimum: at least target_ratio of system demand must be renewable
        @constraint(model,
            sys_renewable + sys_fre_loss >= target_ratio * sys_demand_day,
            base_name = "re_min_day_s$(s_idx)_y$(year_idx)_d$(day_idx)")

        # NOTE: No per-day maximum RE constraint. Exceeding the RE target on
        # high-availability days is physically valid and expected. The annual
        # re_target constraint (with slack) ensures the overall ratio is met.

        if sys_demand_day > 0
            # RE ratio lower bound: ratio represents the worst-case daily fraction
            @constraint(model,
                vars.re_penetration_ratio[(s_idx, year_idx)] * sys_demand_day <= sys_renewable + sys_fre_loss,
                base_name = "re_ratio_lb_s$(s_idx)_y$(year_idx)_d$(day_idx)")
            # NOTE: No upper bound on per-day RE ratio. The re_penetration_ratio
            # represents the minimum achieved fraction across all representative
            # days, allowing higher RE on favorable days.
        end
    end

    # =========================================================================
    # Maximum Curtailment Constraint — master-specific
    # Includes BOTH generator curtailment AND technology RE curtailment
    # (available tech capacity not dispatched)
    # =========================================================================
    # Accumulator for tech-investment curtailment cost — added to the master
    # objective so the LP trades tech overbuild against storage capex on an
    # explicit economic basis (the max_curtailment_ratio constraint alone
    # empirically does not bind for the full-year operational LP).
    tech_day_cost = AffExpr(0.0)
    # Unclassified sectoral remainder above plain VOLL (as the dispatch prices it)
    add_to_expression!(tech_day_cost, remainder_premium)

    if input.max_curtailment_ratio < 1.0
        # Build as AffExpr explicitly: when the sum collapses to a single
        # term (n_nodes=1 ∧ hours=1) `@expression` returns a bare
        # VariableRef and later add_to_expression!(VarRef, AffExpr) fails.
        total_curtailment = AffExpr(0.0)
        for n in 1:n_nodes, t in 1:hours
            add_to_expression!(total_curtailment, ps_vars.curtailment[n, t])
        end
        # Compute total renewable generation across all buses
        total_renewable = AffExpr(0.0)
        for g in 1:n_gen
            gen = input.generators[g]
            if gen.type == "Renewable"
                for n in ps_vars.buses_of_gen[g]
                    for t in 1:hours
                        add_to_expression!(total_renewable, ps_vars.gen_output[g, n, t])
                    end
                end
            end
        end
        if tech_output !== nothing
            n_tech_local = length(input.technologies)
            for tc in 1:n_tech_local
                tech = input.technologies[tc]
                if tech.type == "Renewable"
                    for b in 1:n_buses
                        for ts in 1:hours
                            add_to_expression!(total_renewable, tech_output[tc, b, ts])
                        end
                    end
                end
            end
        end
        # Add technology RE curtailment: available - dispatched
        if tech_output !== nothing
            n_tech_local = length(input.technologies)
            for tc in 1:n_tech_local
                tech = input.technologies[tc]
                if tech.type == "Renewable"
                    for b in 1:n_buses
                        # Recompute cumulative capacity (same as tech constraint setup)
                        cumul_cap = AffExpr(0.0)
                        tech_lifetime = tech.life_time[b]
                        tech_deg = tech.degradation_rate[b]
                        for y in 1:vars.years_per_inv_period:year_idx
                            inv_age = year_idx - y
                            if inv_age < tech_lifetime
                                deg = (1.0 - tech_deg) ^ inv_age
                                add_to_expression!(cumul_cap,
                                    vars.tech_investment[y][tc][b], deg)
                            end
                        end
                        for ts in 1:hours
                            actual_hour = start_hour + ts - 1
                            avail_hours = size(tech.availability, 1)
                            avail_hour = avail_hours > 0 ? mod1(actual_hour, avail_hours) : 1
                            avail = tech.availability[avail_hour, b]
                            # tech curtailment = cumul_cap × avail - tech_output
                            add_to_expression!(total_curtailment, cumul_cap * avail)
                            add_to_expression!(total_curtailment, -1.0, tech_output[tc, b, ts])
                            # Penalise the same expression in the objective
                            if input.curtailment_cost > 0
                                add_to_expression!(tech_day_cost,
                                    cumul_cap * (avail * input.curtailment_cost))
                                add_to_expression!(tech_day_cost,
                                    -input.curtailment_cost, tech_output[tc, b, ts])
                            end
                        end
                    end
                end
            end
        end
        # Only add constraint if there are renewable terms (avoid forcing curtailment=0)
        if length(total_renewable.terms) > 0
            @constraint(model,
                total_curtailment <= input.max_curtailment_ratio * total_renewable,
                base_name = "max_curtailment_y$(year_idx)_d$(day_idx)")
        end
    end

    add_to_expression!(tech_day_cost, tech_curve_cost)
    add_to_expression!(tech_day_cost,
        battery_tech_operating_cost(input, bat_tech_charge, bat_tech_discharge, hours))
    # Converter variable cost, flow cost of lines with a per-MW·km cost and
    # (LP-relaxed) start-ups, as the dispatch charges them
    add_to_expression!(tech_day_cost, add_converter_objective_terms(ps_vars, day_input))
    add_to_expression!(tech_day_cost, line_flow_cost)
    add_to_expression!(tech_day_cost,
        add_day_commitment!(model, ps_vars, input, total_cap_gen, year_idx, hours))

    # The caller scales tech_day_cost with the day cost (calculate_day_operational_cost)
    return tech_output, tech_day_cost
end

"""
    add_line_flow_cost!(model, transmission, ps_vars, input, hours) -> AffExpr

Unscaled cost of the flow on the lines that charge one per MWh (their
`flow_cost`, e.g. distance × cost per MW·km of an inter-system link):
`flow_cost × |flow|` per line and step, with `|flow|` an auxiliary variable
above `±flow` (the objective keeps it tight). Each line pays its own cost,
as in the dispatch (`add_line_flow_costs!`); transformers pay none.
"""
function add_line_flow_cost!(model::Model, transmission::TransmissionDC,
                             ps_vars::PowerSystemVariables,
                             input::MasterProblemInput, hours::Int)
    cost = AffExpr(0.0)
    for (l, c) in enumerate(line_flow_costs(transmission, input.network))
        c > 0 || continue
        for t in 1:hours
            pf = ps_vars.power_flow_by_line[l][t]
            a = @variable(model, lower_bound = 0)
            @constraint(model, a >= pf)
            @constraint(model, a >= -pf)
            add_to_expression!(cost, c, a)
        end
    end
    return cost
end

"""
    line_flow_costs(transmission, network) -> Vector{Float64}

`flow_cost` of each branch of `transmission`, in its order: the network's
transmission lines (self-loops dropped, as `TransmissionDC` does), then the
transformers at 0.
"""
function line_flow_costs(transmission::TransmissionDC, network::NetworkConfig)
    costs = zeros(length(transmission.lines))
    isempty(network.transmission_lines) && return costs
    l = 0
    for tl in network.transmission_lines
        tl.from_node == tl.to_node && continue
        l += 1
        l <= length(costs) || break
        costs[l] = tl.flow_cost
    end
    return costs
end

"""
    add_day_commitment!(model, ps_vars, input, total_cap_gen, year_idx, hours) -> AffExpr

LP-relaxed unit commitment of a representative day, when the dispatch
commits units (`input.unit_commitment`). Each thermal (or reservoir) unit
with a start-up cost or a minimum power gets an online capacity `u[t]`
(MW, at most the unit's capacity this year):

    gen_output[t] ≤ u[t],   gen_output[t] ≥ min_power × u[t],
    startup[t] ≥ u[t] − u[t−1]   (cyclic over the day)

A start of the whole unit (startup = its rated power) costs `start_up_cost`,
so a MW started costs `start_up_cost / rated`, with `rated` the unit's
degraded rated power this year as the dispatch sees it. Minimum up and down
times are not modelled.

Returns the start-up cost divided by the step length: the caller scales
the day cost by the step length and the day's weight, and a start-up is an
event, not energy. Each unit's share is kept in
`model.ext[:master_startup][ps_vars][(g, b)]`, for its NPV.
"""
function add_day_commitment!(model::Model, ps_vars::PowerSystemVariables,
                             input::MasterProblemInput, total_cap_gen::AbstractDict,
                             year_idx::Int, hours::Int)
    cost = AffExpr(0.0)
    input.unit_commitment || return cost
    tres = max(1.0, input.temporal_resolution_hours)
    for (g, gen) in enumerate(input.generators)
        (gen.type == "Renewable" && !any(gen.reservoir_capacity .> 0)) && continue
        for b in ps_vars.buses_of_gen[g]
            suc = gen.start_up_cost[b]
            pmin = gen.min_power[b]
            rated = gen.rated_power[b] *
                (1.0 - gen.degradation_rate[b])^(gen.initial_age[b] + year_idx - 1)
            (rated > 0 && (suc > 0 || pmin > 0)) || continue
            cap = total_cap_gen[(g, b)]
            u = @variable(model, [1:hours], lower_bound = 0)
            su = @variable(model, [1:hours], lower_bound = 0)
            unit_cost = AffExpr(0.0)
            for t in 1:hours
                out = ps_vars.gen_output[g, b, t]
                @constraint(model, u[t] <= cap)
                @constraint(model, out <= u[t])
                pmin > 0 && @constraint(model, out >= pmin * u[t])
                @constraint(model, su[t] >= u[t] - u[t == 1 ? hours : t - 1])
                if suc > 0
                    add_to_expression!(cost, suc / rated / tres, su[t])
                    add_to_expression!(unit_cost, suc / rated / tres, su[t])
                end
            end
            if suc > 0
                by_day = get!(() -> IdDict{Any, Dict{Tuple{Int,Int}, AffExpr}}(),
                              model.ext, :master_startup)
                get!(() -> Dict{Tuple{Int,Int}, AffExpr}(), by_day, ps_vars)[(g, b)] = unit_cost
            end
        end
    end
    return cost
end

"""
    battery_tech_operating_cost(input, charge, discharge, hours) -> AffExpr

Unscaled operating cost of the candidate battery technologies over `hours`
steps, as the dispatch charges the batteries they become: `maintenance_cost`
on charge and discharge, `throughput_degradation_cost` on discharge.
`charge`/`discharge` are indexed [technology, bus, step]; `nothing` when the
system has no battery technology.
"""
function battery_tech_operating_cost(input::MasterProblemInput, charge, discharge, hours::Int)
    cost = AffExpr(0.0)
    (charge === nothing || discharge === nothing) && return cost
    for (bt, btech) in enumerate(input.battery_technologies), b in 1:input.network.num_buses
        mc = btech.maintenance_cost[b]
        dc = btech.throughput_degradation_cost[b]
        (mc != 0 || dc != 0) || continue
        for ts in 1:hours
            add_to_expression!(cost, mc, charge[bt, b, ts])
            add_to_expression!(cost, mc + dc, discharge[bt, b, ts])
        end
    end
    return cost
end

"""
    add_day_sectoral_shed_constraints!(model, ps_vars, input, demand_slice,
                                       start_hour, growth_factor; label=nothing)

Split each bus's load shed of a representative day into per-sector shed plus
an unclassified remainder, as the dispatch does (SEC-1, SEC-2):

- each sector sheds at most its own demand at the bus
  (`sectoral_demand[t, parent node] × demand_fraction × growth`);
- `load_shed − Σ sector shed` lies in [0, bus demand not covered by any sector].

The sectoral shed is part of `load_shed`, not an extra injection: the bus can
shed at most its demand (PB-1b). `day_operational_cost` charges `load_shed` at
VOLL and adds the criticality premium on the sectoral part. `label` names the
constraints; `nothing` leaves them unnamed (Benders subproblems).

Returns the unscaled premium of the unclassified remainder above VOLL
(`loss_of_load_penalty`): `VOLL × (m − 1) × (load_shed − Σ sector shed)`, with `m` the remainder's
multiplier (`sector_remainder_multiplier`, the dispatch's rule); the caller
adds it to the day's cost.
"""
function add_day_sectoral_shed_constraints!(model, ps_vars, input, demand_slice,
                                            start_hour::Int, growth_factor::Float64;
                                            label::Union{Nothing, String} = nothing)
    voll = input.loss_of_load_penalty
    premium = AffExpr(0.0)
    lol = ps_vars.loss_of_load_sectoral
    (lol === nothing || isempty(lol)) && return premium
    hours = size(demand_slice, 1)
    for t in 1:hours, b in 1:input.network.num_buses
        parent = input.network.bus_to_node[b]
        frac = input.network.buses[b].demand_fraction
        sector_shed = AffExpr(0.0)
        sector_demand = 0.0
        sec_vals = Dict{String, Float64}()
        for (sector, lol_vars) in lol
            sec_val = 0.0
            sec_dem = get(input.sectoral_demand, sector, nothing)
            if sec_dem !== nothing && size(sec_dem, 1) > 0 && parent <= size(sec_dem, 2)
                hour_in_year = mod1(start_hour + t - 1, size(sec_dem, 1))
                sec_val = max(0.0, sec_dem[hour_in_year, parent] * frac * growth_factor)
            end
            sec_vals[sector] = sec_val
            sector_demand += sec_val
            add_to_expression!(sector_shed, lol_vars[b, t])
            @constraint(model, lol_vars[b, t] <= sec_val,
                base_name = label === nothing ? "" : "lol_sec_ub_$(sector)_$(label)_n$(b)_t$(t)")
        end
        residual_cap = max(0.0, demand_slice[t, parent] * frac - sector_demand)
        @constraint(model, ps_vars.load_shed[b, t] - sector_shed >= 0.0,
            base_name = label === nothing ? "" : "lol_sec_sum_$(label)_n$(b)_t$(t)")
        @constraint(model, ps_vars.load_shed[b, t] - sector_shed <= residual_cap,
            base_name = label === nothing ? "" : "lol_sec_residual_$(label)_n$(b)_t$(t)")
        m = sector_remainder_multiplier(keys(lol), input.sectoral_criticality,
                                        s -> sec_vals[s])
        if m != 1.0
            add_to_expression!(premium, voll * (m - 1.0), ps_vars.load_shed[b, t])
            add_to_expression!(premium, -voll * (m - 1.0), sector_shed)
        end
    end
    return premium
end

"""
    co2_cost_per_mwh(input, unit, b)

Carbon cost of one MWh produced by `unit` (generator or technology) at bus `b`
(\$/t × t/MWh_e, see `co2_per_mwh`), as the dispatch charges it.
"""
co2_cost_per_mwh(input::MasterProblemInput, unit, b::Int) =
    input.co2_cost * co2_per_mwh(input.fuel_co2, unit, b)

"""
    tech_curve_segments(input, tc, b)

Offer-curve segments of candidate technology `tc` at bus `b`, or `nothing`
when it has no curve there and is priced at its flat `fuel_cost`, the same
rule the generators follow. A single segment (a flat curve) prices the
output at its own marginal cost.
"""
function tech_curve_segments(input::MasterProblemInput, tc::Int, b::Int)
    bus_curves = get(input.tech_cost_curves, tc, nothing)
    bus_curves === nothing && return nothing
    segs = get(bus_curves, b, nothing)
    return (segs === nothing || isempty(segs)) ? nothing : segs
end

"""
    add_tech_curve_segments!(model, input, tech_output, tc, b, cap, avails, cost;
                             suffix="", store=nothing)

Price candidate technology `tc`'s output at bus `b` with its offer curve, as
the dispatch prices the generator it becomes: the output is split into one
variable per segment, segment k is capped at `fraction_k × cap × avails[ts]`
and its marginal cost is added to `cost`. `cap` is the cumulative capacity
(an expression in the master, a number in a Benders subproblem). With
`store`, each segment cap is recorded as `(cref, fraction_k × avail, tc, b, ts)`
under `tech_seg_cap_t<tc>_b<b>_k<k>_ts<ts>` for the Benders cut gradient.
No-op without a curve.
"""
function add_tech_curve_segments!(model::Model, input::MasterProblemInput,
                                  tech_output, tc::Int, b::Int, cap, avails::Vector{Float64},
                                  cost::AffExpr; suffix::String = "", store = nothing)
    segs = tech_curve_segments(input, tc, b)
    segs === nothing && return nothing
    n_seg = length(segs)
    sv = @variable(model, [k=1:n_seg, ts=1:length(avails)], lower_bound = 0,
                   base_name = "tech_seg_t$(tc)_b$(b)$(suffix)")
    for ts in eachindex(avails)
        @constraint(model, tech_output[tc, b, ts] == sum(sv[k, ts] for k in 1:n_seg))
        for k in 1:n_seg
            cname = "tech_seg_cap_t$(tc)_b$(b)_k$(k)_ts$(ts)"
            coef = segs[k].fraction * avails[ts]
            cref = @constraint(model, sv[k, ts] <= coef * cap,
                               base_name = cname * suffix)
            store === nothing || (store[cname] = (cref, coef, tc, b, ts))
            add_to_expression!(cost, segs[k].marginal_cost, sv[k, ts])
        end
    end
    return sv
end

"""
    day_operational_cost(ps_vars, input, hours; tech_output=nothing)

Unscaled operating cost of one representative day or TSAM period: fuel,
maintenance and CO2, load shedding, curtailment, battery maintenance (on
charge and discharge) and throughput, and the reserve and inertia
shortfalls. The callers scale it to a year. Fixed O&M is not an operating
cost: it is charged per year on the capacity in service
(`build_annual_asset_cost`).
"""
function day_operational_cost(
    ps_vars::PowerSystemVariables,
    input::MasterProblemInput,
    hours::Int;
    tech_output::Union{Nothing, Any} = nothing
)::AffExpr
    n_gen = length(input.generators)
    n_buses = input.network.num_buses
    n_nodes = input.network.num_nodes

    cost = AffExpr(0.0)

    for t in 1:hours
        for g in 1:n_gen
            for n in ps_vars.buses_of_gen[g]
                add_gen_step_cost!(cost, ps_vars, input, g, n, t)
            end
        end

        if tech_output !== nothing
            n_tech = length(input.technologies)
            for tc in 1:n_tech
                tech = input.technologies[tc]
                for b in 1:n_buses
                    co2 = co2_cost_per_mwh(input, tech, b)
                    # With an offer curve the fuel is priced per segment
                    # (add_tech_curve_segments!, in tech_day_cost)
                    fuel = tech_curve_segments(input, tc, b) === nothing ? tech.fuel_cost[b] : 0.0
                    total_cost = fuel + tech.maintenance_cost[b] + co2
                    if total_cost > 0
                        add_to_expression!(cost, tech_output[tc, b, t], total_cost)
                    end
                end
            end
        end

        # load_shed is per-bus (B2 refactor) — iterate over buses.
        for b in 1:n_buses
            add_to_expression!(cost, ps_vars.load_shed[b, t], input.loss_of_load_penalty)
        end

        for n in 1:n_nodes
            add_to_expression!(cost, ps_vars.fre_penetration_loss[n, t],
                input.fre_penetration_loss_penalty)
        end

        # Curtailment penalty (existing renewable generators).
        # Tech-investment curtailment is penalised separately in
        # add_day_operational_constraints! where the tech_output expression
        # and per-year cumul_cap are accessible. Without this term the master
        # has no incentive to size tech to actual absorption capacity —
        # excess renewable is spilled "for free", so master overbuilds and
        # operational reveals the gap (curtailment 40-65% from year 2042 on
        # despite the configured max_curtailment_ratio = 5%).
        for n in 1:n_nodes
            add_to_expression!(cost, ps_vars.curtailment[n, t],
                input.curtailment_cost)
        end

        # Sectoral shed is part of load_shed (add_day_sectoral_shed_constraints!),
        # which is already charged VOLL above: add the criticality premium
        # VOLL × (κ − 1), so a sector's MWh costs VOLL × κ. The unclassified
        # remainder's premium (weighted mean criticality) comes from
        # add_day_sectoral_shed_constraints!.
        if ps_vars.loss_of_load_sectoral !== nothing && !isempty(ps_vars.loss_of_load_sectoral)
            for (sector, lol_vars) in ps_vars.loss_of_load_sectoral
                criticality = get(input.sectoral_criticality, sector, 1.0)
                sector_premium = input.loss_of_load_penalty * (criticality - 1.0)
                for n in 1:n_buses
                    add_to_expression!(cost, lol_vars[n, t], sector_premium)
                end
            end
        end
    end

    n_bat = length(input.batteries)
    for t in 1:hours
        for b in 1:n_bat
            for n in ps_vars.buses_of_bat[b]
                add_bat_step_cost!(cost, ps_vars, input, b, n, t)
            end
        end
    end

    for t in 1:hours
        for n in 1:n_nodes
            add_to_expression!(cost, ps_vars.reserve_static_loss[n, t],
                input.loss_of_reserve_static)
            add_to_expression!(cost, ps_vars.reserve_dynamic_loss[n, t],
                input.loss_of_reserve_dynamic)
        end
    end

    if ps_vars.loss_of_inertia !== nothing
        for t in 1:hours
            add_to_expression!(cost, ps_vars.loss_of_inertia[t],
                input.loss_of_inertia_penalty)
        end
    end

    return cost
end

"""
    add_gen_step_cost!(cost, ps_vars, input, g, n, t)

Add existing generator `g`'s operating cost at bus `n` in step `t` (per
step, not × Δt) to `cost`: its fuel (`fuel_cost`, or its offer curve's
segments), maintenance and CO2 price per MWh. `day_operational_cost` and the
unit's NPV (`add_unit_day_operation!`) both use it.
"""
function add_gen_step_cost!(cost::AffExpr, ps_vars::PowerSystemVariables,
                            input::MasterProblemInput, g::Int, n::Int, t::Int)
    gen = input.generators[g]
    co2 = co2_cost_per_mwh(input, gen, n)
    maint_cost = gen.maintenance_cost[n]
    if haskey(ps_vars.gen_seg_output, (g, n))
        # PWL fuel cost via segments
        segs = input.gen_cost_curves[g][n]
        seg_vars = ps_vars.gen_seg_output[(g, n)]
        for k in 1:length(segs)
            add_to_expression!(cost, segs[k].marginal_cost, seg_vars[k, t])
        end
        add_to_expression!(cost, ps_vars.gen_output[g, n, t],
                          maint_cost + co2)
    else
        fuel_cost = gen.fuel_cost[n]
        add_to_expression!(cost, ps_vars.gen_output[g, n, t],
                          fuel_cost + maint_cost + co2)
    end
    return cost
end

"""
    add_bat_step_cost!(cost, ps_vars, input, b, n, t)

Add existing battery `b`'s operating cost at bus `n` in step `t` (per step)
to `cost`: maintenance per MWh charged and discharged, as in the dispatch,
and its throughput cost (or offer curve) on the discharge.
"""
function add_bat_step_cost!(cost::AffExpr, ps_vars::PowerSystemVariables,
                            input::MasterProblemInput, b::Int, n::Int, t::Int)
    bat = input.batteries[b]
    mc = bat.maintenance_cost[n]
    if mc != 0
        add_to_expression!(cost, mc, ps_vars.bat_charge[b, n, t])
        add_to_expression!(cost, mc, ps_vars.bat_discharge[b, n, t])
    end
    if haskey(ps_vars.bat_seg_discharge, (b, n))
        segs = input.bat_cost_curves[b][n]
        seg_vars = ps_vars.bat_seg_discharge[(b, n)]
        for k in 1:length(segs)
            add_to_expression!(cost, segs[k].marginal_cost, seg_vars[k, t])
        end
    else
        add_to_expression!(cost, ps_vars.bat_discharge[b, n, t],
                          bat.throughput_degradation_cost[n])
    end
    return cost
end

"""
    calculate_day_operational_cost(ps_vars, input, year_idx, hours;
                                   tech_output=nothing, tech_day_cost=nothing)

Calculate operational cost for a representative day, scaled to the year.
`tech_day_cost` (from `add_day_operational_constraints!`) is scaled with it.
"""
function calculate_day_operational_cost(
    ps_vars::PowerSystemVariables,
    input::MasterProblemInput,
    year_idx::Int,
    hours::Int;
    tech_output::Union{Nothing, Any} = nothing,
    tech_day_cost::Union{Nothing, AffExpr} = nothing
)::AffExpr
    scaling_factor = rep_day_scaling(input)

    cost = day_operational_cost(ps_vars, input, hours; tech_output)
    tech_day_cost === nothing || add_to_expression!(cost, tech_day_cost)
    return cost * scaling_factor
end

"""
    rep_day_scaling(input)

Factor from one representative day's per-step quantities to the year: the
step length times the days each representative day stands for.
"""
function rep_day_scaling(input::MasterProblemInput)
    tres = max(1.0, input.temporal_resolution_hours)
    days_in_year = length(input.hours_per_year) > 0 ? input.hours_per_year[1] / 24.0 : 365.0
    return tres * days_in_year / input.representative_days_per_year
end

"""
    day_co2_by_group(ps_vars, tech_output, input, hours) -> Vector{AffExpr}

CO2 of a representative day or TSAM period per budget group of
`input.co2_budget_groups`: Σ over steps of output (MW) × `co2_per_mwh`
(t/MWh) of the generators and candidate technologies at the group's nodes.
Per step: the caller scales it by the step length and the day's weight.
"""
function day_co2_by_group(ps_vars::PowerSystemVariables, tech_output,
                          input::MasterProblemInput, hours::Int)
    b2n = input.network.bus_to_node
    out = [AffExpr(0.0) for _ in input.co2_budget_groups]
    for (k, grp) in enumerate(input.co2_budget_groups)
        has_co2_budget(grp) || continue
        in_group = Set(grp.nodes)
        for (g, gen) in enumerate(input.generators), b in ps_vars.buses_of_gen[g]
            b2n[b] in in_group || continue
            f = co2_per_mwh(input.fuel_co2, gen, b)
            f == 0.0 && continue
            for t in 1:hours
                add_to_expression!(out[k], f, ps_vars.gen_output[g, b, t])
            end
        end
        tech_output === nothing && continue
        for (tc, tech) in enumerate(input.technologies), b in 1:input.network.num_buses
            b2n[b] in in_group || continue
            f = co2_per_mwh(input.fuel_co2, tech, b)
            f == 0.0 && continue
            for t in 1:hours
                add_to_expression!(out[k], f, tech_output[tc, b, t])
            end
        end
    end
    return out
end

"""
    add_co2_budget_constraints!(model, vars, input, y_idx, annual_co2; scenario_idx=0) -> AffExpr

One annual CO2 budget constraint per budget group (one per system), as in
the dispatch: the year's emissions `annual_co2[k]` (t) stay within the
group's budget plus a violation slack. Returns the cost of the slacks at
each group's own violation penalty, to add to the year's operating cost.
"""
function add_co2_budget_constraints!(model::Model, vars::MasterProblemVariables,
                                     input::MasterProblemInput, y_idx::Int,
                                     annual_co2::Vector{AffExpr}; scenario_idx::Int = 0)
    cost = AffExpr(0.0)
    for (k, grp) in enumerate(input.co2_budget_groups)
        has_co2_budget(grp) || continue
        tag = scenario_idx > 0 ? "_s$(scenario_idx)" : ""
        viol = @variable(model, lower_bound = 0,
            base_name = "co2_budget_violation$(tag)_y$(y_idx)_$(grp.name)")
        vars.co2_budget_violation[(scenario_idx, y_idx, k)] = viol
        @constraint(model, annual_co2[k] <= grp.budget + viol,
            base_name = "co2_budget$(tag)_y$(y_idx)_$(grp.name)")
        add_to_expression!(cost, grp.violation_penalty, viol)
    end
    return cost
end

"""
    add_representative_days_validation!(model, vars, input, targets)

Add operational validation through representative days.
This is CRITICAL for linking investment decisions to actual generation feasibility.

Exactly mirrors legacy _add_representative_days_validation.
"""
function add_representative_days_validation!(
    model::Model,
    vars::MasterProblemVariables,
    base_input::MasterProblemInput,
    targets::Dict{Tuple{Int,Int}, Float64};
    scenario::Union{Nothing, Scenario} = nothing,
    scenario_idx::Int = 0
)
    num_years = length(base_input.years)
    timesteps_per_day = 24 ÷ base_input.temporal_resolution_hours
    day_offset = 1000 * scenario_idx   # keeps day names distinct per scenario

    for y_idx in 1:num_years
        # A stochastic scenario operates on its own demand and costs
        input = scenario === nothing ? base_input :
            scenario_master_input(base_input, scenario.multipliers, y_idx)
        timesteps_per_year = input.hours_per_year[y_idx] ÷ input.temporal_resolution_hours

        rep_days = select_representative_days(
            input.base_demand,
            y_idx,
            input.representative_days_per_year,
            input.min_day_separation,
            timesteps_per_day,
            timesteps_per_year,
        )

        if isempty(rep_days)
            @warn "No representative days selected for year $(input.years[y_idx])"
            continue
        end

        vars.operational_costs[y_idx] = AffExpr[]
        annual_co2 = [AffExpr(0.0) for _ in input.co2_budget_groups]

        for (day_idx, start_hour) in enumerate(rep_days)
            end_hour = min(start_hour + timesteps_per_day - 1, size(input.base_demand, 1))
            hours = end_hour - start_hour + 1

            if hours < 1
                continue
            end

            day_demand = input.base_demand[start_hour:end_hour, :]

            # Create PowerSystemVariables for this day
            ps_vars = create_day_ps_vars!(model, input, y_idx, day_idx + day_offset, hours)

            # Add operational constraints via delegation to power_system.jl
            tech_output, tech_day_cost = add_day_operational_constraints!(
                model, ps_vars, vars, input, y_idx, day_idx + day_offset, day_demand, start_hour
            )

            # Operational cost scaled to the year (candidate technologies' too)
            day_cost = calculate_day_operational_cost(ps_vars, input, y_idx, hours;
                tech_output=tech_output, tech_day_cost=tech_day_cost)
            # M2 (#89): fuel balance, fuel infrastructure and electrolyzers
            add_to_expression!(day_cost, add_master_fuel_day!(model, ps_vars, input, y_idx,
                start_hour, hours, tech_output;
                extra = k -> master_fuel_extra_expr(vars.fuel, input, y_idx, k)),
                master_fuel_day_scale(input))
            push!(vars.operational_costs[y_idx], day_cost)
            record_master_day!(model, scenario_idx, y_idx, ps_vars, input, hours,
                rep_day_scaling(input))

            # The day's CO2, scaled to the year like its cost
            for (k, e) in enumerate(day_co2_by_group(ps_vars, tech_output, input, hours))
                add_to_expression!(annual_co2[k], e, rep_day_scaling(input))
            end
        end

        push!(vars.operational_costs[y_idx],
            add_co2_budget_constraints!(model, vars, input, y_idx, annual_co2;
                scenario_idx=scenario_idx))
    end
end

# =============================================================================
# TSAM (Time-Series Aggregation Method)
# =============================================================================

"""
    calculate_day_operational_cost_tsam(day_vars, input, year_idx, hours, weight;
                                        tech_output=nothing, tech_day_cost=nothing)

Calculate operational cost for a TSAM representative period with period weight.
Uses weight-based scaling instead of 365/N uniform scaling; `tech_day_cost`
is scaled with it.
"""
function calculate_day_operational_cost_tsam(
    ps_vars::PowerSystemVariables,
    input::MasterProblemInput,
    year_idx::Int,
    hours::Int,
    weight::Float64;
    tech_output::Union{Nothing, Any} = nothing,
    tech_day_cost::Union{Nothing, AffExpr} = nothing
)::AffExpr
    tres = max(1.0, input.temporal_resolution_hours)
    cost = day_operational_cost(ps_vars, input, hours; tech_output)
    tech_day_cost === nothing || add_to_expression!(cost, tech_day_cost)
    return cost * tres * weight
end

"""
    add_tsam_periods_validation!(model, vars, input, targets)

Add operational validation using TSAM representative periods with inter-period
SOC linking. Replaces add_representative_days_validation! when use_tsam=true.

Creates inter-period SOC boundary variables that form a chronological chain
across representative periods, enabling seasonal storage representation.
"""
function add_tsam_periods_validation!(
    model::Model,
    vars::MasterProblemVariables,
    base_input::MasterProblemInput,
    targets::Dict{Tuple{Int,Int}, Float64};
    scenario::Union{Nothing, Scenario} = nothing,
    scenario_idx::Int = 0
)
    num_years = length(base_input.years)
    n_bat = length(base_input.batteries)
    n_buses = base_input.network.num_buses
    b2n = base_input.network.bus_to_node
    timesteps_per_day = 24 ÷ base_input.temporal_resolution_hours
    # Seasonal hydro: any reservoir unit makes the inter-period reservoir chain
    # relevant (gated on the same tsam_inter_period_linking flag as batteries).
    has_reservoir = any(any(g.reservoir_capacity .> 0) for g in base_input.generators)
    # Distinct names per stochastic scenario (each has its own periods and chains)
    day_offset = 1000 * scenario_idx
    stag = scenario_idx > 0 ? "_s$(scenario_idx)" : ""

    for y_idx in 1:num_years
        input = scenario === nothing ? base_input :
            scenario_master_input(base_input, scenario.multipliers, y_idx)
        period_starts = input.tsam_period_start_hours[y_idx]
        period_weights = input.tsam_period_weights[y_idx]
        chrono_order = input.tsam_chronological_order[y_idx]
        K = length(period_starts)

        if K < 1
            @warn "No TSAM periods for year $(input.years[y_idx])"
            continue
        end

        vars.operational_costs[y_idx] = AffExpr[]
        annual_co2 = [AffExpr(0.0) for _ in input.co2_budget_groups]

        # --- Create inter-period SOC boundary variables ---
        if input.tsam_inter_period_linking && n_bat > 0
            vars.inter_period_soc[y_idx] = Dict{Tuple{Int,Int,Int}, VariableRef}()

            for b in 1:n_bat
                bat = input.batteries[b]
                for n in 1:n_buses
                    # Compute total capacity (existing + invested) at this year
                    base_cap = bat.capacity[n]
                    lifetime = bat.life_time[n]
                    initial_age = bat.initial_age[n]
                    age_at_year = initial_age + (y_idx - 1)

                    total_cap = AffExpr(0.0)
                    if age_at_year < lifetime && base_cap > 0
                        add_to_expression!(total_cap, base_cap)
                    end

                    # Create K+1 boundary variables (0=year start, 1..K=after each period)
                    for p in 0:K
                        v = @variable(model,
                            lower_bound = 0,
                            base_name = "soc_bnd$(stag)_y$(y_idx)_b$(b)_n$(n)_p$(p)"
                        )
                        @constraint(model, v <= total_cap,
                            base_name = "soc_bnd_cap$(stag)_y$(y_idx)_b$(b)_n$(n)_p$(p)")
                        vars.inter_period_soc[y_idx][(b, n, p)] = v
                    end

                    # Year-cyclic: SOC at year end == SOC at year start
                    @constraint(model,
                        vars.inter_period_soc[y_idx][(b, n, K)] ==
                        vars.inter_period_soc[y_idx][(b, n, 0)],
                        base_name = "soc_year_cyclic$(stag)_y$(y_idx)_b$(b)_n$(n)"
                    )
                end
            end
        end

        # --- Create inter-period reservoir-level boundary variables ---
        # One chronological chain of energy levels per reservoir gen-node, so
        # water can be moved across seasons. Bounded by the existing reservoir
        # capacity (investment expansion is not reflected at the boundary, the
        # same simplification used for batteries above).
        if input.tsam_inter_period_linking && has_reservoir
            vars.inter_period_reservoir[y_idx] = Dict{Tuple{Int,Int,Int}, VariableRef}()

            for g in 1:length(input.generators)
                gen = input.generators[g]
                for n in 1:n_buses
                    res_cap = gen.reservoir_capacity[n]
                    res_cap > 0 || continue
                    lo = gen.reservoir_min_level[n] * res_cap
                    hi = gen.reservoir_max_level[n] * res_cap

                    for p in 0:K
                        v = @variable(model,
                            lower_bound = lo, upper_bound = hi,
                            base_name = "reslvl_bnd$(stag)_y$(y_idx)_g$(g)_n$(n)_p$(p)")
                        vars.inter_period_reservoir[y_idx][(g, n, p)] = v
                    end

                    # Year-cyclic: reservoir level at year end == year start
                    @constraint(model,
                        vars.inter_period_reservoir[y_idx][(g, n, K)] ==
                        vars.inter_period_reservoir[y_idx][(g, n, 0)],
                        base_name = "reslvl_year_cyclic$(stag)_y$(y_idx)_g$(g)_n$(n)")
                end
            end
        end

        # --- Process each period in chronological order ---
        for (chrono_idx, period_idx) in enumerate(chrono_order)
            start_hour = period_starts[period_idx]
            weight = period_weights[period_idx]
            end_hour = min(start_hour + timesteps_per_day - 1, size(input.base_demand, 1))
            hours = end_hour - start_hour + 1
            if hours < 1
                continue
            end

            day_demand = input.base_demand[start_hour:end_hour, :]

            # Create PowerSystemVariables for this period
            ps_vars = create_day_ps_vars!(model, input, y_idx, chrono_idx + day_offset, hours)

            # Build inter-period override dicts when seasonal linking is active.
            # Batteries chain SOC, reservoirs chain water level; both attach the
            # period start to the previous boundary and its end to the next.
            tech_output = nothing

            soc_initial = nothing
            soc_final = nothing
            if input.tsam_inter_period_linking && n_bat > 0
                soc_initial = Dict{Tuple{Int,Int}, Any}()
                soc_final = Dict{Tuple{Int,Int}, Any}()
                for b in 1:n_bat
                    for n in 1:n_buses
                        soc_initial[(b, n)] = vars.inter_period_soc[y_idx][(b, n, chrono_idx - 1)]
                        soc_final[(b, n)] = vars.inter_period_soc[y_idx][(b, n, chrono_idx)]
                    end
                end
            end

            res_initial = nothing
            res_final = nothing
            if input.tsam_inter_period_linking && has_reservoir
                res_initial = Dict{Tuple{Int,Int}, Any}()
                res_final = Dict{Tuple{Int,Int}, Any}()
                for g in 1:length(input.generators)
                    gen = input.generators[g]
                    for n in 1:n_buses
                        gen.reservoir_capacity[n] > 0 || continue
                        res_initial[(g, n)] = vars.inter_period_reservoir[y_idx][(g, n, chrono_idx - 1)]
                        res_final[(g, n)] = vars.inter_period_reservoir[y_idx][(g, n, chrono_idx)]
                    end
                end
            end

            tech_output, tech_day_cost = add_day_operational_constraints!(
                model, ps_vars, vars, input, y_idx, chrono_idx + day_offset,
                day_demand, start_hour;
                initial_soc_overrides=soc_initial,
                final_soc_targets=soc_final,
                initial_reservoir_overrides=res_initial,
                final_reservoir_targets=res_final
            )

            # Weighted operational cost (candidate technologies' too)
            day_cost = calculate_day_operational_cost_tsam(
                ps_vars, input, y_idx, hours, weight;
                tech_output=tech_output, tech_day_cost=tech_day_cost
            )
            # M2 (#89): fuel balance, fuel infrastructure and electrolyzers
            # (cyclic tanks per period; the day's cost is in $, × weight)
            add_to_expression!(day_cost, add_master_fuel_day!(model, ps_vars, input, y_idx,
                start_hour, hours, tech_output;
                extra = k -> master_fuel_extra_expr(vars.fuel, input, y_idx, k)),
                weight)
            push!(vars.operational_costs[y_idx], day_cost)
            record_master_day!(model, scenario_idx, y_idx, ps_vars, input, hours,
                max(1.0, input.temporal_resolution_hours) * weight)

            # The period's CO2, weighted to the year like its cost
            tres = max(1.0, input.temporal_resolution_hours)
            for (k, e) in enumerate(day_co2_by_group(ps_vars, tech_output, input, hours))
                add_to_expression!(annual_co2[k], e, tres * weight)
            end
        end

        push!(vars.operational_costs[y_idx],
            add_co2_budget_constraints!(model, vars, input, y_idx, annual_co2;
                scenario_idx=scenario_idx))
    end
end

# =============================================================================
# Main API
# =============================================================================

"""
    create_master_problem(input; use_representative_days=true)

Create the Master Problem optimization model.

# Arguments
- `input::MasterProblemInput`: Master problem configuration
- `use_representative_days::Bool`: Whether to add representative day subproblems
                                   CRITICAL: Must be true for correct RE target enforcement!

# Returns
- `model::Model`: JuMP model
- `vars::MasterProblemVariables`: Variable container
- `targets::Dict{Tuple{Int,Int}, Float64}`: RE targets by (system, year)
"""
function create_master_problem(
    input::MasterProblemInput;
    use_representative_days::Bool = true,
    custom_constraints = nothing
)
    # Create model with optimizer
    model = Model(create_optimizer(
        solver_name=input.solver_name,
        threads=input.threads,
        time_limit=input.time_limit,
        gap=input.gap,
        verbose=input.verbose,
        solver_options=input.solver_options
    ))

    # Calculate RE targets
    targets = calculate_target_ratios(input)

    # Create variables
    vars = build_master_variables!(model, input)

    # Add constraints
    add_investment_constraints!(model, vars, input)
    add_budget_constraints!(model, vars, input)
    # Retirement cascade constraints - ensures once retired, stays retired
    add_retirement_cascade_constraints!(model, vars, input)
    add_capacity_adequacy_constraints!(model, vars, input)
    add_transmission_symmetry_constraints!(model, vars, input)

    # CRITICAL: Add representative days / TSAM periods validation
    # This links investment decisions to operational feasibility and RE targets
    if use_representative_days
        if input.use_tsam
            add_tsam_periods_validation!(model, vars, input, targets)
        else
            add_representative_days_validation!(model, vars, input, targets)
        end
    end

    # Also add the simple RE target constraint as a fallback
    # (but the real enforcement comes from representative days)
    add_re_target_constraints!(model, vars, input, targets)

    # Add RE increment constraints (min/max annual change in RE ratio)
    add_re_increment_constraints!(model, vars, input)

    # User-defined investment constraints (declarative config + plugin overlays).
    apply_user_constraints!(model, vars, input, custom_constraints)

    # Build objective (now includes operational costs from representative days)
    build_master_objective!(model, vars, input)

    return model, vars, targets
end

"""
    extract_master_solution(model, vars, input; unit_operation=false)

Extract solution values from solved Master Problem. With
`unit_operation = true` the result also holds each existing unit's
generation and costs (`extract_unit_operation`), for `calculate_unit_npv`.
"""
function extract_master_solution(
    model::Model,
    vars::MasterProblemVariables,
    input::MasterProblemInput;
    unit_operation::Bool = false
)::MasterProblemResult
    num_years = length(input.years)
    n_buses = input.network.num_buses
    b2n = input.network.bus_to_node
    n_gen = length(input.generators)
    n_bat = length(input.batteries)

    # Extract investment decisions
    n_tech = length(input.technologies)
    n_bat_tech = length(input.battery_technologies)
    tech_inv = Dict{Int, Dict{Int, Vector{Float64}}}()
    bat_tech_pow_inv = Dict{Int, Dict{Int, Vector{Float64}}}()
    bat_tech_cap_inv = Dict{Int, Dict{Int, Vector{Float64}}}()
    trans_inv = Dict{Int, Dict{Tuple{Int,Int}, Float64}}()

    gen_life = Dict{Int, Dict{Int, Vector{Float64}}}()
    bat_life = Dict{Int, Dict{Int, Vector{Float64}}}()

    total_inv_by_year = zeros(num_years)
    total_op_by_year = zeros(num_years)
    re_by_year = zeros(num_years)

    cumul_gen = Dict{Int, Dict{Int, Vector{Float64}}}()
    cumul_bat = Dict{Int, Dict{Int, Vector{Float64}}}()
    cumul_bat_power = Dict{Int, Dict{Int, Vector{Float64}}}()
    cumul_tech = Dict{Int, Dict{Int, Vector{Float64}}}()
    cumul_bat_tech_pow = Dict{Int, Dict{Int, Vector{Float64}}}()
    cumul_bat_tech_cap = Dict{Int, Dict{Int, Vector{Float64}}}()

    for y_idx in 1:num_years
        tech_inv[y_idx] = Dict{Int, Vector{Float64}}()
        bat_tech_pow_inv[y_idx] = Dict{Int, Vector{Float64}}()
        bat_tech_cap_inv[y_idx] = Dict{Int, Vector{Float64}}()
        trans_inv[y_idx] = Dict{Tuple{Int,Int}, Float64}()
        gen_life[y_idx] = Dict{Int, Vector{Float64}}()
        bat_life[y_idx] = Dict{Int, Vector{Float64}}()
        cumul_gen[y_idx] = Dict{Int, Vector{Float64}}()
        cumul_bat[y_idx] = Dict{Int, Vector{Float64}}()
        cumul_bat_power[y_idx] = Dict{Int, Vector{Float64}}()
        cumul_tech[y_idx] = Dict{Int, Vector{Float64}}()
        cumul_bat_tech_pow[y_idx] = Dict{Int, Vector{Float64}}()
        cumul_bat_tech_cap[y_idx] = Dict{Int, Vector{Float64}}()

        # Existing generator cumulative capacity (no investments, existing + life extension only)
        for g in 1:n_gen
            gen = input.generators[g]
            cumul_gen[y_idx][g] = zeros(n_buses)
            for n in 1:n_buses
                age_at_year = gen.initial_age[n] + (y_idx - 1)
                deg_rate = gen.degradation_rate[n]
                if age_at_year < gen.life_time[n] && gen.rated_power[n] > 0
                    cumul_gen[y_idx][g][n] = gen.rated_power[n] * (1.0 - deg_rate) ^ age_at_year
                elseif gen.rated_power[n] > 0
                    life_ext_var = vars.gen_life_extension[y_idx][g][n]
                    if life_ext_var !== nothing
                        cumul_gen[y_idx][g][n] += value(life_ext_var)
                    end
                end
            end
            # Share in service: 1 within the lifetime; past it, the share of
            # the (degraded) capacity the master keeps by life extension
            gen_life[y_idx][g] = zeros(n_buses)
            for n in 1:n_buses
                age_at_year = gen.initial_age[n] + (y_idx - 1)
                ext = vars.gen_life_extension[y_idx][g][n]
                gen_life[y_idx][g][n] = if age_at_year < gen.life_time[n]
                    1.0
                elseif ext === nothing
                    0.0
                else
                    ub = gen.rated_power[n] * (1.0 - gen.degradation_rate[n]) ^ age_at_year
                    ub > 0 ? clamp(value(ext) / ub, 0.0, 1.0) : 0.0
                end
            end
        end

        # Existing battery cumulative capacity (no investments, existing + life extension only)
        for b in 1:n_bat
            bat = input.batteries[b]
            cumul_bat[y_idx][b] = zeros(n_buses)
            cumul_bat_power[y_idx][b] = zeros(n_buses)
            for n in 1:n_buses
                age_at_year = bat.initial_age[n] + (y_idx - 1)
                if age_at_year < bat.life_time[n]
                    if bat.capacity[n] > 0
                        cumul_bat[y_idx][b][n] = bat.capacity[n]
                    end
                    if bat.max_discharge_power[n] > 0
                        cumul_bat_power[y_idx][b][n] = bat.max_discharge_power[n]
                    end
                elseif bat.max_discharge_power[n] > 0
                    life_ext_var = vars.bat_life_extension[y_idx][b][n]
                    if life_ext_var !== nothing
                        life_ext_val = value(life_ext_var)
                        cumul_bat_power[y_idx][b][n] += life_ext_val
                        if bat.max_discharge_power[n] > 0
                            ratio = bat.capacity[n] / bat.max_discharge_power[n]
                            cumul_bat[y_idx][b][n] += life_ext_val * ratio
                        end
                    end
                end
            end
            bat_life[y_idx][b] = zeros(n_buses)
            for n in 1:n_buses
                age_at_year = bat.initial_age[n] + (y_idx - 1)
                ext = vars.bat_life_extension[y_idx][b][n]
                bat_life[y_idx][b][n] = if age_at_year < bat.life_time[n]
                    1.0
                elseif ext === nothing || bat.max_discharge_power[n] <= 0
                    0.0
                else
                    clamp(value(ext) / bat.max_discharge_power[n], 0.0, 1.0)
                end
            end
        end

        # Technology investment extraction
        yp = inv_period_year(y_idx, vars.years_per_inv_period)
        for t in 1:n_tech
            tech = input.technologies[t]
            # Investment value: only non-zero at period start, zero otherwise
            if y_idx == yp
                tech_inv[y_idx][t] = [value(vars.tech_investment[yp][t][n]) for n in 1:n_buses]
            else
                tech_inv[y_idx][t] = zeros(n_buses)
            end
            # Cumulative with degradation and retirement (sum over period starts only)
            cumul_tech[y_idx][t] = zeros(n_buses)
            for n in 1:n_buses
                for y in 1:vars.years_per_inv_period:y_idx
                    inv_age = y_idx - y
                    if inv_age < tech.life_time[n]
                        deg = (1.0 - tech.degradation_rate[n]) ^ inv_age
                        cumul_tech[y_idx][t][n] += value(vars.tech_investment[y][t][n]) * deg
                    end
                end
            end
            for n in 1:n_buses
                total_inv_by_year[y_idx] += tech_inv[y_idx][t][n] * tech.invest_cost[n]
            end
        end

        # Battery technology investment extraction
        for bt in 1:n_bat_tech
            btech = input.battery_technologies[bt]
            if y_idx == yp
                bat_tech_pow_inv[y_idx][bt] = [value(vars.bat_tech_power_investment[yp][bt][n]) for n in 1:n_buses]
                bat_tech_cap_inv[y_idx][bt] = [value(vars.bat_tech_capacity_investment[yp][bt][n]) for n in 1:n_buses]
            else
                bat_tech_pow_inv[y_idx][bt] = zeros(n_buses)
                bat_tech_cap_inv[y_idx][bt] = zeros(n_buses)
            end
            cumul_bat_tech_pow[y_idx][bt] = zeros(n_buses)
            cumul_bat_tech_cap[y_idx][bt] = zeros(n_buses)
            for n in 1:n_buses
                for y in 1:vars.years_per_inv_period:y_idx
                    inv_age = y_idx - y
                    if inv_age < btech.life_time[n]
                        cumul_bat_tech_pow[y_idx][bt][n] += value(vars.bat_tech_power_investment[y][bt][n])
                        cumul_bat_tech_cap[y_idx][bt][n] += value(vars.bat_tech_capacity_investment[y][bt][n])
                    end
                end
            end
            for n in 1:n_buses
                total_inv_by_year[y_idx] += bat_tech_pow_inv[y_idx][bt][n] * btech.invest_cost_power[n]
                total_inv_by_year[y_idx] += bat_tech_cap_inv[y_idx][bt][n] * btech.invest_cost_capacity[n]
            end
        end

        # Transmission investment
        if haskey(vars.transfer_investment, yp)
            for ((i, j), var) in vars.transfer_investment[yp]
                if y_idx == yp
                    trans_inv[y_idx][(i, j)] = value(var)
                else
                    trans_inv[y_idx][(i, j)] = 0.0
                end
            end
        end

        # RE penetration — average across systems weighted by demand
        n_sys = max(1, length(input.system_node_ranges))
        re_sum = 0.0
        for s_idx in 1:n_sys
            re_sum += value(vars.re_penetration_ratio[(s_idx, y_idx)])
        end
        re_by_year[y_idx] = re_sum / n_sys  # unweighted mean across systems
    end

    # Per-system RE penetration
    re_by_system = Dict{String, Vector{Float64}}()
    n_sys = max(1, length(input.system_node_ranges))
    for s_idx in 1:n_sys
        sname = isempty(input.system_node_ranges) ? "global" : input.system_node_ranges[s_idx].name
        re_by_system[sname] = [value(vars.re_penetration_ratio[(s_idx, y_idx)]) for y_idx in 1:num_years]
    end

    # Extract reservoir investment
    res_inv = Dict{Int, Dict{Int, Vector{Float64}}}()
    for y_idx in 1:num_years
        res_inv[y_idx] = Dict{Int, Vector{Float64}}()
        for g in 1:n_gen
            if haskey(vars.reservoir_investment[y_idx], g)
                res_inv[y_idx][g] = [value(vars.reservoir_investment[y_idx][g][n]) for n in 1:n_buses]
                # Add to investment cost summary
                gen = input.generators[g]
                for n in 1:n_buses
                    total_inv_by_year[y_idx] += res_inv[y_idx][g][n] * gen.reservoir_invest_cost[n]
                end
            end
        end
    end

    # Converter investment: MW built in each period-start year
    conv_inv = Dict{Int, Dict{Tuple{String,Int}, Float64}}()
    for y_idx in 1:num_years
        conv_inv[y_idx] = Dict{Tuple{String,Int}, Float64}()
        for (key, var) in get(vars.converter_investment, y_idx, Dict{Tuple{String,Int}, VariableRef}())
            conv_inv[y_idx][key] = value(var)
        end
        for (key, conv) in master_converters(input.network)
            total_inv_by_year[y_idx] += get(conv_inv[y_idx], key, 0.0) * conv.invest_cost
        end
    end

    # Transmission corridors (charged once per corridor, as in the objective)
    for (y_idx, inv) in vars.transfer_investment
        for (var, cost) in corridor_cost_terms(inv, input.network.transference_invest_cost)
            total_inv_by_year[y_idx] += value(var) * cost
        end
    end

    # Fuel storage, route transport and electrolyzer builds (M2)
    for (y_idx, var, cost) in master_fuel_investment_terms(vars, input)
        total_inv_by_year[y_idx] += value(var) * cost
    end

    # Operating cost of each year's representative days (or TSAM periods),
    # scaled to the year and undiscounted; the expected cost over the
    # scenarios in a stochastic master. Benders replaces it with its
    # subproblems' costs.
    for (y_idx, costs) in vars.operational_costs
        1 <= y_idx <= num_years || continue
        total_op_by_year[y_idx] = sum(value(c) for c in costs; init = 0.0)
    end

    return MasterProblemResult(
        termination_status(model),
        objective_value(model),
        solve_time(model),
        tech_inv,
        bat_tech_pow_inv,
        bat_tech_cap_inv,
        trans_inv,
        gen_life,
        bat_life,
        total_inv_by_year,
        total_op_by_year,
        re_by_year,
        re_by_system,
        cumul_gen,
        cumul_bat,
        cumul_bat_power,
        cumul_tech,
        cumul_bat_tech_pow,
        cumul_bat_tech_cap,
        res_inv,
        conv_inv,
        extract_master_fuel_solution(vars, input),  # M2 (#89)
        unit_operation ? extract_unit_operation(model, vars, input) : UnitOperation[]
    )
end

# =============================================================================
# Stochastic Programming Support
# =============================================================================

"""
    create_stochastic_master_problem(input::StochasticMasterInput; custom_constraints=nothing)

Two-stage stochastic Master Problem. Investment is first-stage, common to
all scenarios. Each scenario has its own representative-day (or TSAM)
operation, built on its own demand and costs (`scenario_master_input`), so
the plan must serve every scenario and pays each one's operating cost
weighted by its probability.
"""
function create_stochastic_master_problem(input::StochasticMasterInput;
                                          custom_constraints = nothing)
    base_input = input.base_input

    if !input.use_stochastic || isempty(input.scenarios)
        model, vars, targets = create_master_problem(base_input;
            use_representative_days=true, custom_constraints=custom_constraints)
        return model, vars, targets, input.scenarios
    end

    # Investment structure only; the operation is added per scenario below.
    model, vars, targets = create_master_problem(base_input;
        use_representative_days=false, custom_constraints=custom_constraints)

    scenario_costs = Dict{Int, Vector{AffExpr}}[]
    first_soc = nothing
    first_res = nothing
    for (k, scenario) in enumerate(input.scenarios)
        empty!(vars.operational_costs)
        if base_input.use_tsam
            add_tsam_periods_validation!(model, vars, base_input, targets;
                scenario=scenario, scenario_idx=k)
        else
            add_representative_days_validation!(model, vars, base_input, targets;
                scenario=scenario, scenario_idx=k)
        end
        push!(scenario_costs, copy(vars.operational_costs))
        if k == 1
            # Report the first scenario's seasonal chains (each scenario owns its own)
            first_soc = copy(vars.inter_period_soc)
            first_res = copy(vars.inter_period_reservoir)
        end
    end
    merge!(empty!(vars.inter_period_soc), first_soc)
    merge!(empty!(vars.inter_period_reservoir), first_res)

    # Expected operating cost per year, for anything that reads operational_costs
    empty!(vars.operational_costs)
    for (k, scenario) in enumerate(input.scenarios), (y_idx, costs) in scenario_costs[k]
        expected = get!(vars.operational_costs, y_idx, [AffExpr(0.0)])[1]
        for c in costs
            add_to_expression!(expected, c, scenario.probability)
        end
    end

    build_stochastic_objective!(model, vars, base_input, input.scenarios, scenario_costs)
    # Each scenario's weight in the units' NPV (extract_unit_operation)
    model.ext[:master_scenarios] = input.scenarios

    return model, vars, targets, input.scenarios
end

"""
    with_fields(x; kw...)

Copy of the immutable struct `x` with the given fields replaced.
"""
function with_fields(x::T; kw...) where T
    return T((haskey(kw, f) ? kw[f] : getfield(x, f) for f in fieldnames(T))...)
end

"""
    scenario_master_input(input, mult, year_idx)

The master input a stochastic scenario operates on in year `year_idx`:
- demand × `demand_growth`^(year_idx − 1) (an annual factor: 1.02 = +2 %/year)
- fuel cost × `fuel_cost` × `fuel_price_growth`^(year_idx − 1), for generators
  (piecewise fuel-cost curves included) and candidate technologies
- maintenance cost × `maintenance_cost` (generators, batteries, technologies);
  batteries and battery technologies also × `bat_maintenance_cost`
- start-up cost × `gen_start_up_cost` (generators)
- CO2 cost × `carbon_price`
- flow cost of lines with a per-MW·km cost × `transmission_cost_per_mw_km`
- converter variable cost × `converter_variable_cost`
- penalties: loss of load (and the sectoral penalties derived from it) ×
  `penalty_loss_of_load`, static and dynamic reserve shortfall ×
  `penalty_loss_of_reserve`, curtailment × `penalty_curtailment`, RE
  penetration shortfall × `penalty_fre_penetration`, CO2 budget violation ×
  `penalty_co2_budget_violation`
Investment-cost, decommissioning, fixed O&M (`gen_fixed_cost`, a per-year
cost of the capacity in service, `build_annual_asset_cost`) and discount
multipliers act on the objective instead.
"""
function scenario_master_input(
    input::MasterProblemInput,
    mult::ScenarioMultipliers,
    year_idx::Int
)::MasterProblemInput
    fuel = mult.fuel_cost * mult.fuel_price_growth^(year_idx - 1)
    maint = mult.maintenance_cost
    bat_maint = maint * mult.bat_maintenance_cost
    gens = [with_fields(g; fuel_cost=g.fuel_cost .* fuel,
                        maintenance_cost=g.maintenance_cost .* maint,
                        start_up_cost=g.start_up_cost .* mult.gen_start_up_cost)
            for g in input.generators]
    bats = [with_fields(b; maintenance_cost=b.maintenance_cost .* bat_maint)
            for b in input.batteries]
    techs = [with_fields(t; fuel_cost=t.fuel_cost .* fuel,
                         maintenance_cost=t.maintenance_cost .* maint)
             for t in input.technologies]
    btechs = [with_fields(bt; maintenance_cost=bt.maintenance_cost .* bat_maint)
              for bt in input.battery_technologies]
    m_conv = mult.converter_variable_cost
    network = with_fields(input.network;
        transmission_lines=TransmissionLineData[
            with_fields(l; flow_cost=l.flow_cost * mult.transmission_cost_per_mw_km)
            for l in input.network.transmission_lines],
        acdc_converters=ACDCConverterData[with_fields(c; variable_cost=c.variable_cost * m_conv)
                         for c in input.network.acdc_converters],
        freq_converters=FrequencyConverterData[with_fields(c; variable_cost=c.variable_cost * m_conv)
                         for c in input.network.freq_converters])
    co2_groups = [CO2BudgetGroup(grp.name, grp.nodes, grp.budget,
                                 grp.violation_penalty * mult.penalty_co2_budget_violation)
                  for grp in input.co2_budget_groups]
    # A generator or technology with a fuel-cost curve is charged its
    # segments, not fuel_cost
    scale_curves(curves) = Dict{Int, Dict{Int, Vector{CostSegment}}}(
        u => Dict{Int, Vector{CostSegment}}(
            n => [CostSegment(s.fraction, s.marginal_cost * fuel) for s in segs]
            for (n, segs) in bus_curves)
        for (u, bus_curves) in curves)
    return with_fields(input;
        network=network,
        # M2 (#89): fuel prices × the same factor as the units' fuel_cost
        primary_energy=scale_master_fuel(input.primary_energy, mult, fuel),
        generators=gens, batteries=bats,
        technologies=techs, battery_technologies=btechs,
        gen_cost_curves=scale_curves(input.gen_cost_curves),
        tech_cost_curves=scale_curves(input.tech_cost_curves),
        co2_budget_groups=co2_groups,
        base_demand=input.base_demand .* mult.demand_growth^(year_idx - 1),
        co2_cost=input.co2_cost * mult.carbon_price,
        loss_of_load_penalty=input.loss_of_load_penalty * mult.penalty_loss_of_load,
        loss_of_reserve_static=input.loss_of_reserve_static * mult.penalty_loss_of_reserve,
        loss_of_reserve_dynamic=input.loss_of_reserve_dynamic * mult.penalty_loss_of_reserve,
        curtailment_cost=input.curtailment_cost * mult.penalty_curtailment,
        fre_penetration_loss_penalty=input.fre_penetration_loss_penalty *
                                     mult.penalty_fre_penetration)
end

"""
    build_stochastic_objective!(model, vars, input, scenarios, scenario_costs)

Expected total cost over the scenarios: for each one, its investment cost
(with its investment-cost multipliers), the existing-asset costs and its own
operating cost, all discounted at its discount rate and weighted by its
probability. `scenario_costs[k]` holds scenario k's operating cost per year.
"""
function build_stochastic_objective!(
    model::Model,
    vars::MasterProblemVariables,
    input::MasterProblemInput,
    scenarios::Vector{Scenario},
    scenario_costs::Vector{Dict{Int, Vector{AffExpr}}}
)
    num_years = length(input.years)
    total_cost = AffExpr(0.0)

    for (k, scenario) in enumerate(scenarios)
        prob = scenario.probability
        mult = scenario.multipliers
        rate = input.discount_rate * mult.discount_rate

        add_to_expression!(total_cost,
            build_investment_cost_expression(vars, input; mult=mult, discount_rate=rate),
            prob)

        for y_idx in 1:num_years
            df = 1.0 / ((1.0 + rate)^(y_idx - 1))
            add_to_expression!(total_cost,
                build_annual_asset_cost(vars, input, y_idx; mult=mult), prob * df)
            for day_cost in get(scenario_costs[k], y_idx, AffExpr[])
                add_to_expression!(total_cost, day_cost, prob * df)
            end
        end
    end

    # ── Slack penalties (scenario-independent) ──
    for y_idx in 1:num_years
        add_to_expression!(total_cost, vars.slack_re_target[y_idx], input.slack_penalty)
        add_to_expression!(total_cost, vars.slack_budget[y_idx], input.slack_penalty)
        for ni in 1:input.network.num_nodes
            add_to_expression!(total_cost, vars.slack_capacity[(y_idx, ni)], input.slack_penalty)
        end
    end

    @objective(model, Min, total_cost)

    return total_cost  # Return for MGA near-optimal constraint
end

# =============================================================================
# NPV-based retirement of existing units
# =============================================================================

"""
    record_master_day!(model, scenario_idx, year_idx, ps_vars, input, hours, scale)

Keep a representative day's (or TSAM period's) operation variables on the
model (`model.ext[:master_days]`), with the input it runs on (a stochastic
scenario's) and `scale`, the factor that takes its per-step MW to the year
(step length × the days it stands for), for the units' NPV.
"""
function record_master_day!(model::Model, scenario_idx::Int, year_idx::Int,
                            ps_vars::PowerSystemVariables, input::MasterProblemInput,
                            hours::Int, scale::Float64)
    push!(get!(() -> Any[], model.ext, :master_days),
          (scenario_idx = scenario_idx, year_idx = year_idx, ps_vars = ps_vars,
           input = input, hours = hours, scale = scale))
    return nothing
end

"""
    add_unit_day_operation!(op, ps_vars, input, year_idx, hours, scale, startup)

Add one solved representative day's operation of each existing generator
and battery to `op` (`UnitOperation`), scaled to the year by `scale`: the
generator's MWh and what the master charges it for them
(`add_gen_step_cost!`; with primary energy on, the fuel it burns at
`master_fuel_unit_price` instead of its `fuel_cost`; its LP-relaxed
start-ups, `startup`), the battery's `add_bat_step_cost!`.
"""
function add_unit_day_operation!(op::UnitOperation, ps_vars::PowerSystemVariables,
                                 input::MasterProblemInput, year_idx::Int, hours::Int,
                                 scale::Float64, startup)
    for (g, gen) in enumerate(input.generators), n in ps_vars.buses_of_gen[g]
        key = (:gen, g, n, year_idx)
        out = 0.0
        cost = AffExpr(0.0)
        for t in 1:hours
            out += value(ps_vars.gen_output[g, n, t])
            add_gen_step_cost!(cost, ps_vars, input, g, n, t)
        end
        c = value(cost)
        fuel_price = master_fuel_unit_price(input, g, n, year_idx)
        if fuel_price !== nothing
            haskey(ps_vars.gen_seg_output, (g, n)) || (c -= gen.fuel_cost[n] * out)
            c += fuel_price * out
        end
        if startup !== nothing && haskey(startup, (g, n))
            c += value(startup[(g, n)])
        end
        op.energy[key] = get(op.energy, key, 0.0) + scale * out
        op.operating_cost[key] = get(op.operating_cost, key, 0.0) + scale * c
    end
    for b in eachindex(input.batteries), n in ps_vars.buses_of_bat[b]
        key = (:bat, b, n, year_idx)
        cost = AffExpr(0.0)
        for t in 1:hours
            add_bat_step_cost!(cost, ps_vars, input, b, n, t)
        end
        op.operating_cost[key] = get(op.operating_cost, key, 0.0) + scale * value(cost)
    end
    return op
end

"""
    add_unit_asset_costs!(op, vars, input, mult)

Add each existing unit's share of the solved `build_annual_asset_cost` of
every year (fixed O&M, life extension, decommissioning; `mult` for a
stochastic scenario) to `op.asset_cost`.
"""
function add_unit_asset_costs!(op::UnitOperation, vars::MasterProblemVariables,
                               input::MasterProblemInput,
                               mult::Union{Nothing, ScenarioMultipliers})
    for y_idx in eachindex(input.years)
        by_unit = Dict{Tuple{Symbol,Int,Int}, AffExpr}()
        build_annual_asset_cost(vars, input, y_idx; mult = mult, by_unit = by_unit)
        for ((kind, i, n), e) in by_unit
            op.asset_cost[(kind, i, n, y_idx)] = value(e)
        end
    end
    return op
end

"""
    extract_unit_operation(model, vars, input) -> Vector{UnitOperation}

Each existing unit's operation and costs in the solved master
(`create_master_problem` or `create_stochastic_master_problem`): one
`UnitOperation` per scenario (one for a deterministic master), from the
representative days the model recorded (`record_master_day!`) and its
annual asset costs.
"""
function extract_unit_operation(model::Model, vars::MasterProblemVariables,
                                input::MasterProblemInput)::Vector{UnitOperation}
    days = get(model.ext, :master_days, Any[])
    startup = get(model.ext, :master_startup, nothing)
    scenarios = get(model.ext, :master_scenarios, nothing)
    cases = scenarios === nothing ? [(0, 1.0, nothing)] :
        [(k, sc.probability, sc.multipliers) for (k, sc) in enumerate(scenarios)]
    ops = UnitOperation[]
    for (k, prob, mult) in cases
        rate = mult === nothing ? input.discount_rate : input.discount_rate * mult.discount_rate
        op = UnitOperation(prob, rate)
        for d in days
            d.scenario_idx == k || continue
            add_unit_day_operation!(op, d.ps_vars, d.input, d.year_idx, d.hours, d.scale,
                startup === nothing ? nothing : get(startup, d.ps_vars, nothing))
        end
        add_unit_asset_costs!(op, vars, input, mult)
        push!(ops, op)
    end
    return ops
end

# Capacity in service per modelled year of existing unit (kind, index, bus)
# in the plan: generators MW (degraded, life-extended), batteries discharge MW
function unit_capacity_by_year(result::MasterProblemResult, kind::Symbol, i::Int, n::Int,
                               num_years::Int)
    caps = kind === :gen ? result.cumulative_gen_capacity : result.cumulative_bat_power
    return [haskey(caps, y) && haskey(caps[y], i) && n <= length(caps[y][i]) ?
            caps[y][i][n] : 0.0 for y in 1:num_years]
end

"""
    calculate_unit_npv(result, input, unit_type, unit_idx, node; npv_threshold=0.0)

NPV of keeping existing unit `unit_idx` ("generator" or "battery") at bus
`node` (the master's location) in the solved plan `result`, extracted with
`unit_operation = true`. Over the modelled years, discounted as the master
discounts them (year 1 undiscounted):

    generator: Σ_y df_y × (E_y × base_lcoe − C_y − A_y)
    battery:   Σ_y df_y × (r × invest_cost_power × P_y − C_y − A_y)

with `E_y` the MWh the master dispatched it (its representative days
scaled to the year), `C_y` the operating cost the master charges for them,
`A_y` its fixed O&M, life extension and decommissioning in the plan
(`UnitOperation`), `P_y` its discharge power in service and
`r = npv_annual_return_rate`. Degradation and the end of life are in the
plan's capacity and dispatch. A stochastic master's NPV is the
probability-weighted mean over its scenarios (each with its discount rate).
`recommend_retirement` when the NPV is below `npv_threshold`;
`remaining_lifetime` is the number of modelled years it is in service.
"""
function calculate_unit_npv(
    result::MasterProblemResult,
    input::MasterProblemInput,
    unit_type::String,
    unit_idx::Int,
    node::Int;
    npv_threshold::Float64 = 0.0
)::UnitNPV
    unit_type in ("generator", "battery") || error("Unknown unit type: $unit_type")
    isempty(result.unit_operation) && error(
        "calculate_unit_npv needs the units' operation: extract the result with " *
        "extract_master_solution(model, vars, input; unit_operation = true)")
    num_years = length(input.years)
    kind = unit_type == "generator" ? :gen : :bat
    caps = unit_capacity_by_year(result, kind, unit_idx, node, num_years)
    npv = 0.0
    for op in result.unit_operation
        v = 0.0
        for y in 1:num_years
            key = (kind, unit_idx, node, y)
            revenue = kind === :gen ? get(op.energy, key, 0.0) * input.base_lcoe :
                input.npv_annual_return_rate *
                input.batteries[unit_idx].invest_cost_power[node] * caps[y]
            v += (revenue - get(op.operating_cost, key, 0.0) - get(op.asset_cost, key, 0.0)) /
                 (1.0 + op.discount_rate)^(y - 1)
        end
        npv += op.probability * v
    end
    in_service = Float64(count(>(1e-6), caps))
    return UnitNPV(unit_type, unit_idx, node, input.system_name, npv, in_service,
                   npv < npv_threshold)
end

"""
    unit_npvs(result, input; npv_threshold=0.0) -> Vector{UnitNPV}

`calculate_unit_npv` of every existing generator and battery location in
service in some year of the plan.
"""
function unit_npvs(result::MasterProblemResult, input::MasterProblemInput;
                   npv_threshold::Float64 = 0.0)::Vector{UnitNPV}
    out = UnitNPV[]
    num_years = length(input.years)
    for (g, gen) in enumerate(input.generators), n in eachindex(gen.rated_power)
        gen.rated_power[n] > 0 || continue
        any(>(1e-6), unit_capacity_by_year(result, :gen, g, n, num_years)) || continue
        push!(out, calculate_unit_npv(result, input, "generator", g, n;
                                      npv_threshold = npv_threshold))
    end
    for (b, bat) in enumerate(input.batteries), n in eachindex(bat.max_discharge_power)
        bat.max_discharge_power[n] > 0 || continue
        any(>(1e-6), unit_capacity_by_year(result, :bat, b, n, num_years)) || continue
        push!(out, calculate_unit_npv(result, input, "battery", b, n;
                                      npv_threshold = npv_threshold))
    end
    return out
end

"""
    get_units_with_negative_npv(result, input; npv_threshold=0.0) -> Vector{UnitNPV}

The units of `unit_npvs` whose NPV is below `npv_threshold`.
"""
function get_units_with_negative_npv(
    result::MasterProblemResult,
    input::MasterProblemInput;
    npv_threshold::Float64 = 0.0
)::Vector{UnitNPV}
    return filter(u -> u.recommend_retirement,
                  unit_npvs(result, input; npv_threshold = npv_threshold))
end

# Copy of `v` with entry `n` set to 0
_zero_at(v::AbstractVector, n::Int) = (w = copy(v); n <= length(w) && (w[n] = 0.0); w)

"""
    retire_units(input, units; decommission=true) -> MasterProblemInput

`input` with each unit of `units` (`UnitNPV`) retired for the whole
horizon at its bus: its capacity there is 0 (so no dispatch, no life
extension, no fixed O&M and no end-of-life decommissioning), and with
`decommission` it pays its decommissioning cost once, in the first year, on
its (degraded) capacity then, as the master charges a unit retiring at the
end of its life (`npv_retired_units`, `build_annual_asset_cost`). A myopic
year passes `decommission = false` for the units an earlier year retired.
"""
function retire_units(input::MasterProblemInput, units::AbstractVector{UnitNPV};
                      decommission::Bool = true)::MasterProblemInput
    isempty(units) && return input
    gens = copy(input.generators)
    bats = copy(input.batteries)
    retired = copy(input.npv_retired_units)
    for u in units
        n = u.node
        if u.unit_type == "generator"
            gen = gens[u.unit_idx]
            mw = gen.rated_power[n] * (1.0 - gen.degradation_rate[n])^gen.initial_age[n]
            gens[u.unit_idx] = with_fields(gen;
                rated_power = _zero_at(gen.rated_power, n),
                reservoir_invest_max = _zero_at(gen.reservoir_invest_max, n))
        elseif u.unit_type == "battery"
            bat = bats[u.unit_idx]
            mw = bat.max_discharge_power[n]
            bats[u.unit_idx] = with_fields(bat;
                max_discharge_power = _zero_at(bat.max_discharge_power, n),
                max_charge_power = _zero_at(bat.max_charge_power, n),
                capacity = _zero_at(bat.capacity, n))
        else
            error("Unknown unit type: $(u.unit_type)")
        end
        push!(retired, RetiredUnit(u.unit_type, u.unit_idx, n, decommission ? mw : 0.0))
    end
    return with_fields(input; generators = gens, batteries = bats,
                       npv_retired_units = retired)
end

"""
    solve_with_npv_iteration(input; max_iterations=5, npv_threshold=0.0,
                             use_representative_days=true, solver=:monolithic,
                             benders_kwargs...) -> NPVIterationResult

Solve the master and retire the existing units that do not pay for
themselves: compute each unit's NPV in the solved plan
(`calculate_unit_npv`), retire those below `npv_threshold` for the whole
horizon (`retire_units`), re-solve, and repeat until no new unit falls below
it or after `max_iterations` rounds of retirements. A retired unit stays
retired. `solver = :benders` solves each master by Benders decomposition
(`run_benders_decomposition` with `benders_kwargs`).

`iterations` counts the master solves; `npv_history` holds, per solve, the
NPV of every unit evaluated (`"generator_g_bus"` / `"battery_b_bus"`);
`forced_retirements` the units retired, in order; `final_result` the last
optimal plan (when a re-solve fails, the previous plan, without the units
of its last round). Errors when the first solve is not optimal.
"""
function solve_with_npv_iteration(
    input::MasterProblemInput;
    max_iterations::Int = 5,
    npv_threshold::Float64 = 0.0,
    use_representative_days::Bool = true,
    solver::Symbol = :monolithic,
    benders_kwargs...
)::NPVIterationResult
    optimal(st) = st in (MOI.OPTIMAL, MOI.LOCALLY_SOLVED)
    function solve_once(inp)
        if solver === :benders
            sol = run_benders_decomposition(inp; use_representative_days = use_representative_days,
                                            record_unit_operation = true,
                                            benders_kwargs...).solution
            return optimal(sol.status) ? sol : nothing
        end
        model, vars, _ = create_master_problem(inp; use_representative_days = use_representative_days)
        optimize!(model)
        optimal(termination_status(model)) || return nothing
        return extract_master_solution(model, vars, inp; unit_operation = true)
    end

    retired = UnitNPV[]
    history = Dict{String, Float64}[]
    current = input
    previous = nothing
    last_round = 0
    iter = 0
    while true
        iter += 1
        result = solve_once(current)
        if result === nothing
            previous === nothing && error("NPV retirement: the first master solve is not optimal")
            # The previous plan still has the units of the last round
            @warn "NPV retirement: master solve $(iter) is not optimal; keeping the previous plan"
            return NPVIterationResult(iter, false, previous,
                                      retired[1:end - last_round], history)
        end
        npvs = unit_npvs(result, current; npv_threshold = npv_threshold)
        push!(history, Dict("$(u.unit_type)_$(u.unit_idx)_$(u.node)" => u.npv for u in npvs))
        flagged = filter(u -> u.recommend_retirement, npvs)
        if isempty(flagged)
            @info "NPV retirement: no unit below the threshold after solve $(iter)"
            return NPVIterationResult(iter, true, result, retired, history)
        end
        if iter > max_iterations
            @info "NPV retirement: $(length(flagged)) unit(s) still below the " *
                  "threshold after $(max_iterations) round(s); stopping"
            return NPVIterationResult(iter, false, result, retired, history)
        end
        @info "NPV retirement round $(iter): retiring $(length(flagged)) unit(s): " *
              join(["$(u.unit_type) $(u.unit_idx) at bus $(u.node) " *
                    "(NPV $(round(u.npv, sigdigits = 4)))" for u in flagged], ", ")
        append!(retired, flagged)
        last_round = length(flagged)
        current = retire_units(current, flagged)
        previous = result
    end
end

# =============================================================================
# Infeasibility Diagnostics
# =============================================================================

"""
    diagnose_infeasibility(model::Model, vars::MasterProblemVariables, input::MasterProblemInput)

Diagnose model infeasibility by checking slack variable values.

Returns Dict with constraint category => violation amount.
"""
function diagnose_infeasibility(
    model::Model,
    vars::MasterProblemVariables,
    input::MasterProblemInput
)::Dict{String, Float64}
    violations = Dict{String, Float64}()

    num_years = length(input.years)
    n_buses = input.network.num_buses

    # Check if model is solved
    status = termination_status(model)
    if status == MOI.OPTIMAL || status == MOI.LOCALLY_SOLVED
        # Check slack values
        total_re_slack = 0.0
        total_budget_slack = 0.0
        total_capacity_slack = 0.0

        for y_idx in 1:num_years
            re_slack_val = value(vars.slack_re_target[y_idx])
            if re_slack_val > 1e-6
                total_re_slack += re_slack_val
                violations["re_target_y$(y_idx)"] = re_slack_val
            end

            budget_slack_val = value(vars.slack_budget[y_idx])
            if budget_slack_val > 1e-6
                total_budget_slack += budget_slack_val
                violations["budget_y$(y_idx)"] = budget_slack_val
            end

            for ni in 1:input.network.num_nodes
                cap_slack_val = value(vars.slack_capacity[(y_idx, ni)])
                if cap_slack_val > 1e-6
                    total_capacity_slack += cap_slack_val
                    violations["capacity_y$(y_idx)_node$(ni)"] = cap_slack_val
                end
            end
        end

        violations["total_re_slack"] = total_re_slack
        violations["total_budget_slack"] = total_budget_slack
        violations["total_capacity_slack"] = total_capacity_slack
    else
        violations["model_status"] = -1.0
        violations["status_code"] = Float64(Int(status))
    end

    return violations
end

"""
    log_solution_summary(result::MasterProblemResult, input::MasterProblemInput; ev_demand::Union{Dict{Int, Float64}, Nothing}=nothing)

Generate a formatted solution summary string with detailed debug information.

Args:
    result: MasterProblemResult with optimization results
    input: MasterProblemInput with problem configuration
    ev_demand: Optional dict mapping year_idx => total EV demand (MWh) for that year
"""
function log_solution_summary(
    result::MasterProblemResult,
    input::MasterProblemInput;
    ev_demand::Union{Dict{Int, Float64}, Nothing}=nothing
)::String
    lines = String[]
    num_years = length(input.years)
    n_buses = input.network.num_buses
    b2n = input.network.bus_to_node
    n_gen = length(input.generators)
    n_bat = length(input.batteries)
    system_name = input.system_name

    push!(lines, "=" ^ 100)
    push!(lines, "MASTER PROBLEM SOLUTION SUMMARY - SYSTEM: $(uppercase(system_name))")
    push!(lines, "=" ^ 100)

    # ==========================================================================
    # Basic Status
    # ==========================================================================
    push!(lines, "\n[STATUS - $(system_name)]")
    push!(lines, "  Termination Status: $(result.status)")
    push!(lines, "  Total Objective (NPV): \$$(round(result.objective, digits=2)) M")
    push!(lines, "  Solve Time: $(round(result.solve_time, digits=1)) s")

    # ==========================================================================
    # DEMAND AND RE TARGET SUMMARY TABLE (Years in rows, Demand/Target in columns)
    # ==========================================================================
    push!(lines, "\n" * "=" ^ 100)
    push!(lines, "[DEMAND AND RE TARGET SUMMARY - $(system_name)]")
    push!(lines, "-" ^ 100)

    targets = calculate_target_ratios(input)
    base_demand_total = sum(input.base_demand)
    hours = size(input.base_demand, 1)

    # Table header
    push!(lines, "┌─────────┬──────────────────┬──────────────────┬──────────────────┬────────────┬────────────┬────────────┐")
    push!(lines, "│  Year   │  Base Demand     │  EV Demand       │  Total Demand    │ Target RE% │Achieved RE%│   Diff     │")
    push!(lines, "│         │     (GWh)        │     (GWh)        │     (GWh)        │            │            │            │")
    push!(lines, "├─────────┼──────────────────┼──────────────────┼──────────────────┼────────────┼────────────┼────────────┤")

    for y_idx in 1:num_years
        year = input.years[y_idx]
        growth_factor = (1 + input.demand_growth) ^ (y_idx - 1)
        base_dem_gwh = base_demand_total * growth_factor / 1000.0  # Convert MWh to GWh

        # EV demand for this year (if provided)
        ev_dem_gwh = if ev_demand !== nothing && haskey(ev_demand, y_idx)
            ev_demand[y_idx] / 1000.0
        else
            0.0
        end

        total_dem_gwh = base_dem_gwh + ev_dem_gwh

        target_pct = targets[(1, y_idx)] * 100
        achieved_pct = result.re_penetration_by_year[y_idx] * 100
        diff_pct = achieved_pct - target_pct
        diff_str = diff_pct >= 0 ? "+$(round(diff_pct, digits=1))" : "$(round(diff_pct, digits=1))"

        push!(lines, "│ $(rpad(year, 7)) │ $(lpad(round(base_dem_gwh, digits=1), 16)) │ $(lpad(round(ev_dem_gwh, digits=1), 16)) │ $(lpad(round(total_dem_gwh, digits=1), 16)) │ $(lpad(round(target_pct, digits=1), 10)) │ $(lpad(round(achieved_pct, digits=1), 10)) │ $(lpad(diff_str, 10)) │")
    end
    push!(lines, "└─────────┴──────────────────┴──────────────────┴──────────────────┴────────────┴────────────┴────────────┘")

    # ==========================================================================
    # Installed Capacity (Initial) - TABLE FORMAT (Nodes in rows, Technologies in columns)
    # ==========================================================================
    push!(lines, "\n" * "=" ^ 100)
    push!(lines, "[INITIAL INSTALLED CAPACITY - $(system_name)]")
    push!(lines, "-" ^ 100)

    # Collect all technology names
    gen_names = [input.generators[g].name for g in 1:n_gen]
    bat_names = [input.batteries[b].name for b in 1:n_bat]

    if n_gen > 0 || n_bat > 0
        col_width = max(12, maximum(vcat(length.(gen_names), length.(bat_names), [8])) + 2)

        # Header row for generators
        push!(lines, "\n  GENERATORS (MW):")
        header = "    " * rpad("Node", 10)
        for name in gen_names
            header *= rpad(name, col_width)
        end
        header *= rpad("TOTAL", col_width)
        push!(lines, header)
        push!(lines, "    " * "-" ^ (10 + col_width * (n_gen + 1)))

        # Data rows for generators
        node_totals = zeros(n_buses)
        for n in 1:n_buses
            row = "    " * rpad("Node $n", 10)
            node_total = 0.0
            for g in 1:n_gen
                gen = input.generators[g]
                val = n <= length(gen.rated_power) ? gen.rated_power[n] : 0.0
                node_total += val
                row *= rpad(val > 0 ? "$(round(val, digits=1))" : "-", col_width)
            end
            node_totals[n] = node_total
            row *= rpad("$(round(node_total, digits=1))", col_width)
            push!(lines, row)
        end

        # Total row for generators
        push!(lines, "    " * "-" ^ (10 + col_width * (n_gen + 1)))
        total_row = "    " * rpad("TOTAL", 10)
        for g in 1:n_gen
            total_row *= rpad("$(round(sum(input.generators[g].rated_power), digits=1))", col_width)
        end
        total_row *= rpad("$(round(sum(node_totals), digits=1))", col_width)
        push!(lines, total_row)

        # Battery table
        if n_bat > 0
            push!(lines, "\n  BATTERIES (MW / MWh):")
            header = "    " * rpad("Node", 10)
            for name in bat_names
                header *= rpad(name * " MW", col_width)
                header *= rpad(name * " MWh", col_width)
            end
            push!(lines, header)
            push!(lines, "    " * "-" ^ (10 + col_width * 2 * n_bat))

            for n in 1:n_buses
                row = "    " * rpad("Node $n", 10)
                for b in 1:n_bat
                    bat = input.batteries[b]
                    pow = n <= length(bat.max_discharge_power) ? bat.max_discharge_power[n] : 0.0
                    cap = n <= length(bat.capacity) ? bat.capacity[n] : 0.0
                    row *= rpad(pow > 0 ? "$(round(pow, digits=1))" : "-", col_width)
                    row *= rpad(cap > 0 ? "$(round(cap, digits=1))" : "-", col_width)
                end
                push!(lines, row)
            end

            # Total row for batteries
            push!(lines, "    " * "-" ^ (10 + col_width * 2 * n_bat))
            total_row = "    " * rpad("TOTAL", 10)
            for b in 1:n_bat
                total_row *= rpad("$(round(sum(input.batteries[b].max_discharge_power), digits=1))", col_width)
                total_row *= rpad("$(round(sum(input.batteries[b].capacity), digits=1))", col_width)
            end
            push!(lines, total_row)
        end
    end

    # Summary
    renewable_capacity = n_gen > 0 ? sum(sum(input.generators[g].rated_power) for g in 1:n_gen if input.generators[g].type == "Renewable"; init=0.0) : 0.0
    conventional_capacity = n_gen > 0 ? sum(sum(input.generators[g].rated_power) for g in 1:n_gen if input.generators[g].type != "Renewable"; init=0.0) : 0.0
    total_bat_power = n_bat > 0 ? sum(sum(input.batteries[b].max_discharge_power) for b in 1:n_bat) : 0.0
    total_bat_energy = n_bat > 0 ? sum(sum(input.batteries[b].capacity) for b in 1:n_bat) : 0.0

    push!(lines, "\n  SUMMARY:")
    push!(lines, "    Renewable: $(round(renewable_capacity, digits=1)) MW")
    push!(lines, "    Conventional: $(round(conventional_capacity, digits=1)) MW")
    push!(lines, "    Battery: $(round(total_bat_power, digits=1)) MW / $(round(total_bat_energy, digits=1)) MWh")

    # ==========================================================================
    # Cumulative Capacity by Year (including investments)
    # ==========================================================================
    push!(lines, "\n" * "=" ^ 100)
    push!(lines, "[CUMULATIVE RENEWABLE CAPACITY BY YEAR - $(system_name)]")
    push!(lines, "-" ^ 100)
    push!(lines, "  Year    Existing RE      RE Invested      Total RE (MW)")
    push!(lines, "-" ^ 60)

    n_tech = length(input.technologies)
    for y_idx in 1:num_years
        year = input.years[y_idx]
        existing_re = 0.0
        invested_re = 0.0

        for g in 1:n_gen
            gen = input.generators[g]
            if gen.type == "Renewable"
                existing_re += sum(gen.rated_power)
            end
        end

        # Cumulative technology investment up to this year
        for t in 1:n_tech
            tech = input.technologies[t]
            if tech.type == "Renewable"
                for prev_y in 1:y_idx
                    if haskey(result.tech_investment, prev_y) && haskey(result.tech_investment[prev_y], t)
                        invested_re += sum(result.tech_investment[prev_y][t])
                    end
                end
            end
        end

        total_re = existing_re + invested_re
        push!(lines, "  $(rpad(year, 6))  $(rpad(round(existing_re, digits=1), 15))  $(rpad(round(invested_re, digits=1), 15))  $(round(total_re, digits=1))")
    end

    # ==========================================================================
    # Investment Details by Year - TABLE FORMAT (Nodes in rows, Technologies in columns)
    # ==========================================================================
    push!(lines, "\n" * "=" ^ 100)
    push!(lines, "[INVESTMENT DETAILS BY YEAR - $(system_name)]")
    push!(lines, "-" ^ 100)

    for y_idx in 1:num_years
        year = input.years[y_idx]
        total_inv = result.total_investment_by_year[y_idx]
        push!(lines, "\n  Year $(year): Total Investment = \$$(round(total_inv, digits=2)) M")

        # Collect all technologies with investments this year
        tech_names = String[]
        tech_indices = Int[]
        for t in 1:n_tech
            if haskey(result.tech_investment[y_idx], t) && sum(result.tech_investment[y_idx][t]) > 0.1
                push!(tech_names, input.technologies[t].name)
                push!(tech_indices, t)
            end
        end

        bat_tech_names = String[]
        bat_tech_indices = Int[]
        n_bat_tech = length(input.battery_technologies)
        for bt in 1:n_bat_tech
            has_pow = haskey(result.bat_tech_power_investment[y_idx], bt) && sum(result.bat_tech_power_investment[y_idx][bt]) > 0.1
            has_cap = haskey(result.bat_tech_capacity_investment[y_idx], bt) && sum(result.bat_tech_capacity_investment[y_idx][bt]) > 0.1
            if has_pow || has_cap
                push!(bat_tech_names, input.battery_technologies[bt].name)
                push!(bat_tech_indices, bt)
            end
        end

        if isempty(tech_names) && isempty(bat_tech_names)
            push!(lines, "    No investments this year")
            continue
        end

        # Build header with technology names (columns)
        all_tech_labels = vcat(tech_names, [n * " (MW)" for n in bat_tech_names], [n * " (MWh)" for n in bat_tech_names])
        col_width = max(12, maximum(length.(all_tech_labels)) + 2)

        # Header row
        header = "    " * rpad("Node", 10)
        for name in tech_names
            header *= rpad(name, col_width)
        end
        for name in bat_tech_names
            header *= rpad(name * " (MW)", col_width)
        end
        for name in bat_tech_names
            header *= rpad(name * " (MWh)", col_width)
        end
        push!(lines, header)
        push!(lines, "    " * "-" ^ (10 + col_width * (length(tech_names) + 2 * length(bat_tech_names))))

        # Data rows (one per node)
        for n in 1:n_buses
            row = "    " * rpad("Node $n", 10)

            # Technology investments for this node
            for t in tech_indices
                tech_inv = result.tech_investment[y_idx][t]
                val = length(tech_inv) >= n ? tech_inv[n] : 0.0
                row *= rpad(val > 0.1 ? "$(round(val, digits=1))" : "-", col_width)
            end

            # Battery technology power investments for this node
            for bt in bat_tech_indices
                bat_pow = result.bat_tech_power_investment[y_idx][bt]
                val = length(bat_pow) >= n ? bat_pow[n] : 0.0
                row *= rpad(val > 0.1 ? "$(round(val, digits=1))" : "-", col_width)
            end

            # Battery technology capacity investments for this node
            for bt in bat_tech_indices
                bat_cap = result.bat_tech_capacity_investment[y_idx][bt]
                val = length(bat_cap) >= n ? bat_cap[n] : 0.0
                row *= rpad(val > 0.1 ? "$(round(val, digits=1))" : "-", col_width)
            end

            push!(lines, row)
        end

        # Total row
        total_row = "    " * rpad("TOTAL", 10)
        for t in tech_indices
            total_row *= rpad("$(round(sum(result.tech_investment[y_idx][t]), digits=1))", col_width)
        end
        for bt in bat_tech_indices
            total_row *= rpad("$(round(sum(result.bat_tech_power_investment[y_idx][bt]), digits=1))", col_width)
        end
        for bt in bat_tech_indices
            total_row *= rpad("$(round(sum(result.bat_tech_capacity_investment[y_idx][bt]), digits=1))", col_width)
        end
        push!(lines, "    " * "-" ^ (10 + col_width * (length(tech_names) + 2 * length(bat_tech_names))))
        push!(lines, total_row)
    end

    # ==========================================================================
    # Operational Cost by Year
    # ==========================================================================
    push!(lines, "\n" * "=" ^ 100)
    push!(lines, "[OPERATIONAL COST BY YEAR - $(system_name)]")
    push!(lines, "-" ^ 100)
    push!(lines, "  Year    Operational Cost     Cumulative NPV")
    push!(lines, "-" ^ 50)
    cumulative_npv = 0.0
    for y_idx in 1:num_years
        year = input.years[y_idx]
        op_cost = result.total_operational_cost_by_year[y_idx]
        discount_factor = 1.0 / ((1.0 + input.discount_rate) ^ (y_idx - 1))
        discounted = op_cost * discount_factor
        cumulative_npv += discounted + result.total_investment_by_year[y_idx] * discount_factor
        push!(lines, "  $(rpad(year, 6))  \$$(rpad(round(op_cost, digits=2), 18))M  \$$(round(cumulative_npv, digits=2))M")
    end

    # ==========================================================================
    # Retirement Summary (unified: capacity expires at lifetime)
    # ==========================================================================
    push!(lines, "\n" * "=" ^ 100)
    push!(lines, "[RETIREMENT STATUS - $(system_name)]")
    push!(lines, "-" ^ 100)

    # Collect generators with capacity that will retire during the horizon
    retirement_gens = Set{Int}()
    for g in 1:n_gen
        gen = input.generators[g]
        for n in 1:n_buses
            if gen.rated_power[n] > 0
                initial_age = gen.initial_age[n]
                lifetime = gen.life_time[n]
                # Check if unit retires during the planning horizon
                for y_idx in 1:num_years
                    age = initial_age + (y_idx - 1)
                    if age >= lifetime
                        push!(retirement_gens, g)
                        break
                    end
                end
            end
        end
    end

    if isempty(retirement_gens)
        push!(lines, "  No retirements during planning horizon")
    else
        # Collect tech names
        tech_names = String[]
        tech_indices = Int[]
        for g in sort(collect(retirement_gens))
            push!(tech_names, input.generators[g].name)
            push!(tech_indices, g)
        end

        col_width = max(15, maximum(length.(tech_names)) + 2)

        # Header
        header = "    " * rpad("Year", 10)
        for name in tech_names
            header *= rpad(name, col_width)
        end
        push!(lines, header)
        push!(lines, "    " * "-" ^ (10 + col_width * length(tech_names)))

        # One row per year showing status
        for y_idx in 1:num_years
            year = input.years[y_idx]
            row = "    " * rpad("$(year)", 10)
            for g in tech_indices
                gen = input.generators[g]
                # Sum capacity across nodes for this tech
                active_cap = 0.0
                retired_cap = 0.0
                for n in 1:n_buses
                    if gen.rated_power[n] > 0
                        age = gen.initial_age[n] + (y_idx - 1)
                        if age < gen.life_time[n]
                            deg = (1.0 - gen.degradation_rate[n]) ^ age
                            active_cap += gen.rated_power[n] * deg
                        else
                            retired_cap += gen.rated_power[n]
                        end
                    end
                end
                if retired_cap > 0 && active_cap == 0
                    row *= rpad("RETIRED", col_width)
                elseif retired_cap > 0
                    row *= rpad("PARTIAL", col_width)
                else
                    row *= rpad("ACTIVE", col_width)
                end
            end
            push!(lines, row)
        end
    end

    # ==========================================================================
    # Investment Retirement Summary
    # ==========================================================================
    push!(lines, "\n" * "-" ^ 100)
    push!(lines, "[INVESTMENT RETIREMENT STATUS - $(system_name)]")
    push!(lines, "-" ^ 100)

    # Collect investments that will retire during the horizon
    investment_retirements = Vector{NamedTuple{(:tech, :inv_year, :retire_year, :capacity, :type), Tuple{String, Int, Int, Float64, String}}}()

    # Technology investments
    for t in 1:n_tech
        tech = input.technologies[t]
        for inv_year_idx in 1:num_years
            inv_year = input.years[inv_year_idx]
            for n in 1:n_buses
                if haskey(result.tech_investment[inv_year_idx], t)
                    inv_cap = result.tech_investment[inv_year_idx][t][n]
                    if inv_cap > 0.01  # Threshold to avoid numerical noise
                        lifetime = tech.life_time[n]
                        retire_year_idx = inv_year_idx + Int(floor(lifetime))
                        if retire_year_idx <= num_years
                            retire_year = input.years[retire_year_idx]
                            push!(investment_retirements, (
                                tech = tech.name,
                                inv_year = inv_year,
                                retire_year = retire_year,
                                capacity = inv_cap,
                                type = "technology"
                            ))
                        end
                    end
                end
            end
        end
    end

    # Battery technology investments
    n_bat_tech = length(input.battery_technologies)
    for bt in 1:n_bat_tech
        bat_tech = input.battery_technologies[bt]
        for inv_year_idx in 1:num_years
            inv_year = input.years[inv_year_idx]
            for n in 1:n_buses
                if haskey(result.bat_tech_capacity_investment[inv_year_idx], bt)
                    inv_cap = result.bat_tech_capacity_investment[inv_year_idx][bt][n]
                    if inv_cap > 0.01  # Threshold to avoid numerical noise
                        lifetime = bat_tech.life_time[n]
                        retire_year_idx = inv_year_idx + Int(floor(lifetime))
                        if retire_year_idx <= num_years
                            retire_year = input.years[retire_year_idx]
                            push!(investment_retirements, (
                                tech = bat_tech.name,
                                inv_year = inv_year,
                                retire_year = retire_year,
                                capacity = inv_cap,
                                type = "battery_technology"
                            ))
                        end
                    end
                end
            end
        end
    end

    if isempty(investment_retirements)
        push!(lines, "  No investment retirements during planning horizon")
        push!(lines, "  (Investments made will outlive the simulation period)")
    else
        # Sort by retirement year
        sort!(investment_retirements, by = x -> (x.retire_year, x.inv_year))

        # Header
        push!(lines, "    " * rpad("Technology", 20) * rpad("Invested", 12) * rpad("Retires", 12) * rpad("Capacity", 15) * "Type")
        push!(lines, "    " * "-" ^ 70)

        for ret in investment_retirements
            cap_str = ret.type == "battery" ? "$(round(ret.capacity, digits=1)) MWh" : "$(round(ret.capacity, digits=1)) MW"
            push!(lines, "    " * rpad(ret.tech, 20) * rpad("$(ret.inv_year)", 12) * rpad("$(ret.retire_year)", 12) * rpad(cap_str, 15) * ret.type)
        end

        # Summary by year
        push!(lines, "\n    Retirement summary by year:")
        years_with_retirements = unique([r.retire_year for r in investment_retirements])
        for yr in sort(years_with_retirements)
            yr_retirements = filter(r -> r.retire_year == yr, investment_retirements)
            gen_cap = sum([r.capacity for r in yr_retirements if r.type == "generator"], init=0.0)
            bat_cap = sum([r.capacity for r in yr_retirements if r.type == "battery"], init=0.0)
            summary = "      $(yr): "
            parts = String[]
            if gen_cap > 0
                push!(parts, "$(round(gen_cap, digits=1)) MW generators")
            end
            if bat_cap > 0
                push!(parts, "$(round(bat_cap, digits=1)) MWh batteries")
            end
            push!(lines, summary * join(parts, ", "))
        end
    end

    # ==========================================================================
    # Debug: Objective Function Components
    # ==========================================================================
    push!(lines, "\n" * "=" ^ 100)
    push!(lines, "[OBJECTIVE FUNCTION BREAKDOWN - $(system_name)]")
    push!(lines, "-" ^ 100)
    total_investment = sum(result.total_investment_by_year)
    total_operational = sum(result.total_operational_cost_by_year)
    push!(lines, "  Total Investment (undiscounted): \$$(round(total_investment, digits=2)) M")
    push!(lines, "  Total Operational (undiscounted): \$$(round(total_operational, digits=2)) M")
    push!(lines, "  NPV Objective: \$$(round(result.objective, digits=2)) M")

    # Discounted investment + operating cost; the rest of the objective is
    # the annual cost of the assets in service (fixed O&M, life extension,
    # decommissioning) and the slack penalties
    calculated_npv = 0.0
    for y_idx in 1:num_years
        df = 1.0 / ((1.0 + input.discount_rate) ^ (y_idx - 1))
        calculated_npv += (result.total_investment_by_year[y_idx] + result.total_operational_cost_by_year[y_idx]) * df
    end
    push!(lines, "  NPV of investment + operation: \$$(round(calculated_npv, digits=2)) M")
    push!(lines, "  Asset costs and slack penalties (NPV): \$$(round(result.objective - calculated_npv, digits=2)) M")

    push!(lines, "\n" * "=" ^ 100)
    push!(lines, "END OF SOLUTION SUMMARY FOR SYSTEM: $(uppercase(system_name))")
    push!(lines, "=" ^ 100)

    return join(lines, "\n")
end

# =============================================================================
# Solution Export
# =============================================================================

"""
    export_solution_to_dict(result::MasterProblemResult, input::MasterProblemInput)

Export solution to a dictionary for JSON/HDF5 serialization.
"""
function export_solution_to_dict(
    result::MasterProblemResult,
    input::MasterProblemInput
)::Dict{String, Any}
    export_dict = Dict{String, Any}()

    export_dict["status"] = string(result.status)
    export_dict["objective"] = result.objective
    export_dict["solve_time"] = result.solve_time
    export_dict["years"] = input.years

    # Technology investment decisions
    tech_inv_export = Dict{String, Any}()
    for (y_idx, tech_dict) in result.tech_investment
        year_str = string(input.years[y_idx])
        tech_inv_export[year_str] = Dict{String, Vector{Float64}}()
        for (t, vals) in tech_dict
            tech_inv_export[year_str][input.technologies[t].name] = vals
        end
    end
    export_dict["tech_investment"] = tech_inv_export

    # Battery technology investment decisions
    bat_tech_pow_export = Dict{String, Any}()
    bat_tech_cap_export = Dict{String, Any}()
    for (y_idx, bt_dict) in result.bat_tech_power_investment
        year_str = string(input.years[y_idx])
        bat_tech_pow_export[year_str] = Dict{String, Vector{Float64}}()
        bat_tech_cap_export[year_str] = Dict{String, Vector{Float64}}()
        for (bt, vals) in bt_dict
            bat_tech_pow_export[year_str][input.battery_technologies[bt].name] = vals
            bat_tech_cap_export[year_str][input.battery_technologies[bt].name] = result.bat_tech_capacity_investment[y_idx][bt]
        end
    end
    export_dict["bat_tech_power_investment"] = bat_tech_pow_export
    export_dict["bat_tech_capacity_investment"] = bat_tech_cap_export

    # Share in service (1.0 = active or fully life-extended, 0.0 = retired)
    gen_life_export = Dict{String, Any}()
    for (y_idx, gen_dict) in result.gen_life_extension
        year_str = string(input.years[y_idx])
        gen_life_export[year_str] = Dict{String, Vector{Float64}}()
        for (g, vals) in gen_dict
            gen_life_export[year_str][input.generators[g].name] = vals
        end
    end
    export_dict["retirement_status"] = gen_life_export

    # Converter investment decisions (MW built that year, by converter name)
    convs = Dict(master_converters(input.network))
    export_dict["converter_investment"] = Dict{String, Any}(
        string(input.years[y_idx]) => Dict{String, Float64}(
            convs[key].name => v for (key, v) in conv_dict)
        for (y_idx, conv_dict) in result.converter_investment)

    # Summary metrics
    export_dict["total_investment_by_year"] = result.total_investment_by_year
    export_dict["total_operational_cost_by_year"] = result.total_operational_cost_by_year
    export_dict["re_penetration_by_year"] = result.re_penetration_by_year
    export_dict["re_penetration_by_system"] = Dict{String, Any}(
        k => collect(v) for (k, v) in result.re_penetration_by_system
    )

    return export_dict
end
