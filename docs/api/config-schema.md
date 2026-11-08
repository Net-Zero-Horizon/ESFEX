# Config Schema

Module: `esfex.config.schema`

Pydantic v2 models for the ESFEX configuration. YAML files are normally loaded with [`load_config()`](config-loader.md), which converts some sections by hand before validating against these models. Field-by-field tables (types, defaults, units, and which fields the loader actually reads) are in the [Configuration Reference](../reference/config-reference.md).

General behaviour:

- **Extra keys are ignored.** No model sets `extra="forbid"`, so unknown keys are dropped without an error. [`load_config()`](config-loader.md) reports them: it logs each one and keeps the list in `ESFEXConfig.unknown_keys`.
- **Aliases.** `SystemConfig`, `GeneratorConfig`, `BatteryConfig`, `TechnologyConfig`, `BatteryTechnologyConfig` and `SystemLinkConfig` set `populate_by_name=True` and accept both the field name and its alias (for example `target_re_penetration` and `TARGET_RE_PENETRATION`, `availability_file` and `Availability`).
- **Per-node lists.** Fields typed `list[...]` on equipment hold one value per node. Their length is not validated.

## Model Hierarchy

```
ESFEXConfig
 ├── TemporalConfig
 ├── SolverConfig
 ├── NetworkReductionConfig
 ├── N1SecurityConfig
 ├── MasterProblemConfig
 │    └── MGAConfig ── SporesObjective
 ├── MetaNetworkConfig
 │    └── SystemLinkConfig
 ├── RiskConfig
 │    ├── ClimateScenarioConfig, HazardScenarioConfig
 │    └── VOLLConfig
 ├── LoggingConfig
 └── systems: dict[str, SystemConfig]
      ├── NodeConfig ── GeoCoordinate
      ├── BusConfig
      ├── TransmissionLineGeo ── GeoCoordinate (waypoints)
      ├── TransformerConfig
      ├── ACDCConverterConfig
      ├── FrequencyConverterConfig
      ├── DCPowerFlowConfig
      ├── ACPowerFlowConfig
      ├── GeneratorConfig ── CostCurveConfig ── CostCurveBlock
      ├── BatteryConfig ── CostCurveConfig
      ├── TechnologyConfig ── CostCurveConfig
      ├── BatteryTechnologyConfig
      ├── ElectrolyzerConfig
      ├── FuelConfig
      ├── PrimaryEnergySourceConfig
      ├── ConversionTechnologyConfig
      ├── FuelEntryPointConfig
      ├── FuelInfrastructureConfig
      ├── PenaltiesConfig
      ├── CO2BudgetConfig
      ├── CriticalityPenalties
      ├── DemandSectorConfig
      ├── NonElectricDemandConfig
      ├── EVCategoryConfig
      ├── RooftopSolarConfig
      ├── StochasticScenarioConfig ── ScenarioMultipliers
      ├── DevelopmentZoneConfig
      ├── CustomConstraintConfig ── ConstraintTerm
      └── OutageWindow
```

---

## ESFEXConfig

Top-level container.

```python
class ESFEXConfig(BaseModel):
    simulation_mode: Literal["development", "unit_commitment", "economic_dispatch"] = "development"
    unit_commitment_hours: int = 24          # >= 1
    date_start: str = "01/01/2025 00:00"     # DD/MM/YYYY HH:MM
    temporal: TemporalConfig = TemporalConfig()
    solver: SolverConfig = SolverConfig()
    network_reduction: NetworkReductionConfig = NetworkReductionConfig()
    n1_security: N1SecurityConfig = N1SecurityConfig()
    master_problem: MasterProblemConfig = MasterProblemConfig()
    enable_primary_energy: bool = True
    meta_network: MetaNetworkConfig          # required
    systems: dict[str, SystemConfig]         # required
    plugins: dict[str, Any] = {}
    risk: RiskConfig = RiskConfig()
    logging: LoggingConfig = LoggingConfig()
```

