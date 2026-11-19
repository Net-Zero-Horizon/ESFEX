"""``esfex.workflows.tsam_cluster`` (round 44, M44).

It called ``compute_tsam_periods(..., n_periods=...)``, a keyword that
function does not take (TypeError), and documented result fields
(``representative_days``, ``weights``, ``mapping``) that TSAMResult does
not have.
"""

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from esfex.visualization.scripting_api import ESFEXAPI  # noqa: E402


def _year():
    """365 days: weekdays at 100 MW, weekends at 50 MW, a peak day."""
    days = np.where(np.arange(365) % 7 < 5, 100.0, 50.0)
    days[200] = 300.0
    return np.repeat(days, 24)


@pytest.mark.parametrize("shape", ["series", "nodes"])
def test_tsam_cluster_returns_the_tsam_result(shape):
    demand = _year() if shape == "series" else np.column_stack([_year(), 2 * _year()])
    res = ESFEXAPI.workflows.tsam_cluster(demand, n_periods=3)
    assert res.num_periods == 3
    assert len(res.period_start_hours) == len(res.period_weights) == 3
    assert sum(res.period_weights) == 365
    assert len(res.day_period) == 365
    # The peak day is a representative period of its own
    assert 200 * 24 in res.period_start_hours
    assert res.day_period[200] == res.period_start_hours.index(200 * 24)


def test_tsam_cluster_docstring_names_the_real_fields():
    doc = ESFEXAPI.workflows.tsam_cluster.__doc__
    for f in ("period_start_hours", "period_weights", "day_period"):
        assert f in doc
    assert "representative_days" not in doc and ".mapping" not in doc
