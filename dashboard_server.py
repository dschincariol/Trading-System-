# dashboard_server.py
"""
Local UI Dashboard + Job Console

UI:
  http://localhost:8000/ui/dashboard.html

APIs:
  /api/jobs
  /api/embed_model_eval              
  /api/embed_conf_calib
  /api/jobs/start?name=<job>
  /api/jobs/stop?name=<job>
  /api/jobs/log?name=<job>&tail=<n>
  /api/jobs/history?name=<job>&limit=<n>      
  /api/alerts
  /api/validation
  /api/health
  /api/pipeline/run
  /api/model/diagnostics
  /api/confidence_mass
"""
import json
import os
import sys
import threading
import time

# ------------------------------------------------------------------
# Ensure imports work no matter what the working directory is.
# Put repo root (this file's directory) at the front of sys.path
# BEFORE any engine.* imports.
# ------------------------------------------------------------------
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

try:
    import psutil
except Exception:
    psutil = None

# Load .env if present (safe no-op if missing)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from engine.runtime.config_schema import load_runtime_config, ConfigError

from engine.runtime.logging import get_logger
log = get_logger("dashboard")

from engine.api.http_transport import build_handler, run_http_server
from engine.runtime.shutdown import runtime_shutdown

# Ensure static UI paths resolve even when launched from another working directory
try:
    os.chdir(_BASE_DIR)
except Exception:
    pass

# API-layer only access (no direct dev_core access from dashboard)
from engine.api.internal_access import (
    get_execution_mode as _exec_mode_get,
)
from engine.api.api_relevance import api_get_relevance_stats

from engine.runtime.runtime_bootstrap import bootstrap_runtime

try:
    from engine.api.api_handlers import (
        api_get_kill_switches as _api_get_kill_switches_impl,
        api_get_job_log as _api_get_job_log_impl,
        api_get_job_history as _api_get_job_history_impl,
    )

except Exception:
    _api_get_kill_switches_impl = None
    _api_get_job_log_impl = None
    _api_get_job_history_impl = None

from engine.runtime.job_registry import ALLOWED_JOBS
from engine.runtime.supervisor import RuntimeSupervisor
from engine.runtime.jobs_manager import JobManager
from engine.runtime.orchestrator import RuntimeOrchestrator

from engine.runtime.health import (
    get_health_snapshot,
    run_preflight,
    get_schema_audit,
)

from engine.runtime.locks import (
    acquire_lock,
    release_lock,
    write_job_history,
)

from engine.runtime.guards import (
    auto_rollback_loop,
)

from engine.runtime.lifecycle import (
    start_lifecycle_monitor,
    mark_shutdown,
)

from engine.runtime.lifecycle import (
    start_lifecycle_monitor,
    mark_shutdown,
)

# ------------------------------------------------------------------
# Jobs API handlers (branch-safe import)
# ------------------------------------------------------------------
# ------------------------------------------------------------------
# Jobs API handlers (current branch)
# ------------------------------------------------------------------
from engine.api.api_jobs import (
    api_get_jobs,
    api_post_job_start,
    api_post_job_stop,
)
# -------------            -- ------------------------------------------------------
# CONFIG (auto-restart guards)
# -------------            -- ------------------------------------------------------
from engine.runtime.config import (
    AUTO_RESTART_DAEMONS,
    DAEMON_RESTART_BASE_DELAY_MS,
    DAEMON_RESTART_MAX_DELAY_MS,
    DAEMON_RESTART_WINDOW_S,
    DAEMON_RESTART_MAX_IN_WINDOW,
    DAEMON_WATCHDOG_PERIOD_S,

    AUTO_RECALIBRATE,
    AUTO_RECALIBRATE_INTERVAL_S,

    AUTO_SIZE_POLICY,
    AUTO_SIZE_POLICY_INTERVAL_S,
    AUTO_SIZE_POLICY_START_DELAY_S,
    AUTO_SIZE_POLICY_LOG,

    AUTO_PIPELINE,
    AUTO_PIPELINE_INTERVAL_S,
    AUTO_PIPELINE_START_DELAY_S,
    AUTO_PIPELINE_LOG,

    AUTO_CHALLENGER,
    AUTO_CHALLENGER_INTERVAL_S,
    AUTO_CHALLENGER_START_DELAY_S,
    AUTO_CHALLENGER_LOG,
    AUTO_CHALLENGER_MIN_DRIFT,

    AUTO_PIPELINE_INCLUDE_EXECUTION,

    HEALTH_PRICES_MAX_AGE_S,
    HEALTH_EVENTS_MAX_AGE_S,
    HEALTH_PREDICTIONS_MAX_AGE_S,
    HEALTH_JOBS_MAX_STALE_S,
    HEALTH_MIN_LABELS,
    HEALTH_MIN_MODEL_SUPPORT,

    TRAINING_RESUME_MIN_OK_STREAK,

    PREFLIGHT_ENABLE,
    PREFLIGHT_BLOCK_JOBS,
    PREFLIGHT_PRICES_MAX_AGE_S,
)

# # ----------------------------------------
# # STRUCTURAL SCHEMA AUDIT (tables + columns)
# # ----------------------------------------
# # This audits *structure* only (existence + required columns).
# # Optional tables are included but do not fail ok unless required=True.
# SCHEMA_EXPECTATIONS = {
#     # core ingest
#     "prices": {"required": True, "cols": ["ts_ms", "symbol", "price"]},
#     "events": {"required": True, "cols": ["id", "ts_ms"]},
#     "labels": {"required": True, "cols": ["event_id", "label", "ts_ms"]},
#     "predictions": {"required": False, "cols": ["event_id", "ts_ms", "predicted_z"]},

