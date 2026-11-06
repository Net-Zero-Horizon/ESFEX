# EV Analysis
Module: `esfex.models.ev_analysis`

This module re-exports the external `evrex` package; the names below are importable from both `esfex.models.ev_analysis` and `evrex`.

Provides the computational engine for steps 3–4 (Charging & V2G, Grid & Integration) of the EV & V2G Assessment Workflow: charging demand characterization, V2G potential assessment, battery degradation modeling, and grid impact analysis.

---

## Data Structures

### ChargingProfile

24-hour charging demand profile for one vehicle category and scenario.

| Field | Type | Description |
|-------|------|-------------|
| `category` | `str` | Vehicle category (light, medium, heavy, buses) |
| `scenario` | `str` | `"uncontrolled"`, `"tou_shifted"`, or `"optimized"` |
| `hourly_mw` | `list[float]` | 24 values — charging demand per hour (MW) |
| `description` | `str` | Short label, e.g. `"light (uncontrolled): 1000 vehicles x 7 kW"` |

### ChargingScenarioResult

Aggregate charging demand for a complete scenario.

| Field | Type | Description |
|-------|------|-------------|
| `scenario` | `str` | Scenario name |
| `profiles_by_category` | `dict[str, ChargingProfile]` | Per-category profiles |
| `aggregate_hourly_mw` | `list[float]` | 24 values — total demand (MW) |
| `peak_demand_mw` | `float` | Maximum hourly demand |
| `daily_energy_mwh` | `float` | Total daily energy consumption |

### V2GPotential

Hourly V2G capacity and energy availability.

| Field | Type | Description |
|-------|------|-------------|
| `hourly_connected_fraction` | `list[float]` | 24 values — fraction of fleet plugged in |
| `max_v2g_power_mw` | `list[float]` | 24 values — max discharge power (MW) |
| `hourly_available_soc_mwh` | `list[float]` | 24 values — available energy in SOC window |
| `daily_v2g_energy_mwh` | `float` | Total daily V2G energy potential |
| `annual_v2g_potential_gwh` | `float` | Annualized V2G potential |
| `degradation_cost_per_kwh` | `float` | Placeholder, left at 0 by `compute_v2g_potential` (see `DegradationResult`) |
| `breakeven_compensation` | `float` | Placeholder, left at 0 by `compute_v2g_potential` ($/MWh) |

### DegradationResult

Battery degradation analysis output.

| Field | Type | Description |
|-------|------|-------------|
| `chemistry` | `str` | `"NMC"` or `"LFP"` |
| `cycles_per_day` | `float` | Average V2G cycles per day |
| `depth_of_discharge` | `float` | Average DoD for V2G cycling |
| `cycle_life_remaining_pct` | `float` | `100 - total_degradation_pct_per_year`, floored at 0 |
| `calendar_aging_pct_per_year` | `float` | Calendar aging component (%/year) |
| `total_degradation_pct_per_year` | `float` | Total capacity loss (%/year) |
| `degradation_cost_per_kwh` | `float` | Cost per kWh cycled ($/kWh) |
| `breakeven_compensation` | `float` | Break-even V2G rate ($/MWh) |

### GridImpactResult

Grid impact assessment results.

| Field | Type | Description |
|-------|------|-------------|
| `base_demand_24h` | `list[float]` | Base system demand (MW) |
| `ev_charging_24h` | `list[float]` | EV charging demand (MW) |
| `v2g_discharge_24h` | `list[float]` | V2G dispatch (MW) |
| `net_load_24h` | `list[float]` | Net = base + EV - V2G |
| `peak_shaving_mw` | `float` | Peak reduction from V2G |
| `valley_filling_mw` | `float` | Valley filling increase |
| `peak_to_valley_before` | `float` | Peak/valley ratio of base demand |
| `peak_to_valley_after` | `float` | Peak/valley ratio of net load |
| `re_curtailment_reduction_pct` | `float` | Estimated RE curtailment reduction: V2G energy as % of base energy, capped at 15 % |
| `frequency_regulation_mw` | `float` | 50 % of mean V2G power (MW) |
| `arbitrage_revenue_annual` | `float` | Annual arbitrage revenue ($) |
| `avoided_reinforcement` | `float` | Avoided grid reinforcement: peak reduction × reinforcement cost per MW ($) |
| `net_v2g_value` | `float` | Total V2G program value: arbitrage + avoided reinforcement + V2G compensation ($) |

### DEFAULT_CONNECTED_PROFILE

24 hourly values of the fraction of the fleet plugged in (0.85 at midnight, 0.25 at mid-morning), used by `compute_v2g_potential` when no connected profile is passed.

---

## Charging Demand Functions

