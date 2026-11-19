"""Binding constraints, duals and paid slacks of the master problem.

After each master solve the Julia side (``ESFEX.master_diagnostics``) groups
the model's rows by constraint family and year and returns aggregates: per
family the rows that bind and their largest and summed |dual|, the top rows
by |dual|, and the slack and penalty variables the plan pays for. This
module turns that into plain Python data, logs a short digest and writes it
to the results file:

``/diagnostics/master/year_{YYYY}/binding``
    one row per constraint family: ``family, label, code, kind, units,
    count_binding, count_priced, count_total, max_abs_dual, sum_abs_dual``
``/diagnostics/master/year_{YYYY}/top``
    the rows of the largest |dual|: ``name, family, label, dual, units``
``/diagnostics/master/year_{YYYY}/slacks``
    paid slacks with value > tol: ``name, family, label, value,
    value_units, unit_cost, cost, count``

A master of several years (perfect foresight) puts the rows whose names
carry no year in group ``all``. Duals are the cost of tightening the row by
one unit (``$`` per unit, in the year's own money); see
``docs/reference/hdf5-output-schema.md``. Readers must tolerate the groups'
absence (files written before 0.4, Benders and MGA runs).

No Julia import here: the converter takes any object with the Julia
result's attributes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Union

import numpy as np

logger = logging.getLogger(__name__)

SCHEMA = "esfex.master_diagnostics/1"
ROOT = "diagnostics/master"
UNTAGGED_GROUP = "all"

BINDING_FIELDS = (
    ("family", str), ("label", str), ("code", str), ("kind", str), ("units", str),
    ("count_binding", int), ("count_priced", int), ("count_total", int),
    ("max_abs_dual", float), ("sum_abs_dual", float),
)
TOP_FIELDS = (
    ("name", str), ("family", str), ("label", str), ("dual", float), ("units", str),
)
SLACK_FIELDS = (
    ("name", str), ("family", str), ("label", str), ("value", float),
    ("value_units", str), ("unit_cost", float), ("cost", float), ("count", int),
)
TABLES = {"binding": BINDING_FIELDS, "top": TOP_FIELDS, "slacks": SLACK_FIELDS}

# Families the digest names first, then the rest by kind
_DIGEST_KINDS = ("policy", "build_limit", "other")


def group_name(year: Optional[int]) -> str:
    """HDF5 group of a year (``year_2025``), or ``all`` for untagged rows."""
    return UNTAGGED_GROUP if year is None else f"year_{int(year)}"


def from_julia(diag: Any, years: Iterable[int]) -> dict[str, dict]:
    """Groups ``{group name: {"year", "attrs", "binding", "top", "slacks"}}``
    from the Julia ``master_diagnostics`` result *diag* of a master over
    *years* (its year index ``y`` is ``years[y - 1]``; 0 is the untagged
    group)."""
    years = [int(y) for y in years]

    def year_of(idx: int) -> Optional[int]:
        idx = int(idx)
        return years[idx - 1] if 1 <= idx <= len(years) else None

    groups: dict[str, dict] = {}

    def group(idx: int) -> dict:
        year = year_of(idx)
        g = groups.get(group_name(year))
        if g is None:
            g = groups[group_name(year)] = {
                "year": year,
                "attrs": {"duals_available": bool(diag.duals_available),
                          "status": str(diag.status)},
                "binding": [], "top": [], "slacks": []}
        return g

    for table, fields in TABLES.items():
        for row in getattr(diag, table):
            group(row.year)[table].append(
                {k: cast(getattr(row, k)) for k, cast in fields})
    if not groups and years:
        # A solve with no rows to report still records its status
        group(1 if len(years) == 1 else 0)
    return groups


# ── Digest ──────────────────────────────────────────────────────────────

def _num(x: float) -> str:
    a = abs(x)
    if a >= 1e4:
        return f"{x:,.0f}"
    if a >= 10:
        return f"{x:.0f}"
    return f"{x:.3g}"


def _years(years: list[Optional[int]]) -> str:
    """``2025-2027, 2030`` (consecutive years as ranges)."""
    ys = sorted({y for y in years if y is not None})
    if not ys:
        return ""
    out, start, prev = [], ys[0], ys[0]
    for y in ys[1:] + [None]:
        if y is not None and y == prev + 1:
            prev = y
            continue
        out.append(str(start) if start == prev else f"{start}-{prev}")
        if y is not None:
            start = prev = y
    return ", ".join(out)


def digest_lines(groups: Mapping[str, dict], *, prefix: str = "Master",
                 max_items: int = 4) -> list[str]:
    """3-5 log lines saying why the plan looks as it does: the policy and
    build-limit families that bind with their largest dual, the operation
    families that bind most, the paid slacks, and the policy families that
    do not bind."""
    if not groups:
        return []
    duals = all(g["attrs"].get("duals_available", True) for g in groups.values())
    fam: dict[str, dict] = {}
    for g in groups.values():
        for r in g["binding"]:
            f = fam.setdefault(r["family"], {"row": r, "years": [], "max": 0.0,
                                              "max_year": None, "priced": 0,
                                              "binding": 0, "total": 0})
            f["total"] += r["count_total"]
            f["binding"] += r["count_binding"]
            f["priced"] += r["count_priced"]
            hit = r["count_priced"] if duals else r["count_binding"]
            if hit:
                f["years"].append(g["year"])
            m = r["max_abs_dual"]
            if duals and np.isfinite(m) and m > f["max"]:
                f["max"], f["max_year"] = m, g["year"]
    paid = {s["family"] for g in groups.values() for s in g["slacks"]}

    def binds(f):
        return (f["priced"] if duals else f["binding"]) > 0

    def item(f):
        r = f["row"]
        yrs = _years(f["years"])
        where = f" {yrs}" if yrs and len(groups) > 1 else ""
        if not duals:
            return f"{r['label']}{where} ({f['binding']}/{f['total']} rows tight)"
        at = ", at its penalty" if r["family"] in paid else ""
        peak = (f", {f['max_year']}" if f["max_year"] is not None and len(f["years"]) > 1
                else "")
        return f"{r['label']}{where} ({_num(f['max'])} {r['units']}{peak}{at})"

    lines = []
    head = "" if duals else " (no duals: tight rows only)"
    policy = sorted((f for f in fam.values()
                     if f["row"]["kind"] in _DIGEST_KINDS and binds(f)),
                    key=lambda f: (_DIGEST_KINDS.index(f["row"]["kind"]), -f["max"]))
    shown = policy[:max_items]
    more = f" (+{len(policy) - max_items} more)" if len(policy) > max_items else ""
    lines.append(f"{prefix} binding{head}: "
                 + ("; ".join(item(f) for f in shown) + more if shown else "no policy "
                    "or build-limit constraint"))
    oper = sorted((f for f in fam.values() if f["row"]["kind"] == "operation" and binds(f)),
                  key=lambda f: -(f["priced"] if duals else f["binding"]))
    if oper:
        lines.append(f"{prefix} operation binding: " + "; ".join(
            f"{f['row']['label']} {f['priced'] if duals else f['binding']}/{f['total']} rows"
            + (f" (max {_num(f['max'])} {f['row']['units']})" if duals else "")
            for f in oper[:max_items]))
    slacks = sorted((s | {"year": g["year"]} for g in groups.values() for s in g["slacks"]),
                    key=lambda s: -s["cost"])
    def amount(s):
        u = s["value_units"]
        return f"${_num(s['value'])}" if u == "$" else f"{_num(s['value'])} {u}"

    if slacks:
        lines.append(f"{prefix} paid slacks: " + "; ".join(
            f"{s['label']}{' ' + str(s['year']) if s['year'] and len(groups) > 1 else ''} "
            f"{amount(s)} at {_num(s['unit_cost'])} $/{s['value_units'].split('/')[0]} "
            f"(${_num(s['cost'])})"
            for s in slacks[:max_items]))
    else:
        lines.append(f"{prefix} paid slacks: none")
    idle = [f["row"]["label"] for f in fam.values()
            if f["row"]["kind"] == "policy" and not binds(f)]
    if idle:
        lines.append(f"{prefix} not binding: " + ", ".join(sorted(set(idle))))
    return lines


# ── HDF5 ────────────────────────────────────────────────────────────────

def _table(rows: list[dict], fields) -> np.ndarray:
    import h5py
    str_dt = h5py.string_dtype()
    dt = np.dtype([(k, str_dt if t is str else (np.int64 if t is int else np.float64))
                   for k, t in fields])
    arr = np.empty(len(rows), dtype=dt)
    for i, r in enumerate(rows):
        arr[i] = tuple(r[k] for k, _ in fields)
    return arr


def write_master_diagnostics(target: Union[str, Path, Any], groups: Mapping[str, dict],
                             **attrs: Any) -> None:
    """Write *groups* (``from_julia``) under ``/diagnostics/master`` of the
    results file *target* (a path or an open ``h5py.File``), replacing the
    groups of the same name; *attrs* go on ``/diagnostics/master``."""
    import h5py

    if not isinstance(target, h5py.Group):
        with h5py.File(target, "a") as f:
            write_master_diagnostics(f, groups, **attrs)
        return
    root = target.require_group(ROOT)
    root.attrs["schema"] = SCHEMA
    root.attrs["description"] = (
        "Binding constraint families, top duals and paid slacks of each master solve; "
        "duals are the cost of tightening a row by one unit, in $ of the row's year")
    for k, v in attrs.items():
        root.attrs[k] = v
    for gname, g in groups.items():
        if gname in root:
            del root[gname]
        grp = root.create_group(gname)
        if g.get("year") is not None:
            grp.attrs["year"] = int(g["year"])
        for k, v in g.get("attrs", {}).items():
            grp.attrs[k] = v
        for table, fields in TABLES.items():
            grp.create_dataset(table, data=_table(g.get(table, []), fields))


def read_master_diagnostics(source: Union[str, Path, Any]) -> dict[str, dict]:
    """The groups under ``/diagnostics/master`` of a results file (a path or
    an open ``h5py.File``) as written by :func:`write_master_diagnostics`;
    ``{}`` when the file has none."""
    import h5py

    if not isinstance(source, h5py.Group):
        with h5py.File(source, "r") as f:
            return read_master_diagnostics(f)
    root = source.get(ROOT)
    if root is None:
        return {}
    out: dict[str, dict] = {}
    for gname, grp in root.items():
        if not isinstance(grp, h5py.Group):
            continue
        g = {"year": int(grp.attrs["year"]) if "year" in grp.attrs else None,
             "attrs": {k: (v.item() if hasattr(v, "item") else v)
                       for k, v in grp.attrs.items() if k != "year"},
             "binding": [], "top": [], "slacks": []}
        for table in TABLES:
            if table not in grp:
                continue
            data = grp[table][()]
            names = data.dtype.names or ()
            g[table] = [{k: (row[k].decode() if isinstance(row[k], bytes)
                             else row[k].item() if hasattr(row[k], "item") else row[k])
                         for k in names} for row in data]
        out[gname] = g
    return out
