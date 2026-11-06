# Demand Data

Each system reads its electrical demand from one table (`demand_path`) or from one file per node (`demand_paths`). Values are in **MW**, one row per hour, one column per node.

```yaml
date_start: "01/01/2025 00:00"
systems:
  island:
    demand_path: demand/island_demand.csv
    demand_scale: 1.0
```

## File Formats

### CSV (.csv)

Comma-separated. Every row is an hour of data; a header row is optional (see below).

```csv
120.5,65.3,38.2
118.0,63.1,36.9
115.2,61.0,35.5
...
155.0,82.0,46.0
```

### Excel (.xlsx)

One worksheet, one column per node.

!!! note "Header rows"
    Data start in the first row, for CSV and Excel alike. Leading rows with a non-numeric cell (for example a row of node names) are skipped as headers, so a file with or without a header row gives the same hours. The run and the [Python API](#python-api) read the same rows.

### Format Rules

| Rule | Description |
|------|-------------|
| **Rows** | One per hour. Every simulated year is exactly **8,760 rows** (see [Leap years](#leap-years)) |
| **Columns** | One per node, in node order (node 0 first). The run does not check the column count |
| **Values** | Electrical demand in MW, non-negative |
| **No index column** | Do not include timestamps or row numbers |
| **Hourly only** | Rows are always hours, whatever `temporal.resolution_hours` is (see [Time Resolution](#time-resolution)) |

### Example: 3-Node Hourly CSV (first 24 hours)

```csv
120.5,65.3,38.2
118.0,63.1,36.9
115.2,61.0,35.5
113.0,59.8,34.2
112.5,58.5,33.8
114.0,60.0,34.5
125.0,68.0,40.0
145.0,78.5,46.2
160.0,86.0,51.0
165.0,89.0,53.0
168.0,90.5,54.2
170.0,91.0,55.0
172.0,92.0,55.5
170.5,91.5,55.0
168.0,90.0,54.0
165.0,88.5,53.0
162.0,87.0,52.0
158.0,85.0,50.5
150.0,81.0,48.0
145.0,78.0,46.0
140.0,75.5,44.5
135.0,73.0,43.0
128.0,69.0,41.0
122.0,66.0,39.0
```

### File Location

A relative `demand_path` is looked up in the directory of the configuration file first. If no file is found there, it is used as given (relative to the current directory, or absolute).

---


## Time Alignment and Multi-Year Demand

The first data row is the first hour of the simulation. `date_start` (format `DD/MM/YYYY HH:MM`) labels that hour; it does not select rows. The horizon length is set by the run (`esfex run --years N`, default 25). Year labels start at the year of `date_start`; `esfex run --start-year YYYY` relabels them, and the date labels then start on the same day and time of `date_start` in that year.

The run needs `years × 8,760` rows:

| Horizon | Rows for distinct years |
|---------|------------------------|
| 1 year | 8,760 |
| 5 years | 43,800 |
| 10 years | 87,600 |
| 25 years | 219,000 |

What the run does with the rows it finds:

| Rows in the file | Behaviour |
|------------------|-----------|
| ≥ `years × 8,760` | The first `years × 8,760` rows are used, one 8,760-row block per year |
| ≥ 8,760 but fewer than needed | **Only the first 8,760 rows are used**, repeated for every year (warning: `tiling first year for N years`). Any later rows are ignored |
| < 8,760 | Used as-is, with a warning. Provide at least one full year |

There is no demand-growth key: a single-year file gives the same demand every year, in the master problem and in the dispatch. To model growth, write every year into the file.

### Leap years

Every modelled year has 8,760 hours, leap years included: the master problem, the dispatch and the annual scaling all use 8,760 h per year. Feb 29 does not exist in the model:

- If the file has the extra 24 rows of Feb 29, they are dropped when it is read (logged as `dropped 24 Feb 29 hours`). Feb 29 is found from the calendar that starts at `date_start`, provided the file is then a whole number of 8,760-row years. A file of exactly 8,784 rows is taken as one leap year from 1 January, whatever `date_start` says.
- Files already in 8,760-row years are used as they are.
- Date labels skip Feb 29: in a leap year, 28 Feb 23:00 is followed by 1 Mar 00:00, and every year runs from 1 January to 31 December.

Availability and reservoir-inflow profiles follow the same rule.


---


## Per-Node Files (`demand_paths`)

Instead of one multi-column table, give one file per node:

```yaml
systems:
  island:
    demand_paths:
      - demand/demand_node_0.csv
      - demand/demand_node_1.csv
```

- `demand_paths` takes precedence over `demand_path`.
- The list must have exactly one entry per node. Otherwise the run stops with `demand_paths has N entries but system has M nodes`.
- Only the **first column** of each file is read. If the files have different lengths, all are cut to the shortest.

A `demand_path` that points to a single-column file whose name contains `demand_node_<i>` (for example `demand/demand_node_0.csv`) also loads its siblings `demand_node_0` … `demand_node_<N-1>` with the same extension from the same folder, provided the system has more nodes than the file has columns.


---


## Demand Scaling

`demand_scale` (default 1.0, must be > 0) multiplies the system's demand after it is read and tiled:

```yaml
systems:
  island:
    demand_scale: 1.05    # 5 % above the file values
```

This is useful for sensitivity analysis without editing the data file.


---


## Multiple Systems

Each system in `meta_network.systems` reads its own demand. The tables are placed side by side in that order, so the first system's nodes come first. If the systems' files have different lengths, all are cut to the shortest.


---


## Demand on Multi-Bus Nodes

Demand columns are per **node**. When a node has several electrical buses (`buses` with the same `parent_node`), each bus receives a share of the node's demand:

| Bus `role` | Demand at the bus |
|------------|-------------------|
| `load` (default) | node demand × `demand_fraction` |
| `mixed` | node demand × `demand_fraction` |
| `connection` | 0. A connection bus must have `demand_fraction: 0` (validated on load) |

```yaml
systems:
  island:
    buses:
      - bus_id: north_load
        name: North 33 kV
        parent_node: 0
        voltage_kv: 33.0
        role: load
        demand_fraction: 0.7
      - bus_id: north_industrial
        name: North industrial 33 kV
        parent_node: 0
        voltage_kv: 33.0
        role: mixed
        demand_fraction: 0.3
      - bus_id: north_plant
        name: North plant 132 kV
        parent_node: 0
        voltage_kv: 132.0
        role: connection
        demand_fraction: 0.0
```

- **Fractions are not normalised.** Make the `demand_fraction` values of a node's `load` and `mixed` buses sum to 1. Because new buses default to `role: load` with `demand_fraction: 1.0`, two buses on one node left at their defaults carry the node's demand twice.
- If a system defines no `buses`, one bus per node is created with `demand_fraction: 1.0`.
- Load shedding is per bus and can never exceed the demand at that bus, so a `connection` bus cannot shed load.
- Node-level EV charging and V2G enter each bus's power balance scaled by the same fraction.


---


## Sectoral Distribution

Demand can be split into sectors with different loss-of-load costs. The split applies only when the system has **both** `electric_demand` and `sector_distribution`; otherwise all unserved energy costs `LOSS_OF_LOAD`.

```yaml
systems:
  island:
    electric_demand:
      residential:
        criticality: medium
      industrial:
        criticality: high
      hospitals:
        criticality: critical
      street_lighting:
        criticality: low
    sector_distribution:
      0: {residential: 0.55, industrial: 0.35, hospitals: 0.05, street_lighting: 0.05}
      1: {residential: 0.70, industrial: 0.20, hospitals: 0.05, street_lighting: 0.05}
    criticality_penalties:
      critical: 3.0
      high: 2.0
      medium: 1.0
      low: 0.5
    penalties:
      LOSS_OF_LOAD: 50000.0
```

### Criticality and Loss-of-Load Cost

`criticality` is one of `critical`, `high`, `medium` (default) or `low`. Each level selects a **multiplier** from `criticality_penalties`, and the sector's cost of unserved energy is `LOSS_OF_LOAD × multiplier`.

| Level | Schema default multiplier | Value written by a new Studio project |
|-------|--------------------------|----------------------------------------|
| `critical` | 1000 | 3.0 |
| `high` | 100 | 2.0 |
| `medium` | 10 | 1.0 |
| `low` | 1 | 0.5 |

A YAML file that omits `criticality_penalties` gets the schema defaults. These are large multipliers on top of `LOSS_OF_LOAD`, so set `criticality_penalties` explicitly.

!!! note "Sector priorities"
    The load shed at a node is split into per-sector amounts, each capped by that sector's demand, so the optimizer sheds the cheapest sector first and moves to a dearer one only when the cheaper one is fully shed. Demand that no sector accounts for (sector shares missing for a node) is shed at `LOSS_OF_LOAD` with multiplier 1. The master problem's representative days apply the same per-sector caps.

### Sector Fields

| Field | Type | Default | Effect in the current version |
|-------|------|---------|-------------------------------|
| `criticality` | `critical` / `high` / `medium` / `low` | `medium` | Selects the loss-of-load multiplier |
| `is_flexible` | bool | false | The sector can shift demand in time (see below) |
| `flexibility_ratio` | float, 0 to 1 | 0.0 | Share of the sector's demand in a step that can be moved |
| `delay_tolerance` | int, hours | 0 | How far demand can be moved, earlier or later; 0 = no shifting |
| `price_sensitivity` | float, 0 to 1 | 0.0 | Not used by the model |

!!! note "Demand shifting"
    A sector with `is_flexible: true`, `flexibility_ratio > 0` and `delay_tolerance > 0` can move up to `flexibility_ratio` of its demand in each time step to another step at most `delay_tolerance` hours earlier or later, at the same node and within the same rolling window. The energy is moved, not removed, and costs `demand_shift_cost_rate` per MWh and per hour moved. Flexible demand is never curtailed: cutting demand is load shedding, priced by criticality. See [Operational Dispatch -- Demand Shifting Constraints](../formulation/operational-dispatch.md#demand-shifting-constraints).

### Distribution Rules

| Rule | Behaviour |
|------|-----------|
| Keys of `sector_distribution` | Node indices, 0-based (with several systems, each system's own node numbering) |
| Fractions per node | Normalised to sum to 1. When a node's sum is off by more than 0.01, the run logs `Sector distribution normalized for N nodes` |
| Node missing from `sector_distribution` | Uses node 0's fractions |
| Sector missing at a node | Gets no demand at that node |

The sectoral split is applied to the total demand, including EV charging demand when EV optimisation is off.


---


## EV Demand Integration

EV charging demand is generated from `ev_categories`, `ev_quantity` and `base_patterns` with an S-curve fleet growth model.

- **EV optimisation on** (the system has `ev_categories`): charging and V2G are decision variables in the dispatch model. The generated EV demand is **not** added to the demand table, to avoid counting it twice.
- **EV optimisation off**: the generated charging demand is added to the demand table: `total_demand = base_demand + ev_charging_demand`.

See [EV Model](../formulation/ev-model.md) for details.


---


## Time Resolution

The demand file is always hourly. With `temporal.resolution_hours > 1` the run aggregates it: operational dispatch uses the **mean** of each block (energy is preserved), and the master problem uses the **maximum** (peaks are preserved). `resolution_hours` is an integer ≥ 1, so sub-hourly demand is not supported.


---


## Python API

These helpers are for preparing and inspecting data; a simulation run does not call them, but they read the rows a run reads: header rows are skipped, Feb 29 rows are dropped and every year is an 8,760-row block from the year of `date_start`.

### Loading Demand Data

```python
from esfex.io.demand import load_demand_data

demand, hours, num_nodes, years, time_index = load_demand_data(
    "demand.csv",
    date_start="01/01/2025 00:00",
)
print(demand.shape)              # (hours, nodes)
print(years)                     # model years covered (one per 8,760 rows)
print(f"Peak demand: {demand.max():.1f} MW")
```

### Creating Sectoral Demand

```python
from esfex.io.demand import create_sectoral_demand

sector_distribution = {
    0: {"residential": 0.40, "industrial": 0.35, "commercial": 0.25},
    1: {"residential": 0.50, "industrial": 0.30, "commercial": 0.20},
}

sectoral = create_sectoral_demand(demand, sector_distribution)
# {"residential": array(hours, nodes), "industrial": ..., "commercial": ...}
```

### DemandDataManager

`load_demand_data(..., year_to_load=2026)` returns only that year's 8,760-row block.

Converts a demand file to a temporary HDF5 file indexed by model year (8,760-row blocks, as in a run), for fast year-by-year access:

```python
from esfex.io.demand import DemandDataManager

mgr = DemandDataManager("demand.csv", date_start="01/01/2025 00:00")
mgr.prepare_hdf5_storage()
demand_2026, hours, num_nodes, time_idx = mgr.load_year_data(2026)
mgr.cleanup()
```

---


## Troubleshooting

| Message | Cause | Solution |
|---------|-------|----------|
| `Demand file not found: <path>` | `demand_path` does not resolve to a file | Paths are looked up next to the config file first, then as given |
| `Per-node demand file missing for '<system>' node <i>: <path>` | An entry of `demand_paths` (or an auto-discovered `demand_node_<i>` sibling) is missing | Check the file names |
| `demand_paths has N entries but system has M nodes` | `demand_paths` length does not match `nodes.num_nodes` | Give exactly one file per node |
| `No demand_path or demand_paths configured for system '<system>'` | Neither key is set | Add `demand_path` or `demand_paths` |
| `<path>: non-numeric value in data row <n>` | A text cell below the header rows | Data rows must contain numbers only |
| `Demand for '<system>' has <n>h — tiling first year for <N> years.` (warning) | The file is shorter than the horizon | Expected for single-year files. Add the missing years if you want them to differ |
| `Sector distribution normalized for N nodes` (warning) | Some node's sector fractions do not sum to 1 | Fix the fractions in `sector_distribution` |
| Demand looks shifted by one day from March on | A leap-year file was not recognised (its length is not a calendar span from `date_start`) | Remove the Feb 29 rows, or set `date_start` to the file's first hour |
| Demand counted twice on a node | Several `load`/`mixed` buses with fractions that sum to more than 1 | Set `demand_fraction` so a node's load buses sum to 1 |

### Best Practices

1. **Units**: demand values must be in MW, not kW or GW.
2. **Missing data**: do not leave cells empty or use placeholders like `-1`.
3. **Columns**: give exactly one column per node; the run does not check the count.
4. **Format**: prefer CSV. It has no header ambiguity and loads faster than Excel for long multi-year files.
