# Demand Forecast & Bus Distribution

Hourly demand forecast per node and spatial distribution of each node's demand among its buses. There is no separate menu entry; the panel is reached in two ways:

- **Grid Builder, Step 3 "Demand"** (see [Grid Builder](grid-auto-mapping.md)): works on every node of the active system.
- **Node attributes panel > Forecast**: opens the same panel in a dialog ("Demand forecast — *node*") scoped to that one node. The study region is the bounding box of the node's load buses (padded to at least about 0.3°), or a 0.5° box around the node centroid if it has no buses. The Bus Distribution section is shown only if the node has at least two load buses.

The panel has four sections: Demand Forecast, Forecast Results, Validate against observed demand (optional) and Bus Distribution. **Apply Demand & Fractions** writes the forecast to the nodes and the fractions to the buses; no other elements are created or removed.


---


### 1. Demand Forecast

| Input | Default | Description |
|-------|---------|-------------|
| Country | -- | Country used for the World Bank indicators and the national total. **Auto-detect** finds it from the domain centroid (Nominatim). |
| Base year | 2025 | First forecast year (2000 -- 2100). |
| Horizon (years) | 25 | Number of forecast years (1 -- 50). |
| ML engine | Auto (XGBoost) | **Auto (XGBoost)**, **XGBoost**, or **Archetype (no ML)**. |
| National demand (GWh) | 0 = Auto-estimate | Base-year demand total. With the XGBoost engine, a value above 0 scales the base-year total of all nodes in scope to it, and 0 keeps the model's own magnitude; with the Archetype engine, 0 estimates the total from World Bank kWh per capita × population × electricity access. The value is applied to the nodes in scope, so for a domain smaller than the country enter the demand of the domain, not of the country. |
| GDP scenario | SSP2 | Shared Socioeconomic Pathway (SSP1 -- SSP5) for the GDP and population trajectory. It also selects the climate scenario used for future temperatures. |
| Elasticity | 0.80 | Demand-GDP elasticity (0 -- 2). |
| Efficiency | 0.005 /yr | Annual efficiency improvement (0 -- 0.05). |

Click **Fetch and Forecast**. The button:

1. Fetches World Bank indicators (GDP per capita, population, urbanization, electricity access, electricity consumption per capita) and the hourly temperature of the year before the base year at the domain centre (ERA5, from the Open-Meteo archive). The fetch is skipped when data for the same country, domain and weather year is already loaded, so changing forecast parameters and running again does not hit the network.
2. Runs the forecast on a background thread. The XGBoost engine predicts hourly demand density (MW per km² of land) in every 0.25° cell from the cell's population and GDP density, its long-run climate and how electricity is used there, the hour's temperature and the calendar, multiplies it by the cell's land in the domain, and sums the cells of each node, so each node gets its own spatially resolved profile. Every cell that overlaps the domain is used, not only those whose centre is inside it: a cell the domain boundary crosses counts with the share of its land inside the domain, so a coastal cell centred at sea still counts its land whole, and a cell on a land border is split between the countries on either side. The model and how its inputs are built are described in [Demand Density Model](../contributing/demand-density-model.md).

The XGBoost engine computes on UTC hours, the clock its training data were on: the hour of the day and the month are UTC, the weekday and holidays are those of the local date, and local solar time comes from the longitude. The temperature that goes with an hour is that instant's (ERA5 comes in UTC). Each node's result is then put on the node's local civil time (its time zone from its coordinates, with daylight saving time). The Archetype engine, and the simpler model the XGBoost engine falls back to without its density model, work in local civil time directly, with ERA5 put on each node's local time. 29 February is dropped from a leap weather year, and the calendar follows the real date of each hour. The forecast's hours are therefore each node's local civil time, whatever the engine; the saved demand is then put on the case's [model clock](../user-guide/configuration.md#model-clock).

The trained models ship with ESFEX, and with the density model the inputs of every 0.25° cell that do not change over the forecast: its population in 2020, its land area, its long-run climate and its electricity-use context. The gridded GDP (0.25°, historical and SSP) is downloaded the first time a forecast needs it, from the ESFEX data release, into `ESFEX_DATA_ROOT`: one file per year and SSP used, about 1 MB each. Later forecasts read it from disk. If a download fails, the forecast still runs, but the summary names the missing files and the cells without GDP get no demand. If the density model cannot be loaded, the forecast falls back to a simpler method, with no spatial cells, and the summary says so: first ESFEX's national demand model (XGBoost, shipped with ESFEX), which predicts the hourly shape of each node's demand, then, if that fails too, the Archetype method. A dataset of your own under `ESFEX_DATA_ROOT` is used as it is, and a complete copy of the density model in its `data/models` folder replaces the shipped one.

