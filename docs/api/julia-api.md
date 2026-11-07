# Julia API

Module: `ESFEX.jl`

## Module Structure

```julia
module ESFEX
    using JuMP, HiGHS, Graphs, LinearAlgebra, SparseArrays, Statistics
    import MathOptInterface as MOI

    include("types.jl")              # Core type definitions
    include("user_constraints.jl")   # User constraint registry and hooks
    include("transmission_dc.jl")    # DC power flow (angle formulation)
    include("transmission_ac.jl")    # AC power flow (Newton-Raphson verification)
    include("transmission_acopf.jl") # ACOPF relaxations and NLP formulations
    include("power_system.jl")       # Operational dispatch / unit commitment
    include("primary_energy.jl")     # Fuel supply chain optimization
    include("master_problem.jl")     # Capacity expansion (deterministic, multi-system, stochastic)
    include("benders.jl")            # Benders decomposition of the master problem
    include("mga.jl")                # MGA/SPORES near-optimal alternatives
    include("electrolyzer.jl")       # Hydrogen electrolyzer model
    include("dcopf_benchmark.jl")    # Benchmark helpers
    include("acopf_benchmark.jl")
end
```

Optional solvers (SCIP, Xpress, Gurobi, CPLEX, CBC, GLPK, Ipopt, Clarabel, SCS) are loaded on demand by `create_optimizer`; HiGHS is always available.

The type blocks below list the main fields. `types.jl` is the reference for the full field lists.

---

## Key Types

### PowerSystemInput

Main input structure for operational dispatch. All vectors are indexed per-bus (or per-node if `num_buses == num_nodes`). Matrices are indexed as `[time, bus]`. Built with a keyword constructor `PowerSystemInput(; ...)` that supplies defaults for most fields.

```julia
struct PowerSystemInput
    name::String
    year::Int
    base_year::Int
    network::NetworkConfig
    generators::Vector{GeneratorConfig}
    batteries::Vector{BatteryConfig}
    demand::Matrix{Float64}                    # (hours x buses) in MW
    sectoral_demand::Dict{String, Matrix{Float64}}
    temporal::TemporalConfig
    # Penalty coefficients (monetary values in M$, see COST_SCALE)
    loss_of_load_penalty::Float64              # per MW of unserved energy
    loss_of_reserve_static::Float64            # per MW of static reserve shortfall
    loss_of_reserve_dynamic::Float64           # per MW of dynamic reserve shortfall
    co2_cost::Float64                          # per tonne CO2
    curtailment_cost::Float64                  # per MWh of spilled RE
    # Targets
    re_penetration_target::Float64             # RE penetration ratio (0-1)
    co2_budget::Float64                        # Annual CO2 budget (tonnes, Inf = unconstrained)
    inertia_limit::Float64                     # Minimum system inertia (GW*s)
    # Solver settings
    mode::String                               # "development", "economic_dispatch", "unit_commitment"
    solver_name::String                        # "highs", "gurobi", etc.
    threads::Int
    time_limit::Float64
    gap::Float64
    verbose::Bool
    solver_options::Dict{String, Any}
    # Optional subsystem configurations
    fuel_co2::Dict{String, Float64}
    ev_config::Union{EVConfig, Nothing}
    electrolyzers::Vector{ElectrolyzerConfig}   # keyword `electrolyzer_config` (one) still accepted
    # N-1 security
    n1_security_enabled::Bool
    n1_transmission_enabled::Bool
    n1_generation_enabled::Bool
    # Rolling-window boundary state (empty Dict = none)
    generator_initial_status::Dict{Int, Dict{Int, Float64}}
    generator_output_prev::Dict{Int, Dict{Int, Float64}}
    reservoir_level_prev::Dict{Int, Dict{Int, Float64}}
    battery_soc_prev::Dict{Int, Dict{Int, Float64}}   # MWh
    hours_per_year::Int
    power_flow_mode::String                    # "dcopf" (default) or "acopf_soc|qc|sdp|polar|rect"
    # Per-system CO2 budgets of a merged multi-system run; empty = one budget
    # over all nodes from co2_budget / co2_budget_violation_penalty
    co2_budget_groups::Vector{CO2BudgetGroup}  # CO2BudgetGroup(name, nodes, budget, violation_penalty)
    # ... additional fields (see types.jl for full list)
end
```

### GeneratorConfig

Per-generator configuration. All vectors are indexed per-bus.

