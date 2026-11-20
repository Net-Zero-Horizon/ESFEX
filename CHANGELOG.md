# Changelog

All notable changes to **ESFEX** are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Per-release notes are also published on the
[GitHub Releases page](https://github.com/Net-Zero-Horizon/ESFEX/releases).

## [Unreleased]

### Added

- **Translations for the web views.** The map, the single-line diagram,
  the dashboard and the result charts are web pages, and had no way to show
  translated text. `esfex.visualization.web_i18n` gives every page the
  strings of the `web` section of the translation files: scripts call
  `_t("web.<page>.<key>", {params})`, and static HTML marks text with
  `data-i18n="web.<key>"`. A language change reaches pages already open.
  `tests/test_js_i18n_coverage.py` checks that the keys exist and lists the
  texts still written in English in the pages
  (`tests/js_untranslated_baseline.json`, which may only shrink).

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

- **Warning for stochastic multipliers that have nothing to scale.** A
  scenario multiplier other than 1 whose term the configuration lacks used
  to be accepted without a trace: `gen_start_up_cost` without
  `master_problem.use_uc_in_dispatch` (the master models start-ups only
  then), the fuel-chain and electrolyzer keys (`fuel_import_cost`,
  `fuel_transport_cost`, `fuel_storage_invest_cost`,
  `penalty_loss_of_fuel_supply`, `electrolyzer_*`) with primary energy
  off, and `transmission_cost_per_mw_km` when no line or electrical
  inter-system link has a `cost_per_mw_km`. `load_config` now logs a
  warning naming the scenario, the key and the reason (development mode,
  `master_problem.stochastic` on, first system's scenarios: the ones the
  master uses).

- **Battery ramp limits, fixed cost, reserve flag and availability profile.**
  These battery fields were read from the configuration and then ignored
  (and every battery held reserve). They now apply in the operational
  dispatch and in the master problem alike, including the Benders
  subproblems:
  - `ramp_up` / `ramp_down`: how fast charge or discharge power may rise /
    fall per hour, as a fraction of `MaxChargePower` / `MaxDischargePower`
    (× `resolution_hours` per step). 0, the new default, means no limit. A
    rolling window starts from the charge and discharge at the end of the
    previous window's committed part (the first window of each year starts
    free).
  - `fixed_cost`: fixed O&M in $ per MW of `MaxDischargePower` per year
    (it was labelled $/MWh). The master charges it every year on the power in
    service (within the battery's lifetime, or its life-extended MW); each
    dispatch window charges its share of the year, reported in
    `fixed_om_cost`.
  - `reservable`: only reservable batteries hold static and dynamic reserve
    (and count in the preventive N-1 cover). Their headroom now uses the
    degraded, available discharge power; in the master a retired battery
    whose life is not extended no longer counts its nameplate power as
    reserve.
  - `Availability`: the profile multiplies the battery's charge and
    discharge power (and the power it offers as reserve) each hour.

  Battery technologies take the same five fields; the master applies the
  availability and ramp limits to the batteries it builds and charges the
  fixed cost on the power built, and the batteries it hands to the dispatch
  keep them (they were always reservable, with no ramp limit or fixed cost).
  The Studio battery form has a *Provides reserves* checkbox and shows the
  fixed cost in $/MW·yr.

- **Converter minimum power.** `min_power_mva` of AC/DC and frequency
  converters was never read. With unit commitment in the dispatch
  (`unit_commitment` mode or `use_uc_in_dispatch`) a converter is now either
  off or carries at least `min_power_mva` in one direction (an on/off binary
  per direction and step); the energy prices are recovered with those
  binaries fixed. Economic dispatch and the master problem (LP) still ignore
  it.

- **The results file records what the master built units cost to run.**
  `system_configuration/technologies/technology_{i}` now carries the
  technology's `fuel_cost`, `fixed_cost`, `maintenance_cost` and
  `start_up_cost`, and `battery_technologies/battery_technology_{i}` its
  `maintenance_cost` and `fixed_cost` (string-encoded per-node lists, like
  `invest_cost`). The `generation/{name}` dataset of a unit the master built
  (`Investment <technology>`) carries a `rated_power` attribute, its MW in
  service in the year per node; `battery_discharge/{name}` of a built
  battery carries its power in service the same way.

- **NPV-based retirement of existing units in `esfex run`.** A new system
  setting `npv_retirement` (off by default), with
  `npv_retirement_max_iterations` (default 5) and
  `npv_retirement_threshold` (default $0), makes a `development` run retire
  the existing generators and batteries that do not pay for themselves:
  after the master solve, each unit's NPV is computed from the solved plan
  (a generator earns `base_lcoe` on the MWh the master dispatched it on the
  representative days, scaled to the year; a battery earns
  `npv_annual_return_rate` × its power investment cost × its MW in service;
  both pay what the master charges them: fuel or fuel price, maintenance,
  CO2 price, start-ups, fixed O&M, life extension and decommissioning,
  discounted as the objective is). Units below the threshold are retired for
  the whole horizon (no capacity, no life extension, no fixed O&M, their
  `decommissioning_cost` paid once in the first year) and the master is
  solved again, until no new unit falls below it or after the maximum
  rounds; the log lists each round's units and NPVs. It works with the
  monolithic master, Benders, the stochastic master (probability-weighted
  NPV), each annual master of a myopic run (a unit retired one year stays
  retired, without paying its decommissioning again) and before MGA. The
  retired units leave the dispatch, and the results file lists them in a
  new `npv_retirements` group (unit, node, year, NPV, round, MW). The
  Studio shows the three
  settings and `npv_annual_return_rate` under the system's General settings.
  Off, runs are unchanged. The retired generators count in the yearly
  `retirement_gen_{g}` attributes of the results file (and in
  `YearResults.retirements`, the plugins' `post_master_problem` hook and the
  run summary) from their retirement year on, each retired node in full;
  their MW in service (`rated_power` of their `generation/` and
  `battery_discharge/` datasets) is 0 and the dashboard charges them no
  fixed O&M. Batteries have no retirement attribute in the results file;
  their NPV retirements are listed in `npv_retirements`.

- **Clustered unit commitment (`num_units`).** In unit commitment each
  generator at a bus was one on/off unit: a derated fleet whose availability
  was below `min_power` could never be committed, a 2 × 250 MW plant could
  not run one unit, and an isolated 8 MW diesel plant could not follow a
  2 MW night load. Generators now have `num_units` per node (integer ≥ 1,
  default 1). With more than one unit the dispatch commits a whole number of
  them (`gen_status` is an integer 0..N): each unit has `rated_power / N` MW,
  its own minimum output (`min_power` is a fraction of one unit), start-ups
  and minimum up/down times, and the availability is the share of units
  available (output ≤ rated × availability, units online ≤
  ceil(N × availability)). `start_up_cost` stays the cost of starting the
  whole plant, so a unit start costs `start_up_cost / N`. Spinning reserve
  and the N-1 headroom count the capacity of the units online. With
  `num_units: 1` the model is unchanged (same binary formulation, same
  objective). Economic dispatch and the master problem (LP) ignore it. The
  formulation is documented in Operational Dispatch (GEN-6). The Studio
  generator form has a **Units** field, saved and loaded with the project;
  merging generators in the Grid Builder adds their units up.
- **`unit_size` on candidate technologies.** Capacity the master builds
  reaches the dispatch as `ceil(MW at the bus / unit_size)` identical units
  (default 0: one unit, as before). An existing plant that the master keeps
  only in part (a partial life extension) keeps the whole units of its
  original size that fit in that capacity.

- **Investment annuities in the master results.** `MasterProblemResult` has a
  new field `annuity_cost_by_year`, and the master adapter's result dict a
  key `annuity_cost_by_year`: the investment annuities paid in each year
  (undiscounted), which is what the objective charges.
  `total_investment_by_year` is unchanged: the overnight cost of the
  capacity built that year, which is what the annual budget limits. The
  solution summary in the log shows both.

- **`ESFEX_BENDERS_DEBUG_LP`.** Set to `1`, Benders writes the master and
  subproblem LPs that fail in its first five iterations
  (`benders_master_failed_iter<i>.lp`, `benders_sp_failed_y<y>_d<d>_iter<i>.lp`)
  to the run's `<output>/logs/` folder. Every verbose Benders run used to
  write them to hard-coded files in the system temp folder (`/tmp`), where
  they piled up. Without the variable no LP is written.
  `run_benders_decomposition` takes the folder as `debug_lp_dir`.

- **`ESFEX_MASTER_DEBUG_LP`.** Set to `1`, the runner writes the
  perfect-foresight master problem to `<output>/logs/master_problem_debug.lp`
  before solving it (for inspection or the LP-level sensitivity analysis).
  The LP used to be written on every perfect-foresight run, which for a
  large system took GB and minutes before the solve (Cuba: 1.9 GB, about
  13 minutes); without the variable no master LP is written.

- **Myopic yearly costs comparable with perfect foresight.** Each myopic
  year's master charges only the annuities of what that year builds. The
  log now reports each year's cost as its objective plus the annuities it
  pays on the capacity earlier years built and still within its lifetime,
  and the sum over the years at the end
  (`Orchestrator.myopic_year_costs`). Reporting only: the optimisation is
  unchanged. The master result has a new `annuity_streams` field (build
  year, life, annuity per year) behind `annuity_cost_by_year`.

- **Warning for `min_annual_increment` above `max_annual_increment`.** The
  RE floor then rises faster than the renewable share may grow and is
  missed every year (its shortfall paid). The loader logs a warning and the
  Studio validation lists it under *Configuration* (not with a maximum ≥ 1,
  which switches the cap off).

- **Solver guide: large LP master problems.** A new section recommends
  barrier without crossover (Gurobi `Method: 2`, `Crossover: 0`) for large
  multi-year masters, with its trade-offs.

- **Julia version mismatch warning.** At startup ESFEX warns once when its
  Julia project's `Manifest.toml` was resolved with a different Julia minor
  version than the one juliacall runs (for example a Manifest written by the
  `julia` on the `PATH`, 1.12, loaded by juliacall's 1.11). Such a Manifest
  may fail to load or install. The installation troubleshooting guide
  explains how to use one Julia for both (`PYTHON_JULIAPKG_EXE`).

- **Line losses are in the results.** The dispatch's DC loss model loses
  energy on the lines, but the losses appeared only in the runner's DEBUG
  window balance. Each year's HDF5 scenario now has `line_losses`
  `(num_nodes, hours)` in MW (half of each line's loss at each of its two
  end buses, as the bus balances take it, summed over the node's buses),
  and the year's total `transmission_losses` (MWh) is in `summary_results/`,
  `global/summary_results/` and the scenario attributes
  (`YearResults.line_losses` / `transmission_losses`, `extract_solution`'s
  `line_losses` per bus). The energy-flow Sankey shows these losses; for
  older files it still estimates them from the nodes' loss factors or as
  2 % of the flows.

- **One model clock for every hourly series.** A new key,
  `temporal.model_clock`, sets the time base of all hourly series of a case:
  hour 0 is 1 January 00:00 on that clock, a fixed offset from UTC with no
  daylight saving time, the same instant at every node and in every system
  (also across inter-system links). `auto` (the default) is the standard
  offset of the time zone holding the most demand (by node count until
  demand is known); an IANA zone (`America/Havana`) is used at its standard
  offset, and `UTC` or a fixed zone such as `Etc/GMT+5` work too. Node time
  zones come from the node coordinates (new dependency: `timezonefinder`); a
  node at sea gets the offset of its 15° longitude band, with a warning. The
  tools that write series resolve `auto`, convert to the clock and record it:
  the Grid Builder (weather availability and the demand forecast), the
  Solar PV and Wind workflows and the `availability_generator` plugin
  (`--update-config` writes the key; the Studio stores it in the global
  settings, where a new **Model Clock** field shows and edits it). A run
  logs the clock, and the node time zones when there are several. A case
  that does not record its clock (every case written before this release)
  runs as before, with a warning that the time base of its series is
  unknown; existing files are not converted.

- **The master models the critical days.** The representative days (and
  TSAM periods) are chosen to match the year's mean demand and renewable
  energy, so they never include a cloudy, windless day. In the Cuba myopic
  run the hourly dispatch then shed 0.2 to 1.1 % of the yearly demand on
  such days (at night and in the early morning, mostly October to
  December) while every master planned no shortage. Every master
  (monolithic, TSAM, stochastic, MGA and each annual master of a myopic
  run) now also models `master_problem.critical_days` critical days
  (default 3, 0 = none): the days of the horizon with the largest net load,
  the horizon-mean demand over its mean minus the renewable supply over its
  mean, the renewable profiles weighted by the capacity behind each
  (existing renewables plus the candidates' `invest_max_power`). They are
  chosen once, like the representative days, so every planning year models
  the same dates; they skip the representative days and are
  `min_day_separation` days apart. Each stands for one day of the year; the
  representative days (or TSAM clusters) share the rest, reweighted so the
  year keeps their demand and renewable energy. The log lists their dates.
  In the Cuba case they are 20 Mar, 31 May and 20 Oct, the last one in the
  spell where the dispatch shed most. Benders models the representative
  days only (the log says so).

- **Solver options for the master problem only.** The master problems used
  the dispatch's `solver.options`, so a setting meant for the large master
  LP (Gurobi barrier without crossover, for example) also applied to every
  small dispatch window. The new `master_problem.solver_options` (same
  format) is merged over `solver.options` for every master solve:
  monolithic, TSAM, stochastic, MGA, myopic, the NPV-retirement re-solves,
  and the Benders master and subproblems. A key there replaces the same
  option of `solver.options` however it is written (`method: barrier` or
  `Method: 2`); the other options still apply. The dispatch keeps
  `solver.options`. The Studio keeps the key when it opens and saves a
  case (it has no editor for it).

- **Lines and transformers from a bus to itself are reported.** A branch
  whose two ends are the same bus carries no flow, and the model drops it;
  only the Studio validation said so. Loading a configuration now logs one
  warning per system naming those lines and transformers (an
  equipment-to-bus link drawn on one bus is not counted, as in the Studio).
  The Studio still reports them as errors.

- **An annual operating budget for the dispatch,
  `max_annual_operating_cost`.** A new, optional system key ($/yr, off by
  default) caps the money the dispatch pays to operate in a year: fuel,
  maintenance, start-ups, fixed O&M, electrolyzer, converter and line-flow
  costs (with primary energy, the fuel bought and transported); modelling
  penalties, battery degradation and the CO2 price are not spending. The runner
  gives each rolling window what is left of the year's budget in proportion
  to the demand of its committed hours, and carries what those hours spent,
  so a window that spends less leaves more to the next ones. Spending above
  the budget costs 1000 $ per $ (`OPERATING_BUDGET_PENALTY`): above it, a
  unit with a variable cost above VOLL / 1001 is dearer than shedding the
  load, so fuel the budget cannot pay for becomes unserved energy, as in a
  fuel shortage. The excess (money) and its penalty are reported in the cost
  breakdown and the HDF5 file (`operating_budget_excess`,
  `operating_budget_penalty`, per year), and the runner warns for each year
  that goes over the budget. The Studio's system form has a field for it
  (0 = off).

- **`budget_scope: total` for the master's budget.** With the new system key
  `budget_scope` set to `total` (default `investment`), each build year's
  budget row in the master holds the overnight investment plus the money
  that year spends on operation: what the representative days or TSAM
  periods pay, scaled to the year (fuel, maintenance, start-ups, converter
  and line-flow costs, primary-energy fuel and electrolyzers), plus fixed
  O&M, life extension and decommissioning. Modelling penalties (unserved
  energy, reserve, inertia and N-1 shortfalls, curtailment, RE shortfall,
  SOC and spillage, fuel shortfall, slacks), battery degradation and the CO2
  price (a transfer to the state) are not in it. Same slack at 10 $ per $. A build that cuts the year's fuel bill by more than its cost
  then makes room in the budget. Monolithic, TSAM, stochastic (scenarios
  weighted by probability), MGA and myopic masters support it; Benders does
  not, so with `total` the monolithic master is solved instead, with a
  warning. The Studio's system form has a *Budget Scope* choice.


- **Data sources and licences.** A new `DATA_SOURCES.md` at the top of the
  repository, and the documentation page *Reference → Data Sources &
  Licences*, list the third-party data and code shipped with ESFEX, with the
  licence of each and the attribution and citation its terms ask for: the
  demand density model's grids (NEX-GDDP-CMIP6 climate normals, CC0;
  WorldPop 2020 population counts, CC BY 4.0; Natural Earth land area,
  public domain; UN energy balances from UNdata, Statistics Canada table
  25-10-0029-01 and EIA SEDS for the electricity-use context), Studio's
  Natural Earth country outlines, the GPW v4 and gridded-GDP rasters
  downloaded from the ESFEX data release, and the JavaScript libraries
  bundled with Studio (D3, elkjs, Leaflet, Leaflet.draw, plotly.js,
  xterm.js). The README, the documentation home page, the Demand Density
  Model page and the Demand Forecast workflow page link to it.


- **Projects remember their results.** The Studio saves the project's last
  results file (from its last successful run, or the one opened with
  File > Load Results…) as the top-level key `_last_results`, relative to the
  configuration's folder when the file lies under it. Only the Studio reads
  it; `esfex run` ignores it without reporting an unknown key. Opening a
  project whose results file still exists enables **Results** and
  **Sensitivity** and offers, without blocking, to open the results; nothing
  is loaded until you accept, since a results file can be several GB. A
  results file that no longer exists is forgotten without an error.

- **Export Project can include the results.** When the project has a results
  file, Export Project asks whether to include it (off by default; the dialog
  shows the file size). Included results go into the bundle's `results/`
  folder, uncompressed, and Import Project restores them together with the
  remembered file.

- **ESFEX can be driven from scripts and agents.** `esfex validate`, `esfex run`
  and `esfex info` take `--json`, and the new `esfex summary` command reads a
  results file (`--json` too). A `--json` command prints exactly one JSON
  object on stdout (everything else, Julia's and the solver's output
  included, goes to stderr) with `"schema": "esfex.<command>/1"`, `"ok"` and
  `"errors"`, each error with a `code`, the YAML `key` it is about, a
  `message` and a `fix` hint. Exit codes: 0 success, 1 invalid case or failed
  run, 2 usage error. The same functions are in the new `esfex.api` package
  (`validate_case`, `summary`, `system_info`, `collect_warnings`,
  `read_warnings`, `read_master_diagnostics`), plain Python that returns
  JSON-ready data, imports no Qt and does not start Julia. New guide page
  "Using ESFEX from Scripts and Agents".

- **`esfex summary RESULTS.h5`.** Per year: the cost and its components, with
  a check that they add up to the total; the RE share against the year's
  target; load shed (energy, hours, peak); curtailment (energy, share of the
  renewable energy available); emissions; what the master built (MW, MWh by
  technology) and retired; capacity in service by fuel; storage; transmission
  losses and the loss-repair bounds when the file records them. Then the
  run's warnings and the master's binding constraints and paid slacks
  (`/diagnostics`). `--year` and `--section` narrow it. It reads attributes
  and two small datasets per year, never the generation time series, so it
  is quick on multi-GB files; files written by older versions are read too.

- **End-of-run warnings summary.** A run (`esfex run`, the Studio, or
  `Orchestrator.run()` from Python) now collects the warnings and errors it
  logs, the Julia models' `@warn` lines included, groups them by kind
  ("Generator 'pv' has 3 hours ..." and "Generator 'wind' has 7 hours ..."
  are one kind) and ends with a summary such as "9 warnings in 7 kinds: 3×
  ..., 1× ...". The default `basic` console hid warnings entirely, so a run
  could finish "successfully" with problems nobody saw. The groups are
  written to the new `/diagnostics/warnings` group of the results file (also
  when the run fails) and kept in `Orchestrator.warnings_summary`.

- **`esfex validate` names the YAML key of each error and checks the data
  files.** A loader error now says where it is
  (`systems.Cuba.generators.pv.rated_power`) instead of only the field name
  (`'min_power'`); `ConfigLoadError.issues` lists every problem with its key
  path. `validate` (and `run --dry-run`) also checks that the demand files and
  every `availability_file` exist, resolved as the run resolves them; a
  missing one used to show up only once the run reached it.

- **`esfex info --json`.** Versions, Julia and sysimage, every solver with
  `installed` and `licensed` (Gurobi and Xpress: whether a licence file is
  where the solver looks for one; the file is never read), data assets and
  paths, in a fraction of a second: Julia is started only with `--probe`.
  `esfex info --no-probe` gives the same quick view as text.

- **Results files record what wrote them.** New root attributes
  `esfex_version`, `solver` and `config_file`.

- **Why did the master build this? Binding constraints, duals and paid
  slacks are recorded per master year.** After each master solve (each year
  of a myopic run, the single solve of perfect foresight) ESFEX now groups
  the model's rows by constraint family (RE target, RE growth cap,
  curtailment cap, annual budget, capacity adequacy, CO2 budget, build
  limits, hourly capacity, reserves, storage…, with the constraint catalog's
  codes) and reports which of them bind, their largest and summed dual (the
  cost of tightening the constraint by one unit, in $ of that year), the
  rows with the largest duals by name, and every slack or penalty variable
  the plan pays for (RE shortfall, budget overrun, adequacy shortfall,
  curtailment above the cap, CO2 violation, and the representative days'
  load shed and reserve shortfalls scaled to the year) with its amount,
  price and cost. A few INFO lines sum it up in the log, e.g.
  `Master 2025 binding: Annual budget (investment) (10 $/$, at its penalty)`
  and `Master 2025 paid slacks: Budget overrun $11,000,000 at 10 $/$
  ($110,000,000)`, and the full record is written to the results file under
  `/diagnostics/master/year_{YYYY}/{binding,top,slacks}` (perfect foresight:
  rows without a year in `all`), readable with
  `esfex.io.master_diagnostics.read_master_diagnostics`. Until now the only
  way to see why the plan looked as it did was to rerun with the master LP
  exported. A dual of a row with its own slack is measured per unit of that
  slack, so an unattainable RE target shows exactly its
  `FRE_penetration_loss` and the curtailment cap its excess price per MWh. Not
  recorded for Benders or MGA/SPORES runs; a master without duals (integer
  variables) reports its tight rows only. New Julia functions
  `master_diagnostics` and `master_paid_slacks`, adapter method
  `MasterProblemAdapter.diagnostics()`. Documented in the master-problem
  guide ("Why did the master build this?"), the HDF5 output schema and the
  constraint catalog ("Families in the binding report").

- **An interrupted myopic run can be resumed.** A myopic `development`
  run (`master_problem.planning_mode: myopic`) of a large system takes
  hours, and a crash, a kill, a lost solver licence or a reboot at year 18
  used to lose all of it. The run now keeps a checkpoint next to its results
  file (`<results>.checkpoint.json`), rewritten in one step after every
  master year it plans and every year it dispatches: the capacity each year
  built per technology and build year, the retirements and NPV-based
  retirements, the fuel and electrolyzer capacity, the RE shares, annuities
  and yearly costs, the master plan once every year is planned, the fuel
  tank levels carried into the next year, the warnings so far, and a
  fingerprint of the case (a hash of the configuration and the size and
  SHA-256 of every input file it names). `esfex run -c CASE --resume
  [RESULTS.h5]` (default: the newest results file of the case in the output
  directory) drops what the interrupted run wrote of the year it did not
  finish, restores the state and continues from the first year not
  completed, appending to the same results file; the result is the one the
  uninterrupted run gives. It refuses, with exit code 1 and a structured
  error (`--json`: `status` `resume_refused`, `errors[].code`), a file
  without a checkpoint, a run that completed, a perfect-foresight or
  operations run, other `--years` / `--start-year`, and a case whose
  configuration (except `logging` and the solver's `verbose`, `threads`
  and `time_limit`), input files or ESFEX version changed;
  `--force-resume` lets those last differences through with a warning each.
  `--resume --dry-run` only reports whether and where the run would resume.
  `run --json` reports `resumed_from` (the first year solved) and `resume`.
  From Python: `Orchestrator.run(resume_from=..., force_resume=...)` and
  `esfex.io.checkpoint`. The results file records `planning_mode` and the
  checkpoint's name in its root attributes. The Studio has no resume button
  yet.

- **Bounds of the line-loss repair, in the log and the HDF5.** The first
  solve, with lines free to lose more than the PWL loss of their flow, is a
  relaxation of the model with exact PWL losses: its objective is a lower
  bound, and the repaired solve an upper bound. Each master solve and each
  dispatch year now logs both and their gap (INFO above 0.1 %, DEBUG
  otherwise; the dispatch sums its windows and gives the largest window
  gap), and the HDF5 scenario attributes hold them:
  `master_loss_repair_{lower_bound, objective, gap, rounds, linearised,
  released}` (of the master solve that planned the year; not under MGA or
  Benders) and `dispatch_loss_repair_{…, gap_max, windows,
  windows_repaired}`. Julia: `loss_repair_bounds(model)`. A large gap
  measures how much energy the lines discarded in the first solve, not the
  distance of the plan from the optimum (Cuba's master: 1.53 G$ against
  5.77 G$).

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
  periodic shortfall are gone (so are `penalties.coupling_slack_penalty`
  and the `pe_coupling_slack_cost` result, see "Penalties that priced nothing
  were removed" under Changed), and
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

- The unused Grid Builder step class `GridMappingConnectStep` (the old
  "Step 5: Simplify & Aggregate") and its translation keys were removed.
  It was not reachable from the Studio.

- `BatteryConfig` accepts both `availability_file` and `Availability`
  (the docs said only the alias), and generators accept both names too
  (the docs said `availability_file` was ignored for generators).

- Lint: `ReductionMap` is imported for type checking in `bridge/adapters.py`
  (it was an undefined name in an annotation); the duplicate
  `_haversine_km` and unused imports in the Grid Builder steps, the
  adapters and the Studio serializer were removed.

- **Benders solves the monolithic problem exactly.** With
  `master_problem.solver_method: benders`, several differences from the
  monolithic master remained, so Benders could stop at a different plan or
  not converge:
  - **Converter fixed cost.** The Benders master rebuilt the yearly cost of
    existing assets by hand and left out the fixed cost (per MW and year) of
    converter capacity built, so it built converters the monolithic master
    finds too dear. It now uses the monolithic master's yearly asset cost.
  - **Benders-only investment rules removed.** The Benders master required
    renewable capacity to cover the RE target from average availability and
    battery energy to cover an estimated daily renewable surplus. The
    monolithic master has neither rule, so Benders built renewables and
    batteries it did not need (or found no plan at all). The RE ratio is
    already priced through the subproblems, and the cuts carry the value of
    every capacity.
  - **Curtailment limit is hard.** `max_curtailment_ratio` is a hard limit
    in the monolithic master but was soft under Benders (priced at
    `curtailment_excess_penalty`), so Benders overbuilt cheap renewables.
    It is now hard in the subproblems too. A plan whose renewable output a
    day cannot use within the limit gets a feasibility cut, and only plans
    that meet the limit count towards the upper bound.
    `penalties.curtailment_excess_penalty` is therefore removed.
  - **One cut per representative day.** Benders added one aggregated cut
    per year, so with a binding CO2 budget shared out among the days it
    stopped at `benders_max_iterations` above the optimum (0.16 % with 6
    days, 0.39 % with 12). Each representative day now has its own recourse
    variable and cut, and the same cases converge in 7 and 13 iterations.
  - **Warm-start cuts at the real capacity.** The warm-start cuts ran
    existing generators at their rated power, ignoring degradation and
    retirement, so a cut could bound the operating cost from above and the
    lower bound passed the upper bound (no convergence). They now use the
    capacity the subproblems give each unit.
  - **The result is one iteration's plan.** When Benders stopped at
    `benders_max_iterations`, it returned the investments of one more master
    solve together with the operating cost of the best iteration, so the
    reported objective was not what the reported plan costs. It now returns
    the best iteration's plan with that iteration's costs, also when it
    converges.

- **Benders falls back to the monolithic master for TSAM and user
  constraints.** The Benders master models neither TSAM periods nor
  investment-target custom constraints; it used representative days instead
  of `use_tsam` and dropped the user constraints without a word. With
  either, the run now solves the monolithic master and logs a warning that
  says why.

- **`master_problem.benders_lol_penalty_cap`.** Benders capped the
  loss-of-load penalty in its subproblems (default 1000 $/MW per step), so
  its objective and plan differed from the monolithic master whenever load
  was shed. The subproblems now charge the full `loss_of_load` penalty,
  like the monolithic master. A configuration that still sets the key gets
  the usual unknown-key warning.

- **Penalties that priced nothing were removed.** `penalties.curtailment`,
  `curtailment_excess_penalty`, `re_excess_penalty`, `coupling_slack_penalty`,
  `transport_congestion` and `storage_violation` loaded but had no term in any
  model (curtailed RE is priced by `curtailment_cost`, `max_curtailment_ratio`
  is a hard limit, and the fuel model has no congestion, storage-violation or
  coupling slacks). They are gone from the schema, the Julia inputs
  (`MasterProblemInput.re_excess_penalty`,
  `PrimaryEnergyInput.coupling_slack_penalty`, one positional argument
  fewer), the Studio's system form and the saved files, along with the
  `coupling_slack_penalty` deprecation warning and the always-zero
  `pe_coupling_slack_cost` entry of the cost breakdown (HDF5
  `cost_breakdown`). A configuration that still sets one of them gets the
  usual unknown-key warning; Studio projects saved earlier still open and are
  saved without them.

- **Unused SCOPF settings were removed.** `n1_security.scopf_max_iterations`
  and `scopf_violation_tolerance` were saved but never used (the SCOPF
  constraints are built in one pass). They are gone from the schema, the
  Julia `PowerSystemInput` and the Studio's Global Settings, together with
  their load-time warning; a configuration that still sets them gets the
  usual unknown-key warning. The Studio's checkbox is now labelled "Use
  SCOPF" (it was "Use Iterative SCOPF").

- **Battery fields that had no effect are removed.** `min_power`, `min_up`,
  `min_down`, `eff_at_rated`, `eff_at_min`, `start_up_cost` and `fuel_cost`
  are no longer battery fields: the loader no longer requires them (nor
  `ramp_up`, `ramp_down`, `fixed_cost`, now optional), the Studio form no
  longer shows them and saves projects without them. A file that still has
  them loads, with an unknown-key warning for each.

- **Settings that had no effect were removed.** They loaded (some were even
  required) but no model, solver or report read them:
  - `solver.scale_constraints`, with its Studio checkbox in Global Settings
    and in **File > Preferences**;
  - `dc_power_flow.base_impedance`, its flat `DC_BASE_IMPEDANCE` spelling and
    its load-time warning, and `systems_links[].base_impedance` (per-unit
    values use `ac_power_flow.base_mva` and V²/`base_mva` per line);
  - the system keys `loss_demand_threshold` / `LOSS_DEMAND_TRHESHOLD` and
    `flexible_demand_benefit_ratio`, and the always-zero
    `flexible_demand_benefit` term of the dispatch objective and of the cost
    breakdown (`CostBreakdown`, HDF5 `cost_breakdown`);
  - `meta_network.dynamic_transfer_pricing` and
    `meta_network.inter_system_loss_segments`;
  - `rooftop_solar_config.cost_per_kw`, `cost_reduction_rate` and
    `o_and_m_cost` (rooftop solar is behind the meter, with no investment to
    price), with their Studio fields;
  - `primary_energy_sources.*.storage_investment_cost`,
    `max_storage_investment_per_node` and `max_transport_investment_per_arc`,
    which the loader required (the master takes fuel storage and route
    investment from `fuel_infrastructure`);
  - `price_sensitivity` of the `electric_demand` sectors and of
    `non_electric_demand` (the Studio's node form loses the Price Sens.
    column);
  - the electrolyzer fields `technology`, `min_power`, `initial_age` and
    `degradation_rate` (the loader required the last three), with their
    Studio fields;
  - `risk.hazard_data_sources`, `risk.fragility_curves` and
    `risk.risk_criteria`; the Workbench classifies nodes with the fixed
    thresholds documented under *Risk classification*.

  A configuration that still sets one of them gets the usual unknown-key
  warning; Studio projects saved earlier still open and are saved without
  them.

- **Dead code removed.** The Julia multi-system master, which Python never
  built (`MultiSystemMasterInput`, `create_multi_system_master_problem`,
  `add_inter_system_constraints!`, `build_multi_system_objective!`,
  `InterSystemLink`, `SystemConfig`, `ExtendedMasterVariables`,
  `ExtendedMasterResult`), with the Python converters
  `convert_inter_system_link` / `convert_system_config`; the helper
  `esfex.models.integrate_rooftop_solar`, which no run called; and the unused
  `PrimaryEnergyInput` fields `investment_from_master`,
  `h2_production_hourly` and `electrolyzer_config` (three positional
  arguments fewer; the 20- and 17-argument short forms replace the 23- and
  20-argument ones).

- **`fixed_cost` of generators and candidate technologies is fixed O&M in
  $/MW·yr.** It was added to the per-MWh operating cost (dispatch objective,
  master representative days, Benders subproblems, the cost-ceiling
  constraint), so a unit that did not run paid no fixed O&M and the value
  acted as a variable cost. It is now charged, like the batteries'
  `fixed_cost`, on the capacity in service whether or not the unit runs:
  each dispatch window pays its share of the year (degraded rated power, 0
  past `life_time`, plus capacity a delayed retirement keeps), and the master
  and the Benders master charge it once per year, discounted like the other
  annual costs, on the same MW the dispatch then receives: existing units
  within their lifetime at their degraded power, life-extended MW, and the
  technology MW built, degraded and not yet retired. The master's yearly
  fixed O&M is therefore 8760 / window hours times a window's. Fixed O&M no
  longer changes the merit order. **Existing configurations must convert
  their values** (a former 5 $/MWh at a 50 % capacity factor is about
  22 000 $/MW·yr); typical values are 15 000–45 000 $/MW·yr. The Studio's
  generator form shows Fixed ($/MW·yr) with a tooltip, and the documentation
  examples use realistic values.
- **`gen_fixed_cost` scales every fixed O&M.** The stochastic multiplier now
  scales the fixed O&M of generators, technologies, batteries and battery
  technologies (it did not reach batteries) as a per-year cost of each
  scenario, instead of a per-MWh operating cost.
- **The loader's non-fuel O&M adder skips units with their own fixed O&M.**
  The adder (Lazard non-fuel LCOE, 20–60 $/MWh added to `maintenance_cost`)
  includes fixed O&M, so a generator with `fixed_cost` > 0 paid it twice. The
  adder stays a per-MWh cost, but is no longer applied to a unit with a
  `fixed_cost`, as it already was not to one with a substantial
  `maintenance_cost`; the log message says so.

- **Settings with no effect removed.** `primary_energy_sources.<fuel>.
  transport_cost` and `transport_losses` (route costs and losses come from
  `fuel_transport_routes`) and `non_electric_demand.<name>.is_flexible`,
  `flexibility_ratio`, `criticality` and `delay_tolerance` (the
  primary-energy model takes non-electric demand as a fixed annual demand)
  are gone; old files load with the unknown-key warning. The Studio's
  non-electric demand table keeps only Unit and Demand. The unused Julia
  function `recover_uc_duals!` (it read the commitment values after
  changing the model, so it could not work; runs use
  `recover_uc_duals_via_copy`) is removed.
- **`systems_links.loss_factor` is optional.** Only fuel-route links use it
  (route losses per 100 km, a missing entry is 0); a transmission link no
  longer has to carry it.

- **The dispatch's own investment code is gone.** A `development` run
  dispatches each year as `economic_dispatch` (or `unit_commitment` with
  `use_uc_in_dispatch`) on the master's builds, so the dispatch's
  development-mode investment (generator, battery power and energy,
  reservoir and corridor investment variables, their limits and costs, the
  per-node investment limits) could never run. It is removed, together with
  what only it read or wrote: the generator field `invest_max_power`, the
  battery fields `invest_cost_energy` and `invest_max_capacity` (new storage
  energy is built from `battery_technologies`), the result fields
  `gen_investment`, `bat_investment_power`, `bat_investment_capacity`,
  `transfer_investment` and `reservoir_invest_capacity`, the cost-breakdown
  entries `investment_cost` and `reservoir_invest_cost` of the dispatch, and
  the HDF5 groups `gen_investment_power/`, `bat_investment_power/`,
  `bat_investment_capacity/`, `reservoir_invest_capacity/` and the
  `transfer_investment` dataset of each year (they were always empty). A
  YAML file that still has these keys loads with the generic unknown-key
  warning. A battery's `invest_max_power` stays: it places the battery at a
  node in the master's representative days. In the Studio's Investment
  Portfolio a Generator entry now has only an Invest Cost column (it prices
  the unit's life extension), and a Battery entry no longer has the Energy
  Cost and Energy Max columns. A generator with only an investment limit at
  a node no longer gets a Studio instance there.

- **Electrolyzer `fixed_cost` is fixed O&M in $/MW per year.** The
  dispatch and the master charged it per MW and per hour (× the window's
  hours, × the hours of the year in the master), while the Studio labels it
  $/MW/yr, so a yearly figure entered there cost 8760 times too much. It is
  now $/MW·yr like the generators' and batteries' `fixed_cost`: each
  dispatch window pays its share of the year on the rated power (the
  master's builds included), and the master and Benders charge it once per
  year on the rated power plus the built power not yet past its
  `life_time` (× `electrolyzer_fixed_cost` in a stochastic scenario).
  Configurations that gave it per hour must multiply it by 8760.
- **Removed `convert_master_problem_result` and
  `convert_npv_iteration_result`** from `esfex.bridge.converters`. Nothing
  called them, and they read fields `MasterProblemResult` does not have
  (`gen_investment`, `bat_power_investment`, `bat_capacity_investment`), so
  they only worked against test mocks. `MasterProblemAdapter` converts the
  master's result itself.
- **Removed the delayed-retirement mechanism and
  `penalties.delay_retirement_per_mw`.** The dispatch could keep a unit
  scheduled for retirement in service at a penalty, but no run ever
  scheduled one (the `PowerSystemInput` field `pending_retirements` was
  never set from Python), so the penalty priced nothing. The field, the
  delay variables, `delay_retirement_penalty_per_mw` and the always-zero
  `delay_retirement_cost` entry of the cost breakdown (HDF5 and dashboard)
  are gone; an old file that still sets `delay_retirement_per_mw` gets the
  unknown-key warning. Retirement and life extension are decided by the
  master, as before.
- **Removed unused helpers:** `Orchestrator._apply_retirements_to_config`
  (never called) and `esfex.utils.adjust_investment_limits` /
  `adjust_transmission_parameters` (exported but used nowhere).

- **The dispatch's forced replacement was removed.** The operational dispatch
  created a `gen_forced_replacement` / `bat_forced_replacement` variable for
  each unit flagged in `PowerSystemInput.replacement_needed` /
  `bat_replacement_needed` and charged it a decommissioning cost plus 10 % of
  the unit's negative NPV, but no constraint used these variables, so they
  were always 0. Nothing in Python ever flagged a unit or passed an NPV
  either, so `npv_penalty_cost` was always 0. All of it is gone:
  - the `PowerSystemInput` fields `unit_npv`, `replacement_needed`,
    `bat_unit_npv`, `bat_replacement_needed`, `force_replacement_threshold`,
    `decommissioning_cost_gen`, `decommissioning_cost_bat`, `discount_rate`,
    `max_decommission_cost_per_mw` and `max_npv_penalty_per_mw`, and the
    adapter keyword arguments of the same names;
  - the variables, their objective term, the Julia function
    `update_npv_from_results!` and the per-unit
    `calculate_unit_npv(rated_capacity, ...)` method it used (the master's
    `calculate_unit_npv(result, input, ...)` stays);
  - the `npv_penalty_cost` entry of the cost breakdown (`CostBreakdown`, HDF5
    `cost_breakdown`, the dashboard's cost waterfall). Results written
    earlier still open; the old entry was always 0 and is ignored;
  - the system settings `max_npv_penalty_per_mw` / `MAX_NPV_PENALTY_PER_MW`,
    `max_decommission_cost_per_mw` / `MAX_DECOMMISSION_COST_PER_MW` and
    `force_replacement` / `FORCE_REPLACEMENT` (also unused by the master,
    whose `MasterProblemInput` loses the three matching fields), with their
    Studio fields under **Cost Limits**.

  A configuration that still sets one of these settings gets the usual
  unknown-key warning; Studio projects saved earlier still open and are saved
  without them.

- **`base_lcoe` and `npv_annual_return_rate` now take effect** when
  `npv_retirement` is on (they fed only Julia functions no run called). The
  docs no longer call them, or the NPV iteration, "Julia API only".
- **The Julia NPV API works on the solved plan.** `calculate_unit_npv`
  ignored its `result` (fixed capacity factor = mean availability × an
  efficiency field generators do not have, so it raised for any generator),
  and `force_unit_retirements!` did nothing, so `solve_with_npv_iteration`
  retired nothing. `calculate_unit_npv` now reads the units' operation and
  costs kept in `MasterProblemResult.unit_operation`
  (`extract_master_solution(...; unit_operation = true)`,
  `run_benders_decomposition(...; record_unit_operation = true)`);
  `retire_units(input, units)` replaces `force_unit_retirements!` (removed);
  `solve_with_npv_iteration` re-solves with the units retired and takes
  `solver = :benders`. New: `unit_npvs`, `extract_unit_operation`, the
  `UnitOperation` and `RetiredUnit` types and the `MasterProblemInput` field
  `npv_retired_units`.

- **The results file records every unit's capacity in service.** The
  `rated_power` attribute of each `generation/{name}` and
  `battery_discharge/{name}` dataset, written so far only for the units the
  master built, now holds every unit's MW in service per node in that year:
  for an existing unit, its rating after the master's retirements and life
  extension (development runs) or, without a master, degraded with age and 0
  past its `life_time`, as the dispatch charges its fixed O&M. Results files
  written before keep working: the readers fall back to the configured
  `rated_power` (`MaxDischargePower` for a battery) of the units without it.

- **Myopic NPV retirement values a unit over the rest of the horizon.** Each
  annual master of a myopic run valued an existing unit over that one year
  only, so a unit that loses money every year looked a horizon's worth less
  bad than in perfect foresight. Its NPV now covers its remaining in-service
  years inside the planning horizon: the solved year's generation, operating
  cost and fixed O&M held at that level, scaled by the unit's capacity
  degradation (and its fuel cost by the fuel's yearly price growth: the
  primary-energy fuel's `price_growth_rate`, a stochastic scenario's
  `fuel_price_growth`), with no life extension, so it pays its
  decommissioning in the first year past its `life_time`, all discounted
  from the current year. It is an approximation (the later years are not
  dispatched). Perfect-foresight runs are unchanged. Julia:
  `calculate_unit_npv`, `unit_npvs`, `get_units_with_negative_npv` and
  `solve_with_npv_iteration` take `extra_years`; `UnitOperation` keeps each
  unit's fuel cost and its growth.

- **Reserves are held per system, not per node.** The static and dynamic
  reserve requirement of each node (`nodes.reserve_static` /
  `reserve_dynamic`, or `reserve_static_default_ratio` × its demand) used to
  be covered only by the reservable units and batteries at that node, so a
  node with demand and no unit of its own was always short: the Cuba case
  paid 2.6 TWh of reserve shortfall (260 M$) and showed distorted prices
  while the system had GWs of free capacity. The requirements of all the
  nodes of a system are now held in the pooled headroom of every reservable
  unit and battery of that system; in a merged multi-system run each
  original system keeps its own requirement, covered only by its own units
  (development-zone nodes belong to the system of the bus they connect to).
  The dispatch, the master problem and the Benders subproblems use the same
  constraint. The per-node results keep their shape: `reserve_static` /
  `reserve_dynamic` are each node's requirement held somewhere in its system,
  and a system shortfall (`loss_of_reserve_*`) is split over its nodes in
  proportion to their requirement. Every run with reserves can change: less
  reserve shortfall and lower reserve cost wherever some nodes had spare
  capacity and others did not. Constraint names: `reserve_headroom_s{k}_t{t}`
  and `reserve_dynamic_avail_s{k}_t{t}` (system `k`), and
  `reserve_static_held_*` / `reserve_dynamic_held_*` per node replace the
  `*_ub_*` bounds.

- **The RE target applies only to development runs.** `target_re_penetration`
  is a planning target: the dispatch of a `development` run gets that year's
  value on the master's RE path (capped at what the installed capacity can
  deliver), and `economic_dispatch` and `unit_commitment` runs apply no
  RE-penetration constraint. Without a master the dispatch used to fall back
  to the end-of-horizon target, so the first year was held to it (Cuba:
  557 M$ of `fre_penetration_cost` in one year of economic dispatch); a
  development year whose path value was 0 got the final target the same
  way. RE penetration is still computed and reported in every mode.

- **Out-of-range `min_power` and ramps are clamped on load.** A generator's
  `min_power` (a fraction of rated power) outside 0–1 is clamped to 0–1, and
  a negative `ramp_up`/`ramp_down` to 0 (no limit), with one warning per
  unit and field naming the nodes and values changed. They were accepted
  as they were: a `min_power` of 1.6 asked a committed unit for 160 % of its
  rating. The file still loads, so a project holding such values opens in
  the Studio, whose validation now reports `min_power` outside 0–1 and
  negative ramps as errors (it compared `min_power` with the rated power in
  MW, a check that never fired for a fraction).

- **A line's capacity and impedance are those of the whole line.**
  `capacity_mw`, `reactance_pu`, `resistance_pu` and `susceptance_pu` of a
  transmission line now describe all of its `num_circuits` circuits together,
  everywhere: the dispatch (DC and AC power flow, ACOPF, preventive N-1 and
  SCOPF), the master's node links and the Studio. `num_circuits` is
  descriptive; the model no longer multiplies the capacity by it or divides
  the impedances by it. Only a reactance derived from `reactance_per_km`
  (the value of one circuit) is divided over the circuits. A line of two
  300 MW circuits of 0.1 p.u. each is written `capacity_mw: 600`,
  `reactance_pu: 0.05`, `num_circuits: 2`, which is what the Grid Builder
  and the Studio's parallel-line merges already wrote. A hand-written YAML
  that gave the rating of one circuit with `num_circuits` > 1 must now give
  the whole line's.

- **N-1 generation security is per system in a multi-system run.** Like the
  reserves, each original system of a merged run now has its own N-1
  requirement: its own largest unit (or its own demand with
  `generation_reserve_type: percentage`), covered only by its own units' and
  batteries' headroom (`n1_gen_reserve_system_s{k}_t{t}`, likewise for the
  SCOPF and corrective forms). There used to be one requirement over the
  merged network: the largest unit of all systems, covered by the headroom
  of every system. A single-system run is unchanged. The N-1 generation dual
  reported per step is the sum over the systems.

- **A system's nominal frequency follows its buses.** When the Studio saves
  a system whose `frequency_nominal` is a frequency none of its AC buses
  runs at, it writes their most common frequency and moves
  `frequency_nadir_limit` by the same amount (49 Hz on 50 Hz becomes 59 Hz
  on 60 Hz). A Grid Builder network of a 60 Hz region used to keep the
  50 Hz / 49 Hz defaults. A mixed 50/60 Hz system keeps a nominal frequency
  some of its buses run at; `rocof_limit` (Hz/s) is not changed.

- **A new bus takes the frequency of its node.** A bus added in the editor
  or by auto-complete (the LV bus behind an auto transformer) now runs at
  the frequency of the node's other AC buses instead of 50 Hz.

- **`gen_status` and `gen_startup` in the results hold units.** They hold
  the units online and the units started (0/1 for single-unit plants, as
  before), and each dataset records the generator's `num_units` per node.
  At node level `gen_status` is now the sum over the node's buses (it was
  the maximum), so a master-built plant spread over several buses of a node
  reports all its units online. The dashboard and financial-analysis
  fallbacks charge each unit start `start_up_cost / num_units`, and the
  Commitment Heatmap shows the share of a plant's units online, with the
  units in the tooltip.

- **One rule decides which units are committed: variable renewables are
  not, everything else is.** A *variable renewable* is a `Renewable` unit
  whose fuel is sun (`Sun`, `Solar`, `PV`), wind, an ocean resource (`OTEC`,
  `Wave`, `Tidal`) or none (`None`, empty), or water / hydro at a node where
  it has no reservoir. It has no on/off status or start-ups in unit
  commitment, holds no reserve, is left out of the generation N-1 headroom
  and is what curtailment measures. Every other unit is dispatchable,
  renewable or not: biomass, biogas, geothermal, waste and reservoir hydro
  are now committed like thermal units (minimum output, start-up cost,
  minimum up/down times), hold static and dynamic reserve when `reservable`
  (reservoir hydro held none before), count in the N-1 headroom, take part
  in the master's LP start-ups and are PV buses in the AC power flow. A
  candidate technology follows the same rule in the master's reserves and
  curtailment. The renewable-energy share still counts every `Renewable`
  unit. Before, the exemption was keyed on `type == "Renewable"`, so a
  biomass plant with a minimum output and a start-up cost was never
  committed (Cuba: status 0 while it generated) and its unused capacity
  was counted, and priced, as curtailment.

- **Capacity adequacy counts dispatchable units at their availability.** In
  the master problem's capacity adequacy constraint (`reserve_margin`), every
  non-`Renewable` unit and all storage counted at full capacity whatever its
  availability profile, and `Renewable` units that can be dispatched
  (biomass, reservoir hydro) counted at the minimum of their profile, which
  is 0 for a seasonal sugar mill. All units now follow one rule. Variable
  renewables (sun, wind, ocean, run-of-river hydro) keep their capacity
  credit, the minimum of their profile. Every other generator, technology,
  battery and battery technology counts at its mean availability over the
  node's peak period, the year's top 10 % demand steps, so derates, outages
  and seasons in the profile count. A unit without a profile still counts
  fully. The monolithic, Benders and stochastic masters share the
  constraint. Plans whose dispatchable units have availability profiles may
  now build more firm capacity.

- **Large masters no longer stall interior-point solvers.** In the master
  problem, every technology, battery-technology, transmission, reservoir,
  converter and fuel build was written, with its degradation, into each
  hourly constraint of every later year: output and reserve limits,
  curtailment, battery, line, reservoir, converter and fuel limits. On a
  long, many-node horizon these investment columns were dense. The Cuba
  25-year master had about 13,000 of them, and Gurobi's barrier needed
  ~40 GB per factorisation and ~200 s per iteration, so the solve could not
  finish. Each year's built capacity is now one variable, defined once from
  the investments, and the hourly constraints use that variable. The LP and
  its optimum are unchanged: same objective, same investments, same
  results. An investment column now has a few nonzeros whatever the number
  of hours. The Benders master, which has no hourly rows, is unchanged.
  See Capacity Expansion §6.5 (capacity state variables) in the docs.

- **The master pays for investment as annuities.** A MW (or MWh) built used
  to cost its full overnight cost in the build year, with no residual value
  at the end of the horizon. Capacity built in the last years of a run then
  carried its whole cost for a few years of service and was seldom built.
  Each build now pays `CRF(r, L)` × its overnight cost in every year it is in
  service inside the horizon, discounted like that year's other costs (as
  GenX and PyPSA do). Here `CRF(r, L) = r(1+r)^L / ((1+r)^L − 1)`, `r` is the
  discount rate (a stochastic scenario's own), and `L` is the build's
  lifetime: the technology or battery technology `lifetime`, the converter's
  and electrolyzer's `life_time`, the generator's `life_time` for reservoir
  capacity, the fuel infrastructure's storage and transport `lifetime`, and
  40 years for transmission, which has no lifetime field. This applies in
  every master mode (monolithic, TSAM, stochastic, myopic, MGA/SPORES and
  Benders). A plan from the same configuration can change: more capacity
  built late in the horizon, especially renewables and storage. The annual
  budget (`MAX_ANNUAL_SYSTEM_COST`) still limits the overnight cost
  committed in each year.

- **The master's RE target is annual, with one shortfall per system and
  year.** The target used to be held on every representative day, so a
  sunny day's renewable surplus could not make up for a cloudy day. A second,
  yearly ratio constraint had a slack shared by all systems, a fraction
  priced at a generic 1 M$ per 100 % shortfall; it was redundant with the
  daily constraints and never bound. Now each system's renewable energy over
  the year (its representative days or TSAM periods scaled to the year) plus
  the energy missing must cover the target share of its demand. Each MWh
  missing costs
  `penalties.fre_penetration_loss` (the dispatch's penalty, 100 $/MWh by
  default and when it is 0 or less; × `penalty_fre_penetration` in a
  stochastic scenario), the price the daily shortfall already had. The cost
  is added to the year's operating cost and discounted like it. Each system
  of a merged run has its own target and shortfall, and so does each
  stochastic scenario. Benders shares each year's target out over its
  representative days and reaches the same optimum.
  `re_penetration_by_year` / `re_penetration_by_system` now report the
  achieved share (renewable energy over demand).

- **The investment budget and capacity adequacy bind.** Their slacks shared
  the RE target's generic penalty of 1 M$ per unit. Going over the budget
  therefore cost 1 $ per $, so the budget had no effect. Each $ over the
  budget now costs 10 $. Each MW of adequacy shortfall at a node now costs
  `penalties.loss_of_load` × the hours of the node's peak period (the top
  10 % of the year's demand), which is more than any firm capacity.
  Neither slack makes the master infeasible. Both are discounted like the
  year's other costs.

- **Removed `slack_penalty`** from `MasterProblemAdapter` and the Julia
  `MasterProblemInput`, because the master no longer uses it.

- **A myopic run stops when a year's master is not optimal.** The run used
  to log "not optimal" and go on: the later years planned from a fleet
  without that year's builds, and the dispatch ran on the stale plan. It now
  stops with an error that names the year, the solver status and the years
  already planned, and, for an infeasible master, the constraints that
  cannot hold together (when the solver can compute them; HiGHS and Gurobi
  can).
  Like a perfect-foresight master that fails, no dispatch is run.

- `re_penetration_ratio` in the master is now the actual RE share
  (renewable energy used over demand; the probability-weighted energy over
  the weighted demand in a stochastic master), with no upper bound. The
  minimum-increment floor joins the RE target row (`re_target_s*_y*`
  requires the higher of the two); `add_re_increment_constraints!` is
  replaced by `add_re_growth_cap!` and `define_re_share!`, and
  `MasterProblemInput` takes `re_share_anchor` (the share before year 1;
  the myopic runner passes last year's achieved share). A minimum
  increment above the maximum no longer makes the master infeasible.

- **A master problem stopped at a limit with a feasible plan is used.** A
  master that reached `solver.time_limit` (or an iteration, node or other
  limit) was a failure even when the solver held a feasible plan, and the
  run aborted; the dispatch already accepted such a solution. The
  perfect-foresight, myopic, stochastic and NPV-retirement masters now use
  it with a WARNING giving the objective, the solver's bound and the gap,
  and a Benders master iteration stopped that way is evaluated and cut
  (its lower bound is a proven one or the previous iteration's). A limit
  without a feasible plan is still a failure, now with an error that
  suggests raising `solver.time_limit` or using barrier without crossover.

- **Julia log messages reach the ESFEX console and log file.** Under
  juliacall the Julia models' `@info` / `@warn` / `@error` lines went to the
  bare process stderr, outside Python logging: Benders iterations (lower and
  upper bound, gap), warm-start progress and Julia warnings never appeared
  in the run's log file, and a long Benders run showed no progress at all.
  ESFEX now installs a Julia logger (`ESFEX.set_log_callback`) that hands
  every record to the `esfex.julia` Python logger at its level (DEBUG, INFO,
  WARNING, ERROR), with its key = value pairs and honouring `maxlog`. Benders
  progress lines are milestones, so the `basic` console shows them too; other
  Julia INFO lines appear from `verbose` on, and everything is in the log
  file. Debug records are forwarded only from ESFEX's own code, not from Base
  or the packages it loads. Records logged on worker threads are sent with
  the next record of the main thread.

- **The master's representative days are chosen once, for every year.**
  Each planning year used to pick its own peak-demand days, so the weather
  sample changed from year to year and the share the existing renewables
  gave swung with no capacity change (the Cuba myopic run: 49 → 59 → 54 →
  67 %), pushing some years over the RE growth cap. The days are now chosen
  once, on the horizon means, the mean over the planning years of each hour
  of the year (how they are chosen: see the next entry). Every year models
  the same calendar days with its own demand and its own block of
  multi-year profiles; day weights are unchanged. This holds in the
  perfect-foresight, stochastic, MGA and Benders masters and in every
  annual master of a myopic run. The log lists the dates
  ("Representative days, the same for every planning year: …").
  `MasterProblemInput` has a new field `rep_day_offsets` (the days as steps
  within a year; the runner passes them, a master built without them
  chooses them the same way), and `MasterProblemAdapter` the matching
  `rep_day_offsets` argument. With `use_tsam: true` the TSAM periods are
  also clustered once, on the horizon-mean demand and renewable
  availability, and every year (every myopic annual master) uses the same
  periods and weights.

- **The default representative days match the year's demand and renewable
  energy.** The master's days were the peak-demand day plus the
  highest-peak day of each segment of the year, chosen from the demand
  alone. Every day stands for 365 / `representative_days` days, so where hot
  days are also sunny and windy the master saw a year with far more demand,
  sun and wind than the real one. In the Cuba case with 5 days it saw 6 %
  more demand, 14 % more sun and 45 % more wind. It reported 95 % RE in 2040
  where the hourly dispatch of the same plan gave 65 %, and retired thermal
  plants the real year needed (up to 815 GWh/yr of load shed in the
  dispatch). The peak-demand day is still always selected and the year is
  still split into `representative_days` segments with one day each,
  `min_day_separation` apart. The other days are now chosen so that, for the
  system demand and for each distinct availability profile of the renewable
  generators and the renewable candidate technologies, the mean over the
  days matches the annual mean (horizon means, as before; least squares of
  the relative errors). Cuba with 5 days: demand × 1.002, PV × 1.000, wind
  × 1.007, hydro × 1.002. The days stay the same for every planning year,
  day weights are unchanged, and the monolithic, stochastic, MGA, Benders
  and myopic masters all use them. The log now lists, for the demand and
  each renewable profile, the mean on the days over the annual mean
  ("Representative days mean / annual mean: demand 1.002, …"), and warns
  when one is off by more than 10 % (with few days a strongly seasonal
  profile cannot be matched: Cuba's sugar-mill biomass, zero for 8 months,
  is on 2 of the 5 days, × 1.22). A master built in Julia without
  `rep_day_offsets` chooses its days the same way from its renewable
  generators' and technologies' availability. New Julia functions
  `select_matching_days`, `representative_day_ratios` (and
  `representative_day_offsets` takes `profiles`); new bridge function
  `master_rep_day_ratios` (and `master_rep_day_offsets` takes `profiles`).
  `select_representative_days` (the old peak-based rule) is kept but no
  longer used by default.

- **TSAM also clusters the candidate technologies' availability.** With
  `use_tsam: true` the periods were clustered on the demand and the
  existing renewable generators' availability only. The renewable candidate
  technologies' profiles, which are what the master builds, are now
  included, and units that share a profile count it once. The log lists the
  weighted mean of each profile on the periods over its annual mean.

- **One RE-share definition everywhere: renewable over total generation.**
  The RE share (RE penetration) is now the renewable generation over the
  total generation, energy-weighted over the year, in the RE target and its
  bounds, every master (perfect foresight, stochastic, MGA, Benders, each
  year of a myopic run), the myopic anchors and growth caps, the dispatch's
  RE target and results, the run log, the HDF5 `renewable_penetration` and
  the dashboard. "Renewable" is one rule: a generator or technology of type
  `Renewable`; battery discharge is neither renewable nor generation. The
  share never passes 100 %, and 100 % means no fossil generation. Before,
  the master measured renewable energy used over demand (a Cuba run
  reported 102–105 % while fossil units still ran, and load shed counted
  against the share), the dispatch target asked for renewable energy ≥
  target × demand, and the year's dispatch share was the plain mean of its
  rolling windows' shares, however much each window generated.
  - The target is linear: renewable generation + shortfall ≥ target × total
    generation. The growth cap (`max_annual_increment`) is exact in year 1
    and in every myopic year (renewable ≤ (last share + increment) × total
    generation); from the second year of a multi-year master on it takes
    generation to grow like demand from one year to the next, which keeps
    the model linear (exact when losses and load shed are the same share of
    demand in both years).
  - The master variable that links one year's renewable generation to the
    next year's cap is renamed `re_energy_ratio` (renewable generation over
    demand; it was `re_penetration_ratio`, documented as the share), the
    Benders copy key `(:re_energy, s, y)`.
  - The initial share estimate counts the renewables' scheduled outages.
  - Each `generation/` dataset of the HDF5 now carries `is_renewable`. The
    dashboard computes its RE share from it (older files fall back to the
    technology labels, which no longer count nuclear as renewable) and no
    longer counts battery discharge or load shed as generation in the unit
    commitment view; the results panel's renewable flags read the stored
    generator type (they were always false).

- **The curtailment limit is annual, per system.** `max_curtailment_ratio`
  held on every representative day of the master, over all systems
  together: a year far below its limit was infeasible when one of its days
  curtailed more (Cuba, Benders: a year curtailing 226.7 GWh against a
  580.8 GWh limit was rejected because one of its five days curtailed more
  than 5 %, and the upper bound stopped improving for four iterations). Now
  each system's curtailment over the year, its representative days or TSAM
  periods weighted to the year as their cost is, is at most
  `max_curtailment_ratio` × its renewable generation over the year
  (`max_curtailment_s{σ}_y{y}`); single days may curtail more. This holds in
  every master: monolithic (one or many years), TSAM, stochastic (each
  scenario its own), MGA and the myopic years, and in Benders (the year
  subproblems, the master's operating bound, the feasibility cuts and the
  log's "Years: n of N within the curtailment limit", whose report now
  reads "breaks the annual curtailment limit"). Curtailing existing
  renewables down to the RE growth cap stays exempt, now as one annual
  allowance per system. The dispatch is unchanged: it prices curtailment at
  `curtailment_cost`.

- **`esfex train-demand-model` matches what training does.** Its
  `--cache-dir`, `--countries`, `--year-start` and `--year-end` options were
  passed to the training and silently ignored, and its help described a
  World Bank download that no longer happens. The command now trains on the
  dataset of `esfex build-demand-dataset`: `--cache-dir` is that dataset's
  directory, and the new `--engine` (`xgboost` or `tft`) and `--folds`
  (temporal cross-validation folds) options reach the training;
  `--countries`, `--year-start` and `--year-end` are gone. `--output` is now
  also honoured by the TFT engine, which wrote to its default path whatever
  was given. In Python, `train_demand_model` raises `TypeError` for a
  keyword it does not use instead of ignoring it.

- **The Grid Builder's demand forecast uses the final model of the demand
  article.** The density model shipped so far (`demand_density_hourly_xgb`)
  was the design of the article's first submission: 18 predictors, trained
  on series whose clocks had not been checked. It is replaced by the model
  of the revision (`demand_density_v2`, specification
  `level_geo_two_shallow`), fitted on 9.0 million hours of 166 series in 83
  countries, every series audited onto UTC. It adds the long-run climate of
  the place (mean and 5-95 % spread of its daily mean temperature), how
  electricity is used there (electric share of household energy, share of
  electricity used in buildings; by state in the USA and by province in
  Canada), local solar time and the month shifted by half a year in the
  southern hemisphere, and it is trained in two rounds (a shallow model of
  the level of each place, then the hourly model starting from it). On the
  article's cross-validation it explains hourly demand density in withheld
  countries with R² 0.83 (0.79 for the first design) and in withheld
  regions with R² 0.72 (0.60); its sensitivity to temperature tracks the
  observed one with r 0.86 in withheld countries (0.76). ESFEX builds every
  predictor as the article's training table does; on 1.15 million hours of
  that table its predictions are identical to the article's.
- **The density model works on UTC hours.** Its hour of the day and month
  are UTC, its weekday and holidays those of the local date, and the
  temperature of each hour that instant's: ERA5 is read in UTC, and the
  CMIP6 curve, built on each node's local time, is moved to UTC. Each
  node's forecast is then put on its local civil time, as before, and the
  saved demand on the model clock. Where the zone has daylight saving time,
  the autumn hour that local time repeats reads the hour before it.
- **Density per km² of land, from each cell's population.** The forecast
  sampled the population density of one 30-arc-second pixel at the centre
  of each 0.25° cell, divided the cell's GDP by its whole area and
  multiplied the predicted density by the whole area, sea included. It now
  uses each cell's population (WorldPop 2020, summed per cell) and land
  area, as the model was trained and as the article maps it: population and
  GDP per km² of land, density times land area, and a cell needs people,
  land and GDP to get demand. Population, land, climate normals and
  electricity-use context of every cell ship with the model (about 3 MB);
  the GPW population tiles are no longer downloaded for the forecast.
- **Effect on Cuba** (the 136 nodes of the Caribbean case, base year 2025,
  SSP2, same temperature for both models): with the case's CMIP6 climate
  the yearly demand goes from 21.4 to 21.7 TWh and the peak from 3,249 to
  3,219 MW; with the ERA5 weather year, from 20.9 to 20.5 TWh and from
  3,292 to 3,175 MW. The daily peak moves from 17:00 to 18:00 local time
  (to 21:00 on most days with CMIP6), the evening is above midday (it was
  level), the yearly peak moves from 14:00 to 16:00 (ERA5) or 21:00
  (CMIP6), and the
  mean day is closer to Cuba's observed load of 2015-2019 (correlation 0.89
  against 0.82 with ERA5), whose evening peak is at 18:00. Cuba's demand in
  the Caribbean case was made by the earlier model and is not changed;
  forecast it again to use the new one.

- **The master's annual curtailment limit is priced above the cap.**
  `max_curtailment_ratio` was a hard constraint in every master (monolithic,
  TSAM, stochastic, MGA, myopic). When existing renewables had to curtail
  more than it allows, the master was infeasible; with line losses, the loss
  repair then kept the PWL solution, whose lines lose more than the PWL loss
  of their flows. Curtailment above the limit is now a slack
  (`curtailment_excess_s{σ}_y{y}`, MWh/yr) priced at 10⁴ $/MWh
  (`CURTAILMENT_EXCESS_PENALTY`), an operating cost of the year, and a
  WARNING names each system and year that curtails above its limit, with the
  excess in GWh/yr and its renewable generation. When the limit can be met
  the plan and the objective are unchanged. MGA and SPORES alternatives may
  not curtail above the limit more than the cost-optimal plan does, so the
  cost slack cannot buy curtailment. NPV-based retirement undoes a round
  whose re-solve curtails more above the limit than the plan before (as it
  undid the then infeasible re-solve: retiring the store that held the
  limit). Benders keeps the hard limit.

- **Dispatch windows are solved like the master.** A Gurobi dispatch solve
  that ends `NUMERICAL_ERROR` or `SUBOPTIMAL` (`LOCALLY_SOLVED`) is solved
  once more with the homogeneous barrier (`BarHomogeneous = 1`), each solve
  of the line-loss repair too, and a repair round whose re-solve is still
  not optimal is undone (the round before solved again), as the master does
  since round 37 (`ESFEX.optimize_dispatch!`). The dispatch used to accept a
  suboptimal point silently and repair its losses from it. A window still
  `LOCALLY_SOLVED` after the retry keeps its feasible point with a WARNING
  that names the window, its year and first hour (Ipopt's `LOCALLY_SOLVED`
  on the AC models stays silent); one still `LOCALLY_SOLVED` without a
  feasible point is now a failed window.

- **`max_annual_system_cost` is now `max_annual_investment`, and the
  dispatch no longer uses it.** The key had two meanings: the master's cap on
  each build year's overnight investment, and, prorated to each dispatch
  window by its hours, a soft cap on the window's fuel, maintenance and fixed
  O&M. With its fleet fixed, the dispatch could not meet it and paid a
  penalty (10 × the largest variable cost per $) instead of representing any
  real scarcity, so the budget was counted twice. The key is now
  `max_annual_investment` and caps only the master's investment, as before;
  the dispatch has its own optional operating budget (above). Files with
  `max_annual_system_cost` or `MAX_ANNUAL_SYSTEM_COST` still load: the old
  key is read as `max_annual_investment` (one info line), it is not reported
  as unknown, and the Studio saves it under the new name. A run that relied
  on the old dispatch cap now dispatches without it; set
  `max_annual_operating_cost` for an operating budget.


- **Studio runs write their results into the project's folder.** A run
  started from the Studio wrote to `results` relative to the directory the
  Studio was launched from, so a project opened from another directory (or in
  another session) found no results. Runs now write to `results` next to the
  saved configuration; a project that was never saved writes to
  `~/.cache/esfex/studio_results`, and the status bar says where when the run
  starts. `esfex run` keeps its default (`./results` relative to the current
  directory).

### Fixed

- **One Spanish term per concept.** The same concept had several names
  across the Studio. Now curtailment and spillage are "vertimiento" (it was
  also "curtailment", "derrame" and "vertido"), load shedding and the loss-of-
  load cost are "deslastre de carga" (also "pérdida de carga" and
  "deslastre"), and $B is "miles de M$" everywhere ("mil M$" in two charts).
  The reliability indices keep their standard names (LOLP, LOLH, LOLF, VOLL,
  ENS). Converters are "AC/DC" in every language (Spanish, French and
  Portuguese mixed it with "CA/CC"). The French and Japanese $B labels are
  made uniform too.
  `tests/test_i18n_glossary.py` keeps the glossary.

- **The wind wizard reaches its analysis step again.** Leaving the criteria
  step raised a TypeError, so no wind analysis could start: the step built
  windrex's MCDA settings, whose criteria have no `direction` and which keep
  no LULC scores, while the wizard runs esfex's own wind analyzer. It now
  builds esfex's settings, as the turbine step already did for `WindConfig`.
  The solar PV wizard was not affected.

- **Open views follow a language change.** Some texts built in Python kept
  the old language until the view was rebuilt. Now:
  - the dashboard fetches its data again, so its series and cost components
    change too;
  - the results viewer relabels its chart list and toolbar and redraws its
    charts (a hidden viewer does it when it is shown again);
  - the MGA charts' labels and hover texts are cached per language, not
    once per file;
  - the single-line diagram relabels its loads in place, and the N-1 entries
    and badge are rebuilt for the selected contingency.

  The Grid Builder's node-placement criteria, its clustering progress and
  the availability profiles' weather progress were also still English; they
  are translated now, and the multi-hazard screening has a name in every
  language.

- **Risk workbench texts are current.** The fragility library editor said
  "49 built-in curves from NHESS-2024 and PNNL-33587"; the library has 170
  curves from several more sources. The count and the main sources are now
  read from the library. The two warnings for missing node coordinates sent
  the user to a "Site & Screening" tab that no longer exists; they now say
  that the system's nodes and elements have no location and how to give them
  one.

- **MGA charts show their empty-state messages.** Nine MGA result pages
  (composition, pathway, parallel coordinates, dendrogram, decision factors,
  similarity, projection, spatial, robustness frontier) wrote "no data"-type
  messages from their drawing code and then hid the status line as soon as
  drawing returned, so an empty chart showed nothing. The status line is now
  hidden before drawing.

- **Texts built in Python are translated too.** Labels the Studio's Python
  code builds stayed English in every language. They are now translated
  (about 660 new keys):
  - the result charts' data (month names, cash-flow components, Sankey
    nodes, MGA objectives and axes, unit-commitment buckets, system metrics,
    error messages);
  - the dashboard's series and cost components, the map's risk and results
    legends, the single-line diagram's load labels and N-1 descriptions;
  - the financial charts and the wind and solar analysis progress;
  - the validation messages, with their categories and actions in the
    validation dialog;
  - the Grid Builder's build report, warnings, voltage decisions and
    progress.

  English text is word for word what it was. Messages from core modules
  that the CLI and agents also use (hazard and sensitivity progress) stay
  English. `tr()` now returns English before any language is set, so Studio
  modules used outside the Studio read English, not raw keys. Spanish uses
  "tensión" for voltage throughout.

- **The map, single-line diagram, dashboard and result charts are
  translated.** These web views showed English in every language. Their text
  now comes from the `web` section of the translation files (about 570 new
  keys): map popups, drawing hints and the Leaflet.draw tooltips; the SLD
  legend, tooltips, symbols and results overlays; the dashboard's cards, KPIs
  and year detail; the interruptions editor; and the titles, axes, legends and
  hover texts of the 36 result chart pages. Month names follow the language.
  The map, SLD, dashboard and interruptions editor relabel themselves when
  the language changes; an open result chart takes the new language on its
  next refresh. Labels that the Python side still sends in English inside the
  chart data (cash-flow components, Sankey nodes, error messages) are not
  covered yet.

- **The whole Studio is translated.** About 760 texts outside the Grid
  Builder were written in English in the code, so they stayed English in
  every language: the main window (console, SLD bar, dialogs, status bar),
  the risk workbench, the financial wizard, the OTEC Studio, the wind, solar,
  EV and demand wizards, and the element forms, panels and dialogs (around
  1,340 new keys in each of the five languages). Combo boxes that showed
  internal values (`straight_line`, `onshore`, `circle`, `ac`, `cvar`, ...)
  now show translated names; the model still receives the same values.
  Spanish now says costo, VPN and usted throughout, and Portuguese VAL.
  Charts draw Japanese text: a CJK font installed on the system backs up
  matplotlib's default font (`esfex.visualization.plot_fonts`). A test opens
  the main windows and forms in Spanish and Japanese and fails on any raw
  translation key on screen.

- **The Grid Builder is translated.** About 60 of its texts were written in
  English in the code (the step tabs, the Back/Next/Cancel buttons, the
  filter labels, every table header, the warnings and the status messages),
  so they stayed English in every language. The fetch summary also listed the
  feature types by their internal names (`fuel_entry(s)`). All are now
  translated in the five languages.

- **No raw translation keys in the Studio.** Sixteen texts showed their key
  instead of a translation, in every language: the advanced Solar PV
  wizard's resource and financial results (`wizard_solar_pv.lcoe`, `.npv`,
  `.irr`, `.mean_ghi`, ...), the invalid-line message, the reservoir inflow
  file hint, the multi-electrolyzer header, the results charts' day count and
  the fuel-route action in Preferences. They now have translations in all
  five languages, and a test checks that every key the Studio uses exists and
  that no new untranslated text is added (the texts still hard-coded in
  English are listed in `tests/i18n_untranslated_baseline.json`).

- **The Grid Builder's steps fit the wizard.** Step 1 laid out its four data
  sources in one row and its settings in four columns, and steps 1 and 2 had
  unwrapped intro text, so a step's page was wider than the wizard (step 1:
  834 px in Spanish, step 2: 1245 px in English) and scrolled sideways, with
  the build progress bar running past the window's edge. The sources and
  settings now take two columns and the texts wrap.

