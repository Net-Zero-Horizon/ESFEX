# Installation


## Prerequisites

- **Python 3.10, 3.11, 3.12 or 3.13**.
- **pip 21.0+** or **conda** package manager.
- **Julia 1.9+**: managed by `juliacall`. If a suitable Julia is on your `PATH` it is used; otherwise `juliacall` downloads one on first use. A `julia` executable on `PATH` is needed only for [`esfex precompile`](../user-guide/cli.md#esfex-precompile).
- **At least 4 GB of free RAM** for typical simulations. Large multi-node, multi-year studies may require 8--16 GB.
- **Several GB of free disk space**: the Python dependencies include the GIS stack and Qt WebEngine, and the Julia environment, compiled packages and an optional sysimage add more.

### Checking Python Version

```bash
python --version
# Expected: Python 3.10.x, 3.11.x, 3.12.x or 3.13.x
```

If `python` points to Python 2, try `python3 --version` instead. On Windows, you may also use `py -3 --version`.

## Basic Installation

Install the package from PyPI:

```bash
pip install esfex
```

All runtime functionality ships as **core dependencies**; there are no feature extras to choose. The core dependencies include:

| Package | Purpose |
|---------|---------|
| NumPy, Pandas, SciPy | Arrays, tables and numerical utilities |
| h5py | HDF5 results files |
| Pydantic, PyYAML | Configuration schema and YAML parsing |
| Typer, Rich | Command-line interface and terminal output |
| juliacall | Python-Julia bridge |
| NetworkX | Network topology analysis |
| timezonefinder | Node time zones from coordinates (the [model clock](../user-guide/configuration.md#model-clock)) |
| OpenPyXL | Excel demand files and Excel export |
| psutil | System resource information |
| PySide6, superqt, markdown | The Studio (GUI) |
| Matplotlib, Plotly, Kaleido | Result charts |
| SALib | Sobol sensitivity analysis |
| windrex, solarex, rooftex, evrex, otex | Companion packages for wind, solar PV, rooftop solar, EV and OTEC workflows |
| pvlib, geopandas, rasterio, atlite, duckdb, shapely, osmium, overpy, xarray, gcsfs, zarr, scikit-learn | Resource-assessment and grid-mapping workflows |
| pypsa, pandapower, pypower, matpower | Benchmarking and AC power-flow analysis |
| xgboost | Demand-estimation models |

The full list is in `pyproject.toml`. Because of the GIS stack and Qt WebEngine, the installation is large.

### Conda / Mamba

`environment.yml` in the repository creates an environment where conda-forge supplies the native dependencies (Qt, the Julia bridge, HDF5, the GIS stack) and ESFEX is installed from PyPI on top:

```bash
conda env create -f environment.yml   # or: mamba env create -f environment.yml
conda activate esfex
esfex info
```

!!! note
    `environment.yml` allows the same Python versions as pip (3.10–3.13).

## Development Installation

For contributing to ESFEX or running from source:

```bash
# Clone the repository
git clone https://github.com/Net-Zero-Horizon/ESFEX.git
cd ESFEX

# Create a virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate    # Windows

# Install in editable mode with development dependencies
pip install -e ".[dev]"
```

The `dev` extras include:

| Package | Purpose |
|---------|---------|
| pytest >= 7.0 | Test runner |
| pytest-cov >= 4.0 | Code coverage reporting |
| black >= 23.0 | Code formatting |
| ruff >= 0.1 | Fast linting |
| mypy >= 1.0 | Static type checking |

### Running Tests

```bash
# Run all tests
pytest

# Run tests with coverage report
pytest --cov=esfex --cov-report=html

# Skip tests that require Julia (faster feedback during Python-only changes)
pytest -m "not julia"
```

## Julia Setup

ESFEX uses Julia for its optimization backend through the `juliacall` Python package. Julia management is automatic.

### First Run Behavior

1. **Julia installation**: `juliacall` (through `juliapkg`) uses a compatible Julia from your `PATH`, or downloads one into its own environment.
2. **ESFEX's Julia project**: ESFEX activates its own Julia project, the `julia/` folder inside the installed `esfex` package, and installs the packages listed in its `Project.toml` the first time they are missing.
3. **Compilation**: Julia compiles the packages and the ESFEX module on first use, which takes several minutes.
4. **Subsequent runs**: the compiled packages are cached and startup is faster. A sysimage built with `esfex precompile` makes it faster still.

To trigger the first-run setup without running a simulation:

```bash
esfex info
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `JULIA_NUM_THREADS` | `4` | Julia threads. ESFEX sets it to 4 when it is not already set. ESFEX's own Julia code does not run threaded, so this does not speed up a run; the solver's threads are `solver.threads`. |
| `PYTHON_JULIACALL_THREADS` | unset | juliacall option; when set, it is passed to Julia as `--threads` and takes precedence over `JULIA_NUM_THREADS`. |
| `JULIA_DEPOT_PATH` | `~/.julia` | Where Julia stores packages and compiled files. Its first entry also decides where the optional-solver environment `environments/esfex_solvers` lives. |
| `ESFEX_JULIA_RESOLVE` | unset | Set to `1` to let ESFEX re-resolve its Julia project (`Pkg.resolve()`) when `Pkg.instantiate()` cannot install missing packages. Off by default: ESFEX never changes the recorded package versions on its own. See [Julia Package Errors](#julia-package-errors). |
| `PYTHON_JULIACALL_HANDLE_SIGNALS` | unset | juliacall option. When unset, Julia's signal handling is off and juliacall warns if Julia runs with more than one thread; set `yes` or `no` explicitly to silence the warning. |

To use more threads on a large model, raise `solver.threads` in the configuration (see the [Solver Guide](../user-guide/solver-guide.md)); the solve is where the time goes.

### Julia Packages

ESFEX's Julia project (`src/esfex/julia/Project.toml`) depends on:

- **JuMP** (compat 1.15) and **MathOptInterface**: optimization modeling
- **HiGHS**, **GLPK**: LP/MIP solvers
- **Clarabel**, **SCS**: conic solvers for the AC-OPF relaxations
- **Ipopt**: nonlinear solver for the exact AC-OPF formulations
- **Graphs**: network topology analysis
- **LinearAlgebra**, **SparseArrays**, **Statistics**, **Dates**: matrix operations and utilities
- **PackageCompiler**: used by `esfex precompile` to build the sysimage
- **PowerModels**, **Tables**: also declared in `Project.toml`

## Solver Setup

### Bundled Solvers

HiGHS (the default), GLPK, Clarabel, SCS and Ipopt are part of ESFEX's Julia project and need no setup. HiGHS handles LP, MIP and QP and is the right choice for most studies.

### Optional Solvers

Gurobi, CPLEX, Xpress, CBC and SCIP are installed on demand with `esfex add-solver`, which puts the Julia package into a dedicated environment that ESFEX loads automatically and that survives ESFEX upgrades. Select a solver with the `--solver` CLI flag or the `solver.name` configuration field.

=== "Gurobi"

    Gurobi is a commercial LP/MIP solver; academic licences are free.

    1. Install Gurobi and obtain a licence from [gurobi.com](https://www.gurobi.com/).
    2. Make sure `GUROBI_HOME` and, if the licence is not in the default location, `GRB_LICENSE_FILE` are set.
    3. Install the solver into ESFEX:
    ```bash
    esfex add-solver gurobi
    ```
    4. Run ESFEX with:
    ```bash
    esfex run -c config.yaml -s gurobi
    ```

=== "CPLEX"

    IBM ILOG CPLEX is available through academic initiatives or commercial licences.

    1. Install IBM ILOG CPLEX from [ibm.com](https://www.ibm.com/products/ilog-cplex-optimization-studio).
    2. Set `CPLEX_STUDIO_BINARIES` to the CPLEX binary directory.
    3. Install the solver into ESFEX:
    ```bash
    esfex add-solver cplex
    ```
    4. Run ESFEX with:
    ```bash
    esfex run -c config.yaml -s cplex
    ```

=== "Xpress"

    FICO Xpress is a commercial LP/MIP solver.

    1. Install Xpress from [fico.com](https://www.fico.com/en/products/fico-xpress-optimization) and activate the licence.
    2. Install the solver into ESFEX:
    ```bash
    esfex add-solver xpress
    ```

=== "CBC / SCIP"

    Free open-source MIP solvers; no licence needed. CBC is usually slower than HiGHS.

    ```bash
    esfex add-solver cbc
    esfex add-solver scip
    ```
    Run with:
    ```bash
    esfex run -c config.yaml -s cbc
    ```

Check what is available with `esfex list-solvers`; remove a solver with `esfex remove-solver <name>`. See the [Solver Guide](../user-guide/solver-guide.md) for which solvers suit which formulation.

!!! note
    Do not install solvers with `Pkg.add(...)` in a Julia session. That installs into whichever Julia environment is active in that session, which ESFEX does not load.

## Verifying Installation

```bash
esfex info
```

Expected output for a fresh installation:

```
ESFEX version 0.4.0
Python: 3.12.4 | packaged by conda-forge | (main, Jun 17 2024, 10:23:07) [GCC 12.3.0]
Julia: Available via juliacall
Sysimage: Not built
  Run `esfex precompile` for faster simulation startup

Available solvers (Julia/JuMP):
  HiGHS: Available (v1.23.0)
  Gurobi: Not found
  CPLEX: Not found
```

`esfex info` starts Julia and loads the ESFEX module, so a successful run confirms that the Julia backend works. It checks only HiGHS, Gurobi and CPLEX; `esfex list-solvers` shows all ten solvers.

You can also check the installed version from Python:

```bash
python -c "import esfex; print(esfex.__version__)"
# Expected: 0.4.0
```

To check that a configuration file loads, use `esfex validate -c <file>`. It does not start Julia (see the [CLI reference](../user-guide/cli.md#esfex-validate)).

### Optional: Build a Sysimage

```bash
esfex precompile
```

Builds a Julia sysimage that shortens startup on later runs. It takes several minutes and needs a `julia` executable on `PATH` (the Julia downloaded by `juliacall` is not on `PATH`). Rebuild with `esfex precompile --force` after changing Julia source files; `esfex info` reports a stale sysimage.

## Platform-Specific Notes

### Linux (Ubuntu / Debian)

Linux is the recommended platform for ESFEX. No special configuration is required for the core package.

**GUI dependencies**: The PySide6-based Studio requires X11 or Wayland display libraries. If the Studio fails to launch:

```bash
# Ubuntu / Debian
sudo apt install libxcb-xinerama0 libxkbcommon-x11-0 libegl1 libxcb-cursor0

# Fedora / RHEL
sudo dnf install libxcb libxkbcommon-x11 mesa-libEGL
```

**Headless servers**: The Studio is not needed for batch simulations; `esfex run` works without a display.

### macOS

ESFEX works on macOS 12 (Monterey) and newer, on both Intel and Apple Silicon hardware.

**Julia on Apple Silicon**: `juliacall` handles architecture selection automatically. If you encounter issues, make sure you are using a native ARM64 Python build (not Rosetta-emulated x86):

```bash
python -c "import platform; print(platform.machine())"
# Expected on Apple Silicon: arm64
```

**PySide6 on macOS**: The GUI may prompt for accessibility permissions on first launch. Grant these permissions in System Settings > Privacy & Security > Accessibility.

### Windows

ESFEX runs on Windows 10 and Windows 11. Use either the standard Python installer from [python.org](https://www.python.org/downloads/windows/) or Anaconda/Miniconda.

!!! warning "Do not use the Microsoft Store Python"
    Install Python from python.org or conda — **not** from the Microsoft Store.
    The Store build runs in an app sandbox with a redirected filesystem and
    restricted DLL search, which breaks native extension loading and manifests
    as `ImportError: DLL load failed while importing QtWidgets` when launching
    the Studio. If you already installed it that way, uninstall the Store Python
    and reinstall from python.org, then recreate your environment.

**Launching**: if the `esfex` command is "not recognized", the environment's `Scripts\` folder is not on `PATH`. Run `python -m esfex <command>` instead, or activate the virtual environment. `pip` also installs `esfex-studio`, which opens the Studio without a console window.

**UTF-8 encoding**: ESFEX automatically configures UTF-8 output on Windows. If you still encounter encoding errors in your terminal, set the environment variable before running:

```powershell
$env:PYTHONIOENCODING = "utf-8"
```

Or in Command Prompt:

```cmd
set PYTHONIOENCODING=utf-8
```

**Long path support**: On Windows, enable long path support if you encounter path length errors during Julia package installation:

1. Open the Group Policy Editor (`gpedit.msc`)
2. Navigate to Computer Configuration > Administrative Templates > System > Filesystem
3. Enable "Enable Win32 long paths"

Alternatively, set the registry key:

```powershell
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name "LongPathsEnabled" -Value 1 -PropertyType DWORD -Force
```

**Windows Subsystem for Linux (WSL)**: Running ESFEX inside WSL2 with Ubuntu provides the same experience as native Linux. Follow the Linux instructions after setting up WSL.

---


## Troubleshooting

Two Julia environments are involved, and it helps to know where they are:

- **ESFEX's Julia project**: the `julia/` folder of the installed package. Print it with `python -c "from esfex.bridge.julia_setup import get_julia_path; print(get_julia_path())"`.
- **juliacall's own environment** (where a downloaded Julia and juliacall's packages live): print it with `python -c "import juliapkg; print(juliapkg.project())"`. In a virtual or conda environment it is inside that environment (e.g. `<env>/julia_env`); otherwise it is under `~/.julia/environments/`.

### Julia Package Errors

If loading the ESFEX module fails because packages are missing or not installed, ESFEX runs `Pkg.instantiate()` on its project automatically, after copying `Manifest.toml` to `Manifest.toml.bak-<timestamp>` (the copy is removed if the Manifest does not change). ESFEX never deletes `Manifest.toml`. If `Pkg.instantiate()` fails, ESFEX stops with an error; set `ESFEX_JULIA_RESOLVE=1` and run again to let it re-resolve the project with `Pkg.resolve()`, which may pick other package versions. Any other load error, such as a syntax error in a `.jl` file, is reported with its file and line and no repair is attempted.

To repair the project by hand with juliacall's Julia:

```bash
python -c "
from juliacall import Main as jl
from esfex.bridge.julia_setup import get_julia_path
jl.seval(f'using Pkg; Pkg.activate(\"{get_julia_path().as_posix()}\"); Pkg.instantiate()')
"
esfex info
```

If the error persists, clearing the Julia depot forces a full reinstall of all Julia packages. This also removes optional solvers installed with `esfex add-solver`:

```bash
rm -rf ~/.julia
esfex info
```

### Julia Version Mismatch

At startup ESFEX warns when the project's `Manifest.toml` was resolved with a different Julia minor version than the one juliacall runs, for example:

```text
ESFEX's Julia Manifest.toml (...) was resolved with Julia 1.12.4, but juliacall runs Julia 1.11.8.
```

This happens when the Manifest was written by the `julia` on your `PATH` (for example by running the Julia tests) while juliacall uses another Julia. A Manifest resolved by one Julia version may fail to load or install on another. Use one Julia for both: point juliacall at the same Julia with `PYTHON_JULIAPKG_EXE=/path/to/julia`. Or, if packages fail to load, move `Manifest.toml` aside (keep it) and run the repair command above, so that juliacall's Julia resolves a new Manifest for its own version.

### juliacall Hangs on First Run

The first package installation and precompilation can run for several minutes with no output. This is expected on the first invocation. If it takes much longer, check for:

- Insufficient disk space
- Antivirus software blocking Julia compilation (common on Windows)
- Network connectivity issues (Julia downloads packages from the internet on first run)

### PySide6 Platform Plugin (Linux)

If the Studio fails to launch on Linux with an error like `qt.qpa.plugin: Could not load the Qt platform plugin "xcb"`:

```bash
# Install required Qt platform dependencies
sudo apt install libxcb-xinerama0 libxkbcommon-x11-0 libegl1 libxcb-cursor0

# If the issue persists, try setting the platform explicitly
export QT_QPA_PLATFORM=xcb
esfex studio
```

### PySide6 Wayland Issues (Linux)

On Wayland-based desktops (e.g., GNOME on Fedora), PySide6 may have rendering issues. Force X11 mode:

```bash
export QT_QPA_PLATFORM=xcb
esfex studio
```

### HiGHS Solver Not Found

HiGHS is a dependency of ESFEX's Julia project. If `esfex info` reports it as not available, the project is incomplete; repair it as described under [Julia Package Errors](#julia-package-errors). Adding HiGHS with `Pkg.add` in another environment does not help.

### Optional Solver Not Found

If `esfex list-solvers` shows a solver you installed as unavailable, check that you installed it with `esfex add-solver` and that `JULIA_DEPOT_PATH` is the same as when you installed it (the `esfex_solvers` environment lives in the first depot). A licence problem shows up when `esfex add-solver` verifies the package, and in `esfex info` for Gurobi and CPLEX.

### Out of Memory During Optimization

Large models (many nodes, many years, fine temporal resolution) can consume significant memory. Options:

1. **Use shorter rolling-horizon windows** (`temporal.rolling_horizon_hours`) -- smaller subproblems
2. **Reduce the number of representative days** in the master problem (`master_problem.representative_days`)
3. **Use a coarser temporal resolution** (`temporal.resolution_hours: 2` or higher)
4. **Reduce the number of simulation years** (`esfex run --years`) as a first test

### Import Errors After Upgrade

If you upgrade ESFEX and encounter import errors:

```bash
# Reinstall with dependencies
pip install --force-reinstall esfex
```

Optional solvers installed with `esfex add-solver` are kept across upgrades.

### Firewall Blocking Julia Package Downloads

If Julia cannot download packages on first run due to firewall restrictions, you can pre-install the Julia packages on a machine with internet access and copy the Julia depot (`~/.julia`, or the path in `JULIA_DEPOT_PATH`) to the target machine.

## Next Steps

- [Quickstart](quickstart.md) — run your first simulation
- [Core Concepts](concepts.md) — understand the optimization model
- [Configuration Guide](../user-guide/configuration.md) — full YAML reference
