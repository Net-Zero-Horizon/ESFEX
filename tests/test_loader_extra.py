"""
Additive coverage tests for esfex.config.loader.

Targets the branches not exercised by tests/test_loader.py:
- _convert_dc_power_flow: nested dict, non-dict nested fallback, loss_model fields
- _convert_generator: reservoir field passthrough, bus_id_per_node / bus_index,
  current_type / frequency_hz defaults
- _convert_battery: bus_id_per_node / bus_index passthrough
- _convert_system: unit_/bat_ legacy keys, unknown/Storage type routing,
  pre-converted generators/batteries dicts, fuels, primary_energy_sources,
  penalties (case-insensitive), co2_budget, electric_demand, ev_categories,
  rooftop_solar_config, stochastic_scenarios, technologies, battery_technologies,
  DC_/dc_ key stripping, electrolyzer singular/plural, bus current_type sanitize,
  and the full fuel_cost / non-fuel-LCOE linking block (lines 449-532).
- load_config: temporal/solver/n1_security/master_problem/meta_network branches.
- load_system_config: ValidationError wrapping.
"""

import copy
import logging
from pathlib import Path

import pytest
import yaml

from esfex.config.loader import (
    ConfigLoadError,
    _convert_battery,
    _convert_dc_power_flow,
    _convert_generator,
    _convert_system,
    load_config,
    load_system_config,
)
from esfex.config.schema import (
    BatteryConfig,
    DCPowerFlowConfig,
    ElectrolyzerConfig,
    GeneratorConfig,
    SystemConfig,
    ESFEXConfig,
)


# ---------------------------------------------------------------------------
# Reusable data builders (copied shapes from tests/test_loader.py fixtures)
# ---------------------------------------------------------------------------


def _minimal_system():
    return {
        "name": "TestSys",
        "demand_path": None,
        "demand_scale": 1.0,
        "loss_demand_threshold": 0.05,
        "life_extension_cost_factor": 0.2,
        "sim_rooftop": False,
        "target_re_penetration": 0.5,
        "min_annual_increment": 0.01,
        "max_annual_increment": 0.1,
        "discount_rate": 0.05,
        "base_lcoe": 93,
        "inertia_limit_threshold": 0.1,
        "nodes": {
            "nodes_connections": [0, 100, 100, 0],
            "reserve_static": [10, 10],
            "reserve_dynamic": [20, 20],
            "reserve_duration": [2, 2],
            "losses": [0.001, 0.001],
            "transference_invest_cost": [13000, 13000],
            "transference_invest_max": [100, 100],
        },
        "fuel_transport_distances": [[0, 50], [50, 0]],
    }


def _thermal_gen(name="Gas_plant", fuel="Gas", technology=None,
                 fuel_cost=None, maintenance_cost=None, eff_at_rated=None):
    return {
        "name": name,
        "type": "Non-renewable",
        "fuel": fuel,
        "technology": technology,
        "reservable": True,
        "life_time": [30, 30],
        "initial_age": [0, 0],
        "degradation_rate": [0.005, 0.005],
        "decommissioning_cost": [300, 300],
        "rated_power": [100, 100],
        "min_power": [10, 10],
        "min_up": [4, 4],
        "min_down": [4, 4],
        "ramp_up": [0.5, 0.5],
        "ramp_down": [0.5, 0.5],
        "eff_at_rated": eff_at_rated if eff_at_rated is not None else [0.5, 0.5],
        "eff_at_min": [0.4, 0.4],
        "inertia": [4, 4],
        "start_up_cost": [1000, 1000],
        "fuel_cost": fuel_cost if fuel_cost is not None else [0, 0],
        "fixed_cost": [5.0, 5.0],
        "maintenance_cost": maintenance_cost if maintenance_cost is not None else [0.0, 0.0],
        "invest_cost": [800000, 800000],
        "invest_max_power": [200, 200],
        "Availability": None,
    }


