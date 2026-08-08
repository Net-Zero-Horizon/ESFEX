# Changelog

All notable changes to **ESFEX** are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Per-release notes are also published on the
[GitHub Releases page](https://github.com/Net-Zero-Horizon/ESFEX/releases).

## [0.2.6] — 2026-08-08

### Added

- **`esfex add-solver` / `remove-solver` / `list-solvers`** — install optional,
  commercial solvers (Gurobi, CPLEX, CBC, SCIP, Xpress) on demand into a
  dedicated Julia environment that is stacked on the load path, so it survives
  ESFEX upgrades and always targets the Julia the app actually uses. The install
  verifies the solver loads, so a licence/build problem is reported up front
  rather than mid-solve.

### Fixed

- **Solver availability** is now read from ESFEX's Julia environment (the shipped
  Project.toml plus the optional-solvers env), not from unrelated Python
  companion packages — the GUI no longer offers a solver the backend cannot
  load. A project that names a solver unavailable on this machine is coerced to a
  usable one on load instead of failing mid-solve with an empty result.
- **Method-specific solver options** (e.g. HiGHS `pdlp_scaling`) are no longer
  forwarded while the LP method is on *choose*; forwarding them aborted the whole
  model build on some solver builds.
- **Runner** now fails loudly when no operational window solves, instead of
  logging `completed: objective=$0` and exporting an empty result.
- **Grid Builder — transmission-only model.** The minimum-voltage floor is now
  enforced on the *built* network: the sub-floor distribution layer that leaked
  in (unknown-voltage features assigned a low voltage during the build) is
  collapsed, relocating its generation to the nearest transmission bus. Auto-
  transformers between voltage levels are sized by level (275→600, 220→400 MVA…)
  instead of a flat 100 MVA. Together these remove phantom transmission
  congestion — measured load shedding on a Hokkaido UC run fell from ~37 % to
  ~5 %.
