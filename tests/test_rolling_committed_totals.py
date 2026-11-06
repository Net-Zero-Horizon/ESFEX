"""Rolling-horizon year totals add up the committed steps of each window.

A window solves its kept steps plus an overlap tail that the next window
solves again. The year's objective, energies, CO2 and cost breakdown used to
take each window's whole-window value times keep/hours, which mixes the
overlap hours in: with a load that grows over the window the totals no
longer matched the committed dispatch the results file stores.
"""

import sys

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_results_hdf5_fixes import _rolling_orch  # noqa: E402

pytestmark = pytest.mark.julia

FUEL = 50.0   # $/MWh of fuel for every unit of the default test system


def _run_year(tmp_path, hours=72, window=24, overlap=12):
    orch = _rolling_orch(tmp_path, hours=hours)
    orch._availability_cache, orch._inflow_cache = {}, {}
    orch._gen_names, orch._bat_names = [], []
    orch._num_nodes = 2
    t = orch.config.temporal
    t.rolling_horizon_hours, t.overlap_hours = window, overlap
    # A load that grows over the horizon: every overlap tail is heavier
    # than the window's average.
    ramp = np.linspace(20.0, 120.0, hours)
    demand = np.column_stack([ramp, 0.5 * ramp])
    yr = orch._run_operational_dispatch(
        year=2025, year_idx=0, num_years=1,
        demand=demand, hours=hours, num_nodes=2, units_config=orch._gather_units(),
    )
    return yr, demand


def test_year_totals_match_the_committed_dispatch(tmp_path):
    yr, demand = _run_year(tmp_path)
    gen = np.asarray(yr.gen_output)
    assert gen.shape[-1] == demand.shape[0]
    assert yr.total_demand == pytest.approx(demand.sum(), rel=1e-9)
    assert yr.total_generation == pytest.approx(gen.sum(), rel=1e-6)
    shed = np.asarray(yr.load_shed_array) if yr.load_shed_array is not None else 0.0
    assert yr.load_shed == pytest.approx(np.sum(shed), abs=1e-6)
    bd = yr.cost_breakdown
    assert bd["fuel_cost"] == pytest.approx(FUEL * gen.sum(), rel=1e-6)
    # The objective is the sum of its parts (v2g compensation is reported only)
    parts = sum(v for k, v in bd.items() if k not in ("total", "v2g_compensation"))
    assert yr.objective == pytest.approx(parts, rel=1e-6)
    assert bd["total"] == pytest.approx(yr.objective, rel=1e-9)


def test_committed_extraction_of_one_window():
    """keep_steps restricts the objective, totals and breakdown to the first
    steps; the arrays still span the window."""
    from esfex.bridge.adapters import PowerSystemAdapter
    from test_runner import _make_esfex_config

    hours, keep = 12, 7
    ramp = np.linspace(20.0, 120.0, hours)
    ps = PowerSystemAdapter(
        config=_make_esfex_config(), demand=np.column_stack([ramp, 0.5 * ramp]),
        hours=hours, num_nodes=2, year=2025, base_year=2025, mode="economic_dispatch",
    )
    ps.build_model()
    assert ps.solve() == 1
    full = ps.get_solution_values()
    part = ps.get_solution_values(keep_steps=keep)
    gen = np.asarray(part["gen_output"])
    assert gen.shape[-1] == hours
    assert part["total_generation"] == pytest.approx(gen[..., :keep].sum(), rel=1e-6)
    assert part["total_demand"] == pytest.approx(1.5 * ramp[:keep].sum(), rel=1e-9)
    assert part["cost_breakdown"]["fuel_cost"] == pytest.approx(
        FUEL * gen[..., :keep].sum(), rel=1e-6)
    parts = sum(v for k, v in part["cost_breakdown"].items()
                if k not in ("total", "v2g_compensation"))
    assert part["objective"] == pytest.approx(parts, rel=1e-6)
    # Not the prorated window: the kept steps carry less than keep/hours of
    # a growing load's cost
    assert part["objective"] < full["objective"] * keep / hours
    # All steps kept: the plain extraction
    assert ps.get_solution_values(keep_steps=hours)["objective"] == pytest.approx(
        full["objective"], rel=1e-12)