- **OSM voltage tags with units are read.** The Grid Builder read only plain
  numbers in the `voltage` tag, so `110 kV`, `13.8kV`, `220/110 kV`,
  `220000/110000` or `2x220000` counted as untagged. It now accepts units
  (applied to a whole list), the `/` separator, a decimal comma before `kV`,
  circuit prefixes and repeated levels, as PyPSA-Eur and PyPSA-Earth do.
  Words such as `medium` stay unknown, for the builder to infer.

- **Clicking a bus in the single-line diagram opens its form.** The SLD
  sent its layout key (`bus_<id>`) instead of the bus id, so the bus form
  opened empty (no system, bus ID, name or parent node), and selecting a bus
  in the element tree never highlighted its bar in the SLD.

- **Grid Builder voltages come from the data, not fixed numbers.** The OSM
  reader filled a missing `voltage` tag from the substation role
  (`transmission` 220 kV, `sub_transmission` 110 kV, `distribution` 33 kV),
  gave an untagged transformer secondary 110 kV and an untagged converter
  220 kV, and the builder set anything still unknown to 220 kV, in every
  country. In Cuba this made Isla de la Juventud, a 34.5 kV system, a 220 kV
  one (its equivalent line rated about 2,000 MW), and stations tagged 132 or
  138 kV, levels no Cuban line carries, put 132 kV buses and lines into the
  220/110 kV grid. Unknown voltages are now inferred from the lines; a bus no
  tagged line reaches gets the medium-voltage level the data tags most (else
  33 kV at 50 Hz, 34.5 kV at 60 Hz), and the voltage floor keeps those not
  connected to any tagged voltage, such as an island's grid. Station tags
  are checked against the lines at 60 kV and above: a station whose own
  lines all carry another single level takes it, and a level no line
  carries is resolved against the standard nominal voltages (IEC 60038,
  ANSI C84.1): the level the station's name states, else a misplaced decimal
  for a distribution station whose corrected level is standard and used in
  the data, else the nearest line level within 30 % ("Taguasco 13.8 kV",
  tagged 138 kV, becomes 13.8 kV; Ciudad Libertad, tagged 138 kV, 110 kV).
  The default medium voltage is the standard level the data tags most.
  Every decision is listed in the build summary and returned as
  `ParseResult.voltage_decisions`.

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

