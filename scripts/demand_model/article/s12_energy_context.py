#!/usr/bin/env python
"""Country context from the UN energy balances: how electricity is used.

Three descriptors, averaged over 2015-2021, for every country in the UN Energy Balances
(households = transaction 1231, commerce and public services = 1235):

  hh_elec_share      electricity / all final energy used by households. In cold countries
                     household energy is mostly space heating, so this tracks how much of
                     the heating is electric (Norway 0.8, Germany 0.2).
  bld_share_elec     (households + services) / all final electricity: the part of the load
                     that is in buildings, hence exposed to the weather, rather than industry.
  log_hh_elec_pc     log10 household electricity per person (kWh): the stock of electrical
                     equipment, air conditioning included.

Source: UNSD, DF_UNData_EnergyBalance (downloaded by this script if absent). For Canadian
provinces and US states the first two descriptors are replaced by the same ratios computed
from Statistics Canada (table 25-10-0029) and the EIA State Energy Data System.
Writes grids/energy_context.csv (one row per Natural Earth country) and
grids/energy_context_025.npz (the same values on the grid, NaN where unknown).
"""
from __future__ import annotations
import subprocess
import numpy as np, pandas as pd, geopandas as gpd
from paths import RAW, WORK, GRIDS, load_population

URL = ("https://data.un.org/legacy/ws/rest/data/UNSD,DF_UNData_EnergyBalance,1.0/"
       ".B00_CL+B01_CP+B03_OP+B04_NG+B05_BW+B07_EL+B08_HT+B09_TE.B50_1231+B49_1235+B25_FEC."
       "?startPeriod=2014&endPeriod=2022")
CSV = WORK / "raw_un_energy" / "un_energy_balance_households.csv"
SHP0 = RAW / "data/gridded/shapefiles/ne_10m_admin_0/ne_10m_admin_0_countries.shp"
YEARS = range(2015, 2022)
FEATURES = ["hh_elec_share", "bld_share_elec", "log_hh_elec_pc"]
# UN energy statistics file a few countries under codes that differ from Natural Earth's
UN_FIX = {"FRA": 251, "ITA": 382, "NOR": 579, "CHE": 757, "KOS": 412}   # e.g. 251 = France-Monaco


SUBDIR = WORK / "raw_subnational"
STATCAN = "https://www150.statcan.gc.ca/n1/tbl/csv/25100029-eng.zip"
SEDS = "https://www.eia.gov/state/seds/sep_use/total/csv/use_all_btu.csv"
PROVINCE = {"Newfoundland and Labrador": "CA-NL", "Prince Edward Island": "CA-PE", "Nova Scotia": "CA-NS",
            "New Brunswick": "CA-NB", "Quebec": "CA-QC", "Ontario": "CA-ON", "Manitoba": "CA-MB",
            "Saskatchewan": "CA-SK", "Alberta": "CA-AB", "British Columbia": "CA-BC", "Yukon": "CA-YT",
            "Northwest Territories": "CA-NT", "Nunavut": "CA-NU"}
CAN_ELEC = ["Primary electricity, hydro, nuclear, and other renewables", "Secondary electricity, thermal"]


def subnational():
    """hh_elec_share and bld_share_elec for Canadian provinces (Statistics Canada table
    25-10-0029) and US states (EIA State Energy Data System), 2015-2021 means, defined as in
    the national table. Household electricity per person is left at the national value."""
    import zipfile, io
    SUBDIR.mkdir(exist_ok=True)
    zf, sf = SUBDIR / "statcan_25100029.zip", SUBDIR / "eia_seds_use_all_btu.csv"
    if not zf.exists():
        subprocess.run(["curl", "-sfL", "-o", str(zf), STATCAN], check=True)
    if not sf.exists():
        subprocess.run(["curl", "-sfL", "-o", str(sf), SEDS], check=True)
    with zipfile.ZipFile(zf) as z:
        c = pd.read_csv(io.BytesIO(z.read("25100029.csv")), low_memory=False)
    c = c[c.REF_DATE.isin(YEARS) & c.GEO.isin(PROVINCE)]
    val = lambda fuel, char: (c[c["Fuel type"].isin(fuel) & (c["Supply and demand characteristics"] == char)]
                              .groupby(["GEO", "REF_DATE"]).VALUE.sum().groupby("GEO").mean())
    res_e, res_t = val(CAN_ELEC, "Residential"), val(["Total primary and secondary energy"], "Residential")
    com_e, fin_e = val(CAN_ELEC, "Commercial and other institutional"), val(CAN_ELEC, "Energy use, final demand")
    can = pd.DataFrame({"hh_elec_share": res_e / res_t, "bld_share_elec": (res_e + com_e) / fin_e}).dropna()
    can.index = [PROVINCE[g] for g in can.index]
    e = pd.read_csv(sf)
    e = e[e.MSN.isin(["ESRCB", "TNRCB", "ESCCB", "ESTCB"]) & (e.State != "US")]
    m = e.set_index(["State", "MSN"])[[str(y) for y in YEARS]].mean(axis=1).unstack()
    usa = pd.DataFrame({"hh_elec_share": m.ESRCB / m.TNRCB, "bld_share_elec": (m.ESRCB + m.ESCCB) / m.ESTCB,
                        "res_energy": m.TNRCB, "res_elec": m.ESRCB, "com_elec": m.ESCCB, "tot_elec": m.ESTCB})
    usa.index = "US-" + usa.index
    out = pd.concat([can, usa]).rename_axis("code").reset_index()
    return out


