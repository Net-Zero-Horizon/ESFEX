"""
Key translations the loader applies before Pydantic validation.

Older files and the Studio write some keys under names the schema does not
declare: flat ``dc_*`` system keys, ``unit_*``/``bat_*`` units, a single
``electrolyzer`` block, penalties in any case. The loader
(:mod:`esfex.config.loader`) reads them through these helpers, and the
unknown-key walker (:mod:`esfex.config.unknown_keys`) uses the same helpers
to decide which keys are consumed, so the two cannot drift apart.
"""

from __future__ import annotations

from typing import Any, Optional

# ``DCPowerFlowConfig`` fields that may be given as flat system keys
# ``dc_<field>`` (any capitalisation, e.g. ``DC_BASE_IMPEDANCE``).
LEGACY_DC_FIELDS = (
    "base_impedance", "reactance_per_km", "voltage_level_kv",
    "max_angle_diff_deg", "slack_bus",
)

# System keys holding one generator or storage unit each (legacy format).
LEGACY_UNIT_PREFIXES = ("unit_", "bat_")

# System key holding a single electrolyzer (legacy / Studio format).
SINGLE_ELECTROLYZER_KEY = "electrolyzer"

# Penalty keys are read case-insensitively; the keys here are legacy
# spellings of a ``PenaltiesConfig`` field.
PENALTY_ALIASES = {"transfermargin": "transfer_margin"}


def is_flat_dc_key(key: Any) -> bool:
    """True for a flat ``dc_*`` system key. The loader consumes every such
    key (reading the ones :func:`flat_dc_field` recognises)."""
    name = str(key)
    return name[:3].lower() == "dc_" and name != "dc_power_flow"


def flat_dc_field(key: Any) -> Optional[str]:
    """The ``DCPowerFlowConfig`` field a flat ``dc_*`` key sets, or None."""
    if not is_flat_dc_key(key):
        return None
    field = str(key)[3:].lower()
    return field if field in LEGACY_DC_FIELDS else None


def is_legacy_unit_key(key: Any, value: Any) -> bool:
    """True for a ``unit_*``/``bat_*`` system key holding a unit dict."""
    return str(key).startswith(LEGACY_UNIT_PREFIXES) and isinstance(value, dict)


def penalty_field(key: Any) -> str:
    """The ``PenaltiesConfig`` field name a penalties key maps to."""
    name = str(key).lower()
    return PENALTY_ALIASES.get(name, name)
