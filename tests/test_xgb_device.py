"""Device selection for XGBoost demand inference (GPU when available, else CPU)."""

from __future__ import annotations

import numpy as np
import pytest

xgb = pytest.importorskip("xgboost")

from esfex.models import xgb_device


@pytest.fixture
def tiny_booster():
    """A trivial CPU-trained regression booster + its feature names."""
    rng = np.random.default_rng(0)
    X = rng.random((64, 3)).astype(np.float32)
    y = X @ np.array([1.0, -2.0, 0.5], dtype=np.float32)
    names = ["a", "b", "c"]
    dm = xgb.DMatrix(X, label=y, feature_names=names)
    booster = xgb.train({"tree_method": "hist", "verbosity": 0}, dm,
                        num_boost_round=5)
    return booster, names, X, y


# ── resolve_device ──────────────────────────────────────────────────────────


def test_env_cpu_forces_cpu(monkeypatch):
    monkeypatch.setenv("ESFEX_XGB_DEVICE", "cpu")
    assert xgb_device.resolve_device(10_000_000) == "cpu"


def test_env_auto_small_batch_stays_cpu(monkeypatch):
    monkeypatch.setenv("ESFEX_XGB_DEVICE", "auto")
    # Under the row threshold the transfer never pays off, regardless of a GPU.
    assert xgb_device.resolve_device(100) == "cpu"


def test_env_unknown_treated_as_auto(monkeypatch):
    monkeypatch.setenv("ESFEX_XGB_DEVICE", "banana")
    assert xgb_device.resolve_device(1) == "cpu"  # tiny batch → cpu either way


def test_env_auto_large_batch_matches_availability(monkeypatch):
    monkeypatch.setenv("ESFEX_XGB_DEVICE", "auto")
    expected = "cuda" if xgb_device.cuda_available() else "cpu"
    assert xgb_device.resolve_device(10_000_000) == expected


def test_env_cuda_falls_back_to_cpu_without_gpu(monkeypatch):
    monkeypatch.setenv("ESFEX_XGB_DEVICE", "cuda")
    monkeypatch.setattr(xgb_device, "cuda_available", lambda: False)
    assert xgb_device.resolve_device(1) == "cpu"


# ── predict ─────────────────────────────────────────────────────────────────


def test_predict_cpu_matches_plain_dmatrix(tiny_booster, monkeypatch):
    booster, names, X, _y = tiny_booster
    monkeypatch.setenv("ESFEX_XGB_DEVICE", "cpu")

    ref = booster.predict(xgb.DMatrix(X, feature_names=names))
    got = xgb_device.predict(booster, X, feature_names=names)
    assert got.shape == ref.shape
    np.testing.assert_allclose(got, ref, rtol=1e-5, atol=1e-5)


def test_predict_gpu_falls_back_on_error(tiny_booster, monkeypatch):
    booster, names, X, _y = tiny_booster
    # Force the GPU branch, but make the GPU call raise → must fall back to CPU.
    monkeypatch.setenv("ESFEX_XGB_DEVICE", "cuda")
    monkeypatch.setattr(xgb_device, "cuda_available", lambda: True)
    monkeypatch.setattr(
        booster, "inplace_predict",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no GPU")))

    ref = booster.predict(xgb.DMatrix(X, feature_names=names))
    got = xgb_device.predict(booster, X, feature_names=names)
    np.testing.assert_allclose(got, ref, rtol=1e-5, atol=1e-5)


@pytest.mark.skipif(
    not xgb_device.cuda_available(), reason="no usable CUDA GPU")
def test_predict_gpu_matches_cpu(tiny_booster, monkeypatch):
    booster, names, X, _y = tiny_booster
    monkeypatch.setenv("ESFEX_XGB_DEVICE", "cpu")
    cpu = xgb_device.predict(booster, X, feature_names=names)
    monkeypatch.setenv("ESFEX_XGB_DEVICE", "cuda")
    gpu = xgb_device.predict(booster, X, feature_names=names)
    # Same model, same inputs → numerically equivalent within float tolerance.
    np.testing.assert_allclose(gpu, cpu, rtol=1e-4, atol=1e-4)
