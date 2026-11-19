"""Checkpoints of a myopic run, so that it can be resumed.

A myopic development run (``planning_mode: myopic``) first solves one master
problem per year, each starting from the fleet the years before it built,
then dispatches the years one by one. After every completed step (a master
year, then a dispatched year) the runner rewrites a *checkpoint* next to the
results file: ``<results>.checkpoint.json`` beside ``<results>.h5``.
``esfex run -c CASE --resume [RESULTS.h5]`` reads it, checks that the case
is the one that wrote it, drops the data of a year the interrupted run had
started but not finished from the results file, restores the state and
continues with the first year not completed, appending to the same file.

Why a sidecar and not a group of the HDF5 file: the checkpoint is replaced
atomically (written to a temporary file, then renamed), so a run killed
while writing it leaves the previous one intact; HDF5 cannot replace a
group atomically and never reclaims the space of a deleted one, so a group
rewritten every year would grow the file. The sidecar is plain JSON
(schema ``esfex.checkpoint/1``) an agent can read. Keep it with the results
file: without it the run cannot be resumed.

The document holds the ESFEX version, a hash of the model-relevant part of
the configuration (all of it except ``logging`` and the solver's
``verbose``, ``threads`` and ``time_limit``), the size and SHA-256 of every
input file the configuration names, the run's years, the phase reached, the
years completed, the warnings logged so far and the loop-carried state
(``state``: what each year hands to the next one). Arrays, tuples and
non-string dictionary keys are tagged (``{"__nd__": dtype, "shape",
"data"}``, ``{"__tuple__": [...]}``, ``{"__dict__": [[key, value], ...]}``,
``{"__float__": "inf"}``) so the state reads back exactly.

No Julia import here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

import numpy as np

from esfex.api._common import ApiError

logger = logging.getLogger(__name__)

SCHEMA = "esfex.checkpoint/1"
SUFFIX = ".checkpoint.json"

# Phases of a myopic run, in order: the yearly masters, then the dispatch
PHASE_MASTER = "master"
PHASE_DISPATCH = "dispatch"
PHASE_COMPLETE = "complete"

# Configuration fields that do not change the model
_HASH_EXCLUDE = {"logging": True, "solver": {"verbose", "threads", "time_limit"}}

# Results-file groups holding one ``year_{YYYY}...`` member per dispatched year
_YEAR_CONTAINERS = ("detailed_results", "cost_breakdown", "demand",
                    "global/cost_breakdown", "global/demand",
                    "inter_system/detailed_results")
# Results-file groups holding one row per dispatched year (``year`` column)
_ROW_CONTAINERS = ("summary_results", "global/summary_results")
_YEAR_MEMBER_RE = re.compile(r"^year_(\d{4})(?:_|$)")

_TAGS = frozenset({"__nd__", "__tuple__", "__dict__", "__set__", "__float__"})


def checkpoint_path(results: Union[str, Path]) -> Path:
    """The checkpoint of results file *results*: ``x.h5`` → ``x.checkpoint.json``."""
    return Path(results).with_suffix(SUFFIX)


# ── Encoding ──────────────────────────────────────────────────────────────

def _float(x: float) -> Any:
    return x if math.isfinite(x) else {"__float__": repr(x)}


def encode(obj: Any) -> Any:
    """*obj* as strict JSON data that :func:`decode` turns back into it:
    numpy arrays and scalars, tuples, sets and dictionaries with non-string
    keys are tagged; non-finite floats too. Raises ``TypeError`` for
    anything else (a model object has no place in a checkpoint)."""
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        return _float(float(obj))
    if isinstance(obj, np.ndarray):
        if obj.dtype.kind in "biufU":
            flat = obj.ravel().tolist()
            if obj.dtype.kind == "f" and not np.all(np.isfinite(obj)):
                flat = [_float(v) for v in flat]
            return {"__nd__": obj.dtype.str, "shape": list(obj.shape), "data": flat}
        return {"__nd__": "object", "shape": list(obj.shape),
                "data": [encode(v) for v in obj.ravel().tolist()]}
    if isinstance(obj, tuple):
        return {"__tuple__": [encode(v) for v in obj]}
    if isinstance(obj, (set, frozenset)):
        return {"__set__": [encode(v) for v in sorted(obj, key=repr)]}
    if isinstance(obj, dict):
        if all(isinstance(k, str) for k in obj) and not (_TAGS & set(obj)):
            return {k: encode(v) for k, v in obj.items()}
        return {"__dict__": [[encode(k), encode(v)] for k, v in obj.items()]}
    if isinstance(obj, list):
        return [encode(v) for v in obj]
    raise TypeError(f"cannot write a {type(obj).__name__} to a checkpoint")


def decode(obj: Any) -> Any:
    """Inverse of :func:`encode`."""
    if isinstance(obj, list):
        return [decode(v) for v in obj]
    if not isinstance(obj, dict):
        return obj
    if len(obj) == 1:
        (tag, val), = obj.items()
        if tag == "__float__":
            return float(val)
        if tag == "__tuple__":
            return tuple(decode(v) for v in val)
        if tag == "__set__":
            return {decode(v) for v in val}
        if tag == "__dict__":
            return {_hashable(decode(k)): decode(v) for k, v in val}
    if "__nd__" in obj:
        dtype, shape = obj["__nd__"], obj["shape"]
        data = [decode(v) for v in obj["data"]]
        if dtype == "object":
            arr = np.empty(len(data), dtype=object)
            arr[:] = data
            return arr.reshape(shape)
        return np.array(data, dtype=np.dtype(dtype)).reshape(shape)
    return {k: decode(v) for k, v in obj.items()}


def _hashable(key: Any) -> Any:
    return tuple(_hashable(k) for k in key) if isinstance(key, (list, tuple)) else key


# ── Fingerprint of the case ───────────────────────────────────────────────

def config_hash(cfg) -> str:
    """SHA-256 of the model-relevant part of *cfg* (an ``ESFEXConfig``):
    everything but ``logging`` and the solver's ``verbose``, ``threads`` and
    ``time_limit``, which change how the run reports or how long it may
    take, not what it solves."""
    dump = cfg.model_dump(mode="json", exclude=_HASH_EXCLUDE)
    text = json.dumps(dump, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _strings(obj: Any):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def input_files(cfg, config_dir: Union[str, Path]) -> dict[str, dict]:
    """The input files *cfg* names (any string of the configuration that is
    the path of an existing file, relative ones under *config_dir*), as
    ``{path as written: {"size", "sha256"}}``. The content hash, not the
    modification time: copying a case to another machine keeps it."""
    from esfex.utils.paths import resolve_project_data_file

    config_dir = Path(config_dir)
    out: dict[str, dict] = {}
    for ref in _strings(cfg.model_dump(mode="json")):
        if not ref or len(ref) > 1024 or "\n" in ref or ref in out:
            continue
        try:
            path = resolve_project_data_file(config_dir, ref)
            if not path.is_file():
                continue
            out[ref] = {"size": path.stat().st_size, "sha256": _sha256(path)}
        except (OSError, ValueError):
            continue
    return dict(sorted(out.items()))


def fingerprint(cfg, config_path: Optional[Union[str, Path]]) -> dict:
    """``{"config_hash", "data_files", "esfex_version"}`` of a case."""
    try:
        from esfex import __version__ as version
    except ImportError:  # pragma: no cover
        version = "unknown"
    config_dir = Path(config_path).resolve().parent if config_path else Path.cwd()
    return {"esfex_version": str(version), "config_hash": config_hash(cfg),
            "data_files": input_files(cfg, config_dir)}


# ── Reading and writing ───────────────────────────────────────────────────

def write_checkpoint(path: Union[str, Path], doc: dict) -> None:
    """Write *doc* to *path* atomically: a temporary file in the same
    directory, flushed to disk, then renamed over *path*."""
    path = Path(path)
    doc = dict(doc, schema=SCHEMA, updated=datetime.now().isoformat(timespec="seconds"))
    text = json.dumps(encode(doc), separators=(",", ":"), allow_nan=False)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_checkpoint(path: Union[str, Path]) -> dict:
    """The checkpoint document at *path* (``state`` decoded)."""
    with open(path, encoding="utf-8") as f:
        return decode(json.load(f))


def checkpoint_info(results: Union[str, Path]) -> Optional[dict]:
    """What the checkpoint of results file *results* says, without its
    ``state``: schema, status, phase, years completed, case fingerprint.
    ``None`` when the file has no checkpoint."""
    path = checkpoint_path(results)
    if not path.is_file():
        return None
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    doc.pop("state", None)
    return decode(doc)


def newest_results_file(output_dir: Union[str, Path],
                        config_path: Union[str, Path]) -> Optional[Path]:
    """The newest results file of the case *config_path* in *output_dir*
    (``<config stem>_<ddmmyyyy>_<HHMM>.h5``, by modification time)."""
    stem = Path(config_path).stem
    pattern = re.compile(re.escape(stem) + r"_\d{8}_\d{4}\.h5$")
    found = [p for p in Path(output_dir).glob(f"{stem}_*.h5") if pattern.match(p.name)]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


# ── Resume checks ─────────────────────────────────────────────────────────

class ResumeError(RuntimeError):
    """A run cannot be resumed; ``errors`` are the reasons
    (:class:`~esfex.api.ApiError`)."""

    def __init__(self, errors: list[ApiError]):
        self.errors = list(errors)
        super().__init__("; ".join(e.message for e in self.errors))


@dataclass
class ResumePlan:
    """A checked checkpoint: where a resumed run starts."""

    results: Path
    checkpoint: Path
    doc: dict
    # Warnings for the run's log (--force-resume past a difference)
    forced: list[str] = field(default_factory=list)

    @property
    def phase(self) -> str:
        return self.doc["phase"]

    @property
    def state(self) -> dict:
        return self.doc.get("state") or {}

    @property
    def master_years(self) -> list[int]:
        return [int(y) for y in self.doc.get("master", {}).get("completed_years", [])]

    @property
    def dispatch_years(self) -> list[int]:
        return [int(r["year"]) for r in self.doc.get("dispatch", {}).get("completed", [])]

    @property
    def resume_year(self) -> Optional[int]:
        """The first year the resumed run solves (a master year, or a
        dispatch year once every master year was solved); None when every
        year was dispatched and only the file's finishing was left."""
        at = self.doc.get("resume_at")
        return int(at["year"]) if at else None

    def to_dict(self) -> dict:
        return {"results_file": str(self.results), "checkpoint": str(self.checkpoint),
                "phase": self.phase, "resumed_from": self.resume_year,
                "master_years_completed": self.master_years,
                "dispatch_years_completed": self.dispatch_years,
                "forced": list(self.forced)}


