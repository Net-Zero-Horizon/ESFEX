"""
benders.jl - Benders Decomposition for Master Problem

Decomposes the monolithic capacity expansion problem into:
- Master: investment decisions + recourse variables θ[y,d]
- Subproblems: operational dispatch per (year, representative day) with fixed capacities

Algorithm:
1. Create Benders master (investment-only), one θ[y,d] per (year, representative day)
2. Iterate:
   a. Solve master → x*, θ*; LB = objective
   b. Fix capacities from x*, solve all subproblems → Q_{y,d}
   c. If a subproblem breaks the curtailment limit, add a feasibility cut;
      otherwise UB = min(UB, first-stage cost(x*) + Σ Q_{y,d})
   d. Generate one optimality cut per subproblem: θ[y,d] ≥ intercept + gradient^T × x
   e. Check convergence: (UB - LB) / |UB| < tolerance
"""

# =============================================================================
# Constants
# =============================================================================

# Cost scaling factor of the Benders cuts: theta represents
# operational_cost / COST_SCALE, and the cuts and objective account for it.
# The bridge already sends every cost in M$ (converters.scale_cost), so theta
# is in the input's units (1.0). With 1e6 here theta was in units of 1e12 $:
# a cut coefficient per MWh (reservoir, fuel tank, CO2) fell below HiGHS's
# 1e-9 matrix tolerance and was dropped, so the master never saw it.
const COST_SCALE = 1.0

# Curtailment above the limit (MW summed over the day's steps) below which a
# subproblem counts as meeting it
const CURTAILMENT_FEAS_TOL = 1e-6

# =============================================================================
# Types
# =============================================================================

"""Fixed capacity values passed from master to each subproblem."""
struct FixedCapacities
    # Existing generators: (gen_idx, bus) => Float64 effective capacity (MW)
    gen_capacity::Dict{Tuple{Int,Int}, Float64}
    # Existing batteries: (bat_idx, bus) => Float64 (MW or MWh)
    bat_power::Dict{Tuple{Int,Int}, Float64}
    bat_energy::Dict{Tuple{Int,Int}, Float64}
    # Technology investments: (tech_idx, bus) => Float64 cumulative capacity (MW)
    tech_capacity::Dict{Tuple{Int,Int}, Float64}
    # Battery technology investments: (bat_tech_idx, bus) => Float64
    bat_tech_power::Dict{Tuple{Int,Int}, Float64}
    bat_tech_energy::Dict{Tuple{Int,Int}, Float64}
    # Transmission: line_idx => Float64 total capacity (MW)
    line_capacity::Dict{Int, Float64}
    # RE ratios for this year (per system, from master target)
    re_ratios::Vector{Float64}
    # Values of the copy variables (BendersCopyKey => value): fuel
    # storage/transport and electrolyzer capacity in service this year (M2,
    # #89), converter and line capacity built, CO2 allocations. Keys not
    # listed take `copy_value`'s default.
    fuel_extra::Dict{Tuple{Symbol, String, Int}, Float64}
end

# Without fuel builds (warm starts, no primary energy)
FixedCapacities(args::Vararg{Any, 8}) =
    FixedCapacities(args..., Dict{Tuple{Symbol, String, Int}, Float64}())

"""A single Benders optimality cut."""
struct BendersCut
    intercept::Float64
    # Gradients w.r.t. cumulative capacity parameters (aggregated across timesteps)
    # (tech, bus) => marginal value of capacity
    tech_gradients::Dict{Tuple{Int,Int}, Float64}
    bat_tech_pow_gradients::Dict{Tuple{Int,Int}, Float64}
    bat_tech_cap_gradients::Dict{Tuple{Int,Int}, Float64}
    # (gen, bus) => marginal value of life extension capacity
    gen_life_ext_gradients::Dict{Tuple{Int,Int}, Float64}
    bat_life_ext_gradients::Dict{Tuple{Int,Int}, Float64}
    # line_idx => marginal value of line capacity
    line_gradients::Dict{Int, Float64}
    # Copy-variable key => marginal value of that quantity in the cut's year
    # (benders_copy_expr maps it to the master's variables)
    fuel_gradients::Dict{Tuple{Symbol, String, Int}, Float64}
end

BendersCut(args::Vararg{Any, 7}) =
    BendersCut(args..., Dict{Tuple{Symbol, String, Int}, Float64}())

"""Subproblem data container for a single representative day."""
struct SubproblemData
    model::Model
    ps_vars::PowerSystemVariables
    tech_output::Any                    # tech dispatch variables (or nothing)
    bat_tech_discharge::Any
    bat_tech_charge::Any
    bat_tech_soc::Any
    # Named constraint references for dual extraction
    constraint_store::Dict{String, Any}
    # Metadata
    year_idx::Int
    day_idx::Int
    hours::Int
    start_hour::Int
    scaling_factor::Float64             # for cost annualization (tres * days_in_year / num_rep_days)
end

# =============================================================================
# Copy variables
# =============================================================================
#
# A master quantity that a subproblem reads (a built capacity, the RE ratio, a
# CO2 allocation) enters the subproblem as a copy variable fixed to the
# master's value. The dual of the fixing constraint is the day's marginal cost
# of that quantity, and the cut multiplies it by the master expression the
# key stands for (`benders_copy_expr`). Keys are `(kind, name, index)`:
#
#   (:storage | :transport | :electrolyzer, …)  fuel block (M2, master_fuel.jl)
#   (:converter, "acdc" | "freq", c)  converter MW built up to the year
#   (:line, "", l)                    MW built on line l's corridor up to the year
#   (:reservoir, "g", b)              reservoir MWh built up to the year
#   (:gen_life, "g", b), (:bat_life, "bi", b)  life-extended MW this year
#   (:re_ratio, "s", 0)               system s's RE ratio this year
#   (:co2, "k", d)                    share of group k's CO2 budget given to day d

const BendersCopyKey = Tuple{Symbol, String, Int}

"""
Master-side data the copy keys refer to: the CO2 allocation variables
`(year, group, day) => budget share` and the bus pair of each line (`TransmissionDC`
order, which `(:line, "", l)` indexes).
"""
struct BendersCopyContext
    co2_alloc::Dict{Tuple{Int,Int,Int}, VariableRef}
    line_ends::Vector{Tuple{Int,Int}}
end

BendersCopyContext() = BendersCopyContext(Dict{Tuple{Int,Int,Int}, VariableRef}(),
                                          Tuple{Int,Int}[])

"""
    benders_line_ends(input) -> Vector{Tuple{Int,Int}}

Bus pair of each line of the subproblems' DC network (empty when they use
the copper-plate balance).
"""
function benders_line_ends(input::MasterProblemInput)
    (input.network.num_buses > 1 && !isempty(input.network.transmission_lines)) ||
        return Tuple{Int,Int}[]
    return TransmissionDC(input.network).lines
end

"""
    add_benders_co2_allocation!(model, input, rep_days_by_year) -> Dict

CO2 budget decomposition. The annual budget of a group couples every
representative day of the year, so the master shares it out: a variable
`a[y,k,d] ≥ 0` per year, budget group and day, the share of the budget the
day may use, with

    Σ_d a[y,k,d] ≤ 1.

Subproblem d then holds its emissions, scaled to the year like its cost, to
`a[y,k,d] × budget_k + v` and pays the group's violation penalty on `v`.
Summed over the days this is the monolithic constraint
`Σ_d emissions ≤ budget + viol` with `viol = Σ_d v` at the same price, so
both have the same optimum; the cuts carry ∂cost/∂a to the master, which
moves allowance to the days where it saves the most.
"""
function add_benders_co2_allocation!(model::Model, input::MasterProblemInput,
                                     rep_days_by_year::Dict{Int, Vector{Int}})
    alloc = Dict{Tuple{Int,Int,Int}, VariableRef}()
    for (k, grp) in enumerate(input.co2_budget_groups)
        has_co2_budget(grp) || continue
        for (y_idx, days) in rep_days_by_year
            isempty(days) && continue
            for d in eachindex(days)
                alloc[(y_idx, k, d)] = @variable(model, lower_bound = 0,
                    base_name = "co2_alloc_y$(y_idx)_$(grp.name)_d$(d)")
            end
            @constraint(model, sum(alloc[(y_idx, k, d)] for d in eachindex(days)) <= 1.0,
                base_name = "co2_alloc_budget_y$(y_idx)_$(grp.name)")
        end
    end
    return alloc
end

"""
    benders_copy_expr(vars, input, year_idx, key, ctx) -> AffExpr

The master expression copy key `key` stands for in year `year_idx`.
"""
function benders_copy_expr(vars::MasterProblemVariables, input::MasterProblemInput,
                           year_idx::Int, key::BendersCopyKey, ctx::BendersCopyContext)
    kind, name, i = key
    expr = AffExpr(0.0)
    ypp = vars.years_per_inv_period
    if kind === :converter
        for y in 1:ypp:year_idx
            v = get(get(vars.converter_investment, y, Dict{Tuple{String,Int}, VariableRef}()),
                    (name, i), nothing)
            v === nothing || add_to_expression!(expr, v)
        end
    elseif kind === :line
        from, to = ctx.line_ends[i]
        for y in 1:ypp:year_idx
            haskey(vars.transfer_investment, y) || continue
            v = corridor_investment(vars.transfer_investment[y], from, to)
            v === nothing || add_to_expression!(expr, v)
        end
    elseif kind === :reservoir
        add_to_expression!(expr, reservoir_capacity_built(vars, parse(Int, name), i, year_idx))
    elseif kind === :gen_life
        add_to_expression!(expr, vars.gen_life_extension[year_idx][parse(Int, name)][i])
    elseif kind === :bat_life
        add_to_expression!(expr, vars.bat_life_extension[year_idx][parse(Int, name)][i])
    elseif kind === :re_ratio
        add_to_expression!(expr, vars.re_penetration_ratio[(parse(Int, name), year_idx)])
    elseif kind === :co2
        add_to_expression!(expr, ctx.co2_alloc[(year_idx, parse(Int, name), i)])
    else
        add_to_expression!(expr, master_fuel_extra_expr(vars.fuel, input, year_idx, key))
    end
    return expr
