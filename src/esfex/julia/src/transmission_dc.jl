"""
transmission_dc.jl - DC Power Flow using Kirchhoff Formulation

Implements DC power flow constraints based on PyPSA methodology:
- KCL (Kirchhoff's Current Law): bus-level power balance
- KVL (Kirchhoff's Voltage Law): loop flow constraints
- Sparse cycle-based formulation for efficiency
- Per-line impedance data with parallel line support
- Transformer branches with series reactance

Buses are the electrical abstraction; nodes are geographic. Each bus maps to
a parent node via bus_to_node, and carries a demand_fraction of that node's demand.

Reference: "Linear Optimal Power Flow Using Cycle Flows" (Hörsch et al., 2018)
"""

"""
DC Power Flow transmission model using Kirchhoff formulation.

Fields indexed per physical line (supports parallel lines between same bus pair).
Buses are the electrical indexing unit; nodes are geographic.
"""
struct TransmissionDC
    num_buses::Int
    lines::Vector{Tuple{Int, Int}}      # (from_bus, to_bus) per physical line, 1-indexed
    line_reactances::Vector{Float64}    # Per physical line (indexed by position)
    line_capacities::Vector{Float64}    # Per physical line capacity (MW)
    incidence_matrix::Matrix{Float64}   # buses × lines
    max_angle_diff_rad::Float64
    slack_bus::Int
    line_losses::Vector{Float64}        # Per-line series resistance R_pu (loss coefficient)
    pwl_losses::Union{PWLLossSegments, Nothing}  # Loss segments, see with_loss_model (nothing = lossless)
    line_ids::Vector{String}            # line_id per entry in `lines` ("" = transformer/adjacency); id-addresses outage masks
end

"""
    TransmissionDC(network::NetworkConfig) -> TransmissionDC

Construct TransmissionDC from network configuration.
Uses per-line data if available, otherwise falls back to adjacency matrix.
Transformer branches are appended as additional lines.
All indexing is per bus (electrical), not per node (geographic).
"""
function TransmissionDC(network::NetworkConfig)
    if !isempty(network.transmission_lines)
        # Enhanced mode: per-line data with individual impedances
        raw_lines, raw_capacities, raw_reactances = build_from_line_data(network)
    else
        # Legacy mode: adjacency matrix with uniform reactance
        raw_lines, raw_capacities, raw_reactances = build_from_adjacency(network)
    end

    # Track which raw transmission lines are valid (non-self-loop)
    # Also track per-line loss data source indices
    lines = Tuple{Int, Int}[]
    capacities = Float64[]
    reactances = Float64[]
    transmission_src_indices = Int[]  # index into network.transmission_lines or 0 for adjacency

    for (idx, (i, j)) in enumerate(raw_lines)
        if i == j
            continue
        end
        push!(lines, (i, j))
        push!(capacities, raw_capacities[idx])
        push!(reactances, raw_reactances[idx])
        push!(transmission_src_indices, idx)
    end

    n_transmission = length(lines)

    # Append transformer branches (from_node/to_node are bus indices)
    # Skip self-loop transformers (both ends at the same bus)
    trafo_indices = Int[]
    for (ti, trafo) in enumerate(network.transformers)
        i, j = trafo.from_node, trafo.to_node
        if i == j
            continue
        end
        edge = i < j ? (i, j) : (j, i)
        push!(lines, edge)
        push!(reactances, trafo.reactance_pu)
        push!(capacities, trafo.rated_power_mva)
        push!(trafo_indices, ti)
    end

    K = build_incidence_matrix(network.num_buses, lines)

    # Compute per-line loss factors
    losses = Float64[]
    for (idx, (i, j)) in enumerate(lines)
        if idx <= n_transmission
            # Transmission line
            src = transmission_src_indices[idx]
            if !isempty(network.transmission_lines) && src <= length(network.transmission_lines)
                # Whole-line R, like the reactance in build_from_line_data
                push!(losses, network.transmission_lines[src].resistance_pu)
            else
                push!(losses, reactances[idx] * network.default_r_to_x_ratio)
            end
        else
            # Transformer branch: use actual series resistance (not losses_fraction)
            # resistance_pu is the proper R for conductance g = R/(R²+X²)
            trafo_idx = trafo_indices[idx - n_transmission]
            push!(losses, network.transformers[trafo_idx].resistance_pu)
        end
    end

    # line_id per entry in `lines`, aligned to the same (filtered) order, so
    # deterministic-outage masks can be looked up by id (filtering-safe).
    # Transformer branches and adjacency-derived lines carry no line_id ("").
    line_ids = String[]
    for idx in 1:length(lines)
        if idx <= n_transmission
            src = transmission_src_indices[idx]
            if !isempty(network.transmission_lines) && src >= 1 && src <= length(network.transmission_lines)
                push!(line_ids, network.transmission_lines[src].line_id)
            else
                push!(line_ids, "")
            end
        else
            # Transformer branch: use the transformer name so its outage mask
            # (keyed under "transformer") can be looked up on this same flow.
            ti = trafo_indices[idx - n_transmission]
            push!(line_ids, network.transformers[ti].name)
        end
    end

    return TransmissionDC(
        network.num_buses,
        lines,
        reactances,
        capacities,
        K,
        network.max_angle_diff_rad,
        network.slack_bus,
        losses,
        nothing,  # pwl_losses: attached by with_loss_model() after construction
        line_ids
    )
end

"""
    with_loss_model(transmission::TransmissionDC, segments::Int, s_base::Float64) -> TransmissionDC

Attach the configured loss model (`pwl_loss_segments` / `transmission_loss_segments`):
`segments > 0` → PWL quadratic losses with that many segments, `segments < 0` →
linear losses (`linear_loss_segments`), `0` → lossless.
"""
function with_loss_model(t::TransmissionDC, segments::Int, s_base::Float64)
    pwl = segments > 0 ? compute_pwl_loss_segments(t, segments, s_base) :
          segments < 0 ? linear_loss_segments(t) : nothing
    losses = segments == 0 ? zeros(length(t.lines)) : t.line_losses
    return TransmissionDC(t.num_buses, t.lines, t.line_reactances, t.line_capacities,
        t.incidence_matrix, t.max_angle_diff_rad, t.slack_bus, losses, pwl, t.line_ids)
end

"""
    linear_loss_segments(transmission::TransmissionDC) -> PWLLossSegments

Linear loss model as a single segment of slope `λ_l = R_pu`: a constant fraction
`λ_l` of the flow is lost whichever way it runs, split between both ends like the
PWL losses. (It used to scale the flow at the lower-index end by `1 - λ_l`, which
created energy whenever the flow ran from that end.)
"""
function linear_loss_segments(transmission::TransmissionDC)
    n_lines = length(transmission.lines)
    λ = [l <= length(transmission.line_losses) ? clamp(transmission.line_losses[l], 0.0, 1.0) : 0.0
         for l in 1:n_lines]
    widths = [[max(transmission.line_capacities[l], 0.0)] for l in 1:n_lines]
    return PWLLossSegments(1, widths, [[λ[l]] for l in 1:n_lines], λ)
