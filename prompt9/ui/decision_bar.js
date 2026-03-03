"use strict";

/*
  ui/decision_bar.js
  Decision bar rendering + derivation (Phase 7)
  Extracted from ui/dashboard.js
*/

let _getLastAlerts = () => [];
let _getLastHealth = () => null;
let _isExecutionDegraded = () => false;

let _wired = false;

export function initDecisionBarEngine(deps) {
  _getLastAlerts = (deps && deps.getLastAlerts) ? deps.getLastAlerts : (() => []);
  _getLastHealth = (deps && deps.getLastHealth) ? deps.getLastHealth : (() => null);
  _isExecutionDegraded = (deps && deps.isExecutionDegraded) ? deps.isExecutionDegraded : (() => false);

  // wire once (prevents duplicate listeners)
  wireDecisionBarClicks();
}

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

export function wireDecisionBarClicks() {
  if (_wired) return;
  _wired = true;

  document.getElementById("pillSystem")?.addEventListener("click", ()=>_jumpToCard("System Health"));
  document.getElementById("pillCrit")?.addEventListener("click", ()=>_jumpToCard("Alerts"));
  document.getElementById("pillWarn")?.addEventListener("click", ()=>_jumpToCard("Alerts"));
  document.getElementById("pillData")?.addEventListener("click", ()=>_jumpToCard("System Health"));
  document.getElementById("pillModel")?.addEventListener("click", ()=>_jumpToCard("Promotions"));
  document.getElementById("pillExec")?.addEventListener("click", ()=>_jumpToCard("Execution"));
}

export function updateDecisionBarFromState(state) {
  // state: { system, crit, warn, data, model, exec, updated }
  _setPill("pillSystem", `SYSTEM: ${state.system}`, state.system === "CRIT" ? "crit" : state.system === "WARN" ? "warn" : "ok");
  _setPill("pillCrit", `CRIT: ${state.crit}`, state.crit > 0 ? "crit" : "dim");
  _setPill("pillWarn", `WARN: ${state.warn}`, state.warn > 0 ? "warn" : "dim");
  _setPill("pillData", `Data: ${state.data}`, state.data === "BAD" ? "crit" : state.data === "WARN" ? "warn" : "ok");
  _setPill("pillModel", `Model: ${state.model}`, state.model === "BLOCKED" ? "warn" : "ok");
  _setPill("pillExec", `Exec: ${state.exec}`, state.exec === "DEGRADED" ? "warn" : "ok");

  const up = document.getElementById("pillUpdated");
  if (up) up.textContent = `Updated: ${state.updated}`;
}

export function updateDecisionHeader(updatedLabel) {
  try {
    const _lastAlerts = _getLastAlerts() || [];
    const _lastHealth = _getLastHealth() || null;

    const critN = (_lastAlerts || []).filter((a)=>!a.resolved && a.severity === "CRIT").length;
    const warnN = (_lastAlerts || []).filter((a)=>!a.resolved && a.severity === "WARN").length;

    const hp = (_lastHealth && _lastHealth.prices)
      ? (_lastHealth.prices.ok ? `prices ok (${_lastHealth.prices.age_s}s)` : "prices stale")
      : (document.getElementById("healthPrices")?.textContent || "");

    const hl = (_lastHealth && _lastHealth.labels)
      ? (`labels ${_lastHealth.labels.ok ? "ok" : "bad"} (${_lastHealth.labels.count ?? "?"})`)
      : (document.getElementById("healthLabels")?.textContent || "");

    const dataBad = /stale|missing|error|bad/i.test(String(hp) + " " + String(hl));
    const dataWarn = /warn/i.test(String(hp) + " " + String(hl));

    const promo = document.getElementById("promotionPill")?.textContent || "";
    const modelBlocked = /blocked|off|0|false|disable/i.test(promo);

    const execDegraded = !!_isExecutionDegraded();
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
      updated: updatedLabel || "just now",
    });
  } catch {}
}
