"""Hourly demand-DENSITY model (XGBoost) for spatial per-node demand.

The model predicts ``log10(demand_mw / area_km2)`` per 0.25° cell and hour
from the place (population and GDP density, GDP per person, position, long-run
climate, how electricity is used there), the temperature of the hour and the
calendar. Multiplying the predicted density by each cell's area and summing
the cells of a node gives a spatially resolved hourly demand.

The shipped model is the final model of the article "Global map of
electricity demand sensitivity to warming learned from observed hourly load"
(specification ``level_geo_two_shallow``; provenance in its meta file and in
``scripts/demand_model``). It was trained in two rounds: a shallow booster on
the fixed predictors of a place gives its level, which is the base margin of
the hourly booster. Its hours are UTC: :func:`predict_region_cell_demand`
returns UTC rows (8760 per year, 29 February skipped) and the caller puts them
on the time base it needs. The predictors are built in
:mod:`esfex.models.demand_density_features`, as the article's training table
built them.

Files (shipped in ``esfex/assets/models``; a copy of the three model files in
``MODELS_DIR`` takes precedence):
    demand_density_v2.ubj            — hourly round (``.json`` accepted)
    demand_density_v2.level.ubj      — level round
    demand_density_v2.meta.json      — features, training ranges, provenance
    demand_density_v2.grids.npz      — climate normals and energy-use context
    demand_density_v2.countries.csv  — per country: context, time-zone offsets

Pipeline usage lives in
``esfex.visualization.workflows.demand_estimation_analysis.DemandProfileBuilder._build_density``.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Optional

import numpy as np

from esfex.models import demand_density_features as dfeat
from esfex.paths import BUNDLED_MODELS_DIR, model_path

logger = logging.getLogger(__name__)

MODEL_STEM = "demand_density_v2"
_MODEL_FILENAME = f"{MODEL_STEM}.json"
_MODEL_BUNDLED = f"{MODEL_STEM}.ubj"      # same booster, binary

# ──────────────────────────────────────────────────────────────────────────────
# Model wrapper
# ──────────────────────────────────────────────────────────────────────────────


def _model_files() -> Optional[tuple[Path, Path, Path]]:
    """(hourly, level, meta) files of the model in use, or None if incomplete.

    A model in MODELS_DIR replaces the shipped one only as a whole: its level
    round and meta file are read from the same folder. An incomplete copy
    there is ignored, with a warning.
    """
    def complete(hourly: Path):
        folder = hourly.parent
        level = next((folder / f"{MODEL_STEM}.level{ext}" for ext in (".ubj", ".json")
                      if (folder / f"{MODEL_STEM}.level{ext}").exists()), None)
        meta = folder / f"{MODEL_STEM}.meta.json"
        if not hourly.exists() or level is None or not meta.exists():
            return None
        return hourly, level, meta

    hourly = model_path(_MODEL_FILENAME, _MODEL_BUNDLED)
    found = complete(hourly)
    if found is None and hourly.parent != BUNDLED_MODELS_DIR:
        logger.warning("%s lacks its level round or meta file; using the model "
                       "shipped with ESFEX.", hourly)
        found = complete(BUNDLED_MODELS_DIR / _MODEL_BUNDLED)
    return found


def _context_dir(model_dir: Path) -> Path:
    """Folder of the grids and country table: the model's, else the shipped one."""
    if (model_dir / f"{MODEL_STEM}.grids.npz").exists():
        return model_dir
    return BUNDLED_MODELS_DIR


class DemandDensityModel:
    """Two-round XGBoost hourly demand-density predictor (``log10(MW/km²)``)."""

    def __init__(self) -> None:
        self._model = None            # hourly round
        self._level = None            # level round
        self.feature_names: list[str] = []
        self.level_features: list[str] = []
        self.target: str = ""
        self.meta: dict = {}
        self.context: Optional[dfeat.DensityContext] = None

    @classmethod
    def is_available(cls) -> bool:
        """True if the model files are present and xgboost importable."""
        if _model_files() is None:
            return False
        try:
            import xgboost  # noqa: F401
        except ImportError:
            return False
        return True

    @classmethod
    def load_bundled(cls) -> "DemandDensityModel":
        """Load the density model, its meta file and context: a copy in
        MODELS_DIR if there is one, else the model shipped with ESFEX."""
        files = _model_files()
        if files is None:
            raise FileNotFoundError(
                f"No complete demand-density model ({MODEL_STEM}.ubj, "
                f"{MODEL_STEM}.level.ubj, {MODEL_STEM}.meta.json) at "
                f"{model_path(_MODEL_FILENAME, _MODEL_BUNDLED).parent}: the ESFEX "
                "installation is missing its bundled models; reinstall ESFEX.")
        try:
            import xgboost as xgb
        except ImportError as exc:  # pragma: no cover - env dependent
            raise ImportError(
                "xgboost is required for the demand-density model."
            ) from exc
        hourly, level, meta_path = files
        inst = cls()
        inst._model = xgb.Booster()
        inst._model.load_model(str(hourly))
        inst._level = xgb.Booster()
        inst._level.load_model(str(level))
        inst.meta = json.loads(meta_path.read_text())
        inst.feature_names = list(inst.meta["features"])
        inst.level_features = list(inst.meta["level_features"])
        inst.target = inst.meta.get("target", "log10(demand_mw/area_km2)")
        inst.context = dfeat.DensityContext.load(_context_dir(hourly.parent), MODEL_STEM)
        logger.info("Loaded demand-density model from %s (%d features, level round "
                    "on %d)", hourly, len(inst.feature_names), len(inst.level_features))
        return inst

    @property
    def hdd_base(self) -> float:
        return float(self.meta.get("hdd_base", dfeat.HDD_BASE))

    @property
    def cdd_base(self) -> float:
        return float(self.meta.get("cdd_base", dfeat.CDD_BASE))

    def level_margin(self, statics: np.ndarray) -> np.ndarray:
        """Level round on rows of ``self.level_features``: the base margin of
        the hourly round (``log10`` density)."""
        from esfex.models.xgb_device import predict as _xgb_predict

        return np.asarray(_xgb_predict(self._level, statics,
                                       feature_names=self.level_features),
                          dtype=np.float32)

    def predict_log_density(self, features: np.ndarray,
                            base_margin: Optional[np.ndarray] = None) -> np.ndarray:
        """``log10`` density for rows of ``self.feature_names``.

        The level round runs on the rows' level features unless its result
        (*base_margin*, one value per row) is given; the hourly round starts
        from it. Same as the article's ``model.predict``.
        """
        from esfex.models.xgb_device import predict as _xgb_predict

        if self._model is None:
            raise RuntimeError("Model not loaded. Call load_bundled() first.")
        X = np.ascontiguousarray(features, dtype=np.float32)
        if base_margin is None:
            cols = [self.feature_names.index(f) for f in self.level_features]
            base_margin = self.level_margin(X[:, cols])
        return np.asarray(_xgb_predict(self._model, X, feature_names=self.feature_names,
                                       base_margin=base_margin), dtype=np.float64)

    def predict_density_mw_per_km2(self, features: np.ndarray) -> np.ndarray:
        """Predict demand density (MW/km²) for a feature matrix.

        ``features`` must have columns in ``self.feature_names`` order.
        Returns a 1-D array of MW/km² (= ``10 ** model_output``).
        """
        return np.power(10.0, self.predict_log_density(features))


