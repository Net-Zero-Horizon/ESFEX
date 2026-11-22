"""Annual budgets: investment (master) and operation (dispatch) are separate.

``max_annual_system_cost`` meant two things: the master's cap on each build
year's overnight investment, and, prorated to each dispatch window by its
hours, a cap on the window's fuel, maintenance and fixed O&M. The dispatch
could not meet it (its fleet is fixed) and paid a penalty that was in the
objective but in no component of the cost breakdown. Now:

- ``max_annual_investment`` (old key read as it) is the master's budget only;
- ``max_annual_operating_cost`` (off by default) is the dispatch's annual
  operating budget, shared out among the year's windows by demand, with what
  a window leaves unspent available to the next; spending above it costs
  1000 $ per $, so unaffordable fuel becomes unserved energy.
"""

import logging
import sys

import h5py
import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator  # noqa: E402
from test_zones import _make_system_config  # noqa: E402

from esfex.bridge.converters import COST_BREAKDOWN_NON_COMPONENTS  # noqa: E402
from esfex.config.schema import (  # noqa: E402
    OutageWindow,
    PenaltiesConfig,
    SystemConfig,
    TemporalConfig,
)
from esfex.runner import window_operating_budget  # noqa: E402

# Two windows a year at daily steps: 183 and 182 days
TWO_WINDOWS = TemporalConfig(resolution_hours=24, rolling_horizon_hours=183 * 24,
                             overlap_hours=0)
W1_HOURS = 183 * 24
FUEL = 50.0       # $/MWh
LOAD = 10.0       # MW


def _gen(name, rated, fuel_cost, **kw):
    return _make_generator(name, "Non-renewable", "Gas", [rated], fuel_cost=[fuel_cost],
                           maintenance_cost=[0.0], fixed_cost=[kw.pop("fixed_cost", 0.0)],
                           start_up_cost=[0.0], degradation_rate=[0.0], initial_age=[0],
                           **kw)


def _run(tmp_path, tag, generators, outages=(), **sys_kw):
    from esfex.runner import Orchestrator

    demand = tmp_path / "demand.csv"
    np.savetxt(demand, np.full((8760, 1), LOAD), delimiter=",")
    sysc = _make_system_config(num_nodes=1, coords=[(23.0, -82.0)], generators=generators)
    sysc = sysc.model_copy(update={
        "demand_path": str(demand),
        "penalties": PenaltiesConfig(loss_of_load=1000.0),
        "outage_schedule": list(outages),
    })
    # Through validation, so an old key is read as files read it
    sysc = SystemConfig.model_validate({**sysc.model_dump(), **sys_kw})
    cfg = _make_esfex_config(sysc, simulation_mode="economic_dispatch", temporal=TWO_WINDOWS)
    out = tmp_path / tag
    return Orchestrator(cfg, output_dir=out).run(years=1)[0], out


def _components(cb):
    return sum(v for k, v in cb.items() if k not in COST_BREAKDOWN_NON_COMPONENTS)


# ---------------------------------------------------------------------------
# Config: the two keys and the old one
# ---------------------------------------------------------------------------

def _sys(**kw):
    return SystemConfig(name="s", nodes={"num_nodes": 1, "nodes_connections": [0.0]}, **kw)


@pytest.mark.parametrize("key", ["max_annual_system_cost", "MAX_ANNUAL_SYSTEM_COST"])
def test_old_key_is_the_investment_budget(key, caplog):
    with caplog.at_level(logging.INFO):
        s = _sys(**{key: 5e8})
    assert s.max_annual_investment == 5e8
    assert s.max_annual_operating_cost is None
    assert s.budget_scope == "investment"
    assert any("max_annual_investment" in r.message for r in caplog.records)


def test_new_key_wins_over_old():
    s = _sys(max_annual_system_cost=5e8, max_annual_investment=1e8)
    assert s.max_annual_investment == 1e8


def test_old_key_is_not_unknown():
    from esfex.config.unknown_keys import find_unknown_system_keys

    assert find_unknown_system_keys({"MAX_ANNUAL_SYSTEM_COST": 1.0,
                                     "max_annual_system_cost": 1.0,
                                     "max_annual_investment": 1.0,
                                     "max_annual_operating_cost": 1.0,
                                     "budget_scope": "total"}) == []