- **`invest_cost_conventional` scales conventional technologies again.** In
  a stochastic master the multiplier for conventional (non-renewable)
  candidate technologies was overwritten by `converter_invest_cost`, so
  those technologies were priced with the converter multiplier and
  `invest_cost_conventional` had no effect. Each multiplier now scales only
  its own builds.

- **Master fuel routes deliver what they ship.** The master's fuel block
  applied each route's transit delay inside its cyclic representative day,
  so shipments that left in the day's last steps never arrived: they were
  paid for and lost, and the master bought fuel shortfall or over-sized the
  route instead. The master now has no transit delay, as documented (with
  one supply period a day the flow leaves evenly, so wrapping the delay
  around the day would deliver the same). The dispatch keeps its delays.

- **Fuel supply limits are right at steps longer than 1 h.** A source's
  annual `max_availability` was prorated to each supply period by its number
  of steps instead of its hours, so with 2 h steps a period got half its
  share (a third with 3 h), in the master and the dispatch. The proration
  now uses steps × step length, like the non-electric demand.

- **The year's investment total counts every build.**
  `total_investment_by_year` of the master result (and its Python
  conversion) left out
  transmission and the master's fuel storage, route and electrolyzer
  builds. They are now included.

- **Inter-system link flow cost per line in the master.** The master charged
  a link's flow cost on every line and transformer between the same pair of
  buses; the dispatch charges it only on the link line. The master now reads
  each line's own `flow_cost`, as the dispatch does (and
  `transmission_cost_per_mw_km` scales it per scenario). The
  `line_flow_cost` field of `MasterProblemInput` is gone.

