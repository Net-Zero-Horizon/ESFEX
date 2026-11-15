#!/usr/bin/env python
# Provenance: copied from
# /home/manuel/Dropbox/PhD/Pappers/09_Flexibility/ReFlex/demand_estimation/build_density_demand_dataset.py
# (modified 2026-06-09 14:17:31 +0900, md5 60dfc51ee5564ec8fecab72b2c096878), the
# script behind the shipped demand_density_hourly_xgb model (v1). Changes: git log.
"""OPTION B — pixel-transferable HOURLY demand-DENSITY training dataset.

Target (INTENSIVE):  log10(demand_mw / area_km2)  per hour  -> MW/km².
Features (all INTENSIVE -> same range at country and pixel scale):
  log_pop_density   = log10(P / A)        [persons / km²]
  log_gdp_density   = log10(G / A)        [USD / km²]  (industry / economic activity)
  log_gdp_per_cap   = log10(G / P)        [USD / person] (income -> AC penetration)
  urbanization      (GHSL %)
  temperature, hdd, cdd                    (hourly climate, from the parquets)
  calendar          (hour/dow/month sin/cos, is_weekend, is_holiday, days_to/from)
  latitude, longitude
NO demand as input.

Reuses:
  - build_absolute_demand_dataset's hourly frame (ecvi/absolute_demand_dataset.parquet:
    demand_mw + temperature/hdd/cdd + calendar, per (country,zone) series).
  - build_demand_density_model's masking machinery to get per-series
    area_km2, total pop P, total gdp G, urbanization (admin-0 masks + admin-1 zonal;
    gridded GPW pop / GDP raster / GHSL).

Saves ecvi/density_demand_dataset.parquet and a per-series static table
ecvi/density_series_static.parquet (for inference range checks).
"""
from __future__ import annotations
import sys, json, glob
from pathlib import Path
import numpy as np, pandas as pd

HERE = Path("/home/manuel/Dropbox/PhD/Pappers/09_Flexibility/ReFlex/demand_estimation")
DF = Path("/media/manuel/New Volume/Demand_forecasting")
sys.path.insert(0, str(HERE))
POP_NPZ = HERE / "ecvi" / "ecvi_ssp2_2025_2050_tft_84c_fold0.npz"
FEAT_NPZ = HERE / "ecvi" / "feature_grids_025.npz"
ABS = HERE / "ecvi" / "absolute_demand_dataset.parquet"
OUT = HERE / "ecvi" / "density_demand_dataset.parquet"
STATIC = HERE / "ecvi" / "density_series_static.parquet"
DS = DF / "data/demand_dataset"
# zonal countries whose demand series are sub-national (admin-1 footprints)
ZONAL = {"CHN", "AUS", "CAN", "BRA", "THA", "TWN", "USA", "IND", "JPN",
         "ITA", "NOR", "SWE", "DNK"}


def build_series_static():
    """Per (iso3, zone) series: area_km2, total pop P, total gdp G, urb, lat, lon."""
    import generate_sensitivity_figures as g
    import cartopy.io.shapereader as shp
    from shapely.geometry import Point
    from shapely.prepared import prep

    d = np.load(POP_NPZ)
    lat, lon, pop = d["lat"], d["lon"], d["exposure"]
    fg = np.load(FEAT_NPZ); gdp, urb = fg["gdp"], fg["urb"]
    R, dd = 6371.0, np.deg2rad(0.25)
    area = np.abs(R * R * dd * (np.sin(np.deg2rad(lat + 0.125)) - np.sin(np.deg2rad(lat - 0.125))))
    area2d = area[:, None] * np.ones((1, len(lon)))
    lon2d, lat2d = np.meshgrid(lon, lat)

    masks0 = g._build_country_masks(lat, lon)
    a1 = [(prep(r.geometry), r.geometry) for r in shp.Reader(shp.natural_earth(
          resolution="10m", category="cultural", name="admin_1_states_provinces")
          ).records() if r.geometry is not None]
    manifest = json.load(open(DS / "manifest.json"))

    def mask_from_geom(geom):
        m = np.zeros(lat2d.shape, bool)
        minx, miny, maxx, maxy = geom.bounds
        cand = (lon2d >= minx) & (lon2d <= maxx) & (lat2d >= miny) & (lat2d <= maxy)
        if not cand.any():
            return m
        pg = prep(geom)
        for r, c in zip(*np.where(cand)):
            if pg.contains(Point(lon[c], lat[r])):
                m[r, c] = True
        return m

    def agg(mask):
        A = float(area2d[mask].sum())
        P = float(pop[mask].sum())
        G = float(gdp[mask].sum())
        if A <= 0 or P <= 0 or G <= 0:
            return None
        # population-weighted centroid -> the series' real location
        w = pop[mask]
        la = float((lat2d[mask] * w).sum() / w.sum())
        lo = float((lon2d[mask] * w).sum() / w.sum())
        return {"area_km2": A, "tot_pop": P, "tot_gdp": G,
                "urbanization": float(np.nanmean(urb[mask])),
                "latitude": la, "longitude": lo, "ncells": int(mask.sum())}

    out = {}
    for iso3, info in masks0.items():            # national footprint (ALL countries)
        r = agg(info["mask"])                     # zone="" -> national admin-0 mask
        if r:
            out[(iso3, "")] = r

    # ── 10m fallback for entities absent from the coarse 110m mask set
    # (small territories + Kosovo/Singapore: ALA, FRO, GLP, GUF, SGP, XKX).
    # The 110m Natural Earth admin-0 omits them, so without this they were
    # silently dropped by the inner merge -> model trained on 78/84 countries.
    need = set(manifest.keys())                  # all archive countries (84)
    missing = sorted(need - {iso for iso, _ in out})
    if missing:
        print(f"  10m fallback for {len(missing)} entities absent from 110m masks: {missing}")
        a0 = shp.Reader(shp.natural_earth(
            resolution="10m", category="cultural", name="admin_0_countries"))
        geom10 = {}
        for rec in a0.records():
            iso = (rec.attributes.get("ADM0_A3") or rec.attributes.get("ISO_A3", ""))
            if iso in missing and rec.geometry is not None:
                geom10[iso] = rec.geometry
        for iso in missing:
            mk = mask_from_geom(geom10[iso]) if iso in geom10 else np.zeros(lat2d.shape, bool)
            if mk.sum() < 1:
                # too small for any 0.25° cell centre -> use the cell nearest the
                # population-weighted manifest centroid (city-states like SGP)
                info = manifest.get(iso, {})
                la0, lo0 = info.get("lat"), info.get("lon")
                if la0 is None and iso in geom10:
                    c = geom10[iso].centroid; la0, lo0 = c.y, c.x
                if la0 is None:
                    continue
                ri = int(np.argmin(np.abs(lat - la0))); ci = int(np.argmin(np.abs(lon - lo0)))
                mk = np.zeros(lat2d.shape, bool); mk[ri, ci] = True
            r = agg(mk)
            if r:
                out[(iso, "")] = r
                print(f"    recovered {iso}: cells={int(mk.sum())} pop={r['tot_pop']:.0f}")
    # zonal series -> admin-1 polygon containing the manifest zone centroid
    nz = 0
    for iso3 in ZONAL:
        for code, z in manifest.get(iso3, {}).get("zones", {}).items():
            if "lat" not in z:
                continue
            geom = next((gm for pr, gm in a1 if pr.contains(Point(z["lon"], z["lat"]))), None)
            if geom is None:
                continue
            mk = mask_from_geom(geom)
            if mk.sum() < 2:
                continue
            r = agg(mk)
            if r:
                out[(iso3, code)] = r
                nz += 1
    print(f"per-series static computed: {len(out)} units ({nz} zonal)")
    recs = []
    for (iso3, zone), r in out.items():
        recs.append({"country": iso3, "zone": zone, **r})
    return pd.DataFrame(recs)


