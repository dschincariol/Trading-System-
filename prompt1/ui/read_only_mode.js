"use strict";

/*
  ui/read_only_mode.js
  Read-only / Demo / Investor mode enforcement (Phase 10)
*/

const READ_ONLY_KEY = "ui_read_only_mode_v1";

export function isReadOnlyMode() {
  return localStorage.getItem(READ_ONLY_KEY) === "1";
}

export function setReadOnlyMode(on) {
  localStorage.setItem(READ_ONLY_KEY, on ? "1" : "0");
}

export function applyReadOnlyBanner() {
  const banner = document.getElementById("readOnlyBanner");
  if (!banner) return;

  if (isReadOnlyMode()) {
    banner.style.display = "block";
    banner.textContent =
      "🔒 Demo / Investor Mode — System is read-only. Actions are disabled.";
  } else {
    banner.style.display = "none";
  }
}

export function hardBlockIfReadOnly({
  actionName,
  toastFn
}) {
  if (!isReadOnlyMode()) return false;

  if (typeof toastFn === "function") {
    toastFn(
      `Blocked "${actionName}" — Demo / Read-only mode`,
      "warn",
      3500
    );
  }

  return true;
}