- **Flat offer curves set the price.** A `flat` `fuel_cost_curve` whose
  `flat_price` differs from the unit's `fuel_cost` (or any single-block
  curve) was ignored, and the unit was priced at `fuel_cost`, for existing
  generators and candidate technologies, in the master and the dispatch.
  Such a curve now prices the output at its own price. A flat curve without
  `flat_price` still costs nothing extra (no segment variables).

- **Reservoir capacity built by the master is used.** The master charged
  `reservoir_invest_cost` for reservoir expansion but its representative
  days never saw the larger reservoir, and the built capacity never reached
  the dispatch or the next myopic year. The master's level band now includes
  the capacity built up to each year, the dispatch gets it as existing
  reservoir capacity from its build year on (perfect foresight and myopic),
  and a myopic year's master counts earlier builds and has that much less
  left to build. Master results report it per year as
  `reservoir_investment_{g}_{n}` (MWh; HDF5 scenario attribute
  `investment_reservoir_investment_{g}_{n}`, per node). Benders subproblems
  see the built capacity too.

- **Benders reaches the monolithic master's plan.** With
  `master_problem.solver_method: benders`, the subproblems left out several
  terms the monolithic master has, and the cuts did not see several master
  decisions, so Benders planned a different system:
  - **Converters** carried no power in the subproblems, so load behind a
    converter was shed, and converter capacity was charged but never built.
    The subproblems now model AC/DC and frequency converters at their rated
    power plus the MW the master builds, with their variable cost, and the
    cuts price converter builds.
  - **CO2 budget.** The subproblems ignored each system's annual CO2 budget.
    The budget couples all representative days of a year, so the Benders
    master now shares it out: one variable per system, year and day, the
    share of the budget the day may use (the shares sum to at most 1). Each
    subproblem holds the day's emissions, scaled to the year, to its share
    plus a violation slack at the system's `co2_budget_violation` penalty.
    Summed over the days this is the monolithic budget with the same
    penalty, so both reach the same optimum; the cuts tell the master which
    days a tonne of allowance is worth most on.
  - **Inter-system link flow cost** (`cost_per_mw_km`) and **LP start-ups**
    (`use_uc_in_dispatch`) were free in the subproblems; they are now
    charged as in the monolithic master.
  - **Curtailment of candidate renewables** was not charged
    `curtailment_cost` in the subproblems, unlike the monolithic master
    (with `max_curtailment_ratio` < 1). It is now, and the cut includes the
    curtailment cost each MW of capacity adds.
  - **Reservoir builds** were charged by the Benders master but the
    subproblems kept the reservoir at its existing size, so Benders never
    built reservoir capacity. The subproblems' level bounds now include the
    MWh the master built, and the cuts price it.
  - **Line builds, life extension and the RE ratio** had no gradient in the
    cuts, so Benders never built transmission or extended a unit's life on
    operating grounds, and the subproblems used the master's RE ratio
    instead of the year's target as the RE floor. Every master quantity a
    subproblem reads is now a copy variable whose dual enters the cut, and
    the subproblems apply both RE floors (target and ratio) like the
    monolithic master.
  - Because every such quantity now has a gradient, a cut is added for every
    year in every iteration; a plan with no investment option used to run to
    `benders_max_iterations` without converging.
  - **Cut coefficients were scaled down twice.** The Benders master divided
    every cut coefficient by 10⁶ on the assumption that costs arrive in $,
    but the bridge already sends them in M$. Gradients per MWh (reservoir,
    fuel tank) or per tonne fell below the solver's 10⁻⁹ coefficient
    tolerance and were dropped, so the master never saw them. The recourse
    variables are now in the input's cost units.

  A master or subproblem that fails to solve is now written as an LP file
  only with verbose output, to the system's temporary folder (it was always
  written to `/tmp`).

- **Studio: a one-node fuel tank outage stays on its node.** The outage
  calendar's windows had no node, so loading and saving a project turned a
  `fuel_storage` window at one node into a window on that fuel's tanks at
  every node. The node is now kept through load, the calendar and save.

- **Studio: OK in the interruptions calendar no longer deletes the
  schedule.** The dialog started from an empty schedule and filled it only
  when the page reported an edit, so opening the calendar and pressing OK
  without editing removed every outage window. It now starts from the
  project's schedule.

- **Builds of years the dispatch skips reach the later years.** With
  `Orchestrator.run(dispatch_years=...)` (or `ESFEX_OPS_ONLY_YEARS`) the
  year loop skipped a year before adding the transmission and converter
  capacity the master built in it; those builds are per-year increments, so
  they were lost for every later dispatched year. They are now applied for
  skipped years too.

- **No false "ignored" warning for tank outages on entry-point fuels.**
  The outage check took the valid `fuel_storage` fuels only from
  `primary_energy_sources`; when the fuels come from `fuel_entry_points`
  (no `primary_energy_sources`), a window on one of them was reported as
  ignored although the fuel model applies it.

- **Rooftop solar keeps rooftex's defaults per system, and multi-node
  systems keep their rooftop solar.** Every multi-node or multi-system run
  goes through the system merge, which padded `rooftop_max_potential`,
  `systems_per_node`, `avg_system_size` and `initial_adoption` with 0 for
  systems (or nodes) that do not set them. Without `rooftop_max_potential`
  the merged list of zeros set every node's potential to 0, so rooftop
  solar disappeared from any multi-node system; a system without
  `systems_per_node` next to one with it got 0 rooftop systems at its
  nodes. Unset entries are now `null` and take rooftex's default at that
  node (initial adoption 0.05; the potential rooftex draws where the
  system/size lists are missing), a short list repeats its last value as
  rooftex does, and the nodes of a system without rooftop solar
  (`SIM_ROOFTOP` off or no `rooftop_solar_config`) get potential 0. The
  per-node lists now accept `null` entries.

- **Myopic electrolyzer builds retire after their life time.** The myopic
  master carried every electrolyzer build into all later years, while
  perfect foresight retires a build after the electrolyzer's `life_time`.
  A myopic year now gets only the builds still in service; its room to
  build is still reduced by everything built so far, as the
  perfect-foresight build limit counts every build.

- **HDF5 names of fuel results.** `primary_energy/fuel_for_power` replaced
  `/` with `_` in unit names, so a unit `A/B` collided with a unit `A_B`
  and its name differed from the unit's other datasets;
  `master_fuel_capacity` wrote raw fuel names (a `/` made nested groups)
  and electrolyzer keys without de-duplication. Both now use the results
  file's unit-name convention (`/` → ` - `, a clash gets ` (1)`), and
  `fuel_for_power/{unit}` uses the unit's name of the per-generator groups.

- **Flat battery discharge curves are priced.** A `discharge_cost_curve`
  that reduces to one segment (a `flat` curve with a `flat_price`) was
  skipped, so the discharge stayed at `throughput_degradation_cost`. It is
  now priced at its own price in the dispatch and in the master, as flat
  generator curves are (a flat curve at the battery's own throughput cost is
  still skipped, since it changes nothing).

- **Battery technology energy in the Benders cuts.** The cut gradient of a
  battery technology's energy now includes the duals of the day's initial
  and cyclic SOC constraints (both proportional to the energy built), not
  only of the SOC limit.

- **Configuration reference: battery `technology`.** It was documented as a
  label with no effect; it names the battery technology whose
  `invest_cost_power` prices life extension where the battery's own
  `invest_cost` is 0.

