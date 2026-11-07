# Multi-System Interconnection

## Prerequisites

- Completed the [Single-Node Tutorial](single-system.md) and [Multi-Node Tutorial](multi-node.md)
- ESFEX installed: `pip install esfex`
- Python 3.10+ with `h5py` and `numpy` for results analysis

---

## Scenario

Two island systems connected by a submarine cable:

- **System A (Main Island)**: 3 nodes, ~500 MW peak demand, diverse generation mix (diesel, solar, wind). Relatively developed grid with transmission between nodes.
- **System B (Small Island)**: 1 node, ~50 MW peak demand, limited generation (diesel + small solar). High electricity costs due to small scale and fuel dependency.

The submarine cable allows power transfer between Node 2 of the Main Island and Node 0 of the Small Island.

---

## Step 1: Complete Configuration

Create `multi_system.yaml`:

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

# --- Meta-Network: defines which systems exist and how they connect ---
meta_network:
  systems:
    - main_island                 # first system = primary system
    - small_island

  systems_links:
    - systems: [main_island, small_island]
      connections: [[2, 0]]       # [node of main_island, node of small_island]
      existing_capacity_MW: [30.0]
      max_investment_MW: [100.0]
      investment_cost_per_MW: [800000.0]
      distance_km: [80.0]
      resistance_pu: [0.03]
      loss_factor: [0.03]         # required, see the note below
      cost_per_mw_km: [0.0]       # flow cost $/MWh/km, see the note below