end

"""
    compute_pwl_loss_segments(transmission::TransmissionDC, num_segments::Int, s_base::Float64) -> PWLLossSegments

Pre-compute piecewise linear loss segments for all transmission lines.

Approximates quadratic losses `P_loss(f) = g_l × f²_pu × S_base` using `N` linear
segments with increasing marginal slopes. Since power flow `pf` is in MW, we convert
to per-unit: `f_pu = pf / S_base`, giving `P_loss = g_l × pf² / S_base`.
Convexity ensures the LP solver fills lower-cost segments first without binary variables.

For each line `l` with conductance `g_l = R/(R²+X²)` and capacity `f_max` (MW):
- Breakpoints: `f_k = k × f_max / N` for `k = 0, 1, ..., N`
- Segment width: `Δf = f_max / N`
- Slope of segment k: `m_k = g_l × (2k - 1) × Δf / S_base`

Lines with zero resistance or zero capacity get zero slopes (lossless).
"""
function compute_pwl_loss_segments(transmission::TransmissionDC, num_segments::Int, s_base::Float64=100.0)
    if s_base <= 0
        s_base = 100.0
    end
    n_lines = length(transmission.lines)
    widths = Vector{Vector{Float64}}(undef, n_lines)
    slopes = Vector{Vector{Float64}}(undef, n_lines)
    conductances = zeros(n_lines)

    # Defensive clamp on the loss coefficient.  The DC loss coefficient is
    # R_pu (per-unit series resistance); a physical branch has R_pu well
    # under 1.0 p.u.  MAX_G guards against pathological R_pu from bad
    # configs so PWL slopes stay within a solver-friendly range.
    MAX_G = 50.0

    for l in 1:n_lines
        R_l = l <= length(transmission.line_losses) ? transmission.line_losses[l] : 0.0
        f_max = transmission.line_capacities[l]

        if R_l <= 0 || f_max <= 0
            # Lossless line: zero slopes
            conductances[l] = 0.0
            widths[l] = fill(f_max > 0 ? f_max / num_segments : 1.0, num_segments)
            slopes[l] = zeros(num_segments)
            continue
        end

        # DC power-flow series loss: P_loss_pu ≈ R_pu · P_flow_pu², so the
        # loss coefficient is the per-unit SERIES RESISTANCE R_pu — NOT the
        # admittance conductance G = R/(R²+X²).  Using G overstated losses
        # by ≈1/X² (~400× for a typical 0.05 p.u. branch): the PWL slope
        # g_l·(2k-1)·Δf/S_base reached ~13 MW-loss per MW-flow, making
        # power routing infeasible and forcing ~100% load shed.
        # MAX_G is kept as a defensive clamp against pathological R_pu from
        # bad configs (a physical branch has R_pu well under 1.0 p.u.).
        g_l = min(R_l, MAX_G)
        conductances[l] = g_l

        Δf = f_max / num_segments
        widths[l] = fill(Δf, num_segments)
        # PWL slope in MW/MW: m_k = g_l × (2k-1) × Δf / S_base
        # The /S_base converts from per-unit loss to MW loss for MW-valued flows.
        slopes[l] = [g_l * (2k - 1) * Δf / s_base for k in 1:num_segments]
    end

    return PWLLossSegments(num_segments, widths, slopes, conductances)
end

"""
    build_from_line_data(network::NetworkConfig) -> (lines, capacities, reactances)

Build line list from explicit TransmissionLineData.
Each physical line gets its own entry (supports parallel lines between same node pair).
"""
function build_from_line_data(network::NetworkConfig)
    lines = Tuple{Int, Int}[]
    capacities = Float64[]
    reactances = Float64[]

    for tl in network.transmission_lines
        i, j = tl.from_node, tl.to_node
        edge = i < j ? (i, j) : (j, i)
        push!(lines, edge)
        # Whole-line values: they already include all of its circuits
        push!(reactances, tl.reactance_pu)
        push!(capacities, tl.capacity_mw)
    end

    return lines, capacities, reactances
end

"""
    build_from_adjacency(network::NetworkConfig) -> (lines, capacities, reactances)

Legacy mode: build line list from adjacency matrix with uniform reactance.
X_ij = reactance_per_km × distance_km / Z_base, Z_base = voltage_level_kv² / s_base
(0.01 p.u. without a distance)
Adjacency matrix is now bus×bus.
"""
function build_from_adjacency(network::NetworkConfig)
    lines = Tuple{Int, Int}[]
    capacities = Float64[]
    reactances = Float64[]
    n = network.num_buses

    for i in 1:n
        for j in (i+1):n
            cap_ij = network.connections[i, j]
            cap_ji = network.connections[j, i]
            if cap_ij > 0 || cap_ji > 0
                push!(lines, (i, j))
                push!(capacities, max(cap_ij, cap_ji))
                distance_km = network.distances[i, j]
                z_base = network.voltage_level_kv^2 / network.s_base
                x_pu = distance_km > 0 ? network.reactance_per_km * distance_km / z_base : 0.01
                push!(reactances, x_pu)
            end
        end
    end

    return lines, capacities, reactances
end

"""
    build_incidence_matrix(num_buses, lines) -> Matrix{Float64}

Build bus-line incidence matrix K.
K[i, ℓ] = +1 if line ℓ leaves bus i
K[i, ℓ] = -1 if line ℓ enters bus i
"""
function build_incidence_matrix(num_buses::Int, lines::Vector{Tuple{Int,Int}})
    K = zeros(num_buses, length(lines))

    for (ℓ, (i, j)) in enumerate(lines)
        K[i, ℓ] = +1.0  # Flow leaving bus i
        K[j, ℓ] = -1.0  # Flow entering bus j
    end

    return K
end

"""
    pair_flow(vars, i, j, t) -> AffExpr

Flow from bus `i` to bus `j` at step `t`: the sum over every branch between
the pair (parallel lines and transformers), signed for the `i → j` direction
(branches are stored low → high bus). Zero when no branch joins the pair.
"""
function pair_flow(vars::PowerSystemVariables, i::Int, j::Int, t::Int)
    e = AffExpr(0.0)
    key, sgn = i < j ? ((i, j), 1.0) : ((j, i), -1.0)
    for ℓ in get(vars.pair_lines, key, Int[])
        add_to_expression!(e, sgn, vars.power_flow_by_line[ℓ][t])
    end
    return e
end

# =============================================================================
# Exact PWL losses: no line may lose more than the PWL loss of its net flow
# =============================================================================
#
# The PWL loss of a line-step is `ploss = Σ_k m_k (dp_k + dn_k)` with the flow
# `pf = Σ dp_k − Σ dn_k`. Nothing in an LP stops both directions carrying flow
# at once, or a steep segment filling before a flat one, so whenever energy is
# worth less than nothing somewhere (curtailment has a cost, a curtailment
# limit or an RE target that counts renewable output binds) the solver loses
# energy on the lines on purpose: up to twice the full-flow loss of every lossy
# line, at no cost. Cuba, 2049: 9.3 TWh "lost" (23 % of supply) and no
# curtailment. No LP can forbid it (the set {loss = f(|pf|)} is not convex),
# so each solve is repaired instead (`optimize_exact_losses!`).

