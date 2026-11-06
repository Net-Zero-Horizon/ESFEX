# Config Loader

Module: `esfex.config.loader`

Reads YAML configuration files and builds the [schema](config-schema.md) models. The CLI (`esfex run`, `esfex validate`) and runs started from the Studio all load configurations through `load_config()`.

## Functions

### load_config

```python
def load_config(path: Union[str, Path]) -> ESFEXConfig
```

Load and validate a complete ESFEX configuration file.

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `path` | `Union[str, Path]` | Path to the main YAML configuration file |

**Returns:** a validated `ESFEXConfig`. Keys the models do not define are dropped without an error.

**Raises:** `ConfigLoadError` for a missing file, invalid YAML, a Pydantic validation error (`"Configuration validation failed:\n..."`) or any other conversion error, including a missing required unit key (`"Failed to load configuration: 'min_up'"`).

**Processing steps:**

1. Read the file with `load_yaml()`.
2. For each entry under `systems`: if the value is a string, load that file (path relative to the main file); then convert the mapping with `_convert_system()`.
3. Build `TemporalConfig`, `SolverConfig`, `N1SecurityConfig`, `MasterProblemConfig` and `MetaNetworkConfig` (with its `SystemLinkConfig` list) from their mappings.
4. Validate everything as `ESFEXConfig(**raw_config)`; other top-level sections (`network_reduction`, `risk`, `logging`, `plugins`) are validated directly by the schema.

**Example:**

```python
from esfex.config.loader import load_config

config = load_config("isla_juventud.yaml")
print(config.simulation_mode)         # "development"
print(config.systems.keys())          # dict_keys(['isla_juventud'])
print(config.primary_system.name)     # first system in meta_network.systems

sys = config.primary_system
print(f"Generators: {len(sys.generators)}")
print(f"Batteries: {len(sys.batteries)}")
print(f"Technologies: {len(sys.technologies)}")
print(f"Nodes: {sys.num_nodes}, buses: {sys.num_buses}")
```

**Multi-file configuration:**

```yaml
# main.yaml
simulation_mode: development
temporal:
  resolution_hours: 1
systems:
  cuba: "systems/cuba.yaml"      # file reference
  jamaica: "systems/jamaica.yaml"
meta_network:
  systems: [cuba, jamaica]
```

```python
# File references are resolved relative to main.yaml
config = load_config("main.yaml")
cuba = config.get_system("cuba")
jamaica = config.get_system("jamaica")
```

### load_yaml

```python
def load_yaml(path: Union[str, Path]) -> dict[str, Any]
```

Low-level YAML reader (`yaml.safe_load`). Returns the raw dictionary without validation, or `{}` for an empty file.

**Raises:** `ConfigLoadError` if the file does not exist, cannot be read, or contains invalid YAML.

```python
from esfex.config.loader import load_yaml

raw = load_yaml("my_system.yaml")
print(raw.get("simulation_mode"))
```

### load_system_config

```python
def load_system_config(path: Union[str, Path]) -> SystemConfig
```

Load a single system file (the contents of one `systems` entry) through `_convert_system()`, without the `ESFEXConfig` wrapper.

**Raises:** `ConfigLoadError` on a Pydantic validation error. Other conversion errors (for example a missing required generator key, which raises `KeyError`) propagate unchanged.

```python
from esfex.config.loader import load_system_config

sys = load_system_config("systems/isla_juventud.yaml")
print(f"System: {sys.name}")
print(f"Generators: {list(sys.generators.keys())}")
print(f"Target RE: {sys.target_re_penetration:.0%}")
```

---

## System Conversion

`_convert_system(data)` converts one system mapping. Sections it does not handle explicitly (nodes are wrapped in `NodeConfig`, everything else such as `buses`, `transmission_lines_geo`, `development_zones`, `custom_constraints`, `outage_schedule`) go to `SystemConfig` unchanged.

| Function | Description |
|----------|-------------|
| `_convert_system(data)` | Convert a full system mapping to `SystemConfig` |
| `_convert_generator(key, data)` | Build a `GeneratorConfig` from a fixed key list |
| `_convert_battery(key, data)` | Build a `BatteryConfig` from a fixed key list |
| `_convert_fuels(data)` | Build `dict[str, FuelConfig]` |
| `_convert_dc_power_flow(data)` | Build `DCPowerFlowConfig` from `dc_power_flow` or legacy flat keys |
| `_convert_primary_energy_source(name, data)` | Build a `PrimaryEnergySourceConfig` from a fixed key list |

