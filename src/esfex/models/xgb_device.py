"""Device selection for XGBoost inference — use the GPU when it pays off.

XGBoost booster prediction runs on the CPU by default. For the large batches the
demand pipeline produces (per-node ``years × 8760`` hours, or per-cell density
chunks of millions of rows) a CUDA GPU is markedly faster, but for small batches
the host→device transfer outweighs the speedup. This module centralises the
choice so every inference site behaves identically:

* GPU support is probed **once** (cached): the build must be compiled with CUDA
  *and* a usable device must be present at runtime.
* ``resolve_device(n_rows)`` honours the ``ESFEX_XGB_DEVICE`` environment
  variable (``auto`` | ``cpu`` | ``cuda``). ``auto`` (the default) uses the GPU
  only when it exists and the batch clears ``_GPU_MIN_ROWS``; ``cuda`` forces it
  regardless of size; ``cpu`` disables it.
* ``predict()`` runs the booster on the chosen device and **falls back to CPU**
  if anything GPU-related raises, so a misconfigured GPU never breaks a run.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache

import numpy as np

logger = logging.getLogger(__name__)

# Below this row count the host→device transfer dominates, so 'auto' stays on
# the CPU. A single simulation year (8760 h) is intentionally under it; multi-
# year per-node batches and density chunks clear it comfortably.
_GPU_MIN_ROWS = 20_000


@lru_cache(maxsize=1)
def cuda_available() -> bool:
    """True if this XGBoost build can actually run inference on a CUDA device.

    Probed once and cached. Requires both a CUDA-enabled build (``build_info``)
    and a usable device at runtime (verified with a throwaway train+predict).
    """
    try:
        import xgboost as xgb
    except Exception:  # pragma: no cover - xgboost always ships with esfex
        return False
    try:
        if not xgb.build_info().get("USE_CUDA", False):
            return False
    except Exception:
        return False  # older xgboost without build_info → assume CPU-only
    try:
        # The only version-stable proof of a usable GPU is to touch one.
        booster = xgb.train(
            {"device": "cuda", "tree_method": "hist", "verbosity": 0},
            xgb.DMatrix(np.zeros((2, 1), dtype=np.float32),
                        label=np.zeros(2, dtype=np.float32)),
            num_boost_round=1,
        )
        booster.inplace_predict(np.zeros((1, 1), dtype=np.float32))
    except Exception as exc:
        logger.info(
            "XGBoost is CUDA-enabled but no usable GPU device was found "
            "(%s) — demand inference will run on the CPU.", exc)
        return False
    logger.info("XGBoost GPU inference available (CUDA device detected).")
    return True


def resolve_device(n_rows: int | None = None) -> str:
    """Return ``"cuda"`` or ``"cpu"`` for a batch of ``n_rows`` rows.

    Honours ``ESFEX_XGB_DEVICE`` (``auto`` | ``cpu`` | ``cuda``); unknown values
    are treated as ``auto``.
    """
    pref = os.environ.get("ESFEX_XGB_DEVICE", "auto").strip().lower()
    if pref == "cpu":
        return "cpu"
    if pref == "cuda":
        return "cuda" if cuda_available() else "cpu"
    # auto: GPU only when present and the batch is big enough to amortise it.
    if n_rows is not None and n_rows < _GPU_MIN_ROWS:
        return "cpu"
    return "cuda" if cuda_available() else "cpu"


def predict(booster, features, *, feature_names=None, n_rows=None) -> np.ndarray:
    """Run ``booster`` inference on the GPU when worthwhile, else the CPU.

    Parameters
    ----------
    booster
        A loaded ``xgboost.Booster``.
    features
        2-D feature matrix (rows = samples). Cast to ``float32`` (XGBoost's
        internal dtype) so CPU and GPU paths see identical inputs.
    feature_names
        Column names for the CPU ``DMatrix`` path (the GPU ``inplace_predict``
        path takes the raw array; the caller is responsible for column order).
    n_rows
        Override the row count used for the size threshold (defaults to
        ``features.shape[0]``).

    Returns
    -------
    np.ndarray
        The booster's predictions (1-D for these single-output models).
    """
    import xgboost as xgb

    arr = np.ascontiguousarray(features, dtype=np.float32)
    rows = arr.shape[0] if n_rows is None else n_rows
    device = resolve_device(rows)

    if device == "cuda":
        try:
            booster.set_param({"device": "cuda"})
            return np.asarray(booster.inplace_predict(arr))
        except Exception as exc:
            logger.warning(
                "XGBoost GPU inference failed (%s) — falling back to CPU.", exc)
            try:
                booster.set_param({"device": "cpu"})
            except Exception:
                pass
    else:
        try:
            booster.set_param({"device": "cpu"})
        except Exception:
            pass

    dm = xgb.DMatrix(arr, feature_names=feature_names)
    return np.asarray(booster.predict(dm))
