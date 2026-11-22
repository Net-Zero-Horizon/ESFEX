"""Where a Studio project's runs write their results, and which results file
the project remembers.

* A run of a saved project writes to ``<project folder>/results`` (the folder
  of the saved config), not to the ``results`` folder of the directory the
  Studio was started from. A run of a never-saved project writes to
  ``studio_results`` under the ESFEX cache directory. The CLI keeps its own
  default (``./results`` relative to the working directory).
* The project remembers its last results file in the GUI-only top-level YAML
  key ``_last_results``: relative to the config's folder when the file lies
  under it, absolute otherwise. The solver and the unknown-key check ignore
  it (:mod:`esfex.config.unknown_keys`).

Pure (no Qt) so it is unit-testable.
"""

from __future__ import annotations

import os
from pathlib import Path

RESULTS_KEY = "_last_results"
RESULTS_DIRNAME = "results"
UNSAVED_RESULTS_DIRNAME = "studio_results"


def esfex_cache_dir() -> Path:
    """The ESFEX cache directory, ``~/.cache/esfex`` (where the Studio's
    autosave and the weather / hazard caches live)."""
    return Path.home() / ".cache" / "esfex"


def project_output_dir(config_path: str | os.PathLike | None) -> Path:
    """Absolute output folder of a Studio run: ``<config folder>/results`` for
    a saved project, ``<cache dir>/studio_results`` for an unsaved one."""
    if config_path:
        return Path(os.path.abspath(Path(config_path).parent)) / RESULTS_DIRNAME
    return esfex_cache_dir() / UNSAVED_RESULTS_DIRNAME


def newest_results_file(output_dir: str | os.PathLike,
                        since: float | None = None) -> Path | None:
    """The newest results file (``*.h5``, MGA files excluded) directly in
    ``output_dir`` whose modification time is at or after ``since`` (a
    ``time.time()`` stamp; ``None`` accepts any), or ``None``."""
    d = Path(output_dir)
    if not d.is_dir():
        return None
    best: tuple[float, Path] | None = None
    for p in d.glob("*.h5"):
        if p.stem.startswith("mga_") or not p.is_file():
            continue
        mtime = p.stat().st_mtime
        if since is not None and mtime < since:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, p)
    return best[1] if best else None


def results_ref_for(results_file: str | os.PathLike,
                    config_dir: str | os.PathLike) -> str:
    """The ``_last_results`` value for a config written to ``config_dir``:
    a POSIX path relative to it when the file lies under it, else absolute."""
    target = Path(os.path.abspath(results_file))
    base = Path(os.path.abspath(config_dir))
    try:
        return target.relative_to(base).as_posix()
    except ValueError:
        return str(target)


def remembered_results(raw: dict | None,
                       base_dir: str | os.PathLike | None) -> Path | None:
    """The results file a loaded config remembers, as an absolute path, or
    ``None`` when it remembers none or the file no longer exists. A relative
    reference resolves against ``base_dir`` (the folder the config was read
    from)."""
    ref = (raw or {}).get(RESULTS_KEY) if isinstance(raw, dict) else None
    if not isinstance(ref, str) or not ref.strip():
        return None
    p = Path(ref).expanduser()
    if not p.is_absolute():
        if not base_dir:
            return None
        p = Path(base_dir) / p
    p = Path(os.path.abspath(p))
    return p if p.is_file() else None


def format_size(n_bytes: int) -> str:
    """Human-readable size (``"4.7 GB"``)."""
    if n_bytes < 1024:
        return f"{n_bytes} B"
    size = n_bytes / 1024
    for unit in ("KB", "MB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
