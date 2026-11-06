"""Units built from master investments keep their technology's bidding curve.

The master prices a technology's output with its ``fuel_cost_curve``; the
virtual generator that carries the built capacity into the dispatch got only
the flat ``fuel_cost``, so the dispatch priced the same unit differently.
"""

import sys

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator  # noqa: E402
from test_zones import _make_system_config, _make_technology  # noqa: E402

from esfex.config.schema import (  # noqa: E402
    CostCurveBlock,
    CostCurveConfig,
    MasterProblemConfig,
    PenaltiesConfig,
    TemporalConfig,
)


@pytest.mark.julia
def test_virtual_generator_gets_the_technology_curve(tmp_path, monkeypatch):
    from esfex.bridge import adapters
    from esfex.runner import Orchestrator

    demand = tmp_path / "demand.csv"
    np.savetxt(demand, np.full((8760, 1), 80.0), delimiter=",")
    # A small diesel unit cannot cover the load: the master builds gas.
    diesel = _make_generator("Diesel", "Non-renewable", "Diesel", [20.0],
                             degradation_rate=[0.0], fuel_cost=[400.0],
                             invest_max_power=[0.0])
    curve = CostCurveConfig(curve_type="stepwise", blocks=[
        CostCurveBlock(fraction=0.5, price=40.0),
        CostCurveBlock(fraction=0.5, price=120.0)])
    gas = _make_technology("gas_new", "Gas", 1, tech_type="Non-renewable",
                           invest_cost=20000.0, invest_max_power=500.0)
    gas = gas.model_copy(update={"fuel_cost": [80.0], "fuel_cost_curve": [curve]})
    sysc = _make_system_config(num_nodes=1, coords=[(23.0, -82.0)],
                               generators={"diesel": diesel},
                               technologies={"gas_new": gas})
    sysc = sysc.model_copy(update={
        "demand_path": str(demand),
        "penalties": PenaltiesConfig(loss_of_load=10000.0)})
    cfg = _make_esfex_config(
        sysc, simulation_mode="development",
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720,
                                overlap_hours=0),
        master_problem=MasterProblemConfig())

    seen = []
    real = adapters.build_gen_cost_curves_dict

    def spy(generators, *args, **kwargs):
        out = real(generators, *args, **kwargs)
        seen.append(([k for k, _ in generators],
                     {k: g.fuel_cost_curve for k, g in generators}, out, kwargs))
        return out

    monkeypatch.setattr(adapters, "build_gen_cost_curves_dict", spy)
    results = Orchestrator(cfg, output_dir=tmp_path / "out").run(years=1)
    assert results[0].investments.get("tech_investment_power_0_0", 0.0) > 1.0

    from esfex.bridge.julia_setup import get_julia
    haskey = get_julia().seval("haskey")
    dispatch_calls = [c for c in seen if "inv_gas_new" in c[1]]
    assert dispatch_calls, "no dispatch carried the gas investment"
    keys, curves, jl_curves, _ = dispatch_calls[0]
    assert curves["inv_gas_new"] is not None
    assert [b.price for b in curves["inv_gas_new"][0].blocks] == [40.0, 120.0]
    # ... and it reaches the Julia input as a multi-segment curve.
    assert haskey(jl_curves, keys.index("inv_gas_new") + 1)
    # The master gets the technology's curve on every bus of its node.
    master_calls = [c for c in seen if c[0] == ["gas_new"]]
    assert master_calls and haskey(master_calls[0][2], 1)
    assert master_calls[0][3].get("all_buses_of_node") is True


@pytest.mark.julia
def test_per_bus_curves_land_on_the_units_buses():
    """A virtual generator's arrays are per bus: curve i belongs to bus i,
    and buses without capacity get no curve."""
    from types import SimpleNamespace

    from esfex.bridge.converters import build_gen_cost_curves_dict
    from esfex.bridge.julia_setup import get_julia

    curve = CostCurveConfig(curve_type="stepwise", blocks=[
        CostCurveBlock(fraction=0.5, price=40.0),
        CostCurveBlock(fraction=0.5, price=120.0)])
    vgen = SimpleNamespace(rated_power=[0.0, 50.0, 0.0], fuel_cost=[80.0] * 3,
                           fuel_cost_curve=[curve] * 3)
    out = build_gen_cost_curves_dict([("inv_gas", vgen)], [], 3,
                                     bus_to_node=[0, 0, 1],
                                     per_bus_keys={"inv_gas"})
    keys = get_julia().seval("d -> sort(collect(keys(d[1])))")(out)
    assert list(keys) == [2]


@pytest.mark.julia
def test_technology_curves_land_on_every_bus_of_the_node():
    """The master may build a candidate technology at any bus of a node (its
    per-bus arrays are laid out in "property" mode), so every bus of the node
    gets the node's curve. The curve landed only on the node's first bus, and
    a technology built at another bus was priced at its flat fuel_cost."""
    from types import SimpleNamespace

    from esfex.bridge.converters import build_gen_cost_curves_dict
    from esfex.bridge.julia_setup import get_julia

    curve = CostCurveConfig(curve_type="stepwise", blocks=[
        CostCurveBlock(fraction=0.5, price=40.0),
        CostCurveBlock(fraction=0.5, price=120.0)])
    tech = SimpleNamespace(fuel_cost=[80.0, 80.0], fuel_cost_curve=[curve, None])
    out = build_gen_cost_curves_dict([("gas_new", tech)], [], 3,
                                     bus_to_node=[0, 1, 0],
                                     all_buses_of_node=True)
    keys = get_julia().seval("d -> sort(collect(keys(d[1])))")(out)
    assert list(keys) == [1, 3]
