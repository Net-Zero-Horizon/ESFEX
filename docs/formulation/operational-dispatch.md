# Operational Dispatch


The operational dispatch determines the hourly generation schedule that meets demand at minimum cost while respecting all physical and policy constraints. It is implemented in `power_system.jl` and solved for each temporal window within each planning year. The window size is controlled by `rolling_horizon_hours`.

Three operating modes are available:

- **Economic dispatch (ED):** Generator commitment is fixed to 1; the problem is a continuous LP.
- **Unit commitment (UC):** Generator on/off status is a binary decision variable, yielding a MIP with minimum up/down times and startup costs.
- **Development:** Identical to ED but includes investment decision variables for capacity expansion screening.

---


## 1. Sets and Indices

| Symbol | Description | Julia Range |
|--------|-------------|-------------|
| \(g \in \mathcal{G}\) | Generators | `1:n_gen` |
| \(b \in \mathcal{B}\) | Batteries (storage systems) | `1:n_bat` |
| \(n \in \mathcal{N}\) | Buses (electrical nodes) | `1:n_bus` |
| \(i \in \mathcal{I}\) | Geographic nodes (each groups one or more buses) | `1:n_node` |
| \(t \in \mathcal{T}\) | Time steps of `resolution_hours` each | `1:hours` |
| \(s \in \mathcal{S}\) | Demand sectors | `keys(sectoral_demand)` |
| \(\ell \in \mathcal{L}\) | Transmission lines (physical) | `1:n_line` |
| \(k \in \mathcal{K}\) | AC/DC converters | `1:n_acdc` |
| \(f \in \mathcal{F}\) | Frequency converters | `1:n_freq` |

The mapping from bus to geographic node is given by `bus_to_node[n]`, written as \(n_i\). Each bus carries a `demand_fraction` \(\alpha_n\) of its parent node's demand; a bus with `role: connection` carries no demand (\(\alpha_n = 0\) in every balance and bound below).

Generators, batteries, load shedding, voltage angles and electrolyzer power are indexed by bus. Curtailment, the RE-target slack, CO2 emissions, reserves, sectoral load shedding, flexible demand, EV fleet and rooftop curtailment are indexed by node; where a node-level quantity enters a bus balance it is multiplied by \(\alpha_n\). Per-node configuration arrays (capacities, costs, `invest_max`) are mapped onto the buses before the model is built.

Generator and battery variables exist only for the (unit, bus) pairs with installed capacity; the model has no variables, and so no zero-capacity constraints, for the other pairs.

---


## 2. Decision Variables


### 2.1 Generator Variables

| Variable | Indices | Domain | Units | Description |
|----------|---------|--------|-------|-------------|
| \(p_{g,n,t}\) | \(g,n,t\) | \(\geq 0\) | MW | Generator output (`gen_output`) |
| \(u_{g,n,t}\) | \(g,n,t\) | \(\{0,1\}\) (UC) or \(=1\) (ED) | -- | Generator status (`gen_status`) |
| \(v_{g,n,t}\) | \(g,n,t\) | \([0,1]\) | -- | Startup indicator (`gen_startup`); UC mode only |

### 2.2 Battery Variables

| Variable | Indices | Domain | Units | Description |
|----------|---------|--------|-------|-------------|
| \(p^{\text{ch}}_{b,n,t}\) | \(b,n,t\) | \(\geq 0\) | MW | Charge power (`bat_charge`) |
| \(p^{\text{dis}}_{b,n,t}\) | \(b,n,t\) | \(\geq 0\) | MW | Discharge power (`bat_discharge`) |
| \(E_{b,n,t}\) | \(b,n,t\) | \(\geq 0\) | MWh | State of charge (`bat_soc`); index 1 = initial |
| \(\delta^{\text{ch}}_{b,n,t}\) | \(b,n,t\) | \([0,1]\) | -- | Charge status for mutual exclusivity (`bat_charge_status`) |
| \(\sigma_{b,n,t}\) | \(b,n,t\) | \(\geq 0\) | MWh | SOC violation slack (`soc_violation`) |
| \(\text{spill}_{b,n,t}\) | \(b,n,t\) | \(\geq 0\) | MW | Battery spillage (`bat_spillage`); optional per battery |

### 2.3 Network Variables

| Variable | Indices | Domain | Units | Description |
|----------|---------|--------|-------|-------------|
| \(f_{\ell,t}\) | \(\ell,t\) | free | MW | Power flow per physical line (`power_flow`) |
| \(\theta_{n,t}\) | \(n,t\) | \([-\pi, \pi]\) | rad | Voltage angle (`voltage_angle`) |
| \(\mu_{i,j,t}\) | \(i,j,t\) | \(\geq 0\) | MW | Transfer margin slack (`transfer_margin`). Created only when the network has no transmission lines; it appears in no constraint, so it is always 0 |

### 2.4 Converter Variables

| Variable | Indices | Domain | Units | Description |
|----------|---------|--------|-------|-------------|
| \(p^{\text{rect}}_{k,t}\) | \(k,t\) | \(\geq 0\) | MW | AC-to-DC rectification power (`acdc_rectify`) |
| \(p^{\text{inv}}_{k,t}\) | \(k,t\) | \(\geq 0\) | MW | DC-to-AC inversion power (`acdc_invert`) |
| \(p^{A \to B}_{f,t}\) | \(f,t\) | \(\geq 0\) | MW | Frequency converter A-to-B flow (`freq_flow_a_to_b`) |
| \(p^{B \to A}_{f,t}\) | \(f,t\) | \(\geq 0\) | MW | Frequency converter B-to-A flow (`freq_flow_b_to_a`) |

### 2.5 Reserve and Reliability Variables

| Variable | Indices | Domain | Units | Description |
|----------|---------|--------|-------|-------------|
| \(r^{\text{sta}}_{n,t}\) | \(n,t\) | \(\geq 0\) | MW | Static reserve provision (`reserve_static`) |
| \(r^{\text{dyn}}_{n,t}\) | \(n,t\) | \(\geq 0\) | MW | Dynamic reserve provision (`reserve_dynamic`) |
| \(\lambda^{\text{sta}}_{n,t}\) | \(n,t\) | \(\geq 0\) | MW | Static reserve shortage (`reserve_static_loss`) |
| \(\lambda^{\text{dyn}}_{n,t}\) | \(n,t\) | \(\geq 0\) | MW | Dynamic reserve shortage (`reserve_dynamic_loss`) |
| \(\text{LS}_{n,t}\) | \(n,t\) | \(\geq 0\) | MW | Load shedding per bus (`load_shed`) |
| \(\text{CU}_{i,t}\) | \(i,t\) | \(\geq 0\) | MW | Renewable curtailment per node (`curtailment`) |
| \(\phi_{i,t}\) | \(i,t\) | \(\geq 0\) | MW | RE penetration loss slack per node (`fre_penetration_loss`) |
| \(H_t\) | \(t\) | \(\geq 0\) | MW-s | Inertia shortfall (`loss_of_inertia`) |

### 2.6 Emissions Variables

| Variable | Indices | Domain | Units | Description |
|----------|---------|--------|-------|-------------|
| \(\text{CO2}_{i,t}\) | \(i,t\) | \(\geq 0\) | tonnes | CO2 emissions at node and hour (`co2_emissions`) |
| \(\xi^{\text{CO2}}\) | scalar (one per system in a multi-system run) | \(\geq 0\) | tonnes | CO2 budget violation slack (`co2_budget_violation`) |

### 2.7 EV Fleet Variables

| Variable | Indices | Domain | Units | Description |
|----------|---------|--------|-------|-------------|
| \(p^{\text{ev,ch}}_{n,t}\) | \(n,t\) | \(\geq 0\) | MW | EV charging power (`ev_charging`) |
| \(p^{\text{v2g}}_{n,t}\) | \(n,t\) | \(\geq 0\) | MW | Vehicle-to-grid power (`ev_v2g`) |
| \(E^{\text{ev}}_{n,t}\) | \(n,t\) | \(\geq 0\) | MWh | EV fleet SOC (`ev_soc`); index 1 = initial |
| \(\ell^{\text{ev}}_{n,t}\) | \(n,t\) | \(\geq 0\) | MWh | Unserved driving energy (`ev_loss`) |
| \(\delta^{\text{ev}}_{n,t}\) | \(n,t\) | \([0,1]\) | -- | EV charge/V2G mutex status (`ev_charge_status`) |

### 2.8 Sectoral Demand Variables