"""
    LossBlock

One lossy line at one step, as `add_dc_constraints!` builds it: the loss
constraint `ploss == Σ m_k (dp_k + dn_k)`, the flow and its segments, and the
repair state (`dir` 0 = PWL; ±1 = linearised, flow held in that direction
and `ploss = alpha × |pf|`; `retunes`, times `alpha` was updated).
"""
mutable struct LossBlock
    con::ConstraintRef
    ploss::VariableRef
    pf::VariableRef
    dp::Vector{VariableRef}
    dn::Vector{VariableRef}
    widths::Vector{Float64}
    slopes::Vector{Float64}
    open_last::Bool        # last segment unbounded (expandable line)
    group::Int             # time step (of one add_dc_constraints! call)
    dir::Int
    alpha::Float64
    retunes::Int           # times alpha followed the flow in this solve
end

"""
    PWLLossRepair

The lossy line-steps of a model (`model.ext[:pwl_loss_repair]`) and the
number of time-step groups registered so far (one per step of each
`add_dc_constraints!` call: a dispatch window, or one representative day of
a master or Benders subproblem).
"""
mutable struct PWLLossRepair
    blocks::Vector{LossBlock}
    n_groups::Int
end

# A copy (copy_model) gets no registry: the hook then solves it as it stands
JuMP.copy_extension_data(::PWLLossRepair, ::JuMP.AbstractModel, ::JuMP.AbstractModel) = nothing

const LOSS_REPAIR_MAX_ROUNDS = 8
const LOSS_REPAIR_TOL_MW = 1e-3      # over-loss (MW) ignored as solver noise
const LOSS_REPAIR_SECANT_RTOL = 0.02 # linearised loss within 2 % of the PWL loss
const LOSS_REPAIR_SECANT_TOL_MW = 0.1 # ... or within 0.1 MW
const LOSS_REPAIR_MAX_RETUNES = 2    # then the line-step keeps its alpha
# A round only to update alphas runs while the linearised losses, together,
# miss the PWL losses of their flows by more than 0.1 % of all line losses
const LOSS_REPAIR_RETUNE_SHARE = 1e-3

# Per solver: the parameter that makes a re-solve ignore the previous basis,
# its value, and the LP method parameter with its simplex values (for which
# the previous basis is the method's own warm start)
const LP_WARM_START_PARAMS = Dict{String, Tuple{String, Any, String, Tuple}}(
    "Gurobi" => ("LPWarmStart", 0, "Method", (0, 1)),
    "CPLEX" => ("CPXPARAM_Advance", 0, "CPXPARAM_LPMethod", (1, 2, 3)),
)

"""
    lp_warm_start_off(solver, attribute) -> Union{Nothing, Pair{String}}

The parameter (and value) that makes `solver` ignore the previous basis when
it re-solves an LP, read from `LP_WARM_START_PARAMS`; `nothing` for other
solvers and when the LP method (`attribute(name)` reads a parameter) is
simplex, whose own warm start that basis is.
"""
function lp_warm_start_off(solver::AbstractString, attribute)
    spec = get(LP_WARM_START_PARAMS, solver, nothing)
    spec === nothing && return nothing
    param, off, method_param, simplex = spec
    attribute(method_param) in simplex && return nothing
    return param => off
end

"""
    optimize_like_first!(model; kwargs...)

Re-solve a model the loss repair has changed with the LP method of its first
solve. Gurobi and CPLEX restart a changed LP from the previous basis with
simplex, whatever method the user set: on a Cuba master that barrier solves
in 2 minutes, that dual/primal simplex ran 47+ minutes. Unless the user chose
simplex, the previous basis is ignored for this solve (`lp_warm_start_off`)
and the parameter is set back afterwards. Other solvers re-solve as they do.
"""
function optimize_like_first!(model; kwargs...)
    # Set on the backend: through JuMP the change would mark the solution
    # just found as out of date (OPTIMIZE_NOT_CALLED)
    param(name) = MOI.RawOptimizerAttribute(name)
    restore = nothing
    try
        off = lp_warm_start_off(solver_name(model), name -> get_attribute(model, name))
        if off !== nothing
            restore = off.first => get_attribute(model, off.first)
            MOI.set(backend(model), param(off.first), off.second)
        end
    catch
        restore = nothing   # a solver version without the parameter
    end
    try
        optimize!(model; ignore_optimize_hook = true, kwargs...)
    finally
        restore === nothing || MOI.set(backend(model), param(restore.first), restore.second)
    end
    return
end

"""
    pwl_loss_repair!(model) -> PWLLossRepair

The model's loss registry, created (with `optimize_exact_losses!` as its
optimize hook) on first use.
"""
function pwl_loss_repair!(model)
    reg = get(model.ext, :pwl_loss_repair, nothing)
    reg isa PWLLossRepair && return reg
    reg = PWLLossRepair(LossBlock[], 0)
    model.ext[:pwl_loss_repair] = reg
    set_optimize_hook(model, optimize_exact_losses!)
    return reg
end

"""
    pwl_loss_value(blk::LossBlock, F) -> Float64

PWL loss of a flow of magnitude `F` on the line: its segments filled in
order (the open last segment of an expandable line takes the rest).
"""
function pwl_loss_value(blk::LossBlock, F::Real)
    rem = max(F, 0.0)
    loss = 0.0
    N = length(blk.widths)
    for k in 1:N
        take = (k == N && blk.open_last) ? rem : min(rem, blk.widths[k])
        loss += blk.slopes[k] * take
        rem -= take
        rem <= 0 && break
    end
    return loss
end

# Loss per MW of a linearised block at flow F: the PWL secant (first slope at 0)
loss_secant(blk::LossBlock, F::Real) = F > 1e-6 ? pwl_loss_value(blk, F) / F : blk.slopes[1]

# A linearised block carries its whole flow on the first segment of its
# direction (`ploss = alpha × that segment`)
function set_block_alpha!(blk::LossBlock, alpha::Float64)
    set_normalized_coefficient(blk.con, (blk.dir > 0 ? blk.dp : blk.dn)[1], -alpha)
    blk.alpha = alpha
end

function linearise_block!(blk::LossBlock, dir::Int, alpha::Float64)
    same, opp = dir > 0 ? (blk.dp, blk.dn) : (blk.dn, blk.dp)
    foreach(v -> set_upper_bound(v, 0.0), opp)
    # The other segments would be copies of the first at the same alpha: a
    # degenerate LP the solvers then wander through
    foreach(v -> set_upper_bound(v, 0.0), @view same[2:end])
    if blk.open_last
        has_upper_bound(same[1]) && delete_upper_bound(same[1])
    else
        set_upper_bound(same[1], sum(blk.widths))
    end
    blk.dir = dir
    blk.retunes = 0
    set_block_alpha!(blk, alpha)
end

