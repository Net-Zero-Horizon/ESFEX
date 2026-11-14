"""
Temporal resolution utilities for ESFEX.

Provides functions to aggregate time series data to coarser
temporal resolutions (e.g., hourly → 3-hourly) using mean or max aggregation.
"""

import warnings
from datetime import datetime
from typing import Optional, Union

import numpy as np
import pandas as pd

# Hours in every modelled year. Demand and profiles are read in 8760-h blocks,
# so leap years are modelled as 8760 h too: Feb 29 is dropped from input data
# (drop_leap_days) and from result date labels (model_time_index).
HOURS_STD_YEAR: int = 8760

# Years a run simulates when none is given (``esfex run`` without --years,
# ``Orchestrator.run(years=None)``, a Studio run). The Studio's interruptions
# calendar spans the same horizon.
DEFAULT_RUN_YEARS: int = 25

# Feb 29 in an hourly series that starts on 1 Jan of a leap year.
_FEB29_START = 59 * 24
_FEB29_END = _FEB29_START + 24


def aggregate_to_resolution(
    data: Union[np.ndarray, pd.DataFrame],
    target_hours: int,
) -> Union[np.ndarray, pd.DataFrame]:
    """
    Aggregate time series to target resolution using mean.

    Parameters
    ----------
    data : np.ndarray or pd.DataFrame
        Hourly time series with shape (timesteps,) or (timesteps, nodes, ...)
        Assumed to be at 1-hour resolution (8760 timesteps/year)
    target_hours : int
        Target resolution in hours. Must be positive integer.
        Examples: 1 (hourly), 3 (3-hourly), 6 (6-hourly), 12, 24

    Returns
    -------
    np.ndarray or pd.DataFrame
        Aggregated time series with reduced timesteps (same type as input)
        - target_hours=1: Returns data unchanged
        - target_hours=3: 8760 → 2920 timesteps
        - target_hours=6: 8760 → 1460 timesteps

    Examples
    --------
    >>> hourly_demand = np.array([100, 110, 105, 120, 115, 125])
    >>> demand_3h = aggregate_to_resolution(hourly_demand, target_hours=3)
    >>> print(demand_3h)
    [105. 120.]  # Mean of [100,110,105] and [120,115,125]

    Notes
    -----
    - Uses mean aggregation: appropriate for availability (capacity factor)
    - For demand, consider using aggregate_demand_to_resolution() with max
    - If data length is not divisible by target_hours, excess timesteps are truncated
    """
    # Validation
    if not isinstance(target_hours, (int, np.integer)):
        raise TypeError(f"target_hours must be integer, got {type(target_hours)}")

    if target_hours < 1:
        raise ValueError(f"target_hours must be positive, got {target_hours}")

    # No aggregation needed
    if target_hours == 1:
        return data

    # Handle pandas DataFrame
    is_dataframe = isinstance(data, pd.DataFrame)
    if is_dataframe:
        columns = data.columns
        index_name = data.index.name
        data_array = data.values
    else:
        data_array = data

    # Calculate number of output timesteps
    n_input = data_array.shape[0]
    n_output = n_input // target_hours
    n_valid = n_output * target_hours

    # Truncate to valid length
    data_valid = data_array[:n_valid]

    # Reshape and aggregate
    if data_array.ndim == 1:
        reshaped = data_valid.reshape(n_output, target_hours)
        result = reshaped.mean(axis=1)
    else:
        new_shape = (n_output, target_hours) + data_array.shape[1:]
        reshaped = data_valid.reshape(new_shape)
        result = reshaped.mean(axis=1)

    # Convert back to DataFrame if input was DataFrame
    if is_dataframe:
        result = pd.DataFrame(result, columns=columns)
        if index_name:
            result.index.name = index_name

    return result


