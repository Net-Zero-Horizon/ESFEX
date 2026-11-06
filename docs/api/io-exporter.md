# Exporter I/O
Module: `esfex.io.exporter`

## ResultsExporter

Read-only exporter that converts existing HDF5 results files to other formats.

```python
class ResultsExporter:
    def __init__(self, results_path: Union[str, Path]) -> None
```

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `results_path` | `str` or `Path` | Path to an existing HDF5 results file. |

**Raises:** `FileNotFoundError` if the results file does not exist.

### Methods

#### to_csv

```python
def to_csv(self, output_dir: Union[str, Path]) -> None
```

Export the datasets of `summary_results/`, each `detailed_results/<scenario>/` and `demand/` as one CSV file per dataset.

**Output structure:**

```
output_dir/
    summary/                      # /summary_results/*
        year.csv
        threshold.csv
        feasible.csv
        total_cost.csv
        renewable_penetration.csv
        co2_emissions.csv
        loss_of_load.csv
        n1_security_cost.csv
    year_2025_threshold_0/        # /detailed_results/year_2025_threshold_0/*
        generation/
            Solar PV.csv          # one file per dataset in the group
            ...
        battery_charge/
            ...
        curtailment.csv
        electricity_prices.csv
        CO2_emissions.csv
        ...
    year_2026_threshold_0/
        ...
    demand/                       # /demand/*
        year_2025_base_demand.csv
        ...
```

How datasets are laid out:

- 1-D and 2-D datasets keep their layout (for `[node, step]` datasets: one row per node, one column per step). A scalar dataset becomes a one-cell file.
- 3-D datasets (`power_flow` and `transfer_margin` of multi-node runs, `[from_node, to_node, step]`) get one row per node pair: the first two columns are `from_node` and `to_node`, then one column per step. Other datasets with more than two axes are written the same way, with index columns named `dim_0`, `dim_1`, ...
- Nested groups become nested folders (`technology_selling_prices/<gen>/prices_weights.csv`, `n1_security/trans_reserve_duals/*.csv`, `primary_energy/<section>/<fuel>.csv`).

Limitations:

- Attributes are not exported, so groups that hold only attributes (`cost_breakdown/`, `global/cost_breakdown/`) and all `mga/`, `global/`, `inter_system/` and `system_configuration/` content are left out.

#### to_excel

```python
def to_excel(self, output_path: Union[str, Path]) -> None
```

Export results to a single Excel workbook with two sheets: "Summary" (the `summary_results` datasets, one row per year) and "Generation" (first scenario only: one column per generator, summed across nodes, one row per timestep).

Uses `openpyxl` engine.

#### to_json

```python
def to_json(self, output_path: Union[str, Path]) -> None
```

Export the root attributes, the `summary_results` datasets and the attributes of each scenario to a JSON file. Attribute values are converted to strings. Time-series arrays are NOT exported to JSON (use CSV or HDF5 for full time-series data).

**JSON structure** (for a runner results file):

```json
{
  "metadata": {
    "creation_date": "2026-01-15T10:30:00",
    "num_nodes": "4",
    "num_years": "25",
    "simulation_mode": "development",
    "years_range": "2025-2049",
    "...": "..."
  },
  "summary": {
    "year": [2025, 2026, ...],
    "total_cost": [1234567.0, ...],
    "...": [...]
  },
  "scenarios": {
    "year_2025_threshold_0": {
      "year": "2025",
      "threshold": "0",
      "total_cost": "5432100.0",
      "renewable_penetration": "0.42",
      "...": "..."
    }
  }
}
```

### Example

```python
from esfex.io.exporter import ResultsExporter

exporter = ResultsExporter("results/isla_juventud_15012026_1030.h5")

# Export to CSV directory
exporter.to_csv("results/csv/")

# Export to Excel workbook
exporter.to_excel("results/report.xlsx")

# Export to JSON
exporter.to_json("results/data.json")
```

---

## export_system_results

```python
def export_system_results(
    results_dict: Dict[str, Any],
    generators: List[dict],
    batteries: List[dict],
    hours: int,
    num_nodes: int,
    output_filename: Optional[str] = None,
    output_dir: Union[str, Path] = "results",
    temporal_resolution_hours: int = 1,
) -> Path
```

Write optimization results to an HDF5 file in a legacy one-shot layout (data under `hourly_data/`). This is a standalone writer: the runner does not call it and writes its own files with `Orchestrator._initialize_hdf5()`, `_append_year_to_hdf5()` and `_finalize_hdf5()` (layout in [HDF5 Output Schema](../reference/hdf5-output-schema.md)). The layout below differs from runner files.

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `results_dict` | `dict` | required | Dictionary with optimization results keyed by scenario. Keys can be `(year, threshold)` tuples or string identifiers. |
| `generators` | `list[dict]` | required | List of generator configurations (used for names and metadata). |
| `batteries` | `list[dict]` | required | List of battery configurations. |
| `hours` | `int` | required | Number of hours in the simulation. |
| `num_nodes` | `int` | required | Number of nodes. |
| `output_filename` | `str` or `None` | `None` | Output filename. If `None`, auto-generates as `results_YYYYMMDD_HHMMSS.h5`. |
| `output_dir` | `str` or `Path` | `"results"` | Output directory (created if it does not exist). |
| `temporal_resolution_hours` | `int` | `1` | Temporal resolution of the data (for metadata). |