def test_window_share_follows_demand_and_what_is_left():
    # Half of the remaining demand: half of what is left
    assert window_operating_budget(100.0, 5.0, 10.0) == pytest.approx(50.0)
    # The last window gets all that is left; an overspent year gives 0
    assert window_operating_budget(30.0, 4.0, 4.0) == pytest.approx(30.0)
    assert window_operating_budget(-5.0, 4.0, 8.0) == 0.0


# ---------------------------------------------------------------------------
# Dispatch runs
# ---------------------------------------------------------------------------

@pytest.mark.julia
def test_investment_budget_does_not_cap_the_dispatch(tmp_path):
    """A 1 $ investment budget (old key) leaves the dispatch alone: it used
    to cap each window's fuel and pay a penalty outside every component."""
    res, _ = _run(tmp_path, "inv", {"gas": _gen("Gas", 60.0, FUEL)},
                  max_annual_system_cost=1.0)
    cb = res.cost_breakdown
    fuel = LOAD * 8760 * FUEL
    assert float(np.nansum(res.load_shed_array)) == pytest.approx(0.0, abs=1e-3)
    assert cb["fuel_cost"] == pytest.approx(fuel, rel=1e-6)
    assert cb.get("operating_budget_penalty", 0.0) == pytest.approx(0.0, abs=1e-6)
    assert res.objective == pytest.approx(fuel, rel=1e-6)
    assert cb["total"] == pytest.approx(_components(cb), rel=1e-9)


@pytest.mark.julia
def test_operating_budget_sheds_and_carries_underspend(tmp_path):
    """A free 5 MW unit runs in the first window only: window 1 spends less
    than its share of the budget and window 2 gets the rest. The budget
    cannot pay all the fuel, so window 2 sheds load (fuel scarcity)."""
    budget = 2.5e6
    gens = {"gas": _gen("Gas", 60.0, FUEL), "free": _gen("Free", 5.0, 0.0)}
    off = [OutageWindow(element_type="generator", element_id="free",
                        start_hour=W1_HOURS, end_hour=8760, availability=0.0)]
    res, out = _run(tmp_path, "op", gens, off, max_annual_operating_cost=budget)
    cb = res.cost_breakdown

    w1_spend = (LOAD - 5.0) * W1_HOURS * FUEL                # 1.098 M$
    assert w1_spend < budget * W1_HOURS / 8760               # underspent its share
    # Window 2 burns what is left (not its prorated share) and sheds the rest
    w2_mwh = (budget - w1_spend) / FUEL
    shed = LOAD * (8760 - W1_HOURS) - w2_mwh
    assert float(np.nansum(res.load_shed_array)) * 24 == pytest.approx(shed, rel=1e-4)
    assert res.load_shed == pytest.approx(shed, rel=1e-4)
    assert cb["fuel_cost"] == pytest.approx(budget, rel=1e-6)
    # Shedding is cheaper than buying fuel above the budget: no excess
    assert cb["operating_budget_excess"] == pytest.approx(0.0, abs=1e-3)
    assert cb["total"] == pytest.approx(_components(cb), rel=1e-9)


@pytest.mark.julia
def test_operating_budget_excess_reported(tmp_path, caplog):
    """Fixed O&M alone (1.8 M$) is above a 1 M$ budget: the 0.8 M$ excess and
    its penalty are in the breakdown and the HDF5, with a warning."""
    gens = {"gas": _gen("Gas", 60.0, FUEL, fixed_cost=30_000.0)}
    with caplog.at_level(logging.WARNING, logger="esfex.runner"):
        res, out = _run(tmp_path, "ex", gens, max_annual_operating_cost=1e6)
    cb = res.cost_breakdown
    assert cb["fixed_om_cost"] == pytest.approx(1.8e6, rel=1e-6)
    assert cb["operating_budget_excess"] == pytest.approx(0.8e6, rel=1e-6)
    assert cb["operating_budget_penalty"] == pytest.approx(1000 * 0.8e6, rel=1e-6)
    assert cb["fuel_cost"] == pytest.approx(0.0, abs=1e-3)       # all load shed
    assert cb["total"] == pytest.approx(_components(cb), rel=1e-9)
    assert any("max_annual_operating_cost" in r.message for r in caplog.records)
    (h5,) = out.rglob("*.h5")
    with h5py.File(h5, "r") as f:
        attrs = f[f"cost_breakdown/year_{res.year}"].attrs
        assert attrs["operating_budget_excess"] == pytest.approx(0.8e6, rel=1e-6)
        assert attrs["operating_budget_penalty"] == pytest.approx(0.8e9, rel=1e-6)
