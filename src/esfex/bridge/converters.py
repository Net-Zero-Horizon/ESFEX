"""
Type converters between Python and Julia for ESFEX.

Provides functions to convert Python data structures (numpy arrays,
dictionaries, Pydantic models) to Julia types and vice versa.

COST SCALING: All monetary values are scaled by COST_SCALE (1e-6) when
passing to Julia ($ → M$) and unscaled by COST_UNSCALE (1e6) when
extracting results (M$ → $). This improves numerical conditioning of the
optimization model. Physical units (MW, MWh, tonnes, etc.) are unchanged.
"""

from typing import Any, List, Optional, Union

import numpy as np

from esfex.utils.temporal import HOURS_STD_YEAR

# ---------------------------------------------------------------------------
# Cost scaling constants: Julia model operates in M$ (millions of dollars)
# ---------------------------------------------------------------------------
COST_SCALE = 1e-6    # $ → M$  (multiply when sending to Julia)
COST_UNSCALE = 1e6   # M$ → $  (multiply when extracting from Julia)

# Dispatch cost-breakdown entries a window spends against the operating
# budget (max_annual_operating_cost; the Julia OPERATING_BUDGET_COSTS).
OPERATING_BUDGET_COSTS = ("fuel_cost", "maintenance_cost", "fixed_om_cost",
                          "pe_supply_cost", "pe_transport_cost")

# Cost-breakdown entries that are not costs of their own: the total, the
# electrolyzers' cost when their hydrogen fills the primary-energy tank (a
# view of electrolyzer_cost), the V2G payment to EV owners (a transfer,
# never in the objective) and the money spent above the operating budget
# (already in fuel, maintenance and fixed O&M; its penalty is a cost). The
# other entries add up to the total.
COST_BREAKDOWN_NON_COMPONENTS = ("total", "pe_electrolyzer_cost", "v2g_compensation",
                                 "operating_budget_excess")


def scale_cost(val: float) -> float:
    """Scale a single monetary value from $ to M$."""
    return val * COST_SCALE


def line_flow_cost_per_mwh(line) -> float:
    """Flow cost of a line in $ per MWh carried: ``length_km × cost_per_mw_km``
    (inter-system links; 0 for other lines). The master and the dispatch both
    charge it on |flow|."""
    cost = float(getattr(line, "cost_per_mw_km", 0.0) or 0.0)
    return max(0.0, cost * float(getattr(line, "length_km", 0.0) or 0.0))


def scale_cost_list(arr: list) -> list:
    """Scale a list of monetary values from $ to M$."""
    return [x * COST_SCALE for x in arr]


def battery_availability_key(key: str) -> str:
    """Key of battery *key*'s profile in the runner's availability cache
    (apart from the generators', which use the bare unit key)."""
    return f"battery:{key}"


def battery_technology_availability_key(key: str) -> str:
    """Key of battery technology *key*'s profile in the availability cache."""
    return f"battery_technology:{key}"


def _per_node(arr, n: int) -> list:
    """A per-node list of length *n* from an optional field: a single value
    applies to every node, missing nodes get 0."""
    vals = [float(v) for v in (arr or [])]
    if len(vals) == 1:
        return vals * n
    return (vals + [0.0] * n)[:n]
from esfex.config.schema import (
    BatteryConfig,
    BatteryTechnologyConfig,
    BusConfig,
    CostCurveBlock,
    CostCurveConfig,
    DCPowerFlowConfig,
    GeneratorConfig,
    NodeConfig,
    ScenarioMultipliers,
    SolverConfig,
    TechnologyConfig,
    TemporalConfig,
    TransformerConfig,
    TransmissionLineGeo,
    normalize_cost_curve,
)


def py_to_julia_vector(arr: Union[np.ndarray, list]) -> Any:
    """
    Convert a Python list or 1D numpy array to a Julia Vector.

    Args:
        arr: Python list or 1D numpy array

    Returns:
        Julia Vector
    """
    from esfex.bridge.julia_setup import get_julia

    jl = get_julia()

    if isinstance(arr, np.ndarray):
        arr = arr.astype(np.float64)
    else:
        arr = np.array(arr, dtype=np.float64)

    # Use juliacall's pyconvert for numpy arrays
    return jl.seval("Vector{Float64}")(arr)


def py_to_julia_int_vector(arr: Union[np.ndarray, list]) -> Any:
    """
    Convert a Python list or 1D numpy array to a Julia Vector{Int}.

    Args:
        arr: Python list or 1D numpy array of integers

    Returns:
        Julia Vector{Int}
    """
    from esfex.bridge.julia_setup import get_julia

    jl = get_julia()

    if isinstance(arr, np.ndarray):
        arr = arr.astype(np.int64)
    else:
        arr = np.array(arr, dtype=np.int64)

    # Use juliacall's pyconvert for numpy arrays
    return jl.seval("Vector{Int}")(arr)


def py_to_julia_matrix(arr: np.ndarray) -> Any:
    """
    Convert a 2D numpy array to a Julia Matrix{Float64}.

    Uses bulk transfer: flatten in Fortran (column-major) order, transfer as
    a single Vector{Float64}, then reshape in Julia.  This avoids the O(rows*cols)
    Python→Julia boundary crossings of element-by-element setindex!.

    Args:
        arr: 2D numpy array

    Returns:
        Julia Matrix{Float64}
    """
    from esfex.bridge.julia_setup import get_julia

    jl = get_julia()

    if not isinstance(arr, np.ndarray):
        arr = np.array(arr)

    if arr.ndim != 2:
        raise ValueError(f"Expected 2D array, got {arr.ndim}D")

    arr = np.ascontiguousarray(arr, dtype=np.float64)
    rows, cols = arr.shape

    # Julia is column-major: flatten in Fortran order, bulk-transfer, reshape
    flat = np.asfortranarray(arr).ravel(order='F')
    jl_vec = jl.seval("Vector{Float64}")(flat)
    return jl.seval("reshape")(jl_vec, rows, cols)


def py_to_julia_dict(d: dict) -> Any:
    """
    Convert a Python dictionary to a Julia Dict.

    Args:
        d: Python dictionary

    Returns:
        Julia Dict
    """
    from esfex.bridge.julia_setup import get_julia

    jl = get_julia()

    return jl.Dict(d)


def blocks_to_julia_cost_segments(blocks: list[CostCurveBlock]) -> Any:
    """Convert a list of CostCurveBlock to a Julia Vector{CostSegment}.

    Parameters
    ----------
    blocks:
        Normalised cost curve blocks (fraction + price per block).

    Returns
    -------
    Julia ``Vector{CostSegment}``
    """
    from esfex.bridge.julia_setup import get_julia, get_esfex_module

    jl = get_julia()
    ESFEX = get_esfex_module()

    jl_vec = jl.seval("CostSegment[]")
    for b in blocks:
        seg = ESFEX.CostSegment(float(b.fraction), scale_cost(float(b.price)))
        jl.seval("push!")(jl_vec, seg)
    return jl_vec


def build_gen_cost_curves_dict(
    generators: list,
    gen_configs: list,
    num_buses: int,
    bus_to_node: Optional[list] = None,
    gen_bus_per_node: Optional[dict] = None,
    per_bus_keys: Optional[set] = None,
    all_buses_of_node: bool = False,
) -> Any:
    """Build Julia ``Dict{Int, Dict{Int, Vector{CostSegment}}}`` for generators.

    Every curve that changes the unit's price is included: multi-segment
    curves, and single-segment (flat) curves whose price differs from the
    unit's ``fuel_cost`` (the solver then charges that price instead). A flat
    curve at ``fuel_cost`` (e.g. no ``flat_price``) is skipped, since it
    prices the output exactly as having no curve does.

    Parameters
    ----------
    generators:
        Ordered list of ``(key, GeneratorConfig)`` tuples matching Julia push order.
    gen_configs:
        Not used (reserved for virtual generators). Pass empty list.
    num_buses:
        Number of buses in the system.
    bus_to_node:
        0-indexed bus→node mapping (None for single-bus-per-node systems).
    gen_bus_per_node:
        Generator key → {node_idx: bus_idx} (0-based) for explicit physical
        placement. Falls back to the first bus of each node when missing.
    per_bus_keys:
        Keys of units whose arrays (curves included) are already per bus
        (virtual generators from master investments): curve ``i`` belongs to
        bus ``i``, and buses without rated power get no curve.
    all_buses_of_node:
        Give the node's curve to every bus of the node (candidate
        technologies, which the master may build at any bus of a node,
        their per-bus arrays laid out in "property" mode).

    Returns
    -------
    Julia ``Dict{Int, Dict{Int, Vector{CostSegment}}}``
    """
    from esfex.bridge.julia_setup import get_julia

    jl = get_julia()

    outer = jl.seval("Dict{Int, Dict{Int, Vector{CostSegment}}}()")

    for g_idx_0, (key, gen) in enumerate(generators):
        g = g_idx_0 + 1  # Julia 1-based

        curves = gen.fuel_cost_curve
        if not curves:
            continue

        inner = jl.seval("Dict{Int, Vector{CostSegment}}()")
        has_entries = False

        per_bus = per_bus_keys is not None and key in per_bus_keys
        for node_idx, curve in enumerate(curves):
            if curve is None:
                continue
            if per_bus and not (node_idx < len(gen.rated_power)
                                and gen.rated_power[node_idx] > 0):
                continue  # no unit at this bus: no output variable to split
            fuel_cost = gen.fuel_cost[node_idx] if node_idx < len(gen.fuel_cost) else 0.0
            blocks = normalize_cost_curve(curve, fallback_price=fuel_cost)
            if len(blocks) == 1 and blocks[0].price == fuel_cost:
                continue

            # Determine bus index (1-based) for this node — use the
            # per-(unit, node) physical placement when available, else
            # role-aware fallback.
            if per_bus:
                buses_1 = [node_idx + 1]
            elif bus_to_node is not None and all_buses_of_node:
                buses_1 = [b + 1 for b, n in enumerate(bus_to_node) if n == node_idx]
            elif bus_to_node is not None:
                bus_0 = None
                if gen_bus_per_node is not None:
                    bus_0 = gen_bus_per_node.get(key, {}).get(node_idx)
                if bus_0 is None:
                    bus_0 = next(
                        (b for b, n in enumerate(bus_to_node) if n == node_idx),
                        node_idx,
                    )
                buses_1 = [bus_0 + 1]
            else:
                buses_1 = [node_idx + 1]

            for bus_1 in buses_1:
                inner[bus_1] = blocks_to_julia_cost_segments(blocks)
                has_entries = True

        if has_entries:
            outer[g] = inner

    return outer


