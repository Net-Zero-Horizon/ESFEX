"""Dashboard per-system cost reconstruction (DashboardLoader._per_system_cost)."""

import h5py
import numpy as np
import pytest

from esfex.visualization.panels.dashboard_loader import DashboardLoader


def test_fixed_om_per_mw_year_includes_idle_units_and_batteries(tmp_path):
    """fixed_cost is $/MW·yr of rated power (MaxDischargePower for a
    battery) for the simulated share of the year, idle units included. It
    was charged per MWh generated, and batteries had none."""
    path = tmp_path / "res.h5"
    with h5py.File(path, "w") as f:
        f.attrs["temporal_resolution_hours"] = 1
        sc = f.create_group("system_configuration")
        gens = sc.create_group("generators")
        gens.attrs["num_generators"] = 2
        for i, (fixed, rated) in enumerate(((0.0, 50.0), (8760.0, 40.0))):
            g = gens.create_group(f"generator_{i}")
            g.attrs["fuel_cost"] = 1.0
            g.attrs["fixed_cost"] = fixed
            g.attrs["maintenance_cost"] = 0.0
            g.attrs["rated_power"] = rated
        bats = sc.create_group("batteries")
        bats.attrs["num_batteries"] = 1
        b = bats.create_group("battery_0")
        b.attrs["fixed_cost"] = 4380.0
        b.attrs["MaxDischargePower"] = 20.0
        b.attrs["maintenance_cost"] = 0.0
        y = f.create_group("detailed_results/year_2025_threshold_0")
        y.create_dataset("generation/coal", data=np.full((1, 24), 10.0))
        y.create_dataset("generation/gas", data=np.zeros((1, 24)))   # idle
        y.create_dataset("battery_discharge/bess", data=np.zeros((1, 24)))
    with h5py.File(path, "r") as f:
        comp = DashboardLoader({}, {})._per_system_cost(f, "", 2025)
    # gas: 8760 × 40 MW × 24/8760; battery: 4380 × 20 MW × 24/8760
    assert comp["fixed_om_cost"] == pytest.approx(40.0 * 24 + 10.0 * 24)
    assert comp["fuel_cost"] == pytest.approx(240.0)
