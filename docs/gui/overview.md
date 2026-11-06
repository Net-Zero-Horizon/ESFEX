# Overview
Desktop application for building, editing, and validating power system configurations on an interactive geographic map. Built on PySide6 with an embedded Leaflet.js map for designing multi-system power networks, running simulations, and reviewing results without manual YAML editing.


---


## Installation and Launch

```bash
# Install ESFEX (PySide6 and QtWebEngine are core dependencies)
pip install esfex

# Start with a blank project
esfex studio

# Open an existing configuration
esfex studio -c config.yaml
```

### System Requirements

- Python 3.10 or later
- PySide6 6.5+ (included in the core install)
- A display capable of 1280x720 or higher (1920x1080 recommended)
- Internet connection for the online base maps and for workflow data fetching. The **Offline** base map works without a connection.


---


## Window Layout

Three resizable columns separated by splitter handles. The left, right, and bottom panels each have a collapse button on their splitter handle.

```
+------------------+----------------------------+------------------+
|                  |         Toolbar            |                  |
|   Element Tree   +----------------------------+  Properties      |
|   (left panel)   |                            |  Panel           |
|                  |      Map / SLD View        |  (right panel)   |
|                  |                            |                  |
|                  +----------------------------+                  |
|                  | Run Output | Python REPL   |                  |
|                  | (tabs)     | Script Editor |                  |
+------------------+----------------------------+------------------+
```

### Element Tree (Left Panel)

Hierarchical overview of every object in the project:

