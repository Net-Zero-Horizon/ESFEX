"""
Command-line interface for ESFEX.

Provides commands for running optimization, validating configuration,
and exporting results.
"""

import sys
import typing
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

app = typer.Typer(
    name="esfex",
    help="ESFEX: Energy System Flexibility — Power System Optimization",
    add_completion=False,
)
# Force UTF-8 output to avoid cp1252 UnicodeEncodeError on Windows.
# Under pythonw.exe (the console-less GUI launcher) there is no console,
# so sys.stdout/sys.stderr are None — guard before reconfiguring, or the
# whole module fails to import and the Studio never opens.
if sys.platform == "win32":
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.stderr is not None:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _force_blocking_stdio() -> None:
    """Force stdout/stderr file descriptors to blocking mode (POSIX only).

    Background: when ESFEX is launched as a subprocess by the GUI
    (``QProcess`` in python_console.py) and juliacall is initialised
    during the run, Julia's stdio setup leaves the parent Python's
    stdout/stderr file descriptors in ``O_NONBLOCK`` mode. Once that
    happens, a Rich Console write that doesn't fit in the pipe buffer
    raises ``BlockingIOError([Errno 11])`` instead of blocking until
    the GUI drains the pipe. The error then cascades through
    ``Progress.__exit__`` and the typer except hook, producing a
    hundreds-of-lines log spam and obscuring the real outcome.

    Reapplying blocking mode here, before any heavy work runs, removes
    the cascade at the root. Best-effort: silent no-op on Windows or
    when fcntl isn't available, or on fds that don't support it
    (already-redirected to a non-pipe).
    """
    try:
        import fcntl, os
    except ImportError:
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            fd = stream.fileno()
        except (AttributeError, OSError, ValueError):
            continue
        try:
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            if flags & os.O_NONBLOCK:
                fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
        except (OSError, ValueError):
            # Some fds (e.g. closed, non-seekable) reject F_SETFL.
            # Nothing useful to do — the cascade-handling fallback in
            # the except blocks below will catch BlockingIOError if it
            # still happens.
            continue


_force_blocking_stdio()

# Trim tracebacks. The default rich excepthook installed by typer
# dumps every local frame variable; when the locals include a
# ``ESFEXConfig`` object (~1000 fields, deeply nested), the output
# balloons to thousands of lines and on a slow pipe contributes to
# the BlockingIOError cascade _force_blocking_stdio fights against.
# ``show_locals=False`` keeps the frame chain readable; ``max_frames``
# caps the depth so even a recursive bug doesn't explode the output.
try:
    from rich.traceback import install as _install_rich_tb
    _install_rich_tb(show_locals=False, max_frames=10)
except Exception:
    # rich.traceback is best-effort presentation; never block startup
    # because the prettifier itself failed.
    pass

console = Console(force_terminal=True)

# Values `run --mode/--solver` accept: the schema's (esfex/__init__ already
# imports it, so this costs nothing at startup).
from esfex.config.schema import ESFEXConfig as _ESFEXConfig, SolverConfig as _SolverConfig
from esfex.utils.temporal import DEFAULT_RUN_YEARS

SIMULATION_MODES: tuple[str, ...] = typing.get_args(
    _ESFEXConfig.model_fields["simulation_mode"].annotation)
SOLVER_NAMES: tuple[str, ...] = typing.get_args(
    _SolverConfig.model_fields["name"].annotation)


# ── --json output ────────────────────────────────────────────────────────
#
# A --json command prints exactly one JSON object on stdout; everything
# else (progress, rich tables, Julia and solver output) goes to stderr.


def _flush_c_stdio() -> None:
    """Flush the C library's stdio buffers (a solver's printf output to a
    pipe sits there until exit, which would put it after the JSON)."""
    try:
        import ctypes

        libc = ctypes.cdll.msvcrt if sys.platform == "win32" else ctypes.CDLL(None)
        libc.fflush(None)
    except Exception:
        pass


class _StdoutToStderr:
    """Inside the block, writes to stdout land on stderr: Python's
    ``sys.stdout`` and the process's file descriptor 1 (so what Julia and
    the solvers print follows). :func:`_write_json` then prints the
    document on the real stdout."""

    def __enter__(self):
        import os

        self._real = sys.stdout
        self._saved_fd = None
        for stream in (sys.stdout, sys.__stdout__):
            try:
                stream.flush()
            except Exception:
                pass
        _flush_c_stdio()
        try:
            os.fstat(1)
            os.fstat(2)
            self._saved_fd = os.dup(1)
            os.dup2(2, 1)
        except OSError:  # no console (pythonw): Python-level only
            self._saved_fd = None
        sys.stdout = sys.stderr
        return self

    def __exit__(self, *exc):
        import os

        for stream in (sys.stderr, sys.__stdout__):
            try:
                stream.flush()
            except Exception:
                pass
        _flush_c_stdio()
        sys.stdout = self._real
        if self._saved_fd is not None:
            os.dup2(self._saved_fd, 1)
            os.close(self._saved_fd)
        return False


def _write_json(doc: dict) -> None:
    import json

    sys.stdout.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _check_mode(value: Optional[str]) -> Optional[str]:
    if value is not None and value not in SIMULATION_MODES:
        raise typer.BadParameter(
            f"'{value}' is not a simulation mode; use one of: {', '.join(SIMULATION_MODES)}")
    return value


