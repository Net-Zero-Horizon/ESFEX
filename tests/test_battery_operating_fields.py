"""Battery operating fields: ramp limits, fixed cost, reservable, availability.

Batteries used to carry every generator field: the loader required them,
the Studio wrote them, and the model read none of ramp_up, ramp_down,
fixed_cost, reservable or the availability profile (every battery held
reserve). The fields that make sense for storage now reach the dispatch and
the master (and the batteries the master builds); the others are gone and
an old file reports them as unknown keys.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
import yaml

sys.path.insert(0, "tests")
from test_runner import _make_generator, _make_node_config  # noqa: E402
from test_zones import (  # noqa: E402
    _make_battery,
    _make_battery_technology,
    _make_system_config,
)

from esfex.config.loader import load_config  # noqa: E402
from esfex.config.schema import BatteryConfig, BatteryTechnologyConfig, SystemConfig  # noqa: E402
from esfex.config.unknown_keys import find_unknown_keys  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "studio_export_island.yaml"
REMOVED = ("min_power", "min_up", "min_down", "eff_at_rated", "eff_at_min",
           "start_up_cost", "fuel_cost")


def _old_project() -> dict:
    """The island fixture as an earlier Studio saved it: the battery also
    carries the generator fields."""
    raw = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    bat = raw["systems"]["island"]["batteries"]["bess"]
    for key in REMOVED:
        bat[key] = [0, 0] if key in ("min_up", "min_down") else [0.9, 0.0]
    return raw


def _write(tmp_path: Path, raw: dict) -> Path:
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return p


# ── Schema and loader ───────────────────────────────────────────────────


def test_battery_schema_has_no_generator_only_fields():
    for key in REMOVED:
        assert key not in BatteryConfig.model_fields
    for key in ("ramp_up", "ramp_down", "fixed_cost", "reservable", "availability_file"):
        assert key in BatteryConfig.model_fields
        assert key in BatteryTechnologyConfig.model_fields


def test_battery_loads_without_the_optional_operating_fields(tmp_path):
    """The loader required ramp_up, ramp_down, fixed_cost (and the generator
    fields): a battery without them aborted loading with a KeyError."""
    raw = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    bat = raw["systems"]["island"]["batteries"]["bess"]
    for key in ("ramp_up", "ramp_down", "fixed_cost"):
        bat.pop(key, None)
    cfg = load_config(_write(tmp_path, raw))
    b = cfg.systems["island"].batteries["bess"]
    assert b.ramp_up == [0.0] and b.ramp_down == [0.0] and b.fixed_cost == [0.0]
    assert b.reservable is True
    assert cfg.unknown_keys == []


def test_old_battery_fields_are_unknown_keys(tmp_path):
    raw = _old_project()
    assert sorted(find_unknown_keys(raw)) == sorted(
        f"systems.island.batteries.bess.{k}" for k in REMOVED)
    cfg = load_config(_write(tmp_path, raw))
    assert sorted(cfg.unknown_keys) == sorted(
        f"systems.island.batteries.bess.{k}" for k in REMOVED)


def test_battery_technology_reads_operating_fields():
    bt = BatteryTechnologyConfig(**{
        **_make_battery_technology("li", 2).model_dump(),
        "ramp_up": [0.5, 0.5], "fixed_cost": [12.0, 12.0], "reservable": False,
        "Availability": "avail.csv"})
    assert bt.ramp_up == [0.5, 0.5]
    assert bt.fixed_cost == [12.0, 12.0]
    assert bt.reservable is False
    assert bt.availability_file == "avail.csv"


def test_old_studio_project_resaves_without_the_battery_generator_fields(tmp_path):
    pytest.importorskip("PySide6")
    from esfex.visualization.data.serializer import (
        config_to_global_settings, config_to_gui_states, gui_state_to_yaml,
    )

    raw = _old_project()
    bat = raw["systems"]["island"]["batteries"]["bess"]
    bat["ramp_up"] = [0.3, 0.0]
    bat["fixed_cost"] = [15000.0, 0.0]
    bat["reservable"] = False
    cfg = load_config(_write(tmp_path, raw))
    states = config_to_gui_states(cfg, base_dir=FIXTURE.parent)
    out = tmp_path / "resaved.yaml"
    gui_state_to_yaml(states, cfg, out, global_settings=config_to_global_settings(cfg))
    saved = yaml.safe_load(out.read_text(encoding="utf-8"))
    sbat = saved["systems"]["island"]["batteries"]["bess"]
    assert not set(REMOVED) & set(sbat)
    assert find_unknown_keys(saved) == []
    again = load_config(out).systems["island"].batteries["bess"]
    assert again.ramp_up[0] == pytest.approx(0.3)
    assert again.fixed_cost[0] == pytest.approx(15000.0)
    assert again.reservable is False


# ── Studio form ─────────────────────────────────────────────────────────


def test_battery_form_edits_reservable_and_drops_generator_fields(qapp):
    from esfex.visualization.data.gui_model import GuiModel
    from esfex.visualization.panels.battery_form import BatteryForm

    model = GuiModel()
    model.add_node(name="N0")
    iid = model.add_battery_instance(unit_key="bess", name="BESS", node=0)
    form = BatteryForm(model)
    form.load_element(iid)
    for attr in ("_min_power", "_min_up", "_min_down", "_eff_at_rated",
                 "_eff_at_min", "_fuel_cost", "_start_up_cost"):
        assert not hasattr(form, attr)
    assert form._reservable.isChecked()
    form._reservable.setChecked(False)
    assert model.state.batteries[iid].reservable is False


# ── Runner and converters ───────────────────────────────────────────────


def test_battery_technology_unit_fields_carry_the_operating_fields():
    """The batteries the master builds took ramp 1.0, fixed cost 0 and
    reservable from nowhere: the technology's values were dropped."""
    from esfex.runner import battery_technology_unit_fields

    bt = _make_battery_technology("li", 2).model_copy(update=dict(
        ramp_up=[0.25, 0.25], ramp_down=[0.5, 0.5], fixed_cost=[9.0, 9.0],
        reservable=False, availability_file="bat_avail.csv"))
    f = battery_technology_unit_fields(bt, lambda arr, d: list(arr))
    assert f["ramp_up"] == [0.25, 0.25]
    assert f["ramp_down"] == [0.5, 0.5]
    assert f["fixed_cost"] == [9.0, 9.0]
    assert f["reservable"] is False
    assert f["availability_file"] == "bat_avail.csv"


