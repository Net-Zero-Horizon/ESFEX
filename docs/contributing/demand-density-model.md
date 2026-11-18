# Demand Density Model

The Grid Builder and the Demand Forecast workflow predict hourly demand per 0.25° cell with an XGBoost model of demand density, `log10(MW/km²)` of land. The model shipped with ESFEX (`src/esfex/assets/models/demand_density_v2.*`) is the final model of the article *Global map of electricity demand sensitivity to warming learned from observed hourly load* (Nature Communications, revision R1; specification `level_geo_two_shallow`). It was fitted by the article's pipeline and is shipped unchanged; this page describes it and how ESFEX builds its inputs. Where each shipped file comes from, and the script that produces them, is in `scripts/demand_model/README.md`.

## The model

It was fitted on 2026-11-17 on 9,041,858 hours of hourly load: 1,033 series-years of 166 series (national, provincial, state and zonal) in 83 countries, every series moved onto UTC and checked against independent UTC records by the article's clock audit. It is trained in two rounds that add up to one prediction of `log10` demand density:

1. a **level** round (300 shallow trees, depth 3) on the nine predictors that describe the place: `log_pop_density`, `log_gdp_density`, `log_gdp_per_cap`, `latitude`, `longitude`, `t_mean`, `t_range`, `hh_elec_share`, `bld_share_elec`;
2. an **hourly** round (300 trees, depth 9) on all 26 predictors, starting from the level of the first round (its base margin).

