"""Each system of a merged multi-system run keeps its own CO2 budget.

The merge used to give the merged system the first system's budget and the
largest ``co2_budget_violation`` penalty: in the Cuba case (Cuba 50 $/t,
IslaJuventud 500 $/t, both 1 Mt/year) every tonne Cuba emitted over 1 Mt was
priced at 500 $/t, and IslaJuventud's emissions counted against Cuba's budget.
"""

import sys

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator, _make_system_config  # noqa: E402

from esfex.config.schema import (  # noqa: E402
    CO2BudgetConfig,
    FuelConfig,
    MetaNetworkConfig,
    PenaltiesConfig,
    TemporalConfig,
)
from esfex.runner import Orchestrator  # noqa: E402

BUDGET = 100_000.0     # t/year, both systems
LOAD = 50.0            # MW, both systems


def _system(name, penalty, tmp_path=None, enabled=True):
    """One node, 50 MW of load, coal (1 t/MWh, 60 $/MWh) and clean gas (150 $/MWh)."""
    def gen(label, fuel, cost):
        return _make_generator(label, "Non-renewable", fuel, [100.0], degradation_rate=[0.0],
                               min_power=[0.0], eff_at_rated=[0.5], fuel_cost=[cost],
                               fixed_cost=[0.0], maintenance_cost=[0.0], invest_max_power=[0.0])
    s = _make_system_config(num_nodes=1, name=name,
                            generators={"coal": gen("Coal", "Coal", 60.0),
                                        "gas": gen("Gas", "Gas", 150.0)})
    update = {
        "fuels": {"Coal": FuelConfig(name="Coal", emission_factor=0.5, price_base=60.0),
                  "Gas": FuelConfig(name="Gas", emission_factor=0.0, price_base=150.0)},
        "penalties": PenaltiesConfig(loss_of_load=10000.0, co2_cost=0.0,
                                     co2_budget_violation=penalty),
        "co2_budget": CO2BudgetConfig(enabled=enabled, annual_budget=BUDGET),
    }
    if tmp_path is not None:
        path = tmp_path / f"{name}.csv"
        np.savetxt(path, np.full(8760, LOAD))
        update["demand_path"] = str(path)
    return s.model_copy(update=update)


def _config(tmp_path=None, b_enabled=True, **kw):
    a = _system("A", 50.0, tmp_path)
    b = _system("B", 500.0, tmp_path, enabled=b_enabled)
    return _make_esfex_config(a, systems={"A": a, "B": b},
                              meta_network=MetaNetworkConfig(systems=["A", "B"]), **kw)


def test_merge_keeps_each_system_budget_and_penalty():
    master, op, _ = Orchestrator._merge_systems(_config(b_enabled=False))
    for merged in (master, op):
        groups = [(g.system, g.first_node, g.num_nodes, g.enabled, g.annual_budget,
                   g.violation_penalty) for g in merged.co2_budget_by_system]
        assert groups == [("A", 0, 1, True, BUDGET, 50.0), ("B", 1, 1, False, BUDGET, 500.0)]
        # The merged scalar budget is the total of the enabled ones
        assert merged.co2_budget.enabled
        assert merged.co2_budget.annual_budget == BUDGET
    # Internal: never written back to YAML
    assert "co2_budget_by_system" not in op.model_dump()


def test_single_system_has_no_per_system_budgets():
    s = _system("A", 50.0)
    s = s.model_copy(update={"nodes": _make_system_config(num_nodes=2).nodes})
    master, op, _ = Orchestrator._merge_systems(_make_esfex_config(s))
    assert op.co2_budget_by_system == []
    assert op.co2_budget == s.co2_budget


@pytest.mark.julia
def test_each_system_pays_its_own_co2_penalty(tmp_path):
    cfg = _config(tmp_path, simulation_mode="economic_dispatch",
                  temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720,
                                          overlap_hours=0))
    res = Orchestrator(cfg, output_dir=tmp_path / "out").run(years=1)[0]
    co2 = np.asarray(res.co2_emissions) * 24.0     # [node, step]: t/h per 24 h step → t
    a_t, b_t = float(np.nansum(co2[0])), float(np.nansum(co2[1]))
    # A: coal + 50 $/t (110 $/MWh) beats gas, so coal serves all of A's load
    assert a_t == pytest.approx(LOAD * 8760, rel=1e-3)
    # B: coal + 500 $/t loses to gas, so B stops at its own budget
    assert b_t == pytest.approx(BUDGET, rel=1e-3)
