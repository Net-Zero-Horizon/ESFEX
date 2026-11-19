# HDF5 Output Schema

The runner writes one HDF5 file per run, named `<config_stem>_<ddmmyyyy>_<HHMM>.h5` (for example `isla_juventud_15012026_1030.h5`) in the output directory. Without a config path the system name replaces the stem. The file is created by `Orchestrator._initialize_hdf5()`, extended once per year by `_append_year_to_hdf5()`, and closed by `_finalize_hdf5()` (see `src/esfex/runner.py`).

## File Structure Overview

```
<config_stem>_<ddmmyyyy>_<HHMM>.h5
├── [Root Attributes]              # Metadata and subsystem layout
├── system_configuration/          # Static system definition (written once)
│   ├── generators/generator_{g}/  # Existing generators (attributes)
│   ├── batteries/battery_{b}/     # Existing batteries (attributes)
│   ├── nodes/                     # Node data (datasets + attributes)
│   ├── technologies/              # Investment technologies (if any)
│   ├── battery_technologies/      # Battery investment technologies (if any)
│   ├── fuel_price_growth/         # Fuel price growth of the fuels block (attributes)
│   ├── transmission_lines/        # Line geometry (if transmission_lines_geo is set)
│   └── fuel_routes/               # Fuel route geometry (if any)
├── summary_results/               # Per-year scalars (expandable datasets)
├── demand/
│   └── year_YYYY_base_demand      # Per-year demand
├── cost_breakdown/
│   └── year_YYYY/                 # Per-year cost components (attributes)
├── detailed_results/
│   └── year_YYYY_threshold_0/     # Per-year time series
│       ├── generation/            # Generator output
│       ├── gen_status/            # Units online (UC mode)
│       ├── gen_startup/           # Units started (UC mode)
│       ├── battery_charge/        # Charging power
│       ├── battery_discharge/     # Discharging power
│       ├── battery_soc/           # State of charge
│       ├── battery_spillage/      # Energy spillage
│       ├── battery_capacity_factor/
│       ├── battery_lcoe/
│       ├── battery_vallcoe/
│       ├── reservoir_level/       # Reservoir water level
│       ├── reservoir_spillage/    # Reservoir overflow
│       ├── reservoir_pump/        # Pump-back power
│       ├── electrolyzer_power/    # Electrolyzer power (if any electrolyzer)
│       ├── electrolyzer_h2/       # Hydrogen produced (if any electrolyzer)
│       ├── capacity_factor/       # Generator CF
│       ├── lcoe/                  # Generator LCOE
│       ├── vallcoe/               # Generator VALLCOE
│       ├── technology_selling_prices/ # Revenue analysis per generator
│       ├── primary_energy/        # Fuel supply chain results (if enabled)
│       ├── master_fuel_capacity/  # Fuel storage, routes and electrolyzers the master built (if any)
│       ├── n1_security/           # N-1 security results (if enabled)
│       └── [datasets]             # Per-node and system-level time series
├── global/
│   ├── summary_results/           # Per-year scalars for the whole meta-network
│   ├── cost_breakdown/year_YYYY/  # Same attributes as /cost_breakdown
│   └── demand/year_YYYY_base_demand
├── inter_system/
│   └── detailed_results/
│       └── year_YYYY_threshold_0/
│           └── power_flow/{A}_to_{B}  # Multi-system runs only
├── npv_retirements/               # Only when npv_retirement is on
├── mga/                           # Only when MGA / SPORES is enabled
├── diagnostics/
│   ├── warnings/                  # The run's warnings, grouped by kind
│   └── master/                    # Binding constraints, duals, paid slacks per master solve
│       ├── year_YYYY/{binding,top,slacks}
│       └── all/{binding,top,slacks}   # Perfect foresight: rows without a year
└── plugins/{name}/                # Optional, written by plugins in post_year
```

A group can be present and empty, for example the battery groups in a run without batteries. Groups and datasets marked "if" are only written when the corresponding result exists.

## Root Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `creation_date` | string | ISO 8601 creation timestamp |
| `num_nodes` | int | Number of network nodes (all systems merged) |
| `num_years` | int | Number of years simulated (the `years` argument of `run()`; 1 in unit-commitment mode) |
| `export_type` | string | `"incremental_results"` |
| `temporal_resolution_hours` | int | Time step (hours) |
| `simulation_mode` | string | `"development"` or `"unit_commitment"` |
| `years_range` | string | `"YYYY-ZZZZ"`, first and last simulated year |
| `esfex_version` | string | ESFEX version that wrote the file |
| `solver` | string | `solver.name` of the run |
| `config_file` | string | Absolute path of the configuration file (`""` for a run started without one) |
| `num_systems` | int | Number of systems in the meta-network |
| `subsystem_names` | string[] | System names, in node order |
| `subsystem_offsets` | int[] | First node index of each system |
| `subsystem_node_counts` | int[] | Number of nodes of each system |
| `export_complete` | bool | `True` once the run finished and the file was finalized |
| `last_update` | string | ISO 8601 timestamp of the last year written |
| `export_timestamp` | string | ISO 8601 timestamp of finalization |

`esfex_version`, `solver` and `config_file` are missing in files written before ESFEX 0.4.

The `subsystem_*` attributes are written for single-system runs too. Readers use them to slice a system's nodes out of the global node axis.

---

## system_configuration/

Static system configuration stored once at file creation, from the merged operational system. It describes the units that exist in the configuration; investment candidates are listed under `technologies/` and `battery_technologies/`, not under `generators/`.

### system_configuration/generators/generator_{g}/

One group per configured generator, with every field of `GeneratorConfig.model_dump()` as an attribute (`name`, `type`, `fuel`, `rated_power`, `invest_cost`, `life_time`, `initial_age`, ...). Group attribute `num_generators` on `generators/`.

Strings, ints, floats and bools are stored as-is. Every other value (lists, dicts, `None`) is stored as its Python `str()`. Per-node lists therefore come back as strings such as `"[20.0, 0.0, 0.0]"`; parse them with `ast.literal_eval`.

### system_configuration/batteries/battery_{b}/

Same encoding, from `BatteryConfig.model_dump()`. Group attribute `num_batteries`.

### system_configuration/nodes/

| Content | Kind | Description |
|---------|------|-------------|
| `num_nodes` | attribute | Number of nodes |
| numeric node fields (`nodes_connections`, `losses`, `reserve_static`, ...) | datasets | Numeric lists from `NodesConfig.model_dump()` |
| other node fields (`node_names`, `node_coordinates`, ...) | attributes | Stored as `str()` |
| `latitude`, `longitude` | datasets | Node coordinates (0.0 where missing), when `node_coordinates` is set |
| `name` | dataset (string) | `node_names`, or the coordinate labels |

### system_configuration/technologies/technology_{i}/

Attributes `key`, `name`, `type`, `fuel`, `invest_cost`, the operating costs `fuel_cost`, `fixed_cost`, `maintenance_cost` and `start_up_cost` (string-encoded per-node lists) and `color` (if set). Group attribute `num_technologies`. The operating costs are those of the units the master builds from the technology, which have no group under `generators/`; files written before they were recorded lack them.

### system_configuration/battery_technologies/battery_technology_{i}/

Attributes `key`, `name`, `invest_cost_power`, `invest_cost_energy`, `maintenance_cost`, `fixed_cost` (string-encoded per-node lists) and `color` (if set). Group attribute `num_battery_technologies`.

### system_configuration/fuel_price_growth/

