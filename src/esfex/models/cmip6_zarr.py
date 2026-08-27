"""CMIP6 climate from the public Google Cloud (Pangeo) Zarr store.

The climate analog of the Geofabrik OSM backend: open the chosen model's daily
``tasmax``/``tasmin`` Zarr lazily, subset to the study domain + horizon, and read
per node locally. No per-cell API, no rate limits — and, crucially, it honours
the **SSP the user selects in Grid Builder step 3** (``cfg.ssp_scenario``), so the
temperature-driven part of the demand forecast actually responds to the chosen
pathway. Open-Meteo's climate API is locked to a single implicit scenario
(CMCC-CM2-VHR4 / HighResMIP ≈ SSP5-8.5) regardless of that selection.

Store layout (public, anonymous):
    gs://cmip6/CMIP6/ScenarioMIP/<institution>/<source_id>/<experiment>/
        <member>/day/<variable>/<grid_label>/<version>
"""
from __future__ import annotations

import hashlib
import logging
import os

logger = logging.getLogger(__name__)

# The SSP the user picks (SSP1..SSP5) is a *coupled* socioeconomic + forcing
# narrative; the climate experiment must match it, not be chosen separately.
# SSP1..3/5 have Tier-1 ScenarioMIP runs; SSP4 pairs with the sparser Tier-2
# SSP4-6.0 (ssp460).
_SSP_TO_SCENARIO = {
    "1": "ssp126", "2": "ssp245", "3": "ssp370", "4": "ssp460", "5": "ssp585",
}

# Default CMIP6 model: high-resolution, well validated, and carries all four
# Tier-1 SSPs as daily tasmax/tasmin. Override with ESFEX_CMIP6_MODEL.
_DEFAULT_MODEL = os.environ.get("ESFEX_CMIP6_MODEL", "MPI-ESM1-2-HR")

_CMIP6_ZARR_BASE = "cmip6/CMIP6/ScenarioMIP"


def scenario_for_ssp(ssp) -> str | None:
    """Map an SSP spec ('SSP2', 'ssp2', 2, …) to its CMIP6 experiment id."""
    from esfex.models.demand_density_ml import _normalize_ssp
    return _SSP_TO_SCENARIO.get(_normalize_ssp(ssp))


def _resolve_zstore(fs, model: str, scenario: str, variable: str) -> str | None:
    """Discover the ``gs://`` Zarr path for one (model, scenario, variable),
    preferring member ``r1i1p1f1`` and the latest version — no need to download
    the ~1 GB catalog."""
    try:
        institutions = fs.ls(_CMIP6_ZARR_BASE, detail=False)
    except Exception as exc:
        logger.warning("CMIP6 Zarr: cannot list the store (%s).", exc)
        return None
    for inst in institutions:
        base = f"{inst}/{model}/{scenario}"
        try:
            if not fs.exists(base):
                continue
            members = sorted(fs.ls(base, detail=False))
        except Exception:
            continue
        preferred = [m for m in members
                     if m.rstrip("/").split("/")[-1] == "r1i1p1f1"] or members
        for mem in preferred:
            vpath = f"{mem}/day/{variable}"
            try:
                if not fs.exists(vpath):
                    continue
                for grid in fs.ls(vpath, detail=False):        # grid_label(s)
                    versions = sorted(fs.ls(grid, detail=False))
                    if versions:
                        return "gs://" + versions[-1].lstrip("/")
            except Exception:
                continue
    return None


def _open_zarr(zstore: str):
    import xarray as xr
    try:
        return xr.open_zarr(zstore, storage_options={"token": "anon"},
                            consolidated=True)
    except Exception:
        return xr.open_zarr(zstore, storage_options={"token": "anon"},
                            consolidated=False)


def _coord_names(ds):
    lat = "lat" if "lat" in ds.coords else "latitude"
    lon = "lon" if "lon" in ds.coords else "longitude"
    return lat, lon


