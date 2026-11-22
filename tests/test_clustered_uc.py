"""Clustered unit commitment: ``num_units`` per node (bug sweep C15).

Each generator at a bus was ONE on/off unit in unit commitment: a derated
fleet (availability below min_power) could never be committed, a 2 × 250 MW
plant could not run one unit and an isolated 8 MW diesel plant could not
follow a 2 MW night load. Generators now have ``num_units`` per node (the
dispatch commits a whole number of them) and candidate technologies a
``unit_size`` (the units master-built capacity reaches the dispatch as).
These tests cover the Python side; the model itself is tested in
``src/esfex/julia/test/runtests.jl`` ("clustered unit commitment").
"""

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import h5py
import numpy as np
import pytest
import yaml
from pydantic import ValidationError

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator  # noqa: E402
from test_zones import _make_system_config, _make_technology  # noqa: E402

from esfex.runner import (  # noqa: E402
    Orchestrator, YearResults, units_in_service, units_of_capacity)

SC = "detailed_results/year_2025_threshold_0"


# ── config ────────────────────────────────────────────────────────────

def test_num_units_and_unit_size_in_the_schema():
    gen = _make_generator("G", "Non-renewable", "Gas", [100.0, 50.0])
    assert gen.num_units == [1]
    assert _make_generator("G", "Non-renewable", "Gas", [100.0],
                           num_units=[4]).num_units == [4]
    with pytest.raises(ValidationError, match="num_units"):
        _make_generator("G", "Non-renewable", "Gas", [100.0], num_units=[0])
    tech = _make_technology("CC", "Gas", 1, tech_type="Non-renewable")
    assert tech.unit_size == [0.0]
    with pytest.raises(ValidationError, match="unit_size"):
        tech.model_validate({**tech.model_dump(by_alias=True), "unit_size": [-1.0]})


def test_units_of_built_capacity():
    assert units_of_capacity(250.0, 100.0) == 3     # no unit above 100 MW
    assert units_of_capacity(200.0, 100.0) == 2
    assert units_of_capacity(50.0, 0.0) == 1        # no unit size: one unit
    assert units_of_capacity(0.0, 100.0) == 1


def test_units_left_of_a_plant_the_master_keeps():
    # Degradation shrinks every unit: no unit is lost
    deg = 400.0 * 0.98 ** 10
    assert units_in_service(4, 400.0, deg, 0.02, 10) == 4
    assert units_in_service(4, 400.0, 400.0) == 4
    # A partial life extension keeps whole units of the original size
    assert units_in_service(4, 400.0, 200.0) == 2
    assert units_in_service(4, 400.0, 150.0) == 2   # 1.5 units: two, one smaller
    assert units_in_service(4, 400.0, 0.0) == 1
    assert units_in_service(1, 400.0, 100.0) == 1


def test_master_capacities_reach_the_dispatch_as_whole_units(tmp_path):
    gen = _make_generator("CC", "Non-renewable", "Gas", [400.0], num_units=[4],
                          degradation_rate=[0.0], initial_age=[0])
    tech = _make_technology("Diesel", "Diesel", 1, tech_type="Non-renewable")
    tech = tech.model_copy(update={"unit_size": [20.0]})
    sysc = _make_system_config(num_nodes=1, generators={"cc": gen},
                               technologies={"dsl": tech})
    orch = Orchestrator(_make_esfex_config(sysc), output_dir=tmp_path)
    units = orch._build_config_from_cumulative(orch._gather_units(), {
        "gen": {1: np.array([200.0])},       # half of the plant kept
        "tech": {1: np.array([50.0])}}, year_offset=0)
    assert units["cc"]["num_units"] == [2]
    tech_key = next(iter(orch.primary_system.technologies))
    assert units[f"inv_{tech_key}"]["num_units"] == [3]   # 50 MW of 20 MW units


def test_systems_merge_pads_num_units_with_one():
    """Another system's nodes get one unit, not 0 (which is invalid)."""
    from test_merge_node_connections import _two_systems
    cfg = _two_systems()
    b = cfg.systems["B"]
    gas = b.generators["gas"].model_copy(update={"num_units": [3]})
    b = b.model_copy(update={"generators": {"gas": gas}})
    cfg = cfg.model_copy(update={"systems": {"A": cfg.systems["A"], "B": b}})
    _, op, _ = Orchestrator._merge_systems(cfg)
    assert op.generators["B__gas"].num_units == [1, 1, 3]


# ── reduced dispatch network ─────────────────────────────────────────

def test_units_of_an_eliminated_bus_move_with_its_capacity():
    from esfex.bridge.adapters import _per_bus_unit_on_model_buses
    buses = [SimpleNamespace(bus_id=i, parent_node=n, demand_fraction=d)
             for i, (n, d) in enumerate([(0, 0.0), (0, 1.0), (1, 0.0), (1, 1.0)])]
    rm = SimpleNamespace(n_original_buses=4, n_reduced_buses=2,
                         retained_original_indices=[1, 3])
    unit = {"name": "Investment Gas", "_is_per_bus": True,
            "rated_power": [40.0, 60.0, 0.0, 20.0], "num_units": [2, 3, 1, 1]}
    out = _per_bus_unit_on_model_buses(unit, rm, buses)
    assert out["rated_power"] == [100.0, 20.0]
    assert out["num_units"] == [5, 1]


# ── results ──────────────────────────────────────────────────────────

