"""
power_system.jl - PowerSystem optimization model

Implements the operational dispatch and unit commitment optimization
using JuMP. This is the core optimization model that handles:
- Generator dispatch and commitment
- Battery storage operation
- Transmission constraints (via TransmissionDC)
- Reserve requirements
- Renewable penetration targets
"""

# Note: JuMP, HiGHS, etc. are imported from the parent module (ESFEX.jl)
using JuMP: @variable, @constraint, @objective, @expression, Model
using JuMP: value, dual, has_values, has_duals, AffExpr, VariableRef
using JuMP: objective_value, termination_status, solve_time, optimizer_with_attributes
using JuMP: add_to_expression!, constraint_object

"""
    optimize_dispatch!(model; retry = NUMERICAL_RETRY_PARAMS[solver], label = "Dispatch")

Solve a dispatch window as `optimize_master!` solves a master
(`optimize_retrying!`): a solve that ends `NUMERICAL_ERROR` or
`LOCALLY_SOLVED` (Gurobi's `SUBOPTIMAL`) is solved once more with the
solver's retry parameters (Gurobi's homogeneous barrier), each solve of the
loss repair too, and a repair round whose re-solve is still not `OPTIMAL` is
undone. A window still `LOCALLY_SOLVED` keeps its feasible point (the Python
adapter warns, naming the window: `label`).
"""
optimize_dispatch!(model::Model; retry = numerical_retry_params(model),
                   label::AbstractString = "Dispatch") =
    optimize_retrying!(model, retry, label)

# =============================================================================
# CO2 intensity
# =============================================================================

rated_efficiency(gen::GeneratorConfig, b::Int) = gen.efficiency_rated[b]
rated_efficiency(tech::TechnologyConfig, b::Int) = tech.eff_at_rated[b]

"""
    co2_per_mwh(fuel_co2, unit, b)

Tonnes of CO2 per MWh of electricity from `unit` (generator or technology) at
bus `b`: the fuel's emission factor, per MWh of fuel (`fuels.*.emission_factor`),
over the unit's efficiency at rated power there. Without an efficiency (≤ 0)
the factor is taken as per MWh of electricity. 0 for fuels without a factor.
"""
function co2_per_mwh(fuel_co2::Dict{String, Float64}, unit, b::Int)
    ef = get(fuel_co2, unit.fuel, 0.0)
    ef == 0.0 && return 0.0
    eff = rated_efficiency(unit, b)
    return eff > 0 ? ef / eff : ef
end

# =============================================================================
# Temporal Upscaling Utilities
# =============================================================================

"""
    compute_upscaling_indices(hours, points_per_day)

Compute upscaled time indices for reduced-resolution variables.
Returns (indices, time_map) where:
- indices: Vector of hour indices where upscaled variables are defined
- time_map: Dict mapping each hour to its closest upscaled index
"""
function compute_upscaling_indices(hours::Int, points_per_day::Int)
    if points_per_day <= 0 || points_per_day >= 24
        # No upscaling - use all hours
        indices = collect(0:hours)
        time_map = Dict(h => h + 1 for h in 0:hours)
        return indices, time_map
    end

    # Calculate indices at regular intervals within each day
    interval = 24 ÷ points_per_day
    indices = Int[]

    num_days = (hours + 23) ÷ 24
    for day in 0:(num_days - 1)
        for point in 0:(points_per_day - 1)
            hour = day * 24 + point * interval
            if hour <= hours
                push!(indices, hour)
            end
        end
    end

    # Ensure final hour is included
    if hours ∉ indices
        push!(indices, hours)
    end

    # Create mapping from each hour to closest upscaled index
    time_map = Dict{Int, Int}()
    for h in 0:hours
        closest_idx = argmin(abs.(indices .- h))
        time_map[h] = closest_idx
    end

    return indices, time_map
end

"""
    create_power_system(input::PowerSystemInput)

Create and return a JuMP model with variables for the power system optimization.

Returns a tuple (model, vars) where:
- model: JuMP Model ready for optimization
- vars: PowerSystemVariables containing all decision variables
"""
function create_power_system(input::PowerSystemInput; custom_constraints=nothing)

    t_total = time()
    t0 = time()

    # Create model with configured optimizer
    optimizer = create_optimizer(
        solver_name=input.solver_name,
        threads=input.threads,
        time_limit=input.time_limit,
        gap=input.gap,
        verbose=input.verbose,
        solver_options=input.solver_options
    )
    model = solver_model(optimizer)
    t_optimizer = time() - t0

    # Build variables
    t0 = time()
    vars = build_variables!(model, input)
    t_variables = time() - t0

    # Build objective
    t0 = time()
    build_objective!(model, vars, input)
    t_objective = time() - t0

    # Build core constraints
    t0 = time()
    add_generator_constraints!(model, vars, input)
    t_gen_con = time() - t0

    t0 = time()
    add_battery_constraints!(model, vars, input)
    t_bat_con = time() - t0

    t0 = time()
    add_reservoir_constraints!(model, vars, input)
    t_reservoir = time() - t0

    t0 = time()
    add_reserve_constraints!(model, vars, input)
    t_reserve = time() - t0

    # EV fleet constraints (SOC dynamics, charge/V2G limits, mutual exclusivity)
    t0 = time()
    add_ev_constraints!(model, vars, input)
    t_ev = time() - t0

    # Electrolyzer power limits (rated power, ramp)
    add_electrolyzer_dispatch_constraints!(model, vars, input)

    # Add inertia constraints (if configured)
    t0 = time()
    add_inertia_constraints!(model, vars, input)
    t_inertia = time() - t0

    # Add curtailment definition (available_renewable - used_renewable)
    t0 = time()
    add_curtailment_constraints!(model, vars, input)
    t_curtailment = time() - t0

    # Add policy constraints
    t0 = time()
    add_renewable_constraint!(model, vars, input)
    add_co2_emissions_definition!(model, vars, input)  # Define CO2_emissions variable values
    add_co2_constraint!(model, vars, input)
    t_policy = time() - t0

    # Add power flow constraints based on selected mode
    # NOTE: For multi-node, KCL handles power balance
    # (including transmission flows, curtailment, and reserves)
    t0 = time()
    pf_mode = input.power_flow_mode
    is_acopf = startswith(pf_mode, "acopf_")

    t_transmission_build = 0.0
    if input.network.num_buses > 1
        if is_acopf
            # ACOPF formulations: SOC, QC, SDP, Polar NLP, Rectangular NLP
            # Losses are inherent in the AC formulation (no PWL needed).
            # Converter constraints are added inside setup_acopf!.
            acopf_vars = setup_acopf!(model, vars, input)
            vars.acopf_vars = acopf_vars
        else
            # DCOPF path (default): "dcopf" or "dcopf_ac_verify"
            t_tb = time()
            # Loss model: pwl_loss_segments > 0 → PWL, -1 → linear, 0 → lossless
            transmission = with_loss_model(TransmissionDC(input.network),
                input.pwl_loss_segments, input.network.s_base)
            t_transmission_build = time() - t_tb
            # Add converter variables/constraints BEFORE DC constraints
            # so KCL can reference converter flow variables
            add_converter_constraints!(model, vars, input)
            add_dc_constraints!(model, transmission, vars, input)
            add_line_capacity_constraints!(model, transmission, vars, input)
        end
    else
        # For single-bus, add simple power balance
        add_converter_constraints!(model, vars, input)
        add_demand_constraints!(model, vars, input)
    end
    t_power_flow = time() - t0

    # Converter variables exist now (every power-flow path above creates them)
    charge_converter_costs!(model, vars, input)

    # Add sectoral demand constraints (B8: sum sectoral LOL == total LOL, criticality ordering)
    t0 = time()
    add_sectoral_demand_constraints!(model, vars, input)
    t_sectoral = time() - t0

    # Add N-1 security constraints
    t0 = time()
    if input.n1_scopf_enabled
        add_scopf_constraints!(model, vars, input)
    else
        add_n1_security_constraints!(model, vars, input)
    end
    t_n1 = time() - t0

    # Per-MWh flow cost of lines that carry one (inter-system links)
    add_line_flow_costs!(model, vars, input)

    # User-defined constraints (declarative config + plugin overlays). Additive,
    # applied after all base constraints; no-op when none are supplied.
    apply_user_constraints!(model, vars, input, custom_constraints)

    t_build_total = time() - t_total

    # Build-timing / size diagnostics at @debug level (quiet by default;
    # enable with JULIA_DEBUG=ESFEX).
    n_vars = num_variables(model)
    n_cons = num_constraints(model; count_variable_in_set_constraints=false)
    @debug "PowerSystem build complete" n_buses=input.network.num_buses hours=input.temporal.hours n_variables=n_vars n_constraints=n_cons t_build=round(t_build_total, digits=2) t_variables=round(t_variables, digits=2) t_objective=round(t_objective, digits=2) t_gen_constraints=round(t_gen_con, digits=2) t_power_flow=round(t_power_flow, digits=2) t_n1=round(t_n1, digits=2)

    return model, vars
end

"""
    build_variables!(model, input::PowerSystemInput) -> PowerSystemVariables

Create all decision variables for the power system model.
"""
function build_variables!(model, input::PowerSystemInput)
    n_gen = length(input.generators)
    n_bat = length(input.batteries)
    n_bus = input.network.num_buses
    b2n = input.network.bus_to_node
    n_node = input.network.num_nodes
    hours = input.temporal.hours
    is_uc = input.mode == "unit_commitment"

    # =========================================================================
    # Pre-compute sparse lookup maps: which (gen, bus) and (bat, bus) pairs
    # have capacity.
    # =========================================================================
    buses_of_gen = [Int[] for _ in 1:n_gen]
    gens_at_bus = [Int[] for _ in 1:n_bus]
    for g in 1:n_gen
        gen = input.generators[g]
        for b in 1:n_bus
            if gen.rated_power[b] > 0
                push!(buses_of_gen[g], b)
                push!(gens_at_bus[b], g)
            end
        end
    end

    buses_of_bat = [Int[] for _ in 1:n_bat]
    bats_at_bus = [Int[] for _ in 1:n_bus]
    for bi in 1:n_bat
        bat = input.batteries[bi]
        for b in 1:n_bus
            has_cap = bat.max_charge_power[b] > 0 ||
                      bat.max_discharge_power[b] > 0 ||
                      bat.capacity[b] > 0
            if has_cap
                push!(buses_of_bat[bi], b)
                push!(bats_at_bus[b], bi)
            end
        end
    end

    # Generator output — SparseAxisArray (only active gen-bus pairs)
    gen_output = @variable(model, [g=1:n_gen, b=buses_of_gen[g], t=1:hours], lower_bound=0, base_name="gen")

    # Curtailment per node (node × hour) - spilled renewable energy
    @variable(model, curtailment[1:n_node, 1:hours] >= 0)

    # FRE penetration loss slack (node × hour) - for RE penetration constraint
    @variable(model, fre_penetration_loss[1:n_node, 1:hours] >= 0)


    # Generator status and startup variables — SparseAxisArray
    # Matches Python legacy (power_system.py lines 1184-1190)
    gen_startup = nothing
    gen_shutdown = nothing

    if is_uc
        # Unit commitment mode: gen_status is Binary, gen_startup is Continuous [0,1].
        # Only committed units have them: a variable renewable has no
        # on/off decision (left free, its status and start-ups were arbitrary).
        committed = [committed_buses(input.generators[g], buses_of_gen[g]) for g in 1:n_gen]
        gen_status = @variable(model, [g=1:n_gen, b=committed[g], t=1:hours], Bin, base_name="gen_status")
        gen_startup = @variable(model, [g=1:n_gen, b=committed[g], t=1:hours], lower_bound=0, upper_bound=1, base_name="gen_startup")
        # Clustered unit commitment: a plant of N > 1 identical units commits
        # an integer number of them (0..N) and starts up to N in a step
        for g in 1:n_gen, b in committed[g]
            N = units_of(input.generators[g], b)
            N > 1 || continue
            for t in 1:hours
                v = gen_status[g, b, t]
                unset_binary(v)
                set_integer(v)
                set_lower_bound(v, 0)
                set_upper_bound(v, N)
                set_upper_bound(gen_startup[g, b, t], N)
            end
        end
        # gen_shutdown stays nothing (matches Python which has no shutdown variable)
    else
        # Economic dispatch: gen_status is implicitly 1 for
        # every (g,b,t). We do NOT create the variable — that would inject
        # millions of trivial fixed-bound variables that survive presolve and
        # bloat the LP. Downstream code must check `is_uc` before referencing
        # gen_status, or use `nothing` as a sentinel.
        gen_status = nothing
    end

    # CO2 emissions (node × hour)
    @variable(model, co2_emissions[1:n_node, 1:hours] >= 0)

    # Battery variables — SparseAxisArray (only active bat-bus pairs)
    bat_charge = @variable(model, [bi=1:n_bat, b=buses_of_bat[bi], t=1:hours], lower_bound=0, base_name="bat_charge")
    bat_discharge = @variable(model, [bi=1:n_bat, b=buses_of_bat[bi], t=1:hours], lower_bound=0, base_name="bat_discharge")
    # bat_soc has hours+1 elements: index 1 = initial state, indices 2:(hours+1) = state after each hour
    bat_soc = @variable(model, [bi=1:n_bat, b=buses_of_bat[bi], t=1:(hours+1)], lower_bound=0, base_name="bat_soc")
    # Battery charge status for mutex constraint — SparseAxisArray
    bat_charge_status = @variable(model, [bi=1:n_bat, b=buses_of_bat[bi], t=1:hours], lower_bound=0, upper_bound=1, base_name="bat_cs")

    # SOC violation slack variable — SparseAxisArray
    soc_violation = @variable(model, [bi=1:n_bat, b=buses_of_bat[bi], t=1:hours], lower_bound=0, base_name="soc_viol")

    # Battery spillage variables — SparseAxisArray for batteries that allow spillage
    bat_spillage = nothing
    has_any_spillage = n_bat > 0 && any(bat.spillage for bat in input.batteries)
    if has_any_spillage
        # Create spillage variables only for batteries that allow it, at active buses
        bat_spillage_var = Array{Union{VariableRef, Nothing}, 3}(nothing, n_bat, n_bus, hours)
        for bi in 1:n_bat
            if input.batteries[bi].spillage
                for b in buses_of_bat[bi], t in 1:hours
                    bat_spillage_var[bi, b, t] = @variable(model, lower_bound=0,
                        base_name="bat_spillage_$(bi)_$(b)_$(t)")
                end
            end
        end
        bat_spillage = bat_spillage_var
    end

    # Transmission: flow variables are created per line in TransmissionDC,
    # which also fills the bus pair → lines map
    pair_lines = Dict{Tuple{Int,Int}, Vector{Int}}()

    # Voltage angles (bus × hour), bounded to [-π, π] rad
    @variable(model, -π <= voltage_angle[1:n_bus, 1:hours] <= π)

    # Transfer margin violation (from × to × hour)
    # Slack variable for transmission capacity violations with penalty.
    # Only create when DC-OPF is NOT active — when DC-OPF handles line flows,
    # these unconstrained slack variables are always zero (pure model bloat).
    transfer_margin = Dict{Tuple{Int,Int}, Vector{VariableRef}}()
    if isempty(input.network.transmission_lines)
        for i in 1:n_bus
            for j in 1:n_bus
                if i != j && get(input.network.connections, (i, j), 0) > 0
                    transfer_margin[(i, j)] = @variable(model, [1:hours], lower_bound=0,
                        base_name="transfer_margin_$(i)_$(j)")
                end
            end
        end
    end

    # Reserve variables (node × hour) — aggregated at geographic node level
    @variable(model, reserve_static[1:n_node, 1:hours] >= 0)
    @variable(model, reserve_dynamic[1:n_node, 1:hours] >= 0)
    @variable(model, reserve_static_loss[1:n_node, 1:hours] >= 0)
    @variable(model, reserve_dynamic_loss[1:n_node, 1:hours] >= 0)

    # Load shedding (bus × hour).
    #
    # NOTE: load_shed is per-BUS, not per-node.  A per-node load_shed
    # combined with the bus-level KCL `+ load_shed_n * bus_df_b` acts
    # as a free slack at every load bus: the per-bus dual (LMP) at any
    # load bus collapses to 0 because the LP can absorb 1 MW of demand
    # there by adding ~1/bus_df units to the node variable, distributing
    # the cost across all sibling buses simultaneously.  With LMP = 0
    # at load buses, generators with any positive marginal cost cannot
    # profitably dispatch even when shed at the node level costs VOLL.
    # Declaring shed per-bus gives each load bus its own price signal
    # so generation→demand routing is incentivised.
    @variable(model, load_shed[1:n_bus, 1:hours] >= 0)

    # Sectoral load shedding (sector -> bus × hour)
    # Created for each sector in sectoral_demand if available
    loss_of_load_sectoral = nothing
    if !isempty(input.sectoral_demand)
        loss_of_load_sectoral = Dict{String, Matrix{VariableRef}}()
        for sector in keys(input.sectoral_demand)
            loss_of_load_sectoral[sector] = @variable(model, [1:n_node, 1:hours],
                lower_bound=0, base_name="lol_$(sector)")
        end
    end
    # Flexible demand is shifted (below), not curtailed: cutting demand is
    # sectoral load shedding, priced by criticality.

    # Demand shifting: shift[(node, t, t_dest)] MW of a flexible sector's demand
    # moved from step t to t_dest, at most delay_tolerance hours away. A shift
    # never crosses the end of the committed part of a rolling window (the last
    # hours - overlap steps), or demand pushed into the overlap would be lost
    # when the next window restarts from the original profile.
    demand_shift = nothing
    tres_shift = input.temporal.resolution_hours > 0 ? input.temporal.resolution_hours : 1
    committed = max(1, hours - input.temporal.overlap_hours ÷ tres_shift)
    for (sector, ratio) in input.sectoral_flexibility_ratio
        (ratio > 0 && haskey(input.sectoral_demand, sector)) || continue
        delay = get(input.sectoral_delay_tolerance, sector, 0) ÷ tres_shift
        delay > 0 || continue
        demand_shift === nothing && (demand_shift = Dict{String, Dict{Tuple{Int,Int,Int}, VariableRef}}())
        sector_vars = Dict{Tuple{Int,Int,Int}, VariableRef}()
        for ni in 1:n_node, t in 1:hours
            for t_dest in max(1, t - delay):min(hours, t + delay)
                (t_dest == t || (t <= committed) != (t_dest <= committed)) && continue
                sector_vars[(ni, t, t_dest)] = @variable(model, lower_bound=0,
                    base_name="shift_$(sector)_$(ni)_$(t)_$(t_dest)")
            end
        end
        demand_shift[sector] = sector_vars
    end

    # EV variables (optional, only if ev_config is provided)
    # Node-level (not bus-level) to reduce variable count for large networks.
    # In power balance, scaled by bus demand_fraction (same as load_shed, reserves).
    ev_charging = nothing
    ev_v2g = nothing
    ev_soc = nothing
    ev_loss = nothing
    ev_charge_status = nothing
    if input.ev_config !== nothing
        @variable(model, ev_charging_var[1:n_node, 1:hours] >= 0)
        @variable(model, ev_v2g_var[1:n_node, 1:hours] >= 0)
        @variable(model, ev_soc_var[1:n_node, 1:(hours+1)] >= 0)
        @variable(model, ev_loss_var[1:n_node, 1:hours] >= 0)
        # Charge/discharge mutual exclusivity status [0,1] continuous relaxation
        @variable(model, 0 <= ev_charge_status_var[1:n_node, 1:hours] <= 1)
        ev_charging = ev_charging_var
        ev_v2g = ev_v2g_var
        ev_soc = ev_soc_var
        ev_loss = ev_loss_var
        ev_charge_status = ev_charge_status_var
    end

    # Electrolyzer power variable (optional)
    electrolyzer_power = nothing
    if !isempty(input.electrolyzers)
        @variable(model,
            electrolyzer_power_var[1:length(input.electrolyzers), 1:n_bus, 1:hours] >= 0)
        electrolyzer_power = electrolyzer_power_var
    end

    # Loss of inertia variable (for inertia constraint)
    loss_of_inertia = nothing
    if input.inertia_limit > 0 || !isempty(input.inertia_limit_hourly)
        @variable(model, loss_of_inertia_var[1:hours] >= 0)
        loss_of_inertia = loss_of_inertia_var
    end

    # CO2 budget violation slack: one variable (t over budget) per budget
    # group that has a budget. A single group keeps the plain name.
    co2_budget_violation = nothing
    co2_groups = co2_budget_groups(input)
    for (k, grp) in enumerate(co2_groups)
        has_co2_budget(grp) || continue
        co2_budget_violation === nothing && (co2_budget_violation = Dict{Int, VariableRef}())
        co2_budget_violation[k] = @variable(model, lower_bound = 0,
            base_name = length(co2_groups) == 1 ? "co2_budget_violation_var" :
                        "co2_budget_violation_var[$(grp.name)]")
    end

    # Rooftop solar curtailment (node × hour)
    rooftop_curtailment = nothing
    if input.rooftop_generation !== nothing
        @variable(model, rooftop_curtailment_var[1:n_node, 1:hours] >= 0)
        rooftop_curtailment = rooftop_curtailment_var
    end

    # Constraint references for dual extraction (populated by add_demand_constraints!)
    balance_constraints = Dict{Tuple{Int,Int}, Any}()

    # Reservoir hydroelectric variables (only for generators with reservoir_capacity > 0)
    # Use buses_of_gen for sparse indexing (reservoirs are generator-associated)
    reservoir_level = nothing
    reservoir_spillage = nothing
    reservoir_pump = nothing
    has_any_reservoir = any(any(g.reservoir_capacity .> 0) for g in input.generators)
    if has_any_reservoir
        # Level has hours+1 (initial + each hour end state) — SparseAxisArray
        reservoir_level = @variable(model, [g=1:n_gen, b=buses_of_gen[g], t=1:(hours+1)], lower_bound=0, base_name="res_level")
        reservoir_spillage = @variable(model, [g=1:n_gen, b=buses_of_gen[g], t=1:hours], lower_bound=0, base_name="res_spill")
        reservoir_pump = @variable(model, [g=1:n_gen, b=buses_of_gen[g], t=1:hours], lower_bound=0, base_name="res_pump")
    end

    # =========================================================================
    # Segment variables for PWL (bidding curve) cost decomposition
    # Created for every generator and battery with a cost curve: a single
    # segment prices the output (discharge) at its own marginal cost, not at
    # fuel_cost (throughput_degradation_cost).
    # =========================================================================
    gen_seg_output = Dict{Tuple{Int,Int}, Any}()
    # A curve at a bus where the unit has no output variable (no capacity
    # there) is skipped, as in the master.
    for (g, bus_curves) in input.gen_cost_curves
        for (b, segs) in bus_curves
            if !isempty(segs) && b in buses_of_gen[g]
                n_seg = length(segs)
                sv = @variable(model, [k=1:n_seg, t=1:hours],
                               lower_bound=0, base_name="gseg_$(g)_$(b)")
                gen_seg_output[(g, b)] = sv
                # Link: gen_output[g,b,t] = sum of segment outputs
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
                               lower_bound=0, base_name="bseg_$(bi)_$(b)")
                bat_seg_discharge[(bi, b)] = sv
                # Link: bat_discharge[bi,b,t] = sum of segment outputs
                for t in 1:hours
                    @constraint(model, bat_discharge[bi, b, t] ==
                        sum(sv[k, t] for k in 1:n_seg))
                end
            end
        end
    end

    return PowerSystemVariables(
        gen_output, gen_status, gen_startup, gen_shutdown,
        curtailment, fre_penetration_loss,
        bat_charge, bat_discharge, bat_soc,
        pair_lines, voltage_angle,
        reserve_static, reserve_dynamic, reserve_static_loss, reserve_dynamic_loss,
        load_shed,
        buses_of_gen, gens_at_bus, buses_of_bat, bats_at_bus;
        co2_emissions = co2_emissions,
        bat_charge_status = bat_charge_status,
        soc_violation = soc_violation,
        bat_spillage = bat_spillage,
        transfer_margin = transfer_margin,
        ev_charging = ev_charging,
        ev_v2g = ev_v2g,
        ev_soc = ev_soc,
        ev_loss = ev_loss,
        electrolyzer_power = electrolyzer_power,
        loss_of_inertia = loss_of_inertia,
        balance_constraints = balance_constraints,
        loss_of_load_sectoral = loss_of_load_sectoral,
        co2_budget_violation = co2_budget_violation,
        rooftop_curtailment = rooftop_curtailment,
        ev_charge_status = ev_charge_status,
        demand_shift = demand_shift,
        reservoir_level = reservoir_level,
        reservoir_spillage = reservoir_spillage,
        reservoir_pump = reservoir_pump,
        gen_seg_output = gen_seg_output,
        bat_seg_discharge = bat_seg_discharge
    )
