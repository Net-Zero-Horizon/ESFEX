"""Tests for the availability_generator plugin.

Covers generator orchestration, CLI, plugin structure,
and translation keys.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PLUGIN_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "esfex"
    / "plugins"
    / "availability_generator"
)

_TRANSLATIONS_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "esfex"
    / "visualization"
    / "translations"
)


# ===================================================================
# TestGenerator
# ===================================================================


class TestGenerator:
    """Tests for the generator orchestrator."""

    def _make_config(self, num_nodes=1, fuel="Solar", gen_type="Renewable"):
        """Create a minimal mock config."""
        coords = [
            SimpleNamespace(latitude=10.0 + i, longitude=-70.0 + i)
            for i in range(num_nodes)
        ]
        gen = SimpleNamespace(
            fuel=fuel,
            type=gen_type,
            rated_power=[100.0] * num_nodes,
        )
        nodes = SimpleNamespace(node_coordinates=coords)
        system = SimpleNamespace(
            generators={"gen_0": gen},
            nodes=nodes,
        )
        return SimpleNamespace(systems={"sys_a": system})

    def test_scan_renewable_only(self):
        """_collect_tasks should only pick Solar/Wind generators."""
        from esfex.plugins.availability_generator.generator import _collect_tasks

        cfg = self._make_config(fuel="Solar")
        tasks = _collect_tasks(cfg, None)
        assert len(tasks) == 1
        assert tasks[0][1] == "gen_0"
        assert tasks[0][4] == "Solar"  # profile_type

    def test_skip_non_renewable(self):
        """Non-renewable generators should be skipped."""
        from esfex.plugins.availability_generator.generator import _collect_tasks

        cfg = self._make_config(fuel="Diesel", gen_type="Non-renewable")
        tasks = _collect_tasks(cfg, None)
        assert len(tasks) == 0

    def test_profile_type_map_override(self):
        """Explicit profile_type_map should override fuel-name heuristics."""
        from esfex.plugins.availability_generator.generator import _collect_tasks

        cfg = self._make_config(fuel="Renewables", gen_type="Renewable")
        # Without map: "Renewables" doesn't match solar/wind hints
        tasks_no_map = _collect_tasks(cfg, None)
        assert len(tasks_no_map) == 0

        # With explicit map: should be included as Wind
        tasks_with_map = _collect_tasks(
            cfg, None, profile_type_map={"sys_a/gen_0": "Wind"},
        )
        assert len(tasks_with_map) == 1
        assert tasks_with_map[0][4] == "Wind"

    @patch(
        "esfex.plugins.availability_generator.generator.compute_solar_hourly_cf"
    )
    def test_single_node_csv_shape(self, mock_solar):
        """Single node, single year should produce (8760, 1) CSV."""
        from esfex.plugins.availability_generator.generator import (
            generate_availability_profiles,
        )

        mock_solar.return_value = np.random.rand(8760)

        cfg = self._make_config(num_nodes=1, fuel="Solar")
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_availability_profiles(
                config=cfg,
                years=[2020],
                output_dir=Path(tmpdir),
                data_source="open_meteo",
            )
            assert len(result) == 1
            csv_path = list(result.values())[0]
            data = np.loadtxt(csv_path, delimiter=",")
            assert data.shape == (8760,)  # 1 column collapses to 1D

    @patch(
        "esfex.plugins.availability_generator.generator.compute_solar_hourly_cf"
    )
    def test_multi_node_csv_shape(self, mock_solar):
        """3 nodes should produce (8760, 3) CSV."""
        from esfex.plugins.availability_generator.generator import (
            generate_availability_profiles,
        )

        mock_solar.return_value = np.random.rand(8760)

        cfg = self._make_config(num_nodes=3, fuel="Solar")
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_availability_profiles(
                config=cfg,
                years=[2020],
                output_dir=Path(tmpdir),
                data_source="open_meteo",
            )
            csv_path = list(result.values())[0]
            data = np.loadtxt(csv_path, delimiter=",")
            assert data.shape == (8760, 3)

    @patch(
        "esfex.plugins.availability_generator.generator.compute_solar_hourly_cf"
    )
    def test_multi_year_csv_shape(self, mock_solar):
        """3 years should produce (26280, 1) CSV."""
        from esfex.plugins.availability_generator.generator import (
            generate_availability_profiles,
        )

        mock_solar.return_value = np.random.rand(8760)

        cfg = self._make_config(num_nodes=1, fuel="Solar")
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_availability_profiles(
                config=cfg,
                years=[2020, 2021, 2022],
                output_dir=Path(tmpdir),
                data_source="open_meteo",
            )
            csv_path = list(result.values())[0]
            data = np.loadtxt(csv_path, delimiter=",")
            assert data.shape == (8760 * 3,)

    @patch(
        "esfex.plugins.availability_generator.generator.compute_wind_hourly_cf"
    )
    def test_wind_generator_uses_wind_cf(self, mock_wind):
        """Wind fuel generator should call compute_wind_hourly_cf."""
        from esfex.plugins.availability_generator.generator import (
            generate_availability_profiles,
        )

        mock_wind.return_value = np.random.rand(8760)

        cfg = self._make_config(num_nodes=1, fuel="Wind")
        with tempfile.TemporaryDirectory() as tmpdir:
            generate_availability_profiles(
                config=cfg,
                years=[2020],
                output_dir=Path(tmpdir),
                data_source="open_meteo",
            )
            mock_wind.assert_called_once()


# ===================================================================
# TestCLI
# ===================================================================


class TestCLI:
    """Tests for CLI sub-commands."""

    def test_cli_app_exists(self):
        """CLI app should be importable."""
        from esfex.plugins.availability_generator.cli_commands import app

        assert app is not None
        assert app.info.name == "availability_generator"

    def test_cli_has_generate_command(self):
        """The generate command should be registered."""
        from esfex.plugins.availability_generator.cli_commands import app

        command_names = [
            cmd.name or cmd.callback.__name__
            for cmd in app.registered_commands
        ]
        assert "generate" in command_names

    def test_default_years_from_config(self):
        """_default_years takes the year of the top-level date_start."""
        from esfex.plugins.availability_generator.cli_commands import (
            _default_years,
        )

        cfg = SimpleNamespace(date_start="01/01/2021 00:00")
        assert _default_years(cfg) == [2021]

    def test_default_years_from_real_config(self):
        """A real ESFEXConfig has date_start at the top level (not under
        temporal): its year is used, not the 2020 fallback."""
        from esfex.config.schema import ESFEXConfig
        from esfex.plugins.availability_generator.cli_commands import (
            _default_years,
        )

        cfg = ESFEXConfig.model_construct(date_start="01/07/2031 00:00")
        assert _default_years(cfg) == [2031]

    def test_default_years_unreadable_date(self):
        from esfex.plugins.availability_generator.cli_commands import (
            _default_years,
        )

        assert _default_years(SimpleNamespace(date_start="2021")) == [2020]

    def test_default_years_fallback(self):
        """_default_years should return [2020] on missing temporal."""
        from esfex.plugins.availability_generator.cli_commands import (
            _default_years,
        )

        cfg = SimpleNamespace()
        years = _default_years(cfg)
        assert years == [2020]


# ===================================================================
# TestPluginStructure
# ===================================================================


class TestPluginStructure:
    """Tests for plugin.json and factory."""

    def test_plugin_json_valid(self):
        """plugin.json must have name, version, category."""
        pj = _PLUGIN_DIR / "plugin.json"
        assert pj.exists()
        data = json.loads(pj.read_text())
        assert data["name"] == "availability_generator"
        assert "version" in data
        assert data["category"] == "data"

    def test_create_plugin_returns_instance(self):
        """Factory function should return a ESFEXPlugin."""
        from esfex.plugins.availability_generator import create_plugin

        ctx = SimpleNamespace(
            plugin_dir=_PLUGIN_DIR,
            metadata={"name": "availability_generator", "version": "1.0.0"},
        )
        plugin = create_plugin(ctx)
        assert plugin is not None

    def test_get_cli_commands_returns_list(self):
        """get_cli_commands should return a list with the Typer app."""
        from esfex.plugins.availability_generator import create_plugin

        ctx = SimpleNamespace(
            plugin_dir=_PLUGIN_DIR,
            metadata={"name": "availability_generator", "version": "1.0.0"},
        )
        plugin = create_plugin(ctx)
        cmds = plugin.get_cli_commands()
        assert isinstance(cmds, list)
        assert len(cmds) >= 1


# ===================================================================
# TestTranslations
# ===================================================================


class TestTranslations:
    """Verify that translation files contain availability_generator keys."""

    _REQUIRED_KEYS = [
        "menu_action",
        "dialog_title",
        "system",
        "generators",
        "gen_key",
        "fuel",
        "profile_type",
        "node",
        "nodes",
        "data_source",
        "years",
        "solar_params",
        "efficiency",
        "tilt",
        "azimuth",
        "tracking",
        "wind_params",
        "turbine",
        "hub_height",
        "generate",
        "close",
        "done_title",
        "done_message",
    ]

    def test_en_has_availability_keys(self):
        """English translations must have all availability_generator keys."""
        en = json.loads((_TRANSLATIONS_DIR / "en.json").read_text())
        section = en.get("availability_generator", {})
        for key in self._REQUIRED_KEYS:
            assert key in section, f"Missing en.json key: availability_generator.{key}"

    def test_es_has_availability_keys(self):
        """Spanish translations must have all availability_generator keys."""
        es = json.loads((_TRANSLATIONS_DIR / "es.json").read_text())
        section = es.get("availability_generator", {})
        for key in self._REQUIRED_KEYS:
            assert key in section, f"Missing es.json key: availability_generator.{key}"


# ===================================================================
# TestGridBuilderHookDedup — co-located weather queries collapse + parallelize
# ===================================================================


class TestGridBuilderHookDedup:
    """The Grid Builder availability hook must query the weather backend once
    per distinct location, not once per generator (the Japan-scale bottleneck).

    With the default ``open_meteo`` source the per-cell download goes through
    :mod:`open_meteo_weather` (quota-paced, disk-cached), so these tests stub
    ``fetch_cell_weather`` / ``requests.get`` there and point the cache at a
    temporary directory — they must never reach the network."""

    @pytest.fixture(autouse=True)
    def _isolated_weather_cache(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ESFEX_WEATHER_CACHE", str(tmp_path / "weather"))

    @staticmethod
    def _weather(wind_100m=9.0):
        n = 8760
        hours = np.arange(n)
        ghi = np.clip(900 * np.sin(np.pi * ((hours % 24) - 6) / 12), 0, None)
        return {
            "shortwave_radiation": ghi.astype(np.float32),
            "temperature_2m": np.full(n, 25.0, dtype=np.float32),
            "wind_speed_10m": np.full(n, 6.0, dtype=np.float32),
            "wind_speed_100m": np.full(n, wind_100m, dtype=np.float32),
        }

    @staticmethod
    def _gen(lat, lng, fuel="Wind", rated=10.0):
        return SimpleNamespace(
            latitude=lat, longitude=lng, fuel=fuel,
            rated_power=rated, availability_file=None,
        )

    def test_colocated_generators_share_one_weather_fetch(self):
        from esfex.plugins.availability_generator import grid_builder_hook as gbh

        # Six wind units: three pairs of co-located turbines (within one cell).
        gens = {
            "w0": self._gen(35.00, 139.00),
            "w1": self._gen(35.001, 139.001),  # same ~11 km cell as w0
            "w2": self._gen(40.00, 141.00),
            "w3": self._gen(40.002, 141.002),  # same cell as w2
            "w4": self._gen(33.00, 130.00),
            "w5": self._gen(33.0, 130.0),       # identical coords to w4
        }
        state = SimpleNamespace(generators=gens)

        from esfex.plugins.availability_generator import open_meteo_weather as om
        pytest.importorskip("windrex")
        calls = {"n": 0}

        def fake_fetch(lat, lng, year, limiter, cache_dir, stop):
            calls["n"] += 1
            return self._weather()

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(om, "fetch_cell_weather", fake_fetch):
            written = gbh.generate_for_grid_build(
                state, Path(tmp), use_weather_data=True)

            # 6 generators, 3 distinct cells → exactly 3 backend queries.
            assert calls["n"] == 3
            # Every generator still gets its own CSV + availability_file.
            assert len(written) == 6
            for gid, gen in gens.items():
                assert gen.availability_file is not None
                assert Path(written[gid]).exists()

    def test_failed_fetch_skips_generator_no_fallback(self):
        from esfex.plugins.availability_generator import grid_builder_hook as gbh

        gens = {"w0": self._gen(35.0, 139.0)}
        state = SimpleNamespace(generators=gens)

        from esfex.plugins.availability_generator import open_meteo_weather as om
        pytest.importorskip("windrex")

        def boom(*a, **k):
            raise RuntimeError("network down")

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(om, "fetch_cell_weather", boom):
            written = gbh.generate_for_grid_build(
                state, Path(tmp), use_weather_data=True)

            # No real weather data → no fabricated flat profile; the unit is
            # simply left without an availability file (and the build goes on).
            assert "w0" not in written
            assert gens["w0"].availability_file is None
            assert not (Path(tmp) / "w0_availability.csv").exists()

    def test_transient_failure_is_retried_then_succeeds(self):
        from esfex.plugins.availability_generator import grid_builder_hook as gbh

        gens = {"w0": self._gen(35.0, 139.0)}
        state = SimpleNamespace(generators=gens)

        import requests
        from esfex.plugins.availability_generator import open_meteo_weather as om
        pytest.importorskip("windrex")
        weather = self._weather(wind_100m=9.0)
        calls = {"n": 0}

        class _Resp:
            def __init__(self, status):
                self.status_code = status
                self.headers = {}

            def json(self):
                if self.status_code != 200:
                    return {"error": True, "reason": "Internal error"}
                return {"hourly": {k: v.tolist() for k, v in weather.items()}}

        def flaky(*a, **k):
            calls["n"] += 1
            return _Resp(503 if calls["n"] < 3 else 200)  # two transient 5xx

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(requests, "get", flaky), \
                patch.object(om.time, "sleep", lambda *_: None):  # no real wait
            written = gbh.generate_for_grid_build(
                state, Path(tmp), use_weather_data=True)

            # Retried up to the 3rd attempt, then the real profile is written.
            assert calls["n"] == 3
            assert "w0" in written
            data = np.loadtxt(written["w0"], delimiter=",")
            assert np.allclose(data, om.wind_cf(weather), atol=1e-6)

    def test_env_override_caps_workers(self, monkeypatch):
        from esfex.plugins.availability_generator import grid_builder_hook as gbh

        monkeypatch.setenv("ESFEX_AVAILABILITY_WORKERS", "3")
        assert gbh._resolve_max_workers(100) == 3   # env cap wins
        assert gbh._resolve_max_workers(2) == 2      # never more than the work
        monkeypatch.setenv("ESFEX_AVAILABILITY_WORKERS", "garbage")
        # Non-integer override ignored → falls back to the rate-limit-safe default.
        assert gbh._resolve_max_workers(1000) == gbh._default_max_workers()

    def test_default_workers_is_rate_limit_safe(self, monkeypatch):
        from esfex.plugins.availability_generator import grid_builder_hook as gbh

        # The weather fetch pool is network/rate-limited, not CPU-bound: the
        # default must NOT scale with the core count (a cpu_count-2 default 429s
        # ~90% of Open-Meteo requests on a many-core box). It is a low constant.
        monkeypatch.delenv("ESFEX_AVAILABILITY_WORKERS", raising=False)
        monkeypatch.setattr(gbh.os, "cpu_count", lambda: 64)
        assert gbh._default_max_workers() == 4
        assert gbh._resolve_max_workers(1000) == 4
        monkeypatch.setattr(gbh.os, "cpu_count", lambda: 2)
        assert gbh._default_max_workers() == 4   # still 4, independent of cores
        # Never more workers than there is work.
        assert gbh._resolve_max_workers(2) == 2

    def test_synthetic_fuel_still_written_under_weather_mode(self):
        from esfex.plugins.availability_generator import grid_builder_hook as gbh

        # A thermal unit has no weather equivalent — it must still get its
        # synthetic profile even when weather mode is on for wind/solar.
        gens = {"t0": self._gen(35.0, 139.0, fuel="Gas", rated=100.0)}
        state = SimpleNamespace(generators=gens)

        with tempfile.TemporaryDirectory() as tmp:
            written = gbh.generate_for_grid_build(
                state, Path(tmp), use_weather_data=True)

            assert "t0" in written
            assert gens["t0"].availability_file is not None
            data = np.loadtxt(written["t0"], delimiter=",")
            assert data.shape == (8760,)
