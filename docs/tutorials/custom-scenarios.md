# Custom Scenarios

Custom scenarios are separate simulation runs with different configurations, compared after the fact. This approach supports policy analysis, sensitivity exploration, and stakeholder engagement.

## Custom Scenarios vs. Stochastic Scenarios

| Feature | Custom Scenarios | Stochastic Scenarios |
|---------|-----------------|---------------------|
| **How they run** | Separate simulations, independent | Single optimization, simultaneous |
| **Investment decisions** | Different per scenario | Shared across all scenarios |
| **Use case** | "What if X?" policy questions | "How to hedge?" under uncertainty |
| **Computation** | Linear: N scenarios = N solves | Combined: N scenarios in 1 large solve |
| **Comparison** | Post-hoc (after all runs complete) | Built-in (expected cost weighting) |

Use **custom scenarios** for fundamentally different policy paths ("What if 100% RE?" vs. "What if diesel continues?"). Use **stochastic scenarios** for a single robust plan that hedges against uncertainty.

---

## Demand Growth Scenarios

There is no demand-growth key: a system's demand comes only from its demand file. A single-year file gives the same demand every year, in the master problem and in the dispatch. To model growth, write every simulated year into the file (see [Demand Data](../user-guide/demand-data.md)).

### Multi-Year Demand File

```yaml
    demand_path: demand_25years.csv   # 219,000 rows (25 x 8,760), one column per node
```

Each year's 8,760 hours follow each other in the file. If the file has fewer than `years x 8,760` rows, only the first 8,760 rows are used and repeated for every year. A multi-year file allows:

- **Uniform or non-uniform growth** (rapid early growth, saturation later)
- **Evening peak growth** (hours 18-22 increase faster)
- **Industrial shifts** (weekend demand changes as industry electrifies)
- **Seasonal pattern changes** (more cooling demand from climate change)

With a constant rate, year y demand is D_y = D_base x (1 + growth_rate)^(y-1):

| Year | Growth 1% | Growth 3% | Growth 5% |
|------|-----------|-----------|-----------|
| 1 | 100 MW | 100 MW | 100 MW |
| 5 | 104 MW | 113 MW | 122 MW |
| 10 | 109 MW | 130 MW | 155 MW |
| 15 | 115 MW | 151 MW | 198 MW |
| 25 | 127 MW | 203 MW | 339 MW |

### Creating a Multi-Year Demand File

Demand CSV files have no header row and one column per node:

```python
import numpy as np

# Single-year hourly demand: 8,760 rows x one column per node
base_demand = np.loadtxt("demand_base.csv", delimiter=",", ndmin=2)

def write_demand(path, growth_rates):
    """Write one 8,760-row block per year, compounding the yearly rates."""
    blocks, multiplier = [], 1.0
    for y, rate in enumerate(growth_rates):
        if y > 0:
            multiplier *= 1 + rate
        blocks.append(base_demand * multiplier)
    np.savetxt(path, np.vstack(blocks), delimiter=",", fmt="%.3f")
    print(f"Created {path}: {len(growth_rates) * 8760} rows")

years = 25
write_demand("demand_1pct.csv", [0.01] * years)
write_demand("demand_2pct.csv", [0.02] * years)
write_demand("demand_3pct.csv", [0.03] * years)
write_demand("demand_4pct.csv", [0.04] * years)
write_demand("demand_declining.csv", np.linspace(0.04, 0.01, years))  # 4 % early, 1 % late
```

---

## Technology Cost Projections

### Static Costs

```yaml
    technologies:
      solar_pv:
        name: Solar PV
        type: Renewable
        fuel: Sun
        invest_cost: [700000, 700000, 700000]     # $/MW (constant)
        invest_max_power: [500, 300, 200]
```

### Declining Cost Scenarios

To model learning curves, create separate configuration files for different cost trajectories:

**scenario_base_costs.yaml** — current costs, no decline:
```yaml
    technologies:
      solar_pv:
        invest_cost: [700000, 700000, 700000]
      wind:
        invest_cost: [1200000, 1200000, 1200000]
    battery_technologies:
      li_ion:
        invest_cost_power: [180000, 180000, 180000]
        invest_cost_energy: [120000, 120000, 120000]
```

