"""Central path registry for esfex datasets.

All data lives under ``ESFEX_DATA_ROOT`` (env var, ``~`` expanded). Default:
``~/.esfex/data``, so the caches the Studio writes (Geofabrik extracts, OSM
tiles, CMIP6 subsets) work for every user; point the variable at an existing
dataset tree to use it.

Scripts, figures and code live in the repo (``.../Net_zero_horizon/esfex``);
only datasets are under ``DATA_ROOT``.
"""
from __future__ import annotations

import os
from pathlib import Path

DATA_ROOT = Path(os.environ.get(
    "ESFEX_DATA_ROOT",
    str(Path.home() / ".esfex" / "data"),
)).expanduser()

PROJECT_DATA = DATA_ROOT / "data"

DEMAND_DATASET_DIR = PROJECT_DATA / "demand_dataset"
DEMAND_MANIFEST = DEMAND_DATASET_DIR / "manifest.json"
DEMAND_ERA5_DIR = DEMAND_DATASET_DIR / "_era5"
DEMAND_RAW_DIR = DEMAND_DATASET_DIR / "_raw"
# DEMAND_UTCI_DIR and HREA_DIR removed 2026-04-18: features dropped after
# multicollinearity audit. Data directories also deleted.

GRIDDED_DIR = PROJECT_DATA / "gridded"
GHSL_SMOD_DIR = GRIDDED_DIR / "ghsl_smod"
SHAPEFILES_DIR = GRIDDED_DIR / "shapefiles"
ADMIN1_SHP = SHAPEFILES_DIR / "ne_10m_admin_1" / "ne_10m_admin_1_states_provinces.shp"
GRIDDED_CMIP6_DIR = GRIDDED_DIR / "cmip6"
GRIDDED_GDP_DIR = GRIDDED_DIR / "gdp"
GRIDDED_GDP_025_DIR = GRIDDED_GDP_DIR / "025d"
GRIDDED_POP_DIR = GRIDDED_DIR / "population"
GRIDDED_POP_SSP2_DIR = GRIDDED_DIR / "population_ssp2"
GRIDDED_NIGHTLIGHTS_DIR = GRIDDED_DIR / "nightlights"

MODELS_DIR = PROJECT_DATA / "models"
# The trained models the Studio runs on ship inside the package (XGBoost
# binary format). A model of the same name in MODELS_DIR (a retrained one)
# takes precedence; see model_path().
BUNDLED_MODELS_DIR = Path(__file__).resolve().parent / "assets" / "models"
CMIP6_DIR = PROJECT_DATA / "cmip6"
# Regional subsets of the public Google Cloud (Pangeo) CMIP6 Zarr store, cached
# as NetCDF so country-scale climate reads are offline after the first fetch and
# honour the user-selected SSP. See models/cmip6_zarr.py.
CMIP6_ZARR_DIR = PROJECT_DATA / "cmip6_zarr"
OSM_TILE_CACHE_DIR = PROJECT_DATA / "osm_tile_cache"
# Geofabrik regional .osm.pbf extracts + the region index, cached for reuse.
# Country-scale grid builds read these locally instead of the (rate-limited,
# frequently-degraded) live Overpass API. See grid_mapping_geofabrik.py.
GEOFABRIK_DIR = PROJECT_DATA / "geofabrik"
ERA5_DIR = PROJECT_DATA / "era5"
NEW_SOURCES_DIR = PROJECT_DATA / "new_sources"
OWID_DIR = PROJECT_DATA / "owid"
PLEXOS_DIR = PROJECT_DATA / "plexos"
UN_WPP_DIR = PROJECT_DATA / "un_wpp"
WORLDBANK_DIR = PROJECT_DATA / "worldbank"
WORLDBANK_ALL = WORLDBANK_DIR / "wb_all.json"
GEO_ASSETS_DIR = PROJECT_DATA / "geo_assets"

POP_DENSITY_HIST_DIR = DATA_ROOT / "Grided_population_historical"
POP_DENSITY_FUT_DIR = DATA_ROOT / "Grided_population_forecast"
GDP_FULL_DIR = DATA_ROOT / "Grided_GDP"
OUTPUT_DIR = DATA_ROOT / "output"

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = REPO_ROOT / "results"
DEMAND_ESTIMATION_RESULTS = RESULTS_DIR / "demand_estimation"


def model_path(name: str, bundled: str | None = None) -> Path:
    """Path of the trained model ``name``: the copy in MODELS_DIR when there
    is one (a model retrained locally), else the one shipped with ESFEX,
    stored as ``bundled`` (default: the same name)."""
    local = MODELS_DIR / name
    if local.exists():
        return local
    return BUNDLED_MODELS_DIR / (bundled or name)
