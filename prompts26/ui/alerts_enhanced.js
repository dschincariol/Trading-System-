"use strict";

/*
  ui/alerts.js — Alert rendering + filtering engine with human-alignment AI integration
  Extracted from ui/dashboard.js (Phase 4)
  Enhanced with interaction tracking and adaptive alerting
*/

// -----------------------------
// Global state for interaction tracking
// -----------------------------
let currentOperatorId = null;
let currentSessionId = null;

// Initialize session tracking
function initializeInteractionTracking(operatorId) {
  currentOperatorId = operatorId || `operator_${Date.now()}`;
  currentSessionId = `session_${Date.now()}`;
  console.log(`Initialized interaction tracking for ${currentOperatorId}`);
}

// -----------------------------
// Severity helpers
// -----------------------------
export function severityRank(s) {
  if (!s) return 0;
  if (s === "CRIT") return 3;
  if (s === "WARN") return 2;
  if (s === "INFO") return 1;
  return 0;
}

export function cellColor(r) {
  if (!r) return { cls: "dim", sw: "#2f3640" };
  if (r.resolved) return { cls: "ok", sw: "#2ea043" };
  if (r.severity === "CRIT") return { cls: "crit", sw: "#ff6b6b" };
  if (r.severity === "WARN") return { cls: "warn", sw: "#d29922" };
  return { cls: "ok", sw: "#58a6ff" };
}

// -----------------------------
// Interaction tracking
// -----------------------------
export async function trackAlertInteraction(alertId, interactionType, context = {}) {
  if (!currentOperatorId) {
    console.warn("No operator ID set for interaction tracking");
    return false;
  }

  try {
    const response = await fetch('/api/alerts/interaction', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        alert_id: parseInt(alertId),
        interaction_type: interactionType,
        operator_id: currentOperatorId,
        session_id: currentSessionId,
        context: context
      })
    });

    if (response.ok) {
      console.log(`Tracked ${interactionType} for alert ${alertId}`);
      return true;
    } else {
      console.error(`Failed to track interaction: ${response.statusText}`);
      return false;
    }
  } catch (error) {
    console.error('Error tracking alert interaction:', error);
    return false;
  }
}

// -----------------------------
// Enhanced filtering with relevance
// -----------------------------
export function filterAlerts(rows, filters, localState) {
  const minRank =
    filters.sev === "ALL" ? 0 : severityRank(filters.sev);

  const sinceMs = Date.now() - filters.rangeMs;
  const out = [];

  for (const r of rows || []) {
    const ts = Number(r.ts_ms);
    if (Number.isFinite(ts) && ts < sinceMs) continue;

    const sym = String(r.symbol || "").toUpperCase();
    if (filters.sym && sym !== filters.sym) continue;

    if (severityRank(r.severity) < minRank) continue;

    // Filter by relevance score if available
    if (filters.minRelevance && r.relevance_score && r.relevance_score < filters.minRelevance) {
      continue;
    }

    if (filters.changedOnly) {
      if (r.resolved) continue;
      if (localState.isAcked(r.id)) continue;
      if (localState.isSnoozed(r.id)) continue;
    } else {
      if (localState.isSnoozed(r.id)) continue;
    }

    out.push(r);
  }

  return out;
}

// -----------------------------
// Heatmap with relevance overlay
// -----------------------------
function scoreCell(rows) {
  let best = null;
  for (const r of rows || []) {
    const sev = severityRank(r.severity);
    const conf = Number(r.confidence);
    const z = Math.abs(Number(r.expected_z));
    const relevance = Number(r.relevance_score) || 0.5;
    
    if (!Number.isFinite(conf) || !Number.isFinite(z)) continue;

    // Weight by relevance score
    const score = sev * conf * (0.6 + Math.min(3.0, z)) * (0.5 + relevance);
    if (!best || score > best.score) best = { score, r };
  }
  return best ? best.r : null;
}

