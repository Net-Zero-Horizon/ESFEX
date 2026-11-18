"""
Configuration loader for ESFEX.

Loads YAML configuration files and validates them against Pydantic schemas.
Supports both single-file and multi-file configurations.
"""

import logging
from pathlib import Path
from typing import Any, Optional, Union

import yaml
from pydantic import ValidationError

log = logging.getLogger(__name__)

from esfex.config.schema import (
    BatteryConfig,
    BatteryTechnologyConfig,
    CO2BudgetConfig,
    ConversionTechnologyConfig,
    CriticalityPenalties,
    DCPowerFlowConfig,
    DemandSectorConfig,
    DevelopmentZoneConfig,
    ElectrolyzerConfig,
    EVCategoryConfig,
    FuelConfig,
    FuelEntryPointConfig,
    FuelInfrastructureConfig,
    GeoCoordinate,
    GeneratorConfig,
    MasterProblemConfig,
    MetaNetworkConfig,
    N1SecurityConfig,
    NodeConfig,
    NonElectricDemandConfig,
    PenaltiesConfig,
    PrimaryEnergySourceConfig,
    ESFEXConfig,
    ineffective_scenario_multipliers,
    RooftopSolarConfig,
    ScenarioMultipliers,
    SolverConfig,
    StochasticScenarioConfig,
    SystemConfig,
    SystemLinkConfig,
    TechnologyConfig,
    TemporalConfig,
    TransformerConfig,
    TransmissionLineGeo,
    canonical_unit_type,
)
from esfex.config.legacy_keys import (
    SINGLE_ELECTROLYZER_KEY,
    flat_dc_field,
    is_flat_dc_key,
    is_legacy_unit_key,
    penalty_field,
)
from esfex.config.unknown_keys import find_unknown_keys, find_unknown_system_keys


class ConfigLoadError(Exception):
    """Exception raised when configuration loading fails."""

    pass


class SavedConfigInvalidError(ConfigLoadError):
    """A configuration the Studio saved does not load.

    Raised by :func:`esfex.visualization.launch_studio` after the window
    closes when the user chose "Save anyway" on a configuration with
    errors: the file is written, but reloading it fails. ``path`` is the
    saved file and ``error`` the loader's exception.
    """

    def __init__(self, path: Union[str, Path], error: Exception):
        self.path = str(path)
        self.error = error
        super().__init__(
            f"Configuration saved to {self.path}, but it does not validate: {error}")


def load_yaml(path: Union[str, Path]) -> dict[str, Any]:
    """
    Load a YAML file and return its contents as a dictionary.

    Args:
        path: Path to the YAML file

    Returns:
        Dictionary with YAML contents

    Raises:
        ConfigLoadError: If file cannot be read or parsed
    """
    path = Path(path)

    if not path.exists():
        raise ConfigLoadError(f"Configuration file not found: {path}")

    try:
        with open(path, "r", encoding="utf-8") as f:
            content = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigLoadError(f"Invalid YAML in {path}: {e}")
    except IOError as e:
        raise ConfigLoadError(f"Cannot read {path}: {e}")

    if content is None:
        content = {}

    return content


def _convert_dc_power_flow(system_data: dict[str, Any]) -> DCPowerFlowConfig:
    """Extract DC power flow config from system data.

    Reads from nested ``dc_power_flow:`` dict first, then falls back to flat
    ``dc_*`` keys (any capitalisation) for backward compatibility.
    """
    nested = system_data.get("dc_power_flow", {})
    if not isinstance(nested, dict):
        nested = {}
    kwargs: dict[str, Any] = {}
    for key, value in system_data.items():
        field = flat_dc_field(key)
        # ``dc_<field>`` wins over other spellings of the same key.
        if field is not None and (field not in kwargs or key == f"dc_{field}"):
            kwargs[field] = value
    kwargs.update({k: v for k, v in nested.items() if k in DCPowerFlowConfig.model_fields})
    return DCPowerFlowConfig(**kwargs)


def _convert_fuels(fuels_data: dict[str, dict]) -> dict[str, FuelConfig]:
    """Convert fuel definitions to FuelConfig objects."""
    result = {}
    for name, data in fuels_data.items():
        result[name] = FuelConfig(
            name=data.get("name", name),
            unit=data.get("unit"),
            emission_factor=data.get("emission_factor", 0.0),
            energy_content=data.get("energy_content"),
            price_base=data.get("price_base", 0.0),
            price_growth_rate=data.get("price_growth_rate", 0.0),
        )
    return result


