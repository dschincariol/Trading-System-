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

from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from collections import deque
from typing import Deque, Dict, Optional

# Ensure static UI paths resolve even when launched from another working directory
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
try:
    os.chdir(_BASE_DIR)
except Exception:
    pass

# SINGLE SOURCE OF TRUTH FOR SQLITE
from dev_core.storage import connect as _db_connect
from dev_core.storage import connect
from dev_core.trade_attribution_ledger import upsert_from_latest_pnl_attribution_snapshot
from dev_core.storage import init_db as _init_db

from dev_core.learning import learn_relevance_stats
from dev_core.training_guard import (
    training_allowed,
    get_training_status,
    set_training_mode,
)

from dev_core.kill_switch import (
    snapshot as _kill_switch_snapshot,
    set_kill_switch as _kill_switch_set,
    clear as _kill_switch_clear,
)
from dev_core.promotion_hardening import manual_rollback as _manual_rollback

from dev_core.execution_mode import (
    get_execution_mode as _exec_mode_get,
    set_execution_mode as _exec_mode_set,
    get_execution_overlays as _exec_overlays_get,
)

from dev_core.market_stress import get_market_stress_snapshot as _market_stress_snapshot

try:
    from api_handlers import api_get_kill_switches as _api_get_kill_switches_impl
    from api_handlers import api_get_job_log as _api_get_job_log_impl
    from api_handlers import api_get_job_history as _api_get_job_history_impl
except Exception:
    _api_get_kill_switches_impl = None
    _api_get_job_log_impl = None
    _api_get_job_history_impl = None

ALLOWED_JOBS = {

    "post_promotion_monitor": ("post_promotion_monitor.py", "oneshot"),
    "kill_slippage_monitor": ("kill_slippage_monitor.py", "oneshot"),
    "kill_drift_monitor": ("kill_drift_monitor.py", "oneshot"),
    "kill_health_monitor": ("kill_health_monitor.py", "oneshot"),
    "snapshot_equity": ("snapshot_equity.py", "oneshot"),
    "train_drawdown_policy": ("train_drawdown_policy.py", "oneshot"),
    "train_size_policy": ("train_size_policy.py", "oneshot"),
    "compute_exec_labels_from_fills": ("compute_exec_labels_from_fills.py", "oneshot"),
    "compute_exec_labels": ("compute_exec_labels.py", "oneshot"),
    "compute_exec_z": ("compute_exec_z.py", "oneshot"),
    "recalibrate_confidence": ("recalibrate_confidence.py", "oneshot"),
    "poll_prices": ("poll_prices.py", "daemon"),
    "ingest_now": ("ingest_now.py", "oneshot"),
    "process_events": ("process_events.py", "oneshot"),
    "label_due_events": ("label_due_events.py", "oneshot"),
    "compute_drift": ("compute_drift.py", "oneshot"),
    "calibrate_price_confidence": ("calibrate_price_confidence.py", "oneshot"),
    "monitor_calibration_health": ("monitor_calibration_health.py", "oneshot"),

    # A.1 supervised embed regressor training (oneshot, idempotent)
    "train_embed_models": ("train_embed_models.py", "oneshot"),
    "train_and_eval_challenger": ("pipeline_train_and_eval.py", "oneshot"),

    # Backtests / scoring
    "backtest_walk_forward": ("backtest_walk_forward.py", "oneshot"),
    "portfolio_backtest": ("portfolio_backtest.py", "oneshot"),

    # Model
    "train_model_v2": ("train_model_v2.py", "oneshot"),
    "validate_now": ("validate_now.py", "oneshot"),

    # Checks
    "check_predictions": ("check_predictions.py", "oneshot"),
    "check_events": ("check_events.py", "oneshot"),
    "check_labels": ("check_labels.py", "oneshot"),
    "check_alerts": ("check_alerts.py", "oneshot"),

    # Portfolio + execution
    "portfolio_rebalance": ("portfolio_rebalance.py", "oneshot"),
    "broker_apply_orders": ("broker_apply_orders.py", "oneshot"),

    # Production preflight (compile + schema + smoke)
    "prod_preflight": ("prod_preflight.py", "oneshot"),
}

PIPELINE_ORDER = [
    "poll_prices",
    "ingest_now",
    "process_events",
    "label_due_events",
    "compute_drift",

    # A.1: train supervised embed models when labels advance (script skips if not needed)
    "train_embed_models",

    "train_model_v2",
    "validate_now",
    "process_events",

    # execution (guarded by AUTO_PIPELINE_INCLUDE_EXECUTION)
    "portfolio_rebalance",
    "broker_apply_orders",
]

# Stable UI ordering (ops “golden” list)
JOB_ORDER = [
    "poll_prices",
    "ingest_now",
    "process_events",
    "label_due_events",
    "compute_drift",
    "post_promotion_monitor",
    "kill_health_monitor",
    "kill_drift_monitor",
    "kill_slippage_monitor",
    "train_embed_models",
    "train_model_v2",
    "validate_now",
    "check_predictions",
    "check_events",
    "check_labels",
    "check_alerts",
    "portfolio_rebalance",
    "portfolio_backtest",
    "broker_apply_orders",
    "backtest_walk_forward",
]

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

_PREFLIGHT_CACHE = {"ok": True, "notes": [], "tables_ok": True, "health_ok": True, "ts_ms": 0}

def _preflight_check_tables() -> tuple[bool, str]:

    try:
        con = _db_connect()
        try:
            rows = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            have = {r[0] for r in rows}
        finally:
            con.close()

        missing = [t for t in PREFLIGHT_REQUIRED_TABLES if t not in have]
        if missing:
            return False, "missing tables: " + ", ".join(missing)
        return True, "tables ok"
    except Exception as e:
        return False, f"table check failed: {e}"

def _bootstrap_prices_if_empty():
    con = _db_connect()
    try:
        row = con.execute("SELECT COUNT(*) FROM prices").fetchone()
        if row and row[0] > 0:
            return

        csv_path = os.path.join("data", "prices.csv")
        if not os.path.exists(csv_path):
            return

        import csv
        with open(csv_path, "r", newline="") as f:
            rdr = csv.DictReader(f)
            rows = [
                (r["ts_ms"], r["symbol"], r["price"])
                for r in rdr
            ]

        con.executemany(
            "INSERT OR REPLACE INTO prices (ts_ms, symbol, price) VALUES (?, ?, ?)",
            rows,
        )
        con.commit()
        print(f"[bootstrap] loaded {len(rows)} prices from prices.csv")
    finally:
        con.close()

