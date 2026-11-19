# Scripting & Automation

Python console and multi-tab script editor for automating element creation, batch modifications, data extraction, analysis, and simulation. Scripts have full access to the GUI data model, the `esfex` scripting API, and any installed Python library.


---


## Console and Script Editor

### Python Console

Interactive REPL in the **Python REPL** tab at the bottom-left (next to the **Run Output** tab), built on `code.InteractiveInterpreter`. Startup banner:

```
Python Console - ESFEX Studio
Available: model, state, config, window, esfex, np
Type esfex.help() for scripting API reference.
>>>
```

#### Console Keys

| Key | Action |
|-----|--------|
| `Enter` | Execute the current line (or add to multi-line buffer if incomplete) |
| `Up Arrow` | Navigate to the previous command in history |
| `Down Arrow` | Navigate to the next command in history |
| `Home` | Move to the start of the input (after the prompt) |
| `Ctrl+C` | With no text selected: cancel the current input line (or a running subprocess). With a selection: copy. |

#### Multi-Line Input

Incomplete statements (e.g., `for` loops, `if` blocks, function definitions) change the prompt from `>>>` to `...`. Press `Enter` on an empty line to execute.

```python
>>> for g in state.generators.values():
...     if g.rated_power > 50:
...         print(f"{g.name}: {g.rated_power} MW")
...
Solar Farm North: 100.0 MW
Diesel Plant A: 75.0 MW
```

#### Command History

Session-level command history navigable with Up/Down arrow keys. Not persisted between sessions.

### Script Editor

Bottom-right of the window. Multi-line scripting environment:

- **Multiple tabs** -- Open several scripts at once. Each tab has its own undo history. Closing a tab does not ask to save; closing the last tab clears it.
- **Syntax highlighting** -- Python keywords, builtins, strings, numbers, comments, and decorators are highlighted using the current theme's colors.
- **Line numbers** -- Displayed in the left gutter (can be turned off under **File > Preferences... > Studio > Script Editor**, along with font, tab width, word wrap and auto-indent). These preferences apply from startup.
- **Tab key** -- `Tab` inserts 4 spaces; `Shift+Tab` removes 4 leading spaces.
- **Auto-indent** -- `Enter` starts the new line with the indentation of the current one, plus 4 spaces after a line ending in `:`. Turn it off in the preferences above.

#### Script Editor Actions

| Control | Action |
|---------|--------|
| **▶ Run** (`Ctrl+Enter`) | Execute the current tab's script. The console switches to the **Python REPL** tab and shows the output. |
| **☰ > New** | Create a blank script tab. |
| **☰ > Open** | Load a `.py` file into a new tab. |
| **☰ > Save** | Save the current tab to its file (asks for a path the first time). |
| **☰ > Save As** | Save the current tab to a new file. |

Scripts execute in the same namespace as the console. Variables are shared in both directions.


---


## Available Objects

Six pre-loaded objects in the console/script namespace:

