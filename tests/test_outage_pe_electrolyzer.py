"""Scheduled outages of electrolyzers, fuel tanks and fuel routes take effect.

The loader accepted ``electrolyzer``, ``fuel_storage`` and ``fuel_route``
outage windows, but nothing read them: the units ran through their outages.
"""

import sys

import numpy as np
import pytest

sys.path.insert(0, "tests")

from esfex.config.schema import OutageWindow  # noqa: E402


def _ow(etype, eid, start, end, avail=0.0, node=None):
    return OutageWindow(element_type=etype, element_id=eid, start_hour=start,
                        end_hour=end, availability=avail, node=node)


# ---------------------------------------------------------------------------
# Multi-system merge: ids and nodes of the new types
# ---------------------------------------------------------------------------

def test_merge_remaps_electrolyzer_and_tank_windows():
    from test_merge_node_connections import _two_systems
    from test_electrolyzer_dispatch_results import _elz
    from esfex.runner import Orchestrator

    cfg = _two_systems()
    b = cfg.systems["B"].model_copy(update={
        "electrolyzers": {"elz": _elz("Elz", 5.0)},
        "outage_schedule": [_ow("electrolyzer", "elz", 0, 24),
                            _ow("fuel_storage", "Gas", 0, 24),
                            _ow("fuel_route", "r1", 0, 24)]})
    cfg = cfg.model_copy(update={"systems": {"A": cfg.systems["A"], "B": b}})
    _, op, _ = Orchestrator._merge_systems(cfg)
    got = sorted((ow.element_type, ow.element_id, ow.node) for ow in op.outage_schedule)
    # B's single node is merged node 2; its tank window covers only that node
    assert got == [("electrolyzer", "B__elz", None), ("fuel_route", "r1", None),
                   ("fuel_storage", "Gas", 2)]


def test_unknown_electrolyzer_or_fuel_is_warned(caplog):
    from test_zones import _make_system_config
    from esfex.config.schema import SystemConfig

    data = _make_system_config(num_nodes=1, coords=[(23.0, -82.0)],
                               generators={}).model_dump(by_alias=True)
    data["outage_schedule"] = [_ow("electrolyzer", "nope", 0, 24).model_dump(),
                               _ow("fuel_storage", "Unobtainium", 0, 24).model_dump()]
    with caplog.at_level("WARNING"):
        SystemConfig.model_validate(data)
    assert "electrolyzer 'nope'" in caplog.text
    assert "fuel_storage 'Unobtainium'" in caplog.text


# ---------------------------------------------------------------------------
# Electrolyzer: dispatch
# ---------------------------------------------------------------------------

@pytest.mark.julia
def test_electrolyzer_outage_stops_it(tmp_path):
    from test_electrolyzer_dispatch_results import _elz
    from test_runner import _make_esfex_config, _make_generator
    from test_zones import _make_system_config
    from esfex.config.schema import TemporalConfig
    from esfex.runner import Orchestrator

    demand = tmp_path / "demand.csv"
    np.savetxt(demand, np.full((8760, 1), 10.0), delimiter=",")
    gas = _make_generator("Gas", "Non-renewable", "Gas", [100.0],
                          degradation_rate=[0.0], fuel_cost=[50.0],
                          start_up_cost=[0.0], initial_age=[0])
    sysc = _make_system_config(num_nodes=1, coords=[(23.0, -82.0)],
                               generators={"gas": gas})
    sysc = sysc.model_copy(update={
        "demand_path": str(demand),
        "electrolyzers": {"elz_a": _elz("ElzA", 5.0), "elz_b": _elz("ElzB", 3.0)},
        # elz_b out on days 3-4, half available on day 6
        "outage_schedule": [_ow("electrolyzer", "elz_b", 48, 96),
                            _ow("electrolyzer", "elz_b", 120, 144, avail=0.5)]})
    cfg = _make_esfex_config(
        sysc, simulation_mode="economic_dispatch",
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720,
                                overlap_hours=0))
    res = Orchestrator(cfg, output_dir=tmp_path / "out").run(years=1)[0]
    b = res.electrolyzer_power[1, 0]
    assert b[:2] == pytest.approx([3.0, 3.0], abs=1e-5)
    assert b[2:4] == pytest.approx([0.0, 0.0], abs=1e-5)
    assert b[4] == pytest.approx(3.0, abs=1e-5)
    assert b[5] == pytest.approx(1.5, abs=1e-5)
    assert np.allclose(res.electrolyzer_power[0, 0], 5.0, atol=1e-5)   # elz_a untouched