function restore_block!(blk::LossBlock)
    blk.dir == 0 && return false
    same, opp = blk.dir > 0 ? (blk.dp, blk.dn) : (blk.dn, blk.dp)
    set_normalized_coefficient(blk.con, same[1], -blk.slopes[1])
    N = length(blk.widths)
    for k in 1:N, v in (same[k], opp[k])
        if k < N || !blk.open_last
            set_upper_bound(v, blk.widths[k])
        elseif has_upper_bound(v)
            delete_upper_bound(v)
        end
    end
    blk.dir = 0
    blk.alpha = 0.0
    blk.retunes = 0
    return true
end

# Back to the PWL model as built; true if some block was linearised
restore_pwl_losses!(reg::PWLLossRepair) =
    foldl((changed, b) -> restore_block!(b) | changed, reg.blocks; init = false)

"""
    optimize_exact_losses!(model; kwargs...)

Optimize hook of every model with PWL line losses (set by
`add_dc_constraints!`), so each `optimize!` returns losses that never exceed
the PWL loss of the net flow on any line:

1. Solve with the PWL loss model as built (exact wherever energy has a
   positive value: the LP then fills segments in order, one direction).
2. Each step where some line loses more than the PWL loss of its flow
   `|pf|` is repaired on every lossy line of that step: the flow is held in
   the direction it had and the loss becomes `alpha × |pf|`, with `alpha`
   the PWL secant at that flow (a linear function of the flow, so nothing
   can be lost beyond it). The model is solved again; `alpha` follows the
   new flow until the loss is within 2 % (or 0.1 MW) of the PWL loss, at most
   twice per line-step, and steps that now over-lose are repaired too (at
   most $(LOSS_REPAIR_MAX_ROUNDS) rounds). A round that would only update
   alphas is skipped when the linearised losses together miss the PWL
   losses of their flows by at most $(100 * LOSS_REPAIR_RETUNE_SHARE) % of all
   line losses.

Each re-solve of a changed model uses the LP method of the first solve
(`optimize_like_first!`): Gurobi and CPLEX would otherwise restart from the
previous basis with simplex, even under barrier.

The surplus that was lost on the lines is then curtailed (at its cost,
within its limits), stored or not produced. Steps without over-loss keep the
PWL model and its marginal losses. Every call starts again from the PWL
model, so a model re-solved after a change (Benders, MGA, NPV retirement) is
repaired for its new solution. If a repaired solve finds no solution (only
discarding energy on the lines kept the model feasible), the PWL solve is
kept with a warning. A first solve that is not optimal (stopped at a limit,
or a solver without an answer, as HiGHS's "no progress") is returned as it
is, with a warning: its losses mean nothing, and each round re-solves the
whole model. The model stays an LP (or the MIP it was).
"""
# An optimal solve, whose losses the repair can start from
loss_repair_solved(model) =
    termination_status(model) in (MOI.OPTIMAL, MOI.LOCALLY_SOLVED) &&
    primal_status(model) == MOI.FEASIBLE_POINT

function optimize_exact_losses!(model; kwargs...)
    reg = get(model.ext, :pwl_loss_repair, nothing)
    if !(reg isa PWLLossRepair) || isempty(reg.blocks)
        return optimize!(model; ignore_optimize_hook = true, kwargs...)
    end
    if restore_pwl_losses!(reg)
        optimize_like_first!(model; kwargs...)
    else
        optimize!(model; ignore_optimize_hook = true, kwargs...)
    end
    if has_values(model) && !loss_repair_solved(model)
        # Cuba on HiGHS: "no progress" after 12.8 h (relative primal-dual
        # objective gap 1.0), and its repair started another solve as long
        @warn "Line losses: not repaired, the solve ended " *
              "$(termination_status(model)) ($(primal_status(model)))"
        return
    end
    repaired = Set{Int}()
    for r in 1:LOSS_REPAIR_MAX_ROUNDS
        has_values(model) || break
        # Read the whole solution before touching the model
        pf = [value(b.pf) for b in reg.blocks]
        F = abs.(pf)
        L = [value(b.ploss) for b in reg.blocks]
        over_groups = Set{Int}()
        retune = Int[]
        over_mw = 0.0
        retune_mw = 0.0
        for (i, b) in enumerate(reg.blocks)
            exact = pwl_loss_value(b, F[i])
            if b.dir == 0
                excess = L[i] - exact
                if excess > LOSS_REPAIR_TOL_MW + 1e-5 * F[i]
                    push!(over_groups, b.group)
                    over_mw += excess
                end
            elseif b.retunes < LOSS_REPAIR_MAX_RETUNES &&
                   abs(L[i] - exact) > max(LOSS_REPAIR_SECANT_TOL_MW,
                                           LOSS_REPAIR_SECANT_RTOL * exact)
                # A flow that keeps moving with alpha (it sits where alpha
                # decides it) keeps the alpha of its second update
                push!(retune, i)
                retune_mw += abs(L[i] - exact)
            end
        end
        isempty(over_groups) &&
            retune_mw <= LOSS_REPAIR_RETUNE_SHARE * sum(L; init = 0.0) && return
        if r == LOSS_REPAIR_MAX_ROUNDS
            isempty(over_groups) && return
            @warn "Line losses: lines still lose $(round(over_mw; digits = 1)) MW·step " *
                  "beyond the PWL loss of their flows after " *
                  "$(LOSS_REPAIR_MAX_ROUNDS) repair rounds"
            return
        end
        @debug "Line losses: repairing $(length(over_groups)) step(s) losing " *
               "$(round(over_mw; digits = 1)) MW·step beyond the PWL loss, updating " *
               "$(length(retune)) linearised loss(es) off by " *
               "$(round(retune_mw; digits = 1)) MW·step (all lines lose " *
               "$(round(sum(L; init = 0.0); digits = 1)) MW·step; round $(r))"
        for i in retune
            set_block_alpha!(reg.blocks[i], loss_secant(reg.blocks[i], F[i]))
            reg.blocks[i].retunes += 1
        end
        for (i, b) in enumerate(reg.blocks)
            (b.dir == 0 && b.group in over_groups) || continue
            # Held in the direction of its flow (+ at 0)
            linearise_block!(b, pf[i] < 0 ? -1 : 1, loss_secant(b, F[i]))
        end
        union!(repaired, over_groups)
        optimize_like_first!(model; kwargs...)
        if !loss_repair_solved(model)
            @warn "Line losses: the model has no optimal solution once $(length(repaired)) " *
                  "step(s) lose no more than the PWL loss of their flows " *
                  "($(termination_status(model))); keeping the PWL solution, whose " *
                  "lines lose energy beyond their flows' losses"
            restore_pwl_losses!(reg)
            optimize_like_first!(model; kwargs...)
            return
        end
    end
    return
end

