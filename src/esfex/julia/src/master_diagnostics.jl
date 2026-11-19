"""
master_diagnostics.jl - Why did the master build this? Binding constraints,
their duals and the paid slacks of a solved master problem.

After a master solve, `master_diagnostics` makes one pass over the model's
rows (and the upper bounds of its investment variables), groups them by
constraint family (the base-name prefix, `CONSTRAINT_FAMILIES`, with the
constraint catalog's codes) and per year, and returns per family the rows
that bind, their largest and summed |dual|, the top individual rows by
|dual|, and the slack and penalty variables the plan pays for. Only
aggregates and a few named rows cross to Python: never one record per row.

Duals are reported as the cost of tightening the constraint by one unit of
its right-hand side (for an equality row, of raising it): `-shadow_price`
of an inequality, ≥ 0 when it binds. Money is in \$ (the master's M\$ ×
`DIAG_MONEY`); a row tagged with a year (`_y{y}` in its name) is in that
year's own money: the objective's discount factor `(1 + r)^-(y-1)` is taken
off. A row paired with a slack (RE target and growth cap, curtailment cap,
budget, adequacy, CO2 budget) is measured per unit of its slack, so a
binding row priced at its penalty shows the slack's penalty.
"""

using JuMP: all_constraints, list_of_constraint_types, name, dual, has_duals
using JuMP: normalized_coefficient, objective_sense, coefficient
using JuMP: has_values, result_count, VariableRef, objective_function

"""\$ per unit of the master's costs (M\$)."""
const DIAG_MONEY = 1.0e6

"""
A constraint family: rows whose base name starts with `prefix`. `id` is the
family's machine name (one id may gather several prefixes, e.g. a build
limit as a row and as a variable bound), `code` the constraint catalog's
family, `units` the unit of its dual, `kind` one of "policy",
"build_limit", "operation" and "structural" (definitions and dynamics:
always tight, left out of the top rows and the digest), `slack` the base
name of the row's own slack variable (`""` without one: the row's suffix
after `prefix` names it) and `money_row` true for a row in money (its dual
is \$ per \$).
"""
struct ConstraintFamily
    prefix::String
    id::String
    code::String
    label::String
    units::String
    kind::String
    slack::String
    money_row::Bool
end

CF(prefix, id, code, label, units, kind; slack = "", money_row = false) =
    ConstraintFamily(prefix, id, code, label, units, kind, slack, money_row)