def _check_solver(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if value.lower() not in SOLVER_NAMES:
        raise typer.BadParameter(
            f"'{value}' is not a supported solver; use one of: {', '.join(SOLVER_NAMES)}")
    return value.lower()


@app.command()
def run(
    config: Path = typer.Option(
        ...,
        "--config",
        "-c",
        help="Path to configuration YAML file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
    ),
    mode: Optional[str] = typer.Option(
        None,
        "--mode",
        "-m",
        help=f"Simulation mode: {', '.join(SIMULATION_MODES)}. "
             "If omitted, the value from the config file is used.",
        callback=_check_mode,
    ),
    solver: Optional[str] = typer.Option(
        None,
        "--solver",
        "-s",
        help=f"Override the config's solver: {', '.join(SOLVER_NAMES)}. "
             "If omitted, the value from the config file is used.",
        callback=_check_solver,
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Output directory for results (default: ./results)",
    ),
    years: Optional[int] = typer.Option(
        None,
        "--years",
        "-y",
        help=f"Number of years to simulate (default: {DEFAULT_RUN_YEARS})",
    ),
    start_year: Optional[int] = typer.Option(
        None,
        "--start-year",
        help="Label of the first simulated year (default: the year of date_start)",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose output",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate config and show plan without running optimization",
    ),
    json_out: bool = typer.Option(
        False, "--json",
        help="At the end print one JSON object (schema esfex.run/1) on stdout: status, "
             "output file, wall time, warnings, summary of the last year. Everything "
             "else goes to stderr",
    ),
):
    """
    Run the ESFEX optimization model.

    Executes capacity expansion and operational dispatch optimization
    based on the provided configuration file. Exit code 0 on success, 1 if
    the configuration is invalid or the run failed or is incomplete.
    """
    import time

    from esfex.api.diagnostics import collect_warnings

    state: dict = {"status": None, "errors": [], "output_dir": None,
                   "orchestrator": None, "case": None}
    args = (config, mode, solver, output, years, start_year, verbose, dry_run)
    if not json_out:
        with collect_warnings():
            _run_command(state, *args)
        return

    t0 = time.time()
    code = 0
    with collect_warnings() as collector, _StdoutToStderr():
        try:
            _run_command(state, *args)
        except typer.Exit as e:
            code = int(e.exit_code or 0)
        except Exception as e:  # never leave the caller without its JSON
            code = 1
            state["status"] = state["status"] or "failed"
            state["errors"].append(_run_error("run_error", f"{type(e).__name__}: {e}"))
        doc = _run_json_doc(state, code, time.time() - t0, collector, config)
    _write_json(doc)
    raise typer.Exit(code=code)


def _run_error(code: str, message: str, key: str = "", fix: str = ""):
    from esfex.api import ApiError

    return ApiError(code=code, message=message, key=key, fix=fix)


def _run_json_doc(state: dict, code: int, wall: float, collector, config: Path) -> dict:
    """The ``esfex.run/1`` document of a finished (or failed) ``run --json``."""
    from esfex.api import envelope
    from esfex.api import summary as _summary

    orch = state.get("orchestrator")
    out_file = getattr(orch, "_hdf5_path", None) if orch is not None else None
    wsum = getattr(orch, "warnings_summary", None) if orch is not None else None
    if wsum is None:
        wsum = collector.summary()
    status = state.get("status") or ("complete" if code == 0 else "failed")
    years = [r.year for r in (getattr(orch, "results", None) or [])]
    last_year = None
    if out_file is not None and Path(out_file).exists() and years:
        try:
            last_year = _summary(out_file, years=[years[-1]]).last_year()
        except Exception as e:
            state["errors"].append(_run_error("summary_error", f"{type(e).__name__}: {e}",
                                              key=str(out_file)))
    return envelope(
        "run", code == 0, errors=state["errors"], status=status, exit_code=code,
        config=str(config), output_dir=state.get("output_dir"),
        output_file=None if out_file is None else str(Path(out_file).resolve()),
        wall_time_s=round(wall, 3), years=years, warnings=wsum.to_dict(),
        last_year=last_year, case=state.get("case"))


def _run_command(state: dict, config: Path, mode: Optional[str], solver: Optional[str],
                 output: Optional[Path], years: Optional[int], start_year: Optional[int],
                 verbose: bool, dry_run: bool) -> None:
    """Body of ``esfex run``; records the outcome in *state* for ``--json``."""
    import logging
    import sys

    from esfex.api.cases import config_issue_error, case_summary, missing_data_files
    from esfex.config.loader import load_config, ConfigLoadError
    from esfex.runner import Orchestrator, RunAbortedError
    from esfex.logging_config import setup_console_logging

    # Install the (single) console handler at the right verbosity.
    # When the user passed --verbose, lock the level to debug so the
    # later runner-internal call doesn't reset it. Without --verbose,
    # we start at the default "basic" and let cfg.logging.console_level
    # (and then runner) refine it.
    setup_console_logging(level="debug" if verbose else "basic", force=verbose)

    if verbose:
        console.print(f"[bold blue]ESFEX[/bold blue] - Power System Optimization")
        console.print(f"Configuration: {config}")
        console.print(f"Mode: {mode or '(from config)'}")
        console.print(f"Solver: {solver or '(from config)'}")
        console.print()

    # Load and validate configuration
    try:
        with Progress(
            SpinnerColumn("line"),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            progress.add_task(description="Loading configuration...", total=None)
            cfg = load_config(config)
    except ConfigLoadError as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        state["status"] = "invalid_config"
        state["errors"] = [config_issue_error(i) for i in (getattr(e, "issues", None) or [
            {"key": "", "type": "load", "message": str(e), "input": None}])]
        raise typer.Exit(code=1)

    # Override config values from CLI
    if mode:
        cfg.simulation_mode = mode
    if solver:
        cfg.solver.name = solver
    if verbose:
        cfg.solver.verbose = True

    # Re-tune console logging once the yaml's `logging.console_level`
    # is available. --verbose on the CLI always wins (debug everywhere)
    # so the user can override what the yaml says without editing it.
    if not verbose:
        setup_console_logging(level=cfg.logging.console_level)

    # Align the solver's own verbosity with the console mode the user
    # picked.  Without this, a yaml shipped with ``solver.verbose: true``
    # would still flood a "basic" console with Gurobi simplex
    # iterations — defeating the whole point of choosing basic.
    #
    # Policy:
    #   basic   → force solver silent (no Gurobi log)
    #   debug   → force solver verbose (gives the developer full
    #             diagnostic output that pairs with debug logging)
    #   verbose → leave whatever the yaml said; the user is explicitly
    #             asking for an in-between mode and the per-yaml flag
    #             is the natural escape hatch
    if not verbose:
        _cl = cfg.logging.console_level
        if _cl == "basic":
            cfg.solver.verbose = False
        elif _cl == "debug":
            cfg.solver.verbose = True

    # Show configuration summary (only in verbose mode)
    if verbose:
        _show_config_summary(cfg)

    try:
        state["case"] = case_summary(cfg)
    except Exception:  # a mocked or partial config: the run decides
        state["case"] = None

    if dry_run:
        state["status"] = "dry_run"
        missing = missing_data_files(cfg, config.parent)
        if missing:
            state["errors"] = missing
            console.print(f"[red]{len(missing)} data file(s) the run reads cannot be found:[/red]")
            for err in missing:
                console.print(f"  {err.key}: {err.message}", markup=False)
            raise typer.Exit(code=1)
        console.print("\n[yellow]Dry run mode - not executing optimization[/yellow]")
        raise typer.Exit(code=0)

    # Create output directory
    output_dir = output or Path("./results")
    output_dir.mkdir(parents=True, exist_ok=True)
    state["output_dir"] = str(output_dir.resolve())

    def _safe_console_print(msg: str, *, style: str = "") -> None:
        """Print via rich, but if stdout is in non-blocking mode and the
        pipe is full (``BlockingIOError``), fall back to a raw fd write
        so the user sees something instead of a hundred-line cascade of
        '--- Logging error ---' messages. Re-applies blocking mode
        on the way out in case more output follows.
        """
        try:
            if style:
                console.print(f"[{style}]{msg}[/{style}]")
            else:
                console.print(msg)
        except BlockingIOError:
            _force_blocking_stdio()
            try:
                sys.__stderr__.write(msg + "\n")
                sys.__stderr__.flush()
            except Exception:
                pass

    # Re-apply blocking mode in case anything since import time (e.g. a
    # juliacall warmup) flipped the fds back into O_NONBLOCK.
    _force_blocking_stdio()

    # Run optimization
    try:
        orchestrator = Orchestrator(cfg, output_dir=output_dir, config_path=config)
        state["orchestrator"] = orchestrator
        results = orchestrator.run(years=years, start_year=start_year)

        # A year some of whose dispatch windows failed has hours without a
        # dispatch: the run is incomplete, not a success
        incomplete = [r.year for r in results or [] if not getattr(r, "feasible", True)]
        if incomplete:
            _safe_console_print(
                "\nOptimization finished with errors: operational windows of "
                f"year(s) {', '.join(map(str, incomplete))} failed to solve and "
                "have no dispatch (see the log).", style="red")
            _safe_console_print(f"Partial results saved to: {output_dir}")
            state["status"] = "failed_windows"
            state["errors"].append(_run_error(
                "failed_windows", f"Operational windows of year(s) "
                f"{', '.join(map(str, incomplete))} failed to solve and have no dispatch",
                fix="See the log for the windows' solver status."))
            raise typer.Exit(code=1)
        state["status"] = "complete"
        _safe_console_print("\nOptimization completed successfully!", style="green")
        _safe_console_print(f"Results saved to: {output_dir}")

    except typer.Exit:
        raise
    except RunAbortedError as e:
        _force_blocking_stdio()
        _safe_console_print(f"\nOptimization failed: {e}", style="red")
        state["status"] = "aborted"
        state["errors"].append(_run_error("aborted", str(e)))
        raise typer.Exit(code=1)
    except BlockingIOError as e:
        # Stdout pipe was non-blocking and full (typical when GUI
        # subprocess can't drain stdout fast enough). Try once more
        # after re-blocking; otherwise just exit quietly with the code.
        _force_blocking_stdio()
        try:
            sys.__stderr__.write(
                f"\nOptimization aborted: stdout pipe full ({e}). "
                "Increase the GUI console's drain rate or rerun from a "
                "terminal.\n"
            )
            sys.__stderr__.flush()
        except Exception:
            pass
        state["status"] = "failed"
        state["errors"].append(_run_error("stdout_pipe_full", str(e)))
        raise typer.Exit(code=1)
    except Exception as e:
        _force_blocking_stdio()
        _safe_console_print(f"\nOptimization failed: {e}", style="red")
        state["status"] = "failed"
        state["errors"].append(_run_error("run_error", f"{type(e).__name__}: {e}"))
        if verbose:
            import traceback
            _safe_console_print(traceback.format_exc())
        raise typer.Exit(code=1)


@app.command()
def validate(
    config: Path = typer.Option(
        ...,
        "--config",
        "-c",
        help="Path to configuration YAML file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
    ),
    json_out: bool = typer.Option(
        False, "--json",
        help="Print one JSON object (schema esfex.validate/1) on stdout instead of text",
    ),
):
    """
    Validate a configuration file.

    Checks that the configuration loads and that the demand and
    availability files it names exist. Exit code 0 if valid, 1 if not.
    """
    if json_out:
        from esfex.api import envelope, validate_case

        with _StdoutToStderr():
            report = validate_case(config)
        _write_json(envelope("validate", report.ok, errors=report.errors,
                             path=report.path, warnings=report.warnings,
                             case=report.summary))
        raise typer.Exit(code=0 if report.ok else 1)

    from esfex.config.loader import load_config, ConfigLoadError
    from esfex.api.cases import missing_data_files

    console.print(f"Validating: {config}")

    try:
        cfg = load_config(config)
        missing = missing_data_files(cfg, config.parent)
        if missing:
            console.print(f"[red]Validation failed:[/red] {len(missing)} data file(s) "
                          "the run reads cannot be found:")
            for err in missing:
                console.print(f"  {err.key}: {err.message}", markup=False)
            console.print(f"  ({missing[0].fix})", markup=False)
            raise typer.Exit(code=1)
        console.print("[green]Configuration is valid![/green]")
        if cfg.unknown_keys:
            console.print(f"[yellow]{len(cfg.unknown_keys)} unknown key(s) are ignored "
                          "(misspelt or obsolete?):[/yellow]")
            for key in cfg.unknown_keys:
                console.print(f"  {key}", markup=False)
        _show_config_summary(cfg)

    except ConfigLoadError as e:
        console.print(f"[red]Validation failed:[/red]\n{e}")
        raise typer.Exit(code=1)


@app.command()
def export(
    results: Path = typer.Option(
        ...,
        "--results",
        "-r",
        help="Path to HDF5 results file",
        exists=True,
    ),
    format: str = typer.Option(
        "csv",
        "--format",
        "-f",
        help="Export format: 'csv', 'excel', or 'json'",
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Output directory for exported files",
    ),
):
    """
    Export results to different formats.

    Converts HDF5 results to CSV, Excel, or JSON.
    """
    console.print(f"Exporting results from: {results}")
    console.print(f"Format: {format}")

    output_dir = output or results.parent / "export"
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        from esfex.io.exporter import ResultsExporter

        exporter = ResultsExporter(results)

        if format.lower() == "csv":
            exporter.to_csv(output_dir)
        elif format.lower() == "excel":
            exporter.to_excel(output_dir / f"{results.stem}.xlsx")
        elif format.lower() == "json":
            exporter.to_json(output_dir / f"{results.stem}.json")
        else:
            console.print(f"[red]Unknown format: {format}[/red]")
            raise typer.Exit(code=1)

        console.print(f"[green]Export completed![/green]")
        console.print(f"Output: {output_dir}")

    except Exception as e:
        console.print(f"[red]Export failed:[/red] {e}")
        raise typer.Exit(code=1)


def _split_sections(value: Optional[list[str]]) -> Optional[list[str]]:
    from esfex.api.results import SECTIONS

    if not value:
        return None
    out = [s.strip().lower() for v in value for s in v.split(",") if s.strip()]
    unknown = [s for s in out if s not in SECTIONS]
    if unknown:
        raise typer.BadParameter(
            f"unknown section(s) {', '.join(unknown)}; use any of: {', '.join(SECTIONS)}")
    return out


@app.command()
def summary(
    results: Path = typer.Argument(
        ..., help="HDF5 results file written by `esfex run`",
        exists=True, file_okay=True, dir_okay=False, readable=True,
    ),
    json_out: bool = typer.Option(
        False, "--json",
        help="Print one JSON object (schema esfex.summary/1) on stdout instead of tables",
    ),
    year: Optional[list[int]] = typer.Option(
        None, "--year", help="Only this year (repeat for several; default: all years)",
    ),
    section: Optional[list[str]] = typer.Option(
        None, "--section", callback=_split_sections,
        help="Only these sections (repeat or comma-separate): costs, re, shedding, "
             "curtailment, emissions, builds, capacity, storage, losses, warnings, binding",
    ),
):
    """
    Summarise a results file: per-year costs, RE share, shedding, builds, warnings.

    Reads only the attributes and the few small datasets it needs, so it is
    quick on large files. Exit code 0 when the file was read (whatever the
    run's outcome: see metadata.status and checks), 1 when it is not an
    ESFEX results file.
    """
    from esfex.api import ApiError, ResultsFileError, envelope
    from esfex.api import summary as _summary

    def _fail(err: ApiError):
        if json_out:
            _write_json(envelope("summary", False, errors=[err], path=str(results)))
        else:
            console.print(f"[red]Cannot summarise {results}:[/red] {err.message}", markup=True)
        raise typer.Exit(code=1)

    try:
        if json_out:
            with _StdoutToStderr():
                s = _summary(results, years=year, sections=section)
        else:
            s = _summary(results, years=year, sections=section)
    except ResultsFileError as e:
        _fail(e.error)
    except Exception as e:
        _fail(ApiError("read_error", f"{type(e).__name__}: {e}", key=str(results)))

    if json_out:
        _write_json(envelope("summary", True, **s.to_dict()))
        return
    _show_summary(s)


def _fmt(v, spec: str = ",.0f", none: str = "-") -> str:
    if v is None:
        return none
    try:
        return format(v, spec)
    except (TypeError, ValueError):
        return str(v)


def _show_summary(s) -> None:
    """Rich tables of an :class:`esfex.api.RunSummary`."""
    m = s.metadata
    console.print(f"[bold]{s.path}[/bold]", markup=True, highlight=False)
    console.print(
        f"ESFEX {m.get('esfex_version') or '?'} · {m.get('simulation_mode') or '?'} · "
        f"solver {m.get('solver') or '?'} · years {m.get('years_range') or '?'} · "
        f"{m.get('num_nodes') or '?'} nodes · status {m.get('status')}",
        markup=False, highlight=False)
    secs = set(s.sections)
    if s.years:
        t = Table(title="Per year")
        t.add_column("Year", justify="right")
        if "costs" in secs:
            t.add_column("Cost (M$)", justify="right")
        if "re" in secs:
            t.add_column("RE share / target", justify="right")
        if "shedding" in secs:
            t.add_column("Shed MWh (h)", justify="right")
        if "curtailment" in secs:
            t.add_column("Curtailed", justify="right")
        if "emissions" in secs:
            t.add_column("CO2 (kt)", justify="right")
        if "builds" in secs:
            t.add_column("Built MW", justify="right")
        if "losses" in secs:
            t.add_column("Losses MWh", justify="right")
        for y in s.years:
            row = [str(y["year"]) + ("" if y.get("feasible", True) else " !")]
            if "costs" in secs:
                c = y.get("costs") or {}
                tot = c.get("total")
                row.append(_fmt(None if tot is None else tot / 1e6, ",.2f"))
            if "re" in secs:
                r = y.get("re") or {}
                share = _fmt(r.get("share"), ".1%")
                tgt = r.get("target")
                row.append(share + (f" / {tgt:.1%}" if tgt else ""))
            if "shedding" in secs:
                sh = y.get("shedding") or {}
                row.append(f"{_fmt(sh.get('energy_mwh'), ',.1f')} ({_fmt(sh.get('hours'), ',.0f')})")
            if "curtailment" in secs:
                cu = y.get("curtailment") or {}
                row.append(_fmt(cu.get("share"), ".1%"))
            if "emissions" in secs:
                e = (y.get("emissions") or {}).get("co2_t")
                row.append(_fmt(None if e is None else e / 1e3, ",.1f"))
            if "builds" in secs:
                b = (y.get("builds") or {}).get("generation_mw") or {}
                row.append(_fmt(sum(b.values()), ",.1f"))
            if "losses" in secs:
                row.append(_fmt((y.get("losses") or {}).get("energy_mwh"), ",.0f"))
            t.add_row(*row)
        console.print(t)

    last = s.years[-1] if s.years else None
    if last and "costs" in secs and (last.get("costs") or {}).get("components"):
        c = last["costs"]
        t = Table(title=f"Cost components, {last['year']} (M$)")
        t.add_column("Component")
        t.add_column("M$", justify="right")
        for k, v in c["components"].items():
            if abs(v) >= 0.5:
                t.add_row(k, _fmt(v / 1e6, ",.3f"))
        chk = c.get("check") or {}
        t.add_row("[bold]total[/bold]", f"[bold]{_fmt(c['total'] / 1e6, ',.3f')}[/bold]"
                  + ("" if chk.get("ok", True) else " [red](≠ sum)[/red]"))
        console.print(t)
    if last and "builds" in secs and last.get("builds"):
        # Builds are per year: summed over the years shown. Retirements are
        # cumulative: the last year's.
        built: dict[tuple, float] = {}
        for y in s.years:
            for kind, unit in (("generation_mw", "MW"), ("storage_mw", "MW"),
                               ("storage_mwh", "MWh")):
                for k, v in ((y.get("builds") or {}).get(kind) or {}).items():
                    built[(k, unit)] = built.get((k, unit), 0.0) + v
        rows = [(k, f"{v:,.1f} {unit}") for (k, unit), v in built.items()]
        rows += [(k, f"retired {r['share']:.0%}")
                 for k, r in (last["builds"].get("retired") or {}).items()]
        if rows:
            span = (f"{s.years[0]['year']}-{last['year']}" if len(s.years) > 1
                    else str(last["year"]))
            t = Table(title=f"Built ({span}) and retired (by {last['year']})")
            t.add_column("Unit / technology")
            t.add_column("Amount", justify="right")
            for r in rows:
                t.add_row(*r)
            console.print(t)
    if "warnings" in secs and s.warnings:
        w = s.warnings
        console.print(f"\n[bold]Warnings:[/bold] {w['total']} in {w['kinds']} kinds")
        for g in w["groups"][:10]:
            msg = str(g.get("message", "")).splitlines()[0][:110]
            console.print(f"  {g.get('count')}× [{g.get('level')}] {g.get('logger')}: {msg}",
                          markup=False, highlight=False)
    if "binding" in secs and s.master:
        console.print("\n[bold]Master: binding constraints and paid slacks[/bold]")
        for yk, entry in s.master.items():
            fams = [b for b in entry.get("binding", []) if int(b.get("count_binding", 0) or 0) > 0]
            fams.sort(key=lambda b: -float(b.get("sum_abs_dual", 0) or 0))
            text = ", ".join(
                f"{b.get('family')} ({b.get('count_binding')}/{b.get('count_total')}, "
                f"max |dual| {_fmt(b.get('max_abs_dual'), ',.3g')} {b.get('units', '')})"
                for b in fams[:6]) or "none"
            console.print(f"  {yk}: {text}", markup=False, highlight=False)
            for sl in entry.get("slacks", [])[:5]:
                console.print(f"    slack {sl.get('name')}: {_fmt(sl.get('value'), ',.3g')} "
                              f"(cost {_fmt(sl.get('cost'), ',.0f')} $)",
                              markup=False, highlight=False)
    for chk in s.checks:
        console.print(f"[red]Check:[/red] {chk['message']}")
    for note in s.notes:
        console.print(f"[dim]Note: {note}[/dim]")


@app.command()
def studio(
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to existing YAML config to edit",
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="YAML path File > Save writes to (default: the --config file)",
    ),
):
    """
    Launch the GIS-based power system designer (Studio).

    Opens an interactive map-based GUI for creating and editing
    the power system configuration visually.
    """
    try:
        from esfex.visualization import launch_studio
    except ImportError:
        console.print(
            "[red]PySide6 is required for the Studio.[/red]\n"
            "It ships with esfex; reinstall with: "
            "pip install --upgrade --force-reinstall esfex"
        )
        raise typer.Exit(code=1)

    console.print("[bold blue]ESFEX[/bold blue] - Studio")
    if config:
        console.print(f"Loading: {config}")

    from esfex.config.loader import SavedConfigInvalidError

    try:
        result = launch_studio(config=str(config) if config else None, blocking=True,
                               output=str(output) if output else None)
    except SavedConfigInvalidError as e:
        # "Save anyway" on a configuration with errors: written, but invalid.
        console.print(f"[yellow]Configuration saved to {e.path}, "
                      f"but it does not validate:[/yellow]")
        console.print(str(e.error), markup=False)
        console.print(f"Fix it in the Studio, or check it with: esfex validate -c {e.path}",
                      markup=False)
        raise typer.Exit(code=1)

    if result:
        console.print("[green]Configuration saved.[/green]")
    else:
        console.print("[yellow]Studio closed without saving.[/yellow]")


# ── Plugin management sub-commands ────────────────────────────────────

plugin_app = typer.Typer(
    name="plugin",
    help="Manage ESFEX plugins (install, enable, disable, list).",
)
app.add_typer(plugin_app, name="plugin")


@plugin_app.command("list")
def plugin_list():
    """List all discovered plugins and their status."""
    from esfex.plugins import get_plugin_manager

    pm = get_plugin_manager()
    names = pm.discover()

    if not names:
        console.print("[yellow]No plugins found.[/yellow]")
        console.print(
            "Plugins are discovered from:\n"
            "  ~/.esfex/plugins/\n"
            "  $ESFEX_PLUGIN_PATH"
        )
        return

    table = Table(title="ESFEX Plugins")
    table.add_column("Name", style="cyan")
    table.add_column("Version", style="green")
    table.add_column("Category", style="blue")
    table.add_column("Status", style="bold")
    table.add_column("Description")

    for name in sorted(names):
        meta = pm.metas.get(name)
        if meta is None:
            continue
        enabled = pm.is_enabled(name)
        status = "[green]enabled[/green]" if enabled else "[red]disabled[/red]"
        if not pm.is_trusted(name):
            status += " [red]untrusted[/red]"
        table.add_row(name, meta.version, meta.category, status, meta.description)

    console.print(table)


@plugin_app.command("install")
def plugin_install(
    git: Optional[str] = typer.Option(None, "--git", help="Git URL to clone"),
    zip_file: Optional[Path] = typer.Option(
        None, "--zip", help="ZIP file to extract", exists=True
    ),
    name: Optional[str] = typer.Option(
        None, "--name", "-n", help="Target directory name (for git)"
    ),
    trust_host: bool = typer.Option(
        False, "--trust-host",
        help="Allow a git host outside the default list (github.com, "
             "gitlab.com, bitbucket.org, codeberg.org, git.sr.ht). The "
             "plugin's code runs with your permissions: use only for a "
             "source you trust.",
    ),
):
    """Install a plugin from a git repository or ZIP file."""
    from esfex.plugins import get_plugin_manager

    pm = get_plugin_manager()

    if git:
        try:
            installed = pm.install_from_git(
                git, target_name=name, trust_host=trust_host)
            console.print(f"[green]Installed plugin:[/green] {installed}")
        except Exception as e:
            console.print(f"[red]Install failed:[/red] {e}")
            raise typer.Exit(code=1)
    elif zip_file:
        try:
            installed = pm.install_from_zip(zip_file)
            console.print(f"[green]Installed plugin:[/green] {installed}")
        except Exception as e:
            console.print(f"[red]Install failed:[/red] {e}")
            raise typer.Exit(code=1)
    else:
        console.print("[red]Provide --git <url> or --zip <path>[/red]")
        raise typer.Exit(code=1)


@plugin_app.command("uninstall")
def plugin_uninstall(
    name: str = typer.Argument(..., help="Plugin name to uninstall"),
):
    """Uninstall a plugin by removing its directory."""
    from esfex.plugins import get_plugin_manager

    pm = get_plugin_manager()
    pm.uninstall(name)
    console.print(f"[green]Uninstalled plugin:[/green] {name}")


@plugin_app.command("trust")
def plugin_trust(
    name: str = typer.Argument(..., help="Plugin name to trust"),
):
    """Trust a plugin's current files so it loads (it runs with full access)."""
    from esfex.plugins import get_plugin_manager

    pm = get_plugin_manager()
    pm.discover()
    try:
        pm.trust_plugin(name)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1)
    console.print(f"[green]Trusted plugin:[/green] {name}")


