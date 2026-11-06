# Quickstart

This page builds a single-node island system by hand: an existing diesel plant, plus solar PV and lithium-ion storage that the optimizer may build. The same file can also be created and edited in the [Studio](#step-6-open-the-configuration-in-the-studio-optional).


## Step 1: Prepare the Input Data

ESFEX needs an hourly demand profile and, for each renewable technology, an hourly availability (capacity-factor) profile. Both can be CSV files without a header row: one row per hour, one column per node. For a quick test, generate synthetic profiles:

```python
import numpy as np

h = np.arange(8760)                                          # one year, hourly
demand = 30 + 10 * np.sin(2 * np.pi * (h % 24 - 6) / 24)     # MW, between 20 and 40
solar = np.clip(np.sin(np.pi * (h % 24 - 6) / 12), 0, None)  # capacity factor, 0..1

np.savetxt("demand.csv", demand, fmt="%.3f")
np.savetxt("solar.csv", solar, fmt="%.4f")
```

Put both files next to the configuration file; relative paths in the configuration are resolved from the configuration's folder.

!!! note "Demand and availability files"
    - Data start in the first row, in CSV and Excel files alike; a header row (for example node names) is optional and skipped.
    - Demand is in MW, one column per node, 8760 rows per year (leap years too: Feb 29 rows are dropped). If the file holds at least one year but fewer years than simulated, the first year is repeated for the remaining years.
    - An availability file with a single column is applied to every node. Values are clipped to 0..1.

See [Demand Data](../user-guide/demand-data.md) and [Availability Profiles](../user-guide/availability-profiles.md) for the full formats.


## Step 2: Write the Configuration File

Save this as `my_system.yaml`:

```yaml
simulation_mode: development
date_start: "01/01/2025 00:00"
enable_primary_energy: false

temporal:
  use_rolling_horizon: true
  rolling_horizon_hours: 48
  overlap_hours: 24

solver:
  name: highs
  threads: 4
  time_limit: 3600
  gap: 0.01

master_problem:
  representative_days: 5
  min_day_separation: 5

meta_network:
  systems: [island]

systems:
  island:
    name: island
    demand_path: demand.csv        # relative to this file
    TARGET_RE_PENETRATION: 0.8     # renewable share to reach by the last year

    nodes:
      nodes_connections: [0.0]     # flattened N x N line-capacity matrix (MW); here N = 1
      node_names: [Main]

    generators:                    # existing units; every field below is required
      diesel:
        name: Diesel
        type: Non-renewable
        fuel: Diesel
        rated_power: [60.0]        # MW
        life_time: [30]            # years
        initial_age: [10]          # years; retires after 20 more years
        degradation_rate: [0.0]
        decommissioning_cost: [0.0]
        min_power: [0.0]
        min_up: [0]
        min_down: [0]
        ramp_up: [1.0]
        ramp_down: [1.0]
        eff_at_rated: [0.35]
        eff_at_min: [0.25]
        inertia: [2.0]             # s
        start_up_cost: [0.0]
        fuel_cost: [80.0]          # $/MWh
        fixed_cost: [0.0]
        maintenance_cost: [10.0]   # $/MWh
        invest_cost: [1000000.0]   # $/MW; prices life extension past life_time
        invest_max_power: [0.0]

    technologies:                  # candidates the master problem may build
      solar_pv:
        name: Solar PV
        type: Renewable
        fuel: Sun
        Availability: solar.csv    # hourly capacity factor
        invest_cost: [800000.0]    # $/MW
        invest_max_power: [200.0]  # MW
        eff_at_rated: [1.0]
        degradation_rate: [0.005]  # 0.5 % per year
        lifetime: 25

    battery_technologies:
      li_ion:
        name: Li-Ion
        invest_cost_power: [200000.0]   # $/MW
        invest_cost_energy: [150000.0]  # $/MWh
        invest_max_power: [100.0]       # MW
        invest_max_capacity: [400.0]    # MWh
        efficiency_charge: [0.95]
        efficiency_discharge: [0.95]
        degradation_rate: [0.0]
        lifetime: 15

    penalties:
      loss_of_load: 10000.0        # $/MWh of unserved demand
      max_curtailment_ratio: 0.05  # at most 5 % of renewable output curtailed
```

### Understanding the Key Sections

| Section | Purpose |
|---------|---------|
| `simulation_mode` | `development` plans investments (master problem) and then dispatches each year. `unit_commitment` and `economic_dispatch` skip the master problem and dispatch the existing fleet. See [Core Concepts](concepts.md). |
| `temporal` | Rolling-horizon window and overlap for the hourly dispatch. 48 h with 24 h overlap is the default. |
| `solver` | The optimization solver. HiGHS ships with ESFEX. See the [Solver Guide](../user-guide/solver-guide.md). |
| `master_problem` | Capacity-expansion settings. `representative_days` (default 5) is the number of days per year the master problem optimizes. |
| `systems.island` | One power system. Some system keys have upper-case legacy names (`TARGET_RE_PENETRATION`, `LOSS_DEMAND_TRHESHOLD`, ...), as the Studio writes them; the lower-case field name (`target_re_penetration`) is accepted as well. |
| `nodes` | Network topology. `nodes_connections` is the N × N matrix of line capacities, flattened row by row. |
| `generators` | Existing units. Every per-node field is a list with one entry per node. The loader requires all fields shown here; it does not fill in defaults. For an existing unit, `invest_cost` is used to price keeping it beyond its `life_time` (see [Age-Based Retirement](concepts.md#age-based-retirement)). |
| `technologies` / `battery_technologies` | Investment candidates. The master problem decides how much to build, where and when. |
| `penalties` | Soft-constraint costs; `loss_of_load` is the cost of unserved energy. Names are case-insensitive. |

Settings not listed take their defaults. Two of them matter for this example: `master_problem.planning_mode` is `perfect_foresight`, and `co2_budget` is enabled with 1,000,000 t/year. The diesel unit has no CO₂ emissions here because no `fuels:` block gives Diesel an emission factor; see the [Configuration Guide](../user-guide/configuration.md).

!!! warning "Unknown keys are ignored"
    A misspelled key (for example `representative_days_per_year` instead of `representative_days`) is silently ignored and the default is used. Check spellings against the [Configuration Reference](../reference/config-reference.md).

---


## Step 3: Validate the Configuration

```bash
esfex validate -c my_system.yaml
```

Expected output:

```
Validating: my_system.yaml
Configuration is valid!
        Configuration Summary
┏━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━┓
┃ Setting             ┃ Value       ┃
┡━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━┩
│ Simulation Mode     │ development │
│ Solver              │ highs       │
│ Systems             │ island      │
│   island nodes      │ 1           │
│   island generators │ 1           │
│   island batteries  │ 0           │
│ Rolling Horizon     │ True        │
│ Primary Energy      │ False       │
│ N-1 Security        │ False       │
└─────────────────────┴─────────────┘
```

The counts include existing units only, not the investment candidates.

Validation catches YAML syntax errors, missing required unit fields (reported as e.g. `Failed to load configuration: 'min_power'`), wrong types and out-of-range values. It does **not** check per-node list lengths, unknown keys, or whether `demand.csv` and `solar.csv` exist; those surface when the run starts. See the [CLI reference](../user-guide/cli.md#esfex-validate).

---


## Step 4: Run the Simulation

Optionally, check the setup first without starting Julia:

```bash
esfex run -c my_system.yaml --dry-run -v
```

This loads the configuration, prints the summary table and stops.

Then run a 10-year study:

```bash
esfex run -c my_system.yaml --years 10 --verbose
```

Without `--years`, ESFEX simulates 25 years. The years are labelled from the year of `date_start` (2025 here); `--start-year` changes the first label.

Execution stages:

1. **Load** the configuration
2. **Load demand** for all 10 years (here: the one-year file is repeated)
3. **Preload availability profiles** for renewable units and technologies
4. **Solve the master problem** once: capacity expansion over all 10 years on representative days (followed by the MGA/SPORES alternatives, if enabled)
5. **For each year:**
    - apply that year's investments and retirements
    - solve the hourly **operational dispatch** with the rolling horizon
    - compute derived metrics (capacity factor, LCOE, VALLCOE)
    - append the year's results to the HDF5 file
6. **Finish** the HDF5 file

The first run also starts Julia and compiles the model, which adds a few minutes.

### Console Output

`--verbose` prints the configuration summary, debug-level log messages and the solver log. For every year there is a result line in this format:

```
Year 2025 completed: objective=$8,456,789
  → RE=62.3% | cost=$8,456,789 | gen=262,800MWh | shed=0.0MWh
```

and the run ends with:

```
Optimization completed successfully!
Results saved to: results
```

| Field | Meaning |
|-------|---------|
| `objective` / `cost` | Objective value of that year's operational dispatch ($) |
| `RE` | Renewable penetration of the year |
| `gen` | Total generation (MWh) |
| `shed` | Unserved energy (MWh) |

Without `--verbose`, the console shows progress milestones only (set by `logging.console_level`, default `basic`). The complete DEBUG log is always written to `results/logs/`.

### Typical Runtimes

| System size | Years | Approximate runtime |
|-------------|-------|-------------------|
| 1 node, 2 generators | 10 | 5--15 minutes |
| 1 node, 5 generators | 25 | 20--60 minutes |
| 5 nodes, 10 generators | 25 | 1--4 hours |
| 10+ nodes, unit commitment | 25 | 4--24 hours |

Runtimes depend on the solver, the number of rolling-horizon windows, the temporal resolution and the available CPU cores.


---


## Step 5: Inspect Results

Each run writes one HDF5 file to `results/`, named `<config name>_<ddmmyyyy>_<HHMM>.h5`, e.g. `results/my_system_29102026_1430.h5`.

### Export to CSV

```bash
esfex export -r results/my_system_29102026_1430.h5 -f csv -o results/csv/
```

This writes one CSV per dataset:

```
results/csv/
├── summary/                     # one file per summary series, one row per year
│   ├── year.csv
│   ├── total_cost.csv
│   ├── renewable_penetration.csv
│   ├── co2_emissions.csv
│   ├── loss_of_load.csv
│   └── ...
├── year_2025_threshold_0/       # one directory per year
│   ├── generation/
│   │   ├── Diesel.csv
│   │   └── ...
│   ├── battery_soc/
│   ├── curtailment.csv
│   ├── loss_load.csv
│   ├── nodal_electricity_prices.csv
│   └── ...
├── year_2026_threshold_0/
│   └── ...
└── demand/
```

### Export to Excel

```bash
esfex export -r results/my_system_29102026_1430.h5 -f excel -o results/
```

Writes `results/my_system_29102026_1430.xlsx` with a `Summary` sheet and a `Generation` sheet for the first year.

### Read Results in Python

```python
import h5py

with h5py.File("results/my_system_29102026_1430.h5", "r") as f:
    print("Groups:", list(f.keys()))

    # Per-year summary series
    summary = f["summary_results"]
    for year, cost, re in zip(summary["year"][:], summary["total_cost"][:],
                              summary["renewable_penetration"][:]):
        print(f"{year}: cost=${cost:,.0f}, RE={re:.1%}")

    # Hourly generation of each unit in the first year
    year1 = f["detailed_results/year_2025_threshold_0"]
    for name, ds in year1["generation"].items():
        print(name, ds.shape, f"{ds[:].sum():,.0f} MWh")
```

### Where to Look

| Result | Location in HDF5 |
|--------|------------------|
| Per-year cost, RE share, CO₂, unserved energy | `summary_results/` |
| Generator output | `detailed_results/year_<YYYY>_threshold_0/generation/<unit>` |
| Battery charge / discharge / SOC | `detailed_results/year_<YYYY>_threshold_0/battery_charge/`, `battery_discharge/`, `battery_soc/` |
| Unserved energy | `detailed_results/year_<YYYY>_threshold_0/loss_load` |
| Curtailment | `detailed_results/year_<YYYY>_threshold_0/curtailment` |
| Electricity prices | `detailed_results/year_<YYYY>_threshold_0/nodal_electricity_prices` |
| MGA / SPORES alternatives (if enabled) | `mga/` |

The [HDF5 Output Schema](../reference/hdf5-output-schema.md) lists every group and dataset, and [Results Export](../user-guide/results-export.md) has more analysis examples.

**Sanity checks:**

- **Unserved energy** near zero in most hours. Persistent shedding indicates insufficient generation or transmission capacity.
- **RE penetration** increasing year over year toward the target.
- **Battery SOC** cycling daily (charge during solar hours, discharge in the evening).
- **Electricity prices** reflecting marginal generation cost. High prices indicate system stress.

---


## Alternative: Run from Python


### Basic Python Workflow

```python
from pathlib import Path

from esfex import load_config
from esfex.runner import Orchestrator

config_file = Path("my_system.yaml")

# 1. Load and validate the configuration
config = load_config(config_file)

# 2. Create the orchestrator; config_path is used to resolve relative
#    data paths and to name the results file
orchestrator = Orchestrator(
    config=config,
    output_dir="./results",
    config_path=config_file,
)

# 3. Run the simulation (returns one YearResults per year)
results = orchestrator.run(years=10)

for yr in results:
    print(f"Year {yr.year}: "
          f"cost=${yr.objective:,.0f}, "
          f"RE={yr.re_penetration:.1%}, "
          f"CO2={yr.emissions:,.0f} t, "
          f"shed={yr.load_shed:,.1f} MWh")
```

The HDF5 file is written as with the CLI.

!!! note
    Pass `config_path` as a `pathlib.Path`. A plain string currently fails with `AttributeError: 'str' object has no attribute 'parent'`. Without `config_path`, relative data paths are resolved from the current directory and the results file is named after the system.

### Multi-Year Summary

```python
import pandas as pd

summary = pd.DataFrame([{
    "year": yr.year,
    "total_cost": yr.objective,
    "re_penetration": yr.re_penetration,
    "co2_emissions": yr.emissions,
    "load_shed": yr.load_shed,
    "total_generation": yr.total_generation,
} for yr in results])

print(summary.to_string(index=False))
summary.to_csv("simulation_summary.csv", index=False)
```

### Modifying the Configuration Programmatically

```python
from pathlib import Path

from esfex import load_config
from esfex.runner import Orchestrator

config_file = Path("my_system.yaml")
config = load_config(config_file)

# Change solver settings
config.solver.threads = 8

# Allow more solar
island = config.systems["island"]
island.technologies["solar_pv"].invest_max_power = [400.0]

# Change the RE target
island.target_re_penetration = 0.95

orchestrator = Orchestrator(config=config, output_dir="./results_95",
                            config_path=config_file)
results = orchestrator.run(years=15)
```

### Running Parameter Sweeps

```python
from pathlib import Path

import pandas as pd
from esfex import load_config
from esfex.runner import Orchestrator

config_file = Path("my_system.yaml")
rows = []
for target in [0.6, 0.7, 0.8, 0.9, 1.0]:
    config = load_config(config_file)
    config.systems["island"].target_re_penetration = target

    orch = Orchestrator(config=config,
                        output_dir=f"./results/re_{int(target * 100)}",
                        config_path=config_file)
    results = orch.run(years=10)

    rows.append({
        "re_target": target,
        "final_re": results[-1].re_penetration,
        "total_cost": sum(yr.objective for yr in results),
    })

print(pd.DataFrame(rows).to_string(index=False))
```

---


## Step 6: Open the Configuration in the Studio (Optional)

```bash
esfex studio -c my_system.yaml
```

The Studio provides:

- **Interactive map** -- place nodes, generators, batteries and transmission lines on an OpenStreetMap-based map
- **Property forms** -- edit equipment parameters (rated power, costs, lifetimes) in structured forms
- **Polyline transmission lines** -- draw lines with waypoints that follow geographic routes
- **Multi-system management** -- create and switch between interconnected power systems
- **Resource wizards** -- generate solar PV, wind and OTEC availability profiles from geographic data
- **Validation and runs** -- check the configuration and start simulations from the GUI
- **Save to YAML** -- write the design as a configuration file ready for `esfex run`

Save from inside the Studio (File → Save). See the [GUI Editor](../gui/overview.md) pages.

---


## Next Steps


### Learn the Model

- [Core Concepts](concepts.md) -- the two-stage optimization, rolling horizon and penalty structure
- [System Architecture](architecture.md) -- how the Python orchestrator, Julia backend and bridge layer interact
- [Mathematical Formulation](../formulation/overview.md) -- the full mathematical description of the model

### Build Real Systems

- [Single-System Tutorial](../tutorials/single-system.md) -- an island power system with several generator types
- [Multi-Node Tutorial](../tutorials/multi-node.md) -- transmission lines and DC power flow between nodes
- [GUI Workflow](../tutorials/gui-workflow.md) -- build a system in the Studio

### Advanced Features

- [Configuration Guide](../user-guide/configuration.md) -- every YAML field
- [EV Integration](../tutorials/ev-integration.md) -- electric vehicle fleet adoption and V2G
- [Stochastic Planning](../tutorials/stochastic.md) -- demand and availability uncertainty with scenarios
- [Near-Optimal Alternatives](../tutorials/mga.md) -- explore near-optimal investment portfolios with MGA and SPORES
- [Sensitivity Analysis](../tutorials/sensitivity-analysis.md) -- Sobol indices for parameter uncertainty
- [Primary Energy](../tutorials/primary-energy.md) -- fuel supply chains with storage and transport
