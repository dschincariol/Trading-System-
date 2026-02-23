"""
Promotion / Rollback / Exec Calibration
Moved out of dashboard_server to enforce layer isolation.
"""

import json
import time

from engine.api.internal_access import db_connect


# --------------------------------------------------
# ROLLBACK
# --------------------------------------------------

def api_post_rollback(_parsed=None, _body=None, _ctx=None):
    try:
        from engine.model_registry import rollback_champion as _rb
        from engine.promotion_audit import audit as _audit
        from engine.model_registry import get_stage_latest as _get

        ch_before = None
        try:
            ch_before = _get("embed_regressor", "champion")
        except Exception:
            pass

        ch_after = _rb("embed_regressor")
        if not ch_after:
            return {"ok": False, "error": "no retired model available"}

        _audit(
            actor="manual",
            action="rollback",
            model_name="embed_regressor",
            from_kind=(ch_before.get("model_kind") if ch_before else None),
            from_ts_ms=(ch_before.get("model_ts_ms") if ch_before else None),
            to_kind=ch_after.get("model_kind"),
            to_ts_ms=ch_after.get("model_ts_ms"),
            reason={"note": "dashboard rollback"},
        )

        return {"ok": True, "champion": ch_after}

    except Exception as e:
        return {"ok": False, "error": str(e)}


# --------------------------------------------------
# PROMOTION STATUS
# --------------------------------------------------

def get_promotion_status():
    try:
        from engine.promotion_guard import promotion_allowed
        allowed = bool(promotion_allowed())
    except Exception:
        allowed = False

    try:
        con = db_connect()
        row = con.execute(
            """
            SELECT value, updated_ts_ms
            FROM risk_state
            WHERE key='promotion_enabled'
            """
        ).fetchone()
        con.close()

        enabled = (str(row[0]) == "1") if row else True
        ts_ms = int(row[1]) if row else 0
    except Exception:
        enabled = True
        ts_ms = 0

    return {
        "enabled": bool(enabled),
        "allowed": bool(allowed),
        "updated_ts_ms": int(ts_ms),
    }


def get_promotion_explain():
    out = {
        "ok": True,
        "ts_ms": int(time.time() * 1000),
        "promotion_status": get_promotion_status(),
        "registry": {},
        "audit": [],
    }

    try:
        from engine.model_registry import list_recent
        out["registry"]["embed_regressor"] = list_recent("embed_regressor", limit=50) or []
    except Exception:
        out["registry"]["embed_regressor"] = []

    try:
        con = db_connect()
        rows = con.execute(
            """
            SELECT ts_ms, model_name, key, decision, reason, detail_json
            FROM model_promotion_audit
            ORDER BY ts_ms DESC
            LIMIT 50
            """
        ).fetchall()
        con.close()

        for r in rows or []:
            out["audit"].append({
                "ts_ms": int(r[0] or 0),
                "model_name": str(r[1] or ""),
                "key": str(r[2] or ""),
                "decision": str(r[3] or ""),
                "reason": str(r[4] or ""),
                "detail_json": r[5],
            })
    except Exception:
        pass

    return out


# --------------------------------------------------
# EXECUTION CONFIDENCE CALIBRATION
# --------------------------------------------------

def api_get_exec_conf_calib(_parsed=None, _ctx=None):
    try:
        from engine.exec_conf_calibration import get_latest_exec_conf_calib
        return get_latest_exec_conf_calib()
    except Exception as e:
        return {"ok": False, "error": str(e)}
