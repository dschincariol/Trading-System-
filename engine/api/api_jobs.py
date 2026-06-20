# engine/api/api_jobs.py
# Route specs + implementations for job control + pipeline endpoints.
# This file contains no imports from dashboard_server.py.

import time
from engine.api.http_parsing import qs as _qs
from engine.runtime.job_registry import ALLOWED_JOBS, PIPELINE_ORDER, JOB_ORDER


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------

ROUTE_SPECS = [
    ("GET",  "/api/jobs/log",     "api_get_job_log"),
    ("GET",  "/api/jobs/history", "api_get_job_history"),
    ("GET",  "/api/jobs",         "api_get_jobs"),
    ("POST", "/api/jobs/start",   "api_post_job_start"),
    ("GET",  "/api/jobs/start",   "api_post_job_start"),
    ("POST", "/api/jobs/stop",    "api_post_job_stop"),
    ("GET",  "/api/jobs/stop",    "api_post_job_stop"),
    ("POST", "/api/pipeline/run", "api_post_pipeline_run"),
]

ROUTE_SPECS_JOBS = ROUTE_SPECS


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _job_name_from(parsed, body) -> str:
    q = _qs(parsed) or {}
    name = (q.get("name") or "").strip()
    if not name and isinstance(body, dict):
        name = str(body.get("name") or "").strip()
    return name


def _jobs_from(ctx):
    ctx = ctx or {}
    return ctx.get("JOBS")


# ----------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------

def api_get_jobs(parsed, _body=None, ctx=None):
    JOBS = _jobs_from(ctx)
    if not JOBS:
        return {"ok": False, "error": "jobs_manager_unavailable"}

    try:
        running = JOBS.list_jobs() or []
    except Exception:
        running = []

    running_by_name = {}
    for j in running:
        try:
            n = str(j.get("name") or "").strip()
            if n:
                running_by_name[n] = j
        except Exception:
            continue

    allowed_names = list(ALLOWED_JOBS.keys())

    order = list(JOB_ORDER or [])
    remaining = sorted([n for n in allowed_names if n not in set(order)])
    names = [n for n in order if n in set(allowed_names)] + remaining

    out = []
    for name in names:
        if name in running_by_name:
            out.append(running_by_name[name])
        else:
            out.append({
                "name": name,
                "script": (ALLOWED_JOBS.get(name) or ("", "", "", {}))[0],
                "mode": (ALLOWED_JOBS.get(name) or ("", "", "", {}))[1],
                "group": (ALLOWED_JOBS.get(name) or ("", "", "", {}))[2],
                "running": False,
                "started_at_ms": None,
                "exited_at_ms": None,
                "exit_code": None,
                "log_lines": 0,
                "stop_requested": False,
                "next_restart_ms": 0,
            })

    return {
        "ok": True,
        "ts_ms": int(time.time() * 1000),
        "jobs": out,
        "pipeline_order": list(PIPELINE_ORDER or []),
        "allowed": names,
    }


def api_post_job_start(parsed, body=None, ctx=None):
    JOBS = _jobs_from(ctx)
    if not JOBS:
        return {"ok": False, "error": "jobs_manager_unavailable"}

    name = _job_name_from(parsed, body)
    if not name:
        return {"ok": False, "error": "missing_name"}

    if name not in ALLOWED_JOBS:
        return {"ok": False, "error": "job_not_registered", "job": name}

    try:
        res = JOBS.start(name)
        if isinstance(res, dict):
            return res
        return {"ok": True, "job": name, "status": "started"}
    except Exception as e:
        return {"ok": False, "error": str(e), "job": name}


def api_post_job_stop(parsed, body=None, ctx=None):
    JOBS = _jobs_from(ctx)
    if not JOBS:
        return {"ok": False, "error": "jobs_manager_unavailable"}

    name = _job_name_from(parsed, body)
    if not name:
        return {"ok": False, "error": "missing_name"}

    if name not in ALLOWED_JOBS:
        return {"ok": False, "error": "job_not_registered", "job": name}

    try:
        res = JOBS.stop(name)
        if isinstance(res, dict):
            return res
        return {"ok": True, "job": name, "status": "stopped"}
    except Exception as e:
        return {"ok": False, "error": str(e), "job": name}


def api_get_job_log(parsed, _body=None, ctx=None):
    JOBS = _jobs_from(ctx)
    if not JOBS:
        return {"ok": False, "error": "jobs_manager_unavailable"}

    q = _qs(parsed) or {}
    name = str((q.get("name") or "")).strip()
    tail_s = str((q.get("tail") or "200")).strip()

    if not name:
        return {"ok": False, "error": "missing_name"}

    try:
        tail = max(1, min(4000, int(tail_s)))
    except Exception:
        tail = 200

    # JobManager already exposes get_job_log
    try:
        return JOBS.get_job_log(name, tail=tail)
    except Exception as e:
        return {"ok": False, "error": "job_log_exception", "detail": str(e), "job": name}


def api_get_job_history(parsed, _body=None, ctx=None):
    JOBS = _jobs_from(ctx)
    if not JOBS:
        return {"ok": False, "error": "jobs_manager_unavailable"}

    q = _qs(parsed) or {}
    name = str((q.get("name") or "")).strip()
    limit_s = str((q.get("limit") or "200")).strip()

    if not name:
        return {"ok": False, "error": "missing_name"}

    try:
        limit = max(1, min(5000, int(limit_s)))
    except Exception:
        limit = 200

    try:
        return JOBS.get_job_history(name, limit=limit)
    except Exception as e:
        return {"ok": False, "error": "job_history_exception", "detail": str(e), "job": name}


def api_post_pipeline_run(parsed, body=None, ctx=None):
    # Runs the PIPELINE_ORDER sequentially by starting each job (oneshots),
    # leaving daemon start/restart policy to supervisor.
    JOBS = _jobs_from(ctx)
    if not JOBS:
        return {"ok": False, "error": "jobs_manager_unavailable"}

    q = _qs(parsed) or {}
    include_execution = str(q.get("include_execution") or "").strip().lower() in ("1", "true", "yes", "y", "on")

    started = []
    skipped = []
    errors = []

    for name in (PIPELINE_ORDER or []):
        if name not in ALLOWED_JOBS:
            skipped.append({"job": name, "reason": "not_registered"})
            continue

        meta = (ALLOWED_JOBS.get(name) or ("", "", "", {}))[3] if len(ALLOWED_JOBS.get(name) or ()) >= 4 else {}
        if (meta or {}).get("execution") is True and not include_execution:
            skipped.append({"job": name, "reason": "execution_excluded"})
            continue

        try:
            res = JOBS.start(name)
            if isinstance(res, dict) and not res.get("ok", True):
                errors.append({"job": name, "error": res})
                # stop pipeline on first error (safer)
                break
            started.append({"job": name, "result": res})
        except Exception as e:
            errors.append({"job": name, "error": str(e)})
            break

    return {
        "ok": len(errors) == 0,
        "started": started,
        "skipped": skipped,
        "errors": errors,
        "pipeline_order": list(PIPELINE_ORDER or []),
    }