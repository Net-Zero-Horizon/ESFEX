# Sensitivity Analysis

Module: `esfex.sensitivity`

Submodules: `esfex.sensitivity.engine`, `esfex.sensitivity.lp_parser`, `esfex.sensitivity.worker`

Requires: SALib and scipy, both included in the core `pip install esfex`.

## Overview

Sobol global sensitivity analysis [**[11]**](../reference/bibliography.md#ref11), [**[12]**](../reference/bibliography.md#ref12) (via SALib [**[19]**](../reference/bibliography.md#ref19)) varies all parameters simultaneously and decomposes output variance into contributions from each parameter and their interactions.

**Two analysis modes:**

| Mode | Speed | Fidelity | Use Case |
|------|-------|----------|----------|
| LP-level (`"lp"`) | Fast (seconds per eval) | Approximate (linear model) | Screening, quick parameter importance ranking |
| Config-level (`"config"`) | Slow (minutes per eval) | Exact (full nonlinear model) | Final analysis, publication-quality results |

---

## SensitivityEngine

Orchestrates Sobol sensitivity analysis using SALib.

```python
class SensitivityEngine:
    def __init__(
        self,
        mode: str,
        parameters: list[SensitivityParameter],
        kpi_names: list[str] | None = None,
        n_base_samples: int = 128,
        years: int | None = None,
    )
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `mode` | `str` | required | Analysis mode: `"lp"` for LP-level or `"config"` for config-level. |
| `parameters` | `list[SensitivityParameter]` | required | List of parameters to vary. |
| `kpi_names` | `list[str]` or `None` | `None` | KPI names to evaluate. If `None`, uses the default set: `["total_cost", "inv_gen_total", "inv_bat_total", "curtailment", "load_shedding"]`. |
| `n_base_samples` | `int` | `128` | Sobol N parameter. Total evaluations = `N * (D + 2)` where D = number of parameters. |
| `years` | `int` or `None` | `None` | Config mode: years simulated by each run (`--years`). `None` uses the `esfex run` default of 25 years. The Studio dialog sets it with **Years per run**. |

**Properties:**

| Property | Type | Description |
|----------|------|-------------|
| `problem` | `dict` | SALib problem definition with `num_vars`, `names`, and `bounds`. |
| `n_evaluations` | `int` | Total number of model evaluations required: `N * (D + 2)`. |

### generate_samples

```python
def generate_samples(self) -> np.ndarray
```

Generate the sample matrix with `SALib.sample.sobol` (the successor to the deprecated `SALib.sample.saltelli`) and `calc_second_order=False`. Second-order indices are not computed.

**Returns:** Array of shape `(N*(D+2), D)` where each row is a parameter combination.

### run_lp_analysis

```python
def run_lp_analysis(
    self,
    lp_path: str,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> SobolResult
```

Run LP-level Sobol analysis. Parses the LP file once, then perturbs objective coefficients and/or RHS values for each sample, re-solving with scipy's HiGHS backend.

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `lp_path` | `str` | Path to a `.lp` file exported from JuMP (e.g., via `MasterProblemAdapter.write_lp()`). |
| `progress_callback` | callable or `None` | Optional callback `(current, total, message)` for progress tracking. |

**Returns:** `SobolResult` with first-order (S1) and total-effect (ST) indices.

**How it works:**

1. Parse the LP file into an `LPModel` (variables, constraints, objective).
2. Group objective variables and constraints by naming pattern (e.g., `tech_inv_y*_t1_*` -> `"inv_tech_1"`; see [LP-Level Auto-Detection](#lp-level-auto-detection)). Parameters whose `key` matches no group are ignored.
3. For each sample, multiply the relevant objective coefficients or RHS values by the sample multiplier.
4. Re-solve the perturbed LP using `scipy.optimize.linprog` with HiGHS.
5. Extract KPIs from the solution.
6. Compute Sobol indices using `SALib.analyze.sobol`.

### run_config_analysis

```python
def run_config_analysis(
    self,
    base_config_path: str,
    output_dir: str,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> SobolResult
```

Run config-level Sobol analysis. Creates modified YAML configs and runs full ESFEX simulations per sample.

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `base_config_path` | `str` | Path to the base YAML configuration file. |
| `output_dir` | `str` | Output directory for intermediate results and temp configs. |
| `progress_callback` | callable or `None` | Optional progress callback. |

**Returns:** `SobolResult` with Sobol indices.

**How it works:**

1. Load the base YAML config.
2. For each sample, apply parameter multipliers to create a modified config (investment costs, fuel costs, demand growth, etc.).
3. Write the modified config to a temporary YAML file.
4. Run a full ESFEX simulation via subprocess: `python -m esfex.cli run --config temp.yaml --output run_dir [--years N]` (timeout 1 hour; a failed run gives NaN KPIs).
5. Extract KPIs from the run's HDF5 file (the newest `.h5` in `run_dir`, which may also hold files of earlier analyses): `summary_results/total_cost` and `loss_of_load` summed over years; `curtailment` summed over the `detailed_results` year groups; the investment KPIs from those groups' `investment_*` attributes.
6. Compute Sobol indices.

**Config-level parameter application:**

`_apply_config_multipliers()` scales fields of each entry under `systems` in the raw YAML dict. Components may be name-keyed dicts or lists; scalar and per-node list values are both scaled.

| Key | Effect |
|-----|--------|
| `invest_cost_renewables` | Multiplies `invest_cost` of `technologies` and `generators` entries classed as renewable (`type`, `technology`, `fuel` or `name` contains `solar`, `wind`, `pv` or `photovolt`). |
| `invest_cost_conventional` | Multiplies `invest_cost` of the other `technologies` and `generators` entries. |
| `fuel_cost` | Multiplies `fuel_cost` of all `technologies` and `generators` entries. |
| `invest_cost_storage` | Multiplies `invest_cost`, `invest_cost_power`, `invest_cost_energy` and `invest_cost_capacity` of `battery_technologies` and `batteries` entries. |
| `invest_cost_transmission` | Multiplies `nodes.transference_invest_cost` of each system, `line_cost_per_mw_km` and `transformer_cost_per_mw` of its `development_zones`, and `investment_cost_per_MW` of `meta_network.systems_links`. |
| `maintenance_cost` | Multiplies `maintenance_cost` of `generators`, `technologies`, `batteries` and `battery_technologies` entries (as the stochastic scenario multiplier does). |
| `demand_growth` | Multiplies `demand_scale` of each system (1.0 when absent): the level of the whole demand, not a growth rate. A deterministic run has no demand growth to vary. |
| `carbon_price` | Multiplies `penalties.co2_cost` (any key case, e.g. `CO2_cost`); when absent, the schema default (10 $/t) is scaled. |

A parameter key not in this table raises `ValueError` (`fuel_price_growth` is one: generator fuel costs do not escalate by year in a run, so there is nothing to scale). The fields follow the [stochastic scenario multipliers](../formulation/stochastic-programming.md), except `demand_growth`.

---

## SensitivityParameter

Defines a single parameter to vary in the analysis.

```python
@dataclass
class SensitivityParameter:
    name: str                      # Display name
    key: str                       # Internal key for matching
    lower_bound: float = 0.5       # Lower multiplier bound
    upper_bound: float = 2.0       # Upper multiplier bound
    category: str = "objective"    # "objective", "rhs", or "config"
```

**Categories:**

| Category | Used In | Description |
|----------|---------|-------------|
| `"objective"` | LP mode | Multiplier applied to objective coefficients in the LP model. |
| `"rhs"` | LP mode | Multiplier applied to constraint right-hand-side values. |
| `"config"` | Config mode | Multiplier applied to YAML config parameters. |

**Example:**

```python
from esfex.sensitivity.engine import SensitivityParameter

params = [
    SensitivityParameter(
        name="Solar Investment Cost",
        key="invest_cost_renewables",
        lower_bound=0.5,
        upper_bound=2.0,
        category="config",
    ),
    SensitivityParameter(
        name="Diesel Fuel Cost",
        key="fuel_cost",
        lower_bound=0.5,
        upper_bound=3.0,
        category="config",
    ),
]
```

---

## SobolResult

Results from a Sobol analysis run.

```python
@dataclass
class SobolResult:
    parameters: list[str]                           # Parameter names
    kpi_names: list[str]                            # KPI names
    S1: dict[str, np.ndarray]                       # First-order indices {kpi: array}
    ST: dict[str, np.ndarray]                       # Total-effect indices {kpi: array}
    S1_conf: dict[str, np.ndarray]                  # S1 confidence intervals
    ST_conf: dict[str, np.ndarray]                  # ST confidence intervals
    n_samples: int                                  # Base Sobol N
    n_evaluations: int                              # Total evaluations performed
    n_failed: dict[str, int]                        # Evaluations without a finite value, per KPI
```

### Sobol Index Interpretation

**First-Order Index (S1):**

The fraction of output variance explained by varying this parameter alone:

| S1 Value | Interpretation |
|----------|----------------|
| 0.0 - 0.05 | Negligible direct effect |
| 0.05 - 0.20 | Minor influence |
| 0.20 - 0.50 | Significant influence |
| 0.50 - 1.00 | Dominant parameter |

**Total-Order Index (ST):**

The fraction of output variance explained by this parameter including all interactions:

- **ST ~ S1**: The parameter acts independently (no interactions).
- **ST >> S1**: Significant interaction effects with other parameters.
- **Sum of all ST > 1**: Indicates strong parameter interactions.

### to_csv

```python
def to_csv(self, filepath: str | Path) -> None
```

Export Sobol indices to a CSV file with columns: `KPI, Parameter, S1, S1_conf, ST, ST_conf`.

---

## Available KPIs

| KPI Name | LP mode | Config mode |
|----------|---------|-------------|
| `total_cost` | Objective value of the perturbed LP | Sum of `summary_results/total_cost` over years |
| `inv_gen_total` | Sum of all `tech_inv_*` variables (MW) | Sum of the `investment_tech_investment_power_*` attributes over the year groups (MW of new technology capacity) |
| `inv_bat_total` | Sum of all `bat_tech_pow_inv_*` variables (MW) | Sum of the `investment_bat_tech_investment_power_*` attributes (MW); battery energy (MWh) is not included |
| `curtailment` | Sum of variables whose name contains `curtail` | Sum of `detailed_results/*/curtailment` (MWh) |
| `load_shedding` | Sum of `op_ll_*` variables | Sum of `summary_results/loss_of_load` (MWh) |

Sobol indices need every evaluation of the Saltelli design, so a KPI with any non-finite value (a failed or timed-out run, an infeasible LP perturbation) gets NaN indices instead of being analysed with a stand-in value, and `n_failed[kpi]` counts those evaluations; a warning is logged and the Studio chart says how many failed. A KPI with zero variance gets all-zero indices.

---

## Predefined Config Parameters

```python
from esfex.sensitivity.engine import get_config_parameters

params = get_config_parameters()
```

Returns `SensitivityParameter` objects for:

| Key | Display Name | Bounds |
|-----|-------------|--------|
| `invest_cost_renewables` | RE Investment Cost | 0.5 - 2.0 |
| `invest_cost_storage` | Storage Investment Cost | 0.5 - 2.0 |
| `invest_cost_conventional` | Conv. Investment Cost | 0.5 - 2.0 |
| `invest_cost_transmission` | Transmission Inv. Cost | 0.5 - 2.0 |
| `fuel_cost` | Fuel Cost | 0.5 - 3.0 |
| `maintenance_cost` | Maintenance Cost | 0.5 - 2.0 |
| `demand_growth` | Demand Level | 0.8 - 1.5 |
| `carbon_price` | Carbon Price | 0.0 - 3.0 |

Each one changes the simulation; see [Config-level parameter application](#run_config_analysis). A parameter whose fields are 0 or absent in a config (e.g. no maintenance costs) still has no effect there and gets S1 = ST ≈ 0.

---

## LP-Level Auto-Detection

Parameters can be auto-detected from the LP file:

```python
from esfex.sensitivity.engine import get_lp_parameters

params = get_lp_parameters("master_problem.lp")
```

Returns `SensitivityParameter` objects for each detected group. Objective groups get bounds 0.5 - 2.0; RHS groups get 0.8 - 1.5. Variables with a zero objective coefficient are not grouped.

**Objective coefficient groups (from variable name patterns):**

| Pattern | Group Name | Description |
|---------|-----------|-------------|
| `tech_inv_y*_tN_*` | `inv_tech_N` | Investment cost of technology N |
| `trans_inv_y*_*` | `inv_trans` | Transmission investment cost |
| `bat_tech_pow_inv_y*_btN_*` | `inv_bat_pow_N` | Power investment cost of battery technology N |
| `bat_tech_cap_inv_y*_btN_*` | `inv_bat_cap_N` | Energy investment cost of battery technology N |
| `op_ll_*` | `op_load_shedding` | Load shedding penalty |
| `op_fre_loss_*` | `op_fre_penalty` | Frequency penalty |

**RHS groups (from constraint name patterns):**

| Pattern | Group Name | Description |
|---------|-----------|-------------|
| `demand_bal_*`, `power_balance_*` | `demand` | Demand balance RHS |
| `re_target_*`, `re_ratio_*` | `re_target` | RE target RHS |
| `max_curtailment_*`, `curtailment_lim*` | `curtailment_limit` | Curtailment limit RHS |
| `co2_*`, `carbon_*` | `co2_budget` | CO2 budget RHS |
| `budget_*` | `cost_budget` | Budget constraint RHS |

Groups only appear if the LP file contains matching names. The master problem's nodal power-balance constraints are unnamed in the exported LP, so the `demand` group is usually absent; the named RHS groups in the master are `re_ratio_*` and `max_curtailment_*`.

---

## LPModel (LP Parser)

Parses CPLEX LP format files (exported by JuMP) into scipy-compatible structures for fast re-solving.

```python
from esfex.sensitivity.lp_parser import parse_lp_file, solve_lp, perturb_and_solve

model = parse_lp_file("master_problem.lp")
print(f"Variables: {model.n_vars}, Constraints: {model.n_constraints}")

# Solve baseline
obj_val, solution = solve_lp(model)

# Perturb and re-solve
kpis = perturb_and_solve(
    model,
    obj_multipliers={"inv_tech_1": 1.5, "op_load_shedding": 0.8},
    rhs_multipliers={"re_target": 1.1},
)
```

Reads CPLEX LP format sections (minimize/maximize, subject to, bounds, end) and builds sparse scipy matrices for efficient re-solving. Each perturbation creates modified coefficient/RHS arrays without re-parsing.

---

## SensitivityWorker (GUI Integration)

Background QThread worker for running sensitivity analysis without blocking the GUI.

```python
from esfex.sensitivity.worker import SensitivityWorker

worker = SensitivityWorker(engine, lp_path="model.lp")
# Config mode: SensitivityWorker(engine, config_path="config.yaml", output_dir="results/")
worker.progressChanged.connect(on_progress)  # (current, total, message)
worker.resultReady.connect(on_result)         # SobolResult
worker.errorOccurred.connect(on_error)        # error message
worker.start()

# To cancel:
worker.cancel()
```

**Constructor:** `SensitivityWorker(engine, lp_path=None, config_path=None, output_dir=None)`. LP mode needs `lp_path`; config mode needs `config_path` and `output_dir`. A missing argument emits `errorOccurred` instead of starting the analysis.

**Signals:**

| Signal | Arguments | Description |
|--------|-----------|-------------|
| `progressChanged` | `(int, int, str)` | Current eval, total evals, status message. |
| `resultReady` | `(SobolResult,)` | Analysis complete with result. |
| `errorOccurred` | `(str,)` | Error or cancellation message. |

---

## Computational Cost

Total model evaluations follow the Sobol sampling formula:

```
Total evaluations = N * (D + 2)
```

where N = `n_base_samples` and D = number of parameters (first-order and total indices only).

| Parameters (D) | Samples (N) | Evaluations | Approx. Time (LP mode) |
|----------------|-------------|-------------|------------------------|
| 3 | 128 | 640 | ~1 minute |
| 5 | 256 | 1,792 | ~3 minutes |
| 9 | 128 | 1,408 | ~2 minutes |
| 9 | 256 | 2,816 | ~5 minutes |

Config mode runs the simulations one after another, so its run time is the number of evaluations times the time of one full ESFEX run.

---

## Best Practices

1. **Start with LP mode**: Use LP-level analysis for initial screening, then switch to config-level for important parameters.
2. **Start with few parameters**: Begin with 3-5 most uncertain parameters.
3. **Use reasonable bounds**: Bounds should reflect plausible uncertainty ranges (e.g., 0.5x to 2x for costs).
4. **Sufficient samples**: Use at least N=128 for reliable first-order indices, N=512+ for convergence.
5. **Check convergence**: Run with increasing N and verify indices stabilize.
6. **Prioritize results**: Parameters with high ST values should be modeled carefully or treated stochastically.

---

## Full Example

```python
from esfex.sensitivity.engine import (
    SensitivityEngine,
    get_config_parameters,
)

# Use the predefined config-level parameters that change the simulation
active = {"invest_cost_renewables", "invest_cost_storage",
          "invest_cost_conventional", "fuel_cost"}
params = [p for p in get_config_parameters() if p.key in active]

# Create engine
engine = SensitivityEngine(
    mode="config",
    parameters=params,
    kpi_names=["total_cost", "curtailment", "load_shedding"],
    n_base_samples=64,
)

print(f"Total evaluations needed: {engine.n_evaluations}")

# Run analysis
result = engine.run_config_analysis(
    base_config_path="configs/isla_juventud.yaml",
    output_dir="results/sensitivity/",
    progress_callback=lambda cur, tot, msg: print(f"[{cur}/{tot}] {msg}"),
)

# Export results
result.to_csv("results/sobol_indices.csv")

# Print most influential parameters for total cost
for i, param in enumerate(result.parameters):
    print(f"  {param}: S1={result.S1['total_cost'][i]:.3f}, "
          f"ST={result.ST['total_cost'][i]:.3f}")
```
