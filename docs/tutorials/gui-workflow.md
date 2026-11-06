# GUI Workflow
## Prerequisites

Install ESFEX:

```bash
pip install esfex
```

This installs PySide6 and QtWebEngine. Requires Python 3.10+ and a display of at least 1280x720 (1920x1080 recommended).

---

## Step 1: Launch the Editor

```bash
# Start with a blank project
esfex studio

# Or edit an existing configuration
esfex studio -c existing_system.yaml
```

The editor window has three main panels:

- **Element Tree** (left) — hierarchical overview of all objects
- **Map / SLD View** (center) — interactive Leaflet.js map for placing and connecting elements, and the single-line diagram
- **Properties Panel** (right) — editable form for the selected element

Below the map, a tab widget holds **Run Output** (simulation output) and **Python REPL** (interactive console), with the **Script Editor** to the right. See [Overview](../gui/overview.md) for a full tour.

---

## Step 2: Create a New System

1. Click **Add System** in the toolbar.
2. Enter a name, for example `Cuba_Western_Grid`. System names must be unique; prefer underscores to spaces.
3. Click **OK**. The new system appears as a bold top-level item in the Element Tree, with its categories underneath (Nodes, Generators, Energy Storage, Transmission Lines, and so on). It already contains one node, "Node 0".

Drawing mode buttons become enabled once a system exists. The new system is also added to Global Settings > Systems to Simulate.

!!! tip "Multiple Systems"
    You can create more systems later for multi-system studies. They are solved together and can be connected with inter-system links (Step 12).

---

## Step 3: Add Nodes and Their Demand

A node is a geographic region (a substation area or load center). It holds demand, reserves, and losses, and owns one or more buses. Nodes have no marker on the map.

1. Right-click **Nodes** under your system and select **Add Node**. Repeat until you have three nodes (Node 0, Node 1, Node 2).
2. Click a node in the tree. In the **General** tab:
    - **Name**: a readable name, e.g. "Havana".
    - **Centroid (geographic reference)**: click **Place**, then click the map where the region is. Equipment and buses you place later are assigned to the node with the nearest centroid.
3. In the **Demand** tab, click **Load CSV...** and choose the node's hourly demand file (CSV or Excel). For a file with several columns, the column number is the node index. All nodes must have the same number of hours. The **Forecast** button can generate a demand series instead.

!!! tip "Map Navigation"
    Double-click an element in the Element Tree to center the map on it. Use the scroll wheel to zoom and drag to pan.

---

## Step 4: Place Buses

Buses are the electrical connection points. Lines, transformers, and equipment connect to buses.

1. Click **Bus** in the toolbar.
2. Click the map at each substation, at least one per node. Each bus joins the node with the nearest centroid; check **Parent Node** in the bus form and change it if needed.
3. In the bus form, set **Voltage (kV)** (new buses default to 220 kV), and set **Bus Type** to *slack* on one bus of the system (the others stay *PQ*).
4. Press **Escape** to return to selection mode.

**Role** (connection, load, or mixed) and **Demand Fraction** decide which buses carry the node's demand. When you save or run, ESFEX reassigns roles from the topology (buses with generation become *mixed*, the only bus of a node or a low-voltage bus becomes *load*) and spreads the node demand over its load and mixed buses if the fractions do not add up to 1.

---

## Step 5: Draw Transmission Lines

1. Click **Line** in the toolbar.
2. Click the **starting bus**. A dashed rubber-band line follows the cursor.
3. Click on empty map space to add **waypoints** that route the line (along roads, around obstacles).
4. Click the **destination bus** to finish the line.
5. Press **Escape** during the trace to cancel it.

Lines can start and end at buses, transformers, converters, or equipment; clicking a marker of an unsupported type does nothing.

### Configuring Line Properties

Click a line on the map or in the tree:

| Field | Description | Example |
|-------|-------------|---------|
| **From bus / To bus** | Line endpoints (editable) | bus_0 / bus_1 |
| **Capacity (MW)** | Thermal transfer capacity | 100.0 |
| **Voltage (kV)** | Nominal voltage | 220.0 |
| **Length (km)** | Physical length (computed from the trace) | 200.0 |
| **Reactance (pu)** | Per-unit reactance (DC power flow) | 0.05 |
| **Resistance (pu)** | Per-unit resistance | 0.01 |
| **Circuits** | Number of parallel circuits | 1 |

Draw three lines to connect the nodes:

- Node 0 bus to Node 1 bus: 100 MW
- Node 1 bus to Node 2 bus: 50 MW
- Node 0 bus to Node 2 bus: 75 MW (optional)

!!! tip "Parallel Lines"
    You can draw several lines between the same pair of buses. Each keeps its own `line_id` and capacity.

---