"""
The master's constraint families (rows), longest prefix first. Rows of
other names fall back to a generic family (their name without the index
tokens); unnamed rows (the power balance, line capacities…) are family
`unnamed`.
"""
const CONSTRAINT_FAMILIES = sort!([
    # Policy rows and their slacks
    CF("re_target", "re_target", "RE-2", "RE target", "\$/MWh", "policy"; slack = "slack_re"),
    CF("re_max_increment", "re_growth_cap", "RE-3b", "RE growth cap (max_re_increment)",
       "\$/MWh", "policy"; slack = "slack_re_cap"),
    CF("max_curtailment", "curtailment_cap", "CURT-1", "Curtailment cap", "\$/MWh", "policy";
       slack = "curtailment_excess"),
    CF("budget_y", "budget", "BUD-1", "Annual budget", "\$/\$", "policy";
       slack = "slack_budget_y", money_row = true),
    CF("cap_adequacy", "adequacy", "CAP-1", "Capacity adequacy (reserve margin)", "\$/MW",
       "policy"; slack = "slack_cap"),
    CF("cap_import_max", "adequacy_import", "CAP-3", "Adequacy import limit", "\$/MW", "policy"),
    CF("bat_tech_firm_power", "adequacy_storage_firm", "CAP-5b",
       "Storage firm capacity", "\$/MW", "policy"),
    CF("bat_tech_firm_energy", "adequacy_storage_firm", "CAP-5b",
       "Storage firm capacity", "\$/MW", "policy"),
    CF("co2_budget", "co2_budget", "CO2-1", "CO2 budget", "\$/t", "policy";
       slack = "co2_budget_violation"),
    # Build limits: rows over the horizon, and variable bounds (below)
    CF("cumul_tech", "tech_build_limit", "TECH-1", "Technology build limit", "\$/MW", "build_limit"),
    CF("cumul_node_tech", "tech_node_build_limit", "TECH-4", "Technology build limit (node)",
       "\$/MW", "build_limit"),
    CF("cumul_bat_tech_pow", "storage_power_build_limit", "TECH-2", "Storage power build limit",
       "\$/MW", "build_limit"),
    CF("cumul_bat_tech_cap", "storage_energy_build_limit", "TECH-2", "Storage energy build limit",
       "\$/MWh", "build_limit"),
    CF("cumul_node_bat_tech_pow", "storage_power_node_build_limit", "TECH-4",
       "Storage power build limit (node)", "\$/MW", "build_limit"),
    CF("cumul_node_bat_tech_cap", "storage_energy_node_build_limit", "TECH-4",
       "Storage energy build limit (node)", "\$/MWh", "build_limit"),
    CF("bat_tech_min_dur", "storage_duration", "TECH-3", "Storage duration (E/P) bounds",
       "\$/MWh", "build_limit"),
    CF("bat_tech_max_dur", "storage_duration", "TECH-3", "Storage duration (E/P) bounds",
       "\$/MWh", "build_limit"),
    CF("cumul_res", "reservoir_build_limit", "TECH-5", "Reservoir build limit", "\$/MWh",
       "build_limit"),
    CF("cumul_conv", "converter_build_limit", "", "Converter build limit", "\$/MW", "build_limit"),
    CF("elz_inv_total", "electrolyzer_build_limit", "", "Electrolyzer build limit", "\$/MW",
       "build_limit"),
    CF("fuel_storage_inv_total", "fuel_storage_build_limit", "", "Fuel storage build limit",
       "\$/unit", "build_limit"),
    CF("fuel_transport_inv_total", "fuel_transport_build_limit", "",
       "Fuel transport build limit", "\$/unit", "build_limit"),
    CF("cascade_gen", "retirement_cascade", "RET-2", "Retirement cascade", "\$/MW", "build_limit"),
    CF("cascade_bat", "retirement_cascade", "RET-2", "Retirement cascade", "\$/MW", "build_limit"),
    # Operation on the representative days / TSAM periods
    CF("tech_cap", "tech_capacity", "OP-1", "Technology capacity (hourly)", "\$/MW", "operation"),
    CF("tech_seg_cap", "tech_capacity", "OP-1b", "Technology capacity (hourly)", "\$/MW",
       "operation"),
    CF("bt_dis_cap", "storage_power", "OP-2a", "Storage power (hourly)", "\$/MW", "operation"),
    CF("bt_ch_cap", "storage_power", "OP-2a", "Storage power (hourly)", "\$/MW", "operation"),
    CF("bt_soc_cap", "storage_energy", "OP-2a", "Storage energy (hourly)", "\$/MWh", "operation"),
    CF("max_load_shed", "load_shed_limit", "PB-1b", "Load shed at most the demand", "\$/MW",
       "operation"),
    CF("reserve_headroom", "reserve_headroom", "RES-3", "Reserve headroom", "\$/MW", "operation"),
    CF("reserve_static", "reserve_static", "RES-1", "Static reserve requirement", "\$/MW",
       "operation"),
    CF("reserve_dynamic_req", "reserve_dynamic", "RES-2b", "Dynamic reserve",
       "\$/MW", "operation"),
    CF("reserve_dynamic_avail", "reserve_dynamic", "RES-2a", "Dynamic reserve",
       "\$/MW", "operation"),
    CF("inertia", "inertia", "INE-1", "Inertia floor", "\$/MWs", "operation"),
    CF("lol_sec_ub", "sector_load_shed_limit", "OP-5", "Sectoral load shed limit", "\$/MW",
       "operation"),
    CF("soc_inter", "storage_inter_day", "TSAM-CAP", "Storage inter-day level", "\$/MWh",
       "operation"),
    CF("reslvl_inter", "reservoir_inter_day", "TSAM-CAP", "Reservoir inter-day level",
       "\$/MWh", "operation"),
    CF("res_max_level", "reservoir_level", "RES-3", "Reservoir level bounds", "\$/MWh", "operation"),
    CF("res_min_level", "reservoir_level", "RES-3", "Reservoir level bounds", "\$/MWh", "operation"),
    CF("res_min_release", "reservoir_min_release", "RES-7", "Reservoir minimum release",
       "\$/MW", "operation"),
    CF("pe_coupling", "fuel_coupling", "PE-8", "Fuel for power", "\$/MWh", "operation"),
    CF("pp_max_supply", "fuel_supply", "PE-1", "Fuel supply limit", "\$/unit", "operation"),
    CF("pp_transport_cap", "fuel_transport", "PE-2", "Fuel transport capacity", "\$/unit",
       "operation"),
    CF("hr_storage_max", "fuel_tank", "PE-5", "Fuel tank capacity", "\$/unit", "operation"),
    # Structural: definitions, dynamics, symmetry (always tight)
    CF("def_", "capacity_state", "CUM-5", "Capacity state definitions", "\$/MW", "structural"),
    CF("trans_sym", "transmission_symmetry", "TXN-1", "Transmission symmetry", "\$/MW",
       "structural"),
    CF("bt_soc_init", "storage_soc", "OP-2b", "Storage SOC (initial, dynamics, cyclic)",
       "\$/MWh", "structural"),
    CF("bt_soc_dyn", "storage_soc", "OP-2b", "Storage SOC (initial, dynamics, cyclic)",
       "\$/MWh", "structural"),
    CF("bt_soc_cyc", "storage_soc", "OP-2b", "Storage SOC (initial, dynamics, cyclic)",
       "\$/MWh", "structural"),
    CF("bt_soc_zero", "storage_zero", "OP-2c", "Storage fixed to 0", "\$/unit", "structural"),
    CF("bt_dis_zero", "storage_zero", "OP-2c", "Storage fixed to 0", "\$/unit", "structural"),
    CF("bt_ch_zero", "storage_zero", "OP-2c", "Storage fixed to 0", "\$/unit", "structural"),
    CF("bat_soc", "battery_soc", "BAT-M", "Battery SOC links", "\$/MWh", "structural"),
    CF("lol_sec_sum", "sector_load_shed_split", "OP-5", "Sectoral load shed split", "\$/MW",
       "structural"),
    CF("lol_sec_residual", "sector_load_shed_split", "OP-5", "Sectoral load shed split", "\$/MW",
       "structural"),
    CF("reserve_static_held", "reserve_share", "RES-1", "Reserve held per node", "\$/MW",
       "structural"),
    CF("reserve_dynamic_held", "reserve_share", "RES-2b", "Reserve held per node", "\$/MW",
       "structural"),
    CF("reserve_dynamic_zero", "reserve_share", "RES-2c", "Reserve held per node", "\$/MW",
       "structural"),
    CF("flow_angle", "flow_angle", "DC-2", "Flow-angle coupling", "\$/MW", "structural"),
    CF("flow_zero", "flow_angle", "DC-2z", "Flow-angle coupling", "\$/MW", "structural"),
    CF("res_dynamics", "reservoir_dynamics", "RES-2", "Reservoir water balance", "\$/MWh",
       "structural"),
    CF("hr_storage_balance", "fuel_tank_balance", "PE-4", "Fuel tank balance", "\$/unit",
       "structural"),
], by = f -> -length(f.prefix))

