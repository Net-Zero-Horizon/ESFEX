# Group Q — changelog text (round 5)

### Fixed

- **The map no longer throws away your edits after a renderer crash.** When
  the Studio's map page crashed (WebEngine render process), the reloaded map
  repopulated the whole project from the file loaded at startup, so every
  change made since loading was lost. The map is now redrawn from the
  project as it is in memory; the file is only read again when the crash
  happened before the project had been loaded at all.

- **Flat `dc_*` power-flow keys are read in any case.** The legacy system
  keys `dc_base_impedance`, `dc_reactance_per_km`, `dc_voltage_level_kv`,
  `dc_max_angle_diff_deg` and `dc_slack_bus` were read only as written in
  lower case or all upper case; a mixed spelling such as `DC_base_impedance`
  was dropped without a warning (the unknown-key check accepted it). The
  loader and the unknown-key check now share one table of the loader's
  key translations (`esfex.config.legacy_keys`), and penalties are read
  from every `PenaltiesConfig` field instead of a hand-kept list, so the
  two cannot drift apart again.

### Changed

- The unused Grid Builder step class `GridMappingConnectStep` (the old
  "Step 5: Simplify & Aggregate") and its translation keys were removed.
  It was not reachable from the Studio.

### Documentation

- `BatteryConfig` accepts both `availability_file` and `Availability`
  (the docs said only the alias), and generators accept both names too
  (the docs said `availability_file` was ignored for generators).
