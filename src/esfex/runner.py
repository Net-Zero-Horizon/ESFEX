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
import hashlib
import logging
import math
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

from esfex.config.schema import (
    CostCurveConfig,
    ESFEXConfig,
    SystemConfig,
    fuel_price_factor,
    is_committed_unit,
)
from esfex.bridge.adapters import (
    LOSS_REPAIR_GAP_INFO,
    MasterProblemAdapter,
    PowerSystemAdapter,
    PrimaryEnergyAdapter,
    reset_run_warnings,
)
from esfex.bridge.converters import (
    OPERATING_BUDGET_COSTS,
    battery_availability_key,
    battery_technology_availability_key,
)
from esfex.models.ev import (
    aggregate_ev_profiles,
    fleet_growth_factor,
    generate_ev_profiles,
    generate_v2g_availability,
)
from esfex.api._common import ApiError
from esfex.io.checkpoint import PHASE_COMPLETE, PHASE_DISPATCH, PHASE_MASTER, ResumeError
from esfex.io.demand import _read_tabular, create_sectoral_demand, read_hourly_table
from esfex.utils import aggregate_demand_to_resolution
from esfex.utils.per_unit import derived_reactance_pu, line_reactance_pu, system_s_base
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
    R and X are those of each whole line (all its circuits), as in the
    dispatch network.
    """
    by_pair: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for line in lines:
        a = buses[line.from_bus].parent_node if line.from_bus is not None and line.from_bus < len(buses) else line.from_node
        b = buses[line.to_bus].parent_node if line.to_bus is not None and line.to_bus < len(buses) else line.to_node
        if a == b:
            continue
        x = line_reactance_pu(line, line.length_km, dc_config, s_base_mva)
        by_pair.setdefault((min(a, b), max(a, b)), []).append(
            (line.resistance_pu or 0.0, x))
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


def _merged_operating_budget(systems) -> Optional[float]:
    """The operating budget of systems merged into one dispatch: the sum of
    theirs when every system sets ``max_annual_operating_cost``. The merged
    dispatch has one budget over all their spending, so when only some set
    one, none is applied (with a warning)."""
    budgets = [s.max_annual_operating_cost for s in systems]
    if all(b is None for b in budgets):
        return None
    if any(b is None for b in budgets):
        logger.warning(
            "max_annual_operating_cost is set for %s but not for %s; the merged "
            "dispatch has one operating budget over every system, so none is "
            "applied. Set it for every system (or none).",
            ", ".join(s.name for s, b in zip(systems, budgets) if b is not None),
            ", ".join(s.name for s, b in zip(systems, budgets) if b is None))
        return None
    return float(sum(budgets))


def _merged_budget_scope(systems) -> str:
    """The merged master's ``budget_scope``: the first system's (a warning
    when the systems differ; the merged master has one budget)."""
    scopes = [s.budget_scope for s in systems]
    if len(set(scopes)) > 1:
        logger.warning(
            "The systems set different budget_scope values (%s); the merged "
            "master has one budget and uses %r.",
            ", ".join(f"{s.name}: {s.budget_scope}" for s in systems), scopes[0])
    return scopes[0]


def operating_spend(breakdown: Optional[dict]) -> float:
    """What a dispatch window spent for the operating budget
    (``max_annual_operating_cost``), from its cost breakdown: the money it
    paid (fuel, maintenance, start-ups, fixed O&M, electrolyzer, converter
    and line-flow costs, primary-energy fuel bought and transported; the
    Julia ``OPERATING_BUDGET_COSTS``), no modelling penalty."""
    if not breakdown:
        return 0.0
    return float(sum(breakdown.get(k, 0.0) for k in OPERATING_BUDGET_COSTS))


def window_operating_budget(budget_left: float, window_demand: float,
                            remaining_demand: float) -> float:
    """A rolling window's share of the year's operating budget: what is left
    of it × the demand of the window's committed steps over the demand of the
    rest of the year (from the window's start), so a window that spends less
    leaves more to the next ones. Never negative."""
    left = max(budget_left, 0.0)
    if remaining_demand <= 0:
        return left
    return left * min(1.0, max(window_demand, 0.0) / remaining_demand)


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


def _stamp_power_in_service(ds, in_service: list, i: int) -> None:
    """Record on results dataset ``ds`` the MW in service per node of unit
    ``i`` (``in_service[i]``, see ``Orchestrator._rebuild_unit_names``): the
    readers charge fixed O&M on it and count it as installed capacity (a
    unit the master built has no config in the results file)."""
    if i < len(in_service) and in_service[i] is not None:
        ds.attrs["rated_power"] = np.asarray(in_service[i], dtype=float)


def _stamp_num_units(ds, num_units: list, i: int) -> None:
    """Record on a ``gen_status`` / ``gen_startup`` dataset the units per
    node of generator ``i`` (``num_units[i]``, see
    ``Orchestrator._rebuild_unit_names``): the readers show the units online
    against it and charge each unit start ``start_up_cost / num_units``."""
    if i < len(num_units) and num_units[i] is not None:
        ds.attrs["num_units"] = np.asarray(num_units[i], dtype=np.int64)


def existing_power_in_service(unit: dict, power_key: str, num_nodes: int,
                              year_offset: int, degrade: bool = True) -> list[float]:
    """Per-node MW in service of an existing unit in the dispatch's year, as
    the dispatch charges its fixed O&M (``fixed_om_annual_cost`` in
    power_system.jl): ``unit[power_key]``, nothing at a node past its
    ``life_time`` (age = ``initial_age`` + *year_offset*) and, with
    *degrade* (generators), shrunk by ``(1 - degradation_rate)^age``.

    *unit* is the dispatch's config of the unit: after a master run it holds
    the master's capacity (retirements and life extension applied) with no
    degradation and no end of life, so it is taken as is.
    """
    power = _per_node_list(unit.get(power_key) or [], num_nodes)
    age0 = _per_node_list(unit.get("initial_age") or [], num_nodes, 0)
    life = _per_node_list(unit.get("life_time") or [], num_nodes, float("inf"))
    deg = _per_node_list(unit.get("degradation_rate") or [], num_nodes, 0.0)
    out = []
    for p, a0, lt, d in zip(power, age0, life, deg):
        p, age = float(p), float(a0) + year_offset
        if p > 0 and float(lt) - age <= 0:
            p = 0.0
        elif p > 0 and degrade and float(d) > 0:
            p *= (1.0 - float(d)) ** age
        out.append(p)
    return out


# Operating-cost fields of the candidate technologies written to the results
# file (system_configuration/technologies, /battery_technologies)
_TECH_COST_ATTRS = ("fuel_cost", "fixed_cost", "maintenance_cost", "start_up_cost")
_BAT_TECH_COST_ATTRS = ("maintenance_cost", "fixed_cost")


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


class RunAbortedError(RuntimeError):
    """A run stopped before it produced its results (e.g. the master problem
    found no plan): ``esfex run`` reports it and exits with a non-zero code."""


@dataclass
class MasterDayPlan:
    """The days every master of a run models, chosen once
    (``Orchestrator._master_day_plan``)."""

    # Representative days (first steps, 0-based, within a year); None with TSAM
    rep_day_offsets: Optional[list[int]]
    # Critical days (lowest renewable supply against demand), the most
    # critical first
    critical_day_offsets: list[int]
    # Days of the year each representative day and then each critical day
    # stands for; empty = equal shares of the representative days (or TSAM)
    rep_day_weights: list[float]
    # Hours a battery must sustain to count as firm in the adequacy constraint
    storage_firm_hours: float
    # TSAM periods with the critical days appended; None without TSAM
    tsam: Any = None


def _day_plan_digest(plan: MasterDayPlan) -> dict:
    """The days a :class:`MasterDayPlan` models, as plain data: a resumed
    myopic run checks that it plans on the interrupted run's days."""
    tsam = plan.tsam
    return {
        "rep_day_offsets": None if plan.rep_day_offsets is None else [int(d) for d in plan.rep_day_offsets],
        "critical_day_offsets": [int(d) for d in plan.critical_day_offsets],
        "rep_day_weights": [float(w) for w in plan.rep_day_weights],
        "storage_firm_hours": float(plan.storage_firm_hours),
        "tsam_period_start_hours": (None if tsam is None else
                                    [int(h) for h in tsam.period_start_hours]),
        "tsam_period_weights": (None if tsam is None else
                                [float(w) for w in tsam.period_weights]),
    }


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
    # Line losses [node x hour] (MW): half of each line's loss at each end
    line_losses: Optional[np.ndarray] = None
    voltage_angle: Optional[np.ndarray] = None     # [node x hour]
    # AC-specific outputs — populated only when power_flow_mode is acopf_*.
    voltage_magnitude: Optional[np.ndarray] = None        # [bus x hour] in p.u.
    reactive_generation: Optional[np.ndarray] = None      # [gen x bus x hour] in MVAr
    # Prices [node x hour]
    prices: Optional[np.ndarray] = None
    # Demand [hour x node]
    demand: Optional[np.ndarray] = None
    # Investment/retirement decisions
    investments: dict[str, float] = field(default_factory=dict)
    retirements: dict[str, float] = field(default_factory=dict)
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
    re_penetration: float = 0.0   # RE share: renewable over total generation, energy-weighted
    load_shed: float = 0.0
    total_generation: float = 0.0
    total_demand: float = 0.0
    transmission_losses: float = 0.0  # MWh lost on the lines (DC loss model)
    master_re_target: float = 0.0  # Operational RE target: the master trajectory capped at what the plan can deliver
    # Line-loss repair bounds ($): of the master solve that planned this
    # year, and of the year's dispatch windows summed (adapters.loss_repair_bounds)
    master_loss_repair: Optional[dict] = None
    dispatch_loss_repair: Optional[dict] = None
    # Granular cost decomposition (accumulated from operational windows)
    cost_breakdown: Optional[dict] = None



def re_target_trajectory(initial: float, target: float, num_years: int,
                         min_inc: float, max_inc: float) -> list[float]:
    """Yearly RE targets of the planning horizon (RE-1, as the Julia
    ``calculate_target_ratios``): the initial share in year 1, then a
    linear path to ``target`` whose yearly step is clamped to
    ``[min_inc, max_inc]``, never above 1 (100 %)."""
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
        path.append(min(tau, 1.0))
    return path


def re_share(renewable_generation: float, total_generation: float) -> float:
    """The RE share: renewable over total generation (0 without
    generation), energy-weighted over whatever the two sums cover (a window,
    a year, several years). Renewable means a unit of type "Renewable"
    (Julia ``is_renewable_unit``); battery discharge is neither. 100 % means
    no fossil generation."""
    total = float(total_generation)
    return float(renewable_generation) / total if total > 0 else 0.0


def sum_loss_repairs(windows: list[dict]) -> Optional[dict]:
    """A year's dispatch line-loss repair from its windows' bounds
    (``adapters.loss_repair_bounds``): ``lower_bound`` and ``objective``
    summed ($, whole windows, look-ahead included), ``gap`` of the sums,
    ``gap_max`` (the largest window gap), ``rounds``, ``linearised`` and
    ``released`` summed, ``windows`` and ``windows_repaired`` (with at
    least one repair round). ``None`` without any window bounds."""
    if not windows:
        return None
    lower = sum(w["lower_bound"] for w in windows)
    final = sum(w["objective"] for w in windows)
    return {
        "lower_bound": lower,
        "objective": final,
        "gap": 0.0 if final == lower else (final - lower) / max(abs(final), 1e-12),
        "gap_max": max(w["gap"] for w in windows),
        "rounds": sum(w["rounds"] for w in windows),
        "linearised": sum(w["linearised"] for w in windows),
        "released": sum(w["released"] for w in windows),
        "windows": len(windows),
        "windows_repaired": sum(1 for w in windows if w["rounds"] > 0),
    }


def myopic_re_anchor(initial: float, min_inc: float, y_idx: int) -> float:
    """Year-0 anchor of the RE minimum-increment floor (RE-3) in myopic
    year *y_idx* (0-based): where the perfect-foresight chain stands the
    year before, the initial share plus *y_idx* minimum steps, at most 1
    (the Julia ``re_increment_floor``). The share reached in earlier years
    does not move it, so each one-year master gets the floor perfect
    foresight places on that year. (The maximum increment is measured from
    the share the previous year achieved: ``re_share_anchor``.)"""
    start = min(max(float(initial), 0.0), 1.0)
    return min(start + y_idx * max(float(min_inc), 0.0), 1.0)


# Economic lifetime of a build without a positive one (the Julia
# DEFAULT_ASSET_LIFETIME)
_DEFAULT_ASSET_LIFETIME = 40.0


def earlier_build_annuities(streams: Iterable[tuple], y_idx: int) -> float:
    """Investment annuities ($) myopic year *y_idx* (0-based) pays on the
    capacity earlier myopic years built. *streams* holds ``(build year
    index, life, annuity per year)`` with 0-based build years; a build pays
    from its build year while its age is below its life, as the Julia
    ``annuity_years`` (``_DEFAULT_ASSET_LIFETIME`` when not positive). A
    one-year master charges only the annuities of what that year builds, so
    these are what its objective leaves out next to a perfect-foresight
    year."""
    total = 0.0
    for build, life, annuity in streams:
        span = math.ceil(life if life > 0 else _DEFAULT_ASSET_LIFETIME)
        if build < y_idx <= build + span - 1:
            total += annuity
    return total


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


# Master builds (MW, MWh) at or below this are solver noise, not builds: the
# master's CAPACITY_TOL (master_problem.jl), which already reports them as 0.
# An interior-point solve without crossover leaves values like 1e-6 MW on
# capacity it did not build; carried into a later myopic year or the dispatch
# they made rows like `gen <= 2e-9` (round 36). Anything larger is kept in
# full: the master paid for it, so the dispatch and the later years' fleet
# see the same capacity (a battery's power and energy always together).
MIN_BUILD = 1e-3


def remaining_limit(limit: float, used: float) -> float:
    """Room left under an investment limit after *used* was built: 0 when
    at most :data:`MIN_BUILD` is left (a master that built up to the limit
    leaves e.g. 3e-7 MW, which the next myopic year must not see as room)."""
    left = float(limit) - float(used)
    return left if left > MIN_BUILD else 0.0

_MASTER_BUILD_CATEGORIES = (
    'tech_investment', 'bat_tech_power_investment', 'bat_tech_capacity_investment',
    'transfer_investment', 'converter_investment', 'reservoir_investment')


def master_year_builds(year_data: dict) -> dict[str, float]:
    """One year's master investments ``{key: MW or MWh}`` from the solution's
    per-year categories, every build above :data:`MIN_BUILD`."""
    return {key: float(value)
            for category in _MASTER_BUILD_CATEGORIES
            for key, value in (year_data.get(category) or {}).items()
            if value > MIN_BUILD}


def units_of_capacity(power_mw: float, unit_size_mw: float) -> int:
    """Units of *unit_size_mw* MW that *power_mw* MW of a built technology
    reaches unit commitment as: ``ceil(power / unit_size)`` (no unit larger
    than the declared size), and one unit when the technology has no unit
    size or nothing is built."""
    if unit_size_mw <= 0 or power_mw <= 0:
        return 1
    return max(1, math.ceil(power_mw / unit_size_mw - 1e-6))


def units_in_service(num_units: int, rated_mw: float, power_mw: float,
                     degradation_rate: float = 0.0, age: float = 0.0) -> int:
    """Units of an existing plant of *num_units* units and *rated_mw* MW that
    are left when the master keeps *power_mw* MW of it.

    Degradation shrinks every unit, so a capacity at or above the degraded
    rating ``rated × (1 − degradation)^age`` keeps all units. Less (a partial
    life extension) keeps ``ceil(num_units × power / degraded rating)``
    units: whole units of the original size, the last one possibly smaller.
    """
    n = max(1, int(num_units))
    if n == 1 or rated_mw <= 0:
        return n
    ref = rated_mw * (1.0 - degradation_rate) ** max(age, 0.0)
    if ref <= 0 or power_mw >= ref * (1 - 1e-6):
        return n
    return min(n, max(1, math.ceil(n * power_mw / ref - 1e-6)))


def rated_bat_tech_power(builds, y_idx: int, lifetimes: list[float]) -> dict[int, np.ndarray]:
    """Rated (not degraded) MW of each battery technology in service in
    master year *y_idx*: the sum of its builds of the years ``y ≤ y_idx``
    still within the technology's lifetime (``y_idx − y < lifetime``).

    *builds* maps a build year to ``{technology index (1-based): MW per
    master bus}``, as the master's ``bat_tech_power_investment``;
    *lifetimes* lists the technologies' lifetimes in that order. The
    master's ``cumulative_bat_tech_power`` holds the same builds degraded
    by age; the fixed O&M stays on this rated power (round 12).
    """
    out: dict[int, np.ndarray] = {}
    for y, by_tech in builds.items():
        y = int(y)
        if y > y_idx:
            continue
        for bt, mw in by_tech.items():
            bt = int(bt)
            if not 1 <= bt <= len(lifetimes) or y_idx - y >= lifetimes[bt - 1]:
                continue
            mw = np.asarray(mw, dtype=float)
            out[bt] = out[bt] + mw if bt in out else mw.copy()
    return out


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
        "ramp_up": rep(bat_tech.ramp_up, 0.0),
        "ramp_down": rep(bat_tech.ramp_down, 0.0),
        "fixed_cost": rep(bat_tech.fixed_cost, 0.0),
        "reservable": bat_tech.reservable,
        "availability_file": bat_tech.availability_file,
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


def _node_values_on_buses(arr, num_buses: int, bus_to_node, default=0.0) -> list:
    """A candidate technology's per-node list laid out on the *num_buses*
    buses of a unit built from it: each bus gets its parent node's value
    (``bus_to_node[b]``; *default* past the list). Without a node-to-bus
    mapping the buses are the nodes, in order. A single value is broadcast.

    The list is per node whatever its length: a merged system pads it to
    every system's nodes (zero at the other systems'), and a network can
    have fewer buses than nodes, so a list at least as long as the bus list
    is not per bus. Reading its first *num_buses* entries, or broadcasting
    the first one, gave the buses another node's value (another system's
    padding zero: no maintenance or throughput cost)."""
    if not arr:
        return [default] * num_buses
    if len(arr) == 1:
        return [arr[0]] * num_buses
    if bus_to_node is None:
        return (list(arr[:num_buses]) if len(arr) >= num_buses
                else [arr[0]] * num_buses)
    return [arr[node] if 0 <= node < len(arr) else default
            for node in bus_to_node[:num_buses]]


def _unit_node_span(offsets: dict, unit_key: str, num_nodes: int) -> tuple[str, int, int]:
    """``(system, first node, node count)`` of a merged unit key.

    Merged keys are ``{system}__{key}``: the unit's per-node data are its
    own system's, at that system's offset in the merged node set. A key
    of no listed system (or no *offsets*) spans all nodes.
    """
    names = list(offsets)
    offs = list(offsets.values())
    for i, sname in enumerate(names):
        if str(unit_key).startswith(f"{sname}__"):
            end = offs[i + 1] if i + 1 < len(offs) else num_nodes
            return sname, offs[i], end - offs[i]
    return "", 0, num_nodes


def _system_profile_loader(offsets: dict, num_nodes: int, read, fill: float):
    """``load(key, file) -> [hours x num_nodes]`` for per-node unit profiles.

    A profile file holds one column per node of the unit's own system
    (*read(node count)*); it was read as if it covered the merged
    node set, so a second system's units got the first system's columns.
    Each system's read is placed at its own nodes. Units of different
    systems sharing a file get one array with each system's columns at
    its nodes (a unit has capacity only at its own system's nodes);
    columns no system has read hold *fill*.
    """
    merged: dict[str, np.ndarray] = {}
    placed: set = set()

    def load(unit_key: str, file_key: str) -> np.ndarray:
        sname, off, cnt = _unit_node_span(offsets, unit_key, num_nodes)
        arr = merged.get(file_key)
        if (file_key, sname) not in placed:
            prof = np.asarray(read(cnt), dtype=float)
            if prof.ndim == 1:
                prof = prof[:, None]
            if arr is None:
                arr = np.full((prof.shape[0], num_nodes), fill, dtype=float)
                merged[file_key] = arr
            rows = np.arange(arr.shape[0]) % prof.shape[0]
            arr[:, off:off + cnt] = prof[rows, :cnt]
            placed.add((file_key, sname))
        return arr

    return load


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
        # Line-loss repair bounds of the master solve, per planned year
        self._master_loss_repair: dict[int, dict] = {}
        self.master_solution: Optional[dict] = None  # set by run(master_only=True)
        # Myopic runs: {year: {"objective", "earlier_annuities", "total"}} ($)
        self.myopic_year_costs: dict = {}
        # Binding constraints, duals and paid slacks of each master solve
        # ({"year_{YYYY}" | "all": group}, esfex.io.master_diagnostics)
        self.master_diagnostics: dict[str, dict] = {}

        logger.debug(f"Orchestrator initialized for system: {self.system_name}")
        logger.debug(f"Mode: {config.simulation_mode}")
        logger.debug(f"Solver: {config.solver.name}")

        # What a myopic run's checkpoint identifies the case by (hash of the
        # configuration and its input files), taken before the plugins
        # adjust the configuration, as `esfex run --dry-run --resume` sees it
        self._case_fingerprint: Optional[dict] = None
        if (config.simulation_mode == "development"
                and getattr(config.master_problem, "planning_mode", None) == "myopic"):
            from esfex.io.checkpoint import fingerprint

            try:
                self._case_fingerprint = fingerprint(config, self.config_path)
            except Exception as e:  # never fail a run over its checkpoint
                logger.warning(f"This run writes no checkpoint and cannot be resumed: "
                               f"fingerprinting the case failed ({type(e).__name__}: {e})")

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

        # Existing units the NPV-based retirement retired (npv_retirement):
        # dicts with kind, key, name, bus, node, npv ($), iteration, year,
        # index, capacity_mw and share (see _record_npv_retirements)
        self._npv_retirements: list[dict] = []

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
                        if field_name == "num_units":
                            # At least one unit everywhere (no capacity there)
                            d[alias if alias in d else field_name] = _pad_int(val, si, 1)
                        elif field_name in ("life_time", "initial_age", "min_up", "min_down"):
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
        # frequency_nominal / frequency_nadir_limit / rocof_limit /
        # load_damping are not read by the dispatch or the master; the
        # frequency analysis (SLD gauge, Analysis panel) takes them from each
        # system's own config, so a 50 Hz + 60 Hz merge keeps both.
        base_scalars = {f: getattr(base, f) for f in (
            "frequency_nominal", "frequency_nadir_limit", "rocof_limit",
            "load_damping", "reserve_margin", "reserve_static_default_ratio",
            "dynamic_reserve_contribution", "soc_end_tolerance",
            "min_cycling_ratio", "min_cycling_period_days",
            "demand_shift_cost_rate",
            "npv_annual_return_rate", "npv_retirement",
            "npv_retirement_max_iterations", "npv_retirement_threshold",
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
            max_annual_investment=sum(s.max_annual_investment for s in systems),
            max_annual_operating_cost=_merged_operating_budget(systems),
            budget_scope=_merged_budget_scope(systems),
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
        resume_from: Optional[Union[str, Path]] = None,
        force_resume: bool = False,
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
                decisions are left in ``self.master_solution`` and no year
                results are returned.
            dispatch_years: Run the operational dispatch only for these years
                of the horizon (labels); the others are skipped, while every
                year keeps its own data block (year index x 8760 h) and the
                master problem still plans the whole horizon. In
                ``unit_commitment`` mode the first of them is dispatched
                instead of the first horizon year. Default: every year (or
                the ``ESFEX_OPS_ONLY_YEARS`` environment variable).
            resume_from: Results file of an interrupted myopic development
                run to continue (:mod:`esfex.io.checkpoint`): the run starts
                at the first year its checkpoint does not list as completed,
                with the years, start year and dispatch years of that run,
                and appends to that file. *years* and *start_year* may be
                left out (given, they must match).
            force_resume: Resume although the case, its input files or the
                ESFEX version changed since the checkpoint was written (each
                difference is logged as a warning).

        Returns:
            List of YearResults for each simulated year (resumed: the years
            this call solved)

        Raises:
            RunAbortedError: the master problem (development mode) found no
                plan, so nothing was dispatched.
            esfex.io.checkpoint.ResumeError: *resume_from* cannot be resumed
                (its ``errors`` say why); nothing was run or written.

        The WARNING and ERROR records logged meanwhile (Julia's ``@warn``
        included) are grouped by kind: at the end, also after a failure,
        a summary is logged, kept in ``self.warnings_summary``
        (:class:`esfex.api.diagnostics.WarningsSummary`) and written to
        ``/diagnostics/warnings`` of the results file. A resumed run adds
        its records to those of the run it resumes.

        A myopic development run writes a checkpoint next to its results
        file after every master year and every dispatched year (see
        :mod:`esfex.io.checkpoint`), so it can be resumed.
        """
        from esfex.api.diagnostics import collect_warnings

        self._hdf5_path = None
        self.warnings_summary = None
        self.resume_plan = None
        with collect_warnings() as collector:
            try:
                if resume_from is not None:
                    if master_only or dispatch_years is not None:
                        raise ValueError("resume_from cannot be combined with master_only "
                                         "or dispatch_years: the resumed run keeps its own")
                    self.resume_plan = self._check_resume(
                        resume_from, years, start_year, force_resume)
                    collector.merge(self.resume_plan.doc.get("warnings"))
                return self._run(years=years, start_year=start_year,
                                 master_only=master_only, dispatch_years=dispatch_years)
            finally:
                self._finish_warnings(collector)

    def _check_resume(self, results, years, start_year, force: bool):
        """The checked :class:`~esfex.io.checkpoint.ResumePlan` of *results*
        (raises ``ResumeError``); a forced difference is logged loudly."""
        from esfex.io.checkpoint import check_resume

        plan = check_resume(results, self.config, self.config_path,
                            years=years, start_year=start_year, force=force,
                            case=self._case_fingerprint)
        for change in plan.forced:
            logger.warning(f"--force-resume: {change}. The years already solved keep the "
                           "results of the case as it was; the rest use the case as it is now.")
        at = (f"at {plan.resume_year}" if plan.resume_year is not None
              else "to finish the file")
        logger.info(f"Resuming {plan.results.name} {at} ({len(plan.master_years)} master "
                    f"and {len(plan.dispatch_years)} dispatch years completed)",
                    extra={"milestone": True})
        return plan

    def _finish_warnings(self, collector) -> None:
        """The run's warnings: summary logged (a milestone, so the basic
        console, which hides warnings, shows it), kept on
        ``self.warnings_summary`` and written to the results file."""
        from esfex.api.diagnostics import write_warnings

        summary = collector.summary()
        self.warnings_summary = summary
        path = getattr(self, "_hdf5_path", None)
        if path is not None and Path(path).exists():
            try:
                write_warnings(path, summary)
            except Exception as exc:  # never fail a run over its diagnostics
                logger.debug(f"Could not write /diagnostics/warnings to {path}: {exc}")
        if summary.total:
            logger.info(f"The run logged {summary.text()}", extra={"milestone": True})

    def _run(
        self,
        years: Optional[int] = None,
        start_year: Optional[int] = None,
        master_only: bool = False,
        dispatch_years: Optional[Iterable[int]] = None,
    ) -> list[YearResults]:
        """Body of :meth:`run`."""
        if master_only and self.config.simulation_mode != "development":
            raise ValueError(
                "master_only needs simulation_mode 'development' "
                f"(got {self.config.simulation_mode!r})")
        self.master_solution = None
        self.master_diagnostics = {}
        reset_run_warnings()
        plan = getattr(self, "resume_plan", None)
        if plan is not None:
            # The resumed run's own horizon and dispatch years
            years = int(plan.doc["run"]["years"])
            start_year = int(plan.doc["run"]["start_year"])
            stored = plan.doc["run"].get("dispatch_years")
            dispatch_years = stored if stored is not None else range(start_year, start_year + years)
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
        self._log_model_clock()
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
        # Renewable by the model's rule (type "Renewable"), stamped on each
        # generation dataset as ``attrs["is_renewable"]`` for the RE share
        self._gen_renewable = [
            gen.type == "Renewable" for gen in self.primary_system.generators.values()
        ]
        # Committed in unit commitment (not a variable renewable everywhere):
        # only these get gen_status / gen_startup datasets
        self._gen_committed = [
            is_committed_unit(gen.type, gen.fuel, getattr(gen, 'reservoir_capacity', None))
            for gen in self.primary_system.generators.values()
        ]
        self._bat_names = unique_unit_names([
            bat.name if hasattr(bat, 'name') else key
            for key, bat in self.primary_system.batteries.items()
        ])
        # MW in service per unit, set each year (see _rebuild_unit_names)
        self._gen_in_service_mw = []
        self._gen_num_units = []
        self._bat_in_service_mw = []

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
        if plan is not None:
            # A resumed run appends to its own file, without what the
            # interrupted run wrote of the year it did not finish
            from esfex.io.checkpoint import truncate_results

            hdf5_path = plan.results
            dropped = truncate_results(hdf5_path, set(plan.dispatch_years))
            if dropped:
                logger.info(f"Dropped the unfinished year(s) {dropped} from {hdf5_path.name}")
        else:
            _cfg_stem = self.config_path.stem if self.config_path else self.system_name
            _now = datetime.now()
            _ts = _now.strftime("%d%m%Y_%H%M")
            hdf5_path = self.output_dir / f"{_cfg_stem}_{_ts}.h5"
        # Stash on self so deeper methods (MGA export inside the master
        # problem solver) can reach the same file without threading the
        # path through several function signatures.
        self._hdf5_path = hdf5_path
        if plan is None:
            self._initialize_hdf5(
                hdf5_path,
                num_nodes=num_nodes,
                # Years actually dispatched (unit_commitment runs only one)
                num_years=len(run_years),
                start_year=run_years[0],
                end_year=run_years[-1] + 1,
            )
        self._start_checkpoints(hdf5_path, master_only, years_range, ops_only)

        # ============================================================
        # STEP 1: SOLVE MASTER PROBLEM (only in development mode)
        # ============================================================
        master_investments = {}
        master_retirements = {}
        master_cumulative_caps = {}
        master_re_targets = {}

        resume_dispatch = plan is not None and plan.phase == PHASE_DISPATCH

        if self.config.simulation_mode == "development" and resume_dispatch:
            # Every master year was solved before the interruption
            (master_investments, master_retirements, master_cumulative_caps,
             master_re_targets) = self._restore_master_plan(plan.state)
            logger.info(f"Master plan of {len(master_investments)} years restored from "
                        f"{plan.checkpoint.name}")
            self._pm.call_hook(
                "post_master_problem",
                investments=master_investments,
                retirements=master_retirements,
                config=self.config,
            )
        elif self.config.simulation_mode == "development":
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
                # What the years solved before the failure bound
                self._write_master_diagnostics(hdf5_path)
                self._pm.teardown_all()
                raise RunAbortedError(
                    "the master problem found no plan (see the log above); "
                    "no operational dispatch was run")

            total_inv = sum(len(v) for v in master_investments.values())
            total_ret = sum(len(v) for v in master_retirements.values())
            console.print(f"[green]Master Problem solved[/green] ({total_inv} investments, {total_ret} retirements)")
            logger.info(f"Master problem solved for {len(years_range)} years")

            self._write_npv_retirements(hdf5_path)
            self._write_master_diagnostics(hdf5_path)
            self._master_plan = (master_investments, master_retirements,
                                 master_cumulative_caps, master_re_targets)
            self._write_checkpoint(PHASE_DISPATCH, run_years[0])
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

            # Years an interrupted run dispatched (resumed run): their
            # results are in the file; the carry-over is where they ended
            done = set(plan.dispatch_years) if resume_dispatch else set()
            if done:
                self._restore_dispatch_state(plan.state)
                progress.update(year_task, advance=len(done & set(run_years)))
            for year_idx, year in enumerate(years_range):
                if (ops_only is not None and year not in ops_only) or year in done:
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
                            units_config, year_caps, year_offset=year_idx
                        )
                        self._apply_npv_retirements(cumulative_units_config, year)

                    # Rebuild gen/bat name lists to include virtual units
                    # (must happen BEFORE HDF5 export so all generators are captured)
                    self._rebuild_unit_names(cumulative_units_config,
                                             year_offset=year - self.state.base_year)

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

                    # Run operational dispatch with rolling horizon.
                    # target_re_penetration is a planning target: the dispatch
                    # gets the master's trajectory value for this year, and
                    # none in the operations modes (no master).
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
                    if self.config.simulation_mode == "development":
                        logger.info(f"Year {year} RE target (operational): {year_re_target:.1%}")
                    year_result = self._run_operational_dispatch(
                        year=year,
                        year_idx=year_idx,
                        num_years=num_years,
                        demand=year_demand,
                        hours=len(year_demand),
                        num_nodes=num_nodes,
                        units_config=cumulative_units_config,
                        re_penetration_target=year_re_target,
                    )
                    year_result.investments = year_investments
                    year_result.retirements = master_retirements.get(year, {})
                    year_result.master_re_target = year_re_target
                    year_result.master_loss_repair = self._master_loss_repair.get(year)

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

                    nxt = [y for y in run_years if y > year]
                    self._write_checkpoint(PHASE_DISPATCH, nxt[0] if nxt else None,
                                           completed=year_result)
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
        self._write_checkpoint(PHASE_COMPLETE, None)

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

    def _log_model_clock(self) -> None:
        """Log the clock the case's hourly series are on (a warning when the
        case does not record one), and the node time zones when there are
        several."""
        from esfex.utils.model_clock import log_case_clock

        coords: list[tuple[float, float]] = []
        for name in self._system_names:
            nc = getattr(self.config.systems[name].nodes, "node_coordinates", None)
            coords.extend((c.latitude, c.longitude) for c in nc or [])
        try:
            log_case_clock(self.config.temporal.model_clock, coords)
        except Exception as exc:  # never block a run on a log line
            logger.debug(f"Model clock log failed: {exc}")

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

    def _fuel_price_base_year(self, first_year: int) -> int:
        """Year the ``fuels`` prices hold for, from which ``price_growth_rate``
        grows them: the run's first year (``state.base_year``; *first_year*
        when no run has started)."""
        return self.state.base_year if self.state is not None else first_year

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
                upd["invest_max_power"] = [remaining_limit(m, _at(used[key], n))
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
        profile (the dispatch runs such a unit at full capacity), × its
        scheduled outages in that year (``outage_schedule``, as the dispatch
        folds them in).
        """
        from esfex.bridge.converters import unit_outage_factors
        cache = getattr(self, '_availability_cache', None) or {}
        sched = getattr(self.primary_system, "outage_schedule", None) or []
        re_available = np.zeros((hours, num_nodes))
        for avail_key, rated in units:
            rated = np.asarray(rated, dtype=float)[:num_nodes]
            if not np.any(rated > 0):
                continue
            availability = cache.get(avail_key)
            if availability is None or len(availability) == 0:
                block = np.ones(hours)
            else:
                block = _profile_year_block(np.asarray(availability, dtype=float),
                                            year_idx, hours)
            outages = unit_outage_factors(sched, "generator", [avail_key],
                                          year_idx * HOURS_STD_YEAR, hours)
            for n in range(len(rated)):
                if rated[n] <= 0:
                    continue
                if block.ndim == 1:
                    a = block
                elif n < block.shape[1]:
                    a = block[:, n]
                else:
                    continue
                for key in ((1, 0), (1, n + 1)):
                    if key in outages:
                        a = a * outages[key][:len(a)]
                re_available[:, n] += rated[n] * a
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

        The RE share is renewable over total generation (:func:`re_share`).
        Before any solve, generation is taken to meet the demand each
        timestep: the renewable part is ``min(RE_available, demand)`` (excess
        RE above demand is curtailed) and fossil units make up the rest, so
        the share is Σ usable RE / Σ demand. This accounts for the temporal
        mismatch between renewable availability and load profiles. A
        renewable unit without an availability profile counts at full
        capacity, and scheduled outages apply, as in the dispatch.

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
        to account for temporal mismatch, over the demand: the system's RE
        share (renewable over total generation) with generation meeting the
        demand, as ``_calculate_initial_re_penetration``.

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
        date_start = self._data_start().strftime("%d/%m/%Y %H:%M")
        offsets = getattr(self, "_system_node_offsets", None) or {}
        loaders: dict = {}

        def _load(kind: str, key: str, avail_file: str, unit_key: Optional[str] = None):
            # Same reader as the dispatch's fallback (load_run_availability):
            # path resolved like demand paths, hourly, data calendar; the
            # columns are the unit's own system's nodes (_system_profile_loader)
            file_key = str(avail_file)
            if file_key not in loaders:
                loaders[file_key] = _system_profile_loader(
                    offsets, num_nodes, lambda cnt: load_run_availability(
                        config_dir, avail_file, cnt, date_start,
                        owner=f"{kind} {key!r}"), fill=1.0)
            cache[key] = loaders[file_key](unit_key or key, file_key)
            # Also index by the original availability_file string so that
            # virtual generators can look up by file path directly.
            cache[avail_file] = cache[key]

        for gen_key, gen in sys.generators.items():
            avail_file = getattr(gen, 'availability_file', None)
            if avail_file:
                _load("Generator", gen_key, avail_file)
            elif (gen.type == "Renewable"
                  and not any(c > 0 for c in (gen.reservoir_capacity or []))
                  and any(p > 0 for p in gen.rated_power)):
                _warn_flat_renewable("Generator", gen_key)

        # Also preload technology availability profiles
        for tech_key, tech in sys.technologies.items():
            avail_file = getattr(tech, 'availability_file', None)
            if avail_file:
                _load("Technology", tech_key, avail_file)
            elif tech.type == "Renewable" and any(p > 0 for p in tech.invest_max_power):
                _warn_flat_renewable("Technology", tech_key)

        # Batteries and battery technologies (factor on charge/discharge
        # power), under their own keys so they cannot shadow a generator's
        for bat_key, bat in sys.batteries.items():
            if bat.availability_file:
                _load("Battery", battery_availability_key(bat_key), bat.availability_file,
                      unit_key=bat_key)
        for bt_key, bat_tech in sys.battery_technologies.items():
            if bat_tech.availability_file:
                _load("Battery technology", battery_technology_availability_key(bt_key),
                      bat_tech.availability_file, unit_key=bt_key)

        logger.debug(f"Loaded {len(loaders)} unique availability files for {len(cache)} units/technologies")
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
        date_start = self._data_start().strftime("%d/%m/%Y %H:%M")
        offsets = getattr(self, "_system_node_offsets", None) or {}
        loaders: dict = {}

        for gen_key, gen in sys.generators.items():
            inflow_file = getattr(gen, 'reservoir_inflow_file', None)
            if not inflow_file:
                continue

            inflow_path = config_dir / inflow_file
            if not inflow_path.exists():
                inflow_path = Path(inflow_file)

            file_key = str(inflow_path.resolve())
            if file_key not in loaders:
                # Columns are the unit's own system's nodes; no inflow elsewhere
                loaders[file_key] = _system_profile_loader(
                    offsets, num_nodes, lambda cnt, path=inflow_path: load_inflow_profile(
                        path, temporal_resolution_hours=1, num_nodes=cnt,
                        date_start=date_start), fill=0.0)
            try:
                cache[gen_key] = loaders[file_key](gen_key, file_key)
            except Exception as e:
                logger.warning(f"Failed to load inflow for {gen.name}: {e}")

        if cache:
            logger.debug(f"Loaded {len(loaders)} unique inflow files for {len(cache)} generators")
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

        self._npv_retirements = []
        self._master_loss_repair = {}
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

            # Achieved RE share per system, for the log
            current_re_per_system: dict[str, float] = {
                name: init for name, _, _, init in system_node_ranges}

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

            # The representative days (or TSAM periods) and the critical
            # days, chosen once for the horizon: every annual master models
            # the same calendar days with its own year's demand and profiles
            use_tsam = master_cfg.use_tsam if master_cfg else False
            horizon_demand_agg = (aggregate_demand_to_resolution(
                master_demand[:hours_needed], target_hours=resolution_hours)
                if resolution_hours > 1 else master_demand[:hours_needed])
            day_plan = self._master_day_plan(horizon_demand_agg, num_years, resolution_hours)
            tsam = day_plan.tsam

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
            # NPV-based retirement: the original units retired by earlier
            # years stay retired; built units are not checked
            npv_retired: list[tuple] = []
            npv_unit_keys = (set(self.master_system.generators)
                             | set(self.master_system.batteries))
            # Annuity streams of the years solved so far, (0-based build
            # year, life, $/year), for the reported yearly cost
            annuity_streams: list[tuple[int, float, float]] = []
            self.myopic_year_costs = {}

            # A resumed run: the loop state where the interrupted run's
            # last completed year left it (written below, after each year)
            plan = getattr(self, "resume_plan", None)
            carried = (plan.state.get("master") if plan is not None
                       and plan.phase == PHASE_MASTER else None)
            first_idx = 0
            if carried:
                if carried.get("day_plan") != _day_plan_digest(day_plan):
                    raise ResumeError([ApiError(
                        "day_plan_changed",
                        "The master's representative days differ from the interrupted "
                        "run's: the remaining years would not plan on the same days",
                        key="master_problem", fix="Start a new run.")])
                investments_by_year = carried["investments_by_year"]
                retirements_by_year = carried["retirements_by_year"]
                cumulative_caps_by_year = carried["cumulative_caps_by_year"]
                built = carried["built"]
                elz_builds = carried["elz_builds"]
                current_re = carried["current_re"]
                current_re_per_system = carried["current_re_per_system"]
                npv_retired = list(carried["npv_retired"])
                annuity_streams = list(carried["annuity_streams"])
                self.myopic_year_costs = carried["myopic_year_costs"]
                self._master_fuel_by_year = carried["master_fuel_by_year"]
                self._npv_retirements = list(carried["npv_retirements"])
                self.master_diagnostics = carried["master_diagnostics"]
                first_idx = len(investments_by_year)
                logger.info(f"Myopic master: years {years_range[0]}-{years_range[first_idx - 1]} "
                            "restored from the checkpoint")

            for y_idx, year in enumerate(years_range):
                if y_idx < first_idx:
                    continue
                logger.debug(f"\n--- Myopic: Year {year} ({y_idx+1}/{num_years}) ---")

                # 1. This year's RE target on the horizon trajectory
                year_targets = {(s + 1, 1): path[y_idx] for s, path in enumerate(re_paths)}
                year_target_re = re_paths[0][y_idx]
                # The minimum-increment floor (RE-3) perfect foresight places
                # on this year: anchored where its chain stands the year
                # before (initial share + y_idx minimum steps, at most 100 %)
                year_anchor = myopic_re_anchor(overall_initial_re, min_inc, y_idx)
                # The maximum increment caps the growth over the share the
                # previous year achieved (year 0: the initial share), in [0, 1]
                share_anchor = ([min(max(current_re_per_system[name], 0.0), 1.0)
                                 for name, _, _, _ in system_node_ranges]
                                if system_node_ranges else
                                [min(max(current_re, 0.0), 1.0)])
                logger.debug(f"  RE target: {year_target_re:.2%} (achieved last year: "
                             f"{current_re:.2%}, floor anchor: {year_anchor:.2%})")

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
                for unit_key, bt_key in built_bats:
                    bt_cache_key = battery_technology_availability_key(bt_key)
                    if bt_cache_key in year_avail_cache:
                        year_avail_cache[battery_availability_key(unit_key)] = \
                            year_avail_cache[bt_cache_key]

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

                # 4. TSAM periods: the ones chosen once for the horizon
                tsam_starts_y = [[st + 1 for st in tsam.period_start_hours]] if tsam else []
                tsam_weights_y = [list(tsam.period_weights)] if tsam else []
                tsam_day_period_y = [[k + 1 for k in tsam.day_period]] if tsam else []

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
                    for name, first_bus, n_bus, init in system_node_ranges:
                        year_sys_ranges.append(
                            (name, first_bus, n_bus, myopic_re_anchor(init, min_inc, y_idx)))

                # 7. Create single-year MasterProblem
                master = MasterProblemAdapter(
                    config=self.config,
                    years=[year],
                    base_year=year,
                    demand=year_demand_agg,
                    demand_growth=0.0,  # No growth within single year
                    discount_rate=getattr(primary, 'discount_rate', 0.05),
                    max_annual_investment=getattr(primary, 'max_annual_investment', 1e9),
                    budget_scope=getattr(primary, 'budget_scope', 'investment'),
                    target_re_penetration=year_target_re,
                    initial_re_penetration=year_anchor,
                    min_re_increment=min_inc,
                    max_re_increment=max_inc,
                    system_node_ranges=year_sys_ranges,
                    re_target_override=year_targets,
                    re_share_anchor=share_anchor,
                    # Fuel prices grow from the run's first year, not this one
                    fuel_price_base_year=self._fuel_price_base_year(years_range[0]),
                    temporal_resolution_hours=resolution_hours,
                    investment_resolution_hours=getattr(temporal, 'investment_resolution', HOURS_STD_YEAR),
                    representative_days_per_year=master_cfg.representative_days if master_cfg else 5,
                    min_day_separation=master_cfg.min_day_separation if master_cfg else 5,
                    rep_day_offsets=day_plan.rep_day_offsets,
                    critical_day_offsets=day_plan.critical_day_offsets,
                    rep_day_weights=day_plan.rep_day_weights,
                    storage_firm_hours=day_plan.storage_firm_hours,
                    solver_method=master_cfg.solver_method if master_cfg else "monolithic",
                    benders_max_iterations=master_cfg.benders_max_iterations if master_cfg else 50,
                    benders_tolerance=master_cfg.benders_tolerance if master_cfg else 1e-4,
                    use_tsam=use_tsam,
                    tsam_period_start_hours=tsam_starts_y,
                    tsam_period_weights=tsam_weights_y,
                    tsam_day_period=tsam_day_period_y,
                    tsam_inter_period_linking=(
                        master_cfg.tsam_inter_period_linking if master_cfg else True
                    ),
                    tsam_critical_periods=tsam.critical_periods if tsam else None,
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
                    retired_units=list(npv_retired),
                    npv_unit_keys=npv_unit_keys,
                    # The NPV covers the unit's years up to the horizon end
                    npv_horizon_end=years_range[-1],
                    debug_lp_dir=self._benders_debug_lp_dir(),
                )

                # 7. Solve
                master.build_model(use_representative_days=True)
                status = master.solve()

                if status != 1:
                    # Later years build on this one: without its plan they
                    # would plan from a stale fleet, so the run stops
                    self._report_myopic_failure(master, year, years_range[:y_idx])
                    return None, None, None, None

                self._record_master_diagnostics(master, f"Master {year}")
                result = master.get_solution_values()
                if isinstance(getattr(master, "loss_repair", None), dict):
                    self._master_loss_repair[year] = master.loss_repair
                self._report_myopic_year_cost(year, y_idx, result, annuity_streams)
                self._store_master_fuel(master, result, [year], carried=fuel_carried)
                for r in self._record_npv_retirements(master, year):
                    npv_retired.append((r["kind"], r["key"], r["bus"]))
                to_caps = getattr(master, "fuel_capacities_by_year", None)
                year_elz = ((to_caps(result) if to_caps is not None else {})
                            .get(1, {}).get("electrolyzer", {}))
                for key, per_node in year_elz.items():
                    elz_builds.setdefault(key, {})[y_idx] = list(per_node)
                solution = result.get('solution', {})
                year_data = solution.get(year, {})

                # 8. Extract investments (all of them: the later years
                # inherit exactly what this year's dispatch ran with)
                year_inv = master_year_builds(year_data)

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
                        retired = self._retired_share(py_idx, node_values, year)
                        if retired > 0.01:
                            year_ret[f'gen_{py_idx}'] = retired
                retirements_by_year[year] = year_ret

                # 10. Capacity in service this year, for the dispatch
                cumulative_caps_by_year[year] = self._myopic_year_caps(
                    result, built_gens, built_bats, built, y_idx)

                # 11. Accumulate investments for next year
                self._accumulate_myopic_investments(year_inv, y_idx, built)

                # 12. Achieved RE share (global + per system), for the log
                re_pen = np.atleast_1d(result.get('re_penetration_by_year', []))
                current_re = float(re_pen[0]) if re_pen.size else year_target_re
                for sname, re_vals in result.get('re_penetration_by_system', {}).items():
                    re_vals = np.atleast_1d(re_vals)
                    if sname in current_re_per_system and re_vals.size:
                        current_re_per_system[sname] = float(re_vals[0])

                total_year_inv = sum(year_inv.values())
                re_sys_str = ""
                if current_re_per_system:
                    re_sys_str = " (per system: " + ", ".join(
                        f"{s}={v:.1%}" for s, v in current_re_per_system.items()) + ")"
                logger.debug(
                    f"  Year {year}: {len(year_inv)} investments "
                    f"({total_year_inv:.0f} MW total), "
                    f"{len(year_ret)} retirements, "
                    f"RE achieved: {current_re:.2%}{re_sys_str}"
                )

                # 13. Checkpoint: what the next year starts from
                last = y_idx + 1 == num_years
                self._write_checkpoint(
                    PHASE_MASTER, None if last else years_range[y_idx + 1],
                    master_year=year, master_state={
                        "day_plan": _day_plan_digest(day_plan),
                        "investments_by_year": investments_by_year,
                        "retirements_by_year": retirements_by_year,
                        "cumulative_caps_by_year": cumulative_caps_by_year,
                        "built": built, "elz_builds": elz_builds,
                        "current_re": current_re,
                        "current_re_per_system": current_re_per_system,
                        "npv_retired": npv_retired, "annuity_streams": annuity_streams,
                        "myopic_year_costs": self.myopic_year_costs,
                        "master_fuel_by_year": getattr(self, "_master_fuel_by_year", None) or {},
                        "npv_retirements": self._npv_retirements,
                        "master_diagnostics": self.master_diagnostics,
                    })

            # Summary
            total_inv = sum(len(v) for v in investments_by_year.values())
            total_ret = sum(len(v) for v in retirements_by_year.values())
            logger.debug(
                f"\nMyopic planning complete: {total_inv} investments, "
                f"{total_ret} retirements across {num_years} years"
            )
            if self.myopic_year_costs:
                logger.info(
                    "Myopic plan cost, sum of the yearly costs with the earlier "
                    "builds' annuities (undiscounted): $%s",
                    f"{sum(c['total'] for c in self.myopic_year_costs.values()):,.0f}")

            # Each year's RE target, as its master had it
            re_targets_by_year = {year: re_paths[0][y_idx]
                                  for y_idx, year in enumerate(years_range)}

            return investments_by_year, retirements_by_year, cumulative_caps_by_year, re_targets_by_year

        except ResumeError:
            raise
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            logger.error(f"Myopic master problem failed: {e}\n{tb}")
            console.print(f"[bold red]Myopic master problem error:[/bold red] {e}")
            console.print(f"[dim]{tb}[/dim]")
            return None, None, None, None

    def _rep_day_dates(self, offsets: Iterable[int], resolution_hours: int) -> str:
        """Calendar dates ("15 Jan") of master steps *offsets* within a year,
        on the data's calendar (``date_start``, Feb 29 skipped)."""
        steps = HOURS_STD_YEAR // max(1, resolution_hours)
        index = model_time_index(self._data_start(), steps, resolution_hours)
        return ", ".join(index[int(o) % steps].strftime("%d %b") for o in offsets)

    def _master_re_profiles(self, resolution_hours: int,
                            transform=None) -> list[tuple[str, np.ndarray, np.ndarray]]:
        """The distinct availability profiles of the renewable generators
        and candidate technologies the master uses (the run's preloaded
        profiles, ``_availability_cache``), at the master's resolution, as
        ``(label, profile [steps × nodes], series)``. *series* is the mean
        over the nodes where the units sharing the profile have capacity
        (``rated_power`` / ``invest_max_power`` > 0; all nodes if none).
        Units with the same profile (same file or same values) share one
        entry, labelled with their keys. *transform*, when given, maps each
        profile to the one returned (and its series)."""
        return [entry[:3] for entry in
                self._master_re_profiles_with_capacity(resolution_hours, transform)]

    def _master_re_profiles_with_capacity(
            self, resolution_hours: int,
            transform=None) -> list[tuple[str, np.ndarray, np.ndarray, float]]:
        """``_master_re_profiles`` with, for each profile, the renewable
        capacity behind it (MW): the rated power of the existing units plus
        the investment limit of the candidate technologies sharing it, over
        all nodes. It weighs the profile in the renewable supply of the
        critical days (``master_critical_days``)."""
        cache = self._availability_cache or {}
        groups: dict[bytes, list] = {}
        units = [(k, g.rated_power) for k, g in self.primary_system.generators.items()
                 if g.type == "Renewable"]
        units += [(k, t.invest_max_power) for k, t in self.primary_system.technologies.items()
                  if t.type == "Renewable"]
        for key, cap in units:
            avail = cache.get(key)
            if avail is None or len(avail) == 0:
                continue
            avail = np.asarray(avail, dtype=float)
            if avail.ndim == 1:
                avail = avail[:, None]
            if resolution_hours > 1:
                avail = aggregate_to_resolution(avail, target_hours=resolution_hours)
            cap = np.asarray(cap or [], dtype=float)
            mask = np.zeros(avail.shape[1], dtype=bool)
            if len(cap) == avail.shape[1]:
                mask = cap > 0
            avail = np.ascontiguousarray(avail)
            digest = hashlib.blake2b(memoryview(avail).cast("B"),
                                     person=str(avail.shape).encode()[:16]).digest()
            entry = groups.setdefault(digest,
                                      [[], avail, np.zeros(avail.shape[1], dtype=bool), 0.0])
            entry[0].append(key)
            entry[2] |= mask
            entry[3] += float(np.clip(cap, 0.0, None).sum())
        out = []
        for keys, avail, mask, capacity in groups.values():
            label = ", ".join(keys[:3]) + (f" (+{len(keys) - 3})" if len(keys) > 3 else "")
            weights = (mask if mask.any() else np.ones_like(mask)).astype(float)
            if transform is not None:
                avail = transform(avail)
            series = avail @ (weights / weights.sum())
            out.append((label, avail, series, capacity))
        return out

    def _log_rep_day_ratios(self, kind: str, demand_agg: np.ndarray, num_years: int,
                            offsets: list[int], resolution_hours: int, profiles: list,
                            weights: Optional[list[float]] = None,
                            warn: bool = True) -> list[float]:
        """Log, per series, the representative days' (or TSAM periods')
        mean over the annual mean, for the demand and each renewable profile
        (``master_rep_day_ratios``): how far the master's year is from the
        real one. Warns (when *warn*) when one is off by more than 10 %,
        advising more representative days or TSAM. Returns the ratios
        (demand first)."""
        from esfex.bridge.adapters import master_rep_day_ratios
        ratios = master_rep_day_ratios(demand_agg, num_years, offsets, resolution_hours,
                                       [s for _, _, s in profiles], weights)
        names = ["demand"] + [label for label, _, _ in profiles]
        text = ", ".join(f"{n} {r:.3f}" for n, r in zip(names, ratios) if np.isfinite(r))
        logger.info("%s mean / annual mean: %s", kind, text)
        off = [n for n, r in zip(names, ratios) if np.isfinite(r) and abs(r - 1.0) > 0.10]
        if off and warn:
            logger.warning(
                "%s misrepresent the year by more than 10 %% for: %s. The master "
                "sees more or less energy than the year has; use more representative "
                "days (master_problem.representative_days) or TSAM (use_tsam).",
                kind, ", ".join(off))
        return ratios

    def _log_tsam_rescaling(self, demand_agg: np.ndarray, num_years: int, tsam,
                            resolution_hours: int) -> None:
        """Log what the master's TSAM periods (critical days included) give
        each series once the renewable profiles are rescaled to each year's
        energy (``rescale_profile_periods``, as ``MasterProblemAdapter``
        rescales the master's profiles): the periods' weighted mean of the
        horizon-mean series over the annual mean of the original one. The
        residual is ~0 unless the cap at full availability stopped a profile
        (said which); the demand is not rescaled. Warns, advising more TSAM
        periods, when a series is still off by more than 10 %."""
        from esfex.models.tsam import rescale_profile_periods

        res = max(1, resolution_hours)
        steps, period = HOURS_STD_YEAR // res, 24 // res
        n_years = max(1, num_years)
        starts = list(tsam.period_start_hours)
        weights = np.asarray(tsam.period_weights, dtype=float)
        years = [(y * steps, starts, list(weights)) for y in range(n_years)]

        def horizon_mean(x: np.ndarray) -> np.ndarray:
            return np.mean([_profile_year_block(np.asarray(x, dtype=float), y, steps)
                            for y in range(n_years)], axis=0)

        def ratio(on_periods: np.ndarray, reference: np.ndarray) -> float:
            annual = float(horizon_mean(reference).mean())
            if annual <= 1e-12:
                return float("nan")
            mean = horizon_mean(on_periods)
            days = np.array([mean[s:s + period].mean() for s in starts])
            return float(weights @ days / weights.sum() / annual)

        total = np.asarray(demand_agg, dtype=float).sum(axis=1)
        names, ratios = ["demand"], [ratio(total, total)]
        rescaled = self._master_re_profiles(res, transform=lambda avail: (
            rescale_profile_periods(avail, years, period, steps, tsam.critical_periods)[0]))
        for (label, _, before), (_, _, after) in zip(self._master_re_profiles(res), rescaled):
            names.append(label)
            ratios.append(ratio(after, before))
        logger.info("TSAM periods with the renewable profiles rescaled to the year's energy, "
                    "mean / annual mean: %s", ", ".join(
                        f"{n} {r:.3f}" for n, r in zip(names, ratios) if np.isfinite(r)))
        capped = [f"{n} {r:.3f}" for n, r in zip(names[1:], ratios[1:])
                  if np.isfinite(r) and r < 1.0 - 1e-3]
        if capped:
            logger.info("The cap at full availability kept the TSAM periods short of the "
                        "year's energy for: %s", ", ".join(capped))
        off = [n for n, r in zip(names, ratios) if np.isfinite(r) and abs(r - 1.0) > 0.10]
        if off:
            logger.warning(
                "TSAM periods misrepresent the year by more than 10 %% for: %s, after "
                "rescaling the renewable profiles to the year's energy (the demand is "
                "not rescaled; a profile at full availability on the periods cannot "
                "rise further). Use more TSAM periods (master_problem.tsam_num_periods).",
                ", ".join(off))

    def _master_rep_day_offsets(self, demand_agg: np.ndarray, num_years: int,
                                resolution_hours: int) -> list[int]:
        """The representative days every master of this run models, as
        steps within a year (0-based): chosen ONCE, from the horizon-mean
        demand and the renewable profiles (``master_rep_day_offsets``: the
        peak day, then days that give the year's mean demand and mean
        availability of each profile), so each year differs from the others
        only by its demand (and its block of multi-year profiles), never by
        the weather sample. Logged with their dates and their bias per
        profile."""
        from esfex.bridge.adapters import master_rep_day_offsets
        master_cfg = getattr(self.config, 'master_problem', None)
        profiles = self._master_re_profiles(resolution_hours)
        offsets = master_rep_day_offsets(
            demand_agg, num_years,
            master_cfg.representative_days if master_cfg else 5,
            master_cfg.min_day_separation if master_cfg else 5,
            resolution_hours, [s for _, _, s in profiles])
        logger.info("Representative days, the same for every planning year: %s",
                    self._rep_day_dates(offsets, resolution_hours))
        self._log_rep_day_ratios("Representative days", demand_agg, num_years, offsets,
                                 resolution_hours, profiles)
        return offsets

    def _master_tsam_periods(self, demand_agg: np.ndarray, num_years: int,
                             resolution_hours: int):
        """TSAM periods every master of this run models, chosen ONCE: the
        clustering (``compute_tsam_periods``) of the horizon-mean demand and
        the availability of the renewable generators and candidate
        technologies (``_master_re_profiles``; the mean over the years of
        each step, each year's block as the dispatch reads it,
        ``_profile_year_block``). Start steps are 0-based within a year.
        Logged with their dates and their bias per profile before the
        renewable profiles are rescaled to the year's energy (the bias
        that remains is logged by ``_log_tsam_rescaling``)."""
        from esfex.models.tsam import compute_tsam_periods

        master_cfg = getattr(self.config, 'master_problem', None)
        steps = HOURS_STD_YEAR // max(1, resolution_hours)
        n_years = max(1, num_years)

        def horizon_mean(arr: np.ndarray) -> np.ndarray:
            return np.mean([_profile_year_block(arr, y, steps) for y in range(n_years)],
                           axis=0)

        profiles = self._master_re_profiles(resolution_hours)
        availability = {label: horizon_mean(avail) for label, avail, _ in profiles}
        result = compute_tsam_periods(
            demand=horizon_mean(demand_agg),
            num_periods=master_cfg.tsam_num_periods if master_cfg else 10,
            method=master_cfg.tsam_method if master_cfg else "kmedoids",
            period_length_hours=24 // resolution_hours,
            availability=availability or None,
        )
        logger.info("TSAM periods, the same for every planning year: %s",
                    self._rep_day_dates(result.period_start_hours, resolution_hours))
        self._log_rep_day_ratios("TSAM periods (before rescaling the renewable profiles)",
                                 demand_agg, num_years, list(result.period_start_hours),
                                 resolution_hours, profiles, list(result.period_weights),
                                 warn=False)
        return result

    def _master_day_plan(self, demand_agg: np.ndarray, num_years: int,
                         resolution_hours: int) -> MasterDayPlan:
        """The days every master of this run models, chosen ONCE: the
        representative days (``_master_rep_day_offsets``) or TSAM periods
        (``_master_tsam_periods``), plus ``master_problem.critical_days``
        critical days, the days of the horizon with the largest net load
        (``master_critical_days``: horizon-mean demand against the renewable
        profiles weighted by the capacity behind each). Each critical day
        stands for one day of the year. The representative days share the
        rest, reweighted so the year keeps their demand and renewable energy
        (``master_day_weights``); a critical day leaves its TSAM cluster
        instead (``with_critical_days``), so every TSAM weight stays the
        period's count of days in the year, the days the master's seasonal
        storage runs through (the renewable profiles are then rescaled to
        the year's energy). Also the storage firm hours of the adequacy
        constraint: ``master_problem.storage_firm_hours`` or the longest
        stretch without solar on the critical days. Logged with their
        dates."""
        from esfex.bridge.adapters import master_critical_days, master_day_weights
        from esfex.models.tsam import with_critical_days

        master_cfg = getattr(self.config, 'master_problem', None)
        use_tsam = master_cfg.use_tsam if master_cfg else False
        tsam = rep_offsets = None
        if use_tsam:
            tsam = self._master_tsam_periods(demand_agg, num_years, resolution_hours)
            base = list(tsam.period_start_hours)
        else:
            rep_offsets = self._master_rep_day_offsets(demand_agg, num_years, resolution_hours)
            base = rep_offsets

        profiles = self._master_re_profiles_with_capacity(resolution_hours)
        series = [s for _, _, s, _ in profiles]
        critical, firm_hours = master_critical_days(
            demand_agg, num_years, master_cfg.critical_days if master_cfg else 3,
            master_cfg.min_day_separation if master_cfg else 5, resolution_hours,
            series, [c for _, _, _, c in profiles], exclude=base)
        weights: list[float] = []
        if critical and tsam is not None:
            tsam = with_critical_days(tsam, critical, 24 // max(1, resolution_hours))
            weights = list(tsam.period_weights)
        elif critical:
            weights = master_day_weights(demand_agg, num_years, base, critical,
                                         resolution_hours, series)
        if critical:
            logger.info("Critical days (lowest renewable supply against demand), the same "
                        "for every planning year, one day of the year each: %s",
                        self._rep_day_dates(critical, resolution_hours))
            self._log_rep_day_ratios(
                "TSAM periods with critical days (before rescaling the renewable "
                "profiles)" if use_tsam else "Representative and critical days",
                demand_agg, num_years, base + critical, resolution_hours,
                [p[:3] for p in profiles], weights, warn=not use_tsam)
        configured = master_cfg.storage_firm_hours if master_cfg else None
        if configured:
            firm_hours = float(configured)
        logger.info("Storage firm hours in the capacity adequacy: %.1f h (%s); a battery "
                    "counts min(power, usable energy / %.1f h)", firm_hours,
                    "configured" if configured else "longest stretch without solar on "
                    "the critical days", firm_hours)

        if tsam is not None:
            weights = []
            # The masters rescale the renewable profiles on the TSAM periods
            # to each year's energy (MasterProblemAdapter._tsam_energy_matched)
            self._log_tsam_rescaling(demand_agg, num_years, tsam, resolution_hours)
        return MasterDayPlan(rep_day_offsets=rep_offsets, critical_day_offsets=(
            [] if use_tsam else critical), rep_day_weights=weights,
            storage_firm_hours=firm_hours, tsam=tsam)

    def _benders_debug_lp_dir(self) -> Optional[str]:
        """The run's ``logs`` folder when ``ESFEX_BENDERS_DEBUG_LP=1``: Benders
        writes the master and subproblem LPs that fail there. None (no LP
        written) otherwise."""
        if os.environ.get("ESFEX_BENDERS_DEBUG_LP") != "1":
            return None
        lp_dir = self.output_dir / "logs"
        lp_dir.mkdir(parents=True, exist_ok=True)
        return str(lp_dir)

    def _master_debug_lp_path(self) -> Optional[Path]:
        """``<output>/logs/master_problem_debug.lp`` when
        ``ESFEX_MASTER_DEBUG_LP=1``: the perfect-foresight monolithic master
        is written there before it is solved. None (no LP written)
        otherwise."""
        if os.environ.get("ESFEX_MASTER_DEBUG_LP") != "1":
            return None
        lp_dir = self.output_dir / "logs"
        lp_dir.mkdir(parents=True, exist_ok=True)
        return lp_dir / "master_problem_debug.lp"

    def _report_myopic_year_cost(self, year: int, y_idx: int, result: dict,
                                 streams: list) -> None:
        """Log myopic year *year*'s cost comparable with a perfect-foresight
        year and keep it in ``myopic_year_costs[year]``: its master's
        objective, which charges only the annuities of what this year builds,
        plus the annuities this year pays on what earlier years built
        (``earlier_build_annuities``). Reporting only: the optimisation is
        unchanged. Then adds this year's annuity streams to *streams*."""
        objective = result.get("objective")
        if objective is not None:
            earlier = earlier_build_annuities(streams, y_idx)
            self.myopic_year_costs[year] = {
                "objective": float(objective), "earlier_annuities": earlier,
                "total": float(objective) + earlier}
            logger.info(
                f"Myopic year {year}: master objective ${objective:,.0f} + "
                f"${earlier:,.0f} annuities of capacity built in earlier years "
                f"= ${objective + earlier:,.0f}")
        # One-year master: its builds are of year index 1
        streams.extend((y_idx, life, annuity)
                       for _, life, annuity in result.get("annuity_streams", []))

    @staticmethod
    def _report_myopic_failure(master, year: int, solved_years: list[int]) -> None:
        """Log why a myopic run stops at *year*: the master's termination
        status, the years already planned and, for an infeasible master,
        the constraints that cannot hold together (when the solver can
        compute them)."""
        status = getattr(master, "_last_status", "") or "unknown status"
        done = (f"years {solved_years[0]}-{solved_years[-1]} were planned"
                if solved_years else "no year was planned")
        lines = [
            f"Myopic year {year}: the master problem is not optimal ({status}). "
            f"Each year plans from the fleet the years before it built, so the "
            f"run stops here ({done}); no dispatch is run on a partial plan."]
        conflict = master.conflict() if "INFEASIBLE" in status.upper() else []
        if conflict:
            lines.append("Constraints that cannot hold together:")
            lines.extend(f"  {c}" for c in conflict)
        elif "INFEASIBLE" in status.upper():
            lines.append(
                "The RE target, budget and capacity adequacy are priced, so a hard "
                "limit cannot be met: check the investment limits, the life-extension "
                "and retirement settings and the custom constraints (the solver "
                "reported no infeasible subsystem).")
        logger.error("\n".join(lines))

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
            return [remaining_limit(lim, used[i]) for i, lim in enumerate(rep(limits))]

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
                    life_time=[bat_tech.lifetime] * n,
                    initial_age=[age] * n,
                    # Degraded from its build year, as perfect foresight
                    # degrades a battery technology's builds
                    degradation_rate=rep(bat_tech.degradation_rate),
                    # Life extension is priced like a new build
                    invest_cost=rep(bat_tech.invest_cost_power),
                    invest_max_power=zeros,
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
                convs[idx].invest_max_power = remaining_limit(convs[idx].invest_max_power, mw)

        # Reservoir capacity built so far, likewise (MWh, at its node)
        gens = list(sys.generators.values())
        for (g, loc), mwh in built.get("reservoir", {}).items():
            node = self._master_index_node(loc)
            if g < len(gens) and 0 <= node < n:
                gen = gens[g]
                cap = _pad_zeros(gen.reservoir_capacity, n)
                lim = _pad_zeros(gen.reservoir_invest_max, n)
                cap[node] += mwh
                lim[node] = remaining_limit(lim[node], mwh)
                gen.reservoir_capacity, gen.reservoir_invest_max = cap, lim

        return sys, built_gens, built_bats

    def _myopic_year_caps(
        self,
        result: dict,
        built_gens: list[tuple[str, str]],
        built_bats: list[tuple[str, str]],
        built: Optional[dict] = None,
        y_idx: int = 0,
    ) -> dict[str, dict]:
        """Capacity in service in a one-year myopic master, for the dispatch.

        Same layout as the perfect-foresight ``cumulative_capacities_by_year``
        entries (read by :meth:`_build_config_from_cumulative`): the original
        units under ``gen`` / ``bat`` / ``bat_power``, and per technology the
        capacity built this year plus what is left (degraded, not yet
        retired) of the units built from it in earlier years. With *built*
        (the earlier years' builds, see :meth:`_accumulate_myopic_investments`)
        and the year index *y_idx*, ``bat_tech_power_rated`` holds the same
        battery power undegraded, for its fixed O&M.
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
        if built is not None:
            # This year's builds (build year y_idx) and the earlier ones
            builds = {y_idx: _year('bat_tech_power_investment')}
            for (bt_key, build_y), mw in built["bat_tech_power"].items():
                if bt_key in bt_idx:
                    by = builds.setdefault(build_y, {})
                    _add(by, bt_idx[bt_key], mw)
            caps['bat_tech_power_rated'] = rated_bat_tech_power(
                builds, y_idx,
                [bt.lifetime for bt in self.master_system.battery_technologies.values()])
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
            tsam_day_period = []

            # The representative days (or TSAM periods) and the critical
            # days: the same for every year (chosen once)
            day_plan = self._master_day_plan(master_demand_aggregated, num_years,
                                             resolution_hours)
            if use_tsam:
                # Each year on its own block of the demand and profiles
                hours_per_year = HOURS_STD_YEAR // resolution_hours
                tsam = day_plan.tsam
                for y_idx in range(num_years):
                    # Absolute position, 1-indexed for Julia
                    tsam_starts.append([s + y_idx * hours_per_year + 1
                                        for s in tsam.period_start_hours])
                    tsam_weights.append(list(tsam.period_weights))
                    # The period of each day of the year (1-indexed)
                    tsam_day_period.append([k + 1 for k in tsam.day_period])

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
                max_annual_investment=getattr(primary, 'max_annual_investment', 1e9),
                budget_scope=getattr(primary, 'budget_scope', 'investment'),
                target_re_penetration=getattr(primary, 'target_re_penetration', 0.5),
                initial_re_penetration=initial_re,
                min_re_increment=getattr(primary, 'min_annual_increment', 0.0),
                max_re_increment=getattr(primary, 'max_annual_increment', 0.10),
                system_node_ranges=foresight_sys_ranges,
                fuel_price_base_year=self._fuel_price_base_year(years_range[0]),
                # Use configured temporal resolution (e.g., 6 hours reduces model size 6x)
                temporal_resolution_hours=resolution_hours,
                investment_resolution_hours=getattr(temporal, 'investment_resolution', HOURS_STD_YEAR),
                representative_days_per_year=master_cfg.representative_days if master_cfg else 5,
                min_day_separation=master_cfg.min_day_separation if master_cfg else 5,
                rep_day_offsets=day_plan.rep_day_offsets,
                critical_day_offsets=day_plan.critical_day_offsets,
                rep_day_weights=day_plan.rep_day_weights,
                storage_firm_hours=day_plan.storage_firm_hours,
                solver_method=master_cfg.solver_method if master_cfg else "monolithic",
                benders_max_iterations=master_cfg.benders_max_iterations if master_cfg else 50,
                benders_tolerance=master_cfg.benders_tolerance if master_cfg else 1e-4,
                # TSAM parameters
                use_tsam=use_tsam,
                tsam_period_start_hours=tsam_starts,
                tsam_period_weights=tsam_weights,
                tsam_day_period=tsam_day_period,
                tsam_inter_period_linking=(
                    master_cfg.tsam_inter_period_linking if master_cfg else True
                ),
                tsam_critical_periods=day_plan.tsam.critical_periods if use_tsam else None,
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
                debug_lp_dir=self._benders_debug_lp_dir(),
            )

            # --- MGA mode ---
            mga_cfg = getattr(master_cfg, 'mga', None)
            use_mga = mga_cfg is not None and getattr(mga_cfg, 'enabled', False)

            if use_mga:
                from esfex.bridge.adapters import MGAAdapter

                if master.npv_retirement:
                    # The NPV retirement first; the alternatives keep the
                    # units it retires
                    master.build_model(use_representative_days=True)
                    if master.solve() != 1:
                        logger.error("Master problem not optimal (NPV retirement before MGA)")
                        return None, None

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

                # LP file for debugging and the LP-level sensitivity analysis,
                # only on request: a large master's LP takes GB and minutes
                lp_file = self._master_debug_lp_path()
                if lp_file is not None and master.solver_method != "benders":
                    master.write_lp(str(lp_file))
                    logger.info(f"Master problem LP exported to: {lp_file}")

                status = master.solve()

                if status != 1:  # Not optimal
                    logger.error(f"Master problem not optimal (status={status})")
                    return None, None

                self._record_master_diagnostics(master, "Master")
                # Extract investment decisions for each year
                result = master.get_solution_values()
                # One solve plans every year (MGA's alternatives record none)
                if isinstance(getattr(master, "loss_repair", None), dict):
                    self._master_loss_repair = dict.fromkeys(years_range, master.loss_repair)

            # Use the legacy-compatible solution structure keyed by actual year
            solution = result.get('solution', {})
            # Fuel storage, routes and electrolyzers, for the dispatch (#89)
            self._store_master_fuel(master, result, list(years_range))
            # Units the NPV-based retirement retired: out of service from
            # the first year
            self._record_npv_retirements(master, years_range[0])

            investments_by_year = {}
            retirements_by_year = {}

            # Also get structured data for retirement analysis
            gen_life_ext = result.get('gen_life_extension', {})

            for year in years_range:
                year_data = solution.get(year, {})

                # Technology, battery (power and energy), transmission,
                # converter and reservoir investments, per node / pair
                year_inv = master_year_builds(year_data)

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
                        retired = self._retired_share(py_idx, node_values, year)
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
            bt_lifetimes = [bt.lifetime for bt in self.master_system.battery_technologies.values()]
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
                    # Rated, for the fixed O&M (the two above are degraded)
                    'bat_tech_power_rated': rated_bat_tech_power(
                        result.get('bat_tech_power_investment', {}), year_key, bt_lifetimes),
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
        buses = self.master_system.buses or []
        if self._master_on_buses() and 0 <= idx < len(buses):
            return buses[idx].parent_node
        return idx

    def _master_on_buses(self) -> bool:
        """Whether the master's per-location results (investments, life
        extension, cumulative capacities) are indexed by the buses of its
        network rather than by node: it solves on its own buses when they
        are not one per node, in node order (``node_bus_mapping_required``).
        The array length cannot tell: a network can have as many buses as
        nodes without being one bus per node."""
        from esfex.bridge.converters import node_bus_mapping_required

        base = self.master_system
        buses = base.buses or []
        return bool(buses) and node_bus_mapping_required(buses, base.num_nodes)

    def _master_values_by_node(self, values, num_nodes: int) -> np.ndarray:
        """One unit's or technology's per-location master values (MW, MWh)
        as a per-node array: the values of a node's buses are summed on a
        bus-level master (see :meth:`_master_on_buses`)."""
        arr = np.asarray(values, dtype=float)
        out = np.zeros(num_nodes)
        if not self._master_on_buses():
            out[:min(len(arr), num_nodes)] = arr[:num_nodes]
            return out
        for idx, v in enumerate(arr):
            node = self._master_index_node(idx)
            if 0 <= node < num_nodes:
                out[node] += v
        return out

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

    def _record_npv_retirements(self, master, year: int) -> list[dict]:
        """Add the units *master*'s NPV-based retirement retired to
        ``self._npv_retirements``, out of service from *year* on, with the
        configured MW at their node (``capacity_mw``) and its share of the
        unit's configured MW (``share``). Returns the added records."""
        added = []
        for r in getattr(master, "npv_retirements", None) or []:
            units = (self.master_system.generators if r["kind"] == "generator"
                     else self.master_system.batteries)
            unit = units.get(r["key"])
            if unit is None:
                continue
            field = "rated_power" if r["kind"] == "generator" else "MaxDischargePower"
            per_node = [float(v) for v in (getattr(unit, field, None) or [])]
            node = int(r["node"])
            mw = per_node[node] if 0 <= node < len(per_node) else 0.0
            total = sum(per_node)
            rec = dict(r, year=int(year), index=list(units).index(r["key"]),
                       capacity_mw=mw,
                       share=min(1.0, mw / total) if total > 0 else 0.0)
            self._npv_retirements.append(rec)
            added.append(rec)
        return added

    def _apply_npv_retirements(self, units_config: dict[str, Any], year: int) -> None:
        """Batteries the NPV-based retirement retired by *year*: no power or
        energy at their node in the dispatch. (A retired generator already
        has no capacity there: the master's cumulative capacity is 0.)"""
        num_nodes = self.primary_system.nodes.num_nodes or 1
        for r in self._npv_retirements:
            if r["kind"] != "battery" or r["year"] > year:
                continue
            unit = units_config.get(r["key"])
            node = int(r["node"])
            if unit is None or not 0 <= node < num_nodes:
                continue
            for f in ("MaxChargePower", "MaxDischargePower", "capacity", "rated_power"):
                if f in unit:
                    vals = [float(x) for x in _per_node_list(unit.get(f, [0.0]), num_nodes)]
                    vals[node] = 0.0
                    unit[f] = vals

    def _record_master_diagnostics(self, master, prefix: str) -> None:
        """Keep the solved *master*'s binding constraints, duals and paid
        slacks (:meth:`MasterProblemAdapter.diagnostics`) in
        ``master_diagnostics`` and log their digest (a few INFO lines).
        Nothing under Benders or when the extraction fails."""
        from esfex.io.master_diagnostics import digest_lines

        diagnostics = getattr(master, "diagnostics", None)
        groups = diagnostics() if callable(diagnostics) else None
        if not groups:
            return
        self.master_diagnostics.update(groups)
        for line in digest_lines(groups, prefix=prefix):
            logger.info(line)

    def _write_master_diagnostics(self, path: Optional[Path]) -> None:
        """Write ``master_diagnostics`` under ``/diagnostics/master`` of the
        results file (``esfex.io.master_diagnostics``); nothing without
        any."""
        if path is None or not self.master_diagnostics:
            return
        from esfex.io.master_diagnostics import write_master_diagnostics

        master_cfg = getattr(self.config, "master_problem", None)
        try:
            write_master_diagnostics(
                path, self.master_diagnostics,
                planning_mode=str(getattr(master_cfg, "planning_mode", "perfect_foresight")))
        except Exception as e:  # diagnosis only: never fail the run
            logger.warning(f"Writing the master diagnostics failed: {e}")

    # ------------------------------------------------------------------
    # Checkpoints of a myopic run (esfex.io.checkpoint)
    # ------------------------------------------------------------------
    def _start_checkpoints(self, hdf5_path: Path, master_only: bool,
                           years_range: list[int], ops_only: Optional[set]) -> None:
        """Checkpoint this run (``self._ckpt``) when it is a myopic
        development run: the case's fingerprint and the run's years now,
        then after every master year and every dispatched year
        (:meth:`_write_checkpoint`). A resumed run continues the checkpoint
        of the run it resumes."""
        from esfex.io.checkpoint import checkpoint_path

        self._ckpt = None
        master_cfg = getattr(self.config, "master_problem", None)
        case = getattr(self, "_case_fingerprint", None)
        if (master_only or case is None or self.config.simulation_mode != "development"
                or getattr(master_cfg, "planning_mode", None) != "myopic"):
            return
        now = datetime.now().isoformat(timespec="seconds")
        plan = getattr(self, "resume_plan", None)
        if plan is not None:
            ckpt = {k: deepcopy(v) for k, v in plan.doc.items() if k != "state"}
            ckpt["case"] = case
            ckpt["resumes"] = list(ckpt.get("resumes") or []) + [
                {"time": now, "from_year": plan.resume_year, "phase": plan.phase,
                 "forced": list(plan.forced)}]
        else:
            ckpt = {
                "status": "running", "phase": PHASE_MASTER, "created": now,
                "results_file": hdf5_path.name,
                "config_file": str(Path(self.config_path).resolve()) if self.config_path else "",
                "case": case,
                "run": {"years": len(years_range), "start_year": years_range[0],
                        "dispatch_years": sorted(ops_only) if ops_only is not None else None},
                "master": {"completed_years": []},
                "dispatch": {"completed": []},
            }
        self._ckpt = ckpt
        self._ckpt_path = checkpoint_path(hdf5_path)
        self._ckpt_dispatch_start = min(ops_only) if ops_only else years_range[0]
        try:
            import h5py

            with h5py.File(hdf5_path, "a") as f:
                f.attrs["checkpoint"] = self._ckpt_path.name
        except Exception as e:
            logger.debug(f"Could not name the checkpoint in {hdf5_path}: {e}")
        if plan is None:
            self._write_checkpoint(PHASE_MASTER, years_range[0])

    def _write_checkpoint(self, phase: str, resume_year: Optional[int], *,
                          completed: Optional["YearResults"] = None,
                          master_year: Optional[int] = None,
                          master_state: Optional[dict] = None) -> None:
        """Rewrite the run's checkpoint (nothing when it keeps none): *phase*
        reached, the year a resume starts at (*resume_year*; None after the
        last master year: the first dispatch year; after the last dispatched
        year: none left), the master year or
        dispatched year (*completed*) just finished, and the state the next
        year needs: the myopic master loop's (*master_state*) in phase
        ``master``, the master plan and the dispatch carry-over in phase
        ``dispatch``, none once complete. A failed write is logged and the
        run goes on: a resume then repeats the years after the last
        checkpoint written."""
        ckpt = getattr(self, "_ckpt", None)
        if ckpt is None:
            return
        from esfex.api.diagnostics import active_collector
        from esfex.io.checkpoint import write_checkpoint

        if master_year is not None:
            ckpt["master"]["completed_years"].append(int(master_year))
        if completed is not None:
            ckpt["dispatch"]["completed"].append({
                "year": int(completed.year), "feasible": bool(completed.feasible),
                "objective": float(completed.objective)})
        done = ckpt["dispatch"]["completed"]
        ckpt["phase"] = phase
        ckpt["status"] = "complete" if phase == PHASE_COMPLETE else "running"
        ckpt["last_completed_year"] = done[-1]["year"] if done else None
        if resume_year is not None:
            ckpt["resume_at"] = {"phase": phase, "year": int(resume_year)}
        elif phase == PHASE_MASTER:
            ckpt["resume_at"] = {"phase": PHASE_DISPATCH, "year": self._ckpt_dispatch_start}
        else:
            ckpt["resume_at"] = None
        collector = active_collector()
        ckpt["warnings"] = collector.summary().to_dict() if collector is not None else None
        if phase == PHASE_DISPATCH:
            state = self._dispatch_checkpoint_state()
        elif phase == PHASE_COMPLETE:
            state = None
        else:
            state = {"master": master_state} if master_state is not None else None
        try:
            write_checkpoint(self._ckpt_path, dict(ckpt, state=state))
        except Exception as e:  # never fail a run over its checkpoint
            logger.warning(f"Checkpoint not written ({type(e).__name__}: {e}); a resumed "
                           f"run would repeat the years after the previous one")

    def _dispatch_checkpoint_state(self) -> dict:
        """What the dispatch of the remaining years needs: the master plan
        of every year and what the master recorded besides (fuel capacity,
        NPV retirements, yearly costs, diagnostics), and the carry-over of
        the last dispatched year (``state.boundary_conditions``: fuel tank
        levels and fuel in transit; the unit-indexed entries are reset at
        each year's start)."""
        investments, retirements, caps, re_targets = self._master_plan
        return {
            "master_plan": {"investments": investments, "retirements": retirements,
                            "cumulative_caps": caps, "re_targets": re_targets},
            "master_fuel_by_year": getattr(self, "_master_fuel_by_year", None) or {},
            "npv_retirements": self._npv_retirements,
            "myopic_year_costs": self.myopic_year_costs,
            "master_diagnostics": self.master_diagnostics,
            "boundary_conditions": self.state.boundary_conditions if self.state else {},
            "primary_energy_capacities": (self.state.primary_energy_capacities
                                          if self.state else {}),
        }

    def _restore_master_plan(self, state: dict) -> tuple:
        """The master plan a dispatch-phase checkpoint holds, with what the
        master recorded besides restored on ``self``: ``(investments,
        retirements, cumulative capacities, RE targets)`` by year."""
        mp = state["master_plan"]
        self._master_fuel_by_year = state.get("master_fuel_by_year") or {}
        self._npv_retirements = list(state.get("npv_retirements") or [])
        self.myopic_year_costs = state.get("myopic_year_costs") or {}
        self.master_diagnostics = state.get("master_diagnostics") or {}
        self._master_plan = (mp["investments"], mp["retirements"],
                             mp["cumulative_caps"], mp["re_targets"])
        return self._master_plan

    def _restore_dispatch_state(self, state: dict) -> None:
        """The carry-over of the last year a dispatch-phase checkpoint lists."""
        self.state.boundary_conditions = state.get("boundary_conditions") or {}
        pe = state.get("primary_energy_capacities")
        if pe:
            self.state.primary_energy_capacities = pe

    def _write_npv_retirements(self, path: Optional[Path]) -> None:
        """Write the NPV-based retirements to the results file, group
        ``npv_retirements`` (only when the system's ``npv_retirement`` is on):
        one entry per retired unit location with its kind, key, name, index
        (0-based, in the system's generators or batteries, as the
        ``retirement_gen_{index}`` attributes), node, the year it is out of
        service from, its NPV ($), the round that retired it, its configured
        MW there and that MW's share of the unit."""
        if path is None or not getattr(self.master_system, "npv_retirement", False):
            return
        import h5py

        recs = self._npv_retirements
        with h5py.File(path, "a") as f:
            if "npv_retirements" in f:
                del f["npv_retirements"]
            grp = f.create_group("npv_retirements")
            grp.attrs["description"] = (
                "Existing units retired for the whole horizon because their NPV "
                "in the master's plan was below npv_retirement_threshold")
            grp.attrs["threshold"] = float(getattr(
                self.master_system, "npv_retirement_threshold", 0.0))
            grp.attrs["max_iterations"] = int(getattr(
                self.master_system, "npv_retirement_max_iterations", 5))
            str_dt = h5py.string_dtype()
            for col in ("kind", "key", "name"):
                grp.create_dataset(col, data=[str(r[col]) for r in recs], dtype=str_dt)
            for col in ("index", "node", "year", "iteration"):
                grp.create_dataset(col, data=np.array([int(r[col]) for r in recs], dtype=np.int64))
            for col in ("npv", "capacity_mw", "share"):
                grp.create_dataset(col, data=np.array([float(r[col]) for r in recs], dtype=float))
            grp["npv"].attrs["units"] = "$"
            grp["capacity_mw"].attrs["units"] = "MW"

    def _retired_share(self, gen_idx: int, flags, year: Optional[int] = None) -> float:
        """Share of existing master generator *gen_idx* (0-based) retired.

        *flags* is its share in service per master location (1 within its
        lifetime; past it, the share the master keeps by life extension, 0
        when it retires it), per bus on a bus-level master. With *year*, the
        nodes where the NPV-based retirement retired it by then
        (``self._npv_retirements``) are retired in full. Each
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
        if year is not None:
            for r in self._npv_retirements:
                if (r["kind"] == "generator" and r["index"] == gen_idx
                        and r["year"] <= year and 0 <= r["node"] < n):
                    retired[r["node"]] = 1.0
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
            # (life_ext is the share in service: 1 = kept, 0 = retired; the kept
            # share never grows again)
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

    @staticmethod
    def _warn_operating_budget_excess(year: int, budget: float, breakdown: dict,
                                      load_shed_mwh: float) -> None:
        """One warning when the year's dispatch spent more than its operating
        budget (``max_annual_operating_cost``): the excess, its penalty and
        the year's unserved energy."""
        excess = breakdown.get("operating_budget_excess", 0.0)
        if excess <= 1e-6 * max(budget, 1.0):
            return
        logger.warning(
            f"Year {year}: the dispatch spent {excess / 1e6:,.3f} M$ above "
            f"max_annual_operating_cost ({budget / 1e6:,.3f} M$), penalised "
            f"{breakdown.get('operating_budget_penalty', 0.0) / 1e6:,.3f} M$; "
            f"unserved energy {load_shed_mwh:,.1f} MWh. The budget does not cover "
            "the fuel, maintenance and fixed O&M the year needs.")

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
            re_penetration_target: Year-specific RE target (0-1), from the
                development master's trajectory. None or 0: no RE-penetration
                constraint (operations modes).

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
        all_unattached_shed = []
        all_co2_emissions = []
        all_prices = []
        all_voltage_angle = []
        all_voltage_magnitude = []
        all_reactive_generation = []
        all_power_flow = []
        all_line_losses = []
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
        total_objective = 0.0
        total_solve_time = 0.0
        total_load_shed = 0.0
        total_curtailment = 0.0
        total_re_generation = 0.0
        total_emissions = 0.0
        total_generation = 0.0
        total_demand_val = 0.0
        total_losses = 0.0
        window_loss_repairs: list[dict] = []
        accumulated_cost_breakdown: dict[str, float] = {}
        feasible = True
        num_solved = 0

        # Annual operating budget (max_annual_operating_cost, $/yr): each
        # window gets what is left of it in proportion to its committed
        # steps' demand, and what those steps spent is carried
        op_budget = getattr(self.primary_system, 'max_annual_operating_cost', None)
        op_budget_left = op_budget
        # Demand of the year from each step on (MW, summed over nodes)
        step_demand = np.asarray(demand, dtype=float).reshape(len(demand), -1).sum(axis=1)
        demand_from = np.concatenate([np.cumsum(step_demand[::-1])[::-1], [0.0]])

        boundary_conditions = self.state.boundary_conditions.copy()
        # Battery / generator / reservoir rosters can change between years
        # (new investments, retirements), so per-unit indexing from the prior
        # year cannot be safely reused. Reset all unit-indexed carry-over at
        # year boundary; chaining still works within the year across rolling
        # windows. PE fuel storage stays linked (named by fuel, not index).
        boundary_conditions.pop('battery_soc', None)
        boundary_conditions.pop('gen_status_init', None)
        boundary_conditions.pop('gen_output_prev', None)
        boundary_conditions.pop('battery_discharge_prev', None)
        boundary_conditions.pop('battery_charge_prev', None)
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

            window_budget = None
            if op_budget is not None:
                kept_end = min(start_hour + keep_hours, hours)
                window_budget = window_operating_budget(
                    op_budget_left, demand_from[start_hour] - demand_from[kept_end],
                    demand_from[start_hour])

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
                operating_budget=window_budget,
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
            # The window's renewable generation: its RE share × its
            # generation, both over the committed steps
            total_re_generation += (window_result.get("re_penetration", 0.0)
                                    * window_result.get("total_generation", 0.0))
            total_objective += window_result.get("objective", 0.0)
            total_load_shed += window_result.get("load_shed_total", 0.0)
            total_curtailment += window_result.get("total_curtailment", 0.0)
            total_emissions += window_result.get("total_co2", 0.0)
            total_generation += window_result.get("total_generation", 0.0)
            total_demand_val += window_result.get("total_demand", 0.0)
            total_losses += window_result.get("total_losses", 0.0) or 0.0
            if isinstance(window_result.get("loss_repair"), dict):
                window_loss_repairs.append(window_result["loss_repair"])

            wbd = window_result.get("cost_breakdown")
            if wbd:
                for k, v in wbd.items():
                    accumulated_cost_breakdown[k] = accumulated_cost_breakdown.get(k, 0.0) + v
            if op_budget_left is not None:
                # What the window spent within its budget: the excess is
                # already penalised and leaves the later windows' share alone
                op_budget_left -= (operating_spend(wbd)
                                   - (wbd or {}).get("operating_budget_excess", 0.0))

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
            if window_result.get("line_losses") is not None:
                all_line_losses.append(_trim(window_result["line_losses"], keep_hours))
            if window_result.get("unattached_load_shed") is not None:
                all_unattached_shed.append(_trim(window_result["unattached_load_shed"], keep_hours))
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
            # Over the committed steps, like the objective (extract_solution)
            total_n1_security_cost += window_result.get("n1_security_cost") or 0.0

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

        if op_budget is not None:
            self._warn_operating_budget_excess(year, op_budget, accumulated_cost_breakdown,
                                               total_load_shed)

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
        line_losses_arr = np.concatenate(all_line_losses, axis=-1) if all_line_losses else None
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

        # The year's RE share, energy-weighted: renewable over total
        # generation of the committed steps (not a mean of the windows')
        avg_re = re_share(total_re_generation, total_generation)

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
            line_losses_arr = self._aggregate_buses_to_nodes(line_losses_arr, "sum")
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
            # Units online: SUM across buses (a plant the master built is
            # spread over the node's buses, each piece with its own units)
            gen_status = self._aggregate_buses_to_nodes(gen_status, "sum")
            # Prices: MAX across buses (highest marginal price)
            prices = self._aggregate_buses_to_nodes(prices, "max")
            voltage_angle = self._aggregate_buses_to_nodes(voltage_angle, "max")

        # Demand of nodes without a load bus: in no power balance, so it is
        # reported as unserved load at its node (the adapter already added it
        # to load_shed_total, the cost breakdown and the objective).
        if all_unattached_shed and len(all_unattached_shed) == len(all_load_shed):
            unattached = np.concatenate(all_unattached_shed, axis=-1)
            if load_shed_arr is not None and load_shed_arr.shape == unattached.shape:
                load_shed_arr = load_shed_arr + unattached

        logger.debug(f"Year {year} dispatch: obj=${total_objective:,.0f}, "
                      f"RE={avg_re:.1%}, CO2={total_emissions:.0f}t, time={total_solve_time:.1f}s")
        dispatch_loss_repair = sum_loss_repairs(window_loss_repairs)
        if dispatch_loss_repair and dispatch_loss_repair["windows_repaired"]:
            r = dispatch_loss_repair
            logger.log(
                logging.INFO if r["gap"] > LOSS_REPAIR_GAP_INFO else logging.DEBUG,
                f"Year {year} dispatch line losses: PWL solves ${r['lower_bound']:,.0f} "
                f"(lower bound), repaired ${r['objective']:,.0f} (gap {r['gap']:.2%}, "
                f"largest window {r['gap_max']:.2%}; {r['windows_repaired']} of "
                f"{r['windows']} windows repaired, {r['released']} line-step(s) released)")

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
            line_losses=line_losses_arr,
            voltage_angle=voltage_angle,
            voltage_magnitude=voltage_magnitude,
            reactive_generation=reactive_generation,
            prices=prices,
            demand=demand,
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
            electrolyzer_power=electrolyzer_power,
            electrolyzer_h2=electrolyzer_h2,
            primary_energy=pe_merged,
            fuel_for_power=(pe_merged or {}).get("fuel_for_power") or None,
            load_shed=total_load_shed,
            re_penetration=avg_re,
            emissions=total_emissions,
            total_generation=total_generation,
            total_demand=total_demand_val,
            transmission_losses=total_losses,
            dispatch_loss_repair=dispatch_loss_repair,
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
                fuel=vdata.get("fuel", "None"),
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
                # The fuel price of this year, as the dispatch charged it
                growth = fuel_price_factor(sys_fuels, getattr(gen, 'fuel', None), result.year,
                                           self._fuel_price_base_year(result.year))
                for n in range(min(n_nodes, len(gen.fuel_cost))):
                    output = result.gen_output[g, n, :]
                    mask = output > 1e-6
                    if not np.any(mask):
                        continue
                    fc = gen.fuel_cost[n]
                    if fc <= 0:
                        fc = _fuel_based_cost(gen, n)
                    fc *= growth
                    mc = gen.maintenance_cost[n]
                    # Fixed O&M ($/MW·yr) of the rated power over these
                    # hours, spread over the energy produced in them
                    rated = gen.rated_power[n] if n < len(gen.rated_power) else 0.0
                    fxc = (gen.fixed_cost[n] * rated * hours
                           / HOURS_STD_YEAR / float(output.sum()))
                    total_cost = (fc + fxc + mc) * output
                    # Add startup cost if available
                    # (one of num_units units costs start_up_cost / num_units)
                    if result.gen_startup is not None and g < result.gen_startup.shape[0]:
                        su = gen.start_up_cost[n] if n < len(gen.start_up_cost) else 0.0
                        units = (self._gen_num_units[g] if g < len(self._gen_num_units)
                                 else [])
                        su /= max(1, units[n] if n < len(units) else 1)
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
                'transport_transit_days_per_100km': transit[fuel],
                'disruption_start_hour': disrupt[fuel]["start"],
                'disruption_end_hour': disrupt[fuel]["end"],
                'disruption_availability': disrupt[fuel]["avail"],
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
        operating_budget: Optional[float] = None,
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
            operating_budget: The window's operating budget ($) for its
                committed steps (``window_operating_budget``); None: no budget.
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
                # N-1 apply_to_modes lists simulation modes, not ps_mode
                run_mode=self.config.simulation_mode,
                availability_cache=self._availability_cache,
                inflow_cache=getattr(self, "_inflow_cache", None),
                start_hour=start_hour,
                # Names the window in the solver warnings
                window=window,
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
                        # The run's mode: a development run ends each window
                        # with its tanks at least 10 % full
                        mode=self.config.simulation_mode,
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

            # The window's share of the year's operating budget, over its
            # committed steps (the objective is complete now, PE included)
            if operating_budget is not None:
                ps.add_operating_budget(
                    operating_budget, keep_steps=min(keep_hours, hours) if keep_hours else hours,
                    primary_energy=pe)

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

            # Battery ramp continuity: charge and discharge of the last
            # committed step (battery × bus)
            for _src, _dst in (('bat_discharge', 'battery_discharge_prev'),
                               ('bat_charge', 'battery_charge_prev')):
                _arr = solution.get(_src)
                if _arr is not None and hasattr(_arr, 'ndim') and _arr.ndim == 3:
                    new_boundary[_dst] = _state_at(_arr)

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
                # Bounds of the window's line-loss repair (whole window,
                # look-ahead included), or None
                "loss_repair": getattr(ps, "loss_repair", None),
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
                # Demand of nodes without a load bus, unserved [node x hour]
                "unattached_load_shed": solution.get('unattached_load_shed'),
                "co2_emissions": solution.get('co2_emissions'),
                # Network
                "energy_prices": solution.get('energy_prices'),
                "power_flow": solution.get('power_flow'),
                "power_flow_by_line": solution.get('power_flow_by_line'),
                "line_losses": solution.get('line_losses'),
                "voltage_angle": solution.get('voltage_angle'),
                "voltage_magnitude": solution.get('voltage_magnitude'),
                "reactive_generation": solution.get('reactive_generation'),
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
                # N-1 security: duals, binding contingencies, security cost
                "n1_gen_reserve_duals": solution.get('n1_gen_reserve_duals'),
                "n1_trans_reserve_duals": solution.get('n1_trans_reserve_duals'),
                "n1_binding_contingencies": solution.get('n1_binding_contingencies'),
                "n1_security_cost": solution.get('n1_security_cost', 0.0),
                # Scalars
                "load_shed_total": solution.get('load_shed_total', 0.0),
                "total_curtailment": solution.get('total_curtailment', 0.0),
                "re_penetration": solution.get('re_penetration', 0.0),
                "total_co2": solution.get('total_co2', 0.0),
                "total_generation": solution.get('total_generation', 0.0),
                "total_demand": solution.get('total_demand', 0.0),
                "total_losses": solution.get('total_losses', 0.0),
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

            # Energy balance of the committed steps (MWh), with the terms of
            # the power balance. Reserves are headroom, not energy, so they
            # are not in it; transmission losses are reported on their own.
            tres = max(1.0, float(getattr(self.config.temporal, 'resolution_hours', 1) or 1))

            def _mwh(arr):
                if arr is None:
                    return 0.0
                a = np.asarray(arr, dtype=float)
                return float(np.nansum(a[..., :k])) * tres if a.ndim else 0.0

            gen_sum = _mwh(solution.get('gen_output'))
            ls_sum = (_mwh(solution.get('load_shed'))
                      + _mwh(solution.get('unattached_load_shed')))
            bd_sum = _mwh(solution.get('bat_discharge'))
            bc_sum = _mwh(solution.get('bat_charge'))
            ev_ch_sum = _mwh(solution.get('ev_charging'))
            ev_v2g_sum = _mwh(solution.get('ev_v2g'))
            elz_sum = _mwh(solution.get('electrolyzer_power'))
            pump_sum = _mwh(solution.get('reservoir_pump'))
            dem_sum = (float(np.nansum(np.asarray(demand, dtype=float)[:k])) * tres
                       if demand is not None else 0.0)
            rooftop_sum = (float(np.nansum(np.asarray(rooftop_generation, dtype=float)[:k])) * tres
                           if rooftop_generation is not None else 0.0)
            loss_sum = float(solution.get('total_losses', 0.0) or 0.0)

            supply = gen_sum + bd_sum + ev_v2g_sum + ls_sum + rooftop_sum
            consumption = dem_sum + bc_sum + ev_ch_sum + elz_sum + pump_sum + loss_sum

            gap = supply - consumption
            gap_pct = abs(gap) / consumption * 100 if consumption > 0 else 0
            loss_pct = loss_sum / supply * 100 if supply > 0 else 0

            # Concise balance line for every window (matches Julia @info)
            logger.debug(
                f"Window {window} solved: obj=${result['objective']:,.0f} | "
                f"energy balance (MWh): supply={supply:.1f} use={consumption:.1f} "
                f"gap={gap:.1f} ({gap_pct:.2f}%) | losses={loss_sum:.1f} ({loss_pct:.2f}%) | "
                f"gen={gen_sum:.1f} ls={ls_sum:.1f} bat={bd_sum - bc_sum:.1f} ev={ev_v2g_sum - ev_ch_sum:.1f}"
            )

            # Detailed balance for first 3 windows per year
            if window < 3:
                logger.debug(
                    f"  BALANCE W{window} detail (MWh): "
                    f"gen={gen_sum:.1f} bat_dch={bd_sum:.1f} ev_v2g={ev_v2g_sum:.1f} "
                    f"ls={ls_sum:.1f} rooftop={rooftop_sum:.1f} | "
                    f"dem={dem_sum:.1f} bat_ch={bc_sum:.1f} ev_ch={ev_ch_sum:.1f} "
                    f"electrolyzer={elz_sum:.1f} pump={pump_sum:.1f} losses={loss_sum:.1f}"
                )
                # Log per-generator output
                go = solution.get('gen_output')
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

    def _rebuild_unit_names(self, units_config: dict[str, Any],
                            year_offset: int = 0) -> None:
        """Rebuild generator/battery name lists from units_config.

        Must replicate the EXACT same ordering and filtering as the
        PowerSystemAdapter._create_input() method, so that name indices
        match the Julia solver output indices.

        Order:
          1. Original generators (from sys.generators)
          2. Virtual generators (from units_config, not in sys.generators)
          3. Original batteries (from sys.batteries)
          4. Virtual batteries (from units_config, not in sys.batteries)

        Also records each unit's MW in service per node in the dispatch's
        year (*year_offset* = years since the base year), for the export:
        the units the master built have no config in the results file, and
        the configured ``rated_power`` of an existing unit ignores its
        retirement, life extension and degradation.
        """
        sys = self.primary_system
        num_nodes = self._num_nodes

        def existing(key, unit, power_key, degrade):
            data = units_config.get(key) or unit.model_dump()
            return existing_power_in_service(data, power_key, num_nodes,
                                             year_offset, degrade=degrade)

        # --- Generator names + parallel fuel list ---
        # _gen_fuels mirrors _gen_names index-for-index so the export
        # can stamp each generation dataset with ``attrs["fuel"]`` and
        # the dashboard can bucket by fuel without parsing plant names.
        gen_names: list[str] = []
        gen_fuels: list[str] = []
        gen_renewable: list[bool] = []
        gen_in_service: list = []
        # Units per node (clustered unit commitment): what gen_status counts
        gen_units: list = []
        # Committed in unit commitment (see is_committed_unit)
        gen_committed: list[bool] = []
        # 1) Original generators (same order as sys.generators iteration)
        for key, gen in sys.generators.items():
            gen_names.append(gen.name if hasattr(gen, 'name') else key)
            gen_fuels.append(str(getattr(gen, 'fuel', '') or ''))
            gen_renewable.append(gen.type == "Renewable")
            gen_committed.append(is_committed_unit(
                gen.type, gen.fuel, getattr(gen, 'reservoir_capacity', None)))
            gen_in_service.append(existing(key, gen, "rated_power", True))
            data = units_config.get(key) or {}
            gen_units.append([int(n) for n in _per_node_list(
                data.get("num_units") or getattr(gen, "num_units", None) or [1],
                num_nodes, 1)])

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
            gen_renewable.append(vdata.get("type") == "Renewable")
            gen_committed.append(is_committed_unit(
                vdata.get("type", ""), vdata.get("fuel", ""),
                vdata.get("reservoir_capacity")))
            gen_in_service.append(self._unit_power_by_node(vdata, rp))
            # Units at the buses that hold capacity, summed per node
            units = _per_node_list(vdata.get("num_units") or [1], len(rp), 1)
            gen_units.append([int(round(v)) for v in self._unit_power_by_node(
                vdata, [u if p > 0 else 0 for u, p in zip(units, rp)])])

        # --- Battery names ---
        bat_names = []
        bat_in_service: list = []
        # 1) Original batteries (same order as sys.batteries iteration);
        # their fixed O&M is on the rated (not degraded) discharge power
        for key, bat in sys.batteries.items():
            bat_names.append(bat.name if hasattr(bat, 'name') else key)
            bat_in_service.append(existing(key, bat, "MaxDischargePower", False))

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
            # Fixed O&M is on the rated power (as for the original
            # batteries): a built battery's power is degraded by the master
            # (_build_config_from_cumulative keeps the rated power)
            bat_in_service.append(self._unit_power_by_node(
                vdata, vdata.get("_rated_discharge_power")
                or vdata.get("MaxDischargePower", charge_pow)))

        # Homonyms get a " (N)" suffix (HDF5 needs unique dataset names);
        # order and length are kept, so the fuel list stays index-aligned.
        self._gen_names = unique_unit_names(gen_names)
        self._gen_fuels = list(gen_fuels)
        self._gen_renewable = gen_renewable
        self._gen_committed = gen_committed
        self._gen_in_service_mw = gen_in_service
        self._gen_num_units = gen_units
        self._bat_names = unique_unit_names(bat_names)
        self._bat_in_service_mw = bat_in_service

    def _unit_power_by_node(self, vdata: dict, power) -> list[float]:
        """Per-node MW of a virtual unit's ``power`` list, which is per bus
        when the unit carries ``_is_per_bus`` (summed onto the buses' parent
        nodes) and per node otherwise."""
        num_nodes = self._num_nodes
        values = [float(v) for v in (power or [])]
        buses = self.primary_system.buses or []
        if vdata.get("_is_per_bus") and len(values) == len(buses):
            from esfex.bridge.converters import node_bus_mapping_required
            if node_bus_mapping_required(buses, num_nodes):
                out = [0.0] * num_nodes
                for b, v in zip(buses, values):
                    if 0 <= b.parent_node < num_nodes:
                        out[b.parent_node] += v
                return out
        return (values + [0.0] * num_nodes)[:num_nodes]

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
        year_offset: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        Build units config from MasterProblem's cumulative capacities.

        Uses the MasterProblem's correctly computed cumulative capacity which
        accounts for:
        - Existing units: age-based retirement + degradation, and the
          capacity the master keeps past the lifetime (life extension)
        - Investments: each with its own age tracking (NOT retired when
          the original unit retires)

        The dispatch retires a unit at the end of its ``life_time``; the
        units whose capacity comes from the master get no end of life there
        (the master already decided it).

        Args:
            base_units_config: Original units config (for structure/metadata)
            year_caps: {'gen': {julia_g_idx: np.array(per_bus)},
                        'bat': {julia_b_idx: np.array(per_bus)},
                        'bat_power': {julia_b_idx: np.array(per_bus)},
                        'tech' / 'bat_tech_power' / 'bat_tech_capacity':
                            {julia_t_idx: np.array(per_bus)}, in service
                            (each build degraded by its age),
                        'bat_tech_power_rated' (optional): the battery
                            power undegraded, its fixed O&M base}
            year_offset: Years since the first planning year (the dispatch's
                ``year - base_year``). With it, an existing battery past its
                lifetime gets the power and energy the master keeps.
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
        for g_jl, bus_caps in gen_caps.items():
            py_idx = int(g_jl) - 1  # Julia 1-based → Python 0-based
            unit_key = gen_idx_to_key.get(py_idx)
            if unit_key and unit_key in updated:
                # Per master location (bus or node): per node here
                node_caps = self._master_values_by_node(bus_caps, num_nodes)
                updated[unit_key]["rated_power"] = node_caps.tolist()
                # Whole units of the original size left of that capacity
                base = base_units_config[unit_key]
                units = _per_node_list(base.get("num_units") or [1], num_nodes, 1)
                rated0 = _per_node_list(base.get("rated_power") or [], num_nodes)
                deg = _per_node_list(base.get("degradation_rate") or [], num_nodes)
                age0 = _per_node_list(base.get("initial_age") or [], num_nodes, 0)
                updated[unit_key]["num_units"] = [
                    units_in_service(units[n], float(rated0[n]), float(node_caps[n]),
                                     float(deg[n]), float(age0[n]) + (year_offset or 0))
                    for n in range(num_nodes)]
                # Prevent double degradation/retirement in power_system.jl:
                # the master's capacity already holds both (and the life
                # extension), so no degradation and no end of life here
                updated[unit_key]["degradation_rate"] = [0.0] * num_nodes
                updated[unit_key]["initial_age"] = [0] * num_nodes
                updated[unit_key]["life_time"] = [999] * num_nodes

        if year_offset is not None:
            self._apply_master_battery_life(updated, year_caps, year_offset,
                                            bus_to_node, num_nodes)

        # Reservoir capacity the master built up to this year (MWh per node,
        # see _add_reservoir_caps) is existing capacity for the dispatch
        for (g, node), mwh in year_caps.get('reservoir', {}).items():
            unit_key = gen_idx_to_key.get(int(g))
            if unit_key in updated and 0 <= node < num_nodes:
                cap = _pad_zeros(updated[unit_key].get("reservoir_capacity"), num_nodes)
                cap[node] += float(mwh)
                updated[unit_key]["reservoir_capacity"] = cap

        # Existing batteries within their lifetime keep the dispatch's own
        # degradation (the master degrades them alike); past it they hold
        # what the master keeps (_apply_master_battery_life).

        # Create virtual generators from technology investments
        # Keep per-bus arrays so virtual generators inject at the correct buses
        # (where the master problem decided to invest).
        tech_caps = year_caps.get('tech', {})
        tech_keys = list(self.primary_system.technologies.keys())

        # Master granularity (_master_on_buses, not the array length): a
        # node-level master (the merged master network, one bus per node)
        # returns per-node arrays, laid out below on the bus-level
        # operational network; a bus-level master (the master solves on the
        # operational buses themselves) returns per-bus arrays.
        master_per_node = bus_to_node is not None and not self._master_on_buses()
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

                if max(bus_rated) <= MIN_BUILD:
                    continue

                # The technology's per-node properties, per bus
                def _rep(arr, default=0.0):
                    return _node_values_on_buses(arr, num_buses, bus_to_node, default)

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
                    # Units of the technology's unit_size at each bus
                    "num_units": [units_of_capacity(float(p), float(u)) for p, u in
                                  zip(bus_rated, _rep(tech.unit_size, 0.0))],
                    "Availability": tech.availability_file,
                    # Already degraded by master problem — prevent double degradation
                    "degradation_rate": [0.0] * num_buses,
                    "initial_age": [0] * num_buses,
                    "life_time": [999] * num_buses,
                    "invest_cost": [0.0] * num_buses,
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
        bat_tech_rated_caps = year_caps.get('bat_tech_power_rated', {})
        bat_tech_keys = list(self.primary_system.battery_technologies.keys())
        bat_master_per_node = bus_to_node is not None and not self._master_on_buses()
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

                if max(bus_pow) <= MIN_BUILD and max(bus_cap) <= MIN_BUILD:
                    continue

                vbat_key = f"inv_{bt_key}"
                updated[vbat_key] = {
                    **battery_technology_unit_fields(
                        bat_tech, lambda arr, default: _node_values_on_buses(
                            arr, num_buses, bus_to_node, default)),
                    "capacity": bus_cap,
                    "MaxChargePower": bus_pow,
                    "MaxDischargePower": bus_pow,
                    # Already degraded by the master (each build by its age)
                    "degradation_rate": [0.0] * num_buses,
                    "initial_age": [0] * num_buses,
                    "life_time": [999] * num_buses,
                    "invest_cost": [0.0] * num_buses,
                    "invest_max_power": [0.0] * num_buses,
                    "decommissioning_cost": [0.0] * num_buses,
                    "_is_per_bus": True,  # Flag: arrays are already per-bus
                }
                rated = bat_tech_rated_caps.get(bt_jl)
                if rated is not None:
                    # Fixed O&M on the rated power built, as the master
                    # charges it: the power above is degraded, so its
                    # fixed_cost is scaled by rated / degraded at each
                    # master location (a bus's node on a node-level master)
                    rated = np.asarray(rated, dtype=float)

                    def _om_scale(b_idx):
                        loc = bus_to_node[b_idx] if bat_master_per_node else b_idx
                        deg = float(pow_arr[loc]) if loc < len(pow_arr) else 0.0
                        rat = float(rated[loc]) if loc < len(rated) else 0.0
                        return rat / deg if deg > 0 and rat > 0 else 1.0

                    unit = updated[vbat_key]
                    unit["fixed_cost"] = [float(fc) * _om_scale(b_idx)
                                          for b_idx, fc in enumerate(unit["fixed_cost"])]
                    # The rated power per bus, recorded in the results as
                    # the battery's power for fixed O&M (_rebuild_unit_names)
                    unit["_rated_discharge_power"] = [
                        float(p) * _om_scale(b_idx) for b_idx, p in enumerate(bus_pow)]
                # DEBUG: emitted per bus → noisy at INFO. The aggregate
                # "Year X investments" log a few lines up covers the
                # milestone view.
                logger.debug(f"Virtual battery: {vbat_key} ({bat_tech.name}) → pow={bus_pow} MW, cap={bus_cap} MWh (per-bus)")

        return updated

    def _apply_master_battery_life(
        self,
        updated: dict[str, Any],
        year_caps: dict[str, dict],
        year_offset: int,
        bus_to_node: Optional[list[int]],
        num_nodes: int,
    ) -> None:
        """Existing batteries past their lifetime: the master's life extension.

        At a node where the battery's age (``initial_age`` + *year_offset*)
        has reached its ``life_time``, the master keeps
        ``year_caps['bat_power']`` MW and ``year_caps['bat']`` MWh (0 when it
        retires it). The dispatch would retire the battery there, so the node
        gets that capacity, with no degradation and no end of life. Nodes
        within the lifetime are left to the dispatch's own ageing.
        """
        pow_caps = year_caps.get('bat_power', {})
        cap_caps = year_caps.get('bat', {})
        if not pow_caps:
            return

        def to_nodes(arr) -> np.ndarray:
            return self._master_values_by_node(arr, num_nodes)

        bat_keys = [k for k, d in updated.items()
                    if d.get("_type") == "battery" or d.get("type") == "Storage"]
        for b_jl, bus_pow in pow_caps.items():
            py_idx = int(b_jl) - 1
            if not 0 <= py_idx < len(bat_keys):
                continue
            unit = updated[bat_keys[py_idx]]
            if unit.get("_is_per_bus"):
                continue
            ages = _per_node_list(unit.get("initial_age", [0]), num_nodes)
            lives = _per_node_list(unit.get("life_time", [999]), num_nodes)
            kept_pow = to_nodes(bus_pow)
            kept_cap = to_nodes(cap_caps.get(b_jl, np.zeros(len(bus_pow))))
            fields = {f: [float(x) for x in _per_node_list(unit.get(f, [0.0]), num_nodes)]
                      for f in ("MaxChargePower", "MaxDischargePower", "capacity",
                                "rated_power", "degradation_rate")}
            ages, lives = list(ages), list(lives)
            changed = False
            for n in range(num_nodes):
                base = fields["MaxDischargePower"][n]
                if base <= 0 or float(ages[n]) + year_offset < float(lives[n]):
                    continue
                share = min(1.0, kept_pow[n] / base)
                fields["MaxChargePower"][n] *= share
                fields["MaxDischargePower"][n] = float(kept_pow[n])
                fields["rated_power"][n] *= share
                fields["capacity"][n] = float(kept_cap[n])
                fields["degradation_rate"][n] = 0.0
                ages[n], lives[n] = 0, 999
                changed = True
            if changed:
                unit.update(fields)
                unit["initial_age"] = ages
                unit["life_time"] = lives

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
            if self.config.simulation_mode == "development":
                f.attrs["planning_mode"] = str(getattr(
                    self.config.master_problem, "planning_mode", "perfect_foresight"))
            f.attrs["years_range"] = f"{start_year}-{end_year - 1}"
            # What produced the file, for readers such as `esfex summary`
            try:
                from esfex import __version__ as _esfex_version
            except ImportError:
                _esfex_version = "unknown"
            f.attrs["esfex_version"] = str(_esfex_version)
            f.attrs["solver"] = str(self.config.solver.name)
            f.attrs["config_file"] = (str(Path(self.config_path).resolve())
                                      if self.config_path else "")
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
                    # Operating costs of the units the master builds from it
                    # (no generator config of their own): read by the results
                    # dashboard and the financial analysis
                    for k in _TECH_COST_ATTRS:
                        tg.attrs[k] = str(list(getattr(tech, k)))
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
                    for k in _BAT_TECH_COST_ATTRS:
                        btg.attrs[k] = str(list(getattr(bt, k)))
                    if bt.color:
                        btg.attrs["color"] = bt.color

            # Fuel price growth of the fuels block: the dispatch charges a
            # unit's fuel_cost × (1 + rate)^(year − base_year) (see
            # fuel_price_factor); the financial analysis does the same
            fg_group = config_group.create_group("fuel_price_growth")
            fg_group.attrs["base_year"] = self._fuel_price_base_year(start_year)
            for fuel_name, fuel in (getattr(self.primary_system, "fuels", None) or {}).items():
                fg_group.attrs[str(fuel_name)] = float(fuel.price_growth_rate or 0.0)

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
                "transmission_losses": result.transmission_losses,
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
            scenario.attrs["transmission_losses"] = result.transmission_losses
            scenario.attrs["master_re_target"] = result.master_re_target
            # Line-loss repair bounds: the first (PWL) solve, a lower bound,
            # and the repaired one ($), when the solves had PWL losses
            for prefix, bounds in (("master", result.master_loss_repair),
                                   ("dispatch", result.dispatch_loss_repair)):
                for key, val in (bounds or {}).items():
                    scenario.attrs[f"{prefix}_loss_repair_{key}"] = val

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
                gen_renewable = getattr(self, '_gen_renewable', []) or []
                gen_in_service = getattr(self, '_gen_in_service_mw', None) or []
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
                        # The RE share's rule (type "Renewable"), so readers
                        # need not guess it from the fuel or the name
                        if g < len(gen_renewable):
                            ds.attrs["is_renewable"] = bool(gen_renewable[g])
                        _stamp_power_in_service(ds, gen_in_service, g)

            # --- Generator status (unit commitment: units online) ---
            # Only committed generators: a variable renewable has no on/off
            # decision, so no gen_status / gen_startup dataset
            gen_units = getattr(self, '_gen_num_units', None) or []
            committed = getattr(self, '_gen_committed', None) or []

            def _is_committed(g):
                return committed[g] if g < len(committed) else True

            if result.gen_status is not None:
                status_group = scenario.create_group("gen_status")
                status_group.attrs["description"] = (
                    "Generator units online [nodes x hours] for each committed "
                    "generator (0..num_units; 0/1 for a single-unit plant); "
                    "variable renewables have none"
                )
                for g, name in enumerate(self._gen_names):
                    if g < result.gen_status.shape[0] and _is_committed(g):
                        ds_name = gen_ds[g]
                        ds = status_group.create_dataset(
                            ds_name, data=result.gen_status[g],
                            chunks=True, compression="gzip",
                        )
                        ds.attrs["shape"] = f"[{num_nodes} nodes x {hours} hours]"
                        ds.attrs["units"] = "units online"
                        _stamp_num_units(ds, gen_units, g)

            # --- Generator startup events (per generator) ---
            if result.gen_startup is not None:
                startup_group = scenario.create_group("gen_startup")
                startup_group.attrs["description"] = (
                    "Generator units started [nodes x hours] for each committed "
                    "generator; each start costs start_up_cost / num_units"
                )
                for g, name in enumerate(self._gen_names):
                    if g < result.gen_startup.shape[0] and _is_committed(g):
                        ds_name = gen_ds[g]
                        ds = startup_group.create_dataset(
                            ds_name, data=result.gen_startup[g],
                            chunks=True, compression="gzip",
                        )
                        ds.attrs["shape"] = f"[{num_nodes} nodes x {hours} hours]"
                        ds.attrs["units"] = "units started"
                        _stamp_num_units(ds, gen_units, g)

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
                    bat_in_service = getattr(self, '_bat_in_service_mw', None) or []
                    for b, name in enumerate(self._bat_names):
                        if b < bat_data.shape[0]:
                            ds = bat_group.create_dataset(
                                bat_ds[b], data=bat_data[b],
                                chunks=True, compression="gzip",
                            )
                            ds.attrs["shape"] = f"[{num_nodes} nodes x {hours} hours]"
                            ds.attrs["units"] = "MW"
                            if bat_key == "battery_discharge":
                                _stamp_power_in_service(ds, bat_in_service, b)

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

            # --- Line losses [node x hour] ---
            if result.line_losses is not None:
                ds = scenario.create_dataset(
                    "line_losses", data=result.line_losses,
                    chunks=True, compression="gzip",
                )
                ds.attrs["units"] = "MW"
                ds.attrs["dimensions"] = "nodes x hours"
                ds.attrs["description"] = (
                    "Transmission losses of the DC loss model: half of each "
                    "line's loss at each of its two end nodes"
                )

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
                "transmission_losses": result.transmission_losses,
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