@plugin_app.command("enable")
def plugin_enable(
    name: str = typer.Argument(..., help="Plugin name to enable"),
):
    """Enable a disabled plugin."""
    from esfex.plugins import get_plugin_manager

    get_plugin_manager().enable(name)
    console.print(f"[green]Enabled plugin:[/green] {name}")


@plugin_app.command("disable")
def plugin_disable(
    name: str = typer.Argument(..., help="Plugin name to disable"),
):
    """Disable a plugin without uninstalling it."""
    from esfex.plugins import get_plugin_manager

    get_plugin_manager().disable(name)
    console.print(f"[yellow]Disabled plugin:[/yellow] {name}")


@app.command()
def precompile(
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Rebuild sysimage even if one already exists",
    ),
):
    """
    Build a Julia sysimage for faster simulation startup.

    Creates a native sysimage containing JuMP, HiGHS, and the ESFEX
    optimization pipeline pre-compiled into machine code. This is a
    one-time operation (~5-10 minutes) that reduces simulation startup
    from ~30s to <3s.

    The sysimage is automatically detected on subsequent runs.
    Rebuild with --force after modifying Julia source files.
    """
    from esfex.bridge.julia_setup import (
        _find_sysimage,
        _sysimage_is_stale,
        precompile_esfex,
    )

    # Check current state
    existing = _find_sysimage()
    if existing and not force and not _sysimage_is_stale(existing):
        console.print(f"[green]Sysimage is up to date:[/green] {existing}")
        console.print("Use --force to rebuild.")
        return

    if existing and _sysimage_is_stale(existing):
        console.print("[yellow]Sysimage is stale — rebuilding...[/yellow]")
    else:
        console.print("[bold]Building Julia sysimage (this may take 5-10 minutes)...[/bold]")

    path = precompile_esfex(force=force)

    if path:
        console.print(f"\n[green]Sysimage built successfully:[/green] {path}")
        size_mb = path.stat().st_size / (1024 * 1024)
        console.print(f"Size: {size_mb:.0f} MB")
        console.print("Subsequent simulations will start ~10-20x faster.")
    else:
        console.print("\n[red]Sysimage build failed.[/red]")
        console.print("Make sure Julia is installed and available in PATH.")
        raise typer.Exit(code=1)


