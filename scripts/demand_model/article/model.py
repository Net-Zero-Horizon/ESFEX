"""Demand model: feature sets, training, temperature response and sensitivity.

Shared by cross-validation (s04), the final fit (s07) and the gridded fields (s08).

Specifications compared in cross-validation (the "universal" family is defined below):

  level_geo    The submitted design. Target log10(demand density); hour in UTC and calendar
               month, with latitude and longitude so the model can place them.
  level_nogeo  Same target, no coordinates: hour is local solar time, month is shifted by
               six months in the southern hemisphere.
  shape        Target is the hourly anomaly of log10 demand about its series-year mean, so
               the level of a series and any trend between years are removed before
               fitting, as a fixed effect would. Population density, income and the
               long-run climate then have no main effect; they can only change how demand
               responds to temperature and to the calendar.
  shape_daily  As shape, with DAILY mean temperature as the temperature predictor. The
               climate projections are daily, so training and gridded inference then see
               the same kind of temperature.
"""
from __future__ import annotations
import os
from pathlib import Path
import numpy as np, pandas as pd, xgboost as xgb

SOCIO = ["log_pop_density", "log_gdp_density", "log_gdp_per_cap"]
NORMALS = ["t_mean", "t_range"]
CAL_UTC = ["hour_sin", "hour_cos", "dow_sin", "dow_cos", "month_sin", "month_cos", "is_weekend",
           "is_holiday", "days_to_next_holiday", "days_from_prev_holiday"]
CAL_LOCAL = ["lhour_sin", "lhour_cos", "dow_sin", "dow_cos", "smonth_sin", "smonth_cos",
             "is_weekend", "is_holiday", "days_to_next_holiday", "days_from_prev_holiday"]
T_HOURLY = ["temperature", "hdd", "cdd"]
T_DAILY = ["t_day", "hdd_day", "cdd_day"]

SPECS = {
    "level_geo": dict(feats=SOCIO + T_HOURLY + CAL_UTC + ["latitude", "longitude"], target="target",
                      statics=SOCIO + ["latitude", "longitude"], temp=T_HOURLY),
    "level_nogeo": dict(feats=SOCIO + T_HOURLY + CAL_LOCAL, target="target", statics=SOCIO, temp=T_HOURLY),
    "shape": dict(feats=SOCIO + NORMALS + T_HOURLY + CAL_LOCAL, target="target_anom",
                  statics=SOCIO + NORMALS, temp=T_HOURLY),
    "shape_daily": dict(feats=SOCIO + NORMALS + T_DAILY + CAL_LOCAL, target="target_anom",
                        statics=SOCIO + NORMALS, temp=T_DAILY),
}
# Anomaly-target models with daily temperature that differ only in what the response may
# depend on. They are compared on countries and regions held out of training (s04):
#   universal         temperature and calendar only: one response for every place
#   universal_clim    + long-run climate of the place (mean and spread of daily temperature)
#   universal_income  + long-run climate and GDP per capita
#   universal_full    + long-run climate, GDP per capita, population and GDP density
# These are the alternatives against which the final specification ("context", below) is compared.
_D6 = {"max_depth": 6}
for _name, _st in (("universal", []), ("universal_clim", NORMALS),
                   ("universal_income", ["log_gdp_per_cap"] + NORMALS), ("universal_full", SOCIO + NORMALS)):
    SPECS[_name] = dict(feats=_st + T_DAILY + CAL_LOCAL, target="target_anom", statics=_st, temp=T_DAILY,
                        params=_D6)

# Context of the electricity system (UN energy balances, country level), entered as exposures
# so that each can only act on its own side of the response:
#   eh_hdd  electric share of household energy x heating degrees of the day (zero when warm)
#   bw      share of electricity used in buildings x degrees above 18 C     (zero when cold)
DERIVED = {"eh_hdd": ("hh_elec_share", lambda t: np.maximum(HDD_BASE - t, 0.0)),
           "bw": ("bld_share_elec", lambda t: np.maximum(t - HDD_BASE, 0.0))}
SPECS["universal_heat"] = dict(feats=["eh_hdd"] + T_DAILY + CAL_LOCAL, target="target_anom",
                               statics=["hh_elec_share"], temp=T_DAILY, params=_D6)
