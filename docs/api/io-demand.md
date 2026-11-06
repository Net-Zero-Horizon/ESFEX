# Demand I/O
Module: `esfex.io.demand`

## Functions

### load_demand_data

```python
def load_demand_data(
    file_path: Union[str, Path],
    date_start: str = "01/01/2025 00:00",
    year_to_load: Optional[int] = None
) -> Tuple[np.ndarray, int, int, List[int], List[datetime]]
```

Load demand data from an Excel (.xlsx, .xls) or CSV file.

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `file_path` | `str` or `Path` | required | Path to the demand data file. |
| `date_start` | `str` | `"01/01/2025 00:00"` | Timestamp of the first row, `"DD/MM/YYYY HH:MM"`. Gives the first year label, the time index and the calendar used to find Feb 29. |
| `year_to_load` | `int` or `None` | `None` | If specified, loads only that model year: the 8,760-row block number `year_to_load - year of date_start`. If `None`, loads all data in the file. |

**Returns:** A 5-tuple:

| Index | Type | Description |
|-------|------|-------------|
| 0 | `np.ndarray` | Demand array with shape `(hours, num_nodes)` in MW. |
| 1 | `int` | Total number of hours loaded. |
| 2 | `int` | Number of nodes (columns in the file). |
| 3 | `list[int]` | Model years in the data, one per started 8,760-row block from the year of `date_start` (e.g., `[2025, 2026, ..., 2049]`). |
| 4 | `list[datetime]` | Time index -- one `datetime` object per hour, skipping Feb 29. |

**Raises:**
- `FileNotFoundError` if the demand file does not exist.
- `ValueError` if `year_to_load` is not one of the model years in the data.

**Behavior:**

- The file is read like a simulation run reads it: data start in the first row, leading rows with non-numeric cells (headers) are skipped, and Feb 29 rows of leap-year data are dropped (`esfex.utils.temporal.drop_leap_days`), so every year is 8,760 rows.
- When `year_to_load` is specified, only that year's 8,760-row block is returned, with its time index.

**Demand File Format:**

Hourly power demand values in MW. Each column represents a node. A header row is optional, for CSV and Excel alike.

| Column 0 | Column 1 | Column 2 | ... |
|----------|----------|----------|-----|
| 120.5 | 85.3 | 200.1 | ... |
| 118.2 | 84.1 | 198.7 | ... |
| ... | ... | ... | ... |

For a 25-year simulation with hourly resolution, the file contains `25 * 8760 = 219,000` rows.

**Multi-Year Demand and Growth Rates:**

Demand growth is NOT applied inside `load_demand_data()`. The function loads raw data as-is.

`load_demand_data()` is a standalone helper; the runner does not call it. `Orchestrator._load_demand()` reads each system's demand itself:

- It uses `demand_paths` (one file per node, stacked side by side) if set, otherwise `demand_path` (one column per node).
- Files are read with `read_hourly_table()`, as in `load_demand_data()`: header rows are skipped and Feb 29 rows are dropped.
- If the file is shorter than the simulation horizon, the first year is repeated to fill it.
- The system's `demand_scale` is applied, and the systems of the meta-network are stacked side by side into one `(hours, nodes)` array.

The runner passes every year of demand to the master problem and sets `MasterProblemAdapter(demand_growth=0.0)`, so the master uses the file values unchanged. To model growth, provide a multi-year file.

**Example:**

```python
from esfex.io.demand import load_demand_data

# Load all years
demand, hours, nodes, years, time_idx = load_demand_data(
    "data/demand.xlsx",
    date_start="01/01/2025 00:00"
)
print(f"Loaded {hours} hours, {nodes} nodes, years: {years}")

# Load only 2030
demand_2030, h, n, _, t = load_demand_data(
    "data/demand.xlsx",
    date_start="01/01/2025 00:00",
    year_to_load=2030
)
```

### create_sectoral_demand

```python
def create_sectoral_demand(
    base_demand: np.ndarray,
    sector_distribution: Dict[int, Dict[str, float]],
    sectors_list: Optional[List[str]] = None
) -> Dict[str, np.ndarray]
```

Distribute total demand into sector-specific demand arrays.

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `base_demand` | `np.ndarray` | Total demand array with shape `(hours, num_nodes)`. |
| `sector_distribution` | `dict` | Per-node sectoral fractions. Format: `{node_idx: {sector_name: proportion}}`. |
| `sectors_list` | `list[str]` or `None` | Ordered list of sectors to include. If `None`, automatically detects all sector names from `sector_distribution`. |

**Returns:** Dictionary mapping sector name to demand array, each with shape `(hours, num_nodes)`.

**Normalization:**

Proportions are normalized per node to ensure `sum(sectoral_demand) == base_demand`. If proportions for a node sum to a value significantly different from 1.0, a warning is logged. If proportions sum to 0 for a node, demand is distributed equally across all sectors.

**Example:**

```python
from esfex.io.demand import create_sectoral_demand

sector_dist = {
    0: {"residential": 0.4, "commercial": 0.35, "industrial": 0.25},
    1: {"residential": 0.5, "commercial": 0.3, "industrial": 0.2},
}

sectoral = create_sectoral_demand(demand_array, sector_dist)
# sectoral["residential"].shape == (hours, num_nodes)
# sectoral["commercial"].shape == (hours, num_nodes)
# sectoral["industrial"].shape == (hours, num_nodes)
```

### load_availability_profile