@app.command()
def info(
    json_out: bool = typer.Option(
        False, "--json",
        help="Print one JSON object (schema esfex.info/1) on stdout instead of text",
    ),
    probe: Optional[bool] = typer.Option(
        None, "--probe/--no-probe",
        help="Start Julia and load each installed solver (default: on for the text "
             "output, off with --json, where solvers are found from the Julia "
             "project files)",
    ),
):
    """
    Show ESFEX version and system information.
    """
    if json_out:
        from esfex.api import envelope, system_info

        with _StdoutToStderr():
            data = system_info(probe=bool(probe))
        _write_json(envelope("info", True, **data))
        return
    if probe is False:
        from esfex.api import system_info

        _show_system_info(system_info(probe=False))
        return

    import sys
    from esfex import __version__

    console.print(f"[bold blue]ESFEX[/bold blue] version {__version__}")
    console.print(f"Python: {sys.version}")

    # Check for Julia
    julia_available = False
    try:
        from juliacall import Main as jl
        console.print(f"Julia: [green]Available[/green] via juliacall")
        julia_available = True
    except ImportError:
        console.print(f"Julia: [yellow]Not available (juliacall not installed)[/yellow]")

    # Sysimage status
    from esfex.bridge.julia_setup import _find_sysimage, _sysimage_is_stale

    sysimage = _find_sysimage()
    if sysimage:
        stale = _sysimage_is_stale(sysimage)
        size_mb = sysimage.stat().st_size / (1024 * 1024)
        if stale:
            console.print(
                f"Sysimage: [yellow]STALE[/yellow] ({sysimage}, {size_mb:.0f} MB)"
            )
            console.print("  Run `esfex precompile --force` to rebuild")
        else:
            console.print(
                f"Sysimage: [green]UP TO DATE[/green] ({sysimage}, {size_mb:.0f} MB)"
            )
    else:
        console.print("Sysimage: [yellow]Not built[/yellow]")
        console.print("  Run `esfex precompile` for faster simulation startup")

    # Check for solvers (via Julia/JuMP)
    console.print("\n[bold]Available solvers (Julia/JuMP):[/bold]")
    if julia_available:
        _check_julia_solver("HiGHS")
        _check_julia_solver("Gurobi")
        _check_julia_solver("CPLEX")
    else:
        console.print("  [yellow]Julia not available - cannot check solvers[/yellow]")


