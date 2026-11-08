"""Results bookkeeping and HDF5 layout (bug sweep group B).

Rolling-horizon series keep only committed steps (#82), bus-pair flows are
mapped through bus_to_node (#83), per-unit dataset names are '/'-free and
unique for generators and batteries alike (#84, #25), and the scenario
``demand`` is always stored [node, step] (#25).
"""

from types import SimpleNamespace
from unittest.mock import patch

import h5py
import numpy as np
import pytest

from esfex.io.exporter import demand_node_hours
from esfex.runner import SimulationState, YearResults

from tests.test_runner import _create_orchestrator


def _rolling_orch(tmp_path, hours=96):
    orch = _create_orchestrator(tmp_path=tmp_path)
    orch.config.temporal.use_rolling_horizon = True
    orch.state = SimulationState(year=2025, base_year=2025, units_config={})
    orch._sectoral_demand = {}
    orch._total_hours = hours
    orch._rooftop_generation = None
    orch._ev_charging_profiles = None
    orch._progress = None
    orch._bus_to_node = None
    return orch


class TestRollingHorizonKeyedSeries:
    """power_flow, transfer_margin and the N-1 duals used to be joined with
    the overlap tail of every window, so the year had more steps than the
    other arrays (power_flow (3, 3, 10008) for 8760 h)."""

    def test_series_cover_committed_steps_only(self, tmp_path):
        orch = _rolling_orch(tmp_path)
        n_windows = []

        def fake_solve(**kw):
            w = len(n_windows)
            n_windows.append(w)
            t = kw["start_hour"] + np.arange(kw["hours"], dtype=float)
            res = {
                "objective": 0.0,
                "power_flow": {(0, 1): t, (1, 0): -t},
                "transfer_margin": {(0, 1): t * 0.0 + 1.0},
                # N-1 duals only bind in the second window
                "n1_trans_reserve_duals": {(0, 1, 1): t} if w == 1 else None,
                "n1_gen_reserve_duals": t if w == 1 else None,
            }
            return res

        with patch.object(orch, "_solve_window", side_effect=fake_solve), \
                patch.object(orch, "_compute_derived_metrics"):
            yr = orch._run_operational_dispatch(
                year=2025, year_idx=0, num_years=1,
                demand=np.ones((96, 2)), hours=96, num_nodes=2,
            )
        assert len(n_windows) > 1
        np.testing.assert_array_equal(yr.power_flow[(0, 1)], np.arange(96.0))
        np.testing.assert_array_equal(yr.power_flow[(1, 0)], -np.arange(96.0))
        assert yr.transfer_margin[(0, 1)].shape == (96,)
        # Windows without binding duals are zero-filled, so the series stay
        # aligned with the year's time axis.
        duals = yr.n1_trans_reserve_duals[(0, 1, 1)]
        gen_duals = yr.n1_gen_reserve_duals
        assert duals.shape == (96,) and gen_duals.shape == (96,)
        nz = np.nonzero(duals)[0]
        np.testing.assert_array_equal(duals[nz], nz.astype(float))
        np.testing.assert_array_equal(gen_duals, duals)


def _init_h5(orch, tmp_path, num_nodes):
    orch._num_nodes = num_nodes
    path = tmp_path / "out.h5"
    orch._initialize_hdf5(path, num_nodes=num_nodes, num_years=1,
                          start_year=2025, end_year=2026)
    return path