- **The Risk Workbench opens with the project's saved settings.** It always
  started from the defaults (expected value, CVaR α 0.95 and λ 0.5,
  independent combination, no scenarios), so a second **Apply** overwrote the
  saved `risk.*` values. It now loads the saved risk measure, CVaR α and λ,
  combination method and climate and hazard scenarios, and shows them in its
  controls.

- **Documentation.** The HDF5 schema lists the master's converter investment
  attributes (`investment_converter_investment_acdc_{c}` /
  `_freq_{c}`, MW). `gen_life_extension` / `bat_life_extension` of the master
  result are documented as what they hold, a 0/1 flag per node (1 while the
  unit is within its original `life_time`), not MW. Generator and battery
  `invest_cost` are no longer labelled deprecated: they price life
  extension, and a 0 is filled from the matching technology.

- **Reports value fixed O&M per MW·yr.** The per-hour LCOE now spreads the
  result hours' share of the fixed O&M over the energy produced (it added
  `fixed_cost` as if it were $/MWh); the financial analysis (cost
  reconstruction, per-technology and new-investment O&M) and the dashboard's
  per-system cost breakdown charge it on rated power for the simulated share
  of the year, including idle units, and the dashboard now includes the
  batteries' fixed O&M. The unit NPV helpers (`calculate_unit_npv` of the
  dispatch and the master) charge it per MW·yr too, and the battery NPV of
  the dispatch now includes its fixed cost.

- **Units past their lifetime retire in the dispatch, degraded or not.** The
  dispatch zeroed a generator or battery at the end of its `life_time` only
  when it had a degradation rate, so an undegraded unit the master retired
  ran on at full capacity in `economic_dispatch` and `unit_commitment` runs
  (and was charged fixed O&M on it). It now retires at the end of its
  lifetime as in the master; the reserves, curtailment and N-1 terms use the
  same power in service. In a `development` run the master's decisions stand:
  the capacity the master keeps (life extension included) reaches the
  dispatch with no end of life, and an existing battery past its lifetime
  gets the power and energy the master keeps (it was zeroed with a
  degradation rate, or kept in full without one).
- **Life-extended units are no longer reported as retired.** The master's
  retirement output (`gen_life_extension` / `bat_life_extension`, the HDF5
  `retirement_gen_{g}` attributes, `YearResults.retirements` and the log
  tables) read a flag that was 0 for every unit past its original lifetime,
  extended or not. It is now the share in service: 1 within the lifetime,
  and past it the share of the capacity the master keeps.
- **Multi-system runs: availability and inflow profiles at the right
  nodes.** A unit's availability file (generators, technologies, batteries,
  battery technologies) and reservoir inflow file has one column per node of
  its own system, but it was read as if it covered the merged node set: the
  second system's units got their profile at the first system's nodes and
  their last column repeated at their own. Each system's profile is now
  placed at that system's nodes.
- **CVaR α accepts the whole schema range.** The Risk Workbench's CVaR α box
  took only 0.80–0.99 and Global Settings' 0.01–0.99 in steps of 0.01, while
  `risk.cvar_alpha` may be any value in (0, 1): a saved 0.5 or 0.995 was
  shown and saved back changed. Both now take 0.001–0.999.

- **Studio save of a cost curve on a unit missing at some nodes.** A
  generator's fuel cost curve (or a battery's discharge curve) was written
  with `null` at the nodes where the unit has no instance, and the saved
  file then failed to load. Those nodes now get a flat curve.

- **N-1 security now applies to development runs.** `n1_security.apply_to_modes`
  lists simulation modes and includes `development` by default, but the
  dispatch only received its formulation (`economic_dispatch`, or
  `unit_commitment` with `use_uc_in_dispatch`), so the N-1 constraints (and
  `compute_security_cost`) were never applied in development runs. The
  dispatch now also receives the run's `simulation_mode` (new
  `PowerSystemAdapter` argument `run_mode`, by default the configuration's
  `simulation_mode`) and checks the list against it. A development run with
  N-1 enabled and the default `apply_to_modes` now has N-1 constraints in
  every window.
- **Development windows end with fuel tanks at least 10 % full.** The
  documented end-of-window rule of the primary-energy model (each tank at
  least 10 % full at the end of a development-run window, since there is no
  cyclic storage condition) never ran, because the fuel model also got the
  dispatch formulation instead of `development`. It now gets the run's
  simulation mode, and the rule also covers tanks the master built at nodes
  without a configured tank. Operations runs are unchanged.
- **Dashboard per-system costs use each unit's own costs.** The Studio
  dashboard rebuilt a system's fuel, fixed O&M, maintenance, start-up and
  battery maintenance costs by pairing the result datasets with the unit
  configurations by position, but HDF5 lists the datasets alphabetically and
  the batteries were taken from an unordered set, so with several units a
  unit could be charged another unit's costs. Datasets are now matched to
  their configuration by name (by position only in old files whose
  configurations carry no name).
- **The master reports its operating cost per year.**
  `MasterProblemResult.total_operational_cost_by_year` was always zero
  except with Benders: the monolithic, stochastic (expected value) and
  TSAM masters now fill it with each year's representative-day operating
  cost scaled to the year (undiscounted, in $ on the Python side). The
  master's solution summary in the log printed this zero next to an "NPV
  discrepancy" warning; it now shows the NPV of investment plus operation
  and, separately, the rest of the objective (annual asset costs and slack
  penalties).
- **Docs: the master builds the fuel infrastructure.** The primary-energy
  tutorial and the capacity-expansion formulation still said the master
  does not build fuel storage or transport (or has no fuel model) and that
  the dispatch builds them; since the master fuel block it is the other way
  round. Both are corrected, and the master-problem guide's investment
  table now lists fuel storage, routes and electrolyzers with their real
  keys and limits.

- **Units the master built are costed in the results dashboard, charts and
  financial analysis.** Their datasets have no generator or battery config,
  so the dashboard's per-system cost breakdown gave them no fuel,
  maintenance, start-up or fixed O&M cost, the Fuel Supply and Cash Flow
  charts matched them to another unit by a name suffix (or to none), the
  UC marginal-technology chart and the financial analysis's cost fallback
  and per-technology table paired them with another unit's config by
  position (h5py lists datasets alphabetically), and the investment metrics
  borrowed the costs of an existing unit of the same fuel. One shared lookup
  now resolves an `Investment <technology>` dataset to its technology's
  config, with fixed O&M ($/MW·yr) on the MW in service that year. A
  results file whose configs carry names no longer pairs an unmatched
  dataset with a config by position. Files written before this change have
  no operating costs for these units (the investment metrics still borrow
  them from a same-fuel unit).

- **The dashboard's per-system costs of every system after the first were
  0.** For a subsystem the configs were read with `range(num_generators)`
  over a view that keeps the global `generator_<i>` keys (so the second
  system's configs came back empty), and the arrays, already cut to the
  system's nodes, were cut again (to nothing). The results charts' config
  loaders had the same `range` problem. The per-system breakdown also read
  no start-ups for a subsystem, and priced battery builds with a generator
  technology's `invest_cost` (0) and no energy cost: battery power and
  energy builds now use `invest_cost_power` and `invest_cost_energy`.

- **Battery fixed O&M in the financial analysis.** The cost fallback (files
  without `cost_breakdown`) and the per-technology table now charge
  batteries their `fixed_cost` ($/MW·yr) on the discharge power, as the
  dispatch does; the per-technology table also reads each node's own costs
  (it took the first node's, which in a merged system is another system's
  zero).

- **Electrolyzer water cost label.** The Studio's electrolyzer form (all
  five languages) and its GUI page said $/m³; `water_cost` is charged per kg
  of hydrogen produced, so they now say $/kg H₂.

- **Fixed O&M of existing units on the capacity in service.** The results
  dashboard's per-system costs and the financial analysis's cost
  reconstruction (results files without a cost breakdown) charged the fixed
  O&M of existing units on their configured `rated_power`, also after the
  master retired them at the end of their life or kept only part of them by
  life extension, and without their degradation. They now use the MW in
  service the results file records.

- **System Metrics: installed capacity and reserve margin.** The chart's
  Total Installed Capacity and Reserve Margin summed the configured
  generators' rated power (averaged over the nodes, not summed), so the MW
  the master built and its retirements were missing. They now add up each
  generator's MW in service in the year over the system's nodes.

- **Results charts pair each unit with its own configuration.** The UC
  Hourly Dispatch, Net Load Duration, Ramp Distribution and Storage SOC
  charts and the dashboard's UC generation mix took the configuration of
  the unit in the same position as the dataset. HDF5 lists the datasets
  alphabetically and the units the master built have no configuration of
  their own, so a unit could be drawn with another one's fuel (a solar build
  counted as coal, and missing from the renewable net load). They now find
  each unit's configuration by name, or its technology's for a unit the
  master built. The Storage SOC chart's fallback capacity, for a battery
  without state-of-charge data, reads the battery's `capacity` (it read a
  field that does not exist). The per-unit investment groups
  (`gen_investment_power/`, `bat_investment_power/`) of older results files
  were ignored by the Generation Mix, Cash Flow and System Metrics capex;
  they are now counted, each with its own unit's `invest_cost`.

- **Technology properties of a build on a network with fewer buses than
  nodes.** When the master's build of a candidate technology was laid out
  on the buses for the dispatch, the technology's per-node properties (fuel
  and maintenance costs, efficiencies, ramp limits, ...) were read by
  position whenever the node list was at least as long as the bus list (a
  network with as many or fewer buses than nodes, such as a reduced network
  that dropped the buses of some nodes): a bus got another node's value,
  possibly another system's padding zero. Each bus now gets its own node's
  value.

- **UC charts of the second and later systems were empty.** In a
  multi-system run, the UC Hourly Dispatch, Load Shedding & Curtailment, Net
  Load Duration, Ramp Distribution, Storage SOC, Marginal Technology and LMP
  by Node charts, and the dashboard's UC overview, cut the system's nodes
  out of data that was already this system's alone. Every system but the
  first showed no generation, demand, storage or prices (the LMP chart
  failed). They now read the system's data once. The per-unit on/off status
  of a system is now limited to its own units and nodes like its
  generation.

- **Marginal Technology chart: unit capacity.** A unit counted as running
  below its capacity (and so as a candidate to set the price) against the
  mean of its per-node rating, which in a merged system is diluted by the
  other systems' zero entries. It now uses the unit's MW in service over the
  system's nodes.

- **Financial analysis: capacity payments.** The capacity payment
  (`capacity_payment`, $/MW·yr) was paid on the first node's configured
  `rated_power` of each generator (nothing for a unit at another node), the
  same every year, without the master's builds or retirements. It is now
  paid on each year's generator MW in service over all nodes.

- **Master capacities on a bus-level master with as many buses as
  nodes.** The dispatch read the master's capacities (existing units,
  builds, battery life extension) per node whenever the array was as long
  as the node list. A master solved on the buses of a network that has as
  many buses as nodes, without being one bus per node, had its per-bus
  capacities placed on the wrong nodes. The runner now decides from the
  master's network whether its results are per bus or per node.

- **A failed re-solve no longer stops the run.** When the master re-solve
  after an NPV retirement round was not optimal (retiring the units made it
  infeasible, for instance through the hard curtailment limit), the run
  aborted with "Master problem failed". The round is now undone: the last
  optimal plan and the retirements made up to it are kept, a warning names
  the units of the undone round and the solver status, the iteration stops
  and the run continues, in every master mode (myopic years and MGA
  included); the `npv_retirements` group lists only the retirements applied.
  `solve_with_npv_iteration` does the same and names the units and status
  in its warning.

- **Demand at a node without a load bus is reported as unserved.** A node
  with demand and no `load` or `mixed` bus (no bus at all, or only
  `connection` buses) has its demand in no power balance. It was dropped
  silently: the HDF5 file showed its demand with `loss_load` 0, and the
  summary loss of load left it out (Cuba node 35: 22.4 GWh). That demand now
  appears as load shedding at the node (`loss_load`, `YearResults.load_shed`
  and `load_shed_array`) and in `load_shedding_cost` and the objective at
  `LOSS_OF_LOAD` per MWh. The warning that lists such nodes is logged once
  per run instead of once per window.

- **The window balance in the debug log is an energy balance.** It added
  the static and dynamic reserve to the demand side and left out the
  transmission losses, so it reported gaps of up to 3.5 %. It now compares
  the energy of the committed steps (MWh): generation, battery discharge,
  V2G, load shedding and rooftop against demand, battery charge, EV
  charging, electrolyzers, pumping and transmission losses, and prints the
  losses separately. The dispatch result's `total_losses` now holds the
  losses of the DC loss model (it was always 0).

- **"Dropping solver option" warnings are logged once per run.** An option
  that does not apply to the selected solver or LP method was reported
  again for every rolling window (Cuba, three options: 627 lines for one
  year).

- **Merged generators no longer add up their per-MW parameters.** Every
  Studio merge of generators (equipment aggregation of simplification
  level 1 and 2, the Validate dialog's infrastructure and small-generator
  merges) summed `min_power`, `ramp_up`, `ramp_down` and `inertia`, and some
  of them `decommissioning_cost`: four gas units at `min_power` 0.4 became
  one at 1.6, and ramps of 21 appeared (Cuba). These are fractions of rated
  power, H in seconds and $/MW, so they are now averaged with rated power as
  the weight, like the efficiencies and costs already were; a ramp of 0 (no
  limit) counts as a full rating per hour, the droop is the parallel
  equivalent and min up/down times, governor constant and risk derating are
  averaged too. The start-up cost ($ per start) still adds up. Battery
  merges average ramps, inertia, decommissioning and cycling cost the same
  way; the aggregate battery used to take the defaults or the largest
  unit's values for them.
  Projects saved earlier keep their summed values until the units are
  edited; the loader clamps `min_power` above 1.

- **A Renewable unit burning a priced fuel pays for it.** The loader linked
  `fuel_cost` from the `fuels` block only for non-renewable generators, so a
  `Renewable` biomass unit whose fuel had a price (Cuba: `Biomass`,
  40 $/kTon) ran for free. Any generator whose fuel has a price now gets the
  fuel cost, and the biomass non-fuel O&M adder; Sun, Wind and Water have no
  fuel entry or a price of 0 and stay free, without the "no valid fuel"
  warning.

- **The non-fuel O&M adder recognises the Studio's fuel names.** The adder
  was looked up by the exact fuel key `Gas`, so gas units of a Studio
  project (fuel `Natural_gas`) got none while fuel oil and diesel units got
  theirs, and gas looked 60 $/MWh cheaper than it is. The fuel's key and its
  `name` are now compared ignoring case, spaces, `_` and `-`, with aliases
  (gas: `Natural_gas`, `NG`, `LNG`; fuel oil: `Oil`, `HFO`, `Crude`;
  diesel: `Gasoil`). The values and the rules that skip the adder are
  unchanged; each unit that receives it is still named in the log (INFO).

- **Lines without a reactance get the one of their length, not 0.01 p.u.**
  The Grid Builder measured a line's length only when OSM tagged its
  voltage, so an untagged line whose voltage was inferred from its
  neighbours got a voltage and a rating but no impedance (and the impedance
  of a multi-circuit inferred line was not divided over its circuits). The
  node-level reduction then left such lines out of each node pair's
  parallel combination and gave a pair where no line had a reactance
  0.01 p.u. whatever its length: 124 of the 266 Cuba branches, 9 to 150 km
  long. Lines are now always measured, and every parallel, series and
  equivalent-branch combination derives a missing R, X and B from the
  line's length (else the distance between its end buses) and voltage
  (else the higher of its end buses), with the standard line type of that
  voltage or the line's own `reactance_per_km`, on `ac_power_flow.base_mva`.

- **Multi-circuit lines no longer carry n² times their rating.** The Grid
  Builder writes a line of n circuits with its total rating and its total
  impedance (one circuit's divided by n), but the dispatch then multiplied
  the capacity by `num_circuits` and divided the reactance and resistance by
  it once more: a double-circuit OSM line carried four times its rating at a
  quarter of its reactance, and drew too large a share of the flow. The
  master's adequacy and node links, the Studio's results and the contingency
  tools already read the whole-line values, so the master and the dispatch
  also disagreed on these lines. The network reduction left a merged line's
  circuit count at the sum of its members while its reactance was already
  the parallel equivalent, which divided it again.

- **Built capacity holds reserve in the master.** The capacity the master
  builds from reservable, non-renewable technologies and reservable battery
  technologies did not count towards the reserve headroom of its
  representative days, so a plan whose firm capacity was all new paid the
  reserve shortfall in every day, although the dispatch, where those units
  are existing units, held the reserve. The master (monolithic, TSAM,
  stochastic and Benders, whose cuts now carry the value of that headroom)
  now adds `availability × capacity built − output` for each such
  technology and `availability × power built − discharge + charge` for each
  such battery technology, and their share of the dynamic reserve, as the
  dispatch does for the units they become. An existing generator's headroom
  in the master is now its capacity in service × availability − output, as
  in the dispatch (the availability was left out). The capacity-adequacy
  constraint (`reserve_margin`) already counted the battery power built.

- **The Grid Builder built 60 Hz regions at 50 Hz.** GridFinder lines and
  WRI/GEM plants carry no frequency but were given 50 Hz, so their endpoint
  buses were created at 50 Hz. On a sparsely mapped 60 Hz grid such as Cuba
  they outnumbered the 60 Hz substations, and the frequency-consistency
  step then set the whole network (buses, lines, generators) to 50 Hz.
  Elements without a frequency tag now take the mains frequency of their
  location, and the consistency step also aligns AC lines with their area.

- **Bridge lines had no length or impedance.** The bus-to-bus bridge lines
  `iterative_auto_connect` adds for components without equipment carried
  no length, no R/X/B and 50 Hz. They now get the distance between their
  end buses, the impedance of the standard line type of their voltage on
  the system base (like every other built line), and the frequency of the
  main-network bus they connect to.

- **Start-ups and status of solar, wind and run-of-river units were
  arbitrary.** Unit commitment created `gen_status` and `gen_startup` for
  every generator, but the constraints only bound the committed ones: a PV
  or wind unit's start-up variable was free and cost nothing, so the solver
  left any value there (Cuba UC, 720 h: 25,200 start-ups for the PV fleet,
  720 for each wind and hydro unit, 3,600 for the sugar mills). These units
  now get no status or start-up variables; the results arrays hold 0 for
  them, and the HDF5 file has no `gen_status` / `gen_startup` dataset for a
  generator that is a variable renewable at all its nodes (the dashboard,
  commitment heatmap, dispatch stack and single-line diagram already treat a
  generator without one as online whenever it generates).

- **The master ignored the availability of non-renewable technologies.**
  The monolithic master and the Benders subproblems capped a non-renewable
  technology's output at its built capacity × 1, while the generator it
  becomes in the dispatch is capped by the technology's availability
  profile (derates, scheduled outages), as the reserve headroom already
  was. Every technology's output is now capped by its availability in each
  representative-day step (1 without a profile), so the master no longer
  plans on energy the dispatch does not have.

- **False "extending its life is free" warning.** The life-extension cost
  check tested `invest_cost` at every node, including nodes where the unit
  has no capacity (where it is 0), so a unit priced at every node it
  occupies still warned that its life extension was free when no technology
  shared its fuel (Cuba: every plant). Only the nodes with capacity are
  checked now, for generators and batteries. Costs were not affected.

- **The Grid Builder gave 50 Hz countries 60 Hz.** The mains-frequency
  lookup used wide bounding boxes: the South America box also covered
  Argentina, Uruguay, Paraguay, Bolivia and Chile, the Saudi Arabia box
  Kuwait, Bahrain, Qatar, the UAE, Jordan and Yemen, the Liberia box parts of
  Guinea, Sierra Leone and Côte d'Ivoire, and the North America box western
  Greenland, so their buses were built at 60 Hz (Buenos Aires, Kuwait City).
  The lookup now uses coarse country outlines that follow the borders with
  50 Hz neighbours, with 50 Hz exceptions checked first (Greenland, Jamaica,
  most of the Lesser Antilles, Curaçao and Bonaire, French Guiana, Bahrain).
  Okinawa, Kinmen, Matsu, the Galápagos, southern Chiapas, the US Pacific
  territories and French Polynesia, which fell outside every box, are now
  60 Hz.

- **Grid Builder impedances ignored the system base.** Lines it built, lines
  whose voltage it inferred, island reconnection lines and the transformers
  it created (substation banks, generator step-ups, co-located voltage
  levels, OSM transformers, parameter inference) had their per-unit
  impedance computed on 100 MVA whatever `ac_power_flow.base_mva` was. They
  now use the system base, like the validation's line impedances.

- **Bridge lines joined two voltage levels with no transformer.** The
  bus-to-bus bridge that `iterative_auto_connect` adds for an isolated
  component without equipment ran at the higher of the two bus voltages,
  straight from a 110 kV island bus to a 220 kV main bus. It now runs at the
  island's voltage; when the nearest main bus is at another level (more than
  10 % apart), it ends at a new bus beside that bus, joined to it by a
  transformer, as the equipment chains do.

- **SLD line flows on parallel lines.** In results mode every line between
  two nodes showed the node pair's whole flow, counted in one direction
  only, over that line's own capacity, so parallel lines showed the same
  flow and wrong loadings. The net flow of the pair is now shared among its
  lines by susceptance (1/X) when they all have a reactance, otherwise by
  capacity, and each line's loading is its share over its own capacity.

