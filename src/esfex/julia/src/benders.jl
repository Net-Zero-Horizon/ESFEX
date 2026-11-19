"""
benders.jl - Benders Decomposition for Master Problem

Decomposes the monolithic capacity expansion problem into:
- Master: investment decisions + one recourse variable θ[y] per year
- Subproblems: one operational LP per year holding all its representative
  days with FIXED capacities, plus the year's annual CO2 budget and RE target
  and growth cap, which couple the days

Algorithm:
1. Create Benders master (investment-only), one θ[y] per year
2. Iterate:
   a. Solve master → x*, θ*; LB = objective
   b. Fix capacities from x*, solve every year's subproblem → Q_y
   c. If a year subproblem breaks its annual curtailment limit, add a feasibility cut;
      otherwise UB = min(UB, first-stage cost(x*) + Σ_y discount_y × Q_y)
   d. Generate one optimality cut per year: θ[y] ≥ intercept + gradient^T × x
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

# A year subproblem meets a system's curtailment limit when it curtails at
# most CURTAILMENT_LIMIT_ATOL (MWh/year) + CURTAILMENT_LIMIT_RTOL × the
# system's renewable generation of the year above it (`curtailment_tolerance`):
# solver tolerances leave a few MWh/year on a limit of TWh/year
const CURTAILMENT_LIMIT_RTOL = 1e-6
const CURTAILMENT_LIMIT_ATOL = 1e-3

# In-out stabilisation: weight of the master's plan in the point the
# subproblems are solved at (the rest is the previous point solved)
const BENDERS_INOUT_WEIGHT = 0.5
# Gap below which the subproblems are solved at the master's plan itself
const BENDERS_INOUT_GAP = 0.01

# Time limit (s) of a master solve, below `solver.time_limit`: a master that
# reaches it without a plan is solved again with `solver.time_limit`, and so
# are the later ones
const BENDERS_MASTER_TIME_LIMIT = 600.0

# Least battery-technology power (MW) the subproblems see at every bus where
# one can be built (`benders_battery_floor`), so that their duals price a
# battery there; the energy is this × the technology's minimum duration
const BENDERS_BATTERY_FLOOR = 1e-4

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

"""
    benders_battery_floor(fc, input) -> FixedCapacities

`fc` with at least `BENDERS_BATTERY_FLOOR` MW of each battery technology
(and that × its minimum duration of energy, at least 1 h) at every bus where
it can be built. With no battery at a bus, its charge, discharge and level
are all fixed at 0 and the price of its stored energy is free: the solver
may then value a MW of power alone at the value of lost load in every step,
a valid but useless cut (Cuba: the master moved a battery it had already
paid for to such a bus each iteration and the lower bound never moved).
With a sliver of battery the level is free to move and the duals price
power and energy as the battery can use them. A battery never raises a
day's cost, and this one changes it by at most its tiny energy.
"""
function benders_battery_floor(fc::FixedCapacities, input::MasterProblemInput)
    isempty(input.battery_technologies) && return fc
    pow = copy(fc.bat_tech_power)
    en = copy(fc.bat_tech_energy)
    for (bt, btech) in enumerate(input.battery_technologies), b in 1:input.network.num_buses
        b <= length(btech.invest_max_power) && btech.invest_max_power[b] > 0 || continue
        p = BENDERS_BATTERY_FLOOR
        e = p * max(btech.min_duration_hours, 1.0)
        get(pow, (bt, b), 0.0) < p && (pow[(bt, b)] = p)
        get(en, (bt, b), 0.0) < e && (en[(bt, b)] = e)
    end
    return FixedCapacities(fc.gen_capacity, fc.bat_power, fc.bat_energy, fc.tech_capacity,
                           pow, en, fc.line_capacity, fc.re_ratios, fc.fuel_extra)
end

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

"""
One representative day of a subproblem: its variables and the constraint
references its cut gradient is read from. Its `model` is the year's
(`BendersYearSubproblem`), or its own when built alone.
"""
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

"""
The subproblem of one year: one LP with all its representative days
(`days`) and the annual constraints that couple them (CO2 budget, RE target
and growth cap, `create_benders_year_subproblem`). `constraint_store` holds
the copies of the master's RE shares (`"copy_fix"`).
"""
struct BendersYearSubproblem
    model::Model
    days::Vector{SubproblemData}
    constraint_store::Dict{String, Any}
    year_idx::Int
end

# =============================================================================
# Copy variables
# =============================================================================
#
# A master quantity that a subproblem reads (a built capacity, an RE share)
# enters the subproblem as a copy variable fixed to the master's value. The
# dual of the fixing constraint is the year's marginal cost of that quantity,
# and the cut multiplies it by the master expression the key stands for
# (`benders_copy_expr`). Keys are `(kind, name, index)`:
#
#   (:storage | :transport | :electrolyzer, …)  fuel block (M2, master_fuel.jl)
#   (:converter, "acdc" | "freq", c)  converter MW built up to the year
#   (:line, "", l)                    MW built on line l's corridor up to the year
#   (:reservoir, "g", b)              reservoir MWh built up to the year
#   (:gen_life, "g", b), (:bat_life, "bi", b)  life-extended MW this year
#   (:re_energy, "s", y)              system s's renewable generation over its
#                                     demand in year y (`re_energy_ratio`,
#                                     growth cap only)

const BendersCopyKey = Tuple{Symbol, String, Int}

"""
Master-side data the copy keys refer to: the bus pair of each line
(`TransmissionDC` order, which `(:line, "", l)` indexes).
"""
struct BendersCopyContext
    line_ends::Vector{Tuple{Int,Int}}
end

BendersCopyContext() = BendersCopyContext(Tuple{Int,Int}[])

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
    benders_re_capped(input) -> Bool

Whether the RE growth cap is active (`max_re_increment` < 1): each year's
renewable generation (over its demand, `re_energy_ratio`) is then a master
variable, which the next year's cap is measured from
(`create_benders_year_subproblem`).
"""
benders_re_capped(input::MasterProblemInput) = input.max_re_increment < 1.0

"""
    benders_year_demand(input, y_idx, days) -> Vector{Float64}

Demand of each system (`master_systems`) over year `y_idx`'s representative
days (first steps `days`), scaled to the year (MWh/year), as the monolithic
master sums it for the RE target (`add_day_re!`).
"""
function benders_year_demand(input::MasterProblemInput, y_idx::Int, days::Vector{Int})
    timesteps_per_day = 24 ÷ input.temporal_resolution_hours
    out = zeros(length(master_systems(input)))
    for start_hour in days
        end_hour = min(start_hour + timesteps_per_day - 1, size(input.base_demand, 1))
        end_hour >= start_hour || continue
        out .+= rep_day_scaling(input, length(days)) .*
                system_day_demand(input, y_idx, start_hour, end_hour - start_hour + 1)
    end
    return out
end

"""
    benders_re_share_years(input, rep_days_by_year) -> Set{Tuple{Int,Int}}

`(system, year)` whose renewable generation the next year's growth cap is
measured from: with a growth cap, every year but the last in which the
system has demand. The master's `re_energy_ratio` of the other pairs is
fixed to 0.
"""
function benders_re_share_years(input::MasterProblemInput,
                                rep_days_by_year::Dict{Int, Vector{Int}})
    out = Set{Tuple{Int,Int}}()
    benders_re_capped(input) || return out
    num_years = length(input.years)
    for y_idx in 1:(num_years - 1)
        demand = benders_year_demand(input, y_idx, get(rep_days_by_year, y_idx, Int[]))
        for s in eachindex(demand)
            demand[s] > 0 && push!(out, (s, y_idx))
        end
    end
    return out
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
    elseif kind === :re_energy
        # The ratio of the year the key names (this year or the one before)
        add_to_expression!(expr, vars.re_energy_ratio[(parse(Int, name), i)])
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
extension; a renewable-generation ratio, the system's `fc.re_ratios`), or
nothing built.
"""
function copy_value(fc::FixedCapacities, input::MasterProblemInput, key::BendersCopyKey)
    haskey(fc.fuel_extra, key) && return fc.fuel_extra[key]
    kind, name, i = key
    kind === :gen_life && return get(fc.gen_capacity, (parse(Int, name), i), 0.0)
    kind === :bat_life && return get(fc.bat_power, (parse(Int, name), i), 0.0)
    if kind === :re_energy
        s = parse(Int, name)
        isempty(fc.re_ratios) && return 0.0
        return length(fc.re_ratios) >= s ? fc.re_ratios[s] : fc.re_ratios[1]
    end
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

First step of each representative day of each year, the same days of the
year for every year, as the monolithic master has them (`master_rep_days`).
"""
function benders_rep_days(input::MasterProblemInput)
    offsets = master_rep_day_offsets(input)
    return Dict{Int, Vector{Int}}(y_idx => master_rep_days(input, y_idx; offsets)
                                  for y_idx in eachindex(input.years))
end

"""
    benders_cap_exemption(input, targets, rep_days_by_year) -> Dict{Int, Vector{Float64}}

Each year's `re_cap_exemption` over its representative days, as the
monolithic master computes it: the share of the existing renewables'
energy a subproblem may curtail beyond the curtailment limit.
"""
function benders_cap_exemption(input::MasterProblemInput,
                               targets::Dict{Tuple{Int,Int}, Float64},
                               rep_days_by_year::Dict{Int, Vector{Int}})
    timesteps_per_day = 24 ÷ input.temporal_resolution_hours
    out = Dict{Int, Vector{Float64}}()
    prev_floor = nothing
    for y_idx in sort(collect(keys(rep_days_by_year)))
        days = rep_days_by_year[y_idx]
        isempty(days) && continue
        periods = [(h, min(h + timesteps_per_day - 1, size(input.base_demand, 1)) - h + 1,
                    rep_day_scaling(input, length(days))) for h in days]
        out[y_idx], prev_floor = re_cap_exemption(input, targets, y_idx, periods, prev_floor)
    end
    return out
end

"""
    benders_unit_cost(input, unit, b, segs) -> Float64

Least operating cost per MWh a generator or technology `unit` at bus `b`
pays in a subproblem: its fuel cost (the cheapest segment of its offer curve
`segs`; none with the fuel block, which prices fuel through its balance),
maintenance and CO2 price.
"""
function benders_unit_cost(input::MasterProblemInput, unit, b::Int, segs)
    fuel = segs !== nothing ? minimum(s.marginal_cost for s in segs) :
           has_master_fuel(input) ? 0.0 : unit.fuel_cost[b]
    return fuel + unit.maintenance_cost[b] + co2_cost_per_mwh(input, unit, b)
end