def _renewable_gen(name="Solar", fuel="Sun"):
    return {
        "name": name,
        "type": "Renewable",
        "fuel": fuel,
        "technology": "Solar PV",
        "reservable": False,
        "life_time": [25, 25],
        "initial_age": [0, 0],
        "degradation_rate": [0.005, 0.005],
        "decommissioning_cost": [300, 300],
        "rated_power": [50, 80],
        "min_power": [0, 0],
        "min_up": [0, 0],
        "min_down": [0, 0],
        "ramp_up": [1.0, 1.0],
        "ramp_down": [1.0, 1.0],
        "eff_at_rated": [0.98, 0.98],
        "eff_at_min": [0.98, 0.98],
        "inertia": [0, 0],
        "start_up_cost": [0, 0],
        "fuel_cost": [0, 0],
        "fixed_cost": [5.0, 5.0],
        "maintenance_cost": [5.4, 5.4],
        "invest_cost": [900000, 900000],
        "invest_max_power": [200, 200],
        "Availability": None,
    }


def _battery(name="Li-ion"):
    return {
        "name": name,
        "type": "Storage",
        "fuel": "None",
        "reservable": True,
        "spillage": True,
        "life_time": [15, 15],
        "initial_age": [0, 0],
        "degradation_rate": [0.01, 0.01],
        "decommissioning_cost": [200, 200],
        "rated_power": [25, 40],
        "min_power": [0, 0],
        "min_up": [0, 0],
        "min_down": [0, 0],
        "ramp_up": [1.0, 1.0],
        "ramp_down": [1.0, 1.0],
        "eff_at_rated": [0.95, 0.95],
        "eff_at_min": [0.95, 0.95],
        "inertia": [0, 0],
        "start_up_cost": [0, 0],
        "fuel_cost": [0, 0],
        "fixed_cost": [5.0, 5.0],
        "maintenance_cost": [3.0, 3.0],
        "invest_cost": [200000, 200000],
        "invest_max_power": [50, 50],
        "efficiency_charge": [0.95, 0.95],
        "efficiency_discharge": [0.95, 0.95],
        "soc_initial": [0.5, 0.5],
        "max_DoD": [0.9, 0.9],
        "capacity": [50, 80],
        "MaxChargePower": [25, 40],
        "MaxDischargePower": [25, 40],
        "Availability": None,
    }


def _fuel(price_base=110.0, energy_content=12.28, emission_factor=0.2):
    return {
        "name": "Gas",
        "unit": "ton",
        "emission_factor": emission_factor,
        "energy_content": energy_content,
        "price_base": price_base,
        "price_growth_rate": 0.0,
    }


# ---------------------------------------------------------------------------
# _convert_dc_power_flow extra branches
# ---------------------------------------------------------------------------


class TestConvertDCPowerFlowExtra:
    def test_nested_dict_takes_priority(self):
        data = {
            "dc_power_flow": {
                "base_impedance": 250.0,
                "reactance_per_km": 0.7,
                "voltage_level_kv": 500.0,
                "max_angle_diff_deg": 45.0,
                "slack_bus": 3,
                "loss_model": "linear",
                "pwl_loss_segments": 5,
                "pwl_loss_segments_master": 4,
            },
            "dc_base_impedance": 100.0,  # should be ignored
        }
        result = _convert_dc_power_flow(data)
        assert result.base_impedance == 250.0
        assert result.slack_bus == 3
        assert result.loss_model == "linear"
        assert result.pwl_loss_segments == 5
        assert result.pwl_loss_segments_master == 4

    def test_non_dict_nested_falls_back_to_flat(self):
        # nested is not a dict -> reset to {} then fall back to flat keys
        data = {"dc_power_flow": "not_a_dict", "dc_base_impedance": 333.0}
        result = _convert_dc_power_flow(data)
        assert result.base_impedance == 333.0

    def test_loss_model_defaults(self):
        result = _convert_dc_power_flow({})
        assert result.loss_model == "pwl"
        assert result.pwl_loss_segments == 3
        assert result.pwl_loss_segments_master == 2


# ---------------------------------------------------------------------------
# _convert_generator extra branches
# ---------------------------------------------------------------------------