end

"""
    benders_reservoir_builds(vars, input, year_idx) -> Vector{Tuple{Int,Int}}

`(generator, bus)` of the reservoirs the master can have expanded by year
`year_idx` (an existing reservoir with `reservoir_invest_max` > 0 and a
build variable up to that year), as `add_day_operational_constraints!`
selects them.
"""
function benders_reservoir_builds(vars::MasterProblemVariables, input::MasterProblemInput,
                                  year_idx::Int)
    out = Tuple{Int,Int}[]
    for (g, gen) in enumerate(input.generators), b in 1:input.network.num_buses
        gen.reservoir_capacity[b] > 0 && gen.reservoir_invest_max[b] > 0 || continue
        any(haskey(get(vars.reservoir_investment, y, Dict{Int, Vector{VariableRef}}()), g)
            for y in 1:vars.years_per_inv_period:year_idx) || continue
        push!(out, (g, b))
    end
    return out
end

"""
    copy_value(fc, input, key) -> Float64

The value a subproblem fixes copy `key` to: `fc.fuel_extra[key]` when the
master set it, otherwise the capacity in `fc` it stands for (life
extension, RE ratio), an even share of the CO2 budget, or nothing built.
"""
function copy_value(fc::FixedCapacities, input::MasterProblemInput, key::BendersCopyKey)
    haskey(fc.fuel_extra, key) && return fc.fuel_extra[key]
    kind, name, i = key
    kind === :gen_life && return get(fc.gen_capacity, (parse(Int, name), i), 0.0)
    kind === :bat_life && return get(fc.bat_power, (parse(Int, name), i), 0.0)
    if kind === :re_ratio
        s = parse(Int, name)
        return length(fc.re_ratios) >= s ? fc.re_ratios[s] : fc.re_ratios[1]
    end
    kind === :co2 && return 1.0 / input.representative_days_per_year
    return 0.0
end

"""
    add_copy!(sp, fix, fc, input, key) -> VariableRef

A copy variable for `key` fixed to `copy_value`; the fixing constraint goes
into `fix` for the cut.
"""
function add_copy!(sp::Model, fix::AbstractDict, fc::FixedCapacities,
                   input::MasterProblemInput, key::BendersCopyKey)
    c = @variable(sp, base_name = "copy_$(key[1])_$(key[2])_$(key[3])")
    fix[key] = @constraint(sp, c == copy_value(fc, input, key))
    return c
end

"""
    extract_copy_duals(sp) -> Dict{BendersCopyKey, Float64}

∂(day cost)/∂(copied quantity) for each copy variable of a solved subproblem.
"""
function extract_copy_duals(sp)
    out = Dict{BendersCopyKey, Float64}()
    for (k, cref) in get(sp.constraint_store, "copy_fix", Dict{BendersCopyKey, Any}())
        d = dual(cref)
        abs(d) > 1e-10 && (out[k] = d)
    end
    return out
end

# =============================================================================
# Benders Master Problem (investment only)
# =============================================================================

"""
    benders_rep_days(input) -> Dict{Int, Vector{Int}}

First step of each representative day of each year, selected as the
monolithic master selects them (`select_representative_days`).
"""
function benders_rep_days(input::MasterProblemInput)
    timesteps_per_day = 24 ÷ input.temporal_resolution_hours
    rep_days_by_year = Dict{Int, Vector{Int}}()
    for y_idx in eachindex(input.years)
        timesteps_per_year = input.hours_per_year[y_idx] ÷ input.temporal_resolution_hours
        rep_days_by_year[y_idx] = select_representative_days(
            input.base_demand, y_idx, input.representative_days_per_year,
            input.min_day_separation, timesteps_per_day, timesteps_per_year)
    end
    return rep_days_by_year
end

"""
    create_benders_master(input; rep_days_by_year=benders_rep_days(input))

Investment-only master: the investment constraints and objective of the
monolithic master (`create_master_problem` without its representative days),
plus one recourse variable θ[(y, d)] per year and representative day for
that day's operating cost (multi-cut). Returns
`(model, vars, targets, theta, inv_cost_expr)`.

It has no constraint of its own linking investment to the RE ratio or to
storage: the subproblems' `re_ratio_lb` prices the ratio through its copy,
and the cuts carry the value of every capacity.
"""
function create_benders_master(
    input::MasterProblemInput;
    rep_days_by_year::Dict{Int, Vector{Int}} = benders_rep_days(input)
)
    model = Model(create_optimizer(
        solver_name=input.solver_name,
        threads=input.threads,
        time_limit=min(input.time_limit, 600.0),  # master should be fast
        gap=input.gap,
        verbose=input.verbose
    ))

    # Presolve helps with initial model reduction
    if lowercase(input.solver_name) == "highs"
        set_attribute(model, "presolve", "on")
    end

    targets = calculate_target_ratios(input)
    vars = build_master_variables!(model, input)

    # The investment constraints of the monolithic master
    add_investment_constraints!(model, vars, input)
    add_budget_constraints!(model, vars, input)
    add_retirement_cascade_constraints!(model, vars, input)
    add_capacity_adequacy_constraints!(model, vars, input)
    add_transmission_symmetry_constraints!(model, vars, input)
    add_re_target_constraints!(model, vars, input, targets)
    add_re_increment_constraints!(model, vars, input)

    # NOTE: NO add_representative_days_validation! — this is the decomposed part

    # θ[(y, d)] is in units of actual_cost / COST_SCALE; cuts and objective
    # use COST_SCALE accordingly.
    num_years = length(input.years)
    theta = Dict{Tuple{Int,Int}, VariableRef}()
    for y_idx in 1:num_years, d in eachindex(get(rep_days_by_year, y_idx, Int[]))
        theta[(y_idx, d)] = @variable(model, lower_bound = 0,
            base_name = "theta_y$(y_idx)_d$(d)")
    end

    # Objective as build_master_objective!, with θ for the operating cost
    inv_cost_expr = build_investment_cost_expression(vars, input)
    total_cost = copy(inv_cost_expr)
    for y_idx in 1:num_years
        discount_factor = 1.0 / ((1.0 + input.discount_rate)^(y_idx - 1))
        add_to_expression!(total_cost, build_annual_asset_cost(vars, input, y_idx),
                           discount_factor)
        for d in eachindex(get(rep_days_by_year, y_idx, Int[]))
            add_to_expression!(total_cost, theta[(y_idx, d)], discount_factor * COST_SCALE)
        end

        add_to_expression!(total_cost, vars.slack_re_target[y_idx], input.slack_penalty)
        add_to_expression!(total_cost, vars.slack_budget[y_idx], input.slack_penalty)
        for ni in 1:input.network.num_nodes
            add_to_expression!(total_cost, vars.slack_capacity[(y_idx, ni)], input.slack_penalty)
        end
    end

    @objective(model, Min, total_cost)

    return model, vars, targets, theta, inv_cost_expr
end

# =============================================================================
# Fixed Capacity Evaluation
# =============================================================================

"""
    existing_capacities(input, year_idx, life_ext) -> (gen, bat_power, bat_energy)

Capacity of the existing generators and batteries in year `year_idx`, as
`build_cumulative_capacity_expressions` has it: the rated power (degraded,
for generators) while a unit is within its lifetime, and past it
`life_ext(kind, i, b, full)`, the life-extended MW of generator
(`kind = :gen`) or battery (`:bat`) `i` at bus `b`, whose most is `full`.
Battery energy scales with the life-extended power.
"""
function existing_capacities(input::MasterProblemInput, year_idx::Int, life_ext::Function)
    n_buses = input.network.num_buses
    gen_cap = Dict{Tuple{Int,Int}, Float64}()
    for (g, gen) in enumerate(input.generators), b in 1:n_buses
        rated_power = gen.rated_power[b]
        rated_power > 0 || continue
        age_at_year = gen.initial_age[b] + (year_idx - 1)
        full = rated_power * (1.0 - gen.degradation_rate[b]) ^ age_at_year
        cap = age_at_year < gen.life_time[b] ? full : life_ext(:gen, g, b, full)
        cap > 1e-8 && (gen_cap[(g, b)] = cap)
    end

    bat_pow = Dict{Tuple{Int,Int}, Float64}()
    bat_en = Dict{Tuple{Int,Int}, Float64}()
    for (bi, bat) in enumerate(input.batteries), b in 1:n_buses
        base_power = max(bat.max_discharge_power[b], 0.0)
        base_cap = max(bat.capacity[b], 0.0)
        if bat.initial_age[b] + (year_idx - 1) < bat.life_time[b]
            pow, en = base_power, base_cap
        elseif base_power > 0
            pow = life_ext(:bat, bi, b, base_power)
            en = pow * base_cap / base_power
        else
            pow, en = 0.0, 0.0
        end
        pow > 1e-8 && (bat_pow[(bi, b)] = pow)
        en > 1e-8 && (bat_en[(bi, b)] = en)
    end
    return gen_cap, bat_pow, bat_en
end

