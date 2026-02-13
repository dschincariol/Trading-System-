import os
import time
from typing import Any, Dict

from dev_core.storage import connect, init_db
from dev_core.risk_state import get_state

HEALTH_PRICES_MAX_AGE_S = float(os.environ.get("HEALTH_PRICES_MAX_AGE_S", "120"))
HEALTH_ALLOW_STALE_PRICES = os.environ.get("HEALTH_ALLOW_STALE_PRICES", "1") == "1"
HEALTH_EVENTS_MAX_AGE_S = float(os.environ.get("HEALTH_EVENTS_MAX_AGE_S", "600"))
HEALTH_PREDICTIONS_MAX_AGE_S = float(os.environ.get("HEALTH_PREDICTIONS_MAX_AGE_S", "600"))
HEALTH_JOBS_MAX_STALE_S = float(os.environ.get("HEALTH_JOBS_MAX_STALE_S", "180"))

HEALTH_MIN_LABELS = int(os.environ.get("HEALTH_MIN_LABELS", "10"))
HEALTH_MIN_MODEL_SUPPORT = int(os.environ.get("HEALTH_MIN_MODEL_SUPPORT", "10"))


def _age_s(now_ms: int, last_ms: int) -> float:
    return max(0.0, (int(now_ms) - int(last_ms)) / 1000.0)


def get_health_snapshot() -> Dict[str, Any]:
    init_db()
    con = connect()

    try:
        now_ms = int(time.time() * 1000)

        out: Dict[str, Any] = {}
        details: Dict[str, Any] = {
            "thresholds": {
                "prices_max_age_s": HEALTH_PRICES_MAX_AGE_S,
                "events_max_age_s": HEALTH_EVENTS_MAX_AGE_S,
                "predictions_max_age_s": HEALTH_PREDICTIONS_MAX_AGE_S,
                "jobs_max_stale_s": HEALTH_JOBS_MAX_STALE_S,
                "min_labels": HEALTH_MIN_LABELS,
                "min_model_support": HEALTH_MIN_MODEL_SUPPORT,
            },
            "notes": {},
        }

        # ============================================================
        # CAPITAL / TRADING STATE
        # ============================================================

        try:
            cap_ts = int(get_state("capital_mode_ts_ms", "0") or "0")
            out["capital_mode"] = {
                "mode": str(get_state("capital_mode", "normal") or "normal"),
                "reason": str(get_state("capital_mode_reason", "") or ""),
                "ts_ms": cap_ts,
                "age_s": _age_s(now_ms, cap_ts) if cap_ts > 0 else None,
                "exit_streak": int(get_state("capital_mode_exit_streak", "0") or "0"),
            }
        except Exception:
            out["capital_mode"] = {"mode": "normal"}

        try:
            stop_ts = int(get_state("stop_ts_ms", "0") or "0")
            out["trading_state"] = {
                "state": str(get_state("trading_state", "enabled") or "enabled"),
                "stop_reason": str(get_state("stop_reason", "") or ""),
                "stop_ts_ms": stop_ts,
                "stop_age_s": _age_s(now_ms, stop_ts) if stop_ts > 0 else None,
            }
        except Exception:
            out["trading_state"] = {"state": "enabled"}

        # ============================================================
        # PRICES
        # ============================================================

        try:
            row = con.execute("SELECT MAX(ts_ms) FROM prices").fetchone()
            last_ms = int(row[0]) if row and row[0] else 0
        except Exception as e:
            last_ms = 0
            details["notes"]["prices"] = str(e)

        if last_ms > 0:
            age_s = _age_s(now_ms, last_ms)
            ok = age_s < HEALTH_PRICES_MAX_AGE_S
            if not ok and HEALTH_ALLOW_STALE_PRICES:
                ok = True
                details["notes"]["prices"] = "stale allowed"
            out["prices"] = {"ok": bool(ok), "age_s": round(age_s, 1)}
        else:
            out["prices"] = {"ok": False, "age_s": None}

        # ============================================================
        # EVENTS
        # ============================================================

        try:
            row = con.execute("SELECT MAX(ts_ms) FROM events").fetchone()
            last_ms = int(row[0]) if row and row[0] else 0
        except Exception as e:
            last_ms = 0
            details["notes"]["events"] = str(e)

        if last_ms > 0:
            age_s = _age_s(now_ms, last_ms)
            out["events"] = {"ok": age_s < HEALTH_EVENTS_MAX_AGE_S, "age_s": round(age_s, 1)}
        else:
            out["events"] = {"ok": False, "age_s": None}

        # ============================================================
        # PREDICTIONS
        # ============================================================

        try:
            row = con.execute("SELECT MAX(ts_ms) FROM predictions").fetchone()
            last_ms = int(row[0]) if row and row[0] else 0
        except Exception as e:
            last_ms = 0
            details["notes"]["predictions"] = str(e)

        if last_ms > 0:
            age_s = _age_s(now_ms, last_ms)
            out["predictions"] = {"ok": age_s < HEALTH_PREDICTIONS_MAX_AGE_S, "age_s": round(age_s, 1)}
        else:
            out["predictions"] = {"ok": False, "age_s": None}

        # ============================================================
        # LABELS
        # ============================================================

        try:
            row = con.execute("SELECT COUNT(*) FROM labels").fetchone()
            label_n = int(row[0] or 0)
            out["labels"] = {"ok": label_n >= HEALTH_MIN_LABELS, "count": label_n}
        except Exception as e:
            out["labels"] = {"ok": False, "count": 0}
            details["notes"]["labels"] = str(e)

        # ============================================================
        # MODEL SUPPORT
        # ============================================================

        try:
            row = con.execute("SELECT SUM(n) FROM model_stats_regime").fetchone()
            model_n = int(row[0] or 0)
            out["model"] = {"ok": model_n >= HEALTH_MIN_MODEL_SUPPORT, "support_n": model_n}
        except Exception as e:
            out["model"] = {"ok": False, "support_n": 0}
            details["notes"]["model"] = str(e)

        # ============================================================
        # JOB MONITORING
        # ============================================================

        try:
            row = con.execute("SELECT MAX(ts_ms) FROM job_runs").fetchone()
            last_ms = int(row[0]) if row and row[0] else 0
        except Exception:
            last_ms = 0

        if last_ms > 0:
            age_s = _age_s(now_ms, last_ms)
            out["jobs"] = {"ok": age_s < HEALTH_JOBS_MAX_STALE_S, "age_s": round(age_s, 1)}
        else:
            out["jobs"] = {"ok": False, "age_s": None}

        # ============================================================
        # OVERALL STATUS
        # ============================================================

        ok_all = True
        for k in ("prices", "events", "predictions", "labels", "model", "jobs"):
            if not bool(out.get(k, {}).get("ok", False)):
                ok_all = False
                break

        out["ok"] = bool(ok_all)
        out["_details"] = details
        out["ts_ms"] = int(now_ms)

        return out

    finally:
        con.close()