# ──────────────────────────────────────────────────────────────────────────────
# Feature construction
# ──────────────────────────────────────────────────────────────────────────────


def clean_temperature(temperature_hourly, hpy: int = 8760) -> np.ndarray:
    """*hpy* hours of temperature (°C) with non-finite values replaced.

    A short or missing series is 20 °C throughout; NaN/inf hours (e.g. a
    degraded ERA5 fallback after a CMIP6 fetch failure) take the mean of the
    finite ones, so they never reach HDD/CDD and the predictors.
    """
    temp = (np.asarray(temperature_hourly[:hpy], dtype=np.float64).copy()
            if temperature_hourly is not None and len(temperature_hourly) >= hpy
            else np.full(hpy, 20.0, dtype=np.float64))
    bad = ~np.isfinite(temp)
    if bad.any():
        temp[bad] = float(temp[~bad].mean()) if (~bad).any() else 20.0
    return temp


def density_feature_block(
    *,
    latitude: np.ndarray,
    longitude: np.ndarray,
    log_pop_density: np.ndarray,
    log_gdp_density: np.ndarray,
    log_gdp_per_cap: np.ndarray,
    t_mean: np.ndarray,
    t_range: np.ndarray,
    hh_elec_share: np.ndarray,
    bld_share_elec: np.ndarray,
    temperature_utc: np.ndarray,
    year: int,
    feature_order: list[str],
    country_iso: str,
    offset_hours: np.ndarray,
    hdd_base: float = dfeat.HDD_BASE,
    cdd_base: float = dfeat.CDD_BASE,
) -> np.ndarray:
    """Predictor matrix for *n* places over the 8760 UTC rows of *year*.

    Every per-place argument is an array of length *n*; *temperature_utc* is
    one UTC year shared by the places (8760, 29 February skipped).
    *offset_hours* is each place's standard offset for the local date
    (:meth:`DensityContext.std_offset`). Rows run place by place (place 0's
    8760 hours first). Returns float32 columns in *feature_order*.
    """
    n = len(latitude)
    hpy = len(temperature_utc)
    idx = dfeat.utc_row_hours(year, hpy)
    temp = np.asarray(temperature_utc, dtype=np.float32)
    shared = {"temperature": temp,
              "hdd": np.maximum(hdd_base - temp.astype(np.float64), 0.0),
              "cdd": np.maximum(temp.astype(np.float64) - cdd_base, 0.0)}
    per_place = {"latitude": latitude, "longitude": longitude,
                 "log_pop_density": log_pop_density, "log_gdp_density": log_gdp_density,
                 "log_gdp_per_cap": log_gdp_per_cap, "t_mean": t_mean, "t_range": t_range,
                 "hh_elec_share": hh_elec_share, "bld_share_elec": bld_share_elec}
    X = np.empty((n * hpy, len(feature_order)), dtype=np.float32)
    col = {f: j for j, f in enumerate(feature_order)}
    unknown = [f for f in feature_order if f not in shared and f not in per_place
               and f not in dfeat.CAL_UTC and f not in dfeat.CAL_LOCAL]
    if unknown:
        raise ValueError(f"Density feature(s) not built: {unknown}")
    for f, v in per_place.items():
        if f in col:
            X[:, col[f]] = np.repeat(np.asarray(v, dtype=np.float64), hpy)
    for f, v in shared.items():
        if f in col:
            X[:, col[f]] = np.tile(v, n)
    offsets = np.asarray(offset_hours, dtype=np.float64)
    for off in np.unique(offsets):
        places = np.where(offsets == off)[0]
        cal = dfeat.utc_calendar(country_iso, year, idx, float(off))
        rows = (places[:, None] * hpy + np.arange(hpy)[None, :]).ravel()
        for f in dfeat.CAL_UTC:
            if f in col:
                X[rows, col[f]] = np.tile(cal[f], len(places))
        loc = dfeat.local_solar_calendar(
            cal["hour_sin"][None, :], cal["hour_cos"][None, :],
            cal["month_sin"][None, :], cal["month_cos"][None, :],
            np.asarray(latitude, dtype=np.float64)[places, None],
            np.asarray(longitude, dtype=np.float64)[places, None])
        for f in dfeat.CAL_LOCAL:
            if f in col:
                X[rows, col[f]] = np.broadcast_to(loc[f], (len(places), hpy)).ravel()
    return X