systems:
  # --- System A: Main Island (3 nodes) ---
  main_island:
    name: main_island
    demand_path: main_island_demand.csv
    demand_scale: 1.0

    nodes:
      nodes_connections: [  0.0, 100.0, 50.0,
                          100.0,   0.0, 80.0,
                           50.0,  80.0,  0.0]
      node_coordinates:
        - {latitude: 18.00, longitude: -76.80}   # Node 0: Capital
        - {latitude: 18.20, longitude: -77.50}   # Node 1: West Coast
        - {latitude: 17.80, longitude: -76.20}   # Node 2: East (cable landing)
      node_names: ["Capital", "West Coast", "East"]
      reserve_static: [5.0, 2.0, 2.0]
      reserve_dynamic: [3.0, 1.0, 1.0]
      reserve_duration: [2, 2, 2]
      losses: [0.02, 0.02, 0.02]

    # Internal lines with bus indices (one bus per node, created automatically)
    transmission_lines_geo:
      - {line_id: l_cap_west,  from_node: 0, to_node: 1, from_bus: 0, to_bus: 1, capacity_mw: 100.0}
      - {line_id: l_cap_east,  from_node: 0, to_node: 2, from_bus: 0, to_bus: 2, capacity_mw: 50.0}
      - {line_id: l_west_east, from_node: 1, to_node: 2, from_bus: 1, to_bus: 2, capacity_mw: 80.0}

    generators:
      solar_pv:
        name: Solar PV
        type: Renewable
        fuel: Sun
        Availability: solar_availability.csv
        rated_power: [30.0, 20.0, 40.0]
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
        initial_age: [3, 2, 4]
        degradation_rate: [0.005, 0.005, 0.005]
        decommissioning_cost: [0.0, 0.0, 0.0]
        invest_cost: [750000.0, 750000.0, 700000.0]
        invest_max_power: [0.0, 0.0, 0.0]

      wind:
        name: Wind
        type: Renewable
        fuel: Wind
        Availability: wind_availability.csv
        rated_power: [0.0, 40.0, 0.0]
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
        rated_power: [200.0, 80.0, 50.0]
        min_power: [0.3, 0.3, 0.3]
        min_up: [4, 4, 4]
        min_down: [2, 2, 2]
        ramp_up: [0.5, 0.5, 0.5]
        ramp_down: [0.5, 0.5, 0.5]
        eff_at_rated: [0.40, 0.40, 0.40]
        eff_at_min: [0.30, 0.30, 0.30]
        inertia: [5.0, 5.0, 5.0]
        start_up_cost: [5000.0, 5000.0, 5000.0]
        fuel_cost: [80.0, 80.0, 80.0]
        fixed_cost: [3.0, 3.0, 3.0]
        maintenance_cost: [5.0, 5.0, 5.0]
        life_time: [30, 30, 30]
        initial_age: [12, 8, 10]
        degradation_rate: [0.01, 0.01, 0.01]
        decommissioning_cost: [100000.0, 100000.0, 100000.0]
        invest_cost: [500000.0, 500000.0, 500000.0]
        invest_max_power: [0.0, 0.0, 0.0]

    technologies:
      solar_new:
        name: Solar PV
        type: Renewable
        fuel: Sun
        Availability: solar_availability.csv
        invest_cost: [750000.0, 750000.0, 700000.0]
        invest_max_power: [200.0, 150.0, 250.0]
        eff_at_rated: [1.0, 1.0, 1.0]
        degradation_rate: [0.005, 0.005, 0.005]
        lifetime: 25
        maintenance_cost: [2.0, 2.0, 2.0]

      wind_new:
        name: Wind
        type: Renewable
        fuel: Wind
        Availability: wind_availability.csv
        invest_cost: [1200000.0, 1100000.0, 1300000.0]
        invest_max_power: [100.0, 300.0, 50.0]
        eff_at_rated: [1.0, 1.0, 1.0]
        degradation_rate: [0.005, 0.005, 0.005]
        lifetime: 20
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

    penalties:
      loss_of_load: 10000.0
      curtailment_cost: 50.0
      max_curtailment_ratio: 0.05
      loss_of_reserve_static: 500.0
      fre_penetration_loss: 600.0

    co2_budget:
      enabled: true
      annual_budget: 900000.0     # main_island's own plants

    TARGET_RE_PENETRATION: 0.80
    discount_rate: 0.08
    MAX_ANNUAL_SYSTEM_COST: 500000000.0

  # --- System B: Small Island (1 node) ---
  small_island:
    name: small_island
    demand_path: small_island_demand.csv
    demand_scale: 1.0

    nodes:
      nodes_connections: [0.0]
      node_coordinates:
        - {latitude: 17.50, longitude: -75.50}
      node_names: ["Small Island"]

    generators:
      solar_pv:
        name: Solar PV
        type: Renewable
        fuel: Sun
        Availability: solar_availability.csv
        rated_power: [10.0]
        min_power: [0.0]
        min_up: [0]
        min_down: [0]
        ramp_up: [1.0]
        ramp_down: [1.0]
        eff_at_rated: [1.0]
        eff_at_min: [1.0]
        inertia: [0.0]
        start_up_cost: [0.0]
        fuel_cost: [0.0]
        fixed_cost: [6.0]
        maintenance_cost: [3.0]
        life_time: [25]
        initial_age: [2]
        degradation_rate: [0.005]
        decommissioning_cost: [0.0]
        invest_cost: [800000.0]
        invest_max_power: [0.0]

      diesel:
        name: Diesel
        type: Non-renewable
        fuel: Diesel
        rated_power: [40.0]
        min_power: [0.3]
        min_up: [3]
        min_down: [2]
        ramp_up: [0.5]
        ramp_down: [0.5]
        eff_at_rated: [0.35]
        eff_at_min: [0.25]
        inertia: [4.0]
        start_up_cost: [3000.0]
        fuel_cost: [120.0]        # higher fuel cost (shipping premium)
        fixed_cost: [5.0]
        maintenance_cost: [8.0]
        life_time: [25]
        initial_age: [18]
        degradation_rate: [0.015]
        decommissioning_cost: [80000.0]
        invest_cost: [500000.0]
        invest_max_power: [0.0]

    technologies:
      solar_new:
        name: Solar PV
        type: Renewable
        fuel: Sun
        Availability: solar_availability.csv
        invest_cost: [800000.0]
        invest_max_power: [80.0]
        eff_at_rated: [1.0]
        degradation_rate: [0.005]
        lifetime: 25
        maintenance_cost: [3.0]

    battery_technologies:
      li_ion:
        name: Li-Ion Battery
        invest_cost_power: [250000.0]
        invest_cost_energy: [180000.0]
        invest_max_power: [50.0]
        invest_max_capacity: [200.0]
        min_duration_hours: 2.0
        max_duration_hours: 4.0
        efficiency_charge: [0.93]
        efficiency_discharge: [0.93]
        degradation_rate: [0.025]
        lifetime: 12

    penalties:
      loss_of_load: 10000.0
      curtailment_cost: 50.0
      max_curtailment_ratio: 0.08
      loss_of_reserve_static: 500.0
      fre_penetration_loss: 600.0

    TARGET_RE_PENETRATION: 0.70
    MAX_ANNUAL_SYSTEM_COST: 100000000.0
