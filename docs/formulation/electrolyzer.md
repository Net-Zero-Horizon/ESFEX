# Electrolyzer Model

Electrolyzers convert power to hydrogen (P2H2) [**[49]**](../reference/bibliography.md#ref49) and are a common sector-coupling technology [**[50]**](../reference/bibliography.md#ref50). In ESFEX the electrolyzer is a bounded, controllable electrical load in the operational dispatch (`power_system.jl`). With the primary-energy model on and a fuel source named `Hydrogen`, its hydrogen fills that fuel's tank at its node; otherwise the hydrogen is only reported.

---


## 1. Overview


Electrolyzers consume electricity to produce hydrogen via water electrolysis (splitting water molecules into hydrogen and oxygen). In power system planning, electrolyzers serve dual roles:

1. **Flexible load**: Electrolyzers can modulate their power consumption to absorb surplus renewable generation, providing demand-side flexibility.
2. **Sector coupling**: Hydrogen output can satisfy non-electric energy demands (industrial feedstock, transport fuel, heating) or be stored for later reconversion to electricity via fuel cells.

What ESFEX models today:

- The electrolyzer's power consumption in the operational dispatch, bounded by its rated power and ramp rates, as a load in the power balance
- Its variable, water and fixed costs
- With primary energy: investment in new electrolyzer power, decided by the master problem on its representative days, and the electrolyzer in the master's day operation ([Capacity Expansion](capacity-expansion.md#fuel-block)). Each dispatch year receives the power built and in service that year on top of `rated_power`
- Every entry under `electrolyzers`, each with its own power and hydrogen output in the results
- With primary energy: the hydrogen enters the `Hydrogen` tank of the electrolyzer's node ([Primary Energy](primary-energy.md#power-system-coupling), PE-H2), where hydrogen-fuelled generators and non-electric hydrogen demand draw it (role 2)

What it does not model: minimum operating point, a market value for hydrogen, and, without primary energy, investment and the electrolyzer in the master problem. Without a `Hydrogen` primary-energy source the dispatch gives the hydrogen no value, and the electrolyzer plays only role 1: a load the optimizer runs when that lowers system cost, for example to absorb renewable output that would otherwise be curtailed at `curtailment_cost`.

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
| \(P^{elz}_{e,b,t}\) | \(\mathbb{R}_+\) | MW | Power consumption of electrolyzer \(e\) at bus \(b\), time step \(t\) | `electrolyzer_power[e,b,t]` |

The variable is created in `build_variables!()` (`power_system.jl`) when the system has at least one electrolyzer, with one index \(e\) per entry under `electrolyzers`, in configuration order. It is indexed by bus: each electrolyzer's per-node configuration lists are mapped onto buses like generators, with the rated power at the node's first bus and the other parameters copied to every bus of the node. The constraints and costs below hold for each electrolyzer \(e\); the subscript \(e\) is left out.

---


## 4. Constraints

Implemented in `add_electrolyzer_dispatch_constraints!()` (`power_system.jl`).

### ELZ-1: Rated Power

\[
P^{elz}_{b,t} \leq \bar{P}^{elz}_b \, a_t \quad \forall b, t
\tag{ELZ-1}
\]

where \(\bar{P}^{elz}_b\) is `rated_power` mapped to bus \(b\) and \(a_t\) the availability of an `electrolyzer` [scheduled interruption](../user-guide/scheduled-interruptions.md) in step \(t\) (1 without one; it applies to the whole electrolyzer). A bus without rated power gets no electrolyzer load: there is no investment variable. Constraint `electrolyzer_max_b{b}_t{t}` for the first electrolyzer, `electrolyzer_max_e{e}_b{b}_t{t}` for the others.

### ELZ-2: Ramp Rates

\[
P^{elz}_{b,t} - P^{elz}_{b,t-1} \leq \bar{P}^{elz}_b \cdot \min\left(1, r^{up}_b \Delta t\right) \quad \forall b, t \geq 2
\tag{ELZ-2a}
\]

\[
P^{elz}_{b,t-1} - P^{elz}_{b,t} \leq \bar{P}^{elz}_b \cdot \min\left(1, r^{down}_b \Delta t\right) \quad \forall b, t \geq 2
\tag{ELZ-2b}
\]

where \(r^{up}_b\), \(r^{down}_b\) are `ramp_up` and `ramp_down` in fractions of rated power per hour and \(\Delta t\) is the time step (`resolution_hours`). A rate of 0 or less means no limit, and the constraint is omitted. The limits apply inside a window only, not across rolling-horizon window boundaries. Constraints `electrolyzer_ramp_up_b{b}_t{t}` and `electrolyzer_ramp_down_b{b}_t{t}` (with `_e{e}` after `up`/`down` for the second and later electrolyzers).

### Not implemented

- **Hydrogen balance without primary energy.** Without a `Hydrogen` primary-energy source the hydrogen produced is only reported (see [Results](#72-results)).
- **Minimum operating point.** `min_power` is not used; the electrolyzer can run at any level between 0 and its rated power.
- **Investment in the dispatch.** The dispatch builds no electrolyzers; the master does (`invest_cost`, `invest_max_power`, `life_time`), only with primary energy on. A built electrolyzer retires after its `life_time`; existing ones do not age.

The functions in `electrolyzer.jl` (`build_electrolyzer_variables!`, `add_electrolyzer_constraints!`, ...) model investment and hydrogen output, but no run calls them; the master's electrolyzers are built in `master_fuel.jl`.

---


## 5. Objective Terms


Built in `build_objective!()` (`power_system.jl`) as `electrolyzer_cost`, part of the energy costs that are multiplied by the time step \(\Delta t\):

\[
C^{elz} = \Delta t \left[ \sum_{b,t} \left(c^{var}_b + c^{water} h_b\right) P^{elz}_{b,t} + \sum_b \bar{P}^{elz}_b \, c^{fix}_b \, T \right]
\tag{ELZ-OBJ}
\]

where \(T\) is the number of time steps in the window, so \(\Delta t \cdot T\) is the window length in hours, and \(h_b\) the hydrogen made per MWh (kg/MWh, \(1000 \cdot \bar{\eta}_b / e^{H_2}\)). The master charges the same variable and water cost on its representative days, and the fixed cost per year (`fixed_cost` × hours of the year × rated plus built power).

| Term | Units | Description |
|------|-------|-------------|
| \(c^{var}_b\) (`variable_cost`) | \$/MWh | Variable cost of the electricity consumed |
| \(c^{fix}_b\) (`fixed_cost`) | \$/MW per hour | Fixed cost on rated power. It is applied per hour of the window, so a yearly figure in \$/MW/year must be divided by 8760. It is a constant and does not affect the dispatch |
| \(c^{water}\) (`water_cost`) | \$/kg H2 | Water cost of the hydrogen produced |

The cost is reported as `electrolyzer_cost` and, when the electrolyzers feed the primary-energy `Hydrogen` tank, also as `pe_electrolyzer_cost` (the same amount, charged once).

---


## 6. Power System Coupling


The power of all electrolyzers at a bus is a load on the demand side of the bus power balance, in the single-bus balance (PB-1), the DC network balance (DC-1) and the ACOPF balance:

\[
\sum_g P_{g,b,t} + \ldots = D_{b,t} + \sum_e P^{elz}_{e,b,t} + \sum_k P^{ch}_{k,b,t} + \ldots
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
    eff_at_rated: [0.70]          # hydrogen output (reported; fills a Hydrogen fuel tank)
    eff_at_min: [0.65]
    energy_per_kg_h2: 55.0        # kWh per kg H2
    fixed_cost: [3.4]             # $/MW per hour (about 30000 $/MW/year)
    variable_cost: [2.0]          # $/MWh
    water_cost: 0.05              # $/kg H2
    invest_cost: [1500000.0]      # $/MW, master problem (primary energy on)
    invest_max_power: [0.0]       # MW the master may build
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
| `eff_at_rated`, `eff_at_min`, `energy_per_kg_h2` | \(\eta\), \(e^{H_2}\) | --, kWh/kg | With a `Hydrogen` primary-energy source | Hydrogen into the `Hydrogen` tank (PE-H2, in t); always reported in the results |
| `water_cost` | \(c^{water}\) | \$/kg H2 | Yes | Water cost per kg of hydrogen |
| `invest_cost`, `invest_max_power`, `life_time` | -- | \$/MW, MW, years | Master, with primary energy | Investment |

### 7.2 Results

Each electrolyzer's power and hydrogen production are kept per node and hour in the year results (`YearResults.electrolyzer_power`, `YearResults.electrolyzer_h2`, shape electrolyzer × node × hour, in configuration order) and written to the HDF5 file under `electrolyzer_power/{name}` (MW) and `electrolyzer_h2/{name}` (kg/h); see [HDF5 Output Schema](../reference/hdf5-output-schema.md#electrolyzer-data). The hydrogen output is computed after the solve, with the same conversion as the primary-energy hydrogen balance (which takes it in tonnes per step):

\[
H_2 = P^{elz} \cdot \frac{1000 \cdot (\eta^{rated} + \eta^{min}) / 2}{e^{H_2}} \quad \text{[kg/h]}
\]

---


## 8. Implementation Reference


| Function | Purpose | File |
|----------|---------|------|
| `build_variables!()` | Creates `electrolyzer_power` (electrolyzer × bus × time) | `power_system.jl` |
| `add_electrolyzer_dispatch_constraints!()` | ELZ-1 and ELZ-2 | `power_system.jl` |
| `build_objective!()` | `electrolyzer_cost` (ELZ-OBJ) | `power_system.jl` |
| `add_demand_constraints!()`, `add_dc_constraints!()`, `add_acopf_power_balance!()` | Load term in the power balance | `power_system.jl`, `transmission_dc.jl`, `transmission_acopf.jl` |
| `extract_solution()` | `electrolyzer_power`, `electrolyzer_h2` results | `power_system.jl` |
| `PowerSystemAdapter` | Builds one `ElectrolyzerConfig` per `electrolyzers` entry | `bridge/adapters.py` |

---

## References

The power-to-hydrogen conversion modeling and electrolysis technology review follow Buttler and Spliethoff [**[49]**](../reference/bibliography.md#ref49). The economics of renewable hydrogen production are analyzed by Glenk and Reichelstein [**[50]**](../reference/bibliography.md#ref50).

See the [full bibliography](../reference/bibliography.md) for complete citation details.
