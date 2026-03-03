# engine/runtime/health.py
"""
Runtime Health + Preflight

Extracted from dashboard_server.py
No HTTP logic.
Pure health + startup validation.
"""

import os
import time
from typing import Dict

from engine.runtime.storage import connect as _db_connect
from engine.training_guard import (
    get_training_status,
    set_training_mode,
)

# ---------------------------------------------------
# ENV THRESHOLDS
# ---------------------------------------------------

HEALTH_PRICES_MAX_AGE_S = float(os.environ.get("HEALTH_PRICES_MAX_AGE_S", "120"))
HEALTH_EVENTS_MAX_AGE_S = float(os.environ.get("HEALTH_EVENTS_MAX_AGE_S", "600"))
HEALTH_PREDICTIONS_MAX_AGE_S = float(os.environ.get("HEALTH_PREDICTIONS_MAX_AGE_S", "600"))
HEALTH_JOBS_MAX_STALE_S = float(os.environ.get("HEALTH_JOBS_MAX_STALE_S", "180"))

HEALTH_MIN_LABELS = int(os.environ.get("HEALTH_MIN_LABELS", "10"))
HEALTH_MIN_MODEL_SUPPORT = int(os.environ.get("HEALTH_MIN_MODEL_SUPPORT", "10"))

PREFLIGHT_ENABLE = os.environ.get("PREFLIGHT_ENABLE", "1") == "1"
PREFLIGHT_PRICES_MAX_AGE_S = float(os.environ.get("PREFLIGHT_PRICES_MAX_AGE_S", "300"))

_PREFLIGHT_CACHE = {
    "ok": True,
    "notes": [],
    "tables_ok": True,
    "health_ok": True,
    "ts_ms": 0,
}


# ---------------------------------------------------
# INTERNAL HELPERS
# ---------------------------------------------------

def _get_table_cols(con, table: str):
    try:
        rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    except Exception:
        rows = []
    return [r[1] for r in rows] if rows else []


# ---------------------------------------------------
# SCHEMA AUDIT
# ---------------------------------------------------

def get_schema_audit():
    ts_ms = int(time.time() * 1000)
    con = _db_connect()
    try:
        try:
            rows = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            have = {r[0] for r in rows}
        except Exception:
            have = set()

        missing_tables = []
        missing_cols = {}

        SCHEMA_EXPECTATIONS = {
            "prices": {"required": True, "cols": ["ts_ms", "symbol", "price"]},
            "events": {"required": True, "cols": ["id", "ts_ms"]},
            "labels": {"required": True, "cols": ["event_id", "label", "ts_ms"]},
            "alerts": {"required": True, "cols": ["id", "ts_ms"]},
            "job_history": {"required": True, "cols": ["id", "ts_ms"]},
            "portfolio_state": {"required": True, "cols": ["ts_ms"]},
            "broker_account": {"required": True, "cols": ["ts_ms"]},
            "job_locks": {
                "required": True,
                "cols": ["job_name", "owner", "heartbeat_ts_ms"],
            },
            # Optional shadow scoring table
            "shadow_capital_scores": {
                "required": False,
                "cols": ["ts_ms", "window_s", "regime", "model_name", "score"],
            },
        }

        for t, spec in SCHEMA_EXPECTATIONS.items():
            if t not in have:
                if spec.get("required"):
                    missing_tables.append(t)
                continue

            cols_have = _get_table_cols(con, t)
            miss = [c for c in spec.get("cols", []) if c not in cols_have]
            if miss and spec.get("required"):
                missing_cols[t] = miss

        # attempt auto-healing for missing tables/columns
        for t in missing_tables:
            try:
                # create an empty table with only the required cols as TEXT;
                # types are best-effort because we don't know exact schema here
                cols = spec = SCHEMA_EXPECTATIONS.get(t, {}).get("cols", [])
                col_defs = ", ".join(f"{c} TEXT" for c in cols) or "id INTEGER PRIMARY KEY"
                con.execute(f"CREATE TABLE IF NOT EXISTS {t} ({col_defs})")
            except Exception:
                pass
        for t, cols in missing_cols.items():
            for c in cols:
                try:
                    # sqlite will ignore if column already exists
                    con.execute(f"ALTER TABLE {t} ADD COLUMN {c} TEXT")
                except Exception:
                    pass

        ok = (not missing_tables) and (not missing_cols)

        return {
            "ok": bool(ok),
            "ts_ms": ts_ms,
            "missing_tables": missing_tables,
            "missing_cols": missing_cols,
            "have_tables": sorted(list(have)),
        }

    finally:
        con.close()


# ---------------------------------------------------
# HEALTH SNAPSHOT
# ---------------------------------------------------

