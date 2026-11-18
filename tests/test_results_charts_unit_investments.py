"""The Plotly and Bokeh generation-mix charts pair each per-unit investment
with its own config, as the matplotlib chart does.

They read only the old index-aligned ``[unit]`` array and paired it with the
configs by position; the per-unit ``gen_investment_power/<unit>`` datasets
(h5py lists them alphabetically, not in config order) were left out, so
those investments and their cost were missing. Bokeh also never added the
batteries' cost. All three renderers now share
``_add_legacy_unit_investments``.
"""

import h5py
import numpy as np
import pytest

SC = "detailed_results/year_2025_threshold_0"


def _file(path):
    """Configs in an order that does not match the alphabetical datasets."""
    with h5py.File(path, "w") as f:
        f.attrs["temporal_resolution_hours"] = 1
        f.create_dataset("summary_results/year", data=[2025])
        sc = f.create_group("system_configuration")
        gens = sc.create_group("generators")
        gens.attrs["num_generators"] = 2
        gens.create_group("generator_0").attrs.update(
            name="z_coal", fuel="Coal", type="Non-renewable", rated_power=100.0,
            invest_cost=1000.0)
        gens.create_group("generator_1").attrs.update(
            name="a_gas", fuel="Gas", type="Non-renewable", rated_power=50.0,
            invest_cost=2.0e6)
        bats = sc.create_group("batteries")
        bats.attrs["num_batteries"] = 2
        bats.create_group("battery_0").attrs.update(
            name="z_bess", capacity=10.0, MaxDischargePower=5.0, invest_cost=1.0)
        bats.create_group("battery_1").attrs.update(
            name="a_bess", capacity=40.0, MaxDischargePower=20.0, invest_cost=4.0e6)
        y = f.create_group(SC)
        y.attrs["year"] = 2025
        y.create_dataset("generation/z_coal", data=np.full((1, 24), 30.0))
        y.create_dataset("generation/a_gas", data=np.full((1, 24), 10.0))
        y.create_dataset("demand", data=np.full((1, 24), 40.0))
        y.create_dataset("gen_investment_power/a_gas", data=np.array([30.0]))
        y.create_dataset("bat_investment_power/a_bess", data=np.array([5.0]))
    return path


def test_helper_pairs_each_unit_with_its_config():
    from esfex.visualization.panels.results_charts import _add_legacy_unit_investments

    configs = [{"name": "z_coal", "invest_cost": 1.0}, {"name": "a_gas", "invest_cost": 2.0e6}]
    bats = [{"name": "z_bess", "invest_cost": 1.0}, {"name": "a_bess", "invest_cost": 4.0e6}]
    gen_inv, bat_inv = {}, {}
    inv = {"gen_investment_power_by_name": {"a_gas": 30.0},
           "bat_investment_power_by_name": {"a_bess": 5.0}}
    cost = _add_legacy_unit_investments(inv, configs, bats, gen_inv, bat_inv, 0, 1,
                                        lambda n: n)
    assert gen_inv == {"a_gas": pytest.approx([0.03])}
    assert bat_inv == {"a_bess": pytest.approx([0.005])}
    assert cost == pytest.approx(30.0 * 2.0 + 5.0 * 4.0)   # M$
    # An old [unit] array is index-aligned with the configured units
    gen_inv = {}
    _add_legacy_unit_investments({"gen_investment_power": np.array([0.0, 30.0])},
                                 configs, bats, gen_inv, {}, 0, 1, lambda n: n)
    assert gen_inv == {"a_gas": pytest.approx([0.03])}


def test_plotly_gen_mix_shows_the_per_unit_investments(tmp_path):
    from esfex.visualization.panels.results_charts_plotly import GenerationMixChart

    fig = GenerationMixChart().build_figure(_file(tmp_path / "r.h5"), [2025])
    bars = {t.name: list(t.y) for t in fig.data if t.type == "bar"}
    inv = {k: v for k, v in bars.items() if k.endswith("(inv)")}
    assert sum(v[0] for v in inv.values()) == pytest.approx(0.035)
    assert any("(bat)" in k and v[0] == pytest.approx(0.005) for k, v in inv.items())


def test_bokeh_gen_mix_shows_the_per_unit_investments_and_their_cost(tmp_path):
    pytest.importorskip("bokeh")
    from bokeh.models import GlyphRenderer, VBar

    from esfex.visualization.panels.results_charts_bokeh import GenerationMixChart

    layout = GenerationMixChart().build_figure(_file(tmp_path / "r.h5"), [2025])
    tops = [r.data_source.data["top"][0] - r.data_source.data["bottom"][0]
            for r in layout.select({"type": GlyphRenderer}) if isinstance(r.glyph, VBar)]
    assert sum(t for t in tops if t > 0) == pytest.approx(0.035)
    cost = [r.data_source.data["y"][0] for r in layout.select({"type": GlyphRenderer})
            if getattr(r, "y_range_name", "default") == "cost"]
    assert cost and all(c == pytest.approx(30.0 * 2.0 + 5.0 * 4.0) for c in cost)
