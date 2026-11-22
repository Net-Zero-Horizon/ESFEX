/* Net Load Duration Curve (UC) — the classic capacity-sizing diagnostic.
 *
 * Net load = demand − renewable generation. Sorted descending gives a
 * "duration curve": at the left, the few hours where everything must
 * fire; at the right, hours with surplus RE (net load may be negative).
 *
 * Side-by-side: gross demand duration curve vs. net load. The shrink
 * from one to the other is the contribution of RE to peak shaving.
 *
 * Payload:
 *   { year, pct_hours: [0..100],
 *     demand_sorted: [...], net_load_sorted: [...],
 *     peak_demand_mw, peak_netload_mw,
 *     min_netload_mw, hours_negative_netload }
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
        bridge = channel.objects.loader; refresh();
    });
});
function refresh() {
    if (!bridge) return;
    bridge.get_data(rawJson => {
        let data = null;
        try { data = JSON.parse(rawJson); }
        catch (e) { _ucClearPlot(); showStatus(_t("web.common.bad_payload", { error: e.message }), true); return; }
        if (!data || data.error) { _ucClearPlot(); showStatus(data && data.error ? data.error : _t("web.common.no_data"), false); return; }
        if (!data.demand_sorted || !data.demand_sorted.length) { _ucClearPlot(); showStatus(_t("web.charts.uc_netload_duration.no_data"), false); return; }
        try { renderDuration(data); hideStatus(); }
        catch (e) { showStatus(_t("web.common.render_error", { error: e.message }), true); console.error(e); }
    });
}
function showStatus(t, e) { const s = document.getElementById("status"); s.textContent = t; s.style.display = "block"; s.style.color = e ? "#c0392b" : "#7F8C8D"; }
function hideStatus() { document.getElementById("status").style.display = "none"; }

function renderDuration(data) {
    const x = data.pct_hours;
    const traces = [];
    const hoverTop = _t("web.charts.uc_netload_duration.hover_top");
    traces.push({
        x: x, y: data.demand_sorted,
        type: "scatter", mode: "lines",
        name: _t("web.charts.uc_netload_duration.gross_demand"),
        line: { color: "#7F8C8D", width: 2, dash: "dash" },
        hovertemplate: hoverTop + "<extra>" + _t("web.charts.uc_netload_duration.extra_gross") + "</extra>",
    });
    traces.push({
        x: x, y: data.net_load_sorted,
        type: "scatter", mode: "lines",
        name: _t("web.charts.uc_netload_duration.net_load"),
        line: { color: "#2C3E50", width: 2 },
        fill: "tozeroy", fillcolor: "rgba(44, 62, 80, 0.10)",
        hovertemplate: hoverTop + "<extra>" + _t("web.charts.uc_netload_duration.extra_net") + "</extra>",
    });

    const negFrac = data.hours_negative_netload > 0
        ? _t("web.charts.uc_netload_duration.surplus_share", { pct: (100 * data.hours_negative_netload / x.length).toFixed(1) })
        : _t("web.charts.uc_netload_duration.no_surplus");
    const subTitle =
        [_t("web.charts.uc_netload_duration.peak_gross", { mw: data.peak_demand_mw.toFixed(0) }),
         _t("web.charts.uc_netload_duration.peak_net", { mw: data.peak_netload_mw.toFixed(0) }),
         _t("web.charts.uc_netload_duration.min_net", { mw: data.min_netload_mw.toFixed(0) }),
         negFrac].join(" &nbsp;|&nbsp; ");

    const layout = {
        margin: { t: 80, r: 30, b: 60, l: 70 },
        showlegend: true,
        annotations: [
            { text: subTitle,
              x: 0.5, xref: "paper", xanchor: "center",
              y: 1.08, yref: "paper", yanchor: "bottom",
              showarrow: false, font: { size: 11, color: "#5D6D7E" } },
        ],
        legend: {
            orientation: "h", x: 0.5, xanchor: "center",
            y: 1.02, yanchor: "bottom", font: { size: 10 },
            bgcolor: "rgba(255,255,255,0.85)", bordercolor: "rgba(0,0,0,0.1)", borderwidth: 1,
        },
        shapes: [
            { type: "line", xref: "x domain", yref: "y",
              x0: 0, x1: 1, y0: 0, y1: 0,
              line: { color: "#000000", width: 1 } },
        ],
        xaxis: { title: "<b>" + _t("web.charts.uc_netload_duration.axis_top_hours") + "</b>", range: [0, 100] },
        yaxis: { title: "<b>" + _t("web.charts.uc_netload_duration.axis_power") + "</b>" },
    };
    Plotly.purge("plot");
    Plotly.newPlot("plot", traces, layout, {
        responsive: true, displaylogo: false,
        modeBarButtonsToRemove: ["lasso2d", "select2d"],
    });
}

window.addEventListener("resize", () => {
    const plot = document.getElementById("plot");
    if (plot && plot.data) Plotly.Plots.resize(plot);
});