#     # ops / UI
#     "alerts": {"required": True, "cols": ["id", "ts_ms", "severity", "symbol", "horizon_s"]},
#     "job_history": {"required": True, "cols": ["id", "ts_ms", "job_name", "event"]},
#     "job_locks": {"required": True, "cols": ["job_name", "owner", "pid", "acquired_ts_ms", "heartbeat_ts_ms"]},
#     "risk_state": {"required": False, "cols": ["key", "value", "updated_ts_ms"]},

#     # model + promotion
#     "model_stats_regime": {"required": False, "cols": ["symbol", "horizon_s", "regime", "n", "mean_impact_z"]},
#     "model_stats": {"required": False, "cols": ["symbol", "horizon_s", "n", "mean_impact_z"]},
#     "spillover_beta": {"required": False, "cols": ["target_symbol", "driver_symbol", "horizon_s", "n", "beta"]},
#     "model_registry": {"required": False, "cols": ["model_name", "stage", "model_kind", "model_ts_ms", "created_ts_ms"]},
#     "model_promotion_audit": {"required": False, "cols": ["ts_ms", "model_name", "key", "decision"]},
#     "validation_points": {"required": False, "cols": ["ts_ms", "model_name", "rmse", "n"]},

#     # portfolio
#     "portfolio_state": {"required": True, "cols": ["ts_ms"]},
#     "portfolio_orders": {"required": True, "cols": ["ts_ms"]},
#     "portfolio_bt_runs": {"required": True, "cols": ["id", "ts_ms", "start_ts_ms", "end_ts_ms"]},
#     "portfolio_bt_points": {"required": True, "cols": ["run_id", "ts_ms", "equity", "drawdown"]},

#     # broker/execution
#     "broker_account": {"required": True, "cols": ["ts_ms"]},
#     "broker_positions": {"required": True, "cols": ["ts_ms", "symbol"]},
#     "broker_meta": {"required": True, "cols": ["key", "value"]},
#     "broker_fills_v2": {"required": False, "cols": ["ts_ms", "symbol"]},
#     "broker_fills": {"required": False, "cols": ["ts_ms", "symbol"]},

#     # dashboard-only tables created here
#     "alert_acks": {"required": False, "cols": ["alert_id", "acked_ts_ms"]},
#     "alert_resolutions": {"required": False, "cols": ["alert_id", "resolved_ts_ms"]},
#     "equity_drift": {"required": False, "cols": ["ts_ms", "diff_equity", "diff_equity_pct", "level"]},

#     # size policy
#     "size_policy": {"required": False, "cols": ["id", "ts_ms", "lookback_days", "buckets", "method"]},
#     "size_policy_points": {"required": False, "cols": ["policy_id", "bucket_idx", "conf_lo", "conf_hi", "factor"]},
# }

def api_get_schema_audit(_parsed):
    return get_schema_audit()

def api_post_repair_schema(_parsed, _body=None, _ctx=None):
    if not _repair_schema_run:
        return {"ok": False, "error": "repair_schema_unavailable"}

    try:
        result = _repair_schema_run()

        if isinstance(result, dict):
            return result

        return {"ok": True}

    except Exception as e:
        return {"ok": False, "error": str(e)}
    
# -------------            -- ------------------------------------------------------
# CRIT notifications (email / webhook)
# -------------            -- ------------------------------------------------------
EQ_CRIT_EMAIL_TO = os.environ.get("EQ_CRIT_EMAIL_TO", "")   # comma-separated
EQ_CRIT_EMAIL_FROM = os.environ.get("EQ_CRIT_EMAIL_FROM", "alerts@localhost")
EQ_CRIT_SMTP_HOST = os.environ.get("EQ_CRIT_SMTP_HOST", "")
EQ_CRIT_SMTP_PORT = int(os.environ.get("EQ_CRIT_SMTP_PORT", "25"))

EQ_CRIT_WEBHOOK_URL = os.environ.get("EQ_CRIT_WEBHOOK_URL", "")
EQ_CRIT_WEBHOOK_TIMEOUT_S = float(os.environ.get("EQ_CRIT_WEBHOOK_TIMEOUT_S", "4.0"))

# -------------            -- ------------------------------------------------------
# Broker ↔ Backtest equity reconciliation thresholds (NEW)
# -------------            -- ------------------------------------------------------
EQ_DIFF_WARN_PCT = float(os.environ.get("EQ_DIFF_WARN_PCT", "0.01"))   # 1%
EQ_DIFF_CRIT_PCT = float(os.environ.get("EQ_DIFF_CRIT_PCT", "0.03"))   # 3%
EQ_DIFF_WARN_ABS = float(os.environ.get("EQ_DIFF_WARN_ABS", "50"))
EQ_DIFF_CRIT_ABS = float(os.environ.get("EQ_DIFF_CRIT_ABS", "250"))
EQ_DIFF_ALERT_COOLDOWN_S = int(os.environ.get("EQ_DIFF_ALERT_COOLDOWN_S", "300"))

# Auto-resolve hysteresis (must be LOWER than WARN/CRIT to avoid flapping)
EQ_DIFF_RESOLVE_PCT = float(os.environ.get("EQ_DIFF_RESOLVE_PCT", "0.006"))  # 0.6%
EQ_DIFF_RESOLVE_ABS = float(os.environ.get("EQ_DIFF_RESOLVE_ABS", "30"))
EQ_DIFF_RESOLVE_LOOKBACK_S = int(os.environ.get("EQ_DIFF_RESOLVE_LOOKBACK_S", "86400"))  # 24h

# Sustained equity drift detection
EQ_DRIFT_SUSTAINED_WINDOW = int(os.environ.get("EQ_DRIFT_SUSTAINED_WINDOW", "5"))
EQ_DRIFT_SUSTAINED_MIN_WARN = int(os.environ.get("EQ_DRIFT_SUSTAINED_MIN_WARN", "3"))
EQ_DRIFT_SUSTAINED_MIN_CRIT = int(os.environ.get("EQ_DRIFT_SUSTAINED_MIN_CRIT", "3"))

