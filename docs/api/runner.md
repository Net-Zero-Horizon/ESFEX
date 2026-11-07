# Runner

Module: `esfex.runner`

## Orchestrator

Coordinates multi-year capacity expansion and operational optimization.

```python
class Orchestrator:
    def __init__(
        self,
        config: ESFEXConfig,
        output_dir: Optional[Path] = None,
        config_path: Optional[Path] = None,
    ) -> None
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `config` | `ESFEXConfig` | (required) | Validated ESFEX configuration object |
| `output_dir` | `Optional[Path]` | `"./results"` | Directory for HDF5 output files and logs |
| `config_path` | `Optional[Path]` | `None` | Path to the original YAML file (for relative path resolution of demand files, availability profiles, etc.) |

**Attributes:**

| Attribute | Type | Description |
|-----------|------|-------------|
| `config` | `ESFEXConfig` | The validated configuration |
| `output_dir` | `Path` | Output directory (created if needed) |
| `primary_system` | `SystemConfig` | All systems of `meta_network.systems` merged into one operational (bus-level) system, used for dispatch. Inter-system links become extra transmission lines |
| `master_system` | `SystemConfig` | The same merged system at node level, used by the master problem |
| `system_name` | `str` | The system names joined with `_` (for example `IslaJuventud_Cuba`) |
| `state` | `Optional[SimulationState]` | Current simulation state (set during `run()`) |
| `results` | `list[YearResults]` | Accumulated results across years |
| `master_solution` | `Optional[dict]` | Master problem decisions after `run(master_only=True)` (`investments`, `retirements`, `cumulative_capacities`, `re_targets`, each keyed by year); `None` otherwise or if the master problem failed |

When development zones are configured, `run()` replaces `primary_system` and `master_system` with the zone-expanded systems.

The constructor also loads plugins (`PluginManager.load_all()`), gives them the config (`PluginManager.apply_config()`: validates each plugin's `plugins.<name>` section and calls `on_config_loaded`; an invalid section raises `ValueError`) and registers their Julia modules.

**Example:**

```python
from esfex import load_config
from esfex.runner import Orchestrator

config = load_config("isla_juventud.yaml")
orch = Orchestrator(
    config,
    output_dir="./results",
    config_path=Path("isla_juventud.yaml"),
)
```

### run

```python
def run(
    self,
    years: Optional[int] = None,
    start_year: Optional[int] = None,
    master_only: bool = False,
    dispatch_years: Optional[Iterable[int]] = None,
) -> list[YearResults]
```

Execute the full simulation: Master Problem (capacity expansion) followed by year-by-year operational dispatch.


**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `years` | `Optional[int]` | `25` | Number of years to simulate |
| `start_year` | `Optional[int]` | year of `date_start` | Label of the first simulated year. When given, the date labels start on `date_start`'s day and time in this year. It only relabels: it does not select demand rows, and the Feb 29 rows dropped from leap-year data are those of `date_start`'s calendar |
| `dispatch_years` | `Optional[Iterable[int]]` | all years | Run the operational dispatch only for these horizon years (labels); the others are skipped, each year keeps its own data block (year index × 8760 h) and the master problem still plans the whole horizon. In `unit_commitment` mode the first of them is dispatched instead of the first horizon year. `ValueError` for a year outside the horizon. Without it, the `ESFEX_OPS_ONLY_YEARS` environment variable is read |
| `master_only` | `bool` | `False` | Stop after the Master Problem (step 10 below): the decisions are left in `master_solution`, the HDF5 file (system configuration only) is finalized, plugins are torn down and an empty list is returned. Needs `simulation_mode: development` (`ValueError` otherwise) |

**Returns:** `list[YearResults]` -- One result object per simulated year.

In `unit_commitment` mode only the first year is run, whatever `years` is.

**Failure modes:**

- Returns an empty list if the Master Problem fails.
- Raises `RuntimeError` if no operational window of a year solves.
- Any other exception raised while running a year is logged and re-raised.

**Workflow** (plugins are already loaded by the constructor):

1. **`pre_simulation`** plugin hook
2. Load demand data for all years (`_load_demand()`)
3. Generate EV charging profiles with S-curve growth (`_generate_ev_demand()`)
4. **`post_demand_loaded`** plugin hook (plugins may modify demand arrays)
5. Create sectoral demand distribution (`_create_sectoral_demand()`)
6. Preload availability profiles into cache (`_preload_availability_profiles()`)
7. Expand development zones if configured (`expand_config_with_zones()`)
8. Generate rooftop solar profiles if configured
9. Initialize HDF5 output file
10. **Step 1: Master Problem** (development mode only)
    - **`pre_master_problem`** plugin hook
    - Solve capacity expansion: all years at once (`planning_mode: perfect_foresight`) or one year at a time (`myopic`)
    - **`post_master_problem`** plugin hook
11. **Step 2: Operational Dispatch** (year by year)
    - For each year:
        - Apply investment decisions from Master Problem
        - Build cumulative unit configuration (`_build_config_from_cumulative()`)
        - Rebuild generator/battery name lists for HDF5 alignment
        - **`pre_year`** plugin hook
        - Run rolling horizon dispatch (`_run_operational_dispatch()`)
        - Compute derived metrics (LCOE, VALLCOE, capacity factor)
        - Export year to HDF5
        - **`post_year`** plugin hook (receives the HDF5 file open in append mode)
12. Finalize HDF5 file
13. **`post_simulation`** plugin hook
14. `PluginManager.teardown_all()`

**Example:**

```python
results = orchestrator.run(years=25, start_year=2025)

