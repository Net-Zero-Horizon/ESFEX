"""
Detect configuration keys that no part of ESFEX reads.

The Pydantic models ignore keys they do not declare, so a misspelt or
obsolete key (``systems.Cuba.generators.gas_1.dorop``) would be dropped
without a trace. :func:`find_unknown_keys` walks a raw YAML dict alongside
the schema and returns the full path of every key that neither the schema
nor the loader's legacy translations consume. The loader logs them; the
Studio's validation lists them.
"""

from __future__ import annotations

import types
import typing
from typing import Any, Union

from pydantic import BaseModel

from esfex.config.schema import (
    BatteryConfig,
    ElectrolyzerConfig,
    ESFEXConfig,
    GeneratorConfig,
    PenaltiesConfig,
    ScenarioMultipliers,
    SystemConfig,
    canonical_unit_type,
)

# Flat system-level DC power-flow keys the loader folds into ``dc_power_flow``
# (any capitalisation, ``dc_`` or ``DC_`` prefix).
_LEGACY_DC_KEYS = frozenset({
    "base_impedance", "reactance_per_km", "voltage_level_kv",
    "max_angle_diff_deg", "slack_bus",
})

# Penalty keys are read case-insensitively; ``TransferMargin`` is the legacy
# spelling of ``transfer_margin``.
_PENALTY_ALIASES = {"transfermargin": "transfer_margin"}

# Top-level keys only the Studio reads, from the raw YAML (they are not part
# of the schema and the solver ignores them).
_GUI_TOP_LEVEL_KEYS = frozenset({"visual_scaling", "_geo_assets"})


def find_unknown_keys(raw: dict[str, Any]) -> list[str]:
    """Return the dotted path of every key in *raw* (a full configuration,
    with system file references already resolved) that ESFEX ignores."""
    out: list[str] = []
    _walk_model(raw, ESFEXConfig, "", out)
    return out


def find_unknown_system_keys(raw: dict[str, Any], prefix: str = "") -> list[str]:
    """Like :func:`find_unknown_keys` for a single system's dict."""
    out: list[str] = []
    _walk_system(raw, prefix, out)
    return out


def _join(prefix: str, key: Any) -> str:
    return f"{prefix}.{key}" if prefix else str(key)


def _field_for(model: type[BaseModel], key: str):
    fields = model.model_fields
    if key in fields:
        return fields[key]
    for field in fields.values():
        if field.alias == key:
            return field
    return None


def _walk_model(data: Any, model: type[BaseModel], prefix: str, out: list[str]) -> None:
    if not isinstance(data, dict):
        return
    if model is SystemConfig:
        _walk_system(data, prefix, out)
        return
    if model is ScenarioMultipliers:
        return  # normalize_scenario_multipliers warns about these itself
    for key, value in data.items():
        path = _join(prefix, key)
        if model is ESFEXConfig and key in _GUI_TOP_LEVEL_KEYS:
            continue
        name = str(key)
        if model is PenaltiesConfig:
            name = _PENALTY_ALIASES.get(name.lower(), name.lower())
        field = _field_for(model, name)
        if field is None:
            out.append(path)
            continue
        _walk_value(value, field.annotation, path, out)


def _walk_value(value: Any, annotation: Any, path: str, out: list[str]) -> None:
    """Descend into *value* where *annotation* holds a model."""
    origin = typing.get_origin(annotation)
    if origin in (Union, types.UnionType):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if isinstance(value, dict):
            models = [a for a in args if _is_model(a)]
            if models:
                _walk_model(value, models[0], path, out)
                return
            dicts = [a for a in args if typing.get_origin(a) is dict]
            if dicts:
                _walk_value(value, dicts[0], path, out)
        elif isinstance(value, list):
            lists = [a for a in args if typing.get_origin(a) is list]
            if lists:
                _walk_value(value, lists[0], path, out)
        return
    if _is_model(annotation):
        _walk_model(value, annotation, path, out)
    elif origin is list and isinstance(value, list):
        (item,) = typing.get_args(annotation) or (Any,)
        for i, v in enumerate(value):
            _walk_value(v, item, f"{path}[{i}]", out)
    elif origin is dict and isinstance(value, dict):
        args = typing.get_args(annotation)
        item = args[1] if len(args) == 2 else Any
        for k, v in value.items():
            _walk_value(v, item, _join(path, k), out)


def _is_model(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _walk_system(data: dict[str, Any], prefix: str, out: list[str]) -> None:
    """Walk one system, mirroring the translations of ``_convert_system``."""
    for key, value in data.items():
        path = _join(prefix, key)
        name = str(key)
        if (name.startswith("unit_") or name.startswith("bat_")) and isinstance(value, dict):
            unit_type = canonical_unit_type(value.get("type", "Unknown"))
            model = BatteryConfig if unit_type == "Storage" else GeneratorConfig
            _walk_model(value, model, path, out)
            continue
        if name == "electrolyzer":
            _walk_model(value, ElectrolyzerConfig, path, out)
            continue
        if name[:3].lower() == "dc_" and name != "dc_power_flow":
            if name[3:].lower() not in _LEGACY_DC_KEYS:
                out.append(path)
            continue
        field = _field_for(SystemConfig, name)
        if field is None:
            out.append(path)
            continue
        _walk_value(value, field.annotation, path, out)
