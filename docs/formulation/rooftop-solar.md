# Rooftop Solar Model

Rooftop solar is distributed PV that follows an exogenous adoption curve and is treated as behind-the-meter generation: it lowers the demand the operational dispatch must serve. The profiles come from the external `rooftex` library; the runner (`Orchestrator._generate_rooftop_solar` in `runner.py`) turns them into hourly generation per node. `models/solar_rooftop.py` holds the stand-alone helper functions documented in [API: Models Solar Rooftop](../api/models-solar-rooftop.md); the simulation does not call them.

## Overview

1. `rooftex.generate_profiles` gives one year of hourly availability per node, an adoption factor per node and a maximum potential per node.
2. For each simulated year the runner computes the installed capacity from the potential, the adoption factor, an S-curve and degradation, and multiplies it by the availability profile.
3. In each operational window the rooftop output is subtracted from demand before the Julia model is built.

Rooftop solar is active when the system sets `SIM_ROOFTOP: true` and has a `rooftop_solar_config` block. If the peak rooftop output stays below 1 MW, it is dropped.

## Adoption and Installed Capacity

The installed rooftop capacity at node \(n\) in year \(y\) is

\[
\bar{P}^{roof}_n(y) = P^{max}_n \cdot a_n \cdot s(y) \cdot \left(1 - \frac{\delta \,(y - y_b)}{2}\right)
\tag{ROOF-1}
\]

\[
s(y) = \frac{1}{1 + e^{-10\,(\min(1,\,(y - y_b)/(y_t - y_b)) - 0.5)}}
\tag{ROOF-2}
\]

where:

- \(P^{max}_n\) is the maximum potential (MW): `rooftop_max_potential[n]` if the system sets it, otherwise the `rooftex` value. Without `rooftop_max_potential`, rooftex draws a random potential per node, \((50 + \Gamma(2, 30)) \cdot (0.5 + u_n)\) MW, with \(u_n \sim \text{Beta}(2,2)\) an urbanization factor.
- \(a_n\) is the rooftex adoption factor, a logistic curve evaluated at the last simulated year. Its ceiling and growth rate depend on `adoption_scenario` (low / medium / high: ceiling 0.30 / 0.50 / 0.70, rate 0.05 / 0.08 / 0.12 per year, both scaled by \(0.8 + 0.4\,u_n\), ceiling at most 0.9).
- \(s(y)\) is a fixed S-curve over the period from `base_year` \(y_b\) to `target_year` \(y_t\) of `rooftop_solar_config`.
- \(\delta\) is `degradation_rate` (linear, applied at half the elapsed years as a fleet average).

## Availability Profile

rooftex builds the hourly availability \(\alpha_{n,t} \in [0,1]\) for one year from a clear-sky base profile (performance ratio 0.75), multiplied by a node factor, a daily weather factor and a cloud pattern, plus hourly noise in daylight hours:

| `weather_variability` | Daily weather \(\sigma\) | Node factor \(\sigma\) |
|-----------------------|--------------------------|------------------------|
| `low` | 0.05 | 0.10 |
| `normal` | 0.15 | 0.20 |
| `high` | 0.25 | 0.30 |

The random draws use `simulation_seed`. The same one-year profile is repeated every year. The hourly generation is

\[
P^{roof}_{n,t} = \bar{P}^{roof}_n(y_t) \cdot \alpha_{n,t}
\tag{ROOF-3}
\]

## Power System Integration

Rooftop output is behind the meter. In each operational window the runner subtracts it from the node demand before calling Julia:

\[
D^{net}_{n,t} = \max\left(D_{n,t} - P^{roof}_{n,t},\; 0\right)
\]

Output above demand is self-curtailed outside the model. Sectoral demands are scaled by the same ratio \(D^{net}/D\). The dispatch receives no rooftop generation, so the Julia rooftop variables and constraints (`rooftop_curtailment`, `rooftop_curt_limit_*`, penalty `penalties.rooftop_curtailment`) are not created in a normal run.

## Capacity Expansion

The master problem does not use rooftop solar: it plans on the gross demand, without subtracting rooftop output. Rooftop solar therefore lowers the energy the dispatch must supply but does not reduce the capacity the master builds.

## Configuration

```yaml
SIM_ROOFTOP: true
rooftop_max_potential: [25.0, 15.0]   # MW per node (optional)
rooftop_solar_config:
  adoption_scenario: medium           # low, medium, high
  weather_variability: normal         # low, normal, high
  simulation_seed: 42
  base_year: 2025
  target_year: 2050
  degradation_rate: 0.005
```

| Parameter | Default | Used | Description |
|-----------|---------|------|-------------|
| `SIM_ROOFTOP` (system) | `false` | Yes | Enable rooftop solar |
| `rooftop_max_potential` (system) | `[]` | Yes | Maximum potential per node (MW); empty = rooftex value |
| `adoption_scenario` | `"medium"` | Yes | rooftex adoption scenario |
| `weather_variability` | `"normal"` | Yes | rooftex variability level |
| `simulation_seed` | `42` | Yes | Random seed |
| `base_year`, `target_year` | 2025, 2050 | Yes | Span of the S-curve (ROOF-2) |
| `degradation_rate` | `0.005` | Yes | Annual degradation (ROOF-1) |
| `systems_per_node`, `avg_system_size` | `null` | Yes | Per-node rooftop systems and their size (kW); with both set, rooftex's potential is systems × size (the system's `rooftop_max_potential` still takes precedence) |
| `max_adoption`, `adoption_rates` | `null` | Yes | Adoption ceiling and growth rate per scenario (`low`, `medium`, `high`); unset: rooftex defaults |
| `initial_adoption` | `null` | Passed | Passed to rooftex, which (0.1.1) does not use it |
| `performance_ratio` | 0.75 | Yes | Scales the rooftex profile |

## Output

The yearly rooftop series is stored in `YearResults.rooftop_generation` and written to the HDF5 scenario group as `rooftop_generation` (hours × nodes, MW).

## Limitations

- **Behind the meter only**: rooftop output never reaches the network; output above local demand is lost.
- **Not in capacity planning**: the master problem ignores rooftop solar.
- **Fixed adoption**: the trajectory is exogenous and does not respond to prices.
- **Aggregated**: all systems at a node share one profile; orientation, shading and home batteries are not modelled.

---

## References

The distributed generation market demand modeling follows the NREL dGen model by Sigrin et al. [**[54]**](../reference/bibliography.md#ref54). Rooftop suitability estimation methods are reviewed by Melius et al. [**[55]**](../reference/bibliography.md#ref55). IRENA renewable cost benchmarks [**[48]**](../reference/bibliography.md#ref48) are used for default PV cost assumptions.

See the [full bibliography](../reference/bibliography.md) for complete citation details.
