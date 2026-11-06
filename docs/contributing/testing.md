# Testing

## Running Python Tests

### Full Test Suite

```bash
pytest
```

`pyproject.toml` sets `testpaths = ["tests"]` and `addopts = "-v --tb=short"`.

### Without Julia

```bash
pytest -m "not julia"
```

Tests that build and solve optimization models are marked `julia`. When `juliacall` cannot be imported, `tests/conftest.py` skips them automatically. CI runs the suite with `-m "not julia"`.

### With Coverage

```bash
pytest --cov=esfex --cov-report=html
open htmlcov/index.html
```

### Specific Test Files

```bash
pytest tests/test_schema.py            # Pydantic config models
pytest tests/test_loader.py            # YAML loading
pytest tests/test_runner.py            # Orchestrator (non-Julia parts)
pytest tests/test_cli.py               # CLI
pytest tests/test_plugins.py           # Plugin framework + plugins dialog
pytest tests/test_sensitivity_engine.py
pytest tests/test_docs_examples.py     # YAML examples in docs/
```

### Specific Test Functions

```bash
pytest tests/test_plugins.py::TestDiscovery
pytest tests/test_custom_constraints.py -k "resolve"
```

### Editable installs

If imports fail under pytest with "unknown location" errors, reinstall non-editable (`pip install ".[dev]"`). CI does the same, because the setuptools editable finder can resolve some sub-modules (for example `esfex.visualization.i18n`) as namespace packages.


---


## Test Layout

There is one flat `tests/` directory with about 110 `test_*.py` files, grouped by module name (`test_schema.py`, `test_loader.py`, `test_serializer.py`, `test_zones.py`, …). There is no `tests/fixtures/` directory; tests build their inputs in code or under pytest's `tmp_path`.

### Unit Tests

Most tests construct config objects directly and need neither Julia nor a display. Example from `tests/test_custom_constraints.py`:

```python
from esfex.config.schema import CustomConstraintConfig

def test_invalid_sense_rejected():
    with pytest.raises(ValidationError):
        CustomConstraintConfig(name="x", sense="<", terms=[...])
```

### Model Tests (`julia` marker)

End-to-end tests build a small system in code, run the `Orchestrator`, and check the result. They reuse helper builders from other test modules: `_make_generator` and `_make_esfex_config` in `tests/test_runner.py`, `_make_system_config` and `_make_technology` in `tests/test_zones.py`. A minimal sketch in the style of `tests/test_master_co2.py`:

```python
import sys
import numpy as np
import pytest

sys.path.insert(0, "tests")
from test_runner import _make_esfex_config, _make_generator
from test_zones import _make_system_config

@pytest.mark.julia
def test_runs_one_year(tmp_path):
    from esfex.runner import Orchestrator

    demand = tmp_path / "demand.csv"
    np.savetxt(demand, np.full(8760, 60.0))
    diesel = _make_generator("Diesel", "Non-renewable", "Diesel", [150.0])
    sysc = _make_system_config(num_nodes=1, coords=[(21.0, -80.0)],
                               generators={"diesel": diesel})
    sysc = sysc.model_copy(update={"demand_path": str(demand)})
    cfg = _make_esfex_config(sysc, simulation_mode="economic_dispatch")

    results = Orchestrator(cfg, output_dir=tmp_path / "out").run(years=1)
    assert results[0].feasible
```

`Orchestrator.run()` takes `years` and `start_year`. Other model tests include `test_power_system_parity.py`, `test_master_problem.py`, `test_stochastic_master.py`, `test_battery_soc_carryover.py`, `test_zones.py` and `test_network_reduction.py`.

### GUI Tests

Studio tests (`test_gui_model.py`, `test_serializer.py`, `test_studio_qt_runtime.py`, `test_undo.py`, …) have no special marker. Some create Qt widgets and need a display; on a headless Linux machine run them under Xvfb, as CI does:

```bash
xvfb-run -a pytest tests/ -m "not julia"
```

The plugins-dialog tests (`tests/test_plugins.py::TestPluginsDialog`) mock the Qt widgets and need no display.

### Documentation Examples

`tests/test_docs_examples.py` parses every fenced YAML block in `docs/`, checks the keys against the schema and the loader, and loads complete examples with `load_config`. Pages listed in its `KNOWN_STALE` set are expected to fail (xfail). Run it after editing a YAML example in the docs.


---


## Pytest Fixtures

Defined in `tests/conftest.py`:

| Fixture | Returns |
|---------|---------|
| `project_root` | Repository root |
| `sample_demand` | 24 h × 2 nodes demand array |
| `sample_solar_availability` | 24 h × 2 nodes solar profile |
| `sample_wind_availability` | 24 h × 2 nodes wind profile (seeded random) |

`configs_dir`, `fixtures_dir` and `small_system_config` point to directories that do not exist in the repository and are not used by any test.


---


## Running Julia Tests