"""
    evaluate_fixed_capacities(vars, input, year_idx; ctx=BendersCopyContext())

Compute fixed capacity values from master solution for a given year.
Same logic as build_cumulative_capacity_expressions but with value() on variables.
The copy values (fuel, converter and line builds, CO2 allocations) are the
solved `benders_copy_expr` of each key.
"""
function evaluate_fixed_capacities(
    vars::MasterProblemVariables,
    input::MasterProblemInput,
    year_idx::Int;
    ctx::BendersCopyContext = BendersCopyContext()
)::FixedCapacities
    n_tech = length(input.technologies)
    n_bat_tech = length(input.battery_technologies)
    n_buses = input.network.num_buses
    ypp = vars.years_per_inv_period

    # Existing generators and batteries (with life extension)
    gen_cap, bat_pow, bat_en = existing_capacities(input, year_idx,
        (kind, i, b, _) -> begin
            v = (kind === :gen ? vars.gen_life_extension :
                                 vars.bat_life_extension)[year_idx][i][b]
            v === nothing ? 0.0 : value(v)
        end)

    # Technology cumulative investment capacity
    tech_cap = Dict{Tuple{Int,Int}, Float64}()
    for t in 1:n_tech
        tech = input.technologies[t]
        for b in 1:n_buses
            cumul = 0.0
            tech_lifetime = tech.life_time[b]
            tech_deg = tech.degradation_rate[b]
            for y in 1:ypp:year_idx
                inv_age = year_idx - y
                if inv_age < tech_lifetime
                    deg = (1.0 - tech_deg) ^ inv_age
                    cumul += value(vars.tech_investment[y][t][b]) * deg
                end
            end
            if cumul > 1e-8
                tech_cap[(t, b)] = cumul
            end
        end
    end

    # Battery technology cumulative investment
    bt_pow = Dict{Tuple{Int,Int}, Float64}()
    bt_en = Dict{Tuple{Int,Int}, Float64}()
    for bt in 1:n_bat_tech
        btech = input.battery_technologies[bt]
        for b in 1:n_buses
            pow = 0.0
            en = 0.0
            bt_lifetime = btech.life_time[b]
            for y in 1:ypp:year_idx
                inv_age = year_idx - y
                if inv_age < bt_lifetime
                    pow += value(vars.bat_tech_power_investment[y][bt][b])
                    en += value(vars.bat_tech_capacity_investment[y][bt][b])
                end
            end
            if pow > 1e-8
                bt_pow[(bt, b)] = pow
            end
            if en > 1e-8
                bt_en[(bt, b)] = en
            end
        end
    end

    # Line capacity (base + cumulative investment)
    line_cap = Dict{Int, Float64}()
    # Line capacity is computed per subproblem since it depends on the network topology
    # (handled in create_benders_subproblem)

    # RE ratios from master — per system for per-system subproblem constraints
    n_sys = max(1, length(input.system_node_ranges))
    re_ratios = [value(vars.re_penetration_ratio[(s, year_idx)]) for s in 1:n_sys]

    copies = master_fuel_extra_values(vars.fuel, input, year_idx)  # M2 (#89)
    for (key, _) in master_converters(input.network)
        k = (:converter, key[1], key[2])
        copies[k] = value(benders_copy_expr(vars, input, year_idx, k, ctx))
    end
    for l in eachindex(ctx.line_ends)
        k = (:line, "", l)
        copies[k] = value(benders_copy_expr(vars, input, year_idx, k, ctx))
    end
    for (g, b) in benders_reservoir_builds(vars, input, year_idx)
        k = (:reservoir, string(g), b)
        copies[k] = value(benders_copy_expr(vars, input, year_idx, k, ctx))
    end
    for ((y, grp, d), v) in ctx.co2_alloc
        y == year_idx && (copies[(:co2, string(grp), d)] = value(v))
    end

    return FixedCapacities(
        gen_cap, bat_pow, bat_en,
        tech_cap, bt_pow, bt_en,
        line_cap, re_ratios, copies
    )
end

# =============================================================================
# Benders Subproblem
# =============================================================================

