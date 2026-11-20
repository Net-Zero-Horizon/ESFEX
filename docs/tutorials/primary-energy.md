# Primary Energy

## Overview

The primary energy module models the fuel supply chain from import/production to consumption [**[49]**](../reference/bibliography.md#ref49). It is part of the operational dispatch, inside the same model as the hourly power dispatch: supply limits and shipments are set per primary-energy period (daily by default), and each node's fuel tank is balanced every hour.

The module captures:

- **Fuel supply** at import/production nodes with cost and annual availability limits
- **Fuel transport** between nodes along routes with capacity, losses and cost
- **Fuel storage**: one tank per fuel and node, with minimum and initial levels, from which generators and non-electric users draw every hour
- **Non-electric fuel demand** (transport, industry, households) that competes with power generation for the same fuel

---

## Scenario

An island imports diesel by tanker at a port node and receives natural gas by pipeline at the same node. Fuel is trucked inland to two other nodes. The system balances fuel import costs, storage and transport against electricity generation needs, while also serving non-electric fuel demand from transport and industry.

---

## Step 1: Enable Primary Energy

```yaml
enable_primary_energy: true
```

The flag defaults to `true`, but the fuel supply chain is only built for a system that defines `primary_energy_sources` (or, as the Studio writes them, `fuel_entry_points`). Without them, or with the flag set to `false`, generators pay their flat `fuel_cost` per MWh and fuel availability is unlimited.

---

## Step 2: Define Fuel Types

Each fuel has physical properties that determine its emission profile and energy equivalence. The fuel key must match the generators' `fuel` value and the key of its primary energy source:

```yaml
systems:
  island:
    fuels:
      Diesel:
        unit: ton
        emission_factor: 0.267     # t CO2 per MWh of fuel
        energy_content: 11.86      # MWh per ton
        price_base: 800.0          # $/ton (base procurement price)
        price_growth_rate: 0.0     # annual price escalation

      Gas:
        unit: ton
        emission_factor: 0.202
        energy_content: 13.89      # MWh per ton (LNG equivalent)
        price_base: 350.0
```

### Fuel Properties Reference

| Property | Units | Description |
|----------|-------|-------------|
| `unit` | string | Physical unit of the fuel (label only) |
| `emission_factor` | t CO2/MWh of fuel | Generator output divided by its `eff_at_rated`, times this factor, gives emissions and CO2 cost. Set to 0 for zero-emission fuels. |
| `energy_content` | MWh/unit | Converts fuel units to energy. Generators on a fuel with `energy_content: 0` are not coupled to the supply chain. |
| `price_base` | $/unit | Base procurement cost, escalated each year by `price_growth_rate`. The supply chain pays `price_base` plus the source's `import_cost`. |
| `price_growth_rate` | fraction/year | Annual price growth (default 0) |

A generator burns `output / (eff_at_rated × energy_content)` units of its fuel per MWh.

---

## Step 3: Configure Primary Energy Sources

Each source is keyed by the fuel it supplies. All per-node values are lists with one entry per node (node 0 first):

```yaml
    primary_energy_sources:
      Diesel:
        unit: ton
        max_availability: [60000.0, 10000.0, 0.0]   # ton/year that can enter at each node
        import_cost: [50.0, 150.0, 0.0]             # $/ton on top of price_base
        storage_capacity: [10000.0, 3000.0, 1000.0] # ton of tank capacity per node
        initial_storage_level: [0.5, 0.5, 0.5]      # fraction of capacity at the start
        min_storage_level: 0.1                      # safety stock, fraction of capacity

      Gas:
        unit: ton
        max_availability: [20000.0, 0.0, 0.0]
        import_cost: [50.0, 0.0, 0.0]
        storage_capacity: [2000.0, 0.0, 0.0]
        initial_storage_level: [0.5, 0.0, 0.0]
        min_storage_level: 0.1
```

### Source Properties Reference

| Property | Units | Description |
|----------|-------|-------------|
| `unit` | string | Physical unit (required) |
| `max_availability` | units/year, per node | Fuel that can enter the system at each node. Each primary-energy period gets its share of the year. 0 = no supply at that node |
| `import_cost` | $/unit, per node | Landed cost added to `price_base` (shipping, handling, duties) |
| `storage_capacity` | units, per node | Fuel tank capacity |
| `initial_storage_level` | fraction, per node | Starting inventory as a fraction of capacity |
| `min_storage_level` | fraction | Safety stock that must be kept (default 0.1) |
| `transport_transit_days_per_100km` | days | Optional delivery lead time along routes (default 0, instantaneous): a shipment reaches the destination tank that many days per 100 km after it leaves |
| `disruption_start_hour`, `disruption_end_hour`, `disruption_availability` | hour, hour, fraction | Optional supply cut: availability is scaled to `disruption_availability` inside the window. Hours count from 1 January of the base year (8760 per year), like [scheduled interruptions](../user-guide/scheduled-interruptions.md) |

In `development` mode the master problem can add tank capacity on top of `storage_capacity`, and route capacity, within the limits of `fuel_infrastructure` (Step 5); each dispatch year works with the configured capacity plus what the master built. A run without a master (`economic_dispatch`, `unit_commitment`) uses the configured capacities only.

!!! note "Fuel entry points"
    The Studio writes `fuel_entry_points` (a list of `{name, node, coordinate, fuels, max_import_rate, import_cost}`) plus storage facilities under `fuel_infrastructure` instead of `primary_energy_sources`. When a system has no `primary_energy_sources`, the run builds them from these entries (`max_import_rate` is in units/hour). When both are present, `fuel_entry_points` is ignored.

---

## Step 4: Configure Fuel Transport Routes

Routes move fuel between nodes. Each route is one-directional; add a second route for the way back:

```yaml
    fuel_transport_routes:
      - route_id: port_to_hub
        from_node: 0
        to_node: 1
        distance_km: 150.0
        fuel_params:
          Diesel:
            capacity: 200.0          # ton/day (truck fleet)
            transport_cost: 0.10     # $/ton/km
            losses_fraction: 0.005   # fraction lost per 100 km
          Gas:
            capacity: 100.0
            transport_cost: 0.05
            losses_fraction: 0.002

      - route_id: hub_to_east
        from_node: 1
        to_node: 2
        distance_km: 100.0
        fuel_params:
          Diesel:
            capacity: 100.0
            transport_cost: 0.12     # worse roads
            losses_fraction: 0.005
```

### Transport Properties Reference

| Property | Units | Description |
|----------|-------|-------------|
| `route_id` | string | Unique route name (`route_<index>` if omitted) |
| `from_node` | integer | Origin node index (0-based) |
| `to_node` | integer | Destination node index (0-based) |
| `distance_km` | km | Route distance (scales losses and cost) |
| `fuel_params.<fuel>.capacity` | units/day | Maximum daily throughput for that fuel |
| `fuel_params.<fuel>.transport_cost` | $/unit/km | Operating cost per unit per kilometer |
| `fuel_params.<fuel>.losses_fraction` | fraction per 100 km | Share lost per 100 km (evaporation, leakage, spillage) |

A fuel not listed in a route's `fuel_params` cannot use that route. A route without `from_node`, `to_node` or `distance_km` stops the primary-energy block from building; the run logs `Primary energy integration failed` with the route and field, and continues without the supply chain.

The fuel received at the destination is:

```
received = sent * (1 - losses_fraction * distance_km / 100)
```

For example, sending 100 tonnes of diesel over 150 km with `losses_fraction: 0.005`:

```
received = 100 * (1 - 0.005 * 150 / 100) = 100 * 0.9925 = 99.25 tonnes
```

---

## Step 5: Configure Fuel Storage Infrastructure

`fuel_infrastructure` holds per-facility parameters, keyed by facility or pipeline name, with per-fuel values under `fuel_params`. For each fuel, the first facility that lists it is used:

```yaml
    fuel_infrastructure:
      storage_facilities:
        port_tanks:
          fuel_params:
            Diesel:
              efficiency: 0.98         # storage efficiency (handling losses)
              expansion_limit: 2.0     # max new capacity = 2x existing
              investment_cost: 50.0    # $/ton of new capacity
            Gas:
              efficiency: 0.95
              expansion_limit: 1.5
              investment_cost: 200.0
      transport_pipelines: {}
```

`efficiency` (default 1, no loss, when a fuel has none) applies in every mode, to all fuel that enters a node's tank: what is bought there and what arrives by route. The investment fields (`investment_cost`, `expansion_limit`) are used by the master problem, which builds fuel storage and route capacity on its representative days in `simulation_mode: development`; each dispatch year then uses what was built as existing capacity. A run without a master (`economic_dispatch`, `unit_commitment`) builds none. `expansion_limit: 2.0` means new capacity may reach twice the existing capacity, so the total cannot exceed 3x the original. Built capacity does not retire within the horizon; the master charges it as annuities over `lifetime` (defaults 30 years for storage, 20 for routes).

---

## Step 6: Configure Non-Electric Fuel Demand

Non-electric demand captures fuel consumption outside the electricity sector: transport, industry, cooking and heating. The part of the key before the first underscore is the sector name:

```yaml
    non_electric_demand:
      transport_diesel:
        fuel: Diesel
        unit: ton
        demand: [12000, 6000, 3600]   # ton/year per node (integers)

      industrial_gas:
        fuel: Gas
        unit: ton
        demand: [7200, 0, 0]          # only at the port node
```

The demand grows by `non_electric_demand_growth[<fuel>]` per year from the first simulated year (no growth if not given) and is spread over the months by `seasonal_factors[<fuel>][<sector>]` (12 relative values; even if not given). Unmet demand costs `penalties.non_electric_demand_loss` per unit, per sector. The demand is fixed: it has no flexibility or criticality (those are settings of electric sectors, `electric_demand`).

Fuel shipped to a node goes into its tank: a period's shipment leaves the origin evenly over the period's hours and arrives after the route's transit time, less transport losses. Generators and non-electric users at the destination draw from that tank like from locally supplied fuel. Use that the tank cannot cover is a fuel shortfall, priced at `penalties.loss_of_fuel_supply`.

---

## Complete YAML Example

```yaml
simulation_mode: development
date_start: "01/01/2025 00:00"
enable_primary_energy: true

temporal:
  resolution_hours: 1
  use_rolling_horizon: true
  rolling_horizon_hours: 48
  overlap_hours: 24
  primary_energy_resolution: 24     # daily fuel periods (fewer variables)

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
    demand_path: demand.csv           # 3 columns, one per node
    demand_scale: 1.0
    discount_rate: 0.05
    TARGET_RE_PENETRATION: 0.60
    max_annual_investment: 500000000.0

    nodes:
      nodes_connections: [0.0, 100.0, 75.0,
                          100.0, 0.0, 50.0,
                          75.0, 50.0, 0.0]     # line capacities (MW)
      node_coordinates:
        - {latitude: 23.13, longitude: -82.38}
        - {latitude: 22.40, longitude: -81.95}
        - {latitude: 22.07, longitude: -80.45}
      node_names: [Port_City, Inland_Hub, Eastern_Town]

    fuels:
      Diesel:
        unit: ton
        emission_factor: 0.267
        energy_content: 11.86
        price_base: 800.0
      Gas:
        unit: ton
        emission_factor: 0.202
        energy_content: 13.89
        price_base: 350.0

    primary_energy_sources:
      Diesel:
        unit: ton
        max_availability: [60000.0, 10000.0, 0.0]
        import_cost: [50.0, 150.0, 0.0]
        storage_capacity: [10000.0, 3000.0, 1000.0]
        initial_storage_level: [0.5, 0.5, 0.5]
        min_storage_level: 0.1
      Gas:
        unit: ton
        max_availability: [20000.0, 0.0, 0.0]
        import_cost: [50.0, 0.0, 0.0]
        storage_capacity: [2000.0, 0.0, 0.0]
        initial_storage_level: [0.5, 0.0, 0.0]
        min_storage_level: 0.1

    fuel_transport_routes:
      - route_id: port_to_hub
        from_node: 0
        to_node: 1
        distance_km: 150.0
        fuel_params:
          Diesel: {capacity: 200.0, transport_cost: 0.10, losses_fraction: 0.005}
          Gas: {capacity: 100.0, transport_cost: 0.05, losses_fraction: 0.002}
      - route_id: hub_to_east
        from_node: 1
        to_node: 2
        distance_km: 100.0
        fuel_params:
          Diesel: {capacity: 100.0, transport_cost: 0.12, losses_fraction: 0.005}

    fuel_infrastructure:
      storage_facilities:
        port_tanks:
          fuel_params:
            Diesel: {efficiency: 0.98, expansion_limit: 2.0, investment_cost: 50.0}
            Gas: {efficiency: 0.95, expansion_limit: 1.5, investment_cost: 200.0}
      transport_pipelines: {}

    non_electric_demand:
      transport_diesel:
        fuel: Diesel
        unit: ton
        demand: [12000, 6000, 3600]
      industrial_gas:
        fuel: Gas
        unit: ton
        demand: [7200, 0, 0]

    generators:
      diesel_gen:
        name: Diesel Generator
        type: Non-renewable
        fuel: Diesel
        rated_power: [30.0, 20.0, 40.0]
        min_power: [0.3, 0.3, 0.3]
        min_up: [0, 0, 0]
        min_down: [0, 0, 0]
        ramp_up: [1.0, 1.0, 1.0]
        ramp_down: [1.0, 1.0, 1.0]
        eff_at_rated: [0.38, 0.38, 0.38]
        eff_at_min: [0.30, 0.30, 0.30]
        inertia: [2.0, 2.0, 2.0]
        start_up_cost: [0.0, 0.0, 0.0]
        fuel_cost: [0.0, 0.0, 0.0]        # computed from price_base by the loader
        fixed_cost: [25000.0, 25000.0, 25000.0]  # $/MW per year
        maintenance_cost: [3.0, 3.0, 3.0]
        invest_cost: [0.0, 0.0, 0.0]
        life_time: [30, 30, 30]
        initial_age: [10, 10, 10]
        degradation_rate: [0.0, 0.0, 0.0]
        decommissioning_cost: [0.0, 0.0, 0.0]

      gas_turbine:
        name: Gas Turbine
        type: Non-renewable
        fuel: Gas
        rated_power: [25.0, 0.0, 0.0]
        min_power: [0.2, 0.0, 0.0]
        min_up: [0, 0, 0]
        min_down: [0, 0, 0]
        ramp_up: [1.0, 1.0, 1.0]
        ramp_down: [1.0, 1.0, 1.0]
        eff_at_rated: [0.42, 0.42, 0.42]
        eff_at_min: [0.32, 0.32, 0.32]
        inertia: [3.0, 3.0, 3.0]
        start_up_cost: [0.0, 0.0, 0.0]
        fuel_cost: [0.0, 0.0, 0.0]
        fixed_cost: [22000.0, 22000.0, 22000.0]  # $/MW per year
        maintenance_cost: [2.5, 2.5, 2.5]
        invest_cost: [0.0, 0.0, 0.0]
        life_time: [25, 25, 25]
        initial_age: [5, 5, 5]
        degradation_rate: [0.0, 0.0, 0.0]
        decommissioning_cost: [0.0, 0.0, 0.0]

    technologies:
      solar_pv:
        name: Solar PV
        type: Renewable
        fuel: Sun
        invest_cost: [700000.0, 700000.0, 700000.0]
        invest_max_power: [200.0, 100.0, 100.0]
        Availability: solar_profile.csv
        eff_at_rated: [1.0, 1.0, 1.0]
        degradation_rate: [0.005, 0.005, 0.005]
        lifetime: 25

    battery_technologies:
      li_ion:
        name: Li-Ion
        invest_cost_power: [180000.0, 180000.0, 180000.0]
        invest_cost_energy: [120000.0, 120000.0, 120000.0]
        invest_max_power: [100.0, 50.0, 50.0]
        invest_max_capacity: [400.0, 200.0, 200.0]
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
      loss_of_fuel_supply: 100.0        # a shortfall costs at least 10 x the fuel bought
```

A unit of fuel missing at a node costs `loss_of_fuel_supply`, or ten times the fuel's dearest unit bought (`price_base` in the year plus the largest `import_cost`) when that is higher, so a shortfall is never cheaper than buying the fuel. Set `loss_of_fuel_supply` higher to make shortfalls dearer still; `loss_of_fuel_supply` is also the price of excess supply.

---

## Running the Optimization

```bash
esfex run -c fuel_system.yaml --years 10 -v
```

With primary energy active, every dispatch window gets supply and shipment variables for each primary-energy period and an hourly tank balance per fuel and node, coupled to the generators' hourly fuel burn. `primary_energy_resolution: 24` sets the supply limits and shipments daily while the tanks and the power dispatch stay hourly. Tank levels are carried from one window to the next.

---

## Interpreting Results

Results are written to `results/<config_stem>_<ddmmyyyy>_<HHMM>.h5` (or the `--output` folder). Primary-energy results for each year are under `detailed_results/year_<YYYY>_threshold_0/primary_energy/`:

| Path | Content |
|------|---------|
| `total_fuel_supply/<fuel>` | Fuel bought, all nodes, per period of `primary_energy_resolution` hours from the start of the year (`.sum()` gives the yearly total) |
| `total_ne_demand_satisfied/<fuel>` | Non-electric demand served (same layout) |
| `total_loss_of_supply/<fuel>` | Fuel demand not supplied (same layout) |
| `final_storage_levels/<fuel>` | Storage level per node at the end of the year |
| `transport_flows/<fuel>` | Flow per route `[routes x periods]`, same periods |
| attrs `total_fuel_cost`, `total_transport_cost`, `total_loss_penalty` | Yearly totals, in $ |

With a rolling horizon each window reports its own periods; the year keeps only each window's committed steps (not the overlap with the next window), like the dispatch arrays, and the costs add up those steps (a period's supply counts for the share of its steps that the window keeps).

