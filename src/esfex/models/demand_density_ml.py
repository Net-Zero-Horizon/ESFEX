"""Hourly demand-DENSITY model (XGBoost) for spatial per-node demand.

The model predicts ``log10(demand_mw / area_km2)`` per location and hour from
socio-economic density (population & GDP per km²), climate (temperature,
HDD/CDD), and calendar features (cyclical time, weekend, holidays). Multiplying
the predicted density by each node's area gives a genuinely spatially-resolved
hourly demand — replacing the old "national total split by ~uniform weights"
behaviour that collapsed to total/num_nodes.

Files (in ``MODELS_DIR``):
    demand_density_hourly_xgb.json        — the XGBoost booster
    demand_density_hourly_xgb.feats.json  — feature order, target, train ranges

Pipeline usage lives in
``esfex.visualization.workflows.demand_estimation_analysis.DemandProfileBuilder._build_density``.
"""

from __future__ import annotations

import datetime
import json
import logging
import math
from typing import Optional

import numpy as np

from esfex.paths import MODELS_DIR

logger = logging.getLogger(__name__)

_MODEL_FILENAME = "demand_density_hourly_xgb.json"
_FEATS_FILENAME = "demand_density_hourly_xgb.feats.json"

# Local equirectangular metric constants (match grid_mapping_builder).
_M_PER_DEG_LAT = 110_540.0
_M_PER_DEG_LNG_EQ = 111_320.0


# ──────────────────────────────────────────────────────────────────────────────
# Model wrapper
# ──────────────────────────────────────────────────────────────────────────────


class DemandDensityModel:
    """XGBoost hourly demand-density predictor (``log10(MW/km²)``)."""

    def __init__(self) -> None:
        self._model = None
        self.feature_names: list[str] = []
        self.target: str = ""
        self.train_ranges: dict[str, list[float]] = {}

    @classmethod
    def is_available(cls) -> bool:
        """True if the model file is present and xgboost importable."""
        if not (MODELS_DIR / _MODEL_FILENAME).exists():
            return False
        try:
            import xgboost  # noqa: F401
        except ImportError:
            return False
        return True

    @classmethod
    def load_bundled(cls) -> "DemandDensityModel":
        """Load the density model and its feature sidecar from MODELS_DIR."""
        model_path = MODELS_DIR / _MODEL_FILENAME
        feats_path = MODELS_DIR / _FEATS_FILENAME
        if not model_path.exists():
            raise FileNotFoundError(
                f"No demand-density model at {model_path}. "
                "Copy demand_density_hourly_xgb.json into MODELS_DIR."
            )
        try:
            import xgboost as xgb
        except ImportError as exc:  # pragma: no cover - env dependent
            raise ImportError(
                "xgboost is required for the demand-density model."
            ) from exc

        inst = cls()
        inst._model = xgb.Booster()
        inst._model.load_model(str(model_path))

        # Feature order: prefer the sidecar; fall back to the booster's own
        # embedded feature_names so we never mis-align columns.
        meta: dict = {}
        if feats_path.exists():
            meta = json.loads(feats_path.read_text())
        inst.feature_names = (
            list(meta.get("features", []))
            or list(inst._model.feature_names or [])
        )
        inst.target = meta.get("target", "log10(demand_mw/area_km2)")
        inst.train_ranges = meta.get("train_ranges", {}) or {}
        logger.info(
            "Loaded demand-density model from %s (%d features)",
            model_path, len(inst.feature_names),
        )
        return inst

    def predict_density_mw_per_km2(self, features: np.ndarray) -> np.ndarray:
        """Predict demand density (MW/km²) for a feature matrix.

        ``features`` must have columns in ``self.feature_names`` order.
        Returns a 1-D array of MW/km² (= ``10 ** model_output``).
        """
        import xgboost as xgb

        if self._model is None:
            raise RuntimeError("Model not loaded. Call load_bundled() first.")
        dm = xgb.DMatrix(features, feature_names=self.feature_names)
        log_density = np.asarray(self._model.predict(dm), dtype=np.float64)
        return np.power(10.0, log_density)


