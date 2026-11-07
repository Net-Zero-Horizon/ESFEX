"""
ESFEX Simulation Runner.

Main entry point for running ESFEX optimization simulations.
Manages the complete simulation workflow including:
- Master Problem solution for capacity expansion planning
- Year-by-year operational dispatch with rolling horizon
- Primary energy integration
- Results collection and HDF5 export
"""

import gc
import logging
import os
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Optional, Dict, List, Tuple, Union

import sys

import numpy as np
import pandas as pd
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn

from esfex.config.schema import CostCurveConfig, ESFEXConfig, SystemConfig
from esfex.bridge.adapters import PowerSystemAdapter, MasterProblemAdapter, PrimaryEnergyAdapter
from esfex.models.ev import (
    aggregate_ev_profiles,
    fleet_growth_factor,
    generate_ev_profiles,
    generate_v2g_availability,
)
from esfex.io.demand import _read_tabular, create_sectoral_demand, read_hourly_table
from esfex.utils import aggregate_demand_to_resolution
from esfex.utils.per_unit import derived_reactance_pu, system_s_base
from esfex.utils.temporal import (
    DEFAULT_RUN_YEARS,
    HOURS_STD_YEAR,
    aggregate_to_resolution,
    drop_leap_days,
    hours_for_year,
    model_time_index,
    parse_date_start,
)
from esfex.zones import expand_config_with_zones


logger = logging.getLogger(__name__)
# Force UTF-8 output to avoid cp1252 UnicodeEncodeError on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
console = Console(force_terminal=True)


def _node_link_impedances(lines, buses, dc_config, s_base_mva) -> dict[tuple[int, int], tuple[float, float]]:
    """Equivalent series (R, X) in p.u. of the bus-level lines between each node pair.

    The master problem joins nodes with one synthetic link per pair; this gives
    that link the impedance of the lines it stands for. Parallel lines share a
    flow F in proportion to 1/X: the equivalent reactance is 1 / Σ 1/X_k, and
    the loss F² Σ R_k s_k² with s_k = (1/X_k)/Σ 1/X gives the equivalent R.
    R and X are per circuit, as in the dispatch network.
    """
    by_pair: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for line in lines:
        a = buses[line.from_bus].parent_node if line.from_bus is not None and line.from_bus < len(buses) else line.from_node
        b = buses[line.to_bus].parent_node if line.to_bus is not None and line.to_bus < len(buses) else line.to_node
        if a == b:
            continue
        x = line.reactance_pu
        if x is None or x <= 0:
            x = derived_reactance_pu(line.length_km, line.voltage_kv, dc_config,
                                     s_base_mva, line.reactance_per_km)
        n = max(line.num_circuits, 1)
        by_pair.setdefault((min(a, b), max(a, b)), []).append(
            ((line.resistance_pu or 0.0) / n, x / n))
    result = {}
    for pair, rx in by_pair.items():
        total_b = sum(1.0 / x for _, x in rx)
        result[pair] = (sum(r * ((1.0 / x) / total_b) ** 2 for r, x in rx), 1.0 / total_b)
    return result


def _reservoir_investment_key(key: str) -> Optional[tuple[int, int]]:
    """``(generator, master location)`` (0-based) of a
    ``reservoir_investment_{g}_{n}`` investment key; ``None`` for any other key."""
    prefix = "reservoir_investment_"
    if not key.startswith(prefix):
        return None
    g, _, n = key[len(prefix):].partition("_")
    if not (g.isdigit() and n.isdigit()):
        return None
    return int(g), int(n)


def _converter_investment_key(key: str) -> tuple[Optional[str], int]:
    """``("acdc" | "freq", index)`` of a ``converter_investment_{kind}_{c}``
    investment key; ``(None, -1)`` for any other key."""
    prefix = "converter_investment_"
    if not key.startswith(prefix):
        return None, -1
    kind, _, idx = key[len(prefix):].partition("_")
    if kind not in ("acdc", "freq") or not idx.isdigit():
        return None, -1
    return kind, int(idx)


def _link_flow_costs(links, offsets: dict[str, int]) -> dict[tuple[int, int], dict]:
    """Flow cost of the master's node links that stand for inter-system links.

    An electrical inter-system link costs ``distance_km × cost_per_mw_km``
    per MWh it carries. The master joins each pair of merged nodes
    ``(i, j)``, ``i < j``, with one link: it gets the links' mean length and
    a cost per MW·km that gives their mean cost per MWh (weighted by their
    existing capacity), as ``{"length_km": …, "cost_per_mw_km": …}``.
    """
    def _at(seq, idx):
        return float(seq[idx]) if idx < len(seq) and seq[idx] is not None else 0.0

    acc: dict[tuple[int, int], list[float]] = {}
    for link in links:
        if len(link.systems) < 2:
            continue
        sa, sb = link.systems[0], link.systems[1]
        if sa not in offsets or sb not in offsets:
            continue
        for idx, (na, nb) in enumerate(link.connections):
            ga, gb = offsets[sa] + na, offsets[sb] + nb
            dist, cost = _at(link.distance_km, idx), _at(link.cost_per_mw_km, idx)
            w = max(_at(link.existing_capacity_mw, idx), 1e-6)
            a = acc.setdefault((min(ga, gb), max(ga, gb)), [0.0, 0.0, 0.0])
            a[0] += w
            a[1] += w * dist
            a[2] += w * dist * cost
    out: dict[tuple[int, int], dict] = {}
    for pair, (w, wd, wdc) in acc.items():
        if wd > 0 and wdc > 0:
            out[pair] = {"length_km": wd / w, "cost_per_mw_km": wdc / wd}
    return out


def _fuel_link_routes(links, offsets: dict[str, int]) -> list[dict]:
    """Primary-energy transport routes for the ``fuel_route`` inter-system links.

    Each connection of a fuel link joins two merged (global) nodes. Like a
    GUI fuel route it becomes two one-way routes, ``…_fwd`` and ``…_rev``,
    for the link's fuel: route capacity = ``existing_capacity_MW`` (fuel
    units/day), ``transport_cost`` = ``cost_per_mw_km`` ($/unit/km),
    ``losses_fraction`` = ``loss_factor`` (per 100 km). Capacity investment
    on the route follows the fuel's ``fuel_infrastructure``, so the link's
    ``max_investment_MW`` / ``investment_cost_per_MW`` are not used.

    Route ids are ``link{k}_{A}_{B}_{fuel}_{i}_fwd|rev``, with ``k`` the
    link's position in ``systems_links`` and ``i`` the connection, so two
    fuel links between the same systems (e.g. Diesel and LNG) keep apart.
    """
    def _at(seq, idx, default=0.0):
        return float(seq[idx]) if idx < len(seq) and seq[idx] is not None else default

    routes: list[dict] = []
    for pos, link in enumerate(links):
        if link.link_type != "fuel_route" or len(link.systems) < 2:
            continue
        sa, sb = link.systems[0], link.systems[1]
        if sa not in offsets or sb not in offsets:
            continue
        if not link.fuel:
            logger.warning(f"Fuel-route link {sa}-{sb} has no fuel; it is ignored.")
            continue
        if any(link.max_investment_mw) or any(link.investment_cost_per_mw):
            logger.warning(
                f"Fuel-route link {sa}-{sb}: max_investment_MW / investment_cost_per_MW "
                f"are not used; route investment follows fuel_infrastructure.{link.fuel}.")
        for idx, (na, nb) in enumerate(link.connections):
            ga, gb = offsets[sa] + na, offsets[sb] + nb
            params = {link.fuel: {
                "capacity": _at(link.existing_capacity_mw, idx),
                "transport_cost": _at(link.cost_per_mw_km, idx),
                "losses_fraction": _at(link.loss_factor, idx),
            }}
            dist = _at(link.distance_km, idx)
            for suffix, frm, to in (("fwd", ga, gb), ("rev", gb, ga)):
                routes.append({
                    "route_id": f"link{pos}_{sa}_{sb}_{link.fuel}_{idx}_{suffix}",
                    "from_node": frm,
                    "to_node": to,
                    "distance_km": dist,
                    "fuel_params": {f: dict(p) for f, p in params.items()},
                })
    return routes


def _concat_window_series(windows) -> dict:
    """Join per-window ``{key: series}`` dicts along time.

    ``windows`` is a list of ``(dict, keep_steps)``: each series is cut to
    the window's committed steps (the overlap tail belongs to the next
    window), and a key missing from a window (no variable or no binding
    dual there) is zero-filled so every series stays aligned with the
    year's time axis.
    """
    keys: list = []
    for d, _ in windows:
        keys.extend(k for k in d if k not in keys)
    return {
        k: np.concatenate([
            np.asarray(d[k], dtype=float)[:n] if k in d else np.zeros(n)
            for d, n in windows
        ])
        for k in keys
    }


def unique_unit_names(names: list[str]) -> list[str]:
    """Index-aligned unit names with homonyms made unique.

    The first unit keeps its name; later ones with the same name get
    `` (1)``, `` (2)``… (skipping a candidate that is already taken). This
    is the one convention of the results file: the runner's name lists,
    the HDF5 dataset names and the readers that strip the suffix to find a
    unit's configuration (``financial_analysis``, the results charts).
    """
    used = set()
    out: list[str] = []
    for name in names:
        name = str(name)
        cand, k = name, 0
        while cand in used:
            k += 1
            cand = f"{name} ({k})"
        used.add(cand)
        out.append(cand)
    return out


def _profile_year_block(profile: np.ndarray, year_idx: int, steps: int) -> np.ndarray:
    """Rows of ``profile`` for year ``year_idx`` of the horizon.

    Same rule as the dispatch (``absolute_start_hour``): step ``t`` of year
    ``k`` is row ``k × steps_per_year + t``, wrapping around a shorter
    profile, so a multi-year profile gives each year its own block and a
    one-year profile repeats.
    """
    n = len(profile)
    if n == 0 or steps <= 0:
        return profile[:0]
    return np.take(profile, (year_idx * steps + np.arange(steps)) % n, axis=0)


def _pe_period_bounds(n_steps: int, period_steps: int) -> list[tuple[int, int]]:
    """``[start, end)`` steps of each primary-energy period of a window.

    Mirrors ``create_temporal_mapping`` in primary_energy.jl: periods of
    ``min(period_steps, n_steps)`` steps, the last one possibly shorter.
    """
    length = min(period_steps, n_steps) if period_steps > 0 else n_steps
    length = max(length, 1)
    return [(s, min(s + length, n_steps)) for s in range(0, n_steps, length)]


def _merge_pe_windows(windows, period_steps: int) -> dict:
    """Join the primary-energy results of the rolling-horizon windows.

    ``windows`` is a list of ``(pe_result, window_steps, keep_steps)``.
    Per-period series (fuel supply, non-electric demand served, fuel
    shortfall, transport flows ``[route x period]``) are spread evenly over
    the steps of their period, cut to the window's committed steps (the
    overlap tail belongs to the next window), joined along time and summed
    back into periods of ``period_steps`` steps from the start of the year.
    Windows may have different period counts (the last one is shorter).
    Each window's costs already cover only its committed steps
    (``PrimaryEnergyAdapter.get_results(keep_steps=...)``) and are summed.
    ``fuel_for_power`` (``{unit: [node x step]}``) is cut to the committed
    steps and joined like the dispatch arrays. Investments and end-of-year
    tank levels come from the last window.
    """
    def _committed_steps(arr, n_steps, keep):
        arr = np.asarray(arr, dtype=float)
        bounds = _pe_period_bounds(n_steps, period_steps)
        if arr.shape[-1] != len(bounds):
            raise ValueError(
                f"primary-energy series has {arr.shape[-1]} periods, "
                f"expected {len(bounds)} for {n_steps} steps")
        per_step = np.concatenate(
            [np.repeat(arr[..., [p]] / (e - s), e - s, axis=-1)
             for p, (s, e) in enumerate(bounds)], axis=-1)
        return per_step[..., :keep]

    def _to_periods(series):
        total = series.shape[-1]
        return np.stack(
            [series[..., s:e].sum(axis=-1) for s, e in _pe_period_bounds(total, period_steps)],
            axis=-1)

    merged: dict = {}
    for key in ("total_fuel_supply", "total_ne_demand_satisfied",
                "total_loss_of_supply", "transport_flows"):
        fuels: list = []
        for pe, _, _ in windows:
            fuels.extend(f for f in (pe.get(key) or {}) if f not in fuels)
        merged[key] = {}
        for fuel in fuels:
            parts = []
            shape = next(np.asarray(pe[key][fuel]).shape[:-1] for pe, _, _ in windows
                         if fuel in (pe.get(key) or {}))
            for pe, n_steps, keep in windows:
                arr = (pe.get(key) or {}).get(fuel)
                parts.append(_committed_steps(arr, n_steps, keep) if arr is not None
                             else np.zeros(shape + (keep,)))
            merged[key][fuel] = _to_periods(np.concatenate(parts, axis=-1))
    last_pe = windows[-1][0]
    merged["final_storage_levels"] = last_pe.get("final_storage_levels", {})
    merged["transport_investments"] = last_pe.get("transport_investments", {})
    merged["storage_investments"] = last_pe.get("storage_investments", {})
    for cost_key in ("total_fuel_cost", "total_transport_cost", "total_loss_penalty"):
        merged[cost_key] = sum(float(pe.get(cost_key, 0.0)) for pe, _, _ in windows)
    units: list = []
    for pe, _, _ in windows:
        units.extend(u for u in (pe.get("fuel_for_power") or {}) if u not in units)
    if units:
        merged["fuel_for_power"] = {}
        for unit in units:
            n_nodes = next(np.asarray(pe["fuel_for_power"][unit]).shape[0] for pe, _, _ in windows
                           if unit in (pe.get("fuel_for_power") or {}))
            merged["fuel_for_power"][unit] = np.concatenate([
                np.asarray(pe["fuel_for_power"][unit], dtype=float)[:, :keep]
                if unit in (pe.get("fuel_for_power") or {}) else np.zeros((n_nodes, keep))
                for pe, _, keep in windows], axis=-1)
    return merged


@dataclass
class SimulationState:
    """Tracks state across simulation years."""

    year: int
    base_year: int
    units_config: dict[str, Any]
    boundary_conditions: dict[str, Any] = field(default_factory=dict)
    cumulative_investments: dict[str, Any] = field(default_factory=dict)
    cumulative_retirements: dict[str, Any] = field(default_factory=dict)
    # Fuel storage/transport capacity the master built that the current
    # year's dispatch sees: {kind: {fuel: {node or route index: capacity}}}
    primary_energy_capacities: dict[str, Any] = field(default_factory=lambda: {"storage": {}, "transport": {}})


@dataclass
class YearResults:
    """Results from a single year's optimization."""

    year: int
    objective: float
    solve_time: float
    feasible: bool
    # Generation [gen x node x hour]
    gen_output: Optional[np.ndarray] = None
    gen_status: Optional[np.ndarray] = None
    gen_startup: Optional[np.ndarray] = None
    # Curtailment [gen x node x hour]
    curtailment: Optional[np.ndarray] = None
    # Storage [bat x node x hour]
    bat_charge: Optional[np.ndarray] = None
    bat_discharge: Optional[np.ndarray] = None
    bat_soc: Optional[np.ndarray] = None
    # Reserves [node x hour]
    reserve_static: Optional[np.ndarray] = None
    reserve_dynamic: Optional[np.ndarray] = None
    loss_of_reserve_static: Optional[np.ndarray] = None
    loss_of_reserve_dynamic: Optional[np.ndarray] = None
    # Load shedding [node x hour]
    load_shed_array: Optional[np.ndarray] = None
    # CO2 emissions [node x hour]
    co2_emissions: Optional[np.ndarray] = None
    # Network
    power_flow: Optional[dict] = None              # {(from,to): array[hour]}
    voltage_angle: Optional[np.ndarray] = None     # [node x hour]
    # AC-specific outputs — populated only when power_flow_mode is acopf_*.
    voltage_magnitude: Optional[np.ndarray] = None        # [bus x hour] in p.u.
    reactive_generation: Optional[np.ndarray] = None      # [gen x bus x hour] in MVAr
    transfer_investment: Optional[dict] = None     # {(from,to): value}
    # Prices [node x hour]
    prices: Optional[np.ndarray] = None
    # Demand [hour x node]
    demand: Optional[np.ndarray] = None
    # Investment/retirement decisions
    investments: dict[str, float] = field(default_factory=dict)
    retirements: dict[str, float] = field(default_factory=dict)
    gen_investment_array: Optional[np.ndarray] = None   # [gen x node]
    bat_investment_power: Optional[np.ndarray] = None   # [bat x node]
    bat_investment_capacity: Optional[np.ndarray] = None  # [bat x node]
    # Battery spillage [bat x node x hour]
    bat_spillage: Optional[np.ndarray] = None
    # EV variables [node x hour]
    ev_charging: Optional[np.ndarray] = None
    ev_v2g: Optional[np.ndarray] = None
    ev_soc: Optional[np.ndarray] = None
    ev_loss: Optional[np.ndarray] = None
    # Rooftop solar generation [hour x node] — behind-the-meter, already
    # netted out of demand. Exported as diagnostic only.
    rooftop_generation: Optional[np.ndarray] = None
    # System-wide [hour]
    loss_of_inertia: Optional[np.ndarray] = None
    # Transfer margin {(from,to): array[hour]}
    transfer_margin: Optional[dict] = None
    # Reservoir hydroelectric [gen x node x hour]
    reservoir_level: Optional[np.ndarray] = None
    reservoir_spillage: Optional[np.ndarray] = None
    reservoir_pump: Optional[np.ndarray] = None
    reservoir_invest_capacity: Optional[np.ndarray] = None  # [gen x node]
    # Electrolyzers [electrolyzer x node x hour], in config order
    electrolyzer_power: Optional[np.ndarray] = None       # MW drawn
    electrolyzer_h2: Optional[np.ndarray] = None          # kg/h of hydrogen
    # Derived metrics (computed post-optimization)
    capacity_factor: Optional[np.ndarray] = None          # [gen x node x hour]
    lcoe: Optional[np.ndarray] = None                     # [gen x node x hour]
    vallcoe: Optional[np.ndarray] = None                  # [gen x node x hour]
    bat_capacity_factor: Optional[np.ndarray] = None      # [bat x node x hour]
    bat_lcoe: Optional[np.ndarray] = None                 # [bat x node x hour]
    bat_vallcoe: Optional[np.ndarray] = None              # [bat x node x hour]
    fuel_for_power: Optional[dict] = None                 # {unit: [node x hour]} fuel units/step
    technology_selling_prices: Optional[dict] = None      # {tech_name: {prices_weights, ...}}
    price_energy_component: Optional[np.ndarray] = None   # [hour]
    price_congestion_component: Optional[np.ndarray] = None  # [node x hour]
    # Primary energy results
    primary_energy: Optional[dict] = None                 # {metric: {fuel: array}}
    # N-1 security results
    n1_gen_reserve_duals: Optional[np.ndarray] = None      # [hour] — dual of gen N-1 constraint
    n1_trans_reserve_duals: Optional[dict] = None           # {(i,j): array[hour]} — dual of trans N-1
    n1_binding_contingencies: Optional[list] = None         # List of binding contingency IDs per hour
    n1_security_cost: float = 0.0                           # Incremental cost of N-1 security ($)
    # System-level scalars
    emissions: float = 0.0
    re_penetration: float = 0.0
    load_shed: float = 0.0
    total_generation: float = 0.0
    total_demand: float = 0.0
    master_re_target: float = 0.0  # Master problem RE penetration target for this year
    # Granular cost decomposition (accumulated from operational windows)
    cost_breakdown: Optional[dict] = None



def re_target_trajectory(initial: float, target: float, num_years: int,
                         min_inc: float, max_inc: float) -> list[float]:
    """Yearly RE targets of the planning horizon (RE-1, as the Julia
    ``calculate_target_ratios``): the initial share in year 1, then a
    linear path to ``target`` whose yearly step is clamped to
    ``[min_inc, max_inc]``."""
    path: list[float] = []
    for y in range(num_years):
        if y == 0:
            path.append(initial)
            continue
        tau = initial + y / (num_years - 1) * (target - initial)
        step = tau - path[-1]
        if step < min_inc:
            tau = path[-1] + min_inc
        elif step > max_inc:
            tau = path[-1] + max_inc
        path.append(tau)
    return path


def _warn_flat_renewable(kind: str, key: str) -> None:
    """Log that a renewable unit without an availability profile is modelled
    as available in every hour (capacity factor 1.0)."""
    logger.warning(
        "%s %r is Renewable but has no availability_file: it is modelled as "
        "available at full capacity in every hour (capacity factor 1.0). "
        "Right for dispatchable renewables (biomass, geothermal); set a "
        "profile for solar, wind and other variable sources.", kind, key)


def technology_unit_fields(tech, rep, rep_curve=None) -> dict:
    """Operating parameters of a generator built from candidate technology *tech*.

    Shared by the dispatch's virtual generators (master investments, per
    bus) and the myopic master's units built in earlier years (per node).
    ``rep(arr, default)`` lays a per-node technology list out on the unit's
    array positions; ``rep_curve(curves)`` does the same for the bidding
    curves (default: the technology's own list). Capacity, age, lifetime,
    degradation and investment fields are left to the caller.
    """
    return {
        "name": f"Investment {tech.name}",
        "type": tech.type,
        "fuel": tech.fuel,
        # A fraction of the rated power, like every generator's min_power
        "min_power": rep(tech.min_output, 0.0),
        "eff_at_rated": rep(tech.eff_at_rated, 0.0),
        "eff_at_min": rep(tech.eff_at_min, 0.0),
        "ramp_up": rep(tech.ramp_up, 0.0),
        "ramp_down": rep(tech.ramp_down, 0.0),
        "min_up": rep(tech.min_up, 0),
        "min_down": rep(tech.min_down, 0),
        "fuel_cost": rep(tech.fuel_cost, 0.0),
        "fuel_cost_curve": (rep_curve(tech.fuel_cost_curve) if rep_curve
                            else (list(tech.fuel_cost_curve) if tech.fuel_cost_curve else None)),
        "fixed_cost": rep(tech.fixed_cost, 0.0),
        "maintenance_cost": rep(tech.maintenance_cost, 0.0),
        "start_up_cost": rep(tech.start_up_cost, 0.0),
        "inertia": rep(tech.inertia, 0.0),
        "droop": rep(tech.droop, 0.05),
        "governor_time_const": rep(tech.governor_time_const, 5.0),
        "risk_coefficient": rep(tech.risk_coefficient, 1.0),
        "availability_file": tech.availability_file,
        "frequency_hz": tech.frequency_hz,
        "current_type": tech.current_type,
        "reservable": tech.reservable,
    }


def battery_technology_unit_fields(bat_tech, rep) -> dict:
    """Operating parameters of a battery built from battery technology *bat_tech*.

    Counterpart of :func:`technology_unit_fields` for storage; power,
    energy, age, lifetime and investment fields are left to the caller.
    """
    return {
        "_type": "battery",
        "type": "Storage",
        "name": f"Investment {bat_tech.name}",
        "efficiency_charge": rep(bat_tech.efficiency_charge, 0.95),
        "efficiency_discharge": rep(bat_tech.efficiency_discharge, 0.95),
        "soc_initial": rep(bat_tech.soc_initial, 0.0),
        "max_DoD": rep(bat_tech.max_DoD, 0.0),
        "maintenance_cost": rep(bat_tech.maintenance_cost, 0.0),
        "inertia": rep(bat_tech.inertia, 0.0),
        "throughput_degradation_cost": rep(bat_tech.throughput_degradation_cost, 0.0),
        "risk_coefficient": rep(bat_tech.risk_coefficient, 1.0),
        "spillage": bat_tech.spillage,
        "current_type": bat_tech.current_type,
    }


def _pad_zeros(arr, n: int) -> list:
    """*arr* as a list of at least *n* floats, padded with zeros (a per-node
    quantity a unit does not have at the missing nodes)."""
    out = [float(v) for v in (arr or [])]
    return out + [0.0] * (n - len(out))


# rooftex's initial adoption fraction of a node without one
_ROOFTEX_DEFAULT_INITIAL_ADOPTION = 0.05


def _per_node_list(arr, n: int, default=0.0) -> list:
    """A per-node list of length *n*: a single value is broadcast, a short
    list is padded with its last value."""
    if not arr:
        return [default] * n
    if len(arr) >= n:
        return list(arr[:n])
    if len(arr) == 1:
        return [arr[0]] * n
    return list(arr) + [arr[-1]] * (n - len(arr))


class Orchestrator:
    """
    Main orchestrator for ESFEX simulations.

    Coordinates the multi-year capacity expansion and operational
    optimization workflow.
    """

    def __init__(
        self,
        config: ESFEXConfig,
        output_dir: Optional[Union[str, Path]] = None,
        config_path: Optional[Union[str, Path]] = None,
    ):
        """
        Initialize the orchestrator.

        Args:
            config: Validated ESFEXConfig object
            output_dir: Directory for output files
            config_path: Path to the configuration file (for relative path resolution)
        """
        self.config = config
        self.output_dir = Path(output_dir or "./results")
        self.config_path = Path(config_path) if config_path else None
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Setup logging
        self._setup_logging()

        # Merge all selected systems into a single unified SystemConfig.
        # Inter-system links become additional transmission connections.
        self._system_node_offsets: dict[str, int] = {}
        self._system_names: list[str] = list(config.meta_network.systems)
        merged_master, merged_operational, offsets = self._merge_systems(config)
        self._system_node_offsets = offsets
        # Master problem: node-level network (adjacency matrix, no DC-OPF)
        self.master_system = merged_master
        # Operational dispatch: full bus-level network for DC-OPF
        self.primary_system = merged_operational
        self.system_name = "_".join(self._system_names)

        # Initialize state
        self.state: Optional[SimulationState] = None
        self.results: list[YearResults] = []
        self.master_solution: Optional[dict] = None  # set by run(master_only=True)

        logger.debug(f"Orchestrator initialized for system: {self.system_name}")
        logger.debug(f"Mode: {config.simulation_mode}")
        logger.debug(f"Solver: {config.solver.name}")

        # Plugin manager (runtime overlays — never modifies core source)
        from esfex.plugins import get_plugin_manager
        self._pm = get_plugin_manager()
        self._pm.load_all(config, gui_mode=False, project_dir=self.config_path.parent if self.config_path else None)
        plugin_errors = self._pm.apply_config(config)
        if plugin_errors:
            raise ValueError(
                "Invalid plugin configuration:\n  " + "\n  ".join(plugin_errors))
        self._pm.register_julia_modules()

        # Progress tracking
        self._progress: Optional[Progress] = None
        self._year_task = None
        self._window_task = None

    def _setup_logging(self):
        """Attach the run's file handler and ensure a console handler exists.

        The console handler is owned by :mod:`esfex.logging_config`
        and is idempotent: if cli.py already installed one we
        re-tune it instead of adding a second (which used to make every
        ``logger.info`` line appear twice).
        """
        log_dir = self.output_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = log_dir / f"optimization_{timestamp}.log"

        from esfex.logging_config import (
            setup_console_logging, setup_file_logging,
        )
        setup_file_logging(log_file)
        # Honour the per-yaml console_level. If a CLI front-end already
        # set "debug" via --verbose, our normalisation in setup_console_logging
        # keeps that level only if explicitly requested again; otherwise
        # the run's config wins (which is the natural reading: the file
        # the user just chose to run determines its own verbosity).
        cfg_level = getattr(getattr(self.config, "logging", None), "console_level", "basic")
        setup_console_logging(level=cfg_level)

        logger.debug(f"Logging to: {log_file}")

    # ------------------------------------------------------------------
    # Multi-system merge
    # ------------------------------------------------------------------
    @staticmethod
    def _merge_systems(
        config: ESFEXConfig,
    ) -> Tuple["SystemConfig", "SystemConfig", dict[str, int]]:
        """Merge all systems in *config.meta_network.systems* into one.

        Each system's per-node arrays are padded so that generators /
        batteries / technologies from system *A* have zero entries at
        positions that belong to system *B*, and vice-versa.

        Inter-system links (``config.meta_network.systems_links``) of
        type ``transmission`` are injected into the merged adjacency
        matrix so the optimiser "sees" them as regular transmission lines;
        ``fuel_route`` links become primary-energy transport routes
        (``fuel_transport_routes``) between the two systems' nodes.

        Each system keeps its own CO2 budget and violation penalty over
        its own nodes (``co2_budget_by_system``).

        Returns
        -------
        merged_master : SystemConfig
            Node-level unified system for the master investment problem
            (synthetic node-level ``master_lines`` with from_bus/to_bus).
        merged_operational : SystemConfig
            Bus-level unified system for the operational subproblems
            (full electrical bus network + propagated ``gui_layout``).
        node_offsets : dict[str, int]
            ``{system_name: first_global_node_index}``.
        """
        import math
        from esfex.config.schema import (
            SystemConfig, NodeConfig, TechnologyConfig,
            BatteryTechnologyConfig, GeneratorConfig, BatteryConfig,
            CO2BudgetConfig, SystemCO2Budget,
        )

        sys_names = list(config.meta_network.systems)
        systems = [config.systems[n] for n in sys_names]

        # Shortcut: single SINGLE-NODE system → no merging needed.
        # A multi-node single system still needs the merge pipeline so it
        # gets synthetic node-level master_lines (with from_bus/to_bus),
        # the bus-level merged_operational network, gui_layout
        # propagation and per-bus rated_power expansion.  Without this a
        # multi-node single-system master has no inter-node transmission
        # and existing generation capacity is not honoured (it sheds
        # everything).  Single-node systems (e.g. island grids) have no
        # inter-node transmission anyway, so the bare shortcut is safe.
        if len(systems) == 1:
            _s0 = systems[0]
            _nn0 = _s0.nodes.num_nodes or int(
                math.sqrt(len(_s0.nodes.nodes_connections))
            )
            if _nn0 <= 1:
                return _s0, _s0, {sys_names[0]: 0}

        # --- 1. Compute node offsets ------------------------------------
        node_counts = []
        for s in systems:
            nn = s.nodes.num_nodes or int(math.sqrt(len(s.nodes.nodes_connections)))
            node_counts.append(nn)
        total_nodes = sum(node_counts)

        offsets: dict[str, int] = {}
        cum = 0
        for name, nc in zip(sys_names, node_counts):
            offsets[name] = cum
            cum += nc

        logger.info(
            f"Merging {len(systems)} systems → {total_nodes} nodes  "
            f"({', '.join(f'{n}:{nc}' for n, nc in zip(sys_names, node_counts))})"
        )

        # --- 2. Merge adjacency matrix (block-diagonal + links) ---------
        merged_adj = [0.0] * (total_nodes * total_nodes)

        for sname, sys, nc in zip(sys_names, systems, node_counts):
            off = offsets[sname]
            src = sys.nodes.nodes_connections
            for i in range(nc):
                for j in range(nc):
                    merged_adj[(off + i) * total_nodes + (off + j)] = src[i * nc + j]

        # Electrical inter-system links. Fuel-route links carry fuel, not
        # power: they become primary-energy routes further down.
        electrical_links = [lk for lk in config.meta_network.systems_links
                            if lk.link_type == "transmission"]

        # Inject inter-system links
        for link in electrical_links:
            if len(link.systems) < 2:
                continue
            sa, sb = link.systems[0], link.systems[1]
            if sa not in offsets or sb not in offsets:
                continue
            off_a, off_b = offsets[sa], offsets[sb]
            for idx, (na, nb) in enumerate(link.connections):
                cap = link.existing_capacity_mw[idx] if idx < len(link.existing_capacity_mw) else 0.0
                gi = (off_a + na) * total_nodes + (off_b + nb)
                gj = (off_b + nb) * total_nodes + (off_a + na)
                merged_adj[gi] = max(merged_adj[gi], cap)
                merged_adj[gj] = max(merged_adj[gj], cap)

        # --- 3. Merge per-node scalar lists (reserves, losses, …) -------
        def _concat_node_list(attr: str, default=0.0):
            parts = []
            for sys, nc in zip(systems, node_counts):
                arr = getattr(sys.nodes, attr, [])
                if len(arr) >= nc:
                    parts.extend(arr[:nc])
                else:
                    parts.extend(arr + [default] * (nc - len(arr)))
            return parts

        reserve_static = _concat_node_list("reserve_static")
        reserve_dynamic = _concat_node_list("reserve_dynamic")
        reserve_duration = _concat_node_list("reserve_duration", default=1)
        losses = _concat_node_list("losses")
        # transference_invest_cost/max are per-node vectors (Julia indexes by [i])
        trans_inv_cost = _concat_node_list("transference_invest_cost")
        trans_inv_max = _concat_node_list("transference_invest_max")

        # Inject inter-system link investment data (per-node: set at both endpoints)
        for link in electrical_links:
            if len(link.systems) < 2:
                continue
            sa, sb = link.systems[0], link.systems[1]
            if sa not in offsets or sb not in offsets:
                continue
            off_a, off_b = offsets[sa], offsets[sb]
            for idx, (na, nb) in enumerate(link.connections):
                src_a = off_a + na
                src_b = off_b + nb
                if idx < len(link.investment_cost_per_mw):
                    trans_inv_cost[src_a] = max(trans_inv_cost[src_a], link.investment_cost_per_mw[idx])
                    trans_inv_cost[src_b] = max(trans_inv_cost[src_b], link.investment_cost_per_mw[idx])
                if idx < len(link.max_investment_mw):
                    trans_inv_max[src_a] = max(trans_inv_max[src_a], link.max_investment_mw[idx])
                    trans_inv_max[src_b] = max(trans_inv_max[src_b], link.max_investment_mw[idx])

        # Concat node names / coordinates
        node_names: list[str] = []
        node_coords = []
        for sname, sys, nc in zip(sys_names, systems, node_counts):
            nn = sys.nodes.node_names or [f"{sname}_n{i}" for i in range(nc)]
            node_names.extend(nn[:nc])
            nc_coords = sys.nodes.node_coordinates or []
            node_coords.extend(nc_coords[:nc] if nc_coords else [None] * nc)
        node_coords_clean = node_coords if any(c is not None for c in node_coords) else None

        merged_nodes = NodeConfig(
            num_nodes=total_nodes,
            nodes_connections=merged_adj,
            reserve_static=reserve_static,
            reserve_dynamic=reserve_dynamic,
            reserve_duration=reserve_duration,
            losses=losses,
            transference_invest_cost=trans_inv_cost,
            transference_invest_max=trans_inv_max,
            node_names=node_names,
            node_coordinates=node_coords_clean,
        )

        # --- 4. Helper: pad a per-node list for a given system ----------
        def _pad(arr: list, sys_idx: int, default=0.0) -> list:
            """Return a list of length *total_nodes* with *arr* placed at
            the correct offset for system *sys_idx*.

            A length-1 *arr* on a multi-node system is broadcast to all of
            that system's nodes (scalar-per-node convention).  Without this,
            per-node arrays authored as a single value (common when a
            single-node template is reused for a multi-node system) stay
            length 1 and trigger downstream BoundsErrors in the Julia
            master/operational models that index them per node/bus.
            """
            nc = node_counts[sys_idx]
            off = offsets[sys_names[sys_idx]]
            if len(arr) == 1 and nc > 1:
                arr = list(arr) * nc
            out = [default] * total_nodes
            for k in range(min(len(arr), nc)):
                out[off + k] = arr[k]
            return out

        def _pad_int(arr: list, sys_idx: int, default=0) -> list:
            return [int(v) for v in _pad(arr, sys_idx, default)]

        # Each subsystem was built independently by the Grid Builder, so their
        # bus_id namespaces overlap (e.g. Hokkaido and Tohoku both have
        # 'bus_2190'). Merging them into one SystemConfig requires globally
        # unique bus_ids, so namespace each subsystem's bus_id by its system
        # name. Applied only for a genuine multi-system merge so single-system
        # runs keep their original bus_ids. EVERY string reference to a bus_id
        # (generator/battery bus_id_per_node, gui_layout bus positions, and the
        # buses list itself) gets this same transform.
        multi_sys = len(systems) > 1

        def _uniq_bus_id(sname: str, bid):
            return f"{sname}__{bid}" if (multi_sys and bid is not None) else bid

        # --- 5. Merge generators ----------------------------------------
        merged_gens: dict[str, GeneratorConfig] = {}
        for si, (sname, sys) in enumerate(zip(sys_names, systems)):
            sys_off = offsets[sname]
            for gkey, gen in sys.generators.items():
                new_key = f"{sname}__{gkey}"
                d = gen.model_dump(by_alias=True)
                # Pad every per-node list field
                for field_name, field_info in GeneratorConfig.model_fields.items():
                    alias = field_info.alias or field_name
                    val = d.get(alias, d.get(field_name))
                    if isinstance(val, list) and len(val) in (1, node_counts[si]):
                        if field_name in ("life_time", "initial_age", "min_up", "min_down"):
                            d[alias if alias in d else field_name] = _pad_int(val, si)
                        else:
                            d[alias if alias in d else field_name] = _pad(val, si)
                # Shift bus_id_per_node keys by system offset so the per-node
                # bus mapping points at the merged-system indices, and namespace
                # its bus_id values to match the renamed buses.
                bipn = d.get("bus_id_per_node")
                if isinstance(bipn, dict):
                    d["bus_id_per_node"] = {
                        int(k) + sys_off: _uniq_bus_id(sname, v)
                        for k, v in bipn.items()
                    }
                d["name"] = f"{sname}/{gen.name}"
                merged_gens[new_key] = GeneratorConfig(**d)

        # --- 6. Merge batteries -----------------------------------------
        merged_bats: dict[str, BatteryConfig] = {}
        for si, (sname, sys) in enumerate(zip(sys_names, systems)):
            sys_off = offsets[sname]
            for bkey, bat in sys.batteries.items():
                new_key = f"{sname}__{bkey}"
                d = bat.model_dump(by_alias=True)
                for field_name, field_info in BatteryConfig.model_fields.items():
                    alias = field_info.alias or field_name
                    val = d.get(alias, d.get(field_name))
                    if isinstance(val, list) and len(val) in (1, node_counts[si]):
                        if field_name in ("life_time", "initial_age", "min_up", "min_down"):
                            d[alias if alias in d else field_name] = _pad_int(val, si)
                        else:
                            d[alias if alias in d else field_name] = _pad(val, si)
                bipn = d.get("bus_id_per_node")
                if isinstance(bipn, dict):
                    d["bus_id_per_node"] = {
                        int(k) + sys_off: _uniq_bus_id(sname, v)
                        for k, v in bipn.items()
                    }
                d["name"] = f"{sname}/{bat.name}"
                merged_bats[new_key] = BatteryConfig(**d)

        # --- 7. Merge technologies (investment candidates) --------------
        merged_techs: dict[str, TechnologyConfig] = {}
        for si, (sname, sys) in enumerate(zip(sys_names, systems)):
            for tkey, tech in sys.technologies.items():
                new_key = f"{sname}__{tkey}"
                d = tech.model_dump(by_alias=True)
                for field_name, field_info in TechnologyConfig.model_fields.items():
                    alias = field_info.alias or field_name
                    val = d.get(alias, d.get(field_name))
                    if isinstance(val, list) and len(val) in (1, node_counts[si]):
                        if field_name in ("min_up", "min_down"):
                            d[alias if alias in d else field_name] = _pad_int(val, si)
                        else:
                            d[alias if alias in d else field_name] = _pad(val, si)
                d["name"] = f"{sname}/{tech.name}"
                merged_techs[new_key] = TechnologyConfig(**d)

        # --- 8. Merge battery technologies ------------------------------
        merged_bat_techs: dict[str, BatteryTechnologyConfig] = {}
        for si, (sname, sys) in enumerate(zip(sys_names, systems)):
            for btkey, bt in sys.battery_technologies.items():
                new_key = f"{sname}__{btkey}"
                d = bt.model_dump(by_alias=True)
                for field_name, field_info in BatteryTechnologyConfig.model_fields.items():
                    alias = field_info.alias or field_name
                    val = d.get(alias, d.get(field_name))
                    if isinstance(val, list) and len(val) in (1, node_counts[si]):
                        d[alias if alias in d else field_name] = _pad(val, si)
                d["name"] = f"{sname}/{bt.name}"
                merged_bat_techs[new_key] = BatteryTechnologyConfig(**d)

        # --- 8b. Auto-assign technology to generators/batteries that lack it
        # Build fuel → tech_key lookup from each system's technologies
        fuel_to_tech_key: dict[str, dict[str, str]] = {}  # {sname: {fuel: tech_key}}
        for sname, sys in zip(sys_names, systems):
            fuel_map: dict[str, str] = {}
            for tkey, tech in sys.technologies.items():
                if tech.fuel and tech.fuel not in fuel_map:
                    fuel_map[tech.fuel] = tkey
            fuel_to_tech_key[sname] = fuel_map

        for gkey, gen in merged_gens.items():
            if gen.technology is None and gen.fuel:
                # Extract system name from merged key "SystemName__original_key"
                sname = gkey.split("__", 1)[0]
                tech_key = fuel_to_tech_key.get(sname, {}).get(gen.fuel)
                if tech_key:
                    gen.technology = tech_key

        # For batteries: match to battery_technologies by name keywords
        for bkey, bat in merged_bats.items():
            if getattr(bat, "technology", None) is None:
                sname = bkey.split("__", 1)[0]
                sys_bt = {k: v for k, v in merged_bat_techs.items()
                          if k.startswith(f"{sname}__")}
                if len(sys_bt) == 1:
                    # Only one battery technology → assign it
                    bat.technology = next(iter(sys_bt)).split("__", 1)[-1]

        # --- 9. Merge scalar / system-level settings --------------------
        # Use first system as base for scalar settings, override where
        # appropriate (e.g. take the most conservative thresholds).
        base = systems[0]

        # Merge fuels from all systems (union)
        merged_fuels = {}
        for sys in systems:
            merged_fuels.update(sys.fuels)

        # CO2 budget: each system keeps its own budget and violation
        # penalty over its own nodes (the dispatch adds one constraint per
        # system). The merged ``co2_budget`` is only their total, for display.
        co2_by_system: list = []
        if len(systems) > 1:
            for sname, sys, nc in zip(sys_names, systems, node_counts):
                co2_by_system.append(SystemCO2Budget(
                    system=sname,
                    first_node=offsets[sname],
                    num_nodes=nc,
                    enabled=sys.co2_budget.enabled,
                    annual_budget=sys.co2_budget.annual_budget,
                    violation_penalty=sys.penalties.co2_budget_violation,
                ))
            merged_co2_budget = CO2BudgetConfig(
                enabled=any(b.enabled for b in co2_by_system),
                annual_budget=sum(b.annual_budget for b in co2_by_system if b.enabled),
            )
        else:
            merged_co2_budget = base.co2_budget

        # Merge penalties: take max of each penalty across systems
        # (co2_budget_violation is applied per system, see above)
        merged_penalties_d = base.penalties.model_dump()
        for sys in systems[1:]:
            pd2 = sys.penalties.model_dump()
            for k, v in pd2.items():
                if isinstance(v, (int, float)) and v > merged_penalties_d.get(k, 0):
                    merged_penalties_d[k] = v

        # Merge EV / rooftop arrays (concat per-node). A rooftop potential
        # a system leaves unset is None (rooftex's potential at that node);
        # a system without rooftop solar gets 0 at its nodes.
        merged_ev_soc: list[float] = []
        merged_rooftop_potential: list[Optional[float]] = []
        for si, (sys, nc) in enumerate(zip(systems, node_counts)):
            soc = sys.ev_initial_soc or []
            # Nodes without a value start at the dispatch default (80 %)
            merged_ev_soc.extend(soc[:nc] if len(soc) >= nc else soc + [0.8] * (nc - len(soc)))
            if not (sys.sim_rooftop and sys.rooftop_solar_config is not None):
                merged_rooftop_potential.extend([0.0] * nc)
                continue
            rp = list(sys.rooftop_max_potential or [])[:nc]
            merged_rooftop_potential.extend(rp + [None] * (nc - len(rp)))
        if all(v is None for v in merged_rooftop_potential):
            merged_rooftop_potential = []

        # Merge rooftop_solar_config: take the first non-empty config as the
        # template and concat per-node arrays across systems. A system that
        # leaves a list unset gets None at its nodes, which the rooftop
        # generator resolves to rooftex's defaults; a list no system sets
        # stays unset.
        merged_rooftop_solar_config = None
        rooftop_template = next((s.rooftop_solar_config for s in systems
                                 if s.rooftop_solar_config is not None), None)
        if rooftop_template is not None:
            rsc_d = rooftop_template.model_dump()
            for list_field in ('systems_per_node', 'avg_system_size', 'initial_adoption'):
                if not any(getattr(s.rooftop_solar_config, list_field, None)
                           for s in systems if s.rooftop_solar_config is not None):
                    rsc_d[list_field] = None
                    continue
                merged_list: list = []
                for sys, nc in zip(systems, node_counts):
                    src_cfg = sys.rooftop_solar_config
                    src_list = list(getattr(src_cfg, list_field, []) or [])[:nc] if src_cfg else []
                    # A short list repeats its last value, as rooftex does
                    pad = src_list[-1] if src_list else None
                    merged_list.extend(src_list + [pad] * (nc - len(src_list)))
                rsc_d[list_field] = merged_list
            # max_adoption / adoption_rates are keyed by adoption scenario
            # ("low", "medium", "high"), not by node: the merged run has one
            # rooftop configuration, so the first system that sets one wins.
            for dict_field in ('max_adoption', 'adoption_rates'):
                set_by = [(sname, dict(getattr(sys.rooftop_solar_config, dict_field)))
                          for sname, sys in zip(sys_names, systems)
                          if sys.rooftop_solar_config is not None
                          and getattr(sys.rooftop_solar_config, dict_field, None)]
                rsc_d[dict_field] = set_by[0][1] if set_by else None
                ignored = [sname for sname, d in set_by[1:] if d != set_by[0][1]]
                if ignored:
                    logger.warning(
                        f"rooftop_solar_config.{dict_field} differs between systems; "
                        f"the merged run uses {set_by[0][0]!r}'s and ignores "
                        f"{', '.join(repr(s) for s in ignored)}'s")
            merged_rooftop_solar_config = rooftop_template.__class__(**rsc_d)

        # Merge EV categories / quantities (pad per-node arrays)
        merged_ev_categories = {}
        merged_ev_quantity: dict[str, list[int]] = {}
        merged_base_patterns: dict[str, list[float]] = {}
        for si, (sname, sys, nc) in enumerate(zip(sys_names, systems, node_counts)):
            for cat_key, cat_val in (sys.ev_categories or {}).items():
                if cat_key not in merged_ev_categories:
                    merged_ev_categories[cat_key] = cat_val
            for cat_key, qty_list in (sys.ev_quantity or {}).items():
                if cat_key not in merged_ev_quantity:
                    merged_ev_quantity[cat_key] = [0] * total_nodes
                for k in range(min(len(qty_list), nc)):
                    merged_ev_quantity[cat_key][offsets[sname] + k] = qty_list[k]
            for cat_key, pat in (sys.base_patterns or {}).items():
                if cat_key not in merged_base_patterns:
                    merged_base_patterns[cat_key] = pat

        # Merge sector_distribution (re-index by global node)
        merged_sector_dist: dict[int, dict[str, float]] = {}
        for si, (sname, sys, nc) in enumerate(zip(sys_names, systems, node_counts)):
            for local_node, sectors in (sys.sector_distribution or {}).items():
                merged_sector_dist[offsets[sname] + int(local_node)] = sectors

        # Merge electric_demand sector configs (union)
        merged_electric_demand = {}
        for sys in systems:
            merged_electric_demand.update(sys.electric_demand or {})

        # --- 9b. Merge bus-level network for operational dispatch ----------
        # The master problem uses node-level only (adjacency matrix from
        # section 2).  The operational phase uses the full bus-level network
        # (buses, transmission_lines_geo, transformers) for DC-OPF.
        from esfex.config.schema import (
            TransmissionLineGeo, TransformerConfig, BusConfig,
        )

        merged_buses: list[BusConfig] = []
        bus_offsets: dict[str, int] = {}
        bus_cum = 0
        for sname, sys, nc in zip(sys_names, systems, node_counts):
            bus_offsets[sname] = bus_cum
            sys_buses = sys.buses or []
            node_off = offsets[sname]
            for bus in sys_buses:
                merged_buses.append(bus.model_copy(update={
                    'parent_node': bus.parent_node + node_off,
                    'bus_id': _uniq_bus_id(sname, bus.bus_id),
                }))
            bus_cum += len(sys_buses)

        merged_lines_geo: list[TransmissionLineGeo] = []
        for sname, sys, nc in zip(sys_names, systems, node_counts):
            node_off = offsets[sname]
            bus_off = bus_offsets[sname]
            for line in (sys.transmission_lines_geo or []):
                merged_lines_geo.append(line.model_copy(update={
                    'from_node': line.from_node + node_off,
                    'to_node': line.to_node + node_off,
                    'from_bus': (line.from_bus + bus_off) if line.from_bus is not None else None,
                    'to_bus': (line.to_bus + bus_off) if line.to_bus is not None else None,
                }))

        # Inter-system links as transmission lines.
        # CRITICAL: also resolve a representative bus per endpoint node.
        # PowerSystemAdapter._build_transmission_lines silently skips lines
        # whose from_bus/to_bus are None (see adapters.py: "if from_bus is
        # not None and to_bus is not None"). Without explicit bus indices
        # the DC OPF never sees the inter-system edge and Cuba ↔ Isla
        # cross-system flow is silently zero.
        first_bus_of_node: dict[int, int] = {}
        for bus_idx, bus in enumerate(merged_buses):
            first_bus_of_node.setdefault(bus.parent_node, bus_idx)
        link_line_ids: set[str] = set()
        for link in electrical_links:
            if len(link.systems) < 2:
                continue
            sa, sb = link.systems[0], link.systems[1]
            if sa not in offsets or sb not in offsets:
                continue
            off_a, off_b = offsets[sa], offsets[sb]
            for idx, (na, nb) in enumerate(link.connections):
                cap = link.existing_capacity_mw[idx] if idx < len(link.existing_capacity_mw) else 0.0
                dist = link.distance_km[idx] if idx < len(link.distance_km) else 0.0
                cost_km = link.cost_per_mw_km[idx] if idx < len(link.cost_per_mw_km) else 0.0
                x_pu = link.reactance_pu[idx] if idx < len(link.reactance_pu) else None
                r_pu = link.resistance_pu[idx] if idx < len(link.resistance_pu) else None
                global_node_a = off_a + na
                global_node_b = off_b + nb
                from_bus_idx = first_bus_of_node.get(global_node_a)
                to_bus_idx = first_bus_of_node.get(global_node_b)
                # Several links between the same two systems each restart idx
                line_id = f"link_{sa}_{sb}_{idx}"
                k = 2
                while line_id in link_line_ids:
                    line_id = f"link_{sa}_{sb}_{idx}_{k}"
                    k += 1
                link_line_ids.add(line_id)
                merged_lines_geo.append(TransmissionLineGeo(
                    line_id=line_id,
                    from_node=global_node_a,
                    to_node=global_node_b,
                    from_bus=from_bus_idx,
                    to_bus=to_bus_idx,
                    capacity_mw=cap,
                    length_km=dist,
                    reactance_pu=x_pu,
                    resistance_pu=r_pu,
                    # The link's flow cost (length × cost per MWh), as in the master
                    cost_per_mw_km=max(0.0, float(cost_km)),
                ))

        # A system without per-line data is wired by its nodes_connections.
        # Once the merged network has any line, the dispatch builds its
        # network from lines only, so turn those connections into lines
        # (same reactance and R/X ratio the adjacency fallback uses).
        if merged_lines_geo:
            dc = base.dc_power_flow
            s_base = system_s_base(base)
            r_to_x = getattr(dc, 'default_r_to_x_ratio', 0.1)
            for sname, sys, nc in zip(sys_names, systems, node_counts):
                if sys.transmission_lines_geo:
                    continue
                off = offsets[sname]
                conn = sys.nodes.nodes_connections
                dist = sys.fuel_transport_distances or []
                for i in range(nc):
                    for j in range(i + 1, nc):
                        cap = max(conn[i * nc + j], conn[j * nc + i])
                        fb = first_bus_of_node.get(off + i)
                        tb = first_bus_of_node.get(off + j)
                        if cap <= 0 or fb is None or tb is None:
                            continue
                        d = float(dist[i][j]) if i < len(dist) and j < len(dist[i]) else 0.0
                        x = derived_reactance_pu(d, None, dc, s_base)
                        merged_lines_geo.append(TransmissionLineGeo(
                            line_id=f"{sname}_node_link_{i}_{j}",
                            from_node=off + i,
                            to_node=off + j,
                            from_bus=fb,
                            to_bus=tb,
                            capacity_mw=cap,
                            length_km=d,
                            reactance_pu=x,
                            resistance_pu=x * r_to_x,
                        ))

        merged_transformers: list[TransformerConfig] = []
        for sname, sys, nc in zip(sys_names, systems, node_counts):
            node_off = offsets[sname]
            bus_off = bus_offsets[sname]
            for trafo in (sys.transformers or []):
                merged_transformers.append(trafo.model_copy(update={
                    'from_node': trafo.from_node + node_off,
                    'to_node': trafo.to_node + node_off,
                    'from_bus': (trafo.from_bus + bus_off) if trafo.from_bus is not None else None,
                    'to_bus': (trafo.to_bus + bus_off) if trafo.to_bus is not None else None,
                }))

        # --- 9c. Merge gui_layout for physical bus assignment ------------
        # The operational DC-OPF anchors each generator/battery to its real
        # physical bus via Haversine snap to the nearest in-node bus
        # (_resolve_element_bus_mapping Source 3).  That needs each system's
        # per-instance lat/lon, re-keyed to the merged unit-key/node-index
        # scheme: gen keys "{unit}_n{local}" → "{sname}__{unit}_n{local+off}"
        # to match merged_gens keys ("{sname}__{unit}") and the padded
        # per-node rated_power index (local + node offset).  Bus positions are
        # re-keyed with the same per-system bus_id namespacing as the buses.
        import re as _re_gl
        merged_gui_layout: dict = {
            "generators": {}, "batteries": {}, "buses": {}, "transformers": {},
        }
        for sname, sys in zip(sys_names, systems):
            node_off = offsets[sname]
            gl = sys.gui_layout or {}
            for bid, pos in (gl.get("buses") or {}).items():
                merged_gui_layout["buses"][_uniq_bus_id(sname, bid)] = pos
            for grp in ("generators", "batteries"):
                for ekey, pos in (gl.get(grp) or {}).items():
                    m = _re_gl.match(r"^(.*)_n(\d+)$", str(ekey))
                    if m:
                        unit, ln = m.group(1), int(m.group(2))
                        nk = f"{sname}__{unit}_n{ln + node_off}"
                    else:
                        nk = f"{sname}__{ekey}"
                    merged_gui_layout[grp][nk] = pos

        # --- 9b. Merge fuel infrastructure (entries, storage, pipelines, NE demand) ----
        # Per-system fuel_entry_points / fuel_infrastructure use *local* node
        # indices (0..nc-1 inside each system). The merged operational/master
        # configs live in a single namespace 0..total_nodes-1, so we must shift
        # each entry's `node` (and pipelines' `from_node`/`to_node`) by the
        # system offset; otherwise PrimaryEnergyAdapter sees an empty fuel
        # network for everything but the first system and every thermal plant
        # ends up CF=0 → ~70% load shed in multi-system runs.
        from esfex.config.schema import (
            FuelInfrastructureConfig as _FIC,
            FuelEntryPointConfig as _FEPC,
            NonElectricDemandConfig as _NEDC,
        )
        merged_fuel_entries: list[_FEPC] = []
        merged_storage: dict[str, dict] = {}
        merged_pipelines: dict[str, dict] = {}
        merged_ne_demand: dict[str, _NEDC] = {}
        merged_ne_growth: dict[str, float] = {}
        for sname, sys in zip(sys_names, systems):
            sys_off = offsets[sname]
            for fe in (sys.fuel_entry_points or []):
                shifted = fe.model_copy(deep=True)
                shifted.node = fe.node + sys_off
                merged_fuel_entries.append(shifted)
            fi = sys.fuel_infrastructure
            if fi is not None:
                for fac_key, fac_data in (fi.storage_facilities or {}).items():
                    new_fac = dict(fac_data) if isinstance(fac_data, dict) else dict(fac_data.__dict__)
                    if 'node' in new_fac:
                        new_fac['node'] = int(new_fac['node']) + sys_off
                    merged_storage[f"{sname}__{fac_key}"] = new_fac
                for route_key, route_data in (fi.transport_pipelines or {}).items():
                    new_route = dict(route_data) if isinstance(route_data, dict) else dict(route_data.__dict__)
                    for k in ('from_node', 'to_node', 'node'):
                        if k in new_route and new_route[k] is not None:
                            new_route[k] = int(new_route[k]) + sys_off
                    merged_pipelines[f"{sname}__{route_key}"] = new_route
            for fuel_name, ne_cfg in (sys.non_electric_demand or {}).items():
                if fuel_name not in merged_ne_demand:
                    merged_ne_demand[fuel_name] = ne_cfg.model_copy(deep=True)
                    merged_ne_demand[fuel_name].demand = [0] * total_nodes
                src = ne_cfg.demand or []
                tgt = merged_ne_demand[fuel_name].demand
                for local_n, val in enumerate(src):
                    g = local_n + sys_off
                    if 0 <= g < total_nodes:
                        tgt[g] = val
            for fuel_name, gr in (sys.non_electric_demand_growth or {}).items():
                merged_ne_growth.setdefault(fuel_name, gr)
        merged_fuel_infra = _FIC(
            storage_facilities=merged_storage,
            transport_pipelines=merged_pipelines,
        )

        # --- 9d. Remaining system-level settings -----------------------------
        # Everything here used to fall back to schema defaults in the merged
        # config, so any run with more than one node silently lost converters,
        # electrolyzers, fuel sources, custom constraints, scheduled
        # interruptions and the frequency / reserve / storage scalars. Node and
        # bus indices are shifted like the buses above; equipment keys follow
        # the merged ``{system}__{key}`` scheme of generators and batteries.
        from esfex.config.schema import ElectrolyzerConfig, PrimaryEnergySourceConfig

        # Scalars: first system wins, as for discount_rate & co. below.
        base_scalars = {f: getattr(base, f) for f in (
            "frequency_nominal", "frequency_nadir_limit", "rocof_limit",
            "load_damping", "reserve_margin", "reserve_static_default_ratio",
            "dynamic_reserve_contribution", "soc_end_tolerance",
            "min_cycling_ratio", "min_cycling_period_days",
            "demand_shift_cost_rate",
            "npv_annual_return_rate",
        )}

        def _shift_ends(obj, node_off: int, bus_off: int):
            upd = {"from_node": obj.from_node + node_off,
                   "to_node": obj.to_node + node_off}
            for k in ("from_bus", "to_bus"):
                v = getattr(obj, k)
                upd[k] = v + bus_off if v is not None else None
            return obj.model_copy(update=upd)

        merged_acdc: list = []
        merged_freq: list = []
        for sname, sys in zip(sys_names, systems):
            node_off, bus_off = offsets[sname], bus_offsets[sname]
            merged_acdc.extend(_shift_ends(c, node_off, bus_off)
                               for c in (sys.acdc_converters or []))
            merged_freq.extend(_shift_ends(c, node_off, bus_off)
                               for c in (sys.freq_converters or []))

        def _pad_node_lists(obj, model, si: int):
            """Copy of *obj* with every per-node list placed at system *si*."""
            upd = {}
            for f in model.model_fields:
                val = getattr(obj, f)
                if isinstance(val, list) and len(val) in (1, node_counts[si]):
                    is_int = f in ("life_time", "initial_age")
                    upd[f] = _pad_int(val, si) if is_int else _pad(val, si)
            return obj.model_copy(update=upd)

        merged_electrolyzers: dict[str, ElectrolyzerConfig] = {}
        for si, (sname, sys) in enumerate(zip(sys_names, systems)):
            for ekey, elz in (sys.electrolyzers or {}).items():
                merged_electrolyzers[f"{sname}__{ekey}"] = _pad_node_lists(
                    elz, ElectrolyzerConfig, si)

        # Fuel sources are keyed by fuel, so systems sharing a fuel fill their
        # own node range of one entry; scalar fields come from the first.
        _pe_node_fields = ("max_availability", "import_cost",
                           "storage_capacity", "initial_storage_level")
        merged_pe: dict[str, PrimaryEnergySourceConfig] = {}
        for si, (sname, sys) in enumerate(zip(sys_names, systems)):
            off, nc = offsets[sname], node_counts[si]
            for fuel, pe in (sys.primary_energy_sources or {}).items():
                if fuel not in merged_pe:
                    merged_pe[fuel] = pe.model_copy(update={
                        f: [0.0] * total_nodes for f in _pe_node_fields})
                for f in _pe_node_fields:
                    padded = _pad(list(getattr(pe, f)), si)
                    getattr(merged_pe[fuel], f)[off:off + nc] = padded[off:off + nc]

        merged_ftd: list[list[float]] = []
        if any(s.fuel_transport_distances for s in systems):
            merged_ftd = [[0.0] * total_nodes for _ in range(total_nodes)]
            for sname, sys, nc in zip(sys_names, systems, node_counts):
                off = offsets[sname]
                for i, row in enumerate((sys.fuel_transport_distances or [])[:nc]):
                    for j, v in enumerate(row[:nc]):
                        merged_ftd[off + i][off + j] = float(v)

        merged_routes: list[dict] = []
        for sname, sys in zip(sys_names, systems):
            off = offsets[sname]
            for route in (sys.fuel_transport_routes or []):
                r = dict(route)
                for k in ("from_node", "to_node"):
                    if r.get(k) is not None:
                        r[k] = int(r[k]) + off
                merged_routes.append(r)
        merged_routes.extend(_fuel_link_routes(config.meta_network.systems_links, offsets))

        merged_conv_techs: dict = {}
        merged_seasonal: dict = {}
        merged_gui_techs: dict = {}
        for sname, sys in zip(sys_names, systems):
            for ckey, ct in (sys.conversion_technologies or {}).items():
                merged_conv_techs[ckey] = ct.model_copy(update={"units": {
                    f"{sname}__{u}": frac for u, frac in ct.units.items()}})
            merged_seasonal.update(sys.seasonal_factors or {})
            merged_gui_techs.update(sys.gui_technologies or {})

        # Custom constraints name equipment by key and address buses, nodes
        # and lines by integer index; both must follow the merge. Index
        # positions per variable mirror ``resolve_term`` in user_constraints.jl.
        _cc_name_axes = {
            "gen_output": {0}, "bat_charge": {0}, "bat_discharge": {0},
            "bat_soc": {0}, "tech_investment": {1},
            "bat_tech_power_investment": {1},
            "bat_tech_capacity_investment": {1},
        }
        _cc_node_axes = {
            "curtailment": {0}, "tech_investment": {2},
            "bat_tech_power_investment": {2},
            "bat_tech_capacity_investment": {2},
            "transfer_investment": {1, 2},
        }
        _cc_bus_axes = {"load_shed": {0}, "power_flow": {0, 1}}

        def _remap_term(term, sname: str):
            index = []
            for pos, entry in enumerate(term.index):
                if entry == "all" or entry == -1:
                    index.append(entry)
                elif pos in _cc_name_axes.get(term.variable, ()):
                    index.append(f"{sname}__{entry}")
                elif pos in _cc_node_axes.get(term.variable, ()):
                    index.append(int(entry) + offsets[sname])
                elif pos in _cc_bus_axes.get(term.variable, ()):
                    index.append(int(entry) + bus_offsets[sname])
                else:
                    index.append(entry)
            return term.model_copy(update={"index": index})

        merged_custom: list = []
        for sname, sys in zip(sys_names, systems):
            for cc in (sys.custom_constraints or []):
                merged_custom.append(cc.model_copy(update={
                    "name": f"{sname}__{cc.name}" if multi_sys else cc.name,
                    "terms": [_remap_term(t, sname) for t in cc.terms],
                }))

        # Interruptions address generators, batteries and electrolyzers by
        # key (renamed above), lines / transformers / converters / fuel routes
        # by their own id (kept) and fuel tanks by fuel and node: a tank
        # window without a node covers only its own system's nodes.
        _outage_keyed = {"generator", "battery", "electrolyzer"}
        merged_outages: list = []
        for sname, sys, nc in zip(sys_names, systems, node_counts):
            off = offsets[sname]
            for ow in (sys.outage_schedule or []):
                if ow.element_type in _outage_keyed:
                    ow = ow.model_copy(update={
                        "element_id": f"{sname}__{ow.element_id}",
                        "node": None if ow.node is None else ow.node + off})
                elif ow.element_type == "fuel_storage":
                    nodes = range(nc) if ow.node is None else [ow.node]
                    merged_outages.extend(ow.model_copy(update={"node": n + off})
                                          for n in nodes)
                    continue
                merged_outages.append(ow)

        # Development zones: technology and unit keys follow the merged
        # ``{system}__{key}`` scheme; a target bus is shifted like the buses.
        merged_zones: list = []
        for sname, sys in zip(sys_names, systems):
            for zone in (sys.development_zones or []):
                tb = zone.target_bus
                if tb is not None:
                    if sys.buses:
                        tb = tb + bus_offsets[sname]
                    elif not merged_buses:
                        tb = tb + offsets[sname]   # one implicit bus per node
                    else:
                        tb = None
                merged_zones.append(zone.model_copy(update={
                    "name": f"{sname}__{zone.name}" if multi_sys else zone.name,
                    "target_bus": tb,
                    "allowed_generators": (
                        [f"{sname}__{k}" for k in zone.allowed_generators]
                        if zone.allowed_generators is not None else None),
                    "allowed_technologies": (
                        {f"{sname}__{k}": v for k, v in zone.allowed_technologies.items()}
                        if zone.allowed_technologies is not None else None),
                }))

        # --- 10. Build merged SystemConfigs --------------------------------
        # Common kwargs shared by both master (node-level) and operational
        # (bus-level) versions of the merged config.
        common_kwargs = dict(
            name="_".join(sys_names),
            demand_path=None,
            demand_scale=1.0,
            life_extension_cost_factor=base.life_extension_cost_factor,
            sim_rooftop=any(s.sim_rooftop for s in systems),
            target_re_penetration=min(s.target_re_penetration for s in systems),
            min_annual_increment=min(s.min_annual_increment for s in systems),
            max_annual_increment=max(s.max_annual_increment for s in systems),
            max_annual_system_cost=sum(s.max_annual_system_cost for s in systems),
            max_npv_penalty_per_mw=base.max_npv_penalty_per_mw,
            max_decommission_cost_per_mw=base.max_decommission_cost_per_mw,
            force_replacement=base.force_replacement,
            discount_rate=base.discount_rate,
            base_lcoe=base.base_lcoe,
            inertia_limit_threshold=base.inertia_limit_threshold,
            power_flow_mode=base.power_flow_mode,
            dc_power_flow=base.dc_power_flow,
            ac_power_flow=base.ac_power_flow,
            nodes=merged_nodes,
            generators=merged_gens,
            batteries=merged_bats,
            technologies=merged_techs,
            battery_technologies=merged_bat_techs,
            fuels=merged_fuels,
            penalties=base.penalties.__class__(**merged_penalties_d),
            co2_budget=merged_co2_budget,
            co2_budget_by_system=co2_by_system,
            criticality_penalties=base.criticality_penalties,
            ev_initial_soc=merged_ev_soc,
            ev_categories=merged_ev_categories,
            ev_quantity=merged_ev_quantity,
            base_patterns=merged_base_patterns,
            electric_demand=merged_electric_demand,
            sector_distribution=merged_sector_dist,
            rooftop_max_potential=merged_rooftop_potential,
            rooftop_solar_config=merged_rooftop_solar_config,
            rooftop_solar_emission_reduction=base.rooftop_solar_emission_reduction,
            stochastic_scenarios=base.stochastic_scenarios,
            fuel_entry_points=merged_fuel_entries,
            fuel_infrastructure=merged_fuel_infra,
            non_electric_demand=merged_ne_demand,
            non_electric_demand_growth=merged_ne_growth,
            acdc_converters=merged_acdc,
            freq_converters=merged_freq,
            electrolyzers=merged_electrolyzers,
            primary_energy_sources=merged_pe,
            fuel_transport_distances=merged_ftd,
            fuel_transport_routes=merged_routes,
            conversion_technologies=merged_conv_techs,
            seasonal_factors=merged_seasonal,
            custom_constraints=merged_custom,
            outage_schedule=merged_outages,
            development_zones=merged_zones,
            **base_scalars,
        )

        # Master problem config: node-level network from adjacency matrix.
        # Generate synthetic transmission lines so the master problem DC-OPF
        # can transfer power between nodes (without this, each node is isolated
        # and load shedding dominates the objective).
        # Each link carries the equivalent impedance of the bus-level lines it
        # stands for, so the master's flow split and losses (loss_model)
        # follow the network the dispatch sees.
        master_lines: list[TransmissionLineGeo] = []
        link_z = _node_link_impedances(merged_lines_geo, merged_buses, base.dc_power_flow,
                                       system_s_base(base))
        link_flow_cost = _link_flow_costs(electrical_links, offsets)
        line_idx = 0
        for i in range(total_nodes):
            for j in range(i + 1, total_nodes):
                cap = merged_adj[i * total_nodes + j]
                if cap > 0:
                    # Master uses one bus per node (num_buses == num_nodes),
                    # so from_bus = from_node and to_bus = to_node.
                    # Without these, convert_transmission_line_data drops
                    # the synthetic node-link → master sees disconnected
                    # nodes and invests as if each were an autonomous island.
                    master_lines.append(TransmissionLineGeo(
                        line_id=f"node_link_{i}_{j}",
                        from_node=i,
                        to_node=j,
                        from_bus=i,
                        to_bus=j,
                        capacity_mw=cap,
                        resistance_pu=link_z[(i, j)][0] if (i, j) in link_z else None,
                        reactance_pu=link_z[(i, j)][1] if (i, j) in link_z else None,
                        # Inter-system link: its flow cost per MWh
                        **link_flow_cost.get((i, j), {}),
                    ))
                    line_idx += 1

        # The master has one bus per node: a converter joins its nodes, not
        # the dispatch-network buses
        def _node_ends(convs):
            return [c.model_copy(update={"from_bus": None, "to_bus": None}) for c in convs]

        merged_master = SystemConfig(**{**common_kwargs,
            "acdc_converters": _node_ends(merged_acdc),
            "freq_converters": _node_ends(merged_freq)},
            transmission_lines_geo=master_lines,
        )

        # Operational config: full bus-level network for DC-OPF
        merged_operational = SystemConfig(
            **common_kwargs,
            buses=merged_buses,
            transmission_lines_geo=merged_lines_geo,
            transformers=merged_transformers,
        )
        # Physical bus assignment data (geographic snap source).  Set
        # post-construction to bypass the ``_gui_layout`` pydantic alias.
        merged_operational.gui_layout = merged_gui_layout
        # Studio technology catalog: the fuel-cost fallback of the derived
        # metrics reads it. Same alias workaround (``_technologies``).
        if merged_gui_techs:
            merged_master.gui_technologies = merged_gui_techs
            merged_operational.gui_technologies = merged_gui_techs

        logger.info(
            f"Merged system: {total_nodes} nodes, "
            f"{len(merged_gens)} generators, {len(merged_bats)} batteries, "
            f"{len(merged_techs)} technologies, {len(merged_bat_techs)} battery technologies, "
            f"master network: {len(master_lines)} node-links, "
            f"operational network: {len(merged_buses)} buses, "
            f"{len(merged_lines_geo)} lines, {len(merged_transformers)} transformers"
        )

        return merged_master, merged_operational, offsets

    def run(
        self,
        years: Optional[int] = None,
        start_year: Optional[int] = None,
        master_only: bool = False,
        dispatch_years: Optional[Iterable[int]] = None,
    ) -> list[YearResults]:
        """
        Run the full simulation.

        The workflow follows the original ESFEX architecture:
        1. STEP 1: Solve MasterProblem ONCE for all years (capacity expansion planning)
        2. STEP 2: Run operational subproblems year-by-year using MasterProblem decisions

        Args:
            years: Number of years to simulate (default: ``DEFAULT_RUN_YEARS`` = 25)
            start_year: Label of the first simulated year. Default: the
                year of ``date_start``. When given, the date labels start
                on ``date_start``'s day and time in this year. It only
                relabels: the data are still read from the first row, and
                the Feb 29 rows dropped are those of the data's own calendar
                (``date_start``).
            master_only: Stop after step 1 (development mode only). The
                decisions are left in ``self.master_solution`` (None if the
                master problem failed) and no year results are returned.
            dispatch_years: Run the operational dispatch only for these years
                of the horizon (labels); the others are skipped, while every
                year keeps its own data block (year index x 8760 h) and the
                master problem still plans the whole horizon. In
                ``unit_commitment`` mode the first of them is dispatched
                instead of the first horizon year. Default: every year (or
                the ``ESFEX_OPS_ONLY_YEARS`` environment variable).

        Returns:
            List of YearResults for each simulated year
        """
        if master_only and self.config.simulation_mode != "development":
            raise ValueError(
                "master_only needs simulation_mode 'development' "
                f"(got {self.config.simulation_mode!r})")
        self.master_solution = None
        years = years or DEFAULT_RUN_YEARS
        self._start_date = self._resolve_start_date(start_year)
        start_year = self._start_date.year
        end_year = start_year + years
        years_range = list(range(start_year, end_year))

        # Years that run the operational dispatch (None: all of them).
        if dispatch_years is not None:
            ops_only = {int(y) for y in dispatch_years}
        else:
            env_years = os.environ.get("ESFEX_OPS_ONLY_YEARS", "").strip()
            ops_only = ({int(y) for y in env_years.replace(",", " ").split() if y}
                        if env_years else None)
            if ops_only:
                logger.warning(
                    f"[DEBUG] ESFEX_OPS_ONLY_YEARS={sorted(ops_only)} "
                    f"— skipping all other operational years")
        if ops_only is not None:
            unknown = sorted(ops_only - set(years_range))
            if unknown or not ops_only:
                raise ValueError(
                    f"dispatch_years {unknown or '[]'} outside the horizon "
                    f"{years_range[0]}-{years_range[-1]}")

        # unit_commitment mode is a single-shot dispatch over the first
        # ``unit_commitment_hours`` hours of one year; iterating the full
        # planning horizon would repeat the same short window N times.
        # Dispatch one year only: the first horizon year, or the first of
        # ``dispatch_years`` (years before it keep their data blocks).
        # (development mode plans the whole horizon — unchanged.)
        if self.config.simulation_mode == "unit_commitment":
            uc_year = min(ops_only) if ops_only else years_range[0]
            years_range = years_range[:years_range.index(uc_year) + 1]
            ops_only = {uc_year}
            end_year = years_range[-1] + 1
        run_years = [y for y in years_range if ops_only is None or y in ops_only]

        logger.info(f"Starting simulation: {run_years[0]} to {run_years[-1] + 1}")
        start_time = time.time()

        # Plugin hook: pre_simulation
        self._pm.call_hook("pre_simulation", config=self.config, output_dir=self.output_dir)

        # ============================================================
        # LOAD ALL DATA FOR FULL HORIZON
        # ============================================================
        logger.debug("Loading data...")

        # Load base demand for ALL years
        base_demand, total_hours, num_nodes, years_list, time_index = self._load_demand(
            years=years,
            start_year=start_year
        )
        logger.debug(f"Loaded base demand: {total_hours} hours, {num_nodes} nodes")

        # Generate EV demand profiles for ALL years (with S-curve growth)
        ev_demand, ev_charging_profiles, v2g_availability_profiles = self._generate_ev_demand(
            num_nodes=num_nodes,
            total_hours=total_hours,
            base_year=start_year,
            target_year=end_year,
        )

        # Total demand: when EV optimization is enabled, ev_charging in the Julia
        # power balance already handles EV demand, so do NOT add it to base_demand
        # (otherwise EV demand is double-counted: once in demand array, once as ev_charging variable)
        sys = self.primary_system
        ev_optimization_enabled = (
            hasattr(sys, 'ev_categories') and sys.ev_categories
            and ev_charging_profiles is not None
        )
        if ev_optimization_enabled:
            total_demand = base_demand
            logger.debug(
                f"EV optimization enabled: base demand = {np.sum(base_demand):.0f} MWh, "
                f"EV demand handled by optimizer = {np.sum(ev_demand):.0f} MWh"
            )
        else:
            total_demand = base_demand + ev_demand
            logger.debug(f"Total demand (base + EV): {np.sum(total_demand):.0f} MWh")

        # Plugin hook: post_demand_loaded (plugins may return modified demand)
        demand_overrides = self._pm.call_hook(
            "post_demand_loaded",
            base_demand=base_demand,
            ev_demand=ev_demand,
            total_demand=total_demand,
            config=self.config,
        )
        if demand_overrides:
            total_demand = demand_overrides[-1]
            logger.debug(f"Plugin modified total demand: {np.sum(total_demand):.0f} MWh")

        # Create sectoral demand from total demand (including EV)
        sectoral_demand = self._create_sectoral_demand(total_demand)
        if sectoral_demand:
            logger.debug(f"Created sectoral demand for {len(sectoral_demand)} sectors")

        # Store for later use
        self._base_demand = base_demand
        self._ev_demand = ev_demand
        self._total_demand = total_demand
        self._sectoral_demand = sectoral_demand
        self._ev_charging_profiles = ev_charging_profiles
        self._v2g_availability_profiles = v2g_availability_profiles
        self._time_index = time_index
        self._total_hours = total_hours

        # Use first year's hours for single-year calculations.
        # In unit_commitment mode the user controls how many hours of the
        # year to dispatch via ``unit_commitment_hours`` — without this
        # cap the UC mode would expand to the full 8760-hour year and
        # ignore the field entirely. Development mode always uses the
        # full year because the master plans 8760 h.
        hours_per_year = min(HOURS_STD_YEAR, total_hours)
        if self.config.simulation_mode == "unit_commitment":
            uc_hours = int(getattr(self.config, "unit_commitment_hours", hours_per_year))
            uc_hours = max(1, min(uc_hours, hours_per_year))
            if uc_hours < hours_per_year:
                logger.info(
                    f"UC mode: capping dispatch horizon to "
                    f"unit_commitment_hours={uc_hours} (was {hours_per_year})"
                )
                hours_per_year = uc_hours
        hours = hours_per_year

        logger.debug(f"Stored demand data: base={np.sum(base_demand):.0f} MWh, "
                    f"EV={np.sum(ev_demand):.0f} MWh, total={np.sum(total_demand):.0f} MWh")

        # Build ordered generator/battery name lists (matching Julia solver index order).
        # _gen_fuels is the parallel fuel list: written to each generation
        # dataset as ``ds.attrs["fuel"]`` so the dashboard can bucket by
        # fuel directly (no name-parsing). The config's ``technology``
        # field is unreliable here (orphan IDs like ``tech_5`` that don't
        # resolve in the ``technologies`` dict), so we rely on fuel only.
        self._gen_names = unique_unit_names([
            gen.name if hasattr(gen, 'name') else key
            for key, gen in self.primary_system.generators.items()
        ])
        self._gen_fuels = [
            (getattr(gen, 'fuel', None) or '') or ''
            for _, gen in self.primary_system.generators.items()
        ]
        self._bat_names = unique_unit_names([
            bat.name if hasattr(bat, 'name') else key
            for key, bat in self.primary_system.batteries.items()
        ])

        # Preload all availability profiles ONCE (cache for operational phase)
        self._availability_cache = self._preload_availability_profiles(num_nodes)
        logger.debug(f"Preloaded {len(self._availability_cache)} availability profiles")
        self._inflow_cache = self._preload_inflow_profiles(num_nodes)
        self._num_nodes = num_nodes

        # Build bus-to-node mapping for aggregating bus-level results to
        # node-level. Needed whenever the bus list is not the identity mapping
        # over nodes — including the FEWER-buses-than-nodes case a reduced
        # network produces, which the old ``len(buses) > num_nodes`` test
        # missed, leaving bus-indexed results exported as if node-indexed
        # (the Result Viewer then read 289 buses as 289 hours ≈ 13 days).
        from esfex.bridge.converters import node_bus_mapping_required
        sys_cfg = self.primary_system
        if sys_cfg.buses and node_bus_mapping_required(sys_cfg.buses, num_nodes):
            self._bus_to_node = [bus.parent_node for bus in sys_cfg.buses]
            self._num_buses = len(sys_cfg.buses)
        else:
            self._bus_to_node = None
            self._num_buses = num_nodes

        # ── Zone expansion: add virtual nodes for development zones ──
        self._zone_mappings = []
        if self.primary_system.development_zones:
            expanded_sys, self._zone_mappings = expand_config_with_zones(
                self.primary_system,
            )
            if self.master_system is self.primary_system:
                self.master_system = expanded_sys
            else:
                # The master network is node-level (one bus per node), so a
                # zone's target bus becomes that bus's node there. Zones get
                # the same virtual node indices in both networks.
                op_buses = self.primary_system.buses or []
                master_zones = [
                    z.model_copy(update={"target_bus": (
                        op_buses[z.target_bus].parent_node
                        if z.target_bus is not None and z.target_bus < len(op_buses)
                        else None)})
                    for z in self.primary_system.development_zones
                ]
                self.master_system, _ = expand_config_with_zones(
                    self.master_system.model_copy(
                        update={"development_zones": master_zones}),
                )
            self.primary_system = expanded_sys

            num_zone_nodes = len(self._zone_mappings)
            old_num_nodes = num_nodes
            num_nodes = expanded_sys.nodes.num_nodes
            self._num_nodes = num_nodes

            # The zone buses join the bus list: refresh the bus → node map.
            if expanded_sys.buses and node_bus_mapping_required(expanded_sys.buses, num_nodes):
                self._bus_to_node = [bus.parent_node for bus in expanded_sys.buses]
                self._num_buses = len(expanded_sys.buses)
            else:
                self._bus_to_node = None
                self._num_buses = num_nodes

            # Expand demand arrays with zero columns for zone nodes
            zero_cols = np.zeros((total_demand.shape[0], num_zone_nodes))
            total_demand = np.hstack([total_demand, zero_cols])
            base_demand = np.hstack([base_demand, zero_cols])
            ev_demand = np.hstack([ev_demand, zero_cols])
            self._total_demand = total_demand
            self._base_demand = base_demand
            self._ev_demand = ev_demand

            # Copy availability profiles for zone nodes from nearest bus's parent node
            for m in self._zone_mappings:
                ref_node = m.nearest_bus_parent_node
                for cache_key, cache_data in list(self._availability_cache.items()):
                    if cache_data.ndim == 2 and cache_data.shape[1] == old_num_nodes:
                        # Extend with reference node's column
                        ref_col = cache_data[:, ref_node:ref_node + 1]
                        self._availability_cache[cache_key] = np.hstack(
                            [cache_data, ref_col],
                        )

            logger.debug(
                f"Zone expansion: {old_num_nodes} → {num_nodes} nodes "
                f"({num_zone_nodes} zones: "
                f"{', '.join(m.zone_name for m in self._zone_mappings)})"
            )

        # ── Rooftop solar generation ──
        self._rooftop_generation = None  # (total_hours × num_nodes) or None
        if self.primary_system.sim_rooftop and self.primary_system.rooftop_solar_config:
            self._rooftop_generation = self._generate_rooftop_solar(
                num_nodes=num_nodes,
                total_hours=total_hours,
                years_range=years_range,
                start_year=start_year,
            )
            if self._rooftop_generation is not None:
                logger.debug(
                    f"Rooftop solar: peak={np.max(np.sum(self._rooftop_generation, axis=1)):.1f} MW, "
                    f"total={np.sum(self._rooftop_generation):.0f} MWh"
                )

        # Initialize units configuration
        units_config = self._gather_units()

        # Initialize simulation state
        self.state = SimulationState(
            year=start_year,
            base_year=start_year,
            units_config=units_config,
        )

        # Initialize HDF5 output file
        # Build results filename: <config_stem>_<ddmmyyyy>_<HHMM>.h5
        _cfg_stem = self.config_path.stem if self.config_path else self.system_name
        _now = datetime.now()
        _ts = _now.strftime("%d%m%Y_%H%M")
        hdf5_path = self.output_dir / f"{_cfg_stem}_{_ts}.h5"
        # Stash on self so deeper methods (MGA export inside the master
        # problem solver) can reach the same file without threading the
        # path through several function signatures.
        self._hdf5_path = hdf5_path
        self._initialize_hdf5(
            hdf5_path,
            num_nodes=num_nodes,
            # Years actually dispatched (unit_commitment runs only one)
            num_years=len(run_years),
            start_year=run_years[0],
            end_year=run_years[-1] + 1,
        )

        # ============================================================
        # STEP 1: SOLVE MASTER PROBLEM (only in development mode)
        # ============================================================
        master_investments = {}
        master_retirements = {}
        master_cumulative_caps = {}
        master_re_targets = {}

        if self.config.simulation_mode == "development":
            logger.info("Step 1: Solving Master Investment Problem for ALL years")
            self._pm.call_hook("pre_master_problem", config=self.config, years=years_range)

            with console.status("[bold blue]Solving Master Problem...[/bold blue]") as status:
                # The master has no EV model: it plans with base + EV charging
                # demand. (With EV optimization total_demand leaves EV out,
                # since the dispatch serves it through ev_charging.)
                master_total = total_demand + ev_demand if ev_optimization_enabled else total_demand
                # Use first year as reference for demand growth calculation
                first_year_demand = self._extract_year_demand(master_total, 0, hours_per_year)

                master_result = self._solve_master_problem(
                    years_range=years_range,
                    demand=first_year_demand,  # First year demand as base
                    hours=hours_per_year,
                    num_nodes=num_nodes,
                    ev_demand=ev_demand,
                    total_demand=master_total,
                )
                if master_result is None or master_result[0] is None:
                    master_investments = None
                    master_re_targets = {}
                else:
                    master_investments, master_retirements, master_cumulative_caps, master_re_targets = master_result

            if master_investments is None:
                logger.error("Master problem failed. Aborting simulation.")
                console.print("[bold red]Master problem failed. Aborting.[/bold red]")
                return []

            total_inv = sum(len(v) for v in master_investments.values())
            total_ret = sum(len(v) for v in master_retirements.values())
            console.print(f"[green]Master Problem solved[/green] ({total_inv} investments, {total_ret} retirements)")
            logger.info(f"Master problem solved for {len(years_range)} years")

            self._pm.call_hook(
                "post_master_problem",
                investments=master_investments,
                retirements=master_retirements,
                config=self.config,
            )
            if master_only:
                self.master_solution = {
                    "investments": master_investments,
                    "retirements": master_retirements,
                    "cumulative_capacities": master_cumulative_caps,
                    "re_targets": master_re_targets,
                }
                self._finalize_hdf5(hdf5_path)
                self._pm.teardown_all()
                return []
        else:
            logger.info(
                f"{self.config.simulation_mode} mode: skipping Master Problem")

        # ============================================================
        # STEP 2: RUN OPERATIONAL SUBPROBLEMS YEAR BY YEAR
        # ============================================================
        logger.info("Step 2: Running operational subproblems year by year")

        cumulative_units_config = deepcopy(units_config)
        num_years = len(years_range)

        # When stdout is a pipe (not a terminal) the consumer is most
        # likely the GUI subprocess in python_console.py. Rich's Live
        # renders thousands of progress frames; a final flush on
        # `__exit__` can overflow the pipe buffer (BlockingIOError on
        # fds left in O_NONBLOCK by juliacall). `transient=True` tells
        # Rich to clear the live region on exit, which is both nicer
        # for the GUI console and avoids the heavy final paint.
        import sys as _sys
        _is_tty = False
        try:
            _is_tty = bool(_sys.stdout.isatty())
        except (AttributeError, OSError, ValueError):
            pass
        with Progress(
            SpinnerColumn("line"),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=not _is_tty,
        ) as progress:
            self._progress = progress
            year_task = progress.add_task(
                f"[cyan]Operational Dispatch[/cyan]",
                total=len(run_years)
            )
            window_task = progress.add_task(
                "[dim]Windows[/dim]", total=1, visible=False
            )

            for year_idx, year in enumerate(years_range):
                if ops_only is not None and year not in ops_only:
                    # The master's builds are per-year increments: a
                    # skipped year's must still reach the later years.
                    self._apply_master_builds(master_investments.get(year, {}))
                    continue
                logger.debug(f"Year {year} ({year_idx+1}/{num_years})")
                self.state.year = year

                # Update progress description
                progress.update(year_task, description=f"[cyan]Year {year}[/cyan] ({year_idx+1}/{num_years})")

                try:
                    # Get this year's investment decisions from MasterProblem
                    year_investments = master_investments.get(year, {})

                    # Apply transmission investments to network adjacency matrix
                    self._apply_master_builds(year_investments)

                    # Build cumulative_units_config from MasterProblem's cumulative
                    # capacities. This correctly handles:
                    # - Existing units: age-based retirement + degradation
                    # - Investments: each with its own age tracking (NOT retired
                    #   when the original unit retires)
                    year_caps = master_cumulative_caps.get(year, {})
                    if year_caps:
                        cumulative_units_config = self._build_config_from_cumulative(
                            units_config, year_caps
                        )

                    # Rebuild gen/bat name lists to include virtual units
                    # (must happen BEFORE HDF5 export so all generators are captured)
                    self._rebuild_unit_names(cumulative_units_config)

                    # Log capacity state for debugging
                    cap_summary = []
                    for ukey, udata in cumulative_units_config.items():
                        name = udata.get("name", ukey)
                        if udata.get("_type") == "battery" or udata.get("type") == "Storage":
                            # Battery: show power and energy capacity
                            charge_pow = udata.get("MaxChargePower", [])
                            cap_mwh = udata.get("capacity", [])
                            total_pow = sum(charge_pow) if charge_pow else 0
                            total_cap = sum(cap_mwh) if cap_mwh else 0
                            if total_pow > 0.01 or total_cap > 0.01:
                                cap_summary.append(f"{name}={total_pow:.1f}MW/{total_cap:.1f}MWh")
                        else:
                            rp = udata.get("rated_power", [])
                            total = sum(rp) if rp else 0
                            if total > 0.01:
                                cap_summary.append(f"{name}={total:.1f}MW")
                    logger.debug(f"Year {year} capacity: {', '.join(cap_summary)}")
                    if year_investments:
                        logger.debug(f"  Investments: {year_investments}")

                    # Extract year demand from total_demand (base + EV)
                    y_idx = year - start_year
                    year_demand = self._extract_year_demand(
                        total_demand, y_idx, hours_per_year
                    )
                    logger.debug(f"Year {year} demand: {np.sum(year_demand):.0f} MWh "
                                f"(peak: {np.max(np.sum(year_demand, axis=1)):.0f} MW)")

                    # Plugin hook: pre_year
                    self._pm.call_hook(
                        "pre_year",
                        year=year,
                        year_idx=year_idx,
                        units_config=cumulative_units_config,
                        config=self.config,
                    )

                    # Store current units config for derived metrics computation
                    self._current_units_config = cumulative_units_config

                    # Log virtual unit capacities (investment technologies) at INFO level
                    vgen_info = []
                    vbat_info = []
                    for ukey, udata in cumulative_units_config.items():
                        if ukey.startswith("inv_"):
                            name = udata.get("name", ukey)
                            if udata.get("_type") == "battery" or udata.get("type") == "Storage":
                                pow_sum = sum(udata.get("MaxChargePower", []))
                                cap_sum = sum(udata.get("capacity", []))
                                if pow_sum > 0.1 or cap_sum > 0.1:
                                    vbat_info.append(f"{name}={pow_sum:.0f}MW/{cap_sum:.0f}MWh")
                            else:
                                rp_sum = sum(udata.get("rated_power", []))
                                if rp_sum > 0.1:
                                    vgen_info.append(f"{name}={rp_sum:.0f}MW")
                    if vgen_info or vbat_info:
                        logger.info(f"Year {year} investments: {', '.join(vgen_info + vbat_info)}")

                    # Run operational dispatch with rolling horizon
                    # Pass year-specific RE target (not the final target)
                    year_re_target = master_re_targets.get(year, 0.0)
                    # Cap at what the fixed plan can physically deliver. The
                    # master's target is an aspirational ramp the least-cost
                    # plan does not meet until fossil retires; the operational
                    # cannot build capacity, so an un-capped target only creates
                    # a phantom fre_penetration slack (see _estimate_achievable_re).
                    if year_re_target > 0:
                        achievable_re = self._estimate_achievable_re(
                            cumulative_units_config, year_demand, year_idx=y_idx
                        )
                        capped = min(year_re_target, achievable_re)
                        if capped < year_re_target - 1e-4:
                            logger.info(
                                f"Year {year} RE target capped: master={year_re_target:.1%} "
                                f"→ achievable={achievable_re:.1%} (plan capacity-limited)"
                            )
                        year_re_target = capped
                    logger.info(f"Year {year} RE target (operational): {year_re_target:.1%}")
                    year_result = self._run_operational_dispatch(
                        year=year,
                        year_idx=year_idx,
                        num_years=num_years,
                        demand=year_demand,
                        hours=len(year_demand),
                        num_nodes=num_nodes,
                        units_config=cumulative_units_config,
                        re_penetration_target=year_re_target if year_re_target > 0 else None,
                    )
                    year_result.investments = year_investments
                    year_result.retirements = master_retirements.get(year, {})
                    year_result.master_re_target = year_re_target

                    self.results.append(year_result)

                    # Export year results to HDF5
                    self._append_year_to_hdf5(hdf5_path, year_result)

                    # Plugin hook: post_year (HDF5 opened in append mode)
                    try:
                        import h5py
                        with h5py.File(hdf5_path, "a") as h5f:
                            self._pm.call_hook(
                                "post_year",
                                year=year,
                                result=year_result,
                                hdf5_file=h5f,
                                output_dir=self.output_dir,
                                config=self.config,
                            )
                    except ImportError:
                        self._pm.call_hook(
                            "post_year",
                            year=year,
                            result=year_result,
                            hdf5_file=None,
                            output_dir=self.output_dir,
                            config=self.config,
                        )

                    logger.info(f"Year {year} completed: objective=${year_result.objective:,.0f}")
                    logger.info(
                        f"  → RE={year_result.re_penetration:.1%} | "
                        f"cost=${year_result.objective:,.0f} | "
                        f"gen={year_result.total_generation:,.0f}MWh | "
                        f"shed={year_result.load_shed:.1f}MWh"
                    )
                except Exception as e:
                    logger.error(f"Year {year} failed: {e}", exc_info=True)
                    progress.stop()
                    raise

                # Update progress
                progress.update(year_task, advance=1)

                # Clean up memory
                gc.collect()

            self._progress = None

        # Finalize HDF5 file
        self._finalize_hdf5(hdf5_path)

        # Plugin hook: post_simulation
        self._pm.call_hook(
            "post_simulation",
            results=self.results,
            hdf5_path=hdf5_path,
            output_dir=self.output_dir,
            config=self.config,
        )

        # Teardown plugins
        self._pm.teardown_all()

        total_time = time.time() - start_time
        console.print(f"\n[bold green]Simulation completed[/bold green] in {total_time/60:.1f} minutes")
        console.print(f"[bold]Results exported to:[/bold] {hdf5_path}")
        logger.info(f"Simulation completed in {total_time/60:.1f} minutes")

        return self.results

    def _resolve_start_date(self, start_year: Optional[int] = None) -> datetime:
        """Timestamp of hour 0: ``date_start``, moved to ``start_year`` if given."""
        start = parse_date_start(getattr(self.config, 'date_start', "01/01/2025 00:00"))
        if start_year is None or start_year == start.year:
            return start
        try:
            return start.replace(year=int(start_year))
        except ValueError:  # 29 Feb in a common year
            return start.replace(year=int(start_year), month=3, day=1)

    def _data_start(self) -> datetime:
        """Timestamp of the first data row: ``date_start``.

        This is the data's own calendar, which decides the Feb 29 rows that
        are dropped; ``run(start_year=...)`` only relabels the years and
        never changes it.
        """
        return self._resolve_start_date()

    def _load_demand(
        self,
        years: int,
        start_year: int
    ) -> Tuple[np.ndarray, int, int, List[int], List[datetime]]:
        """Load demand data from all configured systems and concatenate.

        For multi-system configs each system's demand is loaded separately
        and horizontally stacked so that columns align with the merged
        node numbering (system A nodes first, then system B, …).

        Returns:
            Tuple of (demand_array, total_hours, num_nodes, years_list, time_index)
        """
        hours_per_year = HOURS_STD_YEAR
        total_hours_needed = years * hours_per_year
        years_list = list(range(start_year, start_year + years))

        # Demand files resolve relative to the config directory first (so a
        # portable project — config + demand/ next to it — runs from anywhere),
        # falling back to the path as-given (CWD-relative or absolute) for
        # backward compatibility with existing configs.
        config_dir = Path(self.config_path).parent if self.config_path else Path('.')

        def _resolve_demand(p) -> Path:
            fp = Path(p)
            if not fp.is_absolute():
                cand = config_dir / fp
                if cand.exists():
                    return cand
            return fp

        # Hour 0 is the first data row; labels start at the (possibly
        # relabelled) start date and skip Feb 29 (every model year is 8760 h).
        # The Feb 29 rows dropped are those of the data's calendar.
        start_date = self._data_start()
        label_start = getattr(self, '_start_date', None) or start_date
        time_index = list(model_time_index(label_start, total_hours_needed).to_pydatetime())

        # Load demand per system and concatenate
        demand_parts: list[np.ndarray] = []
        for sname in self._system_names:
            sys = self.config.systems[sname]
            import math
            expected_nodes = sys.nodes.num_nodes or int(math.sqrt(len(sys.nodes.nodes_connections)))

            # Determine file list: prefer demand_paths (per-node), fall back to demand_path
            if sys.demand_paths:
                file_list = [_resolve_demand(p) for p in sys.demand_paths]
                if len(file_list) != expected_nodes:
                    raise ValueError(
                        f"System '{sname}': demand_paths has {len(file_list)} entries "
                        f"but system has {expected_nodes} nodes"
                    )
            elif sys.demand_path:
                dp = _resolve_demand(sys.demand_path)
                # Single file — check if it's a multi-column file or a single-node file
                test_df = _read_tabular(dp, nrows=2)
                if test_df.shape[1] >= expected_nodes:
                    # Multi-column file covers all nodes
                    file_list = [dp]
                else:
                    # Single-column file — auto-discover per-node siblings
                    import re
                    m = re.search(r'(demand_node_)\d+', dp.stem)
                    if m:
                        prefix = m.group(1)
                        file_list = [dp.parent / f"{prefix}{ni}{dp.suffix}" for ni in range(expected_nodes)]
                    else:
                        file_list = [dp]
            else:
                raise ValueError(f"No demand_path or demand_paths configured for system '{sname}'")

            try:
                if len(file_list) == 1 and not (sys.demand_paths and expected_nodes > 1):
                    # Single file (possibly multi-column)
                    fp = file_list[0]
                    if not fp.exists():
                        raise FileNotFoundError(f"Demand file not found: {fp}")
                    raw = read_hourly_table(fp)
                else:
                    # Multiple per-node files — load and hstack
                    node_arrays = []
                    for ni, fp in enumerate(file_list):
                        if not fp.exists():
                            raise FileNotFoundError(
                                f"Per-node demand file missing for '{sname}' node {ni}: {fp}"
                            )
                        arr = read_hourly_table(fp)
                        node_arrays.append(arr[:, :1])  # take first column only
                    min_h = min(a.shape[0] for a in node_arrays)
                    raw = np.hstack([a[:min_h] for a in node_arrays])

                # Model years are 8760 h: drop Feb 29 rows of leap-year data.
                n_read = raw.shape[0]
                raw = drop_leap_days(raw, start_date)
                if raw.shape[0] != n_read:
                    logger.info(
                        f"Demand for '{sname}': dropped {n_read - raw.shape[0]} "
                        f"Feb 29 hours (every model year is {HOURS_STD_YEAR} h)"
                    )
                file_hours = raw.shape[0]
                file_nodes = raw.shape[1]
                logger.debug(f"Read demand for '{sname}': {file_hours}h × {file_nodes} nodes")
            except (FileNotFoundError, ValueError):
                raise
            except Exception as e:
                raise IOError(f"Failed to load demand for '{sname}': {e}")

            # Tile / trim to match horizon
            if file_hours >= total_hours_needed:
                part = raw[:total_hours_needed, :]
            elif file_hours >= hours_per_year:
                first_year = raw[:hours_per_year, :]
                part = np.tile(first_year, (years, 1))
                logger.warning(f"Demand for '{sname}' has {file_hours}h — tiling first year for {years} years.")
            else:
                part = raw
                logger.warning(f"Demand for '{sname}' has only {file_hours}h (< 1 year).")

            # Apply per-system demand scale
            part = part * sys.demand_scale
            demand_parts.append(part)

        # Ensure all parts have same number of rows
        min_rows = min(p.shape[0] for p in demand_parts)
        demand_parts = [p[:min_rows, :] for p in demand_parts]
        if min_rows < total_hours_needed:
            time_index = time_index[:min_rows]

        demand = np.hstack(demand_parts)
        total_hours = demand.shape[0]
        num_nodes = demand.shape[1]

        logger.debug(
            f"Total demand loaded: {np.sum(demand):.0f} MWh ({np.sum(demand)/1e6:.2f} TWh), "
            f"hours={total_hours}, nodes={num_nodes} "
            f"({' + '.join(f'{p.shape[1]}' for p in demand_parts)} merged)"
        )
        logger.debug(f"Years: {years_list[0]} to {years_list[-1]}")

        return demand, total_hours, num_nodes, years_list, time_index

    def _generate_ev_demand(
        self,
        num_nodes: int,
        total_hours: int,
        base_year: int,
        target_year: int,
    ) -> Tuple[np.ndarray, pd.DataFrame, pd.DataFrame]:
        """Generate EV charging demand with S-curve fleet growth.

        Args:
            num_nodes: Number of nodes in the system
            total_hours: Total simulation hours
            base_year: Base year for growth calculations
            target_year: Target year for projections

        Returns:
            Tuple of (ev_demand_array, ev_charging_profiles, v2g_availability_profiles)
            ev_demand_array has shape (total_hours, num_nodes)
        """
        sys = self.primary_system

        # Check if EV configuration exists
        if not sys.ev_categories or not sys.ev_quantity or not sys.base_patterns:
            logger.debug("No EV configuration found. Skipping EV demand generation.")
            return np.zeros((total_hours, num_nodes)), None, None

        # Convert Pydantic models to dicts for the generate functions
        ev_categories_dict = {
            name: cat.model_dump() for name, cat in sys.ev_categories.items()
        }
        # _build_ev_config sizes the dispatch fleet on the same S-curve.
        self._ev_years = (base_year, target_year)

        logger.debug(f"Generating EV profiles for {total_hours} hours, {num_nodes} nodes")
        logger.debug(f"EV categories: {list(ev_categories_dict.keys())}")
        logger.debug(f"Base year: {base_year}, Target year: {target_year}")

        # Generate EV charging profiles (includes S-curve growth)
        ev_charging = generate_ev_profiles(
            num_nodes=num_nodes,
            num_hours=total_hours,
            ev_categories=ev_categories_dict,
            ev_quantity=sys.ev_quantity,
            base_patterns=sys.base_patterns,
            base_year=base_year,
            target_year=target_year,
        )

        # Generate V2G availability profiles (includes S-curve growth)
        v2g_availability = generate_v2g_availability(
            num_nodes=num_nodes,
            num_hours=total_hours,
            ev_categories=ev_categories_dict,
            ev_quantity=sys.ev_quantity,
            base_patterns=sys.base_patterns,
            base_year=base_year,
            target_year=target_year,
        )

        # Aggregate EV profiles to get total demand per node
        ev_demand = aggregate_ev_profiles(ev_charging, num_nodes)

        # Log EV demand statistics
        hours_per_year = HOURS_STD_YEAR
        first_year_demand = ev_demand[:hours_per_year, :]
        last_year_start = max(0, total_hours - hours_per_year)
        last_year_demand = ev_demand[last_year_start:, :]

        first_year_peak = np.max(np.sum(first_year_demand, axis=1))
        last_year_peak = np.max(np.sum(last_year_demand, axis=1)) if len(last_year_demand) > 0 else 0

        logger.debug(f"EV demand generated: first year peak = {first_year_peak:.1f} MW, "
                    f"last year peak = {last_year_peak:.1f} MW")
        if first_year_peak > 0:
            growth = (last_year_peak / first_year_peak - 1) * 100
            logger.debug(f"EV demand growth over horizon: {growth:.1f}%")

        return ev_demand, ev_charging, v2g_availability

    def _create_sectoral_demand(
        self,
        total_demand: np.ndarray,
    ) -> Dict[str, np.ndarray]:
        """Create sectoral demand distribution from total demand.

        Args:
            total_demand: Total demand array (hours, nodes)

        Returns:
            Dictionary of sectoral demands {sector: array(hours, nodes)}
        """
        sys = self.primary_system

        if not sys.sector_distribution or not sys.electric_demand:
            logger.debug("No sector distribution configured. Using aggregated demand.")
            return {}

        # Convert sector_distribution keys from string to int if needed
        sector_dist = {}
        for key, value in sys.sector_distribution.items():
            node_key = int(key) if isinstance(key, str) else key
            sector_dist[node_key] = value

        # Get sectors list
        sectors_list = list(sys.electric_demand.keys())

        logger.debug(f"Creating sectoral demand for {len(sectors_list)} sectors: {sectors_list}")

        sectoral_demand = create_sectoral_demand(
            base_demand=total_demand,
            sector_distribution=sector_dist,
            sectors_list=sectors_list,
        )

        return sectoral_demand

    # ── Fuel infrastructure and electrolyzers built by the master (#89) ──

    def _pe_enabled(self, system_config) -> bool:
        """Primary energy is on and the system has fuel sources or entry points.

        The same switch turns on the dispatch's fuel model and the master's
        fuel block."""
        return bool(self.config.enable_primary_energy and (
            system_config.primary_energy_sources
            or getattr(system_config, 'fuel_entry_points', None)))

    def _pe_inputs(self, system_config, num_nodes: int) -> dict:
        """Fuel data of ``system_config`` as ``PrimaryEnergyAdapter`` keyword
        arguments: fuel sources (annual availability × step length, since the
        fuel model prorates it per step), non-electric demand, fuel
        infrastructure, routes, penalties, outages. Shared by the dispatch
        windows and the master's fuel block, so both see the same fuel data."""
        import copy

        fuels_def = {}
        for fk, fc in system_config.fuels.items():
            fuels_def[fk] = fc.model_dump() if hasattr(fc, 'model_dump') else fc.__dict__.copy()

        if system_config.primary_energy_sources:
            pe_sources = {
                pk: (pc.model_dump() if hasattr(pc, 'model_dump') else pc.__dict__.copy())
                for pk, pc in system_config.primary_energy_sources.items()}
        else:
            # Auto-build PE sources from fuel_entry_points + storage
            pe_sources = self._build_pe_sources_from_entries(system_config, num_nodes)

        # Growth per fuel (non_electric_demand_growth, none if absent) and
        # monthly factors seasonal_factors[fuel][sector]; the sector is the
        # key's part before the first underscore.
        ne_demand = {}
        ne_growth = system_config.non_electric_demand_growth or {}
        ne_seasonal = system_config.seasonal_factors or {}
        for nk, nc in system_config.non_electric_demand.items():
            ne_demand[nk] = nc.model_dump() if hasattr(nc, 'model_dump') else nc.__dict__.copy()
            fuel_nk = ne_demand[nk].get('fuel')
            ne_demand[nk]['growth_rate'] = float(ne_growth.get(fuel_nk, 0.0))
            ne_demand[nk]['seasonal_factors'] = (
                ne_seasonal.get(fuel_nk, {}).get(nk.split('_')[0]))

        infra = system_config.fuel_infrastructure
        infra_dict = infra.model_dump() if hasattr(infra, 'model_dump') else infra.__dict__.copy()
        penalties = system_config.penalties
        penalties_dict = (penalties.model_dump() if hasattr(penalties, 'model_dump')
                          else penalties.__dict__.copy())

        # The fuel model prorates annual availability by steps / 8760; with
        # res_h-hour steps it must be by hours, hence × res_h.
        res_h = getattr(self.config.temporal, 'resolution_hours', 1)
        if res_h > 1:
            pe_sources = copy.deepcopy(pe_sources)
            for fuel_data in pe_sources.values():
                avail_list = fuel_data.get('max_availability', [])
                fuel_data['max_availability'] = [a * res_h for a in avail_list]

        return dict(
            fuels_config=pe_sources,
            non_electric_demand=ne_demand,
            infrastructure_config=infra_dict,
            transport_distances=system_config.fuel_transport_distances,
            fuels_definition=fuels_def,
            penalties_config=penalties_dict,
            discount_rate=system_config.discount_rate,
            transport_routes=system_config.fuel_transport_routes,
            outage_schedule=getattr(system_config, "outage_schedule", None),
        )

    def _master_fuel_kwargs(self, system_config, last_year: int,
                            cumulative: Optional[dict] = None) -> Optional[dict]:
        """``primary_energy`` argument of ``MasterProblemAdapter``: the fuel
        data, the run's base year (fuel price growth and disruption hours
        count from it), the last modelled year and the fuel capacity built
        before the master's first year. None when primary energy is off."""
        if not self._pe_enabled(system_config):
            return None
        return dict(
            self._pe_inputs(system_config, self._num_nodes),
            base_year=self.state.base_year,
            last_year=last_year,
            cumulative_capacities=deepcopy(cumulative) if cumulative else None,
        )

    def _store_master_fuel(self, master, result: dict, years: list[int],
                           carried: Optional[dict] = None) -> None:
        """Record the fuel storage, route and electrolyzer capacity the
        master built and that is in service in each of ``years``, on top of
        ``carried`` (what earlier myopic years built), for the dispatch.

        ``self._master_fuel_by_year[year]`` = {"storage": {fuel: {node: cap}},
        "transport": {fuel: {route: cap}}, "electrolyzer": {key: [MW per
        node]}}, 0-based like ``state.primary_energy_capacities``."""
        to_caps = getattr(master, "fuel_capacities_by_year", None)
        by_year = to_caps(result) if to_caps is not None else {}
        store = getattr(self, "_master_fuel_by_year", None)
        if store is None:
            store = self._master_fuel_by_year = {}
        for y_idx, year in enumerate(years, start=1):
            caps = deepcopy(carried) if carried else {"storage": {}, "transport": {}, "electrolyzer": {}}
            for kind in ("storage", "transport"):
                for fuel, by_idx in by_year.get(y_idx, {}).get(kind, {}).items():
                    tgt = caps.setdefault(kind, {}).setdefault(fuel, {})
                    for idx, v in by_idx.items():
                        tgt[idx] = tgt.get(idx, 0.0) + v
            for key, per_node in by_year.get(y_idx, {}).get("electrolyzer", {}).items():
                tgt = caps.setdefault("electrolyzer", {}).setdefault(key, [0.0] * len(per_node))
                for n, v in enumerate(per_node):
                    tgt[n] += v
            store[year] = caps

    def _dispatch_pe_capacities(self, year: int) -> dict:
        """Fuel storage and route capacity built by the master and in service
        in ``year`` ({kind: {fuel: {index: capacity}}}); empty without a master."""
        caps = (getattr(self, "_master_fuel_by_year", None) or {}).get(year) or {}
        current = {"storage": caps.get("storage", {}), "transport": caps.get("transport", {})}
        self.state.primary_energy_capacities = current
        return current

    @staticmethod
    def _system_with_electrolyzers(system_config, added: dict, used: Optional[dict] = None):
        """``system_config`` with ``added`` ({electrolyzer key: MW per node})
        added to the electrolyzers' ``rated_power`` and ``used`` (same
        shape) taken off their ``invest_max_power`` (a myopic year: the
        builds still in service, and everything built so far, retired or
        not, since the build limit counts every build)."""
        def _nz(d):
            return {k: v for k, v in (d or {}).items() if any(x > 1e-9 for x in v)}
        added, used = _nz(added), _nz(used)
        if not added and not used:
            return system_config
        elz = dict(system_config.electrolyzers or {})

        def _at(lst, n):
            return lst[n] if n < len(lst) else 0.0
        for key in set(added) | set(used):
            if key not in elz:
                continue
            e = elz[key]
            upd = {}
            if key in added:
                upd["rated_power"] = [float(r) + _at(added[key], n)
                                      for n, r in enumerate(e.rated_power)]
            if key in used:
                upd["invest_max_power"] = [max(0.0, float(m) - _at(used[key], n))
                                           for n, m in enumerate(e.invest_max_power)]
            elz[key] = e.model_copy(update=upd)
        return system_config.model_copy(update={"electrolyzers": elz})

    @staticmethod
    def _myopic_electrolyzers(builds: dict, system_config, y_idx: int) -> tuple[dict, dict]:
        """Electrolyzer power earlier myopic years built ({key: {build year
        index: MW per node}}): ``(in service in year y_idx, built in all)``,
        {key: MW per node} each. A build retires after the electrolyzer's
        ``life_time`` at its node (0 = never), as in perfect foresight."""
        in_service: dict[str, list[float]] = {}
        total: dict[str, list[float]] = {}
        elz = system_config.electrolyzers or {}
        for key, by_year in builds.items():
            life = list(getattr(elz.get(key), "life_time", None) or [])
            for b_idx, per_node in by_year.items():
                tot = total.setdefault(key, [0.0] * len(per_node))
                cur = in_service.setdefault(key, [0.0] * len(per_node))
                for n, v in enumerate(per_node):
                    tot[n] += v
                    lt = life[n] if n < len(life) else 0
                    if lt <= 0 or y_idx - b_idx < lt:
                        cur[n] += v
        return in_service, total

    def _dispatch_system(self, year: int):
        """The operational system of ``year``: ``primary_system`` with the
        electrolyzer power the master built added to each electrolyzer's
        ``rated_power`` (per node)."""
        caps = (getattr(self, "_master_fuel_by_year", None) or {}).get(year) or {}
        if not caps.get("electrolyzer"):
            return self.primary_system
        cache = getattr(self, "_dispatch_system_cache", None)
        if cache is None:
            cache = self._dispatch_system_cache = {}
        if year not in cache:
            cache[year] = self._system_with_electrolyzers(
                self.primary_system, caps["electrolyzer"])
        return cache[year]

    def _build_ev_config(
        self,
        year_idx: int,
        window_start_hour: int,
        window_hours: int,
        resolution_hours: int = 1,
    ) -> Optional[Dict]:
        """Build aggregated EV config data for a specific window.

        Aggregates per-category EV data into a single EVConfig-compatible dict
        that maps to the Julia EVConfig struct.

        Args:
            year_idx: Year index (0-based)
            window_start_hour: Start hour within the year
            window_hours: Number of hours in this window
            resolution_hours: Time step (h); the hourly profiles are averaged
                to it, since Julia indexes them by step like the demand

        Returns:
            Dictionary with EVConfig fields, or None if no EV data
        """
        sys = self.primary_system
        if not sys.ev_categories or self._ev_charging_profiles is None:
            return None

        num_nodes = self._num_nodes
        hours_per_year = HOURS_STD_YEAR

        # Compute absolute hour offsets for slicing full-horizon arrays
        abs_start = year_idx * hours_per_year + window_start_hour
        abs_end = abs_start + window_hours

        # Fleet of this year: the same S-curve the profiles were generated
        # with, so the fleet limits (charge power, SOC capacity, V2G) match
        # the driving consumption. The base-year quantities alone fall far
        # short of the grown consumption in later years.
        base_year, target_year = getattr(self, "_ev_years", (2025, 2050))
        year = base_year + year_idx
        fleet: dict[str, np.ndarray] = {}
        for cat_name, cat in sys.ev_categories.items():
            qty = np.zeros(num_nodes)
            src = sys.ev_quantity.get(cat_name, [])
            qty[:min(len(src), num_nodes)] = src[:num_nodes]
            fleet[cat_name] = qty * fleet_growth_factor(
                cat.model_dump(), year, base_year, target_year)
        total_vehicles = sum(fleet.values()) if fleet else np.zeros(num_nodes)

        if np.sum(total_vehicles) < 1:
            return None

        # The Julia EVConfig holds one parameter set for the whole fleet:
        # vehicle-weighted means keep the fleet's total battery energy and
        # charge / V2G power exact; efficiencies and min SOC are weighted by
        # battery energy.
        n_cat = {k: float(v.sum()) for k, v in fleet.items()}
        n_all = sum(n_cat.values())
        cats = sys.ev_categories

        def _per_vehicle(attr):
            return sum(n_cat[k] * getattr(cats[k], attr) for k in cats) / n_all

        battery_kwh = _per_vehicle("battery_capacity")
        e_cat = {k: n_cat[k] * cats[k].battery_capacity for k in cats}
        e_all = sum(e_cat.values()) or 1.0

        def _per_energy(attr):
            return sum(e_cat[k] * getattr(cats[k], attr) for k in cats) / e_all

        charge_kw = _per_vehicle("charging_power")
        v2g_kw = _per_vehicle("v2g_power")

        try:
            avail_profile = self._v2g_availability_profiles
            charge_profile = self._ev_charging_profiles

            # Julia caps V2G at v2g_kw · N / 1000 · availability. The profiles
            # give the available V2G power in MW, so availability is that power
            # over the fleet's rated V2G power (fraction of the fleet's V2G
            # capability on offer that hour).
            avail_arr = np.ones((window_hours, num_nodes))
            driving_arr = np.zeros((window_hours, num_nodes))

            if avail_profile is not None and len(avail_profile) > abs_start:
                slice_end = min(abs_end, len(avail_profile))
                v2g_mw = np.zeros((window_hours, num_nodes))
                for n in range(num_nodes):
                    for cat_name in sys.ev_categories:
                        col_key = f"Node_{n+1}_{cat_name}"
                        if col_key in avail_profile.columns:
                            v2g_mw[:slice_end - abs_start, n] += (
                                avail_profile[col_key].values[abs_start:slice_end]
                            )
                rated_mw = v2g_kw * total_vehicles / 1000.0
                avail_arr = np.divide(v2g_mw, rated_mw,
                                      out=np.zeros_like(v2g_mw), where=rated_mw > 0)

            if charge_profile is not None and len(charge_profile) > abs_start:
                # Driving consumption from charging profiles
                for n in range(num_nodes):
                    for cat_name in sys.ev_categories:
                        col_key = f"Node_{n+1}_{cat_name}"
                        if col_key in charge_profile.columns:
                            slice_end = min(abs_end, len(charge_profile))
                            if abs_start < slice_end:
                                # generate_ev_profiles already yields MW (MWh
                                # per hour), the unit Julia expects; dividing
                                # by 1000 again made EV load ~vanish.
                                driving_arr[:slice_end - abs_start, n] += (
                                    charge_profile[col_key].values[abs_start:slice_end]
                                )

            if resolution_hours > 1:
                avail_arr = aggregate_to_resolution(avail_arr, target_hours=resolution_hours)
                driving_arr = aggregate_to_resolution(driving_arr, target_hours=resolution_hours)

            pen = sys.penalties
            if pen.ev_loss < pen.loss_of_load and not getattr(self, "_ev_loss_warned", False):
                self._ev_loss_warned = True
                logger.warning(
                    f"System '{sys.name}': ev_loss ({pen.ev_loss:g} $/MWh) is below "
                    f"loss_of_load ({pen.loss_of_load:g} $/MWh). The dispatch leaves EV "
                    f"driving unsupplied whenever generating costs more than ev_loss."
                )

            return {
                'num_vehicles': total_vehicles,
                'battery_capacity_kwh': battery_kwh,
                'max_charge_power_kw': charge_kw,
                'max_discharge_power_kw': v2g_kw,
                'charge_efficiency': _per_energy("efficiency_charge"),
                'discharge_efficiency': _per_energy("efficiency_discharge"),
                'min_soc': _per_energy("min_soc"),
                'max_soc': 1.0,
                'target_soc': 0.8,
                # EV_initial_soc (fraction per node); 80 % where not given
                'initial_soc_fraction': [
                    float(sys.ev_initial_soc[n]) if n < len(sys.ev_initial_soc or []) else 0.8
                    for n in range(num_nodes)],
                'availability_profile': avail_arr,
                'driving_consumption_profile': driving_arr,
                'v2g_compensation': 0.0,
                'loss_penalty': sys.penalties.ev_loss,
            }
        except Exception as e:
            logger.warning(f"Failed to build EV config: {e}")
            return None

    def _extract_year_demand(
        self,
        full_demand: np.ndarray,
        year_idx: int,
        hours_per_year: int = HOURS_STD_YEAR,
    ) -> np.ndarray:
        """Extract demand for a specific year from the full horizon demand.

        Args:
            full_demand: Full demand array for all years
            year_idx: Year index (0-based)
            hours_per_year: Hours per year (default HOURS_STD_YEAR)

        Returns:
            Demand array for the specified year
        """
        start_hour = year_idx * hours_per_year
        end_hour = min(start_hour + hours_per_year, full_demand.shape[0])

        if end_hour > start_hour:
            return full_demand[start_hour:end_hour, :]
        else:
            # Use first year if index out of range
            logger.warning(f"Year index {year_idx} out of range. Using first year.")
            return full_demand[:hours_per_year, :]

    def _renewable_available(
        self,
        units,
        hours: int,
        num_nodes: int,
        year_idx: int = 0,
    ) -> np.ndarray:
        """Renewable power available per step and node, ``(hours, nodes)``.

        ``units`` yields ``(availability_key, rated_per_node)``. Each unit
        counts ``rated × availability``, with the profile read like the
        dispatch reads it: year ``year_idx``'s block (horizon hour, wrapping
        around a shorter profile), and 1.0 in every hour for a unit without a
        profile (the dispatch runs such a unit at full capacity).
        """
        cache = getattr(self, '_availability_cache', None) or {}
        re_available = np.zeros((hours, num_nodes))
        for avail_key, rated in units:
            rated = np.asarray(rated, dtype=float)[:num_nodes]
            if not np.any(rated > 0):
                continue
            availability = cache.get(avail_key)
            if availability is None or len(availability) == 0:
                re_available[:, :len(rated)] += np.maximum(rated, 0.0)
                continue
            block = _profile_year_block(np.asarray(availability, dtype=float), year_idx, hours)
            for n in range(len(rated)):
                if rated[n] <= 0:
                    continue
                if block.ndim == 1:
                    re_available[:, n] += rated[n] * block
                elif n < block.shape[1]:
                    re_available[:, n] += rated[n] * block[:, n]
        return re_available

    def _existing_renewables(self, num_nodes: int):
        """``(key, rated_per_node)`` of the configured renewable generators."""
        return [
            (key, [gen.rated_power[n] if n < len(gen.rated_power) else 0.0
                   for n in range(num_nodes)])
            for key, gen in self.primary_system.generators.items()
            if gen.type == "Renewable"
        ]

    def _calculate_initial_re_penetration(
        self,
        first_year_demand: np.ndarray,
    ) -> float:
        """Calculate initial RE penetration using per-timestep dispatch estimate.

        For each timestep, the usable RE is ``min(RE_available, demand)``
        because excess RE above demand is curtailed.  This gives a realistic
        estimate that accounts for the temporal mismatch between renewable
        availability and load profiles. A renewable unit without an
        availability profile counts at full capacity, as in the dispatch.

        Args:
            first_year_demand: Demand array for first year (hours × nodes)

        Returns:
            Initial RE penetration as fraction (0-1)
        """
        hours = first_year_demand.shape[0]
        num_nodes = first_year_demand.shape[1]
        re_available = self._renewable_available(
            self._existing_renewables(num_nodes), hours, num_nodes)

        # Per-timestep: usable RE = min(RE_available, demand)
        demand = first_year_demand[:hours, :]
        usable_re = np.minimum(re_available, demand)
        total_usable_re = float(np.sum(usable_re))
        total_demand = float(np.sum(demand))

        initial_re = total_usable_re / total_demand if total_demand > 0 else 0.0
        initial_re = min(1.0, max(0.0, initial_re))

        logger.debug(f"Calculated initial RE penetration: {initial_re:.2%} "
                    f"(usable RE: {total_usable_re:.0f}, "
                    f"demand: {total_demand:.0f})")

        return initial_re

    def _estimate_achievable_re(
        self,
        units_config: dict[str, Any],
        year_demand: np.ndarray,
        year_idx: int = 0,
    ) -> float:
        """Upper bound on RE penetration the FIXED yearly plan can deliver.

        usable_re = min(Σ rated×availability, demand) per timestep, summed and
        divided by demand — the same dispatch-estimate convention as
        ``_calculate_initial_re_penetration`` and the master's energy-based RE
        constraint, but evaluated against the cumulative (post-investment)
        capacities for this year.

        Used to cap the operational RE target: the master passes an aspirational
        target ramp (linear → 100%) that the least-cost plan does not meet until
        existing fossil retires (~2036). The operational cannot build capacity,
        so penalizing it against the aspirational target produces a phantom
        fre_penetration slack. Capping at the achievable level removes it without
        changing physical dispatch (RE is still fully used via curtailment_cost).
        Profiles are read like the dispatch reads them (``_renewable_available``).
        """
        hours, num_nodes = year_demand.shape
        b2n = getattr(self, '_bus_to_node', None)
        units = []
        for ukey, udata in units_config.items():
            if udata.get("type") != "Renewable":
                continue
            rated = udata.get("rated_power", []) or []
            if not rated or sum(rated) <= 0:
                continue
            # Investment units are keyed "inv_<tech_key>"; their availability
            # lives in the cache under the bare technology key. Existing units
            # use their own gen_key directly.
            avail_key = ukey[4:] if ukey.startswith("inv_") else ukey
            # rated_power may be per-node (existing gens, len == num_nodes) or
            # per-bus (investment units, len == num_buses). Availability and
            # demand here are per-node, so aggregate rated to per-node first —
            # otherwise per-bus arrays get indexed as if they were per-node,
            # reading only the first num_nodes buses and grossly undercounting.
            arr = list(rated)
            rated_per_node = np.zeros(num_nodes)
            if len(arr) == num_nodes:
                rated_per_node = np.asarray(arr[:num_nodes], dtype=float)
            elif b2n is not None and len(arr) == len(b2n):
                for b, cap in enumerate(arr):
                    nidx = b2n[b]
                    if 0 <= nidx < num_nodes:
                        rated_per_node[nidx] += float(cap)
            else:
                m = min(len(arr), num_nodes)
                rated_per_node[:m] = arr[:m]
            units.append((avail_key, rated_per_node))
        re_available = self._renewable_available(units, hours, num_nodes, year_idx)
        usable_re = float(np.minimum(re_available, year_demand[:hours]).sum())
        total_demand = float(year_demand.sum())
        return usable_re / total_demand if total_demand > 0 else 0.0

    def _calculate_per_system_initial_re(
        self,
        first_year_demand: np.ndarray,
    ) -> dict[str, float]:
        """Calculate initial RE penetration per system using dispatch estimate.

        For each system and timestep, usable RE = ``min(RE_available, demand)``
        to account for temporal mismatch.  Consistent with the energy-based
        RE constraints in the master problem.

        Args:
            first_year_demand: Demand array for first year (hours × nodes)

        Returns:
            Dict mapping system name to initial RE penetration (0-1)
        """
        if not self._system_node_offsets or len(self._system_node_offsets) <= 1:
            sname = list(self._system_node_offsets.keys())[0] if self._system_node_offsets else self.system_name
            return {sname: self._calculate_initial_re_penetration(first_year_demand)}

        hours = first_year_demand.shape[0]
        num_nodes = first_year_demand.shape[1]
        sys_names = list(self._system_node_offsets.keys())
        result = {}

        # Per-timestep RE availability at each node (global)
        re_available = self._renewable_available(
            self._existing_renewables(num_nodes), hours, num_nodes)

        for i, sname in enumerate(sys_names):
            off = self._system_node_offsets[sname]
            if i + 1 < len(sys_names):
                cnt = list(self._system_node_offsets.values())[i + 1] - off
            else:
                cnt = num_nodes - off

            sys_demand = first_year_demand[:, off:off + cnt]
            sys_re = re_available[:, off:off + cnt]
            # Per-timestep per-node: usable RE = min(RE, demand)
            usable = np.minimum(sys_re, sys_demand)
            total_usable = float(np.sum(usable))
            total_dem = float(np.sum(sys_demand))

            re_pen = min(1.0, max(0.0, total_usable / total_dem)) if total_dem > 0 else 0.0
            result[sname] = re_pen
            logger.debug(f"  System '{sname}' initial RE: {re_pen:.2%} "
                        f"(usable RE: {total_usable:.0f}, "
                        f"demand: {total_dem:.0f})")

        return result

    def _bus_pairs_to_node_flows(self, pair_series: dict, num_nodes: int) -> np.ndarray:
        """Sum ``{(from_bus, to_bus): series}`` into a ``[from_node, to_node, step]`` array.

        The LP keys power flows and transfer margins by operational bus
        index; map both ends through ``bus_to_node`` and drop lines inside
        a node.
        """
        n_steps = max((len(v) for v in pair_series.values()), default=0)
        out = np.zeros((num_nodes, num_nodes, n_steps))
        for (i, j), series in pair_series.items():
            nodes = self._bus_pair_nodes(i, j)
            if nodes is None or nodes[0] >= num_nodes or nodes[1] >= num_nodes:
                continue
            out[nodes[0], nodes[1], :len(series)] += series
        return out

    def _bus_pair_nodes(self, i: int, j: int) -> Optional[tuple[int, int]]:
        """(from_node, to_node) of a bus pair; None for intra-node or unknown buses."""
        b2n = self._bus_to_node
        if b2n is None:
            ni, nj = i, j
        else:
            if not (0 <= i < len(b2n) and 0 <= j < len(b2n)):
                return None
            ni, nj = b2n[i], b2n[j]
        if ni == nj or ni < 0 or nj < 0:
            return None
        return ni, nj

    def _aggregate_buses_to_nodes(self, arr, method="sum"):
        """Aggregate bus-level array to node-level.

        Args:
            arr: Array with a bus dimension (2D: [bus, hours] or 3D: [gen/bat, bus, hours])
            method: 'sum' for quantities, 'max' for status variables

        Returns:
            Array with bus dimension replaced by node dimension, or arr unchanged
            if no aggregation is needed.
        """
        if arr is None or self._bus_to_node is None:
            return arr
        n_nodes = self._num_nodes
        b2n = self._bus_to_node
        n_bus = len(b2n)

        # Determine which axis is the bus axis based on shape
        if arr.ndim == 2:
            # (bus, hours) → bus_axis=0
            if arr.shape[0] != n_bus:
                return arr
            result = np.zeros((n_nodes, arr.shape[1]), dtype=arr.dtype)
            for b in range(n_bus):
                ni = b2n[b]
                if method == "max":
                    result[ni] = np.maximum(result[ni], arr[b])
                else:
                    result[ni] += arr[b]
        elif arr.ndim == 3:
            # (gen/bat, bus, hours) → bus_axis=1
            if arr.shape[1] != n_bus:
                return arr
            result = np.zeros((arr.shape[0], n_nodes, arr.shape[2]), dtype=arr.dtype)
            for b in range(n_bus):
                ni = b2n[b]
                if method == "max":
                    result[:, ni, :] = np.maximum(result[:, ni, :], arr[:, b, :])
                else:
                    result[:, ni, :] += arr[:, b, :]
        else:
            return arr

        return result

    def _aggregate_unit_buses_to_nodes(self, arr):
        """Sum a ``[unit x bus]`` array (e.g. investments) into ``[unit x node]``."""
        if arr is None or self._bus_to_node is None:
            return arr
        arr = np.asarray(arr)
        b2n = self._bus_to_node
        if arr.ndim != 2 or arr.shape[1] != len(b2n):
            return arr
        out = np.zeros((arr.shape[0], self._num_nodes), dtype=float)
        for b, n in enumerate(b2n):
            if 0 <= n < self._num_nodes:
                out[:, n] += arr[:, b]
        return out

    def _preload_availability_profiles(self, num_nodes: int) -> dict:
        """Preload all availability profiles once at startup.

        A configured ``availability_file`` that cannot be resolved or found
        is an error (a generator used to be dropped from the dispatch, a
        technology to crash with a KeyError). A renewable unit with no file
        at all runs at capacity factor 1.0 in every hour, which is right for
        dispatchable renewables (biomass, geothermal, reservoir hydro) but
        not for solar or wind: it is logged once per unit
        (:func:`_warn_flat_renewable`).

        Args:
            num_nodes: Number of nodes in the system

        Returns:
            Dictionary mapping gen_key -> availability array
        """
        from esfex.io.demand import load_run_availability
        from pathlib import Path

        cache = {}
        sys = self.primary_system
        config_dir = Path(self.config_path).parent if self.config_path else Path('.')

        # Track unique files to avoid loading the same file multiple times
        file_cache = {}

        def _load(kind: str, key: str, avail_file: str):
            # Same reader as the dispatch's fallback (load_run_availability):
            # path resolved like demand paths, hourly, data calendar.
            file_key = str(avail_file)
            if file_key not in file_cache:
                file_cache[file_key] = load_run_availability(
                    config_dir, avail_file, num_nodes,
                    self._data_start().strftime("%d/%m/%Y %H:%M"),
                    owner=f"{kind} {key!r}",
                )
            cache[key] = file_cache[file_key]
            # Also index by the original availability_file string so that
            # virtual generators can look up by file path directly.
            cache[avail_file] = cache[key]

        for gen_key, gen in sys.generators.items():
            avail_file = getattr(gen, 'availability_file', None)
            if avail_file:
                _load("Generator", gen_key, avail_file)
            elif (gen.type == "Renewable"
                  and not any(c > 0 for c in (gen.reservoir_capacity or []))
                  and (any(p > 0 for p in gen.rated_power)
                       or any(p > 0 for p in gen.invest_max_power))):
                _warn_flat_renewable("Generator", gen_key)

        # Also preload technology availability profiles
        for tech_key, tech in sys.technologies.items():
            avail_file = getattr(tech, 'availability_file', None)
            if avail_file:
                _load("Technology", tech_key, avail_file)
            elif tech.type == "Renewable" and any(p > 0 for p in tech.invest_max_power):
                _warn_flat_renewable("Technology", tech_key)

        logger.debug(f"Loaded {len(file_cache)} unique availability files for {len(cache)} generators/technologies")
        return cache

    def _preload_inflow_profiles(self, num_nodes: int) -> dict:
        """Preload all reservoir inflow profiles once at startup.

        Args:
            num_nodes: Number of nodes in the system

        Returns:
            Dictionary mapping gen_key -> inflow array (hours x nodes, MW-eq)
        """
        from esfex.io.demand import load_inflow_profile
        from pathlib import Path

        cache = {}
        sys = self.primary_system
        config_dir = Path(self.config_path).parent if self.config_path else Path('.')

        file_cache = {}

        for gen_key, gen in sys.generators.items():
            inflow_file = getattr(gen, 'reservoir_inflow_file', None)
            if not inflow_file:
                continue

            inflow_path = config_dir / inflow_file
            if not inflow_path.exists():
                inflow_path = Path(inflow_file)

            file_key = str(inflow_path.resolve())

            if file_key in file_cache:
                cache[gen_key] = file_cache[file_key]
            else:
                try:
                    inflow = load_inflow_profile(
                        inflow_path,
                        temporal_resolution_hours=1,
                        num_nodes=num_nodes,
                        date_start=self._data_start().strftime("%d/%m/%Y %H:%M"),
                    )
                    file_cache[file_key] = inflow
                    cache[gen_key] = inflow
                except Exception as e:
                    logger.warning(f"Failed to load inflow for {gen.name}: {e}")

        if cache:
            logger.debug(f"Loaded {len(file_cache)} unique inflow files for {len(cache)} generators")
        return cache

    def _generate_rooftop_solar(
        self,
        num_nodes: int,
        total_hours: int,
        years_range: list[int],
        start_year: int,
    ) -> Optional[np.ndarray]:
        """Generate rooftop solar generation array for all years.

        Follows the legacy main.py pattern: compute S-curve adoption per year,
        multiply by availability profile and installed capacity to get generation.

        Returns:
            Array of shape (total_hours, num_nodes) with rooftop generation (MW),
            or None if below threshold.
        """
        from rooftex import RooftopConfig, generate_profiles

        sys = self.primary_system
        cfg = sys.rooftop_solar_config

        # A None entry (a merged run's node of a system that leaves the list
        # unset) takes rooftex's default there: initial adoption 0.05, and
        # the potential rooftex draws when the systems/size lists are unset.
        systems_pn = list(cfg.systems_per_node or [])
        sizes = list(cfg.avg_system_size or [])
        partial_size = None in systems_pn or None in sizes
        init = cfg.initial_adoption or None
        if init is not None:
            init = [_ROOFTEX_DEFAULT_INITIAL_ADOPTION if v is None else v for v in init]

        # Generate base availability profile and adoption factors
        _rooftop_cfg = RooftopConfig(
            num_nodes=num_nodes,
            hours=HOURS_STD_YEAR,
            base_year=start_year,
            target_year=years_range[-1] if years_range else start_year + 25,
            adoption_scenario=cfg.adoption_scenario,
            weather_variability=cfg.weather_variability,
            seed=cfg.simulation_seed,
            performance_ratio=cfg.performance_ratio,
            # Unset (or empty, as the Studio writes them) → rooftex defaults:
            # potential drawn per node, default scenario rates and caps.
            initial_adoption=init,
            systems_per_node=None if partial_size else (systems_pn or None),
            avg_system_size_kw=None if partial_size else (sizes or None),
            adoption_rates=cfg.adoption_rates or None,
            max_adoption=cfg.max_adoption or None,
        )
        _rooftop_result = generate_profiles(_rooftop_cfg)
        rooftop_availability = _rooftop_result.availability
        rooftop_adoption = _rooftop_result.adoption_factors
        rooftop_potential = (
            list(_rooftop_result.max_potential_mw)
            if hasattr(_rooftop_result.max_potential_mw, '__iter__')
            else [_rooftop_result.max_potential_mw] * num_nodes
        )
        if partial_size:
            # Nodes with both values keep systems × size; the rest keep the
            # potential rooftex drew.
            for n in range(min(num_nodes, len(systems_pn), len(sizes))):
                if systems_pn[n] is not None and sizes[n] is not None:
                    rooftop_potential[n] = systems_pn[n] * sizes[n] / 1000.0

        # Configured max potential where set (None: rooftex's)
        if sys.rooftop_max_potential:
            for n, v in enumerate(sys.rooftop_max_potential[:num_nodes]):
                if v is not None:
                    rooftop_potential[n] = float(v)

        target_year = cfg.target_year
        base_year = cfg.base_year if hasattr(cfg, 'base_year') else start_year

        # Compute generation for all years
        rooftop_gen = np.zeros((total_hours, num_nodes))
        for year_idx, year in enumerate(years_range):
            year_start = year_idx * HOURS_STD_YEAR
            year_end = min(year_start + HOURS_STD_YEAR, total_hours)
            year_hours = year_end - year_start

            years_diff = year - base_year
            total_years = target_year - base_year
            progress_factor = min(1.0, years_diff / total_years) if total_years > 0 else 0
            s_curve_factor = 1 / (1 + np.exp(-10 * (progress_factor - 0.5)))

            current_adoption = rooftop_adoption * s_curve_factor
            installed_capacity = np.array(rooftop_potential) * current_adoption

            # Apply degradation
            degradation_rate = getattr(cfg, 'degradation_rate', 0.005)
            degradation_factor = 1.0 - (degradation_rate * years_diff / 2)
            installed_capacity = installed_capacity * degradation_factor

            # Tile availability to cover year hours
            avail = np.tile(rooftop_availability[:HOURS_STD_YEAR], (year_hours // HOURS_STD_YEAR + 1, 1))[:year_hours]

            for node in range(num_nodes):
                rooftop_gen[year_start:year_end, node] = installed_capacity[node] * avail[:, node]

        total_installed = np.max(np.sum(rooftop_gen, axis=1))
        if total_installed < 1.0:
            logger.debug(f"Rooftop solar below threshold: {total_installed:.2f} MW peak")
            return None

        return rooftop_gen

    def _gather_units(self) -> dict[str, Any]:
        """Gather all unit configurations from the system."""
        units = {}

        # Add generators
        for key, gen in self.primary_system.generators.items():
            units[key] = gen.model_dump()
            units[key]["_type"] = "generator"

        # Add batteries
        for key, bat in self.primary_system.batteries.items():
            units[key] = bat.model_dump()
            units[key]["_type"] = "battery"

        return units

    def _solve_master_problem(
        self,
        years_range: list[int],
        demand: np.ndarray,
        hours: int,
        num_nodes: int,
        ev_demand: Optional[np.ndarray] = None,
        total_demand: Optional[np.ndarray] = None,
    ) -> tuple[Optional[dict], Optional[dict]]:
        """
        Solve the master problem for capacity expansion.

        Dispatches to either perfect_foresight (all years at once) or
        myopic (year by year) depending on config.master_problem.planning_mode.

        Args:
            years_range: List of years to plan for
            demand: Full demand array (all years)
            hours: Total hours in demand
            num_nodes: Number of nodes

        Returns:
            Tuple of (investments_by_year, retirements_by_year) dictionaries
            or (None, None) if infeasible
        """
        master_cfg = getattr(self.config, 'master_problem', None)
        planning_mode = getattr(master_cfg, 'planning_mode', 'perfect_foresight')

        solve = (self._solve_master_problem_myopic if planning_mode == "myopic"
                 else self._solve_master_problem_foresight)
        result = solve(years_range, demand, hours, num_nodes,
                       ev_demand=ev_demand, total_demand=total_demand)
        if result is not None and len(result) > 2 and result[0] is not None and result[2] is not None:
            self._add_reservoir_caps(result[0], result[2])
        return result

    def _add_reservoir_caps(self, investments_by_year: dict, caps_by_year: dict) -> None:
        """Add the reservoir capacity built up to each year (MWh, summed over
        the ``reservoir_investment_{g}_{n}`` keys of that year and the earlier
        ones) to ``caps_by_year[year]["reservoir"]`` as ``{(g, node): MWh}``,
        for :meth:`_build_config_from_cumulative`. Reservoirs do not retire.
        """
        built: dict[tuple[int, int], float] = {}
        for year in sorted(caps_by_year):
            for key, value in (investments_by_year.get(year) or {}).items():
                gn = _reservoir_investment_key(key)
                if gn is not None:
                    at = (gn[0], self._master_index_node(gn[1]))
                    built[at] = built.get(at, 0.0) + float(value)
            if built and caps_by_year.get(year) is not None:
                caps_by_year[year]["reservoir"] = dict(built)

    def _solve_master_problem_myopic(
        self,
        years_range: list[int],
        demand: np.ndarray,
        hours: int,
        num_nodes: int,
        ev_demand: Optional[np.ndarray] = None,
        total_demand: Optional[np.ndarray] = None,
    ) -> tuple[Optional[dict], Optional[dict]]:
        """
        Solve the master problem year by year (myopic/sequential planning).

        Each year is solved independently using only information available
        at decision time. Previous years' investments are fixed as existing
        capacity. This avoids perfect foresight bias.

        Returns:
            Tuple of (investments_by_year, retirements_by_year) dictionaries
        """
        from esfex.bridge.adapters import MasterProblemAdapter

        logger.debug("=" * 60)
        logger.debug("MYOPIC PLANNING MODE")
        logger.debug(f"Solving {len(years_range)} years sequentially...")
        logger.debug("=" * 60)

        try:
            master_demand = total_demand if total_demand is not None else demand
            num_years = len(years_range)
            start_year = years_range[0]

            # Ensure enough demand data
            hours_needed = num_years * HOURS_STD_YEAR
            if master_demand.shape[0] < hours_needed:
                tiles_needed = (hours_needed + master_demand.shape[0] - 1) // master_demand.shape[0]
                master_demand = np.tile(master_demand, (tiles_needed, 1))[:hours_needed, :]

            # Config values
            temporal = self.config.temporal
            master_cfg = getattr(self.config, 'master_problem', None)
            primary = self.primary_system
            resolution_hours = getattr(temporal, 'resolution_hours', 1)
            hours_per_year_raw = HOURS_STD_YEAR
            hours_per_year_agg = HOURS_STD_YEAR // resolution_hours

            # Per-system RE targets
            target_re = getattr(primary, 'target_re_penetration', 1.0)
            first_year_demand = master_demand[:min(hours_per_year_raw, master_demand.shape[0]), :]
            overall_initial_re = self._calculate_initial_re_penetration(first_year_demand)
            config_initial_re = getattr(primary, 'initial_re_penetration', None)
            if config_initial_re and config_initial_re > 0.0:
                overall_initial_re = config_initial_re

            # Per-system initial RE and system_node_ranges
            per_system_initial_re = self._calculate_per_system_initial_re(first_year_demand)
            system_node_ranges: list[tuple] = []
            if self._system_node_offsets and len(self._system_node_offsets) > 1:
                sys_names = list(self._system_node_offsets.keys())
                for i, sname in enumerate(sys_names):
                    off = self._system_node_offsets[sname]
                    if i + 1 < len(sys_names):
                        cnt = list(self._system_node_offsets.values())[i + 1] - off
                    else:
                        cnt = num_nodes - off
                    sys_init_re = per_system_initial_re.get(sname, overall_initial_re)
                    # (name, first_bus_1indexed, num_buses, initial_re)
                    system_node_ranges.append((sname, off + 1, cnt, sys_init_re))
                logger.debug(f"Per-system RE: {per_system_initial_re}")

            # Per-system current RE tracking
            current_re_per_system: dict[str, float] = dict(per_system_initial_re)

            # Each year's RE target follows the perfect-foresight trajectory
            # (RE-1: initial -> TARGET_RE_PENETRATION over the horizon, steps
            # clamped to the annual increments), not last year's outcome.
            min_inc = getattr(primary, 'min_annual_increment', 0.01)
            max_inc = getattr(primary, 'max_annual_increment', 0.10)
            re_paths = [re_target_trajectory(overall_initial_re, target_re, num_years,
                                             min_inc, max_inc)]
            if system_node_ranges:
                re_paths = [re_target_trajectory(init, target_re, num_years, min_inc, max_inc)
                            for _, _, _, init in system_node_ranges]

            # Accumulation state
            investments_by_year: dict[int, dict] = {}
            retirements_by_year: dict[int, dict] = {}
            cumulative_caps_by_year: dict[int, dict] = {}
            # Capacity built so far: {"tech" | "bat_tech_power" |
            # "bat_tech_capacity": {(technology key, build year index): per-node array}}
            built: dict[str, dict[tuple[str, int], np.ndarray]] = {
                "tech": {}, "bat_tech_power": {}, "bat_tech_capacity": {}}
            # Electrolyzer power the master built: {key: {year index: MW per node}}
            elz_builds: dict[str, dict[int, list[float]]] = {}
            current_re = overall_initial_re

            for y_idx, year in enumerate(years_range):
                logger.debug(f"\n--- Myopic: Year {year} ({y_idx+1}/{num_years}) ---")

                # 1. This year's RE target on the horizon trajectory
                year_targets = {(s + 1, 1): path[y_idx] for s, path in enumerate(re_paths)}
                year_target_re = re_paths[0][y_idx]
                logger.debug(f"  RE target: {year_target_re:.2%} (current: {current_re:.2%})")

                # 2. The master system as this year sees it: units aged,
                # earlier years' builds as existing units, limits reduced
                year_system, built_gens, built_bats = self._prepare_system_for_myopic_year(
                    y_idx, built)
                # Fuel storage, routes and electrolyzers earlier years built
                # (#89): existing capacity now, less room to build. Storage
                # and routes never retire; an electrolyzer build retires
                # after its life_time, as in perfect foresight.
                fuel_carried = ((getattr(self, "_master_fuel_by_year", None) or {})
                                .get(years_range[y_idx - 1]) if y_idx > 0 else None)
                elz_live, elz_used = self._myopic_electrolyzers(elz_builds, year_system, y_idx)
                if fuel_carried:
                    fuel_carried = dict(fuel_carried, electrolyzer=elz_live)
                year_system = self._system_with_electrolyzers(year_system, elz_live, elz_used)
                # Built units read their technology's profile
                year_avail_cache = dict(self._availability_cache or {})
                for unit_key, tech_key in built_gens:
                    if tech_key in year_avail_cache:
                        year_avail_cache[unit_key] = year_avail_cache[tech_key]

                # 3. Extract this year's demand
                yr_start_raw = y_idx * hours_per_year_raw
                yr_end_raw = min((y_idx + 1) * hours_per_year_raw, master_demand.shape[0])
                year_demand_raw = master_demand[yr_start_raw:yr_end_raw, :]

                # Aggregate to configured resolution
                if resolution_hours > 1:
                    year_demand_agg = aggregate_demand_to_resolution(
                        year_demand_raw, target_hours=resolution_hours
                    )
                else:
                    year_demand_agg = year_demand_raw

                # 4. TSAM clustering for this single year
                use_tsam = master_cfg.use_tsam if master_cfg else False
                tsam_starts_y = []
                tsam_weights_y = []
                tsam_order_y = []

                if use_tsam:
                    from esfex.models.tsam import compute_tsam_periods

                    tsam_num_periods = master_cfg.tsam_num_periods if master_cfg else 10
                    tsam_method = master_cfg.tsam_method if master_cfg else "kmedoids"

                    # Build RE availability for this year's clustering
                    year_availability = None
                    if self._availability_cache:
                        year_avail_dict: dict[str, np.ndarray] = {}
                        for gen_key, gen in self.primary_system.generators.items():
                            if gen.type != "Renewable" or gen_key not in self._availability_cache:
                                continue
                            avail = self._availability_cache[gen_key]
                            if resolution_hours > 1:
                                avail = aggregate_to_resolution(avail, target_hours=resolution_hours)
                            year_avail_dict[gen_key] = _profile_year_block(
                                avail, y_idx, hours_per_year_agg)
                        if year_avail_dict:
                            year_availability = year_avail_dict

                    tsam_result = compute_tsam_periods(
                        demand=year_demand_agg,
                        num_periods=tsam_num_periods,
                        method=tsam_method,
                        period_length_hours=24 // resolution_hours,
                        availability=year_availability,
                    )
                    # 1-indexed for Julia (no year offset since single year)
                    tsam_starts_y = [[s + 1 for s in tsam_result.period_start_hours]]
                    tsam_weights_y = [tsam_result.period_weights]
                    tsam_order_y = [[i + 1 for i in tsam_result.chronological_order]]

                # 5. Stochastic scenarios
                use_stochastic = master_cfg.stochastic if master_cfg else False
                stochastic_scenario_dicts: list[dict] = []
                if use_stochastic:
                    scenarios = getattr(primary, 'stochastic_scenarios', [])
                    if scenarios:
                        total_prob = sum(s.probability for s in scenarios)
                        if abs(total_prob - 1.0) > 1e-6:
                            use_stochastic = False
                        else:
                            for sc in scenarios:
                                sc_dict = {
                                    "name": sc.name,
                                    "probability": sc.probability,
                                    "multipliers": sc.multipliers.model_dump()
                                    if hasattr(sc.multipliers, 'model_dump')
                                    else {},
                                }
                                stochastic_scenario_dicts.append(sc_dict)

                # 6. Build per-system node ranges with current RE for this year
                year_sys_ranges: list[tuple] = []
                if system_node_ranges:
                    for name, first_bus, n_bus, _ in system_node_ranges:
                        sys_current_re = current_re_per_system.get(name, current_re)
                        year_sys_ranges.append((name, first_bus, n_bus, sys_current_re))

                # 7. Create single-year MasterProblem
                master = MasterProblemAdapter(
                    config=self.config,
                    years=[year],
                    base_year=year,
                    demand=year_demand_agg,
                    demand_growth=0.0,  # No growth within single year
                    discount_rate=getattr(primary, 'discount_rate', 0.05),
                    max_annual_investment=getattr(primary, 'max_annual_system_cost', 1e9),
                    target_re_penetration=year_target_re,
                    initial_re_penetration=current_re,
                    min_re_increment=min_inc,
                    max_re_increment=max_inc,
                    system_node_ranges=year_sys_ranges,
                    re_target_override=year_targets,
                    temporal_resolution_hours=resolution_hours,
                    investment_resolution_hours=getattr(temporal, 'investment_resolution', HOURS_STD_YEAR),
                    representative_days_per_year=master_cfg.representative_days if master_cfg else 5,
                    min_day_separation=master_cfg.min_day_separation if master_cfg else 5,
                    solver_method=master_cfg.solver_method if master_cfg else "monolithic",
                    benders_max_iterations=master_cfg.benders_max_iterations if master_cfg else 50,
                    benders_tolerance=master_cfg.benders_tolerance if master_cfg else 1e-4,
                    use_tsam=use_tsam,
                    tsam_period_start_hours=tsam_starts_y,
                    tsam_period_weights=tsam_weights_y,
                    tsam_chronological_order=tsam_order_y,
                    tsam_inter_period_linking=(
                        master_cfg.tsam_inter_period_linking if master_cfg else True
                    ),
                    use_stochastic=use_stochastic,
                    stochastic_scenarios=stochastic_scenario_dicts,
                    config_path=str(self.config_path) if self.config_path else None,
                    availability_cache=year_avail_cache,
                    inflow_cache=getattr(self, "_inflow_cache", None),
                    date_start=self._data_start().strftime("%d/%m/%Y %H:%M"),
                    # This year's block of multi-year profiles (horizon hour
                    # y_idx × 8760, wrapping around), as in the dispatch
                    profile_start_hour=y_idx * hours_per_year_raw,
                    system_config=year_system,
                    primary_energy=self._master_fuel_kwargs(
                        year_system, year,
                        cumulative=fuel_carried and {k: fuel_carried.get(k, {})
                                                     for k in ("storage", "transport")}),
                )

                # 7. Solve
                master.build_model(use_representative_days=True)
                status = master.solve()

                if status != 1:
                    logger.error(f"Myopic year {year}: not optimal (status={status})")
                    # Continue with empty investments for this year
                    investments_by_year[year] = {}
                    retirements_by_year[year] = {}
                    if fuel_carried:
                        self._master_fuel_by_year[year] = deepcopy(fuel_carried)
                    continue

                result = master.get_solution_values()
                self._store_master_fuel(master, result, [year], carried=fuel_carried)
                to_caps = getattr(master, "fuel_capacities_by_year", None)
                year_elz = ((to_caps(result) if to_caps is not None else {})
                            .get(1, {}).get("electrolyzer", {}))
                for key, per_node in year_elz.items():
                    elz_builds.setdefault(key, {})[y_idx] = list(per_node)
                solution = result.get('solution', {})
                year_data = solution.get(year, {})

                # 8. Extract investments
                year_inv: dict[str, float] = {}
                for category in ['tech_investment', 'bat_tech_power_investment',
                                 'bat_tech_capacity_investment', 'transfer_investment',
                                 'converter_investment', 'reservoir_investment']:
                    for key, value in year_data.get(category, {}).items():
                        if value > 0.1:
                            year_inv[key] = float(value)

                investments_by_year[year] = year_inv

                # 9. Extract retirements (of the original units; the built
                # ones retire at their technology's lifetime)
                n_gen0 = len(self.master_system.generators)
                year_ret: dict[str, float] = {}
                gen_life_ext = result.get('gen_life_extension', {})
                if 1 in gen_life_ext:  # year_idx=1 for single year
                    for gen_idx, node_values in gen_life_ext[1].items():
                        if int(gen_idx) > n_gen0:
                            continue
                        py_idx = int(gen_idx) - 1
                        retired = self._retired_share(py_idx, node_values)
                        if retired > 0.01:
                            year_ret[f'gen_{py_idx}'] = retired
                retirements_by_year[year] = year_ret

                # 10. Capacity in service this year, for the dispatch
                cumulative_caps_by_year[year] = self._myopic_year_caps(
                    result, built_gens, built_bats)

                # 11. Accumulate investments for next year
                self._accumulate_myopic_investments(year_inv, y_idx, built)

                # 12. Update current RE penetration (global + per-system)
                re_pen = result.get('re_penetration_by_year', [])
                if re_pen:
                    current_re = float(re_pen[0]) if isinstance(re_pen, list) else float(re_pen)
                else:
                    current_re = year_target_re

                # Update per-system RE tracking
                re_per_sys = result.get('re_penetration_by_system', {})
                for sname, re_vals in re_per_sys.items():
                    if isinstance(re_vals, (list, tuple)) and re_vals:
                        current_re_per_system[sname] = float(re_vals[0])
                    elif isinstance(re_vals, (int, float)):
                        current_re_per_system[sname] = float(re_vals)

                total_year_inv = sum(year_inv.values())
                re_sys_str = ", ".join(f"{s}={v:.1%}" for s, v in current_re_per_system.items())
                logger.debug(
                    f"  Year {year}: {len(year_inv)} investments "
                    f"({total_year_inv:.0f} MW total), "
                    f"{len(year_ret)} retirements, "
                    f"RE achieved: {current_re:.2%} (per-system: {re_sys_str})"
                )

            # Summary
            total_inv = sum(len(v) for v in investments_by_year.values())
            total_ret = sum(len(v) for v in retirements_by_year.values())
            logger.debug(
                f"\nMyopic planning complete: {total_inv} investments, "
                f"{total_ret} retirements across {num_years} years"
            )

            # Build RE targets from tracked values
            re_targets_by_year = {}
            for y_idx, year in enumerate(years_range):
                if y_idx == 0:
                    re_targets_by_year[year] = overall_initial_re
                else:
                    progress = y_idx / (num_years - 1) if num_years > 1 else 1.0
                    re_targets_by_year[year] = overall_initial_re + progress * (target_re - overall_initial_re)

            return investments_by_year, retirements_by_year, cumulative_caps_by_year, re_targets_by_year

        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            logger.error(f"Myopic master problem failed: {e}\n{tb}")
            console.print(f"[bold red]Myopic master problem error:[/bold red] {e}")
            console.print(f"[dim]{tb}[/dim]")
            return None, None, None, None

    def _prepare_system_for_myopic_year(
        self,
        y_idx: int,
        built: dict[str, dict[tuple[str, int], np.ndarray]],
    ) -> tuple[SystemConfig, list[tuple[str, str]], list[tuple[str, str]]]:
        """The merged master system as myopic year *y_idx* sees it.

        Starts from ``self.master_system`` (node-level, keys
        ``{system}__{key}`` in a multi-system run):

        * existing generators and batteries are *y_idx* years older;
        * capacity built in earlier years (*built*, see
          :meth:`_accumulate_myopic_investments`) becomes existing units with
          their technology's parameters (:func:`technology_unit_fields`), at
          the nodes where it was built, one unit per technology and build
          year, aged from the build year with the technology's degradation
          and retired at its lifetime, as perfect foresight counts its
          investments. On a bus-level master (a system with buses: one node,
          or development zones) each bus with capacity gets its own unit,
          pinned to that bus (``bus_id_per_node``); the adapter would place a
          node-level unit at the node's first bus;
        * each technology's ``invest_max`` is reduced by what was built (the
          limit holds for the whole horizon, as in perfect foresight).

        Returns:
            ``(system, built_gens, built_bats)``: the year's system and the
            ``(unit key, technology key)`` of the built units it holds, in
            the order they follow the original units.
        """
        from esfex.config.schema import BatteryConfig, GeneratorConfig

        base = self.master_system
        sys = base.model_copy(deep=True)
        n = base.num_nodes

        for gen in sys.generators.values():
            gen.initial_age = [age + y_idx for age in gen.initial_age]
        for bat in sys.batteries.values():
            bat.initial_age = [age + y_idx for age in bat.initial_age]

        def rep(arr, default=0.0):
            return _per_node_list(arr, n, default)

        def _reduce(limits, used):
            return [max(0.0, lim - float(used[i])) for i, lim in enumerate(rep(limits))]

        # *built* arrays are per master bus: one per node on a node-level
        # master, the system's buses otherwise
        from esfex.bridge.converters import node_bus_mapping_required
        buses = base.buses if node_bus_mapping_required(base.buses, n) else []

        def per_node(arr):
            if not buses:
                return arr
            out = np.zeros(n)
            for b, bus in enumerate(buses[:len(arr)]):
                out[bus.parent_node] += arr[b]
            return out

        def pieces(*arrays):
            """``(key suffix, bus_id_per_node, per-node arrays)`` of each unit
            to build from per-master-bus *arrays*."""
            if not buses:
                yield "", None, arrays
                return
            for b, bus in enumerate(buses):
                if any(b < len(a) and a[b] > 0 for a in arrays):
                    node_arrays = []
                    for a in arrays:
                        na = np.zeros(n)
                        na[bus.parent_node] = a[b] if b < len(a) else 0.0
                        node_arrays.append(na)
                    yield f"__{bus.bus_id}", {bus.parent_node: bus.bus_id}, node_arrays

        built_gens: list[tuple[str, str]] = []
        used: dict[str, np.ndarray] = {}
        for (tech_key, build_y), power in sorted(built["tech"].items(), key=lambda kv: kv[0][1]):
            tech = base.technologies.get(tech_key)
            if tech is None:
                continue
            used[tech_key] = used.get(tech_key, np.zeros(n)) + per_node(power)
            age = y_idx - build_y
            if age >= tech.lifetime or not np.any(power > 0):
                continue
            for suffix, bus_ids, (node_power,) in pieces(power):
                unit_key = f"{tech_key}__built_y{build_y + 1}{suffix}"
                fields = technology_unit_fields(tech, rep)
                fields.update(
                    name=f"{tech.name} (built in year {build_y + 1})",
                    technology=tech_key,
                    rated_power=[float(p) for p in node_power],
                    life_time=[tech.lifetime] * n,
                    initial_age=[age] * n,
                    degradation_rate=rep(tech.degradation_rate),
                    # Life extension is priced like a new build
                    invest_cost=rep(tech.invest_cost),
                    invest_max_power=[0.0] * n,
                    decommissioning_cost=rep(tech.decommissioning_cost),
                    bus_id_per_node=bus_ids,
                )
                sys.generators[unit_key] = GeneratorConfig(**fields)
                built_gens.append((unit_key, tech_key))
        for tech_key, total in used.items():
            tech = sys.technologies[tech_key]
            tech.invest_max_power = _reduce(tech.invest_max_power, total)

        built_bats: list[tuple[str, str]] = []
        used_pow: dict[str, np.ndarray] = {}
        used_cap: dict[str, np.ndarray] = {}
        bat_keys = sorted(set(built["bat_tech_power"]) | set(built["bat_tech_capacity"]),
                          key=lambda k: k[1])
        for bt_key, build_y in bat_keys:
            bat_tech = base.battery_technologies.get(bt_key)
            if bat_tech is None:
                continue
            n_master = len(buses) or n
            power = built["bat_tech_power"].get((bt_key, build_y), np.zeros(n_master))
            energy = built["bat_tech_capacity"].get((bt_key, build_y), np.zeros(n_master))
            used_pow[bt_key] = used_pow.get(bt_key, np.zeros(n)) + per_node(power)
            used_cap[bt_key] = used_cap.get(bt_key, np.zeros(n)) + per_node(energy)
            age = y_idx - build_y
            if age >= bat_tech.lifetime or not (np.any(power > 0) or np.any(energy > 0)):
                continue
            for suffix, bus_ids, (node_power, node_energy) in pieces(power, energy):
                unit_key = f"{bt_key}__built_y{build_y + 1}{suffix}"
                fields = battery_technology_unit_fields(bat_tech, rep)
                fields.pop("_type")
                pow_list = [float(p) for p in node_power]
                zeros = [0.0] * n
                fields.update(
                    name=f"{bat_tech.name} (built in year {build_y + 1})",
                    technology=bt_key,
                    capacity=[float(e) for e in node_energy],
                    MaxChargePower=pow_list,
                    MaxDischargePower=pow_list,
                    rated_power=pow_list,
                    min_power=zeros, eff_at_rated=[1.0] * n, eff_at_min=[1.0] * n,
                    ramp_up=[1.0] * n, ramp_down=[1.0] * n, min_up=[0] * n, min_down=[0] * n,
                    fuel_cost=zeros, fixed_cost=zeros, start_up_cost=zeros,
                    life_time=[bat_tech.lifetime] * n,
                    initial_age=[age] * n,
                    degradation_rate=rep(bat_tech.degradation_rate),
                    # Life extension is priced like a new build
                    invest_cost=rep(bat_tech.invest_cost_power),
                    invest_cost_energy=rep(bat_tech.invest_cost_energy),
                    invest_max_power=zeros, invest_max_capacity=zeros,
                    decommissioning_cost=rep(bat_tech.decommissioning_cost),
                    bus_id_per_node=bus_ids,
                )
                sys.batteries[unit_key] = BatteryConfig(**fields)
                built_bats.append((unit_key, bt_key))
        for bt_key in used_pow:
            bat_tech = sys.battery_technologies[bt_key]
            bat_tech.invest_max_power = _reduce(bat_tech.invest_max_power, used_pow[bt_key])
            bat_tech.invest_max_capacity = _reduce(bat_tech.invest_max_capacity, used_cap[bt_key])

        # Converter capacity built so far is existing capacity; the limit
        # holds for the whole horizon
        for (kind, idx), mw in built.get("converter", {}).items():
            convs = getattr(sys, f"{kind}_converters", None) or []
            if idx < len(convs):
                convs[idx].rated_power_mva += mw
                convs[idx].invest_max_power = max(0.0, convs[idx].invest_max_power - mw)

        # Reservoir capacity built so far, likewise (MWh, at its node)
        gens = list(sys.generators.values())
        for (g, loc), mwh in built.get("reservoir", {}).items():
            node = self._master_index_node(loc)
            if g < len(gens) and 0 <= node < n:
                gen = gens[g]
                cap = _pad_zeros(gen.reservoir_capacity, n)
                lim = _pad_zeros(gen.reservoir_invest_max, n)
                cap[node] += mwh
                lim[node] = max(0.0, lim[node] - mwh)
                gen.reservoir_capacity, gen.reservoir_invest_max = cap, lim

        return sys, built_gens, built_bats

    def _myopic_year_caps(
        self,
        result: dict,
        built_gens: list[tuple[str, str]],
        built_bats: list[tuple[str, str]],
    ) -> dict[str, dict]:
        """Capacity in service in a one-year myopic master, for the dispatch.

        Same layout as the perfect-foresight ``cumulative_capacities_by_year``
        entries (read by :meth:`_build_config_from_cumulative`): the original
        units under ``gen`` / ``bat`` / ``bat_power``, and per technology the
        capacity built this year plus what is left (degraded, not yet
        retired) of the units built from it in earlier years.
        """
        n_gen0 = len(self.master_system.generators)
        n_bat0 = len(self.master_system.batteries)
        tech_idx = {k: i + 1 for i, k in enumerate(self.master_system.technologies)}
        bt_idx = {k: i + 1 for i, k in enumerate(self.master_system.battery_technologies)}

        def _year(name):
            return result.get(name, {}).get(1, {})

        def _arrays(name):
            return {k: np.array(v, dtype=float) for k, v in _year(name).items()}

        cumul_gen = _year('cumulative_gen_capacity')
        cumul_bat = _year('cumulative_bat_capacity')
        cumul_bat_pow = _year('cumulative_bat_power')
        caps = {
            'gen': {g: v for g, v in cumul_gen.items() if int(g) <= n_gen0},
            'bat': {b: v for b, v in cumul_bat.items() if int(b) <= n_bat0},
            'bat_power': {b: v for b, v in cumul_bat_pow.items() if int(b) <= n_bat0},
            'tech': _arrays('cumulative_tech_capacity'),
            'bat_tech_power': _arrays('cumulative_bat_tech_power'),
            'bat_tech_capacity': _arrays('cumulative_bat_tech_capacity'),
        }

        def _add(target, t_jl, values):
            if values is None:
                return
            values = np.array(values, dtype=float)
            target[t_jl] = target[t_jl] + values if t_jl in target else values

        for g, (_, tech_key) in enumerate(built_gens, start=n_gen0 + 1):
            _add(caps['tech'], tech_idx[tech_key], cumul_gen.get(g))
        for b, (_, bt_key) in enumerate(built_bats, start=n_bat0 + 1):
            _add(caps['bat_tech_power'], bt_idx[bt_key], cumul_bat_pow.get(b))
            _add(caps['bat_tech_capacity'], bt_idx[bt_key], cumul_bat.get(b))
        return caps

    def _accumulate_myopic_investments(
        self,
        year_inv: dict[str, float],
        y_idx: int,
        built: dict[str, dict[tuple[str, int], np.ndarray]],
    ) -> None:
        """Record a myopic year's technology investments in *built*.

        Keys of *year_inv* index the master's technologies and its network
        buses: one per node in the merged master system, the system's own
        buses on a bus-level master (one node, or development zones; the
        adapter lays a reduced network's results out on them). Entries go to
        ``built[kind][(technology key, y_idx)]`` as per-master-bus arrays, so
        :meth:`_prepare_system_for_myopic_year` can keep each build at its bus.
        """
        sys = self.master_system
        buses = sys.buses or []
        n = len(buses) or sys.num_nodes
        prefixes = (
            ("tech_investment_power_", "tech", list(sys.technologies)),
            ("bat_tech_investment_power_", "bat_tech_power", list(sys.battery_technologies)),
            ("bat_tech_investment_capacity_", "bat_tech_capacity", list(sys.battery_technologies)),
        )
        for inv_key, capacity in year_inv.items():
            conv_kind, conv_idx = _converter_investment_key(inv_key)
            if conv_kind is not None:
                conv = built.setdefault("converter", {})
                conv[(conv_kind, conv_idx)] = conv.get((conv_kind, conv_idx), 0.0) + capacity
                continue
            res_at = _reservoir_investment_key(inv_key)
            if res_at is not None:
                res = built.setdefault("reservoir", {})
                res[res_at] = res.get(res_at, 0.0) + capacity
                continue
            for prefix, kind, keys in prefixes:
                if not inv_key.startswith(prefix):
                    continue
                t_idx, bus_idx = (int(p) for p in inv_key[len(prefix):].split("_")[:2])
                if t_idx < len(keys) and 0 <= bus_idx < n:
                    arr = built[kind].setdefault((keys[t_idx], y_idx), np.zeros(n))
                    arr[bus_idx] += capacity
                break

    def _solve_master_problem_foresight(
        self,
        years_range: list[int],
        demand: np.ndarray,
        hours: int,
        num_nodes: int,
        ev_demand: Optional[np.ndarray] = None,
        total_demand: Optional[np.ndarray] = None,
    ) -> tuple[Optional[dict], Optional[dict]]:
        """
        Solve the master problem for capacity expansion for ALL years at once
        (perfect foresight mode).

        Args:
            years_range: List of years to plan for
            demand: Full demand array (all years)
            hours: Total hours in demand
            num_nodes: Number of nodes

        Returns:
            Tuple of (investments_by_year, retirements_by_year) dictionaries
            or (None, None) if infeasible
        """
        logger.debug(f"Solving master problem for years {years_range[0]}-{years_range[-1]}...")

        try:
            # CRITICAL: Use total_demand (with EV) if available, otherwise base demand
            # Must pass ALL years of demand for proper operational validation
            # The MasterProblem needs demand for each year to select representative days
            # and validate that investments are operationally feasible
            master_demand = total_demand if total_demand is not None else demand

            # Ensure we have enough data for all years
            num_years = len(years_range)
            hours_needed = num_years * HOURS_STD_YEAR
            if master_demand.shape[0] < hours_needed:
                # Tile demand to cover all years if needed
                tiles_needed = (hours_needed + master_demand.shape[0] - 1) // master_demand.shape[0]
                master_demand = np.tile(master_demand, (tiles_needed, 1))[:hours_needed, :]
                logger.warning(f"Demand data shorter than planning horizon, tiled to {master_demand.shape[0]} hours")

            logger.debug(f"  Master problem demand shape: {master_demand.shape} (hours x nodes)")

            # Get config values for master problem
            temporal = self.config.temporal
            master_cfg = getattr(self.config, 'master_problem', None)
            primary = self.primary_system

            # Get temporal resolution from config (default 1 hour if not specified)
            resolution_hours = getattr(temporal, 'resolution_hours', 1)

            # Aggregate demand to configured temporal resolution
            # Uses MAX aggregation to preserve peak demand for capacity planning
            if resolution_hours > 1:
                master_demand_aggregated = aggregate_demand_to_resolution(
                    master_demand,
                    target_hours=resolution_hours
                )
                logger.debug(
                    f"  Aggregated demand from {master_demand.shape[0]} to "
                    f"{master_demand_aggregated.shape[0]} timesteps "
                    f"(resolution: {resolution_hours}h)"
                )
            else:
                master_demand_aggregated = master_demand

            # Calculate initial RE penetration from existing renewable capacity
            # Use first year demand (before aggregation for more accurate calculation)
            first_year_hours = hours_for_year(years_range[0])
            first_year_demand = master_demand[:min(first_year_hours, master_demand.shape[0]), :]
            calculated_initial_re = self._calculate_initial_re_penetration(first_year_demand)

            # Use calculated value, or config value if explicitly set
            initial_re = getattr(primary, 'initial_re_penetration', None)
            if initial_re is None or initial_re == 0.0:
                initial_re = calculated_initial_re
            logger.debug(f"Initial RE penetration: {initial_re:.2%}")

            # Per-system RE for foresight mode
            foresight_per_system_re = self._calculate_per_system_initial_re(first_year_demand)
            foresight_sys_ranges: list[tuple] = []
            if self._system_node_offsets and len(self._system_node_offsets) > 1:
                sys_names = list(self._system_node_offsets.keys())
                for i, sname in enumerate(sys_names):
                    off = self._system_node_offsets[sname]
                    if i + 1 < len(sys_names):
                        cnt = list(self._system_node_offsets.values())[i + 1] - off
                    else:
                        cnt = first_year_demand.shape[1] - off
                    sys_init_re = foresight_per_system_re.get(sname, initial_re)
                    foresight_sys_ranges.append((sname, off + 1, cnt, sys_init_re))
                logger.debug(f"Per-system initial RE (foresight): {foresight_per_system_re}")

            # --- TSAM clustering (if enabled) ---
            use_tsam = master_cfg.use_tsam if master_cfg else False
            tsam_starts = []
            tsam_weights = []
            tsam_order = []

            if use_tsam:
                from esfex.models.tsam import compute_tsam_periods

                tsam_num_periods = master_cfg.tsam_num_periods if master_cfg else 10
                tsam_method = master_cfg.tsam_method if master_cfg else "kmedoids"
                hours_per_year = HOURS_STD_YEAR // resolution_hours

                logger.debug(
                    f"  Running TSAM clustering: {tsam_num_periods} periods, "
                    f"method={tsam_method}"
                )

                # Build RE availability profiles for TSAM clustering
                # This adds renewable capacity factor features so the clustering
                # captures correlations between demand and RE availability
                re_availability_for_tsam: dict[str, np.ndarray] | None = None
                if self._availability_cache:
                    re_avail_arrays: dict[str, np.ndarray] = {}
                    for gen_key, gen in self.primary_system.generators.items():
                        if gen.type != "Renewable":
                            continue
                        if gen_key not in self._availability_cache:
                            continue
                        avail = self._availability_cache[gen_key]
                        # Aggregate to same resolution as demand (MEAN for CF)
                        if resolution_hours > 1:
                            avail = aggregate_to_resolution(
                                avail, target_hours=resolution_hours
                            )
                        re_avail_arrays[gen_key] = avail
                    if re_avail_arrays:
                        re_availability_for_tsam = re_avail_arrays
                        logger.debug(
                            f"  TSAM: using {len(re_avail_arrays)} RE availability "
                            f"profiles as clustering features"
                        )

                for y_idx in range(num_years):
                    year_start = y_idx * hours_per_year
                    year_end = min(
                        (y_idx + 1) * hours_per_year,
                        master_demand_aggregated.shape[0],
                    )
                    year_demand = master_demand_aggregated[year_start:year_end, :]

                    # Extract year slice of RE availability profiles
                    year_availability = None
                    if re_availability_for_tsam:
                        year_availability = {}
                        for name, avail in re_availability_for_tsam.items():
                            # Horizon-hour rule of the dispatch: this year's
                            # block, wrapping around a shorter profile
                            year_avail = _profile_year_block(
                                avail, y_idx, year_end - year_start)
                            if year_avail.shape[0] > 0:
                                year_availability[name] = year_avail
                        if not year_availability:
                            year_availability = None

                    result = compute_tsam_periods(
                        demand=year_demand,
                        num_periods=tsam_num_periods,
                        method=tsam_method,
                        period_length_hours=24 // resolution_hours,
                        availability=year_availability,
                    )
                    # Offset start hours to absolute position + convert to 1-indexed for Julia
                    tsam_starts.append(
                        [s + year_start + 1 for s in result.period_start_hours]
                    )
                    tsam_weights.append(result.period_weights)
                    # Convert chronological order to 1-indexed for Julia
                    tsam_order.append([i + 1 for i in result.chronological_order])

                logger.debug(f"  TSAM clustering complete for {num_years} years")

            # --- Stochastic scenarios (if enabled) ---
            use_stochastic = master_cfg.stochastic if master_cfg else False
            stochastic_scenario_dicts: list[dict] = []

            if use_stochastic:
                scenarios = getattr(primary, 'stochastic_scenarios', [])
                if scenarios:
                    total_prob = sum(s.probability for s in scenarios)
                    if abs(total_prob - 1.0) > 1e-6:
                        logger.error(
                            f"Stochastic scenario probabilities sum to "
                            f"{total_prob:.4f}, must sum to 1.0. "
                            f"Falling back to deterministic."
                        )
                        use_stochastic = False
                    else:
                        for sc in scenarios:
                            sc_dict = {
                                "name": sc.name,
                                "probability": sc.probability,
                                "multipliers": sc.multipliers.model_dump()
                                if hasattr(sc.multipliers, 'model_dump')
                                else {},
                            }
                            stochastic_scenario_dicts.append(sc_dict)
                        logger.debug(
                            f"  Stochastic mode: {len(scenarios)} scenarios — "
                            + ", ".join(
                                f"{s.name} (p={s.probability:.2f})"
                                for s in scenarios
                            )
                        )
                else:
                    logger.warning(
                        "Stochastic mode enabled but no scenarios defined. "
                        "Falling back to deterministic."
                    )
                    use_stochastic = False

            # Create MasterProblem adapter
            master = MasterProblemAdapter(
                config=self.config,
                years=years_range,
                base_year=years_range[0],
                demand=master_demand_aggregated,  # Pass aggregated demand for operational validation
                # The demand above already holds every year (multi-year file,
                # tiled first year, EV S-curve); growing it again here made the
                # master plan for more demand than the dispatch serves.
                demand_growth=0.0,
                discount_rate=getattr(primary, 'discount_rate', 0.05),
                max_annual_investment=getattr(primary, 'max_annual_system_cost', 1e9),
                target_re_penetration=getattr(primary, 'target_re_penetration', 0.5),
                initial_re_penetration=initial_re,
                min_re_increment=getattr(primary, 'min_annual_increment', 0.0),
                max_re_increment=getattr(primary, 'max_annual_increment', 0.10),
                system_node_ranges=foresight_sys_ranges,
                # Use configured temporal resolution (e.g., 6 hours reduces model size 6x)
                temporal_resolution_hours=resolution_hours,
                investment_resolution_hours=getattr(temporal, 'investment_resolution', HOURS_STD_YEAR),
                representative_days_per_year=master_cfg.representative_days if master_cfg else 5,
                min_day_separation=master_cfg.min_day_separation if master_cfg else 5,
                solver_method=master_cfg.solver_method if master_cfg else "monolithic",
                benders_max_iterations=master_cfg.benders_max_iterations if master_cfg else 50,
                benders_tolerance=master_cfg.benders_tolerance if master_cfg else 1e-4,
                # TSAM parameters
                use_tsam=use_tsam,
                tsam_period_start_hours=tsam_starts,
                tsam_period_weights=tsam_weights,
                tsam_chronological_order=tsam_order,
                tsam_inter_period_linking=(
                    master_cfg.tsam_inter_period_linking if master_cfg else True
                ),
                # Stochastic parameters
                use_stochastic=use_stochastic,
                stochastic_scenarios=stochastic_scenario_dicts,
                # Config path for resolving relative availability/inflow paths
                config_path=str(self.config_path) if self.config_path else None,
                # Pre-loaded availability cache (includes zone-extended profiles)
                availability_cache=self._availability_cache,
                inflow_cache=getattr(self, "_inflow_cache", None),
                date_start=self._data_start().strftime("%d/%m/%Y %H:%M"),
                system_config=self.master_system,
                # Fuel balance, fuel infrastructure and electrolyzers (#89)
                primary_energy=self._master_fuel_kwargs(self.master_system, years_range[-1]),
            )

            # --- MGA mode ---
            mga_cfg = getattr(master_cfg, 'mga', None)
            use_mga = mga_cfg is not None and getattr(mga_cfg, 'enabled', False)

            if use_mga:
                from esfex.bridge.adapters import MGAAdapter

                logger.info("=" * 60)
                logger.info(
                    f"MGA MODE ENABLED  "
                    f"(K={mga_cfg.num_alternatives}, slack={mga_cfg.slack_fraction*100:.1f}%)"
                )
                logger.info("=" * 60)

                mga = MGAAdapter(master, mga_cfg)
                mga_result = mga.run(use_representative_days=True)

                # Export all alternatives to main results HDF5
                self._export_mga_to_hdf5(mga_result, years_range, self._hdf5_path)

                # Use cost-optimal (alternative 0) for operational dispatch
                result = mga_result['alternatives'][0]
            else:
                master.build_model(use_representative_days=True)

                # Export LP file for debugging
                lp_dir = self.output_dir / "logs"
                lp_dir.mkdir(parents=True, exist_ok=True)
                lp_file = lp_dir / "master_problem_debug.lp"
                master.write_lp(str(lp_file))
                logger.debug(f"Master problem LP exported to: {lp_file}")

                status = master.solve()

                if status != 1:  # Not optimal
                    logger.error(f"Master problem not optimal (status={status})")
                    return None, None

                # Extract investment decisions for each year
                result = master.get_solution_values()

            # Use the legacy-compatible solution structure keyed by actual year
            solution = result.get('solution', {})
            # Fuel storage, routes and electrolyzers, for the dispatch (#89)
            self._store_master_fuel(master, result, list(years_range))

            investments_by_year = {}
            retirements_by_year = {}

            # Also get structured data for retirement analysis
            gen_life_ext = result.get('gen_life_extension', {})

            for year in years_range:
                year_data = solution.get(year, {})

                # Collect technology investments - preserve per-node detail
                # key format: 'tech_investment_power_{t}_{n}'
                year_inv = {}
                tech_inv_data = year_data.get('tech_investment', {})
                for key, value in tech_inv_data.items():
                    if value > 0.1:
                        year_inv[key] = float(value)

                # Collect battery technology power investments
                # key format: 'bat_tech_investment_power_{bt}_{n}'
                bat_tech_pow_data = year_data.get('bat_tech_power_investment', {})
                for key, value in bat_tech_pow_data.items():
                    if value > 0.1:
                        year_inv[key] = float(value)

                # Collect battery technology capacity investments
                # key format: 'bat_tech_investment_capacity_{bt}_{n}'
                bat_tech_cap_data = year_data.get('bat_tech_capacity_investment', {})
                for key, value in bat_tech_cap_data.items():
                    if value > 0.1:
                        year_inv[key] = float(value)

                # Collect transmission investments
                # Keys already in format: 'transfer_investment_{i}_{j}'
                trans_inv_data = year_data.get('transfer_investment', {})
                for key, value in trans_inv_data.items():
                    if value > 0.1:
                        year_inv[key] = float(value)

                # Collect converter investments
                # key format: 'converter_investment_{acdc|freq}_{c}'
                for key, value in year_data.get('converter_investment', {}).items():
                    if value > 0.1:
                        year_inv[key] = float(value)

                # Collect reservoir investments (MWh)
                # key format: 'reservoir_investment_{g}_{n}'
                for key, value in year_data.get('reservoir_investment', {}).items():
                    if value > 0.1:
                        year_inv[key] = float(value)

                investments_by_year[year] = year_inv

                # Collect retirements (from life extension decisions)
                year_ret = {}
                y_idx = years_range.index(year)
                year_key = y_idx + 1  # Julia uses 1-based indexing for structured data

                if year_key in gen_life_ext:
                    for gen_idx, node_values in gen_life_ext[year_key].items():
                        # node_values: 1.0/0.0 per master location (bus on
                        # a bus-level master); share of the unit's capacity
                        py_idx = int(gen_idx) - 1  # Convert to 0-indexed
                        retired = self._retired_share(py_idx, node_values)
                        if retired > 0.01:  # threshold to avoid floating point noise
                            year_ret[f'gen_{py_idx}'] = retired

                retirements_by_year[year] = year_ret

            # Log summary
            total_inv = sum(len(v) for v in investments_by_year.values())
            total_ret = sum(len(v) for v in retirements_by_year.values())
            logger.info(f"Master problem solved: {total_inv} investments, {total_ret} retirements across {len(years_range)} years")

            # Log detailed investment/retirement information (verbose only)
            if self.config.solver.verbose:
                self._log_master_problem_details(result, years_range)

            # Build cumulative capacities per year from MasterProblem result
            # This correctly handles: existing (with age-based retirement + degradation)
            # + investments (each with its own age tracking)
            cumulative_capacities_by_year = {}
            cumul_gen = result.get('cumulative_gen_capacity', {})
            cumul_bat = result.get('cumulative_bat_capacity', {})
            cumul_bat_pow = result.get('cumulative_bat_power', {})
            cumul_tech = result.get('cumulative_tech_capacity', {})
            cumul_bat_tech_pow = result.get('cumulative_bat_tech_power', {})
            cumul_bat_tech_cap = result.get('cumulative_bat_tech_capacity', {})
            for year in years_range:
                y_idx = years_range.index(year)
                year_key = y_idx + 1  # Julia 1-based
                cumulative_capacities_by_year[year] = {
                    'gen': cumul_gen.get(year_key, {}),
                    'bat': cumul_bat.get(year_key, {}),
                    'bat_power': cumul_bat_pow.get(year_key, {}),
                    'tech': cumul_tech.get(year_key, {}),
                    'bat_tech_power': cumul_bat_tech_pow.get(year_key, {}),
                    'bat_tech_capacity': cumul_bat_tech_cap.get(year_key, {}),
                }

            # Extract RE penetration targets per year from master solution
            re_pen_by_year = result.get('re_penetration_by_year', None)
            re_targets_by_year = {}
            if re_pen_by_year is not None and hasattr(re_pen_by_year, '__len__'):
                for y_idx, year in enumerate(years_range):
                    if y_idx < len(re_pen_by_year):
                        re_targets_by_year[year] = float(re_pen_by_year[y_idx])

            return investments_by_year, retirements_by_year, cumulative_capacities_by_year, re_targets_by_year

        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            logger.error(f"Master problem failed: {e}\n{tb}")
            console.print(f"[bold red]Master problem error:[/bold red] {e}")
            console.print(f"[dim]{tb}[/dim]")
            return None, None, None, None

    def _export_mga_to_hdf5(
        self,
        mga_result: dict,
        years_range: list[int],
        hdf5_path: Optional[Path] = None,
    ) -> None:
        """
        Export MGA alternatives into the main results HDF5 file.

        Writes data under ``/mga/`` group:
            /mga/
                @attrs: num_alternatives, slack_fraction, optimal_cost, years
                alternative_0/  (cost-optimal)
                alternative_1/  (diversity alternative)
                ...
                generator_names, battery_names
                systems/  (per-system split, if multi-system)
        """
        import h5py

        if hdf5_path is None or not hdf5_path.exists():
            logger.warning("MGA export skipped: no HDF5 path provided")
            return

        num_alts = mga_result['num_alternatives']
        num_years = len(years_range)
        gen_names = self._gen_names
        bat_names = self._bat_names
        num_nodes = self._num_nodes

        with h5py.File(hdf5_path, "a") as f:
            # Remove previous MGA data if re-running
            if "mga" in f:
                del f["mga"]

            mga = f.create_group("mga")

            # Metadata
            mga.attrs["num_alternatives"] = num_alts
            mga.attrs["slack_fraction"] = mga_result['slack_fraction']
            mga.attrs["optimal_cost"] = mga_result['optimal_cost']
            mga.attrs["export_timestamp"] = datetime.now().isoformat()
            mga.attrs["years"] = years_range
            # SPORES roadmap (Phase 4): identify the generation method
            # at the group root so the viewer can pick the right colour
            # / labelling strategy. Defaults to "mga" for back-compat
            # when the adapter doesn't emit it (pre-Phase-3 caches).
            mga.attrs["method"] = mga_result.get('method', 'mga')
            # Distinct objectives in display order; useful as a legend
            # cache (the per-alt attrs below carry the authoritative tag).
            method_objectives = [
                alt.get('objective', 'hsj_diversity')
                for alt in mga_result['alternatives']
                if not alt.get('is_optimal', False)
            ]
            seen = set()
            unique_objectives = [
                o for o in method_objectives
                if not (o in seen or seen.add(o))
            ]
            mga.attrs["objectives"] = unique_objectives

            for alt in mga_result['alternatives']:
                alt_id = alt['alternative_id']
                grp = mga.create_group(f"alternative_{alt_id}")

                grp.attrs["alternative_id"] = alt_id
                grp.attrs["is_optimal"] = alt['is_optimal']
                grp.attrs["cost"] = alt['cost']
                if alt['diversity_objective'] is not None:
                    grp.attrs["diversity_objective"] = alt['diversity_objective']
                # SPORES roadmap (Phase 4): per-alt objective tag. For
                # the cost-optimal seed this is "cost_optimal"; for
                # method='mga' alts it's always "hsj_diversity"; for
                # method='spores' it's the SporesObjective enum value
                # that produced the alt (e.g. "max_tech_equity").
                grp.attrs["objective"] = alt.get(
                    'objective',
                    'cost_optimal' if alt['is_optimal'] else 'hsj_diversity'
                )

                # RE penetration
                re_pen = alt.get('re_penetration_by_year', np.zeros(num_years))
                if hasattr(re_pen, '__len__'):
                    grp.create_dataset("re_penetration", data=np.array(re_pen))

                solution = alt.get('solution', {})

                # Technology investments: (years, technologies, nodes)
                tech_names = list(self.primary_system.technologies.keys())
                tech_inv_arr = np.zeros((num_years, max(len(tech_names), 1), num_nodes))
                for y_idx, year in enumerate(years_range):
                    year_data = solution.get(year, {})
                    for key, val in year_data.get('tech_investment', {}).items():
                        parts = key.split('_')
                        t_idx = int(parts[-2])
                        n_idx = self._master_index_node(int(parts[-1]))
                        if t_idx < len(tech_names) and n_idx < num_nodes:
                            tech_inv_arr[y_idx, t_idx, n_idx] += val
                grp.create_dataset("tech_investment", data=tech_inv_arr)

                # Battery technology power investments: (years, bat_techs, nodes)
                bat_tech_names = list(self.primary_system.battery_technologies.keys())
                bat_tech_pow_arr = np.zeros((num_years, max(len(bat_tech_names), 1), num_nodes))
                for y_idx, year in enumerate(years_range):
                    year_data = solution.get(year, {})
                    for key, val in year_data.get('bat_tech_power_investment', {}).items():
                        parts = key.split('_')
                        bt_idx = int(parts[-2])
                        n_idx = self._master_index_node(int(parts[-1]))
                        if bt_idx < len(bat_tech_names) and n_idx < num_nodes:
                            bat_tech_pow_arr[y_idx, bt_idx, n_idx] += val
                grp.create_dataset("bat_tech_power_investment", data=bat_tech_pow_arr)

                # Battery technology capacity investments: (years, bat_techs, nodes)
                bat_tech_cap_arr = np.zeros((num_years, max(len(bat_tech_names), 1), num_nodes))
                for y_idx, year in enumerate(years_range):
                    year_data = solution.get(year, {})
                    for key, val in year_data.get('bat_tech_capacity_investment', {}).items():
                        parts = key.split('_')
                        bt_idx = int(parts[-2])
                        n_idx = self._master_index_node(int(parts[-1]))
                        if bt_idx < len(bat_tech_names) and n_idx < num_nodes:
                            bat_tech_cap_arr[y_idx, bt_idx, n_idx] += val
                grp.create_dataset("bat_tech_capacity_investment", data=bat_tech_cap_arr)

                # Transmission investments: stored as variable-length records
                trans_records = []
                for y_idx, year in enumerate(years_range):
                    year_data = solution.get(year, {})
                    node_transfers = self._master_investments_by_node(
                        year_data.get('transfer_investment', {}))
                    for key, val in node_transfers.items():
                        parts = key.split('_')
                        i_idx = int(parts[-2])
                        j_idx = int(parts[-1])
                        trans_records.append([year, i_idx, j_idx, val])
                if trans_records:
                    grp.create_dataset(
                        "transfer_investment",
                        data=np.array(trans_records),
                    )

                # Cumulative capacities
                cumul_gen = alt.get('cumulative_gen_capacity', {})
                cumul_gen_arr = np.zeros((num_years, len(gen_names), num_nodes))
                for y_idx_jl, gen_data in cumul_gen.items():
                    y_idx = int(y_idx_jl) - 1
                    if 0 <= y_idx < num_years:
                        for g_jl, node_vals in gen_data.items():
                            g_idx = int(g_jl) - 1
                            if 0 <= g_idx < len(gen_names):
                                vals = np.asarray(node_vals).ravel()
                                # Master returns per-bus vectors; collapse
                                # to per-node so the destination shape
                                # ``[year, gen, node]`` is respected even
                                # when master's bus count differs from the
                                # primary system's node count (e.g. when
                                # the master ran on a 19-bus topology
                                # while ``num_nodes`` reflects the merged
                                # 1-node system view).
                                if len(vals) > num_nodes:
                                    # Sum over buses into the single node
                                    # (or take max if you prefer peak); for
                                    # MGA reporting the sum is most natural.
                                    vals = np.array([vals.sum()])
                                n_to_copy = min(len(vals), num_nodes)
                                cumul_gen_arr[y_idx, g_idx, :n_to_copy] = vals[:n_to_copy]
                grp.create_dataset("cumulative_gen_capacity", data=cumul_gen_arr)

            # Store generator and battery names for reference
            if gen_names:
                mga.create_dataset(
                    "generator_names",
                    data=[n.encode('utf-8') for n in gen_names],
                )
            if bat_names:
                mga.create_dataset(
                    "battery_names",
                    data=[n.encode('utf-8') for n in bat_names],
                )

            # ── Per-system split of MGA alternatives ──
            if self._system_node_offsets and len(self._system_node_offsets) > 1:
                mapping = self._build_system_unit_mapping()
                mga.attrs["num_systems"] = len(mapping)

                systems_grp = mga.create_group("systems")
                for sname, smap in mapping.items():
                    sg = systems_grp.create_group(sname)
                    sg.attrs["node_offset"] = smap["node_offset"]
                    sg.attrs["node_count"] = smap["node_count"]

                    off = smap["node_offset"]
                    cnt = smap["node_count"]

                    # Filter technologies belonging to this system
                    sys_tech_indices: list[int] = []
                    tech_names_all = list(self.primary_system.technologies.keys())
                    for t_idx, tk in enumerate(tech_names_all):
                        if tk.startswith(f"{sname}__"):
                            sys_tech_indices.append(t_idx)

                    sys_bat_tech_indices: list[int] = []
                    bat_tech_names_all = list(self.primary_system.battery_technologies.keys())
                    for bt_idx, btk in enumerate(bat_tech_names_all):
                        if btk.startswith(f"{sname}__"):
                            sys_bat_tech_indices.append(bt_idx)

                    # Slice each alternative
                    for alt in mga_result['alternatives']:
                        alt_id = alt['alternative_id']
                        src_grp = mga[f"alternative_{alt_id}"]
                        alt_grp = sg.create_group(f"alternative_{alt_id}")
                        alt_grp.attrs["cost"] = alt['cost']

                        # Tech investments sliced: [years, sys_techs, sys_nodes]
                        if "tech_investment" in src_grp:
                            full = src_grp["tech_investment"][:]
                            if sys_tech_indices:
                                sliced = full[:, sys_tech_indices, :][:, :, off:off + cnt]
                                alt_grp.create_dataset("tech_investment", data=sliced)

                        # Battery tech power investments
                        if "bat_tech_power_investment" in src_grp:
                            full = src_grp["bat_tech_power_investment"][:]
                            if sys_bat_tech_indices:
                                sliced = full[:, sys_bat_tech_indices, :][:, :, off:off + cnt]
                                alt_grp.create_dataset("bat_tech_power_investment", data=sliced)

                        # Battery tech capacity investments
                        if "bat_tech_capacity_investment" in src_grp:
                            full = src_grp["bat_tech_capacity_investment"][:]
                            if sys_bat_tech_indices:
                                sliced = full[:, sys_bat_tech_indices, :][:, :, off:off + cnt]
                                alt_grp.create_dataset("bat_tech_capacity_investment", data=sliced)

                        # Cumulative gen capacity — filter by system gen indices
                        if "cumulative_gen_capacity" in src_grp:
                            full = src_grp["cumulative_gen_capacity"][:]
                            sys_g_idxs = smap["gen_indices"]
                            if sys_g_idxs:
                                sliced = full[:, sys_g_idxs, :][:, :, off:off + cnt]
                                alt_grp.create_dataset("cumulative_gen_capacity", data=sliced)

                    # Store per-system gen/bat names
                    if smap["gen_names"]:
                        sg.create_dataset(
                            "generator_names",
                            data=[n.encode('utf-8') for n in smap["gen_names"]],
                        )
                    if smap["bat_names"]:
                        sg.create_dataset(
                            "battery_names",
                            data=[n.encode('utf-8') for n in smap["bat_names"]],
                        )

        logger.info(f"MGA: Exported {num_alts} alternatives to {hdf5_path}:/mga")
        console.print(
            f"[bold green]MGA results exported to:[/bold green] {hdf5_path} (mga group)"
        )

    def _master_index_node(self, idx: int) -> int:
        """Node of index *idx* of the master's per-location results.

        The master solves on the system's buses when they are not one per
        node (``node_bus_mapping_required``: a node with several buses,
        development zones), so ``tech_investment_power_{t}_{n}``,
        ``transfer_investment_{i}_{j}`` and the life-extension arrays are
        indexed by bus there and by node otherwise.
        """
        from esfex.bridge.converters import node_bus_mapping_required

        base = self.master_system
        buses = base.buses or []
        if buses and node_bus_mapping_required(buses, base.num_nodes) and 0 <= idx < len(buses):
            return buses[idx].parent_node
        return idx

    def _master_investments_by_node(self, investments: dict) -> dict:
        """*investments* (``{key: value}`` of the master) with each key's
        location index turned into its node, values of a node's buses summed
        (``transfer_investment`` within one node dropped). Identity on a
        node-level master."""
        out: dict = {}
        for key, value in investments.items():
            parts = str(key).split("_")
            try:
                if key.startswith("transfer_investment_") and len(parts) >= 4:
                    i, j = self._master_index_node(int(parts[2])), self._master_index_node(int(parts[3]))
                    if i == j:
                        continue
                    key = f"transfer_investment_{i}_{j}"
                elif key.startswith(("tech_investment_", "bat_tech_investment_",
                                     "reservoir_investment_")):
                    parts[-1] = str(self._master_index_node(int(parts[-1])))
                    key = "_".join(parts)
            except ValueError:
                pass
            out[key] = out[key] + float(value) if key in out else value
        return out

    def _retired_share(self, gen_idx: int, flags) -> float:
        """Share of existing master generator *gen_idx* (0-based) retired.

        *flags* is its life-extension status per master location (1 in
        service, 0 past its lifetime), per bus on a bus-level master. Each
        node is retired as far as its locations are, weighted by the unit's
        ``rated_power`` there; a unit without capacity takes the plain share
        of retired nodes.
        """
        vals = np.atleast_1d(np.asarray(flags, dtype=float))
        base = self.master_system
        n = base.num_nodes
        retired = np.zeros(n)
        for i, status in enumerate(vals):
            node = self._master_index_node(i)
            if 0 <= node < n:
                retired[node] = max(retired[node], 1.0 - status)
        gens = list(base.generators.values())
        rated = (np.array(_per_node_list(gens[gen_idx].rated_power, n), dtype=float)
                 if 0 <= gen_idx < len(gens) else np.zeros(n))
        if rated.sum() > 0:
            return float((retired * rated).sum() / rated.sum())
        return float(retired.mean()) if n else 0.0

    def _log_master_problem_details(
        self,
        result: dict,
        years_range: list[int],
    ) -> None:
        """
        Log detailed investment and retirement information from master problem.

        Displays tables with:
        - Nodes in ROWS, Technologies in COLUMNS (inverted format)
        - Demand and RE target summary table per year
        - Separate tables for each system in multi-system mode

        Args:
            result: Solution dictionary from master problem
            years_range: List of planning years
        """
        from rich.table import Table

        solution = result.get('solution', {})
        gen_life_ext = result.get('gen_life_extension', {})

        gen_names = self._gen_names
        bat_names = self._bat_names
        num_nodes = self._num_nodes
        system_name = self.system_name

        # Get rated power/capacity for converting retirement fractions to MW/MWh
        gen_rated = np.zeros((len(gen_names), num_nodes))
        bat_rated_pow = np.zeros((len(bat_names), num_nodes))
        bat_rated_cap = np.zeros((len(bat_names), num_nodes))

        for g, (key, gen) in enumerate(self.primary_system.generators.items()):
            rated = gen.rated_power if hasattr(gen, 'rated_power') else []
            for n in range(min(len(rated), num_nodes)):
                gen_rated[g, n] = rated[n]

        for b, (key, bat) in enumerate(self.primary_system.batteries.items()):
            rated_p = bat.rated_power if hasattr(bat, 'rated_power') else []
            rated_c = bat.capacity if hasattr(bat, 'capacity') else []
            for n in range(min(len(rated_p), num_nodes)):
                bat_rated_pow[b, n] = rated_p[n]
            for n in range(min(len(rated_c), num_nodes)):
                bat_rated_cap[b, n] = rated_c[n]

        # Per-node investment accumulators. Investments are per candidate
        # technology (not per existing unit); the master's last index is a
        # bus on a bus-level master, summed onto its node here.
        tech_names = [t.name or k for k, t in self.master_system.technologies.items()]
        bat_tech_names = [t.name or k
                          for k, t in self.master_system.battery_technologies.items()]
        master_node = self._master_index_node
        gen_inv = np.zeros((len(tech_names), num_nodes))
        bat_inv_pow = np.zeros((len(bat_tech_names), num_nodes))
        bat_inv_cap = np.zeros((len(bat_tech_names), num_nodes))
        trans_inv = {}  # {(from node, to node): MW}
        gen_ret_frac = np.zeros((len(gen_names), num_nodes))  # Retirement fraction
        bat_ret_frac = np.zeros((len(bat_names), num_nodes))  # Retirement fraction

        for year in years_range:
            year_data = solution.get(year, {})

            # Technology investments: key = 'tech_investment_power_{t}_{n}'
            for key, value in year_data.get('tech_investment', {}).items():
                parts = key.split('_')
                if len(parts) >= 5:
                    t_idx, n_idx = int(parts[3]), master_node(int(parts[4]))
                    if 0 <= t_idx < len(tech_names) and 0 <= n_idx < num_nodes:
                        gen_inv[t_idx, n_idx] += float(value)

            # Battery technology power investments: key = 'bat_tech_investment_power_{bt}_{n}'
            for key, value in year_data.get('bat_tech_power_investment', {}).items():
                parts = key.split('_')
                if len(parts) >= 6:
                    bt_idx, n_idx = int(parts[4]), master_node(int(parts[5]))
                    if 0 <= bt_idx < len(bat_tech_names) and 0 <= n_idx < num_nodes:
                        bat_inv_pow[bt_idx, n_idx] += float(value)

            # Battery technology capacity investments: key = 'bat_tech_investment_capacity_{bt}_{n}'
            for key, value in year_data.get('bat_tech_capacity_investment', {}).items():
                parts = key.split('_')
                if len(parts) >= 6:
                    bt_idx, n_idx = int(parts[4]), master_node(int(parts[5]))
                    if 0 <= bt_idx < len(bat_tech_names) and 0 <= n_idx < num_nodes:
                        bat_inv_cap[bt_idx, n_idx] += float(value)

            # Transmission investments: key = 'transfer_investment_{i}_{j}'
            for key, value in year_data.get('transfer_investment', {}).items():
                parts = key.split('_')
                if len(parts) >= 4:
                    i_idx, j_idx = master_node(int(parts[2])), master_node(int(parts[3]))
                    link = (i_idx, j_idx)
                    trans_inv[link] = trans_inv.get(link, 0.0) + float(value)

            # Generator retirements from life extension data
            # FIXED: Use max instead of sum to count each retirement only ONCE
            # (life_ext is binary: 1=keep, 0=retire; once retired, stays retired)
            y_idx = years_range.index(year)
            year_key = y_idx + 1  # Julia 1-based indexing
            if year_key in gen_life_ext:
                for gen_idx, node_values in gen_life_ext[year_key].items():
                    gen_idx_int = int(gen_idx) - 1
                    if 0 <= gen_idx_int < len(gen_names):
                        if hasattr(node_values, '__len__'):
                            arr = np.array(node_values, dtype=float)
                            for i, status in enumerate(arr):
                                n = master_node(i)
                                retired = 1.0 - status
                                if retired > 0 and 0 <= n < num_nodes:
                                    gen_ret_frac[gen_idx_int, n] = max(gen_ret_frac[gen_idx_int, n], retired)
                        else:
                            retired = 1.0 - float(node_values)
                            if retired > 0:
                                gen_ret_frac[gen_idx_int, 0] = max(gen_ret_frac[gen_idx_int, 0], retired)

            # Battery retirements from life extension data
            # FIXED: Use max instead of sum to count each retirement only ONCE
            bat_life_ext = result.get('bat_life_extension', {})
            if year_key in bat_life_ext:
                for bat_idx, node_values in bat_life_ext[year_key].items():
                    bat_idx_int = int(bat_idx) - 1
                    if 0 <= bat_idx_int < len(bat_names):
                        if hasattr(node_values, '__len__'):
                            arr = np.array(node_values, dtype=float)
                            for i, status in enumerate(arr):
                                n = master_node(i)
                                retired = 1.0 - status
                                if retired > 0 and 0 <= n < num_nodes:
                                    bat_ret_frac[bat_idx_int, n] = max(bat_ret_frac[bat_idx_int, n], retired)
                        else:
                            retired = 1.0 - float(node_values)
                            if retired > 0:
                                bat_ret_frac[bat_idx_int, 0] = max(bat_ret_frac[bat_idx_int, 0], retired)

        # Convert retirement fractions to MW/MWh
        gen_ret_mw = gen_ret_frac * gen_rated
        bat_ret_mw = bat_ret_frac * bat_rated_pow
        bat_ret_mwh = bat_ret_frac * bat_rated_cap

        # Aggregate transmission investment per node (outgoing capacity)
        trans_per_node = np.zeros(num_nodes)
        for (i, j), power in trans_inv.items():
            if power > 0.1 and 0 <= i < num_nodes and i != j:
                trans_per_node[i] += power

        has_gen_inv = np.any(gen_inv > 0.1)
        has_bat_inv = np.any(bat_inv_pow > 0.1) or np.any(bat_inv_cap > 0.1)
        has_trans_inv = any(v > 0.1 for v in trans_inv.values())
        has_gen_ret = np.any(gen_ret_mw > 0.1)
        has_bat_ret = np.any(bat_ret_mw > 0.1) or np.any(bat_ret_mwh > 0.1)

        # =======================================================================
        # DEMAND AND RE TARGET TABLE (Years in rows)
        # =======================================================================
        console.print(f"\n[bold cyan]═══ SYSTEM: {system_name.upper()} ═══[/bold cyan]")

        # Get demand and RE target info
        # Use stored demand data from run() method
        hours_per_year = HOURS_STD_YEAR

        # Get first year base demand
        if hasattr(self, '_base_demand') and self._base_demand is not None:
            first_year_base = self._base_demand[:min(hours_per_year, len(self._base_demand))]
            annual_base_demand = np.sum(first_year_base)
            logger.debug(f"First year base demand: {annual_base_demand:.0f} MWh "
                       f"({annual_base_demand/1000:.1f} GWh)")
        else:
            annual_base_demand = 0
            logger.warning("Logging: No base demand found in self._base_demand")

        # Get EV demand from stored EV data (already has S-curve growth)
        if hasattr(self, '_ev_demand') and self._ev_demand is not None:
            first_year_ev = self._ev_demand[:min(hours_per_year, len(self._ev_demand))]
            ev_first_year_demand = np.sum(first_year_ev)
            logger.debug(f"First year EV demand: {ev_first_year_demand:.0f} MWh")
        else:
            ev_first_year_demand = 0

        # demand_growth is not used anymore - EV growth is embedded in profiles
        demand_growth = 0.0  # Base demand doesn't grow, only EV demand grows via S-curve
        re_target_final = self.primary_system.target_re_penetration if hasattr(self.primary_system, 'target_re_penetration') else 0.0

        # Calculate initial RE penetration from existing capacity if not set
        re_target_initial = getattr(self.primary_system, 'initial_re_penetration', None)
        if re_target_initial is None or re_target_initial == 0.0:
            # Calculate from existing capacity
            first_year_demand = self._base_demand[:min(hours_per_year, len(self._base_demand))]
            re_target_initial = self._calculate_initial_re_penetration(first_year_demand)
        logger.debug(f"RE target final={re_target_final}, initial={re_target_initial:.2%}")

        demand_table = Table(title=f"Demand & RE Target Summary - {system_name}", show_header=True, title_style="bold blue")
        demand_table.add_column("Year", style="bold", justify="center")
        demand_table.add_column("Base Demand (GWh)", justify="right")
        demand_table.add_column("EV Demand (GWh)", justify="right")
        demand_table.add_column("Total Demand (GWh)", justify="right")
        demand_table.add_column("RE Target (%)", justify="right")

        num_years = len(years_range)
        for y_idx, year in enumerate(years_range):
            # Extract actual demand for this year from stored arrays
            start_h = y_idx * hours_per_year
            end_h = min(start_h + hours_per_year,
                       len(self._base_demand) if hasattr(self, '_base_demand') and self._base_demand is not None else 0)

            if hasattr(self, '_base_demand') and self._base_demand is not None and end_h > start_h:
                year_base = self._base_demand[start_h:end_h]
                base_dem_gwh = np.sum(year_base) / 1000.0
            else:
                base_dem_gwh = annual_base_demand / 1000.0

            if hasattr(self, '_ev_demand') and self._ev_demand is not None and end_h > start_h:
                year_ev = self._ev_demand[start_h:end_h]
                ev_dem_gwh = np.sum(year_ev) / 1000.0
            else:
                ev_dem_gwh = 0.0

            total_dem_gwh = base_dem_gwh + ev_dem_gwh

            # Interpolate RE target
            if num_years > 1:
                re_target = re_target_initial + (re_target_final - re_target_initial) * (y_idx / (num_years - 1))
            else:
                re_target = re_target_final

            demand_table.add_row(
                str(year),
                f"{base_dem_gwh:,.1f}",
                f"{ev_dem_gwh:,.1f}" if ev_dem_gwh > 0.01 else "-",
                f"{total_dem_gwh:,.1f}",
                f"{re_target * 100:.1f}%"
            )

        console.print(demand_table)

        # =======================================================================
        # INVESTMENTS TABLE (Nodes in ROWS, Technologies in COLUMNS)
        # =======================================================================
        console.print(f"\n[bold cyan]Investment/Retirement Summary - {system_name}[/bold cyan]")

        if has_gen_inv or has_bat_inv or has_trans_inv:
            # Collect technologies with investments
            inv_gen_indices = [g for g in range(len(tech_names)) if np.any(gen_inv[g] > 0.1)]
            inv_bat_indices = [b for b in range(len(bat_tech_names)) if np.any(bat_inv_pow[b] > 0.1) or np.any(bat_inv_cap[b] > 0.1)]

            table = Table(title=f"Investments - {system_name}", show_header=True, title_style="bold green")
            table.add_column("Node", style="bold")

            # Add generator columns
            for g in inv_gen_indices:
                table.add_column(f"[green]{tech_names[g]}[/green] (MW)", justify="right")

            # Add battery columns (MW and MWh)
            for b in inv_bat_indices:
                table.add_column(f"[blue]{bat_tech_names[b]}[/blue] (MW)", justify="right")
                table.add_column(f"[blue]{bat_tech_names[b]}[/blue] (MWh)", justify="right")

            # Add transmission column if exists
            if has_trans_inv:
                table.add_column("[yellow]Trans Out[/yellow] (MW)", justify="right")

            # Add rows for each node
            for n in range(num_nodes):
                row = [f"Node {n}"]

                # Generator investments for this node
                for g in inv_gen_indices:
                    v = gen_inv[g, n]
                    row.append(f"{v:,.0f}" if v > 0.1 else "-")

                # Battery investments for this node
                for b in inv_bat_indices:
                    p, e = bat_inv_pow[b, n], bat_inv_cap[b, n]
                    row.append(f"{p:,.0f}" if p > 0.1 else "-")
                    row.append(f"{e:,.0f}" if e > 0.1 else "-")

                # Transmission for this node
                if has_trans_inv:
                    v = trans_per_node[n]
                    row.append(f"{v:,.0f}" if v > 0.1 else "-")

                table.add_row(*row)

            # Add TOTAL row
            table.add_section()
            total_row = ["[bold]TOTAL[/bold]"]
            for g in inv_gen_indices:
                total_row.append(f"[bold]{np.sum(gen_inv[g]):,.0f}[/bold]")
            for b in inv_bat_indices:
                total_row.append(f"[bold]{np.sum(bat_inv_pow[b]):,.0f}[/bold]")
                total_row.append(f"[bold]{np.sum(bat_inv_cap[b]):,.0f}[/bold]")
            if has_trans_inv:
                total_row.append(f"[bold]{np.sum(trans_per_node):,.0f}[/bold]")
            table.add_row(*total_row)

            console.print(table)

            # Log to file
            for g in inv_gen_indices:
                logger.debug(f"GEN_INV | {system_name} | {tech_names[g]}: " + " | ".join(
                    f"N{n}={gen_inv[g, n]:,.0f}MW" for n in range(num_nodes)))
            for b in inv_bat_indices:
                logger.debug(f"BAT_INV | {system_name} | {bat_tech_names[b]}: " + " | ".join(
                    f"N{n}={bat_inv_pow[b, n]:,.0f}MW/{bat_inv_cap[b, n]:,.0f}MWh" for n in range(num_nodes)))
            for (i, j), power in sorted(trans_inv.items()):
                if power > 0.1:
                    logger.debug(f"TRANS_INV | {system_name} | {i}→{j}: {power:.0f} MW")
        else:
            console.print("[dim]No investments[/dim]")

        # =======================================================================
        # RETIREMENTS TABLE (Nodes in ROWS, Technologies in COLUMNS)
        # =======================================================================
        if has_gen_ret or has_bat_ret:
            # Collect technologies with retirements
            ret_gen_indices = [g for g in range(len(gen_names)) if np.any(gen_ret_mw[g] > 0.1)]
            ret_bat_indices = [b for b in range(len(bat_names)) if np.any(bat_ret_mw[b] > 0.1) or np.any(bat_ret_mwh[b] > 0.1)]

            table = Table(title=f"Retirements - {system_name}", show_header=True, title_style="bold red")
            table.add_column("Node", style="bold")

            # Add generator columns
            for g in ret_gen_indices:
                table.add_column(f"[red]{gen_names[g]}[/red] (MW)", justify="right")

            # Add battery columns
            for b in ret_bat_indices:
                table.add_column(f"[magenta]{bat_names[b]}[/magenta] (MW)", justify="right")
                table.add_column(f"[magenta]{bat_names[b]}[/magenta] (MWh)", justify="right")

            # Add rows for each node
            for n in range(num_nodes):
                row = [f"Node {n}"]

                for g in ret_gen_indices:
                    v = gen_ret_mw[g, n]
                    row.append(f"{v:,.0f}" if v > 0.1 else "-")

                for b in ret_bat_indices:
                    p, e = bat_ret_mw[b, n], bat_ret_mwh[b, n]
                    row.append(f"{p:,.0f}" if p > 0.1 else "-")
                    row.append(f"{e:,.0f}" if e > 0.1 else "-")

                table.add_row(*row)

            # Add TOTAL row
            table.add_section()
            total_row = ["[bold]TOTAL[/bold]"]
            for g in ret_gen_indices:
                total_row.append(f"[bold]{np.sum(gen_ret_mw[g]):,.0f}[/bold]")
            for b in ret_bat_indices:
                total_row.append(f"[bold]{np.sum(bat_ret_mw[b]):,.0f}[/bold]")
                total_row.append(f"[bold]{np.sum(bat_ret_mwh[b]):,.0f}[/bold]")
            table.add_row(*total_row)

            console.print(table)

            # Log to file
            for g in ret_gen_indices:
                logger.debug(f"GEN_RET | {system_name} | {gen_names[g]}: " + " | ".join(
                    f"N{n}={gen_ret_mw[g, n]:,.0f}MW" for n in range(num_nodes)))
            for b in ret_bat_indices:
                logger.debug(f"BAT_RET | {system_name} | {bat_names[b]}: " + " | ".join(
                    f"N{n}={bat_ret_mw[b, n]:,.0f}MW/{bat_ret_mwh[b, n]:,.0f}MWh" for n in range(num_nodes)))
        else:
            console.print("[dim]No retirements[/dim]")

        console.print()

    def _warn_short_overlap(self, overlap_hours: int) -> None:
        """Warn once when storage dispatch is myopic because the look-ahead is short.

        Stored energy has no value at the end of a window, so a window whose
        look-ahead does not reach the next daily peak will not charge for it.
        """
        if getattr(self, "_short_overlap_warned", False):
            return
        sys = self.primary_system
        has_storage = bool(sys.batteries) or bool(getattr(sys, "battery_technologies", None)) or any(
            any(c > 0 for c in (getattr(g, "reservoir_capacity", None) or []))
            for g in sys.generators.values()
        )
        if not has_storage:
            return
        self._short_overlap_warned = True
        logger.warning(
            f"Rolling-horizon overlap is {overlap_hours} h (< 24 h) and the system has storage. "
            "Each window only sees that far past its kept hours, so storage may not charge "
            "for a peak just beyond it; set temporal.overlap_hours to 24 or more."
        )

    def _run_operational_dispatch(
        self,
        year: int,
        year_idx: int,
        num_years: int,
        demand: np.ndarray,
        hours: int,
        num_nodes: int,
        units_config: Optional[dict] = None,
        re_penetration_target: Optional[float] = None,
    ) -> YearResults:
        """
        Run operational dispatch using rolling horizon.

        Args:
            re_penetration_target: Year-specific RE target (0-1). If None, uses config default.

        Returns:
            YearResults for this year
        """
        logger.debug("Running operational dispatch...")

        temporal = self.config.temporal
        resolution_hours = getattr(temporal, 'resolution_hours', 1)
        rolling_hours = temporal.rolling_horizon_hours
        overlap = temporal.overlap_hours

        # Aggregate demand to configured temporal resolution using MEAN (not MAX).
        # MEAN preserves energy balance: sum(mean_demand × Δt) == sum(hourly_demand × 1h).
        # MAX is only used for MasterProblem strategic capacity planning.
        # Matches legacy main.py line 1718.
        if temporal.use_rolling_horizon and overlap < 24:
            self._warn_short_overlap(overlap)

        if resolution_hours > 1:
            demand = aggregate_to_resolution(demand, target_hours=resolution_hours)
            hours = len(demand)
            # Convert rolling horizon parameters to aggregated timesteps
            rolling_hours = max(1, rolling_hours // resolution_hours)
            overlap = overlap // resolution_hours
            logger.debug(
                f"Temporal aggregation: resolution={resolution_hours}h, "
                f"{hours} timesteps/year, window={rolling_hours} steps, overlap={overlap} steps"
            )

        if not temporal.use_rolling_horizon:
            rolling_hours = hours
            overlap = 0
        # Step rounding can make the overlap reach the window; keep at least one kept step.
        overlap = min(overlap, rolling_hours - 1)

        # Calculate number of windows.
        # Use ceiling division so the last window always reaches the end of the year.
        # With floor division, the final (hours % effective_hours) timesteps at the
        # end of the year were never dispatched, causing gen_output to be shorter than
        # the demand array and producing a 1-month-per-year temporal misalignment in charts.
        effective_hours = rolling_hours - overlap
        num_windows = max(1, (hours - overlap + effective_hours - 1) // effective_hours)

        logger.debug(f"Rolling horizon: {num_windows} windows of {rolling_hours} steps")

        # Collect results across windows
        all_gen_output = []
        all_gen_status = []
        all_gen_startup = []
        all_curtailment = []
        all_bat_charge = []
        all_bat_discharge = []
        all_bat_soc = []
        all_reserve_static = []
        all_reserve_dynamic = []
        all_loss_reserve_static = []
        all_loss_reserve_dynamic = []
        all_load_shed = []
        all_co2_emissions = []
        all_prices = []
        all_voltage_angle = []
        all_voltage_magnitude = []
        all_reactive_generation = []
        all_power_flow = []
        all_bat_spillage = []
        all_ev_charging = []
        all_ev_v2g = []
        all_ev_soc = []
        all_ev_loss = []
        all_loss_of_inertia = []
        all_transfer_margin = []
        all_reservoir_level = []
        all_reservoir_spillage = []
        all_reservoir_pump = []
        all_electrolyzer_power = []
        all_electrolyzer_h2 = []
        all_pe_results: list[dict] = []
        all_n1_gen_duals = []
        all_n1_binding: list[str] = []
        all_n1_trans_duals: list[dict] = []
        total_n1_security_cost = 0.0
        # Investment arrays (from last window only, not time-dependent)
        last_reservoir_invest = None
        last_gen_investment = None
        last_bat_inv_power = None
        last_bat_inv_capacity = None
        last_transfer_investment = None
        total_objective = 0.0
        total_solve_time = 0.0
        total_load_shed = 0.0
        total_curtailment = 0.0
        total_re_penetration = 0.0
        total_emissions = 0.0
        total_generation = 0.0
        total_demand_val = 0.0
        accumulated_cost_breakdown: dict[str, float] = {}
        feasible = True
        num_solved = 0

        boundary_conditions = self.state.boundary_conditions.copy()
        # Battery / generator / reservoir rosters can change between years
        # (new investments, retirements), so per-unit indexing from the prior
        # year cannot be safely reused. Reset all unit-indexed carry-over at
        # year boundary; chaining still works within the year across rolling
        # windows. PE fuel storage stays linked (named by fuel, not index).
        boundary_conditions.pop('battery_soc', None)
        boundary_conditions.pop('gen_status_init', None)
        boundary_conditions.pop('gen_output_prev', None)
        boundary_conditions.pop('reservoir_level', None)
        # Battery SOC at the start of the year (battery × bus, MWh), from window 1.
        year_start_soc = None

        # Extract year-specific sectoral demand (self._sectoral_demand covers full horizon)
        year_sectoral_demand = {}
        if self._sectoral_demand:
            hours_per_year = HOURS_STD_YEAR
            year_start = year_idx * hours_per_year
            year_end = min(year_start + hours_per_year, self._total_hours)
            for sector, full_arr in self._sectoral_demand.items():
                if year_end <= full_arr.shape[0]:
                    year_sectoral_demand[sector] = full_arr[year_start:year_end]
                else:
                    year_sectoral_demand[sector] = full_arr[:hours_per_year]
            # Aggregate sectoral demand to temporal resolution using MEAN
            # (matches legacy main.py line 1730)
            if resolution_hours > 1:
                for sector in year_sectoral_demand:
                    year_sectoral_demand[sector] = aggregate_to_resolution(
                        year_sectoral_demand[sector], target_hours=resolution_hours
                    )

        # Extract year-specific rooftop generation
        year_rooftop = None
        if self._rooftop_generation is not None:
            hours_per_year = HOURS_STD_YEAR
            rt_start = year_idx * hours_per_year
            rt_end = min(rt_start + hours_per_year, self._rooftop_generation.shape[0])
            if rt_end > rt_start:
                year_rooftop = self._rooftop_generation[rt_start:rt_end]
                # Aggregate rooftop generation to temporal resolution (MEAN for generation)
                if resolution_hours > 1:
                    year_rooftop = aggregate_to_resolution(
                        year_rooftop, target_hours=resolution_hours
                    )

        # Update window progress bar
        if self._progress and len(self._progress.task_ids) > 1:
            wt = self._progress.task_ids[1]
            self._progress.update(wt, completed=0, total=num_windows, visible=True,
                                  description=f"[dim]Year {year} windows[/dim]")

        for window in range(num_windows):
            start_hour = window * effective_hours
            end_hour = min(start_hour + rolling_hours, hours)

            # Update progress display
            if self._progress:
                self._progress.update(
                    self._progress.task_ids[0],
                    description=f"[cyan]Year {year}[/cyan] ({year_idx+1}/{num_years}) → Window {window+1}/{num_windows}"
                )

            logger.debug(f"Window {window+1}/{num_windows}: steps {start_hour}-{end_hour} ({start_hour*resolution_hours}h-{end_hour*resolution_hours}h)")

            # Diagnostic: log generator capacities for first window
            if window == 0 and units_config:
                for ukey, udata in units_config.items():
                    if udata.get("_type") != "battery" and udata.get("type") != "Storage":
                        rated = udata.get("rated_power", [])
                        name = udata.get("name", ukey)
                        logger.debug(f"  UNIT_CAP W0 | {name} ({ukey}): rated_power={rated}")

            # Extract window demand
            window_demand = demand[start_hour:end_hour]

            # Extract window-specific sectoral demand
            window_sectoral = {}
            for sector, year_arr in year_sectoral_demand.items():
                if end_hour <= year_arr.shape[0]:
                    window_sectoral[sector] = year_arr[start_hour:end_hour]

            # Behind-the-meter rooftop solar: subtract from demand before
            # passing to Julia.  Rooftop distributed generation reduces the
            # net demand seen by the grid; it is NOT dispatched by the operator.
            # This avoids multi-bus duplication issues where node-level rooftop
            # was incorrectly replicated to every bus in the DC-KCL balance.
            window_rooftop_raw = None
            if year_rooftop is not None and end_hour <= year_rooftop.shape[0]:
                window_rooftop_raw = year_rooftop[start_hour:end_hour]
                gross_demand = demand[start_hour:end_hour].copy()
                # Compute reduction ratio per (timestep, node) for sectoral scaling
                # ratio = net / gross (0 where gross is 0)
                with np.errstate(divide='ignore', invalid='ignore'):
                    demand_ratio = np.where(
                        gross_demand > 0,
                        np.maximum(gross_demand - window_rooftop_raw, 0.0) / gross_demand,
                        1.0,
                    )
                # Subtract rooftop from demand (cap at zero: excess is self-curtailed)
                window_demand = np.maximum(window_demand - window_rooftop_raw, 0.0)
                # Scale sectoral demand by the same ratio so constraints stay consistent
                for sector in window_sectoral:
                    window_sectoral[sector] = window_sectoral[sector] * demand_ratio
                rooftop_curtailed = np.maximum(window_rooftop_raw - gross_demand, 0.0)
                rooftop_used = window_rooftop_raw - rooftop_curtailed
                if window == 0:
                    logger.debug(
                        f"  Rooftop behind-the-meter: gross_demand={np.sum(gross_demand):.1f}, "
                        f"rooftop={np.sum(window_rooftop_raw):.1f}, "
                        f"net_demand={np.sum(window_demand):.1f} MW×steps"
                    )

            # Build EV config for this window (if EV data available)
            # EV profiles are stored at hourly resolution, so convert back to hourly indices
            ev_config_data = None
            if self._ev_charging_profiles is not None and self._v2g_availability_profiles is not None:
                ev_start_hourly = start_hour * resolution_hours
                ev_window_hourly = (end_hour - start_hour) * resolution_hours
                ev_config_data = self._build_ev_config(
                    year_idx, ev_start_hourly, ev_window_hourly,
                    resolution_hours=resolution_hours,
                )

            # Hours committed from this window: all but the overlap tail,
            # except for the last window. The next window starts right after.
            is_last_window = (window == num_windows - 1)
            keep_hours = (end_hour - start_hour) if is_last_window else effective_hours

            # The year must not end with less stored energy than it started
            # with (the next year restarts from soc_initial): the last window
            # gets a soft floor at the year-start SOC.
            window_bc = boundary_conditions
            if is_last_window:
                window_bc = dict(boundary_conditions)
                if year_start_soc is not None:
                    window_bc['battery_soc_end_min'] = year_start_soc
                elif window == 0:
                    window_bc['battery_soc_end_cyclic'] = True

            # Solve window
            # Pass start_hour in hourly units for adapter's availability cache slicing
            start_hour_hourly = start_hour * resolution_hours
            window_result = self._solve_window(
                year=year,
                window=window,
                demand=window_demand,  # NET demand (after rooftop subtraction)
                hours=end_hour - start_hour,
                num_nodes=num_nodes,
                boundary_conditions=window_bc,
                units_config=units_config,
                sectoral_demand=window_sectoral,
                ev_config_data=ev_config_data,
                start_hour=start_hour_hourly,
                rooftop_generation=None,  # Behind-the-meter: already in net demand
                re_penetration_target=re_penetration_target,
                keep_hours=keep_hours,
            )

            if window_result is None:
                logger.warning(f"Window {window+1} failed - marking year as infeasible")
                feasible = False
                if self._progress and len(self._progress.task_ids) > 1:
                    self._progress.advance(self._progress.task_ids[1])
                # Early-abort guard: if the first 3 windows all failed, the
                # remaining 200+ windows will fail the same way (solver
                # incompatibility, missing data, malformed config). Continuing
                # leaves the user with "Simulation completed" + obj=$0 — a
                # silent-success UX that hides the real solver/model error.
                if num_solved == 0 and window >= 2:
                    raise RuntimeError(
                        f"Year {year}: the first {window + 1} operational "
                        f"windows all failed to solve. Check the log for the "
                        f"underlying error (typically 'not supported by the "
                        f"solver' for ACOPF formulations with non-conic "
                        f"solvers, or 'infeasible' for a malformed model)."
                    )
                continue

            num_solved += 1
            if self._progress and len(self._progress.task_ids) > 1:
                self._progress.advance(self._progress.task_ids[1])

            if window == 0:
                first_soc = window_result.get("bat_soc")
                if first_soc is not None and getattr(first_soc, "ndim", 0) == 3:
                    year_start_soc = np.asarray(first_soc)[..., 0]

            # Update boundary conditions for next window
            if window_result.get("boundary_conditions"):
                boundary_conditions = window_result["boundary_conditions"]

            def _trim(arr, keep_h):
                """Trim overlap hours from the time axis (last axis)."""
                if arr is None:
                    return None
                if arr.ndim == 1:
                    return arr[:keep_h]
                else:
                    return arr[..., :keep_h]

            # Accumulate scalar results. The window's objective, totals and
            # cost breakdown already cover only its committed steps
            # (_solve_window extracts them with keep_hours).
            total_solve_time += window_result.get("solve_time", 0.0)
            total_re_penetration += window_result.get("re_penetration", 0.0)
            window_hours_total = end_hour - start_hour
            kept_fraction = keep_hours / window_hours_total if window_hours_total > 0 else 1.0
            total_objective += window_result.get("objective", 0.0)
            total_load_shed += window_result.get("load_shed_total", 0.0)
            total_curtailment += window_result.get("total_curtailment", 0.0)
            total_emissions += window_result.get("total_co2", 0.0)
            total_generation += window_result.get("total_generation", 0.0)
            total_demand_val += window_result.get("total_demand", 0.0)

            wbd = window_result.get("cost_breakdown")
            if wbd:
                for k, v in wbd.items():
                    accumulated_cost_breakdown[k] = accumulated_cost_breakdown.get(k, 0.0) + v

            # Collect array results (trimmed to remove overlap hours)
            if window_result.get("gen_output") is not None:
                all_gen_output.append(_trim(window_result["gen_output"], keep_hours))
            if window_result.get("gen_status") is not None:
                all_gen_status.append(_trim(window_result["gen_status"], keep_hours))
            if window_result.get("gen_startup") is not None:
                all_gen_startup.append(_trim(window_result["gen_startup"], keep_hours))
            if window_result.get("curtailment") is not None:
                all_curtailment.append(_trim(window_result["curtailment"], keep_hours))
            if window_result.get("bat_charge") is not None:
                all_bat_charge.append(_trim(window_result["bat_charge"], keep_hours))
            if window_result.get("bat_discharge") is not None:
                all_bat_discharge.append(_trim(window_result["bat_discharge"], keep_hours))
            if window_result.get("bat_soc") is not None:
                all_bat_soc.append(_trim(window_result["bat_soc"], keep_hours))
            if window_result.get("reserve_static") is not None:
                all_reserve_static.append(_trim(window_result["reserve_static"], keep_hours))
            if window_result.get("reserve_dynamic") is not None:
                all_reserve_dynamic.append(_trim(window_result["reserve_dynamic"], keep_hours))
            if window_result.get("loss_of_reserve_static") is not None:
                all_loss_reserve_static.append(_trim(window_result["loss_of_reserve_static"], keep_hours))
            if window_result.get("loss_of_reserve_dynamic") is not None:
                all_loss_reserve_dynamic.append(_trim(window_result["loss_of_reserve_dynamic"], keep_hours))
            if window_result.get("load_shed") is not None:
                all_load_shed.append(_trim(window_result["load_shed"], keep_hours))
            if window_result.get("co2_emissions") is not None:
                all_co2_emissions.append(_trim(window_result["co2_emissions"], keep_hours))
            if window_result.get("energy_prices") is not None:
                all_prices.append(_trim(window_result["energy_prices"], keep_hours))
            if window_result.get("voltage_angle") is not None:
                all_voltage_angle.append(_trim(window_result["voltage_angle"], keep_hours))
            if window_result.get("voltage_magnitude") is not None:
                all_voltage_magnitude.append(
                    _trim(window_result["voltage_magnitude"], keep_hours))
            if window_result.get("reactive_generation") is not None:
                all_reactive_generation.append(
                    _trim(window_result["reactive_generation"], keep_hours))
            # Keyed series (power flow, transfer margin, N-1 duals) keep
            # only the committed steps too, with keys aligned across windows.
            all_power_flow.append((window_result.get("power_flow") or {}, keep_hours))
            if window_result.get("bat_spillage") is not None:
                all_bat_spillage.append(_trim(window_result["bat_spillage"], keep_hours))
            if window_result.get("ev_charging") is not None:
                all_ev_charging.append(_trim(window_result["ev_charging"], keep_hours))
            if window_result.get("ev_v2g") is not None:
                all_ev_v2g.append(_trim(window_result["ev_v2g"], keep_hours))
            if window_result.get("ev_soc") is not None:
                all_ev_soc.append(_trim(window_result["ev_soc"], keep_hours))
            if window_result.get("ev_loss") is not None:
                all_ev_loss.append(_trim(window_result["ev_loss"], keep_hours))
            if window_result.get("loss_of_inertia") is not None:
                all_loss_of_inertia.append(_trim(window_result["loss_of_inertia"], keep_hours))
            all_transfer_margin.append((window_result.get("transfer_margin") or {}, keep_hours))
            if window_result.get("reservoir_level") is not None:
                all_reservoir_level.append(_trim(window_result["reservoir_level"], keep_hours))
            if window_result.get("reservoir_spillage") is not None:
                all_reservoir_spillage.append(_trim(window_result["reservoir_spillage"], keep_hours))
            if window_result.get("reservoir_pump") is not None:
                all_reservoir_pump.append(_trim(window_result["reservoir_pump"], keep_hours))
            if window_result.get("electrolyzer_power") is not None:
                all_electrolyzer_power.append(_trim(window_result["electrolyzer_power"], keep_hours))
                all_electrolyzer_h2.append(_trim(window_result["electrolyzer_h2"], keep_hours))
            if window_result.get("reservoir_invest_capacity") is not None:
                last_reservoir_invest = window_result["reservoir_invest_capacity"]
            # Investment arrays: keep from last window (not time-varying)
            if window_result.get("gen_investment") is not None:
                last_gen_investment = window_result["gen_investment"]
            if window_result.get("bat_investment_power") is not None:
                last_bat_inv_power = window_result["bat_investment_power"]
            if window_result.get("bat_investment_capacity") is not None:
                last_bat_inv_capacity = window_result["bat_investment_capacity"]
            if window_result.get("transfer_investment") is not None:
                last_transfer_investment = window_result["transfer_investment"]
            if window_result.get("primary_energy") is not None:
                all_pe_results.append(
                    (window_result["primary_energy"], end_hour - start_hour, keep_hours))
            # N-1 security results
            # A window without binding N-1 duals returns None: zero-fill it.
            all_n1_gen_duals.append((
                {"d": window_result["n1_gen_reserve_duals"]}
                if window_result.get("n1_gen_reserve_duals") is not None else {},
                keep_hours,
            ))
            if window_result.get("n1_binding_contingencies") is not None:
                for bc in window_result["n1_binding_contingencies"]:
                    if bc not in all_n1_binding:
                        all_n1_binding.append(bc)
            all_n1_trans_duals.append((window_result.get("n1_trans_reserve_duals") or {}, keep_hours))
            total_n1_security_cost += (window_result.get("n1_security_cost") or 0.0) * kept_fraction

        # No window solved: the whole year produced no dispatch. Surface the
        # underlying solver/model error instead of exporting an empty result
        # and reporting "Simulation completed" with objective=$0. This covers
        # the single-/few-window case the in-loop early-abort guard (which only
        # fires at window >= 2) cannot reach — e.g. UC mode with one 24h window
        # whose solve failed because the requested solver is unavailable.
        if num_solved == 0:
            raise RuntimeError(
                f"Year {year}: all {num_windows} operational window(s) failed "
                f"to solve — no dispatch was produced. See the 'Window N failed' "
                f"error above for the root cause (commonly: the requested solver "
                f"is not installed/licensed, or the model is infeasible)."
            )

        # Update state boundary conditions for next year
        self.state.boundary_conditions = boundary_conditions

        # Concatenate arrays along time axis (last axis = hours)
        gen_output = np.concatenate(all_gen_output, axis=-1) if all_gen_output else None
        gen_status = np.concatenate(all_gen_status, axis=-1) if all_gen_status else None
        gen_startup = np.concatenate(all_gen_startup, axis=-1) if all_gen_startup else None
        curtailment_arr = np.concatenate(all_curtailment, axis=-1) if all_curtailment else None
        bat_charge = np.concatenate(all_bat_charge, axis=-1) if all_bat_charge else None
        bat_discharge = np.concatenate(all_bat_discharge, axis=-1) if all_bat_discharge else None
        bat_soc = np.concatenate(all_bat_soc, axis=-1) if all_bat_soc else None
        reserve_static = np.concatenate(all_reserve_static, axis=-1) if all_reserve_static else None
        reserve_dynamic = np.concatenate(all_reserve_dynamic, axis=-1) if all_reserve_dynamic else None
        loss_reserve_static = np.concatenate(all_loss_reserve_static, axis=-1) if all_loss_reserve_static else None
        loss_reserve_dynamic = np.concatenate(all_loss_reserve_dynamic, axis=-1) if all_loss_reserve_dynamic else None
        load_shed_arr = np.concatenate(all_load_shed, axis=-1) if all_load_shed else None
        co2_emissions_arr = np.concatenate(all_co2_emissions, axis=-1) if all_co2_emissions else None
        prices = np.concatenate(all_prices, axis=-1) if all_prices else None
        voltage_angle = np.concatenate(all_voltage_angle, axis=-1) if all_voltage_angle else None
        voltage_magnitude = (np.concatenate(all_voltage_magnitude, axis=-1)
                             if all_voltage_magnitude else None)
        reactive_generation = (np.concatenate(all_reactive_generation, axis=-1)
                               if all_reactive_generation else None)

        bat_spillage = np.concatenate(all_bat_spillage, axis=-1) if all_bat_spillage else None
        ev_charging = np.concatenate(all_ev_charging, axis=-1) if all_ev_charging else None
        ev_v2g = np.concatenate(all_ev_v2g, axis=-1) if all_ev_v2g else None
        ev_soc_arr = np.concatenate(all_ev_soc, axis=-1) if all_ev_soc else None
        ev_loss = np.concatenate(all_ev_loss, axis=-1) if all_ev_loss else None
        loss_of_inertia_arr = np.concatenate(all_loss_of_inertia) if all_loss_of_inertia else None
        reservoir_level = np.concatenate(all_reservoir_level, axis=-1) if all_reservoir_level else None
        reservoir_spillage = np.concatenate(all_reservoir_spillage, axis=-1) if all_reservoir_spillage else None
        reservoir_pump = np.concatenate(all_reservoir_pump, axis=-1) if all_reservoir_pump else None
        electrolyzer_power = (np.concatenate(all_electrolyzer_power, axis=-1)
                              if all_electrolyzer_power else None)
        electrolyzer_h2 = (np.concatenate(all_electrolyzer_h2, axis=-1)
                           if all_electrolyzer_h2 else None)

        # Combine keyed series across windows (committed steps only)
        combined_power_flow = _concat_window_series(all_power_flow)
        combined_transfer_margin = _concat_window_series(all_transfer_margin)
        combined_n1_trans_duals = _concat_window_series(all_n1_trans_duals)
        n1_gen_duals_arr = _concat_window_series(all_n1_gen_duals).get("d")

        # Merge primary energy results across windows (committed steps only)
        pe_merged = (_merge_pe_windows(all_pe_results, self._pe_period_steps())
                     if all_pe_results else None)

        # Average RE penetration
        avg_re = total_re_penetration / num_solved if num_solved > 0 else 0.0

        # Aggregate bus-level results back to node-level whenever the bus
        # network is not the identity mapping over nodes (see
        # node_bus_mapping_required) — otherwise bus-indexed arrays are
        # exported as if node-indexed and every viewer misreads them.
        if self._bus_to_node is not None:
            # Quantity variables: SUM across buses in same node
            gen_output = self._aggregate_buses_to_nodes(gen_output, "sum")
            gen_startup = self._aggregate_buses_to_nodes(gen_startup, "sum")
            curtailment_arr = self._aggregate_buses_to_nodes(curtailment_arr, "sum")
            bat_charge = self._aggregate_buses_to_nodes(bat_charge, "sum")
            bat_discharge = self._aggregate_buses_to_nodes(bat_discharge, "sum")
            bat_soc = self._aggregate_buses_to_nodes(bat_soc, "sum")
            load_shed_arr = self._aggregate_buses_to_nodes(load_shed_arr, "sum")
            co2_emissions_arr = self._aggregate_buses_to_nodes(co2_emissions_arr, "sum")
            reserve_static = self._aggregate_buses_to_nodes(reserve_static, "sum")
            reserve_dynamic = self._aggregate_buses_to_nodes(reserve_dynamic, "sum")
            loss_reserve_static = self._aggregate_buses_to_nodes(loss_reserve_static, "sum")
            loss_reserve_dynamic = self._aggregate_buses_to_nodes(loss_reserve_dynamic, "sum")
            bat_spillage = self._aggregate_buses_to_nodes(bat_spillage, "sum")
            ev_charging = self._aggregate_buses_to_nodes(ev_charging, "sum")
            ev_v2g = self._aggregate_buses_to_nodes(ev_v2g, "sum")
            ev_soc_arr = self._aggregate_buses_to_nodes(ev_soc_arr, "sum")
            ev_loss = self._aggregate_buses_to_nodes(ev_loss, "sum")
            loss_of_inertia_arr = self._aggregate_buses_to_nodes(loss_of_inertia_arr, "sum")
            reservoir_level = self._aggregate_buses_to_nodes(reservoir_level, "sum")
            reservoir_spillage = self._aggregate_buses_to_nodes(reservoir_spillage, "sum")
            reservoir_pump = self._aggregate_buses_to_nodes(reservoir_pump, "sum")
            electrolyzer_power = self._aggregate_buses_to_nodes(electrolyzer_power, "sum")
            electrolyzer_h2 = self._aggregate_buses_to_nodes(electrolyzer_h2, "sum")
            # Investments [unit x bus]: SUM across buses in same node
            last_gen_investment = self._aggregate_unit_buses_to_nodes(last_gen_investment)
            last_bat_inv_power = self._aggregate_unit_buses_to_nodes(last_bat_inv_power)
            last_bat_inv_capacity = self._aggregate_unit_buses_to_nodes(last_bat_inv_capacity)
            last_reservoir_invest = self._aggregate_unit_buses_to_nodes(last_reservoir_invest)
            # Status variables: MAX across buses (1 if active at any bus)
            gen_status = self._aggregate_buses_to_nodes(gen_status, "max")
            # Prices: MAX across buses (highest marginal price)
            prices = self._aggregate_buses_to_nodes(prices, "max")
            voltage_angle = self._aggregate_buses_to_nodes(voltage_angle, "max")

        logger.debug(f"Year {year} dispatch: obj=${total_objective:,.0f}, "
                      f"RE={avg_re:.1%}, CO2={total_emissions:.0f}t, time={total_solve_time:.1f}s")

        year_result = YearResults(
            year=year,
            objective=total_objective,
            solve_time=total_solve_time,
            feasible=feasible,
            gen_output=gen_output,
            gen_status=gen_status,
            gen_startup=gen_startup,
            curtailment=curtailment_arr,
            bat_charge=bat_charge,
            bat_discharge=bat_discharge,
            bat_soc=bat_soc,
            reserve_static=reserve_static,
            reserve_dynamic=reserve_dynamic,
            loss_of_reserve_static=loss_reserve_static,
            loss_of_reserve_dynamic=loss_reserve_dynamic,
            load_shed_array=load_shed_arr,
            co2_emissions=co2_emissions_arr,
            power_flow=combined_power_flow if combined_power_flow else None,
            voltage_angle=voltage_angle,
            voltage_magnitude=voltage_magnitude,
            reactive_generation=reactive_generation,
            transfer_investment=last_transfer_investment,
            prices=prices,
            demand=demand,
            gen_investment_array=last_gen_investment,
            bat_investment_power=last_bat_inv_power,
            bat_investment_capacity=last_bat_inv_capacity,
            bat_spillage=bat_spillage,
            ev_charging=ev_charging,
            ev_v2g=ev_v2g,
            ev_soc=ev_soc_arr,
            ev_loss=ev_loss,
            rooftop_generation=(
                self._rooftop_generation[year_idx * HOURS_STD_YEAR:
                                        (year_idx + 1) * HOURS_STD_YEAR]
                if self._rooftop_generation is not None else None
            ),
            loss_of_inertia=loss_of_inertia_arr,
            transfer_margin=combined_transfer_margin if combined_transfer_margin else None,
            reservoir_level=reservoir_level,
            reservoir_spillage=reservoir_spillage,
            reservoir_pump=reservoir_pump,
            reservoir_invest_capacity=last_reservoir_invest,
            electrolyzer_power=electrolyzer_power,
            electrolyzer_h2=electrolyzer_h2,
            primary_energy=pe_merged,
            fuel_for_power=(pe_merged or {}).get("fuel_for_power") or None,
            load_shed=total_load_shed,
            re_penetration=avg_re,
            emissions=total_emissions,
            total_generation=total_generation,
            total_demand=total_demand_val,
            n1_gen_reserve_duals=n1_gen_duals_arr,
            n1_trans_reserve_duals=combined_n1_trans_duals if combined_n1_trans_duals else None,
            n1_binding_contingencies=all_n1_binding if all_n1_binding else None,
            n1_security_cost=total_n1_security_cost,
            cost_breakdown=accumulated_cost_breakdown if accumulated_cost_breakdown else None,
        )

        # Compute derived metrics (LCOE, VALLCOE, capacity factors, etc.)
        self._compute_derived_metrics(year_result)

        return year_result

    def _compute_derived_metrics(self, result: YearResults):
        """Compute derived metrics from raw optimization results (in-place).

        Computes: capacity_factor, LCOE, VALLCOE, battery metrics,
        technology selling prices, and price decomposition.
        """
        sys = self.primary_system
        num_nodes = self._num_nodes

        # Build full generator/battery lists including virtual units from
        # investment technologies (matching Julia solver ordering).
        # Uses _current_units_config which is set per-year before dispatch.
        units_cfg = getattr(self, '_current_units_config', {})
        generators = list(sys.generators.values())
        batteries = list(sys.batteries.values())

        # Append virtual generators (same filter as adapter and _rebuild_unit_names)
        for key, vdata in units_cfg.items():
            if key in sys.generators:
                continue
            if vdata.get("_type") == "battery" or vdata.get("type") == "Storage":
                continue
            if "rated_power" not in vdata:
                continue
            rp = vdata["rated_power"]
            if not rp or max(rp) < 0.01:
                continue
            # Create a lightweight config-like object with needed fields
            generators.append(SimpleNamespace(
                name=vdata.get("name", key),
                type=vdata.get("type", "Renewable"),
                rated_power=rp,
                fuel_cost=vdata.get("fuel_cost", [0.0] * len(rp)),
                fixed_cost=vdata.get("fixed_cost", [0.0] * len(rp)),
                maintenance_cost=vdata.get("maintenance_cost", [0.0] * len(rp)),
                start_up_cost=vdata.get("start_up_cost", [0.0] * len(rp)),
                ramp_up=vdata.get("ramp_up", [1.0] * len(rp)),
            ))

        # Append virtual batteries (same filter as adapter and _rebuild_unit_names)
        for key, vdata in units_cfg.items():
            if key in sys.batteries:
                continue
            if vdata.get("_type") != "battery" and vdata.get("type") != "Storage":
                continue
            cap = vdata.get("capacity", [])
            charge_pow = vdata.get("MaxChargePower", [])
            if not cap or (max(cap) < 0.01 and max(charge_pow) < 0.01):
                continue
            batteries.append(SimpleNamespace(
                name=vdata.get("name", key),
                MaxDischargePower=vdata.get("MaxDischargePower", charge_pow),
                efficiency_charge=vdata.get("efficiency_charge", [0.95] * len(cap)),
                efficiency_discharge=vdata.get("efficiency_discharge", [0.95] * len(cap)),
                maintenance_cost=vdata.get("maintenance_cost", [0.0] * len(cap)),
            ))

        # --- Capacity Factor (generators) ---
        if result.gen_output is not None:
            n_gen, n_nodes, hours = result.gen_output.shape
            cf = np.zeros_like(result.gen_output)
            for g, gen in enumerate(generators):
                if g >= n_gen:
                    break
                for n in range(min(n_nodes, len(gen.rated_power))):
                    rated = gen.rated_power[n]
                    if rated > 0:
                        cf[g, n, :] = result.gen_output[g, n, :] / rated
            result.capacity_factor = cf

        # --- LCOE (generators) ---
        # Fallback fuel cost: when gen.fuel_cost[n] is 0 (Grid Builder produced
        # configs where per-generator cost arrays are zero and operational cost
        # flows via `fuels:` + technology efficiency instead), compute the
        # per-MWh-electricity cost as price/energy_content/efficiency. Without
        # this fallback LCOE/VALLCOE come out 0 for every generator that uses
        # the fuel-registry cost model, even when the generator dispatches.
        gui_techs = getattr(sys, 'gui_technologies', None) or {}
        sys_fuels = getattr(sys, 'fuels', None) or {}

        def _fuel_based_cost(gen, n) -> float:
            fuel_name = getattr(gen, 'fuel', None)
            if not isinstance(fuel_name, str) or not fuel_name:
                return 0.0
            fuel = sys_fuels.get(fuel_name) if hasattr(sys_fuels, 'get') else None
            if fuel is None:
                return 0.0
            price = getattr(fuel, 'price_base', None)
            if price is None or price <= 0:
                price = getattr(fuel, 'price', 0.0) or 0.0
            energy = getattr(fuel, 'energy_content', None) or 1.0
            if price <= 0 or energy <= 0:
                return 0.0
            eff = 0.0
            tech_id = getattr(gen, 'technology', None)
            if tech_id and isinstance(gui_techs, dict):
                tech = gui_techs.get(tech_id)
                if isinstance(tech, dict):
                    eff = float(tech.get('eff_at_rated', 0.0) or 0.0)
            if eff <= 0:
                eff_list = getattr(gen, 'eff_at_rated', None)
                if eff_list and n < len(eff_list) and eff_list[n] > 0:
                    eff = float(eff_list[n])
            if eff <= 0:
                return 0.0
            return float(price) / float(energy) / eff

        if result.gen_output is not None:
            n_gen, n_nodes, hours = result.gen_output.shape
            lcoe = np.zeros_like(result.gen_output)
            for g, gen in enumerate(generators):
                if g >= n_gen:
                    break
                for n in range(min(n_nodes, len(gen.fuel_cost))):
                    output = result.gen_output[g, n, :]
                    mask = output > 1e-6
                    if not np.any(mask):
                        continue
                    fc = gen.fuel_cost[n]
                    if fc <= 0:
                        fc = _fuel_based_cost(gen, n)
                    fxc = gen.fixed_cost[n]
                    mc = gen.maintenance_cost[n]
                    total_cost = (fc + fxc + mc) * output
                    # Add startup cost if available
                    if result.gen_startup is not None and g < result.gen_startup.shape[0]:
                        su = gen.start_up_cost[n] if n < len(gen.start_up_cost) else 0.0
                        total_cost += su * result.gen_startup[g, n, :]
                    lcoe[g, n, mask] = total_cost[mask] / output[mask]
            result.lcoe = lcoe

        # --- Price decomposition (energy lambda + congestion mu) ---
        if result.prices is not None and result.prices.ndim == 2:
            # Energy component = system average price per timestep
            energy_lambda = result.prices.mean(axis=0)  # [hour]
            # Congestion component = nodal deviation from system average
            congestion_mu = result.prices - energy_lambda[np.newaxis, :]  # [node x hour]
            result.price_energy_component = energy_lambda
            result.price_congestion_component = congestion_mu

        # --- Common VALLCOE inputs (shared between gen and bat) ---
        # NOTE: result arrays may have more hours than original demand due to
        # rolling horizon overlap concatenation. Use result array hours.
        sys_avg_price = None
        overall_sys_price = 1.0
        peak_mask = None
        if result.prices is not None:
            hours_p = result.prices.shape[1] if result.prices.ndim == 2 else result.prices.shape[0]
            if result.demand is not None:
                demand_arr = np.array(result.demand)
                if demand_arr.ndim == 2 and demand_arr.shape[0] > demand_arr.shape[1]:
                    demand_arr = demand_arr.T  # -> [node x hour]
                # Pad or truncate demand to match result hours
                d_hours = demand_arr.shape[1] if demand_arr.ndim == 2 else demand_arr.shape[0]
                if d_hours < hours_p:
                    # Repeat last values to fill overlap hours
                    pad_width = hours_p - d_hours
                    demand_arr = np.pad(demand_arr, ((0, 0), (0, pad_width)), mode="edge")
                elif d_hours > hours_p:
                    demand_arr = demand_arr[:, :hours_p]
                # Align demand to the price array's spatial dimension. result.prices
                # is per-node; with development zones (or any config where buses >
                # nodes) result.demand can be per-bus, so aggregate it to per-node
                # before the demand-weighted average (otherwise the elementwise
                # product broadcasts node-rows against bus-rows and crashes).
                n_price = result.prices.shape[0]
                if demand_arr.shape[0] != n_price:
                    b2n = getattr(self, '_bus_to_node', None)
                    if b2n is not None and demand_arr.shape[0] == len(b2n):
                        agg = np.zeros((n_price, demand_arr.shape[1]))
                        for _b in range(demand_arr.shape[0]):
                            _ni = b2n[_b]
                            if 0 <= _ni < n_price:
                                agg[_ni] += demand_arr[_b]
                        demand_arr = agg
                    elif demand_arr.shape[0] > n_price:
                        demand_arr = demand_arr[:n_price, :]
                    else:
                        demand_arr = np.pad(
                            demand_arr, ((0, n_price - demand_arr.shape[0]), (0, 0)),
                        )
                total_demand_per_h = demand_arr.sum(axis=0)
                sys_avg_price = np.where(
                    total_demand_per_h > 0,
                    (result.prices * demand_arr).sum(axis=0) / total_demand_per_h,
                    result.prices.mean(axis=0),
                )
            else:
                sys_avg_price = result.prices.mean(axis=0)
                total_demand_per_h = np.ones(hours_p)

            overall_sys_price = np.mean(sys_avg_price[sys_avg_price > 0]) if np.any(sys_avg_price > 0) else 1.0
            peak_threshold = np.percentile(total_demand_per_h, 90)
            peak_mask = total_demand_per_h >= peak_threshold

        # --- VALLCOE (generators) ---
        if result.lcoe is not None and result.prices is not None:
            n_gen, n_nodes, hours = result.lcoe.shape
            vallcoe = np.zeros_like(result.lcoe)

            for g, gen in enumerate(generators):
                if g >= n_gen:
                    break
                for n in range(min(n_nodes, len(gen.rated_power))):
                    output = result.gen_output[g, n, :]
                    unit_lcoe = result.lcoe[g, n, :]
                    total_gen = output.sum()
                    if total_gen < 1e-6:
                        continue

                    # Energy adjustment (capture rate)
                    prices_n = result.prices[n, :] if n < result.prices.shape[0] else sys_avg_price
                    revenue = (output * prices_n).sum()
                    unit_avg_price = revenue / total_gen
                    capture_rate = unit_avg_price / overall_sys_price if overall_sys_price > 0 else 1.0
                    energy_adj = 1.0 - capture_rate

                    # Capacity adjustment
                    rated = gen.rated_power[n]
                    if rated > 0 and np.any(peak_mask):
                        peak_gen = output[peak_mask].sum()
                        peak_potential = rated * peak_mask.sum()
                        capacity_credit = peak_gen / peak_potential if peak_potential > 0 else 0.0
                    else:
                        capacity_credit = 0.0
                    ref_cc = 0.85
                    capacity_adj = (ref_cc - capacity_credit) * 0.15

                    # Flexibility adjustment
                    flex_score = 0.0
                    if gen.type != "Renewable" and rated > 0:
                        ramp = gen.ramp_up[n] if n < len(gen.ramp_up) else 0.0
                        flex_score = ramp / rated if rated > 0 else 0.0
                    flexibility_adj = -flex_score * 0.1

                    total_adj = energy_adj + capacity_adj + flexibility_adj
                    vallcoe[g, n, :] = unit_lcoe * (1.0 + total_adj)

            result.vallcoe = vallcoe

        # --- Battery capacity factor ---
        if result.bat_discharge is not None:
            n_bat, n_nodes_b, hours = result.bat_discharge.shape
            bat_cf = np.zeros_like(result.bat_discharge)
            for b, bat in enumerate(batteries):
                if b >= n_bat:
                    break
                for n in range(min(n_nodes_b, len(bat.MaxDischargePower))):
                    rated = bat.MaxDischargePower[n]
                    if rated > 0:
                        bat_cf[b, n, :] = result.bat_discharge[b, n, :] / rated
            result.bat_capacity_factor = bat_cf

        # --- Battery LCOE ---
        if result.bat_charge is not None and result.bat_discharge is not None:
            n_bat, n_nodes_b, hours = result.bat_discharge.shape
            bat_lcoe = np.zeros_like(result.bat_discharge)
            for b, bat in enumerate(batteries):
                if b >= n_bat:
                    break
                for n in range(min(n_nodes_b, len(bat.efficiency_charge))):
                    charge = result.bat_charge[b, n, :]
                    discharge = result.bat_discharge[b, n, :]
                    eff_c = bat.efficiency_charge[n]
                    eff_d = bat.efficiency_discharge[n]
                    mc = bat.maintenance_cost[n] if n < len(bat.maintenance_cost) else 0.0

                    mask = discharge > 0.01
                    if not np.any(mask):
                        continue

                    # Energy cost = charging cost (buy at market price / round-trip efficiency)
                    if result.prices is not None and n < result.prices.shape[0]:
                        price_t = result.prices[n, :]
                    elif result.price_energy_component is not None:
                        price_t = result.price_energy_component
                    else:
                        price_t = np.zeros(hours)

                    rt_eff = max(eff_c * eff_d, 0.01)
                    energy_cost = charge * price_t / rt_eff
                    cycling_cost = (charge + discharge) * mc
                    total_cost = energy_cost + cycling_cost

                    bat_lcoe[b, n, mask] = np.clip(
                        total_cost[mask] / discharge[mask], 0.0, 10000.0
                    )
            result.bat_lcoe = bat_lcoe

        # --- Battery VALLCOE ---
        if result.bat_lcoe is not None and result.prices is not None:
            n_bat, n_nodes_b, hours = result.bat_lcoe.shape
            bat_vallcoe = np.zeros_like(result.bat_lcoe)

            for b, bat in enumerate(batteries):
                if b >= n_bat:
                    break
                for n in range(min(n_nodes_b, len(bat.MaxDischargePower))):
                    discharge = result.bat_discharge[b, n, :]
                    total_dis = discharge.sum()
                    if total_dis < 1e-6:
                        continue

                    prices_n = result.prices[n, :] if n < result.prices.shape[0] else sys_avg_price
                    revenue = (discharge * prices_n).sum()
                    avg_sell = revenue / total_dis
                    capture_rate = avg_sell / overall_sys_price if overall_sys_price > 0 else 1.0
                    energy_adj = 1.0 - capture_rate

                    rated = bat.MaxDischargePower[n]
                    if rated > 0 and np.any(peak_mask):
                        peak_dis = discharge[peak_mask].sum()
                        peak_pot = rated * peak_mask.sum()
                        cc = peak_dis / peak_pot if peak_pot > 0 else 0.0
                    else:
                        cc = 0.0
                    capacity_adj = (0.85 - cc) * 0.15
                    flexibility_adj = -0.05  # Storage is inherently flexible

                    total_adj = energy_adj + capacity_adj + flexibility_adj
                    bat_vallcoe[b, n, :] = result.bat_lcoe[b, n, :] * (1.0 + total_adj)

            result.bat_vallcoe = bat_vallcoe

        # --- Technology selling prices ---
        if result.gen_output is not None and result.prices is not None:
            tech_prices = {}
            n_gen = result.gen_output.shape[0]
            hours = result.gen_output.shape[2]
            # Keyed by the unit's HDF5 dataset name: unique (homonymous
            # generators no longer overwrite each other) and '/'-free.
            known = list(getattr(self, "_gen_names", None) or [])
            names = known if len(known) == len(generators) else [gen.name for gen in generators]
            ds_names = self._h5_unit_names(names)

            for g, gen in enumerate(generators):
                if g >= n_gen:
                    break
                name = ds_names[g]
                rows = []
                total_gen = 0.0
                total_rev = 0.0
                for n in range(min(result.gen_output.shape[1], len(gen.rated_power))):
                    prices_n = result.prices[n, :] if n < result.prices.shape[0] else np.zeros(hours)
                    for t in range(hours):
                        output = result.gen_output[g, n, t]
                        if output > 1e-6:
                            rows.append([prices_n[t], output, t])
                            total_gen += output
                            total_rev += prices_n[t] * output

                if rows:
                    tech_prices[name] = {
                        "prices_weights": np.array(rows),
                        "total_generation": total_gen,
                        "total_revenue": total_rev,
                        "average_selling_price": total_rev / total_gen if total_gen > 0 else 0.0,
                        "technology_type": gen.type,
                    }

            result.technology_selling_prices = tech_prices

        logger.debug("Computed derived metrics (LCOE, VALLCOE, CF, selling prices, price decomposition)")

    def _build_pe_sources_from_entries(
        self, system_config, num_nodes: int,
    ) -> dict:
        """Build PE-adapter ``fuels_config`` from fuel_entry_points + storage.

        When the YAML config defines ``fuel_entry_points`` but not
        ``primary_energy_sources``, we synthesise the per-fuel, per-node
        arrays that the ``PrimaryEnergyAdapter`` expects.
        """
        # Collect all fuels served by entry points
        fuel_names: set[str] = set()
        for fe in system_config.fuel_entry_points:
            fuels = fe.fuels if fe.fuels else ([fe.fuel] if fe.fuel else [])
            fuel_names.update(fuels)
        if not fuel_names:
            return {}

        # Aggregate per-fuel, per-node: max_availability, import_cost
        avail = {f: [0.0] * num_nodes for f in fuel_names}
        cost = {f: [0.0] * num_nodes for f in fuel_names}
        # Per-fuel supply-stress params (transit lead time, disruption window).
        # The solver source is per-fuel (one scalar each), so reconcile across
        # the entry points feeding a fuel: take the longest transit and the
        # most severe disruption (lowest availability) window.
        transit = {f: 0.0 for f in fuel_names}
        disrupt = {f: {"start": 0, "end": 0, "avail": 1.0} for f in fuel_names}

        def _fp_attr(fp, key, default):
            return (fp.get(key, default) if isinstance(fp, dict)
                    else getattr(fp, key, default))

        for fe in system_config.fuel_entry_points:
            node = fe.node if fe.node < num_nodes else 0
            fuels = fe.fuels if fe.fuels else ([fe.fuel] if fe.fuel else [])
            for fuel in fuels:
                if fuel not in fuel_names:
                    continue
                fp = fe.fuel_params.get(fuel, {})
                rate = _fp_attr(fp, 'max_import_rate', fe.max_import_rate)
                ic = _fp_attr(fp, 'import_cost', fe.import_cost)
                # max_import_rate is in units/hour, convert to units/year for max_availability
                avail[fuel][node] += float(rate) * 8760.0
                # Take the cheapest cost when multiple entries feed the same node
                if cost[fuel][node] == 0.0:
                    cost[fuel][node] = float(ic)
                else:
                    cost[fuel][node] = min(cost[fuel][node], float(ic))
                # Supply stress: longest transit, most severe disruption window
                transit[fuel] = max(
                    transit[fuel],
                    float(_fp_attr(fp, 'transport_transit_days_per_100km', 0.0)))
                d_start = int(_fp_attr(fp, 'disruption_start_hour', 0))
                d_end = int(_fp_attr(fp, 'disruption_end_hour', 0))
                d_avail = float(_fp_attr(fp, 'disruption_availability', 1.0))
                if d_end > d_start and d_avail < disrupt[fuel]["avail"]:
                    disrupt[fuel] = {"start": d_start, "end": d_end, "avail": d_avail}

        # Aggregate per-fuel, per-node storage from fuel_infrastructure.storage_facilities
        stor_cap = {f: [0.0] * num_nodes for f in fuel_names}
        stor_init = {f: [0.5] * num_nodes for f in fuel_names}
        min_stor = {f: 0.1 for f in fuel_names}

        infra = getattr(system_config, 'fuel_infrastructure', None)
        if infra:
            facilities = {}
            if hasattr(infra, 'storage_facilities'):
                facilities = infra.storage_facilities if isinstance(infra.storage_facilities, dict) else {}
            elif isinstance(infra, dict):
                facilities = infra.get('storage_facilities', {})

            for _fac_key, fac in facilities.items():
                fac_d = fac if isinstance(fac, dict) else (fac.model_dump() if hasattr(fac, 'model_dump') else fac.__dict__)
                fac_node = int(fac_d.get('node', 0))
                if fac_node >= num_nodes:
                    fac_node = 0
                fac_fuels = fac_d.get('fuels', [])
                if not fac_fuels:
                    f = fac_d.get('fuel', '')
                    if f:
                        fac_fuels = [f]
                fp_dict = fac_d.get('fuel_params', {})
                for fuel in fac_fuels:
                    if fuel not in fuel_names:
                        continue
                    fp = fp_dict.get(fuel, {})
                    if isinstance(fp, dict):
                        cap = fp.get('capacity', fac_d.get('capacity', 0.0))
                        init = fp.get('initial_level', fac_d.get('initial_level', 0.5))
                        ml = fp.get('min_level', fac_d.get('min_level', 0.1))
                    else:
                        cap = getattr(fp, 'capacity', fac_d.get('capacity', 0.0))
                        init = getattr(fp, 'initial_level', fac_d.get('initial_level', 0.5))
                        ml = getattr(fp, 'min_level', fac_d.get('min_level', 0.1))
                    stor_cap[fuel][fac_node] += float(cap)
                    stor_init[fuel][fac_node] = float(init)
                    min_stor[fuel] = min(min_stor[fuel], float(ml))

        pe_sources = {}
        for fuel in sorted(fuel_names):
            pe_sources[fuel] = {
                'name': fuel,
                'unit': 'ton',
                'max_availability': avail[fuel],
                'import_cost': cost[fuel],
                'storage_capacity': stor_cap[fuel],
                'initial_storage_level': stor_init[fuel],
                'min_storage_level': min_stor[fuel],
                'storage_investment_cost': 0.0,
                'transport_cost': 0.0,
                'transport_losses': 0.0,
                'transport_transit_days_per_100km': transit[fuel],
                'disruption_start_hour': disrupt[fuel]["start"],
                'disruption_end_hour': disrupt[fuel]["end"],
                'disruption_availability': disrupt[fuel]["avail"],
                'max_storage_investment_per_node': 0.0,
                'max_transport_investment_per_arc': 0.0,
            }
            # Get unit from fuels definition
            fuel_def = system_config.fuels.get(fuel)
            if fuel_def:
                pe_sources[fuel]['unit'] = getattr(fuel_def, 'unit', 'ton')

        logger.debug(f"Auto-built PE sources from fuel_entry_points: "
                     f"{list(pe_sources.keys())}")
        for fk, fv in pe_sources.items():
            logger.debug(f"  PE source {fk}: max_avail={fv['max_availability']}, "
                        f"import_cost={fv['import_cost']}, "
                        f"storage_cap={fv['storage_capacity']}")
        return pe_sources

    def _pe_period_steps(self) -> int:
        """Primary-energy period length in timesteps (``primary_energy_resolution`` hours)."""
        res_h = getattr(self.config.temporal, 'resolution_hours', 1) or 1
        return max(1, int(self.config.temporal.primary_energy_resolution) // res_h)

    def _solve_window(
        self,
        year: int,
        window: int,
        demand: np.ndarray,
        hours: int,
        num_nodes: int,
        boundary_conditions: dict,
        units_config: Optional[dict] = None,
        sectoral_demand: Optional[Dict[str, np.ndarray]] = None,
        ev_config_data: Optional[Dict] = None,
        start_hour: int = 0,
        rooftop_generation: Optional[np.ndarray] = None,
        re_penetration_target: Optional[float] = None,
        keep_hours: Optional[int] = None,
    ) -> Optional[dict]:
        """
        Solve a single rolling horizon window.

        Args:
            year: Current simulation year
            window: Window index
            demand: Window demand array (hours x nodes)
            hours: Number of hours in this window
            start_hour: Start hour within the year (for availability profile slicing)
            num_nodes: Number of nodes
            boundary_conditions: Battery SOC, generator status from previous window
            keep_hours: Steps of this window that are committed (the next
                window starts after them); the boundary state handed to the
                next window is taken there. Defaults to the whole window.
            units_config: Current units configuration (with investments applied)
            sectoral_demand: Sectoral demand arrays for this window
            ev_config_data: EV configuration data for this window
            rooftop_generation: Rooftop solar generation array (hours x nodes) or None

        Returns:
            Dictionary with window results or None if infeasible
        """
        solve_start = time.time()

        try:
            # Use updated units_config or fall back to primary system
            system_config = self.primary_system

            # In development mode, PowerSystem runs as economic_dispatch subproblem
            # (investments are fixed by MasterProblem, only operations are optimized)
            # Optionally use unit_commitment for more realistic operational costs
            if self.config.simulation_mode == "development":
                master_cfg = getattr(self.config, 'master_problem', None)
                if master_cfg and getattr(master_cfg, 'use_uc_in_dispatch', False):
                    ps_mode = "unit_commitment"
                else:
                    ps_mode = "economic_dispatch"
            else:
                ps_mode = self.config.simulation_mode

            # Create PowerSystem adapter with cached availability and inflow
            # Pass ESFEXConfig (not just SystemConfig) so the adapter can
            # access temporal.resolution_hours for correct availability slicing
            ps = PowerSystemAdapter(
                config=self.config,
                demand=demand,
                hours=hours,
                num_nodes=num_nodes,
                year=year,
                base_year=self.state.base_year,
                mode=ps_mode,
                availability_cache=self._availability_cache,
                inflow_cache=getattr(self, "_inflow_cache", None),
                start_hour=start_hour,
                boundary_conditions=boundary_conditions,
                units_config=units_config,
                sectoral_demand=sectoral_demand or {},
                ev_config_data=ev_config_data,
                rooftop_generation=rooftop_generation,
                re_penetration_target=re_penetration_target,
                # With the electrolyzers the master built up to this year
                system_config=self._dispatch_system(year),
                config_path=self.config_path,
            )

            # Build power system model
            ps.build_model()

            # Integrate primary energy (fuel supply chain) if enabled and configured
            pe = None
            if self._pe_enabled(system_config):
                try:
                    # Build generator list as dicts for PE adapter.
                    # MUST match the PowerSystemAdapter ordering: original gens
                    # first, then virtual investment gens from units_config.
                    gen_list = []
                    for gk, gc in system_config.generators.items():
                        gen_dict = gc.model_dump() if hasattr(gc, 'model_dump') else gc.__dict__.copy()
                        gen_dict['name'] = gk
                        gen_list.append(gen_dict)
                    # Append virtual generators (investments) so indices match
                    if units_config:
                        for vk, vdata in units_config.items():
                            if vk in system_config.generators:
                                continue  # already included above
                            if vdata.get("_type") == "battery" or vdata.get("type") == "Storage":
                                continue
                            if "rated_power" not in vdata:
                                continue
                            rp = vdata["rated_power"]
                            if not rp or max(rp) < 0.01:
                                continue
                            gen_dict = dict(vdata)
                            gen_dict['name'] = vdata.get('name', vk)
                            gen_list.append(gen_dict)

                    res_h = getattr(self.config.temporal, 'resolution_hours', 1)
                    pe_init_storage = boundary_conditions.get('pe_storage_levels')
                    if pe_init_storage and window <= 3:
                        logger.debug(f"Window {window}: PE initial_storage_levels = {pe_init_storage}")

                    pe = PrimaryEnergyAdapter(
                        year=year,
                        base_year=self.state.base_year,
                        hours=hours,
                        num_nodes=num_nodes,
                        generators=gen_list,
                        primary_energy_resolution=self._pe_period_steps(),
                        mode=ps_mode,
                        # Fuel storage and routes the master built up to this
                        # year (the dispatch builds none)
                        cumulative_capacities=self._dispatch_pe_capacities(year),
                        initial_storage_levels=pe_init_storage,
                        step_hours=res_h,
                        year_hour_offset=start_hour,
                        # Fuel the previous window shipped that is still in transit
                        scheduled_arrivals=boundary_conditions.get('pe_in_transit'),
                        **self._pe_inputs(system_config, num_nodes),
                    )

                    # Create PE variables in the same JuMP model
                    pe.create_variables(ps._jl_model)

                    # Couple PE to PowerSystem (adds coupling constraints + PE objective)
                    pe.integrate_with_power_system(ps)

                    logger.debug(f"Window {window}: Primary energy integrated")
                except Exception as e:
                    logger.warning(f"Window {window}: Primary energy integration failed: {e}")
                    pe = None

            # Solve the (possibly coupled) model
            status = ps.solve()

            if status != 1:  # Not optimal
                status_desc = {-1: "INFEASIBLE", -2: "UNBOUNDED", 0: "NOT_SOLVED"}.get(status, f"status={status}")
                logger.warning(f"Window {window} not optimal ({status_desc})")
                # Export LP file for debugging
                lp_dir = self.output_dir / "logs"
                lp_dir.mkdir(parents=True, exist_ok=True)
                lp_file = lp_dir / f"window_infeasible_y{year}_w{window}.lp"
                try:
                    ps.write_lp(str(lp_file))
                    logger.debug(f"Non-optimal window LP exported to: {lp_file}")
                except Exception as e:
                    logger.warning(f"Could not export LP: {e}")
                return None

            # Steps committed from this window (the next window solves the
            # overlap tail again): the scalar totals and costs cover only
            # these, and the boundary state is taken where they end.
            k = min(keep_hours, hours) if keep_hours else hours

            # Extract results
            solution = ps.get_solution_values(keep_steps=k, primary_energy=pe)

            # Extract primary energy results and storage carry-over
            pe_results = None
            if pe is not None:
                try:
                    pe_results = pe.get_results(keep_steps=k)
                except Exception as e:
                    logger.warning(f"Window {window}: PE result extraction failed: {e}")
                if pe_results:
                    # Fuel burnt per coupled unit, keyed by the unit's name
                    unit_names = unique_unit_names([g["name"] for g in gen_list])
                    pe_results["fuel_for_power"] = {
                        unit_names[g]: arr
                        for g, arr in (pe_results.get("fuel_for_power") or {}).items()
                        if 0 <= g < len(unit_names)
                    }

            # Extract boundary conditions for next window, at the end of the
            # committed part: the next window starts there, not at the end of
            # this window's look-ahead (overlap) tail.
            new_boundary = {}

            def _state_at(arr):
                # Level arrays have hours+1 columns (column t = level at the
                # start of step t); flow arrays have one column per step.
                if arr is None or not hasattr(arr, 'ndim') or arr.ndim < 2:
                    return None
                return arr[..., k] if arr.shape[-1] > hours else arr[..., k - 1]

            bat_soc = _state_at(solution.get('bat_soc'))
            if bat_soc is not None:
                new_boundary['battery_soc'] = bat_soc

            # UC commitment carry-over (gen_status per gen × bus). Adapter
            # populates Julia generator_initial_status used by min_up /
            # min_down constraints at t=1 of the next window.
            gen_status = solution.get('gen_status')
            if gen_status is not None and hasattr(gen_status, 'ndim') and gen_status.ndim == 3:
                new_boundary['gen_status_init'] = _state_at(gen_status)

            # Ramp continuity: gen_output of the last committed step.
            gen_output = solution.get('gen_output')
            if gen_output is not None and hasattr(gen_output, 'ndim') and gen_output.ndim == 3:
                new_boundary['gen_output_prev'] = _state_at(gen_output)

            # Reservoir continuity: water level (overrides the configured
            # initial fraction on the next window).
            reservoir_level = solution.get('reservoir_level')
            if reservoir_level is not None and hasattr(reservoir_level, 'ndim') and reservoir_level.ndim == 3:
                new_boundary['reservoir_level'] = _state_at(reservoir_level)

            # Carry PE tank levels and fuel still in transit to the next window
            if pe is not None:
                try:
                    new_boundary['pe_storage_levels'] = pe.get_final_storage_levels(step=k)
                    new_boundary['pe_in_transit'] = pe.get_in_transit_arrivals(step=k)
                except Exception as e:
                    logger.warning(f"Window {window}: Could not extract PE storage levels: {e}")
                    import traceback
                    logger.warning(traceback.format_exc())

            result = {
                "window": window,
                "objective": solution.get('objective', 0.0),
                "solve_time": time.time() - solve_start,
                "status": solution.get('status', 'Unknown'),
                "boundary_conditions": new_boundary,
                # Generation arrays [gen x node x hour]
                "gen_output": solution.get('gen_output'),
                "gen_status": solution.get('gen_status'),
                "gen_startup": solution.get('gen_startup'),
                "curtailment": solution.get('curtailment'),
                # Storage arrays [bat x node x hour]
                "bat_charge": solution.get('bat_charge'),
                "bat_discharge": solution.get('bat_discharge'),
                "bat_soc": solution.get('bat_soc'),
                # Reserve arrays [node x hour]
                "reserve_static": solution.get('reserve_static'),
                "reserve_dynamic": solution.get('reserve_dynamic'),
                "loss_of_reserve_static": solution.get('loss_of_reserve_static'),
                "loss_of_reserve_dynamic": solution.get('loss_of_reserve_dynamic'),
                # Load shedding [node x hour] and CO2 [node x hour]
                "load_shed": solution.get('load_shed'),
                "co2_emissions": solution.get('co2_emissions'),
                # Network
                "energy_prices": solution.get('energy_prices'),
                "power_flow": solution.get('power_flow'),
                "power_flow_by_line": solution.get('power_flow_by_line'),
                "voltage_angle": solution.get('voltage_angle'),
                "voltage_magnitude": solution.get('voltage_magnitude'),
                "reactive_generation": solution.get('reactive_generation'),
                "transfer_investment": solution.get('transfer_investment'),
                # EV / V2G dispatch [node x hour] — produced by Julia when
                # ev_config is set; must be propagated here or the year-level
                # aggregation drops them and they never reach the H5.
                "ev_charging": solution.get('ev_charging'),
                "ev_v2g": solution.get('ev_v2g'),
                "ev_soc": solution.get('ev_soc'),
                "ev_loss": solution.get('ev_loss'),
                # Electrolyzers [electrolyzer x bus x hour]: MW drawn, kg/h H2
                "electrolyzer_power": solution.get('electrolyzer_power'),
                "electrolyzer_h2": solution.get('electrolyzer_h2'),
                # Battery spillage [bat x bus x hour], inertia deficit
                # [hour], transfer margin {(from_bus, to_bus): [hour]}
                "bat_spillage": solution.get('bat_spillage'),
                "loss_of_inertia": solution.get('loss_of_inertia'),
                "transfer_margin": solution.get('transfer_margin'),
                # Reservoir hydro [gen x bus x hour] (level: hour+1 columns)
                "reservoir_level": solution.get('reservoir_level'),
                "reservoir_spillage": solution.get('reservoir_spillage'),
                "reservoir_pump": solution.get('reservoir_pump'),
                "reservoir_invest_capacity": solution.get('reservoir_invest_capacity'),
                # N-1 security: duals, binding contingencies, security cost
                "n1_gen_reserve_duals": solution.get('n1_gen_reserve_duals'),
                "n1_trans_reserve_duals": solution.get('n1_trans_reserve_duals'),
                "n1_binding_contingencies": solution.get('n1_binding_contingencies'),
                "n1_security_cost": solution.get('n1_security_cost', 0.0),
                # Investment arrays
                "gen_investment": solution.get('gen_investment'),
                "bat_investment_power": solution.get('bat_investment_power'),
                "bat_investment_capacity": solution.get('bat_investment_capacity'),
                # Scalars
                "load_shed_total": solution.get('load_shed_total', 0.0),
                "total_curtailment": solution.get('total_curtailment', 0.0),
                "re_penetration": solution.get('re_penetration', 0.0),
                "total_co2": solution.get('total_co2', 0.0),
                "total_generation": solution.get('total_generation', 0.0),
                "total_demand": solution.get('total_demand', 0.0),
                # Granular cost decomposition (capex/opex/fuel/O&M/penalties).
                # Built by Julia extract_solution → CostBreakdown and mapped in
                # converters.py; accumulated across windows and written to the
                # H5 cost_breakdown / global/cost_breakdown groups.
                "cost_breakdown": solution.get('cost_breakdown'),
                # Primary energy results (if enabled)
                "primary_energy": pe_results,
            }

            # ── AC Power Flow Verification (UC mode only) ──
            # Runs when power_flow_mode is "dcopf_ac_verify" (new) or
            # ac_power_flow.enabled is True (legacy)
            pf_mode = getattr(system_config, 'power_flow_mode', 'dcopf')
            ac_cfg = getattr(system_config, 'ac_power_flow', None)
            run_ac_verify = (
                (pf_mode == "dcopf_ac_verify") or
                (ac_cfg and ac_cfg.enabled)
            )
            if (run_ac_verify
                    and ps_mode == "unit_commitment"
                    and system_config.transmission_lines_geo):
                ac_results = self._run_ac_verification(
                    ps, system_config, hours, window, year
                )
                result["ac_power_flow"] = ac_results

            # Compute balance from solution arrays (matches KCL terms)
            go = solution.get('gen_output')
            ls = solution.get('load_shed')
            bd = solution.get('bat_discharge')
            bc = solution.get('bat_charge')
            ev_ch = solution.get('ev_charging')
            ev_v2g = solution.get('ev_v2g')
            rs = solution.get('reserve_static')
            rd = solution.get('reserve_dynamic')

            gen_sum = float(np.sum(go)) if go is not None else 0
            ls_sum = float(np.sum(ls)) if ls is not None else 0
            bd_sum = float(np.sum(bd)) if bd is not None else 0
            bc_sum = float(np.sum(bc)) if bc is not None else 0
            ev_ch_sum = float(np.sum(ev_ch)) if ev_ch is not None else 0
            ev_v2g_sum = float(np.sum(ev_v2g)) if ev_v2g is not None else 0
            rs_sum = float(np.sum(rs)) if rs is not None else 0
            rd_sum = float(np.sum(rd)) if rd is not None else 0
            dem_sum = float(np.sum(demand)) if demand is not None else 0
            rooftop_sum = float(np.sum(rooftop_generation)) if rooftop_generation is not None else 0

            supply = gen_sum + bd_sum + ev_v2g_sum + ls_sum + rooftop_sum
            demand_side = dem_sum + bc_sum + rs_sum + rd_sum + ev_ch_sum

            gap = supply - demand_side
            gap_pct = abs(gap) / demand_side * 100 if demand_side > 0 else 0

            # Concise balance line for every window (matches Julia @info)
            logger.debug(
                f"Window {window} solved: obj=${result['objective']:,.0f} | "
                f"balance: supply={supply:.1f} demand={demand_side:.1f} gap={gap:.1f} ({gap_pct:.1f}%) | "
                f"gen={gen_sum:.1f} ls={ls_sum:.1f} bat={bd_sum - bc_sum:.1f} ev={ev_v2g_sum - ev_ch_sum:.1f}"
            )

            # Detailed balance for first 3 windows per year
            if window < 3:
                logger.debug(
                    f"  BALANCE W{window} detail: "
                    f"gen={gen_sum:.1f} bat_dch={bd_sum:.1f} ev_v2g={ev_v2g_sum:.1f} "
                    f"ls={ls_sum:.1f} rooftop={rooftop_sum:.1f} | "
                    f"dem={dem_sum:.1f} bat_ch={bc_sum:.1f} ev_ch={ev_ch_sum:.1f} "
                    f"res_s={rs_sum:.1f} res_d={rd_sum:.1f}"
                )
                # Log per-generator output
                if go is not None:
                    for g, name in enumerate(self._gen_names):
                        if g < go.shape[0]:
                            g_sum = float(np.sum(go[g]))
                            g_max = float(np.max(go[g]))
                            logger.debug(f"  GEN W{window} [{g}] {name}: sum={g_sum:.2f} max={g_max:.2f} MW")
            return result

        except Exception as e:
            logger.error(f"Window {window} failed: {e}", exc_info=True)
            return None

    def _run_ac_verification(
        self,
        ps: PowerSystemAdapter,
        sys_cfg: SystemConfig,
        hours: int,
        window: int,
        year: int,
    ) -> dict:
        """
        Run Newton-Raphson AC power flow verification on selected hours.

        Uses the Julia-side PowerSystemInput and extracts a PowerSystemResult
        to pass to run_ac_power_flow(), which reads generator outputs and
        demand at each hour.

        Args:
            ps: Solved PowerSystemAdapter instance (model must be solved)
            sys_cfg: System configuration
            hours: Number of hours in this window
            window: Window index (for logging)
            year: Current year (for logging)

        Returns:
            Dictionary with AC PF summary results
        """
        from esfex.bridge.julia_setup import get_esfex_module

        ESFEX = get_esfex_module()
        ac_cfg = sys_cfg.ac_power_flow

        # Create Julia ACPowerFlowConfig
        jl_ac_config = ESFEX.ACPowerFlowConfig(
            max_iterations=ac_cfg.max_iterations,
            tolerance=ac_cfg.tolerance,
            base_mva=ac_cfg.base_mva,
            voltage_min_pu=ac_cfg.voltage_min_pu,
            voltage_max_pu=ac_cfg.voltage_max_pu,
        )

        # Extract Julia PowerSystemResult struct (needed by run_ac_power_flow)
        jl_result = ESFEX.extract_solution(ps._jl_model, ps._jl_vars, ps._jl_input)

        # Select hours to check
        if ac_cfg.check_hours == "all":
            check_hours = list(range(1, hours + 1))  # Julia 1-indexed
        elif ac_cfg.check_hours == "peak":
            # Find peak demand hour from the Julia result
            try:
                gen_out = np.array(jl_result.gen_output)
                if gen_out.ndim >= 2:
                    hourly_gen = gen_out.sum(axis=tuple(range(gen_out.ndim - 1)))
                    peak_hour = int(np.argmax(hourly_gen)) + 1  # Julia 1-indexed
                    check_hours = [peak_hour]
                else:
                    check_hours = [1]
            except Exception:
                check_hours = [1]
        else:  # "sample"
            step = max(1, hours // ac_cfg.sample_count)
            check_hours = list(range(1, hours + 1, step))[:ac_cfg.sample_count]

        # Run AC PF for each selected hour
        results_summary = {
            "hours_checked": len(check_hours),
            "converged_count": 0,
            "total_p_losses_mw": [],
            "voltage_violations": [],
            "line_overloads": [],
            "max_mismatch": [],
        }

        for hour in check_hours:
            try:
                ac_result = ESFEX.run_ac_power_flow(
                    ps._jl_input, jl_result, jl_ac_config, hour
                )
                if bool(ac_result.converged):
                    results_summary["converged_count"] += 1
                results_summary["total_p_losses_mw"].append(float(ac_result.total_p_losses))
                results_summary["max_mismatch"].append(float(ac_result.max_mismatch))

                # Log violations
                v_viols = list(ac_result.voltage_violations)
                l_overloads = list(ac_result.line_overloads)
                if v_viols:
                    results_summary["voltage_violations"].extend(
                        [(int(v[0]), float(v[1]), hour) for v in v_viols]
                    )
                if l_overloads:
                    results_summary["line_overloads"].extend(
                        [(int(o[0]), float(o[1]), float(o[2]), hour) for o in l_overloads]
                    )
            except Exception as e:
                logger.warning(f"AC PF failed for hour {hour} (y{year}/w{window}): {e}")

        # Log summary
        conv_pct = (results_summary["converged_count"] / max(1, len(check_hours))) * 100
        avg_losses = np.mean(results_summary["total_p_losses_mw"]) if results_summary["total_p_losses_mw"] else 0
        n_v_viols = len(results_summary["voltage_violations"])
        n_l_overloads = len(results_summary["line_overloads"])

        logger.debug(
            f"AC PF verification (y{year}/w{window}): "
            f"{conv_pct:.0f}% converged ({results_summary['converged_count']}/{len(check_hours)}), "
            f"avg losses={avg_losses:.1f} MW, "
            f"voltage violations={n_v_viols}, line overloads={n_l_overloads}"
        )

        return results_summary

    def _apply_master_builds(self, investments: dict[str, float]) -> None:
        """Add one year's master builds (transmission, converters) to the
        dispatch's network. The values are that year's increments, so every
        year must pass here once, also the years the dispatch skips."""
        if investments:
            self._apply_transfer_investments(investments)
            self._apply_converter_investments(investments)

    def _apply_converter_investments(self, investments: dict[str, float]) -> None:
        """Add the converter capacity the master built this year to the
        dispatch's converters (``converter_investment_{acdc|freq}_{c}`` keys,
        MW). Converters do not age, so the capacity stays for later years.
        The master and the dispatch list the converters in the same order.
        """
        for key, value in investments.items():
            kind, idx = _converter_investment_key(key)
            if kind is None:
                continue
            convs = getattr(self.primary_system, f"{kind}_converters", None) or []
            if idx < len(convs):
                convs[idx].rated_power_mva += float(value)
                logger.debug(f"Converter {convs[idx].name}: +{value:.1f} MW built")

    def _apply_transfer_investments(
        self,
        investments: dict[str, float],
    ) -> None:
        """Apply transmission investment decisions to the network.

        Updates BOTH:
        1. ``primary_system.nodes.nodes_connections`` (legacy adjacency
           matrix, still used by master DC-OPF).
        2. ``primary_system.transmission_lines_geo`` capacities — without
           this the operational bus-level DC-OPF never sees the master's
           transmission investments and continues to shed at the same
           rate it did before the investment, defeating the master's
           cost-minimisation rationale.

        The master's indices are its own buses. When the master is
        node-level (one bus per node, the merged multi-node case) the
        additional MW of a node pair ``(i, j)`` is distributed proportionally
        across the bus-level lines connecting those nodes. When the master
        shares the operational bus network (single-node systems), it goes to
        the lines joining exactly those two buses. If no such line exists,
        the value is retained only in the node adjacency matrix.

        Args:
            investments: Investment dict from master problem, may contain
                keys like ``transfer_investment_I_J``.
        """
        num_nodes = self._num_nodes
        conn = np.array(self.primary_system.nodes.nodes_connections).reshape(
            num_nodes, num_nodes,
        )
        # Pre-index bus → node for the operational system once.
        sys_op = self.primary_system
        buses_op = sys_op.buses or []
        bus_to_node_op = {i: b.parent_node for i, b in enumerate(buses_op)}
        # Group inter-node lines by node pair (sorted) and pre-compute
        # the current sum of their capacities for proportional scaling.
        from collections import defaultdict
        lines_by_pair: dict[tuple[int, int], list] = defaultdict(list)
        for ln in (sys_op.transmission_lines_geo or []):
            fb, tb = ln.from_bus, ln.to_bus
            if fb is None or tb is None:
                continue
            if fb not in bus_to_node_op or tb not in bus_to_node_op:
                continue
            ni, nj = bus_to_node_op[fb], bus_to_node_op[tb]
            if ni == nj:
                continue  # intra-node line — master invests on inter-node corridor
            key = (min(ni, nj), max(ni, nj))
            lines_by_pair[key].append(ln)

        # Master bus index → node, and whether the master indexes the
        # operational buses themselves.
        master_buses = self.master_system.buses or []
        bus_level = self.master_system is sys_op and bool(buses_op)

        def _master_node(idx: int) -> int:
            return master_buses[idx].parent_node if idx < len(master_buses) else idx

        lines_by_buses: dict[tuple[int, int], list] = defaultdict(list)
        if bus_level:
            for ln in (sys_op.transmission_lines_geo or []):
                fb, tb = ln.from_bus, ln.to_bus
                if fb is not None and tb is not None and fb != tb:
                    lines_by_buses[(min(fb, tb), max(fb, tb))].append(ln)

        # One value per corridor: when both i→j and j→i exist the master
        # holds them equal and its line capacity counts one of them.
        corridors: dict[tuple[int, int], float] = {}
        for key, value in investments.items():
            if not key.startswith("transfer_investment_"):
                continue
            parts = key.split("_")
            if len(parts) >= 4:
                i_bus, j_bus = int(parts[2]), int(parts[3])
                pair = (min(i_bus, j_bus), max(i_bus, j_bus))
                corridors[pair] = max(corridors.get(pair, 0.0), float(value))

        applied = 0
        scaled_bus_lines = 0
        for (i_bus, j_bus), value in corridors.items():
            i_idx, j_idx = _master_node(i_bus), _master_node(j_bus)
            if 0 <= i_idx < num_nodes and 0 <= j_idx < num_nodes:
                if i_idx != j_idx:
                    conn[i_idx, j_idx] += value
                    conn[j_idx, i_idx] += value
                applied += 1
                # Mirror into the bus-level lines of the same corridor.
                if bus_level:
                    lines = lines_by_buses.get((i_bus, j_bus), [])
                else:
                    lines = lines_by_pair.get((min(i_idx, j_idx), max(i_idx, j_idx)), [])
                if lines:
                    total_cap = sum(
                        float(getattr(ln, "capacity_mw", 0.0) or 0.0)
                        for ln in lines
                    )
                    add_mw = value
                    if total_cap > 0:
                        # Proportional scaling
                        for ln in lines:
                            cap_i = float(getattr(ln, "capacity_mw", 0.0) or 0.0)
                            share = cap_i / total_cap
                            ln.capacity_mw = cap_i + share * add_mw
                            scaled_bus_lines += 1
                    else:
                        # Existing lines have zero capacity — split equally.
                        per_line = add_mw / len(lines)
                        for ln in lines:
                            ln.capacity_mw = (
                                float(getattr(ln, "capacity_mw", 0.0) or 0.0)
                                + per_line
                            )
                            scaled_bus_lines += 1
        if applied > 0:
            new_conn = conn.flatten().tolist()
            self.primary_system.nodes.nodes_connections = new_conn
            self.master_system.nodes.nodes_connections = new_conn
            logger.debug(
                f"Applied {applied} transmission investments to adjacency matrix "
                f"({scaled_bus_lines} bus-level lines scaled)"
            )

    def _rebuild_unit_names(self, units_config: dict[str, Any]) -> None:
        """Rebuild generator/battery name lists from units_config.

        Must replicate the EXACT same ordering and filtering as the
        PowerSystemAdapter._create_input() method, so that name indices
        match the Julia solver output indices.

        Order:
          1. Original generators (from sys.generators)
          2. Virtual generators (from units_config, not in sys.generators)
          3. Original batteries (from sys.batteries)
          4. Virtual batteries (from units_config, not in sys.batteries)
        """
        sys = self.primary_system

        # --- Generator names + parallel fuel list ---
        # _gen_fuels mirrors _gen_names index-for-index so the export
        # can stamp each generation dataset with ``attrs["fuel"]`` and
        # the dashboard can bucket by fuel without parsing plant names.
        gen_names: list[str] = []
        gen_fuels: list[str] = []
        # 1) Original generators (same order as sys.generators iteration)
        for key, gen in sys.generators.items():
            gen_names.append(gen.name if hasattr(gen, 'name') else key)
            gen_fuels.append(str(getattr(gen, 'fuel', '') or ''))

        # 2) Virtual generators from units_config (matching adapter filter)
        for key, vdata in units_config.items():
            if key in sys.generators:
                continue
            if vdata.get("_type") == "battery" or vdata.get("type") == "Storage":
                continue
            if "rated_power" not in vdata:
                continue
            rp = vdata["rated_power"]
            if not rp or max(rp) < 0.01:
                continue
            gen_names.append(vdata.get("name", key))
            # Investment gens carry their technology's fuel in vdata
            # (populated when they were synthesised from a tech).
            gen_fuels.append(str(vdata.get("fuel", "") or ""))

        # --- Battery names ---
        bat_names = []
        # 1) Original batteries (same order as sys.batteries iteration)
        for key, bat in sys.batteries.items():
            bat_names.append(bat.name if hasattr(bat, 'name') else key)

        # 2) Virtual batteries from units_config (matching adapter filter)
        for key, vdata in units_config.items():
            if key in sys.batteries:
                continue
            if vdata.get("_type") != "battery" and vdata.get("type") != "Storage":
                continue
            cap = vdata.get("capacity", [])
            charge_pow = vdata.get("MaxChargePower", [])
            if not cap or (max(cap) < 0.01 and max(charge_pow) < 0.01):
                continue
            bat_names.append(vdata.get("name", key))

        # Homonyms get a " (N)" suffix (HDF5 needs unique dataset names);
        # order and length are kept, so the fuel list stays index-aligned.
        self._gen_names = unique_unit_names(gen_names)
        self._gen_fuels = list(gen_fuels)
        self._bat_names = unique_unit_names(bat_names)

    # ------------------------------------------------------------------
    # System-unit mapping (for per-system HDF5 export)
    # ------------------------------------------------------------------

    def _build_system_unit_mapping(self) -> dict[str, dict]:
        """Map generators/batteries to their owning subsystem.

        Uses name prefixes (``"IslaJuventud/Solar PV"``,
        ``"Investment Cuba/Solar PV"``) to assign each unit in
        ``self._gen_names`` / ``self._bat_names`` to the correct system.

        Returns
        -------
        dict
            ``{system_name: {"gen_indices": [...], "bat_indices": [...],
            "gen_names": [...], "bat_names": [...],
            "node_offset": int, "node_count": int}}``
        """
        if not self._system_node_offsets or len(self._system_node_offsets) <= 1:
            # Single system — everything belongs to it
            sname = list(self._system_node_offsets.keys())[0] if self._system_node_offsets else self.system_name
            sys_cfg = self.config.systems.get(sname, None)
            nn = (sys_cfg.nodes.num_nodes if sys_cfg else
                  self.primary_system.nodes.num_nodes) or 1
            return {
                sname: {
                    "gen_indices": list(range(len(self._gen_names))),
                    "bat_indices": list(range(len(self._bat_names))),
                    "gen_names": list(self._gen_names),
                    "bat_names": list(self._bat_names),
                    "node_offset": 0,
                    "node_count": nn,
                }
            }

        sys_names = list(self._system_node_offsets.keys())

        # Compute node counts from offsets + total
        total_nodes = self.primary_system.nodes.num_nodes or 1
        sys_info: dict[str, dict] = {}
        for i, sname in enumerate(sys_names):
            off = self._system_node_offsets[sname]
            if i + 1 < len(sys_names):
                cnt = list(self._system_node_offsets.values())[i + 1] - off
            else:
                cnt = total_nodes - off
            sys_info[sname] = {
                "gen_indices": [], "bat_indices": [],
                "gen_names": [], "bat_names": [],
                "node_offset": off, "node_count": cnt,
            }

        def _match_system(name: str) -> str | None:
            """Extract system name from unit display name."""
            # Strip "Investment " prefix if present
            clean = name
            if clean.startswith("Investment "):
                clean = clean[len("Investment "):]
            # Try prefix before "/"
            if "/" in clean:
                prefix = clean.split("/")[0]
                if prefix in sys_info:
                    return prefix
            # Fallback: check all system names as substring
            for sn in sys_names:
                if sn in name:
                    return sn
            return None

        # Assign generators
        for g_idx, gname in enumerate(self._gen_names):
            sys = _match_system(gname)
            if sys:
                sys_info[sys]["gen_indices"].append(g_idx)
                sys_info[sys]["gen_names"].append(gname)
            else:
                # Fallback: assign to first system
                sys_info[sys_names[0]]["gen_indices"].append(g_idx)
                sys_info[sys_names[0]]["gen_names"].append(gname)

        # Assign batteries
        for b_idx, bname in enumerate(self._bat_names):
            sys = _match_system(bname)
            if sys:
                sys_info[sys]["bat_indices"].append(b_idx)
                sys_info[sys]["bat_names"].append(bname)
            else:
                sys_info[sys_names[0]]["bat_indices"].append(b_idx)
                sys_info[sys_names[0]]["bat_names"].append(bname)

        return sys_info

    def _build_config_from_cumulative(
        self,
        base_units_config: dict[str, Any],
        year_caps: dict[str, dict],
    ) -> dict[str, Any]:
        """
        Build units config from MasterProblem's cumulative capacities.

        Uses the MasterProblem's correctly computed cumulative capacity which
        accounts for:
        - Existing units: age-based retirement + degradation
        - Investments: each with its own age tracking (NOT retired when
          the original unit retires)

        Args:
            base_units_config: Original units config (for structure/metadata)
            year_caps: {'gen': {julia_g_idx: np.array(per_bus)},
                        'bat': {julia_b_idx: np.array(per_bus)}}
        """
        updated = deepcopy(base_units_config)

        # Build bus_to_node mapping (needed whenever the bus list is not the
        # identity mapping over nodes, including fewer-buses-than-nodes).
        from esfex.bridge.converters import node_bus_mapping_required
        sys = self.primary_system
        num_nodes = sys.nodes.num_nodes or 1
        buses = sys.buses or []
        num_buses = len(buses) if buses else num_nodes
        if node_bus_mapping_required(buses, num_nodes):
            bus_to_node = [b.parent_node for b in buses]
        else:
            bus_to_node = None

        # Map gen/bat indices to unit keys
        gen_idx_to_key = {}
        bat_idx_to_key = {}
        gen_idx = 0
        bat_idx = 0
        for unit_key, unit_data in updated.items():
            if unit_data.get("_type") == "battery" or unit_data.get("type") == "Storage":
                bat_idx_to_key[bat_idx] = unit_key
                bat_idx += 1
            else:
                gen_idx_to_key[gen_idx] = unit_key
                gen_idx += 1

        # Apply generator cumulative capacities
        # The MasterProblem's cumulative capacity already includes:
        # - Age-based retirement (existing units set to 0 when age >= lifetime)
        # - Degradation ((1 - deg_rate)^age for existing, per-investment for new)
        # - Investment accumulation (each with its own age tracking)
        # So we must ZERO OUT degradation_rate and initial_age to prevent
        # power_system.jl from applying them again (double degradation/retirement).
        gen_caps = year_caps.get('gen', {})
        # Detect master granularity (same logic as for tech_caps below).
        gen_master_per_node = (
            bus_to_node is not None
            and gen_caps
            and len(next(iter(gen_caps.values()))) == num_nodes
        )
        for g_jl, bus_caps in gen_caps.items():
            py_idx = int(g_jl) - 1  # Julia 1-based → Python 0-based
            unit_key = gen_idx_to_key.get(py_idx)
            if unit_key and unit_key in updated:
                bus_arr = np.array(bus_caps)
                if gen_master_per_node:
                    # Master output is already per-node; copy directly.
                    node_caps = np.array(bus_arr[:num_nodes], dtype=float)
                    if len(node_caps) < num_nodes:
                        node_caps = np.concatenate([
                            node_caps, np.zeros(num_nodes - len(node_caps))
                        ])
                else:
                    # Aggregate per-bus capacities to per-node by
                    # mapping each bus to its parent node.
                    node_caps = np.zeros(num_nodes)
                    for b_idx in range(len(bus_arr)):
                        n_idx = bus_to_node[b_idx] if bus_to_node else b_idx
                        if n_idx < num_nodes:
                            node_caps[n_idx] += bus_arr[b_idx]
                updated[unit_key]["rated_power"] = node_caps.tolist()
                # Prevent double degradation/retirement in power_system.jl
                updated[unit_key]["degradation_rate"] = [0.0] * num_nodes
                updated[unit_key]["initial_age"] = [0] * num_nodes

        # Reservoir capacity the master built up to this year (MWh per node,
        # see _add_reservoir_caps) is existing capacity for the dispatch
        for (g, node), mwh in year_caps.get('reservoir', {}).items():
            unit_key = gen_idx_to_key.get(int(g))
            if unit_key in updated and 0 <= node < num_nodes:
                cap = _pad_zeros(updated[unit_key].get("reservoir_capacity"), num_nodes)
                cap[node] += float(mwh)
                updated[unit_key]["reservoir_capacity"] = cap

        # Note: battery cumulative from MasterProblem is ENERGY capacity (MWh),
        # not power (MW). Battery power/capacity in config is handled separately
        # via the operational dispatch's own age-based retirement logic.

        # Create virtual generators from technology investments
        # Keep per-bus arrays so virtual generators inject at the correct buses
        # (where the master problem decided to invest).
        tech_caps = year_caps.get('tech', {})
        tech_keys = list(self.primary_system.technologies.keys())

        # Detect master granularity: master may run on a node-level network
        # (when `primary_system.buses` is empty after merge → converters
        # auto-create one bus per node, so `n_buses == num_nodes`).  In that
        # case the per-bus arrays returned by Julia are actually **per-node**
        # (length num_nodes) and must be mapped onto the bus-level
        # operational network.  Each node's investment is placed at the
        # node's first (canonical) bus — a deterministic, neutral mapping.
        # (No demand-fraction redistribution heuristic: capacity is placed
        # organically and the DC-OPF routes it; if a single-bus injection is
        # infeasible that indicates a real model issue to fix, not to mask.)
        master_per_node = (
            bus_to_node is not None
            and tech_caps
            and len(next(iter(tech_caps.values()))) == num_nodes
        )
        first_bus_of_node: dict[int, int] | None = None
        node_buses_map: dict[int, list[int]] | None = None
        if master_per_node or bus_to_node is not None:
            first_bus_of_node = {}
            node_buses_map = {}
            for b_idx, b in enumerate(buses):
                first_bus_of_node.setdefault(b.parent_node, b_idx)
                node_buses_map.setdefault(b.parent_node, []).append(b_idx)

        for t_jl, bus_caps in tech_caps.items():
            t_py = int(t_jl) - 1  # Julia 1-based → Python 0-based
            if t_py < len(tech_keys):
                tech_key = tech_keys[t_py]
                tech = self.primary_system.technologies[tech_key]

                bus_arr = np.array(bus_caps)
                if master_per_node:
                    # The master plans per-node capacity assuming copper-plate
                    # within the node. The operational enforces bus-level
                    # transmission, so the placement of that per-node capacity
                    # onto buses matters. Distribute it by demand_fraction so
                    # capacity co-locates with the node's load — this keeps the
                    # node self-serving (no intra-node transformer binds), which
                    # is what the master's copper-plate assumption implies.
                    # Even-split (the previous behavior) stranded capacity on
                    # non-demand buses, leaving the demand bus transformer-limited
                    # and shedding load (e.g. Pinar bus304, fed by one 250 MVA
                    # transformer while carrying 100% of the node's demand).
                    # Fall back to even-split when the node carries no demand.
                    bus_rated = [0.0] * num_buses
                    for n_idx in range(min(len(bus_arr), num_nodes)):
                        v = float(bus_arr[n_idx])
                        if v <= 0:
                            continue
                        node_buses = node_buses_map.get(n_idx, [])
                        if not node_buses:
                            continue
                        dfracs = [
                            max(0.0, float(getattr(buses[b], 'demand_fraction', 0.0) or 0.0))
                            for b in node_buses
                        ]
                        dtot = sum(dfracs)
                        if dtot > 0:
                            for b_idx, df in zip(node_buses, dfracs):
                                bus_rated[b_idx] = v * df / dtot
                        else:
                            per_bus = v / len(node_buses)
                            for b_idx in node_buses:
                                bus_rated[b_idx] = per_bus
                else:
                    bus_rated = list(bus_arr[:num_buses])
                    # Pad to num_buses if shorter
                    while len(bus_rated) < num_buses:
                        bus_rated.append(0.0)

                if max(bus_rated) < 0.1:
                    continue

                # Helper to replicate a per-node property to per-bus length.
                # Critical: when arr is a per-node array padded by _merge_systems
                # (len == total_nodes, with 0 at other systems' offsets), the
                # legacy `[arr[0]]*n` fallback would broadcast the padding-zero
                # of the OTHER system to all buses, zeroing this system's
                # property. Use bus_to_node mapping to put each bus's node's
                # value at that bus.
                def _rep(arr, default=0.0, n=num_buses):
                    if not arr:
                        return [default] * n
                    if len(arr) >= n:
                        return list(arr[:n])
                    if len(arr) == 1:
                        return [arr[0]] * n
                    if bus_to_node is not None and len(arr) <= len(bus_to_node):
                        out = [0.0] * n
                        for _b in range(n):
                            _nidx = bus_to_node[_b]
                            if _nidx < len(arr):
                                out[_b] = arr[_nidx]
                        return out
                    return [arr[0]] * n

                def _rep_curve(curves, n=num_buses):
                    # The technology's per-node bidding curves, per bus. A
                    # bus whose node has no curve gets a flat one (its
                    # fuel_cost). None when the technology has no curve.
                    if not curves:
                        return None
                    if len(curves) == 1:
                        return [curves[0]] * n
                    out = []
                    for _b in range(n):
                        _nidx = bus_to_node[_b] if bus_to_node is not None else _b
                        out.append(curves[_nidx] if _nidx < len(curves)
                                   else CostCurveConfig())
                    return out

                vgen_key = f"inv_{tech_key}"
                updated[vgen_key] = {
                    **technology_unit_fields(tech, _rep, _rep_curve),
                    "rated_power": bus_rated,
                    "Availability": tech.availability_file,
                    # Already degraded by master problem — prevent double degradation
                    "degradation_rate": [0.0] * num_buses,
                    "initial_age": [0] * num_buses,
                    "life_time": [999] * num_buses,
                    "invest_cost": [0.0] * num_buses,
                    "invest_max_power": [0.0] * num_buses,
                    "decommissioning_cost": [0.0] * num_buses,
                    "_is_per_bus": True,  # Flag: arrays are already per-bus
                }
                logger.debug(f"Virtual generator: {vgen_key} ({tech.name}) → {bus_rated} MW (per-bus)")

        # Create virtual batteries from battery technology investments
        # Keep per-bus arrays so virtual batteries inject at the correct buses.
        # Same per-node→per-bus expansion as virtual generators when the
        # master ran on a node-level network.
        bat_tech_pow_caps = year_caps.get('bat_tech_power', {})
        bat_tech_cap_caps = year_caps.get('bat_tech_capacity', {})
        bat_tech_keys = list(self.primary_system.battery_technologies.keys())
        bat_master_per_node = (
            bus_to_node is not None
            and bat_tech_pow_caps
            and len(next(iter(bat_tech_pow_caps.values()))) == num_nodes
        )
        if bat_master_per_node and first_bus_of_node is None:
            first_bus_of_node = {}
            for b_idx, b in enumerate(buses):
                first_bus_of_node.setdefault(b.parent_node, b_idx)
        for bt_jl in set(list(bat_tech_pow_caps.keys()) + list(bat_tech_cap_caps.keys())):
            bt_py = int(bt_jl) - 1
            if bt_py < len(bat_tech_keys):
                bt_key = bat_tech_keys[bt_py]
                bat_tech = self.primary_system.battery_technologies[bt_key]

                pow_arr = np.array(bat_tech_pow_caps.get(bt_jl, np.zeros(num_buses)))
                cap_arr = np.array(bat_tech_cap_caps.get(bt_jl, np.zeros(num_buses)))

                if bat_master_per_node:
                    # Distribute per-node battery power/energy by demand_fraction
                    # so storage co-locates with the node's load (same rationale
                    # as the generator placement above: the master is per-node
                    # copper-plate, so storage must sit at the demand bus to be
                    # usable without binding the node's internal transformer).
                    # Fall back to the node's first bus when it carries no demand.
                    bus_pow = [0.0] * num_buses
                    bus_cap = [0.0] * num_buses
                    for n_idx in range(num_nodes):
                        v_pow = float(pow_arr[n_idx]) if n_idx < len(pow_arr) else 0.0
                        v_cap = float(cap_arr[n_idx]) if n_idx < len(cap_arr) else 0.0
                        if v_pow <= 0 and v_cap <= 0:
                            continue
                        node_buses = node_buses_map.get(n_idx, []) if node_buses_map else []
                        dfracs = [
                            max(0.0, float(getattr(buses[b], 'demand_fraction', 0.0) or 0.0))
                            for b in node_buses
                        ]
                        dtot = sum(dfracs)
                        if node_buses and dtot > 0:
                            for b_idx, df in zip(node_buses, dfracs):
                                bus_pow[b_idx] += v_pow * df / dtot
                                bus_cap[b_idx] += v_cap * df / dtot
                        else:
                            tgt = first_bus_of_node.get(n_idx)
                            if tgt is not None:
                                bus_pow[tgt] += v_pow
                                bus_cap[tgt] += v_cap
                else:
                    bus_pow = list(pow_arr[:num_buses])
                    bus_cap = list(cap_arr[:num_buses])
                    while len(bus_pow) < num_buses:
                        bus_pow.append(0.0)
                    while len(bus_cap) < num_buses:
                        bus_cap.append(0.0)

                if max(bus_pow) < 0.1 and max(bus_cap) < 0.1:
                    continue

                def _brep(arr, n=num_buses, default=0.0):
                    if not arr:
                        return [default] * n
                    if len(arr) >= n:
                        return list(arr[:n])
                    if len(arr) == 1:
                        return [arr[0]] * n
                    # Per-node array (padded by _merge_systems with 0 at other
                    # systems' offsets): expand to per-bus via bus_to_node.
                    # Without this, [arr[0]]*n broadcasts the padding-zero of
                    # the OTHER system to all buses — for batteries this means
                    # zero maintenance_cost and zero throughput_degradation_cost
                    # in the LP, which removes the incentive against the
                    # simultaneous charge+discharge LP-relaxation pathology.
                    if bus_to_node is not None and len(arr) <= len(bus_to_node):
                        out = [default] * n
                        for _b in range(n):
                            _nidx = bus_to_node[_b]
                            if _nidx < len(arr):
                                out[_b] = arr[_nidx]
                        return out
                    return [arr[0]] * n

                vbat_key = f"inv_{bt_key}"
                updated[vbat_key] = {
                    **battery_technology_unit_fields(
                        bat_tech, lambda arr, default: _brep(arr, default=default)),
                    "capacity": bus_cap,
                    "MaxChargePower": bus_pow,
                    "MaxDischargePower": bus_pow,
                    # Already degraded by master problem
                    "degradation_rate": [0.0] * num_buses,
                    "initial_age": [0] * num_buses,
                    "life_time": [999] * num_buses,
                    "invest_cost": [0.0] * num_buses,
                    "invest_cost_energy": [0.0] * num_buses,
                    "invest_max_power": [0.0] * num_buses,
                    "invest_max_capacity": [0.0] * num_buses,
                    "decommissioning_cost": [0.0] * num_buses,
                    "_is_per_bus": True,  # Flag: arrays are already per-bus
                }
                # DEBUG: emitted per bus → noisy at INFO. The aggregate
                # "Year X investments" log a few lines up covers the
                # milestone view.
                logger.debug(f"Virtual battery: {vbat_key} ({bat_tech.name}) → pow={bus_pow} MW, cap={bus_cap} MWh (per-bus)")

        return updated

    def _apply_retirements_to_config(
        self,
        units_config: dict[str, Any],
        retirements: dict[str, float],
    ) -> dict[str, Any]:
        """
        Apply retirement decisions to the units configuration.

        Reduces or removes capacity from generators/batteries based on
        MasterProblem retirement decisions.

        Args:
            units_config: Current units configuration
            retirements: Retirement decisions {key: fraction_retired}

        Returns:
            Updated units configuration (new copy)
        """
        if not retirements:
            return units_config

        updated = deepcopy(units_config)

        # Build index-to-key mapping
        gen_idx_to_key = {}
        gen_idx = 0
        for unit_key, unit_data in updated.items():
            if unit_data.get("_type") != "battery" and unit_data.get("type") != "Storage":
                gen_idx_to_key[gen_idx] = unit_key
                gen_idx += 1

        for ret_key, ret_fraction in retirements.items():
            if ret_fraction <= 0:
                continue

            if ret_key.startswith("gen_"):
                idx = int(ret_key.split("_")[1])
                unit_key = gen_idx_to_key.get(idx)
                if unit_key and unit_key in updated:
                    rated = updated[unit_key].get("rated_power", [])
                    if rated:
                        # Reduce capacity by retirement fraction
                        updated[unit_key]["rated_power"] = [
                            max(0, r * (1 - ret_fraction)) for r in rated
                        ]
                        name = updated[unit_key].get("name", unit_key)
                        logger.debug(f"Retirement: {name} reduced by {ret_fraction*100:.0f}%")

        return updated

    def _initialize_hdf5(self, path: Path, num_nodes: int, num_years: int,
                         start_year: int, end_year: int):
        """Initialize HDF5 output file with legacy-compatible structure."""
        import h5py

        temporal_resolution = self.config.temporal.resolution_hours

        with h5py.File(path, "w") as f:
            # ==============================================================
            # METADATA (matching legacy structure)
            # ==============================================================
            f.attrs["creation_date"] = datetime.now().isoformat()
            f.attrs["num_nodes"] = num_nodes
            f.attrs["num_years"] = num_years
            f.attrs["export_type"] = "incremental_results"
            f.attrs["temporal_resolution_hours"] = temporal_resolution
            f.attrs["export_complete"] = False
            f.attrs["simulation_mode"] = self.config.simulation_mode
            f.attrs["years_range"] = f"{start_year}-{end_year - 1}"
            if hasattr(self.config, "re_target"):
                f.attrs["target_re"] = self.config.re_target

            # Multi-system mapping: which nodes belong to which subsystem
            if self._system_node_offsets:
                f.attrs["subsystem_names"] = list(self._system_node_offsets.keys())
                f.attrs["subsystem_offsets"] = list(self._system_node_offsets.values())
                # Per-subsystem node counts
                import math
                subsys_node_counts = []
                for sname in self._system_node_offsets:
                    sys = self.config.systems[sname]
                    nn = sys.nodes.num_nodes or int(math.sqrt(len(sys.nodes.nodes_connections)))
                    subsys_node_counts.append(nn)
                f.attrs["subsystem_node_counts"] = subsys_node_counts

            # ==============================================================
            # SYSTEM CONFIGURATION (static, written once)
            # ==============================================================
            config_group = f.create_group("system_configuration")
            config_group.attrs["description"] = "System configuration data"

            # Store generators configuration
            gen_group = config_group.create_group("generators")
            gen_group.attrs["num_generators"] = len(self._gen_names)

            for i, (key, gen) in enumerate(self.primary_system.generators.items()):
                gen_subgroup = gen_group.create_group(f"generator_{i}")
                gen_data = gen.model_dump()
                for k, v in gen_data.items():
                    if isinstance(v, (str, int, float, bool)):
                        gen_subgroup.attrs[k] = v
                    else:
                        gen_subgroup.attrs[k] = str(v)

            # Store batteries configuration
            bat_group = config_group.create_group("batteries")
            bat_group.attrs["num_batteries"] = len(self._bat_names)

            for i, (key, bat) in enumerate(self.primary_system.batteries.items()):
                bat_subgroup = bat_group.create_group(f"battery_{i}")
                bat_data = bat.model_dump()
                for k, v in bat_data.items():
                    if isinstance(v, (str, int, float, bool)):
                        bat_subgroup.attrs[k] = v
                    else:
                        bat_subgroup.attrs[k] = str(v)

            # Store nodes configuration
            nodes_group = config_group.create_group("nodes")
            nodes_group.attrs["num_nodes"] = num_nodes
            if hasattr(self.primary_system, "nodes"):
                nodes_data = self.primary_system.nodes.model_dump()
                for k, v in nodes_data.items():
                    if isinstance(v, (str, int, float, bool)):
                        nodes_group.attrs[k] = v
                    elif isinstance(v, (list, np.ndarray)):
                        arr = np.array(v)
                        if arr.dtype.kind in ("U", "O"):
                            # String/object arrays: store as attribute
                            nodes_group.attrs[k] = str(v)
                        else:
                            nodes_group.create_dataset(k, data=arr)
                    else:
                        nodes_group.attrs[k] = str(v)

                # Store node coordinates as separate lat/lng datasets
                # (GeoCoordinate objects are dicts after model_dump, need
                # explicit extraction for the GUI results viewer)
                coords = self.primary_system.nodes.node_coordinates
                if coords:
                    lats = [c.latitude if c else 0.0 for c in coords]
                    lngs = [c.longitude if c else 0.0 for c in coords]
                    labels = [c.label or f"Node {i}" if c else f"Node {i}"
                              for i, c in enumerate(coords)]
                    nodes_group.create_dataset("latitude", data=np.array(lats))
                    nodes_group.create_dataset("longitude", data=np.array(lngs))
                    # Store node names (use labels from coordinates or node_names)
                    node_name_list = self.primary_system.nodes.node_names or labels
                    nodes_group.create_dataset(
                        "name",
                        data=np.array(node_name_list, dtype=h5py.string_dtype()),
                    )

            # Store technology investment configs (for chart investment display)
            if self.primary_system.technologies:
                tech_group = config_group.create_group("technologies")
                tech_group.attrs["num_technologies"] = len(self.primary_system.technologies)
                for i, (key, tech) in enumerate(self.primary_system.technologies.items()):
                    tg = tech_group.create_group(f"technology_{i}")
                    tg.attrs["key"] = key
                    tg.attrs["name"] = tech.name
                    tg.attrs["type"] = tech.type
                    tg.attrs["fuel"] = tech.fuel
                    tg.attrs["invest_cost"] = str(list(tech.invest_cost))
                    if tech.color:
                        tg.attrs["color"] = tech.color

            # Store battery technology investment configs
            if self.primary_system.battery_technologies:
                bt_group = config_group.create_group("battery_technologies")
                bt_group.attrs["num_battery_technologies"] = len(
                    self.primary_system.battery_technologies
                )
                for i, (key, bt) in enumerate(
                    self.primary_system.battery_technologies.items()
                ):
                    btg = bt_group.create_group(f"battery_technology_{i}")
                    btg.attrs["key"] = key
                    btg.attrs["name"] = bt.name
                    btg.attrs["invest_cost_power"] = str(list(bt.invest_cost_power))
                    btg.attrs["invest_cost_energy"] = str(list(bt.invest_cost_energy))
                    if bt.color:
                        btg.attrs["color"] = bt.color

            # Store transmission line geometry for results visualization
            sys = self.primary_system
            if sys.transmission_lines_geo:
                tl_group = config_group.create_group("transmission_lines")
                tl_group.attrs["num_lines"] = len(sys.transmission_lines_geo)
                for i, tl in enumerate(sys.transmission_lines_geo):
                    tlg = tl_group.create_group(f"line_{i}")
                    tlg.attrs["line_id"] = tl.line_id or f"line_{i}"
                    tlg.attrs["from_node"] = tl.from_node
                    tlg.attrs["to_node"] = tl.to_node
                    tlg.attrs["capacity_mw"] = tl.capacity_mw or 0.0
                    if tl.waypoints:
                        wp_arr = np.array(
                            [[wp.latitude, wp.longitude] for wp in tl.waypoints],
                            dtype=np.float64,
                        )
                        tlg.create_dataset("waypoints", data=wp_arr)

            # Store fuel route geometry for results visualization
            if sys.fuel_transport_routes:
                fr_group = config_group.create_group("fuel_routes")
                fr_group.attrs["num_routes"] = len(sys.fuel_transport_routes)
                for i, rt in enumerate(sys.fuel_transport_routes):
                    rg = fr_group.create_group(f"route_{i}")
                    rg.attrs["route_id"] = rt.get("route_id", f"route_{i}")
                    rg.attrs["from_node"] = rt.get("from_node", 0)
                    rg.attrs["to_node"] = rt.get("to_node", 0)
                    rg.attrs["distance_km"] = rt.get("distance_km", 0.0)
                    wps = rt.get("waypoints", [])
                    if wps:
                        wp_arr = np.array(
                            [[wp.get("latitude", wp.get("lat", 0)),
                              wp.get("longitude", wp.get("lng", 0))]
                             for wp in wps],
                            dtype=np.float64,
                        )
                        rg.create_dataset("waypoints", data=wp_arr)

            # ==============================================================
            # CREATE GROUPS FOR INCREMENTAL DATA
            # ==============================================================
            f.create_group("summary_results")
            f.create_group("detailed_results")
            f.create_group("demand")

            # ==============================================================
            # GLOBAL + INTER-SYSTEM STRUCTURE
            # ==============================================================
            # The ``/systems/{name}/`` group used to mirror per-system
            # detailed_results, summary_results and system_configuration.
            # All three are now derived on read from the root block via
            # ``subsystem_offsets`` / ``subsystem_node_counts`` (see
            # ``results_charts._open_scenario`` and
            # ``_open_system_config``), so the group is no longer
            # written. Subsystem names live in a root attribute below.
            f.attrs["num_systems"] = len(self._system_node_offsets) or 1

            global_grp = f.create_group("global")
            global_grp.create_group("summary_results")
            global_grp.create_group("cost_breakdown")
            global_grp.create_group("demand")

            f.create_group("inter_system").create_group("detailed_results")

        logger.debug(f"Initialized HDF5: {path}")

    @staticmethod
    def _h5safe(name: str) -> str:
        """Sanitize a name for use as an HDF5 dataset name.

        HDF5 interprets ``/`` as a path separator, so names like
        ``"Cuba/Solar PV"`` would create nested groups instead of a
        flat dataset.  Replace ``/`` with `` - ``.
        """
        return name.replace("/", " - ")

    @classmethod
    def _h5_unit_names(cls, names: list[str]) -> list[str]:
        """HDF5 dataset names for a unit list, index-aligned and unique.

        Names are passed through ``_h5safe`` and made unique with
        :func:`unique_unit_names` (a homonym, or ``"A/B"`` next to
        ``"A - B"``, gets a `` (1)``, `` (2)``… suffix).
        """
        return unique_unit_names([cls._h5safe(str(name)) for name in names])

    def _append_year_to_hdf5(self, path: Path, result: YearResults):
        """Append year results to HDF5 file with legacy-compatible structure."""
        import h5py

        num_nodes = self._num_nodes
        hours = result.gen_output.shape[-1] if result.gen_output is not None else 0
        # One dataset name per unit, shared by every per-generator and
        # per-battery group so the same unit has the same name everywhere.
        gen_ds = self._h5_unit_names(self._gen_names)
        bat_ds = self._h5_unit_names(self._bat_names)

        with h5py.File(path, "a") as f:
            # ==============================================================
            # UPDATE SUMMARY RESULTS (expandable datasets)
            # ==============================================================
            summary_group = f["summary_results"]

            summary_data = {
                "year": result.year,
                "threshold": 0,
                "feasible": int(result.feasible),
                "total_cost": result.objective,
                "renewable_penetration": result.re_penetration,
                "co2_emissions": result.emissions,
                "loss_of_load": result.load_shed,
                "n1_security_cost": result.n1_security_cost,
            }

            for col, value in summary_data.items():
                if col in summary_group:
                    dataset = summary_group[col]
                    old_size = dataset.shape[0]
                    dataset.resize((old_size + 1,))
                    dataset[old_size] = value
                else:
                    summary_group.create_dataset(
                        col, data=[value], maxshape=(None,), chunks=True
                    )

            # ==============================================================
            # STORE DETAILED RESULTS FOR THIS YEAR
            # ==============================================================
            detailed_group = f["detailed_results"]

            scenario_name = f"year_{result.year}_threshold_0"
            if scenario_name in detailed_group:
                del detailed_group[scenario_name]

            scenario = detailed_group.create_group(scenario_name)
            scenario.attrs["year"] = result.year
            scenario.attrs["threshold"] = 0
            scenario.attrs["feasible"] = result.feasible
            scenario.attrs["total_cost"] = result.objective
            scenario.attrs["renewable_penetration"] = result.re_penetration
            scenario.attrs["co2_emissions"] = result.emissions
            scenario.attrs["solve_time"] = result.solve_time
            scenario.attrs["total_generation"] = result.total_generation
            scenario.attrs["total_demand"] = result.total_demand
            scenario.attrs["load_shed"] = result.load_shed
            scenario.attrs["master_re_target"] = result.master_re_target

            # --- Generation data (per generator) ---
            if result.gen_output is not None:
                gen_group = scenario.create_group("generation")
                gen_group.attrs["description"] = (
                    "Generation output [nodes x hours] for each generator"
                )
                # Parallel fuel list — same order as self._gen_names,
                # populated by setup / _rebuild_unit_names. Index-safe
                # fallback: empty string if for some reason an entry is
                # missing (shouldn't happen, but the export should never
                # crash on stale state).
                gen_fuels = getattr(self, '_gen_fuels', []) or []
                for g, name in enumerate(self._gen_names):
                    if g < result.gen_output.shape[0]:
                        ds_name = gen_ds[g]
                        ds = gen_group.create_dataset(
                            ds_name, data=result.gen_output[g],
                            chunks=True, compression="gzip",
                        )
                        ds.attrs["shape"] = f"[{num_nodes} nodes x {hours} hours]"
                        ds.attrs["units"] = "MW"
                        # Declared fuel for this generator — the dashboard
                        # reads this directly to bucket by tech without
                        # parsing the plant's name. Empty string keeps
                        # HDF5 happy when the source data didn't supply
                        # one (e.g. legacy configs).
                        ds.attrs["fuel"] = gen_fuels[g] if g < len(gen_fuels) else ""

            # --- Generator status (unit commitment on/off) ---
            if result.gen_status is not None:
                status_group = scenario.create_group("gen_status")
                status_group.attrs["description"] = (
                    "Generator on/off status [nodes x hours] for each generator (1=on, 0=off)"
                )
                for g, name in enumerate(self._gen_names):
                    if g < result.gen_status.shape[0]:
                        ds_name = gen_ds[g]
                        ds = status_group.create_dataset(
                            ds_name, data=result.gen_status[g],
                            chunks=True, compression="gzip",
                        )
                        ds.attrs["shape"] = f"[{num_nodes} nodes x {hours} hours]"
                        ds.attrs["units"] = "binary (0/1)"

            # --- Generator startup events (per generator) ---
            if result.gen_startup is not None:
                startup_group = scenario.create_group("gen_startup")
                startup_group.attrs["description"] = (
                    "Generator startup events [nodes x hours] for each generator"
                )
                for g, name in enumerate(self._gen_names):
                    if g < result.gen_startup.shape[0]:
                        ds_name = gen_ds[g]
                        ds = startup_group.create_dataset(
                            ds_name, data=result.gen_startup[g],
                            chunks=True, compression="gzip",
                        )
                        ds.attrs["shape"] = f"[{num_nodes} nodes x {hours} hours]"
                        ds.attrs["units"] = "binary (0/1)"

            # --- Curtailment per node (aggregated across generators) ---
            # Keep the node axis so the visualizer can attribute curtailment
            # to its system via subsystem_offsets when reading.
            if result.curtailment is not None:
                curt = result.curtailment
                if curt.ndim == 3:
                    curt_node = curt.sum(axis=0)        # [gen, node, hour] -> [node, hour]
                elif curt.ndim == 2:
                    curt_node = curt                    # already [node, hour]
                else:
                    curt_node = curt                    # legacy 1D scalar — write as-is
                ds = scenario.create_dataset(
                    "curtailment", data=curt_node,
                    chunks=True, compression="gzip",
                )
                ds.attrs["shape"] = f"[{num_nodes} nodes x {hours} hours]"
                ds.attrs["units"] = "MW"
                ds.attrs["description"] = (
                    "Total renewable curtailment aggregated across all generators"
                )

            # --- Battery charge/discharge (per battery) ---
            for bat_key, bat_data in [
                ("battery_charge", result.bat_charge),
                ("battery_discharge", result.bat_discharge),
            ]:
                if bat_data is not None:
                    bat_group = scenario.create_group(bat_key)
                    bat_group.attrs["description"] = (
                        f"{bat_key} [nodes x hours] for each battery"
                    )
                    for b, name in enumerate(self._bat_names):
                        if b < bat_data.shape[0]:
                            ds = bat_group.create_dataset(
                                bat_ds[b], data=bat_data[b],
                                chunks=True, compression="gzip",
                            )
                            ds.attrs["shape"] = f"[{num_nodes} nodes x {hours} hours]"
                            ds.attrs["units"] = "MW"

            # --- Battery SOC (per battery) ---
            if result.bat_soc is not None:
                soc_group = scenario.create_group("battery_soc")
                soc_group.attrs["description"] = (
                    "Battery state of charge [nodes x hours] for each battery"
                )
                for b, name in enumerate(self._bat_names):
                    if b < result.bat_soc.shape[0]:
                        ds = soc_group.create_dataset(
                            bat_ds[b], data=result.bat_soc[b],
                            chunks=True, compression="gzip",
                        )
                        ds.attrs["shape"] = f"[{num_nodes} nodes x {hours} hours]"
                        ds.attrs["units"] = "MWh"

            # --- Reserve data [node x hour] ---
            if result.reserve_static is not None:
                ds = scenario.create_dataset(
                    "reserve_static", data=result.reserve_static,
                    chunks=True, compression="gzip",
                )
                ds.attrs["units"] = "MW"
                ds.attrs["description"] = "Static reserve provision"

            if result.reserve_dynamic is not None:
                ds = scenario.create_dataset(
                    "reserve_dynamic", data=result.reserve_dynamic,
                    chunks=True, compression="gzip",
                )
                ds.attrs["units"] = "MW"
                ds.attrs["description"] = "Dynamic reserve provision"

            if result.loss_of_reserve_static is not None:
                ds = scenario.create_dataset(
                    "loss_of_reserve_static", data=result.loss_of_reserve_static,
                    chunks=True, compression="gzip",
                )
                ds.attrs["units"] = "MW"
                ds.attrs["description"] = "Unmet static reserve requirement"

            if result.loss_of_reserve_dynamic is not None:
                ds = scenario.create_dataset(
                    "loss_of_reserve_dynamic", data=result.loss_of_reserve_dynamic,
                    chunks=True, compression="gzip",
                )
                ds.attrs["units"] = "MW"
                ds.attrs["description"] = "Unmet dynamic reserve requirement"

            # --- Load shedding [node x hour] ---
            if result.load_shed_array is not None:
                ds = scenario.create_dataset(
                    "loss_load", data=result.load_shed_array,
                    chunks=True, compression="gzip",
                )
                ds.attrs["units"] = "MW"
                ds.attrs["description"] = "Loss of load (unserved energy)"

            # --- CO2 emissions [node x hour] ---
            if result.co2_emissions is not None:
                ds = scenario.create_dataset(
                    "CO2_emissions", data=result.co2_emissions,
                    chunks=True, compression="gzip",
                )
                # Rate per step, like the MW datasets: tonnes in a step
                # are value × temporal_resolution_hours.
                ds.attrs["units"] = "tCO2/h"
                ds.attrs["description"] = (
                    "CO2 emission rate per node and time step "
                    "(x temporal_resolution_hours = tonnes per step)"
                )

            # --- Voltage angle [node x hour] ---
            if result.voltage_angle is not None:
                ds = scenario.create_dataset(
                    "voltage_angle", data=result.voltage_angle,
                    chunks=True, compression="gzip",
                )
                ds.attrs["units"] = "radians"
                ds.attrs["description"] = "Voltage angle at each node"

            # --- Voltage magnitude [bus x hour] (ACOPF only) ---
            if result.voltage_magnitude is not None:
                ds = scenario.create_dataset(
                    "voltage_magnitude", data=result.voltage_magnitude,
                    chunks=True, compression="gzip",
                )
                ds.attrs["units"] = "p.u."
                ds.attrs["description"] = "Voltage magnitude (ACOPF formulations only)"

            # --- Reactive generation [gen x bus x hour] (ACOPF only) ---
            if result.reactive_generation is not None:
                ds = scenario.create_dataset(
                    "reactive_generation", data=result.reactive_generation,
                    chunks=True, compression="gzip",
                )
                ds.attrs["units"] = "MVAr"
                ds.attrs["description"] = "Reactive power dispatch (ACOPF only)"

            # --- Demand data ---
            if result.demand is not None:
                # result.demand is [step, node]; store [node, step] like
                # every other per-node dataset, whatever the shape (the
                # orientation used to depend on steps > nodes).
                demand_array = np.array(result.demand, dtype=np.float32)
                demand_data = demand_array.T if demand_array.ndim == 2 else demand_array
                ds = scenario.create_dataset(
                    "demand", data=demand_data,
                    chunks=True, compression="gzip",
                )
                ds.attrs["units"] = "MW"
                ds.attrs["dimensions"] = "nodes x hours"
                ds.attrs["description"] = "Total system demand"

            # --- Electricity prices ---
            if result.prices is not None:
                # Nodal prices [node x hour]
                ds = scenario.create_dataset(
                    "nodal_electricity_prices",
                    data=np.nan_to_num(result.prices, nan=0.0),
                    compression="gzip",
                )
                ds.attrs["dimensions"] = "nodes x hours"
                ds.attrs["units"] = "USD/MWh"
                ds.attrs["description"] = "Nodal electricity prices (LMPs)"

                # System-average prices [hour]
                avg_prices = result.prices.mean(axis=0)
                ds = scenario.create_dataset(
                    "electricity_prices", data=avg_prices,
                    compression="gzip",
                )
                ds.attrs["units"] = "USD/MWh"
                ds.attrs["description"] = "System-average electricity prices per timestep"

            # --- Power flow ---
            # The Julia LP keys `power_flow` by (from_bus, to_bus) at the
            # operational bus level, not by master-node pair. Aggregate
            # bus-pair flows up to (from_node, to_node) via bus_to_node;
            # drop intra-node bus lines.
            if result.power_flow and isinstance(result.power_flow, dict):
                flow_array = self._bus_pairs_to_node_flows(result.power_flow, num_nodes)
                max_hours = flow_array.shape[-1]
                if max_hours > 0:
                    ds = scenario.create_dataset(
                        "power_flow", data=flow_array,
                        chunks=True, compression="gzip",
                    )
                    ds.attrs["shape"] = (
                        f"[{num_nodes} from_nodes x {num_nodes} to_nodes x {max_hours} hours]"
                    )
                    ds.attrs["units"] = "MW"
                    ds.attrs["description"] = (
                        "Power flow between nodes (positive = from i to j); "
                        "bus-pair flows aggregated to node-pair via bus_to_node"
                    )

            # --- Transfer investment [from x to] ---
            if result.transfer_investment and isinstance(result.transfer_investment, dict):
                trans_inv_array = np.zeros((num_nodes, num_nodes))
                for (i, j), val in result.transfer_investment.items():
                    if i < num_nodes and j < num_nodes:
                        trans_inv_array[i, j] = val
                ds = scenario.create_dataset(
                    "transfer_investment", data=trans_inv_array,
                    chunks=True, compression="gzip",
                )
                ds.attrs["shape"] = f"[{num_nodes} from_nodes x {num_nodes} to_nodes]"
                ds.attrs["units"] = "MW"
                ds.attrs["description"] = "Transmission capacity investment"

            # --- Generator investment [gen x node] ---
            if result.gen_investment_array is not None:
                inv_group = scenario.create_group("gen_investment_power")
                inv_group.attrs["description"] = "Generator capacity investment [nodes] for each generator"
                inv_group.attrs["units"] = "MW"
                for g, name in enumerate(self._gen_names):
                    if g < result.gen_investment_array.shape[0]:
                        ds = inv_group.create_dataset(
                            gen_ds[g], data=result.gen_investment_array[g],
                            chunks=True, compression="gzip",
                        )
                        ds.attrs["shape"] = f"[{num_nodes} nodes]"

            # --- Battery investment [bat x node] ---
            if result.bat_investment_power is not None:
                inv_group = scenario.create_group("bat_investment_power")
                inv_group.attrs["description"] = "Battery power capacity investment [nodes] for each battery"
                inv_group.attrs["units"] = "MW"
                for b, name in enumerate(self._bat_names):
                    if b < result.bat_investment_power.shape[0]:
                        ds = inv_group.create_dataset(
                            bat_ds[b], data=result.bat_investment_power[b],
                            chunks=True, compression="gzip",
                        )
                        ds.attrs["shape"] = f"[{num_nodes} nodes]"

            if result.bat_investment_capacity is not None:
                inv_group = scenario.create_group("bat_investment_capacity")
                inv_group.attrs["description"] = "Battery energy capacity investment [nodes] for each battery"
                inv_group.attrs["units"] = "MWh"
                for b, name in enumerate(self._bat_names):
                    if b < result.bat_investment_capacity.shape[0]:
                        ds = inv_group.create_dataset(
                            bat_ds[b], data=result.bat_investment_capacity[b],
                            chunks=True, compression="gzip",
                        )
                        ds.attrs["shape"] = f"[{num_nodes} nodes]"

            # --- Capacity Factor (per generator) ---
            if result.capacity_factor is not None:
                cf_group = scenario.create_group("capacity_factor")
                cf_group.attrs["description"] = "Capacity factor [nodes x hours] for each generator"
                cf_group.attrs["units"] = "dimensionless (0-1)"
                for g, name in enumerate(self._gen_names):
                    if g < result.capacity_factor.shape[0]:
                        ds = cf_group.create_dataset(
                            gen_ds[g], data=result.capacity_factor[g],
                            chunks=True, compression="gzip",
                        )
                        ds.attrs["shape"] = f"[{num_nodes} nodes x {hours} hours]"

            # --- LCOE (per generator) ---
            if result.lcoe is not None:
                lcoe_group = scenario.create_group("lcoe")
                lcoe_group.attrs["description"] = "LCOE [nodes x hours] for each generator"
                lcoe_group.attrs["units"] = "USD/MWh"
                for g, name in enumerate(self._gen_names):
                    if g < result.lcoe.shape[0]:
                        ds = lcoe_group.create_dataset(
                            gen_ds[g], data=result.lcoe[g],
                            chunks=True, compression="gzip",
                        )
                        ds.attrs["shape"] = f"[{num_nodes} nodes x {hours} hours]"

            # --- VALLCOE (per generator) ---
            if result.vallcoe is not None:
                vallcoe_group = scenario.create_group("vallcoe")
                vallcoe_group.attrs["description"] = "Value-adjusted LCOE [nodes x hours] for each generator"
                vallcoe_group.attrs["units"] = "USD/MWh"
                for g, name in enumerate(self._gen_names):
                    if g < result.vallcoe.shape[0]:
                        ds = vallcoe_group.create_dataset(
                            gen_ds[g], data=result.vallcoe[g],
                            chunks=True, compression="gzip",
                        )
                        ds.attrs["shape"] = f"[{num_nodes} nodes x {hours} hours]"

            # --- Battery spillage (per battery) ---
            if result.bat_spillage is not None:
                spill_group = scenario.create_group("battery_spillage")
                spill_group.attrs["description"] = "Battery spillage [nodes x hours] for each battery"
                spill_group.attrs["units"] = "MW"
                for b, name in enumerate(self._bat_names):
                    if b < result.bat_spillage.shape[0]:
                        ds = spill_group.create_dataset(
                            bat_ds[b], data=result.bat_spillage[b],
                            chunks=True, compression="gzip",
                        )
                        ds.attrs["shape"] = f"[{num_nodes} nodes x {hours} hours]"

            # --- Battery capacity factor (per battery) ---
            if result.bat_capacity_factor is not None:
                bcf_group = scenario.create_group("battery_capacity_factor")
                bcf_group.attrs["description"] = "Battery capacity factor [nodes x hours]"
                bcf_group.attrs["units"] = "dimensionless (0-1)"
                for b, name in enumerate(self._bat_names):
                    if b < result.bat_capacity_factor.shape[0]:
                        ds = bcf_group.create_dataset(
                            bat_ds[b], data=result.bat_capacity_factor[b],
                            chunks=True, compression="gzip",
                        )
                        ds.attrs["shape"] = f"[{num_nodes} nodes x {hours} hours]"

            # --- Battery LCOE (per battery) ---
            if result.bat_lcoe is not None:
                blcoe_group = scenario.create_group("battery_lcoe")
                blcoe_group.attrs["description"] = "Battery LCOE [nodes x hours]"
                blcoe_group.attrs["units"] = "USD/MWh"
                for b, name in enumerate(self._bat_names):
                    if b < result.bat_lcoe.shape[0]:
                        ds = blcoe_group.create_dataset(
                            bat_ds[b], data=result.bat_lcoe[b],
                            chunks=True, compression="gzip",
                        )
                        ds.attrs["shape"] = f"[{num_nodes} nodes x {hours} hours]"

            # --- Battery VALLCOE (per battery) ---
            if result.bat_vallcoe is not None:
                bvallcoe_group = scenario.create_group("battery_vallcoe")
                bvallcoe_group.attrs["description"] = "Battery value-adjusted LCOE [nodes x hours]"
                bvallcoe_group.attrs["units"] = "USD/MWh"
                for b, name in enumerate(self._bat_names):
                    if b < result.bat_vallcoe.shape[0]:
                        ds = bvallcoe_group.create_dataset(
                            bat_ds[b], data=result.bat_vallcoe[b],
                            chunks=True, compression="gzip",
                        )
                        ds.attrs["shape"] = f"[{num_nodes} nodes x {hours} hours]"

            # --- EV variables [node x hour] ---
            if result.ev_charging is not None:
                ds = scenario.create_dataset(
                    "EV_charging", data=result.ev_charging,
                    chunks=True, compression="gzip",
                )
                ds.attrs["units"] = "MW"
                ds.attrs["description"] = "EV charging demand per node and hour"

            if result.ev_v2g is not None:
                ds = scenario.create_dataset(
                    "EV_V2G", data=result.ev_v2g,
                    chunks=True, compression="gzip",
                )
                ds.attrs["units"] = "MW"
                ds.attrs["description"] = "Vehicle-to-grid power per node and hour"

            if result.ev_soc is not None:
                ds = scenario.create_dataset(
                    "EV_soc", data=result.ev_soc,
                    chunks=True, compression="gzip",
                )
                ds.attrs["units"] = "MWh"
                ds.attrs["description"] = "EV fleet state of charge per node and hour"

            if result.ev_loss is not None:
                ds = scenario.create_dataset(
                    "EV_loss", data=result.ev_loss,
                    chunks=True, compression="gzip",
                )
                ds.attrs["units"] = "MW"
                ds.attrs["description"] = "EV demand not met per node and hour"

            # --- Rooftop solar (behind-the-meter, already netted out of demand) ---
            if result.rooftop_generation is not None:
                ds = scenario.create_dataset(
                    "rooftop_generation", data=result.rooftop_generation,
                    chunks=True, compression="gzip",
                )
                ds.attrs["units"] = "MW"
                ds.attrs["description"] = (
                    "Behind-the-meter rooftop solar generation [hour x node]; "
                    "subtracted from gross demand before the LP — diagnostic only"
                )

            # --- Loss of inertia [hour] ---
            if result.loss_of_inertia is not None:
                ds = scenario.create_dataset(
                    "loss_of_inertia", data=result.loss_of_inertia,
                    chunks=True, compression="gzip",
                )
                ds.attrs["units"] = "GW*s"
                ds.attrs["description"] = "System-wide inertia deficit per hour"

            # --- Transfer margin [from_node x to_node x hour] ---
            # Keyed by bus pair like power_flow; summed to node pairs.
            if result.transfer_margin and isinstance(result.transfer_margin, dict):
                tm_array = self._bus_pairs_to_node_flows(result.transfer_margin, num_nodes)
                max_hours_tm = tm_array.shape[-1]
                if max_hours_tm > 0:
                    ds = scenario.create_dataset(
                        "transfer_margin", data=tm_array,
                        chunks=True, compression="gzip",
                    )
                    ds.attrs["shape"] = f"[{num_nodes} x {num_nodes} x {max_hours_tm} hours]"
                    ds.attrs["units"] = "MW"
                    ds.attrs["description"] = "Transfer margin violation between nodes"

            # --- Price decomposition ---
            if result.price_energy_component is not None:
                ds = scenario.create_dataset(
                    "electricity_prices_energy",
                    data=result.price_energy_component,
                    compression="gzip",
                )
                ds.attrs["units"] = "USD/MWh"
                ds.attrs["description"] = "System energy price component (lambda)"

            if result.price_congestion_component is not None:
                ds = scenario.create_dataset(
                    "nodal_electricity_prices_congestion",
                    data=result.price_congestion_component,
                    compression="gzip",
                )
                ds.attrs["dimensions"] = "nodes x hours"
                ds.attrs["units"] = "USD/MWh"
                ds.attrs["description"] = "Congestion price component (mu) per node"

            # --- Technology selling prices ---
            if result.technology_selling_prices:
                tsp_group = scenario.create_group("technology_selling_prices")
                tsp_group.attrs["description"] = "Revenue-weighted price data per technology"
                for tech_name, data in result.technology_selling_prices.items():
                    tech_group = tsp_group.create_group(self._h5safe(tech_name))
                    tech_group.attrs["total_generation"] = data["total_generation"]
                    tech_group.attrs["total_revenue"] = data["total_revenue"]
                    tech_group.attrs["average_selling_price"] = data["average_selling_price"]
                    tech_group.attrs["technology_type"] = data["technology_type"]
                    if data["prices_weights"] is not None and len(data["prices_weights"]) > 0:
                        ds = tech_group.create_dataset(
                            "prices_weights", data=data["prices_weights"],
                            chunks=True, compression="gzip",
                        )
                        ds.attrs["columns"] = "price_USD_MWh, generation_MW, timestep"

            # --- Reservoir hydro data (per generator) ---
            if result.reservoir_level is not None:
                res_group = scenario.create_group("reservoir_level")
                res_group.attrs["description"] = (
                    "Reservoir water level at the start of each hour [nodes x hours] per gen")
                for g, name in enumerate(self._gen_names):
                    if g < result.reservoir_level.shape[0]:
                        ds = res_group.create_dataset(
                            gen_ds[g], data=result.reservoir_level[g],
                            chunks=True, compression="gzip",
                        )
                        ds.attrs["units"] = "MWh-eq"

            if result.reservoir_spillage is not None:
                spill_group = scenario.create_group("reservoir_spillage")
                spill_group.attrs["description"] = "Reservoir spillage [nodes x hours] per gen"
                for g, name in enumerate(self._gen_names):
                    if g < result.reservoir_spillage.shape[0]:
                        ds = spill_group.create_dataset(
                            gen_ds[g], data=result.reservoir_spillage[g],
                            chunks=True, compression="gzip",
                        )
                        ds.attrs["units"] = "MW-eq"

            if result.reservoir_invest_capacity is not None:
                rinv_group = scenario.create_group("reservoir_invest_capacity")
                rinv_group.attrs["description"] = "Reservoir capacity investment [nodes] per gen"
                for g, name in enumerate(self._gen_names):
                    if g < result.reservoir_invest_capacity.shape[0]:
                        ds = rinv_group.create_dataset(
                            gen_ds[g], data=result.reservoir_invest_capacity[g],
                        )
                        ds.attrs["units"] = "MWh"

            if result.reservoir_pump is not None:
                pump_group = scenario.create_group("reservoir_pump")
                pump_group.attrs["description"] = "Reservoir pump power [nodes x hours] per gen"
                for g, name in enumerate(self._gen_names):
                    if g < result.reservoir_pump.shape[0]:
                        ds = pump_group.create_dataset(
                            gen_ds[g], data=result.reservoir_pump[g],
                            chunks=True, compression="gzip",
                        )
                        ds.attrs["units"] = "MW"

            # --- Electrolyzers (per electrolyzer, config order) ---
            if result.electrolyzer_power is not None:
                elz_names = self._h5_unit_names(
                    [getattr(e, "name", None) or key for key, e in
                     (self.primary_system.electrolyzers or {}).items()])
                for group_name, data, units, desc in (
                    ("electrolyzer_power", result.electrolyzer_power, "MW",
                     "Electrolyzer power drawn [nodes x hours] per electrolyzer"),
                    ("electrolyzer_h2", result.electrolyzer_h2, "kg/h",
                     "Hydrogen produced [nodes x hours] per electrolyzer"),
                ):
                    if data is None:
                        continue
                    elz_group = scenario.create_group(group_name)
                    elz_group.attrs["description"] = desc
                    for e, name in enumerate(elz_names):
                        if e < data.shape[0]:
                            ds = elz_group.create_dataset(
                                name, data=data[e],
                                chunks=True, compression="gzip",
                            )
                            ds.attrs["units"] = units

            # --- Investments (scalar attrs) ---
            # Keyed by node: a bus-level master's buses are summed per node
            if result.investments:
                node_investments = self._master_investments_by_node({
                    key: float(value) if not isinstance(value, (list, np.ndarray))
                    else float(np.sum(value))
                    for key, value in result.investments.items()})
                for key, val in node_investments.items():
                    scenario.attrs[f"investment_{key}"] = float(val)

            # --- Retirements (scalar attrs) ---
            if result.retirements:
                for key, value in result.retirements.items():
                    val = float(value) if not isinstance(value, (list, np.ndarray)) else float(np.sum(value))
                    scenario.attrs[f"retirement_{key}"] = val

            # --- Primary Energy ---
            if result.primary_energy:
                pe_grp = scenario.create_group("primary_energy")
                pe = result.primary_energy

                for section in ("total_fuel_supply", "total_ne_demand_satisfied",
                                "total_loss_of_supply", "final_storage_levels"):
                    if section in pe and pe[section]:
                        sec_grp = pe_grp.create_group(section)
                        for fuel_name, arr in pe[section].items():
                            sec_grp.create_dataset(
                                fuel_name, data=np.array(arr, dtype=np.float64))

                for cost_key in ("total_fuel_cost", "total_transport_cost", "total_loss_penalty"):
                    if cost_key in pe:
                        pe_grp.attrs[cost_key] = float(pe[cost_key])

                for inv_key in ("transport_investments", "storage_investments"):
                    if inv_key in pe and pe[inv_key]:
                        inv_grp = pe_grp.create_group(inv_key)
                        for fuel_name, arr in pe[inv_key].items():
                            inv_grp.create_dataset(
                                fuel_name, data=np.array(arr, dtype=np.float64))

                # Transport flows: {fuel: [routes × periods]}
                if "transport_flows" in pe and pe["transport_flows"]:
                    tf_grp = pe_grp.create_group("transport_flows")
                    for fuel_name, arr in pe["transport_flows"].items():
                        tf_grp.create_dataset(
                            fuel_name, data=np.array(arr, dtype=np.float64))

                # Fuel burnt by each coupled unit: {unit: [nodes × steps]},
                # under the unit's dataset name of the per-generator groups
                if result.fuel_for_power:
                    ffp_grp = pe_grp.create_group("fuel_for_power")
                    ffp_grp.attrs["units"] = "fuel units per step"
                    ds_of = dict(zip(unique_unit_names(self._gen_names), gen_ds))
                    units = list(result.fuel_for_power)
                    names = unique_unit_names([ds_of.get(str(u)) or self._h5safe(str(u))
                                               for u in units])
                    for unit, name in zip(units, names):
                        ffp_grp.create_dataset(
                            name, data=np.array(result.fuel_for_power[unit], dtype=np.float64),
                            chunks=True, compression="gzip")
            # --- Fuel infrastructure and electrolyzers the master built (#89) ---
            # In service this year, on top of the configured capacity
            fuel_caps = (getattr(self, "_master_fuel_by_year", None) or {}).get(result.year) or {}
            if any(fuel_caps.get(k) for k in ("storage", "transport", "electrolyzer")):
                mf_grp = scenario.create_group("master_fuel_capacity")
                n_routes = len(self.primary_system.fuel_transport_routes or [])
                for kind, size in (("storage", self._num_nodes), ("transport", n_routes)):
                    if not fuel_caps.get(kind):
                        continue
                    kgrp = mf_grp.create_group(kind)
                    fuels = list(fuel_caps[kind])
                    for fuel_name, ds_name in zip(fuels, self._h5_unit_names(fuels)):
                        by_idx = fuel_caps[kind][fuel_name]
                        arr = np.zeros(max([size] + [i + 1 for i in by_idx]))
                        for i, v in by_idx.items():
                            arr[i] = v
                        kgrp.create_dataset(ds_name, data=arr)
                if fuel_caps.get("electrolyzer"):
                    egrp = mf_grp.create_group("electrolyzer")
                    keys = list(fuel_caps["electrolyzer"])
                    for key, ds_name in zip(keys, self._h5_unit_names(keys)):
                        egrp.create_dataset(ds_name, data=np.array(
                            fuel_caps["electrolyzer"][key], dtype=np.float64))

            # --- N-1 Security Results ---
            if (result.n1_gen_reserve_duals is not None
                    or result.n1_binding_contingencies
                    or result.n1_security_cost > 0):
                n1_group = scenario.create_group("n1_security")
                n1_group.attrs["security_cost"] = result.n1_security_cost

                if result.n1_gen_reserve_duals is not None:
                    ds = n1_group.create_dataset(
                        "gen_reserve_duals", data=result.n1_gen_reserve_duals,
                        chunks=True, compression="gzip",
                    )
                    ds.attrs["units"] = "USD/MW"
                    ds.attrs["description"] = "Dual of generation N-1 reserve constraint per hour"

                if result.n1_trans_reserve_duals and isinstance(result.n1_trans_reserve_duals, dict):
                    trans_grp = n1_group.create_group("trans_reserve_duals")
                    trans_grp.attrs["description"] = "Duals of SCOPF transmission contingency constraints"
                    for key, arr in result.n1_trans_reserve_duals.items():
                        key_str = f"line_{key[0]}_outage_{key[1]}_dir_{key[2]}"
                        ds = trans_grp.create_dataset(
                            key_str, data=np.asarray(arr),
                            chunks=True, compression="gzip",
                        )
                        ds.attrs["units"] = "USD/MW"

                if result.n1_binding_contingencies:
                    # Store as variable-length string dataset
                    binding_arr = np.array(result.n1_binding_contingencies, dtype="S")
                    n1_group.create_dataset("binding_contingencies", data=binding_arr)

            # --- Cost Breakdown (granular cost decomposition from optimizer) ---
            if result.cost_breakdown:
                if "cost_breakdown" not in f:
                    f.create_group("cost_breakdown")
                cbd_grp = f["cost_breakdown"]
                year_key = f"year_{result.year}"
                if year_key in cbd_grp:
                    del cbd_grp[year_key]
                yk = cbd_grp.create_group(year_key)
                for cost_name, cost_val in result.cost_breakdown.items():
                    yk.attrs[cost_name] = float(cost_val)

            # ==============================================================
            # STORE DEMAND DATA IN /demand GROUP
            # ==============================================================
            demand_group = f["demand"]

            if result.demand is not None:
                year_demand_name = f"year_{result.year}_base_demand"
                if year_demand_name in demand_group:
                    del demand_group[year_demand_name]

                demand_array = np.array(result.demand, dtype=np.float32)
                ds = demand_group.create_dataset(
                    year_demand_name, data=demand_array,
                    chunks=True, compression="gzip",
                )
                ds.attrs["description"] = (
                    f"Base demand for year {result.year}"
                )
                ds.attrs["units"] = "MW"

            # Update timestamp
            f.attrs["last_update"] = datetime.now().isoformat()

        logger.debug(f"Saved year {result.year} to HDF5 (legacy)")

        # Also write per-system structured data
        self._append_year_per_system(path, result)

    def _append_year_per_system(self, path: Path, result):
        """Write inter-system flow aggregates and global summary into the
        HDF5. Per-system mirrors of detailed_results / summary_results are
        no longer written: they were pure slices of the global block (see
        ``_open_scenario`` in the visualizer)."""
        import h5py

        mapping = self._build_system_unit_mapping()
        if not mapping:
            return

        with h5py.File(path, "a") as f:
            scenario_name = f"year_{result.year}_threshold_0"

            # Per-system detailed_results, summary_results and
            # system_configuration blocks are no longer mirrored: they
            # are derived on read from the root block (see
            # results_charts._open_scenario / _open_system_config).

            # ============================================================
            # INTER-SYSTEM POWER FLOWS
            # ============================================================
            if (result.power_flow and isinstance(result.power_flow, dict)
                    and len(mapping) > 1):
                inter_det = f["inter_system/detailed_results"]
                if scenario_name in inter_det:
                    del inter_det[scenario_name]
                inter_sc = inter_det.create_group(scenario_name)
                inter_sc.attrs["year"] = result.year

                pf_grp = inter_sc.create_group("power_flow")
                max_h = max((len(v) for v in result.power_flow.values()), default=0)

                # Build node→system lookup
                node_to_sys: dict[int, str] = {}
                for sname, smap in mapping.items():
                    for n in range(smap["node_offset"], smap["node_offset"] + smap["node_count"]):
                        node_to_sys[n] = sname

                # Aggregate flows between system pairs. power_flow is keyed
                # by bus pair: map each end to its node, then its system.
                inter_flows: dict[tuple[str, str], np.ndarray] = {}
                for (i, j), flow in result.power_flow.items():
                    nodes = self._bus_pair_nodes(i, j)
                    if nodes is None:
                        continue
                    sys_i = node_to_sys.get(nodes[0])
                    sys_j = node_to_sys.get(nodes[1])
                    if sys_i and sys_j and sys_i != sys_j:
                        pair = (sys_i, sys_j)
                        if pair not in inter_flows:
                            inter_flows[pair] = np.zeros(max_h)
                        inter_flows[pair][:len(flow)] += flow

                for (sa, sb), flow_arr in inter_flows.items():
                    ds_name = f"{sa}_to_{sb}"
                    ds = pf_grp.create_dataset(ds_name, data=flow_arr,
                                               chunks=True, compression="gzip")
                    ds.attrs["units"] = "MW"
                    ds.attrs["description"] = f"Net power flow from {sa} to {sb}"

            # ============================================================
            # GLOBAL SUMMARY & COST BREAKDOWN & DEMAND
            # ============================================================
            global_sum = f["global/summary_results"]
            global_summary_data = {
                "year": result.year,
                "total_cost": result.objective,
                "renewable_penetration": result.re_penetration,
                "co2_emissions": result.emissions,
                "loss_of_load": result.load_shed,
                "total_generation": result.total_generation,
                "total_demand": result.total_demand,
            }
            for col, value in global_summary_data.items():
                if col in global_sum:
                    dataset = global_sum[col]
                    old_size = dataset.shape[0]
                    dataset.resize((old_size + 1,))
                    dataset[old_size] = value
                else:
                    global_sum.create_dataset(
                        col, data=[value], maxshape=(None,), chunks=True)

            # Global cost breakdown
            if result.cost_breakdown:
                global_cbd = f["global/cost_breakdown"]
                year_key = f"year_{result.year}"
                if year_key in global_cbd:
                    del global_cbd[year_key]
                yk = global_cbd.create_group(year_key)
                for cost_name, cost_val in result.cost_breakdown.items():
                    yk.attrs[cost_name] = float(cost_val)

            # Global demand
            if result.demand is not None:
                global_dem = f["global/demand"]
                dem_key = f"year_{result.year}_base_demand"
                if dem_key in global_dem:
                    del global_dem[dem_key]
                global_dem.create_dataset(
                    dem_key, data=np.array(result.demand, dtype=np.float32),
                    chunks=True, compression="gzip")

        logger.debug(f"Saved year {result.year} per-system data to HDF5")

    def _finalize_hdf5(self, path: Path):
        """Finalize HDF5 output file (matching legacy structure)."""
        import h5py

        with h5py.File(path, "a") as f:
            f.attrs["export_complete"] = True
            f.attrs["export_timestamp"] = datetime.now().isoformat()

        logger.debug(f"Finalized HDF5: {path}")
