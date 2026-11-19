"""Background worker for the Grid Builder "Building network" pipeline.

The whole build — node creation → ``build_grid_from_features`` → parameter
inference → simplification → isolated/dangling cleanup → visual wires → optional
availability — used to run synchronously on the GUI thread, freezing the Studio
for tens of minutes on large systems (e.g. all of Japan). This worker runs it on
a background ``QThread`` so the UI stays responsive and the build is cancellable.

The worker mutates ``model.state`` with the model's per-element signals **blocked
by the caller**; every mutating model method only touches plain ``state`` data
and a (suppressed) signal, so this is safe off the main thread. The caller emits
a single ``stateLoaded`` redraw on the main thread when the worker finishes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from esfex.visualization.i18n import tr

logger = logging.getLogger(__name__)


@dataclass
class BuildParams:
    """Inputs captured on the main thread before the worker starts (so the
    worker never reads a Qt widget)."""

    node_positions: list
    n_clusters: int
    criterion_used: str
    features: list
    config: dict
    station_radius_km: float
    simplify_level: int
    min_component: int
    gen_availability: bool
    use_weather: bool
    cfg_path: str | None = None
    reconnect_max_km: float = 0.0


class GridBuildWorker(QThread):
    """Run the Grid Builder build pipeline off the GUI thread."""

    progress = Signal(str)      # human-readable phase status
    finished = Signal(object)   # result dict (see _run)
    error = Signal(str)

    def __init__(self, model, params: BuildParams, parent=None):
        super().__init__(parent)
        self._model = model
        self._p = params
        self._cancelled = False
        self.phase_timings: list[tuple[str, float]] = []

    def cancel(self):
        self._cancelled = True

    def _timed(self, label, fn):
        t0 = time.perf_counter()
        try:
            return fn()
        finally:
            dt = time.perf_counter() - t0
            self.phase_timings.append((label, dt))
            logger.info("Grid build phase '%s': %.1fs", label, dt)

    def run(self):
        try:
            res = self._run()
            res["phase_timings"] = self.phase_timings
            res["cancelled"] = self._cancelled
            self.finished.emit(res)
        except Exception as exc:  # surfaced to the user; partial state kept
            logger.exception("Grid build worker error")
            self.error.emit(str(exc))

    # ------------------------------------------------------------------
    def _run(self) -> dict:
        from esfex.visualization.workflows.grid_mapping_builder import (
            build_grid_from_features,
        )
        from esfex.visualization.workflows.grid_mapping_inference import (
            infer_electrical_params,
        )
        from esfex.visualization.data.validation import (
            SimplificationConfig,
            apply_simplification_level,
            drop_dangling_refs,
            drop_isolated_components,
            rebuild_visual_wire_lines,
        )

        model, p = self._model, self._p
        out: dict = {"build_summary": "", "inference_summary": "",
                     "simplify_summary": "", "island_summary": ""}
        island_lines: list[str] = []

        with model.suspend_checkpoints():
            # ── Node creation (auto-nodes only; None → use existing nodes) ──
            if p.node_positions is not None:
                self.progress.emit(tr("grid_builder.progress_creating_nodes",
                                      count=len(p.node_positions)))
                # The Grid Builder owns the full node set: drop placeholder
                # nodes created with the system (e.g. default "Node 0").
                while model.state.nodes:
                    model.remove_node(len(model.state.nodes) - 1)
                for lat, lng, name in p.node_positions:
                    idx = model.add_node(name)
                    model.update_node(idx, centroid_lat=lat, centroid_lng=lng)
                if self._cancelled:
                    return out

            # ── Build network ──
            self.progress.emit(tr("grid_builder.building_network"))
            phase = tr("grid_builder.report_phase_build")
            result = self._timed(phase, lambda: build_grid_from_features(
                model=model,
                features=p.features,
                bus_strategy=p.config.get("bus_strategy", "per_voltage"),
                snap_threshold_km=p.config.get("snap_threshold_km", 5.0),
                target_node=None,
                station_radius_km=p.station_radius_km,
                min_capacity_mw=p.config.get("min_capacity_mw", 0.0),
                min_voltage_kv=p.config.get("min_voltage_kv", 0.0),
                reconnect_max_km=p.reconnect_max_km,
            ))
            out["build_summary"] = result.summary()
            if self._cancelled:
                return out

            # ── Parameter inference ──
            self.progress.emit(tr("grid_builder.progress_inferring"))
            inference = self._timed(tr("grid_builder.report_phase_inference"),
                                    lambda: infer_electrical_params(model.state))
            out["inference_summary"] = "\n".join([inference.summary(), *inference.notes])
            logger.info("Grid build parameter inference: %s", out["inference_summary"])
            if self._cancelled:
                return out

            # ── Simplification ──
            self.progress.emit(tr("grid_builder.progress_simplifying",
                                  level=p.simplify_level))
            simp_log, _issues = self._timed(
                tr("grid_builder.report_phase_simplification",
                   level=p.simplify_level),
                lambda: apply_simplification_level(
                    model, p.simplify_level, SimplificationConfig()),
            )
            out["simplify_summary"] = "\n".join(simp_log)
            if self._cancelled:
                return out

            # ── Drop isolated debris components ──
            self.progress.emit(tr("grid_builder.progress_dropping_isolated"))
            # Which nodes have an electrical presence BEFORE the drop. Only
            # nodes the drop *empties* may be pruned below; a node that never
            # had a bus is user scaffolding (e.g. a build with no features)
            # and must survive.
            nodes_with_bus_before = {
                b.parent_node for b in model.state.buses.values()
            }
            counts = self._timed(tr("grid_builder.report_phase_drop_isolated"),
                                  lambda: drop_isolated_components(
                                      model.state, min_buses=p.min_component,
                                      keep_largest=True))
            n_total = counts.get("_components_total", 0)
            n_dropped = counts.get("_components_dropped", 0)
            island_lines.append(tr(
                "grid_builder.report_components", total=n_total,
                largest=counts.get("_largest_size", 0),
                sizes=counts.get("_top_sizes", [])))
            island_lines.append(tr("grid_builder.report_component_threshold",
                                   min=p.min_component))
            if n_dropped:
                island_lines.append("→ " + tr(
                    "grid_builder.report_components_dropped", count=n_dropped,
                    buses=counts["buses"], lines=counts["lines"],
                    transformers=counts["transformers"],
                    converters=counts["converters"],
                    generators=counts["generators"],
                    batteries=counts["batteries"]))

            # ── Restore the "every node has a bus" invariant ──
            # A node is a geographic aggregation; a bus is its electrical
            # presence. Buses are therefore always >= nodes, and the whole
            # bridge relies on it: per-node arrays (rated_power, availability,
            # reserves, demand) are mapped onto the bus network, and results
            # are aggregated back bus→node. drop_isolated_components removes
            # buses but never nodes, so a node whose only bus sat in a dropped
            # fragment is left with NO electrical presence — it cannot connect
            # to the grid, its demand can never be served, and it breaks the
            # invariant (Japan: 296 nodes vs 289 buses, which silently shifted
            # 70 % of generation capacity onto the wrong bus and shed 34 % of
            # the load). Prune those nodes so the two stay consistent.
            #
            # Only nodes the drop EMPTIED are pruned. A node that never had a
            # bus is not debris — it is the user's own scaffolding (nodes are
            # created before any equipment is placed on them), and deleting it
            # would silently undo their work.
            nodes_with_bus_after = {
                b.parent_node for b in model.state.buses.values()
            }
            orphans = [
                n.index for n in model.state.nodes
                if n.index in nodes_with_bus_before
                and n.index not in nodes_with_bus_after
            ]
            if orphans:
                self.progress.emit(tr("grid_builder.progress_pruning_nodes",
                                      count=len(orphans)))
                for idx in sorted(orphans, reverse=True):
                    model.remove_node(idx)
                island_lines.append("→ " + tr(
                    "grid_builder.report_nodes_pruned", count=len(orphans),
                    remaining=len(model.state.nodes)))

            # ── Hard sweep: drop dangling references ──
            self.progress.emit(tr("grid_builder.progress_removing_dangling"))
            ref_counts = self._timed(tr("grid_builder.report_phase_dangling"),
                                     lambda: drop_dangling_refs(model.state))
            if sum(ref_counts.values()) > 0:
                island_lines.append(tr(
                    "grid_builder.report_dangling_removed",
                    lines=ref_counts["lines"],
                    transformers=ref_counts["transformers"],
                    converters=ref_counts["converters"],
                    generators=ref_counts["generators"],
                    batteries=ref_counts["batteries"]))

            # ── Rebuild visual wire-lines ──
            self.progress.emit(tr("grid_builder.progress_rebuilding_wires"))
            n_wires = self._timed(tr("grid_builder.report_phase_wires"),
                                  lambda: rebuild_visual_wire_lines(model.state))
            if n_wires:
                island_lines.append(tr("grid_builder.report_wires_rebuilt",
                                       count=n_wires))

            # ── Optional availability profile generation ──
            if p.gen_availability and model.state.generators and not self._cancelled:
                self.progress.emit(
                    tr("grid_builder.progress_generating_availability"))
                from esfex.plugins.availability_generator.grid_builder_hook import (
                    generate_for_grid_build,
                )
                out_dir = (Path(p.cfg_path).parent / "availability"
                           if p.cfg_path else Path.cwd() / "availability")
                # Weather downloads are paced to Open-Meteo's rate limit and
                # can take minutes on large systems: surface their progress.
                def _avail_progress(_pct, msg):
                    if msg.startswith("Weather"):
                        self.progress.emit(msg)
                # Weather profiles are saved on the model clock: fix it now
                # (by node count unless demand is known) and record it, so the
                # demand written later uses the same clock.
                clock = "auto"
                g = getattr(model, "global_settings", None)
                if p.use_weather and g is not None:
                    from esfex.utils.model_clock import resolve_for_nodes
                    clock = resolve_for_nodes(g.model_clock, model.state.nodes)
                    g.model_clock = clock.zone
                    island_lines.append(tr("grid_builder.report_model_clock",
                                           clock=clock.label))
                written = self._timed(
                    tr("grid_builder.report_phase_availability"),
                    lambda: generate_for_grid_build(
                        model.state, out_dir,
                        use_weather_data=p.use_weather,
                        progress_callback=_avail_progress,
                        model_clock=clock))
                if written:
                    island_lines.append(tr(
                        "grid_builder.report_availability_written",
                        count=len(written), path=out_dir))
                # Wind/solar units with no real weather data are skipped (no
                # fabricated flat profile); every other generator is written.
                n_skipped = len(model.state.generators) - len(written)
                if n_skipped > 0:
                    island_lines.append(tr(
                        "grid_builder.report_availability_skipped",
                        count=n_skipped))

        out["island_summary"] = "\n".join(island_lines)
        out["simplify_level"] = p.simplify_level
        return out
