### Added

- **Unknown configuration keys are reported.** A misspelt or obsolete key
  (`min_up_time` for `min_up`) used to be dropped without a trace. It is
  still ignored, but `load_config` now logs a warning for each one with its
  full path (`systems.Cuba.generators.gas_1.dorop`) and keeps the list in
  `ESFEXConfig.unknown_keys`; `esfex validate` prints it, and the Studio's
  validation lists the keys under a new *Configuration* category. Keys the
  loader translates are not reported: upper-case aliases, flat `dc_*` /
  `DC_*` keys, penalty keys in any case, legacy `unit_*` / `bat_*` units,
  the Studio's `_gui_*` / `_technologies` / `_equipment_buses` metadata,
  `visual_scaling`, `_geo_assets`, `plugins` and `solver.options`.

### Fixed

- **Studio edits of system settings were lost on reload.** The Studio saved
  settings such as `target_re_penetration`, `min_annual_increment`,
  `max_annual_increment`, `force_replacement`, `inertia_limit_threshold`,
  `sim_rooftop` and `ev_initial_soc` twice: the value the file was opened
  with under the upper-case alias (`TARGET_RE_PENETRATION`) and the edited
  value under the field name. The alias won on the next load, so the edit
  was discarded. The Studio now writes each setting once, and the loader
  uses the field name when a file has both (with a warning if they differ),
  which recovers edits in files saved earlier.
- **Risk settings reset on the second save.** The Studio wrote the `risk`
  block but did not read it back when opening a project, so the next save
  replaced it with the defaults. It now round-trips. The block is documented
  as Workbench-only: the optimization reads neither it nor `risk.enabled`.
- **SLD frequency gauge ignored the system's frequency keys.** It used fixed
  values (load damping 0.01 pu, ROCOF limit 2 Hz/s, 50 Hz and 49 Hz with
  results loaded). It now uses `load_damping`, `rocof_limit`,
  `frequency_nominal` and `frequency_nadir_limit` of the edited system; a
  nominal frequency left at 50 Hz follows the buses' frequency and a nadir
  limit left at 49 Hz becomes 1 Hz below nominal.
- **HiGHS offered for the SOC and QC AC-OPF formulations.** HiGHS rejects
  second-order cone constraints, so these runs failed at the first solve.
  The Studio no longer offers it for `acopf_soc` / `acopf_qc`.
- **`esfex run --mode/--solver` accepted any value.** An unknown mode or
  solver was written into the configuration unchecked. Both are now checked
  against the values the configuration accepts (the solver name is
  case-insensitive), the help lists all of them, and the verbose header
  prints `Mode: (from config)` instead of `Mode: None`.
- **`esfex studio -o` had no effect.** File → Save now writes to the `-o`
  file, or to the `-c` file when `-o` is not given (it used to ask for a
  path), and relative data paths resolve against the `-c` file's folder.
  `launch_studio()` returned `None` even after a save; it now returns the
  saved configuration.
- **`Orchestrator(config_path="x.yaml")` with a string** raised
  `AttributeError`; a `str` path now works like a `Path`.
- **`environment.yml` pinned Python below 3.13** although ESFEX supports
  3.13; it now allows the same versions as pip (3.10–3.13).
- **No-effect settings are marked as such.** The `Curtailment` penalty the
  Studio writes was dropped on load; it now loads, like
  `coupling_slack_penalty`, `transport_congestion` and `storage_violation`,
  and all four are documented as having no effect and shown disabled in the
  Studio. `solver.scale_constraints` is read by nothing; it is documented as
  such and shown disabled.
- The loader names each generator that receives the non-fuel O&M adder in
  the log (INFO), with the resulting `maintenance_cost`.