def run_preflight() -> Dict:
    global _PREFLIGHT_CACHE
    ts_ms = int(time.time() * 1000)
    out = {"ok": True, "notes": [], "tables_ok": True, "health_ok": True, "ts_ms": ts_ms}

    if not PREFLIGHT_ENABLE:
        out["notes"].append("preflight disabled (PREFLIGHT_ENABLE=0)")
        _PREFLIGHT_CACHE = out
        return out
    _bootstrap_prices_if_empty()

    ok_tables, note_tables = _preflight_check_tables()
    out["tables_ok"] = ok_tables
    out["notes"].append(note_tables)
    if not ok_tables:
        out["ok"] = False

    # health snapshot already checks model + label presence; we add stricter price freshness for ops startup
    try:
        h = get_health_snapshot()
        prices_ok = bool(h.get("prices", {}).get("ok"))
        labels_ok = bool(h.get("labels", {}).get("ok"))
        model_ok  = bool(h.get("model", {}).get("ok"))
        out["health_ok"] = bool(prices_ok and labels_ok and model_ok)

        # stricter startup freshness gate (PREFLIGHT_PRICES_MAX_AGE_S)
        age_s = float(h.get("prices", {}).get("age_s") or 1e9)
        if age_s > PREFLIGHT_PRICES_MAX_AGE_S:
            if os.environ.get("ALLOW_STALE_PRICES", "0") == "1":
                out["notes"].append(
                    f"prices stale but allowed by ALLOW_STALE_PRICES: age_s={age_s:.1f}"
                )
            else:
                out["ok"] = False
                out["notes"].append(
                    f"prices too stale for preflight: age_s={age_s:.1f} > {PREFLIGHT_PRICES_MAX_AGE_S:.1f}"
                )

        else:
            out["notes"].append(f"prices age ok: {age_s:.1f}s")

        if not labels_ok:
            try:
                subprocess.check_call([sys.executable, "compute_exec_labels.py"])
                out["notes"].append("labels bootstrapped")
            except Exception as e:
                out["ok"] = False
                out["notes"].append(f"labels not ok: {e}")

        if not model_ok:
            try:
                subprocess.check_call([sys.executable, "train_size_policy.py"])
                out["notes"].append("model bootstrapped")
            except Exception as e:
                out["ok"] = False
                out["notes"].append(f"model not ok: {e}")
    except Exception as e:
        out["ok"] = False
        out["health_ok"] = False
        out["notes"].append(f"health check failed: {e}")

    _PREFLIGHT_CACHE = out
    return out

def preflight_cached() -> Dict:
    return dict(_PREFLIGHT_CACHE or {})

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
# SQLITE-BASED JOB LOCKS (cross-process safe)
# -------------            -- ------------------------------------------------------

def _ensure_job_locks():
    """
    Cross-process job locks + heartbeats.

    Legacy schema used:
      job_locks(key TEXT PRIMARY KEY, owner TEXT, expires_ms INTEGER)

    Current schema uses:
      job_name, owner, pid, acquired_ts_ms, heartbeat_ts_ms (+ optional expires_ms)

    Safe to call repeatedly:
      - creates table if missing
      - migrates legacy schema if detected
      - adds missing columns via ALTER TABLE (best-effort)
    """
    con = _db_connect()
    try:
        # Detect existing schema (if table missing PRAGMA returns [])
        try:
            cols = [r[1] for r in con.execute("PRAGMA table_info(job_locks)").fetchall()]
        except Exception:
            cols = []

        has_legacy_key = ("key" in cols) and ("job_name" not in cols)

        if has_legacy_key:
            # Migrate legacy schema -> new schema
            try:
                con.execute("ALTER TABLE job_locks RENAME TO job_locks_legacy")
            except Exception:
                pass
            cols = []

        # Ensure base table exists
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS job_locks (
              job_name TEXT PRIMARY KEY,
              owner TEXT NOT NULL,
              pid INTEGER NOT NULL,
              acquired_ts_ms INTEGER NOT NULL,
              heartbeat_ts_ms INTEGER NOT NULL,
              expires_ms INTEGER
            )
            """
        )

        # Copy legacy rows best-effort (pid unknown -> 0, acquired/heartbeat -> now)
        if has_legacy_key:
            now = int(time.time() * 1000)
            try:
                legacy_rows = con.execute(
                    "SELECT key, owner, expires_ms FROM job_locks_legacy"
                ).fetchall()
            except Exception:
                legacy_rows = []

            for k, owner, exp in legacy_rows or []:
                con.execute(
                    """
                    INSERT OR REPLACE INTO job_locks
                    (job_name, owner, pid, acquired_ts_ms, heartbeat_ts_ms, expires_ms)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (
                        str(k),
                        str(owner or ""),
                        0,
                        int(now),
                        int(now),
                        int(exp) if exp is not None else None,
                    ),
                )

        # Add missing columns (idempotent best-effort)
        try:
            cols = [r[1] for r in con.execute("PRAGMA table_info(job_locks)").fetchall()]
        except Exception:
            cols = []

        def _add(col: str, ddl: str) -> None:
            if col in cols:
                return
            try:
                con.execute(ddl)
            except Exception:
                pass

        _add("job_name", "ALTER TABLE job_locks ADD COLUMN job_name TEXT")
        _add("owner", "ALTER TABLE job_locks ADD COLUMN owner TEXT")
        _add("pid", "ALTER TABLE job_locks ADD COLUMN pid INTEGER")
        _add("acquired_ts_ms", "ALTER TABLE job_locks ADD COLUMN acquired_ts_ms INTEGER")
        _add("heartbeat_ts_ms", "ALTER TABLE job_locks ADD COLUMN heartbeat_ts_ms INTEGER")
        _add("expires_ms", "ALTER TABLE job_locks ADD COLUMN expires_ms INTEGER")

        con.commit()
    finally:
        con.close()


def _acquire_lock(name: str, ttl_ms: int = 10_000) -> bool:
    """Acquire a best-effort cross-process lock with TTL."""
    con = _db_connect()
    try:
        now = int(time.time() * 1000)
        exp = int(now + int(ttl_ms))

        row = con.execute(
            "SELECT owner, pid, expires_ms FROM job_locks WHERE job_name=?",
            (str(name),),
        ).fetchone()

        if row:
            try:
                cur_exp = int(row[2] or 0)
            except Exception:
                cur_exp = 0
            # lock still valid
            if cur_exp > now:
                return False

        con.execute(
            """
            INSERT OR REPLACE INTO job_locks
              (job_name, owner, pid, acquired_ts_ms, heartbeat_ts_ms, expires_ms)
            VALUES (?,?,?,?,?,?)
            """,
            (str(name), str(os.getpid()), int(os.getpid()), int(now), int(now), int(exp)),
        )
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def _touch_lock(name: str, ttl_ms: int = 10_000) -> None:
    """Extend TTL of an existing lock (best-effort)."""
    con = _db_connect()
    try:
        now = int(time.time() * 1000)
        exp = int(now + int(ttl_ms))
        con.execute(
            "UPDATE job_locks SET expires_ms=? WHERE job_name=?",
            (int(exp), str(name)),
        )
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()


