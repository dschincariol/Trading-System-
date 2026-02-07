"use strict";

/* ui/dashboard.js — Market Impact Dashboard controller */

function esc(x) {
  return String(x ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function setOpsError(msg) {
  const el = document.getElementById("console");
  if (el) el.textContent += `[ops] ${msg}\n`;
}

function toast(msg, level = "ok", ms = 2200) {
  let el = document.getElementById("uiToast");
  if (!el) {
    el = document.createElement("div");
    el.id = "uiToast";
    el.style.position = "fixed";
    el.style.bottom = "16px";
    el.style.right = "16px";
    el.style.zIndex = "99999";
    el.style.padding = "10px 14px";
    el.style.borderRadius = "10px";
    el.style.border = "1px solid #30363d";
    el.style.background = "#0b0f15";
    el.style.color = "#e6edf3";
    el.style.boxShadow = "0 12px 30px rgba(0,0,0,.35)";
    el.style.fontSize = "13px";
    document.body.appendChild(el);
  }

  el.textContent = msg;
  el.className = "pill " + (level === "bad" ? "crit" : level === "warn" ? "warn" : "ok");
  el.style.display = "block";

  clearTimeout(el._t);
  el._t = setTimeout(() => {
    el.style.display = "none";
  }, ms);
}

function followJob(name) {
  setSelectedJob(name);
}

function setStatus(el, ok, text) {
  el.textContent = text;
  el.className = "status " + (ok ? "ok" : "bad");
}

async function fetchJSON(path) {
  const res = await fetch(path, { cache: "no-store" });
  const txt = await res.text();
  let data = null;
  try { data = txt ? JSON.parse(txt) : null; } catch {}
  if (!res.ok) {
    const msg = (data && data.error) ? data.error : txt;
    throw new Error(`${res.status} ${res.statusText}: ${msg}`);
  }
  return data;
}

async function postJSON(path, obj) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(obj || {}),
    cache: "no-store",
  });
  const txt = await res.text();
  let data = null;
  try { data = txt ? JSON.parse(txt) : null; } catch {}
  if (!res.ok) {
    const msg = (data && data.error) ? data.error : txt;
    throw new Error(`${res.status} ${res.statusText}: ${msg}`);
  }
  return data;
}

function escapeHTML(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// -----------------------------
// Mode + visibility helpers
// -----------------------------
function applyModeToDOM() {
  document.body.classList.toggle("mode-operator", !!OPERATOR_MODE);
  document.body.classList.toggle("mode-expert", !OPERATOR_MODE);
  const legacy = document.getElementById("alerts");
  if (legacy) legacy.style.display = OPERATOR_MODE ? "none" : "";

  document.body.classList.toggle("expert-unlocked", !!EXPERT_UNLOCK);

  const b = document.getElementById("btnOperatorMode");
  if (b) b.textContent = `👷 Operator Mode: ${OPERATOR_MODE ? "ON" : "OFF"}`;
}

function _setExpertUnlock(on) {
  EXPERT_UNLOCK = !!on;
  localStorage.setItem(_EXPERT_UNLOCK_KEY, EXPERT_UNLOCK ? "1" : "0");
  applyModeToDOM();

  const btn = document.getElementById("btnExpertUnlock");
  if (btn) btn.textContent = `🛡 Unlock Advanced: ${EXPERT_UNLOCK ? "ON" : "OFF"}`;
}

// -----------------------------
// Decision bar helpers
// -----------------------------
function _setPill(id, text, cls) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = text;
  el.className = `pill clickable ${cls || "dim"}`;
}

function _jumpToCard(titleContains) {
  const cards = Array.from(document.querySelectorAll(".card"));
  const hit = cards.find((c) => (c.querySelector("h2")?.textContent || "").includes(titleContains));
  if (hit) hit.scrollIntoView({ behavior: "smooth", block: "start" });
}

function _severityRank(s) {
  if (!s) return 0;
  if (s === "CRIT") return 3;
  if (s === "WARN") return 2;
  if (s === "INFO") return 1;
  return 0;
}

function _parseRangeToMs(r) {
  const map = { "15m": 15*60e3, "1h": 60*60e3, "6h": 6*60*60e3, "24h": 24*60*60e3, "7d": 7*24*60*60e3 };
  return map[r] || map["6h"];
}

function _getGlobalFilters() {
  const range = (document.getElementById("globalRange")?.value || "6h").trim();
  const sev = (document.getElementById("globalSev")?.value || "WARN").trim();
  const sym = (document.getElementById("globalSymbol")?.value || "").trim().toUpperCase();
  const changedOnly = !!document.getElementById("globalChangedOnly")?.checked;
  return { range, sev, sym, changedOnly };
}

// local ack/snooze (UI-only) to reduce noise for operators
const _ACK_KEY = "ui_ack_map";
const _SNOOZE_KEY = "ui_snooze_map";
function _loadMap(key) {
  try { return JSON.parse(localStorage.getItem(key) || "{}") || {}; } catch { return {}; }
}
function _saveMap(key, obj) {
  try { localStorage.setItem(key, JSON.stringify(obj || {})); } catch {}
}
function _ackAlertLocal(id) {
  const m = _loadMap(_ACK_KEY);
  m[String(id)] = Date.now();
  _saveMap(_ACK_KEY, m);
}
function _snoozeAlertLocal(id, minutes) {
  const m = _loadMap(_SNOOZE_KEY);
  m[String(id)] = Date.now() + (minutes * 60 * 1000);
  _saveMap(_SNOOZE_KEY, m);
}
function _isSnoozedLocal(id) {
  const m = _loadMap(_SNOOZE_KEY);
  const until = Number(m[String(id)] || 0);
  if (!until) return false;
  if (Date.now() > until) {
    delete m[String(id)];
    _saveMap(_SNOOZE_KEY, m);
    return false;
  }
  return true;
}
function _isAckedLocal(id) {
  const m = _loadMap(_ACK_KEY);
  return !!m[String(id)];
}

function _debounce(fn, ms=120) {
  let t;
  return (...a) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...a), ms);
  };
}

