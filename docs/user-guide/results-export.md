# Results Export

## HDF5 Output Structure

One HDF5 file is written per run, in the output directory, named `<config_stem>_<ddmmyyyy>_<HHMM>.h5` (for example `results/isla_juventud_15012026_1030.h5`; the system name replaces the stem when there is no config file). The layout is organised by *kind* of data — static configuration, summary scalars, full time series, multi-system aggregates, and the MGA / SPORES sweep:

```
<config_stem>_<ddmmyyyy>_<HHMM>.h5
├── [Root attributes]              # Metadata + multi-system layout (see below)
├── system_configuration/          # Static system definition (generators,
│                                  #   batteries, technologies, nodes)
├── summary_results/               # Per-year scalars (one row per year)
├── detailed_results/              # Full time series, one scenario per year
│   └── year_YYYY_threshold_0/     # ~30 datasets / groups per scenario
├── demand/                        # Input demand profiles
│   └── year_YYYY_base_demand
├── cost_breakdown/                # Granular cost decomposition per year
│   └── year_YYYY/                 # Cost components as group attrs
├── global/                        # Meta-network totals (always present;
│   ├── summary_results/           #   for single-system runs == root totals)
│   ├── cost_breakdown/
│   └── demand/
├── inter_system/                  # Multi-system only — flows between
│   └── detailed_results/          #   subsystems per scenario
│       └── year_YYYY_threshold_0/
│           └── power_flow/{A_to_B}
└── mga/                           # Only when MGA / SPORES enabled
    ├── @method = "mga" | "spores"
    ├── @objectives = [...]        # SPORES menu (empty for MGA runs)
    ├── @num_alternatives
    ├── @slack_fraction
    ├── @optimal_cost
    ├── generator_names, battery_names
    ├── alternative_0/             # Cost-optimal seed (@objective="cost_optimal")
    │   ├── tech_investment            # (years, techs, nodes)
    │   ├── bat_tech_power_investment  # (years, bat_techs, nodes)
    │   ├── bat_tech_capacity_investment # (years, bat_techs, nodes)
    │   ├── transfer_investment        # (n, 4) [year, i, j, MW], if any
    │   ├── cumulative_gen_capacity    # (years, generators, nodes)
    │   └── re_penetration             # (years,)
    └── alternative_K/             # K = num_alternatives  (MGA) or
                                   #     len(objectives)   (SPORES)
        └── @objective = "hsj_diversity" | "min_total_build" | …
                                   # multi-system runs also get mga/systems/{name}/
```