def place_predictors(model: "DemandDensityModel", lats, lons, country_iso: str,
                     fallback_temperature_utc: Optional[np.ndarray] = None,
                     fallback_year: Optional[int] = None) -> dict:
    """The climate normals, energy-use context and local-date offset of places.

    From the shipped grids (:class:`DensityContext`). A place with no land cell
    within 1° takes ``t_mean``/``t_range`` from the daily means of
    *fallback_temperature_utc* (one UTC year, *fallback_year*), the definition
    of the training table; without it they stay NaN (the model's
    missing-value branches).
    """
    ctx = model.context
    lats = np.asarray(lats, dtype=np.float64)
    lons = np.asarray(lons, dtype=np.float64)
    tm, tr = ctx.normals(lats, lons)
    tm = tm.astype(np.float64)
    tr = tr.astype(np.float64)
    missing = ~np.isfinite(tm) | ~np.isfinite(tr)
    if missing.any() and fallback_temperature_utc is not None:
        idx = dfeat.utc_row_hours(int(fallback_year), len(fallback_temperature_utc))
        m, r = dfeat.climate_normals(dfeat.daily_means(fallback_temperature_utc, idx))
        tm[missing], tr[missing] = m, r
        logger.info("Density model: %d place(s) without gridded climate normals use "
                    "the daily means of their temperature year (t_mean %.1f C, "
                    "t_range %.1f C).", int(missing.sum()), m, r)
    hh, bld = ctx.energy_context(lats, lons, country_iso)
    if (~np.isfinite(hh)).any():
        logger.warning("Density model: no electricity-use statistics for %s; %d "
                       "place(s) go without them (missing-value branches).",
                       country_iso or "this country", int((~np.isfinite(hh)).sum()))
    off = np.array([ctx.std_offset(country_iso, float(lo)) for lo in lons])
    return {"t_mean": tm, "t_range": tr, "hh_elec_share": hh, "bld_share_elec": bld,
            "offset_hours": off}


def build_density_features(
    *,
    latitude: float,
    longitude: float,
    log_pop_density: float,
    log_gdp_density: float,
    log_gdp_per_cap: float,
    temperature_hourly: np.ndarray,
    base_year: int,
    feature_order: list[str],
    country_iso: str = "",
    t_mean: float = float("nan"),
    t_range: float = float("nan"),
    hh_elec_share: float = float("nan"),
    bld_share_elec: float = float("nan"),
    offset_hours: Optional[float] = None,
    hdd_base: float = dfeat.HDD_BASE,
    cdd_base: float = dfeat.CDD_BASE,
) -> np.ndarray:
    """One place's predictor matrix for the 8760 UTC rows of *base_year*.

    *temperature_hourly* is the UTC year (29 February skipped); a short series
    is 20 °C and NaN hours take the mean of the rest. *offset_hours* (the
    standard offset of the local date) defaults to the longitude band.
    Columns follow *feature_order*.
    """
    temp = clean_temperature(temperature_hourly)
    off = float(round(longitude / 15)) if offset_hours is None else float(offset_hours)
    one = lambda v: np.array([v], dtype=np.float64)  # noqa: E731
    return density_feature_block(
        latitude=one(latitude), longitude=one(longitude),
        log_pop_density=one(log_pop_density), log_gdp_density=one(log_gdp_density),
        log_gdp_per_cap=one(log_gdp_per_cap), t_mean=one(t_mean), t_range=one(t_range),
        hh_elec_share=one(hh_elec_share), bld_share_elec=one(bld_share_elec),
        temperature_utc=temp, year=base_year, feature_order=feature_order,
        country_iso=country_iso, offset_hours=one(off),
        hdd_base=hdd_base, cdd_base=cdd_base)


# ──────────────────────────────────────────────────────────────────────────────
# Per-CELL inference on the 0.25° grid (matches the training-time pipeline:
# predict a density per 0.25° cell, multiply by its land area, then sum cells).
# ──────────────────────────────────────────────────────────────────────────────

_CELL_DEG = 0.25
_EARTH_R_KM = 6371.0


def cell_area_km2(lat: float | np.ndarray):
    """Area (km²) of a 0.25° grid cell centred at *lat* (latitude-dependent),
    matching the demand-density training pipeline's ``cell_area_km2``."""
    d = math.radians(_CELL_DEG)
    half = _CELL_DEG / 2.0
    lat = np.asarray(lat, dtype=np.float64)
    return np.abs(
        _EARTH_R_KM * _EARTH_R_KM * d
        * (np.sin(np.radians(lat + half)) - np.sin(np.radians(lat - half)))
    )


def cell_overlap_fraction(poly, lats, lons, dlat: float, dlon: float) -> np.ndarray:
    """Fraction of each cell's area inside the study region *poly*.

    The cells are the lat/lon rectangles of *dlat* × *dlon* degrees centred
    at (*lats*, *lons*); *poly* is a shapely geometry in (lon, lat). A cell
    wholly inside is 1 (no intersection computed), one outside it 0, and one
    the boundary crosses ``area(cell ∩ poly) / area(cell)``, so a coastal
    cell whose centre lies at sea still counts with the part of it that is
    in the region."""
    import shapely

    lats = np.asarray(lats, dtype=np.float64)
    lons = np.asarray(lons, dtype=np.float64)
    if not poly.is_valid:
        poly = shapely.make_valid(poly)
    shapely.prepare(poly)
    if dlat <= 0 or dlon <= 0:                # degenerate sampling: the centre decides
        return shapely.contains_xy(poly, lons, lats).astype(np.float64)
    boxes = shapely.box(lons - dlon / 2, lats - dlat / 2, lons + dlon / 2, lats + dlat / 2)
    frac = np.zeros(len(lats), dtype=np.float64)
    hit = shapely.intersects(poly, boxes)
    inner = hit & shapely.contains(poly, boxes)
    frac[inner] = 1.0
    edge = np.where(hit & ~inner)[0]
    if edge.size:
        inter = shapely.area(shapely.intersection(boxes[edge], poly))
        frac[edge] = np.minimum(inter / (dlat * dlon), 1.0)
    return frac


