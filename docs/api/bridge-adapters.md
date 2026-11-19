# Bridge Adapters

Module: `esfex.bridge.adapters`

## Data Flow Overview

```
Python config (Pydantic)
    --> adapter.__init__() stores configuration
    --> adapter._create_input() converts to Julia InputData struct
    --> Julia solver builds JuMP model, solves, extracts solution
    --> adapter.get_solution_values() converts Julia result to Python dict
```

For operational dispatch:

```
ESFEXConfig / SystemConfig
    --> PowerSystemAdapter._create_input()
        --> convert_generator_config() per generator
        --> convert_battery_config() per battery
        --> convert_network_config() for DC power flow topology
        --> ESFEX.PowerSystemInput(...) Julia struct
    --> ESFEX.create_power_system(input; custom_constraints) --> (model, vars)  [JuMP [20]](../reference/bibliography.md#ref20)
    --> JuMP.optimize!(model)
    --> ESFEX.extract_solution(model, vars, input) --> PowerSystemResult
    --> convert_power_system_result() --> Python dict
```

For capacity expansion:

```
ESFEXConfig / SystemConfig + years + demand
    --> MasterProblemAdapter._create_input()
        --> convert generators, batteries, technologies, battery_technologies
        --> convert_network_config() for transmission
        --> ESFEX.MasterProblemInput(...) Julia struct
    --> ESFEX.create_master_problem(input; use_representative_days, custom_constraints) --> (model, vars, targets)
        (or ESFEX.create_stochastic_master_problem(StochasticMasterInput) when stochastic scenarios are set)
    --> JuMP.optimize!(model)
    --> ESFEX.extract_master_solution(...) --> MasterProblemResult
    --> Python dict with investment/retirement decisions per year
```

With `solver_method="benders"`, `build_model()` only creates the input and `solve()` calls `ESFEX.run_benders_decomposition(input; ...)`, which builds its own investment master and one dispatch subproblem per year (all its representative days).

---

## PowerSystemAdapter

Wraps the operational dispatch model (`power_system.jl`). Solves short-horizon economic dispatch or unit commitment problems.

```python
class PowerSystemAdapter:
    def __init__(
        self,
        config: Union[ESFEXConfig, SystemConfig],
        demand: np.ndarray,              # (hours x nodes) in MW
        hours: int,
        num_nodes: int,
        year: int,
        base_year: int,
        mode: str = "development",
        availability_cache: Optional[Dict[str, np.ndarray]] = None,
        inflow_cache: Optional[Dict[str, np.ndarray]] = None,
        start_hour: int = 0,
        run_mode: Optional[str] = None,
        **kwargs
    ) -> None
```

**Key Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `config` | `ESFEXConfig` or `SystemConfig` | System configuration. If `ESFEXConfig`, temporal and solver settings are extracted automatically. |
| `demand` | `np.ndarray` | Demand array for the current window, shape `(hours, num_nodes)` in MW. |
| `hours` | `int` | Number of timesteps in this window. |
| `num_nodes` | `int` | Number of geographic nodes. |
| `year` | `int` | Current simulation year (used for age-based retirement). |
| `base_year` | `int` | Base year for investment age calculations. |
| `mode` | `str` | Dispatch formulation: `"economic_dispatch"` (LP) or `"unit_commitment"` (MIP with binary commitment variables); anything else is solved as economic dispatch. A development run passes one of the two (`master_problem.use_uc_in_dispatch`). |
| `availability_cache` | `dict` | Pre-loaded availability profiles keyed by generator config key. Each value is a full-year NumPy array of shape `(8760, nodes)` with values in [0, 1]. Avoids repeated file I/O when solving multiple windows. |
| `inflow_cache` | `dict` | Pre-loaded reservoir inflow profiles keyed by generator config key. Same format as `availability_cache`. |
| `start_hour` | `int` | Start hour within the year for slicing availability and inflow profiles to the current window. |
| `run_mode` | `str` | The run's `simulation_mode` (`"development"`, `"unit_commitment"`, `"economic_dispatch"`), checked against `n1_security.apply_to_modes`. Defaults to `config.simulation_mode` (to `mode` with a `SystemConfig`). |

**Keyword Arguments (via `**kwargs`):**

