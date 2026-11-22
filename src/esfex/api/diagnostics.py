"""Run diagnostics: the warnings a run logged, and the readers of the
``/diagnostics`` group of a results file.

During a run (``esfex run``, :meth:`esfex.runner.Orchestrator.run`, the
Studio, which runs ``esfex run``) a :class:`WarningsCollector` on the root
logger gathers every WARNING and ERROR record: ESFEX's own, the Julia
``@warn`` lines (forwarded to the ``esfex.julia`` logger) and Python
``warnings`` routed through logging. Records are grouped by *category*, the
logger plus the message with its numbers and quoted names taken out, so
"Generator 'pv' has 3 hours ..." and "Generator 'wind' has 7 hours ..." are
one kind. At the end the run logs a one-paragraph summary and writes the
groups to ``/diagnostics/warnings`` in the results file.

What is not collected: text Julia or a solver prints straight to the
process's stdout/stderr (solver logs, ``println``), which never becomes a
log record.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional, Union

# ── Categories ────────────────────────────────────────────────────────────

# %-style placeholders of a logging template: %r (a repr, quoted) and %s
# like a quoted name, numeric ones like a number
_PCT_NAME_RE = re.compile(r"%[-+ 0#]*\d*(?:\.\d+)?[rsa]")
_PCT_NUM_RE = re.compile(r"%[-+ 0#]*\d*(?:\.\d+)?[diouxXeEfFgG]")
# A quoted name: the quote opens after a non-word character (not the
# apostrophe of "ESFEX's") and closes before one
_QUOTED_RE = re.compile(r"(?<!\w)'[^'\n]{0,200}'(?!\w)|(?<!\w)\"[^\"\n]{0,200}\"(?!\w)")
_NUMBER_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)*(?:[eE][-+]?\d+)?")
_SPACE_RE = re.compile(r"\s+")
_CATEGORY_LEN = 160


def warning_category(logger_name: str, template: str) -> str:
    """The kind of a warning: ``"<logger>: <first line of the message>"``
    with quoted names (and ``%r``/``%s`` placeholders) replaced by ``'*'``
    and numbers (and numeric placeholders) by ``#``."""
    first = str(template).strip().splitlines()[0] if str(template).strip() else ""
    text = _PCT_NAME_RE.sub("'*'", first)
    text = _PCT_NUM_RE.sub("#", text)
    text = _QUOTED_RE.sub("'*'", text)
    text = _NUMBER_RE.sub("#", text)
    text = _SPACE_RE.sub(" ", text).strip()
    if len(text) > _CATEGORY_LEN:
        text = text[:_CATEGORY_LEN - 1] + "…"
    return f"{logger_name}: {text}"


def _record_template(record: logging.LogRecord) -> str:
    # A %-style call keeps its template: its arguments (names, numbers)
    # are not part of the kind. An f-string message is the text itself.
    if record.args and isinstance(record.msg, str):
        return record.msg
    try:
        return record.getMessage()
    except Exception:
        return str(record.msg)


# ── Collector ─────────────────────────────────────────────────────────────

@dataclass
class WarningGroup:
    """The records of one category: when the first came, the highest
    level, the logger, the first message in full and how many there were."""

    category: str
    level: str
    logger: str
    message: str
    time: str
    count: int = 1

    def to_dict(self) -> dict:
        return {"time": self.time, "level": self.level, "logger": self.logger,
                "category": self.category, "message": self.message, "count": self.count}


@dataclass
class WarningsSummary:
    """The warnings of a run, grouped by category (most frequent first)."""

    total: int = 0
    groups: list[WarningGroup] = field(default_factory=list)

    @property
    def kinds(self) -> int:
        return len(self.groups)

    @property
    def count_by_category(self) -> dict[str, int]:
        return {g.category: g.count for g in self.groups}

    @property
    def errors(self) -> int:
        return sum(g.count for g in self.groups if g.level in ("ERROR", "CRITICAL"))

    def to_dict(self) -> dict:
        return {"total": self.total, "kinds": self.kinds, "errors": self.errors,
                "groups": [g.to_dict() for g in self.groups]}

    def text(self, top: int = 5, width: int = 110) -> str:
        """``"12 warnings in 4 kinds: 5× ..., 3× ..."``, the *top* most
        frequent kinds with their first message; ``""`` without warnings."""
        if not self.total:
            return ""
        noun = "warning" if self.total == 1 else "warnings"
        kinds = "kind" if self.kinds == 1 else "kinds"
        lines = [f"{self.total} {noun} in {self.kinds} {kinds}:"]
        for g in self.groups[:top]:
            msg = _SPACE_RE.sub(" ", g.message.splitlines()[0] if g.message else "")
            if len(msg) > width:
                msg = msg[:width - 1] + "…"
            lines.append(f"  {g.count}× [{g.level}] {g.logger}: {msg}")
        if self.kinds > top:
            lines.append(f"  … and {self.kinds - top} more kinds (see /diagnostics/warnings "
                         "or `esfex summary --section warnings`)")
        return "\n".join(lines)


