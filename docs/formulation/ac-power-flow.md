# AC Optimal Power Flow

The ACOPF module implements AC optimal power flow formulations as JuMP constraints in the ESFEX operational dispatch model. It is implemented in `transmission_acopf.jl` and supports five formulations: SOC, QC and SDP relaxations, Polar NLP and Rectangular NLP. ACOPF applies to operational dispatch only; the master problem and the Benders subproblems always use the [DC formulation](dc-power-flow.md).

## Overview

The DC power flow approximation (see [DC Power Flow](dc-power-flow.md)) linearizes the AC equations by assuming flat voltages, small angle differences, and negligible resistance. While computationally efficient, it ignores voltage magnitude variations, reactive power, and apparent power (MVA) limits. The ACOPF formulations model these effects directly, providing:

- **Voltage magnitude** tracking at each bus
- **Reactive power** balance and generation limits
- **Apparent power** line limits (\(P^2 + Q^2 \leq S_{max}^2\))
- **Accurate loss** representation through full admittance modeling

The formulation is selected with the system's `power_flow_mode`:

| Mode | Formulation | Type | Variables |
|------|-------------|------|-----------|
| `acopf_soc` | SOC Relaxation | Convex (relaxed) | \(w, w^r, w^i\) |
| `acopf_qc` | QC Relaxation | Convex (relaxed, tighter) | \(w, w^r, w^i\) |
| `acopf_sdp` | SDP Relaxation | Convex (relaxed, tightest) | \(w, w^r, w^i\) + full \(W\) matrix |
| `acopf_polar` | Polar NLP | Exact (nonlinear) | \(V, \theta\) |
| `acopf_rect` | Rectangular NLP | Exact (nonlinear) | \(e, f\) |

## Formulations

### SOC Relaxation

