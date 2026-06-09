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


def sample_node_densities(
    node_lats: list[float],
    node_lons: list[float],
    year: int,
    polygon_latlon: Optional[list[tuple[float, float]]] = None,
    bounds: Optional[tuple[float, float, float, float]] = None,
    grid: int = 90,
) -> list[Optional[dict]]:
    """Cell-averaged socio-economic density per node from gridded rasters.

    Samples GPW population density and the 0.25° GDP raster over each node's
    Voronoi cell (the same nearest-node partition used for areas) and returns,
    per node, ``{"pop_density": people/km², "gdp_density": USD/km²,
    "gdp_per_cap": USD/person}`` — or ``None`` for a node whose cell yielded no
    valid samples (e.g. all-ocean, or rasters unavailable). The caller falls
    back to population-derived densities for those.
    """
    n = len(node_lats)
    if n == 0:
        return []
    try:
        from esfex.models.pixel_features import (
            sample_pop_density,
            sample_gdp_total,
        )
    except Exception:
        return [None] * n

    ring, mean_lat, total_area = _region_ring(
        node_lats, node_lons, polygon_latlon, bounds)
    if total_area <= 0:
        return [None] * n

    sum_pop = np.zeros(n, dtype=np.float64)
    sum_gdpd = np.zeros(n, dtype=np.float64)
    cnt_pop = np.zeros(n, dtype=np.int64)
    cnt_gdp = np.zeros(n, dtype=np.int64)

    for ni, la, lo in _nearest_node_cells(
            ring, node_lats, node_lons, mean_lat, grid):
        pd = sample_pop_density(la, lo, year)
        if pd is None or pd <= 0:
            continue
        sum_pop[ni] += pd
        cnt_pop[ni] += 1
        gt = sample_gdp_total(la, lo, year)
        if gt is not None and gt > 0:
            sum_gdpd[ni] += gt / _gdp_pixel_km2(la)
            cnt_gdp[ni] += 1

    out: list[Optional[dict]] = []
    for ni in range(n):
        if cnt_pop[ni] == 0:
            out.append(None)
            continue
        pop_density = sum_pop[ni] / cnt_pop[ni]
        gdp_density = (sum_gdpd[ni] / cnt_gdp[ni]) if cnt_gdp[ni] else None
        gdp_per_cap = (gdp_density / pop_density) if (gdp_density and pop_density > 0) else None
        out.append({
            "pop_density": float(pop_density),
            "gdp_density": (float(gdp_density) if gdp_density else None),
            "gdp_per_cap": (float(gdp_per_cap) if gdp_per_cap else None),
        })
    return out
