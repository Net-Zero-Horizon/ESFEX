"""Line losses in the results (the HDF5 had none).

The dispatch's DC loss model loses energy on the lines, but the losses only
appeared in the runner's DEBUG window balance. They are now in the year
results and the HDF5: ``line_losses`` [node x step] (half of each line's loss
at each end node, as the bus balances take it) and the yearly
``transmission_losses`` (MWh) in the summaries and scenario attributes.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator, _make_system_config  # noqa: E402

from esfex.config.schema import (  # noqa: E402
    BusConfig,
    DCPowerFlowConfig,
    PenaltiesConfig,
    TemporalConfig,
    TransmissionLineGeo,
)
from esfex.runner import Orchestrator  # noqa: E402


@pytest.mark.julia
def test_line_losses_reach_the_results_and_hdf5(tmp_path):
    import h5py

    demand = tmp_path / "d.csv"
    load = np.zeros((8760, 2))
    load[:, 1] = 80.0
    np.savetxt(demand, load, delimiter=",")
    gen = _make_generator("Gas", "Non-renewable", "Gas", [300.0, 0.0], min_power=[0.0, 0.0],
                          degradation_rate=[0.0, 0.0])
    sysc = _make_system_config(num_nodes=2, generators={"gas": gen})
    sysc = sysc.model_copy(update={
        "demand_path": str(demand),
        "buses": [BusConfig(bus_id="b0", parent_node=0), BusConfig(bus_id="b1", parent_node=1)],
        "transmission_lines_geo": [TransmissionLineGeo(
            line_id="L", from_node=0, to_node=1, from_bus=0, to_bus=1, capacity_mw=200.0,
            reactance_pu=0.2, resistance_pu=0.1)],
        "dc_power_flow": DCPowerFlowConfig(loss_model="pwl", pwl_loss_segments=8),
        "penalties": PenaltiesConfig(loss_of_load=10000.0)})
    cfg = _make_esfex_config(
        sysc, simulation_mode="economic_dispatch",
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720, overlap_hours=0))
    out = tmp_path / "out"
    res = Orchestrator(cfg, output_dir=out).run(years=1)[0]
    tres = 24.0

    # [node x step]: the line's loss split half and half over its two ends
    ll = np.asarray(res.line_losses)
    gen = np.nansum(np.asarray(res.gen_output, dtype=float), axis=(0, 1))   # MW per step
    assert ll.shape == (2, gen.size)
    np.testing.assert_allclose(ll[0], ll[1], rtol=1e-9)
    # ~7 MW lost to serve 80 MW (r · F² / S_base): generation minus load
    np.testing.assert_allclose(ll.sum(axis=0), gen - 80.0, rtol=1e-4, atol=1e-6)
    assert ll.sum(axis=0).mean() == pytest.approx(7.0, rel=0.1)
    assert res.transmission_losses == pytest.approx(ll.sum() * tres, rel=1e-6)

    (h5_path,) = Path(out).glob("*.h5")
    with h5py.File(h5_path, "r") as f:
        sc = f[f"detailed_results/year_{res.year}_threshold_0"]
        np.testing.assert_allclose(sc["line_losses"][:], ll)
        assert sc["line_losses"].attrs["units"] == "MW"
        assert sc.attrs["transmission_losses"] == pytest.approx(res.transmission_losses)
        assert f["summary_results/transmission_losses"][0] == pytest.approx(
            res.transmission_losses)
        assert f["global/summary_results/transmission_losses"][0] == pytest.approx(
            res.transmission_losses)

    # The energy-flow Sankey shows these losses (it estimated them as 2 % of
    # the flows: ~1.5 GWh here instead of ~61 GWh)
    from esfex.visualization.panels.results_charts import SankeyEnergyFlowChart
    payload = SankeyEnergyFlowChart._build_payload(None, h5_path, [res.year])
    (sankey,) = payload["data"]
    labels = [str(lb).split("<br>")[0] for lb in sankey["node"]["label"]]
    tx = labels.index("Transmission Losses")
    lost_gwh = sum(v for t, v in zip(sankey["link"]["target"], sankey["link"]["value"])
                   if t == tx)
    assert lost_gwh == pytest.approx(res.transmission_losses / 1000.0, rel=1e-3)


def _surplus_pv_config(tmp_path, mode):
    """300 MW of PV at node 0 behind a lossy 200 MW line, 80 MW of load and
    gas at node 1, curtailment priced: the PWL solve loses the PV surplus
    on the line, and the repair holds it to the PWL loss of its flow."""
    demand = tmp_path / "d.csv"
    load = np.zeros((8760, 2))
    load[:, 1] = 80.0
    np.savetxt(demand, load, delimiter=",")
    pv = _make_generator("PV", "Renewable", "Solar", [300.0, 0.0], min_power=[0.0, 0.0],
                         degradation_rate=[0.0, 0.0], fuel_cost=[0.0, 0.0],
                         maintenance_cost=[0.0, 0.0])
    gas = _make_generator("Gas", "Non-renewable", "Gas", [0.0, 300.0], min_power=[0.0, 0.0],
                          degradation_rate=[0.0, 0.0])
    sysc = _make_system_config(num_nodes=2, generators={"pv": pv, "gas": gas})
    sysc = sysc.model_copy(update={
        "demand_path": str(demand),
        "buses": [BusConfig(bus_id="b0", parent_node=0), BusConfig(bus_id="b1", parent_node=1)],
        "transmission_lines_geo": [TransmissionLineGeo(
            line_id="L", from_node=0, to_node=1, from_bus=0, to_bus=1, capacity_mw=200.0,
            reactance_pu=0.2, resistance_pu=0.1)],
        "dc_power_flow": DCPowerFlowConfig(loss_model="pwl", pwl_loss_segments=3),
        "penalties": PenaltiesConfig(loss_of_load=10000.0, curtailment_cost=20.0)})
    return _make_esfex_config(
        sysc, simulation_mode=mode,
        temporal=TemporalConfig(resolution_hours=24, rolling_horizon_hours=720, overlap_hours=0))


def _check_bounds(rep):
    assert rep["lower_bound"] < rep["objective"]
    assert rep["gap"] == pytest.approx(
        (rep["objective"] - rep["lower_bound"]) / rep["objective"])
    assert rep["rounds"] >= 1


@pytest.mark.julia
def test_loss_repair_bounds_reach_the_log_and_hdf5(tmp_path, caplog):
    """The bounds of the dispatch's line-loss repair (the PWL solve below,
    the repaired one above, L44), summed over the year's windows, reach the
    year's log line and the scenario attributes of the HDF5."""
    import logging

    import h5py

    out = tmp_path / "out"
    with caplog.at_level(logging.INFO, logger="esfex.runner"):
        res = Orchestrator(_surplus_pv_config(tmp_path, "economic_dispatch"),
                           output_dir=out).run(years=1)[0]

    rep = res.dispatch_loss_repair
    assert rep is not None and rep["windows_repaired"] >= 1
    _check_bounds(rep)
    assert 0 < rep["gap"] <= rep["gap_max"] + 1e-12
    assert rep["windows"] >= rep["windows_repaired"]
    # No master in this mode
    assert res.master_loss_repair is None
    assert any("dispatch line losses: PWL solves" in r.getMessage() for r in caplog.records)

    (h5_path,) = Path(out).glob("*.h5")
    with h5py.File(h5_path, "r") as f:
        sc = f[f"detailed_results/year_{res.year}_threshold_0"]
        for key, val in rep.items():
            assert sc.attrs[f"dispatch_loss_repair_{key}"] == pytest.approx(val)
        assert not any(k.startswith("master_loss_repair_") for k in sc.attrs)


