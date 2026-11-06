# Primary Energy Model

The primary energy model optimizes the fuel supply chain at multiple temporal scales, coupling fuel availability with power generation. It is implemented in `primary_energy.jl`.

## Overview

The primary energy module models:

- **Fuel supply** at import/production nodes with cost and availability limits
- **Fuel transport** between nodes with capacity, losses, and investment
- **Fuel storage** with dynamic inventory management
- **Non-electric demand** for fuels outside the electricity sector
- **Coupling** with generator fuel consumption in the power system
- **Environmental emissions** from fuel combustion and non-electric consumption

## Multi-Temporal Structure

The primary energy model operates at a coarser temporal resolution than the hourly power system dispatch:

| Scale | Resolution | Variables |
|-------|-----------|-----------|
| Hourly | 1 hour | Generator fuel consumption (from power system) |
| Primary period | 24-168 hours | Fuel supply, transport, storage |
| Investment period | 1 year | Infrastructure investment |

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

This multi-scale approach reduces the number of decision variables for slow-varying quantities (fuel supply scheduling, storage management) while preserving hourly resolution for the fast electrical dispatch coupling.

## Decision Variables

| Variable | Indices | Units | Description |
|----------|---------|-------|-------------|
| \(S_{f,n,p}\) | fuel, node, period | units/period | Fuel supply at node \(n\) |
| \(T_{f,r,p}\) | fuel, route, period | units/period | Fuel transport on route \(r\) |
| \(V^{start}_{f,n,p}\) | fuel, node, period | units | Fuel storage level at period start |
| \(V^{end}_{f,n,p}\) | fuel, node, period | units | Fuel storage level at period end |
| \(V^{hr}_{f,n,t}\) | fuel, node, hour | units | Hourly fuel storage level |
| \(I^{stor}_{f,n,ip}\) | fuel, node, inv. period | units | Storage capacity investment |
| \(I^{trans}_{f,r,ip}\) | fuel, route, inv. period | units/day | Transport capacity investment |
| \(W^{ne}_{f,s,n,p}\) | fuel, sector, node, period | units/period | Non-electric fuel consumption |
| \(W^{elec}_{g,n,t}\) | generator, node, hour | units/step | Fuel drawn for generator \(g\) (`hr_fuel_power_gen{g}`) |
| \(L^{hr}_{f,n,t}\) | fuel, node, hour | units/step | Hourly fuel shortfall (penalized) |
| \(L^{fuel}_{f,n,p}\) | fuel, node, period | units/period | Fuel supply shortfall (penalized) |
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

with \(S^{year}_{f,n}\) = `max_availability[n]` (units/year), \(|p|\) the hours in period \(p\) and \(H_{year}\) the hours of the calendar year (8760 or 8784). Proration uses the full year, so in rolling-horizon dispatch each window can only use its share of the annual availability. There are no seasonal supply profiles. Constraint `pp_max_supply_{fuel}_{n}_{p}`.

#### Source Disruption

A source can be configured with a disruption window — an hour range
\([h^{start}, h^{end})\) over which its availability is scaled to a fraction
\(a \in [0,1]\) (\(a = 0\) is a full cut). The per-period supply cap is reduced
by the share of the period's hours that fall inside the window:

\[
\bar{S}^{disrupted}_{f,n,p} = \bar{S}_{f,n,p} \cdot \big( 1 - \phi_{f,p}(1 - a) \big),
\qquad
\phi_{f,p} = \frac{\left|\{ h \in p : h^{start} \le h < h^{end} \}\right|}{|p|}
\tag{PE-1b}
\]

where \(\phi_{f,p}\) is the fraction of period \(p\)'s hours under the disruption. This models a supply-side shock (a terminal or pipeline down for several days). On its own it forces the consuming node onto its tank; together with the transport lead time (PE-3b) — the next shipment still in transit — and a finite tank, it reproduces real fuel-supply stress, with the penalised shortfall curtailing generation once the tank empties.

### Transport

