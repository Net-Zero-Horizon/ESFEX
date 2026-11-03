/* Shared helpers for charts whose category labels are user-supplied
 * names (nodes, units, technologies) of unbounded length.
 *
 * The chart keeps the full name as the category value (so hover shows
 * it) and displays a shortened `ticktext`; the axis uses
 * `automargin: true` so Plotly sizes the margin to the shortened text
 * instead of clipping it at a fixed pixel margin. Legend entries get
 * the same treatment through the trace `name`.
 *
 * Fitting uses the real geometry once the chart is drawn (axis length
 * in px, plot-area size) and the current font scale from the Results
 * Viewer slider, and is redone on resize and on every font-scale
 * change. Charts call `resetLabelFits()` at the start of each render
 * and `refitLabels()` once `Plotly.newPlot` resolves.
 */
"use strict";

const _CHAR_W = 0.6;   // average glyph width, in em
const _LINE_H = 1.35;  // line height needed between labels, in em

// Middle ellipsis: keeps the start and the end of the name, so suffixes
// such as "@ n12" stay visible.
window.shortenLabel = function (s, maxChars) {
    s = String(s);
    if (maxChars < 4 || s.length <= maxChars) return s;
    const keep = maxChars - 1;
    const head = Math.ceil(keep * 0.6);
    return s.slice(0, head) + "…" + s.slice(s.length - (keep - head));
};

let _labelFits = [];
let _fontScale = 1;

window.resetLabelFits = function () { _labelFits = []; };

function _plotSize() {
    const plot = document.getElementById("plot");
    const sz = plot && plot._fullLayout && plot._fullLayout._size;
    if (sz && sz.w > 0 && sz.h > 0) return { w: sz.w, h: sz.h };
    return { w: Math.max(200, (window.innerWidth || 800) - 150),
             h: Math.max(150, (window.innerHeight || 600) - 140) };
}

function _axisLength(spec, size) {
    const plot = document.getElementById("plot");
    const ax = plot && plot._fullLayout && plot._fullLayout[spec.axis];
    if (ax && ax._length > 0) return ax._length;
    const dom = spec.domain || [0, 1];
    const full = spec.axis.startsWith("x") ? size.w : size.h;
    return full * (dom[1] - dom[0]);
}

// Characters allowed per label: share of the window width
// (`fraction`), a hard `maxChars`, and — for rotated x labels — the
// room below the axis (`depth(size)` in px).
function _charBudget(spec, size) {
    const fs = (spec.fontSize || 11) * _fontScale;
    const charW = fs * _CHAR_W;
    let n = ((window.innerWidth || 800) * (spec.fraction || 0.3)
             - (spec.reservePx || 0)) / charW;
    if (spec.maxChars) n = Math.min(n, spec.maxChars);
    if (spec.depth) {
        const a = Math.abs(spec.angle || 0) * Math.PI / 180;
        const room = spec.depth(size) - fs * _LINE_H * Math.cos(a);
        const perChar = charW * Math.max(Math.sin(a), 0.2);
        n = Math.min(n, room / perChar);
    }
    return Math.max(6, Math.floor(n));
}

function _axisTicks(spec) {
    const size = _plotSize();
    const fs = (spec.fontSize || 11) * _fontScale;
    const nChars = _charBudget(spec, size);
    // Distance along the axis that one label needs so its neighbours
    // don't overlap: a text line for y labels and rotated x labels
    // (divided by sin of the angle), the full text width for flat x.
    let spacing;
    if (spec.axis.startsWith("y")) {
        spacing = fs * _LINE_H;
    } else {
        const a = Math.abs(spec.angle || 0) * Math.PI / 180;
        spacing = (a > 0.05)
            ? fs * _LINE_H / Math.sin(a)
            : fs * _CHAR_W * nChars + 8;
    }
    const limit = Math.max(1, Math.floor(_axisLength(spec, size) / spacing));
    // Thin the ticks when there are more categories than fit; every
    // category stays on the axis, only its tick label is skipped.
    const step = Math.max(1, Math.ceil(spec.labels.length / limit));
    const vals = spec.labels.filter((_, i) => i % step === 0);
    return { tickvals: vals, ticktext: vals.map(s => shortenLabel(s, nChars)) };
}

/* Axis properties for a category axis of long names. Merge into the
 * axis layout: `yaxis: Object.assign({...}, labelAxis({...}))`.
 * spec: { axis: "yaxis" | "xaxis2" | …, labels, fontSize,
 *         fraction?   share of the window width a label may use,
 *         maxChars?   hard cap,
 *         angle?      tickangle in degrees (x axes),
 *         depth?      (plotSize) => px of room below a rotated x axis,
 *         domain?     axis domain, for the estimate before first draw } */
window.labelAxis = function (spec) {
    _labelFits.push(Object.assign({ kind: "axis" }, spec));
    const t = _axisTicks(spec);
    const out = {
        tickmode: "array",
        tickvals: t.tickvals,
        ticktext: t.ticktext,
        tickfont: { size: spec.fontSize || 11 },
        automargin: true,
    };
    if (spec.angle != null) out.tickangle = spec.angle;
    return out;
};

/* Shortened legend names for the given labels; registers the traces
 * (by index) whose `name` must follow the window size.
 * spec: { traces: [i, …], labels: [full name per trace], fraction,
 *         fontSize, reservePx?, maxChars? } */
window.legendNames = function (spec) {
    _labelFits.push(Object.assign({ kind: "legend" }, spec));
    const n = _charBudget(spec, _plotSize());
    return spec.labels.map(s => shortenLabel(s, n));
};

function _sameArray(a, b) {
    if (!a || !b || a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
    return true;
}

// Recompute every registered fit against the current geometry and
// font scale; only touches Plotly when something actually changed.
window.refitLabels = function () {
    const plot = document.getElementById("plot");
    if (!plot || !plot.data || !_labelFits.length) return;
    const relayout = {};
    for (const spec of _labelFits) {
        if (spec.kind === "axis") {
            const t = _axisTicks(spec);
            const cur = plot.layout[spec.axis] || {};
            if (!_sameArray(cur.ticktext, t.ticktext) || !_sameArray(cur.tickvals, t.tickvals)) {
                relayout[spec.axis + ".tickvals"] = t.tickvals;
                relayout[spec.axis + ".ticktext"] = t.ticktext;
            }
        } else {
            const n = _charBudget(spec, _plotSize());
            const names = spec.labels.map(s => shortenLabel(s, n));
            const cur = spec.traces.map(i => plot.data[i] && plot.data[i].name);
            if (!_sameArray(cur, names)) Plotly.restyle(plot, { name: names }, spec.traces);
        }
    }
    if (Object.keys(relayout).length) Plotly.relayout(plot, relayout);
};

window.addEventListener("resize", () => {
    // Let the chart's own resize handler settle the new axis lengths.
    setTimeout(window.refitLabels, 60);
});

// Follow the Results Viewer font slider: font_scale.js (loaded before
// this file) rescales the fonts; the label fit then uses the new size.
(function () {
    const orig = window.setFontScale;
    window.setFontScale = function (scale) {
        if (orig) orig(scale);
        if (typeof scale === "number" && isFinite(scale) && scale > 0) _fontScale = scale;
        setTimeout(window.refitLabels, 30);
    };
})();
