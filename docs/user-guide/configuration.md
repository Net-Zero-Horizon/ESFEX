# Configuration Guide

An ESFEX run is described by one YAML file. `esfex.config.loader.load_config()` reads it, converts each system, and validates the result against the Pydantic v2 models in `esfex.config.schema`. The CLI (`esfex run`, `esfex validate`) and the Studio both go through this loader. This page explains the file section by section; the [Configuration Reference](../reference/config-reference.md) lists every field.

The configuration drives a two-stage decomposition [**[25]**](../reference/bibliography.md#ref25): capacity expansion in the master problem [**[24]**](../reference/bibliography.md#ref24), then operational dispatch over a rolling horizon.

!!! warning "Unknown keys are ignored, with a warning"
    **Unknown keys are not errors.** A typo such as `min_up_time` (instead of `min_up`) or an obsolete key such as `demand_growth` has no effect. The loader logs a warning for each one with its full path (for example `Unknown configuration key systems.island.generators.diesel.min_up_time is ignored`), `esfex validate -c <file>` lists them, and so does the Studio's validation (category *Configuration*).

    Keys the loader translates are not reported: upper-case aliases (`TARGET_RE_PENETRATION`), flat `dc_*` / `DC_*` power-flow keys, penalty keys in any case, legacy `unit_*` / `bat_*` units, the Studio's `_gui_layout`, `_gui_styles`, `_technologies`, `_equipment_buses`, `visual_scaling` and `_geo_assets`, and the free-form `plugins` and `solver.options`. Stochastic scenario multipliers report their own unknown keys.

---

## Top-Level Structure

| Key | Purpose | Required |
|---|---|---|
| `simulation_mode` | `development`, `economic_dispatch` or `unit_commitment` | no (default `development`) |
| `unit_commitment_hours` | Hours dispatched in `unit_commitment` mode | no (24) |
| `date_start` | Start timestamp `DD/MM/YYYY HH:MM` | no |
| `temporal` | Time step and rolling horizon | no |
| `solver` | Solver backend and options | no |
| `n1_security` | N-1 contingency constraints | no |
| `master_problem` | Capacity-expansion settings | no |
| `network_reduction` | Internal bus-network reduction | no |
| `enable_primary_energy` | Include the fuel supply chain (default `true`), in the dispatch and in the master problem, which then builds fuel storage, routes and electrolyzers and charges fuel-coupled units the fuel price | no |
| `meta_network` | Systems to simulate and links between them | **yes** |
| `systems` | One configuration per system, inline or as a file path | **yes** |
| `risk` | Risk and resilience module | no |
| `logging` | `console_level: basic \| verbose \| debug` | no |
| `plugins` | Settings per plugin | no |

A minimal skeleton:

```yaml
simulation_mode: development
date_start: "01/01/2025 00:00"

meta_network:
  systems: [island]          # each name must exist under systems

systems:
  island: island_system.yaml # or an inline mapping, see Complete Example
```

### Simulation Modes

| Mode | Master problem | Dispatch | Horizon |
|---|---|---|---|
| `development` (default) | yes | LP (economic dispatch), or UC if `master_problem.use_uc_in_dispatch: true` | all simulated years |
| `economic_dispatch` | no | LP | all simulated years |
| `unit_commitment` | no | MIP with binary commitment, start-up costs and minimum up/down times | first `unit_commitment_hours` hours of the first year only |

In `development` mode the master problem decides investments and retirements for every year first; each year is then dispatched with those decisions fixed. The two dispatch-only modes use the existing fleet as configured.

`unit_commitment_hours` is not a window size: in `unit_commitment` mode the run is cut to one year and to the first `unit_commitment_hours` hours of it (at most 8,760).

`date_start` labels the hourly time index of the results, and its year is the first year label (`esfex run --start-year` overrides it). Demand is always read from the first row of the demand file. Every modelled year has 8,760 hours, leap years included: Feb 29 is skipped in the labels and dropped from leap-year data (see [Demand Data](demand-data.md#leap-years)).

---

## Temporal Configuration

```yaml
temporal:
  resolution_hours: 1             # operational time step (hours)
  use_rolling_horizon: true       # split each year into windows
  rolling_horizon_hours: 48       # window length
  overlap_hours: 24               # look-ahead appended to each window (< window)
  investment_resolution: 8760     # hours per investment period (8760 = yearly)
  primary_energy_resolution: 24   # fuel-model time step (hours)
```

With the rolling horizon on, each year is solved as a sequence of windows. The overlap gives each window a look-ahead; only the non-overlapping part is kept, and storage state of charge and generator output at the end of that part carry over to the next window. Stored energy has no value at the end of a window, so a window charges storage only for needs it can see: make the overlap long enough to reach the next demand peak (the default 24 h covers a daily cycle), or storage may be empty when a peak arrives just after a window boundary; ESFEX warns when the overlap is shorter than 24 h and the system has storage. The last window of each year must bring batteries back to at least their year-start SOC (soft, within `soc_end_tolerance`), so a year cannot spend its starting stored energy for free. The overlap must be shorter than the window: an explicit `overlap_hours >= rolling_horizon_hours` is rejected, and the default shrinks to half the window when the window is 24 h or shorter. With `use_rolling_horizon: false` each year is one model.

`investment_resolution` groups years into investment periods: `8760` allows investment every year, `17520` every two years (years per period = `max(1, investment_resolution ÷ 8760)`).

There are no `battery_soc_resolution`, `ev_resolution` or `reserve_resolution` keys.

---

## Solver Configuration

```yaml
solver:
  name: highs        # highs, cbc, glpk, gurobi, cplex, scip, xpress, clarabel, scs, ipopt
  threads: 4
  time_limit: 10800  # seconds per solve, 0 = unlimited
  gap: 0.01          # relative MIP gap
  verbose: false
  options: {}        # solver-specific options
```

`options` is passed to the JuMP optimizer. For HiGHS, the key `solver_method` selects the LP algorithm (`choose`, `simplex`, `ipm`, `ipx`, `hipo`, `pdlp`). Options that apply to one algorithm only are forwarded only when `solver_method` is set explicitly to that algorithm; otherwise they are dropped with a warning. For example, `primal_feasibility_tolerance`, `dual_feasibility_tolerance` and `simplex_scale_strategy` need `solver_method: simplex`, while `ipm_optimality_tolerance` and `run_crossover` need an interior-point method:

```yaml
solver:
  name: highs
  threads: 4
  options:
    presolve: "on"
    solver_method: ipm
    run_crossover: "on"
    ipm_optimality_tolerance: 1.0e-8
```

The conic (`clarabel`, `scs`) and NLP (`ipopt`) solvers serve the AC-OPF modes of `power_flow_mode` that need them: `acopf_soc`, `acopf_qc` and `acopf_sdp` need a conic solver (HiGHS has no second-order cones), `acopf_polar` and `acopf_rect` need Ipopt. See the [Solver Guide](solver-guide.md) for tuning.

---

## N-1 Security

```yaml
n1_security:
  enabled: false                          # master switch
  apply_to_modes: [development, unit_commitment]
  transmission_enabled: true              # line contingencies
  transmission_reserve_factor: 0.70       # pre-contingency loading limit (fraction of rating)
  generation_enabled: true                # generator contingencies
  generation_reserve_type: largest_unit   # or: percentage
  generation_reserve_percentage: 0.15     # used with generation_reserve_type: percentage
```

The values shown are the defaults. Further fields select iterative SCOPF (`scopf_enabled`), corrective actions, N-1-1 depth, screening and transformer/battery contingencies; see [N1SecurityConfig](../reference/config-reference.md#n1securityconfig).

---

## Master Problem

```yaml
master_problem:
  planning_mode: perfect_foresight   # or: myopic (year by year)
  solver_method: monolithic          # or: benders
  representative_days: 5             # days per year used to check operation
  min_day_separation: 5              # minimum days between representative days
  use_tsam: false                    # TSAM clustering instead of representative days
  use_uc_in_dispatch: false          # unit commitment in development-mode dispatch
  stochastic: false                  # needs stochastic_scenarios in the system

  # MGA / SPORES: near-optimal alternative exploration
  mga:
    enabled: false
    method: mga                      # "mga" (HSJ loop) | "spores" (one run per objective)
    num_alternatives: 10             # MGA only
    objectives: []                   # SPORES only; must be empty under MGA
    slack_fraction: 0.05             # cost slack (0.05 = 5 % above optimal)
    investment_threshold: 0.1        # MW counted as "invested" for HSJ scoring
```

These are the defaults. Planning modes, Benders, TSAM and the RE trajectory are explained in [Master Problem](master-problem.md).

ESFEX offers two near-optimal exploration methods. Both share the cost-slack envelope $Z \leq (1+\varepsilon) C^*$ but differ in how alternatives are produced:

- **`method: mga`** (default) runs the Hop-Skip-Jump (HSJ) loop `num_alternatives` times. Each iteration maximises a diversity objective that penalises investment variables used in earlier alternatives.
- **`method: spores`** solves one alternative per declared objective (see [SporesObjective](../api/config-schema.md#sporesobjective)). The alternative count equals `len(objectives)`; `num_alternatives` is ignored.

```yaml
master_problem:
  mga:
    enabled: true
    method: spores
    objectives:
      - min_total_build      # smallest near-optimal portfolio
      - max_tech_equity      # technology-diversified plan
      - max_regional_equity  # spatially-spread plan
      - evolutionary_dist    # maximally different from cost-optimal
    slack_fraction: 0.05
```

With `enabled: true`, `method: spores` and an empty `objectives` list fails validation, and so does `method: mga` with a non-empty list. Results are exported under `/mga/` in the HDF5 file with `/mga.attrs["method"]` and a per-alternative `objective` tag (see [HDF5 Output Schema § MGA Results](../reference/hdf5-output-schema.md#mgaspores-results)). Alternative 0 (cost-optimal) is used for operational dispatch.

---

## System Configuration

Each entry under `systems` is one power system. The examples below show fragments of a system mapping.

### Demand

```yaml
systems:
  island:
    name: island
    demand_path: demand/island_demand.csv   # one column per node, one row per hour
    demand_scale: 1.0                       # multiplier on all demand
```

`demand_paths` (a list with one single-column file per node) overrides `demand_path`. Relative paths are resolved against the configuration file's folder first, then the working directory. The file is read when the run starts, not by `esfex validate`.

Every simulated year is 8,760 rows. If the file covers fewer years than the horizon, the first year is repeated; to model growth, provide a multi-year file. There is no `demand_growth` key (it is ignored); the master problem and the dispatch both use the file values. File formats are described in [Demand Data](demand-data.md).

### Nodes and buses

ESFEX separates geography from electrical topology:

- A **node** is a geographic aggregation (a region or substation area). Demand files, per-node equipment arrays (`rated_power: [...]`), reserves and the master problem's regional view are indexed by node.
- A **bus** is an electrical connection point inside a node. The operational power flow runs on buses, and equipment attaches to buses. Every node has at least one bus, so there are always at least as many buses as nodes.

**Nodes.** `nodes_connections` is the node adjacency matrix, flattened row by row; entry `(i, j)` is the transfer capacity in MW, `0` means no direct connection. The node count is inferred from its length (√N²) unless `num_nodes` is given.

```yaml
    nodes:
      nodes_connections: [0.0, 80.0,
                          80.0, 0.0]     # 2 nodes, 80 MW between them
      node_coordinates:                  # one {latitude, longitude} per node
        - {latitude: 21.5, longitude: -80.0}
        - {latitude: 21.0, longitude: -80.0}
      node_names: [North, South]
```

Empty per-node lists such as `reserve_static` or `losses` are filled with zeros.

**Buses.** If `buses` is omitted, one bus per node is created at load time (`bus_<i>`, role `load`, `demand_fraction: 1.0`), so `num_buses` equals `num_nodes`. List buses explicitly when a node has several connection points:

```yaml
    buses:
      - {bus_id: bus_0, name: North 110 kV, parent_node: 0, voltage_kv: 110.0, role: load, demand_fraction: 0.6}
      - {bus_id: bus_1, name: North feeder, parent_node: 0, voltage_kv: 110.0, role: load, demand_fraction: 0.4}
      - {bus_id: bus_2, name: North plant,  parent_node: 0, voltage_kv: 110.0, role: connection, demand_fraction: 0.0}
      - {bus_id: bus_3, name: South 110 kV, parent_node: 1, voltage_kv: 110.0, role: load, demand_fraction: 1.0}

    transmission_lines_geo:                   # bus indices are positions in the buses list (0-based)
      - {line_id: l_plant,  from_node: 0, to_node: 0, from_bus: 2, to_bus: 0, capacity_mw: 100.0}
      - {line_id: l_feeder, from_node: 0, to_node: 0, from_bus: 0, to_bus: 1, capacity_mw: 60.0}
      - {line_id: l_ns,     from_node: 0, to_node: 1, from_bus: 0, to_bus: 3, capacity_mw: 80.0, length_km: 56.0}

    generators:
      north_plant:                            # other generator fields omitted
        bus_id_per_node: {0: bus_2}           # node 0 capacity sits on the plant bus
```

Each bus has a `role`:

| Role | Demand | Load shedding | Typical use |
|---|---|---|---|
| `load` (default) | `node demand × demand_fraction` | up to the bus demand | substation feeding load |
| `mixed` | `node demand × demand_fraction` | up to the bus demand | load and generation at the same bus |
| `connection` | none (`demand_fraction` must be `0`) | none | plant, transformer or junction bus |

How demand and equipment are placed:

- **Demand is split per bus.** Each hour, every `load`/`mixed` bus takes `demand_fraction` of its parent node's demand. The fractions of a node's buses must sum to 1; if they don't, they are rescaled at load time with a warning (two `load` buses left at the default `1.0` become `0.5` each). A node whose buses all have `demand_fraction: 0` gets no demand at all, and the loader warns about it.
- **Load shedding is bounded by the bus demand.** Unserved demand at a bus can never exceed the demand on that bus, so it cannot act as a virtual generator.
- **Equipment attaches to buses.** Generator and battery arrays stay per node. Each node's capacity goes to, in order of priority: the bus named in the unit's `bus_id_per_node` (`{node_index: bus_id}`); the bus at the other end of a `transmission_lines_geo` entry whose endpoint is that unit; the bus given by `bus_index` (0-based) if that bus belongs to the node; otherwise the node's first bus in list order. Per-node properties such as efficiencies, ramp rates and costs are copied to every bus of the node.
- When the bus list is exactly one bus per node in node order, no mapping is needed and per-node arrays are used directly.
- A node with no bus cannot serve its demand or host capacity. When there are fewer buses than nodes, the bridge logs a warning naming the bus-less nodes.

**Lines.** A `transmission_lines_geo` entry enters the operational model only if both bus indices are known, through `from_bus`/`to_bus` or a bus endpoint reference (`from_endpoint_type: bus`, `from_endpoint_id: <bus_id>`). A line with only `from_node`/`to_node` is skipped with a warning. When any lines are listed, the bus network is built from them alone and `nodes_connections` does not add operational capacity; without lines and with one bus per node, `nodes_connections` is the network. Missing line reactance is derived from `length_km × dc_power_flow.reactance_per_km / Z_base`, with `Z_base = V² / ac_power_flow.base_mva` and `V` the line's `voltage_kv` (else `dc_power_flow.voltage_level_kv`); 0.01 p.u. if there is no length. Per-unit values are on the `ac_power_flow.base_mva` power base, which the DC flow and losses use too.

Bus `bus_id` values must be unique within a system. Transformers, AC/DC converters and frequency converters also take `from_bus`/`to_bus`; see the [reference](../reference/config-reference.md#transformerconfig).

### Generators

Existing units go under `generators`, keyed by a unit id. Every list has one entry per node; a unit present at only some nodes has zeros elsewhere.

```yaml
    generators:
      diesel:
        name: Diesel
        type: Non-renewable          # Renewable | Non-renewable | Electrolyzer
        fuel: Diesel                 # key in fuels (Sun / Wind / Water for renewables)
        rated_power: [0.0, 60.0]     # MW per node
        min_power: [0.0, 0.3]        # fraction of rated (unit commitment)
        min_up: [0, 2]               # hours (unit commitment)
        min_down: [0, 2]             # hours (unit commitment)
        ramp_up: [1.0, 0.5]          # fraction of capacity per hour (0 = no limit)
        ramp_down: [1.0, 0.5]
        eff_at_rated: [0.0, 0.35]
        eff_at_min: [0.0, 0.25]
        inertia: [0.0, 2.0]          # H (s)
        start_up_cost: [0.0, 500.0]  # $ per start
        fuel_cost: [0.0, 0.0]        # $/MWh; zero -> computed from fuels (see below)
        fixed_cost: [0.0, 0.0]       # $/MWh
        maintenance_cost: [0.0, 5.0] # $/MWh
        decommissioning_cost: [0.0, 20000.0]  # $/MW
        invest_cost: [0.0, 900000.0] # $/MW, prices life extension
        invest_max_power: [0.0, 0.0]
        life_time: [0, 25]           # years
        initial_age: [0, 10]         # years
        degradation_rate: [0.0, 0.005]  # fraction/year
        bus_id_per_node: {1: bus_3}  # bus of the South node in the example above
```

**Required keys.** `type`, `life_time`, `initial_age`, `degradation_rate`, `decommissioning_cost`, `rated_power`, `min_power`, `min_up`, `min_down`, `ramp_up`, `ramp_down`, `eff_at_rated`, `eff_at_min`, `inertia`, `start_up_cost`, `fuel_cost`, `fixed_cost`, `maintenance_cost`, `invest_cost` and `invest_max_power`. A missing one aborts loading with `Failed to load configuration: '<key>'`. `fuel` defaults to `"None"`, `name` to the unit key. Optional keys: `technology`, `reservable`, `Availability` (profile file, see [Availability Profiles](availability-profiles.md)), `frequency_hz`, `current_type`, `bus_id_per_node`, `bus_index` and the reservoir fields below.

**Units.** `fixed_cost` and `maintenance_cost` are charged per MWh generated. Ramp limits are fractions of capacity per hour; with `resolution_hours` > 1 the allowed change per step is `ramp × resolution_hours` (at most the full capacity). `decommissioning_cost` and `invest_cost` are per MW. Investment in new capacity is modelled with `technologies`; a generator's `invest_cost` is used to price life extension.

**Type.** Use `Renewable`, `Non-renewable` or `Electrolyzer`. Case and surrounding spaces are ignored (`renewable`, `non-renewable`), and the labels saved by a translated Studio (`Renovable`, `No renovable`, `Renouvelable`, ...) are read as their English value. Any other value, such as `Thermal`, is logged as a warning and treated as `Non-renewable`.

**Fuel cost and the non-fuel O&M adder.** When the system has a `fuels` block, the loader adjusts every non-renewable generator whose `fuel` names an entry of that block:

1. If `fuel_cost` is missing or all zero, it is computed per node as `price_base / energy_content / eff_at_rated` (the linked technology's `eff_at_rated` is used where the unit's is 0). Nodes with zero efficiency get 0.
2. For fuels named exactly `Gas`, `Diesel` (60 $/MWh), `Oil`, `Fuel_oil` (55 $/MWh) or `Biomass` (20 $/MWh), a fixed non-fuel cost is **added to `maintenance_cost`** at every node with a positive fuel cost. It is skipped when the unit's largest `maintenance_cost` is already at least half the adder.

In the example above, with `Diesel: {energy_content: 10.0, price_base: 800.0}`, node 1 gets `fuel_cost` ≈ 228.6 $/MWh and `maintenance_cost` 5 + 60 = 65 $/MWh. To keep your own O&M figure, set `maintenance_cost` to at least half the adder (30 $/MWh for diesel). Each unit that receives the adder is named in the run log (INFO level, so in the log file and in the `verbose` console). A non-renewable unit whose fuel is missing from `fuels` and whose fuel cost is zero is reported in a warning, because it would be dispatched for free with zero emissions.

**Optional fields.** Other generator schema fields, such as `fuel_cost_curve`, `droop`, `governor_time_const`, `q_max_mvar`, `q_min_mvar`, `power_factor` and `risk_coefficient`, are read when present and take their schema defaults otherwise.

#### Age, lifetime and retirement

A unit's age in planning year `y` (1-based) is `initial_age + y − 1`. Once the age reaches `life_time`, the master problem can keep the capacity only through a paid life extension, costing `invest_cost × life_extension_cost_factor` per MW per year; otherwise the capacity retires. Setting `initial_age: [20]` with `life_time: [25]` makes the unit reach end of life in year 6. See [Master Problem](master-problem.md).

#### Reservoir hydro

A generator with `reservoir_capacity` is dispatched against stored water energy:

```yaml
    generators:
      hydro:                                # other generator fields omitted
        reservoir_capacity: [0.0, 5000.0]   # MWh
        reservoir_initial_level: [0.0, 0.6] # fraction
        reservoir_min_level: [0.0, 0.1]
        reservoir_max_level: [0.0, 1.0]
        reservoir_turbine_efficiency: [0.0, 0.9]
        reservoir_inflow_file: data/inflow.csv
        reservoir_spillage_allowed: true
```

Also read: `reservoir_evaporation_rate`, `reservoir_pump_capacity`, `reservoir_pump_efficiency`, `reservoir_invest_cost`, `reservoir_invest_max`, and the minimum-release, cascade and head-dependence fields (`reservoir_min_release`, `cascade_downstream`, `cascade_delay_hours`, `reservoir_head_min_factor`). See [Reservoir Hydro](../formulation/reservoir-hydro.md).

### Technologies (Investment Candidates)

Technologies are what the master problem can build. Each is passed to the schema unchanged, so every [TechnologyConfig](../reference/config-reference.md#technologyconfig) field is read.

```yaml
    technologies:
      solar_new:
        name: Solar PV (new)
        type: Renewable                  # Renewable | Non-renewable
        fuel: Sun
        invest_cost: [700000.0, 700000.0]  # $/MW per node
        invest_max_power: [50.0, 50.0]     # MW per node
        Availability: availability/solar.csv
        eff_at_rated: [1.0, 1.0]
        degradation_rate: [0.005, 0.005]
        lifetime: 25                       # years, a single integer
        fixed_cost: [0.0, 0.0]             # $/MWh
        maintenance_cost: [2.0, 2.0]       # $/MWh
```

Required: `type`, `fuel`, `invest_cost`, `invest_max_power`, `eff_at_rated`, `degradation_rate`, `lifetime`. Capacity built by the master problem becomes a virtual generator in operational dispatch.

### Cost curves

A per-node offer curve can replace the flat `fuel_cost` of a technology:

```yaml
    technologies:
      gas_new:                           # other technology fields omitted
        fuel_cost_curve:
          - curve_type: stepwise         # node 0
            blocks:
              - {fraction: 0.4, price: 45.0}
              - {fraction: 0.3, price: 55.0}
              - {fraction: 0.3, price: 75.0}
          - curve_type: linear           # node 1
            price_at_zero: 40.0
            price_at_max: 80.0
```

| `curve_type` | Parameters | Discretisation |
|---|---|---|
| `flat` | `flat_price` | one block |
| `stepwise` | `blocks` of `{fraction, price}` | as given; fractions are rescaled if they do not sum to 1 |
| `linear` | `price_at_zero`, `price_at_max`, `num_segments` | equal blocks priced at their mid-point |
| `exponential` | `base_price`, `scale_factor`, `num_segments` | equal blocks priced at `base_price × exp(scale_factor × mid-point)` |

Missing prices fall back to the scalar `fuel_cost`. A generator `fuel_cost_curve` or battery `discharge_cost_curve` prices that unit in both the dispatch and the master problem. On a technology, the curve prices its output in the master problem (on every bus of the node) and the capacity built from it in the operational dispatch. Details in [CostCurveConfig](../reference/config-reference.md#costcurveconfig).

### Battery Technologies

```yaml
    battery_technologies:
      li_ion_4h:
        name: Li-ion 4h
        invest_cost_power: [180000.0, 180000.0]   # $/MW per node
        invest_cost_energy: [120000.0, 120000.0]  # $/MWh per node
        invest_max_power: [20.0, 20.0]            # MW per node
        invest_max_capacity: [80.0, 80.0]         # MWh per node
        min_duration_hours: 2.0                   # energy/power ratio bounds
        max_duration_hours: 8.0
        efficiency_charge: [0.95, 0.95]
        efficiency_discharge: [0.95, 0.95]
        degradation_rate: [0.01, 0.01]
        lifetime: 15
        throughput_degradation_cost: [1.0, 1.0]   # $/MWh discharged, keep > 0
```

Every [BatteryTechnologyConfig](../reference/config-reference.md#batterytechnologyconfig) field is read. Keep `throughput_degradation_cost` positive: with 0 the LP may charge and discharge in the same hour.

### Batteries (Existing)

```yaml
    batteries:
      bess:
        name: BESS
        type: Storage
        capacity: [40.0, 0.0]            # MWh per node
        rated_power: [10.0, 0.0]         # MW
        MaxChargePower: [10.0, 0.0]      # MW
        MaxDischargePower: [10.0, 0.0]   # MW
        efficiency_charge: [0.95, 0.0]
        efficiency_discharge: [0.95, 0.0]
        soc_initial: [0.5, 0.0]          # fraction
        max_DoD: [0.9, 0.0]              # maximum depth of discharge
        life_time: [15, 0]
        initial_age: [0, 0]
        degradation_rate: [0.01, 0.0]
        decommissioning_cost: [0.0, 0.0]
        min_power: [0.0, 0.0]
        min_up: [0, 0]
        min_down: [0, 0]
        ramp_up: [1.0, 0.0]
        ramp_down: [1.0, 0.0]
        eff_at_rated: [0.9, 0.0]
        eff_at_min: [0.9, 0.0]
        inertia: [0.0, 0.0]
        start_up_cost: [0.0, 0.0]
        fuel_cost: [0.0, 0.0]
        fixed_cost: [0.0, 0.0]
        maintenance_cost: [0.0, 0.0]
        invest_cost: [0.0, 0.0]
        invest_max_power: [0.0, 0.0]
        bus_id_per_node: {0: bus_0}
```

All keys above except `name`, `type` and `bus_id_per_node` are required by the loader (it reads the generator-style keys as well). Optional: `invest_cost_energy` (defaults to `invest_cost`), `invest_max_capacity`, `min_duration_hours`/`max_duration_hours` (integers), `spillage`, `reservable`, `current_type`, `bus_index`, `throughput_degradation_cost` (cycling cost per MWh discharged, default 0), `discharge_cost_curve`, `risk_coefficient`. As a legacy alternative, a unit can be written as a system-level `bat_<n>` key with `type: Storage`.

**Availability.** An `Availability` file is kept in the configuration, but the dispatch does not apply availability profiles to existing batteries.

**State of charge.** In operational dispatch the SOC is carried from one window to the next, and no end-of-window or end-of-year SOC target is imposed on batteries (`soc_end_tolerance` is applied to reservoir end levels, not to batteries). In the master problem's representative periods, SOC is cyclic within each period, or linked across periods when TSAM inter-period linking is on.

### Fuels

```yaml
    fuels:
      Diesel:
        unit: m3
        emission_factor: 0.27      # t CO2 per MWh of fuel (divided by each unit's eff_at_rated)
        energy_content: 10.0       # MWh per unit
        price_base: 800.0          # $ per unit (may be negative)
        price_growth_rate: 0.0     # per year
```

The fuel key must match the generators' `fuel` value exactly; it drives the fuel-cost linking and the non-fuel O&M adder described under [Generators](#generators). `emission_factor` and `price_base` default to 0 when omitted.

### Penalties

Penalties price constraint violations and soft limits. Keys are case-insensitive (`LOSS_OF_LOAD`, `CO2_cost` and `TransferMargin`, as written by the Studio, are accepted).

```yaml
    penalties:
      loss_of_load: 10000000.0          # $/MWh unserved (VOLL)
      loss_of_reserve_static: 100.0     # $/MW
      loss_of_reserve_dynamic: 100.0    # $/MW
      loss_of_inertia: 200.0            # $/MW·s
      transfer_margin: 100.0            # $/MW
      curtailment_cost: 20.0            # $/MWh curtailed RE in dispatch
      max_curtailment_ratio: 0.05       # cap on curtailment / RE generation (master)
      fre_penetration_loss: 100.0       # $/MWh RE shortfall
      co2_cost: 10.0                    # $/t CO2
      co2_budget_violation: 500.0       # $/t CO2 over budget
      ev_loss: 10000000.0               # $/MWh EV driving not supplied (omit: = loss_of_load)
      rooftop_curtailment: 5.0          # $/MWh
      loss_of_fuel_supply: 100.0
      non_electric_demand_loss: 100.0   # $/unit non-electric fuel demand not served
```

The values shown are the defaults. `max_curtailment_ratio` is a hard limit per representative day in the master problem, monolithic or Benders; operational dispatch does not enforce it and charges `curtailment_cost` instead. It belongs in `penalties`; a system-level `max_curtailment_ratio` key is ignored.

`soc_violation` and `delay_retirement_per_mw` can also be set here. Any other key under `penalties` is reported as an unknown key when the configuration loads. Older files may contain `curtailment`, `curtailment_excess_penalty`, `re_excess_penalty`, `coupling_slack_penalty`, `transport_congestion` or `storage_violation`: these were removed because they priced nothing (curtailed RE is priced by `curtailment_cost`, the curtailment limit is hard, and the fuel model has no congestion, storage-violation or coupling slacks), and can be deleted.

`loss_of_load` should be far above any generation cost so the optimizer prefers expensive dispatch to load shedding.

### Targets and Economic Parameters

```yaml
    co2_budget:
      enabled: true
      annual_budget: 1000000.0           # t CO2/year

    TARGET_RE_PENETRATION: 0.8           # final-year RE share
    MIN_ANNUAL_INCREMENT: 0.01           # bounds on the yearly step of the RE target path
    MAX_ANNUAL_INCREMENT: 0.1
    MAX_ANNUAL_SYSTEM_COST: 20000000000.0  # $/year investment budget
    discount_rate: 0.05
    life_extension_cost_factor: 0.2
```

The uppercase names are legacy aliases written by the Studio; the lowercase field names (`target_re_penetration`, `max_annual_system_cost`, ...) work too. There is no `initial_re_penetration` key: the starting RE share is always computed from existing capacity. How the target path is built is described in [Master Problem](master-problem.md).

### Power Flow

```yaml
    power_flow_mode: dcopf               # dcopf | dcopf_ac_verify | acopf_soc | acopf_qc | acopf_sdp | acopf_polar | acopf_rect

    dc_power_flow:
      reactance_per_km: 0.4              # Ω/km, for lines without reactance_pu
      voltage_level_kv: 220.0            # kV, for lines without voltage_kv
      slack_bus: 0                       # unless a bus has bus_type: slack
      loss_model: pwl                    # none | linear | pwl
      pwl_loss_segments: 3
      pwl_loss_segments_master: 2
```

`power_flow_mode` selects the network formulation; there is no `dc_power_flow.enabled` switch and no transport model. In DC-OPF, flows follow `P_ij = (θ_i − θ_j) / x_ij` on the bus network. `max_angle_diff_deg` applies to the AC-OPF formulations only. `ac_power_flow` holds the AC check and AC-OPF parameters ([ACPowerFlowConfig](../reference/config-reference.md#acpowerflowconfig)). See [DC Power Flow](../formulation/dc-power-flow.md) and [AC Power Flow](../formulation/ac-power-flow.md).

### EV Configuration

```yaml
    ev_categories:
      sedan:
        battery_capacity: 60.0       # kWh per vehicle
        charging_power: 11.0         # kW per vehicle
        v2g_power: 7.0               # kW per vehicle
        v2g_participation: 0.2       # fraction of vehicles offering V2G
        efficiency_charge: 0.95
        efficiency_discharge: 0.92
        min_soc: 0.2
        daily_energy_kwh: 8.0        # default: 12 % of battery_capacity

    ev_quantity:
      sedan: [1000, 500]             # vehicles per node

    EV_initial_soc: [0.5, 0.5]       # per node

    base_patterns:
      sedan: [0.9, 0.9, 0.9, 0.9, 0.9, 0.8, 0.5, 0.2, 0.1, 0.1, 0.1, 0.1,
              0.1, 0.1, 0.1, 0.1, 0.2, 0.4, 0.6, 0.8, 0.9, 0.9, 0.9, 0.9]
```

`base_patterns` holds 24 hourly plug-in values per category; the pattern is rescaled so a vehicle charges `daily_energy_kwh` per day. Fleet size grows along an S-curve set by `max_adoption`, `growth_rate` and `mid_point_fraction`. See [EV Model](../formulation/ev-model.md).

### Sectoral Demand

```yaml
    electric_demand:
      residential:
        criticality: medium        # critical | high | medium | low
        is_flexible: true
        flexibility_ratio: 0.3     # shiftable share
        delay_tolerance: 4         # hours it can move (0 = no shifting)
      industrial:
        criticality: high
      commercial:
        criticality: low
        is_flexible: true
        flexibility_ratio: 0.5
        delay_tolerance: 2

    sector_distribution:
      0: {residential: 0.40, industrial: 0.35, commercial: 0.25}   # node 0
      1: {residential: 0.50, industrial: 0.30, commercial: 0.20}   # node 1

    criticality_penalties: {critical: 1000, high: 100, medium: 10, low: 1}
```

Each node's shares should sum to 1; this is not validated. Shedding priority follows `criticality`, weighted by `criticality_penalties`.

### Other System Sections

| Key | Page |
|---|---|
| `development_zones` | [Development Zones](development-zones.md) |
| `custom_constraints` | [Custom Constraints](custom-constraints.md) |
| `outage_schedule` | [Scheduled Interruptions](scheduled-interruptions.md) |
| `primary_energy_sources`, `fuel_entry_points`, `fuel_infrastructure`, `non_electric_demand`, `conversion_technologies` | [Primary Energy](../formulation/primary-energy.md) |
| `rooftop_solar_config`, `sim_rooftop` | [Rooftop Solar](../formulation/rooftop-solar.md) |
| `electrolyzers` | [Electrolyzer](../formulation/electrolyzer.md) |
| `stochastic_scenarios` | [Stochastic Programming](../formulation/stochastic-programming.md) |
| `transformers`, `acdc_converters`, `freq_converters` | [Reference](../reference/config-reference.md#transformerconfig) |


---

## Multi-System Configuration

```yaml
meta_network:
  systems: [mainland, island]

  systems_links:
    - systems: [mainland, island]
      connections: [[2, 0]]               # [node in mainland, node in island]
      existing_capacity_MW: [50.0]
      max_investment_MW: [200.0]
      investment_cost_per_MW: [500000.0]
      loss_factor: [0.02]                 # required, not used
      distance_km: [90.0]
      cost_per_mw_km: [0.0]               # flow cost, $ per MWh and km
```

`systems_links` is a list. Each list field holds one value per entry of `connections`. All listed systems are merged into a single network, and each link becomes a transmission line in both the master problem and the operational dispatch. The master and the dispatch charge every MWh the link carries, in either direction, `distance_km × cost_per_mw_km` (reported as `line_flow_cost` in the dispatch cost breakdown). The first system in `meta_network.systems` is the primary system; some features (for example development zones) read only the primary system. See [Inter-System Links](../gui/inter-system-links.md).

---

## External System Files

```yaml
# main_config.yaml
meta_network:
  systems: [mainland, island]

systems:
  mainland: mainland_system.yaml      # path, relative to this file
  island: island_system.yaml
```

A system file contains the system mapping itself (`name`, `nodes`, `generators`, ...). Demand and availability paths inside it are resolved at run time against the folder of the main configuration file.

---

## Validation

`load_config()` (and `esfex validate -c <file>`) checks:

- **Required keys and types**, including the keys the loader reads directly from each generator and battery.
- **Ranges and enumerations**, for example `discount_rate` in [0, 1] or `criticality` in `critical | high | medium | low`.
- **Cross-field rules:** systems named in `meta_network` exist; `bus_id` values are unique; a `connection` bus has `demand_fraction: 0`; each node's bus `demand_fraction` values are rescaled to sum to 1 (with a warning); stochastic scenario probabilities sum to 1; MGA `method` and `objectives` agree; linear custom constraints have terms; outage windows have `end_hour > start_hour`.

It does **not** check:

- unknown or misspelled keys: these are listed (as warnings), not rejected;
- the length of per-node arrays against the node count;
- that demand, availability or inflow files exist (they are opened when the run starts).

Common errors:

| Message | Cause | Fix |
|---|---|---|
| `Failed to load configuration: 'life_time'` | A key the loader requires is missing from a generator or battery | Add the key (see [Generators](#generators)) |
| `nodes_connections  Field required` | Old `nodes` format (`adjacency_matrix`) | Use the flattened `nodes_connections` list |
| `Input should be a valid number, unable to parse string as a number` | Text where a number is expected | Remove quotes or fix the value |
| `Input should be greater than or equal to 0` | Value outside its range | Check the value against the reference |
| `Input should be 'Renewable' or 'Non-renewable'` | Invalid technology `type` (case and translated labels are accepted; other words are not) | Use `Renewable` or `Non-renewable` |
| `role='connection' requires demand_fraction=0` | Demand on a connection bus | Set `demand_fraction: 0` or change the role |
| `Duplicate bus_id 'bus_0'` | Two buses share an id | Rename one |
| `System 'x' referenced in meta_network but not defined in systems` | Name mismatch | Fix `meta_network.systems` or `systems` |

```bash
esfex validate -c my_config.yaml
```

Loads and validates the file and prints a summary without starting the optimization.

---

## Complete Example

A two-node island: solar and a battery in the north, a diesel plant in the south, one 80 MW line, and candidate solar and battery technologies. This is a trimmed version of what the Studio exports; layout keys and fields left at their defaults are omitted.

```yaml
simulation_mode: development
date_start: "01/01/2025 00:00"

temporal:
  resolution_hours: 1
  use_rolling_horizon: true
  rolling_horizon_hours: 48
  overlap_hours: 24
  investment_resolution: 8760

solver:
  name: highs
  threads: 4
  time_limit: 10800
  gap: 0.01

master_problem:
  planning_mode: perfect_foresight
  representative_days: 5
  min_day_separation: 5

enable_primary_energy: false

meta_network:
  systems: [island]

systems:
  island:
    name: island
    demand_path: demand/island_demand.csv
    demand_scale: 1.0
    discount_rate: 0.05
    TARGET_RE_PENETRATION: 0.8
    MIN_ANNUAL_INCREMENT: 0.01
    MAX_ANNUAL_INCREMENT: 0.1
    MAX_ANNUAL_SYSTEM_COST: 20000000000.0
    power_flow_mode: dcopf

    nodes:
      nodes_connections: [0.0, 80.0, 80.0, 0.0]
      node_coordinates:
        - {latitude: 21.5, longitude: -80.0}
        - {latitude: 21.0, longitude: -80.0}
      node_names: [North, South]

    buses:
      - {bus_id: bus_0, name: North 110 kV, parent_node: 0, voltage_kv: 110.0, role: mixed, demand_fraction: 1.0}
      - {bus_id: bus_1, name: South 110 kV, parent_node: 1, voltage_kv: 110.0, role: mixed, demand_fraction: 1.0}

    transmission_lines_geo:
      - {line_id: l0, from_node: 0, to_node: 1, from_bus: 0, to_bus: 1, capacity_mw: 80.0, length_km: 56.0}

    fuels:
      Diesel: {unit: m3, emission_factor: 0.27, energy_content: 10.0, price_base: 800.0}

    generators:
      solar:
        name: Solar PV
        type: Renewable
        fuel: Sun
        Availability: availability/solar.csv
        bus_id_per_node: {0: bus_0}
        rated_power: [20.0, 0.0]
        min_power: [0.0, 0.0]
        min_up: [0, 0]
        min_down: [0, 0]
        ramp_up: [1.0, 0.0]
        ramp_down: [1.0, 0.0]
        eff_at_rated: [1.0, 0.0]
        eff_at_min: [1.0, 0.0]
        inertia: [0.0, 0.0]
        start_up_cost: [0.0, 0.0]
        fuel_cost: [0.0, 0.0]
        fixed_cost: [0.0, 0.0]
        maintenance_cost: [2.0, 0.0]
        decommissioning_cost: [0.0, 0.0]
        invest_cost: [700000.0, 0.0]
        invest_max_power: [0.0, 0.0]
        life_time: [25, 0]
        initial_age: [0, 0]
        degradation_rate: [0.005, 0.0]
      diesel:
        name: Diesel
        type: Non-renewable
        fuel: Diesel
        bus_id_per_node: {1: bus_1}
        rated_power: [0.0, 60.0]
        min_power: [0.0, 0.3]
        min_up: [0, 2]
        min_down: [0, 2]
        ramp_up: [0.0, 0.5]
        ramp_down: [0.0, 0.5]
        eff_at_rated: [0.0, 0.35]
        eff_at_min: [0.0, 0.25]
        inertia: [0.0, 2.0]
        start_up_cost: [0.0, 500.0]
        fuel_cost: [0.0, 0.0]          # loader: 800 / 10 / 0.35 ≈ 228.6 $/MWh at node 1
        fixed_cost: [0.0, 0.0]
        maintenance_cost: [0.0, 5.0]   # loader adds 60 $/MWh (diesel non-fuel adder) -> 65
        decommissioning_cost: [0.0, 20000.0]
        invest_cost: [0.0, 900000.0]
        invest_max_power: [0.0, 0.0]
        life_time: [0, 25]
        initial_age: [0, 10]
        degradation_rate: [0.0, 0.005]

    batteries:
      bess:
        name: BESS
        type: Storage
        bus_id_per_node: {0: bus_0}
        capacity: [40.0, 0.0]
        rated_power: [10.0, 0.0]
        MaxChargePower: [10.0, 0.0]
        MaxDischargePower: [10.0, 0.0]
        efficiency_charge: [0.95, 0.0]
        efficiency_discharge: [0.95, 0.0]
        soc_initial: [0.5, 0.0]
        max_DoD: [0.9, 0.0]
        life_time: [15, 0]
        initial_age: [0, 0]
        degradation_rate: [0.01, 0.0]
        decommissioning_cost: [0.0, 0.0]
        min_power: [0.0, 0.0]
        min_up: [0, 0]
        min_down: [0, 0]
        ramp_up: [1.0, 0.0]
        ramp_down: [1.0, 0.0]
        eff_at_rated: [0.9, 0.0]
        eff_at_min: [0.9, 0.0]
        inertia: [0.0, 0.0]
        start_up_cost: [0.0, 0.0]
        fuel_cost: [0.0, 0.0]
        fixed_cost: [0.0, 0.0]
        maintenance_cost: [0.0, 0.0]
        invest_cost: [0.0, 0.0]
        invest_max_power: [0.0, 0.0]

    technologies:
      solar_new:
        name: Solar PV (new)
        type: Renewable
        fuel: Sun
        Availability: availability/solar.csv
        invest_cost: [700000.0, 700000.0]
        invest_max_power: [50.0, 50.0]
        eff_at_rated: [1.0, 1.0]
        degradation_rate: [0.005, 0.005]
        lifetime: 25
        maintenance_cost: [2.0, 2.0]

    battery_technologies:
      li_ion_4h:
        name: Li-ion 4h
        invest_cost_power: [180000.0, 180000.0]
        invest_cost_energy: [120000.0, 120000.0]
        invest_max_power: [20.0, 20.0]
        invest_max_capacity: [80.0, 80.0]
        min_duration_hours: 2.0
        max_duration_hours: 8.0
        efficiency_charge: [0.95, 0.95]
        efficiency_discharge: [0.95, 0.95]
        degradation_rate: [0.01, 0.01]
        lifetime: 15
        throughput_degradation_cost: [1.0, 1.0]

    penalties:
      loss_of_load: 50000.0
      curtailment_cost: 20.0
      max_curtailment_ratio: 0.05
      fre_penetration_loss: 100.0
      co2_cost: 10.0

    co2_budget:
      enabled: false
```

See the [Configuration Reference](../reference/config-reference.md) for every field.
