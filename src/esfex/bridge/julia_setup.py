"""
Julia setup and initialization for ESFEX.

Handles Julia environment initialization, package loading,
module compilation, and sysimage management for the ESFEX
optimization models.
"""

import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Global Julia instance
_julia_instance = None
_esfex_module = None


_julia_logger = logging.getLogger("esfex.julia")


def _forward_julia_log(level: int, message: str, milestone: bool) -> None:
    """One Julia log record (``ESFEX.CallbackLogger``) → the ``esfex.julia``
    logger, at its level. Benders progress lines are milestones, shown by
    the basic console too."""
    _julia_logger.log(int(level), str(message), extra={"milestone": bool(milestone)})


def get_julia_path() -> Path:
    """Get the path to the Julia source directory."""
    return Path(__file__).parent.parent / "julia"


def get_solvers_env_path() -> Path:
    """Path to ESFEX's dedicated Julia environment for optional/commercial
    solvers (Gurobi, CPLEX, …).

    Commercial solvers are not bundled (they need a per-user license and a
    large build), so they live in this separate environment instead of the
    shipped ``src/esfex/julia/Project.toml``. The environment is:

    * **Outside the package** — under the Julia depot — so it survives ESFEX
      upgrades (a ``pip install --upgrade`` never touches it).
    * **Stacked on ``LOAD_PATH``** at Julia init, so anything installed here
      is ``import``-able on demand from ESFEX's activated project.
    * **In the depot's ``environments/``** so it shares the global package
      cache (no duplicate downloads).

    Managed via ``esfex add-solver`` / ``remove-solver``.
    """
    depot = os.environ.get("JULIA_DEPOT_PATH", "")
    depot_root = depot.split(os.pathsep)[0] if depot else os.path.expanduser("~/.julia")
    return Path(depot_root) / "environments" / "esfex_solvers"


# ── Sysimage utilities ────────────────────────────────────────────


def _find_sysimage() -> Optional[Path]:
    """Find the ESFEX sysimage if it exists.

    Checks the Julia project directory for ESFEX.so / .dll / .dylib.
    """
    julia_dir = get_julia_path()
    for ext in (".so", ".dll", ".dylib"):
        p = julia_dir / f"ESFEX{ext}"
        if p.exists():
            return p
    return None


def _sysimage_is_stale(sysimage: Path) -> bool:
    """Check if any .jl source file is newer than the sysimage."""
    if not sysimage.exists():
        return True
    mtime = sysimage.stat().st_mtime
    src_dir = get_julia_path() / "src"
    if not src_dir.exists():
        return False
    for jl_file in src_dir.glob("*.jl"):
        if jl_file.stat().st_mtime > mtime:
            return True
    return False


# ── Dependency repair ─────────────────────────────────────────────

# Environment variable that allows ESFEX to re-resolve its Julia project
# (``Pkg.resolve``) when ``Pkg.instantiate`` cannot install the recorded
# dependencies. Off by default: re-resolving picks new package versions.
RESOLVE_ENV_VAR = "ESFEX_JULIA_RESOLVE"

# Julia messages that mean "a package is missing or not installed", the only
# load errors a dependency repair can fix.
_MISSING_PACKAGE_PATTERNS = (
    re.compile(r"Package \S+ not found in current path"),
    re.compile(r"is required but does not seem to be installed"),
    # Julia's own hint for missing packages and missing artifacts
    re.compile(r"Pkg\.instantiate\(\)"),
)


def _is_missing_package_error(err_str: str) -> bool:
    """True when a module load error is a missing/uninstalled package."""
    return any(p.search(err_str) for p in _MISSING_PACKAGE_PATTERNS)


def _source_load_error_message(err_str: str) -> str:
    """Message for a load error in the Julia sources (not a dependency)."""
    # A ParseError names the exact line and column; otherwise the innermost
    # "in expression starting at" is the failing file and line.
    m = re.search(r"# Error @ (.+?:\d+:\d+)", err_str) or re.search(
        r"in expression starting at (.+?:\d+)\s*$", err_str, re.MULTILINE
    )
    where = f" at {m.group(1)}" if m else ""
    return (
        f"the ESFEX Julia sources failed to load{where}. This is an error in "
        "the Julia code, not a missing package, so the Julia environment was "
        f"left unchanged.\n{err_str}"
    )


def _manifest_julia_version(julia_path: Path) -> Optional[str]:
    """``julia_version`` recorded in the project's Manifest.toml, if any."""
    manifest = julia_path / "Manifest.toml"
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r'^julia_version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else None


