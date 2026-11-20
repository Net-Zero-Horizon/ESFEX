# Constraint Catalog

Constraint names as they appear in the JuMP model (and in an LP file written by the solver). `{g}` generator, `{bi}` battery, `{b}` bus, `{ni}` node, `{t}` time step, `{y}` year, `{d}` representative day, `{s}` system, `{l}`/`{ℓ}` line. Rows whose pattern is a formula are constraints JuMP leaves unnamed. The formulation pages explain each family: [Operational Dispatch](../formulation/operational-dispatch.md), [Capacity Expansion](../formulation/capacity-expansion.md), [DC Power Flow](../formulation/dc-power-flow.md), [AC OPF](../formulation/ac-power-flow.md).

Generator and battery variables exist only for (unit, bus) pairs with capacity or investment potential, so there are no zero-capacity constraints. `bat_spillage_{bi}_{b}_{t}` is a variable name, not a constraint.

## power_system.jl --- Operational Dispatch

### Generator Constraints

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `gen_output <= (rated_eff + invest + delay_cap) * availability` | GEN-1a | Capacity × availability (renewable profile, 1 otherwise, × scheduled-outage factor), for every generator |
| `gen_output <= M_gb * status` | GEN-1b | Unit commitment only, non-renewables and reservoir hydro; `M_gb = max(1.1·max D, 1e4, 1.1·(rated + invest_max))` |
| `gen_output >= min_power * rated * status` | GEN-2 | Minimum stable generation (unit commitment) |
| `gen_output[t] - gen_output[t-1] <= cap * min(1, ramp_up * Δt)` | GEN-3a | Ramp-up limit (omitted when `ramp_up <= 0`) |
| `gen_output[t-1] - gen_output[t] <= cap * min(1, ramp_down * Δt)` | GEN-3b | Ramp-down limit |
| `ramp_up_seam_g{g}_b{b}`, `ramp_down_seam_g{g}_b{b}` | GEN-3s | Ramp limits between the previous window's last kept output and step 1 |
| `startup_detect_g{g}_b{b}_t{t}` | GEN-4 | Startup detection: `startup >= status[t] - status[t-1]` |
| `min_up_g{g}_b{b}_t{t}_tau{τ}` | GEN-5a | Minimum up time (in steps: `ceil(hours / Δt)`) |
| `min_down_g{g}_b{b}_t{t}_tau{τ}` | GEN-5b | Minimum down time |
| `gen_output <= rated / N * status`, `gen_output >= min_power * rated / N * status` | GEN-6a | Plant of `num_units` N > 1 (unit commitment): `status` = units online (integer 0..N), replaces GEN-1b and GEN-2 |
| `units_available_g{g}_b{b}_t{t}` | GEN-6b | `status <= ceil(N * availability)`, steps where fewer than N units are available |
| `min_up_g{g}_b{b}_t{t}` | GEN-6c | Units started in the last min-up steps `<= status[t]` (N > 1, replaces GEN-5a) |
| `min_down_g{g}_b{b}_t{t}` | GEN-6d | Units shut down in the last min-down steps `<= N - status[t]` (N > 1, replaces GEN-5b) |
| `gen_online_cap_g{g}_b{b}_t{t}` (variable) | RES-3 | Capacity of the units online, `<= rated / N * status` and `<= rated * availability`; spinning reserve and N-1 headroom of a plant of N > 1 units |

### PWL Cost Curve Constraints (Generators)

Generators with a PWL cost curve (several segments, or a flat curve whose `flat_price` differs from the fuel cost) have their output decomposed into segment variables linked to the aggregate output.

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `gen_output[g,b,t] == sum(gseg_{g}_{b}[k,t] for k)` | PWL-G1 | Segment output summation |
| `gseg_{g}_{b}[k,t] <= fraction_k * total_capacity * availability` | PWL-G2 | Segment upper bound |

The objective applies per-segment marginal costs: `cost += marginal_cost_k * gseg[k,t]`, replacing the flat `fuel_cost * gen_output` term.