class TestConvertGeneratorExtra:
    def test_reservoir_fields_passthrough(self):
        data = _thermal_gen()
        data["type"] = "Renewable"
        data["fuel"] = "Water"
        data["reservoir_capacity"] = [1000, 1000]
        data["reservoir_initial_level"] = [0.5, 0.5]
        gen = _convert_generator("hydro", data)
        assert isinstance(gen, GeneratorConfig)
        assert gen.reservoir_capacity == [1000, 1000]
        assert gen.reservoir_initial_level == [0.5, 0.5]

    def test_bus_assignment_fields_passthrough(self):
        data = _thermal_gen()
        data["bus_id_per_node"] = {0: "bus_a"}
        data["bus_index"] = 2
        gen = _convert_generator("g", data)
        assert gen.bus_id_per_node == {0: "bus_a"}
        assert gen.bus_index == 2

    def test_default_frequency_and_current_type(self):
        data = _thermal_gen()
        gen = _convert_generator("g", data)
        assert gen.frequency_hz == 50.0
        assert gen.current_type == "AC"


# ---------------------------------------------------------------------------
# _convert_battery extra branches
# ---------------------------------------------------------------------------


class TestConvertBatteryExtra:
    def test_bus_assignment_fields_passthrough(self):
        data = _battery()
        data["bus_id_per_node"] = {0: "bus_b"}
        data["bus_index"] = 7
        bat = _convert_battery("bat_0", data)
        assert bat.bus_id_per_node == {0: "bus_b"}
        assert bat.bus_index == 7

    def test_invest_cost_energy_explicit(self):
        data = _battery()
        data["invest_cost_energy"] = [123, 123]
        bat = _convert_battery("bat_0", data)
        assert bat.invest_cost_energy == [123, 123]

    def test_default_current_type_dc(self):
        bat = _convert_battery("bat_0", _battery())
        assert bat.current_type == "DC"


# ---------------------------------------------------------------------------
# _convert_system: legacy unit_/bat_ keys & type routing
# ---------------------------------------------------------------------------


class TestConvertSystemUnitKeys:
    def test_legacy_unit_and_bat_keys(self):
        data = _minimal_system()
        data["unit_0"] = _thermal_gen(name="Therm")
        data["bat_0"] = _battery(name="Bat")
        sc = _convert_system(data)
        assert "unit_0" in sc.generators
        assert "bat_0" in sc.batteries
        # legacy keys removed from system_data (no leftover attribute)
        assert not hasattr(sc, "unit_0")

    def test_unknown_type_sanitized_to_non_renewable(self, caplog):
        data = _minimal_system()
        unit = _thermal_gen(name="Mystery")
        unit["type"] = "Thermal"  # unknown
        data["unit_1"] = unit
        with caplog.at_level(logging.WARNING):
            sc = _convert_system(data)
        assert sc.generators["unit_1"].type == "Non-renewable"
        assert any("unknown type" in r.message for r in caplog.records)

    def test_renewable_unit_routed_to_generators(self):
        data = _minimal_system()
        data["unit_2"] = _renewable_gen(name="Sun1")
        sc = _convert_system(data)
        assert sc.generators["unit_2"].type == "Renewable"


# ---------------------------------------------------------------------------
# _convert_system: pre-converted generators/batteries dicts
# ---------------------------------------------------------------------------


class TestConvertSystemPreConverted:
    def test_generators_dict_with_unknown_type(self, caplog):
        data = _minimal_system()
        g = _thermal_gen(name="Weird")
        g["type"] = "FooBar"
        data["generators"] = {"g0": g}
        with caplog.at_level(logging.WARNING):
            sc = _convert_system(data)
        assert sc.generators["g0"].type == "Non-renewable"

    def test_batteries_dict(self):
        data = _minimal_system()
        data["batteries"] = {"b0": _battery(name="B0")}
        sc = _convert_system(data)
        assert "b0" in sc.batteries


# ---------------------------------------------------------------------------
# _convert_system: nested config sub-blocks
# ---------------------------------------------------------------------------


