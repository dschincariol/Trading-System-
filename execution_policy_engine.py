# execution_policy_engine.py
"""
Execution Policy Engine (EPE)

Sits between signal generation (portfolio_orders) and broker routing.

Responsibilities:
- Enforce alpha TTL (hard stop) and alpha half-life decay (aggressiveness changes with age)
- Decide order type / aggressiveness
- Slice orders
- Emit full audit trail per trade decision
- Integrate with existing kill-switch / degradation logic (fail-soft: returns [] when blocked)
"""

import time
import json
from typing import List, Dict, Any

from dev_core.storage import connect
from dev_core.kill_switch import execution_allowed


DEFAULT_TTL_MS = 5 * 60 * 1000  # 5 minutes


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ensure_tables(con) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS execution_policy_audit (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts_ms INTEGER NOT NULL,
          signal_id TEXT,
          symbol TEXT,
          side TEXT,
          qty REAL,
          age_ms INTEGER,
          ttl_ms INTEGER,
          volatility REAL,
          source_order_id INTEGER,
          policy_json TEXT NOT NULL
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_epe_audit_ts ON execution_policy_audit(ts_ms)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_epe_audit_sym ON execution_policy_audit(symbol)"
    )


def apply_execution_policy(orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Input: raw portfolio orders (dicts)
    Output: execution-shaped orders (dicts)

    Contract:
    - MUST NOT execute after TTL expiry (hard drop)
    - MUST be auditable (execution_policy_audit row per input order)
    - MUST NOT embed prediction logic (uses only order metadata)
    """
    shaped: List[Dict[str, Any]] = []

    con = connect()
    try:
        _ensure_tables(con)

        # WHERE TO PUT THIS (you asked):
        # right after DB connect / ensure tables, before shaping loop
        allow, ks_reason, ks_meta = execution_allowed(con=con, symbol=None, regime=None)
        if not allow:
            return []

        now_ms = _now_ms()

        for o in list(orders or []):
            if not isinstance(o, dict):
                continue

            signal_ts = int(o.get("signal_ts_ms") or 0)
            ttl_ms = int(o.get("alpha_ttl_ms") or DEFAULT_TTL_MS)

            if signal_ts <= 0:
                continue

            age_ms = int(now_ms - signal_ts)
            if age_ms > ttl_ms:
                # hard stop: never execute expired alpha
                continue

            # Half-life style decay mapped onto aggressiveness by age_ratio
            age_ratio = float(age_ms) / float(ttl_ms) if ttl_ms > 0 else 1.0

            if age_ratio < 0.33:
                order_type = "LIMIT"
                aggressiveness = "PASSIVE"
            elif age_ratio < 0.66:
                order_type = "LIMIT"
                aggressiveness = "NEUTRAL"
            else:
                order_type = "MARKET"
                aggressiveness = "AGGRESSIVE"

            qty = float(o.get("qty") or 0.0)
            if qty == 0.0:
                continue

            symbol = str(o.get("symbol") or "").strip()
            if not symbol:
                continue

            side = str(o.get("side") or "").upper().strip()

            volatility = float(o.get("volatility") or 0.0)
            if volatility > 0.03:
                slice_pct = 0.15
            else:
                slice_pct = 0.25

            slice_qty = abs(qty) * float(slice_pct)
            if slice_qty <= 0.0:
                slice_qty = abs(qty)

            slices = max(1, int(abs(qty) // slice_qty))
            slices = max(1, min(25, slices))  # hard cap to prevent runaway

            source_order_id = o.get("source_order_id")
            try:
                source_order_id_i = int(source_order_id) if source_order_id is not None else None
            except Exception:
                source_order_id_i = None

            for _ in range(slices):
                shaped.append(
                    {
                        **o,
                        "order_type": order_type,
                        "aggressiveness": aggressiveness,
                        "slice_qty": slice_qty,
                        "cancel_replace": True,
                        "max_reprice_attempts": 3,
                    }
                )

            # audit per input order (not per slice)
            con.execute(
                """
                INSERT INTO execution_policy_audit(
                  ts_ms, signal_id, symbol, side, qty, age_ms, ttl_ms, volatility, source_order_id, policy_json
                )
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    now_ms,
                    str(o.get("signal_id") or ""),
                    symbol,
                    side,
                    qty,
                    age_ms,
                    ttl_ms,
                    volatility,
                    source_order_id_i,
                    json.dumps(
                        {
                            "age_ratio": age_ratio,
                            "slice_pct": slice_pct,
                            "slice_qty": slice_qty,
                            "slices": slices,
                            "order_type": order_type,
                            "aggressiveness": aggressiveness,
                            "cancel_replace": True,
                            "max_reprice_attempts": 3,
                            "kill_switch_reason": ks_reason,
                            "kill_switch_meta": ks_meta,
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
            )

        con.commit()
        return shaped

    finally:
        try:
            con.close()
        except Exception:
            pass