```

### How the Systems Are Combined

All systems listed in `meta_network.systems` are merged into one network before solving. Their nodes are numbered in order (here main_island nodes 0-2, small_island node 3), and there is one master problem and one operational dispatch for the whole network. Unit names get the system as a prefix (`main_island/Solar PV`).

System-level settings are combined as follows:

| Setting | Merged value |
|---------|--------------|
| `TARGET_RE_PENETRATION` | The lowest of the systems' targets, applied to the whole network |
| `MAX_ANNUAL_SYSTEM_COST` | Sum over the systems |
| `co2_budget` and `penalties.co2_budget_violation` | Kept per system: the dispatch adds one CO2 budget constraint per system, over that system's nodes (development-zone nodes included, by the bus they connect to), with its own budget and violation penalty |
| Other `penalties` | The highest value of each penalty across the systems |
| `discount_rate`, `stochastic_scenarios`, frequency and reserve settings | Taken from the first (primary) system |

So in this example the merged network is planned for a 70% RE share with an 8% discount rate. The main island's plants are held to its 900,000 t budget; the small island sets no `co2_budget`, so it gets the default (enabled, 1,000,000 t/year) for its own plants.

### Why Model Separate Systems?

Separate systems are useful when:

- Each system has its own demand file, network and candidate technologies
- They are connected by a limited, discrete interconnector (submarine cable)
- You want to analyze the value of interconnection vs. autarky

They are not separate optimizations: costs are minimized jointly, and the settings in the table above are shared.

### Inter-System Link Parameters

`systems_links` is a list. Each field is a list with one value per entry of `connections`.

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `connections` | `[[2, 0]]` | Node 2 of main_island to node 0 of small_island |
| `existing_capacity_MW` | 30 MW | Existing submarine cable capacity |
| `investment_cost_per_MW` | $800,000/MW | Expansion cost; set as the transmission investment cost of both end nodes |
| `max_investment_MW` | 100 MW | Maximum cable expansion; set as the transmission investment cap of both end nodes |
| `distance_km` | 80 km | Line length; the reactance is derived from it when `reactance_pu` is not given |
| `resistance_pu` | 0.03 | Series resistance, used for the line losses in the DC power flow |

Each link becomes an ordinary transmission line between the two nodes, in the master problem and in the dispatch. An expansion is costed like any corridor: once, at the higher of the two end nodes' `transference_invest_cost`.

A link can carry fuel instead of power. With `link_type: fuel_route` and a `fuel`, each connection becomes two one-way fuel transport routes between the two nodes (used when primary energy is modelled for that fuel), and no electrical line:

```yaml
meta_network:
  systems_links:
    - systems: [main_island, small_island]
      link_type: fuel_route
      fuel: Diesel
      connections: [[2, 0]]
      existing_capacity_MW: [200.0]   # route capacity, fuel units/day
      max_investment_MW: [0.0]        # not used: see fuel_infrastructure
      investment_cost_per_MW: [0.0]   # not used: see fuel_infrastructure
      loss_factor: [0.005]            # losses per 100 km
      distance_km: [80.0]
      cost_per_mw_km: [0.1]           # transport cost, $/unit/km
