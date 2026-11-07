# Primary Energy Model

The primary energy model optimizes the fuel supply chain at multiple temporal scales, coupling fuel availability with power generation. It is implemented in `primary_energy.jl`.

## Overview

The primary energy module models:

- **Fuel supply** at import/production nodes with cost and availability limits
- **Fuel transport** between nodes with capacity and losses
- **Fuel storage** with dynamic inventory management
- **Non-electric demand** for fuels outside the electricity sector
- **Coupling** with generator fuel consumption in the power system
- **Environmental emissions** from fuel combustion and non-electric consumption

Fuel storage and route capacity are built by the master problem, not here: the master runs this same model on its representative days and decides the builds, and each dispatch year receives them as existing capacity (see [Capacity Expansion](capacity-expansion.md#fuel-block)).

## Multi-Temporal Structure

The primary energy model operates at a coarser temporal resolution than the hourly power system dispatch:

| Scale | Resolution | Variables |
|-------|-----------|-----------|
| Hourly (time step) | 1 step of the dispatch | Tank levels, local supply into the tanks, fuel drawn for generators and non-electric use, shortfall |
| Primary period | `primary_energy_resolution` (24 h default) | Supply limits, route shipments, non-electric demand per sector |
| Investment period | 1 year | Not used: the dispatch builds no fuel infrastructure |

The temporal mapping is built by `create_temporal_mapping()`:

```
Hours:              1  2  3 ... 24  25 ... 48  49 ...
Primary periods:    [--- period 1 ---] [-- period 2 --] ...
Investment periods: [---------- year 1 ----------] ...
```

The `TemporalMapping` struct stores:

- `hour_to_primary_period`: Maps each hour to its enclosing primary period
- `primary_to_investment_period`: Maps each primary period to its investment period
- `hours_in_primary_period`: Lists all hours belonging to each primary period
- `num_primary_periods` / `num_investment_periods`: Period counts

Fuel is stored in one tank per fuel and node, balanced every time step; there is no separate periodic storage. The periods only carry the supply limits and the shipment decisions, which are spread over the period's steps.

## Decision Variables

| Variable | Indices | Units | Description |
|----------|---------|-------|-------------|
| \(S_{f,n,p}\) | fuel, node, period | units/period | Local fuel supply at node \(n\) (`pp_supply_*`) |
| \(s^{in}_{f,n,t}\) | fuel, node, hour | units/step | Local supply delivered into the tank (`hr_storage_in_*`) |
| \(T_{f,r,p}\) | fuel, route, period | units/period | Fuel shipped on route \(r\) |
| \(V^{hr}_{f,n,t}\) | fuel, node, hour | units | Tank level at the start of step \(t\) (`hr_storage_*`, steps 1 … H+1) |
| \(s^{out}_{f,n,t}\) | fuel, node, hour | units/step | Fuel drawn from the tank (`hr_storage_out_*`) |
| \(I^{stor}_{f,n}\) | fuel, node | units | Storage capacity the master builds (master only, see [Capacity Expansion](capacity-expansion.md#fuel-block)) |
| \(I^{trans}_{f,r}\) | fuel, route | units/day | Route capacity the master builds (master only) |
| \(W^{ne}_{f,s,n,p}\) | fuel, sector, node, period | units/period | Non-electric fuel consumption |
| \(W^{elec}_{g,n,t}\) | generator, node, hour | units/step | Fuel drawn for generator \(g\) (`hr_fuel_power_gen{g}`) |
| \(L^{hr}_{f,n,t}\) | fuel, node, hour | units/step | Fuel shortfall: use the tank cannot cover (penalized) |
| \(X^{supply}_{f,n,p}\) | fuel, node, period | units/period | Supply above the period's limit (penalized) |
| \(U^{ne}_{f,s,n,p}\) | fuel, sector, node, period | units/period | Unmet non-electric demand (penalized) |
| \(E^{fuel}_{f,n,t}\) | fuel, node, hour | tonnes CO2 | Emissions from non-electric consumption |

Built by `build_primary_energy_variables!()`.

## Fuel Supply Chain Modeling

The primary energy model captures three stages of the fuel supply chain:

### Extraction / Import (Supply)

Fuel enters the system at designated supply nodes (e.g., ports, refineries, gas wellheads). Each node has a maximum supply rate per primary period:

\[
S_{f,n,p} \leq \bar{S}_{f,n,p} + X^{supply}_{f,n,p}
\tag{PE-1}
\]

where \(X^{supply}_{f,n,p}\) is an excess-supply slack penalized at `penalties.loss_of_fuel_supply`, and \(\bar{S}_{f,n,p}\) is the annual availability prorated to the period:

\[
\bar{S}_{f,n,p} = S^{year}_{f,n} \cdot \frac{|p|}{H_{year}}
\]

with \(S^{year}_{f,n}\) = `max_availability[n]` (units/year), \(|p|\) the hours in period \(p\) (its steps × the step length, so 2 h steps get the same limit as 1 h steps) and \(H_{year}\) the hours of the year (always 8760, leap years included). Proration uses the full year, so in rolling-horizon dispatch each window can only use its share of the annual availability. There are no seasonal supply profiles. Constraint `pp_max_supply_{fuel}_{n}_{p}`.

#### Source Disruption

A source can be configured with a disruption window — an hour range
\([h^{start}, h^{end})\) in absolute horizon hours (hour 0 = Jan 1 of
`base_year`, 8760 h per modelled year, so it happens once) over which its
availability is scaled to a fraction \(a \in [0,1]\) (\(a = 0\) is a full
cut). The per-period supply cap is reduced by the share of the period's hours
that fall inside the window:

\[
\bar{S}^{disrupted}_{f,n,p} = \bar{S}_{f,n,p} \cdot \big( 1 - \phi_{f,p}(1 - a) \big),
\qquad
\phi_{f,p} = \frac{\left|\{ h \in p : h^{start} \le h < h^{end} \}\right|}{|p|}
\tag{PE-1b}
\]

where \(\phi_{f,p}\) is the fraction of period \(p\)'s hours under the disruption, each hour located in the horizon from the window's year and its start hour within the year. A scheduled interruption of type `fuel_source` ([Scheduled interruptions](../user-guide/scheduled-interruptions.md)) sets this window for the model windows it overlaps. This models a supply-side shock (a terminal or pipeline down for several days). On its own it forces the consuming node onto its tank; together with the transport lead time (PE-3b) — the next shipment still in transit — and a finite tank, it reproduces real fuel-supply stress, with the penalised shortfall curtailing generation once the tank empties.

### Transport

Fuel transport is modeled through explicit routes connecting pairs of nodes. In a multi-system run, a `fuel_route` inter-system link adds two such routes (one per direction) between nodes of the two systems; see [Inter-System Links](../gui/inter-system-links.md#how-links-enter-the-model). Each route \(r\) has:

- A **from-node** and **to-node**
- A **distance** \(d_r\) (km)
- Per-fuel parameters: capacity, transport losses, and cost

Transport flow is bounded by the route's capacity:

\[
T_{f,r,p} \leq \left(\bar{T}^{base}_{f,r} + C^{cumul}_{f,r} + I^{trans}_{f,r}\right) \cdot \Delta_p^{days}
\tag{PE-2}
\]

where \(\bar{T}^{base}_{f,r}\) is the base daily capacity, \(C^{cumul}_{f,r}\) is the capacity the master built and that is in service in the year (a constant in the dispatch), \(I^{trans}_{f,r}\) is the master's own build variable (only in the master's day model; 0 in the dispatch), and \(\Delta_p^{days}\) is the number of days in primary period \(p\) (its steps times the step length). Constraint `pp_transport_cap_{fuel}_r{r}_{p}`. A `fuel_route` [scheduled interruption](../user-guide/scheduled-interruptions.md) multiplies the right-hand side by the mean availability of the period's steps (shipments are decided per period).

### Transport Losses

Fuel is lost during transport proportional to route distance:

\[
T^{received}_{f,r,p} = T_{f,r,p} \cdot (1 - \lambda^{trans}_f \cdot d_r / 100)
\tag{PE-3}
\]

where \(\lambda^{trans}_f\) is the loss fraction per 100 km (`losses_fraction` in the route's `fuel_params`) and \(d_r\) is the route distance in km (the delivered share is floored at 0). This captures pipeline leakage, evaporation during tanker transport, and similar physical losses.

### Transport Lead Time

A plant runs from a local tank (its node storage), which the generator draws down hour by hour — so **tank → generator is instantaneous**. The supply stress lives upstream: replenishing that tank from the source takes time. Each fuel carries a lead time \(\delta_f\) in **days per 100 km** of route distance; the per-route delay in time steps is

\[
\tau_{f,r} = \mathrm{round}\!\left( \frac{24 \, \delta_f \cdot d_r / 100}{\Delta t} \right)
\tag{PE-3b}
\]

where \(\Delta t\) is the step length in hours. A period's shipment leaves its origin evenly over the period's \(|p|\) steps and reaches the destination \(\tau_{f,r}\) steps later, net of losses:

\[
\text{dep}_{f,n,t} = \sum_{r:\,from(r)=n} \frac{T_{f,r,p(t)}}{|p(t)|}, \qquad
\text{arr}_{f,n,t} = \sum_{r:\,to(r)=n,\; t-\tau_{f,r} \ge 1} (1 - \lambda^{trans}_f d_r / 100) \, \frac{T_{f,r,p(t-\tau_{f,r})}}{|p(t-\tau_{f,r})|}
\]

where \(p(t)\) is the period of step \(t\). In rolling-horizon dispatch, fuel that left in a window's committed steps and arrives after them (`in_transit_arrivals`) is carried to the next window with the tank levels: it arrives there in its scheduled step as a fixed term \(\text{arr}^{sched}_{f,n,t}\) added to \(\text{arr}_{f,n,t}\) (already net of losses, paid in the window that shipped it). Fuel shipped in a window's uncommitted look-ahead steps is decided again by the next window; fuel still in transit at the end of the year's last window arrives in the next year's first window. With \(\delta_f = 0\) the fuel arrives in the step it leaves (the default). Combined with a source disruption and a finite tank, a lead time reproduces real supply stress: a cut drains the tank while the next shipment is still in transit, and if the tank empties the penalised fuel shortfall curtails generation.

### Storage

Each fuel has one tank per node, balanced every time step. Local supply and arriving shipments go into the tank; generators, non-electric use and departing shipments draw from it:

\[
V^{hr}_{f,n,t+1} = V^{hr}_{f,n,t} + \eta^{stor}_f \left( s^{in}_{f,n,t} + \text{arr}_{f,n,t} \right) - s^{out}_{f,n,t} - \text{dep}_{f,n,t}
\tag{PE-4}
\]

where \(\eta^{stor}_f\) is the storage efficiency (`efficiency` in `fuel_infrastructure.storage_facilities`, default 0.8), applied to inflows only; stored fuel does not decay. Constraint `hr_storage_balance_{fuel}_{n}_{t}`. The tank of the fuel named `Hydrogen` also receives the hydrogen of the dispatch electrolyzers at its node, as another inflow (see [Power System Coupling](#power-system-coupling)).

The local supply of a period is delivered into the tank over its steps, freely within the period:

\[
\sum_{t \in p} s^{in}_{f,n,t} = S_{f,n,p}
\tag{PE-4a}
\]

(`link_supply_storage_in_{fuel}_{n}_{p}`). Fuel drawn in a step must cover what is burnt and consumed in that step; the gap, if any, is the shortfall (PE-5e).

The tank starts from the configured initial level or, in rolling-horizon dispatch after the first window, from the level the previous window left at its last committed step (the fuel then still in transit arrives as scheduled, PE-3b):

\[
V^{hr}_{f,n,1} = V^{init}_{f,n} \cdot \bar{V}_{f,n}
\]

with \(V^{init}_{f,n}\) = `initial_storage_level[n]` and \(\bar{V}_{f,n}\) = `storage_capacity[n]` (`hr_storage_initial_{fuel}_{n}`). There is no cyclic (end = start) storage condition. In `development` mode, for fuels with a `fuel_infrastructure` entry, the window must end with each tank at least 10 % full, \(V^{hr}_{f,n,H+1} \ge 0.1\,(\bar{V}_{f,n} + C^{cumul,stor}_{f,n} + I^{stor}_{f,n,ip})\) (`hr_storage_min_last_*`, fixed fraction).

### Storage Bounds

Tank levels are bounded by capacity (configured plus what the master built):

\[
V^{hr}_{f,n,t+1} \leq \bar{V}^{tot}_{f,n} = \bar{V}_{f,n} + C^{cumul,stor}_{f,n} + I^{stor}_{f,n} \quad \forall f, n, t
\tag{PE-5a}
\]

(`hr_storage_max_*`). At a node with no tank capacity at all (no base capacity and none built) the bound is 0: fuel only passes through, so what arrives or is supplied in a step must be used or shipped on in that step.

**Scheduled tank outages.** A `fuel_storage` [scheduled interruption](../user-guide/scheduled-interruptions.md) of availability \(a_{f,n,t} < 1\) locks the share \(1 - a\) of the fuel the tank held when the outage began (step \(s\)) and leaves only \(a\) of its capacity usable:

\[
(1 - a_{f,n,t})\, V^{hr}_{f,n,s} \le V^{hr}_{f,n,t+1} \le (1 - a_{f,n,t})\, V^{hr}_{f,n,s} + a_{f,n,t}\, \bar{V}^{tot}_{f,n}
\tag{PE-5d}
\]

(`hr_storage_outage_lo_*`, `hr_storage_outage_hi_*`). A full outage (\(a = 0\)) freezes the level: the node's fuel only passes through, as at a node without a tank. The minimum levels PE-5c and the development-mode 10 % end fill do not apply while the tank is out of service.

**Tank safety floor (operational reserve).** The plant tank cannot be drawn below a configured minimum level \(\underline{v}_f \in [0,1]\) (`min_storage_level`, a fraction of capacity) — an operational reserve that must be held back. It applies at nodes with base storage capacity, to the level after every step (against base capacity, `hr_storage_min_*`) and at the period ends (against total capacity \(\bar{V}^{tot}_{f,n}\), `hr_storage_min_final_*`):

\[
V^{hr}_{f,n,t+1} \ge \underline{v}_f \cdot \bar{V}_{f,n}, \qquad
V^{hr}_{f,n,\text{end}(p)+1} \ge \underline{v}_f \cdot \bar{V}^{tot}_{f,n}
\tag{PE-5c}
\]

With \(\underline{v}_f = 0\) the tank may be emptied. A positive floor reserves fuel even under stress: combined with a source disruption (PE-1b) and a transport lead time (PE-3b), the reserve cannot be tapped to cover the gap, so the penalised shortfall — and any consequent generation curtailment — appears sooner.

**Storage and transport expansion limits** (master problem; the dispatch builds nothing):

\[
\sum_y I^{stor}_{f,n,y} \leq \bar{V}_{f,n} \cdot \xi^{stor}_f - C^{cumul}_{f,n}, \qquad \sum_y I^{trans}_{f,r,y} \leq \bar{T}^{base}_{f,r} \cdot \xi^{trans}_f - C^{cumul}_{f,r} \quad \forall f, n, r
\tag{PE-5d}
\]

where \(\xi^{stor}_f\) and \(\xi^{trans}_f\) are the `expansion_limit` of the fuel's storage facility and pipeline entries in `fuel_infrastructure` (defaults 1.0 and 0.5), and \(C^{cumul}\) the capacity built before the master's first year (earlier years of a myopic run). The limit is on the total expansion over the horizon. In a run without a master (`economic_dispatch` or `unit_commitment` mode) no fuel infrastructure is built. Bounds of `fuel_storage_inv_y{y}_{fuel}` and `fuel_transport_inv_y{y}_{fuel}`, constraints `fuel_storage_inv_total_{fuel}_{n}` and `fuel_transport_inv_total_{fuel}_{r}`.

### Hourly Fuel Use

Each step, the fuel drawn from the node's tank must cover the fuel burnt by generators and the non-electric consumption of that step; any gap is a shortfall \(L^{hr}_{f,n,t}\), penalized at `penalties.loss_of_fuel_supply`:

\[
s^{out}_{f,n,t} + L^{hr}_{f,n,t} \geq \sum_{g \in \mathcal{G}_f} W^{elec}_{g,n,t} + \sum_s W^{ne,hr}_{f,s,n,t}
\tag{PE-5e}
\]

Fuel that arrives in a step can be used in that step: the tank level after the step (PE-4) cannot go below zero, which is the only availability limit. The shortfall is therefore a real deficit — fuel the tank does not hold.

(`hr_demand_coverage_{fuel}_{n}_{t}`). Further hourly constraints:

| Constraint | Form |
|------------|------|
| `hr_storage_dispatch_rate_*` | \(s^{out}_{f,n,t} \le \rho_f \, \Delta t \cdot \bar{V}^{tot}_{f,n}\), only when `max_hourly_dispatch_rate_fraction_of_capacity` \(\rho_f \ge 0\) (a fraction of the tank per hour, \(\Delta t\) the step length) and only at nodes with a tank (base or built capacity); a node without one draws what it holds |
| `hr_storage_max_*` | \(V^{hr}_{f,n,t+1} \le \bar{V}^{tot}_{f,n}\) (PE-5a) |
| `hr_ne_limit_*` | \(W^{ne,hr}_{f,s,n,t} \le D^{ne}_{f,s,n,p} / |p|\) (non-electric use spread evenly over the period) |

### Linking the Two Time Scales

| Constraint | Form |
|------------|------|
| `link_supply_storage_in_*` | \(\sum_{t \in p} s^{in}_{f,n,t} = S_{f,n,p}\) (PE-4a) |
| `link_ne_*` | \(\sum_{t \in p} W^{ne,hr}_{f,s,n,t} = W^{ne}_{f,s,n,p}\) |

Shipments are linked through the arrivals and departures of PE-3b. There are no periodic storage levels and no linking slacks.

## Non-Electric Fuel Demand

Non-electric demand represents fuel consumption outside the electricity sector, such as transportation, industrial heating, and cooking. It is specified per fuel, per sector, per node, and per primary period.

### Demand Satisfaction

\[
W^{ne}_{f,s,n,p} \leq D^{ne}_{f,s,n,p}
\tag{PE-6a}
\]

\[
W^{ne}_{f,s,n,p} + U^{ne}_{f,s,n,p} \geq D^{ne}_{f,s,n,p}
\tag{PE-6b}
\]

The first constraint caps consumption at the demand level. The second ensures demand is met, either through actual consumption or through the sector's shortfall (see below). Constraints `pp_ne_max_*` and `pp_ne_satisfy_*`.

The periodic demand is built from the annual demand per node, `non_electric_demand.<key>.demand[n]`:

\[
D^{ne}_{f,s,n,p} = D^{year}_{f,s,n} \cdot (1 + g_f)^{y - y_0} \cdot \frac{\sigma_{f,s,m(p)}}{\bar{\sigma}_{f,s}} \cdot \frac{|p| \, \Delta t}{H_{year}}
\]

where \(m(p)\) is the month of the period's mid-hour within the year (the window's position in the year is passed to the model), \(|p| \Delta t\) the period's length in hours and \(H_{year}\) the hours of the year. The sector \(s\) is the part of the entry's key before the first underscore (`transport_diesel` → `transport`).

- \(g_f\) is `non_electric_demand_growth[<fuel>]` (no growth if the fuel is not listed).
- \(\sigma_{f,s,m}\) are the 12 monthly values `seasonal_factors[<fuel>][<sector>]`, divided by their mean \(\bar{\sigma}\): monthly shares that sum to 1 and multipliers that average 1 give the same result, and the year's total is the annual demand. Without them the demand is even over the year.

Unmet demand is a per-sector slack \(U^{ne}_{f,s,n,p}\) (`pp_ne_short_*`), priced at `penalties.non_electric_demand_loss` per unit; PE-6b reads \(W^{ne} + U^{ne} \geq D^{ne}\).

### Sector Examples

| Sector | Fuel | Typical Demand |
|--------|------|---------------|
| Transport | Diesel, Gasoline | Vehicle fleet consumption |
| Industrial | Natural Gas, Coal | Process heat and feedstock |
| Residential | LPG, Kerosene | Cooking, water heating |
| Commercial | Diesel | Backup generators, heating |

## Fuel Cost Formulation

### Variable Costs

The variable cost of fuel supply is the fuel price, escalated to the simulated year, plus the node's import cost:

\[
C^{supply} = \sum_{f,n,p} \left( c^{base}_f (1 + g^{price}_f)^{y - y_0} + c^{imp}_{f,n} \right) S_{f,n,p}
\]

where \(c^{base}_f\) and \(g^{price}_f\) are `fuels.<fuel>.price_base` and `price_growth_rate`, and \(c^{imp}_{f,n}\) is `import_cost[n]` of the fuel's source ($/unit). Fuel taken from the tanks is valued at the same price, \(c^{base}_f (1 + g^{price}_f)^{y - y_0} \left( V^{hr}_{f,n,\text{first}} - V^{hr}_{f,n,\text{last}} \right)\) per node (the window's drawdown; a refill is a credit), so burning stored fuel is not free within a window. Both are reported as `pe_supply_cost`.

### Transport Costs

Transport costs are proportional to volume and distance:

\[
C^{transport} = \sum_{f,r,p} c^{trans}_{f,r} \cdot d_r \cdot T_{f,r,p}
\]

where \(c^{trans}_{f,r}\) is the transport cost per unit per km (`transport_cost` in the route's `fuel_params`).

### Investment Costs

The dispatch has no investment cost: the master builds the fuel infrastructure and charges it, like its other assets, as an overnight cost discounted to the build year:

\[
C^{invest} = \sum_{y} \delta_y \left( \sum_{f,n} c^{stor,inv}_f \, I^{stor}_{f,n,y} + \sum_{f,r} c^{trans,inv}_f \, d_r \, I^{trans}_{f,r,y} \right)
\]

with \(c^{stor,inv}_f\) and \(c^{trans,inv}_f\) the `investment_cost` of the fuel's `fuel_infrastructure` entries and \(\delta_y\) the master's discount factor. Built fuel capacity does not retire within the horizon.

### Penalty Costs

Fuel shortfalls and excess supply are penalized at \(c^{pen}\) = `penalties.loss_of_fuel_supply` and unmet non-electric demand at \(c^{ne}\) = `penalties.non_electric_demand_loss`:

\[
C^{penalty} = c^{pen} \left( \sum_{f,n,p} X^{supply}_{f,n,p} + \sum_{f,n,t} L^{hr}_{f,n,t} \right) + c^{ne} \sum_{f,s,n,p} U^{ne}_{f,s,n,p}
\]

When the block is coupled to the dispatch, the fuel a coupled generator burns is paid here (supply price); the dispatch then drops that generator's `fuel_cost` from its objective and cost breakdown, so the fuel is not charged twice. Generators with a cost curve keep it.

## Multi-Fuel Systems

ESFEX supports modeling multiple fuel types simultaneously. Each fuel is configured independently with its own:

- Supply nodes and limits
- Transport routes (with per-fuel parameters on shared routes)
- Storage infrastructure
- Energy content and emission factor
- Price structure

Fuels are coupled only through:

1. **Shared transport routes**: A single physical pipeline or road may carry multiple fuels, each with distinct capacity and loss parameters (via `fuel_params` on `TransportRoute`)
2. **Generator fuel assignment**: Each generator is assigned a single fuel type; the fuel consumption is determined by the generator's heat rate/efficiency
3. **Emission budgets**: The power-plant emissions of every fuel count against the CO2 budget of the plant's system (dispatch constraint CO2-2, one budget per system in a multi-system run). Non-electric emissions (below) are reported but not budgeted.

### Fuel Configuration Example (Multi-Fuel)

```yaml
fuels:
  Diesel:
    name: Diesel
    unit: tonne
    emission_factor: 0.267      # tCO2/MWh
    energy_content: 11.86       # MWh/tonne
    price_base: 800.0           # $/tonne
    price_growth_rate: 0.02
  Natural_Gas:
    name: Natural_Gas
    unit: tonne
    emission_factor: 0.202
    energy_content: 13.9        # MWh/tonne (LNG equivalent)
    price_base: 350.0
```

## Import / Export Fuel Flows

Fuel enters and leaves the modeled system through supply nodes. The model supports:

- **Import nodes**: Ports, border crossings, or pipeline entry points where fuel enters the system at a configured import cost
- **Domestic production nodes**: Local extraction sites (e.g., gas wells, biomass processing)
- **Transit flows**: Fuel transported through the system to other nodes, potentially serving both local demand and downstream export

There is no explicit "export" variable. Instead, fuel may be transported to border nodes where non-electric demand represents contractual export obligations. This approach keeps the formulation simple while allowing transit modeling.

## Environmental Emissions from Fuel

### Non-Electric Emissions

Emissions from non-electric fuel consumption are computed hourly:

\[
E^{fuel}_{f,n,t} = W^{ne,hr}_{f,n,t} \cdot \varepsilon_f \cdot \eta^{energy}_f
\tag{PE-EM-1}
\]

where:

- \(W^{ne,hr}_{f,n,t}\) is hourly non-electric consumption
- \(\varepsilon_f\) is the emission factor (tCO2 per MWh thermal or per physical unit)
- \(\eta^{energy}_f\) is the energy content (MWh/unit); set to 1 when the emission factor is already per physical unit

If `energy_content` is greater than zero, the full conversion applies (`hr_emissions_*`). Otherwise, the emission factor is assumed to be directly per physical unit (`hr_emissions_direct_*`):

\[
E^{fuel}_{f,n,t} = W^{ne,hr}_{f,n,t} \cdot \varepsilon_f
\tag{PE-EM-1b}
\]

### Total Primary Emissions

Total emissions across all fuels at each node and hour:

\[
E^{total}_{n,t} = \sum_f E^{fuel}_{f,n,t}
\tag{PE-EM-2}
\]

\(\varepsilon_f\) is `fuels.<fuel>.emission_factor` (t/MWh of fuel energy).

These emissions are tracked separately from electricity-sector CO2 emissions (which are computed in `power_system.jl`). The total system emissions for policy reporting combine both:

\[
E^{system} = \sum_{n,t} E^{total,elec}_{n,t} + \sum_{n,t} E^{total}_{n,t}
\]

### Fuel Availability

Fuel availability is limited only by the prorated annual availability per period (PE-1) and the disruption window (PE-1b). There are no annual supply caps across nodes, seasonal supply profiles or import-dependency limits.

## Objective Terms

\[
C^{PE} = C^{supply} + C^{transport} + C^{invest} + C^{penalty}
\tag{PE-OBJ}
\]

Expanded:

with the terms defined above. The PE terms are added to the operational dispatch objective when the primary-energy model is coupled to it.

| Term | Description |
|------|-------------|
| Supply cost | Fuel procurement at import nodes |
| Transport cost | Operating cost proportional to distance |
| Storage investment | Master only: overnight cost of the tanks it builds |
| Transport investment | Master only: overnight cost of the route capacity it builds |
| Shortfall penalties | Fuel shortfall, unmet non-electric demand, excess supply |

## Power System Coupling

The primary energy model limits generator output by the fuel drawn for it, per generator, node and time step, summed over the generator's buses at the node:

\[
\sum_{b:\, n(b) = n} P_{g,b,t} \cdot \Delta t \leq W^{elec}_{g,n,t} \cdot \kappa_g \quad \forall g \in \mathcal{G}_f,\; n,\; t
\tag{PE-COUPLE}
\]

where \(W^{elec}_{g,n,t}\) is the fuel for generator \(g\) at node \(n\) (`hr_fuel_power_gen{g}`, units per time step), \(\kappa_g = \bar{\eta}_g \cdot e_f\) is the electricity per fuel unit (MWh\(_e\)/unit: mean `eff_at_rated` of the generator times `energy_content` of its fuel), \(n(b)\) is the node of bus \(b\) and \(\Delta t\) the time step. Constraint `pe_coupling_gen{g}_n{n}_{t}`. The fuel then has to be drawn from the node's tank through PE-5e. Generators whose fuel is `Sun`, `Wind`, `Water`, `OTEC` or has no `energy_content` are not coupled. A generator at several buses of one node (for example the virtual unit of a technology built at two buses) burns the node's fuel once for its total output.

**Electrolyzers.** The hydrogen of every dispatch electrolyzer \(e\) at bus \(b\) flows into the `Hydrogen` tank of node \(n(b)\), as an inflow of PE-4 (scaled by \(\eta^{stor}\) like the others):

\[
\text{H}_{2,n,t} = \sum_{e} \sum_{b:\, n(b) = n} P^{elz}_{e,b,t} \cdot \Delta t \cdot \frac{1000\,(\eta^{rated}_e + \eta^{min}_e)/2}{e^{H_2}_e} \cdot \frac{1}{1000} \quad \text{[t per step]}
\tag{PE-H2}
\]

so the hydrogen tank is in tonnes (give the `Hydrogen` fuel an `energy_content` in MWh/t, about 33.3 at LHV). A hydrogen-fuelled unit then runs on electrolyzer hydrogen; without a primary-energy source named `Hydrogen` the electrolyzers stay a plain load. The fuel name must be exactly `Hydrogen`.

Implemented in `couple_primary_energy_to_power_system!()` (with `couple_electrolyzers_to_hydrogen_tank!()`).

## Configuration

The primary-energy settings are system keys. `primary_energy_sources` entries are keyed by fuel name (the same name as in `fuels` and in the generators' `fuel`). Lists are per node.

```yaml
enable_primary_energy: true

systems:
  my_system:
    fuels:
      Diesel:
        name: Diesel
        unit: tonne
        emission_factor: 0.267        # tCO2/MWh
        energy_content: 11.86         # MWh/tonne
        price_base: 800.0             # $/tonne

    primary_energy_sources:
      Diesel:
        name: Diesel
        unit: tonne
        max_availability: [50000.0, 0.0]    # tonnes/year per node
        import_cost: [20.0, 0.0]            # $/tonne per node
        storage_capacity: [5000.0, 1000.0]  # tonnes per node
        initial_storage_level: [0.5, 0.5]   # fraction of capacity
        min_storage_level: 0.1              # safety stock (fraction)
        storage_investment_cost: 100.0
        transport_cost: 0.1
        transport_losses: 0.005
        max_storage_investment_per_node: 10000.0
        max_transport_investment_per_arc: 500.0

    fuel_infrastructure:
      storage_facilities:
        tanks:
          fuel_params:
            Diesel:
              efficiency: 0.98
              investment_cost: 100.0          # $/tonne of capacity
              expansion_limit: 2.0            # x base capacity
              lifetime: 30
      transport_pipelines:
        trucks:
          fuel_params:
            Diesel:
              capacity: 200.0
              investment_cost: 50.0           # $/(tonne/day)/km
              expansion_limit: 0.5
              lifetime: 20

    fuel_transport_routes:
      - route_id: port_to_town
        from_node: 0
        to_node: 1
        distance_km: 150.0
        fuel_params:
          Diesel:
            capacity: 200.0                 # tonnes/day
            transport_cost: 0.1             # $/tonne/km
            losses_fraction: 0.005          # fraction per 100 km

    non_electric_demand:
      transport_diesel:
        fuel: Diesel
        unit: tonne
        demand: [12000, 3000]               # tonnes/year per node
```

Of the source fields, `storage_investment_cost`, `transport_cost`, `transport_losses`, `max_storage_investment_per_node` and `max_transport_investment_per_arc` are required by the schema; the model takes the investment costs and limits from `fuel_infrastructure` and the route costs and losses from `fuel_transport_routes`. The master problem uses the same `fuel_infrastructure` costs and limits when it builds fuel storage and route capacity (PE-5d).

---

## References

The multi-commodity network flow formulation for fuel supply chains follows standard LP transport models as described in Wood et al. [**[31]**](../reference/bibliography.md#ref31). The coupling between fuel logistics and generator dispatch is a feature shared with TIMES [**[16]**](../reference/bibliography.md#ref16), though ESFEX operates at chronological hourly resolution rather than time slices. IRENA cost benchmarks [**[48]**](../reference/bibliography.md#ref48) provide default fuel and infrastructure cost assumptions.

See the [full bibliography](../reference/bibliography.md) for complete citation details.
