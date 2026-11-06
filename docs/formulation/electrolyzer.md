# Electrolyzer Model

Electrolyzers convert power to hydrogen (P2H2) [**[49]**](../reference/bibliography.md#ref49) and are a common sector-coupling technology [**[50]**](../reference/bibliography.md#ref50). In ESFEX the electrolyzer is a bounded, controllable electrical load in the operational dispatch (`power_system.jl`); its hydrogen output is not modelled.

---


## 1. Overview


Electrolyzers consume electricity to produce hydrogen via water electrolysis (splitting water molecules into hydrogen and oxygen). In power system planning, electrolyzers serve dual roles:

1. **Flexible load**: Electrolyzers can modulate their power consumption to absorb surplus renewable generation, providing demand-side flexibility.
2. **Sector coupling**: Hydrogen output can satisfy non-electric energy demands (industrial feedstock, transport fuel, heating) or be stored for later reconversion to electricity via fuel cells.

What ESFEX models today:

- The electrolyzer's power consumption in the operational dispatch, bounded by its rated power and ramp rates, as a load in the power balance
- Its variable and fixed costs

What it does not model: hydrogen output, hydrogen demand or storage coupling, investment in new electrolyzers, minimum operating point, and the electrolyzer in the master problem. Because the dispatch gives the hydrogen no value, it plays only role 1: a load the optimizer runs when that lowers system cost, for example to absorb renewable output that would otherwise be curtailed at `curtailment_cost`. The sector-coupling role (2) is not implemented.

---


## 2. Technology Background

### 2.1 Electrolyzer Types


The model is technology-agnostic but parameterized to represent common electrolyzer technologies:

| Technology | Typical Efficiency | Ramp Rate | Capital Cost | Lifetime |
|------------|-------------------|-----------|-------------|----------|
| Alkaline (AEL) | 60--70% | Moderate | Lower | 20--30 years |
| PEM (PEMEL) | 55--70% | Fast | Higher | 10--20 years |
| Solid Oxide (SOEL) | 70--85% | Slow | Highest | 5--10 years |

The efficiency values represent the ratio of hydrogen energy output (HHV) to electrical energy input.

### 2.2 Energy Content of Hydrogen


The energy required to produce hydrogen depends on the thermodynamic pathway. Key reference values:

| Property | Value |
|----------|-------|
| Lower Heating Value (LHV) | 33.33 kWh/kg |
| Higher Heating Value (HHV) | 39.41 kWh/kg |
| Thermodynamic minimum | 39.41 kWh/kg (reversible, HHV) |
| Practical consumption | 50--60 kWh/kg (including system losses) |

The model parameter `energy_per_kg_h2` captures the total system-level energy consumption per kilogram of hydrogen produced.

---


## 3. Decision Variable


| Variable | Domain | Units | Description | Julia Name |
|----------|--------|-------|-------------|------------|
| \(P^{elz}_{b,t}\) | \(\mathbb{R}_+\) | MW | Power consumption at bus \(b\), time step \(t\) | `electrolyzer_power[b,t]` |

The variable is created in `build_variables!()` (`power_system.jl`) when the system has at least one electrolyzer. It is indexed by bus: the per-node configuration lists are mapped onto buses like generators, with the rated power at the node's first bus and the other parameters copied to every bus of the node.

!!! warning "One electrolyzer per system"
    Only the first entry under `electrolyzers` is used; further entries are ignored. To model several sites, put them in one entry with per-node lists.

---


## 4. Constraints

Implemented in `add_electrolyzer_dispatch_constraints!()` (`power_system.jl`).

### ELZ-1: Rated Power

\[
P^{elz}_{b,t} \leq \bar{P}^{elz}_b \quad \forall b, t
\tag{ELZ-1}
\]

where \(\bar{P}^{elz}_b\) is `rated_power` mapped to bus \(b\). A bus without rated power gets no electrolyzer load: there is no investment variable. Constraint `electrolyzer_max_b{b}_t{t}`.

### ELZ-2: Ramp Rates

\[
P^{elz}_{b,t} - P^{elz}_{b,t-1} \leq \bar{P}^{elz}_b \cdot \min\left(1, r^{up}_b \Delta t\right) \quad \forall b, t \geq 2
\tag{ELZ-2a}
\]

\[
P^{elz}_{b,t-1} - P^{elz}_{b,t} \leq \bar{P}^{elz}_b \cdot \min\left(1, r^{down}_b \Delta t\right) \quad \forall b, t \geq 2
\tag{ELZ-2b}
\]

where \(r^{up}_b\), \(r^{down}_b\) are `ramp_up` and `ramp_down` in fractions of rated power per hour and \(\Delta t\) is the time step (`resolution_hours`). A rate of 0 or less means no limit, and the constraint is omitted. The limits apply inside a window only, not across rolling-horizon window boundaries. Constraints `electrolyzer_ramp_up_b{b}_t{t}` and `electrolyzer_ramp_down_b{b}_t{t}`.

### Not implemented

- **Hydrogen production.** `eff_at_rated`, `eff_at_min` and `energy_per_kg_h2` are read but no hydrogen output is computed in the dispatch, and no hydrogen demand or storage is linked to the electrolyzer.
- **Minimum operating point.** `min_power` is not used; the electrolyzer can run at any level between 0 and its rated power.
- **Investment.** `invest_cost`, `invest_max_power` and `life_time` are not used by the dispatch, and the master problem has no electrolyzer.

The functions in `electrolyzer.jl` (`build_electrolyzer_variables!`, `add_electrolyzer_constraints!`, ...) and the electrolyzer block of `primary_energy.jl` (investment, `h2_production`, `electrolyzer_max_power_*`) model investment and hydrogen output, but no run calls them: the primary-energy adapter passes no electrolyzer configuration.

---


## 5. Objective Terms


Built in `build_objective!()` (`power_system.jl`) as `electrolyzer_cost`, part of the energy costs that are multiplied by the time step \(\Delta t\):

\[
C^{elz} = \Delta t \left[ \sum_{b,t} c^{var}_b \, P^{elz}_{b,t} + \sum_b \bar{P}^{elz}_b \, c^{fix}_b \, T \right]
\tag{ELZ-OBJ}
\]

where \(T\) is the number of time steps in the window, so \(\Delta t \cdot T\) is the window length in hours.

| Term | Units | Description |
|------|-------|-------------|
| \(c^{var}_b\) (`variable_cost`) | \$/MWh | Variable cost of the electricity consumed |
| \(c^{fix}_b\) (`fixed_cost`) | \$/MW per hour | Fixed cost on rated power. It is applied per hour of the window, so a yearly figure in \$/MW/year must be divided by 8760. It is a constant and does not affect the dispatch |

`water_cost` is not used.

---


## 6. Power System Coupling


\(P^{elz}_{b,t}\) is a load on the demand side of the bus power balance, in the single-bus balance (PB-1), the DC network balance (DC-1) and the ACOPF balance:

\[
\sum_g P_{g,b,t} + \ldots = D_{b,t} + P^{elz}_{b,t} + \sum_k P^{ch}_{k,b,t} + \ldots
\]

---


## 7. Configuration


Electrolyzers are a system key. Lists are per node:

```yaml
electrolyzers:
  pem:
    name: PEM Electrolyzer
    technology: PEM
    rated_power: [10.0]           # MW per node
    min_power: [0.1]              # not used by the dispatch
    ramp_up: [0.5]                # fraction of rated power per hour (0 = no limit)
    ramp_down: [0.5]
    eff_at_rated: [0.70]          # read, not used (no H2 output)
    eff_at_min: [0.65]
    energy_per_kg_h2: 55.0        # kWh per kg H2, not used
    fixed_cost: [3.4]             # $/MW per hour (about 30000 $/MW/year)
    variable_cost: [2.0]          # $/MWh
    water_cost: 0.05              # not used
    invest_cost: [1500000.0]      # not used
    invest_max_power: [0.0]       # not used
    life_time: [20]
    initial_age: [0]
    degradation_rate: [0.0]
```

### 7.1 Parameter Reference


| Parameter | Symbol | Units | Used | Description |
|-----------|--------|-------|------|-------------|
| `rated_power` | \(\bar{P}^{elz}\) | MW | Yes | Installed capacity per node (ELZ-1) |
| `ramp_up` / `ramp_down` | \(r^{up}\), \(r^{down}\) | 1/h | Yes | Ramp rates as fraction of rated power per hour (ELZ-2) |
| `variable_cost` | \(c^{var}\) | \$/MWh | Yes | Variable cost |
| `fixed_cost` | \(c^{fix}\) | \$/MW/h | Yes (constant) | Fixed cost per hour |
| `min_power` | -- | fraction | No | Minimum operating point |
| `eff_at_rated`, `eff_at_min`, `energy_per_kg_h2` | -- | -- | No | Hydrogen conversion |
| `water_cost` | -- | \$/kg H2 | No | Water cost |
| `invest_cost`, `invest_max_power`, `life_time` | -- | -- | No | Investment |

Electrolyzer results are not written to the HDF5 output.

---


## 8. Implementation Reference


| Function | Purpose | File |
|----------|---------|------|
| `build_variables!()` | Creates `electrolyzer_power` (bus × time) | `power_system.jl` |
| `add_electrolyzer_dispatch_constraints!()` | ELZ-1 and ELZ-2 | `power_system.jl` |
| `build_objective!()` | `electrolyzer_cost` (ELZ-OBJ) | `power_system.jl` |
| `add_demand_constraints!()`, `add_dc_constraints!()`, `add_acopf_power_balance!()` | Load term in the power balance | `power_system.jl`, `transmission_dc.jl`, `transmission_acopf.jl` |
| `PowerSystemAdapter` | Builds `ElectrolyzerConfig` from the first `electrolyzers` entry | `bridge/adapters.py` |

---

## References

The power-to-hydrogen conversion modeling and electrolysis technology review follow Buttler and Spliethoff [**[49]**](../reference/bibliography.md#ref49). The economics of renewable hydrogen production are analyzed by Glenk and Reichelstein [**[50]**](../reference/bibliography.md#ref50).

See the [full bibliography](../reference/bibliography.md) for complete citation details.