def _major_minor(version: str) -> Optional[tuple[int, int]]:
    m = re.match(r"\s*v?(\d+)\.(\d+)", version)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _warn_julia_version_mismatch(jl: Any, julia_path: Path) -> None:
    """Warn once when the Manifest was resolved by another Julia minor version.

    juliacall may run a different Julia than the ``julia`` on the PATH; a
    Manifest resolved by one minor version can fail to load or instantiate
    on another.
    """
    manifest_version = _manifest_julia_version(julia_path)
    if manifest_version is None:
        return
    try:
        running = str(jl.seval("string(VERSION)"))
    except Exception:
        return
    mv, rv = _major_minor(manifest_version), _major_minor(running)
    if mv is None or rv is None or mv == rv:
        return
    logger.warning(
        "ESFEX's Julia Manifest.toml (%s) was resolved with Julia %s, but "
        "juliacall runs Julia %s. Packages recorded for one Julia version may "
        "fail to load or install on another; use the same Julia for both "
        "(see the installation troubleshooting guide).",
        julia_path / "Manifest.toml", manifest_version, running,
    )


def _backup_manifest(manifest: Path) -> Optional[Path]:
    """Copy Manifest.toml to a timestamped backup next to it."""
    if not manifest.exists():
        return None
    backup = manifest.with_name(
        f"Manifest.toml.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    shutil.copy2(manifest, backup)
    logger.warning("Backed up %s to %s", manifest, backup)
    return backup


def _repair_julia_dependencies(jl: Any, julia_path: Path, err_str: str) -> None:
    """Install the ESFEX project's missing Julia dependencies.

    Runs ``Pkg.instantiate()``, which installs the versions the Manifest
    records. Manifest.toml is never deleted: it is backed up first, and a
    re-resolve (``Pkg.resolve()``, which may pick other package versions)
    runs only when the ``ESFEX_JULIA_RESOLVE`` environment variable is set.
    """
    manifest = julia_path / "Manifest.toml"
    first_line = err_str.strip().splitlines()[0] if err_str.strip() else err_str
    logger.warning(
        "ESFEX Julia dependencies are missing (%s). Running Pkg.instantiate() "
        "on %s", first_line, julia_path,
    )
    before = manifest.read_bytes() if manifest.exists() else None
    backup = _backup_manifest(manifest)

    def _settle_backup() -> bool:
        """Drop a redundant backup; True when the Manifest changed."""
        after = manifest.read_bytes() if manifest.exists() else None
        if before is not None and after != before:
            logger.warning(
                "Manifest.toml changed during the dependency repair "
                "(previous version: %s)", backup,
            )
            return True
        if backup is not None:
            backup.unlink(missing_ok=True)
            logger.info("Manifest.toml unchanged; removed backup %s", backup)
        return False

    try:
        jl.seval("Pkg.instantiate()")
    except Exception as pkg_err:
        if os.environ.get(RESOLVE_ENV_VAR, "").strip().lower() not in (
            "1", "true", "yes", "on",
        ):
            changed = _settle_backup()
            state = (
                f"Manifest.toml changed; the previous version is in {backup}"
                if changed else "Manifest.toml was left unchanged"
            )
            raise RuntimeError(
                f"Pkg.instantiate() could not install ESFEX's Julia "
                f"dependencies: {pkg_err}\n{state}. To re-resolve the Julia "
                f"project (this may pick other package versions), set "
                f"{RESOLVE_ENV_VAR}=1 and run ESFEX again, or see the "
                "installation troubleshooting guide."
            ) from pkg_err
        logger.warning(
            "Pkg.instantiate() failed: %s. %s is set: re-resolving the Julia "
            "project with Pkg.resolve()%s", pkg_err, RESOLVE_ENV_VAR,
            f" (previous Manifest kept at {backup})" if backup else "",
        )
        jl.seval("Pkg.resolve(); Pkg.instantiate()")
    _settle_backup()
    logger.warning("Julia dependency repair finished; reloading ESFEX")


# ── Julia initialization ──────────────────────────────────────────


def initialize_julia(
    threads: int = 4,
    compile: bool = True,
    verbose: bool = False,
) -> Any:
    """
    Initialize the Julia runtime and load the ESFEX module.

    Automatically detects and uses a sysimage if one exists.
    Subsequent calls return the cached Julia instance. A load error caused
    by a missing package runs ``Pkg.instantiate()`` (see
    :func:`_repair_julia_dependencies`; Manifest.toml is never deleted);
    any other load error is raised with the failing file and line.

    Args:
        threads: Number of threads for Julia
        compile: Accepted for compatibility; not used (build a sysimage
            with ``esfex precompile`` instead)
        verbose: Enable verbose output

    Returns:
        The juliacall Main module with ESFEX loaded

    Raises:
        ImportError: If juliacall is not installed
        RuntimeError: If Julia initialization fails
    """
    global _julia_instance, _esfex_module

    if _julia_instance is not None:
        return _julia_instance

    # Set Julia thread count
    os.environ.setdefault("JULIA_NUM_THREADS", str(threads))

    # Auto-detect sysimage BEFORE importing juliacall
    # (PYTHON_JULIACALL_SYSIMAGE must be set before first import)
    sysimage = _find_sysimage()
    if sysimage:
        if _sysimage_is_stale(sysimage):
            logger.info(
                "Julia sysimage is stale (run `esfex precompile` to rebuild)"
            )
            # Still use it — dependency precompilation is still valid
            os.environ.setdefault("PYTHON_JULIACALL_SYSIMAGE", str(sysimage))
        else:
            os.environ.setdefault("PYTHON_JULIACALL_SYSIMAGE", str(sysimage))
            logger.info(f"Using Julia sysimage: {sysimage}")
    else:
        logger.debug(
            "No Julia sysimage found. "
            "Run `esfex precompile` for faster startup."
        )

    try:
        from juliacall import Main as jl
    except ImportError:
        raise ImportError(
            "juliacall is required for Julia integration. "
            "Install with: pip install juliacall"
        )

    logger.info("Initializing Julia runtime...")

    # Get Julia source path
    julia_path = get_julia_path()
    project_toml = julia_path / "Project.toml"
    src_path = julia_path / "src"

    if not project_toml.exists():
        raise RuntimeError(f"Julia Project.toml not found at {project_toml}")

    try:
        # Activate the Julia project
        # Use forward slashes so Julia doesn't interpret backslashes as escapes
        jl_project = str(julia_path).replace("\\", "/")
        jl_src = str(src_path / "ESFEX.jl").replace("\\", "/")
        jl.seval(f'using Pkg; Pkg.activate("{jl_project}")')

        # Stack ESFEX's optional-solvers environment on LOAD_PATH so any
        # commercial solver the user installed via `esfex add-solver` (Gurobi,
        # CPLEX, …) is import-able on demand — without bundling it into the
        # shipped project. Pushing a non-existent path is harmless (it simply
        # resolves nothing until a solver is added).
        solvers_env = str(get_solvers_env_path()).replace("\\", "/")
        jl.seval(f'("{solvers_env}" in LOAD_PATH) || push!(LOAD_PATH, "{solvers_env}")')

        _warn_julia_version_mismatch(jl, julia_path)

        # Load the ESFEX module. Only a missing/uninstalled package triggers a
        # dependency repair; any other load error (syntax error, error thrown
        # by ESFEX's own top-level code) is reported as-is and the Julia
        # environment is left untouched.
        logger.info("Loading ESFEX Julia module...")
        try:
            jl.seval(f'include("{jl_src}")')
            jl.seval("using .ESFEX")
        except Exception as load_error:
            err_str = str(load_error)
            if "invalid redefinition of constant" in err_str:
                # Module already loaded in this Julia session — just use it
                logger.info("ESFEX module already loaded, reusing existing.")
                jl.seval("using .ESFEX")
            elif _is_missing_package_error(err_str):
                _repair_julia_dependencies(jl, julia_path, err_str)
                try:
                    jl.seval(f'include("{jl_src}")')
                    jl.seval("using .ESFEX")
                except Exception as retry_error:
                    raise RuntimeError(
                        "the ESFEX Julia module still fails to load after "
                        "Pkg.instantiate(). A package used by the code but "
                        "missing from Project.toml must be added to it; to "
                        f"re-resolve the project set {RESOLVE_ENV_VAR}=1.\n"
                        f"{retry_error}"
                    ) from None
            else:
                raise RuntimeError(_source_load_error_message(err_str)) from None

        # Julia's @info/@warn go to the Python logger (console and log
        # file) instead of the bare process stderr
        jl.ESFEX.set_log_callback(_forward_julia_log)

        # Pre-import optional solvers so their MOI methods live in the current
        # world age (avoids Julia 1.12 world-age errors when ESFEX lazy-loads
        # them via @eval import inside a function).
        for _pkg in ("Clarabel", "Ipopt", "SCS", "GLPK", "Cbc",
                     "SCIP", "Gurobi", "CPLEX", "Xpress"):
            try:
                jl.seval(f"import {_pkg}")
                logger.debug("Pre-imported Julia solver: %s", _pkg)
            except Exception:
                logger.debug("Optional solver %s not available", _pkg)

        _julia_instance = jl
        _esfex_module = jl.ESFEX

        logger.info("Julia initialization complete")

        return jl

    except Exception as e:
        raise RuntimeError(f"Julia initialization failed: {e}") from e


def get_julia() -> Any:
    """
    Get the Julia runtime instance.

    Initializes Julia if not already initialized.

    Returns:
        The juliacall Main module with ESFEX loaded
    """
    if _julia_instance is None:
        return initialize_julia()
    return _julia_instance


# ── Optional / commercial solver management ───────────────────────
#
# Optional solvers are NOT bundled (commit "do not bundle commercial solvers"):
# they need a per-user license and a heavy build. They live in the dedicated
# environment at get_solvers_env_path(), which is stacked on LOAD_PATH at Julia
# init so an installed solver loads on demand. These helpers install/remove/list
# solvers in that environment using juliacall's own Julia (so the correct Julia
# version and depot are always used — no "installed into the wrong env" footgun).

# ESFEX solver key → Julia package name, for the solvers users may add.
OPTIONAL_SOLVER_PACKAGES: dict[str, str] = {
    "gurobi": "Gurobi",
    "cplex": "CPLEX",
    "cbc": "Cbc",
    "scip": "SCIP",
    "xpress": "Xpress",
}


def _juliacall_main() -> Any:
    """Return juliacall's ``Main`` without loading the full ESFEX module.

    Solver management only needs ``Pkg`` in the correct (juliacall) Julia, so
    we avoid the cost of ``include(ESFEX.jl)``.
    """
    try:
        from juliacall import Main as jl
    except ImportError as e:  # pragma: no cover - env-specific
        raise RuntimeError(
            "juliacall is required to manage Julia solvers. "
            "Install with: pip install juliacall"
        ) from e
    return jl


def add_solver(name: str) -> str:
    """Install an optional solver into ESFEX's dedicated solvers environment.

    Adds the backing Julia package to :func:`get_solvers_env_path` and then
    ``import``s it to surface any build/license error immediately (e.g. Gurobi
    needs ``GUROBI_HOME`` + a licence). Returns the Julia package name.

    Raises ``ValueError`` for an unknown solver and ``RuntimeError`` if the
    package cannot be added or loaded.
    """
    key = name.lower()
    pkg = OPTIONAL_SOLVER_PACKAGES.get(key)
    if pkg is None:
        raise ValueError(
            f"Unknown optional solver {name!r}. Installable solvers: "
            f"{', '.join(sorted(OPTIONAL_SOLVER_PACKAGES))}."
        )
    env = str(get_solvers_env_path()).replace("\\", "/")
    jl = _juliacall_main()
    try:
        jl.seval("using Pkg")
        jl.seval(f'Pkg.activate("{env}")')
        jl.seval(f'Pkg.add("{pkg}")')
        # Verify it actually loads in this Julia — build/license problems throw
        # here rather than silently at the next solve.
        jl.seval(f"import {pkg}")
    except Exception as e:
        raise RuntimeError(f"Failed to install solver {key!r} ({pkg}.jl): {e}") from e
    return pkg


def remove_solver(name: str) -> str:
    """Remove an optional solver from ESFEX's dedicated solvers environment."""
    key = name.lower()
    pkg = OPTIONAL_SOLVER_PACKAGES.get(key)
    if pkg is None:
        raise ValueError(
            f"Unknown optional solver {name!r}. Installable solvers: "
            f"{', '.join(sorted(OPTIONAL_SOLVER_PACKAGES))}."
        )
    env = str(get_solvers_env_path()).replace("\\", "/")
    jl = _juliacall_main()
    try:
        jl.seval("using Pkg")
        jl.seval(f'Pkg.activate("{env}")')
        jl.seval(f'Pkg.rm("{pkg}")')
    except Exception as e:
        raise RuntimeError(f"Failed to remove solver {key!r} ({pkg}.jl): {e}") from e
    return pkg


def list_installed_optional_solvers() -> list[str]:
    """ESFEX solver keys currently installed in the dedicated solvers env.

    Reads the env's ``Project.toml`` directly (no Julia startup).
    """
    project_toml = get_solvers_env_path() / "Project.toml"
    installed: list[str] = []
    try:
        in_deps = False
        present: set[str] = set()
        for raw in project_toml.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("["):
                in_deps = line == "[deps]"
                continue
            if in_deps and "=" in line:
                present.add(line.split("=", 1)[0].strip())
        installed = [k for k, pkg in OPTIONAL_SOLVER_PACKAGES.items() if pkg in present]
    except Exception:
        pass
    return installed


_included_overlays: set[str] = set()


def include_plugin_overlays(paths) -> None:
    """``include()`` plugin Julia overlay files into the running session (once).

    Overlays run after ``ESFEX`` is loaded, so they can call
    ``ESFEX.register_constraint_hook!(...)`` to add custom constraint types.
    Errors in one overlay are logged and skipped (they never abort a run).
    """
    jl = get_julia()
    for path in paths:
        key = str(path).replace("\\", "/")
        if key in _included_overlays:
            continue
        try:
            jl.seval(f'include("{key}")')
            _included_overlays.add(key)
            logger.info("Included plugin Julia overlay: %s", key)
        except Exception:
            logger.exception("Failed to include plugin Julia overlay: %s", key)


def get_esfex_module() -> Any:
    """
    Get the ESFEX Julia module.

    Returns:
        The ESFEX Julia module

    Raises:
        RuntimeError: If Julia is not initialized
    """
    if _esfex_module is None:
        get_julia()  # Initialize if needed
    if _esfex_module is None:
        raise RuntimeError("ESFEX Julia module not loaded")
    return _esfex_module


def create_julia_optimizer(
    solver: str = "highs",
    threads: int = 4,
    time_limit: float = 3600.0,
    gap: float = 0.01,
    verbose: bool = False,
) -> Any:
    """
    Create a configured JuMP optimizer in Julia.

    Uses ESFEX.create_optimizer() which handles all solver-specific
    parameter mapping and on-demand loading of solver packages.

    Args:
        solver: Solver name ('highs', 'gurobi', 'cplex', 'scip', 'xpress', 'cbc', 'glpk')
        threads: Number of threads
        time_limit: Time limit in seconds
        gap: MIP optimality gap
        verbose: Enable verbose output

    Returns:
        Julia optimizer object
    """
    jl = get_julia()
    solver = solver.lower()

    return jl.seval(f"""
    ESFEX.create_optimizer(
        solver_name="{solver}",
        threads={threads},
        time_limit={time_limit},
        gap={gap},
        verbose={str(verbose).lower()}
    )
    """)


def check_julia_available() -> bool:
    """
    Check if Julia is available.

    Returns:
        True if Julia can be initialized, False otherwise
    """
    try:
        from juliacall import Main as jl
        return True
    except ImportError:
        return False
    except Exception:
        return False


def get_julia_version() -> Optional[str]:
    """
    Get the Julia version string.

    Returns:
        Julia version string or None if not available
    """
    try:
        jl = get_julia()
        return str(jl.seval("VERSION"))
    except Exception:
        return None


# ── Sysimage build ────────────────────────────────────────────────


def precompile_esfex(force: bool = False) -> Optional[Path]:
    """Build the Julia sysimage for faster startup.

    Uses PackageCompiler.jl to create a native sysimage containing
    JuMP, HiGHS, and the ESFEX optimization pipeline pre-compiled.

    Args:
        force: Rebuild even if a fresh sysimage already exists.

    Returns:
        Path to the built sysimage, or None on failure.
    """
    julia_dir = get_julia_path()
    sysimage = _find_sysimage()

    if sysimage and not force and not _sysimage_is_stale(sysimage):
        logger.info("Sysimage is up to date: %s", sysimage)
        return sysimage

    build_script = julia_dir / "build_sysimage.jl"
    if not build_script.exists():
        logger.error("build_sysimage.jl not found at %s", build_script)
        return None

    # Find Julia executable
    julia_exe = shutil.which("julia")
    if julia_exe is None:
        logger.error("Julia executable not found in PATH")
        return None

    logger.info("Building Julia sysimage (this may take 5-10 minutes)...")

    try:
        result = subprocess.run(
            [julia_exe, f"--project={julia_dir}", str(build_script)],
            capture_output=False,
            text=True,
        )
        if result.returncode != 0:
            logger.error("Sysimage build failed (exit code %d)", result.returncode)
            return None
    except Exception as e:
        logger.error("Sysimage build failed: %s", e)
        return None

    built = _find_sysimage()
    if built:
        logger.info("Sysimage built successfully: %s", built)
    return built
