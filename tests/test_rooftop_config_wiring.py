"""rooftop_solar_config reaches rooftex.

* The multi-system merge re-keyed ``max_adoption`` / ``adoption_rates`` by
  node index, but they are keyed by adoption scenario ("low", "medium",
  "high"): ``int("medium")`` failed and the merge dropped every value.
* The runner passed only the scenario, the weather variability and the seed
  to rooftex: performance ratio, systems per node, system size and the
  adoption parameters were validated and ignored.
"""

import sys
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_system_config  # noqa: E402

from esfex.config.schema import MetaNetworkConfig, RooftopSolarConfig  # noqa: E402
from esfex.runner import Orchestrator  # noqa: E402

CAPS = {"low": 0.2, "medium": 0.4, "high": 0.6}
RATES = {"low": 0.03, "medium": 0.06, "high": 0.09}


def _two_systems(b_caps=None):
    a = _make_system_config(num_nodes=2, name="A").model_copy(update={
        "rooftop_solar_config": RooftopSolarConfig(
            max_adoption=CAPS, adoption_rates=RATES,
            systems_per_node=[100, 200], avg_system_size=[5.0, 4.0])})
    b = _make_system_config(num_nodes=2, name="B").model_copy(update={
        "rooftop_solar_config": RooftopSolarConfig(max_adoption=b_caps) if b_caps else None})
    return _make_esfex_config(a, systems={"A": a, "B": b},
                              meta_network=MetaNetworkConfig(systems=["A", "B"]))


def test_merge_keeps_the_scenario_keyed_adoption_parameters(caplog):
    master, _, _ = Orchestrator._merge_systems(_two_systems())
    rsc = master.rooftop_solar_config
    assert rsc.max_adoption == CAPS
    assert rsc.adoption_rates == RATES
    # Per-node lists: system B has none, its nodes get zeros
    assert rsc.systems_per_node == [100, 200, 0, 0]
    assert rsc.avg_system_size == [5.0, 4.0, 0.0, 0.0]
    # A list no system sets stays unset (not a list of zeros)
    assert rsc.initial_adoption is None
    # Another system's different caps cannot apply too: warned and ignored
    with caplog.at_level("WARNING", logger="esfex.runner"):
        master, _, _ = Orchestrator._merge_systems(_two_systems(b_caps={"medium": 0.9}))
    assert master.rooftop_solar_config.max_adoption == CAPS
    assert "max_adoption differs between systems" in caplog.text


def _rooftex_config(rsc):
    """The RooftopConfig the runner builds for *rsc* (rooftex mocked)."""
    seen = {}

    def fake_generate(cfg):
        seen["cfg"] = cfg
        n = cfg.num_nodes
        return SimpleNamespace(availability=np.full((cfg.hours, n), 0.5),
                               adoption_factors=np.full(n, 0.5),
                               max_potential_mw=[10.0] * n)

    orch = SimpleNamespace(primary_system=SimpleNamespace(
        rooftop_solar_config=rsc, rooftop_max_potential=[]))
    with patch("rooftex.generate_profiles", side_effect=fake_generate):
        Orchestrator._generate_rooftop_solar(
            orch, num_nodes=2, total_hours=8760, years_range=[2030], start_year=2030)
    return seen["cfg"]


def test_runner_passes_the_configuration_to_rooftex():
    cfg = _rooftex_config(RooftopSolarConfig(
        performance_ratio=0.8, systems_per_node=[100, 200], avg_system_size=[5.0, 4.0],
        initial_adoption=[0.1, 0.2], max_adoption=CAPS, adoption_rates=RATES))
    assert cfg.performance_ratio == 0.8
    assert cfg.systems_per_node == [100, 200]
    assert cfg.avg_system_size_kw == [5.0, 4.0]
    assert cfg.initial_adoption == [0.1, 0.2]
    assert cfg.max_adoption == CAPS
    assert cfg.adoption_rates == RATES


def test_empty_fields_leave_the_rooftex_defaults():
    """The Studio writes empty lists and dicts for fields left blank."""
    cfg = _rooftex_config(RooftopSolarConfig(
        systems_per_node=[], avg_system_size=[], initial_adoption=[],
        max_adoption={}, adoption_rates={}))
    assert cfg.systems_per_node is None and cfg.avg_system_size_kw is None
    assert cfg.initial_adoption is None
    assert cfg.max_adoption is None and cfg.adoption_rates is None