// -----------------------------
// Mini sparkline (tiny canvas)
// -----------------------------
function drawSpark(canvas, values) {
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const w = canvas.width = 86;
  const h = canvas.height = 18;
  ctx.clearRect(0,0,w,h);

  const arr = (values || []).map(Number).filter((x)=>Number.isFinite(x));
  if (arr.length < 2) return;

  let mn = Math.min(...arr);
  let mx = Math.max(...arr);
  if (mn === mx) { mn -= 1; mx += 1; }

  const pad = 2;
  const sx = (i) => pad + (i * (w - pad*2) / (arr.length - 1));
  const sy = (v) => h - pad - ((v - mn) * (h - pad*2) / (mx - mn));

  // baseline
  ctx.globalAlpha = 0.35;
  ctx.beginPath();
  ctx.moveTo(pad, sy(0));
  ctx.lineTo(w - pad, sy(0));
  ctx.stroke();
  ctx.globalAlpha = 1.0;

  // line
  ctx.beginPath();
  for (let i=0;i<arr.length;i++){
    const x = sx(i);
    const y = sy(arr[i]);
    if (i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
  }
  ctx.stroke();
}

// -----------------------------
// Heatmap + incident queue rendering
// -----------------------------
function _scoreCell(rows) {
  // severity * confidence * |z|
  let best = null;
  for (const r of rows) {
    const sevR = _severityRank(r.severity);
    const conf = Number(r.confidence);
    const z = Math.abs(Number(r.expected_z));
    if (!Number.isFinite(conf) || !Number.isFinite(z)) continue;
    const score = sevR * conf * (0.6 + Math.min(3.0, z));
    if (!best || score > best.score) best = { score, r };
  }
  return best ? best.r : null;
}

function _cellColor(r) {
  if (!r) return { cls:"dim", sw:"#2f3640" };
  if (r.resolved) return { cls:"ok", sw:"#2ea043" };
  if (r.severity === "CRIT") return { cls:"crit", sw:"#ff6b6b" };
  if (r.severity === "WARN") return { cls:"warn", sw:"#d29922" };
  return { cls:"ok", sw:"#58a6ff" };
}

function renderHeatmap(rows) {
  const host = document.getElementById("alertsHeatmap");
  if (!host) return;

  const horizons = [ "60", "300", "3600", "14400" ]; // 1m, 5m, 1h, 4h buckets (seconds) — works with your data
  const horizonLabels = { "60":"1m", "300":"5m", "3600":"1h", "14400":"4h" };

  // group by symbol + nearest horizon bucket
  const by = {};
  for (const r of rows || []) {
    const sym = String(r.symbol || "").toUpperCase();
    if (!sym) continue;
    const hs = Number(r.horizon_s);
    if (!Number.isFinite(hs)) continue;

    // bucket horizon to one of the columns
    let b = "3600";
    if (hs <= 120) b = "60";
    else if (hs <= 900) b = "300";
    else if (hs <= 7200) b = "3600";
    else b = "14400";

    by[sym] = by[sym] || {};
    by[sym][b] = by[sym][b] || [];
    by[sym][b].push(r);
  }

  const syms = Object.keys(by).sort((a,b)=>a.localeCompare(b)).slice(0, 22);

  host.innerHTML = "";
  const mk = (cls, txt, extra) => {
    const d = document.createElement("div");
    d.className = `hmCell ${cls||""}`;
    if (extra) Object.assign(d, extra);
    d.innerHTML = txt;
    return d;
  };

  // header row
  host.appendChild(mk("hmCell hmHead", `<span class="mono">symbol</span>`));
  for (const h of horizons) {
    host.appendChild(mk("hmCell hmHead", `<span class="mono">${horizonLabels[h] || (h+"s")}</span>`));
  }

  for (const sym of syms) {
    // symbol label
    host.appendChild(mk("hmCell hmSym", `<span class="mono">${esc(sym)}</span>`));

    for (const h of horizons) {
      const best = _scoreCell((by[sym] && by[sym][h]) ? by[sym][h] : []);
      const c = _cellColor(best);
      const z = best ? Number(best.expected_z) : 0;
      const conf = best ? Number(best.confidence) : 0;

      const sw = `<span class="hmSwatch" style="background:${c.sw};"></span>`;
      const meta = best ? `<span class="hmMeta">${best.severity} • z=${z.toFixed(2)} • c=${conf.toFixed(2)}</span>` : `<span class="hmMeta">—</span>`;
      const cell = mk("hmCell", `${sw}<span class="mono">${esc(sym)}</span>${meta}`);

      cell.addEventListener("click", () => {
        document.getElementById("globalSymbol").value = sym;
        document.getElementById("globalSev").value = "ALL";
        _jumpToCard("Alerts");
        // rerender happens on next refresh tick; do now too:
        renderIncidentQueue(_filterAlerts(_lastAlerts || []));
      });

      host.appendChild(cell);
    }
  }
}

function _meaningForAlert(r) {
  const sym = (r.symbol === "EXECUTION") ? "Execution" : r.symbol;
  if (sym === "EXECUTION") return "Execution quality looks degraded. Treat signals cautiously; avoid amplifying with aggressive actions.";
  if (r.severity === "CRIT") return "This is high severity and likely needs attention now.";
  if (r.severity === "WARN") return "This is a warning. Check context and monitor for escalation.";
  return "Informational alert. Usually safe to monitor.";
}

function _recommendedPosture(r) {
  if (r.severity === "CRIT") return "Act now";
  if (r.severity === "WARN") return "Monitor closely";
  return "Observe only";
}

function _decisionConfidence(r) {
  const c = Number(r.confidence);
  const z = Math.abs(Number(r.expected_z));
  if (c >= 0.85 && z >= 1.5) return "High confidence decision";
  if (c >= 0.65) return "Moderate confidence decision";
  return "Low confidence decision";
}

function _safeToIgnore(r) {
  if (r.severity === "INFO") return "Yes — informational";
  if (r.severity === "WARN" && Number(r.confidence) < 0.6) return "Likely safe short-term";
  return "No";
}

function _ifNothingChanges(r) {
  if (r.severity === "CRIT")
    return "Likely escalation or downstream impact within this horizon.";
  if (r.severity === "WARN")
    return "May self-resolve, but repeated alerts increase risk.";
  return "No material impact expected.";
}

function _stepsForAlert(r) {
  const sym = (r.symbol === "EXECUTION") ? "Execution" : r.symbol;
  if (sym === "EXECUTION") {
    return [
      "Check broker connectivity/latency and fill slippage.",
      "Confirm data freshness (prices/labels).",
      "If degradation persists, avoid risky actions (pipeline/promotion) until stable."
    ];
  }
  return [
    "Open Why for context (signals + priors).",
    "Confirm data freshness and drift status.",
    "If repeated, investigate symbol-specific execution costs."
  ];
}

function _renderSteps(list) {
  return `<ol style="margin:0; padding-left: 18px;">${(list||[]).map(s=>`<li>${esc(s)}</li>`).join("")}</ol>`;
}

function _findSimilarAlerts(row, all) {
  return (all || [])
    .filter(a =>
      a.id !== row.id &&
      a.symbol === row.symbol &&
      a.severity === row.severity
    )
    .slice(0, 3);
}

async function openIncidentDrawer(row) {
    // expose current incident for voice / assistive inputs
  window.__ACTIVE_INCIDENT__ = row;

  const overlay = document.getElementById("incidentOverlay");
  if (!overlay) return;

  // fetch richer details when possible
  let alertRow = row;
  let ex = null;
  try {
    const res = await fetchJSON(`/api/alerts/by_id?id=${encodeURIComponent(row.id)}`);
    if (res && res.ok && res.alert) {
      alertRow = res.alert;
      ex = alertRow.explain_json ? JSON.parse(alertRow.explain_json) : null;
    }
  } catch {}

  const title = document.getElementById("drawerTitle");
  const sub = document.getElementById("drawerSubtitle");
  const meaning = document.getElementById("drawerMeaning");
  const steps = document.getElementById("drawerSteps");
  const facts = document.getElementById("drawerFacts");
  const raw = document.getElementById("drawerRaw");
  const posture = document.getElementById("drawerPosture");
  const decision = document.getElementById("drawerDecision");
  const ignore = document.getElementById("drawerIgnore");
  const future = document.getElementById("drawerFuture");

  if (title) title.textContent = `${alertRow.severity} • ${alertRow.symbol} • h=${alertRow.horizon_s}s`;
  if (sub) sub.textContent = `${alertRow.event_title} • ${fmtTime(alertRow.ts_ms)}`;
  if (meaning) meaning.textContent = _meaningForAlert(alertRow);
  if (steps) steps.innerHTML = _renderSteps(_stepsForAlert(alertRow));

  if (posture) posture.textContent = _recommendedPosture(alertRow);
  if (decision) decision.textContent = _decisionConfidence(alertRow);
  if (ignore) ignore.textContent = _safeToIgnore(alertRow);
  if (future) future.textContent = _ifNothingChanges(alertRow);

  if (facts) {
    const z = Number(alertRow.expected_z);
    const conf = Number(alertRow.confidence);
    const ageMin = Math.max(0, Math.floor((Date.now() - Number(alertRow.ts_ms)) / 60000));
    facts.innerHTML = `
      <div class="kvK">symbol</div><div class="kvV">${esc(alertRow.symbol)}</div>
      <div class="kvK">severity</div><div class="kvV">${esc(alertRow.severity)}</div>
      <div class="kvK">horizon_s</div><div class="kvV">${esc(alertRow.horizon_s)}</div>
      <div class="kvK">expected_z</div><div class="kvV">${Number.isFinite(z) ? z.toFixed(3) : "—"}</div>
      <div class="kvK">confidence</div><div class="kvV">${Number.isFinite(conf) ? conf.toFixed(2) : "—"}</div>
      <div class="kvK">age</div><div class="kvV">${ageMin}m</div>
      <div class="kvK">reason</div><div class="kvV">${esc(alertRow.reason || "")}</div>
    `;
  }

const similar = _findSimilarAlerts(alertRow, _lastAlerts);
const simEl = document.getElementById("drawerSimilar");
if (simEl) {
  simEl.innerHTML = similar.length
    ? similar.map(s =>
        `<div class="small mono">${fmtTime(s.ts_ms)} • z=${Number(s.expected_z).toFixed(2)} • c=${Number(s.confidence).toFixed(2)}</div>`
      ).join("")
    : "<div class='small'>No similar recent incidents</div>";
}

  if (raw) {
    if (ex) raw.textContent = JSON.stringify(ex, null, 2);
    else raw.textContent = "(no explain_json on this alert)";
  }

  overlay.style.display = "block";
}

function closeIncidentDrawer() {
  const overlay = document.getElementById("incidentOverlay");
  if (overlay) overlay.style.display = "none";
}

function _filterAlerts(rows) {
  const f = _getGlobalFilters();
  const minRank = (f.sev === "ALL") ? 0 : _severityRank(f.sev);
  const sinceMs = Date.now() - _parseRangeToMs(f.range);

  const out = [];
  for (const r of (rows || [])) {
    const ts = Number(r.ts_ms);
    if (Number.isFinite(ts) && ts < sinceMs) continue;

    const sym = String(r.symbol || "").toUpperCase();
    if (f.sym && sym !== f.sym) continue;

    if (_severityRank(r.severity) < minRank) continue;

    if (f.changedOnly) {
      // "changed only": hide resolved, hide local-acked, hide snoozed
      if (r.resolved) continue;
      if (_isAckedLocal(r.id)) continue;
      if (_isSnoozedLocal(r.id)) continue;
    } else {
      // always hide snoozed from operator list
      if (_isSnoozedLocal(r.id)) continue;
    }

    out.push(r);
  }
  return out;
}

function renderIncidentQueue(rows) {
  const host = document.getElementById("incidentList");
  if (!host) return;

  host.innerHTML = "";
  const perSymZ = {};
  for (const r of rows || []) {
    const sym = String(r.symbol || "").toUpperCase();
    perSymZ[sym] = perSymZ[sym] || [];
    perSymZ[sym].push(Number(r.expected_z));
    if (perSymZ[sym].length > 10) perSymZ[sym].shift();
  }

  // sort: CRIT first, then WARN, then newest
  const sorted = (rows || []).slice().sort((a,b)=>{
    const sa = _severityRank(a.severity), sb = _severityRank(b.severity);
    if (sb !== sa) return sb - sa;
    return Number(b.ts_ms) - Number(a.ts_ms);
  }).slice(0, 18);

  if (sorted.length === 0) {
    host.innerHTML = `<div class="small" style="color:var(--muted);">No alerts in the selected window.</div>`;
    return;
  }

  for (const r of sorted) {
    const ageMin = Math.max(0, Math.floor((Date.now() - Number(r.ts_ms)) / 60000));
    const z = Number(r.expected_z);
    const conf = Number(r.confidence);
    const c = _cellColor(r);

    const item = document.createElement("div");
    item.className = "incidentItem";
    item.innerHTML = `
      <div class="incidentTop">
        <span class="pill ${c.cls}">${r.resolved ? "RESOLVED" : (r.severity || "INFO")}</span>
        <div class="incidentTitle">${esc(r.symbol)} • ${esc(r.event_title)}</div>
        <div class="incidentActions">
          <button class="btn btnSmall" data-ack="${Number(r.id)}">${_isAckedLocal(r.id) ? "Acked" : "Ack"}</button>
          <button class="btn btnSmall" data-snooze="${Number(r.id)}">Snooze 30m</button>
          <button class="btn btnSmall" data-open="${Number(r.id)}">Open</button>
        </div>
      </div>
      <div class="incidentSub">
        <span class="pill dim">h=${esc(r.horizon_s)}s</span>
        <span class="pill dim">z=${Number.isFinite(z) ? z.toFixed(2) : "—"}</span>
        <span class="pill dim">c=${Number.isFinite(conf) ? conf.toFixed(2) : "—"}</span>
        <span class="pill dim">${ageMin}m ago</span>
        <canvas class="spark" data-spark="${esc(String(r.symbol||"").toUpperCase())}"></canvas>
      </div>
      ${r.reason ? `<div class="small" style="margin-top:8px; color:var(--muted);">${esc(r.reason)}</div>` : ""}
    `;

    // wire buttons
    item.querySelectorAll("button[data-ack]").forEach((b)=>{
      b.addEventListener("click", (e)=>{
        e.stopPropagation();
        _ackAlertLocal(b.getAttribute("data-ack"));
        renderIncidentQueue(_filterAlerts(_lastAlerts || []));
      });
    });
    item.querySelectorAll("button[data-snooze]").forEach((b)=>{
      b.addEventListener("click", (e)=>{
        e.stopPropagation();
        _snoozeAlertLocal(b.getAttribute("data-snooze"), 30);
        renderIncidentQueue(_filterAlerts(_lastAlerts || []));
      });
    });
    item.querySelectorAll("button[data-open]").forEach((b)=>{
      b.addEventListener("click", async (e)=>{
        e.stopPropagation();
        await openIncidentDrawer(r);
      });
    });

    // click row opens drawer
    item.addEventListener("click", async ()=>{ await openIncidentDrawer(r); });

    host.appendChild(item);
  }

  // draw sparklines
  host.querySelectorAll("canvas[data-spark]").forEach((c)=>{
    const sym = c.getAttribute("data-spark");
    drawSpark(c, perSymZ[sym] || []);
  });
}

function updateDecisionBarFromState(state) {
  // state: { system, crit, warn, data, model, exec, updated }
  _setPill("pillSystem", `SYSTEM: ${state.system}`, state.system === "CRIT" ? "crit" : state.system === "WARN" ? "warn" : "ok");
  _setPill("pillCrit", `CRIT: ${state.crit}`, state.crit > 0 ? "crit" : "dim");
  _setPill("pillWarn", `WARN: ${state.warn}`, state.warn > 0 ? "warn" : "dim");
  _setPill("pillData", `Data: ${state.data}`, state.data === "BAD" ? "crit" : state.data === "WARN" ? "warn" : "ok");
  _setPill("pillModel", `Model: ${state.model}`, state.model === "BLOCKED" ? "warn" : "ok");
  _setPill("pillExec", `Exec: ${state.exec}`, state.exec === "DEGRADED" ? "warn" : "ok");

  const up = document.getElementById("pillUpdated");
  if (up) up.textContent = `Updated: ${state.updated}`;

  // jump wiring
  document.getElementById("pillSystem")?.addEventListener("click", ()=>_jumpToCard("System Health"));
  document.getElementById("pillCrit")?.addEventListener("click", ()=>_jumpToCard("Alerts"));
  document.getElementById("pillWarn")?.addEventListener("click", ()=>_jumpToCard("Alerts"));
  document.getElementById("pillData")?.addEventListener("click", ()=>_jumpToCard("System Health"));
  document.getElementById("pillModel")?.addEventListener("click", ()=>_jumpToCard("Promotions"));
  document.getElementById("pillExec")?.addEventListener("click", ()=>_jumpToCard("Execution"));
}

// -----------------------------
// Existing code continues
// -----------------------------
function fmtTime(ms) {

  try { return new Date(Number(ms)).toLocaleTimeString(); } catch { return ""; }
}

function setPill(id, ok, text) {
  const el = document.getElementById(id);
  if (!el) return;
  el.className = "pill " + (ok ? "ok" : "bad");
  el.textContent = text;
}

async function openPromotionExplainModal() {
  const modal = document.getElementById("promoModal");
  const pre = document.getElementById("promoBody");
  if (!modal || !pre) return;

  modal.style.display = "block";
  pre.textContent = "loading…";

  try {
    const data = await fetchJSON("/api/promotion/explain");
    pre.textContent = JSON.stringify(data, null, 2);
  } catch (e) {
    pre.textContent = `error: ${String(e && e.message ? e.message : e)}`;
  }
}

function wirePromotionExplainUI() {
  const btn = document.getElementById("btnWhyNotPromoted");
  const close = document.getElementById("btnClosePromoModal");
  const modal = document.getElementById("promoModal");

  if (btn) btn.addEventListener("click", () => openPromotionExplainModal());
  if (close) close.addEventListener("click", () => { if (modal) modal.style.display = "none"; });

  if (modal) {
    modal.addEventListener("click", (e) => {
      if (e && e.target === modal) modal.style.display = "none";
    });
  }
}


let selectedJob = "poll_prices";

// Operator mode defaults ON for guided ops clarity.
// If user has never set it, force ON once.
if (localStorage.getItem("operator_mode") == null) {
  localStorage.setItem("operator_mode", "1");
}
let OPERATOR_MODE =
  localStorage.getItem("operator_mode") === "1";

// Expert unlock (separate from Operator Mode): reveals risky actions while staying operator-friendly
const _EXPERT_UNLOCK_KEY = "expert_unlock";
let EXPERT_UNLOCK = localStorage.getItem(_EXPERT_UNLOCK_KEY) === "1";

let _killSwitchSnapshot = null;


function setSelectedJob(name) {
  selectedJob = name;
  const el = document.getElementById("selectedJob");
  if (el) el.textContent = name;
}

function setJobStatusPill(running, exitCode) {
  const el = document.getElementById("jobStatus");
  if (!el) return;
  if (running) {
    el.className = "pill ok";
    el.textContent = "running";
  } else {
    el.className = "pill dim";
    el.textContent = (exitCode === null || exitCode === undefined) ? "idle" : `exited rc=${exitCode}`;
  }
}

function setJobButtonState(name, state) {
  document.querySelectorAll(`button[data-job="${name}"]`).forEach(btn => {
    btn.disabled = (state === "running");
    btn.classList.toggle("job-running", state === "running");
    btn.classList.toggle("job-error", state === "error");

    let label = btn.getAttribute("data-label") || btn.textContent.trim();
    if (!btn.getAttribute("data-label")) btn.setAttribute("data-label", label);

    if (state === "running") btn.textContent = `⏳ ${label}`;
    else if (state === "error") btn.textContent = `❌ ${label}`;
    else btn.textContent = label;
  });
}

function barWidth(pct) {
  const v = Math.max(0, Math.min(100, pct));
  return `${v.toFixed(1)}%`;
}

// ---------- Execution degradation detection ----------

const _EXEC_CONF_BASELINE_KEY = "exec_conf_baseline_v2";
const _EXEC_CONF_STATE_KEY    = "exec_conf_state_v2";

function _detectExecutionDegradation(rows) {
  if (!Array.isArray(rows) || !rows.length) return [];

  const stateRaw = localStorage.getItem(_EXEC_CONF_STATE_KEY);
  const state = stateRaw ? JSON.parse(stateRaw) : {};

  const now = Date.now();
  const alerts = [];

  // group by symbol
  const bySym = {};
  for (const r of rows) {
    if (
      Number(r.conf_lo) >= 0.75 &&
      Number(r.n || 0) >= 20 &&
      Number.isFinite(Number(r.mean_cost))
    ) {
      const sym = r.symbol || "GLOBAL";
      (bySym[sym] ||= []).push(r);
    }
  }

  for (const sym of Object.keys(bySym)) {
    const avgCost =
      bySym[sym].reduce((a, r) => a + Number(r.mean_cost), 0) /
      bySym[sym].length;

    const s = state[sym] || {
      baseline: avgCost,
      degraded_since: null,
      level: "OK",
      acked: false,
    };

    // initialize baseline
    if (!Number.isFinite(s.baseline)) {
      s.baseline = avgCost;
      state[sym] = s;
      continue;
    }

    const worsenPct = (avgCost - s.baseline) / Math.abs(s.baseline || 1e-9);

    // -------- DEGRADED --------
    if (worsenPct > 0.3) {
      if (!s.degraded_since) s.degraded_since = now;

      const ageMin = (now - s.degraded_since) / 60000;

      // escalate to CRIT after 30 minutes
      if (ageMin >= 30) s.level = "CRIT";
      else s.level = "WARN";

      s.acked = false;

      alerts.push({
        symbol: sym,
        level: s.level,
        prev: s.baseline,
        cur: avgCost,
        worsenPct,
        ageMin,
      });
    }

    // -------- RECOVERY --------
    else if (avgCost <= s.baseline * 1.05) {
      if (s.level !== "OK") {
        s.level = "OK";
        s.acked = true;
        s.degraded_since = null;

        toast(
          `Execution recovered for ${sym}`,
          "ok",
          2500
        );
      }

      // slowly adapt baseline downward
      s.baseline = s.baseline * 0.8 + avgCost * 0.2;
    }

    state[sym] = s;
  }

  localStorage.setItem(_EXEC_CONF_STATE_KEY, JSON.stringify(state));
  return alerts;
}

function _safeParseJSON(s) {
  try { return JSON.parse(String(s)); } catch { return null; }
}

function _ALERT_SNAPSHOT_KEY(id) {
  return `alert_snapshot_${String(id)}`;
}

// ---------- Relevance helpers ----------

const _RELEVANCE_SNAPSHOT_KEY = "relevance_stats_snapshot";

function _parseRelevanceStats(stats) {
  const rows = [];

  if (!stats || typeof stats !== "object") return rows;

  for (const k of Object.keys(stats)) {
    const v = stats[k] || {};
    let symbol = k;
    let horizon = "";

    if (k.includes(":")) {
      const parts = k.split(":");
      symbol = parts[0];
      horizon = parts[1];
    }

    rows.push({
      key: k,
      symbol,
      horizon,
      relevance: Number(v.relevance ?? v.value ?? NaN),
      mean_abs_z: Number(v.mean_abs_z ?? v.abs_z ?? NaN),
      n: Number(v.n ?? NaN),
    });
  }

  return rows;
}

function _diffRelevance(prev, cur) {
  const out = [];
  const p = prev || {};
  const c = cur || {};

  const keys = new Set([...Object.keys(p), ...Object.keys(c)]);
  for (const k of keys) {
    if (!p[k]) {
      out.push(`+ ${k} added`);
      continue;
    }
    if (!c[k]) {
      out.push(`- ${k} removed`);
      continue;
    }

    const fields = ["relevance", "mean_abs_z", "n"];
    for (const f of fields) {
      const pv = p[k]?.[f];
      const cv = c[k]?.[f];
      if (Number.isFinite(pv) && Number.isFinite(cv) && Math.abs(pv - cv) > 1e-6) {
        out.push(`${k}.${f}: ${pv} → ${cv}`);
      }
    }
  }
  return out;
}

function _diffObjects(prev, cur, path = "") {
  const out = [];
  const p = (prev && typeof prev === "object") ? prev : {};
  const c = (cur && typeof cur === "object") ? cur : {};

  const keys = new Set([...Object.keys(p || {}), ...Object.keys(c || {})]);
  for (const k of keys) {
    const pp = path ? `${path}.${k}` : k;
    const pv = p ? p[k] : undefined;
    const cv = c ? c[k] : undefined;

    const pvObj = pv && typeof pv === "object";
    const cvObj = cv && typeof cv === "object";

    if (pvObj || cvObj) {
      // if arrays or objects, recurse shallowly
      if (Array.isArray(pv) || Array.isArray(cv)) {
        const a = JSON.stringify(pv || []);
        const b = JSON.stringify(cv || []);
        if (a !== b) out.push(`${pp}: changed`);
      } else {
        out.push(..._diffObjects(pv || {}, cv || {}, pp));
      }
      continue;
    }

    if (pv !== cv) out.push(`${pp}: ${String(pv)} -> ${String(cv)}`);
  }
  return out;
}

async function openWhyModal(alertRow) {
  const modal = document.getElementById("whyModal");
  const pre = document.getElementById("whyBody");
  const title = document.getElementById("whyTitle");
  if (!modal || !pre || !title) return;

  let ex = null;
  try {
    const res = await fetchJSON(`/api/alerts/by_id?id=${encodeURIComponent(alertRow.id)}`);
    if (res && res.ok && res.alert) {
      ex = res.alert.explain_json ? JSON.parse(res.alert.explain_json) : null;
      alertRow = res.alert;
    }
  } catch {}

  title.textContent = `Why: ${alertRow.symbol} h=${alertRow.horizon_s}s ${alertRow.severity}`;

  if (!ex) {
    pre.textContent =
      "(no explain data on this alert yet)\n\nRun process_events again after updating predictor.py + alerts.py.";
    _pauseRefresh = true;
    toast("Paused auto-refresh (Why panel open)", "dim");

    modal.style.display = "block";
    return;
  }

  let out = "";
  out += `event: ${alertRow.event_title}\n`;
  out += `symbol: ${alertRow.symbol}\n`;
  out += `horizon_s: ${alertRow.horizon_s}\n`;
  out += `expected_z: ${Number(alertRow.expected_z).toFixed(3)}\n`;
  out += `confidence: ${Number(alertRow.confidence).toFixed(2)}\n`;
  if (typeof ex.confidence_base === "number") {
    out += `confidence_base: ${Number(ex.confidence_base).toFixed(2)}\n`;
  }
  out += `rule_id: ${alertRow.rule_id}\n`;
  out += "\n";

  if (ex.rule) {
    out += "=== RULE ===\n";
    out += `regime=${ex.rule.regime || ""}\n`;
    out += `min_abs_z_resolved=${Number(ex.rule.min_abs_z_resolved || 0).toFixed(3)}\n`;
    out += `min_conf=${Number(ex.rule.min_conf || 0).toFixed(2)}\n`;
    out += `support_n=${ex.rule.support_n ?? ""}\n`;
    out += "\n";
  }

  if (typeof ex.relevance === "number") {
    out += "=== RELEVANCE (P2/P3) ===\n";
    out += `relevance=${ex.relevance.toFixed(3)}\n`;
    if (typeof ex.relevance_heuristic === "number") {
      out += `heuristic=${ex.relevance_heuristic.toFixed(3)}\n`;
    }
    if (typeof ex.relevance_learned === "number") {
      out += `learned=${ex.relevance_learned.toFixed(3)} (n=${ex.relevance_learned_n ?? 0}, abs_z=${ex.relevance_learned_abs_z ?? ""})\n`;
    }
    if (typeof ex.relevance_blend_alpha === "number") {
      out += `blend_alpha=${ex.relevance_blend_alpha.toFixed(3)}\n`;
    }
    if (ex.relevance_reasons && Array.isArray(ex.relevance_reasons)) {
      for (const r of ex.relevance_reasons) out += `- ${r}\n`;
    }
    out += "\n";
  }

  if (ex.learned_relevance) {
    out += "=== LEARNED RELEVANCE (CONF SCALING) ===\n";
    out += `value=${Number(ex.learned_relevance.value || 0).toFixed(3)} n=${ex.learned_relevance.n || 0}\n`;
    out += `abs_z_threshold=${ex.learned_relevance.abs_z_threshold}\n`;
    out += `conf_multiplier=${Number(ex.learned_relevance.conf_multiplier || 0).toFixed(3)} applied=${!!ex.learned_relevance.applied}\n`;
    out += "\n";
  }

  if (ex.knn) {
    out += "=== kNN EVIDENCE (top-k neighbors) ===\n";
    out += `top_k=${ex.knn.top_k} used=${ex.knn.used} weight_sum=${Number(ex.knn.weight_sum || 0).toFixed(3)}\n`;
    if (Array.isArray(ex.knn.neighbors)) {
      for (const n of ex.knn.neighbors) {
        const sim = (n.sim !== undefined) ? Number(n.sim).toFixed(3) : "";
        const decay = (n.decay !== undefined) ? Number(n.decay).toFixed(3) : "";
        const w = (n.weight !== undefined) ? Number(n.weight).toFixed(3) : "";
        const z = (n.impact_z !== undefined) ? Number(n.impact_z).toFixed(3) : "";
        const age = (n.age_days !== undefined) ? Number(n.age_days).toFixed(1) : "";
        out += `- event_id=${n.event_id} sim=${sim} decay=${decay} age_days=${age} w=${w} impact_z=${z}\n`;
      }
    }
    out += "\n";
  }

  if (ex.spillover) {
    out += "=== SPILLOVER (cross-asset contribution) ===\n";
    out += `enabled=${!!ex.spillover.enabled} used=${ex.spillover.used || 0} adj=${Number(ex.spillover.adj || 0).toFixed(3)}\n`;
    if (Array.isArray(ex.spillover.contributions)) {
      for (const c of ex.spillover.contributions) {
        out += `- ${c.driver} beta=${Number(c.beta).toFixed(3)} z_driver=${Number(c.z_driver).toFixed(3)} conf_driver=${Number(c.conf_driver).toFixed(2)} contrib=${Number(c.contrib).toFixed(3)}\n`;
      }
    }
    out += "\n";
  }

  // --- TEMPORAL SHADOW (shadow vs baseline) ---
  if (ex.temporal_shadow && typeof ex.temporal_shadow === "object") {
    try {
      const ts = ex.temporal_shadow;
      const tz = Number(ts.predicted_z);
      const tc = Number(ts.confidence);

      out += "=== TEMPORAL SHADOW (shadow-only) ===\n";
      out += `shadow_expected_z=${Number.isFinite(tz) ? tz.toFixed(3) : "?"}\n`;
      out += `shadow_confidence=${Number.isFinite(tc) ? tc.toFixed(2) : "?"}\n`;

      // baseline (this alert)
      const bz = Number(alertRow.expected_z);
      const bc = Number(alertRow.confidence);
      if (Number.isFinite(bz) && Number.isFinite(tz)) {
        out += `delta_z (shadow - baseline)=${(tz - bz).toFixed(3)}\n`;
      }
      if (Number.isFinite(bc) && Number.isFinite(tc)) {
        out += `delta_conf (shadow - baseline)=${(tc - bc).toFixed(3)}\n`;
      }

      // tiny explain summary if present
      const tex = ts.explain;
      if (tex && typeof tex === "object") {
        if (tex.model_key_type || tex.model_key) {
          out += `shadow_model=${String(tex.model_key_type || "")}:${String(tex.model_key || "")}\n`;
        }
        if (tex.model_ts_ms) out += `shadow_model_ts=${fmtTime(tex.model_ts_ms)}\n`;
        if (tex.model_n !== undefined) out += `shadow_model_n=${tex.model_n}\n`;
      }
      out += "\n";
    } catch {}
  }

  // --- What changed (diff vs previous snapshot) ---
  try {
    const curObj = ex && typeof ex === "object" ? ex : null;

    const prevRaw = localStorage.getItem(_ALERT_SNAPSHOT_KEY(alertRow.id));
    const prevObj = prevRaw ? _safeParseJSON(prevRaw) : null;

    if (curObj && prevObj) {
      const diff = _diffObjects(prevObj, curObj);
      if (diff.length) {
        out = "=== WHAT CHANGED SINCE LAST VIEW ===\n" + diff.slice(0, 80).map(d => `- ${d}`).join("\n") + "\n\n" + out;
      }
    }

    if (curObj) localStorage.setItem(_ALERT_SNAPSHOT_KEY(alertRow.id), JSON.stringify(curObj));
  } catch {}

  pre.textContent = out;
  modal.style.display = "block";
}

function closeWhyModal() {
  const modal = document.getElementById("whyModal");
  if (modal) modal.style.display = "none";
  _pauseRefresh = false;
  toast("Auto-refresh resumed", "dim");
}

async function openPromoWhyModal() {
  const modal = document.getElementById("promoWhyModal");
  const pre = document.getElementById("promoWhyBody");
  const title = document.getElementById("promoWhyTitle");
  if (!modal || !pre || !title) return;

  _pauseRefresh = true;
  toast("Paused auto-refresh (Promotion explainer open)", "dim");

  title.textContent = "Why not promoted?";

  try {
    const st = await fetchJSON("/api/promotion/status");
    const reg = await fetchJSON("/api/model_registry?limit=25");

    let out = "";
    out += "=== PROMOTION STATUS ===\n";
    out += `allowed=${!!st.allowed}\n`;
    out += `training_allowed=${!!st.training_allowed}\n`;
    out += `promotion_enabled_db=${String(st.promotion_enabled_db ?? "?")}\n`;
    if (st.reason) out += `reason=${JSON.stringify(st.reason, null, 2)}\n`;
    out += "\n";

    const champ = reg && reg.champion ? reg.champion : null;
    const chall = reg && reg.challenger ? reg.challenger : null;

    out += "=== MODEL REGISTRY (champion vs challenger) ===\n";
    if (!champ) out += "champion: (none)\n";
    else out += `champion: kind=${champ.model_kind || "?"} rmse=${(champ.metrics && champ.metrics.rmse !== undefined) ? Number(champ.metrics.rmse).toFixed(6) : "?"}\n`;

    if (!chall) out += "challenger: (none)\n";
    else out += `challenger: kind=${chall.model_kind || "?"} rmse=${(chall.metrics && chall.metrics.rmse !== undefined) ? Number(chall.metrics.rmse).toFixed(6) : "?"}\n`;

    out += "\n";
    out += "=== WHAT THIS MEANS ===\n";
    if (!chall) {
      out += "- No challenger exists yet. Run challenger training/eval.\n";
    } else if (!st.allowed) {
      out += "- Promotions are currently BLOCKED by the server guard.\n";
      out += "- See the 'reason' section above for the exact block.\n";
    } else {
      out += "- Promotions are ALLOWED, so the only remaining blocker is usually:\n";
      out += "  • challenger not better than champion (RMSE / directional constraints)\n";
      out += "  • insufficient eval sample size\n";
      out += "  • execution degradation pause (UI may throttle promotions)\n";
    }

    pre.textContent = out;
    modal.style.display = "block";
  } catch (e) {
    pre.textContent = `[error] ${e.message}`;
    modal.style.display = "block";
  }
}

function closePromoWhyModal() {
  const modal = document.getElementById("promoWhyModal");
  if (modal) modal.style.display = "none";
  _pauseRefresh = false;
  toast("Auto-refresh resumed", "dim");
}


/* -----------------------------
   Loaders
----------------------------- */
// -----------------------------
// Portfolio backtest charts (equity + drawdown)
// Uses: GET /api/backtest/portfolio/latest
// -----------------------------

function _fmtMoney(x) {
  const v = Number(x || 0);
  const s = Math.abs(v) >= 1000 ? v.toFixed(0) : v.toFixed(2);
  return (v < 0 ? "-" : "") + "$" + String(s).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

function _computeEquityAndDD(points) {
  // points: [{ts_ms, pnl}] where pnl is cumulative PnL (per your server)
  const xs = (points || []).map(p => ({
    ts_ms: Number(p.ts_ms),
    pnl: Number(p.pnl || 0),
  })).filter(p => Number.isFinite(p.ts_ms) && Number.isFinite(p.pnl))
    .sort((a,b) => a.ts_ms - b.ts_ms);

  if (!xs.length) return { xs: [], equity: [], dd: [] };

  const equity = [];
  const dd = [];
  let peak = -Infinity;

  for (const p of xs) {
    const e = p.pnl; // if your pnl already includes starting capital, keep as-is
    equity.push(e);
    if (e > peak) peak = e;
    const d = peak > 0 ? (e - peak) / peak : (e - peak); // safe fallback
    dd.push(d); // negative or 0
  }

  return { xs, equity, dd };
}

function _drawLineChart(canvas, series, opts = {}) {
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);

  // background
  ctx.fillStyle = "#0a0d12";
  ctx.fillRect(0, 0, W, H);

  const pad = 12;
  const w = W - pad * 2;
  const h = H - pad * 2;

  // grid
  ctx.strokeStyle = "#30363d";
  ctx.lineWidth = 1;
  ctx.globalAlpha = 0.7;
  for (let i = 0; i <= 4; i++) {
    const y = pad + (h * i) / 4;
    ctx.beginPath();
    ctx.moveTo(pad, y);
    ctx.lineTo(pad + w, y);
    ctx.stroke();
  }
  ctx.globalAlpha = 1.0;

  const vals = (series || []).filter(Number.isFinite);
  if (!vals.length) {
    ctx.fillStyle = "#9da7b1";
    ctx.font = "12px Consolas, monospace";
    ctx.fillText("(no data)", pad, pad + 14);
    return;
  }

  let min = Math.min(...vals);
  let max = Math.max(...vals);
  if (min === max) { min -= 1; max += 1; }

  const yOf = (v) => pad + (max - v) * (h / (max - min));
  const xOf = (i) => pad + (i * (w / Math.max(1, vals.length - 1)));

  // line
  ctx.strokeStyle = (opts.stroke || "#2ea043");
  ctx.lineWidth = 2;
  ctx.beginPath();
  for (let i = 0; i < vals.length; i++) {
    const x = xOf(i);
    const y = yOf(vals[i]);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.stroke();

  // label
  ctx.fillStyle = "#9da7b1";
  ctx.font = "12px Consolas, monospace";
  if (opts.label) ctx.fillText(opts.label, pad, H - 6);
}

async function loadPortfolioBacktestLatest_legacy() {
  const meta = document.getElementById("portfolioBtMeta");
  const pre = document.getElementById("portfolioBtMetrics");
  const c1 = document.getElementById("equityCanvas");
  const c2 = document.getElementById("ddCanvas");

  // If the card isn't on this page, silently no-op
  if (!meta || !pre || !c1 || !c2) return;

  try {
    const j = await fetchJSON("/api/backtest/portfolio/latest");
    if (!j || !j.ok || !j.run) {
      meta.textContent = "none";
      meta.className = "pill dim";
      pre.textContent = (j && j.error) ? j.error : "(no backtest runs yet)";
      _drawLineChart(c1, [], { label: "equity" });
      _drawLineChart(c2, [], { label: "drawdown" });
      return;
    }

    meta.textContent = "ok";
    meta.className = "pill ok";

    const pts = Array.isArray(j.run.points) ? j.run.points : [];
    const { equity, dd } = _computeEquityAndDD(pts);

    // Equity curve (assumes pnl is cumulative; if it is “PnL only”, it’s still a valid curve)
    _drawLineChart(c1, equity, { label: `equity (last=${_fmtMoney(equity[equity.length - 1] || 0)})`, stroke: "#2ea043" });

    // Drawdown: show as % (negative)
    const ddPct = dd.map(x => Number.isFinite(x) ? (x * 100.0) : NaN);
    _drawLineChart(c2, ddPct, { label: "drawdown %", stroke: "#ff6b6b" });

    // Metrics
    const metrics = j.run.metrics || {};
    const lines = [];
    lines.push(`run_id: ${j.run.id}`);
    lines.push(`range: ${new Date(j.run.start_ts_ms).toLocaleString()} → ${new Date(j.run.end_ts_ms).toLocaleString()}`);
    const ordered = [
      "start_capital",
      "end_equity",
      "total_return",
      "max_drawdown",
      "ret_mean",
      "ret_volatility",
      "downside_volatility",
      "sharpe_simple",
      "sortino_simple",
      "calmar_simple",
      "turnover_avg",
      "turnover_total",
      "steps_used",
      "steps_skipped",
      "alerts_seen",
      "n_points",
    ];

for (const k of ordered) {
  if (!(k in metrics)) continue;
  const v = metrics[k];
  lines.push(`${k}: ${typeof v === "number" ? v.toFixed(6) : String(v)}`);
}

    pre.textContent = lines.join("\n") || "(no metrics)";
  } catch (e) {
    meta.textContent = "error";
    meta.className = "pill bad";
    pre.textContent = e.message;
  }
}

async function loadSizePolicy() {
  const body = document.getElementById("sizePolicyBody");
  const pill = document.getElementById("sizePolicyPill");
  if (!body || !pill) return;

  function fmt(x, k=6) {
    if (x === null || x === undefined || !Number.isFinite(Number(x))) return "";
    return Number(x).toFixed(k);
  }

  try {
    const j = await fetchJSON("/api/size_policy");
    if (!j || !j.ok) return;

    const p = j.policy;
    if (!p) {
      pill.textContent = "policy: none";
      pill.className = "pill bad";
      body.innerHTML = `<tr><td colspan="6" class="small">No policy yet. Train to enable.</td></tr>`;
      return;
    }

    pill.textContent = `policy: ${p.method} buckets=${p.buckets} lookback=${p.lookback_days}d`;
    pill.className = "pill " + (_isExecutionDegraded() ? "warn" : "ok");

    body.innerHTML = "";
    for (const r of (j.points || [])) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="mono">${r.bucket_idx}</td>
        <td class="mono">${fmt(r.conf_lo,2)}–${fmt(r.conf_hi,2)}</td>
        <td class="mono">${r.n}</td>
        <td class="mono">${fmt(r.mean_net_ret,6)}</td>
        <td class="mono">${fmt(r.std_net_ret,6)}</td>
        <td class="mono">${
  _isExecutionDegraded()
    ? Math.min(Number(r.factor), 0.5).toFixed(3) + " (throttled)"
    : fmt(r.factor,3)
}</td>
      `;
      body.appendChild(tr);
    }
  } catch (e) {
    // ignore
  }
}

// ------            -- ------------------------------------------------------
// Equity Drift Chart (Broker vs Backtest)
// Uses: GET /api/equity_drift
// ------            -- ------------------------------------------------------
async function loadEquityDrift() {
  const panel = document.getElementById("equityDriftPanel");
  const canvas = document.getElementById("equityDriftCanvas");
  const meta = document.getElementById("equityDriftMeta");

  if (!panel || !canvas || !meta) return;
  if (panel.style.display === "none") return;

  try {
    const res = await fetchJSON("/api/equity_drift?limit=500");
    if (!res || !res.ok || !Array.isArray(res.points)) {
      meta.textContent = "n/a";
      meta.className = "pill dim";
      _renderLineChart(canvas, []);
      return;
    }

    const pts = res.points;
    if (!pts.length) {
      meta.textContent = "empty";
      meta.className = "pill dim";
      _renderLineChart(canvas, []);
      return;
    }

    meta.textContent = "live";
    meta.className = "pill ok";

    // Plot % drift (preferred for scale)
    const ys = pts.map(p => Number(p.diff_equity_pct)).filter(Number.isFinite);

    _renderLineChart(canvas, ys, {
      topLabel: "equity drift (%)",
      fmtY: (v) => `${(v * 100).toFixed(2)}%`,
      stroke: "#d29922", // amber
      yMax: Math.max(0.01, Math.max(...ys, 0)),
      yMin: Math.min(-0.01, Math.min(...ys, 0)),
    });

  } catch (e) {
    meta.textContent = "error";
    meta.className = "pill bad";
    _renderLineChart(canvas, []);
  }
}

// ---------- Broker (paper execution) ----------
async function loadBroker() {
  const panel = document.getElementById("brokerPanel");
  const el = document.getElementById("brokerSnapshot");
  if (!panel || !el) return;
  if (panel.style.display === "none") return;

  try {
    const d = await fetchJSON("/api/broker");
    if (!d || !d.ok) {
      el.textContent = d && d.error ? d.error : "(broker not available)";
      return;
    }

    let out = "";
    out += `equity=${Number(d.account?.equity ?? 1).toFixed(3)} cash=${Number(d.account?.cash ?? 0).toFixed(3)}\n\n`;

    out += "=== POSITIONS ===\n";
    for (const p of (d.positions || [])) {
      out += `${p.symbol} qty=${Number(p.qty).toFixed(6)} avg_px=${Number(p.avg_px).toFixed(4)}\n`;
    }
    out += "\n=== FILLS (latest) ===\n";
    for (const f of (d.fills || []).slice(0, 20)) {
      out += `${fmtTime(f.ts_ms)} ${f.symbol} qty=${Number(f.qty).toFixed(6)} px=${Number(f.px).toFixed(4)} oid=${f.order_id ?? ""}\n`;
    }

    el.textContent = out || "(no broker data)";
  } catch (e) {
    el.textContent = `[error] ${e.message}`;
  }
}

// ---------- Execution cost by confidence ----------
async function loadExecutionByConfidence() {
  const body = document.getElementById("execByConfBody");
  if (!body) return;

  try {
    const j = await fetchJSON("/api/execution_metrics/by_confidence");
    const rows = (j && Array.isArray(j.rows)) ? j.rows : [];

    body.innerHTML = "";

    // ---- execution degradation detection ----
    const execAlerts = _detectExecutionDegradation(rows);
    for (const a of execAlerts) {
      toast(
        `${a.level}: execution degraded for ${a.symbol} (+${(a.worsenPct * 100).toFixed(1)}%)`,
        a.level === "CRIT" ? "bad" : "warn",
        a.level === "CRIT" ? 7000 : 5000
      );
      _emitExecutionDegradationAlert(a);
    }

    if (!rows.length) {
      body.innerHTML = `
        <tr>
          <td colspan="4" class="small">(no execution data yet)</td>
        </tr>
      `;
      return;
    }

    const maxAbs = Math.max(
      ...rows.map(r => Math.abs(Number(r.mean_cost || 0))),
      1e-9
    );

    for (const r of rows) {
      const lo = Number(r.conf_lo).toFixed(2);
      const hi = Number(r.conf_hi).toFixed(2);
      const n  = Number(r.n || 0);
      const c  = Number(r.mean_cost || 0);

      const severityPct = Math.min(100, (Math.abs(c) / maxAbs) * 100);

      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="mono">${lo}–${hi}</td>
        <td class="mono">${n}</td>
        <td class="mono">
          ${
            OPERATOR_MODE
              ? (c > 0 ? "Higher than expected" : "Better than expected")
              : c.toFixed(6)
          }
        </td>
        <td>
          <div style="
            height:10px;
            border-radius:999px;
            background:#0e1117;
            border:1px solid #30363d;
            overflow:hidden;
          ">
            <div style="
              height:100%;
              width:${severityPct.toFixed(1)}%;
              background:${c > 0 ? "#ff6b6b" : "#2ea043"};
            "></div>
          </div>
        </td>
      `;
      body.appendChild(tr);
    }
  } catch (e) {
    body.innerHTML = `
      <tr>
        <td colspan="4" class="small">error loading execution metrics</td>
      </tr>
    `;
  }
}

// ---------- Strategy Status ----------
async function loadStrategyStatus() {
  const panel = document.getElementById('strategyStatusPanel');
  const table = document.getElementById('strategyStatusTable');

  try {
    const r = await fetch('/api/strategy/status', { cache: 'no-store' });
    const j = await r.json();

    if (panel) panel.textContent = j && j.ok ? 'active' : 'idle';

    if (table && Array.isArray(j?.rows)) {
      table.innerHTML = '';
      j.rows.forEach(r => {
        table.insertAdjacentHTML('beforeend', `
          <tr>
            <td class="mono">${esc(r.key)}</td>
            <td class="mono">${esc(r.value)}</td>
          </tr>
        `);
      });
    }
  } catch {
    if (panel) panel.textContent = 'unavailable';
  }
}

// ---------- Portfolio ----------

async function loadPortfolio() {
  const meta = document.getElementById("portfolioMeta");
  const stateBody = document.getElementById("portfolioStateBody");
  const ordersBody = document.getElementById("portfolioOrdersBody");

  if (!stateBody || !ordersBody) return;

  try {
    const d = await fetchJSON("/api/portfolio");
    if (!d || !d.ok) {
      if (meta) {
        meta.textContent = "error";
        meta.className = "pill bad";
      }
      return;
    }

    if (meta) {
      meta.textContent = "live";
      meta.className = "pill ok";
    }

    // ---- state ----
    stateBody.innerHTML = "";
    for (const p of (d.state || [])) {
      stateBody.insertAdjacentHTML("beforeend", `
        <tr>
          <td class="mono">${p.symbol}</td>
          <td class="mono">${p.side}</td>
          <td class="mono">${Number(p.weight).toFixed(3)}</td>
          <td class="small">${fmtTime(p.opened_ts_ms)}</td>
          <td class="small">${fmtTime(p.updated_ts_ms)}</td>
        </tr>
      `);
    }

    // ---- orders ----
    ordersBody.innerHTML = "";
    for (const o of (d.orders || []).slice(0, 20)) {
      ordersBody.insertAdjacentHTML("beforeend", `
        <tr>
          <td class="mono">${fmtTime(o.ts_ms)}</td>
          <td class="mono">${o.symbol}</td>
          <td class="mono">${o.action}</td>
          <td class="mono">${o.from_side} ${Number(o.from_weight).toFixed(3)}</td>
          <td class="mono">${o.to_side} ${Number(o.to_weight).toFixed(3)}</td>
          <td class="mono">${Number(o.delta_weight).toFixed(3)}</td>
        </tr>
      `);
    }

  } catch (e) {
    if (meta) {
      meta.textContent = "load failed";
      meta.className = "pill bad";
    }
    console.error("loadPortfolio failed", e);
  }
}

let _lastAlerts = [];
let _pauseRefresh = false;

const _PROMO_PAUSED_KEY = "promo_paused_due_to_exec_v1";

// ---------- HARD MANIPULATION KILL-SWITCH (STEP 5) ----------
// IMPORTANT:
// This kill-switch is UI-enforced only.
// Server-side execution MUST independently enforce manipulation blocks
// to guarantee "never trade" safety.
// UI-enforced safety brake: blocks manual actions when alerts indicate
// likely social manipulation / bot promo / coordinated pump risk.
//
// NOTE: This is UI-only enforcement based on your existing alerts stream.
// Server-side must also enforce if you want "never trade" guarantees.
const _MANIP_STATE_KEY = "ui_manip_killswitch_v1";

let _manipBlockedSyms = new Set();
let _manipReasons = []; // [{symbol, severity, why, id, ts_ms}]

// restore last known manipulation state (best-effort)
try {
  const raw = localStorage.getItem(_MANIP_STATE_KEY);
  if (raw) {
    const st = JSON.parse(raw);
    _manipBlockedSyms = new Set(st.blocked || []);
    _manipReasons = st.reasons || [];
  }
} catch {}

function _kwHit(s) {
  return /(bot|promo|promot|manip|coordinat|astroturf|pump|dump|raid|brigad|shill|sockpuppet|spam)/i.test(String(s || ""));
}

function _isManipulationAlert(r) {
  if (!r) return false;
  const sev = String(r.severity || "").toUpperCase();
  if (sev !== "WARN" && sev !== "CRIT") return false;

  // Quantitative evidence (preferred when available)
  if (typeof r.manip_risk === "number" && r.manip_risk >= 0.7) return true;
  if (typeof r.bot_likelihood === "number" && r.bot_likelihood >= 0.7) return true;
  if (typeof r.promo_likelihood === "number" && r.promo_likelihood >= 0.7) return true;

  // Fallback: keyword scan
  const t = `${r.event_title || ""} ${r.reason || ""} ${r.symbol || ""} ${r.rule_id || ""}`;
  return _kwHit(t);
}

function _updateManipulationStateFromAlerts(rows) {
  const blocked = new Set();
  const reasons = [];

  for (const r of (rows || [])) {
    if (r && r.resolved) continue;
    if (!_isManipulationAlert(r)) continue;

    const sym = String(r.symbol || "").toUpperCase() || "UNKNOWN";
    blocked.add(sym);

    reasons.push({
      symbol: sym,
      severity: String(r.severity || ""),
      why: String(r.reason || r.event_title || "manipulation risk"),
      id: r.id,
      ts_ms: r.ts_ms
    });
  }

  _manipBlockedSyms = blocked;
  _manipReasons = reasons.slice(0, 50);

  // persist (best-effort)
  try {
    localStorage.setItem(_MANIP_STATE_KEY, JSON.stringify({
      ts_ms: Date.now(),
      blocked: Array.from(_manipBlockedSyms),
      reasons: _manipReasons
    }));
  } catch {}
}

function _isManipulationBlocked(sym) {
  if (!_manipBlockedSyms || _manipBlockedSyms.size === 0) return false;
  const s = String(sym || "").toUpperCase();
  if (!s) return true; // global block if unknown
  return _manipBlockedSyms.has(s) || _manipBlockedSyms.has("GLOBAL") || _manipBlockedSyms.has("EXECUTION");
}

function _manipBlockSummary() {
  const syms = Array.from(_manipBlockedSyms || []);
  return syms.length ? syms.join(", ") : "(none)";
}

function _hardBlockActionIfManipulated(actionName, symbol) {
  // HARD block unless explicitly Expert-unlocked
  if (EXPERT_UNLOCK) return false;

  if (_isManipulationBlocked(symbol || "")) {
    const msg =
      `HARD BLOCK (${actionName}) — manipulation risk flagged for: ${_manipBlockSummary()}`;

    const el = document.getElementById("console");
    if (el) el.textContent += `[kill-switch] ${msg}\n`;

    toast(msg, "bad", 5200);
    return true;
  }
  return false;
}

async function _maybeAutoResumePromotionsAfterRecovery() {
  // Never auto-resume promotions during manipulation risk
  if (_manipBlockedSyms && _manipBlockedSyms.size > 0) return;

  // Only attempt if we previously paused due to execution degradation
  if (localStorage.getItem(_PROMO_PAUSED_KEY) !== "1") return;

  // Still degraded? do nothing.
  if (_isExecutionDegraded()) return;

  try {
    const st = await fetchJSON("/api/promotion/status");
    if (!st || !st.ok) return;

    const enabledDb = (st && st.promotion_enabled_db) ? String(st.promotion_enabled_db) : "1";
    if (enabledDb === "1") {
      // already enabled; clear flag
      localStorage.removeItem(_PROMO_PAUSED_KEY);
      return;
    }

    // If not in Operator Mode, require explicit confirmation
    if (!OPERATOR_MODE) {
      const ok = confirm(
        "Execution has recovered.\n\nResume promotions automatically now?"
      );
      if (!ok) return;
    }

    const res = await fetchJSON("/api/promotion/enable?on=1");
    if (res && res.ok) {
      toast("Promotions resumed after execution recovery", "ok", 3500);
      localStorage.removeItem(_PROMO_PAUSED_KEY);
      // refresh pill
      await loadPromotionStatus();
    }
    _clearExecutionDegradationFlag();

  } catch {
    // ignore
  }
}

// execution degradation global flag (derived)
function _isExecutionDegraded() {
  const raw = localStorage.getItem(_EXEC_CONF_STATE_KEY);
  if (!raw) return false;
  try {
    const st = JSON.parse(raw);
    return Object.values(st).some(s => s.level === "WARN" || s.level === "CRIT");
  } catch {
    return false;
  }
}

function _emitExecutionDegradationAlert(info) {
  const id = `exec-degradation-${info.symbol}`;

  const alert = {
    id,
    ts_ms: Date.now(),
    severity: info.level,
    symbol: info.symbol,
    horizon_s: "-",
    expected_z: null,
    confidence: 0.99,
    event_title:
      info.level === "CRIT"
        ? "Execution degradation persists (CRIT)"
        : "Execution degradation detected",
    resolved: false,
    acked: false,
    resolved_reason: "",
    acked_by: "",
    reason:
      `Avg cost ${info.prev.toFixed(6)} → ${info.cur.toFixed(6)} ` +
      `(+${(info.worsenPct * 100).toFixed(1)}%, ${info.ageMin.toFixed(1)}m)`
  };

  // dedupe
  const exists = _lastAlerts.some(a => a.id === id);
  if (!exists) {
    _lastAlerts.unshift(alert);
    _lastAlerts = _lastAlerts.slice(0, 50);
  }
}

// explain_json is fetched on-demand via /api/alerts/by_id

async function loadAlerts() {
  // prefer timeline endpoint when available
  const data = await fetchJSON("/api/alerts/timeline?limit=50");
  const rows =
    (data && data.ok && Array.isArray(data.items))
      ? data.items
      : [];

  _lastAlerts = rows;

  // STEP 5: update manipulation kill-switch state (from existing alerts stream)
  _updateManipulationStateFromAlerts(_lastAlerts);

  // keep visuals + decision state in sync
  const filtered = _filterAlerts(_lastAlerts || []);
  renderHeatmap(filtered);
  renderIncidentQueue(filtered);

  // explain_json fetched on-demand (no eager parsing)

  const tbody = document.querySelector("#alerts tbody");
  if (!tbody) return;
  tbody.innerHTML = "";

  for (const r of _lastAlerts) {
    const tr = document.createElement("tr");
    const ageMin = Math.max(0, Math.floor((Date.now() - Number(r.ts_ms)) / 60000));

    const z = Number(r.expected_z);
    const conf = Number(r.confidence);

    const impactWord =
      (Math.abs(z) >= 2.5) ? "Very strong" :
      (Math.abs(z) >= 1.5) ? "Strong" :
      (Math.abs(z) >= 0.8) ? "Moderate" :
      "Weak";

    const confWord =
      (conf >= 0.85) ? "High" :
      (conf >= 0.65) ? "Medium" :
      "Low";

    tr.innerHTML = `
      <td class="mono">${fmtTime(r.ts_ms)}</td>
      <td>
<div class="pill ${
  r.resolved ? "ok" :
  r.severity === "CRIT" ? "crit" :
  r.severity === "WARN" ? "warn" :
  r.acked ? "dim" :
  "ok"
}">
  ${r.resolved
  ? "RESOLVED"
  : r.acked
    ? "ACKED"
    : r.severity}

</div>

      </td>
      <td>${r.symbol === "EXECUTION" ? "⚠ Execution" : r.symbol}</td>

      <td>${r.horizon_s}s</td>
      <td>${OPERATOR_MODE ? impactWord : z.toFixed(2)}</td>
      <td>${OPERATOR_MODE ? confWord : conf.toFixed(2)}</td>
      <td>
  ${r.event_title}
  ${r.reason ? `<div class="small">${esc(r.reason)}</div>` : ""}
</td>

      <td class="mono small">
${ageMin}m ago${
  r.resolved
    ? ` • RESOLVED${r.resolved_reason ? ` (${r.resolved_reason})` : ""}`
    : r.acked
      ? ` • ACKED by ${r.acked_by || "?"}`
      : ""
}

</td>
      <td>
  <button class="btn btnSmall" data-why="${Number(r.id)}">Why</button>
</td>
    `;

    tbody.appendChild(tr);
tbody.scrollTop = 0;
  }

  tbody.querySelectorAll("button[data-why]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = Number(btn.getAttribute("data-why"));
      const row = _lastAlerts.find(a => Number(a.id) === id);
      if (row) openWhyModal(row);
    });
  });
}

