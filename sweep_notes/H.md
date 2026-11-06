# Changelog text, group H (Studio GUI & Grid Builder)

### Added

- **Realism check in Validate.** The Validate dialog has a new **Realism**
  category that runs the plausibility and operability checks that until now
  were only reachable from Python: probable duplicate plants (same name, or
  same fuel within 15 km; info), installed capacity more than twice the peak
  demand (info), and a max-flow test of whether the line, transformer and
  converter ratings can deliver the peak demand from the generator buses
  (warning).
- **N-1 "Apply in" setting.** Global Settings > N-1 Security has checkboxes
  for the simulation modes that get the N-1 constraints (`apply_to_modes`).
  Studio projects used to fix this to unit commitment with no way to change
  it; new projects now default to both modes, like a YAML file.
- **Per-line reactance per km.** `transmission_lines_geo` entries accept
  `reactance_per_km` (Ω/km). When a line has no `reactance_pu`, its reactance
  is derived from its length with this value instead of
  `dc_power_flow.reactance_per_km`. The Studio line form's Reactance per km
  field, which was saved but dropped on load, now has this effect.

### Changed

- **Bus roles you set are kept.** Every save re-derived each bus's role from
  the topology, so a role picked in the bus form was overwritten. A role you
  pick is now marked as yours (saved in `_gui_layout.user_set_bus_roles`) and
  kept on save and reload; the demand-fraction repair never gives demand to a
  `connection` bus you set and counts a `mixed` bus you set as a demand bus.
  Buses whose role you have not touched are still repaired from the topology.
- **Generation N-1 "Percentage" reserve works.** With Reserve Type =
  Percentage, the generation N-1 constraint requires headroom of
  `generation_reserve_percentage` × the system demand of each step (no unit
  excluded). Before, the setting was ignored and the largest-unit rule was
  always used. The "Fixed MW" choice, which the schema rejects, is removed.
- **Grid Builder simplification levels 0–2.** Levels 2, 3 and 4 of the build
  step ran the same equivalent-network reduction under labels that described
  an older pruning scheme. The build step now offers 0 (cleanup), 1
  (aggregate equipment and parallel lines) and 2 (equivalent network, one
  bus per node); scripts passing 3 or 4 get level 2 as before.
- **Min. System Inertia field.** The system field labelled "Inertia Limit
  Threshold (frac)" is an absolute MW·s requirement; it is now labelled in
  MW·s and accepts values above 1.
- The per-line Base Impedance field is removed from the line and
  inter-system link forms (unused since Z_base is derived from the line
  voltage and `ac_power_flow.base_mva`).

### Fixed

- **Renaming a system.** The rename did not reach the inter-system links,
  Global Settings > Systems to Simulate or the saved `meta_network.systems`,
  so the renamed system dropped out of Validate and Run, its links pointed to
  a missing system and the saved file no longer loaded. Settings the Studio
  does not edit were also lost. All these references now follow the rename,
  the system keeps its position, and renaming onto an existing name shows a
  message instead of raising an error. Save drops names that are not
  systems of the project from `meta_network.systems`, and stochastic
  scenarios are written to the first simulated system, where they are read.
- **Inter-system links keep their impedance on reload.** Opening a project
  or importing systems restored links without `reactance_pu` /
  `resistance_pu` (and, on import, without the electrical metadata and
  style), so they fell back to the defaults. Imported links also get fresh
  ids instead of reusing ids already in the project.
- **SLD Run All N-1 and Run Screening.** Both buttons failed (a missing
  analyzer method, a wrong call signature, attribute access on dicts, and an
  undefined logger that turned every error into a crash). They now report
  the number of contingencies and violations (N-1), the stressed N-1-1 pairs,
  or the contingencies above the PI threshold with the worst one. The
  N-k labels were untranslated. The N-k options of Global Settings (depth,
  redistribution, PI threshold, transformer and battery contingencies,
  critical line threshold), which were saved but used nowhere, now preset the
  SLD Analysis panel and select which elements are tripped; the optimisation
  does not use them, and the form says so.
- **SLD time controls after Analysis.** Switching Analysis off left the
  year, hour and playback controls disabled; with results loaded they come
  back and the diagram shows the results again.
- **Geo imports in a projected CRS** (for example a UTM shapefile, or a
  GeoJSON with a projected `crs` member) are reprojected to WGS84 instead of
  being drawn in the wrong place.
- **Degradation labels.** The generator, battery, electrolyzer and AC/DC
  converter forms labelled degradation in %/yr while the value is a fraction
  per year; the labels now say frac/yr.
- **Reservoir section for new hydro units.** The generator form only showed
  the Reservoir section when a reservoir capacity was already set, so a hydro
  unit created in the Studio could never get one. It now shows for fuel
  Water too.
- **Fuel route waypoints.** Clicking empty map space while tracing a fuel
  route now adds a waypoint, as for transmission lines.
- **Script editor.** `Ctrl+Enter` runs the current script (the Run tooltip
  announced it); the auto-indent preference works; editor preferences apply
  from startup, not only after the Preferences dialog is closed.
- **Interruptions calendar dates.** Hours were turned into dates with the
  real calendar, so in a leap base year every date after 28 February was a
  day off. Dates now follow the model calendar (8,760 h per year, no
  29 February).
- **Building-footprint demand split (Grid Builder).** Each node's bus split
  counted every building in the domain, snapped to that node's own buses,
  which biased the split of multi-node domains. Each building now belongs to
  the node of its nearest bus, and only a node's own buildings set its split.
- **GEM plants with a blank status** are kept, as documented; an early
  status filter dropped them, and kept "construction" / "mothballed" rows only
  to skip them later.
- Grid Builder texts: the demand step header said "Step 6" (it is step 3);
  tooltips no longer mention an auto-connect step that no longer runs; the
  national-demand override tooltip says what 0 does for each forecast
  engine; the "Populate from Nodes" tooltip says what the button does.
