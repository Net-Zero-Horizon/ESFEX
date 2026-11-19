# Data Sources and Licences

ESFEX is released under the [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0). Some files shipped with it
contain or are derived from third-party data, and Studio bundles a few third-party
JavaScript libraries. This page lists them, with the licence of each and the attribution
and citation its terms ask for. The same list is in `DATA_SOURCES.md` at the top of the
repository, which is also installed with the package (in its `.dist-info/licenses/`
folder) together with the licence texts of `LICENSES/`. `REUSE.toml` records the licence
of every file in machine-readable form ([REUSE](https://reuse.software)).

How the demand density model uses these data is described in
[Demand Density Model](../contributing/demand-density-model.md).

## Demand density model (`src/esfex/assets/models/`)

The demand density model is the final model of the article *Global map of electricity
demand sensitivity to warming learned from observed hourly load* (Nature Communications,
revision R1). `scripts/demand_model/README.md` in the repository describes how
each file was produced.

| File | Content | Sources | Licence |
|---|---|---|---|
| `demand_density_v2.ubj`, `demand_density_v2.level.ubj` | the fitted XGBoost boosters | the ESFEX authors | Apache-2.0 |
| `demand_density_v2.meta.json` | feature order, training ranges, provenance, cross-validation scores | the ESFEX authors | Apache-2.0 |
| `demand_model.ubj` | the legacy country-level demand model | the ESFEX authors | Apache-2.0 |
| `demand_density_v2.grids.npz` | cell inputs on the 0.25° grid: `t_mean`, `t_range` | NEX-GDDP-CMIP6 | CC0-1.0 |
| | `pop_2020` | WorldPop | CC-BY-4.0 |
| | `land_km2` | Natural Earth | public domain |
| | `hh_elec_share`, `bld_share_elec` | UN Energy Balances (UNdata), Statistics Canada, EIA SEDS | UNdata terms, Statistics Canada Open Licence, US public domain |
| `demand_density_v2.countries.csv` | `hh_elec_share`, `bld_share_elec` by country | UN Energy Balances (UNdata), Statistics Canada, EIA SEDS | as above |

The aggregation of these data to the 0.25° grid and to countries is the ESFEX authors' work
(Apache-2.0); the upstream terms below apply to the data themselves.

The boosters were trained on hourly electricity load from many system operators and data
platforms, together with the gridded predictors above. The training data are not shipped
with ESFEX; their sources and terms are given in the Data availability statement of the
article. `TODO(licence):` add the article's DOI once it is published.

### NEX-GDDP-CMIP6 (`t_mean`, `t_range`)

Mean and 5-95 % spread of daily mean near-surface air temperature of each cell, from three
models (GFDL-ESM4, IPSL-CM6A-LR, MPI-ESM1-2-HR) under SSP1-2.6, SSP2-4.5, SSP3-7.0 and
SSP5-8.5, 2025-2029 pooled.

- Licence: since September 2022 all NEX-GDDP-CMIP6 outputs are under a blanket
  Creative Commons Zero licence (CC0-1.0); earlier releases carried CC BY-SA 4.0 per model.
- Data: NASA Earth Exchange Global Daily Downscaled Projections (NEX-GDDP-CMIP6), accessed in
  2026 from https://registry.opendata.aws/nex-gddp-cmip6. Documentation:
  https://doi.org/10.7917/OFSG3345
- Cite: Thrasher, B., Wang, W., Michaelis, A., Melton, F., Lee, T. & Nemani, R. NASA Global
  Daily Downscaled Projections, CMIP6. *Scientific Data* 9, 262 (2022).
  https://doi.org/10.1038/s41597-022-01393-4
- CMIP6: Eyring, V. et al. Overview of the Coupled Model Intercomparison Project Phase 6
  (CMIP6) experimental design and organization. *Geoscientific Model Development* 9,
  1937-1958 (2016). https://doi.org/10.5194/gmd-9-1937-2016. We acknowledge the World Climate
  Research Programme, which coordinated and promoted CMIP6, and the climate modelling groups
  for producing and making available their model output.

### WorldPop (`pop_2020`)

WorldPop Population Counts, Unconstrained global mosaics 2000-2020 (1 km resolution),
"World 1000m", year 2020 (`ppp_2020_1km_Aggregated.tif`): people per 30 arc-second pixel,
aggregated from the 100 m country datasets (top-down, Random Forest), not UN-adjusted.
ESFEX ships the counts summed per 0.25° cell.

- Licence: CC BY 4.0 (CC-BY-4.0).
- Cite: WorldPop (www.worldpop.org - School of Geography and Environmental Science,
  University of Southampton; Department of Geography and Geosciences, University of
  Louisville; Departement de Geographie, Universite de Namur) and Center for International
  Earth Science Information Network (CIESIN), Columbia University (2018). Global High
  Resolution Population Denominators Project - Funded by The Bill and Melinda Gates
  Foundation (OPP1134076). https://dx.doi.org/10.5258/SOTON/WP00647

### Natural Earth (`land_km2`, Studio's country outlines)

Natural Earth 1:10m Cultural Vectors, Admin 0 - Countries, rasterized at 0.025° and summed
per 0.25° cell for `land_km2`. Studio's offline base map
(`src/esfex/visualization/resources/world_countries.geojson`) holds Natural Earth admin-0
country outlines and names.

- Licence: public domain (`LicenseRef-NaturalEarth-PublicDomain`).
- Credit (optional): Made with Natural Earth. Free vector and raster map data @
  naturalearthdata.com.

### UN Energy Balances (`hh_elec_share`, `bld_share_elec`)

Electricity in household final energy use, and households plus commerce and public
services in final electricity consumption, by country, from the UN Statistics Division's
energy balances on the UNdata portal: SDMX dataflow `DF_UNData_EnergyBalance` version 1.0
(not the printed *Energy Balances* publication), years 2014-2022 retrieved on 13 November
2026. Products: coal, crude oil, oil products, natural gas, biofuels and waste,
electricity, heat and total energy; flows: households (transaction 1231), commerce and
public services (1235) and total final consumption. ESFEX's values are 2015-2021 means.

- Licence: UNdata terms of use (`LicenseRef-UNdata`): free to copy and redistribute,
  provided UNdata is cited as the reference (https://data.un.org/Host.aspx?Content=UNdataUse).
- Cite: United Nations Statistics Division, Energy Balances (SDMX dataflow
  DF_UNData_EnergyBalance, version 1.0), UNdata, https://data.un.org, accessed
  13 November 2026.

### Statistics Canada (Canadian provinces)

- Licence: Statistics Canada Open Licence (`LicenseRef-StatCan-Open-Licence`).
- Notice: Adapted from Statistics Canada, Table 25-10-0029-01 Supply and demand of primary
  and secondary energy in terajoules, annual, reference period 2015 to 2021
  (https://doi.org/10.25318/2510002901-eng). This does not constitute an endorsement by
  Statistics Canada of this product.

### U.S. Energy Information Administration (US states)

- Licence: public domain as a work of the U.S. Government
  (`LicenseRef-US-Government-Public-Domain`).
- Source: U.S. Energy Information Administration, State Energy Data System (SEDS),
  consumption estimates (`use_all_btu.csv`), retrieved 13 November 2026.

## Data downloaded on demand from the ESFEX data release

The Demand Forecast workflow and the Grid Builder download these files from the `data-v1`
release of the ESFEX repository when first needed (listed in
`src/esfex/assets/data_assets.json`). They are redistributed unchanged except for tiling.

- **GPW v4 population density** (`gpw_v4_popdens_*`): Center for International Earth Science
  Information Network (CIESIN), Columbia University. Gridded Population of the World,
  Version 4 (GPW v4): Population Density, Revision 11. NASA Socioeconomic Data and
  Applications Center (SEDAC), 2018. https://doi.org/10.7927/H49C6VHW. CC BY 4.0. Cut into
  30-degree tiles; values unchanged.
- **Gridded GDP** (`GDP*.tif`): Wang, T. & Sun, F. Global gridded GDP data set consistent with
  the shared socioeconomic pathways. *Scientific Data* 9, 221 (2022).
  https://doi.org/10.1038/s41597-022-01300-x; data: https://doi.org/10.6084/m9.figshare.16741048.
  CC BY 4.0. Values unchanged.

Other data that ESFEX fetches at run time from public services (weather archives, map tiles,
OpenStreetMap, statistics and system-operator portals) are not shipped with ESFEX; their
providers' terms apply to the data you download. Studio's map tiles carry the
providers' attribution on the map (for OpenStreetMap: © OpenStreetMap contributors, data under
the Open Database License).

## Third-party JavaScript libraries bundled with Studio

In `src/esfex/visualization/resources/`:

| File(s) | Library | Copyright | Licence |
|---|---|---|---|
| `d3.v7.min.js` | D3 7.9.0 | 2010-2023 Mike Bostock | ISC |
| `elk.bundled.js` | elkjs (Eclipse Layout Kernel) | 2017 Kiel University and others | EPL-2.0 |
| `leaflet.js`, `leaflet.css`, `images/layers*.png`, `images/marker-*.png` | Leaflet 1.9.4 | 2010-2023 Vladimir Agafonkin, 2010-2011 CloudMade | BSD-2-Clause |
| `leaflet.draw.js`, `leaflet.draw.css`, `images/spritesheet*` | Leaflet.draw 1.0.4 | 2012-2017 Jacob Toye, Jon West, Smartrak, Leaflet | MIT |
| `plotly.min.js` | plotly.js 2.35.2 (bundled dependencies in `plotly.min.js.LICENSE.txt`) | 2012-2024 Plotly, Inc., and the dependencies' authors | MIT AND BSD-3-Clause |
| `xterm.js`, `xterm.css`, `xterm-addon-fit.js` | xterm.js and its fit add-on | 2014 The xterm.js authors, 2012-2013 Christopher Jeffrey | MIT |

## Documentation

`docs/assets/studio-screenshot.png` shows Studio over an OpenStreetMap base map: map data ©
OpenStreetMap contributors, available under the Open Database License
(https://www.openstreetmap.org/copyright).