end

"""
    build_objective!(model, vars::PowerSystemVariables, input::PowerSystemInput)

Build the objective function: minimize total system cost.

Cost components:
1. Operational costs: fuel and maintenance per MWh; fixed O&M per MW·year
   of the capacity in service, prorated to the window
2. Startup costs (unit commitment mode)
3. Penalty costs: load shedding, curtailment, reserve shortage
4. CO2 costs

The dispatch builds nothing: in development runs the master's builds reach it
as existing capacity.
"""
function build_objective!(model, vars::PowerSystemVariables, input::PowerSystemInput)
    n_gen = length(input.generators)
    n_bat = length(input.batteries)
    n_bus = input.network.num_buses
    hours = input.temporal.hours

    # Temporal resolution: each timestep represents this many hours of energy.
    # All energy-based costs ($/MWh × MW × timesteps) must scale by this factor
    # to convert from per-timestep power to actual energy (MWh).
    temporal_resolution_hours = input.temporal.resolution_hours > 0 ? input.temporal.resolution_hours : 1

    # ==========================================================================
    # Operational Costs
    # ==========================================================================

    # Fuel costs ($/MWh × MWh) — sparse iteration over active (gen, bus) pairs
    # When a generator has a bidding curve, the cost is applied per segment
    # (PWL) instead of fuel_cost. Generators without one use the original path.
    fuel_cost = AffExpr(0.0)
    for g in 1:n_gen
        for b in vars.buses_of_gen[g]
            if haskey(vars.gen_seg_output, (g, b))
                # Bidding-curve (PWL) cost
                segs = input.gen_cost_curves[g][b]
                seg_vars = vars.gen_seg_output[(g, b)]
                for k in 1:length(segs), t in 1:hours
                    add_to_expression!(fuel_cost,
                        segs[k].marginal_cost, seg_vars[k, t])
                end
            else
                # Flat cost (original path — zero overhead)
                for t in 1:hours
                    add_to_expression!(fuel_cost,
                        input.generators[g].fuel_cost[b], vars.gen_output[g, b, t])
                end
            end
        end
    end

    # Maintenance costs ($/MWh × MWh) — sparse iteration
    maintenance_cost = AffExpr(0.0)
    for g in 1:n_gen
        for b in vars.buses_of_gen[g], t in 1:hours
            add_to_expression!(maintenance_cost,
                input.generators[g].maintenance_cost[b], vars.gen_output[g, b, t])
        end
    end

    # ==========================================================================
    # Battery Maintenance Cost
    # Cost = (charge + discharge) * maintenance_cost for each battery
    # ==========================================================================
    # Battery maintenance cost — sparse iteration over active (bat, bus) pairs
    battery_maintenance_cost = AffExpr(0.0)
    for bi in 1:n_bat
        for b in vars.buses_of_bat[bi], t in 1:hours
            mc = input.batteries[bi].maintenance_cost[b]
            add_to_expression!(battery_maintenance_cost, mc, vars.bat_charge[bi, b, t])
            add_to_expression!(battery_maintenance_cost, mc, vars.bat_discharge[bi, b, t])
        end
    end

    # ==========================================================================
    # Battery Throughput Degradation Cost
    # Cost = discharge * throughput_degradation_cost for each battery
    # ==========================================================================
    # Battery throughput degradation cost — sparse iteration
    # When a battery has a discharge cost curve, apply it per segment.
    battery_throughput_degradation_cost = AffExpr(0.0)
    for bi in 1:n_bat
        for b in vars.buses_of_bat[bi]
            if haskey(vars.bat_seg_discharge, (bi, b))
                segs = input.bat_cost_curves[bi][b]
                seg_vars = vars.bat_seg_discharge[(bi, b)]
                for k in 1:length(segs), t in 1:hours
                    add_to_expression!(battery_throughput_degradation_cost,
                        segs[k].marginal_cost, seg_vars[k, t])
                end
            else
                for t in 1:hours
                    add_to_expression!(battery_throughput_degradation_cost,
                        input.batteries[bi].throughput_degradation_cost[b],
                        vars.bat_discharge[bi, b, t])
                end
            end
        end
    end

    # ==========================================================================
    # Startup Costs (UC mode only)
    # ==========================================================================
    startup_cost = AffExpr(0.0)
    if vars.gen_startup !== nothing
        for g in 1:n_gen
            gen = input.generators[g]
            for b in committed_buses(gen, vars.buses_of_gen[g]), t in 1:hours
                # start_up_cost starts the whole plant: one of its N units
                # costs start_up_cost / N
                add_to_expression!(startup_cost,
                    gen.start_up_cost[b] / units_of(gen, b), vars.gen_startup[g, b, t])
            end
        end
    end

    # ==========================================================================
    # Penalty Costs
    # ==========================================================================

    # Load shedding penalty (VOLL × MWh) — node-level
    #
    # With a sectoral decomposition, the sectors' shed is priced at
    # VOLL × criticality below (cuba.yaml: medium=10, high=100,
    # critical=1000); only the unclassified remainder
    # (load_shed − Σ sectoral shed, see add_sectoral_demand_constraints!)
    # is priced here, at VOLL × the node's demand-weighted mean criticality
    # (sector_remainder_multiplier). Pricing all of load_shed here would
    # double-count every sectoral MWh.
    n_node = input.network.num_nodes
    has_sectoral = vars.loss_of_load_sectoral !== nothing &&
                   !isempty(vars.loss_of_load_sectoral)
    load_shed_cost = @expression(model,
        input.loss_of_load_penalty *
        sum(vars.load_shed[b, t] for b in 1:n_bus, t in 1:hours)
    )
    if has_sectoral
        voll = input.loss_of_load_penalty
        sectors = collect(keys(vars.loss_of_load_sectoral))
        buses_of_node = [Int[] for _ in 1:n_node]
        for b in 1:n_bus
            push!(buses_of_node[input.network.bus_to_node[b]], b)
        end
        for n in 1:n_node, t in 1:hours
            m = sector_remainder_multiplier(sectors, input.sectoral_criticality,
                                            s -> _sectoral_demand_at(input, s, t, n))
            for b in buses_of_node[n]
                add_to_expression!(load_shed_cost, voll * (m - 1.0), vars.load_shed[b, t])
            end
            for lol_vars in values(vars.loss_of_load_sectoral)
                add_to_expression!(load_shed_cost, -voll * m, lol_vars[n, t])
            end
        end
    end


    # Curtailment penalty in the objective.
    #
    # Original design (left as comment for context): "Curtailment has zero
    # penalty in the objective. Limiting curtailment is handled by the
    # max_curtailment_ratio constraint in the master problem." That constraint
    # is added per-rep-day (master_problem.jl:2160) but empirically does not
    # bind for the full-year operational LP — runs from late 2042 onwards show
    # 40–65% annual curtailment vs the configured 5% ratio, despite every day
    # having an identical solar profile (so master and operational should
    # agree). Until that disconnect is root-caused, expose the configured
    # `curtailment_cost` ($/MWh, ~$20 in cuba.yaml) directly in the operational
    # objective so the LP and the master (via NPV) trade curtailment against
    # storage capex on an explicit economic basis.
    curtailment_cost = @expression(model,
        input.curtailment_cost *
        sum(vars.curtailment[ni, t] for ni in 1:n_node, t in 1:hours)
    )

    # Static reserve shortage penalty — node-level
    reserve_static_cost = @expression(model,
        input.loss_of_reserve_static *
        sum(vars.reserve_static_loss[n, t] for n in 1:n_node, t in 1:hours)
    )

    # Dynamic reserve shortage penalty — node-level
    reserve_dynamic_cost = @expression(model,
        input.loss_of_reserve_dynamic *
        sum(vars.reserve_dynamic_loss[n, t] for n in 1:n_node, t in 1:hours)
    )

    # ==========================================================================
    # CO2 Costs
    # ==========================================================================
    co2_cost_expr = AffExpr(0.0)
    if input.co2_cost > 0
        # CO2 cost based on fuel type and emissions — sparse iteration
        for g in 1:n_gen, b in vars.buses_of_gen[g]
            coeff = input.co2_cost * co2_per_mwh(input.fuel_co2, input.generators[g], b)
            coeff == 0.0 && continue
            for t in 1:hours
                add_to_expression!(co2_cost_expr, coeff, vars.gen_output[g, b, t])
            end
        end
    end

    # ==========================================================================
    # Inertia Loss Penalty
    # ==========================================================================
    inertia_cost = AffExpr(0.0)
    if vars.loss_of_inertia !== nothing
        inertia_cost = @expression(model,
            input.loss_of_inertia_penalty * sum(vars.loss_of_inertia[t] for t in 1:hours)
        )
    end

    # ==========================================================================
    # EV Loss Penalty (for not meeting target SOC)
    # ==========================================================================
    ev_loss_cost = AffExpr(0.0)
    if vars.ev_loss !== nothing && input.ev_config !== nothing
        ev_loss_cost = @expression(model,
            input.ev_config.loss_penalty * sum(vars.ev_loss[n, t] for n in 1:n_node, t in 1:hours)
        )
    end

    # ==========================================================================
    # FRE Penetration Loss Penalty
    # Penalizes the slack on the RE-penetration target so the dispatch has an
    # incentive to use available renewable energy. Charged at the configured
    # per-MWh penalty; temporal_resolution_hours is applied globally in the
    # objective, so penalty × Σslack × tres recovers the true $/MWh energy cost.
    # ==========================================================================
    # Use config-based FRE penalty or fallback to a safe default (100 $/MWh).
    # Avoid fallback formula lolp*hours*100 which yields 1e9 when lolp=10M, causing
    # numerical instability in the simplex solver (dual ratio test failure).
    fre_penalty = if input.fre_penetration_penalty > 0
        input.fre_penetration_penalty
    else
        @warn "fre_penetration_penalty not configured, using default 1e-4 M\$/MWh (= 100 \$/MWh)"
        1e-4
    end
    # fre_penetration_loss is the RE-target slack (MW); see the
    # re_penetration_target constraint. Charged at the plain per-MWh penalty
    # (matches the master problem's convention).
    fre_penetration_cost = @expression(model,
        fre_penalty *
        sum(vars.fre_penetration_loss[n, t] for n in 1:n_node, t in 1:hours)
    )

    # ==========================================================================
    # SOC Violation Penalty
    # Very high penalty (1e6) to enforce SOC limits as soft constraint
    # ==========================================================================
    soc_violation_cost = AffExpr(0.0)
    if n_bat > 0 && vars.soc_violation !== nothing
        SOC_VIOLATION_PENALTY = input.soc_violation_penalty
        for bi in 1:n_bat
            for b in vars.buses_of_bat[bi], t in 1:hours
                add_to_expression!(soc_violation_cost, SOC_VIOLATION_PENALTY, vars.soc_violation[bi, b, t])
            end
        end
    end

    # ==========================================================================
    # Transfer Margin Penalty
    # Penalty for transmission capacity violations
    # ==========================================================================
    transfer_margin_cost = AffExpr(0.0)
    if vars.transfer_margin !== nothing && !isempty(vars.transfer_margin)
        # Use config-based transfer margin penalty or fallback to 10% of VOLL
        transfer_penalty = if input.transfer_margin_penalty > 0
            input.transfer_margin_penalty
        else
            fallback = input.loss_of_load_penalty * 0.1
            @warn "transfer_margin_penalty not configured, using 10% of VOLL = $fallback"
            fallback
        end
        for ((i, j), margin_vars) in vars.transfer_margin
            for t in 1:hours
                add_to_expression!(transfer_margin_cost, margin_vars[t], transfer_penalty)
            end
        end
    end

    # ==========================================================================
    # V2G Compensation
    # Payment to EV owners for V2G energy. It is a transfer between agents,
    # not a system cost: the benefit of V2G is already the generation it
    # displaces in the power balance. Reported only, never in the objective.
    # ==========================================================================
    v2g_compensation = AffExpr(0.0)
    if vars.ev_v2g !== nothing && input.ev_config !== nothing
        has_elec_price_v2g = !isempty(input.electricity_price) && length(input.electricity_price) >= hours
        if has_elec_price_v2g
            # Time-varying V2G compensation using electricity price (matches Python legacy)
            for n in 1:n_node, t in 1:hours
                add_to_expression!(v2g_compensation, input.electricity_price[t], vars.ev_v2g[n, t])
            end
        elseif input.ev_config.v2g_compensation > 0
            # Fallback to constant compensation
            v2g_compensation = @expression(model,
                input.ev_config.v2g_compensation * sum(vars.ev_v2g[n, t]
                    for n in 1:n_node, t in 1:hours)
            )
        end
    end

    # ==========================================================================
    # Sectoral Load Shedding Penalties
    # Weighted by sector criticality (higher criticality = higher penalty)
    # ==========================================================================
    sectoral_load_shed_cost = AffExpr(0.0)
    if vars.loss_of_load_sectoral !== nothing && !isempty(vars.loss_of_load_sectoral)
        for (sector, lol_vars) in vars.loss_of_load_sectoral
            # Get criticality weight for this sector (default 1.0)
            criticality = get(input.sectoral_criticality, sector, 1.0)
            sector_penalty = input.loss_of_load_penalty * criticality
            for n in 1:n_node, t in 1:hours
                add_to_expression!(sectoral_load_shed_cost, lol_vars[n, t], sector_penalty)
            end
        end
    end

    # ==========================================================================
    # CO2 Budget Violation Penalty
    # Penalty for exceeding CO2 budget - default 500 per tonne
    # ==========================================================================
    # The violation is in tonnes, not per step, but this cost sits with the
    # energy costs that are multiplied by the step length: divide it back out.
    # Each budget group pays its own penalty.
    co2_budget_violation_cost = AffExpr(0.0)
    if vars.co2_budget_violation !== nothing
        co2_groups = co2_budget_groups(input)
        for (k, viol) in vars.co2_budget_violation
            add_to_expression!(co2_budget_violation_cost,
                co2_groups[k].violation_penalty / temporal_resolution_hours, viol)
        end
    end

    # ==========================================================================
    # Rooftop Solar Curtailment Penalty
    # Penalty for curtailing rooftop solar
    # ==========================================================================
    rooftop_curtailment_cost = AffExpr(0.0)
    if vars.rooftop_curtailment !== nothing
        # Use config-based rooftop curtailment penalty (default 5.0)
        rooftop_curtailment_cost = @expression(model,
            input.rooftop_curtailment_penalty * sum(vars.rooftop_curtailment[n, t]
                for n in 1:n_node, t in 1:hours)
        )
    end

    # ==========================================================================
    # Battery Spillage Cost
    # Cost of spilling energy without grid injection - opportunity cost
    # Matches Python: bat_spillage * electricity_price[t]
    # ==========================================================================
    spillage_cost = AffExpr(0.0)
    if vars.bat_spillage !== nothing
        # Check if electricity_price is available; if not, use fallback
        has_elec_price = !isempty(input.electricity_price) && length(input.electricity_price) >= hours
        fallback_price = input.loss_of_load_penalty * 0.01  # Fallback if no price vector

        for bi in 1:n_bat
            if input.batteries[bi].spillage
                for b in vars.buses_of_bat[bi], t in 1:hours
                    if vars.bat_spillage[bi, b, t] !== nothing
                        # Use time-varying electricity price if available (matches Python line 1763)
                        price_t = has_elec_price ? input.electricity_price[t] : fallback_price
                        add_to_expression!(spillage_cost,
                            vars.bat_spillage[bi, b, t], price_t)
                    end
                end
            end
        end
    end

    # Converter variable costs are charged by charge_converter_costs! once
    # add_converter_constraints! has created the converter variables (after
    # this function); building them here gave an empty expression.

    # ==========================================================================
    # Electrolyzer Costs (A4 fix: was computed but not added to objective)
    # Variable cost on power consumption
    # ==========================================================================
    electrolyzer_cost = AffExpr(0.0)
    # Fixed O&M ($/MW·year × rated power, the master's builds included): an
    # annual cost, prorated to the window below like the generators'
    electrolyzer_fixed_cost = AffExpr(0.0)
    if vars.electrolyzer_power !== nothing
        for (e, config) in enumerate(input.electrolyzers)
            for b in 1:n_bus, t in 1:hours
                if length(config.variable_cost) >= b
                    add_to_expression!(electrolyzer_cost, config.variable_cost[b],
                                       vars.electrolyzer_power[e, b, t])
                end
                # Water cost ($/kg H2) on the hydrogen produced (#89)
                if config.water_cost != 0 && length(config.eff_at_rated) >= b
                    add_to_expression!(electrolyzer_cost,
                        config.water_cost * electrolyzer_h2_per_mwh(config, b),
                        vars.electrolyzer_power[e, b, t])
                end
            end
            for b in 1:n_bus
                if length(config.rated_power) >= b && length(config.fixed_cost) >= b
                    add_to_expression!(electrolyzer_fixed_cost,
                                       config.rated_power[b] * config.fixed_cost[b])
                end
            end
        end
    end

    # ==========================================================================
    # Demand Shifting Cost: demand_shift_cost_rate per MWh and per hour moved
    # (an energy cost, so the MW variable is multiplied by Δt with the others)
    # ==========================================================================
    demand_shift_cost = AffExpr(0.0)
    if vars.demand_shift !== nothing && !isempty(vars.demand_shift)
        for (sector, shift_vars) in vars.demand_shift
            for ((b, t, t_dest), var) in shift_vars
                add_to_expression!(demand_shift_cost, var,
                    abs(t - t_dest) * temporal_resolution_hours * input.demand_shift_cost_rate)
            end
        end
    end

    # ==========================================================================
    # Reservoir Spillage Cost (opportunity cost of wasted water)
    # ==========================================================================
    reservoir_spillage_cost = AffExpr(0.0)
    if vars.reservoir_spillage !== nothing
        has_elec_price_res = !isempty(input.electricity_price) && length(input.electricity_price) >= hours
        fallback_price_res = input.loss_of_load_penalty * 0.01
        for g in 1:n_gen
            gen = input.generators[g]
            if any(gen.reservoir_capacity .> 0) && gen.reservoir_spillage_allowed
                for b in vars.buses_of_gen[g], t in 1:hours
                    if gen.reservoir_capacity[b] > 0
                        price_t = has_elec_price_res ? input.electricity_price[t] : fallback_price_res
                        add_to_expression!(reservoir_spillage_cost,
                            vars.reservoir_spillage[g, b, t], price_t)
                    end
                end
            end
        end
    end

    # Fixed O&M of generators and batteries ($/MW/year × power in service):
    # an annual cost, prorated to the window below like the other
    # non-energy costs
    fixed_om_cost = fixed_om_annual_cost(vars, input)

    # ==========================================================================
    # Total Objective
    # Each timestep represents temporal_resolution_hours hours of energy.
    # Energy-based costs ($/MWh × MW) must be scaled by this factor.
    # Non-energy costs (annual $/MW, retirement) are NOT scaled.
    # ==========================================================================
    energy_costs = (
        fuel_cost + maintenance_cost +
        battery_maintenance_cost + battery_throughput_degradation_cost +
        load_shed_cost + curtailment_cost +
        reserve_static_cost + reserve_dynamic_cost +
        co2_cost_expr +
        inertia_cost + ev_loss_cost + fre_penetration_cost +
        soc_violation_cost + transfer_margin_cost +
        sectoral_load_shed_cost +
        co2_budget_violation_cost + rooftop_curtailment_cost + spillage_cost +
        electrolyzer_cost +
        demand_shift_cost +
        reservoir_spillage_cost
    )
    # Scale non-energy (one-shot annual) costs by the window's fraction
    # of the year, so that summing operational rolling-window objectives
    # across the year recovers the full annual cost exactly once.
    # Without this scaling each of ~61 windows adds the full non-energy
    # cost block, inflating reported objectives by ~50–60×.
    # For master typical-day dispatch (own objective), this code path is
    # not hit so master's investment costing is unaffected.
    annual_hours = Float64(input.hours_per_year)
    window_real_hours = Float64(hours * temporal_resolution_hours)
    non_energy_window_fraction = annual_hours > 0 ?
        window_real_hours / annual_hours : 1.0
    non_energy_costs = fixed_om_cost + electrolyzer_fixed_cost
    # Start-ups are events inside the window: charged in full, neither per
    # MWh nor prorated (they were prorated × window/year like the annual costs)
    @objective(model, Min,
        temporal_resolution_hours * energy_costs +
        non_energy_window_fraction * non_energy_costs +
        startup_cost
    )

    # Store individual cost expressions for granular extraction after solve.
    # Energy-based costs will be scaled by temporal_resolution_hours at extraction time.
    model.ext[:cost_expressions] = Dict{Symbol, Any}(
        :fuel_cost => fuel_cost,
        # Window share of the annual fixed O&M (not per step: not × tres)
        :fixed_om_cost => non_energy_window_fraction * fixed_om_cost,
        :maintenance_cost => maintenance_cost,
        :startup_cost => startup_cost,
        :battery_maintenance_cost => battery_maintenance_cost,
        :battery_degradation_cost => battery_throughput_degradation_cost,
        :load_shedding_cost => load_shed_cost + sectoral_load_shed_cost,
        :curtailment_cost => curtailment_cost + rooftop_curtailment_cost,
        :reserve_static_cost => reserve_static_cost,
        :reserve_dynamic_cost => reserve_dynamic_cost,
        :co2_emission_cost => co2_cost_expr + co2_budget_violation_cost,
        :fre_penetration_cost => fre_penetration_cost,
        :inertia_cost => inertia_cost + ev_loss_cost,
        :soc_violation_cost => soc_violation_cost,
        :transfer_margin_cost => transfer_margin_cost,
        :v2g_compensation => v2g_compensation,
        # An energy term (× step length at extraction): the window's share of
        # the annual fixed cost is divided by it to come out unscaled
        :electrolyzer_cost => electrolyzer_cost +
            (non_energy_window_fraction / temporal_resolution_hours) * electrolyzer_fixed_cost,
        :converter_cost => AffExpr(0.0),   # set by charge_converter_costs!
        :spillage_cost => spillage_cost,
        :reservoir_spillage_cost => reservoir_spillage_cost,
        :demand_shift_cost => demand_shift_cost,
        :rooftop_curtailment_cost => rooftop_curtailment_cost,
        # Added by add_n1_security_constraints!, which runs after this.
        :n1_security_shortfall_cost => AffExpr(0.0),
    )
    model.ext[:temporal_resolution_hours] = temporal_resolution_hours
