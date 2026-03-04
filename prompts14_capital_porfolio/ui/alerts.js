"use strict";

/*
  ui/alerts.js — Alert rendering + filtering engine
  Extracted from ui/dashboard.js (Phase 4)
*/

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
// Filtering
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
// Heatmap
// -----------------------------
function scoreCell(rows) {
  let best = null;
  for (const r of rows) {
    const sev = severityRank(r.severity);
    const conf = Number(r.confidence);
    const z = Math.abs(Number(r.expected_z));
    if (!Number.isFinite(conf) || !Number.isFinite(z)) continue;

    const score = sev * conf * (0.6 + Math.min(3.0, z));
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

      const cell = mk(
        "",
        `<span class="hmSwatch" style="background:${c.sw};"></span>
         <span class="mono">${sym}</span>
         <span class="hmMeta">${best ? best.severity : "—"}</span>`
      );

      cell.addEventListener("click", () => {
        if (onSelectSymbol) onSelectSymbol(sym);
      });

      host.appendChild(cell);
    }
  }
}

// -----------------------------
// Incident queue
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
        </div>
        <div class="incidentSub">
          <span class="pill dim">h=${r.horizon_s}s</span>
          <span class="pill dim">${ageMin}m ago</span>
        </div>
      `;

      item.addEventListener("click", () => {
        if (opts?.onOpen) opts.onOpen(r);
      });

      host.appendChild(item);
    });
}