class TestConvertSystemSubBlocks:
    def test_fuels_block(self):
        data = _minimal_system()
        data["fuels"] = {"Gas": _fuel()}
        sc = _convert_system(data)
        assert "Gas" in sc.fuels
        assert sc.fuels["Gas"].price_base == 110.0

    def test_primary_energy_sources(self):
        data = _minimal_system()
        data["primary_energy_sources"] = {
            "LNG": {
                "unit": "m3",
                "max_availability": [1000, 1000],
                "import_cost": [50, 50],
                "storage_capacity": [500, 500],
                "initial_storage_level": [0.5, 0.5],
                "storage_investment_cost": 10000,
                "transport_cost": 5,
                "transport_losses": 0.01,
                "max_storage_investment_per_node": 200,
                "max_transport_investment_per_arc": 100,
            }
        }
        sc = _convert_system(data)
        assert "LNG" in sc.primary_energy_sources

    def test_penalties_case_insensitive_and_transfermargin_alias(self):
        data = _minimal_system()
        data["penalties"] = {
            "LOSS_OF_LOAD": 5e6,
            "TransferMargin": 77,
            "curtailment_cost": 25.0,
        }
        sc = _convert_system(data)
        assert sc.penalties.loss_of_load == 5e6
        assert sc.penalties.transfer_margin == 77
        assert sc.penalties.curtailment_cost == 25.0

    def test_co2_budget_block(self):
        data = _minimal_system()
        data["co2_budget"] = {"enabled": False}
        sc = _convert_system(data)
        assert sc.co2_budget.enabled is False

    def test_electric_demand_sectors(self):
        data = _minimal_system()
        data["electric_demand"] = {
            "residential": {"is_flexible": True, "flexibility_ratio": 0.2}
        }
        sc = _convert_system(data)
        assert sc.electric_demand["residential"].is_flexible is True

    def test_ev_categories(self):
        data = _minimal_system()
        data["ev_categories"] = {
            "car": {
                "battery_capacity": 60,
                "charging_power": 7,
                "v2g_power": 5,
                "v2g_participation": 0.3,
                "efficiency_charge": 0.95,
                "efficiency_discharge": 0.95,
                "min_soc": 0.2,
            }
        }
        sc = _convert_system(data)
        assert sc.ev_categories["car"].battery_capacity == 60

    def test_rooftop_solar_config(self):
        data = _minimal_system()
        data["rooftop_solar_config"] = {
            "systems_per_node": [100, 200],
            "avg_system_size": [5.0, 5.0],
            "initial_adoption": [0.1, 0.1],
            "max_adoption": {"low": 0.3},
            "adoption_rates": {"low": 0.05},
        }
        sc = _convert_system(data)
        assert sc.rooftop_solar_config.systems_per_node == [100, 200]

    def test_stochastic_scenarios_with_multipliers(self):
        data = _minimal_system()
        data["stochastic_scenarios"] = [
            {
                "name": "base",
                "probability": 0.6,
                "multipliers": {"fuel_cost": 1.2, "demand_growth": 1.1},
            },
            {"name": "high", "probability": 0.4},
        ]
        sc = _convert_system(data)
        assert len(sc.stochastic_scenarios) == 2
        assert sc.stochastic_scenarios[0].multipliers.fuel_cost == 1.2

    def test_technologies_block_name_injected(self):
        data = _minimal_system()
        data["technologies"] = {
            "solar_pv": {
                "type": "Renewable",
                "fuel": "Sun",
                "invest_cost": [900000, 900000],
                "invest_max_power": [500, 500],
                "eff_at_rated": [0.98, 0.98],
                "degradation_rate": [0.005, 0.005],
                "lifetime": 25,
            }
        }
        sc = _convert_system(data)
        assert sc.technologies["solar_pv"].name == "solar_pv"

    def test_battery_technologies_block_name_injected(self):
        data = _minimal_system()
        data["battery_technologies"] = {
            "liion": {
                "invest_cost_power": [200000, 200000],
                "invest_cost_energy": [150000, 150000],
                "invest_max_power": [100, 100],
                "invest_max_capacity": [400, 400],
                "efficiency_charge": [0.95, 0.95],
                "efficiency_discharge": [0.95, 0.95],
                "degradation_rate": [0.01, 0.01],
                "lifetime": 15,
            }
        }
        sc = _convert_system(data)
        assert sc.battery_technologies["liion"].name == "liion"


