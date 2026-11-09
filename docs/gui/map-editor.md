# Map Editor

Central workspace for placing buses and equipment, drawing transmission lines and fuel routes, defining development zones, and visualizing simulation results. Powered by Leaflet.js embedded in a QWebEngineView.


---


## Nodes, Buses, and Equipment

The Studio separates geography from electrical topology:

- A **node** is a geographic aggregation (a region or substation area). It owns one or more buses and holds demand, reserves, and losses. Nodes have no marker on the map.
- A **bus** is an electrical connection point at one voltage level. Each bus belongs to one node (its *parent node*). Lines, transformers, converters, and equipment connect to buses.
- **Equipment** (generators, batteries, electrolyzers, converters, transformers, fuel facilities) is placed at its own map position.

### Adding Nodes

A new system already contains "Node 0". To add more:

1. Right-click **Nodes** under the system in the Element Tree and select **Add Node**.
2. In the node form, click **Place** next to *Centroid (geographic reference)*, then click the map. The centroid is stored as the node's latitude and longitude. **View** briefly shows a marker at the centroid.

When you place a bus or a piece of equipment, it is assigned to the node whose centroid is nearest to the click. Nodes without a centroid are matched by the positions of their buses. Set the centroids before placing buses so the assignment is right; you can change a bus's node later with **Parent Node** in the bus form.


---


## Drawing Modes

Select a mode from the toolbar and click the map. Placement modes stay active, so you can place several elements in a row. Line, fuel route, and zone modes stay active until the trace is completed or cancelled. Press `Escape` to cancel and return to selection mode.

### Placing Buses

1. Click **Bus** in the toolbar.
2. Click the map. The bus is created at that point, in the node with the nearest centroid.
3. Configure name, voltage, frequency, current type, bus type (PQ/PV/slack), role, and demand fraction in the Properties Panel. New buses default to 220 kV and role *connection*, and take the frequency of the node's other AC buses (50 Hz on a node without buses).

### Placing Generators, Storage, and Electrolyzers

1. Click **Generator**, **Storage**, or **Electrolyzer** in the toolbar.
2. Click the map where the plant is located. The element is created at that exact point and assigned to the node with the nearest centroid.
3. If the system has buses, the element is connected automatically: equipment → line → new low-voltage bus → line → transformer → line → nearest bus above 0.48 kV.
4. The Properties Panel opens the form for the new element. New generators start as Renewable with 0 MW rated power.

Marker sizes scale with rated capacity (see Global Settings > Visual Scaling). Renewable generators use green markers and non-renewable generators gray markers by default; batteries use orange markers. Each form has an **Appearance** group to override color, size, shape, and opacity.

### Placing Converters and Transformers

- **AC/DC Conv.** and **Freq. Conv.**: click the map. If buses exist, the converter is connected between buses (bus_from → converter → bus_to). Configure its buses, rated power (MVA), and efficiencies in the form.
- **Transformer**: click the map, then set the From/To buses, voltages, rated power, impedance, and losses in the form.

### Placing on an Existing Line

Clicking on or near an existing line or fuel route while placing an element offers to connect the element to it ("Do you want to connect … to …?"). If you accept, the line is split in two at that point and both halves connect to the new element. Generators, storage, electrolyzers, and converters are not split into the line; when buses exist they get the auto-connect chain described above instead. Dragging an existing element onto a line also offers the split.

### Placing Fuel Infrastructure

- **Fuel Source** -- A fuel import point (port, pipeline terminal). Configure available fuels, import limits and costs, and supply-stress parameters.
- **Fuel Store** -- A fuel storage facility. Configure capacity, initial and minimum levels.

Both are assigned to the node with the nearest centroid.


---


## Drawing Lines

Transmission lines and fuel routes use a **polyline trace**: click the start element, optionally add waypoints, then click the end element.

### Transmission Lines

1. Click **Line** in the toolbar.
2. Click the starting bus or piece of equipment. It is highlighted, and a dashed rubber-band line follows the cursor.
3. Click on empty map space to add waypoints that shape the route.
4. Click the destination bus or piece of equipment to complete the line.
5. The Properties Panel opens the Line form.
6. Press `Escape` during the trace to cancel without creating the line.

**Key behaviors:**
- Allowed endpoints are buses, generators, batteries, electrolyzers, transformers, AC/DC converters, and frequency converters. Generators, batteries, and electrolyzers can connect to a bus, a transformer, or an AC/DC converter, not to each other. Invalid combinations are rejected with a message.
- The Line form shows the endpoints as **From bus** / **To bus** and lets you rewire them.
- Waypoints are stored in the YAML and preserved through save/load.
- Parallel lines between the same pair of buses are allowed; each is a separate element.
- If the two endpoints belong to different systems, an inter-system link is created instead. See [Inter-System Links](inter-system-links.md).

### Fuel Transport Routes

Click **Fuel Route**, click a fuel source or fuel storage, optionally click empty map space to add waypoints, then click another fuel source or fuel storage. Press `Escape` to cancel.

- Waypoints are stored with the route; **Edit Trace** in the route form reshapes it later.
- Fuel routes are drawn as dashed lines.
- Configure the fuel, capacity, transport cost, and losses in the Properties Panel.

### Snapping

A trace starts and ends only by clicking a marker of an allowed type. The registered types are bus, generator, battery, electrolyzer, transformer, AC/DC converter, frequency converter (for lines), and fuel source and fuel storage (for fuel routes). Lines store their endpoints as references to these elements, so when you drag an element, the line ends attached to it follow.


---


## Drawing Zones

Development zones define geographic areas where new generation capacity may be installed.