### Units: generators and batteries

Units are read from a `generators:` / `batteries:` mapping or from legacy system-level keys starting with `unit_` or `bat_`.

- A legacy `unit_*`/`bat_*` entry with `type: Storage` becomes a battery; `Renewable`, `Non-renewable` and `Electrolyzer` become generators. Any other type is logged (`Generator <key> has unknown type '<type>', treating as Non-renewable`) and replaced by `Non-renewable`.
- In a `generators:` mapping, any type other than `Renewable`, `Non-renewable`, `Storage` or `Electrolyzer` is likewise replaced by `Non-renewable`. This happens before the schema's own type normalisation, so lowercase or translated labels (`renewable`, `Renovable`) end up as `Non-renewable`.
- Every entry of a `batteries:` mapping is converted as a battery.

### What the unit converters read

`_convert_generator`, `_convert_battery` and `_convert_primary_energy_source` map the keys below explicitly (with the defaults shown), then copy every other key that is a field of the target model, by name or alias (`_pass_through`). Keys that are not schema fields are dropped.

| | Generators | Batteries |
|---|---|---|
| **Required** (missing → `KeyError`, reported as `Failed to load configuration: '<key>'`) | `type`, `life_time`, `initial_age`, `degradation_rate`, `decommissioning_cost`, `rated_power`, `min_power`, `min_up`, `min_down`, `ramp_up`, `ramp_down`, `eff_at_rated`, `eff_at_min`, `inertia`, `start_up_cost`, `fuel_cost`, `fixed_cost`, `maintenance_cost`, `invest_cost`, `invest_max_power` | the same list without `type`, plus `efficiency_charge`, `efficiency_discharge`, `soc_initial`, `max_DoD`, `capacity`, `MaxChargePower`, `MaxDischargePower` |
| **Optional** (default) | `name` (key), `fuel` (`"None"`), `technology`, `reservable` (true), `Availability`, `frequency_hz` (50), `current_type` (`AC`), `bus_id_per_node`, `bus_index`, `reservoir_capacity`, `reservoir_initial_level`, `reservoir_min_level`, `reservoir_max_level`, `reservoir_turbine_efficiency`, `reservoir_evaporation_rate`, `reservoir_pump_capacity`, `reservoir_pump_efficiency`, `reservoir_spillage_allowed`, `reservoir_invest_cost`, `reservoir_invest_max`, `reservoir_inflow_file` | `name` (key), `fuel` (`"None"`), `reservable` (true), `spillage` (true), `min_duration_hours`, `max_duration_hours`, `invest_cost_energy` (copy of `invest_cost`), `invest_max_capacity` (zeros), `current_type` (`DC`), `bus_id_per_node`, `bus_index` |
| **Copied when present** (any other schema field) | e.g. `fuel_cost_curve`, `droop`, `governor_time_const`, `q_max_mvar`, `q_min_mvar`, `power_factor`, `risk_coefficient`, `reservoir_min_release`, `cascade_downstream`, `cascade_delay_hours`, `reservoir_head_min_factor` | e.g. `technology`, `throughput_degradation_cost`, `discharge_cost_curve`, `risk_coefficient` |

`Availability` may also be written as `availability_file`.

### Other sections