def _show_system_info(data: dict) -> None:
    """Text view of :func:`esfex.api.system_info` without probing Julia."""
    console.print(f"[bold blue]ESFEX[/bold blue] version {data['esfex']['version']}")
    console.print(f"Python: {data['python']['version']} ({data['python']['executable']})")
    jl = data["julia"]
    console.print("Julia: " + ("juliacall installed" if jl["juliacall_installed"]
                               else "[yellow]juliacall not installed[/yellow]")
                  + (f", julia at {jl['julia_on_path']}" if jl["julia_on_path"] else ""))
    si = jl.get("sysimage")
    console.print("Sysimage: " + ("[yellow]Not built[/yellow]" if not si else
                                  f"{'[yellow]STALE[/yellow]' if si['stale'] else 'up to date'} "
                                  f"({si['path']}, {si['size_mb']} MB)"))
    table = Table(title="Solvers (from the Julia project files)")
    for col in ("Solver", "Installed", "Licensed", "Type"):
        table.add_column(col)
    for s in data["solvers"]:
        table.add_row(s["name"], "yes" if s["installed"] else "no",
                      "yes" if s["licensed"] else "no", s["kind"])
    console.print(table)
    d = data["data"]
    console.print(f"Data root: {d['data_root']}" + ("" if d["data_root_exists"] else " (missing)"))


