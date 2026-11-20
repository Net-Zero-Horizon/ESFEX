# Reservoir Hydropower

The reservoir hydropower model gives hydroelectric generators an explicit water-energy budget instead of treating them as firm capacity. It is implemented in `add_reservoir_constraints!` (`power_system.jl`) and is shared by both the operational dispatch and the capacity-expansion master problem.

---


## 1. Overview


A generator becomes a reservoir unit when `reservoir_capacity` is non-zero at one or more nodes. Its energy is then tracked as a stored "bucket" (MWh-equivalent): generation draws the reservoir down, inflow and pumping fill it, and spillage and evaporation are losses. Reservoir hydro is therefore **energy-limited**, not firm — a key correction over treating must-run renewables as always-available capacity.

The model supports five behaviours, each independently optional:

1. **Water-energy budget** — the core balance, always active for a reservoir unit.
2. **Minimum environmental flow** — a mandatory ecological release floor.
3. **Seasonal storage** — water carried across the year between representative periods (via TSAM inter-period linking).
4. **Hydraulic cascade** — an upstream unit's release feeds a downstream reservoir.
5. **Head dependence** — a depleted reservoir delivers less peak power.

All constraints are linear, so the model remains an LP (no integers, no bilinear terms).

---


## 2. Decision Variables

Per reservoir generator \(g\), node \(b\), and hour \(t\):

| Variable | Domain | Units | Description | Julia name |
|----------|--------|-------|-------------|------------|
| \(L_{g,b,t}\) | \(\mathbb{R}_+\) | MWh-eq | Reservoir storage level, \(t = 1\ldots H{+}1\) | `reservoir_level[g,b,t]` |
| \(P_{g,b,t}\) | \(\mathbb{R}_+\) | MW | Turbined generation | `gen_output[g,b,t]` |
| \(S_{g,b,t}\) | \(\mathbb{R}_+\) | MW-eq | Spillage (release without generation) | `reservoir_spillage[g,b,t]` |
| \(U_{g,b,t}\) | \(\mathbb{R}_+\) | MW | Pump-back power (charging the reservoir) | `reservoir_pump[g,b,t]` |

Parameters: reservoir capacity \(C\), turbine efficiency \(\eta\), pump efficiency \(\eta_p\), evaporation rate \(\rho\), hourly inflow \(W_t\) (MW-eq, read from `reservoir_inflow_file` as given, not clipped), minimum/maximum level fractions \(\ell^{\min},\ell^{\max}\), initial fraction \(\ell^0\), minimum release \(R^{\min}\), head-min factor \(\phi\), cascade delay \(\tau\), and rated power \(\bar P\).

---


## 3. Constraints

### RES-1: Water balance

The reservoir level evolves by inflow (natural + cascade), turbining, pumping, spillage and evaporation:

\[
L_{g,b,t+1} = L_{g,b,t}\,(1-\rho) + W_{g,b,t} + \Phi_{g,b,t} - \frac{P_{g,b,t}}{\eta} + \eta_p\,U_{g,b,t} - S_{g,b,t}
\]

where \(\Phi_{g,b,t}\) is the cascade inflow (RES-5, zero for a unit with no upstream feeders). Constraint `res_dynamics_g{g}_b{b}_t{t}`.

### RES-2: Level band

\[
\ell^{\min} C \le L_{g,b,t+1} \le \ell^{\max} C
\]

Pumping is bounded by the pump capacity, and spillage is forced to zero when `reservoir_spillage_allowed` is false. Constraints `res_min_level_*`, `res_max_level_*`, `res_pump_limit_*` / `res_no_pump_*`, `res_no_spill_*`. In the dispatch's `development` mode the capacity \(C\) in the level band includes a reservoir investment variable bounded by `reservoir_invest_max` (`res_invest_limit_*`). In the master, \(C\) includes the reservoir capacity built up to that year (§4).

### RES-3: Boundary condition

The starting level is fixed, and each period is **cyclic**: the end level returns to the start, within `soc_end_tolerance` \(\epsilon\):

\[
L_{g,b,1} = L^{0}_{g,b}, \qquad (1-\epsilon)\,L^{0}_{g,b} \le L_{g,b,H+1} \le (1+\epsilon)\,L^{0}_{g,b}
\]

\(L^0_{g,b} = \ell^0 C\) in the first window of a year and in the master problem. In rolling-horizon dispatch, later windows start from the level at the end of the previous window's last kept hour, and the end condition is relative to that level, so over a year the level can drift by up to \(\epsilon\) per window. Constraints `res_initial_*`, `res_end_lower_*`, `res_end_upper_*`.

With **seasonal storage** enabled (see §4), the cyclic closure is replaced: the period starts at a free level and ends at that level (less its evaporation) plus the period's change (`res_end_seasonal_*`), which an inter-day level carries through the year, so water is carried across periods rather than reset each one.