Fuel transport is modeled through explicit routes connecting pairs of nodes. In a multi-system run, a `fuel_route` inter-system link adds two such routes (one per direction) between nodes of the two systems; see [Inter-System Links](../gui/inter-system-links.md#how-links-enter-the-model). Each route \(r\) has:

- A **from-node** and **to-node**
- A **distance** \(d_r\) (km)
- Per-fuel parameters: capacity, transport losses, and cost

Transport flow is bounded by existing capacity plus investment:

\[
T_{f,r,p} \leq \left(\bar{T}^{base}_{f,r} + C^{cumul}_{f,r} + I^{trans}_{f,r,ip}\right) \cdot \Delta_p^{days}
\tag{PE-2}
\]

where \(\bar{T}^{base}_{f,r}\) is the base daily capacity, \(C^{cumul}_{f,r}\) is cumulative capacity from prior investments, \(I^{trans}_{f,r,ip}\) is new investment in the current investment period, and \(\Delta_p^{days}\) is the number of days in primary period \(p\).

### Transport Losses

Fuel is lost during transport proportional to route distance:

\[
T^{received}_{f,r,p} = T_{f,r,p} \cdot (1 - \lambda^{trans}_f \cdot d_r / 100)
\tag{PE-3}
\]

where \(\lambda^{trans}_f\) is the loss fraction per 100 km (`losses_fraction` in the route's `fuel_params`) and \(d_r\) is the route distance in km. This captures pipeline leakage, evaporation during tanker transport, and similar physical losses.

### Transport Lead Time

A plant runs from a local tank (its node storage), which the generator draws down hour by hour — so **tank → generator is instantaneous**. The supply stress lives upstream: replenishing that tank from the source takes time. Each fuel carries a lead time \(\delta_f\) in **days per 100 km** of route distance; the per-route delay in primary periods is

\[
\tau_{f,r} = \mathrm{round}\!\left( \frac{\delta_f \cdot d_r / 100}{\Delta_p} \right)
\tag{PE-3b}
\]

where \(\Delta_p\) is the period length in days. Fuel dispatched on route \(r\) at period \(p\) then arrives at the destination at period \(p + \tau_{f,r}\), so the **received inflow** at node \(n\), period \(p\), uses the *shifted* transport variable:

\[
T^{received}_{f,r,p} = T_{f,r,\,p-\tau_{f,r}} \cdot (1 - \lambda^{trans}_f \cdot d_r / 100), \qquad p - \tau_{f,r} \ge 1
\]

Shipments dispatched before the window (\(p - \tau_{f,r} < 1\)) fall outside it and are not counted in that window (a small boundary effect that storage buffers; the rolling horizon carries the rest). With \(\delta_f = 0\) the inflow reduces to PE-3 (instantaneous transport, the default). Combined with a source disruption and a finite tank, a lead time reproduces real supply stress: a cut drains the tank while the next shipment is still in transit, and if the tank empties the penalised fuel shortfall curtails generation.

### Storage

Fuel storage provides temporal flexibility between supply scheduling and consumption. Storage is modeled at two scales:

**Periodic storage balance** (primary period granularity):

\[
V^{start}_{f,n,p} + S_{f,n,p} + \sum_{r: to(r)=n} T^{received}_{f,r,p} = V^{end}_{f,n,p} + W^{ne,total}_{f,n,p} + \sum_{r: from(r)=n} T_{f,r,p} + L^{fuel}_{f,n,p} + \Delta V^{hr}_{f,n,p}
\tag{PE-4}
\]

where:

- \(W^{ne,total}_{f,n,p} = \sum_s W^{ne}_{f,s,n,p}\) is total non-electric consumption across all sectors
- \(\Delta V^{hr}_{f,n,p}\) is the net hourly storage change (coupling variable linking periodic and hourly scales)
- \(L^{fuel}_{f,n,p}\) is the fuel supply shortfall slack

**Hourly storage dynamics** (used for electricity coupling):

\[
V^{hr}_{f,n,t+1} = V^{hr}_{f,n,t} + \text{storage\_in}_{f,n,t} \cdot \eta^{stor}_f - \text{storage\_out}_{f,n,t}
\tag{PE-4b}
\]

where \(\eta^{stor}_f\) is the storage efficiency (`efficiency` in `fuel_infrastructure.storage_facilities`, default 0.8), applied to inflows only; stored fuel does not decay. Constraint `hr_storage_balance_{fuel}_{n}_{t}`. For a fuel named `Hydrogen` the code can add an hourly production term, but no run supplies one (electrolyzers are not coupled to the primary-energy model; see [Electrolyzer](electrolyzer.md)).

**Storage continuity** between periods:

\[
V^{start}_{f,n,p} = V^{end}_{f,n,p-1} \quad \forall f, n, p \geq 2
\tag{PE-4c}
\]

For the first period (\(p=1\)), the periodic and hourly levels start from the configured initial level, or, in rolling-horizon dispatch after the first window, from the level the previous window left:

\[
V^{start}_{f,n,1} = V^{hr}_{f,n,1} = V^{init}_{f,n} \cdot \bar{V}_{f,n}
\]

with \(V^{init}_{f,n}\) = `initial_storage_level[n]` and \(\bar{V}_{f,n}\) = `storage_capacity[n]`. There is no cyclic (end = start) storage condition. In `development` mode the last period must end at least 10 % full, \(V^{end}_{f,n,P} \ge 0.1\,(\bar{V}_{f,n} + C^{cumul,stor}_{f,n} + I^{stor}_{f,n,ip})\) (`pp_storage_min_last_*`, fixed fraction).

### Storage Bounds

Storage levels are bounded by capacity (existing plus investment):

\[
V^{end}_{f,n,p} \leq \bar{V}_{f,n} + C^{cumul,stor}_{f,n} + I^{stor}_{f,n,ip} \quad \forall f, n, p
\tag{PE-5a}
\]

\[
V^{start}_{f,n,p} \leq \bar{V}_{f,n} + C^{cumul,stor}_{f,n} + I^{stor}_{f,n,ip} \quad \forall f, n, p
\tag{PE-5b}
\]

**Tank safety floor (operational reserve).** The plant tank cannot be drawn below a configured minimum level \(\underline{v}_f \in [0,1]\) (`min_storage_level`, a fraction of capacity) — an operational reserve that must be held back. It applies at nodes with base storage capacity, to every hourly level (against base capacity) and to the period ends (against total capacity \(\bar{V}^{tot}_{f,n} = \bar{V}_{f,n} + C^{cumul,stor}_{f,n} + I^{stor}_{f,n,ip}\)):

\[
V^{hr}_{f,n,t} \ge \underline{v}_f \cdot \bar{V}_{f,n}, \qquad
V^{hr}_{f,n,\text{end}(p)+1} \ge \underline{v}_f \cdot \bar{V}^{tot}_{f,n}, \qquad
V^{end}_{f,n,p} \ge \underline{v}_f \cdot \bar{V}^{tot}_{f,n}
\tag{PE-5c}
\]

With \(\underline{v}_f = 0\) the tank may be emptied. A positive floor reserves fuel even under stress: combined with a source disruption (PE-1b) and a transport lead time (PE-3b), the reserve cannot be tapped to cover the gap, so the penalised shortfall — and any consequent generation curtailment — appears sooner.

**Storage and transport investment limits** (in `development` and `economic_dispatch` modes, where investment variables exist):

\[
I^{stor}_{f,n,ip} \leq \bar{V}_{f,n} \cdot \xi^{stor}_f - C^{cumul}_{f,n}, \qquad I^{trans}_{f,r,ip} \leq \bar{T}^{base}_{f,r} \cdot \xi^{trans}_f - C^{cumul}_{f,r} \quad \forall f, n, r, ip
\tag{PE-5d}
\]

where \(\xi^{stor}_f\) and \(\xi^{trans}_f\) are the `expansion_limit` of the fuel's storage facility and pipeline entries in `fuel_infrastructure` (defaults 1.0 and 0.5), and \(C^{cumul}\) the capacity built in earlier windows (the limit is on the total expansion, not per window). What a window builds is added to \(C^{cumul}\) for the windows after it. The master problem has no fuel model, so fuel infrastructure is built here in `development` mode as well. Constraints `inv_storage_limit_{fuel}_{ip}_{n}` and `inv_transport_limit_{fuel}_{ip}_r{r}`.

### Hourly Fuel Use

Each hour, the fuel drawn from the node's tank must cover the fuel burnt by generators and the hourly non-electric consumption; any gap is an hourly shortfall \(L^{hr}_{f,n,t}\), penalized at `penalties.loss_of_fuel_supply`:

\[
\text{storage\_out}_{f,n,t} + L^{hr}_{f,n,t} \geq \sum_{g \in \mathcal{G}_f} W^{elec}_{g,n,t} + \sum_s W^{ne,hr}_{f,s,n,t}
\tag{PE-5e}
\]

(`hr_demand_coverage_{fuel}_{n}_{t}`). Further hourly constraints:

| Constraint | Form |
|------------|------|
| `hr_storage_max_out_*` | \(\text{storage\_out}_{f,n,t} \le V^{hr}_{f,n,t}\) |
| `hr_storage_dispatch_rate_*` | \(\text{storage\_out}_{f,n,t} \le \rho_f \cdot \bar{V}^{tot}_{f,n}\), only when `max_hourly_dispatch_rate_fraction_of_capacity` \(\rho_f \ge 0\) |
| `hr_storage_max_*` | \(V^{hr}_{f,n,t} \le \bar{V}^{tot}_{f,n}\) |
| `hr_ne_limit_*` | \(W^{ne,hr}_{f,s,n,t} \le D^{ne}_{f,s,n,p} / |p|\) (non-electric use spread evenly over the period) |

### Linking the Two Time Scales

| Constraint | Form |
|------------|------|
| `link_storage_start_*` | \(V^{hr}_{f,n,\text{first}(p)} + \text{slack}^{start}_{f,n,p} \ge V^{start}_{f,n,p}\) |
| `link_storage_end_*` | \(V^{hr}_{f,n,\text{last}(p)+1} + \text{slack}^{end}_{f,n,p} \ge V^{end}_{f,n,p}\) |
| `link_net_storage_*` | \(\Delta V^{hr}_{f,n,p} = \sum_{t \in p} (\text{storage\_out}_{f,n,t} - \eta^{stor}_f \, \text{storage\_in}_{f,n,t})\) |
| `link_supply_storage_in_*` | \(\sum_{t \in p} \text{storage\_in}_{f,n,t} = S_{f,n,p}\) |
| `link_ne_*` | \(\sum_{t \in p} W^{ne,hr}_{f,s,n,t} = W^{ne}_{f,s,n,p}\) |

The slacks are penalized at `penalties.coupling_slack_penalty`.

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

### Fixed / Investment Costs

Investment in storage and transport infrastructure (in `development` and `economic_dispatch` modes) is annualized with the capital recovery factor \(\text{CRF}(r, L) = r(1+r)^L / ((1+r)^L - 1)\) at the system discount rate and the facility lifetime (`lifetime`, defaults 30 years for storage, 20 for pipelines):

\[
C^{invest} = \phi^{win} \left( \sum_{f,n,ip} c^{stor,inv}_f \, \text{CRF}^{stor}_f \, I^{stor}_{f,n,ip} + \sum_{f,r,ip} c^{trans,inv}_f \, d_r \, \text{CRF}^{trans}_f \, I^{trans}_{f,r,ip} \right)
\]

with \(c^{stor,inv}_f\) and \(c^{trans,inv}_f\) the `investment_cost` of the fuel's `fuel_infrastructure` entries and \(\phi^{win}\) the window's share of the year (the annuity is annual; the dispatch's own investment costs are scaled the same way).

### Penalty Costs

Periodic and hourly shortfalls and excess supply are penalized at \(c^{pen}\) = `penalties.loss_of_fuel_supply`, unmet non-electric demand at \(c^{ne}\) = `penalties.non_electric_demand_loss`, and the linking slacks at \(c^{link}\) = `penalties.coupling_slack_penalty`:

\[
C^{penalty} = c^{pen} \left( \sum_{f,n,p} \left( L^{fuel}_{f,n,p} + X^{supply}_{f,n,p} \right) + \sum_{f,n,t} L^{hr}_{f,n,t} \right) + c^{ne} \sum_{f,s,n,p} U^{ne}_{f,s,n,p} + c^{link} \sum_{f,n,p} \left( \text{slack}^{start}_{f,n,p} + \text{slack}^{end}_{f,n,p} \right)
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
| Storage investment | Annualized infrastructure cost |
| Transport investment | Annualized route capacity cost |
| Shortfall and slack penalties | Unmet fuel use (periodic and hourly), excess supply, linking slacks |

## Power System Coupling

The primary energy model limits generator output by the fuel drawn for it, per generator, bus and time step:

\[
P_{g,b,t} \cdot \Delta t \leq W^{elec}_{g,n(b),t} \cdot \kappa_g \quad \forall g \in \mathcal{G}_f,\; b,\; t
\tag{PE-COUPLE}
\]

where \(W^{elec}_{g,n,t}\) is the fuel for generator \(g\) at node \(n\) (`hr_fuel_power_gen{g}`, units per time step), \(\kappa_g = \bar{\eta}_g \cdot e_f\) is the electricity per fuel unit (MWh\(_e\)/unit: mean `eff_at_rated` of the generator times `energy_content` of its fuel), \(n(b)\) is the node of bus \(b\) and \(\Delta t\) the time step. Constraint `pe_coupling_gen{g}_{b}_{t}`. The fuel then has to be drawn from the node's tank through PE-5e. Generators whose fuel is `Sun`, `Wind`, `Water`, `OTEC` or has no `energy_content` are not coupled.

The coupling is an inequality and is built per bus against the node-level fuel variable: when a generator sits on several buses of the same node, each bus's output is checked against the same fuel quantity.

Implemented in `couple_primary_energy_to_power_system!()`.

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

Of the source fields, `storage_investment_cost`, `transport_cost`, `transport_losses`, `max_storage_investment_per_node` and `max_transport_investment_per_arc` are required by the schema; the model takes the investment costs and limits from `fuel_infrastructure` and the route costs and losses from `fuel_transport_routes`.

---

## References

The multi-commodity network flow formulation for fuel supply chains follows standard LP transport models as described in Wood et al. [**[31]**](../reference/bibliography.md#ref31). The coupling between fuel logistics and generator dispatch is a feature shared with TIMES [**[16]**](../reference/bibliography.md#ref16), though ESFEX operates at chronological hourly resolution rather than time slices. IRENA cost benchmarks [**[48]**](../reference/bibliography.md#ref48) provide default fuel and infrastructure cost assumptions.

See the [full bibliography](../reference/bibliography.md) for complete citation details.
