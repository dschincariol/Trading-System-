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
from engine.training_guard import get_training_status
from engine.runtime.lifecycle_state import get_state as _lc_get_state, WARMING_UP as _WARMING_UP, LIVE as _LIVE, DEGRADED as _DEGRADED

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

_PREFLIGHT_CACHE: Dict = {
    "ok": False,
    "notes": [],
    "tables_ok": False,
    "health_ok": False,
    "ts_ms": 0,
}


# ---------------------------------------------------
# INTERNAL HELPERS
# ---------------------------------------------------

def _get_table_cols(con, table: str):
    try:
        rows = con.execute(f"PRAGMA table_info({table})").fetchall()
        return [r[1] for r in rows] if rows else []
    except Exception:
        return []


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
            "portfolio_state": {
    "required": True,
    "cols": ["symbol", "side", "weight", "opened_ts_ms", "updated_ts_ms"],
},
            "broker_account": {"required": True, "cols": ["ts_ms"]},
            "job_locks": {
                "required": True,
                "cols": ["job_name", "owner", "heartbeat_ts_ms"],
            },
            "shadow_capital_scores": {
                "required": False,
                "cols": ["ts_ms", "window_s", "regime", "model_name", "score"],
            },
        }

        for table, spec in SCHEMA_EXPECTATIONS.items():
            if table not in have:
                if spec.get("required"):
                    missing_tables.append(table)
                continue

            cols_have = _get_table_cols(con, table)
            missing = [c for c in spec.get("cols", []) if c not in cols_have]
            if missing and spec.get("required"):
                missing_cols[table] = missing

        ok = not missing_tables and not missing_cols

        return {
            "ok": ok,
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
    now_ms = int(time.time() * 1000)

    try:
        out = {}

        # ---------------------------
        # Prices freshness
        # ---------------------------
        try:
            row = con.execute("SELECT MAX(ts_ms) FROM prices").fetchone()
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
        except Exception:
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
        except Exception:
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

        # ---------------------------
        # Job heartbeats
        # ---------------------------
        try:
            row = con.execute(
                "SELECT job_name, MAX(heartbeat_ts_ms) FROM job_locks GROUP BY job_name"
            ).fetchall() or []

            jobs = {}
            for job_name, hb_ts in row:
                if not hb_ts:
                    continue
                age_s = (now_ms - int(hb_ts)) / 1000.0
                jobs[job_name] = {
                    "ok": age_s < HEALTH_JOBS_MAX_STALE_S,
                    "age_s": round(age_s, 1),
                    "max_age_s": HEALTH_JOBS_MAX_STALE_S,
                }

            out["jobs"] = jobs

        except Exception:
            out["jobs"] = {}

        # ---------------------------
        # Portfolio presence
        # ---------------------------
        try:
            row = con.execute("SELECT COUNT(*) FROM portfolio_state").fetchone()
            state_n = int(row[0] or 0)

            _mode = os.environ.get("ENGINE_MODE", "").strip().lower() or "safe"

            if _mode == "safe":
                out["portfolio"] = {
                    "ok": True,
                    "positions": state_n,
                }
            else:
                out["portfolio"] = {
                    "ok": state_n > 0,
                    "positions": state_n,
                }
        except Exception:
            out["portfolio"] = {
                "ok": False,
                "positions": 0,
            }

        # ---------------------------
        # Execution barrier
        # ---------------------------
        try:
            from engine.runtime.execution_barrier import execution_gate_snapshot
            snap = execution_gate_snapshot()
            if isinstance(snap, dict):
                out["execution_barrier"] = snap
            else:
                out["execution_barrier"] = {"allowed": True}
        except Exception:
            out["execution_barrier"] = {"allowed": False, "reason": "execution_barrier_error"}

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

        # ---------------------------
        # Health validation
        # ---------------------------
        health = get_health_snapshot()

        prices = health.get("prices", {}) or {}
        prices_ok = bool(prices.get("ok"))

        # Execution barrier alignment
        try:
            from engine.runtime.execution_barrier import execution_gate_snapshot
            barrier = execution_gate_snapshot()
            barrier_ok = bool(barrier.get("allowed")) if isinstance(barrier, dict) else False
        except Exception:
            barrier_ok = False

        out["health_ok"] = prices_ok and barrier_ok

        age_s = float(prices.get("age_s") or 1e9)

        # In SAFE mode, tolerate stale prices at boot so auto-boot daemons can start
        _mode = os.environ.get("ENGINE_MODE", "").strip().lower()
        if not _mode:
            _mode = "safe"

        if age_s > PREFLIGHT_PRICES_MAX_AGE_S:
            if _mode == "safe":
                out["notes"].append(f"prices too stale (SAFE tolerated): {age_s:.1f}s")
            else:
                out["ok"] = False
                out["notes"].append(f"prices too stale: {age_s:.1f}s")

    except Exception as e:
        out["ok"] = False
        out["health_ok"] = False
        out["notes"].append(str(e))

    _PREFLIGHT_CACHE = out
    _mode = os.environ.get("ENGINE_MODE", "").strip().lower() or "safe"

    # Add derived status field
    prices_ok = bool((out.get("prices") or {}).get("ok"))

    if _mode == "safe" and not prices_ok:
        out["status"] = "WARMING_UP"
    else:
        out["status"] = "LIVE" if prices_ok else "DEGRADED"

    try:
        lc = _lc_get_state()
    except Exception:
        lc = {"state": "BOOTING", "detail": "", "first_price_ts_ms": ""}

    out["lifecycle"] = lc

    # Deterministic status: SAFE warms up until first tick is latched
    mode = (os.environ.get("ENGINE_MODE", "") or "safe").strip().lower()
    first_tick = str((lc or {}).get("first_price_ts_ms") or "").strip()
    prices_ok = bool((out.get("prices") or {}).get("ok"))

    if mode == "safe":
        out["status"] = _LIVE if first_tick else _WARMING_UP
        out["ok"] = True
    else:
        # In live/shadow: require prices_ok
        out["status"] = _LIVE if prices_ok else _DEGRADED
        out["ok"] = bool(prices_ok)

    return out


def preflight_cached(max_age_s: float = 30.0) -> Dict:
    now = int(time.time() * 1000)
    cache = dict(_PREFLIGHT_CACHE or {})
    ts = int(cache.get("ts_ms") or 0)
    age_s = (now - ts) / 1000.0 if ts else 1e9

    if age_s > float(max_age_s):
        return run_preflight()

    return cache