| Variable | Type | Description |
|----------|------|-------------|
| `model` | `GuiModel` | The central data model. Use its methods for create/update/remove operations; they emit the signals that refresh the map and the element tree. |
| `state` | `GuiSystemState` | The active system's data container. Rebound when you switch systems or load a configuration. |
| `config` | `ESFEXConfig` or `None` | The loaded configuration object. `None` until a YAML file has been opened. |
| `window` | `MainWindow` | The Studio window. Gives access to the map widget, element tree, console, and the other UI components. |
| `esfex` | `ESFEXAPI` | The [scripting API](#scripting-api-esfex): load configs and results, run simulations, workflows, plots. |
| `np` | module | `numpy`. |

!!! note
    `state` is not rebound after **Edit > Undo** or **Redo**: undo replaces `model.state` with a restored copy, and `state` keeps pointing to the old object. In scripts, prefer `model.state`, which is always the live state of the active system.

### The `model` Object

Primary interface for programmatic modifications.

**Important:** Use the `model` methods. Direct attribute changes on state objects update the data but do not refresh the map or tree.

```python
# Correct -- refreshes the marker and tooltip
model.update_generator(gen_id, rated_power=200.0)

# Data changes, but the map is not refreshed
model.state.generators[gen_id].rated_power = 200.0
```

`update_*` methods set only attributes that exist on the element and skip unknown names without an error. An unknown element id is also ignored. Check names against the dataclasses in `esfex/visualization/data/gui_model.py`.

Every model method records an undo step (steps closer than 0.3 s apart are merged), so script changes can be reverted with **Edit > Undo** (`Ctrl+Z`). Wrap a batch in `with model.suspend_checkpoints():` to record it as a single step.

#### Key Model Methods

| Method | Returns | Description |
|--------|---------|-------------|
| `add_node(name="")` | node index | Create a node (no coordinates; set the centroid with `update_node`) |
| `update_node(index, **kwargs)` / `remove_node(index)` / `get_node(index)` | | Node operations |
| `add_bus(parent_node=0, name="", bus_id=None, **kwargs)` | bus id | Create a bus in a node (kwargs: `voltage_kv`, `latitude`, `longitude`, `role`, `bus_type`, ...) |
| `update_bus(bus_id, **kwargs)` / `remove_bus(bus_id)` | | Bus operations |
| `add_generator_instance(unit_key, name, gen_type, fuel, node=0, bus=None, **params)` | instance id | Create a generator. With no `bus`, it attaches to the first bus of `node`. |
| `update_generator(instance_id, **kwargs)` / `remove_generator(instance_id)` | | Generator operations |
| `add_battery_instance(unit_key, name, node=0, bus=None, **params)` | instance id | Create a storage unit |
| `update_battery(instance_id, **kwargs)` / `remove_battery(instance_id)` | | Storage operations |
| `add_line(from_node=0, to_node=0, capacity_mw=100.0, line_id=None, waypoints=None, from_endpoint=None, to_endpoint=None, from_bus=None, to_bus=None)` | line id | Create a transmission line. Pass `from_bus`/`to_bus` to connect specific buses. |
| `update_line(line_id, **kwargs)` / `remove_line(line_id)` / `remove_lines(line_ids)` | | Line operations |
| `add_transformer(name, from_node=0, to_node=-1, from_bus=None, to_bus=None, **kwargs)` / `remove_transformer(index)` | index | Transformers |
| `add_electrolyzer_instance(...)`, `add_fuel_storage(...)`, `add_fuel_route(...)`, `add_fuel_entry(...)`, `add_zone(...)`, `add_fuel(...)`, `add_technology(...)`, `add_investment_entry(...)` | | Other element types, each with matching `update_*`/`remove_*` methods where the element has an id |
| `undo()` / `redo()` | bool | Same as **Edit > Undo / Redo** |

Elements created from a script are not auto-wired. Placing a generator on the map builds the line, LV bus and transformer chain to the grid; `add_generator_instance` only attaches the unit to a bus.

### The `state` Object

Read access to the active system's data (`model.state` has the same attributes):

| Attribute | Type | Description |
|-----------|------|-------------|
| `nodes` | `list[GuiNode]` | All nodes; list position = node index |
| `buses` | `dict[str, GuiBus]` | All buses, keyed by bus id |
| `generators` | `dict[str, GuiGeneratorInstance]` | All generators, keyed by instance id |
| `batteries` | `dict[str, GuiBatteryInstance]` | All storage units, keyed by instance id |
| `transmission_lines` | `list[GuiTransmissionLine]` | All lines (each has a `line_id`) |
| `transformers` | `list[GuiTransformer]` | All transformers |
| `acdc_converters` / `freq_converters` | `list` | Converters |
| `electrolyzers` | `dict[str, GuiElectrolyzerInstance]` | All electrolyzers |
| `development_zones` | `list[GuiDevelopmentZone]` | All development zones |
| `fuel_entry_points` | `list[GuiFuelEntryPoint]` | All fuel sources |
| `fuel_storages` | `dict[str, GuiFuelStorage]` | All fuel stores |
| `fuel_transport_routes` | `list[GuiFuelTransportRoute]` | All fuel routes |
| `fuels` | `dict[str, GuiFuel]` | Fuel definitions |
| `technologies` / `investment_portfolio` | `dict` | Technologies and investment entries |
| `settings` / `penalties` | dataclasses | System settings and penalties |

Commonly used element attributes:

- Generator: `name`, `gen_type`, `fuel`, `bus`, `rated_power`, `min_power`, `fuel_cost`, `fixed_cost`, `maintenance_cost`, `eff_at_rated`, `life_time`, `latitude`, `longitude`, `style`.
- Storage: `name`, `bus`, `rated_power`, `capacity`, `efficiency_charge`, `efficiency_discharge`, `soc_initial`, `life_time`.
- Line: `line_id`, `from_bus`, `to_bus`, `capacity_mw`, `voltage_kv`, `length_km`, `num_circuits`.
- Node: `index`, `name`, `centroid_lat`, `centroid_lng`, `reserve_static`, `reserve_dynamic`.
- Bus: `bus_id`, `name`, `parent_node`, `voltage_kv`, `role`, `bus_type`, `demand_fraction`, `latitude`, `longitude`.

The `node` field on generators and storage (and `from_node`/`to_node` on lines) is deprecated and derived from the bus.

### The `window` Object

Access to UI components for advanced scripting:

| Attribute | Type | Description |
|-----------|------|-------------|
| `window.map_widget` | `MapWidget` | The Leaflet.js map widget |
| `window.element_tree` | `ElementTreePanel` | The element tree panel |
| `window.properties_panel` | `PropertiesPanel` | The right-side properties panel |
| `window.console` | `PythonConsole` | The Python console itself |
| `window.script_editor` | `ScriptEditor` | The script editor widget |
| `window.run_output` | `RunOutputView` | The **Run Output** terminal tab |
| `window._all_states` | `dict[str, GuiSystemState]` | All systems, keyed by name (internal) |
| `window._current_system_name` | `str` | Name of the active system (internal) |

`window._all_states` is synced with `model.state` when you switch systems, save or run, so after an undo the entry for the active system can be stale. For multi-system scripts, build the dict like this:

```python
states = dict(window._all_states)
states[window._current_system_name] = model.state
```


---


## Scripting API (`esfex`)

The `esfex` object wraps the runner, result loading and the standalone workflow models. `esfex.help()` prints a quick reference.

| Member | Description |
|--------|-------------|
| `esfex.version` | ESFEX version string |
| `esfex.available_solvers` | Installed solver Python packages (HiGHS, Gurobi, CPLEX, SCIP, CBC, GLPK) |
| `esfex.load_config(path)` | Load and validate a YAML file; returns `ESFEXConfig` |
| `esfex.run(config, output_dir="results", years=None, start_year=None)` | Full simulation (master problem + dispatch) in the Studio process. `config` is an `ESFEXConfig` or a YAML path. `start_year` defaults to the year of `date_start`. Returns a list of `YearResults` (`year`, `objective`, `re_penetration`, ...); raises `RunAbortedError` (a `RuntimeError`) if the master problem finds no plan. The GUI is blocked until it finishes. |
| `esfex.sensitivity(config_path, parameters=None, n_samples=64, output_dir="sensitivity_output", years=None)` | Sobol sensitivity analysis (config mode). `parameters` is a list of `{"name", "lower", "upper"}` dicts, `name` being a config parameter key such as `"fuel_cost"` (default bounds from the predefined set); `None` uses all predefined config parameters. `years` per run defaults to 25. |
| `esfex.export_results(h5_path, output_dir, fmt="csv")` | Export an HDF5 results file to `"csv"`, `"excel"` or `"json"` |
| `esfex.load_results(path, years=None)` | Read an HDF5 results file into a dict of numpy arrays keyed by year (see [Loading Results](#loading-results)) |
| `esfex.plot.generation_mix(results, year)`, `.prices(results, year)`, `.investments(results)`, `.load_duration(results, year)` | Matplotlib charts from a `load_results()` dict |
| `esfex.solve_master(config, solver=None, years=None, start_year=2025, output_dir="results")` | Master problem only (capacity expansion), prepared exactly like `esfex.run()`. Returns a dict with `investments`, `retirements`, `cumulative_capacities` and `re_targets`, each keyed by year (investment keys as in the HDF5 `investment_*` attributes, for example `tech_investment_power_0_3`). The config runs in `development` mode whatever its `simulation_mode`. Raises `RuntimeError` if the master problem fails. |
| `esfex.solve_dispatch(config, solver=None, year=None, output_dir="results")` | Dispatch of one year with the units in the config, no master problem (investment candidates are not built). `year` is a data year (default: the year of `date_start`): year *Y* reads the (*Y* − year of `date_start`)-th 8,760-h block of the demand and profile files and is labelled *Y*; outage windows count from 1 January of the year of `date_start`, as in a full run. A year before `date_start` raises `ValueError`. A `development` config is dispatched in `economic_dispatch` mode; `unit_commitment` and `economic_dispatch` configs keep their mode. Returns the year's `YearResults` and writes an HDF5 file to `output_dir`. |
| `esfex.workflows.*` | Standalone models: `wind_weibull`, `wind_financials`, `solar_financials`, `solar_peak_sun_hours`, `system_financials`, `financial_monte_carlo`, `hazard_screening`, `risk_assessment`, `ev_profiles`, `estimate_demand`, `climate_demand_adjustment`, `otec_capacity_factors`, `tsam_cluster`. `esfex.workflows.help()` lists their arguments. `estimate_demand(iso3, latitude, longitude, year, temperatures_hourly=None, annual_demand_mwh=None, static_features=None)` returns the 8,760-h demand profile of a site from ESFEX's national demand model (the Demand Estimation fallback model, with the features described in [Demand Distribution](../workflows/demand-distribution.md)): MW summing to `annual_demand_mwh` when given, else shape factors with mean 1. `temperatures_hourly` is on the site's local time (None: 20 °C); `static_features` (`log_gdp_per_cap`, `log_pop_density`, `urbanization`) skips sampling the rasters at the site. `tsam_cluster(demand, n_periods=10, method="kmedoids")` clusters an hourly demand series (or one column per node) into representative days and returns a `TSAMResult`: `period_start_hours`, `period_weights` (days each period stands for), `day_period` (each day's representative period, in calendar order), `num_periods`, `critical_periods`. |

`solver` overrides the config's `solver.name` (default: keep it). Like `esfex.run()`, both solvers run in the Studio process and block the GUI until they finish.

### Loading Results

`esfex.load_results(path, years=None)` reads a runner HDF5 file (layout in [HDF5 Output Schema](../reference/hdf5-output-schema.md)). `years` limits it to some years; for large runs, reading every year needs a lot of memory.

| Key | Content |
|-----|---------|
| `years` | Years found in `detailed_results`, sorted |
| `generation[year]` | `[generator, step]` MW, summed over nodes; rows follow `generator_names` (zeros for a unit absent that year) |
| `storage[year]` | `[battery, step]` state of charge (MWh), summed over nodes; rows follow `battery_names`. Absent when there are no batteries |
| `demand[year]` | `[node, step]` MW (also read correctly from older files that stored `[step, node]`) |
| `prices[year]` | `[node, step]` nodal prices ($/MWh); `[1, step]` mean price if the file has no nodal prices |
| `power_flow[year]` | `[node, node, step]` MW |
| `curtailment[year]` | `[node, step]` MW |
| `investments[year]` | `{technology name: MW}`: new generation and battery power capacity built that year, summed over nodes |
| `summary[year]` | Scalars of the year: `total_cost`, `renewable_penetration`, `co2_emissions`, `loss_of_load`, `load_shed`, `total_generation`, `total_demand`, `solve_time`, `master_re_target`, `feasible`, `n1_security_cost`, `transmission_losses` (MWh lost on the lines; files from this version on) |
| `generator_names`, `battery_names` | Dataset names of the units in `generation/` and `battery_soc/` |

For node-level generation or any other dataset, open the file with `h5py`, or use the **Results** dialog.


---


## Example: System Summary

```python
st = model.state
print(f"System: {window._current_system_name}")
print(f"Nodes: {len(st.nodes)}")
print(f"Buses: {len(st.buses)}")
print(f"Generators: {len(st.generators)}")
print(f"Total generation capacity: {sum(g.rated_power for g in st.generators.values()):.0f} MW")
print(f"Storage units: {len(st.batteries)}")
print(f"Total storage capacity: {sum(b.capacity for b in st.batteries.values()):.0f} MWh")
print(f"Lines: {len(st.transmission_lines)}")
print(f"Fuel sources: {len(st.fuel_entry_points)}")
```


---


## Example: Batch Import Generators from CSV

```python
import csv

count = 0
with open("generators.csv") as f:
    for row in csv.DictReader(f):
        model.add_generator_instance(
            unit_key=row["name"].lower().replace(" ", "_"),
            name=row["name"],
            gen_type=row["type"],
            fuel=row["fuel"],
            node=int(row["node"]),
            rated_power=float(row["rated_power_mw"]),
            life_time=int(row.get("lifetime", 25)),
            fuel_cost=float(row.get("fuel_cost", 0)),
            latitude=float(row["lat"]),
            longitude=float(row["lng"]),
        )
        count += 1

print(f"Imported {count} generators")
```

**Expected CSV format:**
```csv
name,type,fuel,node,rated_power_mw,lifetime,fuel_cost,lat,lng
Solar Farm A,Renewable,Sun,0,50,25,0,-21.10,55.53
Diesel Plant B,Non-renewable,Diesel,1,20,30,65,-21.15,55.45
Wind Farm C,Renewable,Wind,2,80,20,0,-21.34,55.47
```

`gen_type` is `Renewable` or `Non-renewable`. `fuel` should match a fuel defined under **Fuels** in the system. Each generator attaches to the first bus of its node, so create the buses first.


---


## Example: Scale All Capacities

```python
FACTOR = 1.5
st = model.state

with model.suspend_checkpoints():          # one undo step for the whole batch
    for gen_id, gen in st.generators.items():
        model.update_generator(gen_id, rated_power=gen.rated_power * FACTOR)

    for bat_id, bat in st.batteries.items():
        model.update_battery(bat_id,
            rated_power=bat.rated_power * FACTOR,
            capacity=bat.capacity * FACTOR)

print(f"All capacities scaled by {FACTOR}x")
```


---


## Example: Create Grid from Adjacency Matrix

```python
import numpy as np

# Nodes: (latitude, longitude, name)
sites = [
    (-21.10, 55.53, "Saint-Denis"),
    (-21.15, 55.45, "Le Port"),
    (-21.34, 55.47, "Saint-Louis"),
    (-21.28, 55.70, "Saint-Benoit"),
]

# Adjacency matrix: capacity in MW (0 = no connection)
adjacency = np.array([
    [0,   300, 0,   200],
    [300, 0,   250, 0  ],
    [0,   250, 0,   150],
    [200, 0,   150, 0  ],
])

# One node and one 63 kV load bus per site
buses = []
for lat, lng, name in sites:
    idx = model.add_node(name=name)
    model.update_node(idx, centroid_lat=lat, centroid_lng=lng)
    buses.append(model.add_bus(parent_node=idx, name=f"{name} 63 kV",
                               voltage_kv=63.0, role="load",
                               latitude=lat, longitude=lng))

# Lines from the upper triangle of the adjacency matrix
line_count = 0
for i in range(len(sites)):
    for j in range(i + 1, len(sites)):
        if adjacency[i, j] > 0:
            model.add_line(from_bus=buses[i], to_bus=buses[j],
                           capacity_mw=float(adjacency[i, j]))
            line_count += 1

print(f"Created {len(sites)} nodes and {line_count} lines")
```


---


## Example: Color Generators by Fuel

```python
from esfex.visualization.data.gui_model import VisualStyle

COLORS = {
    "Sun": "#f39c12",      # Orange
    "Wind": "#3498db",     # Blue
    "Diesel": "#7f8c8d",   # Gray
    "Gas": "#e67e22",      # Dark orange
    "Water": "#1abc9c",    # Teal
    "Biomass": "#27ae60",  # Green
}

for gen_id, gen in model.state.generators.items():
    color = COLORS.get(gen.fuel, "#95a5a6")
    size = max(8, gen.rated_power * 0.05)  # Scale marker with capacity
    model.update_generator(gen_id, style=VisualStyle(color=color, size=size))

print(f"Colored {len(model.state.generators)} generators")
```


---


## Example: Export System Data to Excel

```python
import pandas as pd

st = model.state

gen_data = [{
    "Name": g.name,
    "Type": g.gen_type,
    "Fuel": g.fuel,
    "Bus": g.bus,
    "Rated Power (MW)": g.rated_power,
    "Fuel Cost": g.fuel_cost,
    "Efficiency at Rated": g.eff_at_rated,
    "Lifetime (years)": g.life_time,
} for g in st.generators.values()]

bat_data = [{
    "Name": b.name,
    "Bus": b.bus,
    "Power (MW)": b.rated_power,
    "Capacity (MWh)": b.capacity,
    "Charge Eff.": b.efficiency_charge,
    "Discharge Eff.": b.efficiency_discharge,
} for b in st.batteries.values()]

line_data = [{
    "Line ID": l.line_id,
    "From bus": l.from_bus,
    "To bus": l.to_bus,
    "Capacity (MW)": l.capacity_mw,
    "Voltage (kV)": l.voltage_kv,
} for l in st.transmission_lines]

with pd.ExcelWriter("system_export.xlsx") as writer:
    pd.DataFrame(gen_data).to_excel(writer, sheet_name="Generators", index=False)
    pd.DataFrame(bat_data).to_excel(writer, sheet_name="Storage", index=False)
    pd.DataFrame(line_data).to_excel(writer, sheet_name="Lines", index=False)

print("Exported to system_export.xlsx")
```


---


## Example: Find Undersized Lines

```python
THRESHOLD_MW = 100  # Flag lines below this capacity

undersized = [(ln.line_id, ln.capacity_mw)
              for ln in model.state.transmission_lines
              if ln.capacity_mw < THRESHOLD_MW]

if undersized:
    print(f"Found {len(undersized)} lines below {THRESHOLD_MW} MW:")
    for lid, cap in sorted(undersized, key=lambda x: x[1]):
        print(f"  {lid}: {cap:.0f} MW")
else:
    print(f"All lines are at or above {THRESHOLD_MW} MW")
```


---


## Example: Compute Generation Mix

```python
from collections import defaultdict

capacity_by_fuel = defaultdict(float)
for gen in model.state.generators.values():
    capacity_by_fuel[gen.fuel] += gen.rated_power

total = sum(capacity_by_fuel.values())
print(f"\nGeneration Mix ({total:.0f} MW total)")
print("-" * 40)
for fuel, cap in sorted(capacity_by_fuel.items(), key=lambda x: -x[1]):
    pct = cap / total * 100 if total > 0 else 0
    bar = "#" * int(pct / 2)
    print(f"  {fuel:15s} {cap:8.1f} MW ({pct:5.1f}%) {bar}")
```


---


## Example: Run a Simulation from a Script

The **Run** toolbar button is the usual way to simulate: it runs the CLI in the **Run Output** tab without blocking the GUI. To run from a script instead (for example inside a parameter loop), export the GUI state to YAML the same way Run does and pass it to `esfex.run()`:

```python
import tempfile
from pathlib import Path
from esfex.visualization.data.serializer import gui_state_to_yaml

states = dict(window._all_states)
states[window._current_system_name] = model.state
base = window._loaded_config or window._create_default_config()

yaml_path = Path(tempfile.mkdtemp()) / "gui_export.yaml"
gui_state_to_yaml(
    states, base, yaml_path,
    inter_system_links=model.inter_system_links,
    global_settings=model.global_settings,
    stochastic_scenarios=model.stochastic_scenarios,
)

results = esfex.run(yaml_path, output_dir="results", years=5)   # blocks the GUI
for yr in results:
    print(f"Year {yr.year}: Cost=${yr.objective:,.0f}  RE={yr.re_penetration:.1%}")
```


---


## Example: Bulk Update from External Database

```python
import sqlite3

conn = sqlite3.connect("plant_database.db")
cursor = conn.execute("""
    SELECT name, fuel_cost, efficiency, lifetime
    FROM generators
    WHERE country = 'Cuba'
""")

by_name = {g.name.lower(): gid for gid, g in model.state.generators.items()}

updated = 0
for db_name, fuel_cost, efficiency, lifetime in cursor:
    gen_id = by_name.get(db_name.lower())
    if gen_id is None:
        continue
    model.update_generator(gen_id,
        fuel_cost=fuel_cost,
        eff_at_rated=efficiency,
        life_time=lifetime)
    updated += 1

conn.close()
print(f"Updated {updated} generators from database")
```


---


## Example: Inject Variables for Interactive Exploration

```python
# In a script: compute something
gens = list(model.state.generators.values())
capacities = np.array([g.rated_power for g in gens])

# Make results available in the console
window.console.update_namespace(
    capacities=capacities,
    gen_names=[g.name for g in gens],
    gen_fuels=[g.fuel for g in gens],
)

print("Variables 'capacities', 'gen_names', 'gen_fuels' are now available in the console")
```

Variables defined at the top level of a script are already shared with the console; `update_namespace` is useful to set them from inside functions.


---


## Example: Generate a Matplotlib Chart

```python
import matplotlib.pyplot as plt
from collections import defaultdict

capacity_by_fuel = defaultdict(float)
for gen in model.state.generators.values():
    capacity_by_fuel[gen.fuel] += gen.rated_power

fuels = list(capacity_by_fuel.keys())
caps = [capacity_by_fuel[f] for f in fuels]

fig, ax = plt.subplots(figsize=(10, 5))
ax.barh(fuels, caps)
ax.set_xlabel("Installed Capacity (MW)")
ax.set_title(f"Generation Capacity - {window._current_system_name}")
plt.tight_layout()
plt.savefig("capacity_chart.png", dpi=150)
plt.show()
print("Chart saved to capacity_chart.png")
```


---


## Tips and Best Practices

- **Save before running destructive scripts.** Model methods record undo steps, but the undo history has a limited depth (**File > Preferences... > General > Behaviour > Undo history depth**, default 50).
- **Use the `model` methods** for all modifications. Direct attribute changes on state objects do not refresh the UI.
- **Use `model.state`** rather than `state` in scripts, so the script still works after an undo.
- **Check attribute names.** `update_*` methods ignore unknown keyword names silently.
- **Any installed Python library** is available, for example `numpy`, `pandas`, `matplotlib`, `scipy`, `networkx`, `geopandas`.
- **Multi-system scripts**: `window._all_states` holds every system; see [The `window` Object](#the-window-object).
- **Test on a small subset first** before applying to the full system.


---


## Error Handling

Exceptions print the full traceback in the console. The editor remains functional.

```python
# The console shows the full traceback, e.g.:
# Traceback (most recent call last):
#   File "<script>", line 1, in <module>
#     model.state.generators["nonexistent_id"]
# KeyError: 'nonexistent_id'
```

Model methods do not raise for an unknown id or attribute name: `model.update_generator("nonexistent_id", rated_power=100)` does nothing. Validate inputs yourself when that matters:

```python
st = model.state
changes = {"diesel_bus_3": {"rated_power": 40.0}, "pv_bus_7": {"ramp_up": 1.0}}

for gen_id, kwargs in changes.items():
    gen = st.generators.get(gen_id)
    if gen is None:
        print(f"  unknown generator: {gen_id}")
        continue
    unknown = [k for k in kwargs if not hasattr(gen, k)]
    if unknown:
        print(f"  {gen_id}: unknown attributes {unknown}")
        continue
    model.update_generator(gen_id, **kwargs)
```


---


## Subprocess Execution

`window.console.run_subprocess(args, label="process", env=None)` runs an external command with `QProcess` and streams its output into the Python REPL tab. Only one subprocess can run at a time; `Ctrl+C` in the console (with no text selected) cancels it. The toolbar **Run** button does not use this path: it runs the simulation in the **Run Output** terminal tab.