"""
    add_dc_constraints!(model, transmission::TransmissionDC, vars, input)

Add DC power flow constraints to JuMP model.
- KCL at each bus (power balance with transmission flows)
- KVL for each independent cycle (Kirchhoff's Voltage Law)
- Slack bus angle reference
- Voltage angle limits (optional)

Power flow variables are created per physical line (supports parallel lines).
Bus demand is derived from the parent node's demand scaled by demand_fraction.
"""
function add_dc_constraints!(model, transmission::TransmissionDC,
                             vars::PowerSystemVariables, input;
                             extra_injections_fn::Union{Nothing, Function} = nothing,
                             expandable_lines::Set{Int} = Set{Int}(),
                             expansion_max::AbstractDict{Int, Float64} = Dict{Int, Float64}())
    n_bus = transmission.num_buses
    n_line = length(transmission.lines)
    hours = input.temporal.hours
    slack = transmission.slack_bus

    # Create power flow variables per physical line
    pf_by_line = Vector{Vector{VariableRef}}(undef, n_line)
    for (ℓ, (i, j)) in enumerate(transmission.lines)
        pf_by_line[ℓ] = @variable(model, [1:hours], base_name="pf_$(ℓ)_$(i)_$(j)")
    end
    vars.power_flow_by_line = pf_by_line

    # Bus pair → every branch between it (parallel lines, transformers), for
    # pair flows (results, custom constraints) that sum all of them
    for (ℓ, (i, j)) in enumerate(transmission.lines)
        push!(get!(vars.pair_lines, (i, j), Int[]), ℓ)
    end

    n_gen = length(input.generators)
    n_bat = length(input.batteries)
    n_node = input.network.num_nodes
    b2n = input.network.bus_to_node

    # PWL loss model: pre-allocate storage for per-line per-timestep loss variables
    has_pwl = transmission.pwl_losses !== nothing
    pwl = transmission.pwl_losses  # may be nothing
    pwl_loss_by_line = has_pwl ?
        [Vector{VariableRef}(undef, hours) for _ in 1:n_line] : nothing
    vars.line_losses = pwl_loss_by_line

    shift_net = demand_shift_net(vars)

    # Detect connected components to handle disconnected networks.
    # Each connected component needs its own angle reference (slack bus).
    # Buses with NO lines are isolated → fix their angle to 0.
    component_id = zeros(Int, n_bus)  # 0 = unvisited
    component_count = 0
    adj = [Set{Int}() for _ in 1:n_bus]
    for (i, j) in transmission.lines
        push!(adj[i], j)
        push!(adj[j], i)
    end
    for start_bus in 1:n_bus
        component_id[start_bus] != 0 && continue
        component_count += 1
        # BFS
        queue = [start_bus]
        component_id[start_bus] = component_count
        head = 1
        while head <= length(queue)
            u = queue[head]; head += 1
            for v in adj[u]
                if component_id[v] == 0
                    component_id[v] = component_count
                    push!(queue, v)
                end
            end
        end
    end

    # For each component, pick a reference bus (slack): use global slack
    # for its component, first bus for all others.
    slack_component = component_id[slack]
    component_slack = Dict{Int, Int}()
    for b in 1:n_bus
        c = component_id[b]
        if !haskey(component_slack, c)
            component_slack[c] = b
        end
    end
    # Override: global slack is the reference for its component
    component_slack[slack_component] = slack

    # Every lossy line-step is registered for the exact-loss repair
    # (`optimize_exact_losses!`), each time step of this call as one group
    loss_repair = has_pwl ? pwl_loss_repair!(model) : nothing
    group0 = has_pwl ? loss_repair.n_groups : 0
    has_pwl && (loss_repair.n_groups += hours)

    if component_count > 1
        # @debug not @warn: this function runs at every rolling-horizon
        # window setup. A multi-component network is a property of the
        # topology, so the message would otherwise repeat identically
        # 60+ windows × 25 years and drown the basic console. The
        # per-component angle-reference fixup is deliberate and safe;
        # the topology audit warns once at load time anyway.
        @debug "Disconnected network: $(component_count) components detected. " *
               "Adding per-component angle references."
    end
    for t in 1:hours
        # 1. Slack bus angle reference (one per connected component)
        for (_, ref_bus) in component_slack
            @constraint(model, vars.voltage_angle[ref_bus, t] == 0)
        end

        # PWL loss: create direction decomposition + segment variables for each line
        if has_pwl
            N = pwl.num_segments
            for ℓ in 1:n_line
                g_l = pwl.conductances[ℓ]
                cap_ℓ = transmission.line_capacities[ℓ]
                if g_l <= 0 || (cap_ℓ < 0.1 && !(ℓ in expandable_lines))  # lossless or negligible capacity
                    # Lossless line: loss = 0
                    ploss = @variable(model, lower_bound=0, upper_bound=0,
                        base_name="ploss_$(ℓ)_$(t)")
                    pwl_loss_by_line[ℓ][t] = ploss
                    continue
                end

                # Direction decomposition: pf = f_pos - f_neg
                f_pos = @variable(model, lower_bound=0,
                    base_name="fpos_$(ℓ)_$(t)")
                f_neg = @variable(model, lower_bound=0,
                    base_name="fneg_$(ℓ)_$(t)")
                @constraint(model, pf_by_line[ℓ][t] == f_pos - f_neg)

                # Incremental segment variables with PWL loss accumulation.
                # Segments span the base capacity; on a line that investment can
                # expand, the last one also spans the MW that can be built
                # (`expansion_max`), or is left open when that has no limit.
                # An open segment in both directions lets f_pos and f_neg grow
                # together: Cuba's masters had one on every line (round 36),
                # and barrier iterates ran to 10⁶ MW and ended NUMERICAL_ERROR.
                loss_expr = AffExpr(0.0)
                fpos_sum = AffExpr(0.0)
                fneg_sum = AffExpr(0.0)
                dps = Vector{VariableRef}(undef, N)
                dns = Vector{VariableRef}(undef, N)
                widths = copy(pwl.segment_widths[ℓ])
                extra = ℓ in expandable_lines ? get(expansion_max, ℓ, Inf) : 0.0
                open_last = !isfinite(extra)
                open_last || (widths[N] += extra)
                for k in 1:N
                    w = widths[k]
                    m = pwl.slopes[ℓ][k]
                    dp = @variable(model, lower_bound=0,
                        base_name="dp_$(ℓ)_$(k)_$(t)")
                    dn = @variable(model, lower_bound=0,
                        base_name="dn_$(ℓ)_$(k)_$(t)")
                    if k < N || !open_last
                        set_upper_bound(dp, w)
                        set_upper_bound(dn, w)
                    end
                    dps[k] = dp
                    dns[k] = dn
                    add_to_expression!(fpos_sum, dp)
                    add_to_expression!(fneg_sum, dn)
                    add_to_expression!(loss_expr, m, dp)
                    add_to_expression!(loss_expr, m, dn)
                end
                @constraint(model, f_pos == fpos_sum)
                @constraint(model, f_neg == fneg_sum)

                # Store loss variable for KCL half-loss split
                ploss = @variable(model, lower_bound=0,
                    base_name="ploss_$(ℓ)_$(t)")
                loss_con = @constraint(model, ploss == loss_expr)
                pwl_loss_by_line[ℓ][t] = ploss
                push!(loss_repair.blocks, LossBlock(loss_con, ploss, pf_by_line[ℓ][t],
                    dps, dns, widths, pwl.slopes[ℓ], open_last,
                    group0 + t, 0, 0.0, 0))
            end
        end

        # 2. KCL at each bus
        for i in 1:n_bus
            # Node index and bus role (drives how demand-side terms are handled)
            ni = b2n[i]
            bus_role = input.network.buses[i].role
            is_load_bus = bus_role == "load" || bus_role == "mixed"
            # Connection buses carry no demand: their KCL is pure flow balance
            # (gen ± bat ± converters + extra = Σ flows). All node-level demand
            # variables (load_shed, EV, rooftop) are skipped because
            # they are scaled by demand_fraction which is 0 for connection buses.
            bus_df = is_load_bus ? input.network.buses[i].demand_fraction : 0.0

            # Generation at bus (sparse: only generators with capacity at this bus)
            gen_sum = @expression(model,
                sum(vars.gen_output[g, i, t] for g in vars.gens_at_bus[i]; init=AffExpr(0.0))
            )

            # Battery discharge and charge separately (sparse: only batteries at this bus)
            bat_discharge = @expression(model,
                sum(vars.bat_discharge[b, i, t] for b in vars.bats_at_bus[i]; init=AffExpr(0.0))
            )

            bat_charge_sum = @expression(model,
                sum(vars.bat_charge[b, i, t] for b in vars.bats_at_bus[i]; init=AffExpr(0.0))
            )

            # EV V2G (supply side) and charging (demand side)
            # Node-level variables scaled by demand_fraction (same as load_shed)
            ev_v2g_term = if vars.ev_v2g !== nothing
                vars.ev_v2g[ni, t] * bus_df
            else
                AffExpr(0.0)
            end

            ev_charging_term = if vars.ev_charging !== nothing
                vars.ev_charging[ni, t] * bus_df
            else
                AffExpr(0.0)
            end

            # Electrolyzer power (demand side)
            electrolyzer_term = if vars.electrolyzer_power !== nothing
                electrolyzer_load(vars, i, t)
            else
                AffExpr(0.0)
            end

            # AC/DC converter injection at bus i (from_node/to_node are bus indices)
            acdc_term = AffExpr(0.0)
            if vars.acdc_rectify !== nothing && vars.acdc_invert !== nothing
                for (c_idx, conv) in enumerate(input.network.acdc_converters)
                    if conv.from_node == i  # AC side: loses rectified, gains inverted
                        add_to_expression!(acdc_term,
                            -1.0, vars.acdc_rectify[c_idx, t])
                        add_to_expression!(acdc_term,
                            conv.efficiency_invert, vars.acdc_invert[c_idx, t])
                    elseif conv.to_node == i  # DC side: gains rectified, loses inverted
                        add_to_expression!(acdc_term,
                            conv.efficiency_rectify, vars.acdc_rectify[c_idx, t])
                        add_to_expression!(acdc_term,
                            -1.0, vars.acdc_invert[c_idx, t])
                    end
                end
            end

            # Frequency converter injection at bus i (from_node/to_node are bus indices)
            freq_term = AffExpr(0.0)
            if vars.freq_flow_a_to_b !== nothing && vars.freq_flow_b_to_a !== nothing
                for (c_idx, conv) in enumerate(input.network.freq_converters)
                    if conv.from_node == i  # A side: loses a→b, gains b→a
                        add_to_expression!(freq_term,
                            -1.0, vars.freq_flow_a_to_b[c_idx, t])
                        add_to_expression!(freq_term,
                            conv.efficiency_b_to_a, vars.freq_flow_b_to_a[c_idx, t])
                    elseif conv.to_node == i  # B side: gains a→b, loses b→a
                        add_to_expression!(freq_term,
                            conv.efficiency_a_to_b, vars.freq_flow_a_to_b[c_idx, t])
                        add_to_expression!(freq_term,
                            -1.0, vars.freq_flow_b_to_a[c_idx, t])
                    end
                end
            end

            # Bus demand from parent node scaled by demand_fraction.
            # Connection buses (bus_df = 0) contribute zero to the demand term.
            bus_demand = input.demand[t, input.network.bus_to_node[i]] * bus_df

            # Cap load shedding at the demand actually sitting on this bus.
            # ``load_shed`` enters the KCL below on the SUPPLY side, so with
            # only its ``>= 0`` declaration it is unbounded above and the
            # solver can use it as a virtual generator: shed far more than a
            # bus consumes and export the surplus over the network. Japan:
            # 1 504 MW "shed" at a 33 MW bus, and 66 % of the reported load
            # shedding was this artefact rather than real unserved load, which
            # also displaced real dispatch. The single-bus balance
            # (``add_demand_constraints!``) and the AC-OPF path both bound it;
            # the DC-OPF path — the default for every UC run — did not.
            # Connection buses (bus_df = 0) get the bound at 0, which is the
            # same rule: nothing to leave unserved where there is no load.
            @constraint(model,
                vars.load_shed[i, t] <= max(bus_demand, 0.0),
                base_name = "max_load_shed_dc_b$(i)_t$(t)")

            # Reservoir pump power (demand side — pumping consumes electricity)
            reservoir_pump_term = AffExpr(0.0)
            if vars.reservoir_pump !== nothing
                for g in vars.gens_at_bus[i]
                    gen = input.generators[g]
                    # Reservoir arrays are per bus (expanded by the adapter).
                    if gen.reservoir_capacity[i] > 0 && gen.reservoir_pump_capacity[i] > 0
                        add_to_expression!(reservoir_pump_term, vars.reservoir_pump[g, i, t])
                    end
                end
            end

            rooftop_gen_term = if hasproperty(input, :rooftop_generation) && input.rooftop_generation !== nothing
                input.rooftop_generation[t, ni] * bus_df
            else
                0.0
            end

            # Rooftop curtailment term (node-level, scaled by demand_fraction)
            rooftop_curt_term = if vars.rooftop_curtailment !== nothing
                vars.rooftop_curtailment[ni, t] * bus_df
            else
                AffExpr(0.0)
            end

            # Extra injection terms from caller (master: tech_output, bat_tech, sectoral_lol)
            extra_term = if extra_injections_fn !== nothing
                extra_injections_fn(i, t)
            else
                AffExpr(0.0)
            end

            # Demand moved into this step by demand shifting (net, may be < 0)
            shift_term = haskey(shift_net, (ni, t)) ? shift_net[(ni, t)] * bus_df : AffExpr(0.0)

            # Net injection: LHS - RHS
            # LHS: gen + bat_discharge + EV_V2G + load_shed + rooftop_gen + converter_injections + extra
            # RHS: bus_demand + shifted demand + electrolyzer + bat_charge + reservoir_pump + EV_charging + rooftop_curt
            # Reserves are headroom (add_reserve_constraints!), not energy, so not in KCL.
            # Node-level variables (load_shed, EV, shifts) scaled by demand_fraction for bus-level KCL
            net_inj = @expression(model,
                gen_sum + bat_discharge + ev_v2g_term + vars.load_shed[i, t] +
                rooftop_gen_term + acdc_term + freq_term + extra_term -
                bus_demand - shift_term - electrolyzer_term - bat_charge_sum - reservoir_pump_term -
                ev_charging_term - rooftop_curt_term
            )

            # Sum of flows using incidence matrix (per physical line)
            # With losses (PWL or linear segments, see with_loss_model):
            #   net_inj = Σ_l (K[i,l] × pf[l,t] + 0.5 × |K[i,l]| × loss[l,t])
            flow_sum = AffExpr(0.0)
            for ℓ in 1:n_line
                coeff = transmission.incidence_matrix[i, ℓ]
                if abs(coeff) < 1e-10
                    continue
                end

                if has_pwl
                    # PWL model: flow + half-loss allocated at each endpoint
                    # Sign: +0.5 because losses INCREASE power extracted from bus
                    # (bus must supply midpoint flow + its half of losses)
                    # net_inj_i = Σ_l [K[i,l]*pf + 0.5*|K[i,l]|*ploss]
                    add_to_expression!(flow_sum, coeff, pf_by_line[ℓ][t])
                    add_to_expression!(flow_sum, 0.5 * abs(coeff), pwl_loss_by_line[ℓ][t])
                else
                    add_to_expression!(flow_sum, coeff, pf_by_line[ℓ][t])
                end
            end

            con = @constraint(model, net_inj == flow_sum)

            # Store constraint reference for dual extraction
            if vars.balance_constraints !== nothing
                vars.balance_constraints[(i, t)] = con
            end

            # (Rooftop curtailment limit and max load shed are now per-node, added below)
        end

        # ── Per-node constraints (outside bus loop) ──
        for ni in 1:n_node
            # Rooftop curtailment cannot exceed available rooftop generation at node
            if vars.rooftop_curtailment !== nothing && hasproperty(input, :rooftop_generation) && input.rooftop_generation !== nothing
                @constraint(model,
                    vars.rooftop_curtailment[ni, t] <= input.rooftop_generation[t, ni],
                    base_name = "rooftop_curt_limit_dc_n$(ni)_t$(t)")
            end

        end

        # NOTE: the per-bus load_shed bound lives in the KCL loop above
        # (``max_load_shed_dc_b*``), where it caps every bus at its OWN
        # demand. The zero-demand case this block used to handle separately
        # is that same bound evaluated at 0, so it is no longer repeated here.

        # 3. Flow-angle coupling (standard DCOPF: pf_MW = S_base/x_pu × Δθ)
        # This replaces the cycle-based KVL formulation and correctly handles
        # parallel lines (each line independently linked to bus angles).
        # S_base converts per-unit susceptance to MW/radian.
        s_base = input.network.s_base
        for (ℓ, (i, j)) in enumerate(transmission.lines)
            cap_ℓ = transmission.line_capacities[ℓ]
            # Lines with negligible capacity add constraints without meaningful
            # power transfer and worsen LP conditioning. Fix their flow to 0,
            # unless investment can expand them (a candidate corridor starts at 0 MW).
            if cap_ℓ < 0.1 && !(ℓ in expandable_lines)  # < 0.1 MW is electrically negligible
                @constraint(model, pf_by_line[ℓ][t] == 0,
                    base_name = "flow_zero_$(ℓ)_$(t)")
                continue
            end
            x_ℓ = transmission.line_reactances[ℓ]
            # Guard against zero/negative reactance (e.g. virtual/copper-plate lines)
            if x_ℓ <= 0.0
                x_ℓ = 0.01  # Default minimum reactance (p.u.)
            end
            b_line = s_base / x_ℓ  # MW/radian (physical susceptance)
            @constraint(model,
                pf_by_line[ℓ][t] == b_line * (vars.voltage_angle[i, t] - vars.voltage_angle[j, t]),
                base_name = "flow_angle_$(ℓ)_$(t)")
        end

        # NOTE: DC voltage-angle-difference limits were removed.  In this DC
        # formulation `pf = b_line·Δθ` with b_line capped (numerical
        # conditioning), so a |Δθ| ≤ max_angle constraint imposes the
        # NON-PHYSICAL bound pf ≤ b_line·max_angle — far below the branch
        # thermal rating — stranding generation and forcing spurious load
        # shed.  DC flows are limited physically by the explicit
        # pf ≤ line_capacity constraints; the ±π reference bound on
        # voltage_angle keeps the formulation well-posed.  Voltage-angle /
        # stability limits remain available and physically correct in the
        # ACOPF formulations (transmission_acopf.jl, gated by
        # max_angle_diff_rad < π).
    end
