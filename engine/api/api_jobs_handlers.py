# engine/api/api_jobs_handlers.py
"""
Job control + pipeline endpoints.

- No dashboard_server imports.
- Uses injected ctx from http_transport.build_handler().
"""

from __future__ import annotations

import time
from urllib.parse import parse_qs

from engine.runtime.job_registry import ALLOWED_JOBS, JOB_ORDER, PIPELINE_ORDER
from engine.runtime.lifecycle import snapshot as lifecycle_snapshot
from engine.runtime.gates import execution_gate_snapshot, is_execution_job

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

def _job_name_from(parsed, body) -> str:
    qs = _qs(parsed)
    name = (qs.get("name") or "").strip()
    if not name and isinstance(body, dict):
        name = str(body.get("name") or "").strip()
    return name


def api_get_jobs(_parsed, ctx):
    """
    Returns deterministic list for UI:
      - running jobs from JobManager
      - plus non-running allowed jobs (status=stopped)
      - ordered by JOB_ORDER then remaining alphabetical
    """
    JOBS = ctx.get("JOBS")
    if JOBS is None:
        return {"ok": False, "error": "missing_ctx:JOBS"}

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

    try:
        allowed_names = list(ALLOWED_JOBS.keys())
    except Exception:
        allowed_names = []

    try:
        order = list(JOB_ORDER or [])
    except Exception:
        order = []

    remaining = sorted([n for n in allowed_names if n not in set(order)])
    names = [n for n in order if n in set(allowed_names)] + remaining

    out = []
    for name in names:
        if name in running_by_name:
            out.append(running_by_name[name])
        else:
            out.append(
                {
                    "name": name,
                    "pid": None,
                    "started_ts_ms": None,
                    "state": "stopped",
                    "exit_code": None,
                    "meta": {},
                }
            )

    return {
        "ok": True,
        "ts_ms": int(time.time() * 1000),
        "jobs": out,
        "pipeline_order": list(PIPELINE_ORDER or []),
        "allowed": names,
    }


def api_post_job_start(parsed, body, ctx):
    denied = _deny_if_shutdown()
    if denied:
        return denied

    JOBS = ctx.get("JOBS")
    if JOBS is None:
        return {"ok": False, "error": "missing_ctx:JOBS"}

    name = _job_name_from(parsed, body)
    if not name:
        return {"ok": False, "error": "missing_name"}

    if name not in ALLOWED_JOBS:
        return {"ok": False, "error": f"job_not_allowed:{name}"}

    # Only gate execution-tagged jobs here. Non-execution ingest/monitor jobs must run in SAFE mode.
    if is_execution_job(name):
        gate = execution_gate_snapshot(
            get_execution_mode_fn=JOBS.get_execution_mode_fn
        )
        if not gate.get("allow_execution"):
            return {
                "ok": False,
                "error": f"execution_gated:{gate.get('reason')}",
                "gate": gate,
            }
    try:
        res = JOBS.start(name)  # hard execution gating is enforced inside JobManager.start()
    except Exception as e:
        res = {"ok": False, "error": str(e)}

    try:
        from engine.api.api_write import write_job_event

        write_job_event(job_name=name, event="start", detail=res)
    except Exception:
        pass

    return res

def api_post_job_stop(parsed, body, ctx):
    denied = _deny_if_shutdown()
    if denied:
        return denied

    JOBS = ctx.get("JOBS")
    if JOBS is None:
        return {"ok": False, "error": "missing_ctx:JOBS"}

    name = _job_name_from(parsed, body)
    if not name:
        return {"ok": False, "error": "missing_name"}

    if name not in ALLOWED_JOBS:
        return {"ok": False, "error": f"job_not_allowed:{name}"}

    try:
        res = JOBS.stop(name)
    except Exception as e:
        res = {"ok": False, "error": str(e)}

    try:
        from engine.api.api_write import write_job_event

        write_job_event(job_name=name, event="stop", detail=res)
    except Exception:
        pass

    return res


def api_post_pipeline_run(parsed, body, ctx):
    denied = _deny_if_shutdown()
    if denied:
        return denied

    """
    Runs pipeline in-order, with orchestrator-level locking.
    Optional:
      - ?include_execution=1 (or body {"include_execution": true})
    """
    ORCHESTRATOR = ctx.get("ORCHESTRATOR")
    if ORCHESTRATOR is None:
        return {"ok": False, "error": "missing_ctx:ORCHESTRATOR"}

    qs = _qs(parsed)
    inc = qs.get("include_execution", "")
    if not inc and isinstance(body, dict):
        inc = body.get("include_execution", "")

    include_execution = str(inc).strip() in ("1", "true", "True", "yes", "YES")

    gate = execution_gate_snapshot(
        get_execution_mode_fn=ctx.get("JOBS").get_execution_mode_fn
        if ctx.get("JOBS") else None
    )
    if include_execution and not gate.get("allow_execution"):
        return {
            "ok": False,
            "error": f"execution_gated:{gate.get('reason')}",
            "gate": gate,
        }

    try:
        res = ORCHESTRATOR.run_pipeline(include_execution=include_execution)

    except Exception as e:
        res = {"ok": False, "error": str(e)}

    try:
        from engine.api.api_write import write_job_event

        write_job_event(job_name="pipeline", event="run", detail=res)
    except Exception:
        pass

    return res
