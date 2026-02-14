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

# SINGLE SOURCE OF TRUTH FOR SQLITE
from engine.dev_core.storage import connect as _db_connect
from engine.dev_core.trade_attribution_ledger import upsert_from_latest_pnl_attribution_snapshot
from engine.dev_core.storage import init_db as _init_db

from engine.dev_core.learning import learn_relevance_stats
from engine.dev_core.training_guard import (
    training_allowed,
    get_training_status,
    set_training_mode,
)

from engine.dev_core.kill_switch import (
    snapshot as _kill_switch_snapshot,
    set_kill_switch as _kill_switch_set,
    clear as _kill_switch_clear,
)
from engine.dev_core.promotion_hardening import manual_rollback as _manual_rollback

from engine.dev_core.execution_mode import (
    get_execution_mode as _exec_mode_get,
    set_execution_mode as _exec_mode_set,
    get_execution_overlays as _exec_overlays_get,
)

from engine.dev_core.market_stress import get_market_stress_snapshot as _market_stress_snapshot

try:
    from api_handlers import api_get_kill_switches as _api_get_kill_switches_impl
    from api_handlers import api_get_job_log as _api_get_job_log_impl
    from api_handlers import api_get_job_history as _api_get_job_history_impl
except Exception:
    _api_get_kill_switches_impl = None
    _api_get_job_log_impl = None
    _api_get_job_history_impl = None

from engine.runtime.job_registry import ALLOWED_JOBS, PIPELINE_ORDER, JOB_ORDER
from engine.runtime.supervisor import RuntimeSupervisor
from engine.runtime.jobs_manager import JobManager
from engine.runtime.orchestrator import RuntimeOrchestrator
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
    read_job_history,
    _ensure_job_locks,
    _ensure_job_history,
)

from engine.runtime.guards import (
    auto_rollback_loop,
    detect_sustained_equity_drift,
    classify_equity_diff,
)

from engine.runtime.lifecycle import (
    snapshot as lifecycle_snapshot,
    start_lifecycle_monitor,
    mark_shutdown,
)


# -------------            -- ------------------------------------------------------
# CONFIG (auto-restart guards)
# -------------            -- ------------------------------------------------------
AUTO_RESTART_DAEMONS = os.environ.get("AUTO_RESTART_DAEMONS", "1") == "1"
DAEMON_RESTART_BASE_DELAY_MS = int(os.environ.get("DAEMON_RESTART_BASE_DELAY_MS", "2000"))
DAEMON_RESTART_MAX_DELAY_MS = int(os.environ.get("DAEMON_RESTART_MAX_DELAY_MS", "30000"))
DAEMON_RESTART_WINDOW_S = int(os.environ.get("DAEMON_RESTART_WINDOW_S", "120"))
DAEMON_RESTART_MAX_IN_WINDOW = int(os.environ.get("DAEMON_RESTART_MAX_IN_WINDOW", "5"))
DAEMON_WATCHDOG_PERIOD_S = float(os.environ.get("DAEMON_WATCHDOG_PERIOD_S", "1.0"))
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


def api_get_shadow_capital_scores(parsed):
    qs = _qs(parsed)
    limit = int(qs.get("limit", "50") or "50")
    regime = str(qs.get("regime", "global") or "global").strip() or "global"
    try:
        from engine.dev_core.shadow_capital_allocator import get_shadow_capital_scores
        return get_shadow_capital_scores(limit=limit, regime=regime)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_post_shadow_capital_run(parsed, body):
    qs = _qs(parsed)
    window_s = qs.get("window_s", "")
    regime = qs.get("regime", "")

    if isinstance(body, dict):
        if not window_s:
            window_s = body.get("window_s", "")
        if not regime:
            regime = body.get("regime", "")

    try:
        window_s = int(window_s or 86400)
    except Exception:
        window_s = 86400

    regime = str(regime or "global").strip() or "global"

    try:
        from engine.dev_core.shadow_capital_allocator import compute_and_persist_shadow_capital_scores
        return compute_and_persist_shadow_capital_scores(window_s=window_s, regime=regime)
    except Exception as e:
        return {"ok": False, "error": str(e)}

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

def _ensure_equity_drift():
    con = _db_connect()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS equity_drift (
              ts_ms INTEGER PRIMARY KEY,
              diff_equity REAL NOT NULL,
              diff_equity_pct REAL NOT NULL,
              level TEXT NOT NULL
            )
        """)
        con.commit()
    finally:
        con.close()

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

# -------------            -- ------------------------------------------------------
# ALERT ACKS + RESOLVED (Slack Resolve)
# -------------            -- ------------------------------------------------------

def _ensure_alert_acks():
    con = _db_connect()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS alert_acks (
                alert_id INTEGER PRIMARY KEY,
                acked_ts_ms INTEGER NOT NULL,
                acked_by TEXT,
                source TEXT
            )
        """)
        con.commit()
    finally:
        con.close()