"""
Upper bounds of the master's investment variables that act as build limits
(a one-year master holds its limit as the bound as well as the row, and
either may carry the dual), by variable base-name prefix.
"""
const BOUND_FAMILIES = sort!([
    CF("tech_inv", "tech_build_limit", "TECH-1", "Technology build limit", "\$/MW", "build_limit"),
    CF("bat_tech_pow_inv", "storage_power_build_limit", "TECH-2", "Storage power build limit",
       "\$/MW", "build_limit"),
    CF("bat_tech_cap_inv", "storage_energy_build_limit", "TECH-2", "Storage energy build limit",
       "\$/MWh", "build_limit"),
    CF("trans_inv", "transmission_build_limit", "", "Transmission build limit", "\$/MW",
       "build_limit"),
    CF("res_inv", "reservoir_build_limit", "TECH-5", "Reservoir build limit", "\$/MWh",
       "build_limit"),
    CF("conv_inv", "converter_build_limit", "", "Converter build limit", "\$/MW", "build_limit"),
    CF("elz_inv", "electrolyzer_build_limit", "", "Electrolyzer build limit", "\$/MW",
       "build_limit"),
    CF("fuel_storage_inv", "fuel_storage_build_limit", "", "Fuel storage build limit", "\$/unit",
       "build_limit"),
    CF("fuel_transport_inv", "fuel_transport_build_limit", "", "Fuel transport build limit",
       "\$/unit", "build_limit"),
    CF("gen_life_ext", "life_extension_limit", "RET-1", "Life extension up to the rating",
       "\$/MW", "build_limit"),
    CF("bat_life_ext", "life_extension_limit", "RET-1", "Life extension up to the rating",
       "\$/MW", "build_limit"),
], by = f -> -length(f.prefix))

