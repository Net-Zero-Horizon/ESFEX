# Multi-Node Network

## Prerequisites

- Completed the [Single-Node Tutorial](single-system.md) or familiarity with basic ESFEX configuration
- ESFEX installed: `pip install esfex`
- Python 3.10+ with `h5py`, `numpy`, and `matplotlib` for results analysis

---

## Scenario

A 3-node island system with distinct resource zones:

- **Node 0 (Capital)**: High demand center (peak ~150 MW), existing diesel and some solar, limited space for new RE
- **Node 1 (Coast)**: Moderate demand (~60 MW peak), excellent wind resources, port area
- **Node 2 (Interior)**: Low demand (~40 MW peak), best solar irradiance, abundant land for solar farms

Transmission lines connect the nodes with limited capacity. The optimizer must decide where to invest in generation and whether to expand transmission to deliver remote renewable energy to load centers.

---

## Step 1: Complete Configuration

Create `multi_node_system.yaml`:

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
  time_limit: 3600
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
    demand_path: island_demand_3node.csv
    demand_scale: 1.0
    power_flow_mode: dcopf               # the default

    nodes:
      nodes_connections: [  0.0, 100.0,  0.0,    # Capital:  100 MW to Coast
                          100.0,   0.0, 50.0,    # Coast:    50 MW to Interior
                            0.0,  50.0,  0.0]    # Interior: no direct link to Capital
      node_coordinates:
        - {latitude: 18.00, longitude: -76.80}   # Capital
        - {latitude: 18.20, longitude: -77.50}   # Coast
        - {latitude: 17.80, longitude: -76.20}   # Interior
      node_names: ["Capital", "Coast", "Interior"]
      reserve_static: [5.0, 2.0, 1.0]
      reserve_dynamic: [3.0, 1.0, 1.0]
      reserve_duration: [2, 2, 2]
      losses: [0.02, 0.02, 0.02]
      transference_invest_cost: [500000.0, 500000.0, 600000.0]   # $/MW, per node
      transference_invest_max: [200.0, 200.0, 200.0]            # MW, per node

    dc_power_flow:
      max_angle_diff_deg: 30.0
      slack_bus: 0

    # One bus per node is created automatically (bus 0, 1, 2 in node order).
    # Lines need bus indices to enter the operational model.
    transmission_lines_geo:
      - line_id: line_0
        from_node: 0
        to_node: 1
        from_bus: 0
        to_bus: 1
        capacity_mw: 100.0
        reactance_pu: 0.05
        resistance_pu: 0.01
        length_km: 150.0
        voltage_kv: 220.0
        num_circuits: 1

      - line_id: line_1
        from_node: 1
        to_node: 2
        from_bus: 1
        to_bus: 2
        capacity_mw: 50.0
        reactance_pu: 0.08
        resistance_pu: 0.015
        length_km: 200.0
        voltage_kv: 110.0
        num_circuits: 1

    generators:                          # existing units
      solar_pv:
        name: Solar PV
        type: Renewable
        fuel: Sun
        Availability: solar_availability.csv
        rated_power: [20.0, 10.0, 30.0]  # MW per node
        min_power: [0.0, 0.0, 0.0]
        min_up: [0, 0, 0]
        min_down: [0, 0, 0]
        ramp_up: [1.0, 1.0, 1.0]
        ramp_down: [1.0, 1.0, 1.0]
        eff_at_rated: [1.0, 1.0, 1.0]
        eff_at_min: [1.0, 1.0, 1.0]
        inertia: [0.0, 0.0, 0.0]
        start_up_cost: [0.0, 0.0, 0.0]
        fuel_cost: [0.0, 0.0, 0.0]
        fixed_cost: [5.0, 5.0, 5.0]
        maintenance_cost: [2.0, 2.0, 2.0]
        life_time: [25, 25, 25]
        initial_age: [3, 2, 5]
        degradation_rate: [0.005, 0.005, 0.005]
        decommissioning_cost: [0.0, 0.0, 0.0]
        invest_cost: [750000.0, 750000.0, 700000.0]
        invest_max_power: [0.0, 0.0, 0.0]

      wind:
        name: Wind
        type: Renewable
        fuel: Wind
        Availability: wind_availability.csv
        rated_power: [0.0, 30.0, 0.0]    # existing wind only at Coast
        min_power: [0.0, 0.0, 0.0]
        min_up: [0, 0, 0]
        min_down: [0, 0, 0]
        ramp_up: [1.0, 1.0, 1.0]
        ramp_down: [1.0, 1.0, 1.0]
        eff_at_rated: [1.0, 1.0, 1.0]
        eff_at_min: [1.0, 1.0, 1.0]
        inertia: [0.0, 0.0, 0.0]
        start_up_cost: [0.0, 0.0, 0.0]
        fuel_cost: [0.0, 0.0, 0.0]
        fixed_cost: [8.0, 8.0, 8.0]
        maintenance_cost: [5.0, 5.0, 5.0]
        life_time: [20, 20, 20]
        initial_age: [0, 0, 0]
        degradation_rate: [0.005, 0.005, 0.005]
        decommissioning_cost: [0.0, 0.0, 0.0]
        invest_cost: [1200000.0, 1100000.0, 1300000.0]
        invest_max_power: [0.0, 0.0, 0.0]

      diesel:
        name: Diesel
        type: Non-renewable
        fuel: Diesel
        rated_power: [200.0, 50.0, 30.0] # most diesel at Capital
        min_power: [0.3, 0.3, 0.3]
        min_up: [4, 4, 4]
        min_down: [2, 2, 2]
        ramp_up: [0.5, 0.5, 0.5]
        ramp_down: [0.5, 0.5, 0.5]
        eff_at_rated: [0.40, 0.40, 0.40]
        eff_at_min: [0.30, 0.30, 0.30]
        inertia: [5.0, 5.0, 5.0]
        start_up_cost: [5000.0, 5000.0, 5000.0]
        fuel_cost: [85.0, 85.0, 85.0]
        fixed_cost: [3.0, 3.0, 3.0]
        maintenance_cost: [5.0, 5.0, 5.0]
        life_time: [30, 30, 30]
        initial_age: [15, 10, 12]
        degradation_rate: [0.01, 0.01, 0.01]
        decommissioning_cost: [100000.0, 100000.0, 100000.0]
        invest_cost: [500000.0, 500000.0, 500000.0]
        invest_max_power: [0.0, 0.0, 0.0]

    technologies:                        # candidates, per node
      solar_new:
        name: Solar PV
        type: Renewable
        fuel: Sun
        Availability: solar_availability.csv
        invest_cost: [750000.0, 750000.0, 700000.0]   # slightly cheaper inland
        invest_max_power: [200.0, 100.0, 300.0]       # more room inland
        eff_at_rated: [1.0, 1.0, 1.0]
        degradation_rate: [0.005, 0.005, 0.005]
        lifetime: 25
        fixed_cost: [5.0, 5.0, 5.0]
        maintenance_cost: [2.0, 2.0, 2.0]

      wind_new:
        name: Wind
        type: Renewable
        fuel: Wind
        Availability: wind_availability.csv
        invest_cost: [1200000.0, 1100000.0, 1300000.0]
        invest_max_power: [50.0, 200.0, 50.0]         # major wind potential at Coast
        eff_at_rated: [1.0, 1.0, 1.0]
        degradation_rate: [0.005, 0.005, 0.005]
        lifetime: 20
        fixed_cost: [8.0, 8.0, 8.0]
        maintenance_cost: [5.0, 5.0, 5.0]

    battery_technologies:
      li_ion:
        name: Li-Ion Battery
        invest_cost_power: [200000.0, 200000.0, 200000.0]
        invest_cost_energy: [150000.0, 150000.0, 150000.0]
        invest_max_power: [100.0, 100.0, 100.0]
        invest_max_capacity: [400.0, 400.0, 400.0]
        min_duration_hours: 2.0
        max_duration_hours: 6.0
        efficiency_charge: [0.95, 0.95, 0.95]
        efficiency_discharge: [0.95, 0.95, 0.95]
        degradation_rate: [0.02, 0.02, 0.02]
        lifetime: 15
        maintenance_cost: [1.0, 1.0, 1.0]

    penalties:
      loss_of_load: 10000.0
      curtailment_cost: 50.0
      max_curtailment_ratio: 0.05
      loss_of_reserve_static: 500.0
      fre_penetration_loss: 600.0

    co2_budget:
      enabled: true
      annual_budget: 500000.0

    TARGET_RE_PENETRATION: 0.80
    discount_rate: 0.08
    MAX_ANNUAL_SYSTEM_COST: 500000000.0
