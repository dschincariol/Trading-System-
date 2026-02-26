# dev_core/execution_microstructure.py
"""
Phase 2: Execution Microstructure Layer

Responsibilities:
- Maintain open order registry (cancel/replace state)
- Reprice limit orders based on attempts + aggressiveness
- Fail-soft: never throws; never blocks other jobs
"""

import json
import os
import time
from typing import Any, Dict, Optional

from engine.runtime.storage import connect
from engine.execution.trade_attribution_ledger import upsert_from_latest_pnl_attribution_snapshot
from engine.execution_ledger import log_submit

REPRICE_INTERVAL_S = float(os.environ.get("EPE_REPRICE_INTERVAL_S", "60"))
REPRICE_STEP_BPS = float(os.environ.get("EPE_REPRICE_STEP_BPS", "5.0"))
MAX_OPEN_ORDERS_PER_PASS = int(os.environ.get("EPE_MAX_OPEN_ORDERS_PER_PASS", "50"))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ensure_tables(con) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS exec_open_orders (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts_ms INTEGER NOT NULL,
          updated_ts_ms INTEGER NOT NULL,
          broker TEXT NOT NULL,
          symbol TEXT NOT NULL,
          qty REAL NOT NULL,
          side TEXT,
          order_type TEXT NOT NULL,
          aggressiveness TEXT,
          limit_px REAL,
          client_order_id TEXT,
          broker_order_id TEXT,
          status TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0,
          max_attempts INTEGER NOT NULL DEFAULT 0,
          next_action_ts_ms INTEGER NOT NULL DEFAULT 0,
          portfolio_orders_id INTEGER,
          source_alert_id INTEGER,
          meta_json TEXT NOT NULL
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_exec_open_orders_status_next ON exec_open_orders(status, next_action_ts_ms)"
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_exec_open_orders_broker ON exec_open_orders(broker)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_exec_open_orders_symbol ON exec_open_orders(symbol)")

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS exec_order_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts_ms INTEGER NOT NULL,
          open_order_id INTEGER,
          event TEXT NOT NULL,
          details_json TEXT NOT NULL
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_exec_order_events_ts ON exec_order_events(ts_ms)")


def record_open_order(
    *,
    broker: str,
    symbol: str,
    qty: float,
    order_type: str,
    aggressiveness: str,
    limit_px: Optional[float],
    client_order_id: str,
    broker_order_id: Optional[str],
    max_attempts: int,
    portfolio_orders_id: Optional[int] = None,
    source_alert_id: Optional[int] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    """Best-effort open-order registry insert."""
    con = connect()
    try:
        _ensure_tables(con)
        now = _now_ms()
        con.execute(
            """
            INSERT INTO exec_open_orders(
              ts_ms, updated_ts_ms, broker, symbol, qty, side, order_type, aggressiveness,
              limit_px, client_order_id, broker_order_id, status, attempts, max_attempts,
              next_action_ts_ms, portfolio_orders_id, source_alert_id, meta_json
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                now,
                now,
                str(broker),
                str(symbol),
                float(qty),
                ("BUY" if float(qty) > 0 else "SELL"),
                str(order_type),
                str(aggressiveness or ""),
                float(limit_px) if limit_px is not None else None,
                str(client_order_id),
                str(broker_order_id) if broker_order_id else None,
                "open",
                0,
                int(max_attempts),
                int(now + (float(REPRICE_INTERVAL_S) * 1000.0)),
                int(portfolio_orders_id) if portfolio_orders_id is not None else None,
                int(source_alert_id) if source_alert_id is not None else None,
                json.dumps(meta or {}, separators=(",", ":"), sort_keys=True),
            ),
        )
        con.commit()
    except Exception:
        pass
    finally:
        try:
            con.close()
        except Exception:
            pass


def _log_event(con, open_order_id: int, event: str, details: Dict[str, Any]) -> None:
    try:
        con.execute(
            """
            INSERT INTO exec_order_events(ts_ms, open_order_id, event, details_json)
            VALUES (?,?,?,?)
            """,
            (
                _now_ms(),
                int(open_order_id),
                str(event),
                json.dumps(details or {}, separators=(",", ":"), sort_keys=True),
            ),
        )
    except Exception:
        pass


def _adjust_limit_px(limit_px: float, qty: float, attempt: int) -> float:
    # attempt=1 means first reprice (move further toward market)
    step = float(REPRICE_STEP_BPS) * float(max(1, attempt))
    if float(qty) > 0:
        # buy: increase limit to be more aggressive
        return float(limit_px) * (1.0 + (step / 10000.0))
    # sell: decrease limit to be more aggressive
    return float(limit_px) * (1.0 - (step / 10000.0))


def manage_open_orders() -> Dict[str, Any]:
    """
    Called periodically (e.g. from execution_poll_and_attrib.py).
    - For Alpaca only (today): checks open orders; cancel/replace as needed.
    """
    out = {"ok": True, "managed": 0, "updated": 0, "errors": 0}
    con = connect()
    try:
        _ensure_tables(con)
        now = _now_ms()

        rows = con.execute(
            """
            SELECT id, broker, symbol, qty, order_type, aggressiveness, limit_px,
                   client_order_id, broker_order_id, attempts, max_attempts,
                   next_action_ts_ms, portfolio_orders_id, source_alert_id, meta_json
            FROM exec_open_orders
            WHERE status='open' AND next_action_ts_ms <= ?
            ORDER BY next_action_ts_ms ASC
            LIMIT ?
            """,
            (int(now), int(MAX_OPEN_ORDERS_PER_PASS)),
        ).fetchall()

        if not rows:
            return {**out, "open_due": 0}

        # lazy import to avoid cycles
        try:
            from engine.broker_alpaca_rest import get_order as alpaca_get_order
            from engine.broker_alpaca_rest import cancel_order as alpaca_cancel_order
            from engine.broker_alpaca_rest import submit_limit_order as alpaca_submit_limit_order
        except Exception:
            alpaca_get_order = None
            alpaca_cancel_order = None
            alpaca_submit_limit_order = None

        for r in rows or []:
            out["managed"] += 1
            try:
                open_id = int(r[0])
                broker = str(r[1] or "").lower().strip()
                symbol = str(r[2] or "").upper().strip()
                qty = float(r[3] or 0.0)
                order_type = str(r[4] or "").upper().strip()
                limit_px = r[6]
                client_oid = str(r[7] or "")
                broker_oid = str(r[8] or "")
                attempts = int(r[9] or 0)
                max_attempts = int(r[10] or 0)
                portfolio_orders_id = r[12]
                source_alert_id = r[13]
                meta_json = str(r[14] or "{}")

                if broker != "alpaca":
                    con.execute(
                        """
                        UPDATE exec_open_orders
                        SET updated_ts_ms=?, next_action_ts_ms=?
                        WHERE id=?
                        """,
                        (int(now), int(now + (float(REPRICE_INTERVAL_S) * 1000.0)), int(open_id)),
                    )
                    out["updated"] += 1
                    continue

                if not alpaca_get_order or not alpaca_cancel_order or not alpaca_submit_limit_order:
                    out["errors"] += 1
                    con.execute(
                        """
                        UPDATE exec_open_orders
                        SET updated_ts_ms=?, next_action_ts_ms=?
                        WHERE id=?
                        """,
                        (int(now), int(now + (float(REPRICE_INTERVAL_S) * 1000.0)), int(open_id)),
                    )
                    continue

                if not broker_oid:
                    con.execute(
                        """
                        UPDATE exec_open_orders
                        SET updated_ts_ms=?, status='error', next_action_ts_ms=?
                        WHERE id=?
                        """,
                        (int(now), int(now + (float(REPRICE_INTERVAL_S) * 1000.0)), int(open_id)),
                    )
                    _log_event(con, open_id, "missing_broker_order_id", {"client_order_id": client_oid})
                    out["updated"] += 1
                    continue

                oinfo = alpaca_get_order(str(broker_oid)) or {}
                st = str(oinfo.get("status") or "").lower().strip()

                if st in ("filled", "canceled", "rejected", "expired"):
                    con.execute(
                        """
                        UPDATE exec_open_orders
                        SET updated_ts_ms=?, status=?, next_action_ts_ms=0
                        WHERE id=?
                        """,
                        (int(now), str(st), int(open_id)),
                    )
                    _log_event(con, open_id, "closed", {"status": st, "broker_order_id": broker_oid})
                    out["updated"] += 1
                    continue

                if order_type != "LIMIT" or limit_px is None:
                    con.execute(
                        """
                        UPDATE exec_open_orders
                        SET updated_ts_ms=?, next_action_ts_ms=?
                        WHERE id=?
                        """,
                        (int(now), int(now + (float(REPRICE_INTERVAL_S) * 1000.0)), int(open_id)),
                    )
                    out["updated"] += 1
                    continue

                if max_attempts <= 0 or attempts >= max_attempts:
                    con.execute(
                        """
                        UPDATE exec_open_orders
                        SET updated_ts_ms=?, status='gave_up', next_action_ts_ms=0
                        WHERE id=?
                        """,
                        (int(now), int(open_id)),
                    )
                    _log_event(con, open_id, "gave_up", {"attempts": attempts, "max_attempts": max_attempts})
                    out["updated"] += 1
                    continue

                try:
                    alpaca_cancel_order(str(broker_oid))
                except Exception:
                    pass

                new_attempt = attempts + 1
                new_limit = _adjust_limit_px(float(limit_px), float(qty), new_attempt)
                new_client_oid = f"{client_oid}_r{new_attempt}"

                res = alpaca_submit_limit_order(
                    symbol=symbol,
                    qty=float(qty),
                    limit_price=float(new_limit),
                    client_oid=new_client_oid,
                ) or {}
                new_broker_oid = str(res.get("id") or "") or None

                try:
                    log_submit(
                        client_order_id=new_client_oid,
                        broker="alpaca",
                        symbol=symbol,
                        qty=float(qty),
                        submit_ts_ms=int(_now_ms()),
                        ref_px=float(new_limit),
                        broker_order_id=str(new_broker_oid) if new_broker_oid else None,
                        portfolio_orders_id=int(portfolio_orders_id) if portfolio_orders_id is not None else None,
                        source_alert_id=int(source_alert_id) if source_alert_id is not None else None,
                        extra={
                            "reprice_attempt": int(new_attempt),
                            "prev_client_order_id": client_oid,
                            "meta": json.loads(meta_json or "{}"),
                        },
                    )
                except Exception:
                    pass

                con.execute(
                    """
                    UPDATE exec_open_orders
                    SET updated_ts_ms=?,
                        client_order_id=?,
                        broker_order_id=?,
                        attempts=?,
                        limit_px=?,
                        next_action_ts_ms=?
                    WHERE id=?
                    """,
                    (
                        int(now),
                        str(new_client_oid),
                        str(new_broker_oid) if new_broker_oid else None,
                        int(new_attempt),
                        float(new_limit),
                        int(now + (float(REPRICE_INTERVAL_S) * 1000.0)),
                        int(open_id),
                    ),
                )

                _log_event(
                    con,
                    open_id,
                    "replaced",
                    {"attempt": new_attempt, "limit_px": float(new_limit), "broker_order_id": new_broker_oid},
                )
                out["updated"] += 1

            except Exception:
                out["errors"] += 1
                continue

        con.commit()
        return {**out, "open_due": len(rows)}

    finally:
        try:
            con.close()
        except Exception:
            pass