- **Grid Builder — step 3 data fetching.** World Bank macro indicators get a
  30 s budget with retries (was a single 15 s attempt), and Google Open Buildings
  (unavailable over source.coop's HTTPS glob) transparently falls back to
  Overture instead of failing the demand step.
- **Results charts** group generators by fuel when a system has no
  `TechnologyConfig` catalog (Grid Builder imports), instead of collapsing the
  whole fleet into a single *Other* bucket.
- **Map Results** system selector shows only the current run's systems; previous
  runs in the results directory (often other regions) no longer leak in.

### Changed

- **OSM fetching is faster.** Tiles are fetched concurrently across the Overpass
  mirrors, dense tiles subdivide on the first server-side timeout, and the query
  requests only what the builder consumes — `out center` (centroid) for
  point-like elements and `out geom` (full trace) for lines, dropping the blanket
  recursion that pulled every substation polygon vertex.

## [0.2.5] — 2026-08-06

### Added

- **Grid Builder — bounded reconnection & island cleanup** — a *Reconnect
  isolated ≤ N km* control connects each substation within N km of the backbone
  (tapping a nearby same-voltage line, or linking to the nearest same-voltage
  bus with a real-length line), then absorbs any component that remains
  dysfunctional — generation XOR demand — into the connected grid PyPSA-style:
  its generation/demand is moved to the nearest bus and the dead topology
  removed, while self-sufficient islands (generation **and** demand) are kept.
  Default 8 km; set to 0 to keep the network exactly as mapped.
- **Isolated load-island detection** — validation now reports a component that
  carries demand but has no generation and no path to any generator (unservable
  load the solver can only shed); this previously passed clean.

### Changed

- **Grid Builder data sources** — OSM, WRI, GEM and GridFinder are laid out in a
  single row and all enabled by default; combining them yields the most
  complete network.
- **Minimum generator capacity floor scales with the minimum voltage** — the
  wizard default tracks the voltage floor (~0.3 MW/kV; e.g. 110 kV → 35 MW) so
  distribution-scale plants don't clutter a transmission model as stranded
  islands. Edit by hand to pin a value; set to 0 to include every generator.
- Narrower element-tree and properties panels; compact *New System* dialog.

### Fixed

- **OSM fetch no longer silently returns partial data** — the Overpass fetch
  tiles the region at 1° (was 5°, which blew past the server timeout on dense
  regions and returned a fraction as a clean success), subdivides a tile that
  still times out down to 0.25°, re-queries a zero-element response once to rule
  out a dispatcher load-shed empty, retries transiently-failed tiles once, and
  warns loudly when coverage is incomplete.
- **Project save/load is faithful** — three data-loss bugs fixed: the
  generator→bus assignment (previously re-guessed by nearest coordinate and
  scrambled on every export/import), AC/DC & frequency converters collapsing to
  a self-loop, and a valid demand distribution being drained out of
  disconnected micro-grids. A Grid Builder network now round-trips with
  identical connectivity, stable across repeated export/import.
- **Generation deduplication** — OSM is authoritative and never merges its own
  units, so real multi-unit plants survive; a GEM/WRI plant is kept only where
  OSM is silent; a bare OSM location marker (no capacity tag) no longer drops
  the capacitated GEM entry; an OSM `power=plant` polygon is reconciled against
  its unit nodes (no double count; the polygon output excludes under-
  construction / decommissioning units); and only operating GEM plants are kept.
- **Transmission line ratings** — line circuit count is derived correctly from
  the OSM `cables` tag (3 cables = 1 three-phase circuit, not 3) and preserved
  as the maximum across merged segments, so a double-circuit backbone keeps its
  real thermal capacity.
- **Tooltips wrap to a logical width** instead of a single screen-wide line —
  implemented without a global event filter (which segfaulted alongside the
  embedded QtWebEngine map).
- Project export/import runs off the GUI thread; negative fuel `price_base` is
  accepted (waste-to-energy tipping fees); the QtWebEngine raster-tile memory
  budget is raised so large maps stop dropping tiles.

## [0.2.4] — 2026-08-03

### Added

- **Deterministic scheduled interruptions (maintenance / forced outages)** — a
  new interruptions calendar lets you take any electrical or primary-energy
  element out of service — fully or as a derate — over explicit time windows,
  and the optimizer respects them. Supported for generators, batteries, fuel
  sources, transmission lines, AC/DC and frequency converters, and transformers.
  Windows are edited in a D3 Gantt-timeline dialog (reachable from the toolbar
  and from **right-click → Schedule interruption** on an element in the tree),
  persist with the project, and compile into per-element, per-timestep capacity
  masks for the model — with no change to the operational formulation itself.

### Changed

- **Grid Builder — Bus Distribution unified** — the two-button flow (separate
  *Fetch demand* and *Distribute*) is replaced by a single method dropdown
  (spatial demand + building-footprint proxies) and one **Distribute demand**
  button; footprint options appear only for footprint methods. The step-3 demand
  and distribution labels were also tidied.
- **Validation dialog** — the *Auto-fix errors* button was removed (the
  underlying validation library function is unchanged).
- **System attributes** — the system name field now sits directly after its
  *Name:* label instead of being right-justified.
- **Interruptions calendar** follows the Studio's visual theme.

### Fixed

- **Large config and project save/load froze — or crashed — the GUI** — parsing,
  serialising and `.esfexp` bundling ran on the GUI thread, so a large
  multi-system file painted the progress dialog only partway and could crash Qt.
  Config load/save and project export/import now run on background workers while
  the window and its progress dialog stay responsive.
- **Grid Builder: demand distribution broke when CMIP6 was rate-limited** — a
  429 during the forecast fell back to a climate year that could carry NaN,
  which propagated into per-cell demand and then failed the whole bus
  distribution. Non-finite temperature and cell demand are now scrubbed, so
  demand and distribution compute from the fallback climate instead of breaking.
- **Grid Builder: CMIP6 climate fetch now retries** transient 429 / 5xx /
  network errors with exponential backoff instead of losing a node's future
  climate on the first throttle.
- **Grid Builder: availability profiles came back all-zeros** when the weather
  API rate-limited the concurrent requests; concurrency is capped and all-zero
  responses now trigger a retry.
- **Grid Builder: building footprints never downloaded** during bus
  distribution (Microsoft quadkey, Web-Mercator and GeoJSON parsing bugs).
- **Grid Builder: demand distribution reported "0 multi-bus nodes"** on all-HV
  grids where no bus was pre-tagged as load; it now falls back to all busbars.

## [0.2.3] — 2026-07-22

### Added

- **View a node's centroid on the map** — nodes are abstract and carry no
  permanent map marker (one would overlap the electrical and primary-energy
  elements), so a transient cue was added: the map flies to the centroid and
  drops a pulsing marker that removes itself after a few seconds. Reachable from
  a new **View** button next to the node form's centroid button (the former
  *Pick on map* is shortened to **Place**) and from **right-click → View on map**
  on a node in the element tree.

