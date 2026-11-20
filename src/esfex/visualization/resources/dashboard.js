/* ESFEX Results Dashboard — Plotly frontend.
 *
 * State machine:
 *   QWebChannel handshake → bootstrap() → loader.get_meta()
 *      → populates system <select>, year range bounds, then refresh()
 *   refresh() → loader.get_overview(state)
 *      → updates KPI cards, repaints trajectory, repaints mix
 *
 * Crossfilter (year-range brush):
 *   Plotly emits `plotly_relayout` whenever the user drags a brush on
 *   the trajectory subplot. We extract the new xaxis range, clamp it
 *   to the available years, and re-fetch the overview with that range.
 *   The KPI cards and the mix chart then reflect the brushed window.
 *
 * Async model:
 *   Each loader method returns a JSON string (QWebChannel's safest
 *   wire format for nested dicts). We JSON.parse on receipt. All
 *   awaits go through a small `call` helper that promises-ifies the
 *   QWebChannel callback API.
 */
"use strict";

let bridge = null;
let meta = null;                  // {systems, years, system_default}
let theme = null;                 // {bg, text, accent, ...} from get_theme()
const state = {                   // current filter selection
    system: null,
    yearRange: null,              // [min, max] or null for all
};
// Last data drawn, redrawn in the new language by onEsfexLanguageChanged.
let lastOverview = null;          // {payload, mode}
let lastDetail = null;            // {year, detail} of the open year panel
let lastStatus = { key: "", params: null };

// ─────────────────────────────────────────────────────────────────
// QWebChannel bootstrap
// ─────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
    setStatus("web.dashboard.connecting");
    new QWebChannel(qt.webChannelTransport, channel => {
        bridge = channel.objects.loader;
        bootstrap().catch(err => {
            console.error("bootstrap failed", err);
            setStatus("web.dashboard.connection_error", { error: (err && err.message || err) });
        });
    });
});

async function bootstrap() {
    // Theme first so the page repaints with the GUI's palette before
    // any data lands — avoids a flash of default-light styling on a
    // dark theme.
    try {
        theme = JSON.parse(await call(bridge.get_theme));
        applyTheme(theme);
    } catch (err) {
        console.warn("get_theme failed, using fallback CSS defaults", err);
    }

    meta = JSON.parse(await call(bridge.get_meta));
    if (!meta || !meta.systems || meta.systems.length === 0) {
        setStatus("web.dashboard.no_results");
        return;
    }

    // System combo
    const sel = document.getElementById("system-select");
    sel.innerHTML = "";
    for (const s of meta.systems) {
        const opt = document.createElement("option");
        opt.value = s; opt.textContent = s;
        sel.appendChild(opt);
    }
    sel.value = meta.system_default || meta.systems[0];
    sel.addEventListener("change", () => {
        state.system = sel.value;
        state.yearRange = null;   // clear brush when system changes
        closeYearDetail();        // stale: detail was for the old system
        refresh();
    });
    state.system = sel.value;

    // Allow the host dialog to drive the system selection from its
    // sidebar combo. The host calls ``setActiveSystem(name)`` after
    // ``_on_system_changed`` to keep this view in sync; without it the
    // dashboard would keep rendering the previously-selected system
    // regardless of what the sidebar shows.
    window.setActiveSystem = function(name) {
        if (!name) return;
        // Update the (hidden) inner combo so it stays in sync, but
        // don't gate the state change on the option existing — the
        // host carries the canonical list and may know about systems
        // the bootstrap meta hasn't been refreshed for yet.
        const opt = Array.from(sel.options).find(o => o.value === name);
        if (opt) sel.value = name;
        const changed = state.system !== name;
        state.system = name;
        // Clear the brush — the previous system's range is meaningless
        // for the new one.
        state.yearRange = null;
        closeYearDetail();
        // Always refresh, even if name === state.system: the host may
        // be re-asserting the same system after a results-file reload
        // and the cards need to repaint regardless.
        refresh();
        return changed;
    };

    // Allow the host dialog to drive the year filter from the
    // top-toolbar slider. ``range`` is ``null`` (clear) or
    // ``[yMin, yMax]``.
    window.setYearRange = function(range) {
        if (range === null || range === undefined) {
            if (state.yearRange === null) return;
            state.yearRange = null;
        } else {
            const lo = Number(range[0]);
            const hi = Number(range[1]);
            if (!Number.isFinite(lo) || !Number.isFinite(hi)) return;
            if (state.yearRange
                && state.yearRange[0] === lo
                && state.yearRange[1] === hi) return;
            state.yearRange = [lo, hi];
        }
        closeYearDetail();
        refresh();
    };

    // Year range starts unfiltered (= full range).
    state.yearRange = null;
    updateRangeDisplay();

    // Reset button
    document.getElementById("reset-range").addEventListener("click", () => {
        state.yearRange = null;
        updateRangeDisplay();
        refresh();
        // Also tell Plotly to reset its zoom so the brush visualisation
        // matches the data state.
        Plotly.relayout("trajectory", {
            "xaxis.autorange": true,
        });
    });

    // Year drill-down close button
    const closeBtn = document.getElementById("year-detail-close");
    if (closeBtn) {
        closeBtn.addEventListener("click", closeYearDetail);
    }

    await refresh();
    setStatus("");
}

