"""Read a results file without opening it in the Studio.

:func:`summary` gives, per simulated year, the numbers that say whether a
run is sound and what it decided: cost and its components (with a check
that they add up to the total), RE share against the target, load shed,
curtailment, emissions, builds and retirements, capacity in service,
storage, transmission losses, and the run's warnings and the master's
binding constraints when the file records them.

It reads lazily: attributes, the per-year scalars and only the two
time-series datasets the shedding and curtailment figures need
(``loss_load``, ``curtailment``), never the generation arrays, so it stays
fast on multi-GB files. Files written by older versions, without the
newer groups, still give everything they hold; what is missing is listed in
``notes``.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Union

from esfex.api._common import ApiError

# Sections of a summary, in display order (``esfex summary --section``)
SECTIONS = ("costs", "re", "shedding", "curtailment", "emissions", "builds",
            "capacity", "storage", "losses", "warnings", "binding")

# A cost total and the sum of its components may differ by this much
_COST_ABS_TOL = 1.0      # $
_COST_REL_TOL = 1e-6
# A step sheds load when its shed MW over the nodes exceeds this
_SHED_TOL_MW = 1e-6


class ResultsFileError(Exception):
    """The file cannot be read as an ESFEX results file."""

    def __init__(self, error: ApiError):
        super().__init__(error.message)
        self.error = error


@dataclass
class RunSummary:
    """Result of :func:`summary`. ``years`` holds one dict per year with
    the requested sections; ``checks`` the consistency checks that failed
    (empty when everything adds up); ``notes`` what the file does not
    record (older versions)."""

    path: str
    metadata: dict
    years: list[dict] = field(default_factory=list)
    totals: dict = field(default_factory=dict)
    warnings: Optional[dict] = None
    master: Optional[dict] = None
    checks: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    sections: tuple = SECTIONS

    def to_dict(self) -> dict:
        out = {"path": self.path, "metadata": self.metadata, "sections": list(self.sections),
               "years": self.years, "totals": self.totals, "checks": self.checks,
               "notes": self.notes}
        if "warnings" in self.sections:
            out["warnings"] = self.warnings
        if "binding" in self.sections:
            out["master"] = self.master
        return out

    def last_year(self) -> Optional[dict]:
        return self.years[-1] if self.years else None


# ── Small readers ─────────────────────────────────────────────────────────

def _txt(v: Any) -> Any:
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    try:
        import numpy as np

        if isinstance(v, np.ndarray):
            return [_txt(x) for x in v.tolist()]
        if isinstance(v, np.generic):
            return v.item()
    except ImportError:  # pragma: no cover
        pass
    return v


def _f(v, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _per_node_sum(val) -> float:
    """Sum of a per-node value stored as an array, a number or its
    ``str()`` (``"[20.0, 0.0]"``, how system_configuration stores lists)."""
    if isinstance(val, (bytes, str)):
        try:
            val = ast.literal_eval(_txt(val))
        except (ValueError, SyntaxError):
            return 0.0
    try:
        import numpy as np

        return float(np.nansum(np.asarray(val, dtype=float)))
    except (TypeError, ValueError):
        return 0.0


def _metadata(f, path: Path) -> dict:
    a = f.attrs
    years_range = _txt(a.get("years_range"))
    meta = {
        "esfex_version": _txt(a.get("esfex_version")),
        "config_file": _txt(a.get("config_file")),
        "solver": _txt(a.get("solver")),
        "simulation_mode": _txt(a.get("simulation_mode")),
        "years_range": years_range,
        "num_years": _txt(a.get("num_years")),
        "num_nodes": _txt(a.get("num_nodes")),
        "num_systems": _txt(a.get("num_systems")),
        "systems": _txt(a.get("subsystem_names")),
        "temporal_resolution_hours": _txt(a.get("temporal_resolution_hours", 1)),
        "created": _txt(a.get("creation_date")),
        "finished": _txt(a.get("export_timestamp")),
        "export_complete": bool(_txt(a.get("export_complete", False))),
        "file_size_mb": round(path.stat().st_size / 1e6, 1),
    }
    return meta


def _scenario_years(f) -> dict[int, str]:
    out: dict[int, str] = {}
    if "detailed_results" in f:
        for key in f["detailed_results"]:
            year = int(_f(f["detailed_results"][key].attrs.get("year"), 0) or 0)
            if year > 0:
                out[year] = key
    return dict(sorted(out.items()))


def _summary_rows(f) -> dict[int, dict]:
    """``summary_results`` (or ``global/summary_results``) as
    ``{year: {dataset: value}}``, for files whose scenario groups lack an
    attribute."""
    import h5py

    for gp in ("summary_results", "global/summary_results"):
        if gp in f and "year" in f[gp]:
            g = f[gp]
            years = [int(y) for y in g["year"][()]]
            out: dict[int, dict] = {y: {} for y in years}
            for k in g:
                ds = g[k]
                if k == "year" or not isinstance(ds, h5py.Dataset) or ds.ndim != 1:
                    continue
                vals = ds[()]
                for i, y in enumerate(years):
                    if i < len(vals):
                        out[y][k] = _txt(vals[i])
            return out
    return {}


# ── Sections of one year ──────────────────────────────────────────────────

def _costs(path: Path, f, year: int, attrs, checks: list) -> dict:
    from esfex.bridge.converters import COST_BREAKDOWN_NON_COMPONENTS
    from esfex.models.financial_analysis import _try_load_cost_breakdown

    objective = _f(attrs.get("total_cost"))
    cb = _try_load_cost_breakdown(path, year)
    if cb is None and f"global/cost_breakdown/year_{year}" in f:
        cb = dict(f[f"global/cost_breakdown/year_{year}"].attrs)
    if cb is None:
        return {"total": objective, "objective": objective, "components": None,
                "reporting": None, "check": None, "source": "total_cost"}
    cb = {k: _f(_txt(v), 0.0) for k, v in cb.items()}
    components = {k: v for k, v in cb.items() if k not in COST_BREAKDOWN_NON_COMPONENTS}
    reporting = {k: v for k, v in cb.items()
                 if k in COST_BREAKDOWN_NON_COMPONENTS and k != "total"}
    total = cb.get("total", objective)
    parts = sum(components.values())
    diff = (total or 0.0) - parts
    tol = max(_COST_ABS_TOL, _COST_REL_TOL * abs(total or 0.0))
    ok = abs(diff) <= tol
    check = {"ok": ok, "sum_of_components": parts, "difference": diff, "tolerance": tol}
    if not ok:
        checks.append({"code": "cost_components_mismatch", "year": year,
                       "message": f"Year {year}: cost components add up to {parts:,.2f} $, "
                                  f"the total is {total:,.2f} $ (difference {diff:,.2f} $)"})
    return {"total": total, "objective": objective,
            "components": dict(sorted(components.items(), key=lambda kv: -abs(kv[1]))),
            "reporting": reporting, "check": check, "source": "cost_breakdown"}


def _re(attrs) -> dict:
    share = _f(attrs.get("renewable_penetration"))
    target = _f(attrs.get("master_re_target"))
    met = None
    gap = None
    if share is not None and target is not None and target > 0:
        gap = max(0.0, target - share)
        met = gap <= 1e-4
    return {"share": share, "target": target, "met": met, "gap": gap}


def _year_dataset(sc, name: str):
    try:
        import h5py

        ds = sc.get(name)
        return ds if isinstance(ds, h5py.Dataset) else None
    except Exception:
        return None


def _step_totals(ds, num_nodes: Optional[int]):
    """A ``(nodes, steps)`` (or old ``(steps,)``) dataset summed over nodes."""
    import numpy as np

    arr = np.asarray(ds[()], dtype=float)
    if arr.ndim == 1:
        return arr
    if arr.ndim == 2:
        # (nodes, steps) as written; an old file may hold (steps, nodes)
        if num_nodes and arr.shape[0] != num_nodes and arr.shape[1] == num_nodes:
            return arr.sum(axis=1)
        return arr.sum(axis=0)
    return arr.reshape(arr.shape[0], -1).sum(axis=0)


def _shedding(sc, attrs, tres: float, num_nodes, notes: list) -> dict:
    import numpy as np

    energy = _f(attrs.get("load_shed"))
    demand = _f(attrs.get("total_demand"))
    hours = None
    peak = None
    ds = _year_dataset(sc, "loss_load")
    if ds is not None:
        per_step = _step_totals(ds, num_nodes)
        hours = float(np.count_nonzero(per_step > _SHED_TOL_MW)) * tres
        peak = float(per_step.max()) if per_step.size else 0.0
        if energy is None:
            energy = float(per_step.sum()) * tres
    else:
        notes.append("no loss_load dataset: shedding hours unknown")
    share = energy / demand if energy is not None and demand else None
    return {"energy_mwh": energy, "hours": hours, "peak_mw": peak, "share_of_demand": share}


def _curtailment(sc, attrs, tres: float, num_nodes, notes: list) -> dict:
    ds = _year_dataset(sc, "curtailment")
    if ds is None:
        notes.append("no curtailment dataset")
        return {"energy_mwh": None, "share": None}
    energy = float(_step_totals(ds, num_nodes).sum()) * tres
    share_re = _f(attrs.get("renewable_penetration"))
    gen = _f(attrs.get("total_generation"))
    share = None
    if share_re is not None and gen is not None:
        available = energy + share_re * gen
        share = energy / available if available > 0 else 0.0
    return {"energy_mwh": energy, "share": share}


def _emissions(attrs) -> dict:
    co2 = _f(attrs.get("co2_emissions"))
    gen = _f(attrs.get("total_generation"))
    return {"co2_t": co2, "intensity_t_per_mwh": co2 / gen if co2 is not None and gen else None}


def _name_of(cfgs: list, i: int, fallback: str) -> str:
    if 0 <= i < len(cfgs):
        c = cfgs[i]
        return str(_txt(c.get("name")) or _txt(c.get("key")) or fallback)
    return fallback


_RETIRE_RE = re.compile(r"^retirement_gen_(\d+)$")


def _builds(path: Path, key: str, attrs, info: dict) -> dict:
    from esfex.models.financial_analysis import _year_builds

    b = _year_builds(path, key)

    def _by_name(kind: str, cfgs: list, prefix: str) -> dict:
        out: dict[str, float] = {}
        for t, per_node in b.get(kind, {}).items():
            name = _name_of(cfgs, t, f"{prefix}_{t}")
            out[name] = out.get(name, 0.0) + float(sum(per_node.values()))
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    retired: dict[str, dict] = {}
    for k, v in attrs.items():
        m = _RETIRE_RE.match(k)
        if not m:
            continue
        g = int(m.group(1))
        share = _f(_txt(v), 0.0) or 0.0
        if share <= 0:
            continue
        name = _name_of(info["gen_configs"], g, f"generator_{g}")
        mw = (share * _per_node_sum(info["gen_configs"][g].get("rated_power"))
              if g < len(info["gen_configs"]) else None)
        retired[name] = {"share": share, "mw": mw}
    return {
        "generation_mw": _by_name("tech", info["tech_configs"], "technology"),
        "storage_mw": _by_name("bat_power", info["bat_tech_configs"], "battery_technology"),
        "storage_mwh": _by_name("bat_energy", info["bat_tech_configs"], "battery_technology"),
        "retired": retired,
    }


def _in_service(sc, group: str) -> dict[str, tuple]:
    """``{unit: (MW in service, fuel)}`` from the ``rated_power`` (and
    ``fuel``) attributes of a per-unit group's datasets (no data read)."""
    import h5py

    out: dict[str, tuple] = {}
    if group not in sc:
        return out
    g = sc[group]
    for name in g:
        ds = g[name]
        if not isinstance(ds, h5py.Dataset):
            continue
        mw = ds.attrs.get("rated_power")
        out[name] = (None if mw is None else _per_node_sum(mw), _txt(ds.attrs.get("fuel", "")))
    return out


