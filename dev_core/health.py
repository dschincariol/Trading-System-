# dev_core/health.py
import os
import time
from typing import Any, Dict

from dev_core.storage import connect, init_db


HEALTH_PRICES_MAX_AGE_S = float(os.environ.get("HEALTH_PRICES_MAX_AGE_S", "120"))
HEALTH_ALLOW_STALE_PRICES = os.environ.get("HEALTH_ALLOW_STALE_PRICES", "1") == "1"
HEALTH_EVENTS_MAX_AGE_S = float(os.environ.get("HEALTH_EVENTS_MAX_AGE_S", "600"))
HEALTH_PREDICTIONS_MAX_AGE_S = float(os.environ.get("HEALTH_PREDICTIONS_MAX_AGE_S", "600"))
HEALTH_JOBS_MAX_STALE_S = float(os.environ.get("HEALTH_JOBS_MAX_STALE_S", "180"))

HEALTH_MIN_LABELS = int(os.environ.get("HEALTH_MIN_LABELS", "10"))
HEALTH_MIN_MODEL_SUPPORT = int(os.environ.get("HEALTH_MIN_MODEL_SUPPORT", "10"))


def get_health_snapshot() -> Dict[str, Any]:
    init_db()
    con = connect()
    try:
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

        now_ms = int(time.time() * 1000)

        # prices freshness
        try:
            row = con.execute("SELECT MAX(ts_ms) FROM prices").fetchone()
            last_ms = int(row[0]) if row and row[0] else 0
        except Exception as e:
            last_ms = 0
            details["notes"]["prices"] = f"prices query failed: {e}"

        if last_ms > 0:
            age_s = (now_ms - last_ms) / 1000.0
            if age_s < HEALTH_PRICES_MAX_AGE_S:
                ok = True
            else:
                ok = HEALTH_ALLOW_STALE_PRICES
                if HEALTH_ALLOW_STALE_PRICES:
                    details["notes"]["prices"] = "stale prices allowed for bootstrap"
            out["prices"] = {"ok": ok, "age_s": round(age_s, 1)}

        else:
            out["prices"] = {"ok": False, "age_s": None}

        # events freshness
        try:
            row = con.execute("SELECT MAX(ts_ms) FROM events").fetchone()
            last_ms = int(row[0]) if row and row[0] else 0
        except Exception as e:
            last_ms = 0
            details["notes"]["events"] = f"events query failed: {e}"

        if last_ms > 0:
            age_s = (now_ms - last_ms) / 1000.0
            ok = age_s < HEALTH_EVENTS_MAX_AGE_S
            out["events"] = {"ok": ok, "age_s": round(age_s, 1)}
        else:
            out["events"] = {"ok": False, "age_s": None}

        # predictions freshness
        try:
            row = con.execute("SELECT MAX(ts_ms) FROM predictions").fetchone()
            last_ms = int(row[0]) if row and row[0] else 0
        except Exception as e:
            last_ms = 0
            details["notes"]["predictions"] = f"predictions query failed: {e}"

        if last_ms > 0:
            age_s = (now_ms - last_ms) / 1000.0
            ok = age_s < HEALTH_PREDICTIONS_MAX_AGE_S
            out["predictions"] = {"ok": ok, "age_s": round(age_s, 1)}
        else:
            out["predictions"] = {"ok": False, "age_s": None}

        # labels count
        try:
            row = con.execute("SELECT COUNT(*) FROM labels").fetchone()
            label_n = int(row[0] or 0)
            ok = label_n >= HEALTH_MIN_LABELS
            out["labels"] = {"ok": ok, "count": label_n}
        except Exception as e:
            out["labels"] = {"ok": False, "count": 0}
            details["notes"]["labels"] = f"labels query failed: {e}"

        # model support
        try:
            row = con.execute("SELECT SUM(n) FROM model_stats_regime").fetchone()
            model_n = int(row[0] or 0)
            ok = model_n >= HEALTH_MIN_MODEL_SUPPORT
            out["model"] = {"ok": ok, "support_n": model_n}
        except Exception as e:
            out["model"] = {"ok": False, "support_n": 0}
            details["notes"]["model"] = f"model query failed: {e}"

        ok_all = True
        for k in ("prices", "events", "predictions", "labels", "model"):
            if not bool(out.get(k, {}).get("ok", False)):
                ok_all = False
                break

        out["ok"] = bool(ok_all)
        out["_details"] = details
        out["ts_ms"] = int(now_ms)
        return out
    finally:
        con.close()