### Fixed

- **Generator type not preserved in non-English UIs** — the resource-type combo
  (Renewable / Non-renewable) showed translated labels while the model stores
  English literals, so editing generators in a non-English UI made every
  generator take the last-shown type and saved a localized string that broke the
  schema. The combo now keeps the English literal internally; legacy localized
  values in already-saved projects are normalized on load.
- **OTEC cycle diagrams under NumPy 2.x** — fluid saturation/entropy/enthalpy
  properties can come back as single-element arrays on some NumPy/CoolProp
  combinations, and `float()` on such an array is a hard error under NumPy 2.x,
  crashing the cycle-state and T-s / P-h diagram computations. These values are
  now coerced version-robustly.

### Changed

- **Grid Builder UI is now translated** — its labels, group boxes, checkboxes,
  radio buttons, buttons and tooltips were hard-coded English and never followed
  the selected language. All user-facing strings now use the translation system
  (en/es/fr/pt/ja), matching the Solar/Wind/Demand wizards.
- **New ESFEX logo** across the wordmark, window icon and documentation, with
  the splash logo no longer clipped and a smaller About-dialog logo.
- **Python support capped to `>=3.10,<3.14`** so installs on a too-new Python
  (which has no prebuilt wheels for the scientific stack and fails to build from
  source) get a clear resolver error instead of an obscure compiler failure;
  Python 3.13 is now an official supported/classified version.

## [0.2.2] — 2026-07-06

### Fixed

- **Studio usable on small laptop screens** — the properties panel no longer
  takes ~half the width and can only be hidden: its minimum width is now
  screen-scaled and the main splitter defaults to an 18/58/24 split, so the
  panel starts compact and can be dragged narrow. The default window size is
  clamped to the available screen. Fonts also fit: the base font now shrinks on
  small screens (it never did before), the ~46 hard-coded widget `font-size`
  stylesheets scale with it, and a "QFont: Point size <= 0" warning is guarded.

### Removed

- **Bundled Windows `.exe` installer** and its release build workflow. It
  masked rather than fixed the real Windows launch failure, which comes from
  installing Python via the **Microsoft Store** (a sandboxed, redirected
  filesystem that breaks native PySide6/Qt DLL loading). The README, the
  installation docs and the Studio's DLL-load error message now document the
  cause and fix (install Python from python.org or conda). The console-less
  `esfex-studio` launcher is unaffected.

## [0.2.1] — 2026-06-19

### Fixed

- **Windows: Studio launch crash from a Qt DLL conflict** — on systems with
  another Qt on PATH (a conda `qt-main`/`pyqt`/`qt6`, common in base Anaconda),
  `esfex studio` failed with `ImportError: DLL load failed while importing
  QtWidgets: the specified procedure could not be found`. PySide6's own bundled
  Qt is now put first on the DLL search path (and the conda dirs are skipped
  entirely when PySide6 is self-contained), so a foreign Qt can no longer shadow
  it. The error message for this case now explains the real cause (Qt conflict /
  wrong environment) instead of the misleading "reinstall PySide6".

## [0.2.0] — 2026-06-15

### Added

- **User-defined optimization constraints** — add custom linear constraints to
  the operational *and* investment models, either declaratively in the config
  (`custom_constraints`) or via plugin Julia overlays, editable from a GUI dialog.
- **French and Portuguese GUI translations** — `fr` and `pt` join English,
  Spanish and Japanese, in exact key parity (placeholders and Qt mnemonics
  preserved); the Preferences language list discovers them automatically.
- **GPU-accelerated demand inference** — XGBoost demand/density prediction runs
  on a CUDA GPU when one is available (auto-detected, large-batch only), with
  CPU fallback and an `ESFEX_XGB_DEVICE` override (~2.6× on realistic batches).

