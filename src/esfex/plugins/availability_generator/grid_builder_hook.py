"""Glue between Grid Builder and the availability_generator plugin.

The standard ``generate_availability_profiles`` function operates on a
serialised :class:`ESFEXConfig`.  Inside the Grid Builder pipeline we
have something more direct: a live ``GuiSystemState`` populated with
``GuiGeneratorInstance`` objects, each carrying its own ``latitude``,
``longitude`` and ``fuel``.  This module emits availability CSVs for
that representation in one pass and writes the resulting paths back
onto each generator's ``availability_file`` field.

Wind / solar units use the existing weather-data backends
(``solarex`` / ``windrex``).  Everything else falls back to the
synthetic profiles in :mod:`synthetic_cf`, which are cheap (no I/O)
and good enough for typical screening studies.

Weather-derived profiles come in UTC and are saved on the model clock
(:mod:`esfex.utils.model_clock`).
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import random
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional, Union

import numpy as np

from esfex.plugins.availability_generator.synthetic_cf import (
    compute_synthetic_cf,
    is_synthetic_fuel,
)
from esfex.utils.model_clock import (
    ModelClock,
    resolve_for_nodes,
    resolve_model_clock,
    weather_to_model_clock,
)

if TYPE_CHECKING:
    from esfex.visualization.data.gui_model import (
        GuiGeneratorInstance, GuiSystemState,
    )

logger = logging.getLogger(__name__)

_HOURS_PER_YEAR = 8760

_SOLAR_HINTS = frozenset({"sun", "solar", "pv", "photovoltaic"})
_WIND_HINTS = frozenset({"wind", "eolic", "eolica", "eolico"})

# Co-located generators share one weather query. Coordinates are bucketed to a
# grid cell before fetching: at 0.1° (~11 km) the cell is finer than the weather
# backend's own native resolution (Open-Meteo ≈ 11 km, ERA5 ≈ 31 km), so two
# points inside one cell return all-but-identical capacity factors anyway. This
# collapses hundreds of co-located units (e.g. units of one plant, or a clustered
# wind farm) into a handful of unique fetches.
_WEATHER_CELL_DEG = 0.1

# Bounded concurrency for the remaining unique fetches. These are network- and
# rate-limited, NOT CPU-bound, so the worker count is tuned to the API, not the
# core count. Open-Meteo's free tier throttles by *concurrent* requests, not
# just req/min: measured, ~4 concurrent stream cleanly while 8+ already draw
# HTTP 429s (which, since we no longer fabricate flat profiles, become
# all-zero/skipped generators). A CPU-based ``cpu_count - 2`` (e.g. 38 on a
# 40-core box) 429s ~90 % of requests. Keep the default low and rate-limit-safe;
# the retry-on-failure in ``_fetch_one_weather_cf`` mops up the occasional blip.
# Override with the ESFEX_AVAILABILITY_WORKERS environment variable.
_WEATHER_SAFE_WORKERS = 4



def tr(key: str, **kwargs) -> str:
    """GUI translation; English when no language is set (CLI, agents)."""
    from esfex.visualization.i18n import tr as _tr
    return _tr(key, **kwargs)


def _kind_label(kind: str) -> str:
    return tr(f"availability_generator.kind_{kind}")

def _default_max_workers() -> int:
    return _WEATHER_SAFE_WORKERS

# Retry transient weather-fetch failures (429 / 5xx / timeouts) with exponential
# backoff + jitter, since the backends issue a bare ``requests.get`` with no
# retry of their own. This lets us raise concurrency without turning the
# occasional rate-limit blip into a permanently profile-less generator.
_WEATHER_RETRIES = 3
_WEATHER_BACKOFF_S = 2.0


def _resolve_max_workers(total: int) -> int:
    """Worker count for the fetch pool: ``cpu_count - 2`` by default, never more
    than the work, env-overridable via ESFEX_AVAILABILITY_WORKERS."""
    cap = _default_max_workers()
    override = os.environ.get("ESFEX_AVAILABILITY_WORKERS")
    if override:
        try:
            cap = max(1, int(override))
        except ValueError:
            logger.warning(
                "Ignoring non-integer ESFEX_AVAILABILITY_WORKERS=%r", override)
    return max(1, min(cap, total))


def _profile_kind(canonical_fuel: str) -> str:
    """Return ``'solar'``, ``'wind'`` or ``'synthetic'``."""
    f = (canonical_fuel or "").lower()
    if f in _SOLAR_HINTS:
        return "solar"
    if f in _WIND_HINTS:
        return "wind"
    return "synthetic"


def generate_for_grid_build(
    state: "GuiSystemState",
    output_dir: Path,
    *,
    use_weather_data: bool = False,
    weather_year: int = 2023,
    weather_source: str = "open_meteo",
    progress_callback: Optional[Callable[[int, str], None]] = None,
    model_clock: Union[ModelClock, str] = "auto",
) -> dict[str, Path]:
    """Generate hourly availability CSVs for every generator in *state*.

    Parameters
    ----------
    state
        The newly-built ``GuiSystemState``.  Each generator must have
        ``latitude`` / ``longitude`` / ``fuel`` populated.
    output_dir
        Where to write CSVs (created if missing).  One file per
        generator: ``<instance_id>_availability.csv``.
    use_weather_data
        When True, wind and solar generators query the weather backend
        for their (lat, lng) — realistic, but network-bound. Queries are
        de-duplicated per ~11 km grid cell, so the cost scales with the
        number of *distinct* locations, not the generator count. With
        ``open_meteo`` one request per cell serves solar and wind, requests
        are paced to the free tier's per-minute quota, and downloaded cells
        are cached on disk (see :mod:`open_meteo_weather`). If a query fails (or the backend is unavailable),
        that wind/solar unit is left **without** an availability profile —
        no flat or synthetic value is fabricated — and the omission is
        reported. When False (fast builds) wind/solar use a flat
        0.32 / 0.20 annual factor. Non-weather fuels (thermal / hydro /
        geothermal / biomass) always use synthetic profiles.
    weather_year
        Calendar year for the weather query (only used if
        ``use_weather_data`` is True).
    weather_source
        Weather backend: ``open_meteo`` / ``nasa_power`` / ``era5_atlite``.
    progress_callback
        Optional ``callback(percent, message)``.
    model_clock
        Clock the weather profiles are saved on (they come in UTC). ``auto``
        resolves it from *state*'s nodes; the caller records the clock used
        (``temporal.model_clock``). Flat and synthetic profiles have no time
        of day and are written as they are.

    Returns
    -------
    dict
        Mapping ``instance_id → csv_path``.  ``state.generators[id]
        .availability_file`` is also updated in place.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    from esfex.visualization.workflows.grid_mapping_builder import (
        _normalize_fuel_key,
    )

    gens = list(state.generators.items())
    if not gens:
        if progress_callback:
            progress_callback(100, tr("availability_generator.progress_no_generators"))
        return {}

    # Classify once: (gid, gen, canonical_fuel, kind).
    classified = [
        (gid, gen, (canon := _normalize_fuel_key(gen.fuel)), _profile_kind(canon))
        for gid, gen in gens
    ]

    # Pre-fetch weather capacity factors for the *distinct* wind/solar locations
    # in parallel; co-located generators then reuse a cached array. This is the
    # whole point of the optimization — the slow part is the network query, and
    # there are far fewer unique cells than generators.
    weather_cache: dict[tuple, np.ndarray] = {}
    if use_weather_data:
        weather_cache = _prefetch_weather_cfs(
            classified, weather_year, weather_source, progress_callback,
        )
        nodes = list(getattr(state, "nodes", None) or [])
        if isinstance(model_clock, ModelClock):
            clock = model_clock
        elif nodes:
            clock = resolve_for_nodes(model_clock, nodes)
        else:  # no nodes: the generators' own sites decide
            clock = resolve_model_clock(
                model_clock, [(g.latitude, g.longitude) for _, g, _, _ in classified])
        logger.info("Weather availability on the model clock: %s", clock.label)
        # key = (kind, lat, lng, year, source): one shift per weather cell.
        weather_cache = {
            k: weather_to_model_clock(v, weather_source, k[2], clock,
                                      weather_year)
            for k, v in weather_cache.items()
        }

    from esfex.plugins.availability_generator.synthetic_cf import (
        compute_constant_cf,
    )

    written: dict[str, Path] = {}
    skipped: list[str] = []
    total = len(classified)
    for idx, (gid, gen, canonical, kind) in enumerate(classified):
        pct = int(100 * (idx + 1) / total)
        if progress_callback:
            progress_callback(pct, tr("availability_generator.progress_profile",
                                    i=idx + 1, n=total, gen=gid,
                                    kind=_kind_label(kind)))

        if use_weather_data and kind in ("solar", "wind"):
            cf = weather_cache.get(
                _weather_cache_key(kind, gen, weather_year, weather_source))
            if cf is None:
                # No real weather data for this location: do NOT fabricate a
                # flat capacity factor. Leave the unit without an availability
                # file and report it, so the gap is visible rather than faked.
                logger.warning(
                    "No weather data for %s (%s) — leaving it without an "
                    "availability profile.", gid, kind)
                skipped.append(gid)
                continue
        else:
            # Synthetic fuels (thermal/hydro/geothermal/biomass), or weather
            # explicitly disabled → the cheap local profile (no I/O).
            try:
                cf = _compute_cf(
                    kind, canonical, gen,
                    use_weather_data=use_weather_data,
                    weather_year=weather_year,
                    weather_source=weather_source,
                    seed=idx,
                )
            except Exception as exc:
                logger.warning(
                    "Synthetic profile failed for %s (%s): %s — flat default.",
                    gid, kind, exc,
                )
                cf = compute_constant_cf(_default_for_kind(kind))

        csv_path = output_dir / f"{gid}_availability.csv"
        np.savetxt(csv_path, cf, delimiter=",", fmt="%.6f")
        gen.availability_file = str(csv_path)
        written[gid] = csv_path

    if skipped:
        shown = ", ".join(skipped[:20]) + (" …" if len(skipped) > 20 else "")
        logger.warning(
            "%d wind/solar generator(s) had no weather data available; left "
            "without an availability profile: %s", len(skipped), shown)
    if progress_callback:
        progress_callback(100, tr("availability_generator.progress_wrote",
                                    n=len(written)))
    return written