end

"""
    branch_outage_factor(input, id, t) -> Float64

Scheduled-outage capacity factor (1.0 = none) of the line (`line_id`) or
transformer (name) `id` at step `t`, from `input.outage_masks`. Lightweight
NamedTuple inputs (master problem) carry no masks: always 1.0.
"""
function branch_outage_factor(input, id::AbstractString, t::Int)
    isempty(id) && return 1.0
    masks = hasproperty(input, :outage_masks) ? input.outage_masks : nothing
    masks === nothing && return 1.0
    for kind in ("line", "transformer")
        m = get(masks, kind, nothing)
        if m !== nothing && haskey(m, id)
            return m[id][t]
        end
    end
    return 1.0
end

"""
    add_line_capacity_constraints!(model, transmission::TransmissionDC, vars, input)

Add thermal capacity limits for transmission lines (per physical line), scaled
by scheduled outages. The master passes its investment-augmented capacities in
`capacity_override`; the dispatch never builds lines.
"""
function add_line_capacity_constraints!(model, transmission::TransmissionDC,
                                        vars::PowerSystemVariables, input;
                                        capacity_override::Union{Dict{Int, Any}, Nothing} = nothing)
    hours = input.temporal.hours
    pf_by_line = vars.power_flow_by_line

    for ℓ in eachindex(transmission.lines)
        # Master: investment-augmented line capacity (AffExpr)
        total_cap = capacity_override !== nothing && haskey(capacity_override, ℓ) ?
            capacity_override[ℓ] : transmission.line_capacities[ℓ]
        for t in 1:hours
            # Bidirectional flow limits per physical line (× outage factor)
            om = branch_outage_factor(input, transmission.line_ids[ℓ], t)
            @constraint(model, pf_by_line[ℓ][t] <= total_cap * om)
            @constraint(model, pf_by_line[ℓ][t] >= -total_cap * om)
        end
    end
