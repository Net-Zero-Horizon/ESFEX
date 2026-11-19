### Added

- **Why did the master build this? Binding constraints, duals and paid
  slacks are recorded per master year.** After each master solve (each year
  of a myopic run, the single solve of perfect foresight) ESFEX now groups
  the model's rows by constraint family (RE target, RE growth cap,
  curtailment cap, annual budget, capacity adequacy, CO2 budget, build
  limits, hourly capacity, reserves, storage…, with the constraint catalog's
  codes) and reports which of them bind, their largest and summed dual (the
  cost of tightening the constraint by one unit, in $ of that year), the
  rows with the largest duals by name, and every slack or penalty variable
  the plan pays for (RE shortfall, budget overrun, adequacy shortfall,
  curtailment above the cap, CO2 violation, and the representative days'
  load shed and reserve shortfalls scaled to the year) with its amount,
  price and cost. A few INFO lines sum it up in the log, e.g.
  `Master 2025 binding: Annual budget (investment) (10 $/$, at its penalty)`
  and `Master 2025 paid slacks: Budget overrun $11,000,000 at 10 $/$
  ($110,000,000)`, and the full record is written to the results file under
  `/diagnostics/master/year_{YYYY}/{binding,top,slacks}` (perfect foresight:
  rows without a year in `all`), readable with
  `esfex.io.master_diagnostics.read_master_diagnostics`. Until now the only
  way to see why the plan looked as it did was to rerun with the master LP
  exported. A dual of a row with its own slack is measured per unit of that
  slack, so an unattainable RE target shows exactly its
  `FRE_penetration_loss` and the curtailment cap its excess price per MWh. Not
  recorded for Benders or MGA/SPORES runs; a master without duals (integer
  variables) reports its tight rows only. New Julia functions
  `master_diagnostics` and `master_paid_slacks`, adapter method
  `MasterProblemAdapter.diagnostics()`. Documented in the master-problem
  guide ("Why did the master build this?"), the HDF5 output schema and the
  constraint catalog ("Families in the binding report").
