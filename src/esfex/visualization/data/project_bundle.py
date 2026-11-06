"""Portable project bundles (``.esfexp``).

A project is more than its config YAML: a runnable config references external
user files by path — per-node demand (``demand_paths``), availability profiles
(``Availability``/``availability_file`` on generators, batteries, technologies),
and reservoir inflow (``reservoir_inflow_file``), plus optional external
``systems:`` YAMLs.

``export_project`` serializes the GUI state, copies every referenced file into a
staging tree (``demand/`` / ``availability/`` / ``inflow/`` / ``systems/``),
rewrites the config paths to those relative locations, adds a ``manifest.json``
and zips it into a single ``.esfexp`` file. ``import_project`` extracts it (with
a Zip-Slip guard) into a project folder and returns the config path. The runner
and GUI resolve the now-relative paths under the config directory.

Pure (no Qt) so it is unit-testable.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_NAME = "config.yaml"
MANIFEST_NAME = "manifest.json"
PROJECT_SUFFIX = ".esfexp"


@dataclass
class ExportReport:
    """Outcome of :func:`export_project`."""

    dest: str
    bundled: list[str] = field(default_factory=list)   # relative paths inside the zip
    missing: list[str] = field(default_factory=list)   # referenced paths not found on disk


class _Bundler:
    """Copies referenced files into the staging tree, deduping and returning
    their relative-in-bundle paths."""

    def __init__(self, staging: Path, src_base: Path):
        self.staging = staging
        self.src_base = src_base
        self._copied: dict[str, str] = {}   # resolved source → relative-in-bundle
        self._used: set[str] = set()
        self.bundled: list[str] = []
        self.missing: list[str] = []

    def _resolve(self, raw_path: str) -> Path | None:
        p = Path(raw_path)
        if p.is_absolute():
            return p if p.is_file() else None
        for root in (self.src_base, Path.cwd()):
            cand = root / p
            if cand.is_file():
                return cand
        return None

    def add(self, raw_path: str | None, subdir: str) -> str | None:
        """Copy ``raw_path`` into ``subdir`` and return its bundle-relative path
        (or ``None`` if empty / not found — recorded in ``missing``)."""
        if not raw_path:
            return None
        src = self._resolve(raw_path)
        if src is None:
            self.missing.append(raw_path)
            return None
        key = str(src.resolve())
        if key in self._copied:
            return self._copied[key]
        # Unique name within the subdir (basename collisions → suffix _N).
        base = src.name
        rel = f"{subdir}/{base}"
        n = 1
        while rel in self._used:
            stem, ext = os.path.splitext(base)
            rel = f"{subdir}/{stem}_{n}{ext}"
            n += 1
        self._used.add(rel)
        dest = self.staging / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        self._copied[key] = rel
        self.bundled.append(rel)
        return rel


def map_data_paths(cfg: dict, fn) -> None:
    """Rewrite every data-file reference of a config dict in place.

    ``fn(path, kind)`` is called for each non-empty path string, with ``kind``
    one of ``"demand"``, ``"availability"`` or ``"inflow"``; it returns the
    replacement, or ``None`` to keep the path. Covers ``demand_paths`` /
    ``demand_path`` and the availability / inflow files of generators,
    batteries and technologies — the files a runnable config needs besides
    itself. Shared by the project bundler and the serializer's path rebasing.
    """
    systems = cfg.get("systems")
    if not isinstance(systems, dict):
        return

    def _apply(v, kind):
        if isinstance(v, str) and v:
            return fn(v, kind) or v
        return v

    for sys in systems.values():
        if not isinstance(sys, dict):
            continue  # external system file refs are inlined by the GUI exporter

        dps = sys.get("demand_paths")
        if isinstance(dps, list):
            sys["demand_paths"] = [_apply(p, "demand") for p in dps]
        if "demand_path" in sys:
            sys["demand_path"] = _apply(sys["demand_path"], "demand")

        for grp in ("generators", "batteries", "technologies",
                    "battery_technologies"):
            elems = sys.get(grp)
            if not isinstance(elems, dict):
                continue
            for e in elems.values():
                if not isinstance(e, dict):
                    continue
                for key in ("Availability", "availability_file"):
                    if key in e:
                        e[key] = _apply(e[key], "availability")
                if "reservoir_inflow_file" in e:
                    e["reservoir_inflow_file"] = _apply(
                        e["reservoir_inflow_file"], "inflow")


def _rewrite_refs(cfg: dict, bundler: _Bundler) -> None:
    """Walk a config dict, bundling every referenced file and rewriting its
    path to the bundle-relative location (kept as-is when not found)."""
    map_data_paths(cfg, bundler.add)


def export_project(
    states,
    base_config,
    dest_path,
    *,
    inter_system_links=None,
    global_settings=None,
    stochastic_scenarios=None,
    app_version: str = "",
    created_at: str = "",
    project_name: str = "",
    src_base=None,
    geo_assets=None,
) -> ExportReport:
    """Export the current GUI state + all referenced files to a ``.esfexp``.

    ``src_base`` is the directory relative source paths are resolved against
    (typically the loaded config's directory); absolute paths are used as-is.
    """
    import yaml

    from esfex.visualization.data.serializer import gui_state_to_yaml

    dest_path = Path(dest_path)
    src_base = Path(src_base) if src_base else Path.cwd()

    with tempfile.TemporaryDirectory(prefix="esfexp_") as tmp:
        staging = Path(tmp)
        cfg_yaml = staging / CONFIG_NAME
        gui_state_to_yaml(
            states, base_config, cfg_yaml,
            inter_system_links=inter_system_links,
            global_settings=global_settings,
            stochastic_scenarios=stochastic_scenarios,
            geo_assets=geo_assets,
        )
        with open(cfg_yaml, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

        bundler = _Bundler(staging, src_base)
        _rewrite_refs(cfg, bundler)

        with open(cfg_yaml, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)

        manifest = {
            "name": project_name or dest_path.stem,
            "esfex_version": app_version,
            "created_at": created_at,
            "files": bundler.bundled,
            "missing": bundler.missing,
        }
        (staging / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2), encoding="utf-8")

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for fp in sorted(staging.rglob("*")):
                if fp.is_file():
                    zf.write(fp, fp.relative_to(staging).as_posix())

    if bundler.missing:
        logger.warning("export_project: %d referenced file(s) not found: %s",
                        len(bundler.missing), bundler.missing)
    return ExportReport(
        dest=str(dest_path), bundled=bundler.bundled, missing=bundler.missing)


def _validate_zip_paths(zf: zipfile.ZipFile, target_root: Path) -> None:
    """Raise ValueError if any entry escapes ``target_root`` (Zip Slip)."""
    resolved_root = Path(target_root).resolve()
    for info in zf.infolist():
        member = (Path(target_root) / info.filename).resolve()
        if (not str(member).startswith(str(resolved_root) + os.sep)
                and member != resolved_root):
            raise ValueError(
                f"Unsafe path in project bundle: {info.filename!r} escapes "
                f"{target_root}")


def import_project(esfexp_path, dest_dir) -> Path:
    """Extract a ``.esfexp`` into ``dest_dir`` and return the config path."""
    esfexp_path = Path(esfexp_path)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(esfexp_path) as zf:
        _validate_zip_paths(zf, dest_dir)
        zf.extractall(dest_dir)
    cfg = dest_dir / CONFIG_NAME
    if not cfg.is_file():
        raise ValueError(
            f"{esfexp_path.name} is not a valid project bundle "
            f"(missing {CONFIG_NAME}).")
    return cfg