end

"""
    add_demand_constraints!(model, vars::PowerSystemVariables, input::PowerSystemInput)

Add power balance (demand satisfaction) constraints at each node.
Stores constraint references in vars.balance_constraints for dual extraction.

Matches legacy PowerSystem.py power balance:
  inflow + gen_output + bat_discharge == outflow + demand + bat_charge + reserves + curtailment

For single-node systems, adds full power balance directly.
For multi-node systems, the KCL in add_dc_constraints! handles the balance including
transmission flows, so we skip the simple balance here.
"""
function add_demand_constraints!(model, vars::PowerSystemVariables, input;
    extra_injections_fn::Union{Nothing, Function} = nothing)
    n_gen = length(input.generators)
    n_bat = length(input.batteries)
    n_bus = input.network.num_buses
    hours = input.temporal.hours

    # Callers (create_power_system, master_problem.jl) choose DCOPF
    # (add_dc_constraints!) vs copper-plate balance; this function MUST add
    # balance constraints unconditionally.

    shift_net = demand_shift_net(vars)

    for t in 1:hours
        for b in 1:n_bus
            # Total generation at bus — sparse: only generators with capacity at this bus
            total_gen = @expression(model,
                sum(vars.gen_output[g, b, t] for g in vars.gens_at_bus[b]; init=AffExpr(0.0))
            )

            # Battery discharge and charge — sparse: only batteries at this bus
            bat_discharge = @expression(model,
                sum(vars.bat_discharge[bi, b, t] for bi in vars.bats_at_bus[b]; init=AffExpr(0.0))
            )

            bat_charge_sum = @expression(model,
                sum(vars.bat_charge[bi, b, t] for bi in vars.bats_at_bus[b]; init=AffExpr(0.0))
            )

            # Node index and demand fraction for node-level variable scaling.
            # Connection buses (role="connection") carry no demand: bus_df is
            # forced to 0 so all demand-side terms (load_shed, reserves, EV,
            # rooftop, demand) drop out of the bus-level KCL.
            ni = input.network.bus_to_node[b]
            bus_role = input.network.buses[b].role
            is_load_bus = bus_role == "load" || bus_role == "mixed"
            bus_df = is_load_bus ? input.network.buses[b].demand_fraction : 0.0

            # EV V2G (supply side) and charging (demand side)
            # Node-level variables scaled by demand_fraction (same as load_shed, reserves)
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
                electrolyzer_load(vars, b, t)
            else
                AffExpr(0.0)
            end

            # Reservoir pump power (demand side — pumping consumes electricity)
            reservoir_pump_term = AffExpr(0.0)
            if vars.reservoir_pump !== nothing
                for g in vars.gens_at_bus[b]
                    gen = input.generators[g]
                    if gen.reservoir_capacity[b] > 0 && gen.reservoir_pump_capacity[b] > 0
                        add_to_expression!(reservoir_pump_term, vars.reservoir_pump[g, b, t])
                    end
                end
            end

            # Bus demand from parent node scaled by demand_fraction
            bus_demand = input.demand[t, ni] * bus_df

            # Rooftop solar generation (indexed per node, scaled by demand_fraction)
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
                extra_injections_fn(b, t)
            else
                AffExpr(0.0)
            end

            # Demand moved into this step by demand shifting (net, may be < 0)
            shift_term = haskey(shift_net, (ni, t)) ? shift_net[(ni, t)] * bus_df : AffExpr(0.0)

            # Power balance (node-level vars scaled by demand_fraction for bus-level KCL):
            # LHS: gen_output + bat_discharge + EV_V2G + load_shed + rooftop_gen + extra
            # RHS: bus_demand + shifted demand + electrolyzer + bat_charge + EV_charging + rooftop_curtailment
            # Reserves are headroom, not energy: they are held in
            # add_reserve_constraints! and do not enter the balance.
            con = @constraint(model,
                total_gen + bat_discharge + ev_v2g_term + vars.load_shed[b, t] + rooftop_gen_term + extra_term ==
                bus_demand + shift_term + electrolyzer_term + bat_charge_sum + reservoir_pump_term +
                ev_charging_term + rooftop_curt_term
            )

            # Store constraint reference for dual extraction
            if vars.balance_constraints !== nothing
                vars.balance_constraints[(b, t)] = con
            end
        end

        # ── Per-node constraints (outside bus loop) ──
        n_node = input.network.num_nodes
        for ni in 1:n_node
            # Rooftop curtailment cannot exceed available rooftop generation at node
            if vars.rooftop_curtailment !== nothing && hasproperty(input, :rooftop_generation) && input.rooftop_generation !== nothing
                @constraint(model,
                    vars.rooftop_curtailment[ni, t] <= input.rooftop_generation[t, ni],
                    base_name = "rooftop_curt_limit_n$(ni)_t$(t)"
                )
            end
        end

        # Per-bus cap: load_shed is UNSERVED DEMAND, so it can never exceed
        # the demand actually sitting at that bus.
        #
        # It enters the power balance above on the SUPPLY side, so leaving it
        # unbounded turns it into virtual generation: the solver "sheds" far
        # more than a bus consumes and exports the surplus over the network.
        # Japan: 1 504 MW shed at a 33 MW bus, and 66 % of the reported
        # load shedding was this artefact rather than real unserved load.
        # Relaxing the VOLL penalty is not a substitute for the bound — an
        # expensive virtual generator is still a virtual generator, and it
        # both inflates the shed metric and displaces real dispatch.
        # The zero-demand case (a bus with no load) is this same bound at 0.
        for b in 1:n_bus
            ni = input.network.bus_to_node[b]
            bus_role = input.network.buses[b].role
            is_load_bus = bus_role == "load" || bus_role == "mixed"
            bus_df = is_load_bus ? input.network.buses[b].demand_fraction : 0.0
            bus_demand_t = input.demand[t, ni] * bus_df
            @constraint(model,
                vars.load_shed[b, t] <= max(bus_demand_t, 0.0),
                base_name = "max_load_shed_b$(b)_t$(t)")
        end
    end
end

"""
    add_generator_constraints!(model, vars::PowerSystemVariables, input::PowerSystemInput)

Add generator operational constraints: capacity, ramps, min up/down times.

- Every generator: gen_output <= rated * availability
- Unit commitment, every unit but a variable renewable
  ([`is_variable_renewable`](@ref)): gen_output <= M * gen_status,
  gen_output >= min_power * rated * gen_status, start-ups, min up/down
- Curtailment is NOT part of generator capacity constraints (it's per-node, defined separately)

Unit commitment with `num_units = N > 1` at a bus (clustered units):
`gen_status` counts the units online (integer 0..N), each of `rated / N`:
- min_power × rated/N × status <= gen_output <= rated/N × status
- gen_output <= rated × availability, and status <= ceil(N × availability):
  availability is the share of the plant's units available, so a derated
  plant commits only the units it still has
- startup >= status[t] − status[t−1] (units started)
- min up: Σ startup over the last min_up steps <= status[t]
- min down: Σ shutdown over the last min_down steps <= N − status[t], with
  shutdown = startup − (status[t] − status[t−1])
With N = 1 the binary formulation is unchanged.
"""
function add_generator_constraints!(model, vars::PowerSystemVariables, input;
    capacity_override::Union{Dict{Tuple{Int,Int}, Any}, Nothing} = nothing)
    n_gen = length(input.generators)
    n_bus = input.network.num_buses
    hours = input.temporal.hours
    tres = input.temporal.resolution_hours > 0 ? input.temporal.resolution_hours : 1
    # UC mode is identified by gen_startup being present (gen_status always exists)
    is_uc = vars.gen_startup !== nothing
    # Big-M for on/off constraints. It only needs to force gen_output→0 when the
    # unit is off (status=0); when on it must be ≥ the unit's own capacity so it
    # never caps output below the (already-present) `gen_output ≤ capacity`
    # bound. max-demand is NOT a valid ceiling: aggregated generators routinely
    # exceed the biggest node's demand (e.g. all of a node's gas collapsed onto
    # one bus → 46 GW while peak nodal demand is ~12 GW), and a max-demand big-M
    # then clamps them, stranding the surplus and shedding load at VoLL. Use a
    # per-generator big-M sized to each unit's capacity.
    _demand_M = max(maximum(input.demand) * 1.1, 1e4)

    for g in 1:n_gen
        gen = input.generators[g]

        for b in vars.buses_of_gen[g]
            rated = gen.rated_power[b]
            # Committed in this unit-commitment dispatch: every unit but a
            # variable renewable (its output only follows its availability)
            committed = capacity_override === nothing && is_uc &&
                        !is_variable_renewable(gen, b)

            # When capacity_override provided (master problem), use precomputed capacity
            # and skip degradation
            if capacity_override !== nothing && haskey(capacity_override, (g, b))
                total_capacity = capacity_override[(g, b)]
            else
                # Age-based degradation and end of life
                rated = generator_power_in_service(gen, b, input)
                total_capacity = rated
            end

            # Segment upper bounds for PWL cost decomposition
            if haskey(vars.gen_seg_output, (g, b))
                segs = input.gen_cost_curves[g][b]
                seg_vars = vars.gen_seg_output[(g, b)]
                for k in 1:length(segs), t in 1:hours
                    seg_cap = segs[k].fraction * total_capacity
                    # availability caps every generator: 1.0 by default, the
                    # capacity factor for renewables, and <1 during a scheduled
                    # outage (deterministic interruptions). No-op when 1.0, so
                    # dispatchable units are unaffected unless an outage applies.
                    avail = gen.availability[t, b]
                    @constraint(model, seg_vars[k, t] <= seg_cap * avail)
                end
            end

            # Per-generator big-M: ≥ this unit's own capacity so the on/off
            # constraint below only zeroes output when off — never clamps a
            # large aggregated unit below its rated power.
            M_gb = max(_demand_M, gen.rated_power[b] * 1.1)

            # Clustered unit commitment: N identical units of rated / N each
            N = units_of(gen, b)
            clustered = N > 1 && committed

            for t in 1:hours
                # availability caps every generator (see segment note above):
                # default 1.0, CF for renewables, <1 under a scheduled outage.
                avail = gen.availability[t, b]
                @constraint(model,
                    vars.gen_output[g, b, t] <= total_capacity * avail)

                # gen_status big-M only for a committed unit (UC dispatch, not
                # the master). In economic dispatch there is no gen_status: the
                # big-M would be redundant with `gen_output <= total_capacity`
                # and only pollute the LP matrix with a coefficient of M (~1e4).
                if clustered
                    # Each online unit gives at most rated / N, and only
                    # the units still available can be committed
                    @constraint(model,
                        vars.gen_output[g, b, t] <= (rated / N) * vars.gen_status[g, b, t])
                    n_avail = units_available(N, avail)
                    n_avail < N && @constraint(model,
                        vars.gen_status[g, b, t] <= n_avail,
                        base_name = "units_available_g$(g)_b$(b)_t$(t)")
                elseif committed
                    @constraint(model,
                        vars.gen_output[g, b, t] <= M_gb * vars.gen_status[g, b, t]
                    )
                end

                if committed
                    # Minimum stable output of each online unit
                    min_out = gen.min_power[b] * rated
                    clustered && (min_out /= N)
                    @constraint(model,
                        vars.gen_output[g, b, t] >= min_out * vars.gen_status[g, b, t]
                    )
                end
            end

            # Ramp constraints. Ramp rates are a fraction of capacity per
            # hour, so a step of `tres` hours may move `ramp × tres` (capped
            # at full capacity). A ramp rate <= 0 means "no ramp limit": a
            # literal zero would freeze the unit at its first-window output
            # for the whole year (Studio units used to default to 0).
            has_ramp_up = gen.ramp_up[b] > 0
            has_ramp_down = gen.ramp_down[b] > 0
            ramp_up_limit = total_capacity * min(1.0, gen.ramp_up[b] * tres)
            ramp_down_limit = total_capacity * min(1.0, gen.ramp_down[b] * tres)
            # t=1 ramp from prev-window output (rolling-horizon seam).
            # Empty Dict = no boundary; skip the t=1 constraint to preserve
            # the legacy free-start behaviour of the first window.
            prev_out = get(get(input.generator_output_prev, g, Dict{Int,Float64}()), b, NaN)
            if !isnan(prev_out)
                has_ramp_up && @constraint(model,
                    vars.gen_output[g, b, 1] - prev_out <= ramp_up_limit,
                    base_name = "ramp_up_seam_g$(g)_b$(b)")
                has_ramp_down && @constraint(model,
                    prev_out - vars.gen_output[g, b, 1] <= ramp_down_limit,
                    base_name = "ramp_down_seam_g$(g)_b$(b)")
            end
            for t in 2:hours
                has_ramp_up && @constraint(model,
                    vars.gen_output[g, b, t] - vars.gen_output[g, b, t-1] <= ramp_up_limit
                )
                has_ramp_down && @constraint(model,
                    vars.gen_output[g, b, t-1] - vars.gen_output[g, b, t] <= ramp_down_limit
                )
            end

            # Unit commitment logic (only in operational dispatch, not master)
            if clustered && rated > 0
                add_clustered_commitment!(model, vars, input, g, b, N, tres)
            elseif committed && rated > 0
                # min_up / min_down are in hours; the constraints count time
                # steps. Round up so a step longer than the minimum still
                # enforces at least one step (8 h at 6-h steps → 2 steps).
                min_up = ceil(Int, gen.min_up_time[b] / tres)
                min_down = ceil(Int, gen.min_down_time[b] / tres)

                for t in 1:hours
                    if t > 1
                        prev_status = vars.gen_status[g, b, t-1]
                    else
                        prev_status = get(get(input.generator_initial_status, g, Dict{Int,Float64}()), b, 0.0)
                    end

                    @constraint(model,
                        vars.gen_startup[g, b, t] >= vars.gen_status[g, b, t] - prev_status,
                        base_name = "startup_detect_g$(g)_b$(b)_t$(t)"
                    )

                    for τ in max(1, t - min_up + 1):t
                        if τ > 1
                            τ_prev = vars.gen_status[g, b, τ-1]
                        else
                            τ_prev = get(get(input.generator_initial_status, g, Dict{Int,Float64}()), b, 0.0)
                        end
                        @constraint(model,
                            vars.gen_status[g, b, t] >= vars.gen_status[g, b, τ] - τ_prev,
                            base_name = "min_up_g$(g)_b$(b)_t$(t)_tau$(τ)"
                        )
                    end

                    for τ in max(1, t - min_down + 1):t
                        if τ > 1
                            τ_prev = vars.gen_status[g, b, τ-1]
                        else
                            τ_prev = get(get(input.generator_initial_status, g, Dict{Int,Float64}()), b, 0.0)
                        end
                        @constraint(model,
                            1 - vars.gen_status[g, b, t] >= τ_prev - vars.gen_status[g, b, τ],
                            base_name = "min_down_g$(g)_b$(b)_t$(t)_tau$(τ)"
                        )
                    end
                end
            end
        end
    end

end

"""
    units_available(N, avail) -> Int

Units of an `N`-unit plant that can be committed in a step with
availability `avail` (the share of the plant available): `ceil(N × avail)`,
so 1.2 units' worth lets two units run, sharing the 1.2 units of power.
"""
units_available(N::Int, avail::Real) = clamp(ceil(Int, N * avail - 1e-6), 0, N)

"""
    add_clustered_commitment!(model, vars, input, g, b, N, tres)

Start-up and minimum up/down time constraints of generator `g` at bus `b`
made of `N > 1` identical units (`gen_status` = units online, integer):

    startup[t] ≥ status[t] − status[t−1]
    Σ_{τ=t−MU+1..t} startup[τ] ≤ status[t]                        (MU ≥ 2)
    Σ_{τ=t−MD+1..t} (startup[τ] − status[τ] + status[τ−1]) ≤ N − status[t]  (MD ≥ 2)

with MU / MD the minimum up / down times in steps and status[0] the units
online at the end of the previous window (at most `N`). A unit started in
the last MU steps is still online; a unit shut down in the last MD steps
cannot be online again. `startup` only has a lower bound: a larger value
costs more and tightens both sums, so it equals the units started.
"""
function add_clustered_commitment!(model, vars, input, g::Int, b::Int, N::Int, tres)
    gen = input.generators[g]
    hours = input.temporal.hours
    min_up = ceil(Int, gen.min_up_time[b] / tres)
    min_down = ceil(Int, gen.min_down_time[b] / tres)
    u = t -> vars.gen_status[g, b, t]
    su = t -> vars.gen_startup[g, b, t]
    u0 = clamp(get(get(input.generator_initial_status, g, Dict{Int,Float64}()), b, 0.0),
               0.0, Float64(N))
    prev(t) = t > 1 ? u(t - 1) : u0

    for t in 1:hours
        @constraint(model, su(t) >= u(t) - prev(t),
            base_name = "startup_detect_g$(g)_b$(b)_t$(t)")
    end
    if min_up > 1
        for t in 1:hours
            started = AffExpr(0.0)
            for τ in max(1, t - min_up + 1):t
                add_to_expression!(started, su(τ))
            end
            @constraint(model, started <= u(t), base_name = "min_up_g$(g)_b$(b)_t$(t)")
        end
    end
    if min_down > 1
        for t in 1:hours
            stopped = AffExpr(0.0)
            for τ in max(1, t - min_down + 1):t
                add_to_expression!(stopped, su(τ))
                add_to_expression!(stopped, -1.0, u(τ))
                add_to_expression!(stopped, prev(τ))
            end
            @constraint(model, stopped <= N - u(t), base_name = "min_down_g$(g)_b$(b)_t$(t)")
        end
    end
end

"""
    online_capacity(model, vars, input, g, b, t, p_gb)

Power generator `g` at bus `b` can give in step `t` with the units it has
online (unit commitment), `p_gb` being its power in service:
`p_gb × availability × status` for a single unit; for `N > 1` units the
lesser of `p_gb / N × units online` and `p_gb × availability`, a variable
(made once per step and kept in `model.ext`) bounded by both.
"""
function online_capacity(model, vars, input, g::Int, b::Int, t::Int, p_gb)
    gen = input.generators[g]
    cap_t = p_gb * gen.availability[t, b]
    N = units_of(gen, b)
    N == 1 && return cap_t * vars.gen_status[g, b, t]
    cache = get!(model.ext, :gen_online_capacity) do
        Dict{NTuple{3, Int}, VariableRef}()
    end
    return get!(cache, (g, b, t)) do
        c = @variable(model, lower_bound = 0, upper_bound = cap_t,
                      base_name = "gen_online_cap_g$(g)_b$(b)_t$(t)")
        @constraint(model, c <= (p_gb / N) * vars.gen_status[g, b, t])
        c
    end
end

"""
    unit_age_in_year(unit, b, input) -> (age, life)

Age of an existing generator or battery at bus `b` in the dispatch's year
(`initial_age` + years since the base year) and its `life_time` (Inf when
the unit has none at that bus).
"""
function unit_age_in_year(unit, b::Int, input)
    base_age = length(unit.initial_age) >= b ? unit.initial_age[b] : 0.0
    age = base_age + max(0.0, Float64(input.year - input.base_year))
    life = length(unit.life_time) >= b ? unit.life_time[b] : Inf
    return age, life
end

"""
    generator_power_in_service(gen, b, input) -> Float64

Power (MW) of existing generator `gen` at bus `b` in the dispatch's year:
zero once the unit is past its `life_time` (age = `initial_age` + years
since the base year, with or without degradation, as in the master), and
otherwise shrunk by `(1 − degradation_rate)^age`. After a master run the
dispatch receives the master's capacity (retirement and life extension
included) as `rated_power` with no degradation and no end of life, so it is
used as is.
"""
function generator_power_in_service(gen, b::Int, input)
    rated = gen.rated_power[b]
    rated > 0 || return rated
    age, life = unit_age_in_year(gen, b, input)
    life - age <= 0 && return 0.0
    if length(gen.degradation_rate) >= b && gen.degradation_rate[b] > 0
        return rated * (1 - gen.degradation_rate[b])^age
    end
    return rated
end

"""
    fixed_om_annual_cost(vars, input) -> AffExpr

Fixed O&M (\$/year) of the capacity in service in the dispatch's year:
`fixed_cost` (\$/MW·year) × the generators' power in service
([`generator_power_in_service`](@ref)) and × the batteries' rated discharge
power while within their lifetime. Charged whether or not the units run; the objective prorates it
to the window.
"""
function fixed_om_annual_cost(vars, input)
    cost = AffExpr(0.0)
    for (g, gen) in enumerate(input.generators), b in vars.buses_of_gen[g]
        fc = gen.fixed_cost[b]
        fc > 0 || continue
        add_to_expression!(cost, fc * generator_power_in_service(gen, b, input))
    end
    for (bi, bat) in enumerate(input.batteries), b in vars.buses_of_bat[bi]
        fc = bat.fixed_cost[b]
        # Rated (not degraded) power; nothing once past its life_time
        (fc > 0 && battery_power_limits(bat, b, input)[3] > 0) || continue
        add_to_expression!(cost, fc * bat.max_discharge_power[b])
    end
    return cost
end

"""
    battery_power_limits(bat, b, input) -> (capacity, max_charge, max_discharge)

Energy capacity (MWh) and charge / discharge power (MW) of battery `bat` at
bus `b` in the dispatch's year: zero once the battery is past its
`life_time` (age = `initial_age` + years since the base year, with or
without degradation, as in the master), and otherwise shrunk by
`(1 − degradation_rate)^age`. After a master run a battery the master keeps
past its lifetime arrives with the kept capacity and no end of life.
Energy with neither charge nor discharge power is no storage: the capacity
is zero there too, so the SOC floor, minimum cycling and end-of-window
conditions never ask an idle battery for anything.
"""
function battery_power_limits(bat, b::Int, input)
    capacity = bat.capacity[b]
    max_charge = bat.max_charge_power[b]
    max_discharge = bat.max_discharge_power[b]
    max_charge <= 0 && max_discharge <= 0 && return 0.0, 0.0, 0.0
    age, life = unit_age_in_year(bat, b, input)
    life - age <= 0 && return 0.0, 0.0, 0.0
    if length(bat.degradation_rate) >= b && bat.degradation_rate[b] > 0
        f = (1 - bat.degradation_rate[b])^age
        return capacity * f, max_charge * f, max_discharge * f
    end
    return capacity, max_charge, max_discharge
end

