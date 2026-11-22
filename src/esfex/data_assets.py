"""Datasets the Studio downloads on first use.

The demand forecast samples population density (GPW v4) and gridded GDP
(0.25°, historical and SSP) at the study's cells. Those rasters are too large
to ship in the package, so they are published as assets of the ESFEX
``data-v1`` GitHub release and fetched the first time a forecast needs them,
into the same place under ``ESFEX_DATA_ROOT`` where a full local dataset
keeps them. A file already there (a user's own dataset) is used as it is.

Population comes in 30° tiles, so a study downloads only the tiles it
covers; the GDP rasters are about 1 MB each and come whole. The bundled
manifest lists every asset with its size and SHA-256, checked on download.

``ESFEX_ASSETS_URL`` points the downloads at a mirror (any URL ``urllib``
opens, ``file://`` included).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import urllib.request
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_MANIFEST_PATH = Path(__file__).resolve().parent / "assets" / "data_assets.json"
_RELEASE_URL = "https://github.com/Net-Zero-Horizon/ESFEX/releases/download/{tag}"

_lock = threading.Lock()
_failed: dict[str, str] = {}          # asset name -> reason, for this session


@lru_cache(maxsize=1)
def manifest() -> dict:
    """The bundled asset manifest: ``{"tag": ..., "assets": {name: {...}}}``."""
    try:
        return json.loads(_MANIFEST_PATH.read_text())
    except (OSError, ValueError):
        return {"tag": "", "assets": {}}


def base_url() -> str:
    return os.environ.get("ESFEX_ASSETS_URL") or _RELEASE_URL.format(
        tag=manifest().get("tag", ""))


def published(name: str) -> bool:
    """True if ``name`` is one of the downloadable assets."""
    return name in manifest().get("assets", {})


def ensure(local: Path, name: str | None = None) -> bool:
    """Make ``local`` exist, downloading asset ``name`` (default: the file
    name) when it is missing and published. False when it is neither on
    disk nor obtainable; the reason is kept for :func:`failures`."""
    if local.exists():
        return True
    name = name or local.name
    meta = manifest().get("assets", {}).get(name)
    if meta is None:
        return False
    with _lock:
        if local.exists():
            return True
        if name in _failed:
            return False
        url = f"{base_url().rstrip('/')}/{name}"
        tmp = local.with_name(local.name + ".part")
        try:
            local.parent.mkdir(parents=True, exist_ok=True)
            h = hashlib.sha256()
            with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as fh:
                for chunk in iter(lambda: resp.read(1 << 20), b""):
                    h.update(chunk)
                    fh.write(chunk)
            if h.hexdigest() != meta["sha256"]:
                raise ValueError("checksum mismatch")
            tmp.replace(local)
            logger.info("Downloaded %s (%.1f MB)", name, meta.get("size", 0) / 1e6)
            return True
        except Exception as exc:  # network, HTTP, disk, checksum
            _failed[name] = str(exc)
            logger.warning("Could not download %s from %s: %s", name, url, exc)
            try:
                tmp.unlink()
            except OSError:
                pass
            return False


def failures() -> dict[str, str]:
    """Assets that could not be downloaded in this session, with the reason."""
    return dict(_failed)


def clear_failures() -> None:
    """Forget past download failures, so the next use tries again."""
    _failed.clear()


# ── GPW v4 population density, 30° tiles ──────────────────────────────────


def gpw_tile_name(epoch: int, lat: float, lon: float) -> str:
    """Name of the GPW tile holding (lat, lon): 30° tiles named after their
    north-west corner, e.g. ``gpw_v4_popdens_2020_n30w090.tif``."""
    row = min(max(int((90.0 - lat) // 30), 0), 5)
    col = min(max(int((lon + 180.0) // 30), 0), 11)
    lat_top, lon0 = 90 - 30 * row, -180 + 30 * col
    return (f"gpw_v4_popdens_{epoch}_{'n' if lat_top >= 0 else 's'}{abs(lat_top):02d}"
            f"{'e' if lon0 >= 0 else 'w'}{abs(lon0):03d}.tif")
