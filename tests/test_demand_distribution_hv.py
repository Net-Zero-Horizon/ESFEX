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