```julia
struct GeneratorConfig
    name::String
    type::String                    # "Renewable" or "Non-renewable"
    fuel::String                    # "Sun", "Wind", "Diesel", "Natural Gas", etc.
    rated_power::Vector{Float64}    # MW per bus
    min_power::Vector{Float64}      # Fraction of rated (minimum stable output)
    efficiency_rated::Vector{Float64}
    efficiency_min::Vector{Float64}
    ramp_up::Vector{Float64}        # Fraction of rated per hour (<= 0 = no limit)
    ramp_down::Vector{Float64}
    min_up_time::Vector{Float64}    # Hours
    min_down_time::Vector{Float64}  # Hours
    start_up_cost::Vector{Float64}  # Per start
    fuel_cost::Vector{Float64}      # Per MWh
    fixed_cost::Vector{Float64}
    maintenance_cost::Vector{Float64}
    inertia::Vector{Float64}        # Inertia constant H (s)
    invest_cost::Vector{Float64}    # Per MW
    invest_max::Vector{Float64}     # Maximum investment MW per bus
    availability::Matrix{Float64}   # (hours x buses) capacity factor [0,1]
    reservable::Bool                # Can provide reserve services
    life_time::Vector{Float64}      # Years
    initial_age::Vector{Float64}    # Years
    degradation_rate::Vector{Float64}
    decommissioning_cost::Vector{Float64}
    frequency_hz::Float64           # Nominal frequency (Hz)
    current_type::String            # "AC", "DC" or "AC_DC"
    # Reservoir hydro (zeros = no reservoir)
    reservoir_capacity::Vector{Float64}       # MWh-eq
    reservoir_initial_level::Vector{Float64}  # Fraction 0-1
    reservoir_min_level::Vector{Float64}
    reservoir_max_level::Vector{Float64}
    reservoir_inflow::Matrix{Float64}         # (hours x buses) MW-eq
    # ... turbine/pump efficiency, evaporation, pump capacity, spillage,
    #     reservoir investment, min release, cascade, head dependence
    risk_coefficient::Vector{Float64}         # Geographic derating (0-1)
end
```

### BatteryConfig

```julia
struct BatteryConfig
    name::String
    capacity::Vector{Float64}           # MWh per bus
    max_charge_power::Vector{Float64}   # MW per bus
    max_discharge_power::Vector{Float64} # MW per bus
    charge_efficiency::Vector{Float64}  # 0-1 per bus
    discharge_efficiency::Vector{Float64}
    soc_min::Vector{Float64}            # Minimum SOC (fraction)
    soc_max::Vector{Float64}            # Maximum SOC (fraction)
    soc_initial::Vector{Float64}        # Initial SOC (fraction)
    self_discharge::Vector{Float64}     # Hourly self-discharge rate
    invest_cost_power::Vector{Float64}  # $/MW per bus
    invest_cost_capacity::Vector{Float64} # $/MWh per bus
    invest_max_power::Vector{Float64}   # Max investment MW per bus
    invest_max_capacity::Vector{Float64} # Max investment MWh per bus
    life_time::Vector{Float64}
    initial_age::Vector{Float64}
    decommissioning_cost::Vector{Float64}
    min_duration_hours::Float64         # Minimum E/P ratio
    max_duration_hours::Float64         # Maximum E/P ratio
    maintenance_cost::Vector{Float64}
    inertia::Vector{Float64}
    spillage::Bool                      # Allow energy spillage
    current_type::String                # "AC" or "DC"
    degradation_rate::Vector{Float64}
    throughput_degradation_cost::Vector{Float64}  # Per MWh discharged
    risk_coefficient::Vector{Float64}
end
```

### NetworkConfig

```julia
struct NetworkConfig
    num_nodes::Int                              # Geographic nodes
    num_buses::Int                              # Electrical buses (>= num_nodes)
    buses::Vector{BusData}
    bus_to_node::Vector{Int}                    # Bus -> node mapping (1-indexed)
    connections::Matrix{Float64}                # Adjacency/capacity matrix
    distances::Matrix{Float64}                  # Distance matrix (km)
    s_base::Float64                             # System power base (MVA), ac_power_flow.base_mva
    reactance_per_km::Float64
    voltage_level_kv::Float64
    max_angle_diff_rad::Float64
    slack_bus::Int
    transference_invest_cost::Vector{Float64}   # Transmission investment cost per bus
    transference_invest_max::Vector{Float64}
    transmission_lines::Vector{TransmissionLineData}
    transformers::Vector{TransformerData}
    acdc_converters::Vector{ACDCConverterData}
    freq_converters::Vector{FrequencyConverterData}
    default_r_to_x_ratio::Float64
end
```

### MasterProblemInput

Input structure for the capacity expansion model. Built with a keyword constructor `MasterProblemInput(; ...)`.