# ──────────────────────────────────────────────────────────────────────────────
# Feature construction
# ──────────────────────────────────────────────────────────────────────────────


def _cyclical(values: np.ndarray, period: float) -> tuple[np.ndarray, np.ndarray]:
    angle = 2.0 * math.pi * values / period
    return np.sin(angle), np.cos(angle)


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
    hdd_base: float = 18.0,
    cdd_base: float = 24.0,
    train_ranges: Optional[dict[str, list[float]]] = None,
) -> np.ndarray:
    """Build one base-year (8760-hour) density-feature matrix for a node.

    Socio-economic inputs are constant over the year; climate and calendar
    vary hourly. Columns are returned in ``feature_order`` so they line up with
    the booster regardless of how the feature list is declared. Inter-annual
    growth is handled by the caller via national-total calibration, so this
    builds the base year only.
    """
    hpy = 8760
    ranges = train_ranges or {}

    temp = (
        np.asarray(temperature_hourly[:hpy], dtype=np.float64)
        if len(temperature_hourly) >= hpy
        else np.full(hpy, 20.0, dtype=np.float64)
    )
    hdd = np.maximum(hdd_base - temp, 0.0)
    cdd = np.maximum(temp - cdd_base, 0.0)

    hours = np.arange(hpy)
    hour_of_day = (hours % 24).astype(float)
    day_of_year = hours // 24

    h_sin, h_cos = _cyclical(hour_of_day, 24.0)

    _month_days = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    month_arr = np.zeros(hpy, dtype=int)
    h = 0
    for m, md in enumerate(_month_days):
        month_arr[h:h + md * 24] = m
        h += md * 24
    m_sin, m_cos = _cyclical(month_arr.astype(float), 12.0)

    jan1 = datetime.date(base_year, 1, 1)
    dow = np.array(
        [(jan1 + datetime.timedelta(days=int(d))).weekday() for d in day_of_year]
    )
    d_sin, d_cos = _cyclical(dow.astype(float), 7.0)
    is_weekend = (dow >= 5).astype(float)

    # Holiday calendar (reuse the training-time helper).
    try:
        from esfex.models.demand_training import _holiday_features
        is_holiday, days_to_next, days_from_prev = _holiday_features(
            country_iso or "", base_year, hours=hpy,
        )
        is_holiday = np.asarray(is_holiday, dtype=np.float64)
        days_to_next = np.asarray(days_to_next, dtype=np.float64)
        days_from_prev = np.asarray(days_from_prev, dtype=np.float64)
    except Exception:
        is_holiday = np.zeros(hpy)
        days_to_next = np.full(hpy, 7.0)
        days_from_prev = np.full(hpy, 7.0)

    def _clip(name: str, value):
        rng = ranges.get(name)
        if rng and len(rng) == 2:
            return np.clip(value, rng[0], rng[1])
        return value

    columns: dict[str, np.ndarray] = {
        "log_pop_density": np.full(hpy, _clip("log_pop_density", log_pop_density)),
        "log_gdp_density": np.full(hpy, _clip("log_gdp_density", log_gdp_density)),
        "log_gdp_per_cap": np.full(hpy, _clip("log_gdp_per_cap", log_gdp_per_cap)),
        "temperature": _clip("temperature", temp),
        "hdd": hdd,
        "cdd": cdd,
        "hour_sin": h_sin,
        "hour_cos": h_cos,
        "dow_sin": d_sin,
        "dow_cos": d_cos,
        "month_sin": m_sin,
        "month_cos": m_cos,
        "is_weekend": is_weekend,
        "is_holiday": is_holiday,
        "days_to_next_holiday": days_to_next,
        "days_from_prev_holiday": days_from_prev,
        "latitude": np.full(hpy, latitude),
        "longitude": np.full(hpy, longitude),
    }

    missing = [c for c in feature_order if c not in columns]
    if missing:
        raise ValueError(f"Density feature(s) not built: {missing}")
    return np.column_stack([columns[name] for name in feature_order])


