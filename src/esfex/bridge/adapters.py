"""
Adapter classes for Python↔Julia optimization models.

Provides facade classes that present a Python interface while
delegating to Julia implementations.
"""

import logging
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

import numpy as np

from esfex.utils.temporal import HOURS_STD_YEAR, aggregate_to_resolution
from esfex.utils.per_unit import system_s_base
from esfex.config.schema import (
    BatteryConfig,
    GeneratorConfig,
    N1SecurityConfig,
    NodeConfig,
    ESFEXConfig,
    SolverConfig,
    SystemConfig,
    TemporalConfig,
)

if TYPE_CHECKING:
    from esfex.topology import ReductionMap, ResultExpander
from esfex.bridge.converters import (
    COST_SCALE,
    COST_UNSCALE,
    build_bat_cost_curves_dict,
    build_gen_cost_curves_dict,
    build_outage_mask_by_id,
    build_outage_mask_matrix,
    compile_outage_factor,
    absolute_start_hour,
    battery_availability_key,
    battery_technology_availability_key,
    fuel_source_outage_windows,
    outage_window_spans,
    outage_window_tuple,
    convert_battery_config,
    convert_battery_technology_config,
    convert_generator_config,
    convert_network_config,
    convert_power_system_result,
    convert_technology_config,
    convert_temporal_config,
    expand_node_to_bus_array,
    expand_node_to_bus_matrix,
    py_to_julia_matrix,
    py_to_julia_vector,
    py_to_julia_int_vector,
    scale_cost,
    scale_cost_list,
)
from esfex.bridge.julia_setup import get_julia, get_esfex_module
from esfex.io.demand import load_availability_profile, load_inflow_profile


logger = logging.getLogger(__name__)


# Warnings that would repeat for every rolling window (and every master
# solve) are logged once per run: the runner calls reset_run_warnings() when
# a run starts.
_RUN_WARNINGS: set = set()


def reset_run_warnings() -> None:
    """Forget the warnings logged so far, so a new run logs them again."""
    _RUN_WARNINGS.clear()


def _warn_once(key, msg: str, *args) -> None:
    """``logger.warning(msg, *args)`` the first time ``key`` is seen in a run."""
    if key in _RUN_WARNINGS:
        return
    _RUN_WARNINGS.add(key)
    logger.warning(msg, *args)


# Values of a controlling combo (HiGHS "solver_method", Gurobi "method") that
# mean "let the solver decide" — no specific algorithm is pinned, so no
# algorithm-specific option should be forced.
_METHOD_AUTO_SENTINELS = frozenset({"choose", "auto"})


def _method_gated_drops(options: dict, solver_name: str = "highs") -> set[str]:
    """Identifiers of solver options to drop because they are method-specific
    but the controlling combo is on auto/'choose' (or a different method).

    Some options (e.g. HiGHS ``pdlp_scaling``) only apply when an LP-method
    combo (``solver_method``) is set to a specific algorithm. The GUI collects
    them under ``solver_method='choose'`` too (their ``enabled_when`` lists
    ``choose``), so a saved config carries them even when no method is pinned.
    Forwarding such an option makes the solver reject an inapplicable
    parameter — on some HiGHS builds ``pdlp_scaling`` raises
    ``UnsupportedAttribute`` and aborts the *entire* model build. Only forward
    a method-gated option when its controller is explicitly set to a matching,
    non-auto method.

    Pure (no Julia) so it can be unit-tested directly.
    """
    active = solver_name.lower()
    drops: set[str] = set()
    try:
        from esfex.config.solver import SOLVER_OPTIONS
    except Exception:
        return drops

    catalog = SOLVER_OPTIONS.get(active, [])
    # key ↔ attr map and each controlling combo's default, for this solver.
    key_to_attr: dict[str, str] = {}
    ctrl_default: dict[str, str] = {}
    for opt in catalog:
        key, attr = opt.get("key"), opt.get("attr", opt.get("key"))
        if key and attr and key != attr:
            key_to_attr[key] = attr
        if key:
            ctrl_default[key] = str(opt.get("default", "choose"))

    def _active_ctrl(ctrl_key: str) -> str:
        ctrl_attr = key_to_attr.get(ctrl_key, ctrl_key)
        for kk in (ctrl_key, ctrl_attr):
            if kk in options and options[kk] is not None:
                return str(options[kk])
        return ctrl_default.get(ctrl_key, "choose")

    for opt in catalog:
        gate = opt.get("enabled_when")
        if not gate:
            continue
        applies = True
        for ctrl_key, allowed in gate.items():
            allowed_set = {str(a) for a in allowed}
            active_val = _active_ctrl(ctrl_key)
            if active_val in _METHOD_AUTO_SENTINELS or active_val not in allowed_set:
                applies = False
                break
        if not applies:
            idents = [i for i in (opt.get("attr"), opt.get("key")) if i]
            # Only mark for dropping if the option is actually present in the
            # supplied config, so the result is exactly "what to strip here".
            if any(i in options for i in idents):
                drops.update(idents)
    return drops


def _nodes_without_load_bus(buses, num_nodes: int) -> list[int]:
    """Nodes (0-indexed) whose demand no bus of the dispatch carries.

    The dispatch puts a node's demand on its ``load`` and ``mixed`` buses,
    each taking its ``demand_fraction``; a node with no such bus (no bus at
    all, or only ``connection`` buses) has its demand in no power balance.
    """
    carried = np.zeros(num_nodes)
    for b in buses:
        if getattr(b, "role", "load") in ("load", "mixed") and 0 <= b.parent_node < num_nodes:
            carried[b.parent_node] += max(float(b.demand_fraction), 0.0)
    return [n for n in range(num_nodes) if carried[n] <= 1e-9]


def _solver_options_to_julia(options: dict, solver_name: str = "highs") -> Any:
    """Convert Python solver options dict to Julia Dict{String, Any}.

    Repairs legacy GUI configs in two ways:

    1. **Key remapping**: Older GUI builds saved options under their internal
       ``key`` (e.g. ``solver_method``) instead of the real solver attribute
       (``attr``, e.g. ``solver``). HiGHS rejects unknown keys.
    2. **Value normalization**: Combo options with a ``values`` array map
       label strings to integers (e.g. ``simplex_scale_strategy: "choose"``
       → ``1``). HiGHS rejects strings for those options.

    Both lookups are per-solver via SOLVER_OPTIONS metadata, because the
    same name can mean different things across solvers.
    """
    jl = get_julia()
    if not options:
        return jl.seval("Dict{String, Any}()")

    # Build per-solver maps: key → attr remap, and per-attr value normalization.
    # Also build a cross-solver index so we can drop options whose attr clearly
    # belongs to a different solver (e.g. Gurobi's "FeasibilityTol" leaking
    # into a HiGHS run would crash at set_optimizer time).
    active = solver_name.lower()
    key_to_attr: dict[str, str] = {}
    value_map: dict[str, dict[str, Any]] = {}
    known_attrs_active: set[str] = set()
    attr_owners: dict[str, set[str]] = {}  # attr/key → set of solvers that declare it
    try:
        from esfex.config.solver import SOLVER_OPTIONS
        for sname, sopts in SOLVER_OPTIONS.items():
            for opt in sopts:
                key = opt.get("key")
                attr = opt.get("attr", key)
                for ident in (key, attr):
                    if ident:
                        attr_owners.setdefault(ident, set()).add(sname)
                if sname == active:
                    if key and attr and key != attr:
                        key_to_attr[key] = attr
                    if attr:
                        known_attrs_active.add(attr)
                    if key:
                        known_attrs_active.add(key)
                    values = opt.get("values")
                    choices = opt.get("choices")
                    if values is not None and choices is not None and attr:
                        value_map[attr] = dict(zip(choices, values))
                        if key and key != attr:
                            value_map[key] = value_map[attr]
    except Exception:
        pass

    # Method-specific options whose controlling combo is on auto/'choose' (or a
    # different method) must not be forwarded — see _method_gated_drops.
    method_drops = _method_gated_drops(options, active)

    jl_dict = jl.seval("Dict{String, Any}()")
    for k, v in options.items():
        key_str = str(k)
        # Remap legacy GUI key → real solver attribute
        attr_str = key_to_attr.get(key_str, key_str)
        # Drop method-inapplicable options (e.g. PDLP flags while LP method is
        # left on "choose") before they reach — and are rejected by — the solver.
        if key_str in method_drops or attr_str in method_drops:
            _warn_once(
                ("solver_option_method", active, key_str),
                "Dropping solver option %r: it only applies to a specific "
                "LP/QP method that is not currently selected.",
                key_str,
            )
            continue
        # Drop options whose attr belongs exclusively to a different solver.
        # Unknown attrs (not in any solver catalog) pass through — they may be
        # valid solver parameters that simply aren't surfaced in the GUI.
        owners = attr_owners.get(attr_str) or attr_owners.get(key_str)
        if owners and active not in owners:
            _warn_once(
                ("solver_option_owner", active, key_str),
                "Dropping solver option %r: attribute belongs to %s, "
                "active solver is %r",
                key_str, "/".join(sorted(owners)), active,
            )
            continue
        # Repair label-as-string for combo-with-integer-values
        if attr_str in value_map and isinstance(v, str) and v in value_map[attr_str]:
            v = value_map[attr_str][v]
        jl.seval("setindex!")(jl_dict, v, attr_str)
    return jl_dict


# Custom-constraint variable axes that are referenced by NAME rather than a raw
# 1-based index. Maps variable → {axis position: ("kind", name→index map)}.
def _operational_axes(system_config):
    gen_idx = {k: i for i, k in enumerate(system_config.generators.keys(), 1)}
    bat_idx = {k: i for i, k in enumerate((system_config.batteries or {}).keys(), 1)}
    return {
        "gen_output": {0: ("generator", gen_idx)},
        "bat_charge": {0: ("battery", bat_idx)},
        "bat_discharge": {0: ("battery", bat_idx)},
        "bat_soc": {0: ("battery", bat_idx)},
    }


def _investment_axes(tech_idx, year_idx):
    # Investment vars index [year, technology, node]; year by value, tech by name.
    yr = ("year", year_idx)
    tech = ("technology", tech_idx)
    return {
        "tech_investment": {0: yr, 1: tech},
        "bat_tech_power_investment": {0: yr, 1: tech},
        "transfer_investment": {0: yr},
    }


def _lookup_axis_entry(kind, m, entry):
    """1-based index of ``entry`` on a named axis, or None if unknown.

    Names match as strings, so a YAML ``1`` finds the key ``"1"`` and
    ``"2030"`` finds the year 2030. On a unit or technology axis an integer
    that names no key is a 1-based position, like on the numeric axes.
    """
    for key in (entry, str(entry)):
        if key in m:
            return m[key]
    if kind == "year":
        try:
            return m.get(int(entry))
        except (TypeError, ValueError):
            return None
    if isinstance(entry, (int, np.integer)) and not isinstance(entry, bool) \
            and 1 <= int(entry) <= len(m):
        return int(entry)
    return None


def _resolve_one(cc, name_axes) -> dict:
    """Resolve a single constraint into a plain spec with 1-based Julia indices
    (``"all"`` → -1). Raises ValueError on an unknown name."""
    spec = {"name": cc.name, "type": cc.type, "sense": cc.sense,
            "rhs": float(cc.rhs), "target": cc.target}
    if cc.type != "linear":
        spec.update(dict(cc.params or {}))
        return spec
    terms = []
    for t in cc.terms:
        axes = name_axes.get(t.variable, {})
        resolved = []
        for pos, entry in enumerate(t.index):
            if entry == "all" or entry == -1:
                resolved.append(-1)
            elif pos in axes:
                kind, m = axes[pos]
                idx = _lookup_axis_entry(kind, m, entry)
                if idx is None:
                    raise ValueError(
                        f"custom constraint '{cc.name}': unknown {kind} "
                        f"'{entry}' for variable '{t.variable}'"
                    )
                resolved.append(idx)
            else:
                resolved.append(int(entry))
        terms.append({"variable": t.variable, "index": resolved,
                      "coefficient": float(t.coefficient)})
    spec["terms"] = terms
    return spec


def resolve_custom_constraints(system_config) -> list[dict]:
    """Resolve the *operational*-target custom constraints (generator/battery
    names → 1-based Julia indices). Pure Python; raises on unknown names."""
    ccs = getattr(system_config, "custom_constraints", None) or []
    axes = _operational_axes(system_config)
    return [_resolve_one(cc, axes) for cc in ccs
            if getattr(cc, "target", "operational") == "operational"]


def resolve_investment_constraints(custom_constraints, tech_idx, year_idx) -> list[dict]:
    """Resolve the *investment*-target custom constraints (technology names →
    1-based tech indices, year values → 1-based year indices)."""
    ccs = custom_constraints or []
    axes = _investment_axes(tech_idx, year_idx)
    return [_resolve_one(cc, axes) for cc in ccs
            if getattr(cc, "target", "operational") == "investment"]


def _custom_constraints_to_julia(specs: list[dict], jl) -> Any:
    """Build a Julia ``Vector{Any}`` of ``Dict{String,Any}`` from resolved specs
    (mirrors the seval/setindex! pattern used for solver options). The ``target``
    routing key is dropped — the Julia hooks don't need it."""
    setindex = jl.seval("setindex!")
    push = jl.seval("push!")
    jl_vec = jl.seval("Vector{Any}()")
    for spec in specs:
        jl_spec = jl.seval("Dict{String, Any}()")
        for k, v in spec.items():
            if k == "target":
                continue
            if k == "terms":
                jl_terms = jl.seval("Vector{Any}()")
                for term in v:
                    jl_term = jl.seval("Dict{String, Any}()")
                    setindex(jl_term, str(term["variable"]), "variable")
                    setindex(jl_term, float(term["coefficient"]), "coefficient")
                    jl_idx = jl.seval("Int[]")
                    for i in term["index"]:
                        push(jl_idx, int(i))
                    setindex(jl_term, jl_idx, "index")
                    push(jl_terms, jl_term)
                setindex(jl_spec, jl_terms, "terms")
            else:
                setindex(jl_spec, v, k)
        push(jl_vec, jl_spec)
    return jl_vec


# ─── Renewable fuel types (no transport cost adjustment) ───
_RENEWABLE_FUELS = {"Sun", "Wind", "Water", "OTEC", "None"}


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Haversine distance in km between two lat/lng points."""
    import math

    la1, lo1 = math.radians(lat1), math.radians(lng1)
    la2, lo2 = math.radians(lat2), math.radians(lng2)
    dlat, dlng = la2 - la1, lo2 - lo1
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(la1) * math.cos(la2) * math.sin(dlng / 2) ** 2
    )
    return 6371.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _py_nested_list_to_julia_vec_vec(jl: Any, nested: List[List], elem_type: str = "Int") -> Any:
    """Convert Python list[list[int|float]] to Julia Vector{Vector{T}}."""
    outer = jl.seval(f"Vector{{{elem_type}}}[]")
    for inner in nested:
        jl_inner = jl.seval(f"{elem_type}{list(inner)}")
        jl.seval("push!")(outer, jl_inner)
    return outer


def _pwl_segments_from_config(dc_config) -> int:
    """Map loss_model config to Julia pwl_loss_segments integer.

    Returns:
        N > 0: PWL with N segments
        -1: legacy linear loss model
        0: lossless (no losses)
    """
    model = getattr(dc_config, 'loss_model', 'pwl')
    if model == 'pwl':
        return int(getattr(dc_config, 'pwl_loss_segments', 3))
    elif model == 'linear':
        return -1
    else:  # "none"
        return 0


def _build_gen_q_limits(sys) -> "Any":
    """Build generator reactive power Q_max limits dict for Julia.

    Returns Julia Dict{Int64, Vector{Float64}} — gen_idx → [Q_max per node].
    Only populated for generators with explicit q_max_mvar specified.
    """
    jl = get_julia()
    result = {}
    for g_idx, (gen_key, gen) in enumerate(sys.generators.items(), start=1):
        q_max = getattr(gen, 'q_max_mvar', [])
        if q_max:
            result[g_idx] = [float(q) for q in q_max]
    if not result:
        return jl.seval("Dict{Int64, Vector{Float64}}()")
    pairs = []
    for k, v in result.items():
        vals = ", ".join(str(x) for x in v)
        pairs.append(f"{int(k)} => Float64[{vals}]")
    return jl.seval(f"Dict{{Int64, Vector{{Float64}}}}({', '.join(pairs)})")


def _build_gen_q_limits_min(sys) -> "Any":
    """Build generator reactive power Q_min limits dict for Julia."""
    jl = get_julia()
    result = {}
    for g_idx, (gen_key, gen) in enumerate(sys.generators.items(), start=1):
        q_min = getattr(gen, 'q_min_mvar', [])
        if q_min:
            result[g_idx] = [float(q) for q in q_min]
    if not result:
        return jl.seval("Dict{Int64, Vector{Float64}}()")
    pairs = []
    for k, v in result.items():
        vals = ", ".join(str(x) for x in v)
        pairs.append(f"{int(k)} => Float64[{vals}]")
    return jl.seval(f"Dict{{Int64, Vector{{Float64}}}}({', '.join(pairs)})")


def _build_reserve_requirement_dict(reserve_list) -> "Any":
    """Convert per-node reserve list (Python, 0-indexed) into Julia
    Dict{Int64, Float64} keyed by 1-indexed node id. Drop zeros so the LP
    falls back to default_ratio for those nodes.
    """
    jl = get_julia()
    if not reserve_list:
        return jl.seval("Dict{Int64, Float64}()")
    pairs = []
    for i, v in enumerate(reserve_list):
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0:
            pairs.append(f"{i + 1} => {f}")
    if not pairs:
        return jl.seval("Dict{Int64, Float64}()")
    return jl.seval(f"Dict{{Int64, Float64}}({', '.join(pairs)})")


def _pwl_segments_from_config_master(dc_config) -> int:
    """Map loss_model config to Julia transmission_loss_segments for master problem."""
    model = getattr(dc_config, 'loss_model', 'pwl')
    if model == 'pwl':
        return int(getattr(dc_config, 'pwl_loss_segments_master', 2))
    elif model == 'linear':
        return -1
    else:  # "none"
        return 0


# Per-bus fields of a virtual unit that hold capacity: summed when buses merge
_PER_BUS_CAPACITY_FIELDS = ("rated_power", "MaxChargePower", "MaxDischargePower", "capacity")


def _per_bus_unit_on_model_buses(
    vdata: Dict[str, Any],
    reduction_map: Optional["ReductionMap"],
    original_buses: list,
) -> Dict[str, Any]:
    """Lay a per-bus virtual unit out on a reduced dispatch network's buses.

    Units built from master investments (``_is_per_bus``) carry arrays over
    the system's own buses; with ``network_reduction`` the dispatch is built
    on the reduced network, whose bus ``k`` is original bus
    ``retained_original_indices[k]``, so the arrays are re-indexed (they were
    read as reduced-bus arrays, which put capacity on the wrong buses, even
    at another node). Capacity on an eliminated bus moves to the retained bus
    of the same node with the largest ``demand_fraction``; the other per-bus
    fields take the retained buses' values. Other units pass through.
    """
    rm = reduction_map
    n_orig = len(original_buses)
    rp = vdata.get("rated_power") or vdata.get("capacity") or []
    if (rm is None or not vdata.get("_is_per_bus") or not rm.n_original_buses
            or rm.n_original_buses != n_orig or len(rp) != n_orig):
        return vdata
    retained = list(rm.retained_original_indices)
    reduced_of = {o: r for r, o in enumerate(retained)}
    # Eliminated bus → reduced bus that takes its capacity
    target: Dict[int, Optional[int]] = {}
    for o in range(n_orig):
        if o in reduced_of:
            continue
        node = original_buses[o].parent_node
        same_node = [r for r, ob in enumerate(retained)
                     if original_buses[ob].parent_node == node]
        target[o] = max(same_node, default=None, key=lambda r: float(
            getattr(original_buses[retained[r]], "demand_fraction", 0.0) or 0.0))

    out = dict(vdata)
    for field_name, values in vdata.items():
        if not isinstance(values, list) or len(values) != n_orig:
            continue
        if field_name in _PER_BUS_CAPACITY_FIELDS:
            reduced = [float(values[o]) for o in retained]
            for o, r in target.items():
                v = float(values[o] or 0.0)
                if v == 0.0:
                    continue
                if r is None:
                    logger.warning(
                        "%s: %.3g of %s on eliminated bus %s has no retained bus "
                        "in its node; dropped from the reduced dispatch",
                        vdata.get("name"), v, field_name, original_buses[o].bus_id)
                else:
                    reduced[r] += v
            out[field_name] = reduced
        elif field_name == "num_units":
            # The units of the buses whose capacity a retained bus takes
            units = [0] * len(retained)
            for o in range(n_orig):
                r = reduced_of.get(o, target.get(o))
                if r is not None and float(rp[o] or 0.0) > 0:
                    units[r] += int(values[o])
            out[field_name] = [max(1, u) for u in units]
        else:
            out[field_name] = [values[o] for o in retained]
    return out


def _add_unattached_load_shed(result: Dict[str, Any], demand, nodes, jl_input,
                              keep_steps: Optional[int] = None) -> None:
    """Report the demand of nodes without a load bus as unserved load.

    That demand sits in no power balance (see ``_nodes_without_load_bus``),
    so the model can neither serve nor shed it. It is added to the result as
    ``unattached_load_shed`` [node, step] (MW), to ``load_shed_total`` (MWh)
    and, at the model's loss-of-load penalty, to the load-shedding cost, the
    cost total and the objective (only the committed ``keep_steps`` count
    in the totals, as for the rest of the window).
    """
    if not nodes or demand is None:
        return
    from esfex.bridge.converters import COST_UNSCALE
    dem = np.asarray(demand, dtype=float)               # [step, node]
    n_steps, n_nodes = dem.shape
    unserved = np.zeros((n_nodes, n_steps))
    for n in nodes:
        if n < n_nodes:
            unserved[n] = np.clip(dem[:, n], 0.0, None)
    result["unattached_load_shed"] = unserved
    kept = n_steps if keep_steps is None or not 0 < int(keep_steps) < n_steps else int(keep_steps)
    tres = max(1.0, float(jl_input.temporal.resolution_hours))
    energy = float(unserved[:, :kept].sum()) * tres
    if energy <= 0:
        return
    cost = energy * float(jl_input.loss_of_load_penalty) * COST_UNSCALE
    result["load_shed_total"] = float(result.get("load_shed_total", 0.0)) + energy
    result["objective"] = float(result.get("objective", 0.0)) + cost
    cbd = result.get("cost_breakdown")
    if cbd is not None:
        cbd["load_shedding_cost"] = cbd.get("load_shedding_cost", 0.0) + cost
        cbd["total"] = cbd.get("total", 0.0) + cost


def _expand_powersystem_result(
    result: Dict[str, Any],
    expander: "ResultExpander",
) -> Dict[str, Any]:
    """Expand a reduced-network PowerSystemResult dict to original topology.

    Generation / battery arrays have shape ``(n_unit, n_bus, n_t)`` in the
    Julia result.  Voltage angles / prices have shape ``(n_bus, n_t)``.
    Per-line flow arrays are returned as a list of per-line vectors.

    The expander pads the bus axis to the original bus count and remaps
    the line axis using the stored reduction map.
    """
    import numpy as _np

    rm = expander.rm
    if rm.n_reduced_buses == rm.n_original_buses and rm.n_reduced_lines == rm.n_original_lines:
        return result

    # ── Per-bus (gen/bat) 3-D arrays: bus axis = 1 ──
    for key in (
        "gen_output", "curtailment",
        "gen_status", "gen_startup",
        "bat_charge", "bat_discharge", "bat_soc", "bat_spillage",
        "reservoir_level", "reservoir_spillage", "reservoir_pump",
        "electrolyzer_power", "electrolyzer_h2",
    ):
        arr = result.get(key)
        if arr is None:
            continue
        a = _np.asarray(arr)
        if a.ndim == 3 and a.shape[1] == rm.n_reduced_buses:
            result[key] = expander.expand_bus_array(a, axis=1, copy_from_neighbour=False)

    # ── Per-bus 2-D arrays: bus axis = 0 ──
    for key in ("voltage_angle", "energy_prices"):
        arr = result.get(key)
        if arr is None:
            continue
        a = _np.asarray(arr)
        if a.ndim == 2 and a.shape[0] == rm.n_reduced_buses:
            result[key] = expander.expand_bus_array(
                a, axis=0,
                copy_from_neighbour=True,  # leaves inherit, series interpolates
            )

    # ── Per-line flows ──
    # Julia's ``power_flow_by_line`` concatenates transmission lines +
    # transformers (see ``transmission_dc.jl``).  After Phase 2 reduction
    # the Julia side has 0 transformers, so the reduced output length is
    # ``n_reduced_lines``.  The expander re-emits an array of length
    # ``n_original_lines + n_original_transformers`` matching the
    # pre-reduction ordering.
    pf_by_line = result.get("power_flow_by_line")
    if pf_by_line is not None and len(pf_by_line) == rm.n_reduced_lines:
        stacked = _np.stack([_np.asarray(pf) for pf in pf_by_line], axis=0)
        n_tf = getattr(rm, "n_original_transformers", 0)
        if n_tf > 0:
            expanded = expander.expand_line_plus_transformer_array(stacked, axis=0)
        else:
            expanded = expander.expand_line_array(stacked, axis=0)
        result["power_flow_by_line"] = [expanded[i] for i in range(expanded.shape[0])]

    return result


def _resolve_element_bus_mapping(
    sys_config: "SystemConfig",
) -> tuple[
    Dict[str, Dict[int, int]],
    Dict[str, Dict[int, int]],
]:
    """Resolve per-(unit, node) physical bus placement for existing gens and batteries.

    The operational DC-OPF needs every existing piece of generation/storage
    capacity anchored to its real physical bus so the power-flow equations
    reflect the actual transmission topology.  Aggregated configs
    (e.g. Cuba's ``Antonio Maceo`` with ``rated_power`` non-zero in 7
    different nodes) represent a fleet of plants distributed across the
    network — each per-node entry must land at the bus that actually
    hosts that piece.

    Resolution sources, in priority order:

    1. ``transmission_lines_geo`` entries with ``from/to_endpoint_type ==
       'generator' | 'battery'``: directly identifies the connected bus.
       Provides a unit-wide bus (one entry per gen, all nodes share it).
    2. Explicit ``bus_index`` field on the unit config (Grid Builder anchor).

    Returns:
        ``(gen_bus_per_node, bat_bus_per_node)`` where each dict maps
        ``unit_key → {node_idx: bus_idx}``.  A unit appears for every
        node where its ``rated_power`` is strictly positive.  Nodes
        without an entry fall through to the node's first (canonical)
        bus in :func:`expand_node_to_bus_array`.
    """
    buses = sys_config.buses or []
    bus_id_to_idx = {b.bus_id: i for i, b in enumerate(buses)}
    bus_to_node = {i: int(b.parent_node) for i, b in enumerate(buses)}
    gen_bus_per_node: Dict[str, Dict[int, int]] = {}
    bat_bus_per_node: Dict[str, Dict[int, int]] = {}

    def _set_for_all_active_nodes(
        unit_key: str,
        rated_power: list,
        bus_idx: int,
        dest: Dict[str, Dict[int, int]],
    ) -> None:
        """Apply ``bus_idx`` to every node where the unit has positive capacity.

        Source 1 (endpoint) and Source 2 (``bus_index`` field) only tell
        us a single bus for the whole unit.  Replicate it across every
        active node so we get a complete per-node mapping.
        """
        sub = dest.setdefault(unit_key, {})
        node_of_bus = bus_to_node.get(int(bus_idx))
        for n, v in enumerate(rated_power or []):
            if v and v > 0:
                # Prefer the bus's own parent_node, but the endpoint
                # data is unit-wide so apply unconditionally; the
                # geographic snap will refine it below for multi-node
                # units where the endpoint placement doesn't match.
                if node_of_bus == n:
                    sub.setdefault(n, int(bus_idx))

    # --- Source 0 (authoritative): per-node physical bus_id from the
    # serializer (`bus_id_per_node`).  Each per-node piece of a (possibly
    # multi-node) unit is anchored at its OWN real bus — no replication,
    # no aggregation.  This is the correct physical placement; the other
    # sources are fallbacks for legacy configs that lack it. ---
    def _apply_bus_id_per_node(units, dest):
        for key, cfg in (units or {}).items():
            mapping = getattr(cfg, 'bus_id_per_node', None)
            if not mapping:
                continue
            sub = dest.setdefault(key, {})
            for nd, bid in mapping.items():
                gi = bus_id_to_idx.get(bid)
                if gi is not None:
                    sub[int(nd)] = gi
    _apply_bus_id_per_node(sys_config.generators, gen_bus_per_node)
    _apply_bus_id_per_node(sys_config.batteries, bat_bus_per_node)

    # --- Source 1: explicit gen/bat ↔ bus endpoints in transmission_lines_geo ---
    for line in (sys_config.transmission_lines_geo or []):
        endpoints = [
            (getattr(line, 'from_endpoint_type', None),
             getattr(line, 'from_endpoint_id', None)),
            (getattr(line, 'to_endpoint_type', None),
             getattr(line, 'to_endpoint_id', None)),
        ]
        for i, (etype, eid) in enumerate(endpoints):
            if etype in ('generator', 'battery') and eid:
                other_type, other_id = endpoints[1 - i]
                if other_type == 'bus' and other_id in bus_id_to_idx:
                    # Strip _n{N} suffix: "unit_8_n0" → "unit_8"
                    element_key = re.sub(r'_n\d+$', '', str(eid))
                    bus_idx = bus_id_to_idx[other_id]
                    if etype == 'generator':
                        gen_cfg = (sys_config.generators or {}).get(element_key)
                        if gen_cfg is not None:
                            _set_for_all_active_nodes(
                                element_key, list(getattr(gen_cfg, 'rated_power', []) or []),
                                bus_idx, gen_bus_per_node,
                            )
                    else:
                        bat_cfg = (sys_config.batteries or {}).get(element_key)
                        if bat_cfg is not None:
                            _set_for_all_active_nodes(
                                element_key, list(getattr(bat_cfg, 'rated_power', []) or []),
                                bus_idx, bat_bus_per_node,
                            )

    # --- Source 2: explicit ``bus_index`` field on the unit config ---
    for key, gen in (sys_config.generators or {}).items():
        bi = getattr(gen, 'bus_index', None)
        if bi is None:
            continue
        bi = int(bi)
        if 0 <= bi < len(buses):
            _set_for_all_active_nodes(
                key, list(getattr(gen, 'rated_power', []) or []),
                bi, gen_bus_per_node,
            )
    for key, bat in (sys_config.batteries or {}).items():
        bi = getattr(bat, 'bus_index', None)
        if bi is None:
            continue
        bi = int(bi)
        if 0 <= bi < len(buses):
            _set_for_all_active_nodes(
                key, list(getattr(bat, 'rated_power', []) or []),
                bi, bat_bus_per_node,
            )

    # Drop empty entries to keep the contract clean.
    gen_bus_per_node = {k: v for k, v in gen_bus_per_node.items() if v}
    bat_bus_per_node = {k: v for k, v in bat_bus_per_node.items() if v}
    return gen_bus_per_node, bat_bus_per_node


# Units already warned about free life extension, so the master of every
# myopic year does not repeat the warning: (system, kind, unit key).
_FREE_EXTENSION_WARNED: set = set()


def life_extension_invest_costs(sys) -> tuple[dict, dict]:
    """Investment costs that price the life extension of existing units.

    The master charges a life-extended MW ``invest_cost x
    life_extension_cost_factor`` per year. Existing units often carry an
    ``invest_cost`` of 0 (the Studio default), which made extension free.
    Where a unit's ``invest_cost`` is 0 at a node where it has capacity (the
    other nodes are ignored), the candidate it would be replaced with
    supplies the cost: for a generator, the technology with
    the same fuel and type (the one named by its ``technology`` field
    first, else the first in config order); for a battery, the battery
    technology named by its ``technology`` field, else the first battery
    technology. In a merged multi-system run (keys ``{system}__{key}``) the
    candidate comes from the unit's own system, whose costs sit at the
    unit's own nodes. Without a candidate, or where the candidate's cost is 0
    at a node the unit occupies, the extension stays free and a warning is
    logged once per unit.

    Returns:
        ``(gen_costs, bat_costs)``: per-node ``invest_cost`` lists for the
        generator / battery keys that changed.
    """
    def _fill(own, candidate):
        out = list(own)
        for n, c in enumerate(out):
            if c <= 0 and candidate:
                out[n] = float(candidate[n] if n < len(candidate) else candidate[-1])
        return out

    def _warn(kind, key, nodes=None):
        tag = (getattr(sys, "name", ""), kind, key)
        if tag not in _FREE_EXTENSION_WARNED:
            _FREE_EXTENSION_WARNED.add(tag)
            if nodes:
                logger.warning(
                    "%s %r has invest_cost 0 at node(s) %s and its candidate %s has "
                    "none there either: extending its life there is free in the "
                    "master problem.", kind, key, nodes,
                    "technology" if kind == "Generator" else "battery technology")
                return
            logger.warning(
                "%s %r has invest_cost 0 and no candidate %s to take it "
                "from: extending its life is free in the master problem.",
                kind, key,
                "technology with the same fuel and type" if kind == "Generator"
                else "battery technology")

    def _own_system(key, unit, catalogue):
        """Candidates from the unit's own system and the system's prefix.

        A merged unit has key ``{system}__{key}`` and name ``{system}/...``;
        its system's candidates are keyed ``{system}__...`` and their
        per-node lists are 0 at the other systems' nodes.
        """
        system = key.split("__", 1)[0] if "__" in key else None
        if system is None or not str(getattr(unit, "name", "")).startswith(system + "/"):
            return list(catalogue.items()), ""
        prefix = system + "__"
        return [(k, c) for k, c in catalogue.items() if k.startswith(prefix)], prefix

    def _names(k, cand, prefix):
        """A candidate's key and name, also without the merge prefix."""
        names = {k, cand.name}
        if prefix:
            names |= {k[len(prefix):], str(cand.name).split("/", 1)[-1]}
        return names

    def _still_free(costs, rated):
        return [n for n, p in enumerate(rated) if p > 0 and (n >= len(costs) or costs[n] <= 0)]

    gen_costs: dict = {}
    techs = getattr(sys, "technologies", None) or {}
    for key, gen in (getattr(sys, "generators", None) or {}).items():
        # Only the nodes where the unit has capacity: elsewhere its 0 cost
        # prices nothing
        if not _still_free(gen.invest_cost, gen.rated_power):
            continue
        candidates, prefix = _own_system(key, gen, techs)
        matches = [(k, t) for k, t in candidates
                   if t.fuel == gen.fuel and t.type == gen.type]
        named = [m for m in matches
                 if getattr(gen, "technology", None) in _names(*m, prefix)]
        if not (named or matches):
            _warn("Generator", key)
            continue
        _, tech = (named or matches)[0]
        own = list(gen.invest_cost) + [0.0] * (len(gen.rated_power) - len(gen.invest_cost))
        filled = _fill(own, tech.invest_cost)
        if filled != own:
            gen_costs[key] = filled
        free = _still_free(filled, gen.rated_power)
        if free:
            _warn("Generator", key, free)

    bat_costs: dict = {}
    btechs = getattr(sys, "battery_technologies", None) or {}
    for key, bat in (getattr(sys, "batteries", None) or {}).items():
        if not _still_free(bat.invest_cost, bat.rated_power):
            continue
        candidates, prefix = _own_system(key, bat, btechs)
        named = [m for m in candidates if bat.technology in _names(*m, prefix)]
        if not (named or candidates):
            _warn("Battery", key)
            continue
        _, btech = (named or candidates)[0]
        own = list(bat.invest_cost) + [0.0] * (len(bat.rated_power) - len(bat.invest_cost))
        filled = _fill(own, btech.invest_cost_power)
        if filled != own:
            bat_costs[key] = filled
        free = _still_free(filled, bat.rated_power)
        if free:
            _warn("Battery", key, free)

    return gen_costs, bat_costs