def _open_regional(model, scenario, node_lats, node_lons, start, end):
    """Regional daily tasmax/tasmin dataset for the domain + horizon, cached as
    NetCDF so rebuilds are offline. Returns an xarray Dataset or ``None``."""
    import numpy as np
    import xarray as xr

    from esfex.paths import CMIP6_ZARR_DIR

    south, north = min(node_lats) - 2.0, max(node_lats) + 2.0
    west, east = min(node_lons) - 2.0, max(node_lons) + 2.0
    key = hashlib.md5(
        f"{model}|{scenario}|{south:.1f}|{west:.1f}|{north:.1f}|{east:.1f}"
        f"|{start}|{end}".encode()
    ).hexdigest()[:12]
    cache = CMIP6_ZARR_DIR / f"{model}_{scenario}_{start}_{end}_{key}.nc"
    if cache.exists():
        try:
            return xr.open_dataset(cache)
        except Exception:
            pass  # corrupt cache → refetch

    import gcsfs
    fs = gcsfs.GCSFileSystem(token="anon")

    def _subset(d):
        latn, lonn = _coord_names(d)
        lats = d[latn].values
        lons = d[lonn].values
        latmask = (lats >= south) & (lats <= north)
        w, e = west % 360, east % 360
        lonmask = ((lons >= w) & (lons <= e)) if w <= e else \
                  ((lons >= w) | (lons <= e))
        d = d.isel({latn: np.where(latmask)[0], lonn: np.where(lonmask)[0]})
        return d.sel(time=slice(str(start), str(end)))

    parts = []
    for var in ("tasmax", "tasmin"):
        z = _resolve_zstore(fs, model, scenario, var)
        if z is None:
            logger.warning(
                "CMIP6 Zarr: %s / %s / %s not in the store.",
                model, scenario, var)
            return None
        parts.append(_subset(_open_zarr(z))[[var]])

    # tasmax and tasmin are distinct variables on identical coords → override.
    ds = xr.merge(parts, join="inner", compat="override")
    # Drop the auxiliary bounds/scalar-height variables the model ships: we
    # don't use them and their inherited encodings make the NetCDF cache writer
    # fail with an HDF error. Clear all encodings for the same reason.
    drop = [v for v in ds.variables
            if str(v).endswith(("_bnds", "_bounds")) or v == "height"]
    ds = ds.drop_vars(drop, errors="ignore").load()
    for v in ds.variables:
        ds[v].encoding = {}
    try:
        CMIP6_ZARR_DIR.mkdir(parents=True, exist_ok=True)
        # Write NetCDF3 via scipy: the HDF5 engines (netcdf4/h5netcdf) fail with
        # an HDF error on network / NTFS-style mounts (DATA_ROOT is often such a
        # volume), and NetCDF3 avoids HDF5 file locking entirely. Subsets are a
        # few MB, well within the 64-bit-offset format.
        ds.to_netcdf(cache, engine="scipy", format="NETCDF3_64BIT")
    except Exception as exc:
        logger.debug("CMIP6 Zarr: could not cache subset (%s).", exc)
        try:
            if cache.exists():
                cache.unlink()  # don't leave a truncated cache that fails reads
        except Exception:
            pass
    return ds


def _cell_to_hourly(sel, start, end):
    """One grid cell's daily tasmax/tasmin (Kelvin) → {year: ndarray(8760)}."""
    import numpy as np

    from esfex.models.demand_projection import _daily_to_hourly_temperature

    tmax = np.asarray(sel["tasmax"].values, dtype=np.float64) - 273.15
    tmin = np.asarray(sel["tasmin"].values, dtype=np.float64) - 273.15
    yrs = sel["time"].dt.year.values
    out: dict[int, np.ndarray] = {}
    for yr in range(start, end + 1):
        m = yrs == yr
        n = int(m.sum())
        if n < 300:                       # tolerate 360-day model calendars
            continue
        tmn = np.nan_to_num(tmin[m], nan=15.0)
        tmx = np.nan_to_num(tmax[m], nan=25.0)
        hourly = _daily_to_hourly_temperature(tmn, tmx, n)
        if len(hourly) < 8760:
            hourly = np.pad(hourly, (0, 8760 - len(hourly)), mode="edge")
        out[yr] = hourly[:8760].astype(np.float64)
    return out or None


def fetch_cmip6_zarr_node_temps(
    node_lats, node_lons, base_year, years, ssp,
    model: str = _DEFAULT_MODEL,
):
    """Per-node CMIP6 hourly temperature for the SSP the user selected, read from
    the local Zarr store. Returns a list (one per node) of ``{year: ndarray}`` or
    ``None`` per node; returns ``None`` overall to signal the caller to fall back
    (unsupported SSP, model/scenario missing, or the store is unreachable)."""
    scenario = scenario_for_ssp(ssp)
    if not scenario:
        logger.info("CMIP6 Zarr: SSP %s has no mapped scenario; deferring.", ssp)
        return None
    start = base_year
    end = base_year + max(years - 1, 0)

    ds = _open_regional(model, scenario, list(node_lats), list(node_lons),
                        start, end)
    if ds is None:
        return None

    latn, lonn = _coord_names(ds)
    logger.info(
        "CMIP6 Zarr: %s / %s over %d node(s) (local, scenario-consistent).",
        model, scenario, len(node_lats))

    by_cell: dict[tuple, object] = {}
    out = []
    for la, lo in zip(node_lats, node_lons):
        try:
            sel = ds.sel({latn: float(la), lonn: float(lo) % 360},
                         method="nearest")
            key = (round(float(sel[latn]), 4), round(float(sel[lonn]), 4))
            if key not in by_cell:
                by_cell[key] = _cell_to_hourly(sel, start, end)
            out.append(by_cell[key])
        except Exception as exc:
            logger.warning("CMIP6 Zarr: node (%.2f,%.2f) failed: %s", la, lo, exc)
            out.append(None)
    return out