def _ensure_alert_resolutions():
    con = _db_connect()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS alert_resolutions (
                alert_id INTEGER PRIMARY KEY,
                resolved_ts_ms INTEGER NOT NULL,
                resolved_by TEXT,
                reason TEXT,
                source TEXT
            )
        """)
        con.commit()
    finally:
        con.close()

def _ack_alert(alert_id: int, who: str, source: str):
    con = _db_connect()
    try:
        con.execute("""
            INSERT OR REPLACE INTO alert_acks
            (alert_id, acked_ts_ms, acked_by, source)
            VALUES (?,?,?,?)
        """, (
            int(alert_id),
            int(time.time() * 1000),
            str(who or ""),
            str(source or ""),
        ))
        con.commit()
    finally:
        con.close()

def _resolve_alert(alert_id: int, who: str, reason: str, source: str):
    con = _db_connect()
    try:
        con.execute("""
            INSERT OR IGNORE INTO alert_resolutions
            (alert_id, resolved_ts_ms, resolved_by, reason, source)
            VALUES (?,?,?,?,?)
        """, (
            int(alert_id),
            int(time.time() * 1000),
            str(who or ""),
            str(reason or ""),
            str(source or ""),
        ))
        con.commit()
    finally:
        con.close()

def _is_alert_acked(alert_id: int) -> bool:
    con = _db_connect()
    try:
        row = con.execute(
            "SELECT 1 FROM alert_acks WHERE alert_id = ?",
            (int(alert_id),),
        ).fetchone()
        return bool(row)
    finally:
        con.close()

def _is_alert_resolved(alert_id: int) -> bool:
    con = _db_connect()
    try:
        row = con.execute(
            "SELECT 1 FROM alert_resolutions WHERE alert_id = ?",
            (int(alert_id),),
        ).fetchone()
        return bool(row)
    finally:
        con.close()

def _read_kill_switch_audit(limit: int = 200):
    limit = max(1, min(5000, int(limit)))
    con = _db_connect()
    try:
        rows = con.execute(
            """
            SELECT ts_ms, action, scope, key, enabled, actor, reason, meta_json
            FROM kill_switch_audit
            ORDER BY ts_ms DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    except Exception:
        rows = []
    finally:
        con.close()

    out = []
    for r in rows or []:
        try:
            out.append(
                {
                    "ts_ms": int(r[0] or 0),
                    "action": str(r[1] or ""),
                    "scope": str(r[2] or ""),
                    "key": str(r[3] or ""),
                    "enabled": int(r[4] or 0),
                    "actor": str(r[5] or ""),
                    "reason": str(r[6] or ""),
                    "meta": _normalize_explain_json(r[7]),
                }
            )
        except Exception:
            continue
    return out

# ---------------------------------------------------
# RUNTIME ORCHESTRATION
# ---------------------------------------------------
JOBS = JobManager()
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

# -------------            -- ------------------------------------------------------
# GLOBAL SYSTEM LIFECYCLE (NEW)
# States:
#   BOOTING  -> process start
#   WARMING  -> db init + preflight + boot loops
#   LIVE     -> serving OK
#   DEGRADED -> serving but health/preflight indicates problems
#   KILL     -> kill-switch engaged (trading disabled)
#   SHUTDOWN -> shutting down / no mutations
# -------------            -- ------------------------------------------------------

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
# EXECUTION-AWARE CONFIDENCE CALIBRATION (NEW)
# -------------            -- ------------------------------------------------------

def get_exec_conf_calib():
    """
    Read-only endpoint: latest exec_conf_calib curve.
    Curve is produced by recalibrate_confidence.py (RUN_EXEC_CONF_CALIB=1).
    """
    try:
        from engine.dev_core.exec_conf_calibration import get_latest_exec_conf_calib
        return get_latest_exec_conf_calib()
    except Exception as e:
        return {"ok": False, "error": str(e)}


# -------------            -- ------------------------------------------------------
# DIAGNOSTICS / METRICS

# -------------            -- ------------------------------------------------------

def rollback_champion():
    try:
        from engine.dev_core.model_registry import rollback_champion as _rb
        from engine.dev_core.promotion_audit import audit as _audit
        ch_before = None
        try:
            from engine.dev_core.model_registry import get_stage_latest as _get
            ch_before = _get("embed_regressor", "champion")
        except Exception:
            ch_before = None

        ch_after = _rb("embed_regressor")
        if not ch_after:
            return {"ok": False, "error": "no retired model available to rollback to"}

        _audit(
            actor="manual",
            action="rollback",
            model_name="embed_regressor",
            from_kind=(ch_before.get("model_kind") if ch_before else None),
            from_ts_ms=(ch_before.get("model_ts_ms") if ch_before else None),
            to_kind=ch_after.get("model_kind"),
            to_ts_ms=ch_after.get("model_ts_ms"),
            reason={"note": "dashboard rollback"},
        )
        return {"ok": True, "champion": ch_after}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def api_post_rollback(_parsed, _body):
    d = _deny_if_shutdown()
    if d:
        return d
    return rollback_champion()

def get_promotion_status():
    try:
        from engine.dev_core.promotion_guard import promotion_allowed
        allowed = bool(promotion_allowed())
    except Exception:
        allowed = False

    try:
        con = _db_connect()
        try:
            row = con.execute(
                """
                SELECT value, updated_ts_ms
                FROM risk_state
                WHERE key='promotion_enabled'
                """,
            ).fetchone()
        finally:
            con.close()
        enabled = (str(row[0]) == "1") if row else True
        ts_ms = int(row[1]) if row else 0
    except Exception:
        enabled = True
        ts_ms = 0

    return {
        "enabled": bool(enabled),
        "allowed": bool(allowed),
        "updated_ts_ms": int(ts_ms),
    }


def get_promotion_explain():
    """
    Dashboard-friendly explainer:
    - current promotion gate
    - latest audit rows
    - current champion/challenger registry rows
    """
    out = {
        "ok": True,
        "ts_ms": int(time.time() * 1000),
        "promotion_status": get_promotion_status(),
        "registry": {},
        "audit": [],
    }

    try:
        from engine.dev_core.model_registry import list_recent
        rec = list_recent("embed_regressor", limit=50) or []
        out["registry"]["embed_regressor"] = rec
    except Exception:
        out["registry"]["embed_regressor"] = []

    try:
        con = _db_connect()
        try:
            rows = con.execute(
                """
                SELECT ts_ms, model_name, key, decision, reason, detail_json
                FROM model_promotion_audit
                ORDER BY ts_ms DESC
                LIMIT 50
                """
            ).fetchall()
        finally:
            con.close()

        for r in rows or []:
            out["audit"].append({
                "ts_ms": int(r[0] or 0),
                "model_name": str(r[1] or ""),
                "key": str(r[2] or ""),
                "decision": str(r[3] or ""),
                "reason": str(r[4] or ""),
                "detail_json": r[5],
            })
    except Exception:
        out["audit"] = []

    return out

def set_promotion_enabled(on_value: str):
    try:
        from engine.dev_core.promotion_guard import set_guard
        from engine.dev_core.promotion_audit import audit as _audit
        v = "1" if str(on_value) == "1" else "0"
        set_guard("promotion_enabled", v)
        _audit(
            actor="manual",
            action="set_guard",
            model_name="embed_regressor",
            reason={"promotion_enabled": v},
        )
        return {"ok": True, "promotion_enabled": v}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def api_get_health(_parsed):
    return get_health_snapshot()


