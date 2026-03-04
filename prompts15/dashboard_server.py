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

from engine.api.api_jobs_handlers import (
    api_get_jobs,
    api_post_job_start,
    api_post_job_stop,
    api_post_pipeline_run,
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
AUTO_BOOT_DAEMONS = os.environ.get("AUTO_BOOT_DAEMONS", "0") == "1"
AUTO_BOOT_TARGETS = [
    x.strip() for x in os.environ.get("AUTO_BOOT_TARGETS", "").split(",")
    if x.strip()
]

_HTTPD = None  # set in run_server()

# ---------------------------------------------------
# UI CONSOLE LIFECYCLE ENDPOINTS
# ---------------------------------------------------

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

try:
    from engine.strategy.api_capital_allocation import ROUTE_SPECS_CAPITAL_ALLOCATION
except Exception:
    ROUTE_SPECS_CAPITAL_ALLOCATION = []

ROUTE_SPECS = (
    list(ROUTE_SPECS_SYSTEM)
    + list(ROUTE_SPECS_JOBS)
    + list(ROUTE_SPECS_OPS)
    + list(ROUTE_SPECS_CAPITAL_ALLOCATION)
)

def api_get_kill_switches(parsed):
    if not _api_get_kill_switches_impl:
        return {"ok": False, "error": "kill_switches_unavailable"}
    # Some callers pass None (lifecycle monitor). Provide a minimal parsed shim.
    if parsed is None:
        class _P:  # tiny shim
            query = ""
        parsed = _P()
    return _api_get_kill_switches_impl(parsed, {})


def api_get_job_log(parsed):
    return _api_get_job_log_impl(parsed, {}) if _api_get_job_log_impl else {"ok": False, "error": "job_log_unavailable"}


def api_get_job_history(parsed):
    return _api_get_job_history_impl(parsed, {}) if _api_get_job_history_impl else {"ok": False, "error": "job_history_unavailable"}


# ------------------------------
# JOBS + PIPELINE (MISSING IN FILE)
# ------------------------------
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
    api_get_self_critic_warnings,
    api_post_self_critic_run,
    api_post_rollback,
)

from engine.api.api_system_handlers import (
    api_get_health,
    api_get_system_state,
    api_get_readiness,
    api_get_telemetry,
)

try:
    from engine.strategy.api_capital_allocation import (
        api_get_capital_allocation_status,
        api_post_capital_allocation_run,
        api_get_capital_allocation_metrics,
    )
except Exception:
    api_get_capital_allocation_status = None
    api_post_capital_allocation_run = None
    api_get_capital_allocation_metrics = None

API_HANDLERS = {
    # SYSTEM
    "api_get_kill_switches": api_get_kill_switches,
    "api_get_health": api_get_health,
    "api_get_system_state": api_get_system_state,
    "api_get_readiness": api_get_readiness,
    "api_get_telemetry": api_get_telemetry,

    # UI console lifecycle
    "api_get_server_status": api_get_server_status,
    "api_post_server_shutdown": api_post_server_shutdown,

    # JOBS
    "api_get_jobs": api_get_jobs,
    "api_post_job_start": api_post_job_start,
    "api_post_job_stop": api_post_job_stop,
    "api_post_pipeline_run": api_post_pipeline_run,

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
    "api_get_self_critic_warnings": api_get_self_critic_warnings,
    "api_post_self_critic_run": api_post_self_critic_run,
    "api_post_rollback": api_post_rollback,

    # CAPITAL ALLOCATION
    "api_get_capital_allocation_status": api_get_capital_allocation_status,
    "api_post_capital_allocation_run": api_post_capital_allocation_run,
    "api_get_capital_allocation_metrics": api_get_capital_allocation_metrics,
}

# -------------            -- ------------------------------------------------------
# SERVER
# -------------            -- ------------------------------------------------------

def run_server():
    global _HTTPD

    # ---------------------------------------------------
    # RUNTIME BOOTSTRAP (DB + coordination tables)
    # ---------------------------------------------------
    from engine.runtime.locks import cleanup_stale_locks

    # cleanup any stale locks left over from crashes
    try:
        cleanup_stale_locks()
    except Exception:
        log.warning("cleanup_stale_locks failed", exc_info=True)

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

            if PREFLIGHT_BLOCK_JOBS:
                log.error("PREFLIGHT_BLOCK_JOBS set, aborting further startup")
                raise RuntimeError("preflight blocked jobs")
        else:
            log.info("preflight OK")

    except Exception as e:
        log.exception("preflight exception")
        if PREFLIGHT_BLOCK_JOBS:
            raise

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
    # Production default: bring up UI first, then start jobs intentionally.
    # ---------------------------------------------------
    def _default_daemon_targets() -> list:
        out = []
        for name, spec in (ALLOWED_JOBS or {}).items():
            try:
                mode = spec[1]
            except Exception:
                mode = None
            if str(mode).strip().lower() == "daemon":
                out.append(name)
        return out

    if not AUTO_BOOT_DAEMONS:
        log.info("AUTO_BOOT_DAEMONS=0 -> skipping job auto-boot (UI only)")
    else:
        targets = list(AUTO_BOOT_TARGETS) if AUTO_BOOT_TARGETS else _default_daemon_targets()

        log.info("supervisor deterministic_start targets: %s", targets)

        boot_res = SUPERVISOR.deterministic_start(
            targets,
            include_deps=True,
            strict=True,
        )

        log.info("supervisor boot result: %s", boot_res)

        if not boot_res.get("ok"):
            raise RuntimeError(f"auto_boot_failed: {boot_res}")

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
