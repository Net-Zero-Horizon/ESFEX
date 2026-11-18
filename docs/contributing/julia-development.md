# Julia Development

## Module Structure

The Julia code lives in `src/esfex/julia/`:

```
src/esfex/julia/
├── Project.toml              # Dependencies
├── Manifest.toml             # Lockfile
├── build_sysimage.jl         # Used by `esfex precompile`
├── precompile_workload.jl    # Workload traced into the sysimage
├── test/                     # runtests.jl + test Project.toml
└── src/
    ├── ESFEX.jl              # Module entry point, solver factory, exports
    ├── types.jl              # Input, variable and result structs
    ├── user_constraints.jl   # Custom-constraint hooks (register_constraint_hook!)
    ├── transmission_dc.jl    # DC power flow
    ├── transmission_ac.jl    # AC power flow (Newton-Raphson), Y-bus
    ├── transmission_acopf.jl # ACOPF formulations (SOC, QC, SDP, NLP)
    ├── power_system.jl       # Operational dispatch / unit commitment
    ├── primary_energy.jl     # Fuel supply chain
    ├── master_problem.jl     # Capacity expansion (single, multi-system, stochastic)
    ├── benders.jl            # Benders decomposition of the master problem
    ├── mga.jl                # MGA / SPORES near-optimal alternatives
    ├── electrolyzer.jl       # Electrolyzers
    ├── dcopf_benchmark.jl    # Standalone DCOPF benchmark (solve_dcopf)
    └── acopf_benchmark.jl    # Standalone ACOPF benchmark (solve_acopf)
```

### Module Entry Point

```julia
# ESFEX.jl (abridged)
module ESFEX
    using JuMP
    using JuMP: VariableRef, AffExpr
    using HiGHS
    using Graphs
    using LinearAlgebra
    using SparseArrays
    using Statistics: mean
    import MathOptInterface as MOI

    include("types.jl")
    include("user_constraints.jl")
    include("transmission_dc.jl")
    include("transmission_ac.jl")
    include("transmission_acopf.jl")
    include("power_system.jl")
    include("primary_energy.jl")
    include("master_problem.jl")
    include("benders.jl")
    include("mga.jl")
    include("electrolyzer.jl")
    include("dcopf_benchmark.jl")
    include("acopf_benchmark.jl")

    export create_power_system, create_master_problem
    export create_optimizer, diagnose_infeasibility
    export extract_solution, export_solution_to_dict
    # ... all public API
end
```

Solvers other than HiGHS are loaded lazily by `create_optimizer` (`_load_solver_module`), from ESFEX's Julia project or the optional-solver environment.


---


## Type System

Input data flows through strongly-typed structs in `types.jl`:

### Core Types

`GeneratorConfig` has about 40 positional fields. Abridged:

```julia
struct GeneratorConfig
    name::String
    type::String                     # "Renewable" or "Non-renewable"
    fuel::String
    rated_power::Vector{Float64}     # per bus
    min_power::Vector{Float64}
    efficiency_rated::Vector{Float64}
    efficiency_min::Vector{Float64}
    ramp_up::Vector{Float64}         # fraction of capacity per hour; <= 0 = no limit
    ramp_down::Vector{Float64}
    min_up_time::Vector{Float64}     # hours
    min_down_time::Vector{Float64}
    start_up_cost::Vector{Float64}
    fuel_cost::Vector{Float64}
    fixed_cost::Vector{Float64}       # $/MW of power in service per year
    maintenance_cost::Vector{Float64}
    inertia::Vector{Float64}
    invest_cost::Vector{Float64}      # prices the master's life extension
    availability::Matrix{Float64}    # (hours x buses)
    reservable::Bool
    life_time::Vector{Float64}
    initial_age::Vector{Float64}
    degradation_rate::Vector{Float64}
    decommissioning_cost::Vector{Float64}
    frequency_hz::Float64
    current_type::String             # "AC" or "DC"
    # reservoir_* fields (hydro), risk_coefficient,
    # reservoir_min_release, cascade_downstream, cascade_delay_hours,
    # reservoir_head_min_factor
    num_units::Vector{Int}           # identical units per bus (clustered UC)
end
```

