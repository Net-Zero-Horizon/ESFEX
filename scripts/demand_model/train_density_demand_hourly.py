#!/usr/bin/env python
# Provenance: copied from
# /home/manuel/Dropbox/PhD/Pappers/09_Flexibility/ReFlex/demand_estimation/train_density_demand_hourly.py
# (modified 2026-06-06 09:49:45 +0900, md5 cee4bea1aba495943b0cd76873579b3d), the
# script that trained the shipped demand_density_hourly_xgb model (v1). Changes: git log.
"""OPTION B — train the pixel-transferable HOURLY demand-DENSITY model (XGBoost).

Target: log10(demand_mw / area_km2).  Out-of-time hold-out (latest year) +
leave-one-country-out spot checks.  Then the three OPTION-B verifications:
  (1) scale transfer  — pixel intensive-feature ranges overlap training ranges,
  (2) balanced climate — S(T) U-curve (heating ramp AND cooling ramp),
      + Hokkaido (43.5N) winter-peak vs Fukuoka (33.6N) summer-peak,
  (3) industry        — high-gdp-density / low-pop-density pixel -> nonzero density.

Saves data/models/demand_density_hourly_xgb.json (+ .feats.json).
"""
from __future__ import annotations
import sys, json
from pathlib import Path
import numpy as np, pandas as pd
import xgboost as xgb
from sklearn.metrics import r2_score

HERE = Path("/home/manuel/Dropbox/PhD/Pappers/09_Flexibility/ReFlex/demand_estimation")
DF = Path("/media/manuel/New Volume/Demand_forecasting")
DATA = HERE / "ecvi" / "density_demand_dataset.parquet"
FEAT_NPZ = HERE / "ecvi" / "feature_grids_025.npz"
POP_NPZ = HERE / "ecvi" / "ecvi_ssp2_2025_2050_tft_84c_fold0.npz"
MODEL_DIR = DF / "demand_estimation" / "data" / "models"
MODEL = MODEL_DIR / "demand_density_hourly_xgb.json"

# NOTE: urbanization is DROPPED. It is the one non-intensive feature here — the
# training value is the country/zone MASK-MEAN of GHSL% (median ~3, max ~72) while
# a single pixel is 0 (rural) or 100 (city core): NOT scale-consistent, so it
# breaks OPTION-B transfer (pixel urb is out-of-domain on the very feature that
# dominated gain). Dropping it keeps OOT R² (0.963) and improves cross-country
# LOCO; the truly-intensive densities carry the same information scale-consistently.
FEATS = ["log_pop_density", "log_gdp_density", "log_gdp_per_cap",
         "temperature", "hdd", "cdd", "hour_sin", "hour_cos", "dow_sin", "dow_cos",
         "month_sin", "month_cos", "is_weekend", "is_holiday",
         "days_to_next_holiday", "days_from_prev_holiday", "latitude", "longitude"]

PARAMS = {"max_depth": 9, "eta": 0.06, "subsample": 0.8, "colsample_bytree": 0.8,
          "min_child_weight": 4, "objective": "reg:squarederror",
          "tree_method": "hist", "device": "cuda"}
ROUNDS = 600


def cal(month, hour, dow):
    """calendar sin/cos + weekend for a synthetic row."""
    return dict(
        hour_sin=np.sin(2 * np.pi * hour / 24), hour_cos=np.cos(2 * np.pi * hour / 24),
        dow_sin=np.sin(2 * np.pi * dow / 7), dow_cos=np.cos(2 * np.pi * dow / 7),
        month_sin=np.sin(2 * np.pi * month / 12), month_cos=np.cos(2 * np.pi * month / 12),
        is_weekend=float(dow >= 5), is_holiday=0.0,
        days_to_next_holiday=30.0, days_from_prev_holiday=30.0)