// ─────────────────────────────────────────────────────────────────
// Data refresh
// ─────────────────────────────────────────────────────────────────

async function refresh() {
    if (!bridge || !state.system) return;
    setStatus("web.common.loading");
    try {
        const payload = JSON.parse(await call(
            bridge.get_overview, JSON.stringify(state)
        ));
        const mode = payload.mode || (meta && meta.simulation_mode === "unit_commitment" ? "uc" : "planning");
        lastOverview = { payload: payload, mode: mode };
        renderOverview(payload, mode);
        if (mode === "uc") {
            // Year-detail drill-down only makes sense for multi-year
            // runs — hide it in UC.
            closeYearDetail();
        }
        setStatus("");
    } catch (err) {
        console.error("refresh failed", err);
        setStatus("web.dashboard.refresh_error", { error: (err && err.message || err) });
    }
}

function renderOverview(payload, mode) {
    // UC and planning share the same DOM slots but render entirely
    // different content. Re-label the KPI cards and rebuild the
    // trajectory + mix panels each time so a system switch between
    // UC and planning swaps cleanly.
    applyKpiSchema(mode);
    renderKpis(payload.kpis || {});
    if (mode === "uc") {
        renderHourlyLmp(payload.trajectory || null);
        renderHourlyDispatch(payload.mix || null);
    } else {
        renderTrajectory(payload.trajectory || null);
        renderMix(payload.mix || null);
    }
    renderCost(payload.cost || null);
}

// Language changed (web_i18n): the static labels are refilled by
// _applyI18n; the texts drawn here are redrawn from the last data at
// once, then the data is fetched again because Python builds some of its
// labels (series, cost components) in the language active at the time.
window.onEsfexLanguageChanged = async function () {
    setStatus(lastStatus.key, lastStatus.params);
    updateRangeDisplay();
    if (lastOverview) renderOverview(lastOverview.payload, lastOverview.mode);
    const detail = lastDetail;
    if (detail) {
        setYearDetailTitle(detail.year);
        renderYearKpis(detail.detail.kpis || {});
        renderYearDispatch(detail.detail.dispatch || null);
    }
    if (lastOverview) await refresh();
    if (detail && lastDetail === detail) await reloadYearDetail(detail.year);
};

// Fetch the open year panel again, in place (no scrolling).
async function reloadYearDetail(year) {
    if (!bridge || !state.system) return;
    try {
        const detail = JSON.parse(await call(
            bridge.get_year_detail, state.system, year
        ));
        lastDetail = { year: year, detail: detail };
        renderYearKpis(detail.kpis || {});
        renderYearDispatch(detail.dispatch || null);
    } catch (err) {
        console.error("year detail reload failed", err);
    }
}