def _check_julia_solver(name: str):
    """Check if a solver is available in Julia."""
    try:
        from esfex.config.solver import get_solver_info
        info = get_solver_info(name)
        if info["available"]:
            version_str = f" (v{info['version']})" if info.get("version") else ""
            console.print(f"  {name}: [green]Available{version_str}[/green]")
        else:
            console.print(f"  {name}: [yellow]Not found[/yellow]")
    except Exception as e:
        console.print(f"  {name}: [red]Error checking[/red]")


def _show_config_summary(cfg):
    """Display a summary of the configuration."""
    from esfex.config.schema import ESFEXConfig

    table = Table(title="Configuration Summary")
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Simulation Mode", cfg.simulation_mode)
    table.add_row("Solver", cfg.solver.name)
    table.add_row("Systems", ", ".join(cfg.meta_network.systems))

    for sys_name in cfg.meta_network.systems:
        sys_cfg = cfg.systems[sys_name]
        table.add_row(f"  {sys_name} nodes", str(sys_cfg.num_nodes))
        table.add_row(f"  {sys_name} generators", str(len(sys_cfg.generators)))
        table.add_row(f"  {sys_name} batteries", str(len(sys_cfg.batteries)))

    table.add_row("Rolling Horizon", str(cfg.temporal.use_rolling_horizon))
    table.add_row("Primary Energy", str(cfg.enable_primary_energy))
    table.add_row("N-1 Security", str(cfg.n1_security.enabled))

    console.print(table)


