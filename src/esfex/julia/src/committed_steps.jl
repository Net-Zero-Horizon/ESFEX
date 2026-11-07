"""
Committed part of a rolling-horizon window.

A rolling-horizon window solves `hours` steps but keeps only its first `keep`
steps: the overlap tail is solved again by the next window. The year's
totals add up the kept steps of every window, so the window's costs and
energies must be valued over those steps only (prorating the whole window by
`keep / hours` mixed the overlap hours in).
"""

using JuMP: VariableRef, GenericAffExpr, GenericQuadExpr, name

"""
    CommittedSteps

Share of each variable's value that belongs to the first `keep` of a
window's `hours` steps:

- a variable of a step: 1 for a kept step, 0 for a step of the overlap tail;
- a state level (`hours + 1` columns: battery SOC, fuel tank, reservoir,
  EV): 1 up to step `keep`, 0 after it; the end-of-window level stands for
  the level after step `keep` (`end_level`), so a start-minus-end valuation
  (fuel drawn from a tank) covers the kept steps;
- a primary-energy period variable: the kept fraction of the period's steps
  (the period's quantity is spread evenly over them);
- a variable with no step (investments, budget slacks) and every constant:
  `keep / hours`, as a share of the window.

A variable outside the known containers whose name ends in `_t<step>` (the
N-1 slacks) counts as a variable of that step.
"""
struct CommittedSteps
    keep::Int
    hours::Int
    share::Dict{VariableRef, Float64}
    end_level::Dict{VariableRef, VariableRef}
end

# Containers of PowerSystemVariables whose last index is the step.
const _STEP_INDEXED_PS_FIELDS = (
    :gen_output, :gen_status, :gen_startup, :gen_shutdown,
    :curtailment, :fre_penetration_loss, :co2_emissions,
    :bat_charge, :bat_discharge, :bat_soc, :bat_charge_status, :soc_violation, :bat_spillage,
    :power_flow, :power_flow_by_line, :voltage_angle, :transfer_margin,
    :reserve_static, :reserve_dynamic, :reserve_static_loss, :reserve_dynamic_loss,
    :load_shed, :ev_charging, :ev_v2g, :ev_soc, :ev_loss, :ev_charge_status,
    :electrolyzer_power, :loss_of_inertia,
    :loss_of_load_sectoral, :flexible_demand_curtailed, :rooftop_curtailment,
    :acdc_rectify, :acdc_invert, :freq_flow_a_to_b, :freq_flow_b_to_a,
    :reservoir_level, :reservoir_spillage, :reservoir_pump,
    :gen_seg_output, :bat_seg_discharge,
)

# Containers of PrimaryEnergyVariables whose last index is the step.
const _STEP_INDEXED_PE_FIELDS = (
    :fuel_storage_level_hourly, :fuel_storage_in_hourly, :fuel_storage_out_hourly,
    :fuel_for_power_hourly, :non_electric_consumption_hourly,
    :fuel_loss_of_supply_hourly, :primary_sector_emissions_hourly,
    :total_primary_emissions_hourly,
)

# ... and those whose last index is the primary-energy period.
const _PERIOD_INDEXED_PE_FIELDS = (
    :fuel_supply_periodic, :fuel_transport_periodic,
    :non_electric_consumption_periodic, :fuel_excess_supply_periodic,
    :ne_demand_shortfall_periodic,
)

"""
    period_committed_share(temporal, p, keep) -> Float64

Fraction of primary-energy period `p`'s steps that are among the first `keep`.
"""
function period_committed_share(temporal, p::Int, keep::Int)
    steps = temporal.hours_in_primary_period[p]
    isempty(steps) && return 0.0
    return count(<=(keep), steps) / length(steps)
end

# Call f(last_index, variable) for every variable of a container.
function _each_indexed(f, A)
    A === nothing && return
    if A isa AbstractDict
        foreach(v -> _each_indexed(f, v), values(A))
    elseif A isa JuMP.Containers.SparseAxisArray
        for (k, v) in A.data
            v isa VariableRef && f(last(k), v)
        end
    elseif A isa JuMP.Containers.DenseAxisArray
        for k in keys(A)
            v = A[k]
            v isa VariableRef && f(last(k.I), v)
        end
    elseif A isa AbstractArray
        if all(x -> x === nothing || x isa VariableRef, A)
            for I in CartesianIndices(A)
                v = A[I]
                v isa VariableRef && f(Tuple(I)[end], v)
            end
        else
            foreach(x -> _each_indexed(f, x), A)   # e.g. one vector per line
        end
    end
end