def test_battery_availability_is_preloaded_under_its_own_key(tmp_path):
    from esfex.bridge.converters import (
        battery_availability_key,
        battery_technology_availability_key,
    )
    from esfex.runner import Orchestrator

    np.savetxt(tmp_path / "bat.csv", np.full((8760, 2), 0.5), delimiter=",")
    bat = _make_battery("BESS", 2).model_copy(update={"availability_file": "bat.csv"})
    bt = _make_battery_technology("li", 2).model_copy(update={"availability_file": "bat.csv"})
    sysc = _make_system_config(num_nodes=2, batteries={"bess": bat})
    sysc = sysc.model_copy(update={"battery_technologies": {"li": bt}})
    fake = SimpleNamespace(primary_system=sysc, config_path=str(tmp_path / "case.yaml"),
                           _data_start=lambda: datetime(2025, 1, 1))
    cache = Orchestrator._preload_availability_profiles(fake, 2)
    assert np.allclose(cache[battery_availability_key("bess")], 0.5)
    assert np.allclose(cache[battery_technology_availability_key("li")], 0.5)
    assert "bess" not in cache  # cannot shadow a generator of that key


@pytest.mark.julia
def test_convert_battery_config_passes_the_operating_fields():
    from esfex.bridge.converters import COST_SCALE, convert_battery_config

    bat = _make_battery("BESS", 2).model_copy(update=dict(
        ramp_up=[0.25, 0.0], ramp_down=[0.5, 0.0], fixed_cost=[1.0e4, 0.0],
        reservable=False))
    avail = np.array([[0.5, 1.0], [1.0, 0.2]])
    # node 0 has buses 0 and 2, node 1 bus 1
    jl = convert_battery_config(bat, bus_to_node=[0, 1, 0], availability=avail)
    assert list(jl.ramp_up) == [0.25, 0.0, 0.25]
    assert list(jl.ramp_down) == [0.5, 0.0, 0.5]
    assert list(jl.fixed_cost) == pytest.approx([1.0e4 * COST_SCALE, 0.0, 1.0e4 * COST_SCALE])
    assert jl.reservable is False
    a = np.asarray(jl.availability)
    assert a.shape == (2, 3)
    assert np.allclose(a, [[0.5, 1.0, 0.5], [1.0, 0.2, 1.0]])
    # Without a profile: a 0-row matrix (fully available); a single
    # ramp value applies to every node
    jl = convert_battery_config(_make_battery("B", 2).model_copy(update={"ramp_up": [0.3]}))
    assert np.asarray(jl.availability).shape == (0, 2)
    assert list(jl.ramp_up) == [0.3, 0.3]


@pytest.mark.julia
def test_convert_battery_technology_config_passes_the_operating_fields():
    from esfex.bridge.converters import convert_battery_technology_config

    bt = _make_battery_technology("li", 2).model_copy(update=dict(
        ramp_up=[0.2, 0.2], fixed_cost=[5.0e3, 5.0e3], reservable=False))
    jl = convert_battery_technology_config(bt, availability=np.full((3, 2), 0.7))
    assert list(jl.ramp_up) == [0.2, 0.2]
    assert jl.reservable is False
    assert np.asarray(jl.availability).shape == (3, 2)


