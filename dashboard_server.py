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
import subprocess
import sys
import threading
import time

from engine.runtime.logging import get_logger
log = get_logger("dashboard")

from urllib.parse import parse_qs
# Load .env if present (safe no-op if missing)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Allow importing engine from project root
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from engine.api.http_transport import build_handler, run_http_server

# Ensure static UI paths resolve even when launched from another working directory
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
try:
    os.chdir(_BASE_DIR)
except Exception:
    pass

# API-layer only access (no direct dev_core access from dashboard)
from engine.api.internal_access import (
    learn_relevance_stats,
    get_execution_mode as _exec_mode_get,
)

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

from engine.runtime.gates import is_execution_job

from engine.runtime.job_registry import ALLOWED_JOBS, PIPELINE_ORDER, JOB_ORDER
from engine.runtime.supervisor import RuntimeSupervisor
from engine.runtime.jobs_manager import JobManager
from engine.runtime.orchestrator import RuntimeOrchestrator
from engine.runtime.health import run_preflight

from engine.dev_core.kill_switch import snapshot as kill_switch_snapshot
from engine.dev_core.execution_mode import get_execution_mode as get_execution_mode_snapshot
from engine.runtime.health import (
    get_health_snapshot,
    run_preflight,
    preflight_cached,
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
    snapshot as lifecycle_snapshot,
    start_lifecycle_monitor,
    mark_shutdown,
)

from engine.api.api_read import (
    get_alerts,
    get_execution_metrics,
    get_model_registry,
    get_confidence_mass,
    get_temporal_eval,
    get_embed_model_eval,
    get_embed_conf_calib,
)

from engine.api.api_read_advanced import (
    get_execution_metrics_rolling,
)

from engine.api.api_dashboard_reads import (
    api_get_model_diagnostics as _api_get_model_diagnostics,
    api_get_temporal_models as _api_get_temporal_models,
    api_get_latest_portfolio_backtest as _api_get_latest_portfolio_backtest,
    api_get_execution_metrics_by_symbol as _api_get_execution_metrics_by_symbol,
    api_get_execution_cost_by_confidence as _api_get_execution_cost_by_confidence,
    api_get_social_features as _api_get_social_features,
    api_get_social_regimes as _api_get_social_regimes,
    api_get_social_blocks as _api_get_social_blocks,
    api_get_validation as _api_get_validation,
    api_get_shadow_capital_scores as _api_get_shadow_capital_scores,
    api_post_shadow_capital_run as _api_post_shadow_capital_run,
    api_get_size_policy as _api_get_size_policy,

)

from engine.api.api_write import (
    ack_alert,
    resolve_alert,
    write_job_event,
    set_promotion_enabled,
)

from engine.api.api_governance import (
    api_post_rollback,
    get_promotion_status,
    get_promotion_explain,
    api_get_exec_conf_calib,
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
)

AUTO_RECALIBRATE = os.environ.get("AUTO_RECALIBRATE", "1") == "1"
AUTO_RECALIBRATE_INTERVAL_S = 86400  # daily
# -------------            -- ------------------------------------------------------
# Phase 5.2: AUTO SIZE POLICY (NEW)
# -------------            -- ------------------------------------------------------
AUTO_SIZE_POLICY = os.environ.get("AUTO_SIZE_POLICY", "0") == "1"
AUTO_SIZE_POLICY_INTERVAL_S = float(os.environ.get("AUTO_SIZE_POLICY_INTERVAL_S", "86400"))  # daily
AUTO_SIZE_POLICY_START_DELAY_S = float(os.environ.get("AUTO_SIZE_POLICY_START_DELAY_S", "20.0"))
AUTO_SIZE_POLICY_LOG = os.environ.get("AUTO_SIZE_POLICY_LOG", "1") == "1"