## Step 6: Add Generators

1. Click **Generator** in the toolbar.
2. Click the map where the plant is. The generator is created at that point and connected automatically to the nearest high-voltage bus through a new low-voltage bus, a transformer, and short lines.
3. The Properties Panel shows the generator form.

### Configuring Generator Properties

| Field | Description | Example |
|-------|-------------|---------|
| **Name** | Readable label | "Solar PV" |
| **Type** | Renewable or Non-renewable (new generators start as Renewable) | Renewable |
| **Fuel** | Energy source | Solar |
| **Technology** | Optional technology definition | (None) |
| **Availability** | Hourly capacity-factor CSV (values 0–1) | solar_profile.csv |
| **Rated Power (MW)** | Existing installed capacity (new generators start at 0) | 50.0 |
| **Life Time (years)** | Expected operational lifetime | 25 |
| **Initial Age (years)** | Current age of the unit | 0 |
| **Min Power (pu)** | Minimum stable output as a fraction of rated power | 0.0 |
| **Ramp Up / Ramp Down (pu/h)** | Ramp limit as a fraction of rated power per hour (1.0 = no practical limit) | 1.0 |
| **Eff. at Rated (frac)** | Efficiency at full load | 1.0 |
| **Fuel ($/MWh)** | Fuel cost (0 for renewables) | 0.0 |
| **Fixed ($/MWh)** | Fixed O&M cost | 5.0 |
| **Maintenance ($/MWh)** | Variable O&M cost | 2.0 |

