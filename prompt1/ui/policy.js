"use strict";

/*
  ui/policy.js — Operator vs Expert policy engine
  Extracted from ui/dashboard.js (Phase 5)
*/

// -----------------------------
// Storage keys
// -----------------------------
export const OPERATOR_MODE_KEY = "operator_mode";
export const EXPERT_UNLOCK_KEY = "expert_unlock";

// -----------------------------
// State
// -----------------------------
export function loadPolicyState() {
  // Operator mode defaults ON once
  if (localStorage.getItem(OPERATOR_MODE_KEY) == null) {
    localStorage.setItem(OPERATOR_MODE_KEY, "1");
  }

  return {
    operatorMode: localStorage.getItem(OPERATOR_MODE_KEY) === "1",
    expertUnlocked: localStorage.getItem(EXPERT_UNLOCK_KEY) === "1",
  };
}

export function saveOperatorMode(on) {
  localStorage.setItem(OPERATOR_MODE_KEY, on ? "1" : "0");
}

export function saveExpertUnlock(on) {
  localStorage.setItem(EXPERT_UNLOCK_KEY, on ? "1" : "0");
}

// -----------------------------
// DOM policy application
// -----------------------------
export function applyPolicyToDOM({ operatorMode, expertUnlocked }) {
  document.body.classList.toggle("mode-operator", !!operatorMode);
  document.body.classList.toggle("mode-expert", !operatorMode);
  document.body.classList.toggle("expert-unlocked", !!expertUnlocked);

  // legacy alerts table hidden for operators
  const legacy = document.getElementById("alerts");
  if (legacy) legacy.style.display = operatorMode ? "none" : "";

  const btnOp = document.getElementById("btnOperatorMode");
  if (btnOp) {
    btnOp.textContent =
      `👷 Operator Mode: ${operatorMode ? "ON" : "OFF"}`;
  }

  const btnEx = document.getElementById("btnExpertUnlock");
  if (btnEx) {
    btnEx.textContent =
      `🛡 Unlock Advanced: ${expertUnlocked ? "ON" : "OFF"}`;
  }
}

// -----------------------------
// Guards
// -----------------------------
export function requireExpertUnlock(expertUnlocked, message) {
  if (expertUnlocked) return true;
  if (!message) return false;
  return confirm(message);
}

export function requireConfirmIfDegraded({
  executionDegraded,
  operatorMode,
  message
}) {
  if (!executionDegraded) return true;
  if (operatorMode) return true;
  return confirm(message);
}