def main() -> int:
    if not CSV.exists():
        CSV.parent.mkdir(exist_ok=True)
        subprocess.run(["curl", "-sfL", "-H", "Accept: application/vnd.sdmx.data+csv;version=1.0.0",
                        "-o", str(CSV), URL], check=True)
    d = pd.read_csv(CSV)
    d = d[d.TIME_PERIOD.isin(YEARS)]
    t = d.pivot_table(index=["REF_AREA", "TIME_PERIOD"], columns=["TRANSACTION", "COMMODITY"],
                      values="OBS_VALUE", aggfunc="sum")
    get = lambda tr, co: t[(tr, co)] if (tr, co) in t.columns else pd.Series(np.nan, t.index)
    y = pd.DataFrame({"hh_total": get("B50_1231", "B09_TE"), "hh_elec": get("B50_1231", "B07_EL"),
                      "svc_elec": get("B49_1235", "B07_EL"), "fec_elec": get("B25_FEC", "B07_EL")})
    m = y.groupby("REF_AREA").mean()                      # TJ per year
    m = m[(m.hh_total > 0) & (m.fec_elec > 0)]

    g = gpd.read_file(SHP0); g = g[g.geometry.notna()].reset_index(drop=True)
    cc = np.load(GRIDS / "countries_025.npz"); cid = cc["country_id"].astype(int)
    assert list(cc["adm0_a3"]) == g["ADM0_A3"].astype(str).tolist()
    un = pd.to_numeric(g["UN_A3"], errors="coerce").astype("Int64")
    pop = load_population()
    pop_c = np.bincount(cid[cid >= 0], pop[cid >= 0], minlength=len(g))
    rows = []
    for k in range(len(g)):
        a3 = g["ADM0_A3"][k]
        code = UN_FIX.get(a3, un[k])
        if pd.isna(code) or int(code) not in m.index or pop_c[k] <= 0:
            continue
        r = m.loc[int(code)]
        rows.append({"adm0_a3": a3, "name": g["NAME_EN"][k], "un_code": int(code),
                     "hh_elec_share": r.hh_elec / r.hh_total,
                     "bld_share_elec": np.nansum([r.hh_elec, r.svc_elec]) / r.fec_elec,
                     "log_hh_elec_pc": np.log10(max(r.hh_elec, 1e-9) * 1e12 / 3.6e6 / pop_c[k]),
                     "hh_elec_TJ": r.hh_elec, "hh_total_TJ": r.hh_total, "fec_elec_TJ": r.fec_elec})
    ctx = pd.DataFrame(rows)
    ctx.to_csv(GRIDS / "energy_context.csv", index=False)
    grids = {}
    idx = {a: i for i, a in enumerate(cc["adm0_a3"])}
    for f in FEATURES:
        v = np.full(len(g), np.nan); v[[idx[a] for a in ctx.adm0_a3]] = ctx[f].to_numpy()
        grids[f] = np.where(cid >= 0, v[np.maximum(cid, 0)], np.nan).astype(np.float32)
    np.savez_compressed(GRIDS / "energy_context_025.npz", **grids)
    # ── sub-national values where official statistics give them: Canada and the USA ──
    sub = subnational()
    sub.to_csv(GRIDS / "energy_context_subnational.csv", index=False)
    a1 = gpd.read_file(RAW / "data/gridded/shapefiles/ne_10m_admin_1/ne_10m_admin_1_states_provinces.shp")
    a1 = a1[a1.iso_3166_2.isin(sub.code) & a1.geometry.notna()].reset_index(drop=True)
    from rasterio import features
    from rasterio.transform import from_origin
    from paths import grid, NLAT, NLON, LON0, RES
    lat, _ = grid()
    tr = from_origin(LON0 - RES / 2, lat[-1] + RES / 2, RES, RES)
    aid = features.rasterize([(g_, i + 1) for i, g_ in enumerate(a1.geometry)], out_shape=(NLAT, NLON),
                             transform=tr, fill=0, all_touched=True, dtype="int16")[::-1] - 1
    sv = sub.set_index("code")
    for f in ("hh_elec_share", "bld_share_elec"):
        v = sv[f].reindex(a1.iso_3166_2).to_numpy()
        m = (aid >= 0) & np.isin(cc["adm0_a3"][np.maximum(cid, 0)], ["USA", "CAN"]) & (cid >= 0)
        grids[f] = np.where(m, v[np.maximum(aid, 0)], grids[f]).astype(np.float32)
    np.savez_compressed(GRIDS / "energy_context_025.npz", **grids)
    print(f"sub-national context: {len(sub)} states and provinces")
    print(sv.loc[["CA-QC", "CA-AB", "CA-ON", "CA-BC", "US-FL", "US-TX", "US-NY", "US-WA", "US-CA"], ["hh_elec_share", "bld_share_elec"]].round(2).T.to_string())
    covered = np.isfinite(grids[FEATURES[0]]) & (pop > 0)
    print(f"countries with context: {len(ctx)} | population covered: {100 * pop[covered].sum() / pop[cid >= 0].sum():.1f} %")
    pd.set_option("display.width", 200)
    show = ["NOR", "SWE", "FIN", "FRA", "DEU", "GBR", "POL", "RUS", "CAN", "USA", "JPN", "KOR", "CHN", "IND", "BGD",
            "THA", "PHL", "SGP", "NGA", "PAK", "BRA", "ISL", "ZAF", "SAU", "ARE", "AUS"]
    print(ctx.set_index("adm0_a3").reindex(show)[FEATURES].round(2).to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