def test_primary_energy_costs_of_the_committed_steps():
    """Period quantities count for the kept share of their period, the tank
    drawdown runs from the window's start to the end of the kept steps."""
    from esfex.bridge.julia_setup import get_julia
    from test_primary_energy_fixes import _adapter

    jl = get_julia()
    a = _adapter()                     # 72 steps, 24-step periods, 10 $/unit
    jl._pe_in = a._create_julia_input()
    jl.seval("using JuMP, HiGHS")
    m, pe_vars, temporal, prices = jl.seval("""
        let m = Model(ESFEX.create_optimizer(solver_name = "highs"))
            set_silent(m)
            vars, temporal, prices = ESFEX.create_primary_energy_model(m, _pe_in)
            @objective(m, Min, sum(values(ESFEX.get_primary_energy_objective_terms(
                vars, _pe_in, temporal, prices))))
            optimize!(m)
            (m, vars, temporal, prices)
        end""")
    a._jl_model, a._jl_vars, a._jl_input, a._jl_temporal = m, pe_vars, jl._pe_in, temporal
    keep = 30                          # all of period 1, a quarter of period 2
    jl._v, jl._t, jl._p = pe_vars, temporal, prices
    supply, level, committed = jl.seval(f"""
        let cs = ESFEX.committed_steps(nothing, 72, {keep}; pe_vars = _v, pe_temporal = _t)
            terms = ESFEX.get_primary_energy_objective_terms(_v, _pe_in, _t, _p)
            (JuMP.value.(_v.fuel_supply_periodic["Gas"]),
             JuMP.value.(_v.fuel_storage_level_hourly["Gas"]),
             ESFEX.committed_value(cs, terms[:pe_supply_cost]))
        end""")
    supply, level = np.asarray(supply), np.asarray(level)
    # Node 1's tank keeps emptying after the kept steps (supply < demand)
    assert level[0, keep] - level[0, -1] > 1.0
    price = float(jl.seval('_p["Gas"]'))   # M$/unit (solver scale)
    share = np.array([1.0, 6 / 24, 0.0])
    expected = price * ((supply * share).sum() + (level[:, 0] - level[:, keep]).sum())
    assert committed == pytest.approx(expected, rel=1e-9)
    res = a.get_results(keep_steps=keep)
    assert res["total_fuel_cost"] == pytest.approx(10.0 * (supply * share).sum(), rel=1e-9)
    assert a.get_results()["total_fuel_cost"] == pytest.approx(10.0 * supply.sum(), rel=1e-9)


def test_fuel_for_power_reaches_the_results_file(tmp_path):
    """#85: YearResults.fuel_for_power was declared but never filled."""
    import h5py

    from esfex.runner import YearResults
    from test_results_hdf5_fixes import _init_h5
    from test_runner import _create_orchestrator

    orch = _create_orchestrator(tmp_path=tmp_path)
    orch._gen_names, orch._gen_fuels, orch._bat_names = [], [], []
    orch._bus_to_node = None
    path = _init_h5(orch, tmp_path, num_nodes=2)
    burnt = np.arange(6.0).reshape(2, 3)
    result = YearResults(
        year=2025, objective=1.0, solve_time=0.0, feasible=True,
        primary_energy={"total_fuel_supply": {"Gas": np.ones(1)}, "total_fuel_cost": 1.0,
                        "fuel_for_power": {"Cuba/Gas": burnt}},
        fuel_for_power={"Cuba/Gas": burnt},
    )
    orch._append_year_to_hdf5(path, result)
    with h5py.File(path, "r") as h5:
        ffp = h5["detailed_results/year_2025_threshold_0/primary_energy/fuel_for_power"]
        assert ffp.attrs["units"] == "fuel units per step"
        np.testing.assert_array_equal(ffp["Cuba_Gas"][()], burnt)