```julia
struct MasterProblemInput
    years::Vector{Int}
    base_year::Int
    system_name::String
    network::NetworkConfig
    generators::Vector{GeneratorConfig}
    batteries::Vector{BatteryConfig}
    technologies::Vector{TechnologyConfig}
    battery_technologies::Vector{BatteryTechnologyConfig}
    base_demand::Matrix{Float64}                # (hours x buses), one block per planning year
    demand_growth::Float64                      # Extra growth on top (default 0.0)
    discount_rate::Float64
    max_annual_investment::Float64
    # RE targets
    target_re_penetration::Float64
    initial_re_penetration::Float64
    min_re_increment::Float64
    max_re_increment::Float64
    system_node_ranges::Vector{SystemNodeRange} # Per-system RE targets
    # Representative days
    temporal_resolution_hours::Int
    representative_days_per_year::Int
    min_day_separation::Int
    investment_resolution_hours::Int
    # TSAM (Time Series Aggregation Method)
    use_tsam::Bool
    tsam_period_start_hours::Vector{Vector{Int}}
    tsam_period_weights::Vector{Vector{Float64}}
    tsam_chronological_order::Vector{Vector{Int}}
    tsam_inter_period_linking::Bool
    # Penalty coefficients
    slack_penalty::Float64
    loss_of_load_penalty::Float64
    max_curtailment_ratio::Float64
    curtailment_cost::Float64
    # Reserves, inertia, CO2
    reserve_static_requirement::Dict{Int, Float64}
    reserve_dynamic_requirement::Dict{Int, Float64}
    inertia_limit::Float64
    fuel_co2::Dict{String, Float64}
    co2_cost::Float64
    co2_budget_groups::Vector{CO2BudgetGroup}       # Annual CO2 budget per system (empty = none)
    line_flow_cost::Dict{Tuple{Int,Int}, Float64}  # Cost per MWh of |flow| on a bus pair (low, high)
    unit_commitment::Bool                          # Dispatch commits units: LP start-ups in the master
    # Transmission (per physical line)
    transmission_lines::Vector{Tuple{Int,Int}}
    transmission_reactances::Vector{Float64}
    transmission_capacities::Vector{Float64}
    # Solver
    solver_name::String
    solver_options::Dict{String, Any}
    benders_lol_penalty_cap::Float64
    # ... additional fields (NPV, sectoral demand, primary energy, cost curves)
end
```

### TechnologyConfig / BatteryTechnologyConfig

Investment candidate types (per-technology, not per-generator):

```julia
struct TechnologyConfig
    name::String
    type::String                        # "Renewable" or "Non-renewable"
    fuel::String
    invest_cost::Vector{Float64}        # Per MW per bus
    invest_max::Vector{Float64}         # Max cumulative MW per bus
    availability::Matrix{Float64}       # (hours x buses)
    fuel_cost::Vector{Float64}
    fixed_cost::Vector{Float64}
    maintenance_cost::Vector{Float64}
    life_time::Vector{Float64}
    # ... operational fields mirroring GeneratorConfig
end

struct BatteryTechnologyConfig
    name::String
    invest_cost_power::Vector{Float64}  # $/MW
    invest_cost_capacity::Vector{Float64} # $/MWh
    invest_max_power::Vector{Float64}
    invest_max_capacity::Vector{Float64}
    min_duration_hours::Float64
    max_duration_hours::Float64
    charge_efficiency::Vector{Float64}
    discharge_efficiency::Vector{Float64}
    life_time::Vector{Float64}
    # ... SOC limits, maintenance, degradation, inertia, spillage
end
```

### MGAResult

The result container for both `run_mga_spores` (classical MGA) and `run_spores` (SPORES sweep). A back-compat constructor defaults `objective_labels` to `["hsj_diversity", …]` when callers do not supply it, so pre-Phase-2 code paths continue to compile unchanged.

```julia
struct MGAResult
    alternatives::Vector{MasterProblemResult}
    num_alternatives::Int
    slack_fraction::Float64
    optimal_cost::Float64
    alternative_costs::Vector{Float64}
    diversity_objectives::Vector{Float64}  # one entry per non-optimal alt
    objective_labels::Vector{String}       # Phase 2: tags each non-optimal alt
                                           # with the SPORES objective that
                                           # produced it
end
```