def _heartbeat_lock(job_name: str, ttl_ms: int = 60_000) -> None:
    """Heartbeat a held lock (best-effort)."""
    _touch_lock(job_name, ttl_ms=ttl_ms)

    _ensure_job_locks()
    now = int(time.time() * 1000)
    owner = f"{os.getpid()}:{threading.get_ident()}"
    pid = int(os.getpid())

    con = _db_connect()
    try:
        # Prefer newer schema if present
        try:
            cols = [r[1] for r in con.execute("PRAGMA table_info(job_locks)").fetchall() or []]
        except Exception:
            cols = []

        if "heartbeat_ts_ms" in cols:
            con.execute(
                "UPDATE job_locks SET heartbeat_ts_ms=?, owner=?, pid=? WHERE job_name=?",
                (int(now), str(owner), int(pid), str(job_name)),
            )
        else:
            # Fallback: touch acquired_ts_ms
            if "acquired_ts_ms" in cols:
                con.execute(
                    "UPDATE job_locks SET acquired_ts_ms=?, owner=?, pid=? WHERE job_name=?",
                    (int(now), str(owner), int(pid), str(job_name)),
                )

        con.commit()
    finally:
        con.close()
def _release_lock(job_name: str) -> None:
    _ensure_job_locks()
    con = _db_connect()
    try:
        con.execute("DELETE FROM job_locks WHERE job_name=?", (str(job_name),))
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

# -------------            -- ------------------------------------------------------
# JOB HISTORY (server-side persistence)
# -------------            -- ------------------------------------------------------

def _ensure_job_history():
    con = _db_connect()
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS job_history (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts_ms INTEGER NOT NULL,
              job_name TEXT NOT NULL,
              event TEXT NOT NULL,
              detail TEXT,
              exit_code INTEGER
            )
            """
        )
        con.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_job_history_job_ts
              ON job_history(job_name, ts_ms)
            """
        )
        con.commit()
    finally:
        con.close()

def _write_job_history(
    job_name: str,
    event: str,
    detail: str = "",
    exit_code: int = None,
    ts_ms: int = None,
) -> None:
    """Append a compact job history row (best-effort)."""
    try:
        _ensure_job_history()
    except Exception:
        pass

    con = _db_connect()
    try:
        now = int(ts_ms or (time.time() * 1000))
        con.execute(
            """
            INSERT INTO job_history(ts_ms, job_name, event, detail, exit_code)
            VALUES (?,?,?,?,?)
            """,
            (
                int(now),
                str(job_name or ""),
                str(event or ""),
                str(detail or ""),
                (int(exit_code) if exit_code is not None else None),
            ),
        )

        # Best-effort pruning (keep latest N rows total)
        try:
            max_rows = int(os.environ.get("JOB_HISTORY_MAX_ROWS", "20000"))
        except Exception:
            max_rows = 20000

        if max_rows > 0:
            con.execute(
                "DELETE FROM job_history WHERE id NOT IN (SELECT id FROM job_history ORDER BY ts_ms DESC LIMIT ?)",
                (int(max_rows),),
            )

        con.commit()
    finally:
        con.close()


def _read_job_history(job_name: str, limit: int = 200) -> list:
    """Read recent job history rows for a job."""
    _ensure_job_history()
    con = _db_connect()
    try:
        rows = con.execute(
            """
            SELECT ts_ms, event, detail, exit_code
            FROM job_history
            WHERE job_name=?
            ORDER BY ts_ms DESC
            LIMIT ?
            """,
            (str(job_name or ""), int(limit)),
        ).fetchall()
        out = []
        for ts_ms, event, detail, exit_code in rows or []:
            out.append(
                {
                    "ts_ms": int(ts_ms or 0),
                    "event": str(event or ""),
                    "detail": str(detail or ""),
                    "exit_code": (int(exit_code) if exit_code is not None else None),
                }
            )
        return out
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


# -------------            -- ------------------------------------------------------
# JOB STATE
# -------------            -- ------------------------------------------------------

class JobState:
    def __init__(self, name: str, script: str, mode: str):
        self.name = name
        self.script = script
        self.mode = mode
        self.proc: Optional[subprocess.Popen] = None
        self.started_at_ms: Optional[int] = None
        self.exited_at_ms: Optional[int] = None
        self.exit_code: Optional[int] = None
        self.log: Deque[str] = deque(maxlen=4000)
        self._lock = threading.Lock()

        # auto-restart guards (daemon only)
        self.stop_requested: bool = False
        self.restart_attempts_window: Deque[int] = deque(maxlen=50)  # ts_ms of restarts (rolling)
        self.next_restart_ms: int = 0
        self.last_start_args: Optional[list] = None
        self.last_start_cwd: Optional[str] = None

    def to_dict(self) -> Dict:
        with self._lock:
            running = self.proc is not None and self.proc.poll() is None
            return {
                "name": self.name,
                "script": self.script,
                "mode": self.mode,
                "running": bool(running),
                "started_at_ms": self.started_at_ms,
                "exited_at_ms": self.exited_at_ms,
                "exit_code": self.exit_code,
                "log_lines": len(self.log),
                # auto-restart visibility (additive)
                "stop_requested": bool(self.stop_requested),
                "next_restart_ms": int(self.next_restart_ms or 0),
            }

    def append_log(self, line: str) -> None:
        with self._lock:
            self.log.append(line.rstrip("\n"))

    def tail(self, n: int) -> str:
        with self._lock:
            if n <= 0:
                return ""
            return "\n".join(list(self.log)[-n:])


# -------------            -- ------------------------------------------------------
# JOB MANAGER
# -------------            -- ------------------------------------------------------
def api_get_embed_model_eval(parsed):
    return {"ok": False, "error": "not_implemented"}

def api_get_embed_conf_calib(parsed):
    return {"ok": False, "error": "not_implemented"}

def api_get_jobs(parsed):
    return {"ok": True, "jobs": JOBS.list_jobs()}

def api_post_job_start(_parsed, body):
    name = body.get("name")
    return JOBS.start(name)

def api_post_job_stop(_parsed, body):
    name = body.get("name")
    return JOBS.stop(name)