def api_get_system_state(_parsed):
    from engine.runtime.system_state import compute_system_state
    health = get_health_snapshot()

    try:
        jobs = JOBS.list_jobs()
    except Exception:
        jobs = []

    try:
        kill_switches = api_get_kill_switches(_parsed)
    except Exception:
        kill_switches = {}

    state = compute_system_state(
        health=health,
        jobs=jobs,
        kill_switches=kill_switches,
    )

    state["lifecycle"] = lifecycle_snapshot()
    return state

def get_model_diagnostics():
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
            {"symbol": r[0], "horizon_s": r[1], "n": int(r[2]), "mean_z": float(r[3])}
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

        return out
    finally:
        con.close()

def api_get_model_diagnostics(_parsed):
    return {"ok": True, "data": get_model_diagnostics()}

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

from engine.dev_core.model_registry import get_stage_latest
MODEL_NAME = "embed_regressor"

def get_model_registry(limit: int = 50):
    """
    Returns champion + latest challenger + recent history.
    """
    limit = max(1, min(500, int(limit or 50)))
    con = _db_connect()
    try:
        try:
            ch = con.execute(
                """
                SELECT model_kind, model_ts_ms, metrics_json, created_ts_ms, note
                FROM model_registry
                WHERE model_name='embed_regressor' AND stage='champion'
                ORDER BY created_ts_ms DESC
                LIMIT 1
                """
            ).fetchone()
        except Exception:
            ch = None

        try:
            cl = con.execute(
                """
                SELECT model_kind, model_ts_ms, metrics_json, created_ts_ms, note
                FROM model_registry
                WHERE model_name='embed_regressor' AND stage='challenger'
                ORDER BY created_ts_ms DESC
                LIMIT 1
                """
            ).fetchone()
        except Exception:
            cl = None

        try:
            rows = con.execute(
                """
                SELECT model_kind, model_ts_ms, stage, metrics_json, created_ts_ms, note
                FROM model_registry
                WHERE model_name='embed_regressor'
                ORDER BY created_ts_ms DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        except Exception:
            rows = []

        def _row(r):
            return {
                "model_kind": r[0],
                "model_ts_ms": int(r[1]),
                "stage": r[2],
                "metrics": json.loads(r[3] or "{}"),
                "created_ts_ms": int(r[4]),
                "note": r[5],
            }

        return {
            "ok": True,
            "champion": _row(ch) if ch else None,
            "challenger": _row(cl) if cl else None,
            "history": [_row(r) for r in rows],
        }
    finally:
        con.close()

def get_embed_model_eval(limit: int = 500):
    """
    Read-only endpoint for supervised embed regressor offline eval metrics.
    Table is created by dev_core/embed_regressor.py training flow.
    Returns empty list if table is missing.
    """
    limit = max(1, min(5000, int(limit or 500)))
    con = _db_connect()
    try:
        try:
            rows = con.execute(
                """
                SELECT key_type, key, horizon_s, model_kind, ts_ms,
                       n_train, n_eval, rmse, spearman, directional_acc
                FROM embed_model_eval
                ORDER BY ts_ms DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        except Exception:
            rows = []

        out = []
        for r in rows or []:
            out.append({
                "key_type": str(r[0] or ""),
                "key": str(r[1] or ""),
                "horizon_s": int(r[2] or 0),
                "model_kind": str(r[3] or ""),
                "ts_ms": int(r[4] or 0),
                "n_train": int(r[5] or 0),
                "n_eval": int(r[6] or 0),
                "rmse": float(r[7] or 0.0),
                "spearman": float(r[8] or 0.0),
                "directional_acc": float(r[9] or 0.0),
            })
        return {"ok": True, "rows": out}
    finally:
        con.close()


def get_embed_conf_calib(horizon_s: int, model_kind: str, limit: int = 200):
    """
    Returns calibration curve points for a given horizon + model_kind.
    Stored by dev_core/embed_regressor.py training flow.
    """
    limit = max(2, min(5000, int(limit or 200)))
    hs = int(horizon_s or 0)
    mk = str(model_kind or "").strip().lower()
    if mk not in ("ridge", "mlp"):
        mk = "ridge"

    con = _db_connect()
    try:
        try:
            row = con.execute(
                """
                SELECT ts_ms, conf_k, n_points, x_json, y_json
                FROM embed_conf_calib
                WHERE horizon_s=? AND model_kind=?
                """,
                (int(hs), str(mk)),
            ).fetchone()
        except Exception:
            row = None

        if not row:
            return {"ok": True, "horizon_s": hs, "model_kind": mk, "curve": None}

        ts_ms, conf_k, n_points, xj, yj = row

        try:
            xs = [float(x) for x in json.loads(xj or "[]")]
            ys = [float(y) for y in json.loads(yj or "[]")]
        except Exception:
            xs, ys = [], []

        # cap returned points for UI
        if len(xs) > limit and len(xs) == len(ys):
            xs = xs[-limit:]
            ys = ys[-limit:]

        curve = [{"x": float(xs[i]), "y": float(ys[i])} for i in range(min(len(xs), len(ys)))]

        return {
            "ok": True,
            "horizon_s": int(hs),
            "model_kind": str(mk),
            "ts_ms": int(ts_ms or 0),
            "conf_k": float(conf_k or 0.0),
            "n_points": int(n_points or len(curve)),
            "curve": curve,
        }
    finally:
        con.close()

def get_temporal_eval(limit: int = 50):
    limit = max(1, min(5000, int(limit or 50)))
    con = _db_connect()
    try:
        try:
            rows = con.execute(
                """
                SELECT horizon_s, n, rmse, directional_acc, ts_ms
                FROM temporal_eval
                ORDER BY ts_ms DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        except Exception:
            rows = []

        out = []
        for r in rows or []:
            out.append({
                "horizon_s": int(r[0] or 0),
                "n": int(r[1] or 0),
                "rmse": float(r[2] or 0.0),
                "directional_acc": float(r[3] or 0.0),
                "ts_ms": int(r[4] or 0),
            })

        return {"ok": True, "rows": out}
    finally:
        con.close()

def get_temporal_models(limit: int = 20):
    limit = max(1, min(5000, int(limit or 20)))
    con = _db_connect()
    try:
        try:
            rows = con.execute(
                """
                SELECT model_name, window, input_dim, ts_ms, metrics_json,
                       LENGTH(weights) as weights_bytes
                FROM temporal_models
                ORDER BY ts_ms DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        except Exception:
            rows = []

        out = []
        for r in rows or []:
            try:
                mj = json.loads(r[4] or "{}")
            except Exception:
                mj = {}

            out.append({
                "model_name": str(r[0] or ""),
                "window": int(r[1] or 0),
                "input_dim": int(r[2] or 0),
                "ts_ms": int(r[3] or 0),
                "weights_bytes": int(r[5] or 0),
                "metrics": mj,
            })

        return {"ok": True, "rows": out}
    finally:
        con.close()

def get_latest_portfolio_backtest():

    """
    Returns latest portfolio backtest run + points.
    Used by broker reconciliation and dashboard.
    """
    con = _db_connect()
    try:
        row = con.execute(
            """
            SELECT id, ts_ms, start_ts_ms, end_ts_ms, metrics_json
            FROM portfolio_bt_runs
            ORDER BY ts_ms DESC
            LIMIT 1
            """
        ).fetchone()

        if not row:
            return {"ok": False, "error": "no portfolio backtest runs"}

        run_id, ts_ms, start_ts_ms, end_ts_ms, metrics_json = row

        try:
            metrics = json.loads(metrics_json or "{}")
        except Exception:
            metrics = {}

        pts = con.execute(
            """
            SELECT ts_ms, ret, equity, drawdown, detail_json
            FROM portfolio_bt_points
            WHERE run_id = ?
            ORDER BY ts_ms ASC
            """,
            (int(run_id),),
        ).fetchall()

        points = []
        for r in pts:
            try:
                detail = json.loads(r[4] or "{}")
            except Exception:
                detail = {}
            points.append({
                "ts_ms": int(r[0]),
                "ret": float(r[1]),
                "equity": float(r[2]),
                "drawdown": float(r[3]),
                "detail": detail,
            })

        return {
            "ok": True,
            "run": {
                "id": int(run_id),
                "ts_ms": int(ts_ms),
                "start_ts_ms": int(start_ts_ms),
                "end_ts_ms": int(end_ts_ms),
                "metrics": metrics,
                "points": points,
            },
        }
    finally:
        con.close()
# -------------            -- ------------------------------------------------------
# EXECUTION METRICS (PATCH 24)
# -------------            -- ------------------------------------------------------

def get_execution_metrics():
    """
    Aggregated execution quality for ops / profitability review.
    Read-only. Safe for dashboard polling.
    """
    con = _db_connect()
    try:
        fills_table = _broker_fills_table(con)

        try:
            row = con.execute(
                f"""
                SELECT
                  COUNT(*)        AS n_fills,
                  SUM(slippage)   AS total_slippage,
                  SUM(fees)       AS total_fees,
                  SUM(total_cost) AS total_cost,
                  AVG(slippage)   AS avg_slippage
                FROM {fills_table}
                """
            ).fetchone()
        except Exception:
            row = None

        try:
            last = con.execute(
                f"SELECT MAX(ts_ms) FROM {fills_table}"
            ).fetchone()
        except Exception:
            last = None

        now_ms = int(time.time() * 1000)

        return {
            "ok": True,
            "n_fills": int(row[0] or 0) if row else 0,
            "total_slippage": float(row[1] or 0.0) if row else 0.0,
            "total_fees": float(row[2] or 0.0) if row else 0.0,
            "total_cost": float(row[3] or 0.0) if row else 0.0,
            "avg_slippage": float(row[4] or 0.0) if row else 0.0,
            "last_fill_age_s": (
                (now_ms - int(last[0])) / 1000.0
                if last and last[0]
                else None
            ),
        }
    finally:
        con.close()

def get_execution_metrics_rolling():
    """
    Rolling execution metrics (24h / 7d).
    Safe read-only endpoint.
    """
    con = _db_connect()
    try:
        fills_table = _broker_fills_table(con)

        now_ms = int(time.time() * 1000)
        day_ms = 24 * 60 * 60 * 1000
        week_ms = 7 * day_ms

        def _q(since_ms):
            try:
                return con.execute(
                    f"""
                    SELECT
                      COUNT(*)        AS n_fills,
                      SUM(slippage)   AS total_slippage,
                      SUM(fees)       AS total_fees,
                      SUM(total_cost) AS total_cost,
                      AVG(slippage)   AS avg_slippage
                    FROM {fills_table}
                    WHERE ts_ms >= ?
                    """,
                    (int(since_ms),),
                ).fetchone()
            except Exception:
                return None

        r_24h = _q(now_ms - day_ms)
        r_7d  = _q(now_ms - week_ms)

        def _row(r):
            return {
                "n_fills": int(r[0] or 0) if r else 0,
                "total_slippage": float(r[1] or 0.0) if r else 0.0,
                "total_fees": float(r[2] or 0.0) if r else 0.0,
                "total_cost": float(r[3] or 0.0) if r else 0.0,
                "avg_slippage": float(r[4] or 0.0) if r else 0.0,
            }

        return {
            "ok": True,
            "last_24h": _row(r_24h),
            "last_7d": _row(r_7d),
        }
    finally:
        con.close()

def get_execution_metrics_by_symbol(limit: int = 50):
    """
    Execution cost breakdown by symbol.
    """
    limit = max(1, min(500, int(limit or 50)))
    con = _db_connect()
    try:
        fills_table = _broker_fills_table(con)

        try:
            rows = con.execute(
                f"""
                SELECT
                  symbol,
                  COUNT(*)        AS n_fills,
                  SUM(slippage)   AS total_slippage,
                  SUM(fees)       AS total_fees,
                  SUM(total_cost) AS total_cost,
                  AVG(slippage)   AS avg_slippage
                FROM {fills_table}
                GROUP BY symbol
                ORDER BY total_cost DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        except Exception:
            rows = []

        return {
            "ok": True,
            "symbols": [
                {
                    "symbol": r[0],
                    "n_fills": int(r[1] or 0),
                    "total_slippage": float(r[2] or 0.0),
                    "total_fees": float(r[3] or 0.0),
                    "total_cost": float(r[4] or 0.0),
                    "avg_slippage": float(r[5] or 0.0),
                }
                for r in rows
            ],
        }
    finally:
        con.close()

def get_execution_cost_by_confidence():
    """
    Execution cost vs confidence buckets.
    Links model confidence to real execution quality.
    """
    con = _db_connect()
    try:
        fills_table = _broker_fills_table(con)

        try:
            rows = con.execute(
                f"""
                SELECT
                  CAST(confidence * 10 AS INTEGER) AS bucket,
                  COUNT(*)        AS n_fills,
                  SUM(total_cost) AS total_cost,
                  AVG(total_cost) AS avg_cost
                FROM {fills_table}
                WHERE confidence IS NOT NULL
                GROUP BY bucket
                ORDER BY bucket ASC
                """
            ).fetchall()
        except Exception:
            rows = []

        buckets = []
        for b, n, tc, ac in rows:
            lo = max(0.0, min(0.9, (int(b) or 0) / 10.0))
            hi = lo + 0.1
            buckets.append({
                "conf_lo": lo,
                "conf_hi": hi,
                "n_fills": int(n or 0),
                "total_cost": float(tc or 0.0),
                "avg_cost": float(ac or 0.0),
            })

        return {
            "ok": True,
            "buckets": buckets,
        }
    finally:
        con.close()

def _table_exists(con, name: str) -> bool:
    try:
        row = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (str(name),),
        ).fetchone()
        return bool(row)
    except Exception:
        return False

def _broker_fills_table(con) -> str:
    # Prefer v2 if present; fall back to legacy name
    if _table_exists(con, "broker_fills_v2"):
        return "broker_fills_v2"
    return "broker_fills"


def get_social_features(symbol: str, limit: int = 200):
    """
    Read-only: recent social feature buckets for a symbol.
    Returns [] if table missing or query fails.
    """
    sym = str(symbol or "").upper().strip()
    if not sym:
        return {"ok": True, "rows": []}

    limit = max(1, min(5000, int(limit or 200)))

    con = _db_connect()
    try:
        if not _table_exists(con, "social_features"):
            return {"ok": True, "rows": []}

        try:
            rows = con.execute(
                """
                SELECT
                  bucket_ts_ms,
                  bucket_sec,

                  mention_count,
                  unique_authors,
                  new_author_ratio,
                  engagement_now,

                  sentiment_mean,
                  sentiment_dispersion,

                  mention_rate_z,
                  bot_likelihood_mean,
                  promo_likelihood_mean,
                  manip_risk,
                  attention_shock,

                  cross_platform_confirm
                FROM social_features
                WHERE symbol = ?
                ORDER BY bucket_ts_ms DESC
                LIMIT ?
                """,
                (sym, int(limit)),
            ).fetchall()
        except Exception:
            rows = []

        out = []
        for r in rows or []:
            try:
                out.append({
                    "bucket_ts_ms": int(r[0] or 0),
                    "bucket_sec": int(r[1] or 0),

                    "mention_count": int(r[2] or 0),
                    "unique_authors": int(r[3] or 0),
                    "new_author_ratio": float(r[4] or 0.0),
                    "engagement_now": float(r[5] or 0.0),

                    "sentiment_mean": float(r[6] or 0.0),
                    "sentiment_dispersion": float(r[7] or 0.0),

                    "mention_rate_z": float(r[8] or 0.0),
                    "bot_likelihood_mean": float(r[9] or 0.0),
                    "promo_likelihood_mean": float(r[10] or 0.0),
                    "manip_risk": float(r[11] or 0.0),
                    "attention_shock": float(r[12] or 0.0),

                    "cross_platform_confirm": float(r[13] or 0.0),
                })
            except Exception:
                continue

        return {"ok": True, "symbol": sym, "rows": out}
    finally:
        con.close()


def get_social_regimes(symbol: str, limit: int = 200):
    """
    Read-only: regime timeline for a symbol.
    Returns [] if table missing or query fails.
    """
    sym = str(symbol or "").upper().strip()
    if not sym:
        return {"ok": True, "rows": []}

    limit = max(1, min(5000, int(limit or 200)))

    con = _db_connect()
    try:
        if not _table_exists(con, "social_regimes"):
            return {"ok": True, "rows": []}

        try:
            rows = con.execute(
                """
                SELECT
                  bucket_ts_ms,
                  bucket_sec,
                  regime,
                  regime_conf,
                  features_json
                FROM social_regimes
                WHERE symbol = ?
                ORDER BY bucket_ts_ms DESC
                LIMIT ?
                """,
                (sym, int(limit)),
            ).fetchall()
        except Exception:
            rows = []

        out = []
        for r in rows or []:
            try:
                out.append({
                    "bucket_ts_ms": int(r[0] or 0),
                    "bucket_sec": int(r[1] or 0),
                    "regime": str(r[2] or ""),
                    "regime_conf": float(r[3] or 0.0),
                    "features": (json.loads(r[4]) if (r[4] or "").strip() else None),
                })
            except Exception:
                continue

        return {"ok": True, "symbol": sym, "rows": out}
    finally:
        con.close()


def get_social_blocks(limit: int = 200):
    """
    Read-only: recent decisions that were blocked by a social gate.
    Safe: returns [] if decision log table missing.
    """
    limit = max(1, min(2000, int(limit or 200)))

    con = _db_connect()
    try:
        # Try common table names; return empty if none exist
        table = None
        for t in ("decision_log", "decisions", "trade_decisions"):
            if _table_exists(con, t):
                table = t
                break

        if not table:
            return {"ok": True, "rows": []}

        # We only attempt JSON filtering if SQLite JSON1 is available; otherwise fallback to last rows.
        rows = []
        try:
            rows = con.execute(
                f"""
                SELECT ts_ms, symbol, reason_json
                FROM {table}
                WHERE json_extract(reason_json, '$.social_gate_block') = 1
                ORDER BY ts_ms DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        except Exception:
            try:
                rows = con.execute(
                    f"""
                    SELECT ts_ms, symbol, reason_json
                    FROM {table}
                    ORDER BY ts_ms DESC
                    LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
            except Exception:
                rows = []

        out = []
        for r in rows or []:
            try:
                out.append({
                    "ts_ms": int(r[0] or 0),
                    "symbol": str(r[1] or ""),
                    "reason": (json.loads(r[2]) if (r[2] or "").strip() else {}),
                })
            except Exception:
                continue

        return {"ok": True, "table": table, "rows": out}
    finally:
        con.close()


def get_confidence_mass():


    con = _db_connect()
    try:
        try:
            rows = con.execute(
                """
                SELECT confidence
                FROM predictions
                ORDER BY ts_ms DESC
                LIMIT 2000
                """
            ).fetchall()
        except Exception:
            rows = []

        vals = []
        for r in rows:
            try:
                vals.append(float(r[0]))
            except Exception:
                pass

        bins = [0] * 10
        for v in vals:
            v = max(0.0, min(1.0, float(v)))
            idx = int(min(9, max(0, int(v * 10.0))))
            bins[idx] += 1

        return {
            "n": int(len(vals)),
            "bins": [
                {"lo": i / 10.0, "hi": (i + 1) / 10.0, "count": int(bins[i])}
                for i in range(10)
            ],
        }
    finally:
        con.close()

def api_get_confidence_mass(_parsed):
    return get_confidence_mass()

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

def get_alerts():
    con = _db_connect()
    try:
        rows = con.execute("""
            SELECT
              a.id, a.ts_ms, a.severity, a.symbol, a.horizon_s,
              a.expected_z, a.confidence, a.event_title, a.rule_id, a.explain_json,

              ak.alert_id IS NOT NULL AS acked,
              ak.acked_ts_ms,
              ak.acked_by,

              ar.alert_id IS NOT NULL AS resolved,
              ar.resolved_ts_ms,
              ar.resolved_by,
              ar.reason

            FROM alerts a
            LEFT JOIN alert_acks ak ON ak.alert_id = a.id
            LEFT JOIN alert_resolutions ar ON ar.alert_id = a.id
            ORDER BY a.ts_ms DESC
            LIMIT 50
        """).fetchall()

        return [{

            "id": r[0],
            "ts_ms": r[1],
            "severity": r[2],
            "symbol": r[3],
            "horizon_s": r[4],
            "expected_z": r[5],
            "confidence": r[6],
            "event_title": r[7],
            "rule_id": r[8],
            "explain_json": _normalize_explain_json(r[9]),

            "acked": bool(r[10]),
            "acked_ts_ms": r[11],
            "acked_by": r[12],

            "resolved": bool(r[13]),
            "resolved_ts_ms": r[14],
            "resolved_by": r[15],
            "resolved_reason": r[16],

        } for r in rows]
    finally:
        con.close()

def api_get_alerts(_parsed):
    return {"ok": True, "rows": get_alerts()}

def api_get_validation(_parsed):
    try:
        from engine.dev_core.validation import get_validation
        return {"ok": True, "rows": get_validation()}
    except Exception as e:
        return {"ok": False, "error": str(e)}

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
def get_size_policy():
    con = _db_connect()
    try:
        try:
            r = con.execute(
                """
                SELECT id, ts_ms, lookback_days, buckets, method, params_json, metrics_json
                FROM size_policy
                ORDER BY ts_ms DESC
                LIMIT 1
                """
            ).fetchone()
        except Exception:
            r = None

        if not r:
            return {"ok": True, "policy": None, "points": []}

        pid, ts_ms, lookback_days, buckets, method, params_json, metrics_json = r
        try:
            params = json.loads(params_json or "{}")
        except Exception:
            params = {}
        try:
            metrics = json.loads(metrics_json or "{}")
        except Exception:
            metrics = {}

        try:
            pts = con.execute(
                """
                SELECT bucket_idx, conf_lo, conf_hi, n, mean_net_ret, std_net_ret, factor
                FROM size_policy_points
                WHERE policy_id=?
                ORDER BY bucket_idx ASC
                """,
                (int(pid),),
            ).fetchall()
        except Exception:
            pts = []

        points = []
        for bi, clo, chi, n, mnr, sdr, f in pts or []:
            points.append({
                "bucket_idx": int(bi),
                "conf_lo": float(clo),
                "conf_hi": float(chi),
                "n": int(n),
                "mean_net_ret": float(mnr),
                "std_net_ret": float(sdr),
                "factor": float(f),
            })

        return {
            "ok": True,
            "policy": {
                "id": int(pid),
                "ts_ms": int(ts_ms),
                "lookback_days": int(lookback_days),
                "buckets": int(buckets),
                "method": str(method),
                "params": params,
                "metrics": metrics,
            },
            "points": points,
        }
    finally:
        con.close()

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

def _wrap_get_model_registry(parsed, _ctx):
    if not get_model_registry:
        return _missing("get_model_registry")
    qs = _qs(parsed)
    limit = int(qs.get("limit", "50") or "50")
    return get_model_registry(limit=limit)

def _wrap_get_embed_model_eval(parsed, _ctx):
    if not get_embed_model_eval:
        return _missing("get_embed_model_eval")
    qs = _qs(parsed)
    limit = int(qs.get("limit", "500") or "500")
    return get_embed_model_eval(limit=limit)

def _wrap_get_embed_conf_calib(parsed, _ctx):
    if not get_embed_conf_calib:
        return _missing("get_embed_conf_calib")
    qs = _qs(parsed)
    horizon_s = int(qs.get("horizon_s", "0") or "0")
    model_kind = str(qs.get("model_kind", "") or "")
    limit = int(qs.get("limit", "200") or "200")
    return get_embed_conf_calib(horizon_s=horizon_s, model_kind=model_kind, limit=limit)

def _wrap_get_temporal_eval(parsed, _ctx):
    if not get_temporal_eval:
        return _missing("get_temporal_eval")
    qs = _qs(parsed)
    limit = int(qs.get("limit", "50") or "50")
    return get_temporal_eval(limit=limit)

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

def _wrap_get_social_features(parsed, _ctx):
    if not get_social_features:
        return _missing("get_social_features")
    qs = _qs(parsed)
    symbol = str(qs.get("symbol", "") or "").strip()
    if not symbol:
        return {"ok": False, "error": "missing_symbol"}
    limit = int(qs.get("limit", "200") or "200")
    return get_social_features(symbol=symbol, limit=limit)

def _wrap_get_social_regimes(parsed, _ctx):
    if not get_social_regimes:
        return _missing("get_social_regimes")
    qs = _qs(parsed)
    symbol = str(qs.get("symbol", "") or "").strip()
    if not symbol:
        return {"ok": False, "error": "missing_symbol"}
    limit = int(qs.get("limit", "200") or "200")
    return get_social_regimes(symbol=symbol, limit=limit)

def _wrap_get_social_blocks(parsed, _ctx):
    if not get_social_blocks:
        return _missing("get_social_blocks")
    qs = _qs(parsed)
    limit = int(qs.get("limit", "200") or "200")
    return get_social_blocks(limit=limit)

def _wrap_api_get_validation(parsed, _ctx):
    if not api_get_validation:
        return _missing("api_get_validation")
    return api_get_validation(parsed)

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


def api_get_execution_metrics(_parsed):
    return get_execution_metrics()


def api_get_execution_metrics_rolling(_parsed):
    return get_execution_metrics_rolling()


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


def api_get_temporal_models(parsed):
    qs = _qs(parsed)
    limit = int(qs.get("limit", "20") or "20")
    return get_temporal_models(limit=limit)


def api_get_latest_portfolio_backtest(_parsed):
    return get_latest_portfolio_backtest()


def api_get_execution_metrics_by_symbol(parsed):
    qs = _qs(parsed)
    limit = int(qs.get("limit", "50") or "50")
    return get_execution_metrics_by_symbol(limit=limit)


def api_get_execution_cost_by_confidence(_parsed):
    return get_execution_cost_by_confidence()


def api_get_social_features(parsed):
    qs = _qs(parsed)
    symbol = str(qs.get("symbol", "") or "").strip()
    if not symbol:
        return {"ok": False, "error": "missing_symbol"}
    limit = int(qs.get("limit", "200") or "200")
    return get_social_features(symbol=symbol, limit=limit)


def api_get_social_regimes(parsed):
    qs = _qs(parsed)
    symbol = str(qs.get("symbol", "") or "").strip()
    if not symbol:
        return {"ok": False, "error": "missing_symbol"}
    limit = int(qs.get("limit", "200") or "200")
    return get_social_regimes(symbol=symbol, limit=limit)


def api_get_social_blocks(parsed):
    qs = _qs(parsed)
    limit = int(qs.get("limit", "200") or "200")
    return get_social_blocks(limit=limit)


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

# ------------------------------
# EXECUTION HARD-GATE (production safety)
# ------------------------------

# Default heuristic gate; can be tightened via env.
# - EXECUTION_JOB_NAMES: comma-separated exact names to treat as execution
# - EXECUTION_JOB_SUBSTRINGS: comma-separated substrings; any match => execution
_EXECUTION_JOB_NAMES = {
    x.strip()
    for x in os.environ.get("EXECUTION_JOB_NAMES", "").split(",")
    if x.strip()
}

# Sensible defaults that catch common "live execution" jobs without blocking ingest/training.
# Override in prod if you want only exact name matching.
_EXECUTION_JOB_SUBSTRINGS = [
    x.strip().lower()
    for x in os.environ.get(
        "EXECUTION_JOB_SUBSTRINGS",
        "broker_apply,apply_orders,apply_latest_portfolio_orders,execute,live_execution,ibkr,alpaca",
    ).split(",")
    if x.strip()
]

def _is_execution_job(job_name: str) -> bool:
    n = str(job_name or "").strip()
    if not n:
        return False
    if n in _EXECUTION_JOB_NAMES:
        return True
    ln = n.lower()
    for sub in _EXECUTION_JOB_SUBSTRINGS:
        try:
            if sub and sub in ln:
                return True
        except Exception:
            continue
    return False

def _execution_gate_snapshot():
    """
    Single place to decide if LIVE execution is allowed.
    - must be system_state LIVE
    - must NOT be kill-switch enabled
    - must be execution_mode armed (if available)
    """
    try:
        from engine.runtime.system_state import compute_system_state
    except Exception:
        return {"ok": False, "error": "system_state_module_missing"}

    try:
        health = get_health_snapshot()
    except Exception:
        health = {}

    try:
        jobs = JOBS.list_jobs()
    except Exception:
        jobs = []

    try:
        kill_switches = api_get_kill_switches(None) or {}
    except Exception:
        kill_switches = {}

    st = compute_system_state(health=health, jobs=jobs, kill_switches=kill_switches) or {}
    state = str(st.get("state") or "")
    if state != "LIVE":
        return {
            "ok": False,
            "error": "execution_blocked_not_live",
            "system_state": st,
        }

    # require explicit arming if execution_mode module is present
    try:
        em = _exec_mode_get() or {}
        armed = bool(em.get("armed")) if isinstance(em, dict) else False
        if not armed:
            return {
                "ok": False,
                "error": "execution_blocked_not_armed",
                "execution_mode": em,
                "system_state": st,
            }
    except Exception:
        # If exec_mode is unavailable, fail-closed by default (safer)
        if os.environ.get("EXECUTION_GATE_FAIL_OPEN_IF_NO_EXEC_MODE", "0") != "1":
            return {
                "ok": False,
                "error": "execution_blocked_exec_mode_unavailable",
                "system_state": st,
            }

    return {"ok": True, "system_state": st}

def api_post_job_start(parsed, body):
    name = _job_name_from(parsed, body)
    if not name:
        return {"ok": False, "error": "missing_name"}

    if name not in ALLOWED_JOBS:
        return {"ok": False, "error": f"job_not_allowed:{name}"}

    # HARD-GATE: refuse starting execution jobs unless LIVE+ARMED
    if _is_execution_job(name):
        gate = _execution_gate_snapshot()
        if not gate.get("ok"):
            res = {
                "ok": False,
                "error": str(gate.get("error") or "execution_blocked"),
                "job": name,
                "gate": gate,
            }
            try:
                write_job_history(job_name=name, event="start_blocked", detail=res)
            except Exception:
                pass
            return res

    try:
        res = JOBS.start(name)
    except Exception as e:
        res = {"ok": False, "error": str(e)}

    try:
        write_job_history(job_name=name, event="start", detail=res)
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
        write_job_history(job_name=name, event="stop", detail=res)
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

    # HARD-GATE: refuse pipeline execution leg unless LIVE+ARMED
    if include_execution:
        gate = _execution_gate_snapshot()
        if not gate.get("ok"):
            res = {
                "ok": False,
                "error": str(gate.get("error") or "execution_blocked"),
                "gate": gate,
            }
            try:
                write_job_history(job_name="pipeline", event="run_blocked", detail=res)
            except Exception:
                pass
            return res

    try:
        res = ORCHESTRATOR.run_pipeline(include_execution=include_execution)
    except Exception as e:
        res = {"ok": False, "error": str(e)}

    try:
        write_job_history(job_name="pipeline", event="run", detail=res)
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
    "api_get_alerts": api_get_alerts,
    "api_get_validation": api_get_validation,
    "api_get_model_diagnostics": api_get_model_diagnostics,
    "api_get_model_registry": _wrap_get_model_registry,
    "api_get_embed_model_eval": _wrap_get_embed_model_eval,
    "api_get_embed_conf_calib": _wrap_get_embed_conf_calib,
    "api_get_temporal_eval": _wrap_get_temporal_eval,
    "api_get_temporal_models": _wrap_get_temporal_models,
    "api_get_latest_portfolio_backtest": api_get_latest_portfolio_backtest,
    "api_get_execution_metrics": api_get_execution_metrics,
    "api_get_execution_metrics_rolling": api_get_execution_metrics_rolling,
    "api_get_execution_metrics_by_symbol": api_get_execution_metrics_by_symbol,
    "api_get_execution_cost_by_confidence": api_get_execution_cost_by_confidence,
    "api_get_social_features": api_get_social_features,
    "api_get_social_regimes": api_get_social_regimes,
    "api_get_social_blocks": api_get_social_blocks,
    "api_get_confidence_mass": api_get_confidence_mass,
        ("GET",  "/api/schema/audit", "api_get_schema_audit"),

        # shadow capital allocation scoring (governance)
        ("GET",  "/api/shadow/capital_scores", "api_get_shadow_capital_scores"),
        ("POST", "/api/shadow/capital_scores/run", "api_post_shadow_capital_run"),

    # POST
    "api_post_job_start": api_post_job_start,
    "api_post_job_stop": api_post_job_stop,
    "api_post_pipeline_run": api_post_pipeline_run,
    "api_post_rollback": api_post_rollback,
}

# -------------            -- ------------------------------------------------------
# SERVER
# -------------            -- ------------------------------------------------------

def run_server():
    global _HTTPD

    # ---------------------------------------------------
    # HARD DB BOOTSTRAP (idempotent, REQUIRED)
    # ---------------------------------------------------
    try:
        _init_db()
    except Exception as e:
        log.critical("database init failed: %s", e)

        raise

    # Ensure coordination + persistence tables exist before any jobs
    try:
        _ensure_job_locks()
    except Exception:
        pass
    try:
        _ensure_job_history()
    except Exception:
        pass
    try:
        _ensure_alert_acks()
    except Exception:
        pass
    try:
        _ensure_alert_resolutions()
    except Exception:
        pass
    try:
        _ensure_equity_drift()
    except Exception:
        pass
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
            args=(rollback_champion, write_job_history),
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

    # Temporal predictor tables (shadow-only)
    def _ensure_temporal_eval_boot():

        con = _db_connect()
        try:
            con.execute("""
                CREATE TABLE IF NOT EXISTS temporal_eval (
                  horizon_s INTEGER NOT NULL,
                  n INTEGER NOT NULL,
                  rmse REAL NOT NULL,
                  directional_acc REAL NOT NULL,
                  ts_ms INTEGER NOT NULL,
                  PRIMARY KEY (ts_ms)
                )
            """)
            con.commit()
        finally:
            con.close()

    def _ensure_temporal_predictions():
        con = _db_connect()
        try:
            con.execute("""
                CREATE TABLE IF NOT EXISTS temporal_predictions (
                  event_id INTEGER NOT NULL,
                  ts_ms INTEGER NOT NULL,
                  horizon_s INTEGER NOT NULL,
                  predicted_z REAL NOT NULL,
                  created_at_ms INTEGER NOT NULL,
                  PRIMARY KEY (event_id, horizon_s)
                )
            """)
            con.commit()
        finally:
            con.close()

    def _ensure_temporal_models():
        con = _db_connect()
        try:
            con.execute("""
                CREATE TABLE IF NOT EXISTS temporal_models (
                  model_name TEXT PRIMARY KEY,
                  window INTEGER NOT NULL,
                  input_dim INTEGER NOT NULL,
                  weights BLOB NOT NULL,
                  metrics_json TEXT,
                  ts_ms INTEGER NOT NULL
                )
            """)
            con.commit()
        finally:
            con.close()

    try:
        _ensure_temporal_eval_boot()

    except Exception:
        pass
    try:
        _ensure_temporal_predictions()
    except Exception:
        pass
    try:
        _ensure_temporal_models()
    except Exception:
        pass

    log.info("dashboard running at http://%s:%s/ui/dashboard.html", host, port)

    # ---------------------------------------------------
    # Deterministic Supervisor Boot (optional)
    # - HARD-GATE execution jobs at boot unless LIVE+ARMED
    # ---------------------------------------------------
    if AUTO_BOOT_DAEMONS and AUTO_BOOT_TARGETS:
        try:
            targets = list(AUTO_BOOT_TARGETS)

            # Filter execution jobs unless gate passes
            blocked = []
            try:
                gate = _execution_gate_snapshot()
                if not gate.get("ok"):
                    filt = []
                    for tname in targets:
                        if _is_execution_job(tname):
                            blocked.append(tname)
                        else:
                            filt.append(tname)
                    targets = filt
            except Exception:
                pass

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
