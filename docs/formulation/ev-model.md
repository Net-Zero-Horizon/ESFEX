# EV Model

The electric vehicle (EV) model adds fleet electrification to the operational dispatch: S-curve fleet growth, a driving-energy profile per node, smart charging and vehicle-to-grid (V2G) [**[51]**](../reference/bibliography.md#ref51). The fleet at each node acts as a flexible load with storage [**[52]**](../reference/bibliography.md#ref52).

## Overview

The EV model has two parts:

1. **Python** (`models/ev.py`, `Orchestrator._build_ev_config` in `runner.py`): fleet growth with logistic S-curves per vehicle category, hourly driving-energy and V2G-availability profiles, and aggregation of the categories into one fleet parameter set per window.
2. **Julia** (`add_ev_constraints!()` in `power_system.jl`): fleet state of charge, charging and V2G limits in the operational dispatch.

EV modelling is active when the system defines `ev_categories`, `ev_quantity` and `base_patterns`. It applies to the operational dispatch only: the master problem (capacity expansion) has no EV model, and the EV driving energy is not part of the demand the master plans for (see [Section 7](#7-capacity-expansion)).

---


## 1. S-Curve Fleet Growth


The fleet of each category \(c\) at node \(n\) grows from its base-year count along a logistic curve:

\[
N_{c,n}(y) = N^{0}_{c,n} \cdot \frac{K_c}{1 + e^{-r_c (y - y^{mid}_c)}}
\tag{EV-S}
\]

\[
y^{mid}_c = y^{base} + \text{mid\_point\_fraction}_c \cdot (y^{end} - y^{base})
\]

| Parameter | Description | Config field | Default |
|-----------|-------------|--------------|---------|
| \(N^0_{c,n}\) | Base-year vehicle count per node | `ev_quantity.<category>[n]` | -- |
| \(K_c\) | Maximum growth multiplier | `ev_categories.<category>.max_adoption` | 35 |
| \(r_c\) | Logistic growth rate (1/year) | `ev_categories.<category>.growth_rate` | 0.14 |
| \(\text{mid\_point\_fraction}_c\) | Position of the inflection in the run | `ev_categories.<category>.mid_point_fraction` | 0.5 |

\(y^{base}\) and \(y^{end}\) are the first and last simulated years, so the curve depends on the length of the run. \(K_c\) multiplies the base count: in the first year, with the defaults and a 25-year run, \(N = N^0 \cdot 35 / (1 + e^{1.75}) \approx 5.2\,N^0\). Set `ev_quantity` and \(K_c\) with this in mind.

The same factor (`fleet_growth_factor()`) sizes both the driving-energy profile and the fleet limits used in the dispatch, so charging power, battery capacity and V2G power grow with the fleet.

---


## 2. Vehicle Categories


Each category has its own technical parameters:

```yaml
ev_categories:
  car:
    battery_capacity: 60.0        # kWh per vehicle
    charging_power: 11.0          # kW per vehicle
    v2g_power: 7.0                # kW per vehicle
    v2g_participation: 0.3        # share of connected vehicles offering V2G
    efficiency_charge: 0.95
    efficiency_discharge: 0.92
    min_soc: 0.20
    daily_energy_kwh: 8.0         # default: battery_capacity x 0.12
  van:
    battery_capacity: 100.0
    charging_power: 22.0
    v2g_power: 15.0
    v2g_participation: 0.2
    efficiency_charge: 0.93
    efficiency_discharge: 0.90
    min_soc: 0.25
    max_adoption: 20.0
    growth_rate: 0.2

ev_quantity:
  car: [1000, 1200]               # base-year vehicles per node
  van: [200, 250]

base_patterns:                    # 24 hourly values per category (plug-in shape)
  car: [0.1, 0.1, 0.1, 0.05, 0.05, 0.1, 0.3, 0.5, 0.2, 0.1, 0.1, 0.1,
        0.1, 0.1, 0.1, 0.2, 0.4, 0.8, 0.9, 0.7, 0.5, 0.3, 0.2, 0.1]
  van: [0.2, 0.2, 0.2, 0.2, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1,
        0.1, 0.1, 0.1, 0.1, 0.1, 0.2, 0.3, 0.4, 0.4, 0.3, 0.3, 0.2]
```

### 2.1 Category Parameters


| Parameter | Symbol | Units | Description |
|-----------|--------|-------|-------------|
| `battery_capacity` | \(E^{bat}_c\) | kWh | Battery capacity per vehicle |
| `charging_power` | \(\bar{P}^{ch}_c\) | kW | Maximum charging power per vehicle |
| `v2g_power` | \(\bar{P}^{v2g}_c\) | kW | Maximum V2G power per vehicle |
| `v2g_participation` | \(\phi_c\) | -- | Share of connected vehicles that offer V2G |
| `efficiency_charge` | \(\eta^{ch}_c\) | -- | Charging efficiency |
| `efficiency_discharge` | \(\eta^{dis}_c\) | -- | V2G discharging efficiency |
| `min_soc` | \(\underline{SOC}_c\) | -- | Minimum state of charge (fraction) |
| `daily_energy_kwh` | \(e^{day}_c\) | kWh/day | Driving energy per vehicle per day |
| `max_adoption`, `growth_rate`, `mid_point_fraction` | \(K_c, r_c\) | -- | S-curve (EV-S) |

The penalty for driving energy the fleet cannot supply is `penalties.ev_loss` ($/MWh, default: `penalties.loss_of_load`). A warning is logged when it is set below `loss_of_load`, because the dispatch then prefers leaving vehicles uncharged to generating.

### 2.2 Fleet Aggregation


The Julia model holds one parameter set for the whole fleet. For each year, `_build_ev_config` aggregates the categories with the grown fleets \(N_c = \sum_n N_{c,n}(y)\):

- Battery capacity, charging power and V2G power per vehicle are vehicle-weighted means, so the fleet's total battery energy and total charge and V2G power are exact.
- Charging and discharging efficiency and minimum SOC are weighted by battery energy (\(N_c E^{bat}_c\)).
- The maximum SOC is 1.0 (fixed in the runner). The SOC at the start of each window is `EV_initial_soc` of the node (a fraction, clamped to `[min_soc, 1]`), or 80 % of fleet capacity where it is not given.

With \(N_n = \sum_c N_{c,n}(y)\) vehicles at node \(n\), the fleet totals are

\[
E^{EV}_n = \frac{N_n \bar{E}^{bat}}{1000} \;\text{[MWh]}, \qquad
\bar{P}^{ch}_n = \frac{N_n \bar{P}^{ch}}{1000} \;\text{[MW]}, \qquad
\bar{P}^{v2g}_n = \frac{N_n \bar{P}^{v2g}}{1000} \;\text{[MW]}
\]

---


## 3. Driving and V2G Profiles


`base_patterns` gives, per category, the 24-hour shape of when vehicles charge (the share of the fleet plugged in). The pattern sets *when* energy is drawn; its magnitude comes from `daily_energy_kwh`. The driving energy of category \(c\) at node \(n\) and hour \(t\) (MWh per hour) is

\[
D^{drive}_{c,n,t} = \tilde{\pi}_c(h_t) \cdot N_{c,n}(y_t) \cdot \frac{e^{day}_c}{1000 \sum_{h} \pi_c(h)}
\]

where \(\pi_c(h)\) is the pattern value for hour of day \(h\) and \(\tilde{\pi}_c(h_t) = \text{clip}(\pi_c(h_t) + \varepsilon_t, 0, 1)\) adds Gaussian noise \(\varepsilon_t \sim N(0, 0.02)\). The noise is seeded, so a configuration always gives the same profile. The pattern repeats every day; the fleet size changes once per year. The node profile \(D^{EV}_{n,t} = \sum_c D^{drive}_{c,n,t}\) is the driving consumption passed to Julia.

The V2G power on offer uses the same pattern (noise \(N(0, 0.01)\)) and the participation share:

\[
P^{v2g,avail}_{n,t} = \sum_c \tilde{\pi}_c(h_t) \cdot \phi_c \cdot N_{c,n}(y_t) \cdot \frac{\bar{P}^{v2g}_c}{1000}
\]

The runner passes it to Julia as a fraction of the fleet's rated V2G power, \(\alpha^{v2g}_{n,t} = P^{v2g,avail}_{n,t} / \bar{P}^{v2g}_n\).

---


## 4. Optimization Constraints


`add_ev_constraints!()` adds the following per node \(n\) (EV variables are node-indexed) and time step \(t\). Nodes without vehicles get `ev_zero_*` constraints that fix all EV variables to 0.

| Variable | Description |
|----------|-------------|
| \(P^{ch,EV}_{n,t} \ge 0\) | Fleet charging power (`ev_charging`) |
| \(P^{V2G}_{n,t} \ge 0\) | V2G discharge power (`ev_v2g`) |
| \(E^{EV}_{n,t} \ge 0\) | Fleet state of charge, MWh, \(t = 1 \ldots T+1\) (`ev_soc`) |
| \(L^{EV}_{n,t} \ge 0\) | Driving energy not supplied (`ev_loss`) |
| \(z^{EV}_{n,t} \in [0,1]\) | Charge/V2G status, relaxed (`ev_charge_status`) |

### EV-1: SOC Dynamics

Driving draws its energy from the fleet battery:

\[
E^{EV}_{n,t+1} = E^{EV}_{n,t} + \Delta t \left[ \eta^{ch} P^{ch,EV}_{n,t} - \frac{P^{V2G}_{n,t}}{\eta^{dis}} - \left(D^{EV}_{n,t} - L^{EV}_{n,t}\right) \right]
\tag{EV-1}
\]

Powers are in MW and \(\Delta t\) is the time step; with `resolution_hours > 1` the hourly driving and availability profiles are averaged over each step. Over a window the grid must therefore supply the driving energy (divided by \(\eta^{ch}\)), and V2G can only return energy that is not needed for driving. Charging can move in time within the power and SOC limits (smart charging). Constraint `ev_soc_dynamics_n{n}_{t}`.

### EV-2: Unserved Driving Energy

\[
0 \le L^{EV}_{n,t} \le D^{EV}_{n,t}
\tag{EV-2}
\]

\(L^{EV}\) is driving energy the fleet cannot supply; it is penalized at `penalties.ev_loss`. Constraint `ev_loss_max_n{n}_{t}`.

### EV-3: Charging Power Limit

\[
P^{ch,EV}_{n,t} \le \max\left( \bar{P}^{ch}_n,\; 2 D^{EV}_{n,t} \right)
\tag{EV-3}
\]

The floor of twice the driving energy keeps the problem feasible when the configured charging power is very low. Constraint `ev_max_charge_n{n}_{t}`.

### EV-4: V2G Limit

\[
P^{V2G}_{n,t} \le \bar{P}^{v2g}_n \cdot \alpha^{v2g}_{n,t}
\tag{EV-4}
\]

Constraint `ev_max_v2g_n{n}_{t}`.

### EV-5: SOC Bounds

\[
\underline{SOC} \cdot E^{EV}_n \le E^{EV}_{n,t+1} \le \overline{SOC} \cdot E^{EV}_n
\tag{EV-5}
\]

with \(\overline{SOC} = 1\). Constraints `ev_soc_min_n{n}_{t}` and `ev_soc_max_n{n}_{t}`.

### EV-6: Charge/V2G Mutual Exclusivity

\[
P^{ch,EV}_{n,t} \le M_{EV} \, z^{EV}_{n,t}, \qquad
P^{V2G}_{n,t} \le M_{EV} \, (1 - z^{EV}_{n,t})
\tag{EV-6}
\]

with \(M_{EV} = \max(\bar{P}^{ch}_n, 2D^{EV}_{n,t}) + \bar{P}^{v2g}_n \alpha^{v2g}_{n,t}\). \(z^{EV}\) is continuous, so this is a relaxation; simultaneous charging and discharging loses energy and is not chosen in practice. Constraints `ev_mutex_charge_n{n}_{t}` and `ev_mutex_v2g_n{n}_{t}`.

### EV-7: Initial and Cyclic SOC

\[
E^{EV}_{n,1} = 0.8 \, E^{EV}_n, \qquad E^{EV}_{n,T+1} = E^{EV}_{n,1}
\tag{EV-7}
\]

Each window starts at the node's initial SOC (`EV_initial_soc`, default 80 % of fleet capacity) and must end where it started, so the fleet cannot carry energy in or out of a window. Constraints `ev_soc_initial_n{n}` and `ev_soc_cyclic_n{n}`.

---


## 5. Power System Integration


Charging is a load and V2G a supply in the power balance (PB-1 for a single bus, DC-1 and the ACOPF balance for networks). EV variables are per node; at each bus they enter scaled by the bus `demand_fraction` (0 on `role="connection"` buses):

\[
\sum_g P_{g,b,t} + \ldots + \alpha_b P^{V2G}_{n(b),t} = D_{b,t} + \ldots + \alpha_b P^{ch,EV}_{n(b),t}
\]

When the EV model is active, the EV driving energy is **not** added to the demand array; the dispatch meets it only through \(P^{ch,EV}\) and EV-1. Without EV categories, no EV demand exists.

---


## 6. Objective Terms


### 6.1 Unserved Driving Energy

\[
Z^{EV,loss} = \Delta t \sum_{n,t} c^{EV,loss} L^{EV}_{n,t}
\tag{EV-OBJ}
\]

with \(c^{EV,loss}\) = `penalties.ev_loss` and \(\Delta t\) the time step.

### 6.2 V2G Compensation (reported only)

The payment to EV owners for V2G energy, \(\sum_{n,t} \pi_t P^{V2G}_{n,t}\) at the electricity price \(\pi_t\), is computed and reported as `v2g_compensation` but is not part of the objective. It is a transfer between agents; the system value of V2G is the generation it displaces in the power balance.

---


## 7. Capacity Expansion


The master problem has no EV model: its representative-day input carries no EV configuration. It plans with the base demand plus the EV energy profile as a fixed load (unmanaged charging); the dispatch then optimizes when the fleet charges. When the EV model is active the dispatch's demand array leaves the EV load out, since it is served through \(P^{ch,EV}\).

---


## 8. Configuration Reference


EV settings are system keys (shown here without the `systems.<name>` prefix):

```yaml
ev_categories:
  car:
    battery_capacity: 60.0
    charging_power: 11.0
    v2g_power: 7.0
    v2g_participation: 0.3
    efficiency_charge: 0.95
    efficiency_discharge: 0.92
    min_soc: 0.20
    max_adoption: 10.0
    growth_rate: 0.3
    mid_point_fraction: 0.5
    daily_energy_kwh: 7.2

ev_quantity:
  car: [1000, 1200]

base_patterns:
  car: [0.1, 0.1, 0.1, 0.05, 0.05, 0.1, 0.3, 0.5, 0.2, 0.1, 0.1, 0.1,
        0.1, 0.1, 0.1, 0.2, 0.4, 0.8, 0.9, 0.7, 0.5, 0.3, 0.2, 0.1]

penalties:
  ev_loss: 10000000.0             # $/MWh, default = loss_of_load
```

The system key `EV_initial_soc` gives each node's SOC at the start of every dispatch window (fraction of fleet capacity; 80 % where missing).

---


## 9. EV Adoption Modeling (Wizard)

The EV & V2G Assessment Wizard (`models/ev_adoption.py`, re-exported from the `evrex` library) provides four data-driven methods to *populate* the fleet parameters above from transport data:

### 9.1 Method Summary

| Method | Model | Key Drivers |
|--------|-------|-------------|
| **Logistic Regression** | \(\text{penetration} = \sigma(\beta_0 + \beta_{fuel} \cdot \Delta_{fuel} + \beta_{GDP} \cdot GDP + \ldots)\) | Fuel savings, EV cost, charging infrastructure, GDP, urbanization |
| **Bass Diffusion** | \(F(t) = \frac{1 - e^{-(p+q)t}}{1 + (q/p) \cdot e^{-(p+q)t}}\) | Innovation coefficient \(p\), imitation coefficient \(q\) |
| **TCO-Parity** | \(\text{adoption} = \sigma(s \cdot (TCO_{ICE} - TCO_{EV}) / TCO_{ICE})\) | Battery learning curve, fuel prices, subsidies, vehicle lifetime |
| **Policy-Driven** | Linear sales ramp + scrappage model | ICE ban year, emission targets, vehicle lifetime |

All methods produce `EVAdoptionCurve` outputs (year-by-year penetration, fleet by category, energy demand, peak charging) that feed into `fit_adoption_to_ev_config()`.

### 9.2 S-Curve Parameter Fitting

`fit_adoption_to_ev_config()` fits any adoption trajectory to the logistic parameters of (EV-S) by least squares (`scipy.optimize.curve_fit`), producing `max_adoption`, `growth_rate` and `mid_point_fraction`.

### 9.3 V2G Technical Potential

`models/ev_analysis.py` (also from `evrex`) provides:

- **Charging demand characterization**: uncontrolled, TOU-shifted and optimized valley-filling scenarios
- **V2G capacity**: hourly discharge potential from connected-time profiles and participation rates
- **Battery degradation**: Wohler-type cycle aging plus calendar aging for NMC/LFP chemistries, giving break-even V2G compensation rates
- **Grid impact assessment**: peak shaving, valley filling, arbitrage revenue, avoided grid reinforcement

These analyses are not part of the optimization. See [API: Models EV Adoption](../api/models-ev-adoption.md) and [API: Models EV Analysis](../api/models-ev-analysis.md).

---


## 10. Implementation Reference


| Function | Language | File | Purpose |
|----------|----------|------|---------|
| `fleet_growth_factor()` | Python | `models/ev.py` | S-curve multiplier (EV-S) |
| `generate_ev_profiles()` | Python | `models/ev.py` | Driving-energy profiles per category and node |
| `generate_v2g_availability()` | Python | `models/ev.py` | V2G power on offer per category and node |
| `Orchestrator._build_ev_config()` | Python | `runner.py` | Fleet aggregation and per-window EV input |
| `run_ev_logistic_adoption()`, `run_ev_bass_diffusion()`, `run_ev_tco_parity()`, `run_ev_policy_driven()` | Python | `models/ev_adoption.py` | Adoption models (wizard) |
| `fit_adoption_to_ev_config()` | Python | `models/ev_adoption.py` | S-curve fitting |
| `generate_charging_profiles()`, `compute_v2g_potential()`, `compute_battery_degradation()`, `assess_grid_impact()` | Python | `models/ev_analysis.py` | V2G and grid-impact analysis (wizard) |
| `add_ev_constraints!()` | Julia | `power_system.jl` | EV-1 to EV-7 |
| `build_objective!()` | Julia | `power_system.jl` | EV loss penalty; V2G compensation (reported) |

---

## References

The vehicle-to-grid (V2G) concept and capacity calculation follow Kempton and Tomić [**[51]**](../reference/bibliography.md#ref51). EV integration impacts on power systems are reviewed by Lopes et al. [**[52]**](../reference/bibliography.md#ref52). The Bass diffusion model for technology adoption follows Bass [**[53]**](../reference/bibliography.md#ref53). Battery degradation from cycling (Wohler model) draws on Xu et al. [**[32]**](../reference/bibliography.md#ref32).

See the [full bibliography](../reference/bibliography.md) for complete citation details.
