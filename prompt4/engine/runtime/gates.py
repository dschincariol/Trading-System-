# engine/runtime/gates.py
"""
Execution gating (fail-closed).

Provides:
- execution_gate_snapshot(): stable structured snapshot for UI/API/JobManager
- is_execution_job(job_name): identifies jobs tagged as execution-related
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

from engine.runtime.job_registry import ALLOWED_JOBS


def _now_ms() -> int:
    return int(time.time() * 1000)


def _env_mode() -> str:
    # Operator sets EXECUTION_MODE to safe|shadow|live (see boot/operator_server.js)
    m = (
        os.environ.get("EXECUTION_MODE")
        or os.environ.get("OPERATOR_MODE")
        or os.environ.get("MODE")
        or "safe"
    )
    return str(m).strip().lower() or "safe"


def is_execution_job(job_name: str) -> bool:
    """
    Job registry tags jobs with meta {"execution": True/False} at tuple index 3.
    """
    try:
        spec = ALLOWED_JOBS.get(job_name)
        if not spec:
            return False
        meta = spec[3] if len(spec) > 3 else {}
        return bool(isinstance(meta, dict) and meta.get("execution") is True)
    except Exception:
        return False


def execution_gate_snapshot(get_execution_mode_fn=None) -> Dict[str, Any]:
    """
    Returns a structured snapshot:
      {
        ok: bool,
        ts_ms: int,
        mode: "safe"|"shadow"|"live"|"...",
        armed: 0|1|None,
        allow_execution: bool,
        reason: str,
        source: str
      }

    Rules:
    - safe: execution blocked
    - shadow: execution allowed (backend should remain non-trading / simulated as designed)
    - live: requires armed==1 (if available), else blocked
    - unknown errors: fail-closed
    """
    ts = _now_ms()

    mode: str = _env_mode()
    armed: Optional[int] = None
    source = "env"
    reason = "ok"

    # Best-effort: if provided, use the system's execution mode function (DB-backed)
    if callable(get_execution_mode_fn):
        try:
            r = get_execution_mode_fn()
            if isinstance(r, dict):
                mode = str(r.get("mode") or mode).strip().lower() or mode
                if "armed" in r:
                    try:
                        armed = int(r.get("armed") or 0)
                    except Exception:
                        armed = None
                source = "get_execution_mode_fn"
            elif isinstance(r, str):
                mode = str(r).strip().lower() or mode
                source = "get_execution_mode_fn:str"
        except Exception as e:
            return {
                "ok": False,
                "ts_ms": ts,
                "mode": mode,
                "armed": armed,
                "allow_execution": False,
                "reason": f"execmode_error:{type(e).__name__}",
                "source": "get_execution_mode_fn:error",
            }

    allow_execution = False

    if mode == "safe":
        allow_execution = False
        reason = "mode_safe"
    elif mode == "shadow":
        allow_execution = True
        reason = "mode_shadow"
    elif mode == "live":
        if armed is None:
            # If we don't know armed status, fail-closed for live.
            allow_execution = False
            reason = "mode_live_unarmed_unknown"
        else:
            allow_execution = bool(armed == 1)
            reason = "mode_live_armed" if allow_execution else "mode_live_unarmed"
    else:
        allow_execution = False
        reason = f"mode_unknown:{mode}"

    return {
        "ok": True,
        "ts_ms": ts,
        "mode": mode,
        "armed": armed,
        "allow_execution": bool(allow_execution),
        "reason": reason,
        "source": source,
    }