"""
    create_benders_subproblem(input, year_idx, day_idx, demand, start_hour, fixed_caps, vars)

Create and solve an operational dispatch subproblem for a single representative day
with FIXED capacities from the master solution.

Returns SubproblemData with solved model and constraint references for dual extraction.
"""
function create_benders_subproblem(
    input::MasterProblemInput,
    year_idx::Int,
    day_idx::Int,
    demand::Matrix{Float64},
    start_hour::Int,
    fixed_caps::FixedCapacities,
    master_vars::MasterProblemVariables
)::SubproblemData
    hours = size(demand, 1)
    n_gen = length(input.generators)
    n_bat = length(input.batteries)
    n_tech = length(input.technologies)
    n_bat_tech = length(input.battery_technologies)
    n_buses = input.network.num_buses
    b2n = input.network.bus_to_node
    ypp = master_vars.years_per_inv_period

    constraint_store = Dict{String, Any}()

    # Create separate JuMP model for this subproblem
    # Use single thread and default scaling to avoid HiGHS OTHER_ERROR.
    # Multi-threaded HiGHS can have intermittent failures on certain LP structures.
    sp_model = Model(() -> begin
        opt = HiGHS.Optimizer()
        MOI.set(opt, MOI.RawOptimizerAttribute("threads"), 1)
        MOI.set(opt, MOI.RawOptimizerAttribute("time_limit"), min(input.time_limit, 300.0))
        MOI.set(opt, MOI.RawOptimizerAttribute("output_flag"), false)
        MOI.set(opt, MOI.RawOptimizerAttribute("presolve"), "on")
        return opt
    end)

    # Create PowerSystemVariables (reuse existing function)
    ps_vars = create_day_ps_vars!(sp_model, input, year_idx, day_idx, hours)

    # Apply demand growth
    growth_factor = (1.0 + input.demand_growth)^(year_idx - 1)

    # Copy variables of master quantities, with their fixing constraints
    copy_fix = Dict{BendersCopyKey, Any}()
    copy_of(key) = add_copy!(sp_model, copy_fix, fixed_caps, input, key)

    # =========================================================================
    # Capacity overrides using FIXED values; a life-extended unit's capacity
    # is a copy of the master's life extension
    # =========================================================================
    total_cap_gen = Dict{Tuple{Int,Int}, Any}()
    for g in 1:n_gen
        gen = input.generators[g]
        for b in ps_vars.buses_of_gen[g]
            expired = gen.initial_age[b] + (year_idx - 1) >= gen.life_time[b]
            if expired && gen.rated_power[b] > 0 &&
               master_vars.gen_life_extension[year_idx][g][b] !== nothing
                total_cap_gen[(g, b)] = AffExpr(0.0, copy_of((:gen_life, string(g), b)) => 1.0)
            else
                total_cap_gen[(g, b)] = get(fixed_caps.gen_capacity, (g, b), 0.0)
            end
        end
    end

    total_cap_bat_power = Dict{Tuple{Int,Int}, Any}()
    total_cap_bat_energy = Dict{Tuple{Int,Int}, Any}()
    for bi in 1:n_bat
        bat = input.batteries[bi]
        for b in ps_vars.buses_of_bat[bi]
            expired = bat.initial_age[b] + (year_idx - 1) >= bat.life_time[b]
            base_power = bat.max_discharge_power[b]
            if expired && base_power > 0 &&
               master_vars.bat_life_extension[year_idx][bi][b] !== nothing
                c = copy_of((:bat_life, string(bi), b))
                total_cap_bat_power[(bi, b)] = AffExpr(0.0, c => 1.0)
                ratio = bat.capacity[b] > 0 ? bat.capacity[b] / base_power : 0.0
                total_cap_bat_energy[(bi, b)] = AffExpr(0.0, c => ratio)
            else
                total_cap_bat_power[(bi, b)] = get(fixed_caps.bat_power, (bi, b), 0.0)
                total_cap_bat_energy[(bi, b)] = get(fixed_caps.bat_energy, (bi, b), 0.0)
            end
        end
    end

    # =========================================================================
    # Technology dispatch variables with FIXED capacity constraints
    # =========================================================================
    tech_output = nothing
    tech_curve_cost = AffExpr(0.0)
    if n_tech > 0
        tech_output = @variable(sp_model,
            [t=1:n_tech, b=1:n_buses, ts=1:hours],
            lower_bound = 0,
            base_name = "tech_out_y$(year_idx)_d$(day_idx)")

        for t in 1:n_tech
            tech = input.technologies[t]
            is_renewable = tech.type == "Renewable"
            for b in 1:n_buses
                cumul_cap = get(fixed_caps.tech_capacity, (t, b), 0.0)
                avails = Vector{Float64}(undef, hours)
                for ts in 1:hours
                    actual_hour = start_hour + ts - 1
                    avail_hours = size(tech.availability, 1)
                    avail_hour = avail_hours > 0 ? mod1(actual_hour, avail_hours) : 1
                    avail = is_renewable ? tech.availability[avail_hour, b] : 1.0
                    avails[ts] = avail
                    rhs = cumul_cap * avail
                    cname = "tech_cap_t$(t)_b$(b)_ts$(ts)"
                    cref = @constraint(sp_model,
                        tech_output[t, b, ts] <= rhs,
                        base_name = cname)
                    constraint_store[cname] = (cref, avail, t, b, ts)
                end
                # Offer-curve fuel cost, as in the monolithic master
                add_tech_curve_segments!(sp_model, input, tech_output, t, b,
                    cumul_cap, avails, tech_curve_cost; store = constraint_store)
            end
        end
    end

    # Battery technology dispatch with FIXED capacity, as in the master; the
    # constraints holding that capacity go to the store for the cut
    bat_tech_discharge = nothing
    bat_tech_charge = nothing
    bat_tech_soc = nothing
    if n_bat_tech > 0
        bat_tech_discharge, bat_tech_charge, bat_tech_soc = add_battery_tech_day!(
            sp_model, input,
            (bt, b) -> get(fixed_caps.bat_tech_power, (bt, b), 0.0),
            (bt, b) -> get(fixed_caps.bat_tech_energy, (bt, b), 0.0),
            start_hour, hours;
            var_suffix = "_y$(year_idx)_d$(day_idx)", store = constraint_store)
    end

    # =========================================================================
    # Build day input NamedTuple (same as add_day_operational_constraints!)
    # =========================================================================
    n_nodes = input.network.num_nodes
    demand_slice = Matrix{Float64}(undef, hours, n_nodes)
    for t in 1:hours, n in 1:n_nodes
        demand_slice[t, n] = demand[t, n] * growth_factor
    end

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
        enable_angle_limits = false,
        re_penetration_target = 0.0,
        soc_end_tolerance = 0.0,
        min_cycling_ratio = 0.0,
        min_cycling_period_days = 1.0,
        pwl_loss_segments = input.transmission_loss_segments,
        gen_cost_curves = input.gen_cost_curves,
        bat_cost_curves = input.bat_cost_curves,
        # Rolling-horizon seam slots: not applicable inside a representative-day
        # subproblem (each day is solved fresh). Empty Dicts make the shared
        # power_system.jl constraint builders take their no-boundary paths
        # instead of erroring on a missing field.
        generator_output_prev = Dict{Int, Dict{Int, Float64}}(),
        reservoir_level_prev = Dict{Int, Dict{Int, Float64}}(),
    )

    # =========================================================================
    # Delegate to power_system.jl constraints (with Float64 capacity_override)
    # =========================================================================
    add_generator_constraints!(sp_model, ps_vars, day_input;
        capacity_override=total_cap_gen)
    add_battery_constraints!(sp_model, ps_vars, day_input;
        capacity_override_power=total_cap_bat_power,
        capacity_override_energy=total_cap_bat_energy)
    # Reservoir water balance — keep the Benders subproblem consistent with the
    # monolithic master (no-op when the day has no reservoir variables). The
    # level bounds include a copy of the reservoir capacity built so far.
    res_cap = Dict{Tuple{Int,Int}, Any}(
        (g, b) => input.generators[g].reservoir_capacity[b] +
                  copy_of((:reservoir, string(g), b))
        for (g, b) in benders_reservoir_builds(master_vars, input, year_idx))
    add_reservoir_constraints!(sp_model, ps_vars, day_input; capacity_override=res_cap)
    add_reserve_constraints!(sp_model, ps_vars, day_input;
        capacity_override=total_cap_gen,
        battery_capacity_override=total_cap_bat_power,
        demand_scale=1.0)
    add_inertia_constraints!(sp_model, ps_vars, day_input)
    # Converters at their rated power plus a copy of the MW the master built
    conv_cap = Dict{Tuple{String,Int}, Any}()
    for (key, conv) in master_converters(input.network)
        buildable = any(haskey(get(master_vars.converter_investment, y,
                                   Dict{Tuple{String,Int}, VariableRef}()), key)
                        for y in 1:ypp:year_idx)
        conv_cap[key] = buildable ?
            conv.rated_power_mva + copy_of((:converter, key[1], key[2])) : conv.rated_power_mva
    end
    add_converter_constraints!(sp_model, ps_vars, day_input; capacity_override=conv_cap)

    # =========================================================================
    # Extra injections for power balance (tech + bat_tech output)
    # =========================================================================
    extra_fn = (bus, t) -> begin
        expr = AffExpr(0.0)
        if tech_output !== nothing
            for tc in 1:n_tech
                add_to_expression!(expr, tech_output[tc, bus, t])
            end
        end
        if bat_tech_discharge !== nothing
            for btc in 1:n_bat_tech
                add_to_expression!(expr, bat_tech_discharge[btc, bus, t])
                add_to_expression!(expr, -1.0, bat_tech_charge[btc, bus, t])
            end
        end
        return expr
    end

    # =========================================================================
    # Transmission / power balance
    # =========================================================================
    network_lines = input.network.transmission_lines
    n_lines = length(network_lines)
    line_flow_cost = AffExpr(0.0)
    if n_buses > 1 && n_lines > 0
        transmission = with_loss_model(TransmissionDC(input.network),
            input.transmission_loss_segments, input.network.s_base)

        # Line capacity: base plus a copy of the MW the master built on the
        # line's corridor. As in the master, a line is expandable when its
        # corridor has an investment variable up to this year.
        inv_years = [y for y in 1:ypp:year_idx if haskey(master_vars.transfer_investment, y)]
        line_cap_override = Dict{Int, Any}()
        expandable = Set{Int}()
        for (l, (from, to)) in enumerate(transmission.lines)
            base_cap = transmission.line_capacities[l]
            if any(corridor_investment(master_vars.transfer_investment[y], from, to) !== nothing
                   for y in inv_years)
                push!(expandable, l)
                line_cap_override[l] = base_cap + copy_of((:line, "", l))
            else
                line_cap_override[l] = base_cap
            end
        end
        add_dc_constraints!(sp_model, transmission, ps_vars, day_input;
            extra_injections_fn=extra_fn, expandable_lines=expandable)
        add_line_capacity_constraints!(sp_model, transmission, ps_vars, day_input;
            capacity_override=line_cap_override)
        line_flow_cost = add_line_flow_cost!(sp_model, transmission, ps_vars, input, hours)
    else
        add_demand_constraints!(sp_model, ps_vars, day_input;
            extra_injections_fn=extra_fn)
    end

    # Curtailment
    add_curtailment_constraints!(sp_model, ps_vars, day_input;
        capacity_override=total_cap_gen)

    # =========================================================================
    # Sectoral split of the load shed (same as the monolithic master)
    # =========================================================================
    remainder_premium = add_day_sectoral_shed_constraints!(sp_model, ps_vars, input,
        demand_slice, start_hour, growth_factor)

    # =========================================================================
    # RE Penetration with FIXED ratio — PER SYSTEM
    # =========================================================================
    # Build per-system renewable generation and demand expressions.
    # When system_node_ranges is defined, each system gets its own RE constraint
    # so the optimizer can't concentrate all RE in one subsystem.
    n_sys = max(1, length(input.system_node_ranges))
    sys_ranges = input.system_node_ranges

    # Global totals (still needed for curtailment constraint)
    total_renewable = AffExpr(0.0)
    total_generation = AffExpr(0.0)

    # Per-system renewable expressions
    sys_renewable = [AffExpr(0.0) for _ in 1:n_sys]
    sys_fre_loss = [AffExpr(0.0) for _ in 1:n_sys]
    sys_demand_day = zeros(n_sys)

    # Helper: map bus → system index
    bus_to_sys = ones(Int, n_buses)  # default to system 1
    if length(sys_ranges) > 0
        for s_idx in 1:length(sys_ranges)
            sr = sys_ranges[s_idx]
            for b in sr.first_bus:(sr.first_bus + sr.num_buses - 1)
                if b <= n_buses
                    bus_to_sys[b] = s_idx
                end
            end
        end
    end

    for g in 1:n_gen
        gen = input.generators[g]
        is_renewable = gen.type == "Renewable"
        for n in ps_vars.buses_of_gen[g]
            s = bus_to_sys[n]
            for t in 1:hours
                add_to_expression!(total_generation, ps_vars.gen_output[g, n, t])
                if is_renewable
                    add_to_expression!(total_renewable, ps_vars.gen_output[g, n, t])
                    add_to_expression!(sys_renewable[s], ps_vars.gen_output[g, n, t])
                end
            end
        end
    end
    if tech_output !== nothing
        for tc in 1:n_tech
            tech = input.technologies[tc]
            is_renewable = tech.type == "Renewable"
            for b in 1:n_buses
                s = bus_to_sys[b]
                for t in 1:hours
                    add_to_expression!(total_generation, tech_output[tc, b, t])
                    if is_renewable
                        add_to_expression!(total_renewable, tech_output[tc, b, t])
                        add_to_expression!(sys_renewable[s], tech_output[tc, b, t])
                    end
                end
            end
        end
    end

    # FRE loss per system (node-level variables)
    n_nodes = input.network.num_nodes
    # Map nodes to systems via the bus_to_sys mapping (use first bus of each node)
    node_to_sys = zeros(Int, n_nodes)
    for b in 1:n_buses
        ni = input.network.bus_to_node[b]
        if node_to_sys[ni] == 0
            node_to_sys[ni] = bus_to_sys[b]
        end
    end
    for ni in 1:n_nodes
        s = node_to_sys[ni]
        for t in 1:hours
            add_to_expression!(sys_fre_loss[s], ps_vars.fre_penetration_loss[ni, t])
        end
    end

    # Per-system demand on this representative day
    # demand is already sliced to [hours × nodes] for this representative day
    total_demand_day = sum(demand) * growth_factor
    for n in 1:n_buses
        s = bus_to_sys[n]
        parent_node = input.network.bus_to_node[n]
        bus_fraction = input.network.buses[n].demand_fraction
        for t in 1:hours
            sys_demand_day[s] += demand[t, parent_node] * bus_fraction * growth_factor
        end
    end

    # Per-system RE floors as in the monolithic master: at the year's target
    # (re_min_day) and at the master's RE ratio (re_ratio_lb, a copy, so the
    # cut prices the ratio). There is no upper bound: renewable output above
    # the target on a good day is valid.
    targets = calculate_target_ratios(input)
    for s_idx in 1:n_sys
        @constraint(sp_model,
            sys_renewable[s_idx] + sys_fre_loss[s_idx] >=
                targets[(s_idx, year_idx)] * sys_demand_day[s_idx],
            base_name = "re_min_day_s$(s_idx)_y$(year_idx)_d$(day_idx)")
        if sys_demand_day[s_idx] > 0
            @constraint(sp_model,
                copy_of((:re_ratio, string(s_idx), 0)) * sys_demand_day[s_idx] <=
                    sys_renewable[s_idx] + sys_fre_loss[s_idx],
                base_name = "re_ratio_lb_s$(s_idx)_y$(year_idx)_d$(day_idx)")
        end
    end

    # Max curtailment — includes BOTH generator and technology RE curtailment.
    # Technology curtailment = fixed_cap × avail - tech_output (capacity not dispatched).
    # Including it here is critical: when something other than the capacity
    # constraint limits tech dispatch, the capacity dual is 0 and the master
    # gets no signal about overcapacity.  By including technology curtailment,
    # the dual of this constraint captures the cost of over-investment and
    # feeds it into the Benders cuts via extract_subproblem_duals.
    # An AffExpr even for one node and one step (a bare sum would be a VariableRef)
    total_curtailment = AffExpr(0.0)
    for n in 1:n_nodes, t in 1:hours
        add_to_expression!(total_curtailment, ps_vars.curtailment[n, t])
    end

    # Add technology RE curtailment: available capacity - dispatched output
    # tech_avail_sum tracks Σ(fixed_cap × avail) per (tech, bus) for dual extraction
    tech_avail_sum = Dict{Tuple{Int,Int}, Float64}()
    # The monolithic master also charges curtailment_cost on it (with a
    # curtailment limit). Its cap × avail part is a constant here, so its
    # gradient, curtailment_cost × Σ avail per MW (scaled to the year, see
    # extract_subproblem_duals), goes into the cut separately.
    charge_tech_curt = input.max_curtailment_ratio < 1.0 && input.curtailment_cost > 0
    tech_curt_grad = Dict{Tuple{Int,Int}, Float64}()
    if tech_output !== nothing
        for tc in 1:n_tech
            tech = input.technologies[tc]
            if tech.type != "Renewable"
                continue
            end
            for b in 1:n_buses
                cumul_cap = get(fixed_caps.tech_capacity, (tc, b), 0.0)
                avail_total = 0.0
                for ts in 1:hours
                    actual_hour = start_hour + ts - 1
                    avail_hours = size(tech.availability, 1)
                    avail_hour = avail_hours > 0 ? mod1(actual_hour, avail_hours) : 1
                    avail = tech.availability[avail_hour, b]
                    avail_total += avail
                    # Also at zero capacity: the output's −curtailment_cost is
                    # part of the capacity constraint's dual
                    if charge_tech_curt
                        add_to_expression!(tech_curve_cost, cumul_cap * avail * input.curtailment_cost)
                        add_to_expression!(tech_curve_cost, -input.curtailment_cost,
                                           tech_output[tc, b, ts])
                    end
                    # tech curtailment = cap × avail - tech_output; also at
                    # zero capacity, so the cut sees what building adds
                    add_to_expression!(total_curtailment, cumul_cap * avail)
                    add_to_expression!(total_curtailment, -1.0, tech_output[tc, b, ts])
                end
                tech_avail_sum[(tc, b)] = avail_total
                if charge_tech_curt && avail_total > 0
                    tech_curt_grad[(tc, b)] = input.curtailment_cost * avail_total
                end
            end
        end
    end

    # Hard, as in the monolithic master (and only with renewable output to
    # cap). Its slack is fixed to zero; `solve_benders_day!` frees it to
    # measure the violation when the master's capacities cannot meet the
    # limit, for a feasibility cut.
    if input.max_curtailment_ratio < 1.0 && !isempty(total_renewable.terms)
        curt_excess = @variable(sp_model, lower_bound = 0, upper_bound = 0,
            base_name = "curt_excess_y$(year_idx)_d$(day_idx)")
        curt_cref = @constraint(sp_model,
            total_curtailment <= input.max_curtailment_ratio * total_renewable + curt_excess,
            base_name = "max_curt_y$(year_idx)_d$(day_idx)")
        # Store for dual extraction — needed for Benders cut gradient
        constraint_store["max_curt"] = (curt_cref, tech_avail_sum)
        constraint_store["curt_excess"] = curt_excess
    end

    # =========================================================================
    # Objective: operational cost (includes annualization scaling factor)
    # =========================================================================
    tres = max(1.0, input.temporal_resolution_hours)
    days_in_year = length(input.hours_per_year) > 0 ? input.hours_per_year[1] / 24.0 : 365.0
    scaling_factor = tres * days_in_year / input.representative_days_per_year

    # Technologies' offer-curve fuel cost, battery technologies' maintenance
    # and throughput, converter variable cost, the flow cost of lines with a
    # per-MW·km cost and LP-relaxed start-ups, as in the monolithic master
    add_to_expression!(tech_curve_cost,
        battery_tech_operating_cost(input, bat_tech_charge, bat_tech_discharge, hours))
    add_to_expression!(tech_curve_cost, add_converter_objective_terms(ps_vars, day_input))
    add_to_expression!(tech_curve_cost, line_flow_cost)
    add_to_expression!(tech_curve_cost,
        add_day_commitment!(sp_model, ps_vars, input, total_cap_gen, year_idx, hours))
    isempty(tech_curt_grad) || (constraint_store["tech_curt_cost"] =
        Dict(k => g * scaling_factor for (k, g) in tech_curt_grad))
    day_cost = calculate_day_operational_cost(ps_vars, input, year_idx, hours;
        tech_output=tech_output, tech_day_cost=tech_curve_cost + remainder_premium)

    # CO2 budgets: the day's emissions, scaled to the year, within the share
    # of the budget the master gives this day (a copy) plus a violation slack
    # at the group's penalty (add_benders_co2_allocation!)
    for (k, e) in enumerate(day_co2_by_group(ps_vars, tech_output, input, hours))
        grp = input.co2_budget_groups[k]
        has_co2_budget(grp) || continue
        viol = @variable(sp_model, lower_bound = 0,
            base_name = "co2_budget_violation_y$(year_idx)_d$(day_idx)_$(grp.name)")
        @constraint(sp_model,
            rep_day_scaling(input) * e <= grp.budget * copy_of((:co2, string(k), day_idx)) + viol,
            base_name = "co2_budget_y$(year_idx)_d$(day_idx)_$(grp.name)")
        add_to_expression!(day_cost, grp.violation_penalty, viol)
    end

    # M2 (#89): fuel block with the master's fuel capacities fixed by copy
    # constraints, whose duals feed the cut like the other copies
    fuel_cost, fuel_fix = benders_fuel_day!(sp_model, ps_vars, input, year_idx,
        start_hour, hours, tech_output, fixed_caps.fuel_extra)
    add_to_expression!(day_cost, fuel_cost, master_fuel_day_scale(input))
    merge!(copy_fix, fuel_fix)
    isempty(copy_fix) || (constraint_store["copy_fix"] = copy_fix)

    @objective(sp_model, Min, day_cost)

    return SubproblemData(
        sp_model, ps_vars,
        tech_output, bat_tech_discharge, bat_tech_charge, bat_tech_soc,
        constraint_store,
        year_idx, day_idx, hours, start_hour, scaling_factor
    )
