"""Texts built in Python follow a language change.

Covers the Grid Builder clustering progress and criteria, the availability
plugin's progress, the hazard names, the SLD load labels the page relabels,
the MGA bundle cache, and the contingency combo of the SLD.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from esfex.visualization.i18n import get_language, init_i18n, tr

_TDIR = Path(__file__).resolve().parent.parent / "src/esfex/visualization/translations"


def _keys(lang: str) -> dict:
    out: dict[str, str] = {}

    def walk(node, prefix=""):
        for k, v in node.items():
            key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                walk(v, key)
            else:
                out[key] = v
    walk(json.loads((_TDIR / f"{lang}.json").read_text(encoding="utf-8")))
    return out


@pytest.mark.parametrize("lang", ["en", "es", "fr", "pt", "ja"])
def test_every_hazard_and_criterion_has_a_name(lang):
    from esfex.models.hazard_assessment import FragilityLibrary
    keys = _keys(lang)
    hazards = {c.hazard_type for c in FragilityLibrary().get_all_curves()}
    hazards.add("screening")
    missing = [h for h in sorted(hazards) if f"risk_charts.haz_{h}" not in keys]
    for crit in ("infrastructure", "demand", "regional"):
        for k in (f"grid_builder.criterion_{crit}_label",
                  f"grid_builder.criterion_{crit}_desc",
                  f"grid_builder.cluster_criterion_{crit}"):
            if k not in keys:
                missing.append(k)
    for kind in ("solar", "wind", "synthetic"):
        if f"availability_generator.kind_{kind}" not in keys:
            missing.append(kind)
    assert not missing, missing


def test_clustering_progress_is_translated():
    pytest.importorskip("PySide6.QtCore")
    from esfex.visualization.workflows.grid_mapping_clustering import NodeClusteringWorker
    init_i18n("es")
    w = NodeClusteringWorker([], ["nonsense"])
    errors: list[str] = []
    w.error.connect(errors.append)
    w.run()
    assert errors and errors[0].startswith("No hay criterios válidos")


def test_availability_progress_is_translated(tmp_path):
    from esfex.plugins.availability_generator.grid_builder_hook import (
        generate_for_grid_build,
    )
    from esfex.visualization.data.gui_model import GuiSystemState
    init_i18n("es")
    msgs: list[str] = []
    generate_for_grid_build(GuiSystemState(), tmp_path,
                            progress_callback=lambda p, m: msgs.append(m))
    assert msgs == ["No hay generadores que procesar."]


def test_sld_load_label_carries_its_web_key():
    from esfex.visualization.data.gui_model import (
        GuiBus, GuiNode, GuiNodeDemand, GuiSystemState,
    )
    from esfex.visualization.sld.graph_builder import build_elk_graph
    init_i18n("es")
    state = GuiSystemState()
    state.nodes = [GuiNode(index=0, name="N", demand=GuiNodeDemand(peak_mw=5.0))]
    state.buses = {"b": GuiBus(bus_id="b", parent_node=0, voltage_kv=13.8)}
    (eq,) = build_elk_graph(state)["busEquipment"]["bus_b"]
    assert eq["label"] == "Carga N"
    assert eq["labelKey"].startswith("web.")
    # The page renders _t(labelKey, labelParams): the same text.
    assert tr(eq["labelKey"], **eq["labelParams"]) == eq["label"]


def test_mga_bundle_is_cached_per_language(monkeypatch, tmp_path):
    from esfex.visualization.panels import results_charts as rc
    from esfex.visualization.panels.results_cache import ResultsCache, activate

    built: list[str] = []

    def fake_build(_h5f, _prefix):
        built.append(get_language())
        return {"lang": get_language()}

    class _H5:
        def __enter__(self):
            return None

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(rc, "_build_mga_bundle", fake_build)
    monkeypatch.setattr(rc, "_open_h5", lambda _p: _H5())
    cache = ResultsCache(tmp_path / "r.h5")
    with activate(cache):
        init_i18n("en")
        assert rc._load_mga_bundle(tmp_path / "r.h5", "")["lang"] == "en"
        assert rc._load_mga_bundle(tmp_path / "r.h5", "")["lang"] == "en"
        init_i18n("es")
        assert rc._load_mga_bundle(tmp_path / "r.h5", "")["lang"] == "es"
    assert built == ["en", "es"]


def test_contingency_combo_follows_the_language():
    pytest.importorskip("PySide6.QtWidgets")
    from types import SimpleNamespace

    from PySide6.QtWidgets import QApplication, QComboBox

    from esfex.visualization.main_window import MainWindow
    QApplication.instance() or QApplication([])
    init_i18n("en")
    combo = QComboBox()
    ctg = {"type": "generator", "element_id": "G1", "description": "core text"}
    combo.addItem(tr("messages.sld_no_contingency"), None)
    combo.addItem("Loss of generator G1", ctg)
    reruns: list[int] = []
    host = SimpleNamespace(_sld_contingency_combo=combo,
                           _sld_contingency_analyzer=None,
                           _contingency_ratings=lambda a: {},
                           _on_sld_contingency_changed=reruns.append)
    init_i18n("es")
    MainWindow._retranslate_sld_contingency(host)
    assert combo.itemText(0) == tr("messages.sld_no_contingency")
    assert combo.itemText(0) != "None"
    assert reruns == []          # nothing selected: nothing to re-run
    combo.setCurrentIndex(1)
    MainWindow._retranslate_sld_contingency(host)
    assert reruns == [1]