# ── Internal helpers ────────────────────────────────────────────────


def _weather_cache_key(
    kind: str,
    gen: "GuiGeneratorInstance",
    weather_year: int,
    weather_source: str,
) -> tuple:
    """A dedup key for one weather query: ``(kind, lat, lng, year, source)``.

    Coordinates are bucketed to a ~11 km grid cell (see ``_WEATHER_CELL_DEG``).
    The unit's rating is not part of the key: ``windrex`` is called without a
    power curve, so every wind unit uses its default turbine and the capacity
    factor of a cell does not depend on the unit's rated power.
    """
    lat = round(gen.latitude / _WEATHER_CELL_DEG) * _WEATHER_CELL_DEG
    lng = round(gen.longitude / _WEATHER_CELL_DEG) * _WEATHER_CELL_DEG
    return (kind, round(lat, 3), round(lng, 3), weather_year, weather_source)


def _prefetch_weather_cfs(
    classified: list,
    weather_year: int,
    weather_source: str,
    progress_callback: Optional[Callable[[int, str], None]],
) -> dict[tuple, np.ndarray]:
    """Fetch the distinct wind/solar weather profiles concurrently.

    Returns a ``cache_key → cf`` map. Keys whose fetch fails are simply absent,
    so the caller falls back to the cheap flat default for those generators.
    """
    # One representative generator per distinct location/kind/rating.
    reps: dict[tuple, "GuiGeneratorInstance"] = {}
    kinds: dict[tuple, str] = {}
    for _gid, gen, _canonical, kind in classified:
        if kind not in ("solar", "wind"):
            continue
        key = _weather_cache_key(kind, gen, weather_year, weather_source)
        if key not in reps:
            reps[key] = gen
            kinds[key] = kind

    cache: dict[tuple, np.ndarray] = {}
    keys = list(reps)
    total = len(keys)
    if not total:
        return cache

    if weather_source == "open_meteo":
        return _prefetch_open_meteo(keys, weather_year, progress_callback)

    if progress_callback:
        progress_callback(0, tr("availability_generator.progress_fetching_weather",
                                  n=total))

    def _work(key: tuple):
        return key, _fetch_one_weather_cf(
            kinds[key], reps[key], weather_year, weather_source)

    done = 0
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=_resolve_max_workers(total)
    ) as ex:
        for fut in concurrent.futures.as_completed(
            [ex.submit(_work, k) for k in keys]
        ):
            try:
                key, cf = fut.result()
                if cf is not None:
                    cache[key] = cf
            except Exception as exc:
                logger.warning("Weather fetch failed: %s — flat fallback.", exc)
            done += 1
            if progress_callback:
                progress_callback(
                    int(100 * done / total),
                    tr("availability_generator.progress_weather_n",
                       done=done, n=total))

    logger.info(
        "Weather availability: %d unique location(s) fetched for the build.",
        len(cache),
    )
    return cache


