# Group D (master & dispatch) — changelog text for [Unreleased]

### Added

- **Every electrolyzer is dispatched, and its results are exported.** Only
  the first entry under `electrolyzers` reached the dispatch; the others
  were ignored, and no electrolyzer result was kept. All entries are now
  modelled (`PowerSystemInput` takes a vector, `electrolyzers`; the single
  `electrolyzer_config` keyword is still accepted), and each one's power
  (MW) and hydrogen output (kg/h, the conversion of the primary-energy
  hydrogen balance) are kept in `YearResults.electrolyzer_power` /
  `electrolyzer_h2` and written to the HDF5 file as
  `electrolyzer_power/{name}` and `electrolyzer_h2/{name}`.

### Changed

- **Life extension of units with `invest_cost` 0 is no longer free.** The
  master prices a life-extended MW at `invest_cost × life_extension_cost_factor`
  per year, and existing units default to `invest_cost` 0 in the Studio, so
  they could be kept forever at no cost. Where the cost is 0 at a node, it
  is now taken from the candidate technology with the same fuel and type
  (for batteries, the battery technology named by `technology`, else the
  first one). A unit with no candidate stays free and is named in a warning.
- **`life_extension_cost_factor` is applied.** The master always used a
  hard-coded 0.3; it now uses the system's value (default 0.20).
- **Myopic planning follows the RE target path.** Each one-year master
  targeted the RE share reached the year before, so `TARGET_RE_PENETRATION`
  never entered a myopic run. Each year now targets its point on the
  perfect-foresight path (initial share → target over the horizon, steps
  clamped to `MIN_ANNUAL_INCREMENT` / `MAX_ANNUAL_INCREMENT`); the increment
  bounds still start from last year's share.
- **Sector priorities in load shedding.** In the dispatch, a sector's shed
  load was not capped by its demand, so all unserved energy was booked to
  one sector and priced at the cheapest (or, where sector data was missing,
  the dearest) multiplier. In the master's representative days, the
  sectoral shed was an extra injection next to the bus's load shedding, so
  a day could shed up to about twice its demand and the plain-VOLL load
  shedding absorbed every MWh, ignoring criticality. Both models now split a
  bus's shed into per-sector amounts capped by each sector's demand plus an
  unclassified remainder (demand that no sector covers) priced at VOLL;
  sectors cost VOLL × criticality, cheapest first.
- **Missing availability profiles.** A `Renewable` generator or technology
  without `availability_file` (reservoir hydro excepted) runs at capacity
  factor 1.0 every hour; this is now logged once per unit at startup. A
  configured profile that cannot be found now stops the run with the unit
  and path named: a generator used to be dropped from the dispatch, and a
  technology crashed with a `KeyError`.
- **`LOSS_DEMAND_TRHESHOLD` is deprecated.** It was passed to the model but
  no constraint used it. It is no longer passed, the Studio no longer shows
  or writes it, and old case files that contain it still load.

### Fixed

- **Reservoir pumping on multi-bus nodes.** The DC-OPF and AC-OPF bus
  balances looked up a reservoir's capacity by node index in per-bus
  arrays, so the pump of a reservoir off its node's first bus was left out
  of the balance: pumped water was free.
- **Built technology capacity keeps the technology's bidding curve.** The
  virtual generators that carry master investments into the dispatch got
  the flat `fuel_cost` only; they now carry `fuel_cost_curve`. A curve at a
  bus where a unit has no capacity no longer raises an error in the
  dispatch.
- **Development-mode dispatch could buy a 0-MW line but not use it.** The
  flow on a candidate corridor below 0.1 MW stayed fixed to 0 even after
  investment (Julia API only; the runner dispatches in economic-dispatch or
  unit-commitment mode).
- **Benders load-shedding penalty cap** iterated the per-bus load-shed
  variables by node count.