# ---------------------------------------------------------------------------
# _convert_system: DC key stripping
# ---------------------------------------------------------------------------


class TestConvertSystemDCStripping:
    def test_dc_prefixed_keys_removed(self):
        data = _minimal_system()
        data["DC_BASE_IMPEDANCE"] = 200.0
        data["dc_reactance_per_km"] = 0.9
        sc = _convert_system(data)
        # DC config built from the flat keys
        assert sc.dc_power_flow.base_impedance == 200.0
        assert sc.dc_power_flow.reactance_per_km == 0.9
        # The raw DC_/dc_ keys must not survive as attributes
        assert not hasattr(sc, "DC_BASE_IMPEDANCE")
        assert not hasattr(sc, "dc_reactance_per_km")


# ---------------------------------------------------------------------------
# _convert_system: electrolyzers
# ---------------------------------------------------------------------------


def _electrolyzer_data():
    return {
        "name": "EL1",
        "life_time": [20, 20],
        "initial_age": [0, 0],
        "degradation_rate": [0.01, 0.01],
        "rated_power": [50, 50],
        "min_power": [5, 5],
        "ramp_up": [1.0, 1.0],
        "ramp_down": [1.0, 1.0],
        "eff_at_rated": [0.7, 0.7],
        "eff_at_min": [0.6, 0.6],
        "fixed_cost": [5, 5],
        "variable_cost": [2, 2],
        "invest_cost": [500000, 500000],
        "invest_max_power": [100, 100],
    }


class TestConvertSystemElectrolyzers:
    def test_singular_electrolyzer(self):
        data = _minimal_system()
        data["electrolyzer"] = _electrolyzer_data()
        sc = _convert_system(data)
        assert "electrolyzer" in sc.electrolyzers
        assert isinstance(sc.electrolyzers["electrolyzer"], ElectrolyzerConfig)

    def test_plural_electrolyzers_dict_and_object(self):
        data = _minimal_system()
        already = ElectrolyzerConfig(**_electrolyzer_data())
        data["electrolyzers"] = {
            "el_a": _electrolyzer_data(),
            "el_b": already,
        }
        sc = _convert_system(data)
        assert set(sc.electrolyzers) == {"el_a", "el_b"}
        assert sc.electrolyzers["el_b"] is already


# ---------------------------------------------------------------------------
# _convert_system: bus current_type sanitation
# ---------------------------------------------------------------------------


