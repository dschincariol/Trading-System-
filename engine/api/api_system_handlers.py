# engine/api/api_system_handlers.py

import os
import time
import threading

from engine.runtime.health import get_health_snapshot, run_preflight
from engine.runtime.system_state import compute_system_state


def api_get_health(_parsed, _ctx):
    return get_health_snapshot()


def api_get_system_state(_parsed, ctx):
    JOBS = ctx.get("JOBS")

    try:
        health = get_health_snapshot()
    except Exception:
        health = {}

    try:
        jobs = JOBS.list_jobs() if JOBS else []
    except Exception:
        jobs = []

    try:
        kill_switches = ctx["API_HANDLERS"]["api_get_kill_switches"](_parsed, ctx)
    except Exception:
        kill_switches = {}

    state = compute_system_state(
        health=health,
        jobs=jobs,
        kill_switches=kill_switches,
    )

    return state


def api_get_readiness(_parsed, ctx):
    """
    Structured readiness snapshot for GUI + automation.
    """
    # health
    try:
        health = get_health_snapshot()
    except Exception:
        health = {"ok": False, "error": "health_exception"}

    # system state
    try:
        state = api_get_system_state(_parsed, ctx)
    except Exception:
        state = {"ok": False, "state": "UNKNOWN", "error": "state_exception"}

    # dependency graph
    try:
        sup = ctx.get("SUPERVISOR")
        graph = sup.validate_graph(strict=True) if sup else {"ok": False, "error": "supervisor_missing"}
    except Exception:
        graph = {"ok": False, "error": "graph_exception"}

    # preflight (optional but useful for readiness)
    try:
        preflight = run_preflight()
    except Exception:
        preflight = {"ok": False, "error": "preflight_exception"}

    ok = bool(
        health.get("ok")
        and state.get("ok")
        and state.get("state") == "LIVE"
        and graph.get("ok")
        and preflight.get("ok")
    )

    return {
        "ok": ok,
        "ts_ms": int(time.time() * 1000),

        "health": health,
        "system_state": state,
        "graph": graph,
        "preflight": preflight,
    }


def api_get_telemetry(_parsed, _ctx):
    """
    Live resource telemetry.
    Uses psutil if installed; otherwise returns a safe partial snapshot without new deps.
    """
    ts = int(time.time() * 1000)

    # DB size (works without deps)
    db_path = os.environ.get("DB_PATH", "dev.db")
    db_size_bytes = 0
    try:
        if db_path and os.path.exists(db_path):
            db_size_bytes = os.path.getsize(db_path)
    except Exception:
        db_size_bytes = 0

    # Optional psutil (more detail)
    try:
        import psutil  # optional
        p = psutil.Process(os.getpid())

        return {
            "ok": True,
            "ts_ms": ts,
            "psutil": True,

            "cpu_percent": psutil.cpu_percent(interval=0.2),
            "mem_percent": psutil.virtual_memory().percent,
            "process_rss_mb": round(p.memory_info().rss / (1024 * 1024), 2),
            "thread_count": p.num_threads(),

            "db_path": db_path,
            "db_size_mb": round(db_size_bytes / (1024 * 1024), 2),
        }
    except Exception:
        # Fallback: safe partial telemetry
        return {
            "ok": True,
            "ts_ms": ts,
            "psutil": False,

            "thread_count": int(threading.active_count()),

            "db_path": db_path,
            "db_size_mb": round(db_size_bytes / (1024 * 1024), 2),
        }