// ─────────────────────────────────────────────────────────────────
// KPI schema — relabels cards in-place depending on the run mode.
// Planning uses the original {cost, re_share, co2, load_shed,
// investment}; UC swaps to {cost, avg_lmp, ens, re_share, battery_
// cycles}. Card data-key attrs are mutated so renderKpis can lookup
// against the right payload keys without a fork.
// ─────────────────────────────────────────────────────────────────

// ``label`` is the web.* key of the card's title.
const _UC_KPI_SCHEMA = [
    { key: "cost",            label: "web.dashboard.kpi_system_cost" },
    { key: "avg_lmp",         label: "web.dashboard.kpi_avg_lmp" },
    { key: "ens",             label: "web.dashboard.kpi_unserved_energy" },
    { key: "re_share",        label: "web.dashboard.kpi_re_share" },
    { key: "battery_cycles",  label: "web.dashboard.kpi_battery_cycles" },
];
const _PLANNING_KPI_SCHEMA = [
    { key: "cost",       label: "web.dashboard.kpi_total_cost" },
    { key: "re_share",   label: "web.dashboard.kpi_re_share" },
    { key: "co2",        label: "web.dashboard.kpi_co2" },
    { key: "load_shed",  label: "web.dashboard.kpi_load_shed" },
    { key: "investment", label: "web.dashboard.kpi_new_capacity" },
];

// Set an element's text from a web.* key, and mark it so a language
// change refills it.
function setKeyText(el, key) {
    if (!el) return;
    el.setAttribute("data-i18n", key);
    el.textContent = _t(key);
}

function setHeader(hdr, titleKey, hintKey) {
    if (!hdr) return;
    setKeyText(hdr.querySelector(".card-title"), titleKey);
    setKeyText(hdr.querySelector(".hint"), hintKey);
}

function applyKpiSchema(mode) {
    const schema = (mode === "uc") ? _UC_KPI_SCHEMA : _PLANNING_KPI_SCHEMA;
    const cards = document.querySelectorAll(".kpi");
    schema.forEach((spec, i) => {
        const card = cards[i];
        if (!card) return;
        card.dataset.key = spec.key;
        setKeyText(card.querySelector(".label"), spec.label);
    });
    // Update the section headers too — the planning labels would be
    // misleading in UC mode.
    const trajHdr = document.querySelector(
        "#trajectory"
    )?.previousElementSibling;
    const mixHdr = document.querySelector("#mix")?.previousElementSibling;
    const costHint = document.getElementById("cost-hint");
    if (mode === "uc") {
        setHeader(trajHdr, "web.dashboard.lmp_title", "web.dashboard.lmp_hint");
        setHeader(mixHdr, "web.dashboard.dispatch_title", "web.dashboard.dispatch_hint");
        setKeyText(costHint, "web.dashboard.cost_hint_uc");
    } else {
        setHeader(trajHdr, "web.dashboard.trajectory_title", "web.dashboard.trajectory_hint");
        setHeader(mixHdr, "web.dashboard.mix_title", "web.dashboard.mix_hint");
        setKeyText(costHint, "web.dashboard.cost_hint");
    }
}

// ─────────────────────────────────────────────────────────────────
// KPI cards
// ─────────────────────────────────────────────────────────────────

function renderKpis(kpis) {
    for (const card of document.querySelectorAll(".kpi")) {
        const key = card.dataset.key;
        const k = kpis[key];
        const valueEl = card.querySelector(".value");
        const deltaEl = card.querySelector(".delta");
        if (!k || k.value === null || k.value === undefined) {
            valueEl.textContent = "—";
            deltaEl.textContent = "";
            deltaEl.className = "delta";
            continue;
        }
        valueEl.textContent = k.value;
        if (k.delta && k.delta.text) {
            deltaEl.textContent = k.delta.text;
            deltaEl.className = "delta " + (k.delta.direction || "flat");
        } else {
            deltaEl.textContent = "";
            deltaEl.className = "delta";
        }
    }
}

