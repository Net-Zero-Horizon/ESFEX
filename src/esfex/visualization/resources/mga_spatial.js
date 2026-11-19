/* MGA — Spatial Divergence */
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

// Plotly named/explicit colorscale shims (covers modern Turbo/Plasma/…).
const NAMED_COLORSCALES = {
    Turbo: [
        [0.000,"#30123b"],[0.125,"#4453d6"],[0.250,"#3ea6f8"],
        [0.375,"#23dde0"],[0.500,"#46f08c"],[0.625,"#aafa3f"],
        [0.750,"#f6c12f"],[0.875,"#ee5b1a"],[1.000,"#7a0402"]
    ],
    Plasma: [
        [0.000,"#0d0887"],[0.125,"#41049d"],[0.250,"#6a00a8"],
        [0.375,"#8f0da4"],[0.500,"#b12a90"],[0.625,"#cc4778"],
        [0.750,"#e16462"],[0.875,"#f1834b"],[1.000,"#f0f921"]
    ],
    Magma: [
        [0.000,"#000004"],[0.125,"#180f3d"],[0.250,"#440f76"],
        [0.375,"#721f81"],[0.500,"#9e2f7f"],[0.625,"#cd4071"],
        [0.750,"#f1605d"],[0.875,"#fd9668"],[1.000,"#fcfdbf"]
    ],
    Inferno: [
        [0.000,"#000004"],[0.125,"#1b0c41"],[0.250,"#4a0c6b"],
        [0.375,"#781c6d"],[0.500,"#a52c60"],[0.625,"#cf4446"],
        [0.750,"#ed6925"],[0.875,"#fb9b06"],[1.000,"#fcffa4"]
    ],
    Viridis:"Viridis", Cividis:"Cividis", Jet:"Jet", Hot:"Hot",
    Greys:"Greys", Electric:"Electric", YlOrRd:"YlOrRd", YlGnBu:"YlGnBu",
    RdBu:"RdBu", Bluered:"Bluered", Portland:"Portland",
    Earth:"Earth", Picnic:"Picnic", Rainbow:"Rainbow"
};
function resolveColorscale(name) { return NAMED_COLORSCALES[name] || name; }

function render(data) {
    resetLabelFits();
    const sp = data.spatial;
    if (!sp || !sp.tech_labels || sp.tech_labels.length === 0) {
        showStatus(_t("web.charts.mga_spatial.no_data"), false); return;
    }
    const cmap = data.colormap || "YlOrRd";
    const trace = {
        type: "heatmap",
        z: sp.std_mw, x: sp.node_labels, y: sp.tech_labels,
        colorscale: resolveColorscale(cmap),
        colorbar: { title: { text: _t("web.charts.mga_spatial.colorbar"), side: "right" },
                    thickness: 12, len: 0.9 },
        hovertemplate: _t("web.charts.mga_spatial.hover"),
        customdata: sp.mean_mw
    };
    const layout = {
        margin: { l: 10, r: 30, t: 50, b: 10 },
        title: { text: "<span style='font-size:11px;color:#7f8c8d'>" +
                       _t("web.charts.mga_spatial.subtitle") + "</span>",
                 x: 0.5, xanchor: "center", y: 0.98, yanchor: "top", font: { size: 13 } },
        xaxis: labelAxis({ axis: "xaxis", labels: sp.node_labels, fontSize: 11,
                           fraction: 0.25, maxChars: 24, angle: -40 }),
        yaxis: labelAxis({ axis: "yaxis", labels: sp.tech_labels, fontSize: 11,
                           fraction: 0.25 })
    };
    Plotly.purge("plot");
    Plotly.newPlot("plot", [trace], layout, { responsive: true, displaylogo: false }).then(refitLabels);
}

// Python params widget calls this when the colormap combo changes.
function setColormap(name) {
    const el = document.getElementById("plot");
    if (el && el.data && el.data.length) {
        Plotly.restyle("plot",
            { colorscale: [resolveColorscale(name)], reversescale: [false] },
            [0]);
    }
}

window.addEventListener("resize", () => {
    const el = document.getElementById("plot");
    if (el && el.data) Plotly.Plots.resize(el);
});