### Reservoir Constraints

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `res_initial_g{g}_b{b}` | RES-1 | Initial level (`initial_level * capacity`, the previous window's level, or a TSAM boundary) |
| `res_dynamics_g{g}_b{b}_t{t}` | RES-2 | Water balance: `level[t+1] = level[t]*(1-evap) + inflow + cascade_in - output/eta_t + pump*eta_p - spillage` |
| `res_min_level_g{g}_b{b}_t{t}`, `res_max_level_g{g}_b{b}_t{t}` | RES-3 | Level bounds |
| `res_pump_limit_g{g}_b{b}_t{t}`, `res_no_pump_g{g}_b{b}_t{t}` | RES-4 | Pump-back power limit (0 without pump) |
| `res_no_spill_g{g}_b{b}_t{t}` | RES-5 | No spillage when spillage is not allowed |
| `res_end_lower_g{g}_b{b}`, `res_end_upper_g{g}_b{b}` | RES-6 | End level within `soc_end_tolerance` of the start level |
| `res_end_seasonal_g{g}_b{b}` | RES-9 | End level equals the period's start level after evaporation plus its intra-period change (TSAM seasonal linking, TSAM-SOC1) |
| `res_min_release_g{g}_b{b}_t{t}` | RES-7 | Minimum environmental flow: `output/eta_t + spillage >= min_release` |
| `res_head_limit_g{g}_b{b}_t{t}` | RES-8 | Head-dependent power limit |
| `res_zero_spill_*`, `res_zero_pump_*`, `res_zero_level_*` | RES-0 | Reservoir variables fixed to 0 where the unit has no reservoir |
| `reservoir_pump` subtracted from power balance | RES-PB | Pump as demand-side load |

### Battery Constraints

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `charge <= MaxChargePower * outage_factor * availability` | BAT-1a | Maximum charge rate (degraded rating + investment) |
| `discharge <= MaxDischargePower * outage_factor * availability` | BAT-1b | Maximum discharge rate |
| `soc[t+1] = soc[t]*(1-self_discharge) + (eta_c*charge - discharge/eta_d - spillage)*Δt` | BAT-2 | SOC dynamics |
| `soc >= soc_min * capacity` | BAT-4a | Minimum SOC; after steps where the battery cannot charge (outage, zero availability) the floor is what full charging can hold against self-discharge |
| `soc <= soc_max * capacity + soc_violation` | BAT-4b | Soft maximum SOC |
| `bat_soc_initial_{bi}_{b}` | BAT-3 | Initial SOC (config, or the previous window's energy in MWh) |
| `bat_soc_end_lower_{bi}_{b}` | BAT-5a | End-of-horizon SOC lower bound (only with `cyclic_end_soc = true`, the Julia default; the Python runner turns it off), at most the SOC that charging at every step can reach |
| `bat_soc_end_upper_{bi}_{b}` | BAT-5b | End-of-horizon SOC upper bound (same) |
| `bat_soc_end_floor_{bi}_{b}` | BAT-5c | Soft floor at the year-start SOC on the year's last window; shortfall `bat_soc_end_short_{bi}_{b}` priced at 10 % of VOLL per time step |
| `charge <= M_bat*cs`, `discharge <= M_bat*(1-cs)` | BAT-6 | Relaxed charge/discharge exclusivity, `M_bat = 2·max(P_ch, P_dis)`, `cs ∈ [0,1]` |
| `bat_min_cycling_{bi}_{b}` | BAT-7 | Minimum cycling requirement over period, capped at what the battery can charge in the window (none when it cannot charge); shortfall `bat_min_cycling_short_{bi}_{b}` priced at 10 % of VOLL per MWh |
| `spillage <= MaxDischargePower` | BAT-8 | Spillage power limit (batteries with `spillage = true`) |
| `±(charge[t] - charge[t-1]) <= ramp × Δt × MaxChargePower`, likewise discharge | BAT-9 | Ramp limits (`ramp_up` rising, `ramp_down` falling; none when 0 or ramp × Δt ≥ 1); at t = 1 from the previous window, `bat_charge_bi{bi}_b{b}_ramp_{up,down}_seam`, `bat_discharge_bi{bi}_b{b}_ramp_{up,down}_seam` |
| `bat_soc_link_{bi}_{b}` | BAT-M1 | Master/TSAM: end SOC equals the period's start level plus its intra-period change (TSAM-SOC1) |
| `bat_soc_cyclic_{bi}_{b}` | BAT-M2 | Master: end SOC equals initial SOC on each representative day |

### PWL Cost Curve Constraints (Batteries)

Batteries with a discharge cost curve (also a single segment whose price differs from `throughput_degradation_cost`) have discharge power decomposed into segment variables analogously to generators.

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `bat_discharge[bi,b,t] == sum(bseg_{bi}_{b}[k,t] for k)` | PWL-B1 | Segment discharge summation |
| `bseg_{bi}_{b}[k,t] <= fraction_k * MaxDischargePower` | PWL-B2 | Segment upper bound |

The objective applies per-segment marginal costs: `cost += marginal_cost_k * bseg[k,t]`, replacing the flat throughput degradation cost term.

### Reserve Constraints

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `reserve_static_{ni}_{t}` | RES-1 | Static reserve: `reserve_static + loss_of_reserve >= requirement` (per node) |
| `reserve_static_held_{ni}_{t}` | RES-1 | Static reserve held at node `ni` = the system's held share (`reserve_static_share_s{k}_t{t}`, 0–1) × its requirement |
| `reserve_dynamic_avail_s{k}_t{t}` | RES-2a | Dynamic reserve of system `k` at most `dynamic_reserve_contribution` × rated power of its reservable units (committed units in UC) and batteries; in the master also of the reservable capacity built |
| `reserve_dynamic_req_{ni}_{t}` | RES-2b | Dynamic reserve requirement (per node) |
| `reserve_dynamic_held_{ni}_{t}` | RES-2b | Dynamic reserve held at node `ni` = the system's held share (`reserve_dynamic_share_s{k}_t{t}`) × its requirement |
| `reserve_dynamic_zero_{ni}_{t}` | RES-2c | Dynamic reserve = 0 in systems without a dynamic requirement |
| `reserve_headroom_s{k}_t{t}` | RES-3 | Static + dynamic reserve of system `k` fit in the free capacity of its reservable generators and batteries (committed units in UC); in the master also of the reservable technologies and battery technologies built (RES-M1) |

### Power Balance

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| Single-bus: `gen + discharge + v2g + load_shed + rooftop = demand + electrolyzer + charge + pump + ev_charge + rooftop_curt` | PB-1 | Power balance (single bus); reserves and flexible curtailment are not in it |
| `max_load_shed_b{b}_t{t}` | PB-1b | `load_shed <= max(bus demand, 0)` (0 on connection buses) |
| `rooftop_curt_limit_n{ni}_t{t}` | PB-1c | Rooftop curtailment at most the node's rooftop generation |
| Multi-bus via DC power flow (see `transmission_dc.jl`) | DC-1 | Network power balance |

### N-1 Security

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `n1_gen_reserve_system_t{t}` | N1-1 | Headroom of the other reservable units and batteries + `n1_gen_reserve_system_shortfall_t{t}` >= output of the largest unit. In a merged multi-system run one per system `k` (`n1_gen_reserve_system_s{k}_t{t}`), over that system's units, batteries and largest unit (likewise `scopf_n1_gen_reserve_s{k}_t{t}`, `n1_corrective_gen_s{k}_t{t}`) |
| `n1_trans_reserve_l{l}_t{t}_pos`, `_neg` | N1-2 | Per physical line: `±flow <= reserve_factor * capacity + n1_trans_over_l{l}_t{t}` |
| `scopf_line{l}_outage{k}_t{t}_pos`, `_neg` | N1-3 | SCOPF: `|f_l + LODF[l,k]·f_k| <= cap_l + scopf_over_l{l}_t{t}` |
| `scopf_n1_gen_reserve_t{t}` | N1-1 | SCOPF: same headroom test as `n1_gen_reserve_system_t{t}`, slack `scopf_n1_gen_reserve_shortfall_t{t}` |
| `n1_corrective_gen_t{t}` | N1-4 | SCOPF + corrective: ramp-limited headroom of all non-renewable units (`n1_corrective_gen_response_*`) + battery headroom + shortfall >= output of the largest unit |

The N-1 slacks are priced at `loss_of_reserve_static` × Δt; they are added to the objective after the constraints are built (`n1_security_shortfall_cost`). With `compute_security_cost`, a copy of the solved window is re-solved with these slacks free (`compute_n1_security_cost!`) to report the cost of N-1 security.

### Curtailment

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `curtailment[ni,t] == available_RE - used_RE` | CUR-1 | Node curtailment of variable renewables (reservoir hydro excluded) |
| `curtailment[ni,t] <= available_RE` | CUR-2 | Upper bound |

The `max_curtailment_ratio` cap is not in the dispatch; see `max_curtailment_s*_y*` in the master.

### Renewable Energy Target

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `re_penetration_target` | RE-1 | `Δt·sum(renewable_gen) + Δt·sum(fre_loss) >= target * Δt·sum(demand)`; battery discharge not counted |

### CO2 Emissions

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `co2_emissions_def_n{ni}_t{t}` | CO2-1 | Emissions definition per node and step |
| `CO2_budget_constraint` | CO2-2 | `Δt·sum(emission_factor / eff_at_rated * gen_output) <= annual_budget * window_hours / hours_per_year + violation` (t) |
| `CO2_budget_constraint[{system}]` | CO2-2 | Multi-system run: the same, one per system over that system's nodes, with its own budget and violation penalty |

### Inertia

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `inertia_{t}` | INE-1 | `sum(gen_output * H) + sum((charge + discharge) * H_bat) + loss_of_inertia >= threshold` (MW·s) |

### EV Constraints

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `ev_soc_initial_n{ni}` | EV-7 | Initial fleet SOC |
| `ev_soc_dynamics_n{ni}_{t}` | EV-1 | `soc[t+1] = soc[t] + (eta_c*charge - v2g/eta_d - (driving - ev_loss))*Δt` |
| `ev_loss_max_n{ni}_{t}` | EV-2 | Unserved driving energy at most the driving energy |
| `ev_max_charge_n{ni}_{t}` | EV-3 | `charge <= max(P_ch*N/1000, 2*driving)` |
| `ev_max_v2g_n{ni}_{t}` | EV-4 | `v2g <= P_v2g*N/1000*availability` |
| `ev_soc_min_n{ni}_{t}`, `ev_soc_max_n{ni}_{t}` | EV-5 | SOC bounds |
| `ev_mutex_charge_n{ni}_{t}`, `ev_mutex_v2g_n{ni}_{t}` | EV-6 | Relaxed charge/V2G exclusivity |
| `ev_soc_cyclic_n{ni}` | EV-7 | End-of-window SOC equals initial SOC |
| `ev_zero_charge_n{ni}_{t}`, `ev_zero_v2g_n{ni}_{t}`, `ev_zero_soc_n{ni}_{t}`, `ev_zero_loss_n{ni}_{t}` | EV-0 | EV variables fixed to 0 at nodes without vehicles |

### Electrolyzer

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `electrolyzer_max_b{b}_t{t}` | ELZ-1 | Power at most the rated power × scheduled-outage availability (0 where none is configured); `electrolyzer_max_e{e}_b{b}_t{t}` for the second and later electrolyzers |
| `electrolyzer_ramp_up_b{b}_t{t}`, `electrolyzer_ramp_down_b{b}_t{t}` | ELZ-2 | `±(P[t] - P[t-1]) <= rated * min(1, ramp * Δt)`; omitted when the ramp is 0 |

### Demand & Sectoral

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `sectoral_lol_cap_{sector}_{ni}_{t}` | SEC-1 | Sectoral LOL <= the sector's demand at the node |
| `sectoral_lol_sum_{ni}_{t}`, `sectoral_lol_residual_{ni}_{t}` | SEC-2 | Load shed on the node's buses minus the sectoral LOL is in [0, node demand not covered by any sector]; that remainder is priced at VOLL × the demand-weighted mean of the node's sector criticalities (1 without sector demand) |
| `demand_shift_out_cap_{sector}_{ni}_{t}` | SHIFT-1 | Demand shifted out of a step at most `flexibility_ratio` × sector demand (flexible sectors with `delay_tolerance > 0`); the net shift enters the power balance |

### Cost Limits

The dispatch builds nothing, so it has no investment limits (the master's are under [Investment Limits](#investment-limits)).

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `operating_budget` | COST-1 | Only with `max_annual_operating_cost`: `operating spending of the committed steps - operating_budget_excess_t1 <= window budget` (spending: fuel and maintenance per MWh, the committed steps' share of the fixed O&M, primary-energy fuel bought and transported; window budget: what is left of the year's, × the window's share of the remaining demand); the excess priced at `OPERATING_BUDGET_PENALTY` (1000 $/$) |

`transfer_margin_{i}_{j}` is a slack variable created only when the network has no transmission lines; it appears in no constraint and is always 0.

### User-Defined Constraints

Constraints from `custom_constraints` and plugins (`user_constraints.jl`) carry the name given in their specification (default `user_constraint`). They are added after all base constraints in the dispatch, the master and the stochastic master. See [Custom Constraints](../user-guide/custom-constraints.md).

---

## transmission_dc.jl --- DC Power Flow

### Network Balance (KCL)

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `net_inj == flow_sum` | DC-1 | Kirchhoff's Current Law at each bus (same terms as PB-1, plus converter injections and master extras) |
| `max_load_shed_dc_b{b}_t{t}` | DC-1b | `load_shed <= max(bus demand, 0)` |
| `rooftop_curt_limit_dc_n{ni}_t{t}` | DC-1c | Rooftop curtailment limit |

### PWL Transmission Losses

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `pf == fpos - fneg` | LOSS-1 | Flow direction decomposition (`fpos_{ℓ}_{t}`, `fneg_{ℓ}_{t}`) |
| `fpos == sum_k(dp_k)`, `fneg == sum_k(dn_k)` | LOSS-2 | Segment sums |
| `0 <= dp_k, dn_k <= delta_f` | LOSS-3 | Segment widths, `delta_f = base capacity / N`; on a line the master can expand the last segment also spans the most MW the corridor can gain by that year (unbounded only when its investment has no limit, and on Benders' expandable lines) |
| `ploss == sum_k(m_k * (dp_k + dn_k))` | LOSS-4 | PWL loss, `m_k = min(R_pu, 50)·(2k−1)·delta_f / S_base` |
| `net_inj == sum_l(K * pf + 0.5 * abs(K) * ploss)` | LOSS-5 | KCL with half-loss split: each end supplies half the loss |

Dispatch, master and Benders all use these rows, with `pwl_loss_segments` (dispatch) or `pwl_loss_segments_master` segments. `loss_model: linear` is the same with one segment of slope `m_1 = clamp(R_pu, 0, 1)`; `none` drops them.

### Flow–Angle Coupling

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `flow_angle_{ℓ}_{t}` | DC-2 | `pf = (S_base / x) * (angle[from] - angle[to])`, x ≤ 0 replaced by 0.01 |
| `flow_zero_{ℓ}_{t}` | DC-2z | `pf = 0` on lines below 0.1 MW that cannot be expanded |
| `voltage_angle[ref] == 0` | DC-3 | Reference angle, one per connected component |

There is no DC angle-difference limit; `max_angle_diff_deg` applies to AC-OPF only.

### Line Capacity

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `flow <= (capacity + investment) * outage_factor` | DC-5a | Line/transformer capacity (positive direction) |
| `flow >= -(capacity + investment) * outage_factor` | DC-5b | Line/transformer capacity (negative direction) |
| `line_flow_abs_l{l}_t{t}_pos`, `_neg` | OBJ-17 | Lines with a `cost_per_mw_km` (inter-system links): `line_flow_abs_l{l}_t{t} >= ±flow`, charged length × `cost_per_mw_km` per MWh |

### Devices

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `rectify + invert <= rated_power * outage_factor` | DC-6a | AC/DC converter power limit |
| `freq_ab + freq_ba <= rated_power * outage_factor` | DC-6b | Frequency converter power limit |
| `on_f + on_b <= 1`, `min_power_mva * on <= flow <= rated_power * outage_factor * on` | DC-6c | Unit commitment dispatch only, converters with `min_power_mva > 0`: binaries `acdc_on_rectify_c{c}`/`acdc_on_invert_c{c}`, `freq_on_a_to_b_c{c}`/`freq_on_b_to_a_c{c}` per step |

---

## master_problem.jl --- Capacity Expansion

### Investment Limits

Investment variables are per technology and battery technology (`tech_inv_y*_t*`, `bat_tech_pow_inv_*`, `bat_tech_cap_inv_*`), created at investment-period start years.

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `cumul_tech_t{t}_b{b}` | TECH-1 | `sum_y(tech_invest) <= invest_max` |
| `cumul_bat_tech_pow_bt{bt}_b{b}`, `cumul_bat_tech_cap_bt{bt}_b{b}` | TECH-2 | Battery-technology power and energy limits |
| `bat_tech_min_dur_bt{bt}_b{b}_y{y}`, `bat_tech_max_dur_bt{bt}_b{b}_y{y}` | TECH-3 | Battery-technology E/P ratio per period |
| `cumul_node_tech_t{t}_n{ni}`, `cumul_node_bat_tech_pow_bt{bt}_n{ni}`, `cumul_node_bat_tech_cap_bt{bt}_n{ni}` | TECH-4 | Node-aggregate limits (nodes with several buses) |
| `cumul_res_g{g}_b{b}` | TECH-5 | Cumulative reservoir investment limit |
| `def_cap_tech_y{y}_t{t}_b{b}`, `def_cap_bt_pow_y{y}_bt{bt}_b{b}`, `def_cap_bt_en_y{y}_bt{bt}_b{b}`, `def_cap_trans_y{y}_{i}_{j}`, `def_cap_res_y{y}_g{g}_b{b}`, `def_cap_conv_y{y}_{kind}{c}`, `def_cap_fuel_y{y}_{kind}_{name}_{i}` | CUM-5 | Capacity state variable `cap_*` of year `y` = the capacity built and in service (CUM-1b, CUM-3, CUM-4, reservoirs, converters, fuel builds; technology and battery-technology builds degraded by age); the representative-day rows use `cap_*` |

### Budget

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `budget_y{y}` | BUD-1 | Technology, battery-technology, transmission (once per corridor, at `max(c_i, c_j)`) and reservoir investment <= budget + slack, per period start |

### Transmission

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `trans_sym_{i}_{j}_y{y}` | TXN-1 | Symmetric investment when both directions of a corridor have a variable |

### Capacity Adequacy

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `cap_adequacy_y{y}_node{ni}` | CAP-1 | Firm capacity (firm share × risk coefficient: capacity credit for variable renewables, peak-period mean availability for other units; batteries at most min(power, usable energy / storage firm hours), CAP-5) + net peak imports + slack >= peak demand × reserve margin |
| `bat_tech_firm_power_y{y}_bt{k}_b{b}` | CAP-5b | Firm power `bat_tech_firm_y{y}_bt{k}_b{b}` of battery technology `k` <= its power built and in service |
| `bat_tech_firm_energy_y{y}_bt{k}_b{b}` | CAP-5b | Storage firm hours × firm power <= its usable energy built and in service |
| `cap_import_max_y{y}_{m}_{n}` | CAP-3 | Peak import `cap_import_y{y}_{m}_{n}` <= transfer capacity between the nodes |

### RE Targets

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `re_target_s{s}_y{y}` | RE-2, RE-3a | Annual renewable generation of system `s` (representative days scaled to the year) + `slack_re_s{s}_y{y}` (MWh, at `fre_penetration_loss` per MWh) >= max(target, initial + y × `min_re_increment`) × its total generation; one per scenario (`_sc{k}`) in a stochastic master |
| `re_max_increment_s{s}_y{y}` | RE-3b | Renewable generation <= what the previous year's actual share allows + `max_re_increment` × total generation + `slack_re_cap_s{s}_y{y}` (MWh, at VOLL); year 1 from the initial share × generation (myopic: last year's achieved share), later years last year's renewable generation × the demand growth; one per scenario (`_sc{k}`) in a stochastic master |

### Retirement and Life Extension

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| Age-based: `age_at_year >= lifetime -> capacity = life_extension` | RET-1 | Existing units within their lifetime count their rating degraded by age (generators and batteries, power and energy, CUM-1/CUM-2); past it they keep only `gen_life_ext_*` / `bat_life_ext_*`, each at most that degraded rating; investments retire after `lifetime` |
| `cascade_gen_g{g}_n{n}_y{y}`, `cascade_bat_b{bi}_n{n}_y{y}` | RET-2 | Life extension can only decrease over time |

### Operational Validation (Representative Days)

The days reuse the dispatch constraints above (generators and batteries with capacity overrides: their capacity in service that year, degraded by age as the dispatch degrades it, or life-extended; a battery's charge power is that discharge power × its `MaxChargePower` / `MaxDischargePower`; reservoirs, reserves, inertia, power balance, curtailment), with names prefixed per day, plus:

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `tech_cap_t{t}_b{b}_ts{ts}_y{y}_d{d}` | OP-1 | Technology output <= cumulative capacity `cap_tech_y{y}_t{t}_b{b}` (× availability for renewables) |
| `tech_seg_cap_t{t}_b{b}_k{k}_ts{ts}_y{y}_d{d}` | OP-1b | Technology with a `fuel_cost_curve`: segment `k` <= `fraction_k` × cumulative capacity (× availability); the segments sum to the output and are charged their marginal costs instead of `fuel_cost` |
| `bt_dis_cap_*`, `bt_ch_cap_*`, `bt_soc_cap_*` | OP-2a | Battery-technology power (× availability) and energy limits; unnamed BAT-9 ramp limits on the power built |
| `bt_soc_init_*`, `bt_soc_dyn_*`, `bt_soc_cyc_*` | OP-2b | Battery-technology SOC: initial, dynamics (`× Δt`), cyclic |
| `bt_soc_zero_*`, `bt_dis_zero_*`, `bt_ch_zero_*` | OP-2c | Battery technology fixed to 0 where its efficiency is 0 |
| `lol_sec_ub_{sector}_y{y}_d{d}_n{n}_t{t}` | OP-5 | Sectoral LOL <= sector demand at the bus |
| `lol_sec_sum_y{y}_d{d}_n{n}_t{t}`, `lol_sec_residual_y{y}_d{d}_n{n}_t{t}` | OP-5 | Bus load shed minus sectoral LOL is in [0, bus demand not covered by any sector] (the sectoral LOL is part of the load shed) |
| `max_curtailment_s{σ}_y{y}` | CURT-1 | Annual, per system: curtailment (existing + technologies) over the year <= `max_curtailment_ratio` × RE generation over the year (+ the RE-3c exemption) + the excess `curtailment_excess_s{σ}_y{y}` (MWh/yr, at max(10⁴, 100 × the dearer of the RE shortfall price and the curtailment cost) $/MWh); a stochastic scenario's `max_curtailment_sc{k}_s{σ}_y{y}` |

### TSAM Inter-Period Linking

Seasonal storage by the superposition of inter- and intra-period states (Kotzur et al. 2018), [capacity expansion §8.5.3](../formulation/capacity-expansion.md#853-seasonal-storage-inter-period-linking).

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| unnamed rows on `soc_intra_max_y{y}_b{b}_n{n}_p{p}`, `soc_intra_min_y{y}_b{b}_n{n}_p{p}` | TSAM-SOC2 | The period's highest and lowest SOC relative to its start (`soc_start_*`; its change `soc_delta_*`) |
| `soc_inter_y{y}_b{b}_n{n}_chain_d{d}` | TSAM-CHAIN | Inter-day SOC: start of day d+1 = start of day d + the change of day d's period (year-cyclic, d = 0…364) |
| `soc_inter_y{y}_b{b}_n{n}_max_d{d}`, `soc_inter_y{y}_b{b}_n{n}_min_d{d}` | TSAM-CAP | Day d's SOC within [0, the battery's energy in service that year] (life-extended energy included, as the period's SOC bound) |
| unnamed rows on `reslvl_intra_max_y{y}_g{g}_n{n}_p{p}`, `reslvl_intra_min_y{y}_g{g}_n{n}_p{p}` | TSAM-SOC2 | The period's highest and lowest reservoir level relative to its (evaporating) start (`reslvl_start_*`; its change `reslvl_delta_*`) |
| `reslvl_inter_y{y}_g{g}_n{n}_chain_d{d}` | TSAM-CHAIN | Inter-day reservoir level, decayed by the period's evaporation, plus the change of day d's period (year-cyclic) |
| `reslvl_inter_y{y}_g{g}_n{n}_max_d{d}`, `reslvl_inter_y{y}_g{g}_n{n}_min_d{d}` | TSAM-CAP | Day d's levels within [min level, max level] × the in-service reservoir capacity |
| `reslvl_inter_y{y}_g{g}_n{n}_head_d{d}` | TSAM-HEAD | With head dependence: the period's start level ≤ the inter-day level of each of its days |

In the stochastic master these names carry a scenario tag.

### Families in the binding report

After each master solve the rows are grouped into these families for the binding-constraint report (`master_diagnostics.jl`, written to [`/diagnostics/master`](hdf5-output-schema.md#diagnosticsmaster); see [Why did the master build this?](../user-guide/master-problem.md#why-did-the-master-build-this-binding-constraints-and-duals)). A row belongs to the family with the longest matching name prefix; a build limit counts both as a row over the horizon and as the upper bound of the year's investment variable (a one-year master holds it twice, and either may carry the dual). The kind decides how the report shows it: `policy` and `build_limit` families are named in the log digest, `operation` families are summarised by the number of rows they bind, `structural` ones (definitions and dynamics, always tight) are counted but left out of the top rows. Other rows fall back to a family named after the row without its index tokens (`s1`, `y2`, `ts5`…), kind `other`; a user-defined constraint is its own family `user:<name>`; rows without a name (the power balance, the DC line capacities, the unit-commitment relaxation rows) are family `unnamed`.

| Family | Label | Rows | Catalogue | Kind | Dual unit |
|--------|-------|------|-----------|------|-----------|
| `re_target` | RE target | `re_target*` | RE-2 | policy | $/MWh |
| `re_growth_cap` | RE growth cap (max_re_increment) | `re_max_increment*` | RE-3b | policy | $/MWh |
| `curtailment_cap` | Curtailment cap | `max_curtailment*` | CURT-1 | policy | $/MWh |
| `budget` | Annual budget | `budget_y*` | BUD-1 | policy | $/$ |
| `adequacy` | Capacity adequacy (reserve margin) | `cap_adequacy*` | CAP-1 | policy | $/MW |
| `adequacy_import` | Adequacy import limit | `cap_import_max*` | CAP-3 | policy | $/MW |
| `adequacy_storage_firm` | Storage firm capacity | `bat_tech_firm_power*`, `bat_tech_firm_energy*` | CAP-5b | policy | $/MW |
| `co2_budget` | CO2 budget | `co2_budget*` | CO2-1 | policy | $/t |
| `tech_build_limit` | Technology build limit | `cumul_tech*`; upper bound of `tech_inv*` | TECH-1 | build_limit | $/MW |
| `tech_node_build_limit` | Technology build limit (node) | `cumul_node_tech*` | TECH-4 | build_limit | $/MW |
| `storage_power_build_limit` | Storage power build limit | `cumul_bat_tech_pow*`; upper bound of `bat_tech_pow_inv*` | TECH-2 | build_limit | $/MW |
| `storage_energy_build_limit` | Storage energy build limit | `cumul_bat_tech_cap*`; upper bound of `bat_tech_cap_inv*` | TECH-2 | build_limit | $/MWh |
| `storage_power_node_build_limit` | Storage power build limit (node) | `cumul_node_bat_tech_pow*` | TECH-4 | build_limit | $/MW |
| `storage_energy_node_build_limit` | Storage energy build limit (node) | `cumul_node_bat_tech_cap*` | TECH-4 | build_limit | $/MWh |
| `storage_duration` | Storage duration (E/P) bounds | `bat_tech_min_dur*`, `bat_tech_max_dur*` | TECH-3 | build_limit | $/MWh |
| `reservoir_build_limit` | Reservoir build limit | `cumul_res*`; upper bound of `res_inv*` | TECH-5 | build_limit | $/MWh |
| `converter_build_limit` | Converter build limit | `cumul_conv*`; upper bound of `conv_inv*` |  | build_limit | $/MW |
| `electrolyzer_build_limit` | Electrolyzer build limit | `elz_inv_total*`; upper bound of `elz_inv*` |  | build_limit | $/MW |
| `fuel_storage_build_limit` | Fuel storage build limit | `fuel_storage_inv_total*`; upper bound of `fuel_storage_inv*` |  | build_limit | $/unit |
| `fuel_transport_build_limit` | Fuel transport build limit | `fuel_transport_inv_total*`; upper bound of `fuel_transport_inv*` |  | build_limit | $/unit |
| `retirement_cascade` | Retirement cascade | `cascade_gen*`, `cascade_bat*` | RET-2 | build_limit | $/MW |
| `tech_capacity` | Technology capacity (hourly) | `tech_cap*`, `tech_seg_cap*` | OP-1, OP-1b | operation | $/MW |
| `storage_power` | Storage power (hourly) | `bt_dis_cap*`, `bt_ch_cap*` | OP-2a | operation | $/MW |
| `storage_energy` | Storage energy (hourly) | `bt_soc_cap*` | OP-2a | operation | $/MWh |
| `load_shed_limit` | Load shed at most the demand | `max_load_shed*` | PB-1b | operation | $/MW |
| `reserve_headroom` | Reserve headroom | `reserve_headroom*` | RES-3 | operation | $/MW |
| `reserve_static` | Static reserve requirement | `reserve_static*` | RES-1 | operation | $/MW |
| `reserve_dynamic` | Dynamic reserve | `reserve_dynamic_req*`, `reserve_dynamic_avail*` | RES-2b, RES-2a | operation | $/MW |
| `inertia` | Inertia floor | `inertia*` | INE-1 | operation | $/MWs |
| `sector_load_shed_limit` | Sectoral load shed limit | `lol_sec_ub*` | OP-5 | operation | $/MW |
| `storage_inter_day` | Storage inter-day level | `soc_inter*` | TSAM-CAP | operation | $/MWh |
| `reservoir_inter_day` | Reservoir inter-day level | `reslvl_inter*` | TSAM-CAP | operation | $/MWh |
| `reservoir_level` | Reservoir level bounds | `res_max_level*`, `res_min_level*` | RES-3 | operation | $/MWh |
| `reservoir_min_release` | Reservoir minimum release | `res_min_release*` | RES-7 | operation | $/MW |
| `fuel_coupling` | Fuel for power | `pe_coupling*` | PE-8 | operation | $/MWh |
| `fuel_supply` | Fuel supply limit | `pp_max_supply*` | PE-1 | operation | $/unit |
| `fuel_transport` | Fuel transport capacity | `pp_transport_cap*` | PE-2 | operation | $/unit |
| `fuel_tank` | Fuel tank capacity | `hr_storage_max*` | PE-5 | operation | $/unit |
| `capacity_state` | Capacity state definitions | `def_*` | CUM-5 | structural | $/MW |
| `transmission_symmetry` | Transmission symmetry | `trans_sym*` | TXN-1 | structural | $/MW |
| `storage_soc` | Storage SOC (initial, dynamics, cyclic) | `bt_soc_init*`, `bt_soc_dyn*`, `bt_soc_cyc*` | OP-2b | structural | $/MWh |
| `storage_zero` | Storage fixed to 0 | `bt_soc_zero*`, `bt_dis_zero*`, `bt_ch_zero*` | OP-2c | structural | $/unit |
| `battery_soc` | Battery SOC links | `bat_soc*` | BAT-M | structural | $/MWh |
| `sector_load_shed_split` | Sectoral load shed split | `lol_sec_sum*`, `lol_sec_residual*` | OP-5 | structural | $/MW |
| `reserve_share` | Reserve held per node | `reserve_static_held*`, `reserve_dynamic_held*`, `reserve_dynamic_zero*` | RES-1, RES-2b, RES-2c | structural | $/MW |
| `flow_angle` | Flow-angle coupling | `flow_angle*`, `flow_zero*` | DC-2, DC-2z | structural | $/MW |
| `reservoir_dynamics` | Reservoir water balance | `res_dynamics*` | RES-2 | structural | $/MWh |
| `fuel_tank_balance` | Fuel tank balance | `hr_storage_balance*` | PE-4 | structural | $/unit |
| `transmission_build_limit` | Transmission build limit | upper bound of `trans_inv*` |  | build_limit | $/MW |
| `life_extension_limit` | Life extension up to the rating | upper bound of `gen_life_ext*`, `bat_life_ext*` | RET-1 | build_limit | $/MW |

The slack families of the same report (`slacks` dataset): `budget` (`slack_budget_y{y}`, $), `adequacy` (`slack_cap_y{y}_node{n}`, MW), `re_target` (`slack_re_s{s}_y{y}`, MWh/yr), `re_growth_cap` (`slack_re_cap_s{s}_y{y}`, MWh/yr), `curtailment_cap` (`curtailment_excess_s{s}_y{y}`, MWh/yr), `co2_budget` (`co2_budget_violation_y{y}_{group}`, t), and on the representative days, scaled to the year: `load_shed` (`op_y{y}_d{d}_ll`), `load_shed_sector_<sector>` (`op_y{y}_d{d}_lol_<sector>`, part of the load shed), `reserve_static_shortfall` (`_rs_loss`), `reserve_dynamic_shortfall` (`_rd_loss`) and `inertia_shortfall` (`_inertia_loss`).


## benders.jl --- Benders Decomposition

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `benders_cut_y{y}_iter{k}` | BEN-CUT | Optimality cut on `theta_y{y}`, one per year and iteration (`iter0`: warm start), its gradient summed over the year's representative days |
| `benders_cut_priced_y{y}_iter{k}` | BEN-CUT | Optimality cut from year `y`'s subproblem with its curtailment limits priced at VOLL, added when that subproblem breaks a limit |
| `benders_feas_y{y}_iter{k}` | BEN-FEAS | Feasibility cut: the capacities must let year `y` meet its curtailment limits |
| `co2_budget_y{y}_{group}` | CO2-1 | Year subproblem: the year's emissions over its representative days, scaled to the year, within the budget plus `co2_budget_violation_y{y}_{group}` |
| `re_target_s{s}_y{y}`, `re_max_increment_s{s}_y{y}` | RE-2, RE-3b | Year subproblem: the RE target or floor (shortfall `slack_re_s{s}_y{y}`) and, with `max_re_increment` < 1, the growth cap measured from a copy of the master's share of the year before |
| `re_share_s{s}_y{y}` | BEN-RE | Year subproblem (growth cap only): the year's renewable generation + `re_share_short_s{s}_y{y}` (at VOLL) >= the master's `re_energy_ratio_s{s}_y{y}` (a copy) × the year's demand |
| `op_bound_y{y}` (with `op_bound_balance_*`, `op_bound_curt_s{σ}_y{y}`, `op_bound_sink_*`, `op_bound_*`) | BEN-BOUND | Master: `theta_y{y}` >= the cost of a copper-plate relaxation of year `y` (pooled units, one store per system, one balance per group of linked systems `op_bound_balance_y{y}_g{g}_d{d}_t{t}`, load shed at VOLL, the year's RE target and growth cap, each system's annual curtailment limit, supply above the load at most the lines' losses) |
| `max_curt_s{σ}_y{y}` | BEN-CURT | Year subproblem: each system's annual curtailment cap, as in the monolithic master; its slack `curt_excess_s{σ}_y{y}` costs VOLL per MWh (scaled to the year) in the subproblem whose cut bounds `theta_y{y}`, and a year that uses it beyond `curtailment_tolerance` (1e-3 MWh/yr + 1e-6 × the system's RE generation) is re-solved with the slacks' sum minimised: a violation left gives a feasibility cut and no upper bound, none a re-solve with the slacks held at that least violation |
| `tech_cap_t{t}_b{b}_ts{ts}`, `tech_seg_cap_t{t}_b{b}_k{k}_ts{ts}` | BEN-CAP | Subproblem: technology output and offer-curve segments at the fixed capacity; their duals form the cut's technology gradient |
| `copy_{kind}_{name}_{index} == value`, `fuel_cap_* == value` | BEN-COPY | Subproblem: a master quantity (converter, line, reservoir or fuel capacity built, life extension, an RE share) fixed to the master's value; the dual is its cut gradient |

The Benders master contains TECH, BUD, RET-2, CAP, TXN and the operating bound (BEN-BOUND), and nothing else: no constraint of its own links investment to the renewable energy, the CO2 budget or storage. Each year's subproblem is one LP with all its representative days and the year's annual constraints.

---

## primary_energy.jl --- Fuel Supply Chain

### Investment

The dispatch builds no fuel infrastructure. The master's build limits are `fuel_storage_inv_total_{f}_{n}`, `fuel_transport_inv_total_{f}_{r}` and `elz_inv_total_e{e}_b{b}` (`master_fuel.jl`, PE-5d).

### Periodic Supply, Transport and Demand

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `pp_max_supply_{f}_{n}_{p}` | PE-1 | Supply <= annual availability prorated to the period × disruption factor + excess slack |
| `pp_transport_cap_{f}_r{r}_{p}` | PE-2 | Transport capacity × the period's mean `fuel_route` outage availability |
| `pp_ne_max_{f}_{s}_{n}_{p}`, `pp_ne_satisfy_{f}_{s}_{n}_{p}` | PE-6 | Non-electric demand |

A period's shipment `T[r, p]` leaves its origin evenly over the period's steps and reaches the destination tank `τ` steps later, times `(1 − losses_fraction * distance_km / 100)` (PE-3, PE-3b); these arrivals and departures enter the tank balance below. In rolling-horizon dispatch, fuel shipped in a window's committed steps that arrives after them is a fixed arrival of the next window (`scheduled_arrivals`). There are no periodic storage levels.

### Tanks and Hourly Use

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `hr_storage_initial_{f}_{n}` | PE-H1 | Tank initial level |
| `hr_storage_cyclic_{f}_{n}` | PE-H1 | In the master's representative days: the tank ends each period at its starting level (no initial level) |
| `hr_storage_balance_{f}_{n}_{t}` | PE-4 | `V[t+1] = V[t] + eta * (local supply + arrivals [+ electrolyzer H2 for Hydrogen]) - drawn - departures` (no loss on V) |
| `hr_storage_dispatch_rate_*` | PE-H3 | Drawn <= rate × Δt × capacity (rate per hour; only at nodes with a tank) |
| `hr_storage_min_*`, `hr_storage_max_*`, `hr_storage_min_final_*` | PE-5 | Tank level bounds (after each step; minimum also at period ends, and not during a tank outage). The maximum is 0 at a node without a tank |
| `hr_storage_outage_lo_{f}_{n}_{t}`, `hr_storage_outage_hi_{f}_{n}_{t}` | PE-5d | `fuel_storage` outage: the locked share of the level at the outage start stays, only the available share of the capacity is used |
| `hr_storage_min_last_{f}_{n}` | PE-5 | Development mode: the window ends with the tank >= 10 % of capacity (no cyclic storage) |
| `hr_demand_coverage_{f}_{n}_{t}` | PE-5e | Drawn + shortfall >= fuel for power + non-electric use |
| `hr_ne_limit_{f}_{s}_{n}_{t}` | PE-H6 | Hourly non-electric use limit |
| `hr_emissions_*`, `hr_emissions_direct_*`, `hr_total_emissions_{n}_{t}` | PE-EM | Emissions |
| `link_supply_storage_in_*`, `link_ne_*` | PE-LINK | A period's local supply and non-electric use add up over its steps |
| `pe_coupling_gen{g}_n{n}_{t}` | PE-8 | `Σ_b∈n gen_output[g, b, t] * Δt <= fuel_for_power[n, t] * mwhe_per_unit` (inequality, per unit and node) |
| `pe_coupling_tech{k}_n{n}_{t}` | PE-8 | The same for candidate technology k in the master: `Σ_b∈n tech_output[k, b, t] * Δt <= fuel_for_power[n, t] * mwhe_per_unit` |

The dispatch electrolyzers (ELZ-1/ELZ-2 in `power_system.jl`) have no constraint of their own here: their hydrogen is a term of the `Hydrogen` tank balance (PE-H2). `electrolyzer.jl` defines its own electrolyzer constraints, which the runner does not use.

---

## mga.jl --- MGA and SPORES

### Cost Slack Constraint

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `total_cost <= (1 + slack) * optimal_cost` | MGA-1 | Near-optimal cost slack: total system cost must be within `slack_fraction` of the cost-optimal objective. Shared by both methods |

### MGA — Classical Hop-Skip-Jump diversity objective

The HSJ objective replaces the cost objective after the cost-optimal solution is found. Frequency-based scoring assigns scores based on how often each investment variable has been selected in previous alternatives.

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `max sum(score_k * x_k / x_max_k)` | MGA-2 | Maximize weighted diversity: `score = 1 - 2 * frequency`, where frequency is the fraction of previous alternatives that invested in variable `k`. Used by `run_mga_spores` and reusable inside a SPORES sweep when `:hsj_diversity` is in the objective list |

### SPORES — Per-objective sweep

Each SPORES objective replaces the cost objective (and any previous SPORES objective's aux vars / constraints, via `_clear_spores_aux!`) under the same cost-slack envelope (MGA-1). All formulations are LP — the L1 distance in SPORES-4 is linearised with positive / negative deviation aux variables.

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `min sum(I)` | SPORES-1 | Minimum total build: $\min \sum_{y,t,n} I^{tech} + \sum_{y,b,n} I^{bat,P} + \sum_{y,(i,j)} I^{tr}$. No auxiliary variables. Implemented by `set_min_build_objective!` |
| `min M, sum(I_t/I_max) <= M ∀ t` | SPORES-2 | Technology equity (min-max over per-tech totals). Adds 1 auxiliary scalar $M$ and $|\mathcal T|$ constraints. Implemented by `set_tech_equity_objective!` |
| `min M, sum(I_n/I_max) <= M ∀ n` | SPORES-3 | Regional equity (min-max over per-node totals). Adds 1 auxiliary scalar $M$ and $|\mathcal N|$ constraints. The spatially-explicit objective from which SPORES gets its name. Implemented by `set_regional_equity_objective!` |
| `I - I_ref = d_pos - d_neg; max sum((d_pos + d_neg)/I_max)` | SPORES-4 | Evolutionary distance (L1 from a reference solution, typically the cost-optimal). Adds 2 auxiliary variables and 1 constraint per technology and battery-power investment variable (battery energy and transmission are not included). Implemented by `set_evolutionary_distance_objective!` |

---

## transmission_acopf.jl --- AC Optimal Power Flow

### Voltage Constraints

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `soc_{l}_{t}` | AC-SOC | SOC relaxation as a rotated second-order cone: `w_i · w_j >= wr² + wi²` per branch |
| `psd_{t}` | AC-SDP | SDP relaxation: voltage matrix positive semidefinite |
| `qc_wr_lb_{l}_{t}`, `qc_wr_ub_{l}_{t}` | AC-QC1 | QC tighter bounds on wr using cos bounds |
| `qc_wi_lb_{l}_{t}`, `qc_wi_ub_{l}_{t}` | AC-QC2 | QC tighter bounds on wi using sin bounds |
| `qc_cos_env_{l}_{t}` | AC-QC3 | QC convex envelope for cos relaxation |
| `qc_angle_ub_{l}_{t}`, `qc_angle_lb_{l}_{t}` | AC-QC4 | QC angle bounds via tan(θ_max) |
| `angle_ub_{l}_{t}`, `angle_lb_{l}_{t}` | AC-ANG | Polar angle difference limits |
| `angle_ub_rect_{l}_{t}`, `angle_lb_rect_{l}_{t}` | AC-ANGR | Rectangular angle difference limits |
| `vm_lb_{i}_{t}`, `vm_ub_{i}_{t}` | AC-VM | Rectangular voltage magnitude bounds: `v_min² ≤ e² + f² ≤ v_max²` |

### Power Balance

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `kcl_p_{i}_{t}` | AC-P | Active power balance: `net_injection_MW = base_mva × Σ P_flow_pu` |
| `kcl_q_{i}_{t}` | AC-Q | Reactive power balance: `Q_gen - Q_load + Q_slack = base_mva × Σ Q_flow_pu` |
| `max_load_shed_acopf_b{b}_t{t}` | AC-LS | `load_shed <= max(bus demand, 0)` |
| `rooftop_curt_ub_n{ni}_{t}` | AC-RC | Rooftop curtailment limit |

### Line Limits

| Label Pattern | Family | Description |
|---------------|--------|-------------|
| `sline_from_{l}_{t}` | AC-SL1 | Apparent power from-side: second-order cone for SOC/QC/SDP, quadratic `P² + Q² ≤ cap²` for Polar/Rect |
| `sline_to_{l}_{t}` | AC-SL2 | Apparent power to-side (same form) |

---

## Constraint Count Summary

| Module | Approximate Constraint Families | Main Variables |
|--------|----------------------|-----------|
| `power_system.jl` | ~45 families + PWL segments | gen_output, gen_status, bat_charge, bat_discharge, bat_soc, reservoir_level, reservoir_pump, reservoir_spillage, load_shed, curtailment, reserves, ev_charging, ev_v2g, ev_soc, electrolyzer_power, gen_seg_output, bat_seg_discharge |
| `master_problem.jl` | ~25 families | tech_investment, bat_tech_power_investment, bat_tech_capacity_investment, transfer_investment, gen_life_extension, bat_life_extension, reservoir_investment, re_energy_ratio, slacks |
| `benders.jl` | ~4 families + reused master and dispatch families | theta, master and subproblem copies |
| `transmission_dc.jl` | ~10 families | power_flow_by_line, voltage_angle, ploss, converter flows |
| `primary_energy.jl` | ~30 families | fuel supply, transport, storage, hourly storage flows, fuel for power |
| `mga.jl` | ~6 families (MGA-1, MGA-2, SPORES-1..4) | reuses master-problem variables; SPORES-2/3 add 1 scalar each, SPORES-4 adds 2 per investment var |
| `transmission_acopf.jl` | ~10 families (SOC/SDP/QC/VM/ANG/KCL-P/KCL-Q/SL/LS) | w, wr, wi, vm, va, vr, vi_rect, q_gen, q_slack_pos, q_slack_neg |
