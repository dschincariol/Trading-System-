# dev_core/broker_ibkr_gateway.py
"""
IBKR (Interactive Brokers) IB Gateway / TWS adapter using ibapi.

Supports:
- apply_latest_portfolio_orders_live(dry_run=False)
- poll_and_log_fills(after_ts_ms)

Env:
  IBKR_HOST=127.0.0.1
  IBKR_PORT=7497        (paper) or 7496 (live) depending on your Gateway/TWS config
  IBKR_CLIENT_ID=42

  IBKR_ORDER_TIF=DAY
  IBKR_MAX_ORDERS_PER_PASS=25
  IBKR_SLEEP_BETWEEN_ORDERS_S=0.25

Notes:
- You must run IB Gateway or TWS locally (or reachable) with API enabled.
"""

import json
import os
import time
import threading
from typing import Any, Dict, List, Optional, Tuple

from dev_core.storage import connect
from dev_core.kill_switch import execution_allowed
from dev_core.risk_state import get_state, set_state
from dev_core.execution_ledger import log_submit, log_fill

IBKR_HOST = os.environ.get("IBKR_HOST", "127.0.0.1").strip()
IBKR_PORT = int(os.environ.get("IBKR_PORT", "7497"))
IBKR_CLIENT_ID = int(os.environ.get("IBKR_CLIENT_ID", "42"))

ORDER_TIF = os.environ.get("IBKR_ORDER_TIF", "DAY").strip().upper()
MAX_ORDERS_PER_PASS = int(os.environ.get("IBKR_MAX_ORDERS_PER_PASS", "25"))
SLEEP_BETWEEN_ORDERS_S = float(os.environ.get("IBKR_SLEEP_BETWEEN_ORDERS_S", "0.25"))