def _prefetch_open_meteo(
    keys: list[tuple],
    weather_year: int,
    progress_callback: Optional[Callable[[int, str], None]],
) -> dict[tuple, np.ndarray]:
    """Open-Meteo path: one quota-paced request per grid cell serves both the
    solar and the wind units in it, with a disk cache across builds."""
    from esfex.plugins.availability_generator import open_meteo_weather as om

    try:  # the CF models live in solarex / windrex
        import solarex  # noqa: F401
        import windrex  # noqa: F401
    except ImportError:
        logger.debug("solarex / windrex unavailable — no weather profiles")
        return {}

    cells: dict[tuple, list[tuple]] = {}
    for key in keys:
        cells.setdefault((key[1], key[2]), []).append(key)

    cache_dir = om.default_cache_dir()
    to_download = [c for c in cells
                   if not om._cache_path(cache_dir, *c, weather_year).exists()]
    n_cells = len(cells)
    if progress_callback:
        if to_download:
            per_min = om.CALLS_PER_MINUTE_BUDGET / om.year_request_cost(weather_year)
            minutes = len(to_download) / per_min
            msg = tr("availability_generator.progress_weather_download",
                     n=n_cells, download=len(to_download),
                     minutes=max(1, round(minutes)))
        else:
            msg = tr("availability_generator.progress_weather_cached", n=n_cells)
        progress_callback(0, msg)

    limiter = om.MinuteRateLimiter()
    stop = threading.Event()
    quota_reason: list[str] = []

    def _work(cell):
        weather = om.fetch_cell_weather(cell[0], cell[1], weather_year,
                                        limiter, cache_dir, stop)
        if weather is None:
            return cell, {}
        out = {}
        for key in cells[cell]:
            cf = om.solar_cf(weather) if key[0] == "solar" else om.wind_cf(weather)
            if np.any(cf):
                out[key] = cf
        return cell, out

    cache: dict[tuple, np.ndarray] = {}
    done = 0
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=_resolve_max_workers(n_cells)
    ) as ex:
        futures = [ex.submit(_work, c) for c in cells]
        for fut in concurrent.futures.as_completed(futures):
            try:
                _cell, cfs = fut.result()
                cache.update(cfs)
            except om.OpenMeteoQuotaExhausted as exc:
                if not stop.is_set():
                    quota_reason.append(str(exc))
                    stop.set()  # the rest would only draw more 429s
            except Exception as exc:
                logger.warning("Weather fetch failed: %s", exc)
            done += 1
            if progress_callback:
                progress_callback(int(100 * done / n_cells),
                                  tr("availability_generator.progress_weather_n",
                                     done=done, n=n_cells))

    if quota_reason:
        logger.warning(
            "Open-Meteo quota exhausted (%s). Locations downloaded so far are "
            "cached in %s; re-run the build later to fetch the rest.",
            quota_reason[0], cache_dir)
    logger.info("Weather availability: %d of %d location(s) available.",
                len({(k[1], k[2]) for k in cache}), n_cells)
    return cache


