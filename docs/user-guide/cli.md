# CLI Reference

All commands are accessed through the `esfex` entry point, built with [Typer](https://typer.tiangolo.com/) and formatted with [Rich](https://rich.readthedocs.io/). `python -m esfex <command>` is equivalent and does not depend on the `esfex` script being on `PATH`.

## Commands Overview

| Command | Description |
|---------|-------------|
| `esfex run` | Run the optimization model |
| `esfex validate` | Load and validate a configuration file |
| `esfex export` | Export an HDF5 results file to CSV, Excel or JSON |
| `esfex studio` | Launch the GIS-based Studio |
| `esfex precompile` | Build a Julia sysimage for faster startup |
| `esfex info` | Show version, Julia and sysimage status, and a quick solver check |
| `esfex add-solver` | Install an optional solver (Gurobi, CPLEX, CBC, SCIP, Xpress) |
| `esfex remove-solver` | Remove an optional solver installed with `add-solver` |
| `esfex list-solvers` | Show which solvers ESFEX can use |
| `esfex plugin` | Manage plugins (list, install, trust, uninstall, enable, disable) |
| `esfex train-demand-model` | Train the demand-estimation ML model |
| `esfex build-demand-dataset` | Download and consolidate hourly demand data |

`pip install esfex` also creates `esfex-studio`, a launcher equivalent to `esfex studio` with no options. On Windows it opens the Studio without a console window.

---


## `esfex run`

```bash
esfex run -c CONFIG [OPTIONS]
```

### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--config` | `-c` | Path | *required* | Path to the YAML configuration file |
| `--mode` | `-m` | String | value in config | Overrides `simulation_mode`: `development`, `unit_commitment` or `economic_dispatch`. The schema default is `development`. |
| `--solver` | `-s` | String | value in config | Overrides `solver.name`: `highs`, `cbc`, `glpk`, `gurobi`, `cplex`, `scip`, `xpress`, `clarabel`, `scs`, `ipopt`. The schema default is `highs`. |
| `--output` | `-o` | Path | `./results` | Output directory (created if missing) |
| `--years` | `-y` | Integer | `25` | Number of years to simulate. `unit_commitment` mode always runs a single year. |
| `--start-year` | | Integer | year of `date_start` | Label of the first simulated year. The date labels then start on `date_start`'s day and time in this year. Demand is still read from the first row |
| `--verbose` | `-v` | Flag | off | Debug-level console logging, solver output on, and the configuration summary table |
| `--dry-run` | | Flag | off | Load and validate the configuration, then exit without starting Julia |

The value passed to `--solver` is not checked by the CLI; a solver that is not installed (see [`list-solvers`](#esfex-list-solvers)) fails when the first model is solved.

### Examples

```bash
# Run with the mode and solver from the config
esfex run -c my_system.yaml

# 10-year simulation with verbose output
esfex run -c my_system.yaml -y 10 -v

# Use Gurobi (installed with `esfex add-solver gurobi`) and a custom output directory
esfex run -c my_system.yaml -s gurobi -o ./gurobi_results

# Dry run: check that the configuration loads, show the summary table
esfex run -c my_system.yaml --dry-run -v

# Unit commitment mode (MIP with binary commitment variables)
esfex run -c my_system.yaml -m unit_commitment
```

### Output

Everything is written into the output directory (`./results` by default):

- **HDF5 results**: `<config_stem>_<ddmmyyyy>_<HHMM>.h5`, e.g. `my_system_29102026_1430.h5` for `my_system.yaml` started at 14:30 on 29 October 2026. Each run creates a new file. The layout is described in the [HDF5 Output Schema](../reference/hdf5-output-schema.md).
- **MGA / SPORES alternatives**: when `master_problem.mga.enabled` is true, the alternatives go into the same HDF5 file, under the `mga/` group.
- **Log file**: `logs/optimization_<YYYYmmdd_HHMMSS>.log`, always at DEBUG level.

How much appears on the console is set by `logging.console_level` in the configuration:

| `console_level` | Console output | Solver log |
|-----------------|----------------|------------|
| `basic` (default) | Progress milestones only; warnings are suppressed | Off, even if `solver.verbose: true` |
| `verbose` | All INFO messages | As set by `solver.verbose` |
| `debug` | Everything, including DEBUG | On |

`--verbose` forces `debug` regardless of the configuration.

#### Console Output

With `--verbose`, the run starts with a header that echoes the command-line options, followed by the configuration summary:

```
ESFEX - Power System Optimization
Configuration: island_system.yaml
Mode: unit_commitment
Solver: (from config)

        Configuration Summary
┏━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┓
┃ Setting             ┃ Value           ┃
┡━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━┩
│ Simulation Mode     │ unit_commitment │
│ Solver              │ highs           │
│ Systems             │ island          │
│   island nodes      │ 3               │
│   island generators │ 5               │
│   island batteries  │ 2               │
│ Rolling Horizon     │ True            │
│ Primary Energy      │ False           │
│ N-1 Security        │ False           │
└─────────────────────┴─────────────────┘
```

!!! note
    The `Mode:` and `Solver:` header lines show only what was passed with `-m` / `-s`. Without `-m` the header reads `Mode: None`; the summary table shows the mode and solver that are actually used.

Each simulated year then logs a result line of the form:

```
Year 2025 completed: objective=$8,234,567
  → RE=62.3% | cost=$8,234,567 | gen=245,120MWh | shed=0.0MWh
```

and the run ends with:

```
Optimization completed successfully!
Results saved to: results
```

#### Dry-Run Output

`--dry-run` loads the configuration (the same checks as [`esfex validate`](#esfex-validate)) and exits. It does not start Julia and does not compute windows or subproblems. Without `-v` it prints only the closing line; add `-v` to also see the header and summary table:

```
Dry run mode - not executing optimization
```

### Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success (or dry run completed) |
| `1` | Configuration error or optimization failure |
| `2` | Usage error (missing `-c`, file not found, unknown option) |


---


## `esfex validate`

Loads the configuration exactly as `esfex run` would, without running anything.

```bash
esfex validate -c CONFIG
```

### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--config` | `-c` | Path | *required* | Path to the YAML configuration file |

### What Is Checked

- The file exists and is valid YAML.
- Every generator and battery under `generators:` / `batteries:` has all required fields (for example `min_power`, `ramp_up`, `fuel_cost`; see the [Configuration Guide](configuration.md)).
- Pydantic types, ranges and allowed values (e.g. `solver.gap` between 0 and 1, `simulation_mode`, `power_flow_mode`).
- Every system listed in `meta_network.systems` is defined under `systems`.

What is **not** checked:

- Per-node array lengths (a `rated_power` list longer than the number of nodes is accepted).
- Whether the demand and availability files exist. A missing demand file fails the run when demand is loaded. A missing availability file only logs a warning and the run continues; an investment technology without its profile is then treated as available in every hour.
- Unknown or misspelled keys. They are ignored without a message.
- Unknown generator `type` values. They are replaced by `Non-renewable` with a logged warning.

Julia is not started, so a successful validation says nothing about the Julia backend or the installed solvers. Use [`esfex info`](#esfex-info) and [`esfex list-solvers`](#esfex-list-solvers) for that.

### Example: Successful Validation

```bash
esfex validate -c island_system.yaml
```

```
Validating: island_system.yaml
Configuration is valid!
        Configuration Summary
┏━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━┓
┃ Setting             ┃ Value       ┃
┡━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━┩
│ Simulation Mode     │ development │
│ Solver              │ highs       │
│ Systems             │ island      │
│   island nodes      │ 3           │
│   island generators │ 5           │
│   island batteries  │ 2           │
│ Rolling Horizon     │ True        │
│ Primary Energy      │ False       │
│ N-1 Security        │ False       │
└─────────────────────┴─────────────┘
```

The generator and battery counts include existing units only, not investment candidates from `technologies` / `battery_technologies`.

### Example: Failed Validation

A generator without `min_power`:

```
Validating: broken_config.yaml
Validation failed:
Failed to load configuration: 'min_power'
```

An out-of-range value:

```
Validating: broken_config.yaml
Validation failed:
Configuration validation failed:
1 validation error for SolverConfig
gap
  Input should be less than or equal to 1
```

Exits with code `1` on failure, suitable for CI pipelines.


---


## `esfex export`

Converts an HDF5 results file to CSV, Excel or JSON.

```bash
esfex export -r RESULTS [OPTIONS]
```

### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--results` | `-r` | Path | *required* | Path to the HDF5 results file |
| `--format` | `-f` | String | `csv` | `csv`, `excel` or `json` |
| `--output` | `-o` | Path | `<results_dir>/export/` | Output **directory** (created if missing) |

`-o` is always treated as a directory. Excel and JSON exports are written into it as `<results_stem>.xlsx` / `<results_stem>.json`.

### Examples

```bash
# CSV export into results/export/
esfex export -r results/my_system_29102026_1430.h5

# CSV export into a custom directory
esfex export -r results/my_system_29102026_1430.h5 -o results/csv/

# Excel workbook: results/report/my_system_29102026_1430.xlsx
esfex export -r results/my_system_29102026_1430.h5 -f excel -o results/report/

# JSON: results/export/my_system_29102026_1430.json
esfex export -r results/my_system_29102026_1430.h5 -f json
```

### CSV Export Structure

One CSV file per HDF5 dataset, from three groups of the results file:

```
results/export/
├── summary/                        # from summary_results/
│   ├── year.csv
│   ├── total_cost.csv
│   ├── renewable_penetration.csv
│   ├── co2_emissions.csv
│   ├── loss_of_load.csv
│   └── ...
├── year_2025_threshold_0/          # one directory per simulated year
│   ├── generation/
│   │   ├── Diesel.csv
│   │   └── ...
│   ├── battery_soc/
│   │   └── ...
│   ├── curtailment.csv
│   ├── nodal_electricity_prices.csv
│   └── ...
├── year_2026_threshold_0/
└── demand/                         # from demand/
```

Other groups (for example `mga/` or `system_configuration/`) are not exported; read them directly with `h5py`.

### Excel Export

One workbook with a `Summary` sheet (the `summary_results` datasets, one column each) and a `Generation` sheet for the **first** simulated year only.

### JSON Export

The file attributes (`metadata`), the `summary_results` datasets (`summary`), and the attributes of each year group (`scenarios`). Hourly time series are not included; use CSV or read the HDF5 file directly.


---


## `esfex studio`

Opens the GIS-based Studio (PySide6 with a Leaflet.js / OpenStreetMap map) for building and editing configurations.

```bash
esfex studio [OPTIONS]
```

PySide6 is a core dependency, so no extra installation is needed.

### Options

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--config` | `-c` | Path | none | Existing YAML configuration to open |
| `--output` | `-o` | Path | none | Currently has no effect (see note) |

!!! note
    The configuration is saved from inside the Studio (File → Save / Save As). `-o/--output` is not passed to the Studio, so it does not change where the file is written.

### Examples

```bash
# Start from an empty map
esfex studio

# Open an existing configuration
esfex studio -c island_system.yaml
```

### Console Output

```
ESFEX - Studio
Loading: island_system.yaml
```

When the window closes, the CLI prints `Configuration saved to: <path>` if the configuration was saved during the session, or `Studio closed without saving.` otherwise. The printed path is the `-c` (or `-o`) argument, not necessarily the file you chose in the Save dialog.

See the [GUI Editor](../gui/overview.md) pages for what the Studio can do.


---


## `esfex precompile`

Builds a Julia sysimage (`ESFEX.so`, `.dll` or `.dylib` in the package's `julia/` directory) containing JuMP, HiGHS and the ESFEX Julia code, which shortens Julia startup on later runs. The sysimage is picked up automatically.

```bash
esfex precompile [--force]
```

| Option | Short | Description |
|--------|-------|-------------|
| `--force` | `-f` | Rebuild even if an up-to-date sysimage exists |

- The build takes several minutes (the command announces 5–10).
- It runs `julia` as a separate process, so a `julia` executable must be on `PATH`. The Julia that `juliacall` downloads for itself is not on `PATH`; if `which julia` finds nothing, install Julia (e.g. with [juliaup](https://github.com/JuliaLang/juliaup)) first.
- A sysimage counts as **stale** when any `.jl` file under `julia/src/` is newer than it. A stale sysimage is still used, but `esfex precompile` rebuilds it without `--force`, and `esfex info` reports it.

Exits with code `1` if the build fails.


---


## `esfex info`

Shows the ESFEX and Python versions, whether `juliacall` can be imported, the sysimage status, and a quick check of three solvers.

```bash
esfex info
```

### Example Output

```
ESFEX version 0.3.0
Python: 3.12.4 | packaged by conda-forge | (main, Jun 17 2024, 10:23:07) [GCC 12.3.0]
Julia: Available via juliacall
Sysimage: Not built
  Run `esfex precompile` for faster simulation startup

Available solvers (Julia/JuMP):
  HiGHS: Available (v1.23.0)
  Gurobi: Not found
  CPLEX: Not found
```

The `Sysimage:` line is `UP TO DATE (<path>, <size> MB)`, `STALE (<path>, <size> MB)` with a hint to run `esfex precompile --force`, or `Not built`.

`info` starts Julia and loads the ESFEX module to check HiGHS, Gurobi and CPLEX, so on a fresh installation it also triggers the first-time Julia setup and compilation. For the complete list of solvers, use [`esfex list-solvers`](#esfex-list-solvers).

If `juliacall` cannot be imported:

```
Julia: Not available (juliacall not installed)
...
Available solvers (Julia/JuMP):
  Julia not available - cannot check solvers
```


---


## `esfex add-solver`

Installs an optional solver into ESFEX's Julia environment.

```bash
esfex add-solver NAME
```

`NAME` is one of `gurobi`, `cplex`, `cbc`, `scip`, `xpress`. HiGHS, GLPK, Ipopt, Clarabel and SCS ship with ESFEX and need no installation.

The solver's Julia package is added to a dedicated environment, `<depot>/environments/esfex_solvers`, where `<depot>` is the first entry of `JULIA_DEPOT_PATH` or `~/.julia`. ESFEX puts this environment on Julia's `LOAD_PATH` at startup, so the solver is found on the next run and survives ESFEX upgrades. After installing, the command imports the package once, so build or licence problems are reported here rather than during a solve.

Gurobi and CPLEX need the vendor software and a licence to be discoverable while the Julia package builds (for Gurobi, e.g. `GUROBI_HOME` and `GRB_LICENSE_FILE`). CBC and SCIP need no licence.

```bash
esfex add-solver gurobi
```

```
Gurobi.jl installed and verified. Select 'gurobi' as the solver in Studio or your config.
```

Exit codes: `0` on success, `1` if the install or the verification import fails, `2` for an unknown solver name.

!!! note
    Install solvers with `esfex add-solver` rather than `Pkg.add` in a Julia session. A plain `Pkg.add` goes into whatever environment is active in that session, which is usually not one ESFEX loads.


---


## `esfex remove-solver`

Removes a solver previously installed with `esfex add-solver`.

```bash
esfex remove-solver NAME
```

`NAME` is one of `gurobi`, `cplex`, `cbc`, `scip`, `xpress`. Exit codes as for `add-solver`.


---


## `esfex list-solvers`

Shows every solver ESFEX knows and whether it is available.

```bash
esfex list-solvers
```

```
    ESFEX solvers
┏━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━┓
┃ Solver   ┃ Available ┃ Type     ┃
┡━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━┩
│ cbc      │ no        │ optional │
│ clarabel │ yes       │ bundled  │
│ cplex    │ no        │ optional │
│ glpk     │ yes       │ bundled  │
│ gurobi   │ yes       │ optional │
│ highs    │ yes       │ bundled  │
│ ipopt    │ yes       │ bundled  │
│ scip     │ no        │ optional │
│ scs      │ yes       │ bundled  │
│ xpress   │ no        │ optional │
└──────────┴───────────┴──────────┘

Install a commercial solver with: esfex add-solver cplex
```

Availability is read from the `[deps]` of two Julia `Project.toml` files: ESFEX's own (the bundled solvers) and the `esfex_solvers` environment (solvers added with `add-solver`). Julia is not started, so the check is fast, but it shows what is installed, not whether a licence is valid. Python packages such as `gurobipy` play no part in it.


---


## `esfex plugin`

Plugins are directories containing a manifest and Python code; no pip or PyPI integration is involved. The CLI discovers them in:

1. `~/.esfex/plugins/` (user level; `install` puts plugins here)
2. Each directory listed in `ESFEX_PLUGIN_PATH` (separated by `:` on Linux/macOS, `;` on Windows)

A project's own `<project>/.esfex/plugins/` directory is **not** scanned by default: opening someone else's project would otherwise run code it ships. Project-level plugins are loaded only when a caller of the Python API passes `include_project_dir=True` to the plugin manager; the CLI and runner never do. See [Plugin Development](../contributing/plugin-development.md).

### Sub-commands

| Sub-command | Description |
|-------------|-------------|
| `esfex plugin list` | List discovered plugins and their status |
| `esfex plugin install` | Install a plugin from git or a ZIP file |
| `esfex plugin trust NAME` | Trust a plugin's current files so it loads (see [Trusted Plugins](../gui/plugins.md#trusted-plugins)) |
| `esfex plugin uninstall NAME` | Remove a plugin directory |
| `esfex plugin enable NAME` | Enable a disabled plugin |
| `esfex plugin disable NAME` | Disable a plugin without uninstalling it |

### `esfex plugin list`

```bash
esfex plugin list
```

Example output:

```
                         ESFEX Plugins
┏━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Name             ┃ Version ┃ Category ┃ Status   ┃ Description                  ┃
┡━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ availability_gen │ 0.1.0   │ data     │ enabled  │ Generate availability CSVs   │
│ weather_forecast │ 0.2.1   │ data     │ disabled │ Weather-based demand forecast│
└──────────────────┴─────────┴──────────┴──────────┴─────────────────────────────┘
```

A plugin that would be skipped by the trusted list shows `untrusted` after its status; approve it with `esfex plugin trust NAME`. If no plugins are found, the command prints `No plugins found.` followed by the search locations (`~/.esfex/plugins/` and `$ESFEX_PLUGIN_PATH`).

### `esfex plugin install`

| Option | Short | Type | Description |
|--------|-------|------|-------------|
| `--git` | | String | Git URL to clone (`https://` or `git://`) |
| `--zip` | | Path | ZIP file to extract |
| `--name` | `-n` | String | Target directory name (git installs) |

```bash
# Install from GitHub
esfex plugin install --git https://github.com/user/esfex-weather

# Install from GitHub with a custom directory name
esfex plugin install --git https://github.com/user/esfex-weather --name weather_plugin

# Install from a ZIP file
esfex plugin install --zip weather_plugin.zip
```

From the CLI, git installs work only for the hosts github.com, gitlab.com, bitbucket.org, codeberg.org and git.sr.ht; other hosts are refused.

Output on success:

```
Installed plugin: weather_forecast
```

### `esfex plugin uninstall`

```bash
esfex plugin uninstall weather_forecast
```

```
Uninstalled plugin: weather_forecast
```

### `esfex plugin enable` / `disable`

Disabled plugins are not loaded during runs or in the Studio. The setting persists across sessions.

```bash
esfex plugin disable weather_forecast
esfex plugin enable weather_forecast
```

```
Disabled plugin: weather_forecast
Enabled plugin: weather_forecast
```

!!! note
    The plugin API has a `get_cli_commands()` hook, but the `esfex` CLI does not currently register plugin sub-commands.


---


## `esfex train-demand-model`

Downloads World Bank indicators and ERA5 temperature data and trains the XGBoost model used by the Studio's Demand Estimation workflow. The first run downloads about 500 MB (cached).

```bash
esfex train-demand-model [OPTIONS]
```

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--output` | `-o` | Path | `~/.cache/esfex/models/demand_model.xgb` | Where to write the trained model |
| `--cache-dir` | | Path | | Directory for cached downloads |
| `--countries` | `-n` | Integer | `200` | Number of countries in the training data |
| `--year-start` | | Integer | `1990` | First year |
| `--year-end` | | Integer | `2023` | Last year |


---


## `esfex build-demand-dataset`

Downloads hourly electricity demand from public sources plus ERA5 hourly temperature, and writes standardized Parquet files. The first run downloads 5–50 GB depending on the sources (cached).

```bash
esfex build-demand-dataset [OPTIONS]
```

| Option | Short | Type | Default | Description |
|--------|-------|------|---------|-------------|
| `--sources` | `-s` | String | `all` | Comma-separated: `all`, `opsd`, `entsoe`, `brazil`, `colombia`, `japan`, `australia`, `usa`, `rte`, `uk`, `eskom` |
| `--cache-dir` | | Path | | Output directory for the dataset |


---


## Environment Variables

| Variable | Description |
|----------|-------------|
| `ESFEX_PLUGIN_PATH` | Extra plugin directories (`:`-separated on Linux/macOS, `;` on Windows) |
| `JULIA_DEPOT_PATH` | Julia depot; its first entry also decides where the `esfex_solvers` environment lives |
| `JULIA_NUM_THREADS` | Julia threads; ESFEX sets it to `4` if unset. ESFEX's Julia code is not threaded; solver threads come from `solver.threads` |
| `ESFEX_DATA_ROOT` | Dataset and cache root (Geofabrik extracts, OSM tiles, CMIP6 subsets); default `~/.esfex/data` |
| `GUROBI_HOME`, `GRB_LICENSE_FILE` | Gurobi installation and licence, needed when running `esfex add-solver gurobi` and when solving |
| `CPLEX_STUDIO_BINARIES` | CPLEX installation, needed when running `esfex add-solver cplex` |

Shell completion (`--install-completion`) is disabled in the `esfex` CLI.
