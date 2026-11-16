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

Clock convention (ESFEX, rounds 31-32). Every row is one LOCAL CIVIL hour of
the series' location (its time zone from the population-weighted centroid,
daylight saving time included; 29 February dropped). The demand and the ERA5
temperature of a row are those of the same instant, and the calendar
features describe the row's real local date (``esfex.utils.temporal.
year_row_dates``). v1 instead used archive row ``h`` as hour ``h`` of the
year whatever the source's clock, the UTC temperature of hour ``h`` and a
calendar read from the row index (reflexpy ``build_unified_dataset``, via
build_absolute_demand_dataset.py). ``clock_audit.py`` establishes the clock
of every archive series-year (``series_clocks.csv``); years whose clock it
cannot establish are left out.

Inputs (read only): the demand archive (``{ISO3}/*.parquet`` and the UTC
ERA5 caches in ``_era5``), ``series_clocks.csv`` and a per-series static
table (``density_series_static.parquet``: area, population, GDP,
urbanization, population-weighted centroid). ``--rebuild-static`` recomputes
that table with ``build_series_static`` (cartopy, the 0.25 deg population
and feature grids and the country-mask helper of the paper's workspace).

Usage::

    PYTHONPATH=src python scripts/demand_model/build_density_demand_dataset.py \\
        --archive ".../Demand_forecasting/data/demand_dataset" \\
        --clocks OUTDIR/series_clocks.csv --static ".../ecvi/density_series_static.parquet" \\
        --out OUTDIR/density_demand_dataset.parquet
"""
from __future__ import annotations
import argparse, sys, json
from pathlib import Path
import numpy as np, pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import clocks  # noqa: E402

# zonal countries whose demand series are sub-national (admin-1 footprints)
ZONAL = {"CHN", "AUS", "CAN", "BRA", "THA", "TWN", "USA", "IND", "JPN",
         "ITA", "NOR", "SWE", "DNK"}
HDD_BASE, CDD_BASE = 18.0, 24.0          # as v1 (reflexpy pixel_features)
FEATS = ["log_pop_density", "log_gdp_density", "log_gdp_per_cap", "urbanization",
         "temperature", "hdd", "cdd", "hour_sin", "hour_cos", "dow_sin", "dow_cos",
         "month_sin", "month_cos", "is_weekend", "is_holiday",
         "days_to_next_holiday", "days_from_prev_holiday", "latitude", "longitude"]
SCOLS = ["log_pop_density", "log_gdp_density", "log_gdp_per_cap",
         "urbanization", "latitude", "longitude", "area_km2"]


def build_series_static(pop_npz: Path, feat_npz: Path, manifest_path: Path,
                        helper_dir: Path):
    """Per (iso3, zone) series: area_km2, total pop P, total gdp G, urb, lat, lon.

    *helper_dir* holds ``generate_sensitivity_figures.py`` (its
    ``_build_country_masks``). Run with ``PYTHONDONTWRITEBYTECODE=1`` when it
    is a read-only workspace.
    """
    sys.path.insert(0, str(helper_dir))
    import generate_sensitivity_figures as g
    import cartopy.io.shapereader as shp
    from shapely.geometry import Point
    from shapely.prepared import prep

    d = np.load(pop_npz)
    lat, lon, pop = d["lat"], d["lon"], d["exposure"]
    fg = np.load(feat_npz); gdp, urb = fg["gdp"], fg["urb"]
    R, dd = 6371.0, np.deg2rad(0.25)
    area = np.abs(R * R * dd * (np.sin(np.deg2rad(lat + 0.125)) - np.sin(np.deg2rad(lat - 0.125))))
    area2d = area[:, None] * np.ones((1, len(lon)))
    lon2d, lat2d = np.meshgrid(lon, lat)

    masks0 = g._build_country_masks(lat, lon)
    a1 = [(prep(r.geometry), r.geometry) for r in shp.Reader(shp.natural_earth(
          resolution="10m", category="cultural", name="admin_1_states_provinces")
          ).records() if r.geometry is not None]
    manifest = json.load(open(manifest_path))

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


def era5_years(archive: Path, country: str, zone: str, years) -> dict[int, np.ndarray]:
    """The archive's UTC ERA5 years of a series, as v1 read them: the zone's
    cache first, then the country's (first 8760 UTC hours of each year)."""
    out = {}
    for y in years:
        for name in ([f"era5_{zone}_{y}.npy"] if zone else []) + [f"era5_{y}.npy"]:
            p = archive / "_era5" / country / name
            if p.exists():
                out[int(y)] = np.load(p).astype(float)[:8760]
                break
    return out


def parquet_temperature(archive: Path, country: str, zone: str, year: int):
    """The parquet's ``temperature_c`` (v1's fallback without an ERA5 cache)."""
    from clock_audit import archive_path
    t = pd.read_parquet(archive_path(archive, country, zone, year),
                        columns=["temperature_c"])["temperature_c"].to_numpy(float)[:8760]
    return None if np.isnan(t).all() else t


def series_frame(archive: Path, rows: pd.DataFrame) -> pd.DataFrame:
    """Hourly frame of one series on its local civil time (kept years only)."""
    from clock_audit import load_demand

    c, z = rows.country.iat[0], rows.zone.iat[0]
    local = rows.local_zone.iat[0]
    years = [int(y) for y in rows.year]
    dem = {y: load_demand(archive, c, z, y) for y in years}
    dem_local = clocks.on_local_time(dem, dict(zip(years, rows.clock)),
                                     dict(zip(years, rows.feb29_kept.astype(bool))), local)

    # temperature at the same instants; neighbouring years cover the hours
    # a local year shares with them
    temp = era5_years(archive, c, z, sorted(set(years) | {y - 1 for y in years} | {y + 1 for y in years}))
    t_clock = {y: "UTC" for y in temp}
    for y in years:
        if y not in temp:
            t = parquet_temperature(archive, c, z, y)
            if t is not None:
                temp[y] = t
                t_clock[y] = clocks.PARQUET_TEMPERATURE_CLOCKS.get((c, z, y), "UTC")
    temp_local = (clocks.on_local_time(temp, t_clock, {y: True for y in temp}, local, targets=years)
                  if temp else {})

    parts = []
    for y in years:
        if y not in temp:
            # no temperature of its own (v1 used a constant 20 °C): left out
            continue
        t = temp_local[y]
        parts.append(pd.DataFrame({
            "group_id": f"{c}_{z}_{y}" if z else f"{c}_{y}",
            "country": c, "zone": z, "year": y, "time_idx": np.arange(8760),
            "demand_mw": dem_local[y], "temperature": t,
            "hdd": np.maximum(HDD_BASE - t, 0.0), "cdd": np.maximum(t - CDD_BASE, 0.0),
            **clocks.calendar_features(y, c),
        }))
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def static_features(stat: pd.DataFrame) -> pd.DataFrame:
    """Intensive static features of each series (as v1)."""
    stat = stat.copy()
    stat["zone"] = stat["zone"].fillna("").astype(str)
    stat["log_pop_density"] = np.log10(stat["tot_pop"] / stat["area_km2"])
    stat["log_gdp_density"] = np.log10(stat["tot_gdp"] / stat["area_km2"])
    stat["log_gdp_per_cap"] = np.log10(stat["tot_gdp"] / stat["tot_pop"])
    return stat


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Density demand training dataset on local civil time.")
    ap.add_argument("--archive", type=Path, required=True, help="demand archive (read only)")
    ap.add_argument("--clocks", type=Path, required=True, help="series_clocks.csv (clock_audit.py)")
    ap.add_argument("--static", type=Path, help="density_series_static.parquet to reuse")
    ap.add_argument("--rebuild-static", action="store_true",
                    help="recompute the static table (needs --pop-npz, --feat-npz, --helper-dir)")
    ap.add_argument("--pop-npz", type=Path)
    ap.add_argument("--feat-npz", type=Path)
    ap.add_argument("--helper-dir", type=Path)
    ap.add_argument("--out", type=Path, required=True, help="output dataset parquet")
    a = ap.parse_args(argv)
    a.out.parent.mkdir(parents=True, exist_ok=True)

    if a.rebuild_static:
        print("building per-series static (area/pop/gdp/urb)...")
        stat = build_series_static(a.pop_npz, a.feat_npz, a.archive / "manifest.json", a.helper_dir)
        stat.to_parquet(a.out.with_name("density_series_static.parquet"))
    else:
        stat = pd.read_parquet(a.static)
    stat = static_features(stat)
    print(f"static table: {len(stat)} series")

    tab = pd.read_csv(a.clocks, keep_default_na=False)
    tab["zone"] = tab["zone"].astype(str)
    tab["feb29_kept"] = tab["feb29_kept"].astype(str).str.lower().eq("true")
    kept = tab[tab.status != "excluded"]
    print(f"series-years: {len(tab)}, kept {len(kept)}, left out {len(tab) - len(kept)} "
          "(clock not established)")

    frames = []
    for i, ((c, z), g) in enumerate(kept.groupby(["country", "zone"], sort=True)):
        frames.append(series_frame(a.archive, g))
        if (i + 1) % 20 == 0:
            print(f"  {i + 1} series")
    df = pd.concat(frames, ignore_index=True)
    df = df.merge(stat[["country", "zone"] + SCOLS], on=["country", "zone"], how="inner")

    df = df[df["demand_mw"] > 0].copy()
    df["target"] = np.log10(df["demand_mw"] / df["area_km2"])
    n_before = len(df)
    df = df.dropna(subset=FEATS + ["target"])
    print(f"rows without temperature or demand at their instant: {n_before - len(df):,}")
    keep = ["group_id", "country", "zone", "year", "time_idx",
            "demand_mw", "area_km2", "target"] + FEATS
    df[keep].to_parquet(a.out)
    print(f"saved {a.out}: {len(df):,} rows | series {df.group_id.nunique()} | "
          f"countries {df.country.nunique()} | features {len(FEATS)}")
    print(f"  target log10(MW/km²): {df.target.min():.2f} .. "
          f"{df.target.median():.2f} .. {df.target.max():.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
