# Configuration Reference

Every configuration key ESFEX 0.3.0 accepts, generated from the Pydantic models in `esfex.config.schema` and checked against what `esfex.config.loader.load_config()` actually reads. For a guided introduction with examples, see the [Configuration Guide](../user-guide/configuration.md).

**How to read the tables**

- **Field**: the YAML key. `name` / `ALIAS` means both spellings are accepted (the Studio writes the uppercase legacy alias).
- **Type**: `list[float]` fields marked "per node" take one value per node, in node order. Ranges in parentheses are enforced at load time.
- **Default**: `required` means loading fails without it. "required by loader" means the schema has a default but the loader indexes the key directly, so a missing key fails with `Failed to load configuration: '<key>'`.

!!! warning "Unknown keys are ignored"
    The models do not forbid extra keys. A misspelled or obsolete key (for example `min_up_time` instead of `min_up`) is dropped and the default is used. The loader logs a warning with the key's full path, and `esfex validate` and the Studio's validation list it.

---

## Top-Level: ESFEXConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `simulation_mode` | `development` \| `unit_commitment` \| `economic_dispatch` | `"development"` | `development`: master problem (capacity expansion) then operational dispatch for every year. `economic_dispatch`: dispatch only (LP), no master problem. `unit_commitment`: dispatch only with binary commitment (MIP), first year only |
| `unit_commitment_hours` | int (≥ 1) | `24` | `unit_commitment` mode only: number of hours of the first year that are dispatched (capped at 8,760) |
| `date_start` | str | `"01/01/2025 00:00"` | Start timestamp `DD/MM/YYYY HH:MM` of the first data row. Labels the hourly time index (Feb 29 skipped: every model year is 8,760 h) and gives the first year label unless `esfex run --start-year` is set. It does not select rows: demand is read from the first row of the file |
| `temporal` | [TemporalConfig](#temporalconfig) | see [TemporalConfig](#temporalconfig) | Time resolution and rolling horizon |
| `solver` | [SolverConfig](#solverconfig) | see [SolverConfig](#solverconfig) | Solver backend and options |
| `network_reduction` | [NetworkReductionConfig](#networkreductionconfig) | see [NetworkReductionConfig](#networkreductionconfig) | Optional internal reduction of the bus network before solving |
| `n1_security` | [N1SecurityConfig](#n1securityconfig) | see [N1SecurityConfig](#n1securityconfig) | N-1 contingency settings |
| `master_problem` | [MasterProblemConfig](#masterproblemconfig) | see [MasterProblemConfig](#masterproblemconfig) | Capacity-expansion settings (used in `development` mode) |
| `enable_primary_energy` | bool | `true` | Include the fuel supply-chain model |
| `meta_network` | [MetaNetworkConfig](#metanetworkconfig) | required | List of systems to simulate and inter-system links |
| `systems` | dict[str, [SystemConfig](#systemconfig)] | required | Systems by name. A value can be an inline mapping or a path (string) to a system YAML file, resolved relative to the main file |
| `plugins` | dict[str, any] | `{}` | Per-plugin settings, keyed by plugin name |
| `risk` | [RiskConfig](#riskconfig) | see [RiskConfig](#riskconfig) | Risk and resilience module |
| `logging` | [LoggingConfig](#loggingconfig) | see [LoggingConfig](#loggingconfig) | Console logging verbosity |

`meta_network.systems` must name systems that exist under `systems`; otherwise loading fails.

---

## TemporalConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `resolution_hours` | int (≥ 1) | `1` | Operational time step (hours). Input rows are always hourly |
| `rolling_horizon_hours` | int (≥ 1) | `48` | Length of each operational window (hours) |
| `overlap_hours` | int (≥ 0) | `24` | Look-ahead hours appended to each window and discarded. Must be < `rolling_horizon_hours`; the default shrinks to half a window of 24 h or less. Under 24 h storage may miss the next daily peak (warning) |
| `investment_resolution` | int | `8760` | Hours per investment period: `8760` = one decision per year, `17520` = every two years. Years per period = `max(1, value ÷ 8760)` |
| `primary_energy_resolution` | int | `24` | Time step of the fuel supply-chain model (hours) |
| `use_rolling_horizon` | bool | `true` | Split each year into windows. `false` solves the year in one model |

---

## SolverConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | `highs` \| `cbc` \| `glpk` \| `gurobi` \| `cplex` \| `scip` \| `xpress` \| `clarabel` \| `scs` \| `ipopt` | `"highs"` | Solver backend. The conic (`clarabel`, `scs`) and NLP (`ipopt`) solvers serve the AC-OPF modes that need them |
| `threads` | int (≥ 1) | `4` | Solver threads |
| `time_limit` | int (≥ 0) | `10800` | Time limit per solve (seconds, `0` = unlimited) |
| `gap` | float (0–1) | `0.01` | Relative MIP gap |
| `verbose` | bool | `false` | Print solver output |
| `options` | dict[str, any] | `{}` | Solver-specific options passed to JuMP. See [Solver Guide](../user-guide/solver-guide.md) |

HiGHS options that apply to one LP method only (for example `primal_feasibility_tolerance` or `simplex_scale_strategy` for simplex, `ipm_optimality_tolerance` for IPM) are dropped with a warning unless `options.solver_method` is set explicitly to a matching method. See the [Solver Guide](../user-guide/solver-guide.md).

---

## NetworkReductionConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Reduce the internal bus network before solving (results are expanded back) |
| `kron_deg3` | bool | `false` | Also eliminate degree-3 junction buses by Kron (star-mesh) reduction |

The reduction removes only passive buses: buses with role `connection` that have no demand and host no equipment. A bus is always kept if it has a load or mixed role, if it is a terminal of an AC/DC or frequency converter, or if a generator or battery sits on it. A unit sits on a bus when that bus is named in its `bus_id_per_node` or `bus_index`, or joined to it in `transmission_lines_geo`. A unit's `bus_index` is renumbered to match the reduced bus list. The master problem reduces its network the same way when it is bus-level (a one-node system with several buses, or development zones); its investments are reported on the original buses, and the dispatch places the capacity built from them on the same buses.

---

## LoggingConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `console_level` | `basic` \| `verbose` \| `debug` | `"basic"` | Console verbosity |

---

## N1SecurityConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Master switch |
| `apply_to_modes` | list[str] | `["development", "unit_commitment"]` | Simulation modes (`simulation_mode`) in which N-1 is applied to the dispatch. A development run counts as `development` whatever formulation its windows use (`use_uc_in_dispatch`) |
| `transmission_enabled` | bool | `true` | Include line contingencies |
| `transmission_reserve_factor` | float (0–1) | `0.7` | Pre-contingency loading limit as a fraction of line rating |
| `critical_line_threshold` | float (≥ 0) | `0` | Studio SLD analysis only: minimum loading (fraction of capacity) for a line or transformer to be tripped by Run All N-1 / Screening. Not used by the optimisation. |
| `generation_enabled` | bool | `true` | Include generator contingencies |
| `generation_reserve_type` | `largest_unit` \| `percentage` | `"largest_unit"` | `largest_unit`: other units' headroom covers the largest unit's output. `percentage`: all units' headroom covers `generation_reserve_percentage` × system demand of each step. Per system in a multi-system run (each system's own units, largest unit and demand) |
| `generation_reserve_percentage` | float (0–1) | `0.15` | Reserve fraction when `generation_reserve_type: percentage` |
| `scopf_enabled` | bool | `false` | Use SCOPF instead of preventive N-1 reserves: post-contingency (LODF) flow limits for every pair of lines, in one solve, with priced overloads. |
| `corrective_enabled` | bool | `false` | With `scopf_enabled`: generation N-1 by re-dispatch of all non-renewable units within one step of ramp, plus batteries, instead of reservable units' headroom. |
| `compute_security_cost` | bool | `false` | Re-solve each operational window without the N-1 constraints and report objective(with N-1) − objective(without) as `n1_security_cost`. Roughly doubles the solve time. Off: `n1_security_cost` stays 0. |
| `contingency_depth` | `n1` \| `n1_1` | `"n1"` | Studio SLD analysis only (default depth of Run All N-1): 'n1' = single element outage, 'n1_1' = sequential double outage (N-1-1). |
| `redistribution_mode` | `pro_rata` \| `droop` | `"pro_rata"` | Studio SLD analysis only: how generation is redistributed after a contingency. 'pro_rata' distributes proportionally to capacity, 'droop' uses governor droop response characteristics. |
| `pi_screening_threshold` | float (≥ 0) | `0` | Studio SLD analysis only: Performance Index threshold for contingency screening. 0 = no screening (analyze all contingencies). Higher values filter out low-impact contingencies. |
| `transformer_contingencies` | bool | `false` | Studio SLD analysis only: include transformer outages. |
| `battery_contingencies` | bool | `false` | Studio SLD analysis only: include battery/storage outages. Discharging batteries act as generation loss. |

---

## MasterProblemConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `stochastic` | bool | `false` | Use the stochastic master (needs `stochastic_scenarios` in the system; falls back to deterministic if none are defined) |
| `representative_days` | int (≥ 1) | `5` | Representative days per year (ignored when `use_tsam: true`) |
| `min_day_separation` | int (≥ 1) | `5` | Minimum separation between representative days, in **days** (ignored when `use_tsam: true`) |
| `solver_method` | `monolithic` \| `benders` | `"monolithic"` | Master-problem solver: 'monolithic' or 'benders'. |
| `benders_max_iterations` | int (≥ 1) | `50` | Maximum Benders iterations (benders solver only). |
| `benders_tolerance` | float (> 0.0) | `0.0001` | Relative optimality-gap tolerance for Benders convergence. |
| `use_tsam` | bool | `false` | Enable TSAM clustering for representative period selection. When true, representative_days and min_day_separation are ignored. |
| `tsam_num_periods` | int (2–365) | `10` | Number of representative periods for TSAM clustering. |
| `tsam_method` | `kmedoids` \| `kmeans` | `"kmedoids"` | Clustering method for TSAM. |
| `tsam_inter_period_linking` | bool | `true` | Enable inter-period SOC linking for seasonal storage representation. |
| `use_uc_in_dispatch` | bool | `false` | Use unit commitment (binary gen_status, startup costs, min up/down times) in operational dispatch windows during development mode. Slower but more realistic operational costs. The master problem then models start-ups too (LP relaxation, see [Capacity Expansion](../formulation/capacity-expansion.md)). |
| `planning_mode` | `perfect_foresight` \| `myopic` | `"perfect_foresight"` | Investment planning mode. 'perfect_foresight' solves all years simultaneously (knows future costs, demand, and targets). 'myopic' solves year by year sequentially (only uses information available at decision time). |
| `mga` | [MGAConfig](#mgaconfig) | see [MGAConfig](#mgaconfig) | MGA near-optimal alternative generation settings |

`planning_mode`, `solver_method` and TSAM are described in [Master Problem](../user-guide/master-problem.md).

---

## MGAConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Enable MGA / SPORES to generate diverse near-optimal alternatives |
| `method` | `mga` \| `spores` | `"mga"` | Generation method. 'mga' runs the classical Hop-Skip-Jump loop num_alternatives times. 'spores' solves one alternative per objective listed in 'objectives'. |
| `objectives` | list[[SporesObjective](#sporesobjective)] | `[]` | Objective menu for method='spores'. Each entry produces one alternative under the cost slack. Ignored when method='mga'. |
| `num_alternatives` | int (1–100) | `10` | MGA only: number of diversity alternatives to generate (excludes cost-optimal). Ignored when method='spores' — len(objectives) drives the count there. |
| `slack_fraction` | float (0.0–0.5) | `0.05` | Near-optimal slack ε (e.g., 0.05 = allow up to 5% cost increase) |
| `investment_threshold` | float (≥ 0.0) | `0.1` | MW threshold to count as 'invested' for diversity scoring |

A cross-field validator enforces the method/objectives constraint when `enabled = true`:

- `method = "spores"` with empty `objectives` $\to$ `ValueError`
- `method = "mga"` with populated `objectives` $\to$ `ValueError` (probably meant to switch method)
- `enabled = false` $\to$ the validator is bypassed (so YAML drafts can keep both fields populated)

## SporesObjective

String enum of the SPORES objective menu. Each value names an LP objective installed by `apply_spores_objective!` in `mga.jl`.

| Value | Sense | Formulation tag | Description |
|-------|-------|-----------------|-------------|
| `"hsj_diversity"` | $\max$ | [MGA-4](../formulation/capacity-expansion.md#154-diversity-objective) | Classical Hop-Skip-Jump diversity score, retained for use inside a SPORES sweep |
| `"min_total_build"` | $\min$ | [SPORES-1](../formulation/capacity-expansion.md#1510-minimum-total-build-objective) | Smallest near-optimal portfolio |
| `"max_tech_equity"` | $\min$ (min-max) | [SPORES-2](../formulation/capacity-expansion.md#1511-technology-equity-objective) | Min-max over per-technology totals |
| `"max_regional_equity"` | $\min$ (min-max) | [SPORES-3](../formulation/capacity-expansion.md#1512-regional-equity-objective) | Min-max over per-node totals (the spatially-explicit objective) |
| `"evolutionary_dist"` | $\max$ | [SPORES-4](../formulation/capacity-expansion.md#1513-evolutionary-distance-objective) | L1 distance from cost-optimal plan |

---

## MetaNetworkConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `systems` | list[str] | required | Names of the systems to simulate; each must exist under `systems`. The first is the primary system |
| `systems_links` | list[[SystemLinkConfig](#systemlinkconfig)] | `[]` | Inter-system links |

---

## SystemLinkConfig

One entry of `meta_network.systems_links`. Each list holds one value per link; all lists must have the same length as `connections`. Linked systems are merged into one network. Transmission links become transmission lines in both the master problem and the operational dispatch; fuel route links become two one-way primary-energy transport routes per connection (`fuel_transport_routes`) and are not part of the electrical network.

| Field | Type | Default | Description |
|---|---|---|---|
| `systems` | list[str] | required | The two linked system names |
| `connections` | list[list[int]] | required | One `[node_in_system_1, node_in_system_2]` pair per link (0-based node indices) |
| `link_type` | `transmission` \| `fuel_route` | `transmission` | Electrical lines, or fuel transport routes for `fuel` |
| `fuel` | str | `null` | Fuel carried by a `fuel_route` link (a fuel name). A fuel route without a fuel is ignored, with a warning |
| `existing_capacity_mw` / `existing_capacity_MW` | list[float] | required | Existing capacity per link (MW). Fuel route: route capacity (fuel units/day) |
| `max_investment_mw` / `max_investment_MW` | list[float] | required | Maximum new capacity per link (MW). Fuel route: not used (route investment follows `fuel_infrastructure`) |
| `investment_cost_per_mw` / `investment_cost_per_MW` | list[float] | required | Investment cost per link ($/MW). Fuel route: not used (route investment follows `fuel_infrastructure`) |
| `loss_factor` | list[float] | `[]` | Fuel route: route `losses_fraction` (fraction per 100 km) per connection; a missing entry is 0. Transmission: not used, may be left out (losses come from `resistance_pu`) |
| `distance_km` | list[float] | required | Length per link (km) |
| `cost_per_mw_km` | list[float] | required | Transmission: operating cost of the flow, `distance_km` × `cost_per_mw_km` per MWh carried in either direction, charged by the master and the dispatch (expansion is costed with `investment_cost_per_MW`). Fuel route: route `transport_cost` ($/unit/km) |
| `reactance_pu` | list[float] | `[]` | Series reactance per link (p.u.). Empty: derived from length × `reactance_per_km` / Z_base, Z_base = `voltage_level_kv`² / `ac_power_flow.base_mva`; 0.01 without a length |
| `resistance_pu` | list[float] | `[]` | Series resistance per link (p.u.). Empty: 0 |
| `waypoints` | list[list[dict]] | `[]` | GUI-only: per-link waypoints (list of {lat, lng}) for map polyline |
| `endpoints` | list[list[dict]] | `[]` | GUI-only: per-link [from, to] endpoint refs ({element_type, element_id}) for stable map redraw |
| `voltage_kv` | list[float] | `[]` | Voltage per link (kV) |
| `line_type` | list[str] | `[]` | Construction type per link |
| `length_km` | list[float] | `[]` | Length per link (km) |
| `reactance_per_km` | list[float] | `[]` | Reactance per link (Ω/km) |
| `susceptance_pu` | list[float] | `[]` | Susceptance per link (p.u.) |
| `num_circuits` | list[int] | `[]` | Parallel circuits per link (descriptive: `existing_capacity_MW` and the impedances are per link, all circuits together) |
| `frequency_hz` | list[float] | `[]` | Frequency per link (Hz) |
| `current_type` | list[str] | `[]` | `AC` or `DC` per link |
| `decorative` | list[bool] | `[]` | Studio: draw-only link |
| `style` | list[dict] | `[]` | GUI-only: per-link {color, width, opacity} for the map polyline |

---

## SystemConfig

One entry under `systems`. The uppercase aliases are the legacy keys the Studio still writes; either spelling works.

### General and economics

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | str | required | System identifier |
| `demand_path` | str \| null | `null` | Demand file (CSV/Excel), one column per node. See [Demand Data](../user-guide/demand-data.md) |
| `demand_paths` | list[str] \| null | `null` | One demand file per node; overrides `demand_path`; length must equal the node count |
| `demand_scale` | float (> 0) | `1` | Multiplier applied to all demand |
| `discount_rate` | float (0–1) | `0.05` | Discount rate |
| `base_lcoe` | float (≥ 0) | `93` | Revenue per MWh an existing generator produces, in its NPV for the NPV-based retirement (`npv_retirement`) ($/MWh). See [Capacity Expansion §9](../formulation/capacity-expansion.md#9-npv-based-iterative-retirement) |
| `target_re_penetration` / `TARGET_RE_PENETRATION` | float (0–1) | `1` | Final-year renewable share target of a `development` run (ignored in `economic_dispatch` and `unit_commitment`). See [Master Problem](../user-guide/master-problem.md) |
| `min_annual_increment` / `MIN_ANNUAL_INCREMENT` | float (≥ 0) | `0.01` | Lower bound on the yearly increase of the RE target path |
| `max_annual_increment` / `MAX_ANNUAL_INCREMENT` | float (≥ 0) | `0.1` | Upper bound on the yearly increase of the RE target path |
| `max_annual_system_cost` / `MAX_ANNUAL_SYSTEM_COST` | float | `20000000000` | Annual investment budget cap ($) |
| `life_extension_cost_factor` | float (≥ 0) | `0.2` | Yearly cost of keeping a unit past its lifetime, as a fraction of its `invest_cost` ($/MW) |
| `npv_annual_return_rate` | float (0–1) | `0.15` | An existing battery's revenue per year in its NPV, as a fraction of its power investment cost (`invest_cost` × MW in service), for the NPV-based retirement |
| `npv_retirement` | bool | `false` | NPV-based retirement (`development` runs, every master mode): after the master solve, retire for the whole horizon the existing generators and batteries whose NPV in the plan is below `npv_retirement_threshold` (they pay their decommissioning cost in the first year and no fixed O&M), and solve the master again. Off: the master retires units by age and life extension only. See [Capacity Expansion §9](../formulation/capacity-expansion.md#9-npv-based-iterative-retirement) |
| `npv_retirement_max_iterations` | int (≥ 0) | `5` | Most rounds of retirements, each followed by a master re-solve; units still below the threshold after them are kept |
| `npv_retirement_threshold` | float | `0` | A unit whose NPV ($) is below this is retired |
| `reserve_margin` | float (≥ 1.0) | `1.15` | Capacity adequacy reserve margin (e.g. 1.15 = 15%) |

There is no `demand_growth` or `initial_re_penetration` key; both are ignored if present. The initial RE share is always computed from existing capacity.

### Operation, reserves and frequency

| Field | Type | Default | Description |
|---|---|---|---|
| `sim_rooftop` / `SIM_ROOFTOP` | bool | `false` | Simulate rooftop solar (needs `rooftop_solar_config`) |
| `inertia_limit_threshold` / `INERTIA_LIMIT_THRESHOLD` | float (≥ 0) | `0.1` | Minimum system inertia requirement in MW·s (Σ output × H), an absolute value |
| `load_damping` | float (≥ 0) | `0.01` | Load damping coefficient D (pu): fraction of demand that reduces per Hz deviation |
| `frequency_nominal` | float (> 0) | `50` | Nominal system frequency (Hz). The Studio writes the frequency of the system's AC buses when the value is none of theirs |
| `rocof_limit` | float (> 0) | `2` | Maximum allowable ROCOF before protection trips (Hz/s) |
| `frequency_nadir_limit` | float (> 0) | `49` | Minimum allowable frequency before UFLS (Hz) |
| `soc_end_tolerance` | float (0–0.5) | `0.05` | Allowed deviation (fraction) of the end level from the start level. Reservoirs: ± band at the end of every window. Batteries: the year's last window must end at ≥ (1 − tolerance) × the year-start SOC (soft) |
| `min_cycling_ratio` | float (0–1) | `0.8` | Min battery cycling as fraction of capacity |
| `min_cycling_period_days` | float (> 0) | `7` | Period for min cycling calculation (days) |
| `reserve_static_default_ratio` | float (0–1) | `0.15` | Default static reserve as fraction of demand |
| `demand_shift_cost_rate` | float (≥ 0) | `0.1` | Cost of shifting demand, $/MWh per hour moved |
| `dynamic_reserve_contribution` | float (0–1) | `0.5` | Fraction of rated_power for dynamic reserve |

### Network

| Field | Type | Default | Description |
|---|---|---|---|
| `power_flow_mode` | `dcopf` \| `dcopf_ac_verify` \| `acopf_soc` \| `acopf_qc` \| `acopf_sdp` \| `acopf_polar` \| `acopf_rect` | `"dcopf"` | Operational network formulation: `dcopf`; `dcopf_ac_verify` (DC-OPF plus a Newton-Raphson AC check); `acopf_soc`, `acopf_qc` (convex relaxations); `acopf_sdp` (SDP, needs SCS/Mosek); `acopf_polar`, `acopf_rect` (exact NLP, needs Ipopt) |
| `dc_power_flow` | [DCPowerFlowConfig](#dcpowerflowconfig) | see [DCPowerFlowConfig](#dcpowerflowconfig) | DC power flow settings |
| `ac_power_flow` | [ACPowerFlowConfig](#acpowerflowconfig) | see [ACPowerFlowConfig](#acpowerflowconfig) | AC power flow / AC-OPF settings |
| `nodes` | [NodeConfig](#nodeconfig) | required | Node network |
| `buses` | list[[BusConfig](#busconfig)] | `[]` | Electrical buses. Empty → one bus per node is created (`bus_<i>`, role `load`, `demand_fraction` 1.0) |
| `transmission_lines_geo` | list[[TransmissionLineGeo](#transmissionlinegeo)] | `[]` | Per-line data. When present, the operational bus network is built from these lines only |
| `transformers` | list[[TransformerConfig](#transformerconfig)] | `[]` | Transformers |
| `acdc_converters` | list[[ACDCConverterConfig](#acdcconverterconfig)] | `[]` | AC/DC converters |
| `freq_converters` | list[[FrequencyConverterConfig](#frequencyconverterconfig)] | `[]` | Frequency converters |

The legacy flat keys `DC_REACTANCE_PER_KM`, `DC_VOLTAGE_LEVEL_KV`, `DC_MAX_ANGLE_DIFF_DEG`, `DC_SLACK_BUS` (in any case, for example `dc_slack_bus`) are still read as fallbacks for the matching `dc_power_flow` fields.

### Equipment

| Field | Type | Default | Description |
|---|---|---|---|
| `generators` | dict[str, [GeneratorConfig](#generatorconfig)] | `{}` | Existing generators, by key |
| `batteries` | dict[str, [BatteryConfig](#batteryconfig)] | `{}` | Existing storage units, by key |
| `technologies` | dict[str, [TechnologyConfig](#technologyconfig)] | `{}` | Candidate generation technologies for the master problem, by key |
| `battery_technologies` | dict[str, [BatteryTechnologyConfig](#batterytechnologyconfig)] | `{}` | Candidate storage technologies for the master problem, by key |
| `electrolyzers` | dict[str, [ElectrolyzerConfig](#electrolyzerconfig)] | `{}` | Electrolyzers, by key. A single `electrolyzer:` mapping is also accepted |

Units may also be written as top-level system keys named `unit_<n>` or `bat_<n>` (legacy format). A unit with `type: Storage` is read as a battery, any other as a generator.

### Fuels and primary energy

| Field | Type | Default | Description |
|---|---|---|---|
| `fuels` | dict[str, [FuelConfig](#fuelconfig)] | `{}` | Fuels, by name |
| `primary_energy_sources` | dict[str, [PrimaryEnergySourceConfig](#primaryenergysourceconfig)] | `{}` | Primary energy sources, by name |
| `conversion_technologies` | dict[str, [ConversionTechnologyConfig](#conversiontechnologyconfig)] | `{}` | Fuel conversion technologies |
| `fuel_transport_distances` | list[list[float]] | `[]` | Node-to-node fuel transport distances (km) |
| `fuel_transport_routes` | list[dict] | `[]` | Fuel transport routes (written by the Studio) |
| `fuel_infrastructure` | [FuelInfrastructureConfig](#fuelinfrastructureconfig) | see [FuelInfrastructureConfig](#fuelinfrastructureconfig) | Pipelines and storage facilities |
| `fuel_entry_points` | list[[FuelEntryPointConfig](#fuelentrypointconfig)] | `[]` | Fuel import entry points |
| `non_electric_demand` | dict[str, [NonElectricDemandConfig](#nonelectricdemandconfig)] | `{}` | Non-electric fuel demands |
| `non_electric_demand_growth` | dict[str, float] | `{}` | Annual growth of non-electric demand per fuel (none if a fuel is missing) |
| `seasonal_factors` | dict[str, dict[str, list[float]]] | `{}` | Non-electric demand per month: `{fuel: {sector: [12 values]}}`, relative to their mean (even if missing) |

### Demand side, EVs and rooftop PV

| Field | Type | Default | Description |
|---|---|---|---|
| `electric_demand` | dict[str, [DemandSectorConfig](#demandsectorconfig)] | `{}` | Demand sectors, by name |
| `sector_distribution` | dict[int, dict[str, float]] | `{}` | `{node_index: {sector: share}}` |
| `criticality_penalties` | [CriticalityPenalties](#criticalitypenalties) | see [CriticalityPenalties](#criticalitypenalties) | Weights per criticality level |
| `ev_categories` | dict[str, [EVCategoryConfig](#evcategoryconfig)] | `{}` | EV categories, by name |
| `ev_quantity` | dict[str, list[int]] | `{}` | `{category: [vehicles per node]}` |
| `ev_initial_soc` / `EV_initial_soc` | list[float] | `[]` | Fleet SOC at the start of each dispatch window, per node (fraction; 0.8 if missing) |
| `base_patterns` | dict[str, list[float]] | `{}` | `{category: [24 hourly plug-in values]}` |
| `rooftop_solar_config` | [RooftopSolarConfig](#rooftopsolarconfig) \| null | `null` | Rooftop PV model settings |
| `rooftop_max_potential` | list[float \| null] | `[]` | Rooftop PV potential per node (MW); a `null` entry keeps rooftex's potential at that node |
| `rooftop_solar_emission_reduction` | float | `0.7` | Emission reduction credited to rooftop PV |

### Policy, scenarios and studies

| Field | Type | Default | Description |
|---|---|---|---|
| `penalties` | [PenaltiesConfig](#penaltiesconfig) | see [PenaltiesConfig](#penaltiesconfig) | Penalty costs (keys are case-insensitive) |
| `co2_budget` | [CO2BudgetConfig](#co2budgetconfig) | see [CO2BudgetConfig](#co2budgetconfig) | CO₂ budget |
| `stochastic_scenarios` | list[[StochasticScenarioConfig](#stochasticscenarioconfig)] | `[]` | Scenarios for `master_problem.stochastic` |
| `custom_constraints` | list[[CustomConstraintConfig](#customconstraintconfig)] | `[]` | See [Custom Constraints](../user-guide/custom-constraints.md) |
| `outage_schedule` | list[[OutageWindow](#outagewindow)] | `[]` | See [Scheduled Interruptions](../user-guide/scheduled-interruptions.md) |
| `development_zones` | list[[DevelopmentZoneConfig](#developmentzoneconfig)] | `[]` | See [Development Zones](../user-guide/development-zones.md) |

### Studio metadata

| Field | Type | Default | Description |
|---|---|---|---|
| `map_center` | [GeoCoordinate](#geocoordinate) \| null | `null` | Studio map centre |
| `map_zoom` | int \| null (1–20) | `null` | Studio map zoom |
| `gui_layout` / `_gui_layout` | dict[str, any] \| null | `null` | Studio element positions. Also used at run time to adjust generator fuel cost by distance to placed fuel storages |
| `gui_styles` / `_gui_styles` | dict[str, any] \| null | `null` | Studio metadata, ignored by the model |
| `gui_technologies` / `_technologies` | dict[str, any] \| null | `null` | Studio metadata, ignored by the model |
| `gui_equipment_buses` / `_equipment_buses` | dict[str, dict[str, str]] \| null | `null` | Studio metadata (equipment → bus_id), ignored by the model; the model uses `bus_id_per_node` |

### Computed properties

| Property | Value |
|---|---|
| `num_nodes` | `nodes.num_nodes` |
| `num_buses` | `len(buses)`; equals `num_nodes` when `buses` is empty, because one bus per node is created at load time |

---

## NodeConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `num_nodes` | int \| null | `null` | Number of nodes. Inferred as √`len(nodes_connections)` when omitted |
| `nodes_connections` | list[float] | required | Flattened N×N node adjacency matrix; entry `(i, j)` is the transfer capacity in MW, `0` = no connection |
| `reserve_static` | list[float] | `[]` | Static reserve requirement per node (MW). Empty → zeros |
| `reserve_dynamic` | list[float] | `[]` | Dynamic reserve requirement per node (MW). Empty → zeros |
| `reserve_duration` | list[int] | `[]` | Reserve duration per node (hours). Empty → ones |
| `losses` | list[float] | `[]` | Loss fraction per node. Empty → zeros |
| `transference_invest_cost` | list[float] | `[]` | Transmission investment cost per node ($/MW). A corridor between two buses costs the higher of its two ends' values, charged once per MW. Empty → zeros |
| `transference_invest_max` | list[float] | `[]` | Maximum transmission investment per node (MW). Empty → zeros |
| `node_coordinates` | list[[GeoCoordinate](#geocoordinate)] \| null | `null` | One `{latitude, longitude}` per node |
| `node_names` | list[str] \| null | `null` | Node labels |

A node is a geographic aggregation: demand files, per-node equipment arrays and reserves are indexed by node. The electrical network is made of buses (see [BusConfig](#busconfig)); every node should own at least one bus.

---

## BusConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `bus_id` | str \| null | `null` | Bus identifier; must be unique within the system. Referenced by `bus_id_per_node` |
| `name` | str | `""` | Display name |
| `parent_node` | int (≥ 0) | `0` | Index of the node this bus belongs to (0-based) |
| `voltage_kv` | float (> 0) | `220` | Nominal voltage (kV) |
| `frequency_hz` | float (> 0) | `50` | Frequency (Hz) |
| `current_type` | `AC` \| `DC` | `"AC"` | `AC` or `DC`. Any other value is replaced by `AC` with a warning |
| `bus_type` | `PQ` \| `PV` \| `slack` | `"PQ"` | Power-flow bus type. A bus with `slack` overrides `dc_power_flow.slack_bus` |
| `role` | `connection` \| `load` \| `mixed` | `"load"` | `load`: carries demand. `connection`: junction only, no demand and no load shedding. `mixed`: carries demand and connects equipment |
| `demand_fraction` | float | `1` | Share of the parent node's demand placed on this bus (`load`/`mixed`). Must be `0` for `connection`, `>= 0` otherwise. Rescaled per node to sum to 1 (with a warning) |

Validation at load time:

- `role: connection` requires `demand_fraction: 0`; `load` and `mixed` require `demand_fraction >= 0`.
- `bus_id` values must be unique within a system.
- If `buses` is empty, one bus per node is created: `bus_id: bus_<i>`, `name: Bus <i>`, `parent_node: i`, `role: load`, `demand_fraction: 1.0`, `voltage_kv: 220`.

How the model uses buses:

- **Demand.** Each `load` or `mixed` bus receives `node_demand × demand_fraction` of its parent node every hour; a `connection` bus receives none. The fractions of a node's buses are rescaled at load time to sum to 1, with a warning (two `load` buses left at the default `1.0` become `0.5` each). A node whose buses all have `demand_fraction: 0` gets no demand; the loader warns about it.
- **Load shedding.** Load shed on a bus is bounded by the demand on that bus, so a `connection` bus cannot shed load.
- **Equipment placement.** When the bus list is not exactly one bus per node in node order, per-node equipment arrays are mapped onto buses. Capacity at a node goes to the bus given by `bus_id_per_node`, else by a generator/battery endpoint of a line in `transmission_lines_geo`, else by `bus_index`, else to the node's first bus in list order. Per-node properties such as efficiencies, ramps and costs are copied to every bus of the node.
- A node with no bus cannot serve its demand or host capacity. When there are fewer buses than nodes, the bridge logs a warning naming the bus-less nodes.

See [Nodes and buses](../user-guide/configuration.md#nodes-and-buses) in the guide for an example.

---

## TransmissionLineGeo

| Field | Type | Default | Description |
|---|---|---|---|
| `line_id` | str \| null | `null` | Line identifier |
| `from_node` | int (≥ 0) | required | Origin node (0-based) |
| `to_node` | int (≥ 0) | required | Destination node (0-based) |
| `from_bus` | int \| null | `null` | Origin bus (0-based index into `buses`). **Without it (or a `bus` endpoint) the line is skipped** |
| `to_bus` | int \| null | `null` | Destination bus (0-based). **Without it (or a `bus` endpoint) the line is skipped** |
| `capacity_mw` | float \| null | `null` | Thermal rating of the whole line, all its circuits together (MW). Missing → 0 |
| `waypoints` | list[[GeoCoordinate](#geocoordinate)] | `[]` | Intermediate geographic waypoints for line routing |
| `voltage_kv` | float \| null | `null` | Voltage (kV). Missing → `dc_power_flow.voltage_level_kv` |
| `line_type` | `overhead` \| `underground` \| `submarine` \| null | `null` | `overhead`, `underground` or `submarine` |
| `length_km` | float \| null | `null` | Length (km) |
| `reactance_pu` | float \| null | `null` | Reactance of the whole line (p.u. on `ac_power_flow.base_mva`). Missing → length × `reactance_per_km` / Z_base / `num_circuits` with Z_base = V² / `base_mva` (V = `voltage_kv`, else `voltage_level_kv`); 0.01 / `num_circuits` without a length |
| `reactance_per_km` | float (≥ 0) \| null | `null` | Reactance of one circuit of this line (Ω/km) for the derivation above; null or 0 = `dc_power_flow.reactance_per_km` |
| `resistance_pu` | float \| null | `null` | Resistance of the whole line (p.u.). Missing → 0 |
| `susceptance_pu` | float \| null | `null` | Susceptance of the whole line (p.u.). Missing → 0 |
| `num_circuits` | int | `1` | Parallel circuits. Descriptive: `capacity_mw` and the impedances above already include every circuit, so the model does not multiply or divide them by this count. It only divides a reactance derived from `reactance_per_km` |
| `cost_per_mw_km` | float (≥ 0) | `0` | Operating cost of the flow ($ per MWh and km): the master and the dispatch (DC power flow, not ACOPF) charge `length_km` × `cost_per_mw_km` × \|flow\|. Set by a multi-system run on its inter-system links |
| `frequency_hz` | float (> 0) | `50` | Operating frequency (Hz) |
| `current_type` | `AC` \| `DC` | `"AC"` | Current type |
| `from_endpoint_type` | str \| null | `null` | Studio endpoint reference (`bus`, `generator`, `battery`, `transformer`, ...) |
| `from_endpoint_id` | str \| null | `null` | Studio endpoint id. A `bus` endpoint fills a missing `from_bus` |
| `to_endpoint_type` | str \| null | `null` | Studio endpoint reference |
| `to_endpoint_id` | str \| null | `null` | Studio endpoint id. A `bus` endpoint fills a missing `to_bus` |

A line enters the operational model only if both bus indices are known: `from_bus`/`to_bus`, or a `bus` endpoint reference (`from_endpoint_type: bus` with `from_endpoint_id: <bus_id>`). Lines without them are skipped with a warning. Prefer explicit `from_bus`/`to_bus`: the bus-to-bus capacity matrix reads only those two fields, not the endpoint references. When `transmission_lines_geo` is not empty, the bus-to-bus capacity matrix is the sum of line capacities between bus pairs, and `nodes.nodes_connections` is not used for the operational network. The exception is a system without lines merged with others (inter-system links or several systems): its `nodes_connections` become lines between the first buses of the connected nodes.

---

## TransformerConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | str | required | Transformer name |
| `from_node` | int (≥ 0) | required | HV-side node index (0-indexed) |
| `to_node` | int (≥ 0) | required | LV-side node index (0-indexed) |
| `from_bus` | int \| null | `null` | HV-side bus index (0-based) |
| `to_bus` | int \| null | `null` | LV-side bus index (0-based) |
| `from_voltage_kv` | float (> 0) | required | HV-side voltage (kV) |
| `to_voltage_kv` | float (> 0) | required | LV-side voltage (kV) |
| `rated_power_mva` | float (> 0) | required | Rating (MVA) |
| `impedance_pu` | float (> 0) | `0.1` | Series impedance (p.u.) |
| `resistance_pu` | float \| null | `null` | Series resistance (p.u.), derived from losses if None |
| `losses_fraction` | float (0–1) | `0.005` | Load-loss fraction |

---

## ACDCConverterConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | str | required | Converter name |
| `converter_type` | `VSC` \| `LCC` | `"VSC"` | VSC or LCC topology |
| `from_node` | int (≥ 0) | required | AC-side node index (0-indexed) |
| `to_node` | int (≥ 0) | required | DC-side node index (0-indexed) |
| `from_bus` | int \| null | `null` | Bus index on the `from` side (0-based) |
| `to_bus` | int \| null | `null` | Bus index on the `to` side (0-based) |
| `from_voltage_kv` | float (> 0) | `220` | AC-side voltage (kV) |
| `dc_voltage_kv` | float (> 0) | `320` | DC-side voltage (kV) |
| `rated_power_mva` | float (> 0) | `100` | Rating (MVA) |
| `min_power_mva` | float (≥ 0) | `0` | Minimum operating power (MW) in either direction. With unit commitment in the dispatch (`unit_commitment` mode or `use_uc_in_dispatch`) the converter is off or carries at least this much each step; ignored in economic dispatch and in the master problem (LP) |
| `efficiency_rectify` | float (> 0, ≤ 1) | `0.98` | AC→DC efficiency |
| `efficiency_invert` | float (> 0, ≤ 1) | `0.98` | DC→AC efficiency |
| `standby_losses_mw` | float (≥ 0) | `0.5` | Standby losses (MW) |
| `reactive_power_min_mvar` | float | `-50` | Minimum Q (MVAr) |
| `reactive_power_max_mvar` | float | `50` | Maximum Q (MVAr) |
| `power_factor` | float (> 0, ≤ 1) | `1` | Power factor |
| `impedance_pu` | float (> 0) | `0.05` | Series impedance (p.u.) |
| `resistance_pu` | float (≥ 0) | `0.01` | Series resistance (p.u.) |
| `invest_cost` | float (≥ 0) | `0` | $/MW |
| `fixed_cost` | float (≥ 0) | `0` | $/MW/year |
| `variable_cost` | float (≥ 0) | `0` | $/MWh |
| `invest_max_power` | float (≥ 0) | `0` | Max investment (MW) |
| `life_time` | int (≥ 1) | `30` | Lifetime (years) |
| `initial_age` | int (≥ 0) | `0` | Age at start (years) |
| `degradation_rate` | float (≥ 0) | `0.005` | Degradation (fraction/year) |

---

## FrequencyConverterConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | str | required | Converter name |
| `from_node` | int (≥ 0) | required | Frequency-A bus (0-indexed) |
| `to_node` | int (≥ 0) | required | Frequency-B bus (0-indexed) |
| `from_bus` | int \| null | `null` | Bus index on the `from` side (0-based) |
| `to_bus` | int \| null | `null` | Bus index on the `to` side (0-based) |
| `from_frequency_hz` | float (> 0) | `50` | Frequency on side A (Hz) |
| `to_frequency_hz` | float (> 0) | `60` | Frequency on side B (Hz) |
| `rated_power_mva` | float (> 0) | `100` | Rating (MVA) |
| `min_power_mva` | float (≥ 0) | `0` | Minimum operating power (MW) in either direction. With unit commitment in the dispatch (`unit_commitment` mode or `use_uc_in_dispatch`) the converter is off or carries at least this much each step; ignored in economic dispatch and in the master problem (LP) |
| `efficiency_a_to_b` | float (> 0, ≤ 1) | `0.98` | Efficiency A → B |
| `efficiency_b_to_a` | float (> 0, ≤ 1) | `0.98` | Efficiency B → A |
| `standby_losses_mw` | float (≥ 0) | `0.5` | Standby losses (MW) |
| `reactive_power_min_mvar` | float | `-50` | Minimum Q (MVAr) |
| `reactive_power_max_mvar` | float | `50` | Maximum Q (MVAr) |
| `impedance_pu` | float (> 0) | `0.05` | Series impedance (p.u.) |
| `resistance_pu` | float (≥ 0) | `0.01` | Series resistance (p.u.) |
| `invest_cost` | float (≥ 0) | `0` | $/MW |
| `fixed_cost` | float (≥ 0) | `0` | $/MW/year |
| `variable_cost` | float (≥ 0) | `0` | $/MWh |
| `invest_max_power` | float (≥ 0) | `0` | Max investment (MW) |
| `life_time` | int (≥ 1) | `30` | Lifetime (years) |
| `initial_age` | int (≥ 0) | `0` | Age at start (years) |
| `degradation_rate` | float (≥ 0) | `0.005` | Degradation (fraction/year) |

---

## DCPowerFlowConfig

Nested under `dc_power_flow`. There is no `enabled` switch; the network formulation is chosen with `power_flow_mode`.

| Field | Type | Default | Description |
|---|---|---|---|
| `reactance_per_km` | float | `0.4` | Line reactance (Ω/km) used when a line has no `reactance_pu` |
| `voltage_level_kv` | float | `220` | Voltage (kV) used to derive the reactance of lines without `voltage_kv` |
| `max_angle_diff_deg` | float (0–90) | `30` | Maximum angle difference (degrees). Used by the AC-OPF formulations only |
| `slack_bus` | int (≥ 0) | `0` | Reference bus index (0-based), unless a bus has `bus_type: slack` |
| `loss_model` | `none` \| `linear` \| `pwl` | `"pwl"` | `none`, `linear` or `pwl` (piecewise-linear quadratic losses) |
| `pwl_loss_segments` | int (1–10) | `3` | PWL loss segments in operational dispatch |
| `pwl_loss_segments_master` | int (1–5) | `2` | PWL loss segments in the master problem |

---

## ACPowerFlowConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Run the AC power-flow check |
| `max_iterations` | int (≥ 1) | `50` | Newton-Raphson iteration limit |
| `tolerance` | float (> 0) | `1e-06` | Convergence tolerance |
| `base_mva` | float (> 0) | `100` | System power base (MVA) of all per-unit line and transformer data; the DC flow (MW/rad) and losses use it too, whatever `power_flow_mode` is |
| `voltage_min_pu` | float (> 0, ≤ 1.0) | `0.9` | Minimum voltage (p.u.) |
| `voltage_max_pu` | float (≥ 1.0) | `1.1` | Maximum voltage (p.u.) |
| `check_hours` | `all` \| `peak` \| `sample` | `"peak"` | Hours checked: `all`, `peak` or `sample` |
| `sample_count` | int (≥ 1) | `24` | Hours sampled when `check_hours: sample` |
| `default_power_factor` | float (> 0, ≤ 1.0) | `0.85` | Generator power factor used to estimate Q limits |
| `load_power_factor` | float (> 0, ≤ 1.0) | `0.9` | Load power factor for reactive demand |
| `q_slack_penalty` | float (≥ 0) | `100` | Reactive-power slack penalty ($/MVAr) |
| `min_reactance_pu` | float (> 0) | `0.01` | Branch reactances below this are clamped (p.u.) |
| `tap_ratio_min` | float (> 0) | `0.5` | Transformer taps below this are reset to 1.0 |
| `tap_ratio_max` | float (> 0) | `2` | Transformer taps above this are reset to 1.0 |
| `q_min_ratio` | float (0–1.0) | `0.5` | `Q_min = -ratio × Q_max` when Q limits are not given |

---

## GeneratorConfig

Existing generating units, under `generators: {<key>: {...}}` or as legacy `unit_<n>` keys. All `list` fields are per node. The loader reads the keys listed below; required keys missing from a unit abort loading with `Failed to load configuration: '<key>'`.

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | str | required by schema; loader default: unit key | Display name. Defaults to the unit key |
| `type` | `Renewable` \| `Non-renewable` \| `Storage` \| `Electrolyzer` | required | `Renewable`, `Non-renewable` or `Electrolyzer` (case-insensitive; translated Studio labels accepted). Any other value is logged and treated as `Non-renewable` |
| `fuel` | str | required | Fuel name; links to `fuels` for cost and emissions (`Sun`, `Wind`, `Water` for renewables) |
| `technology` | str \| null | `null` | Key of a `technologies` entry; its `eff_at_rated` is the fallback efficiency for fuel-cost linking |
| `reservable` | bool | `true` | Can provide reserves |
| `life_time` | list[int] | required | Lifetime per node (years) |
| `initial_age` | list[int] | required | Age at the start of the horizon per node (years) |
| `degradation_rate` | list[float] | required | Capacity degradation per node (fraction/year, `0.01` = 1 %) |
| `decommissioning_cost` | list[float] | required | Decommissioning cost per node ($/MW) |
| `rated_power` | list[float] | required | Installed capacity per node (MW) |
| `num_units` | list[int] (≥ 1) | `[1]` | Identical units the capacity at each node is made of. Unit commitment commits a whole number of them, each of `rated_power / num_units` MW with its own minimum output, start-ups and min up/down times; the availability is the share of units available ([GEN-6](../formulation/operational-dispatch.md#gen-6-plants-of-several-units-clustered-unit-commitment)). `1` = one on/off unit. Ignored by economic dispatch and the master problem |
| `min_power` | list[float] | required | Minimum stable output per node, fraction of rated power, of each unit's rating when `num_units` > 1 (applied with unit commitment). Outside 0–1 the loader clamps it with a warning |
| `min_up` | list[int] | required | Minimum up time per node (hours, unit commitment; rounded up to whole time steps) |
| `min_down` | list[int] | required | Minimum down time per node (hours, unit commitment; rounded up to whole time steps) |
| `ramp_up` | list[float] | required | Ramp-up limit per node, fraction of capacity per hour (scaled to `resolution_hours`). `0` = no limit; a negative value is clamped to 0 with a warning |
| `ramp_down` | list[float] | required | Ramp-down limit per node, fraction of capacity per hour (scaled to `resolution_hours`). `0` = no limit; a negative value is clamped to 0 with a warning |
| `eff_at_rated` | list[float] | required | Efficiency at rated output per node (0–1) |
| `eff_at_min` | list[float] | required | Efficiency at minimum output per node (0–1) |
| `inertia` | list[float] | required | Inertia constant H per node (s) |
| `start_up_cost` | list[float] | required | Start-up cost per node ($ per start of the node's whole capacity); one of `num_units` units costs `start_up_cost / num_units` per start |
| `fuel_cost` | list[float] | required | Fuel cost per node ($/MWh). Required key; if all zero and `fuel` is in `fuels` (any type, a `Renewable` biomass unit too), the loader computes it (see [Loader](../api/config-loader.md#fuel-cost-linking-and-non-fuel-om-adder)) |
| `fixed_cost` | list[float] | required | Fixed O&M per node ($ per MW of power in service per year), charged whether or not the unit runs: degraded rated power while within `life_time`, prorated to each dispatch window; the master charges it per year on the same MW (life-extended MW after `life_time`) |
| `maintenance_cost` | list[float] | required | Variable O&M per node ($/MWh). The loader may add a non-fuel adder for thermal fuels (see [Loader](../api/config-loader.md#fuel-cost-linking-and-non-fuel-om-adder)) |
| `invest_cost` | list[float] | required by loader | Per-node $/MW. Required by the loader. Investment in new capacity is modelled with `technologies`; this value prices life extension (× `life_extension_cost_factor`). Where it is 0, the technology with the same `fuel` and `type` supplies it |
| `frequency_hz` | float (> 0) | `50` | Operating frequency (Hz) |
| `current_type` | `AC` \| `DC` \| `AC_DC` | `"AC"` | `AC`, `DC` or `AC_DC` |
| `Availability` | str \| null | `null` | YAML key `Availability`: availability profile (CSV/Excel). See [Availability Profiles](../user-guide/availability-profiles.md) |
| `bus_index` | int \| null | `null` | 0-based bus index; places this unit's capacity at the node that owns that bus |
| `bus_id_per_node` | dict[int, str] \| null | `null` | `{node_index: bus_id}`: the bus that holds this unit's capacity at each node. Takes precedence over `bus_index` |

**Type handling.** The loader accepts `Renewable`, `Non-renewable` and `Electrolyzer` (and `Storage`, which turns a legacy `unit_*`/`bat_*` entry into a battery). Case and surrounding spaces are ignored, and the labels saved by a translated Studio (`Renovable`, `No renovable`, `Renouvelable`, `Renovável`, `再生可能`, ...) are read as their English value. Any other value, such as `Thermal`, is logged as a warning and replaced by `Non-renewable`.

**Frequency response, cost curves, reactive limits and risk:**

| Field | Type | Default | Description |
|---|---|---|---|
| `droop` | list[float] | `[0.05]` | Governor droop characteristic per node (pu), e.g. 0.05 = 5%. |
| `governor_time_const` | list[float] | `[5.0]` | Governor time constant per node (seconds). |
| `fuel_cost_curve` | list[[CostCurveConfig](#costcurveconfig)] \| null | `null` | Per-node bidding/offer curve for fuel cost. Overrides flat fuel_cost when present. |
| `q_max_mvar` | list[float] | `[]` | Max reactive power per node (MVAr). |
| `q_min_mvar` | list[float] | `[]` | Min reactive power per node (MVAr). |
| `power_factor` | float (> 0, ≤ 1) | `0.85` | Power factor for Q limit estimation when q_max/q_min not specified. |
| `risk_coefficient` | list[float] | `[1.0]` | Per-node geographic risk derating factor (0-1). Computed from hazard exposure and component fragility. 1.0 = no risk derating; 0.82 = 18% capacity derating. |

### Reservoir hydro

Empty lists mean no reservoir. With `reservoir_capacity` set, the unit is dispatched against stored water energy with inflow, turbining, pumping, spillage and evaporation. See [Reservoir Hydro](../formulation/reservoir-hydro.md).

| Field | Type | Default | Description |
|---|---|---|---|
| `reservoir_capacity` | list[float] | `[]` | Reservoir capacity per node (MWh) |
| `reservoir_initial_level` | list[float] | `[]` | Initial level fraction (0-1) |
| `reservoir_min_level` | list[float] | `[]` | Min level fraction (0-1) |
| `reservoir_max_level` | list[float] | `[]` | Max level fraction (0-1) |
| `reservoir_inflow_file` | str \| null | `null` | Inflow time series file (MW-eq) |
| `reservoir_turbine_efficiency` | list[float] | `[]` | Turbine efficiency (0-1) |
| `reservoir_evaporation_rate` | list[float] | `[]` | Hourly evaporation rate |
| `reservoir_pump_capacity` | list[float] | `[]` | Pump capacity per node (MW) |
| `reservoir_pump_efficiency` | list[float] | `[]` | Pump efficiency (0-1) |
| `reservoir_spillage_allowed` | bool | `true` | Allow reservoir spillage |
| `reservoir_invest_cost` | list[float] | `[]` | Reservoir invest cost ($/MWh) |
| `reservoir_invest_max` | list[float] | `[]` | Max reservoir expansion (MWh) |
| `reservoir_min_release` | list[float] | `[]` | Mandatory minimum reservoir release per node (MW-eq) — ecological / minimum environmental flow. 0 = none. |
| `cascade_downstream` | str | `""` | Name of the downstream reservoir generator this unit discharges into (hydraulic cascade). Empty = terminal reservoir. |
| `cascade_delay_hours` | int (≥ 0) | `0` | Water travel time (hours) before this unit's release reaches its cascade_downstream reservoir. |
| `reservoir_head_min_factor` | list[float] | `[]` | Per-node turbine power-availability factor at the minimum reservoir level (0-1]. 1.0 = no head effect; below 1.0 the available power scales linearly with the fill level (head dependence). |

The minimum-release, cascade and head-dependence features exist in the model but cannot currently be set from a YAML file, because the loader does not copy those four fields.

---

## BatteryConfig

Existing storage units, under `batteries: {<key>: {...}}` or as legacy `bat_<n>`/`unit_<n>` keys with `type: Storage`. All `list` fields are per node. Required keys missing from a unit abort loading.

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | str | required by schema; loader default: unit key | Display name. Defaults to the unit key |
| `type` | `Storage` | `"Storage"` | Always `Storage`. Under a legacy `bat_*`/`unit_*` key the unit must say `type: Storage` |
| `fuel` | str | `"None"` | Normally `None` |
| `technology` | str \| null | `null` | Battery technology (key or name). Where `invest_cost` is 0, its `invest_cost_power` prices life extension (no match: the system's first battery technology) |
| `reservable` | bool | `true` | Its headroom counts towards the static and dynamic reserve (and the preventive N-1 cover) |
| `spillage` | bool | `true` | Allow energy spillage |
| `min_duration_hours` | int \| null | `null` | Minimum energy-to-power ratio (hours, integer) |
| `max_duration_hours` | int \| null | `null` | Maximum energy-to-power ratio (hours, integer) |
| `life_time` | list[int] | required | Lifetime per node (years) |
| `initial_age` | list[int] | required | Age at the start of the horizon per node (years) |
| `degradation_rate` | list[float] | required | Capacity degradation per node (fraction/year, `0.01` = 1 %) |
| `decommissioning_cost` | list[float] | required | Decommissioning cost per node ($/MW) |
| `rated_power` | list[float] | required | Power rating per node (MW) |
| `ramp_up` | list[float] | `[0.0]` | How fast charge or discharge power may rise, per node: fraction of `MaxChargePower` / `MaxDischargePower` per hour (× `resolution_hours` per step). 0 = no limit |
| `ramp_down` | list[float] | `[0.0]` | How fast charge or discharge power may fall, per node, as `ramp_up` |
| `inertia` | list[float] | required | Inertia constant H per node (s) |
| `fixed_cost` | list[float] | `[0.0]` | Fixed O&M per node ($ per MW of `MaxDischargePower` per year) |
| `maintenance_cost` | list[float] | required | Variable O&M per node ($/MWh) |
| `throughput_degradation_cost` | list[float] \| null | `null` | Cycling cost per MWh discharged, per node ($/MWh). `null` means 0 |
| `discharge_cost_curve` | list[[CostCurveConfig](#costcurveconfig)] \| null | `null` | Per-node discharge cost curve. Overrides flat throughput_degradation_cost when present. |
| `invest_cost` | list[float] | required by loader | Power investment cost per node ($/MW). Required by the loader; prices life extension. Where it is 0, the battery technology named by `technology` (else the first one) supplies `invest_cost_power` |
| `invest_max_power` | list[float] | required by loader | Per-node MW. Required by the loader. A value > 0 places the battery at the node in the master's representative days even where it has no power yet; new storage is built from `battery_technologies` |
| `efficiency_charge` | list[float] | required | Charge efficiency per node (0–1) |
| `efficiency_discharge` | list[float] | required | Discharge efficiency per node (0–1) |
| `soc_initial` | list[float] | required | Initial state of charge per node (fraction) |
| `max_DoD` | list[float] | required | Maximum depth of discharge per node (fraction) |
| `capacity` | list[float] | required | Energy capacity per node (MWh) |
| `MaxChargePower` | list[float] | required | Maximum charge power per node (MW) |
| `MaxDischargePower` | list[float] | required | Maximum discharge power per node (MW) |
| `current_type` | `AC` \| `DC` | `"DC"` | `AC` or `DC` |
| `Availability` / `availability_file` | str \| null | `null` | Availability profile file ([Availability Profiles](../user-guide/availability-profiles.md)): each hour's factor (0–1) multiplies the charge and discharge power, and the power the battery offers as reserve |
| `bus_index` | int \| null | `null` | 0-based bus index; places this unit's capacity at the node that owns that bus |
| `bus_id_per_node` | dict[int, str] \| null | `null` | `{node_index: bus_id}`: the bus that holds this unit's capacity at each node |
| `risk_coefficient` | list[float] | `[1.0]` | Per-node geographic risk derating factor (0-1). Computed from hazard exposure and component fragility. 1.0 = no risk derating; 0.82 = 18% capacity derating. |

The ramp limits, fixed cost, `reservable` and availability profile apply in the operational dispatch and in the master problem alike. The fixed cost is charged on the discharge power in service each year in the master and, prorated to the window's share of the year, in each dispatch window. Projects saved by earlier versions may still hold the generator fields `min_power`, `min_up`, `min_down`, `eff_at_rated`, `eff_at_min`, `start_up_cost` and `fuel_cost` on batteries: they had no effect, and the loader now reports them as unknown keys.

---

## CostCurveConfig

Offer curve for one node; a curve list has one entry per node.

A generator `fuel_cost_curve` or battery `discharge_cost_curve` replaces the flat `fuel_cost` / `throughput_degradation_cost` of that unit in both the operational dispatch and the master problem. On candidate technologies (`technologies.<key>.fuel_cost_curve`) the curve prices the technology's output in the master problem, and the capacity built from the technology keeps it in the operational dispatch.

| Field | Type | Default | Description |
|---|---|---|---|
| `curve_type` | `flat` \| `linear` \| `stepwise` \| `exponential` | `"flat"` | Curve shape |
| `flat_price` | float \| null (≥ 0) | `null` | `flat`: price ($/MWh). Missing → the scalar `fuel_cost` |
| `blocks` | list[[CostCurveBlock](#costcurveblock)] \| null | `null` | `stepwise`: list of `{fraction, price}` |
| `price_at_zero` | float \| null (≥ 0) | `null` | `linear`: price at zero output. Missing → `fuel_cost` |
| `price_at_max` | float \| null (≥ 0) | `null` | `linear`: price at full output. Missing → `fuel_cost` |
| `base_price` | float \| null (≥ 0) | `null` | `exponential`: base price. Missing → `fuel_cost` |
| `scale_factor` | float \| null (≥ 0) | `null` | `exponential`: exponent scale. Missing → 1.0 |
| `num_segments` | int (2–20) | `5` | Segments used to discretise `linear` and `exponential` curves |

### CostCurveBlock

One entry of `blocks`.

| Field | Type | Default | Description |
|---|---|---|---|
| `fraction` | float (0–1) | required | Share of rated capacity covered by the block |
| `price` | float (≥ 0) | required | Marginal cost of the block ($/MWh) |

**How curves are discretised** (`normalize_cost_curve`):

- **flat**: one block covering 100 % of capacity at `flat_price`.
- **stepwise**: the blocks as given. If the fractions do not sum to 1 (±0.01), they are rescaled to sum to 1; no error is raised.
- **linear**: `num_segments` equal blocks, each priced at the curve value at the block mid-point: `price_at_zero + (price_at_max − price_at_zero) × (k + 0.5) / n`.
- **exponential**: `num_segments` equal blocks priced at `base_price × exp(scale_factor × (k + 0.5) / n)`.

The Julia model creates one segment variable per block, bounded by `fraction × capacity`, and prices each segment at its block price. A curve that reduces to a single block prices the whole output (discharge) at that block's price; a flat curve at the unit's own `fuel_cost` / `throughput_degradation_cost` is skipped, since it changes nothing.

**Example: linear curve on a candidate technology**

```yaml
technologies:
  ccgt_new:            # other technology fields omitted
    fuel_cost_curve:
      - curve_type: linear
        price_at_zero: 120.0
        price_at_max: 200.0
        num_segments: 4
```

Gives 4 blocks of 25 % at 130, 150, 170 and 190 $/MWh.

**Example: exponential curve**

```yaml
technologies:
  peaker_new:          # other technology fields omitted
    fuel_cost_curve:
      - curve_type: exponential
        base_price: 100.0
        scale_factor: 1.5
        num_segments: 5
```

Gives 5 blocks of 20 % at about 116, 156, 211, 285 and 386 $/MWh.

---

## TechnologyConfig

Candidate generation technology for the master problem, under `technologies: {<key>: {...}}`. Passed to the schema unchanged, so every field below is read.

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | str | required by schema; loader default: technology key | Display name. Defaults to the technology key |
| `type` | `Renewable` \| `Non-renewable` | required | `Renewable` or `Non-renewable` (case-insensitive; translated Studio labels accepted). Other values are a validation error |
| `fuel` | str | required | Fuel name (links to `fuels` and to availability) |
| `invest_cost` | list[float] | required | Investment cost per node ($/MW) |
| `invest_max_power` | list[float] | required | Maximum investment per node (MW) |
| `Availability` | str \| null | `null` | YAML key `Availability` (or `availability_file`): availability profile |
| `eff_at_rated` | list[float] | required | Efficiency at rated output per node |
| `degradation_rate` | list[float] | required | Degradation per node (fraction/year) |
| `lifetime` | int | required | Economic lifetime (years, scalar) |
| `min_output` | list[float] | `[0.0]` | Minimum output per node (fraction) |
| `ramp_up` | list[float] | `[1.0]` | Ramp-up limit per node (fraction of capacity per hour, scaled to `resolution_hours`; `0` = no limit) |
| `ramp_down` | list[float] | `[1.0]` | Ramp-down limit per node (fraction of capacity per hour, scaled to `resolution_hours`; `0` = no limit) |
| `fuel_cost` | list[float] | `[0.0]` | Fuel cost per node ($/MWh) |
| `fuel_cost_curve` | list[[CostCurveConfig](#costcurveconfig)] \| null | `null` | Per-node offer curve; replaces `fuel_cost` in the master problem and in the dispatch of the built capacity. See [CostCurveConfig](#costcurveconfig) |
| `fixed_cost` | list[float] | `[0.0]` | Fixed O&M per node ($ per MW of power built per year), charged every year the build is in service, at its degraded power |
| `maintenance_cost` | list[float] | `[0.0]` | Maintenance cost per node ($/MWh) |
| `inertia` | list[float] | `[0.0]` | Inertia constant H per node (s) |
| `droop` | list[float] | `[0.05]` | Governor droop characteristic per node (pu), e.g. 0.05 = 5% |
| `governor_time_const` | list[float] | `[5.0]` | Governor time constant per node (seconds) |
| `start_up_cost` | list[float] | `[0.0]` | Startup cost per node ($ per start of the capacity built at a bus; one of its units costs it divided by the number of units) |
| `unit_size` | list[float] (≥ 0) | `[0.0]` | Size of one unit per node (MW). Capacity the master builds reaches unit commitment in the dispatch as `ceil(MW at the bus / unit_size)` identical units (generator `num_units`); `0` = one unit |
| `eff_at_min` | list[float] | `[0.0]` | Efficiency at min power per node |
| `min_up` | list[int] | `[0]` | Min up time per node (hours) |
| `min_down` | list[int] | `[0]` | Min down time per node (hours) |
| `frequency_hz` | float (> 0) | `50` | Operating frequency (Hz) |
| `current_type` | `AC` \| `DC` \| `AC_DC` | `"AC"` | Current type |
| `reservable` | bool | `true` | Can provide reserves |
| `decommissioning_cost` | list[float] | `[0.0]` | Decommissioning cost per node ($/MW) |
| `risk_coefficient` | list[float] | `[1.0]` | Per-node geographic risk derating (0-1). Reduces effective capacity credit to account for hazard exposure. Computed from node hazard intensity and component fragility via the Risk Workbench. |
| `color` | str \| null | `null` | Display color hex code (e.g. '#FF0000') for results visualization |

---

## BatteryTechnologyConfig

Candidate storage technology, under `battery_technologies: {<key>: {...}}`. Every field is read.

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | str | required by schema; loader default: technology key | Display name. Defaults to the technology key |
| `invest_cost_power` | list[float] | required | Power investment cost per node ($/MW) |
| `invest_cost_energy` | list[float] | required | Energy investment cost per node ($/MWh) |
| `invest_max_power` | list[float] | required | Max power investment per node (MW) |
| `invest_max_capacity` | list[float] | required | Max capacity investment per node (MWh) |
| `min_duration_hours` | float (≥ 0) | `1` | Min energy-to-power ratio (hours) |
| `max_duration_hours` | float (≥ 0) | `24` | Max energy-to-power ratio (hours) |
| `efficiency_charge` | list[float] | required | Charge efficiency per node (0-1) |
| `efficiency_discharge` | list[float] | required | Discharge efficiency per node (0-1) |
| `degradation_rate` | list[float] | required | Degradation per node (fraction/year) |
| `lifetime` | int | required | Economic lifetime (years, scalar) |
| `soc_initial` | list[float] | `[0.5]` | Initial SOC fraction per node |
| `max_DoD` | list[float] | `[0.9]` | Max depth of discharge per node |
| `maintenance_cost` | list[float] | `[0.0]` | Maintenance cost per node ($/MWh) |
| `inertia` | list[float] | `[0.0]` | Inertia constant per node (s) |
| `throughput_degradation_cost` | list[float] | `[1.0]` | Cycling cost ($/MWh discharged). Keep it > 0: with 0 the LP can charge and discharge simultaneously |
| `spillage` | bool | `true` | Allow energy spillage |
| `current_type` | `AC` \| `DC` | `"DC"` | Current type |
| `decommissioning_cost` | list[float] | `[0.0]` | Decommissioning cost ($/MW) |
| `risk_coefficient` | list[float] | `[1.0]` | Per-node geographic risk derating (0-1). Reduces effective capacity credit to account for hazard exposure. |
| `ramp_up` | list[float] | `[0.0]` | Ramp-up limit of the batteries it builds (fraction of the power built per hour; 0 = no limit), as [BatteryConfig](#batteryconfig) |
| `ramp_down` | list[float] | `[0.0]` | Ramp-down limit, likewise |
| `fixed_cost` | list[float] | `[0.0]` | Fixed O&M ($ per MW of power built per year), charged every year the build is in service |
| `reservable` | bool | `true` | The batteries it builds hold reserve in the dispatch (the master's candidate technologies hold none) |
| `Availability` / `availability_file` | str \| null | `null` | Availability profile: factor on the charge and discharge power of what it builds, in the master and in the dispatch |
| `color` | str \| null | `null` | Display color hex code (e.g. '#FF0000') for results visualization |

---

## ElectrolyzerConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | str | required | Electrolyzer name |
| `type` | `Electrolyzer` | `"Electrolyzer"` | Always `Electrolyzer` |
| `fuel` | str | `"Hydrogen"` | Output fuel |
| `life_time` | list[int] | required | Lifetime per node (years) of an electrolyzer the master builds; existing electrolyzers do not age |
| `rated_power` | list[float] | required | Rated power per node (MW); the electrolyzer runs anywhere between 0 and this |
| `ramp_up` | list[float] | required | Ramp-up per node (fraction of rated power per hour, scaled to `resolution_hours`; `0` = no limit). Applied in the operational dispatch along with the rated-power limit; not carried across rolling-horizon windows |
| `ramp_down` | list[float] | required | Ramp-down per node (same units and scope as `ramp_up`) |
| `eff_at_rated` | list[float] | required | Efficiency at rated load per node |
| `eff_at_min` | list[float] | required | Efficiency at minimum load per node |
| `energy_per_kg_h2` | float | `50` | Specific energy (kWh/kg H₂) |
| `fixed_cost` | list[float] | required | Fixed O&M per node ($/MW of rated power per year) |
| `variable_cost` | list[float] | required | Variable cost per node |
| `water_cost` | float | `0.001` | Water cost ($/kg H₂) |
| `invest_cost` | list[float] | required | Investment cost per node ($/MW) |
| `invest_max_power` | list[float] | required | Maximum investment per node (MW) |

---

## FuelConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | str | required by schema; loader default: fuel key | Fuel name. Defaults to the key |
| `unit` | str \| null | `null` | Physical unit (e.g. `m3`, `ton`) |
| `emission_factor` | float (≥ 0) | required by schema; loader default: 0.0 | CO₂ emissions per MWh of fuel energy (t/MWh), e.g. IPCC values. Generator output is divided by the unit's `eff_at_rated`; non-electric demand uses `energy_content`. The loader defaults it to 0 when omitted |
| `energy_content` | float \| null (≥ 0) | `null` | MWh per unit; used to derive generator `fuel_cost` |
| `price_base` | float | required by schema; loader default: 0.0 | Price per unit ($/unit). May be negative (tipping fees). The loader defaults it to 0 when omitted |
| `price_growth_rate` | float | `0` | Annual price growth |

---

## PrimaryEnergySourceConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | str | required by schema; loader default: source key | Source name. Defaults to the key |
| `unit` | str | required | Physical unit |
| `max_availability` | list[float] | required | Max availability per node (unit/year) |
| `import_cost` | list[float] | required | Import cost per node ($/unit) |
| `storage_capacity` | list[float] | required | Storage capacity per node (units) |
| `initial_storage_level` | list[float] | required | Initial storage fraction per node |
| `min_storage_level` | float (0–1) | `0.1` | Minimum storage level (fraction) |
| `transport_transit_days_per_100km` | float (≥ 0) | `0` | Source->tank replenishment lead time, in days per 100 km of route distance. 0 = instantaneous transport. |
| `disruption_start_hour` | int (≥ 0) | `0` | Supply-disruption window start, absolute horizon hour (hour 0 = 1 January of `base_year`, 8760 h per year), inclusive. |
| `disruption_end_hour` | int (≥ 0) | `0` | Supply-disruption window end, absolute horizon hour (exclusive). end <= start disables the disruption. |
| `disruption_availability` | float (0–1) | `1` | Availability fraction during the disruption window (0 = full cut, 1 = none). |

---

## FuelEntryPointConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | str | required | Entry point name |
| `fuel` | str | `""` | Single fuel (legacy; copied into `fuels`) |
| `fuels` | list[str] | `[]` | Fuels handled |
| `node` | int (≥ 0) | required | Node index (0-based) |
| `coordinate` | [GeoCoordinate](#geocoordinate) | required | Location |
| `max_import_rate` | float (≥ 0) | `0` | Max import rate (units/hour) |
| `import_cost` | float (≥ 0) | `0` | Import cost ($/unit) |
| `fuel_params` | dict[str, any] | `{}` | Per-fuel parameters |

---

## FuelInfrastructureConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `transport_pipelines` | dict[str, dict[str, any]] | `{}` | Routes by id (written by the Studio) |
| `storage_facilities` | dict[str, dict[str, any]] | `{}` | Storage facilities by id (written by the Studio) |

---

## ConversionTechnologyConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `fuel` | str | required | Input fuel |
| `efficiency` | float \| null | `null` | Conversion efficiency |
| `units` | dict[str, float] | required | Unit key -> fraction mapping |

---

## PenaltiesConfig

Keys are matched case-insensitively (`LOSS_OF_LOAD`, `CO2_cost` and `loss_of_load` are the same key). Any other key is reported as an unknown configuration key; this includes `curtailment`, `curtailment_excess_penalty`, `re_excess_penalty`, `coupling_slack_penalty`, `transport_congestion` and `storage_violation`, which older files may contain: they were removed because they priced nothing.

| Field | Type | Default | Description |
|---|---|---|---|
| `loss_of_load` | float | `10000000` | Value of lost load ($/MWh unserved) |
| `loss_of_reserve_static` | float | `100` | $/MW static reserve deficit |
| `loss_of_reserve_dynamic` | float | `100` | $/MW dynamic reserve deficit |
| `loss_of_inertia` | float | `200` | $/MW-s inertia deficit |
| `transfer_margin` | float | `100` | Transfer-margin violation ($/MW). YAML also accepts `TransferMargin` |
| `max_curtailment_ratio` | float (0–1) | `0.05` | Cap on RE curtailment as a fraction of RE generation. Hard constraint per representative day in the master problem (monolithic and Benders); not enforced in operational dispatch |
| `curtailment_cost` | float (≥ 0) | `20` | Cost of curtailed RE in operational dispatch ($/MWh) |
| `rooftop_curtailment` | float | `5` | $/MWh rooftop curtailed |
| `co2_cost` | float | `10` | $/tCO2 |
| `co2_budget_violation` | float | `500` | $/tCO2 over budget |
| `fre_penetration_loss` | float | `100` | $/MWh RE shortfall |
| `ev_loss` | float | = `loss_of_load` | $/MWh EV driving energy the fleet cannot supply. Omitted, it takes the value of `loss_of_load`; a lower value lets the dispatch leave EVs uncharged whenever generating costs more (a warning is logged). |
| `loss_of_fuel_supply` | float | `100` | $/unit fuel deficit |
| `non_electric_demand_loss` | float | `100` | $/unit of non-electric (sectoral) fuel demand left unmet, per sector, node and period |
| `soc_violation` | float | `1000000` | $/MWh SOC limit violation. |

---

## CO2BudgetConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Enforce the budget |
| `annual_budget` | float (≥ 0) | `1000000` | Annual budget (t CO₂/year) |

The budget covers the system's own power plants. The dispatch and the master problem both hold it (the master over each planning year's representative days scaled to the year). In a multi-system run each system keeps its own budget, enabled flag and `penalties.co2_budget_violation`: both add one budget constraint per system over that system's nodes, including the nodes of development zones connected to its buses.

---

## CriticalityPenalties

| Field | Type | Default | Description |
|---|---|---|---|
| `critical` | float | `1000` | Weight for `critical` sectors |
| `high` | float | `100` | Weight for `high` sectors |
| `medium` | float | `10` | Weight for `medium` sectors |
| `low` | float | `1` | Weight for `low` sectors |

---

## DemandSectorConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `is_flexible` | bool | `false` | Sector can shift demand |
| `flexibility_ratio` | float (0–1) | `0` | Share of the sector's demand per step that can be shifted |
| `criticality` | `critical` \| `high` \| `medium` \| `low` | `"medium"` | Shedding priority; weighted by `criticality_penalties` |
| `delay_tolerance` | int (≥ 0) | `0` | Maximum shift, earlier or later (hours); 0 = no shifting |

---

## NonElectricDemandConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `fuel` | str | required | Fuel consumed |
| `unit` | str | required | Physical unit |
| `demand` | list[int] | required | Annual demand per node (units of `unit`) |

The demand is fixed: unmet demand costs `penalties.non_electric_demand_loss` per unit. Route transport costs and losses are set per route (`fuel_transport_routes`), not on the source.

---

## EVCategoryConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `battery_capacity` | float | required | Battery capacity per vehicle (kWh) |
| `charging_power` | float | required | Charging power per vehicle (kW) |
| `v2g_power` | float | required | V2G power per vehicle (kW) |
| `v2g_participation` | float (0–1) | required | Fraction of vehicles offering V2G |
| `efficiency_charge` | float (0–1) | required | Charging efficiency |
| `efficiency_discharge` | float (0–1) | required | V2G efficiency |
| `min_soc` | float (0–1) | required | Minimum SOC |
| `max_adoption` | float | `35` | Fleet-growth multiplier (S-curve ceiling) |
| `growth_rate` | float | `0.14` | S-curve growth rate |
| `mid_point_fraction` | float (0–1) | `0.5` | S-curve midpoint as a fraction of the horizon |
| `daily_energy_kwh` | float \| null | `null` | Real daily charging energy per vehicle (kWh/day). The availability pattern is normalized so its daily integral equals this value, preventing the pattern from being read as continuous power draw. If None, defaults to battery_capacity × 0.12 (~12% daily depth-of-discharge). |

---

## RooftopSolarConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `adoption_scenario` | `low` \| `medium` \| `high` | `"medium"` | Adoption scenario |
| `weather_variability` | `low` \| `normal` \| `high` | `"normal"` | Weather variability |
| `simulation_seed` | int | `42` | Random seed |
| `systems_per_node` | list[int \| null] \| null | `null` | Rooftop systems per node; with `avg_system_size`, sets rooftex's potential (a node with a `null` in either list keeps the potential rooftex draws) |
| `avg_system_size` | list[float \| null] \| null | `null` | Average system size per node, kW |
| `performance_ratio` | float (0–1) | `0.75` | Performance ratio of the rooftex profile |
| `degradation_rate` | float (≥ 0) | `0.005` | Degradation (fraction/year) |
| `base_year` | int | `2025` | Reference year |
| `target_year` | int | `2050` | Target year |
| `initial_adoption` | list[float \| null] \| null | `null` | Current adoption per node (passed to rooftex, which 0.1.1 does not use; `null` = rooftex's 0.05) |
| `max_adoption` | dict[str, float] \| null | `null` | Maximum adoption per scenario (`low`, `medium`, `high`) |
| `adoption_rates` | dict[str, float] \| null | `null` | Adoption growth rate per scenario |

The runner passes every field except the cost fields, `base_year`, `target_year` and `degradation_rate` to rooftex (unset or empty: rooftex's defaults) and uses `base_year`, `target_year` and `degradation_rate` for the yearly capacity; the per-node capacity comes from rooftex or from the system's `rooftop_max_potential`. See [Rooftop Solar](../formulation/rooftop-solar.md).

---

## StochasticScenarioConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | str | required | Scenario name |
| `probability` | float (0–1) | required | Scenario probability; all probabilities must sum to 1 (±0.01) |
| `description` | str | `""` | Free text |
| `multipliers` | [ScenarioMultipliers](#scenariomultipliers) | see [ScenarioMultipliers](#scenariomultipliers) | Parameter multipliers |

---

## ScenarioMultipliers

Each scenario of the stochastic master has its own operation (demand, operating costs and penalties) and values the common investment plan with its own investment-cost multipliers. Every multiplier defaults to 1 (no change). See [Stochastic Programming](../formulation/stochastic-programming.md).

| Field | Type | Default | Description |
|---|---|---|---|
| `invest_cost_renewables` | float | `1` | Multiplier on the investment cost of renewable technologies |
| `invest_cost_storage` | float | `1` | Multiplier on battery technology investment cost (power and energy) |
| `invest_cost_conventional` | float | `1` | Multiplier on the investment cost of non-renewable technologies |
| `invest_cost_transmission` | float | `1` | Multiplier on transmission investment cost |
| `fuel_cost` | float | `1` | Multiplier on the fuel cost of generators (fuel-cost curves included) and technologies, and on the fuel prices of the master's fuel balance, in the scenario's operation |
| `maintenance_cost` | float | `1` | Multiplier on the maintenance cost of generators, batteries, technologies and battery technologies in the scenario's operation |
| `discount_rate` | float | `1` | Multiplier on the discount rate used for the scenario's costs |
| `demand_growth` | float | `1` | Annual demand factor: the scenario's demand in year *y* is the base demand × `demand_growth`^(y−1). 1.02 = +2 %/year relative to the base case |
| `fuel_price_growth` | float | `1` | Annual fuel price factor: fuel cost and fuel prices × `fuel_price_growth`^(y−1) |
| `carbon_price` | float | `1` | Multiplier on `co2_cost` in the scenario's operation |
| `gen_fixed_cost` | float | `1` | Multiplier on the fixed O&M ($/MW/year) of generators, technologies, batteries and battery technologies |
| `gen_decommissioning_cost` | float | `1` | Multiplier on the decommissioning cost of existing generators |
| `bat_decommissioning_cost` | float | `1` | Multiplier on the decommissioning cost of existing batteries |
| `bat_invest_cost_power` | float | `1` | Multiplier on battery technology power cost ($/MW), applied on top of `invest_cost_storage` |
| `bat_invest_cost_capacity` | float | `1` | Multiplier on battery technology energy cost ($/MWh), applied on top of `invest_cost_storage` |
| `penalty_loss_of_load` | float | `1` | Multiplier on `penalties.loss_of_load` (and the sectoral penalties derived from it) in the scenario's operation |
| `penalty_loss_of_reserve` | float | `1` | Multiplier on `penalties.loss_of_reserve_static` and `loss_of_reserve_dynamic` |
| `penalty_curtailment` | float | `1` | Multiplier on `penalties.curtailment_cost` |
| `penalty_fre_penetration` | float | `1` | Multiplier on `penalties.fre_penetration_loss` |
| `penalty_co2_budget_violation` | float | `1` | Multiplier on `penalties.co2_budget_violation` (each system's) |
| `bat_maintenance_cost` | float | `1` | Multiplier on the maintenance cost of batteries and battery technologies, applied on top of `maintenance_cost` |
| `gen_start_up_cost` | float | `1` | Multiplier on generator `start_up_cost`. Only acts when the dispatch commits units (`master_problem.use_uc_in_dispatch`), the case in which the master models start-ups |
| `converter_invest_cost` | float | `1` | Multiplier on the `invest_cost` of AC/DC and frequency converters |
| `converter_variable_cost` | float | `1` | Multiplier on the `variable_cost` of AC/DC and frequency converters |
| `transmission_cost_per_mw_km` | float | `1` | Multiplier on the flow cost of inter-system links (`cost_per_mw_km`) |
| `fuel_import_cost` | float | `1` | Multiplier on the per-node `import_cost` of the master's fuel balance |
| `penalty_loss_of_fuel_supply` | float | `1` | Multiplier on `penalties.loss_of_fuel_supply` (fuel shortfall and excess supply) in the master |
| `fuel_storage_invest_cost` | float | `1` | Multiplier on the fuel storage investment cost (`fuel_infrastructure`) |
| `fuel_transport_cost` | float | `1` | Multiplier on the route transport cost and on the route investment cost |
| `electrolyzer_invest_cost` | float | `1` | Multiplier on electrolyzer `invest_cost` |
| `electrolyzer_fixed_cost` | float | `1` | Multiplier on electrolyzer `fixed_cost` |
| `electrolyzer_variable_cost` | float | `1` | Multiplier on electrolyzer `variable_cost` |
| `electrolyzer_water_cost` | float | `1` | Multiplier on electrolyzer `water_cost` |

Keys written by earlier Studio versions are translated on load: `gen_fuel_cost` and `fuel_price` → `fuel_cost`, `gen_maintenance_cost` → `maintenance_cost`, `transmission_invest_cost` → `invest_cost_transmission`, `fuel_price_growth_rate` → `fuel_price_growth`, `penalty_co2_cost` → `carbon_price`, and `gen_invest_cost` → both `invest_cost_renewables` and `invest_cost_conventional`. Keys that end up on the same field multiply, with a warning. Any other key is ignored with a warning, among them the electrolyzer, fuel import, fuel storage and transport investment and loss-of-fuel-supply keys of earlier Studio versions.

---

## DevelopmentZoneConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | str | required | Zone name |
| `technology` | str | required | Technology or fuel name the zone matches (e.g. `Solar`, `Wind`) |
| `layer` | `electrical` \| `primary_energy` | `"electrical"` | `electrical` or `primary_energy` |
| `polygon` | list[[GeoCoordinate](#geocoordinate)] | required | Boundary vertices (closed ring) |
| `max_capacity_mw` | float \| null (≥ 0) | `null` | Zone capacity cap (MW) |
| `notes` | str \| null | `null` | Free text |
| `line_cost_per_mw_km` | float (≥ 0) | `1500` | Transmission line cost ($/MW/km) |
| `transformer_cost_per_mw` | float (≥ 0) | `50000` | Step-up transformer cost ($/MW) |
| `target_bus` | int \| null | `null` | Bus index to connect to (0-based); overrides nearest-bus detection |
| `allowed_generators` | list[str] \| null | `null` | Generator keys allowed in zone (None = match by technology name) |
| `allowed_technologies` | dict[str, float] \| null | `null` | `{technology_key: max_MW}` (0 = unlimited). `null` → match by `technology` |
| `exclusive` | bool | `false` | If True, matched technologies can ONLY invest at this zone. invest_max_power at all original nodes is set to 0 after expansion. |

---

## CustomConstraintConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | str | required | Constraint name |
| `type` | str | `"linear"` | `linear` (needs `terms`) or a template type |
| `terms` | list[[ConstraintTerm](#constraintterm)] | `[]` | Linear terms |
| `sense` | `<=` \| `>=` \| `==` | `"<="` | Constraint sense |
| `rhs` | float | `0` | Right-hand side |
| `target` | `operational` \| `investment` | `"operational"` | `operational` (each dispatch model) or `investment` (master problem) |
| `params` | dict[str, any] | `{}` | Template parameters |

### ConstraintTerm

| Field | Type | Default | Description |
|---|---|---|---|
| `variable` | str | required | Model variable name |
| `index` | list[any] | `[]` | Index tuple (names or integers) |
| `coefficient` | float | `1` | Coefficient |

---

## OutageWindow

| Field | Type | Default | Description |
|---|---|---|---|
| `element_type` | `generator` \| `line` \| `battery` \| `transformer` \| `acdc_converter` \| `freq_converter` \| `electrolyzer` \| `fuel_source` \| `fuel_storage` \| `fuel_route` | required | Kind of element taken out |
| `element_id` | str | required | Generator/battery/electrolyzer key, fuel name (`fuel_source`, `fuel_storage`), `line_id` or name (lines, transformers, converters), or `route_id` of a fuel route (`route_<i>` for a route without one). A generator/battery id of the form `<unit key>_n<node>` (saved by earlier Studio versions) is read as that unit and node |
| `node` | int (≥ 0) or null | `null` | Generators: cut only the unit's capacity at this node; `fuel_storage`: only the fuel's tank at this node (`null` = every node). Battery and electrolyzer windows apply to the whole unit |
| `start_hour` | int (≥ 0) | required | First hour (inclusive) of the window, in horizon hours: hour 0 = 1 January of `base_year`, 8760 h per simulated year |
| `end_hour` | int (> 0) | required | End hour (exclusive); must be > `start_hour` |
| `availability` | float (0.0–1.0) | `0` | Capacity fraction available during the window (0 = full outage) |
| `label` | str | `""` | Note shown on the calendar |
| `recurring` | bool | `false` | Repeat the window every year at the same hours of the year (`start_hour` taken modulo 8760). `false`: it happens once |

---

## GeoCoordinate

| Field | Type | Default | Description |
|---|---|---|---|
| `latitude` | float (-90–90) | required | Degrees |
| `longitude` | float (-180–180) | required | Degrees |
| `label` | str \| null | `null` | Optional label |
| `radius_km` | float (0.1–500) | `20` | Influence radius (km) |

---

## RiskConfig

Nested under the top-level `risk` key. See [Risk and Resilience](../formulation/risk-resilience.md).

The block holds the settings of the Studio's Risk & Resilience Workbench (analysis and post-processing). Neither the master problem nor the dispatch reads it, `enabled` included; the only risk input to the optimization is the per-element `risk_coefficient` of generators, batteries and technologies.

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Set by the Workbench's Apply; the optimization does not read it |
| `risk_measure` | `expected` \| `cvar` \| `minimax_regret` | `"expected"` | `expected`, `cvar` or `minimax_regret` |
| `cvar_alpha` | float (> 0, < 1) | `0.95` | CVaR confidence level (0.95 → worst 5 % of scenarios) |
| `cvar_lambda` | float (0–1) | `0.5` | Risk-aversion weight (0 = risk-neutral, 1 = pure CVaR) |
| `climate_scenarios` | list[[ClimateScenarioConfig](#climatescenarioconfig)] | `[]` | Climate scenarios; probabilities must sum to 1 (±0.01) |
| `hazard_scenarios` | list[[HazardScenarioConfig](#hazardscenarioconfig)] | `[]` | Hazard events |
| `combination_method` | `independent` \| `copula` \| `mcda` | `"independent"` | Method for combining multi-hazard failure probabilities |
| `voll` | [VOLLConfig](#vollconfig) | see [VOLLConfig](#vollconfig) | Value of lost load by sector |
| `demand_base_temperature` | float | `24` | Base temperature for HDD/CDD calculation (°C). Default 24°C is appropriate for tropical SIDS; use 18°C for temperate regions. |
| `demand_heating_coefficient` | float (≥ 0) | `0.5` | Heating demand sensitivity (%/°C below base temperature). Refs: Sailor & Munoz (1997). Typical 0.5 for tropical SIDS. |
| `demand_cooling_coefficient` | float (≥ 0) | `2.5` | Cooling demand sensitivity (%/°C above base temperature). Refs: Lam et al. (2018), IRENA (2019). Typical 2-4 for Caribbean. |
| `insurance_premium_rate` | float (≥ 0) | `0` | Annual insurance premium as fraction of asset replacement value |
| `monte_carlo_samples` | int (100–100000) | `1000` | Monte Carlo samples |
| `monte_carlo_seed` | int (≥ 0) | `42` | Random seed |

### VOLLConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `residential` | float (≥ 0) | `5000` | $/MWh |
| `commercial` | float (≥ 0) | `25000` | $/MWh |
| `industrial` | float (≥ 0) | `15000` | $/MWh |
| `critical` | float (≥ 0) | `100000` | $/MWh |

### ClimateScenarioConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | str | required | Scenario name |
| `probability` | float (0–1) | required | Scenario probability |
| `ssp_pathway` | `SSP1-2.6` \| `SSP2-4.5` \| `SSP3-7.0` \| `SSP5-8.5` | `"SSP2-4.5"` | SSP pathway |
| `gcm_model` | str | `""` | GCM model name, e.g. 'ACCESS-CM2' |
| `availability_suffix` | str | `""` | Suffix appended to availability profile filenames (e.g. '_ssp245' → profile_sun_1_ssp245.csv) |
| `demand_scale` | dict[int, float] | `{}` | Year → demand multiplier (e.g. {2040: 1.05, 2050: 1.12}) |
| `temperature_delta` | dict[int, float] | `{}` | Year → ΔT from baseline in °C (e.g. {2040: 0.8, 2050: 1.5}) |
| `ghi_delta_fraction` | dict[int, float] | `{}` | Year → fractional change in GHI (e.g. {2050: -0.02} = −2%) |
| `wind_speed_delta_fraction` | dict[int, float] | `{}` | Year → fractional change in wind speed (e.g. {2050: -0.05} = −5%) |

### HazardScenarioConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | str | required | Event name |
| `probability` | float (0–1) | required | Annual occurrence probability |
| `hazard_type` | str | `""` | earthquake, cyclone, flood, … |
| `year_of_occurrence` | int | `0` | Planning year when event strikes (0 = any year / steady-state) |
| `affected_nodes` | list[int] | `[]` | Affected node indices |
| `damage_fraction` | dict[str, float] | `{}` | gen_key or bat_key → fraction of capacity lost (0-1) |
| `recovery_hours` | int (≥ 0) | `8760` | Hours to fully restore damaged capacity |
| `intensity_measure` | float | `0` | Event intensity (PGA in g, wind in m/s, flood depth in m, …) |
| `description` | str | `""` | Free text |

### Risk classification

The Workbench classifies each node with `evaluate_risk_criteria()` and fixed thresholds; they are not part of the configuration. The expected annual loss (EAL) and the composite failure probability are classified separately:

| EAL ($/yr) | Class |
|---|---|
| EAL < 1000 | `negligible` |
| EAL < 50000 | `tolerable_low` |
| EAL < 500000 | `tolerable_high` |
| otherwise | `intolerable` |

| Composite risk | Band |
|---|---|
| risk < 0.01 | `low` |
| risk < 0.05 | `medium` |
| risk < 0.15 | `high` |
| otherwise | `very_high` |

Action is required when the class is `intolerable` or the band is `very_high`.