export function renderHeatmap(host, rows, onSelectSymbol) {
  if (!host) return;

  const horizons = ["60", "300", "3600", "14400"];
  const labels = { "60": "1m", "300": "5m", "3600": "1h", "14400": "4h" };

  const by = {};
  for (const r of rows || []) {
    const sym = String(r.symbol || "").toUpperCase();
    if (!sym) continue;
    const hs = Number(r.horizon_s);
    if (!Number.isFinite(hs)) continue;

    let b = "3600";
    if (hs <= 120) b = "60";
    else if (hs <= 900) b = "300";
    else if (hs <= 7200) b = "3600";
    else b = "14400";

    (by[sym] ||= {})[b] ||= [];
    by[sym][b].push(r);
  }

  const syms = Object.keys(by).sort().slice(0, 22);

  host.innerHTML = "";

  const mk = (cls, html) => {
    const d = document.createElement("div");
    d.className = `hmCell ${cls || ""}`;
    d.innerHTML = html;
    return d;
  };

  host.appendChild(mk("hmHead", "<span class='mono'>symbol</span>"));
  horizons.forEach(h =>
    host.appendChild(
      mk("hmHead", `<span class='mono'>${labels[h]}</span>`)
    )
  );

  for (const sym of syms) {
    host.appendChild(mk("hmSym", `<span class='mono'>${sym}</span>`));

    for (const h of horizons) {
      const best = scoreCell(by[sym][h] || []);
      const c = cellColor(best);

      const relevanceIndicator = best && best.relevance_score ? 
        `<span class="relevance-indicator" style="opacity: ${best.relevance_score}">●</span>` : "";

      const cell = mk(
        "",
        `<span class="hmSwatch" style="background:${c.sw};"></span>
         <span class="mono">${sym}</span>
         <span class="hmMeta">${best ? best.severity : "—"}</span>
         ${relevanceIndicator}`
      );

      cell.addEventListener("click", () => {
        if (onSelectSymbol) onSelectSymbol(sym);
      });

      host.appendChild(cell);
    }
  }
}

// -----------------------------
// Enhanced incident queue with interaction tracking
// -----------------------------
export function renderIncidentQueue(host, rows, opts) {
  if (!host) return;
  host.innerHTML = "";

  if (!rows.length) {
    host.innerHTML =
      `<div class="small" style="color:var(--muted);">
        No alerts in the selected window.
       </div>`;
    return;
  }

  rows
    .slice()
    .sort((a, b) => {
      const sa = severityRank(a.severity);
      const sb = severityRank(b.severity);
      if (sb !== sa) return sb - sa;
      return Number(b.ts_ms) - Number(a.ts_ms);
    })
    .slice(0, 18)
    .forEach(r => {
      const ageMin =
        Math.max(0, Math.floor((Date.now() - r.ts_ms) / 60000));

      const c = cellColor(r);

      const relevanceBadge = r.relevance_score ? 
        `<span class="relevance-badge" title="Relevance: ${(r.relevance_score * 100).toFixed(1)}%">
          ${(r.relevance_score * 100).toFixed(0)}%
        </span>` : "";

      const item = document.createElement("div");
      item.className = "incidentItem";
      item.innerHTML = `
        <div class="incidentTop">
          <span class="pill ${c.cls}">
            ${r.resolved ? "RESOLVED" : r.severity}
          </span>
          <div class="incidentTitle">
            ${r.symbol} • ${r.event_title}
          </div>
          ${relevanceBadge}
        </div>
        <div class="incidentSub">
          <span class="pill dim">h=${r.horizon_s}s</span>
          <span class="pill dim">${ageMin}m ago</span>
        </div>
      `;

      // Track click interaction
      item.addEventListener("click", async () => {
        await trackAlertInteraction(r.id, 'click', {
          severity: r.severity,
          symbol: r.symbol,
          age_minutes: ageMin
        });
        
        if (opts?.onOpen) opts.onOpen(r);
      });

      host.appendChild(item);
    });
}

// -----------------------------
// Alert interaction controls
// -----------------------------
export function createAlertInteractionControls(alertItem, alertData) {
  const controls = document.createElement("div");
  controls.className = "alert-interaction-controls";

  // Acknowledge button
  const ackBtn = document.createElement("button");
  ackBtn.className = "btn btn-small btn-secondary";
  ackBtn.textContent = "Acknowledge";
  ackBtn.addEventListener("click", async (e) => {
    e.stopPropagation();
    await trackAlertInteraction(alertData.id, 'acknowledge', {
      severity: alertData.severity,
      rule_id: alertData.rule_id
    });
    ackBtn.textContent = "Acknowledged";
    ackBtn.disabled = true;
  });

  // Ignore button
  const ignoreBtn = document.createElement("button");
  ignoreBtn.className = "btn btn-small btn-outline";
  ignoreBtn.textContent = "Ignore";
  ignoreBtn.addEventListener("click", async (e) => {
    e.stopPropagation();
    await trackAlertInteraction(alertData.id, 'ignore', {
      severity: alertData.severity,
      reason: 'manual_ignore'
    });
    ignoreBtn.textContent = "Ignored";
    ignoreBtn.disabled = true;
  });

  // False positive button
  const fpBtn = document.createElement("button");
  fpBtn.className = "btn btn-small btn-outline";
  fpBtn.textContent = "False Positive";
  fpBtn.addEventListener("click", async (e) => {
    e.stopPropagation();
    await trackAlertInteraction(alertData.id, 'false_positive', {
      severity: alertData.severity,
      rule_id: alertData.rule_id
    });
    fpBtn.textContent = "Marked FP";
    fpBtn.disabled = true;
  });

  controls.appendChild(ackBtn);
  controls.appendChild(ignoreBtn);
  controls.appendChild(fpBtn);

  return controls;
}