"""
    benders_power_sink(vars, input) -> Vector{Float64}

Most power (MW per step) the subproblems can draw at each system's buses
beyond its load and its batteries' charge: what its lossy lines can lose
(each its PWL loss at full flow in one direction: the loss repair
`optimize_exact_losses!` leaves no line losing more than the PWL loss of its
net flow), and the battery floor's sliver charging. `Inf` with anything else
that draws power (converters, reservoir pumps, electrolyzers of the fuel
block, a line to another system) or a lossy line the master can expand (its
last segment is open).
"""
function benders_power_sink(vars::MasterProblemVariables, input::MasterProblemInput)
    systems = master_systems(input)
    net = input.network
    sink = zeros(length(systems))
    if has_master_fuel(input) || !isempty(master_converters(net)) ||
       any(g -> any(b -> b <= length(g.reservoir_capacity) && g.reservoir_capacity[b] > 0 &&
                         g.reservoir_pump_capacity[b] > 0,
                    eachindex(g.reservoir_pump_capacity)), input.generators)
        return fill(Inf, length(systems))
    end
    sys_of(b) = findfirst(sr -> sr.first_bus <= b < sr.first_bus + sr.num_buses, systems)
    for bt in input.battery_technologies, b in 1:net.num_buses
        b <= length(bt.invest_max_power) && bt.invest_max_power[b] > 0 || continue
        s = sys_of(b)
        s === nothing || (sink[s] += BENDERS_BATTERY_FLOOR)
    end
    if net.num_buses > 1 && !isempty(net.transmission_lines)
        t = with_loss_model(TransmissionDC(net), input.transmission_loss_segments, net.s_base)
        pwl = t.pwl_losses
        inv_years = [y for y in 1:vars.years_per_inv_period:length(input.years)
                     if haskey(vars.transfer_investment, y)]
        for (l, (from, to)) in enumerate(t.lines)
            # A line between systems carries one's exports to the other,
            # lossy or not
            sf, st = sys_of(from), sys_of(to)
            if sf != st
                foreach(s -> s === nothing || (sink[s] = Inf), (sf, st))
                continue
            end
            pwl === nothing && continue
            pwl.conductances[l] > 0 || continue
            expandable = any(corridor_investment(vars.transfer_investment[y], from, to) !== nothing
                             for y in inv_years)
            t.line_capacities[l] >= 0.1 || expandable || continue
            sf === nothing ||
                (sink[sf] += expandable ? Inf : sum(pwl.slopes[l] .* pwl.segment_widths[l]))
        end
    end
    return sink
end

"""
    benders_linked_systems(input) -> Vector{Int}

Group of each system (`master_systems`) in the operating bound
(`add_benders_operating_bound!`): systems joined, directly or through
others, by a branch of the subproblems' DC network or a converter between
their buses share a group (they can trade power in the subproblem); every
other system is a group of its own. Groups are numbered by their first
system.
"""
function benders_linked_systems(input::MasterProblemInput)
    systems = master_systems(input)
    group = collect(eachindex(systems))
    sys_of(b) = findfirst(sr -> sr.first_bus <= b < sr.first_bus + sr.num_buses, systems)
    root(s) = group[s] == s ? s : root(group[s])
    function link!(a, b)
        sa, sb = sys_of(a), sys_of(b)
        (sa === nothing || sb === nothing || sa == sb) && return
        ra, rb = root(sa), root(sb)
        ra == rb || (group[max(ra, rb)] = min(ra, rb))
    end
    net = input.network
    if net.num_buses > 1 && !isempty(net.transmission_lines)
        foreach(((f, t),) -> link!(f, t), TransmissionDC(net).lines)
    end
    foreach(((_, conv),) -> link!(conv.from_node, conv.to_node), master_converters(net))
    return [root(s) for s in eachindex(systems)]
end

"""
    add_benders_operating_bound!(model, vars, input, targets, theta, rep_days_by_year)

Lower bound on each year's operating cost `θ[y]` from a relaxation of its
subproblem built into the master (partial Benders decomposition). Each
representative day of each system is one copper plate per step: no network,
converters, reserves, inertia, minimum output or ramps; each generator and
technology pooled over the system's buses, up to its capacity ×
availability, at its least cost per MWh (`benders_unit_cost`); the system's
batteries and battery technologies as one store (the best efficiencies,
free initial level); load shed at the value of lost load (times the least
sectoral criticality when below 1). Systems linked by a line or converter
(`benders_linked_systems`) share one balance per step: the subproblem may
import into one from another, so a balance per system would make the bound
shed load the subproblem does not (a lower bound above the year's cost).
The year's RE target or floor on the RE share (renewable over total
generation; shortfall at `re_shortfall_penalty`) and, with a growth cap,
the cap on the master's renewable generation of the year before and the
year's are as in the subproblem (`create_benders_year_subproblem`). So is
each system's annual curtailment limit: the variable renewables' pooled
available energy left unused over the year is at most
`max_curtailment_ratio` × the year's renewable output plus what the growth
cap lets the year curtail (`re_cap_exempt_energy`) and the subproblem's
tolerance (`curtailment_tolerance`), and supply above the load is at most
what the subproblem can draw beyond it (`benders_power_sink`: line losses;
no bound with converters, pumps, electrolyzers or links between systems),
else renewable output dumped in the relaxation would count as used. Without
the two the relaxation curtailed for free whatever renewable capacity the
master built, while the subproblem cannot curtail more than the limit, so
the master built more than a year could absorb. (The sink is every lossy
line's full-flow loss at once, which on a network of lossy lines, as Cuba's,
can still exceed the surplus; the cuts then carry the rest of the limit.)
Every operation of the subproblem is feasible here at no higher cost, so

    θ[y] ≥ cost of the relaxation (scaled to the year)       (op_bound_y{y})

holds at every plan and leaves the optimum unchanged. Without it θ starts
at 0 and the master moves capacity that costs it nothing (a battery from one
bus to another) to wherever the latest cut promises the most, one iteration
after another (Cuba, three years: the lower bound did not move in 15
iterations). The cuts then only add what the relaxation leaves out.
"""
function add_benders_operating_bound!(model::Model, vars::MasterProblemVariables,
                                      input::MasterProblemInput,
                                      targets::Dict{Tuple{Int,Int}, Float64},
                                      theta::Dict{Int, VariableRef},
                                      rep_days_by_year::Dict{Int, Vector{Int}})
    timesteps_per_day = 24 ÷ input.temporal_resolution_hours
    tres = input.temporal_resolution_hours
    systems = master_systems(input)
    b2n = input.network.bus_to_node
    D = input.base_demand
    crit = isempty(input.sectoral_criticality) ? 1.0 :
           minimum(values(input.sectoral_criticality))
    shed_cost = input.loss_of_load_penalty * min(1.0, crit)
    share_years = benders_re_share_years(input, rep_days_by_year)
    sys_buses(s) = systems[s].first_bus:(systems[s].first_bus + systems[s].num_buses - 1)
    gen_curve(g, b) = begin
        c = get(input.gen_cost_curves, g, nothing)
        s = c === nothing ? nothing : get(c, b, nothing)
        s === nothing || isempty(s) ? nothing : s
    end
    # Least cost per MWh of each unit over the buses of a system it is at
    function pooled_cost(units, curves, s, at)
        out = zeros(length(units))
        for (i, u) in enumerate(units)
            c = [benders_unit_cost(input, u, b, curves(i, b)) for b in sys_buses(s) if at(i, b)]
            isempty(c) || (out[i] = minimum(c))
        end
        return out
    end
    empty_expr(e) = isempty(e.terms) && e.constant <= 0
    # The curtailment limit, as the subproblems have it
    curt_limited = input.max_curtailment_ratio < 1.0
    cap_exempt = curt_limited ? benders_cap_exemption(input, targets, rep_days_by_year) :
                                Dict{Int, Vector{Float64}}()
    sink = benders_power_sink(vars, input)
    group = benders_linked_systems(input)

    for y_idx in sort(collect(keys(theta)))
        days = get(rep_days_by_year, y_idx, Int[])
        isempty(days) && continue
        # Per system, over the year's days (each stands for as many days):
        # the variable renewables' available energy left unused, and the
        # renewable output
        curt_left = [AffExpr(0.0) for _ in systems]
        re_out = [AffExpr(0.0) for _ in systems]
        w = rep_day_scaling(input, length(days))
        growth = (1.0 + input.demand_growth)^(y_idx - 1)
        caps = build_cumulative_capacity_expressions(vars, input, y_idx)
        # The technologies' capacity per bus as one variable each (the
        # cumulative builds), so each step's pooled limit stays short
        tech_cap = Dict{Tuple{Int,Int}, Any}()
        for (t, per_bus) in caps["tech"], b in eachindex(per_bus)
            e = per_bus[b]
            empty_expr(e) && continue
            if isempty(e.terms)
                tech_cap[(t, b)] = e.constant
            else
                v = @variable(model, lower_bound = 0,
                              base_name = "op_bound_cap_y$(y_idx)_t$(t)_b$(b)")
                @constraint(model, v == e)
                tech_cap[(t, b)] = v
            end
        end
        cost = AffExpr(0.0)           # per step, scaled to the year below
        energy = [AffExpr(0.0) for _ in systems]   # MWh/year, renewable
        generation = [AffExpr(0.0) for _ in systems]   # MWh/year, all units
        demand = zeros(length(systems))            # MWh/year
        # Each linked group's supply + shed and load per (day, step)
        pool_supply = Dict{Tuple{Int,Int,Int}, AffExpr}()
        pool_load = Dict{Tuple{Int,Int,Int}, Float64}()
        for s in eachindex(systems)
            gcost = pooled_cost(input.generators, gen_curve, s,
                                (g, b) -> input.generators[g].rated_power[b] > 0)
            tcost = pooled_cost(input.technologies, (t, b) -> tech_curve_segments(input, t, b), s,
                                (t, b) -> haskey(tech_cap, (t, b)))
            P = AffExpr(0.0)
            E = AffExpr(0.0)
            eff_c, eff_d = 0.0, 0.0
            for (bi, bat) in enumerate(input.batteries), b in sys_buses(s)
                add_to_expression!(P, caps["bat_power"][bi][b])
                add_to_expression!(E, caps["bat_capacity"][bi][b])
                eff_c = max(eff_c, bat.charge_efficiency[b])
                eff_d = max(eff_d, bat.discharge_efficiency[b])
            end
            for (bt, btech) in enumerate(input.battery_technologies), b in sys_buses(s)
                add_to_expression!(P, caps["bat_tech_power"][bt][b])
                add_to_expression!(E, caps["bat_tech_capacity"][bt][b])
                eff_c = max(eff_c, btech.charge_efficiency[b])
                eff_d = max(eff_d, btech.discharge_efficiency[b])
            end
            has_store = !empty_expr(P) && eff_c > 0 && eff_d > 0
            for (d, start_hour) in enumerate(days)
                end_hour = min(start_hour + timesteps_per_day - 1, size(D, 1))
                hours = end_hour - start_hour + 1
                hours >= 1 || continue
                day_gens = generators_for_day(input, start_hour, hours)
                soc_prev = nothing
                if has_store
                    soc_prev = @variable(model, lower_bound = 0,
                                         base_name = "op_bound_soc_y$(y_idx)_s$(s)_d$(d)_t0")
                    @constraint(model, soc_prev <= E)
                end
                for ts in 1:hours
                    h = start_hour + ts - 1
                    tag = "y$(y_idx)_s$(s)_d$(d)_t$(ts)"
                    load = 0.0
                    for b in sys_buses(s)
                        load += D[h, b2n[b]] * input.network.buses[b].demand_fraction * growth
                    end
                    demand[s] += w * load
                    supply = AffExpr(0.0)
                    for (g, gen) in enumerate(input.generators)
                        lim = AffExpr(0.0)
                        for b in sys_buses(s)
                            gen.rated_power[b] > 0 || continue
                            add_to_expression!(lim, day_gens[g].availability[ts, b],
                                               caps["gen"][g][b])
                        end
                        empty_expr(lim) && continue
                        p = @variable(model, lower_bound = 0, base_name = "op_bound_gen$(g)_$(tag)")
                        @constraint(model, p <= lim)
                        add_to_expression!(supply, p)
                        add_to_expression!(cost, gcost[g], p)
                        add_to_expression!(generation[s], w, p)
                        is_renewable_unit(gen) || continue
                        add_to_expression!(energy[s], w, p)
                        add_to_expression!(re_out[s], p)
                        # What the subproblem counts as curtailed (a unit
                        # variable at only some buses counts nowhere: less)
                        if all(is_variable_renewable(gen, b) for b in sys_buses(s)
                               if gen.rated_power[b] > 0)
                            add_to_expression!(curt_left[s], lim)
                            add_to_expression!(curt_left[s], -1.0, p)
                        end
                    end
                    for (t, tech) in enumerate(input.technologies)
                        lim = AffExpr(0.0)
                        for b in sys_buses(s)
                            c = get(tech_cap, (t, b), nothing)
                            c === nothing || add_to_expression!(lim, tech_availability_at(tech, h, b), c)
                        end
                        empty_expr(lim) && continue
                        p = @variable(model, lower_bound = 0, base_name = "op_bound_tech$(t)_$(tag)")
                        @constraint(model, p <= lim)
                        add_to_expression!(supply, p)
                        add_to_expression!(cost, tcost[t], p)
                        add_to_expression!(generation[s], w, p)
                        is_renewable_unit(tech) || continue
                        add_to_expression!(energy[s], w, p)
                        add_to_expression!(re_out[s], p)
                        if is_variable_renewable(tech, 1)
                            add_to_expression!(curt_left[s], lim)
                            add_to_expression!(curt_left[s], -1.0, p)
                        end
                    end
                    if has_store
                        ch = @variable(model, lower_bound = 0, base_name = "op_bound_ch_$(tag)")
                        dis = @variable(model, lower_bound = 0, base_name = "op_bound_dis_$(tag)")
                        soc = @variable(model, lower_bound = 0, base_name = "op_bound_soc_$(tag)")
                        @constraint(model, ch <= P)
                        @constraint(model, dis <= P)
                        @constraint(model, soc <= E)
                        @constraint(model, soc == soc_prev + tres * (eff_c * ch - dis / eff_d))
                        soc_prev = soc
                        add_to_expression!(supply, dis)
                        add_to_expression!(supply, -1.0, ch)
                    end
                    shed = @variable(model, lower_bound = 0, base_name = "op_bound_shed_$(tag)")
                    add_to_expression!(cost, shed_cost, shed)
                    # The balance of the system's linked group (below)
                    key = (group[s], d, ts)
                    add_to_expression!(get!(() -> AffExpr(0.0), pool_supply, key), supply)
                    add_to_expression!(pool_supply[key], shed)
                    pool_load[key] = get(pool_load, key, 0.0) + load
                    # Supply above the load only as far as the subproblem
                    # can draw it (losses), or what is not curtailed would
                    # count as used (no bound for a linked system: its sink
                    # is the other systems)
                    isfinite(sink[s]) && @constraint(model, supply + shed <= load + sink[s],
                        base_name = "op_bound_sink_$(tag)")
                end
            end
        end
        # One balance per linked group of systems and step
        for ((gi, d, ts), sup) in pool_supply
            @constraint(model, sup >= pool_load[(gi, d, ts)],
                        base_name = "op_bound_balance_y$(y_idx)_g$(gi)_d$(d)_t$(ts)")
        end
        # Each system's annual curtailment limit, with the growth cap's
        # exemption (`re_cap_exempt_energy`) and the subproblem's tolerance
        # (`curtailment_tolerance`, in the sum's units: MWh/year / w)
        if curt_limited
            exempt = zeros(length(systems))
            exempt_share = get(cap_exempt, y_idx, Float64[])
            for start_hour in days
                hours = min(start_hour + timesteps_per_day - 1, size(D, 1)) - start_hour + 1
                hours >= 1 || continue
                exempt .+= re_cap_exempt_energy(input, exempt_share, y_idx, start_hour, hours)
            end
            for s in eachindex(systems)
                isempty(re_out[s].terms) && continue
                @constraint(model, curt_left[s] <=
                    (input.max_curtailment_ratio + CURTAILMENT_LIMIT_RTOL) * re_out[s] +
                    exempt[s] + CURTAILMENT_LIMIT_ATOL / w,
                    base_name = "op_bound_curt_s$(s)_y$(y_idx)")
            end
        end
        # The year's RE target and growth cap, as the subproblem has them
        # (their slacks in MWh/year, hence / w in the per-step cost)
        for s in eachindex(systems)
            demand[s] > 0 || continue
            slack = @variable(model, lower_bound = 0, base_name = "op_bound_re_slack_y$(y_idx)_s$(s)")
            @constraint(model,
                energy[s] + slack >= re_required_share(input, targets, s, y_idx) * generation[s])
            add_to_expression!(cost, re_shortfall_penalty(input) / w, slack)
            benders_re_capped(input) || continue
            allowance = (s, y_idx - 1) in share_years ?
                demand[s] * vars.re_energy_ratio[(s, y_idx - 1)] :
                re_cap_prev_allowance(re_share_anchor(input, s), generation[s], demand[s])
            over = @variable(model, lower_bound = 0, base_name = "op_bound_re_over_y$(y_idx)_s$(s)")
            @constraint(model,
                energy[s] - allowance <= input.max_re_increment * generation[s] + over)
            add_to_expression!(cost, re_cap_penalty(input) / w, over)
            if (s, y_idx) in share_years
                under = @variable(model, lower_bound = 0,
                                  base_name = "op_bound_re_under_y$(y_idx)_s$(s)")
                @constraint(model,
                    energy[s] + under >= demand[s] * vars.re_energy_ratio[(s, y_idx)])
                add_to_expression!(cost, re_cap_penalty(input) / w, under)
            end
        end
        @constraint(model, theta[y_idx] >= w * cost, base_name = "op_bound_y$(y_idx)")
    end
    return nothing
