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
    include("log_forwarding.jl")     # Julia log records → the Python logger
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
    battery_discharge_prev::Dict{Int, Dict{Int, Float64}}   # MW, for the battery ramp limits
    battery_charge_prev::Dict{Int, Dict{Int, Float64}}      # MW
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
    fixed_cost::Vector{Float64}     # Per MW of power in service per year
    maintenance_cost::Vector{Float64}  # Per MWh
    inertia::Vector{Float64}        # Inertia constant H (s)
    invest_cost::Vector{Float64}    # Per MW
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
    # ... reservoir_min_release, cascade, head dependence
    num_units::Vector{Int}                    # Identical units per bus (clustered UC); units_of(gen, b)
end
```

Older call sites may omit the trailing fields: the constructors fill the defaults (`num_units` = one unit per bus).

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
    invest_cost_power::Vector{Float64}  # $/MW per bus, prices life extension
    invest_max_power::Vector{Float64}   # MW > 0: battery placed at the bus in the master
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
    ramp_up::Vector{Float64}            # Fraction of rated power per hour (0 = no limit)
    ramp_down::Vector{Float64}
    fixed_cost::Vector{Float64}         # Per MW of discharge power per year
    reservable::Bool                    # Headroom counts towards reserves
    availability::Matrix{Float64}       # (steps x buses) factor on charge/discharge; 0 rows = 1.0
end
```

A 26-argument constructor (without the last five fields) gives no ramp limit, no fixed cost, `reservable = true` and no availability profile. `BatteryTechnologyConfig` has the same five fields at its end, with the same short constructor.

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
    budget_total::Bool                          # budget_scope: total (BUD-2)
    # RE targets
    target_re_penetration::Float64
    initial_re_penetration::Float64
    min_re_increment::Float64                   # Floor initial + y × min (RE-3a)
    max_re_increment::Float64                   # Cap on the actual share's yearly growth (RE-3b)
    system_node_ranges::Vector{SystemNodeRange} # Per-system RE targets
    # Representative days
    temporal_resolution_hours::Int
    representative_days_per_year::Int
    min_day_separation::Int
    investment_resolution_hours::Int
    # (further down) rep_day_offsets::Vector{Int}; critical_days::Int (Julia's
    # own choice when rep_day_offsets is empty); critical_day_offsets::Vector{Int};
    # rep_day_weights::Vector{Float64} (days each representative, then critical,
    # day stands for); storage_firm_hours::Float64 (≤ 0 = from the data)
    # TSAM (Time Series Aggregation Method)
    use_tsam::Bool
    tsam_period_start_hours::Vector{Vector{Int}}
    tsam_period_weights::Vector{Vector{Float64}}
    tsam_day_period::Vector{Vector{Int}}     # period of each day of the year (counts = weights)
    tsam_inter_period_linking::Bool
    # Penalty coefficients
    loss_of_load_penalty::Float64          # also prices the adequacy shortfall
    fre_penetration_loss_penalty::Float64  # per MWh missing from an RE target
    max_curtailment_ratio::Float64
    curtailment_cost::Float64
    # Reserves, inertia, CO2
    reserve_static_requirement::Dict{Int, Float64}
    reserve_dynamic_requirement::Dict{Int, Float64}
    inertia_limit::Float64
    fuel_co2::Dict{String, Float64}
    co2_cost::Float64
    co2_budget_groups::Vector{CO2BudgetGroup}       # Annual CO2 budget per system (empty = none)
    unit_commitment::Bool                          # Dispatch commits units: LP start-ups in the master
    # Transmission (per physical line)
    transmission_lines::Vector{Tuple{Int,Int}}
    transmission_reactances::Vector{Float64}
    transmission_capacities::Vector{Float64}
    # Solver
    solver_name::String
    solver_options::Dict{String, Any}
    # NPV-based retirement (Capacity Expansion §9)
    base_lcoe::Float64                             # $/MWh a generator earns in its NPV
    npv_annual_return_rate::Float64                # Battery NPV revenue / power investment cost, per year
    npv_retired_units::Vector{RetiredUnit}         # Retired by retire_units: decommissioned in year 1
    re_share_anchor::Vector{Float64}               # Actual RE share per system before year 1 (myopic: last year's); empty = initial
    fuel_price_growth::Dict{String, Float64}       # fuels.<f>.price_growth_rate per fuel (year_master_input)
    fuel_price_base_year::Int                      # Year the fuel prices hold for: the run's first year (default years[1])
    # ... additional fields (sectoral demand, primary energy, cost curves)
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
    fixed_cost::Vector{Float64}         # Per MW built and in service per year
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
| `build_objective!` | `(model, vars, input)` | Build cost minimization objective: fuel and maintenance costs per MWh + the window's share of the annual fixed O&M of the capacity in service (`fixed_om_annual_cost`) + start-up costs + penalty terms. |
| `add_generator_constraints!` | `(model, vars, input)` | Generator capacity limits, ramp rates, minimum up/down times. For units with `rated_power[b]=0` and no investment potential, constrains `gen_output[g,b,t] <= 0` to prevent free generation. |
| `add_battery_constraints!` | `(model, vars, input)` | SOC dynamics, charge/discharge limits, end-of-horizon SOC: two-sided cyclic band when `cyclic_end_soc` is true, otherwise the soft floor `bat_soc_end_min` / `bat_soc_end_min_cyclic` used on the year's last window. For zero-capacity batteries, constrains all variables to zero; a battery with energy but no charge or discharge power at a bus counts as zero capacity there (`battery_power_limits`). The minimum cycling (`bat_min_cycling`) is capped at the window's charge capability and has a priced shortfall, so it never makes a window infeasible. |
| `add_demand_constraints!` | `(model, vars, input; extra_injections_fn=nothing)` | Copper-plate power balance per bus and timestep: `gen + discharge + V2G + load_shed == demand + charge + EV charging + electrolyzer` (plus rooftop terms when given). `load_shed` is capped by the bus demand. With more than one bus, `create_power_system` uses the KCL of `add_dc_constraints!` (or ACOPF) instead. |
| `add_reserve_constraints!` | `(model, vars, input)` | Static (spinning) and dynamic (frequency response) reserve: per-node requirements met per system (`reserve_node_groups`: one group per original system of a merged run) from the pooled free capacity of the system's reservable generators and batteries; committed units only in UC. |
| `add_inertia_constraints!` | `(model, vars, input)` | System inertia floor from synchronous generators. |
| `add_curtailment_constraints!` | `(model, vars, input)` | Defines `curtailment[n,t]` as available minus dispatched variable RE at each node (reservoir hydro excluded). |
| `add_renewable_constraint!` | `(model, vars, input)` | RE penetration target: `renewable_gen >= target * total_gen`. |
| `add_co2_constraint!` | `(model, vars, input)` | CO2 budget: `sum(fuel_gen * emission_factor / eff_at_rated) <= co2_budget` scaled to the window's share of the year, with a penalised violation slack. With `co2_budget_groups` (multi-system run), one such constraint per group (system), over its nodes, with its own budget and penalty. |
| `add_ev_constraints!` | `(model, vars, input)` | EV charging scheduling and V2G dispatch with SOC tracking. |
| `add_sectoral_demand_constraints!` | `(model, vars, input)` | Per-sector loss-of-load with criticality-weighted penalties and demand shifting. |
| `add_n1_security_constraints!` | `(model, vars, input)` | N-1 contingency constraints for transmission and generation. |
| `extract_solution` | `(model, vars, input; committed=nothing) -> PowerSystemResult` | Extract all variable values into a structured result. With `committed` (from `committed_steps`), the objective, scalar totals and cost breakdown cover only a rolling window's kept steps. |
| `committed_steps` | `(ps_vars, hours, keep; pe_vars, pe_temporal) -> CommittedSteps` | Share of each variable in a window's first `keep` steps (1 / 0 per step, period share for primary-energy periods, `keep / hours` for variables without a step). `committed_value(cs, expr)` values a solved expression over those steps. |
| `add_operating_budget!` | `(model, budget; committed=nothing) -> VariableRef` | COST-1: the window's operating spending (`OPERATING_BUDGET_COSTS`: fuel, maintenance, fixed O&M, primary-energy fuel bought and transported), over the committed steps with `committed` (`committed_expression`), at most `budget` (solver units) plus the excess `operating_budget_excess`, priced at `OPERATING_BUDGET_PENALTY` (1000) per unit. Call it after the objective is complete; the excess and its penalty go to the cost breakdown. |
| `recover_uc_duals_via_copy` | `(model, vars, input) -> Union{Matrix, Nothing}` | UC only: on a copy of the solved model, fix the commitment binaries to their rounded values and re-solve as an LP so balance duals (prices) exist; returns the `[bus x hour]` price matrix in M$/MWh, or `nothing`. The model itself is not changed. Used by `PowerSystemAdapter`. |
| `compute_n1_security_cost!` | `(model, input) -> Float64` | Re-solve a copy of the solved model with the N-1 slacks free of charge and store objective(with) − objective(without) in `model.ext[:n1_security_cost]`, which `extract_solution` reports as `n1_security_cost` (solver cost units); the re-solve's solution is kept in `model.ext[:n1_security_resolve]` so that, with `committed`, both objectives are valued over the committed steps (`n1_security_cost(model, committed)`). Used by `PowerSystemAdapter` when `n1_security.compute_security_cost` is on. |

