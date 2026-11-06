# Single-Node Island System

## Prerequisites

- ESFEX installed: `pip install esfex`
- A working solver (HiGHS is included by default)
- Python 3.10+ with `h5py`, `numpy`, and `matplotlib` for results analysis

---

## Scenario

An isolated island currently relies on diesel generators. We want to plan a 10-year transition toward 80% renewable energy by investing in solar PV, wind turbines, and battery storage.

**System characteristics:**

- 1 node (single bus)
- Peak demand: ~200 MW
- Existing: 250 MW diesel, 50 MW solar PV
- Candidates: Additional solar PV, wind, Li-ion batteries
- Target: 80% renewable energy by year 10
- CO2 budget: 500,000 tonnes/year

---

## Step 1: Create the Configuration

Create `island_system.yaml` with the configuration below. The existing diesel and solar plants are `generators`; the units the planner may build are `technologies` and `battery_technologies`. The master problem only invests through those candidates: an existing generator's `invest_max_power` does not add buildable capacity.

```yaml
simulation_mode: development
date_start: "01/01/2025 00:00"
enable_primary_energy: false

temporal:
  resolution_hours: 1
  use_rolling_horizon: true
  rolling_horizon_hours: 48
  overlap_hours: 24

solver:
  name: highs
  threads: 4
  time_limit: 1800
  gap: 0.01
  verbose: false

n1_security:
  enabled: false

master_problem:
  stochastic: false
  representative_days: 5
  min_day_separation: 7

meta_network:
  systems:
    - island

systems:
  island:
    name: island
    demand_path: island_demand.csv
    demand_scale: 1.0

    nodes:
      nodes_connections: [0.0]          # flattened N x N matrix; N = 1
      node_coordinates:
        - {latitude: 18.00, longitude: -76.80}
      node_names: ["Main Island"]

    generators:                         # existing units; all fields below are required
      solar_pv:
        name: Solar PV
        type: Renewable
        fuel: Solar
        Availability: solar_availability.csv
        rated_power: [50.0]             # MW
        min_power: [0.0]
        min_up: [0]                     # hours
        min_down: [0]
        ramp_up: [1.0]                  # fraction of capacity per hour
        ramp_down: [1.0]
        eff_at_rated: [1.0]
        eff_at_min: [1.0]
        inertia: [0.0]
        start_up_cost: [0.0]
        fuel_cost: [0.0]
        fixed_cost: [5.0]
        maintenance_cost: [2.0]
        life_time: [25]
        initial_age: [3]
        degradation_rate: [0.005]
        decommissioning_cost: [0.0]
        invest_cost: [750000.0]         # prices life extension only
        invest_max_power: [0.0]

      diesel:
        name: Diesel Generator
        type: Non-renewable
        fuel: Diesel
        rated_power: [250.0]
        min_power: [0.3]
        min_up: [4]
        min_down: [2]
        ramp_up: [0.5]
        ramp_down: [0.5]
        eff_at_rated: [0.40]
        eff_at_min: [0.30]
        inertia: [5.0]
        start_up_cost: [5000.0]
        fuel_cost: [85.0]               # $/MWh
        fixed_cost: [3.0]
        maintenance_cost: [5.0]
        life_time: [30]
        initial_age: [15]
        degradation_rate: [0.01]
        decommissioning_cost: [100000.0]
        invest_cost: [500000.0]
        invest_max_power: [0.0]

    technologies:                       # candidates the master problem may build
      solar_pv_new:
        name: Solar PV
        type: Renewable
        fuel: Solar
        Availability: solar_availability.csv
        invest_cost: [750000.0]         # $/MW
        invest_max_power: [500.0]       # cumulative MW over the horizon
        eff_at_rated: [1.0]
        degradation_rate: [0.005]
        lifetime: 25
        fixed_cost: [5.0]
        maintenance_cost: [2.0]

      wind:
        name: Wind Turbines
        type: Renewable
        fuel: Wind
        Availability: wind_availability.csv
        invest_cost: [1200000.0]
        invest_max_power: [300.0]
        eff_at_rated: [1.0]
        degradation_rate: [0.005]
        lifetime: 20
        fixed_cost: [8.0]
        maintenance_cost: [5.0]

    battery_technologies:
      li_ion:
        name: Li-Ion Battery
        invest_cost_power: [200000.0]   # $/MW
        invest_cost_energy: [150000.0]  # $/MWh
        invest_max_power: [200.0]       # MW
        invest_max_capacity: [800.0]    # MWh
        min_duration_hours: 2.0
        max_duration_hours: 6.0
        efficiency_charge: [0.95]
        efficiency_discharge: [0.95]
        soc_initial: [0.5]
        max_DoD: [0.9]
        degradation_rate: [0.02]
        lifetime: 15
        maintenance_cost: [1.0]

    penalties:
      loss_of_load: 10000.0             # $/MWh unserved
      curtailment_cost: 50.0            # $/MWh curtailed in dispatch
      max_curtailment_ratio: 0.05       # cap on curtailment / RE output (master)
      loss_of_reserve_static: 500.0     # $/MW
      fre_penetration_loss: 600.0       # $/MWh below the RE target

    co2_budget:
      enabled: true
      annual_budget: 500000.0           # t CO2/year

    TARGET_RE_PENETRATION: 0.80
    discount_rate: 0.08
    MAX_ANNUAL_SYSTEM_COST: 500000000.0
```