async function loadValidation() {
  const rows = await fetchJSON("/api/validation");
  const tbody = document.getElementById("validation"); // tbody id="validation"
  if (!tbody) return;

  tbody.innerHTML = (rows || []).map(r => `
    <tr>
      <td>${esc(r.symbol)}</td>
      <td>${esc(r.horizon_s)}</td>
      <td>${fmtNum(r.mae)}</td>
      <td>${fmtNum(r.rmse)}</td>
      <td>${esc(r.n)}</td>
      <td>${fmtTime(r.ts_ms)}</td>
    </tr>
  `).join("");
}

async function loadTemporalModels() {
  const res = await fetchJSON("/api/temporal_models?limit=20");
  const rows = (res && res.rows) ? res.rows : [];
  const tbody = document.getElementById("temporalModels");
  if (!tbody) return;

  tbody.innerHTML = (rows || []).map(r => {
    const m = r.metrics || {};
    const metricsShort = JSON.stringify({
      rmse: m.rmse,
      dir_acc: m.directional_acc,
      n_train: m.n_train,
      n_eval: m.n_eval
    });
    return `
      <tr>
        <td>${esc(r.model_name)}</td>
        <td>${esc(r.window)}</td>
        <td>${esc(r.input_dim)}</td>
        <td>${esc(r.weights_bytes)}</td>
        <td><code>${esc(metricsShort)}</code></td>
        <td>${fmtTime(r.ts_ms)}</td>
      </tr>
    `;
  }).join("");
}