def _fetch_one_weather_cf(
    kind: str,
    gen: "GuiGeneratorInstance",
    weather_year: int,
    weather_source: str,
) -> Optional[np.ndarray]:
    """Query the weather backend for one location.

    ``None`` (backend not installed) → the caller uses the flat default. A
    transient failure (429 / 5xx / timeout) is retried with backoff; if every
    attempt fails the exception propagates so the caller skips that generator.
    Runs on a worker thread; must not touch Qt or shared mutable state.
    """
    if kind == "solar":
        try:
            from solarex import compute_solar_hourly_cf
        except ImportError:
            logger.debug("solarex unavailable — using flat default")
            return None
        query = lambda: np.asarray(compute_solar_hourly_cf(  # noqa: E731
            gen.latitude, gen.longitude, weather_year, weather_source,
        ))
    elif kind == "wind":
        try:
            from windrex import compute_wind_hourly_cf
        except ImportError:
            logger.debug("windrex unavailable — using flat default")
            return None
        query = lambda: np.asarray(compute_wind_hourly_cf(  # noqa: E731
            gen.latitude, gen.longitude, weather_year, weather_source,
            rated_power_mw=max(gen.rated_power, 1.0),
        ))
    else:
        return None

    label = f"{kind} @ ({gen.latitude:.3f}, {gen.longitude:.3f})"
    last_exc: Optional[Exception] = None
    for attempt in range(_WEATHER_RETRIES):
        try:
            res = query()
            # The backends swallow a failed fetch (HTTP 429/5xx/timeout) and
            # return an all-zero series rather than raising, so a bare
            # ``return query()`` would treat a rate-limited miss as success and
            # never retry. A real yearly CF series is never identically zero, so
            # treat all-zeros (or empty/None) as a transient failure and retry.
            if res is None or getattr(res, "size", 0) == 0 or not np.any(res):
                raise RuntimeError(
                    "weather backend returned an all-zero series "
                    "(rate-limited or no data)")
            return res
        except Exception as exc:  # transient network / rate-limit errors
            last_exc = exc
            if attempt < _WEATHER_RETRIES - 1:
                delay = _WEATHER_BACKOFF_S * (2 ** attempt) * (0.5 + random.random())
                logger.debug(
                    "Weather fetch retry %d/%d for %s in %.1fs (%s)",
                    attempt + 1, _WEATHER_RETRIES - 1, label, delay, exc)
                time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def _default_for_kind(kind: str) -> float:
    if kind == "solar":
        return 0.20
    if kind == "wind":
        return 0.32
    return 0.85