### Key Parameters

| Parameter | Value | Purpose |
|-----------|-------|---------|
| `simulation_mode` | `development` | Master problem plans investments, then each year is dispatched (LP economic dispatch) |
| `resolution_hours` | `1` | Hourly time steps for operational dispatch |
| `rolling_horizon_hours` | `48` | Each operational window solves 48 hours at a time |
| `overlap_hours` | `24` | 24-hour look-ahead so storage sees the next daily peak |
| `representative_days` | `5` | Master problem uses 5 representative days per year |
| `TARGET_RE_PENETRATION` | `0.80` | 80% renewable energy target by the final year |
| `max_curtailment_ratio` | `0.05` | In the master problem, no more than 5% of renewable output can be curtailed on each representative day |
| `discount_rate` | `0.08` | 8% discount rate for NPV calculations |

The starting RE share is computed from the existing fleet; there is no key to set it.

---

## Step 2: Prepare Input Data

### Demand File

Create `island_demand.csv` with 8,760 rows (1 year of hourly data), 1 column (1 node) and no header row. The values are the load in MW at each hour:

```csv
120.5
115.3
112.0
...
125.0
```

The demand profile should reflect typical island load patterns: lower overnight (80-120 MW), morning ramp (120-170 MW), afternoon peak (170-200 MW), and evening decline.

