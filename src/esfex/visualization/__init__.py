"""ESFEX Studio — GIS-based power system designer.

Usage::

    from esfex.visualization import launch_studio

    # Create a new grid from scratch
    config = launch_studio()

    # Edit an existing YAML configuration
    config = launch_studio("configs/cuba_system.yaml")
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional, Union

if TYPE_CHECKING:
    from esfex.config.schema import ESFEXConfig

__all__ = ["launch_studio"]


def _ensure_qt_runtime_on_path() -> None:
    """Make Qt's DLLs resolvable on Windows, with PySide6's own copy winning.

    Two failure modes this guards against (no-op off Windows):

    1. **Wrong Qt loaded → import crash.** If a *different* Qt is reachable on
       PATH (a conda ``qt-main``/``pyqt``/``qt6`` in ``<prefix>/Library/bin``,
       common in base Anaconda), ``QtWidgets.pyd`` can bind ``Qt6Widgets.dll``
       to that mismatched ``Qt6Core.dll`` and die with ERROR_PROC_NOT_FOUND
       ("DLL load failed ... the specified procedure could not be found"). We
       prepend PySide6's own package dir **first** so its bundled Qt wins; when
       PySide6 is self-contained (ships ``Qt6Core.dll``) we don't add the conda
       dirs at all, so they can never shadow it.
    2. **WebEngine child can't find its DLLs.** On a conda-forge layout the Qt
       DLLs live in ``<prefix>/Library/bin`` and ``QtWebEngineProcess.exe`` in
       ``<prefix>/Library/lib/qt6``; launched outside an activated env (the
       installer shortcut) those aren't on PATH and the helper dies with
       STATUS_DLL_NOT_FOUND, crash-looping the map. Child processes inherit
       ``os.environ['PATH']``, so we add them (only when Qt is conda-provided).
    """
    import os
    import sys

    if sys.platform != "win32":
        return

    dirs: list[str] = []

    pyside_dir = None
    try:
        import importlib.util
        spec = importlib.util.find_spec("PySide6")
        if spec and spec.submodule_search_locations:
            pyside_dir = spec.submodule_search_locations[0]
    except Exception:
        pyside_dir = None

    # A pip PySide6 wheel ships Qt6Core.dll next to itself → self-contained.
    self_contained = bool(
        pyside_dir and os.path.isfile(os.path.join(pyside_dir, "Qt6Core.dll")))
    if pyside_dir:
        dirs.append(pyside_dir)  # highest priority

    if not self_contained:
        # Qt is provided by conda: add its dirs at LOWER priority so they can
        # never shadow a bundled PySide6 Qt, but still feed the WebEngine child.
        libbin = os.path.join(sys.prefix, "Library", "bin")
        if os.path.isdir(libbin):
            dirs.append(libbin)
            qt6 = os.path.join(sys.prefix, "Library", "lib", "qt6")
            if os.path.isdir(qt6):
                dirs.append(qt6)

    ordered: list[str] = []
    for d in dirs:
        if d and os.path.isdir(d) and d not in ordered:
            ordered.append(d)
    if not ordered:
        return
    os.environ["PATH"] = (os.pathsep.join(ordered) + os.pathsep
                          + os.environ.get("PATH", ""))
    for d in ordered:
        try:
            os.add_dll_directory(d)
        except (OSError, AttributeError):
            pass


def _raise_qt_import_error(exc: ImportError) -> None:
    """Re-raise a PySide6 import failure with guidance that fits the cause."""
    import sys

    if sys.platform == "win32" and "DLL load failed" in str(exc):
        # PySide6 *is* installed; its Qt runtime failed to load — almost always
        # a clash with another Qt on PATH or a wrong-environment launch.
        raise ImportError(
            "PySide6 is installed but its Qt runtime failed to load:\n"
            f"    {exc}\n\n"
            "On Windows this is a Qt DLL conflict, not a missing package. "
            "Usual causes and fixes:\n"
            "  • Another Qt on PATH — a conda 'qt-main' / 'pyqt' / 'qt6' "
            "package (common in base Anaconda) shadows PySide6's own Qt. "
            "Install ESFEX in a clean, dedicated environment:\n"
            "        conda create -n esfex python=3.11\n"
            "        conda activate esfex\n"
            "        pip install esfex\n"
            "  • Wrong environment — 'esfex' is resolving to a different env "
            "than the active one. Check with 'where esfex'; it must live under "
            "the *active* environment, not base Anaconda.\n"
            "  • Or use the standalone ESFEX Studio Windows installer, which "
            "bundles its own isolated Qt."
        ) from exc

    raise ImportError(
        "PySide6 is required for the Studio. It ships with esfex; "
        "reinstall with: pip install --upgrade --force-reinstall esfex"
    ) from exc


def launch_studio(
    config: Optional[Union["ESFEXConfig", str, Path]] = None,
    system: Optional[str] = None,
    blocking: bool = True,
) -> Optional["ESFEXConfig"]:
    """Launch the GIS-based power system editor.

    Parameters
    ----------
    config : ESFEXConfig | str | Path | None
        Existing configuration to edit.  Pass a :class:`ESFEXConfig`,
        a path to a YAML file, or ``None`` to start from scratch.
    system : str | None
        System name to focus on (default: first system in config).
    blocking : bool
        If ``True`` (default), block until the editor window is closed
        and return the (possibly modified) configuration.

    Returns
    -------
    ESFEXConfig | None
        The configuration object if the user saved, ``None`` if they
        cancelled or closed without saving.
    """
    # Must run before QtWebEngine spawns its render helper, or the map
    # crash-loops with STATUS_DLL_NOT_FOUND when launched outside an
    # activated conda env (e.g. the installer shortcut).
    _ensure_qt_runtime_on_path()

    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        _raise_qt_import_error(exc)

    from esfex.visualization.app import _get_or_create_app, run_studio

    app = _get_or_create_app()
    window = run_studio(config=config, system=system)

    if blocking:
        app.exec()
        return getattr(window, "_result_config", None)

    return None