def build_bat_cost_curves_dict(
    batteries: list,
    num_buses: int,
    bus_to_node: Optional[list] = None,
    bat_bus_per_node: Optional[dict] = None,
) -> Any:
    """Build Julia ``Dict{Int, Dict{Int, Vector{CostSegment}}}`` for batteries.

    Every discharge curve that changes the battery's price is included:
    multi-segment curves, and single-segment (flat) curves whose price
    differs from the battery's ``throughput_degradation_cost`` (the solver
    then charges that price instead), as for generators.

    Parameters
    ----------
    batteries:
        Ordered list of ``(key, BatteryConfig)`` tuples matching Julia push order.
    num_buses:
        Number of buses.
    bus_to_node:
        0-indexed bus→node mapping.
    bat_to_bus:
        Battery key → target bus index (0-based).

    Returns
    -------
    Julia ``Dict{Int, Dict{Int, Vector{CostSegment}}}``
    """
    from esfex.bridge.julia_setup import get_julia

    jl = get_julia()

    outer = jl.seval("Dict{Int, Dict{Int, Vector{CostSegment}}}()")

    for bi_0, (key, bat) in enumerate(batteries):
        bi = bi_0 + 1  # Julia 1-based

        curves = bat.discharge_cost_curve
        if not curves:
            continue

        fallback_costs = bat.throughput_degradation_cost or [0.0] * len(curves)

        inner = jl.seval("Dict{Int, Vector{CostSegment}}()")
        has_entries = False

        for node_idx, curve in enumerate(curves):
            fb = fallback_costs[node_idx] if node_idx < len(fallback_costs) else 0.0
            blocks = normalize_cost_curve(curve, fallback_price=fb)
            if not blocks or (len(blocks) == 1 and blocks[0].price == fb):
                continue

            if bus_to_node is not None:
                bus_0 = None
                if bat_bus_per_node is not None:
                    bus_0 = bat_bus_per_node.get(key, {}).get(node_idx)
                if bus_0 is None:
                    bus_0 = next(
                        (b for b, n in enumerate(bus_to_node) if n == node_idx),
                        node_idx,
                    )
                bus_1 = bus_0 + 1
            else:
                bus_1 = node_idx + 1

            inner[bus_1] = blocks_to_julia_cost_segments(blocks)
            has_entries = True

        if has_entries:
            outer[bi] = inner

    return outer


def julia_to_py_array(jl_arr: Any) -> np.ndarray:
    """
    Convert a Julia Array to a numpy array.

    Args:
        jl_arr: Julia Array

    Returns:
        numpy array
    """
    # juliacall handles this automatically
    return np.array(jl_arr)


def julia_to_py_dict(jl_dict: Any) -> dict:
    """
    Convert a Julia Dict to a Python dictionary.

    Args:
        jl_dict: Julia Dict

    Returns:
        Python dictionary
    """
    return dict(jl_dict)


def convert_index_py_to_julia(idx: int) -> int:
    """
    Convert a Python 0-based index to Julia 1-based index.

    Args:
        idx: Python 0-based index

    Returns:
        Julia 1-based index
    """
    return idx + 1


def convert_index_julia_to_py(idx: int) -> int:
    """
    Convert a Julia 1-based index to Python 0-based index.

    Args:
        idx: Julia 1-based index

    Returns:
        Python 0-based index
    """
    return idx - 1


def node_bus_mapping_required(buses, num_nodes: int) -> bool:
    """Whether per-node arrays must be mapped onto the bus network.

    Per-node arrays (``rated_power``, availability, reserves, …) are indexed by
    node; the operational model is indexed by bus. The two coincide *only* when
    the bus list is the identity mapping — one bus per node, in node order.

    The historical test was ``num_buses > num_nodes``, which assumed the bus
    network is always a refinement of the node network. A reduced network
    breaks that assumption: ``reduce_to_node_mesh`` followed by
    ``drop_isolated_components`` leaves FEWER buses than nodes (every node whose
    buses were dropped). Under the old test the mapping was skipped and
    node-indexed arrays were consumed *positionally* as bus-indexed ones, so
    each node past the first bus-less node had its capacity silently relocated
    onto another node's bus — Japan: 70 % of 330 GW landed on the wrong bus, so
    34 % of load shed at VOLL while that capacity sat idle (and renewables were
    curtailed at the same hour). The same skip left bus-level results
    un-aggregated, exported as if node-level.
    """
    if not buses:
        return False
    if len(buses) != num_nodes:
        return True
    return any(
        int(getattr(b, "parent_node", i) or 0) != i for i, b in enumerate(buses)
    )


def expand_node_to_bus_array(
    node_array: list,
    bus_to_node: list,
    mode: str = "capacity",
    bus_per_node: Optional[dict] = None,
    replicate_to_all_buses_in_node: bool = False,
) -> list:
    """Expand a per-node array to a per-bus array.

    Args:
        node_array: Per-node values (length = num_nodes).
        bus_to_node: Mapping from bus index to node index (0-indexed).
        mode: "capacity" — physical capacity placement.  Each active node's
              value is placed at the bus indicated by ``bus_per_node`` (the
              real physical bus for an existing unit); for nodes lacking a
              specific bus, the value is placed at the node's first
              (canonical) bus.  If ``replicate_to_all_buses_in_node`` is
              True, the value is copied to *every* bus of the node (used
              for virtual gens from master investments — paired with a
              node-level capacity constraint added in the Julia model).
              "property" — ALL buses at a node inherit the node's value.
        bus_per_node: Per-unit physical placement ``{node_idx: bus_idx}``
              derived from ``_resolve_element_bus_mapping`` (endpoint /
              ``bus_index`` field).

    Returns:
        Per-bus array (length = num_buses).
    """
    num_buses = len(bus_to_node)
    result = [0.0] * num_buses

    if mode == "capacity":
        # Build the per-node → set-of-buses mapping for replication mode.
        if replicate_to_all_buses_in_node:
            buses_of_node: dict = {}
            for b_idx, n_idx in enumerate(bus_to_node):
                buses_of_node.setdefault(n_idx, []).append(b_idx)
            for node_idx, val in enumerate(node_array):
                if not val or val <= 0:
                    continue
                for b_idx in buses_of_node.get(node_idx, []):
                    result[b_idx] = val
            return result

        for node_idx, val in enumerate(node_array):
            if not val or val <= 0:
                continue
            # Physical placement: the unit's actual electrical bus, as
            # resolved from explicit topology (endpoint / bus_index).
            if bus_per_node is not None and node_idx in bus_per_node:
                bi = bus_per_node[node_idx]
                if bi is not None and 0 <= bi < num_buses:
                    result[bi] += val
                    continue
            # No explicit bus given for this node's capacity → place it at
            # the node's first (canonical) bus. Deterministic and neutral:
            # no role/demand-weighted redistribution heuristics.
            for b_idx, n in enumerate(bus_to_node):
                if n == node_idx:
                    result[b_idx] += val
                    break
    else:  # "property"
        for bus_idx in range(num_buses):
            node_idx = bus_to_node[bus_idx]
            if node_idx < len(node_array):
                result[bus_idx] = node_array[node_idx]

    return result


def expand_node_to_bus_matrix(
    node_matrix: np.ndarray,
    bus_to_node: list,
    default_value: float = 1.0,
) -> np.ndarray:
    """Expand a per-node matrix [hours x nodes] to per-bus [hours x buses].

    All buses at a node see the same profile (property-mode expansion).

    Args:
        node_matrix: Shape (hours, num_nodes).
        bus_to_node: Mapping from bus index to node index (0-indexed).
        default_value: Fill value when node index is out of range.

    Returns:
        Shape (hours, num_buses).
    """
    hours = node_matrix.shape[0]
    num_buses = len(bus_to_node)
    result = np.full((hours, num_buses), default_value)
    for bus_idx in range(num_buses):
        node_idx = bus_to_node[bus_idx]
        if node_idx < node_matrix.shape[1]:
            result[:, bus_idx] = node_matrix[:, node_idx]
    return result


def convert_transmission_line_data(
    line: TransmissionLineGeo,
    dc_config: DCPowerFlowConfig,
    distances: Optional[np.ndarray] = None,
    s_base_mva: float = 100.0,
) -> Any:
    """
    Convert a Python TransmissionLineGeo to Julia TransmissionLineData struct.

    Args:
        line: Python TransmissionLineGeo
        dc_config: DC power flow defaults (for fallback reactance calculation)
        distances: Optional distance matrix (for fallback length)
        s_base_mva: System power base (MVA) of the per-unit values

    Returns:
        Julia TransmissionLineData struct
    """
    from esfex.bridge.julia_setup import get_esfex_module

    ESFEX = get_esfex_module()

    # Bus indices required.  The previous "fall back to node index"
    # path silently mixed bus-level and node-level coordinates when
    # ``from_bus``/``to_bus`` were missing (legacy YAML, partial
    # construction), producing a Julia model whose topology did NOT
    # match the GUI.  Now we log a warning and refuse to emit the
    # line; the caller is expected to populate from_bus / to_bus via
    # endpoint resolution before reaching this function.
    if line.from_bus is None or line.to_bus is None:
        import logging
        logging.getLogger(__name__).warning(
            "Skipping transmission line %r: from_bus=%r to_bus=%r — "
            "bus indices unresolved (line will not appear in Julia "
            "model). Run validation auto-fix to repair endpoints.",
            getattr(line, "line_id", "?"),
            line.from_bus, line.to_bus,
        )
        return None
    from_idx = line.from_bus
    to_idx = line.to_bus

    # Determine line length
    length_km = line.length_km or 0.0
    if length_km <= 0 and distances is not None:
        i, j = from_idx, to_idx
        if i < distances.shape[0] and j < distances.shape[1]:
            length_km = float(distances[i, j])

    # Reactance of the whole line: its own value, else derived from its length
    # (one circuit's reactance divided over its circuits)
    from esfex.utils.per_unit import line_reactance_pu
    reactance_pu = line_reactance_pu(line, length_km, dc_config, s_base_mva)

    resistance_pu = line.resistance_pu if line.resistance_pu is not None else 0.0
    susceptance_pu = line.susceptance_pu if line.susceptance_pu is not None else 0.0
    voltage_kv = line.voltage_kv if line.voltage_kv is not None else dc_config.voltage_level_kv
    capacity_mw = line.capacity_mw if line.capacity_mw is not None else 0.0
    line_id = line.line_id or f"line_{from_idx}_{to_idx}"

    frequency_hz = getattr(line, 'frequency_hz', 50.0)
    current_type = getattr(line, 'current_type', 'AC')

    return ESFEX.TransmissionLineData(
        line_id,
        convert_index_py_to_julia(from_idx),
        convert_index_py_to_julia(to_idx),
        capacity_mw,
        reactance_pu,
        resistance_pu,
        susceptance_pu,
        length_km,
        voltage_kv,
        line.num_circuits,
        float(frequency_hz),
        str(current_type),
        scale_cost(line_flow_cost_per_mwh(line)),
    )


