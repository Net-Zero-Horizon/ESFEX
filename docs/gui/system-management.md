# System Management

Multiple independent power systems within a single project. Each system represents a self-contained electrical network (island, country, regional grid) with its own nodes, buses, equipment, lines, and settings. Systems interconnect via inter-system links.


---


## Element Tree

Hierarchical organization of all project elements:

- **Global Settings** -- Simulation-wide parameters (solver, temporal resolution, N-1 security). Always at the top, not tied to any system.
- **Scenarios** -- Stochastic scenario definitions with probabilities and multipliers. Shared across all systems.
- **Inter-system Links** -- Cross-system links, in two sub-categories: **Transmission Lines** and **Fuel Routes**.
- **Geo Assets** -- Imported geographic files used as reference overlays.
- **System 1** (bold) -- First power system, expandable into component categories.
- **System 2** (bold) -- Second power system, and so on.

### System Component Categories

Each system contains these categories (element count shown in parentheses):

| Category | Contains |
|----------|----------|
| **Nodes** | Geographic aggregations of buses; hold demand, reserves, and losses |
| **Generators** | All generation units (renewable and thermal) |
| **Energy Storage** | Batteries and other storage |
| **Transmission Lines** | Electrical connections between buses and equipment |
| **Transformers** | Voltage conversion equipment |
| **Buses** | Electrical connection points at specific voltage levels |
| **AC/DC Converters** | Interface between AC and DC buses |
| **Freq. Converters** | Interface between different frequency systems |
| **Development Zones** | Geographic areas for new capacity installation |
| **Fuel Sources** | Fuel supply points (ports/terminals) and their per-fuel supply parameters |
| **Fuel Storages** | Fuel stockpiling facilities |
| **Fuel Routes** | Fuel transport pathways |
| **Fuels** | Fuel definitions (unit, emission factor, price) |
| **Electrolyzers** | Power-to-hydrogen conversion units |
| **EV Configuration** | Electric vehicle fleet parameters |
| **Rooftop Solar** | Distributed PV deployment settings |
| **Technologies** | Technology definitions used by investment candidates |
| **Investment Portfolio** | Investment candidates with per-node costs and limits |
| **Risk & Resilience** | Hazard scenarios for the risk analysis |


---


## Creating a System

### From the Toolbar

1. Click **Add System** in the toolbar.
2. Enter a system name (e.g., "Cuba_Western_Grid", "Jamaica").
3. Click **OK**. The new system appears in the tree with one node, "Node 0", becomes the active system, and is added to Global Settings > Systems to Simulate.
4. Drawing mode buttons are enabled once a system exists.

### From an Imported YAML

**File > Import Configuration…** (`Ctrl+O`) replaces the project with the contents of a YAML file. Each system in the file is created in the tree with all its elements.

### Adding Systems from Another Configuration

**File > Add System from Config…** adds the systems of another YAML file without removing the current ones:

1. Choose the YAML file. An "Import preview" lists the systems that will be added directly and those whose names already exist.
2. Click **Yes** to proceed.
3. For each name conflict, enter a new name. Cancelling skips that system. There is no option to replace an existing system.

Inter-system links in the file are added when both of their systems are present after the import.

**Tips:**
- System names must be unique within the project. The dialog warns you if you enter a duplicate name.
- Prefer names with letters, digits, underscores, and hyphens.


---


## Working with Several Systems

All systems are drawn on the map at the same time. The Studio keeps one system *active*: the one whose data is loaded in the Properties Panel, the Python console (`state`), and the drawing tools.

- **Click an element** of another system, on the map or in the tree, to make that system active. The map view does not move.
- **Click a system name** in the tree to open its System Settings form.
- New elements placed with the drawing tools go to the active system. A line or fuel route traced between elements of two different systems becomes an inter-system link.
- When a system is activated from the tree (for example, when adding a node to it), the map refits to that system's elements.

All system names are shown in bold; the tree does not mark the active system.

**Tips:**
- Inter-system links are listed in the tree regardless of the active system.
- The Python console's `state` variable always refers to the active system's data.


---


## Renaming a System

1. Click the system name in the tree to open the System Settings form.
2. Edit the **Name** field.
3. Press Enter or move to another field to confirm.

The rename updates the Element Tree and the system's entry in the project. It does not update inter-system links that reference the old name, nor the Global Settings > Systems to Simulate list, which Validate, Save, and Run use. A renamed system can therefore be left out of validation and runs, and its links point to a name that no longer exists. Choose final system names before validating or adding inter-system links.


---


## Deleting a System

1. Right-click the system name in the Element Tree.
2. Select **Delete System**.
3. Confirm "Are you sure you want to delete system '…'?" with **Yes**.