_FORCE_FIX = ("Restore the case as it was, start a new run without --resume, or pass "
              "--force-resume to continue anyway (the years already solved keep the "
              "old case's results).")


def check_resume(results: Union[str, Path], cfg, config_path: Optional[Union[str, Path]],
                 *, years: Optional[int] = None, start_year: Optional[int] = None,
                 force: bool = False, case: Optional[dict] = None) -> ResumePlan:
    """Check that the run behind *results* can be resumed with case *cfg*.

    Refuses (:class:`ResumeError`) when the results file or its checkpoint
    is missing or unreadable, the run or the case is not a myopic
    development run, the run already completed, *years* / *start_year*
    differ from the run's, or (unless *force*) the case, its input files or
    the ESFEX version changed since the checkpoint was written. *case* is
    the case's :func:`fingerprint` when already taken."""
    results = Path(results)
    if not results.is_file():
        raise ResumeError([ApiError(
            "results_not_found", f"Results file not found: {results}", key=str(results),
            fix="Pass the results file of the interrupted run: esfex run -c CASE --resume FILE.h5")])
    ckpt = checkpoint_path(results)
    attrs = _results_attrs(results)
    if not ckpt.is_file():
        mode = attrs.get("simulation_mode")
        planning = attrs.get("planning_mode")
        if (mode and mode != "development") or (planning and planning != "myopic"):
            raise ResumeError([_not_resumable(
                f"{results.name} is a {mode or 'development'} run"
                + (f" with planning_mode {planning}" if planning else ""), str(results))])
        raise ResumeError([ApiError(
            "checkpoint_missing", f"{results.name} has no checkpoint ({ckpt.name})",
            key=str(ckpt),
            fix="Only myopic development runs of ESFEX 0.4 or later write one; keep it next "
                "to the results file. Without it, start a new run.")])
    try:
        doc = read_checkpoint(ckpt)
    except (OSError, ValueError) as e:
        raise ResumeError([ApiError("checkpoint_unreadable", f"{ckpt.name}: {e}",
                                    key=str(ckpt), fix="Start a new run.")]) from e
    if doc.get("schema") != SCHEMA:
        raise ResumeError([ApiError(
            "checkpoint_unreadable",
            f"{ckpt.name} has schema {doc.get('schema')!r}, this ESFEX reads {SCHEMA!r}",
            key=str(ckpt), fix="Resume with the ESFEX version that wrote it, or start a new run.")])
    if doc.get("status") == PHASE_COMPLETE or doc.get("phase") == PHASE_COMPLETE:
        raise ResumeError([ApiError(
            "run_complete", f"The run in {results.name} already completed", key=str(results),
            fix="Nothing to resume: read it with esfex summary, or start a new run.")])
    if attrs.get("_error"):
        raise ResumeError([ApiError(
            "results_unreadable", f"{results.name} cannot be opened: {attrs['_error']}",
            key=str(results), fix="The file was damaged (killed while it was written?); "
                                  "start a new run.")])

    sim_mode = getattr(cfg, "simulation_mode", None)
    planning = getattr(getattr(cfg, "master_problem", None), "planning_mode", None)
    if sim_mode != "development" or planning != "myopic":
        raise ResumeError([_not_resumable(
            f"the case is a {sim_mode} run with planning_mode {planning}", "simulation_mode")])

    errors: list[ApiError] = []
    run = doc.get("run", {})
    if years is not None and int(years) != int(run.get("years", -1)):
        errors.append(ApiError(
            "years_mismatch", f"--years {years}: the run has {run.get('years')} years",
            key="--years", fix="Leave --years out when resuming."))
    if start_year is not None and int(start_year) != int(run.get("start_year", -1)):
        errors.append(ApiError(
            "years_mismatch", f"--start-year {start_year}: the run starts in "
            f"{run.get('start_year')}", key="--start-year",
            fix="Leave --start-year out when resuming."))
    if errors:
        raise ResumeError(errors)

    now = case or fingerprint(cfg, config_path)
    then = doc.get("case", {})
    changes: list[ApiError] = []
    if now["config_hash"] != then.get("config_hash"):
        changes.append(ApiError(
            "config_changed", "The case's configuration changed since the run started "
            "(model-relevant settings; logging and solver verbose/threads/time_limit "
            "excepted)", key=str(config_path or ""), fix=_FORCE_FIX))
    old_files = then.get("data_files", {}) or {}
    changed = sorted(k for k in set(old_files) | set(now["data_files"])
                     if old_files.get(k) != now["data_files"].get(k))
    if changed:
        shown = ", ".join(changed[:5]) + (f" and {len(changed) - 5} more" if len(changed) > 5 else "")
        changes.append(ApiError(
            "data_changed", f"Input files changed since the run started: {shown}",
            key=changed[0], fix=_FORCE_FIX))
    if now["esfex_version"] != then.get("esfex_version"):
        changes.append(ApiError(
            "version_changed", f"The run was started with ESFEX {then.get('esfex_version')}, "
            f"this is {now['esfex_version']}", key="esfex_version", fix=_FORCE_FIX))
    plan = ResumePlan(results=results, checkpoint=ckpt, doc=doc)
    if changes:
        if not force:
            raise ResumeError(changes)
        plan.forced = [e.message for e in changes]
    return plan