const UNNAMED_FAMILY = CF("", "unnamed", "", "Unnamed rows (power balance, line capacity…)",
                          "\$/unit", "structural")

# Prefix tables keyed by the name's first token, for a cheap lookup per row
function family_index(families)
    idx = Dict{String, Vector{ConstraintFamily}}()
    for f in families
        tok = first(split(f.prefix, '_'; limit = 2))
        push!(get!(() -> ConstraintFamily[], idx, String(tok)), f)
    end
    return idx
end
const CONSTRAINT_FAMILY_INDEX = family_index(CONSTRAINT_FAMILIES)
const BOUND_FAMILY_INDEX = family_index(BOUND_FAMILIES)

# Index tokens dropped from an unknown name to form its family:
# s1, y2, b3, t4, ts5, d0, n1, node2, sc1, bt1, g1, l2, k1, e1, p3, r1, tau1, iter4, 7
const INDEX_TOKEN = r"^(sc|s|y|b|t|ts|d|n|node|bt|bi|g|l|k|e|p|r|c|tau|iter)?\d+$"

"""
    constraint_family(name, index; user_names) -> ConstraintFamily or nothing

The family of a row (or variable bound) named `name`: the longest matching
prefix in `index`; a user constraint's own family; else, for rows,
a generic family (the name without its index tokens). `nothing` for a
variable bound outside `index`.
"""
function constraint_family(nm::AbstractString, index::Dict{String, Vector{ConstraintFamily}};
                           user_names = nothing, generic::Bool = true)
    isempty(nm) && return generic ? UNNAMED_FAMILY : nothing
    if user_names !== nothing && nm in user_names
        return CF(nm, "user:" * nm, "USER", "User constraint $(nm)", "\$/unit", "policy")
    end
    cut = findfirst('_', nm)
    tok = cut === nothing ? nm : SubString(nm, 1, cut - 1)
    for f in get(index, tok, ConstraintFamily[])
        startswith(nm, f.prefix) && return f
    end
    generic || return nothing
    base = replace(nm, r"\[.*$" => "")
    toks = [t for t in split(base, '_') if !occursin(INDEX_TOKEN, t)]
    id = isempty(toks) ? base : join(toks, "_")
    return CF(id, id, "", id, "\$/unit", "other")
end

"""
    name_year(name) -> Int

The year index a row's name is tagged with (`_y{y}` followed by `_`, `[` or
the end), 0 without one.
"""
function name_year(nm::AbstractString)::Int
    m = match(r"_y(\d+)(?=_|\[|$)", nm)
    return m === nothing ? 0 : parse(Int, m.captures[1])
end

mutable struct FamilyStats
    family::ConstraintFamily
    count_total::Int
    count_binding::Int
    count_priced::Int
    max_abs_dual::Float64
    sum_abs_dual::Float64
    top::Vector{Tuple{Float64, String, Float64}}   # (|dual|, name, dual), largest first
end
FamilyStats(f::ConstraintFamily) = FamilyStats(f, 0, 0, 0, 0.0, 0.0, Tuple{Float64,String,Float64}[])

