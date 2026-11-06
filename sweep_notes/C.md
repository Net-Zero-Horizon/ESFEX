### Changed

- **Primary energy: one fuel tank per node, balanced every hour.** The fuel
  model had two storage layers, a periodic balance and an hourly tank, tied
  together by slack variables. Fuel shipped along a route entered only the
  periodic balance, so generators and non-electric users at a node fed by
  transport ran on the penalised shortfall; non-electric use was counted in
  both layers (50 units in a tank served 25 of non-electric demand); and the
  periodic shortfall sat on the consumption side, where it acted as a sink.
  Each fuel now has a single tank per node:
  level = previous level + (local supply + arriving shipments) × storage
  efficiency − fuel drawn − departing shipments, and the fuel drawn plus the
  shortfall must cover the fuel for power and the non-electric use of each
  hour. Periods keep the supply limits, the non-electric demand per sector
  and the route shipments; a period's shipment leaves evenly over its hours
  and arrives after the route's transit time (now counted in time steps,
  no longer rounded to whole periods), less transport losses. Fuel arriving
  in an hour can be used in that hour, so a shortfall now means fuel is
  really missing. The periodic storage levels, their linking slacks and the
  periodic shortfall are gone: `penalties.coupling_slack_penalty` has no
  effect, the `pe_coupling_slack_cost` result is always 0, and
  `total_loss_of_supply` is the hourly shortfall. Route capacity per period
  now uses the period's real length when `resolution_hours` > 1.
- **Scheduled interruptions happen once, in absolute horizon hours.**
  `start_hour`/`end_hour` of an outage window were taken as hours within
  the year, so a window repeated in every simulated year and a
  `start_hour` of 8760 or more never applied. They are now horizon hours
  (hour 0 = 1 January of the base year, 8760 h per year), as documented.
  The new `recurring: true` repeats a window every year at the same hours
  of the year (also in the Studio calendar, "Repeat every year"). This
  applies to generators, batteries, lines, transformers and converters.
- **Fuel-source disruptions use horizon hours.** `disruption_start_hour` /
  `disruption_end_hour` (and `fuel_source` interruptions) were compared with
  the step numbers of each dispatch window, so the cut hit the start of
  every window and never the hours entered. They are now absolute horizon
  hours, located with the window's year and start hour; a `fuel_source`
  interruption is applied in the dispatch windows it overlaps.
