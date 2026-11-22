"""Shared pieces of the ``esfex.api`` layer: the error record and the JSON
envelope every ``--json`` command prints."""

from __future__ import annotations

import math
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any, Optional

# Version of the JSON documents; part of each document's ``schema`` string
# ("esfex.<command>/1"). Bumped only when a field changes meaning or goes.
SCHEMA_VERSION = 1


def schema_name(command: str) -> str:
    return f"esfex.{command}/{SCHEMA_VERSION}"


@dataclass
class ApiError:
    """One problem, as the JSON contract lists it in ``errors``.

    ``code`` is a stable machine-readable name (``missing``,
    ``missing_file``, ``not_a_results_file``, ...), ``key`` the place it
    refers to (a dotted YAML path, an HDF5 path, or ``""``), ``message`` the
    text for a human and ``fix`` a hint on what to change (``""`` when there
    is none).
    """

    code: str
    message: str
    key: str = ""
    fix: str = ""

    def to_dict(self) -> dict:
        return {"code": self.code, "key": self.key, "message": self.message, "fix": self.fix}


def jsonable(obj: Any) -> Any:
    """*obj* with numpy scalars and arrays, paths, bytes, tuples, sets and
    dataclasses turned into plain JSON types. Non-finite floats become
    ``None`` (JSON has no NaN)."""
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        return jsonable(obj.to_dict())
    if is_dataclass(obj) and not isinstance(obj, type):
        return jsonable({f.name: getattr(obj, f.name) for f in fields(obj)})
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    try:
        import numpy as np

        if isinstance(obj, np.bool_):
            return bool(obj)
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return jsonable(float(obj))
        if isinstance(obj, np.ndarray):
            return jsonable(obj.tolist())
    except ImportError:  # pragma: no cover - numpy ships with esfex
        pass
    return str(obj)


def envelope(command: str, ok: bool, errors: Optional[list] = None, **data) -> dict:
    """The JSON document of one command: ``schema``, ``ok``, ``errors``
    (always present, empty on success) and the command's own fields."""
    doc: dict[str, Any] = {"schema": schema_name(command), "ok": bool(ok)}
    doc["errors"] = [e.to_dict() if isinstance(e, ApiError) else e for e in (errors or [])]
    doc.update(data)
    return jsonable(doc)


__all__ = ["ApiError", "SCHEMA_VERSION", "envelope", "jsonable", "schema_name"]
