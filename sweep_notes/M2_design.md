# M2 design: fuel balance and electrolyzers in the master (#89 part 2)

Status: design only. It needs choices only the user can make (section 6), so
nothing is implemented yet. Base: `fix/bug-sweep` c2acacc.

## 1. Where things stand today

- The master has no fuel model and no electrolyzer
  (`master_problem.jl:410`, `docs/formulation/capacity-expansion.md:434`).
  Fuel-burning units and candidate technologies pay their flat `fuel_cost`
  in $/MWh_e (`day_operational_cost`, `master_problem.jl:2382-2420`).
- The dispatch builds the fuel chain in the same JuMP model
  (`primary_energy.jl`, decisions #103/#113): one hourly tank per (fuel,
  node), period supply limits, route shipments with transit delay and
  losses, non-electric (NE) demand, shortfall and excess slacks at
  `loss_of_fuel_supply`, tank drawdown valued at the fuel price. Units in
  `generator_fuel_map` burn tank fuel and their `fuel_cost` is removed from
  the objective (`couple_primary_energy_to_power_system!`,
  `primary_energy.jl:1138`). Units with a cost curve keep the curve.
- The dispatch also builds fuel storage and transport, myopically, once per
  rolling window: annuity × the window's share of the year
  (`primary_energy.jl:333`, `:1000-1040`). The runner always passes
  `investment_from_master=False` (`runner.py:6011`), and built capacity
  carries over in `state.primary_energy_capacities` (`runner.py:2403`).
- Dispatch electrolyzers: a bus load bounded by `rated_power`, with ramps
  (`power_system.jl:3365`). The variable cost and the fixed cost
  ($/MW/h × window hours) are charged × Δt (`power_system.jl:1131`).
  `water_cost`, `invest_cost`, `invest_max_power` and `life_time` are not
  used anywhere. With primary energy the hydrogen goes into the node's
  `Hydrogen` tank (`couple_electrolyzers_to_hydrogen_tank!`).
- Keys dropped with a warning today: `fuel_import_cost`,
  `fuel_storage_invest_cost`, `fuel_transport_cost`,
  `penalty_loss_of_fuel_supply`, `electrolyzer_invest_cost`,
  `electrolyzer_fixed_cost`, `electrolyzer_variable_cost`,
  `electrolyzer_water_cost` (`config/schema.py:1148-1157`).

## 2. Master fuel block: reuse the dispatch's builders

The master already reuses the dispatch's builders for each representative
day, with capacities passed in as expressions of the investment variables
(`add_day_operational_constraints!`). The fuel block works the same way:
for each (scenario,) year and representative day (or TSAM period), build a
`PrimaryEnergyInput` for that day and call the `primary_energy.jl` builders
on the master model. Master and dispatch then share the physics and the
cost code, so they stay consistent by construction rather than through a
second formulation.

### 2.1 When it is on
When `enable_primary_energy` is on and the (merged) master system has
primary-energy sources or fuel entry points. That is the same condition the
dispatch uses (`runner.py:5906`). Otherwise the master works as it does today.

### 2.2 Granularity
- **Per fuel, per node, per time step of each representative day/TSAM period, per year.**
  These are the same index sets as the dispatch, with node = system node
  (the master is node-level; PE lists are per node in both models).
- **Supply limit per day**: `annual availability × 24/8760 × disruption factor`.
  That is the dispatch's period limit for its default 24 h period. Supply
  above it is allowed on the excess slack at `loss_of_fuel_supply`, as in
  the dispatch. Disruption windows are matched against the day's absolute
  horizon hours: `(year − base_year) × 8760 + (start_hour − 1) × Δt`, as in
  decision #41.
- **Tank**: the same hourly balance. Because representative days are not
  chronological, the level is cyclic within the day: a free start level
  equals the end level, and there is no fixed initial level. The drawdown
  valuation is then zero, as it should be. With
  `tsam_inter_period_linking`, the tank level is chained across the TSAM
  periods in chronological order, like battery SoC and reservoirs.
- **Transport**: a daily route flow spread evenly over the day's steps,
  net of losses. There is no transit delay, because a delay has no meaning
  inside an isolated representative day. Capacity per day is
  `(base + built) × 1 day`.
- **NE demand**: annual × growth^(year − base) × the monthly factor of the
  day's month × 24/8760, with a per-sector shortfall at
  `non_electric_demand_loss`.

### 2.3 Coupling to power
- Fuel-mapped existing units: `Σ_b gen_output × Δt ≤ fuel_for_power × MWh_e/unit`
  (the same builder). Their flat `fuel_cost` is dropped from the day cost
  unless they have a cost curve, which is the dispatch's rule.