"""
    battery_charge_ratio(bat, b) -> Float64

Charge power per MW of discharge power of battery `bat` at bus `b`:
`max_charge_power / max_discharge_power` (1.0 without discharge power).
Degradation and life extension scale both ratings alike
([`battery_power_limits`](@ref), the runner's life extension), so a master
that holds a battery's discharge power in service (degraded, life-extended)
holds `battery_charge_ratio` × that as its charge power.
"""
function battery_charge_ratio(bat, b::Int)::Float64
    dis = bat.max_discharge_power[b]
    dis > 0 || return 1.0
    return max(bat.max_charge_power[b], 0.0) / dis
end

"""
    add_storage_ramp_constraints!(model, power, rated, prev, ramp_up, ramp_down,
                                  tres, hours; seam_name="") -> Vector

Ramp limits of one power flow of a battery (its charge or its discharge) at
one bus: from one step to the next it may rise by at most
`storage_ramp_limit(ramp_up, tres, rated)` and fall by at most
`storage_ramp_limit(ramp_down, tres, rated)`. `power(t)` is the flow at step
`t`; `prev` its value at the previous window's last step (NaN: no limit at
t = 1); `rated` its rated power (a number, or an expression of the master's
capacities). Returns `(constraint, coefficient of rated)` per constraint.
"""
function add_storage_ramp_constraints!(model, power::Function, rated, prev::Float64,
                                       ramp_up::Real, ramp_down::Real, tres::Real,
                                       hours::Int; seam_name::String = "")
    out = Tuple{Any, Float64}[]
    for (ramp, sgn, dir) in ((ramp_up, 1.0, "up"), (ramp_down, -1.0, "down"))
        coef = storage_ramp_limit(ramp, tres, 1.0)
        coef === nothing && continue
        if !isnan(prev)
            c = @constraint(model, sgn * (power(1) - prev) <= coef * rated,
                            base_name = "$(seam_name)_ramp_$(dir)_seam")
            push!(out, (c, coef))
        end
        for t in 2:hours
            c = @constraint(model, sgn * (power(t) - power(t-1)) <= coef * rated)
            push!(out, (c, coef))
        end
    end
    return out
end

"""
    add_battery_constraints!(model, vars::PowerSystemVariables, input::PowerSystemInput)

Add battery storage constraints: SOC dynamics, charge/discharge limits
(× availability and scheduled outages), ramp limits. With
`capacity_override_power` (a master: the discharge power in service per
(battery, bus)) the charge power is that × [`battery_charge_ratio`](@ref).
"""
function add_battery_constraints!(model, vars::PowerSystemVariables, input;
    capacity_override_power::Union{Dict{Tuple{Int,Int}, Any}, Nothing} = nothing,
    capacity_override_energy::Union{Dict{Tuple{Int,Int}, Any}, Nothing} = nothing,
    initial_soc_overrides::Union{Nothing, Dict{Tuple{Int,Int}, Any}} = nothing,
    final_soc_targets::Union{Nothing, Dict{Tuple{Int,Int}, Any}} = nothing)
    n_bat = length(input.batteries)
    if n_bat == 0
        return  # No batteries to constrain
    end

    n_bus = input.network.num_buses
    hours = input.temporal.hours
    tres = input.temporal.resolution_hours > 0 ? input.temporal.resolution_hours : 1
    has_override = capacity_override_power !== nothing

    # Deterministic scheduled outages: [n_bat × hours] capacity mask (1.0 = none).
    # Lightweight NamedTuple inputs (master-problem representative days) omit the
    # field entirely — no outages there, so fall back to nothing.
    bat_outage = hasproperty(input, :outage_masks) ?
        get(input.outage_masks, "battery", nothing) : nothing

    for bi in 1:n_bat
        bat = input.batteries[bi]

        for b in vars.buses_of_bat[bi]
            # When capacity overrides provided (master problem), use precomputed capacity
            # and skip degradation, investment logic, SOC bounds, spillage, mutex, min cycling.
            # The override is the discharge power; the charge power goes with
            # it at the battery's own MaxChargePower / MaxDischargePower
            if has_override && haskey(capacity_override_power, (bi, b))
                effective_max_discharge = capacity_override_power[(bi, b)]
                effective_max_charge = effective_max_discharge * battery_charge_ratio(bat, b)
                effective_capacity = capacity_override_energy[(bi, b)]

                η_charge = bat.charge_efficiency[b]
                η_discharge = bat.discharge_efficiency[b]
                # Guard against zero efficiency (e.g. virtual batteries with sparse config)
                if η_discharge <= 0.0
                    η_discharge = 1.0
                end
                if η_charge <= 0.0
                    η_charge = 1.0
                end

                # Initial SOC (override or config-based)
                soc_init_fraction = if b <= length(bat.soc_initial)
                    bat.soc_initial[b]
                else
                    # @debug not @warn: fires once per (battery × bus)
                    # at every window — would emit thousands of lines.
                    # Missing soc_initial is a config gap, not a fault.
                    @debug "Battery $bi bus $b: soc_initial not specified, defaulting to 0.5"
                    0.5
                end
                # Clamp to soc_min to avoid end-of-horizon vs soc_min conflict
                soc_min_b = bat.soc_min[b]
                if soc_init_fraction < soc_min_b
                    soc_init_fraction = soc_min_b
                end
                soc_init = effective_capacity * soc_init_fraction
                if initial_soc_overrides !== nothing && haskey(initial_soc_overrides, (bi, b))
                    soc_init = initial_soc_overrides[(bi, b)]
                end
                @constraint(model,
                    vars.bat_soc[bi, b, 1] == soc_init,
                    base_name = "bat_soc_initial_$(bi)_$(b)")

                for t in 1:hours
                    avail = storage_availability(bat.availability, t, b)
                    @constraint(model, vars.bat_charge[bi, b, t] <= effective_max_charge * avail)
                    @constraint(model, vars.bat_discharge[bi, b, t] <= effective_max_discharge * avail)
                    @constraint(model, vars.bat_soc[bi, b, t+1] <= effective_capacity)

                    # SOC dynamics (simplified: no self-discharge, no spillage in master LP)
                    # bat_charge/bat_discharge are in MW; multiply by tres to get MWh per timestep
                    @constraint(model,
                        vars.bat_soc[bi, b, t+1] == vars.bat_soc[bi, b, t] +
                        η_charge * vars.bat_charge[bi, b, t] * tres -
                        vars.bat_discharge[bi, b, t] * tres / η_discharge)
                end
                # Ramp limits, as in the dispatch (each day starts fresh)
                add_storage_ramp_constraints!(model, t -> vars.bat_charge[bi, b, t],
                    effective_max_charge, NaN, bat.ramp_up[b], bat.ramp_down[b], tres, hours)
                add_storage_ramp_constraints!(model, t -> vars.bat_discharge[bi, b, t],
                    effective_max_discharge, NaN, bat.ramp_up[b], bat.ramp_down[b], tres, hours)

                # Segment upper bounds for PWL discharge cost decomposition
                if haskey(vars.bat_seg_discharge, (bi, b))
                    segs = input.bat_cost_curves[bi][b]
                    seg_vars = vars.bat_seg_discharge[(bi, b)]
                    for k in 1:length(segs), t in 1:hours
                        @constraint(model, seg_vars[k, t] <= segs[k].fraction * effective_max_discharge)
                    end
                end

                # End-of-horizon SOC: TSAM linking target or cyclic
                if final_soc_targets !== nothing && haskey(final_soc_targets, (bi, b))
                    @constraint(model,
                        vars.bat_soc[bi, b, hours+1] == final_soc_targets[(bi, b)],
                        base_name = "bat_soc_link_$(bi)_$(b)")
                else
                    # Cyclic: SOC at end == initial SOC
                    @constraint(model,
                        vars.bat_soc[bi, b, hours+1] == soc_init,
                        base_name = "bat_soc_cyclic_$(bi)_$(b)")
                end
            else
                # === Standard operational dispatch path ===
                # Age-based degradation: age advances with simulation year
                capacity, max_charge, max_discharge = battery_power_limits(bat, b, input)

                η_charge = bat.charge_efficiency[b]
                η_discharge = bat.discharge_efficiency[b]
                # Guard against zero efficiency (e.g. virtual batteries with sparse config)
                if η_discharge <= 0.0
                    η_discharge = 1.0
                end
                if η_charge <= 0.0
                    η_charge = 1.0
                end
                self_discharge = bat.self_discharge[b]
                # Ensure initial SOC respects minimum SOC bound.
                # When soc_initial=0 but soc_min>0 (max_DoD<1), a zero initial SOC
                # makes the end-of-horizon constraint (soc[end] ≈ soc_init) conflict
                # with soc_min. Fix: clamp initial SOC to at least soc_min.
                soc_init_frac = bat.soc_initial[b]
                if soc_init_frac < bat.soc_min[b] && capacity > 0
                    soc_init_frac = bat.soc_min[b]
                end
                soc_init = soc_init_frac * capacity
                # Rolling horizon: start from the energy the previous window
                # left (MWh), kept within this window's SOC bounds. Carrying
                # energy rather than a fraction keeps it exact when the
                # capacity is degraded.
                prev_soc = hasproperty(input, :battery_soc_prev) ?
                    get(get(input.battery_soc_prev, bi, Dict{Int,Float64}()), b, NaN) : NaN
                if !isnan(prev_soc)
                    soc_init = clamp(prev_soc, bat.soc_min[b] * capacity, capacity)
                end

                @constraint(model,
                    vars.bat_soc[bi, b, 1] == soc_init,
                    base_name = "bat_soc_initial_$(bi)_$(b)"
                )

                # Scheduled outage × availability profile, per step
                omask = [(bat_outage === nothing ? 1.0 : bat_outage[bi, t]) *
                         storage_availability(bat.availability, t, b) for t in 1:hours]
                charge_limit = max_charge .* omask
                # SOC floor per step. Self-discharge drains the battery while
                # it cannot charge (an outage, no availability), so after
                # such steps the floor is what full charging can hold:
                # floor[t+1] = min(soc_min·cap, (1 − sd)·floor[t] + η·limit[t]·Δt).
                # A battery that can charge at least its self-discharge keeps
                # the plain soc_min·cap floor.
                soc_floor = Vector{Float64}(undef, hours + 1)
                soc_floor[1] = bat.soc_min[b] * capacity
                for t in 1:hours
                    soc_floor[t+1] = min(bat.soc_min[b] * capacity,
                        (1 - self_discharge) * soc_floor[t] + η_charge * charge_limit[t] * tres)
                end

                for t in 1:hours
                    @constraint(model, vars.bat_charge[bi, b, t] <= charge_limit[t])
                    @constraint(model, vars.bat_discharge[bi, b, t] <= max_discharge * omask[t])

                    # Mutex constraint (Big-M: 2× max possible power)
                    if vars.bat_charge_status !== nothing && max(max_charge, max_discharge) > 0
                        M_bat = max(max_charge, max_discharge) * 2.0

                        @constraint(model, vars.bat_charge[bi, b, t] <= M_bat * vars.bat_charge_status[bi, b, t])
                        @constraint(model, vars.bat_discharge[bi, b, t] <= M_bat * (1 - vars.bat_charge_status[bi, b, t]))
                    end

                    # SOC bounds
                    @constraint(model, vars.bat_soc[bi, b, t+1] >= soc_floor[t+1])
                    @constraint(model, vars.bat_soc[bi, b, t+1] <=
                        bat.soc_max[b] * capacity + vars.soc_violation[bi, b, t])

                    # SOC dynamics
                    # bat_charge/bat_discharge are in MW; multiply by tres to get MWh per timestep
                    spillage_term = if vars.bat_spillage !== nothing && bat.spillage && vars.bat_spillage[bi, b, t] !== nothing
                        vars.bat_spillage[bi, b, t] * tres
                    else
                        0.0
                    end
                    @constraint(model,
                        vars.bat_soc[bi, b, t+1] == vars.bat_soc[bi, b, t] * (1 - self_discharge) +
                        η_charge * vars.bat_charge[bi, b, t] * tres -
                        vars.bat_discharge[bi, b, t] * tres / η_discharge -
                        spillage_term
                    )

                    # Spillage power limit
                    if vars.bat_spillage !== nothing && bat.spillage && vars.bat_spillage[bi, b, t] !== nothing
                        @constraint(model, vars.bat_spillage[bi, b, t] <= max_discharge)
                    end
                end

                # Ramp limits on charge and discharge; at t=1 from the
                # previous window's last step (rolling-horizon seam)
                prev_of(d) = hasproperty(input, d) ?
                    get(get(getproperty(input, d), bi, Dict{Int,Float64}()), b, NaN) : NaN
                add_storage_ramp_constraints!(model, t -> vars.bat_charge[bi, b, t],
                    max_charge, prev_of(:battery_charge_prev),
                    bat.ramp_up[b], bat.ramp_down[b], tres, hours;
                    seam_name = "bat_charge_bi$(bi)_b$(b)")
                add_storage_ramp_constraints!(model, t -> vars.bat_discharge[bi, b, t],
                    max_discharge, prev_of(:battery_discharge_prev),
                    bat.ramp_up[b], bat.ramp_down[b], tres, hours;
                    seam_name = "bat_discharge_bi$(bi)_b$(b)")

                # Segment upper bounds for PWL discharge cost decomposition
                if haskey(vars.bat_seg_discharge, (bi, b))
                    segs = input.bat_cost_curves[bi][b]
                    seg_vars = vars.bat_seg_discharge[(bi, b)]
                    for k in 1:length(segs), t in 1:hours
                        @constraint(model, seg_vars[k, t] <= segs[k].fraction * max_discharge)
                    end
                end

                # End-of-horizon SOC constraints.
                # When `cyclic_end_soc` is true (single full-year solve), force
                # SOC[end] ≈ SOC[start] for energy conservation.  In rolling
                # windows the runner sets it false and chains SOC across
                # windows via boundary_conditions; in that case we only enforce
                # soc_min/soc_max bounds, letting SOC drift freely.
                if input.cyclic_end_soc
                    initial_soc_energy = soc_init
                    soc_tol = input.soc_end_tolerance
                    # At most the SOC that charging at every step can reach
                    # (less than the start when self-discharge outruns the
                    # charge power, e.g. through a long outage)
                    reach = soc_init
                    for t in 1:hours
                        reach = min(bat.soc_max[b] * capacity,
                            (1 - self_discharge) * reach + η_charge * charge_limit[t] * tres)
                    end
                    @constraint(model,
                        vars.bat_soc[bi, b, hours+1] >=
                            min(initial_soc_energy * (1.0 - soc_tol), reach),
                        base_name = "bat_soc_end_lower_$(bi)_$(b)"
                    )
                    @constraint(model,
                        vars.bat_soc[bi, b, hours+1] <= initial_soc_energy * (1.0 + soc_tol),
                        base_name = "bat_soc_end_upper_$(bi)_$(b)"
                    )
                elseif hasproperty(input, :bat_soc_end_min)
                    # Soft end-of-year floor (last rolling window, or the single
                    # window of a year): the battery may not finish the year with
                    # less energy than it started with. The shortfall is priced
                    # below load shedding, so it is refilled from generation but
                    # never by shedding load, and a short last window that cannot
                    # refill stays feasible.
                    end_floor = if haskey(input.bat_soc_end_min, bi) && haskey(input.bat_soc_end_min[bi], b)
                        input.bat_soc_end_min[bi][b]
                    elseif input.bat_soc_end_min_cyclic
                        soc_init
                    else
                        0.0
                    end
                    if end_floor > 0
                        shortfall = @variable(model, lower_bound = 0.0,
                            base_name = "bat_soc_end_short_$(bi)_$(b)")
                        @constraint(model,
                            vars.bat_soc[bi, b, hours+1] + shortfall >=
                            end_floor * (1.0 - input.soc_end_tolerance),
                            base_name = "bat_soc_end_floor_$(bi)_$(b)")
                        penalty = input.bat_soc_end_penalty > 0 ? input.bat_soc_end_penalty :
                            0.1 * input.loss_of_load_penalty / tres
                        JuMP.set_objective_coefficient(model, shortfall, penalty)
                        # In the breakdown with the other SOC penalties (an
                        # energy entry there, × step length at extraction)
                        add_to_expression!(model.ext[:cost_expressions][:soc_violation_cost],
                                           penalty / tres, shortfall)
                    end
                end

                # Minimum cycling: charge at least min_cycling_ratio × capacity
                # per min_cycling_period_days.
                # bat_charge is in MW; sum(bat_charge) gives MW-timesteps.
                # min_cycling expressed in MW-timesteps: ratio * capacity_MWh * (timesteps/24) / period
                # The tres factor cancels: energy = sum(bat_charge)*tres, days = timesteps*tres/24
                # It never makes a window infeasible: the requirement is at
                # most what the battery can charge in the window (none when it
                # cannot charge at all), and a shortfall (ramp limits, ...) is
                # priced at 10 % of VOLL per MWh, below load shedding.
                days = hours / 24.0
                min_cycling = min(
                    input.min_cycling_ratio * capacity * days / input.min_cycling_period_days,
                    sum(charge_limit))
                if min_cycling > 0 && capacity > 0
                    shortfall = @variable(model, lower_bound = 0.0,
                        base_name = "bat_min_cycling_short_$(bi)_$(b)")
                    @constraint(model,
                        sum(vars.bat_charge[bi, b, t] for t in 1:hours) + shortfall >= min_cycling,
                        base_name = "bat_min_cycling_$(bi)_$(b)"
                    )
                    JuMP.set_objective_coefficient(model, shortfall,
                        0.1 * input.loss_of_load_penalty * tres)
                end
            end
        end
    end

end

"""
    add_reservoir_constraints!(model, vars::PowerSystemVariables, input::PowerSystemInput)

Add reservoir hydroelectric constraints: water level dynamics, spillage, pump-back.

For generators with `reservoir_capacity[n] > 0`, tracks water level (MWh-equivalent) with:
- Inflows (exogenous water input)
- Turbine output (gen_output / turbine_efficiency drains water)
- Pump-back (refills reservoir, consumes electricity)
- Spillage (uncontrolled overflow)
- Evaporation (fractional loss per hour)

Cyclic constraint ensures end-of-horizon level matches initial level (within tolerance).
"""
function add_reservoir_constraints!(model, vars::PowerSystemVariables, input;
    # TSAM seasonal hydro linking: when present, the reservoir starts a period
    # at the level handed in (initial_reservoir_overrides[(g,b)]) and ends it
    # exactly at the expression handed in (final_reservoir_targets[(g,b)]),
    # the period's start and end in the master's seasonal storage
    # (add_tsam_periods_validation!), instead of being cyclic within the period.
    initial_reservoir_overrides::Union{Nothing, Dict{Tuple{Int,Int}, Any}} = nothing,
    final_reservoir_targets::Union{Nothing, Dict{Tuple{Int,Int}, Any}} = nothing,
    # Total reservoir capacity (MWh) per (g, b) where the caller adds its own
    # builds (the master's reservoir investment).
    capacity_override::Union{Nothing, Dict{Tuple{Int,Int}, Any}} = nothing)
    if vars.reservoir_level === nothing
        return  # No reservoir generators
    end

    n_gen = length(input.generators)
    n_bus = input.network.num_buses
    hours = input.temporal.hours

    # --- Hydraulic cascade topology ---------------------------------------
    # Resolve each reservoir's `cascade_downstream` name to a generator index
    # and record, per downstream reservoir, which upstream units feed it and
    # with what travel delay. The released water (turbined + spilled, summed
    # over the upstream's reservoir nodes) is injected into the downstream
    # reservoir's primary node. cascade_feeders[gd] = [(gu, delay), ...].
    name_to_gen = Dict{String, Int}()
    for g in 1:n_gen
        name_to_gen[input.generators[g].name] = g
    end
    primary_res_bus = Dict{Int, Int}()
    for g in 1:n_gen
        for b in vars.buses_of_gen[g]
            if input.generators[g].reservoir_capacity[b] > 0
                primary_res_bus[g] = b
                break
            end
        end
    end
    cascade_feeders = Dict{Int, Vector{Tuple{Int,Int}}}()
    for gu in 1:n_gen
        genu = input.generators[gu]
        isempty(genu.cascade_downstream) && continue
        gd = get(name_to_gen, genu.cascade_downstream, 0)
        gd == 0 && continue
        # Both ends must actually be reservoirs to form a water link.
        (haskey(primary_res_bus, gu) && haskey(primary_res_bus, gd)) || continue
        gd == gu && continue  # guard against a self-referential loop
        push!(get!(cascade_feeders, gd, Tuple{Int,Int}[]),
              (gu, max(0, genu.cascade_delay_hours)))
    end

    for g in 1:n_gen
        gen = input.generators[g]

        for b in vars.buses_of_gen[g]
            res_cap = gen.reservoir_capacity[b]

            # Skip non-reservoir generators at this bus: constrain reservoir variables to 0
            if res_cap <= 0
                for t in 1:hours
                    @constraint(model, vars.reservoir_spillage[g, b, t] <= 0,
                        base_name = "res_zero_spill_g$(g)_b$(b)_t$(t)")
                    @constraint(model, vars.reservoir_pump[g, b, t] <= 0,
                        base_name = "res_zero_pump_g$(g)_b$(b)_t$(t)")
                end
                for t in 1:(hours+1)
                    @constraint(model, vars.reservoir_level[g, b, t] <= 0,
                        base_name = "res_zero_level_g$(g)_b$(b)_t$(t)")
                end
                continue
            end

            # Reservoir parameters
            initial_level_frac = gen.reservoir_initial_level[b]
            min_level_frac = gen.reservoir_min_level[b]
            max_level_frac = gen.reservoir_max_level[b]
            η_turbine = gen.reservoir_turbine_efficiency[b]
            evap_rate = gen.reservoir_evaporation_rate[b]
            pump_cap = gen.reservoir_pump_capacity[b]
            η_pump = gen.reservoir_pump_efficiency[b]
            min_release_b = b <= length(gen.reservoir_min_release) ?
                gen.reservoir_min_release[b] : 0.0
            head_min_factor_b = b <= length(gen.reservoir_head_min_factor) ?
                gen.reservoir_head_min_factor[b] : 1.0
            rated_b = gen.rated_power[b]

            # In-service reservoir capacity: the existing one, or the caller's
            # existing + built expression (master, Benders). The level bounds,
            # the initial/end level and the head fraction all use it. (The
            # dispatch receives the master's builds as existing capacity.)
            total_res_cap = capacity_override !== nothing && haskey(capacity_override, (g, b)) ?
                capacity_override[(g, b)] : res_cap
            # Usable storage span, the head fraction's denominator. With built
            # capacity as a decision the fraction (level - min·cap)/((max -
            # min)·cap) is not linear, so the span stays at the existing
            # capacity: exact without builds; for an enlarged reservoir it is
            # exact at the minimum level (fraction 0) and overstates the head
            # above it (the fraction passes 1 before the top, where `rated`
            # binds anyway).
            head_span = (max_level_frac - min_level_frac) * res_cap

            # Initial level. Precedence: TSAM seasonal start level >
            # rolling-horizon seam value > configured initial fraction.
            seasonal_init = initial_reservoir_overrides === nothing ? nothing :
                get(initial_reservoir_overrides, (g, b), nothing)
            prev_level = get(get(input.reservoir_level_prev, g, Dict{Int,Float64}()), b, NaN)
            initial_level_energy = isnan(prev_level) ? initial_level_frac * total_res_cap : prev_level
            if seasonal_init !== nothing
                @constraint(model,
                    vars.reservoir_level[g, b, 1] == seasonal_init,
                    base_name = "res_initial_g$(g)_b$(b)")
            else
                @constraint(model,
                    vars.reservoir_level[g, b, 1] == initial_level_energy,
                    base_name = "res_initial_g$(g)_b$(b)")
            end

            for t in 1:hours
                # Level bounds
                @constraint(model,
                    vars.reservoir_level[g, b, t+1] >= min_level_frac * total_res_cap,
                    base_name = "res_min_level_g$(g)_b$(b)_t$(t)")
                @constraint(model,
                    vars.reservoir_level[g, b, t+1] <= max_level_frac * total_res_cap,
                    base_name = "res_max_level_g$(g)_b$(b)_t$(t)")

                # Inflow (MW-eq for this hour)
                inflow_t = t <= size(gen.reservoir_inflow, 1) ? gen.reservoir_inflow[t, b] : 0.0

                # Pump term
                pump_term = if pump_cap > 0
                    @constraint(model, vars.reservoir_pump[g, b, t] <= pump_cap,
                        base_name = "res_pump_limit_g$(g)_b$(b)_t$(t)")
                    vars.reservoir_pump[g, b, t] * η_pump
                else
                    @constraint(model, vars.reservoir_pump[g, b, t] <= 0,
                        base_name = "res_no_pump_g$(g)_b$(b)_t$(t)")
                    AffExpr(0.0)
                end

                # Spillage term
                spillage_term = if gen.reservoir_spillage_allowed
                    vars.reservoir_spillage[g, b, t]
                else
                    @constraint(model, vars.reservoir_spillage[g, b, t] <= 0,
                        base_name = "res_no_spill_g$(g)_b$(b)_t$(t)")
                    AffExpr(0.0)
                end

                # Cascade inflow: water released by upstream reservoirs that
                # feed this one arrives (turbined + spilled) after their travel
                # delay. Only injected at the downstream reservoir's primary
                # node. Releases scheduled before the period start (t-delay < 1)
                # are outside this window and not carried in (a small edge
                # approximation; inter-period in-transit water is not tracked).
                cascade_in = AffExpr(0.0)
                if get(primary_res_bus, g, 0) == b && haskey(cascade_feeders, g)
                    for (gu, delay) in cascade_feeders[g]
                        τ = t - delay
                        τ >= 1 || continue
                        genu = input.generators[gu]
                        for bu in vars.buses_of_gen[gu]
                            genu.reservoir_capacity[bu] > 0 || continue
                            η_u = genu.reservoir_turbine_efficiency[bu]
                            add_to_expression!(cascade_in,
                                vars.gen_output[gu, bu, τ] / η_u)
                            add_to_expression!(cascade_in,
                                vars.reservoir_spillage[gu, bu, τ])
                        end
                    end
                end

                # Water level dynamics:
                # level[t+1] = level[t] * (1 - evap) + inflow + cascade - output/η_turbine + pump*η_pump - spillage
                @constraint(model,
                    vars.reservoir_level[g, b, t+1] ==
                    vars.reservoir_level[g, b, t] * (1 - evap_rate) +
                    inflow_t +
                    cascade_in -
                    vars.gen_output[g, b, t] / η_turbine +
                    pump_term -
                    spillage_term,
                    base_name = "res_dynamics_g$(g)_b$(b)_t$(t)")

                # Minimum environmental / ecological release: the water leaving
                # the reservoir (turbined + spilled) must meet the mandatory
                # downstream flow. Met by generating and/or spilling.
                if min_release_b > 0
                    @constraint(model,
                        vars.gen_output[g, b, t] / η_turbine + spillage_term >=
                        min_release_b,
                        base_name = "res_min_release_g$(g)_b$(b)_t$(t)")
                end

                # Head dependence: a low reservoir means low head means reduced
                # peak turbine power. The available power scales linearly with
                # the fill level, from head_min_factor*rated at the minimum
                # level up to rated at the maximum. Evaluated at the start-of-
                # step level — linear in level, so the model stays an LP.
                #   gen <= rated*(hmf + (1-hmf)*(level - min*cap)/span)
                if head_min_factor_b < 1.0 && rated_b > 0 && head_span > 0
                    @constraint(model,
                        vars.gen_output[g, b, t] <=
                        rated_b * head_min_factor_b +
                        rated_b * (1.0 - head_min_factor_b) *
                        (vars.reservoir_level[g, b, t] - min_level_frac * total_res_cap) /
                        head_span,
                        base_name = "res_head_limit_g$(g)_b$(b)_t$(t)")
                end
            end

            # End level. With TSAM seasonal linking the period end is pinned to
            # the target handed in (start + the period's change: water carries over);
            # otherwise the reservoir is cyclic within the period (end ≈ start).
            seasonal_final = final_reservoir_targets === nothing ? nothing :
                get(final_reservoir_targets, (g, b), nothing)
            if seasonal_final !== nothing
                @constraint(model,
                    vars.reservoir_level[g, b, hours+1] == seasonal_final,
                    base_name = "res_end_seasonal_g$(g)_b$(b)")
            else
                soc_tol = input.soc_end_tolerance
                @constraint(model,
                    vars.reservoir_level[g, b, hours+1] >= initial_level_energy * (1.0 - soc_tol),
                    base_name = "res_end_lower_g$(g)_b$(b)")
                @constraint(model,
                    vars.reservoir_level[g, b, hours+1] <= initial_level_energy * (1.0 + soc_tol),
                    base_name = "res_end_upper_g$(g)_b$(b)")
            end
        end
    end