# A step-indexed container: flows (steps 1..hours) or one state series
# (levels 1..hours+1), told apart by its last index.
function _add_step_container!(cs::CommittedSteps, A)
    A === nothing && return
    if A isa AbstractDict || (A isa AbstractArray && !all(x -> x === nothing || x isa VariableRef, A))
        # One series per key / element (each may be a state or a flow)
        foreach(x -> _add_step_container!(cs, x), A isa AbstractDict ? values(A) : A)
        return
    end
    pairs = Tuple{Int, VariableRef}[]
    _each_indexed((t, v) -> push!(pairs, (t, v)), A)
    isempty(pairs) && return
    is_state = maximum(first, pairs) == cs.hours + 1
    ends = Dict{Any, VariableRef}()   # level after step `keep`, by series
    if is_state && cs.keep < cs.hours
        _each_indexed_with_key(A) do key, t, v
            t == cs.keep + 1 && (ends[key] = v)
        end
    end
    _each_indexed_with_key(A) do key, t, v
        if is_state && t == cs.hours + 1
            cs.share[v] = 1.0
            haskey(ends, key) && (cs.end_level[v] = ends[key])
        else
            cs.share[v] = t <= cs.keep ? 1.0 : 0.0
        end
    end
end

# Like _each_indexed, also passing the index without its step (the series).
function _each_indexed_with_key(f, A)
    if A isa JuMP.Containers.SparseAxisArray
        for (k, v) in A.data
            v isa VariableRef && f(k[1:end-1], last(k), v)
        end
    elseif A isa JuMP.Containers.DenseAxisArray
        for k in keys(A)
            v = A[k]
            v isa VariableRef && f(k.I[1:end-1], last(k.I), v)
        end
    elseif A isa AbstractArray
        for I in CartesianIndices(A)
            v = A[I]
            v isa VariableRef && f(Tuple(I)[1:end-1], Tuple(I)[end], v)
        end
    end
end

"""
    committed_steps(ps_vars, hours, keep; pe_vars=nothing, pe_temporal=nothing) -> CommittedSteps

Map the variables of a solved window to their share in its first `keep` steps
(see [`CommittedSteps`](@ref)). `ps_vars` may be `nothing` (a primary-energy
block on its own).
"""
function committed_steps(ps_vars::Union{PowerSystemVariables, Nothing}, hours::Int, keep::Int;
                         pe_vars = nothing, pe_temporal = nothing)
    cs = CommittedSteps(clamp(keep, 1, hours), hours,
                        Dict{VariableRef, Float64}(), Dict{VariableRef, VariableRef}())
    if ps_vars !== nothing
        for f in _STEP_INDEXED_PS_FIELDS
            _add_step_container!(cs, getfield(ps_vars, f))
        end
    end
    # Shifted demand: (bus, step, destination step) => variable
    if ps_vars !== nothing && ps_vars.demand_shift !== nothing
        for shifts in values(ps_vars.demand_shift), ((_, t, _), v) in shifts
            cs.share[v] = t <= cs.keep ? 1.0 : 0.0
        end
    end
    if pe_vars !== nothing
        for f in _STEP_INDEXED_PE_FIELDS
            _add_step_container!(cs, getfield(pe_vars, f))
        end
        if pe_temporal !== nothing
            for f in _PERIOD_INDEXED_PE_FIELDS
                _each_indexed(getfield(pe_vars, f)) do p, v
                    cs.share[v] = period_committed_share(pe_temporal, p, cs.keep)
                end
            end
        end
    end
    return cs
end

function _committed_share(cs::CommittedSteps, v::VariableRef)
    s = get(cs.share, v, nothing)
    s === nothing || return s
    m = match(r"_t(\d+)$", name(v))
    m === nothing && return cs.keep / cs.hours
    return parse(Int, m.captures[1]) <= cs.keep ? 1.0 : 0.0
end

_committed_var_value(cs::CommittedSteps, v::VariableRef) = value(get(cs.end_level, v, v))

"""
    committed_value(cs, ex) -> Float64

Value of the solved expression `ex` over the committed steps: each term
weighted by its variable's share, constants by `keep / hours`.
"""
function committed_value(cs::CommittedSteps, ex::GenericAffExpr)
    total = ex.constant * cs.keep / cs.hours
    for (v, c) in ex.terms
        total += c * _committed_share(cs, v) * _committed_var_value(cs, v)
    end
    return total
end

function committed_value(cs::CommittedSteps, ex::GenericQuadExpr)
    total = committed_value(cs, ex.aff)
    for (pair, c) in ex.terms
        total += c * _committed_share(cs, pair.a) *
                 _committed_var_value(cs, pair.a) * _committed_var_value(cs, pair.b)
    end
    return total
end

committed_value(cs::CommittedSteps, v::VariableRef) =
    _committed_share(cs, v) * _committed_var_value(cs, v)

committed_value(cs::CommittedSteps, x::Real) = Float64(x) * cs.keep / cs.hours

# Any other expression (e.g. nonlinear): the window's share
committed_value(cs::CommittedSteps, ex) = value(ex) * cs.keep / cs.hours