// ─────────────────────────────────────────────────────────────────
// Trajectory chart (multi-axis + brushable)
// ─────────────────────────────────────────────────────────────────

function renderTrajectory(data) {
    const div = document.getElementById("trajectory");
    if (!data || !data.years || data.years.length === 0) {
        showEmpty(div, "web.dashboard.no_trajectory");
        return;
    }

    const traces = [
        {
            x: data.years, y: data.cost_musd,
            type: "scatter", mode: "lines+markers",
            name: _t("web.dashboard.trace_total_cost"),
            line: { color: "#f4b942", width: 2 },
            yaxis: "y",
        },
        {
            x: data.years, y: data.re_pct,
            type: "scatter", mode: "lines+markers",
            name: _t("web.dashboard.trace_re_share"),
            line: { color: "#2ecc71", width: 2 },
            yaxis: "y2",
        },
        {
            x: data.years, y: data.co2_mt,
            type: "scatter", mode: "lines+markers",
            name: _t("web.dashboard.co2_mt"),
            line: { color: "#e74c3c", width: 2 },
            yaxis: "y3",
        },
    ];

    const layout = themedLayout({
        margin: { t: 30, r: 60, b: 40, l: 60 },
        xaxis: {
            title: _t("web.dashboard.axis_year"),
            // ``range`` lets us reflect the brushed window into the
            // chart's own visualisation. When state.yearRange is null
            // we let plotly auto-range.
            range: state.yearRange ? [state.yearRange[0], state.yearRange[1]] : undefined,
        },
        yaxis:  { title: _t("web.dashboard.axis_cost_musd"), side: "left",  color: "#f4b942" },
        yaxis2: { title: _t("web.dashboard.axis_re_pct"), side: "right", overlaying: "y", color: "#2ecc71" },
        yaxis3: {
            title: _t("web.dashboard.co2_mt"),
            side: "right", overlaying: "y", anchor: "free",
            position: 0.96, color: "#e74c3c", showgrid: false,
        },
        legend: { orientation: "h", x: 0, y: 1.1 },
    });

    Plotly.react("trajectory", traces, layout, {
        responsive: true,
        displaylogo: false,
        modeBarButtonsToRemove: ["lasso2d", "select2d"],
    });

    // Wire the brush + click handlers exactly once per element.
    // .on() stacks if called repeatedly, so a flag attribute guards.
    if (!div.dataset.handlersBound) {
        div.on("plotly_relayout", onTrajectoryRelayout);
        div.on("plotly_click", onTrajectoryClick);
        div.dataset.handlersBound = "1";
    }
}

// ─────────────────────────────────────────────────────────────────
// Drill-down: click a year point → detail panel
// ─────────────────────────────────────────────────────────────────

function onTrajectoryClick(evt) {
    // evt.points is an array of points under the cursor (one per
    // trace at that x). They all share the same x = year, so any
    // point gives us the year.
    if (!evt || !evt.points || evt.points.length === 0) return;
    const year = Math.round(Number(evt.points[0].x));
    if (!Number.isFinite(year)) return;
    openYearDetail(year);
}

async function openYearDetail(year) {
    if (!bridge || !state.system) return;
    const panel = document.getElementById("year-detail");
    setYearDetailTitle(year);
    lastDetail = null;
    panel.style.display = "";
    // Scroll the panel into view so the user sees the drill-down
    // result without hunting for it.
    panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    setStatus("web.dashboard.loading_year", { year: year });

    try {
        const detail = JSON.parse(await call(
            bridge.get_year_detail, state.system, year
        ));
        lastDetail = { year: year, detail: detail };
        renderYearKpis(detail.kpis || {});
        renderYearDispatch(detail.dispatch || null);
        setStatus("");
    } catch (err) {
        console.error("year detail failed", err);
        setStatus("web.dashboard.year_detail_error", { error: (err && err.message || err) });
    }
}