See `types.jl` for the full definitions; the Python side builds these in `bridge/adapters.py`.

### Convention: Per-Bus Arrays

Most generator and battery parameters are `Vector{Float64}` with one entry per bus of the network the model is built on. The Python side expands per-node configuration onto buses before calling Julia. Variables are created only for active unit-bus pairs; `vars.buses_of_gen[g]` and `vars.gens_at_bus[b]` (and the battery equivalents) list them.

### Type System Conventions

Conventions for new types in `types.jl`:

**Immutable by default.** Use `struct` (not `mutable struct`) for input data. Immutability prevents accidental modification and enables compiler optimizations.

```julia
# Correct: immutable input data
struct MyConfig
    limit::Float64
    enabled::Bool
end

# Only use mutable when truly needed (e.g., accumulating results)
mutable struct MyResult
    total_cost::Float64
    iterations::Int
end
```

**Parametric types for flexibility.** When a field could be `Int` or `Float64` depending on the caller:

```julia
struct Threshold{T<:Real}
    value::T
    penalty::Float64
end
```

**Top-level input structs.** `PowerSystemInput` and `MasterProblemInput` hold the unit vectors and network as sub-structs (`Vector{GeneratorConfig}`, `NetworkConfig`, `TemporalConfig`) and most scalar settings as flat fields, for example `co2_budget::Float64`, `co2_cost::Float64`, `loss_of_load_penalty::Float64`, `mode::String`, `solver_name::String`. Small grouping structs exist (`PenaltyConfig`, `TargetConfig`, `SolverSettings`) but the dispatch input does not nest them.

**Naming conventions for types:**

| Kind | Convention | Example |
|------|-----------|---------|
| Input config | `XxxConfig` | `GeneratorConfig`, `PenaltyConfig` |
| Model variables | `XxxVariables` | `PowerSystemVariables` |
| Solution output | `XxxResult` | `PowerSystemResult`, `MGAResult` |
| Technology specs | `XxxTechnologyConfig` | `TechnologyConfig`, `BatteryTechnologyConfig` |

**Keyword constructors.** Large input structs get an outer keyword constructor with defaults, so callers set only what they need (from `test/runtests.jl`):

```julia
input = PowerSystemInput(
    name = "ed", year = 2025, network = net, generators = [gen],
    batteries = BatteryConfig[], demand = reshape([50.0, 60.0], H, 1),
    temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
    mode = "economic_dispatch", solver_name = "highs", verbose = false)
```

When you add a field, add it to the struct, the keyword constructor (with a default) and the positional call inside it.

### Adding a New Type

1. Define the struct in `types.jl`
2. Add it to the relevant input struct (`PowerSystemInput` or `MasterProblemInput`) and its keyword constructor
3. Update the Python adapter in `adapters.py` to construct the Julia type
4. Add constraints in the relevant `.jl` file


---


## Constraint Development

### Pattern: Adding a New Constraint

```julia
function add_my_constraints!(model::Model, vars::PowerSystemVariables, input::PowerSystemInput)
    hours = input.temporal.hours
    n_bus = input.network.num_buses

    for t in 1:hours, b in 1:n_bus
        @constraint(model,
            vars.my_variable[b, t] <= input.my_parameter[b],
            base_name = "my_constraint_b$(b)_t$(t)"
        )
    end
end
```

### Naming Conventions for Constraints

Use descriptive base names that include indices:

```julia
# Good
@constraint(model, ..., base_name = "gen_capacity_g$(g)_b$(b)_t$(t)")
@constraint(model, ..., base_name = "ramp_up_seam_g$(g)_b$(b)")

# Avoid
@constraint(model, ..., base_name = "c1")
```