function keep_top!(top::Vector{Tuple{Float64,String,Float64}}, k::Int, a::Float64,
                   nm::String, d::Float64)
    k > 0 || return
    length(top) >= k && a <= top[end][1] && return
    # A name repeated on every representative day is shown once
    j = findfirst(r -> r[2] == nm, top)
    if j !== nothing
        a <= top[j][1] && return
        deleteat!(top, j)
    end
    i = searchsortedfirst(top, (a, nm, d); by = first, rev = true)
    insert!(top, i, (a, nm, d))
    length(top) > k && pop!(top)
    return
end

# The slack variables of the master's policy rows, by name, to measure a
# row per unit of its slack
function master_slack_vars(model::Model, vars::MasterProblemVariables)
    out = Dict{String, VariableRef}()
    add!(v) = (out[name(v)] = v)
    foreach(add!, values(vars.slack_budget))
    foreach(add!, values(vars.slack_capacity))
    foreach(add!, values(vars.slack_re_target))
    foreach(add!, values(vars.co2_budget_violation))
    foreach(add!, values(get(model.ext, :re_cap_slack, Dict{Tuple{Int,Int,Int}, VariableRef}())))
    for e in get(model.ext, :curtailment_excess, CurtailmentExcess[])
        add!(e.excess)
    end
    return out
end

# Sense of a set: -1 for ≤, +1 for ≥, 0 for = (and intervals)
set_sense(::Type{<:MOI.LessThan}) = -1
set_sense(::Type{<:MOI.GreaterThan}) = 1
set_sense(::Type) = 0