`objective_labels[k]` matches the [`SporesObjective`](../api/config-schema.md#sporesobjective) value (`"hsj_diversity"`, `"min_total_build"`, `"max_tech_equity"`, `"max_regional_equity"`, `"evolutionary_dist"`) for the $k$-th non-optimal alternative. The cost-optimal seed is implicit at index 1 of `alternatives`.

---

## Key Functions

### Power System (Operational Dispatch)

| Function | Signature | Description |
|----------|-----------|-------------|
| `create_power_system` | `(input::PowerSystemInput; custom_constraints=nothing) -> (Model, PowerSystemVariables)` | Build complete operational dispatch JuMP model with all variables, constraints, and objective. `custom_constraints` are user constraint specs applied at the end of the build. With `power_flow_mode` starting with `acopf_` it calls `setup_acopf!` instead of the DC flow. |
| `build_variables!` | `(model, input) -> PowerSystemVariables` | Create all decision variables: `gen_output[g,b,t]` and `bat_charge/bat_discharge/bat_soc[bi,b,t]` (sparse, only for unit-bus pairs with capacity or investment potential), `curtailment[n,t]`, `load_shed[n,t]`, reserves, etc. |
| `build_objective!` | `(model, vars, input)` | Build cost minimization objective: fuel costs + fixed costs + start-up costs + penalty terms. |
| `add_generator_constraints!` | `(model, vars, input)` | Generator capacity limits, ramp rates, minimum up/down times. For units with `rated_power[b]=0` and no investment potential, constrains `gen_output[g,b,t] <= 0` to prevent free generation. |
| `add_battery_constraints!` | `(model, vars, input)` | SOC dynamics, charge/discharge limits, end-of-horizon SOC: two-sided cyclic band when `cyclic_end_soc` is true, otherwise the soft floor `bat_soc_end_min` / `bat_soc_end_min_cyclic` used on the year's last window. For zero-capacity batteries, constrains all variables to zero. |
| `add_demand_constraints!` | `(model, vars, input; extra_injections_fn=nothing)` | Copper-plate power balance per bus and timestep: `gen + discharge + V2G + load_shed == demand + charge + EV charging + electrolyzer` (plus rooftop terms when given). `load_shed` is capped by the bus demand. With more than one bus, `create_power_system` uses the KCL of `add_dc_constraints!` (or ACOPF) instead. |
| `add_reserve_constraints!` | `(model, vars, input)` | Static (spinning) and dynamic (frequency response) reserve per node, held as shared free capacity of reservable generators and batteries; committed units only in UC. |
| `add_inertia_constraints!` | `(model, vars, input)` | System inertia floor from synchronous generators. |
| `add_curtailment_constraints!` | `(model, vars, input)` | Defines `curtailment[n,t]` as available minus dispatched variable RE at each node (reservoir hydro excluded). |
| `add_renewable_constraint!` | `(model, vars, input)` | RE penetration target: `renewable_gen >= target * total_gen`. |
| `add_co2_constraint!` | `(model, vars, input)` | CO2 budget: `sum(fuel_gen * emission_factor / eff_at_rated) <= co2_budget` scaled to the window's share of the year, with a penalised violation slack. With `co2_budget_groups` (multi-system run), one such constraint per group (system), over its nodes, with its own budget and penalty. |
| `add_ev_constraints!` | `(model, vars, input)` | EV charging scheduling and V2G dispatch with SOC tracking. |
| `add_sectoral_demand_constraints!` | `(model, vars, input)` | Per-sector loss-of-load with criticality-weighted penalties and demand shifting. |
| `add_n1_security_constraints!` | `(model, vars, input)` | N-1 contingency constraints for transmission and generation. |
| `extract_solution` | `(model, vars, input) -> PowerSystemResult` | Extract all variable values into a structured result. |
| `recover_uc_duals!` | `(model, vars, input) -> Bool` | UC only: fix the commitment binaries to their rounded values on the model itself and re-solve as an LP so balance duals (prices) exist. Mutates the model. |
| `recover_uc_duals_via_copy` | `(model, vars, input) -> Union{Matrix, Nothing}` | Same on a copy of the model; returns the `[bus x hour]` price matrix in M$/MWh, or `nothing`. Used by `PowerSystemAdapter`. |

### Master Problem (Capacity Expansion)

| Function | Signature | Description |
|----------|-----------|-------------|
| `create_master_problem` | `(input::MasterProblemInput; use_representative_days=true, custom_constraints=nothing) -> (Model, MasterProblemVariables, targets)` | Build multi-year capacity expansion model. With `use_representative_days=false` only the investment structure is built (no operational validation). |
| `calculate_target_ratios` | `(input::MasterProblemInput) -> Dict{Tuple{Int,Int}, Float64}` | RE target per `(system_idx, year_idx)`: linear interpolation from the initial to the target penetration, clipped by the min/max annual increments. |
| `build_master_variables!` | `(model, input) -> MasterProblemVariables` | Investment variables, keyed by year index: `tech_investment[y][t]` and `bat_tech_power_investment[y][bt]` / `bat_tech_capacity_investment[y][bt]` (vectors per bus), `transfer_investment[y][(i,j)]`, plus life-extension, RE ratio and slack variables. |
| `add_investment_constraints!` | `(model, vars, input)` | Cumulative investment limits per technology per bus. |
| `add_budget_constraints!` | `(model, vars, input)` | Annual budget caps. |
| `add_retirement_cascade_constraints!` | `(model, vars, input)` | Life-extension capacity of a unit can only decrease from one year to the next, so a retirement is not reversed. |
| `add_capacity_adequacy_constraints!` | `(model, vars, input)` | Ensures installed capacity meets peak demand plus reserve margin. |
| `add_transmission_symmetry_constraints!` | `(model, vars, input)` | Holds `transfer_investment[i,j] == transfer_investment[j,i]` where both directions exist. |
| `add_re_target_constraints!` | `(model, vars, input, targets)` | RE penetration ≥ target per system and year, with a penalised slack. |
| `add_re_increment_constraints!` | `(model, vars, input)` | Year-over-year RE penetration change limits. |
| `select_representative_days` | `(demand, year_idx, num_days, min_separation, timesteps_per_day, timesteps_per_year) -> Vector{Int}` | Pick representative days of one year from the demand profile. |
| `add_representative_days_validation!` | `(model, vars, input, targets; scenario=nothing, scenario_idx=0)` | Adds operational feasibility constraints on representative days. Creates sub-problem variables and constraints per day per year. With `scenario`, the days run on `scenario_master_input(...)`. The TSAM equivalent is the internal `add_tsam_periods_validation!`. |
| `create_day_ps_vars!` | `(model, input, year_idx, day_idx, hours) -> PowerSystemVariables` | Operational variables of one representative day. |
| `add_day_operational_constraints!` | `(model, ps_vars, vars, input, year_idx, day_idx, demand, start_hour; ...)` | Detailed operational constraints for a single representative day: generator dispatch, battery SOC dynamics, power balance, reserves, DC flow. |
| `calculate_day_operational_cost` | `(ps_vars, input, year_idx, hours; tech_output=nothing) -> AffExpr` | Cost of one representative day, scaled to the year by `resolution × days_per_year / representative_days_per_year`. |
| `calculate_day_operational_cost_tsam` | `(ps_vars, input, year_idx, hours, weight; tech_output=nothing) -> AffExpr` | Cost of one TSAM period, scaled by `resolution × weight`. Not exported. |
| `build_master_objective!` | `(model, vars, input)` | NPV of investment costs + operational costs from representative days. |
| `build_investment_cost_expression` | `(vars, input; mult=nothing, discount_rate=input.discount_rate) -> AffExpr` | Discounted investment cost. `mult` (a `ScenarioMultipliers`) scales unit investment costs for a stochastic scenario (technologies, battery power and energy, transmission). |
| `calculate_unit_npv` | `(result, input, unit_type, unit_idx, node; npv_threshold=0.0) -> UnitNPV` | NPV of one existing unit from a solved master result. |
| `solve_with_npv_iteration` | `(input; max_iterations=5, npv_threshold=0.0, use_representative_days=true) -> NPVIterationResult` | Iterative retirement: solve, compute unit NPV, force negative-NPV retirements, re-solve. |
| `extract_master_solution` | `(model, vars, input) -> MasterProblemResult` | Extract investment/retirement decisions per year per technology per bus. |
| `build_cumulative_capacity_expressions` | `(model, vars, input)` | Build JuMP expressions for cumulative installed capacity per technology per bus per year. |

Internal helpers shared by the monolithic master and Benders (not exported):

| Function | Description |
|----------|-------------|
| `day_operational_cost(ps_vars, input, hours; tech_output=nothing) -> AffExpr` | Unscaled operating cost of one representative day or TSAM period: fuel, O&M and CO2, load shedding, curtailment, battery throughput, reserve and inertia shortfalls. `calculate_day_operational_cost` and `calculate_day_operational_cost_tsam` scale it. |
| `co2_cost_per_mwh(input, unit, b)` | `input.co2_cost × co2_per_mwh(fuel_co2, unit, b)`: carbon cost per MWh produced by a generator or technology at bus `b`, as the dispatch charges it. |
| `co2_per_mwh(fuel_co2, unit, b)` | t CO2 per MWh of electricity: the fuel's factor (per MWh of fuel) over the unit's rated efficiency at `b` (the factor itself if the efficiency is ≤ 0). |
| `corridor_investment(inv, from, to)` | Investment variable of the corridor between two buses, in either key order; `nothing` if the corridor cannot be expanded. |
| `corridor_cost_terms(inv, costs)` | `(variable, cost per MW)` once per corridor. When both directions exist only the low→high one is charged; the cost is the higher of the two ends' `costs`. |

### Multi-System and Stochastic Extensions

| Function | Description |
|----------|-------------|
| `create_multi_system_master_problem(input::MultiSystemMasterInput)` | Multi-system coordination with inter-system links (shared transmission, coordinated investment). |
| `add_inter_system_constraints!(model, ext_vars, input::MultiSystemMasterInput)` | Inter-system transfer and coordination constraints. |
| `create_stochastic_master_problem(input::StochasticMasterInput; custom_constraints=nothing) -> (model, vars, targets, scenarios)` | Two-stage stochastic master. Investment is first-stage and shared; each scenario gets its own representative-day (or TSAM) operation built on `scenario_master_input`. If `use_stochastic` is false or there are no scenarios, it builds the deterministic master. |
| `scenario_master_input(input, mult::ScenarioMultipliers, year_idx) -> MasterProblemInput` | The input one scenario operates on in year `year_idx`: demand × `demand_growth^(year_idx-1)`, fuel cost (and generator and technology fuel-cost curves) × `fuel_cost × fuel_price_growth^(year_idx-1)`, fixed O&M × `gen_fixed_cost`, maintenance cost × `maintenance_cost` (batteries also × `bat_maintenance_cost`), start-up cost × `gen_start_up_cost`, converter variable cost × `converter_variable_cost`, link flow cost × `transmission_cost_per_mw_km`, CO2 cost × `carbon_price`, and the loss-of-load, reserve-shortfall, curtailment, RE-shortfall and CO2-budget penalties × their `penalty_*` multipliers. |
| `build_stochastic_objective!(model, vars, input, scenarios, scenario_costs)` | Probability-weighted sum over scenarios of discounted investment cost (with the scenario's investment-cost multipliers and discount rate), existing-asset costs (`build_annual_asset_cost(vars, input, y_idx; mult)`, decommissioning scaled by the scenario) and the scenario's operating cost, plus the slack penalties. |

`StochasticMasterInput(base_input, scenarios::Vector{Scenario}, use_stochastic)` wraps the base input. `Scenario(name, probability, multipliers)` holds a `ScenarioMultipliers` with fields `invest_cost_renewables`, `invest_cost_conventional`, `fuel_cost`, `maintenance_cost`, `invest_cost_storage`, `invest_cost_transmission`, `discount_rate`, `demand_growth`, `fuel_price_growth`, `carbon_price`, `gen_fixed_cost`, `gen_decommissioning_cost`, `bat_decommissioning_cost`, `bat_invest_cost_power`, `bat_invest_cost_capacity`, `penalty_loss_of_load`, `penalty_loss_of_reserve`, `penalty_curtailment`, `penalty_fre_penetration`, `gen_start_up_cost`, `bat_maintenance_cost`, `transmission_cost_per_mw_km`, `converter_invest_cost`, `converter_variable_cost`, `penalty_co2_budget_violation` (all 1.0 by default). Build it by keyword (`ScenarioMultipliers(fuel_cost=1.2)`) or with the first ten positional values in that order.

### Benders Decomposition

| Function | Description |
|----------|-------------|
| `run_benders_decomposition(input; max_iterations=50, tolerance=1e-4, use_representative_days=true, verbose_benders=true) -> BendersResult` | Solve the master problem by Benders decomposition: an investment master plus one operational subproblem per representative day, iterated until the relative gap is below `tolerance`. Load-shedding penalties in the subproblems are capped at `input.benders_lol_penalty_cap`. Stochastic scenarios are not supported. |
| `extract_master_solution_from_benders(model, vars, input, year_operational_costs) -> MasterProblemResult` | Build a `MasterProblemResult` from the Benders master, with operating costs taken from the subproblems. |

### Transmission DC

| Function | Description |
|----------|-------------|
| `TransmissionDC(network::NetworkConfig)` | Build the per-line DC network: lines (from per-line data, or from the adjacency matrix if there is none, plus transformer branches), reactances, capacities, incidence matrix, slack bus and loss model. Self-loops are dropped. |
| `build_incidence_matrix(num_buses::Int, lines::Vector{Tuple{Int,Int}})` | Bus-line incidence matrix K where K[i,ℓ] = +1 if line ℓ leaves bus i and −1 if it enters. |
| `add_dc_constraints!(model, transmission, vars, input; extra_injections_fn=nothing, expandable_lines=Set{Int}())` | Per-line flow variables, KCL at each bus, and flow-angle coupling `pf_ℓ = S_base/x_ℓ · (θ_i − θ_j)` with one angle reference per connected component. Lines below 0.1 MW have their flow fixed to 0 unless their index is in `expandable_lines` (candidate corridors that investment can expand). |
| `add_line_capacity_constraints!(model, transmission, vars, input; capacity_override=nothing)` | Thermal line limits (both directions), scaled by scheduled outages. |
| `add_converter_constraints!(model, vars, input; capacity_override=nothing)` | AC/DC and frequency converter power transfer with directional efficiencies. The master passes each converter's capacity with its investment (`("acdc" \| "freq", c) => expression`). |
| `add_converter_objective_terms(vars, ...)` | Converter operating cost terms for the objective. |

### ACOPF and AC Power Flow

| Function | Description |
|----------|-------------|
| `setup_acopf!(model, vars, input; extra_injections_fn=nothing) -> ACOPFVariables` | Install the ACOPF formulation selected by `input.power_flow_mode`: `acopf_soc`, `acopf_qc`, `acopf_sdp` (relaxations), `acopf_polar` or `acopf_rect` (NLP). Checks that the solver supports the formulation. |
| `parse_acopf_formulation(mode) -> ACOPFFormulation` | Map a mode string to `SOCFormulation`, `QCFormulation`, `SDPFormulation`, `PolarNLPFormulation` or `RectNLPFormulation`. |
| `extract_acopf_voltages(av, n_bus, hours)`, `extract_acopf_reactive_gen(av, ...)` | Voltage magnitudes and reactive generation after the solve. |
| `run_ac_power_flow(input, result, config::ACPowerFlowConfig, hour)` | Newton-Raphson AC power flow for one hour of a solved dispatch (verification). |
| `solve_ac_power_flow(Y_bus, P, Q, bus_types, V_init, theta_init, config)` | Newton-Raphson solver core; `build_ybus`, `classify_buses` and `calculate_line_flows` are the helpers. |
| `solve_gui_ac_power_flow(gui::GuiACPowerFlowInput; max_iterations=50, tolerance=1e-6, base_mva=100.0, voltage_min_pu=0.90, voltage_max_pu=1.10)` | AC power flow on a network drawn in the Studio. |

`ACPowerFlowConfig(; max_iterations=50, tolerance=1e-6, base_mva=100.0, voltage_min_pu=0.90, voltage_max_pu=1.10)`.

### Primary Energy

| Function | Description |
|----------|-------------|
| `create_primary_energy_model(model, input::PrimaryEnergyInput)` | Add the fuel supply chain (supply, storage, transport, non-electric demand) to a model. |
| `create_temporal_mapping(hours, primary_resolution, investment_resolution)` | Map power-system hours to primary-energy steps. |
| `build_primary_energy_variables!`, `add_primary_energy_constraints!`, `get_primary_energy_objective_terms`, `extract_primary_energy_solution` | The steps `create_primary_energy_model` runs, exposed separately. |
| `couple_primary_energy_to_power_system!(model, pe_vars, ps_vars, input; bus_to_node, resolution_hours, generators, electrolyzers)` | Link generator fuel use to fuel supply (per unit and node) and feed the electrolyzers' hydrogen into the `Hydrogen` tank. |
| `in_transit_arrivals(vars, input, temporal, k)` | Fuel still in transit after the window's first `k` steps, as the next window's `scheduled_arrivals` (fuel => [node, step]). |

Types: `FuelConfig`, `FuelInfrastructureConfig`, `NonElectricDemandConfig`, `FuelRouteParams`, `TransportRoute`, `PrimaryEnergyInput`, `PrimaryEnergyVariables`, `PrimaryEnergyResult`. The master problem has no fuel infrastructure investment (fuel storage and transport are built by the dispatch's primary-energy block).

### MGA (classical HSJ loop)

| Function | Description |
|----------|-------------|
| `run_mga_spores(input; num_alternatives=10, slack_fraction=0.05, use_representative_days=true, investment_threshold=0.1)` | Run the classical MGA Hop-Skip-Jump algorithm. Returns an `MGAResult` whose `objective_labels` are all `"hsj_diversity"`. |
| `compute_frequency_scores(alternatives, input; investment_threshold)` | Compute SPORES frequency scores: `score[var] = 1 - 2 * frequency[var]`. Variables appearing in many alternatives get negative scores. |
| `set_spores_objective!(model, vars, input, frequency_scores)` | Replace the objective with `max sum(score[var] * var / var_max)` to maximize diversity. |

### SPORES (Phase 2)

| Function | Description |
|----------|-------------|
| `run_spores(input; objectives::Vector{Symbol}, slack_fraction=0.05, use_representative_days=true, investment_threshold=0.1)` | Run the SPORES sweep — one alternative per entry in `objectives`. Returns an `MGAResult` whose `objective_labels[k]` matches `String(objectives[k])`. |
| `apply_spores_objective!(model, vars, input, objective::Symbol; frequency_scores, reference_solution)` | Dispatcher. Routes a SPORES objective `Symbol` to the matching `set_*_objective!`. Cleans up aux variables from the previous objective via `_clear_spores_aux!` before installing the new one. |
| `set_min_build_objective!(model, vars, input)` | $\min \sum I^{tech} + \sum I^{bat,P} + \sum I^{tr}$ — see [SPORES-1](../formulation/capacity-expansion.md#1510-minimum-total-build-objective). |
| `set_tech_equity_objective!(model, vars, input)` | $\min M$ s.t. $\sum_{n,y} I^{tech}_{y,t,n}/\bar{I}_{t,n} \leq M$ $\forall t$ — see [SPORES-2](../formulation/capacity-expansion.md#1511-technology-equity-objective). Adds one auxiliary variable $M$ and $|\mathcal T|$ constraints. |
| `set_regional_equity_objective!(model, vars, input)` | $\min M$ s.t. $\sum_{t,y} I^{tech}_{y,t,n}/\bar{I}_{t,n} + \sum_{b,y} I^{bat,P}_{y,b,n}/\bar{I}^P_{b,n} \leq M$ $\forall n$ — see [SPORES-3](../formulation/capacity-expansion.md#1512-regional-equity-objective). The spatially-explicit objective at the heart of the SPORES name. |
| `set_evolutionary_distance_objective!(model, vars, input, reference_solution)` | $\max \sum \|I - I^{ref}\|/\bar{I}$ via L1 linearisation with positive/negative deviation aux variables — see [SPORES-4](../formulation/capacity-expansion.md#1513-evolutionary-distance-objective). Typically called with the cost-optimal solution as the reference. |

Auxiliary variables and constraints installed by any of the above are tracked in `model[:_spores_objective_aux]` and deleted by `_clear_spores_aux!(model)` at the start of the next `apply_spores_objective!` call, so a sweep loop can reuse one JuMP model across all objectives without unbounded growth.

### Utility Functions

| Function | Description |
|----------|-------------|
| `create_optimizer(; solver_name="highs", threads=4, time_limit=3600.0, gap=0.01, verbose=false, solver_options=Dict())` | Configure a JuMP optimizer with solver-specific parameter mapping. Supports HiGHS, Gurobi, CPLEX, SCIP, Xpress, CBC, GLPK, Ipopt, Clarabel, SCS. |
| `diagnose_infeasibility(model, vars::MasterProblemVariables, input) -> Dict{String, Float64}` | For a solved master problem, report the RE-target, budget and capacity slack values by category. |
| `export_solution_to_dict(result::MasterProblemResult, input) -> Dict{String, Any}` | Convert a master solution to a Dict for JSON/HDF5 serialization. |
| `log_solution_summary(result::MasterProblemResult, input; ev_demand=nothing) -> String` | Formatted summary of a master solution (investments, costs, RE penetration). |
| `register_constraint_hook!(type, fn)` | Register a user constraint type; `fn(model, vars, input, spec)` is called for each spec with `spec["type"] == type`. Not exported (call as `ESFEX.register_constraint_hook!`). |

---

## Constraint Details

### Battery Cyclic SOC

In the master problem, each representative day starts and ends at the initial SOC, for example for candidate battery technologies:

```julia
@constraint(model, bat_tech_soc[bt, b, 0]     == en_cap * initial_soc_frac)
@constraint(model, bat_tech_soc[bt, b, hours] == en_cap * initial_soc_frac)
```

With TSAM inter-period linking, the period boundaries are chained instead (`inter_period_soc`), and the chain is cyclic over the year. This prevents batteries from acting as free generators by accumulating energy across periods. In the operational model, rolling windows pass SOC on through boundary conditions; see `add_battery_constraints!` above.

### Curtailment Limit

In the master problem, curtailment on each representative day is capped at a fraction of available renewable generation:

```julia
if input.max_curtailment_ratio < 1.0
    @constraint(model, total_curtailment <= input.max_curtailment_ratio * total_renewable)
end
```

Benders subproblems use the same cap as a soft constraint with a penalised slack. The operational model has no ratio cap; curtailment there is priced at `curtailment_cost`.

### Age-Based Retirement

Units are retired based on age exceeding lifetime:

| Unit Type | Age Formula | Active Condition |
|-----------|-------------|-----------------|
| Existing | `age = initial_age + (year_idx - 1)` | `age < lifetime` |
| Investment | `age = year_idx - investment_year` | `age < lifetime` |

Pure LP formulation: life extension of a unit past its lifetime is a continuous variable (no binaries).

### Free Generation Prevention

`build_variables!` creates `gen_output` and the battery variables only for unit-bus pairs that have capacity, investment potential (development mode) or a pending retirement. A generator with `rated_power[bus] = 0` and no investment potential therefore has no output variable at that bus, so it cannot inject free energy into the power balance.

### PWL Transmission Losses

Piecewise linear approximation of quadratic transmission losses:

```julia
# P_loss(f) = g_l * f^2  approximated by N linear segments
# Marginal slope for segment k: m_k = g_l * (2k-1) * delta_f
# where delta_f = f_max / N and g_l = R / (R^2 + X^2)
```

Convexity of the quadratic loss function ensures the PWL approximation is exact at segment boundaries without requiring binary variables.
