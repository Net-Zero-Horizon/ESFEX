"""The SLD legend's line sample is neutral; a voltage mismatch has its own row.

A line is drawn in the colour of its voltage (the Voltage Levels rows), but
the Transmission Line sample took the colour of the first line drawn: red on
Hokkaido, where that line joins buses of different voltages. Such lines are
drawn red and dashed and now have their own legend row.

The renderer is JavaScript, so the test loads ``sld.html`` in an offscreen
QtWebEngine view in a subprocess, as test_sld_view_lod does.
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
    import json
    from pathlib import Path
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtWidgets import QApplication
    from PySide6.QtWebEngineWidgets import QWebEngineView

    from esfex.visualization.data.gui_model import (
        GuiBus, GuiNode, GuiSystemState, GuiTransformer, GuiTransmissionLine,
    )
    from esfex.visualization.sld.graph_builder import build_elk_graph
    import esfex.visualization as vis_pkg

    st = GuiSystemState()
    st.nodes = [GuiNode(index=0, name="A"), GuiNode(index=1, name="B")]
    st.buses = {
        "a220": GuiBus(bus_id="a220", parent_node=0, voltage_kv=220, latitude=22.0, longitude=-80.0),
        "a110": GuiBus(bus_id="a110", parent_node=0, voltage_kv=110, latitude=22.0, longitude=-80.0),
        "b220": GuiBus(bus_id="b220", parent_node=1, voltage_kv=220, latitude=22.5, longitude=-80.0),
        "b138": GuiBus(bus_id="b138", parent_node=1, voltage_kv=138, latitude=22.5, longitude=-80.0),
    }
    # The first line drawn joins 110 and 138 kV buses: a voltage mismatch
    st.transmission_lines = [
        GuiTransmissionLine(line_id="bad", from_bus="a110", to_bus="b138", capacity_mw=100.0),
        GuiTransmissionLine(line_id="hv", from_bus="a220", to_bus="b220", capacity_mw=500.0),
    ]
    st.transformers = [GuiTransformer(name="T", from_bus="a220", to_bus="a110",
                                      rated_power_mva=200.0)]
    data = json.dumps(build_elk_graph(st))

    app = QApplication([])
    from esfex.visualization import i18n, web_i18n
    i18n.init_i18n("en")
    web_i18n.install()
    view = QWebEngineView()
    view.resize(1200, 800)
    view.show()
    page = view.page()

    PROBE = """JSON.stringify(Array.from(document.querySelectorAll('.sld-legend line'))
        .map(function(l) {
            var t = l.parentNode.querySelectorAll('text');
            var y = +l.getAttribute('y1'), label = '';
            t.forEach(function(e) { if (Math.abs(+e.getAttribute('y') - 3 - y) < 1) label = e.textContent; });
            return {label: label, stroke: l.getAttribute('stroke'),
                    dash: l.getAttribute('stroke-dasharray')};
        }))"""

    def ready():
        page.runJavaScript(
            "document.querySelectorAll('.sld-legend line').length > 0",
            lambda ok: page.runJavaScript(PROBE, done) if ok else QTimer.singleShot(200, ready))

    def done(r):
        print("RESULT " + r)
        app.quit()

    view.loadFinished.connect(lambda _ok: (
        page.runJavaScript("layoutAndRender(" + json.dumps(data) + ")"),
        QTimer.singleShot(300, ready)))
    view.load(QUrl.fromLocalFile(str(Path(vis_pkg.__file__).parent / "resources" / "sld.html")))
    QTimer.singleShot(60000, app.quit)
    app.exec()
''')


@pytest.fixture(scope="module")
def rows():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, "-c", _SCRIPT], capture_output=True,
                          text=True, timeout=120, env=env)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    if not lines:
        pytest.skip("offscreen QtWebEngine unavailable: " + (proc.stderr or "")[-400:])
    return {r["label"]: r for r in json.loads(lines[-1][len("RESULT "):]) if r["label"]}


def test_line_sample_is_neutral(rows):
    line = rows["Transmission Line"]
    assert line["stroke"] == "#6B7B8D"
    assert line["dash"] == "none"


def test_voltage_mismatch_has_its_own_row(rows):
    row = rows["Voltage mismatch"]
    assert row["stroke"] == "#E74C3C"
    assert row["dash"] == "6,4"