class TestHdf5Writer:
    def test_unit_names_demand_and_bus_pairs(self, tmp_path):
        orch = _create_orchestrator(tmp_path=tmp_path)
        orch._gen_names = ["Cuba/Gas", "Cuba/Gas", "Solar"]
        orch._gen_fuels = ["Gas", "Gas", "Sun"]
        orch._bat_names = ["BESS", "BESS"]
        # 4 buses on 2 nodes: bus pairs must be summed onto node pairs
        orch._bus_to_node = [0, 0, 1, 1]
        path = _init_h5(orch, tmp_path, num_nodes=2)
        H = 3
        demand = np.array([[10.0, 1.0], [20.0, 2.0], [30.0, 3.0]])   # [step, node]
        result = YearResults(
            year=2025, objective=1.0, solve_time=0.0, feasible=True,
            gen_output=np.ones((3, 2, H)),
            bat_charge=np.ones((2, 2, H)), bat_discharge=np.ones((2, 2, H)),
            bat_soc=np.ones((2, 2, H)),
            co2_emissions=np.ones((2, H)),
            demand=demand,
            power_flow={(1, 2): np.full(H, 5.0), (0, 1): np.full(H, 9.0)},
            transfer_margin={(3, 0): np.full(H, 2.0)},
            technology_selling_prices={
                "Cuba - Gas": {"prices_weights": np.ones((1, 3)), "total_generation": 1.0,
                               "total_revenue": 1.0, "average_selling_price": 1.0,
                               "technology_type": "Non-renewable"},
            },
        )
        orch._append_year_to_hdf5(path, result)
        with h5py.File(path, "r") as f:
            sc = f["detailed_results/year_2025_threshold_0"]
            assert sorted(sc["generation"]) == ["Cuba - Gas", "Cuba - Gas (1)", "Solar"]
            # Batteries used to crash on a homonym (no dedup)
            assert sorted(sc["battery_soc"]) == ["BESS", "BESS (1)"]
            assert sorted(sc["battery_charge"]) == ["BESS", "BESS (1)"]
            # '/' used to create nested groups
            assert list(sc["technology_selling_prices"]) == ["Cuba - Gas"]
            # demand: always [node, step], here with steps > nodes as well
            d = sc["demand"]
            assert d.shape == (2, H) and d.attrs["dimensions"] == "nodes x hours"
            np.testing.assert_array_equal(d[()], demand.T)
            # Bus pair (1, 2) is node 0 -> 1; (0, 1) is inside node 0.
            pf = sc["power_flow"][()]
            np.testing.assert_array_equal(pf[0, 1], np.full(H, 5.0))
            assert pf[0, 0].sum() == 0.0
            tm = sc["transfer_margin"][()]
            np.testing.assert_array_equal(tm[1, 0], np.full(H, 2.0))
            assert sc["CO2_emissions"].attrs["units"] == "tCO2/h"

    def test_homonym_electrolyzers_of_two_systems(self, tmp_path):
        """The merge keeps electrolyzer names (keys get the system prefix):
        two systems' "Elz" crashed the export with "name already exists"."""
        orch = _create_orchestrator(tmp_path=tmp_path)
        orch._gen_names, orch._gen_fuels, orch._bat_names = [], [], []
        orch._bus_to_node = None
        from tests.test_electrolyzer_dispatch_results import _elz
        orch.primary_system = orch.primary_system.model_copy(update={"electrolyzers": {
            "A__elz": _elz("Elz", 5.0), "B__elz": _elz("Elz", 3.0), "B__h2": _elz("A/B", 1.0)}})
        path = _init_h5(orch, tmp_path, num_nodes=2)
        power = np.arange(12.0).reshape(3, 2, 2)
        orch._append_year_to_hdf5(path, YearResults(
            year=2025, objective=0.0, solve_time=0.0, feasible=True,
            electrolyzer_power=power, electrolyzer_h2=power * 10))
        with h5py.File(path, "r") as f:
            sc = f["detailed_results/year_2025_threshold_0"]
            assert list(sc["electrolyzer_power"]) == ["A - B", "Elz", "Elz (1)"]
            np.testing.assert_array_equal(sc["electrolyzer_power/Elz (1)"][()], power[1])
            np.testing.assert_array_equal(sc["electrolyzer_h2/A - B"][()], power[2] * 10)

    def test_unit_name_lists_use_the_writer_convention(self, tmp_path):
        from esfex.runner import unique_unit_names
        assert unique_unit_names(["A", "A", "A", "B"]) == ["A", "A (1)", "A (2)", "B"]
        # A rebuilt name list is already unique: the writer keeps it as is.
        orch = _create_orchestrator(tmp_path=tmp_path)
        names = unique_unit_names(["Cuba/Gas", "Cuba/Gas"])
        assert orch._h5_unit_names(names) == ["Cuba - Gas", "Cuba - Gas (1)"]

    def test_demand_node_major_when_nodes_exceed_steps(self, tmp_path):
        orch = _create_orchestrator(tmp_path=tmp_path)
        orch._gen_names, orch._gen_fuels, orch._bat_names = [], [], []
        orch._bus_to_node = None
        path = _init_h5(orch, tmp_path, num_nodes=3)
        demand = np.arange(6.0).reshape(2, 3)   # 2 steps x 3 nodes
        orch._append_year_to_hdf5(path, YearResults(
            year=2025, objective=0.0, solve_time=0.0, feasible=True, demand=demand))
        with h5py.File(path, "r") as f:
            d = f["detailed_results/year_2025_threshold_0/demand"]
            assert d.shape == (3, 2)    # used to stay (2, 3) because steps < nodes
            np.testing.assert_array_equal(demand_node_hours(d), demand.T)

    def test_inter_system_flows_map_buses_to_nodes(self, tmp_path):
        orch = _create_orchestrator(tmp_path=tmp_path)
        orch._bus_to_node = [0, 0, 1, 1]
        mapping = {"A": {"node_offset": 0, "node_count": 1},
                   "B": {"node_offset": 1, "node_count": 1}}
        path = tmp_path / "sys.h5"
        with h5py.File(path, "w") as f:
            f.create_group("inter_system").create_group("detailed_results")
            g = f.create_group("global")
            for name in ("summary_results", "cost_breakdown", "demand"):
                g.create_group(name)
        result = YearResults(year=2025, objective=0.0, solve_time=0.0, feasible=True,
                             power_flow={(1, 2): np.array([5.0, 6.0]),
                                         (0, 1): np.array([9.0, 9.0])})
        with patch.object(orch, "_build_system_unit_mapping", return_value=mapping):
            orch._append_year_per_system(path, result)
        with h5py.File(path, "r") as f:
            flows = f["inter_system/detailed_results/year_2025_threshold_0/power_flow"]
            # Bus 1 (node 0, A) -> bus 2 (node 1, B). Bus indices used to be
            # read as node indices: bus 1 looked like system B, bus 2 like
            # no system, and the flow was lost.
            assert list(flows) == ["A_to_B"]
            np.testing.assert_array_equal(flows["A_to_B"][()], [5.0, 6.0])


