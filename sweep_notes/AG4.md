### Added

- **An interrupted myopic run can be resumed.** A myopic `development`
  run (`master_problem.planning_mode: myopic`) of a large system takes
  hours, and a crash, a kill, a lost solver licence or a reboot at year 18
  used to lose all of it. The run now keeps a checkpoint next to its results
  file (`<results>.checkpoint.json`), rewritten in one step after every
  master year it plans and every year it dispatches: the capacity each year
  built per technology and build year, the retirements and NPV-based
  retirements, the fuel and electrolyzer capacity, the RE shares, annuities
  and yearly costs, the master plan once every year is planned, the fuel
  tank levels carried into the next year, the warnings so far, and a
  fingerprint of the case (a hash of the configuration and the size and
  SHA-256 of every input file it names). `esfex run -c CASE --resume
  [RESULTS.h5]` (default: the newest results file of the case in the output
  directory) drops what the interrupted run wrote of the year it did not
  finish, restores the state and continues from the first year not
  completed, appending to the same results file; the result is the one the
  uninterrupted run gives. It refuses, with exit code 1 and a structured
  error (`--json`: `status` `resume_refused`, `errors[].code`), a file
  without a checkpoint, a run that completed, a perfect-foresight or
  operations run, other `--years` / `--start-year`, and a case whose
  configuration (except `logging` and the solver's `verbose`, `threads`
  and `time_limit`), input files or ESFEX version changed;
  `--force-resume` lets those last differences through with a warning each.
  `--resume --dry-run` only reports whether and where the run would resume.
  `run --json` reports `resumed_from` (the first year solved) and `resume`.
  From Python: `Orchestrator.run(resume_from=..., force_resume=...)` and
  `esfex.io.checkpoint`. The results file records `planning_mode` and the
  checkpoint's name in its root attributes. The Studio has no resume button
  yet.