def _compute_geographic_fuel_adjustments(
    sys_config: "SystemConfig",
) -> Dict[str, List[float]]:
    """Compute fuel_cost adjustments based on geographic distance to nearest fuel storage.

    For each fuel-consuming generator with known geographic position, find the
    nearest fuel storage containing that fuel and adjust ``fuel_cost`` to account
    for distance-based transport costs and losses.

    Returns:
        dict mapping gen_key → adjusted ``fuel_cost`` array (only keys that need
        adjustment are included).
    """
    layout = sys_config.gui_layout
    if not layout:
        return {}
    gen_positions = layout.get("generators", {})
    storage_positions = layout.get("fuel_storages", {})
    if not gen_positions or not storage_positions:
        return {}

    # --- Build fuel supply map: fuel_name → [(storage_id, lat, lng, node)] ---
    storage_facilities = sys_config.fuel_infrastructure.storage_facilities
    fuel_supply: Dict[str, List[tuple]] = {}
    for sid, sdata in storage_facilities.items():
        if sid not in storage_positions:
            continue
        slat, slng = storage_positions[sid]
        snode = sdata.get("node", 0)
        fuels_list = sdata.get("fuels", [])
        if not fuels_list:
            # Legacy single-fuel format
            single_fuel = sdata.get("fuel", "")
            if single_fuel:
                fuels_list = [single_fuel]
        for fname in fuels_list:
            fuel_supply.setdefault(fname, []).append((sid, slat, slng, snode))

    if not fuel_supply:
        return {}

    # --- Derive per-fuel per-km transport rates from existing routes ---
    transport_pipelines = sys_config.fuel_infrastructure.transport_pipelines
    fuel_cost_per_km: Dict[str, float] = {}
    fuel_loss_per_km: Dict[str, float] = {}

    if transport_pipelines:
        # Accumulate (sum, count) for each fuel
        cost_acc: Dict[str, List[float]] = {}
        loss_acc: Dict[str, List[float]] = {}
        for _route_id, rdata in transport_pipelines.items():
            length_km = rdata.get("length_km", 0.0)
            if length_km < 0.5:
                # Skip very short routes (e.g. port connections < 500m)
                continue
            route_fuels = rdata.get("fuels", [])
            if not route_fuels:
                single = rdata.get("fuel", "")
                if single:
                    route_fuels = [single]
            fuel_params = rdata.get("fuel_params", {})
            for fname in route_fuels:
                fp = fuel_params.get(fname, {})
                cost = fp.get("transport_cost", rdata.get("transport_cost", 0))
                losses = fp.get("losses_fraction", rdata.get("losses_fraction", 0))
                cost_acc.setdefault(fname, []).append(cost / length_km)
                loss_acc.setdefault(fname, []).append(losses / length_km)

        for fname, vals in cost_acc.items():
            fuel_cost_per_km[fname] = sum(vals) / len(vals)
        for fname, vals in loss_acc.items():
            fuel_loss_per_km[fname] = sum(vals) / len(vals)

    # Fallback defaults
    DEFAULT_COST_PER_KM = 0.5  # $/fuel_unit/km
    DEFAULT_LOSS_PER_KM = 0.001  # fraction/km

    # --- For each generator, compute adjusted fuel_cost ---
    # gui_layout keys use canonical format: "{unit_key}_n{node_idx}"
    # config generator keys are just "{unit_key}"
    adjustments: Dict[str, List[float]] = {}
    log_entries: List[str] = []

    num_nodes = sys_config.nodes.num_nodes or 1

    for gen_key, gen_cfg in sys_config.generators.items():
        fuel = gen_cfg.fuel
        if fuel in _RENEWABLE_FUELS:
            continue
        fuel_def = sys_config.fuels.get(fuel)
        if not fuel_def or not fuel_def.energy_content:
            continue

        candidates = fuel_supply.get(fuel, [])
        if not candidates:
            continue

        energy_content = fuel_def.energy_content
        efficiency = float(np.mean(gen_cfg.eff_at_rated)) if gen_cfg.eff_at_rated else 0.35
        if efficiency < 1e-6:
            efficiency = 0.35

        cpk = fuel_cost_per_km.get(fuel, DEFAULT_COST_PER_KM)
        lpk = fuel_loss_per_km.get(fuel, DEFAULT_LOSS_PER_KM)

        # Build per-node adjusted fuel_cost
        adjusted = list(gen_cfg.fuel_cost)
        any_adjusted = False

        for node_idx in range(min(num_nodes, len(adjusted))):
            # Look up position using canonical key format: {gen_key}_n{node_idx}
            layout_key = f"{gen_key}_n{node_idx}"
            if layout_key not in gen_positions:
                continue
            gen_lat, gen_lng = gen_positions[layout_key]

            # Find nearest storage with matching fuel (prefer same node)
            best_dist = float("inf")
            best_sid = ""
            for sid, slat, slng, snode in candidates:
                d = _haversine_km(gen_lat, gen_lng, slat, slng)
                if d < best_dist:
                    best_dist = d
                    best_sid = sid

            if best_dist == float("inf") or best_dist < 0.01:
                continue

            # Transport cost: $/fuel_unit/km → $/MWh_e
            transport_cost_mwhe = cpk * best_dist / (energy_content * efficiency)

            # Transport losses: need more fuel due to losses
            loss_factor = min(lpk * best_dist, 0.5)  # cap at 50%
            loss_multiplier = 1.0 / (1.0 - loss_factor) if loss_factor < 1.0 else 1.0

            adjusted[node_idx] = adjusted[node_idx] * loss_multiplier + transport_cost_mwhe
            any_adjusted = True

            # Log entry
            storage_name = storage_facilities.get(best_sid, {}).get("name", best_sid)
            orig_cost = gen_cfg.fuel_cost[node_idx]
            new_cost = adjusted[node_idx]
            log_entries.append(
                f"  {gen_cfg.name:30s} (n{node_idx}) → {storage_name:25s}  "
                f"dist={best_dist:6.1f}km  "
                f"fuel_cost: {orig_cost:.2f} → {new_cost:.2f} $/MWh_e  "
                f"(+{new_cost - orig_cost:.2f})"
            )

        if any_adjusted:
            adjustments[gen_key] = adjusted

    if log_entries:
        logger.debug(
            "Geographic fuel transport adjustments:\n%s",
            "\n".join(log_entries),
        )
        # Summary at info level (only generator count + range)
        costs = [a[0] for a in adjustments.values() if a]
        orig = [sys_config.generators[k].fuel_cost[0] for k in adjustments if sys_config.generators[k].fuel_cost]
        if costs and orig:
            max_delta = max(c - o for c, o in zip(costs, orig))
            logger.debug(
                "Geographic fuel transport: %d generators adjusted, max delta=+%.2f $/MWh_e",
                len(adjustments), max_delta,
            )

    return adjustments


class TransmissionDCAdapter:
    """
    Python adapter for the Julia TransmissionDC model.

    Provides DC power flow constraints using Kirchhoff formulation.
    """

    def __init__(
        self,
        num_nodes: int,
        nodes_config: NodeConfig,
        fuel_transport_distances: List[List[float]],
        s_base_mva: float = 100.0,
        reactance_per_km: float = 0.4,
        voltage_level_kv: float = 220.0,
        max_angle_diff_deg: float = 30.0,
        transmission_lines_geo=None,
        transformers=None,
        acdc_converters=None,
        freq_converters=None,
        buses=None,
    ):
        """
        Initialize the TransmissionDC adapter.

        Args:
            num_nodes: Number of nodes in the network
            nodes_config: Node configuration
            fuel_transport_distances: Distance matrix (km)
            s_base_mva: System power base (MVA) of the per-unit values
            reactance_per_km: Line reactance (Ohm/km)
            voltage_level_kv: Nominal voltage (kV)
            max_angle_diff_deg: Maximum angle difference (degrees, ACOPF)
            transmission_lines_geo: Per-line transmission data (optional)
            transformers: Transformer definitions (optional)
            acdc_converters: AC/DC converter definitions (optional)
            freq_converters: Frequency converter definitions (optional)
            buses: Bus definitions (optional, auto-creates one per node)
        """
        from esfex.config.schema import DCPowerFlowConfig

        self.num_nodes = num_nodes
        self.nodes_config = nodes_config
        self.distances = fuel_transport_distances

        # Get Julia module
        ESFEX = get_esfex_module()

        # Build a DCPowerFlowConfig from the provided parameters
        dc_config = DCPowerFlowConfig(
            reactance_per_km=reactance_per_km,
            voltage_level_kv=voltage_level_kv,
            max_angle_diff_deg=max_angle_diff_deg,
            slack_bus=0,
        )

        # Create Julia NetworkConfig via the converter
        jl_network = convert_network_config(
            nodes_config, dc_config, fuel_transport_distances,
            transmission_lines_geo=transmission_lines_geo,
            transformers=transformers,
            acdc_converters=acdc_converters,
            freq_converters=freq_converters,
            buses=buses,
            s_base_mva=s_base_mva,
        )

        # Create Julia TransmissionDC instance
        self._jl_transmission = ESFEX.TransmissionDC(jl_network)

        # Cache properties
        self.lines = self._get_lines()
        self.line_reactances = self._get_line_reactances()

    def _get_lines(self) -> List[tuple]:
        """Get the list of transmission lines."""
        jl_lines = self._jl_transmission.lines

        lines = []
        for line in jl_lines:
            # Convert 1-indexed Julia to 0-indexed Python
            lines.append((int(line[0]) - 1, int(line[1]) - 1))

        return lines

    def _get_line_reactances(self) -> Dict[tuple, float]:
        """Get line reactances keyed by (from, to) tuple (0-indexed)."""
        jl_reactances = self._jl_transmission.line_reactances
        jl_lines = self._jl_transmission.lines

        reactances = {}
        for idx in range(len(jl_reactances)):
            line = (int(jl_lines[idx][0]) - 1, int(jl_lines[idx][1]) - 1)
            reactances[line] = float(jl_reactances[idx])

        return reactances

    @property
    def incidence_matrix(self) -> np.ndarray:
        """Get the node-line incidence matrix."""
        return np.array(self._jl_transmission.incidence_matrix)



