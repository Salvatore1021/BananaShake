/* APIx dashboard — vanilla JS, no build step. Talks to the same-origin
 * FastAPI read layer (app/api/routers/{fares,index}.py) over relative
 * fetch() calls, so this file only ever needs to be dropped behind
 * uvicorn — no bundler, no config. */

const BASKET_ROUTE_COUNT = 20; // app/ingestion/scheduler.py's ROUTE_PAIRS
const BACKTEST_TARGET_DAYS = 30; // the project's 30-day DGCA back-test requirement
const AP_WINDOWS = ["T+1", "T+7", "T+15", "T+30", "T+45"];

// Drives drawChart()'s Chart.js animation.duration directly. The KPI
// cards stagger in one-by-one rather than all at once (see .kpi-strip
// .kpi / :nth-child in styles.css) with the LAST card timed to land
// exactly when this finishes -- that CSS hand-computes its own
// delay+duration to sum to this same number, since CSS can't read a JS
// constant. Change this, and update styles.css's comment/numbers next to
// .kpi-strip .kpi to match.
const KPI_REVEAL_MS = 1100;

const fmtMoney = (n) =>
  n == null ? "—" : `₹${Math.round(n).toLocaleString("en-IN")}`;
const fmtIndex = (n) => (n == null ? "—" : Number(n).toFixed(1));
const fmtInt = (n) => (n == null ? "—" : Number(n).toLocaleString("en-IN"));