### Changed

- **Grid Builder is responsive on country-scale regions** — the "Building
  network" pipeline and the Step-1 fetch aggregation (polygon clip + dedup) now
  run on worker threads with live per-stage status and per-phase timings, so the
  Studio no longer freezes; the network build was also de-quadratized.
- **Availability profiles** — weather-based capacity factors are now the default
  for wind/solar. Queries are de-duplicated per ~11 km location and fetched
  concurrently with retry/backoff, so cost scales with distinct locations, not
  generator count (a full-Japan build dropped from >30 min to ~1 min). A failed
  weather fetch leaves the unit without a profile rather than fabricating a flat
  value; thermal/hydro keep synthetic profiles.
- Project **status promoted from alpha to beta**.

### Fixed

- Grid Builder simplification: O(n²) dead-end bus pruning made linear.
- "Create new system" dialog widened so its window title is no longer clipped.

## [0.1.13] — 2026-06-14

### Added

- **Imported GeoAssets as workflow domains** — every area workflow (Grid
  Builder, Solar PV, Wind, Rooftop, EV, Demand) can define its study area from
  an imported GeoAsset (Shapefile/GeoJSON/KML/GPKG), dissolved into one boundary,
  instead of only a hand-drawn region. Fetched features are clipped to the exact
  polygon (no bbox contamination); GeoAssets persist self-contained in the
  project YAML/`.esfexp`.
- **Standardized domain definition** — one shared two-column control (draw a
  polygon **or** apply a GeoAsset) across all workflows, with equal-sized
  selector boxes and mutual exclusivity (last action wins).
- **Portable `.esfexp` project bundles** — export/import a complete project
  (config + demand + availability profiles) as a single self-contained file,
  with a progress dialog for load/save/export.

### Changed

- **Consolidated workflow wizards** — Solar PV, Wind, Rooftop and EV collapse
  from 8–9 single-column steps to 4 content-aware steps: related light panels
  sit side by side, wider panels (tables, charts) take full-width rows, and each
  step scrolls vertically only when needed so nothing is squashed or overflows.
- Toolbar: visible **Layer** / **Base Map** captions above their selectors, font
  scaling with the rest of the bar, and +20% headroom on the icon-scaling cap.

### Fixed

- **Wind workflow restored** — reconciled the GUI wind config with the current
  `windrex` API (fat GUI `WindConfig`; the analyzer adapter now builds the slim
  `windrex.WindConfig` it needs), and fixed turbine selection
  (`specific_power`). The Wind assessment runs end-to-end again.
- Grid Builder: `NameError` when applying a GeoAsset as the domain.

## [0.1.7] — 2026-06-10

### Added

- **Faithful OSM import as the only build mode** — the Grid Builder now always
  reconstructs the network from the source topology (substations, lines,
  transformers). The "skip incomplete" / "faithful import" toggles, the
  GridFinder source, and the dead snapping/interconnection parameters are gone;
  Step 2 is simpler and the build no longer emits spurious "isolated
  generation / no demand" warnings.
- **Spatially-explicit demand forecasting** — node demand comes from a trained
  hourly XGBoost density model evaluated per 0.25° grid cell: each cell's
  demand density (driven by SSP population and GDP rasters plus CMIP6
  multi-year climate) is multiplied by the cell area and summed per node, then
  anchored to a national total. This replaces the previous "national total ÷
  node count" proxy and produces realistic spatial and inter-annual variation.
- **Capacitated-transport distribution of demand to buses** — within a node,
  demand is split among load buses by solving a capacitated transportation
  problem (cells → buses, capacity = transformer MVA with a voltage-scaled
  fallback) instead of a Voronoi/nearest assignment. A bus serves a
  distribution territory bounded by its capacity, and demand spills to the next
  substation once the nearest one saturates.

### Changed

- **Grid Builder Step 2 layout** tidied (Target System / Node Placement), and
  the demand step exposes an SSP-scenario selector in place of a fixed GDP
  growth rate.

### Tested

- **End-to-end solvability** of the faithfully-built network (build → solve →
  validate), plus unit coverage for the per-cell density inference and the
  capacitated demand→bus allocation.