- Candidate technologies: the fuel map gets a technology part
  (`tech index → (fuel, MWh_e/unit)`, with the same
  `mean(eff_at_rated) × energy_content` the dispatch uses for the virtual
  generators that technologies become). It applies to `tech_output[tc,b,t]`,
  and the same fuel-cost rule follows. This touches the tech cost code
  that M3 is changing (tech cost curves), so the two need coordinating.
- Electrolyzers: `ps_vars.electrolyzer_power[e,b,t]` is created in
  `create_day_ps_vars!`. The master's power-balance builders already
  subtract `electrolyzer_load` (`transmission_dc.jl:515`, PB-1).
  - Bound: existing `rated_power[b]` + Σ builds up to that year. Ramps
    follow `add_electrolyzer_dispatch_constraints!`, generalised to take a
    capacity expression.
  - Hydrogen goes into the `Hydrogen` tank through the same
    `couple_electrolyzers_to_hydrogen_tank!`.
  - Without a `Hydrogen` fuel the electrolyzer is only a load, as in the
    dispatch.

### 2.4 Investment variables (master, per year)
| Asset | Variable | Bound | Cost (overnight, discounted at build year, like other master assets) |
|---|---|---|---|
| Fuel storage | `pe_storage_inv[y][fuel][node]` | Σ_y ≤ `storage_capacity × storage_expansion_limit` − capacity already built | `storage_investment_cost` × units |
| Fuel transport | `pe_transport_inv[y][fuel][route]` | Σ_y ≤ `route capacity × transport_expansion_limit` − capacity already built | `transport_investment_cost × distance_km` × units/day |
| Electrolyzer | `elz_inv[y][e][bus]` | Σ_y ≤ `invest_max_power[b]` | `invest_cost[b]` × MW |

Built fuel capacity stays in service for the rest of the horizon. The
dispatch never retires fuel capacity either. Built electrolyzers retire at
`life_time`. Existing electrolyzers keep the dispatch's convention: they
have no age-based retirement.

### 2.5 Costs and units: master vs dispatch
PE quantities are fuel units per step, which are already amounts. The day
scaling for them is `days_in_year / rep_days` (TSAM: the period weight),
without the ×Δt that `calculate_day_operational_cost` applies to MW terms.
They are therefore added beside the scaled MW cost, not inside
`day_operational_cost`. M1 needs the same convention for CO2 budget
emissions if it counts them in tonnes.

| Term | Dispatch (per window) | Master (per day, before scaling) | Scaling |
|---|---|---|---|
| Fuel bought | supply × (price_base·(1+g)^(y−base) + import_cost[n]) | same | days/rep_days |
| Tank drawdown | (level₀ − level_end) × price | 0 (cyclic) or chained | – |
| Route transport | flow × transport_cost × km | same | days/rep_days |
| Fuel shortfall / excess | × loss_of_fuel_supply | same | days/rep_days |
| NE shortfall | × non_electric_demand_loss | same | days/rep_days |
| Electrolyzer variable | Δt × variable_cost × P | same | days/rep_days |
| Electrolyzer water | **missing** → add: Δt × water_cost × kg/MWh × P | same | days/rep_days |
| Electrolyzer fixed | Δt × T × fixed_cost × rated ($/MW/h) | 8760 × fixed_cost × (rated + built), per year | annual, undiscounted inside the year |
| Fuel storage/transport build | annuity × window fraction | overnight (master convention) | discount at build year |
| Electrolyzer build | – (no dispatch investment) | overnight | discount at build year |

Required dispatch change: charge `water_cost` (×Δt × kg H2). Without it the
two models do not charge the same costs. This overlaps D1, which fills
`pe_electrolyzer_cost` in the same block.

### 2.6 Stochastic keys (ScenarioMultipliers, Python + Julia)
`scenario_master_input` scales the day's fuel input. Investment keys go
through `build_investment_cost_expression` / `build_annual_asset_cost`.

| Key | Scales |
|---|---|
| `fuel_import_cost` | `import_cost[n]` |
| `penalty_loss_of_fuel_supply` | `loss_of_fuel_supply` (shortfall and excess) |
| `fuel_storage_invest_cost` | `storage_investment_cost` |
| `fuel_transport_cost` | route `transport_cost` **and** `transport_investment_cost` (see D-5) |
| `electrolyzer_invest_cost` | electrolyzer `invest_cost` |
| `electrolyzer_fixed_cost` | electrolyzer `fixed_cost` |
| `electrolyzer_variable_cost` | electrolyzer `variable_cost` |
| `electrolyzer_water_cost` | electrolyzer `water_cost` |
| existing `fuel_cost`, `fuel_price_growth` | also `price_base`. Otherwise they would stop affecting fuel-coupled units, whose `fuel_cost` is dropped. |

The keys are re-added to the Studio catalog (`stochastic_form.py`), the
serializer, `converters.py`, the schema (removed from the drop list), the 5
translation files and the docs, on lines kept separate from M1's.