| Field | Default | Description |
|-------|---------|-------------|
| `simulation_mode` | `"development"` | `development`: master problem then dispatch. `economic_dispatch`: LP dispatch only. `unit_commitment`: MIP dispatch of the first `unit_commitment_hours` hours of the first year |
| `date_start` | `"01/01/2025 00:00"` | Timestamp of the first data row. Labels the hourly time index (Feb 29 skipped) and gives the first year label |
| `enable_primary_energy` | `True` | Include the fuel supply chain |

**Validator:** every name in `meta_network.systems` must be a key of `systems` (`ValueError: System 'x' referenced in meta_network but not defined in systems`).

**Properties and methods:**

- `primary_system -> SystemConfig`: the first system listed in `meta_network.systems`.
- `get_system(name: str) -> SystemConfig`: raises `KeyError` if the system does not exist.

---

## SystemConfig

One power system.

```python
class SystemConfig(BaseModel):
    model_config = {"populate_by_name": True}

    name: str
    demand_path: Optional[str] = None
    demand_paths: Optional[list[str]] = None     # one file per node, overrides demand_path
    demand_scale: float = 1.0                    # > 0
    target_re_penetration: float = 1.0           # alias TARGET_RE_PENETRATION
    discount_rate: float = 0.05
    max_annual_system_cost: float = 20e9         # alias MAX_ANNUAL_SYSTEM_COST
    power_flow_mode: Literal["dcopf", "dcopf_ac_verify", "acopf_soc", "acopf_qc",
                             "acopf_sdp", "acopf_polar", "acopf_rect"] = "dcopf"
    nodes: NodeConfig                            # required
    buses: list[BusConfig] = []
    generators: dict[str, GeneratorConfig] = {}
    batteries: dict[str, BatteryConfig] = {}
    technologies: dict[str, TechnologyConfig] = {}
    battery_technologies: dict[str, BatteryTechnologyConfig] = {}
    penalties: PenaltiesConfig = PenaltiesConfig()
    ...
```

Legacy uppercase aliases: `SIM_ROOFTOP`, `TARGET_RE_PENETRATION`, `MIN_ANNUAL_INCREMENT`, `MAX_ANNUAL_INCREMENT`, `MAX_ANNUAL_SYSTEM_COST`, `MAX_NPV_PENALTY_PER_MW`, `MAX_DECOMMISSION_COST_PER_MW`, `FORCE_REPLACEMENT`, `INERTIA_LIMIT_THRESHOLD`, `EV_initial_soc`, and the Studio metadata `_gui_layout`, `_gui_styles`, `_technologies`, `_equipment_buses`.

**Properties:**

- `num_nodes -> int`: `nodes.num_nodes`.
- `num_buses -> int`: `len(buses)`, or `num_nodes` when `buses` is empty. In practice `buses` is never empty after validation, because `_ensure_buses` creates one bus per node.

**Validators (run after field validation, in this order):**

| Validator | Effect |
|---|---|
| `_ensure_buses` | If `buses` is empty, creates `BusConfig(bus_id=f"bus_{i}", name=f"Bus {i}", parent_node=i, demand_fraction=1.0)` for each node (role `load`, 220 kV) |
| `_normalize_demand_fractions` | Rescales each node's bus `demand_fraction` values to sum to 1 when they sum to anything else above 0 (logs a warning) |
| `_resolve_outage_unit_ids` | Generator/battery outage windows whose `element_id` is `<unit key>_n<node>` are rewritten to that unit key and `node`; windows for unknown units log a warning |
| `_validate_unique_bus_ids` | `ValueError` if two buses share a `bus_id` |
| `_validate_stochastic_probabilities` | `ValueError` if `stochastic_scenarios` probabilities do not sum to 1 (±0.01) |

---

## NodeConfig