end

"""
    add_converter_constraints!(model, vars, input)

Add AC/DC converter and frequency converter variables and capacity constraints.
Variables:
- acdc_rectify[c, t] ≥ 0  (AC→DC power for converter c at hour t)
- acdc_invert[c, t] ≥ 0   (DC→AC power for converter c at hour t)
- freq_flow_a_to_b[c, t] ≥ 0  (A→B power for converter c at hour t)
- freq_flow_b_to_a[c, t] ≥ 0  (B→A power for converter c at hour t)

Constraints:
- Capacity: rectify + invert ≤ rated_power (per converter)
- Capacity: flow_a_to_b + flow_b_to_a ≤ rated_power (per converter)
- Minimum power (`min_power_mva` > 0, unit commitment dispatch only): binary
  `on` per direction and step (`acdc_on_rectify`/`acdc_on_invert`,
  `freq_on_a_to_b`/`freq_on_b_to_a`), at most one direction on, and
  `min_power_mva × on ≤ flow ≤ rated × on` in each direction. Economic
  dispatch and the master (LP) ignore `min_power_mva`.

KCL injection is handled in add_dc_constraints! (reads these variables).

`capacity_override` (master problem) maps `("acdc", c)` / `("freq", c)` to
the converter's capacity (an expression with the master's investment) in
place of `rated_power_mva`.
"""
function add_converter_constraints!(model, vars::PowerSystemVariables,
                                     input;
                                     capacity_override::Union{Nothing, AbstractDict} = nothing)
    hours = input.temporal.hours
    network = input.network
    rated(kind, c, conv) = capacity_override === nothing ? conv.rated_power_mva :
        get(capacity_override, (kind, c), conv.rated_power_mva)

    # Deterministic scheduled outages: converters are id-addressed by name
    # (name → hourly capacity factor in [0,1]; 1.0 = none).
    _om = hasproperty(input, :outage_masks) ? input.outage_masks : Dict{String, Any}()
    acdc_masks = get(_om, "acdc_converter", nothing)
    freq_masks = get(_om, "freq_converter", nothing)
    conv_om(masks, nm, t) = (masks === nothing || !haskey(masks, nm)) ? 1.0 : masks[nm][t]
    # Unit commitment dispatch: converters with a minimum power are on/off
    uc = capacity_override === nothing && vars.gen_startup !== nothing

    # --- AC/DC Converters ---
    n_acdc = length(network.acdc_converters)
    if n_acdc > 0
        acdc_rect = @variable(model, [1:n_acdc, 1:hours], lower_bound=0,
                              base_name="acdc_rectify")
        acdc_inv = @variable(model, [1:n_acdc, 1:hours], lower_bound=0,
                             base_name="acdc_invert")
        vars.acdc_rectify = acdc_rect
        vars.acdc_invert = acdc_inv

        for (c, conv) in enumerate(network.acdc_converters)
            for t in 1:hours
                # Mutual capacity: total through-power ≤ rated (× outage factor)
                @constraint(model,
                    acdc_rect[c, t] + acdc_inv[c, t] <=
                        rated("acdc", c, conv) * conv_om(acdc_masks, conv.name, t))
            end
            uc && conv.min_power_mva > 0 && add_converter_commitment!(model,
                acdc_rect, acdc_inv, c, conv, hours,
                t -> rated("acdc", c, conv) * conv_om(acdc_masks, conv.name, t),
                "acdc_on_rectify", "acdc_on_invert")
        end
    end

    # --- Frequency Converters ---
    n_freq = length(network.freq_converters)
    if n_freq > 0
        freq_ab = @variable(model, [1:n_freq, 1:hours], lower_bound=0,
                            base_name="freq_a_to_b")
        freq_ba = @variable(model, [1:n_freq, 1:hours], lower_bound=0,
                            base_name="freq_b_to_a")
        vars.freq_flow_a_to_b = freq_ab
        vars.freq_flow_b_to_a = freq_ba

        for (c, conv) in enumerate(network.freq_converters)
            for t in 1:hours
                @constraint(model,
                    freq_ab[c, t] + freq_ba[c, t] <=
                        rated("freq", c, conv) * conv_om(freq_masks, conv.name, t))
            end
            uc && conv.min_power_mva > 0 && add_converter_commitment!(model,
                freq_ab, freq_ba, c, conv, hours,
                t -> rated("freq", c, conv) * conv_om(freq_masks, conv.name, t),
                "freq_on_a_to_b", "freq_on_b_to_a")
        end
    end