class PowerSystemAdapter:
    """
    Python adapter for the Julia PowerSystem optimization model.

    Provides the same interface as the legacy Python PowerSystem class
    while using the Julia JuMP implementation.
    """

    def __init__(
        self,
        config: Union[ESFEXConfig, SystemConfig],
        demand: np.ndarray,
        hours: int,
        num_nodes: int,
        year: int,
        base_year: int,
        mode: str = "development",
        availability_cache: Optional[Dict[str, np.ndarray]] = None,
        inflow_cache: Optional[Dict[str, np.ndarray]] = None,
        start_hour: int = 0,
        run_mode: Optional[str] = None,
        **kwargs,
    ):
        """
        Initialize the PowerSystem adapter.

        Args:
            config: ESFEX or System configuration
            demand: Demand array (hours x nodes)
            hours: Number of hours
            num_nodes: Number of nodes
            year: Current simulation year
            base_year: Base year for calculations
            mode: Dispatch formulation ("economic_dispatch" or
                "unit_commitment"; a development run's dispatch is one of them)
            availability_cache: Pre-loaded availability profiles {gen_key: array}
            inflow_cache: Pre-loaded inflow profiles {gen_key: array} for reservoir hydro
            start_hour: Start hour within the year for slicing availability/inflow profiles
            run_mode: The run's ``simulation_mode`` ("development",
                "unit_commitment", "economic_dispatch"), which
                ``n1_security.apply_to_modes`` lists. Defaults to the
                configuration's ``simulation_mode`` (``mode`` without one).
            **kwargs: Additional parameters (system_config: explicit SystemConfig override)
        """
        # Accept explicit system_config override (used by runner to pass
        # the merged system instead of config.primary_system).
        system_config_override = kwargs.pop("system_config", None)
        # The configuration file: relative profile paths resolve next to it
        self._config_path = kwargs.pop("config_path", None)
        if isinstance(config, ESFEXConfig):
            self.esfex_config = config
            self.system_config = system_config_override or config.primary_system
        else:
            self.esfex_config = None
            self.system_config = system_config_override or config

        self.demand = demand
        self.hours = hours
        self.num_nodes = num_nodes
        self.year = year
        self.base_year = base_year
        self.mode = mode
        if run_mode is None:
            run_mode = self.esfex_config.simulation_mode if self.esfex_config else mode
        self.run_mode = run_mode
        self.availability_cache = availability_cache or {}
        self.inflow_cache = inflow_cache or {}
        self.start_hour = start_hour
        self.units_config = kwargs.get('units_config', {})  # Updated capacities from investments
        self.sectoral_demand = kwargs.get('sectoral_demand', {})  # Sectoral demand arrays
        self.rooftop_generation = kwargs.get('rooftop_generation', None)  # (hours × nodes) or None
        # Year-specific RE target (development mode only). target_re_penetration
        # is a planning target: without a year's value the dispatch applies none.
        self.re_penetration_target_override = kwargs.get('re_penetration_target', None)
        self.boundary_conditions = kwargs.get('boundary_conditions', {}) or {}
        self.kwargs = kwargs

        self._jl_model = None
        self._jl_vars = None
        self._jl_input = None
        self._fallback_model = None  # Set when JuMP optimize! fails but LP fallback succeeds
        self._reduction_map = None  # Set in _create_input when network_reduction.enabled

        logger.debug(f"PowerSystemAdapter initialized: {hours}h, {num_nodes} nodes, mode={mode}")

    def _slice_availability(self, cached: np.ndarray, resolution_hours: int) -> Optional[np.ndarray]:
        """Slice a cached hourly profile (availability, inflow) to this window.

        ``start_hour`` counts from the start of the model year. The profile is
        indexed by horizon hour (year index × 8760 + hour), wrapping around
        when it is shorter, as in the master problem: a multi-year profile
        gives each year its own block, a one-year profile repeats.
        """
        start = absolute_start_hour(self.year, self.base_year, self.start_hour) % max(len(cached), 1)
        if resolution_hours > 1 and len(cached) > self.hours:
            resampled = aggregate_to_resolution(cached, target_hours=resolution_hours)
            start_idx = start // resolution_hours
            end_idx = start_idx + self.hours
            return resampled[start_idx:end_idx]
        else:
            end_h = start + self.hours
            if end_h <= len(cached):
                return cached[start:end_h]
            elif start < len(cached):
                return np.concatenate([
                    cached[start:],
                    cached[:end_h - len(cached)]
                ])
            else:
                wrapped_start = start % len(cached)
                wrapped_end = wrapped_start + self.hours
                if wrapped_end <= len(cached):
                    return cached[wrapped_start:wrapped_end]
                else:
                    return np.concatenate([
                        cached[wrapped_start:],
                        cached[:wrapped_end - len(cached)]
                    ])

    def _storage_availability(self, cache_key: str, availability_file: Optional[str],
                              num_nodes: int, resolution_hours: int,
                              owner: str) -> Optional[np.ndarray]:
        """This window's availability profile of a battery (``[steps × nodes]``),
        or None when it has none: the runner's preloaded profile
        (``availability_cache[cache_key]``, else by file name), else read
        from ``availability_file``."""
        if not availability_file:
            return None
        cached = self.availability_cache.get(cache_key)
        if cached is None:
            cached = self.availability_cache.get(availability_file)
        if cached is None:
            cached = self._load_availability_file(availability_file, num_nodes, owner)
        return self._slice_availability(cached, resolution_hours)

    def _load_availability_file(self, availability_file: str, num_nodes: int,
                                owner: str) -> np.ndarray:
        """Hourly profile of a unit not in ``availability_cache``.

        Read with the runner's reader (:func:`esfex.io.demand.
        load_run_availability`): path under the configuration's directory
        (``config_path``), Feb 29 dropped by the calendar of ``date_start``.
        Pass the result through :meth:`_slice_availability` like a cached one.
        """
        from pathlib import Path

        from esfex.io.demand import load_run_availability
        from esfex.utils.temporal import parse_date_start

        config_path = getattr(self, "_config_path", None)
        config_dir = Path(config_path).parent if config_path else Path(".")
        date_start = None
        if self.esfex_config is not None:
            date_start = parse_date_start(self.esfex_config.date_start).strftime("%d/%m/%Y %H:%M")
        return load_run_availability(config_dir, availability_file, num_nodes, date_start,
                                     owner=owner)

    def build_model(self, external_model=None):
        """
        Build the JuMP optimization model.

        Args:
            external_model: Optional external model to add constraints to
        """
        ESFEX = get_esfex_module()
        jl = get_julia()

        import time as _time
        logger.debug("Building Julia PowerSystem model...")

        # Cross-check the SystemConfig topology vs what the solver
        # will actually see. Surfaces silent drops & orphan islands
        # before the solve consumes them.
        try:
            from esfex.bridge.topology_audit import audit_system_config
            audit = audit_system_config(self.system_config)
            if not audit.is_clean():
                logger.warning(
                    "Topology audit before solve:\n%s", audit.summary()
                )
                if audit.lines_dropped_unresolved:
                    logger.warning(
                        "  Lines silently dropped (unresolved buses): %s",
                        audit.lines_dropped_unresolved[:20],
                    )
                if audit.orphan_buses:
                    logger.warning(
                        "  Orphan buses (no edges): %s",
                        audit.orphan_buses[:20],
                    )
                if audit.surplus_components:
                    logger.warning(
                        "  Components with generation but no demand: %d "
                        "(generation in these will not reach demand)",
                        len(audit.surplus_components),
                    )
        except Exception:
            logger.debug("topology_audit failed (non-fatal)", exc_info=True)

        # Build PowerSystemInput (Python → Julia data conversion)
        t0 = _time.perf_counter()
        self._jl_input = self._create_input()
        t_input = _time.perf_counter() - t0

        # Resolve declarative user constraints targeting the operational model.
        op_specs = resolve_custom_constraints(self.system_config)

        # Create model and variables (JuMP model construction)
        t0 = _time.perf_counter()
        if op_specs:
            self._jl_model, self._jl_vars = ESFEX.create_power_system(
                self._jl_input,
                custom_constraints=_custom_constraints_to_julia(op_specs, jl),
            )
        else:
            self._jl_model, self._jl_vars = ESFEX.create_power_system(self._jl_input)
        t_build = _time.perf_counter() - t0

        logger.info(f"⏱ PowerSystem adapter: _create_input={t_input:.2f}s, "
                     f"create_power_system={t_build:.2f}s")

    def _create_input(self) -> Any:
        """Create the Julia PowerSystemInput struct."""
        ESFEX = get_esfex_module()
        jl = get_julia()

        sys = self.system_config
        # Get num_nodes from NodeConfig or calculate from connections matrix
        import math
        num_nodes = sys.nodes.num_nodes or int(math.sqrt(len(sys.nodes.nodes_connections)))
        temporal = self.esfex_config.temporal if self.esfex_config else TemporalConfig()
        resolution_hours = getattr(temporal, 'resolution_hours', 1)

        # ── Optional internal network reduction ──
        # When enabled, the bus-level transmission topology is reduced
        # before model construction.  The reduction is stored on self so
        # post-solve result expansion can recover original-topology values.
        self._reduction_map = None
        if (self.esfex_config is not None
                and getattr(self.esfex_config, "network_reduction", None) is not None
                and self.esfex_config.network_reduction.enabled
                and sys.buses):
            from esfex.topology import reduce_network
            import time as _t
            t0 = _t.perf_counter()
            kron_flag = bool(getattr(
                self.esfex_config.network_reduction, "kron_deg3", False
            ))
            reduced_sys, self._reduction_map = reduce_network(
                sys, kron_deg3=kron_flag,
            )
            t_reduce = _t.perf_counter() - t0
            logger.info(
                f"⏱ Network reduction: {t_reduce:.2f}s — "
                f"{self._reduction_map.summary()}"
            )
            sys = reduced_sys

        # Pre-compute geographic fuel transport adjustments
        fuel_adjustments = _compute_geographic_fuel_adjustments(sys)

        # Build bus_to_node mapping for per-node → per-bus expansion.
        # Converter functions expand per-node arrays to per-bus arrays
        # (capacity at the unit's bus, properties replicated) whenever the bus
        # list is not the identity mapping over nodes — see
        # ``node_bus_mapping_required``. A reduced network can have FEWER buses
        # than nodes, which the old ``num_buses > num_nodes`` test missed.
        from esfex.config.schema import BusConfig
        from esfex.bridge.converters import node_bus_mapping_required
        buses = sys.buses or [
            BusConfig(bus_id=f"bus_{i}", parent_node=i, demand_fraction=1.0)
            for i in range(num_nodes)
        ]
        bus_to_node_0idx = [b.parent_node for b in buses]
        num_buses = len(buses)
        need_expansion = node_bus_mapping_required(buses, num_nodes)
        b2n_arg = bus_to_node_0idx if need_expansion else None
        self._unattached_nodes = _nodes_without_load_bus(buses, num_nodes)
        _dem = np.asarray(self.demand) if self.demand is not None else None
        _orphan = [n for n in self._unattached_nodes
                   if _dem is not None and _dem.ndim == 2 and n < _dem.shape[1]
                   and float(np.nanmax(_dem[:, n], initial=0.0)) > 0.0]
        if _orphan:
            _warn_once(
                ("unattached_nodes", sys.name, tuple(_orphan)),
                "%d node(s) have no operational load bus (%s%s): their demand "
                "cannot be served and is reported as unserved load (loss of "
                "load, priced at the loss_of_load penalty). Units at a node "
                "with no bus at all are not dispatched. Give each such node a "
                "bus with role 'load' or 'mixed' (a network reduction can "
                "also leave nodes without buses).",
                len(_orphan), _orphan[:10],
                "…" if len(_orphan) > 10 else "",
            )

        # Resolve generator/battery → bus mapping from transmission_lines_geo
        gen_bus_per_node, bat_bus_per_node = _resolve_element_bus_mapping(sys)
        if gen_bus_per_node:
            logger.debug(
                f"Resolved generator (gen,node)→bus mapping: "
                f"{sum(len(v) for v in gen_bus_per_node.values())} entries"
            )
        if bat_bus_per_node:
            logger.debug(
                f"Resolved battery (bat,node)→bus mapping: "
                f"{sum(len(v) for v in bat_bus_per_node.values())} entries"
            )

        # Scheduled interruptions (deterministic outages): index every element's
        # windows once, keyed by type then id. Generators fold their windows into
        # the availability matrix in the loop below; other element categories are
        # compiled into the centralised outage_masks dict passed to Julia.
        # Windows are in absolute horizon hours: locate this window in them.
        outage_hour0 = absolute_start_hour(self.year, self.base_year, self.start_hour)
        outage_by_type: dict[str, dict[str, list]] = {}
        # Generator / battery windows may target one node of the unit.
        outage_nodes: dict[str, dict[str, list]] = {}
        for _ow in getattr(sys, "outage_schedule", None) or []:
            outage_by_type.setdefault(_ow.element_type, {}).setdefault(
                _ow.element_id, []
            ).append(outage_window_tuple(_ow))
            outage_nodes.setdefault(_ow.element_type, {}).setdefault(
                _ow.element_id, []
            ).append(getattr(_ow, "node", None))
        outage_by_gen = outage_by_type.get("generator", {})
        bat_windows_by_key = outage_by_type.get("battery", {})
        for _key, _nodes in outage_nodes.get("battery", {}).items():
            _bat = sys.batteries.get(_key)
            if _bat is not None and any(n is not None for n in _nodes) and \
                    sum(1 for c in _bat.capacity if c > 0) > 1:
                logger.warning(
                    "Outage window on one node of battery %r: battery outages "
                    "apply to the whole unit, which has capacity at several "
                    "nodes.", _key)

        # Convert generators with availability profiles
        # Track order for cost curve mapping (key, python_config) in Julia push order
        gen_order = []
        jl_generators = jl.seval("GeneratorConfig[]")
        for key, gen in sys.generators.items():
            # Use cached availability if available, otherwise load from file
            availability = None
            if key in self.availability_cache:
                # Pre-loaded cache at full resolution: slice this year's window
                # (mean aggregation for resolution > 1, correct for capacity factors)
                availability = self._slice_availability(
                    self.availability_cache[key], resolution_hours
                )
            elif hasattr(gen, 'availability_file') and gen.availability_file:
                # Not preloaded: read it as the runner does and slice this
                # window. A profile that cannot be read is an error: skipping
                # here dropped the whole generator from the dispatch.
                availability = self._slice_availability(
                    self._load_availability_file(
                        gen.availability_file, num_nodes, f"Generator {key!r}"),
                    resolution_hours,
                )

            # Apply updated capacities from units_config if available
            # This includes investments and cumulative capacities from MasterProblem
            gen_to_convert = gen
            if key in self.units_config:
                updated_data = self.units_config[key]
                if 'rated_power' in updated_data:
                    gen_dict = gen.model_dump()
                    gen_dict['rated_power'] = updated_data['rated_power']
                    # Also propagate the ageing overrides (no degradation and
                    # no end of life for MasterProblem cumulative capacities,
                    # which already hold the retirements and life extension)
                    # and the units left of that capacity (units_in_service)
                    for f in ('degradation_rate', 'initial_age', 'life_time', 'num_units'):
                        if f in updated_data:
                            gen_dict[f] = updated_data[f]
                    gen_to_convert = GeneratorConfig(**gen_dict)

            # Apply geographic fuel transport cost adjustment
            if key in fuel_adjustments:
                gen_dict = gen_to_convert.model_dump()
                gen_dict['fuel_cost'] = fuel_adjustments[key]
                gen_to_convert = GeneratorConfig(**gen_dict)

            # Reservoir inflow profile
            inflow = None
            if key in self.inflow_cache:
                # Mean aggregation for resolution > 1 (m³/s average over period)
                inflow = self._slice_availability(
                    self.inflow_cache[key], resolution_hours
                )

            # Apply scheduled interruptions as an availability mask. Materialise
            # a ones matrix for dispatchable units (no CF profile) so the outage
            # still bites — Julia now caps every generator by availability.
            gen_windows = outage_by_gen.get(key)
            if gen_windows:
                if availability is None:
                    availability = np.ones((self.hours, num_nodes), dtype=float)
                elif availability.ndim == 1:
                    availability = np.repeat(availability[:, None], num_nodes, axis=1)
                else:
                    availability = availability.copy()
                win_nodes = outage_nodes["generator"][key]
                for node in sorted({n for n in win_nodes if n is not None}) + [None]:
                    windows = [w for w, n in zip(gen_windows, win_nodes) if n == node]
                    if not windows:
                        continue
                    factor = compile_outage_factor(
                        windows, outage_hour0, self.hours, resolution_hours
                    )
                    if node is None:
                        availability = availability * factor[:, None]
                    elif node < availability.shape[1]:
                        availability[:, node] = availability[:, node] * factor

            gen_bus_map = gen_bus_per_node.get(key)
            jl_gen = convert_generator_config(gen_to_convert, availability, inflow,
                                               bus_to_node=b2n_arg,
                                               bus_per_node=gen_bus_map)
            jl.seval("push!")(jl_generators, jl_gen)
            gen_order.append((key, gen_to_convert))

        # Create virtual generators from investment units in units_config
        # These are created by _build_config_from_cumulative() but don't exist
        # in sys.generators — they represent technology investments from MasterProblem
        per_bus_gen_keys: set = set()
        for key, vdata in self.units_config.items():
            if key in sys.generators:
                continue  # Already handled above
            if vdata.get("_type") == "battery" or vdata.get("type") == "Storage":
                continue  # Batteries handled below
            if "rated_power" not in vdata:
                continue
            vdata = _per_bus_unit_on_model_buses(vdata, self._reduction_map,
                                                 self.system_config.buses or [])
            rp = vdata["rated_power"]
            if not rp or max(rp) < 0.01:
                continue

            # Build a GeneratorConfig from the virtual unit dict
            nn = len(rp)
            vgen = GeneratorConfig(
                name=vdata.get("name", key),
                type=vdata.get("type", "Renewable"),
                fuel=vdata.get("fuel", "None"),
                reservable=vdata.get("reservable", False),
                rated_power=rp,
                min_power=vdata.get("min_power", [0.0] * nn),
                eff_at_rated=vdata.get("eff_at_rated", [1.0] * nn),
                eff_at_min=vdata.get("eff_at_min", [1.0] * nn),
                ramp_up=vdata.get("ramp_up", [1.0] * nn),
                ramp_down=vdata.get("ramp_down", [1.0] * nn),
                min_up=vdata.get("min_up", [0] * nn),
                min_down=vdata.get("min_down", [0] * nn),
                fuel_cost=vdata.get("fuel_cost", [0.0] * nn),
                fixed_cost=vdata.get("fixed_cost", [0.0] * nn),
                maintenance_cost=vdata.get("maintenance_cost", [0.0] * nn),
                start_up_cost=vdata.get("start_up_cost", [0.0] * nn),
                inertia=vdata.get("inertia", [0.0] * nn),
                life_time=vdata.get("life_time", [999] * nn),
                initial_age=vdata.get("initial_age", [0] * nn),
                degradation_rate=vdata.get("degradation_rate", [0.0] * nn),
                decommissioning_cost=vdata.get("decommissioning_cost", [0.0] * nn),
                # Units of the technology's unit_size (one when it has none)
                num_units=vdata.get("num_units", [1] * nn),
                invest_cost=[0.0] * nn,
                frequency_hz=vdata.get("frequency_hz", 50.0),
                current_type=vdata.get("current_type", "AC"),
                availability_file=vdata.get("availability_file") or vdata.get("Availability"),
                # The technology's bidding curve, so the dispatch prices the
                # unit as the master did (flat fuel_cost otherwise).
                fuel_cost_curve=vdata.get("fuel_cost_curve"),
            )

            # Load availability profile for virtual generator
            v_avail = None
            avail_file = vdata.get("availability_file") or vdata.get("Availability")
            if avail_file:
                # Try availability cache by file path
                if avail_file in self.availability_cache:
                    v_avail = self._slice_availability(
                        self.availability_cache[avail_file], resolution_hours
                    )
                else:
                    # Try to find by matching availability file across cached generators
                    for cached_key, cached_arr in self.availability_cache.items():
                        cached_gen = sys.generators.get(cached_key)
                        if cached_gen and (
                            getattr(cached_gen, 'availability_file', None) == avail_file
                        ):
                            v_avail = self._slice_availability(cached_arr, resolution_hours)
                            break
                    else:
                        v_avail = self._slice_availability(
                            self._load_availability_file(
                                avail_file, num_nodes, f"Technology unit {key!r}"),
                            resolution_hours,
                        )

            if v_avail is None:
                logger.warning(f"Virtual gen {key}: NO availability matched for file={avail_file}")

            # Per-bus virtual generators: arrays already at bus-level length,
            # expand availability to per-bus and skip node→bus expansion.
            is_per_bus = vdata.get('_is_per_bus', False)
            if is_per_bus and b2n_arg is not None:
                if v_avail is not None and v_avail.shape[1] < num_buses:
                    v_avail_bus = np.zeros((v_avail.shape[0], num_buses))
                    for b_idx in range(num_buses):
                        n_idx = bus_to_node_0idx[b_idx]
                        if n_idx < v_avail.shape[1]:
                            v_avail_bus[:, b_idx] = v_avail[:, n_idx]
                    v_avail = v_avail_bus
                jl_vgen = convert_generator_config(vgen, v_avail, None,
                                                    bus_to_node=None)
            else:
                # Virtual gens (master investments) have no fixed physical
                # bus per node.  Place at the role-aware preferred bus per
                # node so the operational LP receives the master's per-node
                # capacity at a sensible bus — no replication / node cap
                # gymnastics that risk LP infeasibility.
                jl_vgen = convert_generator_config(vgen, v_avail, None,
                                                    bus_to_node=b2n_arg)
            jl.seval("push!")(jl_generators, jl_vgen)
            gen_order.append((key, vgen))
            if is_per_bus and b2n_arg is not None:
                per_bus_gen_keys.add(key)
            avail_shape = v_avail.shape if v_avail is not None else "NONE"
            avail_mean = float(np.mean(v_avail)) if v_avail is not None else 0.0
            logger.info(f"Virtual gen: {vdata.get('name', key)}, type={vdata.get('type')}, "
                       f"rated={rp}, avail_shape={avail_shape}, avail_mean={avail_mean:.3f}")

        # Convert batteries
        bat_order = []  # (key, python_config) in Julia push order
        jl_batteries = jl.seval("BatteryConfig[]")
        for key, bat in sys.batteries.items():
            # Apply updated capacities from units_config if available
            bat_to_convert = bat
            if key in self.units_config:
                updated_data = self.units_config[key]
                # Check for updated capacity or power ratings
                if 'capacity' in updated_data or 'MaxChargePower' in updated_data:
                    bat_dict = bat.model_dump()
                    if 'capacity' in updated_data:
                        bat_dict['capacity'] = updated_data['capacity']
                    if 'MaxChargePower' in updated_data:
                        bat_dict['MaxChargePower'] = updated_data['MaxChargePower']
                    # Power, energy and ageing of a battery the master keeps
                    # past its lifetime (_apply_master_battery_life)
                    for f in ('MaxDischargePower', 'rated_power', 'degradation_rate',
                              'initial_age', 'life_time'):
                        if f in updated_data:
                            bat_dict[f] = updated_data[f]
                    bat_to_convert = BatteryConfig(**bat_dict)

            bat_bus_map = bat_bus_per_node.get(key)
            bat_avail = self._storage_availability(
                battery_availability_key(key), bat.availability_file, num_nodes,
                resolution_hours, f"Battery {key!r}")
            jl_bat = convert_battery_config(bat_to_convert, bus_to_node=b2n_arg,
                                             bus_per_node=bat_bus_map,
                                             availability=bat_avail)
            jl.seval("push!")(jl_batteries, jl_bat)
            bat_order.append((key, bat_to_convert))

        # Create virtual batteries from investment units in units_config
        # These are created by _build_config_from_cumulative() for battery
        # technology investments from MasterProblem
        for key, vdata in self.units_config.items():
            if key in sys.batteries:
                continue  # Already handled above
            if vdata.get("_type") != "battery" and vdata.get("type") != "Storage":
                continue  # Not a battery
            vdata = _per_bus_unit_on_model_buses(vdata, self._reduction_map,
                                                 self.system_config.buses or [])
            cap = vdata.get("capacity", [])
            charge_pow = vdata.get("MaxChargePower", [])
            if not cap or (max(cap) < 0.01 and max(charge_pow) < 0.01):
                continue

            nn = len(cap)
            soc_init_default = vdata.get("soc_initial", [0.5] * nn)
            # Operating fields of the battery technology it was built from
            # (battery_technology_unit_fields)
            vbat = BatteryConfig(
                name=vdata.get("name", key),
                type="Storage",
                fuel="None",
                reservable=vdata.get("reservable", True),
                spillage=vdata.get("spillage", True),
                capacity=cap,
                MaxChargePower=charge_pow,
                MaxDischargePower=vdata.get("MaxDischargePower", charge_pow),
                efficiency_charge=vdata.get("efficiency_charge", [0.95] * nn),
                efficiency_discharge=vdata.get("efficiency_discharge", [0.95] * nn),
                soc_initial=soc_init_default,
                max_DoD=vdata.get("max_DoD", [0.9] * nn),
                rated_power=charge_pow,  # BatteryConfig requires rated_power
                ramp_up=vdata.get("ramp_up", [0.0] * nn),
                ramp_down=vdata.get("ramp_down", [0.0] * nn),
                fixed_cost=vdata.get("fixed_cost", [0.0] * nn),
                maintenance_cost=vdata.get("maintenance_cost", [0.0] * nn),
                inertia=vdata.get("inertia", [0.0] * nn),
                life_time=vdata.get("life_time", [999] * nn),
                initial_age=vdata.get("initial_age", [0] * nn),
                degradation_rate=vdata.get("degradation_rate", [0.0] * nn),
                decommissioning_cost=vdata.get("decommissioning_cost", [0.0] * nn),
                invest_cost=[0.0] * nn,
                invest_max_power=[0.0] * nn,
                throughput_degradation_cost=vdata.get("throughput_degradation_cost", [0.0] * nn),
                current_type=vdata.get("current_type", "DC"),
                availability_file=vdata.get("availability_file") or vdata.get("Availability"),
            )
            # Cached under the technology's file name by the runner
            v_avail = self._storage_availability(
                vbat.availability_file or "", vbat.availability_file, num_nodes,
                resolution_hours, f"Battery technology unit {key!r}")
            is_per_bus = vdata.get('_is_per_bus', False)
            if is_per_bus and b2n_arg is not None:
                # Arrays already per bus: lay the per-node profile on the buses
                if v_avail is not None:
                    v_avail = expand_node_to_bus_matrix(v_avail, bus_to_node_0idx)
                jl_vbat = convert_battery_config(vbat, bus_to_node=None,
                                                 availability=v_avail)
            else:
                # Virtual battery (master investment): per-node capacity
                # falls back to the node's first (canonical) bus.
                jl_vbat = convert_battery_config(vbat, bus_to_node=b2n_arg,
                                                 availability=v_avail)
            jl.seval("push!")(jl_batteries, jl_vbat)
            bat_order.append((key, vbat))
            logger.info(f"Virtual bat: {vdata.get('name', key)}, "
                       f"power={charge_pow} MW, capacity={cap} MWh")

        # Build cost curve dicts for generators and batteries
        jl_gen_cost_curves = build_gen_cost_curves_dict(
            gen_order, [], num_buses,
            bus_to_node=bus_to_node_0idx if need_expansion else None,
            gen_bus_per_node=gen_bus_per_node,
            per_bus_keys=per_bus_gen_keys,
        )
        jl_bat_cost_curves = build_bat_cost_curves_dict(
            bat_order, num_buses,
            bus_to_node=bus_to_node_0idx if need_expansion else None,
            bat_bus_per_node=bat_bus_per_node,
        )

        # Convert network (pass actual distances + per-line data + buses)
        jl_network = convert_network_config(
            sys.nodes, sys.dc_power_flow, sys.fuel_transport_distances,
            transmission_lines_geo=sys.transmission_lines_geo or None,
            transformers=sys.transformers or None,
            acdc_converters=getattr(sys, 'acdc_converters', None) or None,
            freq_converters=getattr(sys, 'freq_converters', None) or None,
            buses=sys.buses or None,
            s_base_mva=system_s_base(sys),
        )

        # Convert temporal config (already retrieved above)
        jl_temporal = convert_temporal_config(temporal, self.hours)

        # Get solver config
        solver = self.esfex_config.solver if self.esfex_config else SolverConfig()

        # Create CO2 factors dictionary from fuels config
        fuel_co2 = {}
        if sys.fuels:
            for fuel_name, fuel_config in sys.fuels.items():
                if fuel_config.emission_factor > 0:
                    fuel_co2[fuel_name] = fuel_config.emission_factor

        # Convert fuel_co2 to Julia Dict{String, Float64}
        jl_fuel_co2 = jl.seval("Dict{String, Float64}")(fuel_co2) if fuel_co2 else jl.seval("Dict{String, Float64}()")

        # Convert sectoral demand to Julia
        jl_sectoral = jl.seval("Dict{String, Matrix{Float64}}()")
        if self.sectoral_demand:
            for sector_name, sector_arr in self.sectoral_demand.items():
                jl_sectoral[sector_name] = py_to_julia_matrix(sector_arr)

        # Build sectoral criticality from config
        jl_crit = {}
        crit_map = {
            "critical": sys.criticality_penalties.critical,
            "high": sys.criticality_penalties.high,
            "medium": sys.criticality_penalties.medium,
            "low": sys.criticality_penalties.low,
        }
        # Flexible sectors can shift up to flexibility_ratio of their demand
        # by up to delay_tolerance hours.
        jl_shift_ratio = jl.seval("Dict{String, Float64}()")
        jl_shift_delay = jl.seval("Dict{String, Int}()")
        if sys.electric_demand:
            for sector_name, sector_cfg in sys.electric_demand.items():
                level = getattr(sector_cfg, 'criticality', 'medium')
                jl_crit[sector_name] = crit_map.get(level, crit_map['medium'])
                ratio = float(getattr(sector_cfg, 'flexibility_ratio', 0.0))
                delay = int(getattr(sector_cfg, 'delay_tolerance', 0))
                if getattr(sector_cfg, 'is_flexible', False) and ratio > 0 and delay > 0:
                    jl_shift_ratio[sector_name] = ratio
                    jl_shift_delay[sector_name] = delay
        jl_sectoral_criticality = jl.seval("Dict{String, Float64}()")
        for k, v in jl_crit.items():
            jl_sectoral_criticality[k] = float(v)

        # Build EV config if available
        jl_ev_config = jl.seval("nothing")
        ev_config_data = self.kwargs.get('ev_config_data')
        if ev_config_data is not None:
            # Compute initial SOC per node (MWh)
            num_vehicles = ev_config_data['num_vehicles']
            target_soc = float(ev_config_data.get('target_soc', 0.8))
            bat_cap_kwh = float(ev_config_data['battery_capacity_kwh'])
            # EV_initial_soc per node (it used to be ignored: always target_soc),
            # kept within the fleet's SOC limits
            lo = float(ev_config_data.get('min_soc', 0.0))
            hi = float(ev_config_data.get('max_soc', 1.0))
            fracs = ev_config_data.get('initial_soc_fraction') or [target_soc] * len(num_vehicles)
            initial_soc = [min(max(float(f), lo), hi) * bat_cap_kwh * nv / 1000.0
                           for f, nv in zip(fracs, num_vehicles)]

            jl_ev_config = ESFEX.EVConfig(
                py_to_julia_vector(num_vehicles),
                bat_cap_kwh,
                float(ev_config_data['max_charge_power_kw']),
                float(ev_config_data['max_discharge_power_kw']),
                float(ev_config_data['charge_efficiency']),
                float(ev_config_data['discharge_efficiency']),
                float(ev_config_data['min_soc']),
                float(ev_config_data.get('max_soc', 1.0)),
                target_soc,
                py_to_julia_matrix(ev_config_data['availability_profile']),
                py_to_julia_matrix(ev_config_data['driving_consumption_profile']),
                scale_cost(float(ev_config_data.get('v2g_compensation', 0.0))),
                scale_cost(float(ev_config_data.get('loss_penalty', sys.penalties.ev_loss))),
                py_to_julia_vector(initial_soc),
            )

        # Electrolyzers, in config order (results follow the same order).
        # The Julia electrolyzer is indexed by bus; its config lists are per
        # node. Map them like generators: capacity at the node's first bus,
        # properties replicated to every bus of the node.
        jl_electrolyzers = jl.seval("ElectrolyzerConfig[]")
        electrolyzers = getattr(sys, 'electrolyzers', None) or {}

        def _elz(values, mode="property"):
            values = [float(v) for v in values]
            if b2n_arg is None:
                return values
            return expand_node_to_bus_array(values, b2n_arg, mode=mode)

        for e in electrolyzers.values():
            jl_elz = ESFEX.ElectrolyzerConfig(
                py_to_julia_vector(_elz(e.rated_power, "capacity")),
                py_to_julia_vector(_elz(e.eff_at_rated)),
                py_to_julia_vector(_elz(e.eff_at_min)),
                float(e.energy_per_kg_h2),
                py_to_julia_vector(_elz(e.ramp_up)),
                py_to_julia_vector(_elz(e.ramp_down)),
                py_to_julia_vector(_elz(scale_cost_list(e.invest_cost))),
                py_to_julia_vector(_elz(e.invest_max_power, "capacity")),
                py_to_julia_vector(_elz(scale_cost_list(e.fixed_cost))),
                py_to_julia_vector(_elz(scale_cost_list(e.variable_cost))),
                scale_cost(float(e.water_cost)),
                py_to_julia_vector(_elz(e.life_time)),
            )
            jl.seval("push!")(jl_electrolyzers, jl_elz)

        # N-1 security: apply_to_modes lists simulation modes, so a
        # development run is "development" whatever its dispatch formulation
        n1 = self.esfex_config.n1_security if self.esfex_config else N1SecurityConfig()
        n1_enabled = n1.enabled and self.run_mode in n1.apply_to_modes

        # Rolling-horizon boundary conditions for the seam with the previous
        # window. Each boundary entry has shape (gen × bus) at the last
        # timestep of the previous window. Missing → Julia uses safe defaults
        # (status=0, no t=1 ramp constraint, configured reservoir initial).
        def _np2d_to_jl_gen_bus_dict(arr) -> Any:
            """numpy (gen × bus) → Julia Dict{Int, Dict{Int, Float64}}."""
            outer = jl.seval("Dict{Int, Dict{Int, Float64}}()")
            if arr is None:
                return outer
            a = np.asarray(arr, dtype=float)
            if a.ndim != 2:
                return outer
            n_gen, n_bus = a.shape
            for g in range(n_gen):
                inner = jl.seval("Dict{Int, Float64}()")
                for b in range(n_bus):
                    inner[b + 1] = float(a[g, b])
                outer[g + 1] = inner
            return outer

        def _to_model_buses(arr):
            # Boundary state (unit × bus) comes from results expanded to the
            # original buses; with network reduction this model is built on
            # the reduced ones, so keep the retained buses in reduced order.
            rm = self._reduction_map
            if (arr is not None and rm is not None and np.ndim(arr) == 2
                    and np.shape(arr)[1] == rm.n_original_buses):
                return np.asarray(arr)[:, rm.retained_original_indices]
            return arr

        jl_gen_status_init = _np2d_to_jl_gen_bus_dict(
            _to_model_buses(self.boundary_conditions.get('gen_status_init')))
        jl_gen_output_prev = _np2d_to_jl_gen_bus_dict(
            _to_model_buses(self.boundary_conditions.get('gen_output_prev')))
        jl_reservoir_level = _np2d_to_jl_gen_bus_dict(
            _to_model_buses(self.boundary_conditions.get('reservoir_level')))
        # Battery SOC carried from the previous window (battery × bus, MWh),
        # in Julia battery order (existing then virtual, as pushed above).
        jl_battery_soc_prev = _np2d_to_jl_gen_bus_dict(
            _to_model_buses(self.boundary_conditions.get('battery_soc')))
        # Battery discharge / charge (MW) at the previous window's last step,
        # for the batteries' ramp limits at t=1.
        jl_battery_discharge_prev = _np2d_to_jl_gen_bus_dict(
            _to_model_buses(self.boundary_conditions.get('battery_discharge_prev')))
        jl_battery_charge_prev = _np2d_to_jl_gen_bus_dict(
            _to_model_buses(self.boundary_conditions.get('battery_charge_prev')))
        # End-of-year SOC floor (battery × bus, MWh), set on the last window.
        jl_bat_soc_end_min = _np2d_to_jl_gen_bus_dict(
            _to_model_buses(self.boundary_conditions.get('battery_soc_end_min')))

        # Centralised deterministic-outage masks for non-generator elements,
        # each aligned to that element's Julia push order (generators fold their
        # outage into `availability` instead). Empty dict ⇒ no masking.
        jl_outage_masks = jl.seval("Dict{String, Any}()")
        # Batteries: position-indexed matrix (unfiltered push order).
        bat_mask = build_outage_mask_matrix(
            bat_windows_by_key,
            [k for k, _ in bat_order],
            outage_hour0, self.hours, resolution_hours,
        )
        if bat_mask is not None:
            jl.seval("setindex!")(
                jl_outage_masks, py_to_julia_matrix(bat_mask), "battery")
        # Lines: id-addressed (multi-stage filtered) → Dict{line_id → factor}.
        line_masks = build_outage_mask_by_id(
            outage_by_type.get("line", {}),
            outage_hour0, self.hours, resolution_hours,
        )
        if line_masks is not None:
            jl_line_masks = jl.seval("Dict{String, Vector{Float64}}()")
            for lid, fac in line_masks.items():
                jl.seval("setindex!")(
                    jl_line_masks, py_to_julia_vector(fac), str(lid))
            jl.seval("setindex!")(jl_outage_masks, jl_line_masks, "line")
        # Electrolyzers: position-indexed matrix in config order (whole unit).
        elz_windows = outage_by_type.get("electrolyzer", {})
        if any(n is not None for ns in outage_nodes.get("electrolyzer", {}).values() for n in ns):
            logger.warning("Outage window on one node of an electrolyzer: electrolyzer "
                           "outages apply to the whole unit.")
        elz_mask = build_outage_mask_matrix(
            elz_windows, list(electrolyzers.keys()),
            outage_hour0, self.hours, resolution_hours,
        )
        if elz_mask is not None:
            jl.seval("setindex!")(
                jl_outage_masks, py_to_julia_matrix(elz_mask), "electrolyzer")
        # Converters (AC/DC, frequency) and transformers: id-addressed by name.
        for _cat in ("acdc_converter", "freq_converter", "transformer"):
            _m = build_outage_mask_by_id(
                outage_by_type.get(_cat, {}),
                outage_hour0, self.hours, resolution_hours,
            )
            if _m is not None:
                _jl_m = jl.seval("Dict{String, Vector{Float64}}()")
                for _id, _fac in _m.items():
                    jl.seval("setindex!")(_jl_m, py_to_julia_vector(_fac), str(_id))
                jl.seval("setindex!")(jl_outage_masks, _jl_m, _cat)

        # Create input struct using keyword constructor
        jl_input = ESFEX.PowerSystemInput(
            name=sys.name,
            year=self.year,
            base_year=self.base_year,
            network=jl_network,
            generators=jl_generators,
            batteries=jl_batteries,
            demand=py_to_julia_matrix(self.demand),
            sectoral_demand=jl_sectoral,
            temporal=jl_temporal,
            loss_of_load_penalty=scale_cost(float(sys.penalties.loss_of_load)),
            loss_of_reserve_static=scale_cost(float(sys.penalties.loss_of_reserve_static)),
            loss_of_reserve_dynamic=scale_cost(float(sys.penalties.loss_of_reserve_dynamic)),
            co2_cost=scale_cost(float(sys.penalties.co2_cost)),
            curtailment_cost=scale_cost(float(getattr(sys.penalties, 'curtailment_cost', 20.0))),
            re_penetration_target=float(self.re_penetration_target_override or 0.0),
            co2_budget=float(sys.co2_budget.annual_budget) if sys.co2_budget.enabled else float("inf"),
            inertia_limit=float(sys.inertia_limit_threshold),
            mode=self.mode,
            solver_name=solver.name,
            threads=solver.threads,
            time_limit=float(solver.time_limit),
            gap=solver.gap,
            verbose=solver.verbose,
            solver_options=_solver_options_to_julia(solver.options, solver.name),
            outage_masks=jl_outage_masks,
            fuel_co2=jl_fuel_co2,
            ev_config=jl_ev_config,
            electrolyzers=jl_electrolyzers,
            sectoral_criticality=jl_sectoral_criticality,
            sectoral_delay_tolerance=jl_shift_delay,
            sectoral_flexibility_ratio=jl_shift_ratio,
            loss_of_inertia_penalty=scale_cost(float(sys.penalties.loss_of_inertia)),
            n1_security_enabled=n1_enabled,
            n1_transmission_enabled=n1.transmission_enabled,
            n1_generation_enabled=n1.generation_enabled,
            n1_transmission_reserve_factor=n1.transmission_reserve_factor,
            n1_generation_reserve_type=n1.generation_reserve_type,
            n1_generation_reserve_percentage=n1.generation_reserve_percentage,
            n1_scopf_enabled=n1.scopf_enabled,
            n1_corrective_enabled=n1.corrective_enabled,
            # Rolling-horizon seam: per-window-N initial state from window N-1.
            # All three default to empty Dict (no boundary) → Julia preserves
            # legacy behaviour for the first window of every year.
            generator_initial_status=jl_gen_status_init,
            generator_output_prev=jl_gen_output_prev,
            reservoir_level_prev=jl_reservoir_level,
            battery_soc_prev=jl_battery_soc_prev,
            battery_discharge_prev=jl_battery_discharge_prev,
            battery_charge_prev=jl_battery_charge_prev,
            # Penalty coefficients from config (B4)
            fre_penetration_penalty=scale_cost(float(getattr(sys.penalties, 'fre_penetration_loss', 100.0))),
            transfer_margin_penalty=scale_cost(float(getattr(sys.penalties, 'transfer_margin', 0.0))),
            rooftop_curtailment_penalty=scale_cost(float(getattr(sys.penalties, 'rooftop_curtailment', 5.0))),
            co2_budget_violation_penalty=scale_cost(float(getattr(sys.penalties, 'co2_budget_violation', 500.0))),
            # Demand constraints — load shedding bounded to fraction of demand
            max_annual_system_cost=scale_cost(float(getattr(sys, 'max_annual_system_cost', float('inf')))),
            # Electricity price for time-varying V2G compensation and flex demand benefit
            electricity_price=py_to_julia_vector(
                [p * COST_SCALE for p in self.kwargs.get('electricity_price', [])[:self.hours]]
                if self.kwargs.get('electricity_price') else []
            ),
            # Configurable parameters (previously hardcoded)
            soc_end_tolerance=float(getattr(sys, 'soc_end_tolerance', 0.05)),
            # Operational rolling windows chain SOC across windows via boundary
            # conditions; the per-window cyclic constraint must be off. Only the
            # year's last window gets a (soft) floor back to the year-start SOC.
            cyclic_end_soc=False,
            bat_soc_end_min=jl_bat_soc_end_min,
            bat_soc_end_min_cyclic=bool(self.boundary_conditions.get('battery_soc_end_cyclic', False)),
            min_cycling_ratio=float(getattr(sys, 'min_cycling_ratio', 0.8)),
            min_cycling_period_days=float(getattr(sys, 'min_cycling_period_days', 7.0)),
            reserve_static_default_ratio=float(getattr(sys, 'reserve_static_default_ratio', 0.15)),
            reserve_static_requirement=_build_reserve_requirement_dict(
                list(getattr(sys.nodes, 'reserve_static', []) or [])
            ),
            reserve_dynamic_requirement=_build_reserve_requirement_dict(
                list(getattr(sys.nodes, 'reserve_dynamic', []) or [])
            ),
            soc_violation_penalty=scale_cost(float(getattr(sys.penalties, 'soc_violation', 1e6))),
            demand_shift_cost_rate=scale_cost(float(getattr(sys, 'demand_shift_cost_rate', 0.1))),
            dynamic_reserve_contribution=float(getattr(sys, 'dynamic_reserve_contribution', 0.5)),
            # PWL transmission loss model
            pwl_loss_segments=_pwl_segments_from_config(sys.dc_power_flow),
            # Rooftop solar generation (hours × nodes matrix, or nothing)
            rooftop_generation=py_to_julia_matrix(self.rooftop_generation) if self.rooftop_generation is not None else jl.seval("nothing"),
            # Bidding/offer curves (PWL cost decomposition)
            gen_cost_curves=jl_gen_cost_curves,
            bat_cost_curves=jl_bat_cost_curves,
            # ACOPF configuration
            power_flow_mode=str(getattr(sys, 'power_flow_mode', 'dcopf')),
            acopf_base_mva=float(getattr(sys.ac_power_flow, 'base_mva', 100.0)),
            acopf_v_min=float(getattr(sys.ac_power_flow, 'voltage_min_pu', 0.90)),
            acopf_v_max=float(getattr(sys.ac_power_flow, 'voltage_max_pu', 1.10)),
            acopf_default_power_factor=float(getattr(sys.ac_power_flow, 'default_power_factor', 0.85)),
            acopf_load_power_factor=float(getattr(sys.ac_power_flow, 'load_power_factor', 0.9)),
            acopf_q_slack_penalty=scale_cost(float(getattr(sys.ac_power_flow, 'q_slack_penalty', 100.0))),
            acopf_min_reactance_pu=float(getattr(sys.ac_power_flow, 'min_reactance_pu', 0.01)),
            acopf_tap_ratio_min=float(getattr(sys.ac_power_flow, 'tap_ratio_min', 0.5)),
            acopf_tap_ratio_max=float(getattr(sys.ac_power_flow, 'tap_ratio_max', 2.0)),
            acopf_q_min_ratio=float(getattr(sys.ac_power_flow, 'q_min_ratio', 0.5)),
            gen_q_limits=_build_gen_q_limits(sys),
            gen_q_limits_min=_build_gen_q_limits_min(sys),
            co2_budget_groups=self._co2_budget_groups(sys, ESFEX),
        )

        return jl_input

    @staticmethod
    def _co2_budget_groups(sys, ESFEX):
        """Per-system CO2 budgets of a merged run as ``CO2BudgetGroup``s.

        Empty for a single system: the dispatch then uses ``co2_budget`` and
        ``co2_budget_violation_penalty`` over all nodes.
        """
        jl = get_julia()
        groups = jl.seval("CO2BudgetGroup[]")
        for b in getattr(sys, "co2_budget_by_system", None) or []:
            nodes = ([b.first_node + k + 1 for k in range(b.num_nodes)]  # 1-indexed
                     + [n + 1 for n in b.extra_nodes])
            jl.seval("push!")(groups, ESFEX.CO2BudgetGroup(
                str(b.system),
                py_to_julia_int_vector(nodes),
                float(b.annual_budget) if b.enabled else float("inf"),
                scale_cost(float(b.violation_penalty)),
            ))
        return groups

    def write_lp(self, filepath: str) -> None:
        """
        Export the model to LP format for debugging.

        Args:
            filepath: Path to write the LP file
        """
        jl = get_julia()

        if self._jl_model is None:
            raise RuntimeError("Model not built. Call build_model() first.")

        jl.seval("global _ps_model_export")
        jl._ps_model_export = self._jl_model

        jl.seval("using JuMP")
        jl.seval(f'write_to_file(_ps_model_export, "{filepath.replace(chr(92), "/")}")')

        logger.debug(f"PowerSystem LP exported to: {filepath}")

    def solve(self) -> int:
        """
        Solve the optimization model.

        Returns:
            PuLP-compatible status code (1 = optimal)
        """
        import time as _time
        jl = get_julia()

        if self._jl_model is None:
            raise RuntimeError("Model not built. Call build_model() first.")

        logger.debug("Solving Julia model...")

        # Store model in Julia Main namespace for direct optimization
        jl.seval("global _ps_model")
        jl._ps_model = self._jl_model

        # Solve using JuMP. No retries, no fallbacks: if the configured solver
        # cannot prove optimality, that is a real formulation/conditioning bug
        # and must be surfaced — masking it with a simplex fallback hides
        # silent errors that are extremely hard to debug later.
        jl.seval("using JuMP")
        t0 = _time.perf_counter()
        jl.seval("optimize!(_ps_model)")
        t_solve = _time.perf_counter() - t0
        logger.info(f"⏱ PowerSystem solve: {t_solve:.2f}s")

        status_code = self._map_termination_status(jl)

        # Cost of N-1 security (opt-in): re-solve a copy without the N-1
        # constraints; extract_solution reports the objective difference.
        if status_code == 1 and self._n1_security_cost_requested():
            try:
                t0 = _time.perf_counter()
                cost = float(jl.seval("compute_n1_security_cost!")(
                    self._jl_model, self._jl_input))
                logger.info(
                    f"⏱ N-1 security cost re-solve: {_time.perf_counter() - t0:.2f}s "
                    f"(cost={cost:.6g} M$)")
            except Exception as exc:
                logger.warning(f"N-1 security cost skipped due to bridge error: {exc}")

        # UC dual recovery (Ruta A v2): MIPs have no duals, so a UC solve
        # otherwise produces ``prices = 0`` in the HDF5 export. We clone
        # the model (``recover_uc_duals_via_copy``), fix ``gen_status``
        # at the MIP incumbent on the copy, re-solve the copy as LP, and
        # read balance-constraint duals there. The original model is not
        # mutated, so downstream ``extract_solution`` continues to work
        # exactly as before.
        #
        # No-ops for LP runs (development / economic_dispatch) because
        # ``vars.gen_status === nothing`` then. For UC runs, returns
        # ``None`` on any failure path → ``self._recovered_prices``
        # stays ``None`` → ``get_solution_values`` doesn't override the
        # zeros and the HDF5 export looks the same as before this fix.
        self._recovered_prices = None
        if status_code == 1 and self._jl_vars is not None:
            try:
                ESFEX = get_esfex_module()
                t0 = _time.perf_counter()
                jl_prices = ESFEX.recover_uc_duals_via_copy(
                    self._jl_model, self._jl_vars, self._jl_input,
                )
                t_recover = _time.perf_counter() - t0
                if jl_prices is not None:
                    import numpy as _np
                    self._recovered_prices = _np.asarray(jl_prices)
                    logger.info(
                        f"⏱ UC dual recovery: {t_recover:.2f}s "
                        f"(prices shape={self._recovered_prices.shape})"
                    )
            except Exception as exc:
                logger.warning(
                    f"UC dual recovery skipped due to bridge error: {exc}"
                )

        return status_code

    def _n1_security_cost_requested(self) -> bool:
        """``n1_security.compute_security_cost`` is on and N-1 applies to this run's mode."""
        n1 = getattr(self.esfex_config, "n1_security", None) if self.esfex_config else None
        return bool(n1 is not None and n1.enabled and n1.compute_security_cost
                    and self.run_mode in n1.apply_to_modes)

    def _map_termination_status(self, jl) -> int:
        """Map JuMP termination status to PuLP-compatible code.

        ``OPTIMAL`` / ``LOCALLY_SOLVED`` return success. A solver that stops at
        a resource LIMIT (time / nodes / iterations) but holds a FEASIBLE primal
        point is also accepted: an operational dispatch that meets demand is a
        valid result, and discarding it turns a slow-but-solved window into a
        total run failure (Japan's 72 h UC would grind the last ~2 % of the MIP
        gap for the whole time limit, then fail — throwing away a usable
        dispatch). The optimality gap is logged loudly so the result is not
        mistaken for a proven optimum; loosen ``solver.gap`` or raise
        ``solver.time_limit`` to close it. INFEASIBLE / UNBOUNDED stay failures.
        """
        status = jl.seval("termination_status(_ps_model)")
        status_str = str(status)
        logger.info(f"Solve completed with status: {status_str}")

        if "OPTIMAL" in status_str or "LOCALLY_SOLVED" in status_str:
            return 1
        elif "DUAL_INFEASIBLE" in status_str or "UNBOUNDED" in status_str:
            return -2
        elif "INFEASIBLE" in status_str:
            self._dump_iis_if_requested(jl, "_ps_model")
            return -1

        # Accept a feasible incumbent found before a resource limit was hit.
        _LIMITS = ("TIME_LIMIT", "NODE_LIMIT", "ITERATION_LIMIT",
                   "SOLUTION_LIMIT", "OBJECTIVE_LIMIT", "OTHER_LIMIT")
        try:
            primal_status = str(jl.seval("primal_status(_ps_model)"))
        except Exception:
            primal_status = "NO_SOLUTION"
        if "FEASIBLE_POINT" in primal_status and any(
            lim in status_str for lim in _LIMITS
        ):
            try:
                obj = float(jl.seval("objective_value(_ps_model)"))
                bound = float(jl.seval("objective_bound(_ps_model)"))
                gap = abs(obj - bound) / (abs(obj) + 1e-9)
                logger.warning(
                    "Solver stopped at %s with a FEASIBLE incumbent — accepting "
                    "it (objective=%.6g, bound=%.6g, gap=%.2f%%). This dispatch "
                    "is valid but not proven optimal; loosen solver.gap or raise "
                    "solver.time_limit to close the gap.",
                    status_str, obj, bound, gap * 100.0,
                )
            except Exception:
                logger.warning(
                    "Solver stopped at %s with a FEASIBLE incumbent — accepting "
                    "it (optimality gap unknown). Loosen solver.gap or raise "
                    "solver.time_limit to close it.", status_str,
                )
            return 1

        # Diagnose anything else as a failure (no fallback, no acceptance)
        try:
            dual_status = str(jl.seval("dual_status(_ps_model)"))
            logger.error(
                f"Solver failed with non-optimal termination: {status_str}, "
                f"primal={primal_status}, dual={dual_status}"
            )
        except Exception:
            pass
        return 0

    def _dump_iis_if_requested(self, jl, model_var: str) -> None:
        """Compute Gurobi IIS for an infeasible JuMP model and log constraints.

        Triggered when env ``ESFEX_DUMP_IIS=1`` is set. Only runs once per
        process to keep logs small; subsequent infeasibilities are skipped.
        """
        import os
        if os.environ.get("ESFEX_DUMP_IIS") != "1":
            return
        if getattr(self.__class__, "_iis_already_dumped", False):
            logger.warning("IIS already dumped earlier in this process — skipping")
            return
        try:
            from datetime import datetime
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_dir = os.environ.get("ESFEX_IIS_DIR", "/tmp")
            os.makedirs(out_dir, exist_ok=True)
            lp_path = os.path.join(out_dir, f"esfex_infeasible_{ts}.lp")
            jl.seval(f'write_to_file({model_var}, "{lp_path}")')
            logger.error(f"Infeasible LP written to: {lp_path}")
            jl.seval(f"compute_conflict!({model_var})")
            jl.seval(f"""
                _conf_cons = String[]
                for (F, S) in list_of_constraint_types({model_var})
                    for c in all_constraints({model_var}, F, S)
                        st = MOI.get({model_var}, MOI.ConstraintConflictStatus(), c)
                        if st == MOI.IN_CONFLICT
                            push!(_conf_cons, JuMP.name(c))
                        end
                    end
                end
            """)
            n_conf = int(jl.seval("length(_conf_cons)"))
            logger.error(f"IIS contains {n_conf} constraints in conflict")
            if n_conf > 0:
                limit = min(n_conf, 80)
                names = jl.seval(f"_conf_cons[1:{limit}]")
                logger.error("Sample IIS constraints:")
                for nm in names:
                    logger.error(f"  IIS: {nm}")
                iis_path = os.path.join(out_dir, f"esfex_iis_{ts}.txt")
                with open(iis_path, "w") as fh:
                    for nm in jl.seval("_conf_cons"):
                        fh.write(str(nm) + "\n")
                logger.error(f"Full IIS constraint list written to: {iis_path}")
            self.__class__._iis_already_dumped = True
        except Exception as e:
            logger.error(f"Failed to dump IIS: {e}")

    def get_solution_values(self, keep_steps: Optional[int] = None,
                            primary_energy: Optional["PrimaryEnergyAdapter"] = None,
                            ) -> Dict[str, Any]:
        """
        Extract solution values from the solved model.

        Args:
            keep_steps: Steps of a rolling-horizon window that are committed
                (the next window solves the rest again). The objective, the
                scalar totals and ``cost_breakdown`` then cover only those
                steps; the arrays still span the window. Default: all steps.
            primary_energy: The primary-energy block solved in the same
                model, so its costs are split by step too.

        Returns:
            Dictionary with solution values
        """
        import time as _time
        ESFEX = get_esfex_module()

        if self._jl_model is None:
            raise RuntimeError("Model not built. Call build_model() first.")

        committed = None
        n_steps = int(self._jl_input.temporal.hours)
        if keep_steps is not None and 0 < int(keep_steps) < n_steps:
            pe_vars = getattr(primary_energy, "_jl_vars", None)
            pe_temporal = getattr(primary_energy, "_jl_temporal", None)
            committed = ESFEX.committed_steps(
                self._jl_vars, n_steps, int(keep_steps),
                pe_vars=pe_vars, pe_temporal=pe_temporal)

        # Extract solution using Julia function
        t0 = _time.perf_counter()
        jl_result = ESFEX.extract_solution(self._jl_model, self._jl_vars, self._jl_input,
                                           committed=committed)
        t_extract = _time.perf_counter() - t0

        # Convert to Python dictionary
        t0 = _time.perf_counter()
        result = convert_power_system_result(jl_result)
        t_convert = _time.perf_counter() - t0

        # Override LMPs with the ones recovered via UC fix-and-resolve
        # on a copied model (see ``solve``). In UC runs ``energy_prices``
        # from ``extract_solution`` is all zeros because the MIP has no
        # duals; the copy-based LP solve gave us a meaningful matrix.
        #
        # UNIT CONVERSION: ``recover_uc_duals_via_copy`` returns raw duals
        # in M$/MWh (Julia's internal scale; see types.jl:11). The LP
        # path of ``extract_solution`` returns the same scale, and the
        # ``convert_power_system_result`` step above already multiplied
        # ``result["energy_prices"]`` by ``COST_UNSCALE = 1e6`` so it
        # lands in real USD/MWh. To keep the override consistent with
        # the rest of the pipeline (HDF5 export expects USD/MWh), we
        # apply the same unscale here. Without this, prices were
        # written as ~0.05 (the M$/MWh value) instead of ~50 USD/MWh.
        recovered_prices = getattr(self, "_recovered_prices", None)
        if recovered_prices is not None:
            import numpy as _np
            from esfex.bridge.converters import COST_UNSCALE
            recovered_unscaled = _np.asarray(recovered_prices) * COST_UNSCALE
            expected = result.get("energy_prices")
            if expected is None:
                result["energy_prices"] = recovered_unscaled
            elif _np.asarray(expected).shape == recovered_unscaled.shape:
                result["energy_prices"] = recovered_unscaled
            else:
                logger.warning(
                    "UC recovered prices shape %s does not match "
                    "expected %s — keeping the zero matrix from MIP",
                    recovered_unscaled.shape, _np.asarray(expected).shape,
                )

        _add_unattached_load_shed(result, self.demand,
                                  getattr(self, "_unattached_nodes", None),
                                  self._jl_input, keep_steps)

        # Expand results back to the original bus/line topology when
        # network reduction was applied.  All per-bus arrays (generation,
        # battery dispatch, voltage angles, prices) are padded to the
        # original bus count; per-line arrays are remapped with direction
        # and admittance-share preserved.
        t0 = _time.perf_counter()
        if self._reduction_map is not None:
            from esfex.topology import ResultExpander
            expander = ResultExpander(self._reduction_map)
            result = _expand_powersystem_result(result, expander)
        t_expand = _time.perf_counter() - t0

        logger.info(
            f"⏱ PowerSystem results: extract={t_extract:.2f}s, "
            f"convert={t_convert:.2f}s, expand={t_expand:.2f}s"
        )

        return result

    def get_objective_value(self) -> float:
        """Get the objective function value (unscaled from M$ back to $)."""
        jl = get_julia()

        if self._jl_model is None:
            return float("nan")

        jl._ps_model = self._jl_model
        return float(jl.seval("objective_value(_ps_model)")) * COST_UNSCALE


