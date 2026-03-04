
import {
  esc,
  escapeHTML,
  fmtTime,
  fmtNum,
  _fmtPct,
  _clamp,
  barWidth,
  _debounce
} from "./utils.js";

import {
  drawSpark,
  renderLineChart,
  drawCalibration
} from "./charts.js";

import {
  filterAlerts,
  renderHeatmap,
  renderIncidentQueue,
  severityRank
} from "./alerts.js";

import {
  loadPolicyState,
  saveOperatorMode,
  saveExpertUnlock,
  applyPolicyToDOM,
  requireExpertUnlock,
  requireConfirmIfDegraded
} from "./policy.js";

import { renderKillSwitchPills } from "./kill_switch_ui.js";

import {
  initDecisionBarEngine,
  wireDecisionBarClicks,
  updateDecisionHeader
} from "./decision_bar.js";

import {
  detectExecutionDegradation,
  isExecutionDegraded,
  buildExecutionAlert
} from "./execution_degradation.js";

import {
  updateManipulationStateFromAlerts,
  hardBlockActionIfManipulated
} from "./safety_banner.js";

import {
  initPromotionSafetyEngine,
  maybeAutoResumePromotionsAfterRecovery,
  handlePromotionToggle,
  handleAutoFix
} from "./promotion_safety.js";

import { scheduleRefreshTasks } from "./refresh_scheduler.js";

import {
  isReadOnlyMode,
  setReadOnlyMode,
  applyReadOnlyBanner,
  hardBlockIfReadOnly
} from "./read_only_mode.js";

/* ui/dashboard.js - Market Impact Dashboard controller */

// -----------------------------
// Compatibility + globals (Phase 7-10)
// -----------------------------

// Some older code paths still call these names:
function _isExecutionDegraded() { return !!isExecutionDegraded(); }


async function loadSelfCriticWarnings() {
  const host = document.getElementById("selfCriticWarnings");
  if (!host) return;

  try {
    await postJSON("/api/self_critic/run", {
      lookback_ms: 7 * 24 * 60 * 60 * 1000,
      max_rows: 5000
    });
  } catch {
    // ignore run failures (advisory)
  }

  try {
    const d = await fetchJSON("/api/self_critic/warnings?min_severity=INFO&lookback_ms=86400000&limit=50");
    const items = (d && d.ok && Array.isArray(d.items)) ? d.items : [];
    const rows = items.map(w => {
      const sym = (w.symbol || "").toUpperCase();
      return {
        id: w.id,
        ts_ms: w.ts_ms,
        severity: String(w.severity || "INFO").toUpperCase(),
        symbol: sym || "SC",
        horizon_s: 0,
        event_title: `${w.category}: ${w.title}`,
        resolved: false,
        explain_json: JSON.stringify(w.evidence || {})
      };
    });

    renderIncidentQueue(host, rows, {
      onOpen: (r) => {
        toast(r.event_title || "Self-Critic warning", r.severity === "CRIT" ? "bad" : r.severity === "WARN" ? "warn" : "ok", 4200);
      }
    });
  } catch (e) {
    host.innerHTML = `<div class="small" style="color:var(--muted);">Self-Critic unavailable: ${escapeHTML(e.message || "error")}</div>`;
  }
}

// Used by loadHealth() for localStorage parsing (safe default)
const _EXEC_CONF_STATE_KEY = "exec_conf_state_v1";

// Debounced renderer holder (global filters use it)
let _debouncedRender = null;

