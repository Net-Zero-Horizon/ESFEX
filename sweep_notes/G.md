### Added

- **`esfex plugin install --git URL --trust-host`.** Git hosts outside
  github.com, gitlab.com, bitbucket.org, codeberg.org and git.sr.ht were
  always refused on the command line, because the CLI had no way to confirm
  them as the Studio's dialog does. `--trust-host` is that confirmation.
- **`Orchestrator.run(master_only=True)`** stops after the master problem and
  leaves its decisions in `Orchestrator.master_solution`.

### Fixed

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
