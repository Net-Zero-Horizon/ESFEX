"""The generation-mix chart pairs each per-unit investment with its own config.

``_add_legacy_unit_investments`` reads both the per-unit
``gen_investment_power/<unit>`` datasets (h5py lists them alphabetically, not
in config order) and the old index-aligned ``[unit]`` array, and adds the
investments' cost.
"""

import numpy as np
import pytest

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