async function loadJobs() {
  const data = await fetchJSON("/api/jobs");
  const jobs = (data && data.jobs) ? data.jobs : [];
  const cur = jobs.find(j => j.name === selectedJob);
  if (!cur) {
    setJobStatusPill(false, null);
    return;
  }
  setJobStatusPill(!!cur.running, cur.exit_code);
setJobButtonState(
  cur.name,
  cur.running ? "running" :
  (cur.exit_code && cur.exit_code !== 0 ? "error" : "idle")
);

}

async function loadLog() {
  if (!selectedJob) return;

  const el = document.getElementById("console");
  if (!el) return;

  try {
    const data = await fetchJSON(
      `/api/jobs/log?name=${encodeURIComponent(selectedJob)}&tail=800`
    );

    const stickBottom =
      Math.abs((el.scrollTop + el.clientHeight) - el.scrollHeight) < 20;

    el.textContent = data.log || "";

    if (stickBottom) el.scrollTop = el.scrollHeight;
  } catch {
    // ignore transient errors
  }
}

async function loadEquityReconciliation() {
  const badge = document.getElementById("eqReconBadge");
  const detail = document.getElementById("eqReconDetail");
  if (!badge) return;

  try {
    const j = await fetchJSON("/api/reconcile/broker_backtest");

    if (!j || !j.ok) {
      badge.textContent = "n/a";
      badge.className = "pill dim";
      if (detail) detail.textContent = "";
      return;
    }

    const level = String(j.equity_diff_level || "UNKNOWN").toUpperCase();

    if (j.resolved) {
      badge.textContent = "RESOLVED";
      badge.className = "pill resolved";
    } else if (j.acked) {
      badge.textContent = "ACKED";
      badge.className = "pill acked";
    } else if (level === "CRIT") {
      badge.textContent = "CRIT";
      badge.className = "pill crit";
    } else if (level === "WARN") {
      badge.textContent = "WARN";
      badge.className = "pill warn";
    } else {
      badge.textContent = "OK";
      badge.className = "pill ok";
    }

    if (detail) {
      let msg = j.reason || "";
      if (Number.isFinite(j.diff_equity) && Number.isFinite(j.diff_equity_pct)) {
        msg += ` (Δ=${j.diff_equity.toFixed(2)}, ${(j.diff_equity_pct * 100).toFixed(2)}%)`;
      }
      detail.textContent = msg;
    }
  } catch (e) {
    badge.textContent = "error";
    badge.className = "pill bad";
    if (detail) detail.textContent = e.message || String(e);
  }
}