function setYearDetailTitle(year) {
    const el = document.getElementById("year-detail-title");
    if (el) el.textContent = _t("web.dashboard.year_detail_title", { year: year });
}

function renderYearKpis(kpis) {
    const host = document.getElementById("year-detail-kpis");
    host.innerHTML = "";
    // Same five metrics as the top KPI bar, rendered as compact chips.
    const order = [
        ["cost", _t("web.dashboard.chip_cost")],
        ["re_share", _t("web.dashboard.chip_re")],
        ["co2", _t("web.dashboard.chip_co2")],
        ["load_shed", _t("web.dashboard.chip_load_shed")],
        ["investment", _t("web.dashboard.chip_new_cap")],
    ];
    for (const [key, title] of order) {
        const k = kpis[key];
        const chip = document.createElement("div");
        chip.className = "mini-kpi";
        const label = document.createElement("div");
        label.className = "label";
        label.textContent = title;
        const value = document.createElement("div");
        value.className = "value";
        value.textContent = (k && k.value != null) ? k.value : "—";
        chip.appendChild(label);
        chip.appendChild(value);
        host.appendChild(chip);
    }
}

function renderYearDispatch(dispatch) {
    const div = document.getElementById("year-detail-dispatch");
    if (!dispatch || !dispatch.hours || dispatch.hours.length === 0
        || !dispatch.series || dispatch.series.length === 0) {
        showEmpty(div, "web.dashboard.no_year_dispatch");
        return;
    }
    const traces = dispatch.series.map(s => ({
        x: dispatch.hours,
        y: s.values,
        type: "scatter",
        mode: "none",
        stackgroup: "dispatch",
        name: s.label,
        fillcolor: s.color || undefined,
        hovertemplate: _t("web.dashboard.hover_h_mw") + "<extra>%{fullData.name}</extra>",
    }));
    const layout = themedLayout({
        margin: { t: 20, r: 30, b: 40, l: 70 },
        xaxis: { title: _t("web.dashboard.axis_hour_of_year") },
        yaxis: { title: _t("web.dashboard.axis_dispatch_mw") },
        legend: { orientation: "v", x: 1.02, y: 1 },
        showlegend: true,
    });
    Plotly.react("year-detail-dispatch", traces, layout, {
        responsive: true,
        displaylogo: false,
        modeBarButtonsToRemove: ["lasso2d", "select2d"],
    });
}

function closeYearDetail() {
    const panel = document.getElementById("year-detail");
    panel.style.display = "none";
    lastDetail = null;
    // Purge the embedded plot so a stale figure doesn't flash on the
    // next open before fresh data arrives.
    const div = document.getElementById("year-detail-dispatch");
    if (div) Plotly.purge(div);
}

function onTrajectoryRelayout(evt) {
    // Plotly emits relayout for many things (autorange, dragmode, etc).
    // Only react when the user actually changed the X range — either a
    // manual brush ("xaxis.range[0]" / "xaxis.range[1]") or a reset
    // back to autorange.
    const r0 = evt["xaxis.range[0]"];
    const r1 = evt["xaxis.range[1]"];
    if (r0 === undefined && r1 === undefined) {
        // autorange or unrelated event — only matter when explicitly reset
        if (evt["xaxis.autorange"] === true && state.yearRange !== null) {
            state.yearRange = null;
            updateRangeDisplay();
            refresh();
        }
        return;
    }

    // Clamp the floating-point range plotly gives us (the user can
    // brush mid-year) to integer years and to the available domain.
    const ymin = Math.max(meta.years[0], Math.ceil(Number(r0)));
    const ymax = Math.min(meta.years[meta.years.length - 1], Math.floor(Number(r1)));
    if (ymin >= ymax) return;  // degenerate brush, ignore
    state.yearRange = [ymin, ymax];
    updateRangeDisplay();
    refresh();
}

// ─────────────────────────────────────────────────────────────────
// Generation Mix (stacked area)
// ─────────────────────────────────────────────────────────────────