### RES-4: Minimum environmental flow

When `reservoir_min_release` \(R^{\min} > 0\), the water leaving the reservoir (turbined and/or spilled) must meet the mandatory downstream flow:

\[
\frac{P_{g,b,t}}{\eta} + S_{g,b,t} \ge R^{\min}
\]

Constraint `res_min_release_g{g}_b{b}_t{t}`.

### RES-5: Hydraulic cascade

Let \(\mathcal{U}(g)\) be the reservoirs that discharge into \(g\), each with travel delay \(\tau_u\). The cascade inflow injected into \(g\)'s primary node is the upstream release (turbined + spilled), shifted by the delay:

\[
\Phi_{g,b,t} = \sum_{u \in \mathcal{U}(g)} \left( \frac{P_{u,t-\tau_u}}{\eta_u} + S_{u,t-\tau_u} \right), \qquad t - \tau_u \ge 1
\]

Releases scheduled before the period start (\(t - \tau_u < 1\)) are outside the window and not carried in.

### RES-6: Head-dependent power limit

When `reservoir_head_min_factor` \(\phi < 1\), a low reservoir has low head and cannot reach nameplate power. The available output scales linearly with the fill level, from \(\phi\,\bar P\) at the minimum level to \(\bar P\) at the maximum:

\[
P_{g,b,t} \le \bar P\left( \phi + (1-\phi)\,\frac{L_{g,b,t} - \ell^{\min} C}{(\ell^{\max} - \ell^{\min})\,C} \right)
\]

Constraint `res_head_limit_g{g}_b{b}_t{t}`. Evaluated at the start-of-step level \(L_{g,b,t}\), this bound is linear in the level variable — the tractable piecewise-linear treatment of head dependence. In the master, \(C\) in the numerator (the minimum level) is the in-service capacity, existing plus built; the span in the denominator keeps the existing capacity, since a built capacity there would make the bound nonlinear. The fraction is then exact at the minimum level of an enlarged reservoir and overstates the head above it (it reaches 1 before the top, where \(\bar P\) binds anyway). The dispatch receives the master's builds as existing capacity, so its span is the in-service one.

---


## 4. Master-Problem Integration

In the capacity-expansion master problem the same balance (RES-1–RES-6) is enforced inside every representative-day subproblem, so investment decisions see hydro as an energy-limited resource rather than firm MW. Each representative day (TSAM period, Benders subproblem) uses the inflow \(W_t\) of its own hours, like the availability profiles.

**Reservoir investment.** Where a unit has a reservoir and `reservoir_invest_max` > 0, the master may enlarge it (`reservoir_investment`, at `reservoir_invest_cost` per MWh, cumulative cap `cumul_res_*`). Everything that scales with the reservoir size uses the in-service capacity, the existing one plus what was built up to that year: the level band of each representative day, its initial and end level (`reservoir_initial_level` × the in-service capacity, as the dispatch starts it), the TSAM inter-day levels and the minimum level of the head fraction (RES-6, whose span keeps the existing capacity). Earlier versions kept the existing capacity for the initial level, the TSAM boundaries and the head fraction, so water could not be carried across TSAM periods in the capacity built and the day started at a lower level than the dispatch. The capacity built reaches the dispatch as existing reservoir capacity from its build year on, in perfect-foresight and myopic runs; a myopic year's master also counts what earlier years built and has that much less left to build. Benders subproblems include it too, as a copy of the capacity the master built.

**Seasonal storage.** When `tsam_inter_period_linking` is enabled (TSAM time aggregation), the master carries the reservoir level through the year as for the batteries, by the superposition of Kotzur et al. [**[85]**](../reference/bibliography.md#ref85) ([capacity expansion §8.5.3](capacity-expansion.md#853-seasonal-storage-inter-period-linking)): each period keeps its own level trajectory from a free start, and an inter-day level, one per day of the year and year-cyclic, moves by the change of the period that day maps to and keeps each day's levels within the minimum and maximum level of the in-service capacity. A reservoir can thus be filled in a wet season and drawn down in a dry one, while each period's water counts in the year as often as its cost and energy (its weight): the weighted release equals the weighted inflow less spill and evaporation. The evaporation decays the inter-day level by \((1-\epsilon)^{H}\) per day. With head dependence (RES-6) each period's start level is held at or below the inter-day level of each of its days, so the head the period sees is never more than the year gives. Earlier versions chained the periods once each in chronological order while their water counted × weight, which let the master release more water over the year than flowed in.

---


## 5. Configuration

See [GeneratorConfig — reservoir fields](../reference/config-reference.md#generatorconfig) for the YAML schema and the [generator element form](../gui/element-forms.md#reservoir-mode-hydro-generators) for the Studio GUI fields. The seasonal-storage toggle lives in [Global Settings](../gui/global-settings.md).