def _latest_order_row(con) -> Optional[Tuple[int, int, list]]:
    row = con.execute(
        "SELECT id, ts_ms, orders_json FROM portfolio_orders ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    oid = int(row[0])
    ts_ms = int(row[1] or 0)
    orders = json.loads(row[2] or "[]") if row[2] else []
    return oid, ts_ms, orders


def _price_at_or_before(con, symbol: str, ts_ms: int) -> Optional[float]:
    try:
        r = con.execute(
            """
            SELECT px
            FROM prices
            WHERE symbol=? AND ts_ms <= ?
            ORDER BY ts_ms DESC
            LIMIT 1
            """,
            (str(symbol), int(ts_ms)),
        ).fetchone()
        if not r:
            return None
        return float(r[0])
    except Exception:
        return None


def _target_qty(eq: float, px: float, to_side: str, to_w: float) -> float:
    qty = (float(to_w) * float(eq)) / float(px)
    if str(to_side).upper() == "SHORT":
        return -abs(qty)
    if str(to_side).upper() == "LONG":
        return abs(qty)
    return 0.0


# ----------------------------
# Minimal ibapi client wrapper
# ----------------------------
def _connect_ib():
    from ibapi.client import EClient
    from ibapi.wrapper import EWrapper
    from ibapi.contract import Contract
    from ibapi.order import Order

    class App(EWrapper, EClient):
        def __init__(self):
            EClient.__init__(self, self)
            self._next_order_id = None
            self._next_order_evt = threading.Event()
            self._exec = []
            self._exec_lock = threading.Lock()
            self._err = []
            self._err_lock = threading.Lock()

        def nextValidId(self, orderId: int):
            self._next_order_id = int(orderId)
            self._next_order_evt.set()

        def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
            with self._err_lock:
                self._err.append(
                    {"reqId": reqId, "code": errorCode, "msg": errorString, "adv": advancedOrderRejectJson}
                )

        def execDetails(self, reqId, contract, execution):
            with self._exec_lock:
                self._exec.append(
                    {
                        "symbol": getattr(contract, "symbol", None),
                        "orderId": getattr(execution, "orderId", None),
                        "permId": getattr(execution, "permId", None),
                        "clientId": getattr(execution, "clientId", None),
                        "time": getattr(execution, "time", None),
                        "shares": getattr(execution, "shares", None),
                        "price": getattr(execution, "price", None),
                    }
                )

    app = App()
    app.connect(IBKR_HOST, IBKR_PORT, clientId=IBKR_CLIENT_ID)

    t = threading.Thread(target=app.run, daemon=True)
    t.start()

    if not app._next_order_evt.wait(timeout=10):
        raise RuntimeError("IBKR: nextValidId not received (check Gateway/TWS API settings)")

    return app


def _mk_stock_contract(symbol: str):
    from ibapi.contract import Contract

    c = Contract()
    c.symbol = str(symbol).upper().strip()
    c.secType = "STK"
    c.exchange = "SMART"
    c.currency = "USD"
    return c


def _mk_market_order(qty: float):
    from ibapi.order import Order

    o = Order()
    o.orderType = "MKT"
    o.totalQuantity = abs(float(qty))
    o.action = "BUY" if float(qty) > 0 else "SELL"
    o.tif = ORDER_TIF
    return o


def apply_latest_portfolio_orders_live(dry_run: bool = False) -> Dict[str, Any]:
    con = connect()
    try:
        latest = _latest_order_row(con)
        if not latest:
            return {"ok": True, "status": "no_orders"}

        order_id, ts_ms, orders = latest

        last_applied = get_state("ibkr_last_portfolio_orders_id", "0")
        try:
            if int(last_applied) >= int(order_id):
                return {"ok": True, "status": "already_applied", "order_id": int(order_id)}
        except Exception:
            pass

        allow0, _, _ = execution_allowed(con=con, symbol=None, regime=None)
        if not allow0:
            return {"ok": False, "status": "blocked_kill_switch_global", "order_id": int(order_id)}

        # equity from your DB prices + weights model isn't enough here; we mirror Alpaca approach:
        # Use latest broker equity via your own account service (not available in ibapi easily).
        # For safety, require user-provided equity cap in env.
        eq = float(os.environ.get("IBKR_EQUITY_USD", "0") or 0.0)
        if eq <= 0:
            return {"ok": False, "status": "missing_equity", "hint": "set IBKR_EQUITY_USD env to a positive number"}

        if dry_run:
            return {"ok": True, "status": "dry_run_preview", "order_id": int(order_id), "orders": orders}

        app = _connect_ib()

        submitted = []
        n = 0

        for o in (orders or [])[: int(MAX_ORDERS_PER_PASS)]:
            symbol = str(o.get("symbol") or "").strip().upper()
            if not symbol:
                continue

            allow_sym, _, _ = execution_allowed(con=con, symbol=symbol, regime=None)
            if not allow_sym:
                continue

            to_side = str(o.get("to_side") or "FLAT").upper().strip()
            to_w = float(o.get("to_weight") or 0.0)

            px = _price_at_or_before(con, symbol, int(ts_ms))
            if px is None or px <= 0:
                continue

            tgt = _target_qty(eq=eq, px=float(px), to_side=to_side, to_w=float(to_w))

            # IBKR adapter does not fetch current position here (keep minimal).
            # We submit the full target as delta. If you want true reconciliation, add positions pull via account updates.
            delta = float(tgt)
            if abs(delta) < 1e-6:
                continue

            client_oid = f"pf_{int(order_id)}_{symbol}"

            # Submit order
            contract = _mk_stock_contract(symbol)
            order = _mk_market_order(delta)

            oid = int(app._next_order_id)
            app._next_order_id += 1

            app.placeOrder(oid, contract, order)

            # ledger submit
            try:
                log_submit(
                    client_order_id=client_oid,
                    broker="ibkr",
                    symbol=symbol,
                    qty=float(delta),
                    submit_ts_ms=int(time.time() * 1000),
                    ref_px=float(px),
                    broker_order_id=str(oid),
                    portfolio_orders_id=int(order_id),
                    source_alert_id=int(o.get("source_alert_id")) if o.get("source_alert_id") is not None else None,
                    extra={"to_side": to_side, "to_weight": float(to_w), "equity_env": float(eq)},
                )
            except Exception:
                pass

            submitted.append({"symbol": symbol, "delta_qty": float(delta), "client_order_id": client_oid, "ib_order_id": oid})
            n += 1
            time.sleep(max(0.0, float(SLEEP_BETWEEN_ORDERS_S)))

        set_state("ibkr_last_portfolio_orders_id", str(int(order_id)))
        return {"ok": True, "status": "applied", "order_id": int(order_id), "submitted_n": int(n), "submitted": submitted}

    finally:
        con.close()


def poll_and_log_fills(after_ts_ms: int) -> Dict[str, Any]:
    """
    Minimal polling stub for IBKR fills.
    Real IBKR fill capture is event-driven via execDetails; for a full implementation,
    run a long-lived process connected to Gateway that subscribes to executions and writes fills.

    This function is intentionally a no-op placeholder (safe).
    """
    return {"ok": True, "fills_logged": 0, "status": "ibkr_poll_stub"}
