"""Sobol Global Sensitivity Analysis engine for ESFEX.

Supports two modes:
- LP-level: perturb objective/RHS coefficients in parsed .lp file, re-solve with scipy
- Config-level: vary YAML config parameters, re-run full optimization
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
import yaml

logger = logging.getLogger(__name__)

# Default parameter ranges (multipliers)
_DEFAULT_BOUNDS = (0.5, 2.0)

# KPI names
KPI_NAMES = [
    "total_cost",
    "inv_gen_total",
    "inv_bat_total",
    "curtailment",
    "load_shedding",
]

# Config-level parameter definitions. Keys follow ScenarioMultipliers; see
# _apply_config_multipliers for the fields each one scales. demand_growth
# scales the demand level (demand_scale): a deterministic run has no demand
# growth to vary. fuel_price_growth has no counterpart (generator fuel costs
# do not escalate by year), so it is not offered.
CONFIG_PARAMETERS = [
    ("invest_cost_renewables", "RE Investment Cost", (0.5, 2.0)),
    ("invest_cost_storage", "Storage Investment Cost", (0.5, 2.0)),
    ("invest_cost_conventional", "Conv. Investment Cost", (0.5, 2.0)),
    ("invest_cost_transmission", "Transmission Inv. Cost", (0.5, 2.0)),
    ("fuel_cost", "Fuel Cost", (0.5, 3.0)),
    ("maintenance_cost", "Maintenance Cost", (0.5, 2.0)),
    ("demand_growth", "Demand Level", (0.8, 1.5)),
    ("carbon_price", "Carbon Price", (0.0, 3.0)),
]


@dataclass
class SensitivityParameter:
    """A parameter to vary in sensitivity analysis."""

    name: str
    key: str
    lower_bound: float = 0.5
    upper_bound: float = 2.0
    category: str = "objective"  # "objective", "rhs", or "config"


@dataclass
class SobolResult:
    """Results from a Sobol analysis run."""

    parameters: list[str] = field(default_factory=list)
    kpi_names: list[str] = field(default_factory=list)
    S1: dict[str, np.ndarray] = field(default_factory=dict)
    ST: dict[str, np.ndarray] = field(default_factory=dict)
    S1_conf: dict[str, np.ndarray] = field(default_factory=dict)
    ST_conf: dict[str, np.ndarray] = field(default_factory=dict)
    n_samples: int = 0
    n_evaluations: int = 0
    # Evaluations without a finite value per KPI; their indices are NaN
    n_failed: dict[str, int] = field(default_factory=dict)

    def to_csv(self, filepath: str | Path) -> None:
        """Export Sobol indices to CSV file."""
        import csv

        filepath = Path(filepath)
        with open(filepath, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["KPI", "Parameter", "S1", "S1_conf", "ST", "ST_conf"])
            for kpi in self.kpi_names:
                for i, param in enumerate(self.parameters):
                    writer.writerow([
                        kpi,
                        param,
                        f"{self.S1[kpi][i]:.6f}",
                        f"{self.S1_conf[kpi][i]:.6f}",
                        f"{self.ST[kpi][i]:.6f}",
                        f"{self.ST_conf[kpi][i]:.6f}",
                    ])
        logger.info(f"Sobol indices exported to {filepath}")


class SensitivityEngine:
    """Orchestrates Sobol sensitivity analysis."""

    def __init__(
        self,
        mode: str,
        parameters: list[SensitivityParameter],
        kpi_names: list[str] | None = None,
        n_base_samples: int = 128,
        years: int | None = None,
    ):
        self.mode = mode  # "lp" or "config"
        self.parameters = parameters
        self.kpi_names = kpi_names or list(KPI_NAMES)
        self.n_base_samples = n_base_samples
        # Years per config-mode run (None: the `esfex run` default, 25)
        self.years = years

    @property
    def problem(self) -> dict:
        """SALib problem definition."""
        return {
            "num_vars": len(self.parameters),
            "names": [p.name for p in self.parameters],
            "bounds": [[p.lower_bound, p.upper_bound] for p in self.parameters],
        }

    @property
    def n_evaluations(self) -> int:
        """Total number of model evaluations required.

        generate_samples uses Saltelli with calc_second_order=False, which
        produces N*(D+2) rows (not N*(2D+2)); this must match so progress
        reporting agrees with the actual loop count.
        """
        D = len(self.parameters)
        return self.n_base_samples * (D + 2)

    def generate_samples(self) -> np.ndarray:
        """Generate the Sobol (Saltelli sequence) sample matrix.

        Returns array of shape (N*(D+2), D) — ``calc_second_order=False`` — where
        each row is a parameter set. Uses ``SALib.sample.sobol``, the successor
        to the deprecated ``SALib.sample.saltelli``.
        """
        from SALib.sample import sobol

        return sobol.sample(self.problem, self.n_base_samples, calc_second_order=False)

    def run_lp_analysis(
        self,
        lp_path: str,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> SobolResult:
        """Run LP-level Sobol analysis.

        Parses the LP file once, then perturbs and re-solves for each sample.
        """
        from esfex.sensitivity.lp_parser import (
            extract_kpis,
            parse_lp_file,
            perturb_and_solve,
            solve_lp,
        )

        if progress_callback:
            progress_callback(0, self.n_evaluations, "Parsing LP file...")

        model = parse_lp_file(lp_path)
        samples = self.generate_samples()
        n_eval = samples.shape[0]

        # Map parameters to their groups
        obj_groups = model.get_objective_groups()
        rhs_groups = model.get_rhs_groups()

        # Evaluate all samples
        evaluations: dict[str, list[float]] = {kpi: [] for kpi in self.kpi_names}

        for i in range(n_eval):
            if progress_callback:
                progress_callback(i + 1, n_eval, f"Solving perturbation {i + 1}/{n_eval}")

            # Build multiplier dicts from sample row
            obj_mults: dict[str, float] = {}
            rhs_mults: dict[str, float] = {}

            for j, param in enumerate(self.parameters):
                mult = samples[i, j]
                if param.category == "objective" and param.key in obj_groups:
                    obj_mults[param.key] = mult
                elif param.category == "rhs" and param.key in rhs_groups:
                    rhs_mults[param.key] = mult

            kpis = perturb_and_solve(model, obj_mults, rhs_mults)
            for kpi_name in self.kpi_names:
                evaluations[kpi_name].append(kpis.get(kpi_name, float("nan")))

        return self._analyze(samples, evaluations, n_eval)

    def run_config_analysis(
        self,
        base_config_path: str,
        output_dir: str,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> SobolResult:
        """Run config-level Sobol analysis.

        Creates modified configs and runs full simulations for each sample.
        """
        if progress_callback:
            progress_callback(0, self.n_evaluations, "Generating samples...")

        with open(base_config_path) as f:
            base_config = yaml.safe_load(f)

        samples = self.generate_samples()
        n_eval = samples.shape[0]
        output_base = Path(output_dir) / "sensitivity"
        output_base.mkdir(parents=True, exist_ok=True)

        evaluations: dict[str, list[float]] = {kpi: [] for kpi in self.kpi_names}

        for i in range(n_eval):
            if progress_callback:
                progress_callback(i + 1, n_eval, f"Running simulation {i + 1}/{n_eval}")

            # Create modified config
            config = _apply_config_multipliers(base_config, self.parameters, samples[i])

            # Write temp config
            tmp = tempfile.NamedTemporaryFile(
                suffix=".yaml", prefix=f"sa_run_{i}_", delete=False, dir=str(output_base),
            )
            tmp_path = tmp.name
            tmp.close()
            with open(tmp_path, "w") as f:
                yaml.safe_dump(config, f, default_flow_style=False)

            # Run simulation
            run_output = str(output_base / f"run_{i:04d}")
            kpis = _run_simulation_and_extract(tmp_path, run_output, self.years)

            for kpi_name in self.kpi_names:
                evaluations[kpi_name].append(kpis.get(kpi_name, float("nan")))

            # Clean up temp config
            Path(tmp_path).unlink(missing_ok=True)

        return self._analyze(samples, evaluations, n_eval)

    def _analyze(
        self,
        samples: np.ndarray,
        evaluations: dict[str, list[float]],
        n_eval: int,
    ) -> SobolResult:
        """Compute Sobol indices from evaluations."""
        from SALib.analyze import sobol

        result = SobolResult(
            parameters=[p.name for p in self.parameters],
            kpi_names=list(self.kpi_names),
            n_samples=self.n_base_samples,
            n_evaluations=n_eval,
        )

        n_params = len(self.parameters)
        for kpi_name in self.kpi_names:
            Y = np.array(evaluations[kpi_name], dtype=float)

            # The Saltelli design needs every evaluation: a failed run (or an
            # infeasible LP) cannot be dropped, and any stand-in value would
            # bias the indices, so the KPI gets no indices.
            n_failed = int(np.count_nonzero(~np.isfinite(Y)))
            result.n_failed[kpi_name] = n_failed
            if n_failed:
                logger.warning(
                    "KPI %s: %d of %d evaluations have no finite value; "
                    "its Sobol indices are not computed (NaN)",
                    kpi_name, n_failed, n_eval,
                )
                nan = np.full(n_params, np.nan)
                result.S1[kpi_name] = nan.copy()
                result.ST[kpi_name] = nan.copy()
                result.S1_conf[kpi_name] = nan.copy()
                result.ST_conf[kpi_name] = nan.copy()
                continue

            # Check for zero variance
            if np.std(Y) < 1e-15:
                result.S1[kpi_name] = np.zeros(n_params)
                result.ST[kpi_name] = np.zeros(n_params)
                result.S1_conf[kpi_name] = np.zeros(n_params)
                result.ST_conf[kpi_name] = np.zeros(n_params)
                continue

            Si = sobol.analyze(
                self.problem, Y, calc_second_order=False, print_to_console=False,
            )
            result.S1[kpi_name] = Si["S1"]
            result.ST[kpi_name] = Si["ST"]
            result.S1_conf[kpi_name] = Si["S1_conf"]
            result.ST_conf[kpi_name] = Si["ST_conf"]

        return result


def get_lp_parameters(lp_path: str) -> list[SensitivityParameter]:
    """Auto-detect available parameters from an LP file.

    Returns a list of SensitivityParameter with default bounds.
    """
    from esfex.sensitivity.lp_parser import parse_lp_file

    model = parse_lp_file(lp_path)
    params: list[SensitivityParameter] = []

    # Objective coefficient groups
    for group_name in sorted(model.get_objective_groups()):
        display = group_name.replace("_", " ").title()
        params.append(SensitivityParameter(
            name=display, key=group_name,
            lower_bound=0.5, upper_bound=2.0, category="objective",
        ))

    # RHS groups
    for group_name in sorted(model.get_rhs_groups()):
        display = group_name.replace("_", " ").title()
        params.append(SensitivityParameter(
            name=display, key=group_name,
            lower_bound=0.8, upper_bound=1.5, category="rhs",
        ))

    return params


def get_config_parameters() -> list[SensitivityParameter]:
    """Return predefined config-level parameters."""
    return [
        SensitivityParameter(
            name=display, key=key,
            lower_bound=bounds[0], upper_bound=bounds[1],
            category="config",
        )
        for key, display, bounds in CONFIG_PARAMETERS
    ]


def _iter_components(container) -> list[dict]:
    """Yield component dicts whether the container is a list or a name->dict map.

    ESFEX configs (e.g. cuba.yaml) organize generators/technologies/batteries
    as name-keyed dicts; older/simpler configs may use lists.
    """
    if isinstance(container, dict):
        return [v for v in container.values() if isinstance(v, dict)]
    if isinstance(container, list):
        return [v for v in container if isinstance(v, dict)]
    return []


def _scale_field(obj: dict, key: str, mult: float) -> None:
    """Multiply a scalar or per-element-list numeric field in place."""
    if key not in obj or obj[key] is None:
        return
    v = obj[key]
    if isinstance(v, list):
        obj[key] = [x * mult if isinstance(x, (int, float)) else x for x in v]
    elif isinstance(v, (int, float)) and not isinstance(v, bool):
        obj[key] = v * mult


def _is_renewable(component: dict) -> bool:
    """Classify a generator/technology as variable renewable (solar/wind/PV)."""
    hints = " ".join(
        str(component.get(k, "")) for k in ("type", "technology", "fuel", "name")
    ).lower()
    return any(t in hints for t in ("solar", "wind", "pv", "photovolt"))


def _apply_config_multipliers(
    base_config: dict,
    parameters: list[SensitivityParameter],
    sample_row: np.ndarray,
) -> dict:
    """Apply parameter multipliers to a config dict.

    Scales the cost fields where investment actually lives in the current
    schema: new capacity is invested through ``technologies`` (and
    ``battery_technologies``); ``generators``/``batteries`` describe existing
    units. Components may be name-keyed dicts or lists; both are handled.
    The fields follow the stochastic scenario multipliers, except
    ``demand_growth``, which scales the demand level (``demand_scale``).
    A key without a mapping (e.g. ``fuel_price_growth``) raises ValueError.
    """
    import copy
    config = copy.deepcopy(base_config)

    multipliers = {p.key: float(sample_row[i]) for i, p in enumerate(parameters)}
    unknown = set(multipliers) - {key for key, _, _ in CONFIG_PARAMETERS}
    if unknown:
        raise ValueError(f"Config parameters without a config field: {sorted(unknown)}")
    re_mult = multipliers.get("invest_cost_renewables")
    conv_mult = multipliers.get("invest_cost_conventional")
    fuel_mult = multipliers.get("fuel_cost")
    storage_mult = multipliers.get("invest_cost_storage")
    trans_mult = multipliers.get("invest_cost_transmission")
    maint_mult = multipliers.get("maintenance_cost")
    demand_mult = multipliers.get("demand_growth")
    carbon_mult = multipliers.get("carbon_price")

    # Inter-system links are expanded at their investment_cost_per_MW
    if trans_mult is not None:
        for link in (config.get("meta_network") or {}).get("systems_links") or []:
            if isinstance(link, dict):
                for key in ("investment_cost_per_MW", "investment_cost_per_mw"):
                    _scale_field(link, key, trans_mult)

    for sys_config in config.get("systems", {}).values():
        if not isinstance(sys_config, dict):
            continue

        # Investment cost of generation: split renewable vs conventional.
        # New capacity lives in `technologies`; existing units in `generators`.
        for comp_key in ("technologies", "generators"):
            for comp in _iter_components(sys_config.get(comp_key)):
                mult = re_mult if _is_renewable(comp) else conv_mult
                if mult is not None:
                    _scale_field(comp, "invest_cost", mult)
                if fuel_mult is not None:
                    _scale_field(comp, "fuel_cost", fuel_mult)

        # Storage investment cost: battery_technologies (investable) + batteries.
        if storage_mult is not None:
            for comp_key in ("battery_technologies", "batteries"):
                for bat in _iter_components(sys_config.get(comp_key)):
                    for field_key in ("invest_cost", "invest_cost_power",
                                      "invest_cost_energy", "invest_cost_capacity"):
                        _scale_field(bat, field_key, storage_mult)

        # Maintenance cost ($/MWh) of every unit type, as in the scenarios
        if maint_mult is not None:
            for comp_key in ("generators", "technologies", "batteries",
                             "battery_technologies"):
                for comp in _iter_components(sys_config.get(comp_key)):
                    _scale_field(comp, "maintenance_cost", maint_mult)

        # Transmission investment: corridor cost per node and the line and
        # transformer costs of development-zone connections
        if trans_mult is not None:
            nodes = sys_config.get("nodes")
            if isinstance(nodes, dict):
                _scale_field(nodes, "transference_invest_cost", trans_mult)
            for zone in _iter_components(sys_config.get("development_zones")):
                _scale_field(zone, "line_cost_per_mw_km", trans_mult)
                _scale_field(zone, "transformer_cost_per_mw", trans_mult)

        # Demand level: demand_scale multiplies the demand read from file
        if demand_mult is not None:
            sys_config["demand_scale"] = float(sys_config.get("demand_scale") or 1.0) * demand_mult

        # Carbon price: penalties.co2_cost (keys are case-insensitive)
        if carbon_mult is not None:
            penalties = sys_config.setdefault("penalties", {})
            if isinstance(penalties, dict):
                keys = [k for k in penalties if str(k).lower() == "co2_cost"]
                if not keys:
                    from esfex.config.schema import PenaltiesConfig
                    penalties["co2_cost"] = PenaltiesConfig().co2_cost
                    keys = ["co2_cost"]
                for k in keys:
                    _scale_field(penalties, k, carbon_mult)

    return config


def _run_simulation_and_extract(
    config_path: str, output_dir: str, years: int | None = None,
) -> dict[str, float]:
    """Run a single ESFEX simulation and extract KPIs from results."""
    kpis = {kpi: float("nan") for kpi in KPI_NAMES}

    cmd = [sys.executable, "-m", "esfex.cli", "run",
           "--config", config_path, "--output", output_dir]
    if years is not None:
        cmd += ["--years", str(years)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)

        if result.returncode != 0:
            logger.warning(f"Simulation failed: {result.stderr[:200]}")
            return kpis

        # Try to extract KPIs from HDF5 results
        kpis = _extract_kpis_from_results(output_dir)

    except subprocess.TimeoutExpired:
        logger.warning(f"Simulation timed out for {config_path}")
    except Exception as e:
        logger.warning(f"Simulation error: {e}")

    return kpis


def _extract_kpis_from_results(output_dir: str) -> dict[str, float]:
    """Extract KPI values from the HDF5 results written by an ESFEX run.

    A run writes one ``<system>_<timestamp>.h5`` file into its output
    directory, with year-indexed aggregates under ``summary_results``
    (total_cost, loss_of_load), and per-year groups under
    ``detailed_results`` holding curtailment and the master's investments
    (``investment_*`` attributes, new capacity of that year). If the
    directory holds files of earlier runs, only the newest is read.
    """
    import h5py

    kpis = {kpi: float("nan") for kpi in KPI_NAMES}
    output_path = Path(output_dir)

    h5_files = list(output_path.glob("*.h5")) or list(output_path.glob("**/*.h5"))
    if not h5_files:
        logger.warning(f"No HDF5 results found under {output_dir}")
        return kpis
    h5_files = [max(h5_files, key=lambda p: p.stat().st_mtime)]

    total_cost = 0.0
    load_shedding = 0.0
    curtailment = 0.0
    inv_gen = 0.0
    inv_bat = 0.0
    found_summary = False
    found_curt = False
    found_years = False

    for h5_path in h5_files:
        try:
            with h5py.File(h5_path, "r") as f:
                summary = f.get("summary_results")
                if summary is not None:
                    if "total_cost" in summary:
                        total_cost += float(np.sum(summary["total_cost"][...]))
                        found_summary = True
                    if "loss_of_load" in summary:
                        load_shedding += float(np.sum(summary["loss_of_load"][...]))

                detailed = f.get("detailed_results")
                if detailed is not None:
                    for yname in detailed:
                        yg = detailed[yname]
                        if not isinstance(yg, h5py.Group):
                            continue
                        found_years = True
                        if "curtailment" in yg:
                            curtailment += float(np.sum(yg["curtailment"][...]))
                            found_curt = True
                        # Investments below 0.1 MW are not written (count as 0)
                        for key, val in yg.attrs.items():
                            if key.startswith("investment_tech_investment_power_"):
                                inv_gen += float(val)
                            elif key.startswith("investment_bat_tech_investment_power_"):
                                inv_bat += float(val)
        except Exception as e:
            logger.warning(f"Error reading {h5_path}: {e}")

    if found_summary:
        kpis["total_cost"] = total_cost
        kpis["load_shedding"] = load_shedding
    if found_curt:
        kpis["curtailment"] = curtailment
    if found_years:
        kpis["inv_gen_total"] = inv_gen
        kpis["inv_bat_total"] = inv_bat

    return kpis
