### Changed

- **Every stochastic scenario multiplier in the Studio changes the plan.**
  The Studio's scenario form offered 32 multiplier keys, but the model knew
  only 10 names, so the loader dropped the rest: only the demand growth and
  discount rate multipliers reached the model. `ScenarioMultipliers` now has
  a multiplier for each cost the stochastic master charges: besides the
  existing ten, `gen_fixed_cost`, `gen_decommissioning_cost`,
  `bat_decommissioning_cost`, `bat_invest_cost_power` and
  `bat_invest_cost_capacity` (both on top of `invest_cost_storage`),
  `penalty_loss_of_load`, `penalty_loss_of_reserve`, `penalty_curtailment`
  and `penalty_fre_penetration`. The Studio offers exactly these. Keys
  written by earlier Studio versions are translated on load and save
  (`gen_fuel_cost` and `fuel_price` → `fuel_cost`, `gen_maintenance_cost` →
  `maintenance_cost`, `transmission_invest_cost` →
  `invest_cost_transmission`, `fuel_price_growth_rate` → `fuel_price_growth`,
  `penalty_co2_cost` → `carbon_price`, `gen_invest_cost` → both technology
  investment costs). Keys that act on the same multiplier multiply, with a
  warning. Keys for costs the master problem does not have are dropped with
  a warning, and the Studio no longer offers them: start-up, electrolyzer,
  converter, fuel import, fuel storage and transport investment, battery
  maintenance, cost per MW·km, and the loss-of-fuel-supply and CO2-budget
  penalties. A loaded project shows only the multipliers its file sets.

### Fixed

- **Fuel-cost multiplier and fuel-cost curves.** In a stochastic scenario,
  `fuel_cost` and `fuel_price_growth` did not scale generators that use a
  piecewise fuel-cost curve; they do now.