"""
    master_diagnostics(model, vars, input; top_n=20, per_family_top=5,
                       tol=1e-6, dual_tol=1e-9) -> NamedTuple

Binding constraints, duals and paid slacks of a solved master, per year
index (`input.years[y]`; 0: rows with no year in their name in a master of
several years, a single-year master puts every row in its year):

- `duals_available`: whether the solver returned duals (an LP; a MIP has
  none, and its binding counts come from the tight rows alone);
- `binding`: per year and family, the rows counted (`count_total`), those
  that bind (`count_binding`: an inequality at its bound within
  `tol × max(1, |rhs|)` or any row with |dual| > `dual_tol`; an equality
  only by its dual), those with a dual (`count_priced`), and the largest
  and summed |dual| (\$ per unit, `units`; NaN without duals);
- `top`: per year, the `top_n` rows of the largest |dual| outside the
  structural families, at most `per_family_top` per family;
- `slacks`: per year, the slack and penalty variables the plan pays for
  (value > `tol`): the annual slacks one row each (budget overrun in \$,
  adequacy shortfall in MW, RE shortfall and growth-cap excess in MWh/yr,
  curtailment excess in MWh/yr, CO2 violation in t), the representative
  days' load shed, reserve and inertia shortfalls one row per kind and
  year, scaled to the year (MWh/yr); `unit_cost` and `cost` in the year's
  own \$ as the objective charges them.

One pass over the rows; only aggregates and the named top rows are kept.
"""
function master_diagnostics(model::Model, vars::MasterProblemVariables,
                            input::MasterProblemInput;
                            top_n::Int = 20, per_family_top::Int = 5,
                            tol::Float64 = 1e-6, dual_tol::Float64 = 1e-9)
    num_years = length(input.years)
    duals = has_duals(model)
    values_ok = has_values(model)
    sgn = objective_sense(model) == MAX_SENSE ? -1.0 : 1.0
    df(y) = y >= 1 ? (1.0 + input.discount_rate)^-(y - 1) : 1.0
    year_of(nm) = (y = name_year(nm); num_years == 1 ? 1 : (1 <= y <= num_years ? y : 0))
    user_names = get(model.ext, :user_constraint_names, nothing)
    slack_vars = master_slack_vars(model, vars)
    budget_label = input.budget_total ? "Annual budget (investment + operation)" :
                                        "Annual budget (investment)"

    stats = Dict{Tuple{Int, String}, FamilyStats}()
    function record!(f::ConstraintFamily, y::Int, nm::String, d::Float64, tight::Bool)
        st = get!(() -> FamilyStats(f.id == "budget" ? CF(f.prefix, f.id, f.code, budget_label,
                                                          f.units, f.kind; slack = f.slack,
                                                          money_row = true) : f),
                  stats, (y, f.id))
        st.count_total += 1
        a = abs(d)
        priced = duals && a > dual_tol
        st.count_priced += priced
        st.count_binding += (priced || tight)
        if duals
            st.max_abs_dual = max(st.max_abs_dual, a)
            st.sum_abs_dual += a
            priced && f.kind != "structural" && keep_top!(st.top, per_family_top, a, nm, d)
        end
        return
    end

    for (F, S) in list_of_constraint_types(model)
        bound = F == VariableRef
        (bound || F <: JuMP.GenericAffExpr || F <: JuMP.GenericQuadExpr) || continue
        sense = set_sense(S)
        # Variable bounds: only the upper bounds of investment variables
        bound && sense != -1 && continue
        for c in all_constraints(model, F, S)
            # A bound's index is its variable's (MOI convention)
            nm = bound ? name(VariableRef(model, MOI.VariableIndex(JuMP.index(c).value))) :
                         name(c)
            f = bound ? constraint_family(nm, BOUND_FAMILY_INDEX; generic = false) :
                        constraint_family(nm, CONSTRAINT_FAMILY_INDEX; user_names)
            f === nothing && continue
            y = year_of(nm)
            d = duals ? dual(c) : 0.0
            # Cost of tightening by one unit (an equality: of raising it)
            t = sgn * (sense == -1 ? -d : d)
            if duals && !isempty(f.slack)
                sv = get(slack_vars, f.slack * SubString(nm, ncodeunits(f.prefix) + 1), nothing)
                if sv !== nothing
                    coef = normalized_coefficient(c, sv)
                    coef != 0 && (t *= abs(coef))
                end
            end
            t *= (f.money_row ? 1.0 : DIAG_MONEY) / df(y)
            tight = false
            if values_ok && sense != 0
                rhs = MOI.constant(MOI.get(model, MOI.ConstraintSet(), c))
                tight = abs(JuMP.value(c) - rhs) <= tol * max(1.0, abs(rhs))
            end
            record!(f, y, nm, t, tight)
        end
    end

    binding = [(year = y, family = st.family.id, code = st.family.code,
                label = st.family.label, kind = st.family.kind, units = st.family.units,
                count_binding = st.count_binding, count_priced = st.count_priced,
                count_total = st.count_total,
                max_abs_dual = duals ? st.max_abs_dual : NaN,
                sum_abs_dual = duals ? st.sum_abs_dual : NaN)
               for ((y, _), st) in sort!(collect(stats); by = first)]

    top = NamedTuple{(:year, :name, :family, :label, :dual, :units),
                     Tuple{Int, String, String, String, Float64, String}}[]
    for y in sort!(unique(first.(keys(stats))))
        rows = [(a, nm, d, st.family) for ((yy, _), st) in stats if yy == y
                for (a, nm, d) in st.top]
        sort!(rows; by = first, rev = true)
        for (_, nm, d, f) in rows[1:min(top_n, length(rows))]
            push!(top, (year = y, name = nm, family = f.id, label = f.label, dual = d,
                        units = f.units))
        end
    end

    return (duals_available = duals, status = string(JuMP.termination_status(model)),
            binding = binding, top = top,
            slacks = values_ok ? master_paid_slacks(model, vars, input; tol) :
                                 master_slack_row[])
end

const master_slack_row = NamedTuple{
    (:year, :name, :family, :label, :value, :value_units, :unit_cost, :cost, :count),
    Tuple{Int, String, String, String, Float64, String, Float64, Float64, Int}}

