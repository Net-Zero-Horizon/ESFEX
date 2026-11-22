/* Commitment Heatmap (UC) — the signature visualisation of a Unit
 * Commitment run. Rows are committable plants (gen × node), columns are
 * hours, cells are the units online (0/1 for a single-unit plant).
 * Renewable / non-committable units are omitted so the heatmap focuses on
 * what UC actually decides.
 *
 * Payload contract (see UCCommitmentHeatmapChart._build_payload):
 *   { year, hours: [0..N-1], units: ["Diesel @ Havana", …],
 *     status: [[0..n, …], …],  # rows × cols matching units × hours
 *     num_units: [n, …] }      # units of each row's plant (optional)
 */
"use strict";


function _ucClearPlot() {
    // Wipe the previous render so a stale chart from another
    // system selection does not leak through when the current
    // payload has no data.
    try { Plotly.purge("plot"); } catch (e) {}
}
let bridge = null;
let _lastUnits = null;

const MIN_ROW_PX = 16;       // smallest row height that fits a label line
const MARGIN_T = 40;
const MARGIN_B = 60;
const LABEL_FONT = 11;
const LABEL_FRACTION = 0.3;  // labels take at most 30 % of the width

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
        if (!data.units || data.units.length === 0) {
            _ucClearPlot(); showStatus(_t("web.charts.uc_commitment_heatmap.no_data"), false);
            return;
        }
        try { renderHeatmap(data); hideStatus(); }
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

function renderHeatmap(data) {
    // Light grey for off; green for on, paler when only some of a
    // plant's units are online (a single-unit plant is off or full green).
    const colorscale = [
        [0.0, "#ECEFF1"],
        [0.001, "#ECEFF1"],
        [0.001, "#A9DFBF"],
        [1.0, "#27AE60"],
    ];
    const nUnits = data.num_units || data.status.map(() => 1);
    // Colour by the share of the plant online; hover shows the units
    const share = data.status.map((row, i) =>
        row.map(v => v / Math.max(1, nUnits[i])));
    const text = data.status.map((row, i) =>
        row.map(v => v + " / " + Math.max(1, nUnits[i])));

    const trace = {
        z: share,
        x: data.hours,
        y: data.units,
        text: text,
        type: "heatmap",
        colorscale: colorscale,
        zmin: 0, zmax: 1,
        showscale: false,
        hovertemplate: _t("web.charts.uc_commitment_heatmap.hover_cell") + "<extra></extra>",
        xgap: 1, ygap: 1,
    };

    const plot = document.getElementById("plot");
    _lastUnits = data.units;
    resetLabelFits();
    plot.style.height = _plotHeight(data.units.length) + "px";

    const layout = {
        margin: { t: MARGIN_T, r: 30, b: MARGIN_B, l: 10 },
        annotations: [
            { text: "<span style='color:#27AE60'>■</span> " + _t("web.charts.uc_commitment_heatmap.state_on")
                  + "  &nbsp; <span style='color:#ECEFF1'>■</span> " + _t("web.charts.uc_commitment_heatmap.state_off"),
              x: 0.5, xref: "paper", xanchor: "center",
              y: 1, yref: "paper", yanchor: "bottom", yshift: 6,
              showarrow: false, font: { size: 11 } },
        ],
        xaxis: { title: "<b>" + _t("web.charts.uc_commitment_heatmap.axis_hour") + "</b>", side: "bottom" },
        // Full unit names stay as the category values (hover shows
        // them); the tick text is shortened and the left margin grows
        // to fit it instead of clipping long names.
        yaxis: Object.assign({ autorange: "reversed" },  // first unit at top
            labelAxis({ axis: "yaxis", labels: data.units,
                        fraction: LABEL_FRACTION, fontSize: LABEL_FONT })),
    };
    Plotly.purge("plot");
    Plotly.newPlot("plot", [trace], layout, {
        responsive: true,
        displaylogo: false,
        modeBarButtonsToRemove: ["lasso2d", "select2d"],
    }).then(refitLabels);
}

// Fill the window; grow past it (page scrolls) only when the fleet is
// too large to give every unit a readable row.
function _plotHeight(nUnits) {
    const needed = nUnits * MIN_ROW_PX + MARGIN_T + MARGIN_B;
    return Math.max(window.innerHeight, needed);
}

window.addEventListener("resize", () => {
    const plot = document.getElementById("plot");
    if (!plot || !plot.data || !_lastUnits) return;
    plot.style.height = _plotHeight(_lastUnits.length) + "px";
    Plotly.Plots.resize(plot);
});