async function loadHealth() {
  const h = await fetchJSON("/api/health");
  if (!h) return;

  const pricesOk = !!(h.prices && h.prices.ok);
  const labelsOk = !!(h.labels && h.labels.ok);
  const modelOk  = !!(h.model && h.model.ok);
  const execStateRaw = localStorage.getItem(_EXEC_CONF_STATE_KEY);
  const execState = execStateRaw ? JSON.parse(execStateRaw) : {};
  const execCrit = Object.values(execState).some(s => s.level === "CRIT");

  // execution degradation flag (UI-only)
const execDegraded = _isExecutionDegraded();

  setPill("healthPrices", pricesOk, pricesOk ? `prices ok (${h.prices.age_s}s)` : "prices stale");
  setPill("healthLabels", labelsOk, `labels ${h.labels ? h.labels.count : "?"}`);
  setPill("healthModel",  modelOk,  `model n=${h.model ? h.model.support_n : "?"}`);

  const btn = document.getElementById("btnRunPipeline");
  if (btn) {
    btn.disabled = !(pricesOk && labelsOk);
    btn.title = btn.disabled
      ? "Pipeline requires fresh prices + labels"
      : "Run full pipeline";
  }
}

async function loadMarketStress() {
  const badge = document.getElementById("marketStressBadge");
  const updated = document.getElementById("marketStressUpdated");
  const body = document.getElementById("marketStressBody");
  const raw = document.getElementById("marketStressRaw");
  if (!badge || !updated || !body || !raw) return;

  try {
    const j = await fetchJSON("/api/market_stress");
    if (!j || !j.ok || !j.stress) return;

    const s = j.stress || {};
    const score = Number(s.stress_score ?? 0);
    const ts_ms = Number(s.ts_ms ?? 0);

    // pill class by stress
    let cls = "pill ok";
    if (score >= 0.75) cls = "pill bad";
    else if (score >= 0.55) cls = "pill warn";

    badge.className = cls;
    badge.textContent = Number.isFinite(score) ? score.toFixed(3) : "—";

    // Update header badge
    const hdr = document.getElementById("marketStressHeader");
    if (hdr) {
      hdr.className = cls;
      hdr.textContent = Number.isFinite(score)
        ? `Stress: ${score.toFixed(2)}`
        : "Stress: —";
    }

    if (Number.isFinite(ts_ms) && ts_ms > 0) {
      updated.textContent = new Date(ts_ms).toLocaleString();
    } else {
      updated.textContent = "—";
    }

    // Build human explanation tooltip (top contributors)
    const reasons = [];
    if (Number(s.z_vix) > 1.5) reasons.push("VIX elevated");
    if (Number(s.z_vvix) > 1.5) reasons.push("Vol-of-vol spike");
    if (Number(s.z_move) > 1.5) reasons.push("Bond volatility elevated");
    if (Number(s.z_term) > 1.2) reasons.push("VIX term structure stressed");
    if (Number(s.z_credit) > 1.2) reasons.push("Credit stress proxy widening");
    if (Number(s.z_rates) > 1.2) reasons.push("Rates risk-off signal");

      hdr.title = reasons.length
        ? `Stress drivers: ${reasons.join(", ")}`
        : "Market stress within normal range";

    const rows = [
      ["VIX", s.vix, s.z_vix],
      ["VVIX", s.vvix, s.z_vvix],
      ["MOVE", s.move, s.z_move],

      ["VIX1D/VIX", s.vix1d_over_vix, null],
      ["VIX9D/VIX", s.vix9d_over_vix, null],
      ["VIX3M/VIX", s.vix3m_over_vix, null],
      ["Term Z", null, s.z_term],

      ["Credit LQD/HYG", s.credit_lqd_over_hyg, s.z_credit],
      ["Rates TLT/SHY", s.rates_tlt_over_shy, s.z_rates],
    ];

    body.innerHTML = "";
    for (const [name, val, z] of rows) {
      const tr = document.createElement("tr");
      const v = (val === null || val === undefined || !Number.isFinite(Number(val)))
        ? "—"
        : Number(val).toFixed(4);
      const zz = (z === null || z === undefined || !Number.isFinite(Number(z)))
        ? "—"
        : Number(z).toFixed(3);

      tr.innerHTML = `
        <td>${esc(name)}</td>
        <td class="mono">${esc(v)}</td>
        <td class="mono">${esc(zz)}</td>
      `;
      body.appendChild(tr);
    }

    raw.textContent = JSON.stringify(s, null, 2);

    // Confidence dampening (purely informational)
    const conf = document.getElementById("confidenceBadge");
    if (conf) {
      let c = 1.0 - Math.min(1.0, Math.max(0.0, score));
      let cls = "pill ok";
      if (c < 0.5) cls = "pill warn";
      if (c < 0.3) cls = "pill bad";

      conf.className = cls;
      conf.textContent = `${Math.round(c * 100)}%`;
      conf.title = "Displayed confidence dampening due to market stress (informational only)";
    }

  } catch (e) {
    raw.textContent = e && e.message ? e.message : String(e);
  }
}