The national demand model gets, for each node, the features it was trained on (see [`esfex train-demand-model`](../user-guide/cli.md#esfex-train-demand-model)): GDP per capita and population density sampled at the node from the same rasters as its training data (the historical GDP raster and GPW population density, downloaded on first use; a base year after 2020 samples the 2020 rasters), moved over the forecast years by the GDP and population growth rates; the GHSL settlement class when that raster is in `ESFEX_DATA_ROOT` (it is not downloaded; without it every node counts as rural, class 10, as training rows without it did); the country's code in the model and its public holidays; the node's coordinates; the hour's temperature with its day's maximum, minimum and range and the 1-, 7- and 30-day means; and degree hours on the model's own 18 °C and 24 °C bases. A node whose GDP or population cannot be sampled (open sea, a failed download), or whose temperature series has gaps, stops the national model, and the forecast falls back to the Archetype method. For a country the model was not trained on, it uses a country code no training row had and says so in the log.

Data sources: Wang, T. and Sun, F., *Global gridded GDP data set consistent with the shared socioeconomic pathways*, Scientific Data 9, 221 (2022), [doi:10.1038/s41597-022-01300-x](https://doi.org/10.1038/s41597-022-01300-x) (CC BY 4.0, published unchanged). The cell inputs shipped with the density model are the article's: WorldPop 2020 population counts (CC BY 4.0) summed per cell, Natural Earth 10m land, NEX-GDDP-CMIP6 daily temperature 2025-2029 (GFDL-ESM4, IPSL-CM6A-LR, MPI-ESM1-2-HR) for the climate normals, and the UN Energy Balances, Statistics Canada table 25-10-0029 and the EIA State Energy Data System for the electricity-use context. The licence, attribution and citation of each are in [Data Sources and Licences](../reference/data-sources.md).

The temperature of each forecast year comes from CMIP6 daily maximum and minimum temperature, turned into an hourly curve with its minimum at 06:00 and its maximum at 14:00 local time (a leap year loses 29 February), read from the public Google Cloud (Pangeo) Zarr store and cached locally; years CMIP6 does not cover use the ERA5 weather year. The experiment follows the selected SSP (SSP1: ssp126, SSP2: ssp245, SSP3: ssp370, SSP4: ssp460, SSP5: ssp585). The default model is MPI-ESM1-2-HR; set the environment variable `ESFEX_CMIP6_MODEL` to use another. If the Zarr store cannot be used, the Open-Meteo climate API is the fallback; it ignores the SSP choice and is rate-limited.


### 2. Forecast Results

A table with one row per node: **Node**, **Peak (MW)**, **Annual (GWh)** and **Load Factor**, followed by a summary line. **View Demand…** opens a chart of the forecast profiles.


### 3. Validate against observed demand (optional)

Compare the forecast with your own measured demand:

1. Select a node and click **Load observed CSV…**. The file (CSV or Excel, no header) holds one hourly series in its first column.
2. The metrics table compares the series with the base-year forecast of that node: **MAPE %**, **RMSE (MW)**, **Peak err %**, **Energy err %** and **Corr** (correlation).
3. **Overlay** plots the observed series on the forecast chart.
4. **Apply month×hour correction** scales the forecast so that the base year matches the observation in each (month, hour) bin. The same factors are applied to every year, so the growth trajectory is kept. Unchecking it restores the raw forecast.

**Clear all** removes the loaded series.


### 4. Bus Distribution

Splits each node's demand among its buses by setting each bus's `demand_fraction`. Only nodes with two or more buses need a split. The eligible buses are the node's `load`/`mixed` buses; if a node has fewer than two of those, all of its buses are used.

Select the method in the **Method** combo and click **Distribute demand**:

**Spatial demand (default)**

Uses the forecast's own per-cell demand, so it needs a completed forecast.

1. Each forecast cell (clipped to the study domain) is assigned to its nearest node, which keeps the per-node totals of the forecast.
2. Within a node with several buses, the cells' annual demand is allocated to the buses by a capacitated minimum-cost transport problem: minimize the total cell-to-bus distance, serve every cell, and keep each bus below its capacity. A bus's capacity is the sum of the MVA ratings of its transformers; buses without transformers get a capacity proportional to their voltage. Demand spills to the next bus once the nearest one is full.
3. Single-bus nodes, which is every node after a level-2+ simplification in the Grid Builder, get fraction 1.0.

**Building footprint proxy**

Select one of the footprint databases under "Building footprint proxy":

| Source | Description |
|--------|-------------|
| **Microsoft ML** | Machine-learning footprints from satellite imagery. Spatially tiled, so downloads are fast. |
| **Overture Maps** | Global building footprints. The query scans the global dataset and is slower. |
| **Google Open Buildings** | ML footprints, strongest in Africa, South and Southeast Asia and Latin America. When the Google source is unavailable, the fetch falls back to Overture Maps automatically. |

This method needs only the domain, not a forecast. The buildings are classified with an editable rules table:

| Column | Description |
|--------|-------------|
| Type Name | Type label (e.g. "Residential") |
| Area Min (m²) | Minimum footprint area |
| Area Max (m²) | Maximum footprint area (∞ for no upper bound) |
| Weight/m² | Demand weight per square meter of footprint |

Default rules:

| Type | Area range (m²) | Weight/m² |
|------|-----------------|-----------|
| Residential | 30 -- 300 | 0.05 |
| Commercial | 300 -- 2,000 | 0.08 |
| Industrial | 2,000+ | 0.12 |

Use **Add Rule** and **Remove Rule** to edit the table. Rules are evaluated in order and the first match wins; buildings matching no rule get the **Fallback weight/m²** (default 0.03). Each building's weight is

\[
w_b = A_b \times d_r \tag{DD-1}
\]

where \(A_b\) is the footprint area and \(d_r\) the weight per m² of the matching rule. Each building belongs to the node of its nearest bus (any bus of the system), and within that node it is assigned to the nearest eligible bus. A bus fraction is the bus's share of the weight of its node's buildings; a node with no buildings is split equally:

\[
f_i = \frac{\sum_{b \to i} w_b}{\sum_{b \in n} w_b} \tag{DD-2}
\]

Weights are relative: scaling all of them by the same factor does not change the fractions.

**Results and Apply**

The results table lists **Node**, **Bus**, **Buildings** (building count for the footprint method, MWh/yr served for spatial demand), **Old Fraction** and **New Fraction**.

**Apply Demand & Fractions**:

- writes each node's forecast hourly series (all forecast years) to `demand_<system>_<node>.csv` under the project's `demand/` folder (the folder the project was loaded from; `./demand/` when the project has no file yet) and attaches it to the node, so it is exported as the system's demand path. The file name carries the system because the systems of a project share the folder: without it, two systems with a node of the same name (`Node 18`) wrote the same file and both kept the demand of the one forecast last. Earlier versions also wrote to the directory the Studio was started from, whatever project was open. The forecast is in each node's local civil time; the files are converted to the [model clock](../user-guide/configuration.md#model-clock), from each node's own time zone with its daylight saving time. When the clock is still `auto`, it becomes the standard time of the zone with the most forecast demand and is recorded in the project's global settings; the chart under **View Demand…** shows the forecast in local time;
- sets each bus's `demand_fraction`. A bus with role `connection` that receives a non-zero share is promoted to `load`; a `connection` bus with a zero share is left unchanged.

Writing the files takes a while on a large case (about 45 s for 136 nodes and 25 forecast years). It runs in the background: the bar beside the button shows the nodes written, and the window stays responsive. Back and Close wait until it ends; Cancel stops it, and nothing is applied. Earlier versions wrote the files on the interface thread, which froze the window with no progress shown.

**Tips:**

- Prefer spatial demand. It uses the forecast's own spatial detail and respects substation capacities.
- With the footprint method, choose a domain that covers all buses of the nodes with some margin; buildings outside it are not counted. Buildings are not limited to a node's own territory: in a multi-node domain, buildings in other nodes' areas also count towards each node's split, so the spatial demand method is the better choice there.
- After applying, you can run the distribution again with other settings; the Old Fraction column then shows the values just applied.


---


## Scripting

The footprint classification is available as plain functions:

```python
import geopandas as gpd
from esfex.visualization.workflows.demand_analysis import (
    BuildingTypeRule,
    DEFAULT_RULES,
    classify_buildings,
    compute_classification_summary,
)

# Building footprints with a 'footprint_area_m2' column
buildings = gpd.read_file("buildings.geojson")
if "footprint_area_m2" not in buildings:
    buildings["footprint_area_m2"] = buildings.to_crs(
        buildings.estimate_utm_crs()).geometry.area

classified = classify_buildings(buildings, DEFAULT_RULES, fallback_weight_per_m2=0.03)
print(compute_classification_summary(classified))

custom_rules = [
    BuildingTypeRule(name="Small Residential", area_min_m2=20, area_max_m2=150,
                     weight_per_m2=0.04),
    BuildingTypeRule(name="Commercial", area_min_m2=150, area_max_m2=2000,
                     weight_per_m2=0.10),
    BuildingTypeRule(name="Industrial", area_min_m2=2000, area_max_m2=float("inf"),
                     weight_per_m2=0.15),
]
classified_custom = classify_buildings(buildings, custom_rules, fallback_weight_per_m2=0.02)
```

The capacitated allocation used by the spatial method is `esfex.models.demand_density_ml.allocate_demand_capacitated(cell_lats, cell_lons, cell_demand, bus_lats, bus_lons, bus_cap)`, which returns the demand served by each bus.