The system, all its elements, and every inter-system link that references it are removed, and the system is taken out of Systems to Simulate. The last remaining system cannot be deleted. System deletion is not recorded in the undo history; save before deleting.


---


## Adding Elements

### From the Toolbar (Map Interaction)

1. Select a drawing mode (Generator, Storage, Bus, Line, etc.) from the toolbar.
2. Click on the map at the desired location.
3. The element is created in the active system and its form opens in the Properties Panel.

Used for buses, equipment, fuel facilities, lines, fuel routes, and zones. See [Map Editor](map-editor.md).

### From the Element Tree (Right-Click Menu)

Some categories have an add action in their context menu:

| Category | Menu item |
|----------|-----------|
| **Nodes** | Add Node |
| **Fuels** | Add Fuel |
| **Technologies** | Add Technology |
| **Investment Portfolio** | Add Generator, Add Battery, Add Electrolyzer, Add AC/DC Converter, Add Freq. Converter, Add Transmission, Add Fuel Storage |

Stochastic scenarios are added in the Scenarios form.


---


## Deleting Elements

| Method | How |
|--------|-----|
| **Context menu** | Right-click an element on the map or in the tree and select **Delete** |
| **Delete button** | Select one or more elements in the tree and click **Delete** below the tree |
| **Properties Panel** | Some forms (line, inter-system link, zone, fuel, fuel route, electrolyzer, technology, investment entry) have their own delete button |

Deleting from the context menu or the Delete button below the tree happens immediately, without a confirmation dialog. Use **Edit > Undo** (`Ctrl+Z`) to restore it.

Deleting a node or a bus also removes the elements attached to it. A batch delete across elements of different systems is refused.


---


## Duplicating Elements

Right-click an element on the map or in the tree and select **Duplicate**. The copy receives:

- A new unique ID and name.
- The same technical parameters (capacity, costs, efficiency, etc.).
- For generators, batteries, and electrolyzers: a slightly offset map position and no bus connection, so you can place and connect it.


---


## Copy / Paste Attributes

Transfer technical parameters between elements of the same type:

1. Right-click the source element and select **Copy Attributes**. A status message confirms the copy.
2. Right-click the target element (same type) and select **Paste Attributes**.

**What is transferred:** technical properties (capacity, costs, efficiency, impedance, fuel, etc.).

**What is kept on the target:** ID, name, node, bus, line endpoints and waypoints, position, and visual style.


---


## Search and Filtering

### Tree Search

The search box at the top of the Element Tree ("Search elements...") filters elements by name as you type. It applies to all systems.

### Tree Navigation

- **Double-click** an element to center the map on it.
- Category items can be expanded and collapsed with the arrow next to them.


---


## Multi-Selection and Batch Editing

### Selecting Multiple Elements

In the Element Tree (the map selects one element at a time):

| Method | How |
|--------|-----|
| **Ctrl+Click** | Add individual elements to the selection |
| **Shift+Click** | Select a range of elements |

The selection is restricted to one element type; items of other types are deselected.

### Batch Edit Behavior

When several elements of the same type are selected, the Properties Panel shows a batch-edit form:

- Fields with identical values show that value.
- Fields with differing values show the placeholder `-- mixed values --` (or `-- mixed --` in combo boxes).
- Changing a field applies the new value to all selected elements.

### Practical Uses

- Apply the same efficiency to all batteries.
- Update fuel cost for all diesel generators.
- Change the lifetime of all generators of one type.


---


## Context Menus

### On an Element (Element Tree)

| Action | Applies to |
|--------|------------|
| View on map | Nodes: shows a temporary marker at the node centroid |
| Parse to System Elements... | Geo assets |
| Schedule interruption… | Generators, batteries, lines, transformers, converters |
| Duplicate | Most element types |
| Copy Attributes / Paste Attributes | Most element types |
| Delete | All deletable elements |

With several elements selected, the menu shows **Delete N items**.

### On an Element (Map)

**Duplicate**, **Copy attributes**, **Paste attributes**, **Delete**.

### On a Category

Only the categories listed in [From the Element Tree](#from-the-element-tree-right-click-menu) have a menu.

### On a System

| Action | Description |
|--------|-------------|
| Delete System | Remove the system and all its elements |


---


## Import and Export

**Save** and **Export As…** always write all systems of the project to one YAML file; there is no per-system export. To run a subset of systems, select them in Global Settings > Systems to Simulate.

To bring systems from another file into the current project, use [Add System from Config…](#adding-systems-from-another-configuration). **Export Project…** and **Import Project…** move a whole project, with its data files, as a `.esfexp` bundle (see [Overview](overview.md#file-operations)).