# -------------            -- ------------------------------------------------------
# A.1 AUTO PIPELINE SCHEDULER (NEW)
# -------------            -- ------------------------------------------------------
AUTO_PIPELINE = os.environ.get("AUTO_PIPELINE", "0") == "1"
AUTO_PIPELINE_INTERVAL_S = float(os.environ.get("AUTO_PIPELINE_INTERVAL_S", "300"))  # 5 min
AUTO_PIPELINE_START_DELAY_S = float(os.environ.get("AUTO_PIPELINE_START_DELAY_S", "2.0"))
AUTO_PIPELINE_LOG = os.environ.get("AUTO_PIPELINE_LOG", "1") == "1"
# -------------            -- ------------------------------------------------------
# PHASE 3: AUTO CHALLENGER LOOP (NEW)
# -------------            -- ------------------------------------------------------
AUTO_CHALLENGER = os.environ.get("AUTO_CHALLENGER", "0") == "1"
AUTO_CHALLENGER_INTERVAL_S = float(os.environ.get("AUTO_CHALLENGER_INTERVAL_S", "3600"))  # 1h
AUTO_CHALLENGER_START_DELAY_S = float(os.environ.get("AUTO_CHALLENGER_START_DELAY_S", "10.0"))
AUTO_CHALLENGER_LOG = os.environ.get("AUTO_CHALLENGER_LOG", "1") == "1"

# Optional drift gate: only run challenger when max drift_ratio exceeds threshold
AUTO_CHALLENGER_MIN_DRIFT = float(os.environ.get("AUTO_CHALLENGER_MIN_DRIFT", "0.0"))  # 0 disables gate

# Optional: include broker execution after portfolio_rebalance
AUTO_PIPELINE_INCLUDE_EXECUTION = (
    os.environ.get("AUTO_PIPELINE_INCLUDE_EXECUTION", "0") == "1"
)

# Health thresholds (explanations use these)
HEALTH_PRICES_MAX_AGE_S = float(os.environ.get("HEALTH_PRICES_MAX_AGE_S", "120"))
HEALTH_EVENTS_MAX_AGE_S = float(os.environ.get("HEALTH_EVENTS_MAX_AGE_S", "600"))
HEALTH_PREDICTIONS_MAX_AGE_S = float(os.environ.get("HEALTH_PREDICTIONS_MAX_AGE_S", "600"))
HEALTH_JOBS_MAX_STALE_S = float(os.environ.get("HEALTH_JOBS_MAX_STALE_S", "180"))

HEALTH_MIN_LABELS = int(os.environ.get("HEALTH_MIN_LABELS", "10"))
HEALTH_MIN_MODEL_SUPPORT = int(os.environ.get("HEALTH_MIN_MODEL_SUPPORT", "10"))

# ------            -- ------------------------------------------------------
# TRAINING AUTO-RESUME POLICY (SAFE, EXPLICIT)
# ------            -- ------------------------------------------------------
TRAINING_RESUME_MIN_OK_STREAK = int(
    os.environ.get("TRAINING_RESUME_MIN_OK_STREAK", "5")
)

# -------------            -- ------------------------------------------------------
# PREFLIGHT (safe startup checklist)
# -------------            -- ------------------------------------------------------
PREFLIGHT_ENABLE = os.environ.get("PREFLIGHT_ENABLE", "1") == "1"
PREFLIGHT_BLOCK_JOBS = os.environ.get("PREFLIGHT_BLOCK_JOBS", "1") == "1"
PREFLIGHT_PRICES_MAX_AGE_S = float(os.environ.get("PREFLIGHT_PRICES_MAX_AGE_S", "300"))

PREFLIGHT_REQUIRED_TABLES = [
    "prices",
    "events",
    "labels",
    "alerts",
    "job_history",
    "portfolio_state",
    "portfolio_orders",
    "portfolio_bt_runs",
    "portfolio_bt_points",
    "broker_account",
    "broker_positions",
    "broker_fills_v2",
    "broker_meta",
    # cross-process job coordination
    "job_locks",
]