def _capacity(sc, notes: list) -> dict:
    units = _in_service(sc, "generation")
    if units and all(mw is None for mw, _ in units.values()):
        notes.append("generation datasets carry no rated_power (older file): "
                     "capacity in service unknown")
        return {"generation_mw_by_fuel": None, "generation_mw": None}
    by_fuel: dict[str, float] = {}
    for mw, fuel in units.values():
        if mw is None:
            continue
        key = fuel or "None"
        by_fuel[key] = by_fuel.get(key, 0.0) + mw
    return {"generation_mw_by_fuel": dict(sorted(by_fuel.items(), key=lambda kv: -kv[1])),
            "generation_mw": sum(by_fuel.values())}


def _storage(sc, builds: Optional[dict]) -> dict:
    units = _in_service(sc, "battery_discharge")
    mw = {n: v for n, (v, _) in units.items() if v is not None}
    return {"in_service_mw": mw, "in_service_total_mw": sum(mw.values()) if mw else 0.0,
            "built_mw": (builds or {}).get("storage_mw"),
            "built_mwh": (builds or {}).get("storage_mwh")}


def _loss_repair(f, sc, year: int) -> Optional[dict]:
    """Loss-repair bounds (first PWL solve: lower bound; final repaired
    solve: upper bound) wherever the file records them: ``loss_repair*``
    attributes of the year's group, or ``/diagnostics/loss_repair/year_Y``."""
    out = {k: _txt(v) for k, v in sc.attrs.items() if "loss_repair" in k}
    gp = f"diagnostics/loss_repair/year_{year}"
    if gp in f:
        out.update({k: _txt(v) for k, v in f[gp].attrs.items()})
    return out or None


