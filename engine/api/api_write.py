"""
Write-only API layer.

All DB mutations previously inside dashboard_server.py now live here.
No supervisor logic.
No runtime orchestration.
Pure DB mutations.
"""

import time
from engine.dev_core.storage import connect as _db_connect


# ============================================================
# ALERT ACK / RESOLVE
# ============================================================

def ack_alert(alert_id: int, who: str = "", source: str = ""):
    con = _db_connect()
    try:
        con.execute(
            """
            INSERT OR REPLACE INTO alert_acks
            (alert_id, acked_ts_ms, acked_by, source)
            VALUES (?,?,?,?)
            """,
            (
                int(alert_id),
                int(time.time() * 1000),
                str(who or ""),
                str(source or ""),
            ),
        )
        con.commit()
        return {"ok": True}
    finally:
        con.close()


def resolve_alert(alert_id: int, who: str = "", reason: str = "", source: str = ""):
    con = _db_connect()
    try:
        con.execute(
            """
            INSERT OR IGNORE INTO alert_resolutions
            (alert_id, resolved_ts_ms, resolved_by, reason, source)
            VALUES (?,?,?,?,?)
            """,
            (
                int(alert_id),
                int(time.time() * 1000),
                str(who or ""),
                str(reason or ""),
                str(source or ""),
            ),
        )
        con.commit()
        return {"ok": True}
    finally:
        con.close()


# ============================================================
# JOB HISTORY
# ============================================================

def write_job_event(job_name: str, event: str, detail: dict | None = None):
    from engine.runtime.locks import write_job_history

    try:
        write_job_history(job_name=job_name, event=event, detail=detail or {})
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ============================================================
# PROMOTION GUARD
# ============================================================

def set_promotion_enabled(value: str):
    from engine.dev_core.promotion_guard import set_guard

    v = "1" if str(value) == "1" else "0"
    set_guard("promotion_enabled", v)
    return {"ok": True, "promotion_enabled": v}
