# System Architecture

ESFEX is organized in three layers: Python orchestration, a Python–Julia bridge for data conversion, and Julia optimization.


## High-Level Overview

```
+-------------------------------------------------------------+
|                      Python Layer                            |
|  config/schema.py   runner.py   io/demand.py   models/ev.py |
|  plugins/manager.py  sensitivity/engine.py  visualization/  |
+------------------------------+------------------------------+
                               |
                    Python <-> Julia Bridge
                               |
              +----------------+------------------+
              |        bridge/adapters.py          |
              |  bridge/converters.py              |
              |  bridge/julia_setup.py (juliacall) |
              +----------------+------------------+
                               |
+------------------------------+------------------------------+
|                       Julia Layer                            |
|  power_system.jl   master_problem.jl   benders.jl   mga.jl  |
|  transmission_dc.jl  transmission_ac.jl  transmission_acopf.jl|
|  primary_energy.jl  electrolyzer.jl  user_constraints.jl    |
+-------------------------------------------------------------+
                               |
                          JuMP + Solver
       (bundled: HiGHS, GLPK, Clarabel, SCS, Ipopt;
        optional: Gurobi, CPLEX, Xpress, CBC, SCIP)
```

---


## Python Layer

| Module | Role |
|--------|------|
| `config/schema.py` | Pydantic v2 models for all configuration |
| `config/loader.py` | YAML file loading with validation |
| `config/solver.py` | Solver option catalog, formulation/solver compatibility, availability detection |
| `runner.py` | Orchestrator -- the main simulation loop and HDF5 writing |
| `logging_config.py` | Console verbosity levels and the run log file |
| `io/demand.py` | Demand and availability profile loading (CSV/Excel) |
| `io/exporter.py` | HDF5 results export to CSV, Excel, JSON |
| `models/` | EV adoption and charging, rooftop solar, demand estimation, resource models |
| `topology/` | Internal network reduction (`network_reduction`) and result expansion |
| `analysis/` | N-1 and AC contingency analysis, frequency metrics, pandapower bridge |
| `zones.py` | Development zone preprocessor (virtual nodes) |
| `plugins/` | Plugin discovery, loading, and hook dispatch |
| `sensitivity/` | Sobol global sensitivity analysis [**[11]**](../reference/bibliography.md#ref11) |
| `visualization/` | PySide6 Studio with Leaflet.js map |

### Runner (`runner.py`)

The runner is the central orchestrator:

1. Receives the configuration loaded and validated by `config/loader.py`
2. Loads demand data for all simulation years
3. Generates EV charging profiles (S-curve growth model)
4. Preloads renewable availability profiles into an in-memory cache
5. Expands development zones into virtual nodes
6. Generates rooftop solar profiles (if enabled)
7. Solves the multi-year master problem (capacity expansion; `development` mode only)
8. Iterates year-by-year through operational dispatch (rolling horizon)
9. Computes derived metrics (LCOE, VALLCOE, capacity factors)
10. Appends each year's results to the HDF5 file

The runner maintains a `SimulationState` dataclass that tracks cumulative investments, retirements, boundary conditions, and primary energy capacities across years.

---


## Bridge Layer

The bridge (`bridge/`) converts between Python data structures (NumPy arrays, Pydantic models) and Julia data structures (JuMP models, Julia structs).


### Adapter Classes

| Adapter | Julia Module | Purpose |
|---------|-------------|---------|
| `PowerSystemAdapter` | `power_system.jl` | Operational dispatch and unit commitment |
| `MasterProblemAdapter` | `master_problem.jl`, `benders.jl` | Multi-year capacity expansion planning |
| `TransmissionDCAdapter` | `transmission_dc.jl` | DC power-flow constraints (Kirchhoff formulation) |
| `PrimaryEnergyAdapter` | `primary_energy.jl` | Fuel supply chain optimization |
| `ElectrolyzerAdapter` | `electrolyzer.jl` | Power-to-hydrogen modeling |
| `MGAAdapter` | `mga.jl` | Near-optimal alternatives (MGA and SPORES) |

Each adapter follows the same pattern:

1. Receives Python configuration objects (Pydantic models, NumPy arrays)
2. Calls converter functions to translate data into Julia-compatible types
3. Invokes the corresponding Julia function via `juliacall`
4. Receives Julia result structs and converts them back to Python (NumPy arrays, dicts)

### How Python and Julia Interact (juliacall)

`juliacall` embeds a Julia runtime inside the Python process. The initialization sequence:

```
Python process starts
    |
    v
julia_setup.initialize_julia(threads=4)
    |
    v
juliacall imports Julia Main module
    |
    v
Pkg.activate("src/esfex/julia/")  -- activates the Julia project
    |
    v
push!(LOAD_PATH, "<depot>/environments/esfex_solvers")
    |                    -- optional solvers from `esfex add-solver`
    v
include("src/esfex/julia/src/ESFEX.jl")  -- loads the ESFEX module
    |                    (Pkg.instantiate() and retry if packages are missing)
    v
using .ESFEX  -- makes all exported functions available
    |
    v
import Clarabel, Ipopt, SCS, GLPK, Cbc, SCIP, Gurobi, CPLEX, Xpress
    |                    -- each one that is installed; missing ones are skipped
    v
Adapters call ESFEX functions through juliacall
```

If a sysimage built by `esfex precompile` exists in `src/esfex/julia/`, it is passed to juliacall before Julia starts.

Key characteristics:

- **Single process**: Julia runs in the same process as Python (no subprocess or IPC overhead)
- **Shared memory**: NumPy arrays are converted to Julia arrays using `juliacall` type conversions
- **Lazy initialization**: Julia is only started when the first adapter is used
- **Caching**: Once initialized, the Julia runtime persists for the session
- **Thread control**: ESFEX sets `JULIA_NUM_THREADS=4` before initialization unless it is already set

### Data Type Conversions (`converters.py`)

| Python Type | Julia Type | Function |
|------------|-----------|----------|
| `np.ndarray` (1D) | `Vector{Float64}` | `py_to_julia_vector()` |
| `np.ndarray` (2D) | `Matrix{Float64}` | `py_to_julia_matrix()` |
| `list[int]` | `Vector{Int64}` | `py_to_julia_int_vector()` |
| `GeneratorConfig` | `GeneratorConfig` (Julia struct) | `convert_generator_config()` |
| `BatteryConfig` | `BatteryConfig` (Julia struct) | `convert_battery_config()` |
| `TechnologyConfig` | `TechnologyConfig` (Julia struct) | `convert_technology_config()` |
| `BatteryTechnologyConfig` | `BatteryTechnologyConfig` (Julia struct) | `convert_battery_technology_config()` |
| `dict` (network) | `NetworkConfig` (Julia struct) | `convert_network_config()` |

Costs are scaled from $ to M$ on the way into Julia (`scale_cost()`), which keeps the coefficient range of the models smaller.

---


## Julia Layer

The Julia code (`julia/src/`) implements the optimization models using JuMP [**[20]**](../reference/bibliography.md#ref20):

| File | Purpose |
|------|---------|
| `ESFEX.jl` | Module definition, includes, exports, solver loading and `create_optimizer()` |
| `types.jl` | Input/output data structures (structs) |
| `power_system.jl` | Operational dispatch and unit commitment |
| `master_problem.jl` | Multi-year capacity expansion (monolithic) |
| `benders.jl` | Benders decomposition of the master problem (`master_problem.solver_method: benders`) |
| `mga.jl` | MGA/SPORES near-optimal alternative generation |
| `transmission_dc.jl` | DC power flow (bus KCL, angle-based line flows) |
| `transmission_ac.jl` | AC power flow verification (Newton-Raphson post-DC) |
| `transmission_acopf.jl` | AC-OPF formulations: SOC, QC, SDP, polar NLP, rectangular NLP |
| `primary_energy.jl` | Fuel supply chain optimization |
| `electrolyzer.jl` | Power-to-hydrogen conversion |
| `user_constraints.jl` | Registry for user-defined (custom) constraints and plugin constraint hooks |
| `dcopf_benchmark.jl`, `acopf_benchmark.jl` | Stand-alone DC/AC-OPF solves used for benchmarking |

HiGHS [**[21]**](../reference/bibliography.md#ref21) is the default solver. GLPK, Clarabel, SCS and Ipopt are bundled as well; Gurobi, CPLEX, Xpress, CBC and SCIP can be added with `esfex add-solver`. See the [Solver Guide](../user-guide/solver-guide.md) for which solver suits which formulation.

### Optimization Models

| Model | File | Description |
|-------|------|-------------|
| Master Problem | `master_problem.jl` | Multi-year LP for investment/retirement decisions across all years simultaneously |
| Operational Dispatch | `power_system.jl` | Hourly dispatch via rolling horizon; LP (economic dispatch) or MIP (unit commitment) |
| DC Power Flow | `transmission_dc.jl` | Bus-level KCL with angle-based line flows [**[1]**](../reference/bibliography.md#ref1), N-1 contingency analysis [**[34]**](../reference/bibliography.md#ref34) |
| AC Power Flow | `transmission_ac.jl` | Post-DC Newton-Raphson verification of voltages, reactive power, and losses [**[5]**](../reference/bibliography.md#ref5) |
| AC-OPF | `transmission_acopf.jl` | AC-OPF dispatch with convex relaxations (SOC, QC, SDP) or exact NLP (polar, rectangular), selected with `power_flow_mode` |
| Primary Energy | `primary_energy.jl` | Fuel supply chains with storage, transport, and import constraints |
| Electrolyzer | `electrolyzer.jl` | Power-to-hydrogen conversion with capacity investment and efficiency modeling |
| MGA/SPORES | `mga.jl` | Near-optimal alternative generation [**[7]**](../reference/bibliography.md#ref7), [**[8]**](../reference/bibliography.md#ref8): Hop-Skip-Jump diversity (MGA) or one alternative per objective (SPORES) |

---


## Simulation Flow

```
 1. Load Config (YAML)
    |
    v
 2. Load Demand (Excel/CSV, all years)
    |
    v
 3. Generate EV Profiles (S-curve growth)
    |
    v
 4. Preload Availability Profiles (cache)
    |
    v
 5. Expand Development Zones (virtual nodes)
    |
    v
 6. Generate Rooftop Solar Profiles
    |
    v
 7. Solve Master Problem (all years, strategic; development mode only)
    |   --> investments, retirements, transmission expansion
    |   --> MGA/SPORES alternatives, if enabled (written to the mga/ group)
    v
 8. For each YEAR (y = 1..N):
    |
    |   8a. Apply cumulative investments to config
    |       (create virtual generators/batteries)
    |
    |   8b. Rebuild unit names for HDF5 export
    |
    |   8c. Solve Primary Energy (fuel supply chain)
    |
    |   8d. Rolling Horizon Dispatch:
    |       For each WINDOW (w = 1..W):
    |         - Extract window demand slice
    |         - Get availability profiles from cache
    |         - Solve PowerSystem (dispatch/UC)
    |         - Stitch results (discard overlap)
    |
    |   8e. Compute Derived Metrics
    |       (LCOE, VALLCOE, capacity factors, CO2)
    |
    |   8f. Write year results to HDF5
    |
    v
 9. Finalize HDF5, plugin post_simulation hooks, plugin teardown
```

### Rolling Horizon Detail

The operational dispatch uses a rolling horizon to keep solve times manageable. Both window size (`rolling_horizon_hours`) and overlap (`overlap_hours`) are user-configurable:

```
Year (8760 hours)
|<-------- Window 1 -------->|
                     |<-- overlap -->|
                          |<-------- Window 2 -------->|
                                              |<-- overlap -->|
                                                   |<--- Window 3 --->|
                                                   ...
```

- Window size and overlap are set via `rolling_horizon_hours` and `overlap_hours` in the configuration
- The state at the end of the kept (non-overlapping) part of the previous window provides the boundary conditions (battery SOC, generator status and output, reservoir and fuel storage levels) for the next window, which starts there
- Only the non-overlapping portion of each window's results is kept

## File Organization

```
src/esfex/
+-- __init__.py          # Package entry: load_config, ESFEXConfig
+-- cli.py               # CLI commands (typer)
+-- runner.py             # Orchestrator
+-- logging_config.py     # Console verbosity and run log file
+-- paths.py              # Central data-path registry
+-- zones.py              # Development zone preprocessor
+-- config/
|   +-- schema.py         # Pydantic models
|   +-- loader.py         # YAML loading
|   +-- solver.py         # Solver options, compatibility, detection
+-- bridge/
|   +-- adapters.py       # Julia model wrappers (6 adapters)
|   +-- converters.py     # Data type conversion (Python <-> Julia)
|   +-- julia_setup.py    # Julia runtime, sysimage, add/remove solver
|   +-- topology_audit.py # GUI topology vs. solver view cross-check
+-- io/
|   +-- demand.py         # Demand and availability loading
|   +-- exporter.py       # Results export (CSV, Excel, JSON)
+-- models/               # EV, rooftop solar, demand estimation, resource models
+-- topology/             # Network reduction and result expansion
+-- analysis/             # Contingency, frequency, pandapower bridge
+-- plugins/
|   +-- protocol.py       # Plugin base class (ESFEXPlugin)
|   +-- manager.py        # Plugin discovery, loading, lifecycle
|   +-- availability_generator/  # Built-in plugin: solar/wind/OTEC profiles
+-- sensitivity/
|   +-- engine.py         # Sobol global sensitivity analysis
+-- utils/
|   +-- temporal.py       # Time resolution utilities
+-- julia/
|   +-- Project.toml      # Julia project dependencies
|   +-- Manifest.toml     # Julia dependency lockfile
|   +-- build_sysimage.jl # Used by `esfex precompile`
|   +-- src/
|       +-- ESFEX.jl              # Module definition, solver loading
|       +-- types.jl              # Data structures
|       +-- power_system.jl       # Operational dispatch
|       +-- master_problem.jl     # Capacity expansion
|       +-- benders.jl            # Benders decomposition
|       +-- mga.jl                # MGA/SPORES alternatives
|       +-- transmission_dc.jl    # DC power flow
|       +-- transmission_ac.jl    # AC power flow verification
|       +-- transmission_acopf.jl # AC-OPF formulations
|       +-- primary_energy.jl     # Fuel supply chain
|       +-- electrolyzer.jl       # Power-to-hydrogen
|       +-- user_constraints.jl   # Custom constraint registry
+-- visualization/        # Studio (PySide6 + Leaflet.js)
    +-- app.py            # Application entry point
    +-- main_window.py    # Main window layout and orchestration
    +-- map_widget.py     # Leaflet.js map (QWebEngineView + QWebChannel)
    +-- i18n.py           # Internationalization
    +-- data/
    |   +-- gui_model.py  # GUI data model (GuiSystemState)
    |   +-- serializer.py # YAML export from GUI model
    +-- panels/           # Property forms, element tree, results
```

---


## Plugin System

ESFEX includes a directory-based plugin system. Plugins extend ESFEX without modifying its source code.

### Plugin Discovery

Plugins are discovered from:

1. `~/.esfex/plugins/` -- user-level plugins
2. Directories listed in the `$ESFEX_PLUGIN_PATH` environment variable

`<project_dir>/.esfex/plugins/` is scanned only when the caller passes `include_project_dir=True` to the plugin manager. The CLI and the runner never do, so a project opened from someone else cannot run code just by shipping a plugin; a warning is logged when such a directory is skipped.

Each plugin is a directory containing:

- `plugin.json` -- metadata (name, version, description, dependencies)
- `__init__.py` -- factory function `create_plugin(context) -> ESFEXPlugin`

### Plugin Hooks

Plugins can hook into the simulation lifecycle at these points:

| Hook | When |
|------|------|
| `setup()` | After plugin instantiation |
| `on_config_loaded()` | When a run starts, after the plugin's `plugins.<name>` section passed `get_config_schema()` |
| `pre_simulation()` | Before simulation starts |
| `post_demand_loaded()` | After demand data is loaded (can modify demand) |
| `pre_master_problem()` | Before capacity expansion solve |
| `post_master_problem()` | After capacity expansion solve |
| `pre_year()` | Before each year's dispatch |
| `post_year()` | After each year's results (can write to HDF5) |
| `post_simulation()` | After all years complete |
| `teardown()` | After the run, and on shutdown |

Plugins can also add `esfex <plugin name> ...` commands (`get_cli_commands()`) and Studio extensions (tree categories, forms, toolbar actions, menus, result variables, map layers); see [Plugin Development](../contributing/plugin-development.md).

### Julia Extensions

Plugins can provide Julia modules via `get_julia_modules()`. These are `include()`-d at runtime as overlays -- the core `.jl` files are never modified. This allows plugins to add custom constraints, variables, or objective terms to the optimization models.

### Plugin Security

- Plugin names are sanitized (alphanumeric, underscore, hyphen only)
- ZIP installation validates against Zip Slip (CWE-22)
- Git clone disables hooks to prevent pre-checkout RCE
- Git URLs restricted to `https://` and `git://` schemes; hosts other than github.com, gitlab.com, bitbucket.org, codeberg.org and git.sr.ht need explicit consent
- Project-level plugin directories are not scanned by default (see above)
- SHA-256 hash of plugin contents is logged on load

---


## GUI Architecture

The Studio (`visualization/`) is a PySide6 desktop application with an embedded Leaflet.js map.

### Components

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Main window | PySide6 (`QMainWindow`) | Layout, menus, toolbar |
| Map widget | Leaflet.js via `QWebEngineView` | Geographic node/line placement |
| Element tree | PySide6 (`QTreeWidget`) | Hierarchical element browser |
| Property panels | PySide6 (`QWidget` forms) | Element configuration forms |
| Bridge | `QWebChannel` | Python-JS bidirectional communication |

### Data Model

The GUI uses its own data model (`GuiSystemState`) with instance-based elements (one object per generator/battery/line, not per-node arrays). The serializer converts this GUI model into the YAML configuration format expected by the optimizer.

### Multi-System Support

The GUI supports editing multiple power systems simultaneously. Each system has its own `GuiSystemState`, tree root node, and map layers. The user can switch between systems via the element tree.

### Map Interaction

- **Nodes**: Placed and dragged on the map with position snapping
- **Lines**: Drawn as polyline traces with waypoints (start trace, add waypoints, finish)
- **Equipment**: Generators, batteries, transformers, and fuel entries snap to nodes via a magnetic registry
- **Drag propagation**: Moving a node automatically updates all connected equipment and line endpoints

See [GUI Editor](../gui/overview.md) for details.
