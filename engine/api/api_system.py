# engine/api/api_system.py
# Route specs for system/health/telemetry endpoints.
# This file contains route metadata + handler implementations.
# It does NOT import dashboard_server.py.

from engine.runtime.health import get_health_snapshot
from engine.runtime.system_state import compute_system_state


# ----------------------------------------------------------------------
# ROUTE DEFINITIONS
# ----------------------------------------------------------------------

ROUTE_SPECS_SYSTEM = [
    ("GET",  "/api/system/kill_switches", "api_get_kill_switches"),
    ("GET",  "/api/system/state",         "api_get_system_state"),
    ("GET",  "/api/health",               "api_get_health"),
    ("GET",  "/api/readiness",            "api_get_readiness"),
    ("GET",  "/api/allocator/status",     "api_get_allocator_status"),
    ("GET",  "/api/telemetry",            "api_get_telemetry"),
    ("GET",  "/api/training_status", "api_get_training_status"),
    ("GET",  "/api/server/status",        "api_get_server_status"),
    ("POST", "/api/server/shutdown",      "api_post_server_shutdown"),
    ("GET", "/api/execution/barrier", "api_get_execution_barrier"),
    ("GET", "/api/supervisor/status", "api_get_supervisor_status"),
    ("GET", "/api/system/config", "api_get_runtime_config"),
    ("POST", "/api/system/repair_schema", "api_post_repair_schema"),

]


# ----------------------------------------------------------------------
# SYSTEM STATE
# ----------------------------------------------------------------------
from dataclasses import asdict
from engine.runtime.config_schema import load_runtime_config, ConfigError

def api_get_runtime_config(_parsed, ctx):
    try:
        cfg = load_runtime_config()
        return {"ok": True, "config": asdict(cfg)}
    except ConfigError as e:
        return {"ok": False, "error": str(e)}

def api_get_system_state(_parsed, ctx):
    JOBS = ctx["JOBS"]

    try:
        health = get_health_snapshot()
    except Exception:
        health = {}

    try:
        jobs = JOBS.list_jobs()
    except Exception:
        jobs = []

    try:
        kill_switches = ctx["API_HANDLERS"]["api_get_kill_switches"](_parsed, ctx)
        # Normalize shape: api_get_kill_switches returns {"ok": True, "data": {...}}
        if isinstance(kill_switches, dict) and isinstance(kill_switches.get("data"), dict):
            kill_switches = kill_switches["data"]
    except Exception:
        kill_switches = {}

    state = compute_system_state(
        health=health,
        jobs=jobs,
        kill_switches=kill_switches,
    )

    return state

# ----------------------------------------------------------------------
# SUPERVISOR
# ----------------------------------------------------------------------
def api_get_supervisor_status(_parsed, ctx):
    sup = ctx.get("SUPERVISOR")
    if not sup:
        return {"ok": False, "error": "no supervisor"}

    try:
        snap = sup.status()
        return {"ok": True, **snap}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ----------------------------------------------------------------------
# HEALTH
# ----------------------------------------------------------------------

def api_get_health(_parsed, ctx):
    return get_health_snapshot()


# ----------------------------------------------------------------------
# READINESS
# ----------------------------------------------------------------------

def api_get_readiness(_parsed, ctx):
    SUPERVISOR = ctx["SUPERVISOR"]

    try:
        health = get_health_snapshot()
    except Exception:
        health = {"ok": False}

    try:
        sys_state = api_get_system_state(_parsed, ctx)
    except Exception:
        sys_state = {"ok": False}

    try:
        graph = SUPERVISOR.validate_graph(strict=True)
    except Exception:
        graph = {"ok": False}

    return {
        "ok": bool(
            health.get("ok")
            and sys_state.get("state") == "LIVE"
            and graph.get("ok")
        ),
        "health_ok": health.get("ok"),
        "system_state": sys_state.get("state"),
        "graph_valid": graph.get("ok"),
    }


# ----------------------------------------------------------------------
# TELEMETRY
# ----------------------------------------------------------------------
def api_get_telemetry(_parsed, ctx):
    import os
    import time

    ts = int(time.time() * 1000)

    db_path = os.environ.get("DB_PATH", "dev.db")
    db_size = 0
    try:
        if os.path.exists(db_path):
            db_size = os.path.getsize(db_path)
    except Exception:
        pass

    try:
        import psutil
        p = psutil.Process(os.getpid())

        return {
            "ok": True,
            "ts_ms": ts,
            "cpu_percent": psutil.cpu_percent(interval=0.2),
            "memory_percent": psutil.virtual_memory().percent,
            "process_rss_mb": round(p.memory_info().rss / (1024 * 1024), 2),
            "thread_count": p.num_threads(),
            "db_size_mb": round(db_size / (1024 * 1024), 2),
        }
    except Exception:
        # fallback without psutil
        return {
            "ok": True,
            "ts_ms": ts,
            "thread_count": 0,
            "db_size_mb": round(db_size / (1024 * 1024), 2),
            "psutil": False,
        }

from engine.runtime.execution_barrier import execution_barrier_decide
from engine.runtime.system_state import compute_system_state
from engine.runtime.health import get_health_snapshot


def api_get_execution_barrier(_parsed, _ctx=None):
    try:
        from engine.runtime.execution_barrier import execution_gate_snapshot
        snap = execution_gate_snapshot()
        if not isinstance(snap, dict):
            return {"ok": True, "allowed": True, "reason": "unknown_snapshot_format"}
        return {"ok": True, **snap}
    except Exception as e:
        return {"ok": True, "allowed": False, "reason": f"execution_barrier_error: {e}"}