def land_overlap_weight(inside, land_km2, cell_km2) -> np.ndarray:
    """Share of each cell's land that lies in the study region: the weight of
    the cell's demand (``density × land_km2 × w``).

    *inside* is the fraction of the cell's area inside the region
    (:func:`region_cells_025`), *land_km2* the cell's land area (shipped grid)
    and *cell_km2* its total area. ``w = min(1, inside × cell_km2 / land_km2)``:
    a coastal cell whose land the region covers counts whole, and a cell on a
    land border is split between the countries in proportion to their part of
    its land. A cell wholly inside is 1; one without land in the grid
    (``land_km2 = 0``) takes ``inside``."""
    inside = np.asarray(inside, dtype=np.float64)
    land = np.asarray(land_km2, dtype=np.float64)
    cell = np.broadcast_to(np.asarray(cell_km2, dtype=np.float64), inside.shape)
    with np.errstate(divide="ignore", invalid="ignore"):
        w = np.where(land > 0, np.minimum(1.0, inside * cell / land), inside)
    w[inside >= 1.0] = 1.0
    return w


def region_cells_025(
    polygon_latlon: Optional[list[tuple[float, float]]] = None,
    bounds: Optional[tuple[float, float, float, float]] = None,
    node_lats: Optional[list[float]] = None,
    node_lons: Optional[list[float]] = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Enumerate the 0.25° grid cells covering the study region.

    Centroids are aligned to the global 0.25° raster grid so they sample the
    GDP / population rasters at their own cells. Returns ``(lats, lons,
    areas_km2, inside)`` for every cell that overlaps the drawn polygon (or
    lies in the bounding box, or a small pad around the nodes); ``inside`` is
    the fraction of the cell's area in the region (:func:`cell_overlap_fraction`,
    1 without a polygon), which :func:`land_overlap_weight` turns into the
    cell's weight."""
    if bounds is not None:
        s, w, n, e = bounds
    elif node_lats and node_lons:
        s, n = min(node_lats), max(node_lats)
        w, e = min(node_lons), max(node_lons)
        s -= _CELL_DEG; n += _CELL_DEG; w -= _CELL_DEG; e += _CELL_DEG
    elif _as_rings(polygon_latlon):
        pts = [p for r in _as_rings(polygon_latlon) for p in r]
        las = [p[0] for p in pts]; los = [p[1] for p in pts]
        s, n, w, e = min(las), max(las), min(los), max(los)
    else:
        return np.array([]), np.array([]), np.array([]), np.array([])

    # Global-grid-aligned centroids: lat = -90 + (j+0.5)*0.25, lon similarly.
    j0 = int(math.floor((s + 90.0) / _CELL_DEG))
    j1 = int(math.ceil((n + 90.0) / _CELL_DEG))
    k0 = int(math.floor((w + 180.0) / _CELL_DEG))
    k1 = int(math.ceil((e + 180.0) / _CELL_DEG))
    lat_c = np.array([-90.0 + (j + 0.5) * _CELL_DEG for j in range(j0, j1)])
    lon_c = np.array([-180.0 + (k + 0.5) * _CELL_DEG for k in range(k0, k1)])
    LA, LO = np.meshgrid(lat_c, lon_c, indexing="ij")
    lats = LA.ravel(); lons = LO.ravel()

    # Clip cells to the study domain (single ring or multi-ring/archipelago).
    # Without this the cell grid spans the whole bounding box — for an island
    # nation the box reaches neighbouring countries, whose populated cells then
    # get assigned to the nearest border node and inflate its demand (Japan's
    # western nodes absorbed ~45 M of South Korea + ~24 M of China).
    # A cell is kept when it overlaps the domain, with the fraction of it
    # inside: testing only its centre dropped coastal cells centred at sea
    # (Cuba lost 7 % of its population).
    inside = np.ones(len(lats), dtype=np.float64)
    poly = _make_poly(polygon_latlon) if _as_rings(polygon_latlon) else None
    if poly is not None:
        inside = cell_overlap_fraction(poly, lats, lons, _CELL_DEG, _CELL_DEG)
        keep = inside > 0
        lats, lons, inside = lats[keep], lons[keep], inside[keep]

    return lats, lons, cell_area_km2(lats), inside


def _cell_population_pixels(lat: float, lon: float, epoch: int = 2020):
    """Population density of the GPW 30″ pixels inside the 0.25° cell centred
    at (*lat*, *lon*): ``(lats, lons, density)`` of the pixel centres, or
    ``None`` when no raster covers the cell (open ocean, or the tile could not
    be downloaded)."""
    try:
        from rasterio.windows import from_bounds

        from esfex.models.pixel_features import _gpw_path, _open_raster
        path = _gpw_path(epoch, lat, lon)
        if path is None:
            return None
        ds = _open_raster(str(path))
        h = _CELL_DEG / 2.0
        win = from_bounds(lon - h, lat - h, lon + h, lat + h, ds.transform)
        win = win.round_offsets().round_lengths()
        if win.width <= 0 or win.height <= 0:
            return None
        arr = ds.read(1, window=win, boundless=True, fill_value=0).astype(np.float64)
        if ds.nodata is not None:
            arr[arr == ds.nodata] = 0.0
        arr[~np.isfinite(arr) | (arr < 0)] = 0.0
        tr = ds.window_transform(win)
        cols = np.arange(arr.shape[1]) + 0.5
        rows = np.arange(arr.shape[0]) + 0.5
        lons = tr.c + cols * tr.a
        lats = tr.f + rows * tr.e
        grid_lat, grid_lon = np.meshgrid(lats, lons, indexing="ij")
        return grid_lat.ravel(), grid_lon.ravel(), arr.ravel()
    except Exception as exc:                      # no raster: the caller splits by area
        logger.debug("No population pixels for cell (%.3f, %.3f): %s", lat, lon, exc)
        return None


