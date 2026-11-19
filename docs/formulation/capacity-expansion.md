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
| \(\mathcal{D}_y\) | Days modelled in year \(y\): the same days of the year for every \(y\), the representative days (the peak-demand day plus days whose mean demand and renewable availability match the year's, [Representative Days](../user-guide/master-problem.md#representative-days)) and the critical days (§8.1b) | `master_day_plan()`, `master_rep_days()` |
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
| \(\rho_{\sigma,y}\) | \(\geq 0\) | -- | Renewable generation of system \(\sigma\) over its demand, the growth cap's link from one year to the next (RE-2b; not the RE share; an expression of the day variables except in the Benders master) | `re_energy_ratio[(σ,y)]` |
| \(s^{re}_{\sigma,y}\) | \(\mathbb{R}_+\) | MWh/yr | Renewable energy missing from system \(\sigma\)'s RE target | `slack_re_target[(0,σ,y)]` (scenario \(\omega\): `[(ω,σ,y)]`) |
| \(s^{grow}_{\sigma,y}\) | \(\mathbb{R}_+\) | MWh/yr | Renewable energy system \(\sigma\) uses above its growth cap (RE-3b) | `slack_re_cap_s{σ}_y{y}` (`model.ext[:re_cap_slack]`) |
| \(Z_{\sigma,y}\) | \(\mathbb{R}_+\) | MWh/yr | Curtailment of system \(\sigma\) above its annual limit (CURT-1), at 10⁴ \$/MWh | `curtailment_excess_s{σ}_y{y}` (`model.ext[:curtailment_excess]`) |
| \(s^{cap}_{y,n}\) | \(\mathbb{R}_+\) | MW | Capacity adequacy shortfall per node | `slack_capacity[(y,n)]` |
| \(s^{bud}_y\) | \(\mathbb{R}_+\) | \$ | Overnight investment over the annual budget | `slack_budget[y]` |

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
| \(c^{fuel}_{g,n}\) | \$/MWh | Fuel cost in year \(y\): `fuel_cost` × \((1 + g^{price}_f)^{y - y_0}\) when the unit's `fuel` \(f\) is in the `fuels` block (its `price_growth_rate`, from the run's first year \(y_0\), also for a myopic year's master; candidate technologies alike), else `fuel_cost`. With primary energy on, fuel-coupled units pay the fuel price through the fuel balance instead (§8.3.12) | `year_master_input(input, y).generators[g].fuel_cost[n]` |
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
| \(c^{fre}\) | \$/MWh | Price of a MWh missing from an RE target (`penalties.fre_penetration_loss`, the dispatch's; 100 \$/MWh when not positive) | `input.fre_penetration_loss_penalty` (`re_shortfall_penalty`) |
| \(L_u\) | years | Economic lifetime of build \(u\) (table in §5.1; 40 years where the input has none) | `life_time`, `DEFAULT_ASSET_LIFETIME` |
| \(\pi^{bud}\) | \$/\$ | Price of a \$ over the annual budget (10) | `BUDGET_SLACK_PENALTY` |
| \(c^{cap}_{y,n}\) | \$/MW | Price of a MW of adequacy shortfall: \(c^{VOLL}\) × the node's peak hours (§5.3) | `adequacy_slack_penalty` |
| \(\bar{\kappa}\) | [0,1] | Maximum curtailment ratio | `input.max_curtailment_ratio` (default 0.05) |
| \(c^{CU,exc}\) | \$/MWh | Price of a MWh/yr of curtailment above the annual limit (10⁴; CURT-1) | `CURTAILMENT_EXCESS_PENALTY` |
| \(D_r\) | -- | Representative days selected per year: `representative_days_per_year`, or fewer when the minimum separation or the data leave no more (a warning says so) | `length(master_rep_day_offsets(input))` |
| \(\omega_d\) | days | Days of the year modelled day \(d\) stands for: \((H_{year}/24)/D_r\) without critical days; 1 for a critical day, the rest shared by the representative days (§8.1b) | `rep_day_weights`, `master_day_plan` |

---


## 5. Objective Function


**Julia function**: `build_master_objective!(model, vars, input)`

The objective minimizes the NPV of total system costs over the planning horizon:

\[
\min \; Z = C^{inv} + \sum_{y=1}^{Y} \frac{C^{asset}_y + C^{op}_y}{(1+r)^{y-1}} + C^{slack} \tag{OBJ-1}
\]

### 5.1 Investment Cost (Annuities)


**Julia functions**: `build_investment_cost_expression`, `master_investment_terms`, `capital_recovery_factor`, `annuity_years`

Every MW (or MWh) built pays an annuity, the capital recovery factor times its overnight cost, in every year of the horizon in which it is in service:

\[
C^{inv} = \sum_{u} c_u \, I_u \, \mathrm{CRF}(r, L_u) \sum_{y = y_u}^{\min(Y,\; y_u + \lceil L_u \rceil - 1)} \frac{1}{(1+r)^{y-1}} \tag{OBJ-2}
\]

\[
\mathrm{CRF}(r, L) = \frac{r (1+r)^{L}}{(1+r)^{L} - 1}, \qquad \mathrm{CRF}(0, L) = \frac{1}{L} \tag{OBJ-3}
\]

where \(u\) runs over the investment variables, \(I_u\) is the amount built in year \(y_u\) (an investment-period start), \(c_u\) its overnight cost per unit and \(L_u\) its economic lifetime:

| Build | \(I_u\) | \(c_u\) | \(L_u\) |
|---|---|---|---|
| Technology | \(I^{tech}_{y,k,n}\) (MW) | `invest_cost` | technology `lifetime` |
| Battery technology | \(I^{bat,P}_{y,k,n}\) (MW), \(I^{bat,E}_{y,k,n}\) (MWh) | `invest_cost_power`, `invest_cost_energy` | battery technology `lifetime` |
| Transmission, once per corridor | \(I^{tr}_{y,\{i,j\}}\) (MW) | \(\max(c^{tr}_i, c^{tr}_j)\) | 40 years (`DEFAULT_ASSET_LIFETIME`; lines have no lifetime field) |
| Reservoir | \(I^{res}_{y,g,n}\) (MWh) | `reservoir_invest_cost` | the generator's `life_time` |
| Converter | \(I^{conv}_{y,c}\) (MW) | `invest_cost` | `life_time` |
| Fuel storage, route capacity, electrolyzer | §8.3.12 | §8.3.12 | storage and transport `lifetime` of the fuel infrastructure (defaults 30 and 20 years), electrolyzer `life_time` (40 years when 0) |

The years summed are those in which the build is in service: from its build year while its age is below \(L_u\), the same years in which its capacity reaches the representative days (CUM-1b). Each payment is discounted like the operating cost of its year. Nothing is paid after the horizon and nothing is credited back, as in GenX and PyPSA. A build in the last year pays one annuity, so it is judged on the years it serves. A build whose lifetime fits in the horizon pays annuities worth \((1+r)\) times its overnight cost discounted to the build year, because its first payment falls in the build year. With investment periods of several years, a period's build pays from the period's first year. A stochastic scenario scales \(c_u\) by its investment-cost multipliers and discounts at its own rate (STOCH-1). Earlier versions charged the whole overnight cost in the build year with no residual value. A unit built late in the horizon then carried its full cost for a few years of service and was rarely built. The annual budget (BUD-1) still limits the overnight cost committed in each year.

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
C^{op}_y = \Delta t \sum_{d \in \mathcal{D}_y} \omega_d \, C^{day}_{y,d} \tag{OBJ-4}
\]

where \(\Delta t\) is `temporal_resolution_hours` and \(\omega_d\) the days of the year day \(d\) stands for. Without critical days every representative day stands for \((H_{year}/24)/D_r\) days, with \(H_{year}\) the hours of the first year (`hours_per_year[1]`, always 8760: every modelled year has 8760 h, leap years included) and \(D_r\) the days actually selected (`rep_day_scaling`); dividing by `representative_days_per_year` when fewer could be selected underweighted every cost and energy of the year. A critical day stands for one day and the representative days share the rest (§8.1b). The cost for each representative day is (from `day_operational_cost`):

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

OBJ-6a is the converters' variable cost on the power they carry in either direction (plus the constant standby term of the dispatch), the flow cost of inter-system links (§8.3.9) and the start-ups of §8.3.10 (an event, not energy: the \(1/\Delta t\) cancels the step length of OBJ-4). The CO2 budget violation of §8.3.11 is added to \(C^{op}_y\) once per year at each system's penalty, and so are the RE-target shortfall \(c^{fre} s^{re}_{\sigma,y}\) of RE-2, the renewable energy above the growth cap \(c^{VOLL} s^{grow}_{\sigma,y}\) of RE-3b and the curtailment above the annual limit \(c^{CU,exc} Z_{\sigma,y}\) of CURT-1. The per-step slack \(\lambda^{fre}\) of OBJ-5 is in no master constraint and stays 0.

where \(\kappa_s\) is the criticality weight for sector \(s\). The sectoral shed \(L^{sec,day}\) is part of \(L^{day}\) (SEC-2), which OBJ-5 already charges at VOLL; OBJ-6 adds the criticality premium, so a sector's shed MWh costs \(c^{VOLL} \kappa_s\), and the unclassified remainder \(L^{day}_{n,t} - \sum_s L^{sec,day}_{s,n,t}\) gets the premium \(c^{VOLL} (m_{n,t} - 1)\), where \(m_{n,t}\) is the demand-weighted mean of the sector criticalities at the bus's parent node in that hour (1 where no sector has demand), as in the dispatch (OBJ-4a). The carbon cost is the one the dispatch charges, so a carbon price shifts the plan towards lower-emission units. Batteries pay their `maintenance_cost` on every MWh charged and discharged and their throughput cost on discharge, as in the dispatch; for battery technologies (\(k\)) these are the technology's `maintenance_cost` and `throughput_degradation_cost`. When `max_curtailment_ratio` < 1, the curtailment of candidate renewable technologies, \(\sum (\text{capacity} \cdot \alpha - P^{tech})\), is charged at \(c^{CU}\) as well. All of \(C^{day}\) is scaled to the year the same way (OBJ-4, or \(\Delta t \cdot w_p\) for a TSAM period of weight \(w_p\)), in the monolithic, TSAM, multi-system and stochastic masters and in the Benders subproblems ([§11b](#11b-benders-decomposition)).

!!! note "PWL cost curves in the master problem"
    When existing generators have a `fuel_cost_curve` configured, the representative day operational cost (OBJ-5) uses the same piecewise-linear (PWL) fuel cost decomposition as the operational dispatch formulation (see [Operational Dispatch -- PWL Fuel Cost](operational-dispatch.md#311-piecewise-linear-pwl-fuel-cost-decomposition)). The same applies to existing batteries with `discharge_cost_curve`, which then replaces the throughput cost, and to candidate technologies with a `fuel_cost_curve`: on every bus of its node, the technology's output \(P^{tech}_{k,n,t}\) is split into segments, segment \(j\) capped at \(f_j \cdot\) cumulative capacity \(\cdot\, \alpha_{k,n,t}\) (`tech_seg_cap_t*_b*_k*_ts*`) and charged its own marginal cost instead of the flat `fuel_cost`, as the dispatch prices the generator the technology becomes. The Benders subproblems do the same, and their cuts include the segment caps in the capacity gradient.

### 5.3 Slack Penalties


**Julia function**: `master_slack_cost(vars, input)`

\[
C^{slack} = \sum_{y=1}^{Y} \frac{\pi^{bud} \, s^{bud}_y + \sum_{n \in \mathcal{N}} c^{cap}_{y,n} \, s^{cap}_{y,n}}{(1+r)^{y-1}}, \qquad c^{cap}_{y,n} = c^{VOLL} \, |\mathcal{T}^{peak}_{y,n}| \, \Delta t \tag{OBJ-7}
\]

- **Budget** (`BUDGET_SLACK_PENALTY`, \(\pi^{bud} = 10\)): every \$ over the annual budget costs 10 \$, so the budget binds unless keeping it would cost more than ten times the overspend (for example load shed at VOLL).
- **Capacity adequacy** (`adequacy_slack_penalty`): a MW missing at the peak is priced as load not served in each hour of the node's peak period \(\mathcal{T}^{peak}_{y,n}\), the steps CAP-1 takes as the peak (the top 10 % of the year's demand, about 876 hours). This is above the yearly cost of any firm capacity, so the slack is used only when no build can close the gap.
- **RE target**: the shortfall \(s^{re}_{\sigma,y}\) is an operating cost of the year, at \(c^{fre}\) per MWh (RE-2).
- **RE growth cap**: existing renewables above the cap are curtailed at `curtailment_cost` (RE-3c); what cannot be curtailed, \(s^{grow}_{\sigma,y}\), is an operating cost of the year at VOLL per MWh (RE-3b, `re_cap_penalty`).
- **Curtailment limit**: curtailment above the annual limit, \(Z_{\sigma,y}\), is an operating cost of the year at 10⁴ \$/MWh (CURT-1).

No slack makes the master infeasible. In a stochastic master the budget and adequacy slacks are first-stage and are discounted at the base rate. Earlier versions priced the budget and adequacy slacks, and a redundant yearly RE-ratio slack, at a generic `slack_penalty` of 1 M\$ per unit: every \$ over the budget cost 1 \$, so the budget did not constrain the plan.

---


## 6. Cumulative Capacity Expressions


For each unit and bus, the capacity available in year \(y\) combines existing (degraded) capacity, life-extended capacity and invested capacity, each subject to age-based retirement. The capacities are built inline where they are used (`add_day_operational_constraints!`, `add_capacity_adequacy_constraints!`), and the built capacities reach the hourly rows through per-year state variables (§6.5). `build_cumulative_capacity_expressions()` evaluates the same expressions, with the investment sums, for the fixed O&M in the objective.

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

### 6.5 Capacity State Variables


The sums above do not enter the hourly constraints directly. For each year \(y\), each built capacity (candidate technology, battery-technology power and energy per bus, expandable corridor, reservoir, converter, fuel storage, route transport and electrolyzer) has a state variable defined by one equality, e.g. for a technology:

\[
C^{tech}_{y,k,n} = \sum_{\substack{y' \in \mathcal{Y}^{inv},\, y' \leq y \\ y - y' < \tau_{k,n}}} (1 - \delta_{k,n})^{y-y'} \, I^{tech}_{y',k,n}, \qquad C^{tech}_{y,k,n} \geq 0 \tag{CUM-5}
\]

(`cap_tech_y*_t*_b*`, defined by `def_cap_tech_y*_t*_b*`; likewise `cap_bt_pow_*`, `cap_bt_en_*`, `cap_trans_*`, `cap_res_*`, `cap_conv_*` and `cap_fuel_*`). Every representative-day or TSAM-period constraint limited by a built capacity uses \(C_{y,\cdot}\): output and offer-curve segments, reserve headroom, curtailment, battery power and energy, line flows, reservoir levels, converters, fuel tanks, routes and electrolyzers.

The LP and its optimum are the same as with the sums written out. Only the size changes. With the sums, each investment variable appeared in every hourly row of every later year. A 25-year master with 136 nodes had about 13,000 such dense columns, and its interior-point factorisation was too large to solve. Now an investment variable appears only in its CUM-5 definitions (at most one per year), the yearly rows (investment limits, budget, adequacy) and the objective, so its column length does not depend on the number of hours modelled. The objective's fixed O&M and the adequacy constraint keep the sums. The Benders master has no hourly rows and no state variables. Results are extracted from the investment variables as before.

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

For each investment-period start year, the overnight cost of that year's builds must not exceed the budget (with slack, priced at \(\pi^{bud}\) in OBJ-7), `budget_y{y}`:

\[
\sum_{k,n} c^{inv}_{k,n} I^{tech}_{y,k,n} + \sum_{k,n} \left( c^{inv,P}_{k,n} I^{bat,P}_{y,k,n} + c^{inv,E}_{k,n} I^{bat,E}_{y,k,n} \right) + \sum_{\{i,j\}} \max(c^{tr}_i, c^{tr}_j) I^{tr}_{y,\{i,j\}} + \sum_{g,n} c^{res}_{g,n} I^{res}_{y,g,n} \leq \bar{B} + s^{bud}_y \tag{BUD-1}
\]

The budget limits the capital committed in the build year, not the annuities of OBJ-2. Converters and primary-energy investment are not part of it.

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
\sum_{n' \in n} \Big[ \sum_g \alpha^{cc}_{g,n'} \kappa^{risk}_{g,n'} \bar{P}^{cum}_{y,g,n'} + \sum_k \alpha^{cc}_{k,n'} \kappa^{risk}_{k,n'} \bar{P}^{tech}_{y,k,n'} + \sum_b \alpha^{cc}_{b,n'} \kappa^{risk}_{b,n'} \beta_{b,n'} \bar{P}^{bat,cum}_{y,b,n'} + \sum_k \alpha^{cc}_{k,n'} \kappa^{risk}_{k,n'} F^{bt}_{y,k,n'} \Big] + \sum_{m} \left( f^{cap}_{y,m \to n} - f^{cap}_{y,n \to m} \right) + s^{cap}_{y,n} \geq D^{peak}_{y,n} \cdot M_{res} \qquad \forall\, y,\; \text{node } n \tag{CAP-1}
\]

\[
0 \leq f^{cap}_{y,m \to n} \leq \sum_{i \in m,\, j \in n} \left( C_{ij} + \sum_{y' \leq y} I^{tr}_{y',i,j} \right) \tag{CAP-3}
\]

\(f^{cap}_{y,m \to n}\) (`cap_import_y…`) is the peak-hour transfer from node \(m\) to node \(n\), bounded by the existing line capacity \(C_{ij}\) between their buses plus the cumulative transfer investment. A node can therefore rely on a neighbour's surplus instead of building locally; nodal peaks are treated as coincident, which is conservative, and line losses are ignored.

where the peak demand at node \(n\) in year \(y\) is:

\[
D^{peak}_{y,n} = \max_{t \in y} \left[ D_{t,n} \right] \tag{CAP-2}
\]

The constraint (`cap_adequacy_y{y}_node{n}`) is per node and sums over the node's buses. Each unit counts at its firm share \(\alpha^{cc}\) (`firm_capacity_credit`), which depends on its kind:

- **Variable renewables** (`is_variable_renewable`: a `Renewable` unit on sun, wind, an ocean resource, no fuel, or water without a reservoir): their capacity credit, the minimum of their availability profile, so solar counts as 0.
- **Every other generator, technology and storage unit** (thermal, biomass, geothermal, reservoir hydro, batteries): their mean availability \(a\) over the node's peak period \(T^{peak}_{y,n}\), the year's time steps in the top 10 % of the node's demand (at or above its 90th percentile), so the derates, outages and seasons in the profile count: a sugar mill idle in the peak season counts as 0, a unit derated to 80 % at the peak as 0.8, a unit out on a scheduled outage over the whole peak period as 0.

\[
\alpha^{cc}_{g,n'} = \frac{1}{|T^{peak}_{y,n}|} \sum_{t \in T^{peak}_{y,n}} a_{g,n',t} \qquad \text{(dispatchable units and storage)} \tag{CAP-4}
\]

**Storage counts up to its energy** (CAP-5). A battery counts at most the power its usable energy sustains over the critical period of \(H^{firm}\) hours, min(power, usable energy / \(H^{firm}\)). For an existing or life-extended battery, whose energy-to-power ratio is fixed,

\[
\beta_{b,n} = \min\Big(1,\; \frac{\eta^{u}_{b,n} \, E_{b,n}}{P_{b,n} \, H^{firm}}\Big), \qquad \eta^{u}_{b,n} = (\text{SOC}^{max}_{b,n} - \text{SOC}^{min}_{b,n}) \, \eta^{dis}_{b,n} \tag{CAP-5}
\]

(`usable_energy_ratio`; \(E\) MWh, \(P\) MW of discharge). For a battery technology the firm power is a variable (`bat_tech_firm_y{y}_bt{k}_b{n}`) bounded by the power and the usable energy built and in service that year (`bat_tech_power_built`, `bat_tech_energy_built`):

\[
0 \leq F^{bt}_{y,k,n} \leq \bar{P}^{bt}_{y,k,n}, \qquad H^{firm} F^{bt}_{y,k,n} \leq \eta^{u}_{k,n} \, \bar{E}^{bt}_{y,k,n} \tag{CAP-5b}
\]

(`bat_tech_firm_power_…`, `bat_tech_firm_energy_…`; only at buses where the technology may be built). Both are linear. \(H^{firm}\) is `master_problem.storage_firm_hours`, or by default derived from the data (`critical_day_plan`, `master_storage_firm_hours`): the longest stretch of a critical day (§8.1b) without solar, i.e. the steps where the solar profiles (those at most 1 % of their peak at the same step of the day on at least 90 % of days), weighted by the capacity behind them, give at most 1 % of their peak, read cyclically through the day; without a solar profile, the longest stretch whose renewable supply index stays below the relative demand; 4 h without renewable profiles; between one step and 24 h. Cuba: 13 h. Earlier versions counted every battery at its full power whatever its duration, so a 4-hour battery covered a 13-hour low-renewable night on paper and the dispatch shed load at night.

A unit without an availability profile counts fully. Earlier versions counted every non-`Renewable` unit and all storage fully whatever their availability, and `Renewable` dispatchable units (biomass, reservoir hydro) at the minimum of their profile (0 for a seasonal mill). \(\kappa^{risk}\) is the `risk_coefficient` (default 1; see [Risk & Resilience](risk-resilience.md)); life-extended capacity counts at its firm share and risk coefficient, like the unit it extends. \(D^{peak}_{y,n}\) is the maximum of the node's demand over the year's time steps.

### 7.5 RE Target Constraints (RE)


**Julia functions**: `calculate_target_ratios(input)`, `add_re_target_constraints!()`, `add_re_growth_cap!()`, `define_re_share!()`

RE targets are set per system \(\sigma\) (one global system when the configuration has a single system).

The **RE share** is one quantity everywhere in ESFEX (these targets and bounds, the Benders, stochastic and MGA masters, the myopic anchors, the dispatch's target and results, the HDF5 file and the dashboard): renewable generation over total generation, energy-weighted over the year,

\[
\text{RE share}_{\sigma,y} = \frac{E^{RE}_{\sigma,y}}{G_{\sigma,y}} \leq 1,
\]

with renewable meaning a generator or technology of type `Renewable` (`is_renewable_unit`) and \(G_{\sigma,y}\) the output of all of them. Battery discharge is neither (a battery may charge from fossil units). 100 % means no fossil generation; total generation includes the energy later lost in lines and storage, so a 100 % target asks for no fossil generation at all. Earlier versions measured the master's share on demand (renewable energy used over demand), which passed 100 % while fossil units still ran (Cuba: 102–105 %), and the dispatch's annual share was the unweighted mean of its windows' shares.

**RE-1: Target trajectory.** The first year's target is the system's initial penetration; later years interpolate linearly to the final target:

\[
\rho^{target}_{\sigma,1} = \rho^{init}_\sigma, \qquad \rho^{target}_{\sigma,y} = \rho^{init}_\sigma + \frac{y - 1}{Y - 1} \left( \rho^{target} - \rho^{init}_\sigma \right) \quad (y \geq 2) \tag{RE-1}
\]

with the step from the previous year's target clamped to \([\Delta\rho^{min}, \Delta\rho^{max}]\), and the target never above 1 (the minimum step can otherwise push the path past 100 %). In myopic planning each one-year master takes its year's \(\rho^{target}_{\sigma,y}\) from this trajectory over the whole horizon (`re_target_override`), and RE-3 its bounds (below).

**RE-2: RE target** (annual, soft, `re_target_s{σ}_y{y}`). The renewable generation of system \(\sigma\) over the year, from its representative days (or TSAM periods) scaled to the year as their cost is, plus the energy missing, covers the required share of its total generation scaled the same way: the RE-1 target or, with a minimum increment, the floor \(F_{\sigma,y}\) of RE-3, whichever is higher:

\[
E^{RE}_{\sigma,y} + s^{re}_{\sigma,y} \geq \max\!\big(\rho^{target}_{\sigma,y},\, F_{\sigma,y}\big) \, G_{\sigma,y} \tag{RE-2}
\]

\[
E^{RE}_{\sigma,y} = \sum_{d} w_d \sum_{t} \Big( \sum_{g \in \mathcal{G}_{RE}} \sum_{n \in \sigma} P^{day}_{g,n,t} + \sum_{k \in \mathcal{K}_{RE}} \sum_{n \in \sigma} P^{tech}_{k,n,t} \Big), \qquad G_{\sigma,y}: \text{ the same over all } g \in \mathcal{G},\ k \in \mathcal{K}, \qquad D_{\sigma,y} = \sum_d w_d \sum_t \sum_{n \in \sigma} D_{n,t}
\]

with \(w_d = \Delta t \, \omega_d\) for a representative or critical day (\(\omega_d = (H_{year}/24)/D_r\) without critical days) and \(\Delta t \, w_p\) for a TSAM period, and \(D_{n,t}\) a bus's share (`demand_fraction`) of its node's demand with the year's growth (`system_day_renewable`, `system_day_generation`, `system_day_demand`). Battery discharge does not count, as in the dispatch. RE-2 is linear: the target is data. The shortfall \(s^{re}_{\sigma,y}\) (MWh/year) is charged at \(c^{fre}\) in the year's operating cost and discounted like it; in a stochastic master each scenario has its own shortfall, priced with its `penalty_fre_penetration` multiplier and weighted by its probability. The target is a planning target: a day rich in sun or wind may make up for a poor one, as over a dispatch window. Each system of a merged run has its own target and its own slack. Earlier versions held the target on every representative day (its shortfall at the same \(c^{fre}\) per MWh), so a day's surplus could not make up for another's, plus a yearly ratio with a fraction-valued slack that never bound.

**RE-2b: Renewable generation over demand** (`re_energy_ratio[(σ,y)]`). \(\rho_{\sigma,y}\) carries a year's renewable generation (after curtailment) to the next year's growth cap (RE-3b) and, in Benders, from one year's subproblem to the next (BEN-RE):

\[
\rho_{\sigma,y} = E^{RE}_{\sigma,y} / D_{\sigma,y} \tag{RE-2b}
\]

In a master with representative days or TSAM periods \(\rho_{\sigma,y}\) is this expression of the day variables, not a variable with a defining row: the row \(\rho_{\sigma,y} D_{\sigma,y} = E^{RE}_{\sigma,y}\) put the year's demand (MWh, about \(2 \times 10^7\) for Cuba) in the matrix next to the day weights, the widest coefficient range of the master. In the Benders master, which has no days, it is a variable.

It is not the RE share (that is \(E^{RE}/G\), a ratio of two decisions) and has no upper bound. In a stochastic master it is the probability-weighted generation over the weighted demand. The reported share (`re_penetration_by_year`, §12) is the achieved \(E^{RE}/G\), probability-weighted in a stochastic master.

**RE-3: RE increment bounds.** The minimum increment sets a floor that the share must reach or pay the shortfall of RE-2 for, a straight line from the initial share (clamped to \([0, 1]\)) that stops at 100 %:

\[
F_{\sigma,y} = \min\!\big(\rho^{init}_\sigma + y\,\Delta\rho^{min},\, 1\big) \tag{RE-3a}
\]

The maximum increment caps the growth of the **actual** share over the previous year's actual share (`re_max_increment_s{σ}_y{y}`), year 1 over the initial share \(\rho^{0}_\sigma\) (clamped to \([0, 1]\)):

\[
E^{RE}_{\sigma,1} \leq \big(\rho^{0}_\sigma + \Delta\rho^{max}\big)\, G_{\sigma,1} + s^{grow}_{\sigma,1}, \qquad
E^{RE}_{\sigma,y} \leq \frac{D_{\sigma,y}}{D_{\sigma,y-1}}\, E^{RE}_{\sigma,y-1} + \Delta\rho^{max}\, G_{\sigma,y} + s^{grow}_{\sigma,y} \quad (y \geq 2) \tag{RE-3b}
\]

(`re_cap_prev_allowance`). Year 1 (and every myopic year, whose anchor is the share last year achieved) is exact: \(E^{RE}/G \leq \rho^0 + \Delta\rho^{max}\). In later years of a multi-year master the previous share \(E^{RE}_{y-1}/G_{y-1}\) times this year's \(G_y\) would be a product of decisions; RE-3b takes generation to grow like demand, \(G_y/G_{y-1} = D_y/D_{y-1}\), which keeps it linear and is exact when the ratio of generation to demand (losses, load shed) is the same in both years. Both bounds are linear. A system with a 5 % share and \(\Delta\rho^{max} = 0.10\) reaches at most 15 % in year 1 and 25 % in year 2, however cheap new renewables are; a year that grows less (a dark year, or a held-back build) lowers the next year's cap with it. Renewable energy above the cap is **curtailed**: it does not count toward the share and pays `curtailment_cost` per MWh, like any curtailment. Curtailing the existing variable renewables down to the cap is exempt from the curtailment limit `max_curtailment_ratio` (RE-3c), so a year whose existing units alone would pass the cap curtails the excess instead of paying for it at the value of lost load. Building renewables above the cap still does not pay, since their energy would be lost: the exemption covers only the existing units. The slack \(s^{grow}_{\sigma,y}\) (MWh/year, `slack_re_cap_s{σ}_y{y}`) is a last resort, priced at `re_cap_penalty` (the value of lost load \(c^{VOLL}\), at least \(c^{fre}\)) in the year's operating cost, for renewable output that cannot be curtailed (a reservoir's minimum release, say); `diagnose_infeasibility` reports it as `re_max_increment_s{σ}_y{y}`.

**RE-3c: curtailment the cap forces** (`re_cap_exemption`, `re_cap_exempt_energy`). With \(A_{\sigma,y}\) the energy the existing variable renewables of system \(\sigma\) can produce over the year's representative days, scaled to the year (power in service within their lifetime × availability; life-extended units and candidates do not count), \(D_{\sigma,y}\) its demand scaled the same way and \(\hat{k}_{\sigma,y}\) the cap's share (the previous year's share plus \(\Delta\rho^{max}\), at most 1), the system may curtail beyond the limit the share

\[
\theta_{\sigma,y} = \max\Big(0,\; 1 - \frac{(1 + \bar{\kappa})\, \hat{k}_{\sigma,y} D_{\sigma,y}}{A_{\sigma,y}}\Big) \tag{RE-3c}
\]

of the year's available existing energy (\(D\) stands for the generation the share is measured on, which it is but for losses and load shed): \(\theta_{\sigma,y} A_{\sigma,y}\) is added to the right side of the year's curtailment limit CURT-1. Then the existing units can be held at the cap with the year's other curtailment within \(\bar{\kappa}\). \(\theta\) is a constant computed from the data: \(\hat{k}\) uses the initial share \(\rho^0_\sigma\) in year 1 (a myopic year: the share the year before achieved, exact) and later the higher of the year before's required share (RE-2) and the least share its existing renewables left it, \(\min(A/((1+\bar{\kappa})D), \hat{k})\). \(\theta = 0\) in every year whose existing renewables stay under that cap, and without a cap or curtailment limit. Earlier versions priced this excess at the value of lost load: the Cuba myopic run paid 8.3–10.9 G\$ in three years for some 1.2 TWh above the cap. Each bound is added only when it is active (\(\Delta\rho^{min} > 0\), \(\Delta\rho^{max} < 1\)). A minimum increment above the maximum is not infeasible either: the floor's shortfall is paid. In a stochastic master each scenario caps its own share over its own previous year, as its target is its own.

Earlier versions bounded the increments of a *credited* share \(\rho_{\sigma,y} D_{\sigma,y} \leq E^{RE}_{\sigma,y} + s^{re}_{\sigma,y}\) that the master could set as low as it liked, so \(\Delta\rho^{max}\) never limited the renewable energy: a myopic master of Cuba went from a 5 % share to 89 % in its first year.

In myopic planning, year \(k\) (0-based) anchors the floor at \(\min(\rho^{init}_\sigma + k\,\Delta\rho^{min},\, 1)\), where the perfect-foresight chain stands the year before, so its one-year master gets the floor \(F_{\sigma,k+1}\) perfect foresight places on that year; the share reached in earlier years does not move it. The cap RE-3b of year \(k\) is measured from the share the year before achieved, clamped to \([0, 1]\) (`re_share_anchor`; year 0: the initial share). In Benders the same bounds are in each year's subproblem, the cap measured from the master's share of the year before ([§11b](#11b-benders-decomposition)).

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


**Julia functions**: `representative_day_offsets(demand, num_years, num_days, min_separation, timesteps_per_day, timesteps_per_year; profiles)`, `select_matching_days(...)`, `representative_day_ratios(...)`, `master_rep_days(input, y_idx)`

The days are chosen **once for the whole horizon** and every year models the same calendar days, each with its own demand and its own block of multi-year profiles. The selection runs on the horizon-mean demand and the horizon-mean renewable availability (the mean over the planning years of each step of the year). It keeps the peak day and picks the other days so that the year they stand for has the real year's demand and renewable energy:

1. Daily means of each feature \(f\) on each day \(d\) of the mean year, \(x_{d,f}\): the system demand \(\sum_n \bar{D}_{d,t,n}\), and every distinct renewable availability profile (existing renewable generators and renewable candidate technologies, each averaged over the nodes where its units have rated power or may be built). \(\bar{x}_f = \frac{1}{365}\sum_d x_{d,f}\) is the annual mean.
2. Select the global peak day first: \(\arg\max_d \max_t \sum_n \bar{D}_{d,t,n}\).
3. Divide the year into \(D_r\) segments for seasonal diversity; every segment but the peak day's gets one day, \(\geq\) `min_day_separation` days from every other selected day (a segment with no valid day takes one from anywhere in the year).
4. Choose those days to minimise

\[
\sum_f \Big( \frac{1}{|\mathcal{D}|} \sum_{d \in \mathcal{D}} \frac{x_{d,f}}{\bar{x}_f} - 1 \Big)^2 \tag{RD-1}
\]

greedily one segment at a time, then by moving one day, or two days together, within their segments while RD-1 decreases. Ties go to the higher-peak day.

Every day has the same weight \(\Delta t \cdot H_{year} / (24 D_r)\) (OBJ-4), so RD-1 at 0 means that the demand energy, and the energy per MW of each renewable profile, that the master sees equal the year's. The runner logs, per feature, the mean on the days over the annual mean ("Representative days mean / annual mean: demand 1.002, …", `representative_day_ratios`) and warns when one is off by more than 10 %. With few days a strongly seasonal profile cannot be matched exactly: one that is zero for two thirds of the year is either on 1 of 5 days (0.6 ×) or on 2 (1.2 ×).

The runner chooses the days (`master_rep_day_offsets` in the bridge) and passes them to every master of the run as `rep_day_offsets` (steps within a year): the perfect-foresight, stochastic, MGA and Benders masters and each annual master of a myopic run. The log lists their dates ("Representative days, the same for every planning year: …"). A master built without them (the Julia API) chooses them the same way from its own `base_demand` and the availability of its renewable generators and technologies (`master_re_profiles`). Earlier versions chose each year's own peak days, so the weather sample changed from year to year: in the Cuba myopic run the share the existing renewables gave swung 49 → 59 → 54 → 67 % with no capacity change, and years pushed over the growth cap (RE-3b) paid for it at the value of lost load. They also took the highest-peak day of each segment from the demand alone: where hot days are sunny and windy, the master saw a year with more demand, sun and wind than the real one (Cuba, 5 days: demand × 1.06, PV × 1.14, wind × 1.45; now × 1.002, × 1.000, × 1.007). It then overrated renewable energy (95 % RE in the master against 65 % in the hourly dispatch of the same plan) and retired thermal plants the year still needed.

### 8.1b Critical Days

**Julia functions**: `critical_day_plan(demand, num_years, num_critical, min_separation, timesteps_per_day, timesteps_per_year; profiles, capacities, exclude)`, `re_supply_index(...)`, `representative_day_weights(...)`, `master_day_plan(input)`

Days chosen to match the year's mean energy (RD-1) miss the days on which renewables cover the least of the demand, and with them the energy shortage of a dark, windless day: in the Cuba myopic run the dispatch shed 0.2 to 1.1 % of the yearly demand on such days (at night and early morning, mostly October to December) while every master planned none. Besides the representative days (or TSAM periods), every master (monolithic, TSAM, stochastic, MGA, each annual master of a myopic run) models `critical_days` critical days, chosen once from the same horizon-mean data, so the same days for every year:

\[
\nu_d = \frac{1}{H}\sum_{t \in d} \Big( \frac{\bar{D}_t}{\langle \bar{D} \rangle} - \frac{\sum_p c_p \, \bar{a}_{p,t}}{\sum_p c_p \langle \bar{a}_p \rangle} \Big) \tag{CD-1}
\]

with \(\bar{D}_t\) the horizon-mean system demand, \(\bar{a}_{p,t}\) the horizon-mean renewable availability profiles of §8.1 and \(c_p\) the capacity behind each (MW: rated power of the existing renewable units plus `invest_max_power` of the renewable candidate technologies that use it; equal relative weights when there is none), \(\langle \cdot \rangle\) the annual mean. The renewable supply is thus weighted by the energy each profile can give and scaled to the annual demand; \(\nu_d\) is the share of the day's demand that renewables leave uncovered. The critical days are the days with the largest \(\nu_d\), skipping the representative days and at least `min_day_separation` days from each other (relaxed to distinct days when the year leaves too few), the most critical first.

**Weights** (`representative_day_weights`). Each critical day stands for one day (\(\omega_c = 1\)). The representative days start from their prior weights \(p^0\) (equal, or the TSAM cluster sizes) summing to the year's days \(Y\), scaled to \(p = p^0 (Y - N_c)/Y\), then corrected by \(\delta = p \circ z\) with \(\sum \delta = 0\), \(z\) minimising

\[
\Big\| \frac{1}{Y}\Big( X_r (p + \delta) + X_c \mathbf{1} - X_r p^0 \Big) \Big\|^2 + 10^{-4} \|z\|^2 \tag{CD-2}
\]

where \(X_r, X_c\) hold each feature's day mean over its annual mean (system demand, each renewable profile) on the representative and critical days. The year the modelled days stand for then keeps the demand and renewable energy of the representative days alone: adding the critical days adds the shortage case without making the year darker. A weight stays at least a fifth of its prior, and the correction is dropped if it does not lower the mismatch. The runner logs the critical days' dates and the weighted mean of each profile on all modelled days over its annual mean.

On a critical day the master runs the same operation as on any other modelled day (storage cyclic over the day, reserves, curtailment, RE accounting, all scaled by \(\Delta t \, \omega_d\)). Benders models the representative days only.

### 8.2 Operational Variables


**Julia function**: `create_day_ps_vars!(model, input, year_idx, day_idx, hours)`

For each year \(y\) and representative day \(d\), a full set of dispatch variables is created as described in Section 3.3. These variables are embedded within the same JuMP model as the investment variables, allowing the solver to jointly optimize investment and dispatch.

### 8.3 Operational Constraints


**Julia function**: `add_day_operational_constraints!(model, ps_vars, vars, input, year_idx, day_idx, demand, start_hour)`

The day reuses the operational-dispatch constraint builders of `power_system.jl` and `transmission_dc.jl` in economic-dispatch mode, with the capacities of §6 passed in as expressions of the investment variables: generators, batteries, reservoirs, reserves (RES-1 to RES-3, held per system as in the dispatch: one system, or one per original system of a merged run, by existing and built units, §8.3.0), inertia, power balance and curtailment. With primary energy on, the day also has the fuel block of §8.3.12 (fuel balance and electrolyzers). Rooftop solar, EVs, unit commitment and N-1 are not modelled in the master.

#### 8.3.0 Reserves of the Capacity Built

The units the master builds are existing units in the dispatch, where they hold reserve; the master counts them the same way, linear in the investment variables. In the headroom of RES-3 of each system (the free capacity of its reservable units) the master adds, at every bus of the system and step \(t\) of the day:

\[
H^{build}_{n,t} = \sum_{k \in \mathcal{K}^{res}} \left( \alpha_{k,t',n} \, \bar{P}^{tech}_{y,k,n} - P^{tech}_{k,n,t} \right) + \sum_{k \in \mathcal{B}^{res}_{tech}} \left( a^{bt}_{k,t',n} \, \bar{P}^{bt}_{y,k,n} - P^{bt,dis}_{k,n,t} + P^{bt,ch}_{k,n,t} \right) \tag{RES-M1}
\]

with \(\mathcal{K}^{res}\) the `reservable` technologies that are not variable renewables (GEN-1b in [Operational Dispatch](operational-dispatch.md#gen-1-generator-capacity-limits); \(\alpha\) their availability profile, as the dispatch reads it for the generators they become) and \(\mathcal{B}^{res}_{tech}\) the `reservable` battery technologies (\(a^{bt}\) their availability). The dynamic capability (RES-2) gains `dynamic_reserve_contribution` × \(\bar{P}^{tech}_{y,k,n}\) and × \(a^{bt}_{k,t',n} \, \bar{P}^{bt}_{y,k,n}\). Existing generators hold \(\bar{P}^{cum}_{y,g,n} \, \alpha_{g,t',n} - P^{day}_{g,n,t}\), their capacity in service times availability, as in the dispatch (earlier versions left out the availability). Variable renewable technologies (sun, wind, ocean, run-of-river water) hold no reserve, as variable renewables in the dispatch; a biomass or geothermal technology does. Before this, a plan whose firm capacity was all built paid the reserve shortfall in every representative day although its units would hold the reserve in the dispatch. The Benders subproblems add the same terms; the duals of the headroom and dynamic-capability constraints, each times its coefficient, enter the capacity gradients of the cut.

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
P^{tech}_{k,n,t} \leq \bar{P}^{tech}_{y,k,n} \cdot \alpha_{k,t',n} \qquad \forall\, k \tag{GEN-2}
\]

where \(t' = \text{mod1}(\text{start\_hour} + t - 1,\; T_{avail})\) maps the representative day step to the availability profile (`tech_cap_t*_b*_ts*_y*_d*` for technologies). Every technology has its availability \(\alpha_{k}\) (1 without a profile): the capacity factor of a renewable, the derates and scheduled outages of a dispatchable one, as the generator it becomes in the dispatch. Earlier versions capped a non-renewable technology at its full built capacity, so the master counted on energy the dispatch then did not have. An existing generator's \(\alpha_{g,t',n}\) is its profile times its **scheduled outages** (`outage_schedule`) at that step, as the dispatch folds them into availability: each window's availability over its hours of the horizon (hour 0 = 1 January of the first planning year; a recurring window every year; a window with a node only at that node's buses), a step partly inside a window derated in proportion (`gen_outage_factors`, `master_outage_factor`). Existing batteries take their whole-unit outages on their charge and discharge power the same way (`bat_outage_factors`). The adequacy credit (CAP) and the existing renewables' energy of RE-3c count them too. This holds for every master: perfect foresight, stochastic, MGA, Benders (subproblems and operating bound) and each annual master of a myopic run. Earlier versions applied the outages in the dispatch only: in the Cuba 2025 case `pv_2025` (729 MW) is out until each park is commissioned, 0.58 TWh in the year instead of 1.18 TWh, and the master counted the 1.18 TWh (RE 15.0 % in the master against 12.1 % in the dispatch). Ramp limits (GEN-3) apply to existing generators; minimum generation, commitment and minimum up/down times do not.

#### 8.3.3 Battery Constraints Linked to Investment

For existing batteries (via `add_battery_constraints!` with capacity overrides):

\[
P^{ch,day}_{b,n,t},\; P^{dis,day}_{b,n,t} \leq \bar{P}^{bat,cum}_{y,b,n} \, a^{bat}_{b,n,t'}, \qquad E^{day}_{b,n,t} \leq \bar{E}^{cum}_{y,b,n} \tag{BAT-1}
\]

with \(a^{bat}\) the battery's availability profile at the day's step \(t'\) (1 without one). Charge and discharge follow the dispatch's ramp limits (BAT-9) within the day, the battery's power \(\bar{P}^{bat,cum}_{y,b,n}\) standing for both ratings, and only `reservable` batteries hold reserve, up to \(\bar{P}^{bat,cum}_{y,b,n} \, a^{bat}_{b,n,t'}\) (§8.3.4).

\[
E^{day}_{b,n,t+1} = E^{day}_{b,n,t} + \left( \eta^{ch}_{b,n} P^{ch,day}_{b,n,t} - \frac{P^{dis,day}_{b,n,t}}{\eta^{dis}_{b,n}} \right) \Delta t \tag{BAT-2}
\]

\[
E^{day}_{b,n,1} = E^{day}_{b,n,H+1} = \max(E^0_{b,n}, \underline{E}_{b,n}) \cdot \bar{E}^{cum}_{y,b,n} \tag{BAT-3}
\]

The day starts and ends at the initial SOC (`bat_soc_initial_*`, `bat_soc_cyclic_*`); with TSAM inter-period linking the end is the next boundary value instead (`bat_soc_link_*`). There is no self-discharge, spillage, SOC minimum or charge/discharge exclusivity in the master.

Battery technologies have the same structure (`add_battery_tech_day!`: `bt_dis_cap_*`, `bt_ch_cap_*`, `bt_soc_cap_*`, `bt_soc_init_*`, `bt_soc_dyn_*`, `bt_soc_cyc_*`), with power bounded by \(\bar{P}^{bt}\) × the technology's availability, the technology's ramp limits on charge and discharge (BAT-9 with \(\bar{P}^{bt}\)), SOC by \(\bar{E}^{bt}\), and cyclic SOC at `soc_initial` × \(\bar{E}^{bt}\); their SOC dynamics are multiplied by \(\Delta t\) as in BAT-2. A `reservable` battery technology holds reserve as the battery it becomes in the dispatch (§8.3.4).

#### 8.3.4 Transmission Constraints Linked to Investment

\[
-\bar{F}^{cum}_{y,\ell} \leq f^{day}_{\ell,t} \leq \bar{F}^{cum}_{y,\ell} \tag{TRX-1}
\]

The flows follow the DC power flow of the dispatch (flow–angle coupling DC-2). A candidate line of 0 MW is not fixed to zero flow when it can be expanded.

!!! note "Transmission losses in the Master Problem"
    The master uses `loss_model` like the dispatch, with `pwl_loss_segments_master` segments for `pwl`. Each node link carries the equivalent reactance and resistance of the bus-level lines between its two nodes, so the master's flow split and losses follow the grid. As in the dispatch, a line never loses more than the PWL loss of its net flow, so surplus renewable energy is curtailed (and counts toward `max_curtailment_ratio`) rather than lost on the lines. See [DC Power Flow -- Transmission Losses](dc-power-flow.md#transmission-losses) and [Exact losses](dc-power-flow.md#exact-losses).

#### 8.3.5 Renewable Energy of the Day

Each representative day adds its renewable and total generation and its demand per system, scaled to the year, to \(E^{RE}_{\sigma,y}\), \(G_{\sigma,y}\) and \(D_{\sigma,y}\) of the annual target RE-2 (`system_day_renewable`, `system_day_generation`, `system_day_demand`). There is no per-day RE constraint.

#### 8.3.6 Annual Curtailment Limit

When `max_curtailment_ratio < 1.0`, the curtailment of each system's existing variable renewables and candidate variable renewable technologies (\(\mathcal{K}_{VRE}\)) over the year is limited to a fraction \(\bar{\kappa}\) of its renewable generation over the year (`add_curtailment_limit!`, `max_curtailment_s{σ}_y{y}`):

\[
\sum_{d} w_d \Big[ \sum_{n \in \sigma,t} CU^{day}_{n,t} + \sum_{k \in \mathcal{K}_{VRE},n \in \sigma,t} \left( \bar{P}^{tech}_{y,k,n} \alpha_{k,t',n} - P^{tech}_{k,n,t} \right) \Big] \leq \bar{\kappa}\, E^{RE}_{\sigma,y} + \theta_{\sigma,y} A_{\sigma,y} + Z_{\sigma,y} \tag{CURT-1}
\]

with \(w_d\) the weight of each representative day or TSAM period in the year and \(E^{RE}_{\sigma,y}\) the year's renewable generation, as in RE-2 (`system_day_curtailment`, `system_day_renewable`; the constraint is divided by the largest \(w_d\), so for representative days it is the plain sum over the days). Single days may curtail more than \(\bar{\kappa}\) as long as the year stays within it: a sunny day's surplus is set against the days that use all their renewable output. Each system of a merged run has its own limit, each scenario of a stochastic master its own, and a myopic year's master its year's. \(Z_{\sigma,y} \geq 0\) (`curtailment_excess_s{σ}_y{y}`, MWh/year) is the curtailment above the limit, an operating cost of the year at \(c^{CU,exc}\) = 10⁴ \$/MWh (`CURTAILMENT_EXCESS_PENALTY`): far above the RE shortfall price and what avoiding a MWh of curtailment usually costs, so the master pays it only where nothing else can hold the limit, for instance when existing renewables must curtail more than the limit allows once the loss repair stops the lines from discarding energy (§8.3.4). The master then stays feasible and keeps its loss repair, and a WARNING names each system and year with its excess (GWh/yr) and renewable generation (above the limit's tolerance, 10⁻³ MWh/yr + 10⁻⁶ of its renewable generation). When the limit can be met, \(Z_{\sigma,y} = 0\) and the plan is the one of a hard limit. The Benders decomposition keeps the hard limit (§11b). Earlier versions held CURT-1 as a hard constraint: such a master was infeasible, and its loss repair kept the PWL solution, whose lines lose more than the PWL loss of their flows. The limit pushes investment towards storage rather than relying on curtailment to manage surplus RE. The curtailment of existing renewables that the RE growth cap forces is exempt: \(\theta_{\sigma,y} A_{\sigma,y}\) (RE-3c) is added to the right side. The operational dispatch has no such cap; it prices curtailment at `curtailment_cost`. With the cap on, the candidates' curtailment \(\bar{P}^{tech}_{y,k,n} \alpha_{k,t',n} - P^{tech}_{k,n,t}\) is also charged at \(c^{CU}\) in the day's cost, like \(CU^{day}\) in OBJ-5. Earlier versions held the limit on every representative day, over all systems together: a year far below its limit was infeasible when one of its days curtailed more (Cuba, Benders: a year curtailing 226.7 GWh against a limit of 580.8 GWh, because one of its five days curtailed more than 5 %).

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

When the dispatch commits units (`master_problem.use_uc_in_dispatch`), each generator the dispatch commits (every one but a variable renewable: thermal, biomass, geothermal, reservoir hydro) with a start-up cost or a minimum power gets an online capacity \(U_{g,n,t}\) (MW), a continuous relaxation of the dispatch's on/off status:

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
| Fuel storage | `investment_cost` of the `fuel_infrastructure` storage facility × units, as annuities over its `lifetime` | investment cost (§5.1) |
| Route capacity | `transport_investment_cost` × km × units/day, as annuities over its `lifetime` | investment cost (§5.1) |
| Electrolyzer power | `invest_cost` × MW, as annuities over its `life_time` | investment cost (§5.1) |
| Electrolyzer fixed cost | `fixed_cost` (\$/MW·yr) × (rated + built and not yet retired) MW, once per year | existing-asset cost (§5.1b) |
| Fuel bought, route transport, fuel shortfall and excess, unmet non-electric demand | as in the dispatch | day cost |
| Electrolyzer variable and water cost | \(\Delta t\,(c^{var}_b + c^{water}\,h_b)\,P^{elz}_{b,t}\), with \(h_b\) the kg of hydrogen per MWh | day cost |

The day's fuel cost is already in \$ (fuel amounts per step), so it is scaled to the year by \((H_{year}/24)/|\mathcal{D}_y|\), or by the TSAM period weight, without the \(\Delta t\) of SCALE-1.

**Hand-off to the dispatch.** The master is the only model that builds fuel infrastructure and electrolyzers. Each dispatch year receives the storage and route capacity in service that year as existing capacity (`cumulative_capacities` of the fuel model), and the built electrolyzer power is added to each electrolyzer's `rated_power`. A myopic master starts each year from what earlier years built and is still in service (storage and routes never retire; an electrolyzer build retires after its `life_time`, as in perfect foresight), with less room to build by everything built so far. A run without a master (`economic_dispatch` or `unit_commitment` mode) builds no fuel infrastructure.

### 8.4 Cost Scaling


Each representative day's operational cost is scaled to approximate annual costs:

\[
C^{op}_y = \Delta t \sum_{d \in \mathcal{D}_y} \omega_d \, C^{day}_{y,d} \tag{SCALE-1}
\]

with \(\omega_d = (H_{year}/24)/D_r\) for every day without critical days (§8.1b).

**Julia function**: `add_representative_days_validation!(model, vars, input, targets)` orchestrates the creation of variables, constraints, and cost expressions for all representative days across all years.

### 8.5 Time-Series Aggregation Method (TSAM)


When `use_tsam = true`, the peak-demand-based day selection (Section 8.1) is replaced by data-driven clustering with inter-period SOC linking. As the representative days, the periods are chosen once, by clustering the horizon-mean demand and renewable availability, and every year (and every annual master of a myopic run) uses the same periods and weights with its own demand and profiles. The critical days (§8.1b) are added as periods of weight 1, in their chronological place; the cluster weights are scaled down and corrected by CD-2.

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

#### 8.5.2b Renewable Energy Matching

**Python functions**: `rescale_profile_periods()`, `rescale_periods_to_year()` in `models/tsam.py`; `MasterProblemAdapter._tsam_energy_matched()`

The medoids minimise the clustering distance, not the energy error: weighted by the cluster sizes, the periods can hold more or less sun and wind than the year (Cuba, 12 periods: PV × 1.08, wind × 0.87, demand × 1.003). Once the periods and their weights are fixed (the critical days included, with the weights of CD-2), each renewable availability profile the master reads (renewable generators and renewable candidate technologies; the profile of each node, for each modelled year: the year's block of a multi-year profile, once for a one-year profile) is rescaled on the periods so that its weighted energy is the year's:

\[
\sum_{p \notin \mathcal{C}} w_p \sum_{t \in p} \min\!\big(s \, \alpha_t,\ \bar{\alpha}\big) + \sum_{c \in \mathcal{C}} w_c \sum_{t \in c} \alpha_t = \frac{\sum_p w_p T_p}{T_{year}} \sum_{t \in year} \alpha_t \tag{TSAM-R}
\]

with \(\alpha_t\) the original availability, \(\mathcal{C}\) the critical days, \(T_p\) the steps of a period and \(\bar{\alpha}\) the cap: full availability, 1 (or the profile's own maximum where it exceeds 1). \(s\) is found as the `tsam` library does: scale the uncapped steps by one factor, cap those that exceed \(\bar{\alpha}\), spread the energy they cannot take over the remaining uncapped steps, repeat until no further step reaches the cap. The shape of each period is kept (a step at 0 stays 0); only the rows of the periods change, so the dispatch and the rest of the year are untouched. The critical days are not rescaled: they exist to be extreme, and raising their renewable supply to the mean would erase the shortage they represent; the other periods carry their share of the correction, so TSAM-R holds over all periods. When every step of the periods reaches the cap and the year's energy is still not reached, the residual stays and is logged.

The runner logs the weighted mean of each profile on the periods over its annual mean before the rescaling ("TSAM periods (before rescaling the renewable profiles) mean / annual mean: …") and after it ("TSAM periods with the renewable profiles rescaled to the year's energy, mean / annual mean: …", 1.000 unless the cap stopped a profile, named in a separate line). The demand is not rescaled: the demand is the main clustering feature and its periods stay close to the year (Cuba: × 1.003 over the horizon, at most 0.9 % off in any year). A series still off by more than 10 % after the rescaling (the demand, or a profile held back by the cap) gives a warning that advises more TSAM periods (`tsam_num_periods`). The adequacy credit (CAP) reads the same profiles: the minimum of a variable renewable's profile, or a dispatchable renewable's mean over the peak steps, may move slightly when those steps fall on a period.

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
0 \leq \text{SOC}^{bnd}_{y,b,n,p} \leq \bar{E}^{cum}_{y,b,n} \quad \forall p \tag{TSAM-CAP}
\]

\(\bar{E}^{cum}_{y,b,n}\) is the battery's energy in service that year (CUM-2), the same bound as the period's SOC (BAT-1): the rated energy within its lifetime, afterwards the life-extended energy (`battery_energy_in_service`). Earlier versions bounded the boundary SOC by the rated energy within the lifetime only, so a battery the master kept past its lifetime had its boundary SOC held at 0 and could not carry energy from one period to the next.

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
   e. Re-solve not optimal, or curtailing more above the annual curtailment
      limit than the plan before → undo this round and stop: the last optimal
      plan and its retirements are kept
```

A retired unit stays retired in the next rounds. The log lists each round's retired units with their NPV. When a re-solve is not optimal (retiring the units made the master infeasible, for instance), or curtails more above the annual curtailment limit than the plan before (retiring the store that held the limit, which the master pays at 10⁴ \$/MWh, §8.3.6; under the hard limit of earlier versions such a re-solve was infeasible), a warning names the units of the undone round and the reason, and the run goes on with the last optimal plan; the results list only the retirements applied.

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

**Myopic mode.** Each annual master models one year \(y_0\), but the NPV covers the unit's remaining in-service years inside the planning horizon, as perfect foresight values them (`extra_years` = last horizon year − \(y_0\), `extra_unit_npv`). It is an approximation (myopic foresight): the later years' operation is not modelled but held at the solved year's level, with the yearly factors the master knows. For a later year \(y_0 + k\) while the unit is within its `life_time`:

\[
\text{NPV}^{+}_{g,n} = \sum_{k} \frac{\delta^k E_{y_0} p^{LCOE} - \delta^k \left( C^{op}_{y_0} - F_{y_0} + F_{y_0} \gamma^k \right) - c^{fix}_{g,n} \bar{P}_{g,n} (1-d)^{a_{y_0}+k}}{(1+r)^{k}} \tag{NPV-3}
\]

with \(\delta = 1-d\) the capacity degradation, \(F_{y_0}\) the fuel part of the operating cost and \(\gamma\) its yearly growth (1 + the `price_growth_rate` of the fuel it burns, from the `fuels` block or the primary-energy tank, times a stochastic scenario's `fuel_price_growth`; an offer curve and the CO2 price have no path in the master). A battery keeps its yearly revenue, operating cost and fixed O&M. No life extension is assumed: in the first year past its `life_time` (the first later year for a unit the master life-extended) the unit pays its decommissioning on the capacity it had, discounted to that year, and nothing after. In a stochastic master each scenario uses its own multipliers and discount rate.

### 9.3 Retirement


**Julia function**: `retire_units(input, units; decommission=true)`

A retired unit has no capacity at its bus for the whole horizon: no dispatch, no life extension, no fixed O&M and no end-of-life decommissioning. It pays its decommissioning cost once, in the first year, on its (degraded) capacity then, as a unit retiring at the end of its life does (\(C^{dec}_y\), OBJ-3b; `npv_retired_units` of the master input). The master is then solved again from scratch, so the same mechanism works for every solver.

In the run, the retired generators have no capacity in the dispatch from the first year on (the master's cumulative capacity is 0), and the retired batteries no power or energy at their node. The results file lists them in the `npv_retirements` group ([HDF5 Output Schema](../reference/hdf5-output-schema.md)), and the retired generators count in the yearly `retirement_gen_{g}` attributes the dashboard's retirement chart reads.

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
\min \; Z^{stoch} = \sum_{\omega} \pi_\omega \Big[ C^{inv}_{\omega} + \sum_{y=1}^{Y} \frac{C^{asset}_{y,\omega} + C^{op}_{y,\omega}}{(1+r_\omega)^{y-1}} \Big] + C^{slack} \tag{STOCH-1}
\]

where \(r_\omega = r \cdot m^{discount}_\omega\) is the scenario-adjusted discount rate, \(C^{inv}_{\omega}\) the investment annuities of OBJ-2 with the scenario's investment-cost multipliers and its rate \(r_\omega\) (in the CRF and the discounting), \(C^{asset}_{y,\omega}\) the life-extension and decommissioning cost (decommissioning scaled by the scenario's multipliers), and \(C^{op}_{y,\omega}\) the operational cost of the scenario's own dispatch, its RE-target shortfall (RE-2, per scenario) included. The budget and adequacy slacks \(C^{slack}\) are first-stage, at the base rate. See [Stochastic Programming](stochastic-programming.md).

---


## 11b. Benders Decomposition


**Julia file**: `benders.jl` (entry point `run_benders_decomposition`). Selected with `master_problem.solver_method: benders`; the default is `monolithic`.

Instead of one LP with all representative days embedded, Benders solves an investment-only master and one dispatch subproblem per year, holding all of that year's representative days, linked by optimality and feasibility cuts. It reaches the monolithic optimum: the master has exactly the investment constraints and costs of the monolithic master, and the subproblems exactly its representative-day operation and annual constraints.

**Master** (`create_benders_master`): the variables and constraints of §3 and §7 (TECH, BUD, RET, CAP, TXN) without representative days, plus one recourse variable \(\theta_{y} \geq 0\) per year (`theta_y{y}`, in the cost units of the input, M\$ from the bridge) that estimates that year's operating cost:

\[
\min \; C^{inv} + \sum_{y} \frac{C^{asset}_y + \theta_{y}}{(1+r)^{y-1}} + C^{slack} \tag{BEN-1}
\]

with \(\theta_y\) bounded below by a relaxation of the year's operation (operating bound, below). \(C^{inv}\) (the annuities of OBJ-2), \(C^{asset}_y\) (`build_annual_asset_cost`: life extension, decommissioning, the fixed cost of the converter capacity built and of the batteries in service, and the electrolyzers' fixed cost) and \(C^{slack}\) (OBJ-7) are the monolithic master's. Apart from that bound, the master has no constraint of its own linking investment to the renewable energy, the CO2 budget or storage: those are in the subproblems, and the cuts carry the value of every capacity. With a growth cap it also holds each year's renewable generation over demand \(\rho_{\sigma,y}\) (below).

**Subproblems** (`create_benders_year_subproblem`): one LP per year with each of its representative days (`create_benders_subproblem`): the representative-day dispatch of §8.3 with the capacities fixed at the master's values, including the converters, the link flow cost, the LP start-ups, the candidates' curtailment cost, the full loss-of-load penalty (VOLL and the sectoral premiums) and the fuel block. Each system's curtailment over the year is at most \(\bar{\kappa}\) times its renewable output over the year (CURT-1, `max_curt_s{σ}_y{y}`, a hard limit over the year's days) plus the curtailment the growth cap forces on existing renewables (RE-3c, the same \(\theta_{\sigma,y}\) as the monolithic master), as in the monolithic model. The year's annual constraints, which couple its days, are in the same LP, on the days' emissions, renewable generation \(E^{RE}_{\sigma,y}\), total generation \(G_{\sigma,y}\) and demand \(D_{\sigma,y}\), each scaled to the year as in the monolithic master:

- the CO2 budget of each system, CO2-1 (`co2_budget_y{y}_{group}`, violation `co2_budget_violation_y{y}_{group}` at the system's penalty);
- the RE target and floor, RE-2 (`re_target_s{σ}_y{y}`, shortfall `slack_re_s{σ}_y{y}` at \(c^{fre}\));
- with a growth cap (\(\Delta\rho^{max} < 1\)), the cap RE-3b (`re_max_increment_s{σ}_y{y}`, slack at VOLL), its allowance \(D_{\sigma,y}\,\rho_{\sigma,y-1}\) from a copy of the master's \(\rho\) of the year before (year 1, or a year after one without demand: \(\rho^0_\sigma G_{\sigma,y}\)).

So the master has nothing to share out among the days. The objective is the year's operating cost, scaled to the year and not discounted (the master discounts \(\theta_y\)).

**RE share across years** (growth cap only): the cap of year \(y\) is measured from the renewable generation of year \(y-1\), which another subproblem produces. The master holds it over the demand as \(\rho_{\sigma,y-1}\) (`re_energy_ratio_s{σ}_y{y}`, for every year but the last with demand; the others are fixed to 0), and the subproblem of year \(y-1\) must reach it:

\[
E^{RE}_{\sigma,y-1} + u_{\sigma,y-1} \geq \rho_{\sigma,y-1} \, D_{\sigma,y-1} \tag{BEN-RE}
\]

(`re_share_s{σ}_y{y}`, \(u\) = `re_share_short_s{σ}_y{y}` at VOLL, `re_cap_penalty`). A \(\rho\) above the actual one pays at least as much per MWh as the room it gives the next year's cap is worth (renewable energy above the cap costs `re_cap_penalty` itself), so at the optimum \(\rho\) is at most the actual share and the cap is never looser than RE-3b: both have the same optimum. The subproblem's cost only rises with \(\rho_{\sigma,y-1}\) in year \(y-1\) and only falls with it in year \(y\), so the cuts never invite the master to move it for free. Earlier versions gave each representative day its own share of the year's RE target and CO2 budget as master variables, which reached the subproblems through equalities priced at VOLL both ways; the master moved those shares between days at no cost of its own, its \(\theta\) stayed at 0 and the gap barely closed (Cuba, 25 years: 99.89 % after four iterations; three years: 98.72 % after eight, now 0.86 % after 25).

**Copy variables**: every master quantity a subproblem reads, other than the technology and battery-technology capacities (their gradient is the duals of the constraints holding them, each times its coefficient: availability on the power limits, ramp rate × \(\Delta t\) on the ramp limits, availability and `dynamic_reserve_contribution` on the reserve constraints of §8.3.0), enters it as a copy variable fixed to the master's value (`copy_*`, `fuel_cap_*`): the converter MW built up to the year, the MW built on each line's corridor, the reservoir MWh built up to the year (the level bounds of §8.3 include it), a life-extended unit's capacity, the fuel storage, route and electrolyzer capacity in service (§8.3.12) and the RE shares above. The dual of the fixing constraint is that quantity's gradient in the cut; a quantity read by several days has the sum of their duals.

**Operating bound** (`add_benders_operating_bound!`, `op_bound_y{y}`): the master also holds, for each year, a relaxation of its subproblem, and \(\theta_y\) is at least its cost. Each representative day of each system is one copper plate per step (no network, converters, reserves, inertia, minimum output or ramps); each generator and technology is pooled over the system's buses, up to its capacity × availability, at its least cost per MWh over those buses (fuel or cheapest offer-curve segment, maintenance, CO2 price; no fuel price with the fuel block, which prices fuel through its balance); the system's batteries and battery technologies are one store with the best efficiencies and a free initial level; load shed costs VOLL (times the least sectoral criticality when below 1). Systems linked by a line or converter (`benders_linked_systems`, directly or through others) share one balance per step, since the subproblem lets one import from another: with a balance per system, a system that imports had to shed in the bound, which then passed the year's cost (two linked systems, one without units: a bound of 4.4·10⁶ M\$ against an optimum of 1.1·10⁴ M\$). The year's RE target, growth cap and \(\rho\) (BEN-RE) are as in the subproblem, and so is each system's annual curtailment limit (`op_bound_curt_s{σ}_y{y}`): the variable renewables' pooled available energy left unused over the year is at most \(\bar{\kappa}\) times the year's renewable output, plus what the growth cap lets the year curtail (RE-3c) and the subproblem's tolerance (below). Supply above a system's load is at most what the subproblem can draw beyond it (`benders_power_sink`): each lossy line's PWL loss at full flow in one direction (a line never loses more than the loss of its net flow, see [Exact losses](dc-power-flow.md#exact-losses)), with no bound for converters, reservoir pumps, electrolyzers or links between systems. Without the limit the relaxation curtailed for free whatever renewable capacity the master built, while the subproblem cannot curtail beyond it, so the master built more than the years could absorb and their subproblems became infeasible (Cuba, 25 years: from the second iteration on). Every operation of the subproblem is feasible in the relaxation at no higher cost, so the bound holds at every plan and leaves the optimum unchanged; the cuts only add what it leaves out (the network above all). Without it \(\theta\) started at 0 and the master moved capacity that cost it nothing, such as a battery it had already paid for, to wherever the latest cut promised the most, iteration after iteration.

**Battery floor** (`benders_battery_floor`): the subproblems see at least \(10^{-4}\) MW (`BENDERS_BATTERY_FLOOR`) of each battery technology, with that times its minimum duration (at least 1 h) of energy, at every bus where one can be built. With no battery at a bus, its charge, discharge and level are all fixed at 0 and the price of its stored energy is free, so the solver may value a MW of power alone at VOLL in every step: a valid but useless cut, which the master answered by moving a battery there. A sliver of battery lets the level move and the duals price power and energy as a battery can use them; it never raises a day's cost and lowers it by at most the value of its tiny energy, so the upper bound and the reported operating cost may be below the plan's own by that much (under 10⁻⁷ relative in the tests).

**Cuts and loop**: after each master solve, all subproblems are solved and the duals of their capacity constraints and copy variables give one optimality cut per year and iteration (`benders_cut_y{y}_iter{k}`): \(\theta_{y} \geq\) intercept + gradient × (technology and battery-technology capacities, copied quantities), the gradient summed over the year's days. A renewable technology's gradient also includes \(c^{CU}\) times its availability over the days: the curtailment cost its capacity adds. Every master quantity a subproblem reads has a gradient, so the cut is added for every year and iteration, also when all gradients are zero (\(\theta_{y}\) is then at least the year's operating cost).

The subproblems are solved with their curtailment limits priced: the slacks `curt_excess_s{σ}_y{y}` are free and each MWh above a limit, scaled to the year, costs VOLL (`benders_curtailment_penalty`). That LP never costs more than the one with hard limits and costs the same when no slack is used, so its cut bounds the year's cost at every plan, and a year that meets its limits is solved exactly as before. When the master's capacities cannot meet a year's limit (more renewable output than the year can use, store or curtail within \(\bar{\kappa}\)), the year still adds the optimality cut of the priced LP, so \(\theta_y\) learns what the plan costs, and is then re-solved with the slacks' sum minimised, whose duals give a feasibility cut (`benders_feas_y{y}_iter{k}`): intercept + gradient × (capacities, copied quantities) \(\leq 0\), which every plan meeting the limits satisfies. If that least violation is zero, the limits can be met at a cost above VOLL per MWh: the year is solved once more with hard limits, and that cost and cut count as for any year within its limits (the priced cut, `benders_cut_priced_y{y}_iter{k}`, is kept as well). A year counts as within a system's limit when it curtails at most \(10^{-3}\) MWh/year \(+\, 10^{-6}\) × the system's renewable generation of the year above it (`curtailment_tolerance`, also in the operating bound): solver tolerances leave a few MWh on limits of TWh, and such a year was reported as breaking its limit by "0.0 GWh/yr", with a feasibility cut and no upper bound. A least violation within the tolerance counts as zero; the hard re-solve then holds each slack at its value there. Before, such a year gave only the feasibility cut: on the 25-year Cuba case more years became infeasible every iteration, their \(\theta\) never got another cut, the lower bound stopped and no iteration gave an upper bound.

**Stabilisation** (in-out): from the second iteration on, the subproblems are not solved at the master's plan \(x^k\) but at the point \(\tfrac12 x^k + \tfrac12 \bar{x}\) between it and the point solved the iteration before, \(\bar{x}\) (`BENDERS_INOUT_WEIGHT`); that point is then \(\bar{x}\) for the next one. A cut is valid at any plan, and an upper bound is the cost of the point solved, so the method stays exact; after an iteration that did not raise the lower bound, once the gap is below 1 % (`BENDERS_INOUT_GAP`), and when the master has integer variables, the master's plan itself is solved. The cuts of a year price capacity and RE shares at the value of lost load, so the master's plan swung from building far too little to far too much where one cut promised the most (Cuba: renewable shares above what the year reaches, then curtailment above the limit); halfway points damp that. The plan returned is the point of the best iteration; when the gap closes at a halfway point, the master's plan is solved once more and returned instead if it is within the tolerance too.

Before the first master solve, warm-start cuts are added from two high-investment points (large candidate capacity, and moderate renewable and battery capacity), with the RE shares at their targets. Generators and batteries in service enter them at the capacity the subproblems give them (degraded rated power).

The subproblems use the configured solver (`solver.name`, its threads and options); HiGHS solves them single-threaded, which avoids its intermittent failures of multi-threaded runs on these LPs.

The lower bound is the master objective; an iteration whose subproblems all solve within the curtailment limit gives an upper bound, the first-stage cost plus the discounted subproblem costs, in the same units. Each iteration logs how many years met their curtailment limits, each year that did not and by how much (its curtailment against the limit, in GWh/year), and the operating cost by term (generators, load shed, RE target shortfall, RE share below the master's, curtailment above the limit, …); `total_ops` counts a year that broke its limit at its priced cost. At DEBUG it also logs each year's \(\theta\), cost and terms, and what the master builds each year (MW per technology, the capital spent against the budget and its slack, the RE shares). The loop stops when the relative gap is below `benders_tolerance` (default 10⁻⁴) or after `benders_max_iterations` (default 50). Either way, the plan returned is that of the iteration with the best upper bound, with that iteration's operating costs, so the reported objective is what the plan costs. With a growth cap, a run stopped before converging may report a cost that includes \(u\) (a share the master asked for above the actual one). With `ESFEX_BENDERS_DEBUG_LP=1`, a master or subproblem that fails to solve is written as an LP file to the run's `logs` folder.

Benders does not support stochastic scenarios (they are ignored with a warning). It always uses the representative days of §8.1, the same days of the year for every year, as the monolithic master, but not the critical days of §8.1b (the log says so); its master holds the storage firm credit of CAP-5. With `use_tsam` or user investment constraints (`custom_constraints` with `target: investment`), which the Benders master does not model, the runner solves the monolithic master instead and logs a warning saying why.

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
| `re_penetration_by_year`, `re_penetration_by_system` | Achieved RE share \(E^{RE}_{\sigma,y}/D_{\sigma,y}\) (RE-2) per year, all systems together, and per system; the expected value over the scenarios in a stochastic master, the subproblems' in Benders. It was the credited ratio \(\rho\) |
| `annuity_cost_by_year` | Investment annuities paid in each year (\$, undiscounted, base costs and discount rate): what OBJ-2 charges before discounting |
| `annuity_streams` | The annuity streams behind `annuity_cost_by_year`: (build year, life, annuity per year), summed over the builds of a year with the same life; each is paid in its `annuity_years`. The myopic runner adds the streams of earlier years' builds to each year's reported cost |
| `total_investment_by_year`, `total_operational_cost_by_year` | Investment (undiscounted overnight cost of every build of the year: technologies, battery technologies, transmission, converters, reservoirs, fuel storage, routes and electrolyzers; what the budget limits, not what the objective charges) and operating cost per year (\$, undiscounted: the representative days or TSAM periods scaled to the year, CO2-budget violations and the RE-target shortfall included; the expected value over the scenarios in a stochastic master; the subproblems' cost in Benders). The annual cost of the assets in service (fixed O&M, life extension, decommissioning) is in neither |
| `reservoir_investment` | Reservoir storage investment |

The runner turns the cumulative technology capacity into virtual generators and batteries for the operational dispatch (§16.3).

---


## 13. Infeasibility Diagnostics


**Julia function**: `diagnose_infeasibility(model, vars, input)`

If the model solves successfully but with non-zero slack, the diagnostics report which constraints required relaxation:

| Slack Variable | Indicates |
|----------------|-----------|
| \(s^{re}_{\sigma,y} > 0\) | MWh of renewable energy missing from system \(\sigma\)'s target in year \(y\) (`re_target_s{σ}_y{y}`) |
| \(s^{grow}_{\sigma,y} > 0\) | MWh of renewable energy above system \(\sigma\)'s growth cap in year \(y\) (`re_max_increment_s{σ}_y{y}`) that could not be curtailed (RE-3c covers the existing variable renewables) |
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
  |     |-- master_rep_days()                     # Sec. 8.1 (the same days every year)
  |     |-- re_cap_exemption()                    # RE-3c
  |     |-- create_day_ps_vars!()                 # Sec. 8.2
  |     |-- add_day_operational_constraints!()    # Sec. 8.3
  |     |-- calculate_day_operational_cost()      # OBJ-5, SCALE-1
  |     +-- add_re_target_constraints!()          # RE-2, RE-3 (once per year)
  |           +-- add_re_growth_cap!()            # RE-3b
  |-- define_re_share!()                          # RE-2b
  |-- apply_user_constraints!()                   # custom constraints
  +-- build_master_objective!()                   # OBJ-1
        |-- build_investment_cost_expression()    # OBJ-2 (annuities)
        +-- master_slack_cost()                   # OBJ-7

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
