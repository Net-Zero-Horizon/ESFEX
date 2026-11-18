# Solver Guide

## Supported Solvers

ESFEX builds its models with JuMP [**[20]**](../reference/bibliography.md#ref20) and can hand them to ten solvers. Five ship with ESFEX; the other five are installed on demand with [`esfex add-solver`](cli.md#esfex-add-solver).

| Solver | `solver.name` | Shipped with ESFEX | Problem classes | License |
|--------|---------------|:------------------:|-----------------|---------|
| **HiGHS** [**[21]**](../reference/bibliography.md#ref21) | `highs` | Yes (default) | LP, MIP, QP | Open source (MIT) |
| **GLPK** | `glpk` | Yes | LP, MIP | Open source (GPL) |
| **Clarabel** | `clarabel` | Yes | LP, SOCP, SDP | Open source (Apache 2.0) |
| **SCS** | `scs` | Yes | LP, SOCP, SDP | Open source (MIT) |
| **Ipopt** | `ipopt` | Yes | NLP | Open source (EPL) |
| **Gurobi** | `gurobi` | `esfex add-solver gurobi` | LP, MIP, QP, SOCP | Commercial (free academic) |
| **CPLEX** | `cplex` | `esfex add-solver cplex` | LP, MIP, QP, SOCP | Commercial (free academic) |
| **Xpress** | `xpress` | `esfex add-solver xpress` | LP, MIP, QP, SOCP | Commercial (free academic) |
| **CBC** | `cbc` | `esfex add-solver cbc` | LP, MIP | Open source (EPL) |
| **SCIP** | `scip` | `esfex add-solver scip` | LP, MIP | Open source (Apache 2.0) |

### Solver Selection Guidance

- **Start with HiGHS**: free, shipped with ESFEX, and the default. It handles the LP master problem and DC-OPF dispatch well.
- **Use Gurobi, CPLEX or Xpress for large models**, especially unit commitment (MIP), if you have a licence.
- **Avoid GLPK for production**: it is much slower and numerically weaker. Use it only for small tests.
- **AC-OPF needs a matching solver**: see the compatibility table below.

### Formulation and Solver Compatibility

The operational dispatch formulation is chosen per system with `power_flow_mode`. ESFEX passes the configured solver to the model unchanged; it does not switch solvers for you, so an incompatible choice makes the operational solve fail. The Studio greys out incompatible solvers once a formulation is selected.

| `power_flow_mode` | Problem class | Compatible solvers |
|-------------------|---------------|--------------------|
| `dcopf` (default), `dcopf_ac_verify` | LP (MIP in unit commitment) | `highs`, `gurobi`, `cplex`, `xpress`, `glpk`, `cbc`, `scip` |
| `acopf_soc`, `acopf_qc` | Convex SOCP / QC relaxation | `gurobi`, `cplex`, `xpress`, `clarabel`, `scs` (not `highs`: it has no second-order cones) |
| `acopf_sdp` | Semidefinite relaxation | `scs`, `clarabel` |
| `acopf_polar`, `acopf_rect` | Exact nonlinear AC-OPF | `ipopt` |

The master problem (capacity expansion) is always an LP and uses the same `solver` settings.


---


## Configuration

### YAML Configuration

```yaml
solver:
  name: highs              # Solver name (lowercase)
  threads: 4               # Parallel threads
  time_limit: 3600         # Maximum solve time in seconds (0 = unlimited)
  gap: 0.01                # MIP relative optimality gap (1%)
  verbose: false           # Show solver output in the console
  options:                 # Solver-specific options (see Advanced Solver Options)
    solver_method: ipm
    run_crossover: 'on'
    ipm_optimality_tolerance: 1.0e-08
```

### Configuration Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | `"highs"` | One of `highs`, `gurobi`, `cplex`, `xpress`, `cbc`, `glpk`, `scip`, `clarabel`, `scs`, `ipopt` |
| `threads` | int | `4` | Threads for the solver. Ignored by GLPK, Clarabel, SCS and Ipopt; for SCIP it only affects concurrent mode. |
| `time_limit` | int | `10800` | Maximum solve time per model, in seconds; `0` = no limit (every solver, every model: dispatch windows and master problems). Ignored by SCS. Benders also keeps its own caps with `0`: 300 s per subproblem day, and 600 s per master solve until a master reaches them without a plan (that master and the later ones then get `time_limit`). |
| `gap` | float | `0.01` | MIP relative optimality gap (0.01 = 1%). Ignored for pure LP. Not passed to GLPK (set `mip_gap` in `options`). For Ipopt, the value is passed as its convergence tolerance `tol`. |
| `verbose` | bool | `false` | Print the solver log. Overridden by `logging.console_level` (`basic` turns it off, `debug` turns it on) and by `esfex run -v`. |
| `options` | dict | `{}` | Solver-specific options (see [Advanced Solver Options](#advanced-solver-options)) |

### CLI Override

Override the solver from the command line without editing the YAML file:

```bash
esfex run -c config.yaml -s gurobi
```

The `options` block is kept; options that belong to a different solver are dropped with a warning (see below). Each dropped option is reported once per run, not for every window.

### Checking Available Solvers

```bash
esfex list-solvers
```

This lists all ten solvers with their availability and type (bundled or optional). Programmatically:

```python
from esfex.config.solver import detect_available_solvers

for name, available in detect_available_solvers().items():
    print(f"{name}: {'available' if available else 'not installed'}")
```

Detection reads the `[deps]` of ESFEX's Julia `Project.toml` and of the `esfex_solvers` environment that `esfex add-solver` writes to. It does not start Julia and does not look at Python packages such as `gurobipy`. It tells you what is installed, not whether a licence is valid.

`esfex info` starts Julia and actually loads HiGHS, Gurobi and CPLEX, which also catches licence problems for those two commercial solvers.


---


## Solver Installation

HiGHS, GLPK, Clarabel, SCS and Ipopt are dependencies of ESFEX's Julia project and are installed automatically with it. Nothing else is needed.

Gurobi, CPLEX, Xpress, CBC and SCIP are installed on demand:

```bash
esfex add-solver gurobi      # or cplex, xpress, cbc, scip
esfex list-solvers           # check
esfex remove-solver gurobi   # uninstall
```

`add-solver` adds the Julia package to a dedicated environment (`~/.julia/environments/esfex_solvers`, or under the first entry of `JULIA_DEPOT_PATH`), which ESFEX puts on Julia's load path at startup. It survives ESFEX upgrades. After installing, the command loads the package once, so a build or licence problem is reported immediately. See the [CLI reference](cli.md#esfex-add-solver).

!!! note
    Do not use `Pkg.add("Gurobi")` (or similar) in a Julia session to make a solver available to ESFEX. That installs into whichever environment is active in that session, which ESFEX does not load.

### Gurobi

1. Install Gurobi from [gurobi.com](https://www.gurobi.com/downloads/) and obtain a licence (free for academics at [gurobi.com/academia](https://www.gurobi.com/academia/academic-program-and-licenses/)); activate it with `grbgetkey <your-license-key>`.
2. Make sure `GUROBI_HOME` (and, for a non-default licence location, `GRB_LICENSE_FILE`) are set in the shell where you run the next step.
3. `esfex add-solver gurobi`

### CPLEX

1. Install IBM ILOG CPLEX Optimization Studio (free through the [IBM Academic Initiative](https://www.ibm.com/academic)).
2. Set `CPLEX_STUDIO_BINARIES` to the CPLEX binary directory, e.g. `export CPLEX_STUDIO_BINARIES=/path/to/cplex/bin/x86-64_linux`.
3. `esfex add-solver cplex`

### Xpress

1. Install FICO Xpress from [fico.com](https://www.fico.com/en/products/fico-xpress-optimization) and activate its licence.
2. `esfex add-solver xpress`

### CBC and SCIP

Open source, no licence needed:

```bash
esfex add-solver cbc
esfex add-solver scip
```

CBC is generally slower than HiGHS. SCIP is a research-grade MIP solver; its default LP solver is single-threaded.


---


## When to Use LP vs MIP

| Simulation component | Problem class | When used |
|---------------------|---------------|-----------|
| Master problem (capacity expansion) | LP | `development` mode (skipped in the other two modes) |
| Operational dispatch | LP | `development` (default) and `economic_dispatch` with `power_flow_mode: dcopf` |
| Operational dispatch with commitment | MIP | `simulation_mode: unit_commitment`, or `development` with `master_problem.use_uc_in_dispatch: true` |
| AC-OPF dispatch | SOCP / SDP / NLP | `power_flow_mode: acopf_*` (see the [compatibility table](#formulation-and-solver-compatibility)) |

### LP (Linear Programming)

Continuous variables only; solved to optimality within the solver tolerances regardless of the `gap` setting.

- **Used for**: capacity expansion planning and economic dispatch
- **Solver choice**: HiGHS handles LP well; all LP-capable solvers work
- **Performance**: large models usually solve in seconds to minutes

### MIP (Mixed-Integer Programming)

Adds binary commitment variables (on/off status, start-ups) with minimum up/down times. Computationally harder; may not reach optimality within the time limit.

- **Used for**: unit commitment
- **Solver choice**: Gurobi, CPLEX or Xpress if available; otherwise HiGHS. CBC, SCIP and GLPK are usually slower.
- **Performance**: exponential worst case; `gap` sets the accepted optimality tolerance

The master problem has no integer variables: retirements follow unit age (see [Core Concepts](../getting-started/concepts.md#age-based-retirement)) instead of binary decisions.


---


## Performance Tuning

### Thread Count

More threads help MIP (parallel branch-and-bound) and interior-point / barrier LP methods; simplex is mostly serial. The default is `threads: 4`. Suggested values:

| Mode | Suggested threads | Notes |
|------|-------------------|-------|
| Economic dispatch (LP) | 2-4 | Simplex is mostly single-threaded; IPM benefits from parallelism |
| Unit commitment (MIP) | 4-8 | Branch-and-bound benefits from more threads |
| Master problem (LP) | 2-4 | Similar to economic dispatch |
| Very large models (10+ nodes, 25 years) | 8-16 | More threads help presolve and barrier |

With HiGHS `solver_method: simplex`, Gurobi `method: primal_simplex`/`dual_simplex`, CPLEX `lp_method: primal_simplex`/`dual_simplex`/`network` or Xpress `DEFAULTALG: dual_simplex`/`primal_simplex`, the thread count has little effect (the Studio greys out the field). GLPK is always single-threaded.

Julia's own thread count is separate: ESFEX sets `JULIA_NUM_THREADS=4` if it is not already set.

### Time Limits

| Model Size | Suggested Limit | Notes |
|-----------|----------------|-------|
| Single node, 1 year | 300 s | Should solve in seconds |
| 3-5 nodes, 10 years | 1,800 s | Moderate problem size |
| 10+ nodes, 25 years | 3,600-7,200 s | Large problem; may need relaxed gap |
| MIP unit commitment | 600-1,800 s per window | Per rolling-horizon window |

A solve stopped by `time_limit` (or another limit) is used when the solver holds a feasible solution: a dispatch window or a master problem (perfect foresight, myopic year, stochastic, Benders master iteration) then logs a WARNING with the objective, the solver's bound and the gap, and the run goes on with that solution. A limit without a feasible solution stops the run with an error. A barrier solve without crossover has no feasible solution until it converges.

### MIP Gap

The solver stops when:

$$\frac{|BestBound - BestSolution|}{|BestSolution|} \leq gap$$

| Gap Value | Meaning | Speed | Accuracy |
|-----------|---------|-------|----------|
| 0.001 (0.1%) | Very tight | Slow | High |
| 0.01 (1%) | Good balance | Moderate | Good |
| 0.02 (2%) | Relaxed | Fast | Acceptable |
| 0.05 (5%) | Very relaxed | Very fast | Approximate |

For LP models, the solver always reaches exact optimality regardless of this setting.

### Large LP Master Problems

A multi-year master problem of a large system is one big LP (tens of millions of nonzeros). For it, barrier without crossover is usually much faster than the default method, and it uses all the threads:

```yaml
solver:
  name: gurobi
  threads: 16
  time_limit: 21600
  options:
    Method: 2        # barrier (method: barrier)
    Crossover: 0     # no crossover (crossover: off)
```

Without crossover the solution is the barrier's interior point, accurate to `bar_conv_tol` (default 1e-8): investments and costs are reliable, but the solution is not a vertex, so more units can be partly built or dispatched than in a simplex solution. Keep `method: auto` (concurrent) when you need a vertex solution or when barrier struggles numerically. The solver options apply to every solve of the run, the dispatch windows too, where the default method is often faster on the small window LPs.

### LP Method Selection

| Method | Best for | Notes |
|--------|----------|-------|
| Dual simplex | Default choice | Fast for most problems; warm-starts well |
| Primal simplex | Problems with many constraints | Alternative when dual simplex struggles |
| Barrier / IPM [**[4]**](../reference/bibliography.md#ref4) | Large problems | Parallelizes well; may need crossover for a vertex solution |
| Concurrent | When unsure | Runs several methods in parallel (Gurobi) |

Example for HiGHS:

```yaml
solver:
  name: highs
  options:
    solver_method: ipm    # interior point method
    run_crossover: 'on'   # crossover to a vertex solution
```

If `solver_method` is not set, HiGHS chooses the algorithm itself.


---


## Advanced Solver Options

Each solver exposes tunable parameters through the `options` dictionary. The tables list the options the Studio offers; the key in the first column is what you write in `options`.

**Method-specific options.** Some options apply to some LP algorithms only (column *Applies when*, or the *Method-specific* line under a table). With another method selected, the option is dropped with a warning in the log (once per run). With HiGHS, the method must be set **explicitly**: under `solver_method: choose` the option is dropped too, because some HiGHS builds reject an option of an algorithm that is not running. For example, `simplex_scale_strategy` takes effect only together with `solver_method: simplex`. Gurobi, CPLEX and Xpress accept every parameter whatever the algorithm, and their automatic method may run barrier, so with the method on `auto` the option is passed when the line under the table lists `auto`. The method may be written as its name (`method: barrier`) or, as the Studio saves it, as the solver's value (`Method: 2`).

Keys that are not in any table are passed to the solver unchanged, so any native solver parameter can be set. Keys that belong to another solver's table are dropped with a warning (this keeps a Gurobi option from breaking a HiGHS run after `-s highs`).

### HiGHS Options

| Option | Type | Values | Default | Applies when | Description |
|--------|------|--------|---------|--------------|-------------|
| `presolve` | combo | `off`, `on`, `choose` | `choose` | always | Presolve reduction |
| `solver_method` | combo | `choose`, `simplex`, `ipm`, `ipx`, `hipo`, `pdlp` | `choose` | always | LP algorithm |
| `parallel` | combo | `choose`, `off`, `on` | `choose` | always | Parallel computation |
| `simplex_strategy` | combo | `choose`, `dual_serial`, `dual_pami`, `dual_sip`, `primal` | `choose` | `solver_method: simplex` | Simplex variant |
| `run_crossover` | combo | `off`, `choose`, `on` | `on` | `solver_method: ipm`, `ipx` or `hipo` | Crossover after interior point |
| `primal_feasibility_tolerance` | float | 1e-10 to 1.0 | 1e-7 | `solver_method: simplex` | Primal feasibility tolerance |
| `dual_feasibility_tolerance` | float | 1e-10 to 1.0 | 1e-7 | `solver_method: simplex` | Dual feasibility tolerance |
| `ipm_optimality_tolerance` | float | 1e-12 to 1.0 | 1e-8 | `solver_method: ipm`, `ipx` or `hipo` | IPM optimality tolerance |
| `pdlp_scaling` | combo | `off`, `on` | `on` | `solver_method: pdlp` | PDLP scaling |
| `pdlp_iteration_limit` | int | 0 to 2,147,483,647 | 2,147,483,647 | `solver_method: pdlp` | Max PDLP iterations |
| `simplex_iteration_limit` | int | 0 to 2,147,483,647 | 2,147,483,647 | `solver_method: simplex` | Max simplex iterations |
| `simplex_scale_strategy` | combo | `off`, `choose`, `forced_equilibration`, `mean_equilibration`, `max_equilibration`, `max_value_0`, `max_value_1` | `choose` | `solver_method: simplex` | Scaling strategy |

When `solver_method: simplex` is set, ESFEX also turns crossover off.

### Gurobi Options

| Option | Type | Values | Default | Description |
|--------|------|--------|---------|-------------|
| `method` | combo | `auto`, `primal_simplex`, `dual_simplex`, `barrier`, `concurrent` | `auto` | LP method |
| `presolve` | combo | `auto`, `off`, `conservative`, `aggressive` | `auto` | Presolve level |
| `crossover` | combo | `auto`, `off` | `auto` | Crossover after barrier |
| `numeric_focus` | combo | `auto`, `moderate`, `aggressive` | `auto` | Numerical precision focus |
| `scale_flag` | combo | `auto`, `off`, `moderate`, `aggressive` | `auto` | Scaling level |
| `bar_conv_tol` | float | 1e-12 to 1.0 | 1e-8 | Barrier convergence tolerance |
| `feasibility_tol` | float | 1e-9 to 1e-2 | 1e-6 | Feasibility tolerance |
| `optimality_tol` | float | 1e-9 to 1e-2 | 1e-6 | Optimality tolerance |
| `heuristics` | float | 0.0 to 1.0 | 0.05 | Time fraction for MIP heuristics |
| `iteration_limit` | int | 0 to 2,147,483,647 | 2,147,483,647 | Max iterations |

Method-specific: `crossover` and `bar_conv_tol` need `method: auto`, `barrier` or `concurrent`; `iteration_limit` needs `method: auto`, `primal_simplex`, `dual_simplex` or `concurrent`.

With `method: auto`, Gurobi solves an LP with its concurrent optimizer: barrier and simplex run side by side and share the threads, and `crossover: off` only applies if barrier finishes first. With `crossover: off` and `method: auto` the log recommends `method: barrier` (once per run). See [Large LP master problems](#large-lp-master-problems).

### CPLEX Options

| Option | Type | Values | Default | Description |
|--------|------|--------|---------|-------------|
| `lp_method` | combo | `auto`, `primal_simplex`, `dual_simplex`, `barrier`, `network` | `auto` | LP method |
| `presolve` | combo | `on`, `off` | `on` | Presolve |
| `numerical_emphasis` | combo | `off`, `on` | `off` | Numerical emphasis |
| `scale` | combo | `equilibration`, `off`, `aggressive` | `equilibration` | Scaling |
| `feasibility_tol` | float | 1e-9 to 1e-1 | 1e-6 | Feasibility tolerance |
| `optimality_tol` | float | 1e-9 to 1e-1 | 1e-6 | Optimality tolerance |
| `barrier_conv_tol` | float | 1e-12 to 1e-1 | 1e-8 | Barrier convergence tolerance |
| `mip_emphasis` | combo | `balanced`, `feasibility`, `optimality`, `best_bound`, `hidden_feasibility` | `balanced` | MIP solving emphasis |

Method-specific: `feasibility_tol` and `optimality_tol` need `lp_method: auto`, `primal_simplex`, `dual_simplex` or `network`; `barrier_conv_tol` needs `lp_method: auto` or `barrier`. CPLEX ignores `solver.verbose`.

### GLPK Options

| Option | Type | Values | Default | Description |
|--------|------|--------|---------|-------------|
| `msg_lev` | combo | `off`, `errors`, `normal`, `verbose` | `normal` | Message verbosity |
| `meth` | combo | `primal_simplex`, `dual_simplex`, `dual_primal` | `primal_simplex` | LP method |
| `presolve` | combo | `off`, `on` | `on` | Presolve |
| `tol_bnd` | float | 1e-12 to 1e-1 | 1e-7 | Primal tolerance |
| `tol_dj` | float | 1e-12 to 1e-1 | 1e-7 | Dual tolerance |
| `tol_piv` | float | 1e-12 to 1.0 | 1e-10 | Pivot tolerance |
| `it_lim` | int | 0 to 2,147,483,647 | 2,147,483,647 | Iteration limit |
| `mip_gap` | float | 0.0 to 1.0 | 0.0 | MIP gap (`solver.gap` is not passed to GLPK) |

### CBC Options

| Option | Type | Values | Default | Description |
|--------|------|--------|---------|-------------|
| `logLevel` | combo | `off`, `minimal`, `normal`, `verbose` | `minimal` | Log level |
| `primalTolerance` | float | 1e-10 to 1.0 | 1e-7 | Primal tolerance |
| `dualTolerance` | float | 1e-10 to 1.0 | 1e-7 | Dual tolerance |
| `ratioGap` | float | 0.0 to 1.0 | 0.0 | MIP gap |

### SCIP Options

| Option | Type | Values | Default | Description |
|--------|------|--------|---------|-------------|
| `display/verblevel` | combo | `off`, `errors`, `warnings`, `normal`, `full` | `off` | Verbosity |
| `presolving/maxrounds` | combo | `off`, `default`, `aggressive` | `default` | Presolve rounds |
| `separating/maxrounds` | combo | `off`, `default`, `aggressive` | `default` | Cut separation rounds |
| `numerics/feastol` | float | 1e-12 to 1e-1 | 1e-6 | Feasibility tolerance |
| `numerics/dualfeastol` | float | 1e-12 to 1e-1 | 1e-7 | Dual feasibility tolerance |
| `lp/scaling` | combo | `off`, `on` | `on` | LP scaling |

### Xpress Options

| Option | Type | Values | Default | Description |
|--------|------|--------|---------|-------------|
| `OUTPUTLOG` | combo | `off`, `on` | `off` | Output log |
| `PRESOLVE` | combo | `off`, `on` | `on` | Presolve |
| `DEFAULTALG` | combo | `auto`, `dual_simplex`, `primal_simplex`, `barrier` | `auto` | LP algorithm |
| `SCALING` | combo | `off`, `row_col`, `aggressive` | `row_col` | Scaling |
| `FEASTOL` | float | 1e-12 to 1e-1 | 1e-6 | Feasibility tolerance |
| `OPTIMALITYTOL` | float | 1e-12 to 1e-1 | 1e-6 | Optimality tolerance |
| `BARGAPSTOP` | float | 1e-12 to 1.0 | 1e-8 | Barrier gap stop |

Method-specific: `BARGAPSTOP` needs `DEFAULTALG: auto` or `barrier`.

### Clarabel, SCS and Ipopt

These have no option tables; any key in `options` is passed to the solver as a native parameter. ESFEX sets `time_limit` and `verbose` for Clarabel; `max_iters = 100000`, `eps_abs = eps_rel = 1e-6` and `verbose` for SCS; `max_cpu_time`, `tol` (from `solver.gap`) and `print_level` for Ipopt. Because `gap` defaults to 0.01, set a tighter value (e.g. `gap: 1.0e-6`) when running Ipopt.


---


## Numerical Stability

Optimization models can have wide coefficient ranges due to mixing different physical quantities and cost scales:

| Quantity | Typical Range | Units |
|----------|--------------|-------|
| Generator fuel costs | 3-100 | $/MWh |
| Maintenance (variable O&M) costs | 1-50 | $/MWh |
| Fixed O&M costs | 10,000-150,000 | $/MW·yr |
| Investment costs | 100,000-2,000,000 | $/MW |
| Penalty coefficients | 100-10,000,000 | $/MWh or $/MW |
| Power outputs | 0-1,000 | MW |
| Energy capacity | 0-10,000 | MWh |

This creates a coefficient range spanning 6-9 orders of magnitude, which can cause numerical difficulties.

### Scaling Strategy

HiGHS uses automatic scaling by default. Other solvers also enable scaling. If you encounter numerical issues, try:

**HiGHS** (the scaling option needs simplex to be selected explicitly):
```yaml
solver:
  name: highs
  options:
    solver_method: simplex
    simplex_scale_strategy: max_equilibration
```

**Gurobi**:
```yaml
solver:
  name: gurobi
  options:
    scale_flag: aggressive     # Aggressive scaling
    numeric_focus: aggressive  # Prioritize numerical accuracy
```

**CPLEX**:
```yaml
solver:
  name: cplex
  options:
    scale: aggressive
    numerical_emphasis: 'on'
```

### Guidelines for Numerical Stability

1. **Keep penalty values reasonable**: Penalties above 10,000,000 can cause solver stalls or numerical warnings. The recommended range is 100-1,000,000. Note that `penalties.loss_of_load` defaults to 10,000,000 $/MWh when not set.

2. **Check coefficient ranges**: Ensure that the ratio between the largest and smallest non-zero coefficients is less than 10^6.

3. **Enable presolve**: Presolve reduces problem size and can improve numerical properties. It is on by default for all solvers.

4. **Use appropriate tolerances**: The default tolerances (1e-7 for primal/dual, 1e-8 for barrier) are suitable for most models. Only tighten them if you observe solution quality issues.

5. **Monitor solver output**: Enable `verbose: true` together with `logging.console_level: verbose` (or run with `esfex run -v`) for solver diagnostics. With the default `console_level: basic` the solver log is suppressed.

### Numerical Trouble in a Master Problem

A barrier run without crossover can lose its way on a large master although the model is feasible: it ends "Numerical trouble encountered" (termination `NUMERICAL_ERROR`), or it stalls short of its tolerances and returns its best iterate ("Sub-optimal termination", Gurobi `SUBOPTIMAL`, which JuMP reports as `LOCALLY_SOLVED`). The master takes only an optimal plan, so either stopped a myopic run. When a master solve (every master mode, MGA included; Benders excepted) ends `NUMERICAL_ERROR` or `LOCALLY_SOLVED` with Gurobi, ESFEX solves it once more with Gurobi's homogeneous barrier (`BarHomogeneous = 1`, the solver's own advice), with a WARNING in the log, and sets the parameter back afterwards. With line losses this applies to each solve of the [loss repair](../formulation/dc-power-flow.md#exact-losses): a repair round whose re-solve is still not optimal is undone, and the model of the round before (whose solve was optimal) is solved again, with a WARNING. Other solvers keep the status of the solve (a repair round they do not solve to optimality is undone the same way). The dispatch windows are solved the same way: a window's solve that ends `NUMERICAL_ERROR` or `LOCALLY_SOLVED` with Gurobi is solved once more with the homogeneous barrier, and a repair round still not optimal is undone. A window still `LOCALLY_SOLVED` after that keeps its feasible point (its cost may lie above the optimum), with a WARNING naming the window, its year and first hour; the run goes on. (Ipopt's `LOCALLY_SOLVED`, its normal answer on the AC models, is kept without a warning.) On Cuba's rep-day myopic run, the 2037 master's third repair round, which differed from the optimal second round only in 50 loss coefficients within the range the model already had, stalled after 117 barrier iterations; the homogeneous barrier solved it to optimality in 136, and the year's plan was found.

The master itself avoids the causes found on Cuba's myopic runs: a line's loss segments are bounded unless its corridor can be expanded without limit (unbounded segments let the barrier iterates run to 10⁶ MW, see [DC power flow](../formulation/dc-power-flow.md)); the renewable ratio that links one year's growth cap to the next is not a row with the year's demand (MWh, ~10⁷) as a coefficient; and capacities of at most 10⁻³ MW or MWh that a solve reports are read as 0, so a myopic year does not carry them as existing units into the next year's model (see [Master Problem](master-problem.md)).


---


## Common Solver Errors and Troubleshooting

### Solver Stall (No Progress)

| Symptom | Cause | Fix |
|---------|-------|-----|
| Solver runs for hours with no improvement | Penalty too high (e.g., `penalties.fre_penetration_loss: 600000000`) | Reduce penalty coefficients to < 1,000 |
| Simplex cycling | Degenerate problem | Switch to barrier method: `solver_method: ipm` |
| MIP not finding feasible solutions | Tight constraints | Increase `time_limit`, relax `gap`, or use a faster solver |

### Infeasible Model

```
Model status: INFEASIBLE
```

No solution satisfies all constraints simultaneously. Common causes:

1. **Conflicting constraints**: Demand exceeds available generation capacity plus loss-of-load penalty budget. Check that demand data is correct and generators have sufficient capacity.

2. **Reserve requirements too high**: Static or dynamic reserve requirements may be impossible to meet. Try reducing `nodes.reserve_static` or `nodes.reserve_dynamic`.

3. **Missing fuel supply**: Generators may not have fuel available. Check fuel entry points and supply constraints.

4. **Debug strategy**: Enable verbose output (`verbose: true`) and check which constraints are binding. Some solvers (Gurobi, CPLEX) can compute an Irreducible Infeasible Subsystem (IIS) to identify conflicting constraints.

### Unbounded Model

```
Model status: UNBOUNDED
```

The objective can decrease without limit, typically indicating a modeling error:

1. **Variables without upper bounds**: Check that all power output variables have appropriate capacity bounds.
2. **Missing constraints**: Ensure generator output is constrained by rated power and availability.
3. **Sign errors**: Verify that costs are positive and penalties are positive.

### Numerical Warnings

```
WARNING: Numerical issues detected
```

The solver encountered precision issues during solution:

1. **Wide coefficient range**: Check penalty/cost ratios. Reduce the largest penalties.
2. **Enable scaling**: For HiGHS, set `solver_method: simplex` and `simplex_scale_strategy: max_equilibration`.
3. **Increase tolerance**: Slightly relax feasibility tolerances if the solution is acceptable.
4. **Switch method**: Try barrier method instead of simplex (or vice versa).

### Very Slow MIP

If MIP problems (unit commitment) are excessively slow:

1. **Increase gap**: Set `gap: 0.02` or `gap: 0.05` to accept near-optimal solutions
2. **Reduce problem size**: Use fewer rolling horizon hours, or switch to `development` mode (LP)
3. **Use a better solver**: Switch from GLPK/CBC to HiGHS or Gurobi
4. **Reduce binary variables**: ESFEX's master problem is pure LP. MIP is only used in unit commitment mode and with `master_problem.use_uc_in_dispatch: true`

### Out of Memory

Large multi-year, multi-node models can consume significant memory:

1. **Reduce temporal resolution**: Use `resolution_hours: 6` instead of `resolution_hours: 1`
2. **Reduce rolling horizon window**: Use `rolling_horizon_hours: 24` instead of `48`
3. **Fewer representative days**: Reduce `representative_days` in master problem config
4. **Limit threads**: Each thread uses memory for its own copy of the problem data. Reducing thread count reduces memory.


---


## Memory Considerations for Large Models

| Factor | Memory Impact | Typical Contribution |
|--------|--------------|---------------------|
| Number of nodes | Quadratic (adjacency matrix) | Moderate |
| Number of generators | Linear per node | Moderate |
| Number of hours | Linear | Large (dominant factor) |
| Number of years (master problem) | Linear | Moderate |
| Number of binary variables (MIP) | Adds branch-and-bound tree | Large for MIP |

Approximate memory requirements:

| Model Size | LP Memory | MIP Memory |
|-----------|-----------|------------|
| 1 node, 1 year, 48h windows | ~100 MB | ~200 MB |
| 5 nodes, 10 years | ~500 MB | ~2 GB |
| 10 nodes, 25 years | ~2 GB | ~8 GB |
| 50 nodes, 25 years | ~10 GB | ~40+ GB |


---


## Solver Log Interpretation

### Enabling Verbose Output

```yaml
solver:
  verbose: true
logging:
  console_level: verbose   # with the default "basic" the solver log is suppressed
```

Or run with `esfex run -c config.yaml -v`.

### HiGHS Log Example

```
Running HiGHS 1.7.0
Solving LP
Using EKK dual simplex solver - serial
  Iteration        Objective     Infeasibilities  Num
          0    0.0000000000e+00 Pr: 1460(3.5e+04) 0s
       1200    8.5432100000e+06 Pr: 0(0)           2s
Solving report
  Status            Optimal
  Primal bound      8.5432100000e+06
  Dual bound        8.5432100000e+06
  Solution status   feasible
  Timing            1.85 (total)
```

Key information:
- **Iteration count**: Higher counts may indicate scaling issues
- **Primal infeasibilities** (`Pr:`): Should reach 0 at optimality. If stuck above 0, there may be numerical issues
- **Status**: `Optimal` = solved successfully, `Infeasible` = no feasible solution, `Time limit` = stopped early

### Gurobi Log Example

```
Optimize a model with 15000 rows, 25000 columns and 75000 nonzeros
Presolve removed 3000 rows and 5000 columns
Presolved: 12000 rows, 20000 columns, 60000 nonzeros

Iteration    Objective       Primal Inf.    Dual Inf.      Time
       0    0.0000000e+00   3.500000e+04   0.000000e+00      0s
    1500    8.5432100e+06   0.000000e+00   0.000000e+00      2s

Solved in 1500 iterations and 1.8 seconds
Optimal objective  8.543210000e+06
```

Key information:
- **Presolve reduction**: Shows how much the model was simplified. Large reductions are good.
- **Primal/Dual Inf.**: Both should reach 0.
- **Iteration count vs. column count**: If iterations >> columns, the model may have numerical issues.

### Understanding Objective Values

The objective value represents total system cost in dollars. Reasonable ranges depend on system size:

| System Size | Typical Objective (per year) |
|------------|----------------------------|
| Small island (50 MW) | $5M - $50M |
| Medium system (500 MW) | $50M - $500M |
| Large system (5 GW) | $500M - $5B |

If the objective is unexpectedly low (e.g., $50 for a real system), this may indicate a modeling error such as free generation from unconstrained variables. If it is unexpectedly high, check for excessive penalty costs driving the solution.