- **Global Settings** -- Simulation-wide parameters (solver, temporal resolution, N-1 security, master problem). Always at the top.
- **Scenarios** -- Stochastic scenario definitions with probabilities and cost multipliers.
- **Inter-system Links** -- Cross-system links, split into **Transmission Lines** and **Fuel Routes**.
- **Geo Assets** -- Imported geographic files shown as reference overlays (see [Importing Geographic Data](map-editor.md#importing-geographic-data)).
- **Each System** -- A bold top-level item, expandable into component categories:
  - Nodes, Generators, Energy Storage, Transmission Lines, Transformers, Buses, AC/DC Converters, Freq. Converters, Development Zones, Fuel Sources, Fuel Storages, Fuel Routes, Fuels, Electrolyzers, EV Configuration, Rooftop Solar, Technologies, Investment Portfolio, Risk & Resilience

Each category displays an element count in parentheses, e.g. "Generators (12)". Clicking an item opens its property form; double-clicking centers the map on it. The search box at the top filters by name. The tree supports multi-selection (Ctrl+Click, Shift+Click) of elements of one type for batch editing.

### Map and SLD View (Center Panel)

Two tabbed views:

- **Geographic View** -- The interactive Leaflet.js map where you place, edit, and connect elements. Supports several base maps and layer filtering.
- **Single-Line Diagram** -- An auto-generated electrical schematic of the current system, with an analysis mode for stepping through dispatch results. See [Single-Line Diagram](sld-dynamic-analysis.md).

Below the map/SLD tabs, a horizontal splitter divides:

- **Run Output / Python REPL** (left) -- Two tabs. **Run Output** is a terminal that shows the output of simulations started with **Run**; a **■ Stop** button appears in the tab corner while a run is active. **Python REPL** is an interactive console with access to the data model.
- **Script Editor** (right) -- Editor with syntax highlighting for automation scripts. Scripts run in the Python REPL.

### Properties Panel (Right Panel)

Editable form for the currently selected element, organized in tabs and group boxes. Changes are reflected on the map and in the tree. When several elements of the same type are selected in the tree, the panel shows a batch-edit form.


---


## Toolbar

Drawing modes, layer controls, and analysis actions. Drawing mode buttons are mutually exclusive. Toolbar icons adapt to the current theme.

### Drawing Modes

| Button | Mode | What it does |
|--------|------|-------------|
| **Select** | `select` | Default pointer mode. Click to select, drag markers to move them |
| **Line** | `add_line` | Trace a transmission line between two buses or pieces of equipment |
| **Generator** | `add_generator` | Click to place a generator; it is auto-connected to the nearest high-voltage bus |
| **Storage** | `add_battery` | Click to place a battery; auto-connected like a generator |
| **Transformer** | `add_transformer` | Click to place a transformer |
| **Bus** | `add_bus` | Click to place a bus (an electrical connection point) |
| **Dev. Zone** | `draw_zone` | Draw a polygon to define a development zone |
| **AC/DC Conv.** | `add_acdc_converter` | Click to place an AC/DC converter |
| **Freq. Conv.** | `add_freq_converter` | Click to place a frequency converter |
| **Electrolyzer** | `add_electrolyzer` | Click to place an electrolyzer; auto-connected like a generator |
| **Fuel Source** | `add_fuel_entry` | Click to place a fuel source (import point) |
| **Fuel Store** | `add_fuel_storage` | Click to place a fuel storage facility |
| **Fuel Route** | `add_fuel_route` | Trace a fuel transport route between fuel sources and storages |

Drawing mode buttons are disabled until at least one system exists. Nodes are not placed on the map: add them from the Element Tree (right-click **Nodes > Add Node**). See [Map Editor](map-editor.md) for how placement and auto-connection work.

### Layer and Base Map Selectors

Two dropdowns to the right of the action buttons:

- **Layer** -- Filter visible elements: All, Electrical, Primary Energy, or Results.
- **Base Map** -- Switch the background: OpenStreetMap, Satellite, Terrain, Dark, or Offline.

### System Management

| Button | What it does |
|--------|-------------|
| **Add System** | Opens a name dialog and creates a new system with one node ("Node 0") |

### Analysis Actions

| Button | What it does |
|--------|-------------|
| **Validate** | Check the systems selected in Global Settings > Systems to Simulate (all systems if none are selected) |
| **Run** | Launch the simulation (enabled after a validation with no errors) |
| **Sensitivity** | Open the sensitivity analysis dialog (enabled after a successful run or after loading results) |
| **Results** | Open the results viewer and switch the map to the Results layer (enabled after a successful run or after loading results) |
| **Risk and Resilience** | Open the Risk & Resilience workbench |

Press `Escape` to cancel the current drawing operation and return to selection mode.


---


## Menus

| Menu | Items |
|------|-------|
| **File** | New Scenario, Import Configuration…, Add System from Config…, Import Geo Asset…, Import Project…, Save, Export As…, Export Project…, Open Recent, Load Results…, Preferences… |
| **Edit** | Undo, Redo, Custom Constraints…, Interruptions… |
| **Workflows** | Grid Builder…, Solar Rooftop Analysis…, OTEC Studio, Wind Resource Assessment…, Solar PV Assessment…, EV & V2G Assessment…, Financial Analysis… |
| **Plugins** | Manage Plugins…, plus entries added by plugins |
| **Help** | Documentation…, Keyboard Shortcuts…, About ESFEX… |

**Custom Constraints…** and **Interruptions…** open the dialogs described in [Custom Constraints](../user-guide/custom-constraints.md) and [Scheduled Interruptions](../user-guide/scheduled-interruptions.md). The workflows are described in [Analysis Workflows](../workflows/index.md).


---


## Basic Workflow

Typical session steps:

1. **Create a system** -- Click **Add System** and enter a name (e.g., "Cuba_Western_Grid"). The system starts with one node, "Node 0".
2. **Add nodes** -- Right-click **Nodes > Add Node** in the tree for each additional region. In each node's form, click **Place** next to *Centroid* and click the map to give the node a geographic reference, and load its demand in the **Demand** tab.
3. **Place buses** -- Use **Bus** mode to place the high-voltage buses of the network. A bus belongs to the node whose centroid is nearest.
4. **Connect buses** -- Use **Line** mode: click a bus, optionally click waypoints, then click the destination bus.
5. **Add generation and storage** -- Use **Generator** or **Storage** mode and click where the plant is. The element is created at that point and, if buses exist, connected to the nearest high-voltage bus through a new low-voltage bus and a transformer. Configure it in the Properties Panel.
6. **Add investment candidates** -- Right-click **Investment Portfolio** to add generator, storage, or transmission candidates with per-node costs and limits.
7. **Add fuel infrastructure** -- If primary energy is enabled, place fuel sources, fuel storage, and fuel routes, and define fuels under **Fuels**.
8. **Configure system settings** -- Click a system name in the tree to set RE targets, cost limits, penalties, and the discount rate.
9. **Configure global settings** -- Click **Global Settings** to set the simulation mode, solver, temporal resolution, power flow mode, and N-1 parameters.
10. **Validate** -- Click **Validate** and run the checks.
11. **Fix errors** -- Double-click an issue in the validation dialog to go to the affected element.
12. **Run simulation** -- Click **Run**. Output appears in the **Run Output** tab.
13. **Review results** -- Click **Results** to open the charts and the map results panel.
14. **Save** -- **Save** (`Ctrl+S`) writes the configuration YAML, which `esfex run` can use directly.


---


## File Operations

| Menu item | Shortcut | Description |
|-----------|----------|-------------|
| New Scenario | `Ctrl+N` | Asks "Discard current work and create a new empty scenario?" (Yes/No; it does not offer to save) and starts a project with one system, "System_1", containing "Node 0". |
| Import Configuration… | `Ctrl+O` | Load a YAML configuration. Replaces the current project. |
| Add System from Config… | -- | Add the systems of another YAML to the current project. See [System Management](system-management.md#adding-systems-from-another-configuration). |
| Import Geo Asset… | -- | Load a GeoJSON, Shapefile, KML, KMZ, or GeoPackage file as a reference overlay. See [Map Editor](map-editor.md#importing-geographic-data). |
| Import Project… | -- | Extract a `.esfexp` project bundle into a folder you choose and open its configuration. |
| Save | `Ctrl+S` | Write the configuration to the current file. If the project has no file yet, Save behaves like Export As. |
| Export As… | `Ctrl+Shift+S` | Choose a path and write the configuration there. The file becomes the current file. |
| Export Project… | -- | Write a portable `.esfexp` bundle: a zip with `config.yaml`, `manifest.json`, and copies of every referenced data file in `demand/`, `availability/`, `inflow/`, and `systems/`, with the paths in the config rewritten to those folders. |
| Open Recent | -- | Reopen a recently used configuration. |
| Load Results… | `Ctrl+R` | Open an existing HDF5 results file and show it in the results viewer. |

Save and Export As write the same YAML, which includes map coordinates, line waypoints, and marker styles. The file can be run with `esfex run` as is. Before writing, both run an automatic validation; if it finds errors, a dialog asks "Save anyway?".


---


## Map Layers

Controlled by the Layer dropdown in the toolbar:

- **All** -- Every element.
- **Electrical** -- Generators, batteries, transmission lines, transformers, buses, converters, electrolyzers, and development zones, plus results overlays if any. Fuel sources and fuel storages are hidden.
- **Primary Energy** -- Fuel sources, fuel storages, and development zones. Electrical equipment is hidden.
- **Results** -- Only the results overlays (node circles, pie charts, and flow lines) drawn from the **Map Results** panel. Equipment is hidden.

Fuel routes are not part of any layer filter and stay visible in every layer.


---


## Themes

Available under **File > Preferences… > General > Appearance > Theme**:

- **Light**
- **GitHub Light** (default)
- **VS Code Dark+**
- **Dracula**
- **One Dark Pro**

Older theme names in a preferences file are mapped to the current ones (Dark to VS Code Dark+, Twilight to Dracula). Theme changes take effect when you close the Preferences dialog with OK, without restarting. The base map is independent of the theme.


---


## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Ctrl+N` | New Scenario |
| `Ctrl+O` | Import Configuration… |
| `Ctrl+S` | Save |
| `Ctrl+Shift+S` | Export As… |
| `Ctrl+R` | Load Results… |
| `Ctrl+,` | Preferences… |
| `Ctrl+Z` | Undo |
| `Ctrl+Y` | Redo |
| `F1` | Documentation… |
| `Escape` | Cancel the current drawing mode (map) |
| `Ctrl+C` | Stop the running simulation (when the Run Output tab has focus) |

There are no keyboard shortcuts for delete, duplicate, copy, or paste. Use the right-click menu on the map or in the Element Tree (**Duplicate**, **Copy Attributes**, **Paste Attributes**, **Delete**), the **Delete** button below the tree, or the delete button in the element's form.

File menu shortcuts and toolbar actions can be reassigned under **File > Preferences… > Shortcuts**.


---


## Validation

Click **Validate** in the toolbar to check the configuration before running a simulation. The validation dialog:

- Has one checkbox per check category: Structural, Electrical, Demand, Generation, Fuel Network, Connectivity, and Topology Audit (GUI vs solver agreement). Topology Audit reports electrical islands, including islands with demand but no reachable generation (an error).
- Has a **Network Simplification** checkbox that finds dead-end buses, stub lines, and unused fuel elements, and an **Apply Simplification** button to remove them.
- Has an optional **Network reduction (levels)** check with levels L1 (equipment aggregation and parallel lines) to L4 (full node collapse), which suggests merges you can apply selectively.
- Groups results by severity: errors, warnings, and info.
- Double-click an issue to go to the affected element.

Validation covers the systems in Global Settings > Systems to Simulate. When it finishes with no errors, **Run** is enabled and **Validate** is disabled until the model changes. Warnings do not block the run.


---


## Running a Simulation

**Run** writes the project to a temporary YAML file and starts `python -m esfex.cli run --config <tmp> --output results` (with `--verbose` when Global Settings > Console Verbose is High) in the **Run Output** tab. The `results` folder is relative to the directory the Studio was started from.

- **■ Stop** in the tab corner, or `Ctrl+C` in the Run Output tab, sends an interrupt so the run can stop cleanly. Pressing Stop again force-kills the process.
- When the run finishes successfully, **Results** and **Sensitivity** are enabled, and the results viewer opens if Preferences > General > *Auto-open results after simulation* is on (the default).


---


## Results

**Results** switches the map to the Results layer and opens the results viewer:

- **Results dialog** -- Charts grouped in Generation, Storage, Economics, System, UC Operations, Near-Optimal (MGA), and Custom.
- **Map Results** panel (on the map) -- System, Year, Alternative, and Variable selectors and a Screenshot button. Variables include Installed Capacity Mix, Generation Mix, and RE vs Non-RE (pie charts); Total Generation, RE Penetration, Nodal Prices (LMP), Load Shedding, CO2 Emissions, Curtailment, Gen. Investment, and Bat. Investment (node circles); Power Flow and Fuel Transport Flow (lines); and three risk overlays.

**File > Load Results…** (`Ctrl+R`) opens a results file from an earlier run.


---


## Python Console

The **Python REPL** tab has these objects pre-loaded: `model`, `state`, `config`, `window`, `esfex` (the scripting API), and `np` (NumPy). Type `esfex.help()` for the API reference. See [Scripting & Automation](scripting.md) for details.


---


## Next Steps

- [Map Editor](map-editor.md) — placing and editing elements on the map
- [Element Forms](element-forms.md) — reference for all property fields
- [System Management](system-management.md) — working with multiple systems
- [Inter-System Links](inter-system-links.md) — connecting independent power systems
- [Global Settings](global-settings.md) — simulation parameters, solver, and penalties
- [Single-Line Diagram](sld-dynamic-analysis.md) — schematic view and dispatch analysis
- [Analysis Workflows](../workflows/index.md) — resource assessment, financial analysis, and other multi-step wizards
- [Plugin Management](plugins.md) — extending the editor with plugins
- [Scripting & Automation](scripting.md) — Python console usage and script examples