for yr in results:
    print(f"Year {yr.year}: ${yr.objective:,.0f}")
    print(f"  RE penetration: {yr.re_penetration:.1%}")
    print(f"  Load shedding: {yr.load_shed:.1f} MWh")
    print(f"  Emissions: {yr.emissions:.0f} tCO2")
```

### Plugin Hooks

The Orchestrator dispatches hooks to all loaded plugins at key points in the pipeline. Plugins are discovered from `~/.esfex/plugins/` and from the directories in the `ESFEX_PLUGIN_PATH` environment variable, and loaded via the `PluginManager`. The runner does not scan a project's `.esfex/plugins/` directory (it logs a warning if one exists). Each hook call is wrapped in `try/except` so a broken plugin is logged but never crashes the core simulation. Hooks are called with keyword arguments.

| Hook | Timing | Arguments |
|------|--------|-----------|
| `pre_simulation` | Before any computation | `config`, `output_dir` |
| `post_demand_loaded` | After demand loading | `base_demand`, `ev_demand`, `total_demand`, `config` |
| `pre_master_problem` | Before Master Problem solve | `config`, `years` |
| `post_master_problem` | After Master Problem solve | `investments`, `retirements`, `config` |
| `pre_year` | Before each year's dispatch | `year`, `year_idx`, `units_config`, `config` |
| `post_year` | After each year is written to HDF5 | `year`, `result`, `hdf5_file` (open `h5py.File`, or `None` if h5py is missing), `output_dir`, `config` |
| `post_simulation` | After the HDF5 file is finalized | `results`, `hdf5_path`, `output_dir`, `config` |

See `esfex.plugins.protocol.ESFEXPlugin` for the full hook API.

### Internal Methods

These methods are called internally during `run()` but are documented for subclass extension:

| Method | Description |
|--------|-------------|
| `_load_demand(years, start_year)` | Load base demand for all years from file |
| `_generate_ev_demand(num_nodes, total_hours, ...)` | Generate S-curve EV profiles |
| `_create_sectoral_demand(total_demand)` | Distribute demand into sectors |
| `_preload_availability_profiles(num_nodes)` | Cache all availability files at startup |
| `_solve_master_problem(years_range, demand, ...)` | Solve strategic capacity expansion |
| `_run_operational_dispatch(year, demand, ...)` | Rolling horizon dispatch for one year |
| `_solve_window(year, window, demand, hours, num_nodes, boundary_conditions, ...)` | Solve a single dispatch window |
| `_build_config_from_cumulative(units, caps)` | Build unit config with investments applied |
| `_rebuild_unit_names(units_config)` | Rebuild name lists to match Julia ordering |
| `_compute_derived_metrics(result)` | Compute LCOE, VALLCOE, capacity factor, selling prices (in place) |
| `_calculate_initial_re_penetration(first_year_demand)` | Estimate initial RE penetration from existing capacity |
| `_apply_transfer_investments(investments)` | Apply transmission line investment decisions |
| `_extract_year_demand(total_demand, y_idx, hours)` | Slice year-specific demand from multi-year array |
| `_initialize_hdf5(path, ...)`, `_append_year_to_hdf5(path, result)`, `_finalize_hdf5(path)` | Write the results file ([HDF5 Output Schema](../reference/hdf5-output-schema.md)) |

---

## SimulationState

Tracks simulation state across years and rolling horizon windows.

```python
@dataclass
class SimulationState:
    year: int
    base_year: int
    units_config: dict[str, Any]
    boundary_conditions: dict[str, Any] = field(default_factory=dict)
    cumulative_investments: dict[str, Any] = field(default_factory=dict)
    cumulative_retirements: dict[str, Any] = field(default_factory=dict)
    primary_energy_capacities: dict[str, Any] = field(
        default_factory=lambda: {"storage": {}, "transport": {}}
    )