async function loadMarketStressHistory() {
  const canvas = document.getElementById("marketStressSparkline");
  if (!canvas) return;

  try {
    const j = await fetchJSON("/api/market_stress_history");
    if (!j || !j.ok || !Array.isArray(j.series)) return;

    const ctx = canvas.getContext("2d");
    const w = canvas.width;
    const h = canvas.height;

    ctx.clearRect(0, 0, w, h);

    const ys = j.series.map(p => Number(p.stress_score || 0));
    if (!ys.length) return;

    const min = 0;
    const max = 1;

    ctx.beginPath();
    ctx.strokeStyle = "#aaa";
    ys.forEach((v, i) => {
      const x = (i / (ys.length - 1)) * w;
      const y = h - ((v - min) / (max - min)) * h;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  } catch (_) {}
}

async function loadExecutionOverlays() {
  const raw = document.getElementById("execOverlaysRaw");
  if (!raw) return;

  try {
    const j = await fetchJSON("/api/execution_overlays");
    if (!j || !j.ok) return;
    raw.textContent = JSON.stringify(j, null, 2);
  } catch (e) {
    raw.textContent = e && e.message ? e.message : String(e);
  }
}

async function loadStrategyMetrics() {

  try {
    const rows = await fetchJSON("/api/strategy_metrics");
    const tbody = document.querySelector("#strategyMetrics tbody");
    if (!tbody) return;
    tbody.innerHTML = "";
    for (const r of rows || []) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${r.strategy}</td>
        <td>${r.window_days}</td>
        <td>${Number(r.net_calmar).toFixed(3)}</td>
        <td>${Number(r.sharpe).toFixed(3)}</td>
        <td>${Number(r.turnover).toFixed(3)}</td>
        <td class="mono">${fmtTime(r.ts_ms)}</td>
      `;
      tbody.appendChild(tr);
tbody.scrollTop = 0;
    }
  } catch (e) {}
}

async function loadTemporalShadowEval() {
  const tbody = document.querySelector("#temporalShadowEval tbody");
  if (!tbody) return;

  let rows = [];
  try {
    rows = await fetchJSON("/api/temporal_shadow_eval?limit=200");
    if (!Array.isArray(rows)) rows = [];
  } catch {
    rows = [];
  }

  tbody.innerHTML = "";
  for (const r of rows) {
    const why =
  r.detail && Array.isArray(r.detail.reasons)
    ? r.detail.reasons.join(", ")
    : (r.reason || "");
    const pass = !!r.pass_all;

    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHTML(r.symbol || "")}</td>
      <td>${Number(r.horizon_s || 0)}</td>
      <td>${Number(r.n || 0)}</td>
      <td>${Number(r.rmse || 0).toFixed(3)}</td>
      <td>${Number(r.baseline_rmse || 0).toFixed(3)}</td>
      <td>${Number(r.directional_acc || 0).toFixed(3)}</td>
      <td>${Number(r.baseline_directional_acc || 0).toFixed(3)}</td>
      <td>${pass ? "✅" : "❌"}</td>
      <td>${escapeHTML(why)}</td>
    `;
    tbody.appendChild(tr);
  }
}

async function loadPromotionAudit() {
  const tbody = document.querySelector("#promotionAudit tbody");
  if (!tbody) return;

  let rows = [];
  try {
    rows = await fetchJSON("/api/promotion_audit?limit=200");
    if (!Array.isArray(rows)) rows = [];
  } catch {
    rows = [];
  }

  tbody.innerHTML = "";
  for (const r of rows) {
    const ts = fmtTime(r.ts_ms);
    const model = String(r.model_name || "");
    const action = String(r.action || "");
    const reg = (r.regime === null || r.regime === undefined) ? "" : String(r.regime);
    const why = JSON.stringify(r.reason || {}).slice(0, 240);

    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHTML(ts)}</td>
      <td>${escapeHTML(model)}</td>
      <td>${escapeHTML(action)}</td>
      <td>${escapeHTML(reg)}</td>
      <td><code>${escapeHTML(why)}</code></td>
    `;
    tbody.appendChild(tr);
  }
}

function _drawCalib(canvas, pts) {
  if (!canvas || !Array.isArray(pts) || pts.length < 2) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);

  // axes padding
  const pad = 18;
  const x0 = pad, y0 = H - pad;
  const x1 = W - pad, y1 = pad;

  // diagonal y=x reference
  ctx.beginPath();
  ctx.moveTo(x0, y0);
  ctx.lineTo(x1, y1);
  ctx.strokeStyle = "rgba(160,160,160,0.35)";
  ctx.lineWidth = 1;
  ctx.stroke();

  const xs = pts.map(p => Number(p.conf || 0));
  const ys = pts.map(p => Number(p.acc || 0));

  const clamp01 = (v) => Math.max(0, Math.min(1, v));

  ctx.beginPath();
  for (let i = 0; i < pts.length; i++) {
    const x = clamp01(xs[i]);
    const y = clamp01(ys[i]);

    const px = x0 + x * (x1 - x0);
    const py = y0 - y * (y0 - y1);

    if (i === 0) ctx.moveTo(px, py);
    else ctx.lineTo(px, py);
  }
  ctx.strokeStyle = "rgba(220,220,220,0.85)";
  ctx.lineWidth = 2;
  ctx.stroke();
}

async function refreshCalibCurves() {
  const hEl = document.getElementById("calibHorizon");
  const kEl = document.getElementById("calibKind");
  const pre = document.getElementById("calibRaw");
  const canvas = document.getElementById("calibCanvas");
  if (!hEl || !kEl) return;

  const horizon_s = Number(hEl.value || 3600);
  const model_kind = String(kEl.value || "ridge");

  let out = null;
  try {
    out = await fetchJSON(`/api/embed_conf_calib?horizon_s=${encodeURIComponent(horizon_s)}&model_kind=${encodeURIComponent(model_kind)}`);
  } catch {
    out = null;
  }

if (pre) pre.textContent = out ? JSON.stringify(out, null, 2) : "(no calib data)";

if (
  canvas &&
  out &&
  out.ok &&
  Array.isArray(out.points)
) {
  _drawCalib(canvas, out.points);
}
}

async function loadTemporalEval() {

  try {
    const j = await fetchJSON("/api/temporal_eval");
    const tbody = document.querySelector("#temporalEval tbody");
    if (!tbody || !j || !j.rows) return;

    tbody.innerHTML = "";
    for (const r of j.rows) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="mono">${r.horizon_s}s</td>
        <td class="mono">${r.n}</td>
        <td class="mono">${r.rmse.toFixed(3)}</td>
        <td class="mono">${(100*r.directional_acc).toFixed(1)}%</td>
      `;
      tbody.appendChild(tr);
tbody.scrollTop = 0;
    }
  } catch {}
}

async function loadModelMetrics() {
  try {
    const rows = await fetchJSON("/api/model_metrics?model=default");
    const tbody = document.querySelector("#modelMetrics tbody");
    if (!tbody) return;
    tbody.innerHTML = "";
    for (const r of (rows || [])) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${r.symbol}</td>
        <td>${r.horizon_s}s</td>
        <td>${Number(r.r2 || 0).toFixed(3)}</td>
        <td>${Number(r.direction_acc || 0).toFixed(3)}</td>
        <td>${Number(r.ece || 0).toFixed(3)}</td>
        <td>${Number(r.avg_conf || 0).toFixed(3)}</td>
        <td>${Number(r.abs_err_p50 || 0).toFixed(3)}</td>
        <td>${Number(r.abs_err_p90 || 0).toFixed(3)}</td>
        <td>${Number(r.n || 0)}</td>
        <td class="mono">${fmtTime(r.ts_ms)}</td>
      `;
      tbody.appendChild(tr);
tbody.scrollTop = 0;
    }
  } catch (e) {
    // ignore
  }
}

async function loadModelRegistry() {
  const body = document.getElementById("modelRegistryBody");
  const chPill = document.getElementById("championPill");
  const clPill = document.getElementById("challengerPill");
  if (!body || !chPill || !clPill) return;

  try {
    const j = await fetchJSON("/api/model_registry?limit=25");
    if (!j || !j.ok) return;

    const champ = j.champion || {};
    const chall = j.challenger || {};

    function fmtNum(x) {
      if (x === null || x === undefined || !Number.isFinite(Number(x))) return "";
      return Number(x).toFixed(4);
    }

    const champRmse = champ.metrics ? champ.metrics.rmse : null;
    const challRmse = chall.metrics ? chall.metrics.rmse : null;

    chPill.textContent = `champion: ${champ.model_kind || "?"} rmse=${fmtNum(champRmse)}`;
    clPill.textContent = `challenger: ${chall.model_kind || "?"} rmse=${fmtNum(challRmse)}`;

    chPill.className = "pill " + (_isExecutionDegraded() ? "warn" : "ok");
    clPill.className = "pill dim";

    body.innerHTML = "";
    for (const r of (j.history || [])) {
      const m = r.metrics || {};
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${r.stage || ""}</td>
        <td class="mono">${r.model_kind || ""}</td>
        <td class="mono">${fmtNum(m.rmse)}</td>
        <td class="mono">${m.directional_acc !== undefined ? (100*Number(m.directional_acc)).toFixed(2)+"%" : ""}</td>
        <td class="mono">${m.n_eval || ""}</td>
        <td class="mono">${fmtTime(m.eval_ts_ms || r.model_ts_ms || 0)}</td>
        <td class="mono">${fmtTime(r.created_ts_ms || 0)}</td>
      `;
      body.appendChild(tr);
    }
  } catch (e) {
    // ignore
  }
}

async function loadModelDiagnostics() {

  const el = document.getElementById("modelDiagnostics");
  if (!el) return;
  try {
    const d = await fetchJSON("/api/model/diagnostics");
    let out = "";
    out += "=== REGIME PRIORS ===\n";
    const rp = d && d.regime_priors ? d.regime_priors : {};
    for (const k of Object.keys(rp)) {
      out += `\n${k}\n`;
      for (const r of rp[k]) out += `  ${r.regime}: n=${r.n} mean_z=${Number(r.mean_z).toFixed(3)}\n`;
    }
    out += "\n=== GLOBAL PRIORS ===\n";
    for (const g of (d && d.global_priors) ? d.global_priors : []) {
      out += `${g.symbol} h=${g.horizon_s} n=${g.n} mean_z=${Number(g.mean_z).toFixed(3)}\n`;
    }
    out += "\n=== SPILLOVERS ===\n";
    const sp = d && d.spillovers ? d.spillovers : {};
    for (const k of Object.keys(sp)) {
      out += `\n${k}\n`;
      for (const s of sp[k]) out += `  <- ${s.driver}: beta=${Number(s.beta).toFixed(3)} n=${s.n}\n`;
    }
    el.textContent = out || "(no diagnostics yet)";
  } catch (e) {
    el.textContent = `[error] ${e.message}`;
  }
}

// ---------- Relevance Stats ----------
async function loadRelevanceStats() {
  const body = document.getElementById("relevanceTableBody");
  const meta = document.getElementById("relevanceMeta");
  const diffBox = document.getElementById("relevanceDiff");
  if (!body || !meta || !diffBox) return;

  try {
    const res = await fetchJSON("/api/relevance_stats");
    if (!res || !res.ok) {
      meta.textContent = "disabled";
      meta.className = "pill dim";
      body.innerHTML = "";
      diffBox.textContent = res && res.error ? res.error : "not available";
      return;
    }

    meta.textContent = res.cached ? "cached" : "live";
    meta.className = "pill " + (res.cached ? "dim" : "ok");

    const rows = _parseRelevanceStats(res.stats);

    // dominance thresholds (relative, not magic numbers)
    const maxRel = Math.max(...rows.map(r => r.relevance || 0), 0);
    const maxZ   = Math.max(...rows.map(r => r.mean_abs_z || 0), 0);

    body.innerHTML = "";
    rows
      .sort((a, b) => (b.relevance || 0) - (a.relevance || 0))
      .forEach(r => {
        const dom =
          (r.relevance >= 0.9 * maxRel && r.relevance > 0) ||
          (r.mean_abs_z >= 0.9 * maxZ && r.mean_abs_z > 0);

        body.insertAdjacentHTML("beforeend", `
          <tr class="${dom ? "health-ok" : ""}">
            <td class="mono">${r.symbol}</td>
            <td class="mono">${r.horizon}</td>
            <td>${Number.isFinite(r.relevance) ? r.relevance.toFixed(3) : "?"}</td>
            <td>${Number.isFinite(r.mean_abs_z) ? r.mean_abs_z.toFixed(3) : "?"}</td>
            <td>${Number.isFinite(r.n) ? r.n : "?"}</td>
          </tr>
        `);
      });

    // ---- diff vs previous snapshot ----
    const prevRaw = localStorage.getItem(_RELEVANCE_SNAPSHOT_KEY);
    const prev = prevRaw ? _safeParseJSON(prevRaw) : null;

    if (prev) {
      const diff = _diffRelevance(prev, res.stats);
      diffBox.textContent = diff.length
        ? diff.slice(0, 100).map(d => `- ${d}`).join("\n")
        : "(no changes)";
    } else {
      diffBox.textContent = "(first snapshot)";
    }

    localStorage.setItem(_RELEVANCE_SNAPSHOT_KEY, JSON.stringify(res.stats));
  } catch (e) {
    meta.textContent = "error";
    meta.className = "pill bad";
    body.innerHTML = "";
    diffBox.textContent = e.message;
  }
}

async function loadConfidenceMass() {
  const wrap = document.getElementById("confidenceMass");
  if (!wrap) return;
  try {
    const d = await fetchJSON("/api/confidence_mass");
    const bins = (d && d.bins) ? d.bins : [];
    wrap.innerHTML = "";
    if (!bins.length) {
      wrap.textContent = "(no confidence data yet)";
      return;
    }

    const maxN = Math.max(...bins.map(b => b.count || 0), 1);
    for (const b of bins) {
      const row = document.createElement("div");
      row.style.display = "flex";
      row.style.alignItems = "center";
      row.style.gap = "8px";
      row.style.margin = "4px 0";

      const label = document.createElement("div");
      label.className = "mono small";
      label.style.width = "88px";
      label.textContent = `${b.lo.toFixed(1)}–${b.hi.toFixed(1)}`;

      const bar = document.createElement("div");
      bar.style.flex = "1";
      bar.style.height = "10px";
      bar.style.border = "1px solid #30363d";
      bar.style.borderRadius = "999px";
      bar.style.overflow = "hidden";
      bar.style.background = "#0e1117";

      const fill = document.createElement("div");
      fill.style.height = "100%";
      fill.style.width = barWidth((100 * (b.count || 0)) / maxN);
      fill.style.background = "#2ea043";
      bar.appendChild(fill);

      const n = document.createElement("div");
      n.className = "mono small";
      n.style.width = "48px";
      n.style.textAlign = "right";
      n.textContent = String(b.count || 0);

      row.appendChild(label);
      row.appendChild(bar);
      row.appendChild(n);
      wrap.appendChild(row);
    }
  } catch (e) {
    wrap.textContent = `[error] ${e.message}`;
  }
}

// ------            -- ------------------------------------------------------
// Social (read-only) panels
// ------            -- ------------------------------------------------------

async function loadSocialPressure() {
  const body = document.getElementById("socialPressureBody");
  if (!body) return;

  const sym = (document.getElementById("globalSymbol")?.value || "SPY").toUpperCase();

  try {
    const d = await fetchJSON(`/api/social/features?symbol=${encodeURIComponent(sym)}&limit=50`);
    const rows = (d && d.rows) ? d.rows : [];

    body.innerHTML = "";
    for (const r of rows.slice(0, 20)) {
      body.insertAdjacentHTML("beforeend", `
        <tr>
          <td class="mono">${fmtTime(r.bucket_ts_ms)}</td>
          <td class="mono">${Number(r.mention_rate_z).toFixed(2)}</td>
          <td class="mono">${Number(r.attention_shock).toFixed(2)}</td>
          <td class="mono">${Number(r.manip_risk).toFixed(2)}</td>
          <td class="mono">${Number(r.cross_platform_confirm).toFixed(2)}</td>
        </tr>
      `);
    }

    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="5" class="small">(no social data)</td></tr>`;
    }
  } catch (e) {
    body.innerHTML = `<tr><td colspan="5" class="small">error loading social features</td></tr>`;
  }
}

async function loadSocialRegimes() {
  const body = document.getElementById("socialRegimeBody");
  if (!body) return;

  const sym = (document.getElementById("globalSymbol")?.value || "SPY").toUpperCase();

  try {
    const d = await fetchJSON(`/api/social/regimes?symbol=${encodeURIComponent(sym)}&limit=50`);
    const rows = (d && d.rows) ? d.rows : [];

    body.innerHTML = "";
    for (const r of rows.slice(0, 20)) {
      body.insertAdjacentHTML("beforeend", `
        <tr>
          <td class="mono">${fmtTime(r.bucket_ts_ms)}</td>
          <td>${esc(r.regime)}</td>
          <td class="mono">${Number(r.regime_conf).toFixed(2)}</td>
        </tr>
      `);
    }

    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="3" class="small">(no regimes)</td></tr>`;
    }
  } catch {
    body.innerHTML = `<tr><td colspan="3" class="small">error loading regimes</td></tr>`;
  }
}

