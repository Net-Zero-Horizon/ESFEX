/* Interruptions calendar — D3 Gantt timeline over the horizon year.
 * Rows = infrastructure elements (grouped by category); bars = scheduled
 * outages over [start_hour, end_hour). Fully offline (bundled d3). Talks to
 * Python via the QWebChannel `bridge` (get_data / commit). */
"use strict";

var LEFT = 172, ROW_H = 22, HDR_H = 18, TOP = 26, PAD_R = 16;
var baseMs = Date.UTC(2025, 0, 1), horizon = 8760, plotW = 900, contentH = 0;
var rows = [], rowOf = {}, schedule = [], selected = null, uidSeq = 1;
var x, svg, gBars, I = {};

function hourToDate(h) { return new Date(baseMs + h * 3600000); }
function pad(n) { return String(n).padStart(2, "0"); }
function hourToInput(h) {
    var d = hourToDate(h);
    return d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate()) +
           "T" + pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes());
}
function inputToHour(s) {
    var m = /(\d+)-(\d+)-(\d+)T(\d+):(\d+)/.exec(s || "");
    if (!m) return null;
    return Math.round((Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]) - baseMs) / 3600000);
}
function clampH(h) { return Math.max(0, Math.min(horizon, Math.round(h))); }
function keyOf(d) { return d.element_type + " " + d.element_id; }

// ── Bootstrap ──────────────────────────────────────────────────────
window.addEventListener("DOMContentLoaded", function () {
    if (typeof QWebChannel === "undefined" || typeof qt === "undefined") return;
    new QWebChannel(qt.webChannelTransport, function (channel) {
        channel.objects.bridge.get_data(function (payload) {
            boot(JSON.parse(payload));
        });
        window._bridge = channel.objects.bridge;
    });
});

function applyTheme(t) {
    // Mirror the active Studio palette onto the CSS variables the stylesheet
    // reads, so the timeline matches light/dark themes.
    var root = document.documentElement.style;
    var map = {
        bg: "--bg", bg2: "--bg2", elevated: "--elevated", text: "--text",
        text2: "--text2", border: "--border", accent: "--accent",
        danger: "--danger", warn: "--warn", selBg: "--sel-bg",
    };
    for (var k in map) if (t[k]) root.setProperty(map[k], t[k]);
    if (t.bg2) root.setProperty("--row-alt", t.bg2);
}

function boot(data) {
    baseMs = Date.UTC(data.base_year, 0, 1);
    horizon = data.horizon_hours || 8760;
    I = data.i18n || {};
    if (data.theme) applyTheme(data.theme);
    // Flatten groups → rows (a header row per category, then element rows).
    rows = [];
    (data.groups || []).forEach(function (g) {
        rows.push({ kind: "header", label: g.label });
        g.items.forEach(function (it) {
            var r = { kind: "el", category: g.category, id: it.id, label: it.label };
            rows.push(r);
            rowOf[g.category + " " + it.id] = r;
        });
    });
    schedule = (data.schedule || []).map(function (o) {
        var c = Object.assign({ _uid: uidSeq++ }, o); return c;
    });
    // Static labels
    document.getElementById("tb-title").textContent = document.title = I.title || "Interruptions";
    document.getElementById("tb-hint").textContent = I.empty || "";
    document.getElementById("side-placeholder").textContent = I.empty || "";
    document.getElementById("lbl-start").textContent = I.start || "Start";
    document.getElementById("lbl-end").textContent = I.end || "End";
    document.getElementById("lbl-avail").textContent = I.availability || "Availability";
    document.getElementById("lbl-label").textContent = I.label || "Note";
    document.getElementById("btn-delete").textContent = I.delete || "Delete";
    wireForm();
    render();
    if (data.focus) focusRow(data.focus.element_type, data.focus.element_id);
}

function focusRow(cat, id) {
    var r = rowOf[cat + " " + id];
    if (!r) return;
    var wrap = document.getElementById("timeline-wrap");
    wrap.scrollTop = Math.max(0, r._y - wrap.clientHeight / 2);
    // Brief highlight so the user sees which row to schedule.
    svg.append("rect").attr("x", 0).attr("y", r._y).attr("width", LEFT + plotW)
        .attr("height", ROW_H).style("fill", "var(--sel-bg)")
        .attr("pointer-events", "none")
        .transition().duration(1800).style("opacity", 0).remove();
    // Preselect an existing outage on that element, if any.
    var existing = schedule.find(function (d) {
        return d.element_type === cat && d.element_id === id;
    });
    if (existing) { selected = existing; renderBars(); fillForm(); }
}