```python
class NodeConfig(BaseModel):
    num_nodes: Optional[int] = None               # inferred from nodes_connections
    nodes_connections: list[float]                # flattened N×N adjacency (MW), required
    reserve_static: list[float] = []              # MW per node
    reserve_dynamic: list[float] = []             # MW per node
    reserve_duration: list[int] = []              # hours per node
    losses: list[float] = []                      # fraction per node
    transference_invest_cost: list[float] = []    # $/MW per node
    transference_invest_max: list[float] = []     # MW per node
    node_coordinates: Optional[list[GeoCoordinate]] = None
    node_names: Optional[list[str]] = None
```

**Validator `_set_defaults_from_num_nodes`:** sets `num_nodes = int(sqrt(len(nodes_connections)))` when it is `None`, and fills empty per-node lists with zeros (`reserve_duration` with ones).

---

## BusConfig

An electrical bus inside a node. A node is a geographic aggregation; buses carry the electrical network, and there are always at least as many buses as nodes.

```python
class BusConfig(BaseModel):
    bus_id: Optional[str] = None
    name: str = ""
    parent_node: int = 0                         # >= 0
    voltage_kv: float = 220.0                    # > 0
    frequency_hz: float = 50.0
    current_type: Literal["AC", "DC"] = "AC"
    bus_type: Literal["PQ", "PV", "slack"] = "PQ"
    role: Literal["connection", "load", "mixed"] = "load"
    demand_fraction: float = 1.0
```

**Validator `_validate_role_consistency`:** `role="connection"` requires `demand_fraction == 0`; `role` `load` or `mixed` requires `demand_fraction >= 0`.

