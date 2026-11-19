# Using ESFEX from Scripts and Agents

Everything a study needs can be done from the command line, without the Studio or opening the HDF5 file by hand: check a case, run it, and read what it decided. For a script, a CI job or an AI agent, four commands print one JSON object instead of text:

| Command | Schema | What it gives |
|---------|--------|---------------|
| `esfex validate -c CASE --json` | `esfex.validate/1` | Whether the case loads, each error with the YAML key it is about, missing data files, warnings, a description of the case |
| `esfex run -c CASE --json` | `esfex.run/1` | Status, results file, wall time, the run's warnings, the KPIs of the last year |
| `esfex summary RESULTS.h5 --json` | `esfex.summary/1` | Per-year costs, RE share, shedding, curtailment, emissions, builds, capacity, storage, losses; the run's warnings; the master's binding constraints |
| `esfex info --json` | `esfex.info/1` | ESFEX, Python and Julia versions, solvers installed and licensed, data assets, paths |

The same functions are available from Python in `esfex.api` (no Qt; importing it does not start Julia):

```python
from esfex.api import validate_case, summary, system_info

report = validate_case("cases/island.yaml")
if not report.ok:
    for e in report.errors:
        print(e.key, e.message, e.fix)

s = summary("results/island_19112026_1030.h5", years=[2030], sections=["costs", "re"])
print(s.years[0]["costs"]["total"], s.years[0]["re"])
```

Each returns a dataclass with `to_dict()`, the dict the `--json` command prints (without the envelope).

## The JSON Contract

- `--json` prints **exactly one JSON object on stdout**, at the end. Everything else (progress bars, log lines, what Julia and the solver print) goes to **stderr**, so `esfex run -c case.yaml --json > run.json` gives a file that parses, and stderr can be logged or ignored.
- Every document has `"schema": "esfex.<command>/1"`, `"ok"` (true or false) and `"errors"`, a list (empty when `ok` is true) of objects with four fields:

| Field | Meaning |
|-------|---------|
| `code` | Stable machine-readable name: a pydantic error type (`missing`, `list_type`, `less_than_equal`, ...), or `missing_file`, `path_outside_case`, `yaml`, `file_not_found`, `not_hdf5`, `not_a_results_file`, `aborted`, `failed_windows`, `run_error`, or one of the [resume refusals](cli.md#resuming-an-interrupted-run) (`results_not_found`, `checkpoint_missing`, `not_resumable`, `run_complete`, `config_changed`, `data_changed`, ...) |
| `key` | Where: the dotted YAML path (`systems.island.generators.diesel.min_power`), the HDF5 file, or `""` |
| `message` | What is wrong, for a human |
| `fix` | What to change (`""` when there is no hint) |

- The number after the slash changes only when a field changes meaning or is removed; new fields can appear in any version.
- Numbers that are not finite (NaN) are written as `null`. Energies are MWh, powers MW, costs $, emissions tonnes, shares fractions (0-1).

### Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success: the case is valid, the run completed, the file was read |
| `1` | Domain failure: invalid case or missing data file, run aborted or incomplete, not an ESFEX results file. The JSON document says which |
| `2` | Usage error (unknown option, missing argument, file that does not exist): printed by the CLI on stderr, no JSON |

`esfex summary` exits with 0 whenever it could read the file, whatever the run's outcome: read `metadata.status` and `checks` for that.

## `esfex validate --json`

```bash
esfex validate -c cases/island.yaml --json
```

```json
{
  "schema": "esfex.validate/1",
  "ok": false,
  "errors": [
    {
      "code": "missing",
      "key": "systems.island.generators.diesel.min_power",
      "message": "Required key 'min_power' is missing",
      "fix": "Add the key (see docs/reference/config-reference.md for its meaning and units)."
    }
  ],
  "path": "cases/island.yaml",
  "warnings": [],
  "case": null
}
```

Besides the loader's checks (see [`esfex validate`](cli.md#esfex-validate)) it checks that the demand files and the `availability_file` of every generator, technology, battery and battery technology exist, resolved as the run resolves them (relative to the configuration's directory). `warnings` lists the unknown keys (`unknown_key`, with their path) and the warnings the load logs (`loader_warning`: zero ramps, nodes without demand, ...). For a valid case `case` describes it: `simulation_mode`, `solver`, `horizon` (`date_start`, `resolution_hours`, rolling-horizon window), and per system its nodes, buses, lines, generators by type and by fuel, batteries, technologies.

