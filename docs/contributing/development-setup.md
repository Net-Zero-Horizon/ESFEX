# Development Setup

## Prerequisites

| Requirement | Version | Purpose |
|-------------|---------|---------|
| Python | >= 3.10, < 3.14 | Runtime |
| Julia | >= 1.9 | Optimization backend (managed by `juliacall`; see [Installation](../getting-started/installation.md#julia-setup)) |
| Git | Any recent | Version control |
| pip | >= 22.0 | Package management |

A `julia` executable on `PATH` is needed only to build the sysimage (`esfex precompile`) and to run the Julia test suite directly.


---


## Clone the Repository

```bash
git clone https://github.com/Net-Zero-Horizon/ESFEX.git
cd ESFEX
```


---


## Python Environment

Virtual environment setup:

```bash
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# or
.venv\Scripts\activate     # Windows
```

Install in development mode:

```bash
pip install -e ".[dev]"
```

All runtime dependencies are core dependencies in `pyproject.toml`; there are no optional extras besides `dev`. They include the numerical stack (numpy, pandas, scipy, h5py, pydantic, pyyaml), juliacall, typer and rich, the Studio (PySide6, which bundles Qt WebEngine, and superqt), plotting (matplotlib, plotly, kaleido), SALib, benchmarking tools (pypsa, pandapower), the GIS and resource-assessment stack (geopandas, shapely, pvlib, atlite, rasterio, osmium, xarray, …) and demand-model libraries (xgboost, torch, pytorch-lightning). The install is large (several GB).

The `dev` extra adds pytest, pytest-cov, black, ruff and mypy.

!!! note "Editable installs and pytest"
    CI installs the package non-editable (`pip install ".[dev]"`) because the setuptools editable finder can resolve some sub-modules (for example `esfex.visualization.i18n`) as namespace packages under pytest. If tests fail with "unknown location" import errors, reinstall without `-e`.


---


## Julia Environment

Julia initializes automatically on first run: ESFEX activates its own project in `src/esfex/julia` and installs missing packages. Manual setup:

```bash
# From the project root
julia --project=src/esfex/julia -e 'using Pkg; Pkg.instantiate()'
```

### Julia Dependencies

`src/esfex/julia/Project.toml` lists JuMP, MathOptInterface, the bundled solvers (HiGHS, GLPK, Ipopt, SCS, Clarabel), PowerModels, Graphs, PackageCompiler and a few standard libraries. The Julia tests have their own `src/esfex/julia/test/Project.toml`.

Commercial and other optional solvers are installed with the CLI:

```bash
esfex add-solver gurobi      # gurobi, cplex, cbc, scip, xpress
esfex list-solvers           # what is available
esfex remove-solver gurobi
```

`add-solver` installs the Julia package into a separate environment, `<depot>/environments/esfex_solvers` (default depot `~/.julia`), which ESFEX stacks on Julia's load path. Gurobi and CPLEX also need the vendor toolkit and licence to be discoverable (for example `GUROBI_HOME`). Do not `Pkg.add` solvers into `src/esfex/julia`: that changes the shipped project and is not the supported path.


---


## System Image (Optional)

Build a Julia system image for faster startup:

```bash
esfex precompile           # build if missing or stale
esfex precompile --force   # rebuild
```

Builds `src/esfex/julia/ESFEX.so` (`.dll`/`.dylib` on other platforms) with JuMP, HiGHS and the ESFEX pipeline compiled in. The build takes several minutes and needs `julia` on `PATH`. ESFEX picks the image up automatically on later runs. It becomes stale when a `.jl` file under `src/esfex/julia/src` is newer than the image; a stale image is still used, so rebuild after editing Julia code.


---


## IDE Setup

### VS Code

Recommended extensions:

- **Python** (ms-python.python)
- **Julia** (julialang.language-julia)
- **YAML** (redhat.vscode-yaml)
- **Ruff** (charliermarsh.ruff)

Settings (`.vscode/settings.json`):

```json
{
    "python.defaultInterpreterPath": ".venv/bin/python",
    "python.analysis.typeCheckingMode": "basic",
    "[python]": {
        "editor.defaultFormatter": "charliermarsh.ruff"
    },
    "julia.executablePath": "/usr/local/bin/julia"
}
```

#### Debugging Configuration

`.vscode/launch.json` debugging configurations:

```json
{
    "version": "0.2.0",
    "configurations": [
        {
            "name": "ESFEX: Run Simulation",
            "type": "debugpy",
            "request": "launch",
            "module": "esfex.cli",
            "args": ["run", "-c", "${workspaceFolder}/my_system.yaml", "--years", "1", "-v"],
            "cwd": "${workspaceFolder}",
            "env": {
                "JULIA_NUM_THREADS": "4"
            },
            "justMyCode": false
        },
        {
            "name": "ESFEX: GUI Editor",
            "type": "debugpy",
            "request": "launch",
            "module": "esfex.cli",
            "args": ["studio", "-c", "${workspaceFolder}/my_system.yaml"],
            "cwd": "${workspaceFolder}",
            "justMyCode": false
        },
        {
            "name": "ESFEX: Run Tests",
            "type": "debugpy",
            "request": "launch",
            "module": "pytest",
            "args": [
                "tests/",
                "-v",
                "--tb=short",
                "-x"
            ],
            "cwd": "${workspaceFolder}",
            "justMyCode": false
        },
        {
            "name": "ESFEX: Debug Single Test",
            "type": "debugpy",
            "request": "launch",
            "module": "pytest",
            "args": [
                "${file}",
                "-v",
                "--tb=long",
                "-x",
                "--no-header"
            ],
            "cwd": "${workspaceFolder}",
            "justMyCode": false
        }
    ]
}
```

`my_system.yaml` stands for any configuration, for example the one from the [Quickstart](../getting-started/quickstart.md). The repository has no example-config directory. There is no `sensitivity` CLI command; sensitivity analysis runs from the Studio or the Python API (see [Sensitivity Analysis](../tutorials/sensitivity-analysis.md)).

Set breakpoints in `runner.py` or `adapters.py` to inspect the simulation loop. `justMyCode: false` enables stepping into library code (JuliaCall, Pydantic, etc.).

#### Tasks Configuration

`.vscode/tasks.json` for common commands:

```json
{
    "version": "2.0.0",
    "tasks": [
        {
            "label": "Lint (ruff)",
            "type": "shell",
            "command": "ruff check src/ tests/",
            "problemMatcher": []
        },
        {
            "label": "Format (ruff)",
            "type": "shell",
            "command": "ruff format src/ tests/",
            "problemMatcher": []
        },
        {
            "label": "Type check (mypy)",
            "type": "shell",
            "command": "mypy src/esfex/",
            "problemMatcher": []
        },
        {
            "label": "Build Julia sysimage",
            "type": "shell",
            "command": "esfex precompile --force",
            "problemMatcher": []
        }
    ]
}
```

### PyCharm

1. Set Python interpreter to `.venv/bin/python`
2. Install Julia plugin for `.jl` file support
3. Configure Ruff as the external formatter
4. Create a Run Configuration for `esfex.cli` with module mode


---


## Project Structure

Main directories (not every file is shown):

```
ESFEX/
├── src/esfex/
│   ├── __init__.py, __main__.py # Package exports; `python -m esfex`
│   ├── cli.py                   # CLI (Typer)
│   ├── runner.py                # Simulation orchestrator, HDF5 output
│   ├── zones.py                 # Development zones
│   ├── paths.py                 # Dataset paths (ESFEX_DATA_ROOT)
│   ├── logging_config.py
│   ├── config/                  # schema.py (Pydantic), loader.py (YAML), solver.py
│   ├── bridge/                  # adapters.py (Python→Julia), converters.py,
│   │                            # julia_setup.py, topology_audit.py
│   ├── analysis/                # N-1, AC contingency, frequency, pandapower/native AC bridges
│   ├── topology/                # Network reduction and result expansion
│   ├── io/                      # demand.py, exporter.py
│   ├── models/                  # EV, rooftop PV, demand models, TSAM, climate, finance, …
│   ├── plugins/                 # protocol.py, manager.py, availability_generator/ (built-in)
│   ├── sensitivity/             # engine.py (Sobol), worker.py, lp_parser.py
│   ├── utils/                   # helpers.py, temporal.py, paths.py
│   ├── julia/
│   │   ├── Project.toml         # Julia dependencies
│   │   ├── build_sysimage.jl, precompile_workload.jl
│   │   ├── src/                 # ESFEX.jl and the model files (see Julia Development)
│   │   └── test/                # runtests.jl, Project.toml
│   └── visualization/           # Studio
│       ├── app.py, main_window.py, map_widget.py, sld_widget.py, …
│       ├── data/                # gui_model.py, serializer.py, …
│       ├── panels/              # Property forms and dialogs
│       ├── workflows/           # Analysis wizards (Grid Builder, solar, wind, …)
│       ├── translations/        # i18n JSON files
│       └── resources/           # HTML, CSS, JS assets
├── tests/                       # pytest suite (test_schema.py, test_runner.py, …)
├── examples/plugins/            # Example plugin (custom constraint overlay)
├── docs/                        # Documentation (MkDocs)
├── pyproject.toml               # Package configuration
└── mkdocs.yml                   # Documentation config
```


---


## Code Style

### Python

ESFEX uses **Ruff** for linting and formatting:

```bash
# Lint
ruff check src/

# Format
ruff format src/

# Auto-fix
ruff check --fix src/
```

Key style rules (from `[tool.ruff]` in `pyproject.toml`):

- Line length: 100 characters (`E501` is not enforced)
- Rule sets: `E`, `F`, `I` (import sorting), `N`, `W`
- Docstrings: Google style
- Type hints: encouraged for public API

The codebase is not yet ruff-clean; CI runs `ruff check --exit-zero`, so lint findings are reported but do not fail the build. There is no pre-commit configuration in the repository.

### Julia

Julia code follows standard conventions:

- 4-space indentation
- `snake_case` for functions and variables
- `PascalCase` for types
- `UPPER_CASE` for constants
- Document functions with docstrings


---


## Common Development Workflows

### Adding a New Configuration Option

1. Add the Pydantic field in `src/esfex/config/schema.py`
2. Set a sensible default value so existing configs remain valid
3. Wire the field in `runner.py` or the relevant adapter
4. Add a test in `tests/test_schema.py` for both the default and explicit cases
5. If the loader converts the section by hand (`config/loader.py`), make sure the new key is passed through
6. Document the field in the configuration reference and any user-guide page that shows the section

```python
# schema.py — example
class PenaltiesConfig(BaseModel):
    loss_of_load: float = 1e7
    max_curtailment_ratio: float = 0.05
    my_new_penalty: float = 1000.0  # Add with default
```

### Modifying the Julia Backend

1. Edit the relevant `.jl` file (see [Julia Development](julia-development.md))
2. Test from the Julia REPL first to catch syntax errors immediately
3. Run the Python integration through the adapter
4. Check that the Python-Julia bridge serializes new fields correctly

### Running a Quick End-to-End Test

```bash
# Run a minimal 1-year simulation (e.g. the Quickstart config)
esfex run -c my_system.yaml --years 1 -o results

# Results go to results/<config_stem>_<ddmmyyyy>_<HHMM>.h5
ls results/
python -c "import glob, h5py; f = h5py.File(sorted(glob.glob('results/my_system_*.h5'))[-1], 'r'); print(list(f.keys()))"
```

`esfex run -c my_system.yaml --dry-run -v` loads the config and prints the run plan without solving.

### Debugging Julia from Python

Julia error tracebacks can be opaque when propagated to Python. Useful aids:

- The runner writes LP files to `<output>/logs/`: `master_problem_debug.lp` for every master-problem build, and `window_infeasible_y<year>_w<window>.lp` when a dispatch window is not optimal.
- `PowerSystemAdapter.write_lp(path)` and `MasterProblemAdapter.write_lp(path)` export a built model.
- With Gurobi, `ESFEX_DUMP_IIS=1` computes an irreducible infeasible subsystem for the first infeasible model and writes it to `ESFEX_IIS_DIR` (default `/tmp`).
- `--verbose` raises the console log level. Each run also writes `<output>/logs/optimization_<timestamp>.log`.

```python
from esfex.bridge.julia_setup import get_esfex_module

ESFEX = get_esfex_module()
model, vars = ESFEX.create_power_system(jl_input)   # jl_input built by the adapter
```

### Working with HDF5 Output

```python
import glob
import h5py

path = sorted(glob.glob("results/my_system_*.h5"))[-1]   # newest run
with h5py.File(path, "r") as f:
    # List all groups and datasets
    f.visit(print)

    # Per-year results: detailed_results/year_<YYYY>_threshold_0/
    year_key = sorted(f["detailed_results"])[0]
    gen_grp = f[f"detailed_results/{year_key}/generation"]
    for name in gen_grp:                       # one dataset per generator
        data = gen_grp[name][:]                # [nodes x hours], MW
        print(name, data.shape, f"{data.sum():.1f} MWh")
```

See the [HDF5 output schema](../reference/hdf5-output-schema.md) for the full layout.


---


## Environment Variables

Julia-related variables (`JULIA_NUM_THREADS`, `JULIA_DEPOT_PATH`, juliacall options) are described in [Installation](../getting-started/installation.md#environment-variables). ESFEX sets `JULIA_NUM_THREADS` to 4 and `PYTHON_JULIACALL_SYSIMAGE` to the sysimage path when they are unset. The solver is chosen with `--solver` or `solver.name`, and the console log level with `--verbose` or `logging.console_level`; there are no environment variables for these.

ESFEX's own variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `ESFEX_PLUGIN_PATH` | Extra plugin search directories (`os.pathsep`-separated) | unset |
| `ESFEX_DUMP_IIS` | `1` = write the IIS of the first infeasible model (Gurobi only) | unset |
| `ESFEX_IIS_DIR` | Directory for the IIS / infeasible LP file | `/tmp` |
| `ESFEX_OPS_ONLY_YEARS` | Debug: run operational dispatch only for these years (comma- or space-separated) | unset |
| `ESFEX_DATA_ROOT` | Dataset root (`esfex.paths`): demand-model training data and the caches the Studio writes (Geofabrik extracts, OSM tiles, CMIP6 subsets); `~` is expanded | `~/.esfex/data` |
| `ESFEX_XGB_DEVICE` | XGBoost device for demand models: `auto`, `cpu`, `cuda` | `auto` |
| `ESFEX_CMIP6_MODEL` | CMIP6 model used for climate projections | `MPI-ESM1-2-HR` |
| `ESFEX_OSM_BACKEND` | Grid Builder OSM source: `auto`, `geofabrik`, `overpass` | `auto` |
| `ESFEX_AVAILABILITY_WORKERS` | Parallel weather fetches when generating availability profiles | 4 |


---


## Troubleshooting

### Julia Not Found

```bash
# Check Julia is in PATH
which julia
julia --version

# If using pyenv/conda, ensure Julia is accessible
export PATH="$HOME/.julia/juliaup/bin:$PATH"
```

### PySide6 Issues (Linux)

```bash
# Install system dependencies
sudo apt install libxcb-xinerama0 libxkbcommon-x11-0
# Or for Qt WebEngine:
sudo apt install libnss3 libxcomposite1 libxdamage1 libxrandr2
```

### Solver Licensing

Commercial solvers require license files:

```bash
# Gurobi
export GRB_LICENSE_FILE=/path/to/gurobi.lic

# CPLEX
export CPLEX_STUDIO_DIR=/opt/ibm/ILOG/CPLEX_Studio
```

### JuliaCall Import Errors

Troubleshooting `juliacall` import or connection failures:

```bash
# Check that juliacall can start Julia
python -c "import juliacall; print(juliacall.Main)"

# Check that ESFEX's Julia module loads
esfex info
```

See [Installation: Troubleshooting](../getting-started/installation.md#troubleshooting) for package and environment errors.

### Out-of-Memory During Simulation

Mitigation strategies for large multi-node, multi-year simulations:

- Use a shorter rolling-horizon window (`temporal.rolling_horizon_hours`, default 48) or a coarser `temporal.resolution_hours`.
- Reduce the network with network reduction, or use fewer representative days in the master problem.