```

### Network Topology

`nodes_connections` is the node adjacency matrix, flattened row by row; entry (i, j) is the transfer capacity in MW:

```
         Capital  Coast  Interior
Capital  [  0,     100,    0   ]
Coast    [ 100,     0,    50   ]
Interior [  0,      50,    0   ]
```

- **Capital — Coast**: 100 MW bidirectional capacity (line_0)
- **Coast — Interior**: 50 MW bidirectional capacity (line_1)
- **Capital — Interior**: No direct connection (power must flow through Coast)

The master problem plans on this matrix. When `transmission_lines_geo` is given, the operational dispatch builds its network from those lines alone, so keep the two consistent. A line without `from_bus`/`to_bus` is skipped with a warning.

Wind energy from Coast can reach Capital directly (100 MW limit) or Interior (50 MW limit). Solar energy from Interior must pass through Coast to reach Capital, competing for the 50 MW Coast-Interior corridor.

### Transmission Expansion

`transference_invest_cost` and `transference_invest_max` are per-node lists. The master problem can expand only corridors that exist as a line (here Capital-Coast and Coast-Interior). An expansion adds the same MW in both directions and is charged once, at the higher of the two end nodes' costs: $500,000/MW for Capital-Coast and $600,000/MW for Coast-Interior. The expansion of a corridor is limited by the smaller `transference_invest_max` of its two end nodes.

### DC Power Flow Parameters

The DC power flow model [**[1]**](../reference/bibliography.md#ref1) enforces Kirchhoff's Voltage Law (KVL); power flows are determined by electrical impedance, not just capacity limits:

| Parameter | line_0 | line_1 | Notes |
|-----------|--------|--------|-------|
| `reactance_pu` | 0.05 | 0.08 | Lower reactance = more power flow |
| `resistance_pu` | 0.01 | 0.015 | Drives the piecewise-linear line losses |
| `length_km` | 150 | 200 | Used to derive the reactance when `reactance_pu` is missing |
| `voltage_kv` | 220 | 110 | Nominal voltage |

### Per-Node Arrays

In a multi-node system, every generator, technology and battery parameter is a list with one entry per node. For example, `rated_power: [20.0, 10.0, 30.0]` means 20 MW solar at Node 0, 10 MW at Node 1, and 30 MW at Node 2. Use `0.0` where a unit is not present at a node, and `invest_max_power: 0.0` where a technology cannot be built.

---

## Step 2: Prepare Input Data

### Multi-Node Demand File

Create `island_demand_3node.csv` with 8,760 rows, 3 columns (one per node, in node order) and no header row:

```csv
95.0,38.0,25.0
88.0,35.0,22.0
...
100.0,40.0,27.0
```

The Capital node carries approximately 60% of total demand, the Coast ~24%, and the Interior ~16%.

### Availability Files

A single-column availability file is applied to every node. For example, `solar_availability.csv` is shared across all 3 nodes. If solar irradiance differs by location, give the file one column per node (see [Availability Profiles](../user-guide/availability-profiles.md)).

---

## Step 3: Run the Simulation

```bash
esfex validate -c multi_node_system.yaml
esfex run -c multi_node_system.yaml --years 10 -v
```

The multi-node simulation takes longer because the optimizer considers transmission flows and spatial generation-demand balance. Expect 30-90 minutes depending on hardware.

---

## Step 4: Interpreting Network Results

The run writes `results/multi_node_system_<ddmmyyyy>_<HHMM>.h5`. Per-year results are under `detailed_results/year_<YYYY>_threshold_0/`, with years labelled from 2025. The snippets below look at year 5 (2029) of the newest file:

```python
from pathlib import Path