## [0.1.6] — 2026-06-08

### Performance

- **"Building network" no longer hangs on country-scale regions** — four
  independent O(n²) hot paths in the Grid Builder build pipeline are now
  linear: bus snapping (over-wide candidate window), disconnected-component
  bridging and equipment chaining (linear nearest-bus scans → projected
  KD-tree), line removal (per-fix list rebuild → batched), and per-edge bridge
  detection in electrical-parameter inference (a BFS per edge → a single
  iterative Tarjan pass). A ~25k-feature import (e.g. Japan) that previously
  hung for 20+ minutes now completes in seconds.

### Added

- **Demand visualizer** — a reusable Plotly demand chart (Grid Builder and node
  panel) with a date x-axis that auto-scales on zoom, a red mean line, and a
  deep "Demand statistics" panel.
- **Complete, functional built networks** — generators are assigned a fuel and a
  technology from a powerplantmatching-style taxonomy (CCGT/OCGT, steam and
  combustion engines, run-of-river/reservoir/pumped hydro, PV, on/offshore
  wind), lines get capacities and impedances from a standard line-type catalog
  (PyPSA-style r/x/c per km with an N-1 derate), and nodes are filled with
  default operating reserves and transmission losses. No more orphan generators
  without a fuel or technology.
- **Per-phase build timing** — the Grid Builder result panel now reports a
  "Timing" breakdown (seconds per build phase).

### Changed

- **Fuel Entry Point and Fuel Source unified** into a single "Fuel Source"
  concept across the model and the Studio GUI.

### Fixed

- **OSM fetch timeout on large regions** — the Grid Builder tiles large Overpass
  queries (e.g. Japan) into sub-requests instead of failing on a single
  monolithic query, and normalizes wrapped longitudes so the WRI/GEM/GridFinder
  layers return data for areas crossing the ±180° meridian.
- **"Naming nodes" hang** — the node-naming step is time-boxed and the
  subsequent rendering no longer freezes the UI after large-region builds.
- **Map zoom-out and world wrapping** — the Grid Builder map is constrained to a
  single copy of the world: it no longer zooms out below 1× world size or pans
  onto wrapped copies of the globe (which produced out-of-range longitudes).
- **OTEC cycle diagrams across NumPy/SciPy versions** — thermodynamic state
  values are coerced to plain floats so the T-s / P-h loop arrays stay
  homogeneous (no ragged-array errors) and `mass_flow` is always a float,
  regardless of the installed NumPy/CoolProp build.
- **"Lines toward a centroid" after a rebuild** — the node-assignment spatial
  index cached on the centroid *count*, so a rebuild with re-clustered
  centroids of the same count reused a stale tree and collapsed the network
  toward the wrong centroids. It is now keyed on centroid content with a
  projected metric and exact-haversine refinement.

## [0.1.5] — 2026-06-06

### Fixed