end

"""
    add_converter_commitment!(model, fwd, bwd, c, conv, hours, cap, fwd_name, bwd_name)

On/off commitment of converter `c` (`min_power_mva` > 0, unit commitment
dispatch): a binary per direction and step, at most one of them on, and each
direction's flow either 0 or between `min_power_mva` and `cap(t)` (rated
power × outage factor). The binaries are listed in
`model.ext[:converter_commitment]`, so the UC dual recovery fixes them with
the generators' commitment.
"""
function add_converter_commitment!(model, fwd, bwd, c::Int, conv, hours::Int,
                                   cap::Function, fwd_name::String, bwd_name::String)
    on_f = @variable(model, [1:hours], Bin, base_name = "$(fwd_name)_c$(c)")
    on_b = @variable(model, [1:hours], Bin, base_name = "$(bwd_name)_c$(c)")
    for t in 1:hours
        @constraint(model, on_f[t] + on_b[t] <= 1)
        @constraint(model, fwd[c, t] <= cap(t) * on_f[t])
        @constraint(model, bwd[c, t] <= cap(t) * on_b[t])
        @constraint(model, fwd[c, t] >= conv.min_power_mva * on_f[t])
        @constraint(model, bwd[c, t] >= conv.min_power_mva * on_b[t])
    end
    append!(get!(model.ext, :converter_commitment, VariableRef[]), on_f, on_b)
    return nothing
end

"""
    add_converter_objective_terms(model, vars, input) -> AffExpr

Return objective cost expression for converter operations.
Cost = Σ variable_cost × (rectify + invert) for each converter and hour.
"""
function add_converter_objective_terms(vars::PowerSystemVariables, input)
    cost = AffExpr(0.0)
    hours = input.temporal.hours
    network = input.network

    # AC/DC converter variable costs
    if vars.acdc_rectify !== nothing && vars.acdc_invert !== nothing
        for (c, conv) in enumerate(network.acdc_converters)
            if conv.variable_cost > 0
                for t in 1:hours
                    add_to_expression!(cost, conv.variable_cost,
                                       vars.acdc_rectify[c, t])
                    add_to_expression!(cost, conv.variable_cost,
                                       vars.acdc_invert[c, t])
                end
            end
            # Standby losses as fixed cost per hour
            if conv.standby_losses_mw > 0
                # Simple linear proxy: add small cost term
                cost += conv.standby_losses_mw * hours * conv.variable_cost
            end
        end
    end

    # Frequency converter variable costs
    if vars.freq_flow_a_to_b !== nothing && vars.freq_flow_b_to_a !== nothing
        for (c, conv) in enumerate(network.freq_converters)
            if conv.variable_cost > 0
                for t in 1:hours
                    add_to_expression!(cost, conv.variable_cost,
                                       vars.freq_flow_a_to_b[c, t])
                    add_to_expression!(cost, conv.variable_cost,
                                       vars.freq_flow_b_to_a[c, t])
                end
            end
            if conv.standby_losses_mw > 0
                cost += conv.standby_losses_mw * hours * conv.variable_cost
            end
        end
    end

    return cost
end
