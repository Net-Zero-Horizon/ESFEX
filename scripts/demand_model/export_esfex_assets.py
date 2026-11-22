#!/usr/bin/env python
"""Turn the article's fitted demand model into the files ESFEX ships.

Reads (never writes) the work root of the article's revision pipeline
(``R1_WORK_ROOT``, see ``article/README`` in this folder) and writes into
``--out`` (normally ``src/esfex/assets/models``):

  demand_density_v2.ubj            hourly round of the two-round booster (UBJ)
  demand_density_v2.level.ubj      level round (UBJ); its prediction is the base
                                   margin of the hourly round
  demand_density_v2.meta.json      features, training ranges, provenance and the
                                   article's cross-validation scores
  demand_density_v2.grids.npz      0.25 degree grids: present-day climate normals
                                   (t_mean, t_range), electricity-use context
                                   (hh_elec_share, bld_share_elec), land area
                                   (land_km2) and population in 2020 (pop_2020)
  demand_density_v2.countries.csv  per country: national electricity-use context
                                   and the standard UTC offsets of its time zones

The boosters are converted from JSON to UBJ; the script checks that both give
identical predictions. Usage::

    python scripts/demand_model/export_esfex_assets.py \\
        --work "/media/.../Demand_forecasting/r1_revision" \\
        --out src/esfex/assets/models
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

SPEC = "level_geo_two_shallow"
STEM = "demand_density_v2"
ARTICLE = ("The asymmetric burden of climate change revealed by sub-national mapping "
           "of electricity-demand sensitivity and inequity (Nature Communications, revision R1)")
# The article grid: NEX-GDDP-CMIP6 0.25 degree, 600 x 1440, lat -59.875.., lon -179.875..
LAT0, LON0, RES, NLAT, NLON = -59.875, -179.875, 0.25, 600, 1440
# s03c_dataset.CONTEXT_FROM and s03a_series.TZ_ISO2 / HOLIDAY_ISO2
CONTEXT_FROM = {"XKX": "KOS", "ALA": "FIN"}
TZ_ISO2 = {"XKX": "RS", "ALA": "AX", "GLP": "GP", "GUF": "GF", "PRI": "PR", "FRO": "FO",
           "TWN": "TW"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def stamp(path: Path) -> str:
    return dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(
        timespec="seconds")


def export_boosters(models: Path, out: Path) -> dict:
    import xgboost as xgb

    src = {"hourly": models / f"{SPEC}.json", "level": models / f"{SPEC}.level.json"}
    dst = {"hourly": out / f"{STEM}.ubj", "level": out / f"{STEM}.level.ubj"}
    rng = np.random.default_rng(0)
    for k in src:
        b = xgb.Booster()
        b.load_model(str(src[k]))
        b.set_param({"device": "cpu"})
        b.save_model(str(dst[k]))
        c = xgb.Booster()
        c.load_model(str(dst[k]))
        X = rng.normal(size=(50_000, len(b.feature_names))).astype(np.float32)
        d = xgb.DMatrix(X, feature_names=b.feature_names,
                        base_margin=rng.normal(size=len(X)).astype(np.float32))
        if not np.array_equal(b.predict(d), c.predict(d)):
            raise SystemExit(f"{dst[k].name}: UBJ predictions differ from {src[k].name}")
    return {k: {"file": str(v), "modified": stamp(v), "sha256": sha256(v)}
            for k, v in src.items()}


def export_grids(work: Path, out: Path) -> dict:
    eta = work / "eta" / "present.npz"
    ctx = work / "grids" / "energy_context_025.npz"
    cty = work / "grids" / "countries_025.npz"
    pop = work / "grids" / "population_2020_025.npz"
    p = np.load(eta)
    c = np.load(ctx)
    land = np.load(cty, allow_pickle=True)["land_km2"]
    # load_population() with POP_KEY = pop_worldpop: the article's present-day population
    people = np.load(pop)["pop_worldpop"]
    lat, lon = p["lat"], p["lon"]
    assert lat.shape == (NLAT,) and lon.shape == (NLON,)
    assert abs(lat[0] - LAT0) < 1e-6 and abs(lon[0] - LON0) < 1e-6
    np.savez_compressed(
        out / f"{STEM}.grids.npz",
        lat0=np.float64(LAT0), lon0=np.float64(LON0), res=np.float64(RES),
        t_mean=p["t_mean"].astype(np.float32), t_range=p["t_range"].astype(np.float32),
        hh_elec_share=c["hh_elec_share"].astype(np.float32),
        bld_share_elec=c["bld_share_elec"].astype(np.float32),
        land_km2=land.astype(np.float32), pop_2020=people.astype(np.float32))
    return {name: {"file": str(f), "modified": stamp(f)}
            for name, f in (("normals", eta), ("energy_context", ctx), ("land", cty),
                            ("population", pop))}


def std_offsets(iso2: str) -> list[float]:
    """Standard offset of every zone of a country, in pytz order (s03a std_offset)."""
    import pytz

    out = []
    for z in pytz.country_timezones.get(iso2, []):
        out.append(min(pytz.timezone(z).utcoffset(dt.datetime(2021, m, 15)).total_seconds()
                       / 3600 for m in (1, 7)))
    return out


def export_countries(work: Path, out: Path) -> dict:
    import pycountry

    ctx_csv = work / "grids" / "energy_context.csv"
    ctx = pd.read_csv(ctx_csv).set_index("adm0_a3")
    cc = np.load(work / "grids" / "countries_025.npz", allow_pickle=True)
    ne_of_iso = {}                       # ISO 3166 alpha-3 -> Natural Earth adm0_a3
    for a3, eh in zip(cc["adm0_a3"], cc["iso_a3_eh"]):
        if str(eh) not in ("-99", "") and str(eh) not in ne_of_iso:
            ne_of_iso[str(eh)] = str(a3)
    rows = []
    codes = {c.alpha_3: c.alpha_2 for c in pycountry.countries}
    codes.setdefault("XKX", "XK")                      # Kosovo (user-assigned code)
    for iso3, iso2 in sorted(codes.items()):
        src = CONTEXT_FROM.get(iso3) or ne_of_iso.get(iso3, iso3)
        hh = bld = np.nan
        if src in ctx.index:
            hh, bld = ctx.loc[src, "hh_elec_share"], ctx.loc[src, "bld_share_elec"]
        offsets = std_offsets(TZ_ISO2.get(iso3, iso2))
        rows.append({"iso3": iso3, "iso2": iso2, "context_from": src,
                     "hh_elec_share": hh, "bld_share_elec": bld,
                     "std_offsets": ";".join(f"{o:g}" for o in offsets)})
    pd.DataFrame(rows).to_csv(out / f"{STEM}.countries.csv", index=False)
    return {"file": str(ctx_csv), "modified": stamp(ctx_csv)}


def cv_scores(work: Path) -> dict:
    """The article's cross-validation (s04) for the shipped model and the submitted design."""
    path = work / "cv" / "summary.csv"
    s = pd.read_csv(path)
    keep = ["r2_hourly", "rmse_dex", "r2_level", "level_err_dex", "r2_shape", "fe_r"]
    out = {"file": str(path), "modified": stamp(path), "columns": {
        "r2_hourly": "R2 of hourly log10 demand density on held-out data",
        "rmse_dex": "RMSE of hourly log10 demand density (dex)",
        "r2_level": "R2 of the mean log10 density of each held-out series",
        "level_err_dex": "mean error of that level (dex)",
        "r2_shape": "R2 of the hourly anomalies about each series' mean",
        "fe_r": "correlation of the model's and the observed sensitivity to +1 C "
                "(fixed-effects estimate on held-out predictions)"}}
    for spec, label in ((SPEC, "shipped"), ("level_geo_d", "submitted_design")):
        rows = s[s.spec == spec].set_index("design")
        out[label] = {d: {k: float(rows.loc[d, k]) for k in keep if pd.notna(rows.loc[d, k])}
                      for d in rows.index}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--work", type=Path, required=True, help="R1_WORK_ROOT (read only)")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    models = a.work / "models"
    a.out.mkdir(parents=True, exist_ok=True)
    meta_src = json.loads((models / f"{SPEC}.meta.json").read_text())
    boosters = export_boosters(models, a.out)
    grids = export_grids(a.work, a.out)
    countries = export_countries(a.work, a.out)
    fitted = dt.datetime.fromtimestamp((models / f"{SPEC}.json").stat().st_mtime)
    meta = {
        "name": STEM,
        "target": "log10(demand_mw / area_km2)",
        "features": meta_src["features"],
        "level_features": meta_src["level_features"],
        "clock": "UTC: hour_sin/cos and month are the UTC hour and month of the row, "
                 "temperature is that instant's; dow and holidays are on the local date "
                 "at the standard offset of the country's time zone nearest to the "
                 "longitude; lhour_sin/cos is local solar time (longitude/15 h), "
                 "smonth_sin/cos the month shifted by six in the southern hemisphere",
        "hdd_base": 18.0, "cdd_base": 24.0,
        "static_range": meta_src["static_range"],
        "temperature_range": meta_src["temperature_range"],
        "rounds": meta_src["rounds"], "params": meta_src["params"],
        "level_params": meta_src["level_params"],
        "n_hours": meta_src["n_hours"], "n_series": meta_src["n_series"],
        "n_series_years": meta_src["n_series_years"], "n_countries": meta_src["n_countries"],
        "gain_share": meta_src["gain_share"],
        "provenance": {
            "article": ARTICLE, "spec": SPEC,
            "fitted": fitted.astimezone().isoformat(timespec="seconds"),
            "fitted_on": "GPU (device=cuda); ESFEX predicts on the CPU",
            "boosters": boosters, "grids": grids, "countries": countries,
            "normals": "t_mean, t_range: mean and 5-95 % spread of daily mean "
                       "temperature of each cell, NEX-GDDP-CMIP6 (GFDL-ESM4, "
                       "IPSL-CM6A-LR, MPI-ESM1-2-HR) x SSP1-2.6/2-4.5/3-7.0/5-8.5, "
                       "2025-2029 pooled (s08_sensitivity.normals)",
            "energy_context": "UN Energy Balances 2015-2021 by country; Statistics "
                              "Canada 25-10-0029 and EIA SEDS by province and state "
                              "(s12_energy_context)",
            "land_km2": "land area of each cell: Natural Earth 10m admin-0 rasterized "
                        "at 0.025 degree (s02_static_grids)",
            "pop_2020": "WorldPop 2020 1 km population counts summed per cell "
                        "(s01_population_grid, pop_worldpop)",
        },
        "cross_validation": cv_scores(a.work),
    }
    # source files are recorded relative to the article's work root
    text = json.dumps(meta, indent=1).replace(str(a.work), "$R1_WORK_ROOT")
    (a.out / f"{STEM}.meta.json").write_text(text + "\n")
    for f in sorted(a.out.glob(f"{STEM}*")):
        print(f"{f.name:34s} {f.stat().st_size:>11,d} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
