# DC Power Flow

The DC power flow model implements network-constrained power flow with an angle-based (B-θ) DC-OPF: each line's flow is tied to the voltage angles of its two buses. It is implemented in `transmission_dc.jl` and used whenever `power_flow_mode` is `dcopf` (the default) or `dcopf_ac_verify` and the system has more than one bus. The master problem and the Benders subproblems always use this formulation, whatever `power_flow_mode` says.

## Overview

The DC power flow approximation linearizes the AC power flow equations under the assumptions that:

- Voltage magnitudes are close to 1 p.u.
- Voltage angle differences across lines are small
- Line resistance is negligible compared to reactance

This yields a linear model where power flows are determined by voltage angle differences and line reactances.

## Bus vs. Node

ESFEX distinguishes between **buses** (electrical abstraction) and **nodes** (geographic locations):

| Concept | Description | Example |
|---------|-------------|---------|
| **Node** | Geographic location with coordinates | City, substation site |
| **Bus** | Electrical point in the network | HV bus, LV bus at same substation |

Each bus has a `parent_node` and carries a `demand_fraction` \(\alpha_n\) of its parent node's demand. Multiple buses can map to the same node (e.g., different voltage levels at the same substation). Buses with `role: connection` carry no demand: their \(\alpha_n\) is treated as 0, so node-level demand-side terms (demand, load shedding, EV, rooftop) do not appear at them.

## Incidence Matrix

The bus-line incidence matrix \(\mathbf{K} \in \mathbb{R}^{N \times L}\) encodes the network topology:

\[
K_{n,l} = \begin{cases}
+1 & \text{if line } l \text{ leaves bus } n \\
-1 & \text{if line } l \text{ enters bus } n \\
0 & \text{otherwise}
\end{cases}
\tag{DC-K}
\]

