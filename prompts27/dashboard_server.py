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


def api_get_ui_overview(_parsed, _ctx=None):
    """
    Executive Overview endpoint for non-technical landing view.
    Returns system health, market environment, capital at risk, strategy counts,
    last decision timestamp, and alert severity counts.
    """
    import time
    from engine.runtime.storage import connect as _db_connect
    
    now_ms = int(time.time() * 1000)
    
    # Get system health
    try:
        health = get_health_snapshot()
        system_health = "healthy"
        if not health.get("ok"):
            system_health = "degraded"
        # Check if paused via kill switches
        try:
            kill_switches = api_get_kill_switches(None)
            if any(kill_switches.get("switches", {}).values()):
                system_health = "paused"
        except Exception:
            pass
    except Exception:
        system_health = "degraded"
    
    # Market environment (mock implementation - would integrate with real market analysis)
    market_env = "Normal volatility conditions"
    market_confidence = "Medium"
    
    # Capital at risk (today) - would integrate with portfolio/risk systems
    capital_at_risk = 125000  # Mock value
    
    # Active strategy counts by state
    strategy_counts = {"active": 3, "paused": 1, "testing": 2}
    
    # Last decision timestamp
    last_decision_ts = now_ms - 1800000  # 30 minutes ago as mock
    
    # Alert severity counts (24h)
    alert_counts = {"critical": 0, "warning": 2, "info": 5}
    try:
        con = _db_connect()
        try:
            # Get alerts from last 24 hours
            rows = con.execute("""
                SELECT severity, COUNT(*) as count
                FROM alerts
                WHERE ts_ms > ?
                GROUP BY severity
            """, (now_ms - 86400000,)).fetchall()
            
            alert_counts = {"critical": 0, "warning": 0, "info": 0}
            for severity, count in rows:
                if severity and severity.lower() in alert_counts:
                    alert_counts[severity.lower()] = count
        except Exception:
            pass
        finally:
            con.close()
    except Exception:
        pass
    
    return {
        "ok": True,
        "ts_ms": now_ms,
        "system_health": system_health,
        "market_environment": {
            "description": market_env,
            "confidence": market_confidence
        },
        "capital_at_risk_today": capital_at_risk,
        "strategy_counts": strategy_counts,
        "last_decision_timestamp_ms": last_decision_ts,
        "alert_severity_counts_24h": alert_counts
    }

def api_get_decisions(parsed):
    """
    GET /api/ui/decisions
    Returns decision cards with action, symbol, size delta, certainty, risk impact, and plain-English why.
    """
    import time
    from engine.runtime.storage import connect as _db_connect
    
    try:
        con = _db_connect()
        try:
            # Get recent portfolio orders as decisions
            rows = con.execute("""
                SELECT 
                    id,
                    ts_ms,
                    symbol,
                    action,
                    from_side,
                    to_side,
                    from_weight,
                    to_weight,
                    delta_weight,
                    source_alert_id,
                    explain_json
                FROM portfolio_orders 
                ORDER BY ts_ms DESC 
                LIMIT 50
            """).fetchall()
            
            decisions = []
            for row in rows:
                # Determine action type
                action = "hold"
                if row['action'] in ['INCREASE', 'OPEN']:
                    action = "increase"
                elif row['action'] in ['DECREASE', 'CLOSE']:
                    action = "reduce"
                
                # Calculate size delta
                size_delta = abs(float(row['delta_weight'] or 0))
                
                # Extract certainty from explain_json or use default
                certainty = 0.7  # default medium certainty
                explain_data = {}
                if row['explain_json']:
                    try:
                        explain_data = json.loads(row['explain_json'])
                        certainty = float(explain_data.get('confidence', certainty))
                    except Exception:
                        pass
                
                # Determine risk impact based on size delta
                risk_impact = "low"
                if size_delta > 0.1:
                    risk_impact = "high"
                elif size_delta > 0.05:
                    risk_impact = "medium"
                
                # Generate plain-English "why"
                why = f"{action.capitalize()} {row['symbol']} position by {size_delta:.2%}"
                if explain_data.get('reason'):
                    why = explain_data['reason'][:100]  # limit length
                
                decisions.append({
                    "decision_id": row['id'],
                    "ts_ms": row['ts_ms'],
                    "action": action,
                    "symbol": row['symbol'],
                    "size_delta": size_delta,
                    "certainty": certainty,
                    "risk_impact": risk_impact,
                    "why": why,
                    "from_weight": float(row['from_weight'] or 0),
                    "to_weight": float(row['to_weight'] or 0),
                    "source_alert_id": row['source_alert_id']
                })
            
            return {
                "ok": True,
                "decisions": decisions,
                "count": len(decisions)
            }
            
        finally:
            con.close()
    except Exception as e:
        log.error("api_get_decisions failed: %s", e)
        return {
            "ok": False,
            "error": str(e),
            "decisions": []
        }