### Handling Inactive Units

Generation and battery variables are `SparseAxisArray`s that exist only for active unit-bus pairs (capacity at the bus, or a pending retirement). Iterate over those pairs instead of over every bus:

```julia
for g in 1:length(input.generators), b in vars.buses_of_gen[g], t in 1:hours
    add_to_expression!(expr, 1.0, vars.gen_output[g, b, t])
end
```

Indexing `vars.gen_output[g, b, t]` for an inactive pair throws a `KeyError`. Variables that are created must be bounded; an unbounded variable that enters the balance creates free energy.

### Conditional Constraints

```julia
# Only in unit commitment mode
if input.mode == "unit_commitment"
    # Binary commitment variables and min up/down time
end

# Only for the CO2 budget groups that have a budget (add_co2_constraint!):
# one per system in a multi-system run, else one from input.co2_budget
for grp in co2_budget_groups(input)
    has_co2_budget(grp) || continue
    # budget scaled to the window: grp.budget * hours * tres / hours_per_year
end

# Optional variable containers are `nothing` when unused
if vars.ev_charging !== nothing
    # EV constraints
end
```

### Constraint Implementation Patterns

#### End-of-Horizon Storage State

Check the existing conventions before adding a storage element:

- Batteries are not cyclic per dispatch window. State of charge carries over between rolling-horizon windows in MWh (`battery_soc_prev`). The last window of each year has a soft floor that brings the SOC back to at least its year-start level; `cyclic_end_soc` forces an exact cycle for single full-year solves.
- Reservoirs and the EV fleet are cyclic within each window (`ev_soc_cyclic_n…`).
- The master problem makes battery SOC cyclic over each representative day; with TSAM seasonal linking, SOC is carried across periods and cyclic over the year.

#### Linking Constraints Across Time Steps

Ramp constraints link consecutive time steps. In `add_generator_constraints!` the ramp rate is a fraction of capacity per hour, scaled by the step length and capped at full capacity; a rate <= 0 means no limit. The first step is linked to the previous window's output (`gen_output_prev`). Batteries do the same for their charge and their discharge (`add_storage_ramp_constraints!`, from `battery_charge_prev` / `battery_discharge_prev`), without the cap: a rate × step of 1 or more sets no limit.

```julia
tres = input.temporal.resolution_hours
ramp_up_limit = total_capacity * min(1.0, gen.ramp_up[b] * tres)

for t in 2:hours
    gen.ramp_up[b] > 0 && @constraint(model,
        vars.gen_output[g, b, t] - vars.gen_output[g, b, t-1] <= ramp_up_limit)
end
```

#### Big-M Constraints

Use Big-M formulation for conditionally active constraints (e.g., minimum power output only when a generator is on):

```julia
# gen_status is binary (1 = on, 0 = off)
rated = gen.rated_power[b]
min_pwr = gen.min_power[b]

for t in 1:hours
    # Output <= rated * status  (off => output = 0)
    @constraint(model,
        vars.gen_output[g, b, t] <= rated * vars.gen_status[g, b, t],
        base_name = "gen_max_g$(g)_b$(b)_t$(t)"
    )

    # Output >= min_power * status  (on => output >= min_power)
    @constraint(model,
        vars.gen_output[g, b, t] >= min_pwr * vars.gen_status[g, b, t],
        base_name = "gen_min_g$(g)_b$(b)_t$(t)"
    )
end
```

#### Network Constraints (Self-Loop Prevention)

Flows are one variable vector per physical branch, `vars.power_flow_by_line[ℓ][t]` (lines, then transformers, each stored low → high bus). `vars.pair_lines::Dict{(i, j), Vector{Int}}` maps a bus pair `i < j` to all of its branches, so parallel lines are summed wherever a pair flow is meant: `pair_flow(vars, i, j, t)` in custom constraints, and the `power_flow` result. Never create a self-loop branch `(i, i)`: it would appear as incoming power at bus `i` without a matching outflow, creating energy from nothing.

