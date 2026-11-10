# Stochastic Programming

The stochastic extension enables robust capacity expansion planning under uncertainty by considering multiple future scenarios simultaneously. It is implemented in `master_problem.jl` via `create_stochastic_master_problem()`.

---


## 1. Overview


The deterministic Master Problem assumes perfect knowledge of future conditions -- fuel prices, demand growth and technology costs are all known with certainty. In practice, these parameters are subject to significant uncertainty over multi-decade planning horizons. The stochastic formulation [**[9]**](../reference/bibliography.md#ref9), [**[10]**](../reference/bibliography.md#ref10) addresses this by considering multiple **scenarios**, each representing a plausible future, and optimizing the **expected cost** across all scenarios.

The key insight of stochastic programming is the distinction between decisions that must be made before uncertainty is resolved (**here-and-now** decisions) and decisions that can adapt to realized conditions (**wait-and-see** decisions):

- **First-stage (here-and-now)**: Investment decisions \(I^{tech}_{y,k,n}\), \(I^{bat,P}_{y,k,n}\), \(I^{bat,E}_{y,k,n}\), \(I^{tr}_{y,i,j}\) and life extension of existing units are made once and must be the same across all scenarios. These represent infrastructure commitments that cannot be changed after construction.
- **Second-stage (wait-and-see)**: Operational dispatch decisions can differ by scenario. Each scenario has its own set of representative day dispatch variables reflecting how the system would operate under that particular realization of uncertainty.

---


## 2. Scenario Tree Structure

### 2.1 Scenario Definition


Each scenario \(\omega \in \Omega\) is defined by a probability weight and a set of parameter multipliers:

\[
\Omega = \{(\omega_1, \pi_1), (\omega_2, \pi_2), \ldots, (\omega_S, \pi_S)\} \quad \text{where } \sum_{s=1}^{S} \pi_s = 1
\tag{STOCH-1}
\]

The probability weights \(\pi_s\) represent the decision-maker's belief about the likelihood of each scenario. They must be non-negative and sum to one, forming a valid probability distribution.

### 2.2 Scenario Multipliers


The `ScenarioMultipliers` struct defines multiplicative factors applied to the base-case parameters. Only deviations from the base case are specified; every multiplier defaults to 1.0 (no change). In YAML they are the keys of a scenario's `multipliers` block.

| Multiplier | Applies to | Where |
|-----------|-----------|-------|
| `demand_growth` | Demand: \(D_{y,n,t} \cdot m^{dem}_s{}^{\,(y-1)}\). An annual factor: 1.02 = +2 %/year relative to the base case, compounding | Scenario operation |
| `fuel_cost` | Fuel cost of generators (piecewise fuel-cost curves included) and candidate technologies, and the fuel prices of the master's fuel balance (which fuel-coupled units pay instead of their `fuel_cost`) | Scenario operation |
| `fuel_price_growth` | Extra annual fuel price escalation: fuel cost and fuel prices × \(m^{fpg}_s{}^{\,(y-1)}\) | Scenario operation |
| `fuel_import_cost` | Per-node fuel import cost of the fuel balance | Scenario operation |
| `fuel_transport_cost` | Route transport cost (per unit·km) and route capacity investment | Scenario operation and investment cost |
| `penalty_loss_of_fuel_supply` | Fuel shortfall and excess-supply penalty (`loss_of_fuel_supply`) | Scenario operation |
| `electrolyzer_variable_cost` | Electrolyzer variable cost (per MWh drawn) | Scenario operation |
| `electrolyzer_water_cost` | Electrolyzer water cost (per kg of hydrogen) | Scenario operation |
| `electrolyzer_fixed_cost` | Electrolyzer fixed cost (existing and built power) | Scenario asset cost |
| `fuel_storage_invest_cost` | Fuel storage investment | Scenario investment cost |
| `electrolyzer_invest_cost` | Electrolyzer investment | Scenario investment cost |
| `gen_fixed_cost` | Fixed O&M (\$/MW/year) of generators, candidate technologies, batteries and battery technologies | Existing-asset cost (per year) |
| `maintenance_cost` | Maintenance cost of generators, batteries, technologies and battery technologies | Scenario operation |
| `carbon_price` | CO2 cost (`co2_cost`) | Scenario operation |
| `penalty_loss_of_load` | Loss-of-load penalty, and the sectoral penalties derived from it | Scenario operation |
| `penalty_loss_of_reserve` | Static and dynamic reserve shortfall penalties | Scenario operation |
| `penalty_curtailment` | Curtailment cost (existing renewables and candidate technologies) | Scenario operation |
| `penalty_fre_penetration` | RE-penetration shortfall penalty | Scenario operation |
| `penalty_co2_budget_violation` | Penalty per tonne over each system's annual CO2 budget | Scenario operation |
| `bat_maintenance_cost` | Maintenance cost of batteries and battery technologies only, on top of `maintenance_cost` | Scenario operation |
| `gen_start_up_cost` | Start-up cost of generators (LP start-ups of the master, when the dispatch commits units: `use_uc_in_dispatch`) | Scenario operation |
| `converter_variable_cost` | Variable cost of AC/DC and frequency converters | Scenario operation |
| `transmission_cost_per_mw_km` | Flow cost of inter-system links (`distance_km` × `cost_per_mw_km` per MWh carried) | Scenario operation |
| `invest_cost_renewables` | Investment cost of renewable technologies | Scenario investment cost |
| `invest_cost_conventional` | Investment cost of non-renewable technologies | Scenario investment cost |
| `invest_cost_storage` | Battery technology investment cost (power and energy) | Scenario investment cost |
| `bat_invest_cost_power` | Battery technology power cost (\$/MW), on top of `invest_cost_storage` | Scenario investment cost |
| `bat_invest_cost_capacity` | Battery technology energy cost (\$/MWh), on top of `invest_cost_storage` | Scenario investment cost |
| `invest_cost_transmission` | Transmission investment cost (inter-system links included) | Scenario investment cost |
| `converter_invest_cost` | Investment cost of AC/DC and frequency converters | Scenario investment cost |
| `gen_decommissioning_cost` | Decommissioning cost of existing generators | Scenario asset cost |
| `bat_decommissioning_cost` | Decommissioning cost of existing batteries | Scenario asset cost |
| `discount_rate` | Discount rate: \(r_s = r \cdot m^{disc}_s\) | Scenario discounting |

The operation multipliers are applied by `scenario_master_input()`, which builds a copy of the master input for each scenario and year; the investment, asset and discount multipliers act on the scenario's terms of the objective (§4). The fuel and electrolyzer multipliers act on the master's fuel block, which exists only when primary energy is on (see [Capacity Expansion](capacity-expansion.md#fuel-block)); without it they have no effect.

The loader also accepts the keys earlier Studio versions wrote, and translates them: `gen_fuel_cost` and `fuel_price` → `fuel_cost`, `gen_maintenance_cost` → `maintenance_cost`, `transmission_invest_cost` → `invest_cost_transmission`, `fuel_price_growth_rate` → `fuel_price_growth`, `penalty_co2_cost` → `carbon_price`, and `gen_invest_cost` → both `invest_cost_renewables` and `invest_cost_conventional`. When several keys scale the same multiplier, their values multiply (`gen_invest_cost: 1.1` with `invest_cost_renewables: 0.8` gives 0.88 for renewables and 1.1 for conventional technologies), with a warning. Any other key is ignored with a warning.

`gen_start_up_cost` only has an effect when the dispatch commits units (`master_problem.use_uc_in_dispatch`): only then does the master model start-ups (see [Capacity Expansion](capacity-expansion.md)). `transmission_cost_per_mw_km` scales the operating cost of the flow on inter-system links; their investment per MW is scaled by `invest_cost_transmission`. When a scenario sets one of these keys, or a fuel-chain or electrolyzer key with primary energy off, to a value other than 1 in a configuration that lacks its term, loading the configuration logs a warning naming the scenario and the key.

### 2.3 Scenario Construction


The effective parameter for scenario \(\omega_s\) is

\[
\theta_s = \theta^{base} \times m_s
\]

where \(m_s\) is the corresponding multiplier (raised to \(y-1\) for the annual growth factors). For example, a base fuel cost of 50 \$/MWh with `fuel_cost: 1.5` becomes 75 \$/MWh in that scenario.

---


## 3. Two-Stage Formulation

### 3.1 Mathematical Structure


The stochastic Master Problem has the following two-stage structure:

**First stage** (deterministic, common to all scenarios):

\[
\min_{I} \; C^{1st}(I) + \mathbb{E}_\omega\left[Q(I, \omega)\right]
\]

subject to investment constraints (TECH-1 to TECH-4, BUD-1, RET-2, TXN-1, CAP-1) that are scenario-independent. The RE target and increment bounds (RE-2, RE-3) hold per scenario (§5.2).

**Second stage** (recourse, scenario-dependent):

For each scenario \(\omega_s\), the recourse function \(Q(I, \omega_s)\) is the optimal operational cost given investment decisions \(I\) and scenario parameters \(\omega_s\):

\[
Q(I, \omega_s) = \min_{P,E,L,\ldots} \; C^{op}_s(P, E, L, \ldots)
\]

subject to operational constraints (power balance, generator limits, battery dynamics, etc.) parameterized by scenario \(\omega_s\) and linked to investment decisions through cumulative capacity expressions.

### 3.2 Non-Anticipativity


The non-anticipativity constraint is enforced implicitly: there is only one copy of the first-stage investment variables shared across all scenarios. This is equivalent to the explicit constraint:

\[
I^{tech,(\omega_1)}_{y,k,n} = I^{tech,(\omega_2)}_{y,k,n} = \ldots = I^{tech,(\omega_S)}_{y,k,n} \qquad \forall y, k, n
\]

and similarly for battery and transmission investment variables.

In the JuMP implementation, this is achieved by using a single set of investment variables that appear in the constraints of all scenarios. No additional constraints are needed.

Second-stage variables (representative day operational variables) are scenario-specific, each with its own index:

\[
P^{day,(\omega_s)}_{g,n,t} \text{ varies by scenario } s \quad \text{(separate variable set per scenario)}
\]

---


## 4. Objective Function

### 4.1 Stochastic Objective


The stochastic objective minimizes the expected total cost: each scenario values the common investment with its own cost multipliers and discount rate, adds the existing-asset costs (life extension, decommissioning) and its own operating cost, and is weighted by its probability:

\[
\min \; Z^{stoch} = \sum_{s=1}^{S} \pi_s \left[ C^{inv}_{s} + \sum_{y=1}^{Y} \frac{C^{asset}_{y,s} + C^{op}_{y,s}}{(1+r_s)^{y-1}} \right] + C^{slack}
\tag{STOCH-OBJ}
\]

\(C^{slack}\) holds the first-stage budget and adequacy slacks (OBJ-7 of [Capacity Expansion](capacity-expansion.md#53-slack-penalties)), discounted at the base rate.

Built by `build_stochastic_objective!()`.

### 4.2 First-Stage Investment Cost


Investment decisions are common to all scenarios, but their unit cost is uncertain: each scenario values the same investment with its own investment-cost multipliers and discount rate, as the annuities of OBJ-2 in [Capacity Expansion](capacity-expansion.md#51-investment-cost-annuities), and the objective takes the probability-weighted sum:

\[
C^{inv}_{s} = \sum_{u} m^{u}_{s} \, c_u \, I_u \, \mathrm{CRF}(r_s, L_u) \sum_{y = y_u}^{\min(Y,\; y_u + \lceil L_u \rceil - 1)} \frac{1}{(1+r_s)^{y-1}}
\]

where \(u\) runs over the builds (amount \(I_u\) built in year \(y_u\), overnight cost \(c_u\), lifetime \(L_u\)) and \(m^{u}_s\) is the scenario's multiplier for the build's kind: `invest_cost_renewables` or `invest_cost_conventional` by technology type, `invest_cost_storage` × `bat_invest_cost_power` or `bat_invest_cost_capacity` for battery technologies, `invest_cost_transmission` for transmission, `converter_invest_cost` for converters, and the fuel storage, transport and electrolyzer multipliers for the primary-energy builds. Reservoir investment is not scaled. Life extension and decommissioning of existing assets (\(C^{asset}_{y,s}\)) are added every year, discounted at \(r_s\); the decommissioning costs are multiplied by `gen_decommissioning_cost` and `bat_decommissioning_cost`.

### 4.3 Second-Stage Operational Cost


For each scenario \(s\) the operational cost comes from that scenario's own representative-day (or TSAM) dispatch, scaled to annual:

\[
C^{op}_{y,s} = \frac{\Delta t \cdot H_{year}/24}{|\mathcal{D}_y|} \sum_{d \in \mathcal{D}_y} C^{day}_{y,d,s}
\]

where \(C^{day}_{y,d,s}\) is computed with the scenario's demand, fuel, maintenance and CO2 costs and its penalty coefficients (load shedding, reserve shortfall, curtailment), as scaled by the multipliers of §2.2. \(C^{op}_{y,s}\) also holds the scenario's CO2-budget violation, the renewable energy above its RE growth cap (RE-3b) and its RE-target shortfall: each scenario has its own annual RE constraint (RE-2) over its own renewable energy and demand, with its own shortfall priced at `fre_penetration_loss` × `penalty_fre_penetration` (TSAM periods use their weights instead, see [Capacity Expansion](capacity-expansion.md#852-period-weights)).

### 4.4 Scenario-Adjusted Discount Rate


Each scenario can have a different effective discount rate:

\[
r_s = r^{base} \times m^{discount}_s
\]

where \(m^{discount}_s\) is the `discount_rate` multiplier of scenario \(s\). It discounts all of the scenario's costs, investment included. This allows modeling scenarios with different capital market conditions or risk premia.

### 4.5 Expected Cost Calculation


The expected total cost is a weighted sum over scenarios:

\[
\mathbb{E}_\omega[Z] = \sum_{s=1}^{S} \pi_s \cdot Z_s
\]

For example, with three scenarios (high demand, base case, green transition) with probabilities (0.3, 0.5, 0.2):

\[
\mathbb{E}[Z] = 0.3 \cdot Z_{high} + 0.5 \cdot Z_{base} + 0.2 \cdot Z_{green}
\]

The optimal investment plan minimizes this expected cost, hedging against all three futures simultaneously.

### 4.6 Slack Penalties


The RE-target, budget and capacity-adequacy slacks belong to the first stage and are penalised once, outside the scenario sum.

---


## 5. Constraint Structure

### 5.1 Scenario-Independent Constraints


The following constraints apply identically across all scenarios:

| Constraint Family | Reference | Description |
|-------------------|-----------|-------------|
| TECH-1 to TECH-4 | [Capacity Expansion](capacity-expansion.md#71-investment-limits-tech) | Cumulative investment limits |
| RET-2 | [Capacity Expansion](capacity-expansion.md#76-retirement-and-life-extension-ret) | Life-extension cascade |
| CAP-1 | [Capacity Expansion](capacity-expansion.md#74-capacity-adequacy-cap) | Capacity adequacy (base-case demand) |
| BUD-1 | [Capacity Expansion](capacity-expansion.md#72-budget-constraint-bud) | Annual investment budget |
| TXN-1 | [Capacity Expansion](capacity-expansion.md#73-transmission-symmetry-txn) | Transmission symmetry |

### 5.2 Scenario-Dependent Constraints


For each scenario \(s\), a separate set of representative-day operational constraints is created with the scenario's parameters:

| Constraint | Scenario parameter |
|-----------|-------------|
| Power balance (PB-1, DC-1) | Demand × `demand_growth`^(y−1) |
| Operational cost | Fuel × `fuel_cost` × `fuel_price_growth`^(y−1); maintenance × `maintenance_cost` |
| Annual RE target and minimum-increment floor (RE-2, RE-3a) | Hold in every scenario, over its own renewable energy and demand, with its own shortfall at `fre_penetration_loss` × `penalty_fre_penetration` |
| RE growth cap (RE-3b) | Every scenario's actual share grows at most `max_annual_increment` over its own share the year before (year 1: the initial share); energy above it at `loss_of_load` × `penalty_loss_of_load`. `re_penetration_ratio` is the probability-weighted renewable energy over the weighted demand |

Capacity adequacy (CAP-1) is built once, on the base-case demand. The cumulative capacity expressions (CUM-1 through CUM-4) link the common investment variables to each scenario's operation, so the same infrastructure serves all scenarios: a scenario whose demand outgrows the plan sheds load at VOLL, which the expected cost weighs against more investment.

---


## 6. Illustrative Example


Consider a three-scenario planning problem for a small island grid:

| Scenario | Probability | Key Assumptions |
|----------|------------|-----------------|
| High Demand | 0.3 | Demand growing 3 %/year faster than the base case, 10% higher fuel costs |
| Base Case | 0.5 | No modifications |
| Green Transition | 0.2 | 20% cheaper RE investment, 30% cheaper batteries |

**Configuration:**

```yaml
master_problem:
  stochastic: true

systems:
  island_grid:
    stochastic_scenarios:
      - name: high_demand
        probability: 0.3
        multipliers:
          demand_growth: 1.03      # +3 %/year above the base case
          fuel_cost: 1.1

      - name: base_case
        probability: 0.5

      - name: green_transition
        probability: 0.2
        multipliers:
          invest_cost_renewables: 0.8
          invest_cost_storage: 0.7
```

**Interpretation:** The optimizer will select investments that perform reasonably well across all three futures. If the "green transition" scenario strongly favors solar + storage, but the "high demand" scenario requires firm capacity, the stochastic solution will include some firm generation capacity that the purely green-transition deterministic solution might omit.

---


## 7. Computational Considerations

### 7.1 Problem Size


The stochastic formulation scales the second-stage problem linearly with the number of scenarios:

| Component | Deterministic | Stochastic (\(S\) scenarios) |
|-----------|--------------|-------------------------------|
| Investment variables | \(V_1\) | \(V_1\) (shared) |
| Operational variables | \(V_2\) | \(S \times V_2\) |
| Investment constraints | \(C_1\) | \(C_1\) (shared) |
| Operational constraints | \(C_2\) | \(S \times C_2\) |
| Total LP size | \(V_1 + V_2\), \(C_1 + C_2\) | \(V_1 + S \cdot V_2\), \(C_1 + S \cdot C_2\) |

For a typical Master Problem with 5 representative days, 25 years, and 3 nodes, the deterministic problem might have ~100,000 variables. With 3 scenarios, the stochastic problem grows to ~250,000 variables (investment variables shared, operational variables tripled).

### 7.2 Solution Time


Solution time scales roughly linearly with the number of scenarios for LP problems (simplex or barrier methods). For practical planning studies:

- **2--3 scenarios**: Minimal overhead, suitable for sensitivity analysis
- **5--10 scenarios**: Moderate computation, captures key uncertainties
- **20+ scenarios**: Significant computation; consider scenario reduction techniques

### 7.3 Representative Day Sharing


All scenarios share the same representative-day selection (from the base-case demand profile). Each scenario builds its own dispatch on those days.

When TSAM is enabled (`use_tsam: true`), TSAM periods and weights are computed once from the base case and shared across all scenarios. Each scenario has its own inter-period storage and reservoir chains; the first scenario's chains are the ones reported.

---


## 8. Implementation Notes

### 8.1 Implementation Details


- `create_stochastic_master_problem()` builds the investment structure once (`create_master_problem` without operation), then the operation of each scenario with `add_representative_days_validation!` or `add_tsam_periods_validation!` and `scenario = s`.
- `scenario_master_input()` gives each scenario and year its copy of the master input (demand, fuel, maintenance, CO2 cost).
- The solver sees a single LP with shared investment variables and per-scenario operational blocks.
- Investment results are the common plan. The operational dispatch that follows runs on the base case.
- Stochastic mode needs `solver_method: monolithic`; with Benders the scenarios are ignored with a warning.

### 8.2 Function Call Graph


```
create_stochastic_master_problem(input)
  |-- create_master_problem(base; use_representative_days=false)
  |     |-- build_master_variables!, investment, budget, adequacy,
  |     |   transmission symmetry and user constraints     # shared
  |-- for each scenario s:
  |     +-- add_representative_days_validation!(...; scenario=s)
  |           (or add_tsam_periods_validation!)
  |           +-- scenario_master_input(base, s, y)       # per year
  +-- build_stochastic_objective!(..., scenario_costs)    # expected cost
```

---


## 9. Relationship to Other Formulations


The stochastic extension is compatible with all other ESFEX features:

| Feature | Compatibility | Notes |
|---------|--------------|-------|
| MGA/SPORES | Not combined | MGA builds its own deterministic model |
| Benders | Not supported | The master is solved deterministically, with a warning |
| DC Power Flow | Full | Per-scenario representative-day flows |
| Primary Energy | Not in the master | Primary-energy constraints apply only in the operational dispatch, which runs on the base case |
| Multi-System | Full | Merged systems form one master; scenarios come from the first system |
| TSAM | Full | Shared periods, per-scenario operation and seasonal chains |
| NPV-based retirement | Full | With `npv_retirement` on, each unit's NPV is the probability-weighted mean over the scenarios, each with its own operation, costs and discount rate ([Capacity Expansion §9](capacity-expansion.md#9-npv-based-iterative-retirement)) |

---

## References

The two-stage stochastic programming formulation follows the framework of Birge and Louveaux [**[9]**](../reference/bibliography.md#ref9). Decision-making under uncertainty in electricity markets is treated by Conejo et al. [**[10]**](../reference/bibliography.md#ref10). The interaction between stochastic capacity expansion and unit commitment constraints is analyzed by Schwele et al. [**[35]**](../reference/bibliography.md#ref35). The EVPI and VSS metrics for quantifying the value of stochastic solutions are defined in Birge and Louveaux [**[9]**](../reference/bibliography.md#ref9) (Ch. 4).

See the [full bibliography](../reference/bibliography.md) for complete citation details.