def _losses(f, sc, attrs, year: int) -> dict:
    mwh = _f(attrs.get("transmission_losses"))
    demand = _f(attrs.get("total_demand"))
    return {"energy_mwh": mwh, "share_of_demand": mwh / demand if mwh is not None and demand else None,
            "loss_repair": _loss_repair(f, sc, year)}


# ── Public ────────────────────────────────────────────────────────────────

def _normalise_sections(sections: Optional[Iterable[str]]) -> tuple:
    if not sections:
        return SECTIONS
    wanted = [s.strip().lower() for s in sections if s and s.strip()]
    unknown = [s for s in wanted if s not in SECTIONS]
    if unknown:
        raise ValueError(f"unknown section(s) {', '.join(unknown)}; use any of: "
                         f"{', '.join(SECTIONS)}")
    return tuple(s for s in SECTIONS if s in wanted)


def open_results(path: Union[str, Path]):
    """*path* opened read-only with h5py; :class:`ResultsFileError` when it
    is missing, not HDF5 or not an ESFEX results file."""
    import h5py

    path = Path(path)
    if not path.exists():
        raise ResultsFileError(ApiError("file_not_found", f"File not found: {path}",
                                        key=str(path), fix="Check the path."))
    try:
        f = h5py.File(path, "r")
    except OSError as e:
        raise ResultsFileError(ApiError(
            "not_hdf5", f"{path} is not a readable HDF5 file: {e}", key=str(path),
            fix="Give the .h5 file `esfex run` wrote in its output directory.")) from None
    if not any(g in f for g in ("detailed_results", "summary_results", "global")):
        f.close()
        raise ResultsFileError(ApiError(
            "not_a_results_file", f"{path} has no ESFEX results (no detailed_results "
                                  "or summary_results group)", key=str(path),
            fix="Give the .h5 file `esfex run` wrote in its output directory."))
    return f


