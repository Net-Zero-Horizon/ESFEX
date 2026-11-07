"""PowerSystemAdapter reads a profile missing from the runner's cache the
way the runner does.

The fallback used to load the whole file without the data calendar
(``date_start``) and without slicing the window: every window got the first
hours of the file, whatever its year and start hour, and a relative path was
resolved against the current directory instead of the configuration's.
"""

import numpy as np
import pytest

from esfex.io.demand import load_run_availability
from esfex.utils.temporal import HOURS_STD_YEAR as H

from tests.test_runner import _make_esfex_config, _make_generator, _make_system_config

pytestmark = pytest.mark.julia

START = "01/01/2028 00:00"          # leap year: the file has a 29 Feb


def _config(tmp_path, file_name):
    rows = 8784 + H                                             # 2028, 2029
    np.savetxt(tmp_path / "cf.csv", (np.arange(rows) % 997) / 997.0, delimiter=",")
    pv = _make_generator("PV", "Renewable", "Sun", [50.0, 80.0], availability_file=file_name)
    gas = _make_generator("Gas", "Non-renewable", "Gas", [100.0, 50.0])
    cfg = _make_esfex_config(_make_system_config(generators={"pv": pv, "gas": gas}))
    return cfg.model_copy(update={"date_start": START})


def _pv_availability(cfg, cache, config_path, start_hour, year=2029, hours=24):
    from esfex.bridge.adapters import PowerSystemAdapter

    ps = PowerSystemAdapter(
        config=cfg, demand=np.full((hours, 2), 10.0), hours=hours, num_nodes=2,
        year=year, base_year=2028, mode="economic_dispatch",
        availability_cache=cache, start_hour=start_hour, config_path=config_path,
    )
    ps._jl_input = ps._create_input()
    return np.asarray(ps._jl_input.generators[0].availability)


@pytest.mark.parametrize("relative", [True, False])
def test_fallback_matches_the_runner_cache(tmp_path, relative):
    file_name = "cf.csv" if relative else str(tmp_path / "cf.csv")
    cfg = _config(tmp_path, file_name)
    config_path = tmp_path / "model.yaml"
    # What the runner preloads (and hands the adapter as its cache)
    cached = load_run_availability(tmp_path, file_name, 2, START)
    assert cached.shape == (2 * H, 2)
    start = 1000
    via_cache = _pv_availability(cfg, {"pv": cached}, config_path, start)
    via_file = _pv_availability(cfg, {}, config_path, start)
    np.testing.assert_allclose(via_file, via_cache)
    # Second data year, hour 1000: row 8760 + 1000 of the file once Feb 29
    # (24 rows) is dropped
    expected = ((np.arange(24) + H + start + 24) % 997) / 997.0
    np.testing.assert_allclose(via_file[:, 0], expected)