```

**Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `year` | `int` | Current simulation year |
| `base_year` | `int` | First year of the simulation |
| `units_config` | `dict[str, Any]` | Current unit configuration (includes investment adjustments) |
| `boundary_conditions` | `dict[str, Any]` | Rolling horizon boundary state (battery SOC, generator status) |
| `cumulative_investments` | `dict[str, Any]` | Accumulated investment decisions up to current year |
| `cumulative_retirements` | `dict[str, Any]` | Accumulated retirement decisions up to current year |
| `primary_energy_capacities` | `dict[str, Any]` | Primary energy storage and transport capacity state |

---

## YearResults

Complete results for one simulation year, including all optimization variables, derived metrics, and scalar summaries.

Per-node arrays are at node level: when the operational network has buses that differ from the nodes, the runner aggregates bus results to nodes (sum for quantities; max for `gen_status`, `prices` and `voltage_angle`). Exceptions are noted in the tables below. With a rolling horizon the hour axis covers the year once (overlap hours are dropped), also for the `power_flow`, `transfer_margin` and N-1 dual series; a key missing from a window (no binding dual there) is zero-filled for that window's steps. The scalars (`objective`, `emissions`, `load_shed`, `total_generation`, `total_demand`) and `cost_breakdown` likewise add up each window's committed steps only ([Result Stitching](../formulation/operational-dispatch.md#104-result-stitching)).

```python
@dataclass
class YearResults:
    year: int
    objective: float
    solve_time: float
    feasible: bool
    ...
