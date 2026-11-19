"""Scripting API for the ESFEX Python console.

Provides a ``esfex`` namespace object with high-level wrappers around
the optimization runner, result loading, sensitivity analysis, and
plotting utilities.  Injected into the console at startup so users can
write scripts like::

    cfg = esfex.load_config("my_system.yaml")
    results = esfex.run(cfg, output_dir="results/")
    esfex.plot.generation_mix(results, 2025)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Union


# ── Plot sub-namespace ────────────────────────────────────────────


class _PlotNamespace:
    """Chart helpers accessible via ``esfex.plot.*``."""

    @staticmethod
    def generation_mix(results: dict, year: int):
        """Stacked area chart of generation by source for *year*.

        Parameters
        ----------
        results : dict
            Output of ``esfex.load_results()``.
        year : int
            Simulation year to plot.

        Returns
        -------
        matplotlib.figure.Figure
        """
        import matplotlib.pyplot as plt
        import numpy as np

        yr_data = results.get("generation", {}).get(year)
        if yr_data is None:
            print(f"No generation data for year {year}.")
            return None
        gen_names = results.get("generator_names", [])
        hours = np.arange(yr_data.shape[-1])

        fig, ax = plt.subplots(figsize=(12, 5))
        if yr_data.ndim == 3:
            # [gen x node x hour] → aggregate nodes
            data = yr_data.sum(axis=1)
        else:
            data = yr_data

        ax.stackplot(hours, data, labels=gen_names[:len(data)])
        ax.set_xlabel("Hour")
        ax.set_ylabel("MW")
        ax.set_title(f"Generation Mix — {year}")
        ax.legend(loc="upper left", fontsize=7, ncol=3)
        fig.tight_layout()
        plt.show()
        return fig

    @staticmethod
    def prices(results: dict, year: int):
        """Nodal price time series for *year*.

        Returns
        -------
        matplotlib.figure.Figure
        """
        import matplotlib.pyplot as plt
        import numpy as np

        yr_data = results.get("prices", {}).get(year)
        if yr_data is None:
            print(f"No price data for year {year}.")
            return None

        fig, ax = plt.subplots(figsize=(12, 4))
        if yr_data.ndim == 2:
            for n in range(yr_data.shape[0]):
                ax.plot(yr_data[n], label=f"Node {n}", alpha=0.7, linewidth=0.5)
        else:
            ax.plot(yr_data)
        ax.set_xlabel("Hour")
        ax.set_ylabel("$/MWh")
        ax.set_title(f"Nodal Prices — {year}")
        if yr_data.ndim == 2 and yr_data.shape[0] <= 10:
            ax.legend(fontsize=7)
        fig.tight_layout()
        plt.show()
        return fig

    @staticmethod
    def investments(results: dict):
        """Bar chart of cumulative investment decisions across years.

        Returns
        -------
        matplotlib.figure.Figure
        """
        import matplotlib.pyplot as plt

        inv = results.get("investments", {})
        if not inv:
            print("No investment data found.")
            return None

        fig, ax = plt.subplots(figsize=(10, 5))
        years = sorted(inv.keys())
        tech_names: set[str] = set()
        for yr in years:
            tech_names.update(inv[yr].keys())
        tech_names_sorted = sorted(tech_names)

        bottom = [0.0] * len(years)
        for tech in tech_names_sorted:
            vals = [inv[yr].get(tech, 0.0) for yr in years]
            ax.bar(years, vals, bottom=bottom, label=tech)
            bottom = [b + v for b, v in zip(bottom, vals)]

        ax.set_xlabel("Year")
        ax.set_ylabel("MW")
        ax.set_title("Investment Decisions")
        ax.legend(fontsize=7, ncol=2)
        fig.tight_layout()
        plt.show()
        return fig

    @staticmethod
    def load_duration(results: dict, year: int):
        """Load duration curve for *year*.

        Returns
        -------
        matplotlib.figure.Figure
        """
        import matplotlib.pyplot as plt
        import numpy as np

        demand = results.get("demand", {}).get(year)
        if demand is None:
            print(f"No demand data for year {year}.")
            return None

        fig, ax = plt.subplots(figsize=(10, 4))
        if demand.ndim == 2:
            total = demand.sum(axis=0)
        else:
            total = demand
        sorted_load = np.sort(total)[::-1]
        ax.plot(sorted_load)
        ax.set_xlabel("Hours")
        ax.set_ylabel("MW")
        ax.set_title(f"Load Duration Curve — {year}")
        ax.fill_between(range(len(sorted_load)), sorted_load, alpha=0.3)
        fig.tight_layout()
        plt.show()
        return fig


# ── Workflows sub-namespace ────────────────────────────────────────


class _WorkflowsNamespace:
    """Standalone analysis workflows accessible via ``esfex.workflows.*``.

    Each method wraps a pure-Python model module from ``esfex.models``
    with no GUI dependencies.
    """

    # ── Wind ──────────────────────────────────────────────────

    @staticmethod
    def wind_weibull(speeds):
        """Fit Weibull distribution to wind speed data.

        Parameters
        ----------
        speeds : array-like
            Wind speed time series (m/s).

        Returns
        -------
        tuple (k, A)
            Shape and scale parameters.

        Example
        -------
        >>> k, A = esfex.workflows.wind_weibull(wind_speeds)
        >>> print(f"Weibull k={k:.2f}, A={A:.2f} m/s")
        """
        from esfex.models.wind_models import fit_weibull
        import numpy as np
        return fit_weibull(np.asarray(speeds))

    @staticmethod
    def wind_financials(capacity_mw=10.0, capacity_factor=0.30,
                        capex_per_kw=1500.0, opex_per_kw_yr=30.0,
                        discount_rate=0.08, lifetime_years=25):
        """Compute wind project financials (LCOE, NPV, IRR).

        Returns
        -------
        WindFinancialResults
            Object with ``.lcoe``, ``.npv``, ``.irr`` attributes.
        """
        from esfex.models.wind_models import (
            WindFinancialInputs, compute_wind_financials,
        )
        inputs = WindFinancialInputs(
            capacity_mw=capacity_mw, capacity_factor=capacity_factor,
            capex_per_kw=capex_per_kw, opex_per_kw_yr=opex_per_kw_yr,
            discount_rate=discount_rate, lifetime_years=lifetime_years,
        )
        return compute_wind_financials(inputs)

    # ── Solar PV ──────────────────────────────────────────────

    @staticmethod
    def solar_financials(capacity_mw=10.0, capacity_factor=0.20,
                         capex_per_kw=1000.0, opex_per_kw_yr=15.0,
                         discount_rate=0.08, lifetime_years=25,
                         electricity_price=50.0, degradation_rate=0.005):
        """Compute solar PV project financials (LCOE, NPV, IRR).

        Returns
        -------
        SolarFinancialResults
            Object with ``.lcoe``, ``.npv``, ``.irr``, ``.cash_flows``.
        """
        from esfex.models.solar_pv_models import (
            SolarFinancialInputs, compute_pv_financials,
        )
        inputs = SolarFinancialInputs(
            capacity_mw=capacity_mw, capacity_factor=capacity_factor,
            capex_per_kw=capex_per_kw, opex_per_kw_yr=opex_per_kw_yr,
            discount_rate=discount_rate, lifetime_years=lifetime_years,
            electricity_price=electricity_price,
            degradation_rate=degradation_rate,
        )
        return compute_pv_financials(inputs)

    @staticmethod
    def solar_peak_sun_hours(ghi_hourly):
        """Peak sun hours from hourly GHI (W/m²) array."""
        from esfex.models.solar_pv_models import compute_peak_sun_hours
        import numpy as np
        return compute_peak_sun_hours(np.asarray(ghi_hourly))

    # ── Financial Analysis (post-optimization) ────────────────

    @staticmethod
    def system_financials(h5_path, assumptions=None):
        """Full financial analysis from HDF5 results.

        Parameters
        ----------
        h5_path : str or Path
            Path to simulation results HDF5 file.
        assumptions : FinancialAssumptions, optional
            Override default financial parameters.

        Returns
        -------
        SystemFinancials
            NPV, IRR, LCOE, DSCR, cash flows, tech-level breakdown.

        Example
        -------
        >>> fin = esfex.workflows.system_financials("results/system.h5")
        >>> print(f"System LCOE: {fin.lcoe_system:.2f} $/MWh")
        >>> print(f"Project IRR: {fin.project_irr:.1%}")
        """
        from esfex.models.financial_analysis import compute_system_financials
        return compute_system_financials(h5_path, assumptions)

    @staticmethod
    def financial_monte_carlo(h5_path, n_simulations=1000, seed=42,
                              assumptions=None):
        """Monte Carlo financial risk analysis.

        Returns
        -------
        MonteCarloResult
            Distributions of NPV, IRR, LCOE across simulations.
        """
        from esfex.models.financial_analysis import (
            run_monte_carlo, FinancialAssumptions,
        )
        return run_monte_carlo(
            h5_path, assumptions or FinancialAssumptions(),
            n_simulations=n_simulations, seed=seed,
        )

    # ── Hazard Assessment ─────────────────────────────────────

    @staticmethod
    def hazard_screening(coordinates):
        """Screen multiple locations for all 7 natural hazard types.

        Parameters
        ----------
        coordinates : list of (lat, lon) tuples
            Locations to screen.

        Returns
        -------
        HazardIntensityMap
            Categorical risk levels per node per hazard.

        Example
        -------
        >>> coords = [(21.5, -77.8), (22.4, -79.9)]
        >>> him = esfex.workflows.hazard_screening(coords)
        """
        from esfex.models.hazard_assessment import ScreeningFetcher
        fetcher = ScreeningFetcher()
        return fetcher.fetch(coordinates)

    @staticmethod
    def risk_assessment(hazard_maps, node_components,
                        combination="independent",
                        component_values=None):
        """Multi-hazard composite risk assessment.

        Parameters
        ----------
        hazard_maps : list[HazardIntensityMap]
            From ``hazard_screening`` or per-hazard fetchers.
        node_components : dict
            ``{node_idx: ["solar_pv", "battery", ...]}``.
        combination : str
            ``"independent"``, ``"copula"``, or ``"mcda"``.

        Returns
        -------
        list[NodeRiskProfile]
            Per-node risk profiles with failure probs and EAL.
        """
        from esfex.models.hazard_assessment import (
            CompositeRiskAssessment, FragilityLibrary,
        )
        lib = FragilityLibrary()
        assessment = CompositeRiskAssessment(
            fragility_library=lib, combination_method=combination,
        )
        return assessment.assess(
            hazard_maps, node_components,
            component_values=component_values,
        )

    # ── EV Profiles ───────────────────────────────────────────

    @staticmethod
    def ev_profiles(num_nodes, num_hours, ev_categories, ev_quantity,
                    base_patterns, base_year=2025, target_year=2050,
                    growth_rate=0.12):
        """Generate EV charging demand profiles with S-curve growth.

        Returns
        -------
        DataFrame
            Hourly EV demand per node/category (MW).

        Example
        -------
        >>> profiles = esfex.workflows.ev_profiles(
        ...     num_nodes=3, num_hours=8760,
        ...     ev_categories={"light": {"charging_power": 7.0}},
        ...     ev_quantity={"light": [100, 200, 150]},
        ...     base_patterns={"light": [0]*6 + [0.3]*4 + [0.8]*4 + [1.0]*4 + [0.5]*6},
        ... )
        """
        from esfex.models.ev import generate_ev_profiles
        return generate_ev_profiles(
            num_nodes, num_hours, ev_categories, ev_quantity,
            base_patterns, base_year=base_year, target_year=target_year,
            growth_rate=growth_rate,
        )

    # ── Demand Estimation ─────────────────────────────────────

    @staticmethod
    def estimate_demand(iso3, latitude, longitude, year,
                        temperatures_hourly=None, annual_demand_mwh=None,
                        static_features=None):
        """Hourly demand profile of a site from ESFEX's national XGBoost
        demand model (the Demand Estimation workflow's fallback model).

        The features are built as the model was trained: the calendar and
        holidays of *iso3*, the temperature and its daily statistics, and GDP
        per capita, population density and urbanization sampled at the site.

        Parameters
        ----------
        iso3 : str
            Country code (holidays and the model's country encoding).
        latitude, longitude : float
            Site coordinates.
        year : int
            Calendar year of the profile (8760 h, 29 February dropped).
        temperatures_hourly : array, optional
            8760 hourly temperatures (°C) on the site's local time. None
            uses 20 °C, as the model's training data do without a series.
        annual_demand_mwh : float, optional
            Annual energy. Given: the profile is in MW and sums to it.
            None: dimensionless shape factors with mean 1.
        static_features : dict, optional
            ``log_gdp_per_cap``, ``log_pop_density`` and ``urbanization`` (as
            in the training data) instead of sampling them at the site, which
            downloads the GDP and population rasters on first use.

        Returns
        -------
        np.ndarray
            8760 hourly values (MW, or shape factors).
        """
        from esfex.models.demand_ml import DemandMLModel, build_hourly_features

        if not DemandMLModel.is_available("xgboost"):
            raise RuntimeError(
                "Demand ML model not available. xgboost ships with esfex; "
                "reinstall with: pip install --upgrade --force-reinstall esfex"
            )

        model = DemandMLModel.load_bundled(engine="xgboost")
        features = build_hourly_features(
            iso3, temperatures_hourly, latitude, longitude, int(year),
            static_features=static_features)
        shape = model.predict(features)
        if annual_demand_mwh is None:
            return shape
        return shape * (float(annual_demand_mwh) / len(shape))

    # ── Climate Profiles ──────────────────────────────────────

    @staticmethod
    def climate_demand_adjustment(base_demand, temperatures, base_temp=24.0,
                                  alpha_cool=2.5, alpha_heat=0.5):
        """Adjust demand for climate change (HDD/CDD scaling).

        Parameters
        ----------
        base_demand : array
            Baseline hourly demand (MW).
        temperatures : array
            Hourly temperatures (°C).
        base_temp : float
            Reference temperature.
        alpha_cool, alpha_heat : float
            %/°C sensitivity coefficients.

        Returns
        -------
        np.ndarray
            Adjusted demand.
        """
        from esfex.models.climate_profiles import compute_climate_demand
        import numpy as np
        return compute_climate_demand(
            np.asarray(base_demand), np.asarray(temperatures),
            base_temp=base_temp, alpha_cool=alpha_cool,
            alpha_heat=alpha_heat,
        )

    # ── OTEC ──────────────────────────────────────────────────

    @staticmethod
    def otec_capacity_factors(t_warm, t_cold, gross_power_kw=10000.0):
        """Compute daily OTEC capacity factors from temperature profiles.

        Parameters
        ----------
        t_warm, t_cold : array
            Daily warm/cold water temperatures (°C).
        gross_power_kw : float
            Gross plant power.

        Returns
        -------
        np.ndarray
            Daily capacity factors (0-1).
        """
        from esfex.models.otec_models import DailyOTECData, compute_daily_cf
        import numpy as np
        data = DailyOTECData(
            timestamps=[f"day_{i}" for i in range(len(t_warm))],
            t_warm=np.asarray(t_warm),
            t_cold=np.asarray(t_cold),
        )
        return compute_daily_cf(data, gross_power_kw)

    # ── TSAM (Time Series Aggregation) ────────────────────────

    @staticmethod
    def tsam_cluster(demand, n_periods=10, method="kmedoids"):
        """Cluster demand into representative periods.

        Parameters
        ----------
        demand : array (n_hours,) or (n_days, 24)
            Demand time series.
        n_periods : int
            Number of representative periods.
        method : str
            ``"kmedoids"`` or ``"kmeans"``.

        Returns
        -------
        TSAMResult
            ``.representative_days``, ``.weights``, ``.mapping``.
        """
        from esfex.models.tsam import compute_tsam_periods
        import numpy as np
        return compute_tsam_periods(
            np.asarray(demand), n_periods=n_periods, method=method,
        )

    @staticmethod
    def help():
        """Print workflows reference."""
        text = """
── Workflows API ──────────────────────────────────────────────

  Wind:
    esfex.workflows.wind_weibull(speeds)          → (k, A)
    esfex.workflows.wind_financials(...)           → LCOE, NPV, IRR

  Solar PV:
    esfex.workflows.solar_financials(...)          → LCOE, NPV, IRR
    esfex.workflows.solar_peak_sun_hours(ghi)      → float (hours)

  Financial (post-optimization):
    esfex.workflows.system_financials(h5_path)     → full breakdown
    esfex.workflows.financial_monte_carlo(h5_path) → risk distributions

  Hazard & Risk:
    esfex.workflows.hazard_screening(coords)       → HazardIntensityMap
    esfex.workflows.risk_assessment(maps, comps)   → NodeRiskProfiles

  EV:
    esfex.workflows.ev_profiles(...)               → DataFrame (MW/h)

  Demand:
    esfex.workflows.estimate_demand(iso3, lat, lon, year) → 8760 h profile
    esfex.workflows.climate_demand_adjustment(...)  → adjusted MW array

  OTEC:
    esfex.workflows.otec_capacity_factors(tw, tc)  → daily CFs

  Time Series:
    esfex.workflows.tsam_cluster(demand, n)        → representative periods
"""
        print(text)


# ── Main API class ────────────────────────────────────────────────


class ESFEXAPI:
    """High-level scripting API for the ESFEX console.

    Access via the ``esfex`` object in the console::

        esfex.help()
        cfg = esfex.load_config("config.yaml")
        results = esfex.run(cfg, "output/")
    """

    plot = _PlotNamespace()
    workflows = _WorkflowsNamespace()

    # ── Properties ────────────────────────────────────────────

    @property
    def version(self) -> str:
        """Return the esfex version string."""
        try:
            from esfex import __version__
            return __version__
        except ImportError:
            return "unknown"

    @property
    def available_solvers(self) -> list[str]:
        """List optimization solvers that are installed and usable."""
        solvers = []
        for name, mod in [
            ("HiGHS", "highspy"),
            ("Gurobi", "gurobipy"),
            ("CPLEX", "cplex"),
            ("SCIP", "pyscipopt"),
            ("CBC", "cylp"),
            ("GLPK", "glpk"),
        ]:
            try:
                __import__(mod)
                solvers.append(name)
            except ImportError:
                pass
        return solvers

    # ── Config ────────────────────────────────────────────────

    @staticmethod
    def load_config(path: Union[str, Path]):
        """Load a YAML configuration file.

        Parameters
        ----------
        path : str or Path
            Path to the YAML config file.

        Returns
        -------
        ESFEXConfig
            Validated configuration object.  All fields are accessible
            as attributes, e.g. ``cfg.systems``, ``cfg.temporal``.

        Example
        -------
        >>> cfg = esfex.load_config("isla_juventud.yaml")
        >>> print(cfg.temporal.num_years)
        """
        from esfex.config.loader import load_config
        return load_config(Path(path))

    # ── Results I/O ───────────────────────────────────────────

    @staticmethod
    def load_results(
        path: Union[str, Path], years: list[int] | None = None,
    ) -> dict[str, Any]:
        """Load simulation results from an HDF5 file.

        Reads the ``detailed_results`` and ``summary_results`` groups the
        runner writes (see the HDF5 output schema) into numpy arrays keyed
        by year::

            {
                "years": [2025, 2026, ...],
                "generation": {2025: ndarray[generator, step], ...},
                "storage": {2025: ndarray[battery, step], ...},   # SOC, MWh
                "demand": {2025: ndarray[node, step], ...},
                "prices": {2025: ndarray[node, step], ...},
                "power_flow": {2025: ndarray[node, node, step], ...},
                "curtailment": {2025: ndarray[node, step], ...},
                "investments": {2025: {"Solar PV": 100.0, ...}, ...},
                "summary": {2025: {"total_cost": ..., "renewable_penetration": ...}},
                "generator_names": [...],
                "battery_names": [...],
            }

        ``generation`` and ``storage`` are summed over nodes; rows follow
        ``generator_names`` / ``battery_names`` (zeros for a unit absent in a
        year). ``prices`` are the nodal prices (the mean price over nodes
        when a file has none). ``investments`` is the new capacity of each
        technology in the year, in MW, summed over nodes. ``summary`` holds
        the year's scalars (``total_cost``, ``renewable_penetration``,
        ``co2_emissions``, ``loss_of_load``, ``solve_time``, ...).

        Parameters
        ----------
        path : str or Path
            HDF5 results file.
        years : list of int, optional
            Years to read (default: all). Large runs need a lot of memory
            for all years at once.

        Example
        -------
        >>> r = esfex.load_results("results/system_Cuba.h5")
        >>> print(r["years"])
        >>> gen = r["generation"][2025]  # numpy array
        """
        import re

        import h5py
        import numpy as np

        from esfex.io.exporter import demand_node_hours

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Results file not found: {path}")

        out: dict[str, Any] = {
            "years": [],
            "generation": {},
            "demand": {},
            "prices": {},
            "storage": {},
            "power_flow": {},
            "curtailment": {},
            "investments": {},
            "summary": {},
            "generator_names": [],
            "battery_names": [],
        }

        def _unit_rows(group, names: list[str]) -> dict[str, np.ndarray]:
            """{name: series summed over nodes} for a per-unit group."""
            rows = {}
            for name, ds in group.items():
                arr = np.asarray(ds[()], dtype=float)
                rows[name] = arr.sum(axis=0) if arr.ndim == 2 else arr
                if name not in names:
                    names.append(name)
            return rows

        def _tech_names(f, group: str, prefix: str) -> dict[int, str]:
            grp = f.get(f"system_configuration/{group}")
            if grp is None:
                return {}
            out_names = {}
            for key, sub in grp.items():
                if key.startswith(prefix) and key[len(prefix):].isdigit():
                    name = sub.attrs.get("name", key)
                    out_names[int(key[len(prefix):])] = (
                        name.decode() if isinstance(name, bytes) else str(name))
            return out_names

        inv_patterns = [
            (re.compile(r"^investment_tech_investment_power_(\d+)_\d+$"), "tech"),
            (re.compile(r"^investment_bat_tech_investment_power_(\d+)_\d+$"), "bat"),
        ]

        gen_rows: dict[int, dict[str, np.ndarray]] = {}
        soc_rows: dict[int, dict[str, np.ndarray]] = {}
        with h5py.File(path, "r") as f:
            names_by_kind = {
                "tech": _tech_names(f, "technologies", "technology_"),
                "bat": _tech_names(f, "battery_technologies", "battery_technology_"),
            }
            scenarios = {}
            for key, grp in (f.get("detailed_results") or {}).items():
                m = re.match(r"^year_(\d+)", key)
                if m and (years is None or int(m.group(1)) in years):
                    scenarios[int(m.group(1))] = grp

            for yr in sorted(scenarios):
                grp = scenarios[yr]
                out["years"].append(yr)
                ep = grp.get("electricity_prices")
                if "demand" in grp:
                    # [node, step]; old files may hold [step, node]
                    out["demand"][yr] = demand_node_hours(
                        grp["demand"], ep.shape[0] if ep is not None else None)
                for ds_name in ("power_flow", "curtailment"):
                    if ds_name in grp:
                        out[ds_name][yr] = np.asarray(grp[ds_name][()])
                if "nodal_electricity_prices" in grp:
                    out["prices"][yr] = np.asarray(grp["nodal_electricity_prices"][()])
                elif ep is not None:
                    out["prices"][yr] = np.asarray(ep[()])[np.newaxis, :]
                if "generation" in grp:
                    gen_rows[yr] = _unit_rows(grp["generation"], out["generator_names"])
                if "battery_soc" in grp:
                    soc_rows[yr] = _unit_rows(grp["battery_soc"], out["battery_names"])

                summary: dict[str, Any] = {}
                investments: dict[str, float] = {}
                for attr, value in grp.attrs.items():
                    if attr.startswith("investment_"):
                        for pattern, kind in inv_patterns:
                            m = pattern.match(attr)
                            if m:
                                idx = int(m.group(1))
                                name = names_by_kind[kind].get(idx, f"{kind}_{idx}")
                                investments[name] = investments.get(name, 0.0) + float(value)
                    elif not attr.startswith("retirement_"):
                        summary[attr] = value.item() if hasattr(value, "item") else value
                out["summary"][yr] = summary
                if investments:
                    out["investments"][yr] = investments

            # Per-year scalars of summary_results (one row per year)
            sr = f.get("summary_results")
            if sr is not None and "year" in sr:
                sr_years = [int(y) for y in sr["year"][()]]
                for col, ds in sr.items():
                    if col == "year":
                        continue
                    values = ds[()]
                    for i, yr in enumerate(sr_years):
                        if yr in out["summary"] and i < len(values):
                            out["summary"][yr][col] = values[i].item()

        for key, rows_by_year, names in (
            ("generation", gen_rows, out["generator_names"]),
            ("storage", soc_rows, out["battery_names"]),
        ):
            for yr, rows in rows_by_year.items():
                if not rows:
                    continue
                n_steps = max((len(r) for r in rows.values()), default=0)
                out[key][yr] = np.array([
                    rows[n] if n in rows else np.zeros(n_steps) for n in names
                ])

        print(f"Loaded {path.name}: {len(out['years'])} years, "
              f"{len(out['generator_names'])} generators, "
              f"{len(out['battery_names'])} batteries")
        return out

    @staticmethod
    def export_results(
        h5_path: Union[str, Path],
        output_dir: Union[str, Path],
        fmt: str = "csv",
    ) -> None:
        """Export HDF5 results to CSV, Excel, or JSON.

        Parameters
        ----------
        h5_path : str or Path
            Path to the HDF5 results file.
        output_dir : str or Path
            Directory to write exported files.
        fmt : str
            Format: ``"csv"``, ``"excel"``, or ``"json"``.

        Example
        -------
        >>> esfex.export_results("results/system.h5", "exports/", "csv")
        """
        from esfex.io.exporter import ResultsExporter

        exporter = ResultsExporter(Path(h5_path))
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if fmt == "csv":
            exporter.to_csv(output_dir)
        elif fmt == "excel":
            exporter.to_excel(output_dir / "results.xlsx")
        elif fmt == "json":
            exporter.to_json(output_dir / "results.json")
        else:
            raise ValueError(f"Unknown format: {fmt!r}. Use 'csv', 'excel', or 'json'.")
        print(f"Exported to {output_dir} ({fmt})")

    # ── Simulation ────────────────────────────────────────────

    @staticmethod
    def run(
        config: Any,
        output_dir: Union[str, Path] = "results",
        years: int | None = None,
        start_year: int | None = None,
    ):
        """Run a full ESFEX simulation (master + operational dispatch).

        Parameters
        ----------
        config : ESFEXConfig or str or Path
            Configuration object or path to YAML file.
        output_dir : str or Path
            Directory for output HDF5 files.
        years : int, optional
            Number of years to simulate (default: all from config).
        start_year : int, optional
            Label of the first simulated year (default: the year of
            ``date_start``).

        Returns
        -------
        list[YearResults]
            One result object per simulated year.

        Raises
        ------
        esfex.runner.RunAbortedError
            The master problem (``development`` mode) found no plan; nothing
            was dispatched.

        Example
        -------
        >>> cfg = esfex.load_config("config.yaml")
        >>> results = esfex.run(cfg, "output/", years=5)
        >>> for yr in results:
        ...     print(f"{yr.year}: obj={yr.objective:.0f}, RE={yr.re_penetration:.1%}")
        """
        from esfex.config.loader import load_config
        from esfex.runner import Orchestrator

        if isinstance(config, (str, Path)):
            config_path = Path(config)
            config = load_config(config_path)
        else:
            config_path = None

        orch = Orchestrator(
            config, output_dir=Path(output_dir), config_path=config_path,
        )
        return orch.run(years=years, start_year=start_year)

    # ── Individual solvers ────────────────────────────────────

    @staticmethod
    def _run_config(config: Any, solver: str | None, **updates):
        """(config, config_path) with *updates* and the solver applied."""
        from esfex.config.loader import load_config

        config_path = None
        if isinstance(config, (str, Path)):
            config_path = Path(config)
            config = load_config(config_path)
        if solver is not None:
            updates["solver"] = type(config.solver)(
                **{**config.solver.model_dump(), "name": solver})
        if updates:
            config = config.model_copy(update=updates)
        return config, config_path

    @staticmethod
    def solve_master(
        config: Any,
        solver: str | None = None,
        years: int | None = None,
        start_year: int | None = None,
        output_dir: Union[str, Path] = "results",
    ) -> dict[str, Any]:
        """Solve only the master (capacity expansion) problem.

        Prepares demand, availability and units exactly like ``esfex.run()``
        and stops after the master problem (the config is run in
        ``development`` mode whatever its ``simulation_mode``).

        Parameters
        ----------
        config : ESFEXConfig or str or Path
            Configuration object or path to YAML file.
        solver : str, optional
            Solver name; default: the config's ``solver.name``.
        years : int, optional
            Number of years to plan (default: 25, as ``esfex.run()``).
        start_year : int, optional
            First planning year (default: the year of ``date_start``).
        output_dir : str or Path
            Directory for the run log (and an HDF5 file holding only the
            system configuration).

        Returns
        -------
        dict
            ``investments`` and ``retirements`` ({year: {key: value}}, keys
            as in the HDF5 ``investment_*`` / ``retirement_*`` attributes),
            ``cumulative_capacities`` ({year: {...}}) and ``re_targets``
            ({year: fraction}).

        Example
        -------
        >>> sol = esfex.solve_master("config.yaml", years=5)
        >>> print(sol["investments"][2025])
        """
        from esfex.runner import Orchestrator

        config, config_path = ESFEXAPI._run_config(
            config, solver, simulation_mode="development")
        from esfex.runner import RunAbortedError

        orch = Orchestrator(config, output_dir=Path(output_dir), config_path=config_path)
        try:
            orch.run(years=years, start_year=start_year, master_only=True)
        except RunAbortedError as e:
            raise RuntimeError(
                f"Master problem failed; see the log in {Path(output_dir) / 'logs'}") from e
        return orch.master_solution

    @staticmethod
    def solve_dispatch(
        config: Any,
        solver: str | None = None,
        year: int | None = None,
        output_dir: Union[str, Path] = "results",
    ):
        """Solve the operational dispatch of one year with the existing units.

        No master problem: the units in the config are dispatched as they
        are (investment candidates are not built). A ``development`` config
        is dispatched as ``economic_dispatch``; ``unit_commitment`` and
        ``economic_dispatch`` configs keep their mode.

        Parameters
        ----------
        config : ESFEXConfig or str or Path
            Configuration object or path to YAML file.
        solver : str, optional
            Solver name; default: the config's ``solver.name``.
        year : int, optional
            Data year to dispatch (default: the year of ``date_start``).
            Year ``Y`` is the ``(Y - year of date_start)``-th 8760-h block
            of the demand and profile files (a file with fewer years repeats
            its first year, as in a full run); the result is labelled ``Y``
            and outage windows count from 1 January of the year of
            ``date_start``, as in a full run.
        output_dir : str or Path
            Directory for the HDF5 results file and the run log.

        Returns
        -------
        YearResults
            ``objective``, ``gen_output``, ``prices``, ``re_penetration``, ...

        Example
        -------
        >>> res = esfex.solve_dispatch("config.yaml")
        >>> print(res.objective, res.gen_output.shape)
        """
        from esfex.runner import Orchestrator
        from esfex.utils.temporal import parse_date_start

        config, config_path = ESFEXAPI._run_config(config, solver)
        if config.simulation_mode == "development":
            config = config.model_copy(update={"simulation_mode": "economic_dispatch"})
        first_year = parse_date_start(config.date_start).year
        year = first_year if year is None else int(year)
        if year < first_year:
            raise ValueError(
                f"year {year} is before the data's first year {first_year} (date_start)")
        orch = Orchestrator(config, output_dir=Path(output_dir), config_path=config_path)
        # The horizon starts at the data's first year, so year Y keeps its
        # own data block and outage base year; only Y is dispatched.
        results = orch.run(years=year - first_year + 1, dispatch_years=[year])
        if not results:
            raise RuntimeError(
                f"Dispatch failed; see the log in {Path(output_dir) / 'logs'}")
        return results[0]

    # ── Sensitivity ───────────────────────────────────────────

    @staticmethod
    def sensitivity(
        config_path: Union[str, Path],
        parameters: list[dict[str, Any]] | None = None,
        n_samples: int = 64,
        output_dir: Union[str, Path] = "sensitivity_output",
        years: int | None = None,
    ):
        """Run Sobol global sensitivity analysis.

        Parameters
        ----------
        config_path : str or Path
            Path to YAML config file.
        parameters : list of dict, optional
            Each dict: ``{"name": str, "lower": float, "upper": float}``,
            where ``name`` is a config parameter key such as ``"fuel_cost"``
            (see ``esfex.sensitivity.engine.CONFIG_PARAMETERS``).
            If None, uses all predefined config parameters.
        n_samples : int
            Base sample count for Saltelli sampling.
        output_dir : str or Path
            Working directory for intermediate files.
        years : int, optional
            Years simulated per run (default: the ``esfex run`` default, 25).

        Returns
        -------
        SobolResult
            Object with ``S1``, ``ST`` (first/total-order indices),
            ``parameters``, ``kpi_names``. Call ``.to_csv(path)`` to export.

        Example
        -------
        >>> result = esfex.sensitivity("config.yaml", n_samples=128)
        >>> for kpi in result.kpi_names:
        ...     print(f"{kpi}: most sensitive to {result.parameters[result.ST[kpi].argmax()]}")
        """
        from esfex.sensitivity.engine import (
            CONFIG_PARAMETERS,
            SensitivityEngine,
            SensitivityParameter,
            get_config_parameters,
        )

        config_path = str(Path(config_path))
        output_dir = str(Path(output_dir))

        if parameters is None:
            params = get_config_parameters()
        else:
            known = {key: (display, bounds) for key, display, bounds in CONFIG_PARAMETERS}
            params = []
            for p in parameters:
                key = p["name"]
                if key not in known:
                    raise ValueError(
                        f"Unknown sensitivity parameter {key!r}; use one of {sorted(known)}")
                display, (lo, hi) = known[key]
                params.append(SensitivityParameter(
                    name=display, key=key,
                    lower_bound=p.get("lower", lo), upper_bound=p.get("upper", hi),
                    category="config",
                ))
        engine = SensitivityEngine(mode="config", parameters=params,
                                   n_base_samples=n_samples, years=years)

        def _progress(current, total, msg):
            print(f"  [{current}/{total}] {msg}")

        return engine.run_config_analysis(
            config_path, output_dir, progress_callback=_progress,
        )

    # ── Help ──────────────────────────────────────────────────

    @staticmethod
    def help() -> None:
        """Print a quick reference of all available scripting functions."""
        text = """
╔══════════════════════════════════════════════════════════════╗
║                  ESFEX Scripting API                        ║
╚══════════════════════════════════════════════════════════════╝

  esfex.version                  → Version string
  esfex.available_solvers        → List installed solvers

── Configuration ──────────────────────────────────────────────
  esfex.load_config(path)        → Load YAML → ESFEXConfig
  esfex.load_results(path)       → Load HDF5 → dict of arrays
  esfex.export_results(h5, dir, fmt)  → Export to csv/excel/json

── Simulation ─────────────────────────────────────────────────
  esfex.run(config, output_dir)  → Full simulation → YearResults
  esfex.solve_master(config, years=5)  → Capacity expansion only → dict
  esfex.solve_dispatch(config, year=2025)
                                  → One year's dispatch, existing units

── Analysis ───────────────────────────────────────────────────
  esfex.sensitivity(config_path, params, n_samples)
                                  → Sobol global sensitivity

── Workflows (standalone models) ──────────────────────────────
  esfex.workflows.help()                         → Full reference
  esfex.workflows.wind_weibull(speeds)            → Weibull (k, A)
  esfex.workflows.wind_financials(...)            → LCOE, NPV, IRR
  esfex.workflows.solar_financials(...)           → LCOE, NPV, IRR
  esfex.workflows.system_financials(h5)           → Post-opt financials
  esfex.workflows.financial_monte_carlo(h5)       → Risk distributions
  esfex.workflows.hazard_screening(coords)        → Multi-hazard screen
  esfex.workflows.risk_assessment(maps, comps)    → Composite risk
  esfex.workflows.ev_profiles(...)                → EV demand (MW/h)
  esfex.workflows.estimate_demand(...)            → Site demand profile
  esfex.workflows.climate_demand_adjustment(...)  → HDD/CDD scaling
  esfex.workflows.otec_capacity_factors(tw, tc)   → OTEC daily CFs
  esfex.workflows.tsam_cluster(demand, n)         → Representative days

── Plotting ───────────────────────────────────────────────────
  esfex.plot.generation_mix(results, year)
  esfex.plot.prices(results, year)
  esfex.plot.investments(results)
  esfex.plot.load_duration(results, year)

── Console Objects ────────────────────────────────────────────
  model    → GUI data model (all systems, elements)
  state    → Current system state (generators, batteries...)
  config   → Loaded ESFEXConfig (None if not loaded)
  window   → MainWindow (full GUI access)
  np       → numpy

── Examples ───────────────────────────────────────────────────
  # Load and run a simulation
  cfg = esfex.load_config("my_config.yaml")
  results = esfex.run(cfg, "output/", years=5)

  # Load existing results and plot
  r = esfex.load_results("results/system_Cuba.h5")
  esfex.plot.generation_mix(r, 2025)

  # Batch parameter sweep
  cfg = esfex.load_config("config.yaml")
  for discount in [0.05, 0.08, 0.10]:
      cfg.systems["main"].discount_rate = discount
      res = esfex.run(cfg, f"output_dr{discount}/")
      print(f"DR={discount}: cost={res[-1].objective:.0f}")

  # Quick sensitivity analysis
  result = esfex.sensitivity("config.yaml", n_samples=64)
  result.to_csv("sobol_indices.csv")
"""
        print(text)

    def __repr__(self) -> str:
        return "ESFEXAPI — type esfex.help() for usage"


# Module-level singleton
esfex = ESFEXAPI()