"""
    master_paid_slacks(model, vars, input; tol=1e-6) -> Vector{NamedTuple}

The slack and penalty variables a solved master pays for (`master_diagnostics`):
year index, name, family, label, value (`value_units`), the price per unit
and the cost in the year's own \$ as the objective charges them, and the
number of variables the row sums (1 for an annual slack).
"""
function master_paid_slacks(model::Model, vars::MasterProblemVariables,
                            input::MasterProblemInput; tol::Float64 = 1e-6)
    num_years = length(input.years)
    obj = objective_function(model)
    aff = obj isa JuMP.GenericQuadExpr ? obj.aff : obj
    df(y) = 1 <= y <= num_years ? (1.0 + input.discount_rate)^-(y - 1) : 1.0
    out = master_slack_row[]
    # Annual slacks: one row each; `scale` turns the value into its unit
    function annual!(v::VariableRef, y::Int, family, label, units; scale = 1.0)
        x = JuMP.value(v)
        x > tol || return
        cost = coefficient(aff, v) * x * DIAG_MONEY / df(y)
        val = x * scale
        push!(out, (year = num_years == 1 ? 1 : y, name = name(v), family = family,
                    label = label, value = val, value_units = units,
                    unit_cost = cost / val, cost = cost, count = 1))
        return
    end
    for (y, v) in vars.slack_budget
        annual!(v, y, "budget", "Budget overrun", "\$"; scale = DIAG_MONEY)
    end
    for ((y, _), v) in vars.slack_capacity
        annual!(v, y, "adequacy", "Capacity adequacy shortfall", "MW")
    end
    for ((_, _, y), v) in vars.slack_re_target
        annual!(v, y, "re_target", "RE target shortfall", "MWh/yr")
    end
    for ((_, _, y), v) in get(model.ext, :re_cap_slack, Dict{Tuple{Int,Int,Int}, VariableRef}())
        annual!(v, y, "re_growth_cap", "RE above the growth cap", "MWh/yr")
    end
    for e in get(model.ext, :curtailment_excess, CurtailmentExcess[])
        y = something(findfirst(==(e.year), input.years), 0)
        annual!(e.excess, y, "curtailment_cap", "Curtailment above the cap", "MWh/yr")
    end
    for ((_, y, _), v) in vars.co2_budget_violation
        annual!(v, y, "co2_budget", "CO2 budget violation", "t")
    end
    # Representative days: one row per kind, year and scenario, scaled to the year
    acc = Dict{Tuple{Int, Int, String}, Vector{Float64}}()   # value, cost, count
    labels = Dict{String, Tuple{String, String}}()
    function day!(xs, sc, y, scale, family, label, units)
        xs === nothing && return
        labels[family] = (label, units)
        a = get!(() -> zeros(3), acc, (sc, y, family))
        for v in xs
            x = JuMP.value(v)
            x > tol || continue
            a[1] += x * scale
            a[2] += coefficient(aff, v) * x * DIAG_MONEY / df(y)
            a[3] += 1
        end
        return
    end
    for d in get(model.ext, :master_days, Any[])
        p = d.ps_vars
        day!(p.load_shed, d.scenario_idx, d.year_idx, d.scale, "load_shed",
             "Load shed (representative days)", "MWh/yr")
        day!(p.reserve_static_loss, d.scenario_idx, d.year_idx, d.scale,
             "reserve_static_shortfall", "Static reserve shortfall", "MWh/yr")
        day!(p.reserve_dynamic_loss, d.scenario_idx, d.year_idx, d.scale,
             "reserve_dynamic_shortfall", "Dynamic reserve shortfall", "MWh/yr")
        day!(p.loss_of_inertia, d.scenario_idx, d.year_idx, d.scale,
             "inertia_shortfall", "Inertia shortfall", "MWs·h/yr")
        if p.loss_of_load_sectoral !== nothing
            for (sector, xs) in p.loss_of_load_sectoral
                day!(xs, d.scenario_idx, d.year_idx, d.scale, "load_shed_sector_$(sector)",
                     "Load shed of sector $(sector) (part of the load shed)", "MWh/yr")
            end
        end
    end
    for ((sc, y, family), (val, cost, n)) in sort!(collect(acc); by = first)
        n > 0 || continue
        label, units = labels[family]
        tag = sc > 0 ? "sc$(sc)_" : ""
        push!(out, (year = num_years == 1 ? 1 : y, name = "$(tag)op_y$(y)_d*_$(family)",
                    family = family, label = label, value = val, value_units = units,
                    unit_cost = val > 0 ? cost / val : 0.0, cost = cost, count = Int(n)))
    end
    sort!(out; by = r -> (r.year, -r.cost))
    return out
end