end

# =============================================================================
# Dual Extraction and Cut Generation
# =============================================================================

"""
    extract_subproblem_duals(sp, input; cost_terms=true)

Extract aggregated dual values from a solved subproblem for Benders cut generation.
Returns duals aggregated by (tech/bat_tech, bus) across timesteps.
`cost_terms=false` leaves out the capacity terms of the day's cost
(curtailment cost), for a subproblem solved for its least curtailment
violation rather than its cost.
"""
function extract_subproblem_duals(
    sp::SubproblemData,
    input::MasterProblemInput;
    cost_terms::Bool = true
)
    n_tech = length(input.technologies)
    n_bat_tech = length(input.battery_technologies)
    n_buses = input.network.num_buses

    # Technology capacity duals: aggregate across timesteps
    # Constraint: tech_output[t,b,ts] <= cap * avail(ts)
    # dual(cref) = ∂Q/∂RHS where RHS = cap * avail
    # We need ∂Q/∂cap = Σ_ts dual(cref_ts) × avail(ts)  (chain rule)
    tech_duals = Dict{Tuple{Int,Int}, Float64}()
    for t in 1:n_tech
        for b in 1:n_buses
            total_dual = 0.0
            for ts in 1:sp.hours
                cname = "tech_cap_t$(t)_b$(b)_ts$(ts)"
                if haskey(sp.constraint_store, cname)
                    cref, avail, _, _, _ = sp.constraint_store[cname]
                    d = dual(cref)
                    total_dual += d * avail
                end
                # Offer-curve segment caps: seg_k <= fraction_k × avail × cap
                segs = tech_curve_segments(input, t, b)
                segs === nothing && continue
                for k in eachindex(segs)
                    sname = "tech_seg_cap_t$(t)_b$(b)_k$(k)_ts$(ts)"
                    if haskey(sp.constraint_store, sname)
                        sref, coef, _, _, _ = sp.constraint_store[sname]
                        total_dual += dual(sref) * coef
                    end
                end
            end
            if abs(total_dual) > 1e-10
                tech_duals[(t, b)] = total_dual
            end
        end
    end

    # Battery tech power and energy duals: Σ dual × coefficient of the
    # capacity over the constraints holding it (charge and discharge limits
    # × availability, ramp limits; SOC limits), see add_battery_tech_day!
    function bt_duals(kind)
        out = Dict{Tuple{Int,Int}, Float64}()
        for (cref, coef, bt, b) in get(sp.constraint_store, kind, ())
            out[(bt, b)] = get(out, (bt, b), 0.0) + dual(cref) * coef
        end
        return filter(kv -> abs(kv[2]) > 1e-10, out)
    end
    bt_pow_duals = bt_duals("bt_pow")
    bt_cap_duals = bt_duals("bt_cap")

    # Curtailment constraint dual contribution to technology gradient.
    # The curtailment constraint includes technology curtailment:
    #   Σ(curt) + Σ(cap×avail - tech_output) ≤ ratio×renewable + slack
    # cap (master variable) appears on the LHS with coefficient Σ_ts(avail).
    # The gradient ∂Q/∂cap from this constraint = -dual(curt_cref) × Σ_ts(avail).
    # (negative because cap increases the LHS, tightening a ≤ constraint;
    #  JuMP dual for ≤ in minimization is ≤ 0, so -dual ≥ 0 = cost increase)
    # This term is CRITICAL: when something other than the capacity
    # constraint limits tech dispatch, the capacity dual is 0 but the
    # curtailment dual captures the marginal cost of over-investment.
    if haskey(sp.constraint_store, "max_curt")
        curt_data = sp.constraint_store["max_curt"]
        curt_cref = curt_data[1]
        tech_avail_sum = curt_data[2]
        curt_dual = dual(curt_cref)  # ≤ 0 for ≤ constraint in minimization
        if abs(curt_dual) > 1e-10
            for ((tc, b), avail_sum) in tech_avail_sum
                # -curt_dual × avail_sum is the positive gradient (cost increases with capacity)
                curt_contribution = -curt_dual * avail_sum
                tech_duals[(tc, b)] = get(tech_duals, (tc, b), 0.0) + curt_contribution
            end
        end
    end

    # curtailment_cost × (cap × avail − output): cap × avail is a constant of
    # the subproblem, so its cost per MW of capacity is added explicitly
    if cost_terms
        for ((tc, b), g) in get(sp.constraint_store, "tech_curt_cost",
                                Dict{Tuple{Int,Int}, Float64}())
            tech_duals[(tc, b)] = get(tech_duals, (tc, b), 0.0) + g
        end
    end

    return tech_duals, bt_pow_duals, bt_cap_duals
