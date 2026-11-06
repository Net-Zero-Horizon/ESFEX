# Changelog text, group A (multi-system & CO2)

### Changed

- **Each system keeps its own CO2 budget.** When several systems were
  simulated together, the merged system took the first system's
  `co2_budget` for all of them and the largest `co2_budget_violation`
  penalty: in a two-island case every tonne the main island emitted over
  its budget was priced at the small island's 500 $/t, and the small
  island's emissions counted against the main island's budget. The
  dispatch now adds one CO2 budget constraint per system, over that
  system's nodes, with that system's `annual_budget`, `enabled` flag and
  `penalties.co2_budget_violation`. Single-system runs are unchanged. The
  master problem and Benders have no CO2 budget (only the CO2 price), so
  they are unaffected.

### Fixed

- **Inter-system fuel routes are fuel routes.** A fuel route traced in the
  Studio between two systems was saved as an electrical link (no type, no
  fuel), came back as a transmission link after reloading, and the run
  turned it into a power line. `meta_network.systems_links` entries now
  have `link_type` (`transmission`, the default, or `fuel_route`) and
  `fuel`; the Studio saves and reloads both, and the link form has a fuel
  selector for fuel routes. On merge, each connection of a fuel route link
  becomes two one-way primary-energy transport routes between the two
  nodes (capacity = `existing_capacity_MW` in fuel units/day,
  `transport_cost` = `cost_per_mw_km`, `losses_fraction` = `loss_factor`
  per 100 km, length = `distance_km`); it adds no electrical line,
  adjacency or transfer investment. Route investment follows the fuel's
  `fuel_infrastructure`, so the link's `max_investment_MW` and
  `investment_cost_per_MW` are not used (a warning is logged when set). A
  fuel route without a fuel is ignored with a warning, and the Studio
  validation flags it.