// ── Rendering ──────────────────────────────────────────────────────
function render() {
    var wrap = document.getElementById("timeline-wrap");
    plotW = Math.max(700, wrap.clientWidth - LEFT - PAD_R);
    contentH = TOP + rows.reduce(function (a, r) { return a + (r.kind === "header" ? HDR_H : ROW_H); }, 0) + 8;
    x = d3.scaleLinear().domain([0, horizon]).range([LEFT, LEFT + plotW]);

    d3.select("#timeline").selectAll("*").remove();
    svg = d3.select("#timeline")
        .attr("width", LEFT + plotW + PAD_R)
        .attr("height", contentH);

    // Month grid + axis labels
    var gGrid = svg.append("g");
    for (var mo = 0; mo <= 12; mo++) {
        var h = Math.round((Date.UTC(new Date(baseMs).getUTCFullYear(), mo, 1) - baseMs) / 3600000);
        if (h < 0 || h > horizon) continue;
        gGrid.append("line").attr("class", "grid-line month")
            .attr("x1", x(h)).attr("x2", x(h)).attr("y1", TOP).attr("y2", contentH - 8);
        if (mo < 12) {
            var mid = hourToDate(h);
            gGrid.append("text").attr("class", "axis-label")
                .attr("x", x(h) + 4).attr("y", 16)
                .text(mid.toLocaleString("default", { month: "short", timeZone: "UTC" }));
        }
    }

    // Rows (backgrounds, labels, create-hit areas)
    var y = TOP, elIdx = 0;
    var gRows = svg.append("g");
    rows.forEach(function (r) {
        if (r.kind === "header") {
            gRows.append("line").attr("class", "group-sep")
                .attr("x1", 0).attr("x2", LEFT + plotW).attr("y1", y).attr("y2", y);
            gRows.append("text").attr("class", "group-label")
                .attr("x", 8).attr("y", y + 13).text(r.label);
            r._y = y; y += HDR_H;
        } else {
            gRows.append("rect")
                .attr("class", "row-bg" + ((elIdx++ % 2) ? " alt" : ""))
                .attr("x", 0).attr("y", y).attr("width", LEFT + plotW).attr("height", ROW_H);
            gRows.append("text").attr("class", "row-label")
                .attr("x", 10).attr("y", y + ROW_H / 2 + 4)
                .text(r.label.length > 24 ? r.label.slice(0, 23) + "…" : r.label);
            r._y = y;
            addCreateHit(gRows, r, y);
            y += ROW_H;
        }
    });

    gBars = svg.append("g");
    renderBars();
}

function addCreateHit(g, r, y) {
    var start = null, rect = null;
    g.append("rect").attr("class", "row-hit")
        .attr("x", LEFT).attr("y", y + 2).attr("width", plotW).attr("height", ROW_H - 4)
        .call(d3.drag()
            .on("start", function (ev) {
                start = clampH(x.invert(ev.x));
                rect = gBars.append("rect").attr("class", "outage")
                    .attr("y", y + 3).attr("height", ROW_H - 6);
            })
            .on("drag", function (ev) {
                var cur = clampH(x.invert(ev.x));
                var a = Math.min(start, cur), b = Math.max(start, cur);
                rect.attr("x", x(a)).attr("width", Math.max(1, x(b) - x(a)));
            })
            .on("end", function (ev) {
                if (rect) rect.remove();
                var cur = clampH(x.invert(ev.x));
                var a = Math.min(start, cur), b = Math.max(start, cur);
                if (b - a < 1) b = Math.min(horizon, a + 24);  // default 1-day click
                var item = {
                    _uid: uidSeq++, element_type: r.category, element_id: r.id,
                    start_hour: a, end_hour: b, availability: 0.0, label: "",
                };
                schedule.push(item);
                selected = item;
                commit(); renderBars(); fillForm();
            }));
}