def _pass_through(model: type, data: dict[str, Any], kwargs: dict[str, Any]) -> None:
    """Copy into *kwargs* every *model* field present in *data* (by name or
    alias) that the caller did not map explicitly, so fields the Studio
    writes are not silently dropped."""
    for name, field in model.model_fields.items():
        if name in kwargs:
            continue
        if name in data:
            kwargs[name] = data[name]
        elif field.alias and field.alias in data:
            kwargs[name] = data[field.alias]


def _convert_generator(unit_key: str, data: dict[str, Any]) -> GeneratorConfig:
    """Convert a generator unit definition to GeneratorConfig."""
    kwargs: dict[str, Any] = dict(
        name=data.get("name", unit_key),
        type=data["type"],
        fuel=data.get("fuel", "None"),
        technology=data.get("technology"),
        reservable=data.get("reservable", True),
        life_time=data["life_time"],
        initial_age=data["initial_age"],
        degradation_rate=data["degradation_rate"],
        decommissioning_cost=data["decommissioning_cost"],
        rated_power=data["rated_power"],
        min_power=data["min_power"],
        min_up=data["min_up"],
        min_down=data["min_down"],
        ramp_up=data["ramp_up"],
        ramp_down=data["ramp_down"],
        eff_at_rated=data["eff_at_rated"],
        eff_at_min=data["eff_at_min"],
        inertia=data["inertia"],
        start_up_cost=data["start_up_cost"],
        fuel_cost=data["fuel_cost"],
        fixed_cost=data["fixed_cost"],
        maintenance_cost=data["maintenance_cost"],
        invest_cost=data["invest_cost"],
        availability_file=data.get("Availability", data.get("availability_file")),
        frequency_hz=data.get("frequency_hz", 50.0),
        current_type=data.get("current_type", "AC"),
    )
    # Everything else the schema knows (reservoir and cascade fields,
    # droop, risk_coefficient, cost curves, Q limits, and the authoritative
    # per-node bus assignment bus_id_per_node / bus_index — without which
    # multi-bus-per-node plants are left un-anchored).
    _pass_through(GeneratorConfig, data, kwargs)
    return GeneratorConfig(**kwargs)


def _convert_battery(unit_key: str, data: dict[str, Any]) -> BatteryConfig:
    """Convert a storage unit definition to BatteryConfig."""
    kwargs: dict[str, Any] = dict(
        name=data.get("name", unit_key),
        type="Storage",
        fuel=data.get("fuel", "None"),
        reservable=data.get("reservable", True),
        spillage=data.get("spillage", True),
        min_duration_hours=data.get("min_duration_hours"),
        max_duration_hours=data.get("max_duration_hours"),
        life_time=data["life_time"],
        initial_age=data["initial_age"],
        degradation_rate=data["degradation_rate"],
        decommissioning_cost=data["decommissioning_cost"],
        rated_power=data["rated_power"],
        inertia=data["inertia"],
        maintenance_cost=data["maintenance_cost"],
        invest_cost=data["invest_cost"],
        invest_max_power=data["invest_max_power"],
        efficiency_charge=data["efficiency_charge"],
        efficiency_discharge=data["efficiency_discharge"],
        soc_initial=data["soc_initial"],
        max_DoD=data["max_DoD"],
        capacity=data["capacity"],
        MaxChargePower=data["MaxChargePower"],
        MaxDischargePower=data["MaxDischargePower"],
        availability_file=data.get("Availability", data.get("availability_file")),
        current_type=data.get("current_type", "DC"),
    )
    _pass_through(BatteryConfig, data, kwargs)
    return BatteryConfig(**kwargs)


def _convert_primary_energy_source(name: str, data: dict[str, Any]) -> PrimaryEnergySourceConfig:
    """Convert primary energy source definition."""
    kwargs: dict[str, Any] = dict(
        name=data.get("name", name),
        unit=data["unit"],
        max_availability=data["max_availability"],
        import_cost=data["import_cost"],
        storage_capacity=data["storage_capacity"],
        initial_storage_level=data["initial_storage_level"],
        min_storage_level=data.get("min_storage_level", 0.1),
    )
    _pass_through(PrimaryEnergySourceConfig, data, kwargs)
    return PrimaryEnergySourceConfig(**kwargs)


def _prefer_field_names(model: type, data: dict[str, Any]) -> None:
    """Where *data* gives a field both by name and by alias, keep the name.

    Studio versions up to 0.3.0 saved settings such as
    ``target_re_penetration`` twice: the loaded value under the alias
    (``TARGET_RE_PENETRATION``) and the edited one under the name. Pydantic
    takes the alias, which silently discarded the edit.
    """
    for name, field in model.model_fields.items():
        alias = field.alias
        if alias and alias != name and name in data and alias in data:
            if data[alias] != data[name]:
                log.warning("Both %s (%r) and %s (%r) are set; using %s.",
                            name, data[name], alias, data[alias], name)
            del data[alias]