class TestSellingPriceNames:
    def test_homonyms_kept_apart_and_slash_free(self, tmp_path):
        orch = _create_orchestrator(tmp_path=tmp_path)
        orch._num_nodes = 1
        orch._gen_names = ["Cuba/Gas", "Cuba/Gas"]
        gens = {f"g{i}": SimpleNamespace(
            name="Cuba/Gas", type="Non-renewable", rated_power=[10.0], fuel_cost=[1.0],
            fixed_cost=[0.0], maintenance_cost=[0.0], start_up_cost=[0.0], ramp_up=[1.0],
        ) for i in range(2)}
        orch.primary_system = SimpleNamespace(generators=gens, batteries={})
        orch._current_units_config = {}
        res = YearResults(year=2025, objective=0.0, solve_time=0.0, feasible=True,
                          gen_output=np.array([[[1.0, 1.0]], [[3.0, 3.0]]]),
                          prices=np.array([[10.0, 20.0]]))
        orch._compute_derived_metrics(res)
        tsp = res.technology_selling_prices
        assert tsp is not None
        assert sorted(tsp) == ["Cuba - Gas", "Cuba - Gas (1)"]
        assert tsp["Cuba - Gas (1)"]["total_generation"] == 6.0


class TestLcoeFixedOM:
    def test_fixed_om_spread_over_the_energy(self, tmp_path):
        """fixed_cost is $/MW·yr: the LCOE spreads the result hours' share of
        it over the energy produced. It was added as if it were $/MWh."""
        orch = _create_orchestrator(tmp_path=tmp_path)
        orch._num_nodes = 1
        orch._gen_names = ["Gas"]
        gen = SimpleNamespace(
            name="Gas", type="Non-renewable", rated_power=[10.0], fuel_cost=[1.0],
            fixed_cost=[8760.0], maintenance_cost=[0.5], start_up_cost=[0.0], ramp_up=[1.0])
        orch.primary_system = SimpleNamespace(generators={"g": gen}, batteries={})
        orch._current_units_config = {}
        res = YearResults(year=2025, objective=0.0, solve_time=0.0, feasible=True,
                          gen_output=np.array([[[5.0, 5.0]]]),
                          prices=np.array([[10.0, 20.0]]))
        orch._compute_derived_metrics(res)
        # 8760 $/MW·yr × 10 MW × 2 h / 8760 h = 20 $ over 10 MWh = 2 $/MWh
        np.testing.assert_allclose(res.lcoe[0, 0], [1.0 + 2.0 + 0.5] * 2)


class TestDemandReaders:
    """Old files kept demand [step, node] when steps <= nodes; new files are
    always [node, step] with a ``dimensions`` attribute."""

    def _file(self, tmp_path, data, new):
        path = tmp_path / ("new.h5" if new else "old.h5")
        with h5py.File(path, "w") as f:
            sc = f.create_group("detailed_results/year_2025_threshold_0")
            ds = sc.create_dataset("demand", data=data)
            if new:
                ds.attrs["dimensions"] = "nodes x hours"
            sc.create_dataset("electricity_prices", data=np.zeros(2))
        return path

    def test_helper_reads_both_layouts(self, tmp_path):
        node_step = np.arange(6.0).reshape(3, 2)        # 3 nodes, 2 steps
        with h5py.File(self._file(tmp_path, node_step.T, new=False), "r") as f:
            ds = f["detailed_results/year_2025_threshold_0/demand"]
            np.testing.assert_array_equal(demand_node_hours(ds, 2), node_step)
        with h5py.File(self._file(tmp_path, node_step, new=True), "r") as f:
            ds = f["detailed_results/year_2025_threshold_0/demand"]
            np.testing.assert_array_equal(demand_node_hours(ds, 3), node_step)

    def test_scripting_load_results_orients_old_files(self, tmp_path):
        from esfex.visualization.scripting_api import ESFEXAPI
        node_step = np.arange(6.0).reshape(3, 2)
        r = ESFEXAPI.load_results(self._file(tmp_path, node_step.T, new=False))
        np.testing.assert_array_equal(r["demand"][2025], node_step)

    def test_viewer_system_slice_of_old_layout(self, tmp_path):
        pytest.importorskip("PySide6")
        from esfex.visualization.panels import results_charts as rc
        node_step = np.arange(6.0).reshape(3, 2)
        with h5py.File(self._file(tmp_path, node_step.T, new=False), "r") as f:
            sc = f["detailed_results/year_2025_threshold_0"]
            view = rc._SlicedScenarioView(sc, 1, 3, "B")      # nodes 1-2
            np.testing.assert_array_equal(rc._demand_node_hours(view), node_step[1:3])
