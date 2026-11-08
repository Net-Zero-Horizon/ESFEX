# Stochastic Planning

## Two-Stage Stochastic Programming

Energy infrastructure investments last 15-30 years. Over that horizon, fuel prices, demand growth, technology costs, and climate conditions are all uncertain. A deterministic plan optimized for a single "best guess" future may perform poorly if conditions differ from expectations [**[9]**](../reference/bibliography.md#ref9).

**Two-stage stochastic programming** [**[10]**](../reference/bibliography.md#ref10) addresses this by considering multiple plausible futures simultaneously:

- **First stage (here-and-now)**: Investment decisions — which generators, batteries, and transmission lines to build — are made once and must be the same across all scenarios. These represent irreversible infrastructure commitments.
- **Second stage (wait-and-see)**: Operational dispatch decisions — how to run generators and charge/discharge batteries — can adapt to whatever future actually occurs.

The optimizer minimizes **expected cost** across all scenarios, weighted by probability. The result is a robust investment plan that hedges against multiple futures.

---

## Scenario Design

Scenarios should represent distinct, plausible futures that span the range of uncertainty.

### Axis-Based Scenarios

Choose 2-3 key uncertainties and create scenarios at their extremes:

| Scenario | Demand Growth | Fuel Price | RE Cost |
|----------|--------------|------------|---------|
| High Demand, Expensive Fuel | High | High | Base |
| Base Case | Base | Base | Base |
| Green Transition | Base | Base | Low |
| Stagnation | Low | Low | Base |

### Probability Assignment

Probabilities reflect the decision-maker's belief about each future:

| Scenario | Probability | Rationale |
|----------|------------|-----------|
| High Demand | 30% | Strong economic growth, urbanization |
| Base Case | 40% | Most likely trajectory |
| Green Transition | 20% | Technology breakthroughs, strong policy |
| Stagnation | 10% | Economic downturn, delayed investment |

Probabilities must sum to 1.0.

---

## Configuration

### Step 1: Enable Stochastic Mode

```yaml
master_problem:
  stochastic: true
  solver_method: monolithic   # Benders ignores the scenarios
  representative_days: 5
  min_day_separation: 7
```

With `stochastic: true` the master problem shares one set of investment variables across all scenarios and builds a separate set of representative-day operational variables for each scenario.

### Step 2: Define Scenarios

`stochastic_scenarios` is a list. Each scenario has a `name`, a `probability` (they must sum to 1) and optional `multipliers`; a multiplier left out is 1.0:

```yaml
systems:
  island:
    # ... standard system config (nodes, generators, technologies, ...) ...

    stochastic_scenarios:
      - name: high_demand
        probability: 0.3
        multipliers:
          demand_growth: 1.03        # demand +3 %/year relative to the base case
          fuel_cost: 1.1             # 10 % more expensive fuel

      - name: base_case
        probability: 0.4

      - name: green_transition
        probability: 0.2
        multipliers:
          invest_cost_renewables: 0.8   # 20 % cheaper RE investment
          invest_cost_storage: 0.7      # 30 % cheaper batteries

      - name: stagnation
        probability: 0.1
        multipliers:
          demand_growth: 0.98        # demand −2 %/year
          fuel_cost: 0.8
```

### Available Multipliers

Each multiplier scales the corresponding base-case parameter (1.0 = no change). Any other key is ignored with a warning in the log; keys written by earlier Studio versions (such as `gen_fuel_cost` or `penalty_co2_cost`) are translated, see [Stochastic Programming](../formulation/stochastic-programming.md#22-scenario-multipliers).

| Multiplier | Default | What It Scales |
|-----------|---------|----------------|
| `demand_growth` | 1.0 | Demand, as an annual factor: base demand × `demand_growth`^(year − 1) |
| `fuel_cost` | 1.0 | Fuel cost of generators and candidate technologies |
| `fuel_price_growth` | 1.0 | Extra annual fuel price escalation: fuel cost × `fuel_price_growth`^(year − 1) |
| `gen_fixed_cost` | 1.0 | Fixed O&M ($/MW·yr) of generators, technologies, batteries and battery technologies |
| `maintenance_cost` | 1.0 | Maintenance cost of generators, batteries and technologies |
| `carbon_price` | 1.0 | CO2 cost (`co2_cost`) in the scenario's operation |
| `penalty_loss_of_load` | 1.0 | Loss-of-load penalty (VOLL), sectoral penalties included |
| `penalty_loss_of_reserve` | 1.0 | Static and dynamic reserve shortfall penalties |
| `penalty_curtailment` | 1.0 | Curtailment cost |
| `penalty_fre_penetration` | 1.0 | RE-penetration shortfall penalty |
| `penalty_co2_budget_violation` | 1.0 | Penalty per tonne over each system's CO2 budget |
| `bat_maintenance_cost` | 1.0 | Maintenance of batteries and battery technologies, on top of `maintenance_cost` |
| `gen_start_up_cost` | 1.0 | Generator start-up cost (only when the dispatch uses unit commitment, `use_uc_in_dispatch`) |
| `converter_variable_cost` | 1.0 | Variable cost of AC/DC and frequency converters |
| `transmission_cost_per_mw_km` | 1.0 | Flow cost of inter-system links (per MWh and km) |
| `penalty_loss_of_fuel_supply` | 1.0 | Fuel shortfall and excess-supply penalty (with primary energy) |
| `fuel_import_cost` | 1.0 | Per-node fuel import cost (with primary energy) |
| `fuel_transport_cost` | 1.0 | Route transport cost and route investment (with primary energy) |
| `fuel_storage_invest_cost` | 1.0 | Fuel storage investment (with primary energy) |
| `electrolyzer_invest_cost` | 1.0 | Electrolyzer investment (with primary energy) |
| `electrolyzer_fixed_cost` | 1.0 | Electrolyzer fixed cost (with primary energy) |
| `electrolyzer_variable_cost` | 1.0 | Electrolyzer variable cost (with primary energy) |
| `electrolyzer_water_cost` | 1.0 | Electrolyzer water cost (with primary energy) |
| `invest_cost_renewables` | 1.0 | Investment cost of renewable technologies ($/MW) |
| `invest_cost_conventional` | 1.0 | Investment cost of non-renewable technologies ($/MW) |
| `invest_cost_storage` | 1.0 | Battery technology investment cost ($/MW and $/MWh) |
| `bat_invest_cost_power` | 1.0 | Battery technology power cost ($/MW), on top of `invest_cost_storage` |
| `bat_invest_cost_capacity` | 1.0 | Battery technology energy cost ($/MWh), on top of `invest_cost_storage` |
| `invest_cost_transmission` | 1.0 | Transmission investment cost ($/MW) |
| `converter_invest_cost` | 1.0 | Investment cost of AC/DC and frequency converters ($/MW) |
| `gen_decommissioning_cost` | 1.0 | Decommissioning cost of existing generators |
| `bat_decommissioning_cost` | 1.0 | Decommissioning cost of existing batteries |
| `discount_rate` | 1.0 | Discount rate used for the scenario's costs |

There is no multiplier for renewable availability or EV adoption.

### How Multipliers Work

```
parameter_s = parameter_base * multiplier_s
```

For example, with a base fuel cost of $85/MWh and `fuel_cost: 1.3`, the fuel cost in that scenario's operation is $85 × 1.3 = $110.5/MWh.

`demand_growth` is a factor per year, compounding from the first year: with 1.03, the scenario's demand is 3 % above the base case in year 2, 6.1 % in year 3 and 30.5 % in year 10. Each scenario's operation has to serve its own demand with the common plan (or shed it at the `loss_of_load` cost), so a high-demand scenario pushes the plan to build more.

---

## Complete YAML Example

```yaml
simulation_mode: development
date_start: "01/01/2025 00:00"

temporal:
  use_rolling_horizon: true
  rolling_horizon_hours: 48
  overlap_hours: 24

solver:
  name: highs
  threads: 4
  time_limit: 7200            # allow more time for the stochastic solve
  gap: 0.01

master_problem:
  stochastic: true
  solver_method: monolithic
  representative_days: 5
  min_day_separation: 7

meta_network:
  systems: [island]

systems:
  island:
    name: island
    demand_path: demand.csv
    TARGET_RE_PENETRATION: 0.8

    nodes:
      nodes_connections: [0.0]
      node_names: [Main]

    generators:
      diesel:
        name: Diesel
        type: Non-renewable
        fuel: Diesel
        rated_power: [60.0]
        life_time: [30]
        initial_age: [10]
        degradation_rate: [0.0]
        decommissioning_cost: [0.0]
        min_power: [0.0]
        min_up: [0]
        min_down: [0]
        ramp_up: [1.0]
        ramp_down: [1.0]
        eff_at_rated: [0.35]
        eff_at_min: [0.25]
        inertia: [2.0]
        start_up_cost: [0.0]
        fuel_cost: [85.0]
        fixed_cost: [0.0]
        maintenance_cost: [10.0]
        invest_cost: [1000000.0]

    technologies:
      solar_pv:
        name: Solar PV
        type: Renewable
        fuel: Sun
        Availability: solar.csv
        invest_cost: [700000.0]
        invest_max_power: [200.0]
        eff_at_rated: [1.0]
        degradation_rate: [0.005]
        lifetime: 25

    battery_technologies:
      li_ion:
        name: Li-Ion
        invest_cost_power: [180000.0]
        invest_cost_energy: [120000.0]
        invest_max_power: [100.0]
        invest_max_capacity: [400.0]
        efficiency_charge: [0.95]
        efficiency_discharge: [0.95]
        degradation_rate: [0.0]
        lifetime: 15

    penalties:
      loss_of_load: 10000.0
      max_curtailment_ratio: 0.05

    stochastic_scenarios:
      - name: high_demand
        probability: 0.3
        multipliers:
          demand_growth: 1.03
          fuel_cost: 1.1
      - name: base_case
        probability: 0.5
      - name: green_transition
        probability: 0.2
        multipliers:
          invest_cost_renewables: 0.8
          invest_cost_storage: 0.7
```

---

## Running the Stochastic Optimization

```bash
esfex run -c stochastic_config.yaml --years 10 -v
```

The log confirms the scenarios before the master problem is solved:

```
  Stochastic mode: 3 scenarios — high_demand (p=0.30), base_case (p=0.50), green_transition (p=0.20)
Stochastic MasterProblem built with 3 scenarios
MasterProblem termination: OPTIMAL, primal: FEASIBLE_POINT
```

The master problem has one block of representative-day operation per scenario, so it grows roughly in proportion to the number of scenarios. The yearly dispatch that follows the master problem runs on the base case.

---

## Interpreting Results

### Robust Investment Decisions

A single set of investment decisions applies across all scenarios. The run writes `results/stochastic_config_<ddmmyyyy>_<HHMM>.h5`; each year's investments are attributes of its group (`investment_tech_investment_power_<t>_<n>`, `investment_bat_tech_investment_power_<b>_<n>`, `investment_bat_tech_investment_capacity_<b>_<n>`, with `<t>`/`<b>` the 0-based position in `technologies`/`battery_technologies` and `<n>` the node):

```python
from pathlib import Path

import h5py

h5_path = max(Path("results").glob("stochastic_config_*.h5"), key=lambda p: p.stat().st_mtime)

with h5py.File(h5_path, "r") as f:
    for name, grp in f["detailed_results"].items():
        inv = {k.removeprefix("investment_"): round(float(v), 1)
               for k, v in grp.attrs.items() if k.startswith("investment_")}
        if inv:
            print(name, inv)
```

### Per-Scenario Operational Outcomes

The results file holds the common investment plan and the base-case dispatch. Per-scenario operational results are not exported, and there is no option to re-run a scenario with a fixed plan. To see how the plan performs in a given scenario, add the built capacity as existing `generators`/`batteries` in a copy of the scenario's configuration and run it with `simulation_mode: economic_dispatch`, or compare against the deterministic runs used for EVPI below.

### EVPI: Expected Value of Perfect Information

EVPI measures the value of knowing which scenario will occur:

```
EVPI = E[cost_stochastic] - E[cost_with_perfect_info]
```

Compute EVPI by running the deterministic optimization per scenario, weighted by probabilities:

```bash
# Stochastic run, then a deterministic run per scenario
# (scenario files: stochastic: false, base parameters scaled by hand)
esfex run -c stochastic_config.yaml -o results/stochastic/
esfex run -c scenario_high_demand.yaml -o results/det_high/
esfex run -c scenario_base_case.yaml -o results/det_base/
esfex run -c scenario_green.yaml -o results/det_green/
```

```python
from pathlib import Path

import h5py

def newest(folder):
    return max(Path(folder).glob("*.h5"), key=lambda p: p.stat().st_mtime)

def total_cost(path):
    with h5py.File(path, "r") as f:
        return f["summary_results/total_cost"][:].sum()

# Deterministic costs under perfect information
det_costs = {
    "high_demand": (newest("results/det_high"), 0.3),
    "base_case": (newest("results/det_base"), 0.5),
    "green_transition": (newest("results/det_green"), 0.2),
}

expected_perfect = 0.0
for name, (path, prob) in det_costs.items():
    cost = total_cost(path)
    expected_perfect += prob * cost
    print(f"  {name}: ${cost:,.0f}")

print(f"\nExpected cost with perfect info: ${expected_perfect:,.0f}")

# Cost of the stochastic plan (see the note below)
stoch_cost = total_cost(newest("results/stochastic"))

evpi = stoch_cost - expected_perfect
print(f"Stochastic plan cost:            ${stoch_cost:,.0f}")
print(f"EVPI:                            ${evpi:,.0f}")
print(f"EVPI as % of stochastic cost:    {evpi/stoch_cost:.1%}")
```

`summary_results/total_cost` holds the yearly operational dispatch cost; investment cost is not included, and the stochastic run's dispatch covers the base case only. For a strict EVPI, add each run's investment cost and evaluate the stochastic plan in every scenario (as described above), then take the probability-weighted cost. The snippet above gives a first approximation.

Small EVPI (< 2-3%): deterministic solution is nearly as good. Large EVPI (> 5-10%): uncertainty significantly affects optimal investment and stochastic programming provides real value.

### VSS: Value of the Stochastic Solution

VSS measures the benefit of stochastic programming vs. optimizing for the expected (average) scenario:

```
VSS = E[cost_deterministic_expected] - E[cost_stochastic]
```

Run the deterministic optimization with mean parameter values, then evaluate that plan under each scenario:

```python
# Deterministic plan cost evaluated across scenarios
# (requires running deterministic plan in each scenario's conditions)
vss = deterministic_expected_cost - stoch_cost
print(f"VSS: ${vss:,.0f}")
print(f"VSS as % of deterministic cost: {vss/deterministic_expected_cost:.1%}")
```

A positive VSS means the stochastic solution saves money. Typical VSS: 1-15% depending on uncertainty level and system flexibility.

---

## Scenario Comparison and Hedging

The stochastic solution typically exhibits hedging behavior:

- **More diversified investments**: Instead of betting entirely on solar (optimal under green transition) or diesel (optimal under high demand), the stochastic solution invests in a mix.
- **Earlier storage investment**: Batteries provide operational flexibility that is valuable across all scenarios.
- **Moderate RE targets**: Rather than aggressive RE deployment (optimal under green transition) or minimal RE (optimal under stagnation), the stochastic solution takes a middle path.

### Example Comparison Table

Illustrative numbers:

| Metric | Deterministic (Base) | Stochastic | Difference |
|--------|---------------------|------------|------------|
| Solar PV investment (MW) | 180 | 150 | -30 MW |
| Wind investment (MW) | 0 | 40 | +40 MW |
| Battery investment (MW) | 60 | 80 | +20 MW |
| Total cost (expected, $M) | 148 | 142 | -$6M |
| RE penetration (year 10) | 72% | 68% | -4% |
| Max load shedding (any scenario) | 12 MW | 3 MW | -9 MW |

The stochastic solution costs less in expectation with much lower worst-case load shedding, at the expense of slightly lower RE penetration in the base case.

---

## Computational Considerations

### Problem Size Scaling

| Component | Deterministic | 3 Scenarios | 5 Scenarios |
|-----------|--------------|-------------|-------------|
| Investment variables | V1 | V1 | V1 |
| Operational variables | V2 | 3 x V2 | 5 x V2 |
| Total constraints | C | C + 3 x C2 | C + 5 x C2 |
| Approximate solve time | T | ~3T | ~5T |

Problem size grows linearly with scenarios (operational variables replicated, investment variables shared).

### Practical Guidelines

| Scenarios | Solve Time | Recommended Use |
|-----------|-----------|----------------|
| 2-3 | 2-5x deterministic | Standard planning studies |
| 5-7 | 5-10x deterministic | Detailed uncertainty analysis |
| 10+ | 10-20x deterministic | Only with powerful hardware or scenario reduction |

### Tips for Large Problems

1. **Reduce representative days**: Use 3-5 representative days per year instead of 7-10. This is the biggest lever for reducing problem size.
2. **Coarser temporal resolution**: Set `resolution_hours: 2` or `resolution_hours: 6` to reduce hourly variables.
3. **Fewer years**: Start with 5-year horizons for testing, then extend to 10-25 years.
4. **Solver tuning**: Use Gurobi or CPLEX with barrier method for large stochastic problems. HiGHS is adequate for 2-3 scenarios but may be slow for more.
5. **Scenario reduction**: Start with many candidate scenarios and use k-means or similar techniques to select 3-5 representative scenarios that capture the key uncertainties.

---

## When to Use Stochastic vs. Deterministic

| Situation | Recommendation |
|-----------|---------------|
| Quick screening, single future | Deterministic |
| Moderate uncertainty, key decisions | Stochastic (3 scenarios) |
| High uncertainty, large investments | Stochastic (5+ scenarios) |
| Policy analysis, multiple alternatives | Deterministic + MGA/SPORES |
| Publication, comprehensive study | Stochastic + EVPI/VSS analysis |

### Decision Flowchart

1. Run deterministic base case first
2. Run sensitivity analysis [**[11]**](../reference/bibliography.md#ref11) to identify key uncertainties
3. If 2+ parameters have high Sobol indices (ST > 0.15), use stochastic programming
4. Design 2-3 scenarios covering the most uncertain parameters
5. Compare stochastic vs. deterministic investment plans
6. If EVPI > 5%, the stochastic solution provides significant value

---

## Key Takeaways

1. **Robust investments**: Stochastic programming produces investment plans that perform well across multiple futures, avoiding the risk of over-committing to a single scenario.
2. **Expected cost**: The objective minimizes probability-weighted total cost. The result may not be optimal for any single scenario but is best on average.
3. **Hedging behavior**: Stochastic solutions typically invest in more diverse technology portfolios and earlier storage deployment.
4. **EVPI and VSS**: These metrics quantify the value of better information (EVPI) and better modeling (VSS). They justify the additional computational effort of stochastic programming.
5. **Computational cost**: Scales linearly with the number of scenarios. Start with 2-3 scenarios; increase only if EVPI analysis suggests significant value.
6. **Scenario design**: Focus scenarios on the most impactful uncertainties (identified via sensitivity analysis). Scenario probabilities encode the planner's beliefs about the future.

!!! warning "Validation Advisory"
    The stochastic framework is structurally complete but should be validated carefully for production studies. Start with 2-3 scenarios and compare the plan with the deterministic run of each scenario before scaling up.