class JobManager:
    def __init__(self):
        self._jobs: Dict[str, JobState] = {
            name: JobState(name, script, mode)
            for name, (script, mode) in ALLOWED_JOBS.items()
        }
        self._lock = threading.Lock()

        # daemon watchdog thread (auto-restart guards)
        self._watchdog_started = False
        self._start_watchdog_once()

    def _start_watchdog_once(self):
        if self._watchdog_started:
            return
        self._watchdog_started = True
        t = threading.Thread(target=self._daemon_watchdog_loop, daemon=True)
        t.start()

    def list_jobs(self):
        with self._lock:
            # stable ops ordering, then any extras alphabetically
            out = []
            seen = set()

            for name in JOB_ORDER:
                if name in self._jobs:
                    out.append(self._jobs[name].to_dict())
                    seen.add(name)

            for name in sorted(self._jobs):
                if name in seen:
                    continue
                out.append(self._jobs[name].to_dict())

            return out

    def get(self, name: str) -> Optional[JobState]:
        with self._lock:
            return self._jobs.get(name)

    def start(self, name: str) -> Dict:
        job = self.get(name)
        if not job:
            return {"ok": False, "error": f"unknown job: {name}"}

        # Safe startup gating (ops checklist)
        if PREFLIGHT_ENABLE and PREFLIGHT_BLOCK_JOBS:
            p = run_preflight()
            if not p.get("ok"):
                return {"ok": False, "error": "preflight_failed", "notes": p.get("notes", [])}

        with job._lock:

            # operator intent: starting clears stop_requested
            job.stop_requested = False

            if job.proc and job.proc.poll() is None:
                return {"ok": True, "status": "already_running"}

            if job.mode == "daemon":
                for j in self._jobs.values():
                    if j is not job and j.mode == "daemon" and j.proc and j.proc.poll() is None:
                        return {"ok": False, "error": f"daemon already running: {j.name}"}

            if job.mode == "oneshot":
                if not _acquire_lock(f"job:{job.name}", ttl_ms=10 * 60 * 1000):
                    return {"ok": False, "error": f"job locked: {job.name}"}

            job.exited_at_ms = None
            job.exit_code = None

            py = sys.executable
            args = [py, "-u", job.script]

            if not os.path.exists(job.script):
                if job.mode == "oneshot":
                    _release_lock(f"job:{job.name}")
                job.append_log(f"[server] script not found: {job.script}")
                _write_job_history(job.name, "start_failed", f"script not found: {job.script}", None)
                return {"ok": False, "error": f"script not found: {job.script}"}

            job.append_log(f"[server] starting: {args}")
            _write_job_history(job.name, "start", f"{args}", None)

            job.started_at_ms = int(time.time() * 1000)
            job.last_start_args = list(args)
            job.last_start_cwd = os.getcwd()

            job.proc = subprocess.Popen(
                args,
                cwd=os.getcwd(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0),
            )

            # update job lock heartbeat on successful start
            try:
                con_hb = _db_connect()
                con_hb.execute(
                    "UPDATE job_locks SET heartbeat_ts_ms=? WHERE job_name=?",
                    (int(time.time() * 1000), f"job:{job.name}"),
                )
                con_hb.commit()
            finally:
                try:
                    con_hb.close()
                except Exception:
                    pass

            threading.Thread(target=self._pump_output, args=(job,), daemon=True).start()
            return {"ok": True, "status": "started"}

    def stop(self, name: str) -> Dict:
        job = self.get(name)
        if not job:
            return {"ok": False, "error": f"unknown job: {name}"}

        with job._lock:
            # operator intent: stop disables auto-restart for this job until start() is called
            job.stop_requested = True
            job.next_restart_ms = 0

            if not job.proc or job.proc.poll() is not None:
                _write_job_history(job.name, "stop", "not_running", None)
                return {"ok": True, "status": "not_running"}

            job.append_log("[server] stopping...")
            _write_job_history(job.name, "stop", "terminate()", None)

            try:
                job.proc.terminate()
            except Exception as e:
                job.append_log(f"[server] terminate error: {e}")
                _write_job_history(job.name, "stop_failed", str(e), None)
                return {"ok": False, "error": str(e)}

        return {"ok": True, "status": "terminate_sent"}

    def stop_all(self) -> Dict:
        stopped = []
        errors = []

        with self._lock:
            jobs = list(self._jobs.values())

        # first: request stop (disables auto-restart)
        for job in jobs:
            try:
                self.stop(job.name)
                stopped.append(job.name)
            except Exception as e:
                errors.append(f"{job.name}: {e}")

        # second: wait briefly, then hard-kill if needed
        deadline = time.time() + 3.0
        for job in jobs:
            try:
                with job._lock:
                    p = job.proc
                if not p:
                    continue
                while time.time() < deadline:
                    if p.poll() is not None:
                        break
                    time.sleep(0.05)
                if p.poll() is None:
                    try:
                        p.kill()
                        with job._lock:
                            job.append_log("[server] hard-kill (kill())")
                            _write_job_history(job.name, "stop_hard_kill", "kill()", None)
                    except Exception as e:
                        errors.append(f"{job.name}: kill failed: {e}")
            except Exception as e:
                errors.append(f"{job.name}: wait/kill error: {e}")

        return {"ok": len(errors) == 0, "stopped": stopped, "errors": errors}

    def _pump_output(self, job: JobState):
        proc = job.proc
        if not proc or not proc.stdout:
            return
        try:
            for line in proc.stdout:
                if not line:
                    break
                job.append_log(line)
        except Exception as e:
            job.append_log(f"[server] log pump error: {e}")
        finally:
            try:
                rc = proc.poll()
                if rc is None:
                    rc = proc.wait(timeout=1)
            except Exception:
                rc = proc.poll()

            with job._lock:
                job.exited_at_ms = int(time.time() * 1000)
                job.exit_code = int(rc) if rc is not None else None
                job.append_log(f"[server] exited rc={job.exit_code}")
                _write_job_history(job.name, "exit", "process exited", job.exit_code)

            if job.mode == "oneshot":
                _release_lock(f"job:{job.name}")

    # -------------            -- ------------------------------------------------------
    # DAEMON WATCHDOG (auto-restart guards)
    # -------------            -- ------------------------------------------------------
    def _daemon_watchdog_loop(self):
        while True:
            try:
                if AUTO_RESTART_DAEMONS:
                    self._check_and_restart_daemons()
            except Exception:
                pass
            time.sleep(DAEMON_WATCHDOG_PERIOD_S)

    def _check_and_restart_daemons(self):
        now = int(time.time() * 1000)
        with self._lock:
            jobs = list(self._jobs.values())

        for job in jobs:
            if job.mode != "daemon":
                continue

            with job._lock:
                # if operator stopped it, never restart
                if job.stop_requested:
                    continue

                # if running, refresh heartbeat and continue
                if _is_job_running(job.name):
                    try:
                        _heartbeat_lock(f"job:{job.name}")
                    except Exception:
                        pass
                    continue

                # if never started, don't auto-start (guard: only auto-restart after at least one start)
                if not job.started_at_ms:
                    continue

                # backoff timer
                if job.next_restart_ms and now < job.next_restart_ms:
                    continue

                # rolling window guard
                window_start = now - (DAEMON_RESTART_WINDOW_S * 1000)
                while job.restart_attempts_window and job.restart_attempts_window[0] < window_start:
                    job.restart_attempts_window.popleft()

                if len(job.restart_attempts_window) >= DAEMON_RESTART_MAX_IN_WINDOW:
                    job.append_log(
                        f"[server] auto-restart disabled: too many restarts in {DAEMON_RESTART_WINDOW_S}s"
                    )
                    _write_job_history(
                        job.name,
                        "autorestart_blocked",
                        f"too many restarts in {DAEMON_RESTART_WINDOW_S}s",
                        job.exit_code,
                    )
                    # require operator to explicitly start again
                    job.stop_requested = True
                    continue

                # compute backoff based on recent attempts
                attempt_n = len(job.restart_attempts_window)
                delay = DAEMON_RESTART_BASE_DELAY_MS * (2 ** attempt_n)
                delay = min(int(delay), int(DAEMON_RESTART_MAX_DELAY_MS))
                job.next_restart_ms = now + delay

                job.append_log(f"[server] daemon crashed; scheduling restart in {delay}ms")
                _write_job_history(job.name, "autorestart_scheduled", f"delay_ms={delay}", job.exit_code)

            # perform restart outside job lock to avoid long holding, but keep correctness
            time.sleep(delay / 1000.0)

            with job._lock:
                # re-check before restart (operator might have stopped)
                if job.stop_requested:
                    continue
                if _is_job_running(job.name):
                    continue

            # restart via start() to reuse all semantics and logging
            res = self.start(job.name)
            if not res.get("ok"):
                with job._lock:
                    job.append_log(f"[server] auto-restart failed: {res.get('error')}")
                    _write_job_history(job.name, "autorestart_failed", str(res.get("error") or ""), job.exit_code)
                continue

            with job._lock:
                job.restart_attempts_window.append(int(time.time() * 1000))
                job.next_restart_ms = 0
                job.append_log("[server] auto-restart: started")
                _write_job_history(job.name, "autorestart_started", "started", None)