@pytest.mark.julia
def test_flat_discharge_curve_reaches_julia():
    """A single-segment discharge curve was dropped (only curves with more
    than one segment were passed), so the discharge stayed priced at the
    throughput cost."""
    from esfex.bridge.converters import build_bat_cost_curves_dict
    from esfex.config.schema import CostCurveConfig

    flat = CostCurveConfig(curve_type="flat", flat_price=80.0)
    bat = _make_battery("BESS", 2).model_copy(update=dict(
        throughput_degradation_cost=[1.0, 1.0], discharge_cost_curve=[flat, flat]))
    d = build_bat_cost_curves_dict([("bess", bat)], 2)
    assert set(d[1].keys()) == {1, 2}
    assert len(d[1][1]) == 1
    # A flat curve at the throughput cost changes nothing and is skipped
    same = CostCurveConfig(curve_type="flat", flat_price=1.0)
    bat2 = bat.model_copy(update={"discharge_cost_curve": [same, same]})
    assert len(build_bat_cost_curves_dict([("bess", bat2)], 2)) == 0


# ── Dispatch through the adapter ────────────────────────────────────────


def _one_node_system(bat: BatteryConfig) -> SystemConfig:
    gas = _make_generator("Gas", "Non-renewable", "Gas", [40.0])
    return SystemConfig(name="b", nodes=_make_node_config(1),
                        generators={"gas": gas}, batteries={"bess": bat})


def _bess(**kw) -> BatteryConfig:
    return _make_battery("BESS", 1).model_copy(update=dict(
        capacity=[400.0], MaxChargePower=[20.0], MaxDischargePower=[20.0],
        rated_power=[20.0], max_DoD=[1.0], soc_initial=[0.5], degradation_rate=[0.0],
        invest_max_power=[0.0], invest_max_capacity=[0.0], **kw))


@pytest.mark.julia
def test_dispatch_applies_battery_availability_and_ramp_seam():
    from esfex.bridge.adapters import PowerSystemAdapter
    from esfex.bridge.converters import battery_availability_key

    hours = 2
    demand = np.array([[60.0], [60.0]])  # 40 MW of gas: the battery must give 20

    def solve(bat, **kw):
        ps = PowerSystemAdapter(
            config=_one_node_system(bat), demand=demand, hours=hours, num_nodes=1,
            year=2025, base_year=2025, mode="economic_dispatch", **kw)
        ps.build_model()
        assert ps.solve() == 1
        return np.asarray(ps.get_solution_values()["bat_discharge"])[0, 0, :]

    assert solve(_bess()) == pytest.approx([20.0, 20.0], abs=1e-6)
    # Half available in every hour: 10 MW
    avail = {battery_availability_key("bess"): np.full((8760, 1), 0.5)}
    d = solve(_bess(availability_file="bat.csv"), availability_cache=avail)
    assert d == pytest.approx([10.0, 10.0], abs=1e-6)
    # 0.25 of 20 MW per hour from 0 MW at the previous window's last step
    d = solve(_bess(ramp_up=[0.25], ramp_down=[0.25]),
              boundary_conditions={"battery_discharge_prev": np.zeros((1, 1)),
                                   "battery_charge_prev": np.zeros((1, 1))})
    assert d == pytest.approx([5.0, 10.0], abs=1e-6)


@pytest.mark.julia
def test_master_built_battery_keeps_its_technology_fields():
    """The dispatch's virtual battery (a master build) was always reservable,
    with ramp 1.0 and no fixed cost."""
    from esfex.bridge.adapters import PowerSystemAdapter
    from esfex.runner import battery_technology_unit_fields

    bt = _make_battery_technology("li", 1).model_copy(update=dict(
        ramp_up=[0.25], ramp_down=[0.5], fixed_cost=[1.0e4], reservable=False))
    vdata = {
        **battery_technology_unit_fields(bt, lambda arr, d: list(arr)),
        "capacity": [80.0], "MaxChargePower": [20.0], "MaxDischargePower": [20.0],
        "degradation_rate": [0.0], "initial_age": [0], "life_time": [999],
        "decommissioning_cost": [0.0],
    }
    sysc = SystemConfig(name="b", nodes=_make_node_config(1),
                        generators={"gas": _make_generator("Gas", "Non-renewable", "Gas", [40.0])})
    ps = PowerSystemAdapter(config=sysc, demand=np.full((2, 1), 30.0), hours=2, num_nodes=1,
                            year=2025, base_year=2025, mode="economic_dispatch",
                            units_config={"inv_li": vdata})
    ps.build_model()
    jb = ps._jl_input.batteries[0]
    assert jb.reservable is False
    assert list(jb.ramp_up) == [0.25]
    assert list(jb.ramp_down) == [0.5]
    assert list(jb.fixed_cost)[0] > 0