@pytest.mark.julia
def test_master_loss_repair_bounds_reach_the_hdf5(tmp_path):
    """The master's repair bounds, of the solve that planned the year, are
    written beside the dispatch's (here its PWL solve loses nothing beyond
    the flows' losses: no round, the bounds meet)."""
    import h5py

    out = tmp_path / "out"
    res = Orchestrator(_surplus_pv_config(tmp_path, "development"),
                       output_dir=out).run(years=1)[0]
    rep = res.master_loss_repair
    assert rep is not None
    assert rep["lower_bound"] <= rep["objective"]
    assert rep["gap"] == pytest.approx(
        (rep["objective"] - rep["lower_bound"]) / rep["objective"], abs=1e-12)
    assert res.dispatch_loss_repair is not None
    (h5_path,) = Path(out).glob("*.h5")
    with h5py.File(h5_path, "r") as f:
        sc = f[f"detailed_results/year_{res.year}_threshold_0"]
        for key, val in rep.items():
            assert sc.attrs[f"master_loss_repair_{key}"] == pytest.approx(val)


def test_sum_loss_repairs():
    from esfex.runner import sum_loss_repairs

    assert sum_loss_repairs([]) is None
    w = [dict(lower_bound=90.0, objective=100.0, gap=0.1, rounds=2, linearised=4, released=1),
         dict(lower_bound=50.0, objective=50.0, gap=0.0, rounds=0, linearised=0, released=0)]
    s = sum_loss_repairs(w)
    assert s["lower_bound"] == 140.0 and s["objective"] == 150.0
    assert s["gap"] == pytest.approx(10.0 / 150.0)
    assert s["gap_max"] == 0.1
    assert (s["rounds"], s["linearised"], s["released"]) == (2, 4, 1)
    assert (s["windows"], s["windows_repaired"]) == (2, 1)
