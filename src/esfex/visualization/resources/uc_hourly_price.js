/* Hourly Price Profile (UC) — system-average LMP per hour, with the
 * full per-node spread shown as a band underneath. Payload:
 *
 *   { year, hours: [0..N-1], system_avg: [...], node_min: [...], node_max: [...] }
 *
 * The band (min..max across nodes) highlights congestion hours: a wide
 * band means LMPs diverge between nodes (transmission limit binding);
 * a narrow band means the system price is uniform.
 */
"use strict";


function _ucClearPlot() {
    // Wipe the previous render so a stale chart from another
    // system selection does not leak through when the current
    // payload has no data.
    try { Plotly.purge("plot"); } catch (e) {}
}
let bridge = null;

document.addEventListener("DOMContentLoaded", () => {
    new QWebChannel(qt.webChannelTransport, channel => {
        bridge = channel.objects.loader;
        refresh();
    });
});

function refresh() {
    if (!bridge) return;
    bridge.get_data(rawJson => {
        let data = null;
        try { data = JSON.parse(rawJson); }
        catch (e) { _ucClearPlot(); showStatus(_t("web.common.bad_payload", { error: e.message }), true); return; }
        if (!data || data.error) {
            _ucClearPlot(); showStatus(data && data.error ? data.error : _t("web.common.no_data"), false);
            return;
        }
        if (!data.hours || data.hours.length === 0) {
            _ucClearPlot(); showStatus(_t("web.charts.uc_hourly_price.no_data"), false);
            return;
        }
        try { renderProfile(data); hideStatus(); }
        catch (e) { showStatus(_t("web.common.render_error", { error: e.message }), true); console.error(e); }
    });
}

function showStatus(text, isError) {
    const s = document.getElementById("status");
    s.textContent = text;
    s.style.display = "block";
    s.style.color = isError ? "#c0392b" : "#7F8C8D";
}
function hideStatus() {
    document.getElementById("status").style.display = "none";
}

function renderProfile(data) {
    const x = data.hours;
    const traces = [];

    // Per-node band (max — lower bound first, then upper bound with
    // fill="tonexty" to draw the area between the two lines).
    if (data.node_min && data.node_max
        && data.node_min.length === x.length
        && data.node_max.length === x.length) {
        traces.push({
            x: x, y: data.node_min,
            type: "scatter", mode: "lines", name: _t("web.charts.uc_hourly_price.nodal_min"),
            line: { color: "rgba(52, 152, 219, 0)", width: 0 },
            showlegend: false,
            hoverinfo: "skip",
        });
        traces.push({
            x: x, y: data.node_max,
            type: "scatter", mode: "lines", name: _t("web.charts.uc_hourly_price.nodal_range"),
            line: { color: "rgba(52, 152, 219, 0)", width: 0 },
            fill: "tonexty",
            fillcolor: "rgba(52, 152, 219, 0.18)",
            hovertemplate: _t("web.charts.uc_hourly_price.hover_max") + "<extra></extra>",
        });
    }

    // System-average line — the headline series.
    traces.push({
        x: x, y: data.system_avg,
        type: "scatter", mode: "lines", name: _t("web.charts.uc_hourly_price.system_average"),
        line: { color: "#2C3E50", width: 2 },
        hovertemplate: _t("web.charts.uc_hourly_price.hover_avg") + "<extra></extra>",
    });

    const layout = {
        margin: { t: 60, r: 30, b: 60, l: 70 },
        showlegend: true,
        legend: {
            orientation: "h",
            x: 0.5, xanchor: "center",
            y: 1.02, yanchor: "bottom",
            font: { size: 10 },
            bgcolor: "rgba(255,255,255,0.85)",
            bordercolor: "rgba(0,0,0,0.1)",
            borderwidth: 1,
        },
        xaxis: { title: "<b>" + _t("web.charts.uc_hourly_price.axis_hour") + "</b>" },
        yaxis: { title: "<b>" + _t("web.charts.uc_hourly_price.axis_lmp") + "</b>", rangemode: "tozero" },
    };
    Plotly.purge("plot");
    Plotly.newPlot("plot", traces, layout, {
        responsive: true,
        displaylogo: false,
        modeBarButtonsToRemove: ["lasso2d", "select2d"],
    });
}

window.addEventListener("resize", () => {
    const plot = document.getElementById("plot");
    if (plot && plot.data) Plotly.Plots.resize(plot);
});