- **Parallel lines are summed in the flow between two buses.** When several
  lines (or transformers) joined the same two buses, the `power_flow` result
  kept only the first line's flow. Everything built on it undercounted the
  others: the HDF5 node `power_flow` dataset, the single-line diagram, the
  import/export charts, the dashboard and the inter-system
  `power_flow/{A}_to_{B}` flows. The pair flow is now the sum of every line
  between the two buses. A custom constraint on `power_flow` also saw only
  the first line. It now covers all of them, and the pair may be given in
  either direction (`[2, 1, h]` is minus `[1, 2, h]`). A pair with no line
  between it is now a clear error, not a `KeyError`. The per-line flows are
  unchanged.

- **The impedance of small transformers depends on their rating and the
  system base.** The Grid Builder gave every transformer under 100 MVA a
  flat 0.06 p.u. (below 10 MVA) or 0.10 p.u., whatever its rating and
  `ac_power_flow.base_mva`, and capped larger units at 0.14 p.u. Every size
  is now referred to the system base as z_own × S_base / S_rated, with a
  typical z_own of 6 % below 10 MVA and 10 % from 10 MVA up, and a floor of
  0.005 p.u. A small transformer therefore couples its buses as weakly as it
  physically does: a 5 MVA unit is 1.2 p.u. on a 100 MVA base, where it
  used to be 0.06. The transformer form accepted only 0 to 1 p.u. and cut
  such values to 1.0. It now accepts up to 1000 p.u.

- Candidate technologies burning a priced fuel now pay for it. The loader
  computed `fuel_cost` from the `fuels` block (`price_base / energy_content /
  eff_at_rated`) only for generators, so a technology such as `fuel: Fuel_oil`
  without an explicit `fuel_cost` kept a fuel cost of 0: the master built it
  as free generation and the dispatch ran the built units at no fuel cost (on
  the Cuba case, 2,538 MW of fuel-oil engines produced 20.7 TWh for 21.6 M$ of
  fuel a year instead of about 1.8 G$). Technologies now get the same link per
  node (an explicit `fuel_cost` wins; Sun, Wind and Water stay free; a priced
  fuel such as biomass is paid by a `Renewable` technology too). The non-fuel
  O&M adder of generators is not applied to technologies, since it stands in
  for capital cost they pay through `invest_cost`. A `Non-renewable`
  technology left with no fuel cost and no offer curve is named in a warning.
- The dispatch no longer logs "Virtual gen …: NO availability matched for
  file=None" as a warning in every window for each unit built from a
  technology without an availability profile (1,045 times in a 5-year run).
  Such a unit is available at full capacity, which is normal for thermal
  plant; a renewable technology without a profile is still reported once per
  run. The per-window "Virtual gen" summary line moved from INFO to DEBUG.

- **Myopic masters were infeasible after a year that reached a high RE
  share.** Since the achieved RE share was reported, each myopic year's
  increment bounds started from the share the year before had reached, which
  can pass 100 % when renewables also charge storage or cover losses. The
  credited share is capped at 1, so the minimum increment asked for more
  than 100 % and every later master was infeasible in presolve (Cuba,
  25-year run: 107 % in 2027, then `INFEASIBLE_OR_UNBOUNDED` from 2028 on,
  each year logged and skipped). The ratchet also forced overbuilding before
  that: the 2026 master built 9.9 GW of renewables to add one point of
  share. Each myopic year's bounds now start where the perfect-foresight
  chain stands the year before (initial share plus one minimum step per
  year), so a one-year master gets the bounds perfect foresight places on
  its year.
- **The RE increment bounds no longer make a master infeasible.** The
  minimum increment chained the credited share from the initial one, so a
  high initial share or a long horizon (initial share + years × minimum
  step above 100 %) made the perfect-foresight master infeasible too. The
  minimum step now stops at 100 %: once the share reaches it, it only has to
  stay there. The RE target path is also capped at 100 % (the minimum step
  could push it above).

- **The maximum annual RE increment caps the renewable share the plan
  actually reaches.** `max_annual_increment` bounded the yearly change of a
  *credited* share that the master could set as low as it liked (it only
  had to stay below renewable energy plus shortfall), so it never limited
  the renewable energy: a myopic Cuba run went from a 5 % share to 89 % in
  its first year, and perfect foresight did the same. The master now caps
  the actual share of each system and year (renewable energy used, after
  curtailment, over demand, as the master weights the representative days
  or TSAM periods): at most `max_annual_increment` above the previous
  year's actual share, year 1 above the initial share. A system at 5 % with
  the default 0.10 reaches at most 15 % in year 1 and 25 % in year 2, and a
  year that grows less lowers the next year's cap. Renewable energy above
  the cap costs the value of lost load (`loss_of_load`) per MWh, so the
  master curtails or runs other units instead of building more renewables;
  the slack only keeps the model feasible when existing renewables must
  produce more than the cap allows (the curtailment limit keeps them
  running), and the diagnostics report it as `re_max_increment_s*_y*`. The
  cap holds in perfect foresight, TSAM, MGA, per scenario in the stochastic
  master (each over its own previous year), and in Benders, where the
  master now sets each representative day's renewable energy and the
  subproblems match it. In myopic mode each year's cap is measured from the
  share the previous year achieved (clamped to 100 %); the
  `min_annual_increment` floor still follows the perfect-foresight path,
  its shortfall priced at `FRE_penetration_loss` per MWh as before. The
  Cuba myopic master now goes 5.2 % → 15.2 % → 25.2 % → 35.2 % over
  2025-2027, each year within its 2.147 G$ investment budget.

- **Gurobi `Crossover` and `BarConvTol` were silently dropped.** The
  method-specific option filter compared the method's label with the value
  the Studio saves (`Method: 2`), so it never matched: `Crossover` and
  `BarConvTol` were dropped ("not currently selected") even with barrier
  selected, and with `Method: -1` (automatic) too. Gurobi, CPLEX and Xpress
  accept every parameter and their automatic method can run barrier, so an
  option is now passed under the automatic method when it applies to it
  (Gurobi `crossover`, `bar_conv_tol`, `iteration_limit`; CPLEX
  `barrier_conv_tol` and the simplex tolerances; Xpress `BARGAPSTOP`), and
  dropped only when another method is selected. HiGHS keeps the strict rule
  (an explicit method is required). With Gurobi `Method` automatic and
  `Crossover` off, the log recommends `method: barrier` once per run: on an
  LP the concurrent optimizer splits the threads between barrier and
  simplex.

- **A dispatch stopped at a limit with an infeasible point was accepted.**
  The check for a feasible incumbent looked for the text `FEASIBLE_POINT`,
  which `INFEASIBLE_POINT` also contains: a window stopped at an iteration
  limit with an infeasible primal point was taken as solved. Only a
  feasible point is accepted now.

- **A small battery build no longer makes the next myopic year infeasible.**
  The myopic runner carried each year's builds to the later years only when
  they exceeded 0.1 MW or 0.1 MWh, each value on its own. A battery the
  master built with 0.09 MW and 0.18 MWh reached the next year with its
  energy and no power; its minimum-cycling constraint then asked for charge
  that a zero charge limit forbade, and every dispatch window of that year
  was infeasible (Cuba myopic run, year 2026). Every build above solver
  noise (10⁻⁶ MW or MWh) is now kept whole, in the myopic carry-over, in the
  perfect-foresight investments and in the dispatch's investment units, so
  the dispatch and the later years see the capacity the master paid for.
  The reported investments therefore also list builds below 0.1 MW.

- **Battery constraints can no longer make a dispatch window infeasible
  when the battery cannot charge.** A battery with energy but no charge or
  discharge power at a bus now holds no energy there (it cannot move any),
  so no SOC condition applies to it. The minimum cycling
  (`min_cycling_ratio` / `min_cycling_period_days`) is capped at what the
  battery can charge in the window (none during a whole-window outage), and
  a shortfall it cannot avoid (ramp limits) is priced at 10 % of VOLL per
  MWh instead of being infeasible. During steps without charge capability
  (an outage, zero availability) self-discharge may take the SOC below
  `soc_min`; the floor returns as fast as full charging allows. The cyclic
  end-of-horizon band (`cyclic_end_soc`) asks at most for the SOC that full
  charging can reach. Each of these used to make the window infeasible for a
  battery that could not charge. A battery that can charge behaves as before.

- **ESFEX no longer deletes its Julia `Manifest.toml`.** Any error while
  loading the ESFEX Julia module, even a syntax error in one of its `.jl`
  files, was treated as "dependencies missing": ESFEX ran `Pkg.instantiate()`
  and, if that failed, deleted `Manifest.toml` and re-resolved the project,
  silently installing other package versions (with juliacall's Julia this
  could leave HiGHS failing in every solve). Now only a missing or
  uninstalled package triggers a repair. The Manifest is backed up to
  `Manifest.toml.bak-<timestamp>` first (removed again if it does not
  change), `Pkg.instantiate()` installs the recorded versions, every step is
  logged as a warning, and if `Pkg.instantiate()` fails ESFEX stops with an
  error instead of re-resolving. Set the new environment variable
  `ESFEX_JULIA_RESOLVE=1` to allow `Pkg.resolve()`. Any other load error is
  reported with the file and line that failed, and the Julia environment is
  left untouched.

- **Renewable energy above the RE growth cap is curtailed, not charged at
  the value of lost load.** When a system's existing renewables alone would
  pass the cap (`MAX_ANNUAL_INCREMENT` over last year's share) and the
  curtailment limit (`max_curtailment_ratio`) kept them running, the excess
  went to the cap's slack at VOLL: three years of the Cuba myopic run cost
  8.3–10.9 G$ instead of about 2 G$, for some 1.2 TWh above the cap. The
  excess is now curtailed: it does not count toward the share, pays
  `curtailment_cost` per MWh like any curtailment, and curtailing the
  existing variable renewables down to the cap is exempt from the
  curtailment limit. Building renewables above the cap still does not pay,
  since only the existing units are exempt and the new units' energy would
  be lost. The same holds in the monolithic, TSAM, stochastic, Benders and
  myopic masters; the VOLL slack remains only for renewable output that
  cannot be curtailed (a reservoir's minimum release, say). The dispatch
  has no growth cap and is unchanged.

- **Benders decomposition converges on large cases.** On the Cuba
  development case the lower bound stayed at the investment cost while the
  upper bound was hundreds of times larger (25 years: gap 99.89 % after four
  iterations of 24–36 minutes; θ was 0 in every iteration). Three causes,
  all fixed; the optimum is unchanged:
  - Each representative day was its own subproblem, and the master shared
    each year's RE target, growth cap and CO2 budget out among the days, a
    deviation either way priced at the value of lost load. The master moved
    those shares from day to day at no cost of its own and every cut was met
    with θ = 0. Benders now solves one LP per year with all its
    representative days and the year's CO2 budget, RE target and growth cap
    inside it, as in the monolithic master, and adds one cut per year. With
    a growth cap the master keeps each year's actual RE share, which the
    next year's cap is measured from; the year must reach it (a shortfall
    costs the value of lost load), so the cap is never looser than the
    monolithic one.
  - θ had no lower bound but 0, so the master moved capacity that cost it
    nothing (a battery it had already paid for) to wherever the latest cut
    promised the most, one iteration after another. Each year's θ is now at
    least the cost of a copper-plate relaxation of that year built into the
    master (pooled units, one store per system, load shed at the value of
    lost load, the year's RE target and growth cap); the cuts only add what
    it leaves out, mainly the network.
  - At a bus with no battery the subproblem's duals could value battery
    power alone at the value of lost load in every hour. The subproblems now
    see a sliver (10⁻⁴ MW) of each battery technology wherever one can be
    built, so their duals price power and energy as a battery can use them.

  Cuba reduced to three years: gap 98.72 % after eight iterations before,
  0.86 % after 25 now (lower bound 829 → 6260 M$, upper bound 65 332 →
  6 315 M$).
- **Benders subproblems use the configured solver.** They always ran on
  single-threaded HiGHS, also when `solver.name` was Gurobi (12–17 s per Cuba
  day). They now use the configured solver with its threads and options
  (HiGHS still single-threaded): a three-year Cuba iteration takes about
  40 s with Gurobi. Failing subproblem LPs written with
  `ESFEX_BENDERS_DEBUG_LP=1` are now named `benders_sp_failed_y<y>_iter<i>.lp`.
- **Representative days stand for the year over the days actually
  selected.** When fewer days could be selected than
  `representative_days` asks (a `min_day_separation` that leaves no more, or
  under a year of data), each day still stood for 1/`representative_days`
  of the year, so every cost and energy of the master (monolithic,
  stochastic, myopic, Benders) was underweighted: three days asked 200 days
  apart gave two, and the year's operating cost was a third short. Each day
  now stands for the year's days over the days selected, and the log warns
  when fewer than asked could be selected.

- **Benders no longer stalls on years that break the curtailment limit.**
  On the 25-year Cuba development case the gap stayed at 93 % after four
  iterations: from the second iteration more years broke the 5 %
  curtailment limit each time (iteration 3: every year shown), such a year
  only gave a feasibility cut, its θ got no new optimality cut, the lower
  bound stopped moving and no iteration gave an upper bound. Those years
  also counted 0 in the logged `total_ops`, which therefore fell from one
  iteration to the next. Four changes; the optimum is unchanged:
  - The year subproblems are solved with the curtailment limit priced at
    the value of lost load per MWh above it. That LP never costs more than
    the one with a hard limit, so its cut bounds the year at every plan: a
    year that cannot meet the limit now adds this cut as well as the
    feasibility cut, and counts at its priced cost in `total_ops`. A year
    whose priced LP broke the limit although it can meet it (meeting it
    costs more than the value of lost load there) is solved again with the
    hard limit and counts as within it. Only iterations with every year
    within the limit give an upper bound, as before.
  - The master's copper-plate bound on each year's operating cost holds
    each day's curtailment limit, and, where the subproblems cannot draw
    more power than their line losses, no supply above the load: it
    curtailed for free whatever renewable capacity the master built. (On
    Cuba, whose lossy lines can lose more power than the load, the second
    part does not bind and the cuts carry the limit.)
  - In-out stabilisation: while the gap is above 1 %, the subproblems are
    solved halfway between the master's plan and the previous point, as
    the cuts price capacity and RE shares at the value of lost load and the
    master's plan swung between extremes. At the master's plan after an
    iteration that did not raise the lower bound, with integer master
    variables, and once more to confirm a converged halfway point.
  - Each iteration logs how many years met the limit, each year that did
    not and by how much (curtailment against the limit, GWh/year), and
    the operating cost by term (generators, load shed, RE target
    shortfall, RE share below the master's, curtailment above the
    limit, …); at DEBUG each year's θ, cost and terms, and what the master
    builds each year (MW per technology, capital against the budget and
    its slack, RE shares). The per-year lines of the first three iterations
    at INFO are gone.

  Cuba reduced to eight years (5 days, Gurobi): after six iterations the
  gap was 32.7 % before (iterations 2–5 had years beyond the limit and
  gave no upper bound) and is 19.6 % now; after 15 it is 2.3 % (lower
  bound 13 719 M$, upper bound 14 046 M$; it was 30 872 M$ for the first
  five iterations). Most of the cost of the early plans was load shed and
  RE shares the master asked for and the network could not deliver, both
  at the value of lost load; the plan now builds engines and has none.

- **Lines no longer lose energy they do not carry.** The PWL (and linear)
  loss model of the DC power flow let a line carry flow in both directions
  at once, and fill its steep segments before its flat ones, so the loss
  could exceed the loss of the line's net flow by up to twice its full-flow
  loss. Whenever discarding energy paid (curtailment costs
  `curtailment_cost`, a curtailment limit or an RE target that counts
  renewable output binds), the solver did exactly that: surplus renewable
  energy disappeared as "line losses" instead of being curtailed, escaping
  the curtailment cost and `max_curtailment_ratio`. On the Cuba 25-year
  myopic run the dispatch "lost" 1.0 TWh in 2030 (4 % of demand) and
  9.3 TWh in 2049 (32 %), with curtailment at 0, and generation reached
  38 TWh for 29 TWh of demand. Every solve of a model with line losses (the
  dispatch, the master problem, the Benders subproblems, MGA) is now
  repaired: at each step where some line loses more than the PWL loss of
  its net flow, every lossy line of that step is held in its flow direction
  with a loss linear in the flow (the PWL secant at that flow, updated until
  it is within 2 % of the PWL loss) and the model is solved again (at most
  8 rounds). Steps without such losses keep the PWL model. The surplus is
  now curtailed, at its cost and within its limits, and reported as
  curtailment. One week of Cuba 2025 unit commitment with 7.3 GW of PV:
  before, 45.5 GWh "lost" on the lines (12 % of demand) and no curtailment;
  now 7.2 GWh of losses (1.9 %) and 38.5 GWh curtailed. The model stays an LP (or the MIP it was); a solve with
  surplus energy costs one or a few more solves. If the repaired model has
  no solution (losing energy on the lines was the only way to balance it),
  the PWL solution is kept and a warning is logged. See *DC Power Flow →
  Exact losses* in the formulation docs.

- **The master applies scheduled generator and battery outages.** The
  `outage_schedule` windows of existing generators (per node or the whole
  unit) and batteries (whole unit) reached only the dispatch; every master
  now applies them on the representative days or TSAM periods it models,
  with the dispatch's rules (horizon hours from 1 January of the first
  planning year, recurring windows every year, a step partly inside a
  window derated in proportion), also in the capacity-adequacy credit at
  the peak and in the existing renewables' energy behind the growth cap's
  curtailment exemption. On the Cuba 2025 case `pv_2025` (729 MW) is out
  until each park is commissioned (0.58 TWh in the year instead of 1.18 TWh)
  and `ps_suheyla` from hour 5112; the master counted the full year, so its
  2025 RE share (15.0 %) was well above the dispatch's (12.1 %).
  With the outages, the 2025 starting share falls from 5.2 % to 3.6 % and
  the one-year 2025 master's RE share from 14.7 % to 11.1 % (14.8 % before
  this release, measured on demand).
- **Benders: the operating bound is valid with linked systems.** The
  copper-plate bound on each year's operating cost balanced every system
  on its own, so a system that imports power over a line or converter in
  the subproblem had to shed load in the bound, and the lower bound could
  pass the year's real cost (two linked systems, one without units: a first
  bound of 4.4·10⁶ M$ against an optimum of 1.1·10⁴ M$). Systems linked by
  a line or converter now share one balance per step. A lossless line
  between two systems also left their supply capped at their own load in
  the bound; it no longer does.

- **Benders no longer flags a year a few MWh above its curtailment limit.**
  A year counted as breaking its limit for any slack above 1e-6, so solver
  tolerances made years "break" it by "0.0 GWh/yr" (Cuba, 2046), each with a
  feasibility cut and no upper bound. A year is now within a system's limit
  when it curtails at most 1e-3 MWh/year + 1e-6 × the system's renewable
  generation of the year above it; such a year is solved with its slacks
  held at that least violation, and the master's operating bound allows the
  same tolerance.

- **Benders' operating bound counts each lossy line's loss once.** The bound
  let a system's supply exceed its load by what its lossy lines lose with
  both flow directions at full flow. Since lines lose no more than the PWL
  loss of their net flow, it is the full-flow loss in one direction: half
  the room, so the bound curtails less for free.

- **Studio equivalent branches lose what their lines lose.** The level-2
  Grid Builder reduction (one bus per node) joins two nodes by one branch
  standing for all the lines and transformers between them. Its resistance
  was `1/R = Σ 1/R_i` over the lines that had one, so one lossy line beside
  lossless ones set the branch's R and the branch could lose far more than
  its lines; transformers entered with R = 0; a line with neither a length
  nor end-bus coordinates added its capacity but no admittance; and the
  branch was labelled with the higher node-bus voltage and one circuit. The
  branch now combines the same set of branches for every value: capacity
  summed, `1/X = Σ 1/X_i`, the loss-equivalent `R = X² Σ R_i/X_i²` (the
  losses of the lines at the DC flow split), B summed. A line with a
  reactance but no resistance gets the R/X of the standard line type of its
  voltage, a transformer the R of its `losses_fraction`, and a line with no
  length spans the distance between the two node buses. The branch carries
  the voltage and the total circuits of its lines. Level-1 merges of
  parallel lines use the same R rule. Rebuild a reduced network in the
  Studio to get the new values; saved cases are unchanged.

- **Repaired loss re-solves no longer fall back to simplex.** After the
  loss repair changes a model, Gurobi (and CPLEX) restarted it from the
  previous basis with dual/primal simplex whatever `Method` said: with the
  default `Method -1` / `Crossover -1` a Cuba myopic master whose first
  solve took 2.5 minutes ran its re-solve for 47+ minutes. The repair's
  re-solves now ignore the previous basis (Gurobi `LPWarmStart = 0`, CPLEX
  `CPXPARAM_Advance = 0`, set back afterwards) unless the method is simplex,
  so they use the first solve's method. A linearised line-step also carries
  its flow on one segment instead of N identical copies, and a round that
  would only update the linearised loss slopes is skipped when those losses
  together miss the PWL losses of their flows by at most 0.1 % of all line
  losses. On a one-year Cuba master with a high-RE fleet (515k rows, 56
  over-losing steps), Gurobi `Method -1` / `Crossover -1`: the first re-solve
  ran 20+ minutes (killed) and now each re-solve takes 1–1.5 minutes (all
  four solves 15 minutes, 11 of them the unchanged first solve); `Method 2`
  / `Crossover 0`: 8 solves in 164 s before, 5 solves in 112 s now, same
  objective (within 1e-6).

- **The master's solution summary reaches the log.** The adapter called
  `log_solution_summary` and dropped the string it returns, so the RE
  target, the investments, the retirements and the cost breakdown per year
  never appeared anywhere. It is now logged at debug level: always in the
  run's log file, and on the console with `--verbose`.

