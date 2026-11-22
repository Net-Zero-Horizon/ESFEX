"""Models shipped with ESFEX and datasets downloaded on first use.

The demand forecast used to read its trained models and rasters only from
ESFEX_DATA_ROOT: on an installation without the author's dataset it fell
back, silently, to a linear trend with no spatial cells. The models now ship
in the package and the rasters are fetched from the data release.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from esfex import data_assets, paths


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    """A local release mirror with two assets, one with a wrong checksum."""
    src = tmp_path / "mirror"
    src.mkdir()
    good, bad = b"raster bytes", b"tampered"
    (src / "GDP2030_ssp2.tif").write_bytes(good)
    (src / "GDP2035_ssp2.tif").write_bytes(bad)
    man = {"tag": "test", "assets": {
        "GDP2030_ssp2.tif": {"size": len(good), "sha256": hashlib.sha256(good).hexdigest()},
        "GDP2035_ssp2.tif": {"size": len(bad), "sha256": "0" * 64},
    }}
    monkeypatch.setattr(data_assets, "manifest", lambda: man)
    monkeypatch.setenv("ESFEX_ASSETS_URL", src.as_uri())
    data_assets.clear_failures()
    yield tmp_path / "cache"
    data_assets.clear_failures()


def test_missing_asset_is_downloaded_and_checked(mirror):
    target = mirror / "GDP2030_ssp2.tif"
    assert data_assets.ensure(target)
    assert target.read_bytes() == b"raster bytes"
    assert not list(mirror.glob("*.part"))


def test_checksum_mismatch_is_refused_and_reported(mirror):
    target = mirror / "GDP2035_ssp2.tif"
    assert not data_assets.ensure(target)
    assert not target.exists()
    assert "GDP2035_ssp2.tif" in data_assets.failures()


def test_unpublished_or_present_files_need_no_download(mirror):
    assert not data_assets.ensure(mirror / "not_published.tif")
    assert data_assets.failures() == {}
    own = mirror / "GDP2035_ssp2.tif"            # a user's own copy wins
    own.parent.mkdir(parents=True)
    own.write_bytes(b"mine")
    assert data_assets.ensure(own)


@pytest.mark.parametrize("lat, lon, name", [
    (22.0, -80.0, "gpw_v4_popdens_2020_n30w090.tif"),      # Cuba
    (35.7, 139.7, "gpw_v4_popdens_2020_n60e120.tif"),      # Tokyo
    (-33.9, 18.4, "gpw_v4_popdens_2020_s30e000.tif"),      # Cape Town
    (90.0, -180.0, "gpw_v4_popdens_2020_n90w180.tif"),     # corners stay in range
    (-90.0, 180.0, "gpw_v4_popdens_2020_s60e150.tif"),
])
def test_gpw_tile_names(lat, lon, name):
    assert data_assets.gpw_tile_name(2020, lat, lon) == name


def test_population_reads_a_tile_without_the_global_file(tmp_path, monkeypatch):
    from esfex.models import pixel_features as pf
    monkeypatch.setattr(pf, "POP_DENSITY_HIST_DIR", tmp_path)
    tile = tmp_path / "tiles" / "gpw_v4_popdens_2020_n30w090.tif"
    seen = []
    monkeypatch.setattr(data_assets, "ensure", lambda p, name=None: seen.append(p) or True)
    assert pf._gpw_path(2020, 22.0, -80.0) == tile
    assert seen == [tile]
    full = tmp_path / "gpw_v4_population_density_rev11_2020_30_sec_2020.tif"
    full.write_bytes(b"")
    assert pf._gpw_path(2020, 22.0, -80.0) == full    # a local dataset wins


def test_bundled_manifest_lists_population_tiles_and_gdp():
    data_assets.manifest.cache_clear()
    man = json.loads(data_assets._MANIFEST_PATH.read_text())
    names = man["assets"]
    assert man["tag"]
    assert "gpw_v4_popdens_2020_n30w090.tif" in names
    assert "GDP2030_ssp2.tif" in names
    assert all(len(m["sha256"]) == 64 and m["size"] > 0 for m in names.values())


def test_models_ship_with_the_package(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "MODELS_DIR", tmp_path)        # no local dataset
    for name, bundled in [("demand_density_v2.json", "demand_density_v2.ubj"),
                          ("demand_density_v2.level.ubj", None),
                          ("demand_density_v2.meta.json", None),
                          ("demand_density_v2.grids.npz", None),
                          ("demand_density_v2.countries.csv", None),
                          ("demand_model.xgb", "demand_model.ubj")]:
        p = paths.model_path(name, bundled)
        assert p.parent == paths.BUNDLED_MODELS_DIR and p.exists()
    retrained = tmp_path / "demand_model.xgb"                  # a retrained one wins
    retrained.write_bytes(b"")
    assert paths.model_path("demand_model.xgb", "demand_model.ubj") == retrained


def test_bundled_density_model_loads(tmp_path, monkeypatch):
    pytest.importorskip("xgboost")
    monkeypatch.setattr(paths, "MODELS_DIR", tmp_path)
    from esfex.models.demand_density_ml import DemandDensityModel
    assert DemandDensityModel.is_available()
    m = DemandDensityModel.load_bundled()
    assert len(m.feature_names) == 26 and len(m.level_features) == 9
    assert m.context is not None


def test_density_model_copy_replaces_the_shipped_one_only_whole(tmp_path, monkeypatch):
    import shutil

    from esfex.models import demand_density_ml as ddm
    monkeypatch.setattr(paths, "MODELS_DIR", tmp_path)
    shutil.copy(paths.BUNDLED_MODELS_DIR / "demand_density_v2.ubj",
                tmp_path / "demand_density_v2.json")
    # without its level round and meta file the copy is ignored
    assert ddm._model_files()[0].parent == paths.BUNDLED_MODELS_DIR
    for f in ("demand_density_v2.level.ubj", "demand_density_v2.meta.json"):
        shutil.copy(paths.BUNDLED_MODELS_DIR / f, tmp_path / f)
    assert [p.parent for p in ddm._model_files()] == [tmp_path] * 3