1. Click **Dev. Zone** in the toolbar.
2. Click the map to add polygon vertices.
3. Close the polygon by clicking the first vertex.
4. The Properties Panel opens the Zone form. Configure the technology, node, interconnection costs, target bus, and allowed technologies.

Zones are drawn with a semi-transparent fill. Overlapping zones are allowed.


---


## Editing Elements

### Moving Elements

In selection mode, drag any equipment or bus marker to move it. Lines and fuel routes attached to the element follow it. Moving an element does not change its node or bus. Dropping it on a line offers to split the line (see [Placing on an Existing Line](#placing-on-an-existing-line)).

For large systems (more than 200 point elements), markers are drawn on a canvas and can only be dragged after zooming in to level 15 or closer.

### Editing Line Routes

1. Select a line or fuel route.
2. In the Properties Panel, click the **Edit Trace** toggle button.
3. The route's vertices become draggable handles. Drag the midpoint handles to add vertices.
4. Click **Edit Trace** again to finish.

### Editing Zone Boundaries

1. Select a zone.
2. In the Properties Panel, click the **Edit Polygon** toggle button.
3. Drag the vertices to reshape the boundary.
4. Click **Edit Polygon** again to finish.


---


## Selecting Elements

- **Click** an element on the map to select it. Its form appears in the Properties Panel. The map selects one element at a time.
- In selection mode, **click empty map space** to clear the selection.
- **Double-click** an item in the Element Tree to center the map on that element.
- **Ctrl+Click** or **Shift+Click** in the Element Tree to select several elements of the same type for batch editing.
- **Right-click** an element on the map or in the tree for the context menu.

Clicking an element of another system switches the active system without moving the map.


---


## Duplicate, Copy, and Paste

Use the right-click menu on the map or in the Element Tree:

- **Duplicate** -- Creates a copy with a new ID and name. For generators, batteries, and electrolyzers the copy is offset on the map and disconnected from any bus, so you can place and connect it.
- **Copy Attributes** -- Copies the element's attributes to an internal clipboard. The status bar confirms the copy.
- **Paste Attributes** -- Applies the copied attributes to another element of the same type (enabled only when the types match).

Copy skips the identity and position fields: ID, name, node, bus, line endpoints and waypoints, coordinates, and visual style. Everything else (capacity, costs, efficiency, fuel, impedance, etc.) is pasted.


---


## Context Menu

Right-click a marker or line on the map:

| Action | Description |
|--------|-------------|
| **Duplicate** | Create a copy (see above) |
| **Copy attributes** | Copy technical attributes |
| **Paste attributes** | Apply copied attributes (same type required) |
| **Delete** | Remove the element immediately (undo with `Ctrl+Z`) |

The Element Tree has a longer menu; see [System Management](system-management.md#context-menus).


---


## Map Controls

### Navigation

| Action | How |
|--------|-----|
| Zoom in/out | Scroll wheel, or double-click to zoom in |
| Pan | Click and drag on empty map space |
| Fit all elements | Happens automatically when a configuration is loaded or a system is switched from the tree |
| Center on element | Double-click an item in the Element Tree |

### Base Maps

Switch the background with the **Base Map** dropdown in the toolbar:

| Base Map | Description |
|----------|-------------|
| **OpenStreetMap** | Standard street map with labels. |
| **Satellite** | Aerial imagery. Useful for identifying terrain, coastlines, and existing infrastructure. |
| **Terrain** | Topographic map with elevation shading. |
| **Dark** | Dark minimalist map. |
| **Offline** | Country outlines from bundled Natural Earth data. Works without an internet connection. |

The toolbar choice applies to the current session only. The base map used at startup is set in **File > Preferences… > Map > Default base map**.


---


## Layer Filtering

The **Layer** dropdown controls what is drawn on the map:

| Layer | Visible Elements |
|-------|-----------------|
| **All** | Every element |
| **Electrical** | Generators, batteries, lines, transformers, buses, converters, electrolyzers, development zones, results overlays |
| **Primary Energy** | Fuel sources, fuel storages, development zones |
| **Results** | Results overlays from the Map Results panel only |

Fuel routes are visible in every layer. Layer filtering only affects the map; the Element Tree always shows all elements.


---


## Importing Geographic Data

**File > Import Geo Asset…** loads a GeoJSON, Shapefile, KML, KMZ, or GeoPackage file as a reference overlay. Formats other than GeoJSON require `geopandas`.

1. The file appears on the map and under **Geo Assets** in the Element Tree. Nothing is added to the system yet.
2. Right-click the asset and select **Parse to System Elements...** to convert its features into buses, lines, generators, and other elements.

Files in another coordinate reference system (for example a UTM shapefile, or a GeoJSON with a projected `crs` member) are reprojected to WGS84 (EPSG:4326) on import. A file without a CRS is taken as WGS84 longitude/latitude, with a warning in the log.


---


## Coordinate System

All positions use **WGS84** (EPSG:4326) latitude and longitude in decimal degrees. Positive latitudes are north of the equator; positive longitudes are east of the prime meridian. Coordinates are saved in the YAML configuration.


---


## Element Visibility and Scaling

- Generator and battery marker sizes scale with rated power or capacity, between the minimum and maximum sizes set in Global Settings > Visual Scaling. A size set in an element's Appearance group overrides the automatic size.
- Transmission line widths scale with capacity in the same way.
- Hovering a marker shows a tooltip with its name and size.


---


## Performance Considerations

- With more than 200 point elements, the map switches to canvas markers; detailed, draggable markers appear from zoom level 15.
- Use layer filtering to reduce what is drawn.
- Satellite base maps use more bandwidth than the other layers.
- Zoom in before placing elements to position them precisely.
