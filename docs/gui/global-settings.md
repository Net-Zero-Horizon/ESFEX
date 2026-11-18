# Global & System Settings

Two levels: **Global Settings** apply to the entire simulation; **System Settings** are per-system.


---


## Global Settings

Click **Global Settings** in the Element Tree. These parameters apply to all systems. The form has these groups, top to bottom: Systems to Simulate, Simulation, Temporal, Power Flow, Master Problem, N-1 Security, Risk & Resilience, Solver Options and Visual Scaling.

**Defaults for new projects.** A new project takes its simulation and solver defaults from **File > Preferences… > Solver & Simulation**. With unchanged preferences these are: mode `development`, resolution 6 h, rolling horizon 48 h, overlap 24 h, Primary Energy off, solver HiGHS, 4 threads, time limit 3600 s, gap 0.001, verbose off. When a YAML file is opened, keys missing from the file take the schema defaults listed in the tables below.

### Systems to Simulate

One checkbox per system. Only the checked systems are validated and run. If none are stored in the project, all systems are simulated.

### Simulation

| Field | Default | Description |
|-------|---------|-------------|
| Simulation Mode | `development` | `Development`: master problem (capacity expansion) first, then rolling-horizon dispatch of every planning year. `Unit Commitment`: no master problem; a MIP with binary on/off decisions, start-up costs and minimum up/down times for the existing fleet, run over the first UC Hours of the first year only. See [Concepts](../getting-started/concepts.md). |
| UC Hours (h) | 24 | Shown only in `Unit Commitment` mode. Number of hours dispatched (at most 8,760). It is not a window size. |
| Enable UC | Off | Shown only in `Development` mode. Runs each dispatch window with binary commitment instead of an LP (`master_problem.use_uc_in_dispatch`). |
| Start Date | 01/01/2025 00:00 | Simulation start date (`DD/MM/YYYY HH:MM`). The first planning year starts on this date. |
| Primary Energy | On (YAML), Off (new project) | Include the fuel supply chain (fuel sources, storage, routes, electrolyzers) in the optimization, in the dispatch and in the master problem. The master then builds fuel storage, route capacity and electrolyzers, and charges fuel-coupled units the fuel price instead of their fuel cost. When off, fuel is available at each generator at its fuel cost and nothing of the fuel chain is built. |
| Console Verbose | Basic | `High` adds `--verbose` to the run command, so the **Run Output** tab shows debug-level log output. |

**Tips:**
- Start with `Development` mode. Unit commitment MIPs take much longer to solve.
- Start Date sets the base year for cost projections, fuel price growth and degradation.

### Temporal

