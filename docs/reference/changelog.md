# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/). Versioning: [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Electrolyzer hydrogen fills the hydrogen tank.** With the primary-energy
  model on and a fuel source named `Hydrogen`, the hydrogen of every
  dispatch electrolyzer now flows into that fuel's tank at its node (in
  tonnes, scaled by the tank's storage efficiency like any inflow), so
  hydrogen-fuelled units and non-electric hydrogen demand run on it. The
  electrolyzers used to be a plain load whose hydrogen was only reported,
  and hydrogen units drew on fuel supply or the shortfall penalty. The unused
  `PrimaryEnergyInput` fields `h2_production_hourly` and `electrolyzer_config`
  must now be `nothing` (an error says so), and the primary-energy block no
  longer carries its own unused electrolyzer variables.

- **Every electrolyzer is dispatched, and its results are exported.** Only
  the first entry under `electrolyzers` reached the dispatch; the others
  were ignored, and no electrolyzer result was kept. All entries are now
  modelled (`PowerSystemInput` takes a vector, `electrolyzers`; the single
  `electrolyzer_config` keyword is still accepted), and each one's power
  (MW) and hydrogen output (kg/h, the conversion of the primary-energy
  hydrogen balance) are kept in `YearResults.electrolyzer_power` /
  `electrolyzer_h2` and written to the HDF5 file as
  `electrolyzer_power/{name}` and `electrolyzer_h2/{name}`.

- **Unknown configuration keys are reported.** A misspelt or obsolete key
  (`min_up_time` for `min_up`) used to be dropped without a trace. It is
  still ignored, but `load_config` now logs a warning for each one with its
  full path (`systems.Cuba.generators.gas_1.dorop`) and keeps the list in
  `ESFEXConfig.unknown_keys`; `esfex validate` prints it, and the Studio's
  validation lists the keys under a new *Configuration* category. Keys the
  loader translates are not reported: upper-case aliases, flat `dc_*` /
  `DC_*` keys, penalty keys in any case, legacy `unit_*` / `bat_*` units,
  the Studio's `_gui_*` / `_technologies` / `_equipment_buses` metadata,
  `visual_scaling`, `_geo_assets`, `plugins` and `solver.options`.

- **`esfex plugin install --git URL --trust-host`.** Git hosts outside
  github.com, gitlab.com, bitbucket.org, codeberg.org and git.sr.ht were
  always refused on the command line, because the CLI had no way to confirm
  them as the Studio's dialog does. `--trust-host` is that confirmation.
- **`Orchestrator.run(master_only=True)`** stops after the master problem and
  leaves its decisions in `Orchestrator.master_solution`.

- **Realism check in Validate.** The Validate dialog has a new **Realism**
  category that runs the plausibility and operability checks that until now
  were only reachable from Python: probable duplicate plants (same name, or
  same fuel within 15 km; info), installed capacity more than twice the peak
  demand (info), and a max-flow test of whether the line, transformer and
  converter ratings can deliver the peak demand from the generator buses
  (warning).
- **N-1 "Apply in" setting.** Global Settings > N-1 Security has checkboxes
  for the simulation modes that get the N-1 constraints (`apply_to_modes`).
  Studio projects used to fix this to unit commitment with no way to change
  it; new projects now default to both modes, like a YAML file.
- **Per-line reactance per km.** `transmission_lines_geo` entries accept
  `reactance_per_km` (Ω/km). When a line has no `reactance_pu`, its reactance
  is derived from its length with this value instead of
  `dc_power_flow.reactance_per_km`. The Studio line form's Reactance per km
  field, which was saved but dropped on load, now has this effect.

