"""
Execution Mode (Unified)

Modes:
- paper
- shadow
- live

Live safety:
- Requires mode='live' AND armed=1
- DISABLE_LIVE_EXECUTION=1 always blocks
- Armed persisted and audited
- Provider health gate optional
"""

import os
import time
from typing import Dict, Any, Optional, Tuple

from engine.storage import connect, init_db
from engine.execution_costs import (
    DEFAULT_FEES_BPS,
    DEFAULT_SLIPPAGE_BPS,
    DEFAULT_SPREAD_BPS_CAP,
)

# ============================================================
# Constants
# ============================================================

MODES = ("paper", "shadow", "live")

DEFAULT_MODE = os.environ.get("EXECUTION_MODE_DEFAULT", "paper").strip().lower()
if DEFAULT_MODE not in MODES:
    DEFAULT_MODE = "paper"

DEFAULT_ARMED = 0

_PROVIDER_HEALTH: Dict[str, Any] = {}


# ============================================================
# Helpers
# ============================================================

def _put_provider_health(health: Dict[str, Any]) -> None:
    global _PROVIDER_HEALTH
    if isinstance(health, dict):
        _PROVIDER_HEALTH = dict(health)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _norm_mode(mode: str) -> str:
    m = str(mode or "").strip().lower()
    return m if m in MODES else "paper"


def _has_column(con, table: str, col: str) -> bool:
    try:
        rows = con.execute(f"PRAGMA table_info({table})").fetchall() or []
        for r in rows:
            if str(r[1]) == str(col):
                return True
    except Exception:
        pass
    return False


def _ensure_schema(con) -> None:
    con.executescript(
        """
CREATE TABLE IF NOT EXISTS execution_mode (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  mode TEXT NOT NULL,
  armed INTEGER NOT NULL DEFAULT 0,
  updated_ts_ms INTEGER NOT NULL,
  actor TEXT NOT NULL,
  reason TEXT
);

CREATE TABLE IF NOT EXISTS execution_mode_audit (
  ts_ms INTEGER NOT NULL,
  prev_mode TEXT NOT NULL,
  new_mode TEXT NOT NULL,
  actor TEXT NOT NULL,
  reason TEXT,
  prev_armed INTEGER,
  new_armed INTEGER
);

CREATE INDEX IF NOT EXISTS idx_execution_mode_audit_ts
  ON execution_mode_audit(ts_ms);
"""
    )

    # additive safety in case older DB exists
    if not _has_column(con, "execution_mode", "armed"):
        try:
            con.execute("ALTER TABLE execution_mode ADD COLUMN armed INTEGER NOT NULL DEFAULT 0;")
        except Exception:
            pass


def _ensure_row(con) -> None:
    r = con.execute("SELECT mode FROM execution_mode WHERE id=1").fetchone()
    if not r:
        con.execute(
            "INSERT INTO execution_mode(id, mode, armed, updated_ts_ms, actor, reason) VALUES (1,?,?,?,?,?)",
            (DEFAULT_MODE, DEFAULT_ARMED, _now_ms(), "system", "init"),
        )


# ============================================================
# Public API
# ============================================================

def get_execution_mode(con=None) -> Dict[str, Any]:
    owns = False
    if con is None:
        con = connect()
        owns = True
    try:
        init_db()
        _ensure_schema(con)
        _ensure_row(con)
        r = con.execute(
            "SELECT mode, armed, updated_ts_ms, actor, reason FROM execution_mode WHERE id=1"
        ).fetchone()
        return {
            "mode": str(r[0]),
            "armed": int(r[1] or 0),
            "updated_ts_ms": int(r[2] or 0),
            "actor": str(r[3] or ""),
            "reason": str(r[4] or ""),
        }
    finally:
        if owns:
            con.close()


def set_execution_mode(
    mode: str,
    actor: str = "operator",
    reason: str = "",
    con=None,
    keep_armed: bool = False,
) -> Dict[str, Any]:

    mode = _norm_mode(mode)
    actor = str(actor or "operator")
    reason = str(reason or "")

    owns = False
    if con is None:
        con = connect()
        owns = True

    try:
        init_db()
        _ensure_schema(con)
        _ensure_row(con)

        con.execute("BEGIN IMMEDIATE;")

        prev = con.execute("SELECT mode, armed FROM execution_mode WHERE id=1").fetchone()
        prev_mode = str(prev[0])
        prev_armed = int(prev[1] or 0)

        new_armed = prev_armed
        if not keep_armed:
            if mode != "live":
                new_armed = 0
            if mode == "live":
                new_armed = 0

        con.execute(
            "UPDATE execution_mode SET mode=?, armed=?, updated_ts_ms=?, actor=?, reason=? WHERE id=1",
            (mode, int(new_armed), _now_ms(), actor, reason),
        )

        con.execute(
            """
            INSERT INTO execution_mode_audit(ts_ms, prev_mode, new_mode, actor, reason, prev_armed, new_armed)
            VALUES (?,?,?,?,?,?,?)
            """,
            (_now_ms(), prev_mode, mode, actor, reason, prev_armed, new_armed),
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


def set_execution_armed(
    armed: int,
    actor: str = "operator",
    reason: str = "",
    con=None,
) -> Dict[str, Any]:

    actor = str(actor or "operator")
    reason = str(reason or "")

    owns = False
    if con is None:
        con = connect()
        owns = True

    try:
        init_db()
        _ensure_schema(con)
        _ensure_row(con)

        con.execute("BEGIN IMMEDIATE;")

        cur = con.execute("SELECT mode, armed FROM execution_mode WHERE id=1").fetchone()
        mode = str(cur[0])
        prev_armed = int(cur[1] or 0)

        new_armed = 1 if int(armed) == 1 else 0
        if mode != "live":
            new_armed = 0

        con.execute(
            "UPDATE execution_mode SET armed=?, updated_ts_ms=?, actor=?, reason=? WHERE id=1",
            (new_armed, _now_ms(), actor, reason),
        )

        con.execute(
            """
            INSERT INTO execution_mode_audit(ts_ms, prev_mode, new_mode, actor, reason, prev_armed, new_armed)
            VALUES (?,?,?,?,?,?,?)
            """,
            (_now_ms(), mode, mode, actor, reason, prev_armed, new_armed),
        )

        con.execute("COMMIT;")

        st = get_execution_mode(con)
        return st
    except Exception:
        try:
            con.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        if owns:
            con.close()


def execution_allowed_for_real_trading(
    con=None,
    symbol: Optional[str] = None,
    regime: Optional[str] = None,
) -> Tuple[bool, str, Dict[str, Any]]:

    owns = False
    if con is None:
        con = connect()
        owns = True

    try:
        state = get_execution_mode(con=con)
        mode = str(state.get("mode", "paper"))
        armed = int(state.get("armed", 0))

        if os.environ.get("DISABLE_LIVE_EXECUTION", "0") == "1":
            return False, "env_disable_live", {"mode": mode, "armed": armed}

        if mode != "live":
            return False, "mode_not_live", {"mode": mode, "armed": armed}

        if armed != 1:
            return False, "live_not_armed", {"mode": mode, "armed": armed}

        if _PROVIDER_HEALTH:
            if _PROVIDER_HEALTH.get("ok") is False:
                return False, "provider_health_bad", {
                    "mode": mode,
                    "armed": armed,
                    "provider": _PROVIDER_HEALTH,
                }

        return True, "ok", {"mode": mode, "armed": armed}
    finally:
        if owns:
            con.close()


def get_execution_overlays() -> Dict[str, Any]:
    return {
        "mode": {"default": DEFAULT_MODE},
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
