# EV Integration

## Scenario

An island power system with growing EV adoption over a 15-year planning horizon. Three vehicle categories — private sedans, commercial delivery vans, and public transit buses — each have distinct charging patterns and V2G participation rates. The optimizer schedules EV charging and determines whether V2G discharge can reduce peak generation needs.

---

## Prerequisites

- A working ESFEX installation with Julia backend configured
- A base system configuration YAML file (e.g., from the Getting Started tutorial)
- Demand and availability profile files for the system

---

## S-Curve Growth Model

ESFEX models EV fleet growth using a logistic (S-curve) function [**[53]**](../reference/bibliography.md#ref53): slow initial uptake, rapid mid-period growth, and eventual saturation.

The fleet of a category at year `t` is:

```
fleet(t) = ev_quantity * max_adoption / (1 + exp(-growth_rate * (t - mid_point_year)))
mid_point_year = first_year + n_years * mid_point_fraction
```

Where:

- `ev_quantity`: Number of vehicles per node given in the configuration
- `max_adoption`: Saturation multiplier on `ev_quantity` (default 35)
- `growth_rate`: Controls how fast adoption accelerates (default 0.14; higher = steeper curve)
- `mid_point_fraction`: Position of the inflection point within the simulated horizon (default 0.5 = midpoint)
- `first_year`, `n_years`: First simulated year and number of simulated years (`esfex run --years`)

The multiplier is not 1 in the first year, and the curve depends on the run length. For example, with `max_adoption: 30`, `growth_rate: 0.12` and a 15-year run, the first year already has about 8.7 times `ev_quantity` vehicles. Calibrate `ev_quantity` with the run length in mind.

### Growth Rate Examples

| `growth_rate` | Adoption Speed | Typical Use Case |
|---------------|---------------|------------------|
| 0.05 | Very slow | Conservative policy, no subsidies |
| 0.12 | Moderate | Gradual transition with incentives |
| 0.20 | Fast | Aggressive policy, strong subsidies |
| 0.30 | Very fast | Mandated transition, ban on ICE sales |

### Max Adoption Examples

| `max_adoption` | Meaning | Scenario |
|----------------|---------|----------|
| 5 | Saturates at 5x `ev_quantity` | Low electrification |
| 15 | Saturates at 15x `ev_quantity` | Moderate electrification |
| 30 | Saturates at 30x `ev_quantity` | Near-full electrification |
| 50 | Saturates at 50x `ev_quantity` | Growth beyond current vehicle stock |

---

## Configuration

### EV Section of the System

The EV model runs when a system defines all three of `ev_categories`, `ev_quantity` and `base_patterns`:

```yaml
systems:
  island:
    # ... (nodes, generators, batteries, etc.)

    # ── EV Categories ──────────────────────────────────────────────
    ev_categories:
      sedan:
        battery_capacity: 60.0            # kWh per vehicle
        charging_power: 11.0              # kW per vehicle (Level 2 AC)
        v2g_power: 7.0                    # kW per vehicle for V2G
        v2g_participation: 0.30           # 30 % of the fleet offers V2G
        efficiency_charge: 0.95
        efficiency_discharge: 0.92
        min_soc: 0.20                     # fleet SOC floor
        daily_energy_kwh: 8.0             # kWh charged per vehicle per day
        max_adoption: 30.0                # saturates at 30x ev_quantity
        growth_rate: 0.12                 # moderate adoption speed
        mid_point_fraction: 0.5           # inflection at mid-horizon

      commercial_van:
        battery_capacity: 80.0
        charging_power: 22.0
        v2g_power: 15.0
        v2g_participation: 0.15           # commercial use comes first
        efficiency_charge: 0.94
        efficiency_discharge: 0.91
        min_soc: 0.25
        daily_energy_kwh: 25.0
        max_adoption: 20.0
        growth_rate: 0.10
        mid_point_fraction: 0.55          # slightly later adoption than sedans

      electric_bus:
        battery_capacity: 300.0
        charging_power: 50.0              # DC charging at the depot
        v2g_power: 30.0
        v2g_participation: 0.50           # centrally managed fleet
        efficiency_charge: 0.93
        efficiency_discharge: 0.90
        min_soc: 0.30
        daily_energy_kwh: 200.0
        max_adoption: 15.0
        growth_rate: 0.08
        mid_point_fraction: 0.45          # earlier, government-led adoption

    # ── Fleet Quantities (per node) ────────────────────────────────
    ev_quantity:
      sedan: [500, 300, 200]              # nodes 0, 1, 2
      commercial_van: [80, 50, 30]
      electric_bus: [20, 10, 5]

    # ── 24-Hour Base Patterns (one flat list of 24 values per category) ──
    base_patterns:
      sedan: [0.05, 0.05, 0.05, 0.05, 0.05, 0.10,    # 00-05: overnight trickle
              0.15, 0.10, 0.05, 0.05, 0.05, 0.05,    # 06-11: most at work
              0.05, 0.05, 0.10, 0.15, 0.20, 0.80,    # 12-17: return home
              0.90, 0.85, 0.70, 0.50, 0.30, 0.10]    # 18-23: evening peak
      commercial_van: [0.70, 0.70, 0.65, 0.60, 0.50, 0.20,
                       0.05, 0.0, 0.0, 0.0, 0.0, 0.0,
                       0.0, 0.0, 0.0, 0.0, 0.05, 0.10,
                       0.20, 0.40, 0.60, 0.70, 0.70, 0.70]
      electric_bus: [0.80, 0.80, 0.80, 0.70, 0.50, 0.10,
                     0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                     0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                     0.05, 0.10, 0.30, 0.50, 0.70, 0.80]
```

### Category Fields

| Field | Required | Description |
|-------|----------|-------------|
| `battery_capacity` | yes | Battery size per vehicle (kWh) |
| `charging_power` | yes | Charging power per vehicle (kW) |
| `v2g_power` | yes | V2G discharge power per vehicle (kW) |
| `v2g_participation` | yes | Fraction of the fleet that offers V2G (0-1) |
| `efficiency_charge`, `efficiency_discharge` | yes | Charge and V2G efficiencies (0-1) |
| `min_soc` | yes | Minimum fleet state of charge (0-1) |
| `daily_energy_kwh` | no | Energy charged per vehicle per day; default `battery_capacity` × 0.12 |
| `max_adoption`, `growth_rate`, `mid_point_fraction` | no | S-curve parameters (defaults 35, 0.14, 0.5) |

There is no maximum SOC or V2G compensation field: the fleet may charge up to full capacity, and V2G is not paid in the objective.

`EV_initial_soc` sets the fleet SOC at the start of each dispatch window, per node, as a fraction of the fleet's battery capacity (80 % where not given); each window must end at the same level.

### Base Patterns

A base pattern is a 24-value daily shape per category. It is scaled so that each vehicle's daily total equals `daily_energy_kwh`, and a small seeded noise is added (the same configuration always gives the same profiles). Each value also sets the share of the category's V2G power on offer that hour:

```
EV energy (MW)   = pattern[h] / sum(pattern) * daily_energy_kwh / 1000 * fleet
V2G capacity (MW) = pattern[h] * v2g_participation * v2g_power / 1000 * fleet
```

| Factor | Private Sedans | Commercial Vans | Public Buses |
|--------|---------------|-----------------|--------------|
| Peak pattern hours | 18:00-22:00 | 00:00-05:00 | 00:00-05:00 |
| Operating hours | 07:00-17:00 | 06:00-18:00 | 06:00-21:00 |
| Charging location | Home/work | Depot | Depot |

### How the Dispatch Uses the Fleet

All categories are combined into one fleet per node, with vehicle-weighted powers and battery sizes and energy-weighted efficiencies and `min_soc`. In each dispatch window:

- The EV energy profile above is the energy the fleet draws from its batteries each hour (driving).
- The optimizer chooses when to charge, up to `charging_power` × vehicles, keeping the fleet SOC between `min_soc` and full capacity.
- V2G discharge [**[51]**](../reference/bibliography.md#ref51) feeds the grid during peak or high-price hours [**[52]**](../reference/bibliography.md#ref52), up to the V2G capacity above.
- Driving energy the fleet cannot supply is recorded as `EV_loss` and costs `penalties.ev_loss` per MWh (default: equal to `loss_of_load`).

The master problem plans capacity with the EV energy profile added to the base demand as a fixed load; the dispatch then decides when the fleet charges.

---

## Running the Simulation

```bash
# Run with verbose output
esfex run -c ev_system.yaml --years 15 -v

# Run with specific output directory
esfex run -c ev_system.yaml --years 15 -o results/ev_study/ -v
```

The results file is `<output>/ev_system_<ddmmyyyy>_<HHMM>.h5`.

---

## Results Analysis

Each simulated year has a group `detailed_results/year_<YYYY>_threshold_0/` with these EV datasets, all `[node x hour]`:

| Dataset | Units | Content |
|---------|-------|---------|
| `EV_charging` | MW | Charging power |
| `EV_V2G` | MW | V2G discharge |
| `EV_soc` | MWh | Fleet state of charge |
| `EV_loss` | MW | Driving energy not supplied |

The group's `demand` dataset holds the base demand without EV charging, and `nodal_electricity_prices` the hourly price per node.

### EV Charging Over Time

```python
import glob
import h5py
import numpy as np

path = sorted(glob.glob("results/ev_study/ev_system_*.h5"))[-1]   # newest run

with h5py.File(path, "r") as f:
    print(f"{'Year':>6} {'Peak Charge (MW)':>18} {'Peak V2G (MW)':>16} "
          f"{'Total Charge (GWh)':>20} {'Total V2G (GWh)':>18}")
    print("-" * 82)

    for name in sorted(f["detailed_results"]):
        grp = f[f"detailed_results/{name}"]
        if "EV_charging" not in grp:
            continue
        charging = grp["EV_charging"][:].sum(axis=0)   # sum over nodes
        v2g = grp["EV_V2G"][:].sum(axis=0) if "EV_V2G" in grp else np.zeros_like(charging)

        print(f"{grp.attrs['year']:>6} {charging.max():>18.1f} {v2g.max():>16.1f} "
              f"{charging.sum() / 1000:>20.1f} {v2g.sum() / 1000:>18.1f}")
```

The yearly totals follow the S-curve: slow growth in the early years, rapid growth around `mid_point_year`, then saturation.

### EV Share of Demand

```python
with h5py.File(path, "r") as f:
    for name in sorted(f["detailed_results"]):
        grp = f[f"detailed_results/{name}"]
        if "EV_charging" not in grp:
            continue
        base = grp["demand"][:].sum(axis=0)          # MW per hour, all nodes
        ev = grp["EV_charging"][:].sum(axis=0)
        total = base + ev
        print(f"{grp.attrs['year']}: EV share of peak {ev[np.argmax(total)] / total.max():.1%}, "
              f"of energy {ev.sum() / total.sum():.1%}")
```

### V2G Value and Grid Contribution

```python
with h5py.File(path, "r") as f:
    for name in sorted(f["detailed_results"]):
        grp = f[f"detailed_results/{name}"]
        if "EV_V2G" not in grp:
            continue
        v2g = grp["EV_V2G"][:]                         # [node x hour]
        prices = grp["nodal_electricity_prices"][:]    # [node x hour]
        n = min(v2g.shape[1], prices.shape[1])

        value = np.sum(v2g[:, :n] * prices[:, :n])     # energy value at nodal prices
        peak_hour = int(np.argmax(prices.mean(axis=0)))

        print(f"{grp.attrs['year']}:")
        print(f"  V2G energy value:      ${value:>12,.0f}")
        print(f"  V2G at price peak:     {v2g[:, peak_hour].sum():>8.1f} MW")
        print(f"  Total V2G energy:      {v2g.sum():>8.0f} MWh")
```

### Hourly Charging Profile for One Day

```python
with h5py.File(path, "r") as f:
    name = sorted(f["detailed_results"])[-1]           # last simulated year
    charging = f[f"detailed_results/{name}/EV_charging"][:].sum(axis=0)

    day_start = 180 * 24                               # day 180
    day_profile = charging[day_start:day_start + 24]

    print("Hour | Charging (MW) | Bar")
    print("-" * 50)
    for h in range(24):
        bar = "#" * int(day_profile[h] / 2)
        print(f"  {h:02d} | {day_profile[h]:>12.1f} | {bar}")
```

---

## Cost Implications of EV Integration

| Cost Component | Effect of EVs | Direction |
|---------------|---------------|-----------|
| Generation capacity | More peak capacity needed for charging | Increases cost |
| RE investment | More solar/wind to serve EV load | Increases investment |
| Battery storage | V2G reduces need for grid batteries | Decreases investment |
| Fuel cost | Higher demand increases fossil fuel use | Increases cost |
| Curtailment | EVs can absorb excess RE (smart charging) | Decreases waste |
| Grid reinforcement | Higher peak loads stress transmission | Increases cost |

The master plans for the EV energy as unmanaged load, so smart charging and V2G show up only in the dispatch (cost, load shedding, `EV_loss`, curtailment). To quantify the EV effects, compare runs with and without EV:

```python
import glob
import h5py

scenarios = {
    "No EV": sorted(glob.glob("results/no_ev/*.h5"))[-1],
    "With EV": sorted(glob.glob("results/ev_study/*.h5"))[-1],
}

for name, path in scenarios.items():
    with h5py.File(path, "r") as f:
        summary = f["summary_results"]
        cost = summary["total_cost"][:].sum()
        re_pen = summary["renewable_penetration"][-1]
        gen_inv = sum(ds[:].sum()
                      for y in f["detailed_results"].values()
                      if "gen_investment_power" in y
                      for ds in y["gen_investment_power"].values())

        print(f"\n{name}:")
        print(f"  Total cost:           ${cost:>14,.0f}")
        print(f"  Final RE penetration: {re_pen:>8.1%}")
        print(f"  Total gen investment: {gen_inv:>8.1f} MW")
```

---

## Practical Tips

1. **Start small**: Test with a single EV category (sedan) before adding commercial and bus categories. This simplifies debugging.

2. **Avoid double-counting EV demand**: The demand file should not contain EV charging. The runner adds the EV fleet to the dispatch itself.

3. **Pattern sensitivity**: Charging patterns strongly influence peak demand. Run sensitivity analysis on pattern shapes if uncertain about real-world behavior.

4. **V2G participation**: There is no V2G payment or battery degradation cost in the model, so V2G is limited only by `v2g_participation`, `v2g_power` and the fleet SOC. Choose `v2g_participation` to reflect how many owners would really take part.

5. **Node distribution**: Distribute EV quantities across nodes proportionally to population or vehicle registration data. Uneven distribution reveals localized grid stress.

6. **Growth rate calibration**: Compare your S-curve parameters against historical EV adoption data from comparable regions. Norway (fast), EU average (moderate), and developing economies (slow) provide useful benchmarks.

7. **Computational note**: EV profiles are generated once at simulation startup. In the dispatch, EVs add four variables per node and hour (charging, V2G, SOC, unsupplied driving).

---

## Key Takeaways

1. **S-curve adoption** [**[53]**](../reference/bibliography.md#ref53): Fleet growth starts slow, accelerates in mid-years, then saturates — matching real-world technology diffusion patterns.
2. **Demand impact**: EV charging becomes a significant fraction of total demand by year 10-15, potentially 15-30% of peak load.
3. **V2G value**: Bidirectional charging provides peak shaving, partially offsetting the cost of serving EV demand.
4. **Charging patterns**: Off-peak charging (buses at night) is naturally aligned with low-cost hours and can absorb excess renewable generation.
5. **Storage synergy**: EV batteries can shift energy in time like stationary storage, within the limits of driving needs and `min_soc`.
6. **Category diversity**: Different vehicle types (private, commercial, transit) have complementary charging patterns, smoothing aggregate demand.