```

!!! note "Fields without effect"
    On a transmission link, `loss_factor` is required by the schema, and `meta_network.dynamic_transfer_pricing` and `inter_system_loss_segments` are accepted, but the current run does not use them. Losses on the link come from `resistance_pu` like on any other line. `cost_per_mw_km` is the link's flow cost: the master problem and the dispatch charge `distance_km` × `cost_per_mw_km` per MWh the link carries, in either direction (0 = free).

!!! note "Subsystems without lines"
    In the merged dispatch network, the link is a `transmission_lines_geo` entry, and the dispatch builds its network from lines. A subsystem with no `transmission_lines_geo` keeps its `nodes_connections`: each connected node pair becomes a line between the first buses of the two nodes, with the connection's capacity, the `fuel_transport_distances` entry as length (reactance from that length × `reactance_per_km` / Z_base, Z_base = `voltage_level_kv`² / `ac_power_flow.base_mva`, else 0.01) and R = 0.1 × X. A subsystem with lines uses only its lines.

---

## Step 2: Prepare Input Data

### Main Island Demand

Create `main_island_demand.csv` with 8,760 rows, 3 columns (one per node) and no header. Total peak demand should be approximately 500 MW distributed across the three nodes.

### Small Island Demand

Create `small_island_demand.csv` with 8,760 rows and 1 column. Peak demand is approximately 50 MW.

### Shared Availability Files

Both systems can use the same `solar_availability.csv` and `wind_availability.csv` if they are geographically close enough to share similar weather patterns.

---

## Step 3: Run the Simulation

```bash
esfex validate -c multi_system.yaml
esfex run -c multi_system.yaml --years 15 -v
```

The multi-system simulation takes longer because the merged network has more nodes and candidates. Expect 45-120 minutes.

---

## Step 4: How Multi-System Optimization Works

1. **Master Problem**: One master problem covers the merged network for all years. It decides:
    - How much generation and storage to build at each node of each system
    - Whether to expand the submarine cable
    - The timing of each investment

2. **Investment coordination**: The optimizer trades off between:
    - Building local generation on the Small Island (expensive: higher fuel costs, smaller scale)
    - Building more RE on the Main Island and exporting it over the cable (may require cable expansion)
    - A mix of both

3. **Operational dispatch**: Each year, one DC power flow dispatch covers both systems. Flows over the cable are decided hour by hour like flows on any other line.

---

## Step 5: Results Analysis

Results are written for the merged network, as for a single system: `results/multi_system_<ddmmyyyy>_<HHMM>.h5`, with node arrays over all 4 nodes. The file attributes `subsystem_names`, `subsystem_offsets` and `subsystem_node_counts` tell which nodes belong to which system.

```python
from pathlib import Path

import h5py
import numpy as np

h5_path = max(Path("results").glob("multi_system_*.h5"), key=lambda p: p.stat().st_mtime)
years = range(2025, 2040)

with h5py.File(h5_path, "r") as f:
    names = [str(n) for n in f.attrs["subsystem_names"]]
    offsets = list(f.attrs["subsystem_offsets"])
    counts = list(f.attrs["subsystem_node_counts"])
    systems = {n: range(o, o + c) for n, o, c in zip(names, offsets, counts)}
print(systems)   # {'main_island': range(0, 3), 'small_island': range(3, 4)}
```

### Per-System Generation and Investments

Generation datasets are named after the unit, with the system prefix (`/` is written as ` - `), so they can be split by system:

```python
with h5py.File(h5_path, "r") as f:
    grp = f["detailed_results/year_2034_threshold_0"]
    for sys_name in systems:
        total = sum(ds[:].sum() for name, ds in grp["generation"].items()
                    if sys_name in name)
        print(f"{sys_name}: {total / 1000:,.0f} GWh in 2034")

    # Master problem investments of each year (technology index, node index)
    for yr in years:
        g = f[f"detailed_results/year_{yr}_threshold_0"]
        inv = {k: float(v) for k, v in g.attrs.items() if k.startswith("investment_")}
        if inv:
            print(yr, inv)