def convert_transformer_data(trafo: TransformerConfig) -> Any:
    """
    Convert a Python TransformerConfig to Julia TransformerData struct.

    Derives series resistance/reactance from impedance and losses fraction.

    Args:
        trafo: Python TransformerConfig

    Returns:
        Julia TransformerData struct
    """
    import math
    from esfex.bridge.julia_setup import get_esfex_module

    ESFEX = get_esfex_module()

    z_pu = trafo.impedance_pu
    losses_frac = trafo.losses_fraction

    # Derive r and x from impedance
    if trafo.resistance_pu is not None:
        r_pu = trafo.resistance_pu
    else:
        r_pu = losses_frac * z_pu

    x_pu_sq = z_pu**2 - r_pu**2
    x_pu = math.sqrt(max(x_pu_sq, 1e-12))

    tap_ratio = trafo.from_voltage_kv / trafo.to_voltage_kv

    from_idx = trafo.from_bus if trafo.from_bus is not None else trafo.from_node
    to_idx = trafo.to_bus if trafo.to_bus is not None else trafo.to_node

    return ESFEX.TransformerData(
        trafo.name,
        convert_index_py_to_julia(from_idx),
        convert_index_py_to_julia(to_idx),
        trafo.from_voltage_kv,
        trafo.to_voltage_kv,
        trafo.rated_power_mva,
        z_pu,
        r_pu,
        x_pu,
        tap_ratio,
        losses_frac,
    )


def convert_acdc_converter_data(conv) -> Any:
    """Convert a Python ACDCConverterConfig to Julia ACDCConverterData struct."""
    from esfex.bridge.julia_setup import get_esfex_module

    ESFEX = get_esfex_module()

    from_idx = getattr(conv, 'from_bus', None)
    if from_idx is None:
        from_idx = conv.from_node
    to_idx = getattr(conv, 'to_bus', None)
    if to_idx is None:
        to_idx = conv.to_node

    return ESFEX.ACDCConverterData(
        str(conv.name),
        str(getattr(conv, 'converter_type', 'VSC')),
        convert_index_py_to_julia(from_idx),
        convert_index_py_to_julia(to_idx),
        float(getattr(conv, 'from_voltage_kv', 220.0)),
        float(getattr(conv, 'dc_voltage_kv', 320.0)),
        float(getattr(conv, 'rated_power_mva', 100.0)),
        float(getattr(conv, 'min_power_mva', 0.0)),
        float(getattr(conv, 'efficiency_rectify', 0.98)),
        float(getattr(conv, 'efficiency_invert', 0.98)),
        float(getattr(conv, 'standby_losses_mw', 0.5)),
        float(getattr(conv, 'reactive_power_min_mvar', -50.0)),
        float(getattr(conv, 'reactive_power_max_mvar', 50.0)),
        float(getattr(conv, 'power_factor', 1.0)),
        float(getattr(conv, 'impedance_pu', 0.05)),
        float(getattr(conv, 'resistance_pu', 0.01)),
        scale_cost(float(getattr(conv, 'invest_cost', 0.0))),
        scale_cost(float(getattr(conv, 'fixed_cost', 0.0))),
        scale_cost(float(getattr(conv, 'variable_cost', 0.0))),
        float(getattr(conv, 'invest_max_power', 0.0)),
        int(getattr(conv, 'life_time', 30)),
        int(getattr(conv, 'initial_age', 0)),
        float(getattr(conv, 'degradation_rate', 0.005)),
    )


def convert_freq_converter_data(conv) -> Any:
    """Convert a Python FrequencyConverterConfig to Julia FrequencyConverterData struct."""
    from esfex.bridge.julia_setup import get_esfex_module

    ESFEX = get_esfex_module()

    from_idx = getattr(conv, 'from_bus', None)
    if from_idx is None:
        from_idx = conv.from_node
    to_idx = getattr(conv, 'to_bus', None)
    if to_idx is None:
        to_idx = conv.to_node

    return ESFEX.FrequencyConverterData(
        str(conv.name),
        convert_index_py_to_julia(from_idx),
        convert_index_py_to_julia(to_idx),
        float(getattr(conv, 'from_frequency_hz', 50.0)),
        float(getattr(conv, 'to_frequency_hz', 60.0)),
        float(getattr(conv, 'rated_power_mva', 100.0)),
        float(getattr(conv, 'min_power_mva', 0.0)),
        float(getattr(conv, 'efficiency_a_to_b', 0.98)),
        float(getattr(conv, 'efficiency_b_to_a', 0.98)),
        float(getattr(conv, 'standby_losses_mw', 0.5)),
        float(getattr(conv, 'reactive_power_min_mvar', -50.0)),
        float(getattr(conv, 'reactive_power_max_mvar', 50.0)),
        float(getattr(conv, 'impedance_pu', 0.05)),
        float(getattr(conv, 'resistance_pu', 0.01)),
        scale_cost(float(getattr(conv, 'invest_cost', 0.0))),
        scale_cost(float(getattr(conv, 'fixed_cost', 0.0))),
        scale_cost(float(getattr(conv, 'variable_cost', 0.0))),
        float(getattr(conv, 'invest_max_power', 0.0)),
        int(getattr(conv, 'life_time', 30)),
        int(getattr(conv, 'initial_age', 0)),
        float(getattr(conv, 'degradation_rate', 0.005)),
    )