def summary(path: Union[str, Path], years: Optional[Iterable[int]] = None,
            sections: Optional[Iterable[str]] = None) -> RunSummary:
    """Summary of the results file at *path*, for *years* (default: every
    year in the file) and *sections* (default: all of :data:`SECTIONS`).

    Raises :class:`ResultsFileError` for a file that is not an ESFEX
    results file, ``ValueError`` for an unknown section.
    """
    from esfex.api.diagnostics import read_master_diagnostics, read_warnings
    from esfex.models.financial_analysis import _load_system_from_h5

    path = Path(path)
    secs = _normalise_sections(sections)
    want = set(int(y) for y in years) if years else None

    with open_results(path) as f:
        meta = _metadata(f, path)
        info = _load_system_from_h5(path) if ("builds" in secs or "storage" in secs) else None
        tres = float(meta.get("temporal_resolution_hours") or 1)
        num_nodes = _txt(f.attrs.get("num_nodes"))
        scenarios = _scenario_years(f)
        rows = _summary_rows(f)
        all_years = sorted(set(scenarios) | set(rows))
        sel = [y for y in all_years if want is None or y in want]
        out = RunSummary(path=str(path), metadata=meta, sections=secs)
        if want is not None:
            missing = sorted(want - set(all_years))
            if missing:
                out.notes.append(f"year(s) {', '.join(map(str, missing))} not in the file "
                                 f"(it has {', '.join(map(str, all_years)) or 'none'})")

        notes: list[str] = []
        for year in sel:
            key = scenarios.get(year)
            sc = f[f"detailed_results/{key}"] if key else None
            attrs = dict(sc.attrs) if sc is not None else {}
            for k, v in rows.get(year, {}).items():   # older files: scalars only there
                attrs.setdefault({"loss_of_load": "load_shed"}.get(k, k), v)
            attrs = {k: _txt(v) for k, v in attrs.items()}
            y: dict = {"year": year,
                       "feasible": bool(attrs.get("feasible", True)),
                       "total_cost": _f(attrs.get("total_cost")),
                       "demand_mwh": _f(attrs.get("total_demand")),
                       "generation_mwh": _f(attrs.get("total_generation")),
                       "solve_time_s": _f(attrs.get("solve_time"))}
            if "costs" in secs:
                y["costs"] = _costs(path, f, year, attrs, out.checks)
            if "re" in secs:
                y["re"] = _re(attrs)
            if "shedding" in secs:
                y["shedding"] = (_shedding(sc, attrs, tres, num_nodes, notes) if sc is not None
                                 else {"energy_mwh": _f(attrs.get("load_shed")), "hours": None,
                                       "peak_mw": None, "share_of_demand": None})
            if "curtailment" in secs and sc is not None:
                y["curtailment"] = _curtailment(sc, attrs, tres, num_nodes, notes)
            if "emissions" in secs:
                y["emissions"] = _emissions(attrs)
            builds = None
            if ("builds" in secs or "storage" in secs) and key:
                builds = _builds(path, key, attrs, info)
                if "builds" in secs:
                    y["builds"] = builds
            if "capacity" in secs and sc is not None:
                y["capacity"] = _capacity(sc, notes)
            if "storage" in secs and sc is not None:
                y["storage"] = _storage(sc, builds)
            if "losses" in secs:
                y["losses"] = _losses(f, sc, attrs, year) if sc is not None else {
                    "energy_mwh": _f(attrs.get("transmission_losses")),
                    "share_of_demand": None, "loss_repair": None}
            if not y["feasible"]:
                out.checks.append({"code": "infeasible_year", "year": year,
                                   "message": f"Year {year}: some dispatch windows failed "
                                              "(no dispatch for their hours)"})
            out.years.append(y)
        # One note per kind, not one per year
        out.notes.extend(dict.fromkeys(notes))

        if "warnings" in secs:
            out.warnings = read_warnings(f)
            if out.warnings is None:
                out.notes.append("no /diagnostics/warnings (file written before ESFEX "
                                 "recorded the run's warnings)")
        if "binding" in secs:
            out.master = read_master_diagnostics(f, years=want)
            if out.master is None and meta.get("simulation_mode") == "development":
                out.notes.append("no /diagnostics/master (binding constraints not recorded)")

    out.totals = _totals(out.years)
    meta["status"] = _status(meta, out.years, all_years)
    meta["years"] = all_years
    return out


def _totals(years: list[dict]) -> dict:
    def _sum(sec: Optional[str], key: str):
        vals = [(y.get(sec) or {}).get(key) if sec else y.get(key) for y in years]
        vals = [v for v in vals if isinstance(v, (int, float))]
        return sum(vals) if vals else None

    return {"years": len(years), "total_cost": _sum(None, "total_cost"),
            "load_shed_mwh": _sum("shedding", "energy_mwh"),
            "curtailment_mwh": _sum("curtailment", "energy_mwh"),
            "co2_t": _sum("emissions", "co2_t"),
            "demand_mwh": _sum(None, "demand_mwh")}


def _status(meta: dict, years: list[dict], all_years: list[int]) -> str:
    """``complete``; ``incomplete`` (the run did not finish writing:
    still running, or it stopped); ``failed_windows`` (some year's dispatch
    windows failed); ``empty`` (no year written)."""
    if not all_years:
        return "empty"
    if not meta.get("export_complete"):
        return "incomplete"
    if any(not y.get("feasible", True) for y in years):
        return "failed_windows"
    return "complete"


__all__ = ["ResultsFileError", "RunSummary", "SECTIONS", "open_results", "summary"]
