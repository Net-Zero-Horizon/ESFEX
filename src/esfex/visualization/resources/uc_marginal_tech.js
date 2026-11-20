/* Marginal Technology Heatmap (UC) — which tech sets the price.
 *
 * Two views:
 *   Top:    Time × tech color heatmap. Each cell encodes the
 *           marginal tech bucket as a discrete colour from the
 *           tech palette. Optionally tagged with the LMP via hover.
 *   Bottom: Bar chart of "hours marginal" — how many hours each
 *           tech set the system price.
 *   A shared legend names the colours of both panels.
 *
 * Marginal tech is computed Python-side via merit-order ex-post:
 *   1. For every hour, list ON committed gens (gen_status > 0.5).
 *   2. Among ON gens whose output is between 0 and rated capacity
 *      (i.e. dispatching partially — they have headroom to move),
 *      pick the one with the highest variable cost. That's the
 *      marginal unit; its tech bucket is the cell value.
 *   3. If no partial dispatcher exists (everyone at min or max),
 *      fall back to the highest-cost ON unit. If nothing is on,
 *      we tag the cell as "Load shed" (price = VOLL).
 *
 * Payload:
 *   { year, hours: [0..N-1],
 *     tech_indices: [0,1,2,...],
 *     tech_labels: ["Solar","Wind","Diesel",...],
 *     tech_colors: ["#...","#...",...],
 *     marginal_idx: [...],                      # index into tech_labels per hour
 *     hours_marginal: { "Diesel": 18, ... },    # tech → hour count
 *     hour_lmp: [...]                           # USD/MWh per hour (for hover)
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
            _ucClearPlot(); showStatus(_t("web.charts.uc_marginal_tech.no_data"), false);
            return;
        }
        try { renderMarginal(data); hideStatus(); }
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

function renderMarginal(data) {
    resetLabelFits();
    const x = data.hours;
    const N = data.tech_labels.length;
    // Build a discrete colorscale from the per-tech palette: N plateaus
    // of [normalised_pos, color] pairs. Each step repeats its boundary
    // position, so the scale runs exactly from 0 to 1 (Plotly drops a
    // scale that does not end at 1 and paints its default instead).
    const colorscale = [];
    for (let i = 0; i < N; i++) {
        colorscale.push([i / N, data.tech_colors[i]]);
        colorscale.push([(i + 1) / N, data.tech_colors[i]]);
    }

    // Single-row heatmap of the marginal tech index over the horizon.
    const z = [data.marginal_idx];
    const customdata = [data.marginal_idx.map((idx, h) => [
        data.tech_labels[idx] || "—",
        (data.hour_lmp && data.hour_lmp[h] != null) ? data.hour_lmp[h] : null,
    ])];
    const heatmap = {
        z: z, x: x, y: [_t("web.charts.uc_marginal_tech.row_label")],
        type: "heatmap",
        colorscale: colorscale,
        zmin: -0.5, zmax: N - 0.5,
        showscale: false,
        customdata: customdata,
        hovertemplate: _t("web.charts.uc_marginal_tech.hover_cell") + "<extra></extra>",
        xaxis: "x", yaxis: "y",
    };

    // Bar chart of hours-marginal per tech, ordered by frequency: one
    // trace per tech that was marginal at least one hour, so the legend
    // names the colours of both panels.
    const techByCount = Object.entries(data.hours_marginal || {})
        .filter(([, c]) => c > 0)
        .sort((a, b) => b[1] - a[1]);
    const barX = techByCount.map(([t]) => t);
    const bars = techByCount.map(([t, c]) => {
        const idx = data.tech_labels.indexOf(t);
        return {
            x: [t], y: [c], type: "bar", name: t,
            marker: { color: idx >= 0 ? data.tech_colors[idx] : "#95A5A6", opacity: 0.9 },
            hovertemplate: "%{x}: %{y} h<extra></extra>",
            xaxis: "x2", yaxis: "y2",
        };
    });

    const panelTitle = (text, y) => ({
        text: "<b>" + text + "</b>", x: 0, xref: "paper", xanchor: "left",
        y: y, yref: "paper", yanchor: "bottom", showarrow: false,
        font: { size: 12, color: "#2C3E50" },
    });
    const layout = {
        margin: { t: 90, r: 30, b: 20, l: 80 },
        grid: { rows: 2, columns: 1, pattern: "independent", roworder: "top to bottom" },
        barmode: "overlay",
        showlegend: true,
        // The legend explains the colours; toggling a trace would only
        // hide a bar, not its hours in the timeline.
        legend: { orientation: "h", x: 0.5, xanchor: "center",
                  y: 1.02, yanchor: "bottom",
                  itemclick: false, itemdoubleclick: false,
                  title: { text: _t("web.charts.uc_marginal_tech.legend_title") + ":" } },
        annotations: [
            { text: _t("web.charts.uc_marginal_tech.subtitle"),
              x: 0.5, xref: "paper", xanchor: "center",
              y: 1.12, yref: "paper", yanchor: "bottom",
              showarrow: false, font: { size: 11, color: "#5D6D7E" } },
            panelTitle(_t("web.charts.uc_marginal_tech.panel_timeline"), 0.955),
            panelTitle(_t("web.charts.uc_marginal_tech.panel_hours"), 0.49),
        ],
        xaxis:  { domain: [0, 1], anchor: "y", title: "<b>" + _t("web.charts.uc_marginal_tech.axis_hour") + "</b>" },
        yaxis:  { domain: [0.65, 0.95], showticklabels: false, ticks: "" },
        xaxis2: Object.assign({ domain: [0, 1], anchor: "y2",
                  title: "<b>" + _t("web.charts.uc_marginal_tech.axis_technology") + "</b>" },
            labelAxis({ axis: "xaxis2", labels: barX, fontSize: 11,
                        fraction: 0.25, maxChars: 24, angle: -25 })),
        yaxis2: { domain: [0.0, 0.47], title: "<b>" + _t("web.charts.uc_marginal_tech.axis_hours_marginal") + "</b>",
                  rangemode: "tozero" },
    };
    Plotly.purge("plot");
    Plotly.newPlot("plot", [heatmap, ...bars], layout, {
        responsive: true,
        displaylogo: false,
        modeBarButtonsToRemove: ["lasso2d", "select2d"],
    }).then(refitLabels);
}

window.addEventListener("resize", () => {
    const plot = document.getElementById("plot");
    if (plot && plot.data) Plotly.Plots.resize(plot);
});