- **Grid Builder demand forecast not persisted (#7)** — applying the step-3
  demand forecast only stored per-node summary stats and never wrote the hourly
  series to disk nor recorded a CSV path, so the saved config carried empty
  `demand_paths` and the runner had no per-node demand. The forecast is now
  written to per-node CSV files (under a `demand/` folder next to the project)
  and wired into each node, so `demand_paths` is emitted and the runner finds
  the files.

## [0.1.4] — 2026-06-06

### Added

- **Reservoir hydropower modelling overhaul (#4)** — hydroelectric generators
  are now dispatched against an explicit water-energy budget in both the
  operational dispatch and the capacity-expansion master, instead of being
  treated as firm capacity. Five behaviours, each independently optional and
  fully wired into the Studio GUI (en/es/ja):
  - **Energy budget** — water balance (inflow, turbining, pumping, spillage,
    evaporation) modelled in the master, correcting hydro previously
    over-credited as firm MW.
  - **Minimum environmental flow** — a mandatory ecological release floor.
  - **Seasonal storage** — reservoir level chained chronologically across
    representative periods (TSAM inter-period linking), so water banked in a wet
    season is available in a later dry one.
  - **Hydraulic cascade** — an upstream reservoir's release feeds a downstream
    reservoir, with an optional travel delay.
  - **Head dependence** — a depleted reservoir delivers less peak power, via a
    linear (LP-friendly) level-dependent power limit.

### Fixed

- **Deleting a system left orphan inter-system links (#5)** — links whose
  endpoints referenced a deleted system survived in the project and were
  silently dropped by the runner. Deletion now removes every link touching the
  system.
- **Grid Builder country detection (#6)** — step 3 reverse-geocoded the
  bounding-box centroid via Nominatim, folding territories into their sovereign
  state (Puerto Rico → United States), finding only one country per region
  (Haiti was missed), and returning localized names. Detection is now offline
  and territory-aware: grid nodes are tested against bundled country polygons,
  surfacing every country the region intersects with correct ISO3 codes.

### Documentation

- New reservoir-hydropower formulation page documenting the water balance and
  all five behaviours as LP constraints; reservoir config/GUI fields and the
  constraint catalogue updated.
- README links the companion repositories and uses Harvey-ball icons in the
  feature comparison table.

## [0.1.3] — 2026-06-05

### Fixed

- **Grid Builder demand-forecast crash (#3)** — the forecast step's worker
  threads (country detection, World Bank / ERA5 fetch, ML forecast) updated Qt
  widgets directly, violating Qt's main-thread-only GUI rule and segfaulting
  (`Cannot create children for a parent that is in a different thread`). The
  heavy work still runs off the GUI thread, but every widget update is now
  marshalled to the main thread via a queued signal.

## [0.1.2] — 2026-06-05

### Added

- **Benders decomposition** as an optional master-problem solver
  (`master_problem.solver_method: monolithic | benders`): an investment-only
  master with `θ[y]` recourse variables plus per-representative-day dispatch
  subproblems and optimality cuts — beneficial for very large problems.
  Configurable (`benders_max_iterations`, `benders_tolerance`,
  `benders_lol_penalty_cap`) and selectable from the Studio. Monolithic remains
  the default.
- **OpenSSF Best Practices** badge.

### Fixed

- **Grid Builder bus-distribution step no longer freezes the UI** on
  whole-country footprint sets: classification and nearest-bus assignment run in
  a background thread, with a vectorised classifier, a single centroid pass, and
  `np.bincount` accumulation.

## [0.1.1] — 2026-06-04

### Fixed

- **Grid Builder demand forecast crash** — the per-node forecast read
  `latitude`/`longitude` on grid nodes, but `GuiNode` exposes its position as
  `centroid_lat`/`centroid_lng`.
- **Fuel-entry-point duplication crash** — duplicating a fuel entry point used
  `coordinate.latitude`/`.longitude`, but `GeoPoint` uses `lat`/`lng`.
- **GeoJSON fuel-entry import** — fuel entry points were built with invalid
  `max_import_rate`/`import_cost` keyword arguments; import parameters now pass
  through the `fuel_params` mapping.
- **GeoJSON node import** — nodes are created from `Point` features and snapped
  to the nearest existing node by great-circle (haversine) distance, instead of
  always attaching to the first node.

### Changed

- Native Julia test suite expanded from smoke tests to full unit and
  end-to-end model-solve coverage, reported to Codecov under a `julia` flag.

## [0.1.0] — 2026-06-02

First PyPI release of **ESFEX — Energy System Flexibility**.

Hybrid Python/Julia framework for power-system capacity expansion and
operational dispatch under high renewable penetration: two-stage decomposition,
DC/AC optimal power flow, N-1 security, frequency stability, battery storage,
sector coupling (electrolyzer, primary energy, EV/V2G, rooftop solar),
MGA/SPORES, stochastic programming, Sobol sensitivity, and a GIS-based Studio.
Includes the unit-commitment load-shed fix, the test/coverage expansion, and
full packaging (CI, Apache-2.0, REUSE-compliant).

[0.1.6]: https://github.com/Net-Zero-Horizon/ESFEX/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/Net-Zero-Horizon/ESFEX/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/Net-Zero-Horizon/ESFEX/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/Net-Zero-Horizon/ESFEX/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/Net-Zero-Horizon/ESFEX/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/Net-Zero-Horizon/ESFEX/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Net-Zero-Horizon/ESFEX/releases/tag/v0.1.0