function renderMix(data) {
    const div = document.getElementById("mix");
    if (!data || !data.years || data.years.length === 0 || !data.series || data.series.length === 0) {
        showEmpty(div, "web.dashboard.no_generation");
        return;
    }

    const traces = data.series.map(s => ({
        x: data.years,
        y: s.values,
        type: "scatter",
        mode: "none",
        stackgroup: "gen",
        name: s.label,
        fillcolor: s.color || undefined,
        hovertemplate: "%{y:,.0f} GWh<extra>%{fullData.name}</extra>",
    }));

    const layout = themedLayout({
        margin: { t: 30, r: 30, b: 40, l: 70 },
        xaxis: { title: _t("web.dashboard.axis_year") },
        yaxis: { title: _t("web.dashboard.axis_generation_gwh") },
        legend: { orientation: "v", x: 1.02, y: 1 },
        showlegend: true,
    });

    Plotly.react("mix", traces, layout, {
        responsive: true,
        displaylogo: false,
        modeBarButtonsToRemove: ["lasso2d", "select2d"],
    });
}

// ─────────────────────────────────────────────────────────────────
// Cost Composition (waterfall)
// ─────────────────────────────────────────────────────────────────

function renderCost(data) {
    const div = document.getElementById("cost");
    const hint = document.getElementById("cost-hint");
    if (!data || !data.steps || data.steps.length === 0) {
        showEmpty(div, "web.dashboard.no_cost");
        setKeyText(hint, "web.dashboard.cost_hint");
        return;
    }
    if (hint) {
        const ys = data.years || [];
        hint.textContent = ys.length === 0 ? _t("web.dashboard.cost_hint_stack")
            : (ys.length === 1
               ? _t("web.dashboard.cost_hint_year", { year: ys[0] })
               : _t("web.dashboard.cost_hint_years", { first: ys[0], last: ys[ys.length - 1] }));
    }

    // Build the waterfall: one "relative" step per cost component,
    // then one "total" bar that plotly draws as an absolute sum.
    // The solver stores benefit-type components (e.g. flexible-demand
    // benefit) as positive numbers that *reduce*
    // the objective — we keep the raw sign here because the backend
    // already returns them with the correct sign for stacking.
    const labels   = data.steps.map(s => s.label);
    const values   = data.steps.map(s => s.value);
    const measures = data.steps.map(() => "relative");

    labels.push(_t("web.dashboard.cost_total"));
    values.push(data.total);
    measures.push("total");

    const trace = {
        type: "waterfall",
        orientation: "v",
        measure: measures,
        x: labels,
        y: values,
        // Compact $ text on each bar.
        text: values.map(fmtUsdShort),
        textposition: "outside",
        connector: { line: { color: (theme && theme.text_muted) || "#888" } },
        increasing: { marker: { color: (theme && theme.err)   || "#e74c3c" } },
        decreasing: { marker: { color: (theme && theme.ok)    || "#2ecc71" } },
        totals:     { marker: { color: (theme && theme.accent)|| "#2980b9" } },
        hovertemplate: "%{x}: %{y:$,.0f}<extra></extra>",
    };

    const layout = themedLayout({
        margin: { t: 20, r: 20, b: 110, l: 80 },
        xaxis: { title: "", tickangle: -40, automargin: true },
        yaxis: { title: _t("web.dashboard.axis_cost_usd") },
        showlegend: false,
    });

    Plotly.react("cost", [trace], layout, {
        responsive: true,
        displaylogo: false,
        modeBarButtonsToRemove: ["lasso2d", "select2d"],
    });
}

// Short USD formatter shared by the waterfall bar labels.
function fmtUsdShort(v) {
    const a = Math.abs(v);
    if (a >= 1e9) return "$" + (v / 1e9).toFixed(1) + "B";
    if (a >= 1e6) return "$" + (v / 1e6).toFixed(1) + "M";
    if (a >= 1e3) return "$" + (v / 1e3).toFixed(1) + "K";
    return "$" + v.toFixed(0);
}

// ─────────────────────────────────────────────────────────────────
// Utilities
// ─────────────────────────────────────────────────────────────────