end

"""
    add_reserve_constraints!(model, vars::PowerSystemVariables, input::PowerSystemInput)

Add the static (spinning) and dynamic (frequency response) reserve
requirements, one per system (`reserve_node_groups`). Each node keeps its own
requirement (`reserve_static_requirement[n]`, or
`reserve_static_default_ratio` × its demand; `reserve_dynamic_requirement[n]`)
and its own held/shortfall variables, but the system's requirements are held
in the pooled headroom of every reservable generator and battery of the
system, wherever it sits:

    Σ_{n∈S} (reserve_static[n,t] + reserve_dynamic[n,t]) ≤ Σ_{n∈S} headroom[n,t]
    Σ_{n∈S} reserve_dynamic[n,t] ≤ Σ_{n∈S} dynamic_capability[n,t]

Reserve is headroom, not energy: static and dynamic reserve share it (a MW of
headroom counts once). In unit commitment only committed generators hold
headroom. Dynamic reserve is further limited to
`dynamic_reserve_contribution` of the rated power that can respond.

Each node's held reserve is the same share of its requirement across the
system (`reserve_static[n,t] = σ[S,t] × requirement[n,t]`, likewise for the
dynamic reserve), so a system shortfall is reported at every node in
proportion to its requirement rather than at an arbitrary node.

In the master (`capacity_override`), a generator's headroom is its capacity
in service × availability − output, as in the dispatch. `extra_headroom(b, t)`
adds the headroom and dynamic capability of further units at bus `b`
(the master's candidate technologies, see `master_unit_reserve_terms`) as a
tuple `(headroom, dynamic)` of expressions, or `nothing`.

Returns `Dict((k, t) => (headroom constraint, dynamic constraint or nothing))`
over system `k` of `reserve_node_groups(input)` and step `t`.
"""
function add_reserve_constraints!(model, vars::PowerSystemVariables, input;
    capacity_override::Union{Dict{Tuple{Int,Int}, Any}, Nothing} = nothing,
    battery_capacity_override::Union{Dict{Tuple{Int,Int}, Any}, Nothing} = nothing,
    demand_scale::Float64 = 1.0,
    extra_headroom::Union{Function, Nothing} = nothing)
    n_bus = input.network.num_buses
    n_node = input.network.num_nodes
    b2n = input.network.bus_to_node
    hours = input.temporal.hours

    # Build node-to-buses mapping
    node_buses = [Int[] for _ in 1:n_node]
    for b in 1:n_bus
        push!(node_buses[b2n[b]], b)
    end

    # Reservable batteries and their discharge power (see battery_reserve_power)
    n_bat = length(input.batteries)
    bats_at_bus = [Int[] for _ in 1:n_bus]
    for bi in 1:n_bat
        input.batteries[bi].reservable || continue
        for b in vars.buses_of_bat[bi]
            push!(bats_at_bus[b], bi)
        end
    end
    bat_power = battery_reserve_power(vars, input, battery_capacity_override)

    # Spinning reserve: in unit commitment an offline unit holds no headroom.
    spinning = capacity_override === nothing && vars.gen_status !== nothing &&
        vars.gen_startup !== nothing

    # Dispatchable units hold reserve (a variable renewable, never)
    reservable(gen, b) = gen.reservable && !is_variable_renewable(gen, b)
    req_dynamic = [get(input.reserve_dynamic_requirement, ni, 0.0) for ni in 1:n_node]
    groups = reserve_node_groups(input)

    req_static = zeros(n_node)
    refs = Dict{Tuple{Int,Int}, Tuple{Any, Any}}()
    for t in 1:hours
        # Per-node requirements: what the node needs held, and its shortfall
        for ni in 1:n_node
            node_demand = input.demand[t, ni] * demand_scale
            req_static[ni] = haskey(input.reserve_static_requirement, ni) ?
                input.reserve_static_requirement[ni] : input.reserve_static_default_ratio * max(node_demand, 0.0)

            @constraint(model,
                vars.reserve_static[ni, t] + vars.reserve_static_loss[ni, t] >= req_static[ni],
                base_name = "reserve_static_$(ni)_$(t)"
            )
            if req_dynamic[ni] > 0
                @constraint(model,
                    vars.reserve_dynamic[ni, t] + vars.reserve_dynamic_loss[ni, t] >= req_dynamic[ni],
                    base_name = "reserve_dynamic_req_$(ni)_$(t)")
            end
        end

        # Per-system: the share of the requirement held, and the pooled headroom
        for (k, nodes) in enumerate(groups)
            share = @variable(model, lower_bound = 0, upper_bound = 1,
                              base_name = "reserve_static_share_s$(k)_t$(t)")
            for ni in nodes
                @constraint(model, vars.reserve_static[ni, t] == req_static[ni] * share,
                    base_name = "reserve_static_held_$(ni)_$(t)")
            end
            has_dynamic = any(req_dynamic[ni] > 0 for ni in nodes)
            if has_dynamic
                share_dynamic = @variable(model, lower_bound = 0, upper_bound = 1,
                                          base_name = "reserve_dynamic_share_s$(k)_t$(t)")
                for ni in nodes
                    @constraint(model, vars.reserve_dynamic[ni, t] == req_dynamic[ni] * share_dynamic,
                        base_name = "reserve_dynamic_held_$(ni)_$(t)")
                end
            else
                for ni in nodes
                    @constraint(model, vars.reserve_dynamic[ni, t] <= 0,
                        base_name = "reserve_dynamic_zero_$(ni)_$(t)")
                end
            end

            # Free headroom of reservable dispatchable generators and batteries
            available_reserve = AffExpr(0.0)
            held = AffExpr(0.0)
            for ni in nodes
                add_to_expression!(held, vars.reserve_static[ni, t])
                add_to_expression!(held, vars.reserve_dynamic[ni, t])
                for b in node_buses[ni]
                    for g in vars.gens_at_bus[b]
                        gen = input.generators[g]
                        reservable(gen, b) || continue
                        if capacity_override !== nothing && haskey(capacity_override, (g, b))
                            add_to_expression!(available_reserve, capacity_override[(g, b)],
                                               gen.availability[t, b])
                            add_to_expression!(available_reserve, -1.0, vars.gen_output[g, b, t])
                        elseif (p_gb = generator_power_in_service(gen, b, input)) > 0
                            cap_t = p_gb * gen.availability[t, b]
                            if spinning
                                add_to_expression!(available_reserve,
                                    online_capacity(model, vars, input, g, b, t, p_gb))
                            else
                                add_to_expression!(available_reserve, cap_t)
                            end
                            add_to_expression!(available_reserve, -1.0, vars.gen_output[g, b, t])
                        end
                    end
                    # Battery contribution: discharge headroom + charge that can be cut.
                    # A battery currently dispatching P_d has (P_d_max - P_d) more to
                    # give; one currently charging P_c can release P_c by stopping.
                    for bi in bats_at_bus[b]
                        p = bat_power(bi, b, t)
                        p === nothing && continue
                        add_to_expression!(available_reserve, p)
                        add_to_expression!(available_reserve, -1.0, vars.bat_discharge[bi, b, t])
                        add_to_expression!(available_reserve, 1.0, vars.bat_charge[bi, b, t])
                    end
                    if extra_headroom !== nothing
                        extra = extra_headroom(b, t)
                        extra === nothing || add_to_expression!(available_reserve, extra[1])
                    end
                end
            end

            # Static and dynamic reserve are held in the same free headroom
            c_headroom = @constraint(model, held <= available_reserve,
                base_name = "reserve_headroom_s$(k)_t$(t)")
            refs[(k, t)] = (c_headroom, nothing)

            # Dynamic reserve: limited to the share of rated power that can respond
            has_dynamic || continue
            available_dynamic = AffExpr(0.0)
            held_dynamic = AffExpr(0.0)
            for ni in nodes
                add_to_expression!(held_dynamic, vars.reserve_dynamic[ni, t])
                for b in node_buses[ni]
                    for g in vars.gens_at_bus[b]
                        gen = input.generators[g]
                        reservable(gen, b) || continue
                        if capacity_override !== nothing && haskey(capacity_override, (g, b))
                            add_to_expression!(available_dynamic,
                                capacity_override[(g, b)], input.dynamic_reserve_contribution)
                        elseif (p_gb = generator_power_in_service(gen, b, input)) > 0
                            if spinning
                                # each online unit responds with its share
                                add_to_expression!(available_dynamic,
                                    input.dynamic_reserve_contribution * p_gb / units_of(gen, b),
                                    vars.gen_status[g, b, t])
                            else
                                add_to_expression!(available_dynamic, input.dynamic_reserve_contribution, p_gb)
                            end
                        end
                    end
                    # Batteries contribute their discharge power to dynamic reserve
                    for bi in bats_at_bus[b]
                        p = bat_power(bi, b, t)
                        p === nothing && continue
                        add_to_expression!(available_dynamic,
                            input.dynamic_reserve_contribution, p)
                    end
                    if extra_headroom !== nothing
                        extra = extra_headroom(b, t)
                        extra === nothing || add_to_expression!(available_dynamic, extra[2])
                    end
                end
            end
            c_dynamic = @constraint(model, held_dynamic <= available_dynamic,
                base_name = "reserve_dynamic_avail_s$(k)_t$(t)")
            refs[(k, t)] = (c_headroom, c_dynamic)
        end
    end
    return refs
end

"""
    reserve_node_groups(input) -> Vector{Vector{Int}}

The nodes of each system, over which `add_reserve_constraints!` pools the
reserve headroom: `input.reserve_node_groups` when the input sets it,
otherwise the systems of `input.co2_budget_groups` (one per original system
in a merged multi-system run; see `system_node_groups`), otherwise one group
with every node.
"""
function reserve_node_groups(input)
    hasproperty(input, :reserve_node_groups) && return input.reserve_node_groups
    n_node = input.network.num_nodes
    hasproperty(input, :co2_budget_groups) || return [collect(1:n_node)]
    return system_node_groups(input.co2_budget_groups, n_node)
end

"""
    system_node_groups(groups, n_node) -> Vector{Vector{Int}}

The node sets of the per-system `groups` (`CO2BudgetGroup`s: a merged run has
one per original system, its contiguous nodes plus its zone nodes). Nodes no
group lists form one extra group; with no groups every node is one system.
"""
function system_node_groups(groups::AbstractVector, n_node::Integer)
    isempty(groups) && return [collect(1:n_node)]
    seen = falses(n_node)
    out = Vector{Int}[]
    for grp in groups
        nodes = [n for n in grp.nodes if 1 <= n <= n_node && !seen[n]]
        seen[nodes] .= true
        isempty(nodes) || push!(out, nodes)
    end
    rest = findall(!, seen)
    isempty(rest) || push!(out, rest)
    return out
end

"""
    battery_reserve_power(vars, input, capacity_override=nothing) -> Function

`(bi, b, t) -> discharge power` that battery `bi` at bus `b` can offer as
headroom at step `t` (`nothing` when it has none): its discharge power
(`battery_power_limits`, or `capacity_override[(bi, b)]` in the master) ×
its availability × its scheduled-outage factor.
"""
function battery_reserve_power(vars, input,
                               capacity_override::Union{Dict{Tuple{Int,Int}, Any}, Nothing} = nothing)
    outage = hasproperty(input, :outage_masks) ?
        get(input.outage_masks, "battery", nothing) : nothing
    base = Dict{Tuple{Int,Int}, Any}()
    for (bi, bat) in enumerate(input.batteries), b in vars.buses_of_bat[bi]
        if capacity_override !== nothing && haskey(capacity_override, (bi, b))
            base[(bi, b)] = capacity_override[(bi, b)]
        else
            p = battery_power_limits(bat, b, input)[3]
            p > 0 && (base[(bi, b)] = p)
        end
    end
    return (bi, b, t) -> begin
        p = get(base, (bi, b), nothing)
        p === nothing && return nothing
        f = storage_availability(input.batteries[bi].availability, t, b) *
            (outage === nothing ? 1.0 : outage[bi, t])
        return p * f
    end
end

"""
    add_inertia_constraints!(model, vars::PowerSystemVariables, input::PowerSystemInput)

Add system inertia constraints to ensure frequency stability.

Matches legacy PowerSystem.py inertia constraint:
  conventional_inertia + storage_inertia + loss_of_inertia >= INERTIA_LIMIT

Inertia is provided by:
- Conventional (non-renewable) generators: rated_power * inertia constant * gen_status
- Storage systems (batteries): (charge + discharge power) * inertia constant
"""
function add_inertia_constraints!(model, vars::PowerSystemVariables, input)
    # Skip if no inertia constraint
    if vars.loss_of_inertia === nothing
        return
    end

    n_gen = length(input.generators)
    n_bat = length(input.batteries)
    n_bus = input.network.num_buses
    hours = input.temporal.hours

    for t in 1:hours
        inertia_limit = if !isempty(input.inertia_limit_hourly) && t <= length(input.inertia_limit_hourly)
            input.inertia_limit_hourly[t]
        else
            input.inertia_limit
        end

        if inertia_limit <= 0
            continue
        end

        conv_inertia = AffExpr(0.0)
        for g in 1:n_gen
            gen = input.generators[g]
            for b in vars.buses_of_gen[g]
                inertia_const = gen.inertia[b]
                if inertia_const > 0
                    add_to_expression!(conv_inertia, vars.gen_output[g, b, t] * inertia_const)
                end
            end
        end

        storage_inertia = AffExpr(0.0)
        for bi in 1:n_bat
            bat = input.batteries[bi]
            for b in vars.buses_of_bat[bi]
                bat_inertia_const = bat.inertia[b]
                add_to_expression!(storage_inertia,
                    (vars.bat_charge[bi, b, t] + vars.bat_discharge[bi, b, t]) * bat_inertia_const)
            end
        end

        # Inertia constraint: conv_inertia + storage_inertia + loss_of_inertia >= inertia_limit
        @constraint(model,
            conv_inertia + storage_inertia + vars.loss_of_inertia[t] >= inertia_limit,
            base_name = "inertia_$(t)"
        )
    end
end

"""
    add_renewable_constraint!(model, vars::PowerSystemVariables, input::PowerSystemInput)

Add renewable energy penetration target constraint — CUMULATIVE SYSTEM-WIDE.

  total_RE + FRE_loss >= target * total_generation

The RE share is renewable generation over total generation
(`is_renewable_unit`, as every RE share of the model): at a 100 % target no
fossil unit runs. Battery discharge is neither: batteries may charge from
fossil sources, so crediting discharge would let storage game the target.
FRE_loss is a penalised slack so the target can never make the LP
infeasible.
"""
function add_renewable_constraint!(model, vars::PowerSystemVariables, input)
    # Skip if no RE target set
    if input.re_penetration_target <= 0
        return
    end

    n_gen = length(input.generators)
    n_bat = length(input.batteries)
    n_bus = input.network.num_buses
    hours = input.temporal.hours
    tres = input.temporal.resolution_hours > 0 ? input.temporal.resolution_hours : 1

    # Total RE and total generation (cumulative, system-wide) — sparse iteration
    total_re = AffExpr(0.0)
    total_gen = AffExpr(0.0)
    for g in 1:n_gen
        gen = input.generators[g]
        renewable = is_renewable_unit(gen)
        for b in vars.buses_of_gen[g], t in 1:hours
            add_to_expression!(total_gen, tres, vars.gen_output[g, b, t])
            renewable && add_to_expression!(total_re, tres, vars.gen_output[g, b, t])
        end
    end

    # NOTE: Battery discharge does NOT count toward RE — batteries may charge
    # from fossil sources, so counting discharge as RE would game the constraint.

    # Total FRE penetration loss (slack, system-wide) — node-level
    n_node = input.network.num_nodes
    total_fre_loss = AffExpr(0.0)
    for n in 1:n_node, t in 1:hours
        add_to_expression!(total_fre_loss, tres, vars.fre_penetration_loss[n, t])
    end

    @constraint(model,
        total_re + total_fre_loss >= input.re_penetration_target * total_gen,
        base_name = "re_penetration_target")
end

"""
    co2_budget_groups(input::PowerSystemInput) -> Vector{CO2BudgetGroup}

The CO2 budgets of the dispatch: the per-system groups of a merged
multi-system run, or one group over all nodes built from `co2_budget` and
`co2_budget_violation_penalty`.
"""
function co2_budget_groups(input::PowerSystemInput)
    isempty(input.co2_budget_groups) || return input.co2_budget_groups
    return [CO2BudgetGroup(input.name, collect(1:input.network.num_nodes),
                           input.co2_budget, input.co2_budget_violation_penalty)]
end

has_co2_budget(grp::CO2BudgetGroup) = !isinf(grp.budget) && grp.budget > 0