- **Benders upper bound no longer stalls on the curtailment limit.** The
  in-out stabilisation solved the subproblems between the master's plan and
  a core point that moved to every point solved, also one that broke the
  annual curtailment limit. From then on a plan within the limit could give
  a point above it, and no upper bound: Cuba's 25-year run held its bound
  from iteration 10 to 35 at a 19.6 % gap while its plans, with the excess
  priced at the value of lost load, were within about 3 % of the lower
  bound. The core now moves only to points that meet the limits (and the
  subproblems are solved at the master's plan until one does).
- **A Benders master that reaches its 600 s limit is solved again.** A
  master stopped without a plan ended the run (Cuba, iteration 36: 27
  million nonzeros); it is now solved again, and so are the later ones,
  with `solver.time_limit`.
- **Benders returns its best plan instead of aborting.** The best
  iteration's plan is extracted by fixing the master to it; a later
  feasibility cut, or the solver's tolerances on a fixed plan, made that
  master infeasible and the run aborted after 13 hours with no plan. The
  feasibility cuts are now dropped and the plan held within 1e-6 if it does
  not solve, and a master without a plan raises a clear error.

- **Line losses are not repaired from a solve that is not optimal.** The
  repair of the PWL line losses re-solved the whole model from any solve
  with values: Cuba's 25-year monolithic master on HiGHS ended "no
  progress" after 12.8 hours (relative primal-dual objective gap 1.0) and
  its repair started a second solve as long from that point. Such a solve
  is now returned as it is, with a warning, for the caller to handle; a
  repaired re-solve that is not optimal falls back to the PWL solve, as one
  without a solution did.

- **Weather availability was in UTC while demand was in local time.** The
  availability generator, the Grid Builder and the Solar PV / Wind workflows
  saved weather-derived capacity factors on the hour index of the weather
  download, which is UTC (Open-Meteo, ERA5) or local solar time (NASA
  POWER's default), while the Grid Builder demand forecast is in each node's
  local civil time. In a Cuban case PV peaked at hour 17 (solar noon in UTC)
  against a demand whose hour 12 is local noon: the dispatch saw sun at
  23 h local and none in the morning, and shed load from 9 to 13 h. Both are
  now saved on the model clock: weather series are shifted from UTC (NASA
  POWER from the offset of the 15° longitude band), and the demand forecast
  is converted from each node's own time zone, daylight saving time
  included (the hour skipped in spring is dropped, the hour repeated in
  autumn is used twice). The year wraps: the last hours read the first
  ones. Cases generated before this release keep their series as they are;
  regenerate the availability (or the demand) to put them on one clock.

- **The Grid Builder's demand forecast pairs each local hour with that
  hour's temperature.** The demand models read local hours (hour of day, day
  of week, month, holidays), but the base-year ERA5 temperature came from
  Open-Meteo in UTC and was used row by row: in Cuba (UTC-5) the temperature
  given to local hour h was that of local h-5, so the day's warmest hours
  fell in the evening. Each node now gets the temperature on its own local
  civil time (its time zone from its coordinates, with daylight saving time),
  so nodes in different time zones each get theirs, and a leap weather year
  drops 29 February instead of 31 December. This temperature is used when
  CMIP6 climate is unavailable (and by the fallback engines); the CMIP6 hourly
  curve was already on local hours. With the shipped density model, Cuba's
  forecast on this path moves load from the evening to the middle of the day
  (about +10 % of the system load around noon and -5 % late in the evening
  on the average day), its daily peak comes an hour earlier and its yearly
  peak drops by about 1 %; the yearly energy changes by 0.1 %. The ERA5
  fallback of the global demand projector does the same; its cache files
  have a new name (`era5_local_*.npy`), so old ones (UTC) are not reused.

- **The demand training dataset is on one clock.** `esfex
  build-demand-dataset` labelled every Parquet file UTC, but its sources
  deliver demand on different clocks (OPSD and EIA in UTC, ENTSO-E and RTE
  counted from Central European midnight, TEPCO, XM, ONS and AEMO WA in local
  time, AEMO NEM in Australian Eastern Standard Time and IESO in Eastern
  Standard Time all year) and the ERA5 temperature came in UTC, while
  training read every row as a local hour. Each file now holds demand and
  temperature on the local civil time of the series' location, with
  time-zone-aware local timestamps; training reads that temperature. Leap
  years drop 29 February everywhere (the temperature and the national demand
  lost 31 December, the zonal demand 29 February: a one-day shift from
  March). Countries such as Turkey, Chile or Mexico now get their ERA5
  temperature (their coordinates were missing), and the national Australian
  series (Western Australia) is placed at Perth instead of Sydney. ERA5
  downloads are cached under new names (`era5_utc_*.npy`); build the dataset
  again before retraining. The shipped models are unchanged until they are
  retrained.

- **`esfex availability_generator generate` takes its default year from
  `date_start`.** Without `--years` it read a `temporal.date_start` that does
  not exist and always generated 2020; it now uses the year of the
  configuration's `date_start`.

- **The Solar PV and Wind workflows drop 29 February from a leap year.** Their
  availability profiles cut a leap year's 8784 hours to the first 8760, so
  31 December was lost and every day from 1 March was one day early. They now
  drop 29 February, like the rest of ESFEX (`year_series_8760`).


- **Demand calendar features in leap years.** Demand and temperature series
  are 8760 hours with 29 February dropped, but the day of week and the
  holidays of the demand models were counted in real days from 1 January:
  in a leap year every hour from 1 March on had the previous day's weekday
  and holidays (a Saturday read as a Friday, 14 July flagged on 15 July),
  and the TFT annual forecast labelled 1 March as 29 February. Every
  calendar feature (hour, day of week, month, holidays and the days to and
  from them) now comes from the real date of each row
  (`esfex.utils.temporal.year_row_dates`), in the training dataset, the
  density and shape models, the TFT and the Grid Builder's archetype
  demand. Non-leap years are unchanged, and the models receive the same
  feature columns.

- **`esfex build-demand-dataset --sources` fetches only the named
  sources.** Only six sources could be switched off: ENTSO-E, RTE, UK,
  Eskom, Turkey, Chile, Argentina, Mexico, Korea, Taiwan and Peru were
  always downloaded, whatever `--sources` said. Every source can now be
  named, and an unknown name is an error (it was ignored).

- **RTE (France) demand with 15-minute data.** The eco2mix consumption was
  averaged in pairs of rows as if it were half-hourly; with 15-minute data
  the year became 17,520 "half-hours" cut to 8760, so the first half-year
  was spread over the whole year at half speed. The hourly means now come
  from the rows' date and hour, whatever the step (15, 30 or 60 minutes,
  empty quarter-hours skipped), and a year missing more than two days is
  rejected. Text tables named `.xls` inside the eco2mix archives are read
  too.

- **Zonal demand with a daylight-saving fall-back.** Aligning a zonal series
  stamped in civil time (ONS Brazil, which had daylight saving time until
  2019) raised an error at the repeated hour of a fall-back. A repeated wall
  hour now keeps its first occurrence and a wall hour skipped at a
  spring-forward holds the hour after it, the convention of the demand
  clock conversion.

- **Batteries count as firm only up to their energy.** The master's
  capacity adequacy constraint counted every battery (existing,
  life-extended or built from a battery technology) at its full power,
  whatever its duration, so a 4-hour battery covered a 13-hour
  low-renewable night on paper. A battery now counts min(power, usable
  energy / H), usable energy = energy × `max_DoD` × discharge efficiency,
  linear in the master (a firm-power variable per battery technology,
  bus and year). H is the new `master_problem.storage_firm_hours`; by
  default it is derived from the data: the longest stretch without solar
  on the critical days (13 h in the Cuba case), or, with no solar profile,
  the longest stretch whose renewable supply stays below demand (4 h with
  no renewable profile). The log gives H. Plans with short batteries now
  carry more firm capacity or longer storage. The Studio's Global Settings
  has both new fields (Critical Days, Storage Firm Hours), and
  `min_day_separation` stays editable with TSAM because it also spaces the
  critical days.

- **TSAM periods carry the year's renewable energy.** The TSAM periods
  (k-medoids, each weighted by its cluster size) rarely hold the year's
  sun and wind: in the Cuba case with 12 periods they had 6 % more PV and
  12 % less wind than the year, so the master planned on a sunnier, calmer
  year than the dispatch then ran. Once the periods are fixed (the critical
  days and their weights included), every TSAM master (monolithic,
  stochastic, MGA and each annual master of a myopic run) now rescales each
  renewable availability profile it reads (renewable generators and
  renewable candidate technologies, every node, every planning year's
  block) on the periods so their weighted energy equals the year's: each
  period keeps its shape, values are capped at full availability, and the
  energy a capped hour cannot take is spread over the other hours, as the
  `tsam` library does. The critical days are kept as they are (raising
  their sun and wind would remove the shortage they stand for); the other
  periods carry the correction, so the year still matches. Only the
  master's copy of the period hours changes; the dispatch reads the
  original profiles. The demand is not rescaled (its periods stay within
  1 % of the year in the Cuba case). The log gives each profile's ratio
  before and after the rescaling (1.000 unless the cap stopped a profile,
  which a separate line names).
- **The representative-day warning no longer advises TSAM to a TSAM run.**
  "… misrepresent the year by more than 10 % …; use more days or TSAM" was
  printed also when TSAM was in use. With representative days it now
  advises more days (`representative_days`) or TSAM (`use_tsam`). With TSAM
  it is given only for what is still off after the rescaling (the demand,
  or a profile held back by the cap), names it, and advises more TSAM
  periods (`tsam_num_periods`).

- **Myopic master problems no longer end in `NUMERICAL_ERROR` because of
  unbounded line-loss segments.** In the master's representative days and
  TSAM periods every line whose corridor had an investment variable got an
  open last loss segment (no upper bound in either flow direction), even when
  `transference_invest_max` is 0 and the line can never be expanded. The flow
  split `f⁺`/`f⁻` could then grow together without limit: on Cuba (no line
  may be built) Gurobi's barrier iterates ran to 10⁶ MW on the loss
  segments, the dual residual stalled and the myopic TSAM run stopped at
  2027 with `NUMERICAL_ERROR` (the rep-day myopic run at 2046). A line is
  now expandable in the master only if its corridor can gain capacity, and
  its last segment then spans the base capacity's last segment plus the most
  MW the corridor can gain by that year (open only when the investment has no
  limit). The same 2027 model solves in 115 barrier iterations instead of
  failing after 228. (Benders' subproblems are unchanged.)

- **A master solve that ends `NUMERICAL_ERROR` is tried once more.** With
  Gurobi, a master (every mode, MGA included; not Benders) whose solve ends
  `NUMERICAL_ERROR` is solved again with the homogeneous barrier
  (`BarHomogeneous = 1`, Gurobi's own advice), with a warning in the log;
  the parameter is set back afterwards. On the failing Cuba 2027 master it
  solves in 112 iterations.

- **A failed run no longer reports success.** When the master problem of a
  `development` run found no plan, `esfex run` printed "Master problem
  failed. Aborting." and then "Optimization completed successfully!" and
  exited with code 0, so the Studio and scripts saw a successful run. The run
  now raises `RunAbortedError` (a `RuntimeError`, in `esfex.runner`): the CLI
  prints `Optimization failed: the master problem found no plan …` and exits
  with code 1, and `esfex.run()` raises it. A run in which some operational
  windows of a year failed to solve ends with `Optimization finished with
  errors: operational windows of year(s) … failed to solve` and exit code 1
  too (the other hours' results are saved).

- **Better-conditioned master problems.** The renewable-generation ratio
  that links one year's growth cap to the next (`re_energy_ratio`, RE-2b)
  was a variable defined by the row `demand × ratio == renewable energy`,
  which put the year's demand in MWh (2.3·10⁷ for Cuba) in the matrix next
  to coefficients of order 1: every master with representative days or TSAM
  periods had a matrix range up to [10⁻⁴, 3·10⁷]. The ratio is now an
  expression of the day variables (no row, no variable); the Cuba masters'
  range is [10⁻⁴, 10⁵]. Results are unchanged.

- **Solver noise is no longer carried as capacity.** A barrier solve without
  crossover reports tiny values on capacity it did not mean to build (in
  Cuba, 10⁻⁶ MW of PV at 128 of 136 buses) and leaves limits it reached at
  e.g. 3·10⁻⁷ MW. Every master now reports a capacity of at most 10⁻³ MW or
  MWh (1 kW) as 0 (`CAPACITY_TOL`: builds, life extension, cumulative
  capacities, transmission, converters, reservoirs, fuel infrastructure),
  the runner ignores builds up to that size (`MIN_BUILD`, was 10⁻⁶) and room
  of at most that much left under an investment limit counts as none. In a
  myopic run those values became existing units of the next year (Cuba
  2027: 158 000 rows like `gen ≤ 1.8·10⁻⁹`, battery bounds of 10⁻⁶ MWh,
  investment bounds of 3·10⁻⁷ MW; bounds and right-hand sides now start at
  1 and 10⁻⁴), and the models grew every year.

- **A master whose barrier stalls in the loss repair no longer stops a
  myopic run.** On Cuba's rep-day myopic run (Gurobi barrier without
  crossover) the 2037 master stopped the run: the third round of the line
  loss repair, which had only updated 50 linearised loss coefficients of an
  optimal second round, ended "Sub-optimal termination" (Gurobi
  `SUBOPTIMAL`, which JuMP reports as `LOCALLY_SOLVED`). The repair took
  that interior point as a solution, read 85 "over-losing" steps off it and
  repaired them, the fourth round stalled too, and the master, which accepts
  only `OPTIMAL`, ended the run after 12 planned years. Now, with Gurobi,
  every master solve that ends `SUBOPTIMAL` is retried once with the
  homogeneous barrier (`BarHomogeneous = 1`), as `NUMERICAL_ERROR` already
  was, and inside the loss repair each solve is retried on its own instead
  of the whole repair; the stalled 2037 round then solves to optimality in
  136 iterations and finds no over-losing step (the 85 steps were the
  stalled interior point running flow both ways on idle lines). On the
  rerun, five master solves stalled through 2042 (the first solves of 2033
  and 2037, repair rounds of 2036, 2039 and 2042), and the homogeneous barrier
  solved each to optimality, at the cost of one more solve. In a master,
  a repair round whose re-solve is still not optimal (at once with a solver
  without retry parameters) is undone and the model of the round before,
  whose solve was optimal, solved again, with a warning giving how far its
  losses are from the PWL losses of their flows. The dispatch, which accepts
  a point short of optimal, is unchanged.

- **CMIP6 temperature in leap years.** The hourly CMIP6 curve of a leap year
  kept 29 February and lost 31 December, so from 1 March every day read the
  previous day's temperature. It now drops 29 February, like every 8760-h
  year in ESFEX. Open-Meteo CMIP6 downloads are cached under a new name
  (`..._8760.npz`), so the old files are not reused.

- **An enlarged reservoir is used at its full size in the master.** The
  master's reservoir builds raised the level band of each representative day,
  but the day still started (and, being cyclic, ended) at
  `reservoir_initial_level` × the existing capacity, the TSAM boundary levels
  that carry water from one period to the next were bounded by the existing
  capacity, and the head fraction of `reservoir_head_min_factor` measured the
  level from the existing minimum. Water could not be carried across TSAM
  periods in the capacity built, and the master started the day lower than
  the dispatch does with the same reservoir. All of them now use the
  in-service capacity (existing plus built so far). The head fraction's span
  keeps the existing capacity, so the bound stays linear: it is exact at the
  minimum level of an enlarged reservoir and overstates the head above it.
  A master that enlarges a reservoir may build a different amount than
  before (the start level now grows with the reservoir).

- **`fuels.<f>.price_growth_rate` grows the fuel price in every mode.** It
  used to act only on the primary-energy fuel balance: the `fuel_cost` the
  loader derives from the fuels block (and any unit burning a fuel of that
  block) stayed at the first year's price in the dispatch and in every
  master. A generator or candidate technology whose `fuel` is a key of
  `fuels` now pays `fuel_cost` × (1 + rate)^(year − first planning year) in
  the dispatch, the perfect-foresight, TSAM, stochastic and MGA masters and
  each myopic year's master (which grows from the run's first year, not its
  own), and in the LCOE and the unit NPV's years past the horizon. A
  `fuel_cost` of a unit whose fuel is not in `fuels`, and offer curves
  (`fuel_cost_curve`), stay as given. A stochastic scenario's
  `fuel_price_growth` multiplies the grown price (each growth counted once).
  Benders subproblems still charge the first year's price.

- **Grid Builder: a line whose rating parameter inference raises keeps a
  matching impedance.** Inference raised a line's `capacity_mw` to the flow
  it must carry (up to the SIL of its voltage) and left the R, X and B of
  the smaller line, so the raised line drew too little of the flow in the DC
  power flow. The raised line now counts as extra parallel circuits of its
  type: R and X are divided and B multiplied by the rating ratio (the
  convention of the simplification's equivalent lines). A line without a
  reactance first gets the one the builder derives from its length and
  voltage; one whose impedance cannot be derived is named in the result.
  The build result shows a new *Parameter Inference* section with the counts.

- **`solver.time_limit: 0` means no limit.** The schema documents 0 as
  unlimited, but the value was passed to the solver as is, and most solvers
  read a 0-second limit as "stop now" (HiGHS ends `TIME_LIMIT` before
  solving). A limit of 0 now leaves the solver's time limit unset for every
  solver and every model (dispatch windows, masters, MGA); Benders keeps its
  own caps (600 s per master solve until one has no plan, 300 s per
  subproblem day). The Studio's *Time Limit* box accepts 0 (shown as *No
  limit*) and limits above a day; it clamped a loaded 0 to 1 s and anything
  above 86 400 s to 86 400 s.

- **`n1_security_cost` covers each window's committed hours.** With a
  rolling-horizon overlap, the runner took each window's whole-window
  objective difference (with N-1 minus without) and scaled it by kept
  hours / window hours, so the overlap tail (solved again by the next
  window) leaked into the result. Both objectives are now valued over the
  window's committed steps, as the window's objective already is, and the
  year's value adds those. Runs without an overlap are unchanged; in a test
  with a load rising through the year the old value was 2 % too high.

- **The Plotly and Bokeh generation-mix charts show every per-unit
  investment.** They read only the old index-aligned
  `gen_investment_power` / `bat_investment_power` arrays; the per-unit
  datasets (`gen_investment_power/<unit>`, listed alphabetically rather
  than in configuration order) were ignored, so those investments and their
  cost were missing from the investments panel, and the Bokeh chart never
  added the batteries' cost. They now find each unit's configuration by
  name with the same helper as the matplotlib chart.

### Documentation

- **`max_annual_system_cost` is described as what it is.** The field had no
  description, and its name reads as a cap on the total annual system
  cost. It is the annual investment budget: in every master mode it limits
  the overnight cost of what is built in each investment period (soft, 10 $
  per $ over it). The dispatch also uses it, scaled to the window, as a soft
  ceiling on the window's fuel, maintenance and fixed O&M cost. The schema
  description, the configuration reference and the Studio settings page now
  say so. The key is not renamed.

- **A node at sea takes the time zone of the nearest land.** A node whose
  coordinates fall in the sea (an offshore site, or a coastal node placed
  just off the shoreline) got the fixed zone of its 15° longitude band: an
  offshore Cuban node ran on `Etc/GMT+5`, with no daylight saving time,
  instead of `America/Havana`, so its local-time demand was shifted by an
  hour for half the year. The zone now comes from the nearest land within
  200 km (rings searched every 2 km around the point; logged at info level).
  Only a node farther from land keeps the longitude-band zone and its
  warning.

- **Building the demand dataset no longer overwrites a model's country
  map.** Every dataset build (also the one TFT training makes) wrote
  `demand_xgb_country_map.json` into `$ESFEX_DATA_ROOT/data/models`,
  replacing the ISO3 → `country_id` encoding of the XGBoost model deployed
  there, even when the new model was saved elsewhere or not at all. The
  build now writes the map next to the dataset (and keeps it in the frame's
  `attrs["country_to_id"]`); `train_xgboost` saves it next to the model it
  writes, and the ECVI XGBoost backend reads the map next to the model it
  loads.

- **TFT training uses the dataset it was given, without the excluded
  country.** `train_demand_model(engine="tft", exclude_country=...)` built
  the dataset and dropped the country, then `train_tft_model` built the
  whole dataset again from disk: the excluded country leaked into
  leave-one-country-out training, and the build ran twice. The built
  dataset is now passed on (`train_tft_model(df=...)`); the XGBoost path
  also receives the full country map, so the excluded country keeps its id.

- **The cost breakdown's total is the sum of its components.** The penalty
  of the dispatch cost ceiling was in the objective and the breakdown's
  `total` but in no component (Cuba 2025: total 2450.4 M$ against 2080.6 M$
  of components), and so was the battery year-end SOC shortfall (BAT-5). The
  ceiling is gone (above), the operating-budget penalty has its own entry,
  and the SOC shortfall is now in `soc_violation_cost`. The reporting entries
  `pe_electrolyzer_cost`, `v2g_compensation` and `operating_budget_excess`
  stay outside the total (`COST_BREAKDOWN_NON_COMPONENTS`). In a primary-energy
  run the old ceiling also counted the fuel-coupled units' `fuel_cost`, which
  they do not pay there; the operating budget counts the fuel bought instead.

- **The national demand model gets the features it was trained on.** The
  feature builder of ESFEX's national demand model (`demand_ml`, the XGBoost
  model the Studio's Demand Estimation falls back to without the density
  model, and the `xgboost` engine of the global demand projection) filled
  its 29 columns by position from an older 19-column layout: the country
  code held ln(GDP per capita), the GDP column held ln(total population)
  where the model expects ln(population density), urbanization held the
  World Bank percentage instead of the GHSL settlement class, the coordinates
  landed in the holiday columns, and the holiday distances, coordinates and
  daily temperature statistics stayed 0. The model's forecasts rested on
  inputs it had never seen. The features are now filled by name by the same
  code that builds the training dataset (`demand_training.hourly_feature_columns`):
  the country's code from the model's own country encoding (now shipped with
  the model as `demand_xgb_country_map.json`; a country the model was not
  trained on gets an unseen code, with a log message), its public holidays,
  the node's coordinates, the temperature with its daily maximum, minimum,
  range and 1-, 7- and 30-day means, degree hours on the model's 18/24 °C
  bases, and GDP per capita, population density and settlement class
  sampled at the node from the training rasters (the 2020 rasters for a
  later base year), moved over the forecast years by the GDP and population
  growth rates. A node whose GDP or population cannot be sampled, or whose
  temperature series has gaps, is an error (the Studio then falls back to the Archetype method); a missing
  temperature series is 20 °C and a missing settlement raster is class 10,
  as in the training data. The unused `hdd_base_temp` / `cdd_base_temp`
  fields of the Demand Estimation configuration are removed.

