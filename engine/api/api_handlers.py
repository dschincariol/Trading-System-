import json
import time

from engine.dev_core.storage import connect as _db_connect
from engine.dev_core.learning import learn_relevance_stats

from health_checks import get_health_snapshot
from alerts_service import get_alerts
from execution_metrics import (
    get_execution_metrics,
    get_execution_metrics_rolling,
)

from pipeline_runner import (
    run_pipeline,
    LAST_AUTO_PIPELINE_TS,
    LAST_AUTO_CHALLENGER_TS,
    LAST_AUTO_SIZE_POLICY_TS,
)

from engine.runtime.jobs_manager import get_job_log, get_job_history

from dashboard_config import (
    ENABLE_RELEVANCE_STATS,
    RELEVANCE_STATS_CACHE_TTL_S,
    RELEVANCE_STATS_TIMEOUT_S,
    AUTO_PIPELINE,
    AUTO_CHALLENGER,
    AUTO_SIZE_POLICY,
)

# -------------------------------------------------
# Relevance stats (cached + timeout guarded)
# -------------------------------------------------

_relevance_cache = {"ts": 0.0, "value": None}


def _compute_relevance_stats_with_timeout(timeout_s: float):
    import threading

    result = {}
    error = {}

    def _runner():
        try:
            result["value"] = learn_relevance_stats()
        except Exception as e:
            error["error"] = str(e)

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout_s)

    if t.is_alive():
        raise TimeoutError(f"learn_relevance_stats timed out after {timeout_s}s")

    if "error" in error:
        raise RuntimeError(error["error"])

    return result.get("value")


def get_relevance_stats():
    if not ENABLE_RELEVANCE_STATS:
        return {"ok": False, "error": "relevance stats disabled (ENABLE_RELEVANCE_STATS=0)"}

    now = time.time()

    if (
        _relevance_cache["value"] is not None
        and (now - _relevance_cache["ts"]) < RELEVANCE_STATS_CACHE_TTL_S
    ):
        return {
            "ok": True,
            "cached": True,
            "stats": _relevance_cache["value"],
        }

    try:
        stats = _compute_relevance_stats_with_timeout(
            RELEVANCE_STATS_TIMEOUT_S
        )
        _relevance_cache["value"] = stats
        _relevance_cache["ts"] = now
        return {"ok": True, "cached": False, "stats": stats}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# -------------------------------------------------
# System / health
# -------------------------------------------------

def api_get_health(_parsed, _ctx):
    return get_health_snapshot()


def api_get_kill_switches(_parsed, _ctx):
    return {
        "ok": True,
        "kill_switches": {
            "auto_pipeline": {
                "enabled": bool(AUTO_PIPELINE),
                "reason": None if AUTO_PIPELINE else "AUTO_PIPELINE=0",
                "last_run": LAST_AUTO_PIPELINE_TS,
            },
            "auto_challenger": {
                "enabled": bool(AUTO_CHALLENGER),
                "reason": None if AUTO_CHALLENGER else "AUTO_CHALLENGER=0",
                "last_run": LAST_AUTO_CHALLENGER_TS,
            },
            "auto_size_policy": {
                "enabled": bool(AUTO_SIZE_POLICY),
                "reason": None if AUTO_SIZE_POLICY else "AUTO_SIZE_POLICY=0",
                "last_run": LAST_AUTO_SIZE_POLICY_TS,
            },
        },
    }


# -------------------------------------------------
# Jobs
# -------------------------------------------------

def api_get_jobs(_parsed, _ctx):
    JOBS = _ctx["JOBS"]
    return {"ok": True, "jobs": JOBS.list_jobs()}


def api_post_job_start(_parsed, body, _ctx):
    JOBS = _ctx["JOBS"]
    name = body.get("name")
    return JOBS.start(name)


def api_post_job_stop(_parsed, body, _ctx):
    JOBS = _ctx["JOBS"]
    name = body.get("name")
    return JOBS.stop(name)


def api_post_pipeline_run(_parsed, _body, _ctx):
    JOBS = _ctx["JOBS"]
    return run_pipeline(JOBS)


def api_get_job_log(parsed, _ctx):
    q = parsed.query
    params = dict(
        p.split("=", 1) for p in q.split("&") if "=" in p
    ) if q else {}
    name = params.get("name")
    tail = int(params.get("tail", "200"))
    return {"ok": True, "log": get_job_log(name, tail)}


def api_get_job_history(parsed, _ctx):
    q = parsed.query
    params = dict(
        p.split("=", 1) for p in q.split("&") if "=" in p
    ) if q else {}
    name = params.get("name")
    limit = int(params.get("limit", "200"))
    return {"ok": True, "rows": get_job_history(name, limit)}


# -------------------------------------------------
# Alerts
# -------------------------------------------------

def api_get_alerts(_parsed, _ctx):
    return {"ok": True, "rows": get_alerts()}


# -------------------------------------------------
# Execution metrics
# -------------------------------------------------

def api_get_execution_metrics(_parsed, _ctx):
    return get_execution_metrics()


def api_get_execution_metrics_rolling(_parsed, _ctx):
    return get_execution_metrics_rolling()


# -------------------------------------------------
# Model diagnostics
# -------------------------------------------------

def api_get_model_diagnostics(_parsed, _ctx):
    con = _db_connect()
    try:
        out = {}

        try:
            rows = con.execute(
                """
                SELECT symbol, horizon_s, regime, n, mean_impact_z
                FROM model_stats_regime
                ORDER BY symbol, horizon_s, regime
                """
            ).fetchall()
        except Exception:
            rows = []

        priors = {}
        for sym, h, reg, n, mean_z in rows:
            priors.setdefault(f"{sym}:{h}", []).append({
                "regime": reg,
                "n": int(n),
                "mean_z": float(mean_z),
            })
        out["regime_priors"] = priors

        try:
            rows = con.execute(
                """
                SELECT symbol, horizon_s, n, mean_impact_z
                FROM model_stats
                ORDER BY symbol, horizon_s
                """
            ).fetchall()
        except Exception:
            rows = []

        out["global_priors"] = [
            {
                "symbol": r[0],
                "horizon_s": r[1],
                "n": int(r[2]),
                "mean_z": float(r[3]),
            }
            for r in rows
        ]

        try:
            rows = con.execute(
                """
                SELECT target_symbol, driver_symbol, horizon_s, n, beta
                FROM spillover_beta
                ORDER BY target_symbol, horizon_s, n DESC
                """
            ).fetchall()
        except Exception:
            rows = []

        spill = {}
        for tgt, drv, h, n, beta in rows:
            spill.setdefault(f"{tgt}:{h}", []).append({
                "driver": drv,
                "n": int(n),
                "beta": float(beta),
            })
        out["spillovers"] = spill

        return {"ok": True, "data": out}
    finally:
        con.close()