import h5py
import numpy as np

h5_path = max(Path("results").glob("multi_node_system_*.h5"), key=lambda p: p.stat().st_mtime)
year = "detailed_results/year_2029_threshold_0"
nodes = ["Capital", "Coast", "Interior"]
```

### Power Flows

`power_flow` is one `[nodes x nodes x hours]` dataset. A line's signed flow is stored at one orientation and the reverse entry is zero, so the net flow from node i to node j is `pf[i, j] - pf[j, i]`:

```python
with h5py.File(h5_path, "r") as f:
    pf = f[f"{year}/power_flow"][:]

flow_01 = pf[0, 1] - pf[1, 0]   # Capital -> Coast
flow_12 = pf[1, 2] - pf[2, 1]   # Coast -> Interior
for label, data in [("Capital->Coast", flow_01), ("Coast->Interior", flow_12)]:
    print(f"{label}: avg={data.mean():.1f} MW, "
          f"max={data.max():.1f} MW, min={data.min():.1f} MW")
```

Positive values mean flow from the first node to the second; negative values mean reverse flow. For Capital->Coast, negative flow means Coast exports to Capital.

Expected results by year 5 (illustrative):

| Line | Direction | Avg Flow | Peak Flow | Interpretation |
|------|-----------|----------|-----------|----------------|
| line_0 | Coast -> Capital | -45 MW | -98 MW | Wind exports from Coast |
| line_1 | Interior -> Coast | -25 MW | -48 MW | Solar exports from Interior |

### Transmission Congestion Analysis

Congestion occurs when flow reaches line capacity. Congested hours indicate bottlenecks that may justify transmission expansion.

```python
for label, data, capacity in [("Capital-Coast", flow_01, 100.0),
                              ("Coast-Interior", flow_12, 50.0)]:
    congested = np.sum(np.abs(data) >= capacity * 0.99)
    utilization = np.mean(np.abs(data)) / capacity
    print(f"{label}: congested hours {congested} / {len(data)} "
          f"({congested / len(data):.1%}), average utilization {utilization:.1%}")