```

### Core Fields

| Field | Type | Shape | Description |
|-------|------|-------|-------------|
| `year` | `int` | scalar | Simulation year |
| `objective` | `float` | scalar | Total system cost ($) |
| `solve_time` | `float` | scalar | Total solve time (seconds) |
| `feasible` | `bool` | scalar | Whether all windows were feasible |

### Generation Arrays

| Field | Type | Shape | Description |
|-------|------|-------|-------------|
| `gen_output` | `Optional[np.ndarray]` | `(gen, node, hour)` | Generator output (MW) |
| `gen_status` | `Optional[np.ndarray]` | `(gen, node, hour)` | Generator on/off status (UC mode) |
| `gen_startup` | `Optional[np.ndarray]` | `(gen, node, hour)` | Startup events |
| `curtailment` | `Optional[np.ndarray]` | `(gen, node, hour)` | RE curtailment (MW); summed over generators when written to HDF5 |

### Storage Arrays

| Field | Type | Shape | Description |
|-------|------|-------|-------------|
| `bat_charge` | `Optional[np.ndarray]` | `(bat, node, hour)` | Charging power (MW) |
| `bat_discharge` | `Optional[np.ndarray]` | `(bat, node, hour)` | Discharging power (MW) |
| `bat_soc` | `Optional[np.ndarray]` | `(bat, node, hour)` | State of charge (MWh) |
| `bat_spillage` | `Optional[np.ndarray]` | `(bat, node, hour)` | Battery spillage (MW) |

### Reserve and Reliability Arrays

| Field | Type | Shape | Description |
|-------|------|-------|-------------|
| `reserve_static` | `Optional[np.ndarray]` | `(node, hour)` | Static reserve (MW) |
| `reserve_dynamic` | `Optional[np.ndarray]` | `(node, hour)` | Dynamic reserve (MW) |
| `loss_of_reserve_static` | `Optional[np.ndarray]` | `(node, hour)` | Static reserve deficit (MW) |
| `loss_of_reserve_dynamic` | `Optional[np.ndarray]` | `(node, hour)` | Dynamic reserve deficit (MW) |
| `load_shed_array` | `Optional[np.ndarray]` | `(node, hour)` | Load shedding (MW) |
| `co2_emissions` | `Optional[np.ndarray]` | `(node, hour)` | CO2 emission rate (tCO2/h); × `resolution_hours` for tonnes per step |
| `loss_of_inertia` | `Optional[np.ndarray]` | `(hour,)` | System inertia deficit (labelled GW*s in the HDF5 file) |

### Network and Prices

| Field | Type | Shape | Description |
|-------|------|-------|-------------|
| `power_flow` | `Optional[dict]` | `{(from_bus, to_bus): array}` | Line power flows (MW), keyed by operational bus pair (not aggregated to nodes) |
| `voltage_angle` | `Optional[np.ndarray]` | `(node, hour)` | Voltage angles (rad) |
| `voltage_magnitude` | `Optional[np.ndarray]` | `(bus, hour)` | Voltage magnitude (p.u.), ACOPF modes only |
| `reactive_generation` | `Optional[np.ndarray]` | `(gen, bus, hour)` | Reactive power (MVAr), ACOPF modes only |
| `transfer_investment` | `Optional[dict]` | `{(from,to): float}` | Transmission investment (MW) |
| `transfer_margin` | `Optional[dict]` | `{(from_bus, to_bus): array}` | Transfer margin slack (MW), keyed by operational bus pair like `power_flow` |
| `prices` | `Optional[np.ndarray]` | `(node, hour)` | Nodal prices ($/MWh, dual of power balance) |
| `price_energy_component` | `Optional[np.ndarray]` | `(hour,)` | Mean of `prices` over nodes ($/MWh) |
| `price_congestion_component` | `Optional[np.ndarray]` | `(node, hour)` | `prices` minus the energy component ($/MWh) |
| `demand` | `Optional[np.ndarray]` | `(hour, node)` | Gross demand for the year (MW): base demand, plus EV demand when EV optimisation is off. Rooftop generation is not subtracted here (it is subtracted only from the demand sent to the dispatch model) |
| `rooftop_generation` | `Optional[np.ndarray]` | `(hour, node)` | Behind-the-meter rooftop solar (MW), diagnostic only |

### EV Variables

| Field | Type | Shape | Description |
|-------|------|-------|-------------|
| `ev_charging` | `Optional[np.ndarray]` | `(node, hour)` | EV charging demand (MW) |
| `ev_v2g` | `Optional[np.ndarray]` | `(node, hour)` | V2G discharge (MW) |
| `ev_soc` | `Optional[np.ndarray]` | `(node, hour)` | EV fleet SOC (MWh) |
| `ev_loss` | `Optional[np.ndarray]` | `(node, hour)` | Unmet EV demand (MW) |

### Investment and Retirement

| Field | Type | Description |
|-------|------|-------------|
| `investments` | `dict[str, float]` | Year-specific investment decisions from Master Problem |
| `retirements` | `dict[str, float]` | Year-specific retirement decisions |
| `gen_investment_array` | `Optional[np.ndarray]` | Generator investment `(gen, node)` in MW |
| `bat_investment_power` | `Optional[np.ndarray]` | Battery power investment `(bat, node)` in MW |
| `bat_investment_capacity` | `Optional[np.ndarray]` | Battery energy investment `(bat, node)` in MWh |

### Derived Metrics

| Field | Type | Shape | Description |
|-------|------|-------|-------------|
| `capacity_factor` | `Optional[np.ndarray]` | `(gen, node, hour)` | Generator capacity factors |
| `lcoe` | `Optional[np.ndarray]` | `(gen, node, hour)` | Levelized cost of energy ($/MWh) |
| `vallcoe` | `Optional[np.ndarray]` | `(gen, node, hour)` | Value-adjusted LCOE ($/MWh) |
| `bat_capacity_factor` | `Optional[np.ndarray]` | `(bat, node, hour)` | Battery capacity factors |
| `bat_lcoe` | `Optional[np.ndarray]` | `(bat, node, hour)` | Battery LCOE ($/MWh) |
| `bat_vallcoe` | `Optional[np.ndarray]` | `(bat, node, hour)` | Battery value-adjusted LCOE ($/MWh) |
| `fuel_for_power` | `Optional[dict]` | `{unit: (node, hour)}` | Fuel burnt by each fuel-coupled unit (fuel units per step), keyed by unit name; `None` without primary energy. Written to `primary_energy/fuel_for_power/` |
| `technology_selling_prices` | `Optional[dict]` | `{dataset_name: dict}` | Selling-price data per generator, keyed by its HDF5 dataset name (`/` replaced, homonyms suffixed ` (1)`, ` (2)`, ...): `prices_weights`, `total_generation`, `total_revenue`, `average_selling_price`, `technology_type` |

Formulas: [Results Export § Derived Metrics](../user-guide/results-export.md#derived-metrics).

### Primary Energy and N-1 Security

| Field | Type | Description |
|-------|------|-------------|
| `primary_energy` | `Optional[dict]` | Primary energy results merged over windows (committed steps only, per period of `primary_energy_resolution` hours from the start of the year): `{section: {fuel: array}}` plus cost totals (summed over each window's committed steps) and `fuel_for_power` |
| `n1_gen_reserve_duals` | `Optional[np.ndarray]` | `(hour,)` dual of the generation N-1 constraint |
| `n1_trans_reserve_duals` | `Optional[dict]` | Duals of the transmission N-1 constraints |
| `n1_binding_contingencies` | `Optional[list]` | Binding contingency IDs |
| `n1_security_cost` | `float` | Incremental cost of N-1 security ($), default `0.0`; the model does not compute it yet, so it stays `0.0` |

### System-Level Scalars

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `emissions` | `float` | `0.0` | Total CO2 emissions (tonnes; step rates × `resolution_hours`) |
| `re_penetration` | `float` | `0.0` | Renewable energy penetration fraction |
| `load_shed` | `float` | `0.0` | Total unserved energy (MWh) |
| `total_generation` | `float` | `0.0` | Total energy generated (MWh) |
| `total_demand` | `float` | `0.0` | Total demand (MWh) |
| `master_re_target` | `float` | `0.0` | RE target from Master Problem for this year |

### Cost Breakdown

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `cost_breakdown` | `Optional[dict]` | `None` | Per-component decomposition of the annual system cost (fuel, O&M, CO2, investment, penalties, etc.). Contains 35 cost component keys plus `total`. See [HDF5 Output Schema — cost_breakdown](../reference/hdf5-output-schema.md#cost_breakdown) for the full list of fields. `None` when cost decomposition is not available. |

### Reservoir Hydroelectric

| Field | Type | Shape | Description |
|-------|------|-------|-------------|
| `reservoir_level` | `Optional[np.ndarray]` | `(gen, node, hour)` | Reservoir storage level at the start of each hour (MWh) |
| `reservoir_spillage` | `Optional[np.ndarray]` | `(gen, node, hour)` | Reservoir spillage (MW) |
| `reservoir_pump` | `Optional[np.ndarray]` | `(gen, node, hour)` | Pump-storage pumping (MW) |
| `reservoir_invest_capacity` | `Optional[np.ndarray]` | `(gen, node)` | Reservoir capacity investment (MWh) |
