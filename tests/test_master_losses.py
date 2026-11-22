"""The master plans with the transmission losses of ``dc_power_flow.loss_model``.

Its node links carried no resistance and its network ignored ``loss_model``, so
the plan was always lossless; and the linear model scaled the flow at the
lower-index end by (1 - λ), which created energy on flows leaving that end.
"""

import sys

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator  # noqa: E402
from test_zones import _make_system_config, _make_technology  # noqa: E402

from esfex.config.schema import (  # noqa: E402
    DCPowerFlowConfig,
    MasterProblemConfig,
    PenaltiesConfig,
    TemporalConfig,
    TransmissionLineGeo,
)
from esfex.runner import _node_link_impedances  # noqa: E402


def _line(a, b, r, x, circuits=1):
    return TransmissionLineGeo(from_node=a, to_node=b, from_bus=a, to_bus=b,
                               capacity_mw=100.0, resistance_pu=r, reactance_pu=x,
                               num_circuits=circuits)


def test_node_link_impedance_is_the_parallel_equivalent():
    dc = DCPowerFlowConfig()
    # Flow splits 3:1 (X 0.1 vs 0.3): R = 0.02·0.75² + 0.06·0.25² = 0.015,
    # X = 1 / (1/0.1 + 1/0.3) = 0.075
    assert _node_link_impedances([_line(0, 1, 0.02, 0.1), _line(1, 0, 0.06, 0.3)], [], dc, 100.0) == {
        (0, 1): pytest.approx((0.015, 0.075))}
    # A line's R and X are those of all its circuits: a 2-circuit line of
    # (0.02, 0.1) is not halved again
    assert _node_link_impedances(
        [_line(0, 1, 0.02, 0.1, circuits=2), _line(0, 1, 0.06, 0.3)], [], dc, 100.0) == {
        (0, 1): pytest.approx((0.015, 0.075))}
    # Lines inside a node do not join nodes
    assert _node_link_impedances([_line(1, 1, 0.02, 0.1)], [], dc, 100.0) == {}


def _local_gas_built(tmp_path, loss_model):
    """MW of gas built at the load node instead of importing cheaper diesel."""
    from esfex.runner import Orchestrator

    demand = tmp_path / f"demand_{loss_model}.csv"
    load = np.zeros((8760, 2))
    load[:, 1] = 60.0
    np.savetxt(demand, load, delimiter=",")
    # Cheap diesel at node 0; a dear unit at node 1 covers adequacy only.
    diesel = _make_generator("Diesel", "Non-renewable", "Diesel", [150.0, 100.0],
                             degradation_rate=[0.0, 0.0], fuel_cost=[80.0, 400.0],
                             invest_max_power=[0.0, 0.0])
    gas = _make_technology("gas_new", "Gas", 2, tech_type="Non-renewable",
                           invest_cost=20000.0, invest_max_power=500.0)
    gas = gas.model_copy(update={"fuel_cost": [100.0, 100.0], "invest_max_power": [0.0, 500.0]})
    sysc = _make_system_config(num_nodes=2, generators={"diesel": diesel},
                               technologies={"gas_new": gas})
    sysc = sysc.model_copy(update={
        "demand_path": str(demand),
        "nodes": sysc.nodes.model_copy(update={"nodes_connections": [0.0, 500.0, 500.0, 0.0]}),
        "transmission_lines_geo": [TransmissionLineGeo(
            line_id="L01", from_node=0, to_node=1, from_bus=0, to_bus=1,
            capacity_mw=500.0, reactance_pu=0.1, resistance_pu=0.4)],
        "dc_power_flow": DCPowerFlowConfig(loss_model=loss_model),
        "penalties": PenaltiesConfig(loss_of_load=10000.0)})
    cfg = _make_esfex_config(
        sysc, simulation_mode="development",
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720, overlap_hours=0),
        master_problem=MasterProblemConfig())
    results = Orchestrator(cfg, output_dir=tmp_path / loss_model).run(years=1)
    return results[0].investments.get("tech_investment_power_0_1", 0.0)


@pytest.mark.julia
def test_line_losses_drive_the_plan(tmp_path):
    # Lossless, importing diesel beats building gas; losing 40 % of the
    # flow on the line makes local gas the cheaper supply.
    assert _local_gas_built(tmp_path, "none") == pytest.approx(0.0, abs=1e-3)
    assert _local_gas_built(tmp_path, "linear") > 1.0
    assert _local_gas_built(tmp_path, "pwl") > 1.0
