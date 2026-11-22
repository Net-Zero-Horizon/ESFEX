# OTEC Studio

Ocean Thermal Energy Conversion (OTEC) site assessment and plant design workbench. Access via **Workflows > OTEC Studio**.

OTEC exploits the temperature difference between warm surface seawater and cold deep-ocean water to drive a thermodynamic cycle and generate electricity. The technology is viable in tropical and subtropical waters (approximately ±30° latitude) where the temperature difference between the surface and the deep ocean exceeds 20 °C year-round. OTEC Studio uses the [otex](https://pypi.org/project/otex/) library for plant sizing, optimization, economics and uncertainty analysis, and the functions in `esfex.models.otec_models` for cold-water pipe sizing, transmission and annual capacity-factor profiles.

OTEC Studio is a non-modal window, not a step-by-step wizard. You can move freely between its seven tabs:

1. **Site & Resource**: candidate sites, climate scenario, resource characterization, siting hazards.
2. **Cycle & Design**: thermodynamic cycle, working fluid and operating point, with T-s and P-h diagrams.
3. **Optimization ★**: inverse design that minimizes LCOE for a site, plus an LCOE surface explorer.
4. **Operation**: time-series operation against a seasonal seawater profile, cold-water pipe sizing, and an 8,760-hour capacity-factor profile.
5. **Economics**: CAPEX breakdown and lifetime LCOE with performance degradation.
6. **Uncertainty & Sensitivity**: Monte Carlo, Tornado and Sobol analyses.
7. **Regional ★**: batch optimization over every site of a region, with development-zone clustering.

OTEC Studio does not change the system model. Results leave the Studio as exported files (CSV, HDF5); for example, export the annual capacity-factor profile from the Operation tab and assign it as the availability file of an OTEC generator (see [Availability Profiles](../user-guide/availability-profiles.md)).


---


## Scenarios

The bar at the top of the window manages scenarios. A scenario is one set of design settings (cycle, fluid, power, cost level, discount rate, lifetime, depth limits, degradation, climate scenario) plus the results computed for it. All scenarios share the project's site and ocean data, so data is not downloaded again for each scenario.

| Control | Description |
|---------|-------------|
| **Scenario** | Selects the active scenario. Panels read from and write to the active scenario. |
| **New** | Adds a scenario with default settings. |
| **Branch** | Copies the active scenario's settings into a new scenario, so you can change one setting and compare. |
| **Remove** | Deletes the active scenario (disabled when only one is left). |
| **Compare ☰** | Opens a table of all scenarios (Scenario, Cycle, Fluid, LCOE, Net Power, CAPEX, Has results) with an LCOE bar chart and **Export CSV**. |


---


## Site & Resource

Manage candidate sites and choose the one the other panels use.

- **Candidate sites**: a table with Name, Lon, Lat, Warm T, Cold T and Dist (km). Use **Add site** and **Remove** to edit it. **Use as active resource →** pushes the selected site into the project; the Optimization, Economics and Operation panels then fill their site inputs from it.
- **Climate scenario (CMIP6 / SSP)**: choose a scenario (ssp126, ssp245, ssp370, ssp585; default ssp245), a year (2030 -- 2100, default 2050) and a cold-water depth (default 1000 m), then **Fetch & apply delta** to shift the site's design temperatures by the projected climate change. Requires network access.
- **Resource characterization (seasonal ΔT, Carnot)**: **Characterize selected site** shows monthly warm and cold temperatures, the monthly ΔT and the Carnot efficiency at the mean point. Without downloaded daily ocean data, it uses a synthetic seasonal series built from the site temperatures.
- **Siting hazards (MPA / AIS / seismic / cyclone)**: **Enrich hazards for all sites** adds marine-protected-area, ship-traffic density, seismic (PGA) and cyclone frequency columns to the site table. Requires network access.

Network operations run in the background; if they fail (offline, no credentials) the panel stays usable with manually entered site data.


## Cycle & Design

Explore the thermodynamic cycle for the active scenario.

| Input | Default | Description |
|-------|---------|-------------|
| Cycle | Rankine — Closed | Rankine — Closed, Rankine — Open (flash), Rankine — Hybrid, Kalina (NH₃-H₂O) or Uehara (two-stage) |
| Working fluid | ammonia | ammonia, R134a, R245fa, propane, isobutane |
| NH₃ concentration | 0.70 | Ammonia mass fraction for the Kalina and Uehara mixtures (0.50 -- 0.95) |
| T evaporator | 25 °C | Evaporator temperature of the operating point |
| T condenser | 8 °C | Condenser temperature of the operating point |

The panel draws the cycle on **T-s diagram** and **P-h diagram** tabs and lists all states in a **State table**; they update as you change the inputs. **Apply to scenario config** writes the cycle, fluid and composition to the active scenario, which the Optimization panel uses.


## Optimization ★

Inverse design with OTEX's optimizer: find the plant design that minimizes LCOE at a site.

**Site**: Warm-water T (default 26 °C), Cold-water T (5 °C), Distance to shore (20 km), Latitude and Longitude. Filled from the active resource when one is set.

**Design bounds:**

| Bound | Default |
|-------|---------|
| Power min / max | 1 -- 500 MW |
| ΔT warm min / max | 1 -- 6 K |
| ΔT cold min / max | 1 -- 6 K |
| CW depth min / max | 600 -- 3000 m |

**Constraints (cap → interior optimum)**: optional caps, each enabled by its checkbox: Max CAPEX (300 M$), Max net power (100 MW), Max AEP (500,000 MWh), Max gross power (200 MW), Max parasitic ratio (0.4). Without a cap, LCOE falls steadily with plant size and the optimum sits at the maximum-power bound; a cap gives an interior cost/size optimum.

**Optimize (minimize LCOE)** runs in the background and shows the **Optimal design**. The result is stored on the active scenario.

**LCOE surface**: choose two design variables (Gross power, ΔT warm, ΔT cold, CW depth) for the X and Y axes and click **Compute surface** to map LCOE around the optimum.


## Operation

Time-series operation of the nominal plant against a seasonal seawater temperature profile.

**Design point & seawater profile**: Design warm T (26 °C), Design cold T (5 °C), Distance to shore (20 km), Warm mean (26 °C) and Warm seasonal ± (2 K), Cold mean (5 °C) and Cold seasonal ± (0.5 K), and the number of time steps. **Run operation** simulates the plant and plots **Power**, **Parasitics**, **Temperatures** and **Performance**. A diagnosis line attributes any drop below nameplate to a smaller temperature lift (lower gross power) or to higher seawater pumping parasitics.

**Pipe & CWP**: enter the cold-water depth (default 1000 m), pipe diameter (10 m) and slope (7°), then **Analyze pipe + sweep**. The tab reports pipe length, pumping power and its share of gross power, transmission efficiency and losses (see [CWP Pipe Length](#cwp-pipe-length) to [Transmission Efficiency](#transmission-efficiency)), and plots a sweep over pipe diameters.

**Annual CF (8760h)**: enter the nominal capacity factor (default 0.914) and the design ΔT (21 K), then **Generate** an hourly capacity-factor profile (see [Capacity Factor from Temperature](#capacity-factor-from-temperature)). The daily series comes from the synthetic seasonal profile. **Export CSV** saves the 8,760 values.


## Economics

On-design cost model with lifetime power degradation.

| Input | Default | Description |
|-------|---------|-------------|
| Warm-water T / Cold-water T | 26 °C / 5 °C | Site temperatures |
| Distance to shore | 20 km | Sets the transmission efficiency |
| Cost level | low_cost | `low_cost` or `high_cost` OTEX cost scheme |
| Degradation | constant | `constant` (annual rate, default 0.005) or `logistic` (L 0.30, k 0.30, t₀ 15 yr) |

**Analyze economics** shows a **Summary** (nameplate LCOE versus the NPV-based LCOE that includes degradation), the **CAPEX breakdown** by component, and **Power over life**.


## Uncertainty & Sensitivity

Three analyses share one editable table of uncertain parameters (OTEX defaults). For each parameter you can set the distribution and its bounds: P1/P2 are (mean, std) for a normal distribution and (low, high) for a uniform one. Warm T and Cold T (default 26 °C and 5 °C) set the site.

| Tab | Settings | Output |
|-----|----------|--------|
| **Monte Carlo** | Samples (50 -- 5000, default 500), Seed (42), Metric (lcoe, net_power, capex, opex) | Distribution and statistics of the chosen metric, using Latin Hypercube Sampling (see [LHS](#latin-hypercube-sampling-lhs)) |
| **Tornado** | Variation ±% (1 -- 50, default 10), Output (lcoe, capex) | One-at-a-time sensitivity chart |
| **Sobol** | Samples (16 -- 2048, default 256), Output (lcoe, capex) | First-order and total-order indices (see [First-Order Sobol Index](#first-order-sobol-index)) |

Results are stored on the active scenario.


## Regional ★

Batch inverse design over every feasible site in a region (OTEX `run_regional_optimization`). The run downloads ocean data and is slow; it runs in the background.

- **Region**: any region OTEX knows (countries and territories).
- **Cap CAPEX**: optional per-site CAPEX cap (default 300 M$), for questions such as "which sites stay viable if CAPEX per site is capped?".
- **Run regional** shows a site map coloured by LCOE, a portfolio summary and a table. **Feasible only** filters the view.
- **Export CSV** saves the results as CSV or HDF5.
- **Cluster zones** groups feasible sites into development-zone polygons (DBSCAN, buffered by the scenario's zone buffer, 10 km by default; the LCOE threshold is the highest LCOE in the results, so every feasible site is eligible) and draws them on the panel's map. The zones are not added to the system model.


---


## Mathematical Formulations


### Carnot Efficiency

The theoretical maximum (Carnot) efficiency of the OTEC cycle:

\[
\eta_C = 1 - \frac{T_{\text{cold}}}{T_{\text{hot}}} \tag{OTEC-1}
\]

| Symbol | Units | Description |
|--------|-------|-------------|
| \(\eta_C\) | -- | Carnot efficiency (dimensionless) |
| \(T_{\text{hot}}\) | K | Warm water temperature (surface intake, converted from °C: \(T_K = T_{°C} + 273.15\)) |
| \(T_{\text{cold}}\) | K | Cold water temperature (deep intake, converted from °C) |

For typical OTEC conditions (\(T_{\text{hot}} \approx 300\) K, \(T_{\text{cold}} \approx 278\) K), the Carnot efficiency is approximately 7.3%. Practical cycle efficiencies are lower due to irreversibilities.


### Capacity Factor from Temperature

The annual CF profile of the Operation tab scales the nominal capacity factor with the daily temperature difference:

\[
CF_d = CF_{\text{nom}} \times \min\!\left(\max\!\left(\frac{T_{\text{warm},d} - T_{\text{cold},d}}{\Delta T_{\text{design}}},\, 0\right),\, 1.2\right) \tag{OTEC-1a}
\]

Each daily value is repeated for 24 hours to give 8,760 hourly values.


### Net Power Output

The net electrical power delivered after subtracting all parasitic loads:

\[
P_{\text{net}} = P_{\text{gross}} - P_{\text{pump,CWP}} - P_{\text{pump,WWP}} - P_{\text{aux}} \tag{OTEC-3}
\]

| Symbol | Units | Description |
|--------|-------|-------------|
| \(P_{\text{gross}}\) | kW | Gross turbine-generator output |
| \(P_{\text{pump,CWP}}\) | kW | Cold water pipe pumping power |
| \(P_{\text{pump,WWP}}\) | kW | Warm water pipe pumping power |
| \(P_{\text{aux}}\) | kW | Auxiliary plant loads (controls, lighting, etc.) |

For large OTEC plants, parasitic pumping power typically consumes 20-30% of gross output, which is why the Pipe & CWP tab exposes the pipe diameter trade-off.


### CWP Pipe Length

The cold water pipe length from intake depth and slope angle:

\[
L_{\text{pipe}} = \frac{d}{\sin \theta} \tag{OTEC-4a}
\]

| Symbol | Units | Description |
|--------|-------|-------------|
| \(d\) | m | Cold water intake depth |
| \(\theta\) | rad | CWP slope angle from horizontal (minimum 1°) |


### CWP Friction Loss (Darcy-Weisbach)

The pressure drop along the cold water pipe due to friction:

\[
\Delta P = f \cdot \frac{L}{D} \cdot \frac{\rho v^2}{2} \tag{OTEC-4}
\]

Equivalently expressed as head loss:

\[
h_f = f \cdot \frac{L \cdot v^2}{2 \cdot D \cdot g} \tag{OTEC-4b}
\]

| Symbol | Units | Description |
|--------|-------|-------------|
| \(f\) | -- | Darcy friction factor (approximately 0.015 for large-diameter smooth pipes) |
| \(L\) | m | Pipe length |
| \(D\) | m | Internal pipe diameter |
| \(\rho\) | kg/m³ | Seawater density (1025 kg/m³) |
| \(v\) | m/s | Flow velocity (\(v = Q / A\), where \(Q\) is the volumetric flow rate and \(A = \pi D^2 / 4\)) |
| \(g\) | m/s² | Gravitational acceleration (9.81 m/s²) |
| \(h_f\) | m | Friction head loss |

The cold water flow rate is estimated as approximately 4 m³/s per MW of gross power, reflecting typical OTEC cycle heat balance requirements.


### Pumping Power

The electrical power consumed by the cold water circulation pump:

\[
P_{\text{pump}} = \frac{\rho \cdot g \cdot Q \cdot h_f}{\eta_{\text{pump}}} \tag{OTEC-5}
\]

| Symbol | Units | Description |
|--------|-------|-------------|
| \(Q\) | m³/s | Cold water volumetric flow rate |
| \(h_f\) | m | Friction head loss from [OTEC-4b](#cwp-friction-loss-darcy-weisbach) |
| \(\eta_{\text{pump}}\) | -- | Pump efficiency (default 0.80) |


### Levelized Cost of Energy (LCOE)

\[
LCOE = \frac{CRF(r, n) \times CAPEX + O\&M}{P_{\text{net}} \times CF \times 8760} \tag{OTEC-6}
\]

| Symbol | Units | Description |
|--------|-------|-------------|
| \(CRF\) | 1/yr | Capital Recovery Factor |
| \(CAPEX\) | $ | Total capital expenditure |
| \(O\&M\) | $/yr | Annual operation and maintenance cost |
| \(P_{\text{net}}\) | kW | Net power output |
| \(CF\) | -- | Capacity factor |
| \(8760\) | h/yr | Hours per year |

The LCOE represents the constant price per unit of energy that, over the plant lifetime, yields a net present value of zero [**[48]**](../reference/bibliography.md#ref48).


### Capital Recovery Factor (CRF)

\[
CRF(r, n) = \frac{r(1 + r)^n}{(1 + r)^n - 1} \tag{OTEC-7}
\]

| Symbol | Units | Description |
|--------|-------|-------------|
| \(r\) | -- | Discount rate (annual) |
| \(n\) | yr | Plant lifetime |

Converts a lump-sum present value into an equivalent uniform annual cost over \(n\) years at discount rate \(r\).


### Latin Hypercube Sampling (LHS)

\[
x_i^{(j)} \sim U\!\left(\frac{j-1}{N},\; \frac{j}{N}\right), \qquad j = 1, \ldots, N \tag{OTEC-8}
\]

For each input variable \(x_i\), the cumulative distribution is partitioned into \(N\) equal-probability strata. Exactly one sample is drawn from each stratum, and the samples are randomly permuted across strata to break correlations between variables while maintaining stratified coverage.

This ensures each region of the input space is sampled at least once, providing better coverage than simple random sampling for the same number of evaluations [**[12]**](../reference/bibliography.md#ref12).


### First-Order Sobol Index

\[
S_i = \frac{V_i}{V(Y)} = \frac{V\!\left[\mathbb{E}(Y \mid X_i)\right]}{V(Y)} \tag{OTEC-9}
\]

| Symbol | Description |
|--------|-------------|
| \(S_i\) | First-order Sobol sensitivity index for parameter \(i\) |
| \(V_i\) | Variance of the conditional expectation of the output given parameter \(i\) |
| \(V(Y)\) | Total unconditional variance of the output |
| \(Y\) | Model output (e.g., LCOE) |
| \(X_i\) | Input parameter \(i\) |

The first-order index \(S_i\) measures the fraction of total output variance that is explained by parameter \(i\) alone, without interactions. The sum of all first-order indices is at most 1; the gap to 1 is attributable to interaction effects [**[11]**](../reference/bibliography.md#ref11).

The total-order index is:

\[
S_{T_i} = 1 - \frac{V_{\sim i}}{V(Y)} \tag{OTEC-10}
\]

where \(V_{\sim i} = V\!\left[\mathbb{E}(Y \mid X_{\sim i})\right]\) is the variance explained by all parameters except \(i\). The difference \(S_{T_i} - S_i\) quantifies the interaction effects involving parameter \(i\).


### Transmission Efficiency

The submarine cable transmission efficiency model used by OTEX distinguishes between AC and DC transmission based on distance:

**AC transmission** (distance ≤ 50 km):

\[
\eta_{\text{trans}} = 0.979 - 10^{-6} \cdot d^2 - 9 \times 10^{-5} \cdot d \tag{OTEC-11a}
\]

**DC transmission** (distance > 50 km):

\[
\eta_{\text{trans}} = 0.964 - 8 \times 10^{-5} \cdot d \tag{OTEC-11b}
\]

where \(d\) is the distance to shore in km. The efficiency is bounded below by 0.01 to prevent numerical issues.


---


## Scripting

The Studio's engines are GUI-independent and can be used in scripts, together with the `otex` analyses and the functions in `esfex.models.otec_models`:

```python
import numpy as np
from otex.analysis import MonteCarloAnalysis, SobolAnalysis, UncertaintyConfig

from esfex.models.otec_models import (
    compute_carnot_efficiency,
    compute_pipe_analysis,
)
from esfex.visualization.workflows.otec_studio.economics import run_on_design
from esfex.visualization.workflows.otec_studio.project import StudioConfig

T_WW = 27.5   # warm water temperature (°C)
T_CW = 5.0    # cold water temperature (°C)

# --- On-design analysis for a single site (defaults: closed Rankine, ammonia, 136 MW gross) ---
cfg = StudioConfig(cycle_type="rankine_closed", fluid_type="ammonia", cost_level="low_cost")
res = run_on_design(cfg, T_WW, T_CW, dist_shore=20.0)
plant = res["plant"]
print(f"LCOE: {np.ravel(plant['LCOE_nom'])[0]:.2f} ct/kWh")
print(f"Net power: {abs(np.ravel(plant['p_net_nom'])[0]):.0f} kW")

# --- Monte Carlo uncertainty ---
mc = MonteCarloAnalysis(
    T_WW=T_WW, T_CW=T_CW,
    config=UncertaintyConfig(n_samples=500, seed=42),
    p_gross=-136000, cost_level="low_cost",
)
stats = mc.run(show_progress=False).compute_statistics()
print(f"LCOE mean: {stats['lcoe']['lcoe_mean']:.2f} ct/kWh, "
      f"P5-P95: {stats['lcoe']['lcoe_p5']:.2f} - {stats['lcoe']['lcoe_p95']:.2f}")

# --- Sensitivity (Sobol) ---
sobol = SobolAnalysis(T_WW=T_WW, T_CW=T_CW, n_samples=256,
                      p_gross=-136000, cost_level="low_cost")
sobol_results = sobol.run(output="lcoe", show_progress=False)
for i, name in enumerate(sobol_results.parameter_names):
    print(f"  {name}: S1={sobol_results.S1[i]:.3f}, ST={sobol_results.ST[i]:.3f}")

# --- Pipe sizing ---
pipe = compute_pipe_analysis(
    depth_m=1000, dist_shore_km=20,
    gross_power_kw=136000, pipe_diameter_m=10.0,
)
print(f"Pumping: {pipe.pumping_power_kw:.0f} kW ({pipe.pumping_fraction*100:.1f}%)")
print(f"Net delivered: {pipe.net_power_after_pumping_kw:.0f} kW")

# --- Carnot efficiency ---
print(f"Carnot efficiency: {compute_carnot_efficiency(T_WW, T_CW)*100:.2f}%")
```

`esfex.models.otec_models` also provides `compute_daily_cf` and `expand_daily_to_hourly` for annual capacity-factor profiles from a daily temperature series. See the [OTEX library documentation](https://pypi.org/project/otex/) for the full OTEX API.
