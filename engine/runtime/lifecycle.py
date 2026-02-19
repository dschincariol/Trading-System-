# engine/runtime/lifecycle.py
"""
Global Runtime Lifecycle State Machine

States:
  BOOTING
  WARMING
  LIVE
  DEGRADED
  KILL
  SHUTDOWN
"""

import threading
import time
from typing import Callable, Dict


LIFECYCLE_STATES = ("BOOTING", "WARMING", "LIVE", "DEGRADED", "KILL", "SHUTDOWN")

_state = {
    "state": "BOOTING",
    "since_ms": int(time.time() * 1000),
    "last_transition_ms": int(time.time() * 1000),
    "last_error": "",
}

_lock = threading.Lock()


def _set_state(state: str, error: str = ""):
    st = str(state or "").upper().strip()
    if st not in LIFECYCLE_STATES:
        st = "DEGRADED"
        error = error or "invalid_state"

    now_ms = int(time.time() * 1000)

    with _lock:
        if _state["state"] != st:
            _state["state"] = st
            _state["since_ms"] = now_ms
            _state["last_transition_ms"] = now_ms
        if error:
            _state["last_error"] = str(error)[:500]


def snapshot() -> Dict:
    with _lock:
        return dict(_state)


def mark_shutdown():
    _set_state("SHUTDOWN")


def start_lifecycle_monitor(
    get_health: Callable[[], Dict],
    get_jobs: Callable[[], list],
    get_kill_switches: Callable[[], Dict],
    interval_s: float = 2.0,
):
    """
    Background thread that computes lifecycle state continuously.
    """

    def _loop():
        _set_state("BOOTING")

        while True:
            try:
                health = get_health() or {}
                jobs = get_jobs() or []
                kill = get_kill_switches() or {}

                # KILL state dominates
                if any(bool(v) for v in kill.values()):
                    _set_state("KILL")
                else:
                    prices_ok = bool(health.get("prices", {}).get("ok"))
                    labels_ok = bool(health.get("labels", {}).get("ok"))
                    model_ok = bool(health.get("model", {}).get("ok"))

                    if prices_ok and labels_ok and model_ok:
                        _set_state("LIVE")
                    else:
                        _set_state("DEGRADED")

            except Exception as e:
                _set_state("DEGRADED", error=str(e))

            time.sleep(max(0.5, float(interval_s)))

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