def _not_resumable(what: str, key: str) -> ApiError:
    return ApiError(
        "not_resumable", f"Only myopic development runs can be resumed: {what}", key=key,
        fix="Perfect-foresight and operations runs solve the horizon in one go and keep no "
            "checkpoint; start a new run.")


def _results_attrs(path: Path) -> dict:
    try:
        import h5py

        with h5py.File(path, "r") as f:
            return {k: (v.decode() if isinstance(v, bytes) else v) for k, v in f.attrs.items()}
    except Exception as e:  # damaged file
        return {"_error": f"{type(e).__name__}: {e}"}


# ── Results file ──────────────────────────────────────────────────────────

def truncate_results(results: Union[str, Path], keep_years: set[int]) -> list[int]:
    """Drop from results file *results* the per-year data of every year not
    in *keep_years* (what an interrupted run wrote of the year it did not
    finish): the ``year_{YYYY}...`` members of the per-year groups and the
    rows of the per-year summary tables. Marks the file not complete.
    Returns the years dropped."""
    import h5py

    dropped: set[int] = set()
    with h5py.File(results, "a") as f:
        for name in _YEAR_CONTAINERS:
            grp = f.get(name)
            if not isinstance(grp, h5py.Group):
                continue
            for member in list(grp):
                m = _YEAR_MEMBER_RE.match(member)
                if m and int(m.group(1)) not in keep_years:
                    del grp[member]
                    dropped.add(int(m.group(1)))
        for name in _ROW_CONTAINERS:
            grp = f.get(name)
            if not isinstance(grp, h5py.Group) or "year" not in grp:
                continue
            years = np.asarray(grp["year"][()]).astype(int)
            keep = np.isin(years, sorted(keep_years))
            if keep.all():
                continue
            dropped.update(int(y) for y in years[~keep])
            for col in list(grp):
                ds = grp[col]
                if isinstance(ds, h5py.Dataset) and ds.ndim == 1 and ds.shape[0] == len(years):
                    data = ds[()][keep]
                    ds.resize((len(data),))
                    if len(data):
                        ds[:] = data
        f.attrs["export_complete"] = False
    return sorted(dropped)


__all__ = [
    "PHASE_COMPLETE", "PHASE_DISPATCH", "PHASE_MASTER", "ResumeError", "ResumePlan",
    "SCHEMA", "check_resume", "checkpoint_info", "checkpoint_path", "config_hash", "decode",
    "encode", "fingerprint", "input_files", "newest_results_file", "read_checkpoint",
    "truncate_results", "write_checkpoint",
]