| Variable | Indices | Domain | Units | Description |
|----------|---------|--------|-------|-------------|
| \(\text{LOL}_{s,n,t}\) | \(s,n,t\) | \(\geq 0\) | MW | Per-sector load shedding (`loss_of_load_sectoral`) |
| \(\Delta_{s,n,t,t'}\) | \(s,n,t,t'\) | \(\geq 0\) | MW | Demand of flexible sector \(s\) shifted from step \(t\) to \(t'\) (`demand_shift`, `shift_{s}_{n}_{t}_{t'}`) |

### 2.9 Other Variables

| Variable | Indices | Domain | Units | Description |
|----------|---------|--------|-------|-------------|
| \(p^{\text{elz}}_{n,t}\) | \(n,t\) | \([0, \bar{P}^{\text{elz}}_n]\) | MW | Electrolyzer power consumption, summed over the system's electrolyzers (`electrolyzer_power[e,n,t]`); each one is at most the rated power; consecutive steps differ by at most `ramp_up` / `ramp_down` × rated power × \(\Delta t\) (no limit when the ramp is 0) |
| \(\text{RC}_{n,t}\) | \(n,t\) | \(\geq 0\) | MW | Rooftop solar curtailment (`rooftop_curtailment`) |

---


## 3. Objective Function


The total system cost over the window is minimized. Terms priced per MWh are written below as sums of MW over time steps; in the model they are multiplied by the time step \(\Delta t\) (`resolution_hours`). Annual terms (the fixed O&M of generators, batteries and electrolyzers) are multiplied by the window's share of the year, \(T \Delta t / 8760\) (`hours_per_year`), so the windows of a year add up to one annual charge. Start-ups are events inside the window and are charged in full:

\[
\min \; Z = \Delta t \left( Z^{\text{op}} + Z^{\text{bat}} + Z^{\text{pen}} + Z^{\text{sec}} + Z^{\text{CO2}} + Z^{\text{conv}} + Z^{\text{elz}} + Z^{\text{shift}} + Z^{\text{spill}} \right) + \frac{T \Delta t}{8760} \left( Z^{\text{fix}} + Z^{\text{bat,fix}} + Z^{\text{elz,fix}} \right) + Z^{\text{start}} \tag{OBJ}
\]

Implemented in `build_objective!()`. A few terms are added to the objective by the constraint builders that create their variables: the converter cost (OBJ-11, `charge_converter_costs!`), the year-end battery SOC shortfall (BAT-5), the N-1 shortfall (N1-1, N1-2), the line flow cost (OBJ-17) and the cost-ceiling violation (COST-1).

### 3.1 Operational Costs

\[
Z^{\text{op}} = \sum_{g,n,t} \left( c^{\text{fuel}}_{g,n} + c^{\text{maint}}_{g,n} \right) p_{g,n,t} \tag{OBJ-1}
\]

where \(c^{\text{fuel}}_{g,n}\) and \(c^{\text{maint}}_{g,n}\) are the per-MWh fuel and maintenance (variable O&M) costs. The sums are restricted to buses where \(\bar{P}_{g,n} > 0\).

#### 3.1a Generator Fixed O&M

\[
Z^{\text{fix}} = \sum_{g,n} c^{\text{fix}}_{g,n} \, \bar{P}^{\text{eff}}_{g,n} \tag{OBJ-1a}
\]

with \(c^{\text{fix}}_{g,n}\) the generator's `fixed_cost` ($ per MW per year), \(\bar{P}^{\text{eff}}_{g,n}\) its power in service (DEG-1: degraded, 0 past its `life_time`). It is charged whether or not the unit runs: an annual term, prorated to the window in (OBJ), and reported as `fixed_om_cost` with the batteries' (OBJ-3c). After a master run the dispatch receives the master's capacity (degraded, life-extended, built) as rated power, so the dispatch and the master charge the same MW. Earlier versions charged `fixed_cost` per MWh generated.

#### 3.1.1 Piecewise-Linear (PWL) Fuel Cost Decomposition

When a generator has a `fuel_cost_curve` configured (see [Configuration Reference](../reference/config-reference.md#costcurveconfig)), the flat fuel cost \(c^{\text{fuel}}_{g,n}\) is replaced by a piecewise-linear (PWL) cost formulation that decomposes the generator output into segments with increasing marginal costs.

**Segment variables.** The generator output is decomposed into \(K\) segments:

\[
p_{g,n,t} = \sum_{k=1}^{K} p^{(k)}_{g,n,t} \tag{PWL-1}
\]

Each segment is bounded by the segment's fraction of rated capacity:

\[
0 \leq p^{(k)}_{g,n,t} \leq f_k \cdot \bar{P}^{\text{eff}}_{g,n} \quad \forall k \in \{1, \ldots, K\} \tag{PWL-2}
\]

where \(f_k\) is the fraction of rated capacity assigned to segment \(k\) (with \(\sum_k f_k = 1\)) and \(\bar{P}^{\text{eff}}_{g,n}\) is the effective rated capacity at bus \(n\).

**Segment costs.** Each segment has a marginal cost \(c_k\) ($/MWh), and the total fuel cost for the generator replaces the flat-cost term in Eq. (OBJ-1):

\[
Z^{\text{op,pwl}}_{g,n} = \sum_{t} \sum_{k=1}^{K} c_k \cdot p^{(k)}_{g,n,t} \tag{PWL-3}
\]

**Convexity.** The marginal costs must be non-decreasing (\(c_1 \leq c_2 \leq \cdots \leq c_K\)). This ensures that the LP solver fills the cheaper segments first, which is necessary for the decomposition to correctly represent the cost curve without requiring explicit ordering constraints.

**Flat curve special case.** When `curve_type = "flat"`, \(K = 1\), \(f_1 = 1\) and \(c_1\) is `flat_price`, so the unit's output is priced at `flat_price` instead of its fuel cost (one segment variable per step). Without a `flat_price` (or with `flat_price` equal to the fuel cost), or with no curve configured, \(c_1 = c^{\text{fuel}}_{g,n}\) and the formulation reduces exactly to the original Eq. (OBJ-1) with no additional variables or constraints. The master problem follows the same rule for generators, batteries and candidate technologies.

**Stepwise curves.** Each block in the `blocks` list directly maps to a segment: \(f_k = \text{block}_k.\text{fraction}\), \(c_k = \text{block}_k.\text{price}\).

**Linear and exponential curves.** The continuous curve is approximated by `num_segments` equal-width segments. The marginal cost for each segment is evaluated at the segment midpoint.

!!! note "Battery discharge cost curves"
    The same PWL decomposition is available for battery discharge via `discharge_cost_curve`. In this case, the discharge power \(p^{\text{dis}}_{b,n,t}\) is decomposed into segments with non-decreasing marginal costs, and the total discharge cost replaces the flat maintenance cost in Eq. (OBJ-3):

    \[
    p^{\text{dis}}_{b,n,t} = \sum_{k=1}^{K} p^{\text{dis},(k)}_{b,n,t}, \quad Z^{\text{bat,pwl}}_{b,n} = \sum_{t} \sum_{k=1}^{K} c^{\text{dis}}_k \cdot p^{\text{dis},(k)}_{b,n,t}
    \]

    A flat curve (one segment) prices the whole discharge at its `flat_price`, as for generators; a flat curve at the battery's `throughput_degradation_cost` changes nothing and is skipped.

### 3.2 Startup Costs (UC mode only)

\[
Z^{\text{start}} = \sum_{g,n,t} c^{\text{su}}_{g,n} \, v_{g,n,t} \tag{OBJ-2}
\]

Each start costs its full `start_up_cost`, in the objective and in the `startup_cost` breakdown; it is not prorated like the annual one-off terms.

### 3.3 Battery Maintenance Cost

\[
Z^{\text{bat}} = \sum_{b,n,t} c^{\text{bat,maint}}_{b,n} \left( p^{\text{ch}}_{b,n,t} + p^{\text{dis}}_{b,n,t} \right) \tag{OBJ-3}
\]

### 3.3b Battery Throughput Degradation Cost

\[
Z^{\text{bat,thr}} = \sum_{b,n,t} c^{\text{thr}}_{b,n} \cdot p^{\text{dis}}_{b,n,t} \tag{OBJ-3b}
\]

Represents the wear cost of cycling energy through the battery, modelling the economic impact of throughput-related degradation on cell lifetime. The cost is applied to **discharge only** (each MWh discharged represents one full unit of cycled energy). Typical values range from 2 $/MWh (flow batteries) to 15 $/MWh (lead-acid), with Li-ion around 5 $/MWh.

- \(c^{\text{thr}}_{b,n}\) — throughput degradation cost ($/MWh discharged), from `bat.throughput_degradation_cost[n]`
- Defaults to 0 when not specified (backward-compatible)

### 3.3c Battery Fixed Cost

\[
Z^{\text{bat,fix}} = \sum_{b,n} c^{\text{fix}}_{b,n} \, P^{\text{dis}}_{b,n} \tag{OBJ-3c}
\]

with \(c^{\text{fix}}_{b,n}\) the battery's `fixed_cost` ($ per MW per year) and \(P^{\text{dis}}_{b,n}\) its rated `MaxDischargePower` (not degraded; none when BAT-1's degraded rating is 0, past its `life_time`). An annual term, prorated to the window in (OBJ); it is reported with the generators' fixed O&M (OBJ-1a, `fixed_om_cost`). The master problem charges it each year on the discharge power in service.

### 3.4 Penalty Costs

\[
Z^{\text{pen}} = \underbrace{C^{\text{VOLL}} \sum_{i,t} m_{i,t} \Big(\sum_{n \in i} \text{LS}_{n,t} - \sum_{s} \text{LOL}_{s,i,t}\Big)}_{\text{load shedding not attributed to a sector}} + \underbrace{C^{\text{CU}} \sum_{i,t} \text{CU}_{i,t}}_{\text{curtailment}} + \underbrace{C^{\text{r,sta}} \sum_{i,t} \lambda^{\text{sta}}_{i,t}}_{\text{static reserve}} + \underbrace{C^{\text{r,dyn}} \sum_{i,t} \lambda^{\text{dyn}}_{i,t}}_{\text{dynamic reserve}} \tag{OBJ-4a}
\]

\[
+ \underbrace{C^{\text{ine}} \sum_t H_t}_{\text{inertia}} + \underbrace{C^{\text{ev}} \sum_{n,t} \ell^{\text{ev}}_{n,t}}_{\text{EV loss}} + \underbrace{C^{\text{FRE}} \sum_{n,t} \phi_{n,t}}_{\text{RE target}} + \underbrace{C^{\text{SOC}} \sum_{b,n,t} \sigma_{b,n,t}}_{\text{SOC violation}} \tag{OBJ-4b}
\]

\[
+ \underbrace{C^{\text{TM}} \sum_{i,j,t} \mu_{i,j,t}}_{\text{transfer margin}} + \underbrace{C^{\text{CO2,bud}} \, \xi^{\text{CO2}}}_{\text{CO2 budget violation}} + \underbrace{C^{\text{RC}} \sum_{n,t} \text{RC}_{n,t}}_{\text{rooftop curtailment}} \tag{OBJ-4c}
\]

Without sectoral demand the \(\text{LOL}\) terms are absent, \(m_{i,t} = 1\) and all shed load costs \(C^{\text{VOLL}}\). With sectors, each sector's shed load is priced through \(Z^{\text{sec}}\) (OBJ-5), which carries VOLL times the sector's criticality; only the remainder that no sector accounts for (SEC-2) is priced here, so no shed MWh is counted twice. The remainder's multiplier is the demand-weighted mean of the node's sector criticalities in that step,

\[
m_{i,t} = \frac{\sum_s \kappa_s D^{\text{sec}}_{s,i,t}}{\sum_s D^{\text{sec}}_{s,i,t}},
\]

and 1 where no sector has demand at the node. Curtailment is priced at `penalties.curtailment_cost` (default 20 \$/MWh); there is no hard curtailment cap in the dispatch (the ratio cap of the master problem is described in [Capacity Expansion](capacity-expansion.md#836-curtailment-limit-per-day)). The transfer-margin slack \(\mu\) is always 0 (see §2.3).

### 3.5 Sectoral Load Shedding Penalties

\[
Z^{\text{sec}} = \sum_{s,n,t} C^{\text{VOLL}} \cdot \kappa_s \cdot \text{LOL}_{s,n,t} \tag{OBJ-5}
\]

where \(\kappa_s\) is the criticality weight for sector \(s\). Higher criticality sectors incur a higher penalty per unit of load shed, causing the optimizer to shed lower-criticality sectors first.

### 3.6 CO2 Emission Cost

\[
Z^{\text{CO2}} = C^{\text{CO2}} \sum_{g,n,t} \frac{e_{\text{fuel}(g)}}{\eta_{g,n}} \, p_{g,n,t} \tag{OBJ-6}
\]

where \(e_f\) is the emission factor of the fuel of generator \(g\) in tonnes CO2 per MWh of fuel (`fuels.<fuel>.emission_factor`), and \(\eta_{g,n}\) the unit's efficiency at rated power at node \(n\) (`eff_at_rated`), so \(e_f/\eta_{g,n}\) is t per MWh of electricity. A unit without an efficiency (\(\leq 0\)) uses \(e_f\) directly.

### 3.7 Flexible Demand

Flexible demand is shifted in time (SHIFT-1, OBJ-13), not curtailed: reducing a sector's demand is load shedding, priced by its criticality (OBJ-5). There is no flexible-curtailment benefit term.

### 3.8 V2G Compensation (reported, not in the objective)

\[
Z^{\text{V2G}} = \sum_{n,t} \pi_t \, p^{\text{v2g}}_{n,t} \tag{OBJ-8}
\]

The payment to EV owners for V2G energy, priced at the electricity price \(\pi_t\) (or at the constant `v2g_compensation` when no price is given). It is a transfer between agents, not a system cost: the value of V2G is already the generation it displaces in the power balance. It is therefore computed and reported in the cost breakdown (`v2g_compensation`) but not subtracted from the objective.

### 3.9 Battery Spillage Cost

\[
Z^{\text{spill}} = \sum_{b,n,t} \pi_t \cdot \text{spill}_{b,n,t} \tag{OBJ-9}
\]

Opportunity cost of energy discharged from batteries without grid injection, at the electricity price \(\pi_t\) (1 % of VOLL when no price vector is given).

### 3.10 Investment Costs

The dispatch has no investment costs: it builds nothing. In a `development` run each year's dispatch runs as `economic_dispatch` (or `unit_commitment` with `use_uc_in_dispatch`) on the capacity the master problem keeps and builds, which arrives as existing capacity. It still counts as a `development` run for `n1_security.apply_to_modes` and for the fuel model's end-of-window tank fill ([Primary Energy](primary-energy.md), PE-5).

### 3.11 Converter Costs

\[
Z^{\text{conv}} = \sum_{k,t} c^{\text{var}}_k \left( p^{\text{rect}}_{k,t} + p^{\text{inv}}_{k,t} \right) + \sum_{f,t} c^{\text{var}}_f \left( p^{A \to B}_{f,t} + p^{B \to A}_{f,t} \right) \tag{OBJ-11}
\]

Added by `charge_converter_costs!()` after `add_converter_constraints!()` has created the converter variables (`build_objective!()` runs before them), and reported as `converter_cost`.

### 3.12 Electrolyzer Costs

\[
Z^{\text{elz}} = \sum_{n,t} \left(c^{\text{elz,var}}_n + c^{\text{water}} h_n\right) p^{\text{elz}}_{n,t} \tag{OBJ-12}
\]

\[
Z^{\text{elz,fix}} = \sum_n \bar{P}^{\text{elz}}_n \, c^{\text{elz,fix}}_n \tag{OBJ-12b}
\]

with \(c^{\text{water}}\) the `water_cost` (\$/kg), \(h_n\) the kg of hydrogen per MWh drawn ([Electrolyzer](electrolyzer.md#5-objective-terms)) and \(c^{\text{elz,fix}}_n\) the `fixed_cost` (\$ per MW per year), an annual term prorated to the window in (OBJ). \(\bar{P}^{\text{elz}}_n\) includes the electrolyzer power the master built. Both are reported as `electrolyzer_cost`. Earlier versions charged `fixed_cost` per MW and per hour.

### 3.13 Demand Shifting Cost

\[
Z^{\text{shift}} = \sum_{s,n,t,t'} |t - t'| \, \Delta t \cdot \gamma^{\text{shift}} \cdot \Delta_{s,n,t,t'} \tag{OBJ-13}
\]

where \(\gamma^{\text{shift}}\) is the `demand_shift_cost_rate` (\$ per MWh and per hour moved) and \(|t - t'| \, \Delta t\) the shift distance in hours.

### 3.14 Reservoir Costs

Spilled reservoir water is charged at the electricity price \(\pi_t\) (1 % of VOLL when no price vector is given). See [Reservoir Hydropower](reservoir-hydro.md).

### 3.15 Line Flow Cost

Lines with a `cost_per_mw_km` (the inter-system links of a merged multi-system run) pay \(c^{\text{flow}}_l\) = `length_km × cost_per_mw_km` (\$/MWh, the `flow_cost` of `TransmissionLineData`) for the energy they carry in either direction, as the master does:

\[
Z^{\text{flow}} = \Delta t \sum_{l,t} c^{\text{flow}}_l \, a_{l,t}, \qquad a_{l,t} \geq f_{l,t}, \quad a_{l,t} \geq -f_{l,t} \tag{OBJ-17}
\]

(`line_flow_abs_l*_t*_pos/neg`, `add_line_flow_costs!`), reported as `line_flow_cost`. Lines without a cost get no variables. It needs the per-line DC flows, so it has no effect in ACOPF runs.

!!! note "Complete objective"
    The objective is (OBJ) above. \(Z^{\text{bat}}\) includes the throughput cost (OBJ-3b), \(Z^{\text{pen}}\) the EV, SOC, RE-target and rooftop penalties, and \(Z^{\text{spill}}\) both battery and reservoir spillage. The V2G compensation (OBJ-8) is reported but not part of the objective.

---


## 4. Constraint Families


### Generator Constraints

Implemented in `add_generator_constraints!()`.

#### GEN-1: Generator Capacity Limits

Every generator is limited by its capacity times its availability factor \(a_{g,n,t} \in [0,1]\):

\[
p_{g,n,t} \leq \bar{P}^{\text{eff}}_{g,n} \cdot a_{g,n,t} \quad \forall g, n, t \tag{GEN-1a}
\]

For renewables \(a\) is the capacity-factor profile; for other units it is 1 by default. A [scheduled interruption](../user-guide/scheduled-interruptions.md) multiplies \(a\) by the outage factor of its window (0 for a full outage), for any unit type. \(\bar{P}^{\text{eff}}\) is the rated power in service (§5: degraded, 0 past the lifetime).

In unit commitment, non-renewable units and reservoir hydro are also tied to their commitment status:

\[
p_{g,n,t} \leq M_{g,n} \cdot u_{g,n,t}, \qquad M_{g,n} = \max\left(1.1 \cdot \max(\mathbf{D}),\; 10^4,\; 1.1 \, \bar{P}_{g,n}\right) \tag{GEN-1b}
\]

The big-M is per generator so that it never caps a large unit below its own capacity. In economic dispatch \(u = 1\) and GEN-1b is not added.

**Configuration parameters:**

- `generators[g].rated_power[n]` -- installed capacity (MW)
- `generators[g].availability[t,n]` -- time-varying availability profile

#### GEN-2: Minimum Generation (UC mode only)

\[
p_{g,n,t} \geq \underline{P}_{g,n} \cdot \bar{P}_{g,n} \cdot u_{g,n,t} \quad \forall g, n, t \tag{GEN-2}
\]

where \(\underline{P}_{g,n}\) is the minimum stable generation fraction (`min_power[n]`) and \(\bar{P}_{g,n}\) the degraded rated power, without investment. This ensures that when a generator is committed (\(u=1\)), it produces at least its minimum output.

#### GEN-3: Ramp Rate Constraints

\[
p_{g,n,t} - p_{g,n,t-1} \leq \bar{P}^{\text{eff}}_{g,n} \cdot \min\left(1, R^{\text{up}}_{g,n} \Delta t\right) \quad \forall g, n, t \geq 2 \tag{GEN-3a}
\]

\[
p_{g,n,t-1} - p_{g,n,t} \leq \bar{P}^{\text{eff}}_{g,n} \cdot \min\left(1, R^{\text{down}}_{g,n} \Delta t\right) \quad \forall g, n, t \geq 2 \tag{GEN-3b}
\]

where \(R^{\text{up}}_{g,n}\) and \(R^{\text{down}}_{g,n}\) are ramp rates in fractions of capacity per hour (`ramp_up[n]`, `ramp_down[n]`) and \(\Delta t\) is the time step (`resolution_hours`): with 6-hour steps a unit with `ramp_up: 0.1` may rise 60 % of its capacity from one step to the next. A rate of 0 means no ramp limit: the constraint is omitted (a literal zero would freeze the unit at its initial output). In rolling-horizon dispatch the same limits also apply between the last committed hour of one window and the first hour of the next.

**Configuration parameters:**

- `generators[g].ramp_up[n]` -- ramp up limit as fraction of capacity per hour
- `generators[g].ramp_down[n]` -- ramp down limit as fraction of capacity per hour

#### GEN-4: Startup Detection (UC mode only)

\[
v_{g,n,t} \geq u_{g,n,t} - u_{g,n,t-1} \quad \forall g, n, t \tag{GEN-4}
\]

For \(t=1\), the previous status \(u_{g,n,0}\) is taken from `generator_initial_status`. The startup variable \(v_{g,n,t}\) captures the transition from off to on.

#### GEN-5: Minimum Up/Down Time (UC mode only)

**Minimum up time:** once a generator starts, it must remain on for at least \(T^{\text{up}}_g\) hours:

\[
u_{g,n,t} \geq u_{g,n,\tau} - u_{g,n,\tau-1} \quad \forall g, n, t, \; \tau \in [\max(1, t - T^{\text{up}}_g + 1),\, t] \tag{GEN-5a}
\]

**Minimum down time:** once a generator shuts down, it must remain off for at least \(T^{\text{down}}_g\) hours:

\[
1 - u_{g,n,t} \geq u_{g,n,\tau-1} - u_{g,n,\tau} \quad \forall g, n, t, \; \tau \in [\max(1, t - T^{\text{down}}_g + 1),\, t] \tag{GEN-5b}
\]

**Configuration parameters:**

- `generators[g].min_up_time[n]` -- minimum on-time (hours)
- `generators[g].min_down_time[n]` -- minimum off-time (hours)

The windows above count time steps: \(T^{\text{up}}_g\) and \(T^{\text{down}}_g\) are the minimum times in hours divided by the time step (`resolution_hours`) and rounded up, so 8 h at 6-hour steps spans 2 steps.

---


### Battery Constraints

Implemented in `add_battery_constraints!()`.

#### BAT-1: Charge/Discharge Power Limits

\[
p^{\text{ch}}_{b,n,t} \leq \bar{P}^{\text{ch}}_{b,n} \, o_{b,t} \quad \forall b, n, t \tag{BAT-1a}
\]

\[
p^{\text{dis}}_{b,n,t} \leq \bar{P}^{\text{dis}}_{b,n} \, o_{b,t} \quad \forall b, n, t \tag{BAT-1b}
\]

where \(\bar{P}^{\text{ch}}_{b,n}\) and \(\bar{P}^{\text{dis}}_{b,n}\) are the maximum charge and discharge power ratings in service (degraded, 0 past the lifetime, DEG-2) and \(o_{b,t} \in [0,1]\) is the scheduled-interruption factor (1 outside outage windows) times the battery's availability profile `Availability` (1 without one).

#### BAT-2: SOC Dynamics

\[
E_{b,n,t+1} = E_{b,n,t} \cdot (1 - \rho_b) + \left( \eta^{\text{ch}}_b \, p^{\text{ch}}_{b,n,t} - \frac{p^{\text{dis}}_{b,n,t}}{\eta^{\text{dis}}_b} - \text{spill}_{b,n,t} \right) \Delta t \quad \forall b, n, t \tag{BAT-2}
\]

where:

- \(\rho_b\) is the self-discharge rate per time step (`self_discharge[n]`)
- \(\Delta t\) is the time step (`resolution_hours`): power in MW becomes energy in MWh
- \(\eta^{\text{ch}}_b\) is the charging efficiency (`charge_efficiency[n]`)
- \(\eta^{\text{dis}}_b\) is the discharging efficiency (`discharge_efficiency[n]`)
- \(\text{spill}_{b,n,t}\) is an optional spillage term for batteries that allow energy release without grid injection

#### BAT-3: Initial SOC

\[
E_{b,n,1} = E^{0}_{b,n} \quad \forall b, n \tag{BAT-3}
\]

where \(E^{0}_{b,n} = \max(\text{soc\_initial}_{b,n}, \underline{E}_b) \times \bar{E}^{\text{eff}}_{b,n}\) is the initial energy stored, given as a fraction of capacity and raised to `soc_min` if below it. In rolling-horizon dispatch every window after the first starts instead from the energy (MWh) the previous window left, clamped to \([\underline{E}_b \bar{E}^{\text{eff}}_{b,n}, \bar{E}^{\text{eff}}_{b,n}]\) (§10.2).

#### BAT-4: SOC Bounds (Soft Upper)

\[
E_{b,n,t+1} \geq \underline{E}_b \cdot \bar{E}^{\text{eff}}_{b,n} \quad \forall b, n, t \tag{BAT-4a}
\]

\[
E_{b,n,t+1} \leq \overline{E}_b \cdot \bar{E}^{\text{eff}}_{b,n} + \sigma_{b,n,t} \quad \forall b, n, t \tag{BAT-4b}
\]

where \(\underline{E}_b\) and \(\overline{E}_b\) are the minimum and maximum SOC fractions (`soc_min[n]`, `soc_max[n]`), and \(\bar{E}^{\text{eff}}_{b,n}\) is the effective capacity (including investment). The slack variable \(\sigma_{b,n,t}\) allows soft violation of the upper bound, penalized heavily in the objective (\(C^{\text{SOC}}\)).

**Configuration parameters:**

- `batteries[b].soc_min[n]` -- minimum SOC as fraction of capacity
- `batteries[b].soc_max[n]` -- maximum SOC as fraction of capacity
- `soc_violation_penalty` -- penalty coefficient for SOC upper bound violation

#### BAT-5: End-of-Year SOC Floor

Inside a year the rolling windows chain the SOC (each window starts from the SOC where the previous one's kept part ended), so intermediate windows have no end-of-window condition. Only the last window of each year (or the single window when `use_rolling_horizon: false`) gets a soft floor at the SOC the battery had at the start of the year, \(E^{\text{year}}_{b,n}\):

\[
E_{b,n,T+1} + s^{\text{end}}_{b,n} \geq E^{\text{year}}_{b,n} \cdot (1 - \epsilon), \qquad s^{\text{end}}_{b,n} \geq 0 \quad \forall b, n \tag{BAT-5}
\]

where \(\epsilon\) is the `soc_end_tolerance`. The shortfall \(s^{\text{end}}\) costs \(0.1 \cdot C^{\text{LOL}} / \Delta t\) per MWh in the objective: well above any generation cost, so the battery is refilled from generation, but below the cost of load shedding, so load is never shed to refill it, and a last window too short to refill stays feasible.

!!! note "Why the year-end floor matters"
    Each year restarts from the configured `soc_initial`. Without the floor the last window would empty the batteries and the year would get that stored energy for free. The master problem applies the same rule (year-cyclic SOC).

#### BAT-6: Charge/Discharge Mutual Exclusivity

\[
p^{\text{ch}}_{b,n,t} \leq M_{\text{bat}} \cdot \delta^{\text{ch}}_{b,n,t} \quad \forall b, n, t \tag{BAT-6a}
\]

\[
p^{\text{dis}}_{b,n,t} \leq M_{\text{bat}} \cdot (1 - \delta^{\text{ch}}_{b,n,t}) \quad \forall b, n, t \tag{BAT-6b}
\]

where \(M_{\text{bat}} = 2 \max(\bar{P}^{\text{ch}}_{b,n}, \bar{P}^{\text{dis}}_{b,n})\). \(\delta^{\text{ch}}\) is continuous in \([0,1]\), so this is a relaxation: it discourages, but does not strictly forbid, simultaneous charging and discharging.

#### BAT-7: Minimum Cycling Requirement

\[
\sum_t p^{\text{ch}}_{b,n,t} \geq \frac{\gamma^{\text{cycle}} \cdot \bar{E}_{b,n} \cdot T_{\text{days}}}{T^{\text{cycle}}_{\text{period}}} \quad \forall b, n \tag{BAT-7}
\]

where \(\gamma^{\text{cycle}}\) is the `min_cycling_ratio` and \(T^{\text{cycle}}_{\text{period}}\) is `min_cycling_period_days`. Ensures batteries are actively utilized.

#### BAT-8: Spillage Power Limit

\[
\text{spill}_{b,n,t} \leq \bar{P}^{\text{dis}}_{b,n} \quad \forall b, n, t \tag{BAT-8}
\]

Only applies to batteries with `spillage = true`.

#### BAT-9: Ramp Limits

\[
-R^{\text{down}}_{b,n} \Delta t \, \bar{P}^{x}_{b,n} \leq p^{x}_{b,n,t} - p^{x}_{b,n,t-1} \leq R^{\text{up}}_{b,n} \Delta t \, \bar{P}^{x}_{b,n} \quad \forall b, n, t, \; x \in \{\text{ch}, \text{dis}\} \tag{BAT-9}
\]

Charge and discharge each rise by at most `ramp_up` and fall by at most `ramp_down` (fraction of their rated power per hour) per step. A ramp of 0, or \(R \Delta t \geq 1\), sets no limit. At \(t = 1\) the previous rolling window's last committed step is \(p^{x}_{b,n,0}\) (`battery_charge_prev`, `battery_discharge_prev`); the first window and a new year start free. The master problem applies the same limits within each representative day.

---


### Reserve Constraints

Implemented in `add_reserve_constraints!()`.

Reserve is headroom, not energy: it is capacity kept free, and it does not enter the power balance. Both requirements are held in the free capacity of reservable generators (\(\mathcal{G}^{\text{res}}\), non-renewable with `reservable: true`) and reservable batteries (\(\mathcal{B}^{\text{res}}\), `reservable: true`) at the node:

\[
H_{n,t} = \sum_{g \in \mathcal{G}^{\text{res}}} \left( \bar{P}_{g,n} \cdot a_{g,n,t} \cdot u_{g,n,t} - p_{g,n,t} \right) + \sum_{b \in \mathcal{B}^{\text{res}}} \left( \bar{P}^{\text{dis}}_{b,n} \, o_{b,t} - p^{\text{dis}}_{b,n,t} + p^{\text{ch}}_{b,n,t} \right)
\]

where \(u_{g,n,t}\) is the commitment status in unit commitment (an offline unit holds no spinning reserve) and 1 otherwise. A battery can raise its discharge to its limit (degraded rating × outage factor × availability, \(o_{b,t}\) as in BAT-1) and can stop charging. In the master problem the battery's rating is the power in service that year (0 once retired, unless its life is extended).

#### RES-1: Static Reserve Requirement

\[
r^{\text{sta}}_{n,t} + \lambda^{\text{sta}}_{n,t} \geq R^{\text{sta}}_n, \qquad r^{\text{sta}}_{n,t} \leq R^{\text{sta}}_n \quad \forall n, t \tag{RES-1}
\]

\(R^{\text{sta}}_n\) is either specified per node (`reserve_static_requirement[n]`) or defaults to `reserve_static_default_ratio` \(\times\) node demand. The shortage slack \(\lambda^{\text{sta}}_{n,t}\) is penalized in the objective.

#### RES-2: Dynamic Reserve

\[
r^{\text{dyn}}_{n,t} \leq \sum_{g \in \mathcal{G}^{\text{res}}} \alpha^{\text{dyn}} \cdot \bar{P}_{g,n} \cdot u_{g,n,t} + \sum_{b \in \mathcal{B}^{\text{res}}} \alpha^{\text{dyn}} \cdot \bar{P}^{\text{dis}}_{b,n} \, o_{b,t} \quad \forall n, t \tag{RES-2a}
\]

\[
r^{\text{dyn}}_{n,t} + \lambda^{\text{dyn}}_{n,t} \geq R^{\text{dyn}}_n, \qquad r^{\text{dyn}}_{n,t} \leq R^{\text{dyn}}_n \quad \forall n, t \tag{RES-2b}
\]

where \(\alpha^{\text{dyn}}\) is the `dynamic_reserve_contribution` factor (fraction of rated power that can respond fast enough). RES-2a and RES-2b are added only at nodes with a dynamic requirement \(R^{\text{dyn}}_n > 0\) (`reserve_dynamic_requirement`); elsewhere \(r^{\text{dyn}} = 0\).

#### RES-3: Shared Headroom

\[
r^{\text{sta}}_{n,t} + r^{\text{dyn}}_{n,t} \leq H_{n,t} \quad \forall n, t \tag{RES-3}
\]

A MW of free capacity counts once: static and dynamic reserve are held in the same headroom. When it is short, the shortfall is penalized through \(\lambda\); the model may also commit more units (unit commitment) or lower output at a node and import, if that is cheaper than the penalty.

---


### Power Balance Constraints


#### PB-1: Single-Bus Power Balance

Implemented in `add_demand_constraints!()`. For single-bus systems:

\[
\sum_g p_{g,n,t} + \sum_b p^{\text{dis}}_{b,n,t} + \alpha_n p^{\text{v2g}}_{n_i,t} + \text{LS}_{n,t} + \alpha_n P^{\text{roof}}_{n_i,t} \tag{PB-1}
\]

\[
= D_{n,t} + \alpha_n S_{n_i,t} + p^{\text{elz}}_{n,t} + \sum_b p^{\text{ch}}_{b,n,t} + \sum_g p^{\text{pump}}_{g,n,t} + \alpha_n p^{\text{ev,ch}}_{n_i,t} + \alpha_n \text{RC}_{n_i,t}
\]

where \(D_{n,t} = D^{\text{node}}_{t,n_i} \cdot \alpha_n\) is the bus demand derived from the parent node's demand scaled by the bus demand fraction, \(P^{\text{roof}}\) is behind-the-meter rooftop solar generation and \(p^{\text{pump}}\) is reservoir pumping. Reserves are not in the balance: they are free capacity (RES-1 to RES-3), not energy. \(S_{i,t} = \sum_{s} \left( \sum_{t'} \Delta_{s,i,t',t} - \sum_{t'} \Delta_{s,i,t,t'} \right)\) is the net demand shifted into step \(t\) at node \(i\) (SHIFT-1); it is spread over the node's buses like the demand.

!!! note "Curtailment is not in the power balance"
    Curtailment (\(\text{CU}_{i,t}\)) does not appear in the power balance equation. It represents energy that was never dispatched (the difference between available and used renewable generation), not energy that enters and then leaves the system.

**Load-shedding bound.** Load shedding sits on the supply side of the balance, so it is bounded by the demand at its own bus:

\[
\text{LS}_{n,t} \leq \max(D_{n,t},\, 0) \quad \forall n, t \tag{PB-1b}
\]

Connection buses have \(D_{n,t} = 0\) and so cannot shed. Without this bound the solver could "shed" more than a bus consumes and export the surplus, using load shedding as virtual generation. The same bound is applied in all three network formulations (`max_load_shed_b*` single-bus, `max_load_shed_dc_b*` DC-OPF, `max_load_shed_acopf_b*` AC-OPF).

#### DC-1: Multi-Bus Power Balance (KCL)

Implemented in `add_dc_constraints!()` in `transmission_dc.jl`. For multi-bus networks the same terms as PB-1 are balanced against the flows on the lines incident to the bus:

\[
\underbrace{\sum_g p_{g,n,t} + \sum_b p^{\text{dis}}_{b,n,t} + \alpha_n p^{\text{v2g}}_{n_i,t} + \text{LS}_{n,t} + \alpha_n P^{\text{roof}}_{n_i,t} + P^{\text{conv}}_{n,t}}_{\text{supply}} \tag{DC-1}
\]

\[
- \underbrace{\left( D_{n,t} + \alpha_n S_{n_i,t} + p^{\text{elz}}_{n,t} + \sum_b p^{\text{ch}}_{b,n,t} + \sum_g p^{\text{pump}}_{g,n,t} + \alpha_n p^{\text{ev,ch}}_{n_i,t} + \alpha_n \text{RC}_{n_i,t} \right)}_{\text{demand}} = \sum_{\ell} \left( K_{n,\ell} \, f_{\ell,t} + \tfrac{1}{2} |K_{n,\ell}| \, P^{\text{loss}}_{\ell,t} \right)
\]

where \(K_{n,\ell}\) is the bus-line incidence matrix element (\(+1\) if line \(\ell\) leaves bus \(n\), \(-1\) if it enters), \(P^{\text{conv}}_{n,t}\) aggregates AC/DC and frequency converter injections at the bus, and \(P^{\text{loss}}_{\ell,t}\) is the loss of line \(\ell\) (piecewise-linear, or linear with `loss_model: linear`): each end supplies half of it. With `loss_model: none` the losses are zero. See [DC Power Flow](dc-power-flow.md) for the loss model.

The constraint reference is stored for dual price extraction.

#### DC-2: Flow–Angle Coupling

\[
f_{\ell,t} = \frac{S_{\text{base}}}{x_\ell} \left( \theta_{i(\ell),t} - \theta_{j(\ell),t} \right) \quad \forall \ell, t \tag{DC-2}
\]

where \(x_\ell\) is the line reactance in p.u. (values \(\leq 0\) are replaced by 0.01) and \(S_{\text{base}}\) the system base power. Each physical line, including parallel lines, is linked to its end-bus angles (`flow_angle_*`). A line with less than 0.1 MW capacity that cannot be expanded by investment has its flow fixed to 0 instead (`flow_zero_*`).

#### DC-3: Slack Bus Reference

\[
\theta_{\text{slack},t} = 0 \quad \forall t \tag{DC-3}
\]

Each connected component of the network gets its own reference bus. Angles are otherwise only bounded by \([-\pi, \pi]\); the DC formulation has no angle-difference limit (`max_angle_diff_deg` applies to AC-OPF only).

#### DC-5: Line Capacity Limits

Implemented in `add_line_capacity_constraints!()`:

\[
-\bar{F}_\ell \, o_{\ell,t} \leq f_{\ell,t} \leq \bar{F}_\ell \, o_{\ell,t} \quad \forall \ell, t \tag{DC-5}
\]

where \(\bar{F}_\ell\) is the thermal capacity (in a `development` run, including what the master built in earlier years) and \(o_{\ell,t}\) is the scheduled-interruption factor of the line or transformer.

#### DC-6: Converter Capacity Limits

Implemented in `add_converter_constraints!()`:

\[
p^{\text{rect}}_{k,t} + p^{\text{inv}}_{k,t} \leq \bar{S}_k \, o_{k,t} \quad \forall k, t \tag{DC-6a}
\]

\[
p^{A \to B}_{f,t} + p^{B \to A}_{f,t} \leq \bar{S}_f \, o_{f,t} \quad \forall f, t \tag{DC-6b}
\]

where \(\bar{S}\) is the rated apparent power (MVA) of the converter and \(o\) its scheduled-interruption factor.

---


### Curtailment Constraints

Implemented in `add_curtailment_constraints!()`.

#### CUR-1: Curtailment Definition

\[
\text{CU}_{i,t} = \sum_{n \in i} \sum_{g \in \mathcal{G}^{\text{RE}}_{\text{var}}} \left( \bar{P}^{\text{eff}}_{g,n} \, a_{g,n,t} - p_{g,n,t} \right) \quad \forall i, t \tag{CUR-1}
\]

Curtailment is defined per node as the difference between available and dispatched variable renewable generation over the node's buses. \(\mathcal{G}^{\text{RE}}_{\text{var}}\) are the renewable units without a reservoir: reservoir hydro is dispatchable within its water budget and is not counted.

#### CUR-2: Curtailment Upper Bound

\[
\text{CU}_{i,t} \leq \sum_{n \in i} \sum_{g \in \mathcal{G}^{\text{RE}}_{\text{var}}} \bar{P}^{\text{eff}}_{g,n} \, a_{g,n,t} \quad \forall i, t \tag{CUR-2}
\]

Curtailment cannot exceed available renewable generation. In the dispatch, curtailment is priced at `curtailment_cost` (OBJ-4a) and not capped; the `max_curtailment_ratio` cap applies only in the master problem's representative days (see [Capacity Expansion](capacity-expansion.md#836-curtailment-limit-per-day)).

---


### Renewable Penetration Constraint

Implemented in `add_renewable_constraint!()`.

#### RE-1: System-Wide Renewable Penetration Target

\[
\Delta t \sum_{g \in \mathcal{G}^{\text{RE}}, n, t} p_{g,n,t} + \Delta t \sum_{i,t} \phi_{i,t} \geq \tau^{\text{RE}} \cdot E^{\text{total}}_{\text{demand}} \tag{RE-1}
\]

where:

- \(\Delta t\) is the temporal resolution (hours)
- \(\tau^{\text{RE}}\) is `re_penetration_target`; the constraint is added only when it is positive
- \(E^{\text{total}}_{\text{demand}} = \sum_{t,i} D_{t,i} \cdot \Delta t\) is total energy demand in the window

Battery discharge does not count: a battery may have been charged from fossil generation. The slack variable \(\phi_{i,t}\) is penalized by \(C^{\text{FRE}}\) in the objective, so the target never makes the problem infeasible.

!!! note "RE penetration is measured against demand, not generation"
    This is a policy constraint. The target \(\tau^{\text{RE}}\) represents the fraction of total demand that should be served by renewable generation.

---


### CO2 Emission Constraints

Implemented in `add_co2_emissions_definition!()` and `add_co2_constraint!()`.

#### CO2-1: Emissions Definition

\[
\text{CO2}_{i,t} = \sum_{n \in i} \sum_g \frac{e_{\text{fuel}(g)}}{\eta_{g,n}} \cdot p_{g,n,t} \quad \forall i, t \tag{CO2-1}
\]

with \(e_f\) (t per MWh of fuel, from `fuel_co2`) and \(\eta_{g,n}\) as in OBJ-6.

#### CO2-2: CO2 Budget Constraint

\[
\Delta t \sum_{g,n,t} \frac{e_{\text{fuel}(g)}}{\eta_{g,n}} \cdot p_{g,n,t} \leq \frac{T \cdot \Delta t}{H_{\text{year}}} \cdot B^{\text{CO2}} + \xi^{\text{CO2}} \tag{CO2-2}
\]

In a multi-system run there is one CO2-2 constraint per system \(s\): the sum runs over the generators at that system's nodes, with that system's budget \(B^{\text{CO2}}_s\), slack \(\xi^{\text{CO2}}_s\) and penalty \(C^{\text{CO2,bud}}_s\) (its own `co2_budget` and `penalties.co2_budget_violation`); a development-zone node counts in the system of the bus the zone connects to; a system with the budget disabled has no constraint. A single system has one constraint over all nodes.

The annual CO2 budget \(B^{\text{CO2}}\) (`co2_budget`, tonnes/year) is scaled by the window fraction \(T \cdot \Delta t / H_{\text{year}}\), where \(H_{\text{year}}\) is the number of hours in the year (`hours_per_year`, always 8760, leap years included). The left-hand side is the window's emissions in tonnes (output × \(\Delta t\) × emission factor). The violation slack \(\xi^{\text{CO2}}\) is penalized by `co2_budget_violation_penalty` per tonne, once: in OBJ-4c its coefficient is \(C^{\text{CO2,bud}} / \Delta t\), so that the \(\Delta t\) of OBJ cancels.

---


### Inertia Constraint

Implemented in `add_inertia_constraints!()`.

#### INE-1: System Inertia Requirement

\[
\sum_{g,n} p_{g,n,t} \cdot h_{g,n} + \sum_{b,n} \left(p^{\text{ch}}_{b,n,t} + p^{\text{dis}}_{b,n,t}\right) \cdot h^{\text{bat}}_{b,n} + H_t \geq H^{\min}_t \quad \forall t \tag{INE-1}
\]

where:

- \(h_{g,n}\) is the inertia constant of generator \(g\) at bus \(n\) (`inertia[n]`)
- \(h^{\text{bat}}_{b,n}\) is the synthetic inertia constant of battery \(b\) (`inertia[n]`)
- \(H^{\min}_t\) is the minimum inertia requirement in MW·s (the system key `INERTIA_LIMIT_THRESHOLD`, default 0.1, or an hourly profile). It is an absolute value, not a fraction of demand or capacity (the Studio's Min. System Inertia field); the default 0.1 MW·s is effectively no requirement. The constraint is skipped in hours where it is \(\leq 0\)
- \(H_t\) is the inertia shortfall slack, penalized by `loss_of_inertia_penalty`

Inertia is contributed by synchronous generators proportionally to their output and by storage systems proportionally to their power throughput.

---


### EV Fleet Constraints

Implemented in `add_ev_constraints!()`.

#### EV-1: SOC Dynamics

\[
E^{\text{ev}}_{n,t+1} = E^{\text{ev}}_{n,t} + \eta^{\text{ev,ch}} \cdot p^{\text{ev,ch}}_{n,t} - \frac{p^{\text{v2g}}_{n,t}}{\eta^{\text{ev,dis}}} - \left(D^{\text{ev}}_{n,t} - \ell^{\text{ev}}_{n,t}\right) \quad \forall n, t \tag{EV-1}
\]

where \(D^{\text{ev}}_{n,t}\) is the driving energy drawn from the fleet battery (MWh). Driving consumes the stored energy, so over the window the grid must supply it (divided by \(\eta^{\text{ev,ch}}\)) and V2G can only return energy that is not used for driving. Charging can shift in time within the power and SOC limits.

#### EV-2: Unserved Driving Energy

\[
0 \leq \ell^{\text{ev}}_{n,t} \leq D^{\text{ev}}_{n,t} \quad \forall n, t \tag{EV-2}
\]

The slack \(\ell^{\text{ev}}_{n,t}\) is driving energy the fleet cannot supply, penalized in the objective at `penalties.ev_loss` (default: `loss_of_load`).

#### EV-3: Charging Power Limit

\[
p^{\text{ev,ch}}_{n,t} \leq \max\left( \frac{\bar{P}^{\text{ev,ch}}_{\text{kW}} \cdot N^{\text{ev}}_n}{1000},\; 2 \cdot D^{\text{ev}}_{n,t} \right) \quad \forall n, t \tag{EV-3}
\]

The maximum charging power is the aggregate fleet charging capacity (kW to MW conversion), with a floor of twice the driving consumption for feasibility.

#### EV-4: V2G Discharge Limit

\[
p^{\text{v2g}}_{n,t} \leq \frac{\bar{P}^{\text{ev,dis}}_{\text{kW}} \cdot N^{\text{ev}}_n}{1000} \cdot \alpha^{\text{ev}}_{n,t} \quad \forall n, t \tag{EV-4}
\]

where \(\alpha^{\text{ev}}_{n,t}\) is the EV availability profile (fraction of fleet connected to grid).

#### EV-5: SOC Bounds

\[
\underline{E}^{\text{ev}} \cdot \bar{E}^{\text{ev}}_n \leq E^{\text{ev}}_{n,t+1} \leq \overline{E}^{\text{ev}} \cdot \bar{E}^{\text{ev}}_n \quad \forall n, t \tag{EV-5}
\]

where \(\bar{E}^{\text{ev}}_n = E^{\text{ev}}_{\text{kWh}} \cdot N^{\text{ev}}_n / 1000\) is the total fleet battery capacity in MWh.

#### EV-6: Charge/V2G Mutual Exclusivity

\[
p^{\text{ev,ch}}_{n,t} \leq M_{\text{ev}} \cdot \delta^{\text{ev}}_{n,t} \quad \forall n, t \tag{EV-6a}
\]

\[
p^{\text{v2g}}_{n,t} \leq M_{\text{ev}} \cdot (1 - \delta^{\text{ev}}_{n,t}) \quad \forall n, t \tag{EV-6b}
\]

where \(M_{\text{ev}} = P^{\text{max,ch}} + P^{\text{max,v2g}}\). \(\delta^{\text{ev}}\) is continuous in \([0,1]\), so this is a relaxation.

#### EV-7: Initial and Window-End SOC

\[
E^{\text{ev}}_{n,1} = E^{\text{ev},0}_n, \qquad E^{\text{ev}}_{n,T+1} = E^{\text{ev}}_{n,1} \quad \forall n \tag{EV-7}
\]

The fleet starts each window at its initial SOC (`initial_soc`, or `target_soc` × fleet capacity) and must return to it at the end of the window (`ev_soc_cyclic_n*`). Nodes without vehicles have all EV variables fixed to 0. See [EV Model](ev-model.md) for how the fleet parameters and profiles are built.

---


### Sectoral Demand Constraints

Implemented in `add_sectoral_demand_constraints!()`.

#### SEC-1: Per-Sector Cap

\[
\text{LOL}_{s,i,t} \leq D^{\text{sec}}_{s,i,t} \quad \forall s, i, t \tag{SEC-1}
\]

A sector cannot shed more than its own demand \(D^{\text{sec}}_{s,i,t}\) at the node (`sectoral_demand`, `sectoral_lol_cap_*`).

#### SEC-2: LOL Decomposition

\[
0 \;\leq\; \sum_{n \in i} \text{LS}_{n,t} - \sum_{s \in \mathcal{S}} \text{LOL}_{s,i,t} \;\leq\; \max\Big(0,\; D^{\text{node}}_{t,i} - \sum_{s} D^{\text{sec}}_{s,i,t}\Big) \quad \forall i, t \tag{SEC-2}
\]

The load shed on a node's buses is split into per-sector contributions plus an unclassified remainder (`sectoral_lol_sum_*`, `sectoral_lol_residual_*`). The remainder is bounded by the node demand that no sector accounts for: it is 0 when the sector shares cover the whole demand, and it keeps the split feasible at a node whose sectoral data is missing (for example after the Grid Builder split a node's demand across new buses). The remainder is priced at \(C^{\text{VOLL}} m_{i,t}\), the demand-weighted mean of the node's sector criticalities (OBJ-4a).

!!! note "Criticality ordering"
    Higher-criticality sectors receive a higher load shedding penalty in the objective (\(C^{\text{VOLL}} \cdot \kappa_s\)). With the per-sector cap SEC-1, the optimizer sheds the cheapest sector up to its demand before touching a dearer one. No explicit ordering constraint is needed.

---


### Demand Shifting Constraints

Implemented within `add_sectoral_demand_constraints!()`.

#### SHIFT-1: Shift-Out Capacity

\[
\sum_{t'} \Delta_{s,i,t,t'} \leq \phi_s \cdot D^{s}_{i,t} \quad \forall s, i, t \tag{SHIFT-1}
\]

Sectors with `is_flexible: true`, `flexibility_ratio` \(\phi_s > 0\) and `delay_tolerance` \(\tau_s > 0\) hours can move demand from step \(t\) to any step \(t'\) of the same window at most \(\tau_s\) hours earlier or later (\(|t - t'| \le \lfloor \tau_s / \Delta t \rfloor\)), at the same node. What leaves a step is at most \(\phi_s\) of the sector's demand there (`demand_shift_out_cap_*`). The net shifted demand \(S_{i,t}\) enters the power balance (PB-1, DC-1), so the energy is moved, not removed, and each MWh moved costs OBJ-13.

In a rolling window a shift does not cross the end of the committed hours (the window length minus `overlap_hours`): demand moved into the overlap would be dropped when the next window starts from the original profile. Shifts do not cross window boundaries either.

---


### N-1 Security Constraints

Implemented in `add_n1_security_constraints!()` (preventive) or, with `scopf_enabled`, in `add_scopf_constraints!()`. Both require `n1_security.enabled`.

#### N1-1: Generation N-1 (preventive)

\[
H^{\text{N1}}_t + s^{\text{N1,g}}_t \geq p_{g^*,n^*,t} \quad \forall t \tag{N1-1}
\]

where \((g^*, n^*)\) is the unit with the largest rated power in the system and \(H^{\text{N1}}_t\) is the headroom of everything else that could replace it: the same headroom as in RES-3 (reservable non-renewable units, counted only when online in unit commitment, plus the headroom of reservable batteries), summed over the whole system and excluding \((g^*, n^*)\). If the largest unit trips, the rest must be able to pick up its output (`n1_gen_reserve_system_t*`); the shortfall \(s^{\text{N1,g}}_t\) (`n1_gen_shortfall_t*`) keeps the problem feasible.

#### N1-2: Transmission N-1 (preventive)

\[
-\bar{F}_{\ell} \cdot \omega - s^{\text{N1,l}}_{\ell,t} \leq f_{\ell,t} \leq \bar{F}_{\ell} \cdot \omega + s^{\text{N1,l}}_{\ell,t} \quad \forall \ell: \bar{F}_{\ell} > 0, \; t \tag{N1-2}
\]

where \(\omega\) is the `transmission_reserve_factor`. Every transmission line with positive capacity is critical, each parallel circuit on its own flow (`n1_trans_reserve_l*`, over-flow slack `n1_trans_over_l*`).

Both slacks are priced at `loss_of_reserve_static` per MW and time step (reported as `n1_security_shortfall_cost`), so an insecure fleet or network shows up as a cost instead of an infeasible model.

With `generation_reserve_type: percentage` the right-hand side of N1-1 is `generation_reserve_percentage` × the total demand of step \(t\) instead of the largest unit's output, and no unit is excluded from \(H^{\text{N1}}_t\) (same constraint and shortfall names).

`critical_line_threshold`, `contingency_depth`, `redistribution_mode`, `pi_screening_threshold`, `transformer_contingencies` and `battery_contingencies` configure the post-hoc contingency analysis of the Studio's single-line diagram, not the optimisation.

#### SCOPF variant

With `scopf_enabled`, post-contingency line limits replace N1-2. For every pair of lines \(l \neq k\) with non-negligible LODF, one set of constraints is added once (no iteration, no reserve factor), on each physical line's flow in its from→to direction:

\[
-\bar{F}_l - o_{l,t} \leq f_{l,t} + \text{LODF}_{l,k} \, f_{k,t} \leq \bar{F}_l + o_{l,t} \quad \forall l, k, t \tag{N1-3}
\]

(`scopf_line*_outage*_t*_pos/neg`), where \(o_{l,t}\) is line \(l\)'s worst post-contingency overload (`scopf_over_l*_t*`). Generation N-1 is either N1-1 (`scopf_n1_gen_reserve_t*`) or, with `corrective_enabled`, a re-dispatch form: every non-renewable unit other than \((g^*, n^*)\), reservable or not, contributes its headroom \(h_{g,n,t}\) up to what it can ramp in one step, plus the headroom of every battery, reservable or not (`n1_corrective_gen_t*`):

\[
\sum_{(g,n) \neq (g^*,n^*)} r_{g,n,t} + \sum_{b,n} \left( \bar{P}^{\text{dis}}_{b,n} \, o_{b,t} - p^{\text{dis}}_{b,n,t} + p^{\text{ch}}_{b,n,t} \right) + s^{\text{N1,g}}_t \geq p_{g^*,n^*,t}, \quad r_{g,n,t} \leq \min\left(h_{g,n,t}, \bar{P}_{g,n} R^{\text{up}}_{g,n} \Delta t\right) \tag{N1-4}
\]

All SCOPF slacks are priced like the preventive ones.

**Configuration parameters** (section `n1_security:`):

- `enabled` -- master switch for N-1 constraints (default false)
- `generation_enabled`, `transmission_enabled` -- enable each part
- `transmission_reserve_factor` -- fraction \(\omega\) of line capacity usable under N-1 (default 0.70)
- `scopf_enabled`, `corrective_enabled` -- SCOPF variant and corrective generation N-1
- `compute_security_cost` -- re-solve each window without N-1 and report the objective difference as `n1_security_cost` (see [Cost of N-1 security](dc-power-flow.md#cost-of-n-1-security))

---


### Maximum Annual System Cost

Implemented in `add_max_annual_system_cost!()`.

#### COST-1: Operating Cost Ceiling

\[
\Delta t \sum_{g,n,t} \left( c^{\text{fuel}}_{g,n} + c^{\text{maint}}_{g,n} \right) p_{g,n,t} + \frac{T \Delta t}{H_{\text{year}}} \left( Z^{\text{fix}} + Z^{\text{bat,fix}} \right) - s^{\text{cost}} \leq \frac{T \Delta t}{H_{\text{year}}} \, C^{\max}_{\text{annual}} \tag{COST-1}
\]

The annual ceiling `MAX_ANNUAL_SYSTEM_COST` (default 20·10⁹) is scaled to the window, and so is the fixed O&M (OBJ-1a, OBJ-3c). The constraint is soft: the violation \(s^{\text{cost}} \geq 0\) (`cost_violation`) costs 10 × the largest generator variable cost per unit in the objective.

---


## 5. Degradation and Age-Based Capacity


Effective capacity decreases with unit age:

\[
\bar{P}^{\text{eff}}_{g,n} = \bar{P}_{g,n} \cdot (1 - d_g)^{\text{age}_{g,n}} \tag{DEG-1}
\]

where \(d_g\) is the annual degradation rate (`degradation_rate[n]`) and:

| Unit Type | Age Formula |
|-----------|-------------|
| Existing unit | \(\text{age} = \text{initial\_age} + (\text{year\_idx} - 1)\) |
| New investment | \(\text{age} = \text{year\_idx} - \text{investment\_year}\) |

If the remaining lifetime (\(\text{lifetime} - \text{age}\)) is zero or negative, the effective capacity is zero: the unit is retired at the end of its `life_time`, with or without degradation, as in the master problem (`generator_power_in_service`, `battery_power_limits`). The reserves, curtailment (CUR-1) and N-1 terms use the same power in service. In a `development` run the dispatch receives the master's capacity instead (retirements and life extension already applied), with no degradation and no end of life; a battery within its lifetime keeps the dispatch's own degradation, and past it gets the power and energy the master keeps.

The same degradation logic applies to batteries, where capacity, maximum charge power, and maximum discharge power are all degraded by the same factor:

\[
\bar{E}^{\text{eff}}_{b,n} = \bar{E}_{b,n} \cdot (1 - d_b)^{\text{age}_{b,n}}, \quad \bar{P}^{\text{ch,eff}}_{b,n} = \bar{P}^{\text{ch}}_{b,n} \cdot (1 - d_b)^{\text{age}_{b,n}} \tag{DEG-2}
\]

---


## 6. Dual Variables and Electricity Prices


In LP mode (economic dispatch or relaxed unit commitment), the shadow prices of the power balance constraints yield the locational marginal price (LMP):

\[
\pi_{n,t} = \text{dual}\left(\text{balance\_constraint}_{n,t}\right) \tag{DUAL-1}
\]

The constraint references are stored in `vars.balance_constraints[(n,t)]` during construction of either `add_demand_constraints!()` (single bus) or `add_dc_constraints!()` (multi-bus). Prices are extracted in `extract_solution()` using JuMP's `dual()` function.

!!! warning "Duals are unavailable in MIP mode"
    When the model contains binary variables (unit commitment mode), dual prices are not directly available from the solver. The model must be fixed and re-solved as an LP to obtain meaningful price signals.

---


## 7. Solution Extraction


After solving, `extract_solution()` computes the following aggregate metrics:

| Metric | Formula |
|--------|---------|
| Total generation | \(\sum_{g,n,t} p_{g,n,t}\) |
| Total curtailment | \(\sum_{n,t} \text{CU}_{n,t}\) |
| Total load shedding | \(\sum_{n,t} \text{LS}_{n,t}\) |
| RE penetration | \(\sum_{g \in \mathcal{G}^{\text{RE}}} p_g / \sum_g p_g\) |
| Total CO2 | \(\sum_g e_{\text{fuel}(g)} \sum_{n,t} p_{g,n,t}\) |

---


## 8. Parameter Summary


Penalty coefficients and their YAML keys. Keys under `penalties:` belong to each system's `penalties` section; the others are system-level keys. Defaults are the schema defaults.

| Parameter | YAML key | Default | Description |
|-----------|----------|---------|-------------|
| \(C^{\text{VOLL}}\) | `penalties.loss_of_load` | 10 000 000 | Value of lost load (\$/MWh) |
| \(C^{\text{CU}}\) | `penalties.curtailment_cost` | 20 | Curtailment cost (\$/MWh) |
| \(C^{\text{r,sta}}\) | `penalties.loss_of_reserve_static` | 100 | Static reserve shortage (\$/MW per step) |
| \(C^{\text{r,dyn}}\) | `penalties.loss_of_reserve_dynamic` | 100 | Dynamic reserve shortage (\$/MW per step) |
| \(C^{\text{ine}}\) | `penalties.loss_of_inertia` | 200 | Inertia shortfall penalty |
| \(C^{\text{ev}}\) | `penalties.ev_loss` | = `loss_of_load` | Unserved EV driving energy (\$/MWh) |
| \(C^{\text{FRE}}\) | `penalties.fre_penetration_loss` | 100 | RE target shortfall (\$/MWh) |
| \(C^{\text{SOC}}\) | `penalties.soc_violation` | 1 000 000 | SOC upper-limit violation (\$/MWh) |
| \(C^{\text{TM}}\) | `penalties.transfer_margin` | 100 | Transfer margin slack (always 0, see §2.3) |
| \(C^{\text{CO2}}\) | `penalties.co2_cost` | 10 | Carbon price (\$/t) |
| \(C^{\text{CO2,bud}}\) | `penalties.co2_budget_violation` | 500 | CO2 budget excess (\$/t) |
| \(C^{\text{RC}}\) | `penalties.rooftop_curtailment` | 5 | Rooftop solar curtailment (\$/MWh) |
| \(\gamma^{\text{shift}}\) | `demand_shift_cost_rate` | 0.1 | Shift cost per MWh and per hour moved (\$/MWh/h) |

!!! note "Penalty tuning"
    Penalty coefficients establish a merit order for constraint relaxation. They should be set so that \(C^{\text{VOLL}} \gg C^{\text{CU}} > C^{\text{FRE}}\) to ensure that load shedding is the last resort, curtailment is preferred to shedding, and RE targets create appropriate investment signals without dominating the objective.

---


## 9. Implementation Reference

| Julia Function | Section | File |
|----------------|---------|------|
| `create_power_system()` | Top-level model builder | `power_system.jl` |
| `build_variables!()` | All variable creation | `power_system.jl` |
| `build_objective!()` | Objective function | `power_system.jl` |
| `add_generator_constraints!()` | GEN-1 through GEN-5 | `power_system.jl` |
| `add_battery_constraints!()` | BAT-1 through BAT-9 | `power_system.jl` |
| `add_reserve_constraints!()` | RES-1 to RES-3 | `power_system.jl` |
| `add_reservoir_constraints!()` | Reservoir hydro (see [Reservoir Hydropower](reservoir-hydro.md)) | `power_system.jl` |
| `add_electrolyzer_dispatch_constraints!()` | Electrolyzer rated power and ramps | `power_system.jl` |
| `add_demand_constraints!()` | PB-1, PB-1b (single bus) | `power_system.jl` |
| `add_dc_constraints!()` | DC-1 to DC-3, PB-1b | `transmission_dc.jl` |
| `add_line_capacity_constraints!()` | DC-5 | `transmission_dc.jl` |
| `add_converter_constraints!()` | DC-6 | `transmission_dc.jl` |
| `add_curtailment_constraints!()` | CUR-1, CUR-2 | `power_system.jl` |
| `add_renewable_constraint!()` | RE-1 | `power_system.jl` |
| `add_co2_emissions_definition!()` | CO2-1 | `power_system.jl` |
| `add_co2_constraint!()` | CO2-2 | `power_system.jl` |
| `add_inertia_constraints!()` | INE-1 | `power_system.jl` |
| `add_ev_constraints!()` | EV-1 to EV-7 | `power_system.jl` |
| `add_sectoral_demand_constraints!()` | SEC-1, SEC-2, SHIFT-1 | `power_system.jl` |
| `add_n1_security_constraints!()` | N1-1, N1-2 | `power_system.jl` |
| `add_scopf_constraints!()` | N1-3, N1-4 | `power_system.jl` |
| `add_max_annual_system_cost!()` | COST-1 | `power_system.jl` |
| `extract_solution()` | Solution extraction and metrics | `power_system.jl` |

---


## 10. Rolling Horizon Implementation


The full year (8760 hours) is solved as a sequence of overlapping windows rather than a single monolithic problem, reducing memory requirements and solve time by orders of magnitude.

### 10.1 Window Structure

The year is divided into windows of `rolling_horizon_hours` (default: 48) with `overlap_hours` (default: 24) of overlap:

```
Window 1: hours  1 — 48  → keep hours  1 — 24
Window 2: hours 25 — 72  → keep hours 25 — 48
Window 3: hours 49 — 96  → keep hours 49 — 72
...
```

The overlap is a look-ahead: battery SOC has no terminal value or end condition in a rolling window, so a window only stores energy for needs inside its own horizon. An overlap of at least 24 h lets each window see the next daily peak.

### 10.2 Initial Conditions

Each window (except the first) receives initial conditions from the previous window. They are indexed by bus; with `network_reduction` enabled they are mapped onto the reduced network's buses:

| State Variable | Initial Condition |
|----------------|-------------------|
| Battery SOC | Energy (MWh) at the end of the previous window's last kept hour, per battery and bus, clamped to this window's SOC bounds (carried as energy, so it stays exact when the capacity is degraded) |
| Generator status (UC) | On/off status at last kept hour |
| Generator output | Output at the last kept hour; GEN-3 applies between it and the window's first step (`ramp_up_seam_*`, `ramp_down_seam_*`) |
| Reservoir level | Water level at the end of the last kept hour; the window's end condition is relative to this level |

EV fleet SOC and demand shifting are not carried: each window starts the EV fleet at its initial SOC and returns it there (EV-7).

### 10.3 Window Boundary Handling

Intermediate windows have no end-of-window SOC condition: stored energy has no value at a window's end, so the overlap (look-ahead) is what makes a window keep energy for the next day. The last window of the year carries the soft year-end floor (BAT-5), so the year cannot end with less stored energy than it started with.

### 10.4 Result Stitching

Results from all windows are concatenated to form the full-year dispatch, keeping the first \(K = \) `rolling_horizon_hours` − `overlap_hours` hours of each window:

\[
\mathbf{p}^{year} = [\mathbf{p}^{w_1}_{1:K}, \mathbf{p}^{w_2}_{1:K}, \ldots, \mathbf{p}^{w_N}_{1:r}]
\]

where the last window may have fewer than \(K\) kept hours (remainder of the year).

The year's totals are stitched the same way: the objective, the cost breakdown, generation, demand, curtailment, load shedding and CO2 add up each window's first \(K\) hours only (the overlap tail is solved again by the next window). A cost term counts for the hours it belongs to; a term without an hour (investment annuities, the CO2-budget slack) counts for the window's share \(K / H_w\); a primary-energy period counts for the share of its hours among the first \(K\); and fuel drawn from a tank is valued from the window's start to the end of hour \(K\). `n1_security_cost` is the exception: it is a whole-window figure and counts for \(K / H_w\).

### 10.5 Time Resolution

`resolution_hours` is an integer of at least 1, so sub-hourly steps are not supported. With \(\Delta t > 1\) each step represents \(\Delta t\) hours: energy terms in the objective and battery SOC changes are multiplied by \(\Delta t\), ramp limits allow \(\min(1, R \Delta t)\) per step, and minimum up/down times are rounded up to whole steps.

---

## References

The unit commitment formulation and rolling horizon dispatch follow the classical treatment in Wood et al. [**[31]**](../reference/bibliography.md#ref31) (Chs. 2, 4). The importance of chronological operational detail in planning models is studied by Poncelet et al. [**[26]**](../reference/bibliography.md#ref26) and Palmintier and Webster [**[25]**](../reference/bibliography.md#ref25). Battery storage modeling with cyclic SOC constraints and degradation draws on Xu et al. [**[32]**](../reference/bibliography.md#ref32). Load shedding with value of lost load (VOLL) prioritization follows the review by Schröder and Kuckshinrichs [**[33]**](../reference/bibliography.md#ref33). N-1 security reserve constraints follow Capitanescu et al. [**[34]**](../reference/bibliography.md#ref34). The optimization is formulated via JuMP [**[20]**](../reference/bibliography.md#ref20) and solved with HiGHS [**[21]**](../reference/bibliography.md#ref21) (LP/MIP) or Ipopt [**[4]**](../reference/bibliography.md#ref4) (ACOPF).

See the [full bibliography](../reference/bibliography.md) for complete citation details.