end

"""
    build_optimality_cut(sp_cost, tech_duals, bt_pow_duals, bt_cap_duals,
                         fixed_caps, input, year_idx, ypp)

Construct a Benders optimality cut from subproblem duals.

Cut form: θ[y] ≥ Q(x*) + Σ π_i × (x_i - x_i*)
       =  (Q(x*) - Σ π_i × x_i*) + Σ π_i × x_i
       =  intercept + gradient^T × x
"""
function build_optimality_cut(
    sp_cost::Float64,
    tech_duals::Dict{Tuple{Int,Int}, Float64},
    bt_pow_duals::Dict{Tuple{Int,Int}, Float64},
    bt_cap_duals::Dict{Tuple{Int,Int}, Float64},
    fixed_caps::FixedCapacities,
    input::MasterProblemInput,
    year_idx::Int,
    ypp::Int;
    fuel_duals::AbstractDict = Dict{Tuple{Symbol, String, Int}, Float64}()
)::BendersCut
    # The dual π[(t,b)] is w.r.t. cumulative capacity at (t,b) for this year
    # Chain rule: d(cumul_cap[y][t][b]) / d(inv[y'][t][b]) = deg_factor if age < lifetime
    # So the gradient w.r.t. investment variable inv[y'][t][b] is:
    #   grad = π[(t,b)] * (1 - deg)^(year_idx - y')  for each prior period y'

    # For the cut intercept, we need: Q(x*) - Σ π_i × x_i*
    # where x_i* is the cumulative capacity (the parameter that was fixed)
    intercept = sp_cost
    for ((t, b), pi) in tech_duals
        cap_star = get(fixed_caps.tech_capacity, (t, b), 0.0)
        intercept -= pi * cap_star
    end
    for ((bt, b), pi) in bt_pow_duals
        cap_star = get(fixed_caps.bat_tech_power, (bt, b), 0.0)
        intercept -= pi * cap_star
    end
    for ((bt, b), pi) in bt_cap_duals
        cap_star = get(fixed_caps.bat_tech_energy, (bt, b), 0.0)
        intercept -= pi * cap_star
    end
    # Copy variables (fuel, converter and line builds, life extension, RE
    # ratio, CO2 allocations), at the value the subproblems were fixed to
    for (k, pi) in fuel_duals
        intercept -= pi * copy_value(fixed_caps, input, k)
    end

    # Technology gradients: π[(t,b)] maps to investment variables via cumulative sum
    tech_grads = Dict{Tuple{Int,Int}, Float64}()
    for ((t, b), pi) in tech_duals
        tech = input.technologies[t]
        tech_lifetime = tech.life_time[b]
        tech_deg = tech.degradation_rate[b]
        for y in 1:ypp:year_idx
            inv_age = year_idx - y
            if inv_age < tech_lifetime
                deg = (1.0 - tech_deg) ^ inv_age
                key = (t, b)
                # The gradient is w.r.t. the investment variable at year y
                # We store it keyed by (t, b) for this specific y
                # But we need to distinguish by investment year for the cut
                grad_key = (t * 1000 + y, b)  # encode year into key
                tech_grads[grad_key] = get(tech_grads, grad_key, 0.0) + pi * deg
            end
        end
    end

    # Battery tech power gradients
    bt_pow_grads = Dict{Tuple{Int,Int}, Float64}()
    for ((bt, b), pi) in bt_pow_duals
        btech = input.battery_technologies[bt]
        bt_lifetime = btech.life_time[b]
        for y in 1:ypp:year_idx
            inv_age = year_idx - y
            if inv_age < bt_lifetime
                grad_key = (bt * 1000 + y, b)
                bt_pow_grads[grad_key] = get(bt_pow_grads, grad_key, 0.0) + pi
            end
        end
    end

    # Battery tech energy gradients
    bt_cap_grads = Dict{Tuple{Int,Int}, Float64}()
    for ((bt, b), pi) in bt_cap_duals
        btech = input.battery_technologies[bt]
        bt_lifetime = btech.life_time[b]
        for y in 1:ypp:year_idx
            inv_age = year_idx - y
            if inv_age < bt_lifetime
                grad_key = (bt * 1000 + y, b)
                bt_cap_grads[grad_key] = get(bt_cap_grads, grad_key, 0.0) + pi
            end
        end
    end

    # No normalization here — scaling is handled at the master level via COST_SCALE

    return BendersCut(
        intercept,
        tech_grads,
        bt_pow_grads,
        bt_cap_grads,
        Dict{Tuple{Int,Int}, Float64}(),  # gen_life_ext (not yet extracted)
        Dict{Tuple{Int,Int}, Float64}(),  # bat_life_ext (not yet extracted)
        Dict{Int, Float64}(),             # line gradients (not yet extracted)
        Dict{Tuple{Symbol, String, Int}, Float64}(fuel_duals)  # M2 (#89)
    )
end

"""
    benders_cut_expr(cut, year_idx, vars, input; ctx=BendersCopyContext()) -> AffExpr

`intercept + Σ gradient × master variable` of a cut from year `year_idx`.
"""
function benders_cut_expr(
    cut::BendersCut,
    year_idx::Int,
    vars::MasterProblemVariables,
    input::MasterProblemInput;
    ctx::BendersCopyContext = BendersCopyContext()
)
    # Divide all cut coefficients by COST_SCALE since theta is in scaled units
    inv_scale = 1.0 / COST_SCALE

    rhs = AffExpr(cut.intercept * inv_scale)

    # Technology investment gradients
    for ((encoded_key, b), grad) in cut.tech_gradients
        t = encoded_key ÷ 1000
        y = encoded_key % 1000
        if haskey(vars.tech_investment, y) && haskey(vars.tech_investment[y], t)
            add_to_expression!(rhs, vars.tech_investment[y][t][b], grad * inv_scale)
        end
    end

    # Battery tech power investment gradients
    for ((encoded_key, b), grad) in cut.bat_tech_pow_gradients
        bt = encoded_key ÷ 1000
        y = encoded_key % 1000
        if haskey(vars.bat_tech_power_investment, y) && haskey(vars.bat_tech_power_investment[y], bt)
            add_to_expression!(rhs, vars.bat_tech_power_investment[y][bt][b], grad * inv_scale)
        end
    end

    # Battery tech capacity investment gradients
    for ((encoded_key, b), grad) in cut.bat_tech_cap_gradients
        bt = encoded_key ÷ 1000
        y = encoded_key % 1000
        if haskey(vars.bat_tech_capacity_investment, y) && haskey(vars.bat_tech_capacity_investment[y], bt)
            add_to_expression!(rhs, vars.bat_tech_capacity_investment[y][bt][b], grad * inv_scale)
        end
    end

    # Copy variables: the master expression each one stands for this year
    for (k, grad) in cut.fuel_gradients
        add_to_expression!(rhs, benders_copy_expr(vars, input, year_idx, k, ctx),
                           grad * inv_scale)
    end
    return rhs
end

"""
    add_optimality_cut!(model, theta, cut, year_idx, vars, input, name;
                        ctx=BendersCopyContext())

Add a Benders optimality cut `θ ≥ intercept + Σ gradient × investment_variable`
for the recourse variable `theta` of one (year, representative day).
"""
function add_optimality_cut!(
    model::Model,
    theta::VariableRef,
    cut::BendersCut,
    year_idx::Int,
    vars::MasterProblemVariables,
    input::MasterProblemInput,
    name::String;
    ctx::BendersCopyContext = BendersCopyContext()
)
    return @constraint(model, theta >= benders_cut_expr(cut, year_idx, vars, input; ctx),
                       base_name = name)
end

"""
    add_feasibility_cut!(model, cut, year_idx, vars, input, name;
                         ctx=BendersCopyContext())

Add a Benders feasibility cut `intercept + Σ gradient × investment_variable ≤ 0`,
built from a subproblem's least curtailment violation (`solve_benders_day!`):
the capacities must let the day meet its curtailment limit.
"""
function add_feasibility_cut!(
    model::Model,
    cut::BendersCut,
    year_idx::Int,
    vars::MasterProblemVariables,
    input::MasterProblemInput,
    name::String;
    ctx::BendersCopyContext = BendersCopyContext()
)
    return @constraint(model, benders_cut_expr(cut, year_idx, vars, input; ctx) <= 0,
                       base_name = name)
end

"""
    solve_benders_day!(sp) -> Symbol

Solve subproblem `sp`, with its curtailment limit hard as in the monolithic
master. Returns `:optimal`; `:infeasible` when the fixed capacities cannot
meet the limit, `sp` then holding its least violation (the limit's slack
freed and minimised), whose duals give a feasibility cut; or `:failed`.
"""
function solve_benders_day!(sp::SubproblemData)
    solved(m) = termination_status(m) in (MOI.OPTIMAL, MOI.LOCALLY_SOLVED)
    optimize!(sp.model)
    solved(sp.model) && return :optimal

    slack = get(sp.constraint_store, "curt_excess", nothing)
    if slack !== nothing &&
       termination_status(sp.model) in (MOI.INFEASIBLE, MOI.INFEASIBLE_OR_UNBOUNDED)
        cost = objective_function(sp.model)
        delete_upper_bound(slack)
        @objective(sp.model, Min, slack)
        optimize!(sp.model)
        solved(sp.model) && objective_value(sp.model) > CURTAILMENT_FEAS_TOL &&
            return :infeasible
        set_upper_bound(slack, 0.0)
        @objective(sp.model, Min, cost)
    end

    # Retry with fresh optimizer: serial dual simplex, no scaling
    set_optimizer(sp.model, HiGHS.Optimizer)
    set_attribute(sp.model, "output_flag", false)
    set_attribute(sp.model, "threads", 1)
    set_attribute(sp.model, "simplex_strategy", 1)  # serial dual simplex
    set_attribute(sp.model, "simplex_scale_strategy", 0)  # auto scaling
    set_attribute(sp.model, "presolve", "on")
    optimize!(sp.model)
    return solved(sp.model) ? :optimal : :failed