# ----------------------------------------
# STRUCTURAL SCHEMA AUDIT (tables + columns)
# ----------------------------------------
# This audits *structure* only (existence + required columns).
# Optional tables are included but do not fail ok unless required=True.
SCHEMA_EXPECTATIONS = {
    # core ingest
    "prices": {"required": True, "cols": ["ts_ms", "symbol", "price"]},
    "events": {"required": True, "cols": ["id", "ts_ms"]},
    "labels": {"required": True, "cols": ["event_id", "label", "ts_ms"]},
    "predictions": {"required": False, "cols": ["event_id", "ts_ms", "predicted_z"]},

    # ops / UI
    "alerts": {"required": True, "cols": ["id", "ts_ms", "severity", "symbol", "horizon_s"]},
    "job_history": {"required": True, "cols": ["id", "ts_ms", "job_name", "event"]},
    "job_locks": {"required": True, "cols": ["job_name", "owner", "pid", "acquired_ts_ms", "heartbeat_ts_ms"]},
    "risk_state": {"required": False, "cols": ["key", "value", "updated_ts_ms"]},

    # model + promotion
    "model_stats_regime": {"required": False, "cols": ["symbol", "horizon_s", "regime", "n", "mean_impact_z"]},
    "model_stats": {"required": False, "cols": ["symbol", "horizon_s", "n", "mean_impact_z"]},
    "spillover_beta": {"required": False, "cols": ["target_symbol", "driver_symbol", "horizon_s", "n", "beta"]},
    "model_registry": {"required": False, "cols": ["model_name", "stage", "model_kind", "model_ts_ms", "created_ts_ms"]},
    "model_promotion_audit": {"required": False, "cols": ["ts_ms", "model_name", "key", "decision"]},
    "validation_points": {"required": False, "cols": ["ts_ms", "model_name", "rmse", "n"]},

    # portfolio
    "portfolio_state": {"required": True, "cols": ["ts_ms"]},
    "portfolio_orders": {"required": True, "cols": ["ts_ms"]},
    "portfolio_bt_runs": {"required": True, "cols": ["id", "ts_ms", "start_ts_ms", "end_ts_ms"]},
    "portfolio_bt_points": {"required": True, "cols": ["run_id", "ts_ms", "equity", "drawdown"]},

    # broker/execution
    "broker_account": {"required": True, "cols": ["ts_ms"]},
    "broker_positions": {"required": True, "cols": ["ts_ms", "symbol"]},
    "broker_meta": {"required": True, "cols": ["key", "value"]},
    "broker_fills_v2": {"required": False, "cols": ["ts_ms", "symbol"]},
    "broker_fills": {"required": False, "cols": ["ts_ms", "symbol"]},

    # dashboard-only tables created here
    "alert_acks": {"required": False, "cols": ["alert_id", "acked_ts_ms"]},
    "alert_resolutions": {"required": False, "cols": ["alert_id", "resolved_ts_ms"]},
    "equity_drift": {"required": False, "cols": ["ts_ms", "diff_equity", "diff_equity_pct", "level"]},

    # size policy
    "size_policy": {"required": False, "cols": ["id", "ts_ms", "lookback_days", "buckets", "method"]},
    "size_policy_points": {"required": False, "cols": ["policy_id", "bucket_idx", "conf_lo", "conf_hi", "factor"]},
}

def api_get_schema_audit(_parsed):
    return get_schema_audit()

_PREFLIGHT_CACHE = {"ok": True, "notes": [], "tables_ok": True, "health_ok": True, "ts_ms": 0}

from typing import Tuple

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

# -------------            -- ------------------------------------------------------
# RELEVANCE STATS CACHE + TIMEOUT (NEW)
# -------------            -- ------------------------------------------------------

_relevance_cache = {
    "ts": 0.0,
    "value": None,
}

def _compute_relevance_stats_with_timeout(timeout_s: float):
    """
    Runs learn_relevance_stats() with a hard timeout.
    Prevents UI hangs if learner stalls.
    """
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

# -------------            -- ------------------------------------------------------
# RELEVANCE STATS (NEW)
# -------------            -- ------------------------------------------------------

def get_relevance_stats():
    """
    Learned relevance stats used by predictor + alerts.
    Safe, read-only diagnostic endpoint.
    Includes:
      - env gate
      - TTL cache
      - hard timeout
    """
    if not ENABLE_RELEVANCE_STATS:
        return {
            "ok": False,
            "error": "relevance stats disabled (ENABLE_RELEVANCE_STATS=0)",
        }

    now = time.time()

    # serve from cache if fresh
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
        return {
            "ok": True,
            "cached": False,
            "stats": stats,
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
        }

# -------------            -- ------------------------------------------------------
# DIAGNOSTICS / METRICS

# -------------            -- ------------------------------------------------------

def api_get_health(_parsed):
    return get_health_snapshot()

def api_get_system_state(_parsed):
    from engine.runtime.system_state import compute_system_state

    try:
        health = get_health_snapshot()
    except Exception:
        health = {}

    try:
        jobs = JOBS.list_jobs()
    except Exception:
        jobs = []

    try:
        kill_switches = api_get_kill_switches(None)
    except Exception:
        kill_switches = {}

    state = compute_system_state(
        health=health,
        jobs=jobs,
        kill_switches=kill_switches,
    )

    return state

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

