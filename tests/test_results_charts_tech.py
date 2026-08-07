"""Technology grouping in the results charts.

Regression guard: Grid Builder systems declare generators by ``fuel``/``type``
with NO ``TechnologyConfig`` catalog. The catalog-based resolver must then fall
back to the generator's own fuel so each generator groups by its real
technology (Solar, Wind, Coal, Nuclear, …) instead of collapsing into a single
``thermal``/``Other`` bucket.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from esfex.visualization.panels import results_charts as rc


def _gen(name, fuel, gtype):
    return {"name": name, "fuel": fuel, "type": gtype, "technology": "tech_x"}


# A Grid-Builder-style fleet: real fuels, non-English names, empty tech catalog.
FLEET = [
    _gen("Hokkaido/泊発電所", "Nuclear", "Non-renewable"),
    _gen("Hokkaido/苫東厚真火力発電所", "Coal", "Non-renewable"),
    _gen("Hokkaido/石狩湾新港発電所", "Natural_gas", "Non-renewable"),
    _gen("Hokkaido/京極発電所", "Water", "Renewable"),
    _gen("Hokkaido/森地熱発電所", "Geothermal", "Renewable"),
    _gen("Hokkaido/Setana wind farm", "Wind", "Renewable"),
    _gen("Hokkaido/Tomatoh Solar", "Sun", "Renewable"),
    _gen("Hokkaido/知内発電所", "Fuel_oil", "Non-renewable"),
    _gen("Hokkaido/紋別バイオマス", "Biomass", "Renewable"),
    _gen("Hokkaido/pumped hydro", "Other", "Storage"),
]


def test_fuel_fallback_groups_by_real_technology():
    """With no technology catalog, generators resolve via their fuel."""
    gtm = rc._build_gen_tech_map(FLEET, [])  # empty tech_configs
    assert gtm, "map must not be empty when generators carry a fuel"

    expected = {
        "Hokkaido/泊発電所": ("Nuclear", "thermal"),
        "Hokkaido/苫東厚真火力発電所": ("Coal", "thermal"),
        "Hokkaido/石狩湾新港発電所": ("Gas", "thermal"),
        "Hokkaido/京極発電所": ("Hydro", "renewable"),
        "Hokkaido/森地熱発電所": ("Geothermal", "renewable"),
        "Hokkaido/Setana wind farm": ("Wind", "renewable"),
        "Hokkaido/Tomatoh Solar": ("Solar", "renewable"),
        "Hokkaido/知内発電所": ("Fuel oil", "thermal"),
        "Hokkaido/紋別バイオマス": ("Biomass", "renewable"),
        "Hokkaido/pumped hydro": ("Storage", "storage_discharge"),
    }
    for name, (label, cat) in expected.items():
        info = rc._resolve_gen_tech(name, gtm)
        assert info is not None, f"{name} unresolved"
        assert info["label"] == label, f"{name}: {info['label']} != {label}"
        assert info["category"] == cat, f"{name}: {info['category']} != {cat}"


def test_no_generator_collapses_to_other():
    """None of a real, fuel-tagged fleet should land in the 'Other' bucket."""
    gtm = rc._build_gen_tech_map(FLEET, [])
    labels = {rc._resolve_gen_tech(g["name"], gtm)["label"] for g in FLEET}
    assert "Other" not in labels


def test_real_technology_catalog_still_wins():
    """When a TechnologyConfig catalog resolves the generator, it is used
    verbatim — the fuel fallback only applies to unmapped generators."""
    gens = [{"name": "Cuba/WT1", "fuel": "Wind", "type": "Renewable",
             "technology": "tech_wind"}]
    techs = [{"key": "Cuba__tech_wind", "name": "Cuba/Wind Turbine",
              "color": "#123456", "type": "renewable"}]
    gtm = rc._build_gen_tech_map(gens, techs)
    info = rc._resolve_gen_tech("Cuba/WT1", gtm)
    assert info["label"] == "Wind Turbine"   # from catalog, not the fuel bucket
    assert info["color"] == "#123456"


def test_fuel_tech_info_helper():
    info = rc._fuel_tech_info({"name": "x", "fuel": "Coal", "type": "Non-renewable"})
    assert info["label"] == "Coal"
    assert info["category"] == "thermal"
    assert info["color"]  # a colour was assigned
