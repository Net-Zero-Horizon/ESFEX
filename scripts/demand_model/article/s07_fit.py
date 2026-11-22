#!/usr/bin/env python
"""Fit the chosen specification on every series, plus a country bootstrap.

Bootstrap: countries are resampled with replacement and the model refitted. The spread of
the resulting sensitivity fields measures how much the map depends on which countries
happen to be in the archive, which is the uncertainty that matters where no data exist.

Usage: s07_fit.py <spec> [n_bootstrap=30] [first=0]
Writes models/<spec>.json, models/<spec>.meta.json, models/<spec>_boot<k>.json (and, for a
model trained in two rounds, the first round of each in <name>.level.json).

With first > 0 only the refits first..n_bootstrap-1 are fitted, to add refits to those that
exist: the model of the article is left untouched and the draws are the same as in one run,
because the earlier ones are drawn and discarded.
"""
from __future__ import annotations
import sys, json
import numpy as np, pandas as pd
from paths import DATASET, MODELS
import model as M


def main() -> int:
    spec = sys.argv[1]; B = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    first = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    sp = M.SPECS[spec]
    df = M.prepare(pd.read_parquet(DATASET / "density_demand_dataset.parquet"))
    if first == 0:
        fit_all(df, spec, sp)
    countries = np.sort(df.country.unique()); code = pd.Categorical(df.country, categories=countries).codes
    rng = np.random.default_rng(2026)
    for b in range(B):
        mult = np.bincount(rng.integers(0, len(countries), len(countries)), minlength=len(countries))
        if b < first:
            continue
        w = mult[code].astype(np.float32); keep = w > 0
        M.save(M.train(df[keep], spec, weight=w[keep]), MODELS / f"{spec}_boot{b:02d}.json")
        print(f"  bootstrap {b:02d}: {int((mult > 0).sum())} distinct countries", flush=True)
    return 0


def fit_all(df, spec, sp):
    """The model of the article: every series, and the range of its predictors."""
    bst = M.train(df, spec)
    M.save(bst, MODELS / f"{spec}.json")
    ser = df.groupby("series")[M.SOCIO + ["latitude", "longitude"] + M.NORMALS + ["hh_elec_share", "bld_share_elec"]].first()   # range of the archive
    meta = {"spec": spec, "features": sp["feats"], "target": sp["target"], "rounds": M.ROUNDS,
            "params": {k: v for k, v in M.PARAMS.items() if k != "device"},
            "level_features": sp.get("level_feats"), "level_params": sp.get("level_params"),
            "n_hours": int(len(df)), "n_series": int(df.series.nunique()),
            "n_series_years": int(df.group_id.nunique()), "n_countries": int(df.country.nunique()),
            "static_range": {c: [float(ser[c].min()), float(ser[c].max())] for c in ser.columns},
            "temperature_range": [float(df[sp["temp"][0]].min()), float(df[sp["temp"][0]].max())],
            "gain_share": M.gain_share(bst)}
    json.dump(meta, open(MODELS / f"{spec}.meta.json", "w"), indent=1)
    ser.to_csv(MODELS / f"{spec}.training_statics.csv")
    print(f"{spec}: fitted on {len(df):,} hours, {meta['n_series']} series, {meta['n_countries']} countries")
    for name, g in (meta["gain_share"].items() if "level_feats" in sp else [("all", meta["gain_share"])]):
        print(f"gain share ({name}):", {k: round(v, 3) for k, v in list(g.items())[:8]})


if __name__ == "__main__":
    raise SystemExit(main())
