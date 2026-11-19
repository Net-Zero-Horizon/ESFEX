"""Demand estimation ML engine — unified hourly prediction.

Supports pluggable backends (XGBoost / TFT) with a single interface.
Both models predict hourly shape factors directly — no 3h aggregation
or Fourier reconstruction needed.

The model predicts dimensionless shape factors: demand_hour / annual_avg_MW.
Multiply by the annual average demand (MW) to get absolute MW values.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Optional

import numpy as np

# The feature schema of the training dataset, the one the model is fitted on.
from esfex.models.demand_training import (
    COUNTRY_MAP_FILENAME, FEATURE_COLS, hourly_feature_columns)
from esfex.paths import model_path

logger = logging.getLogger(__name__)

_XGB_FILENAME = "demand_model.xgb"
_XGB_BUNDLED = "demand_model.ubj"          # the shipped copy (same booster)

# TFT is intentionally disabled for ESFEX demand estimation: the use case is
# FORWARD per-node hourly generation over a multi-year horizon with no observed
# demand, for which the direct XGBoost shape predictor (all inputs known for any
# future hour) is the right tool. The TFT code path is kept intact and can be
# re-enabled by flipping this flag.
_TFT_ENABLED = False

# FEATURE_COLS: the 29-feature non-AR schema of the bundled demand_model.ubj
# (84 countries). shape_lag_* are excluded: forward per-node generation has no
# demand history to seed autoregressive lags.

# The static features (GDP per capita, population density, urbanization) are
# sampled from rasters whose last historical year is this one; a later study
# year samples them here (the training rows of later years had no raster
# value either and took their country's median of the earlier years).
STATIC_RASTER_LAST_YEAR = 2020

# Temperature used when a node has no temperature series, as the training
# dataset does for a series without one (pixel_features).
_MISSING_TEMPERATURE_C = 20.0

_warned_no_smod: list[bool] = []    # the missing-SMOD warning, once a session


# ── Feature construction (hourly) ───────────────────────────────────────────

def load_country_map() -> dict[str, int]:
    """ISO3 → ``country_id`` encoding of the model ``load_bundled`` loads: the
    ``demand_xgb_country_map.json`` saved next to it (``train_xgboost`` writes
    one next to every model; the shipped model has its own)."""
    path = model_path(_XGB_FILENAME, _XGB_BUNDLED).parent / COUNTRY_MAP_FILENAME
    try:
        with open(path) as f:
            return {str(k): int(v) for k, v in json.load(f).items()}
    except (OSError, ValueError) as exc:
        raise FileNotFoundError(
            f"No country encoding for the demand model at {path} ({exc}). "
            "Retrain the model with 'esfex train-demand-model', which saves "
            "it, or pass country_id explicitly.") from exc


def country_id_for(iso3: str, country_map: Optional[dict] = None) -> float:
    """The model's ``country_id`` for *iso3*. A country the model was not
    trained on gets ``len(map)``, an id no training row had (as the ECVI
    backend does), with a warning: the model then has no country effect to
    apply, only its other features."""
    cmap = load_country_map() if country_map is None else country_map
    code = (iso3 or "").upper()
    if code in cmap:
        return float(cmap[code])
    logger.warning(
        "Demand model: country %r is not one of the %d countries it was "
        "trained on; using an unseen country id.", iso3, len(cmap))
    return float(len(cmap))


def static_features_at(latitude: float, longitude: float,
                       year: int) -> dict[str, float]:
    """``log_gdp_per_cap``, ``log_pop_density`` and ``urbanization`` at a
    point, sampled as the training dataset samples them
    (``pixel_features.static_features_for_point``), in the rasters of *year*
    or, after them, of ``STATIC_RASTER_LAST_YEAR``.

    Raises ValueError when the GDP or population raster has no value there
    (open sea, a raster that cannot be downloaded): the training rows filled
    such gaps with their country's median, which inference cannot know.
    Without the GHSL SMOD raster, urbanization is 10 (rural), as in training.
    """
    from esfex.models.pixel_features import static_features_for_point

    yr = min(int(year), STATIC_RASTER_LAST_YEAR)
    log_gdp, log_pop_density, urbanization, missing = \
        static_features_for_point(latitude, longitude, yr)
    bad = [name for name, v in (("log_gdp_per_cap", log_gdp),
                                ("log_pop_density", log_pop_density))
           if not np.isfinite(v)]
    if bad:
        raise ValueError(
            f"Demand model: {', '.join(bad)} cannot be sampled at "
            f"({latitude:.3f}, {longitude:.3f}) for {yr} "
            f"(missing: {', '.join(missing)}); pass static_features.")
    if "urbanization" in missing and not _warned_no_smod:
        _warned_no_smod.append(True)
        logger.warning(
            "Demand model: no GHSL SMOD raster (first at %.3f, %.3f); "
            "urbanization = 10 (rural), the value training used without it.",
            latitude, longitude)
    return {"log_gdp_per_cap": float(log_gdp),
            "log_pop_density": float(log_pop_density),
            "urbanization": float(urbanization)}


def build_hourly_features(
    iso3: str,
    temperature_hourly: Optional[np.ndarray],
    latitude: float,
    longitude: float,
    base_year: int,
    simulation_years: int = 1,
    *,
    static_features: Optional[dict[str, float]] = None,
    country_id: Optional[float] = None,
    gdp_growth_by_year: Optional[dict[int, float]] = None,
    pop_growth_by_year: Optional[dict[int, float]] = None,
) -> np.ndarray:
    """Build the model's feature matrix at hourly resolution for inference.

    Every column of ``FEATURE_COLS`` is filled by name with the value the
    training dataset computes for it (``demand_training.hourly_feature_columns``):
    calendar and holidays of *iso3* on each row's real date, degree hours on
    the training bases (18/24 °C), the daily temperature statistics, the
    node's coordinates and the ``country_id`` of the model's country map.

    Parameters
    ----------
    iso3 : str
        Country of the node (holidays and ``country_id``).
    temperature_hourly : ndarray (8760,) or None
        Base-year hourly temperature (°C) on the node's local civil time,
        reused for every simulation year. None: 20 °C, the value the training
        dataset uses for a series without temperature (logged).
    static_features : dict, optional
        ``log_gdp_per_cap`` (ln $/person), ``log_pop_density`` (ln people/km²)
        and ``urbanization`` (GHSL SMOD class, 10-30) of the base year.
        Default: sampled at the node by :func:`static_features_at`.
    country_id : float, optional
        Default: :func:`country_id_for` of *iso3*.
    gdp_growth_by_year, pop_growth_by_year : dict, optional
        Yearly growth rates; each simulation year after the first moves
        ``log_gdp_per_cap`` by ln(1 + GDP growth) and ``log_pop_density`` by
        ln(1 + population growth).

    Returns
    -------
    ndarray (simulation_years * 8760, len(FEATURE_COLS))
    """
    hpy = 8760
    if temperature_hourly is None:
        logger.warning("Demand model: no temperature series at (%.3f, %.3f); "
                       "using %.0f °C.", latitude, longitude,
                       _MISSING_TEMPERATURE_C)
        temp = np.full(hpy, _MISSING_TEMPERATURE_C)
    else:
        temp = np.asarray(temperature_hourly, dtype=np.float64)
        if len(temp) < hpy:
            raise ValueError(
                f"temperature_hourly has {len(temp)} hours; the demand model "
                f"needs a year of {hpy}.")
        temp = temp[:hpy]

    statics = dict(static_features if static_features is not None
                   else static_features_at(latitude, longitude, base_year))
    if country_id is None:
        country_id = country_id_for(iso3)
    gdp_growth = gdp_growth_by_year or {}
    pop_growth = pop_growth_by_year or {}

    X = np.empty((simulation_years * hpy, len(FEATURE_COLS)), dtype=np.float64)
    for y in range(simulation_years):
        yr = base_year + y
        if y > 0:
            statics["log_gdp_per_cap"] += math.log1p(gdp_growth.get(yr, 0.0))
            statics["log_pop_density"] += math.log1p(pop_growth.get(yr, 0.0))
        cols = hourly_feature_columns(
            iso3, yr, latitude, longitude, temp,
            log_gdp_per_cap=statics["log_gdp_per_cap"],
            log_pop_density=statics["log_pop_density"],
            urbanization=statics["urbanization"])
        cols["country_id"] = country_id
        sl = slice(y * hpy, (y + 1) * hpy)
        for j, name in enumerate(FEATURE_COLS):
            X[sl, j] = cols[name]

    return X


# ── Unified model class ─────────────────────────────────────────────────────

class DemandMLModel:
    """Unified demand model with pluggable backends (XGBoost / TFT).

    Predicts hourly shape factors (demand / annual_avg_MW, mean ≈ 1.0).

    Engines:
      - 'auto': TFT if available, else XGBoost
      - 'tft': Temporal Fusion Transformer (deep learning)
      - 'xgboost': Gradient boosted trees
    """

    def __init__(self, engine: str = "xgboost"):
        self._model = None
        self._engine = engine

    @property
    def engine(self) -> str:
        return self._engine

    @classmethod
    def is_available(cls, engine: str = "auto") -> bool:
        if not _TFT_ENABLED and engine == "tft":
            engine = "xgboost"  # TFT disabled → resolve to XGBoost
        if _TFT_ENABLED and engine in ("auto", "tft"):
            try:
                from esfex.models.demand_tft import DemandTFTModel
                if DemandTFTModel.is_available():
                    return True
            except ImportError:
                pass
        if engine in ("auto", "xgboost"):
            if model_path(_XGB_FILENAME, _XGB_BUNDLED).exists():
                return True
        return False

    @classmethod
    def load_bundled(cls, engine: str = "auto") -> "DemandMLModel":
        """Load the bundled model. With TFT disabled (default), resolves to
        XGBoost; TFT is loaded only when ``_TFT_ENABLED`` is True."""
        if not _TFT_ENABLED and engine == "tft":
            engine = "xgboost"  # TFT disabled → resolve any tft request to XGBoost
        if _TFT_ENABLED and engine in ("auto", "tft"):
            try:
                from esfex.models.demand_tft import DemandTFTModel
                if DemandTFTModel.is_available():
                    tft = DemandTFTModel.load_bundled()
                    instance = cls(engine="tft")
                    instance._model = tft
                    logger.info("Loaded TFT demand model")
                    return instance
            except (ImportError, Exception) as exc:
                if engine == "tft":
                    raise
                logger.debug("TFT not available: %s", exc)

        if engine in ("auto", "xgboost"):
            mpath = model_path(_XGB_FILENAME, _XGB_BUNDLED)
            if not mpath.exists():
                raise FileNotFoundError(
                    f"No trained model at {mpath}. "
                    "Run 'esfex train-demand-model' to train one."
                )
            try:
                import xgboost as xgb
            except ImportError:
                raise ImportError(
                    "xgboost required. It ships with esfex; reinstall with: "
                    "pip install --upgrade --force-reinstall esfex"
                )
            instance = cls(engine="xgboost")
            instance._model = xgb.Booster()
            instance._model.load_model(str(mpath))
            logger.info("Loaded XGBoost demand model from %s", mpath)
            return instance

        raise FileNotFoundError(f"No model available for engine '{engine}'.")

    def predict(self, features: np.ndarray) -> np.ndarray:
        """Predict hourly demand shape factors.

        Parameters
        ----------
        features : ndarray (n_hours, len(FEATURE_COLS))
            Feature matrix from build_hourly_features().

        Returns
        -------
        ndarray (n_hours,)
            Shape factors (mean ≈ 1.0).
        """
        predictions = self.predict_raw(features)
        m = predictions.mean()
        if m > 0:
            predictions /= m
        return predictions

    def predict_raw(self, features: np.ndarray) -> np.ndarray:
        """Predict shape factors WITHOUT normalizing to mean=1.

        The raw mean encodes climate sensitivity: warmer inputs
        produce mean > 1.0, enabling climate-adjusted demand scaling.
        """
        if self._model is None:
            raise RuntimeError("Model not loaded.")

        if self._engine == "xgboost":
            from esfex.models.xgb_device import predict as _xgb_predict
            predictions = _xgb_predict(
                self._model, features, feature_names=FEATURE_COLS)
        elif self._engine == "tft":
            predictions = self._model.predict_hourly_features(features)
        else:
            raise ValueError(f"Unknown engine: {self._engine}")

        return np.maximum(predictions, 0.0)

    def save(self, path: Path) -> None:
        if self._model is None:
            raise ValueError("No model to save.")
        if self._engine == "xgboost":
            path.parent.mkdir(parents=True, exist_ok=True)
            self._model.save_model(str(path))
        elif self._engine == "tft":
            self._model.save(path)

    def set_booster(self, booster) -> None:
        """Set XGBoost booster (used during training)."""
        self._model = booster
        self._engine = "xgboost"