def _register_plugin_cli(args: Optional[list[str]] = None) -> None:
    """Load the enabled, trusted plugins and mount their CLI sub-commands.

    Called by the ``esfex`` entry point with the command line, not on
    import. Skipped when *args* name a built-in command: those don't need
    plugin commands, and ``run`` and ``studio`` load the plugins themselves
    with the project's config (the manager loads plugins only once).
    """
    args = sys.argv[1:] if args is None else args
    command = next((a for a in args if not a.startswith("-")), None)
    try:
        from esfex.plugins import get_plugin_manager
        from esfex.plugins.manager import _typer_command_names

        if command is not None and command in _typer_command_names(app):
            return
        pm = get_plugin_manager()
        pm.load_all(config=None, gui_mode=False)
        pm.register_cli_commands(app)
    except Exception:
        pass  # A plugin problem must not stop the built-in commands


@app.command()
def train_demand_model(
    output: Optional[Path] = typer.Option(
        None, "--output", "-o",
        help="Where to write the trained model (default: demand_model.xgb "
             "in the ESFEX models directory, where the Studio picks it up)",
    ),
    cache_dir: Optional[Path] = typer.Option(
        None, "--cache-dir",
        help="Dataset directory written by build-demand-dataset "
             "(default: its default directory)",
    ),
    folds: int = typer.Option(
        3, "--folds", min=1, help="Temporal cross-validation folds (by year)",
    ),
):
    """Train the XGBoost demand model on the dataset of build-demand-dataset.

    Reads the hourly Parquet files that ``esfex build-demand-dataset`` wrote
    (real demand with ERA5 temperature), adds the pixel features at each
    series' location, validates by temporal cross-validation and saves the
    model. No demand data is downloaded (raster tiles of the pixel features
    missing locally are fetched on first use). A model saved in the ESFEX
    models directory replaces the shipped one in the Demand Estimation
    workflow.
    """
    from esfex.models.demand_training import train_demand_model as _train

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Training demand model...", total=None)

        def on_progress(pct: int, msg: str) -> None:
            progress.update(task, description=f"[{pct}%] {msg}")

        try:
            model = _train(
                dataset_dir=cache_dir,
                output_path=output,
                n_cv_folds=folds,
                progress_cb=on_progress,
            )
            console.print(
                f"\n[green]Model trained successfully.[/green] "
                f"Ready for use in Demand Estimation workflow."
            )
        except ImportError as exc:
            console.print(f"\n[red]Missing dependency:[/red] {exc}")
            console.print(
                "xgboost ships with esfex; reinstall with: "
                "pip install --upgrade --force-reinstall esfex"
            )
            raise typer.Exit(code=1)
        except Exception as exc:
            console.print(f"\n[red]Training failed:[/red] {exc}")
            raise typer.Exit(code=1)


def _check_demand_sources(value: Optional[str]) -> Optional[str]:
    from esfex.models.demand_real_data import DEMAND_SOURCES

    names = {s.strip().lower() for s in (value or "").split(",") if s.strip()}
    unknown = sorted(names - set(DEMAND_SOURCES) - {"all"})
    if unknown:
        raise typer.BadParameter(
            f"unknown source(s) {', '.join(unknown)}; use all or any of: "
            f"{', '.join(DEMAND_SOURCES)}")
    return value