class WarningsCollector(logging.Handler):
    """Logging handler grouping WARNING+ records by :func:`warning_category`."""

    def __init__(self, level: int = logging.WARNING):
        super().__init__(level)
        self._groups: dict[str, WarningGroup] = {}
        self._total = 0
        self._glock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            category = warning_category(record.name, _record_template(record))
            try:
                message = record.getMessage()
            except Exception:
                message = str(record.msg)
            with self._glock:
                self._total += 1
                g = self._groups.get(category)
                if g is None:
                    self._groups[category] = WarningGroup(
                        category=category, level=record.levelname, logger=record.name,
                        message=message,
                        time=datetime.fromtimestamp(record.created).isoformat(timespec="seconds"))
                else:
                    g.count += 1
                    if record.levelno > logging.getLevelName(g.level):
                        g.level = record.levelname
        except Exception:  # a log handler must never raise into the caller
            pass

    def summary(self) -> WarningsSummary:
        with self._glock:
            groups = sorted((WarningGroup(**vars(g)) for g in self._groups.values()),
                            key=lambda g: (-g.count, g.time))
            return WarningsSummary(total=self._total, groups=groups)

    def clear(self) -> None:
        with self._glock:
            self._groups.clear()
            self._total = 0

    def merge(self, summary: Union["WarningsSummary", dict, None]) -> None:
        """Add the groups of an earlier *summary* (a :class:`WarningsSummary`
        or its ``to_dict()``): a resumed run goes on counting from the
        warnings of the run it resumes."""
        if not summary:
            return
        if isinstance(summary, WarningsSummary):
            summary = summary.to_dict()
        with self._glock:
            for g in summary.get("groups") or []:
                count = int(g.get("count", 1))
                cur = self._groups.get(g["category"])
                if cur is None:
                    self._groups[g["category"]] = WarningGroup(
                        category=g["category"], level=g.get("level", "WARNING"),
                        logger=g.get("logger", ""), message=g.get("message", ""),
                        time=g.get("time", ""), count=count)
                else:
                    cur.count += count
                    if logging.getLevelName(g.get("level", "WARNING")) > \
                            logging.getLevelName(cur.level):
                        cur.level = g["level"]
                    if g.get("time", "") and g["time"] < cur.time:
                        cur.time, cur.message = g["time"], g.get("message", cur.message)
            self._total += int(summary.get("total", 0))


_active: list[WarningsCollector] = []
_active_lock = threading.Lock()


def active_collector() -> Optional[WarningsCollector]:
    """The collector of the run in progress, if any."""
    return _active[-1] if _active else None


@contextmanager
def collect_warnings() -> Iterator[WarningsCollector]:
    """Collect the WARNING+ records logged inside the block.

    Nested use shares the outer collector (``esfex run`` collects from the
    configuration load on; the Orchestrator it starts then adds to the same
    one instead of counting the run's records twice).
    """
    with _active_lock:
        outer = _active[-1] if _active else None
        if outer is None:
            collector = WarningsCollector()
            logging.getLogger().addHandler(collector)
            _active.append(collector)
    if outer is not None:
        yield outer
        return
    try:
        yield collector
    finally:
        with _active_lock:
            logging.getLogger().removeHandler(collector)
            if collector in _active:
                _active.remove(collector)


# ── /diagnostics/warnings ─────────────────────────────────────────────────

WARNINGS_GROUP = "diagnostics/warnings"


def write_warnings(h5_path: Union[str, Path], summary: WarningsSummary) -> None:
    """Write *summary* to ``/diagnostics/warnings`` of *h5_path* (replacing
    an earlier one): dataset ``records``, one row per category, and the
    attributes ``count_by_category`` (JSON), ``total`` and ``kinds``."""
    import h5py
    import numpy as np

    s = h5py.string_dtype("utf-8")
    dtype = np.dtype([("time", s), ("level", s), ("logger", s), ("category", s),
                      ("message", s), ("count", np.int64)])
    rows = np.array([(g.time, g.level, g.logger, g.category, g.message, g.count)
                     for g in summary.groups], dtype=dtype)
    with h5py.File(h5_path, "a") as f:
        diag = f.require_group("diagnostics")
        if "warnings" in diag:
            del diag["warnings"]
        grp = diag.create_group("warnings")
        grp.create_dataset("records", data=rows)
        grp.attrs["count_by_category"] = json.dumps(summary.count_by_category)
        grp.attrs["total"] = int(summary.total)
        grp.attrs["kinds"] = int(summary.kinds)