The generator form has no investment fields. New capacity is defined in the Investment Portfolio (Step 8). See [Element Forms](../gui/element-forms.md#generator) for all fields, including the cost curve options.

For a three-node island system, add for example:

- **Node 0**: Solar PV (50 MW) + Diesel (30 MW)
- **Node 1**: Wind (20 MW) + Diesel (20 MW)
- **Node 2**: Diesel (40 MW) + Gas Turbine (25 MW)

Each generator appears under **Generators** in the Element Tree.

---

## Step 7: Add Storage

1. Click **Storage** in the toolbar.
2. Click the map. The battery is created and connected like a generator.
3. Configure it in the Properties Panel:

| Field | Description | Example |
|-------|-------------|---------|
| **Name** | Readable label | "Li-Ion 4h" |
| **Rated Power (MW)** | Existing power rating | 0 |
| **Capacity (MWh)** | Existing energy capacity | 0 |
| **Max Charge / Max Discharge (MW)** | Charging and discharging limits | 0 / 0 |
| **Min / Max Duration (h)** | Energy-to-power ratio bounds | 2.0 / 8.0 |
| **Charge Eff. / Discharge Eff. (frac)** | One-way efficiencies | 0.95 / 0.95 |
| **SOC Initial (frac)** | State of charge at the start of each year | 0.50 |
| **Max DoD (frac)** | Maximum depth of discharge | 0.90 |
| **Life Time (years)** | Expected lifetime | 15 |

!!! note "Battery state of charge"
    The state of charge is carried from one rolling-horizon window to the next. At the end of each year the battery must return to at least its initial state of charge (a soft constraint).

---

## Step 8: Define Investment Candidates

Investment options live in the **Investment Portfolio**, not in the equipment forms.

1. Right-click **Investment Portfolio** and select **Add Generator** (or **Add Battery**, **Add Transmission**, ...). A new entry is created with one row per node.
2. In the entry form, set the **Name**, the **Type**, and optionally a **Technology** (define technologies under **Technologies** with right-click **Add Technology**).
3. In the **Investment per Node** table, set the investment cost ($/MW) and the maximum new capacity (MW) for each node. Battery entries also have energy cost ($/MWh) and energy maximum (MWh) columns.

Example: a Solar PV entry with 700,000 $/MW and a 200 MW limit at Node 0, and a Wind entry with a 100 MW limit at Node 1.

---

## Step 9: Add Fuel Sources

Fuel sources define where fuel enters the network. They matter only when Global Settings > Primary Energy is on.

1. Define the fuels under **Fuels** (right-click **Add Fuel**).
2. Click **Fuel Source** in the toolbar and click the map at the import point (e.g., a port).
3. In the form, add the fuels available at this point and set their import limits and costs. See [Element Forms](../gui/element-forms.md#fuel-source).

---

## Step 10: Configure System Settings

Click the system name in the Element Tree. The form has three tabs.

### General

| Field | Description | Example |
|-------|-------------|---------|
| **Demand Scale (x)** | Multiplier applied to all demand | 1.0 |
| **Discount Rate (frac)** | Discount rate for NPV | 0.05 |
| **Target RE Penetration (frac)** | Final-year renewable share target | 0.80 |
| **Min / Max Annual Increment (frac)** | Bounds on the yearly RE increase | 0.01 / 0.10 |
| **Enable CO2 Budget** | Activate the CO2 budget (on by default) | on |
| **CO2 Annual Budget (tonnes/year)** | Maximum CO2 emissions per year | 500000 |

### Cost Limits

Max Annual System Cost, Max NPV Penalty, Max Decommission Cost, Force Replacement, and Life Extension Cost Factor.

### Penalties

| Field | Description | Example |
|-------|-------------|---------|
| **Loss of Load** | Value of lost load, $/MWh | 50000 |
| **Curtailment Cost ($/MWh)** | Cost of curtailing renewable output | 10 |
| **Max Curtailment Ratio** | Maximum fraction of renewable output that can be curtailed | 0.05 |
| **CO2 Cost** | Carbon price, $/tCO2 | 10 |

Criticality levels are multipliers on the loss-of-load value. See [Global & System Settings](../gui/global-settings.md#system-settings) for every field and its default. Power flow mode and slack bus are not system settings: the mode is in Global Settings, and the slack bus is chosen with Bus Type in the bus form.

---

## Step 11: Configure Global Settings

Click **Global Settings** at the top of the Element Tree. These apply to all systems. New projects take some defaults from **File > Preferences…** (resolution 6 h, overlap 24 h, primary energy off, time limit 3600 s, gap 0.001).

### Simulation

| Field | Description | Options |
|-------|-------------|---------|
| **Systems to Simulate** | Systems included in Validate and Run | checkboxes |
| **Simulation Mode** | Simulation type | Development (LP) or Unit Commitment (MIP) |
| **Start Date** | Simulation start date | 01/01/2025 00:00 |
| **Primary Energy** | Model fuel supply | on / off |

### Solver

| Field | Description | Example |
|-------|-------------|---------|
| **Solver** | Optimization solver | highs, gurobi, cplex, scip, xpress, cbc, glpk |
| **Threads** | Parallel solver threads | 4 |
| **Time Limit (s)** | Maximum solve time | 3600 |
| **Gap (frac)** | Optimality gap for MIP | 0.001 |

### Temporal

| Field | Description | Example |
|-------|-------------|---------|
| **Resolution (h)** | Time step, whole hours from 1 to 24 | 1 |
| **Use Rolling Horizon** | Solve the year in windows | on |
| **Rolling Horizon (h)** | Window size | 48 |
| **Overlap (h)** | Look-ahead solved but not kept in each window | 24 |

### Master Problem

| Field | Description | Example |
|-------|-------------|---------|
| **Representative Days** | Days per year used by the master problem | 5 |
| **Min Day Separation** | Minimum separation between representative days | 5 |
| **Enable MGA** | Near-optimal alternative exploration | off |

### N-1 Security

| Field | Description | Example |
|-------|-------------|---------|
| **Enable N-1 Security** | Activate contingency constraints | off |
| **Transmission** | Transmission contingencies | on |
| **Generation** | Generation contingencies | on |

See [Global & System Settings](../gui/global-settings.md) for the power flow, Master Problem, Risk & Resilience, and Visual Scaling options.

---

## Step 12: Add More Systems (Multi-System Workflow)

1. Click **Add System** in the toolbar and enter a name (e.g., "Isla_Juventud").
2. Build it with the same steps. All systems are shown on the map at once; clicking an element of a system makes that system active.

### Connecting Systems with Inter-System Links

1. Select **Line** in the toolbar.
2. Click a bus in the first system, add waypoints along the cable route, and click a bus in the second system.
3. Because the endpoints are in different systems, an inter-system link is created (100 MW by default). It appears under **Inter-system Links > Transmission Lines**.
4. Click the link to edit it:

| Field | Description | Example |
|-------|-------------|---------|
| **Capacity (MW)** | Existing interconnection capacity | 50.0 |
| **Length (km)** | Computed from the trace | 120.0 |
| **Investment cost ($/MW)** | Cost for capacity expansion | 500000.0 |
| **Max investment (MW)** | Maximum expansion | 200.0 |

See [Inter-System Links](../gui/inter-system-links.md) for details and current limitations.

---

## Step 13: Validate the Configuration

1. Click **Validate** in the toolbar.
2. The validation dialog has one checkbox per check category:
    - **Structural** (nodes, lines)
    - **Configuration** (keys of the opened YAML file that ESFEX ignores)
    - **Electrical** (buses, equipment references)
    - **Demand** (load data, coverage)
    - **Generation** (adequacy, availability files)
    - **Fuel Network** (supply chain integrity)
    - **Connectivity** (isolated components)
    - **Realism** (duplicate plants, reserve margin, deliverability of the peak demand through the line ratings)
    - **Topology Audit** (GUI vs solver agreement), which also reports load islands with no reachable generation

    There are also a **Network Simplification** check (dead-end buses and unused fuel elements) and an optional **Network reduction (levels)** check, L1 to L4.

3. Click **Run Validation**. Results are grouped by severity: errors, warnings, and info.
4. Double-click an issue to go to the affected element.

Validation covers the systems in Systems to Simulate. Fix all errors; warnings do not block the run. When there are no errors, **Run** is enabled.

---

## Step 14: Run the Simulation from the GUI

1. Click **Run** in the toolbar. It is enabled only after a validation with no errors.
2. The Studio writes the project to a temporary YAML file and runs `esfex run` in the **Run Output** tab, where the solver log appears.
3. To stop the run, click **■ Stop** in the tab corner or press `Ctrl+C` in the Run Output tab. A second Stop force-kills the process.
4. When the run finishes successfully, **Results** and **Sensitivity** are enabled. By default the results viewer opens automatically.

Alternatively, save the configuration and run it from the command line:

```bash
esfex run -c my_system.yaml --years 25 -v
```

---

## Step 15: View Results

1. Click **Results** in the toolbar. The map switches to the Results layer and the results dialog opens, with charts grouped in Generation, Storage, Economics, System, UC Operations, Near-Optimal, and Custom.
2. Use the **Map Results** panel on the map to choose the System, Year, and Variable to draw: capacity and generation mix pies, node values such as RE Penetration, Nodal Prices (LMP), and Load Shedding, and Power Flow lines.
3. Click **Sensitivity** in the toolbar to open the sensitivity analysis dialog.

**File > Load Results…** (`Ctrl+R`) opens a results file from an earlier run.

### Accessing Results Programmatically

Results are written to HDF5 in the `results` folder. A file is named `<config name>_<ddmmyyyy>_<HHMM>.h5`; runs started from the Studio use a temporary config, so their files are named `esfex_run_<random>_<ddmmyyyy>_<HHMM>.h5`. In the Python REPL:

```python
import glob
import h5py

path = sorted(glob.glob("results/*.h5"))[-1]   # most recent run
with h5py.File(path, "r") as f:
    s = f["summary_results"]
    for y, c, r in zip(s["year"][:], s["total_cost"][:], s["renewable_penetration"][:]):
        print(f"{int(y)}: cost = ${c:,.0f}, RE = {r:.1%}")
```

---

## Step 16: Save

| Action | Shortcut | What it writes |
|--------|----------|----------------|
| **Save** | `Ctrl+S` | The configuration YAML at the current path (asks for a path the first time) |
| **Export As…** | `Ctrl+Shift+S` | The same YAML at a new path, which becomes the current file |
| **Export Project…** | -- | A `.esfexp` bundle with the YAML and copies of all referenced data files |

Save and Export As write the same file, including map positions, line waypoints, and marker styles. `esfex run` reads it directly. If the automatic pre-save validation finds errors, you are asked whether to save anyway.

---

## Keyboard Shortcuts Reference

| Shortcut | Action |
|----------|--------|
| `Escape` | Cancel the current drawing mode |
| `Ctrl+Z` / `Ctrl+Y` | Undo / Redo |
| `Ctrl+N` | New Scenario |
| `Ctrl+O` | Import Configuration… |
| `Ctrl+S` | Save |
| `Ctrl+Shift+S` | Export As… |
| `Ctrl+R` | Load Results… |
| `Ctrl+,` | Preferences… |
| `F1` | Documentation |

Delete, Duplicate, Copy Attributes, and Paste Attributes have no shortcuts; use the right-click menu on the map or in the tree.

---

## Tips and Best Practices

- **Drag** a bus or piece of equipment to move it. Lines attached to it follow.
- **Double-click** an element in the Element Tree to center the map on it.
- **Ctrl+Click** in the Element Tree to select several elements of one type for batch editing (e.g., set the same fuel cost for all diesel generators).
- Use the **Appearance** group in property forms to customize marker colors, sizes, and line widths.
- Change the theme under **File > Preferences… > General > Appearance > Theme** (Light, GitHub Light, VS Code Dark+, Dracula, One Dark Pro).
- Import GIS data (GeoJSON, Shapefile, KML, KMZ, GeoPackage) with **File > Import Geo Asset…**, then right-click the asset under **Geo Assets** and select **Parse to System Elements...**. The data must be in WGS84. For building a grid from OpenStreetMap data, use **Workflows > Grid Builder…**.
- Use the **Python REPL** for batch operations, for example: `for gid, g in model.state.generators.items(): model.update_generator(gid, fuel_cost=85.0)` (see [Scripting & Automation](../gui/scripting.md)).
- Use `esfex run -c config.yaml --dry-run` to validate and preview the simulation plan without solving.