def api_get_decision(parsed):
    """
    GET /api/ui/decision/<decision_id>
    Returns detailed drilldown for a specific decision.
    """
    import time
    from engine.runtime.storage import connect as _db_connect
    
    decision_id = parsed.get('decision_id')
    if not decision_id:
        return {"ok": False, "error": "decision_id required"}
    
    try:
        con = _db_connect()
        try:
            # Get the specific decision
            row = con.execute("""
                SELECT 
                    po.id,
                    po.ts_ms,
                    po.symbol,
                    po.action,
                    po.from_side,
                    po.to_side,
                    po.from_weight,
                    po.to_weight,
                    po.delta_weight,
                    po.source_alert_id,
                    po.explain_json,
                    ps.side as current_side,
                    ps.weight as current_weight
                FROM portfolio_orders po
                LEFT JOIN portfolio_state ps ON po.symbol = ps.symbol
                WHERE po.id = ?
            """, (decision_id,)).fetchone()
            
            if not row:
                return {"ok": False, "error": "decision not found"}
            
            # Parse explain data
            explain_data = {}
            if row['explain_json']:
                try:
                    explain_data = json.loads(row['explain_json'])
                except Exception:
                    pass
            
            # Get decision log entries for this symbol around the same time
            decision_logs = []
            try:
                log_rows = con.execute("""
                    SELECT 
                        model_name,
                        model_kind,
                        model_ts_ms,
                        predicted_z,
                        confidence,
                        features_json,
                        explain_json
                    FROM decision_log 
                    WHERE symbol = ? AND ABS(ts_ms - ?) < 300000
                    ORDER BY ts_ms DESC
                    LIMIT 5
                """, (row['symbol'], row['ts_ms'])).fetchall()
                
                for log_row in log_rows:
                    decision_logs.append({
                        "model_name": log_row['model_name'],
                        "model_kind": log_row['model_kind'],
                        "model_ts_ms": log_row['model_ts_ms'],
                        "predicted_z": float(log_row['predicted_z']),
                        "confidence": float(log_row['confidence']),
                        "features": json.loads(log_row['features_json'] or '{}'),
                        "explain": json.loads(log_row['explain_json'] or '{}')
                    })
            except Exception:
                pass
            
            # Get risk gates triggered
            risk_gates = []
            if explain_data.get('risk_gates'):
                risk_gates = explain_data['risk_gates']
            elif float(row['delta_weight'] or 0) > 0.15:
                risk_gates = ["large_position_change"]
            elif row['action'] in ['CLOSE', 'REVERSE']:
                risk_gates = ["position_exit"]
            
            # Determine action type
            action = "hold"
            if row['action'] in ['INCREASE', 'OPEN']:
                action = "increase"
            elif row['action'] in ['DECREASE', 'CLOSE']:
                action = "reduce"
            
            # Build explainability data
            top_drivers = _extract_top_drivers(explain_data, decision_logs)
            model_outputs = _extract_model_outputs(decision_logs)
            
            return {
                "ok": True,
                "decision": {
                    "decision_id": row['id'],
                    "ts_ms": row['ts_ms'],
                    "action": action,
                    "symbol": row['symbol'],
                    "size_delta": abs(float(row['delta_weight'] or 0)),
                    "certainty": explain_data.get('confidence', 0.7),
                    "risk_impact": explain_data.get('risk_impact', 'medium'),
                    "why": explain_data.get('reason', f"{action.capitalize()} {row['symbol']} position"),
                    "inputs_summary": {
                        "from_weight": float(row['from_weight'] or 0),
                        "to_weight": float(row['to_weight'] or 0),
                        "current_side": row['current_side'],
                        "current_weight": float(row['current_weight'] or 0)
                    },
                    "model_versions": list(set([log['model_name'] for log in decision_logs if log['model_name']])),
                    "confidence": explain_data.get('confidence', 0.7),
                    "risk_gates_triggered": risk_gates,
                    "allocation_before_after": {
                        "before": float(row['from_weight'] or 0),
                        "after": float(row['to_weight'] or 0),
                        "change": float(row['delta_weight'] or 0)
                    },
                    "decision_logs": decision_logs,
                    "source_alert_id": row['source_alert_id'],
                    # New explainability fields
                    "top_drivers": top_drivers,
                    "model_outputs": model_outputs,
                    "plain_english_explanation": _generate_plain_explanation(action, row['symbol'], top_drivers, risk_gates)
                }
            }
            
        finally:
            con.close()
    except Exception as e:
        log.error("api_get_decision failed: %s", e)
        return {
            "ok": False,
            "error": str(e)
        }

