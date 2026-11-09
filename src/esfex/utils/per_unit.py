"""Per-unit conventions of the DC network.

Line impedances are per unit on the system power base ``S_base``
(``ac_power_flow.base_mva``, MVA) and the line's own voltage, as the Grid
Builder writes them: ``Z_base = V² / S_base``. The DC model turns them back
into MW with the same ``S_base`` (flow = S_base · Δθ / x, loss = r · F² / S_base).

A line's ``capacity_mw``, ``reactance_pu``, ``resistance_pu`` and
``susceptance_pu`` are those of the whole line, all of its ``num_circuits``
parallel circuits together; the model never rescales them by the circuit
count. Only a reactance derived from a per-km value (that of one circuit) is
divided over the circuits.
"""

from __future__ import annotations

from typing import Any, Optional

# Reactance used when a line has neither reactance_pu nor a usable length
DEFAULT_REACTANCE_PU = 0.01


def system_s_base(system_config: Any) -> float:
    """System power base (MVA): ``ac_power_flow.base_mva``, 100 if absent."""
    ac = getattr(system_config, "ac_power_flow", None)
    return float(getattr(ac, "base_mva", 100.0) or 100.0)


def derived_reactance_pu(
    length_km: Optional[float],
    voltage_kv: Optional[float],
    dc_config: Any,
    s_base_mva: float,
    reactance_per_km: Optional[float] = None,
) -> float:
    """Reactance (p.u.) of a line given only its length.

    ``length × reactance_per_km / Z_base`` with ``Z_base = V² / S_base``; ``V``
    is the line voltage, or ``dc_power_flow.voltage_level_kv`` when unknown.
    ``reactance_per_km`` is the line's own value (ohm/km); when None or 0,
    ``dc_power_flow.reactance_per_km`` is used.
    """
    v = voltage_kv or getattr(dc_config, "voltage_level_kv", 0.0)
    if not length_km or length_km <= 0 or not v or v <= 0 or s_base_mva <= 0:
        return DEFAULT_REACTANCE_PU
    own = isinstance(reactance_per_km, (int, float)) and reactance_per_km > 0
    x_km = reactance_per_km if own else dc_config.reactance_per_km
    z_base = v * v / s_base_mva
    return length_km * x_km / z_base


def line_reactance_pu(
    line: Any,
    length_km: Optional[float],
    dc_config: Any,
    s_base_mva: float,
) -> float:
    """Series reactance (p.u.) of a whole line, all its circuits together.

    The line's own ``reactance_pu`` when > 0. Otherwise the reactance of one
    circuit of *length_km* (:func:`derived_reactance_pu`, from the line's
    ``reactance_per_km`` or the system default) divided by its
    ``num_circuits``.
    """
    x = getattr(line, "reactance_pu", None)
    if x is not None and x > 0:
        return float(x)
    n = max(1, int(getattr(line, "num_circuits", 1) or 1))
    return derived_reactance_pu(
        length_km, getattr(line, "voltage_kv", None), dc_config, s_base_mva,
        getattr(line, "reactance_per_km", None)) / n
