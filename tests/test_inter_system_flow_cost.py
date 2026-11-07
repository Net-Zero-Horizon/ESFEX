"""An inter-system link's cost_per_mw_km is charged in the dispatch.

In a merged multi-system run the link became a plain transmission line and its
operating cost (distance x cost_per_mw_km per MWh carried, which the
multi-system master charges) was lost: the dispatch moved energy over the link
for free. The merge now sets the line's ``cost_per_mw_km`` (the field the
master reads) and the dispatch charges length x cost on |flow|
(``line_flow_cost`` in the cost breakdown).
"""

import sys

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_merge_node_connections import _two_systems  # noqa: E402
from test_network_reduction import _make_buses, _make_line, _minimal_system  # noqa: E402

from esfex.config.schema import TemporalConfig  # noqa: E402
from esfex.runner import Orchestrator  # noqa: E402
from esfex.topology import reduce_network  # noqa: E402


def _with_link_cost(cfg, cost):
    link = cfg.meta_network.systems_links[0].model_copy(update={"cost_per_mw_km": [cost]})
    return cfg.model_copy(update={"meta_network": cfg.meta_network.model_copy(
        update={"systems_links": [link]})})


def test_merge_gives_link_lines_their_flow_cost():
    from esfex.bridge.converters import line_flow_cost_per_mwh

    _, op, _ = Orchestrator._merge_systems(_with_link_cost(_two_systems(), 0.02))
    flow_cost = {ln.line_id: line_flow_cost_per_mwh(ln) for ln in op.transmission_lines_geo}
    assert flow_cost["link_A_B_0"] == pytest.approx(50.0 * 0.02)   # 50 km
    # A system's own lines stay free
    assert all(c == 0.0 for lid, c in flow_cost.items() if lid != "link_A_B_0")


def test_reduction_keeps_flow_cost_of_merged_lines():
    # Series: both originals carry the merged flow
    buses = _make_buses(["load", "connection", "load"], [0.5, 0.0, 0.5])
    from esfex.bridge.converters import line_flow_cost_per_mwh

    # 10 km each: 2 and 3 $/MWh
    lines = [_make_line("L_AB", 0, 1, x=0.1), _make_line("L_BC", 1, 2, x=0.15)]
    lines[0] = lines[0].model_copy(update={"cost_per_mw_km": 0.2})
    lines[1] = lines[1].model_copy(update={"cost_per_mw_km": 0.3})
    reduced, _ = reduce_network(_minimal_system(buses, lines))
    assert [line_flow_cost_per_mwh(ln) for ln in reduced.transmission_lines_geo] == \
        pytest.approx([5.0])
    # Parallel: shares 2/3 and 1/3 of the merged flow
    buses = _make_buses(["load", "load"], [0.5, 0.5])
    lines = [_make_line("L1", 0, 1, x=0.1).model_copy(update={"cost_per_mw_km": 0.3}),
             _make_line("L2", 0, 1, x=0.2)]
    reduced, _ = reduce_network(_minimal_system(buses, lines))
    assert [line_flow_cost_per_mwh(ln) for ln in reduced.transmission_lines_geo] == \
        pytest.approx([2.0])


@pytest.mark.julia
def test_dispatch_charges_link_flow(tmp_path):
    cfg = _with_link_cost(_two_systems(
        tmp_path, simulation_mode="economic_dispatch",
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720,
                                overlap_hours=0)), 0.02)
    res = Orchestrator(cfg, output_dir=tmp_path / "out").run(years=1)[0]
    assert float(np.nansum(res.load_shed_array)) == pytest.approx(0.0, abs=1e-3)
    # A's 50 MW (plus A's internal line losses, ~0.2 %) all cross the 50 km
    # link from B, every hour of the year, at 50 km x 0.02 $/MW/km
    assert res.cost_breakdown["line_flow_cost"] == pytest.approx(
        50.0 * 8760 * 50.0 * 0.02, rel=1e-2)