def aggregate_demand_to_resolution(
    data: Union[np.ndarray, pd.DataFrame],
    target_hours: int,
) -> Union[np.ndarray, pd.DataFrame]:
    """
    Aggregate demand time series to target resolution using MEAN.

    The aggregated value represents the *average power* (MW) over each
    period, so that when the LP multiplies it by ``target_hours`` to
    compute energy (MWh), the annual total matches the original hourly
    sum.  Using MAX (the prior behaviour) treated each aggregated
    step's value as if it had been the demand for the whole period,
    which inflated capacity-adequacy requirements and — more
    importantly — made the energy accounting incorrect: the LP's
    reported annual demand became (peak/avg) × actual demand, which
    for typical load profiles (peak/avg ≈ 1.7) means the model only
    "saw" ~60 % of the real energy, leaving the rest to appear as
    spurious load shedding when balanced against fully-rated supply.

    For capacity planning, the operational LP itself dispatches at
    each (aggregated) step, so the worst step within the period still
    becomes a binding capacity constraint — MEAN aggregation does not
    underestimate the peak there.  The PEAK-per-period concept is
    preserved separately in the representative-day / TSAM selection
    in the master problem, which uses raw hourly profiles.

    Parameters
    ----------
    data : np.ndarray or pd.DataFrame
        Hourly demand time series with shape (timesteps,) or (timesteps, nodes)
    target_hours : int
        Target resolution in hours. Must be positive integer.

    Returns
    -------
    np.ndarray or pd.DataFrame
        Aggregated time series with reduced timesteps
        Each value represents the MEAN demand within that period

    Examples
    --------
    >>> hourly_demand = np.array([100, 150, 120, 180, 140, 110])
    >>> demand_6h = aggregate_demand_to_resolution(hourly_demand, target_hours=6)
    >>> print(demand_6h)
    [133.33]  # MEAN of [100,150,120,180,140,110]; energy preserved as 800 MWh

    Notes
    -----
    - Uses MEAN aggregation: preserves annual energy sum once the LP
      multiplies the (MW, MEAN) value by ``target_hours`` to obtain MWh.
    - For renewable availability profiles, also use mean (already the
      default in :func:`aggregate_to_resolution`).
    """
    # Validation
    if not isinstance(target_hours, (int, np.integer)):
        raise TypeError(f"target_hours must be integer, got {type(target_hours)}")

    if target_hours < 1:
        raise ValueError(f"target_hours must be positive, got {target_hours}")

    # No aggregation needed
    if target_hours == 1:
        return data

    # Handle pandas DataFrame
    is_dataframe = isinstance(data, pd.DataFrame)
    if is_dataframe:
        columns = data.columns
        index_name = data.index.name
        data_array = data.values
    else:
        data_array = data

    # Calculate number of output timesteps
    n_input = data_array.shape[0]
    n_output = n_input // target_hours
    n_valid = n_output * target_hours

    # Check for truncation
    if n_valid < n_input:
        n_truncated = n_input - n_valid
        warnings.warn(
            f"Input length {n_input} not divisible by target_hours {target_hours}. "
            f"Last {n_truncated} timesteps will be truncated.",
            UserWarning
        )

    # Truncate to valid length
    data_valid = data_array[:n_valid]

    # Reshape and aggregate using MEAN to preserve total energy.
    if data_array.ndim == 1:
        reshaped = data_valid.reshape(n_output, target_hours)
        result = reshaped.mean(axis=1)
    else:
        new_shape = (n_output, target_hours) + data_array.shape[1:]
        reshaped = data_valid.reshape(new_shape)
        result = reshaped.mean(axis=1)

    # Convert back to DataFrame if input was DataFrame
    if is_dataframe:
        result = pd.DataFrame(result, columns=columns)
        if index_name:
            result.index.name = index_name

    return result


def validate_hourly_data(
    data: np.ndarray,
    expected_hours: int = HOURS_STD_YEAR,
    data_name: str = "data",
) -> bool:
    """
    Validate that input data has expected hourly resolution.

    Parameters
    ----------
    data : np.ndarray
        Input time series
    expected_hours : int
        Expected number of hours (default: 8760 for yearly)
    data_name : str
        Name of data for error messages

    Returns
    -------
    bool
        True if validation passes

    Raises
    ------
    ValueError
        If data length doesn't match expected hours
    """
    actual_length = data.shape[0]

    if actual_length != expected_hours:
        raise ValueError(
            f"{data_name} length {actual_length} does not match expected {expected_hours} hours. "
            f"Input data should be at hourly resolution."
        )

    return True


def get_aggregated_timesteps(original_hours: int, target_hours: int) -> int:
    """
    Calculate number of timesteps after aggregation.

    Parameters
    ----------
    original_hours : int
        Original number of hours (e.g., 8760)
    target_hours : int
        Target resolution in hours

    Returns
    -------
    int
        Number of timesteps after aggregation

    Examples
    --------
    >>> get_aggregated_timesteps(8760, 1)
    8760
    >>> get_aggregated_timesteps(8760, 3)
    2920
    >>> get_aggregated_timesteps(8760, 6)
    1460
    """
    return original_hours // target_hours


