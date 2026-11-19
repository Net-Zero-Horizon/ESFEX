"""Standard nominal system voltages.

Grid voltages are not arbitrary: they follow IEC 60038 and ANSI C84.1, plus
a few national levels. The Grid Builder uses this list to recognise a
mistyped OSM tag (138 kV for 13.8 kV) and to pick a medium-voltage level
when the data gives none.

A level on this list is not necessarily used in a given country (138 kV is
standard in Jamaica and the Dominican Republic but absent from Cuba's 220/110
kV grid), so callers check the country's data as well.
"""
from __future__ import annotations

# Nominal three-phase system voltages, kV (line to line).
STANDARD_NOMINAL_KV: tuple[float, ...] = tuple(sorted({
    # IEC 60038, 1 to 35 kV (series I, and series II: North American practice)
    3.0, 3.3, 6.0, 6.6, 10.0, 11.0, 15.0, 20.0, 22.0, 30.0, 33.0, 35.0,
    4.16, 12.47, 13.2, 13.8, 24.94, 34.5,
    # ANSI C84.1 medium voltage
    2.4, 4.8, 6.9, 8.32, 12.0, 20.78, 22.86, 23.0, 46.0,
    # IEC 60038, 35 to 230 kV, and ANSI C84.1 high voltage
    45.0, 66.0, 69.0, 110.0, 115.0, 132.0, 138.0, 150.0, 161.0, 220.0, 230.0,
    # Extra-high voltage (IEC 60038 equipment ranges; national nominal levels)
    275.0, 330.0, 345.0, 380.0, 400.0, 500.0, 735.0, 750.0, 765.0, 1000.0,
    1150.0,
    # National levels in wide use: France 63/90, Japan 77/154/187,
    # Korea 154/345, Canada 44/120, Russia 35/150.
    44.0, 63.0, 77.0, 90.0, 120.0, 154.0, 187.0,
}))

# Default medium voltage when the data tags none: the most common
# sub-transmission/primary distribution level of each practice.
MV_DEFAULT_KV = {50: 33.0, 60: 34.5}

_TOLERANCE = 1.005    # 13.8 vs 13.80; distinct from 13.2, 132 vs 138, ...


def is_standard_kv(kv: float) -> bool:
    """True if ``kv`` is a standard nominal system voltage."""
    return kv > 0 and any(
        max(kv, s) / min(kv, s) <= _TOLERANCE for s in STANDARD_NOMINAL_KV)


def mv_default_kv(frequency_hz: float) -> float:
    """Medium-voltage default for a grid at ``frequency_hz``."""
    return MV_DEFAULT_KV[60 if round(frequency_hz or 50) == 60 else 50]