end

# =============================================================================
# Main Benders Decomposition Loop
# =============================================================================

"""
    run_benders_decomposition(input; kwargs...)

Run Benders decomposition to solve the master problem.
Separates investment (master) from operational (subproblems) decisions.

Each representative day of each year has its own recourse variable and gets
its own cut every iteration (multi-cut). A day whose curtailment limit the
master's capacities cannot meet adds a feasibility cut instead, and that
iteration gives no upper bound. The returned plan and operating costs are
those of the iteration with the best upper bound.

# Arguments
- `input::MasterProblemInput`: Problem specification
- `max_iterations::Int=50`: Maximum iterations
- `tolerance::Float64=1e-4`: Relative gap tolerance for convergence
- `use_representative_days::Bool=true`: Use representative days (vs TSAM)
- `verbose_benders::Bool=true`: Log iteration progress
- `record_unit_operation::Bool=false`: keep each existing unit's operation in
  the best iteration's subproblems in `solution.unit_operation`, for
  `calculate_unit_npv`

# Returns
- `BendersResult`: Solution with convergence history
"""
function run_benders_decomposition(
    input::MasterProblemInput;
    max_iterations::Int = 50,
    tolerance::Float64 = 1e-4,
    use_representative_days::Bool = true,
    verbose_benders::Bool = true,
    record_unit_operation::Bool = false
)::BendersResult
    start_time = time()
    num_years = length(input.years)
    timesteps_per_day = 24 ÷ input.temporal_resolution_hours
    discount(y) = 1.0 / ((1.0 + input.discount_rate)^(y - 1))

    # =========================================================================
    # Step 0: Representative days and the Benders master
    # =========================================================================
    @info "Benders: Creating investment master problem..."
    rep_days_by_year = benders_rep_days(input)
    master_model, vars, targets, theta, inv_cost_expr =
        create_benders_master(input; rep_days_by_year)
    ypp = vars.years_per_inv_period

    # Master side of the copy variables: CO2 allocations per day, line ends
    ctx = BendersCopyContext(add_benders_co2_allocation!(master_model, input, rep_days_by_year),
                             benders_line_ends(input))

    # Solve every representative day of year `y_idx` at capacities `caps` and
    # queue its cut in `cuts` as (kind, year, day, cut). Returns the year's
    # operating cost, whether every day solved, and whether every day met
    # its curtailment limit. With `op`, each day's operation of the existing
    # units is added to it (their NPV).
    function evaluate_year!(cuts, y_idx, caps, iter; op = nothing)
        year_cost = 0.0
        solved = true
        feasible = true
        for (day_idx, start_hour) in enumerate(rep_days_by_year[y_idx])
            end_hour = min(start_hour + timesteps_per_day - 1, size(input.base_demand, 1))
            end_hour >= start_hour || continue
            sp = create_benders_subproblem(input, y_idx, day_idx,
                input.base_demand[start_hour:end_hour, :], start_hour, caps, vars)
            status = solve_benders_day!(sp)
            if status === :failed || !has_duals(sp.model)
                @warn "Benders: Subproblem (y=$(y_idx), d=$(day_idx)) failed after retry: " *
                      "$(termination_status(sp.model))"
                solved = false
                # Save the first failing LPs for diagnosis (verbose runs)
                if verbose_benders && iter <= 5
                    lp = joinpath(tempdir(),
                        "benders_sp_failed_y$(y_idx)_d$(day_idx)_iter$(iter).lp")
                    try
                        write_to_file(sp.model, lp)
                        @info "  Debug LP written to $(lp)"
                    catch; end
                end
                continue
            end
            infeasible = status === :infeasible
            td, bpd, bcd = extract_subproblem_duals(sp, input; cost_terms = !infeasible)
            cut = build_optimality_cut(objective_value(sp.model), td, bpd, bcd,
                caps, input, y_idx, ypp; fuel_duals = extract_copy_duals(sp))
            push!(cuts, (infeasible ? :feasibility : :optimality, y_idx, day_idx, cut))
            if infeasible
                feasible = false
            else
                year_cost += objective_value(sp.model)
                op === nothing || add_unit_day_operation!(op, sp.ps_vars, input, y_idx,
                    sp.hours, sp.scaling_factor,
                    get(get(sp.model.ext, :master_startup, IdDict()), sp.ps_vars, nothing))
            end
        end
        return year_cost, solved, feasible
    end

    function add_cuts!(cuts, iter)
        for (kind, y_idx, d, cut) in cuts
            if kind === :feasibility
                add_feasibility_cut!(master_model, cut, y_idx, vars, input,
                    "benders_feas_y$(y_idx)_d$(d)_iter$(iter)"; ctx)
            else
                add_optimality_cut!(master_model, theta[(y_idx, d)], cut, y_idx, vars, input,
                    "benders_cut_y$(y_idx)_d$(d)_iter$(iter)"; ctx)
            end
        end
        empty!(cuts)
    end

    # =========================================================================
    # Step 1: Warm-start cuts
    # =========================================================================
    # Cuts from two high-investment points, before the first master solve
    # (with θ = 0 the master would first invest minimally). Generators and
    # batteries in service run at the capacity the subproblems give them
    # (degraded rated power): it is not a master variable, so a cut built at
    # another value would not bound the true cost. Life-extended units, the
    # candidates and every copy carry their gradient, so any value is valid.
    # A point that breaks the curtailment limit gives a feasibility cut.
    @info "Benders: Computing warm-start cuts..."
    n_tech_ws = length(input.technologies)
    n_bat_tech_ws = length(input.battery_technologies)
    n_buses_ws = input.network.num_buses
    cuts = Tuple{Symbol, Int, Int, BendersCut}[]

    for y_idx in 1:num_years
        warm_gen_cap, warm_bat_pow, warm_bat_en = existing_capacities(input, y_idx,
            (kind, i, b, full) ->
                (kind === :gen ? vars.gen_life_extension :
                                 vars.bat_life_extension)[y_idx][i][b] === nothing ? 0.0 : full)
        # Large technology capacity (1000 MW each) — eliminates capacity constraints
        warm_tech_cap = Dict{Tuple{Int,Int}, Float64}()
        for t in 1:n_tech_ws
            for b in 1:n_buses_ws
                warm_tech_cap[(t, b)] = 1000.0
            end
        end
        warm_bt_pow = Dict{Tuple{Int,Int}, Float64}()
        warm_bt_en = Dict{Tuple{Int,Int}, Float64}()
        for bt in 1:n_bat_tech_ws
            for b in 1:n_buses_ws
                warm_bt_pow[(bt, b)] = 1000.0
                warm_bt_en[(bt, b)] = 5000.0
            end
        end

        n_sys_ws = max(1, length(input.system_node_ranges))
        warm_re_ratios = [haskey(targets, (s, y_idx)) ? targets[(s, y_idx)] : 0.0 for s in 1:n_sys_ws]
        warm_caps = FixedCapacities(
            warm_gen_cap, warm_bat_pow, warm_bat_en,
            warm_tech_cap, warm_bt_pow, warm_bt_en,
            Dict{Int, Float64}(),  # line capacity (handled in subproblem)
            warm_re_ratios
        )
        year_min_cost, _, _ = evaluate_year!(cuts, y_idx, warm_caps, 0)

        # Second warm-start: moderate battery capacity to reveal battery value.
        # The first warm-start uses oversized batteries (1000 MW) → constraints
        # never bind → battery duals are 0 → master gets no battery signal.
        # Here we use RE capacity ≈ demand/avg_avail and battery capacity ≈
        # peak_demand × 0.3, so battery constraints bind during peak solar
        # hours, producing non-zero duals that seed battery investment.
        if n_bat_tech_ws > 0
            peak_demand = maximum(sum(input.base_demand, dims=2))
            target_y = haskey(targets, (1, y_idx)) ? targets[(1, y_idx)] : 0.5

            mod_tech_cap = Dict{Tuple{Int,Int}, Float64}()
            for t in 1:n_tech_ws
                tech_ws = input.technologies[t]
                if tech_ws.type == "Renewable"
                    # Count buses with non-zero availability to distribute capacity
                    avail_hrs = size(tech_ws.availability, 1)
                    n_re_buses = 0
                    for b in 1:n_buses_ws
                        avg_a = avail_hrs > 0 ? sum(tech_ws.availability[:, b]) / avail_hrs : 0.0
                        if avg_a > 0.01
                            n_re_buses += 1
                        end
                    end
                    n_re_buses = max(n_re_buses, 1)
                    for b in 1:n_buses_ws
                        avg_a = avail_hrs > 0 ? sum(tech_ws.availability[:, b]) / avail_hrs : 0.3
                        # Distribute total system RE need across buses with availability
                        mod_tech_cap[(t, b)] = avg_a > 0 ? target_y * peak_demand / (avg_a * n_re_buses) : 0.0
                    end
                else
                    for b in 1:n_buses_ws
                        mod_tech_cap[(t, b)] = 0.0
                    end
                end
            end
            # Moderate battery: 30% of peak demand power, 4h duration
            mod_bt_pow = Dict{Tuple{Int,Int}, Float64}()
            mod_bt_en = Dict{Tuple{Int,Int}, Float64}()
            for bt in 1:n_bat_tech_ws
                for b in 1:n_buses_ws
                    mod_bt_pow[(bt, b)] = peak_demand * 0.3
                    mod_bt_en[(bt, b)] = peak_demand * 0.3 * 4.0
                end
            end
            mod_caps = FixedCapacities(
                warm_gen_cap, warm_bat_pow, warm_bat_en,
                mod_tech_cap, mod_bt_pow, mod_bt_en,
                Dict{Int, Float64}(), warm_re_ratios
            )
            evaluate_year!(cuts, y_idx, mod_caps, 0)
        end

        if verbose_benders && (y_idx <= 3 || y_idx == num_years)
            @info "  Warm-start y$(y_idx): warm_cost = $(round(year_min_cost, sigdigits=4)) (cuts added)"
        end
    end
    add_cuts!(cuts, 0)
    @info "Benders: Warm-start complete. Cuts added from high-investment points."

    # =========================================================================
    # Step 2: Benders iteration loop
    # =========================================================================
    lb_history = Float64[]
    ub_history = Float64[]
    best_ub = Inf
    # The iteration with the best upper bound: its master values (θ aside)
    # and operating costs are what the run returns
    best_iter = 0
    best_values = Dict{VariableRef, Float64}()
    best_year_ops = zeros(num_years)
    year_operational_costs = zeros(num_years)
    # The existing units' operation in the best iteration's subproblems
    new_op() = record_unit_operation ? UnitOperation(1.0, input.discount_rate) : nothing
    iter_op = new_op()
    best_op = iter_op
    theta_vars = Set(values(theta))

    # Create optimizer factory for resetting between iterations
    master_optimizer = create_optimizer(
        solver_name=input.solver_name,
        threads=input.threads,
        time_limit=min(input.time_limit, 600.0),
        gap=input.gap,
        verbose=input.verbose
    )

    # The best iteration's plan: the master as solved if that is the last
    # solve, otherwise re-solved with its variables fixed to the best values
    # With record_unit_operation, the plan's unit operation and asset costs
    unit_ops(op) = op === nothing ? UnitOperation[] :
        [add_unit_asset_costs!(op, vars, input, nothing)]
    function best_solution(last_solved_iter)
        if best_iter == 0
            @warn "Benders: no iteration solved every subproblem within the " *
                  "curtailment limit; returning the last master solution."
            set_optimizer(master_model, master_optimizer)
            optimize!(master_model)
            return extract_master_solution_from_benders(master_model, vars, input,
                                                        year_operational_costs;
                                                        unit_operation = unit_ops(iter_op))
        end
        if best_iter != last_solved_iter
            for (v, x) in best_values
                fix(v, is_integer(v) || is_binary(v) ? round(x) : x; force = true)
            end
            set_optimizer(master_model, master_optimizer)
            optimize!(master_model)
        end
        return extract_master_solution_from_benders(master_model, vars, input, best_year_ops;
                                                    objective = best_ub,
                                                    unit_operation = unit_ops(best_op))
    end

    iterations = 0
    for iter in 1:max_iterations
        # ----- Solve master -----
        # Reset optimizer state before each solve to avoid HiGHS internal state
        # issues when constraints are added incrementally after a previous solve.
        # The LP is re-sent from JuMP's cache, ensuring a clean solve.
        set_optimizer(master_model, master_optimizer)
        optimize!(master_model)
        master_status = termination_status(master_model)
        if master_status != MOI.OPTIMAL && master_status != MOI.LOCALLY_SOLVED
            @warn "Benders: Master solve failed at iteration $(iter): $(master_status)"
            if verbose_benders
                lp = joinpath(tempdir(), "benders_master_failed_iter$(iter).lp")
                try
                    write_to_file(master_model, lp)
                    @info "  Debug LP written to $(lp)"
                catch; end
            end
            break
        end
        iterations = iter

        lb = objective_value(master_model)
        push!(lb_history, lb)

        # ----- Evaluate investment cost at current solution -----
        inv_cost_val = value(inv_cost_expr)

        if verbose_benders
            # Log theta values for diagnostics
            theta_sum_actual = sum(value(t) * COST_SCALE for t in values(theta); init = 0.0)
            @info "Benders iteration $(iter): LB = $(round(lb, digits=2)), " *
                  "inv_cost = $(round(inv_cost_val, digits=0)), " *
                  "Σθ(actual\$) = $(round(theta_sum_actual, digits=0))"
        end

        # ----- Solve subproblems for each year -----
        total_operational_cost = 0.0
        year_operational_costs = zeros(num_years)
        all_subproblems_solved = true
        iter_op = new_op()

        for y_idx in 1:num_years
            isempty(rep_days_by_year[y_idx]) && continue
            # Evaluate fixed capacities from master solution
            fixed_caps = evaluate_fixed_capacities(vars, input, y_idx; ctx)
            year_cost, solved, feasible = evaluate_year!(cuts, y_idx, fixed_caps, iter;
                                                         op = iter_op)
            all_subproblems_solved &= solved && feasible
            year_operational_costs[y_idx] = year_cost
            total_operational_cost += discount(y_idx) * year_cost
        end

        # ----- Compute upper bound -----
        # UB = first-stage cost (investment + life ext + decomm + slack) + actual operational cost
        # The master objective includes θ × COST_SCALE × discount, so subtract that
        theta_contribution = sum(value(t) * COST_SCALE * discount(y)
                                 for ((y, _), t) in theta; init = 0.0)
        master_first_stage = value(objective_function(master_model)) - theta_contribution
        ub = master_first_stage + total_operational_cost
        # Only an iteration whose subproblems all solved within the
        # curtailment limit gives an upper bound
        if all_subproblems_solved && ub < best_ub
            best_ub = ub
            best_iter = iter
            best_year_ops = copy(year_operational_costs)
            best_op = iter_op
            best_values = Dict(v => value(v) for v in all_variables(master_model)
                               if !(v in theta_vars))
        end
        push!(ub_history, best_ub)

        # ----- Check convergence -----
        gap = isfinite(best_ub) ? abs(best_ub - lb) / max(abs(best_ub), 1e-10) : Inf
        if verbose_benders
            @info "  UB = $(round(best_ub, digits=2)), gap = $(round(gap*100, digits=4))%, " *
                  "first_stage = $(round(master_first_stage, digits=0)), " *
                  "total_ops = $(round(total_operational_cost, digits=0))"
            # Log per-year breakdown for first 3 iterations
            if iter <= 3
                for y in 1:min(5, num_years)
                    theta_val = sum(value(theta[(y, d)]) * COST_SCALE
                                    for d in eachindex(rep_days_by_year[y]); init = 0.0)
                    @info "    y$(y): θ=$(round(theta_val, sigdigits=4)), " *
                          "actual_Q=$(round(year_operational_costs[y], sigdigits=4)), " *
                          "disc=$(round(discount(y), sigdigits=4))"
                end
            end
        end

        if gap < tolerance
            @info "Benders: Converged in $(iter) iterations (gap = $(round(gap*100, digits=4))%)"
            return BendersResult(
                best_solution(iter), best_ub, iter, gap,
                lb_history, ub_history,
                time() - start_time
            )
        end

        # ----- Add cuts -----
        n_cuts = length(cuts)
        add_cuts!(cuts, iter)
        if verbose_benders
            @info "  Added $(n_cuts) cuts"
        end
    end

    # If the loop broke before any successful master solve, no lower bound was
    # ever established. Surface a clear, actionable error instead of crashing on
    # an empty lb_history (BoundsError).
    if isempty(lb_history)
        error(
            "Benders master problem was infeasible/unsolved on the first " *
            "iteration; no lower bound was established. This usually means the " *
            "investment master is over-constrained — e.g. an RE-penetration " *
            "target the candidate technologies cannot meet, or too tight an " *
            "investment budget. Run with verbose output to write the master " *
            "LP to $(joinpath(tempdir(), "benders_master_failed_iter1.lp"))."
        )
    end

    # Reached max iterations (or a master solve failed) without convergence
    final_gap = isfinite(best_ub) ?
        abs(best_ub - lb_history[end]) / max(abs(best_ub), 1e-10) : Inf
    @warn (iterations == max_iterations ?
           "Benders: Max iterations ($(max_iterations)) reached. " :
           "Benders: Stopped after $(iterations) iterations without converging. ") *
          "Final gap = $(round(final_gap * 100, digits=4))%"

    # Cuts were added after the last solve, so the best iteration's plan is
    # always re-solved with its values fixed
    return BendersResult(
        best_solution(-1), best_ub, iterations, final_gap,
        lb_history, ub_history,
        time() - start_time
    )
