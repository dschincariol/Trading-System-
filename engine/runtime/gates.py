"""
Execution Gating (Production Safety)

Single source of truth for LIVE execution permission.
Fail-closed by default.
"""

import os
from typing import Dict, Any

from engine.runtime.system_state import compute_system_state
from engine.runtime.health import get_health_snapshot


# ------------------------------------------------------------
# Execution Job Classification
# ------------------------------------------------------------

_EXECUTION_JOB_NAMES = {
    x.strip()
    for x in os.environ.get("EXECUTION_JOB_NAMES", "").split(",")
    if x.strip()
}

_EXECUTION_JOB_SUBSTRINGS = [
    x.strip().lower()
    for x in os.environ.get(
        "EXECUTION_JOB_SUBSTRINGS",
        "broker_apply,apply_orders,apply_latest_portfolio_orders,execute,live_execution,ibkr,alpaca",
    ).split(",")
    if x.strip()
]

# ------------------------------------------------------------
# Execution Permission Gate
# ------------------------------------------------------------

def execution_gate_snapshot(
    *,
    get_jobs,
    get_kill_switches,
    get_execution_mode,
) -> Dict[str, Any]:
    """
    Determines whether LIVE execution is allowed.

    Requirements:
      - system_state == LIVE
      - execution_mode.armed == True (if available)

    Fail-closed if execution_mode unavailable.
    """

    try:
        health = get_health_snapshot()
    except Exception:
        health = {}

    try:
        jobs = get_jobs() or []
    except Exception:
        jobs = []

    try:
        kill_switches = get_kill_switches() or {}
    except Exception:
        kill_switches = {}

    state = compute_system_state(
        health=health,
        jobs=jobs,
        kill_switches=kill_switches,
    )

    lifecycle_state = str(state.get("state") or "")

    if lifecycle_state != "LIVE":
        return {
            "ok": False,
            "error": "execution_blocked_not_live",
            "system_state": state,
        }

    try:
        em = get_execution_mode() or {}
        armed = bool(em.get("armed")) if isinstance(em, dict) else False
        if not armed:
            return {
                "ok": False,
                "error": "execution_blocked_not_armed",
                "execution_mode": em,
                "system_state": state,
            }
    except Exception:
        return {
            "ok": False,
            "error": "execution_blocked_exec_mode_unavailable",
            "system_state": state,
        }

    return {"ok": True, "system_state": state}