def main() -> int:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.read_parquet(DATA)
    print(f"dataset: {len(df):,} rows | {df.group_id.nunique()} series | "
          f"{df.country.nunique()} countries | years {df.year.min()}-{df.year.max()}")

    # ── out-of-time hold-out: latest year ──
    ymax = df.year.max()
    tr = df[df.year < ymax]; va = df[df.year == ymax]
    if len(tr) > 4_000_000:
        tr = tr.sample(4_000_000, random_state=0)
    dtr = xgb.DMatrix(tr[FEATS], label=tr["target"], feature_names=FEATS)
    dva = xgb.DMatrix(va[FEATS], label=va["target"], feature_names=FEATS)
    bst = xgb.train(PARAMS, dtr, num_boost_round=ROUNDS,
                    evals=[(dva, "val")], early_stopping_rounds=40, verbose_eval=100)
    pv = bst.predict(dva)
    r2_logden = r2_score(va["target"], pv)
    # back-transform to MW: demand = 10**target * area_km2
    mw_true = 10 ** va["target"].to_numpy() * va["area_km2"].to_numpy()
    mw_pred = 10 ** pv * va["area_km2"].to_numpy()
    r2_mw = r2_score(mw_true, mw_pred)
    # aggregate to national hourly totals (the quantity we actually anchor)
    vv = va.copy(); vv["mw_pred"] = mw_pred
    nat = vv.groupby(["country", "time_idx"]).agg(
        t=("demand_mw", "sum"), p=("mw_pred", "sum")).reset_index()
    r2_nat = r2_score(nat["t"], nat["p"])
    print(f"\n[OOT VAL {ymax}] R²(log density)={r2_logden:.4f} | "
          f"R²(MW back-transf)={r2_mw:.4f} | R²(national hourly MW)={r2_nat:.4f}")

    # ── leave-one-country-out spot check (cross-country transfer = pixel proxy) ──
    print("\n[LOCO] leave-one-country-out spot check:")
    spot = ["JPN", "USA", "FIN", "ESP", "RUS", "ISL", "BRA", "DEU"]
    spot = [c for c in spot if c in df.country.unique()]
    for c in spot:
        trc = df[df.country != c]; tec = df[df.country == c]
        if len(trc) > 3_000_000:
            trc = trc.sample(3_000_000, random_state=1)
        b = xgb.train(PARAMS, xgb.DMatrix(trc[FEATS], label=trc["target"], feature_names=FEATS),
                      num_boost_round=350)
        pc = b.predict(xgb.DMatrix(tec[FEATS], feature_names=FEATS))
        r2d = r2_score(tec["target"], pc)
        mt = 10 ** tec["target"].to_numpy() * tec["area_km2"].to_numpy()
        mp = 10 ** pc * tec["area_km2"].to_numpy()
        # national hourly
        tt = tec.copy(); tt["mp"] = mp
        n2 = tt.groupby("time_idx").agg(t=("demand_mw", "sum"), p=("mp", "sum"))
        print(f"  {c}: R²(log density)={r2d:+.3f} | "
              f"R²(national hourly MW)={r2_score(n2['t'], n2['p']):+.3f} | "
              f"mean log-dens true={tec['target'].mean():.2f} pred={pc.mean():.2f}")

    # ── refit on ALL data and save ──
    dall = xgb.DMatrix(df[FEATS], label=df["target"], feature_names=FEATS)
    final = xgb.train(PARAMS, dall, num_boost_round=bst.best_iteration + 1 or ROUNDS)
    final.save_model(str(MODEL))
    json.dump({"features": FEATS, "target": "log10(demand_mw/area_km2)",
               "r2_log_density": r2_logden, "r2_mw": r2_mw, "r2_national_hourly": r2_nat,
               "n_rows": len(df), "n_series": int(df.group_id.nunique()),
               "n_countries": int(df.country.nunique()),
               "train_ranges": {f: [float(df[f].min()), float(df[f].max())] for f in FEATS}},
              open(MODEL.with_suffix(".feats.json"), "w"), indent=2)
    imp = final.get_score(importance_type="gain")
    print("\nfeature gain:", {k: round(v, 1) for k, v in
                              sorted(imp.items(), key=lambda x: -x[1])[:10]})
    print(f"saved -> {MODEL}")

    # ════════ VERIFICATION 1 — scale transfer (pixel ranges overlap training) ════════
    print("\n" + "=" * 70)
    print("VERIFY 1 — SCALE TRANSFER: pixel intensive-feature ranges vs training")
    d = np.load(POP_NPZ); lat, lon, pop = d["lat"], d["lon"], d["exposure"]
    fg = np.load(FEAT_NPZ); gdp = fg["gdp"]
    R, dd = 6371.0, np.deg2rad(0.25)
    area = np.abs(R * R * dd * (np.sin(np.deg2rad(lat + 0.125)) - np.sin(np.deg2rad(lat - 0.125))))
    area2d = area[:, None] * np.ones((1, len(lon)))
    land = pop > 0
    pix_popden = np.log10(np.clip(pop[land] / area2d[land], 1e-9, None))
    pix_gdpden = np.log10(np.clip(gdp[land] / area2d[land], 1e-9, None))
    with np.errstate(divide="ignore", invalid="ignore"):
        gpc = np.where(pop[land] > 0, gdp[land] / pop[land], np.nan)
    pix_gpc = np.log10(np.clip(gpc, 1e-9, None))
    for nm, pix, col in [("log_pop_density", pix_popden, "log_pop_density"),
                         ("log_gdp_density", pix_gdpden, "log_gdp_density"),
                         ("log_gdp_per_cap", pix_gpc[np.isfinite(pix_gpc)], "log_gdp_per_cap")]:
        tr_lo, tr_hi = df[col].quantile([0.01, 0.99])
        px_lo, px_hi = np.nanpercentile(pix, [5, 95])
        ov = max(0.0, min(tr_hi, px_hi) - max(tr_lo, px_lo)) / (px_hi - px_lo + 1e-9)
        inrange = ((pix >= tr_lo) & (pix <= tr_hi)).mean()
        print(f"  {nm:16s} train[1-99%]=[{tr_lo:5.2f},{tr_hi:5.2f}] "
              f"pixel[5-95%]=[{px_lo:5.2f},{px_hi:5.2f}] "
              f"overlap={ov*100:4.0f}%  pixels-in-train-range={inrange*100:4.0f}%")
    print("  (prior absolute model used log_total_pop/gdp in [6,9]; pixels fell to 0-6,")
    print("   OUT of domain -> flat magnitude. Intensive densities now OVERLAP.)")

    # ════════ VERIFICATION 2 — balanced climate response (U-curve) ════════
    print("\n" + "=" * 70)
    print("VERIFY 2 — CLIMATE U-CURVE: sweep T at median features (winter noon)")
    med = {f: float(df[f].median()) for f in FEATS}
    med.update(cal(month=1, hour=12, dow=2))  # neutral calendar
    Ts = np.arange(-20, 41, 2.0)
    rows = []
    for T in Ts:
        r = dict(med); r["temperature"] = T
        r["hdd"] = max(0.0, 18 - T); r["cdd"] = max(0.0, T - 24)
        rows.append(r)
    S = 10 ** final.predict(xgb.DMatrix(pd.DataFrame(rows)[FEATS], feature_names=FEATS))
    S = S / S[np.argmin(np.abs(Ts - 18))]  # normalize to 18°C
    Tmin_i = np.argmin(S)
    heat = S[np.argmin(np.abs(Ts - (-10)))] / S[Tmin_i]   # rise into cold
    cool = S[np.argmin(np.abs(Ts - 38))] / S[Tmin_i]      # rise into heat
    print(f"  S(T) normalized to T=18°C  (min at T={Ts[Tmin_i]:.0f}°C):")
    for T in [-20, -10, 0, 10, 18, 25, 30, 35, 40]:
        i = np.argmin(np.abs(Ts - T))
        print(f"    T={T:4d}°C  S={S[i]:.3f}")
    print(f"  HEATING ramp (cold/min)={heat:.2f}x   COOLING ramp (hot/min)={cool:.2f}x")
    print(f"  => U-curve present: heating={'YES' if heat>1.15 else 'NO/WEAK'} "
          f"cooling={'YES' if cool>1.15 else 'NO/WEAK'}")

    # Hokkaido (cold, high-lat) vs Fukuoka (warm) seasonal profiles
    print("\n  Hokkaido(43.5N) winter-peak vs Fukuoka(33.6N) summer-peak:")
    # monthly mean temps (climatology, °C) — Sapporo & Fukuoka
    sapporo = [-3.6, -3.1, 0.6, 7.1, 12.4, 16.7, 20.5, 22.3, 18.1, 11.8, 4.9, -0.9]
    fukuoka = [6.6, 7.4, 10.4, 15.1, 19.4, 23.0, 27.2, 28.1, 24.4, 19.1, 13.8, 8.9]
    def profile(lat_v, lon_v, temps):
        out = []
        for m in range(12):
            r = dict(med); r["latitude"] = lat_v; r["longitude"] = lon_v
            r["temperature"] = temps[m]
            r["hdd"] = max(0.0, 18 - temps[m]); r["cdd"] = max(0.0, temps[m] - 24)
            r.update(cal(month=m + 1, hour=19, dow=2))  # evening peak hour
            out.append(r)
        v = 10 ** final.predict(xgb.DMatrix(pd.DataFrame(out)[FEATS], feature_names=FEATS))
        return v / v.mean()
    hok = profile(43.5, 142.5, sapporo)
    fuk = profile(33.6, 130.4, fukuoka)
    print(f"    Hokkaido peak month={np.argmax(hok)+1:2d} (DJF winter expected); "
          f"Jan/Jul ratio={hok[0]/hok[6]:.2f}")
    print(f"    Fukuoka  peak month={np.argmax(fuk)+1:2d} (JAS summer expected); "
          f"Jul/Jan ratio={fuk[6]/fuk[0]:.2f}")

    # ════════ VERIFICATION 3 — industry pixel ════════
    print("\n" + "=" * 70)
    print("VERIFY 3 — INDUSTRY: high-gdp-density / low-pop-density pixel")
    base = dict(med); base.update(cal(month=6, hour=14, dow=2)); base["temperature"] = 22
    base["hdd"] = 0; base["cdd"] = 4
    # factory pixel: pop density low (10th pct), gdp density high (95th pct)
    fac = dict(base)
    fac["log_pop_density"] = float(df.log_pop_density.quantile(0.10))
    fac["log_gdp_density"] = float(df.log_gdp_density.quantile(0.95))
    fac["log_gdp_per_cap"] = fac["log_gdp_density"] - fac["log_pop_density"]
    # rural residential: same low pop, but LOW gdp density
    rur = dict(base)
    rur["log_pop_density"] = fac["log_pop_density"]
    rur["log_gdp_density"] = float(df.log_gdp_density.quantile(0.20))
    rur["log_gdp_per_cap"] = rur["log_gdp_density"] - rur["log_pop_density"]
    fac_d = 10 ** final.predict(xgb.DMatrix(pd.DataFrame([fac])[FEATS], feature_names=FEATS))[0]
    rur_d = 10 ** final.predict(xgb.DMatrix(pd.DataFrame([rur])[FEATS], feature_names=FEATS))[0]
    print(f"  factory pixel (low pop, HIGH gdp density): density={fac_d:.4f} MW/km²")
    print(f"  rural pixel   (low pop, low  gdp density): density={rur_d:.4f} MW/km²")
    print(f"  => factory/rural ratio={fac_d/rur_d:.1f}x  "
          f"(industry pixel NOT ~0: {'PASS' if fac_d > 5*rur_d and fac_d>0.05 else 'CHECK'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
