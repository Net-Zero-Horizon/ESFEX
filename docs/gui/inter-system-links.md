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

A fuel route traced between fuel facilities of two different systems is stored as a link of type *fuel route*. These links are not supported end to end yet: the link form has no fuel selector, and when the project is saved they are written as electrical links (`meta_network.systems_links`), so after reloading they appear as transmission links. Model cross-system fuel transport with care, or avoid these links for now.


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
| **Properties** | Capacity | MW | Existing transfer capacity. Set to 0 if the link only represents a possible investment. |
| | Voltage | kV | Nominal voltage |
| | Type | -- | overhead, underground, or submarine |
| | Length | km | Physical length. Computed from the trace; editable. |
| | Base Impedance | ohm | Base impedance for per-unit conversion |
| | Reactance per km | ohm/km | Series reactance per km |
| | Reactance / Resistance / Susceptance | pu | Per-unit line parameters |
| | Circuits | -- | Number of circuits (1–10) |
| | Frequency | Hz | Nominal frequency |
| | Current Type | -- | AC or DC |
| **Economic** | Investment cost | $/MW | Cost per MW of new transfer capacity |
| | Max investment | MW | Maximum new capacity the optimizer may build. 0 means no expansion. |
| | Operational cost | $/MW·km | Stored in the configuration; not used by the current solver |
| | Loss factor | fraction | Must be between 0 and 1. Stored in the configuration; not used by the current solver |
| **Appearance** | Color, width, opacity | -- | Map style |

Buttons at the bottom: **Delete Line** and **Edit Trace**.

### How Links Enter the Model

When the systems are merged for the simulation, each link becomes a transmission line between the first bus of each endpoint node, with the link's capacity, length, and per-unit reactance and resistance. The investment cost and maximum are applied as transfer-investment parameters at both endpoint nodes (the larger value wins if several links meet at a node). Voltage, type, circuits, frequency, and current type are kept for display and do not change the solver. Multiple links between the same pair of systems are allowed and act as separate lines.


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
- Renaming a system does not update its links; see [System Management](system-management.md#renaming-a-system).


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