def _export(tmp_path):
    gen = _make_generator("CC", "Non-renewable", "Gas", [100.0], num_units=[2],
                          start_up_cost=[1000.0])
    sysc = _make_system_config(num_nodes=1, coords=[(21.0, -80.0)],
                               generators={"cc": gen})
    orch = Orchestrator(_make_esfex_config(sysc), output_dir=tmp_path)
    orch._num_nodes = 1
    orch._rebuild_unit_names(orch._gather_units(), year_offset=0)
    path = tmp_path / "out.h5"
    orch._bus_to_node = None
    orch._initialize_hdf5(path, num_nodes=1, num_years=1, start_year=2025, end_year=2025)
    status = np.array([[[0.0, 1.0, 2.0, 2.0, 1.0, 2.0]]])
    startup = np.clip(np.diff(status, axis=-1, prepend=0.0), 0, None)
    orch._append_year_to_hdf5(path, YearResults(
        year=2025, objective=0.0, solve_time=0.0, feasible=True,
        gen_output=status * 40.0, gen_status=status, gen_startup=startup))
    return path, orch


def test_export_records_units_online_and_started(tmp_path):
    path, orch = _export(tmp_path)
    with h5py.File(path, "r") as f:
        name = orch._h5safe(orch._gen_names[0])
        for grp in ("gen_status", "gen_startup"):
            ds = f[f"{SC}/{grp}/{name}"]
            np.testing.assert_array_equal(ds.attrs["num_units"], [2])
        assert f[f"{SC}/gen_startup/{name}"][:].sum() == 3.0


def test_start_up_cost_per_unit_in_the_readers(tmp_path):
    """Three units started at 1000 $ per start of the 2-unit plant."""
    from esfex.models import financial_analysis as fa
    from esfex.visualization.panels.dashboard_loader import DashboardLoader
    path, orch = _export(tmp_path)
    with h5py.File(path, "r") as f:
        cost = DashboardLoader({}, {})._per_system_cost(f, "", 2025)
    assert cost["startup_cost"] == pytest.approx(3 * 500.0)
    name = orch._h5safe(orch._gen_names[0])
    with h5py.File(path, "r") as f:
        year_data = {"generation": {name: f[f"{SC}/generation/{name}"][:]},
                     "gen_startup": {name: f[f"{SC}/gen_startup/{name}"][:]},
                     "gen_num_units": {name: f[f"{SC}/gen_startup/{name}"].attrs["num_units"]}}
    costs = fa._recalculate_costs(
        year_data, [{"name": name, "start_up_cost": [1000.0]}], [], 1)
    assert costs["startup_cost"] == pytest.approx(3 * 500.0)


def test_commitment_heatmap_shows_units_online(tmp_path):
    from esfex.visualization.panels.results_charts import (
        UCCommitmentHeatmapChart, num_units_at)
    path, _ = _export(tmp_path)
    with h5py.File(path, "r") as f:
        ds = f[f"{SC}/gen_status"][list(f[f"{SC}/gen_status"])[0]]
        assert num_units_at(ds, 0) == 2
        assert num_units_at(ds, 5) == 1
    payload = UCCommitmentHeatmapChart._build_payload(None, path, [2025])
    assert payload["status"] == [[0, 1, 2, 2, 1, 2]]
    assert payload["num_units"] == [2]


# ── Studio ───────────────────────────────────────────────────────────

def test_num_units_round_trips_through_the_studio():
    from test_serializer import _make_esfex_config as _cfg
    from test_serializer import _make_generator as _sgen
    from esfex.visualization.data.serializer import (
        config_to_gui_states, gui_state_to_yaml)
    gen = _sgen("CC", "Non-renewable", "Gas", rated_power=[500.0, 0.0])
    gen.num_units = [2, 1]
    config = _cfg()
    config.systems["TestSystem"].generators["cc"] = gen
    states = config_to_gui_states(config)
    inst = next(i for i in states["TestSystem"].generators.values() if i.unit_key == "cc")
    assert inst.num_units == 2
    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as fh:
        out = Path(fh.name)
    try:
        gui_state_to_yaml(states=states, base_config=config, output_path=out)
        data = yaml.safe_load(out.read_text())
    finally:
        out.unlink(missing_ok=True)
    # The node without the plant gets one unit (0 would not load)
    assert data["systems"]["TestSystem"]["generators"]["cc"]["num_units"] == [2, 1]


def test_grid_builder_merge_adds_units():
    from esfex.visualization.data.validation import _merged_generator_params
    a = SimpleNamespace(rated_power=250.0, num_units=1, start_up_cost=1000.0,
                        ramp_up=0.0, ramp_down=0.0, min_up=4, min_down=2,
                        life_time=30, initial_age=10, min_power=0.4, eff_at_rated=0.4,
                        eff_at_min=0.3, fuel_cost=50.0, fixed_cost=1.0,
                        maintenance_cost=2.0, degradation_rate=0.0, inertia=4.0,
                        governor_time_const=5.0, decommissioning_cost=0.0,
                        risk_coefficient=1.0, droop=0.05)
    b = SimpleNamespace(**{**vars(a), "num_units": 2, "rated_power": 500.0,
                           "start_up_cost": 2000.0})
    params = _merged_generator_params([a, b])
    assert params["num_units"] == 3
    # One unit of the merged plant still costs one original unit's start
    assert params["start_up_cost"] / params["num_units"] == pytest.approx(1000.0)


@pytest.mark.julia
def test_converter_passes_num_units_to_julia():
    from esfex.bridge.converters import convert_generator_config
    gen = _make_generator("CC", "Non-renewable", "Gas", [500.0, 80.0], num_units=[2, 4])
    jl_gen = convert_generator_config(gen)
    assert list(jl_gen.num_units) == [2, 4]
    # A single value holds at every node, also on buses
    gen = _make_generator("CC", "Non-renewable", "Gas", [500.0, 80.0], num_units=[3])
    jl_gen = convert_generator_config(gen, bus_to_node=[0, 0, 1],
                                      bus_per_node={0: 1, 1: 2})
    assert list(jl_gen.num_units) == [3, 3, 3]