```python
def load_availability_profile(
    file_path: Union[str, Path],
    temporal_resolution_hours: int = 1,
    num_nodes: Optional[int] = None,
    date_start: Optional[str] = None,
) -> np.ndarray
```

Load a generator availability profile from an Excel or CSV file.

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `file_path` | `str` or `Path` | required | Path to the availability data file. |
| `temporal_resolution_hours` | `int` | `1` | Target temporal resolution. If > 1, the hourly data is aggregated using MEAN (appropriate for capacity factors). |
| `num_nodes` | `int` or `None` | `None` | Expected number of nodes. If provided, the columns are broadcast, extended or truncated to match (see below). |
| `date_start` | `str` or `None` | `None` | Timestamp of the first row (`"DD/MM/YYYY HH:MM"`), used to find Feb 29 rows. The run passes the config's `date_start`. |

**Returns:** Array of shape `(hours, nodes)` with values clipped to [0, 1].

**Behavior:**

- If the file does not exist, or reading it fails, logs a warning or error and returns an array of ones with shape `(8760 // temporal_resolution_hours, num_nodes)` (10 columns if `num_nodes` is `None`).
- Header rows (leading rows with non-numeric cells) are skipped. Feb 29 rows are dropped so every year is 8,760 rows: by the calendar from `date_start` when given, and always for a file of exactly 8,784 rows.
- A single-column file is copied to every node.
- If the file has more than one column but fewer than `num_nodes`, the last column is repeated for the missing nodes.
- If the file has more columns than `num_nodes`, truncates to the expected number.
- Values are clipped to [0, 1] after loading.
- If `temporal_resolution_hours > 1`, applies `aggregate_to_resolution()` using MEAN aggregation.

### load_inflow_profile

```python
def load_inflow_profile(
    file_path: Union[str, Path],
    temporal_resolution_hours: int = 1,
    num_nodes: Optional[int] = None,
    date_start: Optional[str] = None,
) -> np.ndarray
```

Load a reservoir inflow profile (MW-eq per hour). Rows, columns, header rows and Feb 29 are handled as in `load_availability_profile`, but the values are flows: they are **not** clipped to [0, 1], and a missing file raises `FileNotFoundError` instead of returning ones. With `temporal_resolution_hours > 1` the steps are averaged.

**Availability File Format:**

Same structure as demand files: one column per node, one row per hour, values between 0 and 1.

| Node 0 | Node 1 | Node 2 |
|--------|--------|--------|
| 0.0 | 0.0 | 0.0 |
| 0.0 | 0.0 | 0.0 |
| 0.15 | 0.12 | 0.18 |
| 0.45 | 0.40 | 0.50 |
| 0.82 | 0.75 | 0.88 |
| ... | ... | ... |

### extract_year_profile

```python
def extract_year_profile(
    full_profile: Union[pd.DataFrame, np.ndarray],
    time_index: List[datetime],
    hours: int,
) -> np.ndarray
```

Extract profile data for a specific year from a full multi-year profile.

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `full_profile` | `DataFrame` or `ndarray` | Full multi-year profile data. |
| `time_index` | `list[datetime]` | Time index for the target year. |
| `hours` | `int` | Number of hours to extract. |

**Returns:** Profile array for the year with shape `(hours, ...)`.

For DataFrames, filters by `index.year == time_index[0].year`. For NumPy arrays, takes the first `hours` rows.

---

## DemandDataManager

Converts large Excel/CSV demand files to HDF5 format for fast year-by-year access.

```python
class DemandDataManager:
    def __init__(
        self,
        excel_path: Union[str, Path],
        date_start: str = "01/01/2025 00:00",
        time_step: int = 1,
    )
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `excel_path` | `str` or `Path` | required | Path to the Excel or CSV demand file. |
| `date_start` | `str` | `"01/01/2025 00:00"` | Timestamp of the first row; its year is the first year in the index. |
| `time_step` | `int` | `1` | Time step in hours. |

### prepare_hdf5_storage

```python
def prepare_hdf5_storage(self) -> Path
```

Convert the Excel/CSV file to an HDF5 file with year-indexed access. Rows are read as in `load_demand_data()` (Feb 29 is dropped for hourly data), and each year is a block of `8760 / time_step` rows.

**Returns:** Path to the created HDF5 temporary file.

The HDF5 file contains:
- `demand` dataset: Full demand array with gzip compression.
- `year_index/` group: Per-year `[start_idx, end_idx]` arrays for fast slicing.
- Metadata attributes: `total_hours`, `num_nodes`, `start_date`, `end_date`, `start_year`, `end_year`, `time_step`.

### load_year_data

```python
def load_year_data(self, year: int) -> Tuple[np.ndarray, int, int, List[datetime]]
```

Load data for a specific year from the prepared HDF5 file.

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `year` | `int` | Year to load. |

**Returns:** A 4-tuple of `(demand_array, hours_in_year, num_nodes, time_index)`.

**Raises:** `ValueError` if HDF5 is not prepared or the year is not found.

### cleanup

```python
def cleanup(self) -> None
```

Remove the temporary HDF5 file.

**Example:**

```python
from esfex.io.demand import DemandDataManager

mgr = DemandDataManager("data/demand_25years.xlsx", date_start="01/01/2025 00:00")
hdf5_path = mgr.prepare_hdf5_storage()

for year in range(2025, 2050):
    demand, hours, nodes, time_idx = mgr.load_year_data(year)
    print(f"Year {year}: {hours} hours, peak={demand.max():.1f} MW")

mgr.cleanup()
```