async function loadSocialBlocks() {
  const body = document.getElementById("socialBlocksBody");
  if (!body) return;

  try {
    const d = await fetchJSON(`/api/social/blocks?limit=20`);
    const rows = (d && d.rows) ? d.rows : [];

    body.innerHTML = "";
    for (const r of rows.slice(0, 10)) {
      body.insertAdjacentHTML("beforeend", `
        <tr>
          <td class="mono">${fmtTime(r.ts_ms)}</td>
          <td class="mono">${esc(r.symbol)}</td>
          <td class="small"><code>${escapeHTML(JSON.stringify(r.reason || {}))}</code></td>
        </tr>
      `);
    }

    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="3" class="small">(no social blocks)</td></tr>`;
    }
  } catch {
    body.innerHTML = `<tr><td colspan="3" class="small">error loading blocks</td></tr>`;
  }
}

async function loadJobHistory() {

  const panel = document.getElementById("jobHistoryPanel");
  const el = document.getElementById("jobHistory");
  if (!panel || !el) return;
  if (panel.style.display === "none") return;

  try {
    const d = await fetchJSON("/api/jobs/history?limit=200");
    const items = (d && d.ok && Array.isArray(d.history)) ? d.history : [];
    let out = "";
    for (const it of items) {
      const t = fmtTime(it.ts_ms);
      const name = it.job_name || "";
      const ev = it.event || "";
      const rc = (it.exit_code === null || it.exit_code === undefined) ? "" : ` rc=${it.exit_code}`;
      const det = it.detail ? ` — ${it.detail}` : "";
      out += `${t}  ${name}  ${ev}${rc}${det}\n`;
    }
    el.textContent = out || "(no history yet)";
  } catch (e) {
    el.textContent = `[error] ${e.message}`;
  }
}

async function loadConfidenceTrends() {
  const panel = document.getElementById("confidenceTrendPanel");
  const el = document.getElementById("confidenceTrends");
  if (!panel || !el) return;
  if (panel.style.display === "none") return;

  try {
    const d = await fetchJSON("/api/alerts/timeline?limit=120");
    const rows = (d && d.ok && Array.isArray(d.items)) ? d.items : [];

    const bySym = {};
    for (const r of rows) {
      const sym = r.symbol || "UNK";
      const c = Number(r.confidence);
      if (!Number.isFinite(c)) continue;
      (bySym[sym] ||= []).push(c);
    }

    let out = "";
    for (const sym of Object.keys(bySym).sort()) {
      const arr = bySym[sym];
      const last10 = arr.slice(0, 10);
      const prev10 = arr.slice(10, 20);
      const avg = (xs) => xs.length ? xs.reduce((a,b)=>a+b,0)/xs.length : 0;

      const a1 = avg(last10);
      const a2 = avg(prev10);
      const trend = (a1 > a2 + 0.03) ? "↑" : (a1 < a2 - 0.03) ? "↓" : "→";

      out += `${sym}: avg_conf=${a1.toFixed(2)} ${trend} (n=${arr.length})\n`;
    }

    el.textContent = out || "(no alerts yet)";
  } catch (e) {
    el.textContent = `[error] ${e.message}`;
  }
}

// ------            -- ------------------------------------------------------
// Portfolio Backtest (Latest) — equity curve + drawdown charts
// Endpoint: GET /api/backtest/portfolio/latest
// ------            -- ------------------------------------------------------
function fmtNum(x) {
  if (x === null || x === undefined) return "";
  const v = Number(x);
  if (!isFinite(v)) return "";
  return v.toFixed(4);
}

function _fmtPct(x) {
  if (!Number.isFinite(x)) return "?";
  return `${(x * 100).toFixed(2)}%`;
}

function _clamp(v, lo, hi) {
  return Math.max(lo, Math.min(hi, v));
}

function _chartClear(ctx, w, h) {
  ctx.clearRect(0, 0, w, h);
}

function _chartFrame(ctx, w, h) {
  ctx.strokeStyle = "#30363d";
  ctx.lineWidth = 1;
  ctx.strokeRect(0.5, 0.5, w - 1, h - 1);
}

function _chartText(ctx, x, y, s) {
  ctx.fillStyle = "#9da7b1";
  ctx.font = "12px Consolas, monospace";
  ctx.fillText(String(s), x, y);
}

function _renderLineChart(canvas, ys, opts = {}) {
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  const w = canvas.width;
  const h = canvas.height;

  const padL = 44;
  const padR = 10;
  const padT = 12;
  const padB = 20;

  _chartClear(ctx, w, h);
  _chartFrame(ctx, w, h);

  if (!Array.isArray(ys) || ys.length < 2) {
    _chartText(ctx, 12, 24, "(no data)");
    return;
  }

  // sanitize
  const vals = ys.map(v => Number(v)).filter(v => Number.isFinite(v));
  if (vals.length < 2) {
    _chartText(ctx, 12, 24, "(no numeric data)");
    return;
  }

  let yMin = Number.isFinite(opts.yMin) ? Number(opts.yMin) : Math.min(...vals);
  let yMax = Number.isFinite(opts.yMax) ? Number(opts.yMax) : Math.max(...vals);

  if (yMin === yMax) {
    yMin -= 1;
    yMax += 1;
  }

  // small padding so line doesn't sit on border
  const yPad = (yMax - yMin) * 0.08;
  yMin -= yPad;
  yMax += yPad;

  // axes labels
  _chartText(ctx, 8, padT + 10, (opts.topLabel || ""));
  _chartText(ctx, 8, h - 8, (opts.bottomLabel || ""));

  // y labels (min/max)
  _chartText(ctx, 8, padT + 22, (opts.fmtY ? opts.fmtY(yMax) : yMax.toFixed(3)));
  _chartText(ctx, 8, h - padB - 4, (opts.fmtY ? opts.fmtY(yMin) : yMin.toFixed(3)));

  const plotW = w - padL - padR;
  const plotH = h - padT - padB;

  function xFor(i) {
    return padL + (plotW * (i / (ys.length - 1)));
  }
  function yFor(v) {
    const t = (Number(v) - yMin) / (yMax - yMin);
    return padT + plotH * (1 - _clamp(t, 0, 1));
  }

  // gridline mid
  ctx.strokeStyle = "#20252c";
  ctx.lineWidth = 1;
  const midY = padT + plotH / 2;
  ctx.beginPath();
  ctx.moveTo(padL, midY);
  ctx.lineTo(w - padR, midY);
  ctx.stroke();

  // line
  ctx.strokeStyle = (opts.stroke || "#2ea043");
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(xFor(0), yFor(ys[0]));
  for (let i = 1; i < ys.length; i++) {
    ctx.lineTo(xFor(i), yFor(ys[i]));
  }
  ctx.stroke();
}

async function loadPortfolioBacktestLatest() {
  const meta = document.getElementById("portfolioBtMeta");
  const sumBody = document.getElementById("portfolioBtSummaryBody");
  const cEq = document.getElementById("portfolioEquityCanvas");
  const cDd = document.getElementById("portfolioDdCanvas");

  // This UI is optional; if elements aren't present, silently skip.
  if (!meta || !sumBody || !cEq || !cDd) return;

  try {
    const res = await fetchJSON("/api/backtest/portfolio/latest");
    if (!res || !res.ok || !res.run) {
      meta.textContent = "no runs";
      meta.className = "pill dim";
      sumBody.innerHTML = "";
      _renderLineChart(cEq, []);
      _renderLineChart(cDd, []);
      return;
    }

    meta.textContent = "ok";
    meta.className = "pill ok";

    const run = res.run || {};
    const metrics = run.metrics || {};
    const pts = Array.isArray(run.points) ? run.points : [];

// Equity curve from backend (capital-aware)
const equity = [];
for (const p of pts) {
  const e = Number(p && p.equity);
  if (!Number.isFinite(e)) continue;
  equity.push(e);
}

    // Drawdown series
// Drawdown comes directly from backend (already peak-to-trough)
const dd = [];
let maxDd = 0;
for (const p of pts) {
  const d = Number(p && p.drawdown);
  if (!Number.isFinite(d)) continue;
  dd.push(d);
  if (d < maxDd) maxDd = d;
}

    // Render charts
    _renderLineChart(cEq, equity, {
      topLabel: "equity",
      fmtY: (v) => Number(v).toFixed(3),
      stroke: "#2ea043",
    });

    _renderLineChart(cDd, dd, {
      topLabel: "drawdown",
      fmtY: (v) => _fmtPct(v),
      stroke: "#ff6b6b",
      yMax: 0, // drawdown top at 0%
    });

    // Summary table
    // We keep this permissive because your metrics_json schema may evolve.
    const startTs = Number(run.start_ts_ms);
    const endTs = Number(run.end_ts_ms);
    const windowStr =
      (Number.isFinite(startTs) && Number.isFinite(endTs))
        ? `${new Date(startTs).toLocaleDateString()} → ${new Date(endTs).toLocaleDateString()}`
        : "—";

    const totalReturn =
      (equity.length >= 2)
        ? ((equity[equity.length - 1] / equity[0]) - 1.0)
        : (Number.isFinite(metrics.total_return) ? Number(metrics.total_return) : NaN);

const sharpe   = Number.isFinite(metrics.sharpe_simple) ? Number(metrics.sharpe_simple) : NaN;
const sortino  = Number.isFinite(metrics.sortino_simple) ? Number(metrics.sortino_simple) : NaN;
const calmar   = Number.isFinite(metrics.calmar_simple) ? Number(metrics.calmar_simple) : NaN;
const vol      = Number.isFinite(metrics.ret_volatility) ? Number(metrics.ret_volatility) : NaN;
const turnover = Number.isFinite(metrics.turnover_avg) ? Number(metrics.turnover_avg) : NaN;

const trades =
  Number.isFinite(metrics.steps_used) ? Number(metrics.steps_used) : NaN;

    sumBody.innerHTML = "";
sumBody.insertAdjacentHTML("beforeend", `
  <tr>
    <td class="small">${windowStr}</td>
    <td class="mono">${Number.isFinite(totalReturn) ? _fmtPct(totalReturn) : "?"}</td>
    <td class="mono">${_fmtPct(maxDd)}</td>
    <td class="mono">
      S=${Number.isFinite(sharpe) ? sharpe.toFixed(2) : "?"}
      &nbsp;So=${Number.isFinite(sortino) ? sortino.toFixed(2) : "?"}
      &nbsp;C=${Number.isFinite(calmar) ? calmar.toFixed(2) : "?"}
    </td>
    <td class="mono">
      n=${Number.isFinite(trades) ? trades : "?"}
      &nbsp;τ=${Number.isFinite(turnover) ? turnover.toFixed(3) : "?"}
    </td>
  </tr>
`);


  } catch (e) {
    meta.textContent = "error";
    meta.className = "pill bad";
    sumBody.innerHTML = "";
    _renderLineChart(cEq, []);
    _renderLineChart(cDd, []);
  }
}

/* -----------------------------
   Actions
----------------------------- */

async function jobAction(name, action) {
  setSelectedJob(name);
  if (action === "start") {
    await fetchJSON(`/api/jobs/start?name=${encodeURIComponent(name)}`);
  } else if (action === "stop") {
    await fetchJSON(`/api/jobs/stop?name=${encodeURIComponent(name)}`);
  }
  await refresh();
}

async function refresh() {


  try {
    const health = await fetchJSON("/api/health");

let trainingStatus = null;
try {
  trainingStatus = await fetchJSON("/api/training_status");
} catch (e) {
  trainingStatus = { mode: "unknown", allowed: false };
}

    const hEl = document.getElementById("healthStatus");
    const hDet = document.getElementById("healthDetails");

    setStatus(
  hEl,
health.ok && !_isExecutionDegraded(),
_isExecutionDegraded()
  ? "DEGRADED (execution)"
  : (health.ok ? "OK" : "DEGRADED")
);
    hDet.textContent = JSON.stringify(health, null, 2);
  } catch (e) {
    console.error(e);
  }

  try {
    const tr = await fetchJSON("/api/training_status");
    const tEl = document.getElementById("trainingStatus");
    const tDet = document.getElementById("trainingDetails");

    const ok = !!tr.allowed;
    setStatus(
      tEl,
      ok,
      ok ? "ENABLED" : ("BLOCKED (" + tr.mode + ")")
    );
    tDet.textContent = JSON.stringify(tr, null, 2);
  } catch (e) {
    console.error(e);
  }

  if (_pauseRefresh) return;
  await Promise.allSettled([
    loadTemporalEval(),
    loadTemporalShadowEval(),
    loadSocialPressure(),
    loadSocialRegimes(),
    loadSocialBlocks(),
    loadPromotionAudit(),
    refreshCalibCurves(),
    loadModelRegistry(),
    loadValidation(),
    loadStrategyStatus(),
    loadStrategyMetrics(),
    loadModelMetrics(),
    loadJobs(),
    loadLog(),
    loadHealth(),
    loadMarketStress(),
    loadMarketStressHistory(),
    loadExecutionOverlays(),
    loadModelDiagnostics(),
    loadConfidenceMass(),
    loadJobHistory(),
    loadConfidenceTrends(),
    loadRelevanceStats(),
    loadExecutionByConfidence(),

    // portfolio layer
    (typeof loadPortfolio === "function") ? loadPortfolio() : Promise.resolve(),
    (typeof loadBroker === "function") ? loadBroker() : Promise.resolve(),
    loadPortfolioBacktestLatest(),
    loadEquityReconciliation(),
    loadEquityDrift(),
  ]);

  // If we paused promotions due to exec degradation, try to resume after recovery
  await _maybeAutoResumePromotionsAfterRecovery();

}

async function loadPromotionStatus() {
  const pill = document.getElementById("promotionPill");
  const reasonEl = document.getElementById("promotionReason");
  if (!pill || !reasonEl) return;

  try {
    if (_isExecutionDegraded()) {
      // mark that we intentionally paused due to exec degradation
      localStorage.setItem("promo_paused_due_to_exec_v1", "1");
    }

    const j = await fetchJSON("/api/promotion/status");
    if (!j || !j.ok) return;

    pill.textContent = j.allowed ? "promotion: ALLOWED" : "promotion: BLOCKED";
    pill.className = "pill " + (j.allowed ? "ok" : "bad");

    const r = j.reason || {};
    reasonEl.textContent = JSON.stringify(r, null, 2);
  } catch (e) {
    // ignore
  }
}

function wirePromotionButtons() {
  const btnToggle = document.getElementById("btnTogglePromotions");
  const btnRollback = document.getElementById("btnRollbackChampion");

  if (btnToggle) {
    btnToggle.addEventListener("click", async () => {
  // STEP 5: HARD manipulation kill-switch (UI enforcement)
  if (_hardBlockActionIfManipulated("toggle promotions", "GLOBAL")) return;

  if (_isExecutionDegraded()) {
    localStorage.setItem("promo_paused_due_to_exec_v1", "1");
    toast("Promotions paused due to execution degradation", "warn", 4000);
    return;
  }

      if (!OPERATOR_MODE) {
    if (!confirm("Toggle promotions? This affects model promotion safety.")) return;
  }
      btnToggle.disabled = true;
      try {
        // read current, then flip
        const st = await fetchJSON("/api/promotion/status");
        const enabledDb = (st && st.promotion_enabled_db) ? st.promotion_enabled_db : "1";
        const next = (enabledDb === "1") ? "0" : "1";
        const res = await fetchJSON(`/api/promotion/enable?on=${next}`);
        if (!res || !res.ok) throw new Error((res && res.error) || "toggle failed");
        await loadPromotionStatus();
      } catch (e) {
        const el = document.getElementById("console");
        if (el) el.textContent += `[ui] toggle promotions ERROR: ${e.message}\n`;
      } finally {
        btnToggle.disabled = false;
      }
    });
  }

  if (btnRollback) {
    btnRollback.addEventListener("click", async () => {
      btnRollback.disabled = true;
      try {
        const res = await fetchJSON("/api/champion/rollback");
        if (!res || !res.ok) throw new Error((res && res.error) || "rollback failed");
        const el = document.getElementById("console");
        if (el) el.textContent += `[ui] rollback ok -> ${JSON.stringify(res.champion || {})}\n`;
      } catch (e) {
        const el = document.getElementById("console");
        if (el) el.textContent += `[ui] rollback ERROR: ${e.message}\n`;
      } finally {
        btnRollback.disabled = false;
      }
    });
  }
}

function wireCollapsibles() {
  document.querySelectorAll(".collapsible h2").forEach(h => {
    h.addEventListener("click", () => {
      const parent = h.parentElement;
      if (parent && parent.classList) {
        parent.classList.toggle("collapsed");
      }
    });
  });
}

/* -----------------------------
   Wiring
----------------------------- */
  const btnSP = document.getElementById("btnTrainSizePolicy");
  if (btnSP) {
    btnSP.addEventListener("click", async () => {
      btnSP.disabled = true;
      try {
        const el = document.getElementById("console");
        if (el) el.textContent += "[ui] training size policy...\n";
        const res = await fetchJSON("/api/size_policy/train");
        if (!res || !res.ok) throw new Error((res && res.error) || "train_size_policy failed");
        if (el) el.textContent += "[ui] size policy train job started\n";
        // allow a moment for job to finish (oneshot is usually quick)
        setTimeout(loadSizePolicy, 1000);
      } catch (e) {
        const el = document.getElementById("console");
        if (el) el.textContent += `[ui] size policy ERROR: ${e.message}\n`;
      } finally {
        btnSP.disabled = false;
      }
    });
  }

function wireUI() {

const btnPipeline = document.getElementById("btnRunPipeline");
if (btnPipeline) {
  btnPipeline.addEventListener("click", async () => {
    // STEP 5: HARD manipulation kill-switch (UI enforcement)
    if (_hardBlockActionIfManipulated("pipeline", "GLOBAL")) return;

    if (_isExecutionDegraded() && !OPERATOR_MODE) {
      const ok = confirm(
        "Execution degradation detected.\n\nRunning full pipeline may amplify bad execution.\n\nProceed anyway?"
      );
      if (!ok) return;
    }


    setSelectedJob("pipeline");

    const el = document.getElementById("console");
    if (el) el.textContent = "[ui] starting pipeline...\n";

    try {
      const res = await fetchJSON("/api/pipeline/run");
      if (!res || !res.ok) throw new Error(res?.error || "pipeline failed");

      if (el) el.textContent += "[ui] pipeline finished\n";
      toast("Pipeline finished successfully", "ok");

      loadHealth();
      loadPortfolio();
      loadBroker();
      loadPortfolioBacktestLatest();
    } catch (e) {
      if (el) el.textContent += `[ui] ERROR: ${e.message}\n`;
      toast(`Pipeline error: ${e.message}`, "bad", 4000);
    }
  });
}

    const btnPortBt = document.getElementById("btnRunPortfolioBacktest");
  if (btnPortBt) {
    btnPortBt.addEventListener("click", async () => {
      btnPortBt.disabled = true;
      try {
        await fetchJSON("/api/jobs/start?name=portfolio_backtest");
      } catch {}
      setTimeout(() => {
        btnPortBt.disabled = false;
        loadPortfolioBacktestLatest();
      }, 2000);
    });
  }

document.querySelectorAll("button[data-job]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const name = btn.getAttribute("data-job");
    const action = btn.getAttribute("data-action") || "start";

    // STEP 5: HARD manipulation kill-switch (UI enforcement)
    if (action === "start" && _hardBlockActionIfManipulated(`job:${name}`, "GLOBAL")) return;

    // FORCE CONFIRM ON JOB START when execution is degraded (non-operator mode)
    if (action === "start" && _isExecutionDegraded() && !OPERATOR_MODE) {
      const ok = confirm(
        `Execution degradation detected.\n\nStarting "${name}" may amplify bad execution.\n\nProceed anyway?`
      );
      if (!ok) return;
    }

    // SELECT + FOLLOW LOG IMMEDIATELY
    setSelectedJob(name);
    followJob(name);

    const el = document.getElementById("console");
    if (el) el.textContent = `[ui] starting ${name}...\n`;

    try {
      await jobAction(name, action);
    } catch (e) {
      if (el) el.textContent += `[ui] error: ${e.message || e}\n`;
      setOpsError(`[job] ${name} failed to start`);
    }
  });
});

const btnFix = document.getElementById("btnFixIssues");
if (btnFix) {
  btnFix.addEventListener("click", async () => {
    if (!OPERATOR_MODE) {
      const ok = confirm(
        "This will automatically attempt to fix startup issues:\n" +
        "- Initialize / migrate databases\n" +
        "- Rebuild labels\n" +
        "- Train size policy if missing\n\n" +
        "Proceed?"
      );
      if (!ok) return;
    }

    const el = document.getElementById("console");
    if (el) el.textContent += "[ui] running automatic fix...\n";

    btnFix.disabled = true;
    try {
      const res = await fetchJSON("/api/system/fix");
      if (!res || !res.ok) {
        throw new Error(res?.error || "fix failed");
      }

      if (el) {
        el.textContent += "[ui] automatic fix complete\n";
        if (res.actions) {
          el.textContent += JSON.stringify(res.actions, null, 2) + "\n";
        }
      }

      toast("Automatic fixes applied", "ok", 3500);

      // refresh everything
      await refresh();
      await loadPromotionStatus();
      await loadSizePolicy();

    } catch (e) {
      if (el) el.textContent += `[ui] FIX ERROR: ${e.message}\n`;
      toast(`Fix failed: ${e.message}`, "bad", 4000);
    } finally {
      btnFix.disabled = false;
    }
  });
}

  const btnClear = document.getElementById("btnClear");
  if (btnClear) {
    btnClear.addEventListener("click", () => {
      const el = document.getElementById("console");
      if (el) el.textContent = "";
    });
  }

    const btnBt = document.getElementById("btnLoadPortfolioBT");
  if (btnBt) {
    btnBt.addEventListener("click", async () => {
      btnBt.disabled = true;
      try { await loadPortfolioBacktestLatest(); } finally { btnBt.disabled = false; }
    });
  }

    const btnCh = document.getElementById("btnRunChallenger");
  if (btnCh) {
    btnCh.addEventListener("click", async () => {
      // STEP 5: HARD manipulation kill-switch (UI enforcement)
      if (_hardBlockActionIfManipulated("challenger run", "GLOBAL")) return;

      btnCh.disabled = true;
      try {
        const el = document.getElementById("console");
        if (el) el.textContent = "[ui] running challenger training/eval...\n";
        const res = await fetchJSON("/api/challenger/run");
        if (!res || !res.ok) throw new Error((res && res.error) || "challenger failed");
        if (el) el.textContent += "[ui] challenger complete\n";
        await loadModelRegistry();
      } catch (e) {
        const el = document.getElementById("console");
        if (el) el.textContent += `[ui] challenger ERROR: ${e.message}\n`;
      } finally {
        btnCh.disabled = false;
      }
    });
  }

  const btnRefresh = document.getElementById("btnRefresh");
  if (btnRefresh) {
    btnRefresh.addEventListener("click", refresh);
  }

  // Expert unlock button (separate from operator mode)
  const btnUnlock = document.getElementById("btnExpertUnlock");
  if (btnUnlock) {
    btnUnlock.addEventListener("click", () => {
      if (!EXPERT_UNLOCK) {
        const ok = confirm(
          "Unlock Advanced Actions?\n\nThese actions can increase risk (pipeline/promotions/rollback)."
        );
        if (!ok) return;
      }
      _setExpertUnlock(!EXPERT_UNLOCK);
      toast(EXPERT_UNLOCK ? "Advanced actions unlocked" : "Advanced actions locked", EXPERT_UNLOCK ? "warn" : "dim", 2500);
    });
  }

  // Incident drawer close
  const btnCloseIncident = document.getElementById("btnCloseIncident");
  if (btnCloseIncident) btnCloseIncident.addEventListener("click", closeIncidentDrawer);

  const incidentOverlay = document.getElementById("incidentOverlay");
  if (incidentOverlay) {
    incidentOverlay.addEventListener("click", (e) => {
      // click outside drawer closes
      if (e.target === incidentOverlay) closeIncidentDrawer();
    });
  }

  // apply mode classes immediately
  applyModeToDOM();
  const btnOperator = document.getElementById("btnOperatorMode");
  if (btnOperator) btnOperator.textContent = `👷 Operator Mode: ${OPERATOR_MODE ? "ON" : "OFF"}`;
  const btnUnlock2 = document.getElementById("btnExpertUnlock");
  if (btnUnlock2) btnUnlock2.textContent = `🛡 Unlock Advanced: ${EXPERT_UNLOCK ? "ON" : "OFF"}`;

  // global filters: rerender alerts instantly (no need to wait)
  ["globalRange","globalSev","globalSymbol","globalChangedOnly"].forEach((id)=>{
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener("input", () => {
      const filtered = _filterAlerts(_lastAlerts || []);
(_debouncedRender ||= _debounce((rows)=>{
  renderHeatmap(rows);
  renderIncidentQueue(rows);
}))(filtered);

    });
    el.addEventListener("change", () => {
      const filtered = _filterAlerts(_lastAlerts || []);
(_debouncedRender ||= _debounce((rows)=>{
  renderHeatmap(rows);
  renderIncidentQueue(rows);
}))(filtered);

    });
  });

  const btnOperator2 = document.getElementById("btnOperatorMode");
  if (btnOperator2) {

    btnOperator2.addEventListener("click", async () => {
      OPERATOR_MODE = !OPERATOR_MODE;
      localStorage.setItem("operator_mode", OPERATOR_MODE ? "1" : "0");
      btnOperator.textContent = `👷 Operator Mode: ${OPERATOR_MODE ? "ON" : "OFF"}`;
      applyModeToDOM();
      toast(
        OPERATOR_MODE
          ? "Operator Mode enabled (guided ops view)"
          : "Operator Mode disabled (expert/raw view)",
        OPERATOR_MODE ? "warn" : "dim"
      );
      await refresh();
    });
  }

  const btnJobHist = document.getElementById("btnShowJobHistory");
  if (btnJobHist) {
    btnJobHist.addEventListener("click", async () => {
      const p = document.getElementById("jobHistoryPanel");
      if (!p) return;
      p.style.display = (p.style.display === "none") ? "" : "none";
      await refresh();
    });
  }

  const btnTrends = document.getElementById("btnShowTrends");
  if (btnTrends) {
    btnTrends.addEventListener("click", async () => {
      const p = document.getElementById("confidenceTrendPanel");
      if (!p) return;
      p.style.display = (p.style.display === "none") ? "" : "none";
      await refresh();
    });
  }

  const btnCloseWhy = document.getElementById("btnCloseWhy");
  if (btnCloseWhy) btnCloseWhy.addEventListener("click", closeWhyModal);

  const modal = document.getElementById("whyModal");
  if (modal) {
    modal.addEventListener("click", (e) => {
      if (e.target === modal) closeWhyModal();
    });
  }

  const btnPromoWhy = document.getElementById("btnWhyNotPromoted");
  if (btnPromoWhy) {
    btnPromoWhy.addEventListener("click", openPromoWhyModal);
  }

  const btnClosePromoWhy = document.getElementById("btnClosePromoWhy");
  if (btnClosePromoWhy) btnClosePromoWhy.addEventListener("click", closePromoWhyModal);

  const promoModal = document.getElementById("promoWhyModal");
  if (promoModal) {
    promoModal.addEventListener("click", (e) => {
      if (e.target === promoModal) closePromoWhyModal();
    });
  }
}


async function loadSystemState() {
  const el = document.getElementById("systemStateText");
  if (!el) return;
  try {
    const j = await fetchJSON("/api/system_state");
    el.textContent = JSON.stringify(j, null, 2);
  } catch {}
}

/* -----------------------------
   Boot
----------------------------- */

// ensureExtrasUI is optional; guard if not present
if (typeof ensureExtrasUI === "function") ensureExtrasUI();

wireUI();
wireCollapsibles();
setSelectedJob("poll_prices");
wirePromotionButtons();
refresh();
loadPromotionStatus(),
loadRelevanceStats();
loadPortfolioBacktestLatest();
loadSizePolicy(),
loadSystemState();

loadAlerts();

    // Decision header summary
    const critN = (_lastAlerts || []).filter((a)=>!a.resolved && a.severity === "CRIT").length;
    const warnN = (_lastAlerts || []).filter((a)=>!a.resolved && a.severity === "WARN").length;

    // Data health proxy from pills (set elsewhere in your refresh)
    const hp = document.getElementById("healthPrices")?.textContent || "";
    const hl = document.getElementById("healthLabels")?.textContent || "";
    const dataBad = /stale|missing|error/i.test(hp + " " + hl);
    const dataWarn = /warn/i.test(hp + " " + hl);

    // Model/promotion proxy (uses your existing pill)
    const promo = document.getElementById("promotionPill")?.textContent || "";
    const modelBlocked = /blocked|off|0|false|disable/i.test(promo);

    const execDegraded = _isExecutionDegraded();
    const system =
      (critN > 0 || dataBad) ? "CRIT" :
      (warnN > 0 || dataWarn || execDegraded) ? "WARN" :
      "OK";

    updateDecisionBarFromState({
      system,
      crit: critN,
      warn: warnN,
      data: dataBad ? "BAD" : dataWarn ? "WARN" : "OK",
      model: modelBlocked ? "BLOCKED" : "OK",
      exec: execDegraded ? "DEGRADED" : "OK",
      updated: "just now",
    });


let _refreshRunning = false;
setInterval(async () => {
  if (_refreshRunning) return;
  _refreshRunning = true;
  try { await refresh(); }
  finally { _refreshRunning = false; }
}, 5000);

let _alertsRunning = false;
setInterval(async () => {
  if (_alertsRunning) return;
  _alertsRunning = true;
  try { await loadAlerts(); }
  finally { _alertsRunning = false; }
}, 7000);