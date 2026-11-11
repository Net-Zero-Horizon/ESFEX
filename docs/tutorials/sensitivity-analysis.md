# Sensitivity Analysis

## Sobol Global Sensitivity Analysis

Energy system models depend on dozens of uncertain parameters. Sobol global sensitivity analysis [**[11]**](../reference/bibliography.md#ref11), [**[12]**](../reference/bibliography.md#ref12) varies all parameters simultaneously and decomposes output variance into contributions from each parameter, capturing both:

- **First-order effects (S1)**: The direct influence of a single parameter, independent of others
- **Total-order effects (ST)**: The combined influence of a parameter, including its interactions with all other parameters

If S_T is much larger than S_1 for a parameter, that parameter's influence depends strongly on other parameters (interaction effects).

---

## Prerequisites

A standard ESFEX install already includes everything needed:

```bash
pip install esfex
```

This bundles SALib [**[19]**](../reference/bibliography.md#ref19) (Sensitivity Analysis Library) and its dependencies as core requirements.

---

## Two Analysis Modes

| Mode | Speed | Accuracy | Use Case |
|------|-------|----------|----------|
| **LP-level** | Fast (seconds per evaluation) | Approximate (perturbs solved LP coefficients) | Quick screening of cost and RHS parameters |
| **Config-level** | Slow (minutes per evaluation) | Exact (re-runs full optimization) | Comprehensive analysis including investment decisions |

### LP-Level Analysis

Parses an exported LP file (`.lp` format), perturbs groups of objective coefficients and constraint right-hand sides, and re-solves each perturbation. A development-mode run with perfect-foresight planning writes its master problem to `<output>/logs/master_problem_debug.lp` when the environment variable `ESFEX_MASTER_DEBUG_LP=1` is set (e.g. `ESFEX_MASTER_DEBUG_LP=1 esfex run -c my_system.yaml`); not with myopic planning, the Benders solver or when MGA/SPORES is enabled. The Studio's sensitivity dialog picks that file up from the run's `logs` folder. The parameters are detected from the LP's variable and constraint names: investment cost per technology (`inv_tech_<t>`), per battery technology (`inv_bat_pow_<b>`, `inv_bat_cap_<b>`), transmission investment (`inv_trans`), the load-shedding and RE-shortfall penalties, and the RE-target and curtailment-limit right-hand sides. Runs thousands of evaluations in minutes.

### Config-Level Analysis

Writes a modified copy of the YAML for each sample by applying multipliers to base-case parameters, then runs `esfex run` on it in a subprocess. Captures how parameter changes affect investment decisions, not just operational costs. Each evaluation is a full run over the default 25-year horizon (the engine does not pass `--years`), so it takes minutes to hours.

The copies are written to the output directory, and relative data paths are resolved against the current directory when they are not found next to the copy. Run the analysis from the directory that holds the base configuration, or use absolute paths for `demand_path` and `Availability`.

---

## Configuration: Defining Parameters

### Predefined Config-Level Parameters

| Parameter Key | Display Name | Default Range | What it changes |
|--------------|-------------|---------------|-------------|
| `invest_cost_renewables` | RE Investment Cost | [0.5, 2.0] | `invest_cost` of solar/wind `technologies` and `generators` |
| `invest_cost_storage` | Storage Investment Cost | [0.5, 2.0] | `invest_cost_power`/`invest_cost_energy` of `battery_technologies` (and `invest_cost` of `batteries`) |
| `invest_cost_conventional` | Conv. Investment Cost | [0.5, 2.0] | `invest_cost` of all other `technologies` and `generators` |
| `fuel_cost` | Fuel Cost | [0.5, 3.0] | The `fuel_cost` field of `generators` and `technologies` |
| `invest_cost_transmission` | Transmission Inv. Cost | [0.5, 2.0] | Nothing at present |
| `maintenance_cost` | Maintenance Cost | [0.5, 2.0] | Nothing at present |
| `demand_growth` | Demand Growth | [0.8, 1.5] | Nothing at present |
| `fuel_price_growth` | Fuel Price Growth | [0.5, 2.0] | Nothing at present |
| `carbon_price` | Carbon Price | [0.0, 3.0] | Nothing at present |

Ranges are multipliers on base-case values. [0.5, 2.0] means half to double the base-case value.

Only the first four parameters change the configuration. The last five are listed (by `get_config_parameters()` and in the GUI dialog) but are not applied, so every sample runs with their base values and their Sobol indices come out near zero. Leave them out of config-level studies.

A unit counts as solar/wind when its `type`, `technology`, `fuel` or `name` contains "solar", "wind", "pv" or "photovolt"; everything else (including hydro and biomass) is scaled by `invest_cost_conventional`. `fuel_cost` scales only explicit `fuel_cost` values: a fuel cost the loader derives from the `fuels` block (`price_base`) is not changed.

### Custom Parameters

You can build your own parameter list, for example with other bounds. For config-level analysis the `key` must be one of the four applied keys above; any other key is accepted but leaves the configuration unchanged:

```python
from esfex.sensitivity.engine import SensitivityParameter

custom_params = [
    SensitivityParameter(
        name="Fuel Cost",
        key="fuel_cost",
        lower_bound=0.5,
        upper_bound=3.0,
        category="config",
    ),
    SensitivityParameter(
        name="Solar Investment",
        key="invest_cost_renewables",
        lower_bound=0.4,
        upper_bound=1.5,
        category="config",
    ),
    SensitivityParameter(
        name="Battery Investment",
        key="invest_cost_storage",
        lower_bound=0.3,
        upper_bound=2.0,
        category="config",
    ),
]
```

---

## Running from the GUI

1. Complete a simulation run (click **Run** and wait for completion).
2. Click the **Sensitivity** button in the toolbar. The Sensitivity Analysis Dialog opens.
3. In the dialog:
   - **Mode**: Choose "LP-level" (fast) or "Config-level" (accurate)
   - **LP File**: For LP-level, browse to the exported `.lp` file from your simulation
   - **Parameters**: Check/uncheck parameters to include. Adjust bounds using the spin boxes.
   - **Samples (N)**: Set the base sample count (16-2048, default 64; higher = more accurate but slower)
   - **KPIs**: Select which output metrics to analyze
4. Click **Run Analysis**. A progress bar shows completion.
5. Results appear as:
   - A horizontal bar chart comparing S1 and ST for each parameter
   - A table of exact Sobol index values with confidence intervals
   - A KPI selector dropdown to switch between output metrics

---

## Running from the Python API

### LP-Level Analysis

```python
from esfex.sensitivity.engine import (
    SensitivityEngine,
    SobolResult,
    get_lp_parameters,
)

# Auto-detect parameters from LP file (written with ESFEX_MASTER_DEBUG_LP=1)
lp_path = "results/logs/master_problem_debug.lp"
parameters = get_lp_parameters(lp_path)

print(f"Found {len(parameters)} parameters:")
for p in parameters:
    print(f"  {p.name} ({p.category}): [{p.lower_bound}, {p.upper_bound}]")

# Create engine
engine = SensitivityEngine(
    mode="lp",
    parameters=parameters,
    kpi_names=["total_cost", "inv_gen_total", "inv_bat_total", "curtailment", "load_shedding"],
    n_base_samples=256,
)

print(f"Total evaluations required: {engine.n_evaluations}")

# Run with progress callback
def on_progress(current, total, message):
    if current % 100 == 0:
        print(f"  [{current}/{total}] {message}")

results = engine.run_lp_analysis(lp_path, progress_callback=on_progress)

# Display results
for kpi_name in results.kpi_names:
    print(f"\n--- {kpi_name} ---")
    print(f"{'Parameter':<30} {'S1':>8} {'ST':>8} {'S1_conf':>8} {'ST_conf':>8}")
    for i, param in enumerate(results.parameters):
        s1 = results.S1[kpi_name][i]
        st = results.ST[kpi_name][i]
        s1c = results.S1_conf[kpi_name][i]
        stc = results.ST_conf[kpi_name][i]
        print(f"{param:<30} {s1:8.4f} {st:8.4f} {s1c:8.4f} {stc:8.4f}")

# Export to CSV
results.to_csv("sensitivity_results.csv")
```

### Config-Level Analysis

```python
from esfex.sensitivity.engine import (
    SensitivityEngine,
    get_config_parameters,
)

# Predefined parameters, keeping only the ones that are applied
applied = {"invest_cost_renewables", "invest_cost_storage",
           "invest_cost_conventional", "fuel_cost"}
parameters = [p for p in get_config_parameters() if p.key in applied]

# Or use custom parameters (see above)
# parameters = custom_params

engine = SensitivityEngine(
    mode="config",
    parameters=parameters,
    kpi_names=["total_cost", "curtailment", "load_shedding"],
    n_base_samples=64,  # Lower for config-level (each evaluation is expensive)
)

print(f"Total evaluations: {engine.n_evaluations}")
# With 4 parameters and N=64: 64 * (4 + 2) = 384 full simulations

results = engine.run_config_analysis(
    base_config_path="island_system.yaml",
    output_dir="results/",          # runs go to results/sensitivity/run_0000, ...
    progress_callback=on_progress,
)

results.to_csv("config_sensitivity_results.csv")
```

---

## Interpreting Results

### Sobol Indices

| Index | Range | Interpretation |
|-------|-------|----------------|
| S1 ~ 0 | [0, 1] | Parameter has negligible direct effect on the output |
| S1 ~ 0.3 | [0, 1] | Parameter explains ~30% of output variance directly |
| S1 ~ 0.5 | [0, 1] | Parameter dominates; explains ~50% of output variance |
| S1 ~ 1.0 | [0, 1] | Parameter almost entirely determines the output |
| ST > S1 | [0, 1] | Significant interaction effects with other parameters |
| ST ~ S1 | [0, 1] | Minimal interaction effects; parameter acts independently |

### Example Output

Illustrative config-level output for the four applied parameters:

```
--- total_cost ---
Parameter                         S1       ST   S1_conf  ST_conf
Fuel Cost                       0.6123   0.6687   0.0312   0.0298
RE Investment Cost               0.1956   0.2434   0.0287   0.0265
Storage Investment Cost          0.0978   0.1312   0.0234   0.0221
Conv. Investment Cost            0.0234   0.0456   0.0123   0.0112

--- curtailment ---
Parameter                         S1       ST   S1_conf  ST_conf
Storage Investment Cost          0.4212   0.4923   0.0298   0.0287
RE Investment Cost               0.3187   0.3712   0.0276   0.0265
Fuel Cost                       0.0923   0.1387   0.0234   0.0221
Conv. Investment Cost            0.0176   0.0334   0.0156   0.0143
```

### Reading the Results

- **Total cost** is most sensitive to fuel cost (S1 = 0.61), which directly drives thermal generation expenditure. The ST-S1 gap (0.67 vs 0.61) indicates interaction with other parameters (e.g., higher fuel cost makes RE investment more attractive).

- **Curtailment** is most sensitive to storage and RE investment cost, which together explain about 74% of its variance: cheap storage absorbs surplus RE, cheap RE leads to more of it. The ST > S1 gap reflects solar-storage complementarity.

- **Conventional investment cost** has little effect when the existing thermal fleet covers demand and few conventional candidates are built.

### Tornado Diagram

The GUI displays a tornado-style horizontal bar chart with:

- Blue bars for first-order indices (S1) — direct effects
- Orange bars for total-order indices (ST) — direct + interaction effects
- Error bars showing confidence intervals

Parameters are sorted by ST (largest at top). A wide gap between blue and orange bars indicates strong interaction effects.

---

## Available KPIs

| KPI | LP-level | Config-level | Units |
|-----|----------|--------------|-------|
| `total_cost` | Objective of the perturbed master problem (investment + representative-day operation) | Sum of the yearly dispatch costs (`summary_results/total_cost`); investment cost is not included | $ |
| `inv_gen_total` | Total technology investment, all years | Not available | MW |
| `inv_bat_total` | Total battery-technology investment, all years | Not available | MW + MWh |
| `curtailment` | Sum of the master's curtailment variables | Total curtailment over all years | MWh |
| `load_shedding` | Sum of the master's load-shedding variables | Total unserved energy over all years (`summary_results/loss_of_load`) | MWh |

In config-level mode `inv_gen_total` and `inv_bat_total` are not extracted from the results, so their indices are always zero. A failed run counts as missing for every KPI and is replaced by a large value before the indices are computed, so check the run logs if the indices look odd.

---

## Computational Cost

Total model evaluations: N x (D + 2), where N = `n_base_samples` and D = number of parameters (Saltelli sampling without second-order indices).

| Parameters (D) | Samples (N) | Evaluations | LP-Level Time | Config-Level Time |
|----------------|-------------|-------------|---------------|-------------------|
| 4 | 64 | 384 | ~1 min | ~6 hours |
| 4 | 128 | 768 | ~1.5 min | ~13 hours |
| 5 | 128 | 896 | ~1.5 min | ~15 hours |
| 5 | 256 | 1,792 | ~3 min | ~30 hours |
| 5 | 1024 | 7,168 | ~12 min | ~120 hours |
| 9 | 256 | 2,816 | ~5 min | ~47 hours |

LP-level times assume ~0.1 seconds per solve. Config-level times assume ~1 minute per full simulation, which is only realistic for a very small system (each run covers 25 years).

### Choosing Sample Size

- **N = 64**: Minimum for meaningful Sobol indices. Confidence intervals will be wide. Use only for initial screening with config-level analysis.
- **N = 128-256**: Good balance of accuracy and computation. Recommended for most studies.
- **N = 512-1024**: High accuracy with tight confidence intervals. Use for publication-quality results.
- **N > 1024**: Rarely needed unless dealing with many parameters (D > 10) or highly nonlinear responses.

### Parallel Execution

The current implementation is sequential, but you can parallelize by splitting the sample matrix:

```python
# Generate all samples
samples = engine.generate_samples()

# Split for 4 workers
n_per_worker = len(samples) // 4
for worker_id in range(4):
    start = worker_id * n_per_worker
    end = start + n_per_worker
    worker_samples = samples[start:end]
    # Save and distribute to worker machines
```

---

## Common Parameters to Test

The uncertainties below are common in planning studies. Only some have a built-in parameter; the others have to be varied by editing the configuration and running separately (or, for an LP-level screen, through the matching LP group).

| Uncertainty | Typical Range | Built-in parameter |
|-----------|--------------|-----------|
| Fuel cost (diesel/gas) | [0.5x, 3.0x] | Config: `fuel_cost` |
| RE investment cost (solar/wind) | [0.4x, 1.5x] | Config: `invest_cost_renewables`; LP: `inv_tech_<t>` |
| Battery investment cost | [0.3x, 2.0x] | Config: `invest_cost_storage`; LP: `inv_bat_pow_<b>`, `inv_bat_cap_<b>` |
| Transmission investment cost | [0.5x, 2.0x] | LP: `inv_trans` |
| RE target | [0.8x, 1.2x] | LP: `re_target` (RHS) |
| Discount rate | [0.5x, 2.0x] | None |
| Demand growth | [0.5x, 2.0x] | None (write the demand years into the demand file) |
| EV adoption | [0.5x, 3.0x] | None |
| Carbon price | [0x, 5.0x] | None (`carbon_price` is listed but not applied) |
| RE availability | [0.8x, 1.2x] | None |

---

## Best Practices

1. **Start with LP-level**: Run LP-level analysis first with N=256 to quickly identify the most important parameters. Then use config-level analysis with the applied parameters and a lower N to refine.

2. **Check convergence**: Run the analysis with increasing N (64, 128, 256) and verify that Sobol indices stabilize. If they change significantly between N=128 and N=256, you need more samples.

3. **Interpret confidence intervals**: If `S1_conf` is larger than `S1`, the index is unreliable. Increase N or reduce the number of parameters.

4. **Sum check**: The sum of all S1 values should be close to 1.0 for linear models. For nonlinear models, the sum may be less than 1.0 (indicating interaction effects) or occasionally greater (numerical artifacts at low N).

5. **Focus resources**: Parameters with S1 < 0.05 and ST < 0.10 have negligible influence. Consider fixing them at base-case values in detailed studies to reduce computational burden.

6. **Report both S1 and ST**: S1 captures direct effects; ST captures total effects including interactions. A parameter with low S1 but high ST is not important by itself but significantly modifies the effect of other parameters.

7. **Connect to stochastic analysis**: Parameters with high ST values are natural candidates for stochastic scenario dimensions. If fuel cost has ST = 0.52, it warrants scenario analysis (see the [Stochastic Programming tutorial](stochastic.md)).

---

## Key Takeaways

1. **Global analysis**: Unlike one-at-a-time sweeps, Sobol indices capture parameter interactions and provide a complete picture of parameter importance.
2. **Prioritization**: Focus detailed modeling and data collection on the most influential parameters. Parameters with low Sobol indices can use rough estimates without significantly affecting results.
3. **Robustness**: Parameters with high ST warrant uncertainty analysis through stochastic scenarios or robust optimization.
4. **Trade-off**: More samples yield more accurate indices but require more computation. LP-level analysis provides fast screening; config-level provides definitive results.
5. **Decision support**: Sensitivity results help stakeholders understand which uncertainties matter most for their investment decisions and where to focus risk mitigation efforts.
