"""Install metadata stays consistent across pip and conda."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def test_environment_yml_python_matches_requires_python():
    """conda users get the same Python range as pip users."""
    requires = re.search(r'^requires-python\s*=\s*"([^"]+)"',
                         (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    env = yaml.safe_load((ROOT / "environment.yml").read_text())
    python = next(d for d in env["dependencies"]
                  if isinstance(d, str) and re.match(r"python\b", d))
    assert re.sub(r"\s", "", python.removeprefix("python")) == re.sub(r"\s", "", requires)