def _extract_top_drivers(explain_data, decision_logs):
    """Extract top drivers from explain data and decision logs"""
    drivers = []
    
    # Market stress driver
    if explain_data.get('market_stress'):
        drivers.append({
            "type": "market_stress",
            "name": "Market Stress",
            "value": explain_data['market_stress'],
            "impact": "high" if explain_data['market_stress'] > 0.7 else "medium"
        })
    
    # Sentiment driver
    if explain_data.get('sentiment'):
        drivers.append({
            "type": "sentiment",
            "name": "Market Sentiment",
            "value": explain_data['sentiment'],
            "impact": "high" if abs(explain_data['sentiment']) > 0.6 else "medium"
        })
    
    # Calibration driver
    if explain_data.get('calibration_score'):
        drivers.append({
            "type": "calibration",
            "name": "Model Calibration",
            "value": explain_data['calibration_score'],
            "impact": "high" if explain_data['calibration_score'] < 0.5 else "medium"
        })
    
    # Extract from decision logs
    for log in decision_logs[:3]:
        if log['features']:
            for feature_name, feature_val in log['features'].items():
                if isinstance(feature_val, (int, float)) and abs(feature_val) > 0.5:
                    drivers.append({
                        "type": "feature",
                        "name": feature_name.replace('_', ' ').title(),
                        "value": feature_val,
                        "model": log['model_name'],
                        "impact": "high" if abs(feature_val) > 1.0 else "medium"
                    })
    
    return drivers[:5]  # Return top 5 drivers

def _extract_model_outputs(decision_logs):
    """Extract model outputs for explainability"""
    outputs = []
    
    for log in decision_logs:
        outputs.append({
            "model_name": log['model_name'],
            "model_kind": log['model_kind'],
            "predicted_z": log['predicted_z'],
            "confidence": log['confidence'],
            "timestamp": log['model_ts_ms'],
            "interpretation": _interpret_prediction(log['predicted_z'])
        })
    
    return outputs