```

If the corridor was expanded in an earlier year, compare against the expanded capacity.

Congestion interpretation:

| Congestion Level | Hours/Year | Meaning |
|-----------------|------------|---------|
| Low | < 200 | Line capacity is adequate |
| Moderate | 200-1000 | Potential for cost savings with expansion |
| High | > 1000 | Strong economic case for transmission investment |

### Transmission Investment

The master problem expands a corridor when the value of delivering cheaper energy exceeds the expansion cost. Its decisions are stored as attributes `investment_transfer_investment_<i>_<j>` (MW, node indices from 0) of each year's group:

```python
with h5py.File(h5_path, "r") as f:
    for name, grp in f["detailed_results"].items():
        for key, mw in grp.attrs.items():
            if key.startswith("investment_transfer_investment_"):
                i, j = key.rsplit("_", 2)[-2:]
                print(f"{name}: {nodes[int(i)]}-{nodes[int(j)]} +{mw:.1f} MW")
```

In this scenario, the Coast-Interior corridor (50 MW, $600,000/MW) is likely expanded first because Interior has 300 MW of solar investment potential but only 50 MW of export capacity. The Capital-Coast corridor (100 MW, $500,000/MW) may also be expanded if wind investment at Coast is large.

### Locational Marginal Prices

With multi-node systems, electricity prices vary by location. Each node's price reflects the marginal cost of serving one additional MW of demand there.

```python
with h5py.File(h5_path, "r") as f:
    prices = f[f"{year}/nodal_electricity_prices"][:]   # [nodes x hours], $/MWh