# "context": the model used for the maps. The response may differ between places through the
# long-run climate and the two exposures above; income and densities add nothing beyond them.
SPECS["context"] = dict(feats=["eh_hdd", "bw"] + NORMALS + T_DAILY + CAL_LOCAL, target="target_anom",
                        statics=["hh_elec_share", "bld_share_elec"] + NORMALS, temp=T_DAILY, params=_D6)

# The model of the submission (level_geo) improved one change at a time, each measured against
# the previous state with the same cross-validation. Target, predictors and algorithm are kept;
# predictors are only added.
#   level_geo_context   + long-run climate (t_mean, t_range) and the two descriptors of
#                         electricity use (hh_elec_share, bld_share_elec)
_CTX = NORMALS + ["hh_elec_share", "bld_share_elec"]
SPECS["level_geo_context"] = dict(feats=SPECS["level_geo"]["feats"] + _CTX, target="target",
                                  statics=SPECS["level_geo"]["statics"] + _CTX, temp=T_HOURLY)
#   level_geo_local     + the same calendar in local solar time and with the month aligned by
#                         season (the UTC calendar stays)
_LOCAL = ["lhour_sin", "lhour_cos", "smonth_sin", "smonth_cos"]
SPECS["level_geo_local"] = dict(feats=SPECS["level_geo_context"]["feats"] + _LOCAL, target="target",
                                statics=SPECS["level_geo_context"]["statics"], temp=T_HOURLY)
# The sensitivity of the article is to the daily mean temperature, so from here on the steps are
# scored against the observed daily-temperature eta ("bench"); the two earlier states likewise.
SPECS["level_geo_local_d"] = dict(SPECS["level_geo_local"], bench="t_day")
SPECS["level_geo_d"] = dict(SPECS["level_geo"], bench="t_day")          # the submitted model, same scoring
#   level_geo_two       the same model, target and predictors, trained in two rounds: first the
#                         level of each place from its fixed predictors ("level_feats"), then,
#                         starting from that level, the hourly part with every predictor. The
#                         prediction is still one sum of trees for log10 demand density.
SPECS["level_geo_two"] = dict(SPECS["level_geo_local"], level_feats=SPECS["level_geo_local"]["statics"],
                              bench="t_day")
#   level_geo_two_shallow  the first round with shallow trees ("level_params"): at full depth it
#                         reproduces the level of every system it has seen and transfers it worse
#                         to places it has not
SPECS["level_geo_two_shallow"] = dict(SPECS["level_geo_two"], level_params={"max_depth": 3})
FINAL = "level_geo_two_shallow"          # the model of the article

HDD_BASE, CDD_BASE = 18.0, 24.0
PARAMS = {"max_depth": 9, "eta": 0.06, "subsample": 0.8, "colsample_bytree": 0.8,
          "min_child_weight": 4, "objective": "reg:squarederror", "tree_method": "hist",
          "device": os.environ.get("R1_DEVICE", "cuda"), "seed": 0}
ROUNDS = 300

BINS = np.arange(-45, 51, 1.0)
BC = (BINS[:-1] + BINS[1:]) / 2
NT = len(BC)
# balanced calendar over which a temperature response is averaged
CAL_GRID = [(mo, ho, dw) for mo in range(12) for ho in (3, 9, 15, 21) for dw in (2, 6)]


