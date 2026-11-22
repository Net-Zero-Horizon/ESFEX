"""Variable renewables are not committed; dispatchable renewables are (bug sweep M16).

Unit commitment created ``gen_status`` / ``gen_startup`` for every generator
but only bound the committed ones, so a PV or wind unit had free, costless
start-ups (Cuba UC 720 h: 25,200 for the PV fleet) and the exemption, keyed
on ``type == "Renewable"``, also freed a biomass unit (status 0 while it
generated). The rule is now ``is_variable_renewable``: type Renewable with
sun / wind / ocean / no fuel, or water without a reservoir. The model side
is tested in ``src/esfex/julia/test/runtests.jl`` ("variable renewables are
not committed; biomass is").
"""

import sys

import h5py
import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator  # noqa: E402
from test_zones import _make_system_config  # noqa: E402

from esfex.config.schema import is_committed_unit, is_variable_renewable  # noqa: E402
from esfex.runner import Orchestrator, YearResults  # noqa: E402

SC = "detailed_results/year_2025_threshold_0"

# (type, fuel, reservoir at the node) -> variable renewable
CASES = [
    ("Renewable", "Sun", 0.0, True),
    ("Renewable", "Solar", 0.0, True),
    ("Renewable", "PV", 0.0, True),
    ("Renewable", "Wind", 0.0, True),
    ("Renewable", "Wind offshore", 0.0, True),
    ("Renewable", "OTEC", 0.0, True),
    ("Renewable", "None", 0.0, True),
    ("Renewable", "", 0.0, True),
    ("Renewable", "sun", 0.0, True),
    ("Renewable", "Water", 0.0, True),
    ("Renewable", "Hydro", 0.0, True),
    ("Renewable", "Water", 100.0, False),
    ("Renewable", "Biomass", 0.0, False),
    ("Renewable", "Biogas", 0.0, False),
    ("Renewable", "Geothermal", 0.0, False),
    ("Non-renewable", "Gas", 0.0, False),
    ("Non-renewable", "Sun", 0.0, False),
]


@pytest.mark.parametrize("unit_type,fuel,res,variable", CASES)
def test_variable_renewable_rule(unit_type, fuel, res, variable):
    assert is_variable_renewable(unit_type, fuel, res) is variable


def test_committed_anywhere():
    assert not is_committed_unit("Renewable", "Sun")
    assert is_committed_unit("Renewable", "Biomass", [0.0, 0.0])
    # Hydro with a reservoir at one of its nodes is committed there
    assert is_committed_unit("Renewable", "Water", [0.0, 50.0])
    assert not is_committed_unit("Renewable", "Water", [0.0, 0.0])


@pytest.mark.julia
def test_same_rule_as_the_model():
    from esfex.bridge.julia_setup import get_esfex_module, get_julia
    jl = get_julia()
    ESFEX = get_esfex_module()
    for unit_type, fuel, res, variable in CASES:
        unit = jl.seval("(t, f, r) -> (type = t, fuel = f, reservoir_capacity = [r])")(
            unit_type, fuel, float(res))
        assert bool(ESFEX.is_variable_renewable(unit, 1)) is variable, (unit_type, fuel, res)


def test_export_has_no_commitment_for_variable_renewables(tmp_path):
    """PV gets no gen_status / gen_startup dataset (the readers then treat
    it as on whenever it generates); biomass and gas keep theirs."""
    from esfex.visualization.panels.dashboard_loader import DashboardLoader
    from esfex.visualization.panels.results_charts import UCCommitmentHeatmapChart

    gens = {
        "pv": _make_generator("PV", "Renewable", "Sun", [50.0], start_up_cost=[0.0]),
        "bio": _make_generator("Bio", "Renewable", "Biomass", [40.0],
                               start_up_cost=[1000.0]),
        "gas": _make_generator("Gas", "Non-renewable", "Gas", [100.0],
                               start_up_cost=[500.0]),
    }
    sysc = _make_system_config(num_nodes=1, coords=[(21.0, -80.0)], generators=gens)
    orch = Orchestrator(_make_esfex_config(sysc), output_dir=tmp_path)
    orch._num_nodes = 1
    orch._rebuild_unit_names(orch._gather_units(), year_offset=0)
    assert orch._gen_committed == [False, True, True]
    path = tmp_path / "out.h5"
    orch._bus_to_node = None
    orch._initialize_hdf5(path, num_nodes=1, num_years=1, start_year=2025, end_year=2025)
    status = np.array([[[0.0, 0.0, 0.0, 0.0]],
                       [[0.0, 1.0, 1.0, 1.0]],
                       [[1.0, 1.0, 0.0, 1.0]]])
    startup = np.clip(np.diff(status, axis=-1, prepend=0.0), 0, None)
    output = np.array([[[0.0, 20.0, 30.0, 0.0]],
                       [[0.0, 20.0, 20.0, 30.0]],
                       [[30.0, 10.0, 0.0, 10.0]]])
    orch._append_year_to_hdf5(path, YearResults(
        year=2025, objective=0.0, solve_time=0.0, feasible=True,
        gen_output=output, gen_status=status, gen_startup=startup))
    names = [orch._h5safe(n) for n in orch._gen_names]
    with h5py.File(path, "r") as f:
        for grp in ("gen_status", "gen_startup"):
            assert sorted(f[f"{SC}/{grp}"]) == sorted(names[1:])
        assert names[0] in f[f"{SC}/generation"]
        cost = DashboardLoader({}, {})._per_system_cost(f, "", 2025)
    # Bio starts once (1000 $), gas twice (500 $ each)
    assert cost["startup_cost"] == pytest.approx(1000.0 + 2 * 500.0)
    payload = UCCommitmentHeatmapChart._build_payload(None, path, [2025])
    assert len(payload["units"]) == 2
