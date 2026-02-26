# dev_core/alerts.py

import json
import os
import time
import logging
from typing import Dict, List, Optional, Tuple

from engine.runtime.storage import connect
from engine.execution.trade_attribution_ledger import upsert_from_latest_pnl_attribution_snapshot
from engine.strategy.model_v2 import get_current_regime, get_regime_prior
from engine.strategy.learning import get_global_prior
from engine.strategy.position_sizing import position_from_signal
from engine.strategy.edge_filter import adjust_expected_z_for_costs

# ------            -- ------------------------------------------------------
# Schema
# ------            -- ------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms INTEGER NOT NULL,
  event_title TEXT NOT NULL,
  symbol TEXT NOT NULL,
  horizon_s INTEGER NOT NULL,
  expected_z REAL NOT NULL,
  confidence REAL NOT NULL,
  severity TEXT NOT NULL,
  rule_id TEXT NOT NULL,
  explain_json TEXT,
  dedupe_key TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts_ms);
CREATE INDEX IF NOT EXISTS idx_alerts_sym ON alerts(symbol);
"""

# ------            -- ------------------------------------------------------
# Defaults / thresholds
# ------            -- ------------------------------------------------------

DEFAULT_RULES = [
    {"rule_id": "warn_z1_conf55", "min_abs_z": 1.0, "min_conf": 0.55, "severity": "WARN"},
    {"rule_id": "high_z15_conf60", "min_abs_z": 1.5, "min_conf": 0.60, "severity": "HIGH"},
    {"rule_id": "crit_z2_conf70", "min_abs_z": 2.0, "min_conf": 0.70, "severity": "CRIT"},
]

MIN_SUPPORT_N = int(os.environ.get("ALERT_MIN_SUPPORT_N", "12"))
MIN_VALIDATION_N = int(os.environ.get("ALERT_MIN_VALIDATION_N", "12"))
MAX_RMSE = float(os.environ.get("ALERT_MAX_RMSE", "1.75"))

MAX_DRIFT_RATIO = float(os.environ.get("ALERT_MAX_DRIFT_RATIO", "2.25"))
LOW_CONF_COOLDOWN_MULT = float(os.environ.get("ALERT_LOW_CONF_COOLDOWN_MULT", "2.0"))
LOW_CONF_LEVEL = float(os.environ.get("ALERT_LOW_CONF_LEVEL", "0.60"))

MIN_RELEVANCE = float(os.environ.get("ALERT_MIN_RELEVANCE", "0.35"))

COOLDOWN_WARN_S = int(os.environ.get("ALERT_COOLDOWN_WARN_S", "600"))
COOLDOWN_HIGH_S = int(os.environ.get("ALERT_COOLDOWN_HIGH_S", "1800"))
COOLDOWN_CRIT_S = int(os.environ.get("ALERT_COOLDOWN_CRIT_S", "3600"))

ALERT_DEDUPE_WINDOW_S = int(os.environ.get("ALERT_DEDUPE_WINDOW_S", "300"))

# ------            -- ------------------------------------------------------
# Rate limits (production safety)
# ------            -- ------------------------------------------------------
ALERT_RATE_WINDOW_S = int(os.environ.get("ALERT_RATE_WINDOW_S", "3600"))  # 1h
ALERT_MAX_PER_WINDOW_GLOBAL = int(os.environ.get("ALERT_MAX_PER_WINDOW_GLOBAL", "250"))
ALERT_MAX_PER_WINDOW_PER_SYMBOL = int(os.environ.get("ALERT_MAX_PER_WINDOW_PER_SYMBOL", "40"))

ALERT_SEV_WARN = float(os.environ.get("ALERT_SEV_WARN", "0.75"))
ALERT_SEV_CRIT = float(os.environ.get("ALERT_SEV_CRIT", "1.50"))

ALERT_PLAYBOOKS = {
    # Default playbooks by severity (used when a rule_id-specific playbook is not present)
    "INFO": {
        "summary": "Monitor only. No action required.",
        "steps": [
            "Verify the alert matches expected news/event flow.",
            "No changes needed unless alerts become frequent or drift increases.",
        ],
    },
    "WARN": {
        "summary": "Investigate. Confirm data freshness and model stability; consider reducing sizing.",
        "steps": [
            "Check /api/health for stale prices/events/predictions.",
            "Review drift dashboard and recent validation scores.",
            "If warnings persist, reduce sizing or raise confidence threshold temporarily.",
        ],
    },
    "HIGH": {
        "summary": "Elevated risk. Validate model performance and recent changes before acting.",
        "steps": [
            "Check model metrics and validation for the affected symbol/horizon.",
            "Review recent job history for failures or repeated restarts.",
            "Consider pausing execution for the affected universe until resolved.",
        ],
    },
    "CRIT": {
        "summary": "High risk. Pause execution and investigate immediately.",
        "steps": [
            "Pause/disable execution (broker_apply_orders) until root cause is identified.",
            "Verify data freshness, drift, and recent model promotions/rollbacks.",
            "Resolve the underlying issue, then resume with reduced sizing and close monitoring.",
        ],
    },
}

RULE_PLAYBOOKS = {
    # Rule-specific overrides (keyed by rule_id)
    "EQUITY_RECON": {
        "summary": "Broker vs backtest mismatch. Stop execution and reconcile fills/prices.",
        "steps": [
            "Stop broker_apply_orders to prevent compounding errors.",
            "Open broker snapshot and compare to backtest latest run (timestamps & prices).",
            "Inspect recent fills for abnormal slippage/fees and missing price updates.",
            "After reconciliation, acknowledge the alert and resume with reduced sizing.",
        ],
    },
    "EQUITY_DRIFT_SUSTAINED": {
        "summary": "Sustained equity drift trend. Investigate execution quality and pricing.",
        "steps": [
            "Check execution_metrics (fees/slippage/cost bps) for sudden changes.",
            "Check poll_prices health and ensure pricing source is stable.",
            "If drift persists, pause execution and re-run portfolio_backtest to verify assumptions.",
        ],
    },
}


def _get_playbook(severity: str, rule_id: str = "") -> dict:
    rid = str(rule_id or "").strip()
    if rid and rid in RULE_PLAYBOOKS:
        return dict(RULE_PLAYBOOKS[rid])
    sev = str(severity or "INFO").strip().upper() or "INFO"
    return dict(ALERT_PLAYBOOKS.get(sev) or ALERT_PLAYBOOKS["INFO"])


REGIME_Z_MULT = {"LOW": 0.9, "MID": 1.0, "HIGH": 1.2}

# ------            -- ------------------------------------------------------
# Logging
# ------            -- ------------------------------------------------------

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [alerts] %(message)s",
)

ALERT_LOG_COST_REJECTS = os.environ.get("ALERT_LOG_COST_REJECTS", "1") == "1"

# ------            -- ------------------------------------------------------
# DB init
# ------            -- ------------------------------------------------------

def init_alerts_db() -> None:
    con = connect()
    try:
        con.executescript(SCHEMA)
        con.commit()
    finally:
        con.close()

# ------            -- ------------------------------------------------------
# Helpers
# ------            -- ------------------------------------------------------

def _put_provider_health(con, ts_ms: int, provider: str, ok: int, latency_ms: int, n_symbols: int, error: str = None) -> None:
    con.execute(
        """
        INSERT INTO price_provider_health(ts_ms, provider, ok, latency_ms, n_symbols, error)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(provider, ts_ms) DO UPDATE SET
          ok=excluded.ok,
          latency_ms=excluded.latency_ms,
          n_symbols=excluded.n_symbols,
          error=excluded.error
        """,
        (
            int(ts_ms),
            str(provider),
            int(ok),
            (int(latency_ms) if latency_ms is not None else None),
            int(n_symbols),
            (str(error) if error else None),
        ),
    )

def severity_rank(s: str) -> int:
    return {"INFO": 0, "WARN": 1, "HIGH": 2, "CRIT": 3}.get((s or "").upper(), 0)


def _get_validation_row(symbol: str, horizon_s: int) -> Optional[Tuple[float, float, int]]:
    con = connect()
    try:
        row = con.execute(
            """
            SELECT mae, rmse, n
            FROM validation_scores
            WHERE symbol=? AND horizon_s=?
            """,
            (symbol, horizon_s),
        ).fetchone()
        if not row:
            return None
        return float(row[0]), float(row[1]), int(row[2])
    except Exception:
        return None
    finally:
        con.close()


def _support_n(symbol: str, horizon_s: int) -> int:
    _, reg_n, _ = get_regime_prior(symbol, horizon_s)
    _, glob_n = get_global_prior(symbol, horizon_s)
    return int(reg_n) if int(reg_n) > 0 else int(glob_n)


def _passes_quality_gates(symbol: str, horizon_s: int) -> bool:
    if _support_n(symbol, horizon_s) < MIN_SUPPORT_N:
        return False

    v = _get_validation_row(symbol, horizon_s)
    if not v:
        return False

    _, rmse, n = v
    return n >= MIN_VALIDATION_N and rmse <= MAX_RMSE


def _cooldown_ms(sev: str) -> int:
    return {
        "WARN": COOLDOWN_WARN_S,
        "HIGH": COOLDOWN_HIGH_S,
        "CRIT": COOLDOWN_CRIT_S,
    }.get(sev.upper(), 0) * 1000


def _passes_rate_limit(symbol: str, now_ms: int) -> bool:
    """
    Hard cap on alert volume to prevent runaway alert storms.
    Fail-closed if DB queries fail.
    """
    try:
        win_ms = int(ALERT_RATE_WINDOW_S) * 1000
        since_ms = int(now_ms) - int(win_ms)

        con = connect()
        try:
            # Global
            row = con.execute(
                "SELECT COUNT(*) FROM alerts WHERE ts_ms >= ?",
                (since_ms,),
            ).fetchone()
            global_n = int(row[0] or 0) if row else 0
            if global_n >= int(ALERT_MAX_PER_WINDOW_GLOBAL):
                return False

            # Per-symbol
            row = con.execute(
                "SELECT COUNT(*) FROM alerts WHERE ts_ms >= ? AND symbol = ?",
                (since_ms, str(symbol)),
            ).fetchone()
            sym_n = int(row[0] or 0) if row else 0
            if sym_n >= int(ALERT_MAX_PER_WINDOW_PER_SYMBOL):
                return False

            return True
        finally:
            try:
                con.close()
            except Exception:
                pass
    except Exception:
        return False


def _passes_cooldown(symbol: str, horizon_s: int, severity: str, now_ms: int) -> bool:
    cutoff = now_ms - _cooldown_ms(severity)
    if cutoff <= 0:
        return True

    con = connect()
    try:
        row = con.execute(
            """
            SELECT severity
            FROM alerts
            WHERE symbol=? AND horizon_s=? AND ts_ms>=?
            ORDER BY ts_ms DESC
            LIMIT 1
            """,
            (symbol, horizon_s, cutoff),
        ).fetchone()
    finally:
        con.close()

    if not row:
        return True

    return severity_rank(row[0]) < severity_rank(severity)

# ------            -- ------------------------------------------------------
# Rule selection
# ------            -- ------------------------------------------------------

def choose_rule(
    expected_z: float,
    conf: float,
    symbol: str,
    horizon_s: int,
    rules: Optional[List[Dict]] = None,
) -> Optional[Dict]:

    rules = rules or DEFAULT_RULES
    reg = get_current_regime(symbol) or "MID"
    mult = REGIME_Z_MULT.get(reg, 1.0)

    az = abs(expected_z)
    best = None
    for r in rules:
        if az >= r["min_abs_z"] * mult and conf >= r["min_conf"]:
            if not best or severity_rank(r["severity"]) > severity_rank(best["severity"]):
                best = dict(r)
                best["regime"] = reg
                best["min_abs_z_resolved"] = r["min_abs_z"] * mult
    return best

# ------            -- ------------------------------------------------------
# Emit alert
# ------            -- ------------------------------------------------------
def emit_alert(
    event_title: str,
    symbol: str,
    horizon_s: int,
    expected_z: float,
    confidence: float,
    explain: Optional[Dict] = None,
    rules: Optional[List[Dict]] = None,
) -> Optional[int]:

    init_alerts_db()
    explain = explain or {}
    now_ms = int(time.time() * 1000)

    # ------------------------------------------------------------
    # Informational: market stress context (read-only)
    # ------------------------------------------------------------
    try:
        from engine.market_stress import get_market_stress_snapshot
        ms = get_market_stress_snapshot(ts_ms=now_ms) or {}
        explain["market_stress"] = {
            "score": float(ms.get("stress_score", 0.0)),
            "explain": (
                "elevated market stress"
                if float(ms.get("stress_score", 0.0)) >= 0.7
                else "normal market stress"
            ),
        }
    except Exception:
        explain["market_stress"] = {
            "score": 0.0,
            "explain": "unavailable",
        }

    # price staleness decay
    try:
        con = connect()
        row = con.execute(
            "SELECT meta_json FROM symbols WHERE symbol=?",
            (symbol,),
        ).fetchone()
        con.close()

        if row and row[0]:
            meta = json.loads(row[0])
            ps = meta.get("price_status") or {}
            if ps.get("stale"):
                decay = float(os.environ.get("STALE_PRICE_CONF_DECAY", "0.65"))
                confidence *= decay
                explain["confidence_decay"] = {
                    "reason": "price_stale",
                    "multiplier": decay,
                }
    except Exception:
        pass

    # ------------------------------------------------------------
    # Execution-cost net-edge filter (opt-in)
    # ------------------------------------------------------------
    try:
        adj = adjust_expected_z_for_costs(
            symbol=str(symbol),
            horizon_s=int(horizon_s),
            expected_z=float(expected_z),
            side=1,
        )
        if adj:
            explain["exec_cost_filter"] = {
                "cost_bps": float(adj.get("cost_bps", 0.0)),
                "cost_z": float(adj.get("cost_z", 0.0)),
                "vol_step": float(adj.get("vol_step", 0.0)),
                "vol_horizon": float(adj.get("vol_horizon", 0.0)),
            }
            
            ez_adj = adj.get("expected_z_adj", None)
            # If helper signals rejection it returns NaN
            if ez_adj is not None and ez_adj == ez_adj:
                expected_z = float(ez_adj)
            else:
                explain["exec_cost_reject"] = True
                if ALERT_LOG_COST_REJECTS:
                    try:
                        logging.info(
                            "exec_cost_reject symbol=%s horizon_s=%s expected_z=%s conf=%s",
                            str(symbol),
                            str(horizon_s),
                            str(expected_z),
                            str(confidence),
                        )
                    except Exception:
                        pass
                return None

    except Exception:
        pass

    rule = choose_rule(expected_z, confidence, symbol, horizon_s, rules)
    if not rule:
        return None

    con = connect()
    try:
        dedupe_key = f"{symbol}:{horizon_s}:{rule['rule_id']}"
        con.execute(
            """
            INSERT OR IGNORE INTO alerts
            (ts_ms, event_title, symbol, horizon_s, expected_z, confidence,
             severity, rule_id, explain_json, dedupe_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_ms,
                event_title,
                symbol,
                horizon_s,
                float(expected_z),
                float(confidence),
                rule["severity"],
                rule["rule_id"],
                json.dumps(explain, separators=(",", ":"), sort_keys=True),
                dedupe_key,
            ),
        )
        con.commit()
    finally:
        con.close()

    return now_ms

# ------------------------------------------------------------
# Query
# ------------------------------------------------------------


def get_recent_alerts(limit: int = 50):
    init_alerts_db()
    con = connect()
    try:
        return con.execute(
            """
            SELECT ts_ms, severity, rule_id, event_title,
                   symbol, horizon_s, expected_z, confidence, explain_json
            FROM alerts
            ORDER BY ts_ms DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        con.close()