MODEL_NAME = "embed_regressor"


def _format_slack_eq_crit(p: dict) -> bytes:
    bt = p.get("bt") or {}
    diff = p.get("diff_equity")
    pct = p.get("diff_equity_pct")
    alert_id = p.get("alert_id", 0)

    text = "*🚨 CRITICAL: Broker vs Backtest Equity Mismatch*"

    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Δ Equity:*\n{diff:.4f}" if diff is not None else "*Δ Equity:*\n?"},
                {"type": "mrkdwn", "text": f"*Δ %:*\n{pct*100:.2f}%" if pct is not None else "*Δ %:*\n?"},
                {"type": "mrkdwn", "text": f"*BT Equity:*\n{bt.get('equity')}"},
                {"type": "mrkdwn", "text": f"*Run ID:*\n{bt.get('run_id')}"},
            ],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Acknowledge"},
                    "value": f"ACK_ALERT:{alert_id}",
                }
            ],
        },
    ]

    return json.dumps(
        {"text": text, "blocks": blocks},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

def _format_discord_eq_crit(p: dict) -> bytes:
    bt = p.get("bt") or {}
    diff = p.get("diff_equity")
    pct = p.get("diff_equity_pct")

    embed = {
        "title": "🚨 CRITICAL: Equity Reconciliation Failed",
        "color": 15158332,  # red
        "fields": [
            {"name": "Δ Equity", "value": f"{diff:.4f}" if diff is not None else "?", "inline": True},
            {"name": "Δ %", "value": f"{pct*100:.2f}%" if pct is not None else "?", "inline": True},
            {"name": "BT Equity", "value": str(bt.get("equity")), "inline": True},
            {"name": "Run ID", "value": str(bt.get("run_id")), "inline": True},
            {"name": "Reason", "value": str(p.get("reason", "n/a")), "inline": False},
        ],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    return json.dumps(
        {"embeds": [embed]},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

def _send_eq_crit_email(subject: str, body: str):
    if not EQ_CRIT_EMAIL_TO or not EQ_CRIT_SMTP_HOST:
        return

    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = EQ_CRIT_EMAIL_FROM
    msg["To"] = EQ_CRIT_EMAIL_TO
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(EQ_CRIT_SMTP_HOST, EQ_CRIT_SMTP_PORT, timeout=5) as s:
        s.send_message(msg)

def _send_eq_crit_webhook(payload: dict):
    if not EQ_CRIT_WEBHOOK_URL:
        return

    url = str(EQ_CRIT_WEBHOOK_URL).lower()

    # Slack detection
    is_slack = "hooks.slack.com" in url

    # Discord detection
    is_discord = "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url

    try:
        import urllib.request

        if is_slack:
            data = _format_slack_eq_crit(payload)
        elif is_discord:
            data = _format_discord_eq_crit(payload)
        else:
            data = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")

        req = urllib.request.Request(
            EQ_CRIT_WEBHOOK_URL,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=EQ_CRIT_WEBHOOK_TIMEOUT_S):
            pass
    except Exception:
        pass  # never raise

# -------------            -- ------------------------------------------------------
# SAFE VOICE / LLM EXPLAINER (THREAD + TIMEOUT)
# -------------            -- ------------------------------------------------------

VOICE_ENABLED = os.environ.get("VOICE_ENABLED", "1") == "1"
VOICE_TIMEOUT_S = float(os.environ.get("VOICE_TIMEOUT_S", "6.0"))
VOICE_MAX_PROMPT_CHARS = int(os.environ.get("VOICE_MAX_PROMPT_CHARS", "8000"))
VOICE_MAX_RESPONSE_CHARS = int(os.environ.get("VOICE_MAX_RESPONSE_CHARS", "700"))

def _run_llm_explain_with_timeout(prompt: str, timeout_s: float) -> str:
    """
    Runs llmExplain(prompt) in a worker thread with a hard timeout.
    Prevents blocking the HTTP server.
    """
    result = {}
    error = {}

    def _worker():
        try:
            # IMPORTANT: this assumes llmExplain already exists in your environment
            # (as referenced in your original JS block)
            from llm import llmExplain  # adjust import if needed
            result["text"] = llmExplain(prompt)
        except Exception as e:
            error["error"] = str(e)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout_s)

    if t.is_alive():
        raise TimeoutError(f"LLM timeout after {timeout_s}s")

    if "error" in error:
        raise RuntimeError(error["error"])

    return str(result.get("text") or "")

# -------------            -- ------------------------------------------------------
# HTTP HANDLER
# -------------            -- ------------------------------------------------------

def run_size_policy_job():
    acquired = False
    try:
        acquired = bool(acquire_lock("train_size_policy", ttl_ms=30 * 60 * 1000))
        if not acquired:
            return {"ok": False, "error": "train_size_policy locked (already running?)"}
        return JOBS.start("train_size_policy")
    finally:
        if acquired:
            try:
                release_lock("train_size_policy")
            except Exception:
                pass

def _qs(parsed):
    try:
        q = parse_qs(parsed.query or "")
        return {k: v[0] for k, v in q.items()}
    except Exception:
        return {}

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

ROUTE_SPECS = list(ROUTE_SPECS_SYSTEM) + list(ROUTE_SPECS_JOBS) + list(ROUTE_SPECS_OPS)

# ----------------------------------------------------------------------
# FALLBACK ROUTES (keeps dashboard usable if split route modules missing)
# ----------------------------------------------------------------------
if not ROUTE_SPECS:
    ROUTE_SPECS = [
        # UI convenience
        ("GET",  "/api/health", "api_get_health"),
        ("GET",  "/api/system/state", "api_get_system_state"),
        ("GET",  "/api/jobs", "api_get_jobs"),
        ("POST", "/api/jobs/start", "api_post_job_start"),
        ("POST", "/api/jobs/stop", "api_post_job_stop"),
        ("GET",  "/api/jobs/log", "api_get_job_log"),
        ("GET",  "/api/jobs/history", "api_get_job_history"),

        ("GET",  "/api/alerts", "api_get_alerts"),
        ("GET",  "/api/validation", "api_get_validation"),

        ("POST", "/api/pipeline/run", "api_post_pipeline_run"),

        ("GET",  "/api/model/diagnostics", "api_get_model_diagnostics"),
        ("GET",  "/api/model/registry", "api_get_model_registry"),

        ("GET",  "/api/embed_model_eval", "api_get_embed_model_eval"),
        ("GET",  "/api/embed_conf_calib", "api_get_embed_conf_calib"),

        ("GET",  "/api/confidence_mass", "api_get_confidence_mass"),
        ("GET",  "/api/schema/audit", "api_get_schema_audit"),
        ("GET",  "/api/size_policy", "api_get_size_policy"),

        # shadow capital allocation scoring (governance)
        ("GET",  "/api/shadow/capital_scores", "api_get_shadow_capital_scores"),
        ("POST", "/api/shadow/capital_scores/run", "api_post_shadow_capital_run"),

        ("POST", "/api/model/rollback", "api_post_rollback"),

        # execution metrics (additive)
        ("GET",  "/api/execution/metrics", "api_get_execution_metrics"),
        ("GET",  "/api/execution/rolling", "api_get_execution_metrics_rolling"),
        ("GET",  "/api/execution/by_symbol", "api_get_execution_metrics_by_symbol"),
        ("GET",  "/api/execution/cost_by_confidence", "api_get_execution_cost_by_confidence"),

        # optional social endpoints
        ("GET",  "/api/social/features", "api_get_social_features"),
        ("GET",  "/api/social/regimes", "api_get_social_regimes"),
        ("GET",  "/api/social/blocks", "api_get_social_blocks"),
    ]

def _missing(name: str):
    return {"ok": False, "error": f"handler_unavailable:{name}"}

def _deny_if_shutdown():
    try:
        s = lifecycle_snapshot() or {}
        if str(s.get("state") or "").upper() == "SHUTDOWN":
            return {"ok": False, "error": "server_shutting_down"}
    except Exception:
        pass
    return None

def _wrap_get_temporal_models(parsed, _ctx):
    if not get_temporal_models:
        return _missing("get_temporal_models")
    qs = _qs(parsed)
    limit = int(qs.get("limit", "20") or "20")
    return get_temporal_models(limit=limit)

def _wrap_get_latest_portfolio_backtest(_parsed, _ctx):
    if not get_latest_portfolio_backtest:
        return _missing("get_latest_portfolio_backtest")
    return get_latest_portfolio_backtest()

def _wrap_get_execution_metrics_by_symbol(parsed, _ctx):
    if not get_execution_metrics_by_symbol:
        return _missing("get_execution_metrics_by_symbol")
    qs = _qs(parsed)
    limit = int(qs.get("limit", "50") or "50")
    return get_execution_metrics_by_symbol(limit=limit)

def _wrap_get_execution_cost_by_confidence(_parsed, _ctx):
    if not get_execution_cost_by_confidence:
        return _missing("get_execution_cost_by_confidence")
    return get_execution_cost_by_confidence()

def _wrap_api_get_confidence_mass(parsed, _ctx):
    if not api_get_confidence_mass:
        return _missing("api_get_confidence_mass")
    return api_get_confidence_mass(parsed)

def _wrap_api_post_rollback(parsed, body, _ctx):
    if not api_post_rollback:
        return _missing("api_post_rollback")
    return api_post_rollback(parsed, body)

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

def api_get_model_registry(parsed):
    qs = _qs(parsed)
    limit = qs.get("limit", "50")
    return get_model_registry(limit=int(limit or 50))


def api_get_embed_model_eval(parsed):
    qs = _qs(parsed)
    limit = qs.get("limit", "500")
    return get_embed_model_eval(limit=int(limit or 500))


def api_get_embed_conf_calib(parsed):
    qs = _qs(parsed)
    horizon_s = int(qs.get("horizon_s", "0") or "0")
    model_kind = str(qs.get("model_kind", "") or "")
    limit = int(qs.get("limit", "200") or "200")
    return get_embed_conf_calib(horizon_s=horizon_s, model_kind=model_kind, limit=limit)


def api_get_temporal_eval(parsed):
    qs = _qs(parsed)
    limit = int(qs.get("limit", "50") or "50")
    return get_temporal_eval(limit=limit)




# ------------------------------
# JOBS + PIPELINE (MISSING IN FILE)
# ------------------------------

def _job_name_from(parsed, body) -> str:
    qs = _qs(parsed)
    name = (qs.get("name") or "").strip()
    if not name and isinstance(body, dict):
        name = str(body.get("name") or "").strip()
    return name

def api_get_jobs(_parsed):
    """
    Returns deterministic list for UI:
      - running jobs from JobManager
      - plus non-running allowed jobs (status=stopped)
      - ordered by JOB_ORDER then remaining alphabetical
    """
    try:
        running = JOBS.list_jobs() or []
    except Exception:
        running = []

    running_by_name = {}
    for j in running:
        try:
            n = str(j.get("name") or "").strip()
            if n:
                running_by_name[n] = j
        except Exception:
            continue

    allowed_names = []
    try:
        allowed_names = list(ALLOWED_JOBS.keys())
    except Exception:
        allowed_names = []

    # ordering: JOB_ORDER first, then remaining allowed sorted
    order = []
    try:
        order = list(JOB_ORDER or [])
    except Exception:
        order = []

    remaining = sorted([n for n in allowed_names if n not in set(order)])
    names = [n for n in order if n in set(allowed_names)] + remaining

    out = []
    for name in names:
        if name in running_by_name:
            out.append(running_by_name[name])
        else:
            out.append({
                "name": name,
                "pid": None,
                "started_ts_ms": None,
                "state": "stopped",
                "exit_code": None,
                "meta": {},
            })

    return {
        "ok": True,
        "ts_ms": int(time.time() * 1000),
        "jobs": out,
        "pipeline_order": list(PIPELINE_ORDER or []),
        "allowed": names,
    }

def api_post_job_start(parsed, body):
    name = _job_name_from(parsed, body)
    if not name:
        return {"ok": False, "error": "missing_name"}

    if name not in ALLOWED_JOBS:
        return {"ok": False, "error": f"job_not_allowed:{name}"}

    try:
        res = JOBS.start(name)
    except Exception as e:
        res = {"ok": False, "error": str(e)}

    try:
        write_job_event(job_name=name, event="start", detail=res)

    except Exception:
        pass

    return res

def api_post_job_stop(parsed, body):
    d = _deny_if_shutdown()
    if d:
        return d
    name = _job_name_from(parsed, body)
    if not name:
        return {"ok": False, "error": "missing_name"}

    if name not in ALLOWED_JOBS:
        return {"ok": False, "error": f"job_not_allowed:{name}"}

    try:
        res = JOBS.stop(name)
    except Exception as e:
        res = {"ok": False, "error": str(e)}

    try:
        write_job_event(job_name=name, event="stop", detail=res)

    except Exception:
        pass

    return res

def api_post_pipeline_run(parsed, body):
    """
    Runs pipeline in-order, with orchestrator-level locking.
    Optional:
      - ?include_execution=1 (or body {"include_execution": true})
    """
    qs = _qs(parsed)
    inc = qs.get("include_execution", "")
    if not inc and isinstance(body, dict):
        inc = body.get("include_execution", "")

    include_execution = str(inc).strip() in ("1", "true", "True", "yes", "YES")

    try:
        res = ORCHESTRATOR.run_pipeline(include_execution=include_execution)
    except Exception as e:
        res = {"ok": False, "error": str(e)}

    try:
        write_job_event(job_name="pipeline", event="run", detail=res)

    except Exception:
        pass

    return res

API_HANDLERS = {
    # GET
    "api_get_kill_switches": api_get_kill_switches,
    "api_get_health": api_get_health,
    "api_get_system_state": api_get_system_state,
    "api_get_jobs": api_get_jobs,
    "api_get_job_log": api_get_job_log,
    "api_get_job_history": api_get_job_history,

    "api_get_alerts": lambda parsed, ctx=None: get_alerts(),
    "api_get_validation": _api_get_validation,

    "api_get_model_diagnostics": _api_get_model_diagnostics,
    "api_get_model_registry": lambda parsed, ctx=None: get_model_registry(limit=int((_qs(parsed).get("limit", "50") or "50"))),

    "api_get_embed_model_eval": lambda parsed, ctx=None: get_embed_model_eval(limit=int((_qs(parsed).get("limit", "500") or "500"))),
    "api_get_embed_conf_calib": lambda parsed, ctx=None: get_embed_conf_calib(
        horizon_s=int((_qs(parsed).get("horizon_s", "0") or "0")),
        model_kind=str((_qs(parsed).get("model_kind", "") or "")),
        limit=int((_qs(parsed).get("limit", "200") or "200")),
    ),

    "api_get_confidence_mass": lambda parsed, ctx=None: get_confidence_mass(),
    "api_get_temporal_eval": lambda parsed, ctx=None: get_temporal_eval(limit=int((_qs(parsed).get("limit", "50") or "50"))),

    "api_get_temporal_models": _api_get_temporal_models,
    "api_get_latest_portfolio_backtest": _api_get_latest_portfolio_backtest,

    "api_get_execution_metrics": lambda parsed, ctx=None: get_execution_metrics(),
    "api_get_execution_metrics_rolling": lambda parsed, ctx=None: get_execution_metrics_rolling(),
    "api_get_execution_metrics_by_symbol": _api_get_execution_metrics_by_symbol,
    "api_get_execution_cost_by_confidence": _api_get_execution_cost_by_confidence,

    "api_get_social_features": _api_get_social_features,
    "api_get_social_regimes": _api_get_social_regimes,
    "api_get_social_blocks": _api_get_social_blocks,

    "api_get_schema_audit": api_get_schema_audit,
    "api_get_size_policy": _api_get_size_policy,

    "api_get_shadow_capital_scores": _api_get_shadow_capital_scores,

    # POST
    "api_post_job_start": api_post_job_start,
    "api_post_job_stop": api_post_job_stop,
    "api_post_pipeline_run": api_post_pipeline_run,
    "api_post_rollback": api_post_rollback,

    "api_post_shadow_capital_run": _api_post_shadow_capital_run,
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
    # Deterministic Supervisor Boot (optional)
    # - HARD-GATE execution jobs at boot unless LIVE+ARMED
    # ---------------------------------------------------
    if AUTO_BOOT_DAEMONS and AUTO_BOOT_TARGETS:
        try:
            targets = list(AUTO_BOOT_TARGETS)

            # Execution gating enforced centrally in JobManager.start()
            blocked = []

            if blocked:
                log.warning("auto-boot blocked execution targets: %s", blocked)

            log.info("supervisor deterministic_start targets: %s", targets)
            boot_res = SUPERVISOR.deterministic_start(
                targets,
                include_deps=True,
                strict=False,
            )
            log.info("supervisor boot result: %s", boot_res)
        except Exception as e:
            log.exception("supervisor boot exception")

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
        },
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
            JOBS.stop_all()
        except Exception:
            pass
        try:
            if _HTTPD:
                _HTTPD.server_close()
        except Exception:
            pass

if __name__ == "__main__":
    try:
        run_server()
    except Exception:
        log.exception("dashboard_server crashed")
        raise