def _compute_cf(
    kind: str,
    canonical_fuel: str,
    gen: "GuiGeneratorInstance",
    *,
    use_weather_data: bool,
    weather_year: int,
    weather_source: str,
    seed: int,
) -> np.ndarray:
    if kind == "solar":
        if use_weather_data:
            try:
                from solarex import compute_solar_hourly_cf
                return np.asarray(compute_solar_hourly_cf(
                    gen.latitude, gen.longitude, weather_year, weather_source,
                ))
            except ImportError:
                logger.debug("solarex unavailable — using flat default")
        from esfex.plugins.availability_generator.synthetic_cf import (
            compute_constant_cf,
        )
        return compute_constant_cf(0.20)

    if kind == "wind":
        if use_weather_data:
            try:
                from windrex import compute_wind_hourly_cf
                return np.asarray(compute_wind_hourly_cf(
                    gen.latitude, gen.longitude, weather_year, weather_source,
                    rated_power_mw=max(gen.rated_power, 1.0),
                ))
            except ImportError:
                logger.debug("windrex unavailable — using flat default")
        from esfex.plugins.availability_generator.synthetic_cf import (
            compute_constant_cf,
        )
        return compute_constant_cf(0.32)

    # Synthetic family
    return compute_synthetic_cf(
        canonical_fuel, lat=gen.latitude, seed=seed,
    )
