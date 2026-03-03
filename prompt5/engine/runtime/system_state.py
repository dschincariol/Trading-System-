import time
import os
from typing import Dict, Any, List

from engine.runtime.config_schema import load_runtime_config, ConfigError


STATE_BOOTING = "BOOTING"
STATE_WARMING_UP = "WARMING_UP"
STATE_LIVE = "LIVE"
STATE_DEGRADED = "DEGRADED"
STATE_KILL_SWITCH = "KILL_SWITCH"
STATE_SHUTDOWN = "SHUTDOWN"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def compute_system_state(
    health: Dict[str, Any],
    jobs: List[Dict[str, Any]],
    kill_switches: Dict[str, Any] = None,
) -> Dict[str, Any]:
    """
    Pure function: computes global system state from health + jobs + kill switches.
    Fail-closed semantics.
    """

    out: Dict[str, Any] = {
        "ok": True,
        "ts_ms": _now_ms(),
        "state": STATE_BOOTING,
        "reasons": [],
        "jobs": {"running_daemons": [], "running_oneshots": []},
    }

    kill_switches = kill_switches or {}
    health = health or {}

    # -------------------------------------------------------
    # CONFIG VALIDATION (fail closed)
    # -------------------------------------------------------
    try:
        load_runtime_config()
    except ConfigError as e:
        out["state"] = STATE_DEGRADED
        out["reasons"].append(f"config_error:{e}")
        out["ok"] = False
        return out

    # -------------------------------------------------------
    # Explicit shutdown state
    # -------------------------------------------------------
    try:
        lifecycle = health.get("lifecycle") or {}
        if lifecycle.get("shutdown") is True:
            out["state"] = STATE_SHUTDOWN
            out["reasons"].append("lifecycle_shutdown")
            out["ok"] = False
            return out
    except Exception:
        pass

    # -------------------------------------------------------
    # Detect running jobs safely
    # -------------------------------------------------------
    running_daemons = []
    running_oneshots = []

    if isinstance(jobs, dict):
        jobs_iter = jobs.values()
    else:
        jobs_iter = jobs or []

    for j in jobs_iter:
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

    # -------------------------------------------------------
    # Health snapshot
    # -------------------------------------------------------
    prices = health.get("prices") or {}
    labels = health.get("labels") or {}
    model = health.get("model") or {}

    prices_ok = bool(prices.get("ok"))
    labels_ok = bool(labels.get("ok"))
    model_ok = bool(model.get("ok"))

    prices_age_s = _safe_float(prices.get("age_s"), 1e9)

    # -------------------------------------------------------
    # Kill switch detection
    # -------------------------------------------------------
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

        elif isinstance(kill_switches.get("state"), list):
            for r in kill_switches.get("state") or []:
                try:
                    if isinstance(r, dict) and int(r.get("enabled") or 0) == 1:
                        ks_enabled = True
                        break
                except Exception:
                    continue

    except Exception:
        pass

    if ks_enabled:
        out["state"] = STATE_KILL_SWITCH
        out["reasons"].append("kill_switch_enabled")
        out["ok"] = False
        return out

    # -------------------------------------------------------
    # BOOTING (no jobs visible)
    # -------------------------------------------------------
    if not jobs_iter:
        out["state"] = STATE_BOOTING
        out["reasons"].append("no_jobs_visible")
        out["ok"] = False
        return out

    # -------------------------------------------------------
    # WARMING_UP (core deps not ready)
    # -------------------------------------------------------
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

    # -------------------------------------------------------
    # Freshness threshold
    # -------------------------------------------------------
    try:
        max_age_s = float(os.environ.get("HEALTH_PRICES_MAX_AGE_S", "120"))
    except Exception:
        max_age_s = 120.0

    # -------------------------------------------------------
    # LIVE
    # -------------------------------------------------------
    has_price_daemon = (
        "poll_prices" in running_daemons
        or "stream_prices_polygon_ws" in running_daemons
    )

    if has_price_daemon and prices_age_s <= max_age_s:
        out["state"] = STATE_LIVE
        out["ok"] = True
        return out

    # -------------------------------------------------------
    # DEGRADED (everything else)
    # -------------------------------------------------------
    out["state"] = STATE_DEGRADED
    out["ok"] = False

    if not has_price_daemon:
        out["reasons"].append("no_price_daemon_running")

    if prices_age_s > max_age_s:
        out["reasons"].append(f"prices_stale_age_s={prices_age_s:.1f}")

    return out