# ──────────────────────────────────────────────────────────────────────────────
# Per-CELL inference on the 0.25° grid (matches the training-time pipeline:
# predict a density per 0.25° cell, multiply by the cell's area, then sum cells).
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


def region_cells_025(
    polygon_latlon: Optional[list[tuple[float, float]]] = None,
    bounds: Optional[tuple[float, float, float, float]] = None,
    node_lats: Optional[list[float]] = None,
    node_lons: Optional[list[float]] = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Enumerate the 0.25° grid-cell centroids covering the study region.

    Centroids are aligned to the global 0.25° raster grid so they sample the
    GDP / population rasters at their own cells. Returns (lats, lons, areas_km2)
    for every cell whose centroid is inside the drawn polygon (or the bounding
    box, or a small pad around the nodes)."""
    if bounds is not None:
        s, w, n, e = bounds
    elif node_lats and node_lons:
        s, n = min(node_lats), max(node_lats)
        w, e = min(node_lons), max(node_lons)
        s -= _CELL_DEG; n += _CELL_DEG; w -= _CELL_DEG; e += _CELL_DEG
    elif polygon_latlon:
        las = [p[0] for p in polygon_latlon]; los = [p[1] for p in polygon_latlon]
        s, n, w, e = min(las), max(las), min(los), max(los)
    else:
        return np.array([]), np.array([]), np.array([])

    # Global-grid-aligned centroids: lat = -90 + (j+0.5)*0.25, lon similarly.
    j0 = int(math.floor((s + 90.0) / _CELL_DEG))
    j1 = int(math.ceil((n + 90.0) / _CELL_DEG))
    k0 = int(math.floor((w + 180.0) / _CELL_DEG))
    k1 = int(math.ceil((e + 180.0) / _CELL_DEG))
    lat_c = np.array([-90.0 + (j + 0.5) * _CELL_DEG for j in range(j0, j1)])
    lon_c = np.array([-180.0 + (k + 0.5) * _CELL_DEG for k in range(k0, k1)])
    LA, LO = np.meshgrid(lat_c, lon_c, indexing="ij")
    lats = LA.ravel(); lons = LO.ravel()

    poly = _make_poly(list(polygon_latlon)) if (polygon_latlon and len(polygon_latlon) >= 3) else None
    if poly is not None:
        from shapely.geometry import Point
        keep = np.array([poly.contains(Point(lo, la)) for la, lo in zip(lats, lons)])
        lats, lons = lats[keep], lons[keep]

    return lats, lons, cell_area_km2(lats)


def predict_region_cell_demand(
    model: "DemandDensityModel",
    lats: np.ndarray,
    lons: np.ndarray,
    areas: np.ndarray,
    log_pop_density: np.ndarray,
    log_gdp_density: np.ndarray,
    log_gdp_per_cap: np.ndarray,
    temperature_hourly: np.ndarray,
    year: int,
    country_iso: str = "",
    hdd_base: float = 18.0,
    cdd_base: float = 24.0,
    chunk: int = 256,
) -> np.ndarray:
    """Per-cell hourly demand (MW), shape (n_cells, 8760).

    For every 0.25° cell: predict the demand density (MW/km²) from the cell's
    own socio features + the (shared) hourly climate/calendar, then multiply by
    the cell area. This integrates the non-linear density correctly per cell —
    the authoritative use of the model — instead of averaging."""
    import xgboost as xgb

    hpy = 8760
    n = len(lats)
    if n == 0:
        return np.zeros((0, hpy), dtype=np.float64)
    ranges = model.train_ranges or {}
    feature_order = model.feature_names

    # ── Shared hourly block (identical for every cell) ──────────────────
    temp = (np.asarray(temperature_hourly[:hpy], dtype=np.float64)
            if len(temperature_hourly) >= hpy else np.full(hpy, 20.0))
    hdd = np.maximum(hdd_base - temp, 0.0)
    cdd = np.maximum(temp - cdd_base, 0.0)
    hours = np.arange(hpy)
    h_sin, h_cos = _cyclical((hours % 24).astype(float), 24.0)
    _md = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    month_arr = np.zeros(hpy, dtype=int); _h = 0
    for _m, _d in enumerate(_md):
        month_arr[_h:_h + _d * 24] = _m; _h += _d * 24
    m_sin, m_cos = _cyclical(month_arr.astype(float), 12.0)
    day_of_year = hours // 24
    jan1 = datetime.date(year, 1, 1)
    dow = np.array([(jan1 + datetime.timedelta(days=int(d))).weekday() for d in day_of_year])
    d_sin, d_cos = _cyclical(dow.astype(float), 7.0)
    is_weekend = (dow >= 5).astype(float)
    try:
        from esfex.models.demand_training import _holiday_features
        is_hol, dtn, dfp = _holiday_features(country_iso or "", year, hours=hpy)
        is_hol = np.asarray(is_hol, float); dtn = np.asarray(dtn, float); dfp = np.asarray(dfp, float)
    except Exception:
        is_hol = np.zeros(hpy); dtn = np.full(hpy, 7.0); dfp = np.full(hpy, 7.0)

    def _clip(name, val):
        rng = ranges.get(name)
        return np.clip(val, rng[0], rng[1]) if (rng and len(rng) == 2) else val

    shared = {
        "temperature": _clip("temperature", temp), "hdd": hdd, "cdd": cdd,
        "hour_sin": h_sin, "hour_cos": h_cos, "dow_sin": d_sin, "dow_cos": d_cos,
        "month_sin": m_sin, "month_cos": m_cos, "is_weekend": is_weekend,
        "is_holiday": is_hol, "days_to_next_holiday": dtn, "days_from_prev_holiday": dfp,
    }
    lpd = _clip("log_pop_density", log_pop_density)
    lgd = _clip("log_gdp_density", log_gdp_density)
    lgpc = _clip("log_gdp_per_cap", log_gdp_per_cap)

    out = np.zeros((n, hpy), dtype=np.float64)
    for s in range(0, n, chunk):
        e = min(s + chunk, n)
        m = e - s
        cols = []
        for name in feature_order:
            if name in shared:
                cols.append(np.tile(shared[name], m))
            elif name == "log_pop_density":
                cols.append(np.repeat(lpd[s:e], hpy))
            elif name == "log_gdp_density":
                cols.append(np.repeat(lgd[s:e], hpy))
            elif name == "log_gdp_per_cap":
                cols.append(np.repeat(lgpc[s:e], hpy))
            elif name == "latitude":
                cols.append(np.repeat(lats[s:e], hpy))
            elif name == "longitude":
                cols.append(np.repeat(lons[s:e], hpy))
            else:
                raise ValueError(f"Unknown density feature {name}")
        X = np.column_stack(cols)
        pred = model._model.predict(xgb.DMatrix(X, feature_names=feature_order))
        dens = np.power(10.0, np.asarray(pred, dtype=np.float64)).reshape(m, hpy)
        out[s:e] = dens * areas[s:e, None]
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Per-node area (Voronoi partition by nearest-node sampling)
# ──────────────────────────────────────────────────────────────────────────────


def _polygon_area_km2(coords_latlon: list[tuple[float, float]], mean_lat: float) -> float:
    """Shoelace area (km²) of a lat/lng ring via local equirectangular metric."""
    if len(coords_latlon) < 3:
        return 0.0
    mlng = _M_PER_DEG_LNG_EQ * math.cos(math.radians(mean_lat))
    xs = [lng * mlng for (_lat, lng) in coords_latlon]
    ys = [lat * _M_PER_DEG_LAT for (lat, _lng) in coords_latlon]
    a = 0.0
    n = len(coords_latlon)
    for i in range(n):
        j = (i + 1) % n
        a += xs[i] * ys[j] - xs[j] * ys[i]
    return abs(a) / 2.0 / 1e6


def _region_ring(
    node_lats: list[float],
    node_lons: list[float],
    polygon_latlon: Optional[list[tuple[float, float]]],
    bounds: Optional[tuple[float, float, float, float]],
) -> tuple[list[tuple[float, float]], float, float]:
    """Resolve the study region to a ring; return (ring, mean_lat, area_km2).

    Prefers the drawn polygon, then the bounding box, then a padded node
    bounding box (so a single/colinear node set still has area)."""
    if polygon_latlon and len(polygon_latlon) >= 3:
        ring = list(polygon_latlon)
    elif bounds:
        s, w, no, e = bounds
        ring = [(s, w), (s, e), (no, e), (no, w)]
    else:
        lat_min, lat_max = min(node_lats), max(node_lats)
        lon_min, lon_max = min(node_lons), max(node_lons)
        pad_lat = max((lat_max - lat_min) * 0.5, 0.25)
        pad_lon = max((lon_max - lon_min) * 0.5, 0.25)
        ring = [
            (lat_min - pad_lat, lon_min - pad_lon),
            (lat_min - pad_lat, lon_max + pad_lon),
            (lat_max + pad_lat, lon_max + pad_lon),
            (lat_max + pad_lat, lon_min - pad_lon),
        ]
    mean_lat = sum(lat for lat, _ in ring) / len(ring)
    return ring, mean_lat, _polygon_area_km2(ring, mean_lat)


def _make_poly(ring: list[tuple[float, float]]):
    try:
        from shapely.geometry import Polygon
        return Polygon([(lng, lat) for (lat, lng) in ring])
    except Exception:
        return None


def _nearest_node_cells(ring, node_lats, node_lons, mean_lat, grid):
    """Yield (node_index, lat, lon) for each interior grid point of *ring*,
    assigning the point to its nearest node (the Voronoi partition)."""
    poly = _make_poly(ring)
    point_cls = None
    if poly is not None:
        from shapely.geometry import Point as point_cls  # noqa: N813

    lat_min = min(lat for lat, _ in ring)
    lat_max = max(lat for lat, _ in ring)
    lon_min = min(lng for _, lng in ring)
    lon_max = max(lng for _, lng in ring)

    mlng = _M_PER_DEG_LNG_EQ * math.cos(math.radians(mean_lat))
    nx = np.asarray(node_lons, dtype=np.float64) * mlng
    ny = np.asarray(node_lats, dtype=np.float64) * _M_PER_DEG_LAT

    for la in np.linspace(lat_min, lat_max, grid):
        py = la * _M_PER_DEG_LAT
        for lo in np.linspace(lon_min, lon_max, grid):
            if poly is not None and not poly.contains(point_cls(lo, la)):
                continue
            px = lo * mlng
            d2 = (nx - px) ** 2 + (ny - py) ** 2
            yield int(np.argmin(d2)), la, lo


def compute_node_areas_km2(
    node_lats: list[float],
    node_lons: list[float],
    polygon_latlon: Optional[list[tuple[float, float]]] = None,
    bounds: Optional[tuple[float, float, float, float]] = None,
    grid: int = 200,
) -> list[float]:
    """Per-node area (km²) as the Voronoi partition of the study region.

    Each sampled cell of the study region is assigned to its nearest node; a
    node's area is its share of the total region area. Robust for any node
    count (including 1)."""
    n = len(node_lats)
    if n == 0:
        return []
    ring, mean_lat, total_area = _region_ring(
        node_lats, node_lons, polygon_latlon, bounds)
    if total_area <= 0:
        return [0.0] * n
    if n == 1:
        return [total_area]

    counts = np.zeros(n, dtype=np.int64)
    for ni, _la, _lo in _nearest_node_cells(
            ring, node_lats, node_lons, mean_lat, grid):
        counts[ni] += 1

    if counts.sum() == 0:
        return [total_area / n] * n
    counts = np.maximum(counts, 1)   # no node vanishes
    shares = counts / counts.sum()
    return [float(total_area * s) for s in shares]


# GDP pixel area (the 0.25° GDP raster), matching pixel_features.
def _gdp_pixel_km2(lat: float) -> float:
    return (0.25 ** 2) * math.cos(math.radians(lat)) * 111.0 ** 2


def _sample_node_raster(
    node_lats: list[float],
    node_lons: list[float],
    sample_fn,
    polygon_latlon: Optional[list[tuple[float, float]]],
    bounds: Optional[tuple[float, float, float, float]],
    grid: int,
) -> list[Optional[float]]:
    """Cell-averaged value of ``sample_fn(lat, lon)`` per node, over each
    node's Voronoi cell. Points where ``sample_fn`` returns None/≤0 are skipped;
    a node with no valid samples yields None."""
    n = len(node_lats)
    if n == 0:
        return []
    ring, mean_lat, total_area = _region_ring(
        node_lats, node_lons, polygon_latlon, bounds)
    if total_area <= 0:
        return [None] * n
    acc = np.zeros(n, dtype=np.float64)
    cnt = np.zeros(n, dtype=np.int64)
    for ni, la, lo in _nearest_node_cells(ring, node_lats, node_lons, mean_lat, grid):
        v = sample_fn(la, lo)
        if v is None or v <= 0:
            continue
        acc[ni] += v
        cnt[ni] += 1
    return [float(acc[i] / cnt[i]) if cnt[i] else None for i in range(n)]


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


def gdp_density_point(lat: float, lon: float, year: int, ssp: str = "ssp2") -> Optional[float]:
    """GDP density (USD/km²) at a point for a given year and SSP scenario.

    For future years uses the IIASA SSP GDP rasters (``GDP{Y}_ssp{N}.tif``,
    5-year steps) with linear interpolation between steps. For years before the
    SSP range, falls back to the historical GDP raster (``GDP{Y}.tif``)."""
    try:
        from esfex.models.pixel_features import (
            _open_raster, _sample_point, sample_gdp_total,
        )
        from esfex.paths import GRIDDED_GDP_025_DIR as GDP_DIR
    except Exception:
        return None

    sn = _normalize_ssp(ssp)

    def _at_step(step_year: int) -> Optional[float]:
        path = GDP_DIR / f"GDP{step_year}_ssp{sn}.tif"
        if not path.exists():
            return None
        v = _sample_point(_open_raster(str(path)), lat, lon)
        if v is None or v <= 0:
            return None
        return v / _gdp_pixel_km2(lat)

    if year < _SSP_GDP_MIN:
        gt = sample_gdp_total(lat, lon, year)
        return (gt / _gdp_pixel_km2(lat)) if (gt and gt > 0) else None
    if year >= _SSP_GDP_MAX:
        return _at_step(_SSP_GDP_MAX)

    lo_year = (year // _SSP_GDP_STEP) * _SSP_GDP_STEP
    hi_year = lo_year + _SSP_GDP_STEP
    v_lo, v_hi = _at_step(lo_year), _at_step(hi_year)
    if v_lo is None:
        return v_hi
    if v_hi is None:
        return v_lo
    f = (year - lo_year) / float(_SSP_GDP_STEP)
    return v_lo * (1.0 - f) + v_hi * f


# ──────────────────────────────────────────────────────────────────────────────
# CMIP6 multi-year climate (Open-Meteo, per point) — the per-year temperature the
# density model expects, so the demand trajectory comes from real evolving
# climate (+ SSP GDP/pop) rather than a single repeated weather year.
# ──────────────────────────────────────────────────────────────────────────────

_CMIP6_MODEL = "CMCC_CM2_VHR4"


def _cmip6_point_hourly(
    lat: float, lon: float, start_year: int, end_year: int,
    model: str = _CMIP6_MODEL,
) -> dict:
    """Per-year hourly temperature (°C) at a point from the Open-Meteo CMIP6
    climate API, reconstructed from daily Tmax/Tmin. Cached to CMIP6_DIR.
    Returns {year: ndarray(8760)} (possibly empty on failure)."""
    from esfex.paths import CMIP6_DIR
    cache = CMIP6_DIR / f"cmip6_{model}_{lat:.2f}_{lon:.2f}_{start_year}_{end_year}.npz"
    if cache.exists():
        try:
            d = np.load(cache)
            return {int(k): d[k] for k in d.files}
        except Exception:
            pass

    import requests
    from esfex.models.demand_projection import _daily_to_hourly_temperature
    url = (
        f"https://climate-api.open-meteo.com/v1/climate?"
        f"latitude={lat:.4f}&longitude={lon:.4f}"
        f"&start_date={start_year}-01-01&end_date={end_year}-12-31"
        f"&models={model}&daily=temperature_2m_max,temperature_2m_min"
    )
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    daily = resp.json().get("daily", {})
    times = daily.get("time", [])
    tmax = daily.get("temperature_2m_max", [])
    tmin = daily.get("temperature_2m_min", [])
    if not times:
        return {}

    year_days: dict[int, list[int]] = {}
    for i, t in enumerate(times):
        year_days.setdefault(int(t[:4]), []).append(i)

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
) -> list[Optional[dict]]:
    """Per-node CMIP6 per-year hourly temperature for the simulation horizon.
    Returns a list (one per node) of {year: ndarray(8760)} or None on failure."""
    start = base_year
    end = base_year + max(years - 1, 0)
    out: list[Optional[dict]] = []
    for la, lo in zip(node_lats, node_lons):
        try:
            d = _cmip6_point_hourly(float(la), float(lo), start, end, model)
            out.append(d or None)
        except Exception as exc:
            logger.warning("CMIP6 climate fetch failed @(%.2f,%.2f): %s", la, lo, exc)
            out.append(None)
    return out


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
    if not path.exists():
        return None
    v = _sample_point(_open_raster(str(path)), lat, lon)
    return float(v) if (v is not None and v > 0) else None


def sample_cells_socio(
    lats: np.ndarray,
    lons: np.ndarray,
    areas: np.ndarray,
    year: int,
    ssp: str,
    pop_year: int = 2020,
):
    """Per-0.25°-cell socio inputs for the density model.

    Returns (log_pop_density, log_gdp_density, log_gdp_per_cap, keep_mask):
    population density from GPW (people/km²) and GDP density from the SSP GDP
    raster total ÷ cell area (USD/km²). Cells with neither population nor GDP
    are flagged ``keep_mask=False`` (no economic activity → no demand)."""
    n = len(lats)
    try:
        from esfex.models.pixel_features import sample_pop_density
    except Exception:
        sample_pop_density = None

    pd = np.full(n, np.nan); gd = np.full(n, np.nan)
    for i in range(n):
        la = float(lats[i]); lo = float(lons[i]); ar = max(float(areas[i]), 1e-6)
        p = sample_pop_density(la, lo, pop_year) if sample_pop_density else None
        pd[i] = p if (p is not None and p > 0) else 0.0
        gt = _ssp_gdp_total_nearest(la, lo, year, ssp)
        gd[i] = (gt / ar) if (gt and gt > 0) else 0.0

    keep = (pd > 0) | (gd > 0)
    # Per-capita = GDP density / pop density (GDP/pop on a per-cell basis).
    with np.errstate(divide="ignore", invalid="ignore"):
        gpc = np.where(pd > 0, gd / pd, gd)
    log_pd = np.log10(np.maximum(pd, 1e-6))
    log_gd = np.log10(np.maximum(gd, 1e-3))
    log_gpc = np.log10(np.maximum(gpc, 1e-3))
    return log_pd, log_gd, log_gpc, keep


def sample_node_pop_density(
    node_lats: list[float],
    node_lons: list[float],
    year: int,
    polygon_latlon: Optional[list[tuple[float, float]]] = None,
    bounds: Optional[tuple[float, float, float, float]] = None,
    grid: int = 90,
) -> list[Optional[float]]:
    """Cell-averaged GPW population density (people/km²) per node."""
    try:
        from esfex.models.pixel_features import sample_pop_density
    except Exception:
        return [None] * len(node_lats)
    return _sample_node_raster(
        node_lats, node_lons,
        lambda la, lo: sample_pop_density(la, lo, year),
        polygon_latlon, bounds, grid,
    )


def sample_node_pop_and_area(
    node_lats: list[float],
    node_lons: list[float],
    year: int,
    polygon_latlon: Optional[list[tuple[float, float]]] = None,
    bounds: Optional[tuple[float, float, float, float]] = None,
    grid: int = 120,
) -> list[tuple[Optional[float], float]]:
    """Per node: (mean GPW population density [people/km²], POPULATED area
    [km²]).

    The populated area is each node's share of the region area restricted to
    grid cells that actually carry population — so ocean and empty land in a
    Voronoi cell do NOT inflate it. This is the correct ``area_km2`` for the
    demand-density model (whose target was demand per km² of inhabited land),
    and it stops sparse island/coastal nodes from being over-forecast."""
    n = len(node_lats)
    if n == 0:
        return []
    try:
        from esfex.models.pixel_features import sample_pop_density
    except Exception:
        return [(None, 0.0)] * n

    ring, mean_lat, total_area = _region_ring(
        node_lats, node_lons, polygon_latlon, bounds)
    if total_area <= 0:
        return [(None, 0.0)] * n

    sum_pop = np.zeros(n, dtype=np.float64)
    cnt_pop = np.zeros(n, dtype=np.int64)
    total_inside = 0
    for ni, la, lo in _nearest_node_cells(ring, node_lats, node_lons, mean_lat, grid):
        total_inside += 1
        pd = sample_pop_density(la, lo, year)
        if pd is not None and pd > 0:
            sum_pop[ni] += pd
            cnt_pop[ni] += 1

    out: list[tuple[Optional[float], float]] = []
    for ni in range(n):
        if cnt_pop[ni] == 0 or total_inside == 0:
            out.append((None, 0.0))
            continue
        pop_density = sum_pop[ni] / cnt_pop[ni]
        pop_area = total_area * (cnt_pop[ni] / total_inside)
        out.append((float(pop_density), float(pop_area)))
    return out


def sample_node_gdp_density(
    node_lats: list[float],
    node_lons: list[float],
    year: int,
    ssp: str = "ssp2",
    polygon_latlon: Optional[list[tuple[float, float]]] = None,
    bounds: Optional[tuple[float, float, float, float]] = None,
    grid: int = 90,
) -> list[Optional[float]]:
    """Cell-averaged SSP GDP density (USD/km²) per node for *year*."""
    return _sample_node_raster(
        node_lats, node_lons,
        lambda la, lo: gdp_density_point(la, lo, year, ssp),
        polygon_latlon, bounds, grid,
    )


def sample_node_densities(
    node_lats: list[float],
    node_lons: list[float],
    year: int,
    polygon_latlon: Optional[list[tuple[float, float]]] = None,
    bounds: Optional[tuple[float, float, float, float]] = None,
    grid: int = 90,
    ssp: str = "ssp2",
) -> list[Optional[dict]]:
    """Cell-averaged socio density per node for one (year, ssp).

    Returns, per node, ``{"pop_density", "gdp_density", "gdp_per_cap"}`` (or
    None when the cell yielded no population sample). Convenience wrapper over
    the population and GDP samplers."""
    pop = sample_node_pop_density(node_lats, node_lons, year, polygon_latlon, bounds, grid)
    gdp = sample_node_gdp_density(node_lats, node_lons, year, ssp, polygon_latlon, bounds, grid)
    out: list[Optional[dict]] = []
    for ni in range(len(node_lats)):
        pd = pop[ni] if ni < len(pop) else None
        if pd is None or pd <= 0:
            out.append(None)
            continue
        gd = gdp[ni] if ni < len(gdp) else None
        gpc = (gd / pd) if (gd and pd > 0) else None
        out.append({
            "pop_density": float(pd),
            "gdp_density": (float(gd) if gd else None),
            "gdp_per_cap": (float(gpc) if gpc else None),
        })
    return out