**scenario_optimistic_costs.yaml** — aggressive cost reduction:
```yaml
    technologies:
      solar_pv:
        invest_cost: [500000, 500000, 500000]       # 30% cheaper
      wind:
        invest_cost: [900000, 900000, 900000]        # 25% cheaper
    battery_technologies:
      li_ion:
        invest_cost_power: [100000, 100000, 100000]  # 44% cheaper
        invest_cost_energy: [70000, 70000, 70000]    # 42% cheaper
```

Alternatively, use the stochastic framework with the `invest_cost_renewables` and `invest_cost_storage` multipliers to cover both cost paths in a single run (see the [Stochastic Planning tutorial](stochastic.md)).

---

## RE Penetration Targets

### Progressive Targets

```yaml
    TARGET_RE_PENETRATION: 0.80       # 80 % in the final year
    MIN_ANNUAL_INCREMENT: 0.02        # yearly step of the target path: at least 2 %
    MAX_ANNUAL_INCREMENT: 0.10        # and at most 10 %
```

The starting RE share is computed from the existing renewable generators and the first year of demand; there is no key for it. In `perfect_foresight` planning the master problem builds a linear path from that share to `TARGET_RE_PENETRATION` in the last year, with each yearly step clamped to `[MIN_ANNUAL_INCREMENT, MAX_ANNUAL_INCREMENT]`. For a 25-year run starting at 15 % RE (step 2.7 %/year):

| Year | Target RE |
|------|-----------|
| 1 | 15% |
| 5 | ~26% |
| 10 | ~39% |
| 15 | ~53% |
| 20 | ~66% |
| 25 | 80% |

