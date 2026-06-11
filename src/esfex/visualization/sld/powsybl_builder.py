"""Map an ESFEX ``GuiSystemState`` to a PowSyBl network for SLD/NAD rendering.

PowSyBl (RTE's open-source grid library, via ``pypowsybl``) produces
production-grade single-line diagrams — busbar sections, feeder cells with
switchgear (real connection points), IEC transformer symbols, and orthogonal
routing — and network-area diagrams for the whole-grid overview. We build a
topologically valid bus/breaker network from the GUI state; electrical
parameters are nominal placeholders since the diagrams are topological (no power
flow is run for rendering).

Mapping
-------
* ``GuiNode``            → ``Substation``
* ``(node, voltage)``    → ``VoltageLevel`` (BUS_BREAKER)
* ``GuiBus``             → ``Bus``
* generator/battery/electrolyzer → injection on its bus (or the node's
  representative bus)
* load-role bus          → ``Load``
* ``GuiTransmissionLine``→ ``Line``
* ``GuiTransformer``     → 2-winding transformer (same substation)
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from esfex.visualization.data.gui_model import GuiSystemState

log = logging.getLogger(__name__)

# Nominal electrical placeholders — diagrams are topological, not solved.
_DEF_R = 0.5
_DEF_X = 5.0
_DEF_G = 0.0
_DEF_B = 0.0


def _sid(prefix: str, raw) -> str:
    """A PowSyBl-safe, stable id."""
    return f"{prefix}{re.sub(r'[^A-Za-z0-9_]', '_', str(raw))}"


def _vl_id(node_idx: int, kv: float) -> str:
    return f"VL_{node_idx}_{int(round(kv))}"


def build_powsybl_network(state: "GuiSystemState", network_id: str = "esfex"):
    """Build and return a ``pypowsybl.network.Network`` from ``state``.

    Raises ``ImportError`` if pypowsybl is unavailable.
    """
    import pypowsybl.network as pp_net

    n = pp_net.create_empty(network_id)

    nodes = {nd.index: nd for nd in state.nodes}
    buses = dict(state.buses)

    # ── Substations + voltage levels + buses ──
    created_subs: set[int] = set()
    created_vls: set[str] = set()
    bus_vl: dict[str, str] = {}        # bus_id → voltage_level_id
    bus_pid: dict[str, str] = {}       # bus_id → powsybl bus id
    node_buses: dict[int, list[str]] = {}

    for bus_id, bus in buses.items():
        node_idx = bus.parent_node
        if node_idx not in nodes:
            continue
        kv = float(bus.voltage_kv or 0.0)
        if kv <= 0:
            continue
        sub = _sid("S", node_idx)
        if node_idx not in created_subs:
            nd = nodes[node_idx]
            n.create_substations(id=sub, name=(nd.name or sub))
            created_subs.add(node_idx)
        vl = _vl_id(node_idx, kv)
        if vl not in created_vls:
            n.create_voltage_levels(
                id=vl, substation_id=sub,
                topology_kind="BUS_BREAKER", nominal_v=kv)
            created_vls.add(vl)
        pid = _sid("B", bus_id)
        n.create_buses(id=pid, voltage_level_id=vl)
        bus_vl[bus_id] = vl
        bus_pid[bus_id] = pid
        node_buses.setdefault(node_idx, []).append(bus_id)

    if not bus_pid:
        return n      # nothing electrical to draw

    def _rep_bus(node_idx: int, bus_hint: Optional[str]) -> Optional[str]:
        """Resolve the bus an injection attaches to: its own bus if valid,
        else the node's highest-voltage bus."""
        if bus_hint and bus_hint in bus_pid:
            return bus_hint
        cands = node_buses.get(node_idx, [])
        if not cands:
            return None
        return max(cands, key=lambda b: float(buses[b].voltage_kv or 0.0))

    # ── Injections: generators ──
    for gid, g in state.generators.items():
        b = _rep_bus(g.node, getattr(g, "bus", None))
        if not b:
            continue
        p = float(g.rated_power or 0.0)
        n.create_generators(
            id=_sid("G", gid), voltage_level_id=bus_vl[b], bus_id=bus_pid[b],
            name=(g.name or gid), max_p=max(p, 1.0), min_p=0.0,
            target_p=p, target_v=float(buses[b].voltage_kv or 1.0),
            voltage_regulator_on=True)

    # ── Injections: batteries (modelled as PowSyBl batteries) ──
    for bid, bat in state.batteries.items():
        b = _rep_bus(bat.node, getattr(bat, "bus", None))
        if not b:
            continue
        p = float(getattr(bat, "rated_power", 0.0) or 0.0)
        try:
            n.create_batteries(
                id=_sid("BAT", bid), voltage_level_id=bus_vl[b],
                bus_id=bus_pid[b], name=(bat.name or bid),
                max_p=max(p, 1.0), min_p=-max(p, 1.0), target_p=0.0,
                target_q=0.0)
        except Exception as exc:                       # pragma: no cover
            log.debug("battery %s skipped: %s", bid, exc)

    # ── Injections: electrolyzers (consumption → loads) ──
    for eid, el in state.electrolyzers.items():
        b = _rep_bus(el.node, getattr(el, "bus", None))
        if not b:
            continue
        n.create_loads(
            id=_sid("ELZ", eid), voltage_level_id=bus_vl[b],
            bus_id=bus_pid[b], name=(el.name or eid),
            p0=float(getattr(el, "rated_power", 0.0) or 0.0), q0=0.0)

    # ── Loads: one per load-role bus ──
    for bus_id, bus in buses.items():
        if bus_id not in bus_pid:
            continue
        if getattr(bus, "role", "") not in ("load", "mixed"):
            continue
        frac = float(getattr(bus, "demand_fraction", 0.0) or 0.0)
        n.create_loads(
            id=_sid("LD", bus_id), voltage_level_id=bus_vl[bus_id],
            bus_id=bus_pid[bus_id], name=f"Load {bus.name or bus_id}",
            p0=max(frac * 100.0, 1.0), q0=0.0)

    # ── Lines (inter-bus, any voltage) ──
    for ln in state.transmission_lines:
        a, b = ln.from_bus, ln.to_bus
        if a not in bus_pid or b not in bus_pid or a == b:
            continue
        try:
            n.create_lines(
                id=_sid("L", ln.line_id),
                voltage_level1_id=bus_vl[a], bus1_id=bus_pid[a],
                voltage_level2_id=bus_vl[b], bus2_id=bus_pid[b],
                r=_DEF_R, x=_DEF_X, g1=_DEF_G, b1=_DEF_B, g2=_DEF_G, b2=_DEF_B)
        except Exception as exc:                       # pragma: no cover
            log.debug("line %s skipped: %s", ln.line_id, exc)

    # ── Transformers (2-winding, same substation) ──
    for i, tr in enumerate(state.transformers):
        a, b = tr.from_bus, tr.to_bus
        if a not in bus_pid or b not in bus_pid or a == b:
            continue
        if buses[a].parent_node != buses[b].parent_node:
            continue                                   # 2WT must share a substation
        u1 = float(tr.from_voltage_kv or buses[a].voltage_kv or 1.0)
        u2 = float(tr.to_voltage_kv or buses[b].voltage_kv or 1.0)
        try:
            n.create_2_windings_transformers(
                id=_sid("T", tr.name or f"tr{i}"),
                voltage_level1_id=bus_vl[a], bus1_id=bus_pid[a],
                voltage_level2_id=bus_vl[b], bus2_id=bus_pid[b],
                rated_u1=u1, rated_u2=u2, r=_DEF_R, x=_DEF_X, g=_DEF_G, b=_DEF_B)
        except Exception as exc:                       # pragma: no cover
            log.debug("transformer %s skipped: %s", tr.name, exc)

    return n


def substation_ids(state: "GuiSystemState") -> list[str]:
    """The PowSyBl substation ids (one per node that has ≥1 valid bus)."""
    out: list[str] = []
    seen: set[int] = set()
    nodes = {nd.index for nd in state.nodes}
    for bus in state.buses.values():
        ni = bus.parent_node
        if ni in nodes and ni not in seen and float(bus.voltage_kv or 0) > 0:
            seen.add(ni)
            out.append(_sid("S", ni))
    return out