In the model, a `load`/`mixed` bus carries `node_demand × demand_fraction` (each node's fractions are rescaled to sum to 1), a `connection` bus carries no demand, and load shedding on each bus is bounded by the demand on that bus. See [Nodes and buses](../user-guide/configuration.md#nodes-and-buses).

---

## GeneratorConfig

Existing generating unit. Per-node lists have one entry per node.

```python
class GeneratorConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    type: Literal["Renewable", "Non-renewable", "Storage", "Electrolyzer"]
    fuel: str
    technology: Optional[str] = None
    reservable: bool = True
    life_time: list[int]
    initial_age: list[int]
    degradation_rate: list[float]          # fraction/year
    decommissioning_cost: list[float]      # $/MW
    rated_power: list[float]               # MW
    min_power: list[float]                 # fraction of rated
    min_up: list[int]                      # hours
    min_down: list[int]                    # hours
    ramp_up: list[float]                   # fraction of capacity per hour
    ramp_down: list[float]
    eff_at_rated: list[float]
    eff_at_min: list[float]
    inertia: list[float]                   # H (s)
    start_up_cost: list[float]             # $ per start
    fuel_cost: list[float]                 # $/MWh
    fixed_cost: list[float]                # $/MW per year (fixed O&M)
    maintenance_cost: list[float]          # $/MWh
    invest_cost: list[float] = [0.0]       # $/MW, prices life extension
    availability_file: Optional[str] = None   # alias "Availability"
    bus_index: Optional[int] = None
    bus_id_per_node: Optional[dict[int, str]] = None
    # reservoir_* fields, fuel_cost_curve, droop, governor_time_const,
    # q_max_mvar, q_min_mvar, power_factor, risk_coefficient, cascade_* ...
```

**Validator `_normalize_type` (mode `before`):** calls `canonical_unit_type()`, which ignores case and surrounding spaces and maps localized labels (`renovable`, `renouvelable`, `no renovable`, ...) to `Renewable` / `Non-renewable` (also `storage` → `Storage`, `electrolyzer` → `Electrolyzer`). Unknown values are returned unchanged and rejected by the `Literal`. `load_config()` applies the same function first and only then replaces a type that is still unknown (e.g. `Thermal`) with `Non-renewable`, with a warning. `TechnologyConfig` uses the same validator.

Every field above can be set from YAML; see [Config Loader](config-loader.md#what-the-unit-converters-read).

---

## BatteryConfig

```python
class BatteryConfig(BaseModel):
    name: str
    type: Literal["Storage"] = "Storage"
    fuel: str = "None"
    technology: Optional[str] = None       # battery technology pricing life extension
    reservable: bool = True
    spillage: bool = True
    min_duration_hours: Optional[int] = None
    max_duration_hours: Optional[int] = None
    capacity: list[float]                  # MWh per node
    MaxChargePower: list[float]            # MW
    MaxDischargePower: list[float]         # MW
    efficiency_charge: list[float]
    efficiency_discharge: list[float]
    soc_initial: list[float]               # fraction
    max_DoD: list[float]                   # fraction
    throughput_degradation_cost: Optional[list[float]] = None
    discharge_cost_curve: Optional[list[CostCurveConfig]] = None
    ramp_up: list[float] = [0.0]           # fraction of rated power per hour, 0 = no limit
    ramp_down: list[float] = [0.0]
    fixed_cost: list[float] = [0.0]        # $/MW of MaxDischargePower per year
    availability_file: Optional[str] = None   # alias "Availability"
    bus_index: Optional[int] = None
    bus_id_per_node: Optional[dict[int, str]] = None
    # plus the per-node lists life_time, initial_age, degradation_rate,
    # decommissioning_cost, rated_power, inertia, maintenance_cost,
    # invest_cost (prices life extension), invest_max_power (> 0 places the
    # battery at the node in the master's representative days)
```

---

## TechnologyConfig

Candidate generation technology for the master problem.

```python
class TechnologyConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    type: Literal["Renewable", "Non-renewable"]
    fuel: str
    invest_cost: list[float]               # $/MW per node
    invest_max_power: list[float]          # MW per node
    eff_at_rated: list[float]
    degradation_rate: list[float]
    lifetime: int                          # years (scalar)
    availability_file: Optional[str] = None   # alias "Availability"
    fuel_cost: list[float] = [0.0]
    fuel_cost_curve: Optional[list[CostCurveConfig]] = None
    fixed_cost: list[float] = [0.0]        # $/MW built per year
    maintenance_cost: list[float] = [0.0]
    ...
```

---

## BatteryTechnologyConfig

Candidate storage technology for the master problem.

```python
class BatteryTechnologyConfig(BaseModel):
    name: str
    invest_cost_power: list[float]         # $/MW per node
    invest_cost_energy: list[float]        # $/MWh per node
    invest_max_power: list[float]          # MW per node
    invest_max_capacity: list[float]       # MWh per node
    min_duration_hours: float = 1.0
    max_duration_hours: float = 24.0
    efficiency_charge: list[float]
    efficiency_discharge: list[float]
    degradation_rate: list[float]
    lifetime: int
    soc_initial: list[float] = [0.5]
    max_DoD: list[float] = [0.9]
    throughput_degradation_cost: list[float] = [1.0]   # $/MWh discharged
    ramp_up: list[float] = [0.0]           # of the batteries it builds, as BatteryConfig
    ramp_down: list[float] = [0.0]
    fixed_cost: list[float] = [0.0]        # $/MW built per year
    reservable: bool = True
    availability_file: Optional[str] = None   # alias "Availability"
    ...
```

---

## CostCurveConfig

Offer curve for one node.

```python
class CostCurveConfig(BaseModel):
    curve_type: Literal["flat", "linear", "stepwise", "exponential"] = "flat"
    flat_price: Optional[float] = None
    blocks: Optional[list[CostCurveBlock]] = None   # CostCurveBlock(fraction, price)
    price_at_zero: Optional[float] = None
    price_at_max: Optional[float] = None
    base_price: Optional[float] = None
    scale_factor: Optional[float] = None
    num_segments: int = 5                            # 2-20
```

```python
def normalize_cost_curve(curve: CostCurveConfig, fallback_price: float = 0.0) -> list[CostCurveBlock]
```

Converts any curve to stepwise blocks whose fractions sum to 1. Missing prices fall back to `fallback_price` (the scalar `fuel_cost`); a missing `scale_factor` falls back to 1.0. Stepwise fractions that do not sum to 1 (±0.01) are rescaled. Linear and exponential curves become `num_segments` equal blocks priced at the curve value at each block's mid-point.

---

## TemporalConfig

```python
class TemporalConfig(BaseModel):
    resolution_hours: int = 1             # >= 1
    rolling_horizon_hours: int = 48
    overlap_hours: int = 24               # < rolling_horizon_hours
    investment_resolution: int = 8760     # hours per investment period
    primary_energy_resolution: int = 24
    use_rolling_horizon: bool = True
```

---

## SolverConfig

```python
class SolverConfig(BaseModel):
    name: Literal["highs", "cbc", "glpk", "gurobi", "cplex", "scip", "xpress",
                  "clarabel", "scs", "ipopt"] = "highs"
    threads: int = 4
    time_limit: int = 10800            # seconds, 0 = unlimited
    gap: float = 0.01                  # MIP optimality gap
    verbose: bool = False
    options: dict[str, Any] = {}       # solver-specific options
```

---

## NetworkReductionConfig

```python
class NetworkReductionConfig(BaseModel):
    enabled: bool = False
    kron_deg3: bool = False
```

## LoggingConfig

```python
class LoggingConfig(BaseModel):
    console_level: Literal["basic", "verbose", "debug"] = "basic"
```

---

## PenaltiesConfig

```python
class PenaltiesConfig(BaseModel):
    loss_of_load: float = 10e6                 # $/MWh unserved
    loss_of_reserve_static: float = 100
    loss_of_reserve_dynamic: float = 100
    loss_of_inertia: float = 200
    transfer_margin: float = 100
    max_curtailment_ratio: float = 0.05        # 0-1
    curtailment_cost: float = 20.0             # $/MWh, operational dispatch
    rooftop_curtailment: float = 5
    co2_cost: float = 10                       # $/tCO2
    co2_budget_violation: float = 500
    fre_penetration_loss: float = 100
    ev_loss: Optional[float] = None          # None -> loss_of_load
    loss_of_fuel_supply: float = 100
    non_electric_demand_loss: float = 100
    soc_violation: float = 1e6
```

The loader reads every field, with keys matched case-insensitively; any other key under `penalties` is reported as an unknown configuration key.

---

## MasterProblemConfig

```python
class MasterProblemConfig(BaseModel):
    stochastic: bool = False
    representative_days: int = 5
    min_day_separation: int = 5                  # days
    solver_method: Literal["monolithic", "benders"] = "monolithic"
    benders_max_iterations: int = 50
    benders_tolerance: float = 1e-4
    use_tsam: bool = False
    tsam_num_periods: int = 10
    tsam_method: Literal["kmedoids", "kmeans"] = "kmedoids"
    tsam_inter_period_linking: bool = True
    use_uc_in_dispatch: bool = False             # UC in development-mode dispatch
    planning_mode: Literal["perfect_foresight", "myopic"] = "perfect_foresight"
    mga: MGAConfig = MGAConfig()
```

---

## MGAConfig

Configuration for near-optimal alternative generation. Two methods share most fields; the cross-field validator enforces that `objectives` and `method` agree (see [Validation](#mgaconfig-validation)).

```python
class MGAConfig(BaseModel):
    enabled: bool = False
    method: Literal["mga", "spores"] = "mga"
    objectives: list[SporesObjective] = []      # SPORES only
    num_alternatives: int = 10                  # 1-100, MGA only
    slack_fraction: float = 0.05                # 0-0.5
    investment_threshold: float = 0.1
```

### MGAConfig validation

A `@model_validator(mode="after")` activates when `enabled = True`:

| Condition | Outcome |
|-----------|---------|
| `method = "spores"` and `objectives` empty | `ValueError` (choose at least one objective) |
| `method = "mga"` and `objectives` non-empty | `ValueError` (objectives only valid with SPORES) |
| `enabled = False` | validator is bypassed (YAML drafts can keep both populated) |

## SporesObjective

```python
class SporesObjective(str, Enum):
    HSJ_DIVERSITY       = "hsj_diversity"
    MIN_TOTAL_BUILD     = "min_total_build"
    MAX_TECH_EQUITY     = "max_tech_equity"
    MAX_REGIONAL_EQUITY = "max_regional_equity"
    EVOLUTIONARY_DIST   = "evolutionary_dist"
```

The lowercase snake_case values match the Julia `Symbol` keys used by `apply_spores_objective!` in `mga.jl` (e.g. `"min_total_build"` $\leftrightarrow$ `:min_total_build`) so round-tripping through YAML, the Pydantic schema, and the bridge layer is lossless. See [Capacity Expansion §15.10–§15.13](../formulation/capacity-expansion.md#1510-minimum-total-build-objective) for the LP formulation of each objective.

---

## EVCategoryConfig

```python
class EVCategoryConfig(BaseModel):
    battery_capacity: float             # kWh per vehicle
    charging_power: float               # kW per vehicle
    v2g_power: float                    # kW per vehicle for V2G
    v2g_participation: float            # fraction (0-1)
    efficiency_charge: float            # 0-1
    efficiency_discharge: float         # 0-1
    min_soc: float                      # 0-1
    max_adoption: float = 35.0          # S-curve ceiling (fleet multiplier)
    growth_rate: float = 0.14           # logistic growth rate
    mid_point_fraction: float = 0.5     # S-curve midpoint position
    daily_energy_kwh: Optional[float] = None   # default: battery_capacity × 0.12
```

---

## DCPowerFlowConfig

```python
class DCPowerFlowConfig(BaseModel):
    reactance_per_km: float = 0.4       # Ohm/km
    voltage_level_kv: float = 220.0     # kV
    max_angle_diff_deg: float = 30.0    # degrees (0-90), AC-OPF only
    slack_bus: int = 0                  # 0-indexed
    loss_model: Literal["none", "linear", "pwl"] = "pwl"
    pwl_loss_segments: int = 3          # operational dispatch (1-10)
    pwl_loss_segments_master: int = 2   # master problem (1-5)
```

There is no `enabled` or `enable_angle_limits` field; the network formulation is selected by `SystemConfig.power_flow_mode`.

---

## Other Configuration Classes

### N1SecurityConfig

```python
class N1SecurityConfig(BaseModel):
    enabled: bool = False
    apply_to_modes: list[str] = ["development", "unit_commitment"]
    transmission_enabled: bool = True
    transmission_reserve_factor: float = 0.70
    critical_line_threshold: float = 0.0
    generation_enabled: bool = True
    generation_reserve_type: Literal["largest_unit", "percentage"] = "largest_unit"
    generation_reserve_percentage: float = 0.15
    scopf_enabled: bool = False
    corrective_enabled: bool = False
    compute_security_cost: bool = False
    contingency_depth: Literal["n1", "n1_1"] = "n1"
    redistribution_mode: Literal["pro_rata", "droop"] = "pro_rata"
    pi_screening_threshold: float = 0.0
    transformer_contingencies: bool = False
    battery_contingencies: bool = False
```

### MetaNetworkConfig

```python
class MetaNetworkConfig(BaseModel):
    systems: list[str]                          # system names, required
    systems_links: list[SystemLinkConfig] = []
```

### StochasticScenarioConfig

```python
class StochasticScenarioConfig(BaseModel):
    name: str
    probability: float                  # 0-1
    description: str = ""
    multipliers: ScenarioMultipliers = ScenarioMultipliers()
```

### RooftopSolarConfig

```python
class RooftopSolarConfig(BaseModel):
    adoption_scenario: Literal["low", "medium", "high"] = "medium"
    weather_variability: Literal["low", "normal", "high"] = "normal"
    simulation_seed: int = 42
    systems_per_node: Optional[list[Optional[int]]] = None    # passed to rooftex; null entry = rooftex default
    avg_system_size: Optional[list[Optional[float]]] = None   # kW per system; passed to rooftex
    performance_ratio: float = 0.75                   # passed to rooftex
    initial_adoption: Optional[list[Optional[float]]] = None  # passed to rooftex
    max_adoption: Optional[dict[str, float]] = None   # per scenario; passed to rooftex
    adoption_rates: Optional[dict[str, float]] = None # per scenario; passed to rooftex
    ...
```

### ElectrolyzerConfig

```python
class ElectrolyzerConfig(BaseModel):
    name: str
    type: Literal["Electrolyzer"] = "Electrolyzer"
    fuel: str = "Hydrogen"
    energy_per_kg_h2: float = 50.0      # kWh/kg H2
    water_cost: float = 0.001           # $/kg H2
    ...                                 # per-node lists, all required
```

### OutageWindow

```python
class OutageWindow(BaseModel):
    element_type: Literal["generator", "line", "battery", "transformer", "acdc_converter",
                          "freq_converter", "electrolyzer", "fuel_source",
                          "fuel_storage", "fuel_route"]
    element_id: str
    node: Optional[int] = None          # generators / batteries: only this node
    start_hour: int                     # inclusive, >= 0; horizon hour (0 = Jan 1 of base_year)
    end_hour: int                       # exclusive; validator: end_hour > start_hour
    availability: float = 0.0           # 0 = full outage
    label: str = ""
    recurring: bool = False             # repeat every year (start_hour modulo 8760)
```

### CustomConstraintConfig

```python
class CustomConstraintConfig(BaseModel):
    name: str
    type: str = "linear"
    terms: list[ConstraintTerm] = []    # validator: required when type == "linear"
    sense: Literal["<=", ">=", "=="] = "<="
    rhs: float = 0.0
    target: Literal["operational", "investment"] = "operational"
    params: dict[str, Any] = {}
```

---

## Validators

| Model | Rule |
|---|---|
| `BusConfig` | `connection` → `demand_fraction == 0`; `load`/`mixed` → `demand_fraction >= 0` |
| `NodeConfig` | infers `num_nodes`, fills empty per-node lists |
| `FuelEntryPointConfig` | keeps `fuel` and `fuels` consistent |
| `SystemConfig` | creates one bus per node if `buses` is empty; rescales each node's `demand_fraction` values to sum to 1; unique `bus_id`; stochastic probabilities sum to 1 |
| `ESFEXConfig` | systems in `meta_network` exist |
| `MGAConfig` | `method` and `objectives` agree (when enabled) |
| `RiskConfig` | climate scenario probabilities sum to 1 |
| `CustomConstraintConfig` | linear constraints have at least one term |
| `OutageWindow` | `end_hour > start_hour` |
| `GeneratorConfig.type` | maps localized labels to English literals |

Field constraints (`ge`, `le`, `gt`) and `Literal` types are checked by Pydantic and reported as `ValidationError`.

---

## Validation Example

```python
from pydantic import ValidationError
from esfex.config.schema import BusConfig, NodeConfig, SystemConfig

nodes = NodeConfig(nodes_connections=[0.0, 80.0, 80.0, 0.0])
print(nodes.num_nodes)            # 2

system = SystemConfig(name="island", nodes=nodes)
print([b.bus_id for b in system.buses])   # ['bus_0', 'bus_1'] (auto-created)
print(system.num_buses)           # 2

try:
    BusConfig(bus_id="plant", role="connection", demand_fraction=0.5)
except ValidationError as e:
    print(e)   # role='connection' requires demand_fraction=0 ...
```

See the [Config Reference](../reference/config-reference.md) for every field with its default and YAML notes.
