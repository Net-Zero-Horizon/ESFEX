"""Leap years in the Solar PV / Wind workflow profile workers.

A leap year's 8784 weather hours become an 8760-h profile by dropping
Feb 29 (the ESFEX calendar), not 31 December.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from esfex.utils.temporal import year_series_8760  # noqa: E402

_FEB29 = 59 * 24


def test_year_series_8760_drops_feb29_of_a_leap_year():
    out = year_series_8760(np.arange(8784.0), 2024)
    assert len(out) == 8760
    assert out[_FEB29 - 1] == _FEB29 - 1          # 28 Feb 23:00
    assert out[_FEB29] == _FEB29 + 24             # 1 Mar 00:00
    assert out[-1] == 8783                        # 31 Dec 23:00 kept


def test_year_series_8760_pads_and_cuts_other_lengths():
    assert np.array_equal(year_series_8760(np.arange(8760.0), 2023),
                          np.arange(8760.0))
    short = year_series_8760(np.ones(100), 2023)
    assert len(short) == 8760 and short[:100].sum() == 100 and short[100:].sum() == 0
    assert len(year_series_8760(np.ones(9000), 2023)) == 8760


def _run_worker(worker_cls, kind, monkeypatch, tmp_path):
    """Run a profile worker on one 8784-h leap-year cell whose CF equals
    the hour index / 1e4, and return the saved column."""
    pytest.importorskip("solarex" if kind == "solar" else "windrex")
    marker = np.arange(8784.0) / 1e4
    if kind == "solar":
        import solarex.core.capacity_factor as cfmod
        monkeypatch.setattr(cfmod, "_irradiance_to_hourly_cf",
                            lambda ghi, *a, **k: np.asarray(ghi))
        cell = SimpleNamespace(ghi=marker, temperature=np.zeros(8784))
    else:
        import windrex.core.capacity_factor as cfmod
        monkeypatch.setattr(cfmod, "_wind_speed_to_hourly_cf",
                            lambda ws, *a, **k: np.asarray(ws))
        cell = SimpleNamespace(wind_speed=marker)
    groups = {"u": [{"lat": 23.0, "lon": -82.0, "node": 0,
                     "instance_id": "u_0"}]}
    worker = worker_cls(groups, {(23.0, -82.0): cell}, None, str(tmp_path),
                        2024)
    errors = []
    worker.error.connect(errors.append)
    worker.run()
    assert not errors
    return np.loadtxt(tmp_path / "u_availability.csv", delimiter=",")


@pytest.mark.parametrize("kind", ["solar", "wind"])
def test_profile_worker_drops_feb29_not_dec31(kind, monkeypatch, tmp_path):
    if kind == "solar":
        from esfex.visualization.workflows.solar_pv_advanced_steps import (
            _SolarProfileWorker as worker_cls,
        )
    else:
        from esfex.visualization.workflows.wind_advanced_steps import (
            _WindProfileWorker as worker_cls,
        )
    col = _run_worker(worker_cls, kind, monkeypatch, tmp_path)
    assert col.shape == (8760,)
    assert col[_FEB29] == pytest.approx((_FEB29 + 24) / 1e4)   # 1 Mar
    assert col[-1] == pytest.approx(8783 / 1e4)                # 31 Dec 23:00