The shipped files are the two boosters (`demand_density_v2.ubj`, `demand_density_v2.level.ubj`, converted from the article's JSON with identical predictions), `demand_density_v2.meta.json` (feature order, training ranges, provenance, cross-validation scores), `demand_density_v2.grids.npz` (cell inputs on the 0.25° grid, below) and `demand_density_v2.countries.csv` (per country: national electricity-use context and the standard offsets of its time zones). ESFEX predicts on the CPU; the article fitted on a GPU.

## Predictors

ESFEX builds each predictor as the article's training table and its gridded maps do (`esfex.models.demand_density_features`, which follows the copied article code in `scripts/demand_model/article/` line by line).

| Predictor | How it is built | Article code |
|---|---|---|
| `log_pop_density`, `log_gdp_density`, `log_gdp_per_cap` | population and GDP of the cell per km² of **land**, and GDP per person. Population: WorldPop 2020 counts summed per cell (shipped grid), grown by the forecast's population trajectory; GDP: the SSP GDP raster of the forecast year (5-year steps, interpolated); land area: Natural Earth 10m land per cell (shipped grid) | `s01_population_grid`, `s02_static_grids`, `s08_sensitivity` |
| `latitude`, `longitude` | cell centre | `s08_sensitivity` |
| `temperature`, `hdd`, `cdd` | 2 m temperature of the UTC hour; degrees below 18 °C and above 24 °C | `s03b_temperature`, `s03c_dataset` |
| `hour_sin`, `hour_cos` | UTC hour of the row | `s03a_series.calendar` |
| `month_sin`, `month_cos` | month of the UTC hour of the year counted on a 365-day calendar (`MONTH_DAYS`); in a leap year the days from 31 March are read one day later, as in training | `s03a_series.calendar` |
| `dow_sin`, `dow_cos`, `is_weekend` | weekday of the local date: the UTC hour plus the standard offset of the country's time zone nearest to the longitude (no daylight saving time) | `s03a_series.calendar`, `std_offset` |
| `is_holiday`, `days_to_next_holiday`, `days_from_prev_holiday` | national holidays of the `holidays` package on that local date (distances clipped at 7 days); the day index is clipped to the year, so the local hours before 1 January carry 1 January's values | `s03a_series.holiday_features` |
| `lhour_sin`, `lhour_cos` | local solar time: the UTC hour turned by the longitude, 15° per hour | `model.prepare` |
| `smonth_sin`, `smonth_cos` | the month turned by six months where the latitude is negative | `model.prepare` |
| `t_mean`, `t_range` | long-run climate of the cell: mean, and spread between the 95th and 5th percentiles, of daily mean temperature, from the shipped grid of the article's present-day climate (NEX-GDDP-CMIP6, three climate models, four scenarios, 2025-2029). A cell without a value takes one within 1°; failing that, the daily (UTC-day) means of the node's temperature of the base year | `s08_sensitivity.normals`, `model.prepare` |
| `hh_elec_share`, `bld_share_elec` | electricity in household final energy, and households plus services in final electricity (UN energy balances 2015-2021; Statistics Canada and EIA SEDS by province and state): the cell's value from the shipped grid, else a cell within 1°, else the national value of the forecast's country; missing where no statistics exist (Taiwan, for instance), as for the training series without them | `s12_energy_context` |

`t_mean`, `t_range`, the electricity-use context and the 2020 population pattern are present-day values, kept for every forecast year, as in the article's maps: the demand trajectory comes from population, GDP and the temperature of each year. Predictors outside the training ranges (in `meta.json`) are passed as they are; a tree model is flat beyond its outermost split.

## Clock

The model's hours are UTC. For each forecast year ESFEX builds the 8,760 UTC hours of the year with 29 February skipped (`temporal.year_row_dates`); the article's year is the first 8,760 hours of the UTC calendar year, so 31 December of a leap year, which it never saw, follows the same rules. The temperature of each row is that UTC instant's: ERA5 comes in UTC; the CMIP6 curve, built on each node's local time, is moved to UTC. Each node's sum of cells is then put on the node's local civil time, like the other engines' forecasts, and the saved demand on the case's [model clock](../user-guide/configuration.md#model-clock). Going through local time costs one hour a year where the zone has daylight saving time: the hour repeated in autumn reads the one before it.

## Accuracy

The article's cross-validation (`cv/summary.csv` of its pipeline, copied into `meta.json` under `cross_validation`), on held-out data: withheld years of the same series, withheld countries (10 folds) and withheld World Bank regions. The submitted design is the model of the first submission (18 predictors, hours and months in UTC, one round) refitted on the same data and scored the same way.

| Held out | Model | R² hourly | RMSE (dex) | R² level | R² hourly shape | r of sensitivity to +1 °C |
|---|---|---|---|---|---|---|
| years | shipped | 0.984 | 0.084 | 0.963 | 0.530 | 0.943 |
| years | submitted design | 0.983 | 0.086 | 0.959 | 0.523 | 0.935 |
| countries | shipped | 0.829 | 0.272 | 0.815 | 0.441 | 0.860 |
| countries | submitted design | 0.788 | 0.303 | 0.799 | 0.292 | 0.756 |
| regions | shipped | 0.718 | 0.349 | 0.737 | 0.337 | 0.798 |
| regions | submitted design | 0.603 | 0.414 | 0.631 | 0.006 | 0.736 |

R² hourly is of hourly `log10` density; R² level of each series' mean; R² hourly shape of the hourly anomalies about that mean; the last column correlates the sensitivity of the held-out predictions with that of the observed series (fixed-effects estimate, 164 series). A place unlike any in the archive is best described by the country and region rows.

## Updating the shipped model

After a new fit of the article's model:

```bash
python scripts/demand_model/export_esfex_assets.py \
    --work /path/to/r1_revision --out src/esfex/assets/models
```

The script reads the article's work folder only; it converts the boosters to UBJ (checking that predictions are identical) and rewrites the five files. A model copied into `$ESFEX_DATA_ROOT/data/models` (`demand_density_v2.json` or `.ubj`, with `demand_density_v2.level.ubj` and `demand_density_v2.meta.json`) replaces the shipped one; an incomplete copy is ignored with a warning.

The article also fits 100 refits on countries resampled with replacement (`level_geo_two_shallow_bootNN`). They are not shipped (about 10 MB each); they would give an uncertainty band of the forecast.

## Tests

`tests/test_demand_density_v2.py` checks the predictors and the prediction against rows of the article's training table (`tests/fixtures/demand_density_v2_rows.csv`: the article's own predictors and prediction), the two rounds against the copied `model.py`, the UTC and local-solar calendar, the southern-hemisphere month, the leap-year rows, the climate normals and electricity-use lookups with their fallbacks, and the way back from UTC to local time and to the model clock.