| Key | Type | Description |
|-----|------|-------------|
| `system_config` | `SystemConfig` | Overrides the system taken from `config`. The runner passes the merged (multi-system, zone-expanded) system here. |
| `config_path` | `str` or `Path` | The configuration file. A profile missing from `availability_cache` is read relative to its directory (current directory when absent). |
| `units_config` | `dict` | Updated unit capacities from MasterProblem investments. Keys are generator/battery config keys, values are dicts with `rated_power`, `capacity`, etc. Virtual generators/batteries created by `_build_config_from_cumulative()` are also included here. |
| `boundary_conditions` | `dict` | State carried over from the previous rolling window, each a `(unit, bus)` array: `gen_status_init`, `gen_output_prev`, `reservoir_level`, `battery_soc` (MWh), `battery_discharge_prev` and `battery_charge_prev` (MW, for the batteries' ramp limits). On the year's last window also `battery_soc_end_min` (MWh floor) and `battery_soc_end_cyclic` (bool). |
| `sectoral_demand` | `dict` | Sectoral demand arrays `{sector_name: np.ndarray(hours, nodes)}`. |
| `rooftop_generation` | `np.ndarray` | Rooftop solar generation profile `(hours, nodes)` in MW. The runner passes `None`: rooftop output is already subtracted from the demand it sends. |
| `re_penetration_target` | `float` | The year's RE penetration target (0-1) from the development master's path. `None` or 0: no RE-penetration constraint (`target_re_penetration` is a planning target and is not applied by the dispatch on its own). |
| `ev_config_data` | `dict` | EV fleet configuration for V2G constraints. |
| `electricity_price` | `list[float]` | Time-varying electricity prices for V2G compensation. |

**Methods:**

### build_model

```python
def build_model(self, external_model=None) -> None
```

Build the JuMP optimization model. Calls `_create_input()` to convert Python config to a Julia `PowerSystemInput` struct, then calls `ESFEX.create_power_system(input)` to construct the JuMP model.

### _create_input

```python
def _create_input(self) -> Any  # Julia PowerSystemInput
```

Converts Python configuration into a Julia `PowerSystemInput` struct. Steps:

1. Iterates over `sys.generators` and converts each to a Julia `GeneratorConfig` via `convert_generator_config()`.
2. Resolves availability profiles from `availability_cache` (slicing to the current window using `start_hour`). A unit missing from the cache is read from its file with the runner's reader (`esfex.io.demand.load_run_availability`: path relative to `config_path`, hourly, Feb 29 dropped by the calendar of `date_start`) and sliced the same way; a missing file is an error.
3. Applies `units_config` overrides to update `rated_power` for generators whose capacity was modified by MasterProblem investments.
4. Creates virtual generators for technology investments that do not exist in `sys.generators` (e.g., "Investment Solar PV"). Their arrays are per bus of the system (`_is_per_bus`); with network reduction they are laid out on the reduced buses, and capacity on an eliminated bus moves to the retained bus of the same node with the largest `demand_fraction`.
5. Repeats the above for batteries and virtual batteries.
6. Applies geographic fuel transport cost adjustments based on generator-to-fuel-storage distance.
7. Builds network, temporal, penalty, and solver configuration Julia structs.
8. Constructs the final `ESFEX.PowerSystemInput(...)` with all fields.

### solve

```python
def solve(self) -> int
```

Solve the optimization model. Returns a PuLP-compatible status code:

| Return Value | Meaning |
|-------------|---------|
| `1` | Optimal solution found, or a feasible solution held when the solver stopped at a limit (time, nodes, iterations…; logged as a WARNING with the gap) |
| `0` | Not solved |
| `-1` | Infeasible |
| `-2` | Unbounded |

### conflict

```python
def conflict(self, limit: int = 20) -> list[str]
```

After an infeasible solve, the constraints and bounds that cannot hold together (the solver's irreducible infeasible subsystem), at most `limit` of them. Empty when the solver cannot compute one (HiGHS and Gurobi can) or under Benders. The myopic runner logs them when a year's master is infeasible.

### get_solution_values

```python
def get_solution_values(self, keep_steps: int | None = None,
                        primary_energy: PrimaryEnergyAdapter | None = None) -> dict
```

Extract all variable values from the solved model. Calls `ESFEX.extract_solution()` on the Julia side and converts the result with `esfex.bridge.converters.convert_power_system_result()` to a Python dictionary with NumPy arrays, indexed by bus. Costs are converted back from M$ to $.

`keep_steps` (rolling horizon) restricts `objective`, the scalar totals and `cost_breakdown` to the window's first `keep_steps` steps, the ones the runner keeps; the arrays still span the whole window. Pass the window's `primary_energy` adapter so its costs are split by step too (see [Rolling Horizon — Result Stitching](../formulation/operational-dispatch.md#104-result-stitching)). Main keys:

| Key | Content |
|-----|---------|
| `status`, `objective`, `solve_time` | Termination status, objective ($), solve time (s) |
| `gen_output` | Generation, `(gen, bus, hour)` MW |
| `gen_status`, `gen_startup` | Commitment status and start-ups (UC mode only) |
| `bat_charge`, `bat_discharge`, `bat_soc` | Battery dispatch, `(bat, bus, hour)` |
| `curtailment`, `load_shed` | `(bus, hour)` MW |
| `reserve_static`, `reserve_dynamic`, `loss_of_reserve_static`, `loss_of_reserve_dynamic` | Reserves and their shortfalls |
| `co2_emissions` | `(bus, hour)` tonnes |
| `voltage_angle` | `(bus, hour)` rad |
| `voltage_magnitude`, `reactive_generation` | ACOPF modes only, otherwise `None` |
| `energy_prices` | Balance-constraint duals, `(bus, hour)` USD/MWh (recovered by a fixed-commitment LP re-solve in UC mode) |
| `power_flow`, `power_flow_by_line` | Flow per bus pair `{(i, j): array}` and per physical line |
| `line_losses` | `(bus, hour)` MW: transmission losses of the DC loss model, half of each line's loss at each end bus (present only with a loss model) |
| `bat_spillage`, `ev_*`, `loss_of_inertia`, `transfer_margin`, `reservoir_*` | Present when the feature is active |
| `n1_gen_reserve_duals`, `n1_trans_reserve_duals`, `n1_binding_contingencies`, `n1_security_cost` | N-1 security outputs |
| `unattached_load_shed` | `(node, hour)` MW: the demand of nodes with no load bus, unserved (present only when such nodes exist); it is included in `load_shed_total`, `objective` and `cost_breakdown` |
| `total_generation`, `total_demand`, `total_losses`, `re_penetration`, `total_co2`, `load_shed_total`, `total_curtailment` | Scalars (MWh, tCO2). `total_losses` is the transmission loss of the DC loss model (0 when the flow is lossless or AC) |

When network reduction is enabled, the arrays are expanded back to the original buses and lines before they are returned.

When cost decomposition is available, the result dictionary also includes a `cost_breakdown` key — a dict with 35 cost component fields (fuel, O&M, CO2, investment, primary energy, N-1 shortfall, etc.) plus `total`. See [HDF5 Output Schema — cost_breakdown](../reference/hdf5-output-schema.md#cost_breakdown) for the field list.

### get_objective_value

```python
def get_objective_value(self) -> float
```

Return the objective function value (total system cost for the window in $).

### write_lp

```python
def write_lp(self, filepath: str) -> None
```

Export the JuMP model to LP format for debugging. Requires `build_model()` to have been called first.

---

## MasterProblemAdapter

Wraps the capacity expansion model (`master_problem.jl`). Determines optimal investment and retirement decisions across a multi-year planning horizon.

```python
class MasterProblemAdapter:
    def __init__(
        self,
        config: Union[ESFEXConfig, SystemConfig],
        years: List[int],
        base_year: int,
        demand: np.ndarray,
        demand_growth: float = 0.0,
        discount_rate: float = 0.05,
        max_annual_investment: float = 1e9,
        target_re_penetration: float = 0.5,
        initial_re_penetration: float = 0.1,
        min_re_increment: float = 0.0,
        max_re_increment: float = 1.0,
        life_extension_cost_factor: float = 0.3,
        decommissioning_cost_factor: float = 0.1,
        temporal_resolution_hours: int = 24,
        representative_days_per_year: int = 12,
        min_day_separation: int = 7,
        investment_resolution_hours: int = 8760,
        use_tsam: bool = False,
        tsam_period_start_hours: Optional[List[List[int]]] = None,
        tsam_period_weights: Optional[List[List[float]]] = None,
        tsam_chronological_order: Optional[List[List[int]]] = None,
        tsam_inter_period_linking: bool = True,
        tsam_critical_periods: Optional[List[int]] = None,
        use_stochastic: bool = False,
        stochastic_scenarios: Optional[List[dict]] = None,
        system_node_ranges: Optional[List[tuple]] = None,
        re_share_anchor: Optional[List[float]] = None,  # actual RE share per system before year 1
        config_path: Optional[str] = None,
        availability_cache: Optional[Dict[str, np.ndarray]] = None,
        inflow_cache: Optional[Dict[str, np.ndarray]] = None,
        date_start: Optional[str] = None,
        profile_start_hour: int = 0,
        solver_method: str = "monolithic",
        benders_max_iterations: int = 50,
        benders_tolerance: float = 1e-4,
        **kwargs
    ) -> None
```

**Key Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `years` | `list[int]` | Planning horizon years, e.g. `[2025, 2026, ..., 2050]`. |
| `base_year` | `int` | Base year for NPV discounting. |
| `demand` | `np.ndarray` | Demand array `(hours, nodes)` holding every planning year, one block per year. |
| `demand_growth` | `float` | Extra annual growth applied on top of each year's block (default `0.0`; the runner always passes 0). |
| `discount_rate` | `float` | Discount rate for NPV calculations (e.g., 0.05 = 5%). |
| `max_annual_investment` | `float` | Maximum annual investment budget in $. |
| `target_re_penetration` | `float` | Target RE penetration ratio by the final year (0-1). |
| `initial_re_penetration` | `float` | Starting RE penetration ratio (auto-calculated from existing RE capacity). |
| `temporal_resolution_hours` | `int` | Hours per timestep for representative days (e.g., 24). |
| `representative_days_per_year` | `int` | Number of representative days per year for operational validation. |
| `rep_day_offsets` | `list[int] \| None` | First step (0-based, within a year) of each representative day, the same for every year; the runner passes the days it chose once for the run (`master_rep_day_offsets`). `None`: the Julia master chooses them from the horizon-mean demand and its renewable profiles (`master_rep_day_offsets`). |
| `critical_day_offsets` | `list[int] \| None` | First step (0-based, within a year) of each critical day (`master_critical_days`), modelled with the representative days in every year. |
| `rep_day_weights` | `list[float] \| None` | Days of the year each representative day and then each critical day stands for (`master_day_weights`); `None`: the year shared equally by the representative days. |
| `storage_firm_hours` | `float \| None` | Hours a battery must sustain to count as firm in the capacity adequacy (min(power, usable energy / hours)); `None`: derived from the data. |
| `critical_days` | `int` | Critical days the Julia master chooses itself when `rep_day_offsets` is `None` (default 0). |
| `investment_resolution_hours` | `int` | Hours per investment period (8760 = one decision per year). |
| `use_tsam` | `bool` | Use Time Series Aggregation Method (TSAM) clustering instead of peak-day selection. |
| `tsam_period_start_hours` | `list[list[int]]` | Per-year start hours for TSAM periods (1-indexed); the runner passes the same days of the year for every year. |
| `tsam_period_weights` | `list[list[float]]` | Per-year weights for TSAM periods. |
| `tsam_inter_period_linking` | `bool` | Enable inter-period SOC linking for battery continuity across representative periods. |
| `tsam_critical_periods` | `list[int] \| None` | Indices (0-based) of the TSAM periods that are critical days. With TSAM, every renewable availability profile the master reads (renewable generators and technologies) is rescaled on the periods so their weighted energy equals each modelled year's (`rescale_profile_periods` in `esfex.models.tsam`: shape kept, capped at full availability, the energy a capped step cannot take spread over the others); these periods are kept as they are and count in that energy. Only the rows of the periods change. |
| `use_stochastic`, `stochastic_scenarios` | `bool`, `list[dict]` | Build the two-stage stochastic master (`create_stochastic_master_problem`): shared investment, one representative-day operation per scenario. Ignored, with a warning, when `solver_method="benders"`. |
| `system_node_ranges` | `list[tuple]` | `(name, first_bus, num_buses, initial_re)` per system (1-indexed buses), for per-system RE targets in multi-system runs. |
| `config_path` | `str` | Path to config file for resolving relative availability file paths. |
| `availability_cache` | `dict` | Pre-loaded hourly availability profiles for generators and technologies (the runner passes the dispatch's cache). A unit not in it is read from its file. |
| `inflow_cache` | `dict` | Pre-loaded hourly reservoir inflows (MW-eq) by generator key; a unit not in it is read from its `reservoir_inflow_file` (not clipped). |
| `date_start` | `str` | Timestamp of the first data row (`"DD/MM/YYYY HH:MM"`) for profiles read from disk, so Feb 29 is dropped by the data's calendar as in the dispatch. |
| `profile_start_hour` | `int` | Horizon hour (year index × 8760 + hour) of the first planned hour: every profile is rotated to start there, wrapping around a shorter one. The myopic runner passes `y_idx × 8760` for each one-year model. |
| `solver_method` | `str` | `"monolithic"` (one JuMP model) or `"benders"` (`run_benders_decomposition`). |
| `benders_max_iterations`, `benders_tolerance` | `int`, `float` | Benders settings: iteration cap and relative gap for convergence. |
| `retired_units` | `list[tuple]` | NPV-based retirement: `(kind, unit key, bus)` units an earlier myopic year retired; they stay out of this model and do not pay their decommissioning again. |
| `npv_unit_keys` | `set[str]` | NPV-based retirement: the generator and battery keys whose NPV is checked (default: all; the myopic runner passes the original units). |
| `npv_horizon_end` | `int` | NPV-based retirement: last planning year when it goes past `years` (the myopic runner passes it): the NPV also covers the unit's in-service years up to it (`extra_years` of Julia `unit_npvs`). |
| `debug_lp_dir` | `str` | Folder where Benders writes the master and subproblem LPs that fail (keyword; the runner passes `<output>/logs` when `ESFEX_BENDERS_DEBUG_LP=1`). None: no LP is written. |

**Methods:**

### build_model

```python
def build_model(self, use_representative_days: bool = True) -> None
```

Build the JuMP capacity expansion model. If `use_representative_days` is True, includes operational validation constraints using representative days (or TSAM clusters). With `solver_method="benders"` it only prepares the Julia input; the models are built during `solve()`. With `use_tsam` or investment-target custom constraints, which Benders does not model, it logs a warning, sets `solver_method` to `"monolithic"` and builds the monolithic master.

### solve

```python
def solve(self) -> int
```

Optimize the master problem (or run Benders decomposition). Returns PuLP-compatible status code: 1 when optimal, and also when the solver stopped at a limit (time, iterations…) holding a feasible plan, which is used with a WARNING giving the objective, the solver's bound and the gap; 0 for a limit without a feasible plan (the error suggests raising `solver.time_limit` or barrier without crossover), -1 infeasible, -2 unbounded. Benders uses a master iteration stopped at a limit with a feasible plan in the same way (Julia `run_benders_decomposition`).

With the system's `npv_retirement` on, it then runs the NPV-based retirement ([Capacity Expansion §9](../formulation/capacity-expansion.md#9-npv-based-iterative-retirement)): it computes each existing unit's NPV in the plan (Julia `unit_npvs`), retires the units below `npv_retirement_threshold` (Julia `retire_units`), rebuilds the model with `build_model` and solves again, for at most `npv_retirement_max_iterations` rounds, in every solver mode. A re-solve that is not optimal (or raises), that curtails more above the annual curtailment limit than the plan before (Julia `curtailment_excess_mwh`), or whose objective is above the plan before's (relative 1e-6; in a myopic year less the retired units' NPV past it, Julia `npv_round_reference_cost`) undoes its round: the last optimal model and its retirements are restored, a warning names the round's units and the reason, and `solve()` returns 1 for that plan. The retired units are listed in `npv_retirements` (dicts with `kind`, `key`, `name`, `bus`, `node`, `npv` in $ and `iteration`), and stay retired in later builds of this adapter.

### get_solution_values

```python
def get_solution_values(self) -> dict
```

Extract investment and retirement decisions keyed by year. Returns a dictionary with:

- Per-year generator investment power (MW) per technology per node
- Per-year battery investment power and capacity (MW/MWh) per technology per node
- Per-year transmission investment capacity (MW) per line
- Cumulative capacities per year
- Retirement decisions
- Total NPV cost

The top-level keys include `status`, `objective`, `solve_time`, `solution` (per-year dicts of `tech_investment_power_{t}_{n}`, `bat_tech_investment_power_{bt}_{n}`, `bat_tech_investment_capacity_{bt}_{n}` and `transfer_investment_{i}_{j}` values), `tech_investment`, `bat_tech_power_investment`, `bat_tech_capacity_investment`, `cumulative_*` capacities, `total_investment_by_year` (overnight cost of each year's builds, what the budget limits), `annuity_cost_by_year` (investment annuities paid each year, what the objective charges), `annuity_streams` (the streams behind them: `(build year index, life, annuity $ per year)`, 1-based build years; the myopic runner adds earlier years' streams to each year's reported cost), `total_operational_by_year` and `re_penetration_by_year` (achieved RE share).

The `{n}`, `{i}`, `{j}` indices and the per-bus arrays refer to the system's own buses (one per node in the merged multi-node master). When network reduction is enabled the master solves on the reduced network, and its results are laid out on the original buses before they are returned: eliminated buses hold no capacity.

### Other methods

| Method | Description |
|--------|-------------|
| `get_investment_decisions()` | Investment decisions by year (technology, battery power and energy, transmission). |
| `get_retirement_decisions()` | Retirement and life-extension decisions by year. |
| `get_year_investments(year)` | Investment keys for one calendar year, e.g. `tech_investment_power_0_0`. |
| `get_cumulative_capacity(year_idx=None, year=None)` | Cumulative `gen` and `bat` capacity per node for one year (`year_idx` is 1-indexed and deprecated). |
| `get_objective_value()` | NPV of total cost in $. |
| `get_re_targets()` | RE target per year index. |
| `export_solution(filepath)` | Write the solution to CSV. |
| `solution` (property) | Legacy-format solution dictionary. |

### write_lp

```python
def write_lp(self, filepath: str) -> None
```

Export the master problem LP file for debugging or sensitivity analysis. Requires a built JuMP model, so it is not available with `solver_method="benders"`.

---

## TransmissionDCAdapter

Builds the Julia `TransmissionDC` structure (lines, reactances, incidence matrix) from a node configuration. The DC power flow itself is angle-based: each line's flow is `S_base / x · (θ_i − θ_j)`, with one reference bus per connected component.

```python
class TransmissionDCAdapter:
    def __init__(
        self,
        num_nodes: int,
        nodes_config: NodeConfig,
        fuel_transport_distances: List[List[float]],
        s_base_mva: float = 100.0,
        reactance_per_km: float = 0.4,
        voltage_level_kv: float = 220.0,
        max_angle_diff_deg: float = 30.0,
        transmission_lines_geo=None,
        transformers=None,
        acdc_converters=None,
        freq_converters=None,
        buses=None,
    ) -> None
```

`s_base_mva` is the system power base of the per-unit line data (the runner passes `ac_power_flow.base_mva`). A line without `reactance_pu` gets `length_km × reactance_per_km / Z_base`, with `Z_base = V² / S_base` and `V` the line's `voltage_kv` (else `voltage_level_kv`).

**Properties:**

| Property | Type | Description |
|----------|------|-------------|
| `lines` | `list[tuple]` | List of `(from_bus, to_bus)` pairs (0-indexed). |
| `line_reactances` | `dict[tuple, float]` | Per-line reactance keyed by `(from, to)`. |
| `incidence_matrix` | `np.ndarray` | Bus-line incidence matrix (`+1` where a line leaves a bus, `-1` where it enters). |

---

## MGAAdapter

Wraps near-optimal alternative exploration (`mga.jl`). Two methods share this adapter, dispatched by `MGAConfig.method`:

- **`"mga"`** (default): wraps Julia's `run_mga_spores` — the classical Hop-Skip-Jump loop. Produces `num_alternatives` alternatives, each maximising a diversity objective weighted by the frequency score `1 − 2·freq`.
- **`"spores"`**: wraps Julia's `run_spores` — the per-objective sweep. Solves one alternative per entry in `MGAConfig.objectives`, with each entry mapped to its matching `Symbol` (see [§ SPORES](julia-api.md#spores-phase-2)).

Both paths return a uniform dictionary; each alternative carries an `objective` tag so downstream consumers (runner export, viewer) render them with one code path.

```python
class MGAAdapter:
    def __init__(
        self,
        master_adapter: MasterProblemAdapter,
        mga_config: MGAConfig
    ) -> None
```

**Key Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `master_adapter` | `MasterProblemAdapter` | A fully configured MasterProblemAdapter instance. Provides system data, demand, and Julia input construction. The adapter must NOT have been solved yet -- MGAAdapter handles the initial solve internally. |
| `mga_config` | `MGAConfig` | Settings from `config.master_problem.mga`. Fields: `enabled`, `method`, `objectives` (SPORES only), `num_alternatives` (MGA only), `slack_fraction`, `investment_threshold`. See [MGAConfig](config-schema.md#mgaconfig). |

**Methods:**

### run

```python
def run(self, use_representative_days: bool = True) -> dict
```

Execute the configured method (MGA or SPORES). The Julia input is built once via `master._create_input()` and reused; both Julia entry points construct their own JuMP model internally. Returns a dictionary with:

| Key | Type | Description |
|-----|------|-------------|
| `method` | `str` | `"mga"` or `"spores"` — the method that was actually run |
| `num_alternatives` | `int` | Total number of alternatives (including cost-optimal) |
| `slack_fraction` | `float` | Cost slack used |
| `optimal_cost` | `float` | Cost-optimal objective value ($) |
| `alternatives` | `list[dict]` | Per-alternative results, ordered with the cost-optimal seed at index 0 |

Each alternative dict contains:

| Key | Type | Description |
|-----|------|-------------|
| `alternative_id` | `int` | 0 = cost-optimal, 1..K = non-optimal alternatives |
| `is_optimal` | `bool` | `True` for alternative 0 |
| `cost` | `float` | Actual system cost for this alternative ($) |
| `diversity_objective` | `float` or `None` | Objective value at this alternative. `None` for the cost-optimal seed |
| `objective` | `str` | SPORES tag: `"cost_optimal"` for the seed, `"hsj_diversity"` for any MGA alt, or one of `"min_total_build"`, `"max_tech_equity"`, `"max_regional_equity"`, `"evolutionary_dist"` for SPORES alts |
| Investment data | various | Same structure as `MasterProblemAdapter.get_solution_values()` |

**Example — classical MGA:**

```python
from esfex.bridge.adapters import MasterProblemAdapter, MGAAdapter
from esfex.config.schema import MGAConfig

master = MasterProblemAdapter(config, years, base_year, demand, ...)
mga_cfg = MGAConfig(
    enabled=True, method="mga",
    num_alternatives=10, slack_fraction=0.05,
)
mga = MGAAdapter(master, mga_cfg)
result = mga.run(use_representative_days=True)

assert result["method"] == "mga"
for alt in result["alternatives"]:
    print(f"Alt {alt['alternative_id']}: cost={alt['cost']:.0f}, "
          f"objective={alt['objective']}, "
          f"diversity={alt['diversity_objective']}")
```

**Example — SPORES sweep:**

```python
from esfex.config.schema import MGAConfig, SporesObjective

mga_cfg = MGAConfig(
    enabled=True, method="spores",
    objectives=[
        SporesObjective.MIN_TOTAL_BUILD,
        SporesObjective.MAX_TECH_EQUITY,
        SporesObjective.MAX_REGIONAL_EQUITY,
        SporesObjective.EVOLUTIONARY_DIST,
    ],
    slack_fraction=0.05,
)
mga = MGAAdapter(master, mga_cfg)
result = mga.run(use_representative_days=True)

assert result["method"] == "spores"
assert result["num_alternatives"] == 1 + 4  # cost-optimal + 4 objectives
for alt in result["alternatives"]:
    print(f"Alt {alt['alternative_id']:>2}  "
          f"objective={alt['objective']:<22}  "
          f"cost=${alt['cost']/1e9:.2f}B")
```

Internally, `_run_spores` coerces `SporesObjective` enum members to their string `.value`s, marshals them into a Julia `Vector{Symbol}` via `seval("Symbol[…]")` (using `json.dumps` for the string literals — Python's `repr` would emit single quotes, which Julia parses as character literals), and forwards the vector to `ESFEX.run_spores`.

---

## units_config Parameter

Bridges strategic (MasterProblem) and operational (PowerSystem) optimization layers. After solving the master problem, the runner calls `_build_config_from_cumulative()` to compute cumulative installed capacities per year. These are stored in a `units_config` dictionary passed to `PowerSystemAdapter` via `kwargs['units_config']`.

**Structure:**

```python
units_config = {
    # Existing generator with updated capacity from investments
    "unit_3": {
        "rated_power": [0.0, 50.0, 0.0],  # MW per node
        "degradation_rate": [0.0, 0.0, 0.0],  # set to 0 (already applied in master)
        "initial_age": [0, 0, 0],  # set to 0 (age tracked in master)
    },
    # Virtual generator from technology investment
    "tech_solar_pv": {
        "name": "Investment Solar PV",
        "type": "Renewable",
        "fuel": "Sun",
        "rated_power": [0.0, 87.5, 0.0],
        "availability_file": "solar_availability.csv",
        "_type": "generator",
    },
    # Virtual battery from battery technology investment
    "tech_li_ion": {
        "name": "Investment Battery",
        "_type": "battery",
        "capacity": [0.0, 96.0, 0.0],  # MWh per node
        "MaxChargePower": [0.0, 24.0, 0.0],  # MW per node
        "MaxDischargePower": [0.0, 24.0, 0.0],
    },
}
```

When `PowerSystemAdapter._create_input()` encounters a key in `units_config`:
- If the key matches an existing generator in `sys.generators`, the `rated_power` is overridden.
- If the key is new (not in `sys.generators` or `sys.batteries`), a virtual generator or battery is created from the dict and added to the JuMP model.

---

## availability_cache Parameter

Dictionary mapping generator config keys to full-year NumPy arrays. Populated once at simulation startup by `runner._preload_availability_profiles()` and reused across all rolling horizon windows, avoiding thousands of redundant file loads.

**Structure:**

```python
availability_cache = {
    "unit_0": np.ndarray(shape=(8760, 4)),  # Solar PV availability, 4 nodes
    "unit_1": np.ndarray(shape=(8760, 4)),  # Wind availability
    "unit_2": np.ndarray(shape=(8760, 4)),  # Hydro availability
}
```

Inside `_create_input()`, the cache is sliced to the current window:

```python
# For hourly resolution:
availability = cached[start_hour : start_hour + hours]

# For coarser resolution (e.g., 6-hourly):
resampled = aggregate_to_resolution(cached, target_hours=resolution_hours)
start_idx = start_hour // resolution_hours
availability = resampled[start_idx : start_idx + hours]
```

---

## ElectrolyzerAdapter

Wraps the electrolyzer model (`electrolyzer.jl`). Handles hydrogen production optimization coupled with the power system.

---

## PrimaryEnergyAdapter

Wraps the primary energy model (`primary_energy.jl`). Optimizes fuel supply chains including storage, transport, and procurement.

`integrate_with_power_system(ps)` adds the block to the dispatch model: the fuel coupling of the generators and the hydrogen of the dispatch's electrolyzers into the `Hydrogen` tank. `get_results(keep_steps=None)` returns the per-period series, the investments, the cost totals in $ (`total_fuel_cost`, `total_transport_cost`, `total_loss_penalty`, over the first `keep_steps` steps when given) and `fuel_for_power` (`{generator index: [node, step]}`, fuel burnt per step by each coupled unit). For rolling horizons, `get_final_storage_levels(step=k)` and `get_in_transit_arrivals(step=k)` give the tank levels and the fuel still in transit after the `k` committed steps; the runner passes them to the next window's adapter as `initial_storage_levels` and `scheduled_arrivals` (`{fuel: array [node, step]}`).

---

## Helper Functions

### master_rep_day_offsets

```python
def master_rep_day_offsets(demand: np.ndarray, num_years: int, num_days: int,
                           min_separation: int, resolution_hours: int = 1,
                           profiles: list[np.ndarray] | None = None) -> list[int]
```

The representative days of a development run, chosen once: the first step (0-based, within a year) of each day, for every planning year. Calls the Julia `representative_day_offsets` on `demand` (steps × nodes, `num_years` yearly blocks) and `profiles` (1-D renewable availability series at the same resolution, one or several years): the peak day of the horizon-mean demand, then one day per segment of the year such that the days' mean demand and mean of each profile match the annual means. The runner passes the distinct availability profiles of the renewable generators and candidate technologies, logs the dates and the ratios (`master_rep_day_ratios`), and passes the days to every master as `rep_day_offsets`.

### master_critical_days

```python
def master_critical_days(demand: np.ndarray, num_years: int, num_days: int,
                         min_separation: int, resolution_hours: int = 1,
                         profiles: list[np.ndarray] | None = None,
                         capacities: list[float] | None = None,
                         exclude: list[int] | None = None) -> tuple[list[int], float]
```

The critical days of a development run, chosen once (Julia `critical_day_plan`): the `num_days` days with the largest net load, the day mean of the horizon-mean system demand over its annual mean minus the renewable supply (the `profiles`, weighted by the MW behind each, `capacities`) over its annual mean, skipping `exclude` (the representative days or TSAM periods) and `min_separation` days apart; the most critical first, as 0-based steps within a year. Also returns the storage firm hours: the longest stretch without solar on those days.

### master_day_weights

```python
def master_day_weights(demand: np.ndarray, num_years: int, offsets: list[int],
                       critical_offsets: list[int], resolution_hours: int = 1,
                       profiles: list[np.ndarray] | None = None,
                       prior: list[float] | None = None) -> list[float]
```

Days of the year each representative day (or TSAM period) and then each critical day stands for (Julia `representative_day_weights`): 1 per critical day; the representative days share the rest from `prior` (equal when `None`, the cluster sizes for TSAM), corrected so the year keeps the demand and renewable energy of the representative days alone.

### master_rep_day_ratios

```python
def master_rep_day_ratios(demand: np.ndarray, num_years: int, offsets: list[int],
                          resolution_hours: int = 1,
                          profiles: list[np.ndarray] | None = None,
                          weights: list[float] | None = None) -> list[float]
```

Mean on the representative days (or TSAM periods, weighted by `weights`) starting at `offsets`, over the annual mean, for the horizon-mean demand and then each of `profiles` (Julia `representative_day_ratios`). 1.0 means the master's year has the real year's energy; NaN for a series that is zero all year.

### _compute_geographic_fuel_adjustments

```python
def _compute_geographic_fuel_adjustments(sys_config: SystemConfig) -> Dict[str, List[float]]
```

Computes fuel cost adjustments based on geographic distance between generators and their nearest fuel storage facilities using haversine distance and per-fuel transport cost rates. Returns a dict mapping generator keys to adjusted fuel cost arrays.

### _resolve_element_bus_mapping

```python
def _resolve_element_bus_mapping(
    sys_config: SystemConfig,
) -> tuple[dict[str, dict[int, int]], dict[str, dict[int, int]]]
```

Resolve the physical bus of each existing generator and battery, per node. Sources, in priority order:

1. `transmission_lines_geo` entries whose `from_endpoint_type` / `to_endpoint_type` is `generator` or `battery` (one bus for the whole unit).
2. The unit's `bus_index` field, set by the Grid Builder.

Returns `(gen_bus_per_node, bat_bus_per_node)`, each mapping `unit_key → {node_idx: bus_idx}` (0-based) for every node where the unit's capacity is positive. Nodes without an entry use the node's first bus (see [Node vs bus indexing](#node-vs-bus-indexing)).

---

## Node vs bus indexing

Config arrays (`rated_power`, availability, reserves, …) are indexed by node; the Julia operational model is indexed by bus. The helpers in `esfex.bridge.converters` map between the two.

```python
def node_bus_mapping_required(buses, num_nodes: int) -> bool
def expand_node_to_bus_array(node_array, bus_to_node, mode="capacity",
                             bus_per_node=None,
                             replicate_to_all_buses_in_node=False) -> list
def expand_node_to_bus_matrix(node_matrix, bus_to_node,
                              default_value=1.0) -> np.ndarray
```

| Function | Description |
|----------|-------------|
| `node_bus_mapping_required` | `True` unless the bus list is the identity over nodes (one bus per node, bus `i` in node `i`). It is also `True` when there are fewer buses than nodes, as after network reduction drops isolated buses. |
| `expand_node_to_bus_array` | Per-node list to per-bus list. `mode="capacity"` puts each node's value on the unit's bus from `bus_per_node`, else on the node's first bus; with `replicate_to_all_buses_in_node=True` it is copied to every bus of the node (virtual investment units). `mode="property"` gives every bus its node's value. |
| `expand_node_to_bus_matrix` | `(hours, nodes)` to `(hours, buses)`: every bus gets its node's profile; out-of-range nodes get `default_value`. |

Both adapters call `node_bus_mapping_required` and expand per-node data when it returns `True`. On the way back the runner aggregates bus results to nodes: quantities (generation, storage, curtailment, load shedding, CO2, reserves, …) are summed over a node's buses, while commitment status, prices and voltage angles take the maximum.