### Fuel Supply and Storage

```python
import glob
import h5py

path = sorted(glob.glob("results/fuel_system_*.h5"))[-1]   # newest run

with h5py.File(path, "r") as f:
    for name in sorted(f["detailed_results"]):
        pe_path = f"detailed_results/{name}/primary_energy"
        if pe_path not in f:
            continue
        pe = f[pe_path]
        print(name)
        for fuel, ds in pe["total_fuel_supply"].items():
            print(f"  {fuel}: {ds[:].sum():,.0f} t supplied")
        for fuel, ds in pe["final_storage_levels"].items():
            print(f"  {fuel} end-of-year storage per node: {ds[:].round(0)}")
        print(f"  fuel cost: {pe.attrs.get('total_fuel_cost', 0.0):,.0f} $, "
              f"transport cost: {pe.attrs.get('total_transport_cost', 0.0):,.0f} $")
```

### Emissions and Total Cost

```python
with h5py.File(path, "r") as f:
    summary = f["summary_results"]
    for year, cost, co2 in zip(summary["year"][:], summary["total_cost"][:],
                               summary["co2_emissions"][:]):
        print(f"{year}: cost {cost:,.0f}, CO2 {co2:,.0f} t")
```

---

## Tips for Calibrating Fuel Parameters

1. **Start simple**: Begin with a single fuel (diesel) and check that the model runs before adding natural gas, LNG or hydrogen.

