"""Plain-Python interface to ESFEX for scripts, the CLI and agents.

Everything here returns plain data (dataclasses with ``to_dict()``, or
dicts) that serialises to JSON as it is. Nothing imports Qt, and importing
the package does not start Julia: only :func:`system_info` with
``probe=True`` and a run do.

- :func:`validate_case`, :func:`load_case`: check / load a configuration.
- :func:`summary`: per-year KPIs of a results file (read lazily).
- :func:`system_info`: versions, Julia, solvers, data, paths.
- :func:`collect_warnings`, :func:`read_warnings`,
  :func:`read_master_diagnostics`: the run's warnings and the
  ``/diagnostics`` group of a results file.
- :func:`envelope`: the JSON document of a ``--json`` command (see the
  "Using ESFEX from scripts and agents" guide for the contract).
"""

from esfex.api._common import ApiError, SCHEMA_VERSION, envelope, jsonable, schema_name
from esfex.api.cases import ValidationReport, case_summary, load_case, validate_case
from esfex.api.diagnostics import (
    WarningsCollector,
    WarningsSummary,
    collect_warnings,
    read_master_diagnostics,
    read_warnings,
    write_warnings,
)
from esfex.api.environment import system_info
from esfex.api.results import SECTIONS, ResultsFileError, RunSummary, summary

__all__ = [
    "ApiError", "ResultsFileError", "RunSummary", "SCHEMA_VERSION", "SECTIONS",
    "ValidationReport", "WarningsCollector", "WarningsSummary", "case_summary",
    "collect_warnings", "envelope", "jsonable", "load_case", "read_master_diagnostics",
    "read_warnings", "schema_name", "summary", "system_info", "validate_case",
    "write_warnings",
]
