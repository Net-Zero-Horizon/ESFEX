# Single-Line Diagram Analysis

The **Single-Line Diagram** tab draws the electrical schematic of the current system. On top of it, three levels of overlay can be shown: operational results from a simulation, post-contingency states and a frequency-response gauge. An analysis mode computes the same overlays from a dispatch you type in, without simulation results.


---


## Prerequisites

1. Build the system in the **Geographic View** with buses, generators and (optionally) lines and transformers.
2. Switch to the **Single-Line Diagram** tab. The drawing tools are disabled there (the view is read-only) and the operational toolbar appears above the diagram.
3. Either click **Load Results** and pick an HDF5 results file from a completed simulation, or click **Analysis** (see [Real-Time Analysis Mode](#real-time-analysis-mode)).

### Operational Toolbar

| Control | Function |
|---------|----------|
| **Analysis** | Toggle real-time analysis mode |
| **Load Results** | Open an HDF5 results file (`*.h5`, `*.hdf5`) |
| **Year** | Simulation year to show |
| **Hour** slider and spin box | Hour of the year |
| **▶** / **⏸** | Play / pause the hourly animation |
| **1x / 2x / 5x / 10x** | Playback speed |
| **Contingency** | Select a contingency to overlay (Level 2) |
| **Export SVG** | Save the diagram, with overlays, as an SVG file |
| **Clear** | Remove all overlays, unload the results and leave analysis mode |

Year, hour, playback, Export SVG and Clear are enabled once results are loaded.

### How the Diagram Is Laid Out

When the buses have map coordinates, the diagram follows the geography of the system:

- **Substations.** Buses on the same site are drawn together inside one dashed frame. A site is a set of buses joined by a transformer whose two ends are less than 5 km apart, or lying within 300 m of each other. Inside the frame, higher-voltage bars sit above lower-voltage ones and each transformer is drawn vertically between its two bars, with the equipment hanging below each bar.
- **Junctions.** A bus with no equipment and no transformer (a tee or tap point on a line) is drawn as a short bar without a frame. Its name appears in the tooltip.
- **Placement.** Each frame is placed as close as possible to its map position, on a grid that keeps a clear channel between frames. Long empty stretches, such as sea, are shortened.
- **Lines** between sites leave their bar vertically and follow the channels between frames, so they never cross another substation. A line leaves from the top or bottom of its bar, whichever faces the other end, as long as it does not have to cut through another bar of the same substation. If a bar is covered above and below by other bars, it is drawn longer so its lines have a clear way out.
- **Parallel lines.** Lines sharing a channel run side by side, 10 px apart. A channel with many lines is widened instead of packing them tighter. Their order in the channel follows where each line turns next, as on a metro map: two lines cross at most once. Parallel circuits between the same two bars follow the same path, as one ribbon.
- **Long transformers.** A transformer whose two ends are more than 5 km apart links two frames. Its symbol is drawn on the connecting line, at a spot not taken by another symbol.

The layout is the same every time the same system is opened. If no bus has coordinates, the diagram falls back to one column per node, with voltage levels in rows.

### Zoom and Detail

What the diagram shows depends on the zoom, so it stays readable from the whole system down to a single bay:

| Zoom | Shown |
|------|-------|
| Whole system (far) | Substation frames (lightly shaded), busbars and lines, at a constant on-screen width |
| Region (middle) | Adds generators, loads and the other equipment symbols |
| Close (scale 0.55 and above) | Everything: voltages, bus and equipment names, line labels, results overlays |

While the diagram's own text is too small to read, the substation names are drawn at a fixed size instead. Higher-voltage and larger substations come first, and a name that would overlap another is left out until you zoom in. Names that are only an imported identifier, such as `Substation way/424481948`, give way to real names.

Parts of the diagram far outside the view are not drawn, so panning stays smooth on systems with thousands of buses. The minimap in the bottom-left corner shows where the view is; click or drag on it to move.

**Export SVG** saves the whole diagram at full detail, whatever the current zoom.

### Open a Substation

Double-click a substation frame to open that substation on its own. Its busbars, equipment and internal transformers are drawn at full detail, however small the substation is on screen. Double-clicking anywhere else zooms in.

Each line to another substation is cut to a short straight stub that leaves its busbar and ends in the far substation's name, for example `→ Bayamo 220 kV`. Click the name to open that substation. A line that reaches a tap point outside any substation names the nearest substations behind the tap, up to two (`→ A / B +1`), and opens the first one. Hover a stub to see the line's own label.

The breadcrumb at the top left shows where you are (**System › Bayamo 220 kV**). Click **System**, or press Esc or Backspace, to go back to the whole system at the zoom you left it at. Selecting an element in the Element Tree that is not in the open substation also goes back.

Selection, the results overlays and the contingency overlay work the same in a substation as in the whole system. **Export SVG** in a substation saves only that substation.


---


## Level 1: Operational Overlay

Values read from the HDF5 results for the selected year and hour, shown with generation, demand and price data.

### Generator Status Indicators

| Status | Visual | Meaning |
|--------|--------|---------|
| **Online** | Normal color + utilization bar | Generator is committed and producing |
| **Offline** | Gray circle overlay + "OFF" label | Generator is not committed for this hour |
| **Starting up** | Green upward arrow badge | Generator starts up this hour |

On/off and start-up data come from the `gen_status` and `gen_startup` datasets, which unit-commitment runs write. Without them, every generator is shown online.

### Reserve Badges

Compact badge near each node's bus:

```
R: 15/8 MW
```

Format: `R: static/dynamic MW`. Badge color indicates adequacy:

| Color | Meaning |
|-------|---------|
| **Green** | No reserve shortfall |
| **Red** | Static or dynamic reserve shortfall above 0.1 MW |

### Voltage Angle Labels

Per-bus voltage angle label:

```
θ=2.3°
```

Color coding:

| Color | Range | Interpretation |
|-------|-------|----------------|
| **Green** | < 10° | Normal operating range |
| **Yellow** | 10°–20° | Elevated, monitor stability |
| **Red** | > 20° | High angle, potential stability concern |

### System Info Bar

Fixed bar at the bottom of the diagram:

```
Year 2030 | Hour 100 | RE: 42.0% | Demand: 300 MW | Gen: 295 MW | CO₂: 15.2 t | ROCOF: 0.83 Hz/s | Nadir: 49.3 Hz | H: 1250 MW·s
```

CO₂ is the system total for the hour. The frequency items appear when a frequency estimate is available (Level 3). In AC analysis mode the bar also shows losses and the number of voltage violations.


---


## Level 2: N-1 Contingency Analysis

Simulate the loss of one element and see the post-contingency state on the diagram. The contingency calculation uses a DC power flow.

### Using the Contingency Combo

The **Contingency** dropdown lists the contingencies for the current hour, sorted by impact (MW lost or MW of flow on the element, highest first):

- **Generators** with output above 0.1 MW: "Loss: gen_id (X MW)"
- **Lines**: "Loss: line_id (X MW cap)"
- **Transformers**: "Loss: transformer name (X MVA)"
- **Batteries** that are discharging

Select a contingency to overlay the post-contingency state. Select "None" to remove the overlay.

### Contingency Overlay

| Element | Visual |
|---------|--------|
| **Tripped element** | Red X cross over the tripped generator or line |
| **Overloaded lines** | Thick red dashed stroke with "OVERLOAD +X%" label |
| **Load shedding** | Red "Shed: X MW" badge on affected nodes |
| **Security status** | Badge in upper right: green "N-1 SECURE" or red "N-1 INSECURE" |

When a frequency estimate is available, the status badge also shows the post-contingency ROCOF and nadir.

### Generator Loss Analysis

1. The lost output is redistributed among the remaining online generators in proportion to their headroom (rated power minus current output).
2. Renewable generators do not take part in the redistribution.
3. If total headroom is insufficient, load is shed in proportion to the demand at each node.
4. The DC power flow is solved with the new injections.
5. Lines above their thermal capacity are flagged as overloaded.

### Line Loss Analysis

1. The network susceptance matrix is rebuilt without the tripped line.
2. New voltage angles and line flows are computed with the DC power flow.
3. Lines above their thermal capacity are flagged as overloaded.
4. Generation dispatch is unchanged (only flows redistribute).


---


## Level 3: Frequency Response Gauge

Compact frequency stability gauge in the upper-right corner, computed with a center-of-inertia model for the loss of the largest online generator.

### Frequency Gauge

1. **Frequency bar** from nominal − 2 Hz to nominal + 2 Hz, with a needle at the predicted nadir:
    - Red zones: more than 1 Hz from nominal
    - Yellow zones: 0.5 to 1 Hz from nominal
    - Green zone: within 0.5 Hz of nominal
2. **Metrics**: ROCOF (Hz/s), green if within the limit and red if not; system inertia H (MW·s) and power imbalance ΔP (MW).
3. **Border color**: green if the nadir stays above the limit, red if not.

### Interpretation

Values for a 50 Hz system:

| Metric | Healthy | Warning | Critical |
|--------|---------|---------|----------|
| ROCOF | < 1.0 Hz/s | 1.0–2.0 Hz/s | > 2.0 Hz/s |
| Nadir | > 49.5 Hz | 49.0–49.5 Hz | < 49.0 Hz |
| Steady-state | > 49.5 Hz | 49.0–49.5 Hz | < 49.0 Hz |

High renewable penetration reduces system inertia (lower H), leading to higher ROCOF and lower nadir values.


---


## Configuration

### Generator Parameters

Per-generator inertia, droop and governor parameters in YAML. They are also in the Electrical group of the generator form (**Inertia H (s)**, **Droop R (pu)**, **Governor T (s)**):

```yaml
generators:
  unit_0:
    name: Diesel
    type: Non-renewable
    inertia: [5.0]              # Inertia constant H (seconds)
    droop: [0.05]               # Governor droop R (pu), 5% is typical
    governor_time_const: [5.0]  # Governor time constant T_g (seconds)
    # ... other parameters
```

With results loaded, the gauge reads these values from the HDF5 file. In analysis mode it reads them from the editor.

### System Parameters

The gauge reads the system-level frequency keys of the system being edited:

```yaml
systems:
  my_system:
    load_damping: 0.01            # Load damping D (pu), 1% typical
    frequency_nominal: 50.0       # Nominal frequency (Hz)
    rocof_limit: 2.0              # Max ROCOF threshold (Hz/s)
    frequency_nadir_limit: 49.0   # Min frequency threshold (Hz)
```

Both with results loaded and in analysis mode, `load_damping` and `rocof_limit` are used as set. A `frequency_nominal` left at the default 50 Hz is replaced by the frequency of the first bus that has one (so a 60 Hz grid works without the key), and a `frequency_nadir_limit` left at the default 49 Hz becomes 1 Hz below the nominal frequency. Systems created in the Studio, which does not edit these keys, get the defaults. See [Frequency Stability](../formulation/frequency-stability.md) for the mathematical formulation.


---


## Playback

All overlays update during playback. When the last hour is reached, playback restarts at hour 0.

| Speed | Interval | Use case |
|-------|----------|----------|
| 1x | 1 s/hour | Detailed inspection |
| 2x | 500 ms/hour | Normal review |
| 5x | 200 ms/hour | Quick scan |
| 10x | 100 ms/hour | Overview |

The contingency selected in the combo is evaluated for the hour shown when it is selected; it is not recomputed at each playback step.


---


## Real-Time Analysis Mode

Runs frequency, power-flow and contingency analysis on the editor state, without simulation results.

### Activating Analysis Mode

1. Switch to the **Single-Line Diagram** tab.
2. Click the **Analysis** toggle button in the toolbar.
3. The **Dispatch Scenario** panel appears to the right of the diagram, and the properties panel is collapsed to make room.

### Dispatch Scenario Panel

**Analysis Mode** selector at the top:

| Mode | Available when | What it does |
|------|----------------|--------------|
| **DC Power Flow** | Always | DC power flow, contingencies and frequency gauge |
| **AC Power Flow** | Julia (`juliacall`) is installed, or else pandapower | Adds an AC Newton-Raphson power flow of the scenario |
| **Short Circuit** | pandapower is installed | AC power flow plus an IEC 60909 short-circuit calculation |

**Generator Dispatch** table:

| Generator | Rated (MW) | Output (MW) | On |
|-----------|-----------|-------------|-----|
| Diesel_0  | 100       | [80.0]      | [✓] |
| Solar_1   | 200       | [100.0]     | [✓] |

- Output: spin box from 0 to rated power. Initial value: 80% of rated power, or 50% for renewables.
- On: on/off checkbox per generator.
- Quick-set buttons: **All On**, **100%**, **80%**.

**Node Demand** table:

| Node   | Demand (MW) |
|--------|-------------|
| Node 0 | [120.0]     |
| Node 1 | [180.0]     |

- Initial demand of each node equals the generation at that node.
- **Balance Demand** sets each node's demand equal to its generation.

A summary line below shows total generation, total demand and the balance.

### Real-Time Updates

Changes trigger, after a 300 ms debounce:

1. A snapshot is built from the scenario.
2. In AC or Short Circuit mode, the AC power flow runs (see below).
3. The frequency analyzer computes ROCOF, nadir and steady-state frequency, using the largest online generator's output as the lost power.
4. The overlays (frequency gauge, info bar, generation and demand indicators) update.
5. The contingency list is rebuilt, and the selected contingency, if any, is recomputed.

### AC Power Flow Mode

The AC power flow uses the native Newton-Raphson solver in ESFEX's Julia package when `juliacall` can be imported; pandapower is used only when it cannot. The first run in a session starts Julia, which can take a while.

How the network is built:

- The slack bus is the bus whose **Bus Type** is `slack`; if there is none, the first bus.
- Loads have a fixed power factor of 0.95.
- Lines without resistance or reactance get 0.001 pu and 0.01 pu.
- Base power is 100 MVA and voltages outside 0.90–1.10 pu are counted as violations. The AC parameters in Global Settings (Power Flow group) are not used here.

Results shown:

- A status line under the mode selector: "AC PF: Converged (N iter) | Losses: X MW", plus "V violations: N" when there are any, or "AC PF: DIVERGED" in red.
- Voltage magnitude badges per bus (`V=0.987 pu`): green within 0.95–1.05 pu, yellow within 0.90–1.10 pu, red outside.
- Reactive flow (`Q: … Mvar`) and loss (`Loss: … MW`) labels on lines; the line flows and loadings are replaced by the AC values.
- Losses and voltage-violation count in the info bar.

If the power flow diverges or fails, the status line shows DIVERGED and the diagram keeps the DC values.

Contingency overlays (Level 2) still use the DC contingency model in AC mode.

In **Short Circuit** mode, pandapower also runs an IEC 60909 calculation and each bus gets a short-circuit power badge (`Sk: … MVA`).

### N-k Analysis Options

Group at the bottom of the panel:

| Control | Description |
|---------|-------------|
| Contingency Depth | `N-1` or `N-1-1` |
| Redistribution | `Pro-rata` or `Droop-based` |
| PI Screening Threshold | Performance-index threshold; 0 analyzes all contingencies |
| **Run All N-1** | Analyze every contingency at the selected depth and show a summary |
| **Run Screening** | Rank contingencies by performance index, PI = Σ (flow / capacity)² over lines, and show how many are critical (at most 50 are ranked) |

These buttons do not work in the current version. **Run All N-1** shows an error message in the summary line at both depths. **Run Screening** gives a result only with the threshold at 0 in DC mode; in AC mode it shows "Screening not available". The Redistribution choice is not applied. Use the **Contingency** combo to analyze contingencies one at a time.

### Switching Between Modes

- Analysis mode and loaded results are independent. Load Results, year, hour and playback controls are disabled while analysis mode is on.
- Unchecking **Analysis** clears the overlays and restores the properties panel. The year, hour and playback controls stay disabled, so click **Load Results** again to go back to a results file.
- **Clear** exits analysis mode and also unloads the results.
