# Master Problem (Capacity Expansion)

The Master Problem is the first stage of ESFEX's two-stage optimization [**[24]**](../reference/bibliography.md#ref24), [**[25]**](../reference/bibliography.md#ref25). It runs only in `simulation_mode: development`. It decides **what to build, when to build it, and how long to keep ageing units online** over the planning horizon, minimizing the discounted cost of the system [**[26]**](../reference/bibliography.md#ref26). The second stage, operational dispatch, then runs every year at full time resolution with the capacity the master chose.

The Master Problem answers questions such as:

- How much solar PV, wind or battery capacity should be built, at which node, and in which year?
- When do existing generators reach the end of their lifetime, and is it cheaper to extend them or replace them?
- Is transmission expansion between nodes cost-effective?
- What does a cost-optimal path to a renewable-share target look like?

```
                          MASTER PROBLEM
                    ┌──────────────────────────┐
                    │ perfect_foresight:       │
                    │   all years in one model │
                    │ myopic:                  │
                    │   one model per year     │
   INPUTS ─────────►│                          ├──────► OUTPUTS
   • technologies   │ Minimize discounted:     │        • MW / MWh built per
   • demand file    │   investment cost        │          technology, node, year
   • RE target path │   + life-extension,      │        • life extension of
   • fuel and O&M   │     decommissioning and  │          ageing units
     costs          │     fixed O&M cost       │        • transmission expansion
   • availability   │   + representative-day   │        • RE share per year
     profiles       │     operating cost       │
                    │   + slack penalties      │
                    └──────────────────────────┘
                              │
                              │ capacity per year
                              ▼
                    ┌──────────────────────────┐
                    │  OPERATIONAL DISPATCH    │
                    │  (year by year,          │
                    │   rolling horizon)       │
                    └──────────────────────────┘
```


---


## How It Works


### Linear Program

