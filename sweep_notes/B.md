# Changelog text, group B (results, HDF5 & export)

### Changed

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

### Fixed

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
