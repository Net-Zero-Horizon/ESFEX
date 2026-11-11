# Julia Setup

Module: `esfex.bridge.julia_setup`

## Architecture

The module uses two global singletons to cache the Julia runtime and the loaded ESFEX module:

```python
_julia_instance = None   # juliacall Main module handle
_esfex_module = None     # ESFEX Julia module reference
```

On first access (via `get_julia()` or any adapter constructor), the module:

1. Points juliacall at the ESFEX sysimage, if one has been built (see [Sysimage](#sysimage)).
2. Imports `juliacall.Main` as the Julia runtime handle.
3. Activates the ESFEX Julia project environment located at `src/esfex/julia/` and stacks the optional-solvers environment on `LOAD_PATH` (see [Optional solvers](#optional-solvers)).
4. Includes and loads the `ESFEX.jl` module.
5. Caches both references for subsequent calls.

Subsequent calls return the cached instances immediately, with no reinitialization overhead.

---

## Functions

### initialize_julia

```python
def initialize_julia(
    threads: int = 4,
    compile: bool = True,
    verbose: bool = False,
) -> Any
```

Initialize the Julia runtime and load the ESFEX module.

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `threads` | `int` | `4` | Number of Julia threads. Sets `JULIA_NUM_THREADS` environment variable. |
| `compile` | `bool` | `True` | Accepted for compatibility; currently not used. |
| `verbose` | `bool` | `False` | Enable verbose output during initialization. |

**Returns:** The `juliacall.Main` module with ESFEX loaded.

**Raises:**
- `ImportError` -- if `juliacall` is not installed.
- `RuntimeError` -- if Julia initialization or module loading fails.

**Initialization Steps:**

1. Sets `JULIA_NUM_THREADS` environment variable (uses `setdefault` to avoid overriding user-set values).
2. Looks for a sysimage `src/esfex/julia/ESFEX.{so,dll,dylib}`. If found, sets `PYTHON_JULIACALL_SYSIMAGE` to it (unless already set). A sysimage older than any `src/*.jl` file is still used, with a log message suggesting `esfex precompile`.
3. Imports `juliacall` and resolves the Julia project path: `src/esfex/julia/Project.toml`.
4. Activates the Julia project via `Pkg.activate()` and pushes the optional-solvers environment ([`get_solvers_env_path()`](#get_solvers_env_path)) onto `LOAD_PATH`.
5. Attempts to load the ESFEX module via `include()` and `using .ESFEX`.
6. If loading fails with "invalid redefinition of constant", reuses the already-loaded module (happens during Julia session reuse).
7. If loading fails with a `LoadError` or `ArgumentError`, runs `Pkg.instantiate()` to install missing dependencies, then retries loading.
8. If `Pkg.instantiate()` fails, deletes `Manifest.toml` and retries with `Pkg.resolve(); Pkg.instantiate()`.
9. Forwards Julia's log records to Python logging (`ESFEX.set_log_callback`): `@debug`/`@info`/`@warn`/`@error` reach the `esfex.julia` logger at the same level, so they appear on the console and in the run's log file like every other ESFEX message. Benders progress lines are milestones, shown by the `basic` console too. Debug records come only from ESFEX itself.
10. Tries `import` for each optional solver package (Clarabel, Ipopt, SCS, GLPK, Cbc, SCIP, Gurobi, CPLEX, Xpress) so that their methods exist in the current world age; missing packages are skipped.

**Example:**

```python
from esfex.bridge.julia_setup import initialize_julia

jl = initialize_julia(threads=8, verbose=True)
# Julia runtime is now ready
```

### get_julia

```python
def get_julia() -> Any
```

Get the Julia runtime instance. Initializes Julia on first call, returns cached instance thereafter. Safe to call repeatedly.

**Returns:** The `juliacall.Main` module with ESFEX loaded.

### get_esfex_module

```python
def get_esfex_module() -> Any
```

Get the ESFEX Julia module reference.

**Returns:** The `ESFEX` Julia module (equivalent to `jl.ESFEX`).

**Raises:** `RuntimeError` if Julia is not initialized and initialization fails.

Used by adapter classes to call Julia functions:

```python
ESFEX = get_esfex_module()
model, vars = ESFEX.create_power_system(input)
```

### get_julia_path

```python
def get_julia_path() -> Path
```

Get the path to the Julia source directory.

**Returns:** `Path` pointing to `src/esfex/julia/`.

Contents:
- `Project.toml` -- Julia project dependencies.
- `Manifest.toml` -- Resolved dependency versions (auto-generated).
- `build_sysimage.jl` -- PackageCompiler script used by `esfex precompile`.
- `precompile_workload.jl` -- Workload run while building the sysimage.
- `ESFEX.so` / `.dll` / `.dylib` -- The sysimage, once built.
- `src/ESFEX.jl` -- Main module entry point.
- `src/types.jl` -- Type definitions.
- `src/user_constraints.jl` -- User-defined constraint registry and hooks.
- `src/power_system.jl` -- Operational dispatch model.
- `src/master_problem.jl` -- Capacity expansion model (deterministic, multi-system and stochastic).
- `src/benders.jl` -- Benders decomposition of the master problem.
- `src/transmission_dc.jl` -- DC power flow constraints.
- `src/transmission_ac.jl` -- AC power flow verification (Newton-Raphson).
- `src/transmission_acopf.jl` -- ACOPF formulations (SOC, QC, SDP, polar and rectangular NLP).
- `src/mga.jl` -- MGA/SPORES near-optimal alternatives.
- `src/primary_energy.jl` -- Fuel supply chain model.
- `src/electrolyzer.jl` -- Electrolyzer model.
- `src/dcopf_benchmark.jl`, `src/acopf_benchmark.jl` -- Benchmark helpers.

### get_solvers_env_path

```python
def get_solvers_env_path() -> Path
```

Path of the Julia environment that holds optional solvers: `<depot>/environments/esfex_solvers`, where `<depot>` is the first entry of `JULIA_DEPOT_PATH` (default `~/.julia`). It lives outside the package, so it survives ESFEX upgrades.

### create_julia_optimizer

```python
def create_julia_optimizer(
    solver: str = "highs",
    threads: int = 4,
    time_limit: float = 3600.0,
    gap: float = 0.01,
    verbose: bool = False,
) -> Any
```

Create a configured JuMP optimizer in Julia. Delegates to `ESFEX.create_optimizer()` for solver-specific parameter mapping and on-demand loading of solver packages.

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `solver` | `str` | `"highs"` | Solver name. Options: `"highs"`, `"gurobi"`, `"cplex"`, `"scip"`, `"xpress"`, `"cbc"`, `"glpk"` (LP/MIP); `"clarabel"`, `"scs"` (conic); `"ipopt"` (NLP ACOPF). |
| `threads` | `int` | `4` | Number of solver threads. |
| `time_limit` | `float` | `3600.0` | Maximum solve time in seconds. |
| `gap` | `float` | `0.01` | MIP optimality gap tolerance. |
| `verbose` | `bool` | `False` | Enable solver output. |

**Returns:** A Julia optimizer object suitable for use with `JuMP.Model(optimizer)`.

**Solver Notes:**

| Solver | License | Install |
|--------|---------|---------|
| HiGHS | Open source (MIT) | Bundled |
| GLPK | Open source (GPL) | Bundled |
| Clarabel, SCS | Open source | Bundled (conic ACOPF) |
| Ipopt | Open source (EPL) | Bundled (NLP ACOPF) |
| SCIP | Apache 2.0 | `esfex add-solver scip` |
| CBC | Open source (EPL) | `esfex add-solver cbc` |
| Gurobi | Commercial | `esfex add-solver gurobi` + license |
| CPLEX | Commercial | `esfex add-solver cplex` + license |
| Xpress | Commercial | `esfex add-solver xpress` + license |

### Optional solvers

Optional solvers are installed into the separate environment returned by [`get_solvers_env_path()`](#get_solvers_env_path), not into the shipped `Project.toml`. `initialize_julia` stacks that environment on `LOAD_PATH`, so an installed solver can be loaded on demand.

```python
OPTIONAL_SOLVER_PACKAGES: dict[str, str]  # {"gurobi": "Gurobi", "cplex": "CPLEX", "cbc": "Cbc", "scip": "SCIP", "xpress": "Xpress"}

def add_solver(name: str) -> str
def remove_solver(name: str) -> str
def list_installed_optional_solvers() -> list[str]
```

| Function | Description |
|----------|-------------|
| `add_solver(name)` | Runs `Pkg.add` for the solver's Julia package in the solvers environment, then `import`s it so build or license errors show up at once. Returns the package name. Raises `ValueError` for a name not in `OPTIONAL_SOLVER_PACKAGES` and `RuntimeError` if the install or import fails. |
| `remove_solver(name)` | Runs `Pkg.rm` for the solver's package in the solvers environment. Same errors as `add_solver`. |
| `list_installed_optional_solvers()` | Solver keys whose package is listed in the solvers environment's `Project.toml` (read directly, without starting Julia). |

The CLI wraps these as `esfex add-solver <name>`, `esfex remove-solver <name>` and `esfex list-solvers`. `add_solver` and `remove_solver` start juliacall but do not load the ESFEX module.

### include_plugin_overlays

```python
def include_plugin_overlays(paths) -> None
```

`include()`s plugin Julia overlay files into the running session, once per path. Overlays run after `ESFEX` is loaded, so they can call `ESFEX.register_constraint_hook!(...)`. An overlay that fails is logged and skipped. Called by the plugin manager.

### check_julia_available

```python
def check_julia_available() -> bool
```

Check whether `from juliacall import Main` succeeds. This does not load the ESFEX module, but importing `juliacall.Main` does start the Julia runtime.

**Returns:** `True` if the import succeeds, `False` otherwise.

### get_julia_version

```python
def get_julia_version() -> Optional[str]
```

Get the Julia version string (e.g., `"1.10.2"`). Returns `None` if Julia is not available.

### precompile_esfex

```python
def precompile_esfex(force: bool = False) -> Optional[Path]
```

Build the ESFEX sysimage. If a sysimage exists, is newer than every `src/*.jl` file and `force` is false, returns its path without rebuilding. Otherwise runs `julia --project=src/esfex/julia src/esfex/julia/build_sysimage.jl` (needs `julia` on `PATH`) and returns the path of the built sysimage, or `None` if the build fails. See [Sysimage](#sysimage).

---

## Sysimage

A Julia system image built with `PackageCompiler.jl` contains JuMP, HiGHS and the ESFEX pipeline compiled to native code, which shortens Julia startup considerably.

**Building:**

```bash
esfex precompile          # build, or report that the existing sysimage is up to date
esfex precompile --force  # rebuild anyway
```

This calls [`precompile_esfex()`](#precompile_esfex), which runs `src/esfex/julia/build_sysimage.jl`. The script installs PackageCompiler if needed, runs the workload `src/esfex/julia/precompile_workload.jl`, and writes `src/esfex/julia/ESFEX.so` (`ESFEX.dll` on Windows, `ESFEX.dylib` on macOS). The build takes several minutes.

**Using:**

No setup is needed. `initialize_julia` finds the sysimage and sets `PYTHON_JULIACALL_SYSIMAGE` before juliacall starts. If `PYTHON_JULIACALL_SYSIMAGE` is already set, that value wins. After editing Julia sources the sysimage is stale: it is still loaded, a log message says so, and `esfex precompile` rebuilds it.

---

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `JULIA_NUM_THREADS` | Number of Julia threads | `4` (set by `initialize_julia`) |
| `PYTHON_JULIACALL_THREADS` | Alternative thread setting (juliacall-specific) | Not set |
| `JULIA_PROJECT` | Not read; `initialize_julia` activates `src/esfex/julia/` with `Pkg.activate()` | Not set |
| `PYTHON_JULIACALL_SYSIMAGE` | Path to pre-built sysimage for faster startup | Set to `src/esfex/julia/ESFEX.*` when that file exists |
| `JULIA_DEPOT_PATH` | Julia package depot location; its first entry also hosts the optional-solvers environment | System default |

---

## Troubleshooting

### Common Errors

**`ImportError: juliacall is not installed`**

Install juliacall:
```bash
pip install juliacall
```

**`RuntimeError: Julia Project.toml not found`**

The Julia project directory is missing. Ensure the package is installed correctly and `src/esfex/julia/Project.toml` exists.

**`LoadError: ... package not found`**

Julia dependencies are not installed. Usually auto-resolved by `Pkg.instantiate()`, but if it fails:
```bash
cd src/esfex/julia
julia --project=. -e 'using Pkg; Pkg.instantiate()'
```

**`invalid redefinition of constant ESFEX`**

Harmless. Occurs when the Julia session already has the ESFEX module loaded (e.g., during interactive development). The module handles this automatically by reusing the existing module.

**Slow first startup (2-5 minutes)**

Normal behavior. Julia compiles all code on first use. To speed up subsequent runs:
1. Keep the Julia session alive (use the ESFEX Studio which maintains state).
2. Build a sysimage with `esfex precompile` (see above).

**`Manifest.toml` corruption**

Delete `src/esfex/julia/Manifest.toml` and reinitialize. The initialization code attempts this automatically when `Pkg.instantiate()` fails.
