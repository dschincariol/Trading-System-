"""
Execution Mode (single source of truth).

Modes:
- paper  : simulate only (no real orders)
- shadow : generate intents/logs only
- live   : allow real execution (subject to kill switches & guards)

Persisted in SQLite and audited on every change.
"""

import json
import os
import time
from typing import Dict, Any, Optional

from dev_core.storage import connect, init_db
from dev_core.execution_costs import DEFAULT_FEES_BPS, DEFAULT_SLIPPAGE_BPS, DEFAULT_SPREAD_BPS_CAP

# ------------------------------------------------------------
# Constants
# ------------------------------------------------------------

MODES = ("paper", "shadow", "live")
DEFAULT_MODE = os.environ.get("EXECUTION_MODE_DEFAULT", "paper").strip().lower()
if DEFAULT_MODE not in MODES:
    DEFAULT_MODE = "paper"

# ------------------------------------------------------------
# Schema
# ------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_mode (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  mode TEXT NOT NULL,
  updated_ts_ms INTEGER NOT NULL,
  actor TEXT NOT NULL,
  reason TEXT
);

CREATE TABLE IF NOT EXISTS execution_mode_audit (
  ts_ms INTEGER NOT NULL,
  prev_mode TEXT NOT NULL,
  new_mode TEXT NOT NULL,
  actor TEXT NOT NULL,
  reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_execution_mode_audit_ts
  ON execution_mode_audit(ts_ms);
"""

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)

def _norm_mode(m: str) -> str:
    s = str(m or "").strip().lower()
    if s not in MODES:
        raise ValueError(f"invalid execution mode: {m}")
    return s

def _ensure_schema(con) -> None:
    con.executescript(_SCHEMA)

def _ensure_row(con) -> None:
    row = con.execute("SELECT mode FROM execution_mode WHERE id=1").fetchone()
    if not row:
        con.execute(
            "INSERT INTO execution_mode(id, mode, updated_ts_ms, actor, reason) VALUES (1,?,?,?,?)",
            (DEFAULT_MODE, _now_ms(), "system", "init"),
        )

# ------------------------------------------------------------
# Public API
# ------------------------------------------------------------

def get_execution_mode(con=None) -> Dict[str, Any]:
    """
    Returns:
      { mode, updated_ts_ms, actor, reason }
    """
    owns = False
    if con is None:
        con = connect()
        owns = True
    try:
        init_db()
        _ensure_schema(con)
        _ensure_row(con)
        r = con.execute(
            "SELECT mode, updated_ts_ms, actor, reason FROM execution_mode WHERE id=1"
        ).fetchone()
        return {
            "mode": str(r[0]),
            "updated_ts_ms": int(r[1] or 0),
            "actor": str(r[2] or ""),
            "reason": str(r[3] or ""),
        }
    finally:
        if owns:
            con.close()

def set_execution_mode(
    mode: str,
    actor: str = "system",
    reason: Optional[str] = None,
    con=None,
) -> Dict[str, Any]:
    """
    Sets execution mode with audit.
    """
    m = _norm_mode(mode)
    a = str(actor or "system")
    r = str(reason or "")
    owns = False
    if con is None:
        con = connect()
        owns = True
    try:
        init_db()
        _ensure_schema(con)
        _ensure_row(con)

        con.execute("BEGIN IMMEDIATE;")
        prev = con.execute(
            "SELECT mode FROM execution_mode WHERE id=1"
        ).fetchone()
        prev_mode = str(prev[0]) if prev else DEFAULT_MODE

        if prev_mode != m:
            con.execute(
                "UPDATE execution_mode SET mode=?, updated_ts_ms=?, actor=?, reason=? WHERE id=1",
                (m, _now_ms(), a, r),
            )
            con.execute(
                """
                INSERT INTO execution_mode_audit(ts_ms, prev_mode, new_mode, actor, reason)
                VALUES (?,?,?,?,?)
                """,
                (_now_ms(), prev_mode, m, a, r),
            )

        con.execute("COMMIT;")
        return get_execution_mode(con)
    except Exception:
        try:
            con.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        if owns:
            con.close()

def execution_allowed_for_real_trading(con=None) -> bool:
    """
    True only when mode == 'live'.
    Shadow/paper must never place real orders.
    """
    st = get_execution_mode(con)
    return st.get("mode") == "live"

def get_execution_overlays() -> Dict[str, Any]:
    """
    Read-only snapshot of execution knobs for UI/ops display.
    """
    return {
        "cost_model": {
            "fees_bps": float(DEFAULT_FEES_BPS),
            "slippage_bps": float(DEFAULT_SLIPPAGE_BPS),
            "spread_bps_cap": float(DEFAULT_SPREAD_BPS_CAP),
        },
        "portfolio_exec_realism": {
            "enabled": os.environ.get("PORTFOLIO_USE_EXEC_REALISM", "1") == "1",
            "max_price_age_s": float(os.environ.get("PORTFOLIO_EXEC_MAX_PRICE_AGE_S", "120")),
            "stale_half_factor": float(os.environ.get("PORTFOLIO_EXEC_STALE_HALF_FACTOR", "0.50")),
            "stress_th": float(os.environ.get("PORTFOLIO_EXEC_STRESS_TH", "0.75")),
            "stress_factor": float(os.environ.get("PORTFOLIO_EXEC_STRESS_FACTOR", "0.60")),
        },
    }
