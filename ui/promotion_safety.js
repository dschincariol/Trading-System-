"use strict";

/*
  ui/promotion_safety.js
  Promotion safety + execution-recovery auto-resume engine (Phase 8)
  Extracted verbatim from dashboard.js
*/

const _PROMO_PAUSED_KEY = "promo_paused_due_to_exec_v1";

let _isExecutionDegraded = () => false;
let _hardBlockActionIfManipulated = () => false;
let _toast = null;
let _fetchJSON = null;
let _loadPromotionStatus = null;
let _loadSizePolicy = null;
let _refresh = null;
let _getManipBlockedSyms = () => new Set();

export function initPromotionSafetyEngine(deps) {
  _isExecutionDegraded = deps.isExecutionDegraded;
  _hardBlockActionIfManipulated = deps.hardBlockActionIfManipulated;
  _toast = deps.toast;
  _fetchJSON = deps.fetchJSON;
  _loadPromotionStatus = deps.loadPromotionStatus;
  _loadSizePolicy = deps.loadSizePolicy;
  _refresh = deps.refresh;
  _getManipBlockedSyms = deps.getManipBlockedSyms || (() => new Set());
}

// -----------------------------
// Auto-resume after execution recovery
// -----------------------------
export async function maybeAutoResumePromotionsAfterRecovery({
  operatorMode
}) {
  // Never auto-resume during manipulation risk
  if (_getManipBlockedSyms().size > 0) return;

  if (localStorage.getItem(_PROMO_PAUSED_KEY) !== "1") return;
  if (_isExecutionDegraded()) return;

  try {
    const st = await _fetchJSON("/api/promotion/status");
    if (!st || !st.ok) return;

    const enabledDb =
      (st && st.promotion_enabled_db)
        ? String(st.promotion_enabled_db)
        : "1";

    if (enabledDb === "1") {
      localStorage.removeItem(_PROMO_PAUSED_KEY);
      return;
    }

    if (!operatorMode) {
      const ok = confirm(
        "Execution has recovered.\n\nResume promotions automatically now?"
      );
      if (!ok) return;
    }

    const res = await _fetchJSON("/api/promotion/enable?on=1");
    if (res && res.ok) {
      _toast("Promotions resumed after execution recovery", "ok", 3500);
      localStorage.removeItem(_PROMO_PAUSED_KEY);
      await _loadPromotionStatus();
    }
  } catch {
    // ignore
  }
}

// -----------------------------
// Promotion toggle guard
// -----------------------------
export async function handlePromotionToggle({
  operatorMode,
  expertUnlocked
}) {
  if (_hardBlockActionIfManipulated({
    actionName: "toggle promotions",
    symbol: "GLOBAL",
    expertUnlocked,
    toastFn: _toast
  })) return;

  if (_isExecutionDegraded()) {
    localStorage.setItem(_PROMO_PAUSED_KEY, "1");
    _toast("Promotions paused due to execution degradation", "warn", 4000);
    return;
  }

  if (!operatorMode) {
    if (!confirm("Toggle promotions? This affects model promotion safety.")) {
      return;
    }
  }

  const st = await _fetchJSON("/api/promotion/status");
  const enabledDb =
    (st && st.promotion_enabled_db) ? st.promotion_enabled_db : "1";
  const next = (enabledDb === "1") ? "0" : "1";

  const res = await _fetchJSON(`/api/promotion/enable?on=${next}`);
  if (!res || !res.ok) {
    throw new Error((res && res.error) || "toggle failed");
  }

  await _loadPromotionStatus();
}

// -----------------------------
// Automatic fix button logic
// -----------------------------
export async function handleAutoFix({
  operatorMode
}) {
  if (!operatorMode) {
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

  const res = await _fetchJSON("/api/system/fix");
  if (!res || !res.ok) {
    throw new Error(res?.error || "fix failed");
  }

  if (el) {
    el.textContent += "[ui] automatic fix complete\n";
    if (res.actions) {
      el.textContent += JSON.stringify(res.actions, null, 2) + "\n";
    }
  }

  _toast("Automatic fixes applied", "ok", 3500);

  await _refresh();
  await _loadPromotionStatus();
  await _loadSizePolicy();
}
