# Changelog

All notable changes to **ESFEX** are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Per-release notes are also published on the
[GitHub Releases page](https://github.com/Net-Zero-Horizon/ESFEX/releases).

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

First PyPI release of **ESFEX — Energy System FlEXibility**.

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