JOBS = JobManager()

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

_HTTPD = None  # set in run_server()

# -------------            -- ------------------------------------------------------
# PIPELINE
# -------------            -- ------------------------------------------------------

def run_pipeline():
    if not _acquire_lock("pipeline", ttl_ms=20 * 60 * 1000):
        return {"ok": False, "error": "pipeline locked (already running?)"}

    try:
        if not _is_job_running("poll_prices"):
            return {"ok": False, "error": "poll_prices must be running before pipeline"}

        for name in PIPELINE_ORDER:
            # execution is opt-in only
            if name in ("portfolio_rebalance", "broker_apply_orders") and not AUTO_PIPELINE_INCLUDE_EXECUTION:
                continue

            job = JOBS.get(name)
            if not job or job.mode == "daemon":
                continue

            # execution ordering guard
            if name == "broker_apply_orders":
                pr = JOBS.get("portfolio_rebalance")
                if not pr or not pr.exited_at_ms:
                    return {"ok": False, "error": "broker_apply_orders requires portfolio_rebalance first"}

            res = JOBS.start(name)
            if not res.get("ok"):
                return {"ok": False, "error": f"{name}: {res.get('error')}"}

            # wait for oneshot completion
            while True:
                time.sleep(0.2)
                if not job.proc:
                    break
                if job.proc.poll() is not None:
                    if job.exit_code not in (0, None):
                        return {"ok": False, "error": f"{name} exited rc={job.exit_code}"}
                    break

        return {"ok": True}

    finally:
        _release_lock("pipeline")

def api_post_pipeline_run(_parsed, _body):
    return run_pipeline()

# -------------            -- ------------------------------------------------------
# A.1 AUTO PIPELINE LOOP (NEW)
# -------------            -- ------------------------------------------------------

def _is_job_running(name: str) -> bool:
    """
    Canonical definition of 'running':
    - process exists
    - poll() == None
    """
    j = JOBS.get(name)
    if not j:
        return False
    p = j.proc
    if not p:
        return False
    try:
        return p.poll() is None
    except Exception:
        return False

def _auto_pipeline_loop():
    # small delay to allow server startup to finish
    time.sleep(max(0.0, float(AUTO_PIPELINE_START_DELAY_S)))

    while True:
        try:
            # Ensure poll_prices is running (required by run_pipeline)
            if not _is_job_running("poll_prices"):
                res = JOBS.start("poll_prices")
                if AUTO_PIPELINE_LOG:
                    print("[auto_pipeline] poll_prices start:", res)

            # run_pipeline has its own cross-process lock and safety checks
            res = run_pipeline()
            if AUTO_PIPELINE_LOG:
                print("[auto_pipeline] run_pipeline:", res)

        except Exception as e:
            if AUTO_PIPELINE_LOG:
                print("[auto_pipeline] ERROR:", str(e))

        # sleep until next tick
        try:
            time.sleep(max(5.0, float(AUTO_PIPELINE_INTERVAL_S)))
        except Exception:
            time.sleep(60.0)

def _max_drift_ratio() -> float:
    con = _db_connect()
    try:
        try:
            r = con.execute("SELECT MAX(drift_ratio) FROM model_drift").fetchone()
            return float(r[0] or 0.0) if r else 0.0
        except Exception:
            return 0.0
    finally:
        con.close()


def _run_challenger_job_wait() -> dict:
    if not _acquire_lock("challenger", ttl_ms=30 * 60 * 1000):
        return {"ok": False, "error": "challenger locked (already running?)"}

    try:
        res = JOBS.start("train_and_eval_challenger")
        if not res.get("ok"):
            return res

        job = JOBS.get("train_and_eval_challenger")
        # wait for oneshot completion
        while True:
            time.sleep(0.25)
            if not job or not job.proc:
                break
            if job.proc.poll() is not None:
                if job.exit_code not in (0, None):
                    return {"ok": False, "error": f"train_and_eval_challenger exited rc={job.exit_code}"}
                break

        return {"ok": True}
    finally:
        _release_lock("challenger")


def _auto_challenger_loop():
    time.sleep(max(0.0, float(AUTO_CHALLENGER_START_DELAY_S)))

    while True:
        try:
            if AUTO_CHALLENGER_MIN_DRIFT > 0.0:
                md = _max_drift_ratio()
                if md < AUTO_CHALLENGER_MIN_DRIFT:
                    if AUTO_CHALLENGER_LOG:
                        print(f"[auto_challenger] skip drift_gate max_drift={md:.3f} < {AUTO_CHALLENGER_MIN_DRIFT:.3f}")
                else:
                    if AUTO_CHALLENGER_LOG:
                        print(f"[auto_challenger] running drift_gate max_drift={md:.3f}")
                    out = _run_challenger_job_wait()
                    if AUTO_CHALLENGER_LOG:
                        print("[auto_challenger] result:", out)
            else:
                out = _run_challenger_job_wait()
                if AUTO_CHALLENGER_LOG:
                    print("[auto_challenger] result:", out)

        except Exception as e:
            if AUTO_CHALLENGER_LOG:
                print("[auto_challenger] ERROR:", str(e))

        try:
            time.sleep(max(30.0, float(AUTO_CHALLENGER_INTERVAL_S)))
        except Exception:
            time.sleep(3600.0)

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
        from dev_core.exec_conf_calibration import get_latest_exec_conf_calib
        return get_latest_exec_conf_calib()
    except Exception as e:
        return {"ok": False, "error": str(e)}


# -------------            -- ------------------------------------------------------
# DIAGNOSTICS / METRICS

# -------------            -- ------------------------------------------------------

def rollback_champion():
    try:
        from dev_core.model_registry import rollback_champion as _rb
        from dev_core.promotion_audit import audit as _audit
        ch_before = None
        try:
            from dev_core.model_registry import get_stage_latest as _get
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
    return rollback_champion()

def get_promotion_status():
    try:
        from dev_core.promotion_guard import promotion_allowed
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
        from dev_core.model_registry import list_recent
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
        from dev_core.promotion_guard import set_guard
        from dev_core.promotion_audit import audit as _audit
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