## `esfex run --json`

```bash
esfex run -c cases/island.yaml -y 10 --json > run.json 2> run.log
```

```json
{
  "schema": "esfex.run/1",
  "ok": true,
  "errors": [],
  "status": "complete",
  "exit_code": 0,
  "config": "cases/island.yaml",
  "output_dir": "/home/me/study/results",
  "output_file": "/home/me/study/results/island_19112026_1030.h5",
  "wall_time_s": 812.4,
  "years": [2025, 2026, 2027],
  "warnings": {"total": 9, "kinds": 7, "errors": 0, "groups": [...]},
  "last_year": {"year": 2027, "costs": {...}, "re": {...}, "shedding": {...}, ...},
  "case": {...},
  "resumed_from": null,
  "resume": null
}
```

`status` is `complete`, `failed_windows` (some operational windows did not solve: partial results), `aborted` (the master problem found no plan), `failed` (another error), `invalid_config`, `resume_refused` (`--resume` was refused: `errors` says why) or `dry_run` (with `--dry-run`: the case loaded and its data files exist; nothing was run). `last_year` is the [summary](#esfex-summary-json) of the last year written, all sections. `output_file` is `null` when the run stopped before creating it.

With [`--resume`](cli.md#resuming-an-interrupted-run), `resumed_from` is the first year the resumed run solved (`null` when only the file's finishing was left) and `resume` says where it started:

```json
"resumed_from": 2031,
"resume": {"results_file": ".../island_19112026_1030.h5",
           "checkpoint": ".../island_19112026_1030.checkpoint.json",
           "phase": "dispatch", "resumed_from": 2031,
           "master_years_completed": [2025, ..., 2049],
           "dispatch_years_completed": [2025, ..., 2030], "forced": []}
```

`phase` is `master` when the run stopped while planning the years, `dispatch` when every year was planned; `forced` lists the differences `--force-resume` let through. `years` lists every year in the file, those of the interrupted run included. With `--dry-run` the two fields say where the run would resume, and nothing is run.

## `esfex summary --json`

```bash
esfex summary results/island_19112026_1030.h5 --json
esfex summary results/island_19112026_1030.h5 --year 2030 --year 2035 --section costs,re --json
```

Without `--json` the same data is printed as tables. `--year` (repeatable) keeps those years; `--section` (repeatable or comma-separated) keeps those sections:

| Section | Per year |
|---------|----------|
| `costs` | `total`, `components` (largest first), `reporting` (the entries that are not part of the total: `v2g_compensation`, `pe_electrolyzer_cost`, `operating_budget_excess`), `check` (`ok`, `sum_of_components`, `difference`, `tolerance`: the components add up to the total within 1 $ or 1 ppm), `source` (`cost_breakdown`, or `total_cost` for a file without the breakdown) |
| `re` | `share` (the dispatch's RE share), `target` (the RE target the year's dispatch had), `met`, `gap` |
| `shedding` | `energy_mwh`, `hours` (steps with load shed × step length), `peak_mw`, `share_of_demand` |
| `curtailment` | `energy_mwh`, `share` (of the renewable energy available: curtailed / (curtailed + renewable generation)) |
| `emissions` | `co2_t`, `intensity_t_per_mwh` |
| `builds` | `generation_mw` and `storage_mw` / `storage_mwh` built that year by technology, `retired` (existing units retired by that year: `share`, `mw`) |
| `capacity` | `generation_mw_by_fuel`: MW in service by fuel |
| `storage` | `in_service_mw` per battery, `built_mw`, `built_mwh` |
| `losses` | `energy_mwh`, `share_of_demand`, `loss_repair` (the loss-repair bounds, when the file records them) |
| `warnings` | (run-wide) the run's warnings, from `/diagnostics/warnings` |
| `binding` | (run-wide) `master`: per master year, the binding constraint families (`family`, `count_binding`, `count_total`, `max_abs_dual`, `sum_abs_dual`, `units`) and the paid slacks (`name`, `value`, `unit_cost`, `cost`), from `/diagnostics/master` |

Each year also has `year`, `feasible`, `total_cost`, `demand_mwh`, `generation_mwh` and `solve_time_s`. The document adds:

- `metadata`: `esfex_version`, `config_file`, `solver`, `simulation_mode`, `years_range`, `years`, `num_nodes`, `systems`, `temporal_resolution_hours`, `created`, `finished`, `file_size_mb` and `status`: `complete`, `incomplete` (the run did not finish writing the file: still running, or it stopped), `failed_windows` or `empty`.
- `totals`: sums over the years shown (`total_cost`, `load_shed_mwh`, `curtailment_mwh`, `co2_t`, `demand_mwh`).
- `checks`: what does not add up (`cost_components_mismatch`, `infeasible_year`), each with `year` and `message`. Empty when everything is consistent.
- `notes`: what the file does not record (files written by older versions have no `/diagnostics` group and no `esfex_version`; their fields are `null`).

The summary reads only attributes, the per-year scalars and two small datasets per year (`loss_load`, `curtailment`), never the generation time series, so it is quick on multi-GB files.

## `esfex info --json`

```bash
esfex info --json
esfex info --json --probe
```

Lists `esfex` (version, package path), `python`, `platform`, `julia` (whether `juliacall` is installed, the `julia` on `PATH`, the project and sysimage), `solvers` and `data` (data root, the GDP rasters and population tiles present out of those published, the demand dataset and model). Each solver has `installed` (its Julia package is in ESFEX's project or in the optional-solvers environment), `licensed` and `kind` (`bundled` or `optional`). Open solvers are always licensed; for Gurobi, `licensed` says whether a licence file is where Gurobi looks for one (`GRB_LICENSE_FILE`, the home directory, the default install paths), for Xpress whether `XPAUTH_PATH` or `XPRESSDIR` holds one. The licence files are never read or printed.

By default nothing is started (a fraction of a second). `--probe` starts Julia and loads each installed solver package, filling `julia.starts`, `julia.version` and each solver's `loads` and `version`; it takes as long as a Julia start.

## The Run's Warnings

Every run (`esfex run`, the Studio's Run button, `Orchestrator.run()` from Python) collects the warnings and errors it logs, ESFEX's own and the Julia models' `@warn` lines, and groups them by kind: the logger plus the message with its numbers and quoted names taken out, so `Generator 'pv' has 3 hours ...` and `Generator 'wind' has 7 hours ...` count as one kind. At the end it logs a summary, shown in the `basic` console mode too (which hides the warnings themselves):

```
The run logged 9 warnings in 7 kinds:
  3× [WARNING] esfex.bridge.adapters: Generator 'island__solar' has invest_cost 0 and no candidate technology ...
  1× [WARNING] esfex.config.loader: 2 generator(s) have ramp_up/ramp_down = 0 where they have capacity: diesel, solar. ...
  ...
  … and 2 more kinds (see /diagnostics/warnings or `esfex summary --section warnings`)
```

and writes the groups to [`/diagnostics/warnings`](../reference/hdf5-output-schema.md#diagnosticswarnings) in the results file, also when the run fails. From Python, `Orchestrator.warnings_summary` holds them after `run()`. `esfex run` counts the configuration load's warnings too. Text that Julia or a solver prints directly (the solver log, `println`) is not a log record and is not collected.

## A Typical Agent Loop

```bash
esfex info --json                                   # solvers available?
esfex validate -c case.yaml --json                  # fix errors by key until ok
esfex run -c case.yaml -y 10 --json > run.json      # status, output_file
esfex summary "$(jq -r .output_file run.json)" --section costs,re,shedding,warnings,binding --json
```

Read `checks` and `warnings` before trusting the numbers, and `binding` to see what constrained the plan (an RE target, a budget, a build limit).

A myopic run that stopped before the end (killed, a crash, a lost licence, a time limit: `status` other than `complete` and `output_file` set) need not start over:

```bash
esfex run -c case.yaml --resume "$(jq -r .output_file run.json)" --dry-run --json   # resumable? from which year?
esfex run -c case.yaml --resume "$(jq -r .output_file run.json)" --json > run2.json
```

A refusal is exit code 1 with `status` `resume_refused` and the reason in `errors[].code`: `run_complete` (nothing to do), `not_resumable` (not a myopic `development` run: run it again), `config_changed` / `data_changed` (the case was edited since the run started: start a new run, or `--force-resume` when the change does not matter). The checkpoint (`*.checkpoint.json` next to the results file) is plain JSON; `esfex.io.checkpoint.checkpoint_info(results)` returns it without the state.
