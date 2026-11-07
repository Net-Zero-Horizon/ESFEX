"""The primary-energy cost totals of the results file are in $ (since they
are unscaled on extraction); the Studio's Fuel Costs charts read them as $
and plot k$."""

import h5py
import pytest

COSTS = {"total_fuel_cost": 2.5e6, "total_transport_cost": 4.0e5, "total_loss_penalty": 1.0e4}


@pytest.fixture
def results_file(tmp_path):
    path = tmp_path / "res.h5"
    with h5py.File(path, "w") as f:
        sc = f.create_group("detailed_results/year_2025_threshold_0")
        sc.attrs["year"] = 2025
        pe = sc.create_group("primary_energy")
        for k, v in COSTS.items():
            pe.attrs[k] = v
    return path


def test_plotly_fuel_cost_chart_in_kdollars(results_file):
    from esfex.visualization.panels.results_charts_plotly import FuelCostChart

    fig = FuelCostChart.build_figure(None, results_file, [2025])
    bars = {t.name: t.y[0] for t in fig.data if t.type == "bar"}
    assert bars == pytest.approx({"Fuel Cost": 2500.0, "Transport Cost": 400.0,
                                  "Loss Penalty": 10.0})


def test_bokeh_fuel_cost_chart_in_kdollars(results_file):
    from esfex.visualization.panels.results_charts_bokeh import FuelCostChart

    layout = FuelCostChart.build_figure(None, results_file, [2025])
    total = layout.children[1].renderers[0].data_source.data["y"]
    assert list(total) == pytest.approx([2910.0])