def _clamp_generator_fractions(generators: dict[str, GeneratorConfig]) -> None:
    """Bring min_power into 0–1 and negative ramps to 0, with a warning.

    min_power is a fraction of rated power and ramp_up/ramp_down are
    fractions of rated power per hour (0 = no limit). Values outside that
    range (e.g. min_power 1.6 left by Studio versions up to 0.3.0, which
    summed it when merging units) are clamped instead of rejected, so the
    project still opens in the Studio and can be corrected there.
    """
    for key, gen in generators.items():
        for attr, lo, hi in (("min_power", 0.0, 1.0),
                             ("ramp_up", 0.0, None), ("ramp_down", 0.0, None)):
            values = list(getattr(gen, attr))
            bad = [i for i, v in enumerate(values)
                   if v < lo or (hi is not None and v > hi)]
            if not bad:
                continue
            changes = []
            for i in bad:
                new = lo if values[i] < lo else hi
                changes.append(f"node {i}: {values[i]:g} → {new:g}")
                values[i] = new
            setattr(gen, attr, values)
            shown = ", ".join(changes[:5])
            if len(changes) > 5:
                shown += f" (+{len(changes) - 5} more)"
            rng = ("0–1 (fraction of rated power)" if hi is not None
                   else "≥ 0 (fraction of rated power per hour, 0 = no limit)")
            log.warning("Generator %s: %s must be %s; clamped %s.",
                        key, attr, rng, shown)


def _warn_zero_ramps(generators: dict[str, GeneratorConfig]) -> None:
    """Point out installed units whose ramp limit is 0. The dispatch treats
    it as "no ramp limit"; projects saved by older Studio versions hold 0
    because that was the default for new generators."""
    zero = sorted(
        key for key, gen in generators.items()
        if any(p > 0 and (gen.ramp_up[i] <= 0 or gen.ramp_down[i] <= 0)
               for i, p in enumerate(gen.rated_power)
               if i < len(gen.ramp_up) and i < len(gen.ramp_down))
    )
    if zero:
        shown = ", ".join(zero[:10]) + (f" (+{len(zero) - 10} more)" if len(zero) > 10 else "")
        log.warning(
            "%d generator(s) have ramp_up/ramp_down = 0 where they have capacity: %s. "
            "0 means no ramp limit; set a fraction of capacity per hour to limit ramping.",
            len(zero), shown,
        )


# Lazard 2024 LCOE 17.0 midpoints minus typical fuel cost — the non-fuel
# portion (capex amortized + fixed + var O&M). Without this thermals look
# artificially cheap (fuel only ~$16-32/MWh) compared to renewable LCOE
# ($34+) and the LP never dispatches/invests in renewables. It is a
# variable adder ($/MWh, into maintenance_cost) that stands in for O&M
# data the generator lacks: a generator with its own fixed_cost
# ($/MW·yr) or a substantial maintenance_cost keeps its values, so the
# fixed O&M is never charged twice.
_NON_FUEL_LCOE = {
    "gas": 60.0,        # CCGT $76 − $16 fuel
    "fuel_oil": 55.0,   # Steam HFO $80 − $27
    "diesel": 60.0,     # Steam diesel (peakers are higher but rare here)
    "biomass": 20.0,    # Mostly fuel; add modest
}
# Fuel names (lower case, without spaces, "_" or "-") of each family above
_NON_FUEL_ALIASES = {
    "gas": ("gas", "naturalgas", "ng", "lng"),
    "fuel_oil": ("fueloil", "oil", "hfo", "heavyfueloil", "crude", "crudeoil"),
    "diesel": ("diesel", "gasoil"),
    "biomass": ("biomass",),
}
_NON_FUEL_FAMILY = {alias: family for family, aliases in _NON_FUEL_ALIASES.items()
                    for alias in aliases}


def _non_fuel_om_adder(fuel_key: str, fuel: Any = None) -> float:
    """Non-fuel O&M adder ($/MWh) for a fuel, 0 if it is not a known family.

    The fuel's key and its display name (``fuels[key].name``) are matched
    ignoring case, spaces, "_" and "-", so "Natural_gas", "Natural Gas",
    "NG" and "LNG" all count as gas, "HFO" or "Crude" as fuel oil and
    "Gasoil" as diesel.
    """
    for name in (fuel_key, getattr(fuel, "name", None)):
        if not isinstance(name, str):
            continue
        norm = "".join(ch for ch in name.lower() if ch not in " _-")
        family = _NON_FUEL_FAMILY.get(norm)
        if family is not None:
            return _NON_FUEL_LCOE[family]
    return 0.0