def convert_network_config(
    config: NodeConfig,
    dc_config: DCPowerFlowConfig,
    fuel_transport_distances: Optional[List[List[float]]] = None,
    transmission_lines_geo: Optional[List[TransmissionLineGeo]] = None,
    transformers: Optional[List[TransformerConfig]] = None,
    acdc_converters: Optional[list] = None,
    freq_converters: Optional[list] = None,
    buses: Optional[List[BusConfig]] = None,
    s_base_mva: float = 100.0,
) -> Any:
    """
    Convert Python NodeConfig to Julia NetworkConfig struct.

    Args:
        config: Python NodeConfig
        dc_config: Python DCPowerFlowConfig
        fuel_transport_distances: Distance matrix (km) as list of lists
        transmission_lines_geo: Per-line transmission data (optional, enables enhanced DC PF)
        transformers: Transformer definitions (optional)
        acdc_converters: AC/DC converter definitions (optional)
        freq_converters: Frequency converter definitions (optional)
        buses: Bus definitions (optional, auto-creates one bus per node if omitted)
        s_base_mva: System power base (MVA): ``ac_power_flow.base_mva``

    Returns:
        Julia NetworkConfig struct with bus support
    """
    from esfex.bridge.julia_setup import get_esfex_module, get_julia

    ESFEX = get_esfex_module()
    jl = get_julia()

    num_nodes = config.num_nodes

    # Build bus data (auto-create one bus per node if not provided)
    if not buses:
        buses = [
            BusConfig(bus_id=f"bus_{i}", parent_node=i, demand_fraction=1.0)
            for i in range(num_nodes)
        ]
    num_buses = len(buses)

    # Build Julia BusData vector and bus_to_node mapping
    jl_buses = jl.seval("BusData[]")
    bus_to_node = []
    for i, bus in enumerate(buses):
        jl_bus = ESFEX.BusData(
            i + 1,                          # bus_id (1-indexed)
            bus.parent_node + 1,            # parent_node (1-indexed)
            bus.voltage_kv,
            bus.frequency_hz,
            bus.current_type,
            bus.bus_type,
            bus.role,
            bus.demand_fraction,
        )
        jl.seval("push!")(jl_buses, jl_bus)
        bus_to_node.append(bus.parent_node + 1)  # 1-indexed

    # Convert flat connections list to node-level matrix
    node_connections = np.array(config.nodes_connections).reshape(num_nodes, num_nodes)

    # Build bus-level connections matrix.  When per-line data exists,
    # the matrix is the SUM of actual line capacities between bus
    # pairs — NOT the fully-meshed expansion of the node adjacency
    # matrix (which used to leak phantom inter-bus capacity wherever
    # two nodes were connected, regardless of which specific buses
    # had real lines). Falls back to the legacy node-expansion only
    # when no per-line data is available (very old configs).
    if transmission_lines_geo:
        connections = np.zeros((num_buses, num_buses))
        for line in transmission_lines_geo:
            fb, tb = line.from_bus, line.to_bus
            if fb is None or tb is None:
                continue
            if not (0 <= fb < num_buses and 0 <= tb < num_buses):
                continue
            cap = float(getattr(line, "capacity_mw", 0) or 0)
            connections[fb, tb] += cap
            connections[tb, fb] += cap
    elif num_buses == num_nodes:
        connections = node_connections
    else:
        # Legacy fallback: fully-mesh node-level connections to all
        # bus pairs sharing those nodes. Kept for backward compat
        # with very old YAMLs that only have nodes_connections.
        connections = np.zeros((num_buses, num_buses))
        for bi in range(num_buses):
            for bj in range(num_buses):
                ni = buses[bi].parent_node
                nj = buses[bj].parent_node
                if ni < num_nodes and nj < num_nodes:
                    connections[bi, bj] = node_connections[ni, nj]

    # Use actual distances if provided, otherwise zeros
    if fuel_transport_distances:
        node_distances = np.array(fuel_transport_distances)
        if node_distances.shape != (num_nodes, num_nodes):
            node_distances = node_distances.reshape(num_nodes, num_nodes)
    else:
        node_distances = np.zeros((num_nodes, num_nodes))

    # Build bus-level distances matrix
    if num_buses == num_nodes:
        distances = node_distances
    else:
        distances = np.zeros((num_buses, num_buses))
        for bi in range(num_buses):
            for bj in range(num_buses):
                ni = buses[bi].parent_node
                nj = buses[bj].parent_node
                if ni < num_nodes and nj < num_nodes:
                    distances[bi, bj] = node_distances[ni, nj]

    # Get transmission investment parameters (per-bus)
    node_invest_cost = config.transference_invest_cost or [1e6] * num_nodes
    node_invest_max = config.transference_invest_max or [0.0] * num_nodes
    if num_buses == num_nodes:
        trans_invest_cost = node_invest_cost
        trans_invest_max = node_invest_max
    else:
        trans_invest_cost = [
            node_invest_cost[min(bus.parent_node, len(node_invest_cost) - 1)]
            for bus in buses
        ]
        trans_invest_max = [
            node_invest_max[min(bus.parent_node, len(node_invest_max) - 1)]
            for bus in buses
        ]

    # Determine slack bus: prefer bus with bus_type=="slack"
    slack_bus_idx = convert_index_py_to_julia(dc_config.slack_bus)
    for i, bus in enumerate(buses):
        if bus.bus_type == "slack":
            slack_bus_idx = i + 1  # 1-indexed
            break

    # Convert per-line transmission data
    # Resolve bus indices from endpoint references when from_bus/to_bus are None.
    # In single-node multi-bus configs, from_node/to_node are both 0 (the only node),
    # creating self-loops unless we resolve to actual bus indices.
    jl_lines = jl.seval("TransmissionLineData[]")

    # Build bus_id → 0-based index mapping for endpoint resolution
    bus_id_to_idx = {bus.bus_id: i for i, bus in enumerate(buses)} if buses else {}

    # Build transformer → connected bus map from "wire" lines.
    # In the GUI, each transformer is a visual node connected to buses via
    # short transmission lines with from_endpoint_type='transformer'.
    # This map records which bus each transformer connects to on its
    # distribution side; the other side defaults to the first bus (backbone).
    trafo_to_bus: dict[str, int] = {}
    if transmission_lines_geo:
        for line in transmission_lines_geo:
            ft = getattr(line, 'from_endpoint_type', None)
            tt = getattr(line, 'to_endpoint_type', None)
            if ft == 'transformer':
                tid = getattr(line, 'from_endpoint_id', None)
                if tid is not None and tt == 'bus':
                    bid = getattr(line, 'to_endpoint_id', None)
                    if bid and bid in bus_id_to_idx:
                        trafo_to_bus[str(tid)] = bus_id_to_idx[bid]
            if tt == 'transformer':
                tid = getattr(line, 'to_endpoint_id', None)
                if tid is not None and ft == 'bus':
                    bid = getattr(line, 'from_endpoint_id', None)
                    if bid and bid in bus_id_to_idx:
                        trafo_to_bus[str(tid)] = bus_id_to_idx[bid]

    if transmission_lines_geo:
        for line in transmission_lines_geo:
            # Resolve from_bus from endpoint reference if not set
            if line.from_bus is None:
                ft = getattr(line, 'from_endpoint_type', None)
                if ft == 'bus':
                    ep_id = getattr(line, 'from_endpoint_id', None)
                    if ep_id and ep_id in bus_id_to_idx:
                        line = line.model_copy(update={'from_bus': bus_id_to_idx[ep_id]})
                elif ft == 'transformer':
                    # Resolve to the transformer's connected bus so this
                    # "wire" line becomes a self-loop (filtered in Julia).
                    tid = getattr(line, 'from_endpoint_id', None)
                    if tid is not None and str(tid) in trafo_to_bus:
                        line = line.model_copy(update={'from_bus': trafo_to_bus[str(tid)]})
            # Resolve to_bus from endpoint reference if not set
            if line.to_bus is None:
                tt = getattr(line, 'to_endpoint_type', None)
                if tt == 'bus':
                    ep_id = getattr(line, 'to_endpoint_id', None)
                    if ep_id and ep_id in bus_id_to_idx:
                        line = line.model_copy(update={'to_bus': bus_id_to_idx[ep_id]})
                elif tt == 'transformer':
                    tid = getattr(line, 'to_endpoint_id', None)
                    if tid is not None and str(tid) in trafo_to_bus:
                        line = line.model_copy(update={'to_bus': trafo_to_bus[str(tid)]})

            jl_line = convert_transmission_line_data(line, dc_config, distances, s_base_mva)
            if jl_line is None:
                continue  # unresolved endpoints
            jl.seval("push!")(jl_lines, jl_line)

    # Convert transformer data
    # Resolve bus endpoints: one side is the connected distribution bus (from
    # trafo_to_bus), the other is the first bus of the node (backbone bus).
    jl_transformers = jl.seval("TransformerData[]")
    if transformers:
        for i, trafo in enumerate(transformers):
            tid = str(i)
            # Resolve self-loop transformers: from_bus==to_bus means both sides
            # mapped to the same bus (typically from _node_to_bus fallback).
            # Use the wire-line map to find the actual distribution bus.
            if trafo.from_bus == trafo.to_bus and tid in trafo_to_bus:
                dist_bus = trafo_to_bus[tid]
                backbone_bus = trafo.from_bus if trafo.from_bus is not None else 0
                if backbone_bus == dist_bus:
                    backbone_bus = trafo.from_node  # fallback
                trafo = trafo.model_copy(update={
                    'from_bus': backbone_bus,
                    'to_bus': dist_bus,
                })
            jl_trafo = convert_transformer_data(trafo)
            jl.seval("push!")(jl_transformers, jl_trafo)

    # Convert AC/DC converter data
    jl_acdc = jl.seval("ACDCConverterData[]")
    if acdc_converters:
        for conv in acdc_converters:
            jl_conv = convert_acdc_converter_data(conv)
            jl.seval("push!")(jl_acdc, jl_conv)

    # Convert frequency converter data
    jl_freq = jl.seval("FrequencyConverterData[]")
    if freq_converters:
        for conv in freq_converters:
            jl_conv = convert_freq_converter_data(conv)
            jl.seval("push!")(jl_freq, jl_conv)

    # Use full constructor with bus data
    return ESFEX.NetworkConfig(
        num_nodes,
        num_buses,
        jl_buses,
        py_to_julia_int_vector(bus_to_node),
        py_to_julia_matrix(connections),
        py_to_julia_matrix(distances),
        float(s_base_mva),
        dc_config.reactance_per_km,
        dc_config.voltage_level_kv,
        np.deg2rad(dc_config.max_angle_diff_deg),
        slack_bus_idx,
        py_to_julia_vector(scale_cost_list(trans_invest_cost)),
        py_to_julia_vector(trans_invest_max),
        jl_lines,
        jl_transformers,
        jl_acdc,
        jl_freq,
        getattr(dc_config, 'default_r_to_x_ratio', 0.1),
    )


def absolute_start_hour(year, base_year, hour_in_year) -> int:
    """Horizon hour of ``hour_in_year`` in ``year`` (hour 0 = Jan 1 of ``base_year``).

    Every modelled year is 8760 h long.
    """
    years = int(year or 0) - int(base_year if base_year is not None else (year or 0))
    return max(0, years) * HOURS_STD_YEAR + int(hour_in_year or 0)


def outage_window_spans(window, lo: int, hi: int) -> list:
    """Absolute ``[start, end)`` hour spans of one outage window overlapping ``[lo, hi)``.

    ``window`` is ``(start_hour, end_hour, availability[, recurring])`` in
    absolute horizon hours (hour 0 = Jan 1 of ``base_year``; every modelled
    year is 8760 h). A one-off window happens once. A recurring window repeats
    every year at the same hours of the year (its start is taken modulo 8760),
    including a window that runs over the new year.
    """
    w0, w1 = int(window[0]), int(window[1])
    if len(window) < 4 or not window[3]:
        return [(w0, w1)] if w1 > lo and w0 < hi else []
    length = w1 - w0
    s = w0 % HOURS_STD_YEAR
    first = (lo - length - s) // HOURS_STD_YEAR
    last = (hi - s) // HOURS_STD_YEAR
    spans = []
    for k in range(first, last + 1):
        a = s + k * HOURS_STD_YEAR
        b = a + length
        if b > lo and a < hi:
            spans.append((a, b))
    return spans


def outage_window_tuple(ow) -> tuple:
    """``(start_hour, end_hour, availability, recurring)`` of an ``OutageWindow``."""
    return (ow.start_hour, ow.end_hour, ow.availability,
            bool(getattr(ow, "recurring", False)))


