"""
Demand data loading and processing for ESFEX.

Provides functions to load demand data from files and create
sectoral demand distributions.
"""

import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import h5py
import numpy as np
import pandas as pd

from esfex.utils.temporal import (
    HOURS_STD_YEAR,
    drop_leap_days,
    model_time_index,
    parse_date_start,
)

logger = logging.getLogger(__name__)


def _read_tabular(file_path: Path, **kwargs) -> pd.DataFrame:
    """Read a tabular file (Excel or CSV) into a DataFrame.

    Dispatches to ``pd.read_excel`` or ``pd.read_csv`` based on the file
    extension.  CSV files are assumed to have **no header row** (pure
    numeric data), consistent with per-node demand CSVs.
    """
    suffix = file_path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(file_path, header=None, **kwargs)
    # Default: treat as Excel (.xlsx, .xls, …) — no header row (pure numeric data)
    return pd.read_excel(file_path, header=None, **kwargs)


def read_hourly_table(file_path: Union[str, Path]) -> np.ndarray:
    """Read an hourly table (CSV or Excel) as a float array (rows = hours).

    Data start in the first row. Leading rows with a non-numeric cell (a
    header such as node names) are skipped, so files with or without a
    header row give the same hours. A non-numeric cell below the header
    raises ``ValueError``.
    """
    df = _read_tabular(Path(file_path))
    numeric = df.apply(pd.to_numeric, errors="coerce")
    text_cells = (numeric.isna() & df.notna()).to_numpy().any(axis=1)
    first = 0
    while first < len(df) and text_cells[first]:
        first += 1
    if first:
        logger.debug(f"{file_path}: skipped {first} header row(s)")
    bad = np.flatnonzero(text_cells[first:])
    if bad.size:
        raise ValueError(
            f"{file_path}: non-numeric value in data row {first + int(bad[0]) + 1}"
        )
    return numeric.iloc[first:].to_numpy(dtype=float)