def _fuel_cost_from_fuel(fuel: Any, effs: list, n: int,
                         fallback_eff: float = 0.0) -> Optional[list[float]]:
    """Per-node fuel cost ($/MWh) of a unit burning *fuel*:
    ``price_base / energy_content / eff`` at each of *n* nodes.

    ``eff`` is ``effs[i]`` where positive, else *fallback_eff*; a node with
    no usable efficiency gets 0. None when the fuel has no positive price
    or energy content.
    """
    price = float(getattr(fuel, "price_base", 0.0) or 0.0)
    energy = float(getattr(fuel, "energy_content", 0.0) or 0.0)
    if price <= 0 or energy <= 0:
        return None
    out = []
    for i in range(n):
        eff = float(effs[i]) if i < len(effs) and effs[i] > 0 else fallback_eff
        out.append(price / energy / eff if eff > 0 else 0.0)
    return out


def _link_technology_fuel_costs(technologies: dict, fuels: dict) -> None:
    """Price the fuel of candidate technologies from the ``fuels`` block.

    Same rule as for generators: a technology whose ``fuel`` is a key of
    *fuels* and whose ``fuel_cost`` is missing or all zero gets
    ``price_base / energy_content / eff_at_rated`` per node, whatever its
    type (Sun, Wind and Water have no entry or a price of 0). Without it,
    the master built thermal candidates as free generation and the dispatch
    ran the built units at no fuel cost. The non-fuel O&M adder is not
    applied: it includes capital, which a technology pays through its
    ``invest_cost``. A non-renewable technology that still has no fuel cost
    (and no offer curve) is named in a warning.
    """
    linked: list[str] = []
    free: list[str] = []
    for key, tech in technologies.items():
        fuel_name = getattr(tech, "fuel", None) or "None"
        fuel = fuels.get(fuel_name)
        fc = list(getattr(tech, "fuel_cost", None) or [])
        if fuel is not None and (not fc or max(fc) <= 0):
            n = max(len(tech.invest_cost), len(tech.invest_max_power),
                    len(tech.eff_at_rated), len(fc), 1)
            effs = list(tech.eff_at_rated or [])
            # A single efficiency is the technology's at every node
            new_fc = _fuel_cost_from_fuel(
                fuel, effs, n, float(effs[0]) if len(effs) == 1 else 0.0)
            if new_fc is not None:
                tech.fuel_cost = new_fc
                fc = new_fc
                linked.append(key)
        if (tech.type != "Renewable" and not tech.fuel_cost_curve
                and (not fc or max(fc) <= 0)):
            reason = ("no fuel" if fuel_name in ("None", "") else
                      f"fuel {fuel_name!r} not in fuels" if fuel is None else
                      f"fuel {fuel_name!r} has no price_base/energy_content > 0"
                      " or eff_at_rated is 0")
            free.append(f"{key} ({reason})")
    if linked:
        log.info("Loader: fuel_cost of technologies %s computed from the fuels block "
                 "(price_base / energy_content / eff_at_rated)", ", ".join(linked))
    if free:
        log.warning(
            "Non-renewable candidate technologies with fuel_cost 0: the master "
            "builds them as free generation and the dispatch runs them at no fuel "
            "cost. Give them a fuel_cost or a priced fuel in fuels. Affected (%d): %s",
            len(free), ", ".join(free))