The Second-Order Cone (SOC) relaxation [**[2]**](../reference/bibliography.md#ref2) lifts the voltage phasor products into W-space variables, replacing the nonlinear voltage-angle relationships with a convex cone constraint.

**Variables** (per branch \(l\) connecting buses \(i, j\), per hour \(t\)):

| Variable | Definition | Bounds |
|----------|-----------|--------|
| \(w_{i,t}\) | \(\|V_i\|^2\) (voltage magnitude squared) | \([V_{min}^2,\; V_{max}^2]\) |
| \(w^r_{l,t}\) | \(\text{Re}(V_i \cdot V_j^*) = \|V_i\| \|V_j\| \cos(\theta_i - \theta_j)\) | \([-V_{max}^2,\; V_{max}^2]\) |
| \(w^i_{l,t}\) | \(\text{Im}(V_i \cdot V_j^*) = \|V_i\| \|V_j\| \sin(\theta_i - \theta_j)\) | \([-V_{max}^2,\; V_{max}^2]\) |

**Linking constraint** (convex relaxation of the exact equality \(w_i \cdot w_j = (w^r)^2 + (w^i)^2\)):

\[
w_{i,t} \cdot w_{j,t} \geq (w^r_{l,t})^2 + (w^i_{l,t})^2 \quad \forall l, t
\tag{SOC-1}
\]

It is written as a rotated second-order cone, \(\left(\tfrac{1}{2} w_{i,t},\, w_{j,t},\, w^r_{l,t},\, w^i_{l,t}\right) \in \mathcal{Q}_r\) (`soc_{l}_{t}`), which conic solvers take natively. The slack bus has \(w = 1\). The SOC relaxation itself has no angle-difference limit.

The inequality relaxation makes the feasible set convex. The solution is a lower bound on the true AC-OPF cost. Power flow expressions are affine in \(w, w^r, w^i\) (see [Power Flow Expressions](#power-flow-expressions)).

### QC Relaxation

The Quadratic Convex (QC) relaxation [**[3]**](../reference/bibliography.md#ref3) starts from the SOC relaxation and adds McCormick envelopes and trigonometric bounds to tighten the feasible region.

**Additional constraints** on top of SOC:

Tighter bounds on \(w^r\) using cosine bounds (\(\cos\) is even, minimum at \(\pm\theta_{max}\)):

\[
V_{min}^2 \cdot \cos(\theta_{max}) \leq w^r_{l,t} \leq V_{max}^2
\tag{QC-1}
\]

Tighter bounds on \(w^i\) using sine bounds:

\[
|w^i_{l,t}| \leq V_{max}^2 \cdot \sin(\theta_{max})
\tag{QC-2}
\]

Convex envelope for cosine:

\[
w^r_{l,t} \geq \tfrac{1}{2}(w_{i,t} + w_{j,t}) \cdot \cos(\theta_{max})
\tag{QC-3}
\]

Angle difference limits via tangent:

\[
|w^i_{l,t}| \leq \tan(\theta_{max}) \cdot w^r_{l,t} \quad \text{(when } \theta_{max} < \pi/2\text{)}
\tag{QC-4}
\]

The QC relaxation is strictly tighter than SOC — it excludes more of the non-physical region while remaining convex. Constraint names: `qc_wr_lb/ub_{l}_{t}` (QC-1), `qc_wi_lb/ub_{l}_{t}` (QC-2), `qc_cos_env_{l}_{t}` (QC-3), `qc_angle_ub/lb_{l}_{t}` (QC-4).

### SDP Relaxation

The semidefinite relaxation keeps the per-branch SOC constraints (SOC-1) and adds, for each hour, a positive-semidefinite constraint on the full voltage-product matrix \(W = W_R + j W_I\) in its real form:

\[
\begin{bmatrix} W_R & -W_I \\ W_I & W_R \end{bmatrix} \succeq 0 \quad \forall t
\tag{SDP-1}
\]

(`psd_{t}`). The diagonal holds \(w_i\); off-diagonal entries of bus pairs joined by a branch are that branch's \(w^r, w^i\), and the other pairs get free variables constrained only by SDP-1. The matrix is \(2N \times 2N\) per hour, so the model grows quickly: a warning is logged above 30 buses. Like SOC, the SDP relaxation has no angle-difference limit.

### Polar NLP

The exact formulation using voltage magnitude \(V\) and angle \(\theta\) variables. This is the standard textbook ACOPF.

**Variables:**

| Variable | Definition | Bounds |
|----------|-----------|--------|
| \(V_{i,t}\) | Voltage magnitude at bus \(i\) | \([V_{min},\; V_{max}]\) |
| \(\theta_{i,t}\) | Voltage angle at bus \(i\) | \([-\pi,\; \pi]\) |

Power flow expressions involve trigonometric functions of angle differences (\(\cos(\theta_i - \theta_j)\), \(\sin(\theta_i - \theta_j)\)), making the problem nonlinear and nonconvex. Angle difference limits are enforced directly (`angle_ub_{l}_{t}`, `angle_lb_{l}_{t}`):

\[
|\theta_{i,t} - \theta_{j,t}| \leq \theta_{max} \quad \forall (i,j) \in \mathcal{L}, t
\tag{POLAR-1}
\]

The slack bus is fixed: \(V_{slack} = 1.0\) p.u., \(\theta_{slack} = 0\).

### Rectangular NLP

The exact formulation using real and imaginary voltage components.

**Variables:**

| Variable | Definition | Bounds |
|----------|-----------|--------|
| \(e_{i,t}\) | \(V_i \cos\theta_i\) (real component) | \([-V_{max},\; V_{max}]\) |
| \(f_{i,t}\) | \(V_i \sin\theta_i\) (imaginary component) | \([-V_{max},\; V_{max}]\) |

Voltage magnitude bounds become quadratic:

\[
V_{min}^2 \leq e_{i,t}^2 + f_{i,t}^2 \leq V_{max}^2 \quad \forall i \neq \text{slack}, t
\tag{RECT-1}
\]

(`vm_lb_{i}_{t}`, `vm_ub_{i}_{t}`). Angle difference limits are expressed via cross and dot products of the voltage components (`angle_ub_rect_{l}_{t}`, `angle_lb_rect_{l}_{t}`):

\[
|f_i e_j - e_i f_j| \leq \tan(\theta_{max}) \cdot (e_i e_j + f_i f_j) \quad \forall (i,j) \in \mathcal{L}, t
\tag{RECT-2}
\]

The slack bus is fixed: \(e_{slack} = 1.0\), \(f_{slack} = 0.0\).

## Network Model

### Branch Admittance

Each branch (transmission line or transformer) is modeled using the 4-terminal admittance representation. The complex current injections are:

\[
I_{from} = Y_{ff} \cdot V_i + Y_{ft} \cdot V_j, \qquad
I_{to} = Y_{tf} \cdot V_i + Y_{tt} \cdot V_j
\]

where \(V_i\) and \(V_j\) are the complex bus voltages. The admittance elements for a branch with series impedance \(z_s = r + jx\) (giving series admittance \(y_s = g_s + jb_s = 1/z_s\)), shunt susceptance \(b_{sh}\), and tap ratio \(\tau\) are:

| Element | Formula |
|---------|---------|
| \(Y_{ff}\) | \((g_s + jb_s)/\tau^2 + jb_{sh}/(2\tau^2)\) |
| \(Y_{ft}\) | \(-(g_s + jb_s)/\tau\) |
| \(Y_{tt}\) | \(g_s + jb_s + jb_{sh}/2\) |
| \(Y_{tf}\) | \(-(g_s + jb_s)/\tau\) |

For transmission lines, \(\tau = 1\); \(r\), \(x\), \(b_{sh}\) and the capacity `capacity_mw` (MW taken as MVA) are those of the whole line, all its circuits together, and are not rescaled by `num_circuits`. For transformers, \(\tau\) is the tap ratio and there is no shunt term.

The real and imaginary parts of each admittance element (\(g_{ff}, b_{ff}, g_{ft}, b_{ft}\), etc.) are precomputed in the `ACOPFBranch` struct and used directly in the power flow expressions.

### Reactance Clamping

Very short lines or bus-ties can have near-zero reactance, producing extreme admittances that cause numerical issues. Transmission line reactances are clamped to a minimum value:

\[
|x| \geq x_{min} \quad (\text{default: } 0.01 \text{ p.u.})
\]

This limits the maximum admittance magnitude to \(|b| \leq 1/x_{min} = 100\) p.u. per line. Configured via `min_reactance_pu`. Transformers are not clamped; a transformer with zero reactance uses \(0.99 \times\) `impedance_pu`.

### Transformer Tap Ratio Normalization

Transformer tap ratios that represent voltage level changes (e.g., 480V/34.5kV = 0.014) create enormous admittances in a single-base per-unit system. Tap ratios outside the range \([\tau_{min}, \tau_{max}]\) are reset to 1.0, treating the transformer as a simple series impedance:

\[
\tau = \begin{cases}
\tau & \text{if } \tau_{min} \leq \tau \leq \tau_{max} \\
1.0 & \text{otherwise}
\end{cases}
\]

Default bounds: \(\tau_{min} = 0.5\), \(\tau_{max} = 2.0\). Configured via `tap_ratio_min` and `tap_ratio_max`.

## Power Flow Expressions

The active and reactive power flows on each branch are expressed differently depending on the formulation, but all use the same precomputed admittance coefficients.

### W-Space (SOC / QC / SDP)

Power flow expressions are **affine** in the W-space variables — no trigonometric or quadratic terms:

\[
P_{from} = g_{ff} \cdot w_i + g_{ft} \cdot w^r + b_{ft} \cdot w^i
\tag{PF-W-P}
\]

\[
Q_{from} = -b_{ff} \cdot w_i - b_{ft} \cdot w^r + g_{ft} \cdot w^i
\tag{PF-W-Q}
\]

\[
P_{to} = g_{tt} \cdot w_j + g_{tf} \cdot w^r - b_{tf} \cdot w^i
\]

\[
Q_{to} = -b_{tt} \cdot w_j - b_{tf} \cdot w^r - g_{tf} \cdot w^i
\]

### Polar (V, theta)

Power flow expressions involve **trigonometric** nonlinearities:

\[
P_{from} = g_{ff} \cdot V_i^2 + V_i V_j \left( g_{ft} \cos\theta_{ij} + b_{ft} \sin\theta_{ij} \right)
\tag{PF-P-P}
\]

\[
Q_{from} = -b_{ff} \cdot V_i^2 + V_i V_j \left( -b_{ft} \cos\theta_{ij} + g_{ft} \sin\theta_{ij} \right)
\tag{PF-P-Q}
\]

where \(\theta_{ij} = \theta_i - \theta_j\). The to-side expressions follow the same pattern with \(g_{tt}, b_{tt}, g_{tf}, b_{tf}\) and reversed sine sign.

### Rectangular (e, f)

Power flow expressions are **quadratic** in the voltage components. The W-space products are computed from \(e, f\):

\[
w_{ii} = e_i^2 + f_i^2, \qquad w^r_{ij} = e_i e_j + f_i f_j, \qquad w^i_{ij} = f_i e_j - e_i f_j
\]

These are substituted into the same admittance expressions as the W-space formulation:

\[
P_{from} = g_{ff}(e_i^2 + f_i^2) + g_{ft}(e_i e_j + f_i f_j) + b_{ft}(f_i e_j - e_i f_j)
\tag{PF-R-P}
\]

\[
Q_{from} = -b_{ff}(e_i^2 + f_i^2) - b_{ft}(e_i e_j + f_i f_j) + g_{ft}(f_i e_j - e_i f_j)
\tag{PF-R-Q}
\]

## Constraints

### AC-1: Active Power Balance (KCL)

Active power balance at each bus \(n\) and hour \(t\). The injection terms are identical to the DC KCL (see [DC-1](dc-power-flow.md#dc-1-kcl-kirchhoffs-current-law)), but the flow side uses formulation-specific power flow expressions:

\[
\underbrace{\sum_{g} P_{g,n,t} + \sum_{b} P^{dis}_{b,n,t} + L_{n,t} + \ldots}_{\text{injection (MW)}} - \underbrace{D_{n,t} \cdot \phi_n + \sum_{b} P^{ch}_{b,n,t} + \ldots}_{\text{withdrawal (MW)}} = S_{base} \cdot \sum_{l \in \mathcal{B}_n} P_{flow,l,t}
\tag{AC-1}
\]

where \(S_{base}\) is the system base power (MVA) and \(P_{flow,l,t}\) is the per-unit active power flow from bus \(n\) on branch \(l\), computed via the formulation-specific expressions. Both sides are in MW, keeping coefficient ratios manageable for the solver.

!!! note "Injection Terms"
    The active KCL includes the same injection terms as the DC formulation: generator output, battery discharge/charge, EV V2G/charging, electrolyzer consumption, AC/DC and frequency converter flows, reservoir pumping, rooftop solar generation/curtailment and load shedding. Reserves are free capacity, not energy, and are not in the KCL.

Constraint name: `kcl_p_{n}_{t}`. Implemented in `add_acopf_power_balance!()`.

As in the DC model, load shedding is bounded by the demand on its own bus, \(LS_{n,t} \leq \max(D_{n,t} \cdot \phi_n, 0)\), with the bound at 0 on connection buses (`max_load_shed_acopf_b{n}_t{t}`), and rooftop curtailment by the available rooftop generation of the node (`rooftop_curt_ub_n{i}_{t}`).

### AC-2: Reactive Power Balance (KCL)

Reactive power balance at each bus, using estimated reactive load and reactive generation variables:

\[
\sum_{g} Q_{g,n,t} + Q^{slack+}_{n,t} - Q^{slack-}_{n,t} - Q^{load}_{n,t} = S_{base} \cdot \sum_{l \in \mathcal{B}_n} Q_{flow,l,t}
\tag{AC-2}
\]

where the reactive load is estimated from the active load using the load power factor:

\[
Q^{load}_{n,t} = D_{n,t} \cdot \phi_n \cdot \tan\!\left(\arccos(pf_{load})\right)
\]

(`kcl_q_{n}_{t}`). The slack variables \(Q^{slack+}\) and \(Q^{slack-}\) absorb reactive power mismatch with a penalty cost \(c_Q\) ($/MVAr) added to the objective. This is necessary because in SOC/QC relaxations, the relaxed voltage products may not correspond to a physically realizable voltage profile, making exact reactive balance infeasible.

### AC-3: Voltage Magnitude Bounds

For SOC/QC/SDP formulations, voltage bounds are applied to the squared magnitude variable:

\[
V_{min}^2 \leq w_{i,t} \leq V_{max}^2 \quad \forall i, t
\tag{AC-3a}
\]

For Polar NLP:

\[
V_{min} \leq V_{i,t} \leq V_{max} \quad \forall i, t
\tag{AC-3b}
\]

For Rectangular NLP:

\[
V_{min}^2 \leq e_{i,t}^2 + f_{i,t}^2 \leq V_{max}^2 \quad \forall i \neq \text{slack}, t
\tag{AC-3c}
\]

The slack bus voltage is fixed to 1.0 p.u. in all formulations.

### AC-4: W-Space Voltage Linking

The SOC constraint links the W-space diagonal and off-diagonal variables (see [SOC-1](#soc-relaxation)). The QC formulation adds tighter bounds via McCormick envelopes and trigonometric inequalities (see [QC-1 through QC-4](#qc-relaxation)), and the SDP formulation adds the PSD constraint (see [SDP-1](#sdp-relaxation)).

### AC-5: Angle Difference Limits

Voltage angle differences across each branch are bounded. The implementation depends on the formulation:

- **SOC and SDP**: no angle-difference limit.
- **QC**: trigonometric bounds on \(w^r\) and \(w^i\) (QC-4).
- **Polar NLP**: Direct constraint on angle variables (POLAR-1).
- **Rectangular NLP**: Nonlinear constraint via cross/dot products (RECT-2).

\(\theta_{max}\) is `dc_power_flow.max_angle_diff_deg` (default 30° = 0.524 rad). The DC formulation does not use it.

### AC-6: Apparent Power Line Limits

Each branch has an apparent power (MVA) limit enforced on both ends:

\[
P_{from,l,t}^2 + Q_{from,l,t}^2 \leq \left(\frac{S^{max}_l}{S_{base}}\right)^2 \quad \forall l, t
\tag{AC-6a}
\]

\[
P_{to,l,t}^2 + Q_{to,l,t}^2 \leq \left(\frac{S^{max}_l}{S_{base}}\right)^2 \quad \forall l, t
\tag{AC-6b}
\]

where \(S^{max}_l\) is the branch thermal capacity in MVA. Unlike the DC formulation which limits only active power flow (\(|f_l| \leq \bar{F}_l\)), the ACOPF limits the full apparent power magnitude. The form depends on the formulation:

- **SOC, QC, SDP**: a second-order cone, \(\left(S^{max}_l/S_{base},\, P,\, Q\right) \in \mathcal{Q}\).
- **Polar, Rectangular**: the quadratic inequality \(P^2 + Q^2 \leq (S^{max}_l/S_{base})^2\), since Ipopt does not take cone constraints on nonlinear expressions.

Constraint names `sline_from_{l}_{t}` and `sline_to_{l}_{t}`. A branch with capacity \(\leq 0\) has no limit. \(S^{max}_l\) is multiplied by the scheduled-outage factor of the line or transformer. Lines the master built are part of \(S^{max}_l\) in the following years' dispatch. N-1 transmission constraints act on DC flow variables and are therefore skipped in ACOPF.

Implemented in `add_acopf_line_limits!()`.

### AC-7: Reactive Generation Limits

Each generator's reactive power output is bounded:

\[
Q^{min}_{g,n} \leq Q_{g,n,t} \leq Q^{max}_{g,n} \quad \forall g, n, t
\tag{AC-7}
\]

Explicit limits come from the generator's `q_max_mvar` and `q_min_mvar` (per node). When they are not provided, they are estimated from the rated active power and default power factor:

\[
Q^{max}_{g,n} = P^{rated}_{g,n} \cdot \tan\!\left(\arccos(pf)\right)
\]

\[
Q^{min}_{g,n} = -\rho_Q \cdot Q^{max}_{g,n}
\]

where \(pf\) is `default_power_factor` (default: 0.85) and \(\rho_Q\) is `q_min_ratio` (default: 0.5). This allows generators to absorb reactive power up to 50% of their maximum reactive output.

### AC-8: Reactive Power Slack

Reactive slack variables penalize Q imbalance in the objective:

\[
\min \ldots + c_Q \sum_{n,t} \left( Q^{slack+}_{n,t} + Q^{slack-}_{n,t} \right)
\tag{AC-8}
\]

where \(c_Q\) is the `q_slack_penalty` (default: 100 $/MVAr). The penalty is small enough to avoid distorting the active power dispatch, but large enough to keep Q close to balanced.

## Solver Requirements

The relaxations are conic problems; the exact formulations are nonlinear programs. The solvers each formulation accepts (`FORMULATION_SOLVERS` in `config/solver.py`, used by the Studio to filter the solver list) are:

| Formulation | Problem type | Solvers |
|-------------|--------------|---------|
| SOC (`acopf_soc`) | SOCP (rotated and standard cones) | `clarabel`, `scs`, `gurobi`, `cplex`, `xpress` |
| QC (`acopf_qc`) | SOCP + linear | same as SOC |
| SDP (`acopf_sdp`) | SDP (PSD cone) | `scs`, `clarabel` |
| Polar (`acopf_polar`) | NLP (trigonometric), nonconvex | `ipopt` |
| Rectangular (`acopf_rect`) | NLP (polynomial), nonconvex | `ipopt` |

!!! note "Conic solvers"
    HiGHS solves LP, MIP and QP but not second-order cone programs, so it cannot solve SOC or QC (JuMP reports that `RotatedSecondOrderCone` constraints are not supported); use Clarabel, SCS or a commercial conic solver. Real networks can produce wide coefficient ranges, which first-order (SCS) and interior-point (Clarabel) conic solvers may handle poorly; if one reports an infeasible or unbounded status, try another.

The solver is set in the top-level `solver` section (see [Configuration](#configuration)).

## Numerical Considerations

### KCL Scaling

The power balance constraints are written in MW (not per-unit):

\[
\text{injection}_{MW} = S_{base} \times \text{flow}_{pu}
\]

This keeps both sides of the KCL equation in comparable magnitude ranges (MW on the left, \(S_{base} \times\) small p.u. values on the right). The alternative — dividing injection by \(S_{base}\) to get p.u. — would introduce a factor of \(1/S_{base} = 0.01\) that creates 260,000:1 coefficient ratios when combined with summed admittances.

### Flat Voltage Start Values

All formulations initialize voltage variables to the flat start (\(V = 1.0\) p.u., \(\theta = 0\)):

| Formulation | Start Values |
|-------------|-------------|
| SOC/QC | \(w = 1.0\), \(w^r = 1.0\), \(w^i = 0.0\) |
| SDP | none (slack bus fixed at \(w = 1\)) |
| Polar | \(V = 1.0\), \(\theta = 0.0\) |
| Rectangular | \(e = 1.0\), \(f = 0.0\) |

For NLP formulations, these start values are critical for convergence. Without them, Ipopt often starts from \((0, 0)\), which violates the voltage magnitude lower bound and leads to `LOCALLY_INFEASIBLE` status.

### Tap Ratio Normalization

See [Transformer Tap Ratio Normalization](#transformer-tap-ratio-normalization).

### Reactance Clamping

See [Reactance Clamping](#reactance-clamping).

## Configuration

ACOPF parameters are configured in the `ac_power_flow` section of each system, defined by `ACPowerFlowConfig`:

```yaml
ac_power_flow:
  base_mva: 100.0
  voltage_min_pu: 0.90
  voltage_max_pu: 1.10
  default_power_factor: 0.85
  load_power_factor: 0.9
  q_slack_penalty: 100.0
  min_reactance_pu: 0.01
  tap_ratio_min: 0.5
  tap_ratio_max: 2.0
  q_min_ratio: 0.5
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `base_mva` | float | 100.0 | System base power for per-unit conversion (also \(S_{base}\) of the DC power flow and losses) |
| `voltage_min_pu` | float | 0.90 | Lower voltage magnitude limit (p.u.) |
| `voltage_max_pu` | float | 1.10 | Upper voltage magnitude limit (p.u.) |
| `default_power_factor` | float | 0.85 | Generator power factor for Q limit estimation |
| `load_power_factor` | float | 0.9 | Load power factor for reactive demand estimation |
| `q_slack_penalty` | float | 100.0 | Penalty for reactive imbalance ($/MVAr) |
| `min_reactance_pu` | float | 0.01 | Minimum branch reactance (p.u.) for clamping |
| `tap_ratio_min` | float | 0.5 | Lower bound for valid transformer tap ratios |
| `tap_ratio_max` | float | 2.0 | Upper bound for valid transformer tap ratios |
| `q_min_ratio` | float | 0.5 | Ratio for Q_min = -ratio x Q_max when Q limits not specified |

The power flow mode is set per system, the solver at the top level, and the angle limit in the system's `dc_power_flow` section:

```yaml
solver:
  name: clarabel                # ipopt for acopf_polar / acopf_rect
systems:
  my_system:
    power_flow_mode: acopf_soc  # or acopf_qc, acopf_sdp, acopf_polar, acopf_rect
    dc_power_flow:
      max_angle_diff_deg: 30.0  # used by QC, Polar and Rectangular
```

## GUI

The power flow mode and AC parameters can be configured from the System Settings panel under "Power Flow". Selecting any `acopf_*` mode reveals the AC-specific fields (base MVA, voltage limits, power factor, Q penalty, reactance clamping, tap ratio bounds).

## Implementation

The ACOPF module is organized around Julia's multiple dispatch pattern:

| Function | Purpose |
|----------|---------|
| `setup_acopf!()` | Main entry point — orchestrates variable creation, constraints, and line limits |
| `ACOPFNetwork()` | Builds precomputed branch admittance data from network configuration |
| `build_acopf_variables!()` | Creates formulation-specific voltage and reactive generation variables |
| `add_acopf_voltage_constraints!()` | Adds SOC cones, QC bounds, the SDP PSD constraint, angle limits (dispatched by formulation) |
| `add_acopf_power_balance!()` | Adds active and reactive KCL at each bus |
| `add_acopf_line_limits!()` | Adds apparent power limits on branches |
| `extract_acopf_voltages()` | Extracts voltage magnitude and angle from the solved model |
| `extract_acopf_reactive_gen()` | Extracts reactive generation from the solved model |

All functions dispatch on the `ACOPFFormulation` abstract type (`SOCFormulation`, `QCFormulation`, `SDPFormulation`, `PolarNLPFormulation`, `RectNLPFormulation`), allowing formulation-specific behavior without conditional branching.

## References

The ACOPF formulations implemented in ESFEX draw from the following foundational works. For a historical survey of OPF formulations, see Cain et al. [**[56]**](../reference/bibliography.md#ref56). The SOC relaxation follows Jabr [**[2]**](../reference/bibliography.md#ref2); the QC relaxation follows Coffrin et al. [**[3]**](../reference/bibliography.md#ref3); the SDP formulation follows Bai et al. [**[57]**](../reference/bibliography.md#ref57). For a comprehensive theoretical treatment of convex relaxations applied to OPF, see Low [**[27]**](../reference/bibliography.md#ref27) and the survey by Molzahn and Hiskens [**[58]**](../reference/bibliography.md#ref58). The NLP formulations (Polar and Rectangular) solve the exact AC power flow equations using the Ipopt interior-point solver [**[4]**](../reference/bibliography.md#ref4).

See the [full bibliography](../reference/bibliography.md) for complete citation details.
