"""Substation drill-down of the SLD renderer.

Double-clicking a substation frame opens that substation on its own, with
each line to another substation cut to a stub that names (and links to) the
far substation. The renderer is JavaScript, so the test loads ``sld.html``
in an offscreen QtWebEngine view and drives it from JS, in a subprocess so
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
    import json
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

    # 6 x 5 substations (220/110 kV + a generator) ~40 km apart, each joined
    # to its east and south neighbours; plus two bare tap points (junctions):
    # J1 between Sub 5_4 and Sub 4_4, J2 joining Sub 0_0, Sub 2_0 and Sub 0_2.
    st = GuiSystemState()
    st.nodes = [GuiNode(index=0, name="N")]
    st.buses, st.transformers, st.transmission_lines, st.generators = {}, [], [], {}

    def line(lid, a, b):
        st.transmission_lines.append(GuiTransmissionLine(
            line_id=lid, from_bus=a, to_bus=b, capacity_mw=200.0, voltage_kv=220.0))

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
                    line(f"L{k}_{di}{dj}", f"h{k}", f"h{i + di}_{j + dj}")
    st.buses["j1"] = GuiBus(bus_id="j1", parent_node=0, voltage_kv=220, name="Tap 1",
                            latitude=22.0 + 0.4 * 4 + 0.15, longitude=-80.0 + 0.4 * 4.5)
    line("J1a", "h5_4", "j1")
    line("J1b", "j1", "h4_4")
    st.buses["j2"] = GuiBus(bus_id="j2", parent_node=0, voltage_kv=220, name="Tap 2",
                            latitude=22.0 + 0.15, longitude=-80.0 + 0.15)
    line("J2a", "h0_0", "j2")
    line("J2b", "j2", "h2_0")
    line("J2c", "j2", "h0_2")
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

    # Key of a substation by name, and the screen point of its frame centre.
    HELPERS = r"""
    window.__key = function(name) {
        var g = currentData.nodeGroups.filter(function(g) { return g.name === name; })[0];
        return g ? _groupKey(g) : null;
    };
    window.__centre = function(name) {
        var k = window.__key(name);
        var f = _frames.filter(function(f) { return f.key === k; })[0];
        var t = d3.zoomTransform(svg.node()), r = svg.node().getBoundingClientRect();
        return [r.left + t.applyX(f.x + f.w / 2), r.top + t.applyY(f.y + f.h / 2)];
    };
    window.__dbl = function(x, y) {
        svg.node().dispatchEvent(new MouseEvent('dblclick',
            { clientX: x, clientY: y, bubbles: true, cancelable: true, view: window }));
    };
    window.__state = function() {
        var stubs = [];
        document.querySelectorAll('.layer-edges .edge').forEach(function(g) {
            var t = g.querySelector('.stub-end');
            if (!t) return;
            var n = g.querySelector('.edge-path').getAttribute('d')
                     .match(/-?[\d.]+(e-?\d+)?/g).map(Number);
            stubs.push({ text: t.textContent, link: t.classList.contains('stub-link'),
                         pts: n });
        });
        var fr = _frames.length === 1 ? _frames[0] : null;
        var t = d3.zoomTransform(svg.node());
        return {
            site: _site ? _site.name : null,
            crumb: document.getElementById('sld-breadcrumb').style.display,
            crumbText: document.getElementById('sld-bc-site').textContent,
            buses: document.querySelectorAll('.bus-bar').length,
            frame: fr ? [fr.x, fr.y, fr.x + fr.w, fr.y + fr.h] : null,
            stubs: stubs, k: t.k, x: t.x, y: t.y,
            lod: svg.attr('class') || '',
            flowArrows: document.querySelectorAll('.layer-ops polygon').length,
        };
    };
    """

    def probe(tag):
        js("JSON.stringify(window.__state())",
           lambda r: (out.__setitem__(tag, json.loads(r)), nxt()))

    def later(fn, ms=400):
        return lambda: QTimer.singleShot(ms, fn)

    def dbl_on(name):
        js(f"var c = window.__centre({json.dumps(name)}); window.__dbl(c[0], c[1]);")

    steps += [later(lambda: probe("system"))]
    steps += [lambda: (dbl_on("Sub 2_2"), QTimer.singleShot(400, lambda: probe("open")))]
    # Results overlay on the open substation's lines.
    steps += [lambda: (js("""updateOperationalData(JSON.stringify({lines: (function() {
            var o = {};
            currentLayouted.edges.forEach(function(e) {
                if ((e.properties || {}).elementType === 'transmission')
                    o['edge_' + e.properties.elementId] = {flow_mw: 50, capacity_mw: 200,
                                                          utilization_pct: 25};
            });
            return o; })()}))"""), QTimer.singleShot(300, lambda: probe("ops")))]
    # Follow the stub to Sub 3_2.
    steps += [lambda: (js("""Array.from(document.querySelectorAll('.stub-link'))
            .filter(function(t) { return /Sub 3_2$/.test(t.textContent); })[0]
            .dispatchEvent(new MouseEvent('click', {bubbles: true}));"""),
                       QTimer.singleShot(400, lambda: probe("followed")))]
    # Escape: back to the system at the zoom it was left at.
    steps += [lambda: (js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}))"),
                       QTimer.singleShot(400, lambda: probe("back")))]
    # Tap points: the stub names the substations behind them.
    steps += [lambda: (js("openSite(window.__key('Sub 5_4'))"),
                       QTimer.singleShot(300, lambda: probe("tap1")))]
    steps += [lambda: (js("openSite(window.__key('Sub 0_0'))"),
                       QTimer.singleShot(300, lambda: probe("tap2")))]
    # Selecting an element of another substation shows the whole system.
    steps += [lambda: (js("highlightElement('generator', 'g5_0')"),
                       QTimer.singleShot(300, lambda: probe("highlight")))]
    # Double-click away from every frame zooms in instead.
    steps += [lambda: (js("""fitView(false);
            var r = svg.node().getBoundingClientRect(); window.__dbl(r.left + 2, r.top + 2);"""),
                       QTimer.singleShot(600, lambda: probe("empty")))]

    def nxt():
        if steps:
            steps.pop(0)()
        else:
            print("RESULT " + json.dumps(out))
            app.quit()

    def ready():
        js("diagramBounds !== null && document.querySelectorAll('.bus-bar').length > 0",
           lambda ok: (js(HELPERS), nxt()) if ok else QTimer.singleShot(200, ready))

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


def _names(state):
    return sorted(s["text"].replace("→", "").replace("←", "").strip()
                  for s in state["stubs"])


def test_double_click_opens_the_substation(result):
    sys_view, opened = result["system"], result["open"]
    assert sys_view["site"] is None and sys_view["crumb"] == "none"
    assert opened["site"] == "Sub 2_2"
    assert opened["crumb"] == "flex" and opened["crumbText"] == "Sub 2_2"
    assert opened["buses"] == 2                     # its 220 and 110 kV bars only
    assert sys_view["buses"] > 60
    # Fitted to the view (and not zoomed in further by the same double-click).
    x0, y0, x1, y1 = opened["frame"]
    k, tx, ty = opened["k"], opened["x"], opened["y"]
    assert 0 <= x0 * k + tx and x1 * k + tx <= 1200
    assert 0 <= y0 * k + ty and y1 * k + ty <= 800


def test_lines_to_other_substations_end_in_named_stubs(result):
    opened = result["open"]
    assert _names(opened) == ["Sub 1_2", "Sub 2_1", "Sub 2_3", "Sub 3_2"]
    assert all(s["link"] for s in opened["stubs"])
    x0, y0, x1, y1 = opened["frame"]
    for s in opened["stubs"]:
        ax, ay, bx, by = s["pts"]                   # one straight segment
        assert ax == pytest.approx(bx, abs=0.5) or ay == pytest.approx(by, abs=0.5)
        inside = [x0 <= x <= x1 and y0 <= y <= y1 for x, y in ((ax, ay), (bx, by))]
        assert sorted(inside) == [False, True]      # from its bar to past the frame


def test_open_substation_keeps_full_detail_and_overlays(result):
    assert "lod-" not in result["open"]["lod"]
    assert result["ops"]["flowArrows"] == 4         # one per stub line


def test_stub_link_opens_the_far_substation(result):
    assert result["followed"]["site"] == "Sub 3_2"
    assert "Sub 2_2" in _names(result["followed"])


def test_escape_returns_to_the_system_view(result):
    back, sys_view = result["back"], result["system"]
    assert back["site"] is None and back["crumb"] == "none"
    assert back["buses"] == sys_view["buses"]
    assert back["k"] == pytest.approx(sys_view["k"])
    assert (back["x"], back["y"]) == pytest.approx((sys_view["x"], sys_view["y"]))


def test_stubs_through_tap_points_name_the_substations_behind(result):
    assert "Sub 4_4" in _names(result["tap1"])
    assert "Sub 0_2 / Sub 2_0" in _names(result["tap2"])


def test_selecting_an_element_elsewhere_shows_the_system(result):
    assert result["highlight"]["site"] is None
    assert result["highlight"]["buses"] == result["system"]["buses"]


def test_double_click_off_the_frames_zooms_in(result):
    empty = result["empty"]
    assert empty["site"] is None
    assert empty["k"] == pytest.approx(2 * result["system"]["k"], rel=0.01)
