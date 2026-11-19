"""Load and check a case (a configuration YAML) without the Studio.

:func:`validate_case` is what ``esfex validate`` reports: the loader's
errors with the YAML key path of each, the data files the run would read
and cannot find, the unknown keys and the warnings the load logs, and a
short description of the case.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from esfex.api._common import ApiError


@dataclass
class ValidationReport:
    """Result of :func:`validate_case`. ``ok`` is False when ``errors`` is
    not empty: the case would not load or would fail at the start of a run.
    ``warnings`` are :class:`ApiError` records too (``unknown_key``,
    ``loader_warning``); ``summary`` describes the case (``None`` when it
    did not load)."""

    path: str
    ok: bool
    errors: list[ApiError] = field(default_factory=list)
    warnings: list[ApiError] = field(default_factory=list)
    summary: Optional[dict] = None

    def to_dict(self) -> dict:
        return {"path": self.path, "ok": self.ok,
                "errors": [e.to_dict() for e in self.errors],
                "warnings": [w.to_dict() for w in self.warnings],
                "summary": self.summary}


def load_case(path: Union[str, Path]):
    """The validated :class:`~esfex.config.schema.ESFEXConfig` of *path*.
    Raises :class:`~esfex.config.loader.ConfigLoadError` (its ``issues``
    carry the key paths)."""
    from esfex.config.loader import load_config

    return load_config(path)


# pydantic error type → what to do about it
_FIX_BY_TYPE = {
    "missing": "Add the key (see docs/reference/config-reference.md for its meaning and units).",
    "extra_forbidden": "Remove the key or correct its spelling.",
    "literal_error": "Use one of the values the message lists.",
    "enum": "Use one of the values the message lists.",
    "float_parsing": "Give a number.",
    "float_type": "Give a number.",
    "int_parsing": "Give a whole number.",
    "int_type": "Give a whole number.",
    "int_from_float": "Give a whole number.",
    "bool_parsing": "Give true or false.",
    "bool_type": "Give true or false.",
    "list_type": "Give a list (one value per node where the key is per node).",
    "dict_type": "Give a mapping (key: value pairs).",
    "string_type": "Give a text value.",
    "greater_than": "Give a larger value (see the bound in the message).",
    "greater_than_equal": "Give a larger value (see the bound in the message).",
    "less_than": "Give a smaller value (see the bound in the message).",
    "less_than_equal": "Give a smaller value (see the bound in the message).",
    "too_short": "Give more entries (per-node lists need one value per node).",
    "too_long": "Give fewer entries (per-node lists need one value per node).",
    "yaml": "Fix the YAML syntax at the line and column the message gives.",
    "file_not_found": "Check the path of the configuration file.",
}


def config_issue_error(issue: dict) -> ApiError:
    itype = str(issue.get("type") or "load")
    message = str(issue.get("message") or "")
    if issue.get("input") is not None and itype not in ("missing", "file_not_found"):
        message = f"{message} (got {issue['input']!r})"
    return ApiError(code=itype, key=str(issue.get("key") or ""), message=message,
                    fix=_FIX_BY_TYPE.get(itype, ""))


def _demand_file(config_dir: Path, p: str) -> Path:
    # As the runner resolves demand paths: under the config directory
    # first, else as given (absolute or relative to the working directory)
    fp = Path(p)
    if not fp.is_absolute():
        cand = config_dir / fp
        if cand.exists():
            return cand
    return fp


def missing_data_files(cfg, config_dir: Union[str, Path]) -> list[ApiError]:
    """The demand and availability files *cfg* names that a run would not
    find (resolved as the runner resolves them)."""
    from esfex.utils.paths import resolve_project_data_file

    config_dir = Path(config_dir)
    out: list[ApiError] = []
    fix = (f"Relative paths resolve against the configuration's directory ({config_dir}); "
           "correct the path or copy the file there.")

    def _is_path(ref) -> bool:
        return isinstance(ref, (str, Path)) and bool(str(ref))

    def _check_avail(key: str, ref) -> None:
        if not _is_path(ref):
            return
        try:
            path = resolve_project_data_file(config_dir, ref)
        except ValueError:
            out.append(ApiError("path_outside_case", f"{ref!r} resolves outside {config_dir}",
                                key=key, fix="Use a path inside the configuration's directory "
                                             "or an absolute path."))
            return
        if not path.exists():
            out.append(ApiError("missing_file", f"File not found: {ref}", key=key, fix=fix))

    for sname, sysc in cfg.systems.items():
        base = f"systems.{sname}"
        if sysc.demand_paths:
            for i, p in enumerate(sysc.demand_paths):
                if _is_path(p) and not _demand_file(config_dir, p).exists():
                    out.append(ApiError("missing_file", f"File not found: {p}",
                                        key=f"{base}.demand_paths.{i}", fix=fix))
        elif (_is_path(sysc.demand_path)
              and not _demand_file(config_dir, sysc.demand_path).exists()):
            out.append(ApiError("missing_file", f"File not found: {sysc.demand_path}",
                                key=f"{base}.demand_path", fix=fix))
        for key, gen in sysc.generators.items():
            _check_avail(f"{base}.generators.{key}.availability_file", gen.availability_file)
        for key, tech in sysc.technologies.items():
            _check_avail(f"{base}.technologies.{key}.availability_file", tech.availability_file)
        for key, bat in sysc.batteries.items():
            _check_avail(f"{base}.batteries.{key}.availability_file", bat.availability_file)
        for key, bt in sysc.battery_technologies.items():
            _check_avail(f"{base}.battery_technologies.{key}.availability_file",
                         bt.availability_file)
    return out


def case_summary(cfg) -> dict:
    """What the case is: mode, solver, horizon and, per system, its size
    and units by type."""
    from esfex.utils.temporal import DEFAULT_RUN_YEARS

    systems = []
    for name in cfg.meta_network.systems:
        s = cfg.systems[name]
        nodes = s.nodes.num_nodes or int(math.sqrt(len(s.nodes.nodes_connections or [])) or 0)
        gen_types = Counter(g.type for g in s.generators.values())
        gen_fuels = Counter((g.fuel or "None") for g in s.generators.values())
        systems.append({
            "name": name,
            "nodes": nodes,
            "buses": len(s.buses or []),
            "lines": len(s.transmission_lines_geo or []),
            "generators": len(s.generators),
            "generators_by_type": dict(gen_types),
            "generators_by_fuel": dict(gen_fuels),
            "generation_mw": round(sum(sum(g.rated_power or []) for g in s.generators.values()), 3),
            "batteries": len(s.batteries),
            "technologies": len(s.technologies),
            "battery_technologies": len(s.battery_technologies),
            "electrolyzers": len(s.electrolyzers or {}),
            "development_zones": len(s.development_zones or []),
        })
    t = cfg.temporal
    return {
        "simulation_mode": cfg.simulation_mode,
        "solver": cfg.solver.name,
        "horizon": {
            "date_start": cfg.date_start,
            "default_years": DEFAULT_RUN_YEARS,
            "resolution_hours": t.resolution_hours,
            "rolling_horizon": bool(t.use_rolling_horizon),
            "window_hours": t.rolling_horizon_hours,
            "overlap_hours": t.overlap_hours,
            "unit_commitment_hours": (cfg.unit_commitment_hours
                                      if cfg.simulation_mode == "unit_commitment" else None),
        },
        "primary_energy": bool(cfg.enable_primary_energy),
        "n1_security": bool(cfg.n1_security.enabled),
        "systems": systems,
        "system_links": len(cfg.meta_network.systems_links or []),
    }


def validate_case(path: Union[str, Path], check_files: bool = True) -> ValidationReport:
    """Check the case at *path*: does it load, and would a run find its data.

    Never raises for a bad case; the problems are in the report. The
    warnings the load logs (zero ramps, demandless nodes, ...) are listed
    as ``loader_warning`` entries, the ignored keys as ``unknown_key``.
    """
    from esfex.api.diagnostics import WarningsCollector
    from esfex.config.loader import ConfigLoadError
    import logging

    path = Path(path)
    report = ValidationReport(path=str(path), ok=False)
    collector = WarningsCollector()
    root = logging.getLogger()
    root.addHandler(collector)
    try:
        try:
            cfg = load_case(path)
        except ConfigLoadError as e:
            issues = getattr(e, "issues", None) or [
                {"key": "", "type": "load", "message": str(e), "input": None}]
            report.errors = [config_issue_error(i) for i in issues]
            cfg = None
    finally:
        root.removeHandler(collector)

    if cfg is not None:
        for key in cfg.unknown_keys:
            report.warnings.append(ApiError(
                "unknown_key", "Unknown key, ignored (misspelt or obsolete?)", key=key,
                fix="Correct the spelling or remove the key."))
        if check_files:
            report.errors.extend(missing_data_files(cfg, path.parent))
        try:
            report.summary = case_summary(cfg)
        except Exception as exc:  # a summary must not hide the verdict
            report.summary = {"error": str(exc)}

    for g in collector.summary().groups:
        if "nknown configuration key" in g.message:
            continue  # listed above with their key paths
        report.warnings.append(ApiError(
            "loader_warning", g.message if g.count == 1 else f"{g.message} (×{g.count})",
            key=""))
    report.ok = not report.errors
    return report


__all__ = ["ValidationReport", "case_summary", "config_issue_error", "load_case",
           "missing_data_files", "validate_case"]