There is no demand-growth key. A one-year file is repeated for every simulated year. To model growth, write every year into the file (87,600 rows for 10 years); see [Demand Data](../user-guide/demand-data.md#time-alignment-and-multi-year-demand).

### Availability Files

Create `solar_availability.csv` with 8,760 rows, 1 column, no header, values between 0.0 and 1.0. Each value represents the fraction of rated capacity available at that hour.

```csv
0.0
0.0
0.0
0.0
0.0
0.05
0.25
0.55
0.75
0.85
0.90
0.88
0.85
0.80
0.65
0.40
0.15
0.02
0.0
0.0
0.0
0.0
0.0
0.0
```

This example shows a typical tropical solar day: zero output at night, ramping from sunrise (~06:00) to noon peak (~0.90), then declining to zero by ~18:00. The full file continues this pattern for all 365 days, with seasonal and weather variations.

Create `wind_availability.csv` following the same format. Wind profiles are typically less predictable than solar, with values varying between 0.0 and 1.0 throughout the day and season.

---

## Step 3: Run the Simulation

```bash
# Validate the configuration first (catches errors before the long solve)
esfex validate -c island_system.yaml

# Run the 10-year planning study
esfex run -c island_system.yaml --years 10 --verbose
```

The simulation proceeds in two stages:

1. **Master Problem**: Solves the investment and retirement decisions for all years at once, using representative days
2. **Operational Dispatch**: Solves the detailed hourly dispatch of each year with the rolling horizon, with that year's investments fixed

Expect the full run to take 15-60 minutes depending on your hardware and solver configuration.

---

## Step 4: Interpret Results

The run writes one HDF5 file to `results/`, named `island_system_<ddmmyyyy>_<HHMM>.h5`. Year groups are labelled with calendar years starting at 2025 (`detailed_results/year_2025_threshold_0`, ..., `year_2034_threshold_0`). The snippets below open the newest file:

```python
from pathlib import Path

import h5py
import numpy as np

h5_path = max(Path("results").glob("island_system_*.h5"), key=lambda p: p.stat().st_mtime)
years = range(2025, 2035)
```

### Investment Decisions

The master problem's investments of each year are stored as attributes of that year's group. The keys are `investment_tech_investment_power_<t>_<n>` (MW of technology `t`), `investment_bat_tech_investment_power_<b>_<n>` (MW) and `investment_bat_tech_investment_capacity_<b>_<n>` (MWh), where `<t>`/`<b>` is the 0-based position in `technologies`/`battery_technologies` and `<n>` the node:

```python
techs = ["Solar PV", "Wind Turbines"]          # order of `technologies`

with h5py.File(h5_path, "r") as f:
    for yr in years:
        grp = f[f"detailed_results/year_{yr}_threshold_0"]
        inv = {k.removeprefix("investment_"): v
               for k, v in grp.attrs.items() if k.startswith("investment_")}
        if inv:
            print(yr, {k: round(float(v), 1) for k, v in inv.items()})
```

Built capacity appears in the dispatch as virtual units named `Investment <technology name>` (here `Investment Solar PV`, `Investment Wind Turbines` and `Investment Li-Ion Battery`).

Expected pattern:

- **Early years (1-3)**: Solar PV investment dominates because it has the lowest investment cost ($750,000/MW vs $1,200,000/MW for wind). Small battery additions begin to manage solar intermittency.
- **Mid years (4-7)**: Wind investment starts for temporal diversity (wind generates when the sun does not). Battery capacity grows to 4-6 hour duration as RE penetration rises above 50%.
- **Late years (8-10)**: The diesel generator (initial age 15, lifetime 30) is still in service, but derated by 1%/year of age; the master problem may also invest to replace lost capacity.

### Generation Mix

Each unit's hourly output is a `[nodes x hours]` dataset under `generation/`:

```python
with h5py.File(h5_path, "r") as f:
    for yr in years:
        gen = f[f"detailed_results/year_{yr}_threshold_0/generation"]
        totals = {name: ds[:].sum() / 1000 for name, ds in gen.items()}  # GWh
        print(yr, ", ".join(f"{n}={v:.0f} GWh" for n, v in totals.items()))
```

A typical progression might look like (illustrative):

| Year | Solar (GWh) | Wind (GWh) | Diesel (GWh) | Battery (GWh) | RE Share |
|------|------------|-----------|-------------|--------------|---------|
| 1 | 85 | 0 | 1,300 | 0 | 6% |
| 3 | 350 | 120 | 1,000 | 45 | 32% |
| 5 | 550 | 280 | 680 | 95 | 55% |
| 7 | 700 | 400 | 440 | 130 | 72% |
| 10 | 850 | 500 | 250 | 170 | 84% |

### RE Penetration Trajectory

```python
with h5py.File(h5_path, "r") as f:
    summary = f["summary_results"]
    for yr, pen in zip(summary["year"][:], summary["renewable_penetration"][:]):
        print(f"{yr}: RE penetration = {pen:.1%}")
```

`summary_results` holds one value per year for `year`, `threshold`, `feasible`, `total_cost`, `renewable_penetration`, `co2_emissions`, `loss_of_load` and `n1_security_cost`.

The RE penetration should rise from the initial value (~6% from the existing 50 MW of solar) toward the 80% target. The master problem builds a target path to the final-year value, bounded by `MIN_ANNUAL_INCREMENT` and `MAX_ANNUAL_INCREMENT` if set (see [Master Problem](../user-guide/master-problem.md)).

### LCOE Analysis

`lcoe/<unit>` holds an hourly LCOE (`[nodes x hours]`, $/MWh). A generation-weighted average per unit:

```python
with h5py.File(h5_path, "r") as f:
    grp = f["detailed_results/year_2034_threshold_0"]
    for name in grp["lcoe"]:
        lcoe = grp[f"lcoe/{name}"][:]
        gen = grp[f"generation/{name}"][:]
        if gen.sum() > 0:
            print(f"{name}: ${(lcoe * gen).sum() / gen.sum():.1f}/MWh")
```

Expected LCOE ranges for this scenario (illustrative):

| Technology | LCOE ($/MWh) | Notes |
|-----------|-------------|-------|
| Solar PV | 35-55 | Low fuel cost, moderate investment |
| Wind | 50-75 | Higher investment, good capacity factor |
| Diesel | 180-250 | Dominated by fuel cost at $85/MWh |
| Li-Ion Battery | 120-180 | Enables RE integration, reduces curtailment |

The system LCOE (blended cost) should decline over the planning horizon as renewable energy displaces diesel.

### Cost Breakdown

`summary_results/total_cost` is the objective of each year's operational dispatch (fuel, O&M and penalty costs). Investment costs are decided in the master problem and are not part of it.

```python
with h5py.File(h5_path, "r") as f:
    costs = f["summary_results/total_cost"][:]
    npv = sum(c / 1.08 ** i for i, c in enumerate(costs))
    print(f"NPV of operating cost (10 years): ${npv:,.0f}")
    print(f"Year 1 cost: ${costs[0]:,.0f}")
    print(f"Year 10 cost: ${costs[-1]:,.0f}")
```

### Alternative: Full Python API Workflow

```python
from pathlib import Path

from esfex import load_config
from esfex.runner import Orchestrator

# Load, run, and analyze - no CLI needed
config_file = Path("island_system.yaml")
config = load_config(config_file)
orchestrator = Orchestrator(config, config_path=config_file)
results = orchestrator.run(years=10, start_year=2025)

# Print year-by-year summary
for yr in results:
    print(f"Year {yr.year}: "
          f"Cost=${yr.objective:,.0f}  "
          f"RE={yr.re_penetration:.1%}  "
          f"CO2={yr.emissions:,.0f}t  "
          f"Load shed={yr.load_shed:.1f} MWh")

# Plot generation stack for the final year
import matplotlib.pyplot as plt
import numpy as np

yr = results[-1]
hours = np.arange(168)  # First week
fig, ax = plt.subplots(figsize=(14, 5))
# gen_output is [unit x node x hour]: existing generators first,
# then the virtual "Investment ..." units in the order they were built
ax.stackplot(hours, *[yr.gen_output[g, 0, :168] for g in range(yr.gen_output.shape[0])])
if yr.bat_discharge is not None:
    ax.stackplot(hours, yr.bat_discharge.sum(axis=0)[0, :168],
                 labels=["Battery"], colors=["#2ecc71"], alpha=0.7)
ax.plot(hours, yr.demand[:168, 0], "k-", linewidth=1.5, label="Demand")  # demand is [hour x node]
ax.set_xlabel("Hour")
ax.set_ylabel("Power (MW)")
ax.set_title(f"Generation Mix - Year {yr.year} (First Week)")
ax.legend(loc="upper right")
plt.tight_layout()
plt.savefig("island_generation_week.png", dpi=150)

# Investment decisions of each year (same keys as the HDF5 attributes)
print("\nInvestment Decisions:")
for yr in results:
    if yr.investments:
        print(f"  Year {yr.year}: " + ", ".join(f"{k}: {v:.1f}" for k, v in yr.investments.items()))
```

`Orchestrator` expects `config_path` as a `Path`, not a string.

---

## Step 5: Results Analysis Deep Dive

### Curtailment Analysis

The 5% curtailment cap is a hard limit on each representative day of the master problem, so the optimizer invests in storage rather than overbuilding renewables. The operational dispatch does not enforce the cap; it charges `curtailment_cost` per MWh instead, so the yearly ratio below can be somewhat above 5%.

```python
with h5py.File(h5_path, "r") as f:
    for yr in years:
        grp = f[f"detailed_results/year_{yr}_threshold_0"]
        curt = grp["curtailment"][:].sum()          # [nodes x hours], all generators
        re_gen = sum(ds[:].sum() for ds in grp["generation"].values()
                     if ds.attrs.get("fuel") in ("Solar", "Wind"))
        ratio = curt / re_gen if re_gen > 0 else 0
        print(f"{yr}: Curtailment = {curt:.0f} MWh ({ratio:.1%} of RE generation)")
```

### CO2 Emissions Tracking

```python
with h5py.File(h5_path, "r") as f:
    summary = f["summary_results"]
    budget = 500_000  # tonnes/year
    for yr, em in zip(summary["year"][:], summary["co2_emissions"][:]):
        status = "WITHIN" if em <= budget else "EXCEEDED"
        print(f"{yr}: {em:,.0f} tCO2 ({status} budget)")
```

Emissions should decline as diesel generation is displaced. The CO2 budget is a soft limit of the operational dispatch: each rolling-horizon window gets its share of `annual_budget`, and emissions above it cost `co2_budget_violation` ($/t, default 500). The master problem does not see the budget; it sees the CO2 price (`penalties.co2_cost`).

### Battery Utilization

```python
with h5py.File(h5_path, "r") as f:
    for yr in [2025, 2029, 2034]:
        grp = f[f"detailed_results/year_{yr}_threshold_0"]
        if "battery_charge" not in grp:
            continue
        charge = sum(ds[:].sum() for ds in grp["battery_charge"].values())
        discharge = sum(ds[:].sum() for ds in grp["battery_discharge"].values())
        cycles = discharge / 800  # Approximate full cycles (800 MWh capacity)
        print(f"{yr}: Charged={charge:.0f} MWh, "
              f"Discharged={discharge:.0f} MWh, "
              f"~{cycles:.0f} full cycles")
```

See the [HDF5 Output Schema](../reference/hdf5-output-schema.md) for every dataset.

---

## Key Takeaways

1. **Investment timing**: The optimizer spreads investments across years to match the RE target progression and minimize NPV. Front-loading all investment is rarely optimal due to discounting and technology degradation.
2. **Storage role**: Batteries become essential once RE exceeds ~50% to handle intermittency. The 5% curtailment cap in the master problem favours storage investment over overbuilding RE.
3. **Diesel phase-out**: Existing diesel capacity degrades (1% per year of age) and stays in service until its lifetime ends in year 16. By year 10 its effective capacity is about 196 MW, but its utilization falls as renewables grow.
4. **Curtailment limit**: Without the 5% cap, the optimizer might overbuild solar and curtail the excess. The constraint drives storage sizing in the master problem.
5. **Cost trade-offs**: Cheaper solar comes first; wind adds value through temporal diversity (evening and nighttime generation). The optimal mix depends on relative availability profiles.
6. **Discount rate sensitivity**: At 8%, the optimizer slightly delays investments. Lower discount rates (3-5%) favor earlier, larger RE investments. See the [Sensitivity Analysis Tutorial](sensitivity-analysis.md) for systematic exploration.

---

## Next Steps

- [Multi-Node Tutorial](multi-node.md) — add transmission constraints and spatial optimization
- [EV Integration](ev-integration.md) — add electric vehicles with V2G capability
- [Primary Energy](primary-energy.md) — model the diesel supply chain
- [Sensitivity Analysis](sensitivity-analysis.md) — quantify the impact of uncertain parameters