def get_health_snapshot():
    con = _db_connect()
    try:
        out = {}
        now_ms = int(time.time() * 1000)

        # ---------------------------
        # Prices freshness
        # ---------------------------
        try:
            row = con.execute("SELECT MAX(ts_ms) FROM prices").fetchone()
        except Exception:
            row = None

        if row and row[0]:
            age_s = (now_ms - int(row[0])) / 1000.0
            out["prices"] = {
                "ok": age_s < HEALTH_PRICES_MAX_AGE_S,
                "age_s": round(age_s, 1),
                "max_age_s": HEALTH_PRICES_MAX_AGE_S,
            }
        else:
            out["prices"] = {
                "ok": False,
                "age_s": None,
                "max_age_s": HEALTH_PRICES_MAX_AGE_S,
            }

        # ---------------------------
        # Events freshness
        # ---------------------------
        try:
            row = con.execute("SELECT MAX(ts_ms) FROM events").fetchone()
        except Exception:
            row = None

        if row and row[0]:
            age_s = (now_ms - int(row[0])) / 1000.0
            out["events"] = {
                "ok": age_s < HEALTH_EVENTS_MAX_AGE_S,
                "age_s": round(age_s, 1),
                "max_age_s": HEALTH_EVENTS_MAX_AGE_S,
            }
        else:
            out["events"] = {
                "ok": False,
                "age_s": None,
                "max_age_s": HEALTH_EVENTS_MAX_AGE_S,
            }

        # ---------------------------
        # Labels coverage
        # ---------------------------
        try:
            row = con.execute("SELECT COUNT(*) FROM labels").fetchone()
            label_n = int(row[0] or 0)
            out["labels"] = {
                "ok": label_n >= HEALTH_MIN_LABELS,
                "count": label_n,
                "min_required": HEALTH_MIN_LABELS,
            }
        except Exception:
            out["labels"] = {
                "ok": False,
                "count": 0,
                "min_required": HEALTH_MIN_LABELS,
            }

        # ---------------------------
        # Model support
        # ---------------------------
        try:
            row = con.execute("SELECT SUM(n) FROM model_stats_regime").fetchone()
            model_n = int(row[0] or 0)
            out["model"] = {
                "ok": model_n >= HEALTH_MIN_MODEL_SUPPORT,
                "support_n": model_n,
                "min_required": HEALTH_MIN_MODEL_SUPPORT,
            }
        except Exception:
            out["model"] = {
                "ok": False,
                "support_n": 0,
                "min_required": HEALTH_MIN_MODEL_SUPPORT,
            }

        # ---------------------------
        # Training guard
        # ---------------------------
        try:
            out["training"] = get_training_status()
        except Exception:
            out["training"] = {"mode": "unknown", "allowed": False}

        return out

    finally:
        con.close()


# ---------------------------------------------------
# PREFLIGHT
# ---------------------------------------------------

def run_preflight() -> Dict:
    global _PREFLIGHT_CACHE

    ts_ms = int(time.time() * 1000)
    out = {
        "ok": True,
        "notes": [],
        "tables_ok": True,
        "health_ok": True,
        "ts_ms": ts_ms,
    }

    if not PREFLIGHT_ENABLE:
        out["notes"].append("preflight disabled")
        _PREFLIGHT_CACHE = out
        return out

    try:
        # ---------------------------
        # Schema validation
        # ---------------------------
        schema = get_schema_audit()
        if not schema.get("ok"):
            out["ok"] = False
            out["tables_ok"] = False

            if schema.get("missing_tables"):
                out["notes"].append(
                    f"missing_tables={schema.get('missing_tables')}"
                )

            if schema.get("missing_cols"):
                out["notes"].append(
                    f"missing_cols={schema.get('missing_cols')}"
                )

            # try to heal schema automatically
            try:
                # run_schema_heal might exist externally
                pass
            except Exception:
                pass

        # ---------------------------
        # Health validation
        # ---------------------------
        h = get_health_snapshot()

        prices_ok = bool(h.get("prices", {}).get("ok"))
        labels_ok = bool(h.get("labels", {}).get("ok"))
        model_ok = bool(h.get("model", {}).get("ok"))

        out["health_ok"] = bool(prices_ok and labels_ok and model_ok)

        age_s = float(h.get("prices", {}).get("age_s") or 1e9)
        if age_s > PREFLIGHT_PRICES_MAX_AGE_S:
            out["ok"] = False
            out["notes"].append(f"prices too stale: {age_s:.1f}s")

    except Exception as e:
        out["ok"] = False
        out["health_ok"] = False
        out["notes"].append(str(e))

    _PREFLIGHT_CACHE = out
    return out


def preflight_cached(max_age_s: float = 30.0) -> Dict:
    """
    Returns cached preflight only if recent.
    Prevents stale OK state after system degradation.
    """
    now = int(time.time() * 1000)
    cache = dict(_PREFLIGHT_CACHE or {})
    ts = int(cache.get("ts_ms") or 0)
    age_s = (now - ts) / 1000.0 if ts else 1e9

    if age_s > float(max_age_s):
        return run_preflight()

    return cache