for n, name in enumerate(nodes):
    print(f"{name}: avg=${prices[n].mean():.2f}/MWh, "
          f"peak=${prices[n].max():.2f}/MWh, "
          f"off-peak=${np.percentile(prices[n], 10):.2f}/MWh")
```

Expected price patterns (illustrative):

| Node | Avg Price | Explanation |
|------|-----------|-------------|
| Capital | $80-120/MWh | High demand, relies on diesel + imports |
| Coast | $40-70/MWh | Local wind generation keeps prices low |
| Interior | $30-60/MWh | Abundant solar, but may spike without export capacity |

**Price differences between nodes indicate transmission congestion value.** If Capital prices are consistently $30/MWh higher than Coast during wind hours, the congestion rent on the Capital-Coast line is approximately $30/MWh times the flow, representing the economic value of expanding that corridor.

### Visualizing Power Flows

```python
import matplotlib.pyplot as plt

# Plot one week
hours = np.arange(168)
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True)

ax1.plot(hours, flow_01[:168], "b-", linewidth=1)
ax1.axhline(y=100, color="r", linestyle="--", label="Capacity (+)")
ax1.axhline(y=-100, color="r", linestyle="--", label="Capacity (-)")
ax1.set_ylabel("Flow (MW)")
ax1.set_title("Line 0-1: Capital - Coast")
ax1.legend()

ax2.plot(hours, flow_12[:168], "g-", linewidth=1)
ax2.axhline(y=50, color="r", linestyle="--", label="Capacity (+)")
ax2.axhline(y=-50, color="r", linestyle="--", label="Capacity (-)")
ax2.set_xlabel("Hour")
ax2.set_ylabel("Flow (MW)")
ax2.set_title("Line 1-2: Coast - Interior")
ax2.legend()

plt.tight_layout()
plt.savefig("transmission_flows.png", dpi=150)
```

### Per-Node Generation Analysis

Each unit's output is a `[nodes x hours]` dataset under `generation/`; units built by the master problem appear as `Investment <technology name>`:

```python
with h5py.File(h5_path, "r") as f:
    gen = f["detailed_results/year_2034_threshold_0/generation"]
    for n, node_name in enumerate(nodes):
        print(f"\n{node_name}:")
        for unit, ds in gen.items():
            energy = ds[n, :].sum()
            if energy > 0:
                print(f"  {unit}: {energy:.0f} MWh")
```

---

## Key Takeaways

1. **Spatial optimization**: The model co-optimizes generation investment and transmission expansion. Cheap remote renewables are only valuable if transmission can deliver them to load centers.
2. **Resource-load mismatch**: Wind at Coast and solar at Interior are far from the Capital load center. This geographic separation drives the need for transmission planning alongside generation planning.
3. **Transmission investment signals**: Congestion hours and locational price differences are the primary indicators of transmission investment value. The optimizer balances expansion costs against congestion savings.
4. **DC power flow (KVL)** [**[1]**](../reference/bibliography.md#ref1): Unlike a simple transport model, the DC power flow ensures physically consistent flows that respect Kirchhoff's laws [**[41]**](../reference/bibliography.md#ref41). Power distributes across parallel paths according to impedance, not just capacity.
5. **Locational prices**: Price differences between nodes directly reflect the marginal value of transmission expansion. Persistent price gaps signal that the network is constraining the use of cheaper generation.
6. **Cascading flows**: Without a direct Capital-Interior link, solar power from Interior must transit through Coast. This means the Coast-Interior and Capital-Coast lines share the burden, and expanding one may shift congestion to the other.

---

## Next Steps

- [Multi-System](multi-system.md) — connect multiple independent power systems
- [Sensitivity Analysis](sensitivity-analysis.md) — analyze sensitivity to transmission costs
- [Configuration Reference](../reference/config-reference.md) — all transmission parameters
