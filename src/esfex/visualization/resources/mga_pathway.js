/* MGA — Pathway Divergence (small multiples) */
"use strict";
let bridge = null;
document.addEventListener("DOMContentLoaded", () => {
    new QWebChannel(qt.webChannelTransport, channel => {
        bridge = channel.objects.loader; refresh();
    });
});
function refresh() {
    if (!bridge) return;
    bridge.get_data(raw => {
        let data; try { data = JSON.parse(raw); }
        catch (e) { showStatus(_t("web.common.bad_payload", { error: e.message }), true); return; }
        if (!data || data.error) {
            showStatus(data && data.error ? data.error : _t("web.common.no_data"), false); return;
        }
        try { render(data); hideStatus(); }
        catch (e) { showStatus(_t("web.common.render_error", { error: e.message }), true); console.error(e); }
    });
}
function showStatus(text, isError) {
    const s = document.getElementById("status");
    s.textContent = text; s.style.display = "block";
    s.style.color = isError ? "#c0392b" : "#7F8C8D";
    const p = document.getElementById("plot"); if (p) p.style.display = "none";
}
function hideStatus() {
    const s = document.getElementById("status"); if (s) s.style.display = "none";
    const p = document.getElementById("plot"); if (p) p.style.display = "";
}

// MGA technology categories, as _mga_category (results_charts.py) names
// them: Solar / Wind / Other RE / Storage / Thermal / Other, and "All"
// for the treemap root. The ids stay in the data; only the shown text
// is translated.
function mgaCatSlug(cat) {
    return String(cat).toLowerCase().replace(/\s+/g, "_");
}
function mgaCategoryLabel(cat) {
    const names = {
        solar: _t("web.charts.helpers.mga_cat_solar"), wind: _t("web.charts.helpers.mga_cat_wind"),
        other_re: _t("web.charts.helpers.mga_cat_other_re"), storage: _t("web.charts.helpers.mga_cat_storage"),
        thermal: _t("web.charts.helpers.mga_cat_thermal"), other: _t("web.charts.helpers.mga_cat_other"),
        all: _t("web.charts.helpers.mga_cat_all"),
    };
    return names[mgaCatSlug(cat)] || cat;
}

function render(data) {
    const pw = data.pathways; const years = data.years;
    if (!pw || !pw.alts || pw.alts.length === 0) {
        showStatus(_t("web.charts.mga_pathway.no_data"), false); return;
    }
    const alts = pw.alts;
    const cols = 4, rows = Math.ceil(alts.length / cols);
    const xGap = 0.025, yGap = 0.14;
    const cellW = (1 - xGap*(cols-1)) / cols;
    const topPad = 0.04;
    const cellH = (1 - topPad - yGap*(rows-1)) / rows;
    const traces = [];
    const layout = {
        margin: { l: 40, r: 10, t: 50, b: 80 },
        title: { text: "<span style='font-size:11px;color:#7f8c8d'>" + _t("web.charts.mga_pathway.subtitle") + "</span>",
                 x: 0.5, xanchor: "center", y: 0.98, yanchor: "top", font: { size: 13 } },
        showlegend: true,
        legend: { orientation: "h", y: -0.10, x: 0.5, xanchor: "center",
                  yanchor: "top", font: { size: 10 } },
        annotations: []
    };
    alts.forEach((a, idx) => {
        const r = Math.floor(idx/cols), c = idx % cols;
        const xS = c * (cellW + xGap), xE = xS + cellW;
        const yE = 1 - topPad - r * (cellH + yGap), yS = yE - cellH;
        const axId = idx === 0 ? "" : String(idx + 1);
        layout["xaxis"+axId] = { domain: [xS, xE], anchor: "y"+axId,
            showticklabels: r === rows-1, gridcolor: "rgba(0,0,0,0.05)",
            ticks: "outside", tickfont: { size: 9 }, automargin: false };
        layout["yaxis"+axId] = { domain: [yS, yE], anchor: "x"+axId,
            showticklabels: c === 0, gridcolor: "rgba(0,0,0,0.05)",
            tickfont: { size: 9 }, automargin: false, rangemode: "tozero" };
        const altName = _t("web.charts.mga_pathway.alt_label", { id: a.id });
        layout.annotations.push({
            text: a.is_optimal ? "<b>" + altName + " ★</b>" : altName,
            xref: "x"+axId+" domain", yref: "y"+axId+" domain",
            x: 0.5, y: 1.02, xanchor: "center", yanchor: "bottom",
            showarrow: false,
            font: { size: 10, color: a.is_optimal ? "#E74C3C" : "#2C3E50" }
        });
        pw.categories.forEach((cat, ci) => {
            const catName = mgaCategoryLabel(cat);
            traces.push({
                type: "scatter", mode: "lines",
                x: years, y: a.stack_mw[cat],
                stackgroup: "one", name: catName,
                line: { width: 0, color: pw.colors[ci] },
                fillcolor: pw.colors[ci],
                xaxis: "x"+axId, yaxis: "y"+axId,
                legendgroup: cat, showlegend: idx === 0,
                hovertemplate: "%{x}: %{y:,.0f} MW<extra>" + catName + "</extra>"
            });
        });
    });
    Plotly.purge("plot");
    Plotly.newPlot("plot", traces, layout, { responsive: true, displaylogo: false });
}

window.addEventListener("resize", () => {
    const el = document.getElementById("plot");
    if (el && el.data) Plotly.Plots.resize(el);
});