def _convert_system(system_data: dict[str, Any]) -> SystemConfig:
    """Convert a system dictionary to SystemConfig."""
    # Extract generators and batteries
    generators = {}
    batteries = {}

    # Look for unit_* and bat_* keys (legacy format)
    _valid_gen_types = {"Renewable", "Non-renewable", "Electrolyzer"}
    for key, value in list(system_data.items()):
        if is_legacy_unit_key(key, value):
            unit_type = canonical_unit_type(value.get("type", "Unknown"))
            value["type"] = unit_type
            if unit_type == "Storage":
                batteries[key] = _convert_battery(key, value)
            elif unit_type in _valid_gen_types:
                generators[key] = _convert_generator(key, value)
            else:
                # Sanitize unknown types (e.g. "Thermal") to "Non-renewable"
                log.warning("Generator %s has unknown type '%s', treating as Non-renewable", key, unit_type)
                value["type"] = "Non-renewable"
                generators[key] = _convert_generator(key, value)
            # Remove from system_data to avoid passing to SystemConfig
            del system_data[key]

    # Handle pre-converted generators/batteries dictionaries
    if "generators" in system_data and isinstance(system_data["generators"], dict):
        for key, value in system_data["generators"].items():
            if isinstance(value, dict):
                # Sanitize unknown generator types
                gt = canonical_unit_type(value.get("type", "Non-renewable"))
                value["type"] = gt
                if gt not in ("Renewable", "Non-renewable", "Storage", "Electrolyzer"):
                    log.warning("Generator %s has unknown type '%s', treating as Non-renewable", key, gt)
                    value["type"] = "Non-renewable"
                generators[key] = _convert_generator(key, value)
        del system_data["generators"]

    if "batteries" in system_data and isinstance(system_data["batteries"], dict):
        for key, value in system_data["batteries"].items():
            if isinstance(value, dict):
                batteries[key] = _convert_battery(key, value)
        del system_data["batteries"]

    _clamp_generator_fractions(generators)
    _warn_zero_ramps(generators)

    # Convert fuels
    if "fuels" in system_data and isinstance(system_data["fuels"], dict):
        system_data["fuels"] = _convert_fuels(system_data["fuels"])

    # Convert DC power flow
    system_data["dc_power_flow"] = _convert_dc_power_flow(system_data)

    # Convert primary energy sources
    if "primary_energy_sources" in system_data:
        pes = {}
        for name, data in system_data["primary_energy_sources"].items():
            if isinstance(data, dict):
                pes[name] = _convert_primary_energy_source(name, data)
        system_data["primary_energy_sources"] = pes

    # Convert nodes
    if "nodes" in system_data and isinstance(system_data["nodes"], dict):
        system_data["nodes"] = NodeConfig(**system_data["nodes"])

    # Convert penalties
    if "penalties" in system_data and isinstance(system_data["penalties"], dict):
        # Keys are case-insensitive; the field name wins over a legacy alias.
        pen_data: dict[str, Any] = {}
        for key, value in system_data["penalties"].items():
            field = penalty_field(key)
            if field in PenaltiesConfig.model_fields and (
                    field not in pen_data or str(key).lower() == field):
                pen_data[field] = value
        system_data["penalties"] = PenaltiesConfig(**pen_data)

    # Convert CO2 budget
    if "co2_budget" in system_data and isinstance(system_data["co2_budget"], dict):
        system_data["co2_budget"] = CO2BudgetConfig(**system_data["co2_budget"])

    # Convert electric demand sectors
    if "electric_demand" in system_data and isinstance(system_data["electric_demand"], dict):
        ed = {}
        for sector, data in system_data["electric_demand"].items():
            if isinstance(data, dict):
                ed[sector] = DemandSectorConfig(**data)
        system_data["electric_demand"] = ed

    # Convert EV categories
    if "ev_categories" in system_data and isinstance(system_data["ev_categories"], dict):
        ev = {}
        for cat, data in system_data["ev_categories"].items():
            if isinstance(data, dict):
                ev[cat] = EVCategoryConfig(**data)
        system_data["ev_categories"] = ev

    # Convert rooftop solar config
    if "rooftop_solar_config" in system_data and isinstance(system_data["rooftop_solar_config"], dict):
        system_data["rooftop_solar_config"] = RooftopSolarConfig(**system_data["rooftop_solar_config"])

    # Convert stochastic scenarios
    if "stochastic_scenarios" in system_data and isinstance(system_data["stochastic_scenarios"], list):
        scenarios = []
        for sc in system_data["stochastic_scenarios"]:
            if isinstance(sc, dict):
                if "multipliers" in sc and isinstance(sc["multipliers"], dict):
                    sc["multipliers"] = ScenarioMultipliers(**sc["multipliers"])
                scenarios.append(StochasticScenarioConfig(**sc))
        system_data["stochastic_scenarios"] = scenarios

    # Convert candidate technologies for new investment
    if "technologies" in system_data and isinstance(system_data["technologies"], dict):
        techs = {}
        for key, data in system_data["technologies"].items():
            if isinstance(data, dict):
                if "name" not in data:
                    data["name"] = key
                techs[key] = TechnologyConfig(**data)
        system_data["technologies"] = techs

    if "battery_technologies" in system_data and isinstance(system_data["battery_technologies"], dict):
        bat_techs = {}
        for key, data in system_data["battery_technologies"].items():
            if isinstance(data, dict):
                if "name" not in data:
                    data["name"] = key
                bat_techs[key] = BatteryTechnologyConfig(**data)
        system_data["battery_technologies"] = bat_techs

    # Remove DC_ prefixed keys (already converted into dc_power_flow)
    # Keep dc_power_flow itself (now a DCPowerFlowConfig object)
    keys_to_remove = [k for k in system_data.keys() if is_flat_dc_key(k)]
    for k in keys_to_remove:
        del system_data[k]

    # Convert electrolyzer(s) to dict format
    electrolyzers = {}
    # Handle singular "electrolyzer" key (legacy / GUI-generated)
    if SINGLE_ELECTROLYZER_KEY in system_data:
        el_data = system_data.pop(SINGLE_ELECTROLYZER_KEY)
        if isinstance(el_data, dict):
            electrolyzers["electrolyzer"] = ElectrolyzerConfig(**el_data)
    # Handle plural "electrolyzers" dict
    if "electrolyzers" in system_data:
        el_dict = system_data.pop("electrolyzers")
        if isinstance(el_dict, dict):
            for key, val in el_dict.items():
                if isinstance(val, dict):
                    electrolyzers[key] = ElectrolyzerConfig(**val)
                elif isinstance(val, ElectrolyzerConfig):
                    electrolyzers[key] = val

    # Sanitize bus current_type before Pydantic validation
    if "buses" in system_data and isinstance(system_data["buses"], list):
        for bus_data in system_data["buses"]:
            if isinstance(bus_data, dict):
                ct = bus_data.get("current_type")
                if ct is not None and ct not in ("AC", "DC"):
                    log.warning("Bus has invalid current_type '%s', defaulting to AC", ct)
                    bus_data["current_type"] = "AC"

    # === Link gen.fuel → fuels[fuel].price_base when gen.fuel_cost is missing ===
    # Generators in the YAML often declare `fuel: Gas/Fuel_oil/Diesel/...` without
    # an explicit `fuel_cost`. The legacy loader leaves fuel_cost = [0.0]*N in that
    # case, so the operational LP sees thermal generation as free and never
    # dispatches investment renewables that have a nonzero marginal cost. Compute
    # fuel_cost = price_base / energy_content / efficiency from the fuels block
    # (same formula already in runner._fuel_based_cost, applied post-hoc for LCOE).
    _fuels_dict = system_data.get("fuels", {})
    _techs_dict = system_data.get("technologies", {})

    if isinstance(_fuels_dict, dict) and _fuels_dict:
        _linked_fuel = 0
        _linked_maint = 0
        _silent_freebies: list[str] = []  # non-renewable gens that end up at $0/MWh
        for _gk, _g in generators.items():
            # Any type burning a priced fuel pays for it (a Renewable
            # biomass unit too); Sun/Wind/Water have no fuel entry or a
            # price of 0, so they stay free.
            _renewable = getattr(_g, "type", "") == "Renewable"
            _fuel_name = getattr(_g, "fuel", None) or "None"
            # Flag the silent-failure mode: a non-renewable generator with
            # no fuel reference ends up at fuel_cost=0 AND 0 emissions in the
            # LP (no key in fuel_co2). The LP then dispatches it as free with
            # zero CO2 — invisible bug at solve time. Loud warning here so
            # the user sees it before solving.
            if _fuel_name in ("None", "", None):
                _fc_now = list(getattr(_g, "fuel_cost", None) or [])
                if not _renewable and (not _fc_now or max(_fc_now) <= 0):
                    _silent_freebies.append(getattr(_g, "name", _gk))
                continue
            _fuel = _fuels_dict.get(_fuel_name)
            if _fuel is None:
                _fc_now = list(getattr(_g, "fuel_cost", None) or [])
                if not _renewable and (not _fc_now or max(_fc_now) <= 0):
                    _silent_freebies.append(
                        f"{getattr(_g, 'name', _gk)} (fuel={_fuel_name!r} not in fuels)"
                    )
                continue

            # --- Part 1: compute fuel_cost from fuels block if missing ---
            _fc_existing = list(getattr(_g, "fuel_cost", None) or [])
            if not _fc_existing or max(_fc_existing) <= 0:
                _tech_eff = 0.0
                _tech_id = getattr(_g, "technology", None)
                if _tech_id and isinstance(_techs_dict, dict):
                    _tech = _techs_dict.get(_tech_id)
                    if _tech is not None:
                        _te = list(getattr(_tech, "eff_at_rated", None) or [])
                        if _te and _te[0] > 0:
                            _tech_eff = float(_te[0])
                _rp = list(getattr(_g, "rated_power", None) or [])
                _n = max(len(_rp), len(_fc_existing), 1)
                _new_fc = _fuel_cost_from_fuel(
                    _fuel, list(getattr(_g, "eff_at_rated", None) or []), _n, _tech_eff)
                if _new_fc is not None:
                    try:
                        _g.fuel_cost = _new_fc
                        _linked_fuel += 1
                    except Exception as _exc:
                        log.warning("Cannot set fuel_cost on %s: %s", _g.name, _exc)

            # --- Part 2: add non-fuel LCOE component to maintenance_cost ---
            # Applied REGARDLESS of whether fuel_cost was just set or already
            # present — the non-fuel portion is separate from fuel cost.
            # Only adds where fuel_cost > 0 (active nodes), not where padding
            # leaves 0.
            _adder = _non_fuel_om_adder(_fuel_name, _fuel)
            if _adder <= 0:
                continue
            _existing_maint = list(getattr(_g, "maintenance_cost", None) or [])
            if _existing_maint and max(_existing_maint) >= _adder * 0.5:
                continue  # Already has substantial maintenance — don't double up
            _fixed = list(getattr(_g, "fixed_cost", None) or [])
            if _fixed and max(_fixed) > 0:
                continue  # Own fixed O&M ($/MW·yr): the adder would count it again
            _fc_now = list(getattr(_g, "fuel_cost", None) or [])
            _n_m = max(len(_existing_maint), len(_fc_now), 1)
            _new_maint = []
            for _i in range(_n_m):
                _e = _existing_maint[_i] if _i < len(_existing_maint) else 0.0
                _has_fuel = (_i < len(_fc_now) and _fc_now[_i] > 0)
                _new_maint.append(_e + (_adder if _has_fuel else 0.0))
            try:
                _g.maintenance_cost = _new_maint
                _linked_maint += 1
                log.info(
                    "Generator %s (fuel %s): added the non-fuel O&M adder of %g $/MWh "
                    "to maintenance_cost (now %s). Set a fixed_cost ($/MW·yr) or a "
                    "maintenance_cost of at least %g $/MWh to keep your own values.",
                    _gk, _fuel_name, _adder, _new_maint, _adder * 0.5,
                )
            except Exception as _exc:
                log.warning("Cannot set maintenance_cost on %s: %s", _g.name, _exc)

        if _linked_fuel > 0 or _linked_maint > 0:
            log.info(
                "Loader: linked fuel_cost for %d gens, added non-fuel LCOE for %d gens (Lazard 2024)",
                _linked_fuel, _linked_maint,
            )
        if _silent_freebies:
            log.warning(
                "Non-renewable generators with no valid fuel — LP would dispatch "
                "them as free with 0 emissions (silent bug). Affected (%d): %s",
                len(_silent_freebies),
                ", ".join(_silent_freebies[:10]) + (
                    f" … (+{len(_silent_freebies)-10} more)" if len(_silent_freebies) > 10 else ""
                ),
            )

    if isinstance(_techs_dict, dict) and _techs_dict:
        _link_technology_fuel_costs(
            _techs_dict, _fuels_dict if isinstance(_fuels_dict, dict) else {})

    _prefer_field_names(SystemConfig, system_data)
    system = SystemConfig(
        generators=generators,
        batteries=batteries,
        electrolyzers=electrolyzers,
        **system_data,
    )
    _warn_demandless_nodes(system)
    _warn_self_loop_branches(system)
    return system