def load_demand_data(
    file_path: Union[str, Path],
    date_start: str = "01/01/2025 00:00",
    year_to_load: Optional[int] = None,
) -> Tuple[np.ndarray, int, int, List[int], List[datetime]]:
    """
    Load demand data from a CSV or Excel file, as a simulation run reads it.

    The first data row is the hour at ``date_start``. Every year is an
    8760-row block: Feb 29 rows are dropped (see
    :func:`esfex.utils.temporal.drop_leap_days`) and the time labels skip
    Feb 29.

    Parameters
    ----------
    file_path : str or Path
        Path to the demand file (one row per hour, one column per node)
    date_start : str
        Start date and time in format "DD/MM/YYYY HH:MM"
    year_to_load : int, optional
        If specified, loads only that year: the 8760-row block number
        ``year_to_load - year of date_start``. If None, loads all data.

    Returns
    -------
    tuple
        (demand_array, hours, num_nodes, years_list, time_index)
        - demand_array: numpy array with shape (hours, num_nodes)
        - hours: number of hours
        - num_nodes: number of nodes
        - years_list: model years covered by the data (one per 8760 rows)
        - time_index: list of datetime objects for each hour
    """
    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(f"Demand file not found: {file_path}")

    start_date = parse_date_start(date_start)
    demand = drop_leap_days(read_hourly_table(file_path), start_date)
    total_hours, num_nodes = demand.shape
    num_years = max(1, -(-total_hours // HOURS_STD_YEAR))
    years_to_analyze = list(range(start_date.year, start_date.year + num_years))

    if year_to_load is None:
        time_index = list(model_time_index(start_date, total_hours).to_pydatetime())
        logger.info(f"Loaded demand: {total_hours} hours, {num_nodes} nodes")
        return demand, total_hours, num_nodes, years_to_analyze, time_index

    if year_to_load not in years_to_analyze:
        raise ValueError(
            f"Year {year_to_load} not in demand data "
            f"({years_to_analyze[0]}-{years_to_analyze[-1]})"
        )
    start_row = (year_to_load - start_date.year) * HOURS_STD_YEAR
    demand = demand[start_row:start_row + HOURS_STD_YEAR]
    hours = demand.shape[0]
    time_index = list(
        model_time_index(start_date, start_row + hours)[start_row:].to_pydatetime()
    )

    logger.info(f"Loaded demand for year {year_to_load}: {hours} hours, {num_nodes} nodes")

    return demand, hours, num_nodes, years_to_analyze, time_index


def create_sectoral_demand(
    base_demand: np.ndarray,
    sector_distribution: Dict[int, Dict[str, float]],
    sectors_list: Optional[List[str]] = None,
) -> Dict[str, np.ndarray]:
    """
    Divide base demand into sectoral demands.

    Parameters
    ----------
    base_demand : np.ndarray
        Base demand array with shape (hours, num_nodes)
    sector_distribution : dict
        Distribution percentages by node and sector
        Format: {node_idx: {sector_name: proportion}}
    sectors_list : list, optional
        List of sectors to include. If None, uses all sectors.

    Returns
    -------
    dict
        Sectoral demand by sector name, each with shape (hours, num_nodes)

    Notes
    -----
    Proportions are normalized per node to ensure sum(sectoral_demand) == base_demand.
    """
    hours = base_demand.shape[0]
    num_nodes = base_demand.shape[1] if base_demand.ndim > 1 else 1

    # Get all sectors if not specified
    if sectors_list is None:
        all_sectors = set()
        for node_dist in sector_distribution.values():
            all_sectors.update(node_dist.keys())
        sectors_list = list(all_sectors)

    # Initialize sectoral demand
    sectoral_demand = {}
    for sector in sectors_list:
        sectoral_demand[sector] = np.zeros((hours, num_nodes))

    # Pre-calculate normalized proportions per node
    normalized_proportions = {}
    normalization_warnings = []

    for node in range(num_nodes):
        node_distribution = sector_distribution.get(node, sector_distribution.get(0, {}))

        # Calculate sum of proportions for sectors in sectors_list
        total_proportion = sum(
            prop for sector, prop in node_distribution.items()
            if sector in sectors_list
        )

        # Warn if proportions don't sum to ~1.0
        if abs(total_proportion - 1.0) > 0.01 and total_proportion > 0:
            normalization_warnings.append(
                f"Node {node}: sector proportions sum to {total_proportion:.3f}"
            )

        # Normalize proportions
        if total_proportion > 0:
            normalized_proportions[node] = {
                sector: prop / total_proportion
                for sector, prop in node_distribution.items()
                if sector in sectors_list
            }
        else:
            # Distribute equally if no valid proportions
            n_sectors = len(sectors_list)
            normalized_proportions[node] = {
                sector: 1.0 / n_sectors for sector in sectors_list
            }

    # Print warnings once
    if normalization_warnings:
        logger.warning(
            f"Sector distribution normalized for {len(normalization_warnings)} nodes"
        )

    # Distribute demand by sector
    for t in range(hours):
        for node in range(num_nodes):
            node_props = normalized_proportions[node]
            base_value = base_demand[t, node] if base_demand.ndim > 1 else base_demand[t]

            for sector, proportion in node_props.items():
                sectoral_demand[sector][t, node] = base_value * proportion

    return sectoral_demand


class DemandDataManager:
    """
    Manager for efficient demand data access using HDF5.

    Converts Excel demand files to HDF5 for faster year-by-year loading.
    """

    def __init__(
        self,
        excel_path: Union[str, Path],
        date_start: str = "01/01/2025 00:00",
        time_step: int = 1,
    ):
        """
        Initialize the demand data manager.

        Parameters
        ----------
        excel_path : str or Path
            Path to the Excel demand file
        date_start : str
            Start date in format "DD/MM/YYYY HH:MM"
        time_step : int
            Time step in hours (default 1)
        """
        self.excel_path = Path(excel_path)
        self.date_start = date_start
        self.time_step = time_step
        self.hdf5_path: Optional[Path] = None
        self.metadata: Dict[str, Any] = {}

    def prepare_hdf5_storage(self) -> Path:
        """
        Convert the demand file to HDF5 for efficient access.

        Rows are read like :func:`load_demand_data` (header rows skipped,
        Feb 29 dropped for hourly data) and indexed by model year: one
        block of ``8760 / time_step`` rows per year from the year of
        ``date_start``.

        Returns
        -------
        Path
            Path to the created HDF5 file
        """
        logger.info("Converting demand from Excel to HDF5...")

        start_date = parse_date_start(self.date_start)
        demand_array = read_hourly_table(self.excel_path)
        if self.time_step == 1:
            demand_array = drop_leap_days(demand_array, start_date)
        total_hours, num_nodes = demand_array.shape
        # Model years are 8760-h blocks; labels skip Feb 29.
        steps_per_year = HOURS_STD_YEAR // self.time_step
        num_years = max(1, -(-total_hours // steps_per_year))
        end_date = model_time_index(start_date, total_hours, self.time_step)[-1]

        # Create temporary HDF5 file
        temp_dir = tempfile.gettempdir()
        self.hdf5_path = Path(temp_dir) / f"demand_data_{os.getpid()}.h5"

        with h5py.File(self.hdf5_path, "w") as f:
            # Store demand data
            f.create_dataset(
                "demand",
                data=demand_array,
                dtype="float32",
                compression="gzip"
            )

            # Store metadata
            f.attrs["total_hours"] = int(total_hours)
            f.attrs["num_nodes"] = int(num_nodes)
            f.attrs["start_date"] = start_date.strftime("%Y-%m-%d %H:%M:%S")
            f.attrs["end_date"] = end_date.strftime("%Y-%m-%d %H:%M:%S")
            f.attrs["start_year"] = int(start_date.year)
            f.attrs["end_year"] = int(start_date.year + num_years - 1)
            f.attrs["time_step"] = int(self.time_step)

            # Year index for fast access: one block of steps_per_year rows per year
            year_index = f.create_group("year_index")
            for k in range(num_years):
                year_index.create_dataset(
                    str(start_date.year + k),
                    data=[k * steps_per_year, min((k + 1) * steps_per_year, total_hours)],
                )

        # Store metadata in memory
        self.metadata = {
            "total_hours": int(total_hours),
            "num_nodes": int(num_nodes),
            "start_date": start_date,
            "end_date": end_date.to_pydatetime(),
            "years": list(range(start_date.year, start_date.year + num_years))
        }

        logger.info(f"HDF5 created: {self.hdf5_path}")
        logger.info(f"Years available: {self.metadata['years']}")

        return self.hdf5_path

    def load_year_data(
        self,
        year: int,
    ) -> Tuple[np.ndarray, int, int, List[datetime]]:
        """
        Load data for a specific year from HDF5.

        Parameters
        ----------
        year : int
            Year to load

        Returns
        -------
        tuple
            (demand_array, hours, num_nodes, time_index)
        """
        if self.hdf5_path is None or not self.hdf5_path.exists():
            raise ValueError("HDF5 not prepared. Run prepare_hdf5_storage() first.")

        with h5py.File(self.hdf5_path, "r") as f:
            if str(year) not in f["year_index"]:
                raise ValueError(f"Year {year} not found in data")

            # Get year indices
            start_idx, end_idx = f["year_index"][str(year)][:]
            start_idx = int(start_idx)
            end_idx = int(end_idx)

            # Load year data
            year_demand = f["demand"][start_idx:end_idx, :]

            # Calculate time index
            start_date = datetime.strptime(
                f.attrs["start_date"],
                "%Y-%m-%d %H:%M:%S"
            )
            hours_in_year = end_idx - start_idx
            time_index = list(
                model_time_index(start_date, end_idx, self.time_step)[start_idx:]
                .to_pydatetime()
            )

            num_nodes = int(f.attrs["num_nodes"])

        logger.info(f"Loaded year {year}: {hours_in_year} hours, {num_nodes} nodes")

        return year_demand, hours_in_year, num_nodes, time_index

    def cleanup(self) -> None:
        """Remove temporary HDF5 file."""
        if self.hdf5_path and self.hdf5_path.exists():
            os.remove(self.hdf5_path)
            logger.info(f"Cleaned up: {self.hdf5_path}")


def extract_year_profile(
    full_profile: Union[pd.DataFrame, np.ndarray],
    time_index: List[datetime],
    hours: int,
) -> np.ndarray:
    """
    Extract profile data for a specific year.

    Parameters
    ----------
    full_profile : DataFrame or ndarray
        Full profile data
    time_index : list
        Time index for the target year
    hours : int
        Number of hours to extract

    Returns
    -------
    np.ndarray
        Profile data for the year
    """
    if isinstance(full_profile, pd.DataFrame):
        year = time_index[0].year
        year_profile = full_profile[full_profile.index.year == year]
        return year_profile.values[:hours]
    elif isinstance(full_profile, np.ndarray):
        return full_profile[:hours]
    else:
        raise ValueError(f"Unsupported profile type: {type(full_profile)}")


def load_availability_profile(
    file_path: Union[str, Path],
    temporal_resolution_hours: int = 1,
    num_nodes: Optional[int] = None,
    date_start: Optional[str] = None,
) -> np.ndarray:
    """
    Load availability profile from a CSV or Excel file.

    Rows are hours (header rows are skipped). Feb 29 rows are dropped so
    every year is 8760 rows: by the calendar from ``date_start`` when given,
    and always for a file of exactly 8784 rows.

    Parameters
    ----------
    file_path : str or Path
        Path to availability Excel file
    temporal_resolution_hours : int
        Temporal resolution for aggregation (1 = hourly, 6 = 6-hourly, etc.)
    num_nodes : int, optional
        Expected number of nodes. If provided, validates the file.
    date_start : str, optional
        Timestamp of the first row ("DD/MM/YYYY HH:MM"), used to find Feb 29.

    Returns
    -------
    np.ndarray
        Availability array with shape (hours, nodes), values in [0, 1]
    """
    from esfex.utils.temporal import aggregate_to_resolution

    file_path = Path(file_path)
    if not file_path.exists():
        logger.warning(f"Availability file not found: {file_path}, using default 1.0")
        hours = HOURS_STD_YEAR // temporal_resolution_hours
        nodes = num_nodes or 10
        return np.ones((hours, nodes))

    try:
        availability = drop_leap_days(
            read_hourly_table(file_path),
            parse_date_start(date_start) if date_start else None,
        )

        # Adjust columns to match num_nodes.
        # Availability is a per-generator temporal profile, so a single-column
        # file is the common case — broadcast it to all nodes.
        if num_nodes is not None and availability.shape[1] != num_nodes:
            if availability.shape[1] == 1:
                # Single profile → replicate for every node
                availability = np.tile(availability, (1, num_nodes))
            elif availability.shape[1] < num_nodes:
                # Partial columns → replicate the last column for missing nodes
                padding = np.tile(
                    availability[:, -1:],
                    (1, num_nodes - availability.shape[1]),
                )
                availability = np.hstack([availability, padding])
            else:
                # More columns than nodes → truncate
                availability = availability[:, :num_nodes]

        # Clip to [0, 1]
        availability = np.clip(availability, 0.0, 1.0)

        # Apply temporal aggregation using MEAN (availability is a capacity factor)
        if temporal_resolution_hours > 1:
            availability = aggregate_to_resolution(availability, temporal_resolution_hours)

        logger.debug(
            f"Loaded availability from {file_path}: shape={availability.shape}, "
            f"mean={availability.mean():.3f}"
        )
        return availability

    except Exception as e:
        logger.error(f"Error loading availability from {file_path}: {e}")
        hours = HOURS_STD_YEAR // temporal_resolution_hours
        nodes = num_nodes or 10
        return np.ones((hours, nodes))