## 3. How master decisions reach the dispatch (Python)
- `MasterProblemAdapter` builds the master fuel input from the same code
  path as `PrimaryEnergyAdapter._create_julia_input`, refactored into a
  shared helper, and from the same sources
  (`_build_pe_sources_from_entries`, NE demand growth and seasonal
  factors, routes, infrastructure, penalties, `scale_cost`).
- Perfect foresight: the master result gains per-year cumulative
  `pe_storage[fuel][node]`, `pe_transport[fuel][route]` and
  `electrolyzer[e][bus]`. For year y the dispatch gets:
  - the PE capacities as `cumulative_capacities`;
  - the electrolyzer capacity added to `ElectrolyzerConfig.rated_power`
    (`PowerSystemAdapter`, `adapters.py:1785`).
- Myopic: each annual master starts from the capacity built in earlier
  years (`state.primary_energy_capacities`, electrolyzer builds), and adds
  its own.
- `investment_from_master` is set according to D-1.
- Investments are reported like the other master builds (investment keys
  and HDF5 master group).

## 4. Validation tests
- Julia: a 1-node system with a gas unit, a `Gas` source (price, import
  cost, a supply limit that binds), NE demand, one route, an electrolyzer
  and a `Hydrogen` tank feeding an H2 unit. Fix the master investments,
  take one representative day, run the dispatch on the same 24 h with the
  same capacities, and check that every PE cost bucket and the electrolyzer
  cost are equal (master day value before scaling = dispatch window
  value), with Δt = 1 and Δt = 2.
- Master investment test: a binding dispatch-rate limit or NE demand makes
  the master build storage/transport/electrolyzer, and doubling each
  multiplier key changes the built amount or the cost.
- Python: the handoff test checks that master-built capacities appear in
  the dispatch PE input and electrolyzer rating for the right year, in
  perfect-foresight and myopic runs.

## 5. Size and effort
- The master gains about `(6 + sectors) × fuels × nodes + fuel units × nodes`
  variables per step per representative day per year (per scenario).
  This only applies to runs with primary energy enabled.
- Code touched:
  - Julia: `primary_energy.jl` (capacity expressions, cyclic/chained tank,
    no internal investment); `master_problem.jl` (variables, day hook in
    the representative-day, TSAM and stochastic paths, costs, extraction);
    `types.jl` (MasterProblemInput field, ScenarioMultipliers);
    `power_system.jl` (water cost).
  - Python: `adapters.py`, `runner.py` (both master paths, dispatch
    handoff), schema, Studio and translations, docs, tests.
- Rough size: 1,500–2,000 lines, plus Benders if D-2 (i) is chosen.

## 6. Choices for the user

**D-1 — Who builds fuel storage and transport in a development run.**
- (A) Master only. The dispatch PE investment is off whenever the master
  ran, and master-built capacity is its existing capacity. This is how
  generators and batteries work, and it is strictly consistent. Risk: the
  master's isolated representative days cannot see multi-day needs (ship
  transit delays, multi-day disruptions, seasonal NE demand). In those
  cases it under-builds storage, and the dispatch, which can no longer
  build, pays `loss_of_fuel_supply` (1000 $/unit) instead. Today's runs
  rely on the dispatch building that capacity.
- (B) Master builds, dispatch tops up. Master capacity is passed as
  existing capacity, the dispatch keeps its myopic investment for extra
  capacity only, and each model charges what it built. No decision is
  lost and nothing is charged twice, but the dispatch can still add
  capacity the master did not plan. *Recommended* (safer for fuel chains
  with long lead times). Under (B), myopic runs feed the top-ups into the
  next year's master.

**D-2 — Benders (`solver_method: benders`).** Benders rebuilds the day
model and hand-derives the cut coefficients for each capacity type
(`benders.jl:747`, `:1270`). (i) Extend the subproblems and cuts to fuel
storage, transport and electrolyzer capacities (several hundred lines more).
(ii) Run the monolithic master, with a warning, when the fuel block is
active. *Recommended: (ii) now, (i) later if needed.*

**D-3 — Consequence to confirm.** In every development run with primary
energy, the master stops charging fuel-coupled units their `fuel_cost` and
charges the fuel price through the balance instead, as the dispatch does.
Master investment results change wherever `fuel_cost` and the fuel prices
in the PE data disagree.

**D-4 — Master size.** The fuel block is on whenever primary energy is on
(*recommended*, same switch as the dispatch), or behind a new
`master_problem` flag.

**D-5 — `fuel_transport_cost`.** The Studio labels it "Fuel transport cost".
It can scale both the route operating cost and the transport investment
(*recommended*), or only one of them, with a new key
`fuel_transport_invest_cost` for the other.