def _interpret_prediction(z_score):
    """Interpret z-score prediction"""
    if z_score > 2.0:
        return "Strong positive signal"
    elif z_score > 1.0:
        return "Moderate positive signal"
    elif z_score > 0.5:
        return "Weak positive signal"
    elif z_score > -0.5:
        return "Neutral signal"
    elif z_score > -1.0:
        return "Weak negative signal"
    elif z_score > -2.0:
        return "Moderate negative signal"
    else:
        return "Strong negative signal"

def api_get_timeline(parsed):
    """
    GET /api/ui/timeline
    Returns system activity timeline with entries for INGEST, MODEL, RISK, DECISION, EXECUTION
    """
    import time
    from engine.runtime.storage import connect as _db_connect
    
    # Parse optional parameters
    limit = int(parsed.get('limit', 100))
    since_ms = parsed.get('since_ms')
    
    try:
        con = _db_connect()
        try:
            timeline_entries = []
            
            # 1. Get INGEST entries from job_history
            try:
                ingest_rows = con.execute("""
                    SELECT 
                        id,
                        ts_ms,
                        job_name as label,
                        event as description,
                        'INGEST' as entry_type,
                        id as reference_id
                    FROM job_history 
                    WHERE job_name LIKE '%ingest%' OR job_name LIKE '%poll%' OR job_name LIKE '%process%'
                    AND (? IS NULL OR ts_ms > ?)
                    ORDER BY ts_ms DESC
                    LIMIT ?
                """, (since_ms, since_ms, limit // 5)).fetchall()
                
                for row in ingest_rows:
                    timeline_entries.append({
                        "id": f"ingest_{row['id']}",
                        "ts_ms": row['ts_ms'],
                        "type": "INGEST",
                        "label": row['label'],
                        "description": row['description'] or "Data ingestion completed",
                        "reference_id": row['reference_id']
                    })
            except Exception as e:
                log.warning("Failed to get ingest entries: %s", e)
            
            # 2. Get MODEL entries from model_registry
            try:
                model_rows = con.execute("""
                    SELECT 
                        model_name as id,
                        model_ts_ms as ts_ms,
                        model_name as label,
                        'Model ' || stage || ' - ' || model_kind as description,
                        'MODEL' as entry_type,
                        model_name as reference_id
                    FROM model_registry
                    WHERE (? IS NULL OR model_ts_ms > ?)
                    ORDER BY ts_ms DESC
                    LIMIT ?
                """, (since_ms, since_ms, limit // 5)).fetchall()
                
                for row in model_rows:
                    timeline_entries.append({
                        "id": f"model_{row['id']}",
                        "ts_ms": row['ts_ms'],
                        "type": "MODEL",
                        "label": row['label'],
                        "description": row['description'],
                        "reference_id": row['reference_id']
                    })
            except Exception as e:
                log.warning("Failed to get model entries: %s", e)
            
            # 3. Get RISK entries from alerts
            try:
                risk_rows = con.execute("""
                    SELECT 
                        id,
                        ts_ms,
                        severity || ' - ' || symbol as label,
                        event_title as description,
                        'RISK' as entry_type,
                        id as reference_id
                    FROM alerts
                    WHERE (? IS NULL OR ts_ms > ?)
                    ORDER BY ts_ms DESC
                    LIMIT ?
                """, (since_ms, since_ms, limit // 5)).fetchall()
                
                for row in risk_rows:
                    timeline_entries.append({
                        "id": f"risk_{row['id']}",
                        "ts_ms": row['ts_ms'],
                        "type": "RISK",
                        "label": row['label'],
                        "description": row['description'],
                        "reference_id": str(row['reference_id'])
                    })
            except Exception as e:
                log.warning("Failed to get risk entries: %s", e)
            
            # 4. Get DECISION entries from portfolio_orders
            try:
                decision_rows = con.execute("""
                    SELECT 
                        id,
                        ts_ms,
                        action || ' - ' || symbol as label,
                        printf('Weight change: %+.2f%%', (delta_weight * 100)) as description,
                        'DECISION' as entry_type,
                        id as reference_id
                    FROM portfolio_orders
                    WHERE (? IS NULL OR ts_ms > ?)
                    ORDER BY ts_ms DESC
                    LIMIT ?
                """, (since_ms, since_ms, limit // 5)).fetchall()
                
                for row in decision_rows:
                    timeline_entries.append({
                        "id": f"decision_{row['id']}",
                        "ts_ms": row['ts_ms'],
                        "type": "DECISION",
                        "label": row['label'],
                        "description": row['description'],
                        "reference_id": str(row['reference_id'])
                    })
            except Exception as e:
                log.warning("Failed to get decision entries: %s", e)
            
            # 5. Get EXECUTION entries from job_history (execution-related jobs)
            try:
                execution_rows = con.execute("""
                    SELECT 
                        id,
                        ts_ms,
                        job_name as label,
                        event as description,
                        'EXECUTION' as entry_type,
                        id as reference_id
                    FROM job_history
                    WHERE (job_name LIKE '%execute%' OR job_name LIKE '%broker%' OR job_name LIKE '%order%')
                    AND (? IS NULL OR ts_ms > ?)
                    ORDER BY ts_ms DESC
                    LIMIT ?
                """, (since_ms, since_ms, limit // 5)).fetchall()
                
                for row in execution_rows:
                    timeline_entries.append({
                        "id": f"execution_{row['id']}",
                        "ts_ms": row['ts_ms'],
                        "type": "EXECUTION",
                        "label": row['label'],
                        "description": row['description'],
                        "reference_id": row['reference_id']
                    })
            except Exception as e:
                log.warning("Failed to get execution entries: %s", e)
            
            # Sort all entries by timestamp (most recent first) and apply overall limit
            timeline_entries.sort(key=lambda x: x['ts_ms'], reverse=True)
            timeline_entries = timeline_entries[:limit]
            
            return {
                "ok": True,
                "entries": timeline_entries,
                "count": len(timeline_entries),
                "types": list(set(entry['type'] for entry in timeline_entries))
            }
            
        finally:
            con.close()
    except Exception as e:
        log.error("api_get_timeline failed: %s", e)
        return {
            "ok": False,
            "error": str(e),
            "entries": []
        }

def _generate_plain_explanation(action, symbol, drivers, risk_gates):
    """Generate plain English explanation"""
    explanation = f"Decision to {action} {symbol} position was driven by "
    
    if drivers:
        top_driver = drivers[0]
        explanation += f"{top_driver['name'].lower()} "
        if top_driver.get('value'):
            explanation += f"(score: {top_driver['value']:.2f}) "
    
    if len(drivers) > 1:
        explanation += f"and {len(drivers)-1} other factors. "
    else:
        explanation += ". "
    
    if risk_gates:
        explanation += f"Risk checks triggered: {', '.join(risk_gates)}. "
    
    explanation += f"Action taken with {drivers[0]['impact'] if drivers else 'medium'} confidence."
    
    return explanation

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

# Decisions UI route specs
ROUTE_SPECS_DECISIONS = [
    ("GET", "/api/ui/decisions", "api_get_decisions"),
    ("GET", "/api/ui/decision", "api_get_decision"),
]

# Timeline UI route specs
ROUTE_SPECS_TIMELINE = [
    ("GET", "/api/ui/timeline", "api_get_timeline"),
]

ROUTE_SPECS = (
    list(ROUTE_SPECS_SYSTEM)
    + list(ROUTE_SPECS_JOBS)
    + list(ROUTE_SPECS_OPS)
    + list(ROUTE_SPECS_CAPITAL_ALLOCATION)
    + list(ROUTE_SPECS_DECISIONS)
    + list(ROUTE_SPECS_TIMELINE)
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
    "api_get_ui_overview": api_get_ui_overview,

    # Decisions UI
    "api_get_decisions": api_get_decisions,
    "api_get_decision": api_get_decision,

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
