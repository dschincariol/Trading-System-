# FILE: engine/runtime/runtime_bootstrap.py
"""
Runtime bootstrap (idempotent)

Purpose:
- Centralize DB + coordination table bootstrap
- Keep dashboard_server.py / bootstrap_server.py thin

Notes:
- NO job starts here
- NO schema creation beyond coordination tables
"""

from __future__ import annotations

from engine.api.internal_access import init_db as _init_db
from engine.runtime.locks import _ensure_job_locks, _ensure_job_history


def bootstrap_runtime(log=None) -> dict:
    """
    Bootstraps runtime prerequisites.
    Safe to call multiple times.
    """
    out = {
        "ok": True,
        "init_db": False,
        "job_locks": False,
        "job_history": False,
        "errors": [],
    }

    # ---------------------------------------------------
    # HARD DB BOOTSTRAP (idempotent, REQUIRED)
    # ---------------------------------------------------
    try:
        _init_db()
        out["init_db"] = True
    except Exception as e:
        out["ok"] = False
        out["errors"].append(f"init_db:{e}")
        if log:
            try:
                log.critical("bootstrap_runtime init_db failed: %s", e)
            except Exception:
                pass
        return out  # fail-fast

    # ---------------------------------------------------
    # Cross-process coordination tables (idempotent)
    # ---------------------------------------------------
    try:
        _ensure_job_locks()
        out["job_locks"] = True
    except Exception as e:
        out["ok"] = False
        out["errors"].append(f"ensure_job_locks:{e}")
        if log:
            try:
                log.error("bootstrap_runtime ensure_job_locks failed: %s", e)
            except Exception:
                pass

    try:
        _ensure_job_history()
        out["job_history"] = True
    except Exception as e:
        out["ok"] = False
        out["errors"].append(f"ensure_job_history:{e}")
        if log:
            try:
                log.error("bootstrap_runtime ensure_job_history failed: %s", e)
            except Exception:
                pass

    return out