- **Fuels:** `name`, `unit`, `emission_factor` (default 0), `energy_content`, `price_base` (default 0), `price_growth_rate`.
- **Primary energy sources:** `name`, `unit`, `max_availability`, `import_cost`, `storage_capacity`, `initial_storage_level`, `min_storage_level` (0.1), `storage_investment_cost`, `transport_cost`, `transport_losses`, `max_storage_investment_per_node`, `max_transport_investment_per_arc`, plus any other schema field when present (`transport_transit_days_per_100km`, `disruption_start_hour`, `disruption_end_hour`, `disruption_availability`).
- **Penalties:** keys are lower-cased before lookup, so `LOSS_OF_LOAD` and `loss_of_load` are the same. Read: `loss_of_load`, `loss_of_reserve_static`, `loss_of_reserve_dynamic`, `loss_of_inertia`, `transfer_margin` (also `TransferMargin`), `curtailment_cost`, `curtailment_excess_penalty`, `re_excess_penalty`, `max_curtailment_ratio`, `rooftop_curtailment`, `co2_cost`, `co2_budget_violation`, `fre_penetration_loss`, `ev_loss`, `loss_of_fuel_supply`, `transport_congestion`, `storage_violation`, `non_electric_demand_loss`, `soc_violation`, `delay_retirement_per_mw`, `coupling_slack_penalty`. `curtailment`, `coupling_slack_penalty`, `transport_congestion` and `storage_violation` load but have no effect (`PENALTIES_WITHOUT_EFFECT` in `config/schema.py`). `ev_loss` defaults to `loss_of_load` when absent.
- **DC power flow:** fields are taken from the nested `dc_power_flow` mapping. `base_impedance`, `reactance_per_km`, `voltage_level_kv`, `max_angle_diff_deg` and `slack_bus` fall back to flat system keys `dc_<field>` or `DC_<FIELD>`; those flat keys are then removed.
- **Electrolyzers:** a single `electrolyzer:` mapping becomes `electrolyzers["electrolyzer"]`; an `electrolyzers:` mapping is converted entry by entry.
- **Buses:** a `current_type` other than `AC`/`DC` is replaced by `AC` with a warning.
- **Technologies and battery technologies:** passed to `TechnologyConfig(**data)` / `BatteryTechnologyConfig(**data)` with `name` defaulting to the key, so every schema field (including `fuel_cost_curve` and `throughput_degradation_cost`) is read.
- **Demand sectors, EV categories, rooftop solar, stochastic scenarios, CO₂ budget:** passed to their models unchanged.

### Fuel-cost linking and non-fuel O&M adder

After the units are built, and only if the system has a non-empty `fuels` block, the loader adjusts each generator whose type is not `Renewable` and whose `fuel` is a key of `fuels`:

1. **Fuel cost.** If `fuel_cost` is missing or its maximum is ≤ 0, and the fuel has `price_base > 0` and `energy_content > 0`, each node's fuel cost becomes `price_base / energy_content / eff`, where `eff` is the unit's `eff_at_rated` at that node, or the linked technology's first `eff_at_rated` when the unit's is 0. Nodes with no usable efficiency get 0.
2. **Non-fuel adder.** A fixed non-fuel cost (described in the code as Lazard 2024 LCOE midpoints minus fuel) is added to `maintenance_cost`, by exact fuel name:

    | Fuel name | Adder ($/MWh) |
    |---|---|
    | `Gas`, `Diesel` | 60 |
    | `Oil`, `Fuel_oil` | 55 |
    | `Biomass` | 20 |

    The adder is applied at every node whose fuel cost is positive, and skipped for the whole unit if its largest `maintenance_cost` is already at least half the adder. It applies whether the fuel cost was given or computed in step 1. Other fuel names get no adder.

The loader logs (INFO) each unit that receives the adder, with the resulting `maintenance_cost`, and how many units were linked. Separately, it warns about non-renewable units whose fuel is `None`, empty or not in `fuels` and whose fuel cost is zero, because the model would dispatch them for free with zero emissions.

---

## Exceptions

### ConfigLoadError

```python
class ConfigLoadError(Exception):
    """Exception raised when configuration loading fails."""
```

Raised by `load_config()` when:

- the configuration file, or a referenced system file, does not exist;
- the YAML is malformed;
- Pydantic validation fails (type mismatch, missing required field, value out of range, failed cross-field validator);
- conversion fails for another reason, such as a required unit key missing.

Unknown keys never raise.

```python
from esfex.config.loader import load_config, ConfigLoadError

try:
    config = load_config("nonexistent.yaml")
except ConfigLoadError as e:
    print(e)   # Configuration file not found: nonexistent.yaml

try:
    config = load_config("invalid.yaml")
except ConfigLoadError as e:
    print(e)   # Configuration validation failed: ... / Failed to load configuration: ...
```

---

## Path Resolution

- `load_config()` resolves only system-file references, relative to the main configuration file.
- Demand, availability and inflow paths are stored as written and resolved when the run starts, by the `Orchestrator`, against the folder of the main configuration file (`config_path`). Demand paths fall back to the path as given (working directory or absolute). See [Demand Data](../user-guide/demand-data.md) and [Availability Profiles](../user-guide/availability-profiles.md) for the exact rules.
- File existence is therefore not checked by `load_config()` or `esfex validate`.
