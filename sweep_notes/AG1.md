### Added

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
