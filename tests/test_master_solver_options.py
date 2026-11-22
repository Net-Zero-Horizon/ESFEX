"""``master_problem.solver_options`` override ``solver.options`` in the masters.

The masters used to share ``solver.options`` with the dispatch, so a setting
meant for the large master LP (barrier without crossover) also applied to
every small dispatch window.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_runner import _make_generator, _make_node_config  # noqa: E402

from esfex.bridge.adapters import master_solver_options  # noqa: E402
from esfex.config.schema import (  # noqa: E402
    ESFEXConfig, MasterProblemConfig, MetaNetworkConfig, SolverConfig, SystemConfig,
)


def _config(solver_options, master_options, solver="gurobi") -> ESFEXConfig:
    system = SystemConfig(
        name="S", nodes=_make_node_config(1),
        generators={"gas": _make_generator("Gas", "Non-renewable", "Gas", [40.0],
                                           fuel_cost=[50.0])})
    return ESFEXConfig(
        meta_network=MetaNetworkConfig(systems=["S"]), systems={"S": system},
        solver=SolverConfig(name=solver, options=solver_options),
        master_problem=MasterProblemConfig(solver_options=master_options))


def test_master_options_are_merged_over_the_solver_options():
    cfg = _config({"Method": 1, "Presolve": 2}, {"Crossover": 0, "Presolve": 0})
    assert master_solver_options(cfg) == {"Method": 1, "Presolve": 0, "Crossover": 0}
    # The dispatch keeps its own
    assert cfg.solver.options == {"Method": 1, "Presolve": 2}


def test_a_master_key_replaces_the_option_however_it_is_written():
    # The Studio saves Gurobi's method as ``Method: 2``; a YAML may say ``method``
    cfg = _config({"Method": 1}, {"method": "barrier"})
    assert master_solver_options(cfg) == {"method": "barrier"}


def test_no_master_options_keep_the_solver_options():
    assert master_solver_options(_config({"presolve": "on"}, {}, "highs")) == {"presolve": "on"}
    assert master_solver_options(None) == {}


def test_master_solver_options_are_a_known_key():
    from esfex.config.unknown_keys import find_unknown_keys
    raw = {"master_problem": {"solver_options": {"Method": 2, "AnyNative": 1}}}
    assert find_unknown_keys(raw) == []


def test_studio_round_trips_the_master_solver_options():
    from esfex.visualization.data.serializer import (
        config_to_global_settings, global_settings_to_config_dict,
    )
    cfg = _config({}, {"Method": 2, "Crossover": 0}, "highs")
    g = config_to_global_settings(cfg)
    assert g.mp_solver_options == {"Method": 2, "Crossover": 0}
    d: dict = {}
    global_settings_to_config_dict(g, d)
    assert d["master_problem"]["solver_options"] == {"Method": 2, "Crossover": 0}
    assert "options" not in d["solver"]


@pytest.mark.julia
def test_master_input_carries_the_merged_options():
    from esfex.bridge.adapters import MasterProblemAdapter

    cfg = _config({"presolve": "on", "random_seed": 1}, {"random_seed": 7}, "highs")
    master = MasterProblemAdapter(config=cfg, years=[2025], base_year=2025,
                                  demand=np.full((24, 1), 30.0))
    opts = dict(master._create_input().solver_options)
    assert opts == {"presolve": "on", "random_seed": 7}
