"""Every fuel bucket of the UC charts (dispatch, marginal technology,
ramps) has its own colour from the theme palette.

"Gas" and "Hydrogen" used to miss their palette keys ("Gas turbine",
"Hydrogen_FuelCell") and fall to the grey of "Other", so the marginal
technology chart could not tell them apart.
"""
from __future__ import annotations

from esfex.visualization.panels.results_charts import (
    _SERIES_LABEL_KEYS,
    _bucket_color,
)
from esfex.visualization.theme import (
    get_generation_colors,
    get_generation_default_color,
)

_BUCKETS = ["Solar", "Wind", "Hydro", "Biomass", "Geothermal", "OTEC",
            "Nuclear", "Hydrogen", "Fuel oil", "Gas", "Coal"]


def test_buckets_have_distinct_palette_colors():
    pal, default = get_generation_colors(), get_generation_default_color()
    colors = {b: _bucket_color(b, pal, default) for b in _BUCKETS}
    assert default not in colors.values(), colors
    assert len(set(colors.values())) == len(colors), colors
    assert _bucket_color("Other", pal, default) == default


def test_buckets_are_the_ones_the_charts_label():
    # Guard against a new bucket that the colour map does not know.
    assert set(_BUCKETS) - {"OTEC"} <= set(_SERIES_LABEL_KEYS)