# Job history retention
JOB_HISTORY_MAX_ROWS = int(os.environ.get("JOB_HISTORY_MAX_ROWS", "5000"))

# -------------            -- ------------------------------------------------------
# RELEVANCE STATS CONFIG (NEW)
# -------------            -- ------------------------------------------------------

ENABLE_RELEVANCE_STATS = os.environ.get("ENABLE_RELEVANCE_STATS", "1") == "1"
RELEVANCE_STATS_CACHE_TTL_S = int(os.environ.get("RELEVANCE_STATS_CACHE_TTL_S", "60"))
RELEVANCE_STATS_TIMEOUT_S = float(os.environ.get("RELEVANCE_STATS_TIMEOUT_S", "5.0"))

# ---------------------------------------------------
# RUNTIME ORCHESTRATION
# ---------------------------------------------------
JOBS = JobManager(
    preflight_fn=run_preflight,
    get_kill_switches_fn=lambda: api_get_kill_switches(None),
    get_execution_mode_fn=lambda: (_exec_mode_get() or {}),
)
SUPERVISOR = RuntimeSupervisor(jobs=JOBS)

ORCHESTRATOR = RuntimeOrchestrator(
    jobs=JOBS,
    acquire_lock=acquire_lock,
    release_lock=release_lock,
    auto_pipeline_include_execution=AUTO_PIPELINE_INCLUDE_EXECUTION,
    auto_pipeline_log=AUTO_PIPELINE_LOG,
    auto_pipeline_interval_s=AUTO_PIPELINE_INTERVAL_S,
    auto_pipeline_start_delay_s=AUTO_PIPELINE_START_DELAY_S,
    auto_challenger_log=AUTO_CHALLENGER_LOG,
    auto_challenger_interval_s=AUTO_CHALLENGER_INTERVAL_S,
    auto_challenger_start_delay_s=AUTO_CHALLENGER_START_DELAY_S,
    auto_challenger_min_drift=AUTO_CHALLENGER_MIN_DRIFT,
    auto_size_policy_log=AUTO_SIZE_POLICY_LOG,
    auto_size_policy_interval_s=AUTO_SIZE_POLICY_INTERVAL_S,
    auto_size_policy_start_delay_s=AUTO_SIZE_POLICY_START_DELAY_S,
    get_kill_switches=lambda: api_get_kill_switches(None),
    get_execution_mode=lambda: (_exec_mode_get() or {}),
)

# -------------            -- ------------------------------------------------------
# SERVER LIFECYCLE (status + graceful shutdown)
# -------------            -- ------------------------------------------------------
SERVER_SHUTDOWN_TOKEN = os.environ.get("SERVER_SHUTDOWN_TOKEN", "").strip()

# Optional API token for any mutating endpoints (start/stop jobs, pipeline run, training mode, etc).
# - If empty: mutating endpoints are allowed ONLY from localhost.
# - If set: token is required for ALL mutating endpoints (local + remote).
DASHBOARD_API_TOKEN = os.environ.get("DASHBOARD_API_TOKEN", "").strip()

SERVER_STARTED_AT_MS = int(time.time() * 1000)
CRASH_LOG_PATH = os.environ.get("CRASH_LOG_PATH", os.path.join(_BASE_DIR, "logs", "crash_analytics.jsonl"))

