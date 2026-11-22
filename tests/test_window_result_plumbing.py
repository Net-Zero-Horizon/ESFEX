"""Rolling-horizon window results reach YearResults and the HDF5 file.

``_solve_window`` built its result dict by hand and left out the reservoir
series, battery spillage, the inertia deficit, the transfer margin and every
N-1 result, so those YearResults fields stayed empty and their HDF5 groups
were never written. The other tests mock ``_solve_window``; this one runs the
real windows (HiGHS) on a small case with a reservoir and N-1 enabled.
"""

import sys

import h5py
import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator, _make_node_config  # noqa: E402
from test_zones import _make_battery  # noqa: E402

from esfex.config.schema import N1SecurityConfig, SystemConfig, TemporalConfig  # noqa: E402

STEPS = 365   # 24-h steps in a year


def _case(tmp_path):
    demand = tmp_path / "demand.csv"
    np.savetxt(demand, np.tile([70.0, 20.0], (8760, 1)), delimiter=",")
    inflow = tmp_path / "inflow.csv"
    np.savetxt(inflow, np.full((8760, 1), 3.0))   # MW-eq, above 1
    gas = _make_generator("Gas", "Non-renewable", "Gas", [100.0, 0.0],
                          degradation_rate=[0.0, 0.0], start_up_cost=[0.0, 0.0],
                          initial_age=[0, 0], fuel_cost=[50.0, 50.0])
    diesel = _make_generator("Diesel", "Non-renewable", "Diesel", [30.0, 0.0],
                             degradation_rate=[0.0, 0.0], start_up_cost=[0.0, 0.0],
                             initial_age=[0, 0], fuel_cost=[200.0, 200.0])
    hydro = _make_generator(
        "Hydro", "Renewable", "Water", [0.0, 20.0],
        degradation_rate=[0.0, 0.0], start_up_cost=[0.0, 0.0], initial_age=[0, 0],
        fuel_cost=[0.0, 0.0],
        reservoir_capacity=[0.0, 2000.0], reservoir_initial_level=[0.0, 0.5],
        reservoir_min_level=[0.0, 0.0], reservoir_max_level=[0.0, 1.0],
        reservoir_turbine_efficiency=[1.0, 1.0], reservoir_evaporation_rate=[0.0, 0.0],
        reservoir_pump_capacity=[0.0, 0.0], reservoir_pump_efficiency=[0.0, 0.0],
        reservoir_inflow_file=str(inflow),
    )
    bat = _make_battery("BESS", 2).model_copy(update=dict(
        capacity=[20.0, 0.0], MaxChargePower=[5.0, 0.0], MaxDischargePower=[5.0, 0.0],
        rated_power=[5.0, 0.0], degradation_rate=[0.0, 0.0],
        invest_max_power=[0.0, 0.0], invest_max_capacity=[0.0, 0.0]))
    # Two nodes linked by a 200 MW connection (no line list: transfer margin)
    sysc = SystemConfig(name="TestSystem", nodes=_make_node_config(2),
                        generators={"gas": gas, "diesel": diesel, "hydro": hydro},
                        batteries={"bess": bat}, demand_path=str(demand))
    return _make_esfex_config(
        sysc, simulation_mode="economic_dispatch",
        n1_security=N1SecurityConfig(enabled=True, apply_to_modes=["economic_dispatch"]),
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=1440,
                                overlap_hours=240))