"""
    add_co2_constraint!(model, vars::PowerSystemVariables, input::PowerSystemInput)

Add one CO2 emissions budget constraint per budget group (one per system in
a merged multi-system run), each with its own violation slack:

  sum(CO2 of the group's generators) <= budget × window fraction + violation

The violation variables are penalized in the objective at each group's own
penalty.
"""
function add_co2_constraint!(model, vars::PowerSystemVariables, input::PowerSystemInput)
    groups = co2_budget_groups(input)
    any(has_co2_budget, groups) || return
    # Without per-system groups the single budget covers every bus.
    per_system = !isempty(input.co2_budget_groups)

    n_gen = length(input.generators)
    hours = input.temporal.hours
    b2n = input.network.bus_to_node

    tres = input.temporal.resolution_hours > 0 ? input.temporal.resolution_hours : 1

    # Scale CO2 budget by window fraction (matches Python legacy: budget * hours * tres / year_hours)
    window_fraction = hours * tres / Float64(input.hours_per_year)

    for (k, grp) in enumerate(groups)
        has_co2_budget(grp) || continue
        in_group = Set(grp.nodes)

        # Group CO2 emissions (t): output (MW) × step length (h) × intensity (t/MWh_e)
        total_co2 = AffExpr(0.0)
        for g in 1:n_gen, b in vars.buses_of_gen[g]
            per_system && !(b2n[b] in in_group) && continue
            co2_factor = co2_per_mwh(input.fuel_co2, input.generators[g], b)
            co2_factor == 0.0 && continue
            for t in 1:hours
                add_to_expression!(total_co2, co2_factor * tres, vars.gen_output[g, b, t])
            end
        end

        scaled_budget = grp.budget * window_fraction
        cname = length(groups) == 1 ? "CO2_budget_constraint" : "CO2_budget_constraint[$(grp.name)]"
        if vars.co2_budget_violation !== nothing && haskey(vars.co2_budget_violation, k)
            @constraint(model, total_co2 <= scaled_budget + vars.co2_budget_violation[k],
                       base_name = cname)
        else
            # Fallback to hard constraint if no violation variable
            @constraint(model, total_co2 <= scaled_budget, base_name = cname)
        end
    end
end

"""
    add_co2_emissions_definition!(model, vars::PowerSystemVariables, input::PowerSystemInput)

Define the CO2 emission rate (tCO2/h) at each node and step; the tonnes
emitted in step t are `CO2_emissions[node, t] × resolution_hours`.

  CO2_emissions[node, t] == sum(gen_output[g, b, t] * co2_per_mwh(g, b) for g, b at node)
"""
function add_co2_emissions_definition!(model, vars::PowerSystemVariables, input::PowerSystemInput)
    n_gen = length(input.generators)
    n_bus = input.network.num_buses
    n_node = input.network.num_nodes
    b2n = input.network.bus_to_node
    hours = input.temporal.hours

    # Build node-to-buses mapping
    node_buses = [Int[] for _ in 1:n_node]
    for b in 1:n_bus
        push!(node_buses[b2n[b]], b)
    end

    for ni in 1:n_node, t in 1:hours
        co2_expr = AffExpr(0.0)
        for b in node_buses[ni]
            for g in vars.gens_at_bus[b]
                co2_factor = co2_per_mwh(input.fuel_co2, input.generators[g], b)
                co2_factor == 0.0 && continue
                add_to_expression!(co2_expr, co2_factor, vars.gen_output[g, b, t])
            end
        end

        @constraint(model,
            vars.co2_emissions[ni, t] == co2_expr,
            base_name = "co2_emissions_def_n$(ni)_t$(t)"
        )
    end
end

"""
    add_curtailment_constraints!(model, vars::PowerSystemVariables, input::PowerSystemInput)

Define curtailment as the difference between available and used renewable generation.

Matches legacy PowerSystem.py (line 1410):
  curtailment[node][t] == available_renewable_gen - used_renewable_gen

Also enforces that curtailment cannot exceed available renewable capacity (line 1412-1420).
"""
function add_curtailment_constraints!(model, vars::PowerSystemVariables, input;
    capacity_override::Union{Dict{Tuple{Int,Int}, Any}, Nothing} = nothing)
    n_gen = length(input.generators)
    n_bus = input.network.num_buses
    n_node = input.network.num_nodes
    b2n = input.network.bus_to_node
    hours = input.temporal.hours

    # Build node-to-buses mapping
    node_buses = [Int[] for _ in 1:n_node]
    for b in 1:n_bus
        push!(node_buses[b2n[b]], b)
    end

    for t in 1:hours
        for ni in 1:n_node
            available_renewable = AffExpr(0.0)
            used_renewable = AffExpr(0.0)

            for b in node_buses[ni]
                for g in vars.gens_at_bus[b]
                    gen = input.generators[g]
                    # Only variable renewables are curtailed: a dispatchable
                    # unit (reservoir hydro, biomass, ...) that runs below
                    # its availability is not spilling anything.
                    if is_variable_renewable(gen, b)
                        avail = gen.availability[t, b]
                        if capacity_override !== nothing && haskey(capacity_override, (g, b))
                            add_to_expression!(available_renewable,
                                capacity_override[(g, b)] * avail)
                        else
                            # Power in service: a unit past its lifetime
                            # has nothing to curtail
                            add_to_expression!(available_renewable,
                                generator_power_in_service(gen, b, input) * avail)
                        end
                        add_to_expression!(used_renewable, 1.0, vars.gen_output[g, b, t])
                    end
                end
            end

            @constraint(model,
                vars.curtailment[ni, t] == available_renewable - used_renewable)

            @constraint(model,
                vars.curtailment[ni, t] <= available_renewable)
        end
    end
end

# =============================================================================
# N-1 Security Constraints
# Matches Python legacy power_system.py lines 5415-5516
# =============================================================================

"""
    identify_n1_critical_elements(input::PowerSystemInput)

Identify critical elements for N-1 security analysis.

Returns:
    Dict with:
    - 'lines': Vector of (i, j, capacity) tuples for critical transmission lines
    - 'generators': Dict{Int, Dict} with largest generator per node

Matches Python legacy _identify_n1_critical_elements (lines 480-526).
"""
function identify_n1_critical_elements(input::PowerSystemInput)
    n_bus = input.network.num_buses
    n_gen = length(input.generators)

    critical_elements = Dict{String, Any}(
        "lines" => Vector{Tuple{Int,Int,Float64}}(),
        "generators" => Dict{Int, Dict{String, Any}}()
    )

    # Identify critical transmission lines (bus×bus connections)
    for i in 1:n_bus
        for j in (i+1):n_bus
            capacity = get(input.network.connections, (i, j), 0.0)
            if capacity > 0
                push!(critical_elements["lines"], (i, j, capacity))
            end
        end
    end

    # Identify largest generator at each bus
    for b in 1:n_bus
        bus_generators = Tuple{Int, Float64}[]
        for g in 1:n_gen
            gen = input.generators[g]
            if gen.rated_power[b] > 0
                push!(bus_generators, (g, gen.rated_power[b]))
            end
        end

        if !isempty(bus_generators)
            largest_capacity, largest_idx = findmax(x -> x[2], bus_generators)
            g_idx, cap = bus_generators[largest_idx]
            critical_elements["generators"][b] = Dict{String, Any}(
                "gen_idx" => g_idx,
                "name" => input.generators[g_idx].name,
                "capacity" => cap
            )
        end
    end

    return critical_elements
end

"""
    add_n1_security_constraints!(model, vars::PowerSystemVariables, input::PowerSystemInput)

Preventive N-1 security: the system must survive the loss of its largest
element without re-dispatch.

1. Generation N-1, per system (`reserve_node_groups`: one per original system
   of a merged run): the headroom of the system's other reservable units
   (online ones in unit commitment) and batteries must cover the output of
   the system's largest unit (`n1_generation_reserve_type = "largest_unit"`)
   or `n1_generation_reserve_percentage` × the system's demand of each step
   (`"percentage"`).
2. Transmission N-1: each line carries at most `n1_transmission_reserve_factor`
   of its capacity, leaving room for post-contingency redistribution.

Both are soft: shortfalls are priced at `loss_of_reserve_static`.
"""
function add_n1_security_constraints!(model, vars::PowerSystemVariables, input::PowerSystemInput)
    input.n1_security_enabled || return
    n1_cost = AffExpr(0.0)
    if input.n1_generation_enabled
        _add_gen_n1!(model, vars, input, n1_cost; corrective = false,
                     cname = "n1_gen_reserve_system")
    end
    if input.n1_transmission_enabled
        _add_line_n1_reserve!(model, vars, input, n1_cost)
    end
    _charge_n1_shortfall!(model, n1_cost, input)
end

"""
    _n1_largest_unit(input, in_system=b -> true) -> (g, b)

Generator and bus of the largest unit by rated power among the buses
`in_system(b)` accepts, or (0, 0) if none.
"""
function _n1_largest_unit(input::PowerSystemInput, in_system::Function = b -> true)
    best = (0, 0)
    best_cap = 0.0
    for (g, gen) in enumerate(input.generators), b in eachindex(gen.rated_power)
        in_system(b) || continue
        p_gb = generator_power_in_service(gen, b, input)
        if p_gb > best_cap
            best_cap = p_gb
            best = (g, b)
        end
    end
    return best
end

"""
    _add_gen_n1!(model, vars, input, n1_cost; corrective, cname)

Generation N-1, per system S of `reserve_node_groups(input)` (one per
original system of a merged run): if the system's largest unit (g*, b*)
trips, the rest of that system must replace its output. For each step

    cover_{S,t} + shortfall_{S,t} ≥ gen_output[g*, b*, t]

(constraint `\$(cname)_t\$(t)` with one system, `\$(cname)_s\$(k)_t\$(t)`
for system k of several). With `n1_generation_reserve_type = "percentage"`
the requirement is instead `n1_generation_reserve_percentage × Σ_{n∈S}
demand[t, n]` and no unit is excluded from the cover.

with cover_{S,t}, over the buses of S only, the batteries' headroom
(discharge power × availability − discharge + charge; reservable batteries
only when preventive) plus,
per non-renewable unit other than (g*, b*), its headroom
(available capacity, × status in unit commitment, − output):
- preventive (`corrective = false`): reservable units only, full headroom
  (spinning reserve, the RES-3 rule);
- corrective: every non-renewable unit, re-dispatched within one step of its
  ramp limit (`ramp_up × Δt` of rated power; no limit when the ramp is 0).

The shortfall is priced at `loss_of_reserve_static` through `n1_cost`.
"""
function _add_gen_n1!(model, vars::PowerSystemVariables, input::PowerSystemInput,
                      n1_cost::AffExpr; corrective::Bool, cname::String)
    pct = input.n1_generation_reserve_type == "percentage"
    pct && input.n1_generation_reserve_percentage <= 0 && return
    groups = reserve_node_groups(input)
    b2n = input.network.bus_to_node
    for (k, nodes) in enumerate(groups)
        in_system = b -> b2n[b] in nodes
        prefix = length(groups) == 1 ? cname : "$(cname)_s$(k)"
        if pct
            g_star, b_star = 0, 0
        else
            g_star, b_star = _n1_largest_unit(input, in_system)
            (g_star == 0 || !(b_star in vars.buses_of_gen[g_star])) && continue
        end
        _add_system_gen_n1!(model, vars, input, n1_cost, in_system, nodes,
                            g_star, b_star; corrective, cname = prefix)
    end
end

# Generation N-1 of one system (buses `in_system`, nodes `nodes`); the largest
# unit (g_star, b_star), or (0, 0) for the percentage requirement
function _add_system_gen_n1!(model, vars::PowerSystemVariables, input::PowerSystemInput,
                             n1_cost::AffExpr, in_system::Function, nodes,
                             g_star::Int, b_star::Int; corrective::Bool, cname::String)
    pct = g_star == 0
    spinning = vars.gen_status !== nothing && vars.gen_startup !== nothing
    tres = input.temporal.resolution_hours > 0 ? input.temporal.resolution_hours : 1
    bat_power = battery_reserve_power(vars, input)
    for t in 1:input.temporal.hours
        cover = AffExpr(0.0)
        for (g, gen) in enumerate(input.generators)
            corrective || gen.reservable || continue
            for b in vars.buses_of_gen[g]
                in_system(b) || continue
                is_variable_renewable(gen, b) && continue
                p_gb = generator_power_in_service(gen, b, input)
                ((g, b) == (g_star, b_star) || p_gb <= 0) && continue
                headroom = AffExpr(0.0)
                cap_t = p_gb * gen.availability[t, b]
                if spinning
                    add_to_expression!(headroom,
                        online_capacity(model, vars, input, g, b, t, p_gb))
                else
                    add_to_expression!(headroom, cap_t)
                end
                add_to_expression!(headroom, -1.0, vars.gen_output[g, b, t])
                ramp = corrective && gen.ramp_up[b] > 0 ? gen.ramp_up[b] * tres : Inf
                if ramp < 1
                    response = @variable(model, lower_bound = 0,
                        upper_bound = p_gb * ramp,
                        base_name = "$(cname)_response_g$(g)_b$(b)_t$(t)")
                    @constraint(model, response <= headroom)
                    add_to_expression!(cover, response)
                else
                    add_to_expression!(cover, headroom)
                end
            end
        end
        for bi in eachindex(input.batteries), b in vars.buses_of_bat[bi]
            corrective || input.batteries[bi].reservable || continue
            in_system(b) || continue
            p = bat_power(bi, b, t)
            p === nothing && continue
            add_to_expression!(cover, p)
            add_to_expression!(cover, -1.0, vars.bat_discharge[bi, b, t])
            add_to_expression!(cover, 1.0, vars.bat_charge[bi, b, t])
        end
        short = @variable(model, lower_bound = 0, base_name = "$(cname)_shortfall_t$(t)")
        need = pct ?
            input.n1_generation_reserve_percentage * sum(input.demand[t, n] for n in nodes) :
            vars.gen_output[g_star, b_star, t]
        @constraint(model, cover + short >= need, base_name = "$(cname)_t$(t)")
        add_to_expression!(n1_cost, input.loss_of_reserve_static, short)
    end
end

"""
    _line_flows(vars, input) -> Vector{Union{Nothing, Tuple{Vector{VariableRef}, Float64}}}

For each entry of `input.network.transmission_lines`: its own flow variables
(`power_flow_by_line`, one per physical line) and the sign that turns them into
its from→to direction (TransmissionDC stores lines low→high bus and skips
self-loops), or `nothing` for a self-loop or a run without DC flows.
"""
function _line_flows(vars::PowerSystemVariables, input::PowerSystemInput)
    lines = input.network.transmission_lines
    out = Vector{Union{Nothing, Tuple{Vector{VariableRef}, Float64}}}(nothing, length(lines))
    vars.power_flow_by_line === nothing && return out
    idx = 0
    for (k, line) in enumerate(lines)
        line.from_node == line.to_node && continue
        idx += 1
        idx <= length(vars.power_flow_by_line) || break
        out[k] = (vars.power_flow_by_line[idx], line.from_node < line.to_node ? 1.0 : -1.0)
    end
    return out
end

"""
    _add_line_n1_reserve!(model, vars, input, n1_cost)

Preventive transmission N-1: each line (all of its circuits together)
carries at most `n1_transmission_reserve_factor` × its capacity;
the excess is a priced slack (`n1_trans_over_l*_t*`).
"""
function _add_line_n1_reserve!(model, vars::PowerSystemVariables, input::PowerSystemInput,
                               n1_cost::AffExpr)
    factor = input.n1_transmission_reserve_factor
    for (k, lf) in enumerate(_line_flows(vars, input))
        lf === nothing && continue
        pf, _ = lf
        line = input.network.transmission_lines[k]
        usable = line.capacity_mw * factor
        usable > 0 || continue
        for t in 1:input.temporal.hours
            over = @variable(model, lower_bound = 0, base_name = "n1_trans_over_l$(k)_t$(t)")
            @constraint(model, pf[t] <= usable + over, base_name = "n1_trans_reserve_l$(k)_t$(t)_pos")
            @constraint(model, pf[t] >= -usable - over, base_name = "n1_trans_reserve_l$(k)_t$(t)_neg")
            add_to_expression!(n1_cost, input.loss_of_reserve_static, over)
        end
    end
end

"""
    charge_converter_costs!(model, vars, input)

Add the converters' variable cost (`add_converter_objective_terms`, an energy
cost × step length) to the objective and to the `converter_cost` breakdown.
Called after `add_converter_constraints!`: `build_objective!` runs before the
converter variables exist.
"""
function charge_converter_costs!(model, vars::PowerSystemVariables, input::PowerSystemInput)
    cost = add_converter_objective_terms(vars, input)
    (isempty(cost.terms) && cost.constant == 0) && return
    tres = input.temporal.resolution_hours > 0 ? input.temporal.resolution_hours : 1
    @objective(model, Min, objective_function(model) + tres * cost)
    model.ext[:cost_expressions][:converter_cost] = cost
end

"""
    add_line_flow_costs!(model, vars, input)

Charge each line with a `flow_cost` for the energy it carries in either
direction: `|f_l,t| ≤ a_l,t` (`line_flow_abs_l*_t*_pos/neg`) and
`Δt × flow_cost_l × a_l,t` in the objective and in the `line_flow_cost`
breakdown. Lines without a cost get no variables. Needs per-line DC flows; no
effect in ACOPF or single-bus runs.
"""
function add_line_flow_costs!(model, vars::PowerSystemVariables, input::PowerSystemInput)
    lines = input.network.transmission_lines
    any(l -> l.flow_cost > 0, lines) || return
    cost = AffExpr(0.0)
    for (k, lf) in enumerate(_line_flows(vars, input))
        c = lines[k].flow_cost
        (lf === nothing || c <= 0) && continue
        pf, _ = lf
        for t in 1:input.temporal.hours
            a = @variable(model, lower_bound = 0, base_name = "line_flow_abs_l$(k)_t$(t)")
            @constraint(model, a >= pf[t], base_name = "line_flow_abs_l$(k)_t$(t)_pos")
            @constraint(model, a >= -pf[t], base_name = "line_flow_abs_l$(k)_t$(t)_neg")
            add_to_expression!(cost, c, a)
        end
    end
    isempty(cost.terms) && return
    tres = input.temporal.resolution_hours > 0 ? input.temporal.resolution_hours : 1
    @objective(model, Min, objective_function(model) + tres * cost)
    model.ext[:cost_expressions][:line_flow_cost] = cost
end

"""
    _charge_n1_shortfall!(model, n1_cost, input)

The objective is built before the N-1 constraints: add their priced slacks
to it (an energy cost, scaled by the step length as in build_objective!) and
to the cost breakdown.
"""
function _charge_n1_shortfall!(model, n1_cost::AffExpr, input::PowerSystemInput)
    tres = input.temporal.resolution_hours > 0 ? input.temporal.resolution_hours : 1
    @objective(model, Min, objective_function(model) + tres * n1_cost)
    model.ext[:cost_expressions][:n1_security_shortfall_cost] = n1_cost
end

# =============================================================================
# SCOPF (Security-Constrained OPF) — Iterative Contingency Analysis
# =============================================================================

"""
    _build_ptdf_matrix(input::PowerSystemInput) -> Matrix{Float64}

Build the Power Transfer Distribution Factor matrix.
PTDF[l, b] = sensitivity of flow on line l to injection at bus b.

PTDF = diag(1/x) × A × B_bus^(-1)
where A is the branch-bus incidence matrix, B_bus is the bus susceptance matrix.
"""
function _build_ptdf_matrix(input::PowerSystemInput)
    n_bus = input.network.num_buses
    lines = input.network.transmission_lines
    n_lines = length(lines)
    slack = input.network.slack_bus

    # Build bus susceptance matrix B
    B = zeros(n_bus, n_bus)
    for line in lines
        i, j = line.from_node, line.to_node
        x = line.reactance_pu
        if abs(x) < 1e-12 || i == j
            continue
        end
        b = 1.0 / x
        B[i, i] += b
        B[j, j] += b
        B[i, j] -= b
        B[j, i] -= b
    end

    # Remove slack bus, invert, then restore
    non_slack = setdiff(1:n_bus, [slack])
    B_red = B[non_slack, non_slack]

    # Use pseudo-inverse for robustness
    B_inv_red = zeros(n_bus, n_bus)
    if size(B_red, 1) > 0
        B_inv_full = pinv(B_red)
        for (ri, i) in enumerate(non_slack)
            for (ci, j) in enumerate(non_slack)
                B_inv_red[i, j] = B_inv_full[ri, ci]
            end
        end
    end

    # Build PTDF: for each line l from i to j with reactance x_l
    # PTDF[l, b] = (B_inv[i,b] - B_inv[j,b]) / x_l
    ptdf = zeros(n_lines, n_bus)
    for (l, line) in enumerate(lines)
        i, j = line.from_node, line.to_node
        x = line.reactance_pu
        if abs(x) < 1e-12 || i == j
            continue
        end
        for b in 1:n_bus
            ptdf[l, b] = (B_inv_red[i, b] - B_inv_red[j, b]) / x
        end
    end

    return ptdf
end

"""
    _build_lodf_matrix(ptdf::Matrix{Float64}, lines) -> Matrix{Float64}

Build Line Outage Distribution Factor matrix from PTDF.
LODF[l, k] = change in flow on line l per unit of pre-outage flow on line k,
when line k is removed.

LODF[l, k] = (PTDF[l, from_k] - PTDF[l, to_k]) / (1 - (PTDF[k, from_k] - PTDF[k, to_k]))
"""
function _build_lodf_matrix(ptdf::Matrix{Float64}, lines)
    n_lines = length(lines)
    lodf = zeros(n_lines, n_lines)

    for k in 1:n_lines
        i_k, j_k = lines[k].from_node, lines[k].to_node
        denom = 1.0 - (ptdf[k, i_k] - ptdf[k, j_k])

        if abs(denom) < 1e-10
            # Line k is a bridge (radial) — its outage isolates buses
            # LODF is undefined; set to zero (no redistribution possible)
            for l in 1:n_lines
                if l != k
                    lodf[l, k] = 0.0
                end
            end
            continue
        end

        for l in 1:n_lines
            if l == k
                lodf[l, k] = -1.0  # Flow on tripped line goes to zero
                continue
            end
            lodf[l, k] = (ptdf[l, i_k] - ptdf[l, j_k]) / denom
        end
    end

    return lodf
end

"""
    add_scopf_constraints!(model, vars::PowerSystemVariables, input::PowerSystemInput)

Security-constrained OPF (`n1_scopf_enabled`):
1. Transmission: for every outage of line k, every other line l stays within
   its capacity after the flow of k redistributes through the LODF:
   |f_l + LODF[l,k] f_k| ≤ cap_l + over_l,t (`scopf_line*_outage*_t*_pos/neg`).
   Flows are per physical line, in each line's from→to direction.
2. Generation: preventive N-1 (`scopf_n1_gen_reserve_t*`, as in
   add_n1_security_constraints!) or, with `n1_corrective_enabled`, corrective
   re-dispatch within ramp limits (`n1_corrective_gen_t*`).

All are soft: overloads and shortfalls are priced at `loss_of_reserve_static`.
"""
function add_scopf_constraints!(model, vars::PowerSystemVariables, input::PowerSystemInput)
    input.n1_security_enabled || return
    hours = input.temporal.hours
    lines = input.network.transmission_lines
    n1_cost = AffExpr(0.0)

    if input.n1_transmission_enabled && !isempty(lines)
        lodf = _build_lodf_matrix(_build_ptdf_matrix(input), lines)
        flows = _line_flows(vars, input)
        caps = [line.capacity_mw for line in lines]
        over = Dict{Tuple{Int,Int}, VariableRef}()   # (l, t) → worst overload of l
        for k in eachindex(lines), l in eachindex(lines)
            (l == k || caps[k] <= 0 || caps[l] <= 0 || abs(lodf[l, k]) < 1e-6) && continue
            (flows[l] === nothing || flows[k] === nothing) && continue
            (pf_l, s_l), (pf_k, s_k) = flows[l], flows[k]
            for t in 1:hours
                o = get!(over, (l, t)) do
                    v = @variable(model, lower_bound = 0, base_name = "scopf_over_l$(l)_t$(t)")
                    add_to_expression!(n1_cost, input.loss_of_reserve_static, v)
                    v
                end
                post = s_l * pf_l[t] + lodf[l, k] * s_k * pf_k[t]
                @constraint(model, post <= caps[l] + o,
                    base_name = "scopf_line$(l)_outage$(k)_t$(t)_pos")
                @constraint(model, post >= -caps[l] - o,
                    base_name = "scopf_line$(l)_outage$(k)_t$(t)_neg")
            end
        end
    end

    if input.n1_generation_enabled
        if input.n1_corrective_enabled
            _add_gen_n1!(model, vars, input, n1_cost; corrective = true,
                         cname = "n1_corrective_gen")
        else
            _add_gen_n1!(model, vars, input, n1_cost; corrective = false,
                         cname = "scopf_n1_gen_reserve")
        end
    end
    _charge_n1_shortfall!(model, n1_cost, input)
