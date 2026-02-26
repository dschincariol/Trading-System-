# engine/api/api_handlers.py
"""
Legacy compatibility handlers.

dashboard_server.py imports these (best-effort):
  - api_get_kill_switches
  - api_get_job_log
  - api_get_job_history

All other endpoints have been refactored into:
  - engine.api.api_system_handlers
  - engine.api.api_jobs_handlers
  - engine.api.api_ops_handlers
"""

from __future__ import annotations

from typing import Any, Dict
from engine.api.http_parsing import qs as _qs


def api_get_kill_switches(parsed: Any, _ctx: Dict[str, Any] | None = None) -> Dict[str, Any]:
    try:
        from engine.execution.kill_switch import snapshot as _snapshot
        return {"ok": True, "data": _snapshot()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_get_job_log(parsed: Any, ctx: Dict[str, Any] | None = None) -> Dict[str, Any]:
    q = _qs(parsed)
    name = (q.get("name") or q.get("job") or "").strip()
    try:
        tail = int(q.get("tail") or "400")
    except Exception:
        tail = 400

    if not name:
        return {"ok": False, "error": "missing_job_name"}

    try:
        if not isinstance(ctx, dict) or "JOBS" not in ctx:
            return {"ok": False, "error": "missing_ctx_jobs"}
        JOBS = ctx["JOBS"]
        out = JOBS.get_job_log(name=name, tail=tail)
        if isinstance(out, dict):
            out.setdefault("ok", True)
            return out
        return {"ok": True, "data": out}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_get_job_history(parsed, body=None, ctx=None):
    if not _api_get_job_history_impl:
        return {"ok": False, "error": "job_history_unavailable"}

    try:
        ctx = ctx or {}
        if "JOBS" not in ctx:
            ctx["JOBS"] = JOBS

        return _api_get_job_history_impl(parsed, body, ctx)

    except Exception as e:
        return {"ok": False, "error": "job_history_exception", "detail": str(e)}

    try:
        if not isinstance(ctx, dict) or "JOBS" not in ctx:
            return {"ok": False, "error": "missing_ctx_jobs"}
        JOBS = ctx["JOBS"]
        out = JOBS.get_job_history(limit=limit)
        if isinstance(out, dict):
            out.setdefault("ok", True)
            return out
        return {"ok": True, "data": out}
    except Exception as e:
        return {"ok": False, "error": str(e)}