`MIN_ANNUAL_INCREMENT` forces steady progress; `MAX_ANNUAL_INCREMENT` prevents unrealistic construction rates. Because of the clamp, the path can end below or above the target. Shortfalls are paid at `penalties.fre_penetration_loss` per MWh. See [Master Problem](../user-guide/master-problem.md#re-penetration-trajectory) for the details and for `myopic` planning.

### Comparing RE Target Levels

Create scenarios with different ambition levels:

```yaml
# scenario_conservative.yaml
TARGET_RE_PENETRATION: 0.40       # 40% RE

# scenario_moderate.yaml
TARGET_RE_PENETRATION: 0.60       # 60% RE

# scenario_ambitious.yaml
TARGET_RE_PENETRATION: 0.80       # 80% RE

# scenario_100re.yaml
TARGET_RE_PENETRATION: 1.00       # 100% RE (most expensive)
```

---

## CO2 Budget Scenarios

### Constant Budget

```yaml
    co2_budget:
      enabled: true
      annual_budget: 500000.0          # tonnes CO2/year (constant)
```

The budget acts in the operational dispatch only: emissions above `annual_budget` cost `penalties.co2_budget_violation` per tonne. The master problem has no CO2 budget, but it does charge `co2_cost` on generator emissions, so a carbon price steers the investment plan.

### Budget and Carbon Price Scenarios

```yaml
# scenario_mild_climate.yaml
co2_budget:
  annual_budget: 500000.0
penalties:
  co2_cost: 10.0                      # Low carbon price

# scenario_moderate_climate.yaml
co2_budget:
  annual_budget: 300000.0
penalties:
  co2_cost: 50.0                      # Moderate carbon price

# scenario_strict_climate.yaml
co2_budget:
  annual_budget: 100000.0
penalties:
  co2_cost: 150.0                     # High carbon price
```

---

## Economic Parameters

### Discount Rate

The discount rate impacts the trade-off between upfront investment (RE + storage) and ongoing operational costs (fossil fuel):

```yaml
    discount_rate: 0.05               # e.g. 5 % for developed economies,
                                      # 8 % for developing countries,
                                      # 12 % for high-risk environments
```

Higher discount rates favor technologies with lower upfront costs (diesel) over technologies with higher upfront costs but lower lifetime costs (solar + storage).

### Investment Budget Constraints

```yaml
    MAX_ANNUAL_SYSTEM_COST: 500000000.0   # $500M/year cap
```

This caps the master problem's investment cost per investment period (a soft cap: excess spending is allowed through a penalised slack). Lower budgets spread deployments over time and delay the RE transition, but may be more realistic for constrained economies.

---

## Practical Example: Four Scenarios

### Scenario Definitions

| Scenario | RE Target | Demand Growth (file) | Carbon Price | Budget |
|----------|-----------|--------------|-------------|--------|
| BAU (Business as Usual) | 30% | 3% (`demand_3pct.csv`) | $0/tCO2 | $200M/yr |
| Green | 80% | 2% (`demand_2pct.csv`) | $50/tCO2 | $500M/yr |
| Conservative | 50% | 1% (`demand_1pct.csv`) | $10/tCO2 | $300M/yr |
| Aggressive | 100% | 4% (`demand_4pct.csv`) | $100/tCO2 | $800M/yr |

### Creating Scenario Configuration Files

Use a base configuration file with scenario-specific overrides.

**base_system.yaml**:

```yaml
simulation_mode: development
date_start: "01/01/2025 00:00"

temporal:
  resolution_hours: 1
  use_rolling_horizon: true
  rolling_horizon_hours: 48
  overlap_hours: 24

solver:
  name: highs
  threads: 4
  time_limit: 3600

master_problem:
  representative_days: 5

meta_network:
  systems: [island]

systems:
  island:
    name: island
    demand_path: demand_2pct.csv
    demand_scale: 1.0
    TARGET_RE_PENETRATION: 0.60
    discount_rate: 0.05
    MAX_ANNUAL_SYSTEM_COST: 500000000.0

    nodes:
      nodes_connections: [0.0, 100.0, 75.0,
                          100.0, 0.0, 50.0,
                          75.0, 50.0, 0.0]     # line capacities (MW)
      node_coordinates:
        - {latitude: 23.13, longitude: -82.38}
        - {latitude: 22.40, longitude: -81.95}
        - {latitude: 22.07, longitude: -80.45}
      node_names: [Node_A, Node_B, Node_C]

    fuels:
      Diesel: {unit: ton, emission_factor: 0.267, energy_content: 11.86, price_base: 800.0}

    generators:
      diesel:
        name: Diesel
        type: Non-renewable
        fuel: Diesel
        rated_power: [50.0, 30.0, 40.0]
        min_power: [0.3, 0.3, 0.3]
        min_up: [0, 0, 0]
        min_down: [0, 0, 0]
        ramp_up: [1.0, 1.0, 1.0]
        ramp_down: [1.0, 1.0, 1.0]
        eff_at_rated: [0.38, 0.38, 0.38]
        eff_at_min: [0.30, 0.30, 0.30]
        inertia: [2.0, 2.0, 2.0]
        start_up_cost: [0.0, 0.0, 0.0]
        fuel_cost: [85.0, 85.0, 85.0]
        fixed_cost: [5.0, 5.0, 5.0]
        maintenance_cost: [3.0, 3.0, 3.0]
        invest_cost: [0.0, 0.0, 0.0]
        invest_max_power: [0.0, 0.0, 0.0]
        life_time: [30, 30, 30]
        initial_age: [10, 10, 10]
        degradation_rate: [0.0, 0.0, 0.0]
        decommissioning_cost: [0.0, 0.0, 0.0]

    technologies:
      solar_pv:
        name: Solar PV
        type: Renewable
        fuel: Sun
        invest_cost: [700000.0, 700000.0, 700000.0]
        invest_max_power: [500.0, 300.0, 200.0]
        Availability: solar_profile.csv
        eff_at_rated: [1.0, 1.0, 1.0]
        degradation_rate: [0.005, 0.005, 0.005]
        lifetime: 25

      wind:
        name: Wind
        type: Renewable
        fuel: Wind
        invest_cost: [1200000.0, 1200000.0, 1200000.0]
        invest_max_power: [200.0, 100.0, 100.0]
        Availability: wind_profile.csv
        eff_at_rated: [1.0, 1.0, 1.0]
        degradation_rate: [0.002, 0.002, 0.002]
        lifetime: 20

    battery_technologies:
      li_ion:
        name: Li-Ion
        invest_cost_power: [180000.0, 180000.0, 180000.0]
        invest_cost_energy: [120000.0, 120000.0, 120000.0]
        invest_max_power: [200.0, 100.0, 100.0]
        invest_max_capacity: [800.0, 400.0, 400.0]
        min_duration_hours: 2.0
        max_duration_hours: 8.0
        efficiency_charge: [0.95, 0.95, 0.95]
        efficiency_discharge: [0.95, 0.95, 0.95]
        degradation_rate: [0.0, 0.0, 0.0]
        lifetime: 15

    penalties:
      loss_of_load: 10000000.0
      curtailment_cost: 20.0
      max_curtailment_ratio: 0.05
      fre_penetration_loss: 100.0
      co2_cost: 10.0
```

ESFEX does not support YAML inheritance natively. A Python script can generate complete scenario files:

```python
import yaml
import copy
from pathlib import Path

# Load base config
with open("base_system.yaml") as f:
    base = yaml.safe_load(f)

# Define scenario modifications
scenarios = {
    "bau": {
        "systems.island.TARGET_RE_PENETRATION": 0.30,
        "systems.island.demand_path": "demand_3pct.csv",
        "systems.island.penalties.co2_cost": 0.0,
        "systems.island.MAX_ANNUAL_SYSTEM_COST": 200000000.0,
        "systems.island.discount_rate": 0.08,
    },
    "green": {
        "systems.island.TARGET_RE_PENETRATION": 0.80,
        "systems.island.demand_path": "demand_2pct.csv",
        "systems.island.penalties.co2_cost": 50.0,
        "systems.island.MAX_ANNUAL_SYSTEM_COST": 500000000.0,
        "systems.island.discount_rate": 0.05,
    },
    "conservative": {
        "systems.island.TARGET_RE_PENETRATION": 0.50,
        "systems.island.demand_path": "demand_1pct.csv",
        "systems.island.penalties.co2_cost": 10.0,
        "systems.island.MAX_ANNUAL_SYSTEM_COST": 300000000.0,
        "systems.island.discount_rate": 0.06,
    },
    "aggressive": {
        "systems.island.TARGET_RE_PENETRATION": 1.00,
        "systems.island.demand_path": "demand_4pct.csv",
        "systems.island.penalties.co2_cost": 100.0,
        "systems.island.MAX_ANNUAL_SYSTEM_COST": 800000000.0,
        "systems.island.discount_rate": 0.04,
    },
}

def set_nested(d, path, value):
    """Set a nested dict value using dot-separated path."""
    keys = path.split(".")
    for key in keys[:-1]:
        d = d[key]
    d[keys[-1]] = value

for name, mods in scenarios.items():
    config = copy.deepcopy(base)
    for path, value in mods.items():
        set_nested(config, path, value)

    output_path = f"scenario_{name}.yaml"
    with open(output_path, "w") as f:
        yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)
    print(f"Created {output_path}")
```

---

## Batch Execution from CLI

```bash
# Create output directories
mkdir -p results/bau results/green results/conservative results/aggressive

# Run all four scenarios
esfex run -c scenario_bau.yaml -o results/bau/ --years 25 -v
esfex run -c scenario_green.yaml -o results/green/ --years 25 -v
esfex run -c scenario_conservative.yaml -o results/conservative/ --years 25 -v
esfex run -c scenario_aggressive.yaml -o results/aggressive/ --years 25 -v
```

For parallel execution, use background processes:

```bash
esfex run -c scenario_bau.yaml -o results/bau/ --years 25 &
esfex run -c scenario_green.yaml -o results/green/ --years 25 &
esfex run -c scenario_conservative.yaml -o results/conservative/ --years 25 &
esfex run -c scenario_aggressive.yaml -o results/aggressive/ --years 25 &
wait
echo "All scenarios complete"
```

---

## Result Comparison

Each run writes `<output>/<config_stem>_<ddmmyyyy>_<HHMM>.h5`. `summary_results` holds one value per simulated year for `year`, `total_cost`, `renewable_penetration`, `co2_emissions` (t), `loss_of_load` (MWh) and a few more. The master problem's new capacity for each year is stored as attributes of `detailed_results/year_<YYYY>_threshold_0`, named `investment_tech_investment_power_<t>_<n>` (MW of technology `t` at node `n`) and `investment_bat_tech_investment_power_<bt>_<n>` (MW of battery technology `bt`); see the [HDF5 output schema](../reference/hdf5-output-schema.md).

```python
import glob
import h5py
import numpy as np

def newest(folder):
    return sorted(glob.glob(f"{folder}/*.h5"))[-1]

scenarios = {
    "BAU": newest("results/bau"),
    "Green": newest("results/green"),
    "Conservative": newest("results/conservative"),
    "Aggressive": newest("results/aggressive"),
}

def new_capacity(f, prefix):
    """MW per year from the master-problem investment attributes."""
    out = []
    for name in sorted(f["detailed_results"]):
        attrs = f[f"detailed_results/{name}"].attrs
        out.append(sum(float(v) for k, v in attrs.items() if k.startswith(prefix)))
    return np.array(out)

print(f"{'Scenario':<15} {'Total cost ($M)':>15} {'Final RE (%)':>12} "
      f"{'New RE+bat (MW)':>16} {'Emissions (ktCO2)':>18}")
print("-" * 80)

for name, path in scenarios.items():
    with h5py.File(path, "r") as f:
        summary = f["summary_results"]
        total_cost = summary["total_cost"][:].sum() / 1e6
        re_pen = summary["renewable_penetration"][-1] * 100
        new_mw = (new_capacity(f, "investment_tech_investment_power_").sum()
                  + new_capacity(f, "investment_bat_tech_investment_power_").sum())
        emissions = summary["co2_emissions"][:].sum() / 1e3

        print(f"{name:<15} {total_cost:>15,.1f} {re_pen:>12.1f} "
              f"{new_mw:>16,.1f} {emissions:>18,.1f}")
```

### Example Output

Illustrative numbers:

```
Scenario        Total cost ($M) Final RE (%)  New RE+bat (MW)  Emissions (ktCO2)
--------------------------------------------------------------------------------
BAU                       285.3         28.5             45.2           12,500.3
Green                     312.7         78.2            185.6            4,200.1
Conservative              268.4         48.7             98.3            8,100.5
Aggressive                425.8         97.1            310.2            1,050.8
```

### Year-by-Year Comparison

```python
import matplotlib.pyplot as plt

fig, axes = plt.subplots(2, 2, figsize=(14, 10))

for name, path in scenarios.items():
    with h5py.File(path, "r") as f:
        summary = f["summary_results"]
        years = summary["year"][:]

        axes[0, 0].plot(years, summary["total_cost"][:] / 1e6, label=name)
        axes[0, 1].plot(years, summary["renewable_penetration"][:] * 100, label=name)
        cum_mw = np.cumsum(new_capacity(f, "investment_tech_investment_power_"))
        axes[1, 0].plot(years, cum_mw, label=name)
        axes[1, 1].plot(years, summary["co2_emissions"][:] / 1e3, label=name)

axes[0, 0].set_title("Annual System Cost")
axes[0, 0].set_ylabel("Cost ($M)")
axes[0, 1].set_title("RE Penetration")
axes[0, 1].set_ylabel("RE (%)")
axes[1, 0].set_title("Cumulative New Technology Capacity")
axes[1, 0].set_ylabel("Capacity (MW)")
axes[1, 1].set_title("Annual CO2 Emissions")
axes[1, 1].set_ylabel("Emissions (ktCO2)")

for ax in axes.flat:
    ax.set_xlabel("Year")
    ax.legend()
    ax.grid(True, alpha=0.3)

plt.tight_layout()
plt.savefig("scenario_comparison.png", dpi=150)
plt.show()
```

---

## Organizing Scenario Files and Results

```
project/
  base_system.yaml              # Common configuration
  data/
    demand_1pct.csv ...         # Multi-year demand files
    solar_profile.csv           # Solar availability
    wind_profile.csv            # Wind availability
  scenarios/
    generate_scenarios.py       # Script to generate scenario files
    scenario_bau.yaml
    scenario_green.yaml
    scenario_conservative.yaml
    scenario_aggressive.yaml
  results/
    bau/
      scenario_bau_<ddmmyyyy>_<HHMM>.h5
    green/
      scenario_green_<ddmmyyyy>_<HHMM>.h5
    conservative/
      scenario_conservative_<ddmmyyyy>_<HHMM>.h5
    aggressive/
      scenario_aggressive_<ddmmyyyy>_<HHMM>.h5
  analysis/
    compare_scenarios.py        # Comparison script
    scenario_comparison.png     # Output charts
    summary_table.csv           # Summary metrics
```

Relative data paths in a configuration (`demand_path`, `Availability`) are resolved against the configuration file's folder; demand and technology profiles also fall back to the current directory. Write generated scenario files next to the base configuration, or use absolute paths.

### Generating Summary CSV

```python
import csv

rows = []
for name, path in scenarios.items():
    with h5py.File(path, "r") as f:
        summary = f["summary_results"]
        rows.append({
            "scenario": name,
            "total_cost_musd": summary["total_cost"][:].sum() / 1e6,
            "final_re_pct": summary["renewable_penetration"][-1] * 100,
            "new_tech_mw": new_capacity(f, "investment_tech_investment_power_").sum(),
            "new_battery_mw": new_capacity(f, "investment_bat_tech_investment_power_").sum(),
            "total_emissions_ktco2": summary["co2_emissions"][:].sum() / 1e3,
            "total_load_shed_mwh": summary["loss_of_load"][:].sum(),
        })

with open("analysis/summary_table.csv", "w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
```

---

## Advanced: Parameterized Scenarios with Environment Variables

```python
import os
import yaml
import subprocess
import copy

# Load base config
with open("base_system.yaml") as f:
    base_config = yaml.safe_load(f)

# Read parameters from environment or defaults
re_target = float(os.environ.get("ESFEX_RE_TARGET", "0.60"))
demand_file = os.environ.get("ESFEX_DEMAND_FILE", "demand_2pct.csv")
carbon_price = float(os.environ.get("ESFEX_CARBON_PRICE", "10.0"))
output_dir = os.environ.get("ESFEX_OUTPUT_DIR", "results/default")

# Modify config
config = copy.deepcopy(base_config)
config["systems"]["island"]["TARGET_RE_PENETRATION"] = re_target
config["systems"]["island"]["demand_path"] = demand_file
config["systems"]["island"]["penalties"]["co2_cost"] = carbon_price

# Write the temporary config next to the base config so relative data paths resolve
tmp_config = f"esfex_config_{os.getpid()}.yaml"
with open(tmp_config, "w") as f:
    yaml.safe_dump(config, f, default_flow_style=False)

# Run simulation
subprocess.run([
    "esfex", "run",
    "-c", tmp_config,
    "-o", output_dir,
    "--years", "25",
    "-v",
], check=True)

# Clean up
os.unlink(tmp_config)
```

Usage:

```bash
ESFEX_RE_TARGET=0.80 ESFEX_CARBON_PRICE=50 ESFEX_OUTPUT_DIR=results/green \
  python run_parameterized.py
```

---

## Key Takeaways

1. **Multiple levers**: Demand growth (via the demand file), RE targets, carbon prices, discount rates, and investment limits all interact to determine the optimal energy mix. No single parameter tells the whole story.
2. **Scenario comparison**: Running multiple configs and comparing results is the simplest way to explore the solution space. It requires no special configuration beyond separate YAML files.
3. **Budget constraints**: Investment caps can significantly delay the RE transition. A $200M/year budget may prevent achieving 80% RE even if it is cost-optimal without the constraint.
4. **Discount rate impact**: Moving from 5% to 12% discount rate can shift the optimal solution from solar-dominated to diesel-dominated, because RE's advantage lies in low lifetime costs that are discounted away at high rates.
5. **Stochastic hedging**: For uncertain parameters, use the stochastic framework (see the [Stochastic Planning tutorial](stochastic.md)) rather than running separate deterministic scenarios. Stochastic programming produces a single robust plan; custom scenarios produce multiple incompatible plans.
6. **Automation**: Use Python scripts to generate scenario files and batch scripts to run them in parallel. This is essential for studies with more than 4-5 scenarios.
7. **Structured comparison**: Always produce a summary table and year-by-year charts. Decision-makers need both aggregate metrics (total cost, final RE%) and trajectory information (how costs and RE evolve over time).