end

"""
    sector_remainder_multiplier(sectors, criticality, demand_of) -> Float64

VOLL multiplier of the unclassified remainder of a node's demand (the node's
demand minus its sectors' demand) in one step: the sectors' criticality
multipliers weighted by their demand there, `Σ_s κ_s d_s / Σ_s d_s`
(`demand_of(sector)` gives d_s); 1 where no sector has demand. Shared by the
dispatch and the master's representative days.
"""
function sector_remainder_multiplier(sectors, criticality, demand_of)
    num = 0.0
    den = 0.0
    for s in sectors
        d = max(0.0, demand_of(s))
        num += get(criticality, s, 1.0) * d
        den += d
    end
    return den > 0 ? num / den : 1.0
end

"""
    _sectoral_demand_at(input, sector, t, ni) -> Float64

Demand of `sector` at node `ni` and step `t` (MW, >= 0); 0 when the sector's
array does not cover that node or step.
"""
function _sectoral_demand_at(input, sector, t, ni)
    arr = get(input.sectoral_demand, sector, nothing)
    arr === nothing && return 0.0
    (t <= size(arr, 1) && ni <= size(arr, 2)) || return 0.0
    return max(0.0, arr[t, ni])
end

# Export the new constraint functions
export add_renewable_constraint!, add_co2_constraint!, add_co2_emissions_definition!, add_curtailment_constraints!
export add_n1_security_constraints!, identify_n1_critical_elements
export add_scopf_constraints!

"""
    add_sectoral_demand_constraints!(model, vars::PowerSystemVariables, input::PowerSystemInput)

Add sectoral load shedding constraints, per node and step:
1. Each sector sheds at most its own demand: lol_sector <= sectoral_demand[sector].
2. The node's shed splits into the sectors' shed plus an unclassified
   remainder: load_shed - sum(lol_sector) in [0, max(0, demand - sum(sectoral_demand))].

The remainder covers node demand that no sector accounts for (sectoral data
missing for a node, or not matching the demand passed in), so the split is
always feasible. The objective prices each sector's shed at
VOLL x criticality and the remainder at VOLL x the demand-weighted mean of the
node's sector criticalities in that step (`sector_remainder_multiplier`; 1
where no sector has demand). With the per-sector caps, the cheapest sector can
no longer absorb all shed: dearer sectors are shed only once the cheaper ones
are fully shed.
"""
function add_sectoral_demand_constraints!(model, vars::PowerSystemVariables, input::PowerSystemInput)
    if vars.loss_of_load_sectoral === nothing || isempty(vars.loss_of_load_sectoral)
        return
    end

    n_node = input.network.num_nodes
    n_bus = input.network.num_buses
    hours = input.temporal.hours
    sectors = collect(keys(vars.loss_of_load_sectoral))

    # Buses per node (load_shed is now per-bus; sectoral lol stays per-node).
    buses_of_node = [Int[] for _ in 1:n_node]
    for b in 1:n_bus
        push!(buses_of_node[input.network.bus_to_node[b]], b)
    end

    for ni in 1:n_node, t in 1:hours
        # 1. Each sector sheds at most its own demand. Without this cap the
        # whole shed went to the sector with the lowest criticality, so all
        # unserved energy was priced at the cheapest multiplier.
        sector_lol_sum = AffExpr(0.0)
        sector_demand_sum = 0.0
        for sector in sectors
            lol = vars.loss_of_load_sectoral[sector][ni, t]
            add_to_expression!(sector_lol_sum, 1.0, lol)
            sec_dem = _sectoral_demand_at(input, sector, t, ni)
            sector_demand_sum += sec_dem
            @constraint(model, lol <= sec_dem,
                base_name = "sectoral_lol_cap_$(sector)_$(ni)_$(t)")
        end
        node_load_shed = AffExpr(0.0)
        for b in buses_of_node[ni]
            add_to_expression!(node_load_shed, 1.0, vars.load_shed[b, t])
        end

        # 2. The node's shed = sectoral shed + an unclassified remainder,
        # bounded by the demand no sector accounts for. Sectoral data can
        # miss a node (e.g. the Grid Builder split demand across new buses
        # without rebuilding the sector shares); the remainder keeps the
        # split feasible there instead of forcing load_shed = 0.
        residual_cap = max(0.0, input.demand[t, ni] - sector_demand_sum)
        @constraint(model, node_load_shed - sector_lol_sum >= 0.0,
            base_name = "sectoral_lol_sum_$(ni)_$(t)")
        @constraint(model, node_load_shed - sector_lol_sum <= residual_cap,
            base_name = "sectoral_lol_residual_$(ni)_$(t)")
    end

    # B13: Demand shifting: what leaves a step is at most flexibility_ratio of
    # the sector's demand there.
    if vars.demand_shift !== nothing
        for (sector, shift_vars) in vars.demand_shift
            ratio = input.sectoral_flexibility_ratio[sector]
            out = Dict{Tuple{Int,Int}, AffExpr}()
            for ((ni, t, _), var) in shift_vars
                add_to_expression!(get!(out, (ni, t), AffExpr(0.0)), var)
            end
            for ((ni, t), expr) in out
                sec_dem = max(0.0, input.sectoral_demand[sector][t, ni])
                @constraint(model, expr <= ratio * sec_dem,
                    base_name = "demand_shift_out_cap_$(sector)_$(ni)_$(t)")
            end
        end
    end
end

"""
    demand_shift_net(vars) -> Dict{Tuple{Int,Int}, AffExpr}

Net demand moved into each (node, step) by demand shifting: shifts in minus
shifts out, summed over flexible sectors. Added to the demand side of the bus
balances, scaled by the bus's demand_fraction like the other node-level terms.
"""
function demand_shift_net(vars)
    net = Dict{Tuple{Int,Int}, AffExpr}()
    vars.demand_shift === nothing && return net
    for shift_vars in values(vars.demand_shift), ((ni, t, t_dest), var) in shift_vars
        add_to_expression!(get!(net, (ni, t_dest), AffExpr(0.0)), var)
        add_to_expression!(get!(net, (ni, t), AffExpr(0.0)), -1.0, var)
    end
    return net
end

"""
Price of each \$ a dispatch window spends above its operating budget
(`add_operating_budget!`): 1000 \$ per \$. Above the budget a MWh from a unit
with variable cost c costs c × 1001, so shedding the load (at VOLL) is
cheaper for every unit with c > VOLL / 1001, i.e. above 1 \$/MWh at a VOLL of
1000 \$/MWh: fuel the budget cannot pay for becomes unserved energy, as a
fuel shortage would. A ratio, so the cost scaling (M\$) leaves it as it is;
any higher would only widen the objective's coefficient range.
"""
const OPERATING_BUDGET_PENALTY = 1000.0

"""
The dispatch's operating spending for the operating budget: the money it
pays, as entries of `model.ext[:cost_expressions]` (with `true` for a
per-step energy cost, × step length): the generators' fuel, maintenance and
start-ups, the batteries' maintenance, the window's share of the fixed O&M,
the electrolyzers' cost, converter and line-flow costs and, with primary
energy, the fuel bought and its transport (a fuel-coupled unit pays its
fuel there, not in `fuel_cost`). The master's `budget_scope: total` budget
counts the same categories (`day_operating_spend`). Modelling penalties
(unserved energy, reserve, inertia, N-1, curtailment, RE shortfall, SOC,
spillage, fuel shortfall), battery degradation and the CO2 price (a
transfer to the state) are not spending.
"""
const OPERATING_BUDGET_COSTS = ((:fuel_cost, true), (:maintenance_cost, true),
                                (:startup_cost, false), (:battery_maintenance_cost, true),
                                (:fixed_om_cost, false), (:electrolyzer_cost, true),
                                (:converter_cost, true), (:line_flow_cost, true),
                                (:pe_supply_cost, false), (:pe_transport_cost, false))

"""
    add_operating_budget!(model, budget; committed=nothing) -> VariableRef

COST-1: the window's operating spending (`OPERATING_BUDGET_COSTS`) stays
within `budget` (solver cost units), plus the excess
`operating_budget_excess`, priced at `OPERATING_BUDGET_PENALTY` per unit.
With `committed` (a [`CommittedSteps`](@ref)) the spending is that of the
window's committed steps only: the runner gives each window its share of
what is left of `max_annual_operating_cost` for the year and carries what
the committed steps spent. Call it once the objective is complete (after the
primary-energy coupling). The excess and its penalty go to the cost
breakdown (`operating_budget_excess`, `operating_budget_penalty`).
"""
function add_operating_budget!(model, budget::Real; committed = nothing)
    ce = model.ext[:cost_expressions]
    tres = get(model.ext, :temporal_resolution_hours, 1.0)
    spend = AffExpr(0.0)
    for (sym, is_energy) in OPERATING_BUDGET_COSTS
        ex = get(ce, sym, nothing)
        ex === nothing || add_to_expression!(spend, ex, is_energy ? tres : 1.0)
    end
    committed === nothing || (spend = committed_expression(committed, spend))
    # Named as a variable of step 1, so a committed valuation (CommittedSteps)
    # counts it in full: it is the excess of the committed steps
    excess = @variable(model, lower_bound = 0.0, base_name = "operating_budget_excess_t1")
    @constraint(model, spend - excess <= max(Float64(budget), 0.0),
        base_name = "operating_budget")
    set_objective_function(model,
        objective_function(model) + OPERATING_BUDGET_PENALTY * excess)
    ce[:operating_budget_excess] = 1.0 * excess
    ce[:operating_budget_penalty] = OPERATING_BUDGET_PENALTY * excess
    return excess
end

"""
    add_electrolyzer_dispatch_constraints!(model, vars::PowerSystemVariables, input::PowerSystemInput)

Bound each electrolyzer's consumption by its rated power per bus, times its
scheduled-outage availability (`outage_masks["electrolyzer"]`, electrolyzer ×
step, whole unit), and apply its ramp rates (fraction of rated power per
hour, scaled to the time step; <= 0 = no limit). The dispatch has no
electrolyzer investment variable, so a bus without rated power gets no
electrolyzer load.
"""
function add_electrolyzer_dispatch_constraints!(model, vars::PowerSystemVariables, input::PowerSystemInput)
    vars.electrolyzer_power === nothing && return
    hours = input.temporal.hours
    tres = input.temporal.resolution_hours > 0 ? input.temporal.resolution_hours : 1
    outage = get(input.outage_masks, "electrolyzer", nothing)
    avail(e, t) = (outage === nothing || e > size(outage, 1) || t > size(outage, 2)) ?
        1.0 : outage[e, t]
    for (e, config) in enumerate(input.electrolyzers)
        sfx = e == 1 ? "" : "_e$(e)"  # names unchanged for the first one
        for b in 1:size(vars.electrolyzer_power, 2)
            rated = b <= length(config.rated_power) ? config.rated_power[b] : 0.0
            for t in 1:hours
                @constraint(model, vars.electrolyzer_power[e, b, t] <= rated * avail(e, t),
                    base_name = "electrolyzer_max$(sfx)_b$(b)_t$(t)")
            end
            rated > 0 || continue
            ramp_up = b <= length(config.ramp_up) ? config.ramp_up[b] : 0.0
            ramp_down = b <= length(config.ramp_down) ? config.ramp_down[b] : 0.0
            up_limit = rated * min(1.0, ramp_up * tres)
            down_limit = rated * min(1.0, ramp_down * tres)
            for t in 2:hours
                ramp_up > 0 && @constraint(model,
                    vars.electrolyzer_power[e, b, t] - vars.electrolyzer_power[e, b, t-1] <= up_limit,
                    base_name = "electrolyzer_ramp_up$(sfx)_b$(b)_t$(t)")
                ramp_down > 0 && @constraint(model,
                    vars.electrolyzer_power[e, b, t-1] - vars.electrolyzer_power[e, b, t] <= down_limit,
                    base_name = "electrolyzer_ramp_down$(sfx)_b$(b)_t$(t)")
            end
        end
    end
end

"""
    electrolyzer_load(vars, b, t) -> AffExpr

Total electrolyzer power drawn at bus `b` in step `t` (all electrolyzers).
"""
function electrolyzer_load(vars, b::Int, t::Int)
    load = AffExpr(0.0)
    for e in axes(vars.electrolyzer_power, 1)
        add_to_expression!(load, vars.electrolyzer_power[e, b, t])
    end
    return load
end

"""
    add_ev_constraints!(model, vars::PowerSystemVariables, input::PowerSystemInput)

Add EV fleet constraints: SOC dynamics, charging/V2G limits, mutual exclusivity.

- SOC dynamics: SOC[t+1] = SOC[t] + charging*η_c - V2G/η_d - (driving - loss)
- Unserved driving energy: 0 <= loss <= driving, penalised in the objective
- Charging/V2G power limits
- Charge/V2G mutual exclusivity via Big-M relaxation

Driving energy leaves the fleet battery, so the grid must supply it (plus
losses) and the same energy cannot be sold back through V2G.
"""
function add_ev_constraints!(model, vars::PowerSystemVariables, input::PowerSystemInput)
    if input.ev_config === nothing || vars.ev_charging === nothing
        return
    end

    ev = input.ev_config
    n_node = input.network.num_nodes
    hours = input.temporal.hours
    tres = input.temporal.resolution_hours > 0 ? input.temporal.resolution_hours : 1

    for ni in 1:n_node
        # Skip nodes with no EV fleet
        if ni > length(ev.num_vehicles) || ev.num_vehicles[ni] <= 0
            for t in 1:hours
                @constraint(model, vars.ev_charging[ni, t] <= 0, base_name = "ev_zero_charge_n$(ni)_$(t)")
                @constraint(model, vars.ev_v2g[ni, t] <= 0, base_name = "ev_zero_v2g_n$(ni)_$(t)")
                @constraint(model, vars.ev_soc[ni, t+1] <= 0, base_name = "ev_zero_soc_n$(ni)_$(t)")
                @constraint(model, vars.ev_loss[ni, t] <= 0, base_name = "ev_zero_loss_n$(ni)_$(t)")
            end
            @constraint(model, vars.ev_soc[ni, 1] == 0, base_name = "ev_soc_initial_n$(ni)")
            continue
        end

        # Initial SOC (node-level, no bus_df scaling needed)
        if length(ev.initial_soc) >= ni
            @constraint(model, vars.ev_soc[ni, 1] == ev.initial_soc[ni],
                base_name = "ev_soc_initial_n$(ni)")
        else
            default_soc = ev.target_soc * ev.battery_capacity_kwh * ev.num_vehicles[ni] / 1000.0
            @constraint(model, vars.ev_soc[ni, 1] == default_soc,
                base_name = "ev_soc_initial_n$(ni)")
        end

        for t in 1:hours
            # SOC dynamics (MWh): driving draws on the battery; the part the
            # fleet cannot supply is unserved driving (ev_loss). Powers are MW
            # over a step of tres hours.
            ev_demand = ev.driving_consumption_profile[t, ni]
            @constraint(model,
                vars.ev_soc[ni, t+1] == vars.ev_soc[ni, t]
                    + tres * (vars.ev_charging[ni, t] * ev.charge_efficiency
                              - vars.ev_v2g[ni, t] / ev.discharge_efficiency
                              - (ev_demand - vars.ev_loss[ni, t])),
                base_name = "ev_soc_dynamics_n$(ni)_$(t)")
            @constraint(model, vars.ev_loss[ni, t] <= ev_demand,
                base_name = "ev_loss_max_n$(ni)_$(t)")

            # Charging power limit
            max_charge = ev.max_charge_power_kw * ev.num_vehicles[ni] / 1000.0
            max_charge = max(max_charge, ev_demand * 2.0)
            @constraint(model, vars.ev_charging[ni, t] <= max_charge,
                base_name = "ev_max_charge_n$(ni)_$(t)")

            # V2G discharge limit
            avail = size(ev.availability_profile, 1) >= t && size(ev.availability_profile, 2) >= ni ?
                ev.availability_profile[t, ni] : 1.0
            v2g_cap = ev.max_discharge_power_kw * ev.num_vehicles[ni] / 1000.0 * avail
            @constraint(model, vars.ev_v2g[ni, t] <= v2g_cap,
                base_name = "ev_max_v2g_n$(ni)_$(t)")

            # SOC bounds
            total_capacity_mwh = ev.battery_capacity_kwh * ev.num_vehicles[ni] / 1000.0
            @constraint(model, vars.ev_soc[ni, t+1] >= ev.min_soc * total_capacity_mwh,
                base_name = "ev_soc_min_n$(ni)_$(t)")
            @constraint(model, vars.ev_soc[ni, t+1] <= ev.max_soc * total_capacity_mwh,
                base_name = "ev_soc_max_n$(ni)_$(t)")

            # Charge/V2G mutual exclusivity (Big-M relaxation)
            if vars.ev_charge_status !== nothing
                M_ev = max_charge + v2g_cap
                @constraint(model, vars.ev_charging[ni, t] <= M_ev * vars.ev_charge_status[ni, t],
                    base_name = "ev_mutex_charge_n$(ni)_$(t)")
                @constraint(model, vars.ev_v2g[ni, t] <= M_ev * (1 - vars.ev_charge_status[ni, t]),
                    base_name = "ev_mutex_v2g_n$(ni)_$(t)")
            end
        end

        # Cyclic SOC constraint: EV fleet must return to initial SOC at end of window
        @constraint(model, vars.ev_soc[ni, hours+1] == vars.ev_soc[ni, 1],
            base_name = "ev_soc_cyclic_n$(ni)")
    end
end

"""
    _copy_with_optimizer(model, input, what) -> Union{Tuple{Model, ReferenceMap}, Nothing}

`copy_model` of a built model with a fresh optimizer configured like the
original (log off). `nothing` (with a warning naming `what`) when the copy or
the optimizer fails.

`copy_model` warns (harmlessly) that it cannot copy the entries stashed in
`model.ext` (cost_expressions, temporal_resolution_hours); they are only read
on the original, so `ext` is emptied across the copy and restored.
"""
function _copy_with_optimizer(model, input::PowerSystemInput, what::String)
    saved_ext = copy(model.ext)
    empty!(model.ext)
    copied = try
        copy_model(model)
    catch e
        @warn "$(what): copy_model failed" exception=e
        nothing
    finally
        merge!(model.ext, saved_ext)
    end
    copied === nothing && return nothing
    new_model, reference_map = copied
    optimizer = try
        create_optimizer(
            solver_name=input.solver_name,
            threads=input.threads,
            time_limit=input.time_limit,
            gap=input.gap,
            verbose=false,
            solver_options=input.solver_options,
        )
    catch e
        @warn "$(what): create_optimizer failed" exception=e
        return nothing
    end
    set_optimizer(new_model, optimizer)
    set_optimize_hook(new_model, optimize_synced!)
    return new_model, reference_map
end

"""
    compute_n1_security_cost!(model, input::PowerSystemInput) -> Float64

Cost of N-1 security in a solved window: the objective with the N-1
constraints minus the objective of the same model solved without them
(`n1_security.compute_security_cost`). The model is copied (with everything
added to it after `create_power_system`, such as the primary-energy block and
user constraints) and the copy is re-solved with the N-1 shortfall and
overload slacks free of charge, which makes every N-1 constraint inactive
(all of them are soft). The original model and its solution are untouched.

Stores the value in `model.ext[:n1_security_cost]` and returns it, in
solver cost units; it also keeps the re-solve's solution
(`model.ext[:n1_security_resolve]`) so that `extract_solution` of a
rolling-horizon window values both solves over the committed steps only
([`n1_security_cost`](@ref)). Returns 0.0 when N-1 is off, the model has no
solution, or the re-solve fails (with a warning). Both solves stop at the
solver's gap, so in a MIP the difference is exact only up to that gap.
"""
function compute_n1_security_cost!(model, input::PowerSystemInput)
    model.ext[:n1_security_cost] = 0.0
    delete!(model.ext, :n1_security_resolve)
    input.n1_security_enabled || return 0.0
    has_values(model) || return 0.0
    ce = get(model.ext, :cost_expressions, Dict{Symbol, Any}())
    n1_cost = get(ce, :n1_security_shortfall_cost, nothing)
    (n1_cost === nothing || isempty(n1_cost.terms)) && return 0.0

    copied = _copy_with_optimizer(model, input, "N-1 security cost")
    copied === nothing && return 0.0
    new_model, reference_map = copied
    tres = input.temporal.resolution_hours > 0 ? input.temporal.resolution_hours : 1
    free = AffExpr(0.0)        # the N-1 penalty terms, in the original's variables
    for (v, c) in n1_cost.terms
        add_to_expression!(free, tres * c, v)
    end
    free_copy = AffExpr(0.0)
    for (v, c) in free.terms
        add_to_expression!(free_copy, c, reference_map[v])
    end
    @objective(new_model, Min, objective_function(new_model) - free_copy)
    try
        optimize!(new_model)
    catch e
        @warn "N-1 security cost: re-solve without N-1 threw" exception=e
        return 0.0
    end
    if !has_values(new_model)
        @warn "N-1 security cost: re-solve without N-1 found no solution " *
              "($(termination_status(new_model))); n1_security_cost left at 0"
        return 0.0
    end
    cost = objective_value(model) - objective_value(new_model)
    model.ext[:n1_security_cost] = cost
    model.ext[:n1_security_resolve] = (
        values = Dict{VariableRef, Float64}(v => value(reference_map[v])
                                            for v in all_variables(model)),
        free = free)
    return cost
end

"""
    n1_security_cost(model, committed = nothing) -> Float64

The window's cost of N-1 security computed by
[`compute_n1_security_cost!`](@ref) (0.0 when it did not run). With
`committed` (a [`CommittedSteps`](@ref)), the objectives with and without
N-1 are both valued over the committed steps, as `extract_solution` values
the window's objective, instead of the whole window.
"""
function n1_security_cost(model, committed = nothing)
    cost = Float64(get(model.ext, :n1_security_cost, 0.0))
    resolve = get(model.ext, :n1_security_resolve, nothing)
    (committed === nothing || resolve === nothing) && return cost
    obj = objective_function(model)
    without = committed_value(committed, obj - resolve.free;
                              val = v -> resolve.values[v])
    return committed_value(committed, obj) - without
end

