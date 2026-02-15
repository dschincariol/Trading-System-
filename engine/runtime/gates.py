"""
Execution Gating (Production Safety)

Single source of truth for LIVE execution permission.
Fail-closed by default.
"""

import os
from typing import Dict, Any, Callable, Optional

from engine.runtime.system_state import compute_system_state
from engine.runtime.health import get_health_snapshot

# DB connect (compat bridge supports engine.dev_core.* -> top-level dev_core/)
from engine.dev_core.storage import connect as _db_connect

# Hard global kill switch (fail closed)
_DISABLE_LIVE_EXECUTION = os.environ.get("DISABLE_LIVE_EXECUTION", "0").strip() == "1"

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
    get_execution_mode_fn: Optional[Callable[[], Dict[str, Any]]] = None
) -> Dict[str, object]:
    """
    Unified execution gate snapshot (single source of truth).
    API-compatible: callers may pass get_execution_mode_fn() to avoid DB reads.

    Returns BOTH:
      - allow_execution: bool  (canonical key used by runtime)
      - ok: bool              (back-compat alias)

    Fail-closed semantics.
    """
    if _DISABLE_LIVE_EXECUTION:
        return {
            "ok": False,
            "allow_execution": False,
            "mode": "SAFE",
            "armed": 0,
            "reason": "DISABLE_LIVE_EXECUTION=1",
            "updated_ts_ms": 0,
            "lifecycle_state": "UNKNOWN",
        }

    # ---------------------------------------------
    # Read execution mode (prefer injected function)
    # ---------------------------------------------
    mode = "UNKNOWN"
    reason = ""
    updated_ts_ms = 0
    armed = 0

    if callable(get_execution_mode_fn):
        try:
            snap = get_execution_mode_fn() or {}
            mode = str(snap.get("mode") or "").strip().upper() or "UNKNOWN"
            reason = str(snap.get("reason") or "").strip()
            updated_ts_ms = int(snap.get("updated_ts_ms") or 0)
            armed = int(snap.get("armed") or 0)
        except Exception as e:
            return {
                "ok": False,
                "allow_execution": False,
                "mode": "UNKNOWN",
                "armed": 0,
                "reason": f"get_execution_mode_failed:{e}",
                "updated_ts_ms": 0,
                "lifecycle_state": "UNKNOWN",
            }
    else:
        try:
            con = _db_connect(readonly=True)
        except Exception as e:
            return {
                "ok": False,
                "allow_execution": False,
                "mode": "UNKNOWN",
                "armed": 0,
                "reason": f"db_open_failed:{e}",
                "updated_ts_ms": 0,
                "lifecycle_state": "UNKNOWN",
            }

        try:
            try:
                row = con.execute(
                    "SELECT mode, reason, updated_ts_ms, armed FROM execution_mode ORDER BY updated_ts_ms DESC LIMIT 1"
                ).fetchone()
            except Exception:
                row = con.execute(
                    "SELECT mode, reason, updated_ts_ms FROM execution_mode ORDER BY updated_ts_ms DESC LIMIT 1"
                ).fetchone()
                if row is not None:
                    row = (row[0], row[1], row[2], 0)

            if not row:
                return {
                    "ok": False,
                    "allow_execution": False,
                    "mode": "SAFE",
                    "armed": 0,
                    "reason": "no_execution_mode_row",
                    "updated_ts_ms": 0,
                    "lifecycle_state": "UNKNOWN",
                }

            mode = str(row[0] or "").strip().upper()
            reason = str(row[1] or "").strip()
            updated_ts_ms = int(row[2] or 0)
            armed = int(row[3] or 0)
        except Exception as e:
            return {
                "ok": False,
                "allow_execution": False,
                "mode": "UNKNOWN",
                "armed": 0,
                "reason": f"gate_read_failed:{e}",
                "updated_ts_ms": 0,
                "lifecycle_state": "UNKNOWN",
            }
        finally:
            try:
                con.close()
            except Exception:
                pass

    # ---------------------------------------------
    # Lifecycle must also be LIVE (state machine)
    # ---------------------------------------------
    try:
        health = get_health_snapshot()
        system = compute_system_state(
            health=health,
            jobs=None,
            kill_switches=None,
        )
        lifecycle_state = str(system.get("state") or "").upper()
    except Exception:
        lifecycle_state = "UNKNOWN"

    allow = bool(mode == "LIVE" and int(armed) == 1 and lifecycle_state == "LIVE")

    return {
        "ok": bool(allow),  # back-compat
        "allow_execution": bool(allow),
        "mode": mode,
        "armed": int(armed),
        "reason": reason,
        "updated_ts_ms": int(updated_ts_ms),
        "lifecycle_state": lifecycle_state,
    }

def is_execution_job(name: str) -> bool:
    """
    True if job is considered "execution" (must be hard-gated).
    Uses:
      - ALLOWED_JOBS metadata if present (meta["execution"] == True)
      - fallback to EXECUTION_JOB_NAMES / EXECUTION_JOB_SUBSTRINGS
    """
    if not name:
        return False

    n = str(name).strip()

    # Primary: registry metadata
    try:
        from engine.runtime.job_registry import ALLOWED_JOBS
        spec = ALLOWED_JOBS.get(n)
        if spec and isinstance(spec, (tuple, list)) and len(spec) >= 4:
            meta = spec[3] or {}
            if isinstance(meta, dict) and bool(meta.get("execution")):
                return True
    except Exception:
        pass

    # Fallback: explicit list + substring rules
    try:
        if n in _EXECUTION_JOB_NAMES:
            return True
    except Exception:
        pass

    try:
        ln = n.lower()
        for s in _EXECUTION_JOB_SUBSTRINGS:
            if s and str(s).lower() in ln:
                return True
    except Exception:
        pass

    return False
