"""Demand distribution across busbars for faithful HV grids.

A faithful OSM import produces HV-only substations, so every busbar starts as
``role="connection"``. ``repair_bus_roles_and_demand`` then promotes exactly
ONE bus per node to ``load`` (so node demand has a carrier). That left the Grid
Builder's demand distribution — which required >= 2 ``load``/``mixed`` buses —
seeing "0 multi-bus nodes" and doing nothing. The fix falls back to *all* of a
node's busbars when it has fewer than two load/mixed ones.
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest


@pytest.fixture(scope="module")
def _dist_buses():
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from esfex.visualization.workflows.grid_mapping_steps import (
        GridMappingDemandStep,
    )
    return GridMappingDemandStep._distribution_buses


def _bus(bid, node, role):
    return NS(bus_id=bid, parent_node=node, role=role,
              latitude=1.0, longitude=1.0, voltage_kv=220.0)


def _state(*buses):
    return NS(buses={b.bus_id: b for b in buses})


class TestDistributionBuses:
    def test_prefers_load_mixed_when_enough(self, _dist_buses):
        st = _state(_bus("b1", 0, "load"), _bus("b2", 0, "mixed"),
                    _bus("b3", 0, "connection"))
        got = {b.bus_id for b in _dist_buses(st, 0)}
        assert got == {"b1", "b2"}  # the connection bus is not needed

    def test_hv_node_falls_back_to_all_busbars(self, _dist_buses):
        # One promoted load bus + two HV connection busbars → split across all.
        st = _state(_bus("b1", 0, "load"), _bus("b2", 0, "connection"),
                    _bus("b3", 0, "connection"))
        got = {b.bus_id for b in _dist_buses(st, 0)}
        assert got == {"b1", "b2", "b3"}

    def test_single_busbar_node_not_eligible(self, _dist_buses):
        st = _state(_bus("b1", 0, "connection"), _bus("bx", 1, "load"))
        assert len(_dist_buses(st, 0)) < 2  # nothing to split

    def test_only_this_node_buses_considered(self, _dist_buses):
        st = _state(_bus("b1", 0, "connection"), _bus("b2", 0, "connection"),
                    _bus("z1", 1, "load"), _bus("z2", 1, "load"))
        got = {b.bus_id for b in _dist_buses(st, 0)}
        assert got == {"b1", "b2"}


@pytest.fixture(scope="module")
def _demand_step_cls():
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from esfex.visualization.workflows.grid_mapping_steps import (
        GridMappingDemandStep,
    )
    return GridMappingDemandStep


class TestSpatialDistributionSingleBusNodes:
    """After the PyPSA-style node-mesh reduction every node is a single bus, so
    the transport split has nothing to divide. The distribution must NOT skip
    them silently — that left ``_assignments`` empty, disabled Apply, and
    dropped ALL per-node forecast demand."""

    def _load_bus(self, bid, node, lat, lon):
        return NS(bus_id=bid, name=bid, parent_node=node, role="load",
                  latitude=lat, longitude=lon, voltage_kv=275.0,
                  demand_fraction=1.0)

    def _run(self, cls, buses, nodes, res):
        state = NS(buses={b.bus_id: b for b in buses}, transformers=[])
        flags = {"apply_enabled": None}
        stub = NS(
            _forecast_result=res,
            _model=NS(state=state),
            _nodes_in_scope=lambda: nodes,
            _distribution_buses=cls._distribution_buses,
            _assignments=[],
            _render_assignments_table=lambda: None,
            _lbl_bld_status=NS(setText=lambda *a, **k: None),
            _progress=NS(setValue=lambda *a, **k: None),
            _btn_apply=NS(setEnabled=lambda v: flags.__setitem__(
                "apply_enabled", v)),
        )
        cls._distribute_by_spatial_demand(stub)
        return stub, flags

    def test_single_bus_nodes_get_full_fraction_and_enable_apply(
            self, _demand_step_cls):
        cls = _demand_step_cls
        buses = [self._load_bus("node_0", 0, 35.0, 139.0),
                 self._load_bus("node_1", 1, 36.0, 140.0)]
        nodes = [NS(index=0, name="N0", centroid_lat=35.0, centroid_lng=139.0),
                 NS(index=1, name="N1", centroid_lat=36.0, centroid_lng=140.0)]
        res = NS(cell_lats=[35.0, 36.0], cell_lons=[139.0, 140.0],
                 cell_annual_mwh=[100.0, 200.0], cell_nodes=[0, 1])

        stub, flags = self._run(cls, buses, nodes, res)

        # One assignment per single-bus node, each carrying all its demand.
        assert len(stub._assignments) == 2
        assert all(a["new_fraction"] == 1.0 for a in stub._assignments)
        assert {a["bus_id"] for a in stub._assignments} == {"node_0", "node_1"}
        # Apply is enabled so the per-node forecast demand is actually written.
        assert flags["apply_enabled"] is True