**Returns:** `Path` to the created HDF5 file.

### HDF5 File Structure

```
results_20260215_143000.h5
    [attrs]
        creation_date: "2026-02-15T14:30:00"
        hours: 8760
        num_nodes: 4
        temporal_resolution_hours: 1
        num_generators: 7
        num_batteries: 1
        generator_names: ["Solar_PV", "Wind", "Diesel", ...]
        battery_names: ["Li_Ion"]

    detailed_results/
        year_2025_threshold_0/
            [attrs]
                year: 2025
                threshold: 0
                objective: 5432100.0
                total_generation: 1234000.0
                renewable_generation: 987000.0
                renewable_penetration: 0.80
                co2_emissions: 12345.0

            hourly_data/
                generation/
                    Solar_PV  [nodes x hours]  (units: MW)
                    Wind      [nodes x hours]  (units: MW)
                    Diesel    [nodes x hours]  (units: MW)
                    ...

                battery_charge/
                    Li_Ion    [nodes x hours]  (units: MW)

                battery_discharge/
                    Li_Ion    [nodes x hours]  (units: MW)

                curtailment          [nodes x hours]  (units: MW)
                electricity_prices   [hours]           (units: $/MWh)
                nodal_electricity_prices [nodes x hours] (units: $/MWh)
                CO2_emissions        [hours]           (units: tonnes)
                power_flow           [from x to x hours] (units: MW)
                EV_charging          [nodes x hours]  (units: MW)
                EV_V2G              [nodes x hours]  (units: MW)
                loss_of_inertia      [hours]          (units: GW*s)

                capacity_factor/
                    Solar_PV  [array as given]  (dimensionless)
                    Wind      [array as given]  (dimensionless)
                    ...

                lcoe/
                    Solar_PV  [array as given]  (USD/MWh)
                    Diesel    [array as given]  (USD/MWh)
                    ...

                vallcoe/
                    Solar_PV  [array as given]  (USD/MWh)
                    ...

                technology_selling_prices/
                    Solar_PV/
                        [attrs] total_generation, total_revenue,
                                average_selling_price, technology_type
                        prices_weights [N x 3]  (price, generation, timestep)
                    ...
```

All time-series datasets use gzip compression and chunked storage for efficient access.

---

## Derived Metrics

The derived metrics in runner files (`capacity_factor/`, `lcoe/`, `vallcoe/`, `battery_*` and `technology_selling_prices/`) are computed by the runner's `Orchestrator._compute_derived_metrics()`, not by this module. They are hourly `(nodes, hours)` arrays per unit, not scalars. See [Results Export § Derived Metrics](../user-guide/results-export.md#derived-metrics) for the formulas.

---

## read_results

```python
def read_results(results_path: Union[str, Path]) -> Dict[str, Any]
```

Read results from an HDF5 file back into Python.

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `results_path` | `str` or `Path` | Path to HDF5 results file. |

**Returns:** Dictionary with structure:

```python
{
    "metadata": {
        "creation_date": "2026-02-15T14:30:00",
        "num_nodes": 4,
        ...                       # root attributes as stored
    },
    "scenarios": {
        "year_2025_threshold_0": {
            "attrs": {"year": 2025, "threshold": 0, "total_cost": 5432100.0, ...},
            "hourly_data": {      # key name kept for both formats
                "generation": {"Solar PV": np.ndarray, "Diesel": np.ndarray, ...},
                "battery_charge": {"Li_Ion": np.ndarray},
                "curtailment": np.ndarray,
                "electricity_prices": np.ndarray,
                ...
            }
        }
    }
}
```

Handles both HDF5 formats:
- Incremental format (runner files): data directly in the scenario group.
- One-shot format (`export_system_results`): data nested in an `hourly_data/` subgroup.

Only datasets directly in the scenario group, or one group level below it, are read. Deeper datasets (for example `technology_selling_prices/<gen>/prices_weights`) are skipped.

**Example:**

```python
from esfex.io.exporter import read_results

results = read_results("results/isla_juventud_15012026_1030.h5")
gen_data = results["scenarios"]["year_2030_threshold_0"]["hourly_data"]["generation"]
solar_output = gen_data["Solar PV"]  # np.ndarray shape (nodes, hours)
print(f"Peak solar: {solar_output.max():.1f} MW")
```
