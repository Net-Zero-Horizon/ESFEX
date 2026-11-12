"""One RE-share definition and scheduled outages in the master (round 27).

The RE share is renewable over total generation everywhere, energy-weighted
over the year (the dispatch's annual figure was the unweighted mean of its
windows); the generators' ``outage_schedule`` windows reach the master as
they reach the dispatch.
"""

from types import SimpleNamespace
from unittest.mock import patch

import h5py
import numpy as np
import pytest

from esfex.bridge.converters import compile_outage_factor, unit_outage_factors
from esfex.config.schema import OutageWindow
from esfex.runner import Orchestrator, YearResults, re_share
from esfex.visualization.panels.dashboard_loader import DashboardLoader

from tests.test_results_hdf5_fixes import _init_h5, _rolling_orch
from tests.test_runner import (
    _create_orchestrator, _make_esfex_config, _make_generator, _make_system_config,
)


def _ow(eid, start, end, avail=0.0, node=None, recurring=False, kind="generator"):
    return OutageWindow(element_type=kind, element_id=eid, node=node, start_hour=start,
                        end_hour=end, availability=avail, recurring=recurring)


class TestYearShareEnergyWeighted:
    def test_year_share_is_renewable_over_generation(self, tmp_path):
        """A sunny window with little generation and dark windows with a lot:
        the year's share is Σ renewable / Σ generation (it was the mean of
        the windows' shares)."""
        orch = _rolling_orch(tmp_path)
        n = []

        def fake_solve(**kw):
            w = len(n)
            n.append(w)
            return {"objective": 0.0, "re_penetration": 1.0 if w == 0 else 0.0,
                    "total_generation": 10.0 if w == 0 else 90.0}

        with patch.object(orch, "_solve_window", side_effect=fake_solve), \
                patch.object(orch, "_compute_derived_metrics"):
            yr = orch._run_operational_dispatch(
                year=2025, year_idx=0, num_years=1,
                demand=np.ones((96, 2)), hours=96, num_nodes=2)
        assert len(n) > 1
        assert yr.total_generation == pytest.approx(10.0 + 90.0 * (len(n) - 1))
        assert yr.re_penetration == pytest.approx(10.0 / yr.total_generation)
        assert yr.re_penetration != pytest.approx(1.0 / len(n))

    def test_re_share(self):
        assert re_share(30.0, 120.0) == pytest.approx(0.25)
        assert re_share(5.0, 0.0) == 0.0


class TestHdf5RenewableFlag:
    def test_generation_datasets_carry_the_rule(self, tmp_path):
        orch = _create_orchestrator(tmp_path=tmp_path)
        orch._gen_names = ["Gas", "Nuclear", "Solar"]
        orch._gen_fuels = ["Gas", "Uranium", "Sun"]
        orch._gen_renewable = [False, False, True]
        orch._bat_names = []
        orch._bus_to_node = None
        path = _init_h5(orch, tmp_path, num_nodes=1)
        H = 4
        result = YearResults(
            year=2025, objective=1.0, solve_time=0.0, feasible=True,
            gen_output=np.stack([np.full((1, H), v) for v in (30.0, 50.0, 20.0)]),
            co2_emissions=np.zeros((1, H)), demand=np.full((H, 1), 100.0))
        orch._append_year_to_hdf5(path, result)
        with h5py.File(path, "r") as f:
            gen = f["detailed_results/year_2025_threshold_0/generation"]
            assert [bool(gen[k].attrs["is_renewable"]) for k in ("Gas", "Nuclear", "Solar")] \
                == [False, False, True]
            # The dashboard's share follows the flag: nuclear is not
            # renewable (its label used to count as such)
            mix = DashboardLoader({}, {})._read_mix(f, "", [2025], [2025])
            assert mix["re_gwh"] == [pytest.approx(20.0 * H / 1000.0)]
            assert DashboardLoader({}, {})._re_share_at_year(mix, 2025) == pytest.approx(0.2)

    def test_legacy_files_fall_back_to_labels(self):
        mix = {"years": [2025], "series": [{"label": "Solar", "values": [20.0]},
                                           {"label": "Nuclear", "values": [50.0]},
                                           {"label": "Gas", "values": [30.0]}]}
        assert DashboardLoader({}, {})._re_share_at_year(mix, 2025) == pytest.approx(0.2)


