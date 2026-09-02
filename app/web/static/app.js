/* APIx dashboard — vanilla JS, no build step. Talks to the same-origin
 * FastAPI read layer (app/api/routers/{fares,index}.py) over relative
 * fetch() calls, so this file only ever needs to be dropped behind
 * uvicorn — no bundler, no config. */

const BASKET_ROUTE_COUNT = 20; // app/ingestion/scheduler.py's ROUTE_PAIRS
const BACKTEST_TARGET_DAYS = 30; // the project's 30-day DGCA back-test requirement
const AP_WINDOWS = ["T+1", "T+7", "T+15", "T+30", "T+45"];

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
  valueEl.textContent = fmtIndex(latest.index_value);

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
  document.getElementById("kpi-observations").textContent = fmtInt(totalObservations);

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

function renderRouteTable(summaryRows) {
  const tbody = document.getElementById("route-table-body");
  if (!summaryRows.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="table-loading">No fare data loaded yet.</td></tr>';
    return;
  }
  const latestDate = summaryRows.reduce((max, r) => (r.fare_date > max ? r.fare_date : max), summaryRows[0].fare_date);
  const latestRows = summaryRows.filter((r) => r.fare_date === latestDate);

  const byRoute = new Map();
  for (const row of latestRows) {
    if (!byRoute.has(row.route_id)) byRoute.set(row.route_id, {});
    byRoute.get(row.route_id)[row.lead_window] = row.avg_total_fare;
  }

  const routeIds = [...byRoute.keys()].sort();
  tbody.innerHTML = routeIds
    .map((routeId) => {
      const cells = AP_WINDOWS.map((w) => {
        const v = byRoute.get(routeId)[w];
        return `<td>${v != null ? fmtMoney(v) : '<span class="cell-empty">—</span>'}</td>`;
      }).join("");
      return `<tr><td>${routeId}</td>${cells}</tr>`;
    })
    .join("");
}

/* ---------------- Index chart ---------------- */

let chart = null;
let indexRowsByWindow = { OVERALL: [] };
for (const w of AP_WINDOWS) indexRowsByWindow[w] = [];

function seriesFor(windowLabel) {
  const rows = [...(indexRowsByWindow[windowLabel] || [])].sort((a, b) => a.index_date.localeCompare(b.index_date));
  return {
    labels: rows.map((r) => r.index_date),
    values: rows.map((r) => Number(r.index_value)),
  };
}

function drawChart(windowLabel) {
  const { labels, values } = seriesFor(windowLabel);
  const canvas = document.getElementById("index-chart");
  const empty = document.getElementById("chart-empty");
  const note = document.getElementById("chart-baseline-note");

  if (!labels.length) {
    canvas.style.display = "none";
    empty.hidden = false;
    note.hidden = true;
    return;
  }
  canvas.style.display = "block";
  empty.hidden = true;
  note.hidden = labels.length > 1;

  const styles = getComputedStyle(document.documentElement);
  const accent = styles.getPropertyValue("--accent").trim();
  const accentSoft = styles.getPropertyValue("--accent-soft").trim();
  const gridColor = styles.getPropertyValue("--line").trim();
  const inkSoft = styles.getPropertyValue("--ink-soft").trim();

  const data = {
    labels,
    datasets: [
      {
        label: windowLabel === "OVERALL" ? "Overall index" : windowLabel,
        data: values,
        borderColor: accent,
        backgroundColor: accentSoft,
        pointBackgroundColor: accent,
        pointRadius: labels.length > 1 ? 3 : 6,
        pointHoverRadius: 6,
        borderWidth: 2.5,
        fill: true,
        tension: 0.25,
      },
    ],
  };

  const options = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { display: false },
      tooltip: {
        callbacks: { label: (ctx) => `Index: ${ctx.parsed.y.toFixed(2)}` },
      },
    },
    scales: {
      x: { grid: { color: gridColor }, ticks: { color: inkSoft, font: { family: "'IBM Plex Mono', monospace", size: 11 } } },
      y: {
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
  const [runs, indexRows, summaryRows, routes] = await Promise.all([
    fetchJSON("/scrape-runs?limit=10").catch(() => []),
    fetchJSON("/index/daily").catch(() => []),
    fetchJSON("/fares/daily-summary").catch(() => []),
    fetchJSON("/routes").catch(() => []),
  ]);

  renderFreshness(runs);
  renderRunList(runs);

  indexRowsByWindow = { OVERALL: [] };
  for (const w of AP_WINDOWS) indexRowsByWindow[w] = [];
  for (const row of indexRows) {
    const key = row.lead_window === "OVERALL" ? "OVERALL" : row.lead_window;
    if (!indexRowsByWindow[key]) indexRowsByWindow[key] = [];
    indexRowsByWindow[key].push(row);
  }
  renderIndexKpi(indexRowsByWindow.OVERALL);
  drawChart(activeWindowLabel());

  renderVolumeKpis(summaryRows);
  renderRoutesKpi(routes);
  renderRouteTable(summaryRows);

  latestKnownRunSignature = runs[0] ? `${runs[0].id}:${runs[0].status}:${runs[0].finished_at}` : null;
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
});