**Per-system mirrors have been removed.** Earlier versions wrote a `/systems/{name}/detailed_results/` / `/systems/{name}/summary_results/` mirror per subsystem; the viewer now slices the global block on read using the subsystem root attributes. See [HDF5 Output Schema § File Structure](../reference/hdf5-output-schema.md#file-structure-overview) for the full canonical reference.

### Root Attributes

Always present:

| Attribute | Type | Description |
|-----------|------|-------------|
| `creation_date` | string | ISO 8601 creation timestamp |
| `num_nodes` | int | Number of network nodes |
| `num_years` | int | Number of simulation years |
| `temporal_resolution_hours` | int | Time step (1 = hourly, 3 = 3-hourly, …) |
| `simulation_mode` | string | `"development"` or `"unit_commitment"` |
| `years_range` | string | e.g. `"2025-2049"` |
| `export_type` | string | `"incremental_results"` |
| `export_complete` | bool | Whether the export finished successfully |

These attributes describe how to slice the global block per system. They are written for single-system runs too:

| Attribute | Type | Description |
|-----------|------|-------------|
| `num_systems` | int | Number of subsystems |
| `subsystem_names` | string[] | e.g. `["IslaJuventud", "Cuba"]` |
| `subsystem_offsets` | int[] | Starting node index of each subsystem |
| `subsystem_node_counts` | int[] | Number of nodes in each subsystem |

The Studio results viewer (`_open_scenario` and `_system_node_range` in `esfex/visualization/panels/results_charts.py`) uses these to derive a per-system view from the global block — e.g. to extract Cuba's nodes from a `(10, T)` global array, it slices `[1:10, :]`.

### Inside `detailed_results/year_YYYY_threshold_0/`

Most arrays carry the global node axis as their first dimension; the viewer applies subsystem slicing on read. Names retain underscores and capitalisation as written by the runner. Entries are written only when the result exists (for example `gen_status` only in unit-commitment mode). Per-unit datasets are named after the unit, with `/` replaced by ` - `.

| Entry | Shape | Units | Description |
|-------|-------|-------|-------------|
| `demand` | (N, T) | MW | Gross demand (float32): base demand, plus EV demand when EV optimisation is off. Rooftop solar is not subtracted. Files written before the `dimensions` attribute was added hold it (T, N) when N ≥ T; `esfex.io.exporter.demand_node_hours()` reads both |
| `generation/{gen_name}` | (N, T) | MW | Per-generator output (group, one dataset per unit; attribute `fuel`) |
| `gen_status/{gen_name}` | (N, T) | 0/1 | Commitment status (UC mode only) |
| `gen_startup/{gen_name}` | (N, T) | 0-1 | Startup events (UC mode only) |
| `curtailment` | (N, T) | MW | Renewable curtailment per node, summed over generators (legacy files had a `(T,)` aggregate) |
| `battery_charge/{bat_name}` | (N, T) | MW | Per-battery charge (group) |
| `battery_discharge/{bat_name}` | (N, T) | MW | Per-battery discharge (group) |
| `battery_soc/{bat_name}` | (N, T) | MWh | Per-battery SOC (group) |
| `battery_spillage/{bat_name}` | (N, T) | MW | Per-battery spillage (group) |
| `battery_capacity_factor/{bat_name}` | (N, T) | unitless | Per-battery CF (group) |
| `battery_lcoe/{bat_name}` | (N, T) | $/MWh | Per-battery LCOE (group) |
| `battery_vallcoe/{bat_name}` | (N, T) | $/MWh | Per-battery value-adjusted LCOE (group) |
| `reserve_static`, `reserve_dynamic` | (N, T) | MW | Reserve held as free capacity (at most the requirement; not generated energy) |
| `loss_of_reserve_static`, `loss_of_reserve_dynamic` | (N, T) | MW | Unmet reserves |
| `loss_load` | (N, T) | MW | Unserved demand |
| `CO2_emissions` | (N, T) | tCO2/h | Per-node emission rate; × `temporal_resolution_hours` gives tonnes per step |
| `nodal_electricity_prices` | (N, T) | $/MWh | LMPs per node (max over a node's buses) |
| `nodal_electricity_prices_congestion` | (N, T) | $/MWh | Nodal price minus the system average |
| `electricity_prices` | (T,) | $/MWh | Mean of the nodal prices |
| `electricity_prices_energy` | (T,) | $/MWh | Energy component (same values as `electricity_prices`) |
| `voltage_angle` | (N, T) | radians | Voltage angles |
| `voltage_magnitude` | (buses, T) | p.u. | ACOPF modes only |
| `reactive_generation` | (G, buses, T) | MVAr | ACOPF modes only |
| `power_flow` | (N, N, T) | MW | Inter-node flows aggregated from bus-pair flows |
| `transfer_margin` | (N, N, T) | MW | Transfer margin slack, aggregated from bus pairs |
| `transfer_investment` | (N, N) | MW | Transmission investment |
| `loss_of_inertia` | (T,) | GW*s | System inertia deficit |
| `rooftop_generation` | (T, N) | MW | Behind-the-meter rooftop solar (note: `(T, N)` not `(N, T)`) |
| `EV_charging`, `EV_V2G`, `EV_soc`, `EV_loss` | (N, T) | MW / MWh | EV fleet variables |
| `reservoir_level/`, `reservoir_spillage/`, `reservoir_pump/` | (N, T) | MWh-eq / MW | Per-generator reservoir hydro (groups; level has T+1 steps) |
| `gen_investment_power/`, `bat_investment_power/`, `bat_investment_capacity/` | (N,) | MW / MWh | Per-unit investments (groups) |
| `capacity_factor/{gen}`, `lcoe/{gen}`, `vallcoe/{gen}` | (N, T) | various | Per-generator derived metrics |
| `technology_selling_prices/{gen_name}/prices_weights` | (K, 3) | mixed | Revenue analysis per generator (price, MW, timestep), named like its `generation/` dataset |
| `primary_energy/` | groups | various | Fuel supply chain results (if enabled) |
| `n1_security/` | group | various | N-1 duals and binding contingencies (if enabled) |

Scenario attributes include `year`, `feasible`, `total_cost`, `renewable_penetration`, `co2_emissions`, `solve_time`, `total_generation`, `total_demand`, `load_shed`, `master_re_target`, and the master problem's investment and retirement decisions as `investment_*` / `retirement_*` attributes (see [HDF5 Output Schema](../reference/hdf5-output-schema.md#detailed_resultsyear_yyyy_threshold_0)).

Where `N` = `num_nodes` and `T` = time steps in the year at `temporal_resolution_hours` cadence (e.g. 2920 for 3-hourly). `/demand/year_YYYY_base_demand` holds the same demand as `(T, N)`. The energy and CO2 attributes (`total_generation`, `total_demand`, `load_shed`, `co2_emissions`) are MWh and tonnes at any step length.

### `summary_results/` (per-year scalars)

| Dataset | Shape | Description |
|---------|-------|-------------|
| `year` | (num_years,) | Simulation year |
| `threshold` | (num_years,) | Threshold iteration (usually 0) |
| `feasible` | (num_years,) | 1 if feasible, 0 if not |
| `total_cost` | (num_years,) | Total annual cost ($) |
| `renewable_penetration` | (num_years,) | RE penetration (0-1) |
| `co2_emissions` | (num_years,) | Annual CO₂ (tonnes) |
| `loss_of_load` | (num_years,) | Total unserved energy (MWh) |
| `n1_security_cost` | (num_years,) | N-1 security cost ($) |

### Virtual Generator/Battery Naming

Virtual units from technology investments appear alongside original units. HDF5 naming order follows the adapter ordering:

1. Original generators (as defined in config)
2. Virtual generators (from technology investments)
3. Original batteries (as defined in config)
4. Virtual batteries (from battery technology investments)

Virtual units are named `Investment {Technology Name}` (e.g., `Investment Solar PV`, `Investment Li-Ion`).


---


## Derived Metrics

Computed after each year's dispatch by `Orchestrator._compute_derived_metrics()`. All are hourly `(N, T)` arrays per unit (g = generator, n = node, t = time step), not annual scalars:

| Metric | Formula | Units | Notes |
|--------|---------|-------|-------|
| **Capacity Factor** | output[g,n,t] / rated_power[g,n] | [0, 1] | Per hour |
| **LCOE** | ((fuel_cost + fixed_cost + maintenance_cost) × output + start_up_cost × startup) / output | $/MWh | Hours with output only (0 otherwise). No capital cost. When `fuel_cost` is 0, uses fuel price / energy content / efficiency from the fuel registry |
| **VALLCOE** | LCOE × (1 + energy_adj + capacity_adj + flexibility_adj) | $/MWh | energy_adj = 1 − capture rate (unit's output-weighted price / system average price); capacity_adj = 0.15 × (0.85 − output share in the top-10 % demand hours); flexibility_adj = −0.1 × ramp_up / rated power (non-renewables only) |
| **Battery CF** | discharge[b,n,t] / rated discharge power | [0, 1] | Per hour |
| **Battery LCOE** | (charge × price / round-trip efficiency + (charge + discharge) × maintenance_cost) / discharge | $/MWh | Hours with discharge only, clipped to [0, 10000] |
| **Battery VALLCOE** | Battery LCOE × (1 + energy_adj + capacity_adj − 0.05) | $/MWh | Adjustments computed on discharge |
| **Technology Selling Prices** | Σ price × output / Σ output, per generator | $/MWh | Stored with the (price, output, timestep) rows in `technology_selling_prices/` |

The RE penetration (`renewable_penetration`) is computed in the dispatch, not here. Full formulas: [HDF5 Output Schema § Derived Metrics Formulas](../reference/hdf5-output-schema.md#derived-metrics-formulas).

### Capacity Factor Interpretation

The annual capacity factor of a unit is the mean of its hourly values. Typical ranges:

| Range | Interpretation |
|-------|---------------|
| 0.85 - 1.0 | Baseload operation (nuclear, some coal) |
| 0.40 - 0.85 | Mid-merit / high-resource renewable |
| 0.15 - 0.40 | Typical solar PV or moderate wind |
| 0.05 - 0.15 | Peaking unit (gas turbine, diesel) |
| < 0.05 | Rarely dispatched; candidate for retirement |

---


## Export Commands

`-o/--output` is always a directory (default: `export/` next to the results file). It is created if needed.

### CSV Export

One CSV file per dataset:

```bash
esfex export -r results/isla_juventud_15012026_1030.h5 -f csv -o results/csv/
```

Output structure (one folder per scenario, directly under the output directory):

```
results/csv/
├── summary/                    # /summary_results/*
│   ├── year.csv
│   ├── threshold.csv
│   ├── feasible.csv
│   ├── total_cost.csv
│   ├── renewable_penetration.csv
│   ├── co2_emissions.csv
│   ├── loss_of_load.csv
│   └── n1_security_cost.csv
├── year_2025_threshold_0/      # /detailed_results/year_2025_threshold_0/*
│   ├── generation/
│   │   ├── Solar PV.csv
│   │   └── …                   # one CSV per generator
│   ├── curtailment.csv
│   ├── loss_load.csv
│   ├── nodal_electricity_prices.csv
│   ├── electricity_prices.csv
│   ├── battery_charge/
│   ├── battery_discharge/
│   ├── CO2_emissions.csv
│   ├── capacity_factor/
│   └── lcoe/
└── demand/                     # /demand/*
    └── year_2025_base_demand.csv
```

`[node, step]` datasets keep their layout (one row per node, one column per step). The 3-D `power_flow.csv` and `transfer_margin.csv` of multi-node runs have one row per node pair: `from_node`, `to_node`, then one column per step. Nested groups (`technology_selling_prices/<gen>/`, `primary_energy/`, `n1_security/trans_reserve_duals/`) become nested folders.

Attributes are not exported, so `cost_breakdown/`, `global/`, `inter_system/`, `mga/` and `system_configuration/` are left out.

### Excel Export

Single workbook with summary and selected operational data:

```bash
esfex export -r results/isla_juventud_15012026_1030.h5 -f excel -o results/
```

This writes `results/isla_juventud_15012026_1030.xlsx` (the results file name with `.xlsx`). Sheets:

- **Summary**: per-year scalars from `/summary_results/` (one row per year)
- **Generation**: first year only; one column per generator, summed across nodes, one row per timestep

### JSON Export

```bash
esfex export -r results/isla_juventud_15012026_1030.h5 -f json -o results/
```

This writes `results/isla_juventud_15012026_1030.json` with the root attributes, the `/summary_results/` arrays, and the attributes of each scenario (all attribute values as strings). Time-series data is omitted; use CSV or direct HDF5 access for time-series analysis.


---


## Python API

### Direct HDF5 Access

The scenario key encodes the year and the threshold iteration: `year_2025_threshold_0`. Generation, status and battery data live in groups keyed by unit name (one dataset per generator / battery).

```python
import h5py

with h5py.File("results/isla_juventud_15012026_1030.h5", "r") as f:
    # Top-level groups
    print("Groups:", list(f.keys()))

    # Root metadata
    print("Created:        ", f.attrs.get("creation_date"))
    print("Years range:    ", f.attrs.get("years_range"))
    print("Temporal res:   ", f.attrs.get("temporal_resolution_hours"), "h")
    print("Mode:           ", f.attrs.get("simulation_mode"))
    # Multi-system layout (absent for single-system runs)
    if "subsystem_names" in f.attrs:
        names = [n.decode() if isinstance(n, bytes) else str(n)
                 for n in f.attrs["subsystem_names"]]
        offsets = f.attrs["subsystem_offsets"]
        counts = f.attrs["subsystem_node_counts"]
        for n, o, c in zip(names, offsets, counts):
            print(f"  subsystem {n}: nodes [{o}, {o+c}) ({c} node(s))")

    # ── Per-year summary (one row per year) ──
    years = f["summary_results/year"][:]
    costs = f["summary_results/total_cost"][:]
    re_pen = f["summary_results/renewable_penetration"][:]
    for y, c, r in zip(years, costs, re_pen):
        print(f"  {y}: cost=${c:,.0f}  RE={r:.1%}")

    # ── Detailed time series for one year ──
    sc = f["detailed_results/year_2025_threshold_0"]

    # Per-generator output: group with one (N, T) dataset per generator
    for gen_name in list(sc["generation"].keys())[:5]:
        arr = sc["generation"][gen_name][:]   # shape (N, T)
        print(f"  {gen_name}: total = {arr.sum():,.0f} MW·step")

    # Nodal LMP and curtailment
    lmp = sc["nodal_electricity_prices"][:]   # (N, T)
    print(f"  LMP: mean=${lmp.mean():.2f}/MWh  max=${lmp.max():.2f}/MWh")
    curt = sc["curtailment"][:]                # (N, T) for new files,
                                               # (T,) for legacy
    print(f"  Curtailment total: {curt.sum():,.0f} MW·step")

    # Loss of load (note the field name)
    ll = sc["loss_load"][:]                   # (N, T)
    print(f"  Loss of load: {ll.sum():,.0f} MWh")
```

### Plotting Generation Stack

```python
import h5py
import matplotlib.pyplot as plt

with h5py.File("results/isla_juventud_15012026_1030.h5", "r") as f:
    sc = f["detailed_results/year_2025_threshold_0"]
    gen_group = sc["generation"]                 # group keyed by unit name

    gen_names = sorted(gen_group.keys())
    # Sum across nodes for each generator → (G, T)
    gen_total = [gen_group[name][:].sum(axis=0) for name in gen_names]

    tres = int(f.attrs.get("temporal_resolution_hours", 1))
    steps_per_week = (24 * 7) // tres            # e.g. 56 steps for 3-hourly

    fig, ax = plt.subplots(figsize=(14, 6))
    ax.stackplot(range(steps_per_week),
                 [g[:steps_per_week] for g in gen_total],
                 labels=gen_names, alpha=0.8)
    ax.set_xlabel(f"Step (× {tres} h)")
    ax.set_ylabel("Generation (MW)")
    ax.set_title("Generation Stack — Week 1 (Year 2025)")
    ax.legend(loc="upper right", fontsize=7, ncol=2)
    plt.tight_layout()
    plt.savefig("generation_stack.png", dpi=150)
```

### Plotting Prices

```python
with h5py.File("results/isla_juventud_15012026_1030.h5", "r") as f:
    sc = f["detailed_results/year_2025_threshold_0"]
    lmp = sc["nodal_electricity_prices"][:]    # (N, T)

    tres = int(f.attrs.get("temporal_resolution_hours", 1))
    steps_per_week = (24 * 7) // tres

    fig, ax = plt.subplots(figsize=(14, 4))
    for n in range(lmp.shape[0]):
        ax.plot(lmp[n, :steps_per_week], label=f"Node {n}", alpha=0.6)
    ax.set_xlabel(f"Step (× {tres} h)")
    ax.set_ylabel("Price ($/MWh)")
    ax.set_title("Locational Marginal Prices — Week 1")
    ax.legend(ncol=4, fontsize=8)
    plt.tight_layout()
    plt.savefig("prices.png", dpi=150)
```

### Computing Custom Metrics

```python
with h5py.File("results/isla_juventud_15012026_1030.h5", "r") as f:
    sc = f["detailed_results/year_2025_threshold_0"]

    # Total generation per generator (summed across nodes and time)
    gen_group = sc["generation"]
    tres = int(f.attrs.get("temporal_resolution_hours", 1))
    print("Per-generator energy (MWh):")
    gen_total = {}
    for name in sorted(gen_group.keys()):
        arr = gen_group[name][:]               # (N, T)
        mwh = arr.sum() * tres                 # MW · h
        gen_total[name] = mwh
        if mwh > 0:
            print(f"  {name:50s} {mwh:>14,.0f}")

    # Energy not served (ENS) and Loss of Load Events (LOLE)
    ll = sc["loss_load"][:]                    # (N, T)
    demand = sc["demand"][:]                   # (N, T); (T, N) if N > T
    ens = ll.sum() * tres                      # MWh of unserved demand
    total_demand = demand.sum() * tres
    print(f"\nENS:  {ens:,.0f} MWh ({ens/total_demand:.4%} of demand)")

    # Loss-of-load events: count of timesteps with any unserved demand
    lole_events = (ll.sum(axis=0) > 0.01).sum()
    print(f"LOLE: {lole_events} steps  ({lole_events*tres} h)")
```

### Using ResultsExporter

```python
from esfex.io.exporter import ResultsExporter

exporter = ResultsExporter("results/isla_juventud_15012026_1030.h5")

# Export to CSV (fails on files with 3-D datasets; see CSV Export above)
exporter.to_csv("results/csv/")

# Export to Excel
exporter.to_excel("results/report.xlsx")

# Export to JSON
exporter.to_json("results/data.json")
```

### Using read_results for Full Data Access

```python
from esfex.io.exporter import read_results

results = read_results("results/isla_juventud_15012026_1030.h5")

# Access metadata
print(results["metadata"])

# Access scenarios
for scenario_name, data in results["scenarios"].items():
    print(f"\nScenario: {scenario_name}")
    print(f"  Attributes: {data['attrs']}")

    # Access hourly data
    hourly = data["hourly_data"]
    if "generation" in hourly:
        for gen_name, gen_array in hourly["generation"].items():
            print(f"  {gen_name}: shape={gen_array.shape}, "
                  f"total={gen_array.sum():,.0f} MWh")
```

### Using YearResults (Programmatic Runs)

Results are returned directly as Python objects:

```python
from esfex.config.loader import load_config
from esfex.runner import Orchestrator

config = load_config("config.yaml")
orch = Orchestrator(config)
results = orch.run(years=10)

# Access year 1 results
yr1 = results[0]
print(f"Year:           {yr1.year}")
print(f"Objective:      ${yr1.objective:,.0f}")
print(f"RE penetration: {yr1.re_penetration:.1%}")
print(f"Generation:     shape={yr1.gen_output.shape}  (G × N × T)")
print(f"LMPs:           shape={yr1.prices.shape}      (N × T)")
print(f"Load shed:      {yr1.load_shed:,.1f} MWh "
      f"(per-step: yr1.load_shed_array, shape={yr1.load_shed_array.shape})")
print(f"CO2 emissions:  {yr1.emissions:,.0f} tonnes")
```

**Note**: the `YearResults` dataclass exposes both a scalar `load_shed` (total MWh for the year) and a time-series `load_shed_array` (shape `(N, T)`). The same pattern applies to `emissions` (scalar) and `co2_emissions` (`(N, T)` array). Use whichever fits your downstream code.

---


## MGA / SPORES Results

Near-optimal alternatives land in `/mga/` *inside the same results file* — there is no separate `mga_*.h5` file. The group root carries the metadata the viewer needs to pick a colour / label encoding (see [Near-Optimal Alternatives tutorial](../tutorials/mga.md)).

```python
with h5py.File("results/isla_juventud_15012026_1030.h5", "r") as f:
    if "mga" not in f:
        print("This run did not enable MGA / SPORES.")
    else:
        mga = f["mga"]
        # Root attributes describe the method and the cost envelope.
        method = mga.attrs.get("method", b"mga")
        method = method.decode() if isinstance(method, bytes) else str(method)
        objectives = [o.decode() if isinstance(o, bytes) else str(o)
                      for o in mga.attrs.get("objectives", [])]
        print(f"Method:       {method}")
        print(f"Objectives:   {objectives or '— (MGA HSJ loop)'}")
        print(f"Optimal cost: ${mga.attrs['optimal_cost']/1e9:.2f}B")
        print(f"Slack:        {mga.attrs['slack_fraction']*100:.1f}%")

        alt_keys = [k for k in mga.keys() if k.startswith("alternative_")]
        for k in sorted(alt_keys, key=lambda s: int(s.rsplit("_", 1)[-1])):
            alt = mga[k]
            obj = alt.attrs.get("objective", b"")
            obj = obj.decode() if isinstance(obj, bytes) else str(obj)
            cost = float(alt.attrs["cost"])
            opt = bool(alt.attrs.get("is_optimal", False))
            tag = "★" if opt else " "
            print(f"  {tag} {k:18s} cost=${cost/1e9:.2f}B  objective={obj}")
            # Per-alternative datasets
            for ds in ("tech_investment", "bat_tech_power_investment",
                       "bat_tech_capacity_investment", "transfer_investment",
                       "cumulative_gen_capacity", "re_penetration"):
                if ds in alt:
                    print(f"      {ds:30s} {alt[ds].shape}")
```

### What's in each alternative

| Dataset | Shape | Units | Description |
|---------|-------|-------|-------------|
| `tech_investment` | (years, techs, nodes) | MW | New technology power installed per year, tech and node |
| `bat_tech_power_investment` | (years, bat_techs, nodes) | MW | New battery power per year, tech and node |
| `bat_tech_capacity_investment` | (years, bat_techs, nodes) | MWh | New battery energy per year, tech and node |
| `transfer_investment` | (n, 4) | MW | One row `[year, i, j, MW]` per transmission investment; absent when there are none |
| `cumulative_gen_capacity` | (years, generators, nodes) | MW | Total deployed capacity per generator over time (generator order in `/mga/generator_names`) |
| `re_penetration` | (years,) | fraction | Annual RE-penetration trajectory |

### Per-alternative attributes (Phase 4 export)

| Attribute | Type | Description |
|-----------|------|-------------|
| `alternative_id` | int | 0 = cost-optimal seed, 1..K = non-optimal alternatives |
| `is_optimal` | bool | `True` for `alternative_0` only |
| `cost` | float | Actual system cost ($) — non-optimal alternatives typically saturate the cost cap (1 + slack)·optimal_cost |
| `diversity_objective` | float | Objective value at this alternative (absent for the seed) |
| `objective` | string | Tag identifying which objective produced this alternative: `"cost_optimal"` (seed), `"hsj_diversity"` (any MGA alt), or one of `"min_total_build"`, `"max_tech_equity"`, `"max_regional_equity"`, `"evolutionary_dist"` (SPORES alts). Result files predating Phase 4 fall back to `"cost_optimal"` / `"hsj_diversity"` on read |

See [HDF5 Output Schema § MGA/SPORES Results](../reference/hdf5-output-schema.md#mgaspores-results) for the full attribute catalogue and [Near-Optimal Alternatives](../tutorials/mga.md) for a worked walkthrough of both methods.

---


## Troubleshooting

| Issue | Cause | Solution |
|-------|-------|----------|
| HDF5 file is empty or very small | Optimization failed or was interrupted | Check console output for error messages; re-run with `--verbose` |
| Missing virtual generators in output | `_rebuild_unit_names` was not called | This was fixed in v0.1.0; update to latest version |
| GUI shows 90%+ load shedding but optimization reports feasible | Virtual units missing from HDF5 export | Ensure unit name lists match adapter ordering (original then virtual) |
| `KeyError` when accessing a dataset | Dataset name may differ between versions | List available keys with `list(f["detailed_results/..."].keys())` |
| Large file size (> 1 GB) | Many years, nodes, or generators | Use compression; data is already gzip-compressed by default |