@pytest.mark.julia
def test_window_results_reach_year_results_and_hdf5(tmp_path):
    from esfex.runner import Orchestrator

    orch = Orchestrator(_case(tmp_path), output_dir=tmp_path / "out")
    res = orch.run(years=1)[0]

    assert res.gen_output.shape == (3, 2, STEPS)
    # Reservoir series: committed steps only, like the other arrays
    assert res.reservoir_level is not None and res.reservoir_level.shape == (3, 2, STEPS)
    assert res.reservoir_spillage is not None and res.reservoir_spillage.shape == (3, 2, STEPS)
    assert res.reservoir_pump is not None
    assert res.reservoir_level[2, 1, 0] == pytest.approx(1000.0, rel=1e-6)
    assert res.bat_spillage is not None and res.bat_spillage.shape[-1] == STEPS
    assert res.loss_of_inertia is not None and res.loss_of_inertia.shape == (STEPS,)
    assert res.transfer_margin and all(v.shape == (STEPS,) for v in res.transfer_margin.values())
    # N-1: the 30 MW diesel and the 5 MW battery cannot cover the loss of
    # the gas unit, so the generation reserve binds in every step.
    assert res.n1_gen_reserve_duals is not None
    assert res.n1_gen_reserve_duals.shape == (STEPS,)
    assert np.all(np.abs(res.n1_gen_reserve_duals) > 1e-6)
    assert res.n1_binding_contingencies

    h5 = next((tmp_path / "out").glob("*.h5"))
    with h5py.File(h5, "r") as f:
        sc = f["detailed_results/year_2025_threshold_0"]
        for group in ("reservoir_level", "reservoir_spillage", "reservoir_pump",
                      "battery_spillage"):
            assert group in sc, group
        assert sc["reservoir_level/TestSystem - Hydro"].shape == (2, STEPS)
        assert sc["loss_of_inertia"].shape == (STEPS,)
        assert sc["transfer_margin"].shape == (2, 2, STEPS)
        n1 = sc["n1_security"]
        assert n1["gen_reserve_duals"].shape == (STEPS,)
        assert len(n1["binding_contingencies"]) > 0


class TestPrimaryEnergyWindowMerge:
    """Primary-energy results of the rolling-horizon windows were summed
    element-wise, overlap included and with no kept fraction on the costs
    (about 2x with 48 h windows / 24 h overlap), and windows with a different
    number of fuel periods (the last, shorter one) crashed the year with a
    broadcast error."""

    def _run(self, tmp_path, hours=130):
        from unittest.mock import patch

        from tests.test_results_hdf5_fixes import _rolling_orch

        orch = _rolling_orch(tmp_path, hours=hours)
        t = orch.config.temporal
        t.rolling_horizon_hours, t.overlap_hours, t.primary_energy_resolution = 72, 24, 24
        windows = []

        def fake_solve(**kw):
            n, keep = kw["hours"], kw["keep_hours"]
            windows.append((kw["start_hour"], n))
            # One unit of fuel per step, reported per 24-step period.
            per_period = np.array([min(24, n - s) for s in range(0, n, 24)], dtype=float)
            return {
                "objective": 0.0,
                "primary_energy": {
                    "total_fuel_supply": {"gas": per_period},
                    "total_ne_demand_satisfied": {"gas": per_period * 2},
                    "total_loss_of_supply": {"gas": per_period * 0},
                    "transport_flows": {"gas": np.vstack([per_period, per_period * 3])},
                    "final_storage_levels": {"gas": np.array([float(len(windows))])},
                    # The window's costs cover its committed steps only
                    "total_fuel_cost": float(keep), "total_transport_cost": 2.0 * keep,
                    "total_loss_penalty": 0.0,
                    # Fuel burnt per step: the window's start step + step
                    "fuel_for_power": {"Gas plant": np.vstack(
                        [kw["start_hour"] + np.arange(n, dtype=float)] * 2)},
                },
            }

        with patch.object(orch, "_solve_window", side_effect=fake_solve), \
                patch.object(orch, "_compute_derived_metrics"):
            yr = orch._run_operational_dispatch(
                year=2025, year_idx=0, num_years=1,
                demand=np.ones((hours, 2)), hours=hours, num_nodes=2,
            )
        return yr.primary_energy, windows

    def test_committed_steps_only_with_a_short_last_window(self, tmp_path):
        pe, windows = self._run(tmp_path)
        # 72-step windows every 48 steps; the last one has 34 steps (2 periods)
        assert [n for _, n in windows] == [72, 72, 34]
        expected = np.array([24.0, 24, 24, 24, 24, 10])   # 130 steps, 24-step periods
        np.testing.assert_allclose(pe["total_fuel_supply"]["gas"], expected)
        np.testing.assert_allclose(pe["total_ne_demand_satisfied"]["gas"], expected * 2)
        np.testing.assert_allclose(pe["transport_flows"]["gas"],
                                   np.vstack([expected, expected * 3]))
        # Costs: summed over the windows (48 + 48 + 34 committed steps)
        assert pe["total_fuel_cost"] == pytest.approx(130.0)
        assert pe["total_transport_cost"] == pytest.approx(260.0)
        # Fuel for power: the committed steps of each window, joined
        np.testing.assert_allclose(pe["fuel_for_power"]["Gas plant"],
                                   np.vstack([np.arange(130.0)] * 2))
        # End-of-year tank levels from the last window
        assert pe["final_storage_levels"]["gas"].tolist() == [3.0]
