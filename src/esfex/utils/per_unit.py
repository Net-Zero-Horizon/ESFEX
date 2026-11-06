"""Per-unit conventions of the DC network.

Line impedances are per unit on the system power base ``S_base``
(``ac_power_flow.base_mva``, MVA) and the line's own voltage, as the Grid
Builder writes them: ``Z_base = V² / S_base``. The DC model turns them back
into MW with the same ``S_base`` (flow = S_base · Δθ / x, loss = r · F² / S_base).
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
    x_km = reactance_per_km if reactance_per_km and reactance_per_km > 0 \
        else dc_config.reactance_per_km
    z_base = v * v / s_base_mva
    return length_km * x_km / z_base