#### Age-Based Retirement

The master problem retires units by age without binary variables: an existing unit is active while `initial_age + (year_idx - 1) < life_time`, and an investment made in year `y` is active for `life_time` years after it.


---


## Variable Conventions

### Standard Variable Dimensions

Fields of `PowerSystemVariables` (`types.jl`):

| Variable | Indices | Container |
|----------|---------|-----------|
| `gen_output[g, b, t]` | generator, bus, time | `SparseAxisArray` (active pairs only) |
| `gen_status`, `gen_startup` | generator, bus, time | `SparseAxisArray` over the committed pairs only (`committed_buses`: no variable renewable, see `is_variable_renewable`); `nothing` outside UC |
| `gen_shutdown` | — | Always `nothing` (field kept for compatibility; no shutdown variable is built) |
| `bat_charge`, `bat_discharge`, `bat_soc` | battery, bus, time | `SparseAxisArray` |
| `bat_spillage`, `soc_violation` | battery, bus, time | `nothing` when not needed |
| `curtailment[n, t]` | node, time | `Array` |
| `load_shed[b, t]` | bus, time | `Array` |
| `reserve_static`, `reserve_dynamic` (+ `_loss`) | node, time | `Array` |
| `power_flow_by_line[ℓ][t]` | branch, time | `Vector{Vector{VariableRef}}` (`nothing` without DC flows) |
| `pair_lines[(i, j)]` | bus pair `i < j` | `Dict{Tuple{Int,Int}, Vector{Int}}`: indices into `power_flow_by_line` |
| `voltage_angle[n, t]` | node, time | `Array` |
| `ev_charging`, `ev_v2g`, `ev_loss` | node, time | `Array` or `nothing` |
| `ev_soc` | node, time + 1 | `Array` or `nothing` |
| `electrolyzer_power` | electrolyzer, bus, time | `Array` or `nothing` |
| `balance_constraints[(b, t)]` | bus, time | power-balance constraint references |

Check the variable's construction in `build_variables!` before indexing: some arrays are per node, others per bus.


---


## Objective Function

`build_objective!` in `power_system.jl` minimizes total cost. Energy terms are scaled by `resolution_hours`. Components, in code order:

1. Fuel costs (flat or piecewise-linear cost curve) and maintenance, per MWh
2. Battery maintenance and throughput degradation costs
3. Start-up costs (UC mode)
4. Load-shedding penalty (VOLL, with sectoral multipliers when sectoral demand is used)
5. Curtailment penalty
6. Static and dynamic reserve shortage penalties
7. CO2 cost (fuel emission factor × output × `co2_cost`)
8. Inertia shortfall, EV unmet-driving (`ev_loss`) and RE-target slack penalties
9. SOC-violation and transfer-margin penalties
10. Fixed O&M of the generators and batteries in service (`fixed_om_annual_cost`, $/MW·yr), prorated to the window with the other annual terms