2. **Check storage dynamics**: Storage levels should stay between `min_storage_level` and capacity. If `total_loss_of_supply` is non-zero, increase `max_availability`, `storage_capacity` or route capacity.

3. **Calibrate transport costs**: Transport costs affect which node's generators run. Too-cheap transport makes all generation draw on the cheapest fuel node; too-expensive transport pushes generation to local fuel.

4. **Penalties**: A fuel shortfall costs at least ten times the fuel's price plus import cost, whatever `loss_of_fuel_supply` says; unmet non-electric demand costs `non_electric_demand_loss`.

5. **Emission factors**: `emission_factor` is per MWh of fuel energy (IPCC-style values such as 0.202 for natural gas); the dispatch divides generator output by its `eff_at_rated`.

6. **Temporal resolution**: `primary_energy_resolution` sets the period length for supply limits and shipments. Values of 24 (daily) or 168 (weekly) are typical. Shorter periods are more detailed but slower.

7. **Fuel names**: The same name must appear in `fuels`, as the `primary_energy_sources` key, in each route's `fuel_params`, and as the generators' `fuel`.

---

## Key Takeaways

1. **Supply chain costs matter**: Fuel transport and storage add to generation costs beyond the base fuel price, especially for island systems.
2. **Strategic reserves**: `min_storage_level` models safety stock. Set too high, it ties up tank capacity; set too low, it risks shortfalls during demand peaks.
3. **Import constraints**: Limited fuel availability or transport capacity shows up as unsupplied fuel and higher dispatch costs, which makes the value of renewables visible.
4. **Non-electric competition**: Transport and industrial fuel demand competes with power generation for the same supply. This competition is invisible in models without primary energy.
5. **RE value amplified** [**[48]**](../reference/bibliography.md#ref48): Renewable energy reduces fuel dependence, with cost savings (avoided fuel procurement), security benefits (reduced import exposure) and lower emissions.
