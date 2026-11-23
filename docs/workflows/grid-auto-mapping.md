# Grid Builder

Automated power grid construction from open geospatial databases. Access via **Workflows > Grid Builder...**.

The Grid Builder creates a power system network (buses, generators, batteries, transmission lines, transformers, converters and, optionally, fuel infrastructure) from publicly available geographic databases. Starting from a study area drawn on the map or taken from an imported GeoAsset, it fetches real-world infrastructure, normalizes it into a common intermediate format, removes cross-source duplicates, places nodes, and builds the network from the real substation and line topology. A final step forecasts hourly demand per node and splits it among the buses.

The wizard has three steps:

1. **Region & Fetch**: define the study area, select data sources and filters, and download infrastructure data.
2. **Build & Connect**: choose the target system, create nodes (spatial clustering or manual), and build and simplify the network.
3. **Demand**: forecast hourly demand per node and distribute it among each node's buses.

The build always imports the real topology ("faithful import"). It does not fabricate connectivity: there is no automatic bridging of isolated components, no node star-coupling and no generator step-up chains, so island subsystems are kept as islands.

The spatial computations live in the `esfex.visualization.workflows.grid_mapping_fetchers`, `grid_mapping_clustering` and `grid_mapping_builder` modules, which can also be called from scripts. See [Scripting](#scripting).


---


### Step 1: Region & Fetch

Define the study area, configure the data sources and filters, then click **Fetch Data**.

**Region:**

The study area is set with the shared domain selector (see [Study area selector](index.md#study-area-selector)): either **Draw domain** (click vertices on the map and close the polygon) or **Use imported area as domain** > **Apply as domain** with an imported GeoAsset. Multi-ring domains (for example an archipelago) are kept. The status line shows the vertex count and an approximate area computed from the polygon's bounding box (see [GM-4](#gm-4-bounding-box-area)).

The fetchers query by bounding box. After download, a point-in-polygon test (ray casting) clips the results to the actual domain: point features must lie inside a ring, and lines must have at least one vertex inside. Irregular shapes such as coastlines or administrative borders therefore work as expected.

**Data sources:**

All four sources are enabled by default. Combining them (OSM topology, plant databases, and GridFinder gap-filling) gives the most complete network.

| Source | Default | Description |
|--------|---------|-------------|
| **OpenStreetMap** | Enabled | Crowd-sourced geographic data. Provides substations, generators, transmission lines, transformers, AC/DC converters, energy storage, fuel entry points and fuel storage. The authoritative source for network topology. Fetched from the Overpass API or from a local Geofabrik extract (see [Country-scale OSM (Geofabrik)](#country-scale-osm-geofabrik)). |
| **WRI Global Power Plant Database** | Enabled | World Resources Institute database (~30,000 plants worldwide) with capacity and fuel type. |
| **GEM Global Power Plants (2025)** | Enabled | Global Energy Monitor database (February 2025 update): capacity, fuel, commissioning year, owner and technology sub-type. Only units with status "operating" (or a blank status) are kept. |
| **GridFinder (predicted lines)** | Enabled | ML-predicted line routes from satellite night-lights (Zenodo dataset). Geometry only: voltage and capacity are derived from the buses each line connects. GridFinder lines are kept only where OSM has no line, and their endpoints snap to buses within 2 km, so they fill gaps in OSM coverage. |

**Filters:**

| Parameter | Range | Default | Description |
|-----------|-------|---------|-------------|
| Min voltage | 10 -- 750 kV, in steps of 0.1 kV | 66 kV | Substations and lines with a known voltage below this value are excluded at fetch time, as are substations tagged `substation=distribution` without a voltage when the floor is above 60 kV. After the build, buses that ended up below the floor are collapsed into the nearest bus at or above it (see [Build pipeline](#build-pipeline)). The 66 kV default keeps the sub-transmission layer that holds regional grids together; a higher floor can split a regional grid into disconnected islands. |
| Min gen capacity | 0 -- 10,000 MW | 0.3 MW per kV of Min voltage (20 MW at 66 kV) | Generator capacity floor. The default follows Min voltage (0.3 MW/kV, rounded) until you edit the value by hand, which pins it. When the value is above 0, the build drops every generator below it, **including generators with unknown capacity** (0 MW in the source). Set it to 0 to include every generator. |

**Element types to import:**

| Element Type | Default | OSM Tags |
|--------------|---------|----------|
| Substations / Buses | Enabled | `power=substation` |
| Generators | Enabled | `power=generator`, `power=plant` |
| Transmission Lines | Enabled | `power=line`, `power=cable` |
| Transformers | Enabled | `power=transformer` |
| Energy Storage | Enabled | `power=storage` |
| AC/DC Converters | Enabled | `power=converter` |
| Fuel Entry Points | Disabled | `industrial=refinery`, `industrial=fuel_depot`, `man_made=oil_terminal`, fuel-cargo ports |
| Fuel Storage | Disabled | `man_made=storage_tank` (fuel content), `industrial=tank_farm` |

**Bus creation strategy:**

| Strategy | Description |
|----------|-------------|
| **One bus per voltage level** (recommended, default) | Multi-voltage substations produce one bus per voltage level, connected by auto-created transformers. |
| **One bus per substation** | Each substation becomes a single bus at its highest voltage. |

**Fetching:**

Click **Fetch Data** to start one download thread per enabled source. Each source shows its own progress bar and status message. A failed source is reported in the error log but does not block the others. After completion the button changes to **Re-fetch Data**.

When all sources finish, a background worker:

1. **Clips to the domain** (the APIs return the larger bounding box).
2. **Removes cross-source duplicates** (see [Deduplication Algorithm](#deduplication-algorithm)).
3. **Summarizes** the feature counts by type.

There is no manual review table: the fetched features go straight to Step 2.

**Tips:**

- Keep OSM and GEM enabled. OSM provides the network topology (substations, lines); GEM adds capacities and plants missing from OSM.
- Enable fuel entry and fuel storage only if you plan to model fuel supply chains. The OSM fetch then also downloads major roads near the fuel features.
- For large domains, install `osmium` so the OSM data comes from a local Geofabrik extract instead of the rate-limited Overpass API.


#### Country-scale OSM (Geofabrik)

The OSM backend is chosen automatically:

- **Geofabrik**: used when the domain spans 2 degrees or more (latitude or longitude) and is a polygon. The smallest Geofabrik regional `.osm.pbf` extract covering the domain is downloaded once, cached under the ESFEX data directory (`data/geofabrik`), and filtered locally with `pyosmium`. A cached extract younger than 30 days is reused. The extracted elements go through the same conversion and filters as the Overpass path, so the resulting features are the same.
- **Overpass API**: used for smaller domains.

Set the environment variable `ESFEX_OSM_BACKEND` to `geofabrik`, `overpass` or `auto` (default) to override the choice. The Geofabrik backend needs the `osmium` Python package (`pip install osmium`); if it is not importable, the fetch falls back to Overpass and logs a warning.


### Step 2: Build & Connect

Choose the target system, create nodes, and build the network. The build runs on a background thread; while it runs, the **Build Network** button becomes **Cancel**. A cancelled build keeps the partial network.

**Target system:**

Select an existing system from the dropdown, or click **New System...** to create one. The wizard switches the editor to the selected system before building. Building again after going back restores the system to its state before the first build, so elements are not duplicated.

**Node placement:**

With **Automatically create nodes from spatial clustering** checked (default), node locations come from K-means clustering of the fetched features. The Grid Builder replaces any existing nodes of the target system (for example the default "Node 0").

| Setting | Range | Default | Description |
|---------|-------|---------|-------------|
| Minimum nodes | 1 -- 5000 | 2 | Lower bound on the number of nodes |
| Maximum nodes | 1 -- 5000 | 20 | Upper bound on the number of nodes (also capped by the number of features) |

**Clustering criteria (select one or more):**

When several criteria are checked, each runs independently and the candidate positions are merged by a consensus K-means pass (pool all candidate centroids, re-cluster to the mean of the individual k values).

| Criterion | Method | Description |
|-----------|--------|-------------|
| **Infrastructure Density** | K-means on infrastructure positions | Clusters the positions of substations, generators, batteries, transformers, converters and fuel entries. Places nodes where physical infrastructure is concentrated. Default criterion. |
| **Demand Proxy (Building Footprints)** | Fetch + cluster building centroids | Downloads building footprints from Overture Maps, Microsoft ML Buildings or Google Open Buildings (tried in that order). Clusters by building density to approximate demand hotspots. Requires an additional download. Falls back to Infrastructure Density if building data is unavailable. |
| **Regional Balance (Uniform Coverage)** | Grid-seeded K-means | Seeds the initial centroids on a regular grid over the study area, then runs K-means. Gives even geographic coverage regardless of infrastructure density. |

The number of clusters is chosen with the elbow method (see [GM-3](#gm-3-elbow-method-knee-detection)). Clustering works on coordinates projected to approximate kilometers (see [GM-5](#gm-5-utm-approximation-for-metric-distances)).

**Node naming:** clustered nodes are named after the place they fall in:

1. Nominatim (OpenStreetMap) reverse geocoding, at about one request per 1.1 s and stopping after 80 calls or 30 s.
2. For the remaining nodes, an offline point-in-polygon lookup in the Natural Earth admin-1 boundaries (state, province, prefecture). They are not shipped with ESFEX nor downloaded on demand: put the 1:10m *Admin 1 – States, Provinces* shapefile from [naturalearthdata.com](https://www.naturalearthdata.com/downloads/10m-cultural-vectors/) at `$ESFEX_DATA_ROOT/data/gridded/shapefiles/ne_10m_admin_1/ne_10m_admin_1_states_provinces.shp` (with its `.shx`, `.dbf` and `.prj`; `ESFEX_DATA_ROOT` defaults to `~/.esfex/data`). Without it this step is skipped, and ESFEX logs a warning once with that path.
3. Otherwise "Node i".

Repeated names get a numeric suffix ("Springfield", "Springfield 2").

**Manual nodes:**

With auto-creation unchecked, a node editor appears: a table (Name, Lat, Lng) with **Add node (click map)**, **Add node (row)** and **Delete selected**. Each node's Voronoi territory, clipped to the domain, is drawn live on the map. Buses are assigned to the nearest node centroid, so the territories show where elements will land. At least one node must exist before building.

**Build options:**

| Setting | Range | Default | Description |
|---------|-------|---------|-------------|
| Generate availability profiles | on/off | on | After the build, write one availability CSV per generator (see [Availability profiles](#availability-profiles)). |
| Station merge radius | 0.05 -- 5 km | 1.0 km | Buses of the same voltage within this radius are treated as one physical station and merged. This is what connects line endpoints to their substation. It is a "same station" tolerance, not a reach distance: lines are never snapped to a distant bus. Increase it if large substations split into separate components; decrease it if two distinct stations merge. |
| Simplification level | 0 -- 2 | 0 | Simplification applied after the build, before the network is drawn (see below). |
| Drop isolated < N bus(es) | 1 -- 50 | 2 | Connected components with fewer buses than N are removed. The largest component is always kept. Set to 1 to keep everything. |

**Simplification levels:**

| Level | Effect |
|-------|--------|
| 0 -- Cleanup only | Repairs broken references and self-loops, removes orphaned fuel infrastructure and buses with nothing attached. |
| 1 -- Aggregate equipment & parallel lines | Level 0, plus merging of parallel lines and aggregation of equivalent generators and batteries in place. |
| 2 -- Equivalent network (one bus per node and voltage level) | Level 1, plus an equivalent-network reduction (PyPSA-style): each node becomes one bus per voltage level that carries lines, with the generation and demand of that level, and the buses are joined by one equivalent branch per connected pair (see [Equivalent branches](#equivalent-branches) below). A node containing an AC/DC or frequency converter keeps one bus per side of the converter. |

<a id="voltage-levels"></a>**Voltage levels at level 2.** A node keeps the voltage levels its lines run at: the voltages of lines whose two buses are at that voltage. A line between buses of two voltages (a bus whose tag disagrees with its only line, a generator terminal wired to its bus by a line) does not make a level, and its bus joins the level it is wired to. Its transformers between two levels stay transformers: those of the same pair of levels are combined in parallel (MVA summed, X and R as for the lines below), from the higher to the lower voltage, and named after the transformer when there is one. A bus at a level without lines (a generator terminal, a bus without a voltage) joins the nearest kept level through its transformers and lines, so a generator behind a 13.8/110 kV step-up transformer lands on the 110 kV bus. A level the data does not connect to the node's main levels, inside the node or through other nodes (OSM often lacks the transformer), joins the main level of the nearest voltage: no transformer is invented. The main levels are those on the largest connected network; earlier, the highest level anchored the node, so a stray 500 kV fragment of a few buses absorbed a node's whole 275–77 kV network. The highest level keeps the node's name and the others add their voltage (`Node A (110 kV)`). A node's demand is shared between its levels as its buses carried it. All the buses of a node are placed at the position of its highest level, so the single-line diagram draws the node as one substation with its transformers inside; the equivalent branches keep the length between the buses they stand for. Earlier versions collapsed each node to one bus and dropped its transformers, so 220 and 110 kV lines met on the same bus.

**Merged units.** Every merge of generators or batteries (these levels, the equipment and small-generator merges of the Validate dialog's reduction levels) combines their parameters the same way. Rated power, energy capacity, charge/discharge limits and the start-up cost ($ per start) add up. Values per MW of rating or as a fraction of it (`min_power`, ramps, inertia H, efficiencies, costs in $/MWh, $/MW·yr and $/MW, degradation, min up/down times, governor constants, risk derating) are averaged with rated power as the weight, battery efficiencies and initial SOC with energy capacity. A ramp of 0 (no limit) counts as a full rating per hour, so the merged ramp is 0 only if no unit was limited; the droop is the parallel equivalent ΣP / Σ(P/R). The lifetime is the shortest, the age the oldest and the battery depth of discharge the smallest. Studio versions up to 0.3.0 summed `min_power`, ramps, inertia and decommissioning cost, so four units at `min_power` 0.4 became 1.6.

**Lines without an impedance.** Parallel, series and equivalent-branch combinations give a line with no reactance (an OSM line without a voltage tag, a bridge line) the R, X and B of its length and voltage, as the builder derives the others: the standard line type of its voltage (or its own `reactance_per_km`), on the system base `ac_power_flow.base_mva`, divided over its circuits. The length is its `length_km`, else the distance from its end buses through its waypoints; the voltage is its own, else the higher of its end buses. In an equivalent branch, a line with neither a length nor end-bus coordinates spans the distance between the two node buses. A line with a reactance but no resistance gets the R/X of the standard line type of its voltage (0.2 at 110 kV, 0.15 at 220 kV). Earlier versions left such lines out of the combination, and gave a node pair where no line had a reactance 0.01 p.u. whatever its length.

<a id="equivalent-branches"></a>**Equivalent branches.** Level 2 joins two reduced buses by one branch standing for every line between them, and every transformer between two buses of the same level, in parallel, all with the same set of branches:

| Value | Rule |
|-------|------|
| Capacity | Sum of the lines' ratings and the transformers' MVA. |
| X | \(1/x_{eq} = \sum 1/x_i\). |
| R | \(r_{eq} = x_{eq}^2 \sum r_i / x_i^2\): the DC flow splits as \(f_i = f\,x_{eq}/x_i\), so the branch loses what its lines lose, \(\sum r_i f_i^2\). It equals \(1/r_{eq} = \sum 1/r_i\) only when every line has the same R/X. A transformer's R is its `losses_fraction` × impedance, as the model derives it. |
| B | Sum. |
| Voltage | The highest voltage of its lines (the reduced buses' voltage for a transformer-only branch). |
| Circuits | Sum of its lines' circuits. |
| Length | Distance between the two reduced buses (map and costs per km). |

Each line's p.u. values are on its own voltage base, as the model reads them, so lines of different voltages combine directly. Parallel lines between the same two buses (level 1) use the same X and R rules. In earlier versions the R of an equivalent branch was \(1/\sum 1/r_i\) over the lines that had a resistance (one lossy line beside lossless ones set the branch R), transformers had R = 0, a line with neither a length nor coordinates added its capacity but no admittance, and the branch was labelled with the higher node-bus voltage and one circuit (two 220 kV circuits and a 110 kV one, 1543 MW, read as a single circuit).

Levels 3 and 4 of earlier versions ran the same reduction as level 2 and are no longer offered; `apply_simplification_level` still accepts them as level 2. The selective L1–L4 suggestions of the Validate dialog's **Network reduction (levels)** check are a different tool (radial, series, voltage and node collapse suggestions you apply one by one).

#### Build pipeline

`build_grid_from_features` creates the elements in this order:

| Phase | Input | Result |
|-------|-------|--------|
| 0. Voltage tags | `substation`, `transformer`, `line` | Station voltages are checked against the lines (see [Voltages](#voltages)). |
| 1. Substations | `substation` | Buses (one per voltage level or per substation, depending on the strategy). In per-voltage mode, transformers between the levels. |
| 1.5. Fuels & technologies | Generator fuels | `GuiFuel` and `GuiTechnology` objects (see [Fuel & Technology Mapping](#fuel-technology-mapping)). |
| 2. Generators | `generator` | Generators with mapped fuel, technology, capacity and initial age (from the commissioning year). Generators below Min gen capacity are dropped first. |
| 3. Batteries | `battery` | Batteries with power rating and energy capacity (explicit, or a 4-hour default duration). |
| 4. Lines | `line` | OSM segments are merged into contiguous lines and split where a substation sits on them. Endpoints share a bus only when they coincide (0.05 km); GridFinder endpoints snap within 2 km. Capacity from tags or the standard-line-type estimate (see [Line Capacity Estimation](#line-capacity-estimation)). Self-loops are skipped. |
| 5. Transformers | `transformer` | Transformers with HV and LV buses. A side with no voltage tag snaps to the nearest bus of any voltage; when that is the other side's bus, the transformer is skipped (the station's levels are tied by its auto-transformer instead). |
| 6. Converters | `converter` | AC/DC converters with AC-side and DC-side buses. |
| 7. Fuel entries | `fuel_entry` | Fuel entry points with mapped fuel type. |
| 8. Fuel storage | `fuel_storage` | Fuel storage facilities with mapped fuel type. |
| 8.4. Voltage propagation | Buses and lines with no voltage tag | An untagged bus takes the voltage of the lines and neighbouring buses it touches (fixed-point relaxation); untagged lines take the voltage of their endpoints, and their rating and impedance (from the length measured at phase 4, for all the line's circuits together) are recomputed. A bus no tagged line reaches gets medium voltage (see [Voltages](#voltages)). Transformer and converter voltages follow their buses. |
| 8.5. Voltage floor | Buses below Min voltage | Generators and batteries on sub-floor buses move to the nearest bus at or above the floor; the sub-floor buses and their lines and transformers are removed. Buses not connected to any tagged voltage (an island's grid, a loose plant) are kept. |
| 9. Fuel consistency | All generators | Every generator fuel exists in the catalog and is supplied. |
| Station clustering | All buses | Same-voltage buses within the station merge radius are merged; internal self-loops are dropped. |
| Co-located voltage levels | Buses of different voltage at one station | A transformer is inserted between voltage levels that sit within the station merge radius. |
| Frequency consistency | All buses | Missing frequencies are filled from the location (50/60 Hz: coarse outlines of the 60 Hz countries, which follow the borders with their 50 Hz neighbours, e.g. Brazil/Peru 60 Hz but Bolivia, Paraguay, Argentina, Uruguay and Chile 50 Hz, Saudi Arabia 60 Hz but the other Gulf states 50 Hz, Jamaica and most of the Lesser Antilles 50 Hz, Japan split at the Fossa Magna), each AC-connected area gets one frequency, and equipment and AC lines are aligned with their bus. Features without a frequency tag (GridFinder lines, WRI/GEM plants, untagged OSM elements) take the frequency of their location; earlier versions gave GridFinder lines 50 Hz, so on a sparsely mapped 60 Hz grid such as Cuba their buses outvoted the substations and the whole network was built at 50 Hz. On save, the system's `frequency_nominal` becomes the frequency its AC buses run at (see [Frequency Stability](../formulation/frequency-stability.md)). |

#### Voltages

The builder takes voltages from the data and never fills a gap with a fixed number. The `voltage` tag is read as mappers write it: a list separated by `;`, `,` or `/`, in volts or with a unit (`110 kV`, `13,8 kV`, `220/110 kV`, where the unit applies to the whole list), with a circuit prefix (`2x220000`) or repeated levels, as the OSM cleaning of PyPSA-Eur and PyPSA-Earth also accepts. Earlier versions read only plain numbers, so `110 kV` counted as untagged. Words such as `medium` or `high` stay unknown (PyPSA turns them into fixed numbers); the rules below resolve them. A substation, transformer or converter without a `voltage` tag enters the build with an unknown voltage (earlier versions read 220 kV off `substation=transmission`, 110 kV off `sub_transmission` and 33 kV off `distribution`, and gave an untagged transformer secondary 110 kV and an untagged converter 220 kV, the same numbers in every country). Unknown voltages are then resolved in this order:

1. **From the lines.** A bus takes the voltage of the tagged lines it touches, and the value flows along untagged lines (phase 8.4). A line end snaps to a bus of its own voltage or to an untagged one, so an untagged bus can collect lines of several voltages: an untagged substation with its 220 and 110 kV lines, or a chain of untagged buses between a 220 kV and a 110 kV line. The bus takes the highest voltage, and each other voltage of its lines gets a bus of its own at the same place, in the same node, with those lines moved onto it; the co-located voltage levels step then joins them with the substation's transformer. The build report lists these buses. Earlier versions left the other lines on the higher-voltage bus, joining two voltages with no transformer (Boca de Jaruco and TE Nuevitas, Cuba).
2. **Medium voltage.** A bus no tagged line reaches gets the standard medium-voltage level (10 to 60 kV) that the fetched data tags most often, or, when it tags none, the standard default of its frequency: 33 kV at 50 Hz (IEC practice), 34.5 kV at 60 Hz (ANSI practice). OSM tags nearly every transmission line (94 % of the Cuban lines), so what stays untagged and unreached is distribution or an insular grid. Where a bus's whole component (over lines, transformers and converters) knows no voltage, the voltage floor keeps it: an island has no HV grid to fold into. Earlier versions set these buses to 220 kV, so Isla de la Juventud, a 34.5 kV system, became a 220 kV one.

Before any bus is created, station tags that the lines contradict are corrected, at high voltage (60 kV and above) only:

- **Line consensus.** A substation or transformer tagged with one level, whose own lines (at least two, each end counted for the nearest station within the station merge radius) all carry another single level and none carries its own, takes the lines' level.
- **Level no line carries.** Grid voltages follow standards: the builder keeps the nominal system voltages of IEC 60038 and ANSI C84.1, plus national levels such as Japan's 77/154/187 kV (`esfex.visualization.workflows.voltage_standards`). A standard level can still be wrong for a country (138 kV is used in Jamaica and the Dominican Republic but not in Cuba's 220/110 kV grid), so a level no line in the data carries is resolved in this order:
    1. *Name.* When the station's name states another level, the name wins: "Taguasco 13.8 kV", tagged 138 kV, becomes 13.8 kV.
    2. *Name repeats the tag.* "Ciudad Libertad 138 kV" means a high-voltage level was intended, so step 3 is skipped.
    3. *Misplaced decimal.* When the level divided or multiplied by 10 is a standard voltage the data uses, and the station is tagged `substation=distribution` or `minor_distribution` (or its level is not a standard voltage at all), the decimal is moved.
    4. *Nearest line level.* Otherwise the station takes the nearest level the lines carry, when within 30 %: Ciudad Libertad, San José PCC and La Mambisa (132 kV) become 110 kV.

  Levels the lines carry are never changed.

Medium voltage is left as tagged: generator terminals and distribution levels legitimately have no lines in a transmission fetch.

Every voltage assigned or corrected is listed in the build summary under **Voltage decisions**: corrections station by station with the old and new level and the reason, medium-voltage assignments grouped by node, and inference from tagged lines as a count. `build_grid_from_features` returns the same list as `ParseResult.voltage_decisions` (one dict per decision: `element`, `id`, `name`, `node`, `lat`, `lng`, `kv`, `was`, `rule`, `detail`), with `rule` one of `inferred_from_lines`, `isolated_mv`, `unreached_mv`, `line_consensus`, `name_level`, `decimal_slip` or `off_standard_level`.

The build worker then runs:

1. **Parameter inference** (`infer_electrical_params`): fills missing line and transformer ratings and raises (never lowers) a rating below the worst-case flow across the branch. A raised line is treated as extra parallel circuits of its type (R and X divided, B multiplied by the rating ratio). Its counts and notes appear under *Parameter Inference* in the build result.
2. **Simplification** at the selected level.
3. **Isolated-component cleanup** (`drop_isolated_components`, keeping the largest component).
4. **Node pruning**: nodes whose only buses were removed in the cleanup are deleted, so every node keeps at least one bus. Nodes that never had a bus are kept.
5. **Dangling-reference sweep** (`drop_dangling_refs`).
6. **Availability profiles**, if enabled.

The result panel shows the build summary, the simplification log, the isolated-component cleanup, and the time spent in each phase.

#### Availability profiles

With **Generate availability profiles** checked, the build writes `<system>_<instance_id>_availability.csv` for each generator into the project's `availability/` folder (the folder the project was loaded from; `./availability/` when the project has no file yet) and assigns it to the generator. The file name carries the system, as the demand files do, so the systems of a project do not replace each other's profiles. Thermal, hydro, geothermal and biomass profiles are synthetic. Wind and solar use weather-based capacity factors (Open-Meteo, via `windrex`/`solarex`), which adds time per unique location. One request per ~11 km cell fetches the weather for both; the downloads are paced to Open-Meteo's free-tier limit of calls per minute (one location-year costs about 26 calls) and cached on disk (`~/.cache/esfex/weather/open_meteo`, or the directory in `ESFEX_WEATHER_CACHE`), so a rebuild fetches only the missing locations. When the hourly or daily quota is used up, the downloads stop with a warning in the log; run the build again later to fetch the rest. A wind or solar generator with no weather data gets no profile; there is no flat fallback. The weather comes in UTC; the profiles are saved on the [model clock](../user-guide/configuration.md#model-clock), which the build sets (when it is still `auto`) to the standard time of the zone holding the most nodes and records in the project's global settings. The demand step then writes demand on the same clock.

**Tips:**

- Start with the default Infrastructure Density criterion. Add Demand Proxy or Regional Balance only if the default placement is unsatisfactory.
- For island systems, set minimum nodes to 1. National models can need hundreds of nodes; raise the maximum accordingly.
- Check the build log. Warnings list features that could not be placed (for example lines with no geometry) and the effect of each pipeline phase.


### Step 3: Demand

Forecast hourly demand for each node and split it among the node's buses. The same panel is available for a single node from the node attributes panel (**Forecast** button). It has four parts:

1. **Demand Forecast**: country (with **Auto-detect**), base year, horizon, ML engine, national demand, GDP scenario (SSP1--SSP5), elasticity and efficiency, then **Fetch and Forecast**.
2. **Forecast Results**: Node, Peak (MW), Annual (GWh) and Load Factor per node.
3. **Validate against observed demand** (optional): import an observed hourly CSV per node, compare, and optionally apply a month-hour correction.
4. **Bus Distribution**: split each node's demand among its buses (spatial demand by default, or a building-footprint proxy), then **Apply Demand & Fractions**, which writes `demand_<system>_<node>.csv` files under the project's `demand/` folder.

See [Demand Forecast & Bus Distribution](demand-distribution.md) for the parameters and methods.


---


## Data Sources


### GridFeature Schema

All fetchers normalize raw source data into a common `GridFeature` dataclass before any processing:

| Field | Type | Description |
|-------|------|-------------|
| `source` | str | Origin database: `"osm"`, `"wri"`, `"gem"` or `"gridfinder"` |
| `feature_type` | str | Element category: `"substation"`, `"generator"`, `"line"`, `"transformer"`, `"battery"`, `"converter"`, `"fuel_entry"`, `"fuel_storage"`, `"road"` |
| `name` | str | Feature name from the source database |
| `latitude` | float | WGS-84 latitude (decimal degrees) |
| `longitude` | float | WGS-84 longitude (decimal degrees) |
| `voltage_kv` | float | Primary voltage in kV (0 = unknown) |
| `voltage_kv_secondary` | float | Secondary voltage for multi-voltage substations (0 = single voltage) |
| `capacity_mw` | float | Rated capacity in MW or MVA (0 = unknown) |
| `frequency_hz` | float | Mains frequency in Hz (0 = unknown: the bus takes the frequency of its location) |
| `current_type` | str | `"AC"`, `"DC"` or `"AC_DC"` |
| `fuel` | str | Fuel type for generators (e.g., `"Solar"`, `"Natural Gas"`, `"Diesel"`) |
| `gen_type` | str | `"Renewable"`, `"Non-renewable"` or `"Storage"` |
| `energy_mwh` | float | Energy capacity for batteries in MWh (0 = unknown, builder defaults to 4h duration) |
| `line_coords` | list | Line geometry as `[(lat, lng), ...]` waypoints |
| `num_circuits` | int | Number of parallel circuits (lines only, default 1) |
| `operator` | str | Plant owner or grid operator |
| `commissioning_year` | int | Year commissioned (0 = unknown) |
| `technology` | str | Technology sub-type (e.g., `"CCGT"`, `"Onshore"`, `"Offshore"`) |
| `raw_tags` | dict | Raw source tags |
| `osm_id` | str | OpenStreetMap element ID (e.g., `"node/123456"`) |
| `include` | bool | Whether the builder uses the feature (default `True`; the GUI no longer exposes a toggle) |


### Fuel & Technology Mapping

The builder creates a fuel for each distinct generator fuel, and a technology for each (technology, fuel) pair. Fuel names are normalized to canonical keys with an alias table; a generator with no detected fuel goes to `other`.

**Fuel alias table (selected entries):**

| Input variants | Canonical key | Created fuel |
|---------------|---------------|-------------|
| solar, solarpv, pv, photovoltaic, sun | `sun` | Sun |
| wind, eolica, eolico | `wind` | Wind |
| hydro, hydroelectric, hydropower, water | `water` | Water |
| naturalgas, gas, ng, gasnatural | `naturalgas` | Natural Gas |
| diesel, gasoil, dieselengine | `diesel` | Diesel |
| oil, fueloil, hfo, heavyfueloil, residualfueloil, petroleum, crude, bunker, mazut, steamoil | `fuel_oil` | Fuel Oil |
| coal, carbon, petcoke | `coal` | Coal |
| nuclear, uranium | `nuclear` | Nuclear |
| biomass, biomasa | `biomass` | Biomass |
| biogas | `biogas` | Biogas |
| waste, residuos | `waste` | Waste |
| geothermal, geotermica | `geothermal` | Geothermal |
| otec | `otec` | OTEC |
| other, otro, otros | `other` | Other |

An unrecognized fuel keeps its own name and gets the `other` numeric defaults.

**Default fuel parameters:**

| Canonical Key | Unit | Emission Factor (tCO2/unit) | Energy Content (MWh/unit) | Base Price ($/unit) |
|--------------|------|----------------------------|--------------------------|-------------------|
| `naturalgas` | MMBTU | 0.202 | 0.293 | 4.0 |
| `coal` | kTon | 0.341 | 8.14 | 60.0 |
| `diesel` | kTon | 0.267 | 11.63 | 600.0 |
| `fuel_oil` | kTon | 0.279 | 11.30 | 450.0 |
| `nuclear` | kgU | 0.0 | 45,000.0 | 0.006 |
| `biomass` | kTon | 0.0 | 4.5 | 40.0 |
| `biogas` | Nm3 | 0.0 | 0.006 | 0.3 |
| `waste` | kTon | 0.33 | 3.0 | -20.0 |
| `other` | MWh | 0.5 | 1.0 | 50.0 |

Renewable fuels (sun, wind, water, geothermal, otec) have no unit, no emission factor and zero price.

**Technology catalog:**

Each generator is classified into a technology from its canonical fuel, the technology hint of the source (GEM technology column, OSM `generator:method`) and its capacity:

| Technology | Fuel | Efficiency (rated / min) | Lifetime (years) | Assigned when |
|-----------|------|--------------------------|------------------|---------------|
| Solar PV | sun | 1.00 / 1.00 | 25 | always for sun |
| Onshore Wind | wind | 1.00 / 1.00 | 25 | wind, no "offshore" hint |
| Offshore Wind | wind | 1.00 / 1.00 | 27 | hint contains "offshore" |
| Run-of-River Hydro | water | 0.90 / 0.85 | 60 | hint contains "run" or "ror" |
| Reservoir Hydro | water | 0.90 / 0.85 | 60 | other hydro |
| Pumped-Storage Hydro | water | 0.80 / 0.75 | 60 | hint contains "pump" |
| Geothermal | geothermal | 0.90 / 0.85 | 30 | always for geothermal |
| CCGT | naturalgas | 0.58 / 0.45 | 30 | hint "ccgt"/"combined", or no hint and capacity ≥ 100 MW or unknown |
| OCGT | naturalgas | 0.40 / 0.28 | 25 | hint "ocgt"/"open"/"peak", or no hint and capacity < 100 MW |
| Coal Steam Turbine | coal | 0.40 / 0.30 | 40 | always for coal |
| Fuel-Oil Steam Turbine | fuel_oil | 0.35 / 0.28 | 35 | fuel oil, or diesel with a "steam"/"boiler" hint |
| Diesel Engine | diesel | 0.42 / 0.30 | 25 | other diesel |
| Nuclear | nuclear | 0.33 / 0.33 | 60 | always for nuclear |
| Biomass Steam Turbine | biomass | 0.30 / 0.20 | 25 | always for biomass |
| Biogas Engine | biogas | 0.40 / 0.30 | 20 | always for biogas |
| Waste-to-Energy | waste | 0.25 / 0.18 | 25 | always for waste |
| Other Generator | other | 0.35 / 0.25 | 25 | anything else |

If a fuel or technology with a matching key or name already exists in the system, it is reused instead of duplicated.


### Deduplication Algorithm

When several sources report the same asset, `deduplicate_features` keeps one record. OSM is treated as authoritative.

**Generators:**

1. **Plant/unit reconciliation (OSM).** OSM often maps a station both as a `power=plant` polygon and as the `power=generator` units inside it. Units within 1 km of a plant polygon are matched to it. If the polygon carries a capacity, it is kept and its units are dropped; a polygon without capacity is dropped in favour of its units. Within any source, a record whose capacity matches the sum of its co-located neighbours (±15%) is dropped as an aggregate.
2. **OSM-authoritative gap fill.** Every OSM generator is kept. A GEM or WRI generator is kept only if no OSM generator of the same fuel with a capacity of at least 2 MW lies within 2 km. OSM markers without a real capacity therefore do not hide a GEM/WRI entry with a known capacity.
3. **Plausibility caps.** A capacity above the largest plausible single plant for its fuel is capped: hydro 27 GW, nuclear 8.3 GW, gas and oil 8.2 GW, coal 7.7 GW, solar 4.8 GW, wind 3.6 GW, biomass/biogas/waste 3.1 GW, geothermal 0.5 GW (unknown fuels 27 GW).

The GEM fetcher returns units with status "operating" and units with a blank status; units with any other status (construction, mothballed, retired, ...) are skipped and counted in the progress message.

**Other point features:** batteries are merged by proximity (1 km) and capacity (±20%); substations by proximity (1 km); transformers by proximity (0.5 km). The higher-priority source (OSM > GEM > WRI > GridFinder) is kept and its empty fields are filled from the duplicate.

**Lines:** OSM lines are always kept. A GridFinder line whose endpoints both match an OSM line's endpoints (within 1 km, either direction) is merged into the OSM line (its attributes fill empty OSM fields); other GridFinder lines are kept.


### Line Capacity Estimation

A line without a capacity tag gets the thermal rating of the standard overhead line type with the nearest nominal voltage:

\[
P = \sqrt{3}\, V\, I_{nom} \times 0.9 \times n_{circuits}
\]

| Nominal voltage (kV) | \(I_{nom}\) per circuit (kA) | Rating per circuit at nominal voltage (MW) |
|---------------------|------------------------------|-------------------------------------------|
| 33 | 0.45 | 23 |
| 66 | 0.65 | 67 |
| 110 | 1.00 | 171 |
| 132 | 1.05 | 216 |
| 220 | 2.00 | 686 |
| 300 | 2.80 | 1,309 |
| 380 | 3.60 | 2,133 |
| 500 | 4.30 | 3,352 |
| 750 | 5.80 | 6,781 |

\(V\) is the line's own voltage and 0.9 is a thermal margin (not an N-1 derate). The same line type provides the per-km impedance, so rating and impedance stay consistent. The number of circuits comes from the OSM `circuits` tag, or else from `cables` (3 cables per AC circuit); default 1. A tagged capacity is multiplied by the number of circuits. The builder writes the whole line: its rating is that of all circuits together and its R and X those of one circuit divided by the number of circuits (B multiplied by it), which is how the model reads a line's values. Per-unit values are on the system base `ac_power_flow.base_mva` (\(Z_{base} = V^2 / S_{base}\)), for the lines it builds, those whose voltage it infers and those it adds when reconnecting islands. A transformer it creates has a typical impedance on its own rating, 10 % (6 % below 10 MVA), referred to the same system base: \(z_{own} \times S_{base} / S_{rated}\), at least 0.005 p.u. Earlier versions used 100 MVA whatever `base_mva` was, and gave units under 100 MVA a flat 0.10 p.u. (0.06 p.u. below 10 MVA) whatever their rating, and units of 100 MVA or more at most 0.14 p.u.

Parameter inference later raises a line or transformer rating that is below 1.25 times the worst-case flow across it, capped by the system peak demand (×1.25) and by a surge-impedance-loading ceiling per voltage class (500 kV: 2,400 MW; 345 kV: 1,200; 220 kV: 600; 110 kV: 250; 66 kV: 120; 33 kV: 60; 10 kV: 20; below: 10), with a 5 MW floor. It never lowers a rating. A line whose rating it raises from \(S_{old}\) to \(S_{new}\) counts as \(k = S_{new}/S_{old}\) times its circuits of the same type in parallel, as the simplification's equivalent lines do: its R and X are divided by \(k\) and its B multiplied by \(k\), so the impedance matches the larger rating. A line without a reactance first gets the one the builder derives from its length and voltage; one whose length or voltage is unknown keeps its values and is named in the result notes. `num_circuits` is unchanged (it is descriptive: the line's values already hold every circuit). Earlier versions raised the rating and kept the impedance of the smaller line, so the raised line drew too little of the flow in the DC power flow. A transformer whose rating is raised gets its impedance re-derived from the new rating.


---


## Mathematical Formulations


### GM-1: Haversine Distance

The great-circle distance between two points on the Earth's surface, used for proximity calculations throughout the wizard (snapping, deduplication, station clustering):

\[
d = 2R \arctan2\!\left(\sqrt{a},\; \sqrt{1-a}\right) \tag{GM-1}
\]

where

\[
a = \sin^2\!\left(\frac{\Delta\varphi}{2}\right) + \cos\varphi_1 \cos\varphi_2 \sin^2\!\left(\frac{\Delta\lambda}{2}\right)
\]

| Symbol | Description |
|--------|-------------|
| \(R\) | Earth's mean radius (6,371 km) |
| \(\varphi_1, \varphi_2\) | Latitudes of the two points (radians) |
| \(\lambda_1, \lambda_2\) | Longitudes of the two points (radians) |
| \(\Delta\varphi\) | \(\varphi_2 - \varphi_1\) |
| \(\Delta\lambda\) | \(\lambda_2 - \lambda_1\) |

The Haversine formula assumes a spherical Earth. For the distance scales in this wizard (typically 0.05 -- 100 km), the error relative to an ellipsoidal calculation is negligible (< 0.3%).


### GM-2: K-Means Clustering Objective

Node placement minimizes the within-cluster sum of squared distances:

\[
J = \sum_{c=1}^{k} \sum_{\mathbf{x}_i \in C_c} \|\mathbf{x}_i - \boldsymbol{\mu}_c\|^2 \tag{GM-2}
\]

| Symbol | Description |
|--------|-------------|
| \(k\) | Number of clusters (nodes to create) |
| \(C_c\) | Set of points assigned to cluster \(c\) |
| \(\mathbf{x}_i\) | Position of infrastructure feature \(i\) (in projected km) |
| \(\boldsymbol{\mu}_c\) | Centroid of cluster \(c\) |

The algorithm uses k-means++ initialization (10 restarts, random state 42) for the Infrastructure Density and Demand Proxy criteria. The Regional Balance criterion uses a regular grid as the initial centroid seed instead.


### GM-3: Elbow Method (Knee Detection)

The number of clusters \(k^*\) is chosen at the "knee" of the inertia curve, the point of diminishing returns where adding clusters yields little improvement:

\[
k^* = \arg\max_{k \in [k_{\min},\, k_{\max}]} \; d_\perp(k) \tag{GM-3}
\]

where \(d_\perp(k)\) is the perpendicular distance from the point \((k,\, J(k))\) to the line connecting \((k_{\min},\, J(k_{\min}))\) and \((k_{\max},\, J(k_{\max}))\):

\[
d_\perp(k) = \frac{|\hat{x}_k \cdot \Delta \hat{y} - \hat{y}_k \cdot \Delta \hat{x}|}{\sqrt{\Delta\hat{x}^2 + \Delta\hat{y}^2}}
\]

Both axes are normalized to \([0, 1]\) before computing distances so that the number of clusters and the inertia value contribute equally:

\[
\hat{x}_k = \frac{k - k_{\min}}{k_{\max} - k_{\min}}, \qquad \hat{y}_k = \frac{J(k) - J(k_{\max})}{J(k_{\min}) - J(k_{\max})}
\]

This avoids the low-\(k\) bias of the silhouette score and needs no extra parameter.


### GM-4: Bounding-Box Area

The area shown in the domain status line is an estimate from the domain's bounding box, not the polygon area:

\[
A \approx (\Delta\varphi \times 111.32) \times (\Delta\lambda \times 111.32 \times \cos\varphi_{\text{mid}}) \tag{GM-4}
\]

| Symbol | Description |
|--------|-------------|
| \(\Delta\varphi, \Delta\lambda\) | Latitude and longitude span of the bounding box (degrees) |
| \(\varphi_{\text{mid}}\) | Mid-latitude of the bounding box |

For irregular polygons the value overstates the enclosed area. It is used for display only.


### GM-5: UTM Approximation for Metric Distances

All clustering computations project geographic coordinates to approximate Cartesian kilometers before running K-means, so that distances in both axes are comparable:

\[
x_{\text{km}} = \varphi \times 111.32, \qquad y_{\text{km}} = \lambda \times 111.32 \times \cos\left(\frac{\pi}{180}\bar{\varphi}\right) \tag{GM-5}
\]

| Symbol | Description |
|--------|-------------|
| \(\varphi\) | Latitude (decimal degrees) |
| \(\lambda\) | Longitude (decimal degrees) |
| \(\bar{\varphi}\) | Mean latitude of all points in the dataset |

This is equivalent to a simplified UTM projection centered at the mean latitude. For study areas spanning less than 5 degrees of latitude (roughly 550 km), the approximation error is below 0.5%.

After clustering, centroids are back-projected to geographic coordinates by dividing by the same scale factors.


---


## Scripting

The grid mapping modules can be used without the GUI for batch processing:

```python
from esfex.visualization.workflows.grid_mapping_fetchers import (
    GEMGridFetcher,
    deduplicate_features,
    filter_features_by_polygon,
)
from esfex.visualization.workflows.grid_mapping_geofabrik import make_osm_fetcher
from esfex.visualization.workflows.grid_mapping_clustering import (
    _determine_optimal_k,
    _extract_infrastructure_points,
    _project_to_km,
    _run_kmeans,
)

# Define study area (south, west, north, east)
bounds = (21.4, -83.0, 23.3, -79.5)  # Cuba

# Polygon vertices [(lat, lng), ...]
polygon = [
    (21.5, -83.0), (23.2, -83.0),
    (23.3, -79.5), (21.4, -79.5),
]

# Fetch features (outside Qt, call _fetch() directly).
# make_osm_fetcher picks Geofabrik or Overpass like the GUI does.
osm = make_osm_fetcher(
    bounds, min_voltage_kv=66, min_capacity_mw=20.0,
    element_types=None, polygon=polygon,
)
features = osm._fetch()
features += GEMGridFetcher(bounds, min_capacity_mw=20.0)._fetch()

# Clip and deduplicate
features = filter_features_by_polygon(features, polygon)
features = deduplicate_features(features)

for f in features:
    print(f"{f.feature_type:15s} {f.name:40s} "
          f"{f.capacity_mw:8.1f} MW  {f.fuel}")

# Determine the node count from infrastructure positions
pts = _extract_infrastructure_points(features)
pts_km = _project_to_km(pts)
k = _determine_optimal_k(pts_km, min_k=2, max_k=20)
print(f"Optimal k = {k}")

# Run K-means
labels, centroids = _run_kmeans(pts_km, k)
```

The fetcher classes inherit from `QThread` and emit Qt signals for GUI progress. When used in scripts without a Qt event loop, call the `_fetch()` method directly instead of `start()`.

To build the network, create a `GuiModel`, load a blank `GuiSystemState` into it, add the nodes and call `build_grid_from_features()`:

```python
import math

import numpy as np

from esfex.visualization.data.gui_model import GuiModel, GuiSystemState
from esfex.visualization.workflows.grid_mapping_builder import (
    build_grid_from_features,
)

model = GuiModel()
model.load_state(GuiSystemState(name="scripted_grid"))

# Add nodes from the clustering result (back-project km to lat/lng)
cos_lat = math.cos(math.radians(float(np.mean(pts[:, 0]))))
for i, (cy, cx) in enumerate(centroids):
    idx = model.add_node(f"Node {i}")
    model.update_node(idx, centroid_lat=cy / 111.32,
                      centroid_lng=cx / (111.32 * cos_lat))

result = build_grid_from_features(
    model=model,
    features=features,
    bus_strategy="per_voltage",
    station_radius_km=1.0,
    min_capacity_mw=20.0,
    min_voltage_kv=66.0,
)
print(result.summary())
```

`build_grid_from_features` runs the build phases listed in [Build pipeline](#build-pipeline); the GUI worker additionally runs parameter inference, simplification and the isolated-component cleanup (`infer_electrical_params` in `grid_mapping_inference`, `apply_simplification_level` and `drop_isolated_components` in `esfex.visualization.data.validation`).

It also accepts `reconnect_max_km` (default 0). A positive value enables an opt-in bounded reconnection: isolated components within that distance of the main network are tapped onto nearby lines or linked to the nearest same-voltage bus, and remaining islands that have only generation or only demand are absorbed into the connected grid. The GUI always passes 0, and the builder modules also contain an `iterative_auto_connect` and a `fuel_auto_route` function (in `grid_mapping_steps`) that the GUI no longer calls. The bus-to-bus bridge lines `iterative_auto_connect` adds for components without equipment run at the island bus's voltage and carry the distance between their end buses as `length_km`, the R, X and B of the standard line type of their voltage on the system base, and the frequency of the main-network bus they connect to. When the nearest main-network bus is at another voltage level (more than 10 % apart), the line ends at a new bus of the island's voltage beside that bus, joined to it by a transformer; earlier versions ran a single line at the higher of the two voltages straight between the two levels.