def get_hours_per_year(leap_year: bool = False) -> int:
    """
    Get the number of hours in a *calendar* year.

    This is calendar arithmetic only. The model treats every year as
    ``HOURS_STD_YEAR`` hours; use :func:`hours_for_year` for model years.

    Parameters
    ----------
    leap_year : bool
        Whether this is a leap year

    Returns
    -------
    int
        Number of hours (8760 or 8784 for leap year)
    """
    return 8784 if leap_year else HOURS_STD_YEAR


def hours_for_year(year: int) -> int:
    """Return the hours of a modelled year: always ``HOURS_STD_YEAR`` (8760).

    Leap years are not longer in the model. Input data come in 8760-h
    blocks and Feb 29 is dropped from them and from the result labels.
    """
    return HOURS_STD_YEAR


def parse_date_start(date_start: str) -> datetime:
    """Parse the config ``date_start`` string (``DD/MM/YYYY HH:MM``)."""
    return datetime.strptime(date_start, "%d/%m/%Y %H:%M")


def _is_feb29(index: pd.DatetimeIndex) -> np.ndarray:
    return np.asarray((index.month == 2) & (index.day == 29))


def model_time_index(
    start: Union[str, datetime, pd.Timestamp],
    periods: int,
    step_hours: int = 1,
) -> pd.DatetimeIndex:
    """Date labels for ``periods`` model timesteps starting at ``start``.

    Model years have 8760 h, so Feb 29 is skipped: the step after
    28 Feb 23:00 is 1 Mar 00:00, and every year ends on 31 Dec.
    """
    start = pd.Timestamp(start)
    step_hours = max(1, int(step_hours))
    # At most one Feb 29 per started year: generate enough spare steps.
    years_spanned = periods * step_hours // HOURS_STD_YEAR + 2
    spare = years_spanned * -(-24 // step_hours)
    idx = pd.date_range(start, periods=periods + spare, freq=f"{step_hours}h")
    return idx[~_is_feb29(idx)][:periods]


def drop_leap_days(
    data: np.ndarray,
    start: Optional[Union[str, datetime, pd.Timestamp]] = None,
) -> np.ndarray:
    """Remove Feb 29 rows from an hourly series so each year is 8760 rows.

    ``start`` is the timestamp of the first row (the config ``date_start``).
    Rows that fall on Feb 29 by that calendar are dropped when this leaves a
    whole number of 8760-h years. A series of exactly 8784 rows is one leap
    calendar year from 1 Jan whatever ``start`` is, and loses rows
    1416-1439. Anything else (already in 8760-h blocks, or a length that
    does not match a calendar) is returned unchanged.
    """
    n = data.shape[0]
    if n == 0 or n % HOURS_STD_YEAR == 0:
        return data
    if start is not None:
        stamps = pd.date_range(pd.Timestamp(start), periods=n, freq="h")
        feb29 = _is_feb29(stamps)
        if feb29.any() and (n - int(feb29.sum())) % HOURS_STD_YEAR == 0:
            return data[~feb29]
    if n == HOURS_STD_YEAR + 24:
        return np.concatenate([data[:_FEB29_START], data[_FEB29_END:]])
    return data


def year_series_8760(data: np.ndarray, year: int) -> np.ndarray:
    """One calendar year of an hourly series (from 1 Jan 00:00) as 8760 rows.

    A leap year's 8784 hours lose Feb 29 (:func:`drop_leap_days`), not
    31 December. Any other length is cut to 8760 rows or zero-padded to them.
    """
    arr = drop_leap_days(np.asarray(data, dtype=float), f"{year}-01-01")
    if len(arr) >= HOURS_STD_YEAR:
        return arr[:HOURS_STD_YEAR]
    out = np.zeros(HOURS_STD_YEAR)
    out[:len(arr)] = arr
    return out


def calculate_rolling_horizon_windows(
    total_hours: int,
    window_hours: int,
    overlap_hours: int,
) -> list:
    """
    Calculate rolling horizon windows.

    Parameters
    ----------
    total_hours : int
        Total number of hours to cover
    window_hours : int
        Hours per window
    overlap_hours : int
        Hours of overlap between windows

    Returns
    -------
    list
        List of (start_hour, end_hour) tuples for each window
    """
    if overlap_hours >= window_hours:
        raise ValueError("overlap_hours must be less than window_hours")

    effective_hours = window_hours - overlap_hours
    windows = []

    start_hour = 0
    while start_hour < total_hours:
        end_hour = min(start_hour + window_hours, total_hours)
        windows.append((start_hour, end_hour))
        start_hour += effective_hours

        # Break if we've covered all hours
        if end_hour >= total_hours:
            break

    return windows