def compile_outage_factor(
    windows: list,
    start_hour: int,
    hours: int,
    resolution_hours: int = 1,
) -> np.ndarray:
    """Per-timestep availability factor in [0, 1] from scheduled outage windows.

    ``windows`` is a list of ``(start_hour, end_hour, availability[, recurring])``
    tuples in absolute hour-of-horizon coordinates (``[start, end)``, hour 0 =
    Jan 1 of ``base_year``; see :func:`outage_window_spans`). ``start_hour`` is
    the absolute horizon hour at which the window starts. The result has
    length ``hours`` — the current rolling-horizon window at
    ``resolution_hours`` resolution — where local period ``i`` covers absolute
    hours ``[(start_hour // res + i) * res, ...)``.

    A period fully inside a window takes that window's ``availability``; partial
    overlap is weighted exactly like the fuel-supply disruption model
    (``primary_energy.jl``): ``factor = (1 - frac) + frac * availability``.
    Multiple overlapping windows compound multiplicatively.
    """
    factor = np.ones(hours, dtype=float)
    if not windows:
        return factor
    res = max(1, int(resolution_hours))
    base_period = start_hour // res
    lo = base_period * res
    hi = lo + hours * res
    for w in windows:
        wav = float(w[2])
        for w0, w1 in outage_window_spans(w, lo, hi):
            # Only the periods the span touches
            i0 = max(0, (w0 - lo) // res)
            i1 = min(hours, -(-(w1 - lo) // res))
            for i in range(i0, i1):
                p_start = lo + i * res
                frac = (min(p_start + res, w1) - max(p_start, w0)) / res
                if frac > 0:
                    factor[i] *= (1.0 - frac) + frac * wav
    return factor


def unit_outage_factors(
    outage_schedule,
    element_type: str,
    unit_keys: list,
    start_hour: int,
    hours: int,
    resolution_hours: int = 1,
    by_node: bool = True,
) -> dict:
    """Scheduled outages of a list of units as availability factors per step.

    The master's form of the dispatch's outage masks (Julia
    ``gen_outage_factors`` / ``bat_outage_factors``): ``{(unit, node):
    factor}`` with ``unit`` the 1-based position of the window's
    ``element_id`` in ``unit_keys``, ``node`` the 1-based node of a
    node-specific window (0 for a window on the whole unit, and for every
    window when ``by_node`` is False: battery outages apply to the whole
    unit, as in the dispatch) and ``factor`` the :func:`compile_outage_factor`
    of those windows over ``hours`` steps of ``resolution_hours`` from
    horizon hour ``start_hour`` (recurring windows repeat every year).
    Entries that stay 1 over the span are left out.
    """
    index = {k: i + 1 for i, k in enumerate(unit_keys)}
    windows: dict = {}
    for ow in outage_schedule or []:
        if ow.element_type != element_type or ow.element_id not in index:
            continue
        node = getattr(ow, "node", None)
        key = (index[ow.element_id],
               int(node) + 1 if by_node and node is not None else 0)
        windows.setdefault(key, []).append(outage_window_tuple(ow))
    out = {}
    for key, ws in windows.items():
        factor = compile_outage_factor(ws, start_hour, hours, resolution_hours)
        if np.any(factor < 1.0):
            out[key] = factor
    return out


def fuel_source_outage_windows(outage_schedule, fuel_name: str) -> list:
    """Scheduled outage windows for a fuel source, as ``(start, end, avail, recurring)``.

    Fuel-source outages reuse ``FuelConfig``'s built-in single-window disruption
    in the primary-energy model (no Julia change), so the caller applies one
    window per model window and warns if several overlap it.
    """
    return [
        outage_window_tuple(ow)
        for ow in (outage_schedule or [])
        if ow.element_type == "fuel_source" and ow.element_id == fuel_name
    ]


def build_outage_mask_matrix(
    windows_by_id: dict,
    id_order: list,
    start_hour: int,
    hours: int,
    resolution_hours: int = 1,
) -> "np.ndarray | None":
    """Dense ``[len(id_order) × hours]`` capacity mask for a set of elements.

    Row ``i`` is the per-timestep availability factor for element ``id_order[i]``
    (see :func:`compile_outage_factor`), defaulting to all-ones for elements with
    no scheduled outage. ``windows_by_id`` maps an element id to its list of
    ``(start_hour, end_hour, availability[, recurring])`` windows.

    Returns ``None`` when no element in ``id_order`` has an outage, so callers
    can skip populating the mask entirely (keeping the model identical).
    """
    if not windows_by_id or not any(eid in windows_by_id for eid in id_order):
        return None
    mask = np.ones((len(id_order), hours), dtype=float)
    for i, eid in enumerate(id_order):
        w = windows_by_id.get(eid)
        if w:
            mask[i, :] = compile_outage_factor(w, start_hour, hours, resolution_hours)
    return mask


def build_outage_mask_by_id(
    windows_by_id: dict,
    start_hour: int,
    hours: int,
    resolution_hours: int = 1,
) -> "dict | None":
    """Id-addressed masks for multi-stage-filtered elements (lines, converters…).

    Returns ``{element_id: factor_array}`` (length-``hours`` factor per id that
    has an outage), or ``None`` when nothing is scheduled. Unlike
    :func:`build_outage_mask_matrix`, this keys by stable element id rather than
    position, so it is safe when the Julia model filters/reorders elements — the
    constraint looks each element up by the id it carries.
    """
    if not windows_by_id:
        return None
    out = {
        eid: compile_outage_factor(w, start_hour, hours, resolution_hours)
        for eid, w in windows_by_id.items() if w
    }
    return out or None


def convert_generator_config(
    gen: GeneratorConfig,
    availability: Optional[np.ndarray] = None,
    inflow: Optional[np.ndarray] = None,
    bus_to_node: Optional[list] = None,
    bus_per_node: Optional[dict] = None,
    replicate_capacity_across_node: bool = False,
) -> Any:
    """Convert Python GeneratorConfig to Julia GeneratorConfig struct.

    Per-node arrays are expanded to per-bus arrays when ``bus_to_node`` is
    provided.  Capacity placement rules:

    - **Existing generators** with a physical bus per node (resolved by
      :func:`_resolve_element_bus_mapping`): pass ``bus_per_node`` —
      capacity for each active node lands on its physical bus.
    - **Virtual generators** (master investments without a fixed
      location): pass ``replicate_capacity_across_node=True`` —
      capacity is mirrored to every bus of each active node so the
      operational LP can choose where to dispatch.  A node-level
      ``rated_power`` cap is enforced in the Julia model to keep total
      dispatch within the master's per-node decision.

    When neither is supplied, per-node capacity falls back to the node's
    first (canonical) bus.
    """
    from esfex.bridge.julia_setup import get_esfex_module

    ESFEX = get_esfex_module()

    num_nodes = len(gen.rated_power)
    hours = availability.shape[0] if availability is not None else HOURS_STD_YEAR

    # Use availability if provided, otherwise default to 1.0
    if availability is None:
        availability = np.ones((HOURS_STD_YEAR, num_nodes))

    # Convert min_up and min_down to float arrays (Julia expects Float64)
    min_up_float = [float(x) for x in gen.min_up]
    min_down_float = [float(x) for x in gen.min_down]
    life_time_float = [float(x) for x in gen.life_time]
    initial_age_float = [float(x) for x in gen.initial_age]
    decommissioning_cost = getattr(gen, 'decommissioning_cost', None)
    if decommissioning_cost is None:
        decommissioning_cost = [0.0] * num_nodes

    frequency_hz = float(getattr(gen, 'frequency_hz', 50.0))
    current_type = str(getattr(gen, 'current_type', 'AC'))
    # Units per node for clustered unit commitment (a single value is
    # broadcast; Julia counts fewer than 1 as 1)
    num_units_node = [int(n) for n in (getattr(gen, 'num_units', None) or [1])]
    if len(num_units_node) == 1:
        num_units_node = num_units_node * num_nodes
    num_units_node += [1] * (num_nodes - len(num_units_node))

    # Reservoir fields — fill with zeros if empty
    res_capacity = gen.reservoir_capacity if gen.reservoir_capacity else [0.0] * num_nodes
    res_initial = gen.reservoir_initial_level if gen.reservoir_initial_level else [0.0] * num_nodes
    res_min = gen.reservoir_min_level if gen.reservoir_min_level else [0.0] * num_nodes
    res_max = gen.reservoir_max_level if gen.reservoir_max_level else [1.0] * num_nodes
    res_turb_eff = gen.reservoir_turbine_efficiency if gen.reservoir_turbine_efficiency else [0.9] * num_nodes
    res_evap = gen.reservoir_evaporation_rate if gen.reservoir_evaporation_rate else [0.0] * num_nodes
    res_pump_cap = gen.reservoir_pump_capacity if gen.reservoir_pump_capacity else [0.0] * num_nodes
    res_pump_eff = gen.reservoir_pump_efficiency if gen.reservoir_pump_efficiency else [0.85] * num_nodes
    res_inv_cost = gen.reservoir_invest_cost if gen.reservoir_invest_cost else [0.0] * num_nodes
    res_inv_max = gen.reservoir_invest_max if gen.reservoir_invest_max else [0.0] * num_nodes

    # Inflow matrix (hours x nodes)
    if inflow is None:
        inflow = np.zeros((hours, num_nodes))

    # --- Expand per-node → per-bus when multi-bus ---
    if bus_to_node is not None:
        prop = lambda arr: expand_node_to_bus_array(arr, bus_to_node, "property")

        # Capacity placement modes:
        # - replicate_capacity_across_node: virtual gens — mirror to all buses
        #   of each active node (node-level cap enforced in Julia model)
        # - bus_per_node provided: existing gens — physical bus per active node
        # - else: fall back to the node's first (canonical) bus
        cap = lambda arr: expand_node_to_bus_array(
            arr, bus_to_node, "capacity",
            bus_per_node=bus_per_node,
            replicate_to_all_buses_in_node=replicate_capacity_across_node,
        )

        # Capacity fields: physical unit at one bus
        rated_power = cap(gen.rated_power)
        min_power = cap(gen.min_power)
        start_up_cost = cap(gen.start_up_cost)
        inertia_arr = cap(gen.inertia)
        decomm_cost = cap(decommissioning_cost)
        r_capacity = cap(res_capacity)
        r_initial = cap(res_initial)
        r_pump_cap = cap(res_pump_cap)

        # Property fields: same value at all buses in node
        eff_rated = prop(gen.eff_at_rated)
        eff_min = prop(gen.eff_at_min)
        ramp_up = prop(gen.ramp_up)
        ramp_down = prop(gen.ramp_down)
        min_up = prop(min_up_float)
        min_down = prop(min_down_float)
        num_units = [int(n) for n in prop(num_units_node)]
        fuel_cost = prop(gen.fuel_cost)
        fixed_cost = prop(gen.fixed_cost)
        maint_cost = prop(gen.maintenance_cost)
        invest_cost = prop(gen.invest_cost)
        life_time = prop(life_time_float)
        initial_age = prop(initial_age_float)
        degradation = prop(gen.degradation_rate)
        r_min = prop(res_min)
        r_max = prop(res_max)
        r_turb_eff = prop(res_turb_eff)
        r_evap = prop(res_evap)
        r_pump_eff = prop(res_pump_eff)
        r_inv_cost = prop(res_inv_cost)
        r_inv_max = prop(res_inv_max)

        # Matrices: property expansion (all buses see same profile)
        availability = expand_node_to_bus_matrix(availability, bus_to_node, default_value=1.0)
        inflow = expand_node_to_bus_matrix(inflow, bus_to_node, default_value=0.0)
    else:
        rated_power = gen.rated_power
        min_power = gen.min_power
        start_up_cost = gen.start_up_cost
        inertia_arr = gen.inertia
        decomm_cost = decommissioning_cost
        r_capacity = res_capacity
        r_initial = res_initial
        r_pump_cap = res_pump_cap
        eff_rated = gen.eff_at_rated
        eff_min = gen.eff_at_min
        ramp_up = gen.ramp_up
        ramp_down = gen.ramp_down
        min_up = min_up_float
        min_down = min_down_float
        num_units = num_units_node
        fuel_cost = gen.fuel_cost
        fixed_cost = gen.fixed_cost
        maint_cost = gen.maintenance_cost
        invest_cost = gen.invest_cost
        life_time = life_time_float
        initial_age = initial_age_float
        degradation = gen.degradation_rate
        r_min = res_min
        r_max = res_max
        r_turb_eff = res_turb_eff
        r_evap = res_evap
        r_pump_eff = res_pump_eff
        r_inv_cost = res_inv_cost
        r_inv_max = res_inv_max

    # --- Cost scaling: $ → M$ ---
    fuel_cost = scale_cost_list(fuel_cost)
    fixed_cost = scale_cost_list(fixed_cost)
    maint_cost = scale_cost_list(maint_cost)
    invest_cost = scale_cost_list(invest_cost)
    start_up_cost = scale_cost_list(start_up_cost)
    decomm_cost = scale_cost_list(decomm_cost)
    r_inv_cost = scale_cost_list(r_inv_cost)

    return ESFEX.GeneratorConfig(
        gen.name,
        gen.type,
        gen.fuel,
        py_to_julia_vector(rated_power),
        py_to_julia_vector(min_power),
        py_to_julia_vector(eff_rated),
        py_to_julia_vector(eff_min),
        py_to_julia_vector(ramp_up),
        py_to_julia_vector(ramp_down),
        py_to_julia_vector(min_up),
        py_to_julia_vector(min_down),
        py_to_julia_vector(start_up_cost),
        py_to_julia_vector(fuel_cost),
        py_to_julia_vector(fixed_cost),
        py_to_julia_vector(maint_cost),
        py_to_julia_vector(inertia_arr),
        py_to_julia_vector(invest_cost),
        py_to_julia_matrix(availability),
        gen.reservable,
        py_to_julia_vector(life_time),
        py_to_julia_vector(initial_age),
        py_to_julia_vector(degradation),
        py_to_julia_vector(decomm_cost),
        frequency_hz,
        current_type,
        # Reservoir fields
        py_to_julia_vector(r_capacity),
        py_to_julia_vector(r_initial),
        py_to_julia_vector(r_min),
        py_to_julia_vector(r_max),
        py_to_julia_matrix(inflow),
        py_to_julia_vector(r_turb_eff),
        py_to_julia_vector(r_evap),
        py_to_julia_vector(r_pump_cap),
        py_to_julia_vector(r_pump_eff),
        gen.reservoir_spillage_allowed,
        py_to_julia_vector(r_inv_cost),
        py_to_julia_vector(r_inv_max),
        py_to_julia_vector(
            prop(getattr(gen, 'risk_coefficient', [1.0]))
            if bus_to_node else getattr(gen, 'risk_coefficient', [1.0])
        ),
        py_to_julia_vector(
            prop(getattr(gen, 'reservoir_min_release', None) or [0.0])
            if bus_to_node
            else (getattr(gen, 'reservoir_min_release', None) or [0.0])
        ),
        # Hydraulic cascade (plant-level scalars): downstream reservoir name and
        # water travel delay. Empty name = terminal reservoir.
        getattr(gen, 'cascade_downstream', '') or '',
        int(getattr(gen, 'cascade_delay_hours', 0) or 0),
        # Head dependence: per-node power-availability factor at minimum level
        # (1.0 = no head effect).
        py_to_julia_vector(
            prop(getattr(gen, 'reservoir_head_min_factor', None) or [1.0])
            if bus_to_node
            else (getattr(gen, 'reservoir_head_min_factor', None) or [1.0])
        ),
        py_to_julia_int_vector(num_units),
    )


def _storage_availability_matrix(availability: Optional[np.ndarray], num_nodes: int,
                                 bus_to_node: Optional[list]) -> np.ndarray:
    """Availability of a battery or battery technology for Julia: the
    ``[steps × nodes]`` profile laid out on the buses, or a 0-row matrix
    (fully available) when there is none."""
    if availability is None:
        n = len(bus_to_node) if bus_to_node is not None else num_nodes
        return np.zeros((0, n))
    availability = np.asarray(availability, dtype=float)
    if bus_to_node is not None:
        availability = expand_node_to_bus_matrix(availability, bus_to_node, default_value=1.0)
    return availability


def convert_battery_config(
    bat: BatteryConfig,
    bus_to_node: Optional[list] = None,
    bus_per_node: Optional[dict] = None,
    replicate_capacity_across_node: bool = False,
    availability: Optional[np.ndarray] = None,
) -> Any:
    """Convert Python BatteryConfig to Julia BatteryConfig struct.

    ``availability`` is the battery's profile ``[steps × nodes]`` (factor on
    charge and discharge power), None when it has none.

    Capacity placement mirrors :func:`convert_generator_config`:

    - **Existing batteries**: pass ``bus_per_node`` to anchor each
      active node's capacity to its real physical bus.
    - **Virtual batteries** (master investments): pass
      ``replicate_capacity_across_node=True`` to mirror capacity to all
      buses of each active node; a node-level cap in the Julia model
      enforces the master's per-node investment decision.
    """
    from esfex.bridge.julia_setup import get_esfex_module

    ESFEX = get_esfex_module()

    num_nodes = len(bat.capacity)

    # Convert life_time and initial_age to float arrays (Julia expects Float64)
    life_time_float = [float(x) for x in bat.life_time]
    initial_age_float = [float(x) for x in getattr(bat, 'initial_age', [0] * num_nodes)]
    decommissioning_cost = getattr(bat, 'decommissioning_cost', None)
    if decommissioning_cost is None:
        decommissioning_cost = [0.0] * num_nodes

    # Duration constraints (default to no constraint if not set)
    min_duration = float(getattr(bat, 'min_duration_hours', None) or 0.0)
    max_duration = float(getattr(bat, 'max_duration_hours', None) or float('inf'))

    # New fields for parity with Python legacy
    # maintenance_cost: default to 0 if not set
    maintenance_cost = getattr(bat, 'maintenance_cost', None)
    if maintenance_cost is None:
        maintenance_cost = [0.0] * num_nodes

    # inertia: default to 0 if not set (synthetic inertia contribution)
    inertia_arr = getattr(bat, 'inertia', None)
    if inertia_arr is None:
        inertia_arr = [0.0] * num_nodes

    # spillage: whether spillage is allowed (default False)
    spillage = bool(getattr(bat, 'spillage', False))

    current_type = str(getattr(bat, 'current_type', 'DC'))

    # Age-based capacity degradation rate per node
    degradation_rate = getattr(bat, 'degradation_rate', None)
    if degradation_rate is None:
        degradation_rate = [0.0] * num_nodes

    # Throughput degradation cost ($/MWh discharged)
    throughput_degradation_cost = getattr(bat, 'throughput_degradation_cost', None)
    if throughput_degradation_cost is None:
        throughput_degradation_cost = [0.0] * num_nodes

    soc_min = [1 - dod for dod in bat.max_DoD]

    # --- Expand per-node → per-bus when multi-bus ---
    if bus_to_node is not None:
        num_buses = len(bus_to_node)
        prop = lambda arr: expand_node_to_bus_array(arr, bus_to_node, "property")

        cap = lambda arr: expand_node_to_bus_array(
            arr, bus_to_node, "capacity",
            bus_per_node=bus_per_node,
            replicate_to_all_buses_in_node=replicate_capacity_across_node,
        )

        # Capacity fields: physical unit at one bus
        capacity = cap(bat.capacity)
        max_charge = cap(bat.MaxChargePower)
        max_discharge = cap(bat.MaxDischargePower)
        soc_initial = cap(bat.soc_initial)
        inertia_out = cap(inertia_arr)
        decomm_cost = cap(decommissioning_cost)

        # Property fields: same value at all buses in node
        eff_charge = prop(bat.efficiency_charge)
        eff_discharge = prop(bat.efficiency_discharge)
        soc_min_out = prop(soc_min)
        soc_max_out = prop([1.0] * num_nodes)
        self_discharge = prop([0.0001] * num_nodes)
        inv_cost = prop(bat.invest_cost)
        # invest_max_power only places the battery at a bus in the master:
        # "capacity" mode (first bus of the node only)
        inv_max_power = cap(bat.invest_max_power)
        life_time = prop(life_time_float)
        initial_age = prop(initial_age_float)
        maint_cost = prop(maintenance_cost)
        degrad = prop(degradation_rate)
        tp_degrad_cost = prop(throughput_degradation_cost)
        ramp_up = prop(_per_node(bat.ramp_up, num_nodes))
        ramp_down = prop(_per_node(bat.ramp_down, num_nodes))
        fixed_cost = prop(_per_node(bat.fixed_cost, num_nodes))
    else:
        num_buses = num_nodes
        capacity = bat.capacity
        max_charge = bat.MaxChargePower
        max_discharge = bat.MaxDischargePower
        soc_initial = bat.soc_initial
        inertia_out = inertia_arr
        decomm_cost = decommissioning_cost
        eff_charge = bat.efficiency_charge
        eff_discharge = bat.efficiency_discharge
        soc_min_out = soc_min
        soc_max_out = [1.0] * num_nodes
        self_discharge = [0.0001] * num_nodes
        inv_cost = bat.invest_cost
        inv_max_power = bat.invest_max_power
        life_time = life_time_float
        initial_age = initial_age_float
        maint_cost = maintenance_cost
        degrad = degradation_rate
        tp_degrad_cost = throughput_degradation_cost
        ramp_up = _per_node(bat.ramp_up, num_nodes)
        ramp_down = _per_node(bat.ramp_down, num_nodes)
        fixed_cost = _per_node(bat.fixed_cost, num_nodes)

    # --- Cost scaling: $ → M$ ---
    inv_cost = scale_cost_list(inv_cost)
    maint_cost = scale_cost_list(maint_cost)
    decomm_cost = scale_cost_list(decomm_cost)
    tp_degrad_cost = scale_cost_list(tp_degrad_cost)
    fixed_cost = scale_cost_list(fixed_cost)

    return ESFEX.BatteryConfig(
        bat.name,
        py_to_julia_vector(capacity),
        py_to_julia_vector(max_charge),
        py_to_julia_vector(max_discharge),
        py_to_julia_vector(eff_charge),
        py_to_julia_vector(eff_discharge),
        py_to_julia_vector(soc_min_out),
        py_to_julia_vector(soc_max_out),
        py_to_julia_vector(soc_initial),
        py_to_julia_vector(self_discharge),
        py_to_julia_vector(inv_cost),
        py_to_julia_vector(inv_max_power),
        py_to_julia_vector(life_time),
        py_to_julia_vector(initial_age),
        py_to_julia_vector(decomm_cost),
        min_duration,
        max_duration,
        py_to_julia_vector(maint_cost),
        py_to_julia_vector(inertia_out),
        spillage,
        current_type,
        py_to_julia_vector(degrad),
        py_to_julia_vector(tp_degrad_cost),
        py_to_julia_vector(
            prop(getattr(bat, 'risk_coefficient', [1.0]))
            if bus_to_node else getattr(bat, 'risk_coefficient', [1.0])
        ),
        py_to_julia_vector(ramp_up),
        py_to_julia_vector(ramp_down),
        py_to_julia_vector(fixed_cost),
        bool(bat.reservable),
        py_to_julia_matrix(_storage_availability_matrix(availability, num_nodes, bus_to_node)),
    )


def convert_technology_config(
    tech: TechnologyConfig,
    availability: Optional[np.ndarray] = None,
    bus_to_node: Optional[list] = None,
) -> Any:
    """
    Convert Python TechnologyConfig to Julia TechnologyConfig struct.

    Args:
        tech: Python TechnologyConfig
        availability: Optional availability matrix (hours x nodes)
        bus_to_node: Optional bus-to-node mapping (0-indexed).

    Returns:
        Julia TechnologyConfig struct
    """
    from esfex.bridge.julia_setup import get_esfex_module

    ESFEX = get_esfex_module()

    num_nodes = len(tech.invest_cost)
    hours = availability.shape[0] if availability is not None else HOURS_STD_YEAR

    if availability is None:
        availability = np.ones((HOURS_STD_YEAR, num_nodes))

    min_up_float = [float(x) for x in tech.min_up]
    min_down_float = [float(x) for x in tech.min_down]
    life_time_float = [float(tech.lifetime)] * num_nodes
    decommissioning_cost = list(tech.decommissioning_cost)

    if bus_to_node is not None:
        cap = lambda arr: expand_node_to_bus_array(arr, bus_to_node, "capacity")
        prop = lambda arr: expand_node_to_bus_array(arr, bus_to_node, "property")

        invest_cost = prop(tech.invest_cost)
        # invest_max uses "property" mode so the master optimizer can invest
        # at ANY bus within the node.  The per-node aggregate constraint in
        # master_problem.jl (add_investment_constraints!) caps the total.
        invest_max = prop(tech.invest_max_power)
        eff_rated = prop(tech.eff_at_rated)
        eff_min = prop(tech.eff_at_min)
        ramp_up = prop(tech.ramp_up)
        ramp_down = prop(tech.ramp_down)
        min_up = prop(min_up_float)
        min_down = prop(min_down_float)
        min_power = prop(tech.min_output)
        fuel_cost = prop(tech.fuel_cost)
        fixed_cost = prop(tech.fixed_cost)
        maint_cost = prop(tech.maintenance_cost)
        start_up_cost = prop(tech.start_up_cost)
        inertia_arr = prop(tech.inertia)
        life_time = prop(life_time_float)
        degradation = prop(tech.degradation_rate)
        decomm_cost = prop(decommissioning_cost)
        availability = expand_node_to_bus_matrix(availability, bus_to_node, default_value=1.0)
    else:
        invest_cost = tech.invest_cost
        invest_max = tech.invest_max_power
        eff_rated = tech.eff_at_rated
        eff_min = tech.eff_at_min
        ramp_up = tech.ramp_up
        ramp_down = tech.ramp_down
        min_up = min_up_float
        min_down = min_down_float
        min_power = tech.min_output
        fuel_cost = tech.fuel_cost
        fixed_cost = tech.fixed_cost
        maint_cost = tech.maintenance_cost
        start_up_cost = tech.start_up_cost
        inertia_arr = tech.inertia
        life_time = life_time_float
        degradation = tech.degradation_rate
        decomm_cost = decommissioning_cost

    # --- Cost scaling: $ → M$ ---
    invest_cost = scale_cost_list(invest_cost)
    fuel_cost = scale_cost_list(fuel_cost)
    fixed_cost = scale_cost_list(fixed_cost)
    maint_cost = scale_cost_list(maint_cost)
    start_up_cost = scale_cost_list(start_up_cost)
    decomm_cost = scale_cost_list(decomm_cost)

    return ESFEX.TechnologyConfig(
        tech.name,
        tech.type,
        tech.fuel,
        py_to_julia_vector(invest_cost),
        py_to_julia_vector(invest_max),
        py_to_julia_matrix(availability),
        py_to_julia_vector(eff_rated),
        py_to_julia_vector(eff_min),
        py_to_julia_vector(ramp_up),
        py_to_julia_vector(ramp_down),
        py_to_julia_vector(min_up),
        py_to_julia_vector(min_down),
        py_to_julia_vector(min_power),
        py_to_julia_vector(fuel_cost),
        py_to_julia_vector(fixed_cost),
        py_to_julia_vector(maint_cost),
        py_to_julia_vector(start_up_cost),
        py_to_julia_vector(inertia_arr),
        py_to_julia_vector(life_time),
        py_to_julia_vector(degradation),
        py_to_julia_vector(decomm_cost),
        float(tech.frequency_hz),
        str(tech.current_type),
        tech.reservable,
        py_to_julia_vector(
            prop(getattr(tech, 'risk_coefficient', [1.0]))
            if bus_to_node else getattr(tech, 'risk_coefficient', [1.0])
        ),
    )


def convert_battery_technology_config(
    bat_tech: BatteryTechnologyConfig,
    bus_to_node: Optional[list] = None,
    availability: Optional[np.ndarray] = None,
) -> Any:
    """
    Convert Python BatteryTechnologyConfig to Julia BatteryTechnologyConfig struct.

    Args:
        bat_tech: Python BatteryTechnologyConfig
        bus_to_node: Optional bus-to-node mapping (0-indexed).
        availability: The technology's profile ``[steps × nodes]``, or None.

    Returns:
        Julia BatteryTechnologyConfig struct
    """
    from esfex.bridge.julia_setup import get_esfex_module

    ESFEX = get_esfex_module()

    num_nodes = len(bat_tech.invest_cost_power)
    life_time_float = [float(bat_tech.lifetime)] * num_nodes
    soc_min = [1.0 - dod for dod in bat_tech.max_DoD]

    if bus_to_node is not None:
        cap = lambda arr: expand_node_to_bus_array(arr, bus_to_node, "capacity")
        prop = lambda arr: expand_node_to_bus_array(arr, bus_to_node, "property")

        inv_cost_power = prop(bat_tech.invest_cost_power)
        inv_cost_capacity = prop(bat_tech.invest_cost_energy)
        # invest_max uses "property" mode so the master optimizer can invest
        # at ANY bus within the node.  Per-node aggregate in master_problem.jl.
        inv_max_power = prop(bat_tech.invest_max_power)
        inv_max_capacity = prop(bat_tech.invest_max_capacity)
        eff_charge = prop(bat_tech.efficiency_charge)
        eff_discharge = prop(bat_tech.efficiency_discharge)
        degradation = prop(bat_tech.degradation_rate)
        life_time = prop(life_time_float)
        soc_initial = prop(bat_tech.soc_initial)
        soc_min_out = prop(soc_min)
        soc_max_out = prop([1.0] * num_nodes)
        maint_cost = prop(bat_tech.maintenance_cost)
        inertia_arr = prop(bat_tech.inertia)
        tp_degrad_cost = prop(bat_tech.throughput_degradation_cost)
        decomm_cost = prop(bat_tech.decommissioning_cost)
        ramp_up = prop(_per_node(bat_tech.ramp_up, num_nodes))
        ramp_down = prop(_per_node(bat_tech.ramp_down, num_nodes))
        fixed_cost = prop(_per_node(bat_tech.fixed_cost, num_nodes))
    else:
        inv_cost_power = bat_tech.invest_cost_power
        inv_cost_capacity = bat_tech.invest_cost_energy
        inv_max_power = bat_tech.invest_max_power
        inv_max_capacity = bat_tech.invest_max_capacity
        eff_charge = bat_tech.efficiency_charge
        eff_discharge = bat_tech.efficiency_discharge
        degradation = bat_tech.degradation_rate
        life_time = life_time_float
        soc_initial = bat_tech.soc_initial
        soc_min_out = soc_min
        soc_max_out = [1.0] * num_nodes
        maint_cost = bat_tech.maintenance_cost
        inertia_arr = bat_tech.inertia
        tp_degrad_cost = bat_tech.throughput_degradation_cost
        decomm_cost = bat_tech.decommissioning_cost
        ramp_up = _per_node(bat_tech.ramp_up, num_nodes)
        ramp_down = _per_node(bat_tech.ramp_down, num_nodes)
        fixed_cost = _per_node(bat_tech.fixed_cost, num_nodes)

    # --- Cost scaling: $ → M$ ---
    inv_cost_power = scale_cost_list(inv_cost_power)
    inv_cost_capacity = scale_cost_list(inv_cost_capacity)
    maint_cost = scale_cost_list(maint_cost)
    tp_degrad_cost = scale_cost_list(tp_degrad_cost)
    decomm_cost = scale_cost_list(decomm_cost)
    fixed_cost = scale_cost_list(fixed_cost)

    return ESFEX.BatteryTechnologyConfig(
        bat_tech.name,
        py_to_julia_vector(inv_cost_power),
        py_to_julia_vector(inv_cost_capacity),
        py_to_julia_vector(inv_max_power),
        py_to_julia_vector(inv_max_capacity),
        float(bat_tech.min_duration_hours),
        float(bat_tech.max_duration_hours),
        py_to_julia_vector(eff_charge),
        py_to_julia_vector(eff_discharge),
        py_to_julia_vector(degradation),
        py_to_julia_vector(life_time),
        py_to_julia_vector(soc_initial),
        py_to_julia_vector(soc_min_out),
        py_to_julia_vector(soc_max_out),
        py_to_julia_vector(maint_cost),
        py_to_julia_vector(inertia_arr),
        py_to_julia_vector(tp_degrad_cost),
        bat_tech.spillage,
        str(bat_tech.current_type),
        py_to_julia_vector(decomm_cost),
        py_to_julia_vector(
            prop(getattr(bat_tech, 'risk_coefficient', [1.0]))
            if bus_to_node else getattr(bat_tech, 'risk_coefficient', [1.0])
        ),
        py_to_julia_vector(ramp_up),
        py_to_julia_vector(ramp_down),
        py_to_julia_vector(fixed_cost),
        bool(bat_tech.reservable),
        py_to_julia_matrix(_storage_availability_matrix(availability, num_nodes, bus_to_node)),
    )


def convert_temporal_config(temporal: TemporalConfig, hours: int) -> Any:
    """
    Convert Python TemporalConfig to Julia TemporalConfig struct.

    Args:
        temporal: Python TemporalConfig
        hours: Total hours in simulation

    Returns:
        Julia TemporalConfig struct
    """
    from esfex.bridge.julia_setup import get_esfex_module

    ESFEX = get_esfex_module()

    # Get upscaling resolutions with defaults matching Python legacy
    battery_soc_resolution = getattr(temporal, 'battery_soc_resolution', 6)
    ev_resolution = getattr(temporal, 'ev_resolution', 6)
    reserve_resolution = getattr(temporal, 'reserve_resolution', 4)

    return ESFEX.TemporalConfig(
        hours,
        temporal.resolution_hours,
        temporal.rolling_horizon_hours,
        temporal.overlap_hours,
        temporal.investment_resolution,
        temporal.primary_energy_resolution,
        battery_soc_resolution,
        ev_resolution,
        reserve_resolution,
    )


def convert_power_system_result(jl_result: Any) -> dict:
    """
    Convert a Julia PowerSystemResult to a Python dictionary.

    Args:
        jl_result: Julia PowerSystemResult struct

    Returns:
        Python dictionary with solution values
    """
    result = {
        "status": str(jl_result.status),
        "objective": float(jl_result.objective) * COST_UNSCALE,
        "solve_time": float(jl_result.solve_time),
        # Generation
        "gen_output": julia_to_py_array(jl_result.gen_output),
        "curtailment": julia_to_py_array(jl_result.curtailment),
        "total_curtailment": float(jl_result.total_curtailment),
        # Storage
        "bat_charge": julia_to_py_array(jl_result.bat_charge),
        "bat_discharge": julia_to_py_array(jl_result.bat_discharge),
        "bat_soc": julia_to_py_array(jl_result.bat_soc),
        # Reserves
        "reserve_static": julia_to_py_array(jl_result.reserve_static),
        "reserve_dynamic": julia_to_py_array(jl_result.reserve_dynamic),
        "loss_of_reserve_static": julia_to_py_array(jl_result.reserve_static_loss),
        "loss_of_reserve_dynamic": julia_to_py_array(jl_result.reserve_dynamic_loss),
        # Load shedding and emissions
        "load_shed": julia_to_py_array(jl_result.load_shed),
        "co2_emissions": julia_to_py_array(jl_result.co2_emissions),
        # Network
        "voltage_angle": julia_to_py_array(jl_result.voltage_angle),
        # AC-specific outputs (None on DC runs, populated when power_flow_mode
        # is acopf_*). voltage_magnitude is per-bus per-hour in p.u.;
        # reactive_generation is [gen × bus × hour] in MVAr.
        "voltage_magnitude": (julia_to_py_array(jl_result.voltage_magnitude)
                              if jl_result.voltage_magnitude is not None else None),
        "reactive_generation": (julia_to_py_array(jl_result.reactive_generation)
                                if jl_result.reactive_generation is not None else None),
        # Prices (dual variables are in M$/MWh → $/MWh)
        "energy_prices": julia_to_py_array(jl_result.energy_prices) * COST_UNSCALE,
        # System metrics
        "total_generation": float(jl_result.total_generation),
        "total_demand": float(jl_result.total_demand),
        "total_losses": float(jl_result.total_losses),
        "re_penetration": float(jl_result.re_penetration),
        "total_co2": float(jl_result.total_co2),
        "load_shed_total": float(jl_result.load_shed_total),
    }

    # Handle optional generation fields
    if jl_result.gen_status is not None:
        result["gen_status"] = julia_to_py_array(jl_result.gen_status)

    if jl_result.gen_startup is not None:
        result["gen_startup"] = julia_to_py_array(jl_result.gen_startup)

    # Convert power flow dict (legacy node-pair aggregated flows)
    power_flow = {}
    for key, value in jl_result.power_flow.items():
        py_key = (convert_index_julia_to_py(key[0]), convert_index_julia_to_py(key[1]))
        power_flow[py_key] = julia_to_py_array(value)
    result["power_flow"] = power_flow

    # Convert per-line power flow (if available from enhanced DC PF)
    if jl_result.power_flow_by_line is not None:
        pf_by_line = []
        for line_pf in jl_result.power_flow_by_line:
            pf_by_line.append(julia_to_py_array(line_pf))
        result["power_flow_by_line"] = pf_by_line

    # Line losses [bus x hour] (MW): half of each line's loss at each end
    if jl_result.line_losses is not None:
        result["line_losses"] = julia_to_py_array(jl_result.line_losses)

    # Battery spillage [bat x node x hour]
    if jl_result.bat_spillage is not None:
        result["bat_spillage"] = julia_to_py_array(jl_result.bat_spillage)

    # EV variables [node x hour]
    if jl_result.ev_charging is not None:
        result["ev_charging"] = julia_to_py_array(jl_result.ev_charging)
    if jl_result.ev_v2g is not None:
        result["ev_v2g"] = julia_to_py_array(jl_result.ev_v2g)
    if jl_result.ev_soc is not None:
        result["ev_soc"] = julia_to_py_array(jl_result.ev_soc)
    if jl_result.ev_loss is not None:
        result["ev_loss"] = julia_to_py_array(jl_result.ev_loss)

    # Loss of inertia [hour]
    if jl_result.loss_of_inertia is not None:
        result["loss_of_inertia"] = julia_to_py_array(jl_result.loss_of_inertia)

    # Transfer margin {(from, to): array[hour]}
    if jl_result.transfer_margin is not None:
        tm = {}
        for key, value in jl_result.transfer_margin.items():
            py_key = (convert_index_julia_to_py(key[0]), convert_index_julia_to_py(key[1]))
            tm[py_key] = julia_to_py_array(value)
        result["transfer_margin"] = tm

    # Reservoir hydroelectric results
    if jl_result.reservoir_level is not None:
        result["reservoir_level"] = julia_to_py_array(jl_result.reservoir_level)
    if jl_result.reservoir_spillage is not None:
        result["reservoir_spillage"] = julia_to_py_array(jl_result.reservoir_spillage)
    if jl_result.reservoir_pump is not None:
        result["reservoir_pump"] = julia_to_py_array(jl_result.reservoir_pump)

    # Electrolyzers [electrolyzer x bus x hour]: power (MW), hydrogen (kg/h)
    if jl_result.electrolyzer_power is not None:
        result["electrolyzer_power"] = julia_to_py_array(jl_result.electrolyzer_power)
        result["electrolyzer_h2"] = julia_to_py_array(jl_result.electrolyzer_h2)

    # N-1 security results
    if jl_result.n1_gen_reserve_duals is not None:
        result["n1_gen_reserve_duals"] = julia_to_py_array(jl_result.n1_gen_reserve_duals)
    if jl_result.n1_trans_reserve_duals is not None:
        n1_trans = {}
        for key, value in jl_result.n1_trans_reserve_duals.items():
            py_key = (int(key[0]), int(key[1]), int(key[2]))
            n1_trans[py_key] = julia_to_py_array(value)
        result["n1_trans_reserve_duals"] = n1_trans
    if jl_result.n1_binding_contingencies is not None:
        result["n1_binding_contingencies"] = list(jl_result.n1_binding_contingencies)
    result["n1_security_cost"] = float(jl_result.n1_security_cost) * COST_UNSCALE

    # Cost breakdown (granular decomposition from build_objective!)
    if jl_result.cost_breakdown is not None:
        cb = jl_result.cost_breakdown
        result["cost_breakdown"] = {
            "fuel_cost": float(cb.fuel_cost) * COST_UNSCALE,
            "fixed_om_cost": float(cb.fixed_om_cost) * COST_UNSCALE,
            "maintenance_cost": float(cb.maintenance_cost) * COST_UNSCALE,
            "startup_cost": float(cb.startup_cost) * COST_UNSCALE,
            "battery_maintenance_cost": float(cb.battery_maintenance_cost) * COST_UNSCALE,
            "battery_degradation_cost": float(cb.battery_degradation_cost) * COST_UNSCALE,
            "load_shedding_cost": float(cb.load_shedding_cost) * COST_UNSCALE,
            "curtailment_cost": float(cb.curtailment_cost) * COST_UNSCALE,
            "reserve_static_cost": float(cb.reserve_static_cost) * COST_UNSCALE,
            "reserve_dynamic_cost": float(cb.reserve_dynamic_cost) * COST_UNSCALE,
            "co2_emission_cost": float(cb.co2_emission_cost) * COST_UNSCALE,
            "fre_penetration_cost": float(cb.fre_penetration_cost) * COST_UNSCALE,
            "inertia_cost": float(cb.inertia_cost) * COST_UNSCALE,
            "soc_violation_cost": float(cb.soc_violation_cost) * COST_UNSCALE,
            "transfer_margin_cost": float(cb.transfer_margin_cost) * COST_UNSCALE,
            "v2g_compensation": float(cb.v2g_compensation) * COST_UNSCALE,
            "electrolyzer_cost": float(cb.electrolyzer_cost) * COST_UNSCALE,
            "converter_cost": float(cb.converter_cost) * COST_UNSCALE,
            "spillage_cost": float(cb.spillage_cost) * COST_UNSCALE,
            "reservoir_spillage_cost": float(cb.reservoir_spillage_cost) * COST_UNSCALE,
            "demand_shift_cost": float(cb.demand_shift_cost) * COST_UNSCALE,
            "rooftop_curtailment_cost": float(cb.rooftop_curtailment_cost) * COST_UNSCALE,
            # PrimaryEnergy sub-costs.
            "pe_supply_cost": float(cb.pe_supply_cost) * COST_UNSCALE,
            "pe_loss_cost": float(cb.pe_loss_cost) * COST_UNSCALE,
            "pe_excess_cost": float(cb.pe_excess_cost) * COST_UNSCALE,
            "pe_transport_cost": float(cb.pe_transport_cost) * COST_UNSCALE,
            "pe_investment_cost": float(cb.pe_investment_cost) * COST_UNSCALE,
            "pe_electrolyzer_cost": float(cb.pe_electrolyzer_cost) * COST_UNSCALE,
            # N-1 SCOPF reliability shortfall.
            "n1_security_shortfall_cost": float(cb.n1_security_shortfall_cost) * COST_UNSCALE,
            "line_flow_cost": float(cb.line_flow_cost) * COST_UNSCALE,
            # Operating budget: the money above it and its penalty
            "operating_budget_excess": float(cb.operating_budget_excess) * COST_UNSCALE,
            "operating_budget_penalty": float(cb.operating_budget_penalty) * COST_UNSCALE,
            "total": float(cb.total) * COST_UNSCALE,
        }

    return result


def convert_scenario(scenario: dict) -> Any:
    """
    Convert a Python scenario dict to Julia Scenario struct.

    Args:
        scenario: Dictionary with scenario configuration

    Returns:
        Julia Scenario struct
    """
    from esfex.bridge.julia_setup import get_esfex_module

    ESFEX = get_esfex_module()

    # Validating translates legacy Studio keys and drops keys with no effect
    mult = ScenarioMultipliers.model_validate(scenario.get("multipliers") or {})
    jl_multipliers = ESFEX.ScenarioMultipliers(
        **{name: float(value) for name, value in mult.model_dump().items()})

    return ESFEX.Scenario(
        scenario["name"],
        float(scenario["probability"]),
        jl_multipliers,
    )
