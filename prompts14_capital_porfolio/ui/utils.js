"use strict";

/* ui/utils.js — pure UI helpers (no DOM, no side effects) */

export function esc(x) {
  return String(x ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

export function escapeHTML(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function fmtTime(ms) {
  try { return new Date(Number(ms)).toLocaleTimeString(); }
  catch { return ""; }
}

export function fmtNum(x) {
  if (x === null || x === undefined) return "";
  const v = Number(x);
  if (!isFinite(v)) return "";
  return v.toFixed(4);
}

export function _fmtPct(x) {
  if (!Number.isFinite(x)) return "?";
  return `${(x * 100).toFixed(2)}%`;
}

export function _clamp(v, lo, hi) {
  return Math.max(lo, Math.min(hi, v));
}

export function barWidth(pct) {
  const v = Math.max(0, Math.min(100, pct));
  return `${v.toFixed(1)}%`;
}

export function _debounce(fn, ms = 120) {
  let t;
  return (...a) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...a), ms);
  };
}
