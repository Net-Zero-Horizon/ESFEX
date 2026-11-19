/* Carbon & System Penalty Costs — Plotly interactive chart.
 *
 * (a) CO₂ emissions per year (bar, Mt) + CO₂ emission intensity
 *     (g CO₂ / kWh) on the secondary y axis — the line shows how
 *     quickly the system is decarbonising, independent of how demand
 *     itself grows.
 * (b) Reliability / reserve penalty quantities per year (stacked bars,
 *     MWh): loss-of-load + dynamic & static reserve violations. A run
 *     with no penalties shows empty bars — confirming the operational
 *     feasibility from a single glance.
 */
"use strict";

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
        catch (e) { showStatus(_t("web.common.bad_payload", { error: e.message }), true); return; }
        if (!data || data.error) {
            showStatus(data && data.error ? data.error : _t("web.common.no_data"), false);
            return;
        }
        if (!data.years || data.years.length === 0) {
            showStatus(_t("web.common.no_data"), false);
            return;
        }
        try {
            renderCarbon(data);
            hideStatus();
        } catch (e) {
            showStatus(_t("web.common.render_error", { error: e.message }), true);
            console.error("[carbon_penalty.js] render threw:", e);
        }
    });
}

function showStatus(text, isError) {
    const s = document.getElementById("status");
    if (!s) return;
    s.textContent = text;
    s.style.display = "block";
    s.style.color = isError ? "#c0392b" : "#7F8C8D";
}
function hideStatus() {
    const s = document.getElementById("status");
    if (s) s.style.display = "none";
}

function renderCarbon(data) {
    const traces = [];
    const years = data.years;

    // ── Subplot (a): CO2 emissions bars + intensity line ──
    if (data.co2_mt && data.co2_mt.length) {
        traces.push({
            type: "bar",
            x: years, y: data.co2_mt,
            name: _t("web.charts.carbon_penalty.trace_co2"),
            marker: { color: "#7F8C8D",
                      line: { color: "#FFFFFF", width: 0.5 } },
            xaxis: "x", yaxis: "y",
            hovertemplate: _t("web.charts.carbon_penalty.hover_co2"),
        });
    }
    if (data.co2_intensity_g_per_kwh && data.co2_intensity_g_per_kwh.length) {
        traces.push({
            type: "scatter", mode: "lines+markers",
            x: years, y: data.co2_intensity_g_per_kwh,
            name: _t("web.charts.carbon_penalty.trace_intensity"),
            line: { color: "#E67E22", width: 2.5 },
            marker: { color: "#E67E22", size: 7,
                      line: { color: "#FFFFFF", width: 1 } },
            xaxis: "x", yaxis: "y2",
            hovertemplate: _t("web.charts.carbon_penalty.hover_intensity"),
        });
    }

    // ── Subplot (b): penalty quantities (stacked bars) ──
    const series = [
        { key: "loss_load_mwh", label: _t("web.charts.carbon_penalty.series_loss_load"),
          color: "#E74C3C" },
        { key: "reserve_dynamic_violation_mwh",
          label: _t("web.charts.carbon_penalty.series_res_dynamic"),
          color: "#9B59B6" },
        { key: "reserve_static_violation_mwh",
          label: _t("web.charts.carbon_penalty.series_res_static"),
          color: "#3498DB" },
    ];
    let anyPenalty = false;
    for (const s of series) {
        const vals = data[s.key];
        if (!vals || !vals.length) continue;
        if (vals.some(v => v > 0)) anyPenalty = true;
        traces.push({
            type: "bar",
            x: years, y: vals,
            name: s.label,
            marker: { color: s.color,
                      line: { color: "#FFFFFF", width: 0.5 } },
            xaxis: "x2", yaxis: "y3",
            offsetgroup: "pen",
            hovertemplate: _t("web.charts.carbon_penalty.hover_penalty", { label: s.label }),
        });
    }

    const layout = {
        margin: { t: 70, r: 8, b: 70, l: 80 },
        showlegend: true,
        barmode: "relative",
        annotations: [
            { text: "<b>" + _t("web.charts.carbon_penalty.title_a") + "</b>",
              x: 0.435, xref: "paper", xanchor: "center",
              y: 1.00, yref: "paper", yanchor: "bottom",
              showarrow: false, font: { size: 13 } },
            { text: "<b>" + _t("web.charts.carbon_penalty.title_b") + "</b>" +
                    (anyPenalty ? ""
                        : "  <i>" + _t("web.charts.carbon_penalty.no_penalties") + "</i>"),
              x: 0.435, xref: "paper", xanchor: "center",
              y: 0.46, yref: "paper", yanchor: "bottom",
              showarrow: false, font: { size: 13 } },
        ],
        legend: {
            orientation: "v",
            x: 0.88, xanchor: "left",
            y: 0.5, yanchor: "middle",
            font: { size: 10 },
            tracegroupgap: 6,
            bgcolor: "rgba(255,255,255,0.85)",
            bordercolor: "rgba(0,0,0,0.1)",
            borderwidth: 1,
        },
        xaxis: {
            domain: [0, 0.87], anchor: "y",
            type: "category",
            showticklabels: false,
            gridcolor: "rgba(0,0,0,0.08)",
        },
        yaxis: {
            domain: [0.54, 0.98], anchor: "x",
            title: "<b>" + _t("web.charts.carbon_penalty.axis_emissions") + "</b>",
            gridcolor: "rgba(0,0,0,0.08)",
            rangemode: "tozero",
        },
        yaxis2: {
            domain: [0.54, 0.98], anchor: "x",
            overlaying: "y", side: "right",
            title: { text: "<b>" + _t("web.charts.carbon_penalty.axis_intensity") + "</b>",
                     font: { color: "#E67E22" } },
            tickfont: { color: "#E67E22" },
            showgrid: false,
            rangemode: "tozero",
        },
        xaxis2: {
            domain: [0, 0.87], anchor: "y3",
            type: "category",
            tickangle: -45,
            title: "<b>" + _t("web.charts.carbon_penalty.axis_year") + "</b>",
            gridcolor: "rgba(0,0,0,0.08)",
        },
        yaxis3: {
            domain: [0.0, 0.44], anchor: "x2",
            title: "<b>" + _t("web.charts.carbon_penalty.axis_penalty") + "</b>",
            gridcolor: "rgba(0,0,0,0.08)",
            rangemode: "tozero",
        },
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
