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


def test_units_are_matched_to_their_configs_by_name(tmp_path):
    """h5py lists the datasets alphabetically and the batteries came out of
    a set, but both were paired with the configs by position: with several
    units each could get another unit's costs."""
    path = tmp_path / "res.h5"
    gens = [("zinc", 2.0, 0.0, 10.0), ("coal", 5.0, 876.0, 30.0)]   # config order
    bats = [("delta", 1.0, 0.0, 10.0), ("charlie", 2.0, 8760.0, 20.0),
            ("bravo", 3.0, 0.0, 30.0), ("alpha", 4.0, 87600.0, 40.0)]
    with h5py.File(path, "w") as f:
        f.attrs["temporal_resolution_hours"] = 1
        sc = f.create_group("system_configuration")
        g_grp = sc.create_group("generators")
        g_grp.attrs["num_generators"] = len(gens)
        for i, (name, fuel, fixed, rated) in enumerate(gens):
            g = g_grp.create_group(f"generator_{i}")
            g.attrs.update(name=name, fuel_cost=fuel, fixed_cost=fixed,
                           maintenance_cost=0.0, rated_power=rated)
        b_grp = sc.create_group("batteries")
        b_grp.attrs["num_batteries"] = len(bats)
        for i, (name, maint, fixed, power) in enumerate(bats):
            b = b_grp.create_group(f"battery_{i}")
            b.attrs.update(name=name, maintenance_cost=maint, fixed_cost=fixed,
                           MaxDischargePower=power)
        y = f.create_group("detailed_results/year_2025_threshold_0")
        y.create_dataset("generation/zinc", data=np.full((1, 24), 1.0))
        y.create_dataset("generation/coal", data=np.full((1, 24), 2.0))
        for k, (name, *_rest) in enumerate(bats):
            y.create_dataset(f"battery_discharge/{name}", data=np.full((1, 24), k + 1.0))
            y.create_dataset(f"battery_charge/{name}", data=np.zeros((1, 24)))
    with h5py.File(path, "r") as f:
        comp = DashboardLoader({}, {})._per_system_cost(f, "", 2025)
    # zinc 24 MWh × 2 + coal 48 MWh × 5
    assert comp["fuel_cost"] == pytest.approx(24 * 2.0 + 48 * 5.0)
    # Discharge (k + 1) × 24 MWh at each battery's own maintenance cost
    assert comp["battery_maintenance_cost"] == pytest.approx(
        24 * (1 * 1.0 + 2 * 2.0 + 3 * 3.0 + 4 * 4.0))
    # coal 876 × 30, charlie 8760 × 20, alpha 87600 × 40, each × 24/8760
    assert comp["fixed_om_cost"] == pytest.approx(
        (876 * 30 + 8760 * 20 + 87600 * 40) * 24 / 8760)