# ── feature construction ──
def prepare(df):
    """Add every derived column the specifications need (in place)."""
    a = np.deg2rad(df["longitude"].to_numpy())            # 15 deg of longitude = 1 hour
    hs, hc = df["hour_sin"].to_numpy(), df["hour_cos"].to_numpy()
    df["lhour_sin"] = (hs * np.cos(a) + hc * np.sin(a)).astype(np.float32)
    df["lhour_cos"] = (hc * np.cos(a) - hs * np.sin(a)).astype(np.float32)
    sgn = np.where(df["latitude"].to_numpy() < 0, -1.0, 1.0)   # six months = rotation by pi
    df["smonth_sin"] = (df["month_sin"].to_numpy() * sgn).astype(np.float32)
    df["smonth_cos"] = (df["month_cos"].to_numpy() * sgn).astype(np.float32)
    # daily mean temperature (UTC day of the series-year)
    day = (df["time_idx"].to_numpy() // 24).astype(np.int32)
    df["t_day"] = df.groupby([df["group_id"].to_numpy(), day])["temperature"].transform("mean").astype(np.float32)
    df["hdd_day"] = np.maximum(HDD_BASE - df["t_day"], 0.0).astype(np.float32)
    df["cdd_day"] = np.maximum(df["t_day"] - CDD_BASE, 0.0).astype(np.float32)
    # long-run climate of the series: mean and 5-95 % spread of daily mean temperature,
    # the statistics that are also available for every grid cell from the climate models
    daily = df.groupby([df["series"].to_numpy(), df["year"].to_numpy(), day])["temperature"].mean()
    g = daily.groupby(level=0)
    df["t_mean"] = df["series"].map(g.mean()).astype(np.float32)
    df["t_range"] = df["series"].map(g.quantile(0.95) - g.quantile(0.05)).astype(np.float32)
    df["target_anom"] = (df["target"] - df.groupby("group_id")["target"].transform("mean")).astype(np.float32)
    for name, (static, f) in DERIVED.items():
        if static in df.columns:
            df[name] = (df[static] * f(df["t_day"])).astype(np.float32)
    return df


def decode_calendar(df):
    """Integer UTC hour, day of week and month from their sin/cos encodings."""
    h = np.round(np.arctan2(df.hour_sin, df.hour_cos) / (2 * np.pi) * 24).astype(int) % 24
    w = np.round(np.arctan2(df.dow_sin, df.dow_cos) / (2 * np.pi) * 7).astype(int) % 7
    m = np.round(np.arctan2(df.month_sin, df.month_cos) / (2 * np.pi) * 12).astype(int) % 12
    return h.to_numpy(), w.to_numpy(), m.to_numpy()


# ── fitting and prediction ──
class TwoRound:
    """A model trained in two rounds: `level` on the fixed predictors, `hourly` on all of them
    starting from the level (its base margin). The prediction is the sum of the two."""
    def __init__(self, level, hourly, level_feats):
        self.level, self.hourly, self.level_feats = level, hourly, list(level_feats)


def train(df, spec, rounds=ROUNDS, weight=None, params=None):
    s = SPECS[spec]; p = params or {**PARAMS, **s.get("params", {})}
    if "level_feats" in s:
        lf = s["level_feats"]
        d1 = xgb.DMatrix(df[lf], label=df[s["target"]], weight=weight, feature_names=lf)
        level = xgb.train({**p, **s.get("level_params", {})}, d1, num_boost_round=rounds)
        d2 = xgb.DMatrix(df[s["feats"]], label=df[s["target"]], weight=weight, feature_names=s["feats"],
                         base_margin=level.predict(d1))
        return TwoRound(level, xgb.train(p, d2, num_boost_round=rounds), lf)
    d = xgb.DMatrix(df[s["feats"]], label=df[s["target"]], weight=weight, feature_names=s["feats"])
    return xgb.train(p, d, num_boost_round=rounds)


def save(bst, path):
    """<path> holds the model; a two-round model keeps its first round in <stem>.level.json."""
    path = Path(path)
    if isinstance(bst, TwoRound):
        bst.level.save_model(str(path.with_suffix(".level.json"))); bst.hourly.save_model(str(path))
    else:
        bst.save_model(str(path))


def load(path, spec):
    def one(f):
        b = xgb.Booster(); b.load_model(str(f)); b.set_param({"device": PARAMS["device"]}); return b
    path = Path(path); s = SPECS[spec]
    if "level_feats" in s:
        return TwoRound(one(path.with_suffix(".level.json")), one(path), s["level_feats"])
    return one(path)


def gain_share(bst):
    """Share of the total gain of each predictor (per round for a two-round model)."""
    def one(b):
        g = b.get_score(importance_type="total_gain"); tot = sum(g.values())
        return {k: v / tot for k, v in sorted(g.items(), key=lambda kv: -kv[1])}
    return {"level": one(bst.level), "hourly": one(bst.hourly)} if isinstance(bst, TwoRound) else one(bst)


def predict(bst, X, feats):
    feats = list(feats)
    if isinstance(bst, TwoRound):
        lf = bst.level_feats
        L = X[lf] if hasattr(X, "columns") else X[:, [feats.index(f) for f in lf]]
        base = bst.level.predict(xgb.DMatrix(L, feature_names=lf))
        return bst.hourly.predict(xgb.DMatrix(X, feature_names=feats, base_margin=base))
    return bst.predict(xgb.DMatrix(X, feature_names=feats))


def predict_warmer(bst, df, spec, dT=1.0):
    """Prediction for the same hours with the temperature raised by dT (degree-days recomputed)."""
    s = SPECS[spec]; t, h, c = s["temp"]
    X = df[s["feats"]].copy()
    X[t] = X[t] + dT
    X[h] = np.maximum(HDD_BASE - X[t], 0.0)
    X[c] = np.maximum(X[t] - CDD_BASE, 0.0)
    for name, (static, f) in DERIVED.items():
        if name in X.columns:
            X[name] = df[static].to_numpy() * f(X[t].to_numpy())
    return predict(bst, X, s["feats"])


def _cyc(v, p):
    return np.sin(2 * np.pi * v / p), np.cos(2 * np.pi * v / p)


def calendar_rows(names, months=range(12), hours=(3, 9, 15, 21), dows=(2, 6)):
    """Calendar feature rows for every (month, hour, day-of-week) combination.

    Months are season-aligned and hours are local for the *_local names; for the UTC names
    the same values are used, which is exact once averaged over a balanced grid.
    """
    rows = []
    for mo in months:
        for ho in hours:
            for dw in dows:
                v = {"dow_sin": _cyc(dw, 7)[0], "dow_cos": _cyc(dw, 7)[1], "is_weekend": float(dw >= 5),
                     "is_holiday": 0.0, "days_to_next_holiday": 7.0, "days_from_prev_holiday": 7.0}
                for pre in ("hour", "lhour"):
                    v[pre + "_sin"], v[pre + "_cos"] = _cyc(ho, 24)
                for pre in ("month", "smonth"):
                    v[pre + "_sin"], v[pre + "_cos"] = _cyc(mo, 12)
                rows.append([v[n] for n in names])
    return np.asarray(rows, np.float32)


def response_surface(bst, spec, statics, by_month=False, batch=400, n=None):
    """Predicted demand (10**prediction) as a function of temperature for each row of statics.

    by_month=False  S[n, NT]       averaged over the whole balanced calendar
    by_month=True   S[n, 12, NT]   averaged over hours and day types within each month
    statics: dict of 1-D arrays keyed by the specification's static features.
    """
    s = SPECS[spec]
    if not s["statics"]:                    # one response for every place: evaluate once, repeat
        one = response_surface_rows(bst, s, {}, 1, by_month)
        return np.repeat(one, n or 1, axis=0)
    n = len(next(iter(statics.values())))
    out = np.empty((n, 12, NT) if by_month else (n, NT), np.float32)
    for a in range(0, n, batch):
        b = min(a + batch, n)
        out[a:b] = response_surface_rows(bst, s, {k: statics[k][a:b] for k in s["statics"]}, b - a, by_month)
    return out


def response_surface_rows(bst, s, statics, nb, by_month):
    feats = s["feats"]; tcol, hcol, ccol = s["temp"]
    calnames = [f for f in feats if f not in s["statics"] + s["temp"] + list(DERIVED)]
    cal = calendar_rows(calnames); ncal = len(cal)                    # 12 months x 8
    fi = {f: i for i, f in enumerate(feats)}
    rep = NT * ncal
    X = np.empty((nb * rep, len(feats)), np.float32)
    for k in s["statics"]:
        if k in fi:
            X[:, fi[k]] = np.repeat(np.asarray(statics[k], np.float32), rep)
    X[:, fi[tcol]] = np.tile(np.repeat(BC.astype(np.float32), ncal), nb)
    X[:, fi[hcol]] = np.maximum(HDD_BASE - X[:, fi[tcol]], 0.0)
    X[:, fi[ccol]] = np.maximum(X[:, fi[tcol]] - CDD_BASE, 0.0)
    for name, (static, f) in DERIVED.items():
        if name in fi:
            X[:, fi[name]] = np.repeat(np.asarray(statics[static], np.float32), rep) * f(X[:, fi[tcol]])
    ctile = np.tile(cal, (nb * NT, 1))
    for j, nm in enumerate(calnames):
        X[:, fi[nm]] = ctile[:, j]
    _utc_from_local(X, fi)
    if isinstance(bst, TwoRound):                   # the level of a place is evaluated once
        lf = bst.level_feats
        L = np.stack([np.asarray(statics[k], np.float32) for k in lf], axis=1)
        base = np.repeat(bst.level.predict(xgb.DMatrix(L, feature_names=lf)), rep)
        pred = bst.hourly.predict(xgb.DMatrix(X, feature_names=feats, base_margin=base))
    else:
        pred = predict(bst, X, feats)
    p = (10.0 ** pred).reshape(nb, NT, 12, ncal // 12)                     # month is the outer calendar loop
    return p.mean(axis=3).transpose(0, 2, 1) if by_month else p.mean(axis=(2, 3))


def _utc_from_local(X, fi):
    """With both calendars among the predictors, the UTC one follows from the local one and the place."""
    if "lhour_sin" in fi and "hour_sin" in fi:
        a = np.deg2rad(X[:, fi["longitude"]]); ls, lc = X[:, fi["lhour_sin"]], X[:, fi["lhour_cos"]]
        X[:, fi["hour_sin"]] = ls * np.cos(a) - lc * np.sin(a)
        X[:, fi["hour_cos"]] = lc * np.cos(a) + ls * np.sin(a)
        sgn = np.where(X[:, fi["latitude"]] < 0, -1.0, 1.0).astype(np.float32)
        X[:, fi["month_sin"]] = X[:, fi["smonth_sin"]] * sgn
        X[:, fi["month_cos"]] = X[:, fi["smonth_cos"]] * sgn


def response_blocks(models, spec, statics, need, rows=3_600_000):
    """The by-month response of several models of one specification, block of places by block.

    statics   dict of 1-D arrays (one value per place) keyed by the specification's static features
    need      bool [n, 12, NT]: the month-temperature pairs to evaluate; the rest is returned as 0
    Yields (a, b, [S of each model, float32 (b - a, 12, NT)]). The predictor matrix of a block is
    built once and shared by the models; the values equal response_surface(..., by_month=True).
    """
    s = SPECS[spec]; feats = s["feats"]; fi = {f: i for i, f in enumerate(feats)}
    tcol, hcol, ccol = s["temp"]
    calnames = [f for f in feats if f not in s["statics"] + s["temp"] + list(DERIVED)]
    cal = calendar_rows(calnames).reshape(12, -1, len(calnames)); nper = cal.shape[1]     # month is the outer loop
    n = len(need); cum = np.cumsum(need.reshape(n, -1).sum(axis=1, dtype=np.int64) * nper)
    a = 0
    while a < n:
        b = max(a + 1, int(np.searchsorted(cum, (cum[a - 1] if a else 0) + rows, side="right")))
        ci, mi, ti = np.nonzero(need[a:b]); npair = len(ci)
        out = [np.zeros((b - a, 12, NT), np.float32) for _ in models]
        if npair:
            X = np.empty((npair * nper, len(feats)), np.float32)
            for k in s["statics"]:
                if k in fi:
                    X[:, fi[k]] = np.repeat(np.asarray(statics[k][a:b], np.float32)[ci], nper)
            X[:, fi[tcol]] = np.repeat(BC.astype(np.float32)[ti], nper)
            X[:, fi[hcol]] = np.maximum(HDD_BASE - X[:, fi[tcol]], 0.0)
            X[:, fi[ccol]] = np.maximum(X[:, fi[tcol]] - CDD_BASE, 0.0)
            for name, (static, f) in DERIVED.items():
                if name in fi:
                    X[:, fi[name]] = np.repeat(np.asarray(statics[static][a:b], np.float32)[ci], nper) * f(X[:, fi[tcol]])
            cb = cal[mi]                                                               # (pairs, nper, calendar features)
            for j, nm in enumerate(calnames):
                X[:, fi[nm]] = cb[:, :, j].ravel()
            _utc_from_local(X, fi)
            d = xgb.DMatrix(X, feature_names=feats)
            for S, bst in zip(out, models):
                if isinstance(bst, TwoRound):                                          # the level of a place is evaluated once
                    lf = bst.level_feats
                    L = np.stack([np.asarray(statics[k][a:b], np.float32) for k in lf], axis=1)
                    d.set_base_margin(np.repeat(bst.level.predict(xgb.DMatrix(L, feature_names=lf))[ci], nper))
                    pred = bst.hourly.predict(d)
                else:
                    pred = bst.predict(d)
                S[ci, mi, ti] = (10.0 ** pred).reshape(npair, nper).mean(axis=1)
        yield a, b, out
        a = b


def eta_from_surface(S, H):
    """Sensitivity (% per deg C) and its cooling and heating parts.

    S, H have the same shape, (..., NT): response and time spent per temperature bin (any
    leading axes after the first are summed, e.g. months).
      eta = [sum H(T) S(T+1) - sum H(T) S(T)] / sum H(T) S(T) * 100
    """
    Sh = np.concatenate([S[..., 1:], S[..., -1:]], axis=-1)
    ax = tuple(range(1, S.ndim))
    base = (H * S).sum(axis=ax)
    dS = Sh - S
    with np.errstate(divide="ignore", invalid="ignore"):
        eta = np.where(base > 0, (H * dS).sum(axis=ax) / base * 100.0, np.nan)
        cool = np.where(base > 0, (H * np.maximum(dS, 0)).sum(axis=ax) / base * 100.0, np.nan)
        heat = np.where(base > 0, (H * np.minimum(dS, 0)).sum(axis=ax) / base * 100.0, np.nan)
    return eta, cool, heat


# ── sensitivity of one hourly series by fixed-effects regression ──
MIN_CELL, MIN_BINS, MIN_HOURS = 5, 6, 4000


def _onehot(v):
    u, col = np.unique(v, return_inverse=True)
    M = np.zeros((len(v), len(u)), np.float32); M[np.arange(len(v)), col] = 1.0
    return M, u


def fe_surface(logd, t, hour, dow, month, year):
    """log demand ~ temperature bin + hour + day of week + month + year.

    Returns S on BC (NaN where the series has no data) and the hours per bin.
    """
    tb = np.digitize(t, BINS) - 1
    v = (tb >= 0) & (tb < NT) & np.isfinite(logd) & np.isfinite(t)
    tb, logd, hour, dow, month, year = (a[v] for a in (tb, logd, hour, dow, month, year))
    if len(logd) < MIN_HOURS:
        return None, None
    cnt = np.bincount(tb, minlength=NT)
    good = np.where(cnt >= MIN_CELL)[0]
    if good.size < MIN_BINS:
        return None, None
    keep = np.isin(tb, good)
    tb, logd, hour, dow, month, year = (a[keep] for a in (tb, logd, hour, dow, month, year))
    Xtb, utb = _onehot(tb)
    blocks = [Xtb]
    for fac in (hour, dow, month, year):
        M, u = _onehot(fac)
        if u.size > 1:
            blocks.append(M[:, 1:])
    beta, *_ = np.linalg.lstsq(np.hstack(blocks), logd.astype(np.float64), rcond=None)
    b = beta[:Xtb.shape[1]]
    S = np.full(NT, np.nan); F = np.zeros(NT)
    S[utb] = np.exp(b - b.mean()); F[utb] = np.bincount(tb, minlength=NT)[utb]
    return S, F


def eta_matched(S, F):
    """Sensitivity of one response S over the temperature range the series populates."""
    ok = np.isfinite(S) & (F > 0)
    if ok.sum() < MIN_BINS:
        return np.nan
    idx = np.where(ok)[0]; lo, hi = idx.min(), idx.max()
    s = S[lo:hi + 1].copy(); f = F[lo:hi + 1]
    s = np.where(np.isfinite(s), s, np.nanmean(s))
    sh = np.concatenate([s[1:], s[-1:]])
    base = (f * s).sum()
    return float(((f * sh).sum() - base) / base * 100.0) if base > 0 else np.nan
