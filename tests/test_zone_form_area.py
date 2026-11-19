"""Area of a drawn development-zone polygon (round 44, M44).

The shoelace formula on a self-crossing ("bowtie") outline let its two lobes
cancel: a symmetric bowtie had area 0. The outline is repaired first
(shapely ``make_valid``) and its parts' areas added.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from esfex.visualization.panels.zone_form import _polygon_area_km2  # noqa: E402


def _pts(*latlng):
    return [SimpleNamespace(lat=lat, lng=lng) for lat, lng in latlng]


def test_simple_polygon_area_unchanged():
    square = _pts((0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0))
    # A 1° × 1° square at the equator: ~111.2 km × 111.2 km
    assert _polygon_area_km2(square) == pytest.approx(12_364, rel=1e-3)


def test_bowtie_counts_both_lobes():
    square = _polygon_area_km2(_pts((0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0)))
    # Corners in crossing order: two triangles meeting at the centre, each
    # a quarter of the square
    bowtie = _polygon_area_km2(_pts((0.0, 0.0), (1.0, 1.0), (1.0, 0.0), (0.0, 1.0)))
    assert bowtie == pytest.approx(square / 2, rel=1e-3)


def test_degenerate_polygon_is_zero():
    assert _polygon_area_km2(_pts((0.0, 0.0), (1.0, 1.0))) == 0.0
    assert _polygon_area_km2(_pts((0.0, 0.0), (1.0, 1.0), (2.0, 2.0))) == pytest.approx(0.0, abs=1e-6)
