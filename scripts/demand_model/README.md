# Demand density model: where the shipped files come from

The density model in `src/esfex/assets/models/demand_density_v2.*` is the final model
(`level_geo_two_shallow`) of the article *The asymmetric burden of climate change
revealed by sub-national mapping of electricity-demand sensitivity and inequity* (Nature
Communications, revision R1). It was trained by the article's own pipeline, outside this
repository; ESFEX ships its output unchanged. `docs/contributing/demand-density-model.md`
describes the model and how ESFEX builds its predictors. The licences, attributions and
citations of the third-party data in the shipped grids are in `DATA_SOURCES.md` at the
top of the repository.

## Files here

| File | What it is |
|---|---|
| `article/model.py` | the article's feature sets, two-round training (`train`, `TwoRound`) and prediction (`predict`); `prepare` derives `t_mean`, `t_range`, `lhour_*`, `smonth_*` |
| `article/s07_fit.py` | the final fit: `s07_fit.py level_geo_two_shallow` trains on every series and writes the model, its meta file and the training ranges |
| `article/s03a_series.py` | the training table's time axis and calendar (`calendar`, `holiday_features`, `std_offset`) |
| `article/s12_energy_context.py` | the electricity-use descriptors by country, US state and Canadian province |
| `export_esfex_assets.py` | turns the article's outputs into the five files ESFEX ships |

The four `article/` files are byte-identical copies of the article's code
(`demand_estimation/r1_revision/`, read 2026-11-18):

| File | Modified | MD5 |
|---|---|---|
| `model.py` | 2026-11-17 14:51 +0900 | `89b67bdb6b5762a4669ec8e80e3edefa` |
| `s07_fit.py` | 2026-11-18 11:23 +0900 (adds `first`, for further refits; the fit itself is unchanged) | `d75c1689ebe3ff6d8b68b17179944e19` |
| `s03a_series.py` | 2026-11-17 17:20 +0900 | `7a6b1979adffe0c449a7457244033597` |
| `s12_energy_context.py` | 2026-11-13 15:43 +0900 | `799639b714e040db80ec3e29e6f4d801` |

They are not run from here: they need the article's `paths.py`, its raw data (hourly
load archive, gridded population and GDP, boundaries) and, for `model.py`, nothing else.
`tests/test_demand_density_v2.py` imports `article/model.py` to check that ESFEX's
two-round prediction is the article's.

## The shipped model

Fitted on 2026-11-17 17:53 +0900 on a GPU (`device=cuda`), 300 rounds per round,
9,041,858 hours of 1,033 series-years, 166 series, 83 countries. Training series are
on UTC (the article's clock audit, `s03d_clock_audit.py`, `s03e_clock_table.py`).

| Shipped file | From (under the article's `R1_WORK_ROOT`) |
|---|---|
| `demand_density_v2.ubj` | `models/level_geo_two_shallow.json` (hourly round), JSON → UBJ |
| `demand_density_v2.level.ubj` | `models/level_geo_two_shallow.level.json` (level round), JSON → UBJ |
| `demand_density_v2.meta.json` | `models/level_geo_two_shallow.meta.json`, plus provenance and `cv/summary.csv` |
| `demand_density_v2.grids.npz` | `eta/present.npz` (`t_mean`, `t_range`), `grids/energy_context_025.npz`, `grids/countries_025.npz` (`land_km2`), `grids/population_2020_025.npz` (`pop_worldpop`) |
| `demand_density_v2.countries.csv` | `grids/energy_context.csv`; time-zone offsets from `pytz` as `s03a_series.std_offset` reads them |

The UBJ boosters give the same predictions as the JSON ones (the export script checks
it). To rebuild the files after a new fit:

```bash
python scripts/demand_model/export_esfex_assets.py \
    --work "/media/manuel/New Volume/Demand_forecasting/r1_revision" \
    --out src/esfex/assets/models
```

The script only reads `--work`.