- **`esfex.workflows.estimate_demand` works again.** The scripting function
  imported helpers of a removed 3-hourly model and always failed with an
  `ImportError`. It now wraps the national demand model:
  `estimate_demand(iso3, latitude, longitude, year, temperatures_hourly=None,
  annual_demand_mwh=None, static_features=None)` returns the site's 8,760-h
  profile, in MW summing to `annual_demand_mwh` when given, else as shape
  factors with mean 1. The old arguments (GDP per capita, population,
  urbanization, `base_temp`, `beta_cdd`, `beta_hdd`) are gone.

- **Licensing of third-party data and libraries.** `REUSE.toml` declared
  every file in the repository, including the data derived from third-party
  datasets and the bundled JavaScript libraries, as Apache-2.0 by the ESFEX
  authors. Those files now carry their own copyright holders and licences
  (CC0-1.0, CC-BY-4.0, EPL-2.0, MIT, BSD-2-Clause, ISC, and `LicenseRef-`
  entries for Natural Earth's public domain, the UNdata terms of use, the
  Statistics Canada Open Licence and US-Government public domain), whose
  texts are in `LICENSES/`. The licence texts and `DATA_SOURCES.md` are
  now installed with the package (in its `.dist-info/licenses/` folder),
  included in the source distribution, and listed in the conda recipe next
  to `LICENSE`. ESFEX's own code and models stay Apache-2.0.

- **Package licence metadata names every shipped licence.** `pyproject.toml`
  declares an SPDX expression (`Apache-2.0 AND MIT AND BSD-2-Clause AND
  BSD-3-Clause AND ISC AND EPL-2.0 AND CC-BY-4.0 AND CC0-1.0 AND LicenseRef-…`)
  instead of `Apache-2.0` alone (PEP 639, setuptools ≥ 77), and so does the conda
  recipe. plotly.js now ships with `plotly.min.js.LICENSE.txt`, the licences of
  the dependencies it bundles (two of them BSD-3-Clause).

- **NPV retirement no longer keeps a round that makes the plan dearer.**
  A unit can have a negative NPV at `base_lcoe` and still be the cheapest
  way to serve its load. The NPV iteration kept any optimal re-solve, so
  retiring such a unit could raise the plan's total cost (a $32/MWh gas
  unit at a `base_lcoe` of $10/MWh, replaced by a $200/MWh peaker). A
  round is now undone, with a warning naming its units and both costs,
  when the re-solve's total cost is above the plan before's (relative
  tolerance 1e-6). The total cost is the master's objective, which already
  holds every existing unit's operating cost, fixed O&M, life extension and
  decommissioning over the modelled years. A myopic year's master models
  only that year, so the comparison also counts the retired units' later
  years as their NPV values them. Retiring a unit there can cost more in its
  own year (the decommissioning) and still be kept for the fixed O&M it
  saves later. The infeasible and curtailment-excess guards are unchanged.
  This applies to both `esfex run` (`MasterProblemAdapter`) and the Julia
  `solve_with_npv_iteration`. The Julia function also solves each master
  with `optimize_master!` now (the solver's numerical retry, and the warning
  for curtailment above the limit) instead of plain `optimize!`. New Julia
  helpers: `npv_round_reference_cost` and `cost_grew`.

- **A life-extended battery carries energy across TSAM periods.** With
  TSAM inter-period linking, the state of charge at each period boundary
  was bounded by the battery's rated energy only while the battery was
  within its `life_time`. A battery the master kept in service past its
  lifetime (life extension) could still charge and discharge inside each
  period, but its boundary SOC was held at 0, so it could not move energy
  from one period to the next (seasonal storage), and the master
  undervalued extending it. The boundary SOC is now bounded by the same
  energy in service as the period's own SOC: the rated energy within the
  lifetime, afterwards the life-extended MW at the battery's
  energy-to-power ratio. The master's day model, the TSAM chain and the
  capacity expressions now share one helper for an existing battery's
  power and energy in service. As before, the master does not degrade
  batteries.

- **The batteries the master builds degrade.** A battery technology's
  `degradation_rate` had no effect: what the master built kept the power
  and energy it was built with for its whole lifetime, in the master and in
  the dispatch, and a myopic year carried earlier builds at 0 %/yr. Each
  build now shrinks by `(1 − degradation_rate)^age`, age counted from its
  build year (0 in that year), exactly as an existing battery of that age:
  in the master's capacity state (`cap_bt_pow_*`, `cap_bt_en_*`), so the
  day's (or TSAM period's) charge, discharge and SOC bounds, the reserve
  headroom and the firm capacity of the adequacy constraint see it; in the
  reported `cumulative_bat_tech_power` / `cumulative_bat_tech_capacity`,
  which the dispatch receives as the built battery's power and energy (no
  further degradation); and in a later myopic year, which holds the build
  as an existing battery of its age at the technology's rate. Benders'
  copies and cut gradients degrade each build the same way. Fixed O&M stays
  on the power built, in the master and in the dispatch (the built
  battery's `fixed_cost` is scaled by rated / degraded power). With a
  non-zero rate the master now builds more storage, or builds it later, to
  hold the same power in later years, and the reported capacity of built
  storage falls year by year.
- **The master problem honours `MaxChargePower`.** The master bounded an
  existing battery's charge by its discharge power, so a battery that
  charges slower than it discharges (`MaxChargePower` < `MaxDischargePower`)
  recharged faster in the plan than in the dispatch. The charge is now
  bounded by the discharge power in service times the battery's own
  `MaxChargePower` / `MaxDischargePower` (degraded and life-extended alike,
  as the dispatch and the runner's life extension scale both ratings), in
  every master that builds the day from the dispatch's battery constraints
  (monolithic, TSAM, stochastic, MGA, myopic, Benders subproblems). Battery
  technologies have one power rating, so their charge power equals their
  discharge power, as before.

- **The master problem degrades existing batteries as the dispatch does.**
  An existing battery with a `degradation_rate` kept its rated power and
  energy in every master (monolithic, TSAM, stochastic, MGA, myopic,
  Benders), while the operational dispatch of the same year held it at
  `(1 − degradation_rate)^age` of its ratings, so the plan counted on
  storage the dispatch did not have (a 20 MW / 100 MWh battery aged 10 at
  5 %/yr: 20 MW in the plan, 12 MW in the dispatch). The master now ages
  it the same way (age = `initial_age` + years since the first planning
  year) in one place, `battery_power_in_service` /
  `battery_energy_in_service`, used by the day's (or TSAM period's) charge,
  discharge and SOC bounds, the TSAM boundary SOC, the reserve headroom,
  the firm capacity of the adequacy constraint and the reported capacity
  (`cumulative_bat_power`, `cumulative_bat_capacity`). Past its lifetime
  the master can keep at most the degraded power (it could keep the full
  rated power), the energy at the battery's own ratio, and a battery that
  retires pays its decommissioning on its degraded power, as generators
  do; the reported life-extension share is over the degraded power. The
  NPV of a battery degrades its revenue and operating cost over a myopic
  year's later years, and an NPV retirement decommissions its degraded
  power. Fixed O&M stays on the rated `MaxDischargePower`, as the dispatch
  charges it. Capacity built from a battery technology is still not
  degraded (as before), so a myopic year now carries earlier builds with
  no degradation, as perfect foresight counts them.

- **TSAM seasonal storage no longer creates energy.** With `use_tsam` and
  `tsam_inter_period_linking` (the default), the master chained its TSAM
  periods once each, in calendar order, between boundary levels of every
  existing battery and reservoir, while each period's cost, energy balance,
  RE target and curtailment counted as many times as the days it stands for
  (its weight, up to ~40 days in Cuba). A period that discharged on the
  chain had that discharge counted for all its days, and the period that
  recharged it counted its own, so over the year the master's batteries
  could discharge more than they charged and its reservoirs release more
  water than flowed in. In the Cuba 2049 master (12 periods + 3 critical
  days) the weighted change of stored energy was −1.28 TWh/yr and one
  existing battery delivered +170 GWh/yr net, which explains 2–5.5 of the
  3.4–7 point gap between the master's renewable share and the dispatch's.
  On a test year of three day types the old chain put the renewable share
  at 47.1 % against the full year's 39.5 %; the new formulation gives the
  full year's plan exactly.
  The master now uses the superposition of Kotzur et al. (2018, *Applied
  Energy* 213:123–135): each period keeps its own storage trajectory from a
  free start level, and an inter-day level, one per day of the year and
  year-cyclic, moves by the change of the period that day belongs to and
  keeps every day within the storage's range (batteries: 0 to the energy in
  service; reservoirs: minimum to maximum level of the in-service capacity,
  with the evaporation decaying the level day by day). Each period then
  counts in the storage exactly as often as in the costs and balances:
  over the year a battery discharges its charge less its losses, and a
  reservoir releases its inflow less spill and evaporation. A reservoir
  with head-dependent power reads its head from a period start held at or
  below the reservoir's level on each of the period's days. Candidate
  battery technologies stay cyclic within each period, as before.
  `MasterProblemVariables.inter_period_soc` / `inter_period_reservoir` now
  hold the level at the start of each day of the year (keys
  `(unit, bus, day)`, day 0…364) instead of the K+1 period boundaries. The
  master grows by about 3 × 365 rows and 365 columns per battery bus and
  reservoir per year (plus 2 rows per hour of each period).

- **TSAM weights are the periods' day counts.** The clustering now returns
  the period of every day of the year (`TSAMResult.day_period`, which
  replaces `chronological_order`), passed to Julia per year as
  `tsam_day_period` (`MasterProblemInput.tsam_day_period` replaces
  `tsam_chronological_order`; the adapter's keyword likewise). Each period's
  weight is the number of days the map sends to it: a k-means cluster left
  empty takes its representative day instead of a weight of 1 that no day
  backed, and a critical day leaves its cluster (one day less) instead of
  the clusters being rescaled into fractions by the least-change correction,
  which still applies to representative days. The renewable profiles are
  then rescaled to each year's energy on these weights, as before. The
  master checks the map before building the seasonal storage (one entry
  per day of the year, every entry a period, each period's count equal to
  its weight) and stops with an error naming the year and period when it
  does not hold.

- **Curtailment above the annual limit stays the last resort whatever the
  penalties.** The master priced curtailment above `max_curtailment_ratio` at
  a fixed 10⁴ $/MWh, not scaled by a stochastic scenario's multipliers. With
  an RE shortfall price above it (`penalties.fre_penetration_loss`, or a
  scenario's `penalty_fre_penetration` multiplier) the master built
  renewables that curtailed far above the limit to meet its RE target. The
  excess is now priced at `curtailment_excess_penalty`: at least 10⁴ $/MWh
  and 100 × the dearer of the RE shortfall price and the curtailment cost of
  the scenario, so the limit keeps its priority. With the default penalties
  the price is unchanged.
- **The financial analysis values fuel at each year's price.** Fuel was
  valued at the flat `fuel_cost` in every year, while the dispatch grows it
  with its fuel's `price_growth_rate` (`fuels` block). The results file now
  records the growth rates and the base year
  (`system_configuration/fuel_price_growth`), and the financial analysis
  (per-technology report, investment metrics and the cost recalculation of
  files without a cost breakdown) grows each unit's `fuel_cost` by
  `(1 + rate)^(year − base_year)`, as the dispatch does. Older files keep
  the flat price, with a note in the log.
- **Fixed O&M of the batteries the master built is on their rated power in
  the financial analysis.** Since built batteries degrade, the power
  recorded for them in the results (`rated_power` of their
  `battery_discharge` dataset) was the degraded one, and the financial
  analysis charged their fixed O&M on it. The results now record their
  rated power, as for the configured batteries, so fixed O&M stays on the
  power built. Installed-capacity charts now show a built battery at its
  rated power, like the configured ones.
- **The financial analysis pays for every build.** The capex of the units
  the master built was booked on the year-on-year growth of their power in
  service: a build that only replaced retired or degraded capacity (a
  battery rebuilt as it degrades, a fleet kept constant) cost nothing. It
  is now each year's builds from the master's build records in the results
  file × the technology's investment cost, in the system cash flows, the
  investment metrics and the per-technology report (which booked no capex
  for built units). Files without build records keep the old estimate.
- **Preferences accept "no time limit".** The solver time limit in
  *File > Preferences… > Solver & Simulation* only took 60 to 86 400 s,
  although 0 means no limit (as in the project's Global Settings). It now
  takes 0 (shown as "No limit") and limits of more than a day.
- **A self-crossing zone outline has an area.** The Zone form measured a
  drawn development zone with the shoelace formula on the raw outline, so
  the lobes of a self-crossing ("bowtie") outline cancelled and a symmetric
  one showed 0 km². The outline is now repaired first (as the demand
  model's domain clip already did) and the areas of its parts are added.
- **`esfex.workflows.tsam_cluster` works.** It passed `n_periods` to
  `compute_tsam_periods`, which takes `num_periods` (TypeError), did not
  accept the 1-D demand series it documented, and described result fields
  (`representative_days`, `weights`, `mapping`) that do not exist. It now
  clusters an hourly series or an hours × nodes matrix and documents the
  real `TSAMResult` fields (`period_start_hours`, `period_weights`,
  `day_period`, `num_periods`, `critical_periods`).
- **XGBoost demand training into a new directory.** `train_xgboost` created
  the `cv_predictions` folder next to the model without its parents, so an
  output directory that did not exist yet stopped the training
  (FileNotFoundError) before the model was saved. It is created with its
  parents now.


- The Grid Builder page called the Natural Earth admin-1 boundaries used to
  name nodes offline "bundled". They are neither shipped nor downloaded on
  demand: the page now says where to put the shapefile
  (`$ESFEX_DATA_ROOT/data/gridded/shapefiles/ne_10m_admin_1/`) and that the
  step is skipped without it.

- **Coastal and border cells count in the density demand forecast.** The
  density engine (Grid Builder and Demand Forecast) used a 0.25° cell only
  when its centre was inside the study domain, so coastal cells centred at
  sea were dropped whole: Cuba lost about a third of its cells and 7 % of
  its population, and coastal cities lost part of their share even when the
  total was scaled to a national figure. Every cell that overlaps the domain
  is now used, and its demand covers the share of its land inside the domain
  (density × land area × `w`, `w = min(1, area(cell ∩ domain) / land area)`):
  a coastal cell whose land the domain covers counts whole, and a cell on a
  land border is split between the countries on either side. The model's
  predictors are unchanged (densities stay per km² of the cell's land). For
  Cuba the population covered rises from 10.4 to 11.0 M (11.2 M with a
  detailed coastline) and the unscaled annual demand by about 4 %. The
  per-node area split of the study region weighs its samples the same way.
  `region_cells_025` now also returns each cell's fraction inside the domain,
  and `land_overlap_weight` gives the weight.

- **The line-loss repair can depend on the first solve's line
  directions (known limitation, 0.5).** When the lines discard energy in
  the first solve (surplus renewables, curtailment priced or capped), the
  repair holds each line of an over-losing step in the direction that
  solve gave it, and that direction is arbitrary there: Cuba's one-year
  master with 8 GW of PV costs 6.69 G$ under Gurobi's barrier without
  crossover and 6.99 G$ under its default method (+4.4 %). A direction
  check that releases held line-steps whose reversal would lower the cost
  is implemented (5.77 G$ and 5.79 G$ on that master) but off: it took the
  repair from 3 rounds to 19 and the master 2.5 to 10 times longer, a TSAM
  year of the Cuba case from 0.5 h to over 6 h. It comes back as an option
  once it is faster. The repair runs at most 8 rounds, as before, and ends
  on its best round without over-loss.
- **Solves with HiGHS 1.16 (HiGHS.jl 1.27) no longer end
  `OTHER_ERROR`.** HiGHS 1.16 runs every solve of a process on one thread
  scheduler, sized by its first solve, and refuses a solve that asks for
  another thread count. The Julia `Manifest.toml` is not shipped, so a
  fresh install got that version, and a solve after one with another
  count failed: Benders' single-threaded subproblems after its master, or a
  second case with other `solver.threads` in the same Python process (121
  failed checks in the Julia tests). Every model ESFEX builds
  (`solver_model`) now resets the scheduler before a solve whose thread
  count changed (`sync_highs_threads!`); a run with one count is
  unaffected. Tested with HiGHS 1.14 and 1.16.


- **The Grid Builder says when it cannot name nodes offline.** Without the
  Natural Earth admin-1 shapefile (not shipped), the nodes Nominatim did not
  name kept "Node i" with no explanation, and every one of them tried to read
  the missing file again. ESFEX now logs a warning once, with the path to put the
  shapefile at, and the file is not looked for again.

- **Runs with GLPK work.** Every master solve set the solver output through
  `output_flag`, a parameter name only HiGHS has, so with
  `solver.name: glpk` the first master solve failed (`UnsupportedAttribute`)
  and the run aborted. The master now uses JuMP's solver-independent silent
  switch, as set from `solver.verbose`.

### Removed

- **The Bokeh and Plotly results-chart modules.** `results_charts_bokeh.py`
  and `results_charts_plotly.py` (about 3,200 lines) were not used: the
  results viewer and panel take their charts from `results_charts.py`, and
  Bokeh was not even a dependency. Their four tests went with them.

- The unused modules `availability_generator/solar_cf.py` and `wind_cf.py`
  (and their tests). Profiles are computed by `solarex` / `windrex` and, in
  the Grid Builder, by `open_meteo_weather`.

- The previous density model files (`demand_density_hourly_xgb.ubj`,
  `.feats.json`) and the scripts that retrained it on local civil time
  (`scripts/demand_model/clock_audit.py`, `clocks.py`,
  `series_clocks.csv`, `build_density_demand_dataset.py`,
  `train_density_demand_hourly.py`). The shipped model is fitted by the
  article's own pipeline; `scripts/demand_model/` now holds a copy of the
  article's model code, the provenance of every shipped file and
  `export_esfex_assets.py`, which writes them from the article's outputs.
- Clipping of the density model's inputs to their training range (a tree
  model is flat beyond its outermost split; the article does not clip).

- The unused `SimulationDialog` (`panels/simulation_dialog.py`) and the
  preferences `simulation.default_output_dir` and `advanced.cache_dir`,
  which were defined and translated but never shown nor read, with their
  translation strings.
- The unused per-node samplers of the demand-density model
  (`compute_node_areas_km2`, `sample_node_densities`,
  `sample_node_pop_density`, `sample_node_gdp_density`,
  `sample_node_pop_and_area`) and their private helpers: nothing outside the
  tests called them (the demand build works on the 0.25° cells).


- **The TFT demand engine, the gridded ECVI and the PyTorch dependencies.**
  XGBoost is now the only demand engine. The Temporal Fusion Transformer
  (`models/demand_tft.py`, `models/tft_inference.py`) had been disabled
  for some time (every TFT request already resolved to XGBoost), and the
  gridded ECVI (`models/ecvi_gridded.py`, with the script that prepared its
  CMIP6 sub-daily inputs, `models/download_cmip6_subdaily.py`) and the
  `GlobalDemandProjector` batch projector of `models/demand_projection.py`
  (with its CMIP6 / ERA5 point fetchers, annual trajectory and GDP
  elasticity) had no users. `torch`, `pytorch-lightning` and
  `pytorch-forecasting` are no longer dependencies, which takes about
  4 GB out of a Linux install (torch itself 1.6 GB plus some 2.5 GB of
  CUDA libraries it pulls in).
- The `--engine` / `-e` option of `esfex train-demand-model` (its only
  remaining value was `xgboost`), and the `engine` and extra keyword
  arguments of `demand_training.train_demand_model`. The command trains
  the XGBoost hourly shape model; `--engine` is now an unknown option.
  `DemandMLModel` accepts only `engine="xgboost"` or `"auto"` (the same
  model); any other engine raises `ValueError`.
- The unused demand-shape autoregressive lag columns (`shape_lag_1h`,
  `shape_lag_24h`, `shape_lag_168h`) of the training dataset frame, which
  only the TFT comparison read. The XGBoost features and model are
  unchanged.

- `demand_density_ml.gdp_density_point`, a point GDP-density lookup with its
  own interpolation between SSP steps that nothing in ESFEX called (the demand
  estimation reads each cell's SSP GDP and interpolates between steps itself).

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