"""
    recover_uc_duals_via_copy(model, vars::PowerSystemVariables, input::PowerSystemInput) -> Union{Matrix{Float64}, Nothing}

Recover locational marginal prices from a UC (MIP) solution without
mutating the original model.

Modifying the solved model itself (unsetting the binaries) discards its
solution, so the rounded commitment could not be read back and
``extract_solution`` failed afterwards (``OptimizeNotCalled``). This
takes a fresh ``copy_model`` of the MIP, attaches a clean
optimizer, fixes ``gen_status`` to the rounded MIP values on the copy,
solves it as an LP, and reads the balance-constraint duals there.

Returns the recovered price matrix ``[n_bus × hours]`` in USD/MWh, or
``nothing`` if recovery isn't applicable (model isn't UC, has no
solution, or the LP re-solve failed). The caller is responsible for
overriding ``result.prices`` with the returned matrix.

Cost: ~1× MIP memory during the LP solve (the copy lives only inside
this call), plus the time of the LP itself — typically a fraction of
the MIP solve since the integers are fixed.
"""
function recover_uc_duals_via_copy(
    model, vars::PowerSystemVariables, input::PowerSystemInput,
)
    if vars.gen_status === nothing
        # Not UC — the original LP path already populated prices.
        return nothing
    end
    if !has_values(model)
        return nothing
    end

    n_gen = length(input.generators)
    n_bus = input.network.num_buses
    hours = input.temporal.hours

    # Snapshot the optimal commitment values from the MIP BEFORE the
    # copy so we read from the still-solved original. Rounding scrubs
    # solver floating-point noise (``0.0 ± 1e-9``) that would otherwise
    # make ``fix`` reject the value as infeasible.
    status_vals = Array{Float64}(undef, n_gen, n_bus, hours)
    fill!(status_vals, 0.0)
    for g in 1:n_gen
        for b in committed_buses(input.generators[g], vars.buses_of_gen[g]), t in 1:hours
            status_vals[g, b, t] = round(value(vars.gen_status[g, b, t]))
        end
    end

    # Clone the model + attach a fresh optimizer built with the same
    # config the MIP used. The ``ReferenceMap`` translates the original
    # variable / constraint handles into their copies — both ``gen_status``
    # (for fix) and ``balance_constraints`` (for dual lookup) live on the
    # original.
    conv_on = get(model.ext, :converter_commitment, VariableRef[])
    conv_vals = [round(value(v)) for v in conv_on]

    copied = _copy_with_optimizer(model, input, "UC dual recovery (copy)")
    copied === nothing && return nothing
    new_model, reference_map = copied
    for (v, val) in zip(conv_on, conv_vals)
        new_v = reference_map[v]
        is_binary(new_v) && unset_binary(new_v)
        fix(new_v, val; force=true)
    end

    # On the copy: relax ``gen_status`` to continuous and pin to MIP
    # incumbent values. The original model is untouched.
    for g in 1:n_gen
        for b in committed_buses(input.generators[g], vars.buses_of_gen[g]), t in 1:hours
            new_v = reference_map[vars.gen_status[g, b, t]]
            if is_binary(new_v)
                unset_binary(new_v)
            end
            # clustered units (integer units online)
            is_integer(new_v) && unset_integer(new_v)
            fix(new_v, status_vals[g, b, t]; force=true)
        end
    end

    try
        optimize!(new_model)
    catch e
        @warn "UC dual recovery (copy): LP re-solve threw" exception=e
        return nothing
    end

    if !has_duals(new_model)
        @warn "UC dual recovery (copy): LP duals not available after re-solve"
        return nothing
    end

    # Extract balance-constraint duals → $/MWh. The objective is
    # scaled by ``temporal_resolution_hours``, so raw duals need to be
    # divided by ``tres`` to land in real prices (same convention as
    # the original LP path in ``extract_solution``).
    prices = zeros(n_bus, hours)
    tres_norm = max(1.0, Float64(input.temporal.resolution_hours))
    if vars.balance_constraints !== nothing
        for ((b, t), old_con) in vars.balance_constraints
            try
                new_con = reference_map[old_con]
                prices[b, t] = dual(new_con) / tres_norm
            catch
                prices[b, t] = 0.0
            end
        end
    end

    @info "UC dual recovery (copy): recovered LMPs for $(n_bus)×$(hours) cells"
    return prices
end

"""
    extract_solution(model, vars::PowerSystemVariables, input::PowerSystemInput) -> PowerSystemResult

Extract solution values from solved model.

Returns a PowerSystemResult containing all solution values, metrics, and optional
dual prices (if the model is LP or has been fixed and resolved as LP).

Time-series arrays are per-step rates: MW for power, tCO2/h for
`co2_emissions`. The scalar totals (`total_generation`, `total_demand`,
`total_curtailment`, `load_shed_total` in MWh; `total_co2` in tCO2) are
energy/mass over the window: the rates summed over steps × `resolution_hours`.

With `committed` (a [`CommittedSteps`](@ref) of a rolling-horizon window),
the objective, the scalar totals and the cost breakdown cover only the
window's first `committed.keep` steps; the arrays still span the window.
"""
function extract_solution(model, vars::PowerSystemVariables, input::PowerSystemInput;
                          committed = nothing)
    status = termination_status(model)
    obj = !has_values(model) ? NaN :
        committed === nothing ? objective_value(model) :
        committed_value(committed, objective_function(model))
    solve_t = solve_time(model)

    n_gen = length(input.generators)
    n_bat = length(input.batteries)
    n_bus = input.network.num_buses
    n_node = input.network.num_nodes
    b2n = input.network.bus_to_node
    hours = input.temporal.hours

    # Extract values (with default for infeasible)
    # SparseAxisArray variables must be manually converted to dense arrays
    if has_values(model)
        # Generator output — sparse to dense
        gen_out = zeros(n_gen, n_bus, hours)
        for g in 1:n_gen, b in vars.buses_of_gen[g], t in 1:hours
            gen_out[g, b, t] = value(vars.gen_output[g, b, t])
        end

        # Generator status and startup — sparse to dense. Only committed
        # units have them (0 for a variable renewable: no on/off decision).
        gen_stat = nothing
        if vars.gen_status !== nothing
            gen_stat = zeros(n_gen, n_bus, hours)
            for g in 1:n_gen, b in committed_buses(input.generators[g], vars.buses_of_gen[g]),
                    t in 1:hours
                gen_stat[g, b, t] = value(vars.gen_status[g, b, t])
            end
        end

        gen_start = nothing
        if vars.gen_startup !== nothing
            gen_start = zeros(n_gen, n_bus, hours)
            for g in 1:n_gen, b in committed_buses(input.generators[g], vars.buses_of_gen[g]),
                    t in 1:hours
                gen_start[g, b, t] = value(vars.gen_startup[g, b, t])
            end
        end

        # Generator shutdown — sparse to dense (currently always nothing)
        gen_shut = nothing
        if vars.gen_shutdown !== nothing
            gen_shut = zeros(n_gen, n_bus, hours)
            for g in 1:n_gen, b in vars.buses_of_gen[g], t in 1:hours
                gen_shut[g, b, t] = value(vars.gen_shutdown[g, b, t])
            end
        end

        # Expand node-level results to bus-level for backward compatibility.
        # DISTRIBUTE (not copy) the node value uniformly across the node's
        # buses, so that summing buses back to the node — done both by
        # `total_curt = sum(curt)` below and by the Python
        # `_aggregate_buses_to_nodes(..., "sum")` — recovers the original node
        # value. A plain copy inflates the total by the number of buses per
        # node (e.g. ~40× for Cuba's 398-bus / 10-node network).
        buses_per_node = zeros(Int, n_node)
        for b in 1:n_bus
            buses_per_node[b2n[b]] += 1
        end
        curt_node = value.(vars.curtailment)
        curt = zeros(n_bus, hours)
        for b in 1:n_bus
            curt[b, :] = curt_node[b2n[b], :] ./ max(buses_per_node[b2n[b]], 1)
        end

        # Battery variables — sparse to dense
        if n_bat > 0
            b_charge = zeros(n_bat, n_bus, hours)
            b_discharge = zeros(n_bat, n_bus, hours)
            b_soc = zeros(n_bat, n_bus, hours + 1)
            for bi in 1:n_bat, b in vars.buses_of_bat[bi]
                for t in 1:hours
                    b_charge[bi, b, t] = value(vars.bat_charge[bi, b, t])
                    b_discharge[bi, b, t] = value(vars.bat_discharge[bi, b, t])
                end
                for t in 1:(hours+1)
                    b_soc[bi, b, t] = value(vars.bat_soc[bi, b, t])
                end
            end
        else
            b_charge = zeros(0, n_bus, hours)
            b_discharge = zeros(0, n_bus, hours)
            b_soc = zeros(0, n_bus, hours)
        end

        # load_shed is per-bus natively (B2 refactor); extract directly.
        ls = value.(vars.load_shed)
        # Expand co2, reserves to bus-level (still node-level vars)

        # co2: same distribute-don't-copy treatment as curtailment above —
        # node-level emissions split uniformly across the node's buses so the
        # downstream bus→node sum recovers the true node total (a plain copy
        # would inflate it by buses-per-node).
        co2_node = vars.co2_emissions !== nothing ? value.(vars.co2_emissions) : zeros(n_node, hours)
        co2_em = zeros(n_bus, hours)
        for b in 1:n_bus
            co2_em[b, :] = co2_node[b2n[b], :] ./ max(buses_per_node[b2n[b]], 1)
        end

        v_angle = value.(vars.voltage_angle)

        res_static_node = value.(vars.reserve_static)
        res_dynamic_node = value.(vars.reserve_dynamic)
        res_static_loss_node = value.(vars.reserve_static_loss)
        res_dynamic_loss_node = value.(vars.reserve_dynamic_loss)
        res_static = zeros(n_bus, hours)
        res_dynamic = zeros(n_bus, hours)
        res_static_loss = zeros(n_bus, hours)
        res_dynamic_loss = zeros(n_bus, hours)
        for b in 1:n_bus
            df = input.network.buses[b].demand_fraction
            res_static[b, :] = res_static_node[b2n[b], :] .* df
            res_dynamic[b, :] = res_dynamic_node[b2n[b], :] .* df
            res_static_loss[b, :] = res_static_loss_node[b2n[b], :] .* df
            res_dynamic_loss[b, :] = res_dynamic_loss_node[b2n[b], :] .* df
        end

        # Try to extract dual prices from balance constraints
        # The objective is `temporal_resolution_hours × energy_costs`, so raw duals
        # are scaled by tres.  Divide by tres to get actual $/MWh prices.
        prices = zeros(n_bus, hours)
        tres_norm = max(1.0, Float64(input.temporal.resolution_hours))
        if vars.balance_constraints !== nothing && has_duals(model)
            for ((b, t), con) in vars.balance_constraints
                try
                    prices[b, t] = dual(con) / tres_norm
                catch
                    # Dual not available (MIP or solver limitation)
                    prices[b, t] = 0.0
                end
            end
        end
    else
        gen_out = zeros(n_gen, n_bus, hours)
        gen_stat = nothing
        gen_start = nothing
        gen_shut = nothing
        curt = zeros(n_bus, hours)
        b_charge = zeros(max(1, n_bat), n_bus, hours)
        b_discharge = zeros(max(1, n_bat), n_bus, hours)
        b_soc = zeros(max(1, n_bat), n_bus, hours)
        ls = zeros(n_bus, hours)
        co2_em = zeros(n_bus, hours)
        v_angle = zeros(n_bus, hours)
        res_static = zeros(n_bus, hours)
        res_dynamic = zeros(n_bus, hours)
        res_static_loss = zeros(n_bus, hours)
        res_dynamic_loss = zeros(n_bus, hours)
        prices = zeros(n_bus, hours)
    end

    # Calculate metrics. The arrays are per-step rates (MW, tCO2/h); each
    # step lasts `tres` hours, so energy and mass totals are sum × tres
    # (MWh, tCO2), whatever the temporal resolution.
    tres_tot = Float64(input.temporal.resolution_hours > 0 ? input.temporal.resolution_hours : 1)
    kept = committed === nothing ? (1:hours) : (1:committed.keep)
    total_gen = sum(@view gen_out[:, :, kept]) * tres_tot
    total_demand = sum(@view input.demand[kept, :]) * tres_tot
    total_curt = sum(@view curt[:, kept]) * tres_tot
    total_ls = sum(@view ls[:, kept]) * tres_tot
    # Transmission losses of the DC loss model (0 when lossless), per bus:
    # half of each line's loss at each end, as the bus balances take it
    total_losses = 0.0
    bus_losses = nothing
    if vars.line_losses !== nothing && has_values(model)
        bus_losses = zeros(n_bus, hours)
        for ((i, j), lines) in vars.pair_lines, ℓ in lines
            lv = value.(vars.line_losses[ℓ])
            @views bus_losses[i, :] .+= 0.5 .* lv
            @views bus_losses[j, :] .+= 0.5 .* lv
        end
        total_losses = sum(@view bus_losses[:, kept]) * tres_tot
    end

    # RE share: renewable over total generation (is_renewable_unit)
    re_gen = 0.0
    for g in 1:n_gen
        if is_renewable_unit(input.generators[g])
            re_gen += sum(@view gen_out[g, :, kept]) * tres_tot
        end
    end
    re_pen = total_gen > 0 ? re_gen / total_gen : 0.0

    # CO2 emissions (using fuel_co2 factors if available)
    total_co2 = 0.0
    for g in 1:n_gen, b in 1:size(gen_out, 2)
        co2_factor = co2_per_mwh(input.fuel_co2, input.generators[g], b)
        co2_factor == 0.0 && continue
        total_co2 += sum(@view gen_out[g, b, kept]) * co2_factor * tres_tot
    end

    # Per-line power flow values
    pf_by_line = nothing
    if has_values(model) && vars.power_flow_by_line !== nothing
        pf_by_line = [value.(pf_var) for pf_var in vars.power_flow_by_line]
    end

    # Bus-pair flows: sum of every branch between the pair (parallel lines)
    pf = Dict{Tuple{Int,Int}, Vector{Float64}}()
    if pf_by_line !== nothing
        for (key, lines) in vars.pair_lines
            pf[key] = sum(pf_by_line[ℓ] for ℓ in lines)
        end
    end

    # Battery spillage
    spillage_out = nothing
    if has_values(model) && vars.bat_spillage !== nothing
        spillage_out = zeros(n_bat, n_bus, hours)
        for bi in 1:n_bat, b in 1:n_bus, t in 1:hours
            if vars.bat_spillage[bi, b, t] !== nothing
                spillage_out[bi, b, t] = value(vars.bat_spillage[bi, b, t])
            end
        end
    end

    # EV variables
    ev_charge_out = nothing
    ev_v2g_out = nothing
    ev_soc_out = nothing
    ev_loss_out = nothing
    if has_values(model) && vars.ev_charging !== nothing
        ev_charge_out = value.(vars.ev_charging)
        ev_v2g_out = value.(vars.ev_v2g)
        # ev_soc has hours+1 columns, take first `hours` for consistency
        ev_soc_full = value.(vars.ev_soc)
        ev_soc_out = ev_soc_full[:, 1:hours]
        ev_loss_out = value.(vars.ev_loss)
    end

    # Loss of inertia
    loi_out = nothing
    if has_values(model) && vars.loss_of_inertia !== nothing
        loi_out = value.(vars.loss_of_inertia)
    end

    # Transfer margin
    tm_out = nothing
    if has_values(model) && vars.transfer_margin !== nothing && !isempty(vars.transfer_margin)
        tm_out = Dict{Tuple{Int,Int}, Vector{Float64}}()
        for (key, var_vec) in vars.transfer_margin
            tm_out[key] = value.(var_vec)
        end
    end

    # Reservoir hydroelectric results — sparse to dense
    res_level_out = nothing
    res_spillage_out = nothing
    res_pump_out = nothing
    if has_values(model) && vars.reservoir_level !== nothing
        res_level_out = zeros(n_gen, n_bus, hours + 1)
        res_spillage_out = zeros(n_gen, n_bus, hours)
        res_pump_out = zeros(n_gen, n_bus, hours)
        for g in 1:n_gen, b in vars.buses_of_gen[g]
            for t in 1:hours
                res_spillage_out[g, b, t] = value(vars.reservoir_spillage[g, b, t])
                res_pump_out[g, b, t] = value(vars.reservoir_pump[g, b, t])
            end
            for t in 1:(hours+1)
                res_level_out[g, b, t] = value(vars.reservoir_level[g, b, t])
            end
        end
    end

    # ── N-1 security duals extraction ──
    n1_gen_duals = nothing
    n1_trans_duals = nothing
    n1_binding = nothing
    n1_cost = 0.0

    if has_values(model) && has_duals(model) && input.n1_security_enabled
        binding_names = String[]

        # Generation N-1 duals: preventive, SCOPF or corrective constraint
        if input.n1_generation_enabled
            gen_duals = zeros(hours)
            prefix = !input.n1_scopf_enabled ? "n1_gen_reserve_system" :
                     input.n1_corrective_enabled ? "n1_corrective_gen" : "scopf_n1_gen_reserve"
            # One constraint per system (_add_gen_n1!): the step's dual is
            # their sum
            n_sys = length(reserve_node_groups(input))
            for t in 1:hours
                names = n_sys == 1 ? ["$(prefix)_t$(t)"] :
                    ["$(prefix)_s$(k)_t$(t)" for k in 1:n_sys]
                for cname in names
                    try
                        con = constraint_by_name(model, cname)
                        if con !== nothing
                            d = dual(con)
                            gen_duals[t] += d
                            if abs(d) > 1e-6 && !(cname in binding_names)
                                push!(binding_names, cname)
                            end
                        end
                    catch
                    end
                end
            end
            if any(abs.(gen_duals) .> 1e-6)
                n1_gen_duals = gen_duals
            end
        end

        # Transmission SCOPF duals (scopf_line*_outage*_t*_pos/neg)
        if input.n1_transmission_enabled && input.n1_scopf_enabled
            trans_duals = Dict{Tuple{Int,Int,Int}, Vector{Float64}}()
            n_lines = length(input.network.transmission_lines)
            for k in 1:n_lines, l in 1:n_lines
                if l == k
                    continue
                end
                duals_pos = zeros(hours)
                duals_neg = zeros(hours)
                has_any = false
                for t in 1:hours
                    for (suffix, dvec) in [("pos", duals_pos), ("neg", duals_neg)]
                        cname = "scopf_line$(l)_outage$(k)_t$(t)_$(suffix)"
                        try
                            con = constraint_by_name(model, cname)
                            if con !== nothing
                                d = dual(con)
                                dvec[t] = d
                                if abs(d) > 1e-6
                                    has_any = true
                                    key_name = "scopf_line$(l)_outage$(k)"
                                    if !(key_name in binding_names)
                                        push!(binding_names, key_name)
                                    end
                                end
                            end
                        catch
                        end
                    end
                end
                if has_any
                    trans_duals[(l, k, 1)] = duals_pos   # dir=1 for positive
                    trans_duals[(l, k, -1)] = duals_neg  # dir=-1 for negative
                end
            end
            if !isempty(trans_duals)
                n1_trans_duals = trans_duals
            end
        end

        n1_binding = isempty(binding_names) ? nothing : binding_names
    end
    # Objective with N-1 minus without, over the committed steps
    # (compute_n1_security_cost!; 0 when not run)
    n1_cost = n1_security_cost(model, committed)

    # ── Extract granular cost breakdown ──
    cost_bd = nothing
    if has_values(model) && haskey(model.ext, :cost_expressions)
        ce = model.ext[:cost_expressions]
        tres = get(model.ext, :temporal_resolution_hours, 1.0)
        _v(sym, is_energy=true) = try
            ex = get(ce, sym, AffExpr(0.0))
            raw = committed === nothing ? value(ex) : committed_value(committed, ex)
            is_energy ? raw * tres : raw
        catch
            0.0
        end
        cost_bd = CostBreakdown(
            _v(:fuel_cost),
            _v(:fixed_om_cost, false),
            _v(:maintenance_cost),
            _v(:startup_cost, false),
            _v(:battery_maintenance_cost),
            _v(:battery_degradation_cost),
            _v(:load_shedding_cost),
            _v(:curtailment_cost),
            _v(:reserve_static_cost),
            _v(:reserve_dynamic_cost),
            _v(:co2_emission_cost),
            _v(:fre_penetration_cost),
            _v(:inertia_cost),
            _v(:soc_violation_cost),
            _v(:transfer_margin_cost),
            _v(:v2g_compensation),
            _v(:electrolyzer_cost),
            _v(:converter_cost),
            _v(:spillage_cost),
            _v(:reservoir_spillage_cost),
            _v(:demand_shift_cost),
            _v(:rooftop_curtailment_cost),
            # PrimaryEnergy sub-costs. NOT scaled by tres — the PE objective
            # is built per-period/per-hour and already in solver units for the window.
            _v(:pe_supply_cost, false),
            _v(:pe_loss_cost, false),
            _v(:pe_excess_cost, false),
            _v(:pe_transport_cost, false),
            _v(:pe_investment_cost, false),
            _v(:pe_electrolyzer_cost, false),
            # N-1 reliability-shortfall: same $/MWh scale as load shed (× tres).
            _v(:n1_security_shortfall_cost),
            _v(:line_flow_cost),
            _v(:operating_budget_excess, false),
            _v(:operating_budget_penalty, false),
            obj,
        )
    end

    # ACOPF AC-side outputs: filled only when an acopf_* formulation ran;
    # nothing otherwise so DC runs stay zero-cost.
    vm_out = nothing
    qgen_out = nothing
    if vars.acopf_vars !== nothing
        n_bus = input.network.num_buses
        vm_out, _ = extract_acopf_voltages(vars.acopf_vars, n_bus, hours)
        qgen_out = extract_acopf_reactive_gen(vars.acopf_vars, n_gen, n_bus, hours)
    end

    # Electrolyzers: power drawn and hydrogen produced (electrolyzer × bus × hour)
    elz_power_out = nothing
    elz_h2_out = nothing
    if has_values(model) && vars.electrolyzer_power !== nothing
        elz_power_out = value.(vars.electrolyzer_power)
        elz_h2_out = zeros(size(elz_power_out))
        for (e, ec) in enumerate(input.electrolyzers), b in 1:size(elz_power_out, 2)
            b <= length(ec.eff_at_rated) || continue
            elz_h2_out[e, b, :] .= elz_power_out[e, b, :] .* electrolyzer_h2_per_mwh(ec, b)
        end
    end

    return PowerSystemResult(
        status, obj, solve_t,
        gen_out, gen_stat, gen_start, gen_shut,
        curt, total_curt,
        b_charge, b_discharge, b_soc,
        pf, pf_by_line, v_angle, vm_out, qgen_out,
        res_static, res_dynamic, res_static_loss, res_dynamic_loss,
        ls, co2_em,
        prices,
        total_gen, total_demand, total_losses,
        re_pen, total_co2, total_ls,
        spillage_out,
        ev_charge_out, ev_v2g_out, ev_soc_out, ev_loss_out,
        loi_out,
        tm_out,
        res_level_out, res_spillage_out, res_pump_out,
        n1_gen_duals, n1_trans_duals, n1_binding, n1_cost,
        cost_bd,
        elz_power_out, elz_h2_out,
        bus_losses,
    )
end