def self_loop_branches(system: SystemConfig) -> list[str]:
    """Lines and transformers of *system* whose two ends are the same bus.
    The models drop them (``TransmissionDC``).

    As in the Studio validation, a line from a bus to itself whose two
    endpoint references name different elements (an equipment-to-bus link)
    is not counted. A transformer end is its bus, else its node (as the
    bridge resolves it).
    """
    def linked_elements_differ(ln) -> bool:
        ft, tt = ln.from_endpoint_type, ln.to_endpoint_type
        return bool(ft and tt) and (ft != tt or ln.from_endpoint_id != ln.to_endpoint_id)

    out = [f"line {ln.line_id or f'#{i}'}"
           for i, ln in enumerate(system.transmission_lines_geo or [])
           if ln.from_bus is not None and ln.from_bus == ln.to_bus
           and not linked_elements_differ(ln)]
    out += [f"transformer {tr.name}" for tr in system.transformers or []
            if (tr.from_node if tr.from_bus is None else tr.from_bus)
            == (tr.to_node if tr.to_bus is None else tr.to_bus)]
    return out


def _warn_self_loop_branches(system: SystemConfig) -> None:
    """One warning naming the branches that connect a bus to itself: they
    carry no flow, so the dispatch and the master ignore them."""
    loops = self_loop_branches(system)
    if loops:
        log.warning(
            "System %r: %d branch(es) connect a bus to itself (from_bus == "
            "to_bus) and are ignored by the model: %s. Connect them to two "
            "different buses or remove them.",
            system.name, len(loops),
            ", ".join(loops[:10]) + (f" … (+{len(loops) - 10} more)" if len(loops) > 10 else ""),
        )