# ---------------------------------------------------------------------------
# Fuel tanks and routes: primary-energy block
# ---------------------------------------------------------------------------

def _shortfall(adapter):
    """Solve the adapter's block alone (with its outage masks); total shortfall."""
    from esfex.bridge.julia_setup import get_julia

    jl = get_julia()
    jl._pe_in = adapter._create_julia_input()
    jl._pe_masks = adapter._outage_masks()
    jl.seval("using JuMP, HiGHS")
    return float(jl.seval("""
        let m = Model(ESFEX.create_optimizer(solver_name = "highs"))
            set_silent(m)
            vars, temporal, prices = ESFEX.create_primary_energy_model(m, _pe_in;
                                                                       outage_masks = _pe_masks)
            terms = ESFEX.get_primary_energy_objective_terms(vars, _pe_in, temporal, prices)
            @objective(m, Min, sum(values(terms)))
            optimize!(m)
            @assert termination_status(m) == MOI.OPTIMAL
            sum(sum(value.(v)) for v in values(vars.ne_demand_shortfall_periodic)) +
            sum(sum(value.(v)) for v in values(vars.fuel_loss_of_supply_hourly))
        end"""))


@pytest.mark.julia
def test_tank_outage_locks_its_fuel():
    from test_primary_energy_fixes import _adapter

    # A full 100-unit tank, no supply, 30/day of demand for 3 days (90)
    fuels = {"Gas": {"max_availability": [0.0, 0.0], "storage_capacity": [100.0, 0.0],
                     "initial_storage_level": [1.0, 0.0], "import_cost": [0.0, 0.0]}}
    ne = {"Industrial_Gas": {"fuel": "Gas", "demand": [3 * 3650.0, 0.0]}}

    def short(schedule, year=2026):
        return _shortfall(_adapter(year=year, fuels_config=fuels, non_electric_demand=ne,
                                   mode="economic_dispatch", outage_schedule=schedule))
    assert short([]) == pytest.approx(0.0, abs=1e-6)
    # Out of service for the whole window (horizon hours of 2026): nothing drawn
    assert short([_ow("fuel_storage", "Gas", 8760, 8760 + 72)]) == pytest.approx(90.0, rel=1e-6)
    # Half out of service: 50 usable
    assert short([_ow("fuel_storage", "Gas", 8760, 8760 + 72, avail=0.5)]) == \
        pytest.approx(40.0, rel=1e-6)
    # Another node's tank, or another year: no effect
    assert short([_ow("fuel_storage", "Gas", 8760, 8760 + 72, node=1)]) == \
        pytest.approx(0.0, abs=1e-6)
    assert short([_ow("fuel_storage", "Gas", 0, 72)]) == pytest.approx(0.0, abs=1e-6)


@pytest.mark.julia
def test_route_outage_stops_shipments(caplog):
    from test_primary_energy_fixes import _route

    a = _route()
    assert _shortfall(a) == pytest.approx(0.0, abs=1e-6)
    # Route out on day 1: node 2 (no tank, no supply) is short that day
    a.outage_schedule = [_ow("fuel_route", "r", 0, 24)]
    assert _shortfall(a) == pytest.approx(10.0, rel=1e-6)
    with caplog.at_level("WARNING"):
        a.outage_schedule = [_ow("fuel_route", "nope", 0, 24)]
        assert _shortfall(a) == pytest.approx(0.0, abs=1e-6)
    assert "fuel route 'nope'" in caplog.text
