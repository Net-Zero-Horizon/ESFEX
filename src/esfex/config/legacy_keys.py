"""
Key translations the loader applies before Pydantic validation.

Older files and the Studio write some keys under names the schema does not
declare: flat ``dc_*`` system keys, ``unit_*``/``bat_*`` units, a single
``electrolyzer`` block, penalties in any case, renamed system keys
(``max_annual_system_cost``; ``SystemConfig`` renames those itself). The loader
(:mod:`esfex.config.loader`) reads them through these helpers, and the
unknown-key walker (:mod:`esfex.config.unknown_keys`) uses the same helpers
to decide which keys are consumed, so the two cannot drift apart.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

# ``DCPowerFlowConfig`` fields that may be given as flat system keys
# ``dc_<field>`` (any capitalisation, e.g. ``DC_SLACK_BUS``).
LEGACY_DC_FIELDS = (
    "reactance_per_km", "voltage_level_kv", "max_angle_diff_deg", "slack_bus",
)

# System keys holding one generator or storage unit each (legacy format).
LEGACY_UNIT_PREFIXES = ("unit_", "bat_")

# System key holding a single electrolyzer (legacy / Studio format).
SINGLE_ELECTROLYZER_KEY = "electrolyzer"

# System keys renamed since: old name → field name. A file may still use
# the old one; it is read as the new field.
LEGACY_SYSTEM_KEYS = {
    "max_annual_system_cost": "max_annual_investment",
    "MAX_ANNUAL_SYSTEM_COST": "max_annual_investment",
}

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


def legacy_system_field(key: Any) -> Optional[str]:
    """The ``SystemConfig`` field an old system key now names, or None."""
    return LEGACY_SYSTEM_KEYS.get(str(key))


def rename_legacy_system_keys(data: dict[str, Any]) -> dict[str, Any]:
    """*data* (a system's keys) with each old key of ``LEGACY_SYSTEM_KEYS``
    under its field name, with one info line. When the field is also given
    by its own name, that one is kept (a warning if the values differ)."""
    old = [k for k in data if str(k) in LEGACY_SYSTEM_KEYS]
    if not old:
        return data
    data = dict(data)
    for key in old:
        field = LEGACY_SYSTEM_KEYS[str(key)]
        value = data.pop(key)
        if field in data:
            if data[field] != value:
                log.warning("System %r: both %s (%r) and %s (%r) are set; using %s.",
                            data.get("name"), field, data[field], key, value, field)
            continue
        data[field] = value
        log.info("System %r: %s is read as %s (the annual investment budget).",
                 data.get("name"), key, field)
    return data