def _warn_demandless_nodes(system: SystemConfig) -> None:
    """Point out nodes whose buses all have demand_fraction 0: the models
    place a node's demand only on its buses, so that demand is not served
    (nor counted as shed)."""
    if not system.buses:
        return
    totals: dict[int, float] = {}
    for b in system.buses:
        totals[b.parent_node] = totals.get(b.parent_node, 0.0) + b.demand_fraction
    zero = sorted(n for n, t in totals.items() if t <= 0)
    if zero:
        log.warning(
            "System %r: node(s) %s have buses but all with demand_fraction 0 "
            "(e.g. role 'connection'); their demand is dropped. Give one of "
            "their buses role 'load' or 'mixed' and a demand_fraction.",
            system.name, zero,
        )


# Unknown keys logged one per line up to this many; the rest are counted.
_UNKNOWN_KEYS_LOGGED = 50


def _warn_unknown_keys(keys: list[str]) -> None:
    """Log each configuration key nothing reads (misspelt or obsolete)."""
    for key in keys[:_UNKNOWN_KEYS_LOGGED]:
        log.warning("Unknown configuration key %s is ignored (misspelt or obsolete?)", key)
    if len(keys) > _UNKNOWN_KEYS_LOGGED:
        log.warning("... and %d more unknown configuration keys, also ignored",
                    len(keys) - _UNKNOWN_KEYS_LOGGED)