def get_health_snapshot():
    """
    Old behavior preserved:
      out["prices"] = {"ok": ..., "age_s": ...}
      out["labels"] = {"ok": ..., "count": ...}
      out["model"]  = {"ok": ..., "support_n": ...}

    NEW additive:
      out["_details"] contains explanation strings and thresholds.
    """

    con = _db_connect()
    try:
        out = {}
        details = {
            "thresholds": {
                "prices_max_age_s": HEALTH_PRICES_MAX_AGE_S,
                "events_max_age_s": HEALTH_EVENTS_MAX_AGE_S,
                "predictions_max_age_s": HEALTH_PREDICTIONS_MAX_AGE_S,
                "jobs_max_stale_s": HEALTH_JOBS_MAX_STALE_S,
                "min_labels": HEALTH_MIN_LABELS,
                "min_model_support": HEALTH_MIN_MODEL_SUPPORT,
            },
            "notes": {},
        }

        now_ms = int(time.time() * 1000)

        # prices freshness
        try:
            row = con.execute("SELECT MAX(ts_ms) FROM prices").fetchone()
        except Exception as e:
            row = None
            details["notes"]["prices"] = f"prices query failed: {e}"

        if row and row[0]:
            age_s = (now_ms - int(row[0])) / 1000.0
            ok = age_s < HEALTH_PRICES_MAX_AGE_S
            out["prices"] = {"ok": ok, "age_s": round(age_s, 1)}
            if ok:
                details["notes"]["prices"] = "OK: prices updated recently"
            else:
                details["notes"]["prices"] = (
                    f"STALE: last price update is {round(age_s,1)}s ago; "
                    f"expected < {HEALTH_PRICES_MAX_AGE_S}s. "
                    f"Check poll_prices job and upstream price source."
                )
        else:
            out["prices"] = {"ok": False, "age_s": None}
            details["notes"]["prices"] = (
                "MISSING: no rows in prices. Start poll_prices and confirm it is writing into the DB."
            )

        # events freshness
        try:
            row = con.execute("SELECT MAX(ts_ms) FROM events").fetchone()
        except Exception as e:
            row = None
            details["notes"]["events"] = f"events query failed: {e}"

        if row and row[0]:
            age_s = (now_ms - int(row[0])) / 1000.0
            ok = age_s < HEALTH_EVENTS_MAX_AGE_S
            out["events"] = {"ok": ok, "age_s": round(age_s, 1)}
            if ok:
                details["notes"]["events"] = "OK: events updated recently"
            else:
                details["notes"]["events"] = (
                    f"STALE: last event ts is {round(age_s,1)}s ago; "
                    f"expected < {HEALTH_EVENTS_MAX_AGE_S}s. "
                    f"Check ingest_now and RSS sources."
                )
        else:
            out["events"] = {"ok": False, "age_s": None}
            details["notes"]["events"] = (
                "MISSING: no rows in events. Run ingest_now and confirm it is writing into the DB."
            )

        # labels count
        try:
            row = con.execute("SELECT COUNT(*) FROM labels").fetchone()
            label_n = int(row[0] or 0)
            ok = label_n >= HEALTH_MIN_LABELS
            out["labels"] = {"ok": ok, "count": label_n}
            if ok:
                details["notes"]["labels"] = "OK: enough labeled examples exist"
            else:
                details["notes"]["labels"] = (
                    f"LOW: labels count={label_n}; expected >= {HEALTH_MIN_LABELS}. "
                    f"Run label_due_events / validate_now and confirm labels table is populated."
                )
        except Exception as e:
            out["labels"] = {"ok": False, "count": 0}
            details["notes"]["labels"] = f"labels query failed: {e}"

        # model support
        try:
            row = con.execute("SELECT SUM(n) FROM model_stats_regime").fetchone()
            model_n = int(row[0] or 0)
            ok = model_n >= HEALTH_MIN_MODEL_SUPPORT
            out["model"] = {"ok": ok, "support_n": model_n}
            if ok:
                details["notes"]["model"] = "OK: model has enough support"
            else:
                details["notes"]["model"] = (
                    f"LOW: model support_n={model_n}; expected >= {HEALTH_MIN_MODEL_SUPPORT}. "
                    f"Run train_model_v2 and confirm model_stats_regime has rows."
                )
        except Exception as e:
            out["model"] = {"ok": False, "support_n": 0}
            details["notes"]["model"] = f"model query failed: {e}"

        # predictions freshness
        try:
            row = con.execute("SELECT MAX(ts_ms) FROM predictions").fetchone()
        except Exception as e:
            row = None
            details["notes"]["predictions"] = f"predictions query failed: {e}"

        if row and row[0]:
            age_s = (now_ms - int(row[0])) / 1000.0
            ok = age_s < HEALTH_PREDICTIONS_MAX_AGE_S
            out["predictions"] = {"ok": ok, "age_s": round(age_s, 1)}
            if ok:
                details["notes"]["predictions"] = "OK: predictions updated recently"
            else:
                details["notes"]["predictions"] = (
                    f"STALE: last prediction ts is {round(age_s,1)}s ago; "
                    f"expected < {HEALTH_PREDICTIONS_MAX_AGE_S}s. "
                    f"Check process_events and predictor pipeline."
                )
        else:
            out["predictions"] = {"ok": False, "age_s": None}
            details["notes"]["predictions"] = (
                "MISSING: no rows in predictions. Run process_events and confirm it is writing into the DB."
            )

        # job locks / heartbeats (requires storage.py Patch 1)
        try:
            rows = con.execute(
                """
                SELECT job_name, owner, pid, acquired_ts_ms, heartbeat_ts_ms
                FROM job_locks
                ORDER BY job_name
                """
            ).fetchall()
        except Exception as e:
            rows = []
            details["notes"]["jobs"] = f"job_locks query failed: {e}"

        jobs = []
        any_stale = False
        for job_name, owner, pid, acquired_ts_ms, heartbeat_ts_ms in rows:
            hb_age_s = (now_ms - int(heartbeat_ts_ms or 0)) / 1000.0 if heartbeat_ts_ms else None
            ok = (hb_age_s is not None) and (hb_age_s < HEALTH_JOBS_MAX_STALE_S)
            if not ok:
                any_stale = True
            jobs.append(
                {
                    "job_name": str(job_name),
                    "owner": str(owner),
                    "pid": int(pid),
                    "acquired_ts_ms": int(acquired_ts_ms),
                    "heartbeat_ts_ms": int(heartbeat_ts_ms),
                    "heartbeat_age_s": (round(hb_age_s, 1) if hb_age_s is not None else None),
                    "ok": bool(ok),
                }
            )

        out["jobs"] = {"ok": (not any_stale), "locks": jobs}

        if rows:
            if any_stale:
                details["notes"]["jobs"] = (
                    f"STALE: one or more job locks have heartbeat_age_s >= {HEALTH_JOBS_MAX_STALE_S}. "
                    f"Check daemon jobs and ensure they are running (poll_prices) and writing heartbeats."
                )
            else:
                details["notes"]["jobs"] = "OK: job locks present and heartbeats are fresh"
        else:
            details["notes"]["jobs"] = "INFO: no job locks currently present"

        # training status (kill switch visibility)
        try:
            out["training"] = get_training_status()
        except Exception:
            out["training"] = {
                "mode": "unknown",
                "allowed": False,
            }

        # ------            -- ------------------------------------------------------
        # AUTO-PAUSE TRAINING ON CRIT HEALTH
        # ------            -- ------------------------------------------------------
        try:
            # define CRIT as any core subsystem failing
            core_ok = (
                out.get("prices", {}).get("ok")
                and out.get("events", {}).get("ok")
                and out.get("labels", {}).get("ok")
                and out.get("model", {}).get("ok")
            )

            if not core_ok:
                ts = get_training_status()
                if ts.get("allowed"):
                    set_training_mode(
                        "paused",
                        actor="health_guard",
                        reason="auto-pause: CRIT health",
                    )
                    try:
                        _write_job_history(
                            job_name="training_guard",
                            event="auto_pause",
                            detail="CRIT health detected",
                            exit_code=None,
                        )
                    except Exception:
                        pass
        except Exception:
            pass

        # training status (kill switch visibility)
        try:
            out["training"] = get_training_status()
        except Exception:
            out["training"] = {"mode": "unknown", "allowed": False}

        out["_details"] = details
        return out

    finally:
        con.close()