def split_cells_among_nodes(
    cell_lats, cell_lons, node_lats, node_lons,
    polygon_latlon=None, n_sub: int = 10, min_share: float = 0.005,
    pixels_fn=None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """Share of each 0.25° cell that belongs to each node.

    A cell is about 27 km wide; the nodes of a grid are often closer than that.
    Giving a whole cell to the node nearest to its centre left every other node
    in the cell without demand. Here the
    cell is cut into its 30″ population pixels (GPW, about 1 km); each pixel
    goes to its nearest node and weighs its population, so a node takes the
    part of the cell's people that are nearer to it than to any other node.
    Pixels outside the study region weigh nothing. A cell with no population
    raster (or no people in it) is cut into ``n_sub × n_sub`` equal parts
    instead: a split by area.

    Returns ``(share, part_lat, part_lon, n_by_area)``: ``share[c, n]`` is the
    fraction of cell ``c`` that node ``n`` takes (each row sums to 1; shares
    below *min_share* are dropped and the row renormalised), ``part_lat`` /
    ``part_lon`` the weighted centre of that part (NaN where the share is 0),
    and ``n_by_area`` the number of cells split by area. *pixels_fn*
    ``(lat, lon) -> (lats, lons, weights) | None`` replaces the population
    raster (tests)."""
    import shapely

    clat = np.asarray(cell_lats, dtype=np.float64)
    clon = np.asarray(cell_lons, dtype=np.float64)
    nlat = np.asarray(node_lats, dtype=np.float64)
    nlon = np.asarray(node_lons, dtype=np.float64)
    n_c, n_n = len(clat), len(nlat)
    share = np.zeros((n_c, n_n), dtype=np.float64)
    plat = np.full((n_c, n_n), np.nan)
    plon = np.full((n_c, n_n), np.nan)
    if n_c == 0 or n_n == 0:
        return share, plat, plon, 0
    poly = _make_poly(polygon_latlon) if _as_rings(polygon_latlon) else None
    if poly is not None:
        if not poly.is_valid:
            poly = shapely.make_valid(poly)
        shapely.prepare(poly)
    pixels_fn = pixels_fn or _cell_population_pixels
    mlng = math.cos(math.radians(float(np.mean(clat))))
    nx = nlon * mlng
    off = ((np.arange(n_sub) + 0.5) / n_sub - 0.5) * _CELL_DEG
    n_by_area = 0
    for c in range(n_c):
        px = pixels_fn(float(clat[c]), float(clon[c]))
        w = None
        if px is not None:
            la, lo, w = (np.asarray(v, dtype=np.float64) for v in px)
            if poly is not None:
                inside = shapely.contains_xy(poly, lo, la)
                if (w * inside).sum() > 0:
                    w = w * inside
            if w.sum() <= 0:
                w = None
        if w is None:                             # no people known: split by area
            n_by_area += 1
            grid_lat, grid_lon = np.meshgrid(clat[c] + off, clon[c] + off, indexing="ij")
            la, lo = grid_lat.ravel(), grid_lon.ravel()
            w = np.ones(la.size)
            if poly is not None:
                inside = shapely.contains_xy(poly, lo, la)
                if inside.any():
                    w = inside.astype(np.float64)
        keep = w > 0
        la, lo, w = la[keep], lo[keep], w[keep]
        d2 = (lo[:, None] * mlng - nx[None, :]) ** 2 + (la[:, None] - nlat[None, :]) ** 2
        owner = np.argmin(d2, axis=1)
        tot = np.bincount(owner, weights=w, minlength=n_n)
        row = tot / tot.sum()
        small = (row > 0) & (row < min_share)
        if small.any() and (row[~small] > 0).any():
            row[small] = 0.0
            row /= row.sum()
        share[c] = row
        has = row > 0
        with np.errstate(invalid="ignore", divide="ignore"):
            plat[c, has] = (np.bincount(owner, weights=w * la, minlength=n_n) / tot)[has]
            plon[c, has] = (np.bincount(owner, weights=w * lo, minlength=n_n) / tot)[has]
    return share, plat, plon, n_by_area


def predict_region_cell_demand(
    model: "DemandDensityModel",
    lats: np.ndarray,
    lons: np.ndarray,
    areas: np.ndarray,
    log_pop_density: np.ndarray,
    log_gdp_density: np.ndarray,
    log_gdp_per_cap: np.ndarray,
    temperature_utc: np.ndarray,
    year: int,
    country_iso: str = "",
    places: Optional[dict] = None,
    chunk: int = 64,
) -> np.ndarray:
    """Per-cell hourly demand (MW) on UTC rows, shape (n_cells, 8760).

    For every 0.25° cell: predict the demand density (MW/km²) from the cell's
    own predictors and the (shared) UTC hourly temperature and calendar of
    *year*, then multiply by *areas*: the density workflow passes each cell's
    land area times its weight (:func:`land_overlap_weight`). Row ``h`` is UTC
    hour ``h`` of the year with 29 February skipped. *places* holds the cells'
    climate normals, energy-use context and local-date offsets
    (:func:`place_predictors`, computed here when not given)."""
    hpy = 8760
    n = len(lats)
    if n == 0:
        return np.zeros((0, hpy), dtype=np.float64)
    temp = clean_temperature(temperature_utc, hpy)
    lats = np.asarray(lats, dtype=np.float64)
    lons = np.asarray(lons, dtype=np.float64)
    if places is None:
        places = place_predictors(model, lats, lons, country_iso, temp, year)
    stat = {"latitude": lats, "longitude": lons,
            "log_pop_density": np.asarray(log_pop_density, dtype=np.float64),
            "log_gdp_density": np.asarray(log_gdp_density, dtype=np.float64),
            "log_gdp_per_cap": np.asarray(log_gdp_per_cap, dtype=np.float64),
            **{k: np.asarray(places[k], dtype=np.float64)
               for k in ("t_mean", "t_range", "hh_elec_share", "bld_share_elec")}}
    # The level of a place is the same at every hour: one level-round call per cell.
    level = model.level_margin(np.column_stack([stat[f] for f in model.level_features]))
    off = np.asarray(places["offset_hours"], dtype=np.float64)
    out = np.zeros((n, hpy), dtype=np.float64)
    for s in range(0, n, chunk):
        e = min(s + chunk, n)
        X = density_feature_block(
            **{k: v[s:e] for k, v in stat.items()},
            temperature_utc=temp, year=year, feature_order=model.feature_names,
            country_iso=country_iso, offset_hours=off[s:e],
            hdd_base=model.hdd_base, cdd_base=model.cdd_base)
        pred = model.predict_log_density(X, base_margin=np.repeat(level[s:e], hpy))
        out[s:e] = np.power(10.0, pred).reshape(e - s, hpy) * np.asarray(areas[s:e])[:, None]
    return out


def _as_rings(polygon) -> list[list[tuple[float, float]]]:
    """Normalise a domain to a list of ``(lat, lng)`` rings.

    Accepts a single ring ``[(lat, lng), …]`` or a multi-ring (archipelago)
    ``[[(lat, lng), …], …]``. Detection is by nesting depth of the first
    element. Rings with < 3 vertices are dropped. This is the same contract as
    the fetcher's domain clip, so a country like Japan (Honshu, Hokkaido,
    Kyushu, …) clips every island and excludes neighbouring countries' cells."""
    if not polygon:
        return []
    first = polygon[0]
    is_multi = (
        isinstance(first, (list, tuple))
        and len(first) > 0
        and isinstance(first[0], (list, tuple))
    )
    rings = polygon if is_multi else [polygon]
    return [list(r) for r in rings if r and len(r) >= 3]


def _make_poly(polygon):
    """Shapely geometry (lng, lat) for a single-ring or multi-ring domain — a
    ``Polygon`` for one ring, a ``MultiPolygon`` for several. ``None`` if
    shapely is unavailable or the domain has no valid ring."""
    try:
        from shapely.geometry import MultiPolygon, Polygon
    except Exception:
        return None
    rings = _as_rings(polygon)
    if not rings:
        return None
    polys = [Polygon([(lng, lat) for (lat, lng) in r]) for r in rings]
    return polys[0] if len(polys) == 1 else MultiPolygon(polys)


# SSP GDP rasters: GDP{year}_ssp{N}.tif at 5-year steps, 2025–2100.
_SSP_GDP_STEP = 5
_SSP_GDP_MIN = 2025
_SSP_GDP_MAX = 2100


def _normalize_ssp(ssp) -> str:
    """Coerce an SSP spec ('ssp2', 'SSP245', 2, …) to a single digit '1'–'5'."""
    s = str(ssp).lower().replace("ssp", "")
    for ch in s:
        if ch in "12345":
            return ch
    return "2"


# ──────────────────────────────────────────────────────────────────────────────
# CMIP6 multi-year climate (Open-Meteo, per point) — the per-year temperature the
# density model expects, so the demand trajectory comes from real evolving
# climate (+ SSP GDP/pop) rather than a single repeated weather year.
# ──────────────────────────────────────────────────────────────────────────────

_CMIP6_MODEL = "CMCC_CM2_VHR4"

# The Open-Meteo climate API rate-limits aggressively (heavy CMIP6 queries); a
# bare request loses a node's future climate on the first HTTP 429, degrading
# its SSP demand forecast. Retry transient failures (429 / 5xx / timeout /
# connection) with exponential backoff + jitter, honouring a ``Retry-After``
# header when the server sends one.
_CMIP6_RETRIES = 6
_CMIP6_BACKOFF_S = 3.0
_CMIP6_BACKOFF_CAP_S = 90.0
# CMCC_CM2_VHR4 is a ~0.25° (~25 km) model, so nodes within a cell get
# physically identical climate. Snap node coordinates to this grid and fetch
# each unique cell once — collapses the hundreds of near-duplicate country-scale
# requests that trip the 429 limit down to a few dozen.
_CMIP6_GRID_DEG = 0.25
# Minimum spacing between *network* fetches (cache hits are free), to stay under
# the Open-Meteo per-minute rate limit instead of bursting into 429s.
_CMIP6_THROTTLE_S = 1.2


class _Cmip6DailyLimit(Exception):
    """Open-Meteo's *daily* request quota is exhausted (``"Daily API request
    limit exceeded. Please try again tomorrow."``). Unlike a per-minute/hour
    burst, this will not clear within the run, so we stop immediately and
    degrade instead of retrying every cell for hours against a dead quota."""


def _is_daily_limit(resp) -> bool:
    """True if a 429 response is the hard *daily* quota (not a burst limit)."""
    try:
        reason = (resp.json() or {}).get("reason", "")
    except Exception:
        reason = getattr(resp, "text", "") or ""
    reason = reason.lower()
    return "daily" in reason or "try again tomorrow" in reason


def _cmip6_get_with_retry(url: str):
    """GET *url* with retry/backoff on transient (429 / 5xx / network) errors.

    A *daily* quota 429 raises :class:`_Cmip6DailyLimit` immediately — retrying
    it would only stall the run (every subsequent cell fails the same way)."""
    import random
    import time

    import requests

    last_exc = None
    for attempt in range(_CMIP6_RETRIES):
        retry_after = None
        try:
            resp = requests.get(url, timeout=120)
            resp.raise_for_status()
            return resp
        except requests.HTTPError as exc:
            code = getattr(exc.response, "status_code", None)
            last_exc = exc
            if code != 429 and not (code and 500 <= code < 600):
                raise  # non-transient (e.g. 400) — don't retry
            if code == 429 and exc.response is not None:
                # Hard daily quota → abort now (don't burn retries on a limit
                # that only resets tomorrow).
                if _is_daily_limit(exc.response):
                    raise _Cmip6DailyLimit(
                        "Open-Meteo daily API quota exhausted") from exc
                # Burst (per-minute/hour) 429 → honour Retry-After if present.
                ra = getattr(exc.response, "headers", {}).get("Retry-After")
                if ra:
                    try:
                        retry_after = float(ra)
                    except ValueError:
                        retry_after = None
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
        if attempt < _CMIP6_RETRIES - 1:
            backoff = _CMIP6_BACKOFF_S * (2 ** attempt) * (0.5 + random.random())
            wait = retry_after if retry_after is not None else backoff
            time.sleep(min(wait, _CMIP6_BACKOFF_CAP_S))
    raise last_exc


def _cmip6_cache_path(lat, lon, start_year, end_year, model=_CMIP6_MODEL):
    """On-disk cache path for one point's CMIP6 timeseries. Keyed at 0.01°, so
    grid-snapped coordinates (see :data:`_CMIP6_GRID_DEG`) reuse the same file
    across every node that falls in the cell. (The ``cmip6_{model}_*.npz``
    files of earlier versions lost 31 December of a leap year instead of
    29 February and are not read.)"""
    from esfex.paths import CMIP6_DIR
    return (CMIP6_DIR
            / f"cmip6_{model}_{lat:.2f}_{lon:.2f}_{start_year}_{end_year}_8760.npz")


def _cmip6_point_hourly(
    lat: float, lon: float, start_year: int, end_year: int,
    model: str = _CMIP6_MODEL,
) -> dict:
    """Per-year hourly temperature (°C) at a point from the Open-Meteo CMIP6
    climate API, reconstructed from daily Tmax/Tmin. Cached to CMIP6_DIR.
    Returns {year: ndarray(8760)} (possibly empty on failure)."""
    from esfex.paths import CMIP6_DIR
    cache = _cmip6_cache_path(lat, lon, start_year, end_year, model)
    if cache.exists():
        try:
            d = np.load(cache)
            return {int(k): d[k] for k in d.files}
        except Exception:
            pass

    from esfex.models.demand_projection import _daily_to_hourly_temperature
    # An OPEN_METEO_API_KEY (paid/non-commercial subscription) routes to the
    # customer endpoint, which has far higher rate limits than the shared free
    # tier — the escape hatch when the free daily quota is a bottleneck.
    import os
    api_key = os.environ.get("OPEN_METEO_API_KEY", "").strip()
    host = ("customer-climate-api.open-meteo.com" if api_key
            else "climate-api.open-meteo.com")
    url = (
        f"https://{host}/v1/climate?"
        f"latitude={lat:.4f}&longitude={lon:.4f}"
        f"&start_date={start_year}-01-01&end_date={end_year}-12-31"
        f"&models={model}&daily=temperature_2m_max,temperature_2m_min"
    )
    if api_key:
        url += f"&apikey={api_key}"
    resp = _cmip6_get_with_retry(url)
    daily = resp.json().get("daily", {})
    times = daily.get("time", [])
    tmax = daily.get("temperature_2m_max", [])
    tmin = daily.get("temperature_2m_min", [])
    if not times:
        return {}

    year_days: dict[int, list[int]] = {}
    for i, t in enumerate(times):
        year_days.setdefault(int(t[:4]), []).append(i)
    for yr, idxs in year_days.items():         # 8760-h years: 29 February goes
        if len(idxs) == 366:
            year_days[yr] = [i for i in idxs if times[i][5:10] != "02-29"]

    result: dict[int, np.ndarray] = {}
    for yr in range(start_year, end_year + 1):
        idxs = year_days.get(yr, [])
        if len(idxs) < 360:
            continue
        tmn = np.array([tmin[i] if tmin[i] is not None else 15.0 for i in idxs])
        tmx = np.array([tmax[i] if tmax[i] is not None else 25.0 for i in idxs])
        hourly = _daily_to_hourly_temperature(tmn, tmx, len(idxs))
        if len(hourly) < 8760:
            hourly = np.pad(hourly, (0, 8760 - len(hourly)), mode="edge")
        hourly = hourly[:8760]
        m = np.isnan(hourly)
        if m.any():
            hourly[m] = np.nanmean(hourly[~m]) if (~m).any() else 20.0
        result[yr] = hourly.astype(np.float64)

    if result:
        try:
            CMIP6_DIR.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(cache, **{str(k): v for k, v in result.items()})
        except Exception:
            pass
    return result


def fetch_cmip6_node_temps(
    node_lats: list[float], node_lons: list[float],
    base_year: int, years: int, model: str = _CMIP6_MODEL,
    ssp=None,
) -> list[Optional[dict]]:
    """Per-node CMIP6 per-year hourly temperature for the simulation horizon.
    Returns a list (one per node) of {year: ndarray(8760)} or None on failure.

    When ``ssp`` is given (the pathway the user selected in Grid Builder step 3),
    the local Google Cloud Zarr backend is tried first: it is scenario-consistent
    (the climate matches the chosen SSP, not Open-Meteo's fixed implicit one) and
    isn't rate-limited. Open-Meteo is the fallback.

    Open-Meteo path: country-scale grids have hundreds of nodes but the CMIP6
    model is only ~0.25°; fetching per node hammered Open-Meteo into 429s. Nodes
    are snapped to the model grid, each unique cell is fetched exactly once (cache
    hits are free; only real network calls are throttled), and the result is
    shared back to every node in the cell."""
    import time

    start = base_year
    end = base_year + max(years - 1, 0)
    grid = _CMIP6_GRID_DEG

    # Scenario-consistent local backend first (honours the selected SSP).
    if ssp is not None:
        try:
            from esfex.models.cmip6_zarr import fetch_cmip6_zarr_node_temps
            zarr_out = fetch_cmip6_zarr_node_temps(
                node_lats, node_lons, base_year, years, ssp)
            if zarr_out is not None:
                got = sum(1 for c in zarr_out if c)
                logger.info(
                    "CMIP6: served %d/%d node(s) from the local Zarr store "
                    "(SSP-consistent, no Open-Meteo).", got, len(zarr_out))
                return zarr_out
        except Exception as exc:
            logger.warning(
                "CMIP6 Zarr backend unavailable (%s); falling back to "
                "Open-Meteo (single implicit scenario).", exc)

    # Map each node to its coarse model-grid cell.
    node_cells: list[tuple] = []
    unique_cells: dict[tuple, tuple[float, float]] = {}
    for la, lo in zip(node_lats, node_lons):
        clat = round(round(float(la) / grid) * grid, 4)
        clon = round(round(float(lo) / grid) * grid, 4)
        key = (clat, clon)
        node_cells.append(key)
        unique_cells.setdefault(key, (clat, clon))

    logger.info(
        "CMIP6: %d node(s) → %d unique %.2f° cell(s) to fetch",
        len(node_cells), len(unique_cells), grid,
    )

    by_cell: dict[tuple, Optional[dict]] = {}
    daily_exhausted = False
    for key, (clat, clon) in unique_cells.items():
        if daily_exhausted:
            by_cell[key] = None  # quota is dead for the day — don't hammer it
            continue
        cached = _cmip6_cache_path(clat, clon, start, end, model).exists()
        try:
            d = _cmip6_point_hourly(clat, clon, start, end, model)
            by_cell[key] = d or None
        except _Cmip6DailyLimit:
            # Stop immediately: every remaining cell would fail identically.
            # The forecast degrades to shared climate for the uncached cells;
            # the quota resets tomorrow (or set OPEN_METEO_API_KEY for a keyed
            # endpoint with higher limits).
            daily_exhausted = True
            by_cell[key] = None
            n_left = len(unique_cells) - len(by_cell)
            logger.warning(
                "CMIP6: Open-Meteo DAILY quota exhausted — stopping fetch; "
                "%d remaining cell(s) will use degraded (shared) climate. "
                "Retry tomorrow, or set OPEN_METEO_API_KEY. Already-cached "
                "cells are unaffected.", n_left,
            )
            continue
        except Exception as exc:
            logger.warning(
                "CMIP6 climate fetch failed @cell(%.2f,%.2f): %s",
                clat, clon, exc,
            )
            by_cell[key] = None
        # Throttle only genuine network fetches so a country-scale run stays
        # under the per-minute rate limit; cached cells cost nothing.
        if not cached and by_cell[key] is not None:
            time.sleep(_CMIP6_THROTTLE_S)

    return [by_cell.get(k) for k in node_cells]


def allocate_demand_capacitated(
    cell_lats, cell_lons, cell_demand,
    bus_lats, bus_lons, bus_cap,
    headroom: float = 1.25,
) -> np.ndarray:
    """Capacity-constrained allocation of cell demand to buses (substations).

    Solves the capacitated transportation problem — minimise the total
    feeder distance Σ x(c,b)·d(c,b) subject to every cell's demand being fully
    served (Σ_b x = D_c) and no bus serving beyond its capacity (Σ_c x ≤ cap_b).
    Unlike a Voronoi/nearest assignment this lets a large substation draw demand
    from farther away and spills demand to the next substation when the nearest
    one is full — i.e. a bus no longer carries only its immediate cell.

    Returns the per-bus served demand (same units as ``cell_demand``)."""
    # A single non-finite cell demand makes linprog reject the whole problem
    # (b_eq must not contain NaN) and the gravity fallback propagate NaN into
    # every bus, which then poisons the bus demand fractions. Treat bad/negative
    # cell demand as zero so allocation always degrades gracefully.
    cell_demand = np.asarray(cell_demand, dtype=np.float64).copy()
    cell_demand[~np.isfinite(cell_demand) | (cell_demand < 0)] = 0.0
    nb = len(bus_cap)
    nc = len(cell_demand)
    if nb == 0:
        return np.zeros(0)
    if nb == 1:
        return np.array([float(cell_demand.sum())])
    if nc == 0:
        return np.zeros(nb)

    cap = np.asarray(bus_cap, dtype=np.float64).copy()
    cap[~np.isfinite(cap) | (cap < 0)] = 0.0
    if cap.sum() <= 0:
        cap = np.ones(nb)
    Dtot = float(cell_demand.sum())
    # Substations have headroom; guarantee feasibility (total cap ≥ demand).
    if cap.sum() < headroom * Dtot and Dtot > 0:
        cap = cap * (headroom * Dtot / cap.sum())

    mean_lat = float(np.mean(cell_lats))
    mlng = 111.320 * math.cos(math.radians(mean_lat))
    cx = np.asarray(cell_lons) * mlng; cy = np.asarray(cell_lats) * 110.540
    bx = np.asarray(bus_lons) * mlng; by = np.asarray(bus_lats) * 110.540
    dist = np.sqrt((cx[:, None] - bx[None, :]) ** 2
                   + (cy[:, None] - by[None, :]) ** 2)        # (nc, nb) km

    try:
        from scipy.optimize import linprog
        from scipy.sparse import lil_matrix
        nv = nc * nb
        A_eq = lil_matrix((nc, nv))
        for c in range(nc):
            A_eq[c, c * nb:(c + 1) * nb] = 1.0
        A_ub = lil_matrix((nb, nv))
        for b in range(nb):
            A_ub[b, b::nb] = 1.0          # variables (c·nb + b) for all c
        res = linprog(
            dist.ravel(), A_ub=A_ub.tocsr(), b_ub=cap,
            A_eq=A_eq.tocsr(), b_eq=cell_demand,
            bounds=(0.0, None), method="highs",
        )
        if res.success:
            return np.asarray(res.x).reshape(nc, nb).sum(axis=0)
    except Exception as exc:  # pragma: no cover - solver/env dependent
        logger.warning("Capacitated transport failed (%s); capacity-weighted "
                       "fallback.", exc)

    # Fallback: capacity-weighted inverse-distance (gravity) share.
    w = cap[None, :] / np.maximum(dist, 1e-3) ** 2
    w = w / w.sum(axis=1, keepdims=True)
    return (cell_demand[:, None] * w).sum(axis=0)


def _ssp_gdp_total_nearest(lat: float, lon: float, year: int, ssp: str) -> Optional[float]:
    """Per-cell SSP GDP TOTAL (USD) for the NEAREST 5-year step (no interpolation,
    matching the pipeline's nearest-year stepping). Falls back to the historical
    GDP raster for pre-2025 years."""
    try:
        from esfex.models.pixel_features import _open_raster, _sample_point, sample_gdp_total
        from esfex.paths import GRIDDED_GDP_025_DIR as GDP_DIR
    except Exception:
        return None
    if year < _SSP_GDP_MIN:
        gt = sample_gdp_total(lat, lon, year)
        return gt if (gt and gt > 0) else None
    step = int(round(year / _SSP_GDP_STEP) * _SSP_GDP_STEP)
    step = min(max(step, _SSP_GDP_MIN), _SSP_GDP_MAX)
    sn = _normalize_ssp(ssp)
    path = GDP_DIR / f"GDP{step}_ssp{sn}.tif"
    from esfex.data_assets import ensure
    if not ensure(path):
        return None
    v = _sample_point(_open_raster(str(path)), lat, lon)
    return float(v) if (v is not None and v > 0) else None