The Master Problem is a **linear program (LP)**. All investment variables are continuous, so a result reads "build 87.3 MW of solar PV at node 2 in year 5", not "build or don't build a 100 MW plant". Age-based retirement is computed when the model is built; keeping a unit past its lifetime is a continuous life-extension decision (see [Retirement and Life Extension](#retirement-and-life-extension)). The representative days inside the master are dispatched as economic dispatch, without unit commitment binaries.


### Planning Mode

`master_problem.planning_mode` selects how the years are coupled:

| Mode | What is solved | Consequence |
|------|----------------|-------------|
| `perfect_foresight` (default) | One model covering every year of the horizon | Investment timing sees all future demand, costs and targets |
| `myopic` | One single-year model per year, solved in sequence; capacity built in earlier years is added to the system as existing capacity. Each year's model reads that year's block of multi-year availability and inflow profiles | Each decision only sees the current year |

In `myopic` mode, each year's model starts from the base-year fleet aged by the number of years elapsed. The capacity built in earlier years is added as existing units: one unit per technology and build year, at the nodes where it was built, with the technology's operating parameters. When the master works on buses (a one-node system with several buses, or development zones), each bus with new capacity gets its own unit on that bus, so the next year's model sees the capacity where it was built. Each unit ages from its build year with the technology's degradation rate and retires at the end of the technology's `lifetime`, as perfect foresight counts its investments. The `invest_max_power` / `invest_max_capacity` of each technology is a limit over the whole horizon, so every year's model can only build what earlier years left. The operational dispatch of each year receives the capacity of that year's model: the existing units plus, per technology, what was built that year and what is left of earlier builds.

The two modes treat the RE target differently. See [RE Penetration Trajectory](#re-penetration-trajectory).

MGA/SPORES runs only in `perfect_foresight` mode.


### Solver Method

`master_problem.solver_method` selects how the model is solved:

| Method | Description |
|--------|-------------|
| `monolithic` (default) | One LP that holds the investment variables and all representative-day dispatch |
| `benders` | Benders decomposition: an investment-only master with one recourse variable per year and representative day, and one dispatch subproblem per year and representative day. Iterates until the relative gap is below `benders_tolerance` or `benders_max_iterations` is reached |

Benders solves the same problem as the monolithic master and reaches the same optimum: the same investment constraints and costs, the same representative-day dispatch with the full loss-of-load penalty, and the same hard curtailment limit (`max_curtailment_ratio`). If it stops at `benders_max_iterations`, it returns the plan of its best iteration together with that plan's operating cost, and logs the remaining gap.

!!! note "Benders: limitations"
    - Benders always selects representative days by peak demand. With `use_tsam: true`, or with investment-target custom constraints, the run solves the monolithic master instead and logs a warning saying why.
    - Benders ignores `stochastic: true` (with a warning) and solves the master deterministically.


### Representative Days

The master cannot dispatch every hour of every year. Instead, it builds a full dispatch model (generation, storage, curtailment, load shedding, reserves) for a few **representative days** per year, and checks the planned capacity against them.

By default the days are chosen by **peak demand**, separately for each year:

1. The day with the highest hourly total demand (summed over all nodes) is always selected.
2. The year is split into `representative_days` equal segments. In each segment, ESFEX picks the day with the highest peak that is at least `min_day_separation` **days** away from every day already selected.

Each representative day's cost is scaled by (days in the year) / `representative_days`, so the days stand in for the full year. More days give better accuracy but a larger model:

| Representative days | Use case |
|---------------------|----------|
| 3 | Screening and parameter tuning |
| 5 (default) | Standard planning studies |
| 7 to 10 | Final studies |

### TSAM Clustering

With `use_tsam: true`, peak-based selection is replaced by time-series clustering. In that case `representative_days` and `min_day_separation` are ignored. For each year, the daily profiles of demand and of the existing renewable generators' availability (that year's block of a multi-year profile, a shorter profile repeating, as in the dispatch) are clustered into `tsam_num_periods` representative days (`tsam_method: kmedoids` or `kmeans`). Each day is weighted by the size of its cluster. With `tsam_inter_period_linking: true`, the state of charge of existing batteries (and reservoir levels) is carried across the representative days in chronological order, which lets the model represent seasonal storage.


### Time Resolution

The master works at `temporal.resolution_hours`. Demand is aggregated with the **maximum** of each block, so peaks are kept. `temporal.investment_resolution` is the length of an investment period in hours (default 8760). It is converted to whole years (`investment_resolution ÷ 8760`, minimum 1). Investment variables exist only in the first year of each period; for example, `43800` gives one investment decision every 5 years.


### Objective and Discounting

The objective is the discounted sum of the yearly costs, with year 1 undiscounted:

$$
\min \; \sum_{y=1}^{Y} \frac{1}{(1+r)^{y-1}} \left[ C^{inv}_y + C^{ext}_y + C^{dec}_y + C^{op}_y \right] + C^{slack}
$$

- \(C^{inv}_y\): **overnight investment cost**, charged in full in the year the capacity is built (the first year of its investment period). It covers technologies (`invest_cost` × MW), battery technologies (`invest_cost_power` × MW + `invest_cost_energy` × MWh), transmission (`transference_invest_cost` × MW), primary-energy storage and transport, and reservoir capacity. The cost is **not** annualised, and there is no salvage credit for lifetime left at the end of the horizon.
- \(C^{ext}_y\): life-extension cost of existing units kept beyond their lifetime.
- \(C^{dec}_y\): decommissioning cost of existing units when they retire.
- \(C^{op}_y\): representative-day operating cost scaled to the year. It includes fuel, fixed and maintenance cost of existing units and new technologies, plus the penalty terms of the dispatch (unserved energy, RE shortfall, curtailment, reserve and inertia shortfall).
- \(C^{slack}\): fixed internal penalties on the slack of the yearly RE target, the investment budget and capacity adequacy. These are not discounted.

`r` is the system's `discount_rate` (default 0.05). A higher rate favours deferring capital-intensive investment.


---


## Investment Decisions

The master only invests through **candidate technologies**. Existing `generators` and `batteries` are not investment candidates. Their `invest_cost` is used only to price life extension; a battery's `invest_max_power` only places it at a node in the representative days.

| Investment | Defined by | Units | Bounded by |
|------------|------------|-------|------------|
| Generation capacity | `technologies` | MW | `invest_max_power` |
| Storage power / energy | `battery_technologies` | MW / MWh | `invest_max_power` / `invest_max_capacity`, and E/P ratio between `min_duration_hours` and `max_duration_hours` |
| Transmission | `nodes.transference_invest_cost` / `transference_invest_max` | MW | `transference_invest_max` (same expansion in both directions) |
| Fuel storage / transport | `fuel_infrastructure` storage facilities / pipelines (`investment_cost` per fuel), with primary energy on | units, units/day | their `expansion_limit` × the configured capacity |
| Electrolyzers | `electrolyzers` (`invest_cost`), with primary energy on | MW | `invest_max_power` |
| Reservoir capacity | generator `reservoir_invest_cost` / `reservoir_invest_max` | MWh | `reservoir_invest_max` |

`invest_max_power` and `invest_max_capacity` are per-node lists. Each value is a **cumulative** cap on what can be built at that node over the whole horizon. When a node has several buses, investment can go to any of them, but the node total stays within the cap. A value of `0.0` disables investment at that node. Total investment cost per investment period is limited to the system's `MAX_ANNUAL_SYSTEM_COST`, with a penalised slack.

```yaml
systems:
  island:
    technologies:
      solar_pv:
        name: Solar PV
        type: Renewable
        fuel: Sun
        invest_cost: [800000.0, 800000.0]       # $/MW, per node
        invest_max_power: [500.0, 200.0]        # cumulative MW per node
        eff_at_rated: [1.0, 1.0]
        degradation_rate: [0.005, 0.005]        # fraction per year
        lifetime: 25                            # years (scalar)
        maintenance_cost: [5.0, 5.0]            # $/MWh
        Availability: data/solar_cf.csv
    battery_technologies:
      li_ion:
        name: Li-ion storage
        invest_cost_power: [200000.0, 200000.0]   # $/MW
        invest_cost_energy: [150000.0, 150000.0]  # $/MWh
        invest_max_power: [100.0, 50.0]
        invest_max_capacity: [400.0, 200.0]
        min_duration_hours: 1.0
        max_duration_hours: 8.0
        efficiency_charge: [0.95, 0.95]
        efficiency_discharge: [0.95, 0.95]
        degradation_rate: [0.01, 0.01]
        lifetime: 15
```

See the [Configuration Reference](../reference/config-reference.md) for every technology field.


### Cumulative Capacity

The capacity available in year \(y\) is the sum of:

1. **Existing units** still within their lifetime, derated as `rated × (1 - degradation_rate)^age` with `age = initial_age + (y - 1)`.
2. **Life-extended capacity** of existing units past their lifetime.
3. **Every earlier investment** still within its technology `lifetime`, derated as `(1 - degradation_rate)^(y - y_built)`.

Renewable technologies are further limited by their availability profile in each representative-day hour.


---


## Retirement and Life Extension

| Unit type | Age in year \(y\) | In service while |
|-----------|-------------------|------------------|
| Existing generator / battery | `initial_age + (y - 1)` | `age < life_time` |
| Technology investment | `y - y_built` | `age < lifetime` |

**Example:** a diesel generator with `initial_age: [10]` and `life_time: [30]` is in service in years 1 to 20. In year 21 its age reaches 30 and its base capacity drops out.

From that year on, the master may keep any part of it online with a continuous **life-extension** variable, up to its degraded capacity. Life extension:

- costs `invest_cost × life_extension_cost_factor` per MW per year (default factor 0.20), using the existing unit's own `invest_cost`, or its candidate's where that is 0 (see below);
- can only decrease from one year to the next (capacity, once retired, does not come back);
- avoids the unit's `decommissioning_cost`, which is charged on the capacity that retires.

!!! note "Units with `invest_cost` 0"
    Existing units often have `invest_cost: [0.0]` (the Studio default). At a node where it is 0, life extension is priced with the investment cost of the unit's candidate: for a generator, the technology with the same `fuel` and `type` (the one named by the generator's `technology` field if several match, else the first in the configuration); for a battery, the battery technology named by its `technology` field, else the first battery technology (`invest_cost_power`). With no candidate the extension stays free, and a warning names the unit (`... extending its life is free in the master problem`); such a unit never retires in the master.

Technology investments are not life-extended: they leave the capacity sum when they reach `lifetime`.

### NPV-based retirement

With the system setting `npv_retirement: true` (off by default), the run also retires existing units that do not pay for themselves. After the master solve, each existing generator and battery gets an NPV over the modelled years, from the plan itself:

- a generator earns `base_lcoe` ($/MWh) on the energy the master dispatched it on the representative days (scaled to the year), and pays what the master charges it: fuel (or the fuel price, with primary energy on), maintenance, CO2 price, start-ups, fixed O&M on its capacity in service, life extension and decommissioning;
- a battery earns `npv_annual_return_rate` × its power investment cost × its MW in service per year, and pays its fixed O&M, maintenance, throughput cost, life extension and decommissioning.

Units with an NPV below `npv_retirement_threshold` ($, default 0) are retired for the whole horizon: they leave the plan and the dispatch from the first year, pay no more fixed O&M and pay their `decommissioning_cost` once, in the first year. The master is then solved again, and the check repeats until no new unit falls below the threshold or after `npv_retirement_max_iterations` rounds (default 5). If a re-solve is not optimal (retiring the units made the master infeasible), that round is undone with a warning naming its units and the solver status, and the run continues with the last optimal plan. The log lists each round's retired units and their NPV, and the results file the `npv_retirements` group ([HDF5 Output Schema](../reference/hdf5-output-schema.md#npv_retirements)).

It works with every master mode: monolithic, Benders, stochastic (the NPV is the probability-weighted mean over the scenarios) and myopic (each year's master checks the original units it still has, valuing them over their remaining in-service years up to the end of the horizon with that year's operation held, an approximation; a unit retired one year stays retired). The formula is in [Capacity Expansion §9](../formulation/capacity-expansion.md#9-npv-based-iterative-retirement).

```yaml
systems:
  my_system:
    npv_retirement: true
    npv_retirement_max_iterations: 5
    npv_retirement_threshold: 0.0     # $
    base_lcoe: 93.0                   # $/MWh earned per MWh generated
    npv_annual_return_rate: 0.15      # battery revenue per year / power investment cost
```


---


## RE Targets and Policy Constraints


### RE Penetration Trajectory

The starting share \(\rho^{init}\) is always computed from the existing renewable generators and the first year of demand: per hour, the smaller of demand and the renewable output available (rated power × availability; a renewable unit without an availability profile counts at full capacity, as in the dispatch). There is no configuration key for it.

In `perfect_foresight` mode, the yearly target path is:

$$
\tau_1 = \rho^{init}, \qquad
\tau_y = \rho^{init} + \frac{y-1}{Y-1}\left(\rho^{target} - \rho^{init}\right)
$$

The step \(\tau_y - \tau_{y-1}\) is then clamped to `[MIN_ANNUAL_INCREMENT, MAX_ANNUAL_INCREMENT]` (defaults 0.01 and 0.10). Because of the clamp, the path can end **below** `TARGET_RE_PENETRATION` (large gap, small maximum step) or **above** it (small gap, larger minimum step).

The path enters the model in three ways:

- On every representative day, renewable generation (existing renewable generators plus renewable technologies) plus an RE-shortfall slack must cover \(\tau_y\) × day demand. The slack costs `FRE_penetration_loss` per MWh. Battery discharge does not count as renewable.
- A yearly RE-share variable, bounded by the lowest daily share, must reach \(\tau_y\). This uses the fixed internal slack penalty.
- The year-to-year change of that RE share must lie between `MIN_ANNUAL_INCREMENT` and `MAX_ANNUAL_INCREMENT`, starting from \(\rho^{init}\).

With several systems, each system has its own RE share and starting value.

In `myopic` mode, each year is a one-year model whose target is that year's \(\tau_y\) on the same path, computed over the whole horizon from \(\rho^{init}\) to `TARGET_RE_PENETRATION` with the same clamped steps. The increment bounds start from the RE share reached the year before: this year's share must rise by between `MIN_ANNUAL_INCREMENT` and `MAX_ANNUAL_INCREMENT` (shortfalls are paid through `FRE_penetration_loss`).

```yaml
systems:
  island:
    TARGET_RE_PENETRATION: 0.8
    MIN_ANNUAL_INCREMENT: 0.01
    MAX_ANNUAL_INCREMENT: 0.10
    discount_rate: 0.05
    life_extension_cost_factor: 0.2
    MAX_ANNUAL_SYSTEM_COST: 20000000000.0
    reserve_margin: 1.15
    penalties:
      FRE_penetration_loss: 100.0
```


### Capacity Adequacy

For each node and year, firm capacity must cover the year's peak demand × `reserve_margin` (default 1.15). Firm capacity counts existing renewable generators at their **capacity credit** (the minimum of their availability profile, so solar contributes almost nothing) and dispatchable generators at full capacity, each times its `risk_coefficient`, plus life-extended capacity, technology investments (renewables also at their capacity credit), and the power of existing and new batteries. Net imports from neighbouring nodes also count, up to the transfer capacity between them (existing lines plus transfer investment), so a node can rely on a neighbour's surplus. A penalised slack keeps the model feasible.


### CO₂

The representative-day operating cost charges `co2_cost` ($/t) times the emission factor of each unit's fuel (`fuels.<name>.emission_factor`, t per MWh of fuel) divided by the unit's `eff_at_rated`, on generator and technology output, as the dispatch does. A carbon price therefore steers investment towards lower-emission units.

When `co2_budget` is enabled, the master also holds each year's emissions within the annual budget, as the dispatch does: the emissions of the representative days, scaled to the year like their cost, may exceed it only at `penalties.co2_budget_violation` per tonne. In a multi-system run each system has its own budget and penalty over its own nodes.

### Converters, link flow costs and start-ups

AC/DC and frequency converters carry power between their nodes in the representative days, within their rated power plus what the master builds. A converter with `invest_max_power` > 0 can be expanded at `invest_cost` ($/MW, plus `fixed_cost` $/MW/year); the MW built are added to its `rated_power_mva` in the dispatch from the year they are built. The converters' `variable_cost` is charged on the power they carry.

An inter-system link with `cost_per_mw_km` charges `distance_km` × `cost_per_mw_km` per MWh it carries in the master's representative days.

With `use_uc_in_dispatch: true` the master also models start-ups, as a linear relaxation: each thermal or hydro unit with a start-up cost or a minimum power has an online capacity between 0 and its capacity, its output stays between `min_power` × that capacity and the capacity, and bringing the whole unit online costs one `start_up_cost`. Minimum up and down times are left to the dispatch. With economic dispatch (the default) neither the dispatch nor the master charges start-ups.


---


## Configuration

```yaml
master_problem:
  planning_mode: perfect_foresight   # perfect_foresight | myopic
  solver_method: monolithic          # monolithic | benders
  representative_days: 5
  min_day_separation: 5              # days
  use_tsam: false
  tsam_num_periods: 10
  tsam_method: kmedoids              # kmedoids | kmeans
  tsam_inter_period_linking: true
  use_uc_in_dispatch: false
  stochastic: false
  benders_max_iterations: 50
  benders_tolerance: 0.0001
  mga:
    enabled: false
    method: mga                      # mga | spores
    objectives: []                   # SPORES only
    num_alternatives: 10             # MGA only
    slack_fraction: 0.05
    investment_threshold: 0.1
temporal:
  investment_resolution: 8760        # hours per investment period
```

### Key Parameters

| Parameter | Default | Effect |
|-----------|---------|--------|
| `planning_mode` | `perfect_foresight` | `perfect_foresight` or `myopic` (see [Planning Mode](#planning-mode)) |
| `solver_method` | `monolithic` | `monolithic` or `benders` |
| `representative_days` | 5 | Days per year (peak-based selection). Ignored when `use_tsam: true` |
| `min_day_separation` | 5 | Minimum distance, in **days**, between selected days. Ignored when `use_tsam: true` |
| `use_tsam` | false | Cluster days instead of picking peak days |
| `tsam_num_periods` | 10 | Number of clustered representative days per year (2 to 365) |
| `tsam_method` | `kmedoids` | `kmedoids` or `kmeans` |
| `tsam_inter_period_linking` | true | Chain storage state of charge across clustered days |
| `use_uc_in_dispatch` | false | Run the operational windows of a development run as unit commitment (binary on/off, start-up costs, minimum up/down times) instead of economic dispatch. Slower, more realistic operating cost. The master then models start-ups too, as a linear relaxation |
| `stochastic` | false | Scenario-weighted objective (see [Stochastic Mode](#stochastic-mode)) |
| `benders_max_iterations` | 50 | Benders only |
| `benders_tolerance` | 1e-4 | Benders only: relative gap for convergence |
| `mga.enabled` | false | Generate near-optimal alternatives |
| `mga.method` | `mga` | `mga` runs the HSJ loop \(K\) times; `spores` solves one alternative per declared objective |
| `mga.slack_fraction` | 0.05 | Accept alternatives up to 5 % more expensive than the optimum (0 to 0.5) |
| `mga.num_alternatives` | 10 | MGA only (1 to 100) |
| `mga.objectives` | `[]` | SPORES objective menu, see [SporesObjective](../api/config-schema.md#sporesobjective). Must be non-empty for SPORES and empty for MGA |

The system-level keys that shape the plan (`TARGET_RE_PENETRATION`, `MIN_ANNUAL_INCREMENT`, `MAX_ANNUAL_INCREMENT`, `discount_rate`, `life_extension_cost_factor`, `MAX_ANNUAL_SYSTEM_COST`, `reserve_margin`) are described in the [Configuration guide](configuration.md); the NPV-based retirement keys [above](#npv-based-retirement) and in the [Configuration Reference](../reference/config-reference.md).


### Demand Seen by the Master

The master reads the same demand file as operational dispatch, one 8,760-hour block per year (see [Demand Data](demand-data.md)). If the file holds fewer years than the horizon, the first year is repeated. The master uses these values unchanged (plus the EV charging demand when EVs are defined), in both planning modes, so it plans for the same demand the dispatch serves.


---


## Interpreting Results

The master's decisions show up in the yearly results that operational dispatch writes to the HDF5 file:

- Technology investments become extra units named `inv_<technology key>` (display name "Investment &lt;name&gt;"). They appear in each year's generation and storage results alongside the existing units.
- Each year group `detailed_results/year_<YYYY>_threshold_0` carries the attributes `master_re_target` (the master's RE path for that year) and `renewable_penetration` (what dispatch achieved).
- In `perfect_foresight` mode with the monolithic solver and without MGA, the master LP is written to `<output>/logs/master_problem_debug.lp` for inspection.

See [Results Export](results-export.md) and the [HDF5 Output Schema](../reference/hdf5-output-schema.md) for the file layout.


### What to Look For

| Indicator | Healthy | Problematic |
|-----------|---------|-------------|
| Investment timing | Spread across years | Everything in year 1: check costs, `discount_rate` and the RE path |
| RE share vs. `master_re_target` | Close to the path | Far below: `FRE_penetration_loss` may be too low, or `invest_max_power` too tight |
| Load shedding in dispatch | Zero or minimal | Significant: more representative days, or more candidate capacity |
| Old units never retiring | Expected only if extension is cheap | Check existing units' `invest_cost` (life extension is free when it is 0) |
| Solve time | Seconds to minutes | Hours: fewer representative days or a coarser `resolution_hours` |


---


## Connection to Operational Dispatch

For each year of the horizon:

1. The master's cumulative capacity for that year (existing units after retirement, degradation and life extension, plus investments) replaces the units' `rated_power`.
2. Technology and battery-technology investments are added as `inv_<key>` units at the buses where they were built. Transmission investments are added to line capacity.
3. Operational dispatch solves the year in rolling-horizon windows as economic dispatch, or as unit commitment with `use_uc_in_dispatch: true`. See the [Configuration guide](configuration.md) for `temporal` settings.
4. The master's RE share for the year becomes the dispatch RE target, capped at what the installed capacity can deliver. A year whose share on the path is 0 gets no RE constraint. (Without a master, in `economic_dispatch` and `unit_commitment` runs, the dispatch applies no RE target at all.)

The representative-day cost is an estimate, so dispatch costs differ from it. If dispatch sheds load that the master did not anticipate, increase `representative_days` or switch to TSAM.


---


## MGA and SPORES

When `mga.enabled` is `true` (perfect foresight only), the solver first finds the cost-optimal solution $C^*$ and adds the constraint $Z \leq C^* \times (1 + \text{slack\_fraction})$. What it does next depends on `mga.method`:

- **`method: mga`** (default): runs the **Hop-Skip-Jump (HSJ)** loop `num_alternatives` times. Each iteration maximises a single diversity objective weighted by a frequency score that penalises investment variables seen in earlier alternatives.
- **`method: spores`**: runs the [SPORES](../api/config-schema.md#sporesobjective) sweep, solving one alternative per declared objective (`min_total_build`, `max_tech_equity`, `max_regional_equity`, `evolutionary_dist`). The alternative count equals `len(objectives)`; `num_alternatives` is ignored.

MGA builds its own deterministic monolithic model, so `solver_method`, `stochastic` and investment custom constraints do not apply to it. Alternative 0 is always the cost-optimal solution and is the one used for operational dispatch. Results are exported under `/mga/` in the system HDF5 file, with `attrs["method"]` at the root and a per-alternative `attrs["objective"]` carrying the objective tag.

Typical use cases:

- **Identifying robust decisions**: technologies that appear in every alternative are robust; those that vary are interchangeable.
- **Stakeholder engagement**: presenting a range of options rather than a single "optimal" answer.
- **Targeted policy questions** (SPORES only): when you need *named* alternatives ("smallest portfolio", "spatially-spread plan", "maximally different from the optimum").

For the LP formulation see [Capacity Expansion § 15](../formulation/capacity-expansion.md#15-mgaspores-near-optimal-alternative-exploration); for a step-by-step tutorial see [Near-Optimal Alternatives](../tutorials/mga.md).


---


## Stochastic Mode

`stochastic: true` takes effect only when the first system defines `stochastic_scenarios`. Their probabilities must sum to 1 (the loader rejects a sum that is off by more than 0.01). With no scenarios, the run logs a warning and falls back to the deterministic master. Each scenario is a set of scalar multipliers on investment costs (`invest_cost_renewables`, `invest_cost_conventional`, `invest_cost_storage`, `bat_invest_cost_power`, `bat_invest_cost_capacity`, `invest_cost_transmission`), decommissioning costs (`gen_decommissioning_cost`, `bat_decommissioning_cost`), operating costs (`fuel_cost`, `fuel_price_growth`, `gen_fixed_cost`, `maintenance_cost`, `carbon_price`), penalties (`penalty_loss_of_load`, `penalty_loss_of_reserve`, `penalty_curtailment`, `penalty_fre_penetration`, `penalty_co2_budget_violation`), battery maintenance alone (`bat_maintenance_cost`), start-ups (`gen_start_up_cost`), converters (`converter_invest_cost`, `converter_variable_cost`), the flow cost of inter-system links (`transmission_cost_per_mw_km`), the fuel chain and electrolyzers when primary energy is on (`fuel_import_cost`, `fuel_transport_cost`, `fuel_storage_invest_cost`, `penalty_loss_of_fuel_supply`, `electrolyzer_invest_cost`, `electrolyzer_fixed_cost`, `electrolyzer_variable_cost`, `electrolyzer_water_cost`), `demand_growth` and `discount_rate`; see the [ScenarioMultipliers reference](../reference/config-reference.md#scenariomultipliers). A multiplier other than 1 whose term this configuration lacks (`gen_start_up_cost` without `use_uc_in_dispatch`, the fuel and electrolyzer keys with primary energy off, `transmission_cost_per_mw_km` with no line or electrical inter-system link carrying a `cost_per_mw_km`) has no effect; the loader logs a warning for each.

It is a two-stage problem. The investment plan is common to all scenarios. Each scenario has its own representative-day dispatch, built on its own demand (`demand_growth` is an annual factor: 1.02 = +2 %/year) and operating costs. The objective is the probability-weighted cost over the scenarios, each with its own investment-cost multipliers and discount rate. The master grows roughly in proportion to the number of scenarios. The operational dispatch that follows runs on the base case. Stochastic mode needs `solver_method: monolithic`. See [Stochastic Programming](../formulation/stochastic-programming.md) and the [Stochastic Tutorial](../tutorials/stochastic.md).


---


## Performance Tips

| Issue | Solution |
|-------|----------|
| Solve time too long | Reduce `representative_days` (for example from 10 to 5) |
| Memory exceeded | Shorten the horizon, coarsen `temporal.resolution_hours`, or use longer investment periods (`temporal.investment_resolution`) |
| Too many investment candidates | Set `invest_max_power` to 0 at nodes where a technology cannot be built |
| Solver numerical issues | Reduce large penalty coefficients (for example `FRE_penetration_loss`, `LOSS_OF_LOAD`) |
| Inaccurate operational cost estimates | Increase `representative_days` to 7 to 10, or use TSAM |
| Investments all in year 1 | Check that `discount_rate` and technology costs are realistic |
| No investments despite need | Check that `invest_max_power > 0` for the candidate technologies and that their costs are not unrealistically high |


---


## Further Reading

- [Mathematical Formulation](../formulation/capacity-expansion.md): constraint-by-constraint specification
- [Configuration guide](configuration.md) and [Configuration Reference](../reference/config-reference.md): all YAML fields
- [Stochastic Tutorial](../tutorials/stochastic.md): scenario-based planning
- [MGA/SPORES Formulation](../formulation/capacity-expansion.md#15-mgaspores-near-optimal-alternative-exploration): mathematical details of near-optimal exploration
- [Solver Guide](solver-guide.md): solver selection and performance tuning