function renderBars() {
    var sel = gBars.selectAll("g.bar").data(
        schedule.filter(function (d) { return rowOf[keyOf(d)]; }),
        function (d) { return d._uid; });
    sel.exit().remove();
    var enter = sel.enter().append("g").attr("class", "bar");
    enter.append("rect").attr("class", "outage");
    enter.append("text").attr("class", "outage-label");
    enter.append("rect").attr("class", "handle handle-l");
    enter.append("rect").attr("class", "handle handle-r");

    var all = enter.merge(sel);
    all.each(function (d) {
        var r = rowOf[keyOf(d)], gEl = d3.select(this);
        var x0 = x(d.start_hour), w = Math.max(2, x(d.end_hour) - x(d.start_hour));
        gEl.select(".outage")
            .attr("x", x0).attr("y", r._y + 3).attr("width", w).attr("height", ROW_H - 6)
            .attr("class", "outage" + (d.availability > 0 ? " derate" : "") + (d === selected ? " selected" : ""))
            .call(d3.drag().on("drag", function (ev) { moveBar(d, ev.dx); }))
            .on("click", function (ev) { ev.stopPropagation(); selected = d; renderBars(); fillForm(); });
        gEl.select(".outage-label")
            .attr("x", x0 + 4).attr("y", r._y + ROW_H / 2 + 3)
            .text(w > 34 ? (d.availability > 0 ? Math.round(d.availability * 100) + "%" : "") : "");
        gEl.select(".handle-l").attr("x", x0 - 2).attr("y", r._y + 3).attr("width", 5).attr("height", ROW_H - 6)
            .call(d3.drag().on("drag", function (ev) { resizeBar(d, "l", ev.x); }));
        gEl.select(".handle-r").attr("x", x0 + w - 3).attr("y", r._y + 3).attr("width", 5).attr("height", ROW_H - 6)
            .call(d3.drag().on("drag", function (ev) { resizeBar(d, "r", ev.x); }));
    });
}

function moveBar(d, dxPx) {
    var dur = d.end_hour - d.start_hour;
    var dh = Math.round(dxPx / (plotW / horizon));
    var ns = clampH(d.start_hour + dh);
    if (ns + dur > horizon) ns = horizon - dur;
    d.start_hour = ns; d.end_hour = ns + dur;
    if (d === selected) fillForm();
    renderBars(); commitDeferred();
}
function resizeBar(d, side, px) {
    var h = clampH(x.invert(px));
    if (side === "l") d.start_hour = Math.min(h, d.end_hour - 1);
    else d.end_hour = Math.max(h, d.start_hour + 1);
    if (d === selected) fillForm();
    renderBars(); commitDeferred();
}

// ── Side form ──────────────────────────────────────────────────────
function wireForm() {
    document.getElementById("timeline-wrap").addEventListener("click", function () {
        selected = null; renderBars(); fillForm();
    });
    document.getElementById("in-start").addEventListener("change", function () {
        if (!selected) return; var h = inputToHour(this.value);
        if (h !== null) { selected.start_hour = clampH(Math.min(h, selected.end_hour - 1)); renderBars(); commit(); }
    });
    document.getElementById("in-end").addEventListener("change", function () {
        if (!selected) return; var h = inputToHour(this.value);
        if (h !== null) { selected.end_hour = clampH(Math.max(h, selected.start_hour + 1)); renderBars(); commit(); }
    });
    document.getElementById("in-avail").addEventListener("input", function () {
        if (!selected) return; selected.availability = (+this.value) / 100;
        document.getElementById("avail-val").textContent = this.value + "%";
        renderBars(); commitDeferred();
    });
    document.getElementById("in-label").addEventListener("input", function () {
        if (selected) { selected.label = this.value; commitDeferred(); }
    });
    document.getElementById("btn-delete").addEventListener("click", function () {
        if (!selected) return;
        schedule = schedule.filter(function (o) { return o !== selected; });
        selected = null; renderBars(); fillForm(); commit();
    });
}

function fillForm() {
    var side = document.getElementById("side");
    if (!selected) { side.classList.add("empty"); return; }
    side.classList.remove("empty");
    var r = rowOf[keyOf(selected)];
    document.getElementById("sel-element").textContent = r ? r.label : selected.element_id;
    document.getElementById("in-start").value = hourToInput(selected.start_hour);
    document.getElementById("in-end").value = hourToInput(selected.end_hour);
    var pct = Math.round((selected.availability || 0) * 100);
    document.getElementById("in-avail").value = pct;
    document.getElementById("avail-val").textContent = pct + "%";
    document.getElementById("in-label").value = selected.label || "";
}

// ── Commit to Python ───────────────────────────────────────────────
var _commitTimer = null;
function commitDeferred() {
    if (_commitTimer) clearTimeout(_commitTimer);
    _commitTimer = setTimeout(commit, 200);
}
function commit() {
    if (_commitTimer) { clearTimeout(_commitTimer); _commitTimer = null; }
    if (!window._bridge) return;
    var out = schedule.map(function (d) {
        return {
            element_type: d.element_type, element_id: d.element_id,
            start_hour: d.start_hour, end_hour: d.end_hour,
            availability: d.availability, label: d.label || "",
        };
    });
    window._bridge.commit(JSON.stringify(out));
}

window.addEventListener("resize", function () { if (rows.length) render(); });