def main() -> int:
    print("loading hourly frame (demand+climate+calendar)...")
    df = pd.read_parquet(ABS)
    if "zone" not in df.columns:
        df["zone"] = ""
    df["zone"] = df["zone"].fillna("").astype(str)
    df["country"] = df["country"].astype(str)

    print("building per-series static (area/pop/gdp/urb)...")
    stat = build_series_static()
    stat["zone"] = stat["zone"].fillna("").astype(str)
    stat.to_parquet(STATIC)
    print(f"saved {STATIC.name} ({len(stat)} series)")

    # intensive static features
    stat["log_pop_density"] = np.log10(stat["tot_pop"] / stat["area_km2"])
    stat["log_gdp_density"] = np.log10(stat["tot_gdp"] / stat["area_km2"])
    stat["log_gdp_per_cap"] = np.log10(stat["tot_gdp"] / stat["tot_pop"])
    SCOLS = ["log_pop_density", "log_gdp_density", "log_gdp_per_cap",
             "urbanization", "latitude", "longitude", "area_km2"]

    # join static onto hourly frame (drop absolute-dataset's static dups first)
    drop = [c for c in ["log_total_pop", "log_total_gdp", "urbanization",
                        "latitude", "longitude"] if c in df.columns]
    df = df.drop(columns=drop)
    df = df.merge(stat[["country", "zone"] + SCOLS], on=["country", "zone"], how="inner")

    df = df[df["demand_mw"] > 0].copy()
    df["target"] = np.log10(df["demand_mw"] / df["area_km2"])

    FEATS = ["log_pop_density", "log_gdp_density", "log_gdp_per_cap", "urbanization",
             "temperature", "hdd", "cdd", "hour_sin", "hour_cos", "dow_sin", "dow_cos",
             "month_sin", "month_cos", "is_weekend", "is_holiday",
             "days_to_next_holiday", "days_from_prev_holiday", "latitude", "longitude"]
    FEATS = [f for f in FEATS if f in df.columns]
    df = df.dropna(subset=FEATS + ["target"])
    keep = ["group_id", "country", "zone", "year", "time_idx",
            "demand_mw", "area_km2", "target"] + FEATS
    df[keep].to_parquet(OUT)
    print(f"saved {OUT.name}: {len(df):,} rows | series {df.group_id.nunique()} | "
          f"countries {df.country.nunique()} | features {len(FEATS)}")
    print(f"  target log10(MW/km²): {df.target.min():.2f} .. "
          f"{df.target.median():.2f} .. {df.target.max():.2f}")
    print(f"  log_pop_density: {df.log_pop_density.min():.2f} .. {df.log_pop_density.max():.2f}")
    print(f"  log_gdp_density: {df.log_gdp_density.min():.2f} .. {df.log_gdp_density.max():.2f}")
    print(f"  log_gdp_per_cap: {df.log_gdp_per_cap.min():.2f} .. {df.log_gdp_per_cap.max():.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