@app.command()
def build_demand_dataset(
    sources: Optional[str] = typer.Option(
        "all", "--sources", "-s", callback=_check_demand_sources,
        help="Comma-separated source names, or all: opsd, entsoe, brazil, "
             "colombia, japan, rte, uk, eskom, australia, usa, turkey, chile, "
             "argentina, mexico, korea, taiwan, peru",
    ),
    cache_dir: Optional[Path] = typer.Option(
        None, "--cache-dir",
        help="Output directory for the dataset",
    ),
):
    """Download and consolidate hourly electricity demand from public sources.

    Downloads real hourly demand data from OPSD (Europe), ENTSO-E, Brazil ONS,
    Colombia XM, TEPCO Japan, AEMO Australia, and more. Also downloads ERA5
    hourly temperature for all country-years. Outputs standardized Parquet files.

    First run downloads 5-50 GB depending on sources. All data is cached.
    """
    from esfex.models.demand_dataset import build_dataset

    source_list = [s.strip() for s in sources.split(",")] if sources else ["all"]

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Building demand dataset...", total=None)

        def on_progress(pct: int, msg: str) -> None:
            progress.update(task, description=f"[{pct}%] {msg}")

        try:
            manifest = build_dataset(
                cache_dir=cache_dir,
                sources=source_list,
                progress_cb=on_progress,
            )
            console.print(
                f"\n[green]Dataset built:[/green] "
                f"{manifest['n_countries']} countries, "
                f"{manifest['n_country_years']} country-years"
            )
        except Exception as exc:
            console.print(f"\n[red]Dataset build failed:[/red] {exc}")
            raise typer.Exit(code=1)


@app.command("add-solver")
def add_solver(
    name: str = typer.Argument(
        ..., help="Solver to install: gurobi, cplex, cbc, scip, xpress",
    ),
):
    """Install an optional/commercial solver into ESFEX's Julia environment.

    Open solvers (HiGHS, GLPK, Ipopt, SCS, Clarabel) ship with ESFEX. Commercial
    solvers need a per-user licence and a large build, so they are installed on
    demand into a dedicated environment that is stacked on Julia's load path —
    surviving ESFEX upgrades and used automatically once installed.

    Gurobi/CPLEX also require the vendor toolkit + licence to be discoverable at
    build time (e.g. GUROBI_HOME). The install verifies the solver loads, so any
    licence/build problem is reported here rather than mid-solve.
    """
    from esfex.bridge.julia_setup import OPTIONAL_SOLVER_PACKAGES
    from esfex.bridge.julia_setup import add_solver as _add

    if name.lower() not in OPTIONAL_SOLVER_PACKAGES:
        console.print(
            f"[red]Unknown optional solver '{name}'. Installable solvers: "
            f"{', '.join(sorted(OPTIONAL_SOLVER_PACKAGES))}.[/red]"
        )
        raise typer.Exit(code=2)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        progress.add_task(
            f"Installing {name} into ESFEX's Julia environment "
            "(may take a few minutes)...", total=None,
        )
        try:
            pkg = _add(name)
        except Exception as exc:
            console.print(f"\n[red]Could not install {name}:[/red] {exc}")
            raise typer.Exit(code=1)

    console.print(
        f"\n[green]{pkg}.jl installed and verified.[/green] "
        f"Select '{name.lower()}' as the solver in Studio or your config."
    )


@app.command("remove-solver")
def remove_solver(
    name: str = typer.Argument(..., help="Solver to remove: gurobi, cplex, cbc, scip, xpress"),
):
    """Remove an optional solver previously added with ``esfex add-solver``."""
    from esfex.bridge.julia_setup import OPTIONAL_SOLVER_PACKAGES
    from esfex.bridge.julia_setup import remove_solver as _remove

    if name.lower() not in OPTIONAL_SOLVER_PACKAGES:
        console.print(
            f"[red]Unknown optional solver '{name}'. Installable solvers: "
            f"{', '.join(sorted(OPTIONAL_SOLVER_PACKAGES))}.[/red]"
        )
        raise typer.Exit(code=2)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        progress.add_task(f"Removing {name}...", total=None)
        try:
            pkg = _remove(name)
        except Exception as exc:
            console.print(f"\n[red]Could not remove {name}:[/red] {exc}")
            raise typer.Exit(code=1)

    console.print(f"\n[green]{pkg}.jl removed.[/green]")


@app.command("list-solvers")
def list_solvers():
    """Show which optimization solvers are available to ESFEX."""
    from esfex.bridge.julia_setup import OPTIONAL_SOLVER_PACKAGES
    from esfex.config.solver import detect_available_solvers

    avail = detect_available_solvers()
    table = Table(title="ESFEX solvers")
    table.add_column("Solver")
    table.add_column("Available")
    table.add_column("Type")
    bundled = {"highs", "glpk", "ipopt", "scs", "clarabel"}
    for key in sorted(avail):
        kind = ("bundled" if key in bundled
                else "optional" if key in OPTIONAL_SOLVER_PACKAGES
                else "conic/nlp")
        mark = "[green]yes[/green]" if avail[key] else "[dim]no[/dim]"
        table.add_row(key, mark, kind)
    console.print(table)
    missing = [k for k in OPTIONAL_SOLVER_PACKAGES if not avail.get(k)]
    if missing:
        console.print(
            "\nInstall a commercial solver with: "
            f"[cyan]esfex add-solver {missing[0]}[/cyan]"
        )


def _entrypoint() -> None:
    """CLI entrypoint (registered in pyproject)."""
    _register_plugin_cli()
    app()


def _studio_entrypoint() -> None:
    """GUI entrypoint for the Start-Menu / desktop launcher.

    Registered as a ``gui-script`` in pyproject so pip builds a
    console-less ``esfex-studio.exe`` (pythonw-based) on Windows:
    double-clicking it opens the Studio with no terminal window.
    Equivalent to ``esfex studio`` with no options — we call
    ``launch_studio`` directly instead of routing through the typer
    ``studio`` command, which would write to a console that doesn't
    exist under pythonw.
    """
    from esfex.config.loader import SavedConfigInvalidError
    from esfex.visualization import launch_studio

    try:
        launch_studio(blocking=True)
    except SavedConfigInvalidError:
        # No console under pythonw; the Studio warned before "Save anyway".
        pass


if __name__ == "__main__":
    _entrypoint()
