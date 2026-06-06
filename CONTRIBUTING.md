# Contributing to ESFEX

Thank you for your interest in contributing! This document describes the
**requirements for acceptable contributions**. For environment setup and deeper
guides it links to the pages under `docs/contributing/`.

By contributing, you agree that your contributions are licensed under the
project's [Apache-2.0 License](LICENSE).

## How to contribute

1. **Open an issue first** for bugs and features so the change can be discussed
   and tracked. Bug reports and feature requests go to the
   [GitHub issue tracker](https://github.com/Net-Zero-Horizon/ESFEX/issues).
2. **Fork and branch** from `main`; keep each pull request focused on a single
   change.
3. **Open a pull request** against `main` with a clear description that
   references the relevant issue.

See [Development Setup](docs/contributing/development-setup.md) to get a working
environment (`pip install -e ".[dev]"` plus the Julia project).

## Requirements for acceptable contributions

A contribution is acceptable when it meets **all** of the following.

### Coding standard

- Python code follows **PEP 8** as enforced by
  **[Ruff](https://docs.astral.sh/ruff/)**. The exact rule set and formatting
  are defined in [`pyproject.toml`](pyproject.toml) (`[tool.ruff]` /
  `[tool.black]`): rule groups `E`, `F`, `I` (import sorting), `N` (naming) and
  `W`, with a line length of **100**.
- Run before submitting:
  ```bash
  ruff format src/ tests/      # format
  ruff check src/ tests/       # lint
  ```
- New or changed code should carry **type hints**; they are checked with
  **mypy** (`[tool.mypy]` in `pyproject.toml`).
- Pull requests and pushes to `main` are scanned by **CodeQL**
  (security + quality static analysis); please review and address any new
  [Code scanning](https://github.com/Net-Zero-Horizon/ESFEX/security/code-scanning)
  alerts your change introduces.
- Match the style, naming and structure of the surrounding code.

### Tests

- All tests must pass:
  ```bash
  pytest -m "not julia"        # Python suite (enforced in CI)
  ```
  Changes to the Julia optimization core must also pass its tests:
  ```bash
  julia --project=src/esfex/julia -e 'using Pkg; Pkg.test()'
  ```
- **New features and bug fixes must include tests** that cover the change.
- See [Testing](docs/contributing/testing.md) for the full workflow and
  [Julia Development](docs/contributing/julia-development.md) for the Julia side.

### Documentation and GUI wiring

- Update the relevant documentation when you change behaviour, configuration
  options or public APIs.
- Any new user-facing configuration parameter must be wired into the Studio GUI
  (schema, serializer, form widget and translations), not only the schema and
  the Julia/solver layer.

### Commits and pull requests

- Write clear, imperative commit messages and reference the issue they address
  (e.g. `Closes #123`).
- Keep pull requests small and focused; unrelated changes belong in separate
  PRs.

## Reporting security issues

Do **not** file security vulnerabilities as public issues — follow the
[Security Policy](SECURITY.md).
