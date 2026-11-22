"""What this ESFEX installation has: versions, Julia, solvers, data, paths
(``esfex info``).

Without ``probe`` nothing is started: Julia and its solver packages are
found from the Julia project files, commercial licences from the licence
file's location (never its contents). With ``probe`` Julia starts and each
installed solver package is loaded, which takes a while the first time.
"""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Optional

# Open solvers need no licence
_OPEN_SOLVERS = ("highs", "glpk", "ipopt", "scs", "clarabel", "cbc", "scip")


def _gurobi_licence_found() -> bool:
    """A Gurobi licence file where Gurobi looks for one (GRB_LICENSE_FILE,
    the home directory, the default install paths). Its contents are not
    read; a token server or WLS licence given that way counts too."""
    env = os.environ.get("GRB_LICENSE_FILE")
    candidates = [Path(env)] if env else []
    home = Path.home()
    candidates += [home / "gurobi.lic", Path("/opt/gurobi/gurobi.lic"),
                   Path("/usr/local/gurobi/gurobi.lic"), Path("C:/gurobi/gurobi.lic"),
                   Path("/Library/gurobi/gurobi.lic")]
    gh = os.environ.get("GUROBI_HOME")
    if gh:
        candidates.append(Path(gh) / "gurobi.lic")
    return any(p.is_file() for p in candidates)


def _xpress_licence_found() -> bool:
    candidates = []
    for var in ("XPAUTH_PATH",):
        if os.environ.get(var):
            candidates.append(Path(os.environ[var]))
    xd = os.environ.get("XPRESSDIR")
    if xd:
        candidates += [Path(xd) / "bin" / "xpauth.xpr", Path(xd) / "xpauth.xpr"]
    return any(p.is_file() for p in candidates)


def _licensed(name: str, installed: bool) -> bool:
    if not installed:
        return False
    if name in _OPEN_SOLVERS:
        return True
    if name == "gurobi":
        return _gurobi_licence_found()
    if name == "xpress":
        return _xpress_licence_found()
    # CPLEX (and anything else): no licence file to look for; installed
    # means the vendor library was found when the package was built
    return True


def _julia_info(probe: bool) -> dict:
    from esfex.bridge.julia_setup import _find_sysimage, _sysimage_is_stale, get_julia_path

    sysimage = _find_sysimage()
    info = {
        "juliacall_installed": importlib.util.find_spec("juliacall") is not None,
        "julia_on_path": shutil.which("julia"),
        "project": str(get_julia_path()),
        "manifest_present": (get_julia_path() / "Manifest.toml").exists(),
        "sysimage": None if sysimage is None else {
            "path": str(sysimage), "stale": _sysimage_is_stale(sysimage),
            "size_mb": round(sysimage.stat().st_size / 1e6)},
        "probed": bool(probe),
        "starts": None,
        "version": None,
    }
    if probe and info["juliacall_installed"]:
        try:
            from esfex.bridge.julia_setup import get_julia

            jl = get_julia()
            info["starts"] = True
            try:
                info["version"] = str(jl.seval("string(VERSION)"))
            except Exception:
                pass
        except Exception as exc:
            info["starts"] = False
            info["error"] = str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__
    return info


def _solvers(probe: bool, julia_starts: Optional[bool]) -> list[dict]:
    from esfex.bridge.julia_setup import OPTIONAL_SOLVER_PACKAGES
    from esfex.config.solver import _SOLVER_JULIA_PKG, detect_available_solvers

    avail = detect_available_solvers()
    out = []
    for name in sorted(avail):
        installed = bool(avail[name])
        entry = {
            "name": name,
            "package": _SOLVER_JULIA_PKG.get(name),
            "installed": installed,
            "kind": ("optional" if name in OPTIONAL_SOLVER_PACKAGES else "bundled"),
            "licensed": _licensed(name, installed),
            "loads": None,
            "version": None,
        }
        if probe and julia_starts and installed:
            try:
                from esfex.bridge.julia_setup import get_julia

                jl = get_julia()
                pkg = _SOLVER_JULIA_PKG[name]
                jl.seval(f"import {pkg}")
                entry["loads"] = True
                try:
                    entry["version"] = str(jl.seval(f"string(pkgversion({pkg}))"))
                except Exception:
                    pass
            except Exception:
                entry["loads"] = False
        out.append(entry)
    return out


def _data_info() -> dict:
    from esfex import data_assets, paths

    published = data_assets.manifest().get("assets", {})
    gdp = [n for n in published if n.startswith("GDP")]
    gpw = [n for n in published if n.startswith("gpw_v4_popdens_")]
    gdp_dir = paths.GRIDDED_GDP_025_DIR
    gpw_dir = paths.POP_DENSITY_HIST_DIR / "tiles"
    demand_model = paths.model_path("demand_model.xgb", "demand_model.ubj")
    return {
        "data_root": str(paths.DATA_ROOT),
        "data_root_exists": paths.DATA_ROOT.exists(),
        "data_root_from_env": "ESFEX_DATA_ROOT" in os.environ,
        "assets_release": data_assets.manifest().get("tag"),
        "gdp_rasters": {"dir": str(gdp_dir), "published": len(gdp),
                        "present": sum((gdp_dir / n).exists() for n in gdp)},
        "population_tiles": {"dir": str(gpw_dir), "published": len(gpw),
                             "present": sum((gpw_dir / n).exists() for n in gpw)},
        "demand_dataset": {"dir": str(paths.DEMAND_DATASET_DIR),
                           "present": paths.DEMAND_MANIFEST.exists()},
        "demand_model": {"path": str(demand_model), "present": demand_model.exists(),
                         "retrained": demand_model.parent == paths.MODELS_DIR},
    }


def system_info(probe: bool = False) -> dict:
    """The installation as plain data (the ``esfex info --json`` fields
    besides the envelope): ``esfex``, ``python``, ``platform``, ``julia``,
    ``solvers``, ``data``. *probe* starts Julia and loads each installed
    solver package (``loads``, ``version``)."""
    import esfex

    julia = _julia_info(probe)
    return {
        "esfex": {"version": esfex.__version__, "package": str(Path(esfex.__file__).parent)},
        "python": {"version": platform.python_version(), "executable": sys.executable},
        "platform": {"system": platform.system(), "release": platform.release(),
                     "machine": platform.machine()},
        "julia": julia,
        "solvers": _solvers(probe, julia.get("starts")),
        "data": _data_info(),
    }


__all__ = ["system_info"]