def _warn_ineffective_multipliers(config: ESFEXConfig) -> None:
    """Warn about stochastic multipliers this configuration gives no term
    to scale (see :func:`ineffective_scenario_multipliers`)."""
    for scenario, key, reason in ineffective_scenario_multipliers(config):
        log.warning("Stochastic scenario %r: multiplier %s has no effect: %s",
                    scenario, key, reason)


def load_config(path: Union[str, Path]) -> ESFEXConfig:
    """
    Load and validate a ESFEX configuration file.

    Supports both single-file configurations (with embedded systems)
    and references to external system files.

    Keys that nothing reads (misspelt or obsolete) do not stop the load: each
    is logged as a warning with its full path, and the list is kept in
    ``config.unknown_keys``.

    Args:
        path: Path to the main YAML configuration file

    Returns:
        Validated ESFEXConfig object

    Raises:
        ConfigLoadError: If loading or validation fails
    """
    path = Path(path)
    raw_config = load_yaml(path)

    try:
        # Systems are embedded or given as paths to their own files.
        systems_raw = raw_config.get("systems")
        if isinstance(systems_raw, dict):
            for sys_name, sys_data in list(systems_raw.items()):
                if isinstance(sys_data, str):
                    systems_raw[sys_name] = load_yaml(path.parent / sys_data)

        # Before the converters below consume the dicts.
        unknown = find_unknown_keys(raw_config)
        _warn_unknown_keys(unknown)

        if isinstance(systems_raw, dict):
            raw_config["systems"] = {
                sys_name: _convert_system(sys_data)
                for sys_name, sys_data in systems_raw.items()
            }

        # Convert temporal config
        if "temporal" in raw_config and isinstance(raw_config["temporal"], dict):
            raw_config["temporal"] = TemporalConfig(**raw_config["temporal"])

        # Convert solver config
        if "solver" in raw_config and isinstance(raw_config["solver"], dict):
            raw_config["solver"] = SolverConfig(**raw_config["solver"])

        # Convert N-1 security config
        if "n1_security" in raw_config and isinstance(raw_config["n1_security"], dict):
            raw_config["n1_security"] = N1SecurityConfig(**raw_config["n1_security"])

        # Convert master problem config
        if "master_problem" in raw_config and isinstance(raw_config["master_problem"], dict):
            raw_config["master_problem"] = MasterProblemConfig(**raw_config["master_problem"])

        # Convert meta_network config
        if "meta_network" in raw_config and isinstance(raw_config["meta_network"], dict):
            mn = raw_config["meta_network"]
            if "systems_links" in mn and isinstance(mn["systems_links"], list):
                links = []
                for link_data in mn["systems_links"]:
                    if isinstance(link_data, dict):
                        links.append(SystemLinkConfig(**link_data))
                mn["systems_links"] = links
            raw_config["meta_network"] = MetaNetworkConfig(**mn)

        # Validate with Pydantic
        config = ESFEXConfig(**raw_config)
        config._unknown_keys = unknown
        _warn_ineffective_multipliers(config)
        return config

    except ValidationError as e:
        raise ConfigLoadError(f"Configuration validation failed:\n{e}")
    except Exception as e:
        raise ConfigLoadError(f"Failed to load configuration: {e}")


def load_system_config(path: Union[str, Path]) -> SystemConfig:
    """
    Load a single system configuration file.

    Useful for loading individual system configs without the full
    ESFEXConfig wrapper.

    Args:
        path: Path to the system YAML file

    Returns:
        Validated SystemConfig object
    """
    raw_data = load_yaml(path)
    _warn_unknown_keys(find_unknown_system_keys(raw_data))

    try:
        return _convert_system(raw_data)
    except ValidationError as e:
        raise ConfigLoadError(f"System configuration validation failed:\n{e}")
