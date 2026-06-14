"""QThread adapters for standalone analysis packages.

Bridges the callback-based standalone libraries (windrex, solarex) to
PySide6 QThread + Signal patterns expected by the GUI wizard steps.
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QThread, Signal


class QtWindAnalyzer(QThread):
    """QThread wrapper around windrex.WindAnalyzer."""

    progress = Signal(int, str)
    finished = Signal(object)
    error = Signal(str)

    def __init__(
        self,
        bounds,
        wind_config,
        mcda_config,
        transmission_lines=None,
        parent=None,
    ):
        super().__init__(parent)
        self._bounds = bounds
        self._wind_config = wind_config
        self._mcda_config = mcda_config
        self._transmission_lines = transmission_lines
        self._analyzer = None

    def run(self):
        try:
            from windrex import TurbineSpec, WindAnalyzer, WindConfig

            fat = self._wind_config
            if hasattr(fat, "bounds") and hasattr(fat, "mcda"):
                # Already a windrex.WindConfig — just fill in bounds + MCDA.
                config = replace(fat, bounds=self._bounds, mcda=self._mcda_config)
            else:
                # esfex's GUI WindConfig (turbine as a key string, hub_height, …)
                # → build the windrex.WindConfig the analyzer expects (turbine as
                # a TurbineSpec, hub_height_m, bounds, mcda).
                turbine = TurbineSpec(
                    key=getattr(fat, "turbine", ""),
                    rated_power_mw=getattr(fat, "turbine_capacity_mw", 3.0),
                    hub_height_m=getattr(fat, "hub_height", 80),
                    wind_speeds=list(getattr(fat, "wind_speeds", []) or []),
                    power_curve=list(getattr(fat, "power_curve", []) or []),
                )
                config = WindConfig(
                    bounds=self._bounds,
                    turbine=turbine,
                    hub_height_m=getattr(fat, "hub_height", 80),
                    grid_resolution=getattr(fat, "grid_resolution", 0.25),
                    data_source=getattr(fat, "data_source", "open_meteo"),
                    year=getattr(fat, "year", 2023),
                    mcda=self._mcda_config,
                )
            self._analyzer = WindAnalyzer(
                config, transmission_lines=self._transmission_lines,
            )
            result = self._analyzer.run(
                on_progress=lambda p, m: self.progress.emit(p, m),
            )
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))

    def cancel(self):
        if self._analyzer is not None:
            self._analyzer.cancel()


class QtSolarPVAnalyzer(QThread):
    """QThread wrapper around solarex.SolarPVAnalyzer."""

    progress = Signal(int, str)
    finished = Signal(object)
    error = Signal(str)

    def __init__(
        self,
        bounds,
        solar_config,
        mcda_config,
        transmission_lines=None,
        parent=None,
    ):
        super().__init__(parent)
        self._bounds = bounds
        self._solar_config = solar_config
        self._mcda_config = mcda_config
        self._transmission_lines = transmission_lines
        self._analyzer = None

    def run(self):
        try:
            from solarex import SolarPVAnalyzer

            self._analyzer = SolarPVAnalyzer(
                self._bounds,
                self._solar_config,
                self._mcda_config,
                self._transmission_lines,
            )
            result = self._analyzer.run(
                on_progress=lambda p, m: self.progress.emit(p, m),
            )
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))

    def cancel(self):
        if self._analyzer is not None:
            self._analyzer.cancel()
