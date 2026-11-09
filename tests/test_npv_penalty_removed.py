"""The dispatch's forced-replacement term and its ``npv_penalty_cost`` are gone.

The term had a cost but no constraint (its variables were always 0) and no
run ever set its inputs. Result files written before still carry the
always-zero ``npv_penalty_cost`` attribute; readers ignore it.
"""

import h5py
import pytest

from esfex.visualization.panels.dashboard_loader import DashboardLoader


def test_legacy_cost_breakdown_with_npv_penalty_still_loads(tmp_path):
    path = tmp_path / "old.h5"
    with h5py.File(path, "w") as f:
        g = f.create_group("cost_breakdown/year_2025")
        g.attrs.update(fuel_cost=1000.0, fixed_om_cost=200.0,
                       npv_penalty_cost=0.0, total=1200.0)
    with h5py.File(path, "r") as f:
        wf = DashboardLoader({}, {})._read_cost_breakdown(f, "", [2025])
    assert {s["label"] for s in wf["steps"]} == {"Fuel", "Fixed O&M"}
    assert wf["total"] == pytest.approx(1200.0)
    assert "npv_penalty_cost" not in dict(DashboardLoader._COST_COMPONENTS)
