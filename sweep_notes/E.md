# Changelog text, group E (demand & time handling)

### Changed

- **Every modelled year has 8760 hours, leap years included.** The master
  problem counted 8784 h in leap years (`hours_per_year`), while demand
  and profiles are read in 8760-row years. For a leap year the master
  picked its representative days from rows starting at (y−1)·8784
  instead of (y−1)·8760, one day late per earlier year and running into
  the next year, and the annual scaling of operating costs, CO2 budgets
  and fuel supply limits used 8784 h in leap years. Now `hours_per_year` is
  always 8760, in Python (`hours_for_year`) and Julia (`hours_in_year`).
  Feb 29 rows of leap-year input data are dropped when demand,
  availability and inflow files are read (found from the calendar that
  starts at `date_start`; a file of exactly 8784 rows is always taken as
  one leap year), and date labels skip Feb 29, so every year runs from
  1 January to 31 December in the results viewer.
- **Year labels follow `date_start`.** A run always labelled its years
  from 2025, whatever `date_start` said, because the CLI did not pass a
  start year. Years are now labelled from the year of `date_start`, and
  the new `esfex run --start-year YYYY` option (and `start_year=` in
  `Orchestrator.run()` and `esfex.run()`) relabels them; the date labels
  then start on `date_start`'s day and time in that year. `date_start`
  still does not select rows: demand is read from the first row.

### Fixed

- **Excel demand: the first row is data again.** A run read Excel demand
  files with the first row as a header, so a file without a header lost
  its first hour and every later hour moved up by one. The run now reads
  CSV and Excel the same way, from the first row; leading rows with text
  (a header such as node names) are skipped, so files with or without a
  header give the same hours. Availability files follow the same rule (a
  header row used to make the profile fall back to a constant 1.0). A
  text cell below the header is reported as
  `non-numeric value in data row <n>`.
- **Multi-year availability profiles in the dispatch.** The dispatch
  looked up availability and reservoir inflow by the hour of the year, so
  every year used the first 8760 rows of a multi-year profile while the
  master used each year's own rows. The dispatch now indexes profiles by
  horizon hour, wrapping around like the master.
- **`load_demand_data(..., year_to_load=Y)`** returned the file's first row
  followed by the rows of year Y (off by one), and used calendar years of
  8784 h. It now returns the 8760-row block of year Y, raises `ValueError`
  for a year outside the data, and reads the rows a run reads.
  `DemandDataManager` also indexes 8760-row model years.
- **Custom constraints: integers on a unit axis.** An integer index on a
  generator, battery or technology axis was rejected as an unknown name,
  even when it matched a key such as `"1"`. Names now match as strings
  (also `"2030"` on a year axis), and an integer that is not a key is the
  unit's 1-based position.
