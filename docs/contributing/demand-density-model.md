# Retraining the Demand Density Model

The Grid Builder and the Demand Forecast workflow predict hourly demand per 0.25° cell with an XGBoost model of demand density, `log10(MW/km²)`, shipped as `src/esfex/assets/models/demand_density_hourly_xgb.ubj` with its feature sidecar `demand_density_hourly_xgb.feats.json` (feature order, target, training ranges). This page explains how that model is rebuilt. The scripts live in `scripts/demand_model/`; they are maintenance tools, not part of the installed package, and they need the hourly demand archive (about 1.3 GB) that the model was trained on.

## Features and target

Each training row is one hour of one demand series (a country, or a zone of a country) and year:

| Feature | Meaning |
|---|---|
| `log_pop_density`, `log_gdp_density`, `log_gdp_per_cap` | population and GDP of the series' footprint per km², and GDP per person |
| `temperature`, `hdd`, `cdd` | ERA5 2 m temperature of the hour (°C); heating degrees below 18 °C, cooling degrees above 24 °C |
| `hour_sin/cos`, `dow_sin/cos`, `month_sin/cos`, `is_weekend`, `is_holiday`, `days_to_next_holiday`, `days_from_prev_holiday` | calendar of the hour |
| `latitude`, `longitude` | population-weighted centroid of the footprint |

The target is `log10(demand_mw / area_km2)`.

## Clock convention

The rows follow the same convention as the forecast that uses the model:

- each row is a **local civil hour** of the series' location: the time zone of its centroid, with daylight saving time where the zone has it, 8760 hours per year with 29 February dropped;
- the demand and the temperature of a row are those of the **same instant**: ERA5 comes in UTC and is read at the row's instant;
- the calendar features describe the row's **real local date** (`esfex.utils.temporal.year_row_dates`), so in a leap year the rows after 28 February are 1 March onward, with that date's weekday and holidays.

The demand sources report on different clocks: UTC, a fixed local standard time, or local civil time. The first model (v1, the one shipped with ESFEX 0.3.0) used archive row `h` as hour `h` of the year whatever the source's clock, the UTC temperature of hour `h`, and a calendar counted from the row index. For the series recorded on local time this paired each demand hour with the temperature of another instant (8 hours away in China, 13 in New Zealand; a day more after 28 February in the leap years of the zonal series, which had dropped 29 February), and for the series recorded in UTC it gave the model UTC hours and dates instead of local ones. Over the 906 series-years kept, v1's temperature was that of another instant in half of them (3.9 hours away on average, 1.5 °C mean absolute error), its hour of the day was wrong in three quarters (1.8 hours on average) and its weekday in 13 % of the rows.

## Steps

All three scripts read their inputs read-only and write only to the output paths given.

### 1. Establish each series' clock

```bash
PYTHONPATH=src python scripts/demand_model/clock_audit.py \
    --archive /path/to/demand_dataset \
    --v1-dataset /path/to/density_demand_dataset.parquet \
    --out OUTDIR
```

`--v1-dataset` lists the series-years to audit (the v1 training set). For each one the audit decides the clock of the archive's rows:

1. the series' core clock, from `CORE_CLOCKS` in `scripts/demand_model/clocks.py` (source, trough of the mean day in local solar time, daylight-saving test);
2. for the European national series, the OPSD load in UTC: a year equal to it at some lag over 90 % of its hours is on that clock; a year that matches it at no lag is not the published load and is left out;
3. otherwise the delay of the year's mean daily profile against the series' other years: a delay under 0.7 h keeps the core clock, a clean whole-hour delay is a change of clock (a different source for that year), and anything else leaves the clock unknown and the year out.

It also tells whether each leap year kept 29 February (and lost 31 December) or dropped it, from the weekly cycle after 1 March. The result is `OUTDIR/series_clocks.csv`, one row per series-year with the clock used and the evidence, and `OUTDIR/clock_misalignment.csv`, how far v1's pairing was from the instant of each demand hour. The table of the current archive is kept in the repository as `scripts/demand_model/series_clocks.csv`, so step 2 can run without the audit.

### 2. Build the training dataset

```bash
PYTHONPATH=src python scripts/demand_model/build_density_demand_dataset.py \
    --archive /path/to/demand_dataset \
    --clocks OUTDIR/series_clocks.csv \
    --static /path/to/density_series_static.parquet \
    --out OUTDIR/density_demand_dataset.parquet
```

Every kept year of a series is placed at its instants; each local year then reads the demand and temperature of its own hours, from the neighbouring archive year where its first or last hours fall there (nothing wraps around the year). Hours with no demand or temperature at their instant are dropped. `--static` reuses the per-series table of footprint areas, population, GDP and centroids; `--rebuild-static` recomputes it from the 0.25° grids.

### 3. Train

```bash
PYTHONPATH=src python scripts/demand_model/train_density_demand_hourly.py \
    --data OUTDIR/density_demand_dataset.parquet --out-dir OUTDIR --nthread 16
```

Same features, hyper-parameters and rounds as v1: an out-of-time hold-out (the latest year) sets the number of rounds and gives the validation scores (R² of log density, of MW and of national hourly MW), eight leave-one-country-out checks follow, then the model is refitted on all rows. The script writes `demand_density_hourly_xgb.json` and `.ubj` (the same booster), the `.feats.json` sidecar with the scores and training ranges, and `_oot.ubj`, the hold-out model, for comparisons on a common test year. `--device cuda` trains on a GPU.

## Using a retrained model

Copy `demand_density_hourly_xgb.json` (or `.ubj`) and `demand_density_hourly_xgb.feats.json` into `$ESFEX_DATA_ROOT/data/models`: a model there replaces the shipped one, and is read with its own sidecar. To ship it, replace the two files in `src/esfex/assets/models`.

## Tests

`tests/test_density_model_training_clock.py` checks the pieces on synthetic data: the instant of each archive row on a fixed or civil clock, the pairing of demand and temperature on local time (no wrap, half-hour zones), the calendar of a leap year, and the audit's decision rules, leap-year test and daylight-saving test.
