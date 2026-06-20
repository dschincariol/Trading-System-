// ui/terminal.js
import { setProChartsState, startLiveMarketChart, applyTerminalOverlays } from "./pro_charting.js";

const LS_KEY = "terminal.state.v1";

async function postJson(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {})
  });
  return await r.json();
}

function lsGet() {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return null;
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function lsSet(st) {
  try { localStorage.setItem(LS_KEY, JSON.stringify(st)); } catch {}
}

function esc(s) {
  return String(s ?? "")
    .replaceAll("&","&amp;")
    .replaceAll("<","&lt;")
    .replaceAll(">","&gt;")
    .replaceAll('"',"&quot;")
    .replaceAll("'","&#039;");
}

function fmtNum(x, d=2) {
  const n = Number(x);
  if (!Number.isFinite(n)) return "—";
  return n.toFixed(d);
}

function fmtTs(tsMs) {
  const n = Number(tsMs);
  if (!Number.isFinite(n) || n <= 0) return "—";
  try { return new Date(n).toLocaleString(); } catch { return String(n); }
}

async function fetchJson(url) {
  const r = await fetch(url, { cache: "no-store" });
  return await r.json();
}

const el = {
  symInput: document.getElementById("symInput"),
  tfSel: document.getElementById("tfSel"),
  typeSel: document.getElementById("typeSel"),
  ovVwap: document.getElementById("ovVwap"),
  ovEma: document.getElementById("ovEma"),
  ovMarkers: document.getElementById("ovMarkers"),
  ovEquity: document.getElementById("ovEquity"),
  watchFilter: document.getElementById("watchFilter"),
  watchList: document.getElementById("watchList"),
  watchMeta: document.getElementById("watchMeta"),
  chartTitle: document.getElementById("chartTitle"),
  chartHealth: document.getElementById("chartHealth"),
  xhairBox: document.getElementById("xhairBox"),
  acctCash: document.getElementById("acctCash"),
  acctEquity: document.getElementById("acctEquity"),
  acctUpdated: document.getElementById("acctUpdated"),
  acctMeta: document.getElementById("acctMeta"),
  posTbl: document.getElementById("posTbl"),
  ordTbl: document.getElementById("ordTbl"),
  fillsTbl: document.getElementById("fillsTbl"),
  fillsMeta: document.getElementById("fillsMeta"),
};

let STATE = {
  symbol: "SPY",
  tf: "1m",
  type: "candle",
  ov: { vwap: true, ema: true, markers: true, equity: true },
  watchFilter: "",
};

const saved = lsGet();
if (saved && typeof saved === "object") {
  STATE = {
    ...STATE,
    ...saved,
    ov: { ...STATE.ov, ...(saved.ov || {}) },
  };
}

el.symInput.value = STATE.symbol;
el.tfSel.value = STATE.tf;
el.typeSel.value = STATE.type;
el.ovVwap.checked = !!STATE.ov.vwap;
el.ovEma.checked = !!STATE.ov.ema;
el.ovMarkers.checked = !!STATE.ov.markers;
el.ovEquity.checked = !!STATE.ov.equity;
el.watchFilter.value = STATE.watchFilter || "";

function persist() { lsSet(STATE); }

function setSymbol(sym) {
  const s = String(sym || "").trim().toUpperCase();
  if (!s) return;
  STATE.symbol = s;
  el.symInput.value = s;
  persist();
  bootChart();
}

function setTf(tf) {
  STATE.tf = String(tf || "1m").trim();
  persist();
  bootChart();
}

function setType(tp) {
  STATE.type = String(tp || "candle").trim();
  persist();
  bootChart();
}

function setOv(key, v) {
  STATE.ov[key] = !!v;
  persist();
  bootChart();
}

function renderTable(container, headerCols, rows, rowColsFn) {
  const h = `<div class="row h">${headerCols.map(c => `<div class="c">${esc(c)}</div>`).join("")}</div>`;
  const body = (rows || []).map(r => {
    const cols = rowColsFn(r);
    return `<div class="row">${cols.map(c => `<div class="c">${c}</div>`).join("")}</div>`;
  }).join("");
  container.innerHTML = h + body;
}

let _watchSymbols = [];
let _snapshotTimer = null;

async function refreshSnapshot() {
  try {
    const j = await fetchJson("/api/terminal/snapshot");
    if (!j || !j.ok) return;

    // Watchlist
    _watchSymbols = Array.isArray(j.watchlist) ? j.watchlist : _watchSymbols;
    el.watchMeta.textContent = `${_watchSymbols.length} symbols`;

    // Account
    const acct = (j.equity && j.equity.account) ? j.equity.account : null;
    el.acctCash.textContent = acct ? fmtNum(acct.cash, 2) : "—";
    el.acctEquity.textContent = acct ? fmtNum(acct.equity, 2) : "—";
    el.acctUpdated.textContent = acct ? fmtTs(acct.updated_ts_ms) : "—";
    el.acctMeta.textContent = `snapshot latency ${Number(j.latency_ms || 0)}ms`;

    // Positions
    const pos = Array.isArray(j.positions) ? j.positions : [];
    renderTable(el.posTbl, ["Symbol","Qty","AvgPx","Updated"], pos, (r) => ([
      esc((r.symbol || "").toUpperCase()),
      `<span class="mono">${esc(fmtNum(r.qty, 4))}</span>`,
      `<span class="mono">${esc(fmtNum(r.avg_px, 4))}</span>`,
      `<span class="mono">${esc(fmtTs(r.updated_ts_ms))}</span>`,
    ]));

    // Orders
    const ords = (j.orders && typeof j.orders === "object") ? j.orders : { broker: [], portfolio: [] };
    const bro = Array.isArray(ords.broker) ? ords.broker : [];
    const por = Array.isArray(ords.portfolio) ? ords.portfolio : [];
    const merged = [
      ...bro.slice(0, 150).map(r => ({ kind: "broker", ...r })),
      ...por.slice(0, 150).map(r => ({ kind: "portfolio", ...r })),
    ].sort((a,b) => Number(b.updated_ts_ms || b.ts_ms || 0) - Number(a.updated_ts_ms || a.ts_ms || 0));

    renderTable(el.ordTbl, ["Kind","Symbol","State/Action","Updated"], merged, (r) => ([
      esc(r.kind || "—"),
      esc((r.symbol || "").toUpperCase()),
      esc(r.state || r.action || "—"),
      `<span class="mono">${esc(fmtTs(r.updated_ts_ms || r.ts_ms))}</span>`,
    ]));

    // Fills
    const fills = Array.isArray(j.fills) ? j.fills : [];
    el.fillsMeta.textContent = `${fills.length} rows`;
    renderTable(el.fillsTbl, ["Time","Symbol","Qty","Px"], fills.slice(0, 2000), (r) => ([
      `<span class="mono">${esc(fmtTs(r.ts_ms))}</span>`,
      esc((r.symbol || "").toUpperCase()),
      `<span class="mono">${esc(fmtNum(r.qty, 4))}</span>`,
      `<span class="mono">${esc(fmtNum(r.px, 4))}</span>`,
    ]));

  } catch {}
}

function renderWatch() {
  const q = String(STATE.watchFilter || "").trim().toUpperCase();
  const list = (q ? _watchSymbols.filter(s => String(s).toUpperCase().includes(q)) : _watchSymbols).slice(0, 500);

  el.watchList.innerHTML = list.map(s => {
    const active = (String(s).toUpperCase() === String(STATE.symbol).toUpperCase());
    return `<div class="item ${active ? "active":""}" data-sym="${esc(s)}">
      <div class="mono">${esc(s)}</div>
      <div class="badge">chart</div>
    </div>`;
  }).join("");

  el.watchList.querySelectorAll(".item").forEach(n => {
    n.addEventListener("click", () => setSymbol(n.getAttribute("data-sym")));
  });
}

async function bootChart() {
  const sym = String(STATE.symbol || "").trim().toUpperCase();
  if (!sym) return;

  el.chartTitle.textContent = `${sym} • ${STATE.tf} • ${STATE.type}`;
  el.chartHealth.textContent = "boot…";

  // Ensure pro charts enabled always in terminal
  setProChartsState({ enabled: true, tf: STATE.tf, type: STATE.type });

  // Start base chart
  await startLiveMarketChart({
    containerId: "terminalChart",
    symbol: sym,
    tf: STATE.tf,
    type: STATE.type,
    crosshairElId: "xhairBox",
    healthElId: "chartHealth",
  });

  // Load overlays inputs
  const overlays = {
    vwap: !!STATE.ov.vwap,
    ema: !!STATE.ov.ema,
    markers: !!STATE.ov.markers,
    equity: !!STATE.ov.equity,
  };

  // Fetch data for overlays
  let markers = [];
  let equitySeries = [];

  try {
    if (overlays.markers) {
      const mj = await fetchJson(`/api/terminal/markers?symbol=${encodeURIComponent(sym)}`);
      if (mj && mj.ok && Array.isArray(mj.markers)) markers = mj.markers;
    }
  } catch {}

  try {
    if (overlays.equity) {
      const ej = await fetchJson(`/api/terminal/equity?limit=3000`);
      if (ej && ej.ok && Array.isArray(ej.series)) equitySeries = ej.series;
    }
  } catch {}

  // Apply overlays + markers
  applyTerminalOverlays({
    symbol: sym,
    overlays,
    markers,
    equitySeries,
  });

  el.chartHealth.textContent = "live";
}

function wire() {
  el.symInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") setSymbol(el.symInput.value);
  });

  el.tfSel.addEventListener("change", () => setTf(el.tfSel.value));
  el.typeSel.addEventListener("change", () => setType(el.typeSel.value));

  el.ovVwap.addEventListener("change", () => setOv("vwap", el.ovVwap.checked));
  el.ovEma.addEventListener("change", () => setOv("ema", el.ovEma.checked));
  el.ovMarkers.addEventListener("change", () => setOv("markers", el.ovMarkers.checked));
  el.ovEquity.addEventListener("change", () => setOv("equity", el.ovEquity.checked));

  el.watchFilter.addEventListener("input", () => {
    STATE.watchFilter = el.watchFilter.value || "";
    persist();
    renderWatch();
  });

  document.addEventListener("keydown", async (e) => {
  if (e.target.tagName === "INPUT") return;

  if (e.key === "b") {
    await postJson("/api/terminal/order", {
      symbol: STATE.symbol,
      side: "BUY",
      qty: 100
    });
    refreshSnapshot();
  }

  if (e.key === "s") {
    await postJson("/api/terminal/order", {
      symbol: STATE.symbol,
      side: "SELL",
      qty: 100
    });
    refreshSnapshot();
  }

  if (e.key === "f") {
    await postJson("/api/terminal/flatten", {
      symbol: STATE.symbol
    });
    refreshSnapshot();
  }
});

  const ordQty = document.getElementById("ordQty");
const btnBuy = document.getElementById("btnBuy");
const btnSell = document.getElementById("btnSell");
const btnFlat = document.getElementById("btnFlat");

btnBuy.addEventListener("click", async () => {
  const j = await postJson("/api/terminal/order", {
    symbol: STATE.symbol,
    side: "BUY",
    qty: Number(ordQty.value || 0)
  });
  if (j.ok) refreshSnapshot();
});

btnSell.addEventListener("click", async () => {
  const j = await postJson("/api/terminal/order", {
    symbol: STATE.symbol,
    side: "SELL",
    qty: Number(ordQty.value || 0)
  });
  if (j.ok) refreshSnapshot();
});

btnFlat.addEventListener("click", async () => {
  const j = await postJson("/api/terminal/flatten", {
    symbol: STATE.symbol
  });
  if (j.ok) refreshSnapshot();
});

  // periodic snapshot refresh
  if (_snapshotTimer) clearInterval(_snapshotTimer);
  _snapshotTimer = setInterval(async () => {
    await refreshSnapshot();
    renderWatch();
  }, 2500);
}

(async function main(){
  wire();
  await refreshSnapshot();
  renderWatch();
  await bootChart();
})();