class TestUnitOutageFactors:
    def test_node_whole_unit_and_recurring(self):
        sched = [
            _ow("pv", 0, 24, node=2),                     # one node, first day
            _ow("pv", 8760 + 10, 8760 + 14, avail=0.5),   # whole unit, year 2
            _ow("gas", 100, 110, recurring=True),         # every year
            _ow("other", 0, 8760),                        # not a listed unit
            _ow("pv", 3 * 8760, 4 * 8760),                # past the span
        ]
        f = unit_outage_factors(sched, "generator", ["gas", "pv"], 0, 2 * 8760)
        assert set(f) == {(1, 0), (2, 3), (2, 0)}
        assert f[(2, 3)][:24].tolist() == [0.0] * 24 and f[(2, 3)][24:].min() == 1.0
        assert f[(2, 0)][8770:8774].tolist() == [0.5] * 4
        gas = f[(1, 0)]
        assert gas[100:110].max() == 0.0 and gas[8860:8870].max() == 0.0
        assert gas.sum() == 2 * 8760 - 20
        # The dispatch's factor for the same windows and span
        np.testing.assert_array_equal(
            gas, compile_outage_factor([(100, 110, 0.0, True)], 0, 2 * 8760))

    def test_myopic_year_and_resolution(self):
        """A later year's master starts at its horizon hour; at 6 h steps a
        step partly out is derated in proportion, as in the dispatch."""
        sched = [_ow("gas", 8760 + 3, 8760 + 9)]
        f = unit_outage_factors(sched, "generator", ["gas"], 8760, 8760 // 6, 6)
        assert f[(1, 0)][:2].tolist() == [0.5, 0.5]
        assert f[(1, 0)][2:].min() == 1.0
        # Battery windows are whole-unit, a node or not
        b = unit_outage_factors([_ow("li", 0, 5, node=1, kind="battery")],
                                "battery", ["li"], 0, 10, by_node=False)
        assert set(b) == {(1, 0)}


class TestMasterOutageInput:
    def test_adapter_builds_julia_factors(self):
        """The master adapter hands the outage factors of its generators and
        batteries to Julia, keyed by their position in its unit lists."""
        from esfex.bridge.adapters import MasterProblemAdapter
        from esfex.bridge.julia_setup import get_julia
        jl = get_julia()
        sys = SimpleNamespace(outage_schedule=[
            _ow("pv", 0, 12, node=0), _ow("li", 24, 48, avail=0.25, kind="battery")])
        fake = SimpleNamespace(temporal_resolution_hours=1, years=[2025],
                               _profile_start_hour=0)
        g, b = MasterProblemAdapter._outage_factors(
            fake, jl, sys, [("gas", None), ("pv", None)], [("li", None)])
        assert int(jl.length(g)) == 1 and int(jl.length(b)) == 1
        pv = np.asarray(jl.seval("d -> d[(2, 1)]")(g))
        assert len(pv) == 8760 and pv[:12].max() == 0.0 and pv[12:].min() == 1.0
        li = np.asarray(jl.seval("d -> d[(1, 0)]")(b))
        assert li[24:48].tolist() == [0.25] * 24


class TestInitialShareWithOutages:
    def test_outages_reduce_the_initial_share(self, tmp_path):
        """The initial RE share (the first growth cap's anchor) counts the
        renewables' scheduled outages, as the dispatch does: Cuba 2025's
        pv_2025 is out until each park is commissioned."""
        gens = {"solar": _make_generator("Solar", "Renewable", "Sun", [100.0, 100.0],
                                         availability_file="solar.csv")}
        sys_cfg = _make_system_config(num_nodes=2, generators=gens)
        sys_cfg = sys_cfg.model_copy(update={"outage_schedule": [
            _ow("solar", 0, 4380, node=1)]})
        orch = Orchestrator(_make_esfex_config(system_config=sys_cfg), output_dir=tmp_path)
        orch._availability_cache = {f"{sys_cfg.name}__solar": np.ones((8760, 2))}
        demand = np.full((8760, 2), 100.0)
        assert orch._calculate_initial_re_penetration(demand) == pytest.approx(0.75)
