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


def _master_built_file(path):
    """A run whose master built a solar technology and a battery technology:
    their dispatch datasets ("Investment <technology>") have no generator or
    battery config, only the technology's, and carry the MW in service."""
    with h5py.File(path, "w") as f:
        f.attrs["temporal_resolution_hours"] = 1
        sc = f.create_group("system_configuration")
        g = sc.create_group("generators/generator_0")
        sc["generators"].attrs["num_generators"] = 3
        g.attrs.update(name="coal", fuel_cost=1.0, fixed_cost=0.0,
                       maintenance_cost=0.0, rated_power=50.0)
        techs = sc.create_group("technologies")
        techs.attrs["num_technologies"] = 2
        t = techs.create_group("technology_0")
        t.attrs.update(name="Wind", invest_cost="[7.0, 7.0]")
        t = techs.create_group("technology_1")
        t.attrs.update(name="Gas CC", fuel="Gas", invest_cost="[100.0, 100.0]",
                       fuel_cost="[3.0, 3.0]", fixed_cost="[876.0, 876.0]",
                       maintenance_cost="[0.5, 0.5]", start_up_cost="[10.0, 10.0]")
        bts = sc.create_group("battery_technologies")
        bts.attrs["num_battery_technologies"] = 1
        bt = bts.create_group("battery_technology_0")
        bt.attrs.update(name="Li", invest_cost_power="[20.0, 20.0]",
                        invest_cost_energy="[4.0, 4.0]",
                        maintenance_cost="[2.0, 2.0]", fixed_cost="[4380.0, 4380.0]")
        sc.create_group("batteries").attrs["num_batteries"] = 1
        y = f.create_group("detailed_results/year_2025_threshold_0")
        y.create_dataset("generation/coal", data=np.full((1, 24), 1.0))
        ds = y.create_dataset("generation/Investment Gas CC", data=np.full((1, 24), 4.0))
        ds.attrs["rated_power"] = np.array([10.0])
        # Two starts of the built unit
        st = np.zeros((1, 24))
        st[0, 2:5] = st[0, 10:12] = 1
        y.create_dataset("gen_status/Investment Gas CC", data=st)
        ds = y.create_dataset("battery_discharge/Investment Li", data=np.full((1, 24), 1.0))
        ds.attrs["rated_power"] = np.array([5.0])
        y.create_dataset("battery_charge/Investment Li", data=np.full((1, 24), 0.5))
        # This year's builds: 2 MW of Gas CC, 3 MW / 6 MWh of Li at node 0
        y.attrs["investment_tech_investment_power_1_0"] = 2.0
        y.attrs["investment_bat_tech_investment_power_0_0"] = 3.0
        y.attrs["investment_bat_tech_investment_capacity_0_0"] = 6.0


def test_master_built_units_take_their_technology_costs(tmp_path):
    """The units the master built have datasets but no config: every cost
    of theirs was 0 (fuel, maintenance, fixed O&M, start-ups), and battery
    builds were priced at the power cost of a generator technology (0)
    with no energy cost."""
    path = tmp_path / "res.h5"
    _master_built_file(path)
    with h5py.File(path, "r") as f:
        comp = DashboardLoader({}, {})._per_system_cost(f, "", 2025)
    # coal 24 MWh × 1 + Gas CC 96 MWh × 3
    assert comp["fuel_cost"] == pytest.approx(24 + 96 * 3.0)
    assert comp["maintenance_cost"] == pytest.approx(96 * 0.5)
    # Gas CC 876 $/MW·yr × 10 MW, Li 4380 × 5 MW, each × 24/8760
    assert comp["fixed_om_cost"] == pytest.approx((876 * 10 + 4380 * 5) * 24 / 8760)
    assert comp["startup_cost"] == pytest.approx(2 * 10.0)
    # (24 + 12) MWh of throughput × 2 $/MWh
    assert comp["battery_maintenance_cost"] == pytest.approx(36 * 2.0)
    assert comp["investment_cost"] == pytest.approx(2 * 100.0 + 3 * 20.0 + 6 * 4.0)


def _two_system_file(path):
    with h5py.File(path, "w") as f:
        f.attrs["temporal_resolution_hours"] = 1
        f.attrs["subsystem_names"] = ["A", "B"]
        f.attrs["subsystem_offsets"] = [0, 1]
        f.attrs["subsystem_node_counts"] = [1, 1]
        g = f.create_group("system_configuration/generators")
        g.attrs["num_generators"] = 2
        g.create_group("generator_0").attrs.update(
            name="A/coal", fuel_cost="[1.0, 0.0]", rated_power="[10.0, 0.0]",
            fixed_cost="[0.0, 0.0]", start_up_cost="[0.0, 0.0]")
        g.create_group("generator_1").attrs.update(
            name="B/gas", fuel_cost="[0.0, 2.0]", rated_power="[0.0, 10.0]",
            fixed_cost="[0.0, 876.0]", start_up_cost="[0.0, 5.0]")
        y = f.create_group("detailed_results/year_2025_threshold_0")
        y.create_dataset("generation/A - coal", data=np.array([[1.0] * 24, [0.0] * 24]))
        y.create_dataset("generation/B - gas", data=np.array([[0.0] * 24, [1.0] * 24]))
        st = np.zeros((2, 24))
        st[1, 3:6] = 1                       # one start of B's gas at node 1
        y.create_dataset("gen_status/B - gas", data=st)
        y.create_dataset("gen_status/A - coal", data=np.ones((2, 24)))


def test_second_system_costs_are_not_zero(tmp_path):
    """Past the first system the configs came back empty (the subsystem view
    keeps the global ``generator_<i>`` keys but reports the filtered count)
    and the already system-sliced arrays were sliced again (empty): every
    cost of the second system was 0, and start-ups of a subsystem were never
    read."""
    path = tmp_path / "res.h5"
    _two_system_file(path)
    loader = DashboardLoader({}, {})
    with h5py.File(path, "r") as f:
        a = loader._per_system_cost(f, "systems/A", 2025)
        b = loader._per_system_cost(f, "systems/B", 2025)
        whole = loader._per_system_cost(f, "", 2025)
    assert a["fuel_cost"] == pytest.approx(24.0)
    assert b["fuel_cost"] == pytest.approx(48.0)
    assert b["fixed_om_cost"] == pytest.approx(876 * 10 * 24 / 8760)
    assert b["startup_cost"] == pytest.approx(5.0)
    assert a["startup_cost"] == 0.0
    assert whole["fuel_cost"] == pytest.approx(72.0)
    assert whole["startup_cost"] == pytest.approx(5.0)
