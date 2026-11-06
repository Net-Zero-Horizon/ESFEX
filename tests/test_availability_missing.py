"""Units whose availability profile is missing are flagged, not silently 1.0.

A renewable technology without ``availability_file`` ran at capacity factor
1.0 every hour without a word; a technology whose file was missing crashed
with a KeyError, and a generator whose file was missing was dropped from the
dispatch.
"""

import logging
import sys
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_generator  # noqa: E402
from test_zones import _make_system_config, _make_technology  # noqa: E402

from esfex.runner import Orchestrator  # noqa: E402


def _preload(tmp_path, generators=None, technologies=None):
    sysc = _make_system_config(num_nodes=2, generators=generators or {},
                               technologies=technologies or {})
    fake = SimpleNamespace(primary_system=sysc,
                           config_path=str(tmp_path / "case.yaml"))
    return Orchestrator._preload_availability_profiles(fake, 2)


def test_renewable_technology_without_profile_is_flagged(tmp_path, caplog):
    solar = _make_technology("solar_new", "Sun", 2)
    with caplog.at_level(logging.WARNING, logger="esfex.runner"):
        cache = _preload(tmp_path, technologies={"solar_new": solar})
    assert "solar_new" not in cache
    assert any("solar_new" in r.message and "capacity factor 1.0" in r.message
               for r in caplog.records)


def test_renewable_generator_without_profile_is_flagged(tmp_path, caplog):
    wind = _make_generator("Wind", "Renewable", "Wind", [10.0, 0.0])
    gas = _make_generator("Gas", "Non-renewable", "Gas", [10.0, 0.0])
    with caplog.at_level(logging.WARNING, logger="esfex.runner"):
        _preload(tmp_path, generators={"wind": wind, "gas": gas})
    flagged = [r.message for r in caplog.records if "capacity factor 1.0" in r.message]
    assert len(flagged) == 1 and "'wind'" in flagged[0]


def test_dispatchable_units_and_reservoirs_are_not_flagged(tmp_path, caplog):
    hydro = _make_generator("Hydro", "Renewable", "Water", [10.0, 0.0],
                            reservoir_capacity=[500.0, 0.0])
    gas = _make_technology("gas_new", "Gas", 2, tech_type="Non-renewable")
    with caplog.at_level(logging.WARNING, logger="esfex.runner"):
        _preload(tmp_path, generators={"hydro": hydro},
                 technologies={"gas_new": gas})
    assert not [r for r in caplog.records if "capacity factor 1.0" in r.message]


def test_missing_technology_file_is_an_error(tmp_path):
    solar = _make_technology("solar_new", "Sun", 2).model_copy(
        update={"availability_file": "missing.csv"})
    with pytest.raises(FileNotFoundError, match="solar_new"):
        _preload(tmp_path, technologies={"solar_new": solar})


def test_missing_generator_file_is_an_error(tmp_path):
    solar = _make_generator("Solar", "Renewable", "Sun", [10.0, 0.0],
                            availability_file="missing.csv")
    with pytest.raises(FileNotFoundError, match="solar"):
        _preload(tmp_path, generators={"solar": solar})


def test_profiles_are_loaded_and_shared(tmp_path):
    np.savetxt(tmp_path / "pv.csv", np.full((8760, 2), 0.25), delimiter=",")
    solar = _make_generator("Solar", "Renewable", "Sun", [10.0, 0.0],
                            availability_file="pv.csv")
    tech = _make_technology("solar_new", "Sun", 2).model_copy(
        update={"availability_file": "pv.csv"})
    cache = _preload(tmp_path, generators={"solar": solar},
                     technologies={"solar_new": tech})
    assert cache["solar"].shape[0] == 8760
    assert cache["solar_new"] is cache["solar"]
    assert cache["pv.csv"] is cache["solar"]