def api_get_health(_parsed):
    return get_health_snapshot()

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

def _auto_rollback_loop():

    bad_streak = 0

    while True:
        try:
            time.sleep(float(os.environ.get("AUTO_ROLLBACK_POLL_S", "30")))

            champ = get_stage_latest(MODEL_NAME, stage="champion")
            if not champ:
                bad_streak = 0
                continue

            champ_rmse = champ.get("rmse")
            if champ_rmse is None:
                bad_streak = 0
                continue

            window = int(os.environ.get("AUTO_ROLLBACK_WINDOW", "100"))
            sustained = int(os.environ.get("AUTO_ROLLBACK_SUSTAINED", "3"))
            rmse_mult = float(os.environ.get("AUTO_ROLLBACK_RMSE_MULT", "1.10"))
            min_n = int(os.environ.get("AUTO_ROLLBACK_MIN_N", "20"))

            conn = connect()
            try:
                rows = conn.execute(
                    """
                    SELECT rmse, n
                    FROM validation_points
                    WHERE model_name = ?
                    ORDER BY ts_ms DESC
                    LIMIT ?
                    """,
                    (MODEL_NAME, window),
                ).fetchall()
            finally:
                conn.close()

            if not rows:
                bad_streak = 0
                continue

            rmse_w = 0.0
            n_tot = 0
            for r in rows:
                if r["rmse"] is None or r["n"] is None:
                    continue
                rmse_w += float(r["rmse"]) * float(r["n"])
                n_tot += int(r["n"])

            if n_tot < min_n:
                bad_streak = 0
                continue

            cur_rmse = rmse_w / max(1, n_tot)

            if cur_rmse >= champ_rmse * rmse_mult:
                bad_streak += 1
            else:
                bad_streak = 0

            if bad_streak >= sustained:
                try:
                    result = rollback_champion()

                    _write_job_history(
                        job_name="auto_rollback",
                        event="rollback",
                        detail=f"rollback executed: {result}",
                        exit_code=None,
                    )

                except Exception:
                    # rollback or logging failure should not crash the loop
                    pass
                finally:
                    bad_streak = 0

        except Exception:
            bad_streak = 0
            continue

from dev_core.model_registry import get_stage_latest
MODEL_NAME = "embed_regressor"

def _detect_sustained_equity_drift(con) -> str:
    """
    Returns: "CRIT", "WARN", or None
    Based on recent equity_drift samples.
    """
    try:
        rows = con.execute(
            """
            SELECT level
            FROM equity_drift
            ORDER BY ts_ms DESC
            LIMIT ?
            """,
            (EQ_DRIFT_SUSTAINED_WINDOW,),
        ).fetchall()
    except Exception:
        return None

    if not rows:
        return None

    levels = [r[0] for r in rows]

    crit_n = sum(1 for l in levels if l == "CRIT")
    warn_n = sum(1 for l in levels if l == "WARN")

    if crit_n >= EQ_DRIFT_SUSTAINED_MIN_CRIT:
        return "CRIT"
    if warn_n >= EQ_DRIFT_SUSTAINED_MIN_WARN:
        return "WARN"

    return None