| Field | Default | Description |
|-------|---------|-------------|
| Resolution (h) | 1 (YAML), 6 (new project) | Operational time step, an integer from 1 to 24 hours. Coarser steps make the problem smaller; ramp limits and minimum up/down times are scaled to the step. |
| Rolling Horizon (h) | 48 | Size of each dispatch window in hours. The operational year (8760 hours) is divided into overlapping windows of this length. Each window is solved independently as an optimization problem. |
| Overlap (h) | 24 | Look-ahead at the end of each window: solved, then discarded and re-solved by the next window. Only the first `rolling_horizon_hours - overlap_hours` of each window are kept, and the state at the end of that part (battery SOC, generator status and output, reservoir levels) starts the next window. Must be shorter than the window; the form caps it at window − 1. |
| Investment Resolution (h) | 8760 | Hours per investment period of the master problem. Use 8760 for annual periods. |
| PE Resolution (h) | 24 | Time step of the fuel supply chain model. Only used when Primary Energy is on. |
| Use Rolling Horizon | On | When off, each year is dispatched in a single window. |
| Model Clock | auto | Time base of every hourly series (`temporal.model_clock`): hour 0 = 1 Jan 00:00 on this clock, a fixed offset with no daylight saving time. `auto` is replaced by the zone the Grid Builder, the Solar PV / Wind workflows or the availability generator chose when they write series. Type an IANA zone (`America/Havana`, used at its standard offset), `UTC` or `Etc/GMT+5`; an unknown zone is rejected and the previous value kept. See [Model clock](../user-guide/configuration.md#model-clock). |

**Rolling horizon:** The operational year is solved as overlapping windows. Example with 48-hour windows and 24-hour overlap:
- Window 1: hours 1-48 (keep hours 1-24).
- Window 2: hours 25-72 (initial conditions from the end of hour 24, keep hours 25-48).
- Window 3: hours 49-96, and so on.

Stored energy has no value at the end of a window, so the overlap is what lets a window charge storage for a peak in the next day. With less than 24 h, storage can be empty when a peak falls just after a window boundary; ESFEX logs a warning in that case.

### Power Flow

The power-flow formulation applies to the whole model: multi-system projects are merged into one network and solved with one formulation.

| Field | Default | Description |
|-------|---------|-------------|
| Power Flow Mode | DC OPF | `DC OPF`: linearized DC power flow. `DC OPF + AC Verify`: DC OPF, then a post-hoc Newton-Raphson AC check of the dispatch (only in `Unit Commitment` mode). `AC OPF — SOC`, `AC OPF — QC`, `AC OPF — SDP`: convex relaxations of the AC OPF. `AC OPF — Polar`, `AC OPF — Rect`: exact nonlinear AC OPF. See [AC Power Flow](../formulation/ac-power-flow.md). |

The mode restricts the solver list: solvers that cannot handle the formulation are greyed out, and the selection moves to a compatible one.

| Mode | Compatible solvers |
|------|--------------------|
| DC OPF, DC OPF + AC Verify | HiGHS, Gurobi, GLPK, CBC, SCIP, CPLEX, Xpress |
| AC OPF — SOC, AC OPF — QC | Gurobi, CPLEX, Xpress, Clarabel, SCS |
| AC OPF — SDP | SCS, Clarabel |
| AC OPF — Polar, AC OPF — Rect | Ipopt |

The AC parameters are shown for every mode except `DC OPF`:

| Field | Default | Description |
|-------|---------|-------------|
| Base MVA | 100 | System base power for per-unit conversion |
| Voltage Min (p.u.) / Voltage Max (p.u.) | 0.90 / 1.10 | Bus voltage limits |
| Gen Default PF | 0.85 | Power factor used to estimate generator reactive limits when none are given |
| Load Power Factor | 0.9 | Reactive demand: Q = P × tan(acos(pf)) |
| Q Slack Penalty ($/MVAr) | 100 | Penalty on reactive power imbalance |
| Min Reactance (p.u.) | 0.01 | Floor applied to the reactance of short lines and bus ties |
| Tap Ratio Min / Tap Ratio Max | 0.5 / 2.0 | Transformer taps outside this range are reset to 1.0 |
| Q Min Ratio (×Q_max) | 0.5 | Q_min = −ratio × Q_max when reactive limits are not given |

There is no slack-bus field and no angle limit. The reference bus is the bus whose **Bus Type** is `slack` in the bus form. The DC angle-difference limit is not part of the formulation.

### Master Problem

Strategic multi-year capacity expansion planning. See [Master Problem](../user-guide/master-problem.md) for how these settings are used.

| Field | Default | Description |
|-------|---------|-------------|
| Enable Stochastic | Off | Two-stage stochastic master over the scenarios defined under **Scenarios** in the tree (see [Scenarios](#scenarios)). |
| Representative Days | 5 | Days per year used to estimate operating cost in the master (1 to 365): the peak-demand day plus days whose mean demand and renewable availability match the year's. Ignored when TSAM is on. |
| Min Day Separation | 5 | Minimum distance, in **days**, between selected representative days, and between critical days (0 to 365 in the form; the schema requires at least 1). Stays on with TSAM for the critical days. |
| Critical Days | 3 | Days with the lowest renewable supply against demand that the master models besides the representative days or TSAM periods, one day of the year each (0 to 365; 0 = none). See [Critical Days](../user-guide/master-problem.md#critical-days). |
| Storage Firm Hours | Auto | Hours a battery must sustain to count as firm power in the capacity adequacy: it counts min(power, usable energy / hours). Auto (0) = the longest stretch without solar on the critical days. |
| Solver Method | Monolithic (full model) | `Monolithic (full model)` or `Benders Decomposition` |
| Max Iterations | 50 | Benders only |
| Convergence Tolerance | 0.0001 | Benders only: relative gap for convergence |
| Enable TSAM Clustering | Off | Replace peak-day selection with time-series clustering |
| TSAM Periods | 10 | Number of clustered representative days per year (2 to 365) |
| Clustering Method | `kmedoids` | `kmedoids` or `kmeans` |
| Inter-Period Storage Linking (Battery + Hydro) | On | Chain storage state of charge across clustered days |

### MGA / SPORES (Near-Optimal Alternative Exploration)

Part of the Master Problem group. Generates a set of investment portfolios near the cost-optimal solution. Two methods share the cost-slack envelope but differ in how alternatives are produced (see [Concepts: MGA and SPORES](../getting-started/concepts.md#near-optimal-alternatives-mga-and-spores)):

| Field | Default | Description |
|-------|---------|-------------|
| Enable MGA | Off | Master toggle for the near-optimal sub-section |
| Method | `MGA (HSJ loop)` | `MGA (HSJ loop)` runs the classical loop $K$ times; `SPORES (per-objective)` solves one alternative per declared objective |
| Alternatives | 10 | Number of alternatives $K$ for the HSJ loop. **Hidden when Method = SPORES** (the count equals the number of checked objectives) |
| SPORES Objectives | (none checked) | Multi-select checklist: `HSJ diversity`, `Min total build`, `Tech equity`, `Regional equity`, `Evolutionary distance`. **Hidden when Method = MGA**. Required when Method = SPORES — saving with the method set but no objectives selected will trigger a validation error at run time |
| Cost Slack (fraction) | 0.05 | Maximum cost increase allowed (0.05 = alternatives can cost up to 105% of optimal), from 0 to 0.5. Shared by both methods |
| Invest. Threshold (MW) | 0.1 | Minimum investment to count as "selected" by HSJ frequency scoring. Used by `method = mga` and by the SPORES `hsj_diversity` objective; the other SPORES objectives ignore it |

The visible field set updates automatically: choosing `SPORES (per-objective)` hides Alternatives and reveals SPORES Objectives, and vice versa.

### N-1 Security

Adds security constraints for the loss of a single element to the dispatch. See [N-1 Security-Constrained DC Power Flow](../formulation/dc-power-flow.md#n-1-security-constrained-dc-power-flow).

| Field | Default | Description |
|-------|---------|-------------|
| Enable N-1 Security | Off | Master switch |
| Apply in | Development, Unit commitment | Simulation modes that get the N-1 constraints (`apply_to_modes`). Other modes listed in the YAML (e.g. `economic_dispatch`) are kept. |
| Transmission | On | Line outage constraints |
| Trans. Reserve Factor (frac) | 0.70 | Usable fraction of line capacity under N-1 |
| Generation | On | Generator outage constraints |
| Reserve Type | Largest Unit | `Largest Unit`: the headroom of the other units and batteries must cover the output of the largest unit. `Percentage`: the headroom of all units and batteries must cover Reserve Percentage × the system demand of each step. In a multi-system run each system meets it on its own, with its own largest unit and demand. |
| Reserve Percentage (frac) | 0.15 | Used when Reserve Type is `Percentage` |
| Use SCOPF | Off | Replace preventive N-1 reserves with post-contingency (LODF) line limits for every pair of lines, added in one solve (not iterative) |
| Allow Corrective Actions | Off | Allow post-contingency battery and generator re-dispatch instead of preventive reserve |
| Compute N-1 Security Cost | Off | Re-solve each operational window without the N-1 constraints and report the objective difference as the N-1 security cost (`compute_security_cost`). Roughly doubles the solve time; off, the cost is reported as 0 |
| *SLD contingency analysis* | | The fields below do not change the optimisation: they set up **Run All N-1** and **Run Screening** in the SLD Analysis panel |
| Critical Line Threshold (frac) | 0.0 | Only lines and transformers loaded at or above this fraction of their capacity are tripped; 0 trips them all |
| Contingency Depth | N-1 | `N-1` or `N-1-1`; initial value of the panel's depth selector |
| Redistribution Mode | Pro-rata | `Pro-rata (proportional to capacity)` or `Droop-based (governor response)`; initial value of the panel's selector |
| PI Screening Threshold | 0.0 | Initial value of the panel's threshold; 0 keeps every contingency |
| Include Transformer Contingencies | Off | Trip transformers too |
| Include Battery Contingencies | Off | Trip discharging batteries too |

The depth, redistribution and threshold values preset the SLD Analysis panel each time **Analysis** is switched on; you can change them there for a single run (see [Single-Line Diagram](sld-dynamic-analysis.md#n-k-analysis-options)). The transformer and battery options and the critical line threshold are read when the runs start. The optimisation itself only uses the fields above the *SLD contingency analysis* row.

### Risk & Resilience

Settings stored in the `risk:` block of the YAML. The fields are greyed out until **Enable Risk & Resilience Analysis** is checked. The **Risk and Resilience** workbench (toolbar) fills the measure, CVaR and combination fields when you click **Apply** there, and stores its climate and hazard scenarios, which are saved as `risk.climate_scenarios` and `risk.hazard_scenarios` (climate probabilities are rescaled to sum to 1 on save, as the schema requires; a scenario the schema rejects is not saved and a warning is logged). When the workbench opens, it starts from these saved values (measure, CVaR, combination method and scenarios), so a second **Apply** keeps what was not changed. The optimization run itself does not read the `risk:` block. See [Risk and Resilience](../formulation/risk-resilience.md).

| Field | Default | Description |
|-------|---------|-------------|
| Enable Risk & Resilience Analysis | Off | Enables the group |
| Risk Measure | Expected Value | `Expected Value`, `CVaR (Conditional Value-at-Risk)` or `Minimax Regret` |
| CVaR Confidence (α) | 0.95 | CVaR confidence level |
| Risk Aversion (λ) | 0.5 | Weight of the CVaR term |
| Hazard Combination | Independent | `Independent`, `Copula` or `MCDA` |
| Value of Lost Load: Residential / Commercial / Industrial / Critical | 5,000 / 25,000 / 15,000 / 100,000 $/MWh | Sector VOLL used by the risk analysis (not the dispatch VOLL, which is the system Loss of Load penalty) |
| Base Temperature | 24.0 °C | Reference temperature for degree days (24 °C suits tropical islands; use about 18 °C for temperate regions) |
| Heating Coeff. (%/°C) / Cooling Coeff. (%/°C) | 0.5 / 2.5 | Demand change per °C below / above the base temperature |
| Insurance Premium Rate | 0 | Fraction of asset value |
| Monte Carlo Samples | 1000 | 100 to 100,000 |
| Monte Carlo Seed | 42 | Random seed |

### Solver Options

| Field | Default | Description |
|-------|---------|-------------|
| Solver | `highs` | Only solvers whose Julia package is installed in ESFEX's Julia environment are listed. Bundled: HiGHS, GLPK, Ipopt, SCS, Clarabel. Gurobi, CPLEX, CBC, SCIP and Xpress appear after `esfex add-solver` (commercial solvers also need their own license). Solvers incompatible with the Power Flow Mode are greyed out. See [Solver Guide](../user-guide/solver-guide.md). |
| Threads | 4 | Number of parallel solver threads (1 to 128) |
| Time Limit (s) | 10800 (YAML), 3600 (new project) | Maximum solve time per optimization problem; 0 (shown as *No limit*) = no limit |
| Gap (frac) | 0.01 (YAML), 0.001 (new project) | Relative MIP optimality gap. Only relevant for MIP problems (unit commitment). |
| Verbose | Off | Print the full solver log in the Run Output tab |

Below these fields, the group shows the options specific to the selected solver (for example presolve or LP method). They are stored in `solver.options`.

### Visual Scaling

Controls marker and line sizes on the map. Each element class is scaled automatically from its smallest to its largest value onto the pixel range below, so no per-system calibration is needed.

| Field | Default | Description |
|-------|---------|-------------|
| Marker Min Size (px) / Marker Max Size (px) | 6 / 40 | Size of the smallest and largest marker |
| Marker Scaling | Area ∝ value (sqrt) | `Area ∝ value (sqrt)`, `Radius ∝ value (linear)` or `Log (heavy-tailed)` |
| Line Min Width (px) / Line Max Width (px) | 1.5 / 8 | Width of the thinnest and thickest line (linear scaling) |


---


## System Settings

Click a **system name** in the Element Tree. Each system has independent parameters, in three groups: General, Cost Limits and Penalties. The name field at the top renames the system.

### General

| Field | Default | Description |
|-------|---------|-------------|
| Demand Scale (x) | 1.0 | Multiplier applied to all demand in this system |
| Discount Rate (frac) | 0.05 | Discount rate for net present value |
| Base LCOE ($/MWh) | 93.0 | Revenue per MWh an existing generator produces, in its NPV for the NPV-based retirement (`base_lcoe`) |
| Battery NPV Return Rate (frac/yr) | 0.15 | An existing battery's yearly revenue in its NPV, as a fraction of its power investment cost (`npv_annual_return_rate`) |
| NPV-based Retirement | Off | After the master solve, retire for the whole horizon the existing generators and batteries whose NPV in the plan is below the threshold, and solve the master again (`npv_retirement`; development runs, every master mode). See [Capacity Expansion §9](../formulation/capacity-expansion.md#9-npv-based-iterative-retirement) |
| NPV Retirement Max Iterations | 5 | Most rounds of retirements, each followed by a master re-solve (`npv_retirement_max_iterations`) |
| NPV Retirement Threshold ($) | 0 | A unit whose NPV is below this is retired (`npv_retirement_threshold`) |
| Target RE Penetration (frac) | 1.0 | Final-year renewable energy share target. Shortfalls are penalized with the FRE Penetration Loss penalty. |
| Min Annual Increment (frac) | 0.01 | Minimum yearly increase in RE share |
| Max Annual Increment (frac) | 0.10 | Maximum yearly increase in RE share |
| Loss Demand Threshold (frac) | 0.05 | Acceptable unserved energy ratio |
| Min. System Inertia (MW·s) | 0.1 | Minimum system inertia per hour, the sum of output × H over online units (an absolute value; 0.1 is effectively no requirement) |
| Simulate Rooftop | Off | Include distributed rooftop PV |
| Enable CO2 Budget | On | Enforce an annual CO2 budget for this system; emissions above it pay the CO2 Budget Violation penalty |
| CO2 Annual Budget (tonnes/year) | 1,000,000 | Annual CO2 budget |

The master-problem meaning of the RE target, increments, discount rate and cost limits is described in [Master Problem](../user-guide/master-problem.md).

### Cost Limits

| Field | Default | Description |
|-------|---------|-------------|
| Max Annual System Cost ($) | 20e9 | Cap on annual system cost |
| Life Extension Cost Factor (x) | 0.20 | Life extension costs `invest_cost × factor` per MW per year |

### Penalties

Penalty costs on constraint violations. Higher penalties push the optimizer toward constraint satisfaction.

| Penalty | Default | Unit | Description |
|---------|---------|------|-------------|
| Loss of Load | 50,000 | $/MWh | Value of lost load (VOLL) for unserved demand. The schema default for a YAML file without this key is 10,000,000. |
| Loss Reserve (Static) | 100 | $/MW | Static reserve shortfall, per MW and hour |
| Loss Reserve (Dynamic) | 100 | $/MW | Dynamic (frequency response) reserve shortfall |
| Loss of Inertia | 200 | $/MW·s | Inertia shortfall below the Min. System Inertia |
| Transfer Margin | 100 | $/MW | Transfer margin violation |
| Max Curtailment Ratio | 0.05 | fraction | Hard annual cap on curtailment as a fraction of RE generation, per system, in the master problem (single days may curtail more; not enforced in operational dispatch, which charges Curtailment Cost) |
| Curtailment Cost ($/MWh) | 20 | $/MWh | Cost of each curtailed MWh of RE |
| Rooftop Curtailment | 5 | $/MWh | Curtailed rooftop PV |
| CO2 Cost | 10 | $/t | Carbon price on all emissions |
| CO2 Budget Violation | 500 | $/t | Emissions above the CO2 budget |
| FRE Penetration Loss | 100 | $/MWh | Shortfall from the RE target |
| EV Loss | 50,000 | $/MWh | EV driving energy not supplied. Same as Loss of Load by default; a lower value makes leaving EVs uncharged cheaper than generating. |
| Loss of Fuel Supply | 100 | $/unit | Fuel supply deficit |
| Non-Electric Demand Loss | 100 | $/unit | Non-electric (sectoral) fuel demand left unmet |

#### Criticality (Load Shedding Priority)

The four **Criticality … ($/MWh)** fields are **multipliers on the Loss of Load penalty**, not prices: shedding load of a sector costs VOLL × multiplier. Each demand sector is assigned a criticality level in the node's Demand Sectors table.

| Level | Default multiplier | Cost with default VOLL | Typical assignment |
|-------|--------------------|------------------------|--------------------|
| Critical | 3.0 | 150,000 $/MWh | Hospitals, emergency services, water treatment |
| High | 2.0 | 100,000 $/MWh | Industrial loads, large commercial consumers |
| Medium | 1.0 | 50,000 $/MWh | Residential loads, small commercial |
| Low | 0.5 | 25,000 $/MWh | Interruptible loads, non-essential services |

The optimizer sheds the cheapest (lowest-multiplier) load first. A YAML file without `criticality_penalties` uses the schema defaults 1000 / 100 / 10 / 1, which are also applied as VOLL multipliers.


---


## Scenarios

Scenarios for the stochastic master problem. Click **Scenarios** in the Element Tree.

### Scenario Table

Type a name and click **Add**; **Remove Selected** deletes the selected scenarios after a confirmation. Removing every scenario also persists on save (the primary system's `stochastic_scenarios` is written as an empty list). Each row has:

| Column | Description |
|--------|-------------|
| Name | Scenario name (e.g., "High Oil Price") |
| Probability | Weight from 0 to 1. The form warns when the probabilities do not sum to 1.0. |
| Description | Free text |

### Cost Multipliers

**Add Multiplier** opens the list of multipliers, grouped as Investment, Operational, Fuel, Penalty, Demand and Financial. Each added multiplier becomes a row with one value per scenario (default 1.0). **Remove Selected Multiplier** removes a row.

| Group | Multipliers (YAML key) |
|-------|------------------------|
| Investment | Renewable / conventional technology investment cost (`invest_cost_renewables`, `invest_cost_conventional`), battery investment cost for power and energy together (`invest_cost_storage`) or separately (`bat_invest_cost_power`, `bat_invest_cost_capacity`, applied on top of `invest_cost_storage`), transmission investment cost (`invest_cost_transmission`), generator and battery decommissioning cost (`gen_decommissioning_cost`, `bat_decommissioning_cost`), AC/DC and frequency converter investment cost (`converter_invest_cost`), fuel storage investment (`fuel_storage_invest_cost`), electrolyzer investment (`electrolyzer_invest_cost`) |
| Operational | Fixed O&M ($/MW·yr) of generators, technologies, batteries and battery technologies (`gen_fixed_cost`), maintenance cost (`maintenance_cost`), battery maintenance cost on top of it (`bat_maintenance_cost`), generator start-up cost (`gen_start_up_cost`; only when the dispatch uses unit commitment), converter variable cost (`converter_variable_cost`), inter-system link flow cost per MW·km (`transmission_cost_per_mw_km`), electrolyzer fixed, variable and water cost (`electrolyzer_fixed_cost`, `electrolyzer_variable_cost`, `electrolyzer_water_cost`) |
| Fuel | Fuel cost (`fuel_cost`), fuel price escalation as an annual factor (`fuel_price_growth`), fuel import cost (`fuel_import_cost`), fuel transport cost, which scales both the route shipping cost and route investment (`fuel_transport_cost`) |
| Penalty | Loss of load (`penalty_loss_of_load`), CO2 emission cost (`carbon_price`), loss of reserve (`penalty_loss_of_reserve`), curtailment (`penalty_curtailment`), RE penetration shortfall (`penalty_fre_penetration`), CO2 budget violation (`penalty_co2_budget_violation`), loss of fuel supply (`penalty_loss_of_fuel_supply`) |
| Demand | Demand growth as an annual factor (`demand_growth`) |
| Financial | Discount rate (`discount_rate`) |

Each one scales a term of the stochastic master; see [Stochastic Programming](../formulation/stochastic-programming.md#22-scenario-multipliers) for what it applies to. The fuel-chain and electrolyzer multipliers need **Primary Energy** on: the master has a fuel balance only then. A project saved by an earlier version may contain other keys: the ones that mean one of these multipliers (for example `gen_fuel_cost` or `fuel_price` for `fuel_cost`, `gen_invest_cost` for both technology investment costs) are translated when the project is opened, and the rest (electrolyzer, fuel import and fuel infrastructure costs, loss of fuel supply penalty) are dropped with a warning in the log, because the master problem has no cost they could scale.

### Using Stochastic Optimization

1. Define at least two scenarios with probabilities summing to 1.0.
2. Check **Global Settings > Master Problem > Enable Stochastic**.
3. Run the simulation.

Investment decisions are common to all scenarios (first stage); operation is evaluated per scenario (second stage), and the objective is the probability-weighted cost. See [Stochastic Programming](../formulation/stochastic-programming.md).


---


## How Settings Interact

| Level | Scope | Examples |
|-------|-------|---------|
| **Global** | All systems | Solver, temporal resolution, power flow mode, N-1 security, simulation mode |
| **System** | One system | Demand scale, RE targets, penalties, CO2 budget, discount rate |
| **Scenarios** | Stored with the first system | Cost, penalty, demand-growth and discount-rate multipliers |

- Global settings cannot be overridden per system. Different temporal resolutions require separate projects.
- System settings are independent. Connected systems can have different RE targets, penalties and CO2 budgets.


---


## Settings Persistence

- Global and system settings are part of the project YAML. **Save** (`Ctrl+S`) writes it to the current file, or asks for a path if there is none. **Export As…** (`Ctrl+Shift+S`) writes the same YAML to a new path. Nothing is stripped in either case.
- Before writing, the Studio validates all systems. If there are errors, it asks "Save anyway?".
- **File > Export Project…** writes a portable `.esfexp` bundle with the YAML and its data files.
- Keys missing from an opened YAML take the schema defaults; new projects take the Preferences defaults (see the top of this page).


---


## Quick Reference: Recommended Settings by Study Type

| Study Type | Simulation Mode | Resolution | Rolling Horizon | Solver | N-1 |
|------------|----------------|------------|-----------------|--------|-----|
| Screening study | `development` | 6 h | 48 h | HiGHS | Off |
| Detailed planning | `development` | 1 h | 48 h | Gurobi | On |
| Operational study | `unit_commitment` | 1 h | 24 h | Gurobi | On |
| Rapid feasibility | `development` | 6 h | 168 h | HiGHS | Off |
| Stochastic planning | `development` | 1 h | 48 h | Gurobi | Off |


---


## Troubleshooting Common Settings Issues

| Problem | Likely Cause | Solution |
|---------|-------------|----------|
| Solver reports infeasible | Penalties too low or conflicting constraints | Increase VOLL penalty, relax RE target |
| Very slow solve | UC mode with many generators, small windows | Use `development` mode, increase window size |
| Unrealistic curtailment | `max_curtailment_ratio` too high | Reduce to 0.05 (5%) |
| No battery investment | Curtailment cost too low | Increase Curtailment Cost, reduce `max_curtailment_ratio` |
| Memory issues | Too many years × nodes × generators | Reduce planning horizon, aggregate nodes |
| Numerical warnings | Poor problem scaling | Enable solver verbose mode, check penalty magnitudes |
| Solver missing from the list | Julia package not installed, or incompatible with the Power Flow Mode | Run `esfex add-solver`, or change the Power Flow Mode |


---


## Keyboard Shortcuts for Settings

| Shortcut | Action |
|----------|--------|
| `Ctrl+S` | Save the project YAML |
| `Ctrl+Shift+S` | Export As… (same YAML, new path) |
| `Ctrl+Z` / `Ctrl+Y` | Undo / Redo (model-wide, not per form) |
| `Ctrl+,` | Preferences… |
