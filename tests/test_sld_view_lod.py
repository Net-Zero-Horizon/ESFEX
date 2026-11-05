"""Level of detail, viewport culling and overview names of the SLD renderer.

The renderer is JavaScript (``resources/sld_controller.js``), so the test
loads ``sld.html`` in an offscreen QtWebEngine view, renders a synthetic
geographic system and inspects the page from JS. It runs in a subprocess so
the web engine's QApplication never meets the rest of the suite.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("PySide6.QtWebEngineWidgets")

_SCRIPT = textwrap.dedent(r'''
    import json, sys
    from pathlib import Path
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtWidgets import QApplication
    from PySide6.QtWebEngineWidgets import QWebEngineView

    from esfex.visualization.data.gui_model import (
        GuiBus, GuiGeneratorInstance, GuiNode, GuiSystemState, GuiTransformer,
        GuiTransmissionLine,
    )
    from esfex.visualization.sld.graph_builder import build_elk_graph
    import esfex.visualization as vis_pkg

    # 6 x 5 substations (220/110 kV + a generator) in a grid of ~40 km,
    # each joined to its east and south neighbours.
    st = GuiSystemState()
    st.nodes = [GuiNode(index=0, name="N")]
    st.buses, st.transformers, st.transmission_lines, st.generators = {}, [], [], {}
    for i in range(6):
        for j in range(5):
            k = f"{i}_{j}"
            lat, lon = 22.0 + 0.4 * j, -80.0 + 0.4 * i
            st.buses[f"h{k}"] = GuiBus(bus_id=f"h{k}", parent_node=0, voltage_kv=220,
                                       name=f"Sub {k}", latitude=lat, longitude=lon)
            st.buses[f"l{k}"] = GuiBus(bus_id=f"l{k}", parent_node=0, voltage_kv=110,
                                       name=f"Sub {k} LV", latitude=lat + 1e-4, longitude=lon)
            st.transformers.append(GuiTransformer(name=f"T{k}", from_bus=f"h{k}",
                                                  to_bus=f"l{k}", rated_power_mva=100.0))
            st.generators[f"g{k}"] = GuiGeneratorInstance(
                instance_id=f"g{k}", unit_key="u", name=f"G{k}", gen_type="Non-renewable",
                fuel="Gas", bus=f"l{k}", rated_power=50.0)
            for di, dj in ((1, 0), (0, 1)):
                if i + di < 6 and j + dj < 5:
                    st.transmission_lines.append(GuiTransmissionLine(
                        line_id=f"L{k}_{di}{dj}", from_bus=f"h{k}",
                        to_bus=f"h{i + di}_{j + dj}", capacity_mw=200.0, voltage_kv=220.0))
    data = json.dumps(build_elk_graph(st))

    app = QApplication([])
    view = QWebEngineView()
    view.resize(1200, 800)
    view.show()
    page = view.page()
    out = {}
    steps = []

    def js(code, cb=None):
        page.runJavaScript(code, cb or (lambda _r: None))

    def zoom(k, fx=0.5, fy=0.5):
        js(f"""(function(){{var b=diagramBounds;
            zoomBehavior.scaleExtent([0.0001, 20]);
            var cx=b.x+b.w*{fx}, cy=b.y+b.h*{fy};
            svg.call(zoomBehavior.transform,
                     d3.zoomIdentity.translate(600-cx*{k}, 400-cy*{k}).scale({k}));}})()""")

    PROBE = """JSON.stringify({
        cls: document.querySelector('#sld-container svg').getAttribute('class') || '',
        names: document.querySelectorAll('.sld-names text').length,
        culled: Array.from(document.querySelectorAll('.sld-root *'))
                     .filter(function(e){ return e.style.display === 'none'; }).length,
        texts: document.querySelectorAll('.sld-root text').length})"""

    def probe(tag):
        def run():
            js(PROBE, lambda r: (out.__setitem__(tag, json.loads(r)), nxt()))
        return run

    def export():
        js("""sldBridge = {on_svg_exported: function(m) { window.__svg = m; }};
              exportSvg();""")
        QTimer.singleShot(200, lambda: js(
            """JSON.stringify({len: window.__svg.length,
                hidden: (window.__svg.match(/display: none/g) || []).length,
                lod: /class="[^"]*lod-(far|mid)/.test(window.__svg),
                names: window.__svg.indexOf('sld-names') >= 0})""",
            lambda r: (out.__setitem__("export", json.loads(r)), nxt())))

    steps += [lambda: (zoom(0.03), QTimer.singleShot(400, probe("far")))]
    steps += [lambda: (zoom(0.3), QTimer.singleShot(400, probe("mid")))]
    steps += [lambda: (zoom(1.0), QTimer.singleShot(400, probe("near")))]
    steps += [lambda: (zoom(2.0, 0.05, 0.05), QTimer.singleShot(400, probe("corner")))]
    steps += [export]

    def nxt():
        if steps:
            steps.pop(0)()
        else:
            print("RESULT " + json.dumps(out))
            app.quit()

    def ready():
        js("diagramBounds !== null && document.querySelectorAll('.bus-bar').length > 0",
           lambda ok: nxt() if ok else QTimer.singleShot(200, ready))

    view.loadFinished.connect(lambda _ok: (js("layoutAndRender(" + json.dumps(data) + ")"),
                                           QTimer.singleShot(300, ready)))
    view.load(QUrl.fromLocalFile(str(Path(vis_pkg.__file__).parent / "resources" / "sld.html")))
    QTimer.singleShot(60000, app.quit)
    app.exec()
''')


@pytest.fixture(scope="module")
def result():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", _SCRIPT], capture_output=True,
                          text=True, timeout=120, env=env)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    if not lines:
        pytest.skip("offscreen QtWebEngine unavailable: " + (proc.stderr or "")[-400:])
    return json.loads(lines[-1][len("RESULT "):])


def test_far_zoom_shows_structure_and_names_only(result):
    far = result["far"]
    assert "lod-far" in far["cls"]
    assert far["names"] > 0                 # substation names at a fixed size


def test_mid_zoom_hides_text_but_keeps_names(result):
    mid = result["mid"]
    assert "lod-mid" in mid["cls"] and "lod-far" not in mid["cls"]
    assert mid["names"] > 0


def test_near_zoom_shows_everything(result):
    near = result["near"]
    assert "lod-" not in near["cls"]
    assert near["names"] == 0               # the diagram's own titles take over


def test_zoomed_in_corner_culls_the_rest(result):
    assert result["far"]["culled"] == 0     # whole diagram in view
    assert result["corner"]["culled"] > 0


def test_export_is_the_full_diagram(result):
    exp = result["export"]
    assert exp["len"] > 0
    assert exp["hidden"] == 0 and not exp["lod"] and not exp["names"]
