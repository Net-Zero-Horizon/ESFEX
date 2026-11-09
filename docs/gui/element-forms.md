# Element Forms

Property form reference for all element types. Select an element in the Element Tree or on the map and its form opens in the Properties panel. Forms are made of grouped sections (the node form also has tabs). Edits are applied when a field loses focus or a value changes, and are reflected on the map and in the Element Tree. Every edit can be undone with **Edit > Undo** (`Ctrl+Z`).

Field names below are the English UI labels; the unit shown in the label is repeated in the Unit column.


---


## Electrical Infrastructure

### Node

A node is a geographic aggregation: it owns one or more buses and holds the demand and reserve data for that area. Equipment attaches to buses, and each bus belongs to a parent node. A node has no marker of its own on the map; it is added from the Element Tree (right-click **Nodes > Add Node**).

| Tab | Field | Unit | Description |
|-----|-------|------|-------------|
| **General** | Name | -- | Display name for the node (e.g., "Saint-Denis Substation") |
| | Centroid (geographic reference): Latitude / Longitude | degrees | Optional reference point (WGS84). **Place** picks it on the map; **View** centres the map on it. |
| | Static Reserve | MW | Minimum spinning reserve requirement of this node. The requirements of all the nodes of a system are added up and held in the free capacity of any reservable generator or battery of that system (in unit commitment, only committed units count), not only the ones at this node. It is capacity kept free, not energy generated. |
| | Dynamic Reserve | MW | Frequency-response reserve requirement. Held in the same free capacity as the static reserve (a MW counts once); each unit can give at most `dynamic_reserve_contribution` of its rated power. |
| | Reserve Duration | h | Duration over which reserve capacity must be sustained. Integer, 0 to 24; default 1. |
| | Losses | frac | Distribution losses at this node (0.0 to 1.0). A value of 0.03 means 3% of generation is lost in local distribution. |
| | Trans. Invest Cost | $/MW | Cost per MW to increase transfer capacity at this node. |
| | Trans. Invest Max | MW | Maximum transfer capacity expansion at this node. |
| **Demand** | Electric Demand: Load CSV... | path | Hourly demand in MW from a CSV or Excel file (no header row). A single-column file is used as is; with several columns, the column whose position equals the node index is read. The number of hours must match the demand already loaded on the other nodes. |
| | Forecast | -- | Opens a demand-forecast dialog for this node only, without running the whole Grid Builder. |
| | Hours / Peak / Total | h, MW, MWh | Read-only summary of the loaded demand. |
| | View Demand... | -- | Plots the node's demand (enabled once demand is loaded or forecast). |
| | Demand Sectors | table | Sectors defined for the system (added by Sector ID), with columns Flexible, Flex. Ratio, Criticality (critical / high / medium / low, which selects the system's VOLL multiplier) and Delay Tol. |
| | Sector Distribution (this node) | frac | Share of this node's demand assigned to each sector. |
| | Non-Electric Demand Types | table | Non-electric demand per fuel (pick a fuel, then **Add**), with columns Unit, Demand, Flexible, Flex. Ratio, Criticality and Delay Tol. |

If no demand is loaded, the node has zero demand. Per-node, per-technology investment limits are set in the [Investment Portfolio](#investment-portfolio), not on the node.

### Generator

All forms of electricity production: solar PV, wind turbines, diesel engines, gas turbines, etc.

| Section | Field | Unit | Description |
|---------|-------|------|-------------|
| **Identification** | Name | -- | Generator name (e.g., "Solar Farm North") |
| | Type | -- | `Renewable` or `Non-renewable`. Determines how the generator counts toward RE penetration targets. |
| | Fuel | -- | Fuel, chosen from the system's fuel list (Solar, Wind, Water, Diesel, ...). |
| | Technology | -- | Optional reference to an entry under **Technologies**. |
| | Node | -- | Parent node. The bus the generator connects to is set when it is placed on the map. |
| | Availability | path | Hourly availability CSV (values 0 to 1, fraction of rated power available in each hour). The button inside the field browses for a file, or clears the path when one is set. |
| **Existing Capacity** | Rated Power | MW | Nameplate capacity. |
| | Life Time | years | Expected operational life. |
| | Initial Age | years | Age of the unit at the start of the horizon. |
| | Degradation | fraction/yr | Annual capacity degradation, as a fraction per year: 0.005 means 0.5 %/yr. |
| | Risk Coefficient | 0-1 | Geographic risk derating factor, normally computed by the Risk workflow. 1.0 = no derating. |
| **Operating Parameters** | Units | - | Identical units the plant at this node is made of (`num_units`, default 1). Unit commitment commits a whole number of them, each of Rated Power / Units with its own Min Power, start-ups and Min Up / Min Down; the availability is the share of units available. Merging generators in the Grid Builder adds their units up. |
| | Min Power | pu | Minimum stable output as a fraction of rated power (of one unit's rating when Units > 1). Applied with unit commitment. |
| | Min Up / Min Down | h | Minimum on / off time after a start / stop. Unit commitment only. |
| | Ramp Up / Ramp Down | pu/h | Maximum change in output per hour as a fraction of capacity; the limit per time step is `min(1, ramp × resolution)`. Default 1.0; 0 = no limit. |
| | Eff. at Rated / Eff. at Min | frac | Conversion efficiency at full load and at minimum output (0.0 to 1.0). |
| **Costs** | Fuel | $/MWh | Variable fuel cost per MWh of electrical output. Typically 0 for renewables. |
| | Cost Curve | dropdown | Fuel cost model: flat (default), linear, stepwise or exponential. See [Cost Curve Widgets](#cost-curve-widgets). |
| | Fixed | $/MW·yr | Fixed O&M per MW of rated power per year, charged while the unit is in service whether or not it runs (prorated to the simulated hours). |
| | Maintenance | $/MWh | Variable O&M cost per MWh generated. |
| | Start-up | $ | Cost of starting the whole plant; one of its units costs Start-up / Units. Unit commitment only. |
| | Decommissioning | $/MW | Cost of retiring capacity. |
| **Electrical** | Frequency | Hz | Unit frequency. |
| | Current Type | -- | `AC`, `DC` or `AC_DC`. |
| | Inertia H | s | Inertia constant. Set to 0 for inverter-based resources. |
| | Droop R | pu | Governor droop (0.05 = 5%). |
| | Governor T | s | Governor time constant. |
| **Appearance** | Color, Size | -, px | Marker colour and size. Size 0 = auto (uses the global marker scaling). |

There are no investment fields on the generator form: new capacity is configured in the [Investment Portfolio](#investment-portfolio).

#### Reservoir Mode (Hydro Generators)

The **Reservoir** section appears for a generator whose fuel is `Water`, and for any generator whose reservoir capacity is above 0. To give a new hydro unit a reservoir, set its fuel to `Water` and enter a Reservoir Capacity. With a reservoir, hydro is dispatched against a water-energy budget rather than as firm capacity.

| Field | Unit | Description |
|-------|------|-------------|
| Reservoir Capacity | MWh | Maximum energy storage of the reservoir. 0 = no reservoir (run-of-river); on a non-`Water` generator, 0 also hides the section. |
| Initial Level | frac | Starting level as a fraction of reservoir capacity |
| Min Level / Max Level | frac | Allowed level band |
| Inflow File | path | CSV with hourly inflow in MW-equivalent |
| Turbine Efficiency | frac | Turbine conversion efficiency |
| Evaporation Rate | /h | Fraction of stored energy lost per hour |
| Pump Capacity / Pump Efficiency | MW, frac | Pumping capability; a capacity above 0 makes the unit pumped storage |
| Allow Spillage | -- | Permit uncontrolled release when the reservoir is full |
| Reservoir Invest Cost / Max Reservoir Expansion | $/MWh, MWh | Optional endogenous reservoir capacity expansion |
| Min Environmental Flow | MW | Mandatory minimum release (turbined and/or spilled) for ecological flow |
| Head Min. Power Factor | 0-1 | Available turbine power at the minimum level. `1.0` = no head effect; below 1.0 the peak power scales linearly with the fill level (a low reservoir delivers less power) |
| Cascade Downstream Reservoir | -- | Another reservoir generator that receives this unit's release (hydraulic cascade). Picked from the other reservoirs in the system |
| Cascade Travel Delay | h | Water travel time before the release reaches the downstream reservoir (0 to 168) |

!!! tip "Seasonal hydro"
    Enable **Inter-Period Storage Linking** in [Global Settings](global-settings.md) to let reservoirs carry water across representative periods — water banked in a wet season is then available in a later dry one, instead of the level being cyclic within each period. The same toggle drives seasonal carry-over for batteries.

### Battery

All forms of energy storage: lithium-ion, flow batteries, pumped hydro, compressed air, etc.

| Section | Field | Unit | Description |
|---------|-------|------|-------------|
| **Identification** | Name | -- | Storage name (e.g., "Li-ion Battery Bank A") |
| | Fuel | -- | Normally `None`. |
| | Technology | -- | Optional reference to an entry under **Technologies**. |
| | Node | -- | Parent node. |
| | Availability | path | Availability file: each hour's factor (0-1) multiplies the charge and discharge power (see [Availability Profiles](../user-guide/availability-profiles.md)). |
| | Current Type | -- | `AC` or `DC`. |
| **Power & Capacity** | Rated Power | MW | Power rating. |
| | Capacity | MWh | Energy storage capacity. |
| | Max Charge / Max Discharge | MW | Maximum charging and discharging power. |
| | Min Duration / Max Duration | h | Allowed energy-to-power ratio (E/P) for investment. |
| **Efficiency & SOC** | Charge Eff. / Discharge Eff. | frac | Charging and discharging efficiency (0.0 to 1.0). |
| | SOC Initial | frac | Starting state of charge (0.0 to 1.0) at the start of each year. Rolling windows carry the SOC from one to the next; at the end of the year the battery must be back to at least this level (within `soc_end_tolerance`, soft). |
| | Max DoD | frac | Maximum depth of discharge. |
| **Lifetime** | Life Time / Initial Age | years | Expected life and age at the start of the horizon. |
| | Degradation | fraction/yr | Annual capacity degradation, as a fraction per year (0.01 = 1 %/yr). |
| | Risk Coefficient | 0-1 | Geographic risk derating factor. 1.0 = no derating. |
| **Operating Parameters** | Ramp Up / Ramp Down | pu/h | How fast charge or discharge power may rise / fall, as a fraction of Max Charge / Max Discharge per hour. 0 = no limit. |
| | Provides reserves | checkbox | Its headroom (unused discharge power plus the charging it can stop) counts towards the static and dynamic reserve. |
| | Inertia H | s | Inertia constant. |
| **Costs** | Fixed | $/MW·yr | Fixed O&M per MW of Max Discharge per year, charged while the battery is in service whether or not it is used. |
| | Maintenance | $/MWh | Variable O&M cost. |
| | Throughput Degr. | $/MWh | Cycling (wear) cost per MWh discharged. |
| | Discharge Cost Curve | dropdown | flat (default), linear, stepwise or exponential. See [Cost Curve Widgets](#cost-curve-widgets). |
| | Decommissioning | $/MW | Cost of retiring capacity. |
| **Appearance** | Color, Size | -, px | Marker colour and size (0 = auto). |

A battery's `invest_cost` and `invest_max_power` are set in the [Investment Portfolio](#investment-portfolio); new storage is built from battery technologies.

#### Cost Curve Widgets

The generator **Cost Curve** and the battery **Discharge Cost Curve** dropdowns select the marginal cost model. Every curve is converted internally to stepwise blocks.

- **flat** (default) -- No additional widgets. Uses the flat `Fuel` cost (generator) or `Throughput Degr.` cost (battery).

- **linear** -- Three fields appear:
  - `Price at 0%` ($/MWh) -- Marginal cost at zero output.
  - `Price at 100%` ($/MWh) -- Marginal cost at rated output.
  - `Segments` (2 to 20, default 5) -- Number of blocks used to approximate the line. More segments give a smoother curve but add constraints.

- **stepwise** -- A table where each row is a generation block:
  - `Fraction` -- Share of rated capacity in this block (e.g., 0.4 = 40% of rated power).
  - `Price ($/MWh)` -- Marginal cost within this block.
  - **+ Block** and **- Block** add and remove rows.
  - Fractions should sum to 1.0; if they do not, they are rescaled to sum to 1.0. Use non-decreasing prices (cheapest block first).

- **exponential** -- Three fields appear:
  - `Base Price` ($/MWh) -- Cost at zero output.
  - `Scale Factor` -- Exponential growth rate.
  - `Segments` -- Number of approximation blocks.
  - The cost follows `base_price * exp(scale_factor * P/P_max)`, which models rising marginal cost at high utilization.

### Transmission Line

Electrical connections between buses or equipment: overhead lines, underground cables, or submarine cables.

| Section | Field | Unit | Description |
|---------|-------|------|-------------|
| (header) | System and endpoints | -- | Read-only: system name and the names of the connected nodes. |
| **Bus endpoints** | From bus / To bus | -- | The buses the line connects. Set when the line is traced on the map; can be changed here. |
| **Properties** | Capacity | MW | Maximum power transfer in either direction, all circuits together. |
| | Voltage | kV | Operating voltage. |
| | Type | -- | overhead, underground or submarine (blank = unspecified). |
| | Length | km | Line length. 0 (shown as "Auto") = a distance is computed when the model is built. |
| | Reactance per km | ohm/km | This line's series reactance per km (saved as `reactance_per_km`). Used with the length when no per-unit reactance is set. 0 = "Default" (the system's DC power flow reactance per km). |
| | Reactance / Resistance / Susceptance | pu | Per-unit X, R and B on the system base (`ac_power_flow.base_mva`). Reactance 0 = "Global": derived as length × reactance per km / Z_base, with Z_base = V² / S_base from the line voltage; 0.01 pu without a length. |
| | Circuits | -- | Number of parallel circuits (1 to 10). Descriptive: the capacity and the per-unit values are those of the whole line, all circuits together, and changing the count does not rescale them. Only a reactance derived from the reactance per km is divided over the circuits. |
| | Frequency / Current Type | Hz, -- | Inherited from the connected bus. |
| **Appearance** | Color, Width | -, px | Line colour and width (0 = auto, uses the global line scaling). |
| **Actions** | Edit Trace | toggle | Enable/disable editing of the polyline vertices. |
| | Delete Line | button | Deletes the line after a confirmation. |

There are no investment fields on the line form: transmission expansion is configured in the [Investment Portfolio](#investment-portfolio) (type Transmission).

### Transformer

Connects buses at different voltage levels.

| Field | Unit | Description |
|-------|------|-------------|
| Name | -- | Transformer name |
| Connected From / Connected To | -- | Read-only list of what is connected on each side (labelled HV/LV or Side A/Side B depending on the voltages). |
| From bus / To bus | -- | The buses on each side |
| From Voltage / To Voltage | kV | Voltage on each side (the labels switch to HV/LV or Side A/Side B) |
| Rated Power | MVA | Transformer power rating |
| Impedance | pu | Series impedance in per-unit (0 to 1) |
| Losses | frac | Loss fraction |
| Color, Size | -, px | Marker style (Appearance section) |

### Bus

Electrical connection points at a given voltage. Each bus belongs to a parent node; equipment and lines attach to buses.

| Field | Unit | Description |
|-------|------|-------------|
| Bus ID | -- | Read-only identifier (e.g., "bus_0") |
| Name | -- | Bus name (e.g., "110kV Busbar A") |
| Parent Node | -- | Node this bus belongs to |
| Voltage | kV | Bus voltage level |
| Frequency | Hz | System frequency |
| Current Type | -- | `AC` or `DC` |
| Bus Type | -- | `PQ`, `PV` or `slack`. The slack bus is the angle reference for power flow. A network uses a single slack bus; choosing `slack` when another bus already has it shows a warning. |
| Role | -- | `connection` (pure junction, no demand), `load` (serves demand) or `mixed` (equipment and demand). Default `connection`. On every save, export or run the Studio sets the role of the other buses from the topology (bus with generation or storage: `mixed`; only bus of its node, or low-voltage bus below 100 kV / LV side of a step-down transformer: `load`; otherwise `connection`). A role you pick in this field is yours: it is kept on save and reload (the project stores the list of such buses in `_gui_layout.user_set_bus_roles`), a `connection` bus you set never receives demand, and a `mixed` bus you set counts as a demand bus. |
| Demand Fraction | frac | Share of the parent node's demand served by this bus. Forced to 0 and disabled for `connection` buses. The fractions of a node's buses should sum to 1.0. At save time, a node whose fractions do not sum to 1.0 gets its demand redistributed over its `load` buses (and the `mixed` buses whose role you set); if every bus of a node is a `connection` you set, the node's demand has nowhere to go and a warning is logged; when a configuration is loaded, fractions are rescaled per node to sum to 1.0, with a warning. |
| Color, Size | -, px | Marker style (Appearance section) |

### AC/DC Converter

Interfaces between AC and DC buses for HVDC transmission and DC-coupled storage.

| Section | Field | Unit | Description |
|---------|-------|------|-------------|
| **Identity** | Name | -- | Converter name |
| | Converter Type | -- | `VSC` (Voltage Source Converter) or `LCC` (Line-Commutated Converter) |
| **Topology** | Connected From (AC) / Connected To (DC) | -- | Read-only list of connected equipment |
| | AC bus / DC bus | -- | Connected buses |
| | AC Voltage / DC Voltage | kV | Voltage on each side |
| **Capacity** | Rated Power | MVA | Maximum power transfer |
| | Min Power | MVA | Minimum operating power in either direction. With unit commitment the converter is off or carries at least this much; ignored in economic dispatch and in the master problem. |
| **Efficiency** | Rectifier Efficiency | frac | AC-to-DC conversion efficiency |
| | Inverter Efficiency | frac | DC-to-AC conversion efficiency |
| | Standby Losses | MW | No-load consumption when energized |
| **Reactive Power** | Q Min / Q Max | MVAr | Reactive power limits (negative = absorb) |
| | Power Factor | -- | Power factor |
| **Impedance** | Impedance / Resistance | pu | Converter impedance and resistance |
| **Economics** | Fixed Cost | $/MW/yr | Annual fixed O&M |
| | Variable Cost | $/MWh | Cost per MWh transferred |
| **Lifecycle** | Lifetime / Initial Age | years | Expected life and current age |
| | Degradation Rate | fraction/yr | Fraction per year (0 to 0.1; 0.005 = 0.5 %/yr) |

### Frequency Converter

Interconnects systems operating at different frequencies (e.g., 50 Hz and 60 Hz).

| Section | Field | Unit | Description |
|---------|-------|------|-------------|
| **Identity** | Name | -- | Converter name |
| **Topology** | Connected From / Connected To | -- | Read-only list of connected equipment |
| | From bus / To bus | -- | Connected buses (sides A and B) |
| | From Frequency / To Frequency | Hz | Frequency on each side |
| **Capacity** | Rated Power / Min Power | MVA | Maximum and minimum transfer. The minimum applies only with unit commitment (off or at least Min Power), not in economic dispatch or in the master problem. |
| **Efficiency** | A→B Efficiency / B→A Efficiency | frac | Efficiency in each direction |
| | Standby Losses | MW | No-load consumption |
| **Reactive Power** | Q Min / Q Max | MVAr | Reactive power limits |
| **Impedance** | Impedance / Resistance | pu | Converter impedance and resistance |
| **Economics** | Fixed Cost | $/MW/yr | Annual fixed O&M |
| | Variable Cost | $/MWh | Cost per MWh transferred |
| **Lifecycle** | Lifetime / Initial Age | years | Expected life and current age |
| | Degradation Rate | frac/yr | Annual degradation |


---


## Fuel Infrastructure

### Fuel Source

A location where fuel enters the system — a port, pipeline terminal, or LNG
terminal — together with the supply characteristics of each fuel delivered
there. Placed on the map and configured per fuel. (A Fuel Source is the single
fuel-supply concept; earlier versions split it into a separate "Fuel Entry
Point" and "Fuel Source".)

| Field | Unit | Description |
|-------|------|-------------|
| Name | -- | Source name (e.g., "Port of Mariel LNG Terminal") |
| Node | -- | Associated network node |
| Latitude / Longitude | degrees | Position on the map |
| Fuels | -- | Fuel types supplied here (**Add** / **Remove**). |
| **Per-fuel table** | | For each fuel: |
| -- Max Import Rate | units/h | Maximum supply (import) rate |
| -- Import Cost | $/unit | Cost per unit of fuel supplied here |
| -- Transit (days/100km) | days/100km | Source→tank replenishment lead time. Models supply stress: a shipment dispatched now arrives after this delay scaled by route distance. `0` = instantaneous. |
| -- Disrupt. Start (h) | hour | Supply-disruption window start (inclusive), in horizon hours: hour 0 = 1 January of the base year, 8760 per year. |
| -- Disrupt. End (h) | hour | Supply-disruption window end (exclusive), in horizon hours. `end <= start` disables the disruption. |
| -- Disrupt. Avail. (0-1) | fraction | Availability during the disruption window (`0` = full cut, `1` = no disruption). |

### Fuel Storage

Tank farms, gas holders, and other fuel stockpiling facilities.

| Field | Unit | Description |
|-------|------|-------------|
| Name | -- | Facility name |
| Node | -- | Location node |
| Fuels | -- | Stored fuel types (**Add** / **Remove**) |
| **Per-fuel table** | | For each fuel: |
| -- Capacity | fuel units | Maximum storage, in the fuel's own unit |
| -- Initial Level | frac | Starting inventory as a fraction of capacity |
| -- Min Level | frac | Minimum reserve as a fraction of capacity |

### Fuel Transport Route

Pipelines, shipping lanes, and other fuel delivery pathways.

| Field | Unit | Description |
|-------|------|-------------|
| (header) | -- | Read-only: system name and the connected nodes |
| Fuels | -- | Transported fuels (**Add** / **Remove**) |
| **Per-fuel table** | | For each fuel: |
| -- Capacity | units/h | Maximum transport rate |
| -- Transport Cost | $/unit/km | Distance-dependent transport cost |
| -- Losses | frac | Fraction of fuel lost in transport |
| Length | km | Route length |
| Edit Trace | toggle | Enable polyline vertex editing for the route geometry |
| Delete Route | button | Deletes the route after a confirmation |

### Fuel Properties

System-level fuel definitions (Element Tree: right-click **Fuels > Add Fuel**).

| Field | Unit | Description |
|-------|------|-------------|
| Name | -- | Identifier (e.g., Diesel, Natural Gas, Hydrogen, Ammonia) |
| Unit | -- | Physical unit of the fuel (e.g., kTon, MMBTU). Blank for renewables. |
| Emission Factor | tCO2/MWh | CO2 emissions per MWh of fuel energy content. Generator emissions divide output by the unit's rated efficiency. Set to 0 for zero-carbon fuels. |
| Energy Content | MWh/unit | Energy per fuel unit. Left blank for renewable sources (Solar, Wind, Water, OTEC). |
| Base Price | $/unit | Fuel price at the start of the horizon |
| Price Growth Rate | frac/yr | Annual price escalation. 0.02 means +2% per year. |
| Delete Fuel | button | Deletes the fuel after a confirmation |

### Electrolyzer

Converts electricity to hydrogen, enabling power-to-gas coupling.

| Section | Field | Unit | Description |
|---------|-------|------|-------------|
| **Identity** | Name | -- | Electrolyzer name |
| | Fuel | -- | Fuel produced (e.g., Hydrogen) |
| | Technology Ref | -- | Optional reference to an entry under **Technologies** |
| **Capacity** | Rated Power | MW | Maximum electrical input power |
| | Ramp Up / Ramp Down | pu/h | Maximum change in input power per hour as a fraction of rated power |
| **Efficiency** | Eff. at Rated / Eff. at Min | frac | Conversion efficiency at full and at minimum load |
| | Energy per kg H₂ | kWh/kg | Electricity needed per kg of hydrogen |
| **Economics** | Fixed Cost | $/MW/yr | Annual fixed O&M |
| | Variable Cost | $/MWh | Cost per MWh of electricity consumed |
| | Water Cost | $/kg H₂ | Cost of the water per kg of hydrogen produced |
| **Lifecycle** | Lifetime | years | Life of an electrolyzer the master builds (existing ones do not age) |

Investment in new electrolyzer capacity is configured in the [Investment Portfolio](#investment-portfolio). The **Delete Electrolyzer** button asks for confirmation.


---


## Development Zone

Geographic areas (polygons) where the optimizer may install new generation capacity.

| Section | Field | Unit | Description |
|---------|-------|------|-------------|
| | Name | -- | Zone name (e.g., "Northern Solar Zone") |
| | Technology | -- | Zone technology (Solar, Wind, Battery, Hydro, Biomass, Hydrogen, Gas, Nuclear; editable) |
| | Node | -- | Associated node, or `(none)` |
| | Polygon | -- | Read-only: number of vertices and area |
| | Notes | -- | Free text |
| **Interconnection** | Line Cost | $/MW/km | Cost of the connection line, per MW and km |
| | Transformer Cost | $/MW | Cost of the connection transformer |
| | Target Bus | -- | Bus the zone connects to; `(auto - nearest)` by default. The distance and the total interconnection cost ($/MW) are shown below. |
| **Allowed Technologies** | (table) | -- | Tick the technologies that may invest in the zone, with an optional Max. Invest per technology (`Unlimited` by default). None selected = auto-match by the zone technology name. |
| | Exclusive (invest only in this zone) | -- | When enabled, the selected technologies can invest only in this zone; investment at their original nodes is set to zero. |
| **Appearance** | Color, Opacity | -- | Polygon fill style |
| | Edit Polygon | toggle | Enable boundary vertex editing |
| | Delete Zone | button | Deletes the zone after a confirmation |


---


## EV Configuration

Per-system electric vehicle configuration. The form has four stacked sections.

### Categories

Define EV categories with **Add** (by Category ID, e.g. `light`) and **Remove Selected**. The table has one column per category and one row per field:

| Row | Unit | Description |
|-----|------|-------------|
| Battery | kWh | Onboard battery capacity per vehicle |
| Charging | kW | Maximum charging power per vehicle |
| V2G Power | kW | Maximum vehicle-to-grid discharge power per vehicle (0 if V2G is not supported) |
| V2G Participation | fraction | Share of vehicles that take part in V2G |
| Charge Eff. / Discharge Eff. | fraction | Charger and V2G efficiencies |
| Min SOC | fraction | Minimum state of charge |
| Max Adoption | fraction | Saturation level of the adoption S-curve |
| Growth Rate | -- | Steepness of the adoption S-curve |
| Mid Point | fraction | Position of the S-curve midpoint within the horizon |

### Initial SOC (per Node)

One initial state of charge per node (fraction, 0.0 to 1.0), shared by all categories.

### Quantities (per Node)

Vehicle count per node (rows) and category (columns).

### Base Patterns (24h)

Base 24-hour pattern for each category: 24 rows (00:00 to 23:00) by category columns.


---


## Rooftop Solar

Per-system distributed rooftop PV configuration.

| Section | Field | Description |
|---------|-------|-------------|
| **Settings** | Adoption Scenario | Low, Medium or High growth curve. Controls the S-curve adoption trajectory over the planning horizon. |
| | Weather Variability | Low, Normal or High inter-annual solar resource variation |
| | Simulation Seed | Random seed for the adoption simulation |
| | Performance Ratio (frac) | System-level efficiency factor (typically 0.75-0.85) |
| | Degradation Rate (frac/yr) | Annual panel degradation |
| | Base Year / Target Year | Start and end years of the adoption curve |
| **Per-Node Parameters** | Systems/Node | Number of potential rooftop installations at the node |
| | Avg Size (kW) | Average size per installation |
| | Init Adoption | Current adoption fraction (0.0 to 1.0) |
| **Adoption Limits (by Scenario)** | Max Adoption / Adoption Rate | One row per adoption scenario |


---


## Technology

Technology catalogue entries (Element Tree: right-click **Technologies > Add Technology**). Generators, batteries, electrolyzers and Investment Portfolio entries can reference a technology.

| Section | Field | Description |
|---------|-------|-------------|
| **Identity** | Name | Technology name |
| | Category | `Renewable`, `Non-renewable`, `Storage` or `Electrolyzer` |
| | Fuel | Fuel, or `(None)` |
| | Color | Colour used in result charts |
| **Basic Operations** | Lifetime | Expected life (years) |
| | Degradation Rate | Annual degradation (fraction) |
| | Efficiency at Rated / Efficiency at Min | Conversion efficiencies |
| | Delete Technology | Deletes the entry after a confirmation |

Investment costs and limits are not stored on the technology; they are set in the Investment Portfolio.


## Investment Portfolio

Each entry defines where and how much new capacity of one technology the optimizer may build. Add entries from the Element Tree: right-click **Investment Portfolio** and choose **Add Generator**, **Add Battery**, **Add Electrolyzer**, **Add AC/DC Converter**, **Add Freq. Converter**, **Add Transmission** or **Add Fuel Storage**.

| Section | Field | Description |
|---------|-------|-------------|
| **Identity** | Name | Entry name (e.g., "Solar PV") |
| | Type | Generator, Battery / Storage, Electrolyzer, AC/DC Converter, Freq. Converter, Transmission, Fuel Storage or Fuel Transport |
| | Technology | Optional reference to a Technologies entry |
| **Investment per Node** | Node / Invest Cost ($/MW) / Invest Max (MW) | One row per node. A Generator entry has only Invest Cost: it is the existing generator's `invest_cost`, which prices its life extension (new generation is built from technologies). For a Battery entry, Invest Cost and Invest Max are the battery's `invest_cost` (life extension) and `invest_max_power`. |
| | Populate from Nodes | Adds a row, with default values, for every node of the system not yet in the table |
| | Delete Entry | Deletes the entry after a confirmation |

This is the only place where per-node investment costs and limits are set; the node, generator, battery and line forms have no investment fields.


## Custom Constraints and Interruptions

- **Edit > Custom Constraints...** opens the custom constraints dialog. See [Custom Constraints](../user-guide/custom-constraints.md).
- **Edit > Interruptions...**, or right-click a generator, battery, line, transformer or converter in the Element Tree and choose **Schedule interruption…**, opens the outage calendar. See [Scheduled Interruptions](../user-guide/scheduled-interruptions.md).


---


## Multi-Select Editing

Selecting several elements of the same type in the Element Tree (`Ctrl+Click` to add items, `Shift+Click` for a range) shows one form for all of them. The map selects one element at a time. Multi-edit is available for nodes, generators, batteries, lines, transformers, buses, AC/DC and frequency converters, electrolyzers, fuels, fuel routes, technologies and Investment Portfolio entries.

- Fields with the same value across the selection show that value.
- Fields with different values show `-- mixed values --` (text fields) or `-- mixed --` (numeric fields and dropdowns). A field left mixed is not changed.
- Changing a field applies the new value to **all** selected elements, including text fields such as the name.

Useful for:
- Setting uniform fuel costs across all diesel generators.
- Applying a common availability profile to all solar generators.

With several items selected, the Element Tree context menu offers **Delete *n* items** (the items must belong to one system).


---


## Deleting Elements

Elements are deleted from the context menu (**Delete**, in the Element Tree or on the map). There is no confirmation dialog and no list of dependents: deleting a node or a bus also removes the generators, batteries and electrolyzers on its buses and the lines, transformers and converters attached to them. Use **Edit > Undo** (`Ctrl+Z`) to restore them.

The **Delete** buttons on the line, fuel route, fuel, electrolyzer, zone, technology and Investment Portfolio forms ask for a simple confirmation. Deleting a system asks for confirmation and is refused for the last remaining system.