Built by `build_incidence_matrix()`. Every branch is oriented from its lower-index bus to its higher-index bus, so a positive flow runs from the lower to the higher bus index. Parallel lines between the same bus pair are separate columns. Transformers are appended as extra branches (see [Transformer Modeling](#transformer-modeling)).

## Constraints

### DC-1: KCL (Kirchhoff's Current Law)

Power balance at each bus \(n\) and time \(t\):

\[
\underbrace{\sum_{g} p_{g,n,t} + \sum_{b} p^{dis}_{b,n,t} + \alpha_n p^{v2g}_{i,t} + LS_{n,t} + \alpha_n P^{roof}_{i,t} + P^{conv}_{n,t}}_{\text{injection}}
- \underbrace{\left(\alpha_n D_{i,t} + p^{elz}_{n,t} + \sum_{b} p^{ch}_{b,n,t} + p^{pump}_{n,t} + \alpha_n p^{ev}_{i,t} + \alpha_n RC_{i,t}\right)}_{\text{withdrawal}}
= \sum_{l} K_{n,l} \, f_{l,t} + \sum_{l} \tfrac{1}{2} |K_{n,l}| \, P^{loss}_{l,t}
\tag{DC-1}
\]

where \(i\) is the parent node of bus \(n\), \(D_{i,t}\) the node demand, \(P^{conv}\) the net AC/DC and frequency converter injection (CONV-2), \(p^{pump}\) reservoir pumping and \(RC\) rooftop curtailment. The loss term is that of the PWL or linear loss model (LOSS-5, see [Transmission Losses](#transmission-losses)); with `loss_model: none` it is absent.

Two terms are deliberately left out of the balance:

- **Flexible demand curtailment** is not in the KCL. It enters only the objective (as a benefit) and the sectoral constraints; in the balance it would let the model remove demand at negative cost.
- **Reserves** are free capacity, not energy (see [Operational Dispatch](operational-dispatch.md)).

In the master problem the same KCL also carries the candidate technologies' output, the battery technologies' net discharge and the sectoral loss of load, passed through `extra_injections_fn`.

Implemented in `add_dc_constraints!()`.

#### DC-1b: Load Shedding Bound

Load shedding sits on the supply side of DC-1, so it is bounded by the demand on its own bus:

\[
LS_{n,t} \leq \max(\alpha_n D_{i,t},\, 0) \quad \forall n, t
\tag{DC-1b}
\]

Without the bound, `load_shed` would act as a virtual generator: shed more than a bus consumes and export the surplus. On connection buses the bound is 0. Constraint name: `max_load_shed_dc_b{n}_t{t}`. The single-bus balance and the ACOPF formulations apply the same bound.

### DC-2: Flow-Angle Coupling

Each line's flow follows the angle difference across it:

\[
f_{l,t} = \frac{S_{base}}{x_l} \left( \theta_{i,t} - \theta_{j,t} \right) \quad \forall l = (i,j),\; t
\tag{DC-2}
\]

where \(x_l\) is the line reactance in p.u. and \(S_{base}\) converts the per-unit susceptance to MW per radian. \(S_{base}\) is the system power base `ac_power_flow.base_mva` (default 100 MVA), the base the Grid Builder uses for line per-unit values. Constraint name: `flow_angle_{l}_{t}`.

- **Parallel circuits**: `capacity_mw`, `reactance_pu` and `resistance_pu` describe the whole line, all its `num_circuits` circuits together; the model uses them as given and does not rescale them by the circuit count. A line of two 300 MW circuits of 0.1 p.u. each is written `capacity_mw: 600`, `reactance_pu: 0.05`, `num_circuits: 2` (this is what the Grid Builder writes).
- **Missing reactance**: a line without `reactance_pu` gets `length_km × reactance_per_km / Z_base / num_circuits` (the per-km value is that of one circuit), with \(Z_{base} = V^2 / S_{base}\) and \(V\) the line's `voltage_kv` (else `dc_power_flow.voltage_level_kv`); 0.01 p.u. / `num_circuits` when it has no length. A reactance \(\leq 0\) is replaced by 0.01 p.u.
- **Negligible lines**: a branch with capacity below 0.1 MW has its flow fixed to zero (`flow_zero_{l}_{t}`) unless investment can expand it: a candidate corridor of 0 MW in the master problem, such as a development-zone link, or a corridor whose ends both allow transference investment in a development-mode dispatch.

DC-2 replaced an earlier cycle-based KVL formulation; one equation per line handles parallel lines and disconnected networks directly.

### DC-3: Line Capacity

Each branch has a thermal limit:

\[
-\bar{F}_{l} \cdot o_{l,t} \leq f_{l,t} \leq \bar{F}_{l} \cdot o_{l,t} \quad \forall l, t
\tag{DC-3}
\]

where \(o_{l,t} \in [0, 1]\) is the deterministic scheduled-outage factor of the line or transformer (1 when no outage is scheduled; see [Scheduled Interruptions](../user-guide/scheduled-interruptions.md)) and \(\bar{F}_l\) is the transfer capacity, which may include investment:

\[
\bar{F}_l = F^{base}_l + I^{tr}_{l}
\]

In the master problem \(I^{tr}_l\) is the cumulative investment on the line's corridor; in operational dispatch in development mode it is a per-corridor variable `tf_inv_{i}_{j}`, with `transfer_symmetry_{i}_{j}` forcing equal investment in both directions. The capacity constraints are unnamed.

Implemented in `add_line_capacity_constraints!()`.

### DC-4: Voltage Angle Reference

One bus per connected component of the network is the angle reference:

\[
\theta_{n^{ref}_c,t} = 0 \quad \forall c, t
\tag{DC-4}
\]

The component that contains `dc_power_flow.slack_bus` (0-indexed, default 0) uses that bus; every other component, including an isolated bus, uses its first bus. All angles are bounded to \([-\pi, \pi]\).

!!! note "No angle-difference limits in the DC model"
    The DC formulation has no limit on \(|\theta_i - \theta_j|\). With \(f = (S_{base}/x)\,\Delta\theta\), such a limit caps the flow at \((S_{base}/x)\,\theta^{max}\), which for many lines is far below the thermal rating. Flows are limited by DC-3 only. `dc_power_flow.max_angle_diff_deg` is used by the ACOPF formulations (see [AC Optimal Power Flow](ac-power-flow.md)).

## Converter Constraints

### AC/DC Converters

AC/DC converters (VSC or LCC type) carry power between an AC bus (`from`) and a DC bus (`to`) with directional efficiency. Each converter \(c\) has a rectifying flow \(p^{rect}_{c,t} \geq 0\) (AC→DC, `acdc_rectify`) and an inverting flow \(p^{inv}_{c,t} \geq 0\) (DC→AC, `acdc_invert`):

\[
p^{rect}_{c,t} + p^{inv}_{c,t} \leq S^{conv}_c \cdot o_{c,t}
\tag{CONV-1}
\]

\[
P^{conv}_{\text{AC bus},t} = -p^{rect}_{c,t} + \eta^{inv}_c \, p^{inv}_{c,t}, \qquad
P^{conv}_{\text{DC bus},t} = \eta^{rect}_c \, p^{rect}_{c,t} - p^{inv}_{c,t}
\tag{CONV-2}
\]

where \(S^{conv}_c\) is `rated_power_mva` and \(o_{c,t}\) the scheduled-outage factor. Converter capacity is not an investment variable of the dispatch (the master problem can build it).

### Frequency Converters

Frequency converters connect networks of different frequencies (e.g., 50 Hz to 60 Hz) in the same way, with flows `freq_a_to_b` and `freq_b_to_a`, efficiencies per direction, and

\[
p^{a \to b}_{c,t} + p^{b \to a}_{c,t} \leq S^{conv}_c \cdot o_{c,t}
\tag{FREQ-1}
\]

Both converter types add `variable_cost` × throughput to the objective.

**Minimum power.** When the dispatch runs unit commitment (`unit_commitment` mode, or `use_uc_in_dispatch`), a converter with `min_power_mva` \(\underline{S}_c > 0\) is either off or carries at least \(\underline{S}_c\) in one direction. Binaries \(u^{f}_{c,t}, u^{b}_{c,t} \in \{0, 1\}\) per direction (`acdc_on_rectify`/`acdc_on_invert`, `freq_on_a_to_b`/`freq_on_b_to_a`) give

\[
u^{f}_{c,t} + u^{b}_{c,t} \leq 1, \qquad
\underline{S}_c \, u^{f}_{c,t} \leq p^{f}_{c,t} \leq S^{conv}_c \, o_{c,t} \, u^{f}_{c,t}, \qquad
\underline{S}_c \, u^{b}_{c,t} \leq p^{b}_{c,t} \leq S^{conv}_c \, o_{c,t} \, u^{b}_{c,t}
\tag{CONV-3}
\]

with \(p^{f}, p^{b}\) the two flows (rectify/invert, A→B/B→A). For the energy prices the binaries are fixed at their optimum with the generators' commitment and the LP re-solved. Economic dispatch and the master problem (LP) ignore `min_power_mva`. Implemented in `add_converter_constraints!()`.

## Transformer Modeling

Transformers are modeled as additional branches appended to the line set:

- **Resistance**: `resistance_pu`, or `losses_fraction × impedance_pu` when not given
- **Reactance**: \(x = \sqrt{z^2 - r^2}\) from `impedance_pu` and the resistance
- **Capacity**: thermal limit from `rated_power_mva`
- **Tap ratio**: not modeled in the DC approximation

Self-loop transformers (both ends at the same bus) are skipped.

## Transmission Losses

ESFEX supports three transmission loss models, selected via `dc_power_flow.loss_model`:

| Mode | Key | Description |
|------|-----|-------------|
| Lossless | `"none"` | No transmission losses |
| Linear | `"linear"` | A constant fraction \(\lambda_l\) of the flow is lost |
| Piecewise linear | `"pwl"` | PWL approximation of quadratic \(I^2R\) losses (default) |

The loss coefficient of a branch is its per-unit series resistance \(R_l\): `resistance_pu` for lines (0 when not given, i.e. lossless) and the transformer resistance above for transformers.

The master problem's representative days and the Benders subproblems use the same `loss_model`, with `pwl_loss_segments_master` segments for `pwl`. Their network has one link per connected node pair (from `nodes_connections`). The link's reactance and resistance are those of the bus-level lines joining the two nodes in parallel, the resistance weighted by how the flow splits between them:

\[
X^{link}_{ij} = \Big(\sum_k 1/X_k\Big)^{-1}, \qquad R^{link}_{ij} = \sum_{k} R_k \, s_k^2, \qquad s_k = \frac{1/X_k}{\sum_{m} 1/X_m}
\]

with \(R_k\), \(X_k\) those of each whole line. A node pair with no bus-level lines between them gets a lossless link whose reactance is derived from its `fuel_transport_distances` entry and `voltage_level_kv` (0.01 p.u. without a distance).

### Linear Model

The linear model is the PWL model below with one segment of slope \(\lambda_l = R_l\) (clamped to \([0, 1]\)):

\[
P^{loss}_{l,t} = \lambda_l \left( f^{+}_{l,t} + f^{-}_{l,t} \right)
\]

so a fraction \(\lambda_l\) of the flow is lost whichever way it runs, half at each end (LOSS-5). Unlike the quadratic model, the loss fraction does not grow with the flow.

### Physical Model

Real power losses on a branch follow the quadratic relationship:

\[
P^{loss}_l(f) = R_l \cdot \frac{f_l^2}{S_{base}} \tag{LOSS-PHYS}
\]

with \(f_l\) in MW and \(R_l\) in p.u. The coefficient is clamped at \(R_l \leq 50\) as a guard against bad data. Direct inclusion of the quadratic term would make the problem a QP. Instead, a piecewise linear (PWL) approximation keeps it an LP.

### PWL Approximation

The quadratic loss curve is approximated by \(N\) linear segments (`pwl_loss_segments`). The flow range \([0, F^{base}_l]\) is divided into \(N\) equal segments with breakpoints:

\[
f_k = k \cdot \Delta f, \qquad \Delta f = \frac{F^{base}_l}{N}, \qquad k = 0, 1, \ldots, N
\]

The slope of the \(k\)-th segment (\(k = 1, \ldots, N\)) is the secant of the quadratic between breakpoints \(f_{k-1}\) and \(f_k\):

\[
m_k = \frac{R_l \cdot (2k - 1) \cdot \Delta f}{S_{base}} \tag{LOSS-SLOPE}
\]

Since \(m_1 < m_2 < \cdots < m_N\), the PWL approximation is **convex**: while energy has a positive value, a cost-minimizing LP fills lower-slope segments first and runs one direction only, so no binary variables are needed. When energy is worth less than nothing at some bus (surplus renewables whose curtailment has a cost, a binding curtailment limit or RE target), it does not: see [Exact losses](#exact-losses). Branches with \(R_l = 0\), or with a capacity below 0.1 MW that investment cannot expand, are lossless (`ploss_{l}_{t}` fixed to 0). A PWL branch with zero base capacity is lossless too, since its segments have zero width; with the linear model it loses \(\lambda_l\) of its flow like any other branch.

### Flow Decomposition

Because losses depend on the magnitude of flow (not its direction), each line flow is decomposed into positive and negative components (`fpos_{l}_{t}`, `fneg_{l}_{t}`):

\[
f_{l,t} = f^{+}_{l,t} - f^{-}_{l,t}, \qquad f^{+}_{l,t}, f^{-}_{l,t} \geq 0 \tag{LOSS-1}
\]

Each direction is further decomposed into \(N\) segment variables (`dp_{l}_{k}_{t}`, `dn_{l}_{k}_{t}`):

\[
f^{+}_{l,t} = \sum_{k=1}^{N} \delta^{+}_{l,k,t}, \qquad f^{-}_{l,t} = \sum_{k=1}^{N} \delta^{-}_{l,k,t} \tag{LOSS-2}
\]

with segment width bounds:

\[
0 \leq \delta^{+}_{l,k,t} \leq \Delta f, \qquad 0 \leq \delta^{-}_{l,k,t} \leq \Delta f \tag{LOSS-3}
\]

The segments span the base capacity \(F^{base}_l\). On a line that investment can expand (master problem and Benders), the last segment has no upper bound, so flow can use the built capacity at the last slope \(m_N\).

### PWL Loss Computation

The total loss on each line is the sum of segment contributions from both directions:

\[
P^{loss}_{l,t} = \sum_{k=1}^{N} m_k \cdot \left( \delta^{+}_{l,k,t} + \delta^{-}_{l,k,t} \right) \tag{LOSS-4}
\]

### Half-Loss Split KCL

Losses are allocated equally between the two endpoint buses of each line. The flow side of DC-1 becomes:

\[
\text{net\_inj}_{n,t} = \sum_{l} \left( K_{n,l} \cdot f_{l,t} + \frac{1}{2} \cdot |K_{n,l}| \cdot P^{loss}_{l,t} \right) \tag{LOSS-5}
\]

The \(+\) sign makes the loss a withdrawal: each end supplies its share of the flow plus half of the line's loss. \(|K_{n,l}|\) charges both endpoint buses regardless of flow direction.

### Exact Losses

LOSS-1 to LOSS-4 allow \(\delta^{+}\) and \(\delta^{-}\) to be positive together, and a steep segment to fill before a flat one. Both raise \(P^{loss}\) above the PWL loss of the net flow \(|f_{l,t}|\), so the LP can discard energy on any lossy line, up to twice its full-flow loss, at no cost. It does so whenever discarding energy pays: curtailment has a cost (`curtailment_cost`), a curtailment limit binds, or an RE target counts renewable output that is then lost. Surplus renewable energy then disappears as "losses" instead of being curtailed. No LP formulation can forbid this, because the set \(\{P^{loss} = \text{PWL}(|f|)\}\) is not convex.

Every solve of a model with PWL or linear losses (the dispatch, the master problem, the Benders subproblems, MGA) is therefore repaired:

1. Solve with LOSS-1 to LOSS-4.
2. At each step where some branch loses more than \(\text{PWL}(|f_{l,t}|)\) (beyond 0.001 MW), every lossy branch of that step is linearised: its flow is held in the direction it had (\(\delta^{-} = 0\) or \(\delta^{+} = 0\)), and its loss becomes \(P^{loss}_{l,t} = \alpha_{l,t} \, |f_{l,t}|\), with \(\alpha_{l,t} = \text{PWL}(|f^{*}|)/|f^{*}|\) the PWL secant at the flow \(f^{*}\) just found (\(m_1\) at zero flow). The flow runs on the first segment of its direction (the others are fixed at 0: at the same \(\alpha\) they would only be copies of it). A loss that is linear in the flow cannot be inflated.
3. Solve again. \(\alpha\) follows the new flow until each linearised loss is within 2 % (or 0.1 MW) of the PWL loss of its flow, at most twice per branch-step (a flow that keeps moving with \(\alpha\) keeps its second value). Steps that now over-lose are linearised too. A round that would only update \(\alpha\) (no step over-loses) is not run when the linearised losses together miss the PWL losses of their flows by at most 0.1 % of all line losses. There are at most 8 rounds.

Each re-solve uses the LP method of the first solve. Gurobi and CPLEX restart a changed LP from the previous basis with simplex whatever `Method` (`CPXPARAM_LPMethod`) says; on a large master that barrier solves in a few minutes, that simplex can run for an hour. Unless the method is simplex, the re-solves ignore the previous basis (Gurobi `LPWarmStart = 0`, CPLEX `CPXPARAM_Advance = 0`, set back afterwards). Other solvers re-solve as they do.

Steps without over-loss keep the PWL model and its marginal losses. The surplus is curtailed (at `curtailment_cost`, within `max_curtailment_ratio` in the master), stored or not generated, and curtailment is reported. Each solve starts again from the PWL model, so a model solved again after a change (Benders iterations, MGA, NPV retirement) is repaired for its new solution. The model stays an LP (or the MIP it was): a step costs one more solve per round, and only when some branch over-loses. If a repaired solve has no solution (losing energy on the lines was the only way to balance the model, e.g. a hard curtailment limit with nothing to build), the PWL solution is kept and a warning is logged.

The dispatch's losses are in the results: per node and step (`line_losses`, half of each line's loss at each end) and per year (`transmission_losses`, MWh); see the [HDF5 Output Schema](../reference/hdf5-output-schema.md#network-data).

### Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `loss_model` | `"pwl"` | Loss model selection: `"none"`, `"linear"`, or `"pwl"` |
| `pwl_loss_segments` | 3 | Number of PWL segments for operational dispatch |
| `pwl_loss_segments_master` | 2 | Number of PWL segments for the master problem and Benders subproblems |

Increasing the number of segments improves accuracy but adds variables and constraints.

## Power Balance: Single Bus vs. Multi-Bus

| Network Size | Model | Function |
|-------------|-------|----------|
| 1 bus | Simple power balance (PB-1) | `add_demand_constraints!()` |
| 2+ buses | DC power flow (DC-1 through DC-4) | `add_dc_constraints!()` |

The model selects the formulation from `network.num_buses`.

## Inter-System Links

When several systems are coupled by inter-system links (`meta_network.systems_links`), the runner merges all systems into one network before building the models. Each connection of a `link_type: transmission` link (the default) becomes an ordinary transmission line between the first bus of each endpoint node, with:

- capacity `existing_capacity_MW`
- length `distance_km`
- `reactance_pu` and `resistance_pu` when given (otherwise the usual defaults for lines)

The link then follows DC-1 to DC-3 and the configured loss model like any other line, inside one angle reference. Investment uses `max_investment_MW` and `investment_cost_per_MW`, which are written into `transference_invest_max` and `transference_invest_cost` at both endpoint nodes (the larger value where a node already has one). `loss_factor` is not used by this path. `cost_per_mw_km` is the link's flow cost: the master problem ([Capacity Expansion](capacity-expansion.md), §8.3.9) and the dispatch charge `distance_km` × `cost_per_mw_km` per MWh it carries, in either direction. In the dispatch it is the line's `flow_cost` \(c^{\text{flow}}_l\), charged as \(\Delta t \sum_t c^{\text{flow}}_l a_{l,t}\) with \(a_{l,t} \geq \pm f_{l,t}\) (`line_flow_abs_l*_t*_pos/neg`, `add_line_flow_costs!`) and reported as `line_flow_cost` in the cost breakdown.

A `link_type: fuel_route` link is not part of the electrical network: its connections become primary-energy fuel transport routes for its `fuel` (see [Inter-System Links](../gui/inter-system-links.md#how-links-enter-the-model) for the field mapping).

A merged system without `transmission_lines_geo` keeps its internal network: each node pair connected in `nodes_connections` becomes a line between the first buses of the two nodes, with that capacity, the `fuel_transport_distances` entry as length and the adjacency defaults (X from the length, R = 0.1 × X). Systems with lines use only their lines.

```yaml
meta_network:
  systems: ["SystemA", "SystemB"]
  systems_links:
    - systems: ["SystemA", "SystemB"]
      connections: [[0, 1]]  # Node pairs (node of SystemA, node of SystemB)
      existing_capacity_MW: [500.0]
      max_investment_MW: [500.0]
      investment_cost_per_MW: [1000000.0]
      # loss_factor is for fuel_route links only (optional here)
      reactance_pu: [0.01]
      resistance_pu: [0.001]
      distance_km: [100.0]
      cost_per_mw_km: [0.01]     # Flow cost, $ per MWh and km
```

---


## N-1 Security-Constrained DC Power Flow

When `n1_security.enabled: true` and the run's `simulation_mode` is in `n1_security.apply_to_modes` (default: `development` and `unit_commitment`), the operational dispatch adds N-1 security constraints. The list holds simulation modes, not the dispatch formulation: the windows of a development run get N-1 when `development` is listed, whether they are solved as economic dispatch or, with `master_problem.use_uc_in_dispatch`, as unit commitment. They are not applied in the master problem or in Benders. They act on the DC flow variables, so with an `acopf_*` mode only the generation part applies.

With `scopf_enabled: false` (default) the constraints are preventive; with `scopf_enabled: true` they are replaced by the SCOPF constraints below. Both are soft: every shortfall or overload is a slack priced at `penalties.loss_of_reserve_static`.

### Critical Elements

- **Transmission lines**: every transmission line (all its circuits together: the outage of a line takes out all of them) with capacity \(C_l = \) `capacity_mw` \(> 0\). No loading threshold is applied. Transformers are not included.
- **Largest generator**: the (generator, bus) pair with the largest `rated_power`, chosen once from the configured ratings, independently of commitment, output or investment. In a merged multi-system run each original system has its own (see below).

### Preventive N-1 (soft)

#### N1-1: Generation

If the largest unit \((g^*, n^*)\) trips, the headroom of the rest must cover its output:

\[
H^{N1}_t + s^{gen}_t \geq p_{g^*,n^*,t} \quad \forall t
\tag{N1-1}
\]

where \(H^{N1}_t\) is the system-wide headroom of reservable non-renewable units other than \((g^*, n^*)\) (online units only in unit commitment) plus battery headroom, as in the reserve constraints of the [operational dispatch](operational-dispatch.md). Constraint `n1_gen_reserve_system_t{t}`, shortfall `n1_gen_reserve_system_shortfall_t{t}`. In a merged multi-system run N1-1 holds per original system, like the reserves: each system's largest unit, covered by that system's headroom only (`n1_gen_reserve_system_s{k}_t{t}` for system \(k\)). The N-1 generation dual reported per step is the sum over the systems.

#### N1-2: Transmission

Each critical line keeps a share of its capacity free for post-contingency flows:

\[
-\rho^{post} C_l - s^{tr}_{l,t} \leq f_{l,t} \leq \rho^{post} C_l + s^{tr}_{l,t} \quad \forall l, t
\tag{N1-2}
\]

where \(\rho^{post}\) is `transmission_reserve_factor` (default 0.70) and \(f_{l,t}\) the flow of that physical line. Constraints `n1_trans_reserve_l{l}_t{t}_pos` / `_neg`, slack `n1_trans_over_l{l}_t{t}`.

Both slacks are priced at `penalties.loss_of_reserve_static` per MW and time step and reported as `n1_security_shortfall_cost`, so an insecure fleet or network shows up as a cost instead of an infeasible model.

With `generation_reserve_type: percentage` the requirement of N1-1 is `generation_reserve_percentage` × the system's total demand of the step instead of the largest unit's output, and no unit is excluded from the headroom.

`critical_line_threshold`, `contingency_depth`, `redistribution_mode`, `pi_screening_threshold`, `transformer_contingencies` and `battery_contingencies` only configure the post-hoc contingency analysis of the Studio's single-line diagram.

### PTDF Matrix Construction

The PTDF (Power Transfer Distribution Factor) matrix is computed from the network susceptances:

\[
\text{PTDF} = \mathbf{B}_f \cdot \mathbf{B}^{-1}_{bus}
\]

where:
- \(\mathbf{B}_{bus}\) is the bus susceptance matrix with \(b_l = 1/X_l\) (\(X_l\) the reactance of the whole line)
- \(\mathbf{B}_f\) is the branch-bus susceptance matrix

The slack bus row and column are removed before inversion (pseudo-inverse). Only transmission lines are included, not transformers.

### LODF-Based Contingency Analysis

Line Outage Distribution Factors (LODFs) give the post-contingency flows without rebuilding the B-matrix:

\[
\text{LODF}_{l,k} = \frac{\text{PTDF}_{l,\text{from}_k} - \text{PTDF}_{l,\text{to}_k}}{1 - (\text{PTDF}_{k,\text{from}_k} - \text{PTDF}_{k,\text{to}_k})}
\tag{LODF}
\]

Post-contingency flow on line \(l\) when line \(k\) trips:

\[
f_l^{(k)} = f_l + \text{LODF}_{l,k} \cdot f_k
\]

For a radial line (denominator ≈ 0) the LODF column is set to 0. The Python `ContingencyAnalyzer` uses LODF for `analyze_line_loss_fast()`, while the Julia SCOPF embeds LODF constraints in the optimization.

### Security-Constrained OPF (SCOPF)

When `scopf_enabled: true`, the preventive constraints are replaced, in a single solve, by:

**Line outages** (if `transmission_enabled`): for every pair of transmission lines \(l \neq k\) with capacity and \(|\text{LODF}_{l,k}| \geq 10^{-6}\),

\[
-C_l - o_{l,t} \leq f_{l,t} + \text{LODF}_{l,k} \cdot f_{k,t} \leq C_l + o_{l,t} \quad \forall t
\tag{SCOPF-1}
\]

with \(C_l\) the base capacity (`capacity_mw`), \(f\) the flow of each physical line in its own from→to direction, and \(o_{l,t} \geq 0\) the worst post-contingency overload of line \(l\) over all outages (`scopf_over_l{l}_t{t}`). Constraint names `scopf_line{l}_outage{k}_t{t}_pos` / `_neg`. All pairs are added at once; there is no iteration.

**Generation** (if `generation_enabled`):

- With `corrective_enabled: false`: the preventive headroom test N1-1 (`scopf_n1_gen_reserve_t{t}`, shortfall `scopf_n1_gen_reserve_shortfall_t{t}`).
- With `corrective_enabled: true`: re-dispatch after the trip. Every non-renewable unit other than \((g^*, n^*)\), reservable or not, contributes its headroom up to what it can ramp in one step, and batteries their headroom:

\[
\sum_{(g,n) \neq (g^*,n^*)} r_{g,n,t} + \sum_{b,n} \left( \bar{P}^{dis}_{b,n} - p^{dis}_{b,n,t} + p^{ch}_{b,n,t} \right) + s^{gen}_t \geq p_{g^*,n^*,t}, \qquad 0 \leq r_{g,n,t} \leq \min\left( h_{g,n,t},\; \bar{P}_{g,n} \, R^{up}_{g,n} \Delta t \right)
\tag{SCOPF-2}
\]

with \(h_{g,n,t}\) the unit's headroom as in N1-1 and no ramp cap when `ramp_up` is 0 or \(R^{up} \Delta t \geq 1\) (`n1_corrective_gen_t{t}`, shortfall `n1_corrective_gen_shortfall_t{t}`). `corrective_enabled` has no effect without `scopf_enabled`.

All slacks are priced at `penalties.loss_of_reserve_static` and reported as `n1_security_shortfall_cost`. The number of line-outage constraints grows with the square of the number of lines.

### Post-Optimization Contingency Analysis

The following analyses run in Python after the dispatch (`contingency.py`, `n1_assessment.py`) and do not change the optimization.

#### Contingency Screening

The Performance Index (PI) provides a fast screening metric:

\[
\text{PI}_c = \sum_l \left(\frac{f_{l}^{(c)}}{\bar{F}_l}\right)^2
\tag{PI}
\]

Contingencies with PI below a threshold are unlikely to cause overloads and can be skipped (`pi_screening_threshold`, default 0 = analyze all). The `screen_contingencies()` method returns a ranked list of the most critical contingencies.

#### Transformer Contingencies

Transformers are modeled as branches with their own impedance and thermal rating. When a transformer trips, the B-matrix is rebuilt without that branch, identical to a line outage (`transformer_contingencies`).

#### Battery Contingencies

Batteries actively discharging act as generation sources. When a discharging battery trips, the power deficit is redistributed among remaining generators using the same droop-based or pro-rata redistribution as generator contingencies. Only batteries with net injection > 0 are considered (`battery_contingencies`).

#### N-1-1 (N-k) Analysis

Sequential contingency analysis (`contingency_depth: n1_1`): after the first element trips and power redistributes, a second contingency is evaluated on the stressed system. The `analyze_n1_1()` method:

1. Applies the first contingency → post-contingency state
2. Rebuilds a modified snapshot reflecting the new operating point
3. Applies the second contingency on the modified state

The `screen_n1_1()` method identifies the most critical pairs by first finding N-1 contingencies that stress lines above a threshold (default 80% loading), then testing those stressed lines as second contingencies.

#### Droop-Based Generation Redistribution

With `redistribution_mode: droop`, remaining generators pick up a lost unit's output with droop-based participation factors instead of pro-rata:

\[
\Delta P_g = \frac{(1/R_g) \cdot P^{rated}_g}{\sum_{g'} (1/R_{g'}) \cdot P^{rated}_{g'}} \cdot P^{lost}
\]

where \(R_g\) is the generator's droop coefficient. This matches the primary frequency response assumption from the frequency analysis module.

#### Integrated N-1 Assessment

The `IntegratedN1Analyzer` combines three security dimensions into a unified assessment:

| Dimension | Metric | Source |
|-----------|--------|--------|
| Thermal | Line overloads, load shedding | `ContingencyAnalyzer` / `ACContingencyAnalyzer` |
| Frequency | ROCOF, nadir | `FrequencyAnalyzer` |
| Voltage | Bus voltage violations | `NativeACBridge` / `PandapowerBridge` |

Each contingency receives a composite severity score:

\[
S = \underbrace{\max\_\text{overload}\%}_{\text{thermal}} + \underbrace{\frac{P^{shed}}{P^{demand}} \times 100}_{\text{load shed}} + \underbrace{(\Delta f^{lim} - f^{nadir}) \times 20}_{\text{frequency}} + \underbrace{\sum |V^{viol}| \times 100}_{\text{voltage}}
\]

### N-1 Results in HDF5

When N-1 security is active, the following data is exported to the HDF5 output file under each year's `n1_security/` group:

| Dataset | Shape | Description |
|---------|-------|-------------|
| `gen_reserve_duals` | `[hours]` | Dual of generation N-1 reserve constraint (USD/MW) |
| `trans_reserve_duals/` | Group | Duals of SCOPF transmission constraints per line/outage pair |
| `binding_contingencies` | String array | Names of binding contingency constraints |
| `security_cost` | Scalar attr | Cost of N-1 security ($): objective with N-1 minus objective without it, with `compute_security_cost: true`; 0 otherwise |

### Configuration

`n1_security` is a top-level section of the configuration:

```yaml
n1_security:
  enabled: true
  apply_to_modes: [development, unit_commitment]
  transmission_enabled: true
  transmission_reserve_factor: 0.70
  generation_enabled: true
  scopf_enabled: false            # true: LODF post-contingency limits instead of preventive N-1
  corrective_enabled: false       # SCOPF generation N-1 by ramp-limited re-dispatch
  compute_security_cost: false    # true: re-solve each window without N-1 for n1_security_cost
```

### Cost of N-1 security

With `compute_security_cost: true`, every operational window that has N-1 constraints is solved a second time without them (`compute_n1_security_cost!`, `power_system.jl`): the solved model is copied, including the primary-energy block and user constraints, and the copy is re-solved with the N-1 shortfall and overload slacks free of charge, which makes all N-1 constraints inactive (they are soft). The difference

\[
C^{\text{N-1}} = z^{\text{with N-1}} - z^{\text{without N-1}}
\]

is the window's `n1_security_cost`: the re-dispatch, commitment and shortfall cost that security adds. A whole-window objective difference cannot be split by step, so the year's value adds each window's difference times its committed share (kept steps / window steps). In unit commitment both solves stop at the solver's `gap`, so the difference is exact only up to that gap. The extra solve roughly doubles the solve time; with the option off, `n1_security_cost` is 0.

### Computational Impact

Preventive N-1 adds two constraints and one slack per critical line and hour, plus one constraint per hour for generation. SCOPF adds two constraints per ordered pair of lines and hour, which grows quickly with network size. To keep the model small, apply N-1 only to the modes where it is needed (`apply_to_modes`), use the network reduction (`network_reduction`, see [Config Reference](../reference/config-reference.md#networkreductionconfig)) for large networks, and use the post-optimization analysis to study contingencies in detail.

Implemented in `add_n1_security_constraints!()` and `add_scopf_constraints!()` in `power_system.jl`.

---


## Example: 3-Bus System

A small lossless example (`loss_model: none`) illustrates the DC power flow formulation, with \(S_{base} = 100\):

```
    Bus 1 (Slack, Gen=130MW)
     / \
  L1/   \L2
   /     \
Bus 2     Bus 3
(D=100MW) (D=80MW, Gen=50MW)
    \     /
   L3\   /
      \ /
```

**Network data:**

| Line | From | To | Reactance (pu) | Susceptance \(S_{base}/x\) (MW/rad) | Capacity (MW) |
|------|------|----|-----------------|------|---------------|
| L1 | 1 | 2 | 0.10 | 1000 | 150 |
| L2 | 1 | 3 | 0.15 | 666.7 | 100 |
| L3 | 2 | 3 | 0.20 | 500 | 80 |

**Incidence matrix K:**

|  | L1 | L2 | L3 |
|--|----|----|-----|
| Bus 1 | +1 | +1 | 0 |
| Bus 2 | -1 | 0 | +1 |
| Bus 3 | 0 | -1 | -1 |

**Flow-angle coupling (DC-2)**, with \(\theta_1 = 0\):

- \(f_1 = 1000\,(\theta_1 - \theta_2)\)
- \(f_2 = 666.7\,(\theta_1 - \theta_3)\)
- \(f_3 = 500\,(\theta_2 - \theta_3)\)

**KCL at each bus (DC-1):**

- Bus 1: 130 = f₁ + f₂
- Bus 2: −100 = −f₁ + f₃
- Bus 3: 50 − 80 = −f₂ − f₃

**Solution:** θ₂ = −0.0878 rad, θ₃ = −0.0633 rad, giving f₁ ≈ 87.8 MW, f₂ ≈ 42.2 MW, f₃ ≈ −12.2 MW (L3 flows from bus 3 to bus 2). All flows are within capacity.


---


## AC Power Flow Verification

The DC power flow approximation is computationally efficient and well-suited for optimization, but it neglects voltage magnitude variations and reactive power. ESFEX provides an optional post-DC AC power flow verification step that runs a full Newton-Raphson solver on the DC-OPF dispatch solution, validating that the operating point is physically feasible under the full AC power flow equations.

### Purpose

It runs in unit-commitment dispatch, for systems with `transmission_lines_geo`, when `power_flow_mode: dcopf_ac_verify` or `ac_power_flow.enabled: true`.

The AC verification serves as a validation layer, not a re-optimization:

1. **Voltage profile validation** — Verify that bus voltage magnitudes remain within acceptable bounds (typically 0.90–1.10 p.u.) under the dispatch solution determined by the DC-OPF.
2. **Reactive power assessment** — Compute reactive power flows and generator reactive power output, which are invisible to the DC approximation.
3. **Accurate loss computation** — Calculate true \(I^2R\) + \(I^2X\) losses on each branch, including both active and reactive components.
4. **Thermal limit verification** — Check branch MVA flows against thermal ratings using the full apparent power (not just active power as in DC).
5. **Violation detection** — Flag voltage violations and line overloads that the DC approximation may miss, providing feedback for the planner.

### Dual Implementation

ESFEX offers two AC power flow implementations:

| Implementation | File | Engine | Use Case |
|---------------|------|--------|----------|
| Native Julia NR | `transmission_ac.jl` | Custom Newton-Raphson (polar form) | Integrated post-dispatch verification |
| Pandapower bridge | `pandapower_bridge.py` | pandapower Newton-Raphson | GUI analysis, IEC 60909 short-circuit |

#### Julia Newton-Raphson (`transmission_ac.jl`)

The native solver implements the standard Newton-Raphson method in polar coordinates:

- **Y-bus construction** from line impedances (π-model) and transformer models (with tap ratio)
- **Bus classification**: Slack (reference, known \(|V|\) and \(\theta\)), PV (generators, known \(P\) and \(|V|\)), PQ (loads, known \(P\) and \(Q\))
- **Jacobian**: 4-submatrix structure \(\mathbf{J} = [H, N; M, L]\) where \(H = \partial P/\partial\theta\), \(N = \partial P/\partial|V|\), \(M = \partial Q/\partial\theta\), \(L = \partial Q/\partial|V|\)
- **Convergence**: \(\max(|\Delta P|, |\Delta Q|) < \epsilon\) (default \(\epsilon = 10^{-6}\))
- **Initialization**: DC voltage angles as warm-start for \(\theta\), flat start (1.0 p.u.) for \(|V|\)

The solver takes the DC-OPF dispatch result and network data as input, and returns per-bus voltages, per-branch active/reactive power flows, total losses, voltage violations, and line overloads.

#### Native AC Power Flow Bridge (`native_ac_bridge.py`)

The native AC bridge uses the Julia Newton-Raphson solver (`transmission_ac.jl`) directly from the GUI, without requiring external dependencies. It converts the Studio state (`GuiSystemState`) into flat arrays, passes them to Julia via `solve_gui_ac_power_flow()`, and returns results in the same `ACPowerFlowResult` format used throughout the analysis pipeline.

The bridge implements the same duck-typed interface as the pandapower bridge, making it a drop-in replacement for AC power flow and N-1 contingency analysis. The GUI prefers the native bridge when available and falls back to pandapower automatically.

#### Pandapower Bridge (`pandapower_bridge.py`)

The pandapower bridge is now used exclusively for IEC 60909 short-circuit analysis (`ikss`, `ip`, `sk`) via `pandapower.shortcircuit.calc_sc()`. AC power flow has been migrated to the native Julia solver, reducing the external dependency footprint.

### AC Contingency Analysis

The `ACContingencyAnalyzer` extends N-1 security analysis to the AC domain:

- For each contingency (generator or line loss), the element is taken out of service in the bridge network
- For generator loss: output is redistributed pro-rata to remaining dispatchable generators based on headroom
- AC power flow is re-solved; if it diverges, the analysis falls back to DC contingency results
- Post-contingency results include voltage magnitudes, reactive power, line loading percentages, and voltage violation detection
- The analyzer is backend-agnostic: it works with both `NativeACBridge` (default) and `PandapowerBridge` through duck typing

### Configuration

Settings are read from the system's `ac_power_flow` section:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_iterations` | 50 | Maximum Newton-Raphson iterations |
| `tolerance` | 1e-6 | Convergence tolerance on power mismatch |
| `base_mva` | 100.0 | System base power for per-unit conversion |
| `voltage_min_pu` | 0.90 | Lower voltage magnitude limit |
| `voltage_max_pu` | 1.10 | Upper voltage magnitude limit |

### References

The DC power flow approximation is formally analyzed by Stott et al. [**[1]**](../reference/bibliography.md#ref1). The general power systems analysis framework follows Glover et al. [**[41]**](../reference/bibliography.md#ref41). PTDF and LODF factors for fast contingency screening follow Van Hertem et al. [**[42]**](../reference/bibliography.md#ref42) and Guler et al. [**[43]**](../reference/bibliography.md#ref43). Security-constrained OPF with post-contingency corrective actions follows Capitanescu et al. [**[34]**](../reference/bibliography.md#ref34) and Monticelli et al. [**[44]**](../reference/bibliography.md#ref44). Performance index-based contingency screening follows Ejebe and Wollenberg [**[45]**](../reference/bibliography.md#ref45). AC power flow verification uses Newton-Raphson as implemented in MATPOWER [**[46]**](../reference/bibliography.md#ref46) and pandapower [**[47]**](../reference/bibliography.md#ref47).

See the [full bibliography](../reference/bibliography.md) for complete citation details.
