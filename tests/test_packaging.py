"""Install metadata stays consistent across pip and conda, and shipped
third-party code and data carry their own licences and attributions."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

try:
    import tomllib
except ImportError:  # Python 3.10
    tomllib = pytest.importorskip("tomli")

ROOT = Path(__file__).resolve().parent.parent
_RESOURCES = "src/esfex/visualization/resources"
_MODELS = "src/esfex/assets/models"


def test_environment_yml_python_matches_requires_python():
    """conda users get the same Python range as pip users."""
    requires = re.search(r'^requires-python\s*=\s*"([^"]+)"',
                         (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    env = yaml.safe_load((ROOT / "environment.yml").read_text())
    python = next(d for d in env["dependencies"]
                  if isinstance(d, str) and re.match(r"python\b", d))
    assert re.sub(r"\s", "", python.removeprefix("python")) == re.sub(r"\s", "", requires)


# ── Licences and attribution of shipped third-party code and data ───────────

def _reuse_annotations() -> list[dict]:
    return tomllib.loads((ROOT / "REUSE.toml").read_text())["annotations"]


def _paths(ann: dict) -> list[str]:
    return ann["path"] if isinstance(ann["path"], list) else [ann["path"]]


def _licence_of(relpath: str) -> str:
    """SPDX expression REUSE.toml gives a file (the last matching table wins;
    the tables name files literally, apart from the catch-all ``**``)."""
    expr = None
    for ann in _reuse_annotations():
        if "**" in _paths(ann) or relpath in _paths(ann):
            expr = ann["SPDX-License-Identifier"]
    return expr


def _spdx_ids(expr: str) -> set[str]:
    return {t for t in re.split(r"[\s()]+", expr) if t and t not in ("AND", "OR", "WITH")}


def test_reuse_licences_have_texts_and_all_texts_are_used():
    used = set()
    for ann in _reuse_annotations():
        used |= _spdx_ids(ann["SPDX-License-Identifier"])
    assert used == {p.stem for p in (ROOT / "LICENSES").glob("*.txt")}


def test_reuse_annotated_paths_exist():
    for ann in _reuse_annotations():
        for p in _paths(ann):
            if p != "**":
                assert (ROOT / p).is_file(), p


@pytest.mark.parametrize("name, expected", [
    ("demand_density_v2.grids.npz",
     {"CC0-1.0", "CC-BY-4.0", "LicenseRef-NaturalEarth-PublicDomain", "LicenseRef-UNdata",
      "LicenseRef-StatCan-Open-Licence", "LicenseRef-US-Government-Public-Domain"}),
    ("demand_density_v2.countries.csv",
     {"LicenseRef-UNdata", "LicenseRef-StatCan-Open-Licence",
      "LicenseRef-US-Government-Public-Domain"}),
])
def test_demand_model_data_carry_their_sources_licences(name, expected):
    assert expected <= _spdx_ids(_licence_of(f"{_MODELS}/{name}"))


def test_bundled_third_party_files_are_not_declared_apache():
    """Every Studio resource that carries a third-party notice, or is one of the
    known libraries or their images, has its own licence in REUSE.toml."""
    known = {"d3.v7.min.js", "elk.bundled.js", "leaflet.js", "leaflet.css",
             "leaflet.draw.js", "leaflet.draw.css", "plotly.min.js", "xterm.js",
             "xterm.css", "xterm-addon-fit.js", "world_countries.geojson"}
    notice = re.compile(r"copyright|@license|SPDX-License-Identifier|\(c\) \d{4}", re.I)
    for f in sorted(p for p in (ROOT / _RESOURCES).rglob("*") if p.is_file()):
        rel = f.relative_to(ROOT).as_posix()
        head = f.read_bytes()[:4000].decode("utf-8", "ignore")
        if f.name in known or f.parent.name == "images" or notice.search(head):
            assert "Apache-2.0" not in _spdx_ids(_licence_of(rel)), rel


def test_data_sources_ship_with_the_package():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())
    license_files = set(meta["project"]["license-files"])
    assert {"LICENSE", "LICENSES/*.txt", "DATA_SOURCES.md"} <= license_files
    # The package licence is an SPDX expression naming every shipped licence.
    assert set(_spdx_ids(meta["project"]["license"])) == {
        f.stem for f in (ROOT / "LICENSES").glob("*.txt")}
    manifest = (ROOT / "MANIFEST.in").read_text()
    assert "DATA_SOURCES.md" in manifest and "graft LICENSES" in manifest
    recipe = (ROOT / "recipe" / "meta.yaml").read_text()
    assert "- DATA_SOURCES.md" in recipe and "- LICENSES/" in recipe


def test_data_sources_page_is_linked_and_in_sync():
    assert "reference/data-sources.md" in (ROOT / "mkdocs.yml").read_text()
    assert "DATA_SOURCES.md" in (ROOT / "README.md").read_text()
    assert "../reference/data-sources.md" in (
        ROOT / "docs" / "contributing" / "demand-density-model.md").read_text()
    doi = re.compile(r"doi\.org/(10\.[^\s)]+)")
    root_md = (ROOT / "DATA_SOURCES.md").read_text()
    page = (ROOT / "docs" / "reference" / "data-sources.md").read_text()
    assert set(doi.findall(root_md)) == set(doi.findall(page))
    assert set(doi.findall(root_md)) >= {
        "10.1038/s41597-022-01393-4",   # NEX-GDDP-CMIP6, Thrasher et al. 2022
        "10.5258/SOTON/WP00647",        # WorldPop
        "10.25318/2510002901-eng",      # Statistics Canada table 25-10-0029-01
    }
    assert root_md.count("TODO(licence):") == page.count("TODO(licence):")
