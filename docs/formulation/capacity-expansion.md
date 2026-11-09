# Capacity Expansion (Master Problem)


The **Master Problem** (`master_problem.jl`) determines optimal investment and retirement decisions over a multi-year planning horizon by minimizing the net present value (NPV) of total system costs. Representative days approximate operational costs and validate that investment plans are operationally feasible. Notation follows the conventions in the [Formulation Overview](overview.md).

---


## 1. Overview


The Master Problem is a **two-stage linear program** [**[24]**](../reference/bibliography.md#ref24), [**[25]**](../reference/bibliography.md#ref25):

1. **First stage (investment)**: Capacity additions of candidate technologies, battery technologies, transmission and reservoir storage, plus life extension of existing units, across all planning years.
2. **Second stage (operational validation)**: Representative-day dispatch subproblems embedded within the master model to link investment decisions to operational feasibility.

Existing generators and batteries leave service when their age reaches their configured lifetime. The model may keep part of that capacity online with continuous **life-extension** variables, charged every year at a fraction of the investment cost. There are no binary decisions, so the formulation stays a pure LP.

The master works on a node-level network: when the systems are merged for planning, each node becomes one bus and the lines between nodes become synthetic master lines (`master_lines`). The operational dispatch then works on the full bus network.

**Entry point**: `create_master_problem(input; use_representative_days=true)`. With `solver_method: benders` the same problem is solved by Benders decomposition (§11b).

---


## 2. Sets


| Symbol | Description | Source |
|--------|-------------|--------|
| \(\mathcal{Y} = \{1, \ldots, Y\}\) | Planning years | `input.years` |
| \(\mathcal{Y}^{inv} \subseteq \mathcal{Y}\) | Investment-period start years: \(1, 1+p, 1+2p, \ldots\) with \(p\) = `investment_resolution_hours` / 8760 | `years_per_inv_period` |
| \(\mathcal{G}\) | Existing generators | `input.generators` |
| \(\mathcal{K}\), \(\mathcal{K}_{RE} \subseteq \mathcal{K}\) | Candidate technologies; renewable ones | `input.technologies` |
| \(\mathcal{B}\) | Existing batteries | `input.batteries` |
| \(\mathcal{B}^{K}\) | Candidate battery technologies | `input.battery_technologies` |
| \(\mathcal{N} = \{1, \ldots, N\}\) | Buses of the master network (one per node after merging) | `input.network.num_buses` |
| \(\mathcal{T}_d = \{1, \ldots, H\}\) | Time steps within a representative day | `24 / temporal_resolution_hours` |
| \(\mathcal{D}_y\) | Representative days for year \(y\) | `select_representative_days()` |
| \(\mathcal{S}\) | Demand sectors | `input.sectoral_demand` |
| \(\Sigma\) | Systems (for per-system RE targets) | `input.system_node_ranges` |

---


## 3. Decision Variables

### 3.1 Investment Variables


Investment variables exist only for period-start years \(y \in \mathcal{Y}^{inv}\); the other years of a period share them.

| Variable | Domain | Units | Description | Julia name |
|----------|--------|-------|-------------|------------|
| \(I^{tech}_{y,k,n}\) | \([0, \bar{I}_{k,n}]\) | MW | Technology capacity investment | `tech_investment[y][k][n]` (`tech_inv_*`) |
| \(I^{bat,P}_{y,k,n}\) | \([0, \bar{I}^{P}_{k,n}]\) | MW | Battery-technology power investment | `bat_tech_power_investment[y][k][n]` |
| \(I^{bat,E}_{y,k,n}\) | \([0, \bar{I}^{E}_{k,n}]\) | MWh | Battery-technology energy investment | `bat_tech_capacity_investment[y][k][n]` |
| \(I^{tr}_{y,i,j}\) | \([0, \bar{I}^{tr}_i]\) | MW | Transmission expansion on an existing corridor | `transfer_investment[y][(i,j)]` |
| \(I^{res}_{y,g,n}\) | \(\mathbb{R}_+\) | MWh | Reservoir storage investment (investment-period starts) | `reservoir_investment[y][g]` |

### 3.2 Life Extension, RE Tracking and Slack Variables


| Variable | Domain | Units | Description | Julia name |
|----------|--------|-------|-------------|------------|
| \(X_{y,g,n}\) | \([0, \bar{P}_{g,n}(1-\delta_{g,n})^{a}]\) | MW | Capacity of generator \(g\) kept past its lifetime | `gen_life_extension` (`gen_life_ext_*`) |
| \(X^{bat}_{y,b,n}\) | \([0, \bar{P}^{bat}_{b,n}]\) | MW | Battery power kept past its lifetime | `bat_life_extension` (`bat_life_ext_*`) |
| \(\rho_{\sigma,y}\) | \([0, 1]\) | -- | RE penetration ratio of system \(\sigma\) | `re_penetration_ratio[(σ,y)]` |
| \(s^{re}_y\) | \(\mathbb{R}_+\) | -- | RE target slack | `slack_re_target[y]` |
| \(s^{cap}_{y,n}\) | \(\mathbb{R}_+\) | MW | Capacity adequacy slack per node | `slack_capacity[(y,n)]` |
| \(s^{bud}_y\) | \(\mathbb{R}_+\) | \$ | Budget slack | `slack_budget[y]` |

Life-extension variables exist only for years in which the unit is past its lifetime.

### 3.3 Representative Day Operational Variables


For each year \(y\) and representative day \(d\), a set of dispatch variables is created by `create_day_ps_vars!()` (the same `PowerSystemVariables` structure as the operational dispatch, with names prefixed `rd_y{y}_d{d}`), plus the candidate-technology variables:

| Variable | Domain | Units | Description | Julia name |
|----------|--------|-------|-------------|------------|
| \(P^{day}_{g,n,t}\) | \(\mathbb{R}_+\) | MW | Existing generator output | `gen_output[g,n,t]` |
| \(P^{tech}_{k,n,t}\) | \(\mathbb{R}_+\) | MW | Technology output | `tech_out_y*_d*` |
| \(P^{ch,day}_{b,n,t}\), \(P^{dis,day}_{b,n,t}\), \(E^{day}_{b,n,t}\) | \(\mathbb{R}_+\) | MW, MWh | Existing battery charge, discharge, SOC | `bat_charge`, `bat_discharge`, `bat_soc` |
| \(P^{bt,ch}_{k,n,t}\), \(P^{bt,dis}_{k,n,t}\), \(E^{bt}_{k,n,t}\) | \(\mathbb{R}_+\) | MW, MWh | Battery-technology charge, discharge, SOC | `bat_tech_ch/dis/soc_y*_d*` |
| \(CU^{day}_{n,t}\) | \(\mathbb{R}_+\) | MW | Curtailment of existing renewables per node | `curtailment[n,t]` |
| \(L^{day}_{n,t}\) | \(\mathbb{R}_+\) | MW | Load shedding per bus | `load_shed[n,t]` |
| \(f^{day}_{\ell,t}\) | \(\mathbb{R}\) | MW | Line flow (DC power flow) | `pf_*` |
| \(\lambda^{fre}_{n,t}\) | \(\mathbb{R}_+\) | MW | FRE penetration loss slack | `fre_penetration_loss[n,t]` |
| \(L^{sec,day}_{s,n,t}\) | \(\mathbb{R}_+\) | MW | Sectoral loss of load | `loss_of_load_sectoral[s][n,t]` |
| \(r^{sta}, r^{dyn}, \lambda^{sta}, \lambda^{dyn}\) | \(\mathbb{R}_+\) | MW | Reserves and reserve shortfalls per node | `reserve_*` |
| \(H^{day}_t\) | \(\mathbb{R}_+\) | MW-s | Inertia shortfall | `loss_of_inertia[t]` |

Reservoir level, spillage and pumping variables are added for reservoir units.

---


## 4. Parameters

### 4.1 Generator and Technology Parameters


| Symbol | Units | Description | Julia field |
|--------|-------|-------------|-------------|
| \(\bar{P}_{g,n}\) | MW | Rated power of an existing generator | `gen.rated_power[n]` |
| \(c^{inv}_{g,n}\) | \$/MW | Investment cost of an existing generator (used to price life extension; the candidate technology's when 0, see OBJ-3b) | `gen.invest_cost[n]` |
| \(c^{inv}_{k,n}\) | \$/MW | Technology investment cost | `tech.invest_cost[n]` |
| \(\bar{I}_{k,n}\) | MW | Maximum cumulative technology investment | `tech.invest_max[n]` |
| \(\tau_{g,n}\), \(\tau_{k,n}\) | years | Lifetime | `life_time[n]` |
| \(a^0_{g,n}\) | years | Initial age of an existing unit | `gen.initial_age[n]` |
| \(\delta_{g,n}\), \(\delta_{k,n}\) | [0,1] | Annual degradation rate | `degradation_rate[n]` |
| \(\alpha_{g,t,n}\) | [0,1] | Availability factor | `availability[t,n]` |
| \(c^{fuel}_{g,n}\) | \$/MWh | Fuel cost; with primary energy on, fuel-coupled units pay the fuel price through the fuel balance instead (§8.3.12) | `gen.fuel_cost[n]` |
| \(c^{fix}_{g,n}\), \(c^{fix}_{k,n}\) | \$/MW/year | Fixed O&M cost, on the power in service (OBJ-3b) | `fixed_cost[n]` |
| \(c^{maint}_{g,n}\) | \$/MWh | Maintenance cost | `gen.maintenance_cost[n]` |
| \(c^{CO_2}_g\) | \$/MWh | Carbon cost: `co2_cost` × the emission factor of the unit's fuel (per MWh of fuel) / the unit's rated efficiency | `co2_cost_per_mwh(input, gen, n)` |
| \(c^{dec}_{g,n}\) | \$/MW | Decommissioning cost | `gen.decommissioning_cost[n]` |
| \(\kappa^{risk}_{g,n}\) | [0,1] | Risk coefficient (capacity derating in CAP-1) | `risk_coefficient[n]` |

### 4.2 Battery Parameters


| Symbol | Units | Description | Julia field |
|--------|-------|-------------|-------------|
| \(\bar{P}^{bat}_{b,n}\) | MW | Max discharge power of an existing battery | `bat.max_discharge_power[n]` |
| \(\bar{E}_{b,n}\) | MWh | Energy capacity of an existing battery | `bat.capacity[n]` |
| \(\eta^{ch}\), \(\eta^{dis}\) | [0,1] | Charge and discharge efficiency | `charge_efficiency[n]`, `discharge_efficiency[n]` |
| \(E^0_{b,n}\) | [0,1] | Initial SOC fraction | `soc_initial[n]` |
| \(c^{inv,P}_{k,n}\), \(c^{inv,E}_{k,n}\) | \$/MW, \$/MWh | Battery-technology power and energy investment cost | `invest_cost_power[n]`, `invest_cost_capacity[n]` |
| \(h^{min}_{k}\), \(h^{max}_k\) | hours | Minimum and maximum duration of a battery technology | `min_duration_hours`, `max_duration_hours` |
| \(c^{\text{thr}}_{b,n}\) | \$/MWh | Throughput degradation cost | `bat.throughput_degradation_cost[n]` |

### 4.3 System Parameters


| Symbol | Units | Description | Julia field |
|--------|-------|-------------|-------------|
| \(r\) | [0,1] | Discount rate | `input.discount_rate` |
| \(\bar{B}\) | \$ | Max investment budget per investment period | `input.max_annual_investment` |
| \(\rho^{target}\) | [0,1] | Target RE penetration (final year) | `input.target_re_penetration` |
| \(\rho^{init}_\sigma\) | [0,1] | Initial RE penetration of system \(\sigma\) | `initial_re_penetration` / `system_node_ranges[σ].initial_re` |
| \(\Delta\rho^{min}\), \(\Delta\rho^{max}\) | [0,1] | Minimum and maximum annual RE increment | `input.min_re_increment`, `input.max_re_increment` |
| \(M_{res}\) | -- | Reserve margin multiplier | `input.reserve_margin` (default 1.15) |
| \(\phi^{ext}\) | -- | Life-extension cost factor | `input.life_extension_cost_factor` |
| \(c^{VOLL}\) | \$/MWh | Value of lost load penalty | `input.loss_of_load_penalty` |
| \(c^{CU}\) | \$/MWh | Curtailment cost | `input.curtailment_cost` |
| \(c^{fre}\) | \$/MWh | FRE penetration loss penalty | `input.fre_penetration_loss_penalty` |
| \(c^{slack}\) | \$ | Generic slack penalty | `input.slack_penalty` |
| \(\bar{\kappa}\) | [0,1] | Maximum curtailment ratio | `input.max_curtailment_ratio` (default 0.05) |
| \(D_r\) | -- | Representative days per year | `input.representative_days_per_year` |

---


## 5. Objective Function


**Julia function**: `build_master_objective!(model, vars, input)`

The objective minimizes the NPV of total system costs over the planning horizon:

\[
\min \; Z = \sum_{y \in \mathcal{Y}^{inv}} \frac{C^{inv}_y}{(1+r)^{y-1}} + \sum_{y=1}^{Y} \frac{C^{asset}_y + C^{op}_y}{(1+r)^{y-1}} + C^{slack} \tag{OBJ-1}
\]

### 5.1 Investment Cost


\[
C^{inv}_y = \underbrace{\sum_{k,n} c^{inv}_{k,n} I^{tech}_{y,k,n}}_{\text{Technologies}} + \underbrace{\sum_{k,n} \left( c^{inv,P}_{k,n} I^{bat,P}_{y,k,n} + c^{inv,E}_{k,n} I^{bat,E}_{y,k,n} \right)}_{\text{Battery technologies}} + \underbrace{\sum_{\{i,j\}} \max(c^{tr}_i, c^{tr}_j) \, I^{tr}_{y,\{i,j\}}}_{\text{Transmission, once per corridor}} \tag{OBJ-2}
\]

\[
\quad + \underbrace{\sum_{g,n} c^{res}_{g,n} I^{res}_{y,g,n}}_{\text{Reservoir}} \tag{OBJ-3}
\]

\[
\quad + \underbrace{\sum_{c} c^{inv}_{c} I^{conv}_{y,c}}_{\text{Converters}} \tag{OBJ-3a}
\]

\(I^{conv}_{y,c}\) is the MW added to AC/DC or frequency converter \(c\) (`converter_investment`), up to its `invest_max_power` over the horizon, at its `invest_cost` (\$/MW). Like the dispatch, a converter does not age: its capacity is `rated_power_mva` plus everything built so far, and the built MW reach the dispatch as added rated power from the year they are built.

Existing generators and batteries have no investment variable; their capacity is fixed (§6). With primary energy on, the master also builds fuel storage, route transport capacity and electrolyzers (§8.3.12, within the limits of [Primary Energy](primary-energy.md) PE-5d); the operational dispatch builds none and uses the master's builds as existing capacity.

### 5.1b Existing-Asset Costs


\[
C^{asset}_y = \sum_{g,n} \phi^{ext} c^{inv}_{g,n} X_{y,g,n} + \sum_{b,n} \phi^{ext} c^{inv,P}_{b,n} X^{bat}_{y,b,n} + C^{dec}_y + C^{fix}_y \tag{OBJ-3b}
\]

\[
C^{fix}_y = \sum_{g,n} c^{fix}_{g,n} \bar{P}^{cum}_{y,g,n} + \sum_{k,n} c^{fix}_{k,n} \bar{P}^{tech}_{y,k,n} + \sum_{b,n} c^{fix}_{b,n} \bar{P}^{bat,cum}_{y,b,n} + \sum_{k,n} c^{fix}_{k,n} \bar{P}^{bt}_{y,k,n} \tag{OBJ-3c}
\]

\(C^{fix}_y\) is the fixed O&M of the capacity in service in year \(y\), each unit at its `fixed_cost` (\$/MW/year), whether or not it runs: existing generators within their lifetime at their degraded power, their life-extended MW after it (CUM-1), the technology MW built, degraded and not yet retired (CUM-1b), an existing battery's `MaxDischargePower` within its lifetime (its life-extended MW after it, CUM-2), and the power built from each battery technology and not yet retired (CUM-3). These are the MW the dispatch receives as rated power, and the dispatch charges the same cost prorated to each window (OBJ-1a and OBJ-3c of the [Operational Dispatch](operational-dispatch.md)): the master's yearly fixed O&M is \(H_{year}/(T\Delta t)\) times a window's. Built converter capacity also pays its `fixed_cost` (\$/MW/year) every year, included in \(C^{asset}_y\). The Benders master has the same \(C^{asset}_y\). Fixed O&M was charged per MWh generated in \(C^{day}\) in earlier versions.

Every MW kept past its lifetime pays \(\phi^{ext}\) (`life_extension_cost_factor`) times its investment cost each year. \(C^{dec}_y\) is the decommissioning cost: in a unit's first retirement year, \(c^{dec}\) times its (degraded) capacity minus the extended capacity; in later years, \(c^{dec}\) times the drop in extended capacity, \(X_{y-1} - X_y\).

Where an existing unit's investment cost is 0 at a node (the Studio default), \(c^{inv}\) is taken from the unit's candidate when the master input is built (`life_extension_invest_costs` in `bridge/adapters.py`): for a generator, the technology with the same `fuel` and `type` (the one its `technology` field names if several match, else the first in configuration order); for a battery, the battery technology its `technology` field names, else the first battery technology (`invest_cost_power`). In a multi-system run, where keys are `{system}__{key}`, the candidate comes from the unit's own system and its cost at the unit's own nodes. A unit with no candidate, or whose candidate has no cost at one of its nodes, keeps \(c^{inv} = 0\) there, so its extension is free; a warning names it once per run.

### 5.2 Operational Cost


Operational costs come from the representative day subproblems, scaled to a year:

\[
C^{op}_y = \frac{\Delta t \cdot H_{year}/24}{D_r} \sum_{d \in \mathcal{D}_y} C^{day}_{y,d} \tag{OBJ-4}
\]

where \(\Delta t\) is `temporal_resolution_hours` and \(H_{year}\) the hours of the first year (`hours_per_year[1]`, always 8760: every modelled year has 8760 h, leap years included). The cost for each representative day is (from `day_operational_cost`):

\[
\begin{aligned}
C^{day}_{y,d} = \sum_{t=1}^{H} \Big[ & \sum_{g,n} \left(c^{fuel}_{g,n} + c^{maint}_{g,n} + c^{CO_2}_g\right) P^{day}_{g,n,t} + \sum_{k,n} \left(c^{fuel}_{k,n} + c^{maint}_{k,n} + c^{CO_2}_k\right) P^{tech}_{k,n,t} \\
& + c^{VOLL} \sum_{n} L^{day}_{n,t} + c^{fre} \sum_{n} \lambda^{fre}_{n,t} + c^{CU} \sum_n CU^{day}_{n,t} + \sum_{b,n} \left( c^{maint}_{b,n} (P^{ch,day}_{b,n,t} + P^{dis,day}_{b,n,t}) + c^{\text{thr}}_{b,n} P^{dis,day}_{b,n,t} \right) \\
& + \sum_{k,n} \left( c^{maint}_{k,n} (P^{bt,ch}_{k,n,t} + P^{bt,dis}_{k,n,t}) + c^{\text{thr}}_{k,n} P^{bt,dis}_{k,n,t} \right) \\
& + c^{r,sta} \sum_n \lambda^{sta}_{n,t} + c^{r,dyn} \sum_n \lambda^{dyn}_{n,t} + c^{ine} H^{day}_t \Big]
\end{aligned} \tag{OBJ-5}
\]

\[
\quad + \sum_{t=1}^{H} \sum_{s \in \mathcal{S}} \sum_{n} c^{VOLL} \cdot (\kappa_s - 1) \cdot L^{sec,day}_{s,n,t} \tag{OBJ-6}
\]

\[
\quad + \sum_{t=1}^{H} \Big[ \sum_{c} c^{var}_c \left(P^{c,1}_{c,t} + P^{c,2}_{c,t}\right) + \sum_{\ell} c^{flow}_\ell \, |f^{day}_{\ell,t}| \Big] + \frac{1}{\Delta t} \sum_{g,n,t} \frac{c^{su}_{g,n}}{\bar{P}_{g,n}} \, SU_{g,n,t} \tag{OBJ-6a}
\]

OBJ-6a is the converters' variable cost on the power they carry in either direction (plus the constant standby term of the dispatch), the flow cost of inter-system links (§8.3.9) and the start-ups of §8.3.10 (an event, not energy: the \(1/\Delta t\) cancels the step length of OBJ-4). The CO2 budget violation of §8.3.11 is added to \(C^{op}_y\) once per year at each system's penalty.

where \(\kappa_s\) is the criticality weight for sector \(s\). The sectoral shed \(L^{sec,day}\) is part of \(L^{day}\) (SEC-2), which OBJ-5 already charges at VOLL; OBJ-6 adds the criticality premium, so a sector's shed MWh costs \(c^{VOLL} \kappa_s\), and the unclassified remainder \(L^{day}_{n,t} - \sum_s L^{sec,day}_{s,n,t}\) gets the premium \(c^{VOLL} (m_{n,t} - 1)\), where \(m_{n,t}\) is the demand-weighted mean of the sector criticalities at the bus's parent node in that hour (1 where no sector has demand), as in the dispatch (OBJ-4a). The carbon cost is the one the dispatch charges, so a carbon price shifts the plan towards lower-emission units. Batteries pay their `maintenance_cost` on every MWh charged and discharged and their throughput cost on discharge, as in the dispatch; for battery technologies (\(k\)) these are the technology's `maintenance_cost` and `throughput_degradation_cost`. When `max_curtailment_ratio` < 1, the curtailment of candidate renewable technologies, \(\sum (\text{capacity} \cdot \alpha - P^{tech})\), is charged at \(c^{CU}\) as well. All of \(C^{day}\) is scaled to the year the same way (OBJ-4, or \(\Delta t \cdot w_p\) for a TSAM period of weight \(w_p\)), in the monolithic, TSAM, multi-system and stochastic masters; the Benders subproblems charge the same day cost except the candidates' curtailment, which they price only above the cap ([§11b](#11b-benders-decomposition)).

!!! note "PWL cost curves in the master problem"
    When existing generators have a `fuel_cost_curve` configured, the representative day operational cost (OBJ-5) uses the same piecewise-linear (PWL) fuel cost decomposition as the operational dispatch formulation (see [Operational Dispatch -- PWL Fuel Cost](operational-dispatch.md#311-piecewise-linear-pwl-fuel-cost-decomposition)). The same applies to existing batteries with `discharge_cost_curve`, which then replaces the throughput cost, and to candidate technologies with a `fuel_cost_curve`: on every bus of its node, the technology's output \(P^{tech}_{k,n,t}\) is split into segments, segment \(j\) capped at \(f_j \cdot\) cumulative capacity \(\cdot\, \alpha_{k,n,t}\) (`tech_seg_cap_t*_b*_k*_ts*`) and charged its own marginal cost instead of the flat `fuel_cost`, as the dispatch prices the generator the technology becomes. The Benders subproblems do the same, and their cuts include the segment caps in the capacity gradient.

### 5.3 Slack Penalties


\[
C^{slack} = \sum_{y=1}^{Y} \left[ c^{slack} \cdot s^{re}_y + c^{slack} \cdot s^{bud}_y + \sum_{n \in \mathcal{N}} c^{slack} \cdot s^{cap}_{y,n} \right] \tag{OBJ-7}
\]

---


## 6. Cumulative Capacity Expressions


For each unit and bus, the capacity available in year \(y\) combines existing (degraded) capacity, life-extended capacity and invested capacity, each subject to age-based retirement. The capacities are built inline where they are used (`add_day_operational_constraints!`, `add_capacity_adequacy_constraints!`); `build_cumulative_capacity_expressions()` evaluates the same expressions for result extraction.

### 6.1 Existing Generators


\[
\bar{P}^{cum}_{y,g,n} = \begin{cases} \bar{P}_{g,n} (1 - \delta_{g,n})^{a_{y,g,n}} & a_{y,g,n} < \tau_{g,n} \\ X_{y,g,n} & \text{otherwise} \end{cases}, \qquad a_{y,g,n} = a^0_{g,n} + y - 1 \tag{CUM-1}
\]

### 6.2 Candidate Technologies


\[
\bar{P}^{tech}_{y,k,n} = \sum_{\substack{y' \in \mathcal{Y}^{inv},\, y' \leq y \\ y - y' < \tau_{k,n}}} (1 - \delta_{k,n})^{y-y'} \, I^{tech}_{y',k,n} \tag{CUM-1b}
\]

The conditions on age are evaluated at model construction time, so no binary variables are needed.

### 6.3 Batteries


Existing batteries are not degraded in the master; past their lifetime their power is the life-extension variable and their energy keeps the original energy-to-power ratio:

\[
\bar{P}^{bat,cum}_{y,b,n} = \begin{cases} \bar{P}^{bat}_{b,n} & a_{y,b,n} < \tau_{b,n} \\ X^{bat}_{y,b,n} & \text{otherwise} \end{cases}, \qquad \bar{E}^{cum}_{y,b,n} = \begin{cases} \bar{E}_{b,n} & a_{y,b,n} < \tau_{b,n} \\ X^{bat}_{y,b,n} \, \bar{E}_{b,n} / \bar{P}^{bat}_{b,n} & \text{otherwise} \end{cases} \tag{CUM-2}
\]

Battery technologies accumulate their investments without degradation:

\[
\bar{P}^{bt}_{y,k,n} = \sum_{\substack{y' \in \mathcal{Y}^{inv},\, y' \leq y \\ y - y' < \tau_{k,n}}} I^{bat,P}_{y',k,n}, \qquad \bar{E}^{bt}_{y,k,n} = \sum_{\substack{y' \in \mathcal{Y}^{inv},\, y' \leq y \\ y - y' < \tau_{k,n}}} I^{bat,E}_{y',k,n} \tag{CUM-3}
\]

### 6.4 Transmission Cumulative Capacity


\[
\bar{F}^{cum}_{y,\ell} = \bar{F}_{\ell} + \sum_{y' \in \mathcal{Y}^{inv},\, y' \leq y} I^{tr}_{y',\{i(\ell),j(\ell)\}} \tag{CUM-4}
\]

The corridor's investment is added to the line once, in whichever direction the variable exists. Transmission investment does not retire.

---


## 7. Constraint Families

### 7.1 Investment Limits (TECH)


**Julia function**: `add_investment_constraints!(model, vars, input)`

**TECH-1: Cumulative technology investment limit** (`cumul_tech_t*_b*`):

\[
\sum_{y \in \mathcal{Y}^{inv}} I^{tech}_{y,k,n} \leq \bar{I}_{k,n} \qquad \forall\, k,\; n : \bar{I}_{k,n} > 0 \tag{TECH-1}
\]

**TECH-2: Cumulative battery-technology limits** (`cumul_bat_tech_pow_*`, `cumul_bat_tech_cap_*`):

\[
\sum_{y \in \mathcal{Y}^{inv}} I^{bat,P}_{y,k,n} \leq \bar{I}^{P}_{k,n}, \qquad \sum_{y \in \mathcal{Y}^{inv}} I^{bat,E}_{y,k,n} \leq \bar{I}^{E}_{k,n} \tag{TECH-2}
\]

**TECH-3: Battery-technology duration**, applied to each period's investment (`bat_tech_min_dur_*`, `bat_tech_max_dur_*`):

\[
h^{min}_{k} \, I^{bat,P}_{y,k,n} \leq I^{bat,E}_{y,k,n} \leq h^{max}_{k} \, I^{bat,P}_{y,k,n} \qquad \forall\, y \in \mathcal{Y}^{inv},\, k,\, n \tag{TECH-3}
\]

**TECH-4: Node-aggregate limits.** Per-node `invest_max` values are copied to every bus of the node, so on nodes with several buses the sum over the node's buses is capped at the node value (`cumul_node_tech_*`, `cumul_node_bat_tech_pow_*`, `cumul_node_bat_tech_cap_*`). Reservoir investment has a cumulative cap per bus (`cumul_res_g*_b*`).

### 7.2 Budget Constraint (BUD)


**Julia function**: `add_budget_constraints!(model, vars, input)`

For each investment-period start year, the investment expenditure must not exceed the budget (with slack), `budget_y{y}`:

\[
\sum_{k,n} c^{inv}_{k,n} I^{tech}_{y,k,n} + \sum_{k,n} \left( c^{inv,P}_{k,n} I^{bat,P}_{y,k,n} + c^{inv,E}_{k,n} I^{bat,E}_{y,k,n} \right) + \sum_{\{i,j\}} \max(c^{tr}_i, c^{tr}_j) I^{tr}_{y,\{i,j\}} + \sum_{g,n} c^{res}_{g,n} I^{res}_{y,g,n} \leq \bar{B} + s^{bud}_y \tag{BUD-1}
\]

Primary-energy investment is not part of the budget.

### 7.3 Transmission Symmetry (TXN)


**Julia function**: `add_transmission_symmetry_constraints!(model, vars, input)`

Transmission investment exists only on corridors that have a line or transformer between buses \(i\) and \(j\), possibly a candidate line of 0 MW such as a development zone's interconnection. \(I^{tr}_{y,i,j}\) is created where the corridor already has capacity or where bus \(i\) allows investment (`transference_invest_max[i] > 0`). Capacity bought between buses with no line could not reach the dispatch, so it is not offered. A line of 0 MW base capacity carries flow once capacity is built on it.

Bidirectional transmission investment is symmetric:

\[
I^{tr}_{y,i,j} = I^{tr}_{y,j,i} \qquad \forall\, y,\; i < j \tag{TXN-1}
\]

A line's capacity in year \(y\) is its base capacity plus the cumulative investment on its corridor, whichever direction the variable exists in. In the dispatch each corridor's investment is added to its line once.

Each corridor is charged once, at the higher of its two ends' `transference_invest_cost` per MW, \(\max(c_i, c_j)\), whether one or both directions exist. An end left at 0 (the default) does not make the line cheaper. A development zone's interconnection therefore costs the larger of the zone's interconnection cost and the connection node's `transference_invest_cost`.

### 7.4 Capacity Adequacy (CAP)


**Julia function**: `add_capacity_adequacy_constraints!(model, vars, input)`

Firm generation and storage capacity, plus net imports at the peak, must meet peak demand with a reserve margin at each node:

\[
\sum_{n' \in n} \Big[ \sum_g \alpha^{cc}_{g,n'} \kappa^{risk}_{g,n'} \bar{P}^{cum}_{y,g,n'} + \sum_k \alpha^{cc}_{k,n'} \kappa^{risk}_{k,n'} \bar{P}^{tech}_{y,k,n'} + \sum_b \kappa^{risk}_{b,n'} \bar{P}^{bat,cum}_{y,b,n'} + \sum_k \kappa^{risk}_{k,n'} \bar{P}^{bt}_{y,k,n'} \Big] + \sum_{m} \left( f^{cap}_{y,m \to n} - f^{cap}_{y,n \to m} \right) + s^{cap}_{y,n} \geq D^{peak}_{y,n} \cdot M_{res} \qquad \forall\, y,\; \text{node } n \tag{CAP-1}
\]

\[
0 \leq f^{cap}_{y,m \to n} \leq \sum_{i \in m,\, j \in n} \left( C_{ij} + \sum_{y' \leq y} I^{tr}_{y',i,j} \right) \tag{CAP-3}
\]

\(f^{cap}_{y,m \to n}\) (`cap_import_y…`) is the peak-hour transfer from node \(m\) to node \(n\), bounded by the existing line capacity \(C_{ij}\) between their buses plus the cumulative transfer investment. A node can therefore rely on a neighbour's surplus instead of building locally; nodal peaks are treated as coincident, which is conservative, and line losses are ignored.

where the peak demand at node \(n\) in year \(y\) is:

\[
D^{peak}_{y,n} = \max_{t \in y} \left[ D_{t,n} \right] \tag{CAP-2}
\]

The constraint (`cap_adequacy_y{y}_node{n}`) is per node and sums over the node's buses. Renewables count at their capacity credit \(\alpha^{cc}\), the minimum of their availability profile (so solar counts as 0); other units count fully. \(\kappa^{risk}\) is the `risk_coefficient` (default 1; see [Risk & Resilience](risk-resilience.md)); life-extended capacity counts at its capacity credit and risk coefficient, like the unit it extends. \(D^{peak}_{y,n}\) is the maximum of the node's demand over the year's time steps.

### 7.5 RE Target Constraints (RE)


**Julia functions**: `calculate_target_ratios(input)`, `add_re_target_constraints!()`, `add_re_increment_constraints!()`

RE targets are set per system \(\sigma\) (one global system when the configuration has a single system).

**RE-1: Target trajectory.** The first year's target is the system's initial penetration; later years interpolate linearly to the final target:

\[
\rho^{target}_{\sigma,1} = \rho^{init}_\sigma, \qquad \rho^{target}_{\sigma,y} = \rho^{init}_\sigma + \frac{y - 1}{Y - 1} \left( \rho^{target} - \rho^{init}_\sigma \right) \quad (y \geq 2) \tag{RE-1}
\]

with the step from the previous year's target clamped to \([\Delta\rho^{min}, \Delta\rho^{max}]\). In myopic planning each one-year master takes its year's \(\rho^{target}_{\sigma,y}\) from this trajectory over the whole horizon (`re_target_override`), while RE-3 starts from the share reached the year before.

**RE-2: RE target** (soft, `re_target_s{σ}_y{y}`):

\[
\rho_{\sigma,y} + s^{re}_y \geq \rho^{target}_{\sigma,y} \qquad \forall\, \sigma,\, y \tag{RE-2}
\]

**RE-3: RE increment bounds** (`re_min_increment_*`, `re_max_increment_*`), with year 1 anchored to the initial penetration:

\[
\Delta\rho^{min} \leq \rho_{\sigma,y} - \rho_{\sigma,y-1} \leq \Delta\rho^{max} \qquad \forall\, \sigma,\, y, \quad \rho_{\sigma,0} = \rho^{init}_\sigma \tag{RE-3}
\]

Each bound is added only when it is active (\(\Delta\rho^{min} > 0\), \(\Delta\rho^{max} < 1\)). The ratio \(\rho_{\sigma,y}\) is tied to the representative-day dispatch by RE-DAY-2.

### 7.6 Retirement and Life Extension (RET)


**Julia function**: `add_retirement_cascade_constraints!()`

**RET-1: Age-based retirement.** An existing unit contributes its degraded capacity while \(a^0 + y - 1 < \tau\); an investment made in year \(y'\) contributes while \(y - y' < \tau\). These conditions are evaluated at model construction time.

**RET-2: Life-extension cascade.** Past its lifetime a unit contributes only \(X_{y,g,n}\) (CUM-1), which can only decrease over time, so a retired MW cannot come back (`cascade_gen_g*_n*_y*`, `cascade_bat_b*_n*_y*`):

\[
X_{y,g,n} \leq X_{y-1,g,n}, \qquad X^{bat}_{y,b,n} \leq X^{bat}_{y-1,b,n} \qquad \forall\, y \geq 2 \tag{RET-2}
\]

---


## 8. Representative Day Validation


Representative day subproblems link strategic investment decisions to operational feasibility. Without them, the Master Problem could select investments that are infeasible in practice.

### 8.1 Representative Day Selection


**Julia function**: `select_representative_days(demand, year_idx, num_days, min_separation, timesteps_per_day, timesteps_per_year)`

The algorithm selects high-demand days with temporal diversity:

1. Compute daily peak demand for each day in the year: \(D^{peak}_d = \max_t \sum_n D_{d,t,n}\)
2. Select the global peak day first.
3. Divide the year into \(D_r\) segments for seasonal diversity.
4. Within each segment, select the day with the highest peak that satisfies a minimum separation constraint (\(\geq\) `min_day_separation` days from any already-selected day).

### 8.2 Operational Variables


**Julia function**: `create_day_ps_vars!(model, input, year_idx, day_idx, hours)`

For each year \(y\) and representative day \(d\), a full set of dispatch variables is created as described in Section 3.3. These variables are embedded within the same JuMP model as the investment variables, allowing the solver to jointly optimize investment and dispatch.

### 8.3 Operational Constraints


**Julia function**: `add_day_operational_constraints!(model, ps_vars, vars, input, year_idx, day_idx, demand, start_hour)`

The day reuses the operational-dispatch constraint builders of `power_system.jl` and `transmission_dc.jl` in economic-dispatch mode, with the capacities of §6 passed in as expressions of the investment variables: generators, batteries, reservoirs, reserves (RES-1 to RES-3), inertia, power balance and curtailment. With primary energy on, the day also has the fuel block of §8.3.12 (fuel balance and electrolyzers). Rooftop solar, EVs, unit commitment and N-1 are not modelled in the master.

#### 8.3.1 Power Balance

For each bus and time step (DC-1 with these extra terms, or PB-1 on a single bus):

\[
\sum_{g} P^{day}_{g,n,t} + \sum_k P^{tech}_{k,n,t} + \sum_{b} P^{dis,day}_{b,n,t} + \sum_k \left( P^{bt,dis}_{k,n,t} - P^{bt,ch}_{k,n,t} \right) + L^{day}_{n,t} = D^{day}_{n,t} + \sum_{b} P^{ch,day}_{b,n,t} + \sum_{\ell} K_{n,\ell} f^{day}_{\ell,t} \tag{PB-1}
\]

where the demand at bus \(n\) is the parent node's demand times the bus demand fraction:

\[
D^{day}_{n,t} = D_{t, \text{parent}(n)} \cdot \alpha_n \tag{PB-2}
\]

\(L^{day}_{n,t} \leq D^{day}_{n,t}\) as in the dispatch (PB-1b). The sectoral loss of load \(L^{sec,day}\) is a split of \(L^{day}\) (SEC-2 below), not a separate injection, so a day sheds at most its demand. Curtailment does **not** appear in the power balance.

#### 8.3.2 Generator Capacity Linked to Investment

\[
P^{day}_{g,n,t} \leq \bar{P}^{cum}_{y,g,n} \cdot \alpha_{g,t',n} \qquad \forall\, g \tag{GEN-1}
\]

\[
P^{tech}_{k,n,t} \leq \bar{P}^{tech}_{y,k,n} \cdot \alpha_{k,t',n} \quad (k \in \mathcal{K}_{RE}), \qquad P^{tech}_{k,n,t} \leq \bar{P}^{tech}_{y,k,n} \quad (k \notin \mathcal{K}_{RE}) \tag{GEN-2}
\]

where \(t' = \text{mod1}(\text{start\_hour} + t - 1,\; T_{avail})\) maps the representative day step to the availability profile (`tech_cap_t*_b*_ts*_y*_d*` for technologies). Ramp limits (GEN-3) apply to existing generators; minimum generation, commitment and minimum up/down times do not.

#### 8.3.3 Battery Constraints Linked to Investment

For existing batteries (via `add_battery_constraints!` with capacity overrides):

\[
P^{ch,day}_{b,n,t},\; P^{dis,day}_{b,n,t} \leq \bar{P}^{bat,cum}_{y,b,n} \, a^{bat}_{b,n,t'}, \qquad E^{day}_{b,n,t} \leq \bar{E}^{cum}_{y,b,n} \tag{BAT-1}
\]

with \(a^{bat}\) the battery's availability profile at the day's step \(t'\) (1 without one). Charge and discharge follow the dispatch's ramp limits (BAT-9) within the day, the battery's power \(\bar{P}^{bat,cum}_{y,b,n}\) standing for both ratings, and only `reservable` batteries hold reserve, up to \(\bar{P}^{bat,cum}_{y,b,n} \, a^{bat}_{b,n,t'}\).

\[
E^{day}_{b,n,t+1} = E^{day}_{b,n,t} + \left( \eta^{ch}_{b,n} P^{ch,day}_{b,n,t} - \frac{P^{dis,day}_{b,n,t}}{\eta^{dis}_{b,n}} \right) \Delta t \tag{BAT-2}
\]

\[
E^{day}_{b,n,1} = E^{day}_{b,n,H+1} = \max(E^0_{b,n}, \underline{E}_{b,n}) \cdot \bar{E}^{cum}_{y,b,n} \tag{BAT-3}
\]

The day starts and ends at the initial SOC (`bat_soc_initial_*`, `bat_soc_cyclic_*`); with TSAM inter-period linking the end is the next boundary value instead (`bat_soc_link_*`). There is no self-discharge, spillage, SOC minimum or charge/discharge exclusivity in the master.

Battery technologies have the same structure (`add_battery_tech_day!`: `bt_dis_cap_*`, `bt_ch_cap_*`, `bt_soc_cap_*`, `bt_soc_init_*`, `bt_soc_dyn_*`, `bt_soc_cyc_*`), with power bounded by \(\bar{P}^{bt}\) × the technology's availability, the technology's ramp limits on charge and discharge (BAT-9 with \(\bar{P}^{bt}\)), SOC by \(\bar{E}^{bt}\), and cyclic SOC at `soc_initial` × \(\bar{E}^{bt}\); their SOC dynamics are multiplied by \(\Delta t\) as in BAT-2. Like the candidate generation technologies they hold no reserve in the master; the batteries they become in the dispatch do when the technology is `reservable`.

#### 8.3.4 Transmission Constraints Linked to Investment

\[
-\bar{F}^{cum}_{y,\ell} \leq f^{day}_{\ell,t} \leq \bar{F}^{cum}_{y,\ell} \tag{TRX-1}
\]

The flows follow the DC power flow of the dispatch (flow–angle coupling DC-2). A candidate line of 0 MW is not fixed to zero flow when it can be expanded.

!!! note "Transmission losses in the Master Problem"
    The master uses `loss_model` like the dispatch, with `pwl_loss_segments_master` segments for `pwl`. Each node link carries the equivalent reactance and resistance of the bus-level lines between its two nodes, so the master's flow split and losses follow the grid. See [DC Power Flow -- Transmission Losses](dc-power-flow.md#transmission-losses).

#### 8.3.5 RE Penetration per Day

For each system \(\sigma\), renewable generation of existing units and candidate technologies must meet the year's target on every representative day (`re_min_day_s*_y*_d*`):

\[
\sum_{g \in \mathcal{G}_{RE}} \sum_{n \in \sigma,t} P^{day}_{g,n,t} + \sum_{k \in \mathcal{K}_{RE}} \sum_{n \in \sigma, t} P^{tech}_{k,n,t} + \sum_{n \in \sigma,t} \lambda^{fre}_{n,t} \geq \rho^{target}_{\sigma,y} \cdot D^{total}_{\sigma,y,d} \tag{RE-DAY-1}
\]

where \(D^{total}_{\sigma,y,d}\) is the system's total demand for this day. The slack \(\lambda^{fre}\) keeps the constraint soft; it is penalized at \(c^{fre}\) in OBJ-5.

The ratio variable is bounded by each day's achieved share, so it equals the worst representative day (`re_ratio_lb_s*_y*_d*`):

\[
\rho_{\sigma,y} \cdot D^{total}_{\sigma,y,d} \leq (\text{left-hand side of RE-DAY-1}) \tag{RE-DAY-2}
\]

#### 8.3.6 Curtailment Limit per Day

When `max_curtailment_ratio < 1.0`, curtailment of existing renewables and of candidate renewable technologies is limited to a fraction of renewable generation (`max_curtailment_y*_d*`):

\[
\sum_{n,t} CU^{day}_{n,t} + \sum_{k \in \mathcal{K}_{RE},n,t} \left( \bar{P}^{tech}_{y,k,n} \alpha_{k,t',n} - P^{tech}_{k,n,t} \right) \leq \bar{\kappa} \left( \sum_{g \in \mathcal{G}_{RE},n,t} P^{day}_{g,n,t} + \sum_{k \in \mathcal{K}_{RE},n,t} P^{tech}_{k,n,t} \right) \tag{CURT-1}
\]

This is a hard constraint. It pushes investment towards storage rather than relying on curtailment to manage surplus RE. The operational dispatch has no such cap; it prices curtailment at `curtailment_cost`. With the cap on, the candidates' curtailment \(\bar{P}^{tech}_{y,k,n} \alpha_{k,t',n} - P^{tech}_{k,n,t}\) is also charged at \(c^{CU}\) in the day's cost, like \(CU^{day}\) in OBJ-5.

#### 8.3.7 Sectoral Loss-of-Load Bounds

Each sector's load shedding cannot exceed the sector's demand at that bus and step (`lol_sec_ub_{s}_y*_d*_n*_t*`):

\[
L^{sec,day}_{s,n,t} \leq \max\left(0,\, D^{sec}_{s,\text{parent}(n),t} \cdot \alpha_n \right) \qquad \forall\, s,\, n,\, t \tag{SEC-1}
\]

The bus's load shed splits into the sectors' shed plus a remainder bounded by the bus demand that no sector accounts for (`lol_sec_sum_*`, `lol_sec_residual_*`):

\[
0 \;\leq\; L^{day}_{n,t} - \sum_{s} L^{sec,day}_{s,n,t} \;\leq\; \max\Big(0,\, D^{day}_{n,t} - \sum_{s} D^{sec}_{s,\text{parent}(n),t} \cdot \alpha_n\Big) \qquad \forall\, n,\, t \tag{SEC-2}
\]

Sectoral demand is scaled by the year's demand growth like \(D^{day}\). The Benders subproblems use the same constraints.

#### 8.3.8 Converters

AC/DC and frequency converters enter the day's bus balances as in the dispatch (`add_converter_constraints!`): converter \(c\) carries \(P^{c,1}_{c,t} \geq 0\) from its first to its second end and \(P^{c,2}_{c,t} \geq 0\) back, each delivered with its efficiency, within its capacity this year:

\[
P^{c,1}_{c,t} + P^{c,2}_{c,t} \leq \bar{P}^{conv}_{c} + \sum_{y' \leq y} I^{conv}_{y',c} \tag{CONV-1}
\]

The master network has one bus per node, so a converter joins its `from_node` and `to_node`.

#### 8.3.9 Flow Cost of Inter-System Links

An electrical inter-system link charges `distance_km` × `cost_per_mw_km` per MWh it carries. In a merged run its node link gets that cost as the line's `flow_cost` (several links between the same two nodes share their capacity-weighted mean), and the day's cost includes \(c^{flow}_\ell |f^{day}_{\ell,t}|\) for each line \(\ell\) with a flow cost, with \(|f|\) an auxiliary variable above \(\pm f\). As in the dispatch, each line pays its own cost: other lines and transformers between the same buses pay none.

#### 8.3.10 Start-ups (LP)

When the dispatch commits units (`master_problem.use_uc_in_dispatch`), each thermal or reservoir generator with a start-up cost or a minimum power gets an online capacity \(U_{g,n,t}\) (MW), a continuous relaxation of the dispatch's on/off status:

\[
U_{g,n,t} \leq \bar{P}^{cum}_{y,g,n}, \qquad \underline{p}_{g,n} U_{g,n,t} \leq P^{day}_{g,n,t} \leq U_{g,n,t}, \qquad SU_{g,n,t} \geq U_{g,n,t} - U_{g,n,t-1} \tag{SU-1}
\]

with \(U_{g,n,0} = U_{g,n,H}\) (the day repeats). Starting the whole unit (\(SU = \bar{P}_{g,n}\), its degraded rated power this year) costs one `start_up_cost`, as one start in the dispatch. Minimum up and down times are not modelled, and candidate technologies have no start-ups in the master. With economic dispatch (the default) the dispatch has no start-up cost and neither has the master.

#### 8.3.11 CO2 Budget

Each system's annual CO2 budget holds in the master as in the dispatch (one budget per system in a merged run, `co2_budget_groups`; one over all nodes otherwise). The emissions of the generators and candidate technologies at the system's nodes, over the representative days scaled to the year like their cost, stay within the budget plus a violation slack:

\[
\frac{\Delta t \cdot H_{year}/24}{D_r} \sum_{d} \sum_{t} \Big( \sum_{g,n \in \sigma} e_{g,n} P^{day}_{g,n,t} + \sum_{k,n \in \sigma} e_{k,n} P^{tech}_{k,n,t} \Big) \leq B_\sigma + V_{\sigma,y} \tag{CO2-1}
\]

(\(\Delta t \cdot w_p\) for a TSAM period), with \(e\) the emission factor per MWh of electricity (`co2_per_mwh`). \(V_{\sigma,y}\) is charged at the system's `co2_budget_violation` penalty in \(C^{op}_y\). A budget that is disabled or not finite adds nothing.

<a id="fuel-block"></a>

#### 8.3.12 Fuel Balance, Fuel Infrastructure and Electrolyzers

**Julia file**: `master_fuel.jl` (`add_master_fuel_day!`). Active whenever primary energy is: `enable_primary_energy: true` and the system has `primary_energy_sources` or `fuel_entry_points`, the same switch as the dispatch's fuel model.

On each representative day (or TSAM period) the master builds the dispatch's own [primary-energy model](primary-energy.md) for that day, with the same fuel prices, import costs, supply limits, tanks, routes, non-electric demand and penalties, so master and dispatch charge the same fuel costs. Three things differ, because a representative day is not a chronological window:

- each tank ends the day at the level it started with (`hr_storage_cyclic_*`, also for TSAM periods), so drawing it down is worth nothing;
- route shipments arrive the same day (no transit delay: in a cyclic day a delayed shipment that left in the last steps would never arrive, and with one supply period a day the flow leaves evenly, so wrapping the delay around the day would deliver the same);
- the supply-limit period is the day, which is the dispatch's default 24 h `primary_energy_resolution`.

Supply limits and disruption windows use the day's absolute horizon hours, as in the dispatch.

**Fuel-coupled units.** A generator or candidate technology whose fuel is in the model (it has an `energy_content`) burns tank fuel: \(\sum_{b \in n} \Delta t \, P_{g,b,t} \le \kappa_g W^{elec}_{g,n,t}\), with \(\kappa_g\) = mean `eff_at_rated` × `energy_content` (PE coupling, `pe_coupling_gen*` and `pe_coupling_tech*`). Like the dispatch, the master then does not also charge such a unit its flat `fuel_cost`: the fuel is paid through the fuel balance, at the fuel's price plus the node's import cost. A unit with a fuel-cost curve keeps its curve.

**Electrolyzers.** Each electrolyzer draws power at its bus (`op_y*_d*_elz`), a load in the power balance, capped at its existing plus built power and ramp-limited as in the dispatch. Its hydrogen fills the node's `Hydrogen` tank. Without a `Hydrogen` fuel it is only a load. Without primary energy the master has no electrolyzers.

**Investments** (per investment-period start year \(y\)): fuel storage \(I^{stor}_{f,n,y}\) (`fuel_storage_inv_y{y}_{fuel}`), route capacity \(I^{trans}_{f,r,y}\) (`fuel_transport_inv_y{y}_{fuel}`) and electrolyzer power \(I^{elz}_{e,b,y}\) (`elz_inv_y{y}_e{e}`), within the expansion limits of [PE-5d](primary-energy.md) and `invest_max_power` over the horizon. Fuel capacity stays in service for the rest of the horizon. An electrolyzer build retires after its `life_time`. Costs:

| Cost | Charged as | Where |
|---|---|---|
| Fuel storage | `investment_cost` of the `fuel_infrastructure` storage facility × units, overnight at the build year | investment cost (§5.1) |
| Route capacity | `transport_investment_cost` × km × units/day, overnight | investment cost (§5.1) |
| Electrolyzer power | `invest_cost` × MW, overnight | investment cost (§5.1) |
| Electrolyzer fixed cost | `fixed_cost` (\$/MW·yr) × (rated + built and not yet retired) MW, once per year | existing-asset cost (§5.1b) |
| Fuel bought, route transport, fuel shortfall and excess, unmet non-electric demand | as in the dispatch | day cost |
| Electrolyzer variable and water cost | \(\Delta t\,(c^{var}_b + c^{water}\,h_b)\,P^{elz}_{b,t}\), with \(h_b\) the kg of hydrogen per MWh | day cost |

The day's fuel cost is already in \$ (fuel amounts per step), so it is scaled to the year by \((H_{year}/24)/|\mathcal{D}_y|\), or by the TSAM period weight, without the \(\Delta t\) of SCALE-1.

**Hand-off to the dispatch.** The master is the only model that builds fuel infrastructure and electrolyzers. Each dispatch year receives the storage and route capacity in service that year as existing capacity (`cumulative_capacities` of the fuel model), and the built electrolyzer power is added to each electrolyzer's `rated_power`. A myopic master starts each year from what earlier years built and is still in service (storage and routes never retire; an electrolyzer build retires after its `life_time`, as in perfect foresight), with less room to build by everything built so far. A run without a master (`economic_dispatch` or `unit_commitment` mode) builds no fuel infrastructure.

### 8.4 Cost Scaling


Each representative day's operational cost is scaled to approximate annual costs:

\[
C^{op}_y = \frac{\Delta t \cdot H_{year}/24}{|\mathcal{D}_y|} \sum_{d \in \mathcal{D}_y} C^{day}_{y,d} \tag{SCALE-1}
\]

**Julia function**: `add_representative_days_validation!(model, vars, input, targets)` orchestrates the creation of variables, constraints, and cost expressions for all representative days across all years.

### 8.5 Time-Series Aggregation Method (TSAM)


When `use_tsam = true`, the peak-demand-based day selection (Section 8.1) is replaced by data-driven clustering with inter-period SOC linking.

**Python function**: `compute_tsam_periods()` in `models/tsam.py`
**Julia function**: `add_tsam_periods_validation!(model, vars, input, targets)`

#### 8.5.1 Clustering Algorithm

The annual demand series is reshaped into daily blocks and clustered using k-medoids (default) or k-means:

1. Build feature matrix \(F \in \mathbb{R}^{365 \times (H \cdot N)}\) from daily demand blocks
2. Optionally concatenate normalized availability profiles as additional features
3. Standardize features (zero mean, unit variance)
4. Apply k-medoids: select \(K\) cluster medoids as representative periods
5. Assign each original day to the nearest cluster

#### 8.5.2 Period Weights

Each representative period \(p\) has a weight equal to its cluster size:

\[
w_p = |\mathcal{C}_p|, \qquad \sum_{p=1}^{K} w_p = 365 \tag{TSAM-W}
\]

The operational cost scaling becomes:

\[
C^{op}_y = \sum_{p=1}^{K} w_p \cdot C^{day}_{y,p} \tag{TSAM-COST}
\]

This replaces the uniform \(365/N\) scaling (SCALE-1). Only the scaling differs: each period's \(C^{day}_{y,p}\) is the same cost as for a representative day, including the reserve and inertia shortfall penalties.

#### 8.5.3 Inter-Period SOC Linking

To enable seasonal storage representation, inter-period SOC boundary variables are introduced:

**Variables**: \(\text{SOC}^{bnd}_{y,b,n,p}\) for \(p = 0, 1, \ldots, K\), where \(p=0\) represents the year start.

**SOC chain** (periods ordered chronologically):

\[
\text{SOC}_{b,n,1}^{(p)} = \text{SOC}^{bnd}_{y,b,n,p-1} + \eta^{ch} \cdot p^{ch}_{b,n,1} - \frac{p^{dis}_{b,n,1}}{\eta^{dis}} \tag{TSAM-SOC1}
\]

\[
\text{SOC}_{b,n,T}^{(p)} = \text{SOC}^{bnd}_{y,b,n,p} \tag{TSAM-SOC2}
\]

**Year-cyclic constraint**:

\[
\text{SOC}^{bnd}_{y,b,n,K} = \text{SOC}^{bnd}_{y,b,n,0} \tag{TSAM-CYC}
\]

**Capacity bound**:

\[
0 \leq \text{SOC}^{bnd}_{y,b,n,p} \leq \bar{E}^{total}_{b,n,y} \quad \forall p \tag{TSAM-CAP}
\]

When `tsam_inter_period_linking = false`, each period is cyclic on its own (BAT-3), and only the weighted cost scaling is active.

---


## 9. NPV-Based Iterative Retirement


**Julia function**: `solve_with_npv_iteration(input; max_iterations=5, npv_threshold=0.0)`
**Run setting**: `npv_retirement` (system settings, off by default), with `npv_retirement_max_iterations` and `npv_retirement_threshold`.

With `npv_retirement` on, a `development` run checks after the master solve whether each existing generator and battery pays for itself in the solved plan, retires for the whole horizon the units that do not, and solves the master again. Off, the master retires units by age and life extension only (Sec. 7.6) and the run is unchanged. The runner applies it to every master mode: the monolithic master, Benders decomposition, the stochastic master and each annual master of a myopic run (a unit a myopic year retires stays retired in the later years, which do not charge its decommissioning again; only the original units are checked, not earlier years' builds). With MGA, the NPV retirement runs first and the alternatives keep its retirements.

### 9.1 Algorithm


```
1. Solve the master
2. Loop:
   a. NPV of each existing unit location (generator g or battery b at bus n)
      in service in some year of the plan (NPV-1, NPV-2)
   b. Units with NPV < npv_retirement_threshold: none → stop (converged)
   c. max_iterations rounds of retirements done → stop (they are kept)
   d. Retire them for the whole horizon (9.3) and solve the master again
```

A retired unit stays retired in the next rounds. The log lists each round's retired units with their NPV.

### 9.2 Unit NPV


**Julia functions**: `calculate_unit_npv(result, input, unit_type, unit_idx, node)`, `unit_npvs(result, input)`, `extract_unit_operation(model, vars, input)`

The NPV is that of keeping the unit in the solved plan, over the modelled years, discounted as the objective discounts them (OBJ-1):

\[
\text{NPV}_{g,n} = \sum_{y=1}^{Y} \frac{E_{y,g,n} \cdot p^{LCOE} - C^{op}_{y,g,n} - C^{asset}_{y,g,n}}{(1+r)^{y-1}} \tag{NPV-1}
\]

\[
\text{NPV}_{b,n} = \sum_{y=1}^{Y} \frac{\rho \cdot c^{inv,P}_{b,n} \cdot \bar{P}^{bat,cum}_{y,b,n} - C^{op}_{y,b,n} - C^{asset}_{y,b,n}}{(1+r)^{y-1}} \tag{NPV-2}
\]

- \(E_{y,g,n}\) is the energy the master dispatched the generator in year \(y\): its output on the representative days (or TSAM periods), scaled to the year as their cost is (SCALE-1, \(\Delta t \cdot\) the days each one stands for). The generation comes from the master's own operation, not from a fixed capacity factor, and already holds the unit's degradation and the end of its life (CUM-1).
- \(p^{LCOE}\) is `base_lcoe` (\$/MWh), \(\rho\) is `npv_annual_return_rate` and \(c^{inv,P}_{b,n}\) the battery's power investment cost; \(\bar{P}^{bat,cum}_{y,b,n}\) its discharge power in service (CUM-2).
- \(C^{op}_{y,g,n}\) is what the master charges the unit for that operation (\(C^{day}\), scaled the same way): its fuel (`fuel_cost`, or its offer curve), maintenance per MWh, the CO2 price on its emissions and its LP-relaxed start-ups (unit commitment in the dispatch). With primary energy on, a fuel-coupled unit pays no `fuel_cost` in the master (it burns tank fuel, §8.3.12); its NPV charges the fuel it burns at the fuel's price that year (`price_base` grown at `price_growth_rate`) per MWh it makes from a unit of fuel. The fuel system's import, transport and shortfall costs are not shared out to the units. For a battery, maintenance on the MWh charged and discharged and its throughput (or offer-curve) cost on the discharge.
- \(C^{asset}_{y,g,n}\) is the unit's share of OBJ-3b: fixed O&M on its capacity in service, life extension past its lifetime and its decommissioning in the plan (at the end of its life if that falls within the horizon).

The terms are the same expressions the objective uses (`add_gen_step_cost!`, `add_bat_step_cost!`, `build_annual_asset_cost`), evaluated at the solution: summed over the units of a system where every cost is a unit's, they make up the master's objective. In a stochastic master the NPV is the probability-weighted mean over the scenarios, each with its own operation, cost multipliers and discount rate \(r_\omega\) (STOCH-1). Benders takes the operation from the subproblems of the iteration whose plan it returns.

A unit with \(\text{NPV} <\) `npv_retirement_threshold` (\$, default 0) is retired. A unit no year of the plan keeps in service (past its lifetime, not extended) is not checked.

### 9.3 Retirement


**Julia function**: `retire_units(input, units; decommission=true)`

A retired unit has no capacity at its bus for the whole horizon: no dispatch, no life extension, no fixed O&M and no end-of-life decommissioning. It pays its decommissioning cost once, in the first year, on its (degraded) capacity then, as a unit retiring at the end of its life does (\(C^{dec}_y\), OBJ-3b; `npv_retired_units` of the master input). The master is then solved again from scratch, so the same mechanism works for every solver.

In the run, the retired generators have no capacity in the dispatch from the first year on (the master's cumulative capacity is 0), and the retired batteries no power or energy at their node. The results file lists them in the `npv_retirements` group ([HDF5 Output Schema](../reference/hdf5-output-schema.md)), which the dashboard's retirement chart also reads.

### 9.4 Result


`solve_with_npv_iteration` (Julia API; `solver = :benders` for Benders) returns an `NPVIterationResult` with the number of master solves, whether the last one left no unit below the threshold, the final `MasterProblemResult`, the retired units (`UnitNPV` records) and, per solve, the NPV of every unit checked. A result extracted with `extract_master_solution(model, vars, input; unit_operation = true)` (or by `run_benders_decomposition(...; record_unit_operation = true)`) holds the units' operation (`unit_operation`) that `calculate_unit_npv` needs.

---


## 10. Multi-System Planning


For planning across several interconnected systems (e.g. countries or regions), the runner merges all systems listed in `meta_network.systems` into one system before building the master (`Orchestrator._merge_systems` in `runner.py`). Per-node arrays are padded so that each system's units sit on its own nodes, and the inter-system links (`meta_network.systems_links`) are added to the merged network as ordinary transmission lines. The merged system is then solved by the single master described above: one set of investment variables, one objective and one discount rate, with the RE targets of §7.5 set per system (`system_node_ranges`). The operational dispatch runs on the merged bus-level network in the same way.

---


## 11. Stochastic Extension


**Julia function**: `create_stochastic_master_problem(input::StochasticMasterInput)`

The stochastic formulation extends the deterministic Master Problem to handle uncertainty through scenario-based optimization.

### 11.1 Scenario Definition


Each scenario \(\omega\) has:

- A probability weight \(\pi_\omega\) where \(\sum_\omega \pi_\omega = 1\)
- A set of multipliers (`ScenarioMultipliers`) that scale costs and parameters:

| Multiplier | Description |
|------------|-------------|
| `invest_cost_renewables` | Scales RE technology investment costs |
| `invest_cost_conventional` | Scales conventional technology investment costs |
| `invest_cost_storage` | Scales battery technology investment costs (power and energy) |
| `bat_invest_cost_power`, `bat_invest_cost_capacity` | Scale the battery power and energy costs, on top of `invest_cost_storage` |
| `invest_cost_transmission` | Scales transmission investment costs |
| `gen_decommissioning_cost`, `bat_decommissioning_cost` | Scale the decommissioning costs of existing generators and batteries |
| `fuel_cost` | Scales fuel costs in the scenario's operation |
| `fuel_price_growth` | Extra annual fuel price escalation in the scenario's operation |
| `gen_fixed_cost` | Scales the fixed O&M (\$/MW/year, OBJ-3c) of generators, technologies, batteries and battery technologies |
| `maintenance_cost` | Scales maintenance costs in the scenario's operation |
| `carbon_price` | Scales the CO2 cost in the scenario's operation |
| `penalty_loss_of_load`, `penalty_loss_of_reserve`, `penalty_curtailment`, `penalty_fre_penetration` | Scale the loss-of-load (incl. sectoral), reserve-shortfall, curtailment and RE-shortfall penalties in the scenario's operation |
| `penalty_co2_budget_violation` | Scales the CO2 budget violation penalty (§8.3.11) |
| `bat_maintenance_cost` | Scales battery and battery-technology maintenance, on top of `maintenance_cost` |
| `gen_start_up_cost` | Scales generator start-up costs (§8.3.10) |
| `converter_invest_cost`, `converter_variable_cost` | Scale the converter investment (OBJ-3a) and variable (OBJ-6a) costs |
| `transmission_cost_per_mw_km` | Scales the flow cost of inter-system links (§8.3.9) |
| `demand_growth` | Annual demand factor of the scenario's operation (demand × factor^(y−1)) |
| `discount_rate` | Scenario-specific discount rate adjustment |

See [Stochastic Programming](stochastic-programming.md#22-scenario-multipliers) for the legacy keys the loader translates.

### 11.2 Two-Stage Structure


**First stage (here-and-now)**: Investment and life-extension decisions (\(I^{tech}\), \(I^{bat,P}\), \(I^{bat,E}\), \(I^{tr}\), \(X\)) are common across all scenarios. Each scenario values them with its own investment-cost multipliers.

**Second stage (wait-and-see)**: Each scenario has its own representative-day dispatch, built on its own demand and operating costs by `scenario_master_input()`.

### 11.3 Stochastic Objective


**Julia function**: `build_stochastic_objective!(model, vars, input, scenarios, scenario_costs)`

\[
\min \; Z^{stoch} = \sum_{\omega} \pi_\omega \sum_{y=1}^{Y} \frac{C^{inv}_{y,\omega} + C^{asset}_{y,\omega} + C^{op}_{y,\omega}}{(1+r_\omega)^{y-1}} + C^{slack} \tag{STOCH-1}
\]

where \(r_\omega = r \cdot m^{discount}_\omega\) is the scenario-adjusted discount rate, \(C^{inv}_{y,\omega}\) the investment cost with the scenario's multipliers (period-start years only), \(C^{asset}_{y,\omega}\) the life-extension and decommissioning cost (decommissioning scaled by the scenario's multipliers), and \(C^{op}_{y,\omega}\) the operational cost of the scenario's own dispatch. See [Stochastic Programming](stochastic-programming.md).

---


## 11b. Benders Decomposition


**Julia file**: `benders.jl` (entry point `run_benders_decomposition`). Selected with `master_problem.solver_method: benders`; the default is `monolithic`.

Instead of one LP with all representative days embedded, Benders solves an investment-only master and one dispatch subproblem per year and representative day, linked by optimality and feasibility cuts. It reaches the monolithic optimum: the master has exactly the investment constraints and costs of the monolithic master, and the subproblems exactly its representative-day operation.

**Master** (`create_benders_master`): the variables and constraints of §3 and §7 (TECH, BUD, RET, CAP, TXN, RE-2, RE-3) without representative days, plus one recourse variable \(\theta_{y,d} \geq 0\) per year and representative day (`theta_y{y}_d{d}`, in the cost units of the input, M\$ from the bridge) that estimates that day's operating cost:

\[
\min \; \sum_{y \in \mathcal{Y}^{inv}} \frac{C^{inv}_y}{(1+r)^{y-1}} + \sum_{y} \frac{C^{asset}_y + \sum_d \theta_{y,d}}{(1+r)^{y-1}} + C^{slack} \tag{BEN-1}
\]

\(C^{asset}_y\) is the monolithic master's (`build_annual_asset_cost`): life extension, decommissioning, the fixed cost of the converter capacity built and of the batteries in service, and the electrolyzers' fixed cost. The master has no constraint of its own linking investment to the RE ratio or to storage: the RE ratio is priced through its copy in the subproblems (`re_ratio_lb_*`), and the cuts carry the value of every capacity.

**Subproblems** (`create_benders_subproblem`): the representative-day dispatch of §8.3 with the capacities fixed at the master's values, including the converters, the link flow cost, the LP start-ups, the candidates' curtailment cost, the full loss-of-load penalty (VOLL and the sectoral premiums) and the fuel block. Each day's RE share must be at least the year's target (`re_min_day_*`) and the master's RE ratio (`re_ratio_lb_*`), and its curtailment at most \(\bar{\kappa}\) times its renewable output (`max_curt_y*_d*`, a hard limit), as in the monolithic model.

**Copy variables**: every master quantity a subproblem reads, other than the technology and battery-technology capacities (their gradient is the duals of the constraints holding them, each times its coefficient: availability on the power limits, ramp rate × \(\Delta t\) on the ramp limits), enters it as a copy variable fixed to the master's value (`copy_*`, `fuel_cap_*`): the converter MW built up to the year, the MW built on each line's corridor, the reservoir MWh built up to the year (the level bounds of §8.3 include it), a life-extended unit's capacity, each system's RE ratio, the fuel storage, route and electrolyzer capacity in service (§8.3.12) and the CO2 allocations below. The dual of the fixing constraint is that quantity's gradient in the cut.

**CO2 budget** (§8.3.11): the annual budget of a group couples all representative days of a year, so the master shares it out. It has a variable \(a_{\sigma,y,d} \geq 0\) per system (budget group), year and day (`co2_alloc_y{y}_{group}_d{d}`), the share of the budget day \(d\) may use, with

\[
\sum_{d} a_{\sigma,y,d} \leq 1 \tag{BEN-CO2-1}
\]

(`co2_alloc_budget_y{y}_{group}`). Subproblem \(d\) holds its emissions (the left-hand side of CO2-1 for that day alone, scaled to the year like its cost) to its share of the budget plus a violation slack priced at the system's penalty:

\[
\frac{\Delta t \cdot H_{year}/24}{D_r} \sum_{t} \Big( \sum_{g,n \in \sigma} e_{g,n} P^{day}_{g,n,t} + \sum_{k,n \in \sigma} e_{k,n} P^{tech}_{k,n,t} \Big) \leq a_{\sigma,y,d} \, B_\sigma + v_{\sigma,y,d} \tag{BEN-CO2-2}
\]

(`co2_budget_y{y}_d{d}_{group}`, slack `co2_budget_violation_y{y}_d{d}_{group}`). Summed over the days this is CO2-1 with \(V_{\sigma,y} = \sum_d v_{\sigma,y,d}\) at the same price, so both have the same optimum. The cuts carry each day's marginal cost of allowance to the master, which moves the budget to the days where it saves the most.

**Cuts and loop**: after each master solve, all subproblems are solved and the duals of their capacity constraints and copy variables give one optimality cut per year, representative day and iteration (`benders_cut_y{y}_d{d}_iter{k}`, multi-cut): \(\theta_{y,d} \geq\) intercept + gradient × (technology and battery-technology capacities, copied quantities). A renewable technology's gradient also includes \(c^{CU}\) times its availability over the day: the curtailment cost its capacity adds. Every master quantity a subproblem reads has a gradient, so the cut is added for every day and iteration, also when all gradients are zero (\(\theta_{y,d}\) is then at least the day's operating cost). One cut per day, rather than one per year, lets the master see how each day's cost depends on its own quantities, such as its share of the CO2 budget.

When the master's capacities cannot meet a day's curtailment limit (more renewable output than the day can use, store or curtail within \(\bar{\kappa}\)), the subproblem is infeasible. It is then re-solved with the limit's slack `curt_excess_y*_d*` freed and minimised, and the duals of that problem give a feasibility cut (`benders_feas_y{y}_d{d}_iter{k}`): intercept + gradient × (capacities, copied quantities) \(\leq 0\), which every plan meeting the limit satisfies.

Before the first master solve, warm-start cuts are added from two high-investment points (large candidate capacity, and moderate renewable and battery capacity). Generators and batteries in service enter them at the capacity the subproblems give them (degraded rated power).

The lower bound is the master objective; an iteration whose subproblems all solve within the curtailment limit gives an upper bound, the first-stage cost plus the subproblem costs. The loop stops when the relative gap is below `benders_tolerance` (default 10⁻⁴) or after `benders_max_iterations` (default 50). Either way, the plan returned is that of the iteration with the best upper bound, with that iteration's operating costs, so the reported objective is what the plan costs. With verbose output, a master or subproblem that fails to solve is written as an LP file to the system's temporary folder.

Benders does not support stochastic scenarios (they are ignored with a warning). It always uses the peak-based representative days of §8.1. With `use_tsam` or user investment constraints (`custom_constraints` with `target: investment`), which the Benders master does not model, the runner solves the monolithic master instead and logs a warning saying why.

---


## 12. Solution Extraction


**Julia function**: `extract_master_solution(model, vars, input)`

After optimization, the following quantities are extracted into a `MasterProblemResult`:

| Output | Description |
|--------|-------------|
| `tech_investment[y][k]` | Technology investment per period-start year and bus (MW) |
| `bat_tech_power_investment[y][k]`, `bat_tech_capacity_investment[y][k]` | Battery-technology investment (MW, MWh) |
| `transfer_investment[y][(i,j)]` | Transmission investment (MW) |
| `gen_life_extension[y][g]`, `bat_life_extension[y][b]` | Per node, the share of the existing unit in service: 1 within its original `life_time`; past it, the MW kept by life extension over the most it could keep (the degraded rated power; the discharge power for a battery), 0 when it retires. A share, not MW: the capacity is in the cumulative capacities below. The retirement reports (`retirement_gen_{g}` in the HDF5 file, the log tables) are 1 − this share |
| `cumulative_gen_capacity`, `cumulative_bat_capacity`, `cumulative_bat_power` | Capacity of existing units per year (CUM-1, CUM-2) |
| `cumulative_tech_capacity`, `cumulative_bat_tech_power`, `cumulative_bat_tech_capacity` | Cumulative technology capacity per year (CUM-1b, CUM-3) |
| `re_penetration_by_year`, `re_penetration_by_system` | RE ratio \(\rho\) per year (and per system) |
| `total_investment_by_year`, `total_operational_cost_by_year` | Investment (undiscounted overnight cost of every build of the year: technologies, battery technologies, transmission, converters, reservoirs, fuel storage, routes and electrolyzers) and operating cost per year (\$, undiscounted: the representative days or TSAM periods scaled to the year, CO2-budget violations included; the expected value over the scenarios in a stochastic master; the subproblems' cost in Benders). The annual cost of the assets in service (fixed O&M, life extension, decommissioning) is in neither |
| `reservoir_investment` | Reservoir storage investment |

The runner turns the cumulative technology capacity into virtual generators and batteries for the operational dispatch (§16.3).

---


## 13. Infeasibility Diagnostics


**Julia function**: `diagnose_infeasibility(model, vars, input)`

If the model solves successfully but with non-zero slack, the diagnostics report which constraints required relaxation:

| Slack Variable | Indicates |
|----------------|-----------|
| \(s^{re}_y > 0\) | RE target cannot be met in year \(y\) |
| \(s^{bud}_y > 0\) | Investment budget exceeded in year \(y\) |
| \(s^{cap}_{y,n} > 0\) | Capacity inadequacy at node \(n\) in year \(y\) |

---


## 14. Implementation Summary

### Function Call Graph

```
create_master_problem(input)
  |-- calculate_target_ratios(input)             # RE-1
  |-- build_master_variables!(model, input)       # All variables (Sec. 3)
  |-- add_investment_constraints!()               # TECH-1 to TECH-4
  |-- add_budget_constraints!()                   # BUD-1
  |-- add_retirement_cascade_constraints!()       # RET-2
  |-- add_capacity_adequacy_constraints!()        # CAP-1, CAP-3
  |-- add_transmission_symmetry_constraints!()    # TXN-1
  |-- add_representative_days_validation!()       # Sec. 8 (or add_tsam_periods_validation!)
  |     |-- select_representative_days()          # Sec. 8.1
  |     |-- create_day_ps_vars!()                 # Sec. 8.2
  |     |-- add_day_operational_constraints!()    # Sec. 8.3
  |     +-- calculate_day_operational_cost()      # OBJ-5, SCALE-1
  |-- add_re_target_constraints!()                # RE-2
  |-- add_re_increment_constraints!()             # RE-3
  |-- apply_user_constraints!()                   # custom constraints
  +-- build_master_objective!()                   # OBJ-1

solve_with_npv_iteration(input)                  # Sec. 9 (the runner: MasterProblemAdapter.solve)
  +-- loop:
        |-- create_master_problem(); optimize!()  # or run_benders_decomposition()
        |-- extract_master_solution(...; unit_operation = true)
        |-- unit_npvs()                           # NPV-1, NPV-2
        +-- retire_units()                        # Sec. 9.3
```

---


## 15. MGA/SPORES: Near-Optimal Alternative Exploration


**Julia file**: `mga.jl`

MGA (Modeling to Generate Alternatives) [**[8]**](../reference/bibliography.md#ref8) and SPORES (Spatially-explicit Practically Optimal REsultS, Lombardi et al. 2020 [**[7]**](../reference/bibliography.md#ref7)) explore the space of near-optimal investment plans. In energy planning, multiple investment configurations can have similar total costs but very different compositions (e.g., more wind vs. more solar, distributed vs. centralized storage). These diverse alternatives inform policy decisions.

ESFEX implements **both methods** as distinct paths through the same machinery:

- **MGA** ($\S 15.2$–$\S 15.8$): the classical Hop-Skip-Jump (HSJ) loop. One diversity objective is applied $K$ times, each iteration penalising investment variables seen in the previous solutions.
- **SPORES** ($\S 15.9$–$\S 15.15$): a *menu* of distinct objectives. Each declared objective produces one alternative under the shared cost-slack constraint; the alternative count equals `len(objectives)`.

Both share the near-optimal constraint $Z \leq (1+\varepsilon) C^*$ and the per-alternative cost recovery; only the objective family differs.

### 15.1 Algorithm


```
1. Solve cost-optimal Master Problem → C*, x*₀
2. Add near-optimal constraint: total_cost ≤ (1 + ε) × C*
3. For k = 1, ..., K:
   a. Compute frequency scores from {x*₀, ..., x*_{k-1}}
   b. Set diversity-maximizing LP objective
   c. Solve → x*_k
```

**Key property**: The diversity objective is LP-compatible -- no binary variables are introduced.

### 15.2 Near-Optimal Constraint


After obtaining the cost-optimal solution with cost \(C^*\), a single constraint is added:

\[
Z \leq (1 + \varepsilon) \cdot C^* \tag{MGA-1}
\]

where \(\varepsilon\) is the `slack_fraction` (e.g., 0.05 for 5% cost increase).

### 15.3 Frequency Scoring


**Julia function**: `compute_frequency_scores(alternatives, input; investment_threshold=0.1)`

For each investment variable indexed by technology \(k\), node \(n\), and year \(y\), the frequency of investment across all previous alternatives is:

\[
\text{freq}_{k,n,y} = \frac{|\{a : I^{(a)}_{k,n,y} > \tau\}|}{K_{prev}} \tag{MGA-2}
\]

where \(\tau\) is the `investment_threshold` (MW) and \(K_{prev}\) is the number of alternatives found so far. The diversity score is:

\[
\sigma_{k,n,y} = 1 - 2 \cdot \text{freq}_{k,n,y} \tag{MGA-3}
\]

| Score range | Meaning | Effect |
|-------------|---------|--------|
| \(\sigma \approx +1\) | Rarely invested | Encourage |
| \(\sigma \approx 0\) | Invested ~50% of the time | Neutral |
| \(\sigma \approx -1\) | Always invested | Discourage |

Variables that have never appeared receive a default score of +1 (maximum encouragement).

### 15.4 Diversity Objective


**Julia function**: `set_spores_objective!(model, vars, input, frequency_scores)`

The diversity-maximizing objective replaces the cost objective:

\[
\max \sum_{y,k,n} \frac{\sigma_{k,n,y}}{\bar{I}_{k,n}} \cdot I^{tech}_{y,k,n} + \sum_{y,k,n} \frac{\sigma^{P}_{k,n,y}}{\bar{I}^{P}_{k,n}} \cdot I^{bat,P}_{y,k,n} + \sum_{y,k,n} \frac{\sigma^{E}_{k,n,y}}{\bar{I}^{E}_{k,n}} \cdot I^{bat,E}_{y,k,n} + \sum_{y,\{i,j\}} \frac{\sigma^{tr}_{i,j,y}}{\bar{I}^{tr}_i} \cdot I^{tr}_{y,\{i,j\}} \tag{MGA-4}
\]

Each investment variable (period-start years only, each transmission corridor once) is weighted by its diversity score and normalized by its maximum investment \(\bar{I}\). This normalization ensures technologies with different scales (e.g., 10 MW solar vs. 1000 MW wind) are treated comparably.

### 15.5 Cost Evaluation


After solving with the diversity objective, the actual system cost is recovered by evaluating the original cost expression:

\[
C_k = \text{value}(Z_{\text{original}}) \tag{MGA-5}
\]

Note: `objective_value(model)` returns the diversity objective, not the cost. The cost is obtained via `value(total_cost_expr)` where `total_cost_expr` was saved before modifying the objective.

### 15.6 Computational Cost


For \(K\) alternatives:

- **Solves**: \(K + 1\) (1 cost-optimal + \(K\) diversity)
- **Time**: Approximately \((K+1) \times T_{\text{master}}\)
- **Memory**: The JuMP model is reused; only the objective changes and one constraint is added
- **LP size**: Unchanged from the base Master Problem

### 15.7 Function Call Graph


```
run_mga_spores(input)
  |-- create_master_problem(input)        # Build base model
  |-- optimize!(model)                    # Step 0: cost-optimal
  |-- objective_function(model)           # Save cost expression
  |-- extract_master_solution(...)        # Alternative 0
  |-- @constraint(cost ≤ (1+ε)×C*)       # Near-optimal bound
  +-- loop k = 1..K:
        |-- compute_frequency_scores()    # MGA-2, MGA-3
        |-- set_spores_objective!()       # MGA-4 (replaces obj)
        |-- optimize!(model)              # Diversity solve
        |-- value(total_cost_expr)        # MGA-5 (actual cost)
        +-- extract_master_solution(...)  # Alternative k
```

### 15.8 Configuration


| Parameter | Default | Description |
|-----------|---------|-------------|
| `mga.enabled` | `false` | Enable MGA / SPORES |
| `mga.method` | `"mga"` | Generation method: `"mga"` (HSJ loop, $\S 15.2$–$\S 15.7$) or `"spores"` (per-objective sweep, $\S 15.9$–$\S 15.15$) |
| `mga.num_alternatives` | 10 | Number of diversity alternatives \(K\). **Used only when `method = "mga"`** — ignored under SPORES (the count equals `len(objectives)`) |
| `mga.slack_fraction` | 0.05 | Near-optimal slack \(\varepsilon\) — shared by both methods |
| `mga.investment_threshold` | 0.1 MW | Threshold \(\tau\) for frequency counting. Used by HSJ and by the SPORES `hsj_diversity` objective; ignored by the other SPORES objectives |
| `mga.objectives` | `[]` | SPORES objective menu (list of [`SporesObjective`](../api/config-schema.md#sporesobjective)). **Required when `method = "spores"`** — ignored under MGA |

---

## 15. (continued) SPORES: per-objective alternative generation

### 15.9 Overview


**Julia entry point**: `run_spores(input; objectives, slack_fraction, …)`

SPORES replaces the HSJ frequency loop with a *menu* of distinct LP objectives. Each entry in `objectives` produces one alternative under the same cost-slack constraint \(Z \leq (1+\varepsilon) C^*\). ESFEX ships five canonical objectives, the four "classical" SPORES objectives from Lombardi et al. (2020) plus the HSJ score retained as a special case:

| Symbol | Sense | Section |
|--------|-------|---------|
| `:hsj_diversity` | \(\max\) | § 15.4 (same as MGA — reusable inside a SPORES sweep) |
| `:min_total_build` | \(\min\) | § 15.10 |
| `:max_tech_equity` | \(\min\) (min-max) | § 15.11 |
| `:max_regional_equity` | \(\min\) (min-max) | § 15.12 |
| `:evolutionary_dist` | \(\max\) | § 15.13 |

All objectives are linear; auxiliary variables introduced by the min-max equity objectives and by the L1-linearised distance objective are tracked in `model[:_spores_objective_aux]` and deleted between sweep iterations (see § 15.14) so the JuMP model never accumulates dead variables across the sweep.

### 15.10 Minimum total build objective


**Julia function**: `set_min_build_objective!(model, vars, input)`

Selects the *smallest* near-optimal portfolio. Useful when the cost slack admits a deployment-light plan that meets the same demand and renewable-energy targets:

\[
\min \sum_{y,t,n} I^{tech}_{y,t,n} + \sum_{y,b,n} I^{bat,P}_{y,b,n} + \sum_{y,(i,j)} I^{tr}_{y,(i,j)} \tag{SPORES-1}
\]

where the investment-period gating of §3.1 still applies (the sums only run over the period-start years \(y_{\mathrm{idx}} \in \{1, 1+y_{pp}, 1+2 y_{pp}, \ldots\}\)). Battery *energy* investments are deliberately excluded from the MW sum because the units (MWh vs MW) do not combine cleanly; the energy variable is implicitly constrained through the per-tech duration limit.

### 15.11 Technology equity objective


**Julia function**: `set_tech_equity_objective!(model, vars, input)`

Minimises the largest per-technology share of the build (a min-max equity formulation, sometimes called Gini-min in the SPORES literature):

\[
\min \; M \tag{SPORES-2a}
\]
\[
\text{s.t.}\quad \sum_{y, n} \frac{I^{tech}_{y,t,n}}{\bar{I}_{t,n}} \;\leq\; M \qquad \forall\, t \in \mathcal{T} \tag{SPORES-2b}
\]

Investments are normalised by the per-tech-per-node investment cap \(\bar{I}_{t,n}\) so technologies with different scales (10 MW vs 1000 MW) compete on the same footing. The auxiliary scalar \(M \geq 0\) and the \(|\mathcal{T}|\) per-tech constraints are anonymous (no `base_name`) and stashed in the aux registry. Technologies whose investment cap is zero everywhere contribute an empty sum that reduces to the trivial constraint \(0 \leq M\).

### 15.12 Regional equity objective


**Julia function**: `set_regional_equity_objective!(model, vars, input)`

The spatially-explicit twin of § 15.11 — minimises the largest per-node share of the build. This is the canonical SPORES objective from which the name *spatially-explicit* derives:

\[
\min \; M \tag{SPORES-3a}
\]
\[
\text{s.t.}\quad \sum_{y, t} \frac{I^{tech}_{y,t,n}}{\bar{I}_{t,n}}
\;+\; \sum_{y, b} \frac{I^{bat,P}_{y,b,n}}{\bar{I}^{P}_{b,n}}
\;\leq\; M \qquad \forall\, n \in \mathcal{N} \tag{SPORES-3b}
\]

Battery power investments are included because they materially affect spatial siting; battery energy is omitted for the same unit-mixing reason as in (SPORES-1).

### 15.13 Evolutionary distance objective


**Julia function**: `set_evolutionary_distance_objective!(model, vars, input, reference_solution)`

Maximises the L1 distance (normalised) from a reference solution \(x_{\mathrm{ref}}\) — typically the cost-optimal plan \(x_0^*\). Used to surface the *maximally different* feasible plan when several visually similar near-optima exist:

\[
\max \sum_{y,t,n} \frac{|I^{tech}_{y,t,n} - I^{tech,\mathrm{ref}}_{y,t,n}|}{\bar{I}_{t,n}}
\;+\; \sum_{y,b,n} \frac{|I^{bat,P}_{y,b,n} - I^{bat,P,\mathrm{ref}}_{y,b,n}|}{\bar{I}^{P}_{b,n}} \tag{SPORES-4}
\]

The L1 norm is linearised via positive / negative deviation auxiliaries:

\[
I^{tech}_{y,t,n} - I^{tech,\mathrm{ref}}_{y,t,n} \;=\; d^{+}_{y,t,n} - d^{-}_{y,t,n}, \quad d^{+}_{y,t,n},\, d^{-}_{y,t,n} \geq 0 \tag{SPORES-4a}
\]

so the objective becomes \(\max \sum (d^+ + d^-)/\bar{I}\). The Euclidean L2 distance would force the model out of LP into QP and is therefore not used.

### 15.14 Dispatcher and aux-variable cleanup


**Julia function**: `apply_spores_objective!(model, vars, input, objective::Symbol; frequency_scores = nothing, reference_solution = nothing)`

Routes a SPORES objective symbol to the matching `set_*_objective!` function and validates required kwargs:

- `:hsj_diversity` requires `frequency_scores` (a `Dict{String, Float64}`).
- `:evolutionary_dist` requires `reference_solution` (a `MasterProblemResult`).
- The other three need neither.

Each call begins with `_clear_spores_aux!(model)` which deletes every variable / constraint installed by the previous objective. Aux references live in `model[:_spores_objective_aux]` as a `Vector{Any}`; `JuMP.delete` is wrapped in `try` / `catch` to stay idempotent across repeated cleanups. This is what allows the sweep loop (§ 15.15) to re-use a single model across all objectives without unbounded growth.

### 15.15 SPORES sweep loop


**Julia function**: `run_spores(input; objectives, slack_fraction, use_representative_days, investment_threshold)`

```
run_spores(input, objectives = [:min_total_build, :max_tech_equity, …])
  |-- create_master_problem(input)        # Build base model
  |-- optimize!(model)                    # Step 0: cost-optimal
  |-- objective_function(model)           # Save cost expression
  |-- extract_master_solution(...)        # Reference / alt 0
  |-- @constraint(cost ≤ (1+ε)×C*)       # Near-optimal envelope
  +-- for k = 1..len(objectives):
        |-- _clear_spores_aux!(model)     # Drop previous aux
        |-- apply_spores_objective!(...)  # Install objective k
        |-- optimize!(model)              # Solve under cost cap
        |-- value(total_cost_expr)        # MGA-5 (actual cost)
        +-- extract_master_solution(...)  # Alternative k
```

The result is an `MGAResult` whose `alternatives[1]` is the cost-optimal seed, `alternatives[2:end]` are the SPORES solutions, and `objective_labels[k]` carries the symbol that produced `alternatives[k+1]`.

### 15.16 When to use MGA vs SPORES


| Use case | Use MGA | Use SPORES |
|----------|---------|------------|
| "Show me K alternatives, surprise me" | ✅ | — |
| "Show me the smallest near-optimal plan" | — | ✅ (`min_total_build`) |
| "How spatially flexible is the optimum?" | — | ✅ (`max_regional_equity`) |
| "Quantify the technology-substitution envelope" | indirect | ✅ (`max_tech_equity`) |
| Need *named* alternatives for policy discussion | — | ✅ |
| Need a large set (K = 20+) for statistical robustness | ✅ | ❌ (limited by objective count) |
| Cost cap is the only constraint | ✅ | ✅ |

In practice, SPORES is preferable when each alternative needs to *answer a specific question*; MGA is preferable when the goal is to *map the breadth* of the near-optimal space with a larger sample.

---

### Solver Configuration

The Master Problem uses the solver selected in the `solver` section (HiGHS by default), with its thread count, time limit and gap. Since the formulation is a pure LP (all continuous variables, age-based retirement evaluated at construction time), the MIP gap setting has no effect; the solver uses simplex or interior point.

---


## 16. Per-Technology Investment Model


The master invests per **technology**, not per existing generator: candidate technologies and battery technologies are defined separately from the existing fleet, and each has its own investment variables per bus (§3.1).

### 16.1 Technology Configuration


Technologies are defined per system, keyed by name. Per-node fields are lists with one value per node:

```yaml
technologies:
  solar_pv:
    name: Solar PV
    type: Renewable
    fuel: Solar
    invest_cost: [800000.0]       # $/MW per node
    invest_max_power: [500.0]     # MW per node (cumulative cap)
    Availability: solar_profile.csv
    eff_at_rated: [1.0]
    degradation_rate: [0.005]
    lifetime: 25
    fixed_cost: [18000.0]         # $/MW per year (fixed O&M)
    maintenance_cost: [2.0]       # $/MWh

battery_technologies:
  li_ion:
    name: Li-Ion Storage
    invest_cost_power: [150000.0]   # $/MW
    invest_cost_energy: [200000.0]  # $/MWh
    invest_max_power: [200.0]       # MW
    invest_max_capacity: [800.0]    # MWh
    min_duration_hours: 1.0
    max_duration_hours: 4.0
    efficiency_charge: [0.95]
    efficiency_discharge: [0.95]
    degradation_rate: [0.0]
    lifetime: 15
```

See the [Configuration Reference](../reference/config-reference.md) for all fields.

### 16.2 Cumulative Capacity


The capacity of technology \(k\) at bus \(n\) in year \(y\) is the sum of its surviving, degraded investments (CUM-1b); battery technologies follow CUM-3. Investments retire when their age reaches the technology's `lifetime`.

### 16.3 Virtual Generators


Technology investments become **virtual generators** (and virtual batteries) for operational dispatch. After the master problem solves, the runner:

1. Computes cumulative investment per technology per year
2. Creates synthetic generator entries with `rated_power = cumulative_investment`
3. Passes these virtual generators to the operational dispatch adapter
4. Gives them the availability profile and cost parameters of the technology, including its `fuel_cost_curve` (per bus), so the dispatch prices built capacity with the same bidding curve the master planned with (see the note on cost curves above)

---

## References

The capacity expansion formulation draws on the general framework for generation expansion planning reviewed by Koltsaklis and Dagoumas [**[24]**](../reference/bibliography.md#ref24). The importance of embedding operational flexibility constraints within planning models is analyzed by Palmintier and Webster [**[25]**](../reference/bibliography.md#ref25) and Poncelet et al. [**[26]**](../reference/bibliography.md#ref26). The interaction between unit commitment constraints and generation expansion is studied by Schwele et al. [**[35]**](../reference/bibliography.md#ref35), which motivates ESFEX's two-stage decomposition approach. The role of operational detail in planning models with high variable RE shares is reviewed by Helistö et al. [**[36]**](../reference/bibliography.md#ref36). Representative day selection and time series aggregation methods follow Kotzur et al. [**[22]**](../reference/bibliography.md#ref22) and Nahmmacher et al. [**[37]**](../reference/bibliography.md#ref37); for a broader review see Hoffmann et al. [**[23]**](../reference/bibliography.md#ref23). The MGA/SPORES methodology for near-optimal space exploration follows Lombardi et al. [**[7]**](../reference/bibliography.md#ref7) and DeCarolis [**[8]**](../reference/bibliography.md#ref8); see also Neumann and Brown [**[38]**](../reference/bibliography.md#ref38). The optimization is formulated via JuMP [**[20]**](../reference/bibliography.md#ref20) and solved with HiGHS [**[21]**](../reference/bibliography.md#ref21) by default.

See the [full bibliography](../reference/bibliography.md) for complete citation details.
