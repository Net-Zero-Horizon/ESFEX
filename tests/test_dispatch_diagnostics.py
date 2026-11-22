"""Dispatch reporting found on the Cuba case (economic_dispatch, one year).

- Demand at a node with no load bus sits in no power balance: it was
  dropped silently (the HDF5 showed its demand with ``loss_load`` 0). It is
  now unserved load at that node, priced at the loss-of-load penalty.
- That node's warning, and the "Dropping solver option" warnings, were
  logged for every window (627 lines a year); now once per run.
- The window balance line added the reserves to the demand side and left
  out the transmission losses, so it showed gaps of several percent.
"""

import logging
import re
import sys

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator, _make_node_config, _make_system_config  # noqa: E402

from esfex.bridge import adapters
from esfex.bridge.adapters import _add_unattached_load_shed, _nodes_without_load_bus
from esfex.config.schema import BusConfig, PenaltiesConfig, TemporalConfig
from esfex.runner import Orchestrator

LOAD = 50.0
VOLL = 1000.0


class _Temporal:
    resolution_hours = 2


class _JlInput:
    temporal = _Temporal()
    loss_of_load_penalty = VOLL * 1e-6      # model scale (M$/MWh)


def test_nodes_without_load_bus():
    buses = [BusConfig(bus_id="a", parent_node=1),
             BusConfig(bus_id="b", parent_node=2, role="connection", demand_fraction=0.0),
             BusConfig(bus_id="c", parent_node=3, demand_fraction=0.0),
             BusConfig(bus_id="d", parent_node=3, role="mixed", demand_fraction=0.5)]
    # Node 0: no bus; node 2: only a connection bus; node 3 carries half.
    assert _nodes_without_load_bus(buses, 4) == [0, 2]


def test_unattached_demand_is_unserved_load():
    demand = np.array([[10.0, 1.0], [20.0, 1.0], [30.0, 1.0]])   # [step, node]
    result = {"objective": 5.0, "load_shed_total": 1.0,
              "cost_breakdown": {"load_shedding_cost": 2.0, "total": 5.0}}
    # Two of the three steps are committed; steps last 2 h.
    _add_unattached_load_shed(result, demand, [0], _JlInput(), keep_steps=2)
    assert result["unattached_load_shed"].tolist() == [[10.0, 20.0, 30.0], [0.0, 0.0, 0.0]]
    energy = (10.0 + 20.0) * 2
    assert result["load_shed_total"] == pytest.approx(1.0 + energy)
    assert result["cost_breakdown"]["load_shedding_cost"] == pytest.approx(2.0 + energy * VOLL)
    assert result["cost_breakdown"]["total"] == pytest.approx(5.0 + energy * VOLL)
    assert result["objective"] == pytest.approx(5.0 + energy * VOLL)


def test_warn_once_per_run(caplog):
    adapters.reset_run_warnings()
    with caplog.at_level(logging.WARNING, logger="esfex.bridge.adapters"):
        for _ in range(3):
            adapters._warn_once(("k", 1), "repeated %s", "x")
        adapters.reset_run_warnings()
        adapters._warn_once(("k", 1), "repeated %s", "x")
    assert [r.getMessage() for r in caplog.records] == ["repeated x"] * 2


def _system(tmp_path, buses, solver_options=None):
    """Two nodes with 50 MW each, one 300 MW gas unit at node 1."""
    gas = _make_generator("Gas", "Non-renewable", "Gas", [0.0, 300.0],
                          degradation_rate=[0.0, 0.0], min_power=[0.0, 0.0],
                          fuel_cost=[50.0, 50.0], fixed_cost=[0.0, 0.0],
                          maintenance_cost=[0.0, 0.0])
    path = tmp_path / "demand.csv"
    np.savetxt(path, np.full((8760, 2), LOAD), delimiter=",")
    s = _make_system_config(num_nodes=2, name="S", generators={"gas": gas})
    nodes = _make_node_config(2).model_copy(update={
        "reserve_static": [0.0, 0.0], "reserve_dynamic": [0.0, 0.0]})
    s = s.model_copy(update={
        "nodes": nodes, "demand_path": str(path), "buses": buses,
        "reserve_static_default_ratio": 0.2, "target_re_penetration": 0.0,
        "penalties": PenaltiesConfig(loss_of_load=VOLL, co2_cost=0.0)})
    cfg = _make_esfex_config(s, simulation_mode="economic_dispatch",
                             temporal=TemporalConfig(resolution_hours=24,
                                                     rolling_horizon_hours=720,
                                                     overlap_hours=0))
    if solver_options:
        cfg = cfg.model_copy(update={"solver": cfg.solver.model_copy(
            update={"options": solver_options})})
    return cfg


@pytest.mark.julia
def test_node_without_bus_reports_unserved_load(tmp_path, caplog):
    # Node 0 has demand and no bus: its 50 MW can never be served.
    cfg = _system(tmp_path, [BusConfig(bus_id="b1", parent_node=1)],
                  solver_options={"FeasibilityTol": 1e-6})   # a Gurobi option on HiGHS
    with caplog.at_level(logging.WARNING, logger="esfex.bridge.adapters"):
        res = Orchestrator(cfg, output_dir=tmp_path / "out").run(years=1)[0]
    shed = np.asarray(res.load_shed_array)
    assert np.allclose(shed[0], LOAD) and np.allclose(shed[1], 0.0)
    unserved = LOAD * 8760
    assert res.load_shed == pytest.approx(unserved, rel=1e-9)
    assert res.cost_breakdown["load_shedding_cost"] == pytest.approx(unserved * VOLL, rel=1e-9)
    # 13 windows, each warning logged once
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("no operational load bus" in m for m in msgs) == 1
    assert sum("Dropping solver option 'FeasibilityTol'" in m for m in msgs) == 1


@pytest.mark.julia
def test_window_energy_balance_closes_with_losses(tmp_path, caplog):
    # Node 0's load is served over the line from node 1: the line losses
    # are in the balance, and the 20 % reserve is not.
    cfg = _system(tmp_path, None)
    with caplog.at_level(logging.DEBUG, logger="esfex.runner"):
        Orchestrator(cfg, output_dir=tmp_path / "out").run(years=1)
    pat = re.compile(r"supply=([\d.]+) use=([\d.]+) gap=(-?[\d.]+) .*losses=([\d.]+)")
    lines = [pat.search(r.getMessage()) for r in caplog.records
             if "energy balance (MWh)" in r.getMessage()]
    assert len(lines) == 13
    for m in lines:
        supply, use, gap, losses = map(float, m.groups())
        assert abs(gap) <= 1e-6 * supply + 0.1
        assert losses > 0