// -----------------------------
// AI transparency panel
// -----------------------------
export function createTransparencyPanel(alertData) {
  const panel = document.createElement("div");
  panel.className = "transparency-panel";
  panel.innerHTML = `
    <div class="transparency-header">
      <h4>AI Decision Transparency</h4>
      <button class="btn btn-small" onclick="this.parentElement.parentElement.classList.toggle('expanded')">
        Details
      </button>
    </div>
    <div class="transparency-content">
      <div class="decision-log">
        <h5>Recent AI Decisions</h5>
        <div id="decision-log-${alertData.id}">Loading...</div>
      </div>
      <div class="relevance-info">
        <h5>Relevance Score</h5>
        <div class="relevance-score">
          <span class="score-value">${(alertData.relevance_score * 100).toFixed(1)}%</span>
          <div class="score-bar">
            <div class="score-fill" style="width: ${alertData.relevance_score * 100}%"></div>
          </div>
        </div>
      </div>
    </div>
  `;

  // Load decision log
  loadDecisionLog(alertData.id);

  return panel;
}

async function loadDecisionLog(alertId) {
  try {
    const response = await fetch(`/api/alerts/${alertId}/transparency`);
    const decisions = await response.json();
    
    const logContainer = document.getElementById(`decision-log-${alertId}`);
    if (logContainer) {
      logContainer.innerHTML = decisions.map(decision => `
        <div class="decision-entry">
          <span class="decision-type">${decision.decision_type}</span>
          <span class="decision-result">${decision.decision}</span>
          <span class="decision-confidence">${(decision.confidence * 100).toFixed(1)}%</span>
          <div class="decision-time">${new Date(decision.timestamp_ms).toLocaleTimeString()}</div>
        </div>
      `).join('');
    }
  } catch (error) {
    console.error('Failed to load decision log:', error);
  }
}

// -----------------------------
// Override controls
// -----------------------------
export function createOverrideControls() {
  const controls = document.createElement("div");
  controls.className = "override-controls";
  controls.innerHTML = `
    <h4>Alert Override Controls</h4>
    <div class="override-buttons">
      <button class="btn btn-warning" onclick="createEmergencyOverride()">
        Emergency Bypass
      </button>
      <button class="btn btn-secondary" onclick="showActiveOverrides()">
        Active Overrides
      </button>
      <button class="btn btn-outline" onclick="showTransparencyReport()">
        Transparency Report
      </button>
    </div>
  `;

  return controls;
}

window.createEmergencyOverride = async function() {
  const reason = prompt("Reason for emergency bypass:");
  if (!reason) return;

  try {
    const response = await fetch('/api/overrides/emergency', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        operator_id: currentOperatorId,
        reason: reason,
        duration_hours: 2
      })
    });

    if (response.ok) {
      alert('Emergency override created successfully');
    } else {
      alert('Failed to create emergency override');
    }
  } catch (error) {
    console.error('Failed to create emergency override:', error);
    alert('Error creating emergency override');
  }
};

window.showActiveOverrides = async function() {
  try {
    const response = await fetch('/api/overrides/active');
    const overrides = await response.json();
    
    console.log('Active overrides:', overrides);
    // In a real implementation, this would show a modal or panel
    alert(`Active overrides: ${overrides.length}`);
  } catch (error) {
    console.error('Failed to load active overrides:', error);
  }
};

window.showTransparencyReport = async function() {
  try {
    const response = await fetch('/api/transparency/report');
    const report = await response.json();
    
    console.log('Transparency report:', report);
    // In a real implementation, this would show a detailed report
    alert(`Transparency log entries: ${report.length}`);
  } catch (error) {
    console.error('Failed to load transparency report:', error);
  }
};

// Export initialization function
export { initializeInteractionTracking };