function applyTheme(t) {
    // Map the colour dict onto the CSS custom properties the
    // stylesheet expects. Setting on documentElement means the
    // variables propagate to every element via `var(--theme-*)`.
    const root = document.documentElement.style;
    root.setProperty("--theme-bg",         t.bg);
    root.setProperty("--theme-bg-2",       t.bg_secondary);
    root.setProperty("--theme-bg-el",      t.bg_elevated);
    root.setProperty("--theme-text",       t.text);
    root.setProperty("--theme-text-muted", t.text_muted);
    root.setProperty("--theme-border",     t.border);
    root.setProperty("--theme-accent",     t.accent);
    root.setProperty("--theme-accent2",    t.accent2);
    root.setProperty("--theme-ok",         t.ok);
    root.setProperty("--theme-warn",       t.warn);
    root.setProperty("--theme-err",        t.err);
    root.setProperty("--theme-selection",  t.selection);
}

function themedLayout(overrides) {
    // Plotly doesn't read CSS vars, so we pass the theme colours in
    // directly. Falls back to the hardcoded light defaults if the
    // bridge handshake hasn't filled `theme` yet.
    const t = theme || {
        bg: "#FFFFFF", bg_elevated: "#FFFFFF",
        text: "#2C3E50", text_muted: "#7F8C8D",
        border: "#DEE2E6",
    };
    return Object.assign({
        paper_bgcolor: t.bg_elevated,
        plot_bgcolor:  t.bg_elevated,
        font: { color: t.text, size: 11 },
        xaxis: { gridcolor: t.border, zerolinecolor: t.border, color: t.text_muted },
        yaxis: { gridcolor: t.border, zerolinecolor: t.border, color: t.text_muted },
        autosize: true,
    }, overrides);
}

// Status line: ``key`` is a web.* key ("" clears it), kept so a language
// change can redraw it.
function setStatus(key, params) {
    lastStatus = { key: key, params: params || null };
    const el = document.getElementById("status");
    if (el) el.textContent = key ? _t(key, params) : "";
}

function showEmpty(div, key) {
    div.innerHTML = "";
    const e = document.createElement("div");
    e.className = "empty";
    e.textContent = _t(key);
    div.appendChild(e);
}

function updateRangeDisplay() {
    const el = document.getElementById("year-range");
    if (!el) return;
    if (state.yearRange) {
        el.textContent = `${state.yearRange[0]} – ${state.yearRange[1]}`;
    } else if (meta && meta.years && meta.years.length) {
        const first = meta.years[0], last = meta.years[meta.years.length - 1];
        el.textContent = _t("web.dashboard.range_all", { first: first, last: last });
    } else {
        el.textContent = "—";
    }
}

// Promise-ify a QWebChannel slot call. The QWebChannel API passes
// results to a callback (variadic args); we wrap it in a Promise so
// the rest of the file reads like normal async code.
function call(fn, ...args) {
    return new Promise((resolve, reject) => {
        try {
            fn(...args, result => resolve(result));
        } catch (err) {
            reject(err);
        }
    });
}

// ─────────────────────────────────────────────────────────────────
// UC renderers — replace the planning trajectory + mix with hourly
// operational views when the run is unit_commitment. The DOM divs
// (#trajectory, #mix) are reused so a system switch between UC and
// planning swaps content cleanly.
// ─────────────────────────────────────────────────────────────────