- **The master problem holds the CO2 budget, models converters and link
  flow costs, and starts units when the dispatch commits them.** The master
  had none of these, so it planned for a cheaper system than the dispatch
  then ran:
  - **CO2 budget.** Each system's annual `co2_budget` now holds in every
    planning year of the master, as in the dispatch: the emissions of the
    representative days (or TSAM periods), scaled to the year like their
    cost, may exceed the budget only at that system's
    `penalties.co2_budget_violation` per tonne. A multi-system run keeps one
    budget per system over its own nodes.
  - **Converters.** AC/DC and frequency converters carry power in the
    master's representative days as in the dispatch, at their
    `variable_cost`. A converter with `invest_max_power` > 0 can be expanded
    at its `invest_cost` ($/MW, plus `fixed_cost` $/MW/year); the MW built
    are added to its `rated_power_mva` in the dispatch from the year they
    are built (myopic years see earlier builds). In a merged run the
    master's converters join nodes, not dispatch buses.
  - **Inter-system link flow cost.** An electrical inter-system link with
    `cost_per_mw_km` costs `distance_km` × `cost_per_mw_km` per MWh it
    carries in the master (new `TransmissionLineGeo.cost_per_mw_km`, set on
    the merged master's link). The field used to be ignored by the merged
    run, so a link with a large placeholder value now carries that cost.
  - **Start-ups.** With `master_problem.use_uc_in_dispatch: true` the master
    gives each thermal or hydro unit with a start-up cost or minimum power a
    continuous online capacity: output between `min_power` × online
    capacity and online capacity, and one `start_up_cost` for bringing the
    whole unit online. The master stays an LP; minimum up and down times
    are left to the dispatch.

- **Six more stochastic multipliers change the plan.** The Studio offers
  again, and the model now applies, `penalty_co2_budget_violation`,
  `bat_maintenance_cost` (batteries and battery technologies only, on top
  of `maintenance_cost`), `gen_start_up_cost` (only acts with
  `use_uc_in_dispatch`), `converter_invest_cost`, `converter_variable_cost`
  and `transmission_cost_per_mw_km` (the flow cost of inter-system links;
  shown as "Inter-system link flow cost (per MW·km)"). They were dropped
  with a warning because the master had no term they could scale.

- **The master plans the fuel chain and electrolyzers.** With primary energy
  on, the master now runs the dispatch's fuel model on each representative
  day (or TSAM period): the same fuel prices, import costs, supply limits,
  tanks, routes, non-electric demand and penalties. On these days tanks are
  cyclic and shipments arrive the same day. It builds fuel storage per node,
  route capacity and electrolyzer power within the `fuel_infrastructure`
  expansion limits and `invest_max_power`, charging overnight costs at the
  build year. Electrolyzers draw power at their bus, with their fixed,
  variable and water cost, and fill the node's `Hydrogen` tank. Benders cuts
  carry the new capacities. The master had no fuel model, and electrolyzers
  could not be built at all.

- **Stochastic fuel and electrolyzer multipliers.** `fuel_import_cost`,
  `penalty_loss_of_fuel_supply`, `fuel_storage_invest_cost`,
  `fuel_transport_cost` (route transport cost and route investment), and
  `electrolyzer_invest_cost`, `electrolyzer_fixed_cost`,
  `electrolyzer_variable_cost` and `electrolyzer_water_cost` are back in the
  Studio's scenario multiplier catalog. They now scale the master's fuel
  block. Before, they were dropped with a warning. `fuel_cost` and
  `fuel_price_growth` now also scale the fuel prices of the fuel balance.

- **Cost of N-1 security.** `n1_security_cost` was always 0: the dispatch
  never computed it. With the new option `n1_security.compute_security_cost`
  (Studio: Global Settings → N-1 Security → *Compute N-1 Security Cost*),
  each operational window that has N-1 constraints is solved a second time
  without them, and the objective difference is reported as
  `n1_security_cost` (YearResults, the HDF5 `n1_security_cost` dataset and
  the `n1_security/security_cost` attribute). The re-solve copies the solved
  model, primary-energy block and user constraints included, with the N-1
  slacks free of charge. It roughly doubles the solve time, so it is off by
  default, and the field stays 0 when it is off.

- **Scheduled outages of electrolyzers, fuel tanks and fuel routes.**
  Outage windows of type `electrolyzer`, `fuel_storage` and `fuel_route`
  were accepted but had no effect. Now, in absolute horizon hours like the
  other types: an `electrolyzer` window scales the unit's rated power; a
  `fuel_storage` window (`element_id` = fuel, optional `node`) takes the
  fuel's tank out of service, locking its contents in proportion to the
  outage (frozen at availability 0, so the node's fuel only passes through)
  with its minimum levels suspended; a `fuel_route` window (`element_id` =
  the route's `route_id`) scales the route's capacity in each
  primary-energy period by the period's mean availability. In a merged
  multi-system run, electrolyzer ids follow the `{system}__{key}` renaming
  and a tank window without a node covers only its own system's nodes.
  Unknown electrolyzer or fuel ids are reported at load time, unknown route
  ids when the run reaches the window.

- **Fuel burnt per unit in the results (`fuel_for_power`).** With the
  primary-energy model on, `YearResults.fuel_for_power` holds the fuel each
  fuel-coupled unit burns per step and node (`{unit: [node x step]}`, fuel
  units per step; committed steps of each rolling window), and the results
  file stores it as `primary_energy/fuel_for_power/{unit}`. The field was
  declared but never filled. `PrimaryEnergyAdapter.get_results()` returns it
  as `fuel_for_power` (keyed by generator index).

### Changed

- **The old master fuel-infrastructure variables are replaced.** The
  master's fuel storage and transport investment variables had a cost but no
  effect (no constraint used them and the adapter passed no routes). They are
  removed with `PrimaryEnergyInvestmentConfig`, the `pe_configs` /
  `transport_routes` fields of `MasterProblemInput` and the
  `add_primary_energy_investment_*` functions. Fuel storage, routes and
  electrolyzers are now built by the master's fuel block (see "The master
  plans the fuel chain and electrolyzers" under Added), which reads
  `storage_investment_cost`, `max_storage_investment_per_node` and
  `max_transport_investment_per_arc`.

- **Each system keeps its own CO2 budget.** When several systems were
  simulated together, the merged system took the first system's
  `co2_budget` for all of them and the largest `co2_budget_violation`
  penalty: in a two-island case every tonne the main island emitted over
  its budget was priced at the small island's 500 $/t, and the small
  island's emissions counted against the main island's budget. The
  dispatch now adds one CO2 budget constraint per system, over that
  system's nodes, with that system's `annual_budget`, `enabled` flag and
  `penalties.co2_budget_violation`. Single-system runs are unchanged. The
  master problem applies the same per-system budgets (see "The master
  problem holds the CO2 budget…" under Added).

- **Primary energy: one fuel tank per node, balanced every hour.** The fuel
  model had two storage layers, a periodic balance and an hourly tank, tied
  together by slack variables. Fuel shipped along a route entered only the
  periodic balance, so generators and non-electric users at a node fed by
  transport ran on the penalised shortfall; non-electric use was counted in
  both layers (50 units in a tank served 25 of non-electric demand); and the
  periodic shortfall sat on the consumption side, where it acted as a sink.
  Each fuel now has a single tank per node:
  level = previous level + (local supply + arriving shipments) × storage
  efficiency − fuel drawn − departing shipments, and the fuel drawn plus the
  shortfall must cover the fuel for power and the non-electric use of each
  hour. Periods keep the supply limits, the non-electric demand per sector
  and the route shipments; a period's shipment leaves evenly over its hours
  and arrives after the route's transit time (now counted in time steps,
  no longer rounded to whole periods), less transport losses. Fuel arriving
  in an hour can be used in that hour, so a shortfall now means fuel is
  really missing. The periodic storage levels, their linking slacks and the
  periodic shortfall are gone: `penalties.coupling_slack_penalty` has no
  effect, the `pe_coupling_slack_cost` result is always 0, and
  `total_loss_of_supply` is the hourly shortfall. Route capacity per period
  now uses the period's real length when `resolution_hours` > 1.
- **Scheduled interruptions happen once, in absolute horizon hours.**
  `start_hour`/`end_hour` of an outage window were taken as hours within
  the year, so a window repeated in every simulated year and a
  `start_hour` of 8760 or more never applied. They are now horizon hours
  (hour 0 = 1 January of the base year, 8760 h per year), as documented.
  The new `recurring: true` repeats a window every year at the same hours
  of the year (also in the Studio calendar, "Repeat every year"). This
  applies to generators, batteries, lines, transformers and converters.
- **Fuel-source disruptions use horizon hours.** `disruption_start_hour` /
  `disruption_end_hour` (and `fuel_source` interruptions) were compared with
  the step numbers of each dispatch window, so the cut hit the start of
  every window and never the hours entered. They are now absolute horizon
  hours, located with the window's year and start hour; a `fuel_source`
  interruption is applied in the dispatch windows it overlaps.

- **Life extension of units with `invest_cost` 0 is no longer free.** The
  master prices a life-extended MW at `invest_cost × life_extension_cost_factor`
  per year, and existing units default to `invest_cost` 0 in the Studio, so
  they could be kept forever at no cost. Where the cost is 0 at a node, it
  is now taken from the candidate technology with the same fuel and type
  (for batteries, the battery technology named by `technology`, else the
  first one). A unit with no candidate stays free and is named in a warning.
- **`life_extension_cost_factor` is applied.** The master always used a
  hard-coded 0.3; it now uses the system's value (default 0.20).
- **Myopic planning follows the RE target path.** Each one-year master
  targeted the RE share reached the year before, so `TARGET_RE_PENETRATION`
  never entered a myopic run. Each year now targets its point on the
  perfect-foresight path (initial share → target over the horizon, steps
  clamped to `MIN_ANNUAL_INCREMENT` / `MAX_ANNUAL_INCREMENT`); the increment
  bounds still start from last year's share.
- **Sector priorities in load shedding.** In the dispatch, a sector's shed
  load was not capped by its demand, so all unserved energy was booked to
  one sector and priced at the cheapest (or, where sector data was missing,
  the dearest) multiplier. In the master's representative days, the
  sectoral shed was an extra injection next to the bus's load shedding, so
  a day could shed up to about twice its demand and the plain-VOLL load
  shedding absorbed every MWh, ignoring criticality. Both models now split a
  bus's shed into per-sector amounts capped by each sector's demand plus an
  unclassified remainder (demand that no sector covers) priced at VOLL;
  sectors cost VOLL × criticality, cheapest first.
- **Missing availability profiles.** A `Renewable` generator or technology
  without `availability_file` (reservoir hydro excepted) runs at capacity
  factor 1.0 every hour; this is now logged once per unit at startup. A
  configured profile that cannot be found now stops the run with the unit
  and path named: a generator used to be dropped from the dispatch, and a
  technology crashed with a `KeyError`.
- **`LOSS_DEMAND_TRHESHOLD` is deprecated.** It was passed to the model but
  no constraint used it. It is no longer passed, the Studio no longer shows
  or writes it, and old case files that contain it still load.

- **Every stochastic scenario multiplier in the Studio changes the plan.**
  The Studio's scenario form offered 32 multiplier keys, but the model knew
  only 10 names, so the loader dropped the rest: only the demand growth and
  discount rate multipliers reached the model. `ScenarioMultipliers` now has
  a multiplier for each cost the stochastic master charges: besides the
  existing ten, `gen_fixed_cost`, `gen_decommissioning_cost`,
  `bat_decommissioning_cost`, `bat_invest_cost_power` and
  `bat_invest_cost_capacity` (both on top of `invest_cost_storage`),
  `penalty_loss_of_load`, `penalty_loss_of_reserve`, `penalty_curtailment`
  and `penalty_fre_penetration`. The Studio offers exactly these. Keys
  written by earlier Studio versions are translated on load and save
  (`gen_fuel_cost` and `fuel_price` → `fuel_cost`, `gen_maintenance_cost` →
  `maintenance_cost`, `transmission_invest_cost` →
  `invest_cost_transmission`, `fuel_price_growth_rate` → `fuel_price_growth`,
  `penalty_co2_cost` → `carbon_price`, `gen_invest_cost` → both technology
  investment costs). Keys that act on the same multiplier multiply, with a
  warning. Keys for costs the master problem does not have are dropped with
  a warning, and the Studio no longer offers them: start-up, electrolyzer,
  converter, fuel import, fuel storage and transport investment, battery
  maintenance, cost per MW·km, and the loss-of-fuel-supply and CO2-budget
  penalties. A loaded project shows only the multipliers its file sets.

- **The detailed `demand` dataset is always `[node, step]`.** The scenario
  `demand` in `detailed_results/year_YYYY_threshold_0/` was transposed to
  `(nodes, hours)` only when the run had more hours than nodes; a
  unit-commitment run on a large network (for example 24 h on 72 nodes)
  kept `(hours, nodes)`. It is now always `(nodes, hours)`, like every
  other per-node dataset, and carries the attribute
  `dimensions = "nodes x hours"`. The results viewer, the SLD,
  `esfex.load_results()` and the financial analysis read both new and old
  files (`esfex.io.exporter.demand_node_hours()`); in old files the number
  of time steps decides the axis. `/demand/year_YYYY_base_demand` keeps its
  `(hours, nodes)` layout.
- **`rooftop_solar_config`: unused fields are optional.**
  `systems_per_node`, `avg_system_size`, `initial_adoption`,
  `max_adoption` and `adoption_rates` were required by the schema although
  the runner never passes them to rooftex. They now default to `null`; the
  documentation marks them, and `performance_ratio` and the cost fields,
  as not used by the runner.

- **Every modelled year has 8760 hours, leap years included.** The master
  problem counted 8784 h in leap years (`hours_per_year`), while demand
  and profiles are read in 8760-row years. For a leap year the master
  picked its representative days from rows starting at (y−1)·8784
  instead of (y−1)·8760, one day late per earlier year and running into
  the next year, and the annual scaling of operating costs, CO2 budgets
  and fuel supply limits used 8784 h in leap years. Now `hours_per_year` is
  always 8760, in Python (`hours_for_year`) and Julia (`hours_in_year`).
  Feb 29 rows of leap-year input data are dropped when demand,
  availability and inflow files are read (found from the calendar that
  starts at `date_start`; a file of exactly 8784 rows is always taken as
  one leap year), and date labels skip Feb 29, so every year runs from
  1 January to 31 December in the results viewer.
- **Year labels follow `date_start`.** A run always labelled its years
  from 2025, whatever `date_start` said, because the CLI did not pass a
  start year. Years are now labelled from the year of `date_start`, and
  the new `esfex run --start-year YYYY` option (and `start_year=` in
  `Orchestrator.run()` and `esfex.run()`) relabels them; the date labels
  then start on `date_start`'s day and time in that year. `date_start`
  still does not select rows: demand is read from the first row.

- **Bus roles you set are kept.** Every save re-derived each bus's role from
  the topology, so a role picked in the bus form was overwritten. A role you
  pick is now marked as yours (saved in `_gui_layout.user_set_bus_roles`) and
  kept on save and reload; the demand-fraction repair never gives demand to a
  `connection` bus you set and counts a `mixed` bus you set as a demand bus.
  Buses whose role you have not touched are still repaired from the topology.
- **Generation N-1 "Percentage" reserve works.** With Reserve Type =
  Percentage, the generation N-1 constraint requires headroom of
  `generation_reserve_percentage` × the system demand of each step (no unit
  excluded). Before, the setting was ignored and the largest-unit rule was
  always used. The "Fixed MW" choice, which the schema rejects, is removed.
- **Grid Builder simplification levels 0–2.** Levels 2, 3 and 4 of the build
  step ran the same equivalent-network reduction under labels that described
  an older pruning scheme. The build step now offers 0 (cleanup), 1
  (aggregate equipment and parallel lines) and 2 (equivalent network, one
  bus per node); scripts passing 3 or 4 get level 2 as before.
- **Min. System Inertia field.** The system field labelled "Inertia Limit
  Threshold (frac)" is an absolute MW·s requirement; it is now labelled in
  MW·s and accepts values above 1.
- The per-line Base Impedance field is removed from the line and
  inter-system link forms (unused since Z_base is derived from the line
  voltage and `ac_power_flow.base_mva`).

- **CO2 of generation is per MWh of fuel.** `fuels.*.emission_factor`
  (IPCC-style values such as 0.202 t/MWh for gas, as the Grid Builder
  writes them and the primary-energy block applies them) was applied per
  MWh of electricity by the dispatch, the master and Benders: emissions,
  the CO2 cost and the CO2 budget were short by the unit's efficiency
  (about 2.5× at 40 %). Output is now divided by the unit's `eff_at_rated`
  at its node; a unit without an efficiency uses the factor as is.
- **Per-unit bases of the DC network.** `dc_power_flow.base_impedance` was
  used both as Z_base (Ω), to turn a line's length into reactance, and as
  S_base (MVA), for the DC flow and losses. S_base is now
  `ac_power_flow.base_mva` (default 100, so flows and losses are unchanged
  by default) and a line without `reactance_pu` gets
  length × `reactance_per_km` / (V²/S_base) with its own voltage, the
  convention of the Grid Builder's per-unit values; it used a fixed
  100 Ω at every voltage. `base_impedance` is ignored (with a warning when
  set). The master's node links carry the parallel reactance of the lines
  they stand for; they used the fuel transport distance (or 0.01 p.u.).
- **Reserves are headroom, not energy.** Static and dynamic reserves no longer
  enter the power balance as demand; they are kept as spare capacity on the
  committed units. Runs with reserves dispatch less generation, fuel and CO2
  than before.
- **Stochastic master problem is a real two-stage model.** Each scenario now
  has its own representative-day operation with the common investment plan,
  and the scenario multipliers (demand, fuel, maintenance, CO2 price,
  investment costs) change the plan. `demand_growth` acts as an annual factor
  (demand × `demand_growth`^(year − 1)). A warning is logged if Benders is
  selected, which ignores the scenarios.
- **CO2 cost now affects planning.** The master problem charges `co2_cost` ×
  fuel emission factor (`fuels.*.emission_factor`) in its operating cost, on
  representative days and with TSAM, so a CO2 price can change what is built.
- **Transmission corridor cost is charged once**, at the higher of the two end
  nodes' `transference_invest_cost`, instead of once per direction. The master
  problem builds more transmission than before.
- **Ramp limits are a fraction of capacity per hour.** With `resolution_hours`
  > 1 the limit per step is `ramp × resolution_hours` (at most the full
  capacity). A ramp of 0 now means no limit, new Studio generators default to
  1.0, and the Studio labels say pu/h instead of pu/min.
- **Minimum up/down times are in hours** and are converted to time steps
  (`ceil(hours / resolution_hours)`).
- **Unserved demand is bounded by the demand on its bus** in every
  formulation, so load shedding can no longer act as a virtual generator.
  Reported shedding can be higher than before where it used to hide a real
  shortfall.
- **Rolling horizon:** default `overlap_hours` raised from 6 to 24; an overlap
  must be shorter than the window; the last window of each year has a soft
  end-of-year battery SOC floor.
- The perfect-foresight master problem no longer applies a hidden 2 %/year
  demand growth; demand comes from the demand file only.
- The master problem's per-node capacity adequacy counts imports up to the
  transfer capacity of the node's lines.
- With TSAM, the master problem charges reserve and inertia shortfalls like
  the representative-day master.
- **Preventive N-1 now takes effect.** Its shortfall was never added to the
  objective, so `n1_security` did not change the dispatch. It is now charged
  at `loss_of_reserve_static`, and generation N-1 is a headroom test: the
  other units (online ones in unit commitment) and batteries must be able to
  replace the largest unit's output. Runs with N-1 enabled commit more
  capacity than before.
- **Transmission losses in planning, and a sound linear model.** The master
  problem and Benders now apply `loss_model` (with `pwl_loss_segments_master`
  segments for `pwl`); their node links had no resistance and the network
  ignored the setting, so planning was lossless. Each link now carries the
  equivalent resistance of the bus-level lines between its nodes, so
  multi-node plans favour local supply more than before. `loss_model: linear`
  loses a fraction `resistance_pu` of the flow in either direction; it used
  to create energy on flows leaving a line's lower-index end. Line resistance
  is divided by `num_circuits`, like the reactance, so multi-circuit lines
  have lower losses than before.
- **Development-mode transmission investment in the dispatch is bounded and
  charged.** `tf_inv_*` (used when the dispatch itself runs in `development`
  mode, e.g. `PowerSystemAdapter` without `mode`, or `esfex.solve_dispatch`)
  had no upper bound and no cost, so any line could be built for free. It is
  now limited by `transference_invest_max` and charged
  `transference_invest_cost`, as in the master problem.
- **Energy balances use the step length.** With `resolution_hours > 1` the
  CO2 budget counted emissions in MW instead of tonnes, the EV fleet SOC and
  the SOC of new batteries in the master (and Benders) moved by MW instead of
  MWh, and the EV profiles were read hour by hour as if they were steps. All
  now scale by the step length and the EV profiles are averaged to it; the
  CO2 budget excess is charged once per tonne. Hourly runs are unchanged.
- **Flexible demand is shifted in the dispatch.** Sectors with `is_flexible`,
  `flexibility_ratio > 0` and `delay_tolerance > 0` can move up to
  `flexibility_ratio` of their demand per step by up to `delay_tolerance`
  hours, and the shift now enters the power balance (it used to change
  nothing). The flexible-curtailment "benefit", which lowered the objective
  without serving any less demand, is removed; `flexible_demand_benefit_ratio`
  is no longer used and `flexible_demand_benefit` in the cost breakdown is 0.
  `demand_shift_cost_rate` is charged per MWh and per hour moved.
- **Benders matches the monolithic master on renewables, and runs from the
  runner.** Its subproblems also capped each day's renewable output at the
  master's RE ratio, so with a low target renewables could not be used
  (with 0 % none was built); they now have a floor only. The runner's debug
  LP export no longer aborts Benders runs, and a one-node, one-step day no
  longer breaks the subproblem.
- **N-1 and SCOPF are soft and per physical line.** SCOPF generation N-1
  used the old form (others' output >= demand), which forced the largest
  unit to 0; it now uses the headroom test of preventive N-1. Corrective N-1
  lets every non-renewable unit re-dispatch within one step of its ramp, plus
  batteries. SCOPF line and generation constraints were hard (infeasible
  dispatch when unmet); their overloads and shortfalls are now priced at
  `loss_of_reserve_static`. Transmission N-1 and SCOPF read the flow of each
  physical line (they used the first circuit of a bus pair, with SCOPF also
  ignoring the line's direction). ACOPF line limits now apply scheduled
  outages and development-mode transmission investment.
- **Primary energy and EV inputs reach the model.** Non-electric fuel demand
  was about 1/12 of the configured annual demand; it now follows
  `non_electric_demand_growth` and `seasonal_factors` (it used 1 %/year and
  an even split), uses the month of the year (not of the window) and the step
  length. Non-electric CO2 used to be 0 (the fuel's `emission_factor` was not
  read). Unmet non-electric demand is priced per sector at
  `non_electric_demand_loss`. Coupled generators no longer pay `fuel_cost` on
  top of the fuel the supply chain buys, and fuel drawn from the tanks is
  charged at the fuel price (stored fuel is not free within a window). Fuel
  storage and transport are built
  by the dispatch's primary-energy block in development mode too, at the
  window's share of the annuity, and built capacity carries over to later
  windows (it was never kept, and the master never built any). A route
  without `route_id` gets `route_<index>` instead of disabling the block.
  The master plans with the EV charging load, and `EV_initial_soc` sets each
  window's starting fleet SOC (it was always 80 %).
- `ev_loss` defaults to the `loss_of_load` penalty (a warning is logged if it
  is set lower).
- Per-node `demand_fraction` values are rescaled to sum to 1 (with a warning).
- Grid Builder: the "Reconnect isolated" option is removed; island
  subsystems are kept as built from OpenStreetMap.

- **Only the master builds fuel infrastructure.** Each dispatch window used
  to build fuel storage and route capacity on its own, at an annuity × its
  share of the year, in `development` and `economic_dispatch` modes (and
  nothing with `use_uc_in_dispatch` or in `unit_commitment` mode). Now the
  master builds them, and each dispatch year receives the capacity in
  service that year as existing capacity. The master's built electrolyzer
  power is added to each electrolyzer's `rated_power`. A myopic master
  starts each year from what earlier years built. Runs without a master
  (`economic_dispatch`, `unit_commitment`) build no fuel infrastructure.
  The HDF5 file has a new per-year `master_fuel_capacity/` group, and
  `primary_energy/storage_investments` and `transport_investments` are no
  longer written. `pe_investment_cost` is always 0.

- **Fuel-coupled units pay the fuel price in the master too.** With primary
  energy on, the master's generators and candidate technologies that burn a
  modelled fuel now pay for it through the fuel balance (price + node import
  cost), as in the dispatch, and no longer pay their flat `fuel_cost`. Units
  with a fuel-cost curve keep their curve. This changes the master's plan
  wherever `fuel_cost` and the fuel prices disagree.

- **`penalties.coupling_slack_penalty` is deprecated.** It has priced nothing
  since the primary-energy model lost its periodic–hourly coupling slacks
  (the `pe_coupling_slack_cost` result stays 0, kept for file
  compatibility). A configuration that sets it still loads, now with a
  warning; the Studio no longer shows it in the system form nor writes it.
- **Unused SCOPF settings are reported.** `n1_security.scopf_max_iterations`
  and `scopf_violation_tolerance` have no effect (the SCOPF constraints are
  built in one pass). A value other than the default now logs a warning when
  the configuration is loaded; the Studio tooltip says so.

- The unused Grid Builder step class `GridMappingConnectStep` (the old
  "Step 5: Simplify & Aggregate") and its translation keys were removed.
  It was not reachable from the Studio.

- `BatteryConfig` accepts both `availability_file` and `Availability`
  (the docs said only the alias), and generators accept both names too
  (the docs said `availability_file` was ignored for generators).

### Fixed

- **Myopic planning remembers earlier years.** Each one-year master in
  `planning_mode: myopic` planned from the base-year fleet: the aged units
  and the capacity built in earlier years were applied to a copy of the
  configuration the master never read, and were matched by technology key
  against generator keys, so they never applied. Every year rebuilt what the
  year before had built, and the operational dispatch received none of the
  myopic investments. Each year's master now starts from the merged master
  system with its units aged by the years elapsed, holds the capacity built
  in earlier years as existing units of their technology (at the nodes where
  they were built, ageing from their build year, retired at the technology's
  lifetime), and can only build what is left of each technology's
  `invest_max_power` / `invest_max_capacity` (a limit over the whole
  horizon, as in perfect foresight). The dispatch of each year now gets that
  year's capacity, like a perfect-foresight run.

- **Reservoir inflow on the master's representative days.** Each
  representative day of the master problem (also each TSAM period and each
  Benders subproblem) read the availability profiles of its own hours, but
  the reservoir inflow from the start of the profile: every day saw the
  inflow of the first hours of the year (the first hours of the year's block
  in a myopic run). Hydro investment and dispatch in the master were planned
  on that inflow. Each day now reads the inflow of its own hours.

- **Distinct ids for fuel routes between the same systems.** The routes of
  an inter-system `fuel_route` link were named `link_<A>_<B>_<i>_fwd|rev`
  with `i` counted within the link, so two fuel links between the same
  systems (Diesel and LNG, say) produced the same route ids. The ids are now
  `link<k>_<A>_<B>_<fuel>_<i>_fwd|rev`, with `k` the link's position in
  `systems_links`.

- **`esfex studio` no longer ends in a traceback after "Save anyway".**
  When the last save wrote a configuration with errors, reloading the file
  after the window closed raised and the CLI printed a traceback. It now
  says the file was saved but does not validate, prints the error, and exits
  with code 1. `launch_studio()` raises the new
  `esfex.config.loader.SavedConfigInvalidError` (a `ConfigLoadError`, with
  the file in `path`), and the `esfex-studio` launcher closes quietly.

- **Primary-energy cost totals in $.** The `total_fuel_cost`,
  `total_transport_cost` and `total_loss_penalty` attributes of each year's
  `primary_energy` group were written in M$ (the solver's internal unit),
  while every other cost in the results file is in $ and the Studio's
  primary-energy cost charts read them as $ (showing values a million times
  too small). They are now in $; the primary-energy tutorial said M$ and is
  corrected.

- **Minimum output of units built by the master.** The dispatch's units for
  master investments stored the technology's `min_output` times the built
  MW in `min_power`, which every other unit reads as a fraction of rated
  power. With unit commitment their minimum stable output was
  `min_output × rated²` MW, so a built thermal unit with a minimum output
  could not be committed. `min_power` is now the technology's `min_output`
  per node.

- Two transmission links between the same two systems no longer share a
  line id: each link restarts its connection index, so the second one's
  lines were also named `link_<A>_<B>_0`; later ones now get a `_2`, `_3`…
  suffix.

- **Reservoir, battery-spillage, inertia, transfer-margin and N-1 results are
  kept.** The rolling-horizon window result left them out, so
  `YearResults.reservoir_level` / `reservoir_spillage` / `reservoir_pump` /
  `reservoir_invest_capacity`, `bat_spillage`, `loss_of_inertia`,
  `transfer_margin` and the N-1 duals and binding contingencies were always
  empty and their HDF5 groups (`reservoir_*`, `battery_spillage`,
  `loss_of_inertia`, `transfer_margin`, `n1_security/`) were never written.
  They are now forwarded, trimmed to each window's committed steps like the
  other series, summed from buses to nodes, and expanded back to the
  original buses under network reduction. `reservoir_level` holds the level
  at the start of each hour (`(nodes, hours)`, like `battery_soc`); the
  reservoir capacity built by the dispatch is written as
  `reservoir_invest_capacity/{name}`. `n1_security_cost` is still 0: the
  model does not compute the incremental cost of N-1 (the shortfall penalty
  is `cost_breakdown/n1_security_shortfall_cost`), which the docs now say.
- **Primary-energy results over rolling-horizon windows.** Each window's
  per-period fuel supply, non-electric demand served, fuel shortfall and
  transport flows were added element-wise to the other windows', overlap
  included, and the fuel, transport and shortfall costs were summed in full
  (about twice the real values with 48-h windows and a 24-h overlap); a last
  window with fewer fuel periods than the others stopped the run with a
  broadcast error. The year now keeps only each window's committed steps,
  like the dispatch arrays, re-binned into periods of
  `primary_energy_resolution` hours from the start of the year, and the
  window costs count in proportion to those steps.
- **Electrolyzers with the same name no longer stop a run.** The
  multi-system merge keeps electrolyzer names, so two systems with an
  electrolyzer called `Elz` made the HDF5 export fail with "name already
  exists" after the first year. Electrolyzer datasets now get unique names
  like every other per-unit group.
- **One suffix for homonymous units.** The runner's unit lists suffixed a
  repeated name with ` (1)`, the HDF5 writer with `_2`, and the financial
  analysis looked for ` (N)`. Every place now uses ` (1)`, ` (2)`, ...: two
  generators named `Gas` are written as `Gas` and `Gas (1)` (files written
  before used `Gas_2`).
- **`--start-year` only relabels.** The Feb 29 rows dropped from leap-year
  demand, availability and inflow data followed the relabelled calendar, so
  relabelling 2027-2028 data as 2024 dropped 24 hours of 2027 and kept
  29 Feb 2028. They now always follow the data's own calendar
  (`date_start`).
- **`esfex.solve_dispatch(config, year=Y)` dispatches data year Y.** It ran
  the first data year labelled Y and moved the outage base year to Y, so
  scheduled interruptions given in horizon hours of year Y never applied. It
  now dispatches the (Y − year of `date_start`)-th year of the data with the
  run's own outage base year; a year before `date_start` raises
  `ValueError`. `Orchestrator.run()` takes a new `dispatch_years` argument
  (the same as the `ESFEX_OPS_ONLY_YEARS` debug variable) for this.
- **Reservoir inflows above 1 MW-eq were clipped to 1.** Inflow files were
  read with the availability loader, which clips capacity factors to
  [0, 1]. A new `load_inflow_profile` reads them as given (a missing file is
  an error instead of a flat 1.0).
- **The master problem reads the same profiles as the dispatch.** It loaded
  availability and inflow files itself without `date_start`, so multi-year
  leap-year data kept Feb 29 in the master while the dispatch dropped it.
  It now uses the dispatch's preloaded profiles (and the data calendar for
  any it reads from disk). Each myopic one-year master read the first year
  of a multi-year profile; it now reads its own year's block, and the TSAM
  clustering of every year reads that year's block too, wrapping around a
  shorter profile like the dispatch instead of falling back to the first
  year.
- **Initial RE share.** A renewable unit without an availability profile
  counted as 0 in the initial RE share (and in the cap on the operational RE
  target), while the dispatch runs it at full capacity. Both now count it
  at full capacity, and the cap reads the year's own profile block.
- **Development-zone emissions count against their system's CO2 budget.**
  In a multi-system run zone nodes are appended after every system's nodes,
  so they belonged to no system's budget. Each zone node now joins the
  system of the bus it connects to.

- **Fuel in transit is no longer lost between rolling-horizon windows.**
  Fuel shipped in a window's committed steps that arrived after them was
  paid but never delivered, and the next window saw no shipment made
  before it: with the default 48 h / 24 h windows and a transit of a day or
  more, a node fed only by a route was short at the start of every window.
  The fuel still in transit at the commit point is now carried to the next
  window (and from a year's last window to the next year) with the tank
  levels, and arrives there in its scheduled step.
- **A unit at several buses of a node burns the node's fuel once.** The
  fuel coupling was one constraint per bus against the same node fuel, so
  a unit at two buses (for example a technology built at two buses) only
  bought fuel for its largest bus and the rest of its output was free. The
  output is now summed over the node's buses.
- **Tank dispatch rate.** `max_hourly_dispatch_rate_fraction_of_capacity`
  forced zero draw at nodes without a tank (all their use ran on the
  shortfall penalty) and was applied per time step instead of per hour. It
  now applies only where there is a tank and is multiplied by the step
  length.
- **The master charges battery maintenance.** The dispatch charges a
  battery's `maintenance_cost` on every MWh charged and discharged; the
  master did not, and charged battery technologies neither maintenance nor
  throughput cost. All are now in the representative-day cost (monolithic,
  TSAM, multi-system, stochastic and Benders), and the stochastic
  `maintenance_cost` multiplier scales them too.
- **Candidate renewables' curtailment penalty is scaled to the year.** In
  the master it was added once per representative day instead of × 365 /
  days × step length (and without the step length under TSAM), so it was
  almost free next to the other operating costs.
- **Life extension in multi-system runs takes the unit's own candidate.**
  In a merged run every key is `{system}__{key}` and per-node costs are 0 at
  the other systems' nodes, so the named technology never matched and the
  first system's candidate was used: units of the other systems kept a
  life-extension cost of 0, with no warning. The candidate now comes from the
  unit's own system, and a unit whose candidate has no cost at one of its
  nodes is named in a warning.

- **The Studio no longer overwrites a configuration opened with
  `esfex studio -c`.** Autosave wrote straight into the `-c` (or `-o`)
  file, without a prompt, and cleared the unsaved-changes mark, so the CLI
  then reported "closed without saving". Worse, the `-c` file is loaded only
  once the map is ready; a Save, the close prompt or an autosave before that
  replaced the whole file with an empty `System_1`. Autosave now writes only
  `~/.cache/esfex/autosave.yaml` (also after File > Open) and leaves the
  unsaved-changes mark on; Save, Export As and Export Project are disabled
  until the `-c` project has loaded, and closing does not offer to save it
  meanwhile; and an empty project (no system, or only an empty placeholder
  system) is never saved over a file that defines systems.
- **`esfex studio -c a/cfg.yaml -o b/out.yaml` keeps the data paths
  working.** Relative demand, availability and inflow paths were copied
  unchanged, so `out.yaml` pointed at files that do not exist next to it.
  Saving to another folder now rewrites them relative to the new file when
  the data lies under its folder, and as absolute paths otherwise (also for
  the autosave copy and the temporary config of a Studio run). Export
  Project resolves them against the folder the configuration was loaded
  from, not the `-o` folder.
- **Realism check: the transmission bottleneck uses each node's own peak and
  no longer blames the grid for missing generation.** The max-flow test split
  the system peak by each bus's demand fraction divided by the sum of all
  fractions (which add up to 1 per node), so the demand of every node was
  wrong; and when installed capacity was below the peak (a greenfield
  project) the shortfall was reported as a transmission bottleneck. Each
  node's peak is now split over its own demand buses, and only demand the
  installed generation could serve but the network cannot deliver is
  reported; capacity below the peak stays a Generation finding.
- **Realism check: no more "duplicate plants" made of one key's per-node
  instances.** `gas_n1` … `gas_n8` share their key's name and were grouped
  as one plant counted eight times (Cuba showed groups of up to 57 units).
  Units now match only within 15 km, never within one generator key, and a
  key with instances in several nodes is not compared; besides an identical
  name, same-fuel neighbours must share a distinctive name word, so two
  different "Parque Fotovoltaico" plants or unnamed OSM turbines next to each
  other are not flagged.
- **The interruptions calendar covers the whole simulation horizon.** It
  spanned one year (8760 h) and clamped every edit to it, so a window could
  not be placed after the base year, although outage hours count over the
  whole horizon; and its base year was always 2025. It now starts on
  1 January of the year of `date_start` and spans the 25 years a Studio run
  simulates (more if a window is already scheduled later), with a year axis
  for multi-year horizons.
- **A power line and a fuel route between the same two nodes are no longer
  reported as duplicate inter-system links.** The duplicate check ignored
  the link type (and the fuel); it now compares links of the same kind only.
- **Deleting the last inter-system link or the last stochastic scenario now
  persists.** An empty list was not written, so the loaded file's links or
  scenarios came back on the next load. The saved file now holds exactly
  the Studio's links and scenarios, `[]` included.
- **The Risk & Resilience Workbench's climate and hazard scenarios are
  saved.** Apply stored them on attributes the global settings did not have,
  and the serializer never wrote them, so they were lost on save. They are
  now kept in the global settings, saved as `risk.climate_scenarios` /
  `risk.hazard_scenarios` and read back on load (keys the schema does not
  know are dropped; climate probabilities are rescaled to sum to 1 so the
  file still loads).
- **Studio risk defaults match the schema.** A new project used a base
  temperature of 18 °C and heating / cooling coefficients of 0, while the
  `risk:` block defaults to 24 °C, 0.5 and 2.5 %/°C; the Studio now uses the
  schema's values (also in the Workbench's climate tab), and the
  coefficient labels say %/°C instead of MW/HDD and MW/CDD.
- **Grid Builder demand step: no crash when no forecast or no node is
  available.** A loop variable named `tr` hid the translation function, so
  the "run the demand forecast first" and "no nodes" messages raised
  `UnboundLocalError`.

- **Inter-system fuel routes are fuel routes.** A fuel route traced in the
  Studio between two systems was saved as an electrical link (no type, no
  fuel), came back as a transmission link after reloading, and the run
  turned it into a power line. `meta_network.systems_links` entries now
  have `link_type` (`transmission`, the default, or `fuel_route`) and
  `fuel`; the Studio saves and reloads both, and the link form has a fuel
  selector for fuel routes. On merge, each connection of a fuel route link
  becomes two one-way primary-energy transport routes between the two
  nodes (capacity = `existing_capacity_MW` in fuel units/day,
  `transport_cost` = `cost_per_mw_km`, `losses_fraction` = `loss_factor`
  per 100 km, length = `distance_km`); it adds no electrical line,
  adjacency or transfer investment. Route investment follows the fuel's
  `fuel_infrastructure`, so the link's `max_investment_MW` and
  `investment_cost_per_MW` are not used (a warning is logged when set). A
  fuel route without a fuel is ignored with a warning, and the Studio
  validation flags it.

- **Reservoir pumping on multi-bus nodes.** The DC-OPF and AC-OPF bus
  balances looked up a reservoir's capacity by node index in per-bus
  arrays, so the pump of a reservoir off its node's first bus was left out
  of the balance: pumped water was free.
- **Built technology capacity keeps the technology's bidding curve.** The
  virtual generators that carry master investments into the dispatch got
  the flat `fuel_cost` only; they now carry `fuel_cost_curve`. A curve at a
  bus where a unit has no capacity no longer raises an error in the
  dispatch.
- **Development-mode dispatch could buy a 0-MW line but not use it.** The
  flow on a candidate corridor below 0.1 MW stayed fixed to 0 even after
  investment (Julia API only; the runner dispatches in economic-dispatch or
  unit-commitment mode).
- **Benders load-shedding penalty cap** iterated the per-bus load-shed
  variables by node count.

- **Fuel-cost multiplier and fuel-cost curves.** In a stochastic scenario,
  `fuel_cost` and `fuel_price_growth` did not scale generators that use a
  piecewise fuel-cost curve; they do now.

- **Yearly energy and CO2 totals with steps longer than 1 h.** The
  dispatch summed MW (and tCO2/h) per step for total generation, demand,
  curtailment, load shed and CO2, so with `resolution_hours > 1` every
  yearly total was divided by the step length: a 24-hour-step run reported
  22,417 t of CO2 for a real 538,000 t. This affected `YearResults`
  (`emissions`, `total_generation`, `total_demand`, `load_shed`), the HDF5
  scenario attributes and `summary_results` / `global/summary_results`,
  and the runner's `gen=… MWh` / `CO2=… t` log lines. The totals are now
  the step values times the step length. The `CO2_emissions` dataset
  stays a per-step rate, like the MW datasets; its `units` attribute now
  says `tCO2/h` (it said `tonnes CO2`) and the readers multiply it by
  `temporal_resolution_hours`, as before. Hourly runs are unchanged.
- **CSV export of multi-node results.** `esfex export -f csv` stopped with
  `ValueError: Must pass 2-d input` on any multi-node run, at the 3-D
  `power_flow` / `transfer_margin` datasets. These are now written with
  one row per node pair (`from_node`, `to_node`, then one column per
  step), scalar datasets as one cell, and nested groups
  (`technology_selling_prices/<gen>/`, `n1_security/trans_reserve_duals/`,
  `primary_energy/`) as nested folders instead of being skipped.
- **Rolling-horizon `power_flow`, `transfer_margin` and N-1 duals.** These
  series were joined window by window including each window's overlap
  tail, so they had more steps than the year (`power_flow` of
  `(3, 3, 10008)` for 8760 h) and drifted out of step with every other
  array. They now keep only the committed steps. A transmission N-1 dual
  (or the generation N-1 dual) that binds only in some windows is
  zero-filled in the others instead of being concatenated short.
- **Inter-system flows with a bus network.** `inter_system/.../power_flow`
  read the bus indices of each line as node indices, so when buses differ
  from nodes the flows between systems were attributed to the wrong
  systems or lost. Line ends are now mapped bus → node → system. The
  scenario `transfer_margin` dataset had the same mix-up and is now
  aggregated to node pairs like `power_flow`.
- **HDF5 per-unit dataset names.** `technology_selling_prices/` used the
  raw generator name, so a name with `/` (`Cuba/Gas`) created nested
  groups, and two generators with the same name overwrote each other. In
  the battery groups (and in the other per-generator groups outside
  `generation/`) two units with the same name made the HDF5 write fail.
  Every per-unit group now uses the same `/`-free, unique name per unit
  (`Cuba - Gas`, `Cuba - Gas_2`, ...).
- **`num_years` in unit-commitment runs.** The HDF5 root attribute stored
  the requested number of years, although unit-commitment mode runs only
  the first; it now stores the number of years simulated.
- A missing open solver no longer suggests `] add <Package>`: the message
  points to `esfex add-solver` for Gurobi, CPLEX and Cbc, and to
  reinstalling ESFEX's Julia project for the solvers that ship with it.
  Docstrings: `aggregate_demand_to_resolution` returns the MEAN per period
  (it said MAX), and the `compile` argument of `initialize_julia()` is
  documented as unused.

- **Excel demand: the first row is data again.** A run read Excel demand
  files with the first row as a header, so a file without a header lost
  its first hour and every later hour moved up by one. The run now reads
  CSV and Excel the same way, from the first row; leading rows with text
  (a header such as node names) are skipped, so files with or without a
  header give the same hours. Availability files follow the same rule (a
  header row used to make the profile fall back to a constant 1.0). A
  text cell below the header is reported as
  `non-numeric value in data row <n>`.
- **Multi-year availability profiles in the dispatch.** The dispatch
  looked up availability and reservoir inflow by the hour of the year, so
  every year used the first 8760 rows of a multi-year profile while the
  master used each year's own rows. The dispatch now indexes profiles by
  horizon hour, wrapping around like the master.
- **`load_demand_data(..., year_to_load=Y)`** returned the file's first row
  followed by the rows of year Y (off by one), and used calendar years of
  8784 h. It now returns the 8760-row block of year Y, raises `ValueError`
  for a year outside the data, and reads the rows a run reads.
  `DemandDataManager` also indexes 8760-row model years.
- **Custom constraints: integers on a unit axis.** An integer index on a
  generator, battery or technology axis was rejected as an unknown name,
  even when it matched a key such as `"1"`. Names now match as strings
  (also `"2030"` on a year axis), and an integer that is not a key is the
  unit's 1-based position.

- **Studio edits of system settings were lost on reload.** The Studio saved
  settings such as `target_re_penetration`, `min_annual_increment`,
  `max_annual_increment`, `force_replacement`, `inertia_limit_threshold`,
  `sim_rooftop` and `ev_initial_soc` twice: the value the file was opened
  with under the upper-case alias (`TARGET_RE_PENETRATION`) and the edited
  value under the field name. The alias won on the next load, so the edit
  was discarded. The Studio now writes each setting once, and the loader
  uses the field name when a file has both (with a warning if they differ),
  which recovers edits in files saved earlier.
- **Risk settings reset on the second save.** The Studio wrote the `risk`
  block but did not read it back when opening a project, so the next save
  replaced it with the defaults. It now round-trips. The block is documented
  as Workbench-only: the optimization reads neither it nor `risk.enabled`.
- **SLD frequency gauge ignored the system's frequency keys.** It used fixed
  values (load damping 0.01 pu, ROCOF limit 2 Hz/s, 50 Hz and 49 Hz with
  results loaded). It now uses `load_damping`, `rocof_limit`,
  `frequency_nominal` and `frequency_nadir_limit` of the edited system; a
  nominal frequency left at 50 Hz follows the buses' frequency and a nadir
  limit left at 49 Hz becomes 1 Hz below nominal.
- **HiGHS offered for the SOC and QC AC-OPF formulations.** HiGHS rejects
  second-order cone constraints, so these runs failed at the first solve.
  The Studio no longer offers it for `acopf_soc` / `acopf_qc`.
- **`esfex run --mode/--solver` accepted any value.** An unknown mode or
  solver was written into the configuration unchecked. Both are now checked
  against the values the configuration accepts (the solver name is
  case-insensitive), the help lists all of them, and the verbose header
  prints `Mode: (from config)` instead of `Mode: None`.
- **`esfex studio -o` had no effect.** File → Save now writes to the `-o`
  file, or to the `-c` file when `-o` is not given (it used to ask for a
  path), and relative data paths resolve against the `-c` file's folder.
  `launch_studio()` returned `None` even after a save; it now returns the
  saved configuration.
- **`Orchestrator(config_path="x.yaml")` with a string** raised
  `AttributeError`; a `str` path now works like a `Path`.
- **`environment.yml` pinned Python below 3.13** although ESFEX supports
  3.13; it now allows the same versions as pip (3.10–3.13).
- **No-effect settings are marked as such.** The `Curtailment` penalty the
  Studio writes was dropped on load; it now loads, like
  `coupling_slack_penalty`, `transport_congestion` and `storage_violation`,
  and all four are documented as having no effect and shown disabled in the
  Studio. `solver.scale_constraints` is read by nothing; it is documented as
  such and shown disabled.
- The loader names each generator that receives the non-fuel O&M adder in
  the log (INFO), with the resulting `maintenance_cost`.

- **Plugin commands work.** `esfex <plugin name> ...` failed with "No such
  command": the CLI only discovered plugins and never loaded them, so
  `get_cli_commands()` was never called. When `esfex` starts with a command
  that is not built in (or with `--help`), it now loads the enabled, trusted
  plugins and mounts their commands; `esfex --help` lists them. Built-in
  commands such as `esfex run` do not load plugins at startup. A plugin that
  fails to load, returns something other than a Typer app or is named like a
  built-in command is skipped with a message, and the other commands keep
  working.
- **Plugin configuration hooks are called.** `get_config_schema()` and
  `on_config_loaded()` were defined but never called. When a run starts, each
  plugin's `plugins.<name>` section is validated against its schema (an
  invalid section stops the run with an error naming it) and
  `on_config_loaded(config)` is called; the Studio does the same when it
  starts with a config file and logs invalid sections as warnings.
  `context.config` is updated before the call.
- **Plugin tree categories, forms, toolbar actions and map layers appear in
  the Studio.** The plugin manager looked for the main window's panels under
  names it does not have, so these four hooks were never called. A plugin
  category now appears in every system of the element tree (also in systems
  added later, with its label), and `element_tree.add_plugin_element()` adds
  elements to it whose selection shows the plugin's form. Returned toolbar
  actions are added to the toolbar.
- **`esfex.solve_master()` and `esfex.solve_dispatch()` in the Python console
  run.** Both raised `TypeError` (the adapters were built without their
  demand and horizon arguments). They now go through the runner:
  `solve_master()` returns the master problem's investments, retirements,
  cumulative capacities and RE targets per year; `solve_dispatch()` dispatches
  one year with the configured units and returns its `YearResults`. Both take
  `solver=None` (keep the config's solver) and an `output_dir`.
- **`esfex.load_results()` reads current results files.** Only `demand`,
  `power_flow` and `curtailment` matched the HDF5 layout; generation held
  dataset names and prices, storage, investments, summary and the unit names
  came back empty, so `esfex.plot.generation_mix`, `prices` and `investments`
  drew nothing. Generation and battery state of charge are now
  `[unit, step]` arrays summed over nodes, prices are the nodal prices,
  investments are MW per technology name and year, and summary holds each
  year's scalars. A new `years=` argument limits what is read.

- **Renaming a system.** The rename did not reach the inter-system links,
  Global Settings > Systems to Simulate or the saved `meta_network.systems`,
  so the renamed system dropped out of Validate and Run, its links pointed to
  a missing system and the saved file no longer loaded. Settings the Studio
  does not edit were also lost. All these references now follow the rename,
  the system keeps its position, and renaming onto an existing name shows a
  message instead of raising an error. Save drops names that are not
  systems of the project from `meta_network.systems`, and stochastic
  scenarios are written to the first simulated system, where they are read.
- **Inter-system links keep their impedance on reload.** Opening a project
  or importing systems restored links without `reactance_pu` /
  `resistance_pu` (and, on import, without the electrical metadata and
  style), so they fell back to the defaults. Imported links also get fresh
  ids instead of reusing ids already in the project.
- **SLD Run All N-1 and Run Screening.** Both buttons failed (a missing
  analyzer method, a wrong call signature, attribute access on dicts, and an
  undefined logger that turned every error into a crash). They now report
  the number of contingencies and violations (N-1), the stressed N-1-1 pairs,
  or the contingencies above the PI threshold with the worst one. The
  N-k labels were untranslated. The N-k options of Global Settings (depth,
  redistribution, PI threshold, transformer and battery contingencies,
  critical line threshold), which were saved but used nowhere, now preset the
  SLD Analysis panel and select which elements are tripped; the optimisation
  does not use them, and the form says so.
- **SLD time controls after Analysis.** Switching Analysis off left the
  year, hour and playback controls disabled; with results loaded they come
  back and the diagram shows the results again.
- **Geo imports in a projected CRS** (for example a UTM shapefile, or a
  GeoJSON with a projected `crs` member) are reprojected to WGS84 instead of
  being drawn in the wrong place.
- **Degradation labels.** The generator, battery, electrolyzer and AC/DC
  converter forms labelled degradation in %/yr while the value is a fraction
  per year; the labels now say frac/yr.
- **Reservoir section for new hydro units.** The generator form only showed
  the Reservoir section when a reservoir capacity was already set, so a hydro
  unit created in the Studio could never get one. It now shows for fuel
  Water too.
- **Fuel route waypoints.** Clicking empty map space while tracing a fuel
  route now adds a waypoint, as for transmission lines.
- **Script editor.** `Ctrl+Enter` runs the current script (the Run tooltip
  announced it); the auto-indent preference works; editor preferences apply
  from startup, not only after the Preferences dialog is closed.
- **Interruptions calendar dates.** Hours were turned into dates with the
  real calendar, so in a leap base year every date after 28 February was a
  day off. Dates now follow the model calendar (8,760 h per year, no
  29 February).
- **Building-footprint demand split (Grid Builder).** Each node's bus split
  counted every building in the domain, snapped to that node's own buses,
  which biased the split of multi-node domains. Each building now belongs to
  the node of its nearest bus, and only a node's own buildings set its split.
- **GEM plants with a blank status** are kept, as documented; an early
  status filter dropped them, and kept "construction" / "mothballed" rows only
  to skip them later.
- Grid Builder texts: the demand step header said "Step 6" (it is step 3);
  tooltips no longer mention an auto-connect step that no longer runs; the
  national-demand override tooltip says what 0 does for each forecast
  engine; the "Populate from Nodes" tooltip says what the button does.

- The dataset root (`ESFEX_DATA_ROOT`) defaults to `~/.esfex/data`. It
  defaulted to a maintainer-local volume, so the Grid Builder's Geofabrik
  and OSM tile caches and the CMIP6 cache failed for other users.
- Config-mode sensitivity parameters now all change the run:
  `invest_cost_transmission` (node corridor, development-zone and
  inter-system link costs), `maintenance_cost`, `carbon_price`
  (`co2_cost`) and `demand_growth` (now the demand level, `demand_scale`)
  had no effect and got zero indices. `fuel_price_growth` is no longer
  offered (a run has no fuel price escalation to vary), and a parameter
  key without a config field raises an error.
- Sensitivity analysis: config mode now reports the investment KPIs
  (`inv_gen_total`, `inv_bat_total`; they were always NaN and so got
  zero indices), reads only the newest results file of a run directory
  (a reused directory summed earlier runs in), and runs the number of
  years set in the new **Years per run** option or `years` argument (it
  always ran 25). A KPI with failed evaluations gets no indices (NaN, with
  the count in `n_failed`) instead of indices computed with 10 × the
  largest value standing in. LP mode's `inv_bat_total` no longer adds MWh
  to MW. `esfex.sensitivity()` in the scripting console works (it raised
  a TypeError with `parameters` and analysed nothing without).
- Trusted plugins load on every start. Their hash included the
  `__pycache__/` Python writes on first import, so every installed plugin
  was skipped from the second start on. Bytecode and `.git/` are no longer
  hashed, and plugins are imported from source only. Untrusted plugins can
  be approved with the new **Trust** button in the Plugin Manager or
  `esfex plugin trust NAME`; both show which plugins are untrusted.
- In a merged multi-system run, a system without `transmission_lines_geo`
  keeps its `nodes_connections` in the dispatch (they become lines). Its
  internal links used to vanish from the dispatch as soon as the merged
  network had any line, such as an inter-system link, islanding its nodes
  while the master still planned with them.
- Life-extended capacity is derated by `risk_coefficient` in the master's
  capacity adequacy constraint (it counted at full rating once a unit passed
  its lifetime).
- Generator, battery, primary-energy source and penalty fields written by the
  Studio (droop, risk coefficients, reservoir and cascade fields, cost curves,
  `throughput_degradation_cost`, `soc_violation`, ...) are no longer dropped
  when a configuration is loaded.
- Generator types are normalised (case, translated Studio labels) before
  validation instead of being rewritten to `Non-renewable`.
- A battery's `Availability` file is kept when loading.
- Development zones work in multi-node and multi-system configurations; zones,
  custom constraints and outage schedules survive the merge of systems.
- Scheduled outages created in the Studio apply to the right unit and node.
- EV: driving consumption is no longer divided by 1000 twice; all categories
  contribute (weighted) to the fleet parameters; the V2G availability profile
  and the fleet size follow the S-curve growth; profiles are reproducible
  (seeded); driving energy is taken from the fleet SOC; V2G compensation is no
  longer subtracted from the objective a second time.
- Rolling horizon: storage, unit status, ramp and reservoir state are carried
  over from the end of the kept part of each window, at the right bus (also
  with network reduction), and inherited battery energy is passed in MWh.
- Network reduction keeps buses that host equipment and remaps bus indices.
- The electrolyzer is limited to its rated power in the dispatch.
- The native AC bridge works (it previously always fell back to DC), and AC
  contingency analysis restores generator output after each case.
- Topology: every node keeps at least one bus, and per-node arrays are mapped
  onto the bus network instead of being read positionally.
- Grid Builder no longer deletes islanded subsystems (for example ones linked
  only by HVDC) before demand is assigned.
- Studio projects resolve availability files like demand files, so runs from
  the GUI keep their availability profiles.
- Results Viewer: demand is read with the right orientation (fixes the Net
  Load Heatmap, demand line and Commitment Heatmap); chart titles removed, long
  labels shortened to fit, and Hourly Dispatch uses the Overview's names and
  colours.

- **Electrolyzer water cost is charged.** `water_cost` (\$/kg H2) was never
  used. The dispatch and the master now charge it on the hydrogen produced.

- **The master prices candidate technologies with their offer curve.** A
  technology's `fuel_cost_curve` was converted for the master problem and
  then never used: the master charged the technology its flat `fuel_cost`
  on every MWh, while the dispatch priced the capacity built from it with
  the curve, so the plan was made with a different marginal cost than the
  one it was operated at. The master now splits the technology's output
  into the curve's segments (segment *k* capped at its fraction of the
  cumulative capacity, times the availability for renewables) and charges
  each segment its own price, on every bus of the node. The Benders
  subproblems do the same and include the segment caps in their cut
  gradient, and stochastic scenarios scale the curve with the fuel-cost
  multiplier like the generators' curves.

- **Investments keep their bus with `network_reduction`.** The reduction
  renumbers the buses it keeps, and the master's per-bus results came back
  on the reduced numbering but were read as the system's own buses; the
  dispatch, in turn, read the units built from master investments (laid out
  on the system's buses) as reduced-bus arrays. New capacity could land on
  the wrong bus or at another node (a two-node system with one pruned
  connection bus put node 0's investment at node 1). The master's results
  are now laid out on the original buses, and the dispatch maps built units
  onto the reduced buses (capacity on an eliminated bus goes to the retained
  bus of the same node with the largest `demand_fraction`). This affects
  bus-level masters (a one-node system with several buses, or development
  zones) and every dispatch with the reduction on.

- **Myopic years keep earlier builds at their bus.** On a bus-level master,
  capacity built in an earlier myopic year was added to the next year's
  model as one unit per node, which the adapter placed at the node's first
  bus; behind a weak internal line that capacity was stranded and the next
  year built it again. Each bus with new capacity now gets its own unit
  (`{technology}__built_y{n}__{bus_id}`) pinned to that bus.

- **Unclassified shed demand is priced like the sectors around it.** Node
  demand that no sector accounts for (sector shares that do not add up to
  the node's demand) was shed at VOLL × 1, below every sector's criticality
  multiplier, so the optimizer always shed it first. It is now priced at
  the demand-weighted mean of the node's sector multipliers in that step
  (1 where no sector has demand), in the dispatch and in the master's and
  Benders' representative days.

- **Inter-system links charge their operating cost in the dispatch.** In a
  merged multi-system run each transmission link became a plain line and its
  `cost_per_mw_km` was dropped, so energy crossed the link for free. The
  merge now sets the link line's `cost_per_mw_km` (the field the master
  already reads), network reduction carries the per-MWh cost to the merged
  lines, and the dispatch charges `length_km × cost_per_mw_km` on |flow|
  (DC power flow; reported as `line_flow_cost` in the cost breakdown and the
  dashboard waterfall), as the master does. Configurations that used large
  placeholder values for `cost_per_mw_km` (documented as "not used") now
  pay them.

- **The dispatch charges the converters' variable cost.** The objective was
  built before the AC/DC and frequency converter variables existed, so their
  `variable_cost` was never charged and `converter_cost` was always 0. It is
  now added once the converters are built, in every power-flow mode.

- **Start-ups cost their full price in the dispatch.** The objective
  prorated each start-up by the window's share of the year (a 24 h window
  charged 24/8760 of `start_up_cost`), while the cost breakdown reported the
  full amount; unit commitment started units almost for free. Start-ups are
  now charged in full, like the breakdown.

- **A primary-energy node without a tank no longer stores fuel.** The level
  of a node with no tank capacity had no upper bound, so it worked as a free
  tank within each window (fuel bought or shipped early kept for later
  hours). Its level is now bounded by 0: fuel there passes through within
  the step.

- **`pe_electrolyzer_cost` reports the electrolyzer cost again.** It was
  always 0 after the primary-energy electrolyzer model was removed. When the
  electrolyzers feed the `Hydrogen` tank it now shows their cost as charged
  by the dispatch; it is the same money as `electrolyzer_cost` (reported
  twice, charged once).

- **Docs:** the constraint catalog no longer says the N-1 slacks are free
  (they have been priced since the N-1 fix); the HDF5 `total` cost is
  described as the objective value.

- **Bus-level master results are summed onto nodes.** On a master that
  solves on buses (a one-node system with several buses, or development
  zones), the investment keys `tech_investment_power_{t}_{b}` (and the
  battery and transmission ones) and the life-extension arrays are indexed
  by bus. The results summary printed after the master, the MGA export and
  the `investment_*` attributes of the results file (read by the results
  map and dashboard) took that index as a node and dropped every bus index
  past the node count, so those investments vanished. They are now mapped
  to the bus's node (`buses[b].parent_node`) and summed; transmission built
  between two buses of one node is left out of the node-level attributes.
  The summary's investment columns are now labelled with the candidate
  technologies (they used existing generators' names and dropped
  technologies past the generator count). A unit's `retirement_gen_{g}` is
  now the share of its capacity retired (it was the mean of the retired
  flags over all buses or nodes, including those where the unit has no
  capacity), and the Studio's retirement charts multiply it by the unit's
  total `rated_power` (they used the mean over nodes).

- **Rolling-horizon year totals count each window's committed steps.** A
  window solves its kept steps plus an overlap tail that the next window
  solves again; the year's objective, cost breakdown, generation, demand,
  curtailment, load shedding and CO2 took each window's whole-window total
  times `kept / window` steps, mixing the overlap hours in (on a load that
  grows through the window the year's demand came out 8 % too high in a
  test, while the dispatch arrays were right). Each window's values are now
  taken over its committed steps only: a cost counts for the steps it
  belongs to, a cost without a step (investment annuities, CO2-budget slack)
  for the window's committed share, a primary-energy period for the share of
  its steps that the window keeps, and fuel drawn from a tank is valued
  from the window's start to the end of its kept steps. The primary-energy
  cost totals (`total_fuel_cost`, `total_transport_cost`,
  `total_loss_penalty`) follow the same rule. `PowerSystemAdapter.
  get_solution_values(keep_steps=..., primary_energy=...)`,
  `PrimaryEnergyAdapter.get_results(keep_steps=...)` and the Julia
  `extract_solution(...; committed=committed_steps(...))` /
  `extract_primary_energy_solution(...; keep=...)` take the committed steps.
  `n1_security_cost` is still a whole-window figure counted for the window's
  committed share.
- **Dispatch profiles missing from the runner's cache are read like the
  runner's.** When a unit's `availability_file` was not preloaded,
  `PowerSystemAdapter` read the whole file without the data calendar
  (`date_start`, Feb 29) and without slicing the window, so every window got
  the first hours of the file whatever its year and start hour; a relative
  path was resolved against the current directory. It now uses the runner's
  reader (`esfex.io.demand.load_run_availability`) with the configuration's
  directory (new `config_path` keyword, passed by the runner) and slices the
  window as for a cached profile; technology units built by the master that
  match no cached file are read the same way instead of running at
  availability 1.
- **Rooftop solar settings reach rooftex.** The runner passed only
  `adoption_scenario`, `weather_variability` and `simulation_seed` to
  rooftex; `performance_ratio`, `systems_per_node`, `avg_system_size`,
  `initial_adoption`, `max_adoption` and `adoption_rates` were validated and
  ignored. They are now passed (unset or empty fields, as the Studio writes
  them, keep rooftex's defaults), so results change for configurations that
  set them. The multi-system merge also re-keyed `max_adoption` and
  `adoption_rates` by node index although they are keyed by scenario
  (`low` / `medium` / `high`), which dropped every value; it now keeps the
  first system's (warning when another system's differ), and a per-node list
  that no system sets stays unset instead of becoming zeros. The cost fields
  (`cost_per_kw`, `cost_reduction_rate`, `o_and_m_cost`) still have no
  consumer: rooftop solar is behind the meter, with no investment decision
  to price.

- **The map no longer throws away your edits after a renderer crash.** When
  the Studio's map page crashed (WebEngine render process), the reloaded map
  repopulated the whole project from the file loaded at startup, so every
  change made since loading was lost. The map is now redrawn from the
  project as it is in memory; the file is only read again when the crash
  happened before the project had been loaded at all.

- **Flat `dc_*` power-flow keys are read in any case.** The legacy system
  keys `dc_base_impedance`, `dc_reactance_per_km`, `dc_voltage_level_kv`,
  `dc_max_angle_diff_deg` and `dc_slack_bus` were read only as written in
  lower case or all upper case; a mixed spelling such as `DC_base_impedance`
  was dropped without a warning (the unknown-key check accepted it). The
  loader and the unknown-key check now share one table of the loader's
  key translations (`esfex.config.legacy_keys`), and penalties are read
  from every `PenaltiesConfig` field instead of a hand-kept list, so the
  two cannot drift apart again.

## [0.3.0] — 2026-08-27

### Added

- **Country-scale OSM via Geofabrik** — a local `.osm.pbf` extract backend
  (pyosmium) that downloads the relevant regional extract once and filters it
  offline, replacing the rate-limited / frequently-degraded live Overpass API
  for large study areas. Region is auto-selected by domain span; overridable
  with `ESFEX_OSM_BACKEND`.
- **PyPSA-style equivalent-network reduction (L2+)** — collapses each bus
  cluster to one node substation with aggregated generation/load, meshed by the
  parallel equivalent of the original crossing lines (capacity summed,
  `1/x_eq = Σ 1/x_i`).
- **CMIP6 climate from the public Google Cloud (Pangeo) Zarr store** — driven by
  the SSP selected in Grid Builder step 3 (so the temperature-driven demand
  responds to the chosen pathway), cached offline; the Open-Meteo fallback gains
  spatial dedup to the model grid and a daily-limit fail-fast.
- **Offline admin-1 node naming** (Natural Earth) — names every node instantly
  with no Nominatim rate limit.
- **IEEE benchmark reproduction** (`esfex_benchmark_repro/`) — DC/AC-OPF
  validation harness and reference results against scipy / PyPSA / pandapower /
  PYPOWER / PowerModels / Sienna on the IEEE 9–300 and PEGASE cases.

### Changed

- Default minimum-voltage filter lowered from 110 to **66 kV** so regional
  subtransmission (which holds many grids together) is kept; node-count ceilings
  raised to 5000 for country-scale builds.
- OSM query decoupled (roads fetched separately, best-effort), wasteful fields
  trimmed, and per-tile results cached. Data-source labels are backend-neutral
  ("OpenStreetMap").

### Fixed

- **Per-generator big-M** in unit commitment so aggregated units are no longer
  clamped below their rated power (which stranded surplus and shed load at VoLL).
- Demand distribution now handles single-bus (node-mesh) nodes; density cells
  are clipped to the study domain; demand is oriented (nodes, hours) so the
  shed share isn't inflated.
- Multi-island study domains are kept intact; OSM-untagged voltages are inferred
  instead of defaulting to 220 kV; implausible generator capacities are capped
  per fuel; `bus_id` is namespaced per subsystem when merging.
- Benign Qt (signal disconnect) and JuMP (`model.ext` copy) warnings silenced.

## [0.2.6] — 2026-08-08

### Added

- **`esfex add-solver` / `remove-solver` / `list-solvers`** — install optional,
  commercial solvers (Gurobi, CPLEX, CBC, SCIP, Xpress) on demand into a
  dedicated Julia environment that is stacked on the load path, so it survives
  ESFEX upgrades and always targets the Julia the app actually uses. The install
  verifies the solver loads, so a licence/build problem is reported up front
  rather than mid-solve.

### Fixed

- **Solver availability** is now read from ESFEX's Julia environment (the shipped
  Project.toml plus the optional-solvers env), not from unrelated Python
  companion packages — the GUI no longer offers a solver the backend cannot
  load. A project that names a solver unavailable on this machine is coerced to a
  usable one on load instead of failing mid-solve with an empty result.
- **Method-specific solver options** (e.g. HiGHS `pdlp_scaling`) are no longer
  forwarded while the LP method is on *choose*; forwarding them aborted the whole
  model build on some solver builds.
- **Runner** now fails loudly when no operational window solves, instead of
  logging `completed: objective=$0` and exporting an empty result.
- **Grid Builder — transmission-only model.** The minimum-voltage floor is now
  enforced on the *built* network: the sub-floor distribution layer that leaked
  in (unknown-voltage features assigned a low voltage during the build) is
  collapsed, relocating its generation to the nearest transmission bus. Auto-
  transformers between voltage levels are sized by level (275→600, 220→400 MVA…)
  instead of a flat 100 MVA. Together these remove phantom transmission
  congestion — measured load shedding on a Hokkaido UC run fell from ~37 % to
  ~5 %.
- **Grid Builder — step 3 data fetching.** World Bank macro indicators get a
  30 s budget with retries (was a single 15 s attempt), and Google Open Buildings
  (unavailable over source.coop's HTTPS glob) transparently falls back to
  Overture instead of failing the demand step.
- **Results charts** group generators by fuel when a system has no
  `TechnologyConfig` catalog (Grid Builder imports), instead of collapsing the
  whole fleet into a single *Other* bucket.
- **Map Results** system selector shows only the current run's systems; previous
  runs in the results directory (often other regions) no longer leak in.

### Changed

- **OSM fetching is faster.** Tiles are fetched concurrently across the Overpass
  mirrors, dense tiles subdivide on the first server-side timeout, and the query
  requests only what the builder consumes — `out center` (centroid) for
  point-like elements and `out geom` (full trace) for lines, dropping the blanket
  recursion that pulled every substation polygon vertex.

## [0.2.5] — 2026-08-06

### Added

- **Grid Builder — bounded reconnection & island cleanup** — a *Reconnect
  isolated ≤ N km* control connects each substation within N km of the backbone
  (tapping a nearby same-voltage line, or linking to the nearest same-voltage
  bus with a real-length line), then absorbs any component that remains
  dysfunctional — generation XOR demand — into the connected grid PyPSA-style:
  its generation/demand is moved to the nearest bus and the dead topology
  removed, while self-sufficient islands (generation **and** demand) are kept.
  Default 8 km; set to 0 to keep the network exactly as mapped.
- **Isolated load-island detection** — validation now reports a component that
  carries demand but has no generation and no path to any generator (unservable
  load the solver can only shed); this previously passed clean.

### Changed

- **Grid Builder data sources** — OSM, WRI, GEM and GridFinder are laid out in a
  single row and all enabled by default; combining them yields the most
  complete network.
- **Minimum generator capacity floor scales with the minimum voltage** — the
  wizard default tracks the voltage floor (~0.3 MW/kV; e.g. 110 kV → 35 MW) so
  distribution-scale plants don't clutter a transmission model as stranded
  islands. Edit by hand to pin a value; set to 0 to include every generator.
- Narrower element-tree and properties panels; compact *New System* dialog.

### Fixed

- **OSM fetch no longer silently returns partial data** — the Overpass fetch
  tiles the region at 1° (was 5°, which blew past the server timeout on dense
  regions and returned a fraction as a clean success), subdivides a tile that
  still times out down to 0.25°, re-queries a zero-element response once to rule
  out a dispatcher load-shed empty, retries transiently-failed tiles once, and
  warns loudly when coverage is incomplete.
- **Project save/load is faithful** — three data-loss bugs fixed: the
  generator→bus assignment (previously re-guessed by nearest coordinate and
  scrambled on every export/import), AC/DC & frequency converters collapsing to
  a self-loop, and a valid demand distribution being drained out of
  disconnected micro-grids. A Grid Builder network now round-trips with
  identical connectivity, stable across repeated export/import.
- **Generation deduplication** — OSM is authoritative and never merges its own
  units, so real multi-unit plants survive; a GEM/WRI plant is kept only where
  OSM is silent; a bare OSM location marker (no capacity tag) no longer drops
  the capacitated GEM entry; an OSM `power=plant` polygon is reconciled against
  its unit nodes (no double count; the polygon output excludes under-
  construction / decommissioning units); and only operating GEM plants are kept.
- **Transmission line ratings** — line circuit count is derived correctly from
  the OSM `cables` tag (3 cables = 1 three-phase circuit, not 3) and preserved
  as the maximum across merged segments, so a double-circuit backbone keeps its
  real thermal capacity.
- **Tooltips wrap to a logical width** instead of a single screen-wide line —
  implemented without a global event filter (which segfaulted alongside the
  embedded QtWebEngine map).
- Project export/import runs off the GUI thread; negative fuel `price_base` is
  accepted (waste-to-energy tipping fees); the QtWebEngine raster-tile memory
  budget is raised so large maps stop dropping tiles.

## [0.2.4] — 2026-08-03

### Added

- **Deterministic scheduled interruptions (maintenance / forced outages)** — a
  new interruptions calendar lets you take any electrical or primary-energy
  element out of service — fully or as a derate — over explicit time windows,
  and the optimizer respects them. Supported for generators, batteries, fuel
  sources, transmission lines, AC/DC and frequency converters, and transformers.
  Windows are edited in a D3 Gantt-timeline dialog (reachable from the toolbar
  and from **right-click → Schedule interruption** on an element in the tree),
  persist with the project, and compile into per-element, per-timestep capacity
  masks for the model — with no change to the operational formulation itself.

### Changed

- **Grid Builder — Bus Distribution unified** — the two-button flow (separate
  *Fetch demand* and *Distribute*) is replaced by a single method dropdown
  (spatial demand + building-footprint proxies) and one **Distribute demand**
  button; footprint options appear only for footprint methods. The step-3 demand
  and distribution labels were also tidied.
- **Validation dialog** — the *Auto-fix errors* button was removed (the
  underlying validation library function is unchanged).
- **System attributes** — the system name field now sits directly after its
  *Name:* label instead of being right-justified.
- **Interruptions calendar** follows the Studio's visual theme.

### Fixed

- **Large config and project save/load froze — or crashed — the GUI** — parsing,
  serialising and `.esfexp` bundling ran on the GUI thread, so a large
  multi-system file painted the progress dialog only partway and could crash Qt.
  Config load/save and project export/import now run on background workers while
  the window and its progress dialog stay responsive.
- **Grid Builder: demand distribution broke when CMIP6 was rate-limited** — a
  429 during the forecast fell back to a climate year that could carry NaN,
  which propagated into per-cell demand and then failed the whole bus
  distribution. Non-finite temperature and cell demand are now scrubbed, so
  demand and distribution compute from the fallback climate instead of breaking.
- **Grid Builder: CMIP6 climate fetch now retries** transient 429 / 5xx /
  network errors with exponential backoff instead of losing a node's future
  climate on the first throttle.
- **Grid Builder: availability profiles came back all-zeros** when the weather
  API rate-limited the concurrent requests; concurrency is capped and all-zero
  responses now trigger a retry.
- **Grid Builder: building footprints never downloaded** during bus
  distribution (Microsoft quadkey, Web-Mercator and GeoJSON parsing bugs).
- **Grid Builder: demand distribution reported "0 multi-bus nodes"** on all-HV
  grids where no bus was pre-tagged as load; it now falls back to all busbars.

## [0.2.3] — 2026-07-22

### Added

- **View a node's centroid on the map** — nodes are abstract and carry no
  permanent map marker (one would overlap the electrical and primary-energy
  elements), so a transient cue was added: the map flies to the centroid and
  drops a pulsing marker that removes itself after a few seconds. Reachable from
  a new **View** button next to the node form's centroid button (the former
  *Pick on map* is shortened to **Place**) and from **right-click → View on map**
  on a node in the element tree.

### Fixed

- **Generator type not preserved in non-English UIs** — the resource-type combo
  (Renewable / Non-renewable) showed translated labels while the model stores
  English literals, so editing generators in a non-English UI made every
  generator take the last-shown type and saved a localized string that broke the
  schema. The combo now keeps the English literal internally; legacy localized
  values in already-saved projects are normalized on load.
- **OTEC cycle diagrams under NumPy 2.x** — fluid saturation/entropy/enthalpy
  properties can come back as single-element arrays on some NumPy/CoolProp
  combinations, and `float()` on such an array is a hard error under NumPy 2.x,
  crashing the cycle-state and T-s / P-h diagram computations. These values are
  now coerced version-robustly.

### Changed

- **Grid Builder UI is now translated** — its labels, group boxes, checkboxes,
  radio buttons, buttons and tooltips were hard-coded English and never followed
  the selected language. All user-facing strings now use the translation system
  (en/es/fr/pt/ja), matching the Solar/Wind/Demand wizards.
- **New ESFEX logo** across the wordmark, window icon and documentation, with
  the splash logo no longer clipped and a smaller About-dialog logo.
- **Python support capped to `>=3.10,<3.14`** so installs on a too-new Python
  (which has no prebuilt wheels for the scientific stack and fails to build from
  source) get a clear resolver error instead of an obscure compiler failure;
  Python 3.13 is now an official supported/classified version.

## [0.2.2] — 2026-07-06

### Fixed

- **Studio usable on small laptop screens** — the properties panel no longer
  takes ~half the width and can only be hidden: its minimum width is now
  screen-scaled and the main splitter defaults to an 18/58/24 split, so the
  panel starts compact and can be dragged narrow. The default window size is
  clamped to the available screen. Fonts also fit: the base font now shrinks on
  small screens (it never did before), the ~46 hard-coded widget `font-size`
  stylesheets scale with it, and a "QFont: Point size <= 0" warning is guarded.

### Removed

- **Bundled Windows `.exe` installer** and its release build workflow. It
  masked rather than fixed the real Windows launch failure, which comes from
  installing Python via the **Microsoft Store** (a sandboxed, redirected
  filesystem that breaks native PySide6/Qt DLL loading). The README, the
  installation docs and the Studio's DLL-load error message now document the
  cause and fix (install Python from python.org or conda). The console-less
  `esfex-studio` launcher is unaffected.

## [0.2.1] — 2026-06-19

### Fixed

- **Windows: Studio launch crash from a Qt DLL conflict** — on systems with
  another Qt on PATH (a conda `qt-main`/`pyqt`/`qt6`, common in base Anaconda),
  `esfex studio` failed with `ImportError: DLL load failed while importing
  QtWidgets: the specified procedure could not be found`. PySide6's own bundled
  Qt is now put first on the DLL search path (and the conda dirs are skipped
  entirely when PySide6 is self-contained), so a foreign Qt can no longer shadow
  it. The error message for this case now explains the real cause (Qt conflict /
  wrong environment) instead of the misleading "reinstall PySide6".

## [0.2.0] — 2026-06-15

### Added

- **User-defined optimization constraints** — add custom linear constraints to
  the operational *and* investment models, either declaratively in the config
  (`custom_constraints`) or via plugin Julia overlays, editable from a GUI dialog.
- **French and Portuguese GUI translations** — `fr` and `pt` join English,
  Spanish and Japanese, in exact key parity (placeholders and Qt mnemonics
  preserved); the Preferences language list discovers them automatically.
- **GPU-accelerated demand inference** — XGBoost demand/density prediction runs
  on a CUDA GPU when one is available (auto-detected, large-batch only), with
  CPU fallback and an `ESFEX_XGB_DEVICE` override (~2.6× on realistic batches).

### Changed

- **Grid Builder is responsive on country-scale regions** — the "Building
  network" pipeline and the Step-1 fetch aggregation (polygon clip + dedup) now
  run on worker threads with live per-stage status and per-phase timings, so the
  Studio no longer freezes; the network build was also de-quadratized.
- **Availability profiles** — weather-based capacity factors are now the default
  for wind/solar. Queries are de-duplicated per ~11 km location and fetched
  concurrently with retry/backoff, so cost scales with distinct locations, not
  generator count (a full-Japan build dropped from >30 min to ~1 min). A failed
  weather fetch leaves the unit without a profile rather than fabricating a flat
  value; thermal/hydro keep synthetic profiles.
- Project **status promoted from alpha to beta**.

### Fixed

- Grid Builder simplification: O(n²) dead-end bus pruning made linear.
- "Create new system" dialog widened so its window title is no longer clipped.

## [0.1.13] — 2026-06-14

### Added

- **Imported GeoAssets as workflow domains** — every area workflow (Grid
  Builder, Solar PV, Wind, Rooftop, EV, Demand) can define its study area from
  an imported GeoAsset (Shapefile/GeoJSON/KML/GPKG), dissolved into one boundary,
  instead of only a hand-drawn region. Fetched features are clipped to the exact
  polygon (no bbox contamination); GeoAssets persist self-contained in the
  project YAML/`.esfexp`.
- **Standardized domain definition** — one shared two-column control (draw a
  polygon **or** apply a GeoAsset) across all workflows, with equal-sized
  selector boxes and mutual exclusivity (last action wins).
- **Portable `.esfexp` project bundles** — export/import a complete project
  (config + demand + availability profiles) as a single self-contained file,
  with a progress dialog for load/save/export.

### Changed

- **Consolidated workflow wizards** — Solar PV, Wind, Rooftop and EV collapse
  from 8–9 single-column steps to 4 content-aware steps: related light panels
  sit side by side, wider panels (tables, charts) take full-width rows, and each
  step scrolls vertically only when needed so nothing is squashed or overflows.
- Toolbar: visible **Layer** / **Base Map** captions above their selectors, font
  scaling with the rest of the bar, and +20% headroom on the icon-scaling cap.

### Fixed

- **Wind workflow restored** — reconciled the GUI wind config with the current
  `windrex` API (fat GUI `WindConfig`; the analyzer adapter now builds the slim
  `windrex.WindConfig` it needs), and fixed turbine selection
  (`specific_power`). The Wind assessment runs end-to-end again.
- Grid Builder: `NameError` when applying a GeoAsset as the domain.

## [0.1.7] — 2026-06-10

### Added

- **Faithful OSM import as the only build mode** — the Grid Builder now always
  reconstructs the network from the source topology (substations, lines,
  transformers). The "skip incomplete" / "faithful import" toggles, the
  GridFinder source, and the dead snapping/interconnection parameters are gone;
  Step 2 is simpler and the build no longer emits spurious "isolated
  generation / no demand" warnings.
- **Spatially-explicit demand forecasting** — node demand comes from a trained
  hourly XGBoost density model evaluated per 0.25° grid cell: each cell's
  demand density (driven by SSP population and GDP rasters plus CMIP6
  multi-year climate) is multiplied by the cell area and summed per node, then
  anchored to a national total. This replaces the previous "national total ÷
  node count" proxy and produces realistic spatial and inter-annual variation.
- **Capacitated-transport distribution of demand to buses** — within a node,
  demand is split among load buses by solving a capacitated transportation
  problem (cells → buses, capacity = transformer MVA with a voltage-scaled
  fallback) instead of a Voronoi/nearest assignment. A bus serves a
  distribution territory bounded by its capacity, and demand spills to the next
  substation once the nearest one saturates.

### Changed

- **Grid Builder Step 2 layout** tidied (Target System / Node Placement), and
  the demand step exposes an SSP-scenario selector in place of a fixed GDP
  growth rate.

### Tested

- **End-to-end solvability** of the faithfully-built network (build → solve →
  validate), plus unit coverage for the per-cell density inference and the
  capacitated demand→bus allocation.

## [0.1.6] — 2026-06-08

### Performance

- **"Building network" no longer hangs on country-scale regions** — four
  independent O(n²) hot paths in the Grid Builder build pipeline are now
  linear: bus snapping (over-wide candidate window), disconnected-component
  bridging and equipment chaining (linear nearest-bus scans → projected
  KD-tree), line removal (per-fix list rebuild → batched), and per-edge bridge
  detection in electrical-parameter inference (a BFS per edge → a single
  iterative Tarjan pass). A ~25k-feature import (e.g. Japan) that previously
  hung for 20+ minutes now completes in seconds.

### Added

- **Demand visualizer** — a reusable Plotly demand chart (Grid Builder and node
  panel) with a date x-axis that auto-scales on zoom, a red mean line, and a
  deep "Demand statistics" panel.
- **Complete, functional built networks** — generators are assigned a fuel and a
  technology from a powerplantmatching-style taxonomy (CCGT/OCGT, steam and
  combustion engines, run-of-river/reservoir/pumped hydro, PV, on/offshore
  wind), lines get capacities and impedances from a standard line-type catalog
  (PyPSA-style r/x/c per km with an N-1 derate), and nodes are filled with
  default operating reserves and transmission losses. No more orphan generators
  without a fuel or technology.
- **Per-phase build timing** — the Grid Builder result panel now reports a
  "Timing" breakdown (seconds per build phase).

### Changed

- **Fuel Entry Point and Fuel Source unified** into a single "Fuel Source"
  concept across the model and the Studio GUI.

### Fixed

- **OSM fetch timeout on large regions** — the Grid Builder tiles large Overpass
  queries (e.g. Japan) into sub-requests instead of failing on a single
  monolithic query, and normalizes wrapped longitudes so the WRI/GEM/GridFinder
  layers return data for areas crossing the ±180° meridian.
- **"Naming nodes" hang** — the node-naming step is time-boxed and the
  subsequent rendering no longer freezes the UI after large-region builds.
- **Map zoom-out and world wrapping** — the Grid Builder map is constrained to a
  single copy of the world: it no longer zooms out below 1× world size or pans
  onto wrapped copies of the globe (which produced out-of-range longitudes).
- **OTEC cycle diagrams across NumPy/SciPy versions** — thermodynamic state
  values are coerced to plain floats so the T-s / P-h loop arrays stay
  homogeneous (no ragged-array errors) and `mass_flow` is always a float,
  regardless of the installed NumPy/CoolProp build.
- **"Lines toward a centroid" after a rebuild** — the node-assignment spatial
  index cached on the centroid *count*, so a rebuild with re-clustered
  centroids of the same count reused a stale tree and collapsed the network
  toward the wrong centroids. It is now keyed on centroid content with a
  projected metric and exact-haversine refinement.

## [0.1.5] — 2026-06-06

### Fixed

- **Grid Builder demand forecast not persisted (#7)** — applying the step-3
  demand forecast only stored per-node summary stats and never wrote the hourly
  series to disk nor recorded a CSV path, so the saved config carried empty
  `demand_paths` and the runner had no per-node demand. The forecast is now
  written to per-node CSV files (under a `demand/` folder next to the project)
  and wired into each node, so `demand_paths` is emitted and the runner finds
  the files.

## [0.1.4] — 2026-06-06

### Added

- **Reservoir hydropower modelling overhaul (#4)** — hydroelectric generators
  are now dispatched against an explicit water-energy budget in both the
  operational dispatch and the capacity-expansion master, instead of being
  treated as firm capacity. Five behaviours, each independently optional and
  fully wired into the Studio GUI (en/es/ja):

    - **Energy budget** — water balance (inflow, turbining, pumping, spillage,
      evaporation) modelled in the master, correcting hydro previously
      over-credited as firm MW.
    - **Minimum environmental flow** — a mandatory ecological release floor.
    - **Seasonal storage** — reservoir level chained chronologically across
      representative periods (TSAM inter-period linking), so water banked in a wet
      season is available in a later dry one.
    - **Hydraulic cascade** — an upstream reservoir's release feeds a downstream
      reservoir, with an optional travel delay.
    - **Head dependence** — a depleted reservoir delivers less peak power, via a
      linear (LP-friendly) level-dependent power limit.

### Fixed

- **Deleting a system left orphan inter-system links (#5)** — links whose
  endpoints referenced a deleted system survived in the project and were
  silently dropped by the runner. Deletion now removes every link touching the
  system.
- **Grid Builder country detection (#6)** — step 3 reverse-geocoded the
  bounding-box centroid via Nominatim, folding territories into their sovereign
  state (Puerto Rico → United States), finding only one country per region
  (Haiti was missed), and returning localized names. Detection is now offline
  and territory-aware: grid nodes are tested against bundled country polygons,
  surfacing every country the region intersects with correct ISO3 codes.

### Documentation

- New reservoir-hydropower formulation page documenting the water balance and
  all five behaviours as LP constraints; reservoir config/GUI fields and the
  constraint catalogue updated.
- README links the companion repositories and uses Harvey-ball icons in the
  feature comparison table.

## [0.1.3] — 2026-06-05

### Fixed

- **Grid Builder demand-forecast crash (#3)** — the forecast step's worker
  threads (country detection, World Bank / ERA5 fetch, ML forecast) updated Qt
  widgets directly, violating Qt's main-thread-only GUI rule and segfaulting
  (`Cannot create children for a parent that is in a different thread`). The
  heavy work still runs off the GUI thread, but every widget update is now
  marshalled to the main thread via a queued signal.

## [0.1.2] — 2026-06-05

### Added

- **Benders decomposition** as an optional master-problem solver
  (`master_problem.solver_method: monolithic | benders`): an investment-only
  master with `θ[y]` recourse variables plus per-representative-day dispatch
  subproblems and optimality cuts — beneficial for very large problems.
  Configurable (`benders_max_iterations`, `benders_tolerance`,
  `benders_lol_penalty_cap`) and selectable from the Studio. Monolithic remains
  the default.
- **OpenSSF Best Practices** badge.

### Fixed

- **Grid Builder bus-distribution step no longer freezes the UI** on
  whole-country footprint sets: classification and nearest-bus assignment run in
  a background thread, with a vectorised classifier, a single centroid pass, and
  `np.bincount` accumulation.

## [0.1.1] — 2026-06-04

### Fixed

- **Grid Builder demand forecast crash** — the per-node forecast read
  `latitude`/`longitude` on grid nodes, but `GuiNode` exposes its position as
  `centroid_lat`/`centroid_lng`.
- **Fuel-entry-point duplication crash** — duplicating a fuel entry point used
  `coordinate.latitude`/`.longitude`, but `GeoPoint` uses `lat`/`lng`.
- **GeoJSON fuel-entry import** — fuel entry points were built with invalid
  `max_import_rate`/`import_cost` keyword arguments; import parameters now pass
  through the `fuel_params` mapping.
- **GeoJSON node import** — nodes are created from `Point` features and snapped
  to the nearest existing node by great-circle (haversine) distance, instead of
  always attaching to the first node.

### Changed

- Native Julia test suite expanded from smoke tests to full unit and
  end-to-end model-solve coverage, reported to Codecov under a `julia` flag.

## [0.1.0] — 2026-06-02

First PyPI release of **ESFEX — Energy System Flexibility**.

Hybrid Python/Julia framework for power-system capacity expansion and
operational dispatch under high renewable penetration: two-stage decomposition,
DC/AC optimal power flow, N-1 security, frequency stability, battery storage,
sector coupling (electrolyzer, primary energy, EV/V2G, rooftop solar),
MGA/SPORES, stochastic programming, Sobol sensitivity, and a GIS-based Studio.
Includes a directory-based plugin system with a GUI **Plugins** menu
(`PluginsDialog`: list, install from ZIP or Git, enable/disable, uninstall).
Includes the unit-commitment load-shed fix, the test/coverage expansion, and
full packaging (CI, Apache-2.0, REUSE-compliant).

[0.1.6]: https://github.com/Net-Zero-Horizon/ESFEX/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/Net-Zero-Horizon/ESFEX/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/Net-Zero-Horizon/ESFEX/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/Net-Zero-Horizon/ESFEX/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/Net-Zero-Horizon/ESFEX/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/Net-Zero-Horizon/ESFEX/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Net-Zero-Horizon/ESFEX/releases/tag/v0.1.0
