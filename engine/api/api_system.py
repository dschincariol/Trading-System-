# CREATE NEW FILE: api_system.py
# Route specs for system/health endpoints.
# This file contains only route metadata (no runtime imports from dashboard_server.py).

ROUTE_SPECS = [
    ("GET", "/api/system/kill_switches", "api_get_kill_switches"),
    ("GET", "/api/system/state", "api_get_system_state"),
    ("GET", "/api/health", "api_get_health"),
]

ROUTE_SPECS_SYSTEM = ROUTE_SPECS

# ----------------------------------------------------------------------
# System endpoint implementations (moved from dashboard_server.py)
# ----------------------------------------------------------------------

from engine.runtime.health import get_health_snapshot
from engine.runtime.system_state import compute_system_state


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
    except Exception:
        kill_switches = {}

    state = compute_system_state(
        health=health,
        jobs=jobs,
        kill_switches=kill_switches,
    )

    return state
