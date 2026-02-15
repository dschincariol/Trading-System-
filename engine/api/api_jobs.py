# CREATE NEW FILE: api_jobs.py
# Route specs for job control + pipeline endpoints.
# This file contains only route metadata (no runtime imports from dashboard_server.py).

ROUTE_SPECS = [
    ("GET", "/api/jobs/log", "api_get_job_log"),
    ("GET", "/api/jobs/history", "api_get_job_history"),
    ("GET", "/api/jobs", "api_get_jobs"),
    ("POST", "/api/jobs/start", "api_post_job_start"),
    ("POST", "/api/jobs/stop", "api_post_job_stop"),
    ("POST", "/api/pipeline/run", "api_post_pipeline_run"),
]

ROUTE_SPECS_JOBS = ROUTE_SPECS

# ----------------------------------------------------------------------
# Job endpoint implementations (moved from dashboard_server.py)
# ----------------------------------------------------------------------

import time
from urllib.parse import parse_qs
from engine.runtime.job_registry import ALLOWED_JOBS, PIPELINE_ORDER, JOB_ORDER


def _qs(parsed):
    try:
        q = parse_qs(parsed.query or "")
        return {k: v[0] for k, v in q.items()}
    except Exception:
        return {}


def _deny_if_shutdown():
    try:
        snap = lifecycle_snapshot() or {}
        if str(snap.get("state") or "").upper() == "SHUTDOWN":
            return {"ok": False, "error": "server_shutting_down"}
    except Exception:
        pass
    return None
    try:
        q = parse_qs(parsed.query or "")
        return {k: v[0] for k, v in q.items()}
    except Exception:
        return {}


def _job_name_from(parsed, body) -> str:
    qs = _qs(parsed)
    name = (qs.get("name") or "").strip()
    if not name and isinstance(body, dict):
        name = str(body.get("name") or "").strip()
    return name


def api_get_jobs(parsed, ctx):
    JOBS = ctx["JOBS"]

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
                "pid": None,
                "started_ts_ms": None,
                "state": "stopped",
                "exit_code": None,
                "meta": {},
            })

    return {
        "ok": True,
        "ts_ms": int(time.time() * 1000),
        "jobs": out,
        "pipeline_order": list(PIPELINE_ORDER or []),
        "allowed": names,
    }


def api_post_job_start(parsed, body, ctx):
    JOBS = ctx["JOBS"]

    name = _job_name_from(parsed, body)
    if not name:
        return {"ok": False, "error": "missing_name"}

    if name not in ALLOWED_JOBS:
        return {"ok": False, "error": f"job_not_allowed:{name}"}

    try:
        return JOBS.start(name)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_post_job_stop(parsed, body, ctx):
    JOBS = ctx["JOBS"]

    name = _job_name_from(parsed, body)
    if not name:
        return {"ok": False, "error": "missing_name"}

    if name not in ALLOWED_JOBS:
        return {"ok": False, "error": f"job_not_allowed:{name}"}

    try:
        return JOBS.stop(name)
    except Exception as e:
        return {"ok": False, "error": str(e)}
