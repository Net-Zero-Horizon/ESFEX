# Core Concepts


## Capacity Expansion Planning

The **Master Problem** minimizes the net present value (NPV) of total system cost over a multi-year planning horizon [**[24]**](../reference/bibliography.md#ref24), choosing which candidate technologies (`technologies`, `battery_technologies`) to build, which transmission links to expand, how long to keep existing units, and when. With the default `master_problem.planning_mode: perfect_foresight` it considers all years simultaneously — a decision to invest in year 3 accounts for the remaining useful life and operational savings through the last year. With `planning_mode: myopic` it solves year by year, using only the information available at each decision.

The Master Problem is a linear program (LP) operating on representative days [**[22]**](../reference/bibliography.md#ref22), [**[23]**](../reference/bibliography.md#ref23) rather than full hourly detail. Investment decisions are continuous (MW of capacity), avoiding the computational cost of mixed-integer programming at the strategic level. Investment costs are annualized and discounted to present value; the optimizer balances capital cost against operational savings.

---


## Operational Dispatch

Operational dispatch determines the output of every generator and battery in every hour to meet demand at minimum cost while respecting technical constraints. The `simulation_mode` setting selects one of three workflows:

- **`development`** (default) -- Master problem first, then dispatch of every year with the investments applied. Dispatch is a linear program (LP): generators operate continuously between zero and rated power, with no binary on/off decisions, minimum up/down times or start-up costs. Setting `master_problem.use_uc_in_dispatch: true` switches the dispatch windows to unit commitment (MIP), which is slower but gives more realistic operating costs.

- **`unit_commitment`** [**[31]**](../reference/bibliography.md#ref31) -- No master problem. A mixed-integer program (MIP) with binary on/off decisions, minimum up/down times, start-up costs and minimum stable generation, for the existing fleet over the first `unit_commitment_hours` hours (default 24) of a single year.

- **`economic_dispatch`** -- No master problem. Dispatch of the existing fleet over the simulated years without investments; LP with DC power flow, or a conic/nonlinear problem with the AC-OPF formulations (`power_flow_mode: acopf_*`).

For planning studies use `development`. Use `unit_commitment` for short-term operational detail and `economic_dispatch` for operating a fixed system, e.g. AC-OPF studies.


---


## Two-Stage Decomposition

ESFEX solves the planning problem in two stages [**[25]**](../reference/bibliography.md#ref25), [**[26]**](../reference/bibliography.md#ref26).

```
                    STAGE 1: Master Problem
                    =======================
     All years simultaneously (e.g., 25 years)
     Representative days per year (e.g., 5 days)
     Decision: How much to invest in each technology per year

                           |
                           | Investment schedule
                           | (MW per technology per year)
                           v

                    STAGE 2: Operational Dispatch
                    =============================
     Year by year, sequentially (year 1, then year 2, ...)
     Full hourly detail (8760 hours per year)
     Rolling horizon windows (e.g., 48h with 24h overlap)
     Decision: How much each generator produces each hour

                           |
                           | Hourly dispatch, prices, emissions
                           v

                    Results & Metrics
                    =================
     LCOE, VALLCOE, capacity factors, CO2, load shedding
     HDF5 export with full time series
```

**Stage 1 (Master Problem)** solves over all years simultaneously, determining investment and retirement decisions using representative days to estimate operational costs. Because it sees the entire planning horizon, it can make forward-looking decisions — investing in battery storage in year 5 in anticipation of curtailment problems in year 8.

**Stage 2 (Operational Dispatch)** takes the investment decisions from Stage 1 and solves detailed hourly dispatch for each year independently using the full 8760-hour profiles and a rolling horizon. This stage reveals the actual operational cost, curtailment, load shedding, and electricity prices.

The two stages are solved sequentially (not iteratively). The Master Problem's operational cost estimates from representative days may differ from actual Stage 2 costs; for well-configured systems the difference is small.

The master problem itself is solved as one model by default (`master_problem.solver_method: monolithic`). For very large problems, `solver_method: benders` decomposes it into an investment master and per-representative-day dispatch subproblems. See [Master Problem](../user-guide/master-problem.md).


---


## Rolling Horizon

Each year's 8760 hours are too many for a single optimization problem. ESFEX divides the year into overlapping windows solved sequentially. For example, with a 48-hour window and 24-hour overlap (the default):

1. Solve hours 1--48 (window 1).
2. Keep results for hours 1--24 (the non-overlapping portion).
3. Use the state at the end of hour 24 (battery SOC, generator status) as initial conditions for window 2.
4. Solve hours 25--72 (window 2, which overlaps hours 25--48 with window 1).
5. Repeat until all 8760 hours are covered.

Key parameters:

- **Window size** (`rolling_horizon_hours`) -- Larger windows capture longer-duration storage dynamics but are slower to solve.
- **Overlap** (`overlap_hours`) -- A look-ahead: each window plans its kept hours knowing what comes next. Stored energy has no value at the end of a window, so with a short overlap storage is not charged for a peak just past it. Keep it at 24 h or more when the system has storage.
- **Boundary conditions** -- Battery SOC and generator status (on/off in unit commitment mode) from the end of the non-overlapping portion of one window initialize the next window.

Only results from the non-overlapping portion are kept. The overlap region is discarded and re-solved by the next window with better foresight.


---


## Representative Days

The Master Problem selects a few **representative days** [**[37]**](../reference/bibliography.md#ref37) from the demand profile to estimate annual operational costs, avoiding the need for full 8760-hour dispatch across all years [**[22]**](../reference/bibliography.md#ref22). For each year:

- **Peak day** -- The day containing the highest hourly system demand is always selected.
- **One day per season segment** -- The rest of the year is divided into equal segments, and the day with the highest peak demand in each segment is added, so the selection spans the seasons.
- **Minimum separation** -- Selected days must be at least `min_day_separation` days apart (default 5).

The number of days (`master_problem.representative_days`, default 5) controls the accuracy/speed trade-off. The Master Problem scales costs from representative days to approximate the full year; Stage 2 then refines with full hourly detail. Alternatively, `master_problem.use_tsam: true` selects representative periods by clustering (`tsam_num_periods`, `tsam_method`); `representative_days` and `min_day_separation` are then ignored.


---


## Age-Based Retirement

Retirement is driven by age — no binary retirement decisions are introduced, preserving the LP structure.

- **Existing units**: `age = initial_age + (year - 1)`. The unit runs at full capacity while `age < life_time`.
- **Life extension**: once an existing unit reaches its `life_time`, the master problem may keep any part of its capacity online, as a continuous decision. Each MW kept costs `invest_cost × life_extension_cost_factor` per year (`life_extension_cost_factor` defaults to 0.2). With `invest_cost: 0` the extension is free, so set a realistic `invest_cost` on existing units.
- **New investments**: `age = year - investment_year`. The capacity is active while `age < lifetime`.

Example: a diesel generator with `initial_age: 10` and `life_time: 30` reaches its lifetime in year 21 of the simulation (age = 30); from then on it runs only as far as the optimizer pays for life extension. An investment made in year 5 with `lifetime: 25` retires in year 30.

Capacity degrades over time: `effective_capacity = rated_power * (1 - degradation_rate)^age`, with `degradation_rate` given as a fraction per year. Typical values: 0.003--0.005 (0.3--0.5 %/year) for renewables, 0 for thermal units.


---


## Penalties and Soft Constraints

Several constraints are enforced through penalties in the objective function rather than as hard bounds. This guarantees feasibility even for overconstrained systems. The penalties represent the economic cost of violating each constraint.

The penalties are set per system under `penalties:` (key names are case-insensitive):

| Constraint | Config key (`penalties.`) | Default | Interpretation |
|-----------|---------------------------|---------|----------------|
| Unserved demand (loss of load) | `loss_of_load` | 10,000,000 $/MWh | Value of lost load [**[33]**](../reference/bibliography.md#ref33) -- the economic damage of not serving demand |
| RE penetration shortfall | `fre_penetration_loss` | 100 $/MWh | Policy cost of missing the renewable energy target |
| Reserve shortfall | `loss_of_reserve_static`, `loss_of_reserve_dynamic` | 100 $/MW | Cost of not holding the required reserve |
| Curtailment | `curtailment_cost` | 20 $/MWh | Cost of curtailed renewable energy (the master problem also caps curtailment at `max_curtailment_ratio` of RE output) |
| CO₂ budget violation | `co2_budget_violation` | 500 $/t | Penalty for exceeding the annual emissions budget (`co2_budget`) |

All penalty values are user-configurable. Higher penalties push the optimizer harder to satisfy the constraint; lower values allow more violation when it is cost-effective. Setting penalties too high can cause numerical issues. Do not confuse `loss_of_load` with the deprecated system-level `LOSS_DEMAND_TRHESHOLD`, which is ignored. See the [Configuration Reference](../reference/config-reference.md) for all penalty keys.

Any violated constraint in the optimal solution has a clear economic interpretation: satisfying the constraint costs more than the penalty.


---


## Renewable Energy Targets

RE targets interpolate linearly from the initial RE share to `target_re_penetration` (default 1.0) over the planning horizon. The initial share is computed from the existing renewable fleet and the first year's demand; it is not a configuration setting. Example: with an initial share of 0.20 and a target of 0.80 over 25 years, the year 10 target is approximately 42.5%.

Annual increment bounds (`min_annual_increment`, default 0.01, and `max_annual_increment`, default 0.10) limit year-to-year changes of the target, reflecting construction timelines and grid integration constraints.

RE penetration is defined as:

```
RE penetration = total renewable generation / total demand served
```

Battery discharge from energy originally produced by renewable sources counts toward renewable generation. Curtailed renewable energy does not count, since it was not used.

---


## Curtailment

Curtailment is available renewable energy that goes unused because demand is already met or the network cannot absorb it. Rather than relying on cost penalties alone, the master problem limits curtailment on each representative day through a hard constraint:

```
total curtailment <= max_curtailment_ratio * total renewable generation
```

`max_curtailment_ratio` is set under `penalties:` and defaults to 0.05 (5%). When the system approaches this limit, the optimizer has to invest in storage or other flexibility to absorb excess renewable production instead of overbuilding renewables. In addition, curtailed energy is charged at `penalties.curtailment_cost`.


---


## Sectoral Demand

Demand can be split into sectors (`electric_demand`) with different criticality levels: `critical`, `high`, `medium` or `low`. The level selects a weight from `criticality_penalties` that scales the load-shedding cost, so the optimizer sheds low-criticality load first:

- **Critical sectors** (hospitals, water treatment, essential services) -- Shedding these loads is the most expensive, so the optimizer avoids it.
- **Standard sectors** (residential, commercial) -- Shed only when necessary.
- **Flexible sectors** (`is_flexible: true`; industrial processes, EV charging, water heating) -- Can participate in demand response: up to `flexibility_ratio` of the load can be shifted in time, within `delay_tolerance` hours.

Sectoral decomposition enables intelligent load-shedding decisions, prioritizing essential services over flexible loads.


---


## Electric Vehicle Integration

The EV module uses an **S-curve adoption model** [**[53]**](../reference/bibliography.md#ref53) to project fleet growth from an initial count to a saturation level.

- **Multi-category vehicles** -- Different vehicle types (passenger cars, buses, trucks) with distinct energy consumption, battery capacity, and driving patterns.
- **Charging profiles** -- Time-of-day charging demand based on driving patterns and charger availability.
- **V2G (Vehicle-to-Grid)** [**[51]**](../reference/bibliography.md#ref51) -- Bidirectional charging that allows EVs to discharge back to the grid during peak demand, effectively acting as distributed battery storage.
- **Demand integration** -- When the system defines EV categories, the fleet's charging is a decision variable of the dispatch (driving draws energy from the fleet battery, so the charging can shift in time but must replace that energy; V2G can discharge back to the grid only what is left); the EV energy is then not added to the base demand, so it is counted once.

---


## Multi-System Modeling

ESFEX can model multiple independent power systems connected through inter-system transmission links and fuel routes. Each system is defined separately with its own nodes, generators, batteries, and demand; the `meta_network` section defines interconnections. Useful for island archipelagos, regional grids, or separate utility territories sharing fuel infrastructure.


---


## Primary Energy

The primary energy module models the fuel supply chain from import to consumption:

- **Fuel availability** -- How much fuel can be imported at each node per time period, with costs and capacity limits.
- **Transport** -- Fuel movement between nodes with capacity limits, transport costs, and losses.
- **Storage** -- Fuel inventory at each node with minimum and maximum levels, injection and withdrawal rates.
- **Coupling to generators** -- Each thermal generator consumes fuel at a rate determined by its heat rate and output. If fuel is unavailable at a node, the generator cannot run.

The primary energy module operates at a coarser temporal resolution than electrical dispatch (typically daily or weekly).


---


## NPV Minimization

$$
\min \sum_{y=1}^{Y} \frac{1}{(1+r)^{y-1}} \left[ C^{inv}_y + C^{op}_y + C^{penalty}_y \right]
$$

where:

- $Y$ is the number of planning years
- $r$ is the discount rate (typically 5--10%)
- $C^{inv}_y$ are investment costs in year $y$ (capital expenditure for new generators, batteries, and transmission)
- $C^{op}_y$ are operational costs in year $y$ estimated from representative days (fuel, O&M, start-up costs)
- $C^{penalty}_y$ are soft constraint violation penalties (unserved demand, RE shortfall, CO2 excess)

The discount rate reflects the time value of money: higher rates favor deferring investment; lower rates favor early investment. The choice of discount rate significantly affects the optimal investment schedule, particularly for capital-intensive renewable technologies.


---


## Near-Optimal Alternatives: MGA and SPORES

In power system planning, multiple investment portfolios often achieve nearly the same total cost. The cost-optimal plan is one feasible answer to *what should we build?*, but it is rarely the only sensible answer. **Near-optimal exploration** systematically maps the rest of the practically-relevant solution space. ESFEX implements two distinct methods for this — they share the same cost-slack envelope but differ in *how* alternatives are produced:

### MGA — Modeling to Generate Alternatives

The classical formulation, due to DeCarolis [**[8]**](../reference/bibliography.md#ref8):

1. Solve the cost-optimal master problem $\to$ $C^*$, $x_0^*$.
2. Add the slack constraint $Z \leq (1+\varepsilon) \cdot C^*$.
3. For $k = 1, \ldots, K$: maximise a single **Hop-Skip-Jump** diversity objective that penalises investment variables seen in $\{x_0^*, \ldots, x_{k-1}^*\}$, then solve.

Every alternative optimises the same diversity score $\sigma = 1 - 2 \cdot \mathrm{freq}$; the score history is the only thing that changes between iterations. Configuration: `master_problem.mga.method = "mga"`, `num_alternatives`, `slack_fraction`.

### SPORES — Spatially-explicit Practically Optimal Results

Introduced by Lombardi et al. (2020) [**[7]**](../reference/bibliography.md#ref7) for Calliope. Instead of one diversity objective applied repeatedly, SPORES solves **one alternative per declared objective** under the same cost cap. ESFEX ships four canonical objectives:

| Objective | LP form | Question it answers |
|-----------|---------|---------------------|
| `min_total_build` | $\min \sum I$ | What is the *smallest* near-optimal portfolio? |
| `max_tech_equity` | min-max over per-tech totals | Can the portfolio be technology-diversified? |
| `max_regional_equity` | min-max over per-node totals | Can investments be spatially spread? |
| `evolutionary_dist` | $\max$ L1 distance from $x_0^*$ | What is the *maximally different* feasible plan? |

Configuration: `master_problem.mga.method = "spores"`, `objectives = [...]`. The alternative count equals `len(objectives)`.

### Reading the output

Both methods produce a set of alternative investment portfolios, each near-optimal in cost but differing in technology mix and location. Decisions appearing in most alternatives are **robust** (must-build); those varying across alternatives are **swappable** (the cost slack actually moved them). SPORES additionally tags each alternative with the objective that produced it so charts and post-processing can colour by objective rather than by index.

For the LP formulation of each method, see [Capacity Expansion §15](../formulation/capacity-expansion.md#15-mgaspores-near-optimal-alternative-exploration). For the tutorial walkthrough, see [Near-Optimal Alternatives](../tutorials/mga.md).


---


## How Concepts Relate

```
Configuration (YAML)
    |
    +--> Demand data (CSV/Excel)
    +--> Availability profiles (CSV/Excel)
    +--> Generator / battery / network parameters
    |
    v
Master Problem (Stage 1)
    |
    | Uses: representative days, NPV minimization,
    |       age-based retirement, RE targets,
    |       penalty-based soft constraints
    |
    +--> Investment schedule (MW per technology per year)
    |
    v
Operational Dispatch (Stage 2) -- for each year
    |
    | Uses: rolling horizon, full hourly demand,
    |       availability profiles, curtailment limits,
    |       sectoral demand, EV profiles,
    |       primary energy (if enabled)
    |
    +--> Hourly generation, storage, prices, emissions
    |
    v
Results & Metrics
    |
    +--> LCOE, VALLCOE, capacity factors
    +--> Annual cost, RE penetration, CO2 emissions
    +--> HDF5 export for analysis and visualization
    |
    v
Optional: MGA or SPORES
    |
    | MGA   = classical Hop-Skip-Jump loop (one diversity
    |         objective, K iterations under cost slack)
    | SPORES = one alternative per declared objective
    |         (min-build / tech-equity / regional-equity /
    |          evolutionary-dist under the same cost slack)
    |
    +--> Multiple alternative investment portfolios
```

The two-stage decomposition is the organizing principle: the Master Problem makes strategic decisions (what to build), and operational dispatch makes tactical decisions (how to run it). Penalties provide cost signals guiding investment toward meeting operational constraints.