async function fetchJSON(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}`);
  return res.json();
}

function relativeTime(isoString) {
  const then = new Date(isoString + (isoString.endsWith("Z") ? "" : "Z"));
  const diffMs = Date.now() - then.getTime();
  const mins = Math.round(diffMs / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return `${days}d ago`;
}

function statusPillClass(status) {
  return { success: "pill--success", partial: "pill--partial", failed: "pill--failed", running: "pill--running" }[status] || "pill--muted";
}

const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* Animates one element's text from 0 up to targetValue on every call,
 * formatting each intermediate frame with the same formatter the final
 * value uses (fmtIndex/fmtInt) so it never flashes an oddly-shaped number
 * mid-count. Keyed by a per-element token rather than a shared flag so a
 * second call (e.g. the 30s poll refresh landing while a KPI is still
 * mid-count from the last one) cleanly supersedes the first instead of
 * both rAF loops fighting over the same textContent. */
function animateCountUp(el, targetValue, formatFn, durationMs = 900) {
  if (!el) return;
  if (prefersReducedMotion || !Number.isFinite(targetValue)) {
    el.textContent = formatFn(targetValue);
    return;
  }
  const token = (el._countUpToken = (el._countUpToken || 0) + 1);
  const start = performance.now();

  function tick(now) {
    if (el._countUpToken !== token) return; // a newer animateCountUp call took over
    const progress = Math.min(1, (now - start) / durationMs);
    const eased = 1 - Math.pow(1 - progress, 3); // ease-out cubic
    el.textContent = formatFn(targetValue * eased);
    if (progress < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

/* ---------------- Freshness pill + run list ---------------- */

function renderFreshness(runs) {
  const el = document.getElementById("freshness");
  const label = document.getElementById("freshness-label");
  if (!runs.length) {
    label.textContent = "no scrape runs recorded yet";
    return;
  }
  const latest = runs[0];
  el.className = `freshness pill ${statusPillClass(latest.status)}`;
  const when = latest.finished_at || latest.started_at;
  label.textContent = `${latest.status} · updated ${relativeTime(when)}`;
}

function renderRunList(runs) {
  const list = document.getElementById("run-list");
  if (!runs.length) {
    list.innerHTML = '<li class="run-loading">No runs yet — the daily job hasn’t fired.</li>';
    return;
  }
  list.innerHTML = runs
    .slice(0, 8)
    .map((run) => {
      const missing = sumMissing(run);
      const coveragePct = run.tasks_total ? Math.round(((run.tasks_total - missing) / run.tasks_total) * 100) : null;
      const when = run.finished_at || run.started_at;
      return `
        <li>
          <div class="run-meta">
            <span class="run-time">${new Date(when + (when.endsWith("Z") ? "" : "Z")).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" })}</span>
            <span class="run-detail">${fmtInt(run.items_loaded)} loaded${coveragePct != null ? ` · ${coveragePct}% task coverage` : ""}${run.error_message ? " · db error" : ""}</span>
          </div>
          <span class="pill ${statusPillClass(run.status)}"><span class="pill-dot"></span>${run.status}</span>
        </li>`;
    })
    .join("");
}

function sumMissing(run) {
  if (!run.missing_data_summary) return 0;
  try {
    const obj = JSON.parse(run.missing_data_summary);
    return Object.values(obj).reduce((a, b) => a + b, 0);
  } catch {
    return 0;
  }
}

/* ---------------- KPI strip ---------------- */

function renderIndexKpi(overallRows) {
  const valueEl = document.getElementById("kpi-index-value");
  const deltaEl = document.getElementById("kpi-index-delta");
  if (!overallRows.length) {
    valueEl.textContent = "—";
    return;
  }
  const sorted = [...overallRows].sort((a, b) => a.index_date.localeCompare(b.index_date));
  const latest = sorted[sorted.length - 1];
  animateCountUp(valueEl, Number(latest.index_value), fmtIndex);

  if (sorted.length < 2) {
    deltaEl.textContent = "day 1 — baseline";
    deltaEl.className = "kpi-delta is-flat";
    return;
  }
  const prev = sorted[sorted.length - 2];
  const change = Number(latest.index_value) - Number(prev.index_value);
  const pct = (change / Number(prev.index_value)) * 100;
  deltaEl.textContent = `${change >= 0 ? "▲" : "▼"} ${Math.abs(pct).toFixed(1)}% vs prior day`;
  deltaEl.className = `kpi-delta ${change > 0 ? "is-up" : change < 0 ? "is-down" : "is-flat"}`;
}

function renderVolumeKpis(summaryRows) {
  const totalObservations = summaryRows.reduce((sum, r) => sum + r.sample_count, 0);
  // fmtInt doesn't round on its own (fine for its other, already-integer
  // call sites) -- wrapped here so the mid-count frames show whole
  // numbers instead of toLocaleString's raw fractional digits.
  animateCountUp(document.getElementById("kpi-observations"), totalObservations, (n) => fmtInt(Math.round(n)));

  const distinctDates = new Set(summaryRows.map((r) => r.fare_date));
  const days = distinctDates.size;
  document.getElementById("kpi-days").textContent = `${days} / ${BACKTEST_TARGET_DAYS}`;
  document.getElementById("progress-fill").style.width = `${Math.min((days / BACKTEST_TARGET_DAYS) * 100, 100)}%`;

  return { totalObservations, days, distinctDates };
}

function renderRoutesKpi(routes) {
  document.getElementById("kpi-routes").textContent = `${routes.length}`;
  document.getElementById("kpi-routes").nextElementSibling.textContent = `of ${BASKET_ROUTE_COUNT} in the daily basket`;
}

/* ---------------- Route table (latest day) ---------------- */

// Group a route with its reverse (BOM-DEL next to DEL-BOM) by sorting on the
// unordered city pair first, direction second — a plain alphabetical sort
// would scatter DEL-BOM and BOM-DEL to opposite ends of the table/chart.
const pairKey = (routeId) => routeId.split("-").sort().join("-");
function sortRouteIds(routeIds) {
  return [...routeIds].sort((a, b) => {
    const byPair = pairKey(a).localeCompare(pairKey(b));
    return byPair !== 0 ? byPair : a.localeCompare(b);
  });
}

/* summaryRows -> {latestDate, byRoute} where byRoute maps route_id to a
 * {lead_window: avg_total_fare} object -- the one grouping the route table
 * and the advance-purchase premium chart both need, computed once per
 * loadData() call rather than twice over the same rows. */
function latestDayByRoute(summaryRows) {
  if (!summaryRows.length) return { latestDate: null, byRoute: new Map() };
  const latestDate = summaryRows.reduce((max, r) => (r.fare_date > max ? r.fare_date : max), summaryRows[0].fare_date);
  const latestRows = summaryRows.filter((r) => r.fare_date === latestDate);
  const byRoute = new Map();
  for (const row of latestRows) {
    if (!byRoute.has(row.route_id)) byRoute.set(row.route_id, {});
    byRoute.get(row.route_id)[row.lead_window] = row;
  }
  return { latestDate, byRoute, latestRows };
}

function renderRouteTable(byRoute) {
  const tbody = document.getElementById("route-table-body");
  if (!byRoute.size) {
    tbody.innerHTML = '<tr><td colspan="6" class="table-loading">No fare data loaded yet.</td></tr>';
    return;
  }
  const routeIds = sortRouteIds(byRoute.keys());
  tbody.innerHTML = routeIds
    .map((routeId) => {
      const cells = AP_WINDOWS.map((w) => {
        const v = byRoute.get(routeId)[w]?.avg_total_fare;
        return `<td>${v != null ? fmtMoney(v) : '<span class="cell-empty">—</span>'}</td>`;
      }).join("");
      return `<tr><td>${routeId}</td>${cells}</tr>`;
    })
    .join("");
}

/* ---------------- Advance-purchase premium by route ---------------- */

// How much more a route costs to book at the last minute vs. early --
// max(avg fare across AP windows) - min(avg fare across AP windows), on
// the most recent day, ranked descending. Genuinely informative (it's
// the real early-bird-discount / last-minute-markup signal this whole
// project's AP-window basket exists to capture) and replaces a donut
// that plotted the AP-window SHARE OF QUOTES COLLECTED -- a figure this
// project's own equal-effort-per-window scraper keeps close to even by
// construction (see app/ingestion/scheduler.py), so that chart rarely
// showed anything but five near-equal slices.
let premiumBarChart = null;
const PREMIUM_BAR_LIMIT = 8;

function renderAdvancePurchasePremium(byRoute) {
  const canvas = document.getElementById("premium-bar-chart");
  const empty = document.getElementById("premium-bar-empty");

  const entries = sortRouteIds(byRoute.keys())
    .map((routeId) => {
      const fares = AP_WINDOWS.map((w) => byRoute.get(routeId)[w]?.avg_total_fare)
        .filter((v) => v != null)
        .map(Number);
      return fares.length >= 2 ? { routeId, spread: Math.max(...fares) - Math.min(...fares) } : null;
    })
    .filter(Boolean)
    .sort((a, b) => b.spread - a.spread)
    .slice(0, PREMIUM_BAR_LIMIT);

  if (!entries.length) {
    canvas.style.display = "none";
    empty.hidden = false;
    return;
  }
  canvas.style.display = "block";
  empty.hidden = true;

  const styles = getComputedStyle(document.documentElement);
  const accent = styles.getPropertyValue("--accent").trim();
  const gridColor = styles.getPropertyValue("--line").trim();
  const inkSoft = styles.getPropertyValue("--ink-soft").trim();

  const data = {
    labels: entries.map((e) => e.routeId),
    datasets: [
      {
        data: entries.map((e) => e.spread),
        backgroundColor: accent,
        borderRadius: 4,
        maxBarThickness: 16,
      },
    ],
  };
  const options = {
    indexAxis: "y",
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: KPI_REVEAL_MS, easing: "easeOutQuart" },
    plugins: {
      legend: { display: false },
      tooltip: { callbacks: { label: (ctx) => `Spread: ${fmtMoney(ctx.parsed.x)}` } },
    },
    scales: {
      x: {
        grid: { color: gridColor },
        ticks: { color: inkSoft, font: { family: "'IBM Plex Mono', monospace", size: 11 }, callback: (v) => fmtMoney(v) },
      },
      y: { grid: { display: false }, ticks: { color: inkSoft, font: { family: "'IBM Plex Mono', monospace", size: 11.5 } } },
    },
  };

  if (premiumBarChart) {
    premiumBarChart.data = data;
    premiumBarChart.options = options;
    premiumBarChart.update();
  } else {
    premiumBarChart = new Chart(canvas.getContext("2d"), { type: "bar", data, options });
  }
}

/* ---------------- Pie / donut charts ---------------- */

let runPieChart = null;

function renderPieLegend(listEl, entries) {
  listEl.innerHTML = entries
    .map(
      (e) => `
      <li>
        <span class="pie-legend-swatch" style="background:${e.color}"></span>
        <span class="pie-legend-label">${e.label}</span>
        <span class="pie-legend-value">${e.value}</span>
      </li>`
    )
    .join("");
}

function drawDonut(canvas, existingChart, labels, data, colors) {
  const cfg = {
    type: "doughnut",
    data: { labels, datasets: [{ data, backgroundColor: colors, borderColor: "#F5F5F4", borderWidth: 2, hoverOffset: 4 }] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: "62%",
      animation: { duration: KPI_REVEAL_MS, easing: "easeOutQuart" },
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: (ctx) => ` ${ctx.label}: ${ctx.formattedValue}` } } },
    },
  };
  if (existingChart) {
    existingChart.data = cfg.data;
    existingChart.update();
    return existingChart;
  }
  return new Chart(canvas.getContext("2d"), cfg);
}

// Run outcome is a STATUS job (good/warning/critical state), so this pie
// reuses the app's existing fixed status palette (same hexes as the
// freshness pill / run list) rather than a categorical or ordinal ramp --
// see dataviz skill's color-formula.md, "status is fixed."
function renderRunPie(runs) {
  const canvas = document.getElementById("run-pie-chart");
  const empty = document.getElementById("run-pie-empty");
  const legendEl = document.getElementById("run-pie-legend");

  const recent = runs.slice(0, 10);
  if (!recent.length) {
    canvas.style.display = "none";
    empty.hidden = false;
    legendEl.innerHTML = "";
    return;
  }
  canvas.style.display = "block";
  empty.hidden = true;

  const styles = getComputedStyle(document.documentElement);
  const statusOrder = ["success", "partial", "failed", "running"];
  const statusVar = { success: "--status-success", partial: "--status-partial", failed: "--status-failed", running: "--status-running" };
  const counts = new Map(statusOrder.map((s) => [s, 0]));
  for (const run of recent) {
    counts.set(run.status, (counts.get(run.status) || 0) + 1);
  }

  const present = statusOrder.filter((s) => counts.get(s) > 0);
  const colors = present.map((s) => styles.getPropertyValue(statusVar[s]).trim());
  const data = present.map((s) => counts.get(s));
  runPieChart = drawDonut(canvas, runPieChart, present, data, colors);
  renderPieLegend(
    legendEl,
    present.map((s, i) => ({ color: colors[i], label: s, value: `${counts.get(s)}/${recent.length}` }))
  );
}

/* ---------------- Index chart ---------------- */

// The three index-construction formulas app/index/psd_index.py computes
// and persists side by side (see that module's docstring) -- every one
// of them is plotted as its own line, in this fixed order, so a 4th
// method later would extend this array rather than require touching
// drawChart()/renderChartLegend() at all. carli_arithmetic_v1 (the
// original, superseded formula) is deliberately left off the dashboard
// -- still readable via GET /index/daily?method=carli_arithmetic_v1 for
// anyone who wants it, just not one of the headline chart lines.
const INDEX_METHODS = [
  { key: "jevons_geometric_v2", label: "Jevons (elementary)", colorVar: "--series-jevons" },
  { key: "tornqvist_bilateral_v1", label: "Törnqvist (bilateral)", colorVar: "--series-tornqvist" },
  { key: "geks_multilateral_v1", label: "GEKS (multilateral)", colorVar: "--series-geks" },
];

let chart = null;
let indexRowsByMethod = {}; // method -> { OVERALL: [...], "T+1": [...], ... }
for (const m of INDEX_METHODS) {
  indexRowsByMethod[m.key] = { OVERALL: [] };
  for (const w of AP_WINDOWS) indexRowsByMethod[m.key][w] = [];
}

const BASELINE_LABEL = "Baseline (100)";

function renderChartLegend() {
  const el = document.getElementById("chart-legend");
  const styles = getComputedStyle(document.documentElement);
  el.innerHTML = "";
  for (const m of INDEX_METHODS) {
    const item = document.createElement("span");
    item.className = "chart-legend-item";
    const swatch = document.createElement("span");
    swatch.className = "chart-legend-swatch";
    swatch.style.background = styles.getPropertyValue(m.colorVar).trim();
    item.append(swatch, m.label);
    el.appendChild(item);
  }
  const baselineItem = document.createElement("span");
  baselineItem.className = "chart-legend-item";
  const baselineSwatch = document.createElement("span");
  baselineSwatch.className = "chart-legend-swatch chart-legend-swatch--dashed";
  baselineItem.append(baselineSwatch, BASELINE_LABEL);
  el.appendChild(baselineItem);
}

function drawChart(windowLabel) {
  const canvas = document.getElementById("index-chart");
  const empty = document.getElementById("chart-empty");
  const note = document.getElementById("chart-baseline-note");
  const legendEl = document.getElementById("chart-legend");

  const perMethod = INDEX_METHODS.map((m) => ({
    ...m,
    rows: [...(indexRowsByMethod[m.key]?.[windowLabel] || [])].sort((a, b) => a.index_date.localeCompare(b.index_date)),
  }));

  // Union of every date any of the three methods has a value for --
  // in practice all three are computed from the same fare_observations
  // so their date coverage matches exactly, but building the label axis
  // this way means one method quietly having a gap (e.g. GEKS needing at
  // least one bridging date) never breaks the chart, just leaves that
  // point out of that one line (spanGaps below).
  const labels = [...new Set(perMethod.flatMap((m) => m.rows.map((r) => r.index_date)))].sort();

  if (!labels.length) {
    canvas.style.display = "none";
    empty.hidden = false;
    note.hidden = true;
    legendEl.innerHTML = "";
    return;
  }
  canvas.style.display = "block";
  empty.hidden = true;
  note.hidden = labels.length > 1;
  renderChartLegend();

  const styles = getComputedStyle(document.documentElement);
  const gridColor = styles.getPropertyValue("--line").trim();
  const inkSoft = styles.getPropertyValue("--ink-soft").trim();

  const datasets = perMethod.map((m) => {
    const byDate = new Map(m.rows.map((r) => [r.index_date, Number(r.index_value)]));
    const color = styles.getPropertyValue(m.colorVar).trim();
    return {
      label: m.label,
      data: labels.map((d) => (byDate.has(d) ? byDate.get(d) : null)),
      borderColor: color,
      pointBackgroundColor: color,
      pointRadius: labels.length > 1 ? 3 : 6,
      pointHoverRadius: labels.length > 1 ? 5 : 8,
      borderWidth: 2.5,
      // No area fill with three overlapping lines -- fills would stack
      // into a muddy blend where they cross, which a single-series chart
      // never had to worry about. Thin lines + the legend above carry
      // identity instead (see the dataviz skill's mark-spec guidance).
      fill: false,
      spanGaps: true,
      tension: 0.25,
    };
  });

  // Every point across all three series sets the scale -- no point is
  // ever clamped or pinned to an edge. 100 is always included too, even
  // on days every method happens to sit well clear of it, so the
  // baseline line below is never scrolled out of view. A flat 12%
  // headroom above/below that combined min/max keeps a spike from being
  // cut off by the chart's edge while still leaving real day-to-day
  // movement readable.
  const allValues = [...datasets.flatMap((d) => d.data.filter((v) => v != null)), 100];
  const seriesMax = Math.max(...allValues);
  const seriesMin = Math.min(...allValues);
  const pad = (seriesMax - seriesMin) * 0.12 || seriesMax * 0.05 || 1;
  const yMax = seriesMax + pad;
  const yMin = Math.max(0, seriesMin - pad);

  // A fixed reference line at index=100 -- every method's own value is
  // real, unadjusted data (see INDEX_METHODOLOGY.md); this line doesn't
  // change any of them, it just makes which side of the baseline each
  // one sits on unambiguous at a glance, day by day, since different
  // aggregation formulas CAN legitimately land on opposite sides of 100
  // on the same day (a mix of routes moving in different directions
  // reads differently through an arithmetic vs. a geometric combination
  // across routes) -- that divergence is a real result, not a rendering
  // artifact, and this line exists to make it legible rather than to
  // hide it.
  datasets.push({
    label: BASELINE_LABEL,
    data: labels.map(() => 100),
    borderColor: inkSoft,
    borderWidth: 1,
    borderDash: [5, 4],
    pointRadius: 0,
    pointHoverRadius: 0,
    fill: false,
    tension: 0,
  });

  const data = { labels, datasets };

  const options = {
    responsive: true,
    maintainAspectRatio: false,
    // Explicit (rather than Chart.js's own default) so it's pinned to the
    // same KPI_REVEAL_MS the KPI-strip reveal transition uses — see that
    // constant's own comment.
    animation: { duration: KPI_REVEAL_MS, easing: "easeOutQuart" },
    plugins: {
      // The HTML legend above the canvas (renderChartLegend) replaces
      // Chart.js's own -- it keeps the label text in a fixed ink color
      // (never the series color) and stays visible outside the canvas.
      legend: { display: false },
      tooltip: {
        filter: (ctx) => ctx.dataset.label !== BASELINE_LABEL,
        callbacks: {
          label: (ctx) => (ctx.parsed.y == null ? null : `${ctx.dataset.label}: ${ctx.parsed.y.toFixed(2)}`),
        },
      },
    },
    scales: {
      x: { grid: { color: gridColor }, ticks: { color: inkSoft, font: { family: "'IBM Plex Mono', monospace", size: 11 } } },
      y: {
        min: yMin,
        max: yMax,
        grid: { color: gridColor },
        ticks: { color: inkSoft, font: { family: "'IBM Plex Mono', monospace", size: 11 } },
      },
    },
  };

  if (chart) {
    chart.data = data;
    chart.options = options;
    chart.update();
  } else {
    chart = new Chart(canvas.getContext("2d"), { type: "line", data, options });
  }
}

function wireWindowToggle() {
  const container = document.getElementById("window-toggle");
  container.addEventListener("click", (e) => {
    const btn = e.target.closest(".toggle-btn");
    if (!btn) return;
    container.querySelectorAll(".toggle-btn").forEach((b) => {
      b.classList.remove("is-active");
      b.setAttribute("aria-selected", "false");
    });
    btn.classList.add("is-active");
    btn.setAttribute("aria-selected", "true");
    drawChart(btn.dataset.window);
  });
}


/* ---------------- Init + auto-refresh ---------------- */

// The daily job can finish loading new rows while a dashboard tab is
// already open — poll for that instead of leaving the tab showing
// whatever was current at page-load until someone manually reloads.
const REFRESH_INTERVAL_MS = 30_000;
let latestKnownRunSignature = null;

function activeWindowLabel() {
  const active = document.querySelector("#window-toggle .toggle-btn.is-active");
  return active ? active.dataset.window : "OVERALL";
}

async function loadData() {
  const [runs, summaryRows, routes, ...methodRows] = await Promise.all([
    fetchJSON("/scrape-runs?limit=10").catch(() => []),
    fetchJSON("/fares/daily-summary").catch(() => []),
    fetchJSON("/routes").catch(() => []),
    ...INDEX_METHODS.map((m) => fetchJSON(`/index/daily?method=${encodeURIComponent(m.key)}`).catch(() => [])),
  ]);

  renderFreshness(runs);
  renderRunList(runs);

  const jevonsIdx = INDEX_METHODS.findIndex((m) => m.key === "jevons_geometric_v2");
  renderIndexKpi(methodRows[jevonsIdx].filter((r) => r.lead_window === "OVERALL"));

  indexRowsByMethod = {};
  INDEX_METHODS.forEach((m, i) => {
    const byWindow = { OVERALL: [] };
    for (const w of AP_WINDOWS) byWindow[w] = [];
    for (const row of methodRows[i]) {
      const key = row.lead_window === "OVERALL" ? "OVERALL" : row.lead_window;
      if (!byWindow[key]) byWindow[key] = [];
      byWindow[key].push(row);
    }
    indexRowsByMethod[m.key] = byWindow;
  });
  drawChart(activeWindowLabel());
  revealKpiStrip(); // same tick as drawChart() -- see KPI_REVEAL_MS

  renderVolumeKpis(summaryRows);
  renderRoutesKpi(routes);
  const { byRoute } = latestDayByRoute(summaryRows);
  renderRouteTable(byRoute);
  renderAdvancePurchasePremium(byRoute);
  renderRunPie(runs);

  latestKnownRunSignature = runs[0] ? `${runs[0].id}:${runs[0].status}:${runs[0].finished_at}` : null;
}

// Fires once per page load, not on every 30s poll refresh -- the KPI
// cards sliding out from behind the chart panel is a load moment, not
// something that should replay every time the background poll happens to
// land new data.
let hasRevealedKpis = false;
function revealKpiStrip() {
  if (hasRevealedKpis) return;
  hasRevealedKpis = true;
  document.getElementById("kpi-strip")?.classList.add("is-revealed");
}

async function pollForUpdates() {
  try {
    const runs = await fetchJSON("/scrape-runs?limit=1");
    const signature = runs[0] ? `${runs[0].id}:${runs[0].status}:${runs[0].finished_at}` : null;
    if (signature !== latestKnownRunSignature) {
      await loadData();
    }
  } catch {
    // Transient fetch failure — leave the dashboard showing its last good
    // state and just try again on the next tick.
  }
}

async function init() {
  wireWindowToggle();
  await loadData();
  setInterval(pollForUpdates, REFRESH_INTERVAL_MS);
}

init().catch((err) => {
  console.error("APIx dashboard failed to load:", err);
  document.getElementById("freshness-label").textContent = "failed to load data — is the API reachable?";
  // loadData() threw before ever reaching revealKpiStrip() -- without
  // this the KPI cards would stay permanently at opacity:0 (their
  // pre-reveal CSS default), invisible forever rather than just showing
  // their "—" placeholder state.
  revealKpiStrip();
});