def _master_co2_budget_groups(sys, ESFEX):
    """The master's annual CO2 budgets as ``CO2BudgetGroup``s, as the dispatch
    sets them: one per system of a merged run (``co2_budget_by_system``), or
    one over all nodes from ``co2_budget`` and
    ``penalties.co2_budget_violation``. Empty when no budget is enabled.
    """
    jl = get_julia()
    if getattr(sys, "co2_budget_by_system", None):
        return PowerSystemAdapter._co2_budget_groups(sys, ESFEX)
    groups = jl.seval("CO2BudgetGroup[]")
    budget = getattr(sys, "co2_budget", None)
    if budget is not None and budget.enabled:
        jl.seval("push!")(groups, ESFEX.CO2BudgetGroup(
            str(sys.name),
            py_to_julia_int_vector(list(range(1, sys.num_nodes + 1))),
            float(budget.annual_budget),
            scale_cost(float(getattr(sys.penalties, 'co2_budget_violation', 500.0))),
        ))
    return groups


class MasterProblemAdapter:
    """
    Python adapter for the Julia MasterProblem optimization model.

    Handles capacity expansion decisions across the planning horizon.
    """

    def __init__(
        self,
        config: Union[ESFEXConfig, SystemConfig],
        years: List[int],
        base_year: int,
        demand: np.ndarray,
        demand_growth: float = 0.0,
        discount_rate: float = 0.05,
        max_annual_investment: float = 1e9,
        target_re_penetration: float = 0.5,
        initial_re_penetration: float = 0.1,
        min_re_increment: float = 0.0,
        max_re_increment: float = 1.0,
        life_extension_cost_factor: Optional[float] = None,
        decommissioning_cost_factor: float = 0.1,
        temporal_resolution_hours: int = 24,
        representative_days_per_year: int = 12,
        min_day_separation: int = 7,
        investment_resolution_hours: int = 8760,
        # TSAM parameters
        use_tsam: bool = False,
        tsam_period_start_hours: Optional[List[List[int]]] = None,
        tsam_period_weights: Optional[List[List[float]]] = None,
        tsam_chronological_order: Optional[List[List[int]]] = None,
        tsam_inter_period_linking: bool = True,
        # Stochastic parameters
        use_stochastic: bool = False,
        stochastic_scenarios: Optional[List[dict]] = None,
        # Per-system node ranges for per-system RE constraints
        # List of (name, first_bus_1indexed, num_buses, initial_re)
        system_node_ranges: Optional[List[tuple]] = None,
        # RE targets {(system_1idx, year_1idx): share} replacing the RE-1
        # trajectory (myopic mode: each year's target from the full horizon)
        re_target_override: Optional[dict] = None,
        # Config path for resolving relative file paths (availability, inflow)
        config_path: Optional[str] = None,
        # Pre-loaded availability cache (includes zone-extended profiles)
        availability_cache: Optional[Dict[str, np.ndarray]] = None,
        # Pre-loaded reservoir inflow cache {gen_key: hourly array}
        inflow_cache: Optional[Dict[str, np.ndarray]] = None,
        # Timestamp of the first data row ("DD/MM/YYYY HH:MM") for profiles
        # read from disk (Feb 29 rows are dropped by this calendar)
        date_start: Optional[str] = None,
        # Horizon hour of the first planned hour: profiles start there
        # (myopic masters plan one later year at a time)
        profile_start_hour: int = 0,
        # Master-problem solver method and Benders settings
        solver_method: str = "monolithic",
        benders_max_iterations: int = 50,
        benders_tolerance: float = 1e-4,
        # NPV-based retirement (system setting npv_retirement): units an
        # earlier myopic year retired [(kind, unit key, bus)], kept retired
        # here without paying their decommissioning again, and the unit
        # keys whose NPV is checked (None: every existing unit)
        retired_units: Optional[List[tuple]] = None,
        npv_unit_keys: Optional[set] = None,
        npv_horizon_end: Optional[int] = None,
        **kwargs,
    ):
        """
        Initialize the MasterProblem adapter.

        Args:
            config: ESFEX or System configuration
            years: List of simulation years
            base_year: Base year for NPV calculations
            demand: Base demand array (hours x nodes)
            demand_growth: Extra annual growth applied on top of each year's
                demand (default 0: the demand array already holds every year)
            discount_rate: Discount rate for NPV
            max_annual_investment: Maximum annual investment budget
            target_re_penetration: Target RE penetration ratio
            initial_re_penetration: Initial RE penetration ratio
            min_re_increment: Minimum annual RE penetration increment
            max_re_increment: Maximum annual RE penetration increment
            re_target_override: RE targets {(system, year): share}, both
                1-based, that replace the RE-1 trajectory (myopic mode)
            life_extension_cost_factor: Cost factor for life extension
                (default: the system's life_extension_cost_factor)
            decommissioning_cost_factor: Cost factor for decommissioning
            temporal_resolution_hours: Hours per timestep
            investment_resolution_hours: Hours per investment period (8760=annual)
            representative_days_per_year: Number of representative days
            min_day_separation: Minimum days between representative days
            use_tsam: Enable TSAM clustering
            tsam_period_start_hours: Per-year list of period start hours (1-indexed)
            tsam_period_weights: Per-year list of period weights
            tsam_chronological_order: Per-year list of chronological period indices (1-indexed)
            tsam_inter_period_linking: Enable inter-period SOC linking
            use_stochastic: Enable stochastic two-stage optimization
            stochastic_scenarios: List of scenario dicts with name/probability/multipliers
            config_path: Path to config file for resolving relative availability/inflow paths
            availability_cache: Pre-loaded hourly availability profiles {key: array},
                the ones the dispatch uses (the runner's cache)
            inflow_cache: Pre-loaded hourly reservoir inflows {gen_key: array}
            date_start: First data row's timestamp, for profiles read from disk
            profile_start_hour: Horizon hour (year index × 8760 + hour) of the
                first planned hour; every profile is rotated to start there,
                wrapping around when it is shorter, as in the dispatch
            retired_units: Units already retired by the NPV iteration of an
                earlier (myopic) year, as ``(kind, unit key, bus)`` with kind
                "generator" or "battery" and the 1-based master bus; they stay
                retired, without paying their decommissioning again
            npv_unit_keys: Generator and battery keys whose NPV the NPV
                retirement checks (default: every existing unit)
            npv_horizon_end: Last year of the planning horizon when it goes
                past ``years`` (a myopic year's master): the NPV also covers
                the unit's remaining in-service years up to it, its last
                modelled year's operation held (Julia ``extra_unit_npv``)
            **kwargs: Additional parameters (system_config: explicit SystemConfig override)
        """
        system_config_override = kwargs.pop("system_config", None)
        if isinstance(config, ESFEXConfig):
            self.esfex_config = config
            self.system_config = system_config_override or config.primary_system
        else:
            self.esfex_config = None
            self.system_config = system_config_override or config

        self._config_path = config_path
        self._availability_cache = availability_cache or {}
        self._inflow_cache = inflow_cache or {}
        self._date_start = date_start
        self._profile_start_hour = int(profile_start_hour or 0)

        self.years = years
        self.base_year = base_year
        self.demand = demand
        self.demand_growth = demand_growth
        self.discount_rate = discount_rate
        self.max_annual_investment = max_annual_investment
        self.target_re_penetration = target_re_penetration
        self.initial_re_penetration = initial_re_penetration
        self.min_re_increment = min_re_increment
        self.max_re_increment = max_re_increment
        self.system_node_ranges = system_node_ranges or []
        self.re_target_override = re_target_override or {}
        self.life_extension_cost_factor = life_extension_cost_factor
        self.decommissioning_cost_factor = decommissioning_cost_factor

        # Reserve parameters for master problem operational constraints
        self.reserve_static_default_ratio = float(getattr(
            self.system_config, 'reserve_static_default_ratio', 0.15))
        self.dynamic_reserve_contribution = float(getattr(
            self.system_config, 'dynamic_reserve_contribution', 0.5))
        # Penalty values from config penalties
        penalties = getattr(self.system_config, 'penalties', None)
        if penalties is not None:
            self.loss_of_reserve_static = float(getattr(
                penalties, 'Loss_of_reserve_static',
                getattr(penalties, 'loss_of_reserve_static', 1e4)))
            self.loss_of_reserve_dynamic = float(getattr(
                penalties, 'Loss_of_reserve_dynamic',
                getattr(penalties, 'loss_of_reserve_dynamic', 1e4)))
            self.loss_of_inertia_penalty = float(getattr(
                penalties, 'Loss_of_inertia',
                getattr(penalties, 'loss_of_inertia', 1e4)))
        else:
            self.loss_of_reserve_static = 1e4
            self.loss_of_reserve_dynamic = 1e4
            self.loss_of_inertia_penalty = 1e4

        # Inertia
        self.inertia_limit = float(getattr(
            self.system_config, 'INERTIA_LIMIT',
            getattr(self.system_config, 'inertia_limit', 0.0)))

        # Life extension: the system's life_extension_cost_factor unless the
        # caller overrides it (the runner never passed it, so the config key
        # had no effect and a hard-coded 0.3 applied).
        if self.life_extension_cost_factor is None:
            self.life_extension_cost_factor = float(getattr(
                self.system_config, 'life_extension_cost_factor', 0.20))

        # CO2: same sources as the dispatch (fuels' emission factors and
        # penalties.co2_cost), so the plan sees the carbon cost it will pay.
        fuels = getattr(self.system_config, 'fuels', None) or {}
        self.fuel_co2 = {
            name: float(f.emission_factor)
            for name, f in fuels.items() if f.emission_factor > 0
        }
        self.co2_cost = float(getattr(penalties, 'co2_cost', 0.0)) if penalties is not None else 0.0

        # Transmission line data for DC power flow in master problem
        self._build_transmission_line_data()

        self.temporal_resolution_hours = temporal_resolution_hours
        self.representative_days_per_year = representative_days_per_year
        self.min_day_separation = min_day_separation
        self.investment_resolution_hours = investment_resolution_hours
        self.use_tsam = use_tsam
        self.tsam_period_start_hours = tsam_period_start_hours or []
        self.tsam_period_weights = tsam_period_weights or []
        self.tsam_chronological_order = tsam_chronological_order or []
        self.tsam_inter_period_linking = tsam_inter_period_linking
        self.use_stochastic = use_stochastic
        self.stochastic_scenarios = stochastic_scenarios or []
        self.kwargs = kwargs

        # Master-problem solver method ("monolithic" | "benders") and settings.
        self.solver_method = str(solver_method or "monolithic").lower()
        self.benders_max_iterations = int(benders_max_iterations)
        self.benders_tolerance = float(benders_tolerance)

        self._jl_model = None
        self._jl_vars = None
        self._jl_input = None
        self._jl_targets = None
        self._jl_scenarios = None
        # Benders state (populated only when solver_method == "benders")
        self._benders_result = None
        self._benders_solution = None
        self._benders_use_rep_days = True
        self._use_rep_days = True

        # NPV-based retirement: settings of the system, the units earlier
        # years retired, and the ones this master retires
        # (npv_retirements: dicts, see _solve_npv_retirement)
        sc = self.system_config
        self.npv_retirement = bool(getattr(sc, 'npv_retirement', False))
        self.npv_max_iterations = int(getattr(sc, 'npv_retirement_max_iterations', 5))
        self.npv_threshold = float(getattr(sc, 'npv_retirement_threshold', 0.0))
        self._retired_carried = list(retired_units or [])
        self._npv_unit_keys = set(npv_unit_keys) if npv_unit_keys is not None else None
        self.npv_extra_years = (max(0, int(npv_horizon_end) - int(years[-1]))
                                if npv_horizon_end is not None and years else 0)
        self.npv_retirements: List[Dict[str, Any]] = []
        self._last_status = ""

        # Get solver config
        self._solver = self.esfex_config.solver if self.esfex_config else SolverConfig()

        logger.debug(f"MasterProblemAdapter initialized: {len(years)} years, base_year={base_year}")

    def _build_transmission_line_data(self):
        """Extract transmission line data from network config for DC power flow."""
        self.transmission_lines = []
        self.transmission_reactances = []
        self.transmission_capacities = []
        self.transmission_resistances = []

        network = self.system_config.network if hasattr(self.system_config, 'network') else None
        if network is None:
            return

        # Try per-line data first (transmission_lines_geo or transmission_lines)
        tl_data = getattr(network, 'transmission_lines', None)
        if tl_data and hasattr(tl_data, '__iter__'):
            for line in tl_data:
                from_bus = getattr(line, 'from_bus', None)
                to_bus = getattr(line, 'to_bus', None)
                if from_bus is not None and to_bus is not None:
                    reactance = float(getattr(line, 'reactance_pu',
                        getattr(line, 'reactance', 0.05)))
                    resistance = float(getattr(line, 'resistance_pu',
                        getattr(line, 'resistance', reactance * 0.1)))
                    capacity = float(getattr(line, 'capacity_mw',
                        getattr(line, 'capacity', 100.0)))
                    # Julia is 1-indexed
                    self.transmission_lines.append(
                        (int(from_bus) + 1, int(to_bus) + 1))
                    self.transmission_reactances.append(reactance)
                    self.transmission_resistances.append(resistance)
                    self.transmission_capacities.append(capacity)

        # Fallback: build from adjacency matrix
        if not self.transmission_lines:
            connections = getattr(network, 'connections', None)
            if connections is not None:
                if isinstance(connections, np.ndarray):
                    n = connections.shape[0]
                    for i in range(n):
                        for j in range(i + 1, n):
                            if connections[i, j] > 0:
                                # Julia is 1-indexed
                                self.transmission_lines.append((i + 1, j + 1))
                                impedance = getattr(network, 'impedance_pu', None)
                                if impedance is not None and isinstance(impedance, np.ndarray):
                                    x = float(impedance[i, j]) if impedance[i, j] > 0 else 0.05
                                    self.transmission_reactances.append(x)
                                    self.transmission_resistances.append(x * 0.1)
                                else:
                                    self.transmission_reactances.append(0.05)
                                    self.transmission_resistances.append(0.005)
                                self.transmission_capacities.append(float(connections[i, j]))

    def _build_jl_system_node_ranges(self, jl: Any) -> Any:
        """Convert system_node_ranges to Julia Vector{SystemNodeRange}."""
        ESFEX = get_esfex_module()
        jl_ranges = jl.seval("SystemNodeRange[]")
        for name, first_bus, num_buses, initial_re in self.system_node_ranges:
            jl.seval("push!")(jl_ranges, ESFEX.SystemNodeRange(
                str(name), int(first_bus), int(num_buses), float(initial_re)
            ))
        return jl_ranges

    def _build_jl_transmission_lines(self, jl: Any) -> Any:
        """Convert transmission_lines list of (int,int) tuples to Julia Vector{Tuple{Int64,Int64}}."""
        if not self.transmission_lines:
            return jl.seval("Tuple{Int64,Int64}[]")
        pairs_str = ", ".join(f"({f}, {t})" for f, t in self.transmission_lines)
        return jl.seval(f"Tuple{{Int64,Int64}}[{pairs_str}]")

    def _master_profile(self, cache: Dict[str, np.ndarray], key: str,
                        file_name: Optional[str], num_nodes: int, loader,
                        required: bool = True) -> Optional[np.ndarray]:
        """Hourly profile of one unit for the master, at its resolution.

        Takes the dispatch's preloaded profile (``cache[key]``) when it has
        one with ``num_nodes`` columns, else reads ``file_name`` with
        ``loader`` and the run's ``date_start`` (so Feb 29 is dropped by the
        data's calendar, as in the dispatch). A missing file gives None when
        not ``required``. The profile is rotated to start at
        ``profile_start_hour`` (wrapping around) and averaged to the master's
        resolution.
        """
        prof = cache.get(key)
        if prof is not None and (np.ndim(prof) != 2 or np.shape(prof)[1] != num_nodes):
            prof = None
        if prof is None:
            if not file_name:
                return None
            from pathlib import Path
            config_dir = Path(self._config_path).parent if self._config_path else Path('.')
            path = config_dir / file_name
            if not path.exists():
                path = Path(file_name)
            if not required and not path.exists():
                return None
            prof = loader(path, temporal_resolution_hours=1, num_nodes=num_nodes,
                          date_start=self._date_start)
        prof = np.asarray(prof, dtype=float)
        if self._profile_start_hour and len(prof) > 0:
            prof = np.roll(prof, -(self._profile_start_hour % len(prof)), axis=0)
        if self.temporal_resolution_hours > 1:
            prof = aggregate_to_resolution(prof, target_hours=self.temporal_resolution_hours)
        return prof

    def _create_input(self) -> Any:
        """Create the Julia MasterProblemInput struct."""
        ESFEX = get_esfex_module()
        jl = get_julia()

        sys = self.system_config
        # Get num_nodes from NodeConfig or calculate from connections matrix
        import math
        num_nodes = sys.nodes.num_nodes or int(math.sqrt(len(sys.nodes.nodes_connections)))

        # ── Optional internal network reduction ──
        self._reduction_map = None
        if (self.esfex_config is not None
                and getattr(self.esfex_config, "network_reduction", None) is not None
                and self.esfex_config.network_reduction.enabled
                and sys.buses):
            from esfex.topology import reduce_network
            import time as _t
            t0 = _t.perf_counter()
            kron_flag = bool(getattr(
                self.esfex_config.network_reduction, "kron_deg3", False
            ))
            reduced_sys, self._reduction_map = reduce_network(
                sys, kron_deg3=kron_flag,
            )
            t_reduce = _t.perf_counter() - t0
            logger.info(
                f"⏱ Master network reduction: {t_reduce:.2f}s — "
                f"{self._reduction_map.summary()}"
            )
            sys = reduced_sys

        # Pre-compute geographic fuel transport adjustments
        fuel_adjustments = _compute_geographic_fuel_adjustments(sys)

        # Build bus_to_node mapping for per-node → per-bus expansion
        from esfex.config.schema import BusConfig
        from esfex.bridge.converters import node_bus_mapping_required
        buses = sys.buses or [
            BusConfig(bus_id=f"bus_{i}", parent_node=i, demand_fraction=1.0)
            for i in range(num_nodes)
        ]
        bus_to_node_0idx = [b.parent_node for b in buses]
        num_buses = len(buses)
        need_expansion = node_bus_mapping_required(buses, num_nodes)
        b2n_arg = bus_to_node_0idx if need_expansion else None

        # Resolve per-(unit, node) physical bus mapping (endpoint /
        # bus_index).  See _resolve_element_bus_mapping.
        gen_bus_per_node, bat_bus_per_node = _resolve_element_bus_mapping(sys)

        # Life extension is priced at invest_cost; units without one take it
        # from their candidate technology (life_extension_invest_costs).
        ext_gen_costs, ext_bat_costs = life_extension_invest_costs(sys)

        # Convert generators with availability profiles
        gen_order = []  # (key, python_config) in Julia push order
        jl_generators = jl.seval("GeneratorConfig[]")
        for key, gen in sys.generators.items():
            if key in ext_gen_costs:
                gen = gen.model_copy(update={"invest_cost": ext_gen_costs[key]})
            # Availability: the dispatch's profile (runner cache) when there
            # is one, else read from disk with the same calendar.
            availability = self._master_profile(
                self._availability_cache, key, getattr(gen, 'availability_file', None),
                num_nodes, load_availability_profile)
            if availability is not None:
                logger.debug(f"Availability for {gen.name}: shape={availability.shape}")

            # Apply geographic fuel transport cost adjustment
            gen_to_convert = gen
            if key in fuel_adjustments:
                gen_dict = gen.model_dump()
                gen_dict['fuel_cost'] = fuel_adjustments[key]
                gen_to_convert = GeneratorConfig(**gen_dict)

            # Reservoir inflow (MW-eq, not clipped), same sources as above
            inflow = self._master_profile(
                self._inflow_cache, key, getattr(gen, 'reservoir_inflow_file', None),
                num_nodes, load_inflow_profile, required=False)
            if inflow is not None:
                logger.debug(f"Inflow for {gen.name}: shape={inflow.shape}")

            gen_bus_map = gen_bus_per_node.get(key)
            jl_gen = convert_generator_config(gen_to_convert, availability, inflow,
                                               bus_to_node=b2n_arg,
                                               bus_per_node=gen_bus_map)
            jl.seval("push!")(jl_generators, jl_gen)
            gen_order.append((key, gen_to_convert))

        # Convert batteries
        bat_order = []  # (key, python_config) in Julia push order
        jl_batteries = jl.seval("BatteryConfig[]")
        for key, bat in sys.batteries.items():
            if key in ext_bat_costs:
                bat = bat.model_copy(update={"invest_cost": ext_bat_costs[key]})
            bat_bus_map = bat_bus_per_node.get(key)
            bat_avail = self._master_profile(
                self._availability_cache, battery_availability_key(key),
                bat.availability_file, num_nodes, load_availability_profile)
            jl_bat = convert_battery_config(bat, bus_to_node=b2n_arg,
                                             bus_per_node=bat_bus_map,
                                             availability=bat_avail)
            jl.seval("push!")(jl_batteries, jl_bat)
            bat_order.append((key, bat))

        # Convert technologies (investment candidates)
        tech_order = []  # (key, python_config) in Julia push order
        jl_technologies = jl.seval("TechnologyConfig[]")
        for key, tech in sys.technologies.items():
            # Use cached availability if available, otherwise load from disk
            tech_availability = self._master_profile(
                self._availability_cache, key, tech.availability_file,
                num_nodes, load_availability_profile)
            if tech_availability is not None:
                logger.debug(f"Availability for tech {tech.name}: shape={tech_availability.shape}")
            jl_tech = convert_technology_config(tech, tech_availability, bus_to_node=b2n_arg)
            jl.seval("push!")(jl_technologies, jl_tech)
            tech_order.append((key, tech))

        # Convert battery technologies (investment candidates)
        jl_battery_technologies = jl.seval("BatteryTechnologyConfig[]")
        for key, bat_tech in sys.battery_technologies.items():
            bt_avail = self._master_profile(
                self._availability_cache, battery_technology_availability_key(key),
                bat_tech.availability_file, num_nodes, load_availability_profile)
            jl_bat_tech = convert_battery_technology_config(bat_tech, bus_to_node=b2n_arg,
                                                            availability=bt_avail)
            jl.seval("push!")(jl_battery_technologies, jl_bat_tech)

        # Build cost curve dicts for generators, technologies, and batteries
        jl_gen_cost_curves = build_gen_cost_curves_dict(
            gen_order, [], num_buses,
            bus_to_node=bus_to_node_0idx if need_expansion else None,
            gen_bus_per_node=gen_bus_per_node,
        )
        jl_bat_cost_curves = build_bat_cost_curves_dict(
            bat_order, num_buses,
            bus_to_node=bus_to_node_0idx if need_expansion else None,
            bat_bus_per_node=bat_bus_per_node,
        )
        # Technologies use the same fuel_cost_curve attribute; the master may
        # build them at any bus of a node, so each bus gets the node's curve
        jl_tech_cost_curves = build_gen_cost_curves_dict(
            tech_order, [], num_buses,
            bus_to_node=bus_to_node_0idx if need_expansion else None,
            all_buses_of_node=True,
        )

        # Convert network (pass per-line data + transformers + buses)
        jl_network = convert_network_config(
            sys.nodes, sys.dc_power_flow, sys.fuel_transport_distances,
            transmission_lines_geo=sys.transmission_lines_geo or None,
            transformers=sys.transformers or None,
            acdc_converters=getattr(sys, 'acdc_converters', None) or None,
            freq_converters=getattr(sys, 'freq_converters', None) or None,
            buses=sys.buses or None,
            s_base_mva=system_s_base(sys),
        )

        # Convert years to Julia vector
        jl_years = py_to_julia_int_vector(self.years)

        # Read penalties from config (not hardcoded) — scale $ → M$
        penalties = sys.penalties
        loss_of_load_penalty = scale_cost(float(penalties.loss_of_load))  # $/MW → M$/MW
        fre_penetration_loss_penalty = scale_cost(float(penalties.fre_penetration_loss))  # $/MWh → M$/MWh
        max_curtailment_ratio = float(getattr(penalties, 'max_curtailment_ratio', 0.05))  # fraction (no scaling)
        curtailment_cost = scale_cost(float(getattr(penalties, 'curtailment_cost', 20.0)))  # $/MWh → M$/MWh

        # Build sectoral demand for master problem (M2)
        jl_sectoral = jl.seval("Dict{String, Matrix{Float64}}()")
        sectoral_demand = self.kwargs.get('sectoral_demand', {})
        if sectoral_demand:
            for sector_name, sector_arr in sectoral_demand.items():
                jl_sectoral[sector_name] = py_to_julia_matrix(sector_arr)

        jl_sectoral_criticality = jl.seval("Dict{String, Float64}()")
        sectoral_criticality = self.kwargs.get('sectoral_criticality', {})
        if sectoral_criticality:
            for sector_name, crit_val in sectoral_criticality.items():
                jl_sectoral_criticality[sector_name] = float(crit_val)

        # Fuel balance, fuel infrastructure and electrolyzers (#89)
        jl_fuel = self._create_fuel_input(
            jl, ESFEX, sys, gen_order, tech_order, num_nodes,
            b2n_arg, bus_to_node_0idx)

        # Create input struct using keyword constructor
        jl_input = ESFEX.MasterProblemInput(
            years=jl_years,
            base_year=self.base_year,
            system_name=sys.name,
            network=jl_network,
            generators=jl_generators,
            batteries=jl_batteries,
            technologies=jl_technologies,
            battery_technologies=jl_battery_technologies,
            base_demand=py_to_julia_matrix(self.demand),
            demand_growth=self.demand_growth,
            discount_rate=self.discount_rate,
            max_annual_investment=scale_cost(self.max_annual_investment),
            target_re_penetration=self.target_re_penetration,
            initial_re_penetration=self.initial_re_penetration,
            min_re_increment=self.min_re_increment,
            max_re_increment=self.max_re_increment,
            system_node_ranges=self._build_jl_system_node_ranges(jl),
            loss_of_load_penalty=loss_of_load_penalty,
            fre_penetration_loss_penalty=fre_penetration_loss_penalty,
            max_curtailment_ratio=max_curtailment_ratio,
            curtailment_cost=curtailment_cost,
            temporal_resolution_hours=self.temporal_resolution_hours,
            representative_days_per_year=self.representative_days_per_year,
            min_day_separation=self.min_day_separation,
            investment_resolution_hours=self.investment_resolution_hours,
            use_tsam=self.use_tsam,
            tsam_period_start_hours=_py_nested_list_to_julia_vec_vec(jl, self.tsam_period_start_hours, "Int"),
            tsam_period_weights=_py_nested_list_to_julia_vec_vec(jl, self.tsam_period_weights, "Float64"),
            tsam_chronological_order=_py_nested_list_to_julia_vec_vec(jl, self.tsam_chronological_order, "Int"),
            tsam_inter_period_linking=self.tsam_inter_period_linking,
            life_extension_cost_factor=self.life_extension_cost_factor,
            decommissioning_cost_factor=self.decommissioning_cost_factor,
            sectoral_demand=jl_sectoral,
            sectoral_criticality=jl_sectoral_criticality,
            # Configurable parameters (previously hardcoded)
            reserve_margin=float(getattr(sys, 'reserve_margin', 1.15)),
            npv_annual_return_rate=float(getattr(sys, 'npv_annual_return_rate', 0.15)),
            base_lcoe=scale_cost(float(getattr(sys, 'base_lcoe', 93.0))),
            solver_name=self._solver.name,
            threads=self._solver.threads,
            time_limit=float(self._solver.time_limit),
            gap=self._solver.gap,
            verbose=self._solver.verbose,
            solver_options=_solver_options_to_julia(self._solver.options, self._solver.name),
            # Reserve, inertia, CO2 parameters for operational constraints
            reserve_static_default_ratio=self.reserve_static_default_ratio,
            reserve_static_requirement=_build_reserve_requirement_dict(
                list(getattr(self.system_config.nodes, 'reserve_static', []) or [])
            ),
            reserve_dynamic_requirement=_build_reserve_requirement_dict(
                list(getattr(self.system_config.nodes, 'reserve_dynamic', []) or [])
            ),
            dynamic_reserve_contribution=self.dynamic_reserve_contribution,
            loss_of_reserve_static=scale_cost(self.loss_of_reserve_static),
            loss_of_reserve_dynamic=scale_cost(self.loss_of_reserve_dynamic),
            inertia_limit=self.inertia_limit,
            loss_of_inertia_penalty=scale_cost(self.loss_of_inertia_penalty),
            fuel_co2=jl.seval("Dict{String, Float64}")(self.fuel_co2) if self.fuel_co2 else jl.seval("Dict{String, Float64}()"),
            co2_cost=scale_cost(self.co2_cost),
            # Transmission line data for DC power flow
            transmission_lines=self._build_jl_transmission_lines(jl),
            transmission_reactances=py_to_julia_vector(self.transmission_reactances) if self.transmission_reactances else jl.seval("Float64[]"),
            transmission_capacities=py_to_julia_vector(self.transmission_capacities) if self.transmission_capacities else jl.seval("Float64[]"),
            # PWL transmission loss model
            transmission_resistances=py_to_julia_vector(self.transmission_resistances) if self.transmission_resistances else jl.seval("Float64[]"),
            transmission_loss_segments=_pwl_segments_from_config_master(self.system_config.dc_power_flow),
            # Bidding/offer curves (PWL cost decomposition)
            gen_cost_curves=jl_gen_cost_curves,
            bat_cost_curves=jl_bat_cost_curves,
            tech_cost_curves=jl_tech_cost_curves,
            re_target_override=jl.seval(
                "Dict{Tuple{Int,Int}, Float64}(" + ", ".join(
                    f"({int(s)}, {int(y)}) => {float(v)!r}"
                    for (s, y), v in self.re_target_override.items()) + ")"),
            # CO2 budgets and start-ups, as in the dispatch (link flow costs
            # ride on the network's lines, `flow_cost`)
            co2_budget_groups=_master_co2_budget_groups(sys, ESFEX),
            unit_commitment=self._dispatch_commits_units(),
            primary_energy=jl_fuel,
        )

        return jl_input

    def _dispatch_commits_units(self) -> bool:
        """Whether the dispatch of this development run commits units
        (``master_problem.use_uc_in_dispatch``): the master then models
        start-ups."""
        cfg = self.esfex_config
        mp = getattr(cfg, 'master_problem', None) if cfg is not None else None
        return bool(getattr(mp, 'use_uc_in_dispatch', False))

    def build_model(self, use_representative_days: bool = True):
        """
        Build the master problem model.

        Args:
            use_representative_days: Whether to use representative days for operations
        """
        ESFEX = get_esfex_module()

        logger.debug("Building Julia MasterProblem model...")

        self._use_rep_days = use_representative_days
        self._jl_input = self._with_npv_retirements(self._create_input())

        # Resolve declarative user constraints targeting the investment model
        # (technology names → 1-based tech indices, year values → year indices).
        jl = get_julia()
        tech_idx = {k: i for i, k in
                    enumerate(self.system_config.technologies.keys(), 1)}
        year_idx = {y: i for i, y in enumerate(self.years, 1)}
        inv_specs = resolve_investment_constraints(
            getattr(self.system_config, "custom_constraints", None),
            tech_idx, year_idx,
        )

        if self.solver_method == "benders":
            # The Benders master has neither TSAM periods nor user
            # constraints: solve the monolithic master instead of dropping them
            unsupported = []
            if self.use_tsam:
                unsupported.append("TSAM periods (use_tsam)")
            if inv_specs:
                unsupported.append("user investment constraints (custom_constraints)")
            if unsupported:
                logger.warning(
                    "Benders decomposition does not support "
                    f"{' or '.join(unsupported)}; solving the monolithic master "
                    "problem instead."
                )
                self.solver_method = "monolithic"

        if self.solver_method == "benders":
            # Benders builds its own investment master and per-day subproblems
            # internally during solve(); here we only need the input ready.
            self._benders_use_rep_days = use_representative_days
            if self.use_stochastic and self.stochastic_scenarios:
                logger.warning(
                    "Stochastic scenarios are not supported with Benders "
                    "decomposition; the master is solved deterministically. "
                    "Use solver_method: monolithic for the stochastic master."
                )
            logger.debug(
                "MasterProblem will be solved by Benders decomposition "
                f"(max_iter={self.benders_max_iterations}, "
                f"tol={self.benders_tolerance})"
            )
            return

        if self.use_stochastic and self.stochastic_scenarios:
            jl_scenarios = self._build_julia_scenarios()
            jl_stochastic_input = ESFEX.StochasticMasterInput(
                self._jl_input,
                jl_scenarios,
                True,
            )
            custom = _custom_constraints_to_julia(inv_specs, jl) if inv_specs else None
            (self._jl_model, self._jl_vars,
             self._jl_targets, self._jl_scenarios) = (
                ESFEX.create_stochastic_master_problem(
                    jl_stochastic_input, custom_constraints=custom)
            )
            logger.debug(
                f"Stochastic MasterProblem built with "
                f"{len(self.stochastic_scenarios)} scenarios"
            )
        else:
            if inv_specs:
                self._jl_model, self._jl_vars, self._jl_targets = (
                    ESFEX.create_master_problem(
                        self._jl_input,
                        use_representative_days=use_representative_days,
                        custom_constraints=_custom_constraints_to_julia(inv_specs, jl),
                    )
                )
            else:
                self._jl_model, self._jl_vars, self._jl_targets = (
                    ESFEX.create_master_problem(
                        self._jl_input,
                        use_representative_days=use_representative_days,
                    )
                )

        logger.debug("MasterProblem model built successfully")

    # ── Fuel block of the master (#89) ──────────────────────────────────────

    def _create_fuel_input(self, jl, ESFEX, sys, gen_order, tech_order, num_nodes,
                           b2n_arg, bus_to_node_0idx):
        """Julia ``MasterFuelInput`` from the ``primary_energy`` keyword
        (``Orchestrator._master_fuel_kwargs``), or ``nothing`` without it.

        The fuel data go through ``PrimaryEnergyAdapter`` exactly as for a
        dispatch window. Its generator list is the master's generators and
        then its candidate technologies, so technology ``tc`` is fuel-map key
        ``len(generators) + tc`` (as the dispatch's virtual generators).
        The window spans the horizon, so scheduled fuel-source outages are
        placed by their absolute hours."""
        self._fuel_elz_keys = []
        self._fuel_bus_to_node = None
        pe_kw = self.kwargs.get('primary_energy')
        if not pe_kw:
            return jl.seval("nothing")
        pe_kw = dict(pe_kw)
        base_year = int(pe_kw.pop('base_year', self.base_year))
        last_year = int(pe_kw.pop('last_year', self.years[-1]))
        res_h = max(1, int(self.temporal_resolution_hours))
        gen_list = []
        for key, unit in list(gen_order) + list(tech_order):
            d = unit.model_dump() if hasattr(unit, 'model_dump') else dict(unit.__dict__)
            d['name'] = key
            gen_list.append(d)
        pe = PrimaryEnergyAdapter(
            year=base_year, base_year=base_year,
            hours=(last_year - base_year + 1) * HOURS_STD_YEAR // res_h,
            num_nodes=num_nodes, generators=gen_list,
            primary_energy_resolution=max(1, 24 // res_h),
            mode="master", step_hours=res_h, year_hour_offset=0,
            **pe_kw)
        jl_pe = pe._create_julia_input()

        # Electrolyzers per bus, mapped like the dispatch's (capacity at the
        # node's first bus, properties copied to its buses)
        def _elz(values, mode="property"):
            values = [float(v) for v in values]
            if b2n_arg is None:
                return values
            return expand_node_to_bus_array(values, b2n_arg, mode=mode)

        jl_elz = jl.seval("ElectrolyzerConfig[]")
        for key, e in (getattr(sys, 'electrolyzers', None) or {}).items():
            jl.seval("push!")(jl_elz, ESFEX.ElectrolyzerConfig(
                py_to_julia_vector(_elz(e.rated_power, "capacity")),
                py_to_julia_vector(_elz(e.eff_at_rated)),
                py_to_julia_vector(_elz(e.eff_at_min)),
                float(e.energy_per_kg_h2),
                py_to_julia_vector(_elz(e.ramp_up)),
                py_to_julia_vector(_elz(e.ramp_down)),
                py_to_julia_vector(_elz(scale_cost_list(e.invest_cost))),
                py_to_julia_vector(_elz(e.invest_max_power, "capacity")),
                py_to_julia_vector(_elz(scale_cost_list(e.fixed_cost))),
                py_to_julia_vector(_elz(scale_cost_list(e.variable_cost))),
                scale_cost(float(e.water_cost)),
                py_to_julia_vector(_elz(e.life_time)),
            ))
            self._fuel_elz_keys.append(key)
        self._fuel_bus_to_node = bus_to_node_0idx if b2n_arg is not None else None
        self._fuel_num_nodes = num_nodes
        return ESFEX.MasterFuelInput(jl_pe, jl_elz)

    def fuel_capacities_by_year(self, result: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
        """Fuel capacity the master built and that is in service in each
        year, for the dispatch: {year index (1-based): {"storage": {fuel:
        {node: capacity}}, "transport": {fuel: {route: capacity}},
        "electrolyzer": {key: [MW per node]}}}, 0-based nodes and routes."""
        fi = (result or {}).get('fuel_investment') or {}
        out: Dict[int, Dict[str, Any]] = {}
        for kind, src in (("storage", "storage_capacity"), ("transport", "transport_capacity")):
            for y_idx, by_fuel in (fi.get(src) or {}).items():
                tgt = out.setdefault(int(y_idx), {}).setdefault(kind, {})
                for fuel, arr in by_fuel.items():
                    vals = {i: float(v) for i, v in enumerate(arr) if v > 1e-9}
                    if vals:
                        tgt[str(fuel)] = vals
        b2n = getattr(self, '_fuel_bus_to_node', None)
        keys = getattr(self, '_fuel_elz_keys', [])
        n_nodes = getattr(self, '_fuel_num_nodes', 0)
        for y_idx, by_elz in (fi.get('electrolyzer_capacity') or {}).items():
            tgt = out.setdefault(int(y_idx), {}).setdefault('electrolyzer', {})
            for e, per_bus in by_elz.items():
                if not 1 <= int(e) <= len(keys) or not any(v > 1e-9 for v in per_bus):
                    continue
                per_node = [0.0] * max(n_nodes, len(per_bus) if b2n is None else 0)
                for b, v in enumerate(per_bus):
                    n = b if b2n is None else b2n[b]
                    per_node[n] += float(v)
                tgt[keys[int(e) - 1]] = per_node
        return out

    def _build_julia_scenarios(self):
        """Convert Python scenario dicts to Julia Vector{Scenario}."""
        from esfex.bridge.converters import convert_scenario

        jl = get_julia()
        ESFEX = get_esfex_module()

        jl_scenarios = jl.seval("ESFEX.Scenario[]")
        for sc_dict in self.stochastic_scenarios:
            jl_sc = convert_scenario(sc_dict)
            jl.seval("push!")(jl_scenarios, jl_sc)

        return jl_scenarios

    def write_lp(self, filepath: str) -> None:
        """
        Export the model to LP format for debugging.

        Args:
            filepath: Path to write the LP file
        """
        jl = get_julia()

        if self.solver_method == "benders":
            # Benders builds its master and subproblems inside solve(); there is
            # no single model to export.
            logger.debug("Benders decomposition: no master LP to export")
            return
        if self._jl_model is None:
            raise RuntimeError("Model not built. Call build_model() first.")

        jl.seval("global _mp_model_export")
        jl._mp_model_export = self._jl_model

        jl.seval("using JuMP")
        jl.seval(f'write_to_file(_mp_model_export, "{filepath.replace(chr(92), "/")}")')

        logger.debug(f"MasterProblem LP exported to: {filepath}")

    # ── NPV-based retirement (system setting npv_retirement) ────────────────

    def _unit_index(self, kind: str, key: str) -> Optional[int]:
        """1-based Julia index of generator or battery *key*."""
        units = (self.system_config.generators if kind == "generator"
                 else self.system_config.batteries)
        keys = list(units.keys())
        return keys.index(key) + 1 if key in keys else None

    def _jl_units(self, units: List[tuple]) -> Any:
        """Julia ``Vector{UnitNPV}`` of ``(kind, key, bus)`` units."""
        jl = get_julia()
        ESFEX = get_esfex_module()
        vec = jl.seval("ESFEX.UnitNPV[]")
        name = str(getattr(self.system_config, 'name', 'primary'))
        for kind, key, bus in units:
            idx = self._unit_index(kind, key)
            if idx is not None:
                jl.seval("push!")(vec, ESFEX.UnitNPV(kind, idx, int(bus), name, 0.0, 0.0, True))
        return vec

    def _with_npv_retirements(self, jl_input: Any) -> Any:
        """*jl_input* with the units retired by the NPV iteration: the ones an
        earlier myopic year retired (no decommissioning again) and the ones
        this master retired so far (decommissioned in its first year)."""
        if not (self._retired_carried or self.npv_retirements):
            return jl_input
        ESFEX = get_esfex_module()
        if self._retired_carried:
            jl_input = ESFEX.retire_units(
                jl_input, self._jl_units(self._retired_carried), decommission=False)
        if self.npv_retirements:
            jl_input = ESFEX.retire_units(jl_input, self._jl_units(
                [(r["kind"], r["key"], r["bus"]) for r in self.npv_retirements]))
        return jl_input

    def _unit_npvs(self) -> List[Dict[str, Any]]:
        """NPV ($) of each existing unit in the solved plan (Julia
        ``unit_npvs``), as dicts with kind, key, name, bus (1-based, the
        master's location), node (0-based), npv and retire (below the
        threshold)."""
        jl = get_julia()
        ESFEX = get_esfex_module()
        if self.solver_method == "benders":
            result = self._benders_solution
        else:
            result = ESFEX.extract_master_solution(
                self._jl_model, self._jl_vars, self._jl_input, unit_operation=True)
        npvs = ESFEX.unit_npvs(result, self._jl_input,
                               npv_threshold=scale_cost(self.npv_threshold),
                               extra_years=self.npv_extra_years)
        node_of = jl.seval("(inp, b) -> inp.network.bus_to_node[b]")
        out = []
        for u in npvs:
            kind = str(u.unit_type)
            units = (self.system_config.generators if kind == "generator"
                     else self.system_config.batteries)
            key = list(units.keys())[int(u.unit_idx) - 1]
            if self._npv_unit_keys is not None and key not in self._npv_unit_keys:
                continue
            bus = int(u.node)
            out.append({
                "kind": kind, "key": key,
                "name": str(getattr(units[key], 'name', key)),
                "bus": bus, "node": int(node_of(self._jl_input, bus)) - 1,
                "npv": float(u.npv) * COST_UNSCALE,
                "retire": bool(u.recommend_retirement),
            })
        return out

    # The solved model the NPV iteration falls back on when a re-solve fails
    _SOLVED_STATE = ("_jl_model", "_jl_vars", "_jl_input", "_jl_targets", "_jl_scenarios",
                     "_benders_result", "_benders_solution", "solver_method",
                     "_benders_use_rep_days")

    def _solve_npv_retirement(self, status: int) -> int:
        """NPV-based retirement around the solve: while the plan is optimal,
        retire the existing units whose NPV is below the threshold for the
        whole horizon, rebuild and re-solve; stop when no new unit is below
        it or after ``npv_retirement_max_iterations`` rounds. Each retired
        unit is added to ``npv_retirements`` with its NPV and round.

        A re-solve that is not optimal (or fails) undoes its round: the last
        optimal model and its retirements are kept, a warning names the
        units of the undone round and the solver status, and the iteration
        stops (the run goes on with that plan). Returns the status of the
        plan kept."""
        rounds = 0
        while status == 1:
            npvs = self._unit_npvs()
            for u in npvs:
                logger.debug(f"  NPV {u['kind']} {u['name']} (node {u['node']}, "
                             f"bus {u['bus']}): ${u['npv']:,.0f}")
            flagged = [u for u in npvs if u["retire"]]
            if not flagged:
                logger.info(
                    f"NPV retirement: no existing unit below the threshold "
                    f"(${self.npv_threshold:,.0f}) after {rounds} round(s)")
                break
            if rounds >= self.npv_max_iterations:
                logger.warning(
                    f"NPV retirement: {len(flagged)} unit(s) still below the "
                    f"threshold after {rounds} round(s) "
                    f"(npv_retirement_max_iterations); keeping them")
                break
            rounds += 1
            logger.info(
                f"NPV retirement, round {rounds}: retiring {len(flagged)} unit(s): "
                + ", ".join(f"{u['name']} at node {u['node']} (NPV ${u['npv']:,.0f})"
                            for u in flagged))
            kept = {a: getattr(self, a) for a in self._SOLVED_STATE}
            n_before = len(self.npv_retirements)
            for u in flagged:
                u = dict(u, iteration=rounds)
                u.pop("retire")
                self.npv_retirements.append(u)
            try:
                self.build_model(use_representative_days=self._use_rep_days)
                status = self._solve_once()
            except Exception as e:  # noqa: BLE001 — a failed re-solve is undone
                status, self._last_status = 0, f"error: {e}"
            if status != 1:
                undone = self.npv_retirements[n_before:]
                del self.npv_retirements[n_before:]
                for a, v in kept.items():
                    setattr(self, a, v)
                logger.warning(
                    f"NPV retirement, round {rounds}: the master re-solve after retiring "
                    + ", ".join(f"{u['name']} at node {u['node']}" for u in undone)
                    + f" is not optimal ({self._last_status}); keeping these units and "
                    "the last optimal plan")
                return 1
        return status

    def solve(self) -> int:
        """
        Solve the master problem; with the system's ``npv_retirement`` on,
        retire the existing units whose NPV is below the threshold and
        re-solve (:meth:`_solve_npv_retirement`).

        Returns:
            PuLP-compatible status code (1 = optimal)
        """
        status = self._solve_once()
        if self.npv_retirement:
            status = self._solve_npv_retirement(status)
        return status

    def _solve_once(self) -> int:
        """Solve the built master problem once."""
        jl = get_julia()

        if self.solver_method == "benders":
            return self._solve_benders()

        if self._jl_model is None:
            raise RuntimeError("Model not built. Call build_model() first.")

        logger.debug("Solving MasterProblem...")

        jl.seval("global _mp_model")
        jl._mp_model = self._jl_model

        jl.seval("using JuMP")
        # Set solver verbose from config
        verbose_flag = "true" if self._solver.verbose else "false"
        jl.seval(f'set_optimizer_attribute(_mp_model, "output_flag", {verbose_flag})')
        logger.debug(f"Solver output_flag={self._solver.verbose}, calling optimize!...")
        jl.seval("optimize!(_mp_model)")

        status = jl.seval("termination_status(_mp_model)")
        status_str = str(status)
        self._last_status = status_str

        primal_status = str(jl.seval("primal_status(_mp_model)"))
        logger.info(f"MasterProblem termination: {status_str}, primal: {primal_status}")

        if "OPTIMAL" in status_str:
            obj_val = float(jl.seval("objective_value(_mp_model)")) * COST_UNSCALE
            logger.info(f"MasterProblem objective value: {obj_val:,.2f}")
            ESFEX = get_esfex_module()
            jl_result = None
            # Log detailed solution summary (costs in M$ internally)
            try:
                jl_result = ESFEX.extract_master_solution(
                    self._jl_model, self._jl_vars, self._jl_input)
                ESFEX.log_solution_summary(jl_result, self._jl_input)
            except Exception as e:
                logger.warning(f"log_solution_summary skipped: {e}")
            return 1
        elif "INFEASIBLE" in status_str:
            logger.error("MasterProblem INFEASIBLE — check constraints")
            return -1
        elif "UNBOUNDED" in status_str:
            return -2
        else:
            # Strict: do not accept "feasible point" results from non-optimal
            # terminations. The solver must prove optimality. Anything else is
            # a real bug in the formulation that needs to be surfaced.
            logger.error(
                f"MasterProblem solver failed: status={status_str}, "
                f"primal={primal_status}"
            )
            return 0

    def _solve_benders(self) -> int:
        """Solve the master problem by Benders decomposition.

        Runs the full investment-master / operational-subproblem iteration in
        Julia and stores the recovered ``MasterProblemResult`` for downstream
        extraction. Returns a PuLP-compatible status code (1 = optimal).
        """
        ESFEX = get_esfex_module()
        if self._jl_input is None:
            raise RuntimeError("Model not built. Call build_model() first.")

        logger.info(
            "Solving MasterProblem by Benders decomposition "
            f"(max_iter={self.benders_max_iterations}, "
            f"tol={self.benders_tolerance})..."
        )
        self._benders_result = ESFEX.run_benders_decomposition(
            self._jl_input,
            max_iterations=self.benders_max_iterations,
            tolerance=self.benders_tolerance,
            use_representative_days=self._benders_use_rep_days,
            verbose_benders=self._solver.verbose,
            record_unit_operation=self.npv_retirement,
        )
        self._benders_solution = self._benders_result.solution

        status_str = str(self._benders_solution.status)
        self._last_status = status_str
        gap = float(self._benders_result.gap)
        iters = int(self._benders_result.iterations)
        obj = float(self._benders_result.objective) * COST_UNSCALE
        logger.info(
            f"Benders finished: {status_str} in {iters} iteration(s), "
            f"gap={gap * 100:.4f}%, objective={obj:,.2f}"
        )
        if "OPTIMAL" in status_str:
            return 1
        elif "INFEASIBLE" in status_str:
            return -1
        elif "UNBOUNDED" in status_str:
            return -2
        return 0

    def get_solution_values(self) -> Dict[str, Any]:
        """
        Extract all solution values from the solved model.

        Returns:
            Dictionary with investment decisions and costs
        """
        if self.solver_method == "benders":
            if self._benders_solution is None:
                raise RuntimeError("Benders not solved. Call solve() first.")
            return self._convert_result(self._benders_solution)

        ESFEX = get_esfex_module()

        if self._jl_model is None:
            raise RuntimeError("Model not built. Call build_model() first.")

        jl_result = ESFEX.extract_master_solution(
            self._jl_model,
            self._jl_vars,
            self._jl_input
        )

        # Convert Julia result to Python dictionary
        return self._convert_result(jl_result)

    def _convert_result(self, jl_result: Any) -> Dict[str, Any]:
        """Convert Julia MasterProblemResult to Python dictionary."""
        return self._convert_single_result(jl_result)

    def _convert_single_result(self, jl_result: Any) -> Dict[str, Any]:
        """
        Convert a single Julia MasterProblemResult to Python dictionary.

        Extracted as helper to support MGA conversion of multiple alternatives.

        Returns a structure matching the legacy MasterProblem.py format:
        {
            year: {
                'tech_investment': {'tech_investment_power_t_n': value, ...},
                'bat_tech_power_investment': {'bat_tech_investment_power_bt_n': value, ...},
                'bat_tech_capacity_investment': {'bat_tech_investment_capacity_bt_n': value, ...},
                'transfer_investment': {'transfer_investment_i_j': value, ...},
                'converter_investment': {'converter_investment_acdc_c': MW, ...},
                'reservoir_investment': {'reservoir_investment_g_n': MWh, ...},
                're_penetration_ratio': float,
            },
            ...
        }

        Plus additional metadata keys at the top level.
        """
        num_years = len(self.years)
        num_nodes = self.system_config.num_nodes
        to_buses, bus_of = self._original_bus_layout()

        # Build legacy-compatible solution structure keyed by actual year
        solution = {}

        for y_idx in range(1, num_years + 1):
            year = self.years[y_idx - 1]  # Convert 1-indexed to actual year

            solution[year] = {
                'tech_investment': {},
                'bat_tech_power_investment': {},
                'bat_tech_capacity_investment': {},
                'transfer_investment': {},
                'converter_investment': {},
                'reservoir_investment': {},
                're_penetration_ratio': 0.0,
            }

            # Technology investments: Julia 1-indexed → Python 0-indexed keys
            for t_jl in jl_result.tech_investment[y_idx].keys():
                t_py = int(t_jl) - 1  # Convert to 0-indexed
                node_values = to_buses(jl_result.tech_investment[y_idx][t_jl])
                for n in range(len(node_values)):
                    value = float(node_values[n])
                    if value > 1e-6:
                        key = f"tech_investment_power_{t_py}_{n}"
                        solution[year]['tech_investment'][key] = value

            # Battery technology power investments
            for bt_jl in jl_result.bat_tech_power_investment[y_idx].keys():
                bt_py = int(bt_jl) - 1  # Convert to 0-indexed
                node_values = to_buses(jl_result.bat_tech_power_investment[y_idx][bt_jl])
                for n in range(len(node_values)):
                    value = float(node_values[n])
                    if value > 1e-6:
                        key = f"bat_tech_investment_power_{bt_py}_{n}"
                        solution[year]['bat_tech_power_investment'][key] = value

            # Battery technology capacity investments
            for bt_jl in jl_result.bat_tech_capacity_investment[y_idx].keys():
                bt_py = int(bt_jl) - 1  # Convert to 0-indexed
                node_values = to_buses(jl_result.bat_tech_capacity_investment[y_idx][bt_jl])
                for n in range(len(node_values)):
                    value = float(node_values[n])
                    if value > 1e-6:
                        key = f"bat_tech_investment_capacity_{bt_py}_{n}"
                        solution[year]['bat_tech_capacity_investment'][key] = value

            # Transmission investments: Julia 1-indexed → Python 0-indexed keys
            for key, val in jl_result.transfer_investment[y_idx].items():
                i_py = bus_of(int(key[0]) - 1)  # Convert to 0-indexed
                j_py = bus_of(int(key[1]) - 1)
                value = float(val)
                if value > 1e-6:
                    trans_key = f"transfer_investment_{i_py}_{j_py}"
                    solution[year]['transfer_investment'][trans_key] = value

            # Converter investments: ("acdc" | "freq", 1-indexed) → 0-indexed keys
            for key, val in jl_result.converter_investment[y_idx].items():
                value = float(val)
                if value > 1e-6:
                    conv_key = f"converter_investment_{key[0]}_{int(key[1]) - 1}"
                    solution[year]['converter_investment'][conv_key] = value

            # Reservoir capacity built (MWh) per generator and location
            res_inv = getattr(jl_result, 'reservoir_investment', None)
            if res_inv is not None:
                for g_jl in res_inv[y_idx].keys():
                    loc_values = to_buses(res_inv[y_idx][g_jl])
                    for n, value in enumerate(loc_values):
                        if float(value) > 1e-6:
                            key = f"reservoir_investment_{int(g_jl) - 1}_{n}"
                            solution[year]['reservoir_investment'][key] = float(value)

            # RE penetration ratio
            re_by_year = np.array(jl_result.re_penetration_by_year)
            if y_idx <= len(re_by_year):
                solution[year]['re_penetration_ratio'] = float(re_by_year[y_idx - 1])

        # Build structured format for detailed analysis
        tech_investment = {}
        bat_tech_power_investment = {}
        bat_tech_capacity_investment = {}
        gen_life_extension = {}
        bat_life_extension = {}
        cumulative_gen_capacity = {}
        cumulative_bat_capacity = {}
        cumulative_bat_power = {}
        cumulative_tech_capacity = {}
        cumulative_bat_tech_power = {}
        cumulative_bat_tech_capacity = {}

        for y_idx in range(1, num_years + 1):
            tech_investment[y_idx] = {}
            bat_tech_power_investment[y_idx] = {}
            bat_tech_capacity_investment[y_idx] = {}
            gen_life_extension[y_idx] = {}
            bat_life_extension[y_idx] = {}
            cumulative_gen_capacity[y_idx] = {}
            cumulative_bat_capacity[y_idx] = {}
            cumulative_bat_power[y_idx] = {}
            cumulative_tech_capacity[y_idx] = {}
            cumulative_bat_tech_power[y_idx] = {}
            cumulative_bat_tech_capacity[y_idx] = {}

            for t_jl in jl_result.tech_investment[y_idx].keys():
                tech_investment[y_idx][int(t_jl)] = to_buses(
                    jl_result.tech_investment[y_idx][t_jl]
                )

            for bt_jl in jl_result.bat_tech_power_investment[y_idx].keys():
                bat_tech_power_investment[y_idx][int(bt_jl)] = to_buses(
                    jl_result.bat_tech_power_investment[y_idx][bt_jl]
                )
                bat_tech_capacity_investment[y_idx][int(bt_jl)] = to_buses(
                    jl_result.bat_tech_capacity_investment[y_idx][bt_jl]
                )

            for g_jl in jl_result.gen_life_extension[y_idx].keys():
                gen_life_extension[y_idx][int(g_jl)] = to_buses(
                    jl_result.gen_life_extension[y_idx][g_jl], flags=True
                )

            for b_jl in jl_result.bat_life_extension[y_idx].keys():
                bat_life_extension[y_idx][int(b_jl)] = to_buses(
                    jl_result.bat_life_extension[y_idx][b_jl], flags=True
                )

            for g_jl in jl_result.cumulative_gen_capacity[y_idx].keys():
                cumulative_gen_capacity[y_idx][int(g_jl)] = to_buses(
                    jl_result.cumulative_gen_capacity[y_idx][g_jl]
                )

            for b_jl in jl_result.cumulative_bat_capacity[y_idx].keys():
                cumulative_bat_capacity[y_idx][int(b_jl)] = to_buses(
                    jl_result.cumulative_bat_capacity[y_idx][b_jl]
                )

            for b_jl in jl_result.cumulative_bat_power[y_idx].keys():
                cumulative_bat_power[y_idx][int(b_jl)] = to_buses(
                    jl_result.cumulative_bat_power[y_idx][b_jl]
                )

            for t_jl in jl_result.cumulative_tech_capacity[y_idx].keys():
                cumulative_tech_capacity[y_idx][int(t_jl)] = to_buses(
                    jl_result.cumulative_tech_capacity[y_idx][t_jl]
                )

            for bt_jl in jl_result.cumulative_bat_tech_power[y_idx].keys():
                cumulative_bat_tech_power[y_idx][int(bt_jl)] = to_buses(
                    jl_result.cumulative_bat_tech_power[y_idx][bt_jl]
                )

            for bt_jl in jl_result.cumulative_bat_tech_capacity[y_idx].keys():
                cumulative_bat_tech_capacity[y_idx][int(bt_jl)] = to_buses(
                    jl_result.cumulative_bat_tech_capacity[y_idx][bt_jl]
                )

        # Fuel storage, routes and electrolyzers (#89): {kind: {year index:
        # {fuel or electrolyzer index: values per node/route/bus}}}, 1-based
        fuel_investment = {}
        jl_fi = getattr(jl_result, 'fuel_investment', None)
        if jl_fi is not None:
            for kind in jl_fi.keys():
                fuel_investment[str(kind)] = {
                    int(y): {(k if isinstance(k, (int, str)) else str(k)):
                             np.asarray(v, dtype=float)
                             for k, v in by.items()}
                    for y, by in jl_fi[kind].items()}

        return {
            # Legacy-compatible solution keyed by year
            'solution': solution,
            'fuel_investment': fuel_investment,
            # Metadata (unscale M$ → $)
            'status': str(jl_result.status),
            'objective': float(jl_result.objective) * COST_UNSCALE,
            'solve_time': float(jl_result.solve_time),
            # Overnight cost of each year's builds (what the budget limits)
            'total_investment_by_year': np.array(jl_result.total_investment_by_year) * COST_UNSCALE,
            # Investment annuities paid each year (what the objective charges)
            'annuity_cost_by_year': np.array(getattr(jl_result, 'annuity_cost_by_year', []),
                                             dtype=float) * COST_UNSCALE,
            'total_operational_by_year': np.array(jl_result.total_operational_cost_by_year) * COST_UNSCALE,
            're_penetration_by_year': np.array(jl_result.re_penetration_by_year),
            're_penetration_by_system': {
                str(k): list(v) for k, v in dict(jl_result.re_penetration_by_system).items()
            },
            # Structured format (1-indexed, for detailed analysis)
            'tech_investment': tech_investment,
            'bat_tech_power_investment': bat_tech_power_investment,
            'bat_tech_capacity_investment': bat_tech_capacity_investment,
            'gen_life_extension': gen_life_extension,
            'bat_life_extension': bat_life_extension,
            'cumulative_gen_capacity': cumulative_gen_capacity,
            'cumulative_bat_capacity': cumulative_bat_capacity,
            'cumulative_bat_power': cumulative_bat_power,
            'cumulative_tech_capacity': cumulative_tech_capacity,
            'cumulative_bat_tech_power': cumulative_bat_tech_power,
            'cumulative_bat_tech_capacity': cumulative_bat_tech_capacity,
        }

    def _original_bus_layout(self):
        """``(to_buses, bus_of)``: lay master results out on the system's own
        buses.

        With ``network_reduction`` the master solves on the reduced network,
        whose bus ``k`` is original bus ``retained_original_indices[k]``; the
        runner, the myopic history and the dispatch index master results by
        the original buses. ``to_buses(values, flags=False)`` returns a
        per-bus array on the original buses: eliminated buses hold no
        capacity (0), and a per-unit status flag (``flags``, e.g. life
        extension) takes the value of a retained bus of the same node.
        ``bus_of(k)`` maps a reduced bus index. Both are identities without
        reduction.
        """
        rm = getattr(self, "_reduction_map", None)
        if rm is None or not rm.n_original_buses or not rm.n_reduced_buses:
            return (lambda values, flags=False: np.array(values, dtype=float)), (lambda k: k)
        retained = list(rm.retained_original_indices)
        parent = [b.parent_node for b in (self.system_config.buses or [])]

        def to_buses(values, flags=False):
            arr = np.array(values, dtype=float)
            if arr.ndim != 1 or len(arr) != len(retained):
                return arr
            out = np.zeros(rm.n_original_buses)
            if flags:
                node_value = {}
                for r, o in enumerate(retained):
                    node_value.setdefault(parent[o], arr[r])
                out[:] = [node_value.get(parent[o], 1.0) if o < len(parent) else 1.0
                          for o in range(rm.n_original_buses)]
            out[retained] = arr
            return out

        def bus_of(k):
            return retained[k] if 0 <= k < len(retained) else k

        return to_buses, bus_of

    def get_investment_decisions(self) -> Dict[str, Any]:
        """
        Get investment decisions.

        Returns:
            Dictionary with investment decisions by year, where each year contains:
            - 'tech_investment': {'tech_investment_power_t_n': value, ...}
            - 'bat_tech_power_investment': {'bat_tech_investment_power_bt_n': value, ...}
            - 'bat_tech_capacity_investment': {'bat_tech_investment_capacity_bt_n': value, ...}
            - 'transfer_investment': {'transfer_investment_i_j': value, ...}
        """
        result = self.get_solution_values()
        solution = result.get('solution', {})

        # Return format keyed by year
        investments = {}
        for year, year_data in solution.items():
            investments[year] = {
                'tech_investment': year_data.get('tech_investment', {}),
                'bat_tech_power_investment': year_data.get('bat_tech_power_investment', {}),
                'bat_tech_capacity_investment': year_data.get('bat_tech_capacity_investment', {}),
                'transfer_investment': year_data.get('transfer_investment', {}),
            }

        return {
            'investments': investments,
            'total_investment_by_year': result['total_investment_by_year'],
        }

    def get_retirement_decisions(self) -> Dict[str, Any]:
        """
        Get retirement and life extension decisions.

        Returns:
            Dictionary with life extension decisions by year (1-indexed year keys)
        """
        result = self.get_solution_values()
        return {
            'gen_life_extension': result['gen_life_extension'],
            'bat_life_extension': result['bat_life_extension'],
        }

    def get_year_investments(self, year: int) -> Dict[str, Any]:
        """
        Get investment decisions for a specific year in legacy format.

        This matches the legacy MasterProblem.get_year_investments() method.

        Args:
            year: The actual year (e.g., 2025, 2026, ...)

        Returns:
            Dictionary with all investment decisions for the year:
            - Keys like 'tech_investment_power_0_0', 'bat_tech_investment_power_0_0', etc.
        """
        result = self.get_solution_values()
        solution = result.get('solution', {})

        if year not in solution:
            return {}

        year_data = solution[year]
        year_investments = {}

        # Merge all investment types into a single dict
        year_investments.update(year_data.get('tech_investment', {}))
        year_investments.update(year_data.get('bat_tech_power_investment', {}))
        year_investments.update(year_data.get('bat_tech_capacity_investment', {}))
        year_investments.update(year_data.get('transfer_investment', {}))
        year_investments.update(year_data.get('converter_investment', {}))
        year_investments.update(year_data.get('reservoir_investment', {}))

        return year_investments

    def get_cumulative_capacity(self, year_idx: int = None, year: int = None) -> Dict[str, Any]:
        """
        Get cumulative capacity for a specific year.

        Args:
            year_idx: Year index (1-indexed Julia style) - deprecated, use year instead
            year: Actual year (e.g., 2025, 2026, ...)

        Returns:
            Dictionary with cumulative capacities:
            - 'gen': {gen_idx (1-indexed): np.array per node}
            - 'bat': {bat_idx (1-indexed): np.array per node}
        """
        result = self.get_solution_values()

        # Convert year to year_idx if provided
        if year is not None:
            try:
                year_idx = self.years.index(year) + 1  # Convert to 1-indexed
            except ValueError:
                return {'gen': {}, 'bat': {}}
        elif year_idx is None:
            year_idx = 1

        return {
            'gen': result['cumulative_gen_capacity'].get(year_idx, {}),
            'bat': result['cumulative_bat_capacity'].get(year_idx, {}),
            'tech': result['cumulative_tech_capacity'].get(year_idx, {}),
            'bat_tech_power': result['cumulative_bat_tech_power'].get(year_idx, {}),
            'bat_tech_capacity': result['cumulative_bat_tech_capacity'].get(year_idx, {}),
        }

    def get_objective_value(self) -> float:
        """Get the objective function value (NPV of total costs, unscaled M$ -> $)."""
        jl = get_julia()

        if self._jl_model is None:
            return float("nan")

        jl._mp_model = self._jl_model
        return float(jl.seval("objective_value(_mp_model)")) * COST_UNSCALE

    def get_re_targets(self) -> Dict[int, float]:
        """Get RE penetration targets by year index."""
        if self._jl_targets is None:
            return {}

        return {int(k): float(v) for k, v in self._jl_targets.items()}

    def export_solution(self, filepath: str) -> None:
        """
        Export the solution to a CSV file (legacy-compatible format).

        Matches the legacy MasterProblem.export_solution() method.

        Args:
            filepath: Path to save the CSV file
        """
        import pandas as pd

        result = self.get_solution_values()
        solution = result.get('solution', {})

        if not solution:
            logger.warning("No solution to export")
            return

        rows = []
        for year in self.years:
            year_data = solution.get(year, {})
            for inv_type in ['tech_investment', 'bat_tech_power_investment',
                           'bat_tech_capacity_investment', 'transfer_investment']:
                for key, value in year_data.get(inv_type, {}).items():
                    rows.append({
                        'Year': year,
                        'Investment_Type': inv_type,
                        'Component': key,
                        'Value': value
                    })

        df = pd.DataFrame(rows)
        df.to_csv(filepath, index=False)
        logger.debug(f"Solution exported to {filepath}")

    @property
    def solution(self) -> Optional[Dict[str, Any]]:
        """
        Get the legacy-compatible solution dictionary.

        This property provides direct access to the solution in the same
        format as the legacy MasterProblem.solution attribute.
        """
        if self._jl_model is None:
            return None

        result = self.get_solution_values()
        return result.get('solution', {})


class MGAAdapter:
    """
    Python adapter for the near-optimal alternative-generation family.

    Two methods share this adapter — selected via ``MGAConfig.method``:

    - ``"mga"`` (default): wraps Julia's ``run_mga_spores``, the classical
      MGA Hop-Skip-Jump loop that iteratively maximises diversity under a
      cost-slack constraint. Produces ``num_alternatives`` alts.
    - ``"spores"``: wraps Julia's ``run_spores``, the spatially-explicit
      sweep that solves one alternative per entry in ``config.objectives``
      (Lombardi 2020 style). The number of alts equals
      ``len(config.objectives)``; ``num_alternatives`` is ignored.

    The result format is identical between the two paths — each
    alternative carries an ``objective`` tag (always ``"hsj_diversity"``
    for the MGA path) so downstream consumers (runner export, viewer)
    can render them uniformly.
    """

    def __init__(
        self,
        master_adapter: 'MasterProblemAdapter',
        mga_config: Any,
    ):
        """
        Initialize MGA adapter.

        Args:
            master_adapter: Configured MasterProblemAdapter (NOT yet built)
            mga_config: MGAConfig with method, num_alternatives, slack_fraction,
                       objectives (only for method='spores'), etc.
        """
        self.master = master_adapter
        self.config = mga_config
        self._jl_result = None
        # Cached method name; defaults to "mga" so configs that predate
        # the Phase-1 schema keep working unchanged.
        self._method: str = getattr(mga_config, "method", "mga")

    def run(self, use_representative_days: bool = True) -> Dict[str, Any]:
        """
        Run MGA or SPORES depending on ``config.method``.

        Args:
            use_representative_days: Whether to use representative days

        Returns:
            Dictionary with all alternatives, each tagged with the
            objective that produced it.
        """
        # Create Julia input once and reuse for whichever method is
        # selected. ``_create_input`` builds the Pydantic-to-Julia
        # marshalled input without constructing the optimisation model —
        # both ``run_mga_spores`` and ``run_spores`` do that internally.
        # Units the NPV-based retirement retired stay out.
        jl_input = self.master._with_npv_retirements(self.master._create_input())

        if self._method == "spores":
            self._jl_result = self._run_spores(jl_input, use_representative_days)
        else:
            self._jl_result = self._run_mga(jl_input, use_representative_days)

        num_alts = int(self._jl_result.num_alternatives)
        logger.info(
            f"{self._method.upper()}: Generated {num_alts} alternatives "
            "(including cost-optimal)"
        )
        return self._convert_mga_result()

    def _run_mga(self, jl_input: Any, use_representative_days: bool) -> Any:
        """Classical MGA sweep — sequential HSJ diversity loop."""
        ESFEX = get_esfex_module()
        logger.info(
            f"MGA: Running with {self.config.num_alternatives} alternatives, "
            f"slack={self.config.slack_fraction * 100:.1f}%"
        )
        return ESFEX.run_mga_spores(
            jl_input,
            num_alternatives=self.config.num_alternatives,
            slack_fraction=self.config.slack_fraction,
            use_representative_days=use_representative_days,
            investment_threshold=self.config.investment_threshold,
        )

    def _run_spores(self, jl_input: Any, use_representative_days: bool) -> Any:
        """SPORES sweep — one alternative per declared objective.

        Each entry in ``config.objectives`` is mapped to the matching
        Julia ``Symbol`` and handed to ``ESFEX.run_spores`` as a
        vector. The schema already validates that at least one objective
        is present when ``method='spores'``."""
        ESFEX = get_esfex_module()
        from juliacall import Main as _jl
        import json as _json

        objectives = list(getattr(self.config, "objectives", []) or [])
        if not objectives:
            # Defensive guard — should be unreachable because the schema
            # validator catches this case, but raising here turns any
            # late mutation into an immediate, attributable error.
            raise ValueError(
                "MGAConfig.method='spores' requires a non-empty "
                "'objectives' list."
            )
        # SporesObjective enum members carry a ``.value`` (lowercase
        # snake_case); plain strings pass through.
        obj_strings = [
            o.value if hasattr(o, "value") else str(o)
            for o in objectives
        ]
        # Marshal Python list[str] → Julia Vector{Symbol}. We assemble a
        # literal ``Symbol[…]`` expression and seval it — ``json.dumps``
        # gives us Julia-compatible double-quoted strings (Python's
        # ``repr`` would emit single quotes, which Julia parses as
        # character literals).
        symbol_literals = ",".join(
            f"Symbol({_json.dumps(s)})" for s in obj_strings
        )
        obj_symbols = _jl.seval(f"Symbol[{symbol_literals}]")

        logger.info(
            f"SPORES: Running with {len(obj_strings)} objective(s) "
            f"({', '.join(obj_strings)}), "
            f"slack={self.config.slack_fraction * 100:.1f}%"
        )
        return ESFEX.run_spores(
            jl_input,
            objectives=obj_symbols,
            slack_fraction=self.config.slack_fraction,
            use_representative_days=use_representative_days,
            investment_threshold=self.config.investment_threshold,
        )

    def _convert_mga_result(self) -> Dict[str, Any]:
        """
        Convert Julia MGAResult to Python dictionary.

        Returns:
            {
                'method': 'mga' | 'spores',
                'num_alternatives': int,
                'slack_fraction': float,
                'optimal_cost': float,
                'alternatives': [
                    {
                        'alternative_id': int,
                        'is_optimal': bool,
                        'cost': float,
                        'diversity_objective': float or None,
                        'objective': str,      # SPORES tag, e.g. 'hsj_diversity'
                        'solution': {...},
                        ...
                    },
                    ...
                ]
            }
        """
        if self._jl_result is None:
            raise RuntimeError("No MGA result. Call run() first.")

        jl_result = self._jl_result
        num_alts = int(jl_result.num_alternatives)

        # Phase 2 added ``objective_labels`` to the MGAResult struct. The
        # back-compat constructor in types.jl defaults the labels to
        # "hsj_diversity" for MGA runs, so the field is always present;
        # we still guard against pre-Phase-2 result objects that might
        # surface from cached pickles or older Julia compiles.
        labels = getattr(jl_result, "objective_labels", None)
        labels = list(labels) if labels is not None else []
        labels = [
            (lbl.decode() if isinstance(lbl, bytes) else str(lbl))
            for lbl in labels
        ]

        alternatives = []
        for k in range(num_alts):
            jl_alt = jl_result.alternatives[k]
            alt_dict = self.master._convert_single_result(jl_alt)

            alt_dict['alternative_id'] = k
            alt_dict['is_optimal'] = (k == 0)
            alt_dict['cost'] = float(jl_result.alternative_costs[k]) * COST_UNSCALE

            if k > 0 and k - 1 < len(jl_result.diversity_objectives):
                alt_dict['diversity_objective'] = float(
                    jl_result.diversity_objectives[k - 1]
                )
            else:
                alt_dict['diversity_objective'] = None

            # Objective tag — the SPORES objective that produced this alt,
            # or "cost_optimal" for the seed solve at index 0. Missing
            # labels (very old result files) default to the historically
            # accurate "hsj_diversity".
            if k == 0:
                alt_dict['objective'] = 'cost_optimal'
            else:
                label_idx = k - 1
                alt_dict['objective'] = (
                    labels[label_idx] if label_idx < len(labels)
                    else 'hsj_diversity'
                )

            alternatives.append(alt_dict)

        return {
            'method': self._method,
            'num_alternatives': num_alts,
            'slack_fraction': float(jl_result.slack_fraction),
            'optimal_cost': float(jl_result.optimal_cost) * COST_UNSCALE,
            'alternatives': alternatives,
        }


class ElectrolyzerAdapter:
    """
    Python adapter for the Julia Electrolyzer model.

    Handles hydrogen production from electricity.
    """

    def __init__(
        self,
        num_nodes: int,
        num_hours: int,
        electrolyzer_config: Dict[str, Any],
        var_prefix: str = "",
    ):
        """
        Initialize the Electrolyzer adapter.

        Args:
            num_nodes: Number of nodes
            num_hours: Number of hours
            electrolyzer_config: Electrolyzer configuration dictionary
            var_prefix: Optional prefix for variable names
        """
        self.num_nodes = num_nodes
        self.num_hours = num_hours
        self.config = electrolyzer_config
        self.var_prefix = var_prefix

        self._jl_model = None
        self._jl_vars = None
        self._jl_config = None

        logger.debug(f"ElectrolyzerAdapter initialized: {num_nodes} nodes, {num_hours} hours")

    def _create_julia_config(self) -> Any:
        """Create the Julia ElectrolyzerConfig struct."""
        ESFEX = get_esfex_module()

        cfg = self.config
        n = self.num_nodes

        # Handle per-node arrays
        def to_vector(val, default=0.0):
            if isinstance(val, (list, np.ndarray)):
                return py_to_julia_vector(val)
            return py_to_julia_vector([val] * n)

        jl_config = ESFEX.ElectrolyzerConfig(
            to_vector(cfg.get('rated_power', [0.0] * n)),
            to_vector(cfg.get('eff_at_rated', [0.7] * n)),
            to_vector(cfg.get('eff_at_min', [0.6] * n)),
            float(cfg.get('energy_per_kg_h2', 50.0)),  # kWh/kg H2
            to_vector(cfg.get('ramp_up', [1.0] * n)),
            to_vector(cfg.get('ramp_down', [1.0] * n)),
            py_to_julia_vector(scale_cost_list(cfg.get('invest_cost', [0.0] * n))),
            to_vector(cfg.get('invest_max_power', [0.0] * n)),
            py_to_julia_vector(scale_cost_list(cfg.get('fixed_cost', [0.0] * n))),
            py_to_julia_vector(scale_cost_list(cfg.get('variable_cost', [0.0] * n))),
            scale_cost(float(cfg.get('water_cost', 0.0))),
            to_vector(cfg.get('life_time', [25.0] * n)),
        )

        return jl_config

    def create_variables(self, model: Any = None) -> Any:
        """
        Create electrolyzer variables in the model.

        Args:
            model: JuMP model to add variables to (creates new if None)

        Returns:
            ElectrolyzerVariables container
        """
        ESFEX = get_esfex_module()
        jl = get_julia()

        if model is None:
            # Create new model
            model = jl.seval("using JuMP; Model()")

        self._jl_model = model
        self._jl_config = self._create_julia_config()

        # Build variables
        jl = get_julia()
        jl._e_model = model
        jl._e_config = self._jl_config
        jl._e_num_nodes = self.num_nodes
        jl._e_num_hours = self.num_hours
        jl._e_var_prefix = self.var_prefix

        self._jl_vars = jl.seval("""
        build_electrolyzer_variables!(
            _e_model, _e_config, _e_num_nodes, _e_num_hours;
            var_prefix=_e_var_prefix
        )
        """)

        return self._jl_vars

    def add_constraints(self):
        """Add electrolyzer constraints to the model."""
        ESFEX = get_esfex_module()
        jl = get_julia()

        if self._jl_model is None or self._jl_vars is None:
            raise RuntimeError("Variables not created. Call create_variables() first.")

        jl._e_model = self._jl_model
        jl._e_vars = self._jl_vars
        jl._e_config = self._jl_config
        jl._e_num_nodes = self.num_nodes
        jl._e_num_hours = self.num_hours

        jl.seval("""
        add_electrolyzer_constraints!(
            _e_model, _e_vars, _e_config, _e_num_nodes, _e_num_hours
        )
        """)

    def get_objective_terms(self) -> Any:
        """Get objective function terms from the electrolyzer model."""
        ESFEX = get_esfex_module()

        if self._jl_vars is None:
            raise RuntimeError("Variables not created. Call create_variables() first.")

        return ESFEX.get_electrolyzer_objective_terms(
            self._jl_vars,
            self._jl_config,
            self.num_nodes,
            self.num_hours,
        )

    def get_solution_values(self) -> Dict[str, Any]:
        """
        Extract solution values after solving.

        Returns:
            Dictionary with electrolyzer solution values
        """
        ESFEX = get_esfex_module()

        if self._jl_model is None or self._jl_vars is None:
            raise RuntimeError("Model not built.")

        jl_result = ESFEX.extract_electrolyzer_solution(
            self._jl_model,
            self._jl_vars,
            self._jl_config,
            self.num_nodes,
            self.num_hours,
        )

        # Convert to Python types
        return {
            'investment': np.array(jl_result.investment),
            'power': np.array(jl_result.power),
            'h2_production': np.array(jl_result.h2_production),
            'total_investment': float(jl_result.total_investment),
            'total_h2_produced': float(jl_result.total_h2_produced),
            'total_power_consumed': float(jl_result.total_power_consumed),
        }


class PrimaryEnergyAdapter:
    """
    Python adapter for the Julia PrimaryEnergy optimization model.

    Handles fuel supply chain and storage optimization with multi-scale
    temporal resolution.
    """

    def __init__(
        self,
        year: int,
        base_year: int,
        hours: int,
        num_nodes: int,
        fuels_config: Dict[str, Dict],
        non_electric_demand: Dict[str, Dict],
        infrastructure_config: Dict[str, Dict],
        transport_distances: List[List[float]],
        generators: List[Dict],
        fuels_definition: Dict[str, Dict],
        penalties_config: Dict[str, float],
        primary_energy_resolution: int = 24,
        investment_resolution: int = HOURS_STD_YEAR,
        discount_rate: float = 0.05,
        mode: str = "development",
        cumulative_capacities: Optional[Dict] = None,
        initial_storage_levels: Optional[Dict] = None,
        transport_routes: Optional[List[Dict]] = None,
        outage_schedule: Optional[list] = None,
        step_hours: int = 1,
        year_hour_offset: int = 0,
        scheduled_arrivals: Optional[Dict[str, np.ndarray]] = None,
    ):
        """
        Initialize the PrimaryEnergy adapter.

        Args:
            year: Simulation year
            base_year: Base year for calculations
            hours: Total simulation hours
            num_nodes: Number of nodes
            fuels_config: Primary energy sources configuration
            non_electric_demand: Non-electric demand configuration
            infrastructure_config: Infrastructure configuration
            transport_distances: Distance matrix (legacy, used as fallback)
            generators: Generator configurations
            fuels_definition: Fuel definitions
            penalties_config: Penalty configuration
            primary_energy_resolution: Resolution for fuel planning (hours)
            investment_resolution: Resolution for investment decisions (hours)
            discount_rate: Discount rate for annualization
            mode: The run's ``simulation_mode`` ("development",
                "unit_commitment", "economic_dispatch"), not the dispatch
                formulation; "master" for the master's day model. In
                "development" each window ends with its tanks at least 10 % full.
            cumulative_capacities: Fuel storage/transport capacity built by the
                master: {"storage"|"transport": {fuel: {node or route: capacity}}}
            initial_storage_levels: Initial storage levels for carry-over
            transport_routes: Route-based fuel transport list (preferred over distance matrix)
            step_hours: Length of a time step (h)
            year_hour_offset: First hour of the window within the year; with
                step_hours it places periods in their month for the
                non-electric demand's seasonal factors
            scheduled_arrivals: Fuel shipped in an earlier window and still in
                transit, ``{fuel: array [node, step]}`` arriving in this
                window's steps (from the previous window's
                :meth:`get_in_transit_arrivals`)
        """
        self.year = year
        self.base_year = base_year
        self.hours = hours
        self.num_nodes = num_nodes
        self.fuels_config = fuels_config
        self.non_electric_demand = non_electric_demand
        self.infrastructure_config = infrastructure_config
        self.transport_distances = transport_distances
        self.transport_routes = transport_routes or []
        self.outage_schedule = outage_schedule or []
        self.generators = generators
        self.fuels_definition = fuels_definition
        self.penalties_config = penalties_config
        self.primary_energy_resolution = primary_energy_resolution
        self.investment_resolution = investment_resolution
        self.discount_rate = discount_rate
        self.mode = mode
        self.cumulative_capacities = cumulative_capacities or {'storage': {}, 'transport': {}}
        self.initial_storage_levels = initial_storage_levels
        self.step_hours = step_hours
        self.year_hour_offset = year_hour_offset
        self.scheduled_arrivals = scheduled_arrivals or {}

        self._jl_model = None
        self._jl_vars = None
        self._jl_input = None
        self._jl_temporal = None
        self._adjusted_prices = None

        # Warn if loss penalty is below max fuel cost (user should fix in config)
        loss_pen = float(self.penalties_config.get('loss_of_fuel_supply', 100.0))
        max_fuel_cost = 0.0
        for fuel_name, fuel_cfg in self.fuels_config.items():
            fuel_def = self.fuels_definition.get(fuel_name, {})
            bp = float(fuel_def.get('price_base', 0.0))
            ic_list = fuel_cfg.get('import_cost', [0.0])
            max_ic = max(float(x) for x in ic_list) if ic_list else 0.0
            max_fuel_cost = max(max_fuel_cost, bp + max_ic)
        if loss_pen < max_fuel_cost * 1.5:
            logger.warning(
                f"PE loss_of_fuel_supply penalty ({loss_pen}) is below max fuel cost ({max_fuel_cost:.1f}). "
                f"The optimizer may prefer fuel loss over purchasing. "
                f"Consider increasing penalties.loss_of_fuel_supply in config."
            )

        # Generator to fuel mapping (computed during initialization)
        self.generator_fuel_map = self._create_generator_fuel_map()

        logger.debug(f"PrimaryEnergyAdapter initialized: {hours}h, {num_nodes} nodes, {len(fuels_config)} fuels")
        logger.debug(f"PE generator_fuel_map: {self.generator_fuel_map}")
        for gi, gen in enumerate(self.generators):
            logger.debug(f"  PE gen_list[{gi}] → Julia {gi+1}: name={gen.get('name','?')}, fuel={gen.get('fuel','?')}")

    def _create_generator_fuel_map(self) -> Dict[int, tuple]:
        """Create mapping from generator index to fuel information."""
        fuel_map = {}

        for gen_idx, gen in enumerate(self.generators):
            fuel_id = gen.get('fuel')

            # Skip non-fuel generators
            if fuel_id in ['Sun', 'Wind', 'Water', 'OTEC', 'None', None]:
                continue

            if fuel_id in self.fuels_definition:
                fuel_def = self.fuels_definition[fuel_id]
                energy_content = fuel_def.get('energy_content', 0.0)

                if energy_content > 1e-6:
                    eff_rated = gen.get('eff_at_rated', [0.35])
                    efficiency = np.mean(eff_rated) if isinstance(eff_rated, list) else eff_rated

                    # MWh_e per physical fuel unit
                    mwhe_per_unit = efficiency * energy_content

                    fuel_map[gen_idx] = (fuel_id, mwhe_per_unit, energy_content, efficiency)

        return fuel_map

    def _create_julia_input(self) -> Any:
        """Create the Julia PrimaryEnergyInput struct."""
        ESFEX = get_esfex_module()
        jl = get_julia()

        n = self.num_nodes

        # Convert fuel configurations
        jl_fuels = jl.seval("FuelConfig[]")
        for fuel_name, fuel_cfg in self.fuels_config.items():
            fuel_def = self.fuels_definition.get(fuel_name, {})

            # Scheduled fuel-source outages reuse FuelConfig's built-in
            # disruption window (start/end/availability, absolute horizon
            # hours). The PE model takes one window per source: the first
            # scheduled one that falls inside this model window.
            d_start = int(fuel_cfg.get('disruption_start_hour', 0))
            d_end = int(fuel_cfg.get('disruption_end_hour', 0))
            d_avail = float(fuel_cfg.get('disruption_availability', 1.0))
            win_lo = absolute_start_hour(self.year, self.base_year, self.year_hour_offset)
            win_hi = win_lo + int(self.hours) * max(1, int(self.step_hours))
            _spans = [
                (a, b, float(w[2]))
                for w in fuel_source_outage_windows(self.outage_schedule, fuel_name)
                for a, b in outage_window_spans(w, win_lo, win_hi)
            ]
            if _spans:
                d_start, d_end, d_avail = _spans[0]
                if len(_spans) > 1:
                    logger.warning(
                        "Fuel source %r has %d scheduled outages in hours %d–%d; "
                        "the primary-energy model applies one window per source "
                        "— using the first (%d–%d h).", fuel_name, len(_spans),
                        win_lo, win_hi, d_start, d_end,
                    )

            # Cost fields go through scale_cost to match the M$ convention of
            # the PowerSystem objective the PE terms get summed into.
            jl_fuel = ESFEX.FuelConfig(
                fuel_name,
                scale_cost(float(fuel_def.get('price_base', 0.0))),
                float(fuel_def.get('price_growth_rate', 0.0)),
                float(fuel_def.get('energy_content', 0.0)),
                # The fuel definition (fuels.<name>.emission_factor) carries the
                # factor; the source config has none, so this used to be 0.
                float(fuel_def.get('emission_factor', fuel_cfg.get('emission_factor', 0.0))),
                py_to_julia_vector(fuel_cfg.get('max_availability', [0.0] * n)),
                py_to_julia_vector(fuel_cfg.get('storage_capacity', [0.0] * n)),
                py_to_julia_vector(fuel_cfg.get('initial_storage_level', [0.5] * n)),
                float(fuel_cfg.get('min_storage_level', 0.0)),
                py_to_julia_vector(scale_cost_list(fuel_cfg.get('import_cost', [0.0] * n))),
                # Transport cost and losses are per route (fuel_transport_
                # routes); these fallbacks only feed Julia's distance-matrix
                # constructor, which the runner does not use
                0.0,
                0.0,
                float(fuel_cfg.get('transport_transit_days_per_100km', 0.0)),
                d_start,
                d_end,
                d_avail,
            )
            jl.seval("push!")(jl_fuels, jl_fuel)

        # Convert infrastructure configurations
        # Storage facilities and transport pipelines are keyed by facility/route ID,
        # but Julia expects Dict keyed by fuel name. Aggregate per-fuel configs.
        jl_infra = jl.seval("Dict{String, FuelInfrastructureConfig}()")
        storage_facilities = self.infrastructure_config.get('storage_facilities', {})
        transport_pipelines = self.infrastructure_config.get('transport_pipelines', {})

        # Collect per-fuel storage config from facilities
        fuel_storage_cfg: dict[str, dict] = {}
        for fac_id, fac_data in storage_facilities.items():
            fuel_params = fac_data.get('fuel_params', {})
            for fuel_name, fp in fuel_params.items():
                if fuel_name not in fuel_storage_cfg:
                    fuel_storage_cfg[fuel_name] = {
                        'efficiency': float(fp.get('efficiency', fac_data.get('efficiency', 0.8))),
                        'investment_cost': float(fp.get('investment_cost', fac_data.get('investment_cost', 0.0))),
                        'expansion_limit': float(fp.get('expansion_limit', fac_data.get('expansion_limit', 1.0))),
                        'lifetime': float(fp.get('lifetime', fac_data.get('lifetime', 30.0))),
                        'max_hourly_dispatch_rate_fraction_of_capacity': float(
                            fp.get('max_hourly_dispatch_rate_fraction_of_capacity',
                                   fac_data.get('max_hourly_dispatch_rate_fraction_of_capacity', -1.0))),
                    }

        # Collect per-fuel transport config from pipelines
        fuel_transport_cfg: dict[str, dict] = {}
        for route_id, route_data in transport_pipelines.items():
            fuel_params = route_data.get('fuel_params', {})
            for fuel_name, fp in fuel_params.items():
                if fuel_name not in fuel_transport_cfg:
                    fuel_transport_cfg[fuel_name] = {
                        'capacity': float(fp.get('capacity', route_data.get('capacity', 0.0))),
                        'investment_cost': float(fp.get('investment_cost', route_data.get('investment_cost', 0.0))),
                        'expansion_limit': float(fp.get('expansion_limit', route_data.get('expansion_limit', 0.5))),
                        'lifetime': float(fp.get('lifetime', route_data.get('lifetime', 20.0))),
                    }

        all_infra_fuels = set(fuel_storage_cfg.keys()) | set(fuel_transport_cfg.keys())
        for fuel_name in all_infra_fuels:
            s_cfg = fuel_storage_cfg.get(fuel_name, {})
            t_cfg = fuel_transport_cfg.get(fuel_name, {})

            jl_infra_config = ESFEX.FuelInfrastructureConfig(
                float(t_cfg.get('capacity', 0.0)),
                scale_cost(float(t_cfg.get('investment_cost', 0.0))),
                float(t_cfg.get('expansion_limit', 0.5)),
                scale_cost(float(s_cfg.get('investment_cost', 0.0))),
                float(s_cfg.get('expansion_limit', 1.0)),
                float(s_cfg.get('efficiency', 0.8)),
                float(t_cfg.get('lifetime', 20.0)),
                float(s_cfg.get('lifetime', 30.0)),
                float(s_cfg.get('max_hourly_dispatch_rate_fraction_of_capacity', -1.0)),
            )
            jl_infra[fuel_name] = jl_infra_config

        # Convert non-electric demand configurations
        jl_ne_demand = jl.seval("NonElectricDemandConfig[]")
        for demand_key, demand_cfg in self.non_electric_demand.items():
            parts = demand_key.split('_')
            sector = parts[0]
            fuel = demand_cfg.get('fuel', "_".join(parts[1:]))

            # Monthly factors, relative to their mean (even when absent)
            seasonal = demand_cfg.get('seasonal_factors') or [1.0] * 12
            if len(seasonal) != 12:
                logger.warning(
                    "Non-electric demand %r: seasonal_factors needs 12 monthly values, "
                    "got %d; using an even profile.", demand_key, len(seasonal))
                seasonal = [1.0] * 12

            jl_ne = ESFEX.NonElectricDemandConfig(
                sector,
                fuel,
                py_to_julia_vector(demand_cfg.get('demand', [0.0] * n)),
                float(demand_cfg.get('growth_rate', 0.0)),
                py_to_julia_vector(seasonal),
            )
            jl.seval("push!")(jl_ne_demand, jl_ne)

        # Convert generator fuel map
        jl_gen_map = jl.seval("Dict{Int, Tuple{String, Float64, Float64, Float64}}()")
        for gen_idx, fuel_info in self.generator_fuel_map.items():
            fuel_name, mwhe_per_unit, energy_content, eff = fuel_info
            # Julia is 1-indexed
            jl_gen_map[gen_idx + 1] = (fuel_name, mwhe_per_unit, energy_content, eff)

        # Convert cumulative capacities
        # {kind: {fuel: {0-based node/route: capacity}}} -> 1-based for Julia
        jl_cumul = jl.seval("Dict{String, Any}()")
        for kind in ("storage", "transport"):
            jl_kind = jl.seval("Dict{String, Dict{Int, Float64}}()")
            for fuel_name, by_idx in (self.cumulative_capacities.get(kind) or {}).items():
                jl_idx = jl.seval("Dict{Int, Float64}()")
                for idx, cap in by_idx.items():
                    jl_idx[int(idx) + 1] = float(cap)
                jl_kind[fuel_name] = jl_idx
            jl_cumul[kind] = jl_kind

        # Convert initial storage levels
        jl_initial_storage = jl.seval("nothing")
        if self.initial_storage_levels:
            jl_initial_storage = jl.seval("Dict{String, Vector{Float64}}()")
            for fuel_name, levels in self.initial_storage_levels.items():
                if isinstance(levels, dict):
                    # Convert {node_idx: level} dict to ordered list
                    max_idx = max(levels.keys()) if levels else -1
                    levels_list = [float(levels.get(i, 0.0)) for i in range(max_idx + 1)]
                    jl_initial_storage[fuel_name] = py_to_julia_vector(levels_list)
                else:
                    jl_initial_storage[fuel_name] = py_to_julia_vector(levels)

        # Fuel still in transit from the previous window: {fuel: [node, step]}
        jl_arrivals = jl.seval("nothing")
        if self.scheduled_arrivals:
            jl_arrivals = jl.seval("Dict{String, Matrix{Float64}}()")
            for fuel_name, arr in self.scheduled_arrivals.items():
                arr = np.asarray(arr, dtype=float)
                if arr.ndim == 2 and arr.size:
                    jl_arrivals[fuel_name] = py_to_julia_matrix(arr)

        # Build generator rated_power dict for coupling optimization (skip zero-capacity nodes)
        jl_gen_rated = jl.seval("Dict{Int, Vector{Float64}}()")
        for gen_idx in self.generator_fuel_map:
            gen = self.generators[gen_idx]
            rated = gen.get('rated_power', [0.0] * self.num_nodes)
            if isinstance(rated, (int, float)):
                rated = [rated] * self.num_nodes
            # Julia is 1-indexed
            jl_gen_rated[gen_idx + 1] = py_to_julia_vector(rated)

        # Build transport routes
        jl_routes = jl.seval("TransportRoute[]")
        for i, rt in enumerate(self.transport_routes):
            missing = [k for k in ("from_node", "to_node", "distance_km") if rt.get(k) is None]
            if missing:
                raise ValueError(f"Fuel transport route {rt.get('route_id', i)!r} has no "
                                 f"{', '.join(missing)}")
            jl_fparams = jl.seval("Dict{String, FuelRouteParams}()")
            for fuel_name, fp in rt.get("fuel_params", {}).items():
                jl_fparams[fuel_name] = ESFEX.FuelRouteParams(
                    float(fp.get("capacity", 0.0)),
                    scale_cost(float(fp.get("transport_cost", 0.0))),
                    float(fp.get("losses_fraction", 0.0)),
                )
            jl_route = ESFEX.TransportRoute(
                str(rt.get("route_id") or f"route_{i}"),
                int(rt["from_node"]) + 1,   # 0-indexed Python -> 1-indexed Julia
                int(rt["to_node"]) + 1,
                float(rt["distance_km"]),
                jl_fparams,
            )
            jl.seval("push!")(jl_routes, jl_route)

        jl_input = ESFEX.PrimaryEnergyInput(
            self.year,
            self.base_year,
            self.num_nodes,
            self.hours,
            jl_fuels,
            jl_infra,
            jl_ne_demand,
            jl_routes,
            jl_gen_map,
            self.primary_energy_resolution,
            self.investment_resolution,
            self.discount_rate,
            scale_cost(float(self.penalties_config.get('loss_of_fuel_supply', 1000.0))),
            self.mode,
            jl_cumul,
            jl_initial_storage,
            jl_gen_rated,
            scale_cost(float(self.penalties_config.get('non_electric_demand_loss', 100.0))),
            int(self.step_hours),
            int(self.year_hour_offset),
            jl_arrivals,
        )

        return jl_input

    def create_variables(self, model: Any):
        """
        Create primary energy variables in the model.

        Args:
            model: JuMP model to add variables to
        """
        ESFEX = get_esfex_module()

        self._jl_model = model
        self._jl_input = self._create_julia_input()

        # Create model components
        self._jl_vars, self._jl_temporal, self._adjusted_prices = \
            ESFEX.create_primary_energy_model(
                model, self._jl_input, outage_masks=self._outage_masks())

        logger.debug("PrimaryEnergy variables created")

    def _outage_masks(self) -> Any:
        """Scheduled tank and route outages of this window, for Julia.

        ``fuel_storage`` windows (``element_id`` = fuel, ``node`` = one node or
        None for all) give ``{fuel: [node × step]}``; ``fuel_route`` windows
        (``element_id`` = the route's ``route_id``, ``route_<i>`` for a route
        without one) give ``{route_id: [step]}``. Factors compound like the
        other outage masks (:func:`compile_outage_factor`).
        """
        jl = get_julia()
        out = jl.seval("Dict{String, Any}()")
        win_lo = absolute_start_hour(self.year, self.base_year, self.year_hour_offset)
        res = max(1, int(self.step_hours))
        route_ids = {str(rt.get("route_id") or f"route_{i}")
                     for i, rt in enumerate(self.transport_routes)}
        tanks: dict[str, np.ndarray] = {}
        routes: dict[str, np.ndarray] = {}
        for ow in self.outage_schedule:
            if ow.element_type not in ("fuel_storage", "fuel_route"):
                continue
            fac = compile_outage_factor([outage_window_tuple(ow)], win_lo, self.hours, res)
            if np.all(fac >= 1.0):
                continue
            if ow.element_type == "fuel_storage":
                if ow.element_id not in self.fuels_config:
                    logger.warning("Outage window for fuel storage %r: no such fuel; "
                                   "ignored.", ow.element_id)
                    continue
                mask = tanks.setdefault(ow.element_id, np.ones((self.num_nodes, self.hours)))
                nodes = range(self.num_nodes) if ow.node is None else [ow.node]
                for n in nodes:
                    if 0 <= n < self.num_nodes:
                        mask[n] *= fac
            else:
                if ow.element_id not in route_ids:
                    logger.warning("Outage window for fuel route %r: no such route_id; "
                                   "ignored.", ow.element_id)
                    continue
                routes.setdefault(ow.element_id, np.ones(self.hours))
                routes[ow.element_id] *= fac
        if tanks:
            jl_tanks = jl.seval("Dict{String, Matrix{Float64}}()")
            for fuel, mask in tanks.items():
                jl_tanks[fuel] = py_to_julia_matrix(mask)
            jl.seval("setindex!")(out, jl_tanks, "fuel_storage")
        if routes:
            jl_routes = jl.seval("Dict{String, Vector{Float64}}()")
            for rid, fac in routes.items():
                jl_routes[rid] = py_to_julia_vector(fac)
            jl.seval("setindex!")(out, jl_routes, "fuel_route")
        return out

    def add_constraints(self, model: Any):
        """Add primary energy constraints (called by create_variables)."""
        # Constraints are added in create_primary_energy_model
        pass

    def get_objective_terms(self) -> Any:
        """Get objective function terms from the primary energy model."""
        ESFEX = get_esfex_module()

        if self._jl_vars is None:
            raise RuntimeError("Variables not created. Call create_variables() first.")

        return ESFEX.get_primary_energy_objective_terms(
            self._jl_vars,
            self._jl_input,
            self._jl_temporal,
            self._adjusted_prices,
        )

    def integrate_with_power_system(self, power_system: PowerSystemAdapter):
        """
        Integrate with a PowerSystem model.

        Args:
            power_system: PowerSystemAdapter instance
        """
        ESFEX = get_esfex_module()
        jl = get_julia()

        if self._jl_vars is None:
            raise RuntimeError("PrimaryEnergy variables not created.")

        if power_system._jl_vars is None:
            raise RuntimeError("PowerSystem variables not created.")

        # Add coupling constraints
        jl._pe_model = self._jl_model
        jl._pe_vars = self._jl_vars
        jl._ps_vars = power_system._jl_vars
        jl._pe_input = self._jl_input

        # Get temporal resolution for correct energy-to-fuel conversion.
        # When resolution_hours > 1, gen_output (MW) × resolution_hours = MWh per timestep.
        res_hours = 1.0
        if hasattr(power_system, 'esfex_config') and power_system.esfex_config:
            temporal = getattr(power_system.esfex_config, 'temporal', None)
            if temporal:
                res_hours = float(getattr(temporal, 'resolution_hours', 1))
        jl._pe_resolution_hours = res_hours

        # Pass bus_to_node mapping so PE (node-indexed) can couple
        # with PS (bus-indexed) when num_buses > num_nodes, and the
        # electrolyzers whose hydrogen fills the Hydrogen tank.
        if power_system._jl_input is not None:
            jl._ps_input = power_system._jl_input
            jl.seval("""
            couple_primary_energy_to_power_system!(
                _pe_model, _pe_vars, _ps_vars, _pe_input;
                bus_to_node = _ps_input.network.bus_to_node,
                resolution_hours = _pe_resolution_hours,
                generators = _ps_input.generators,
                electrolyzers = _ps_input.electrolyzers
            )
            """)
        else:
            jl.seval("""
            couple_primary_energy_to_power_system!(
                _pe_model, _pe_vars, _ps_vars, _pe_input;
                resolution_hours = _pe_resolution_hours
            )
            """)

        # get_objective_terms returns a Dict so each PE sub-cost is both summed
        # into the objective AND merged into model.ext[:cost_expressions]; the
        # merge is what makes PE appear in the granular breakdown rather than
        # only inside `total`.
        pe_cost_terms = self.get_objective_terms()
        jl._pe_costs_dict = pe_cost_terms
        jl._ps_model = self._jl_model
        jl.seval("""
        using JuMP
        pe_sum = isempty(_pe_costs_dict) ? AffExpr(0.0) : sum(values(_pe_costs_dict))
        current_obj = objective_function(_ps_model)
        @objective(_ps_model, Min, current_obj + pe_sum)
        if haskey(_ps_model.ext, :cost_expressions)
            for (k, v) in _pe_costs_dict
                _ps_model.ext[:cost_expressions][k] = v
            end
        end
        """)

        logger.debug("PrimaryEnergy integrated with PowerSystem")

    def get_results(self, keep_steps: Optional[int] = None) -> Dict[str, Any]:
        """
        Get primary energy results after solving.

        Args:
            keep_steps: Committed steps of a rolling-horizon window; the cost
                totals cover only those (a period counts for the share of
                its steps among them). Default: all steps.

        Returns:
            Dictionary with solution values (``total_fuel_cost``,
            ``total_transport_cost``, ``total_loss_penalty`` in $;
            ``fuel_for_power``: ``{generator index (0-based): [node x step]}``,
            the fuel each coupled unit burns per step)
        """
        ESFEX = get_esfex_module()

        if self._jl_model is None:
            raise RuntimeError("Model not built.")

        keep = self.hours if keep_steps is None else max(1, min(int(keep_steps), self.hours))
        jl_result = ESFEX.extract_primary_energy_solution(
            self._jl_model,
            self._jl_vars,
            self._jl_input,
            self._jl_temporal,
            keep=keep,
        )
        hourly = ESFEX.extract_hourly_results(self._jl_vars, self._jl_input, self._jl_temporal)

        # Convert to Python types
        return {
            'transport_investments': {k: np.array(v) for k, v in jl_result.transport_investments.items()},
            'storage_investments': {k: np.array(v) for k, v in jl_result.storage_investments.items()},
            'total_fuel_supply': {k: np.array(v) for k, v in jl_result.total_fuel_supply.items()},
            'total_ne_demand_satisfied': {k: np.array(v) for k, v in jl_result.total_ne_demand_satisfied.items()},
            'total_loss_of_supply': {k: np.array(v) for k, v in jl_result.total_loss_of_supply.items()},
            'final_storage_levels': {k: np.array(v) for k, v in jl_result.final_storage_levels.items()},
            'transport_flows': {k: np.array(v) for k, v in jl_result.transport_flows.items()},
            # Julia costs are in M$; results are in $ like every other cost
            'total_fuel_cost': float(jl_result.total_fuel_cost) * COST_UNSCALE,
            'total_transport_cost': float(jl_result.total_transport_cost) * COST_UNSCALE,
            'total_loss_penalty': float(jl_result.total_loss_penalty) * COST_UNSCALE,
            'fuel_for_power': {
                int(g) - 1: np.array(m, dtype=float)
                for g, m in hourly["fuel_for_power_hourly"].items()
            },
        }

    def get_debug_info(self) -> Dict[str, Any]:
        """Extract detailed PE solution values for debugging."""
        jl = get_julia()
        info = {}
        try:
            # fuel_for_power_hourly: gen_idx → [node, hour]
            for gen_idx_jl in self._jl_vars.fuel_for_power_hourly:
                gen_idx = int(gen_idx_jl)
                mat = self._jl_vars.fuel_for_power_hourly[gen_idx_jl]
                total = float(jl.seval("m -> sum(JuMP.value.(m))")(mat))
                info[f'fuel_for_power_gen{gen_idx}'] = total

            # fuel_supply_periodic: fuel → [node, period]
            for fuel_jl in self._jl_vars.fuel_supply_periodic:
                fuel = str(fuel_jl)
                mat = self._jl_vars.fuel_supply_periodic[fuel_jl]
                total = float(jl.seval("m -> sum(JuMP.value.(m))")(mat))
                info[f'supply_periodic_{fuel}'] = total

            # hourly loss: hr_loss_supply
            if hasattr(self._jl_vars, 'fuel_loss_of_supply_hourly'):
                for fuel_jl in self._jl_vars.fuel_loss_of_supply_hourly:
                    fuel = str(fuel_jl)
                    mat = self._jl_vars.fuel_loss_of_supply_hourly[fuel_jl]
                    total = float(jl.seval("m -> sum(JuMP.value.(m))")(mat))
                    info[f'loss_hourly_{fuel}'] = total

            # storage_in, storage_out hourly
            if hasattr(self._jl_vars, 'fuel_storage_in_hourly'):
                for fuel_jl in self._jl_vars.fuel_storage_in_hourly:
                    fuel = str(fuel_jl)
                    mat = self._jl_vars.fuel_storage_in_hourly[fuel_jl]
                    total = float(jl.seval("m -> sum(JuMP.value.(m))")(mat))
                    info[f'storage_in_{fuel}'] = total

            if hasattr(self._jl_vars, 'fuel_storage_out_hourly'):
                for fuel_jl in self._jl_vars.fuel_storage_out_hourly:
                    fuel = str(fuel_jl)
                    mat = self._jl_vars.fuel_storage_out_hourly[fuel_jl]
                    total = float(jl.seval("m -> sum(JuMP.value.(m))")(mat))
                    info[f'storage_out_{fuel}'] = total

        except Exception as e:
            info['debug_error'] = str(e)
        return info

    def get_in_transit_arrivals(self, step: Optional[int] = None) -> Dict[str, np.ndarray]:
        """Fuel still in transit after *step* timesteps, for the next window.

        Shipments that left in steps 1..*step* (default: the whole window)
        and arrive later, plus scheduled arrivals of this window due after
        *step*. Pass the result as the next window's ``scheduled_arrivals``,
        alongside :meth:`get_final_storage_levels` with the same *step*.

        Returns:
            ``{fuel: array [node, s]}``: quantity reaching the node's tank in
            step s of the next window (net of transport losses).
        """
        ESFEX = get_esfex_module()
        k = self.hours if step is None else min(int(step), self.hours)
        jl_out = ESFEX.in_transit_arrivals(
            self._jl_vars, self._jl_input, self._jl_temporal, k)
        return {str(f): np.array(m, dtype=float) for f, m in jl_out.items()}

    def get_final_storage_levels(self, step: Optional[int] = None) -> Dict[str, Dict[int, float]]:
        """
        Get the tank levels for carry-over to the next window.

        Uses the tank level after *step* timesteps (default: the whole
        window, t=hours+1). Rolling horizons pass the committed steps, where
        the next window starts.

        Returns:
            Dictionary with {fuel_id: {node_idx: level}}
        """
        jl = get_julia()

        final_levels = {}
        # Tank levels (the model's only fuel storage)
        for fuel_name_jl in self._jl_vars.fuel_storage_level_hourly:
            fuel_name = str(fuel_name_jl)
            mat = self._jl_vars.fuel_storage_level_hourly[fuel_name_jl]
            n_nodes = self.num_nodes
            # hourly storage has hours+1 timesteps (0..hours)
            last_t = (self.hours if step is None else min(step, self.hours)) + 1
            final_levels[fuel_name] = {}
            for n in range(n_nodes):
                try:
                    val = float(jl.seval("JuMP.value")(mat[n + 1, last_t]))
                    final_levels[fuel_name][n] = val
                except Exception:
                    # Fallback to the window-end level of the results
                    try:
                        results = self.get_results()
                        levels = results['final_storage_levels'].get(fuel_name, [])
                        final_levels[fuel_name][n] = float(levels[n]) if n < len(levels) else 0.0
                    except Exception:
                        final_levels[fuel_name][n] = 0.0

        return final_levels