// Some older UI paths call applyModeToDOM(); keep it as a thin wrapper.
function applyModeToDOM() {
  applyPolicyToDOM({
    operatorMode: OPERATOR_MODE,
    expertUnlocked: EXPERT_UNLOCK
  });
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
  try {
    data = txt ? JSON.parse(txt) : null;
  } catch (e) {
    throw new Error(`Invalid JSON from ${path}: ${e.message}`);
  }

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

// -----------------------------
// Mode + visibility helpers
// -----------------------------
function _setExpertUnlock(on) {
  EXPERT_UNLOCK = !!on;
  saveExpertUnlock(EXPERT_UNLOCK);
  applyPolicyToDOM({
    operatorMode: OPERATOR_MODE,
    expertUnlocked: EXPERT_UNLOCK
  });
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

// -----------------------------
// Heatmap + incident queue rendering
// -----------------------------
function _scoreCell(rows) {
  // severity * confidence * |z|
  let best = null;
  for (const r of rows) {
    const sevR = severityRank(r.severity);
    const conf = Number(r.confidence);
    const z = Math.abs(Number(r.expected_z));
    if (!Number.isFinite(conf) || !Number.isFinite(z)) continue;
    const score = sevR * conf * (0.6 + Math.min(3.0, z));
    if (!best || score > best.score) best = { score, r };
  }
  return best ? best.r : null;
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

// -----------------------------
// Existing code continues
// -----------------------------

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


let _killSwitchSnapshot = null;

let { operatorMode: OPERATOR_MODE, expertUnlocked: EXPERT_UNLOCK } =
  loadPolicyState();

initPromotionSafetyEngine({
  isExecutionDegraded,
  hardBlockActionIfManipulated,
  toast,
  fetchJSON,
  loadPromotionStatus,
  loadSizePolicy: loadSizePolicyUI,

  refresh,
  getManipBlockedSyms: () => (typeof _manipBlockedSyms !== "undefined" ? _manipBlockedSyms : new Set())
});

applyPolicyToDOM({
  operatorMode: OPERATOR_MODE,
  expertUnlocked: EXPERT_UNLOCK
});

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

// ---------- Execution degradation detection ----------

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

async function loadSizePolicyUI() {

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
        <td class="mono">${fmt(r.conf_lo,2)}-${fmt(r.conf_hi,2)}</td>
        <td class="mono">${r.n}</td>
        <td class="mono">${fmt(r.mean_net_ret,6)}</td>
        <td class="mono">${fmt(r.std_net_ret,6)}</td>
        <td class="mono">${
  isExecutionDegraded()
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
      renderLineChart(canvas, []);
      return;
    }

    const pts = res.points;
    if (!pts.length) {
      meta.textContent = "empty";
      meta.className = "pill dim";
      renderLineChart(canvas, []);
      return;
    }

    meta.textContent = "live";
    meta.className = "pill ok";

    // Plot % drift (preferred for scale)
    const ys = pts.map(p => Number(p.diff_equity_pct)).filter(Number.isFinite);

    renderLineChart(canvas, ys, {
      topLabel: "equity drift (%)",
      fmtY: (v) => `${(v * 100).toFixed(2)}%`,
      stroke: "#d29922", // amber
      yMax: Math.max(0.01, Math.max(...ys, 0)),
      yMin: Math.min(-0.01, Math.min(...ys, 0)),
    });

  } catch (e) {
    meta.textContent = "error";
    meta.className = "pill bad";
    renderLineChart(canvas, []);
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
    const execAlerts = detectExecutionDegradation(rows, toast);
    for (const a of execAlerts || []) {

      toast(
        `${a.level}: execution degraded for ${a.symbol} (+${(a.worsenPct * 100).toFixed(1)}%)`,
        a.level === "CRIT" ? "bad" : "warn",
        a.level === "CRIT" ? 7000 : 5000
      );
      buildExecutionAlert(a);

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
        <td class="mono">${lo}-${hi}</td>
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

// last successful /api/health snapshot (for decision bar derivation)
let _lastHealth = null;

// Decision bar engine (Phase 7)
initDecisionBarEngine({
  getLastAlerts: () => _lastAlerts,
  getLastHealth: () => _lastHealth,
  isExecutionDegraded: isExecutionDegraded
});


// explain_json is fetched on-demand via /api/alerts/by_id

async function loadAlerts() {
  // prefer timeline endpoint when available
  const data = await fetchJSON("/api/alerts/timeline?limit=50");
  const rows =
    (data && data.ok && Array.isArray(data.items))
      ? data.items
      : [];

_lastAlerts = rows || [];

  // STEP 5: update manipulation kill-switch state (from existing alerts stream)
  updateManipulationStateFromAlerts(_lastAlerts);

  // keep visuals + decision state in sync
  const filtered = filterAlerts(
  _lastAlerts || [],
  _getGlobalFilters(),
  {
    isAcked: _isAckedLocal,
    isSnoozed: _isSnoozedLocal
  }
);
  renderHeatmap(
  document.getElementById("alertsHeatmap"),
  filtered,
  (sym) => {
    document.getElementById("globalSymbol").value = sym;
  }
);

renderIncidentQueue(
  document.getElementById("incidentList"),
  filtered,
  { onOpen: openIncidentDrawer }
);

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
  }

  tbody.scrollTop = 0;

  tbody.querySelectorAll("button[data-why]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = Number(btn.getAttribute("data-why"));
      const row = _lastAlerts.find(a => Number(a.id) === id);
      if (row) openWhyModal(row);
    });
  });

  // decision header follows alerts
  updateDecisionHeader(fmtTime(Date.now()));
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

  // automatically fetch an AI ops explanation when health loads
  setTimeout(loadOpsExplain, 0);
}


// ------------------------------------------------------------------
// AI Ops explanation helpers
// ------------------------------------------------------------------
async function loadOpsExplain() {
  const target = document.getElementById("opsExplainText");
  if (!target) return;
  target.textContent = "(loading...)";
  try {
    const res = await fetchJSON("/api/ai/ops_explain");
    if (res && res.ok && res.explanation) {
      target.textContent = res.explanation;
    } else if (res && res.error) {
      target.textContent = `error: ${res.error}`;
    } else {
      target.textContent = "no explanation available";
    }
  } catch (e) {
    target.textContent = `error: ${e.message || e}`;
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
  drawCalibration(canvas, out.points);
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
      label.textContent = `${b.lo.toFixed(1)}-${b.hi.toFixed(1)}`;

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

async function loadCrashAnalytics() {
  const el = document.getElementById("jobHistory");
  if (!el) return;

  try {
    const j = await fetchJSON("/api/crash_analytics?limit=50");
    if (!j || !j.ok || !Array.isArray(j.rows)) return;

    const lines = [];
    lines.push("=== Crash Analytics ===");
    for (const r of j.rows) {
      lines.push(
        `${fmtTime(r.ts_ms)} | ${r.job_name} | rc=${r.exit_code} | ${r.reason || ""}`
      );
    }

    el.textContent += "\n\n" + lines.join("\n");

  } catch (_) {}
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
      renderLineChart(cEq, []);
      renderLineChart(cDd, []);
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
    renderLineChart(cEq, equity, {
      topLabel: "equity",
      fmtY: (v) => Number(v).toFixed(3),
      stroke: "#2ea043",
    });

    renderLineChart(cDd, dd, {
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
    renderLineChart(cEq, []);
    renderLineChart(cDd, []);
  }
}

/* -----------------------------
   Actions
----------------------------- */
async function loadSystemState() {
  const el = document.getElementById("systemStateText");
  if (!el) return;

  try {
    const state = await fetchJSON("/api/system/state");

    let killSwitches = null;
    try {
      const k = await fetchJSON("/api/system/kill_switches");
      if (k && k.ok) killSwitches = k.kill_switches || null;
    } catch {
      killSwitches = null;
    }

    _killSwitchSnapshot = killSwitches;
if (typeof renderKillSwitchPills === "function") {
  renderKillSwitchPills(killSwitches);
}

    const out = {
      system_state: state,
      kill_switches: killSwitches
    };

    el.textContent = JSON.stringify(out.system_state, null, 2);
  } catch (e) {
    el.textContent = `[error] ${e.message || String(e)}`;
  }
}

async function loadSupervisorStatus() {
  const pill = document.getElementById("supervisorPill");
  const raw  = document.getElementById("supervisorRaw");
  if (!pill || !raw) return;

  try {
    const j = await fetchJSON("/api/supervisor/status");
    if (!j || !j.ok) throw new Error((j && j.error) || "supervisor unavailable");

    pill.className = "pill " + (j.enabled ? "ok" : "dim");
    pill.textContent = j.enabled ? "supervisor: ON" : "supervisor: OFF";
    raw.textContent = JSON.stringify(j, null, 2);
  } catch (e) {
    pill.className = "pill bad";
    pill.textContent = "supervisor: error";
    raw.textContent = e.message || String(e);
  }
}

// -----------------------------
// Structured Readiness + Telemetry
// -----------------------------
async function loadStructuredReadiness() {
  const el = document.getElementById("systemStateText");
  const banner = document.getElementById("systemStateBanner");
  if (!el) return;

  try {
    const state = await fetchJSON("/api/system/state");
    if (!state) return;

    const lines = [];
    lines.push(`state: ${state.state}`);
    lines.push(`ok: ${state.ok}`);
    lines.push(`ts_ms: ${state.ts_ms}`);
    lines.push("");

    if (Array.isArray(state.reasons) && state.reasons.length) {
      lines.push("reasons:");
      for (const r of state.reasons) {
        lines.push(`  - ${r}`);
      }
      lines.push("");
    }

    if (state.jobs) {
      lines.push("running_daemons:");
      for (const j of (state.jobs.running_daemons || [])) {
        lines.push(`  - ${j}`);
      }

      lines.push("running_oneshots:");
      for (const j of (state.jobs.running_oneshots || [])) {
        lines.push(`  - ${j}`);
      }
    }

    el.textContent = lines.join("\n");

    // ---------- Institutional Banner State ----------
    if (banner) {
      banner.textContent = state.state || "UNKNOWN";

      banner.className = "pill ";

      if (state.state === "LIVE") {
        banner.className += "ok";
      } else if (state.state === "DEGRADED") {
        banner.className += "warn";
      } else if (state.state === "KILL_SWITCH") {
        banner.className += "crit";
      } else {
        banner.className += "dim";
      }
    }

  } catch (e) {
    el.textContent = `[readiness error] ${e.message || e}`;
  }
}

async function loadTelemetry() {
  const strip = document.getElementById("telemetryStrip");
  if (!strip) return;

  try {
    const t = await fetchJSON("/api/telemetry");
    if (!t || !t.ok) throw new Error("telemetry unavailable");

    const cpu = document.getElementById("tCpu");
    const ram = document.getElementById("tRam");
    const db  = document.getElementById("tDb");

    if (cpu) cpu.textContent = `CPU ${Number(t.cpu_percent || 0).toFixed(1)}%`;
    if (ram) ram.textContent = `RAM ${Number(t.process_rss_mb || 0).toFixed(0)}MB`;
    if (db)  db.textContent  = `DB ${Number(t.db_size_mb || 0).toFixed(1)}MB`;

  } catch (e) {
    const cpu = document.getElementById("tCpu");
    const ram = document.getElementById("tRam");
    const db  = document.getElementById("tDb");

    if (cpu) cpu.textContent = "CPU —";
    if (ram) ram.textContent = "RAM —";
    if (db)  db.textContent  = "DB —";
  }
}

async function jobAction(name, action) {
  setSelectedJob(name);
  if (action === "start") {
    await fetchJSON(`/api/jobs/start?name=${encodeURIComponent(name)}`);
  } else if (action === "stop") {
    await fetchJSON(`/api/jobs/stop?name=${encodeURIComponent(name)}`);
  }
  await refresh();
applyReadOnlyBanner();
}

async function loadExecutionBarrier() {
  const pill = document.getElementById("execBarrierPill");
  const raw  = document.getElementById("execBarrierRaw");
  if (!pill || !raw) return;

  const root = document.documentElement;

  try {
    const j = await fetchJSON("/api/execution/barrier");
    if (!j || !j.ok) throw new Error((j && j.error) || "barrier unavailable");

    pill.className = "pill " + (j.allowed ? "ok" : "bad");
    pill.textContent = j.allowed ? "execution: ALLOWED" : "execution: BLOCKED";
    raw.textContent = JSON.stringify(j, null, 2);

    root.style.setProperty("--exec-blocked", j.allowed ? "0" : "1");

  } catch (e) {
    pill.className = "pill bad";
    pill.textContent = "execution: error";
    raw.textContent = e.message || String(e);
    root.style.setProperty("--exec-blocked", "1"); // fail closed
  }
}

async function refresh() {
try {
    const health = await fetchJSON("/api/health");

    // snapshot for decision derivation
    _lastHealth = health || null;

    let trainingStatus = null;
    try {
      trainingStatus = await fetchJSON("/api/training_status");
    } catch (e) {
      trainingStatus = { mode: "unknown", allowed: false };
    }

    const hEl = document.getElementById("healthStatus");
    const hDet = document.getElementById("healthDetails");

    let systemState = null;
    try {
      systemState = await fetchJSON("/api/system/state");
    } catch {}

    const degraded =
      !systemState ||
      systemState.state !== "LIVE" ||
      _isExecutionDegraded();

    setStatus(
      hEl,
      !degraded,
      degraded ? "DEGRADED" : "LIVE"
    );

    const root = document.documentElement;
    if (degraded) {
      root.classList.add("system-degraded");
    } else {
      root.classList.remove("system-degraded");
    }

    hDet.textContent = JSON.stringify(health, null, 2);

    // keep decision header fresh even if alerts haven’t ticked yet
    updateDecisionHeader("just now");
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

  const explBtn = document.getElementById("btnExplain");
  if (explBtn) {
    explBtn.addEventListener("click", loadOpsExplain);
  }

await Promise.allSettled([
  loadHealth(),
  loadStructuredReadiness(),
  loadTelemetry(),
  loadTemporalEval(),
  loadTemporalShadowEval(),
  loadSocialPressure(),
  loadSocialRegimes(),
  loadExecutionBarrier(),
  loadSocialBlocks(),
  loadSupervisorStatus(),
  loadPromotionAudit(),
  refreshCalibCurves(),
  loadModelRegistry(),
  loadValidation(),
  loadStrategyStatus(),
  loadStrategyMetrics(),
  loadModelMetrics(),
  loadJobs(),
  loadLog(),
  loadMarketStress(),
  loadMarketStressHistory(),
  loadExecutionOverlays(),
  loadModelDiagnostics(),
  loadConfidenceMass(),
  loadJobHistory(),
  loadConfidenceTrends(),
  loadRelevanceStats(),
  loadDecisions(),
  loadExecutionByConfidence(),
  loadPromotionStatus(),
  loadSelfCriticWarnings(),

  // portfolio layer
  (typeof loadPortfolio === "function") ? loadPortfolio() : Promise.resolve(),
  (typeof loadBroker === "function") ? loadBroker() : Promise.resolve(),
  loadPortfolioBacktestLatest(),
  loadEquityReconciliation(),
  loadEquityDrift(),
]);

  // If we paused promotions due to exec degradation, try to resume after recovery
await maybeAutoResumePromotionsAfterRecovery({
  operatorMode: OPERATOR_MODE
});

// --- Telemetry Strip Update (Institutional density layer) ---
try {
  const tNav = document.getElementById("tNav");
  if (!tNav) {
    // telemetry not present; do nothing (do not exit refresh)
  } else {

  const navEl = document.getElementById("portfolioMeta");
  const stressEl = document.getElementById("marketStressHeader");
  const promoEl = document.getElementById("promotionPill");

  const tReturn = document.getElementById("tReturn");
  const tDD = document.getElementById("tDD");
  const tSharpe = document.getElementById("tSharpe");
  const tCalmar = document.getElementById("tCalmar");
  const tStress = document.getElementById("tStress");
  const tPromotion = document.getElementById("tPromotion");

  if (navEl) tNav.textContent = navEl.textContent || "NAV —";
  if (stressEl && tStress) tStress.textContent = stressEl.textContent || "Stress —";
  if (promoEl && tPromotion) tPromotion.textContent = promoEl.textContent || "Promo —";

  const summaryRow = document.querySelector("#portfolioBtSummaryBody tr");
  if (summaryRow) {
    const cells = summaryRow.querySelectorAll("td");
    if (cells.length >= 3) {
      if (tReturn) tReturn.textContent = "RET " + (cells[1]?.textContent || "—");
      if (tDD) tDD.textContent = "DD " + (cells[2]?.textContent || "—");
      if (tSharpe) tSharpe.textContent = cells[3]?.textContent || "Sharpe —";
    }
  }
  } // close telemetry else-block
} catch (e) {
  console.warn("Telemetry update failed", e);
}

// Load Executive Overview data
loadExecutiveOverview();

} // <-- END refresh()

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
      if (hardBlockIfReadOnly({ actionName: "toggle promotions", toastFn: toast })) return;

      // STEP 5: HARD manipulation kill-switch (UI enforcement)
      if (hardBlockActionIfManipulated({
        actionName: "toggle promotions",
        symbol: "GLOBAL",
        expertUnlocked: EXPERT_UNLOCK,
        toastFn: toast
      })) return;

      if (!requireConfirmIfDegraded({
        executionDegraded: _isExecutionDegraded(),
        operatorMode: OPERATOR_MODE,
        message:
          "Execution degradation detected.\n\nToggling promotions during degradation may increase risk.\n\nProceed anyway?"
      })) return;

      btnToggle.disabled = true;
      try {
        await handlePromotionToggle({
          operatorMode: OPERATOR_MODE,
          expertUnlocked: EXPERT_UNLOCK
        });
        await loadPromotionStatus();
      } catch (e) {
        const el = document.getElementById("console");
        if (el) el.textContent += `[ui] toggle promotions ERROR: ${e.message}\n`;
        toast(`Toggle promotions failed: ${e.message}`, "bad", 4000);
      } finally {
        btnToggle.disabled = false;
      }
    });
  }

  if (btnRollback) {
    btnRollback.addEventListener("click", async () => {
      if (hardBlockIfReadOnly({ actionName: "champion rollback", toastFn: toast })) return;

      // STEP 5: HARD manipulation kill-switch (UI enforcement)
      if (hardBlockActionIfManipulated({
        actionName: "champion rollback",
        symbol: "GLOBAL",
        expertUnlocked: EXPERT_UNLOCK,
        toastFn: toast
      })) return;

      if (!requireConfirmIfDegraded({
        executionDegraded: _isExecutionDegraded(),
        operatorMode: OPERATOR_MODE,
        message:
          "Execution degradation detected.\n\nRollback during degradation may be unsafe.\n\nProceed anyway?"
      })) return;

      btnRollback.disabled = true;
      try {
        const res = await fetchJSON("/api/champion/rollback");
        if (!res || !res.ok) throw new Error((res && res.error) || "rollback failed");

        const el = document.getElementById("console");
        if (el) el.textContent += `[ui] rollback ok -> ${JSON.stringify(res.champion || {})}\n`;

        toast("Rollback complete", "ok", 3000);
        await loadModelRegistry();
      } catch (e) {
        const el = document.getElementById("console");
        if (el) el.textContent += `[ui] rollback ERROR: ${e.message}\n`;
        toast(`Rollback failed: ${e.message}`, "bad", 4000);
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
   Boot (single authoritative entrypoint)
----------------------------- */

function wireUI() {
  wireDecisionBarClicks();
  wireCollapsibles();
  wirePromotionButtons();
  wirePromotionExplainUI();

  // -----------------------------
  // Size policy training button
  // -----------------------------
  const btnSP = document.getElementById("btnTrainSizePolicy");
  if (btnSP) {
    btnSP.addEventListener("click", async () => {
      if (hardBlockIfReadOnly({ actionName: "train size policy", toastFn: toast })) return;

      btnSP.disabled = true;
      try {
        const el = document.getElementById("console");
        if (el) el.textContent += "[ui] training size policy...\n";

        const res = await fetchJSON("/api/size_policy/train");
        if (!res || !res.ok) {
          throw new Error((res && res.error) || "train_size_policy failed");
        }

        if (el) el.textContent += "[ui] size policy train job started\n";
        setTimeout(loadSizePolicyUI, 1000);
      } catch (e) {
        const el = document.getElementById("console");
        if (el) el.textContent += `[ui] size policy ERROR: ${e.message}\n`;
      } finally {
        btnSP.disabled = false;
      }
    });
  }
}

// Executive Overview Functions
async function loadExecutiveOverview() {
  try {
    const data = await fetchJSON("/api/ui/overview");
    if (!data || !data.ok) return;
    
    // Update system health badge
    const healthBadge = document.getElementById("execHealthBadge");
    if (healthBadge) {
      healthBadge.textContent = data.system_health.toUpperCase();
      healthBadge.className = "pill";
      if (data.system_health === "healthy") {
        healthBadge.classList.add("ok");
      } else if (data.system_health === "degraded") {
        healthBadge.classList.add("warn");
      } else if (data.system_health === "paused") {
        healthBadge.classList.add("dim");
      }
    }
    
    // Update last decision timestamp
    const lastDecision = document.getElementById("execLastDecision");
    if (lastDecision && data.last_decision_timestamp_ms) {
      const ago = Math.floor((Date.now() - data.last_decision_timestamp_ms) / 60000);
      lastDecision.textContent = ago <= 1 ? "just now" : `${ago}m ago`;
    }
    
    // Update market environment
    const marketDesc = document.getElementById("execMarketDesc");
    const marketConf = document.getElementById("execMarketConf");
    if (marketDesc && data.market_environment) {
      marketDesc.textContent = data.market_environment.description || "Unknown";
    }
    if (marketConf && data.market_environment) {
      marketConf.textContent = `Confidence: ${data.market_environment.confidence || "Unknown"}`;
    }
    
    // Update capital at risk
    const capitalRisk = document.getElementById("execCapitalRisk");
    if (capitalRisk && data.capital_at_risk_today !== undefined) {
      capitalRisk.textContent = `$${(data.capital_at_risk_today / 1000).toFixed(0)}K`;
    }
    
    // Update strategy counts
    const stratActive = document.getElementById("stratActive");
    const stratPaused = document.getElementById("stratPaused");
    const stratTesting = document.getElementById("stratTesting");
    if (data.strategy_counts) {
      if (stratActive) stratActive.textContent = data.strategy_counts.active || 0;
      if (stratPaused) stratPaused.textContent = data.strategy_counts.paused || 0;
      if (stratTesting) stratTesting.textContent = data.strategy_counts.testing || 0;
    }
    
    // Update alert counts
    const alertsCritical = document.getElementById("alertsCritical");
    const alertsWarning = document.getElementById("alertsWarning");
    const alertsInfo = document.getElementById("alertsInfo");
    if (data.alert_severity_counts_24h) {
      if (alertsCritical) alertsCritical.textContent = data.alert_severity_counts_24h.critical || 0;
      if (alertsWarning) alertsWarning.textContent = data.alert_severity_counts_24h.warning || 0;
      if (alertsInfo) alertsInfo.textContent = data.alert_severity_counts_24h.info || 0;
    }
    
  } catch (error) {
    console.warn("Failed to load Executive Overview:", error);
  }
}

function bootDashboard() {
  const bootStage = document.getElementById("bootStage");
  const setStage = (s) => { if (bootStage) bootStage.textContent = s; };

  setStage("BOOTING");

  if (typeof wireUI === "function") {
    wireUI();
    setStage("UI WIRED");
  }

  if (typeof wireVoiceUI === "function") {
    wireVoiceUI();
    setStage("VOICE READY");
  }

  applyReadOnlyBanner();
  setStage("POLICY APPLIED");

refresh().then(async () => {
  try {
    const st = await fetchJSON("/api/system/state");
    setStage(st && st.state ? st.state : "UNKNOWN");
  } catch {
    setStage("ERROR");
  }
  
  // Load Executive Overview
  loadExecutiveOverview();
});

  // Auto voice summary (CRIT alerts)
  setTimeout(() => {
    if (sessionStorage.getItem("voice_autosummary_done")) return;

    const rows = Array.isArray(_lastAlerts) ? _lastAlerts : [];
    const crits = rows.filter(r => r.severity === "CRIT" && !r.resolved);

    if (crits.length > 0 && typeof _sayAndToast === "function") {
      _sayAndToast(
        `Attention. ${crits.length} critical alert${crits.length > 1 ? "s" : ""} detected.`,
        "warn",
        6000
      );
    }

    sessionStorage.setItem("voice_autosummary_done", "1");
  }, 800);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", bootDashboard);
} else {
  bootDashboard();
}

function renderDockDetail(strategy) {
  const dock = document.getElementById("right-dock");
  if (!dock || !strategy) return;

  // Escape everything because this is innerHTML
  const safe = (v) => escapeHTML(v === null || v === undefined ? "" : String(v));

  dock.innerHTML = `
    <h3>${safe(strategy.name)}</h3>
    <div>Return: ${safe(strategy.return)}</div>
    <div>Drawdown: ${safe(strategy.drawdown)}</div>
    <div>Decay Sharpe: ${safe(strategy.decay_sharpe)}</div>
    <div>Slippage: ${safe(strategy.slippage_pct)}%</div>
    <div>Capital Efficiency: ${safe(strategy.cap_eff)}</div>
    <div>Regime Fit: ${safe(strategy.regime_fit)}%</div>
    <div>Promotion Streak: ${safe(strategy.promotion_streak)}</div>
  `;
}

function renderEventLog(events) {
  const log = document.getElementById("bottom-log");
  if (!log || !Array.isArray(events)) return;
  log.innerHTML = "";

  events.slice(-50).reverse().forEach(e => {
    const row = document.createElement("div");
    row.innerText = `${new Date(e.ts).toLocaleTimeString()} | ${e.type} | ${e.message}`;
    log.appendChild(row);
  });
}

// -----------------------------
// Decisions UI
// -----------------------------
async function loadDecisions() {
  const container = document.getElementById("decisionsContainer");
  const loading = document.getElementById("decisionsLoading");
  const empty = document.getElementById("decisionsEmpty");
  
  if (!container || !loading) return;
  
  try {
    loading.style.display = "block";
    container.style.display = "none";
    empty.style.display = "none";
    
    const response = await fetchJSON("/api/ui/decisions");
    
    if (!response.ok) {
      throw new Error(response.error || "Failed to load decisions");
    }
    
    const decisions = response.decisions || [];
    
    if (decisions.length === 0) {
      loading.style.display = "none";
      empty.style.display = "block";
      return;
    }
    
    container.innerHTML = "";
    decisions.forEach(decision => {
      container.appendChild(createDecisionCard(decision));
    });
    
    loading.style.display = "none";
    container.style.display = "grid";
    
  } catch (error) {
    loading.textContent = `Error: ${error.message}`;
    console.error("loadDecisions error:", error);
  }
}

function createDecisionCard(decision) {
  const card = document.createElement("div");
  card.className = "incidentItem";
  card.style.cursor = "pointer";
  
  // Set severity rail color based on risk impact
  const riskColor = decision.risk_impact === "high" ? "var(--crit)" : 
                   decision.risk_impact === "medium" ? "var(--warn)" : 
                   "var(--ok)";
  card.style.setProperty("--risk-color", riskColor);
  
  // Action badge styling
  const actionColor = decision.action === "increase" ? "var(--ok)" : 
                     decision.action === "reduce" ? "var(--crit)" : 
                     "var(--muted)";
  
  card.innerHTML = `
    <div class="incidentTop">
      <div style="display:flex; align-items:center; gap:8px;">
        <span class="pill" style="background: ${actionColor}; color: white; border: none;">
          ${decision.action.toUpperCase()}
        </span>
        <span class="incidentTitle">${decision.symbol}</span>
        <span class="pill dim">${(decision.size_delta * 100).toFixed(1)}%</span>
      </div>
      <div class="incidentActions">
        <button class="btn btnSmall" onclick="event.stopPropagation(); window.decisionExplainer.showDecisionExplain('${decision.decision_id}')" title="Explain This Decision">
          🔍 Explain
        </button>
        <span class="pill dim">${fmtTime(decision.ts_ms)}</span>
      </div>
    </div>
    <div class="incidentSub">
      <div>${decision.why}</div>
      <div style="display:flex; gap:12px; margin-top:4px;">
        <span>Certainty: ${(decision.certainty * 100).toFixed(0)}%</span>
        <span>Risk: ${decision.risk_impact}</span>
      </div>
    </div>
  `;
  
  // Keep existing click handler for legacy modal
  card.onclick = (e) => {
    if (!e.target.closest('button')) {
      openDecisionModal(decision.decision_id);
    }
  };
  
  // Add custom severity rail
  card.style.position = "relative";
  card.style.paddingLeft = "20px";
  
  const rail = document.createElement("div");
  rail.style.cssText = `
    position: absolute;
    left: 0;
    top: 8px;
    bottom: 8px;
    width: 4px;
    border-radius: 4px;
    background: ${riskColor};
  `;
  card.appendChild(rail);
  
  return card;
}

async function openDecisionModal(decisionId) {
  const modal = document.getElementById("decisionModal");
  if (!modal) return;
  
  try {
    modal.style.display = "flex";
    
    const response = await fetchJSON(`/api/ui/decision?decision_id=${decisionId}`);
    
    if (!response.ok) {
      throw new Error(response.error || "Failed to load decision details");
    }
    
    const decision = response.decision;
    populateDecisionModal(decision);
    
  } catch (error) {
    console.error("openDecisionModal error:", error);
    modal.style.display = "none";
    toast(`Error loading decision: ${error.message}`, "bad");
  }
}

function populateDecisionModal(decision) {
  // Summary section
  document.getElementById("decisionAction").textContent = decision.action.toUpperCase();
  document.getElementById("decisionSymbol").textContent = decision.symbol;
  document.getElementById("decisionSizeDelta").textContent = `${(decision.size_delta * 100).toFixed(2)}%`;
  document.getElementById("decisionCertainty").textContent = `${(decision.certainty * 100).toFixed(0)}%`;
  document.getElementById("decisionRiskImpact").textContent = decision.risk_impact;
  document.getElementById("decisionWhy").textContent = decision.why;
  
  // Allocation before/after
  document.getElementById("allocationBefore").textContent = `${(decision.allocation_before_after.before * 100).toFixed(2)}%`;
  document.getElementById("allocationAfter").textContent = `${(decision.allocation_before_after.after * 100).toFixed(2)}%`;
  document.getElementById("allocationChange").textContent = `${(decision.allocation_before_after.change * 100).toFixed(2)}%`;
  
  // Inputs summary
  document.getElementById("inputsCurrentSide").textContent = decision.inputs_summary.current_side || "—";
  document.getElementById("inputsCurrentWeight").textContent = `${(decision.inputs_summary.current_weight * 100).toFixed(2)}%`;
  document.getElementById("inputsFromWeight").textContent = `${(decision.inputs_summary.from_weight * 100).toFixed(2)}%`;
  document.getElementById("inputsToWeight").textContent = `${(decision.inputs_summary.to_weight * 100).toFixed(2)}%`;
  
  // Model versions
  const modelVersions = decision.model_versions.length > 0 ? decision.model_versions.join(", ") : "No models";
  document.getElementById("decisionModelVersions").textContent = modelVersions;
  
  // Risk gates
  const riskGates = decision.risk_gates_triggered.length > 0 ? 
    decision.risk_gates_triggered.map(gate => `<span class="pill warn">${gate}</span>`).join(" ") :
    "<span style='color: var(--muted);'>No risk gates triggered</span>";
  document.getElementById("decisionRiskGates").innerHTML = riskGates;
  
  // Decision logs
  const logsContainer = document.getElementById("decisionLogs");
  if (decision.decision_logs.length > 0) {
    logsContainer.innerHTML = decision.decision_logs.map(log => `
      <div style="margin-bottom: 8px; padding: 8px; background: var(--panel2); border-radius: 6px;">
        <div style="font-weight: 600; margin-bottom: 4px;">${log.model_name} (${log.model_kind})</div>
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; font-size: 11px;">
          <div>Predicted Z: ${log.predicted_z.toFixed(3)}</div>
          <div>Confidence: ${(log.confidence * 100).toFixed(0)}%</div>
          <div>Model TS: ${fmtTime(log.model_ts_ms)}</div>
          <div>Features: ${Object.keys(log.features).length} items</div>
        </div>
      </div>
    `).join("");
  } else {
    logsContainer.innerHTML = "<span style='color: var(--muted);'>No decision logs available</span>";
  }
}

function closeDecisionModal() {
  const modal = document.getElementById("decisionModal");
  if (modal) modal.style.display = "none";
}