def _write_crash_analytics(exit_code):
    try:
        os.makedirs(os.path.dirname(CRASH_LOG_PATH), exist_ok=True)
    except Exception:
        pass

    try:
        payload = {
            "ts_ms": int(time.time() * 1000),
            "exit_code": int(exit_code),
            "uptime_s": int((int(time.time() * 1000) - SERVER_STARTED_AT_MS) / 1000),
        }
        with open(CRASH_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")
    except Exception:
        pass


# HTTP bind
host = os.environ.get("DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1"
port = int(os.environ.get("DASHBOARD_PORT", "8000"))

# ---------------------------------------------------
# SUPERVISOR AUTO BOOT (deterministic, ENV-gated)
# ---------------------------------------------------
def _env_bool(key: str, default: bool = False) -> bool:
    v = os.environ.get(key)
    if v is None:
        return bool(default)
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")

# Default ON for production-safe deterministic boot
AUTO_BOOT_DAEMONS = _env_bool("AUTO_BOOT_DAEMONS", True)

AUTO_BOOT_TARGETS = [
    x.strip() for x in os.environ.get("AUTO_BOOT_TARGETS", "").split(",")
    if x.strip()
]

# If no explicit targets provided, default to price feed WS
if AUTO_BOOT_DAEMONS and not AUTO_BOOT_TARGETS:
    AUTO_BOOT_TARGETS = ["stream_prices_polygon_ws"]

_HTTPD = None  # set in run_server()

# ---------------------------------------------------
# UI CONSOLE LIFECYCLE ENDPOINTS
# ---------------------------------------------------
def api_get_training_status(_parsed, _ctx=None):
    """
    UI calls /api/training_status.
    Source of truth is engine.training_guard.get_training_status() when available.
    """
    try:
        from engine.training_guard import get_training_status as _get_training_status
        out = _get_training_status()
        if isinstance(out, dict):
            out.setdefault("ok", True)
            return out
        return {"ok": True, "mode": str(out), "allowed": False}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def api_get_server_status(_parsed, _ctx=None):
    now_ms = int(time.time() * 1000)
    uptime_s = int((now_ms - SERVER_STARTED_AT_MS) / 1000)
    return {
        "ok": True,
        "ts_ms": now_ms,
        "uptime_s": uptime_s,
        "host": host,
        "port": port,
    }

def api_post_server_shutdown(_parsed, _body=None, _ctx=None):
    # mark shutdown FIRST so mutating endpoints fail-closed (if your guards consult lifecycle)
    try:
        mark_shutdown()
    except Exception:
        pass

    # stop HTTP loop
    try:
        if _HTTPD:
            _HTTPD.shutdown()
    except Exception:
        pass

    return {"ok": True}

#------------            -- ------------------------------------------------------
# DIAGNOSTICS / METRICS

# -------------            -- ------------------------------------------------------
def _normalize_explain_json(val) -> str:
    """
    Ensure explain_json is always a JSON string.
    - If None/empty: returns "{}"
    - If already JSON text: returns as-is
    - If bytes: decodes utf-8
    - Otherwise: returns JSON-encoded wrapper
    """
    if val is None:
        return "{}"
    try:
        if isinstance(val, (bytes, bytearray)):
            val = val.decode("utf-8", errors="replace")
    except Exception:
        pass

    s = str(val).strip()
    if not s:
        return "{}"

    # if it parses, return original text (keeps exact content)
    try:
        json.loads(s)
        return s
    except Exception:
        return json.dumps({"raw": s})

# -------------            -- ------------------------------------------------------
# HTTP HANDLER
# -------------            -- ------------------------------------------------------


# ------------------------------
# ROUTE SPECS (split into files)
# ------------------------------
try:

    from engine.api.api_system import ROUTE_SPECS_SYSTEM
except Exception:
    ROUTE_SPECS_SYSTEM = []

try:
    from engine.api.api_jobs import ROUTE_SPECS_JOBS
except Exception:
    ROUTE_SPECS_JOBS = []

try:
    from engine.api.api_ops import ROUTE_SPECS_OPS
except Exception:
    ROUTE_SPECS_OPS = []

ROUTE_SPECS = (
    list(ROUTE_SPECS_SYSTEM)
    + list(ROUTE_SPECS_JOBS)
    + list(ROUTE_SPECS_OPS)
)

# -------------------------------------------------------------------
# FALLBACK ROUTES (UI hard-dep)
# If ROUTE_SPECS_* failed to import or are incomplete, the UI will 404.
# These map paths -> handler keys in API_HANDLERS below.
# -------------------------------------------------------------------
_FALLBACK_ROUTE_SPECS = [
    # SYSTEM
    {"method": "GET",  "path": "/api/health",           "handler": "api_get_health"},
    {"method": "GET",  "path": "/api/system/state",     "handler": "api_get_system_state"},
    {"method": "GET",  "path": "/api/system/readiness", "handler": "api_get_readiness"},
    {"method": "GET",  "path": "/api/telemetry",        "handler": "api_get_telemetry"},
    {"method": "GET",  "path": "/api/training_status",  "handler": "api_get_training_status"},
    {"method": "GET",  "path": "/api/pnl",              "handler": "api_get_pnl"},
    {"method": "POST", "path": "/api/repair_schema",    "handler": "api_post_repair_schema"},

    # JOBS
    {"method": "GET",  "path": "/api/jobs",         "handler": "api_get_jobs"},
    {"method": "POST", "path": "/api/jobs/start",   "handler": "api_post_job_start"},
    {"method": "POST", "path": "/api/jobs/stop",    "handler": "api_post_job_stop"},
    {"method": "GET",  "path": "/api/jobs/log",     "handler": "api_get_job_log"},
    {"method": "GET",  "path": "/api/jobs/history", "handler": "api_get_job_history"},
    {"method": "POST", "path": "/api/pipeline/run", "handler": "api_post_pipeline_run"},

    # OPS
    {"method": "GET", "path": "/api/alerts",                          "handler": "api_get_alerts"},
    {"method": "GET", "path": "/api/alerts/timeline",                 "handler": "api_get_alerts"},
    {"method": "GET", "path": "/api/validation",                      "handler": "api_get_validation"},
    {"method": "GET", "path": "/api/model/diagnostics",               "handler": "api_get_model_diagnostics"},
    {"method": "GET", "path": "/api/model_registry",                  "handler": "api_get_model_registry"},
    {"method": "GET", "path": "/api/embed_model_eval",                "handler": "api_get_embed_model_eval"},
    {"method": "GET", "path": "/api/embed_conf_calib",                "handler": "api_get_embed_conf_calib"},
    {"method": "GET", "path": "/api/temporal_eval",                   "handler": "api_get_temporal_eval"},
    {"method": "GET", "path": "/api/temporal_models",                 "handler": "api_get_temporal_models"},
    {"method": "GET", "path": "/api/backtest/portfolio/latest",       "handler": "api_get_latest_portfolio_backtest"},
    {"method": "GET", "path": "/api/execution_metrics",               "handler": "api_get_execution_metrics"},
    {"method": "GET", "path": "/api/execution_metrics/rolling",       "handler": "api_get_execution_metrics_rolling"},
    {"method": "GET", "path": "/api/execution_metrics/by_symbol",     "handler": "api_get_execution_metrics_by_symbol"},
    {"method": "GET", "path": "/api/execution_metrics/by_confidence", "handler": "api_get_execution_cost_by_confidence"},
    {"method": "GET", "path": "/api/confidence_mass",                 "handler": "api_get_confidence_mass"},
    {"method": "GET", "path": "/api/social/features",                 "handler": "api_get_social_features"},
    {"method": "GET", "path": "/api/social/regimes",                  "handler": "api_get_social_regimes"},
    {"method": "GET", "path": "/api/social/blocks",                   "handler": "api_get_social_blocks"},
    {"method": "GET", "path": "/api/relevance_stats",                 "handler": "api_get_relevance_stats"},
    {"method": "POST","path": "/api/champion/rollback",               "handler": "api_post_rollback"},

    # EXECUTION / PORTFOLIO / PROMOTION (UI hard-deps)
    {"method": "GET", "path": "/api/execution/barrier",               "handler": "api_get_execution_barrier"},
    {"method": "GET", "path": "/api/market_stress",                   "handler": "api_get_market_stress"},
    {"method": "GET", "path": "/api/market_stress_history",           "handler": "api_get_market_stress_history"},
    {"method": "GET", "path": "/api/portfolio",                       "handler": "api_get_portfolio"},
    {"method": "GET", "path": "/api/broker",                          "handler": "api_get_broker"},
    {"method": "GET", "path": "/api/strategy/status",                 "handler": "api_get_strategy_status"},
    {"method": "GET", "path": "/api/strategy_metrics",                "handler": "api_get_strategy_metrics"},
    {"method": "GET", "path": "/api/reconcile/broker_backtest",       "handler": "api_get_reconcile_broker_backtest"},
    {"method": "GET", "path": "/api/equity_drift",                    "handler": "api_get_equity_drift"},
    {"method": "GET", "path": "/api/temporal_shadow_eval",            "handler": "api_get_temporal_shadow_eval"},
    {"method": "GET", "path": "/api/promotion_audit",                 "handler": "api_get_promotion_audit"},
    {"method": "GET", "path": "/api/promotion/status",                "handler": "api_get_promotion_status"},

    # UI hard-deps present in ui/dashboard.js but missing from ROUTE_SPECS_* in this repo
    {"method": "GET",  "path": "/api/system/kill_switches",           "handler": "api_get_kill_switches"},  # alias
    {"method": "GET",  "path": "/api/alerts/by_id",                   "handler": "api_get_alert_by_id"},
    {"method": "GET",  "path": "/api/promotion/explain",              "handler": "api_get_promotion_explain"},
    {"method": "GET",  "path": "/api/size_policy",                    "handler": "api_get_size_policy"},
    {"method": "POST", "path": "/api/size_policy/train",              "handler": "api_post_size_policy_train"},
    {"method": "GET",  "path": "/api/model_metrics",                  "handler": "api_get_model_metrics"},
    {"method": "GET",  "path": "/api/execution_overlays",             "handler": "api_get_execution_overlays"},
    {"method": "GET",  "path": "/api/crash_analytics",                "handler": "api_get_crash_analytics"},
]
# De-dup (method,path) while preserving earlier specs
_seen = set()
_merged = []

for r in (ROUTE_SPECS + _FALLBACK_ROUTE_SPECS):

    # dict style
    if isinstance(r, dict):
        method = str(r.get("method", "")).upper()
        path = str(r.get("path", ""))

    # tuple style (method, path, handler)
    elif isinstance(r, tuple) and len(r) >= 2:
        method = str(r[0]).upper()
        path = str(r[1])

    else:
        continue

    key = (method, path)

    if key in _seen:
        continue

    _seen.add(key)
    _merged.append(r)

ROUTE_SPECS = _merged

# ---------------------------------------------------
# NORMALIZE ROUTE SPECS (tuple -> dict)
# ---------------------------------------------------
_normalized = []

for r in ROUTE_SPECS:

    # already dict
    if isinstance(r, dict):
        _normalized.append(r)
        continue

    # tuple style: (method, path, handler)
    if isinstance(r, tuple) and len(r) >= 3:
        _normalized.append({
            "method": str(r[0]).upper(),
            "path": str(r[1]),
            "handler": r[2],
        })
        continue

ROUTE_SPECS = _normalized

def api_get_kill_switches(parsed):
    if not _api_get_kill_switches_impl:
        return {"ok": False, "error": "kill_switches_unavailable"}
    # Some callers pass None (lifecycle monitor). Provide a minimal parsed shim.
    if parsed is None:
        class _P:  # tiny shim
            query = ""
        parsed = _P()
    return _api_get_kill_switches_impl(parsed, {})


def api_get_job_log(parsed, body=None, ctx=None):
    if not _api_get_job_log_impl:
        return {"ok": False, "error": "job_log_unavailable"}

    try:
        ctx = ctx or {}
        if "JOBS" not in ctx:
            ctx["JOBS"] = JOBS

        # Try 3-arg signature
        try:
            return _api_get_job_log_impl(parsed, body, ctx)
        except TypeError:
            # Try 2-arg signature
            try:
                return _api_get_job_log_impl(parsed, ctx)
            except TypeError:
                # Try 1-arg signature
                return _api_get_job_log_impl(parsed)

    except Exception as e:
        return {"ok": False, "error": "job_log_exception", "detail": str(e)}

def api_get_job_history(parsed, body=None, ctx=None):
    if not _api_get_job_history_impl:
        return {"ok": False, "error": "job_history_unavailable"}

    try:
        ctx = ctx or {}
        if "JOBS" not in ctx:
            ctx["JOBS"] = JOBS

        # Try 3-arg signature
        try:
            return _api_get_job_history_impl(parsed, body, ctx)
        except TypeError:
            # Try 2-arg signature
            try:
                return _api_get_job_history_impl(parsed, ctx)
            except TypeError:
                # Try 1-arg signature
                return _api_get_job_history_impl(parsed)

    except Exception as e:
        return {"ok": False, "error": "job_history_exception", "detail": str(e)}
    
# ------------------------------
# JOBS + PIPELINE (MISSING IN FILE)
# ------------------------------
# Provide safe pipeline stub if not present in this branch
def api_post_pipeline_run(_parsed, _body=None, _ctx=None):
    try:
        name = "pipeline_run"
        if name not in ALLOWED_JOBS:
            return {"ok": False, "error": "job_not_registered", "job": name}
        JOBS.start(name)
        return {"ok": True, "job": name, "started": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}
# ---------------------------------------------------
# OPS HANDLERS
# - api_ops_handlers.py in this repo does NOT define many "NEW UI hard-deps"
#   referenced by older dashboard_server variants.
# - Import the ones that exist; provide safe stubs for missing to avoid startup crash.
# ---------------------------------------------------

def _unavailable(name: str):
    def _fn(*_a, **_k):
        return {"ok": False, "error": "unavailable", "name": name}
    return _fn

try:
    from engine.api.api_ops_handlers import (
        api_get_alerts,
        api_get_validation,
        api_get_model_diagnostics,
        api_get_model_registry,
        api_get_embed_model_eval,
        api_get_embed_conf_calib,
        api_get_temporal_eval,
        api_get_temporal_models,
        api_get_latest_portfolio_backtest,
        api_get_execution_metrics,
        api_get_execution_metrics_rolling,
        api_get_execution_metrics_by_symbol,
        api_get_execution_cost_by_confidence,
        api_get_social_features,
        api_get_social_regimes,
        api_get_social_blocks,
        api_get_confidence_mass,
        api_post_rollback,
    )
except Exception:
    api_get_alerts = _unavailable("api_get_alerts")
    api_get_validation = _unavailable("api_get_validation")
    api_get_model_diagnostics = _unavailable("api_get_model_diagnostics")
    api_get_model_registry = _unavailable("api_get_model_registry")
    api_get_embed_model_eval = _unavailable("api_get_embed_model_eval")
    api_get_embed_conf_calib = _unavailable("api_get_embed_conf_calib")
    api_get_temporal_eval = _unavailable("api_get_temporal_eval")
    api_get_temporal_models = _unavailable("api_get_temporal_models")
    api_get_latest_portfolio_backtest = _unavailable("api_get_latest_portfolio_backtest")
    api_get_execution_metrics = _unavailable("api_get_execution_metrics")
    api_get_execution_metrics_rolling = _unavailable("api_get_execution_metrics_rolling")
    api_get_execution_metrics_by_symbol = _unavailable("api_get_execution_metrics_by_symbol")
    api_get_execution_cost_by_confidence = _unavailable("api_get_execution_cost_by_confidence")
    api_get_social_features = _unavailable("api_get_social_features")
    api_get_social_regimes = _unavailable("api_get_social_regimes")
    api_get_social_blocks = _unavailable("api_get_social_blocks")
    api_get_confidence_mass = _unavailable("api_get_confidence_mass")
    api_post_rollback = _unavailable("api_post_rollback")

# Optional (missing in this repo): keep names defined so API_HANDLERS can reference them safely.
api_get_market_stress = _unavailable("api_get_market_stress")
api_get_market_stress_history = _unavailable("api_get_market_stress_history")
api_get_portfolio = _unavailable("api_get_portfolio")
api_get_broker = _unavailable("api_get_broker")
api_get_strategy_status = _unavailable("api_get_strategy_status")
api_get_strategy_metrics = _unavailable("api_get_strategy_metrics")
api_get_reconcile_broker_backtest = _unavailable("api_get_reconcile_broker_backtest")
api_get_equity_drift = _unavailable("api_get_equity_drift")
api_get_temporal_shadow_eval = _unavailable("api_get_temporal_shadow_eval")
api_get_promotion_audit = _unavailable("api_get_promotion_audit")
api_get_promotion_status = _unavailable("api_get_promotion_status")

from engine.api.api_system_handlers import (
    api_get_health,
    api_get_system_state,
    api_get_readiness,
    api_get_telemetry,
)

from engine.api.api_system import (
    api_get_execution_barrier,
)

# ---- SCHEMA REPAIR (Operator controlled) ----
_repair_schema_run = None

try:
    # Primary location (preferred)
    from engine.runtime.repair_schema import run as _repair_schema_run
except Exception:
    try:
        # Fallback location (if placed under jobs/)
        from engine.runtime.jobs.repair_schema import run as _repair_schema_run
    except Exception:
        _repair_schema_run = None

def api_get_pnl(_parsed, _ctx=None):
    try:
        from engine.runtime.position_store import get_pnl_snapshot
        data = get_pnl_snapshot()
        return {"ok": True, "data": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------
# UI hard-deps missing from ROUTE_SPECS_* in this repo
# ---------------------------------------------------

def _qs(parsed, key: str, default: str = "") -> str:
    try:
        q = getattr(parsed, "query", "") or ""
    except Exception:
        q = ""
    if not q:
        return default
    try:
        from urllib.parse import parse_qs
        d = parse_qs(q, keep_blank_values=True)
        v = d.get(key)
        if not v:
            return default
        return str(v[0])
    except Exception:
        return default


def api_get_alert_by_id(parsed, _ctx=None):
    alert_id = _qs(parsed, "id", "")
    if not alert_id:
        return {"ok": False, "error": "missing_id"}

    try:
        from engine.runtime.db import get_conn
    except Exception:
        return {"ok": False, "error": "db_unavailable"}

    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM alerts WHERE id = ? LIMIT 1", (alert_id,))
        row = cur.fetchone()
        if not row:
            return {"ok": False, "error": "not_found", "id": alert_id}

        cols = [d[0] for d in cur.description] if cur.description else []
        out = dict(zip(cols, row)) if cols else {"row": row}
        return {"ok": True, "id": alert_id, "alert": out}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_get_promotion_explain(parsed, _ctx=None):
    try:
        from engine.api.api_governance import api_get_promotion_explain as _impl
    except Exception:
        return {"ok": False, "error": "promotion_explain_unavailable"}

    # keep signature flexibility
    try:
        return _impl(parsed, {})
    except TypeError:
        try:
            return _impl(parsed)
        except TypeError:
            return _impl()


def api_get_size_policy(parsed, _ctx=None):
    try:
        from engine.api.api_dashboard_reads import api_get_size_policy as _impl
    except Exception:
        return {"ok": False, "error": "size_policy_unavailable"}

    try:
        return _impl(parsed, {})
    except TypeError:
        try:
            return _impl(parsed)
        except TypeError:
            return _impl()


def api_post_size_policy_train(_parsed, _body=None, _ctx=None):
    # Kick off existing job if registered.
    try:
        name = "train_size_policy"
        if name not in ALLOWED_JOBS:
            return {"ok": False, "error": "job_not_registered", "job": name}
        # Start via JobManager directly (same process)
        JOBS.start(name)
        return {"ok": True, "job": name, "started": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_get_model_metrics(_parsed, _ctx=None):
    try:
        from engine.strategy.validation import get_model_metrics
        data = get_model_metrics()
        return {"ok": True, "data": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_get_execution_overlays(_parsed, _ctx=None):
    try:
        from engine.execution.execution_overlays import get_execution_overlays
        data = get_execution_overlays()
        return {"ok": True, "data": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_get_crash_analytics(parsed, _ctx=None):
    # Reads CRASH_LOG_PATH jsonl written by _write_crash_analytics
    limit_s = _qs(parsed, "limit", "100")
    try:
        limit = max(1, min(10000, int(limit_s)))
    except Exception:
        limit = 100

    try:
        if not os.path.exists(CRASH_LOG_PATH):
            return {"ok": True, "rows": [], "path": CRASH_LOG_PATH}
        rows = []
        with open(CRASH_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    rows.append({"raw": line})
        if len(rows) > limit:
            rows = rows[-limit:]
        return {"ok": True, "rows": rows, "path": CRASH_LOG_PATH}
    except Exception as e:
        return {"ok": False, "error": str(e), "path": CRASH_LOG_PATH}


API_HANDLERS = {
    # SYSTEM
    "api_get_kill_switches": api_get_kill_switches,
    "api_get_health": api_get_health,
    "api_get_system_state": api_get_system_state,
    "api_get_readiness": api_get_readiness,
    "api_get_telemetry": api_get_telemetry,
    "api_get_pnl": api_get_pnl,
    "api_post_repair_schema": api_post_repair_schema,
    "api_get_execution_barrier": api_get_execution_barrier,

    # UI console lifecycle
    "api_get_server_status": api_get_server_status,
    "api_get_training_status": api_get_training_status,
    "api_post_server_shutdown": api_post_server_shutdown,

    # JOBS
    "api_get_jobs": api_get_jobs,
    "api_post_job_start": api_post_job_start,
    "api_post_job_stop": api_post_job_stop,
    "api_post_pipeline_run": api_post_pipeline_run,
    "api_get_job_log": api_get_job_log,
    "api_get_job_history": api_get_job_history,

    # OPS
    "api_get_alerts": api_get_alerts,
    "api_get_validation": api_get_validation,
    "api_get_model_diagnostics": api_get_model_diagnostics,
    "api_get_model_registry": api_get_model_registry,
    "api_get_embed_model_eval": api_get_embed_model_eval,
    "api_get_embed_conf_calib": api_get_embed_conf_calib,
    "api_get_temporal_eval": api_get_temporal_eval,
    "api_get_temporal_models": api_get_temporal_models,
    "api_get_latest_portfolio_backtest": api_get_latest_portfolio_backtest,
    "api_get_execution_metrics": api_get_execution_metrics,
    "api_get_execution_metrics_rolling": api_get_execution_metrics_rolling,
    "api_get_execution_metrics_by_symbol": api_get_execution_metrics_by_symbol,
    "api_get_execution_cost_by_confidence": api_get_execution_cost_by_confidence,
    "api_get_social_features": api_get_social_features,
    "api_get_social_regimes": api_get_social_regimes,
    "api_get_social_blocks": api_get_social_blocks,
    "api_get_confidence_mass": api_get_confidence_mass,
    "api_get_relevance_stats": api_get_relevance_stats,
    "api_post_rollback": api_post_rollback,

    # UI hard-deps (aliases + additional endpoints)
    "api_get_alert_by_id": api_get_alert_by_id,
    "api_get_promotion_explain": api_get_promotion_explain,
    "api_get_size_policy": api_get_size_policy,
    "api_post_size_policy_train": api_post_size_policy_train,
    "api_get_model_metrics": api_get_model_metrics,
    "api_get_execution_overlays": api_get_execution_overlays,
    "api_get_crash_analytics": api_get_crash_analytics,

    # MISSING OPS / EXECUTION / PORTFOLIO (kept for compatibility; safe stubs if absent)
    "api_get_market_stress": api_get_market_stress,
    "api_get_market_stress_history": api_get_market_stress_history,
    "api_get_portfolio": api_get_portfolio,
    "api_get_broker": api_get_broker,
    "api_get_strategy_status": api_get_strategy_status,
    "api_get_strategy_metrics": api_get_strategy_metrics,
    "api_get_reconcile_broker_backtest": api_get_reconcile_broker_backtest,
    "api_get_equity_drift": api_get_equity_drift,
    "api_get_temporal_shadow_eval": api_get_temporal_shadow_eval,
    "api_get_promotion_audit": api_get_promotion_audit,
    "api_get_promotion_status": api_get_promotion_status,
}

# -------------            -- ------------------------------------------------------
# SERVER
# -------------            -- ------------------------------------------------------
def run_server():
    global _HTTPD

    # ---------------------------------------------------
    # RUNTIME BOOTSTRAP (DB + coordination tables)
    # ---------------------------------------------------
    boot = bootstrap_runtime(log=log)
    if not boot.get("ok"):
        raise RuntimeError(f"bootstrap_runtime failed: {boot}")

    # ---------------------------------------------------
    # Start lifecycle monitor (global state machine)
    # ---------------------------------------------------
    try:
        start_lifecycle_monitor(
            get_health=lambda: get_health_snapshot(),
            get_jobs=lambda: JOBS.list_jobs(),
            get_kill_switches=lambda: api_get_kill_switches(None),
            interval_s=2.0,
        )
    except Exception:
        pass

    # Optional: auto-rollback watcher (ONLY ONE LOOP)
    try:
        t = threading.Thread(
            target=auto_rollback_loop,
            args=(api_post_rollback, write_job_history),
            daemon=True,
        )

        t.start()
    except Exception:
        pass

    # ------            -- ------------------------------------------------------
    # PREFLIGHT SNAPSHOT AT BOOT (safe startup checklist)
    # ------            -- ------------------------------------------------------
    try:
        p = run_preflight()
        if not p.get("ok"):
            log.error("preflight FAILED at startup")

            for note in p.get("notes", []):
                log.error("preflight note: %s", note)

        else:
            log.info("preflight OK")

    except Exception as e:
        log.exception("preflight exception")

    log.info("dashboard running at http://%s:%s/ui/dashboard.html", host, port)

    # ---------------------------------------------------
    # Dependency DAG validation (FAIL FAST) - master prompt requirement
    # ---------------------------------------------------
    v = SUPERVISOR.validate_graph(strict=True)
    if not v.get("ok"):
        raise RuntimeError(f"invalid_dependency_graph: {list(v.get('errors') or [])}")

    if not ALLOWED_JOBS:
        raise RuntimeError("no_allowed_jobs_registered")

    # ---------------------------------------------------
    # Deterministic Supervisor Boot (ENV-gated)
    # ---------------------------------------------------

    def _default_daemon_targets():
        # Deterministic price-first boot
        preferred = ["stream_prices_polygon_ws", "stream_prices_ibkr", "poll_prices"]

        ordered = []
        for p in preferred:
            if p in ALLOWED_JOBS:
                ordered.append(p)

        # Include any other daemons after price feeds
        for name, meta in ALLOWED_JOBS.items():
            if meta.get("kind") == "daemon" and name not in ordered:
                ordered.append(name)

        return ordered

    if not AUTO_BOOT_DAEMONS:
        log.info("AUTO_BOOT_DAEMONS=0 -> skipping job auto-boot (UI only)")
    else:
        targets = list(AUTO_BOOT_TARGETS) if AUTO_BOOT_TARGETS else _default_daemon_targets()

        log.info("SUPERVISOR deterministic_start targets: %s", targets)

        result = SUPERVISOR.deterministic_start(
            targets,
            include_deps=True,
            strict=True,
        )

        log.info("SUPERVISOR boot result: %s", result)

        if not result.get("ok"):
            raise RuntimeError(f"auto_boot_failed: {result}")

        if AUTO_PIPELINE:
            log.info("auto_pipeline enabled interval_s=%s", AUTO_PIPELINE_INTERVAL_S)
            threading.Thread(target=ORCHESTRATOR.auto_pipeline_loop, daemon=True).start()

        if AUTO_CHALLENGER:
            log.info(
                "auto_challenger enabled interval_s=%s drift_gate=%s",
                AUTO_CHALLENGER_INTERVAL_S,
                AUTO_CHALLENGER_MIN_DRIFT,
            )
            threading.Thread(target=ORCHESTRATOR.auto_challenger_loop, daemon=True).start()

        if AUTO_SIZE_POLICY:
            log.info("auto_size_policy enabled interval_s=%s", AUTO_SIZE_POLICY_INTERVAL_S)
            threading.Thread(target=ORCHESTRATOR.auto_size_policy_loop, daemon=True).start()

    HandlerCls = build_handler(
        ROUTE_SPECS=ROUTE_SPECS,
        API_HANDLERS=API_HANDLERS,
        dashboard_api_token=DASHBOARD_API_TOKEN,
        ctx={
            "JOBS": JOBS,
            "SUPERVISOR": SUPERVISOR,
            "ORCHESTRATOR": ORCHESTRATOR,
            "ALLOWED_JOBS": ALLOWED_JOBS,
            "API_HANDLERS": API_HANDLERS,
        },
        static_dir=_BASE_DIR,
    )

    _HTTPD = run_http_server(host, port, HandlerCls)

    try:
        import signal

        def _shutdown(_sig=None, _frame=None):
            # mark shutdown FIRST so all mutating endpoints can fail-closed
            try:
                mark_shutdown()
            except Exception:
                pass
            try:
                if _HTTPD:
                    _HTTPD.shutdown()
            except Exception:
                pass

        try:
            signal.signal(signal.SIGINT, _shutdown)
        except Exception:
            pass
        try:
            signal.signal(signal.SIGTERM, _shutdown)
        except Exception:
            pass
    except Exception:
        pass
    try:
        _HTTPD.serve_forever()
    finally:
        try:
            mark_shutdown()
        except Exception:
            pass
    try:
        runtime_shutdown(JOBS=JOBS, SUPERVISOR=SUPERVISOR)
    except Exception as e:
        log.error("runtime_shutdown error: %s", e)

        try:
            if _HTTPD:
                _HTTPD.server_close()
        except Exception:
            pass

def stop_server():
    global _HTTPD
    try:
        mark_shutdown()
    except Exception:
        pass
    try:
        if _HTTPD:
            _HTTPD.shutdown()
    except Exception:
        pass

if __name__ == "__main__":
    try:
        run_server()
    except Exception:
        _write_crash_analytics(exit_code=1)
        log.exception("dashboard_server crashed")
        raise