end

"""
    create_benders_master(input; rep_days_by_year=benders_rep_days(input))

Investment-only master: the investment constraints and objective of the
monolithic master (`create_master_problem` without its representative days),
plus one recourse variable θ[y] per year with representative days for that
year's operating cost. Returns `(model, vars, targets, theta, inv_cost_expr)`.

It has no constraint of its own linking investment to the renewable energy
or to storage: the year subproblems hold the RE target, the growth cap and
the CO2 budget (`create_benders_year_subproblem`), and the cuts carry the
value of every capacity. With a growth cap, the renewable generation over
demand `re_energy_ratio[(σ, y)]` of a year the next year's cap is measured
from (`benders_re_share_years`) is a master variable the subproblems copy;
the others are fixed to 0.
"""
function create_benders_master(
    input::MasterProblemInput;
    rep_days_by_year::Dict{Int, Vector{Int}} = benders_rep_days(input)
)
    model = Model(create_optimizer(
        solver_name=input.solver_name,
        threads=input.threads,
        time_limit=capped_time_limit(input.time_limit, BENDERS_MASTER_TIME_LIMIT),
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
    # Investment budget only: the operation lives in the subproblems (the
    # Python adapter solves the monolithic master for budget_scope: total)
    input.budget_total &&
        @warn "Benders: the annual budget covers the investment only (budget_scope: total is ignored)"
    add_budget_constraints!(model, vars, input)
    add_retirement_cascade_constraints!(model, vars, input)
    add_capacity_adequacy_constraints!(model, vars, input)
    add_transmission_symmetry_constraints!(model, vars, input)

    # NOTE: NO add_representative_days_validation! — this is the decomposed
    # part; the RE target, growth cap and CO2 budget are in the subproblems
    share_years = benders_re_share_years(input, rep_days_by_year)
    for (key, v) in vars.re_energy_ratio
        key in share_years || fix(v, 0.0; force = true)
    end

    # θ[y] is in units of actual_cost / COST_SCALE; cuts and objective
    # use COST_SCALE accordingly.
    num_years = length(input.years)
    theta = Dict{Int, VariableRef}()
    for y_idx in 1:num_years
        isempty(get(rep_days_by_year, y_idx, Int[])) && continue
        theta[y_idx] = @variable(model, lower_bound = 0, base_name = "theta_y$(y_idx)")
    end
    # θ is at least the cost of a copper-plate relaxation of the year
    add_benders_operating_bound!(model, vars, input, targets, theta, rep_days_by_year)

    # Objective as build_master_objective!, with θ for the operating cost
    inv_cost_expr = build_investment_cost_expression(vars, input)
    total_cost = copy(inv_cost_expr)
    for y_idx in 1:num_years
        discount_factor = 1.0 / ((1.0 + input.discount_rate)^(y_idx - 1))
        add_to_expression!(total_cost, build_annual_asset_cost(vars, input, y_idx),
                           discount_factor)
        haskey(theta, y_idx) &&
            add_to_expression!(total_cost, theta[y_idx], discount_factor * COST_SCALE)
    end
    add_to_expression!(total_cost, master_slack_cost(vars, input))

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
    evaluate_fixed_capacities(vars, input, year_idx; ctx=BendersCopyContext(), val=value)

Compute fixed capacity values from master solution for a given year.
Same logic as build_cumulative_capacity_expressions but with value() on variables.
The copy values (fuel, converter, line and reservoir builds, the RE shares
of the year and the year before) are the solved `benders_copy_expr` of
each key. `val(v)` is the value taken for each master variable (another
plan than the solved one: the separation point of `run_benders_decomposition`).
"""
function evaluate_fixed_capacities(
    vars::MasterProblemVariables,
    input::MasterProblemInput,
    year_idx::Int;
    ctx::BendersCopyContext = BendersCopyContext(),
    val::Function = value
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
            v === nothing ? 0.0 : val(v)
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
                    cumul += val(vars.tech_investment[y][t][b]) * deg
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
                    pow += val(vars.bat_tech_power_investment[y][bt][b])
                    en += val(vars.bat_tech_capacity_investment[y][bt][b])
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

    # Renewable-generation ratios from the master (the subproblems read copies)
    re_ratios = [val(vars.re_energy_ratio[(s, year_idx)])
                 for s in eachindex(master_systems(input))]

    copies = master_fuel_extra_values(vars.fuel, input, year_idx; val)  # M2 (#89)
    for (key, _) in master_converters(input.network)
        k = (:converter, key[1], key[2])
        copies[k] = value(val, benders_copy_expr(vars, input, year_idx, k, ctx))
    end
    for l in eachindex(ctx.line_ends)
        k = (:line, "", l)
        copies[k] = value(val, benders_copy_expr(vars, input, year_idx, k, ctx))
    end
    for (g, b) in benders_reservoir_builds(vars, input, year_idx)
        k = (:reservoir, string(g), b)
        copies[k] = value(val, benders_copy_expr(vars, input, year_idx, k, ctx))
    end
    # The renewable-generation ratios of this year and the year before (growth cap)
    for ((s, y), v) in vars.re_energy_ratio
        y in (year_idx - 1, year_idx) && (copies[(:re_energy, string(s), y)] = val(v))
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
    benders_subproblem_optimizer(input; time_limit=300.0)

Optimizer of the Benders subproblems: the configured solver (its threads
and `solver_options`), so a commercial solver also solves the large
operational LPs; HiGHS single-threaded, which avoids its intermittent
OTHER_ERROR of multi-threaded runs on these LPs. At most `time_limit`
seconds (and the input's, when it sets one: `capped_time_limit`).
"""
function benders_subproblem_optimizer(input::MasterProblemInput; time_limit::Float64 = 300.0)
    limit = capped_time_limit(input.time_limit, time_limit)
    if lowercase(input.solver_name) == "highs"
        return () -> begin
            opt = HiGHS.Optimizer()
            MOI.set(opt, MOI.RawOptimizerAttribute("threads"), 1)
            MOI.set(opt, MOI.RawOptimizerAttribute("time_limit"), limit)
            MOI.set(opt, MOI.RawOptimizerAttribute("output_flag"), false)
            MOI.set(opt, MOI.RawOptimizerAttribute("presolve"), "on")
            return opt
        end
    end
    return create_optimizer(solver_name = input.solver_name, threads = input.threads,
                            time_limit = limit, gap = input.gap, verbose = false,
                            solver_options = input.solver_options)
end

"""
    create_benders_subproblem(input, year_idx, day_idx, demand, start_hour, fixed_caps, vars;
                              re_cap_exempt=Float64[], model=nothing, ndays)

Operational dispatch of a single representative day with FIXED capacities
from the master solution, added to `model` (a year's subproblem,
`create_benders_year_subproblem`), or, without one, in a model of its own
whose objective is the day's cost.

The day's cost scaled to the year (`rep_day_scaling` with the `ndays` days
selected in the year) is `constraint_store["day_cost"]`; its renewable and
total generation and demand per system (`"re_day"`) and CO2 per budget group
(`"co2_day"`), per step, are left for the year's annual constraints.
Returns SubproblemData with constraint references for dual extraction.
"""
function create_benders_subproblem(
    input::MasterProblemInput,
    year_idx::Int,
    day_idx::Int,
    demand::Matrix{Float64},
    start_hour::Int,
    fixed_caps::FixedCapacities,
    master_vars::MasterProblemVariables;
    re_cap_exempt::Vector{Float64} = Float64[],
    model::Union{Nothing, Model} = nothing,
    ndays::Integer = input.representative_days_per_year
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

    sp_model = model === nothing ? Model(benders_subproblem_optimizer(input)) : model

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
            for b in 1:n_buses
                cumul_cap = get(fixed_caps.tech_capacity, (t, b), 0.0)
                avails = Vector{Float64}(undef, hours)
                for ts in 1:hours
                    # Same availability as the monolithic master (every technology)
                    avail = tech_availability_at(tech, start_hour + ts - 1, b)
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

    day_generators = generators_for_day(input, start_hour, hours)

    day_input = (
        generators = day_generators,
        batteries = batteries_for_day(input, start_hour, hours),
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
        # Reserves are pooled per system (one per original system of a merged run)
        reserve_node_groups = system_node_groups(input.co2_budget_groups, n_nodes),
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
    # The units the master builds hold reserve, as in the monolithic master;
    # their capacity is a constant of the reserve constraints, recorded for
    # the cut like the capacity limits ("tech_pow", "bt_pow")
    reserve_refs = add_reserve_constraints!(sp_model, ps_vars, day_input;
        capacity_override=total_cap_gen,
        battery_capacity_override=total_cap_bat_power,
        demand_scale=1.0,
        extra_headroom=master_unit_headroom(input, start_hour, tech_output,
            (t, b) -> get(fixed_caps.tech_capacity, (t, b), 0.0),
            bat_tech_discharge, bat_tech_charge,
            (bt, b) -> get(fixed_caps.bat_tech_power, (bt, b), 0.0)))
    reserve_groups = reserve_node_groups(day_input)
    for ((k, ts), crefs) in reserve_refs, b in 1:n_buses
        input.network.bus_to_node[b] in reserve_groups[k] || continue
        for (kind, i, a, d) in master_unit_reserve_terms(input, start_hour, b, ts)
            store_kind = kind === :tech ? "tech_pow" : "bt_pow"
            (kind === :tech ? tech_output : bat_tech_discharge) === nothing && continue
            entries = get!(constraint_store, store_kind, Tuple{Any, Float64, Int, Int}[])
            push!(entries, (crefs[1], a, i, b))
            crefs[2] === nothing || push!(entries, (crefs[2], d, i, b))
        end
    end
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

    # The day's renewable and total generation and demand per system, as
    # the monolithic master counts them (add_day_re!), for the year's RE
    # target and growth cap (create_benders_year_subproblem) and the
    # achieved share
    constraint_store["re_day"] = (system_day_renewable(ps_vars, tech_output, input, hours),
                                  system_day_generation(ps_vars, tech_output, input, hours),
                                  system_day_demand(input, year_idx, start_hour, hours))

    # Candidate technologies' curtailment, fixed_cap × avail − tech_output
    # (variable renewables only, as the monolithic master). The curtailment
    # limit counts it (`add_benders_curtailment_limits!`): when something
    # other than the capacity constraint limits tech dispatch, the capacity
    # dual is 0 and only the limit's dual tells the master about
    # overcapacity (extract_subproblem_duals). tech_avail_sum tracks
    # Σ avail per (tech, bus), the capacity's coefficient in it.
    tech_avail_sum = Dict{Tuple{Int,Int}, Float64}()
    # The monolithic master also charges curtailment_cost on it (with a
    # curtailment limit). Its cap × avail part is a constant here, so its
    # gradient, curtailment_cost × Σ avail per MW (scaled to the year, see
    # extract_subproblem_duals), goes into the cut separately.
    curt_limited = input.max_curtailment_ratio < 1.0
    charge_tech_curt = curt_limited && input.curtailment_cost > 0
    tech_curt_grad = Dict{Tuple{Int,Int}, Float64}()
    if tech_output !== nothing
        for tc in 1:n_tech
            tech = input.technologies[tc]
            is_variable_renewable(tech, 1) || continue
            for b in 1:n_buses
                cumul_cap = get(fixed_caps.tech_capacity, (tc, b), 0.0)
                avail_total = 0.0
                for ts in 1:hours
                    avail = tech_availability_at(tech, start_hour + ts - 1, b)
                    avail_total += avail
                    # Also at zero capacity: the output's −curtailment_cost is
                    # part of the capacity constraint's dual
                    if charge_tech_curt
                        add_to_expression!(tech_curve_cost, cumul_cap * avail * input.curtailment_cost)
                        add_to_expression!(tech_curve_cost, -input.curtailment_cost,
                                           tech_output[tc, b, ts])
                    end
                end
                tech_avail_sum[(tc, b)] = avail_total
                if charge_tech_curt && avail_total > 0
                    tech_curt_grad[(tc, b)] = input.curtailment_cost * avail_total
                end
            end
        end
    end
    # The day's terms of the year's curtailment limit, per system
    if curt_limited
        constraint_store["curt_day"] = (
            system_day_curtailment(ps_vars, tech_output,
                (tc, b) -> get(fixed_caps.tech_capacity, (tc, b), 0.0),
                input, start_hour, hours),
            constraint_store["re_day"][1],
            re_cap_exempt_energy(input, re_cap_exempt, year_idx, start_hour, hours))
        constraint_store["tech_avail_sum"] = tech_avail_sum
    end

    # =========================================================================
    # Objective: operational cost (includes annualization scaling factor)
    # =========================================================================
    scaling_factor = rep_day_scaling(input, ndays)

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
        tech_output=tech_output, tech_day_cost=tech_curve_cost + remainder_premium, ndays)
    # The day's CO2 per budget group, for the year's budgets
    constraint_store["co2_day"] = day_co2_by_group(ps_vars, tech_output, input, hours)

    # M2 (#89): fuel block with the master's fuel capacities fixed by copy
    # constraints, whose duals feed the cut like the other copies
    fuel_cost, fuel_fix = benders_fuel_day!(sp_model, ps_vars, input, year_idx,
        start_hour, hours, tech_output, fixed_caps.fuel_extra)
    add_to_expression!(day_cost, fuel_cost, master_fuel_day_scale(input, ndays))
    merge!(copy_fix, fuel_fix)
    isempty(copy_fix) || (constraint_store["copy_fix"] = copy_fix)
    constraint_store["day_cost"] = day_cost

    if model === nothing
        # Built alone, the day is its year
        constraint_store["curt_limits"] = add_benders_curtailment_limits!(sp_model, input,
            year_idx, [constraint_store], [scaling_factor])
        @objective(sp_model, Min, day_cost)
    end

    return SubproblemData(
        sp_model, ps_vars,
        tech_output, bat_tech_discharge, bat_tech_charge, bat_tech_soc,
        constraint_store,
        year_idx, day_idx, hours, start_hour, scaling_factor
    )
end

"""
    create_benders_year_subproblem(input, year_idx, days, fixed_caps, vars, targets;
                                   re_cap_exempt=Float64[], share_years=Set())

The subproblem of year `year_idx`: one LP with each of its representative
days (first steps `days`, `create_benders_subproblem`) at the master's
capacities, and the annual constraints that couple them, as the monolithic
master has them (`add_representative_days_validation!`), on the days'
renewable generation `E_σ`, total generation `G_σ`, demand `D_σ` and
emissions, each scaled to the year:

- each CO2 budget: emissions ≤ budget + violation, priced at the group's
  penalty (`co2_budget_y{y}_{group}`);
- each system's RE target or floor: `E_σ + shortfall ≥ re_required_share × G_σ`,
  the shortfall at `re_shortfall_penalty` (`re_target_s{σ}_y{y}`);
- with a growth cap (`benders_re_capped`), the cap on the share over the
  year before (`add_re_growth_cap!`, its allowance `D_σ × ρ_σ,y-1` from a
  copy of the master's ratio of that year, or `re_share_anchor × G_σ`), and,
  for a year the next one is measured from (`share_years`),
  `E_σ + under ≥ ρ_σ,y × D_σ` with `ρ_σ,y` a copy of the master's
  renewable generation over demand (`re_energy_ratio`) and `under` at
  `re_cap_penalty` (`re_share_s{σ}_y{y}`).

The master's `ρ_σ,y` thus never exceeds the year's actual ratio without
paying at least as much as the room it gives the next year's cap is worth
(that room's own price is `re_cap_penalty`), and is no higher than the
actual one at the optimum, so the cap is never looser than the monolithic
one and both have the same optimum. Every coupling of the days is inside
the LP, so the master has nothing to share out among them. The objective is
the year's operating cost, scaled to the year and not discounted.
"""
function create_benders_year_subproblem(
    input::MasterProblemInput,
    year_idx::Int,
    days::Vector{Int},
    fixed_caps::FixedCapacities,
    master_vars::MasterProblemVariables,
    targets::Dict{Tuple{Int,Int}, Float64};
    re_cap_exempt::Vector{Float64} = Float64[],
    share_years::Set{Tuple{Int,Int}} = Set{Tuple{Int,Int}}()
)::BendersYearSubproblem
    timesteps_per_day = 24 ÷ input.temporal_resolution_hours
    # Each day had at most 300 s
    model = Model(benders_subproblem_optimizer(input;
        time_limit = 300.0 * max(length(days), 1)))
    day_sps = SubproblemData[]
    cost = AffExpr(0.0)
    for (day_idx, start_hour) in enumerate(days)
        end_hour = min(start_hour + timesteps_per_day - 1, size(input.base_demand, 1))
        end_hour >= start_hour || continue
        sp = create_benders_subproblem(input, year_idx, day_idx,
            input.base_demand[start_hour:end_hour, :], start_hour, fixed_caps, master_vars;
            re_cap_exempt, model, ndays = length(days))
        push!(day_sps, sp)
        add_to_expression!(cost, sp.constraint_store["day_cost"])
    end

    w = rep_day_scaling(input, length(days))
    store = Dict{String, Any}()
    copy_fix = Dict{BendersCopyKey, Any}()

    # The annual curtailment limit of each system
    store["curt_limits"] = add_benders_curtailment_limits!(model, input, year_idx,
        [sp.constraint_store for sp in day_sps], [sp.scaling_factor for sp in day_sps])

    # CO2 budgets (add_co2_budget_constraints!)
    for (k, grp) in enumerate(input.co2_budget_groups)
        has_co2_budget(grp) || continue
        emissions = AffExpr(0.0)
        for sp in day_sps
            add_to_expression!(emissions, sp.constraint_store["co2_day"][k], w)
        end
        viol = @variable(model, lower_bound = 0,
            base_name = "co2_budget_violation_y$(year_idx)_$(grp.name)")
        @constraint(model, emissions <= grp.budget + viol,
            base_name = "co2_budget_y$(year_idx)_$(grp.name)")
        add_to_expression!(cost, grp.violation_penalty, viol)
    end

    # RE target, floor and growth cap (add_re_target_constraints!)
    n_sys = length(master_systems(input))
    energy = [AffExpr(0.0) for _ in 1:n_sys]
    generation = [AffExpr(0.0) for _ in 1:n_sys]
    demand = zeros(n_sys)
    for sp in day_sps
        ren, gen, dem = sp.constraint_store["re_day"]
        for s in 1:n_sys
            add_to_expression!(energy[s], ren[s], w)
            add_to_expression!(generation[s], gen[s], w)
            demand[s] += w * dem[s]
        end
    end
    share_copy(s, y) = add_copy!(model, copy_fix, fixed_caps, input, (:re_energy, string(s), y))
    for s in 1:n_sys
        demand[s] > 0 || continue
        slack = @variable(model, lower_bound = 0, base_name = "slack_re_s$(s)_y$(year_idx)")
        @constraint(model,
            energy[s] + slack >=
                re_required_share(input, targets, s, year_idx) * generation[s],
            base_name = "re_target_s$(s)_y$(year_idx)")
        add_to_expression!(cost, re_shortfall_penalty(input), slack)
        benders_re_capped(input) || continue
        allowance = (s, year_idx - 1) in share_years ?
            demand[s] * share_copy(s, year_idx - 1) :
            re_cap_prev_allowance(re_share_anchor(input, s), generation[s], demand[s])
        add_to_expression!(cost,
            add_re_growth_cap!(model, input, s, year_idx, energy[s], generation[s], allowance))
        if (s, year_idx) in share_years
            under = @variable(model, lower_bound = 0,
                base_name = "re_share_short_s$(s)_y$(year_idx)")
            @constraint(model, energy[s] + under >= demand[s] * share_copy(s, year_idx),
                base_name = "re_share_s$(s)_y$(year_idx)")
            add_to_expression!(cost, re_cap_penalty(input), under)
        end
    end
    isempty(copy_fix) || (store["copy_fix"] = copy_fix)

    @objective(model, Min, cost)
    return BendersYearSubproblem(model, day_sps, store, year_idx)
end

"""
    add_benders_curtailment_limits!(model, input, year_idx, stores, scales) -> Vector

The annual curtailment limit (`add_curtailment_limit!`) of each system in a
year subproblem (or a day subproblem built alone), over its days' terms
(`stores[d]["curt_day"]`, scaled `scales[d]`), with a slack
`curt_excess_s{σ}_y{y}` fixed to 0 (`max_curt_s{σ}_y{y}`); a violation is
measured or priced by freeing it (`solve_benders_subproblem!`). Each day's
store gets the limits and the capacities' coefficients in them
(`"max_curt"`, for `extract_subproblem_duals`). Returns one record per
limit: its slack and constraint, system, the year's curtailment,
renewable generation and exemption over `scale` (the largest of `scales`,
the slack's MWh per MWh/year), for `benders_curt_limits`.
"""
function add_benders_curtailment_limits!(model::Model, input::MasterProblemInput,
                                         year_idx::Int, stores::Vector{Dict{String, Any}},
                                         scales::Vector{Float64})
    limits = NamedTuple[]
    input.max_curtailment_ratio < 1.0 || return limits
    year = CurtailmentYear()
    for (st, w) in zip(stores, scales)
        haskey(st, "curt_day") && add_period_curtailment!(year, st["curt_day"]..., w)
    end
    curt, re, exempt, smax = curtailment_year_sums(year)
    crefs = Dict{Int, ConstraintRef}()
    for s in eachindex(curt)
        isempty(re[s].terms) && continue
        x = @variable(model, lower_bound = 0, upper_bound = 0,
                      base_name = "curt_excess_s$(s)_y$(year_idx)")
        crefs[s] = @constraint(model,
            curt[s] <= input.max_curtailment_ratio * re[s] + exempt[s] + x,
            base_name = "max_curt_s$(s)_y$(year_idx)")
        push!(limits, (excess = x, cref = crefs[s], system = s, curt = curt[s],
                       re = re[s], exempt = exempt[s], scale = smax))
    end
    bus_sys = master_bus_systems(input)
    for (st, w) in zip(stores, scales)
        haskey(st, "tech_avail_sum") || continue
        st["max_curt"] = (crefs, Dict(k => a * w / smax for (k, a) in st["tech_avail_sum"]),
                          bus_sys)
    end
    return limits
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
    # Reserve headroom and dynamic capability of the technologies built
    # (master_unit_reserve_terms): Σ dual × coefficient of the capacity
    for (cref, coef, t, b) in get(sp.constraint_store, "tech_pow", ())
        tech_duals[(t, b)] = get(tech_duals, (t, b), 0.0) + dual(cref) * coef
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
    #   Σ(curt) + Σ(cap×avail - tech_output) ≤ ratio×renewable + exempt + slack
    # cap (master variable) appears on the LHS with coefficient Σ_ts(avail).
    # The gradient ∂Q/∂cap from this constraint = -dual(curt_cref) × Σ_ts(avail).
    # (negative because cap increases the LHS, tightening a ≤ constraint;
    #  JuMP dual for ≤ in minimization is ≤ 0, so -dual ≥ 0 = cost increase)
    # This term is CRITICAL: when something other than the capacity
    # constraint limits tech dispatch, the capacity dual is 0 but the
    # curtailment dual captures the marginal cost of over-investment.
    # The limit is the system's, annual (add_benders_curtailment_limits!):
    # `avail_sum` is the day's coefficient in it.
    if haskey(sp.constraint_store, "max_curt")
        crefs, tech_avail_sum, bus_sys = sp.constraint_store["max_curt"]
        curt_duals = Dict(s => dual(c) for (s, c) in crefs)  # ≤ 0 (≤, minimisation)
        for ((tc, b), avail_sum) in tech_avail_sum
            curt_dual = get(curt_duals, bus_sys[b], 0.0)
            abs(curt_dual) > 1e-10 || continue
            # -curt_dual × avail_sum is the positive gradient (cost increases with capacity)
            tech_duals[(tc, b)] = get(tech_duals, (tc, b), 0.0) - curt_dual * avail_sum
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
    build_year_cut(ysp, input, fixed_caps, ypp; cost_terms=true) -> BendersCut

The cut of a solved year subproblem: its objective and the sum over its
days of their capacity and copy duals (`extract_subproblem_duals`,
`extract_copy_duals`), plus the duals of the year's own copies (the RE
shares). The days share the year's capacities, so the gradient is the sum
of each day's.
"""
function build_year_cut(ysp::BendersYearSubproblem, input::MasterProblemInput,
                        fixed_caps::FixedCapacities, ypp::Int; cost_terms::Bool = true)
    td = Dict{Tuple{Int,Int}, Float64}()
    bpd = Dict{Tuple{Int,Int}, Float64}()
    bcd = Dict{Tuple{Int,Int}, Float64}()
    cd = extract_copy_duals(ysp)
    for sp in ysp.days
        t, p, c = extract_subproblem_duals(sp, input; cost_terms)
        mergewith!(+, td, t)
        mergewith!(+, bpd, p)
        mergewith!(+, bcd, c)
        mergewith!(+, cd, extract_copy_duals(sp))
    end
    return build_optimality_cut(objective_value(ysp.model), td, bpd, bcd, fixed_caps,
                                input, ysp.year_idx, ypp; fuel_duals = cd)
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
built from a subproblem's least curtailment violation (`solve_benders_subproblem!`):
the capacities must let the year meet its curtailment limits.
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
    curtailment_tolerance(re) -> Float64

Curtailment (MWh/year) above a system's limit that still counts as within
it, for its renewable generation `re` (MWh/year): `CURTAILMENT_LIMIT_ATOL` +
`CURTAILMENT_LIMIT_RTOL` × `re`.
"""
curtailment_tolerance(re::Real) = CURTAILMENT_LIMIT_ATOL + CURTAILMENT_LIMIT_RTOL * max(re, 0.0)

"""
    benders_curt_limits(sp) -> Vector

The annual curtailment limits (`add_benders_curtailment_limits!`) of a year
subproblem, or of a day subproblem built alone.
"""
benders_curt_limits(sp::SubproblemData) = get(sp.constraint_store, "curt_limits", NamedTuple[])
benders_curt_limits(ysp::BendersYearSubproblem) =
    get(ysp.constraint_store, "curt_limits", NamedTuple[])

"""
    benders_curt_slacks(sp) -> Vector{VariableRef}

The curtailment-limit slacks (`curt_excess_*`, one per system) of a year
subproblem, or of a day subproblem built alone.
"""
benders_curt_slacks(sp::Union{SubproblemData, BendersYearSubproblem}) =
    VariableRef[l.excess for l in benders_curt_limits(sp)]

"""
    benders_curt_violated(sp) -> Bool

True when the solution of `sp` curtails more than a system's limit allows,
beyond `curtailment_tolerance`.
"""
benders_curt_violated(sp::Union{SubproblemData, BendersYearSubproblem}) =
    any(l -> l.scale * value(l.excess) > curtailment_tolerance(l.scale * value(l.re)),
        benders_curt_limits(sp))

# Cost term of a subproblem variable, from its name without the indices,
# year, day and system (`benders_cost_terms`)
const BENDERS_TERM_LABELS = Dict(
    "gen" => "generators", "gseg" => "generators", "tech_out" => "technologies",
    "tech_seg" => "technologies", "ll" => "load shed", "lol" => "load shed",
    "slack_re" => "RE target shortfall", "slack_re_cap" => "RE growth cap excess",
    "re_share_short" => "renewable generation below the master's",
    "curt" => "curtailment", "rs_loss" => "reserve shortfall",
    "rd_loss" => "reserve shortfall", "inertia_loss" => "inertia shortfall",
    "co2_budget_violation" => "CO2 budget violation",
    "curt_excess" => "curtailment above the limit",
    "bat_ch" => "batteries", "bat_dch" => "batteries",
    "bat_tech_ch" => "batteries", "bat_tech_dis" => "batteries")

function benders_term_label(v::VariableRef)
    n = replace(name(v), r"\[.*$" => "", r"^op_y\d+_d\d+_" => "", r"_y\d+_d\d+" => "",
                r"_s\d+_y\d+" => "", r"_y\d+" => "", r"(_[A-Za-z]*\d+)+$" => "")
    for p in ("lol", "tech_seg", "co2_budget_violation")
        startswith(n, p) && (n = p)
    end
    return get(BENDERS_TERM_LABELS, n, isempty(n) ? "other" : n)
end

"""
    benders_cost_terms(model) -> Dict{String, Float64}

A solved subproblem's objective split by kind of cost (generators, load
shed, RE target shortfall, …, `BENDERS_TERM_LABELS`; others by variable
name), for the iteration log. `"technologies"` includes the curtailment
cost of the candidates (the objective's constant, their curtailment at full
output, less their output's credit for it); `"curtailment"` is that of the
existing units.
"""
function benders_cost_terms(model::Model)
    obj = objective_function(model)
    out = Dict{String, Float64}()
    obj isa AffExpr || return out
    iszero(obj.constant) || (out["technologies"] = obj.constant)
    for (v, c) in obj.terms
        x = c * value(v)
        iszero(x) && continue
        k = benders_term_label(v)
        out[k] = get(out, k, 0.0) + x
    end
    return out
end

"""
    benders_curtailment_report(ysp, ratio) -> String

Why a year subproblem cannot meet its curtailment limit, at its least
violation (`solve_benders_subproblem!`): in how many systems, and the
year's curtailment against the limit (`max_curtailment_ratio` × the
renewable energy used, plus what the growth cap lets it curtail), in
GWh/year, summed over the systems.
"""
function benders_curtailment_report(ysp::BendersYearSubproblem, ratio::Float64)
    curt = re = exempt = excess = 0.0
    broken = 0
    limits = benders_curt_limits(ysp)
    for l in limits
        k = l.scale / 1000.0   # GWh/year
        x = value(l.excess)
        curt += k * value(l.curt)
        re += k * value(l.re)
        exempt += k * l.exempt
        excess += k * x
        l.scale * x > curtailment_tolerance(l.scale * value(l.re)) && (broken += 1)
    end
    f(x) = round(x, digits = 1)
    return "breaks the annual curtailment limit" *
           (length(limits) > 1 ? " in $(broken) of $(length(limits)) systems" : "") * ": " *
           "curtails $(f(curt)) GWh/yr, limit $(f(ratio * re + exempt)) GWh/yr " *
           "($(round(100 * ratio, digits = 2)) % of $(f(re)) GWh/yr of RE used" *
           (exempt > 0 ? " + $(f(exempt)) exempt by the growth cap" : "") *
           "), $(f(excess)) GWh/yr above it"
end

"""
    benders_curtailment_penalty(input) -> Float64

Price per MWh (scaled to the year) of curtailment above the limit in the
priced subproblem (`solve_benders_subproblem!`): `re_cap_penalty`, the
value of lost load. Making room for renewable output costs far less (fuel
displaced, battery losses), so a plan that can meet the limit does.
"""
benders_curtailment_penalty(input::MasterProblemInput) = re_cap_penalty(input)

# The curtailment-limit slacks of `sp` with their MWh/year per unit
benders_weighted_slacks(sp::Union{SubproblemData, BendersYearSubproblem}) =
    Tuple{VariableRef, Float64}[(l.excess, l.scale) for l in benders_curt_limits(sp)]

"""
    solve_benders_subproblem!(sp; penalty=0.0) -> Symbol

Solve subproblem `sp` (a year's, or a day built alone). With `penalty` > 0
its curtailment limits are priced: their slacks are freed and each MWh above
a limit (scaled to the year) costs `penalty` (`benders_curtailment_penalty`).
That LP's cost is never above the subproblem's with hard limits and equals
it when no slack is used, so its cut bounds the year's cost at every plan;
returns `:optimal` when the limits hold, otherwise `:infeasible` with `sp`
holding the priced solution (`benders_least_violation!` then gives the
feasibility cut). With `penalty` = 0 the limits are hard and an infeasible
`sp` holds its least violation (`:infeasible`). `:failed` when the solver
fails, also after a retry with HiGHS.
"""
function solve_benders_subproblem!(sp::Union{SubproblemData, BendersYearSubproblem};
                                   penalty::Float64 = 0.0)
    solved(m) = termination_status(m) in (MOI.OPTIMAL, MOI.LOCALLY_SOLVED)
    slacks = benders_weighted_slacks(sp)
    priced = penalty > 0 && !isempty(slacks)
    violated() = benders_curt_violated(sp)
    if priced
        cost = objective_function(sp.model)
        for (x, w) in slacks
            delete_upper_bound(x)
            add_to_expression!(cost, penalty * w, x)
        end
        @objective(sp.model, Min, cost)
    end
    optimize!(sp.model)
    solved(sp.model) && return priced && violated() ? :infeasible : :optimal

    if !priced && !isempty(slacks) &&
       termination_status(sp.model) in (MOI.INFEASIBLE, MOI.INFEASIBLE_OR_UNBOUNDED)
        cost = objective_function(sp.model)
        benders_least_violation!(sp) && return :infeasible
        benders_hold_slacks!(sp)
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
    solved(sp.model) || return :failed
    return priced && violated() ? :infeasible : :optimal
end

"""
    benders_hold_slacks!(sp)

Make the curtailment limits of `sp` hard again after its least violation
(`benders_least_violation!`) found them met: each slack is bounded by the
value it had there (within `curtailment_tolerance`), or 0 when that solve
failed, so the limits hold as far as they can.
"""
function benders_hold_slacks!(sp::Union{SubproblemData, BendersYearSubproblem})
    ok = termination_status(sp.model) in (MOI.OPTIMAL, MOI.LOCALLY_SOLVED) &&
         primal_status(sp.model) == MOI.FEASIBLE_POINT
    for x in benders_curt_slacks(sp)
        set_upper_bound(x, ok ? max(value(x), 0.0) : 0.0)
    end
    return nothing
end

"""
    benders_solve_hard!(sp, cost) -> Bool

Re-solve `sp` with its curtailment limits hard (`benders_hold_slacks!`) and
objective `cost`. True when it solved with duals.
"""
function benders_solve_hard!(sp::Union{SubproblemData, BendersYearSubproblem}, cost)
    benders_hold_slacks!(sp)
    @objective(sp.model, Min, cost)
    optimize!(sp.model)
    return termination_status(sp.model) in (MOI.OPTIMAL, MOI.LOCALLY_SOLVED) &&
           has_duals(sp.model)
end

"""
    benders_least_violation!(sp) -> Bool

Re-solve `sp` for its least curtailment above the limits (their slacks
freed, their sum minimised), whose duals give a feasibility cut. True when
it solved with a violation left beyond `curtailment_tolerance`.
"""
function benders_least_violation!(sp::Union{SubproblemData, BendersYearSubproblem})
    slacks = benders_curt_slacks(sp)
    isempty(slacks) && return false
    foreach(x -> has_upper_bound(x) && delete_upper_bound(x), slacks)
    @objective(sp.model, Min, sum(slacks))
    optimize!(sp.model)
    return termination_status(sp.model) in (MOI.OPTIMAL, MOI.LOCALLY_SOLVED) &&
           has_duals(sp.model) && benders_curt_violated(sp)
end

"""Termination statuses of a solver stopped by a resource limit."""
const LIMIT_STATUSES = (MOI.TIME_LIMIT, MOI.NODE_LIMIT, MOI.ITERATION_LIMIT,
                        MOI.SOLUTION_LIMIT, MOI.OBJECTIVE_LIMIT, MOI.OTHER_LIMIT,
                        MOI.MEMORY_LIMIT)

"""
    limit_with_feasible_point(model) -> Bool

True when the solver stopped `model` at a resource limit (`LIMIT_STATUSES`)
and holds a feasible primal point: a valid plan, not proven optimal.
"""
limit_with_feasible_point(model::Model) =
    termination_status(model) in LIMIT_STATUSES &&
    primal_status(model) == MOI.FEASIBLE_POINT

"""
    limited_master_lower_bound(model, lb_history) -> Float64

Lower bound of a Benders iteration whose master stopped at a limit: its
objective is no bound. A proven bound is used: the objective of a feasible
dual point (an LP, by weak duality) or the solver's MIP bound
(`objective_bound`, for a master with integer variables); else, and when
it is lower, the previous iteration's lower bound (the master only gains
cuts, so it stays valid); else `-Inf`.
"""
function limited_master_lower_bound(model::Model, lb_history::Vector{Float64})
    previous = isempty(lb_history) ? -Inf : lb_history[end]
    bound = try
        if dual_status(model) == MOI.FEASIBLE_POINT
            dual_objective_value(model)
        elseif num_constraints(model, VariableRef, MOI.Integer) +
               num_constraints(model, VariableRef, MOI.ZeroOne) > 0
            objective_bound(model)
        else
            -Inf
        end
    catch
        -Inf
    end
    return isfinite(bound) ? max(bound, previous) : previous
end

"""
    log_benders_years(diag, theta, year_costs, discount, base_year)

Log an iteration's years: at INFO how many solved within the curtailment
limit, each one that did not and why, and the operating cost of those that
did by term (discounted, M\$); at DEBUG each year's θ, cost and terms.
`diag` is what `evaluate_year!` recorded per year.
"""
function log_benders_years(diag::AbstractDict, theta::AbstractDict,
                           year_costs::Vector{Float64}, discount::Function, base_year::Int)
    years = sort(collect(keys(diag)))
    kind(y) = diag[y][1]
    label(y) = "y$(y) ($(base_year + y - 1))"
    infeasible = filter(y -> kind(y) === :infeasible, years)
    failed = filter(y -> kind(y) === :failed, years)
    @info "  Years: $(length(years) - length(infeasible) - length(failed)) of " *
          "$(length(years)) within the curtailment limit" *
          (isempty(infeasible) ? "" : ", $(length(infeasible)) not (feasibility cuts: " *
                                      join(label.(infeasible), ", ") * ")") *
          (isempty(failed) ? "" : ", $(length(failed)) failed: " * join(label.(failed), ", "))
    for y in vcat(infeasible, failed)
        @info "    $(label(y)) $(diag[y][3])"
    end
    total = Dict{String, Float64}()
    fmt(terms) = join(("$(k) $(round(v, sigdigits = 4))"
                       for (k, v) in sort(collect(terms); by = kv -> -abs(kv[2]))
                       if abs(v) > 1e-9), ", ")
    for y in years
        mergewith!(+, total, Dict(k => discount(y) * v for (k, v) in diag[y][2]))
    end
    isempty(total) || @info "  Operating cost by term (discounted M\$" *
        (isempty(infeasible) ? "" : "; curtailment above the limit priced at VOLL") *
        "): " * fmt(total)
    for y in years
        @debug "    $(label(y)): θ=$(round(get(theta, y, 0.0), sigdigits = 4)), " *
               "Q=$(round(year_costs[y], sigdigits = 4)), " *
               "disc=$(round(discount(y), sigdigits = 4)), $(kind(y)); $(fmt(diag[y][2]))"
    end
    return nothing
end

"""
    log_benders_plan(vars, input)

Log at DEBUG what the solved master builds each year: MW of each
technology, MW / MWh of battery technologies, the budget slack and the RE
shares the subproblems copy.
"""
function log_benders_plan(vars::MasterProblemVariables, input::MasterProblemInput)
    r(x) = round(x, sigdigits = 4)
    total(d, i) = haskey(d, i) ? sum(value(v) for v in d[i] if v !== nothing; init = 0.0) : 0.0
    for y in sort(collect(keys(vars.tech_investment)))
        builds = ["$(t.name) $(r(total(vars.tech_investment[y], i))) MW"
                  for (i, t) in enumerate(input.technologies)]
        for (i, bt) in enumerate(input.battery_technologies)
            push!(builds, "$(bt.name) $(r(total(vars.bat_tech_power_investment[y], i))) MW / " *
                          "$(r(total(vars.bat_tech_capacity_investment[y], i))) MWh")
        end
        slack = haskey(vars.slack_budget, y) ? value(vars.slack_budget[y]) : 0.0
        cref = haskey(vars.slack_budget, y) ?
            constraint_by_name(owner_model(vars.slack_budget[y]), "budget_y$(y)") : nothing
        # The constraint holds capex − slack ≤ budget
        capex = cref === nothing ? "" : ", capex $(r(value(cref) + slack))"
        shares = ["$(r(value(v)))" for ((s, yy), v) in vars.re_energy_ratio
                  if yy == y && !is_fixed(v)]
        @debug "    plan y$(y): " * join(builds, ", ") * capex * " (budget " *
               "$(r(input.max_annual_investment)), slack $(r(slack)))" *
               (isempty(shares) ? "" : ", RE generation / demand " * join(shares, "/"))
    end
    return nothing
end

"""
    recover_benders_plan!(model, values, optimizer) -> Bool

Solve the Benders master `model` with its variables fixed to `values` (a
plan solved earlier), to extract it. The plan met the curtailment limits
within `curtailment_tolerance`, and the cuts and the master's constraints
hold it to the solver's tolerances only: Cuba's master, fixed to the plan
of iteration 10 after 35, was infeasible in presolve and the run aborted
with no plan. If it does not solve, the feasibility cuts
(`benders_feas_*`) are deleted and the variables held within a relative
1e-6 of `values`. True when the master then holds a plan.
"""
function recover_benders_plan!(model::Model, values::AbstractDict{VariableRef, Float64},
                               optimizer)
    target(v, x) = is_integer(v) || is_binary(v) ? round(x) : x
    for (v, x) in values
        fix(v, target(v, x); force = true)
    end
    set_optimizer(model, optimizer)
    optimize!(model)
    primal_status(model) == MOI.FEASIBLE_POINT && return true
    @warn "Benders: the master fixed to the best plan is $(termination_status(model)); " *
          "solving it without the feasibility cuts, within 1e-6 of the plan."
    for (F, S) in list_of_constraint_types(model)
        F <: GenericAffExpr || continue
        for c in all_constraints(model, F, S)
            startswith(name(c), "benders_feas_") && delete(model, c)
        end
    end
    for (v, x) in values
        x = target(v, x)
        d = 1e-6 * max(1.0, abs(x))
        unfix(v)
        set_lower_bound(v, x - d)
        set_upper_bound(v, x + d)
    end
    set_optimizer(model, optimizer)
    optimize!(model)
    return primal_status(model) == MOI.FEASIBLE_POINT
end

# =============================================================================
# Main Benders Decomposition Loop
# =============================================================================

"""
    run_benders_decomposition(input; kwargs...)

Run Benders decomposition to solve the master problem.
Separates investment (master) from operational (subproblems) decisions.

Each year has its own recourse variable and gets its own cut every
iteration, from one subproblem holding all its representative days
(`create_benders_year_subproblem`). A year with a day whose curtailment
limit the master's capacities cannot meet adds a feasibility cut instead,
and that iteration gives no upper bound. The returned plan and operating costs are
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
- `debug_lp_dir::String=""`: directory where the master and subproblem LPs
  that fail in the first five iterations are written
  (`benders_master_failed_iter*.lp`, `benders_sp_failed_y*_iter*.lp`);
  empty (the default) writes none

# Returns
- `BendersResult`: Solution with convergence history
"""
function run_benders_decomposition(
    input::MasterProblemInput;
    max_iterations::Int = 50,
    tolerance::Float64 = 1e-4,
    use_representative_days::Bool = true,
    verbose_benders::Bool = true,
    record_unit_operation::Bool = false,
    debug_lp_dir::String = ""
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
    warn_fewer_rep_days(input, maximum(length, values(rep_days_by_year); init = 0))
    master_model, vars, targets, theta, inv_cost_expr =
        create_benders_master(input; rep_days_by_year)
    ypp = vars.years_per_inv_period
    cap_exempt = benders_cap_exemption(input, targets, rep_days_by_year)

    # Master side of the copy variables: the line ends
    ctx = BendersCopyContext(benders_line_ends(input))
    # (system, year) whose renewable generation the next year's growth cap reads
    share_years = benders_re_share_years(input, rep_days_by_year)

    # Solve year `y_idx`'s subproblem (all its representative days) at
    # capacities `caps` and queue its cut in `cuts` as (kind, year, cut).
    # Returns the year's operating cost, whether it solved, and whether it
    # met its annual curtailment limits (within `curtailment_tolerance`).
    # With `op`, each day's operation of the existing units is added to it
    # (their NPV); with `re`, each system's renewable and total generation
    # scaled to the year (its achieved RE share).
    # With `diag`, the year's outcome for the iteration log: (:optimal |
    # :infeasible | :failed, cost by term, why it broke the limit or failed).
    # The year is solved with its curtailment limits priced; one that breaks
    # them there adds that cut (it bounds the year's cost at every plan) and,
    # when even its least violation breaks them, costs what the priced LP
    # does and adds a feasibility cut; otherwise it is solved again with the
    # limits hard (meeting them costs more than the penalty, which then is
    # no exact price) and counts as within them.
    function evaluate_year!(cuts, y_idx, caps, iter; op = nothing, re = nothing,
                            diag = nothing)
        caps = benders_battery_floor(caps, input)
        ysp = create_benders_year_subproblem(input, y_idx, rep_days_by_year[y_idx], caps,
            vars, targets; re_cap_exempt = get(cap_exempt, y_idx, Float64[]), share_years)
        cost = objective_function(ysp.model)
        function failed()
            diag === nothing || (diag[y_idx] = (:failed, Dict{String, Float64}(),
                                                string(termination_status(ysp.model))))
            @warn "Benders: Subproblem of year $(y_idx) failed after retry: " *
                  "$(termination_status(ysp.model))"
            # Save the first failing LPs for diagnosis (debug runs)
            if !isempty(debug_lp_dir) && iter <= 5
                lp = joinpath(debug_lp_dir, "benders_sp_failed_y$(y_idx)_iter$(iter).lp")
                try
                    write_to_file(ysp.model, lp)
                    @info "  Debug LP written to $(lp)"
                catch; end
            end
            return 0.0, false, true
        end
        status = solve_benders_subproblem!(ysp; penalty = benders_curtailment_penalty(input))
        (status === :failed || !has_duals(ysp.model)) && return failed()
        if status === :infeasible
            year_cost = objective_value(ysp.model)
            push!(cuts, (:priced, y_idx, build_year_cut(ysp, input, caps, ypp)))
            terms = benders_cost_terms(ysp.model)
            why = benders_curtailment_report(ysp, input.max_curtailment_ratio)
            if benders_least_violation!(ysp)
                push!(cuts, (:feasibility, y_idx,
                             build_year_cut(ysp, input, caps, ypp; cost_terms = false)))
                diag === nothing || (diag[y_idx] = (:infeasible, terms, why))
                return year_cost, true, false
            end
            benders_solve_hard!(ysp, cost) || return failed()
        end
        push!(cuts, (:optimality, y_idx, build_year_cut(ysp, input, caps, ypp)))
        diag === nothing || (diag[y_idx] = (:optimal, benders_cost_terms(ysp.model), ""))
        for sp in ysp.days
            op === nothing || add_unit_day_operation!(op, sp.ps_vars, input, y_idx,
                sp.hours, sp.scaling_factor,
                get(get(ysp.model.ext, :master_startup, IdDict()), sp.ps_vars, nothing))
            re === nothing && continue
            renewable, generation, _ = sp.constraint_store["re_day"]
            for s in eachindex(generation)
                e0, g0 = get(re, (s, y_idx), (0.0, 0.0))
                re[(s, y_idx)] = (e0 + sp.scaling_factor * value(renewable[s]),
                                  g0 + sp.scaling_factor * value(generation[s]))
            end
        end
        return objective_value(ysp.model), true, true
    end

    function add_cuts!(cuts, iter)
        for (kind, y_idx, cut) in cuts
            if kind === :feasibility
                add_feasibility_cut!(master_model, cut, y_idx, vars, input,
                    "benders_feas_y$(y_idx)_iter$(iter)"; ctx)
            else
                add_optimality_cut!(master_model, theta[y_idx], cut, y_idx, vars, input,
                    (kind === :priced ? "benders_cut_priced_y" : "benders_cut_y") *
                    "$(y_idx)_iter$(iter)"; ctx)
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
    cuts = Tuple{Symbol, Int, BendersCut}[]

    for y_idx in 1:num_years
        isempty(rep_days_by_year[y_idx]) && continue
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
        # The renewable-generation ratios of this year and the year before
        # at their targets
        warm_copies = Dict{BendersCopyKey, Float64}(
            (:re_energy, string(s), y) => get(targets, (s, y), 0.0)
            for s in 1:n_sys_ws for y in (y_idx - 1, y_idx) if y >= 1)
        warm_caps = FixedCapacities(
            warm_gen_cap, warm_bat_pow, warm_bat_en,
            warm_tech_cap, warm_bt_pow, warm_bt_en,
            Dict{Int, Float64}(),  # line capacity (handled in subproblem)
            warm_re_ratios, warm_copies
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
                Dict{Int, Float64}(), warm_re_ratios, warm_copies
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
    # Each system's renewable and total generation per year in the subproblems
    new_re() = Dict{Tuple{Int,Int}, Tuple{Float64, Float64}}()
    iter_re = new_re()
    best_re = iter_re
    theta_vars = Set(values(theta))
    # In-out stabilisation: the subproblems are solved at a point between
    # the master's plan and a core point, whose cuts are as valid; after an
    # iteration that did not raise the lower bound, once the gap is below
    # BENDERS_INOUT_GAP (or with integer master variables), or before any
    # point met the curtailment limits, at the master's plan. The core is
    # the last point solved within the limits: the plans that meet them are
    # a convex set, so a plan within them gives a point within them, and an
    # upper bound (with a core above them it may not, and the bound stalled)
    continuous_master = num_constraints(master_model, VariableRef, MOI.Integer) +
                        num_constraints(master_model, VariableRef, MOI.ZeroOne) == 0
    core = Dict{VariableRef, Float64}()
    best_at_master = true
    last_gap = Inf
    # Converged at a point between two plans: solve the master's plan once
    # more, so that the plan returned is one the master chose
    confirming = false

    # Create optimizer factory for resetting between iterations
    new_master_optimizer(limit) = create_optimizer(
        solver_name=input.solver_name,
        threads=input.threads,
        time_limit=limit,
        gap=input.gap,
        verbose=input.verbose
    )
    master_optimizer = new_master_optimizer(
        capped_time_limit(input.time_limit, BENDERS_MASTER_TIME_LIMIT))

    # The best iteration's plan: the master as solved if that is the last
    # solve, otherwise re-solved with its variables fixed to the best values
    # (`recover_benders_plan!`)
    # With record_unit_operation, the plan's unit operation and asset costs
    unit_ops(op) = op === nothing ? UnitOperation[] :
        [add_unit_asset_costs!(op, vars, input, nothing)]
    function best_solution(last_solved_iter)
        if best_iter == 0
            @warn "Benders: no iteration solved every subproblem within the " *
                  "curtailment limit; returning the last master solution."
            set_optimizer(master_model, master_optimizer)
            optimize!(master_model)
            has_values(master_model) || error("Benders: the master has no plan to " *
                "return: $(termination_status(master_model)).")
            return extract_master_solution_from_benders(master_model, vars, input,
                                                        year_operational_costs;
                                                        unit_operation = unit_ops(iter_op),
                                                        re_energy = iter_re)
        end
        if best_iter != last_solved_iter || !best_at_master
            recover_benders_plan!(master_model, best_values, master_optimizer) ||
                error("Benders: the plan of iteration $(best_iter) (the best upper " *
                      "bound) could not be recovered from the master: " *
                      "$(termination_status(master_model)).")
        end
        return extract_master_solution_from_benders(master_model, vars, input, best_year_ops;
                                                    objective = best_ub,
                                                    unit_operation = unit_ops(best_op),
                                                    re_energy = best_re)
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
        master_solved = master_status == MOI.OPTIMAL || master_status == MOI.LOCALLY_SOLVED
        if !master_solved && master_status in LIMIT_STATUSES &&
           !limit_with_feasible_point(master_model) &&
           (!has_time_limit(input.time_limit) ||
            input.time_limit > BENDERS_MASTER_TIME_LIMIT)
            # The cuts make the master larger every iteration (Cuba: 27M
            # nonzeros at iteration 36, 600 s did not do)
            limit_text = has_time_limit(input.time_limit) ?
                "$(input.time_limit) s" : "0 = no limit"
            @warn "Benders: the master stopped at $(master_status) at iteration " *
                  "$(iter) without a plan; solving it again, and the next ones, " *
                  "with solver.time_limit ($(limit_text))."
            master_optimizer = new_master_optimizer(input.time_limit)
            set_optimizer(master_model, master_optimizer)
            optimize!(master_model)
            master_status = termination_status(master_model)
            master_solved = master_status == MOI.OPTIMAL || master_status == MOI.LOCALLY_SOLVED
        end
        if !master_solved && limit_with_feasible_point(master_model)
            # A master stopped at a limit (time, iterations…) still holds a
            # feasible plan: evaluate it, but its objective is no lower bound
            @warn "Benders: the master stopped at $(master_status) at iteration " *
                  "$(iter) with a feasible plan; using it (the lower bound is the " *
                  "solver's bound or the previous one). Raise solver.time_limit " *
                  "to solve it to optimality."
        elseif !master_solved
            @warn "Benders: Master solve failed at iteration $(iter): $(master_status)"
            if !isempty(debug_lp_dir) && iter <= 5
                lp = joinpath(debug_lp_dir, "benders_master_failed_iter$(iter).lp")
                try
                    write_to_file(master_model, lp)
                    @info "  Debug LP written to $(lp)"
                catch; end
            end
            break
        end
        iterations = iter

        lb = master_solved ? objective_value(master_model) :
             limited_master_lower_bound(master_model, lb_history)
        push!(lb_history, lb)

        # ----- Evaluate investment cost at current solution -----
        inv_cost_val = value(inv_cost_expr)

        # ----- The point the subproblems are solved at -----
        stalled = length(lb_history) >= 2 &&
                  lb_history[end] - lb_history[end - 1] <= 1e-6 * max(1.0, abs(lb))
        weight = isempty(core) || stalled || !continuous_master || confirming ||
                 last_gap <= BENDERS_INOUT_GAP ? 1.0 : BENDERS_INOUT_WEIGHT
        point = Dict{VariableRef, Float64}(v => value(v) for v in all_variables(master_model))
        if weight < 1.0
            for (v, x) in point
                point[v] = weight * x + (1.0 - weight) * get(core, v, x)
            end
        end
        val = v -> point[v]

        if verbose_benders
            # Log theta values for diagnostics
            theta_sum_actual = sum(value(t) * COST_SCALE for t in values(theta); init = 0.0)
            @info "Benders iteration $(iter): LB = $(round(lb, digits=2)), " *
                  "inv_cost = $(round(inv_cost_val, digits=0)), " *
                  "Σθ(actual\$) = $(round(theta_sum_actual, sigdigits=6))"
            log_benders_plan(vars, input)
        end

        # ----- Solve subproblems for each year -----
        total_operational_cost = 0.0
        year_operational_costs = zeros(num_years)
        all_subproblems_solved = true
        iter_op = new_op()
        iter_re = new_re()
        year_diag = Dict{Int, Any}()

        for y_idx in 1:num_years
            isempty(rep_days_by_year[y_idx]) && continue
            # Evaluate fixed capacities from master solution
            fixed_caps = evaluate_fixed_capacities(vars, input, y_idx; ctx, val)
            year_cost, solved, feasible = evaluate_year!(cuts, y_idx, fixed_caps, iter;
                                                         op = iter_op, re = iter_re,
                                                         diag = year_diag)
            all_subproblems_solved &= solved && feasible
            year_operational_costs[y_idx] = year_cost
            total_operational_cost += discount(y_idx) * year_cost
        end
        all_subproblems_solved && (core = point)

        # ----- Compute upper bound -----
        # UB = first-stage cost (investment + life ext + decomm + slack) + actual operational cost
        # The master objective includes θ × COST_SCALE × discount, so subtract
        # that; both at the point solved
        theta_contribution = sum(val(t) * COST_SCALE * discount(y)
                                 for (y, t) in theta; init = 0.0)
        master_first_stage = value(val, objective_function(master_model)) - theta_contribution
        ub = master_first_stage + total_operational_cost
        # Only an iteration whose subproblems all solved within the
        # curtailment limit gives an upper bound
        # (the master's plan confirming a converged point is taken when it
        # is within the tolerance too)
        if all_subproblems_solved &&
           (ub < best_ub || (confirming && ub - lb <= tolerance * max(abs(ub), 1e-10)))
            best_ub = ub
            best_iter = iter
            best_year_ops = copy(year_operational_costs)
            best_op = iter_op
            best_re = iter_re
            best_values = Dict(v => x for (v, x) in point if !(v in theta_vars))
            best_at_master = weight == 1.0
        end
        push!(ub_history, best_ub)

        # ----- Check convergence -----
        gap = isfinite(best_ub) ? abs(best_ub - lb) / max(abs(best_ub), 1e-10) : Inf
        last_gap = gap
        if verbose_benders
            @info "  UB = $(round(best_ub, digits=2)), gap = $(round(gap*100, digits=4))%, " *
                  "first_stage = $(round(master_first_stage, digits=0)), " *
                  "total_ops = $(round(total_operational_cost, digits=0))" *
                  (weight < 1.0 ? " (solved between the master plan and the previous point)" : "")
            log_benders_years(year_diag, Dict(y => value(t) * COST_SCALE for (y, t) in theta),
                              year_operational_costs, discount, input.base_year)
        end

        if gap < tolerance && !best_at_master && !confirming && iter < max_iterations
            confirming = true
        elseif gap < tolerance
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
            "investment budget. Set ESFEX_BENDERS_DEBUG_LP=1 to write the " *
            "master LP (benders_master_failed_iter1.lp) to the run's logs folder."
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
                                         objective=nothing, unit_operation=UnitOperation[],
                                         re_energy=nothing)

Extract a MasterProblemResult from the Benders master solution.
Reuses the logic from extract_master_solution but provides operational costs
from subproblem solves instead of embedded variables, and `objective` (the
plan's upper bound) instead of the master's objective when given.
`unit_operation` is the existing units' operation in the plan's subproblems,
`re_energy` each system's renewable and total generation per year in them
(the achieved RE share, `master_re_shares`).
"""
function extract_master_solution_from_benders(
    model::Model,
    vars::MasterProblemVariables,
    input::MasterProblemInput,
    year_operational_costs::Vector{Float64};
    objective::Union{Nothing, Float64} = nothing,
    unit_operation::Vector{UnitOperation} = UnitOperation[],
    re_energy::Union{Nothing, AbstractDict} = nothing
)::MasterProblemResult
    # Use existing extraction (it handles investment vars, cumulative caps, etc.)
    result = extract_master_solution(model, vars, input)
    re_by_year, re_by_system = re_energy === nothing || isempty(re_energy) ?
        (result.re_penetration_by_year, result.re_penetration_by_system) :
        master_re_shares(input, re_energy)

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
        re_by_year,
        re_by_system,
        result.cumulative_gen_capacity,
        result.cumulative_bat_capacity,
        result.cumulative_bat_power,
        result.cumulative_tech_capacity,
        result.cumulative_bat_tech_power,
        result.cumulative_bat_tech_capacity,
        result.reservoir_investment,
        result.converter_investment,
        result.fuel_investment,  # M2 (#89)
        unit_operation,
        result.annuity_cost_by_year,
        result.annuity_streams
    )
end