end

# =============================================================================
# Solution Extraction
# =============================================================================

"""
    extract_master_solution_from_benders(model, vars, input, year_operational_costs;
                                         objective=nothing, unit_operation=UnitOperation[])

Extract a MasterProblemResult from the Benders master solution.
Reuses the logic from extract_master_solution but provides operational costs
from subproblem solves instead of embedded variables, and `objective` (the
plan's upper bound) instead of the master's objective when given.
`unit_operation` is the existing units' operation in the plan's subproblems.
"""
function extract_master_solution_from_benders(
    model::Model,
    vars::MasterProblemVariables,
    input::MasterProblemInput,
    year_operational_costs::Vector{Float64};
    objective::Union{Nothing, Float64} = nothing,
    unit_operation::Vector{UnitOperation} = UnitOperation[]
)::MasterProblemResult
    # Use existing extraction (it handles investment vars, cumulative caps, etc.)
    result = extract_master_solution(model, vars, input)

    # Override operational costs with values from subproblem solves
    return MasterProblemResult(
        result.status,
        objective === nothing ? result.objective : objective,
        result.solve_time,
        result.tech_investment,
        result.bat_tech_power_investment,
        result.bat_tech_capacity_investment,
        result.transfer_investment,
        result.gen_life_extension,
        result.bat_life_extension,
        result.total_investment_by_year,
        year_operational_costs,  # Override with actual subproblem costs
        result.re_penetration_by_year,
        result.re_penetration_by_system,
        result.cumulative_gen_capacity,
        result.cumulative_bat_capacity,
        result.cumulative_bat_power,
        result.cumulative_tech_capacity,
        result.cumulative_bat_tech_power,
        result.cumulative_bat_tech_capacity,
        result.reservoir_investment,
        result.converter_investment,
        result.fuel_investment,  # M2 (#89)
        unit_operation
    )
end