```bash
julia --project=src/esfex/julia -e 'using Pkg; Pkg.test()'
```

Or from the Julia REPL:

```julia
] activate src/esfex/julia
] test
```

The test dependencies (Ipopt, JuMP, Test) are declared in `src/esfex/julia/test/Project.toml`.

### Julia Test Structure

`src/esfex/julia/test/runtests.jl` is a single file with one top-level testset:

```julia
using Test
using ESFEX

@testset "ESFEX" begin
    @testset "build_ybus" begin ... end          # one testset per function
    @testset "create_optimizer" begin ... end
    @testset "data structs" begin ... end
    @testset "integration" begin
        # small models built with create_power_system and solved, e.g.
        # "economic dispatch (single bus)", "reserves are headroom, not energy",
        # "ramp rate is per hour, scaled to the time step"
    end
end
```

Unit testsets cover the self-contained pieces (Y-bus and incidence matrix, line flows, solver factory, temporal mappings, NPV, representative-day selection, data structs). The `integration` block builds small `PowerSystemInput`s directly (a local `mkgen` helper fills the positional `GeneratorConfig` fields) and checks solved results. Larger end-to-end runs live in the Python `julia`-marked tests.


---


## Writing Tests

### Testing Optimization Results

Use tolerance-based assertions for solver results:

```python
assert results[0].load_shed == pytest.approx(0.0, abs=1e-6)
assert built_mw == pytest.approx(60.0, rel=1e-3)
```

### Testing Configuration Validation

```python
def test_negative_emission_factor_rejected():
    with pytest.raises(ValidationError):
        FuelConfig(name="Diesel", emission_factor=-0.1)
```

### Pattern: New Configuration Field

1. Test the default and an explicit value on the Pydantic model (`tests/test_schema.py`).
2. If `config/loader.py` converts the section by hand, test that the key survives `load_config` (`tests/test_loader.py`).
3. If the field reaches Julia, add a `julia`-marked test that shows its effect on a small model.

### Pattern: New Constraint in Julia

Add a small testset to the `integration` block of `runtests.jl` that builds a `PowerSystemInput`, calls `create_power_system`, solves and checks the binding value. If the constraint depends on Python-side preprocessing, also add a `julia`-marked Python test that runs the `Orchestrator` on a minimal config.

### Pattern: Plugin Hook

`tests/test_plugins.py` shows how to write a plugin into `tmp_path` and point the manager at it, by patching `esfex.plugins.manager._USER_PLUGINS_DIR` and `_STATE_FILE` or by setting `ESFEX_PLUGIN_PATH`. Call `reset_plugin_manager()` between tests, since the manager is a process-wide singleton.


---


## Coverage

### Generating Coverage

```bash
pytest --cov=esfex --cov-report=term-missing
pytest --cov=esfex --cov-report=html
pytest --cov=esfex --cov-report=xml      # format uploaded by CI
```

### What is measured

`[tool.coverage.run] omit` in `pyproject.toml` excludes code that unit tests cannot reach: the Qt windows, panels and dialogs, the Julia-bound glue (`runner.py`, `bridge/adapters.py`, `bridge/converters.py`, `analysis/native_ac_bridge.py`) and the heavy data pipelines. Coverage of those files is not reported, even when `julia`-marked tests exercise them. `[tool.coverage.report] exclude_also` drops `if TYPE_CHECKING:`, `raise NotImplementedError`, `__main__` guards and abstract methods.

Coverage is informational: `codecov.yml` marks both the project and patch status as `informational: true`, so coverage never fails CI or blocks a merge. There are no per-module targets.


---


## Continuous Integration

`.github/workflows/ci.yml` runs on pushes and pull requests to `main` and on published releases:

| Job | What it does |
|-----|--------------|
| `lint` | `ruff check --exit-zero src tests` on Python 3.11. Informational: findings appear as annotations but never fail the build. No mypy step. |
| `test` | Ubuntu, Python 3.10, 3.11 and 3.12. Installs Qt system libraries and Xvfb, runs `pip install ".[dev]"` (non-editable), then `xvfb-run -a pytest tests/ -m "not julia" --cov=esfex`. Coverage from 3.11 is uploaded to Codecov. |
| `julia` | Julia 1.11: builds `src/esfex/julia` and runs its `Pkg.test` suite with coverage. |
| `build` | Builds sdist and wheel and checks them with `twine check` (after `test`). |
| `publish` | On a published GitHub release only: publishes to PyPI with trusted publishing. |

There are no macOS or Windows jobs, and the `julia`-marked Python tests do not run in CI. Run `pytest -m julia` locally before changing model code.

### Troubleshooting CI Failures

- **Solver results differ across machines**: use tolerance-based assertions instead of exact equality.
- **GUI tests fail locally on a headless machine**: run them under `xvfb-run -a`.
- **Import errors only under pytest**: see [Editable installs](#editable-installs).