def _classify_equity_diff(diff_pct: float, diff_abs: float = None):
    if diff_pct is None and diff_abs is None:
        return ("UNKNOWN", "no diff computed")

    ap = abs(float(diff_pct or 0.0))
    aa = abs(float(diff_abs or 0.0))

    if ap >= EQ_DIFF_CRIT_PCT or aa >= EQ_DIFF_CRIT_ABS:
        return ("CRIT", "equity diff exceeds CRIT threshold")
    if ap >= EQ_DIFF_WARN_PCT or aa >= EQ_DIFF_WARN_ABS:
        return ("WARN", "equity diff exceeds WARN threshold")

    return ("OK", "equity diff within tolerance")

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
        try:
            row = con.execute(
                """
                SELECT
                  COUNT(*)        AS n_fills,
                  SUM(slippage)   AS total_slippage,
                  SUM(fees)       AS total_fees,
                  SUM(total_cost) AS total_cost,
                  AVG(slippage)   AS avg_slippage
                FROM broker_fills
                """
            ).fetchone()
        except Exception:
            row = None

        try:
            last = con.execute(
                "SELECT MAX(ts_ms) FROM broker_fills"
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
        now_ms = int(time.time() * 1000)
        day_ms = 24 * 60 * 60 * 1000
        week_ms = 7 * day_ms

        def _q(since_ms):
            try:
                return con.execute(
                    """
                    SELECT
                      COUNT(*)        AS n_fills,
                      SUM(slippage)   AS total_slippage,
                      SUM(fees)       AS total_fees,
                      SUM(total_cost) AS total_cost,
                      AVG(slippage)   AS avg_slippage
                    FROM broker_fills
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
        try:
            rows = con.execute(
                """
                SELECT
                  symbol,
                  COUNT(*)        AS n_fills,
                  SUM(slippage)   AS total_slippage,
                  SUM(fees)       AS total_fees,
                  SUM(total_cost) AS total_cost,
                  AVG(slippage)   AS avg_slippage
                FROM broker_fills
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
        try:
            rows = con.execute(
                """
                SELECT
                  CAST(confidence * 10 AS INTEGER) AS bucket,
                  COUNT(*)        AS n_fills,
                  SUM(total_cost) AS total_cost,
                  AVG(total_cost) AS avg_cost
                FROM broker_fills
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
        from dev_core.validation import get_validation
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
    if not _acquire_lock("train_size_policy", ttl_ms=30 * 60 * 1000):

        return {"ok": False, "error": "train_size_policy locked (already running?)"}
    try:
        return JOBS.start("train_size_policy")
    finally:
        _release_lock("train_size_policy")


def _auto_size_policy_loop():
    time.sleep(max(0.0, float(AUTO_SIZE_POLICY_START_DELAY_S)))
    while True:
        try:
            if AUTO_SIZE_POLICY_LOG:
                print("[auto_size_policy] running train_size_policy")
            out = run_size_policy_job()
            if AUTO_SIZE_POLICY_LOG:
                print("[auto_size_policy] result:", out)
        except Exception as e:
            if AUTO_SIZE_POLICY_LOG:
                print("[auto_size_policy] ERROR:", str(e))
        time.sleep(max(300.0, float(AUTO_SIZE_POLICY_INTERVAL_S)))

# ------------------------------
# ROUTE SPECS (split into files)
# ------------------------------
from api_system import ROUTE_SPECS_SYSTEM

from api_jobs import ROUTE_SPECS_JOBS

from api_ops import ROUTE_SPECS_OPS


ROUTE_SPECS = list(ROUTE_SPECS_SYSTEM) + list(ROUTE_SPECS_JOBS) + list(ROUTE_SPECS_OPS)

def _qs(parsed):
    try:
        q = parse_qs(parsed.query or "", keep_blank_values=True)
        return {k: (v[0] if isinstance(v, list) and v else "") for k, v in q.items()}
    except Exception:
        return {}

def _missing(name: str):
    return {"ok": False, "error": f"handler_unavailable:{name}"}

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

# ------------------------------
# API HANDLER BINDINGS
# ------------------------------
def _qs(parsed):
    try:
        q = parse_qs(parsed.query or "", keep_blank_values=True)
        return {k: (v[0] if isinstance(v, list) and v else "") for k, v in q.items()}
    except Exception:
        return {}


def api_get_kill_switches(parsed):
    return _api_get_kill_switches_impl(parsed, {}) if _api_get_kill_switches_impl else {"ok": False, "error": "kill_switches_unavailable"}


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


API_HANDLERS = {
    # GET
    "api_get_kill_switches": api_get_kill_switches,
    "api_get_health": api_get_health,
    "api_get_jobs": api_get_jobs,
    "api_get_job_log": api_get_job_log,
    "api_get_job_history": api_get_job_history,
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

    # POST
    "api_post_job_start": api_post_job_start,
    "api_post_job_stop": api_post_job_stop,
    "api_post_pipeline_run": api_post_pipeline_run,
    "api_post_rollback": api_post_rollback,
}

class Handler(SimpleHTTPRequestHandler):

    # ------------------------------
    # API ROUTE TABLE (collapsed)
    # ------------------------------
    ROUTES = {(m, p): h for (m, p, h) in ROUTE_SPECS}

    def _normalize_ui_legacy_path(self):
        # Backward-compat: allow /dashboard.html and / -> /ui/dashboard.html
        try:
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/dashboard.html"):
                self.path = "/ui/dashboard.html"
        except Exception:
            pass

    def _read_json_body(self):
        try:
            n = int(self.headers.get("Content-Length") or "0")
        except Exception:
            n = 0
        if n <= 0:
            return None
        try:
            raw = self.rfile.read(n)
        except Exception:
            return None
        try:
            return json.loads(raw.decode("utf-8", errors="replace") or "{}")
        except Exception:
            return None

    def respond_json(self, obj, status=200):
        try:
            data = json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")
        except Exception:
            data = b'{"ok":false,"error":"json_encode_failed"}'
            status = 500

        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _is_localhost_client(self) -> bool:
        try:
            ip = str(self.client_address[0] or "")
            return ip in ("127.0.0.1", "::1")
        except Exception:
            return False

    def _require_mutation_auth(self):
        """
        Mutating endpoints:
        - If DASHBOARD_API_TOKEN is set: require token for ALL clients.
        - Else: allow only localhost.
        """
        token = (DASHBOARD_API_TOKEN or "").strip()
        if token:
            try:
                hdr = (self.headers.get("X-API-Token") or "").strip()
            except Exception:
                hdr = ""
            if hdr == token:
                return None

            try:
                parsed = urlparse(self.path)
                q = parse_qs(parsed.query)
                qtok = (q.get("token") or [""])[0]
            except Exception:
                qtok = ""

            if str(qtok).strip() == token:
                return None

            return {"ok": False, "error": "unauthorized"}

        if self._is_localhost_client():
            return None

        return {"ok": False, "error": "forbidden (localhost only)"}

    def _dispatch(self):
        method = str(self.command or "").upper().strip()
        self._normalize_ui_legacy_path()

        parsed = urlparse(self.path)
        key = (method, parsed.path)
        handler_name = self.ROUTES.get(key)
        if not handler_name:
            if method == "GET":
                return super().do_GET()
            return self.respond_json({"ok": False, "error": "unknown endpoint"}, 404)

        fn = API_HANDLERS.get(handler_name)
        if not fn:
            return self.respond_json({"ok": False, "error": f"handler_missing:{handler_name}"}, 500)

        # auth for POST/PUT/PATCH/DELETE
        if method != "GET":
            auth = self._require_mutation_auth()
            if auth:
                return self.respond_json(auth, 403)

        try:
            if method == "GET":
                return self.respond_json(fn(parsed))
            body = self._read_json_body() or {}
            return self.respond_json(fn(parsed, body))
        except Exception as e:
            return self.respond_json({"ok": False, "error": str(e)}, 500)

    def do_GET(self):
        return self._dispatch()

    def do_POST(self):
        return self._dispatch()

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
        print(f"[fatal] database init failed: {e}", file=sys.stderr)
        raise

    # Optional: auto-rollback watcher (ONLY ONE LOOP)
    try:
        t = threading.Thread(target=_auto_rollback_loop, daemon=True)
        t.start()
    except Exception:
        pass

    # Ensure tables exist early
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
    # ------            -- ------------------------------------------------------
    # PREFLIGHT SNAPSHOT AT BOOT (safe startup checklist)
    # ------            -- ------------------------------------------------------
    try:
        p = run_preflight()
        if not p.get("ok"):
            print("[preflight] FAILED at startup:")
            for note in p.get("notes", []):
                print("  -", note)
        else:
            print("[preflight] OK")
    except Exception as e:
        print(f"[preflight] exception: {e}")

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

    print(f"Dashboard running at http://localhost:{port}/dashboard.html  (or /ui/dashboard.html)")

    if AUTO_PIPELINE:
        print(f"[auto_pipeline] enabled interval_s={AUTO_PIPELINE_INTERVAL_S}")
        threading.Thread(target=_auto_pipeline_loop, daemon=True).start()
    if AUTO_CHALLENGER:
        print(f"[auto_challenger] enabled interval_s={AUTO_CHALLENGER_INTERVAL_S} drift_gate={AUTO_CHALLENGER_MIN_DRIFT}")
        t2 = threading.Thread(target=_auto_challenger_loop, daemon=True)
        t2.start()

    if AUTO_SIZE_POLICY:
        print(f"[auto_size_policy] enabled interval_s={AUTO_SIZE_POLICY_INTERVAL_S}")
        t_sp = threading.Thread(target=_auto_size_policy_loop, daemon=True)
        t_sp.start()

    _HTTPD = HTTPServer((host, int(port)), Handler)

    try:
        _HTTPD.serve_forever()
    finally:
        # best-effort: stop child jobs if the server is exiting
        try:
            JOBS.stop_all()
        except Exception:
            pass
        try:
            _HTTPD.server_close()
        except Exception:
            pass

if __name__ == "__main__":
    run_server()