### generate_charging_profiles

```python
def generate_charging_profiles(
    fleet_by_category: dict[str, int],
    ev_categories: dict[str, dict],
    scenario: str = "uncontrolled",
    smart_charging_fraction: float = 0.0,
    base_demand_24h: list[float] | None = None,
) -> ChargingScenarioResult
```

Generate 24-hour charging demand profiles for a given scenario.

**Scenarios:**

| Scenario | Pattern | Description |
|----------|---------|-------------|
| `uncontrolled` | Evening peak | Charge immediately on plug-in. Peak 18:00-22:00 for light vehicles. |
| `tou_shifted` | Night off-peak | Respond to time-of-use tariff signals. Peak 23:00-06:00. |
| `optimized` | Valley filling | Smart charging fills demand valleys to flatten net load. Blends smart and uncontrolled fractions. |

**Charging demand per category:**

```
hourly_mw[h] = pattern[h] * vehicle_count * charging_power_kW / 1000
```

Patterns are empirical 24-hour profiles based on literature for each vehicle category and scenario.

### generate_all_scenarios

```python
def generate_all_scenarios(
    fleet_by_category: dict[str, int],
    ev_categories: dict[str, dict],
    smart_charging_fraction: float = 0.5,
    base_demand_24h: list[float] | None = None,
) -> dict[str, ChargingScenarioResult]
```

Generate all three scenarios at once. Returns a dict keyed by scenario name.

---

## V2G Potential

### compute_v2g_potential

```python
def compute_v2g_potential(
    fleet_by_category: dict[str, int],
    ev_categories: dict[str, dict],
    connected_profile: list[float] | None = None,
    v2g_min_soc: float = 0.30,
    v2g_max_soc: float = 0.90,
) -> V2GPotential
```

Compute hourly V2G discharge capacity and available energy.

**V2G power per hour:**

```
n_v2g = count * connected_fraction[h] * v2g_participation
power_mw = n_v2g * v2g_power_kW * efficiency / 1000
```

**Default connected-time profile**: High at night (0.85-0.90), low during commute (0.25-0.30), medium evening (0.55-0.82).

---

## Battery Degradation

### compute_battery_degradation

```python
def compute_battery_degradation(
    v2g_cycles_per_day: float = 0.5,
    battery_capacity_kwh: float = 50.0,
    depth_of_discharge: float = 0.30,
    chemistry: str = "NMC",
    battery_cost_per_kwh: float | None = None,
) -> DegradationResult
```

Wohler-type battery degradation model:

```
equivalent_cycles = actual_cycles * (DoD / ref_DoD) ^ exponent
cycle_degradation = (annual_eq_cycles / ref_cycles) * 20%
total_degradation = cycle_degradation + calendar_aging
```

**Chemistry parameters:**

| Chemistry | Cycles at 80% DoD | Wohler Exponent | Calendar Aging |
|-----------|-------------------|-----------------|----------------|
| NMC | 2000 | 1.5 | 2.5%/year |
| LFP | 4000 | 1.2 | 1.5%/year |

**Break-even compensation**: The $/MWh rate at which V2G revenue exactly offsets degradation cost.

---

## Grid Impact Assessment

### assess_grid_impact

```python
def assess_grid_impact(
    base_demand_24h: list[float],
    ev_charging_24h: list[float],
    v2g_potential: V2GPotential,
    electricity_prices_24h: list[float] | None = None,
    v2g_compensation_per_mwh: float = 50.0,
    grid_reinforcement_cost_per_mw: float = 500000.0,
) -> GridImpactResult
```

Comprehensive grid impact analysis:

1. **V2G dispatch**: Discharged during the 8 most expensive hours per day.
2. **Peak shaving**: Reduction in system peak from V2G discharge.
3. **Valley filling**: Increase in minimum load from smart EV charging.
4. **Arbitrage revenue**: V2G discharge × electricity price during high-price hours.
5. **Avoided reinforcement**: Peak reduction × grid upgrade cost per MW.

**Synthetic prices** (when not provided): Dual-peak pattern with morning ($90/MWh) and evening ($110/MWh) peaks on a $50/MWh base.

---

## Fleet Evolution Metrics

### compute_fleet_evolution_metrics

```python
def compute_fleet_evolution_metrics(
    years: list[int],
    fleet_ev_by_year: list[int],
    fleet_by_category_by_year: dict[str, list[int]],
    ev_categories: dict[str, dict],
    base_demand_annual_gwh: float = 100.0,
) -> dict
```

Compute yearly metrics for fleet evolution visualization. Returns dict with keys: `years`, `total_ev`, `energy_gwh`, `peak_mw`, `ev_demand_pct`, `v2g_capacity_mw`.