```

Technologies are numbered across the merged system in system order (main_island's technologies first), and node indices are global.

### Inter-System Transfer Analysis

The net flow between systems is stored per year under `inter_system/detailed_results/year_<YYYY>_threshold_0/power_flow/<system>_to_<system>` (MW per hour):

```python
with h5py.File(h5_path, "r") as f:
    for yr in years:
        grp = f.get(f"inter_system/detailed_results/year_{yr}_threshold_0/power_flow")
        if grp is None:
            continue
        for pair, ds in grp.items():
            flow = ds[:]
            print(f"{yr} {pair}: avg {flow.mean():+.1f} MW, "
                  f"{flow.clip(min=0).sum():,.0f} MWh forward, "
                  f"{(-flow).clip(min=0).sum():,.0f} MWh back")
```

### Cable Expansion Decisions

The cable is the corridor between global nodes 2 and 3:

```python
with h5py.File(h5_path, "r") as f:
    total = 0.0
    for yr in years:
        attrs = f[f"detailed_results/year_{yr}_threshold_0"].attrs
        mw = attrs.get("investment_transfer_investment_2_3", 0.0) + \
             attrs.get("investment_transfer_investment_3_2", 0.0)
        if mw > 0.1:
            print(f"  {yr}: +{mw:.1f} MW")
            total += mw
    print(f"Cable expansion: {total:.1f} MW total")
```

### Comparative Cost Analysis

`summary_results/total_cost` is the operating cost of the whole merged network per year (from the dispatch). Investment cost is decided in the master problem and is not included.

```python
with h5py.File(h5_path, "r") as f:
    costs = f["summary_results/total_cost"][:]
print(f"Operating cost over the horizon: ${costs.sum():,.0f}")
```

### Value of Interconnection

To quantify the cable's value, run an isolated scenario with `existing_capacity_MW: [0.0]` and `max_investment_MW: [0.0]` (output to another directory with `-o isolated_results`), and compare:

```python
iso_path = max(Path("isolated_results").glob("multi_system_*.h5"), key=lambda p: p.stat().st_mtime)
with h5py.File(iso_path, "r") as f_iso, h5py.File(h5_path, "r") as f_linked:
    iso_cost = f_iso["summary_results/total_cost"][:].sum()
    linked_cost = f_linked["summary_results/total_cost"][:].sum()
savings = iso_cost - linked_cost
print(f"Operating-cost savings from interconnection: ${savings:,.0f} "
      f"({savings / iso_cost:.1%})")
```

This compares operating costs only; add each run's investment (from the `investment_*` attributes and the technology costs) for a full comparison.

Expected result: interconnection lowers total system cost by:

- Allowing the Small Island to import cheap RE instead of running expensive local diesel ($120/MWh)
- Reducing storage needs on the Small Island (imports provide flexibility)
- Enabling the Main Island to build slightly more RE than needed, exporting the surplus

---

## Key Takeaways

1. **One merged problem**: The systems are planned and dispatched as one network. Each keeps its own demand, nodes, candidates and CO2 budget, but the RE target, discount rate and penalties are combined as shown above.
2. **Inter-system coordination**: The master problem jointly optimizes investments across systems. It may invest more RE on the Main Island specifically to export to the Small Island.
3. **Transfer investment**: The optimizer expands the submarine cable when the cost of cable capacity ($800,000/MW) is less than the avoided cost of local generation and storage on the Small Island.
4. **Asymmetric benefit**: The Small Island typically benefits more from the interconnection because its local generation costs are higher.
5. **Losses matter**: Cable losses come from `resistance_pu` and grow with the square of the flow, so a heavily loaded cable delivers proportionally less.
6. **Resilience trade-off**: Heavy reliance on the cable makes the Small Island vulnerable to cable outages. Because the RE target applies to the merged network, it does not force local generation on the Small Island; use per-node `invest_max_power` limits or custom constraints for that.

---

## Next Steps

- [EV Integration](ev-integration.md) — add electric vehicles to either or both systems
- [Stochastic Planning](stochastic.md) — evaluate cable investment under demand uncertainty
- [Custom Scenarios](custom-scenarios.md) — compare different cable capacities and costs
