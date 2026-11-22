/* Hourly Dispatch Stack (UC) — what serves load every hour.
 *
 * Stacked area: each renewable / thermal / battery-discharge tech is a
 * positive band; battery charge and curtailment are stacked downward
 * (negative); the demand curve overlays as a black line so the user
 * can see at a glance whether dispatch matches load.
 *
 * Payload contract:
 *   { year, hours: [0..N-1],
 *     gen_by_tech: { "Solar": [..], "Wind": [..], "Diesel": [..], ... },  // MW per hour, positive
 *     bat_discharge: [...],  // MW per hour, positive
 *     bat_charge: [...],     // MW per hour, positive
 *     curtailment: [...],    // MW per hour, positive
 *     load_shed: [...],      // MW per hour, positive
 *     demand: [...],         // MW per hour, positive
 *     tech_colors: { "Solar": "#F39C12", ... }
 *   }
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
            _ucClearPlot(); showStatus(_t("web.charts.uc_dispatch_stack.no_data"), false);
            return;
        }
        try { renderDispatch(data); hideStatus(); }
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

function _mean(values) {
    let sum = 0, n = 0;
    for (const v of values || []) {
        if (v != null && isFinite(v)) { sum += v; n++; }
    }
    return n ? sum / n : 0;
}

function renderDispatch(data) {
    const x = data.hours;
    const traces = [];
    const hoverMW = _t("web.charts.uc_dispatch_stack.hover_hour");
    const hoverMWAbs = _t("web.charts.uc_dispatch_stack.hover_hour_abs");

    // Generation by tech, stacked by mean output, largest first so the
    // biggest contributors sit at the bottom: the same order as the
    // Overview's hourly dispatch (dashboard_loader._read_uc_hourly).
    const gen = data.gen_by_tech || {};
    const techs = Object.keys(gen).sort(
        (a, b) => _mean(gen[b]) - _mean(gen[a]) || a.localeCompare(b)
    );
    for (const tech of techs) {
        const series = data.gen_by_tech[tech];
        if (!series || !series.length || series.every(v => !v)) continue;
        traces.push({
            x: x, y: series,
            type: "scatter", mode: "none",
            stackgroup: "pos", groupnorm: "",
            name: tech,
            fillcolor: (data.tech_colors || {})[tech] || "#95A5A6",
            hovertemplate: hoverMW + "<extra>" + tech + "</extra>",
        });
    }

    if (data.bat_discharge && data.bat_discharge.some(v => v > 0)) {
        traces.push({
            x: x, y: data.bat_discharge,
            type: "scatter", mode: "none",
            stackgroup: "pos", name: _t("web.charts.uc_dispatch_stack.battery_discharge"),
            fillcolor: "rgba(155, 89, 182, 0.85)",
            hovertemplate: hoverMW + "<extra>" + _t("web.charts.uc_dispatch_stack.battery_discharge") + "</extra>",
        });
    }
    if (data.load_shed && data.load_shed.some(v => v > 0)) {
        traces.push({
            x: x, y: data.load_shed,
            type: "scatter", mode: "none",
            stackgroup: "pos", name: _t("web.charts.uc_dispatch_stack.load_shed"),
            fillcolor: "rgba(231, 76, 60, 0.85)",
            hovertemplate: hoverMW + "<extra>" + _t("web.charts.uc_dispatch_stack.load_shed") + "</extra>",
        });
    }

    // Battery charge and curtailment go DOWN (negative stack).
    if (data.bat_charge && data.bat_charge.some(v => v > 0)) {
        traces.push({
            x: x, y: data.bat_charge.map(v => -Math.abs(v || 0)),
            type: "scatter", mode: "none",
            stackgroup: "neg", name: _t("web.charts.uc_dispatch_stack.battery_charge"),
            fillcolor: "rgba(155, 89, 182, 0.45)",
            customdata: data.bat_charge,
            hovertemplate: hoverMWAbs + "<extra>" + _t("web.charts.uc_dispatch_stack.battery_charge") + "</extra>",
        });
    }
    if (data.curtailment && data.curtailment.some(v => v > 0)) {
        traces.push({
            x: x, y: data.curtailment.map(v => -Math.abs(v || 0)),
            type: "scatter", mode: "none",
            stackgroup: "neg", name: _t("web.charts.uc_dispatch_stack.curtailment"),
            fillcolor: "rgba(241, 196, 15, 0.7)",
            customdata: data.curtailment,
            hovertemplate: hoverMWAbs + "<extra>" + _t("web.charts.uc_dispatch_stack.curtailment") + "</extra>",
        });
    }

    // Demand line on top of the stack — the reference the dispatch
    // must meet (positive sum must equal demand).
    if (data.demand && data.demand.length === x.length) {
        traces.push({
            x: x, y: data.demand,
            type: "scatter", mode: "lines", name: _t("web.charts.uc_dispatch_stack.demand"),
            line: { color: "#000000", width: 2, dash: "solid" },
            hovertemplate: hoverMW + "<extra>" + _t("web.charts.uc_dispatch_stack.demand") + "</extra>",
        });
    }

    const layout = {
        margin: { t: 60, r: 30, b: 60, l: 70 },
        showlegend: true,
        legend: {
            orientation: "h",
            x: 0.5, xanchor: "center",
            y: 1.02, yanchor: "bottom",
            font: { size: 10 },
            tracegroupgap: 8,
            bgcolor: "rgba(255,255,255,0.85)",
            bordercolor: "rgba(0,0,0,0.1)",
            borderwidth: 1,
        },
        shapes: [
            { type: "line", xref: "x domain", yref: "y",
              x0: 0, x1: 1, y0: 0, y1: 0,
              line: { color: "#000000", width: 1 } },
        ],
        xaxis: { title: "<b>" + _t("web.charts.uc_dispatch_stack.axis_hour") + "</b>" },
        yaxis: { title: "<b>" + _t("web.charts.uc_dispatch_stack.axis_power") + "</b>" },
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