The dispatch has no investment costs: it builds nothing (the master's builds reach it as existing capacity).

V2G compensation is reported but is not part of the objective. Primary-energy and electrolyzer terms are added when those models are coupled in.


---


## Solution Extraction

`extract_solution(model, vars, input)` returns a `PowerSystemResult` with dense arrays (`gen_output::Array{Float64,3}`, `bat_soc`, `load_shed`, `power_flow`, …), totals (`objective`, `total_generation`, `re_penetration`, `total_co2`) and `energy_prices`. Prices are the duals of `vars.balance_constraints[(b, t)]` divided by the step length. In UC mode the MIP has no duals; `recover_uc_duals_via_copy` re-solves a copy as an LP with the commitment fixed to obtain them.

```julia
model, vars = create_power_system(input)
optimize!(model)
res = extract_solution(model, vars, input)
res.gen_output        # [gen x bus x hour]
res.energy_prices     # [bus x hour]
```


---


## Performance Tips

### Memory Allocation

- Pre-allocate arrays instead of growing them
- Use views (`@view`) for array slices passed to functions
- Avoid creating temporary arrays in hot loops

```julia
# Good: Pre-allocated
output = zeros(num_generators, num_nodes, hours)
for g in 1:num_generators
    output[g, :, :] .= value.(vars.gen_output[g, :, :])
end

# Avoid: Temporary allocations
for g in 1:num_generators
    output = vcat(output, value.(vars.gen_output[g, :, :]))  # Allocates new array
end
```

### Constraint Generation

- Use `@constraints` macro for batch constraint creation when possible
- Avoid string interpolation in tight loops (use `base_name` sparingly for very large models)
- For models with thousands of constraints, consider dropping `base_name` entirely and relying on constraint indexing

```julia
# Fast: No string allocation per constraint
for t in 1:hours, n in 1:num_nodes
    @constraint(model, vars.gen_output[g, n, t] <= rated)
end

# Slower but debuggable: Named constraints
for t in 1:hours, n in 1:num_nodes
    @constraint(model, vars.gen_output[g, n, t] <= rated,
                base_name = "gen_cap_g$(g)_n$(n)_t$(t)")
end
```

Named constraints aid debugging; remove them for production runs with large models.

### Solver Configuration

```julia
create_optimizer(; solver_name="highs", threads=4, time_limit=3600.0,
                 gap=0.01, verbose=false, solver_options=Dict{String,Any}())
```

`create_optimizer` dispatches on `solver_name` (highs, gurobi, cplex, xpress, scip, cbc, glpk, ipopt, clarabel, scs) and maps threads, time limit, gap and verbosity to each solver's own attribute names. A `time_limit` of 0 (or less, or `Inf`) sets no time limit: the attribute is left out (`has_time_limit`); a limit ESFEX sets below `solver.time_limit` (the Benders master and subproblems) uses `capped_time_limit`, which keeps the cap when `solver.time_limit` is 0. For HiGHS it sets `threads`, `time_limit`, `mip_rel_gap` and `output_flag`, then applies `solver_options` on top; it sets no presolve or parallel options. Pinning `"solver" => "simplex"` also turns crossover off.


---


## Debugging

### Infeasibility Diagnosis

For the master problem, `diagnose_infeasibility(model, vars::MasterProblemVariables, input::MasterProblemInput)` reports the values of the slack variables (RE target, budget, capacity) by category as a `Dict{String,Float64}`. It does not compute an IIS.

For an IIS, use a solver that supports it. With Gurobi, setting `ESFEX_DUMP_IIS=1` makes the Python adapter write the first infeasible model as an LP file to `ESFEX_IIS_DIR` and log the constraints `compute_conflict!` finds. The runner also writes `window_infeasible_y<year>_w<window>.lp` to `<output>/logs/` when a dispatch window is not optimal. `master_conflict(model; limit=20)` returns the constraints of an infeasible master's conflict as strings (empty when the solver cannot compute one); the myopic runner logs them when a year's master is infeasible. With `ESFEX_BENDERS_DEBUG_LP=1`, Benders writes the master and subproblem LPs that fail to `<output>/logs/` (`run_benders_decomposition(...; debug_lp_dir)`).

### Model Statistics

```julia
println("Variables: ", num_variables(model))
println("Constraints: ", num_constraints(model))
println("Objective sense: ", objective_sense(model))
```

### Exporting the Model

```julia
# Write to MPS format for external debugging
write_to_file(model, "debug_model.mps")

# Write to LP format (human-readable)
write_to_file(model, "debug_model.lp")
```

LP format is human-readable for small models. For larger models (10,000+ constraints), use MPS format and inspect with a solver CLI tool.

### Inspecting Variable Values

```julia
# Single variable
println("gen_output[1,1,1] = ", value(vars.gen_output[1, 1, 1]))

# Full slice for one generator at one node
for t in 1:hours
    println("t=$t: gen=$(value(vars.gen_output[1, 1, t]))")
end

# Check dual values of the power balance
if has_duals(model)
    for ((b, t), con) in vars.balance_constraints
        price = dual(con)
        abs(price) > 1e-6 && println("Bus $b, t=$t: dual = $price")
    end
end
```

### Common Debugging Scenarios

**Suspiciously low objective value.** Check for unconstrained variables created but never bounded:

```julia
# After solve
for g in 1:length(input.generators), b in vars.buses_of_gen[g], t in 1:hours
    val = value(vars.gen_output[g, b, t])
    if val > 1e-6 && input.generators[g].rated_power[b] <= 0 && input.mode != "development"
        @warn "Generator $g producing $(val) MW at bus $b with zero rated power"
    end
end
```

**All duals are zero.** Indicates free energy entering the system. Check for self-loop flow entries or unbounded variables in the balance.

**Solver stalls.** Check penalty coefficients. A penalty of 600,000,000 (instead of 600) causes the solver to spend excessive time exploring near-penalty solutions.


---


## Testing Julia Code

### Running Tests

```bash
julia --project=src/esfex/julia -e 'using Pkg; Pkg.test()'
```

The test dependencies (Ipopt, JuMP, Test) are in `src/esfex/julia/test/Project.toml`. CI runs this suite on Julia 1.11.

### Writing Tests

`test/runtests.jl` is a single file with one `@testset "ESFEX"`. Unit tests get one testset per function (`build_ybus`, `create_optimizer`, …). Model tests go in the `integration` block, which builds small inputs directly; its local `mkgen(name, type, fuel, rated, fcost, avail; nb, H)` helper fills the positional `GeneratorConfig` fields:

```julia
@testset "economic dispatch (single bus)" begin
    H = 2
    gen = mkgen("GasCC", "Non-renewable", "Gas", [100.0],
                [3.0e-5], ones(H, 1); nb = 1, H = H)
    # ... bus, NetworkConfig, PowerSystemInput(...; mode = "economic_dispatch")
    model, vars = create_power_system(input)
    optimize!(model)
    @test string(termination_status(model)) == "OPTIMAL"
    res = extract_solution(model, vars, input)
    @test res.total_generation ≈ 110.0 atol = 1e-6
end
```

There is no `create_test_input` helper and no per-file test layout. End-to-end runs through the Python bridge live in the `julia`-marked pytest tests (see [Testing](testing.md)).

### Testing Constraints in Isolation

Build a minimal model to verify the feasible region of a single constraint:

```julia
@testset "Cyclic SOC Constraint" begin
    model = Model(HiGHS.Optimizer)
    set_silent(model)

    hours = 24
    initial_soc = 50.0
    capacity = 100.0

    @variable(model, 0 <= soc[t=1:hours] <= capacity)
    @variable(model, -20 <= charge[t=1:hours] <= 20)

    # SOC dynamics
    @constraint(model, soc[1] == initial_soc + charge[1])
    for t in 2:hours
        @constraint(model, soc[t] == soc[t-1] + charge[t])
    end

    # Cyclic constraint
    @constraint(model, soc[hours] == initial_soc)

    # Trivial objective
    @objective(model, Min, 0)
    optimize!(model)

    @test termination_status(model) == MOI.OPTIMAL
    @test isapprox(value(soc[hours]), initial_soc, atol=1e-6)

    # Verify that total charge over the cycle is zero
    total_charge = sum(value.(charge))
    @test isapprox(total_charge, 0.0, atol=1e-6)
end
```


---


## Python-Julia Bridge

### How Python Calls Julia

The adapter converts Python data structures to Julia types:

`PowerSystemAdapter` (and the master, MGA, electrolyzer and primary-energy adapters) in `bridge/adapters.py` build the Julia structs from the Pydantic config and call the Julia entry points. Schematically:

```python
# In adapters.py (schematic)
ESFEX = get_esfex_module()

gen_config = ESFEX.GeneratorConfig(      # positional fields, in struct order
    name, gen_type, fuel, rated_power, min_power, ...)

jl_input = ESFEX.PowerSystemInput(       # keyword constructor
    name=..., year=..., network=net, generators=gens, ...)

model, vars = ESFEX.create_power_system(jl_input, custom_constraints=specs)
```

### Bridge Type Mapping

| Python Type | Julia Type | Notes |
|------------|-----------|-------|
| `float` | `Float64` | Direct mapping |
| `int` | `Int64` | Direct mapping |
| `bool` | `Bool` | Direct mapping |
| `str` | `String` | Direct mapping |
| `list[float]` | `Vector{Float64}` | Wrap with `jl.Vector[jl.Float64](...)` |
| `list[int]` | `Vector{Int}` | Wrap with `jl.Vector[jl.Int](...)` |
| `np.ndarray` (2D) | `Matrix{Float64}` | Automatic conversion via juliacall |
| `None` | `nothing` | Maps to Julia `Nothing` |

### Adding a New Feature

Steps to add a new optimization feature:

1. **Julia side** (`types.jl`): Add new fields to input structs
2. **Julia side** (relevant `.jl`): Add constraint/variable/objective code
3. **Python side** (`adapters.py`): Add field mapping in adapter
4. **Python side** (`schema.py`): Add Pydantic field for configuration
5. **Python side** (`runner.py`): Wire the new feature into the orchestration loop
6. **Tests**: Add Julia and Python tests
7. **Docs**: Update formulation and API documentation

### Illustrative Example: Adding a Hydrogen Storage Constraint

The files touched when adding a new constraint (hypothetical; ESFEX has no hydrogen storage model):

**Step 1 --- Julia type** (`types.jl`):

```julia
struct HydrogenStorageConfig
    capacity_kg::Vector{Float64}     # Per node
    charge_rate_kg::Vector{Float64}
    discharge_rate_kg::Vector{Float64}
    initial_level_kg::Vector{Float64}
    cost_per_kg::Float64
end
```

**Step 2 --- Julia constraints** (`electrolyzer.jl`):

```julia
function add_h2_storage_constraints!(model, vars, input)
    h2 = input.h2_storage
    hours = input.temporal.hours
    num_nodes = input.network.num_nodes

    for n in 1:num_nodes
        h2.capacity_kg[n] <= 0 && continue

        for t in 1:hours
            @constraint(model, vars.h2_level[n, t] <= h2.capacity_kg[n])
            @constraint(model, vars.h2_charge[n, t] <= h2.charge_rate_kg[n])
        end

        # Cyclic constraint
        @constraint(model, vars.h2_level[n, hours] == h2.initial_level_kg[n])
    end
end
```

**Step 3 --- Python adapter** (`adapters.py`):

```python
h2_config = jl.HydrogenStorageConfig(
    capacity_kg=jl.Vector[jl.Float64](sys.h2_storage.capacity_kg),
    charge_rate_kg=jl.Vector[jl.Float64](sys.h2_storage.charge_rate_kg),
    discharge_rate_kg=jl.Vector[jl.Float64](sys.h2_storage.discharge_rate_kg),
    initial_level_kg=jl.Vector[jl.Float64](sys.h2_storage.initial_level_kg),
    cost_per_kg=sys.h2_storage.cost_per_kg,
)
```

**Step 4 --- Python schema** (`schema.py`):

```python
class HydrogenStorageConfig(BaseModel):
    capacity_kg: list[float] = []
    charge_rate_kg: list[float] = []
    discharge_rate_kg: list[float] = []
    initial_level_kg: list[float] = []
    cost_per_kg: float = 5.0
```

**Step 5 --- Tests**: Write both Julia (`@testset`) and Python (`pytest`) tests validating the constraint behavior.
