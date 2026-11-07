# Inter-System Links

Connections between two power systems of the same project: submarine cables, HVDC interconnectors, cross-border lines. Links appear under the **Inter-system Links** item in the Element Tree, in the **Transmission Lines** and **Fuel Routes** sub-categories.


---


## Concepts

All systems selected in Global Settings > Systems to Simulate are solved together in one optimization. Without links, the systems do not exchange anything. A transmission link lets power flow between a node of one system and a node of another, so the optimizer can share generation and reserves across systems.

### Link Ownership

A link belongs to the project, not to either system. It appears once under **Inter-system Links**. Deleting either connected system removes its links.


---


## Link Types

### Transmission Links

Electrical connection between two systems. Power can flow in either direction.

### Fuel Route Links

A fuel route traced between fuel facilities of two different systems is stored as a link of type *fuel route* (`link_type: fuel_route` in `meta_network.systems_links`), together with the fuel it carries. It moves fuel, not power: in the simulation it becomes a pair of primary-energy transport routes, one in each direction, between the two nodes (see [How Links Enter the Model](#how-links-enter-the-model)). Choose the fuel in the link form; a fuel route without a fuel is ignored by the run.


---


## Creating Links

All systems are drawn on the map at once, so you create a link by tracing across systems with the normal tools:

1. Make sure both systems have the elements you want to connect (buses or equipment for a transmission link; fuel sources or fuel storages for a fuel route).
2. Select **Line** (or **Fuel Route**) in the toolbar.
3. Click the start element in the first system.
4. For a transmission link, click on the map to add waypoints that route the cable.
5. Click the end element in the other system.

When the two endpoints belong to different systems, an inter-system link is created instead of a normal line. The status bar confirms it. A new transmission link starts with 100 MW capacity, and its length is computed from the endpoints and waypoints; a new fuel route link starts with 0 capacity.

**Tips:**
- Press `Escape` during the trace to cancel.
- Waypoints can be edited later with **Edit Trace** in the link form.
- There is no "Add" entry in the tree for links; the Inter-system Links item has no context menu.


---


## Link Properties

The link form mirrors the transmission line form. A read-only header shows the two systems and nodes.

| Section | Field | Unit | Description |
|---------|-------|------|-------------|
| **Bus endpoints** | From bus / To bus | -- | Buses at each end of the link |
| **Properties** | Fuel | -- | Fuel route links only: the fuel the route carries (a fuel of either system) |
| | Capacity | MW | Existing transfer capacity. Set to 0 if the link only represents a possible investment. On a fuel route: route capacity in fuel units per day. |
| | Voltage | kV | Nominal voltage |
| | Type | -- | overhead, underground, or submarine |
| | Length | km | Physical length. Computed from the trace and the endpoints (read-only; move the endpoints or edit the trace to change it). |
| | Reactance per km | ohm/km | Series reactance per km; stored with the link, not used by the run (set the per-unit reactance) |
| | Reactance / Resistance / Susceptance | pu | Per-unit line parameters |
| | Circuits | -- | Number of circuits (1–10) |
| | Frequency | Hz | Nominal frequency |
| | Current Type | -- | AC or DC |
| **Economic** | Investment cost | $/MW | Cost per MW of new transfer capacity |
| | Max investment | MW | Maximum new capacity the optimizer may build. 0 means no expansion. |
| | Operational cost | $/MW·km | Transmission links: flow cost; each MWh the link carries costs length × this value in the master problem. Fuel routes: transport cost in $ per fuel unit per km. |
| | Loss factor | fraction | Must be between 0 and 1. Transmission links: stored in the configuration, not used by the current solver. Fuel routes: fuel lost per 100 km. |
| **Appearance** | Color, width, opacity | -- | Map style |

Buttons at the bottom: **Delete Line** and **Edit Trace**.

### How Links Enter the Model

When the systems are merged for the simulation, each transmission link becomes a transmission line between the first bus of each endpoint node, with the link's capacity, length, and per-unit reactance and resistance. The investment cost and maximum are applied as transfer-investment parameters at both endpoint nodes (the larger value wins if several links meet at a node). Voltage, type, circuits, frequency, and current type are kept for display and do not change the solver. Multiple links between the same pair of systems are allowed and act as separate lines.

Each fuel route link becomes two one-way fuel transport routes (`link<k>_<A>_<B>_<fuel>_<i>_fwd` and `_rev`, where `k` is the link's position in `systems_links` and `i` its connection, so several fuel links between the same systems keep distinct ids) between its two nodes, added to the merged `fuel_transport_routes`. It is not part of the electrical network. The link fields map as follows:

| Link field | Route parameter |
|------------|-----------------|
| Fuel | the route's fuel (`fuel_params` key) |
| Capacity | `capacity` (fuel units/day) |
| Length (`distance_km`) | `distance_km` |
| Operational cost (`cost_per_mw_km`) | `transport_cost` ($/unit/km) |
| Loss factor | `losses_fraction` (per 100 km) |
| Investment cost, Max investment | not used: new route capacity follows the fuel's `fuel_infrastructure` (a warning is logged if they are set) |

The routes only carry fuel when primary energy is modelled for that fuel (see [Primary Energy](../formulation/primary-energy.md#transport)).


---


## Managing Links

### Selecting and Editing

- Click a link in the Element Tree or on the map. Its form appears in the Properties Panel.
- Tree items are labelled `<system>:<node> -> <system>:<node>`, with the capacity (or fuel) in the info column.

### Editing Link Routes

1. Select the link.
2. Click **Edit Trace** in the Properties Panel.
3. Drag the vertex handles to reshape the route.
4. Click **Edit Trace** again to finish.

### Deleting Links

- Right-click the link in the tree and select **Delete**, or use the **Delete** button below the tree or **Delete Line** in the form.
- Links are removed automatically when either connected system is deleted.
- Deleting links, including the last one, persists on save: the saved `meta_network.systems_links` lists exactly the links in the Studio.
- Renaming a system updates its links; see [System Management](system-management.md#renaming-a-system).


---


## Validation

The Validate dialog does not check inter-system links. They are checked by the automatic validation that runs before every save and after loading a configuration. The rules are:

| Rule | Severity |
|------|----------|
| Both systems are loaded in the project | Error |
| A link does not connect a system to itself | Error |
| Node indices are within range in their systems | Error |
| Capacity, costs, and distance are not negative | Error |
| Loss factor is between 0 and 1 | Error |
| Endpoint buses exist in their systems | Warning (the line may not be drawn) |
| A transmission link has a capacity above 0 | Warning (it carries no power otherwise) |
| A fuel route link has a fuel | Warning (the run ignores it otherwise) |
| No two links of the same kind join the same pair of nodes (a transmission link and a fuel route, or fuel routes for different fuels, may share a pair) | Warning (the second one is ignored) |


---


## Map Display

Links are drawn as purple (`#8e44ad`) lines, 3 px wide by default, following their waypoints. Change color, width, and opacity in the form's Appearance group. Waypoints are saved in the YAML configuration.


---


## Workflow Example: Connecting Two Island Systems

1. Create two systems, "Cuba" and "Jamaica", and add their buses (see [Map Editor](map-editor.md)).
2. Select **Line** in the toolbar.
3. Click a coastal bus in Cuba, click waypoints along the cable route, then click a bus in Jamaica.
4. In the link form, set Capacity = 0 MW (no existing interconnection).
5. Set Investment cost = 2,000,000 $/MW and Max investment = 500 MW.
6. Check the computed Length and the per-unit reactance.
7. Validate and run. The optimizer can now invest in the interconnection.
