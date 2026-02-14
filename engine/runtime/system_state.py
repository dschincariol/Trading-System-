import time
from typing import Dict, Any, List


STATE_BOOTING = "BOOTING"
STATE_WARMING_UP = "WARMING_UP"
STATE_LIVE = "LIVE"
STATE_DEGRADED = "DEGRADED"
STATE_KILL_SWITCH = "KILL_SWITCH"
STATE_SHUTDOWN = "SHUTDOWN"


def _now_ms() -> int:
    return int(time.time() * 1000)


def compute_system_state(
    health: Dict[str, Any],
    jobs: List[Dict[str, Any]],
    kill_switches: Dict[str, Any] = None,
) -> Dict[str, Any]:
    """
    Pure function: computes global system state from health + jobs + kill switches.
    No imports from your dashboard_server.py to avoid cycles.
    """
    out: Dict[str, Any] = {
        "ok": True,
        "ts_ms": _now_ms(),
        "state": STATE_BOOTING,
        "reasons": [],
        "jobs": {"running_daemons": [], "running_oneshots": []},
    }

    kill_switches = kill_switches or {}

    # Explicit shutdown state (from lifecycle snapshot)
    try:
        lifecycle = (health or {}).get("lifecycle") or {}
        if lifecycle.get("shutdown") is True:
            out["state"] = STATE_SHUTDOWN
            out["reasons"].append("lifecycle_shutdown")
            out["ok"] = False
            return out
    except Exception:
        pass


    # detect running daemons
    running_daemons = []
    running_oneshots = []
    for j in (jobs or []):
        try:
            if j.get("running"):
                if str(j.get("mode") or "") == "daemon":
                    running_daemons.append(str(j.get("name") or ""))
                else:
                    running_oneshots.append(str(j.get("name") or ""))
        except Exception:
            continue

    out["jobs"]["running_daemons"] = running_daemons
    out["jobs"]["running_oneshots"] = running_oneshots

    # health snapshot (your health_checks / dashboard_server health)
    prices_ok = bool((health or {}).get("prices", {}).get("ok"))
    labels_ok = bool((health or {}).get("labels", {}).get("ok"))
    model_ok = bool((health or {}).get("model", {}).get("ok"))

    prices_age_s = float((health or {}).get("prices", {}).get("age_s") or 1e9)

    # kill switch / overlays (if provided)
    ks_enabled = False
    try:
        if kill_switches.get("enabled") is True:
            ks_enabled = True
        elif kill_switches.get("state") == "KILL":
            ks_enabled = True
        elif isinstance(kill_switches.get("kill_switches"), dict):
            for v in kill_switches["kill_switches"].values():
                if isinstance(v, dict) and v.get("enabled") is True:
                    ks_enabled = True
                    break
    except Exception:
        pass


    if ks_enabled:
        out["state"] = STATE_KILL_SWITCH
        out["reasons"].append("kill_switch_enabled")
        out["ok"] = False
        return out

    # BOOTING -> if nothing is ready
    if not jobs:
        out["state"] = STATE_BOOTING
        out["reasons"].append("no_jobs_visible")
        out["ok"] = False
        return out

    # WARMING_UP: core dependencies not ready
    if not (prices_ok and labels_ok and model_ok):
        out["state"] = STATE_WARMING_UP
        if not prices_ok:
            out["reasons"].append("prices_not_ok")
        if not labels_ok:
            out["reasons"].append("labels_not_ok")
        if not model_ok:
            out["reasons"].append("model_not_ok")
        out["ok"] = False
        return out

    # LIVE/DEGRADED freshness threshold (env-controlled)
    try:
        max_age_s = float(__import__("os").environ.get("HEALTH_PRICES_MAX_AGE_S", "120"))
    except Exception:
        max_age_s = 120.0

    # LIVE: at least one price daemon running and prices are fresh
    has_price_daemon = ("poll_prices" in running_daemons) or ("stream_prices_polygon_ws" in running_daemons)
    if has_price_daemon and prices_age_s <= float(max_age_s):
        out["state"] = STATE_LIVE
        return out

    # DEGRADED: health ok, but price daemon not running or prices stale
    out["state"] = STATE_DEGRADED
    if not has_price_daemon:
        out["reasons"].append("no_price_daemon_running")
    if prices_age_s > float(max_age_s):
        out["reasons"].append(f"prices_stale_age_s={prices_age_s:.1f}")
