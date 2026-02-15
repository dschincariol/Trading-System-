# engine/api/api_system_handlers.py

from engine.runtime.health import get_health_snapshot
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