def _text(v: Any) -> Any:
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    try:
        import numpy as np

        if isinstance(v, np.generic):
            return v.item()
    except ImportError:  # pragma: no cover
        pass
    return v


def read_table(obj) -> list[dict]:
    """Rows of a diagnostics table, whichever way it was written: a compound
    dataset (one field per column), a 1-D string dataset with one JSON
    object per row, a scalar JSON string holding a list, or a group of
    equal-length column datasets."""
    import h5py
    import numpy as np

    if isinstance(obj, h5py.Group):
        cols = {k: obj[k][()] for k in obj if isinstance(obj[k], h5py.Dataset)}
        if not cols:
            return []
        n = min(len(np.atleast_1d(v)) for v in cols.values())
        return [{k: _text(np.atleast_1d(v)[i]) for k, v in cols.items()} for i in range(n)]
    data = obj[()]
    if obj.dtype.names:
        return [{name: _text(row[name]) for name in obj.dtype.names}
                for row in np.atleast_1d(data)]
    if obj.shape == ():
        val = json.loads(_text(data) or "[]")
        return val if isinstance(val, list) else [val]
    out = []
    for item in np.atleast_1d(data):
        item = _text(item)
        if isinstance(item, str):
            try:
                val = json.loads(item)
            except ValueError:
                val = {"value": item}
            out.append(val if isinstance(val, dict) else {"value": val})
        else:
            out.append({"value": item})
    return out


def read_warnings(h5_or_path) -> Optional[dict]:
    """``/diagnostics/warnings`` of a results file as
    ``{"total", "kinds", "errors", "groups": [...], "count_by_category"}``;
    ``None`` for a file written before it existed."""
    import h5py

    def _read(f):
        if WARNINGS_GROUP not in f:
            return None
        grp = f[WARNINGS_GROUP]
        groups = read_table(grp["records"]) if "records" in grp else []
        for g in groups:
            if "count" in g:
                g["count"] = int(g["count"])
        try:
            by_cat = json.loads(_text(grp.attrs.get("count_by_category", "{}")) or "{}")
        except ValueError:
            by_cat = {}
        if not by_cat:
            by_cat = {g.get("category", ""): int(g.get("count", 1)) for g in groups}
        total = int(grp.attrs.get("total", sum(by_cat.values())))
        errors = sum(int(g.get("count", 1)) for g in groups
                     if str(g.get("level", "")).upper() in ("ERROR", "CRITICAL"))
        return {"total": total, "kinds": int(grp.attrs.get("kinds", len(by_cat))),
                "errors": errors, "groups": groups, "count_by_category": by_cat}

    if isinstance(h5_or_path, (str, Path)):
        with h5py.File(h5_or_path, "r") as f:
            return _read(f)
    return _read(h5_or_path)


# ── /diagnostics/master (written by the master problem) ──────────────────

MASTER_GROUP = "diagnostics/master"
_YEAR_KEY_RE = re.compile(r"^year_(\d{4})$")


def master_key_year(key: str) -> Union[int, str]:
    """``year_2030`` → 2030; another group name (``all``: one solve for the
    whole horizon) stays as it is."""
    m = _YEAR_KEY_RE.match(key)
    return int(m.group(1)) if m else key


def read_master_diagnostics(h5_or_path, years: Optional[set] = None) -> Optional[dict]:
    """The binding constraints and paid slacks of each master solve,
    ``{year or "all": {"binding": [...], "slacks": [...], "top": [...]}}``,
    from ``/diagnostics/master``; ``None`` when the file has none. *years*
    keeps those years (and the non-year groups such as ``all``)."""
    import h5py

    def _read(f):
        if MASTER_GROUP not in f:
            return None
        out: dict = {}
        for key in f[MASTER_GROUP]:
            grp = f[MASTER_GROUP][key]
            if not isinstance(grp, h5py.Group):
                continue
            yk = master_key_year(key)
            if years is not None and isinstance(yk, int) and yk not in years:
                continue
            entry: dict = {k: _text(v) for k, v in grp.attrs.items()}
            for name in ("binding", "slacks", "top"):
                entry[name] = read_table(grp[name]) if name in grp else []
            out[str(yk)] = entry
        return out

    if isinstance(h5_or_path, (str, Path)):
        with h5py.File(h5_or_path, "r") as f:
            return _read(f)
    return _read(h5_or_path)


__all__ = [
    "WarningGroup", "WarningsCollector", "WarningsSummary", "active_collector",
    "collect_warnings", "master_key_year", "read_master_diagnostics", "read_table",
    "read_warnings", "warning_category", "write_warnings",
]
