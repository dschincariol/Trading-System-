# engine/api/api_system_handlers.py

import os
import time
import threading

from engine.runtime.health import get_health_snapshot, run_preflight
from engine.runtime.system_state import compute_system_state
from engine.runtime.jobs.repair_schema import run as repair_schema
from engine.runtime.first_run import bootstrap_first_run

def api_post_repair_schema(_parsed):
    return repair_schema()

def api_post_self_heal(_parsed, ctx):
    """
    Non-technical one-click recovery:
    - stop jobs
    - db guard + schema repair + minimal seeding
    - restart price daemon
    """
    JOBS = ctx.get("JOBS")
    SUP = ctx.get("SUPERVISOR")

    out = {
        "ok": True,
        "stopped": [],
        "bootstrap": None,
        "restart": None,
    }

    try:
        if JOBS:
            try:
                JOBS.stop_all()
                out["stopped"] = [j.get("name") for j in (JOBS.list_jobs() or []) if not j.get("running")]
            except Exception:
                pass
    except Exception:
        pass

    try:
        out["bootstrap"] = bootstrap_first_run(mode=(os.environ.get("ENGINE_MODE","safe") or "safe"))
    except Exception as e:
        out["ok"] = False
        out["bootstrap"] = {"ok": False, "error": str(e)}

    try:
        if SUP:
            out["restart"] = SUP.deterministic_start(["stream_prices_polygon_ws"], include_deps=True, strict=False)
        elif JOBS:
            out["restart"] = JOBS.start("stream_prices_polygon_ws")
        else:
            out["restart"] = {"ok": False, "error": "no_supervisor"}
    except Exception as e:
        out["ok"] = False
        out["restart"] = {"ok": False, "error": str(e)}

    return out

def api_get_health(_parsed, _ctx):
    try:
        h = get_health_snapshot()
        if not isinstance(h, dict):
            return {"ok": False, "error": "invalid_health_snapshot"}

        mode = (os.environ.get("ENGINE_MODE", "") or "safe").strip().lower()

        prices_ok = bool((h.get("prices") or {}).get("ok"))
        barrier_allowed = bool((h.get("execution_barrier") or {}).get("allowed", False))

        # SAFE is allowed to warm up while prices start streaming
        if mode == "safe":
            ok = True
        else:
            ok = prices_ok and barrier_allowed

        h["ok"] = bool(ok)
        h.setdefault("mode", mode)
        return h
    except Exception as e:
        return {"ok": False, "error": str(e)}

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

    # Ensure stable API contract for UI console
    if isinstance(state, dict):
        state.setdefault("ok", True)
        return state

    return {
        "ok": False,
        "state": "UNKNOWN",
        "error": "invalid_system_state",
    }


def api_get_readiness(_parsed, ctx):
    """
    Structured readiness snapshot for GUI + automation.
    """
    # health
    try:
        health = get_health_snapshot()
    except Exception:
        health = {"ok": False, "error": "health_exception"}

    # execution gate (from health snapshot)
    execution = (health.get("execution") or {})
    execution_allowed = bool(execution.get("allowed"))

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

    # allocator (best-effort; do not crash readiness)
    allocator = {"ok": False, "error": "allocator_unavailable"}
    try:
        from engine.runtime.allocator_status import get_allocator_status
        allocator = get_allocator_status(window_days=0) or allocator
    except Exception as e:
        allocator = {"ok": False, "error": str(e)}

    require_allocator = str(os.environ.get("READINESS_REQUIRE_ALLOCATOR", "0") or "0").strip() == "1"

    ok = bool(
        health.get("ok")
        and execution_allowed
        and state.get("ok")
        and state.get("state") == "LIVE"
        and graph.get("ok")
        and preflight.get("ok")
        and ((allocator.get("ok") is True) if require_allocator else True)
    )

    return {
        "ok": ok,
        "ts_ms": int(time.time() * 1000),

        "execution_allowed": execution_allowed,

        "health": health,
        "system_state": state,
        "graph": graph,
        "preflight": preflight,

        "allocator": allocator,
        "allocator_required": bool(require_allocator),
    }

def api_get_allocator_status(parsed, _ctx):
    try:
        from engine.api.http_parsing import qs as _qs
        q = _qs(parsed)
        try:
            wd = int(q.get("window_days") or "0")
        except Exception:
            wd = 0

        from engine.runtime.allocator_status import get_allocator_status
        return get_allocator_status(window_days=int(wd))
    except Exception as e:
        return {"ok": False, "error": str(e)}

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