Attribute `base_year` (the run's first year, which the `fuels` prices hold for) and one attribute per fuel of the `fuels` block, named after the fuel, with its `price_growth_rate`. The dispatch charges a unit burning that fuel `fuel_cost × (1 + rate)^(year − base_year)`; the financial analysis values fuel the same way. Files written before this group existed value fuel at the flat `fuel_cost`.

### system_configuration/transmission_lines/line_{i}/

Attributes `line_id`, `from_node`, `to_node`, `capacity_mw`; dataset `waypoints` `(k, 2)` of `[latitude, longitude]` if the line has waypoints. Group attribute `num_lines`.

### system_configuration/fuel_routes/route_{i}/

Attributes `route_id`, `from_node`, `to_node`, `distance_km`; dataset `waypoints` `(k, 2)` if present. Group attribute `num_routes`.

---

## summary_results/

Expandable arrays that grow as each year completes. One row per simulated year.

| Dataset | Shape | Type | Description |
|---------|-------|------|-------------|
| `year` | (num_years,) | int | Simulation year |
| `threshold` | (num_years,) | int | Always 0 |
| `feasible` | (num_years,) | int | 1 if feasible, 0 if not |
| `total_cost` | (num_years,) | float | Total annual cost ($) |
| `renewable_penetration` | (num_years,) | float | RE share (0-1): renewable over total generation of the year, energy-weighted; battery discharge is neither |
| `co2_emissions` | (num_years,) | float | Annual CO2 (tonnes) |
| `loss_of_load` | (num_years,) | float | Total unserved energy (MWh) |
| `transmission_losses` | (num_years,) | float | Energy lost on the lines by the DC loss model (MWh); 0 without a loss model (`loss_model: none`) and in ACOPF modes |

Energy and CO2 totals here and in the scenario attributes below are sums of the per-step rates times `temporal_resolution_hours`, so they are MWh and tonnes at any time step.
| `n1_security_cost` | (num_years,) | float | Cost of N-1 security ($): objective with N-1 minus objective without it, with `n1_security.compute_security_cost: true`; 0 otherwise. The shortfall penalty alone is `n1_security_shortfall_cost` in `cost_breakdown/` |

---

## demand/

### demand/year_YYYY_base_demand

| Property | Value |
|----------|-------|
| Shape | (hours, num_nodes) |
| Type | float32 |
| Units | MW |
| Description | Gross demand of the year: base demand from the demand files, plus EV demand when EV optimisation is off. Rooftop solar is not subtracted |

Despite the name, this is the same total demand array as `detailed_results/.../demand`, but always written as `(hours, num_nodes)`.

---

## cost_breakdown/

Granular decomposition of the annual system cost into the components below; `total` is the sum of every entry except `pe_electrolyzer_cost`, `v2g_compensation` and `operating_budget_excess` (reporting entries). Each year's costs are stored as attributes on a `/cost_breakdown/year_YYYY/` group, summed over the rolling-horizon windows. Each window counts only its committed steps (the overlap tail is solved again by the next window); costs with no time step (investment annuities, CO2-budget slack) count for the committed share of the window.

This group is present when the optimizer exports per-component cost data. The [Financial Analysis](../api/models-financial-analysis.md) module reads this group directly; when it is absent, costs are recalculated from generation output and system configuration.

### cost_breakdown/year_YYYY/ (Attributes)

| Attribute | Type | Units | Description |
|-----------|------|-------|-------------|
| `fuel_cost` | float | $ | Fuel consumption cost |
| `fixed_om_cost` | float | $ | Fixed O&M of the generators and batteries in service ($/MW·yr × MW, prorated to the simulated hours), whether or not they run |
| `maintenance_cost` | float | $ | Variable maintenance cost |
| `startup_cost` | float | $ | Generator startup cost |
| `battery_maintenance_cost` | float | $ | Battery O&M cost |
| `battery_degradation_cost` | float | $ | Battery degradation cost |
| `load_shedding_cost` | float | $ | Cost of unserved energy |
| `curtailment_cost` | float | $ | Renewable curtailment penalty |
| `reserve_static_cost` | float | $ | Static reserve deficit penalty |
| `reserve_dynamic_cost` | float | $ | Dynamic reserve deficit penalty |
| `co2_emission_cost` | float | $ | Carbon emission cost |
| `fre_penetration_cost` | float | $ | RE penetration shortfall penalty |
| `inertia_cost` | float | $ | System inertia deficit penalty |
| `soc_violation_cost` | float | $ | Battery SOC violation penalty, the year-end SOC shortfall (BAT-5) included |
| `transfer_margin_cost` | float | $ | Transfer margin deficit penalty |
| `v2g_compensation` | float | $ | V2G payment to EV owners: a transfer between agents, reported only, not part of `total` |
| `electrolyzer_cost` | float | $ | Electrolyzer variable and water cost, and the window's share of its fixed O&M |
| `converter_cost` | float | $ | AC/DC converter cost |
| `spillage_cost` | float | $ | Battery spillage penalty |
| `reservoir_spillage_cost` | float | $ | Reservoir overflow penalty |
| `demand_shift_cost` | float | $ | Demand shifting cost |
| `rooftop_curtailment_cost` | float | $ | Rooftop solar curtailment penalty |
| `pe_supply_cost` | float | $ | Primary energy supply cost |
| `pe_loss_cost` | float | $ | Primary energy loss-of-supply penalty |
| `pe_excess_cost` | float | $ | Primary energy excess penalty |
| `pe_transport_cost` | float | $ | Primary energy transport cost |
| `pe_investment_cost` | float | $ | Always 0: fuel storage and transport are built (and charged) by the master problem |
| `pe_electrolyzer_cost` | float | $ | The electrolyzers' cost (`electrolyzer_cost`) when their hydrogen fills the primary-energy `Hydrogen` tank, 0 otherwise. The same money as `electrolyzer_cost`, not an extra cost: do not add both |
| `n1_security_shortfall_cost` | float | $ | N-1 security shortfall penalty |
| `line_flow_cost` | float | $ | Flow cost of lines with a `cost_per_mw_km` (inter-system links: length × `cost_per_mw_km` × MWh carried) |
| `operating_budget_excess` | float | $ | Operating spending above the dispatch's operating budget (`max_annual_operating_cost`, COST-1). Money already in `fuel_cost`, `maintenance_cost`, `fixed_om_cost` and the primary-energy fuel costs, not an extra cost: not part of `total`. 0 without an operating budget |
| `operating_budget_penalty` | float | $ | Penalty on that excess (1000 $ per $), part of `total` |
| `total` | float | $ | Objective value of the dispatch, the sum of the other entries. Reporting-only entries, `pe_electrolyzer_cost` (a view of `electrolyzer_cost`), `v2g_compensation` (a transfer) and `operating_budget_excess` (money already in the costs), are not part of it |

### Reading Cost Breakdown

```python
import h5py

with h5py.File("isla_juventud_15012026_1030.h5", "r") as f:
    if "cost_breakdown" in f:
        for year_key in sorted(f["cost_breakdown"].keys()):
            attrs = dict(f[f"cost_breakdown/{year_key}"].attrs)
            print(f"{year_key}: total=${attrs['total']:,.0f}")
            print(f"  Fuel: ${attrs['fuel_cost']:,.0f}")
            print(f"  CO2:  ${attrs['co2_emission_cost']:,.0f}")
```

---

## detailed_results/year_YYYY_threshold_0/

Full time-series results for each year. The `threshold_0` suffix is fixed.

Node axes are at node level: when the operational network has buses that differ from the nodes, bus results are aggregated to nodes before writing (sum for quantities, `gen_status` included (units online); max for `nodal_electricity_prices` and `voltage_angle`). `voltage_magnitude` and `reactive_generation` stay at bus level.

### Attributes (Per-Year Metadata)

| Attribute | Type | Description |
|-----------|------|-------------|
| `year` | int | Simulation year |
| `threshold` | int | Always 0 |
| `feasible` | bool | Feasibility flag |
| `total_cost` | float | Total annual cost ($) |
| `renewable_penetration` | float | The year's RE share: renewable over total generation, energy-weighted over the dispatch windows (committed steps) |
| `co2_emissions` | float | Annual CO2 emissions (tonnes) |
| `solve_time` | float | Total solve time (seconds) |
| `total_generation` | float | Total energy generated (MWh) |
| `total_demand` | float | Total energy demand (MWh) |
| `load_shed` | float | Total unserved energy (MWh) |
| `transmission_losses` | float | Energy lost on the lines (MWh), the sum of `line_losses` × `temporal_resolution_hours` |
| `master_re_target` | float | RE penetration target from the master problem for this year |
| `master_loss_repair_lower_bound` | float | Objective ($) of the first solve of the master that planned this year, with the PWL loss model: lines may lose more than the PWL loss of their flow, a relaxation, so a lower bound of the master with exact PWL losses. The same value for every year a solve planned (all years of a perfect-foresight master). Written when the master has PWL or linear losses and its first solve was optimal; not under MGA or Benders |
| `master_loss_repair_objective` | float | Objective ($) of that master after the loss repair: an upper bound of it, within the 2 % tolerance of the linearised losses |
| `master_loss_repair_gap` | float | (objective − lower bound) / \|objective\|, 0 when no line over-lost |
| `master_loss_repair_rounds` | int | Repair rounds (re-solves) of that solve |
| `master_loss_repair_linearised` | int | Line-steps held in one direction at the end |
| `master_loss_repair_released` | int | Held line-steps the direction check released |
| `dispatch_loss_repair_lower_bound` | float | Sum over the year's dispatch windows of the objective ($) of their first, PWL, solve (whole windows, look-ahead steps included, so not comparable to `total_cost`). Written when the dispatch has PWL or linear losses |
| `dispatch_loss_repair_objective` | float | Sum over the windows of their repaired objective ($), on the same steps |
| `dispatch_loss_repair_gap` | float | Gap of the two sums |
| `dispatch_loss_repair_gap_max` | float | Largest gap of a window |
| `dispatch_loss_repair_rounds`, `dispatch_loss_repair_linearised`, `dispatch_loss_repair_released` | int | Summed over the windows, as for the master |
| `dispatch_loss_repair_windows`, `dispatch_loss_repair_windows_repaired` | int | Windows with bounds, and those with at least one repair round |

See [Bounds of the repair](../formulation/dc-power-flow.md#bounds-of-the-repair). Files written before this version have none of the `*_loss_repair_*` attributes.

Investment and retirement decisions of the master problem for this year are stored as scalar attributes. Keys use 0-based technology, node and generator *indices*, not names:

| Pattern | Description |
|---------|-------------|
| `investment_tech_investment_power_{t}_{n}` | New capacity of technology `t` at node `n` (MW) |
| `investment_bat_tech_investment_power_{bt}_{n}` | New power of battery technology `bt` at node `n` (MW) |
| `investment_bat_tech_investment_capacity_{bt}_{n}` | New energy of battery technology `bt` at node `n` (MWh) |
| `investment_transfer_investment_{i}_{j}` | New transmission capacity from node `i` to node `j` (MW) |
| `investment_reservoir_investment_{g}_{n}` | Reservoir capacity added to generator `g` at node `n` (MWh) |
| `investment_converter_investment_acdc_{c}` | Capacity added to AC/DC converter `c` (MW) |
| `investment_converter_investment_freq_{c}` | Capacity added to frequency converter `c` (MW) |
| `retirement_gen_{g}` | Share (0-1) of existing generator `g`'s capacity (`rated_power` summed over nodes) retired by this year (capacity the master keeps by life extension counts as in service; a node where the NPV-based retirement retired it counts as retired) |

The node index `n` (and `i`, `j`) is always a node: when the master solves on buses (a one-node system with several buses, development zones), the buses of a node are summed onto it, and transmission built between two buses of the same node is not listed. Investments below 0.1 and retirement fractions below 0.01 are not written. Battery retirements are not exported. Technology indices follow `system_configuration/technologies/`; generator indices follow `system_configuration/generators/`; converter indices follow the system's `acdc_converters` and `freq_converters` lists.

### Dataset Naming

Datasets inside per-unit groups are named after the unit's `name`, with `/` replaced by ` - ` (so `Cuba/Solar PV` becomes `Cuba - Solar PV`). A name already used by another generator (or, in battery groups, another battery; in electrolyzer groups, another electrolyzer) gets a ` (1)`, ` (2)`, ... suffix: two units named `Gas` are written as `Gas` and `Gas (1)`. A unit has the same dataset name in every per-unit group (`generation/`, `lcoe/`, `technology_selling_prices/`, ...; `battery_soc/`, `battery_charge/`, ...). Readers that look up a unit's configuration by name (financial analysis, results charts) strip the suffix.

Each `generation/{name}` dataset carries the attributes `fuel` (declared fuel, `""` if none), `is_renewable` (the unit is of type `Renewable`, what the RE share counts as renewable), `shape`, `units` and `rated_power`: the unit's MW in service in the year, per node, the capacity the dispatch charges fixed O&M on. For an existing unit this is its rated power after the master's retirements and life extension (development runs), or, without a master, its rated power degraded with age and 0 past its `life_time`; for a unit the master built (see [Virtual Generator and Battery Datasets](#virtual-generator-and-battery-datasets)), its built capacity in service. `battery_discharge/{name}` carries the battery's discharge power in service the same way (not degraded; 0 past its `life_time` unless the master keeps it); for a battery the master built, its rated power (the master degrades the power it dispatches, but fixed O&M stays on the power built; older files hold the degraded power). The results dashboard, the results charts (installed capacity, reserve margin) and the financial analysis read it; for files written before this version, where only the units the master built carry it, they use the configured `rated_power` (`MaxDischargePower`) of the other units.

### Generation Data

#### generation/{generator_name}

| Property | Value |
|----------|-------|
| Shape | (num_nodes, hours) |
| Type | float64 |
| Units | MW |
| Description | Generator output power per node per hour |

#### gen_status/{generator_name}

| Property | Value |
|----------|-------|
| Shape | (num_nodes, hours) |
| Type | float64 |
| Units | units online (0..`num_units`; 0/1 for a single-unit plant) |
| Attributes | `num_units`: the generator's units per node (summed over the node's buses) |
| Description | Commitment status: units online. Written only in unit-commitment mode, and only for committed generators: a variable renewable (PV, wind, ocean, run-of-river hydro; see [GEN-1b](../formulation/operational-dispatch.md#gen-1-generator-capacity-limits)) has no dataset, and readers count it online whenever it generates. Files written before clustered unit commitment hold 0/1 and no `num_units` (read as one unit); files written before this rule also hold datasets for variable renewables, with status 0 and meaningless start-ups |

#### gen_startup/{generator_name}

| Property | Value |
|----------|-------|
| Shape | (num_nodes, hours) |
| Type | float64 |
| Units | units started (0..`num_units`) |
| Attributes | `num_units`, as on `gen_status` |
| Description | Units started per step. A start costs `start_up_cost / num_units`. Written only in unit-commitment mode, for the generators that have a `gen_status` dataset |

#### curtailment

| Property | Value |
|----------|-------|
| Shape | (num_nodes, hours) |
| Type | float64 |
| Units | MW |
| Description | Renewable curtailment summed over generators |

### Battery Data

#### battery_charge/{battery_name}

| Property | Value |
|----------|-------|
| Shape | (num_nodes, hours) |
| Type | float64 |
| Units | MW |
| Description | Charging power |

#### battery_discharge/{battery_name}

| Property | Value |
|----------|-------|
| Shape | (num_nodes, hours) |
| Type | float64 |
| Units | MW |
| Description | Discharging power |

#### battery_soc/{battery_name}

| Property | Value |
|----------|-------|
| Shape | (num_nodes, hours) |
| Type | float64 |
| Units | MWh |
| Description | State of charge |

#### battery_spillage/{battery_name}

| Property | Value |
|----------|-------|
| Shape | (num_nodes, hours) |
| Type | float64 |
| Units | MW |
| Description | Energy spillage (excess energy discarded) |

#### battery_capacity_factor/{battery_name}

| Property | Value |
|----------|-------|
| Shape | (num_nodes, hours) |
| Type | float64 |
| Units | Fraction (0-1) |
| Description | Hourly discharge / rated discharge power |

#### battery_lcoe/{battery_name}

| Property | Value |
|----------|-------|
| Shape | (num_nodes, hours) |
| Type | float64 |
| Units | $/MWh |
| Description | Levelized cost of storage (hourly) |

#### battery_vallcoe/{battery_name}

| Property | Value |
|----------|-------|
| Shape | (num_nodes, hours) |
| Type | float64 |
| Units | $/MWh |
| Description | Value-adjusted LCOS (hourly) |

### Reservoir Data

#### reservoir_level/{generator_name}

| Property | Value |
|----------|-------|
| Shape | (num_nodes, hours) |
| Type | float64 |
| Units | MWh-eq |
| Description | Reservoir water level at the start of each hour (like `battery_soc`) |

#### reservoir_spillage/{generator_name}

| Property | Value |
|----------|-------|
| Shape | (num_nodes, hours) |
| Type | float64 |
| Units | MW-eq |
| Description | Uncontrolled water overflow |

#### reservoir_pump/{generator_name}

| Property | Value |
|----------|-------|
| Shape | (num_nodes, hours) |
| Type | float64 |
| Units | MW |
| Description | Pump-back power consumed (demand-side load) |

### Electrolyzer Data

Written when the system has electrolyzers, one dataset per entry under `electrolyzers`, named by its `name` (or key).

#### electrolyzer_power/{electrolyzer_name}

| Property | Value |
|----------|-------|
| Shape | (num_nodes, hours) |
| Type | float64 |
| Units | MW |
| Description | Electric power drawn by the electrolyzer (demand-side load) |

#### electrolyzer_h2/{electrolyzer_name}

| Property | Value |
|----------|-------|
| Shape | (num_nodes, hours) |
| Type | float64 |
| Units | kg/h |
| Description | Hydrogen produced: power × 1000 × mean(`eff_at_rated`, `eff_at_min`) / `energy_per_kg_h2` |

### Master-Built Fuel Infrastructure

`master_fuel_capacity/` holds the capacity the master problem built and that is in service in the year, on top of the configured capacity: what the year's dispatch used. Written only when the master built any (primary energy on). The dispatch builds no fuel infrastructure, so `primary_energy/storage_investments` and `primary_energy/transport_investments` are no longer written.

| Dataset | Shape | Units | Description |
|---------|-------|-------|-------------|
| `storage/{fuel}` | (num_nodes,) | fuel units | Fuel storage built, per node |
| `transport/{fuel}` | (num_routes,) | units/day | Route capacity built, per `fuel_transport_routes` entry |
| `electrolyzer/{electrolyzer key}` | (num_nodes,) | MW | Electrolyzer power built (added to `rated_power`) |

Fuel names and electrolyzer keys are written as HDF5-safe names (`/` becomes ` - `, a clash gets a ` (1)` suffix), like the unit names of the per-generator groups.

### Investment Data

The dispatch builds nothing: in a `development` run the master's builds reach each year's dispatch as existing capacity, and the investments are the `investment_*` attributes of the scenario group (see [Attributes](#attributes-per-year-metadata) above). Files written by earlier versions may still hold the empty dispatch groups `gen_investment_power/`, `bat_investment_power/`, `bat_investment_capacity/`, `reservoir_invest_capacity/` and the `transfer_investment` dataset; current versions no longer write them.

### Performance Metrics

#### capacity_factor/{generator_name}

| Property | Value |
|----------|-------|
| Shape | (num_nodes, hours) |
| Type | float64 |
| Units | Fraction (0-1) |
| Description | Hourly output / rated power |

#### lcoe/{generator_name}

| Property | Value |
|----------|-------|
| Shape | (num_nodes, hours) |
| Type | float64 |
| Units | $/MWh |
| Description | Hourly operating cost per MWh (see [Derived Metrics Formulas](#derived-metrics-formulas)) |

#### vallcoe/{generator_name}

| Property | Value |
|----------|-------|
| Shape | (num_nodes, hours) |
| Type | float64 |
| Units | $/MWh |
| Description | Value-adjusted LCOE (hourly) |

### System-Level Time Series

| Dataset | Shape | Type | Units | Description |
|---------|-------|------|-------|-------------|
| `demand` | (num_nodes, hours) | float32 | MW | Gross demand (same content as `/demand/year_YYYY_base_demand`, transposed); attribute `dimensions = "nodes x hours"` |
| `reserve_static` | (num_nodes, hours) | float64 | MW | The node's static reserve requirement held as free capacity anywhere in its system |
| `reserve_dynamic` | (num_nodes, hours) | float64 | MW | The node's dynamic reserve requirement held as free capacity anywhere in its system |
| `loss_of_reserve_static` | (num_nodes, hours) | float64 | MW | Static reserve deficit: the system's shortfall, split over its nodes in proportion to their requirement |
| `loss_of_reserve_dynamic` | (num_nodes, hours) | float64 | MW | Dynamic reserve deficit, split like the static one |
| `loss_load` | (num_nodes, hours) | float64 | MW | Load shedding |
| `CO2_emissions` | (num_nodes, hours) | float64 | tCO2/h | CO2 emission rate per step; × `temporal_resolution_hours` gives the tonnes emitted in the step |
| `voltage_angle` | (num_nodes, hours) | float64 | radians | Voltage angles (max over a node's buses) |
| `voltage_magnitude` | (num_buses, hours) | float64 | p.u. | Voltage magnitude, ACOPF modes only |
| `reactive_generation` | (generators, num_buses, hours) | float64 | MVAr | Reactive power, ACOPF modes only |
| `rooftop_generation` | (hours, num_nodes) | float64 | MW | Behind-the-meter rooftop solar, already subtracted from the demand sent to the model; diagnostic only |

The detailed `demand` is always `(num_nodes, hours)` and carries `dimensions = "nodes x hours"`. Files written before this attribute existed transposed it only when there were more hours than nodes; with more nodes than hours (for example a 24-hour unit-commitment run on a 72-node system) they kept `(hours, num_nodes)`. `esfex.io.exporter.demand_node_hours(dataset, n_hours)` reads either layout as `(num_nodes, hours)`, using the number of time steps (the length of `electricity_prices`) for old files; the results viewer and `load_results()` use it.

### Economic Data

| Dataset | Shape | Units | Description |
|---------|-------|-------|-------------|
| `electricity_prices` | (hours,) | $/MWh | Mean of the nodal prices over nodes |
| `electricity_prices_energy` | (hours,) | $/MWh | Energy component: same values as `electricity_prices` |
| `nodal_electricity_prices` | (num_nodes, hours) | $/MWh | Locational marginal prices (max over a node's buses; NaN written as 0) |
| `nodal_electricity_prices_congestion` | (num_nodes, hours) | $/MWh | Congestion component: nodal price minus the energy component |

### Network Data

| Dataset | Shape | Units | Description |
|---------|-------|-------|-------------|
| `power_flow` | (num_nodes, num_nodes, hours) | MW | Flow from node i to node j, aggregated from bus-pair flows (all parallel lines summed); lines inside one node are dropped |
| `transfer_margin` | (num_nodes, num_nodes, hours) | MW | Transfer margin slack, aggregated from bus pairs like `power_flow` |
| `line_losses` | (num_nodes, hours) | MW | Transmission losses of the DC loss model, per node: half of each line's loss at each of its two end buses (as the bus balances take it), summed over the node's buses, lines inside a node included. Its sum over nodes is the system's loss in each step. Written when the dispatch has a loss model (`loss_model` `pwl` or `linear`); attribute `dimensions = "nodes x hours"` |

Losses are kept per node rather than per line: the file has no stable identity for the operational lines (buses, parallel lines, transformers), and a node-pair array like `power_flow` would add another `(num_nodes, num_nodes, hours)` array to every year. Files written before this version have no `line_losses` and no `transmission_losses`; the energy-flow Sankey then estimates the losses from the nodes' `losses` factors or the flows, as before.

With a rolling horizon, each window contributes only its committed steps (the overlap tail is dropped), as for every other time series.

### EV Data

| Dataset | Shape | Units | Description |
|---------|-------|-------|-------------|
| `EV_charging` | (num_nodes, hours) | MW | EV charging demand |
| `EV_V2G` | (num_nodes, hours) | MW | Vehicle-to-grid power |
| `EV_soc` | (num_nodes, hours) | MWh | EV fleet SOC |
| `EV_loss` | (num_nodes, hours) | MW | Unmet EV demand |

### Other

#### loss_of_inertia

| Property | Value |
|----------|-------|
| Shape | (hours,) |
| Type | float64 |
| Units | GW*s |
| Description | System inertia deficit |

#### technology_selling_prices/{generator_name}

Revenue analysis, one group per generator (despite the group name, not per technology), named like the generator's `generation/` dataset (see [Dataset Naming](#dataset-naming)). Generators that never produce are skipped.

**Attributes:**

| Attribute | Type | Description |
|-----------|------|-------------|
| `total_generation` | float | Sum of hourly output over nodes and hours (MW·step) |
| `total_revenue` | float | Sum of price × output |
| `average_selling_price` | float | `total_revenue / total_generation` ($/MWh) |
| `technology_type` | string | Generator type |

**Dataset: `prices_weights`**

| Property | Value |
|----------|-------|
| Shape | (n_entries, 3) |
| Type | float64 |
| Columns | [price ($/MWh), generation (MW), timestep] |
| Description | One row per node and hour with output above zero |

#### primary_energy/

Written when the primary energy model is active.

| Content | Kind | Description |
|---------|------|-------------|
| `total_fuel_supply/{fuel}` | dataset | Fuel supplied per period of `primary_energy_resolution` hours from the start of the year |
| `total_ne_demand_satisfied/{fuel}` | dataset | Non-electric demand served (same periods) |
| `total_loss_of_supply/{fuel}` | dataset | Unserved fuel demand (same periods) |
| `final_storage_levels/{fuel}` | dataset | Storage levels at the end of the year |
| `transport_investments/{fuel}` | dataset | Transport capacity investments |
| `storage_investments/{fuel}` | dataset | Storage capacity investments |
| `transport_flows/{fuel}` | dataset | Flows per route and period `[routes x periods]` |
| `fuel_for_power/{unit}` | dataset | Fuel burnt by each fuel-coupled unit `[nodes x steps]`, in fuel units per step (attribute `units`); `{unit}` is the unit's dataset name in the per-generator groups |
| `total_fuel_cost`, `total_transport_cost`, `total_loss_penalty` | attributes | Cost totals ($) |

Rolling-horizon windows contribute only their committed steps (the overlap with the next window is dropped, as for the dispatch arrays). The cost totals add up each window's committed steps: a period's supply and flows count for the share of its steps that the window keeps.

#### n1_security/

Written when N-1 security results exist.

| Content | Kind | Description |
|---------|------|-------------|
| `security_cost` | attribute | Cost of N-1 security ($); 0 unless `compute_security_cost` is on (see `n1_security_cost` above) |
| `gen_reserve_duals` | dataset (hours,) | Dual of the generation N-1 reserve constraint (USD/MW) |
| `trans_reserve_duals/line_{l}_outage_{k}_dir_{d}` | datasets | Duals of the transmission contingency constraints (USD/MW) |
| `binding_contingencies` | dataset (bytes) | IDs of binding contingencies |

---

## global/ and inter_system/

`global/` holds meta-network totals and is written for every run:

| Content | Description |
|---------|-------------|
| `global/summary_results/{year, total_cost, renewable_penetration, co2_emissions, loss_of_load, total_generation, total_demand, transmission_losses}` | Expandable datasets, one row per year |
| `global/cost_breakdown/year_YYYY/` | Same attributes as `/cost_breakdown/year_YYYY/` |
| `global/demand/year_YYYY_base_demand` | Same array as `/demand/year_YYYY_base_demand`, `(hours, num_nodes)` float32 |

`inter_system/detailed_results/year_YYYY_threshold_0/power_flow/{A}_to_{B}` holds the hourly flow (MW) on the lines from system A to system B (each line's bus ends are mapped to their nodes, then to their systems). It is written only for runs with more than one system.

Per-system copies of `detailed_results/` and `summary_results/` are not written; readers slice the global node axis with the `subsystem_*` root attributes.

---

## Virtual Generator and Battery Datasets

Virtual generators and batteries from technology investments appear in the HDF5 output **after** the original physical units.

### Ordering Convention

Dataset ordering within each group follows the adapter ordering:

1. **Original generators** (from `sys.generators` in config) --- e.g., "Diesel_1", "Solar_PV", "Wind"
2. **Virtual generators** (from technology investments) --- e.g., "Investment Solar PV", "Investment Wind"
3. **Original batteries** (from `sys.batteries` in config) --- e.g., "Li_Ion_1"
4. **Virtual batteries** (from battery technology investments) --- e.g., "Investment Battery"

Virtual units use the same dataset structure as their physical counterparts.

### Identifying Virtual Units

Virtual unit names start with `"Investment "`, followed by the technology's dataset name (`Investment Cuba - Solar PV` for technology `Cuba/Solar PV`). They do not appear in `system_configuration/generators/` or `batteries/`, which only list the configured units; the candidate technologies are in `system_configuration/technologies/` and `battery_technologies/`. The results dashboard, the results charts and the financial analysis cost a virtual unit with its technology's operating costs, and charge its fixed O&M, like every unit's, on the `rated_power` attribute of its `generation/` (or `battery_discharge/`) dataset.

---

## Dataset Dimensions and Data Types

| Category | Dataset Pattern | Shape | dtype | Units |
|----------|----------------|-------|-------|-------|
| Generation | `generation/{name}` | (N, H) | float64 | MW |
| Status | `gen_status/{name}` | (N, H) | float64 | units online |
| Startup | `gen_startup/{name}` | (N, H) | float64 | units started |
| Curtailment | `curtailment` | (N, H) | float64 | MW |
| Bat. charge | `battery_charge/{name}` | (N, H) | float64 | MW |
| Bat. discharge | `battery_discharge/{name}` | (N, H) | float64 | MW |
| Bat. SOC | `battery_soc/{name}` | (N, H) | float64 | MWh |
| Bat. spillage | `battery_spillage/{name}` | (N, H) | float64 | MW |
| Reservoir level | `reservoir_level/{name}` | (N, H) | float64 | MWh-eq |
| Reservoir spill | `reservoir_spillage/{name}` | (N, H) | float64 | MW-eq |
| Reservoir pump | `reservoir_pump/{name}` | (N, H) | float64 | MW |
| Electrolyzer power | `electrolyzer_power/{name}` | (N, H) | float64 | MW |
| Electrolyzer H2 | `electrolyzer_h2/{name}` | (N, H) | float64 | kg/h |
| Capacity factor | `capacity_factor/{name}` | (N, H) | float64 | 0-1 |
| LCOE | `lcoe/{name}` | (N, H) | float64 | $/MWh |
| VALLCOE | `vallcoe/{name}` | (N, H) | float64 | $/MWh |
| Demand (detailed) | `demand` | (N, H); (H, N) in older files when N ≥ H | float32 | MW |
| Demand (per year) | `/demand/year_YYYY_base_demand` | (H, N) | float32 | MW |
| Rooftop solar | `rooftop_generation` | (H, N) | float64 | MW |
| Power flow | `power_flow` | (N, N, H) | float64 | MW |
| Prices | `electricity_prices` | (H,) | float64 | $/MWh |
| Nodal prices | `nodal_electricity_prices` | (N, H) | float64 | $/MWh |
| Loss of load | `loss_load` | (N, H) | float64 | MW |
| Line losses | `line_losses` | (N, H) | float64 | MW |

Where **N** = `num_nodes` and **H** = time steps in the year (8760 at hourly resolution, 8760 / `temporal_resolution_hours` otherwise; the unit-commitment horizon in UC mode).

---

## Derived Metrics Formulas

Computed by `Orchestrator._compute_derived_metrics()` after each year's dispatch. All are hourly arrays per unit, node and time step; they are not annual scalars. Index g = generator, b = battery, n = node, t = time step.

### Capacity Factor

$$CF_{g,n,t} = \frac{P_{g,n,t}}{P^{rated}_{g,n}}$$

For batteries, the rated discharge power replaces $P^{rated}$ and discharge replaces output.

### LCOE

$$LCOE_{g,n,t} = \frac{(c^{fuel}_{g,n} + \tilde{c}^{fixed}_{g,n} + c^{maint}_{g,n}) \, P_{g,n,t} + c^{start}_{g,n} \, SU_{g,n,t}}{P_{g,n,t}}, \qquad \tilde{c}^{fixed}_{g,n} = \frac{c^{fixed}_{g,n} \, \bar{P}_{g,n} \, T / 8760}{\sum_t P_{g,n,t}}$$

for hours with output, and 0 otherwise. $c^{fuel}$, $c^{fixed}$ ($/MW·yr), $c^{maint}$ and $c^{start}$ are the generator's `fuel_cost`, `fixed_cost`, `maintenance_cost` and `start_up_cost`, $\bar{P}$ its `rated_power` and $T$ the hours of the result; the fixed O&M of those hours is spread over the energy produced in them; $SU$ is the startup variable (UC mode). When `fuel_cost` is 0, the fuel cost is taken from the fuel registry as price / energy content / efficiency. No capital cost is included.

Battery LCOE: (charging energy × nodal price / round-trip efficiency + (charge + discharge) × maintenance cost) / discharge, for hours with discharge, clipped to [0, 10000] $/MWh.

### VALLCOE (Value-Adjusted LCOE)

$$VALLCOE_{g,n,t} = LCOE_{g,n,t} \times (1 + a^{energy} + a^{capacity} + a^{flex})$$

with one set of adjustments per generator and node for the year:

- $a^{energy} = 1 - \text{capture rate}$, where capture rate = (output-weighted average nodal price) / (average demand-weighted system price).
- $a^{capacity} = 0.15 \times (0.85 - CC)$, where the capacity credit $CC$ = output in the top-10 % demand hours / (rated power × number of those hours).
- $a^{flex} = -0.1 \times$ `ramp_up` / rated power for non-renewable generators, 0 for renewables.

Battery VALLCOE uses the same energy and capacity adjustments (on discharge) and a fixed $a^{flex} = -0.05$.

---

## npv_retirements/

Written when the system setting `npv_retirement` is on (empty datasets when no unit was retired): the existing units the NPV-based retirement took out of service for the whole horizon ([Capacity Expansion §9](../formulation/capacity-expansion.md#9-npv-based-iterative-retirement)). One entry per retired unit location; all datasets have the same length.

| Dataset | Type | Description |
|---------|------|-------------|
| `kind` | str | `generator` or `battery` |
| `key` | str | Unit key in the configuration |
| `name` | str | Unit name |
| `index` | int | 0-based index in `system_configuration/generators/` (or `batteries/`), as in `retirement_gen_{g}` |
| `node` | int | 0-based node |
| `year` | int | First year the unit is out of service (the first planning year; in a myopic run, the year whose master retired it) |
| `npv` | float | The unit's NPV when it was retired ($) |
| `iteration` | int | Round of the NPV iteration that retired it |
| `capacity_mw` | float | The unit's configured MW at that node (`rated_power`, or `MaxDischargePower` for a battery) |
| `share` | float | That MW's share of the unit's configured MW over all nodes |

Attributes: `threshold` (`npv_retirement_threshold`, $) and `max_iterations`. The retired generators also count in the `retirement_gen_{g}` attributes of `detailed_results` from `year` on (their nodes fully retired), which the dashboard's retirement chart reads. Batteries have no retirement attribute; this group is where their retirements are listed. The retired units' capacity in the dispatch, and the `rated_power` (MW in service) of their `generation/` and `battery_discharge/` datasets, is 0 from `year` on.

## diagnostics/

Written at the end of the run, also when it fails. Files written before ESFEX 0.4 have no `diagnostics/` group; readers (`esfex summary`, `esfex.api`) treat a missing group as "not recorded".

### diagnostics/warnings

The WARNING and ERROR records the run logged (ESFEX's own and the Julia models' `@warn` lines; with `esfex run`, those of the configuration load too), grouped by kind: the logger plus the first line of the message with quoted names replaced by `'*'` and numbers by `#`.

| Content | Kind | Description |
|---------|------|-------------|
| `records` | compound dataset, one row per kind | Fields `time` (ISO 8601, first record of the kind), `level` (the highest: `WARNING`, `ERROR`, `CRITICAL`), `logger`, `category` (the kind), `message` (the first record's full message), `count` (int64); most frequent first. Strings are variable-length UTF-8 |
| `count_by_category` | attribute (JSON string) | `{category: count}` |
| `total` | attribute (int) | Number of records |
| `kinds` | attribute (int) | Number of kinds |

Readers also accept `records` written as a string dataset with one JSON object per row.

```python
import json, h5py

with h5py.File("isla_juventud_15012026_1030.h5", "r") as f:
    if "diagnostics/warnings" in f:
        for row in f["diagnostics/warnings/records"][()]:
            print(row["count"], row["level"].decode(), row["message"].decode())
```

Text that Julia or a solver prints directly (the solver log) is not a log record and is not collected.

### diagnostics/master

Written after the master problem (development mode, monolithic solver, not with MGA/SPORES or Benders): which constraint families bind in each master solve, what they cost at the margin and which slack or penalty variables the plan pays for. A myopic run has one group per year (`year_YYYY`, each from that year's master); a perfect-foresight run splits its single solve by the year in the rows' names (`..._y{y}`), and puts the rows without one (the operational rows of the representative days, the horizon-wide build limits) in `all`. Files written before this group existed, and runs that do not write it, simply lack it: readers must tolerate its absence. See [Why did the master build this?](../user-guide/master-problem.md#why-did-the-master-build-this-binding-constraints-and-duals) for how to read it.

Attributes of `diagnostics/master`: `schema` (`esfex.master_diagnostics/1`), `planning_mode`, `description`. Attributes of each year group: `year` (absent on `all`), `duals_available` (false when the solver returned no duals, e.g. a MIP: the binding counts then come from the tight rows alone and the dual columns are NaN) and `status` (the solver's termination status).

Each group holds three compound datasets (one row per record; strings are UTF-8):

**`binding`**: one row per constraint family (all families of the model, binding or not):

| Field | Type | Description |
|-------|------|-------------|
| `family` | str | Family id (`re_target`, `budget`, `curtailment_cap`, `adequacy`, `co2_budget`, `tech_build_limit`, `tech_capacity`, `unnamed`, `user:<name>`, …; see the [Constraint Catalog](constraint-catalog.md#families-in-the-binding-report)) |
| `label` | str | Readable name ("RE target", "Annual budget (investment)", …) |
| `code` | str | The [Constraint Catalog](constraint-catalog.md) family code (`RE-2`, `BUD-1`, …; empty when none) |
| `kind` | str | `policy`, `build_limit`, `operation`, `structural` (definitions and dynamics, always tight) or `other` |
| `units` | str | Unit of the duals (`$/MWh`, `$/MW`, `$/$`, `$/t`, `$/unit`) |
| `count_binding` | int | Rows that bind: an inequality at its bound (within 1e-6 relative) or any row with a nonzero dual; an equality only by its dual |
| `count_priced` | int | Rows with a nonzero dual (|dual| > 1e-9 in the model's M$) |
| `count_total` | int | Rows of the family in the group |
| `max_abs_dual` | float | Largest |dual| ($ per unit) |
| `sum_abs_dual` | float | Sum of the |duals| |

**`top`**: the rows with the largest |dual| outside the structural families (at most 20 per group, 5 per family, each name once): `name` (the row's name), `family`, `label`, `dual` (signed), `units`.

**`slacks`**: the slack and penalty variables with a value above 1e-6, largest cost first:

| Field | Type | Description |
|-------|------|-------------|
| `name` | str | Variable name (`slack_re_s1_y1`, `curtailment_excess_s1_y1`, `slack_budget_y1`, `slack_cap_y1_node2`, `co2_budget_violation_y1_<group>`), or for the representative days' variables a pattern `op_y{y}_d*_<kind>` (`load_shed`, `reserve_static_shortfall`, `reserve_dynamic_shortfall`, `inertia_shortfall`, `load_shed_sector_<sector>`; `sc{k}_` in front per stochastic scenario) |
| `family` | str | The family whose penalty it is (`re_target`, `budget`, …, `load_shed`) |
| `label` | str | Readable name ("RE target shortfall", "Budget overrun", …) |
| `value` | float | Amount, in `value_units`: `$` (budget overrun), `MW` (adequacy shortfall), `MWh/yr` (RE shortfall, growth-cap excess, curtailment above the cap, the representative days' shed and shortfalls scaled to the year), `t` (CO2 above the budget) |
| `value_units` | str | Unit of `value` |
| `unit_cost` | float | Price per unit of `value` as the objective charges it ($) |
| `cost` | float | `value × unit_cost` ($) |
| `count` | int | Variables summed (1 for an annual slack; the steps with a value for a representative-day kind) |

**Sign and units of the duals.** A dual is the cost of tightening the row by one unit of its right-hand side (for an equality row, of raising it): `−shadow_price` in JuMP's convention, ≥ 0 for a binding inequality of the minimised cost. Money is in $ (the master's M$ × 10⁶). A row tagged with a year is in that year's own money: the objective's discount factor `(1 + discount_rate)^-(y-1)` is taken off, so the RE target's dual compares directly with `FRE_penetration_loss` (an unattainable target binds at exactly that penalty). In the `all` group the rows of different years keep the discounting of the objective. A row with its own slack (RE target and growth cap, curtailment cap, budget, adequacy, CO2 budget) is measured per unit of that slack: the curtailment cap per MWh/yr of curtailment, although its row is scaled down by the largest day weight. The hourly rows of the representative days carry the days' weight (the days of the year each stands for), so their duals are per MW over the year. A stochastic master's scenario rows carry the scenario's probability.

```python
from esfex.io.master_diagnostics import read_master_diagnostics

groups = read_master_diagnostics("results.h5")   # {} when absent
for name, g in groups.items():
    for r in g["binding"]:
        if r["count_priced"] and r["kind"] == "policy":
            print(name, r["label"], r["max_abs_dual"], r["units"])
    for s in g["slacks"]:
        print(name, s["label"], s["value"], s["value_units"], s["cost"])
```

## MGA/SPORES Results

When near-optimal alternative generation is enabled (`master_problem.mga.enabled: true`), an `/mga/` group is added to the main results file (there is no separate MGA file). The same layout serves both methods; the `method` and `objective` attrs tell which generator produced each alternative.

```
<results>.h5
└── mga/
    ├── @method = "mga" | "spores"
    ├── @objectives = [...]            # Distinct objectives in display order
    ├── @num_alternatives
    ├── @slack_fraction
    ├── @optimal_cost
    ├── @export_timestamp
    ├── @years
    ├── generator_names                 # bytes; index of cumulative_gen_capacity axis 1
    ├── battery_names                   # bytes
    ├── alternative_0/                  # Cost-optimal seed
    │   ├── @is_optimal = True
    │   ├── @objective = "cost_optimal"
    │   ├── tech_investment             # (years, technologies, nodes)
    │   ├── bat_tech_power_investment   # (years, battery_technologies, nodes)
    │   ├── bat_tech_capacity_investment # (years, battery_technologies, nodes)
    │   ├── transfer_investment         # (n, 4) records, only if non-empty
    │   ├── cumulative_gen_capacity     # (years, generators, nodes)
    │   └── re_penetration              # (years,)
    ├── alternative_1/
    │   ├── @objective = "hsj_diversity"          # under method=mga
    │   │   or                "min_total_build"   # under method=spores
    │   └── ... (same datasets as alternative_0)
    ├── alternative_K/
    └── systems/{name}/                 # Multi-system runs only
        ├── @node_offset, @node_count
        ├── generator_names, battery_names
        └── alternative_N/              # @cost; system slices of the datasets
```

### mga/ (root attributes)

| Attribute | Type | Description |
|-----------|------|-------------|
| `method` | string | `"mga"` or `"spores"`. Older result files without it are read as `"mga"` |
| `objectives` | string[] | Distinct objectives of the non-optimal alternatives, in display order |
| `num_alternatives` | int | Total alternatives (including cost-optimal) |
| `slack_fraction` | float | Cost slack used (e.g., 0.05 = 5%) |
| `optimal_cost` | float | Cost-optimal objective value ($) |
| `export_timestamp` | string | ISO timestamp when the `/mga/` group was written |
| `years` | int[] | Planning-horizon years (same as the rest of the file) |
| `num_systems` | int | Number of systems (multi-system runs only) |

### mga/alternative_N/ (Attributes)

| Attribute | Type | Description |
|-----------|------|-------------|
| `alternative_id` | int | 0 = cost-optimal, 1..K = non-optimal alternatives |
| `is_optimal` | bool | True for the cost-optimal seed |
| `cost` | float | Actual system cost ($) |
| `diversity_objective` | float | Objective value at this alternative. For `method=mga` this is the HSJ diversity score; for `method=spores` it is the value of the SPORES objective named by `attrs["objective"]`. Absent for `alternative_0` |
| `objective` | string | Which objective produced this alternative: `"cost_optimal"` (seed), `"hsj_diversity"` (any MGA alt), or one of `"min_total_build"`, `"max_tech_equity"`, `"max_regional_equity"`, `"evolutionary_dist"` (SPORES alts). Older files without it are read as `"cost_optimal"` for the seed and `"hsj_diversity"` for the rest |

### mga/alternative_N/ (Datasets)

| Dataset | Shape | Type | Units | Description |
|---------|-------|------|-------|-------------|
| `tech_investment` | (years, technologies, nodes) | float64 | MW | New capacity per technology (order of `system_configuration/technologies/`) |
| `bat_tech_power_investment` | (years, battery_technologies, nodes) | float64 | MW | New battery power per battery technology |
| `bat_tech_capacity_investment` | (years, battery_technologies, nodes) | float64 | MWh | New battery energy per battery technology |
| `transfer_investment` | (n, 4) | float64 | MW | One row `[year, i, j, value]` per transmission investment; absent when there are none |
| `cumulative_gen_capacity` | (years, generators, nodes) | float64 | MW | Cumulative capacity per generator (with degradation/retirement) |
| `re_penetration` | (years,) | float64 | fraction | RE penetration ratio per year |

The technology axes have length 1 (all zeros) when the system has no technologies of that kind. The node axis and `i`, `j` are nodes: a bus-level master's buses are summed onto their node (transmission between two buses of one node is left out).

---

## Reading HDF5 Results

### Basic Example with h5py

```python
import h5py
import numpy as np

with h5py.File("isla_juventud_15012026_1030.h5", "r") as f:
    # --- Metadata ---
    num_years = f.attrs["num_years"]
    num_nodes = f.attrs["num_nodes"]
    print(f"Simulation: {num_years} years, {num_nodes} nodes")

    # --- Summary across all years ---
    years = f["summary_results/year"][:]
    costs = f["summary_results/total_cost"][:]
    re_pen = f["summary_results/renewable_penetration"][:]

    for y, c, r in zip(years, costs, re_pen):
        print(f"  Year {y}: cost=${c:,.0f}, RE={r:.1%}")

    # --- Detailed: generation for a specific year ---
    gen_grp = f["detailed_results/year_2030_threshold_0/generation"]
    for name in gen_grp:
        gen_output = gen_grp[name][:]          # (num_nodes, hours)
        print(f"{name}: {gen_output.sum():.0f} MW·step, fuel={gen_grp[name].attrs['fuel']}")
```

### Reading Investment Decisions

```python
import h5py

with h5py.File("isla_juventud_15012026_1030.h5", "r") as f:
    year_grp = f["detailed_results/year_2030_threshold_0"]
    techs = f["system_configuration/technologies"]

    # Scalar attributes: investment_tech_investment_power_{t}_{n}, ...
    for attr_name, value in year_grp.attrs.items():
        if attr_name.startswith("investment_tech_investment_power_"):
            t, n = map(int, attr_name.rsplit("_", 2)[-2:])
            tech_name = techs[f"technology_{t}"].attrs["name"]
            print(f"  {tech_name} at node {n}: {value:.1f} MW")
```

### Reading Battery SOC Profiles

```python
import h5py
import matplotlib.pyplot as plt

with h5py.File("isla_juventud_15012026_1030.h5", "r") as f:
    soc_grp = f["detailed_results/year_2030_threshold_0/battery_soc"]
    name = next(iter(soc_grp))
    soc = soc_grp[name][:]                   # (num_nodes, hours)

    # Plot SOC for node 0 over the first week
    hours = range(168)
    plt.figure(figsize=(12, 4))
    plt.plot(hours, soc[0, :168])
    plt.xlabel("Hour")
    plt.ylabel("SOC (MWh)")
    plt.title(f"{name} state of charge - Node 0, Week 1")
    plt.grid(True)
    plt.show()
```

### Reading Demand

```python
import h5py

with h5py.File("isla_juventud_15012026_1030.h5", "r") as f:
    hours = f["detailed_results/year_2030_threshold_0/electricity_prices"].shape[0]
    demand = f["detailed_results/year_2030_threshold_0/demand"][:]
    if demand.shape[0] == hours and demand.shape[1] != hours:
        demand = demand.T                    # -> (num_nodes, hours)

    base = f["demand/year_2030_base_demand"][:]   # always (hours, num_nodes)
```

### Reading Electricity Prices

```python
import h5py
import numpy as np

with h5py.File("isla_juventud_15012026_1030.h5", "r") as f:
    year_grp = f["detailed_results/year_2030_threshold_0"]

    # System-average prices
    prices = year_grp["electricity_prices"][:]
    print(f"Average price: ${np.mean(prices):.2f}/MWh")
    print(f"Peak price: ${np.max(prices):.2f}/MWh")

    # Nodal prices (locational marginal prices)
    nodal = year_grp["nodal_electricity_prices"][:]
    print(f"Nodal prices shape: {nodal.shape}")  # (num_nodes, hours)

    # Price spread between nodes
    price_spread = nodal.max(axis=0) - nodal.min(axis=0)
    print(f"Average nodal spread: ${np.mean(price_spread):.2f}/MWh")
```

### Reading MGA Results

```python
import h5py

with h5py.File("isla_juventud_15012026_1030.h5", "r") as f:
    if "mga" in f:
        mga = f["mga"]
        n_alts = mga.attrs["num_alternatives"]
        optimal_cost = mga.attrs["optimal_cost"]
        slack = mga.attrs["slack_fraction"]

        print(f"MGA: {n_alts} alternatives, {slack:.0%} slack")
        print(f"Optimal cost: ${optimal_cost:,.0f}")

        alt_keys = sorted((k for k in mga if k.startswith("alternative_")),
                          key=lambda s: int(s.rsplit("_", 1)[-1]))
        for k in alt_keys:
            alt = mga[k]
            cost = alt.attrs["cost"]
            tech_inv = alt["tech_investment"][:]        # (years, techs, nodes)
            bat_inv = alt["bat_tech_power_investment"][:]
            re_pen = alt["re_penetration"][:]

            print(f"\n{k}:")
            print(f"  Cost: ${cost:,.0f} ({(cost/optimal_cost - 1)*100:+.1f}%)")
            print(f"  Total technology investment: {tech_inv.sum():.0f} MW")
            print(f"  Total battery investment: {bat_inv.sum():.0f} MW")
            print(f"  Final RE penetration: {re_pen[-1]:.1%}")
```

### Reading Virtual Generator Output

```python
import h5py

with h5py.File("isla_juventud_15012026_1030.h5", "r") as f:
    gen_grp = f["detailed_results/year_2035_threshold_0/generation"]

    # List all generators (original + virtual)
    all_gens = list(gen_grp.keys())
    print(f"All generators: {all_gens}")

    # Virtual generators have the "Investment " prefix
    for name in all_gens:
        data = gen_grp[name][:]
        total = data.sum()
        if name.startswith("Investment "):
            print(f"  [VIRTUAL] {name}: {total:,.0f} MW·step")
        else:
            print(f"  [ORIGINAL] {name}: {total:,.0f} MW·step")
```

Multiply sums by `temporal_resolution_hours` to get MWh.

### With the CLI

`esfex summary results/isla_juventud_15012026_1030.h5` prints the per-year KPIs, the run's warnings and the master's binding constraints (`--json` for a JSON object; see [Using ESFEX from Scripts and Agents](../user-guide/scripts-and-agents.md)).

```bash
# Export to CSV (one folder per scenario under ./csv_results/)
esfex export -r isla_juventud_15012026_1030.h5 -f csv -o ./csv_results/

# Export to Excel: writes ./export/isla_juventud_15012026_1030.xlsx
esfex export -r isla_juventud_15012026_1030.h5 -f excel -o ./export/
```

`-o` is always a directory. The export command has no year filter. See [Results Export § Export Commands](../user-guide/results-export.md#export-commands) for what each format contains and its limitations.
