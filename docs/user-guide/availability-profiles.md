# Availability Profiles

## What Are Availability Profiles?

An availability profile is a time series of **capacity factors** (0.0 to 1.0): the fraction of a unit's capacity that can be used in each hour. A 100 MW solar PV generator with availability 0.85 at hour 12 can produce up to 85 MW.

- **0.0**: the resource is unavailable (solar at night, wind during calm)
- **1.0**: the unit can run at full capacity
- Values in between reflect partial availability (clouds, moderate wind)

A generator or technology without a profile has a constant availability of 1.0. That is right for dispatchable units, but not for solar or wind: a `Renewable` unit without a profile (other than a reservoir hydro unit) is therefore logged once at the start of the run (`... is Renewable but has no availability_file: it is modelled as available at full capacity in every hour (capacity factor 1.0) ...`). A profile is not limited to renewables: in operational dispatch, the profile caps the output of **any** generator that has one (for example a seasonal derating of a biomass plant).


---


## File Format

| Format | Extension | Header row |
|--------|-----------|------------|
| CSV | `.csv` | None. Every row is data |
| Excel | `.xlsx`, `.xls` | None. Every row is data |

Both formats are read without a header. A text header (for example node names) is not detected; it makes the file unreadable, and the whole profile then falls back to a constant 1.0 (an error is logged, see [Troubleshooting](#common-errors-and-troubleshooting)).

### Layout

Each row is one hour; each column is one **node**.

```
0.00,0.00,0.00
0.00,0.00,0.00
0.00,0.00,0.00
0.00,0.00,0.00
0.00,0.00,0.00
0.05,0.04,0.06
0.15,0.12,0.18
0.35,0.30,0.40
0.55,0.50,0.60
0.75,0.72,0.78
0.85,0.80,0.90
0.90,0.87,0.92
0.88,0.85,0.91
0.80,0.76,0.84
0.60,0.55,0.65
0.35,0.30,0.40
0.10,0.08,0.12
0.00,0.00,0.00
0.00,0.00,0.00
0.00,0.00,0.00
0.00,0.00,0.00
0.00,0.00,0.00
0.00,0.00,0.00
0.00,0.00,0.00
```

| | Node 0 | Node 1 | Node 2 |
|---|--------|--------|--------|
| Hour 1 | 0.00 | 0.00 | 0.00 |
| ... | ... | ... | ... |
| Hour 7 | 0.15 | 0.12 | 0.18 |
| Hour 12 | 0.90 | 0.87 | 0.92 |
| ... | ... | ... | ... |
| Hour 8760 | 0.00 | 0.00 | 0.00 |

- **Values**: 0.0 to 1.0. Values outside this range are clipped.
- **Rows**: one per hour; 8,760 rows for one year. A header row is optional and skipped. Leap-year data lose their Feb 29 rows when read (see [Demand Data](demand-data.md#leap-years)), so a year is always 8,760 rows.
- **Columns**: one per node. A bus uses the column of its parent node.

### Column Handling

The file does not need exactly one column per node:

| Columns in the file | Result |
|---------------------|--------|
| 1 | The single profile is used at **every** node |
| Fewer than the number of nodes | The **last** column is repeated for the missing nodes |
| Equal to the number of nodes | Used as-is |
| More than the number of nodes | Extra columns are dropped |

Missing columns are filled from the last column, not with 1.0. No warning is logged for any of these adjustments.


---


## Example Profiles

### Solar PV (one day)

```
Hour,  Node 0 (South),  Node 1 (North)
  1        0.00              0.00
  ...
  7        0.12              0.08
  8        0.30              0.22
  9        0.52              0.45
 10        0.70              0.62
 11        0.82              0.75
 12        0.88              0.82
 13        0.90              0.84
 14        0.85              0.80
 15        0.72              0.68
 16        0.55              0.50
 17        0.32              0.28
 18        0.10              0.07
 19        0.01              0.00
 20        0.00              0.00
  ...
```

Typical features: zero at night, peaks of 0.80 to 0.95 on clear days, seasonal variation, and an annual capacity factor of roughly 0.15 to 0.25.

### Wind (one day)

```
Hour,  Node 0 (Coastal),  Node 1 (Inland)
  1        0.45               0.20
  2        0.50               0.22
  3        0.48               0.18
  4        0.52               0.25
  ...
 12        0.30               0.12
 13        0.28               0.10
  ...
 20        0.60               0.40
 21        0.55               0.35
  ...
```

Typical features: no fixed daily pattern, higher hour-to-hour variability than solar, and an annual capacity factor of roughly 0.20 to 0.45 depending on the site.

The header lines and the hour column above are only for display. The files themselves contain numbers only.


---


## Mapping Profiles to Units in YAML

Reference a profile with the `Availability` key (capital A):

```yaml
systems:
  my_system:
    generators:
      solar_pv:
        name: Solar PV
        type: Renewable
        fuel: Sun
        rated_power: [50.0, 30.0]
        Availability: data/profile_sun_1.csv
        # ... other fields ...

      wind_farm:
        name: Wind Farm
        type: Renewable
        fuel: Wind
        rated_power: [20.0, 0.0]
        Availability: data/profile_wind_1.csv
        # ... other fields ...

      diesel_gen:
        name: Diesel Generator
        type: Non-renewable
        fuel: Diesel
        rated_power: [15.0, 10.0]
        # No Availability key: constant 1.0
        # ... other fields ...

    technologies:
      tech_solar:
        name: Solar PV Investment
        type: Renewable
        fuel: Sun
        invest_cost: [900000.0, 900000.0]
        invest_max_power: [500.0, 500.0]
        Availability: data/profile_sun_1.csv
        # ... other fields ...
```

- **Generators** read only `Availability`. The key `availability_file` is ignored for generators.
- **Technologies** accept `Availability` or `availability_file`.
- **Batteries** do not use availability profiles.

In the master problem, renewable technologies use their profile; non-renewable technologies are taken as always available.

### Path Resolution

| Unit | Absolute path | Relative path |
|------|---------------|---------------|
| Generator | Used as-is | Resolved under the directory of the configuration file. A path that leaves that directory (`../`) is refused, and the generator is skipped with a warning |
| Technology | Used as-is | Looked up under the configuration file's directory first, then relative to the current working directory |

### Which Generators Need Profiles?

| Generator Type | Profile | Typical Values |
|---------------|---------|----------------|
| Solar PV | Yes | 0.0 to 0.90, daily pattern |
| Wind | Yes | 0.0 to 0.95, weather-driven |
| Run-of-river hydro | Yes | 0.2 to 0.8, seasonal |
| Biomass, OTEC | Optional | Near-constant, planned derating |
| Diesel / Gas / Fuel oil | No | Constant 1.0 |

For planned outages over specific dates, [Scheduled Interruptions](scheduled-interruptions.md) are an alternative to editing a profile.


---


## Temporal Resolution

Profiles are hourly. With `temporal.resolution_hours > 1`, ESFEX averages each block (mean), so with `resolution_hours: 6` the 8,760 hourly values become 1,460 six-hour values.

```yaml
temporal:
  resolution_hours: 6
  rolling_horizon_hours: 48
  overlap_hours: 24
```


---


## Profile Length and Multiple Years

The master problem and the operational dispatch look up hours by their position **in the whole horizon** (year index × 8,760 + hour of the year), wrapping around when the profile is shorter:

- A one-year (8,760-row) profile is repeated every year.
- A multi-year profile gives each simulated year its own 8,760-row block (rows 8,761 to 17,520 for the second year, and so on). If it has fewer years than the horizon, it starts again from its first row after its last year.


---


## How Availability Is Used in the Optimization

In operational dispatch, availability caps the output of every generator in every hour:

$$P_{g,b,t} \leq \bar{P}_{g,b,y} \times \alpha_{g,t,n(b)}$$

where:

- $P_{g,b,t}$: output of generator $g$ at bus $b$ in hour $t$
- $\bar{P}_{g,b,y}$: its capacity in year $y$, as set by the master problem (existing capacity after retirement and degradation, or the capacity built for an `inv_<technology>` unit)
- $\alpha_{g,t,n(b)}$: the profile value for hour $t$ at the bus's parent node $n(b)$, multiplied by any [scheduled interruption](scheduled-interruptions.md) factor

The optimizer can produce less than the available amount (curtailment), but never more. In the master problem, the profiles cap existing generators and renewable technologies on the representative days. For capacity adequacy, a renewable unit counts with its minimum availability.


---


## Generating Profiles from Weather Data

### Using ESFEX Built-in Workflows

The workflow dependencies (`pvlib`, `atlite`, `geopandas`, …) are part of the core install, so `pip install esfex` is all you need.

- **Solar PV**: uses `pvlib` with ERA5 reanalysis data to compute hourly capacity factors.
- **Wind**: uses `atlite` with ERA5 or MERRA-2 data to compute hourly capacity factors.

See [Analysis Workflows](../workflows/index.md) for detailed instructions.

### Using External Tools

You can also generate profiles externally and save them in the required format:

1. **Renewables.ninja** (https://www.renewables.ninja/): solar and wind capacity factors from ERA5/MERRA-2. Download as CSV, then remove the header and timestamp columns and keep one column per node.

2. **PVLIB Python** (https://pvlib-python.readthedocs.io/). Example:
   ```python
   import pvlib
   import pandas as pd

   location = pvlib.location.Location(23.0, -82.5, tz='America/Havana')
   times = pd.date_range('2025-01-01', periods=8760, freq='h')
   clearsky = location.get_clearsky(times)
   system = pvlib.pvsystem.PVSystem(
       surface_tilt=23, surface_azimuth=180,
       module_parameters={'pdc0': 1, 'gamma_pdc': -0.004},
       inverter_parameters={'pdc0': 1.1, 'eta_inv_nom': 0.96},
   )
   mc = pvlib.modelchain.ModelChain(system, location)
   mc.run_model(clearsky)
   capacity_factors = mc.results.ac.clip(0, 1)
   capacity_factors.to_csv('profile_sun_1.csv', header=False, index=False)
   ```

3. **Atlite** (https://atlite.readthedocs.io/): wind and solar resource assessment at scale (ERA5, MERRA-2, SARAH).

### Quality Checks

- All values are between 0.0 and 1.0.
- Solar profiles are zero at night.
- Annual capacity factors are plausible for the technology and site.
- No long runs of constant values, which may indicate data errors.
- 8,760 rows per year, no timestamp column.


---


## Loading Rules

1. **Value range**: values are clipped to [0.0, 1.0] without a warning.
2. **Columns**: see [Column Handling](#column-handling).
3. **Rows**: see [Profile Length and Multiple Years](#profile-length-and-multiple-years).
4. **Missing file** (generator or technology): the run stops at startup with `FileNotFoundError: Generator '<key>': availability_file '<file>' not found under <dir>` (or `Technology '<key>': ...`). A path that resolves outside the project directory stops it the same way.
5. **No file** on a `Renewable` unit: constant 1.0, with the warning described above.
6. **Unreadable file** (non-numeric cells below the header rows): an error `Error loading availability from <path>: …` is logged, and a constant 1.0 profile is used. Leading rows with non-numeric cells are skipped as headers.

---


## Profile Caching

1. **Startup preloading**: at the start of a run, ESFEX loads every profile referenced by a generator or technology once, at hourly resolution.
2. **Deduplication**: units that point to the same file share one array, keyed by the resolved path.
3. **Cache key**: the generator or technology key.
4. **Operational windows**: each rolling-horizon window takes its slice of the cached array instead of reading the file again.

### Zone Expansion

With [development zones](development-zones.md), each zone adds a node to the network. Every cached profile with one column per original node gets a column for each zone node, copied from the parent node of the bus the zone connects to.


---


## Common Errors and Troubleshooting

### File Not Found

```
FileNotFoundError: Generator 'solar_pv': availability_file 'data/profile_sun_1.csv' not found under /path/to/project
```

**Cause**: the `Availability` path does not resolve to a file.
**Effect**: the run stops before solving.
**Solution**: check the path relative to the configuration file's directory, or use an absolute path.

### Path Outside the Project

```
ValueError: Generator 'solar_pv': availability_file '../shared/sun.csv' resolves outside config directory /path/to/project (refusing path traversal)
```

**Solution**: move the file under the project directory, or use an absolute path.

### Profile Silently Constant at 1.0

```
ERROR  Error loading availability from /path/to/profile.xlsx: /path/to/profile.xlsx: non-numeric value in data row 5
```

**Cause**: the file has non-numeric cells below its header rows.
**Solution**: below an optional header, the file must contain numbers only.

### Wrong Profile at Some Nodes

The file has fewer columns than the system has nodes, so the last column was repeated. Provide one column per node, or a single column if the same profile applies everywhere.

### Unrealistic Results (High Curtailment or Load Shedding)

- Check that solar profiles are not used for wind generators, or the other way round.
- Check that the profile is hourly and has 8,760 rows.
- For generators, the key must be `Availability`; `availability_file` is ignored and the generator runs at a constant 1.0.

### Performance

- Excel files load more slowly than CSV; convert large `.xlsx` files to `.csv`.
- All profiles are held in memory for the whole run. Very large systems with many profiles need correspondingly more RAM.