function renderHourlyLmp(data) {
    const div = document.getElementById("trajectory");
    try { Plotly.purge("trajectory"); } catch (e) {}
    if (!data || !data.hours || data.hours.length === 0) {
        showEmpty(div, "web.dashboard.no_lmp");
        return;
    }
    const x = data.hours;
    const traces = [
        {
            x: x, y: data.lmp, type: "scatter", mode: "lines",
            name: "LMP",
            line: { color: theme && theme.accent ? theme.accent : "#2C3E50",
                    width: 2 },
            xaxis: "x", yaxis: "y",
            hovertemplate: _t("web.dashboard.hover_hour_usd_mwh") + "<extra></extra>",
        },
        {
            x: x, y: data.load_shed_mw, type: "bar",
            name: _t("web.dashboard.load_shed_mw"),
            marker: { color: "rgba(231, 76, 60, 0.80)" },
            xaxis: "x", yaxis: "y2",
            hovertemplate: _t("web.dashboard.hover_hour_mw")
                + "<extra>" + _t("web.dashboard.load_shed") + "</extra>",
        },
    ];
    const layout = {
        margin: { t: 30, r: 60, b: 50, l: 70 },
        showlegend: true,
        legend: { orientation: "h", x: 0.5, xanchor: "center",
                  y: 1.10, yanchor: "bottom", font: { size: 10 } },
        xaxis: { title: "<b>" + _t("web.dashboard.axis_hour") + "</b>" },
        yaxis: { title: "<b>" + _t("web.dashboard.axis_lmp") + "</b>", rangemode: "tozero" },
        yaxis2: { title: "<b>" + _t("web.dashboard.load_shed_mw") + "</b>",
                  overlaying: "y", side: "right",
                  rangemode: "tozero", showgrid: false },
    };
    Plotly.newPlot("trajectory", traces, layout, {
        responsive: true, displaylogo: false,
        modeBarButtonsToRemove: ["lasso2d", "select2d"],
    });
}

function renderHourlyDispatch(data) {
    const div = document.getElementById("mix");
    try { Plotly.purge("mix"); } catch (e) {}
    if (!data || !data.hours || data.hours.length === 0) {
        showEmpty(div, "web.dashboard.no_dispatch");
        return;
    }
    const x = data.hours;
    const traces = [];

    // Positive stack: generation by tech (+ battery discharge +
    // load shed).
    for (const s of (data.series_pos || [])) {
        if (!s.values || !s.values.length) continue;
        traces.push({
            x: x, y: s.values, type: "scatter", mode: "none",
            stackgroup: "pos", name: s.label,
            fillcolor: s.color,
            hovertemplate:
                _t("web.dashboard.hover_hour_mw") + "<extra>" + s.label + "</extra>",
        });
    }
    // Negative stack: battery charge + curtailment (drawn below 0).
    for (const s of (data.series_neg || [])) {
        if (!s.values || !s.values.length) continue;
        const neg = s.values.map(v => -Math.abs(v || 0));
        traces.push({
            x: x, y: neg, type: "scatter", mode: "none",
            stackgroup: "neg", name: s.label,
            fillcolor: s.color,
            customdata: s.values,
            hovertemplate:
                _t("web.dashboard.hover_hour_customdata_mw") + "<extra>" + s.label + "</extra>",
        });
    }
    // Demand line on top.
    if (data.demand_mw && data.demand_mw.length) {
        traces.push({
            x: x, y: data.demand_mw, type: "scatter", mode: "lines",
            name: _t("web.dashboard.demand"), line: { color: "#000000", width: 2 },
            hovertemplate: _t("web.dashboard.hover_hour_mw")
                + "<extra>" + _t("web.dashboard.demand") + "</extra>",
        });
    }
    const layout = {
        margin: { t: 30, r: 30, b: 50, l: 70 },
        showlegend: true,
        legend: { orientation: "h", x: 0.5, xanchor: "center",
                  y: 1.10, yanchor: "bottom", font: { size: 10 } },
        shapes: [
            { type: "line", xref: "x domain", yref: "y",
              x0: 0, x1: 1, y0: 0, y1: 0,
              line: { color: "#000000", width: 1 } },
        ],
        xaxis: { title: "<b>" + _t("web.dashboard.axis_hour") + "</b>" },
        yaxis: { title: "<b>" + _t("web.dashboard.axis_power_mw") + "</b>" },
    };
    Plotly.newPlot("mix", traces, layout, {
        responsive: true, displaylogo: false,
        modeBarButtonsToRemove: ["lasso2d", "select2d"],
    });
}