class TestConvertSystemBusSanitize:
    def test_invalid_bus_current_type_defaulted(self, caplog):
        data = _minimal_system()
        data["buses"] = [
            {"current_type": "WEIRD"},
            {"current_type": "AC"},
        ]
        with caplog.at_level(logging.WARNING):
            sc = _convert_system(data)
        # First bus sanitized to AC; both buses survive validation.
        assert len(sc.buses) == 2
        assert sc.buses[0].current_type == "AC"
        assert any("invalid current_type" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _convert_system: the fuel_cost / non-fuel LCOE linking block (449-532)
# ---------------------------------------------------------------------------


class TestFuelLinking:
    def test_links_fuel_cost_from_fuels_block(self):
        """Thermal gen with fuel_cost=[0,0] gets fuel_cost computed from fuels."""
        data = _minimal_system()
        data["fuels"] = {"Gas": _fuel(price_base=110.0, energy_content=12.28)}
        data["unit_0"] = _thermal_gen(
            name="GasPlant", fuel="Gas", fuel_cost=[0, 0], eff_at_rated=[0.5, 0.5]
        )
        sc = _convert_system(data)
        fc = sc.generators["unit_0"].fuel_cost
        expected = 110.0 / 12.28 / 0.5
        assert fc[0] == pytest.approx(expected)

    def test_uses_technology_efficiency_when_gen_eff_zero(self):
        """eff_at_rated entry 0 -> fall back to technology eff_at_rated[0]."""
        data = _minimal_system()
        data["fuels"] = {"Gas": _fuel(price_base=100.0, energy_content=10.0)}
        data["technologies"] = {
            "ccgt": {
                "type": "Non-renewable",
                "fuel": "Gas",
                "invest_cost": [800000, 800000],
                "invest_max_power": [200, 200],
                "eff_at_rated": [0.4, 0.4],
                "degradation_rate": [0.005, 0.005],
                "lifetime": 30,
            }
        }
        data["unit_0"] = _thermal_gen(
            name="GP", fuel="Gas", technology="ccgt",
            fuel_cost=[0, 0], eff_at_rated=[0.0, 0.0],
        )
        sc = _convert_system(data)
        fc = sc.generators["unit_0"].fuel_cost
        # eff falls back to tech 0.4 -> 100/10/0.4 = 25
        assert fc[0] == pytest.approx(25.0)

    def test_non_fuel_lcoe_added_to_maintenance(self):
        """Gas gen with existing positive fuel_cost gets +60 maintenance adder."""
        data = _minimal_system()
        data["fuels"] = {"Gas": _fuel()}
        data["unit_0"] = _thermal_gen(
            name="GP", fuel="Gas", fuel_cost=[30, 30], maintenance_cost=[0.0, 0.0]
        )
        sc = _convert_system(data)
        maint = sc.generators["unit_0"].maintenance_cost
        # Gas adder is 60.0, fuel_cost>0 at both nodes
        assert maint[0] == pytest.approx(60.0)

    def test_existing_substantial_maintenance_not_doubled(self):
        """If maintenance already >= adder*0.5, skip the LCOE add."""
        data = _minimal_system()
        data["fuels"] = {"Gas": _fuel()}
        data["unit_0"] = _thermal_gen(
            name="GP", fuel="Gas", fuel_cost=[30, 30], maintenance_cost=[40.0, 40.0]
        )
        sc = _convert_system(data)
        # 40 >= 60*0.5=30 -> unchanged
        assert sc.generators["unit_0"].maintenance_cost == [40.0, 40.0]

    def test_renewable_generators_skipped(self):
        """Renewable gens are not modified by the linking pass."""
        data = _minimal_system()
        data["fuels"] = {"Gas": _fuel()}
        data["unit_0"] = _renewable_gen(name="Sun", fuel="Sun")
        sc = _convert_system(data)
        # Untouched maintenance from the fixture
        assert sc.generators["unit_0"].maintenance_cost == [5.4, 5.4]

    def test_silent_freebie_no_fuel_warns(self, caplog):
        """Non-renewable with fuel='None' and zero fuel_cost triggers warning."""
        data = _minimal_system()
        data["fuels"] = {"Gas": _fuel()}
        g = _thermal_gen(name="Free", fuel="None", fuel_cost=[0, 0])
        data["unit_0"] = g
        with caplog.at_level(logging.WARNING):
            _convert_system(data)
        assert any("dispatch" in r.message for r in caplog.records)

    def test_silent_freebie_fuel_not_in_fuels_warns(self, caplog):
        """Non-renewable referencing a fuel absent from fuels block warns."""
        data = _minimal_system()
        data["fuels"] = {"Gas": _fuel()}
        g = _thermal_gen(name="Ghost", fuel="Kerosene", fuel_cost=[0, 0])
        data["unit_0"] = g
        with caplog.at_level(logging.WARNING):
            _convert_system(data)
        assert any("not in fuels" in r.message for r in caplog.records)

    def test_no_link_when_fuel_price_zero(self):
        """price_base=0 -> no fuel_cost computed (stays as-is)."""
        data = _minimal_system()
        data["fuels"] = {"Gas": _fuel(price_base=0.0)}
        data["unit_0"] = _thermal_gen(name="GP", fuel="Gas", fuel_cost=[0, 0])
        sc = _convert_system(data)
        # fuel_cost not linked (price 0); but Gas LCOE adder still 60 where
        # fuel_cost>0 — here fuel_cost stays 0, so maintenance unchanged.
        assert max(sc.generators["unit_0"].fuel_cost) == 0

    def test_unknown_fuel_no_lcoe_adder(self):
        """Fuel present in fuels but not in _NON_FUEL_LCOE -> no maintenance add."""
        data = _minimal_system()
        biomass = _fuel(price_base=50.0, energy_content=8.0)
        biomass["name"] = "Coal"
        data["fuels"] = {"Coal": biomass}
        g = _thermal_gen(name="CoalPlant", fuel="Coal", fuel_cost=[20, 20],
                         maintenance_cost=[0.0, 0.0])
        data["unit_0"] = g
        sc = _convert_system(data)
        # Coal not in _NON_FUEL_LCOE -> adder 0 -> maintenance unchanged
        assert sc.generators["unit_0"].maintenance_cost == [0.0, 0.0]


# ---------------------------------------------------------------------------
# load_config: temporal / solver / n1_security / master_problem / meta_network
# ---------------------------------------------------------------------------


class TestLoadConfigBranches:
    def _write(self, tmp_path, cfg, name="cfg.yaml"):
        p = tmp_path / name
        p.write_text(yaml.dump(cfg, default_flow_style=False), encoding="utf-8")
        return p

    def _base_cfg(self):
        return {
            "meta_network": {"systems": ["s"]},
            "systems": {"s": _minimal_system()},
        }

    def test_temporal_and_solver_blocks(self, tmp_path):
        cfg = self._base_cfg()
        cfg["temporal"] = {"resolution_hours": 2}
        cfg["solver"] = {"name": "gurobi"}
        result = load_config(self._write(tmp_path, cfg))
        assert isinstance(result, ESFEXConfig)
        assert result.temporal.resolution_hours == 2
        assert result.solver.name == "gurobi"

    def test_n1_security_block(self, tmp_path):
        cfg = self._base_cfg()
        cfg["n1_security"] = {"enabled": True}
        result = load_config(self._write(tmp_path, cfg))
        assert result.n1_security.enabled is True

    def test_master_problem_block(self, tmp_path):
        cfg = self._base_cfg()
        cfg["master_problem"] = {}
        result = load_config(self._write(tmp_path, cfg))
        assert result.master_problem is not None

    def test_meta_network_systems_links(self, tmp_path):
        cfg = self._base_cfg()
        cfg["systems"]["s2"] = _minimal_system()
        cfg["systems"]["s2"]["name"] = "Sys2"
        cfg["meta_network"] = {
            "systems": ["s", "s2"],
            "systems_links": [
                {
                    "systems": ["s", "s2"],
                    "connections": [[0, 0]],
                    "existing_capacity_MW": [100.0],
                    "max_investment_MW": [200.0],
                    "investment_cost_per_MW": [5000.0],
                    "loss_factor": [0.02],
                    "distance_km": [50.0],
                    "cost_per_mw_km": [100.0],
                }
            ],
        }
        result = load_config(self._write(tmp_path, cfg))
        assert len(result.meta_network.systems_links) == 1

    def test_generic_exception_wrapped(self, tmp_path, monkeypatch):
        """Non-ValidationError during conversion is wrapped as 'Failed to load'."""
        cfg = self._base_cfg()
        p = self._write(tmp_path, cfg)

        import esfex.config.loader as loader_mod

        def boom(_data):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(loader_mod, "_convert_system", boom)
        with pytest.raises(ConfigLoadError, match="Failed to load"):
            load_config(p)


# ---------------------------------------------------------------------------
# load_system_config: ValidationError wrapping
# ---------------------------------------------------------------------------


class TestLoadSystemConfigValidation:
    def test_validation_error_wrapped(self, tmp_path):
        """Missing required SystemConfig fields -> ConfigLoadError."""
        p = tmp_path / "bad_system.yaml"
        p.write_text(yaml.dump({"name": "OnlyName"}), encoding="utf-8")
        with pytest.raises(ConfigLoadError):
            load_system_config(p)