### Master Problem (Capacity Expansion)

| Function | Signature | Description |
|----------|-----------|-------------|
| `create_master_problem` | `(input::MasterProblemInput; use_representative_days=true, custom_constraints=nothing, capacity_states=true) -> (Model, MasterProblemVariables, targets)` | Build multi-year capacity expansion model. With `use_representative_days=false` only the investment structure is built (no operational validation). `capacity_states=false` writes the investment sums into the hourly rows instead of the per-year capacity state variables (`add_capacity_states!`): the same LP, much denser; for comparisons only. |
| `calculate_target_ratios` | `(input::MasterProblemInput) -> Dict{Tuple{Int,Int}, Float64}` | RE target per `(system_idx, year_idx)`: linear interpolation from the initial to the target penetration, clipped by the min/max annual increments. |
| `build_master_variables!` | `(model, input) -> MasterProblemVariables` | Investment variables, keyed by year index: `tech_investment[y][t]` and `bat_tech_power_investment[y][bt]` / `bat_tech_capacity_investment[y][bt]` (vectors per bus), `transfer_investment[y][(i,j)]`, plus life-extension, RE ratio and slack variables. |
| `add_investment_constraints!` | `(model, vars, input)` | Cumulative investment limits per technology per bus. |
| `add_budget_constraints!` | `(model, vars, input; operating=nothing)` | Annual budget on the overnight cost of each investment period's builds (technologies, battery technologies, transmission, reservoirs), with a slack at `BUDGET_SLACK_PENALTY` (10) per unit over it. With `operating` (year index => expression) the year's operating cost counts against it too (`budget_scope: total`, BUD-2). |
| `budget_operating_cost` | `(model, vars, input; scenarios=nothing) -> Dict{Int, AffExpr}` | Each year's operating money for BUD-2: what the representative days (or TSAM periods) pay, scaled to the year (`day_operating_spend` and the fuel block's `master_fuel_day_spend`, recorded by `record_year_operating_cost!`: fuel, maintenance, start-ups, converter and line-flow costs, primary-energy fuel and electrolyzers), plus `build_annual_asset_cost`; no modelling penalty, no battery degradation and no CO2 price. With stochastic `scenarios`, probability-weighted, each with its multipliers. `create_master_problem` adds BUD-2 itself (`defer_budget = true` leaves it to the stochastic master). |
| `add_retirement_cascade_constraints!` | `(model, vars, input)` | Life-extension capacity of a unit can only decrease from one year to the next, so a retirement is not reversed. |
| `add_capacity_adequacy_constraints!` | `(model, vars, input)` | Ensures installed capacity meets peak demand plus reserve margin. |
| `add_transmission_symmetry_constraints!` | `(model, vars, input)` | Holds `transfer_investment[i,j] == transfer_investment[j,i]` where both directions exist. |
| `add_re_target_constraints!` | `(model, vars, input, y_idx, targets, re_energy, re_generation, re_demand; scenario_idx=0) -> AffExpr` | Annual RE bounds of each system in year `y_idx`: its renewable generation over the representative days (scaled to the year) plus a shortfall slack `slack_re_target[(scenario_idx, σ, y_idx)]` (MWh) covers `re_required_share` of its total generation (RE-2, RE-3a), and the growth cap `add_re_growth_cap!` holds its actual share over the same scenario's previous year (`re_cap_prev_allowance`). Called by the representative-day and TSAM validations; returns the slacks' cost, part of the year's operating cost. |
| `add_re_growth_cap!` | `(model, input, s, y_idx, energy, generation, allowance; scenario_idx=0) -> AffExpr` | RE-3b: `energy ≤ allowance + max_re_increment × generation + slack`, with `allowance` the renewable generation the previous year's share allows (`re_cap_prev_allowance(prev, generation, demand)`: a share × this year's generation, or last year's renewable generation × this year's demand / last year's), the slack (MWh, `model.ext[:re_cap_slack]`) priced at `re_cap_penalty` (VOLL, at least the RE shortfall price), a last resort: existing renewables above the cap are curtailed (`re_cap_exemption`). Nothing when `max_re_increment` ≥ 1. |
| `re_cap_exemption` | `(input, targets, y_idx, periods, prev_floor) -> (θ, floor)` | RE-3c: per system, the share θ of its existing variable renewables' available energy it may curtail beyond `max_curtailment_ratio` to stay under the growth cap (0 when they stay under it), from the year's periods `(start_hour, hours, scale)`; `floor` (the least share they leave the year) feeds the next year's call. The year's curtailment limit allows θ × the available existing energy of its days on top (`re_cap_exempt_energy`). |
| `define_re_share!` | `(model, vars, input)` | Defines `re_energy_ratio[σ, y]`, renewable generation over demand (probability-weighted in a stochastic master), the growth cap's link between years, as an expression of the day variables (no row: `demand × ratio == energy` put the year's demand in the matrix; the placeholder variables are deleted). The RE share itself is renewable over total generation (`is_renewable_unit`; `master_re_shares`). |
| `re_required_share` | `(input, targets, s, y_idx) -> Float64` | The RE-1 target or the minimum-increment floor, whichever is higher. |
| `re_increment_floor` | `(initial_re, min_inc, y_idx) -> Float64` | Least RE share the minimum increment asks of year `y_idx`: `initial_re + y_idx × min_inc`, at most 1. |
| `re_share_anchor` | `(input, s) -> Float64` | Actual RE share of system `s` before year 1, from which year 1's cap is measured: `input.re_share_anchor[s]` (myopic: last year's achieved share) or its initial share, clamped to [0, 1]. |
| `master_conflict` | `(model; limit=20) -> Vector{String}` | Constraints of an infeasible master's conflict (IIS), empty when the solver cannot compute one. |
| `master_diagnostics` | `(model, vars, input; top_n=20, per_family_top=5, tol=1e-6, dual_tol=1e-9) -> NamedTuple` | Binding constraints, duals and paid slacks of a solved master (`master_diagnostics.jl`), in one pass over its rows and the upper bounds of its investment variables: `duals_available`, `status`, `binding` (per year index and constraint family: `count_binding`, `count_priced`, `count_total`, `max_abs_dual`, `sum_abs_dual`, `units`; year 0 for rows without `_y{y}` in a master of several years), `top` (per year, the rows of the largest \|dual\| outside the structural families) and `slacks` (`master_paid_slacks`). A dual is the cost of tightening the row by one unit, in $ of the row's year, per unit of the row's own slack where it has one. Families: `CONSTRAINT_FAMILIES`, `BOUND_FAMILIES` ([Constraint Catalog](../reference/constraint-catalog.md#families-in-the-binding-report)). |
| `master_paid_slacks` | `(model, vars, input; tol=1e-6) -> Vector{NamedTuple}` | The slack and penalty variables a solved master pays for (value > `tol`): the annual slacks one row each, the representative days' load shed and reserve/inertia shortfalls per kind and year scaled to the year (MWh/yr), each with `unit_cost` and `cost` in the year's own $ as the objective charges them. |
| `select_representative_days` | `(demand, year_idx, num_days, min_separation, timesteps_per_day, timesteps_per_year) -> Vector{Int}` | Peak-based days of one year from the demand profile alone (the peak day, then the highest-peak day of each segment). Not the default any more: see `representative_day_offsets`. |
| `representative_day_offsets` | `(demand, num_years, num_days, min_separation, timesteps_per_day, timesteps_per_year; profiles=Vector{Float64}[]) -> Vector{Int}` | The representative days of a whole horizon, chosen once, as 0-based steps within a year: `select_matching_days` on the horizon-mean demand and the horizon mean of each renewable availability series in `profiles`. |
| `select_matching_days` | `(day_means, daily_peaks, num_days, min_separation) -> Vector{Int}` | The peak day, then one day per segment of the year, chosen so that the mean of every feature (column of `day_means`) over the days matches its annual mean: minimises Σ (days' mean / annual mean − 1)², greedily, then moving one or two days at a time. |
| `representative_day_ratios` | `(demand, num_years, offsets, timesteps_per_day, timesteps_per_year; profiles, weights) -> Vector{Float64}` | Mean on the representative days or periods (weighted) over the annual mean, demand first, then each profile. |
| `master_rep_day_offsets` | `(input) -> Vector{Int}` | `input.rep_day_offsets` when given (the runner's choice), else `representative_day_offsets` of `input.base_demand` with the distinct availability profiles of the renewable generators and candidate technologies (`master_re_profiles`). |
| `master_rep_days` | `(input, y_idx; offsets) -> Vector{Int}` | First master step of each representative day of year `y_idx`: the same days of the year in every year's block. Used by the monolithic, stochastic, MGA and Benders masters. |
| `critical_day_plan` | `(demand, num_years, num_critical, min_separation, timesteps_per_day, timesteps_per_year; profiles, capacities, exclude) -> (offsets, firm_hours)` | The critical days (0-based steps within a year, most critical first): the days with the largest net load, horizon-mean demand over its mean minus the renewable supply index (`re_supply_index`: profiles weighted by the MW behind each) over its mean, skipping `exclude` and `min_separation` days apart. `firm_hours`: the longest stretch without solar on those days (with no solar profile, with renewable supply below demand; 4 h without profiles). |
| `representative_day_weights` | `(demand, num_years, offsets, critical_offsets, timesteps_per_day, timesteps_per_year; profiles, prior) -> Vector{Float64}` | Days of the year each representative day (or TSAM period) and then each critical day stands for: 1 per critical day; the representative days share the rest from `prior` (equal when empty), corrected so the year keeps the demand and renewable energy of the representative days alone. |
| `master_day_plan` | `(input) -> (offsets, weights, num_rep)` | The days the monolithic, stochastic and MGA masters model every year: the representative days, then the critical days (`input.critical_day_offsets`, or `input.critical_days` chosen by `critical_day_plan` when Julia picks the days itself), with their weights (`input.rep_day_weights` or `representative_day_weights`). |
| `master_storage_firm_hours` | `(input) -> Float64` | `input.storage_firm_hours` when positive, otherwise `critical_day_plan`'s firm hours: the H of the battery firm credit min(power, usable energy / H) in `add_capacity_adequacy_constraints!`. |
| `year_master_input` | `(input, year_idx) -> MasterProblemInput` | The input of year `year_idx`: every generator and candidate technology whose `fuel` is in `input.fuel_price_growth` pays `fuel_cost × fuel_price_factor(rate, year, input.fuel_price_base_year)`; the others, and offer curves, as given. Each representative day and TSAM period runs on it (a scenario on `scenario_master_input` of it). |
| `fuel_price_factor` | `(growth_rate, year, base_year) -> Float64` | (1 + `growth_rate`)^(`year` − `base_year`): the growth of a fuel's price, for the fuel balance (primary energy) and the units' `fuel_cost`. |
| `add_representative_days_validation!` | `(model, vars, input, targets; scenario=nothing, scenario_idx=0)` | Adds operational feasibility constraints on representative days. Creates sub-problem variables and constraints per day per year. With `scenario`, the days run on `scenario_master_input(...)`. The TSAM equivalent is the internal `add_tsam_periods_validation!`. |
| `create_day_ps_vars!` | `(model, input, year_idx, day_idx, hours) -> PowerSystemVariables` | Operational variables of one representative day. |
| `add_day_operational_constraints!` | `(model, ps_vars, vars, input, year_idx, day_idx, demand, start_hour; ...)` | Detailed operational constraints for a single representative day: generator dispatch, battery SOC dynamics, power balance, reserves, DC flow. |
| `calculate_day_operational_cost` | `(ps_vars, input, year_idx, hours; tech_output=nothing) -> AffExpr` | Cost of one representative day, scaled to the year by `resolution × days_per_year / representative_days_per_year` (Benders). |
| `calculate_day_operational_cost_tsam` | `(ps_vars, input, year_idx, hours, weight; tech_output=nothing) -> AffExpr` | Cost of one TSAM period, or of one representative or critical day of the monolithic master, scaled by `resolution × weight` (the days it stands for). Not exported. |
| `build_master_objective!` | `(model, vars, input)` | NPV of investment costs + operational costs from representative days. |
| `build_investment_cost_expression` | `(vars, input; mult=nothing, discount_rate=input.discount_rate) -> AffExpr` | Discounted investment annuities: each build pays `capital_recovery_factor(discount_rate, life)` × its overnight cost in every year it is in service inside the horizon (`master_investment_terms`, `annuity_years`). `mult` (a `ScenarioMultipliers`) scales unit investment costs for a stochastic scenario (technologies, battery power and energy, transmission, converters, fuel builds). |
| `capital_recovery_factor` | `(rate, life) -> Float64` | `r(1+r)^L / ((1+r)^L − 1)`, `1/L` at rate 0; a non-positive `life` takes `DEFAULT_ASSET_LIFETIME` (40 years). |
| `master_slack_cost` | `(vars, input; discount_rate=input.discount_rate) -> AffExpr` | Cost of the budget slack (`BUDGET_SLACK_PENALTY` per unit) and of the capacity adequacy slack (`adequacy_slack_penalty`: VOLL × the node's peak hours per MW), each year discounted. |
| `calculate_unit_npv` | `(result, input, unit_type, unit_idx, node; npv_threshold=0.0, extra_years=0) -> UnitNPV` | NPV of keeping existing unit `unit_idx` ("generator" or "battery") at bus `node` in the solved plan: its dispatched MWh × `base_lcoe` (a battery: `npv_annual_return_rate` × `invest_cost_power` × MW in service) less the operating, fixed O&M, life-extension and decommissioning cost the master charges it, discounted per year; probability-weighted in a stochastic master. With `extra_years > 0` (a myopic year's master) it also covers that many later years while the unit is in service, the last modelled year's operation held (degradation, fuel price growth, end of life without extension; `extra_unit_npv`). `result` must hold `unit_operation`. See [Capacity Expansion §9](../formulation/capacity-expansion.md#9-npv-based-iterative-retirement). |
| `unit_npvs` / `get_units_with_negative_npv` | `(result, input; npv_threshold=0.0, extra_years=0) -> Vector{UnitNPV}` | `calculate_unit_npv` of every existing unit location in service in some year of the plan / of those below the threshold. |
| `extract_unit_operation` | `(model, vars, input) -> Vector{UnitOperation}` | Each existing unit's MWh and operating, fixed O&M, life-extension and decommissioning cost per year in a solved monolithic or stochastic master (one entry per scenario). |
| `retire_units` | `(input, units::Vector{UnitNPV}; decommission=true) -> MasterProblemInput` | The input with those units retired for the whole horizon at their bus: capacity 0, decommissioning paid once in the first year (`decommission=false`: not again, for a later myopic year). |
| `solve_with_npv_iteration` | `(input; max_iterations=5, npv_threshold=0.0, use_representative_days=true, solver=:monolithic, extra_years=0, benders_kwargs...) -> NPVIterationResult` | Solve (`optimize_master!`), retire the units whose NPV is below the threshold, re-solve, until none is below it or after `max_iterations` rounds. A re-solve that is not optimal, that curtails more above the annual curtailment limit than the plan before (`curtailment_excess_mwh`), or whose objective is above `npv_round_reference_cost` of the plan before (`cost_grew`, relative 1e-6), undoes its round (warning with the units and the reason): the result is the last optimal plan and its retirements. `solver=:benders` uses `run_benders_decomposition`. `esfex run` does the same through `MasterProblemAdapter` when the system setting `npv_retirement` is on, in every master mode. |
| `npv_round_reference_cost` | `(result, input, units::Vector{UnitNPV}; extra_years=0) -> Float64` | The total cost a re-solve that retires `units` must not exceed: `result.objective` less the units' NPV past the master's horizon (`extra_unit_npv`, probability-weighted; 0 with `extra_years = 0`). The objective already holds the units' operating, fixed O&M, life-extension and decommissioning costs. See [Capacity Expansion §9.1](../formulation/capacity-expansion.md#91-algorithm). |
| `extract_master_solution` | `(model, vars, input; unit_operation=false) -> MasterProblemResult` | Extract investment/retirement decisions per year per technology per bus; with `unit_operation=true`, also each existing unit's operation and costs (`result.unit_operation`) for its NPV. `result.annuity_cost_by_year` holds the annuities paid each year and `result.annuity_streams` the streams behind them, `(build year, life, annuity per year)`. Capacities (builds, life extension, cumulative capacities, transmission, converters, reservoirs, fuel infrastructure) of at most `CAPACITY_TOL` = 10⁻³ MW or MWh are reported as 0 (`capacity_value`): solver noise of a barrier solve without crossover. |
| `optimize_master!` | `(model; retry=NUMERICAL_RETRY_PARAMS[solver])` | Solve a master; when a solve ends `NUMERICAL_ERROR` or `LOCALLY_SOLVED` (Gurobi's `SUBOPTIMAL`) and the solver has retry parameters (Gurobi: `BarHomogeneous = 1`), solve once more with them (WARNING in the log) and set them back (`solve_retrying!`). With line losses, each solve of the loss repair (`optimize_exact_losses!`) is retried that way, and a repair round still not optimal is undone (the round before solved again). Used by the adapter's master solve, MGA and `solve_with_npv_iteration`. A solved master that curtails above its annual curtailment limit gets a WARNING per system and year (`warn_curtailment_excess`). |
| `optimize_dispatch!` | `(model; retry=NUMERICAL_RETRY_PARAMS[solver], label="Dispatch")` | Solve a dispatch window the way `optimize_master!` solves a master (`optimize_retrying!`): one more solve with the retry parameters, each loss-repair solve retried, a repair round still not optimal undone; `label` names the window in the warnings. Used by the adapter's dispatch solve, which keeps a window still `LOCALLY_SOLVED` with a WARNING. |
| `transfer_expansion_max` | `(vars, from, to, year_idx) -> Float64` | Most MW the corridor can have gained by `year_idx` (the upper bounds of its investment variables; `Inf` when one has none, 0 without any). A line is expandable in the master's days only when it is above 0, and it bounds the line's last loss segment. |
| `build_cumulative_capacity_expressions` | `(vars, input, year_idx) -> Dict` | JuMP expressions for the capacity in service in year `year_idx` per unit and bus (existing generators and batteries, technologies, battery technologies), as sums of the investment and life-extension variables. |
| `add_capacity_states!` | `(model, vars, input)` | One state variable per year and built capacity (technology, battery-technology power and energy per bus, corridor, reservoir, converter, fuel build), defined by one equality on the investment variables and kept in `vars.capacity_state`. The representative-day rows use it instead of the investment sum (CUM-5). Called by `create_master_problem`. Not exported. |

Internal helpers shared by the monolithic master and Benders (not exported):

| Function | Description |
|----------|-------------|
| `day_operational_cost(ps_vars, input, hours; tech_output=nothing) -> AffExpr` | Unscaled operating cost of one representative day or TSAM period: fuel, O&M and CO2, load shedding, curtailment, battery throughput, reserve and inertia shortfalls. `calculate_day_operational_cost` and `calculate_day_operational_cost_tsam` scale it. |
| `co2_cost_per_mwh(input, unit, b)` | `input.co2_cost × co2_per_mwh(fuel_co2, unit, b)`: carbon cost per MWh produced by a generator or technology at bus `b`, as the dispatch charges it. |
| `co2_per_mwh(fuel_co2, unit, b)` | t CO2 per MWh of electricity: the fuel's factor (per MWh of fuel) over the unit's rated efficiency at `b` (the factor itself if the efficiency is ≤ 0). |
| `corridor_investment(inv, from, to)` | Investment variable of the corridor between two buses, in either key order; `nothing` if the corridor cannot be expanded. |
| `corridor_cost_terms(inv, costs)` | `(variable, cost per MW)` once per corridor. When both directions exist only the low→high one is charged; the cost is the higher of the two ends' `costs`. |

### Stochastic Extensions

| Function | Description |
|----------|-------------|
| `create_stochastic_master_problem(input::StochasticMasterInput; custom_constraints=nothing) -> (model, vars, targets, scenarios)` | Two-stage stochastic master. Investment is first-stage and shared; each scenario gets its own representative-day (or TSAM) operation built on `scenario_master_input`. If `use_stochastic` is false or there are no scenarios, it builds the deterministic master. |
| `scenario_master_input(input, mult::ScenarioMultipliers, year_idx) -> MasterProblemInput` | The input one scenario operates on in year `year_idx`: demand × `demand_growth^(year_idx-1)`, fuel cost (and generator and technology fuel-cost curves) × `fuel_cost × fuel_price_growth^(year_idx-1)`, maintenance cost × `maintenance_cost` (batteries also × `bat_maintenance_cost`), start-up cost × `gen_start_up_cost`, converter variable cost × `converter_variable_cost`, link flow cost × `transmission_cost_per_mw_km`, CO2 cost × `carbon_price`, and the loss-of-load, reserve-shortfall, curtailment, RE-shortfall and CO2-budget penalties × their `penalty_*` multipliers. |
| `build_stochastic_objective!(model, vars, input, scenarios, scenario_costs)` | Probability-weighted sum over scenarios of discounted investment cost (with the scenario's investment-cost multipliers and discount rate), existing-asset costs (`build_annual_asset_cost(vars, input, y_idx; mult)`, decommissioning scaled by the scenario) and the scenario's operating cost, plus the slack penalties. |

`StochasticMasterInput(base_input, scenarios::Vector{Scenario}, use_stochastic)` wraps the base input. `Scenario(name, probability, multipliers)` holds a `ScenarioMultipliers` with fields `invest_cost_renewables`, `invest_cost_conventional`, `fuel_cost`, `maintenance_cost`, `invest_cost_storage`, `invest_cost_transmission`, `discount_rate`, `demand_growth`, `fuel_price_growth`, `carbon_price`, `gen_fixed_cost`, `gen_decommissioning_cost`, `bat_decommissioning_cost`, `bat_invest_cost_power`, `bat_invest_cost_capacity`, `penalty_loss_of_load`, `penalty_loss_of_reserve`, `penalty_curtailment`, `penalty_fre_penetration`, `gen_start_up_cost`, `bat_maintenance_cost`, `transmission_cost_per_mw_km`, `converter_invest_cost`, `converter_variable_cost`, `penalty_co2_budget_violation`, `fuel_import_cost`, `penalty_loss_of_fuel_supply`, `fuel_storage_invest_cost`, `fuel_transport_cost`, `electrolyzer_invest_cost`, `electrolyzer_fixed_cost`, `electrolyzer_variable_cost`, `electrolyzer_water_cost` (all 1.0 by default). Build it by keyword (`ScenarioMultipliers(fuel_cost=1.2)`) or with the first ten positional values in that order.

### Benders Decomposition

| Function | Description |
|----------|-------------|
| `run_benders_decomposition(input; max_iterations=50, tolerance=1e-4, use_representative_days=true, verbose_benders=true, record_unit_operation=false, debug_lp_dir="") -> BendersResult` | Solve the master problem by Benders decomposition: an investment master with one recourse variable per year, plus one operational subproblem per year holding all its representative days, iterated until the relative gap is below `tolerance`. It reaches the monolithic optimum: the master has the monolithic master's investment constraints and costs (`build_annual_asset_cost`) and nothing else, and the subproblems have its full loss-of-load penalty, hard curtailment limit, converters, link flow cost, LP start-ups, candidates' curtailment cost and fuel block; the annual CO2 budget and each system's annual RE target and growth cap are in the year's subproblem, the cap measured from the master's share of the year before (a copy of `re_penetration_ratio`). Each year adds one optimality cut per iteration, or a feasibility cut when the master's capacities cannot meet a day's curtailment limit. The subproblems use the configured solver (HiGHS single-threaded). The result is the plan of the iteration with the best upper bound, with that iteration's operating costs, also when `max_iterations` is reached. Stochastic scenarios, TSAM periods and user constraints are not supported (the bridge solves the monolithic master instead). With `record_unit_operation`, `solution.unit_operation` holds each existing unit's operation in the returned plan's subproblems, for `calculate_unit_npv`. A master iteration stopped at a resource limit (time, iterations…) with a feasible plan is used with a warning: its subproblems are solved and its cuts added, and its lower bound is a proven one (a feasible dual point's objective, or the MIP bound of a master with integer variables) or the previous iteration's (`limited_master_lower_bound`); a master without a feasible plan ends the iterations. With a non-empty `debug_lp_dir`, the master and subproblem LPs that fail in the first five iterations are written there (the runner passes `<output>/logs` when `ESFEX_BENDERS_DEBUG_LP=1`); none are written otherwise. |
| `extract_master_solution_from_benders(model, vars, input, year_operational_costs; objective=nothing, unit_operation=UnitOperation[], re_energy=nothing) -> MasterProblemResult` | Build a `MasterProblemResult` from the Benders master, with operating costs taken from the subproblems and, when given, `objective` (the run passes its best upper bound) in place of the master's objective and `re_energy` (each system's renewable energy and demand per year in the subproblems) for the achieved RE share. |

Internal helpers (not exported): `create_benders_master(input; rep_days_by_year=benders_rep_days(input))` (returns `theta` keyed by year, each bounded below by a copper-plate relaxation of its year, `add_benders_operating_bound!`), `create_benders_year_subproblem(input, year_idx, days, fixed_caps, vars, targets; re_cap_exempt, share_years)` (a `BendersYearSubproblem`: one LP with each day of `create_benders_subproblem(input, year_idx, day_idx, demand, start_hour, fixed_caps, vars; model)` and the year's CO2 budget, RE target and growth cap) build the models; `solve_benders_subproblem!(sp; penalty=0.0)` solves a year's (or a lone day's) subproblem and returns `:optimal`, `:infeasible` (curtailment limit not met: with `penalty` > 0, `benders_curtailment_penalty(input)` in the loop, `sp` holds the LP with the limit priced at `penalty` per MWh, whose cut bounds the year at every plan; with 0, the least violation) or `:failed`; a year breaks a limit when its slack passes `curtailment_tolerance(re)` (MWh/year: 1e-3 + 1e-6 × the system's renewable generation `re`, `benders_curt_violated(sp)`); `benders_least_violation!(sp)` re-solves for the least violation (for `add_feasibility_cut!`) and `benders_solve_hard!(sp, cost)` with the limit hard (each slack held at its least violation, within the tolerance); `benders_cost_terms(model)` and `benders_curtailment_report(ysp, ratio)` give the iteration log's cost by term and why a year broke the limit; `build_year_cut` sums the days' duals into the year's cut. `benders_battery_floor(fixed_caps, input)` gives the subproblems at least `BENDERS_BATTERY_FLOOR` MW of each battery technology wherever one can be built, so their duals price a battery there. Every master quantity a subproblem reads, other than the technology and battery-technology capacities, is a copy variable keyed `(kind, name, index)`: `(:converter, "acdc"/"freq", c)`, `(:line, "", l)`, `(:reservoir, "g", b)`, `(:gen_life, "g", b)`, `(:bat_life, "bi", b)`, `(:re_energy, "s", y)` (system `s`'s renewable generation over demand in year `y`, `re_energy_ratio`, growth cap only) and the fuel keys `(:storage/:transport/:electrolyzer, …)`. `add_copy!` fixes it to `copy_value(fixed_caps, input, key)`, `extract_copy_duals(sp)` reads the duals of the fixings, and `benders_copy_expr(vars, input, year_idx, key, ctx)` is the master expression the cut multiplies them by (`ctx::BendersCopyContext` holds the line ends).

### Transmission DC

| Function | Description |
|----------|-------------|
| `TransmissionDC(network::NetworkConfig)` | Build the per-line DC network: lines (from per-line data, or from the adjacency matrix if there is none, plus transformer branches), reactances, capacities, incidence matrix, slack bus and loss model. Self-loops are dropped. |
| `build_incidence_matrix(num_buses::Int, lines::Vector{Tuple{Int,Int}})` | Bus-line incidence matrix K where K[i,ℓ] = +1 if line ℓ leaves bus i and −1 if it enters. |
| `add_dc_constraints!(model, transmission, vars, input; extra_injections_fn=nothing, expandable_lines=Set{Int}(), expansion_max=Dict{Int,Float64}())` | Per-line flow variables, KCL at each bus, and flow-angle coupling `pf_ℓ = S_base/x_ℓ · (θ_i − θ_j)` with one angle reference per connected component. Lines below 0.1 MW have their flow fixed to 0 unless their index is in `expandable_lines` (candidate corridors that investment can expand). The last loss segment of an expandable line spans the base capacity's last segment plus `expansion_max[ℓ]` MW, or is open when the line has no entry there. |
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

Types: `FuelConfig`, `FuelInfrastructureConfig`, `NonElectricDemandConfig`, `FuelRouteParams`, `TransportRoute`, `PrimaryEnergyInput`, `PrimaryEnergyVariables`, `PrimaryEnergyResult`. The dispatch builds no fuel infrastructure: `cumulative_capacities` holds what the master built. `create_primary_energy_model(model, input; storage_extra, transport_extra, cyclic)` also builds the master's day fuel model (`master_fuel.jl`: `MasterFuelInput`, `add_master_fuel_day!`, `extract_master_fuel_solution`), with the master's builds as `storage_extra(fuel, node)` / `transport_extra(fuel, route)` expressions and a cyclic tank.

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
| `set_log_callback(callback) -> CallbackLogger` | Install a `CallbackLogger` as Julia's global logger: each record goes to `callback(level, message, milestone)`, `level` as a Python logging level (10/20/30/40), `message` with its key = value pairs, `milestone` true for Benders progress (group `:benders`). Debug records only from ESFEX; `maxlog` honoured. Records logged on another thread than the installing one are queued and sent before the next record of that thread, or by `flush_log_queue()`. The Python bridge installs it after loading ESFEX. |
| `register_constraint_hook!(type, fn)` | Register a user constraint type; `fn(model, vars, input, spec)` is called for each spec with `spec["type"] == type`. Not exported (call as `ESFEX.register_constraint_hook!`). |

---

## Constraint Details

### Battery Cyclic SOC

In the master problem, each representative day starts and ends at the initial SOC, for example for candidate battery technologies:

```julia
@constraint(model, bat_tech_soc[bt, b, 0]     == en_cap * initial_soc_frac)
@constraint(model, bat_tech_soc[bt, b, hours] == en_cap * initial_soc_frac)
```

With TSAM inter-period linking, an existing battery's period starts at a free level and ends at that level plus the period's change, and an inter-day SOC (`inter_period_soc[y][(b, n, d)]`, the SOC at the start of day `d` = 0…364) runs through the year on the period of each day (`tsam_day_period`), cyclic over the year (Kotzur et al. 2018; `add_inter_day_state!`, `tsam_day_map`). Each period then counts in the storage as often as in the costs and energy balance (its weight), so a battery cannot act as a free generator across periods. Reservoirs use the same state (`inter_period_reservoir`). In the operational model, rolling windows pass SOC on through boundary conditions; see `add_battery_constraints!` above.

### Curtailment Limit

In the master problem, each system's curtailment over the year is capped at a fraction of its renewable generation over the year. Each representative day or TSAM period adds its per-system curtailment (`system_day_curtailment`), renewable output (`system_day_renewable`) and exemption to a `CurtailmentYear` with its weight in the year; after the year's days, `add_curtailment_limit!` adds one constraint per system, with an excess priced at `curtailment_excess_penalty(input)`: at least `CURTAILMENT_EXCESS_PENALTY` (10⁴ $/MWh) and 100 × (`CURTAILMENT_EXCESS_FACTOR`) the dearer of the RE shortfall price (`re_shortfall_penalty`) and `curtailment_cost`, with a stochastic scenario's multipliers, so the excess stays the last resort whatever those prices are. It returns the excess's cost, an operating cost of the year:

```julia
curt, re, exempt, smax = curtailment_year_sums(year)   # weighted, / the largest weight
for s in eachindex(curt)
    excess = @variable(model, lower_bound = 0)          # MWh/year
    @constraint(model, curt[s] <= input.max_curtailment_ratio * re[s] + exempt[s] + excess / smax)
    add_to_expression!(cost, curtailment_excess_penalty(input), excess)
end
```

`exempt` is the curtailment of existing renewables that the RE growth cap forces (`re_cap_exemption`, `re_cap_exempt_energy`), exempt from the limit. `curtailment_excess(model)` lists the system-years of a solved master whose excess passes the limit's tolerance, and `optimize_master!` warns about each (`warn_curtailment_excess`).

Benders year subproblems hold the same annual cap (`add_benders_curtailment_limits!`); their cut is built with its slack priced at the value of lost load, and a year that cannot meet it also adds a feasibility cut (see [Benders decomposition](../formulation/capacity-expansion.md#11b-benders-decomposition)). The operational model has no ratio cap; curtailment there is priced at `curtailment_cost`.

### Age-Based Retirement

Units are retired based on age exceeding lifetime:

| Unit Type | Age Formula | Active Condition |
|-----------|-------------|-----------------|
| Existing | `age = initial_age + (year_idx - 1)` | `age < lifetime` |
| Investment | `age = year_idx - investment_year` | `age < lifetime` |

Pure LP formulation: life extension of a unit past its lifetime is a continuous variable (no binaries).

### Free Generation Prevention

`build_variables!` creates `gen_output` and the battery variables only for unit-bus pairs that have capacity or a pending retirement. A generator with `rated_power[bus] = 0` therefore has no output variable at that bus, so it cannot inject free energy into the power balance. The dispatch has no investment variables: in a `development` run the master's builds reach it as existing capacity.

### PWL Transmission Losses

Piecewise linear approximation of quadratic transmission losses:

```julia
# P_loss(f) = R_pu * f^2 / S_base  approximated by N linear segments
# Marginal slope for segment k: m_k = R_pu * (2k-1) * delta_f / S_base
# where delta_f = f_max / N
```

`add_dc_constraints!` registers every lossy line-step in `model.ext[:pwl_loss_repair]` and sets `optimize_exact_losses!` as the model's optimize hook, so `optimize!(model)` returns losses that never exceed the PWL loss of each line's net flow (see [Exact losses](../formulation/dc-power-flow.md#exact-losses)). `loss_repair_bounds(model)` returns the bounds of the last such solve as a `Dict{String, Float64}` (`nothing` without PWL losses, or when the first solve was not optimal): `lower_bound` (the first, PWL, solve), `objective` (the repaired one), `gap`, `rounds`, `linearised` and `released`, in model units (M\$); see [Bounds of the repair](../formulation/dc-power-flow.md#bounds-of-the-repair).
